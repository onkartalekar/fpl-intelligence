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
        if (decision.get("model") or {}).get("is_champion"):
            champions[str(origin_event)] = forecast_id
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
    }
