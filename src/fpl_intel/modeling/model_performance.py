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


def normalize_manager_transfers(payload):
    """Bucket a manager's full raw `/transfers/` history (issue #285) by gameweek.

    Unlike `normalize_manager_picks` (one payload per event), FPL's `/transfers/` endpoint
    returns a manager's *entire* transfer history in a single call, kept indefinitely -- so this
    takes the whole raw list at once and groups it, rather than being called once per event.

    Returns `{event_key: [{"in_id", "out_id"}, ...]}` -- only events with at least one transfer
    appear here. A finished event with zero transfers that gameweek is a real, meaningful result
    ("the manager rolled"), not missing data -- callers backfilling per-event state must
    explicitly record `[]` for any finished event absent from this dict, the same "checked, found
    nothing" vs "not yet checked" distinction `manager_picks`'s own backfill already makes.
    """
    by_event = {}
    for row in payload or []:
        event = row.get("event")
        if event is None or row.get("element_in") is None or row.get("element_out") is None:
            continue
        key = str(int(event))
        by_event.setdefault(key, []).append(
            {"in_id": int(row["element_in"]), "out_id": int(row["element_out"])}
        )
    return by_event


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


def _generated_strictly_before(generated_at, deadline_time):
    """True only when `generated_at` parses as a tz-aware timestamp strictly before
    `deadline_time`. Fail-closed: any parse failure, missing value, or naive datetime returns
    False. Mirrors the pre-deadline gate `archive_forecast` applies inline above; extracted so
    `archive_team_forecast` can apply the identical check (issue #286) without `archive_forecast`
    itself changing.
    """
    try:
        generated = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
        deadline = datetime.fromisoformat(str(deadline_time).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if generated.tzinfo is None or deadline.tzinfo is None:
        return False
    return generated < deadline


def archive_team_forecast(store, team_id, weekly_decisions, lead_hours, deadline_time=None):
    """Archive one team's real weekly transfer/captaincy decision at one deadline checkpoint
    (issue #102).

    Deliberately a separate function from `archive_forecast` above, not a generalization of it:
    `archive_forecast` archives `decision_center`'s generic, no-squad-required squad-construction
    recommendation (status `active_preliminary`/`active`, shaped around `profile_recommendations`/
    `horizons`) -- `weekly_decisions` (`build_transfer_decisions`/`build_draft_decisions`'s output,
    status `active` on a real decision) is a structurally different shape (`profiles[].
    recommendation` holding `action`/`transfer_count`/`point_cost`/`starting_xi`/`captain`/etc, no
    `horizons` concept at all). No existing function could be reused unmodified for this.

    Gated on `status == "active"` -- the only status representing a complete, real decision;
    every other status (`waiting_for_gw2`, `manager_not_configured`, `manager_squad_unavailable`,
    `scenario_unavailable`, ...) has nothing worth freezing. `lead_hours` (one of
    `server._ALLOWED_REMINDER_LEAD_HOURS`, the same three checkpoints issue #79 already exposes
    to visitors) is folded into the archive key alongside the origin event, so this team's three
    checkpoints for one gameweek are stored independently rather than colliding on a single-
    snapshot key -- captures how the recommendation evolves through the week (team news,
    injuries, price moves) rather than one point-in-time snapshot. Within one checkpoint, first
    capture wins and is never overwritten, mirroring `archive_forecast`'s own immutability
    guarantee -- a retried/duplicate call for a checkpoint already captured is a no-op.

    Stores IDs, not full player objects, matching `archive_forecast`'s own minimal-footprint
    style -- the full player catalog is already available elsewhere (`players.json`), so this
    only needs to record which player IDs the recommendation chose.

    `deadline_time` (issue #286): when supplied, a server-side backstop refusing any decision
    whose `generated_at` is at or after the event deadline -- so a caller bug, or a future
    change to the archiver's own capture window, can never silently start freezing a
    hindsight-contaminated recommendation. Optional and defaulting to None only for backward
    compatibility with existing callers/tests; the `/api/archive-team-forecast` endpoint always
    passes it. `archive_forecast` above already enforces the equivalent gate inline.

    Issue #266: also freezes each profile's `chip_recommendation` (scalars only) and its
    `multiweek_plan.conditional_branches` (trimmed to `event`/`action`/`chip_signal`) -- needed so
    a later refresh can tell "this action/chip signal was already flagged last week" from "this is
    new since last week" (`build_team_plan_diff`, below). A checkpoint archived before this field
    existed simply lacks these keys; callers must treat that as "no prior plan data," never
    reconstruct one.
    """
    if weekly_decisions.get("status") != "active" or not weekly_decisions.get("event"):
        return store
    if deadline_time is not None and not _generated_strictly_before(
        weekly_decisions.get("generated_at"), deadline_time
    ):
        return store
    profiles_in = weekly_decisions.get("profiles") or []
    if not profiles_in:
        return store
    event = int(weekly_decisions["event"])
    forecast_id = f"gw{event}:{lead_hours}"
    team_forecasts = store.setdefault("team_forecasts", {}).setdefault(str(team_id), {})
    if forecast_id in team_forecasts:
        return store
    profiles_out = []
    for profile in profiles_in:
        recommendation = profile.get("recommendation") or {}
        chip_recommendation = profile.get("chip_recommendation") or {}
        captain = recommendation.get("captain") or {}
        vice_captain = recommendation.get("vice_captain") or {}
        starting_xi = recommendation.get("starting_xi") or []
        bench = recommendation.get("bench") or []
        if not starting_xi or captain.get("id") is None:
            continue
        profiles_out.append(
            {
                "profile_id": profile.get("id"),
                "action": recommendation.get("action"),
                "transfer_count": recommendation.get("transfer_count"),
                "point_cost": recommendation.get("point_cost"),
                "net_gain_5gw": recommendation.get("net_gain_5gw"),
                "projected_event_points_including_captain": recommendation.get(
                    "projected_event_points_including_captain"
                ),
                "formation": recommendation.get("formation"),
                "lineup_player_ids": [int(player["id"]) for player in starting_xi],
                "bench_player_ids": [int(player["id"]) for player in bench],
                "captain_id": int(captain["id"]),
                "vice_captain_id": (
                    int(vice_captain["id"]) if vice_captain.get("id") is not None else None
                ),
                # Issue #285: the explicit in/out player pairs behind this recommendation --
                # `transfer_count`/`lineup_player_ids` alone can't say *which* players a "1
                # transfer" recommendation meant, which a future recommended-vs-performed
                # comparison needs to compare player-for-player against the manager's actual
                # transfers. `_move_record` (transfer_decisions.py) always shapes each entry as
                # `{"out": {"id", ...}, "in": {"id", ...}}`; only the IDs are kept here, matching
                # this function's existing minimal-footprint style.
                "transfers": [
                    {"out_id": int(move["out"]["id"]), "in_id": int(move["in"]["id"])}
                    for move in recommendation.get("transfers") or []
                ],
                # Issue #266: required_margin/margin_above_required only exist on the
                # recommendation when a multi-transfer override actually won (see
                # transfer_decisions.py's own comment where they're attached) -- absent/None here
                # for every other action, same as any other field this function reads with .get().
                "required_margin": recommendation.get("required_margin"),
                "margin_above_required": recommendation.get("margin_above_required"),
                # Issue #266: the chip verdict and the near-future conditional plan, neither
                # previously frozen -- both needed for a later refresh to tell "this was already
                # flagged last week" from "this is new since last week." `chip_recommendation`'s
                # `threshold`/`effective_threshold`/`value_above_threshold` are the exact fields
                # #267 added to the live payload; kept as-is rather than re-derived. Trimmed to
                # scalars only (no `alternatives`, `chip_squad`) -- the archive's minimal-footprint
                # style, and neither is needed to tell what changed.
                "chip_recommendation": {
                    "action": chip_recommendation.get("action"),
                    "chip": chip_recommendation.get("chip"),
                    "marginal_value": chip_recommendation.get("marginal_value"),
                    "threshold": chip_recommendation.get("threshold"),
                    "effective_threshold": chip_recommendation.get("effective_threshold"),
                    "value_above_threshold": chip_recommendation.get("value_above_threshold"),
                },
                # Trimmed to event/action/chip_signal per the issue's own request (#266) --
                # `condition`/`point_cost`/free-transfer counts are re-derivable narrative text,
                # not needed to compute a week-over-week diff, and would only grow this payload
                # for no comparison benefit.
                "conditional_branches": [
                    {
                        "event": branch.get("event"),
                        "action": branch.get("action"),
                        "chip_signal": branch.get("chip_signal"),
                    }
                    for branch in (profile.get("multiweek_plan") or {}).get("conditional_branches") or []
                ],
            }
        )
    if not profiles_out:
        return store
    team_forecasts[forecast_id] = {
        "origin_event": event,
        "lead_hours": lead_hours,
        "generated_at": weekly_decisions.get("generated_at"),
        "profiles": profiles_out,
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


def _published_path_points(picks, actual_points_by_element):
    """The manager's actually-published XI/captain, scored with official points (issue #285).

    Same multiplier-weighted-sum shape `_team_performance` already computes inline for its own
    `actual_points` -- factored out here so this module has one definition of "what a manager's
    published picks actually scored," rather than two independently-maintained copies.
    """
    return sum(
        int(pick.get("multiplier") or 0) * int(actual_points_by_element.get(str(pick.get("element_id")), 0))
        for pick in picks
    )


def _adherence_label(recommended_transfer_count, actual_transfer_count):
    """Whether the manager's actual transfer count matches what a profile recommended.

    Issue #285 (direction (a), decided 2026-09-02): scored on transfer *count* alone, not exact
    player identity -- the frozen `recommendation["transfers"]` in/out pairs exist for a future,
    richer player-for-player comparison, but "did the manager take the recommended action" is
    itself the question this panel answers; `recommended_path_points`/`actual_path_points`/`delta`
    already carry the outcome difference regardless of which specific players were involved.

    A manager who made more transfers than the model's own menu ever considers (roll / 1 / 2 / 3+,
    `transfer_decisions.py`'s `_scenario`/`_best_double`/`_best_multi`) is flagged distinctly
    rather than scored "no" -- there was no modeled alternative for them to have followed.

    Known limitation, not addressed by this function: chip usage (`recommended_action` starting
    with `play_`, always `transfer_count == 0`) is not separately verified on the actual side --
    `manager_picks`/`manager_transfers` carry no chip signal today, so a chip recommendation and an
    actual zero-transfer roll are indistinguishable here and both score "yes". A real gap, not
    hidden: flagged in `build_team_transfer_adherence`'s returned `method` text.
    """
    if actual_transfer_count > 3:
        return "not among modeled scenarios"
    return "yes" if actual_transfer_count == recommended_transfer_count else "no"


def build_team_transfer_adherence(store, team_id):
    """One team's "recommended vs performed" transfer-adherence rows (issue #285).

    For every frozen `team_forecasts` checkpoint of a *finished* gameweek, compares each profile's
    single frozen headline recommendation (direction (a), decided 2026-09-02 -- not the full
    scenario menu, not collapsed to one profile) against what the manager actually did that
    gameweek, using only already-persisted, never-hindsight-reconstructed data:

    - Recommended side: `store["team_forecasts"][team_id]` (issue #102/#286), frozen pre-deadline.
    - Actual side: `store["manager_transfers"][team_id]` (issue #285) for the transfer count, and
      `store["manager_picks"][team_id]` (issue #64) + `store["actual_events"]` for what the
      manager's own published XI actually scored.

    A row is only produced once BOTH sides exist for that (team, event) -- a gameweek whose
    checkpoint was never archived (see issue #288's cron-reliability gaps) or whose actual side
    hasn't backfilled yet simply produces no row for that event, mirroring `_team_performance`'s
    own "no frozen forecast -> no comparison" rule rather than fabricating one.

    `recommended_transfers`/`actual_transfers` carry the raw `{"out_id", "in_id"}` pairs behind
    each side's transfer count -- IDs only, matching this store's own minimal-footprint style; the
    frontend already has the player catalog to resolve names (`renderPlayerPerformance` does the
    same lookup). `recommended_transfers` is `[]` for a checkpoint archived before
    `archive_team_forecast` started capturing it (see that function's own docstring) -- a real,
    permanent gap for anything archived before that field existed, not a bug in this function.
    """
    team_key = str(team_id)
    team_forecasts = (store.get("team_forecasts") or {}).get(team_key, {})
    actual_events = store.get("actual_events", {})
    manager_picks = (store.get("manager_picks") or {}).get(team_key, {})
    manager_transfers = (store.get("manager_transfers") or {}).get(team_key, {})
    rows = []
    for forecast in team_forecasts.values():
        event = forecast.get("origin_event")
        lead_hours = forecast.get("lead_hours")
        event_key = str(event)
        if event_key not in actual_events:
            continue  # gameweek not finished yet
        if event_key not in manager_transfers:
            continue  # actual transfers not backfilled for this event yet -- pending, not guessed
        picks = manager_picks.get(event_key)
        if not picks:
            continue  # actual published picks not backfilled for this event yet
        actual_points_by_element = actual_events[event_key]
        actual_transfer_count = len(manager_transfers[event_key])
        actual_path_points = _published_path_points(picks, actual_points_by_element)
        for profile in forecast.get("profiles", []):
            recommended_transfer_count = int(profile.get("transfer_count") or 0)
            recommended_path_points = _actual_points(
                actual_events, event, 1,
                profile.get("lineup_player_ids", []), profile.get("captain_id"),
            )
            if recommended_path_points is None:
                continue
            rows.append(
                {
                    "event": event,
                    "lead_hours": lead_hours,
                    "profile_id": profile.get("profile_id"),
                    "recommended_action": profile.get("action"),
                    "recommended_transfer_count": recommended_transfer_count,
                    "recommended_transfers": profile.get("transfers") or [],
                    "actual_transfer_count": actual_transfer_count,
                    "actual_transfers": manager_transfers[event_key],
                    "followed": _adherence_label(recommended_transfer_count, actual_transfer_count),
                    "recommended_path_points": recommended_path_points,
                    "actual_path_points": actual_path_points,
                    "delta": actual_path_points - recommended_path_points,
                }
            )
    rows.sort(
        key=lambda row: (row["event"], row["lead_hours"] or 0, row["profile_id"] or ""),
        reverse=True,
    )
    scored = [row for row in rows if row["followed"] != "not among modeled scenarios"]
    followed_count = sum(1 for row in scored if row["followed"] == "yes")
    return {
        "status": "active" if rows else "waiting_for_results",
        "rows": rows,
        "summary": {
            "count": len(rows),
            "adherence_rate": _rounded(followed_count / len(scored)) if scored else None,
            "mean_delta": _rounded(sum(row["delta"] for row in rows) / len(rows)) if rows else None,
        },
        "method": (
            "Each profile's single frozen headline recommendation (not the full scenario menu) "
            "compared with the manager's actual published XI and transfer count for the same "
            "gameweek; never recomputed with hindsight. Chip usage is not yet independently "
            "verified on the actual side, so a chip recommendation matched against a zero-"
            "transfer actual week is scored as followed even if no chip was played."
        ),
    }


def build_team_plan_diff(store, team_id, weekly_decisions):
    """Per profile, what this week's live recommendation says that last week's plan already
    anticipated -- or didn't (issue #266).

    This is a *live*, forward-facing comparison (unlike `build_team_transfer_adherence`'s
    retrospective scoring) -- `weekly_decisions` is the just-computed live decision
    (`build_transfer_decisions`/`build_draft_decisions`'s output), not something read from the
    store. Only the *prior* side comes from the store, which is why this takes both.

    The lookup is a cross-checkpoint search, not a same-key read: `archive_team_forecast` keys
    each frozen snapshot by the gameweek *being decided* at that checkpoint (`gw{event}:
    {lead_hours}`), so a future gameweek's provisional action only ever appears inside an
    *earlier* checkpoint's own `conditional_branches` -- there is no frozen record anywhere of
    "what did we predict about GW6" independent of when it was predicted. This walks every
    earlier-event checkpoint for this team, most recent first, and uses the first one whose
    `conditional_branches` names the current event for that same profile. A checkpoint gap (an
    unarchived deadline -- see issue #288's cron-reliability history) or a plan that genuinely
    never looked this far ahead simply yields no entry for that profile, never a guessed one.

    Returns raw comparison data, not composed sentences -- `decision-center.js` already has its
    own `actionLabels`/`labelFor` for turning an `action` token into display text; duplicating
    that formatting here in Python would just be a second copy to keep in sync.
    """
    if weekly_decisions.get("status") != "active" or not weekly_decisions.get("event"):
        return {"event": None, "profiles": []}
    current_event = int(weekly_decisions["event"])
    team_forecasts = (store.get("team_forecasts") or {}).get(str(team_id), {})
    prior_checkpoints = sorted(
        (
            forecast for forecast in team_forecasts.values()
            if int(forecast.get("origin_event") or 0) < current_event
        ),
        key=lambda forecast: (forecast.get("origin_event") or 0, forecast.get("lead_hours") or 0),
        reverse=True,
    )
    entries = []
    for profile in weekly_decisions.get("profiles") or []:
        profile_id = profile.get("id")
        recommendation = profile.get("recommendation") or {}
        chip = profile.get("chip_recommendation") or {}
        prior_branch = None
        for checkpoint in prior_checkpoints:
            archived_profile = next(
                (row for row in checkpoint.get("profiles", []) if row.get("profile_id") == profile_id),
                None,
            )
            if archived_profile is None:
                continue
            prior_branch = next(
                (
                    branch for branch in archived_profile.get("conditional_branches") or []
                    if branch.get("event") == current_event
                ),
                None,
            )
            if prior_branch is not None:
                break
        if prior_branch is None:
            continue
        prior_action = prior_branch.get("action")
        current_action = recommendation.get("action")
        entries.append(
            {
                "profile_id": profile_id,
                "prior_action": prior_action,
                "current_action": current_action,
                "action_changed": bool(prior_action) and bool(current_action) and prior_action != current_action,
                "chip_signal_was_flagged": bool(prior_branch.get("chip_signal")),
                "chip_now_recommended": chip.get("action") == "play",
            }
        )
    return {"event": current_event, "profiles": entries}


def build_team_model_performance(store, team_id):
    """Compute one team's request-time model-performance slice (issue #64).

    Splits back out what `build_performance_report` used to bake at refresh time for a single
    hardcoded team: `team_performance` (scored against this team's slice of the now-per-team-keyed
    `manager_picks`) and `player_performance` (already team-independent, but grouped here since it
    was previously returned alongside `team_performance` and is just as cheap to compute on
    demand). Mirrors `compute_manager_view`'s per-request role in `refresh.py`.

    Issue #285: `transfer_adherence` joins this same request-time splice -- cheap enough to
    compute fresh per request, like its two siblings, rather than precomputed at refresh time.
    """
    return {
        "team_performance": _team_performance(store, team_id),
        "player_performance": _player_performance(store),
        "transfer_adherence": build_team_transfer_adherence(store, team_id),
    }
