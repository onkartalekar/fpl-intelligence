"""Immutable projection snapshots and post-gameweek model evaluation."""

import math
from datetime import datetime


_HORIZONS = (1, 3, 5)
_MIN_CALIBRATION_COMPARISONS = 8


def normalize_live_event(payload):
    """Return official event points keyed by stable FPL element ID."""
    return {
        str(row["id"]): int((row.get("stats") or {}).get("total_points") or 0)
        for row in payload.get("elements", [])
        if row.get("id") is not None
    }


def normalize_manager_picks(payload):
    """Return a manager's published picks as compact element/multiplier/captain rows."""
    return [
        {
            "element_id": int(pick["element"]),
            "multiplier": int(pick.get("multiplier") or 0),
            "is_captain": bool(pick.get("is_captain")),
        }
        for pick in (payload or {}).get("picks", [])
        if pick.get("element") is not None
    ]


def migrate_manager_picks(store, team_id):
    """Reshape a pre-#64 single-team `manager_picks` store in place, idempotently.

    Before issue #64, `manager_picks` was `{event_key: picks}` for whichever one team
    `config/user-profile.json` configured. It is now `{team_id: {event_key: picks}}`, so that
    many teams with a saved #45 profile can each accumulate their own history. The two shapes are
    reliably distinguishable: old values are pick lists, new values are per-event dicts. A no-op
    once already migrated, when there's nothing to migrate, or when there's no known `team_id` to
    attribute the old flat data to (it is then simply left alone -- stale, but harmless, since it
    won't match any team's lookup key).
    """
    manager_picks = store.get("manager_picks")
    if not manager_picks or team_id is None:
        return store
    if all(isinstance(value, list) for value in manager_picks.values()):
        store["manager_picks"] = {str(team_id): manager_picks}
    return store


def archive_forecast(store, decision, deadline_time=None):
    """Archive the first pre-result forecast for an origin event without rewriting it."""
    if decision.get("status") != "active_preliminary" or not decision.get("event"):
        return store
    if not deadline_time:
        return store
    try:
        generated = datetime.fromisoformat(str(decision.get("generated_at", "")).replace("Z", "+00:00"))
        deadline = datetime.fromisoformat(str(deadline_time).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return store
    if generated.tzinfo is None or deadline.tzinfo is None:
        return store
    if generated >= deadline:
        return store
    forecasts = store.setdefault("forecasts", [])
    origin_event = int(decision["event"])
    model_version = (decision.get("model") or {}).get("version")
    forecast_id = f"gw{origin_event}:{model_version or 'unversioned'}"
    if any(
        int(row.get("origin_event") or 0) == origin_event
        and row.get("model_version") == model_version
        for row in forecasts
    ):
        return store
    profiles = []
    for profile in decision.get("profile_recommendations", []):
        metrics = profile.get("metrics", {})
        evaluation = profile.get("evaluation_horizons", {})
        horizons = {}
        for horizon in _HORIZONS:
            key = str(horizon)
            setup = evaluation.get(key)
            modeled = metrics.get(f"central_{horizon}gw")
            if setup is None or modeled is None:
                continue
            event_lineups = []
            for event_setup in setup.get("event_lineups", []):
                if not event_setup.get("event") or event_setup.get("captain_id") is None:
                    continue
                event_lineups.append(
                    {
                        "event": int(event_setup["event"]),
                        "lineup_player_ids": [int(player_id) for player_id in event_setup.get("lineup_player_ids", [])],
                        "bench_player_ids": [int(player_id) for player_id in event_setup.get("bench_player_ids", [])],
                        "captain_id": int(event_setup["captain_id"]),
                        "vice_captain_id": (
                            int(event_setup["vice_captain_id"])
                            if event_setup.get("vice_captain_id") is not None else None
                        ),
                        "formation": event_setup.get("formation"),
                    }
                )
            horizons[key] = {
                "modeled_points": float(modeled),
                "lower_points": float(metrics.get(f"lower_{horizon}gw", modeled)),
                "upper_points": float(metrics.get(f"upper_{horizon}gw", modeled)),
                "lineup_player_ids": [int(player_id) for player_id in setup.get("lineup_player_ids", [])],
                "captain_id": int(setup["captain_id"]),
                "event_lineups": event_lineups,
            }
        if horizons:
            profiles.append(
                {
                    "profile_id": profile.get("id"),
                    "label": profile.get("label") or str(profile.get("id", "")).title(),
                    "horizons": horizons,
                }
            )
    if profiles:
        forecasts.append(
            {
                "origin_event": origin_event,
                "forecast_id": forecast_id,
                "generated_at": decision.get("generated_at"),
                "model_version": model_version,
                "profiles": profiles,
            }
        )
        champions = store.setdefault("champion_forecasts", {})
        is_champion = bool((decision.get("model") or {}).get("is_champion"))
        if is_champion:
            champions[str(origin_event)] = forecast_id
        player_forecasts = decision.get("player_forecasts")
        if player_forecasts and is_champion:
            frozen_players = store.setdefault("player_forecasts", {})
            origin_key = str(origin_event)
            if origin_key not in frozen_players:
                frozen_players[origin_key] = {
                    "forecast_id": forecast_id,
                    "model_version": model_version,
                    "generated_at": decision.get("generated_at"),
                    "players": {
                        str(row["id"]): [row.get("modeled", 0.0), row.get("lower", 0.0), row.get("upper", 0.0)]
                        for row in player_forecasts
                    },
                }
    return store


def archive_shadow_forecast(store, model_version, origin_event, generated_at, player_forecasts):
    """Archive the first pre-result per-player forecast for one non-champion `model_version`.

    Issue #65's shadow challenger(s) have no squad/profile concept of their own -- see
    `ml_minutes.build_shadow_forecast` -- so this is a smaller sibling of `archive_forecast`'s
    `player_forecasts` freeze, keyed by `model_version` rather than gated on `is_champion`.
    Kept fully separate from `store["player_forecasts"]`/`store["champion_forecasts"]` so the
    champion's own frozen forecasts and report shape are untouched by any number of shadow
    challengers coming and going over time. Idempotent per (model_version, origin_event), same
    "archive the first one seen, never rewrite it" discipline as `archive_forecast`.
    """
    if not model_version or not origin_event or not player_forecasts:
        return store
    shadow = store.setdefault("shadow_forecasts", {}).setdefault(str(model_version), {})
    origin_key = str(int(origin_event))
    if origin_key in shadow:
        return store
    shadow[origin_key] = {
        "model_version": model_version,
        "generated_at": generated_at,
        "players": {
            str(row["id"]): [row.get("modeled", 0.0), row.get("lower", 0.0), row.get("upper", 0.0)]
            for row in player_forecasts
        },
    }
    return store


def _actual_points(actual_events, origin_event, horizon, lineup_ids, captain_id, event_lineups=None):
    event_ids = range(origin_event, origin_event + horizon)
    if not all(str(event_id) in actual_events for event_id in event_ids):
        return None
    schedule = {
        int(row["event"]): row
        for row in (event_lineups or [])
        if row.get("event") is not None
    }
    total = 0
    for event_id in event_ids:
        points = actual_events[str(event_id)]
        event_setup = schedule.get(event_id, {})
        selected_ids = event_setup.get("lineup_player_ids", lineup_ids)
        selected_captain = event_setup.get("captain_id", captain_id)
        total += sum(int(points.get(str(player_id), 0)) for player_id in selected_ids)
        total += int(points.get(str(selected_captain), 0))
    return total


def _rounded(value):
    return round(value, 2)


def _summarize(rows):
    if not rows:
        return {"count": 0, "mae": None, "bias": None, "rmse": None, "range_coverage": None}
    errors = [row["error"] for row in rows]
    return {
        "count": len(rows),
        "mae": _rounded(sum(abs(error) for error in errors) / len(errors)),
        "bias": _rounded(sum(errors) / len(errors)),
        "rmse": _rounded(math.sqrt(sum(error * error for error in errors) / len(errors))),
        "range_coverage": _rounded(sum(row["inside_range"] for row in rows) / len(rows)),
    }


def _calibration(summary, comparisons):
    count = len({row["origin_event"] for row in comparisons})
    if count < _MIN_CALIBRATION_COMPARISONS:
        return {
            "ready": False,
            "completed_origin_events": count,
            "status": f"More completed forecasts are needed before calibration ({count}/{_MIN_CALIBRATION_COMPARISONS}).",
            "recommendations": [
                "Keep forecasts immutable and collect actual points after each finished Gameweek.",
                "Do not retune the model from a single result or short hot/cold streak.",
            ],
        }
    recommendations = []
    if summary["bias"] > 2:
        recommendations.append("Actual points are running above projections; review scoring-rate and minutes assumptions for underprediction.")
    elif summary["bias"] < -2:
        recommendations.append("Actual points are running below projections; review scoring-rate and minutes assumptions for overprediction.")
    if summary["range_coverage"] < 0.7:
        recommendations.append("Observed range coverage is low; widen or re-estimate uncertainty intervals.")
    if not recommendations:
        recommendations.append("Calibration is broadly stable; keep collecting data before changing model weights.")
    return {
        "ready": True,
        "completed_origin_events": count,
        "status": "Calibration diagnostics are active. Changes remain reviewable rather than automatic.",
        "recommendations": recommendations,
    }


def _score_frozen_player_forecasts(frozen_forecasts, actual_events):
    """Compare a store of frozen per-player forecasts (keyed by origin event) with official
    per-player actuals. Shared by `_player_performance` (the champion's own
    `player_forecasts`) and `_shadow_model_performance` (one non-champion `model_version`'s
    `shadow_forecasts` entry, issue #65) so both apply the exact same cohort rule and error
    metrics -- a player only enters the comparison set for an origin event if the model gave
    them a positive modeled score or they actually scored, which keeps untouched fringe
    players from diluting the error metrics with trivial correct-zero predictions.
    """
    comparisons = []
    scored_events = []
    for origin_key, frozen in frozen_forecasts.items():
        if origin_key not in actual_events:
            continue
        event = int(origin_key)
        scored_events.append(event)
        actual = actual_events[origin_key]
        for element_id, values in frozen.get("players", {}).items():
            modeled, lower, upper = (float(values[0]), float(values[1]), float(values[2]))
            actual_points = int(actual.get(element_id, 0))
            if modeled <= 0 and actual_points == 0:
                continue
            comparisons.append(
                {
                    "event": event,
                    "element_id": int(element_id),
                    "modeled_points": modeled,
                    "actual_points": actual_points,
                    "error": _rounded(actual_points - modeled),
                    "absolute_error": _rounded(abs(actual_points - modeled)),
                    "lower_points": lower,
                    "upper_points": upper,
                    "inside_range": lower <= actual_points <= upper,
                }
            )
    comparisons.sort(key=lambda row: (row["event"], row["element_id"]), reverse=True)
    return comparisons, sorted(scored_events)


def _player_performance(store):
    """Compare the champion's own frozen per-player forecasts with official per-player actuals."""
    comparisons, scored_events = _score_frozen_player_forecasts(
        store.get("player_forecasts", {}), store.get("actual_events", {})
    )
    return {
        "status": "active" if comparisons else "waiting_for_results",
        "events": scored_events,
        "comparisons": comparisons,
        "summary": _summarize(comparisons),
    }


def _shadow_model_performance(store, model_version):
    """Score one non-champion `model_version`'s frozen shadow forecasts (issue #65).

    Uses the exact same cohort rule and error metrics as the champion's own
    `_player_performance`, via `_score_frozen_player_forecasts` -- but reads from
    `store["shadow_forecasts"][model_version]`, entirely separate from the champion's
    `player_forecasts`/`build_performance_report` numbers. Deliberately kept apart per
    plans/issue-65-ml-shadow-model.md: the champion's own calibration gate
    (`_MIN_CALIBRATION_COMPARISONS`, "don't retune from a small sample") is champion-specific
    and would be corrupted by challenger rows blended in.
    """
    comparisons, scored_events = _score_frozen_player_forecasts(
        store.get("shadow_forecasts", {}).get(model_version, {}), store.get("actual_events", {})
    )
    return {
        "model_version": model_version,
        "status": "active" if comparisons else "waiting_for_results",
        "events": scored_events,
        "comparisons": comparisons,
        "summary": _summarize(comparisons),
    }


def build_shadow_performance_report(store):
    """One performance report per non-champion `model_version` currently tracked in shadow.

    Additive alongside `build_performance_report`'s champion-only numbers (issue #65) --
    driven entirely by whatever `model_version` keys `archive_shadow_forecast` has recorded
    into `store["shadow_forecasts"]`, so a new challenger needs no change here to start
    being scored, and a retired one simply stops appearing once its forecasts age out of the
    store (nothing here removes stored data itself).
    """
    return {
        model_version: _shadow_model_performance(store, model_version)
        for model_version in sorted(store.get("shadow_forecasts", {}).keys())
    }


def _team_performance(store, team_id):
    """Score one team's own published picks against frozen per-player forecasts.

    Only events with BOTH published picks (facts) and a frozen pre-deadline
    forecast (never reconstructed with hindsight) are scored; an event
    missing a frozen forecast yields no comparison rather than one derived
    after the fact.

    `manager_picks` is keyed per team ID (issue #64) -- `team_id` selects which team's slice to
    score; a team with no collected picks yet (or an unrecognized team_id) simply yields no
    comparisons, the same "waiting for results" shape as before this store became multi-team.
    """
    manager_picks = (store.get("manager_picks") or {}).get(str(team_id), {})
    actual_events = store.get("actual_events", {})
    frozen_forecasts = store.get("player_forecasts", {})
    comparisons = []
    for event_key, picks in manager_picks.items():
        if event_key not in actual_events:
            continue
        frozen = frozen_forecasts.get(event_key)
        if not frozen:
            continue
        actual = actual_events[event_key]
        players = frozen.get("players", {})
        modeled_points = 0.0
        lower_points = 0.0
        upper_points = 0.0
        actual_points = 0
        for pick in picks:
            multiplier = int(pick.get("multiplier") or 0)
            element_id = str(pick.get("element_id"))
            values = players.get(element_id, [0.0, 0.0, 0.0])
            modeled_points += multiplier * float(values[0])
            lower_points += multiplier * float(values[1])
            upper_points += multiplier * float(values[2])
            actual_points += multiplier * int(actual.get(element_id, 0))
        comparisons.append(
            {
                "event": int(event_key),
                "modeled_points": _rounded(modeled_points),
                "lower_points": _rounded(lower_points),
                "upper_points": _rounded(upper_points),
                "actual_points": actual_points,
                "error": _rounded(actual_points - modeled_points),
                "absolute_error": _rounded(abs(actual_points - modeled_points)),
                "inside_range": lower_points <= actual_points <= upper_points,
            }
        )
    comparisons.sort(key=lambda row: row["event"], reverse=True)
    return {
        "status": "active" if comparisons else "waiting_for_results",
        "comparisons": comparisons,
        "summary": _summarize(comparisons),
        "method": (
            "Published official picks and multipliers scored with pre-deadline frozen "
            "per-player projections; no autosubs are simulated on either side."
        ),
    }


def build_performance_report(store):
    actual_events = store.get("actual_events", {})
    comparisons = []
    possible = 0
    champions = store.get("champion_forecasts", {})
    for forecast in store.get("forecasts", []):
        origin_key = str(forecast["origin_event"])
        champion_id = champions.get(origin_key)
        if not champion_id or forecast.get("forecast_id") != champion_id:
            continue
        origin_event = int(forecast["origin_event"])
        for profile in forecast.get("profiles", []):
            for key, horizon_setup in profile.get("horizons", {}).items():
                possible += 1
                horizon = int(key)
                actual = _actual_points(
                    actual_events,
                    origin_event,
                    horizon,
                    horizon_setup.get("lineup_player_ids", []),
                    horizon_setup.get("captain_id"),
                    horizon_setup.get("event_lineups"),
                )
                if actual is None:
                    continue
                modeled = float(horizon_setup["modeled_points"])
                lower = float(horizon_setup["lower_points"])
                upper = float(horizon_setup["upper_points"])
                comparisons.append(
                    {
                        "origin_event": origin_event,
                        "through_event": origin_event + horizon - 1,
                        "profile_id": profile.get("profile_id"),
                        "profile_label": profile.get("label"),
                        "horizon": horizon,
                        "modeled_points": modeled,
                        "actual_points": actual,
                        "error": _rounded(actual - modeled),
                        "absolute_error": _rounded(abs(actual - modeled)),
                        "lower_points": lower,
                        "upper_points": upper,
                        "inside_range": lower <= actual <= upper,
                        "model_version": forecast.get("model_version"),
                        "forecast_generated_at": forecast.get("generated_at"),
                    }
                )
    comparisons.sort(key=lambda row: (row["origin_event"], row["horizon"], row["profile_id"] or ""), reverse=True)
    summary = _summarize(comparisons)
    by_horizon = {
        str(horizon): _summarize([row for row in comparisons if row["horizon"] == horizon])
        for horizon in _HORIZONS
    }
    by_profile = {
        profile_id: _summarize([row for row in comparisons if row["profile_id"] == profile_id])
        for profile_id in ("conservative", "balanced", "aggressive")
    }
    # `player_performance`/`team_performance` deliberately no longer live here (issue #64) --
    # `team_performance` is inherently per-team now that `manager_picks` is keyed by team ID, and
    # both are cheap enough to compute fresh per request rather than precomputed for every saved
    # profile on every refresh. See `build_team_model_performance` below, spliced in at request
    # time by `server.py`'s `_serve_dashboard` the same way `state["manager"]`/`state["profile"]`
    # already are for issues #45/#46.
    return {
        "status": "active" if comparisons else "waiting_for_results",
        "method": "Frozen pre-event profile XI and captain compared with official FPL event points; no hindsight substitutions or autosubs.",
        "completed_comparisons": len(comparisons),
        "pending_comparisons": max(0, possible - len(comparisons)),
        "actual_events_collected": len(actual_events),
        "comparisons": comparisons,
        "summary": summary,
        "by_horizon": by_horizon,
        "by_profile": by_profile,
        "calibration": _calibration(summary, comparisons),
        # Issue #65: every non-champion `model_version` currently running in shadow, scored
        # the same way as the champion above but kept in its own key -- never blended into
        # `summary`/`by_horizon`/`by_profile`/`calibration`, which stay champion-only exactly
        # as before this key was added. See build_shadow_performance_report.
        "shadow_models": build_shadow_performance_report(store),
    }


def build_team_model_performance(store, team_id):
    """Compute one team's request-time model-performance slice (issue #64).

    Splits back out what `build_performance_report` used to bake at refresh time for a single
    hardcoded team: `team_performance` (scored against this team's slice of the now-per-team-keyed
    `manager_picks`) and `player_performance` (already team-independent, but grouped here since it
    was previously returned alongside `team_performance` and is just as cheap to compute on
    demand). Mirrors `compute_manager_view`'s per-request role in `refresh.py`.
    """
    return {
        "team_performance": _team_performance(store, team_id),
        "player_performance": _player_performance(store),
    }
