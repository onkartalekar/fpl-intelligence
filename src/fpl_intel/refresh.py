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
from .model_performance import (
    archive_forecast,
    build_performance_report,
    normalize_live_event,
    normalize_manager_picks,
)
from .recommendations import build_gw_recommendations
from .transfer_decisions import build_transfer_decisions
from .relevance import enrich_transfers, summarize_clubs
from .transfers import canonical_club, normalize_transfer


class RefreshAlreadyRunning(RuntimeError):
    """Another process currently owns the project refresh lock."""


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
    team_id = profile.get("manager", {}).get("team_id")
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
        if team_id and key not in performance_store.setdefault("manager_picks", {}):
            picks_payload = provided_manager_picks.get(event_id) or provided_manager_picks.get(key)
            if picks_payload is None and bootstrap_payload is None:
                try:
                    picks_payload = fetch_manager_event_picks(team_id, event_id)
                except Exception:
                    actual_collection_errors.append(f"GW{event_id}: manager picks collection failed")
                    picks_payload = None
            if picks_payload is not None:
                performance_store["manager_picks"][key] = normalize_manager_picks(picks_payload)
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

    transfer_payload = _load_json(root / "data" / "confirmed-transfers.json")
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
