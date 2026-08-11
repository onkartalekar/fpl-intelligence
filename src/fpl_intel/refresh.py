"""Refresh the local FPL dataset and dashboard."""

from datetime import datetime
from collections import Counter
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from .catalog import build_fixture_catalog, build_player_catalog
from .dashboard import render_dashboard
from .fpl_data import (
    fetch_bootstrap,
    fetch_event_live,
    fetch_fixtures,
    summarize_bootstrap,
)
from .generation import publish_generation, resolve_artifact
from .manager_data import collect_public_manager, fetch_manager_event_picks, summarize_manager
from . import ml_minutes
from .model_performance import (
    archive_forecast,
    archive_shadow_forecast,
    build_performance_report,
    migrate_manager_picks,
    normalize_live_event,
    normalize_manager_picks,
)
from . import profiles
from .recommendations import build_gw_recommendations
from .transfer_decisions import build_draft_decisions, build_transfer_decisions
from .relevance import enrich_transfers, summarize_clubs
from .transfers import canonical_club, normalize_transfer


class RefreshAlreadyRunning(RuntimeError):
    """Another process currently owns the project refresh lock."""


# Issue #64 (C1): every team with a saved #45 profile now gets its `manager_picks` collected on
# the refresh cadence, not just one hardcoded team -- but capped per refresh run so duration stays
# bounded regardless of how many teams have saved a profile (relevant to #28's finding that
# `/api/refresh` has no time-based rate limit yet). Only teams that still have at least one
# finished-gameweek pick missing count against the cap; teams already caught up cost nothing.
# Uncapped teams simply get picked up again on the next refresh -- no persisted "resume point" is
# needed since already-collected picks are skipped, so nothing is repeated.
_MANAGER_PICKS_TEAM_CAP = 25


def _profiles_db_path(root):
    return Path(root) / "data" / "profiles.db"


def _load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_json_or(path, default):
    path = Path(path)
    return _load_json(path) if path.exists() else default


def _load_current_json(root, filename, default):
    path = resolve_artifact(root, filename)
    return _load_json(path) if path.exists() else default


def _transfer_identity(record):
    return (
        (record.get("player") or "").casefold(),
        canonical_club(record.get("from_club") or ""),
        canonical_club(record.get("to_club") or ""),
    )


def _merge_transfer_candidates(candidates):
    """Normalize duplicate moves while retaining typed evidence per source URL."""
    transfers_by_key = {}
    evidence_by_key = {}
    for item in candidates:
        record = normalize_transfer(item)
        key = _transfer_identity(record)
        evidence = evidence_by_key.setdefault(key, {})
        for source in record.get("supporting_sources", []):
            if source.get("url"):
                evidence[source["url"]] = {
                    "url": source["url"],
                    "source_type": source.get("source_type") or "unknown",
                }
        for url in record.get("supporting_source_urls", []):
            evidence.setdefault(url, {"url": url, "source_type": "unknown"})
        evidence[record["source_url"]] = {
            "url": record["source_url"],
            "source_type": record["source_type"],
        }
        record["supporting_sources"] = sorted(evidence.values(), key=lambda source: source["url"])
        record["supporting_source_urls"] = [source["url"] for source in record["supporting_sources"]]
        transfers_by_key[key] = record
    return sorted(
        transfers_by_key.values(),
        key=lambda row: (row.get("announced_at") or "", row["player"]),
        reverse=True,
    )


def _record_actual_collection_attempt(store, event_id, attempted_at, error=None):
    """Persist sanitized result-collection history across refreshes."""
    key = str(int(event_id))
    health = store.setdefault("actual_event_collection", {}).setdefault(
        key,
        {"status": "pending", "attempt_count": 0, "failure_count": 0},
    )
    health["attempt_count"] = int(health.get("attempt_count", 0)) + 1
    health["last_attempt_at"] = attempted_at
    if error is None:
        health["status"] = "ok"
        health["last_success_at"] = attempted_at
        health["last_error_type"] = None
    else:
        health["status"] = "error"
        health["failure_count"] = int(health.get("failure_count", 0)) + 1
        health["last_failure_at"] = attempted_at
        health["last_error_type"] = type(error).__name__
    return health


def _changes_since_previous(previous_state, previous_bootstrap, transfers, bootstrap):
    previous_moves = {_transfer_identity(row) for row in previous_state.get("transfers", [])}
    current_moves = {_transfer_identity(row) for row in transfers}
    previous_players = {row.get("id"): row for row in previous_bootstrap.get("elements", [])}
    current_players = {row.get("id"): row for row in bootstrap.get("elements", [])}
    shared_ids = previous_players.keys() & current_players.keys()
    return {
        "new_confirmed_transfers": len(current_moves - previous_moves),
        "new_fpl_players": len(current_players.keys() - previous_players.keys()),
        "club_mapping_changes": sum(
            previous_players[player_id].get("team") != current_players[player_id].get("team")
            for player_id in shared_ids
        ),
        "availability_changes": sum(
            previous_players[player_id].get("status") != current_players[player_id].get("status")
            for player_id in shared_ids
        ),
        "material_expected_minutes_changes": 0,
        "expected_minutes_tracking": "not_active_until_target_season_model",
        "has_previous_snapshot": bool(previous_state or previous_bootstrap),
    }


@contextmanager
def project_refresh_lock(root):
    """Hold the project-wide non-blocking refresh lock."""
    lock_path = Path(root) / ".refresh.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RefreshAlreadyRunning("A refresh is already running") from error
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _refresh_project_unlocked(
    root,
    bootstrap_payload=None,
    generated_at=None,
    official_transfer_records=None,
    manager_payload=None,
    fixture_payload=None,
    event_live_payloads=None,
    manager_picks_payloads=None,
    source_errors=None,
):
    root = Path(root)
    profile = _load_json_or(root / "config" / "user-profile.json", {})
    timezone_name = profile.get("manager", {}).get("timezone") or "America/New_York"
    config_team_id = profile.get("manager", {}).get("team_id")
    previous_state = _load_current_json(root, "dashboard-state.json", {})
    source_errors = dict(source_errors or {})
    if source_errors.get("transfers"):
        print(f"Transfer source refresh failed: {source_errors['transfers']}", file=sys.stderr)
    source_health = {
        "fpl": {"status": "ok", "error": None},
        "fixtures": {"status": "ok", "error": None},
        "transfers": {
            "status": "stale" if source_errors.get("transfers") else "ok",
            "error": "Transfer source refresh failed" if source_errors.get("transfers") else None,
        },
        "manager": {"status": "not_configured", "error": None},
    }
    previous_bootstrap = _load_current_json(root, "fpl-bootstrap-latest.json", {})
    performance_store = _load_current_json(
        root,
        "model-performance.json",
        {"forecasts": [], "actual_events": {}},
    )
    migrate_manager_picks(performance_store, config_team_id)
    previous_decision = previous_state.get("decision_center", {})
    previous_event = next(
        (
            event for event in previous_bootstrap.get("events", [])
            if event.get("id") == previous_decision.get("event")
        ),
        None,
    )
    if previous_event and not previous_event.get("started") and not previous_event.get("finished"):
        archive_forecast(performance_store, previous_decision, previous_event.get("deadline_time"))
    bootstrap = bootstrap_payload if bootstrap_payload is not None else fetch_bootstrap()
    generated_at = generated_at or datetime.now(ZoneInfo(timezone_name)).isoformat()
    fpl_summary = summarize_bootstrap(bootstrap, expected_first_deadline_year=2026)
    if fixture_payload is not None:
        raw_fixtures = fixture_payload
    elif bootstrap_payload is None:
        try:
            raw_fixtures = fetch_fixtures()
        except Exception as error:
            print(f"Fixture source refresh failed: {error!r}", file=sys.stderr)
            raw_fixtures = _load_current_json(root, "fpl-fixtures-latest.json", [])
            source_health["fixtures"] = {
                "status": "stale" if raw_fixtures else "error",
                "error": "Fixture source refresh failed",
            }
    else:
        raw_fixtures = []
        source_health["fixtures"] = {"status": "not_requested", "error": None}
    players = build_player_catalog(bootstrap) if fpl_summary["ready_for_2026_27"] else []
    fixtures = build_fixture_catalog(raw_fixtures, bootstrap) if fpl_summary["ready_for_2026_27"] else []
    fixture_summary = {
        "status": "ready" if fixtures else "not_active",
        "fixture_count": len(fixtures),
        "gameweek_count": len({row["event"] for row in fixtures if row.get("event")}),
        "scheduled_count": sum(1 for row in fixtures if row.get("kickoff_time")),
    }
    actual_collection_errors = []
    provided_live = event_live_payloads or {}
    provided_manager_picks = manager_picks_payloads or {}
    team_id = config_team_id
    finished_event_ids = [
        int(event["id"]) for event in bootstrap.get("events", []) if event.get("finished")
    ]
    # Read-only view for now -- deliberately not `setdefault`, so a refresh with no configured or
    # saved team leaves `manager_picks` absent from the store entirely, same as before issue #64.
    manager_picks_store = performance_store.get("manager_picks", {})
    # Issue #64: manager_picks collection is no longer just config_team_id's job -- every team
    # with a saved #45 profile gets the same season-long tracking, capped per run (see
    # _MANAGER_PICKS_TEAM_CAP). config_team_id keeps its historical role too, folded in as "one
    # more team ID that happens to have picks collected" rather than a special case.
    saved_team_ids = profiles.list_team_ids(_profiles_db_path(root))
    candidate_team_ids = list(dict.fromkeys(
        ([config_team_id] if config_team_id else []) + list(saved_team_ids)
    ))
    teams_needing_picks = [
        candidate_team_id for candidate_team_id in candidate_team_ids
        if any(
            str(event_id) not in manager_picks_store.get(str(candidate_team_id), {})
            for event_id in finished_event_ids
        )
    ]
    manager_picks_team_ids = teams_needing_picks[:_MANAGER_PICKS_TEAM_CAP]
    for event in bootstrap.get("events", []):
        if not event.get("finished"):
            continue
        event_id = int(event["id"])
        key = str(event_id)
        if key not in performance_store.setdefault("actual_events", {}):
            payload = provided_live.get(event_id) or provided_live.get(key)
            if payload is None and bootstrap_payload is None:
                try:
                    payload = fetch_event_live(event_id)
                except Exception as error:
                    _record_actual_collection_attempt(performance_store, event_id, generated_at, error)
                    actual_collection_errors.append(f"GW{event_id}: result collection failed")
                    payload = None
            if payload is not None:
                performance_store["actual_events"][key] = normalize_live_event(payload)
                _record_actual_collection_attempt(performance_store, event_id, generated_at)
        for picks_team_id in manager_picks_team_ids:
            team_key = str(picks_team_id)
            team_picks = performance_store.setdefault("manager_picks", {}).setdefault(team_key, {})
            if key in team_picks:
                continue
            team_provided = provided_manager_picks.get(picks_team_id) or provided_manager_picks.get(team_key) or {}
            picks_payload = team_provided.get(event_id) if isinstance(team_provided, dict) else None
            if picks_payload is None and isinstance(team_provided, dict):
                picks_payload = team_provided.get(key)
            if picks_payload is None and bootstrap_payload is None:
                try:
                    picks_payload = fetch_manager_event_picks(picks_team_id, event_id)
                except Exception:
                    actual_collection_errors.append(
                        f"GW{event_id}: manager picks collection failed for team {picks_team_id}"
                    )
                    picks_payload = None
            if picks_payload is not None:
                team_picks[key] = normalize_manager_picks(picks_payload)
    manager_raw = None
    manager_state = {"connection_status": "not_configured", "squad": []}
    if team_id:
        try:
            manager_raw = manager_payload if manager_payload is not None else collect_public_manager(team_id)
            if manager_payload is not None and manager_payload.get("team_id") and not manager_payload.get("entry"):
                manager_state = dict(manager_payload)
            else:
                manager_state = summarize_manager(manager_raw, bootstrap)
            source_health["manager"] = {"status": "ok", "error": None}
        except Exception as error:
            print(f"Manager source refresh failed: {error!r}", file=sys.stderr)
            manager_state = previous_state.get("manager") or manager_state
            source_health["manager"] = {
                "status": "stale" if previous_state.get("manager") else "error",
                "error": "Manager source refresh failed",
            }
        confirmed_free_transfers = profile.get("manager", {}).get("confirmed_free_transfers")
        if confirmed_free_transfers is not None:
            manager_state["confirmed_free_transfers"] = confirmed_free_transfers
            manager_state["confirmed_free_transfers_event"] = profile.get("manager", {}).get(
                "confirmed_free_transfers_event"
            )

    # Defense-in-depth (see `scripts/start_dashboard.py`'s `seed_missing_data_files`, the primary
    # fix): this file is meant to always exist -- either seeded on first boot from `data-seed/`
    # or git-tracked in a plain local checkout -- so this fallback should essentially never fire
    # in practice. It exists only to keep a refresh from hard-failing if it's ever transiently
    # missing anyway (a corrupted volume, a manual `rm`, ...), same tolerance already given to a
    # missing `fpl-fixtures-latest.json` a few lines below.
    transfer_payload = _load_json_or(
        root / "data" / "confirmed-transfers.json", {"schema_version": 1, "transfers": []}
    )
    candidates = list(transfer_payload.get("transfers", []))
    if source_errors.get("transfers"):
        candidates.extend(previous_state.get("transfers", []))
    candidates.extend(official_transfer_records or [])
    transfers = _merge_transfer_candidates(candidates)
    transfers = enrich_transfers(transfers, bootstrap, generated_at)
    relevance_counts = Counter(row["fpl_relevance"] for row in transfers)

    decision_center = {
        "status": "model_unavailable",
        "reason": "A complete current-season player and five-gameweek fixture catalog is required.",
    }
    if (
        fpl_summary["ready_for_2026_27"]
        and len(bootstrap.get("elements", [])) >= 15
        and bootstrap.get("element_types")
        and fixtures
    ):
        try:
            decision_center = build_gw_recommendations(
                bootstrap, raw_fixtures, generated_at=generated_at, horizon=5,
                recent_transfers=transfers,
            )
            decision_center["weekly_decisions"] = build_transfer_decisions(
                bootstrap, raw_fixtures, manager_state, generated_at=generated_at, horizon=5,
                recent_transfers=transfers,
            )
        except ValueError as error:
            decision_center = {"status": "model_unavailable", "reason": str(error)}

    risk = profile.get("manager", {}).get("risk_profile")
    if risk in {"conservative", "balanced", "aggressive"}:
        if decision_center.get("profile_recommendations"):
            decision_center["default_profile"] = risk
        weekly = decision_center.get("weekly_decisions")
        if isinstance(weekly, dict) and weekly.get("profiles"):
            weekly["default_profile"] = risk

    current_event = next(
        (
            event for event in bootstrap.get("events", [])
            if event.get("id") == decision_center.get("event")
        ),
        None,
    )
    if current_event and not current_event.get("started") and not current_event.get("finished"):
        archive_forecast(performance_store, decision_center, current_event.get("deadline_time"))
        # Issue #65: compute and log the ML minutes shadow challenger's own forecast for this
        # same origin event, additively -- never read by decision_center/build_gw_recommendations
        # above, so it cannot change the champion's own recommendation. Failure here must not
        # take down a refresh that otherwise succeeded; it only means shadow tracking misses one
        # event, the same tolerance already given to manager-picks/actual-event collection above.
        if decision_center.get("status") == "active_preliminary":
            try:
                shadow_forecast = ml_minutes.build_shadow_forecast(
                    bootstrap, raw_fixtures, generated_at,
                    recent_transfers=transfers, horizon=5, start_event=decision_center.get("event"),
                )
            except Exception as error:
                print(f"Shadow model computation failed: {error!r}", file=sys.stderr)
                shadow_forecast = None
            if shadow_forecast:
                archive_shadow_forecast(
                    performance_store,
                    shadow_forecast["model_version"],
                    shadow_forecast["event"],
                    generated_at,
                    shadow_forecast["player_forecasts"],
                )
    model_performance = build_performance_report(performance_store)
    model_performance["collection_errors"] = actual_collection_errors
    model_performance["collection_health"] = performance_store.get("actual_event_collection", {})

    source_payload = _load_json(root / "config" / "sources.json")
    sources = [
        {"name": source["name"], "url": source["url"].format(team_id=team_id)}
        for source in source_payload.get("sources", [])
        if "{team_id}" not in source.get("url", "") or team_id
    ]

    state = {
        "generated_at": generated_at,
        "timezone": timezone_name,
        "profile": {
            "team_id": profile.get("manager", {}).get("team_id"),
            "timezone": timezone_name,
            "confirmed_free_transfers": profile.get("manager", {}).get("confirmed_free_transfers"),
            "confirmed_free_transfers_event": profile.get("manager", {}).get("confirmed_free_transfers_event"),
            "risk_profile": profile.get("manager", {}).get("risk_profile") or "balanced",
            # Issue #78: display-only, threaded through the same spot as `risk_profile` above --
            # never fed into `risk_profile` selection or any recommendation logic. Overwritten by
            # `server.py`'s per-team splice (`_default_visitor_profile_action`, reading the real
            # profiles.db-backed `goal`) whenever a team ID is known at request time; this is only
            # the initial/no-team-known value baked into the generated dashboard-state.json.
            "goal": profile.get("manager", {}).get("goal") or "top_50k",
        },
        "fpl": fpl_summary,
        "source_health": source_health,
        "manager": manager_state,
        "players": players,
        "fixtures": fixtures,
        "fixture_summary": fixture_summary,
        "decision_center": decision_center,
        "model_performance": model_performance,
        "transfers": transfers,
        "transfer_summary": {
            "total": len(transfers),
            "high": relevance_counts.get("high", 0),
            "medium": relevance_counts.get("medium", 0),
            "low": relevance_counts.get("low", 0),
            "actionable": relevance_counts.get("high", 0) + relevance_counts.get("medium", 0),
        },
        "club_summaries": summarize_clubs(transfers, bootstrap),
        "changes_since_last_refresh": _changes_since_previous(
            previous_state, previous_bootstrap, transfers, bootstrap
        ),
        "sources": sources,
    }
    json_artifacts = {
        "fpl-bootstrap-latest.json": bootstrap,
        "official-transfers-latest.json": {"transfers": transfers},
        "model-performance.json": performance_store,
        "dashboard-state.json": state,
    }
    retained_fixtures = raw_fixtures if fixtures else _load_current_json(root, "fpl-fixtures-latest.json", None)
    if retained_fixtures is not None:
        json_artifacts["fpl-fixtures-latest.json"] = retained_fixtures
    retained_manager = manager_state if manager_raw is not None else _load_current_json(root, "fpl-manager-latest.json", None)
    if retained_manager is not None:
        json_artifacts["fpl-manager-latest.json"] = retained_manager
    publish_generation(
        root,
        generated_at=generated_at,
        json_artifacts=json_artifacts,
        dashboard_html=render_dashboard(state),
    )
    return state


def refresh_project(root, **kwargs):
    with project_refresh_lock(root):
        return _refresh_project_unlocked(root, **kwargs)


def compute_manager_view(
    bootstrap, fixtures, transfers, generated_at, team_id, horizon=5,
    confirmed_free_transfers=None, confirmed_free_transfers_event=None,
    draft_squad_ids=None,
):
    """Compute one team's manager summary and weekly decision, decoupled from the shared refresh.

    This is the per-team half split out of `_refresh_project_unlocked` for issue #46: it takes
    the shared refresh's already-fetched bootstrap/fixtures/transfers as input and computes a
    single team's view at request time, so it can serve an unauthenticated visitor's directly
    supplied team ID without waiting on (or persisting through) the periodic shared refresh.
    `fixtures` is the same raw upstream fixtures payload `build_transfer_decisions` already
    consumes for the shared refresh (see `_refresh_project_unlocked`'s `raw_fixtures`), not the
    built fixture catalog.

    `confirmed_free_transfers`/`confirmed_free_transfers_event` are issue #45's per-team saved
    override (FPL's public API doesn't always reflect a recently-used free transfer promptly) --
    mirrors the same override `_refresh_project_unlocked` already applies from
    `config/user-profile.json` for the single configured team, now available to any team with a
    saved profile. Applied before `build_transfer_decisions` runs, since it changes the
    computation itself, not just which already-computed result is shown by default.

    A registered account's stored profile (issue #45) is a second source feeding this same
    function, alongside a request-supplied `team_id` -- this deliberately does not touch
    `_refresh_project_unlocked`'s own per-manager block, which keeps its own more elaborate
    stale-fallback handling for the periodic refresh (see issue #64 for the follow-up question of
    extending that separate, season-long tracking beyond one hardcoded team).

    Network/lookup failures (unknown team ID, the official FPL API being unavailable) are
    captured into a clean, non-raising result rather than propagated, since a bad request-supplied
    team ID is an expected, frequent case here -- not the exceptional case it is for a configured
    profile's own team ID.

    `draft_squad_ids` is issue #61's saved preseason draft (15 element IDs, or None): when
    `build_transfer_decisions` reports `waiting_for_gw2` -- the season hasn't reached Gameweek 2,
    so there is no real published squad to personalize against yet -- and a draft is saved for
    this team, `build_draft_decisions` is used instead so the Weekly Decision panel shows
    personalized feedback on the manager's own declared squad rather than staying inactive. Once
    the season reaches Gameweek 2, `build_transfer_decisions`'s own gate stops applying and this
    fallback is never reached, so the real GW2+ path takes back over automatically.
    """
    try:
        manager_raw = collect_public_manager(team_id)
        manager_state = summarize_manager(manager_raw, bootstrap)
        if confirmed_free_transfers is not None:
            manager_state["confirmed_free_transfers"] = confirmed_free_transfers
            manager_state["confirmed_free_transfers_event"] = confirmed_free_transfers_event
        weekly_decisions = build_transfer_decisions(
            bootstrap, fixtures, manager_state, generated_at=generated_at, horizon=horizon,
            recent_transfers=transfers,
        )
        if weekly_decisions.get("status") == "waiting_for_gw2" and draft_squad_ids:
            weekly_decisions = build_draft_decisions(
                bootstrap, fixtures, draft_squad_ids, generated_at=generated_at, horizon=horizon,
                recent_transfers=transfers,
            )
    except Exception:
        manager_state = {
            "connection_status": "lookup_failed",
            "team_id": team_id,
            "squad": [],
            "squad_publicly_available": False,
        }
        weekly_decisions = {
            "status": "team_not_found",
            "reason": "Team not found, or the official FPL API is temporarily unavailable.",
        }
    return {"manager": manager_state, "weekly_decisions": weekly_decisions}
