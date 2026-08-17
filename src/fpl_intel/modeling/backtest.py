"""No-lookahead historical backtest for the projection model.

Replays ``project_players`` at every historical gameweek deadline using
only season-to-date results known strictly before that deadline, then
scores the projections against official results already recorded in the
public vaastav/Fantasy-Premier-League dataset (see scripts/fetch_history.py).

This is the Phase 0 harness required before any change to the projection
model may be adopted: SPECIFICATION.md requires model changes to remain
reviewable and validated against frozen history rather than adopted from a
small live sample.

Known simplifications versus the live pipeline:
- Per-gameweek injury status, chance_of_playing, ep_next, and ownership are
  not available point-in-time in the free historical dataset; every
  snapshot treats players as available and leaves ep_next at 0, so the
  GW1 official-ep_next blend never activates historically.
- Recent-transfer role-transition scenarios are not replayed (no
  historical transfer-window feed available offline).
- Scoring is per player, not per frozen-squad-and-captain like
  model_performance.py, since historical manager squads do not exist.
  This measures projection accuracy, not squad-selection value.
"""

import csv
from pathlib import Path

from .model_performance import _summarize
from .recommendations import project_players


_HORIZONS = (1, 3, 5)
_POSITION_IDS = {"GK": 1, "GKP": 1, "DEF": 2, "MID": 3, "FWD": 4}
_TOP_POOL_SIZE = 120

_BACKTEST_LIMITATIONS = [
    "The official ep_next estimate is unavailable historically, so its GW1 blend is inactive.",
    "Injury/availability status is unavailable historically; every player is treated as available.",
    "Recent-transfer role-transition scenarios are not replayed (no historical transfer feed).",
    "Historical point-in-time FDR snapshots are unavailable; fixture difficulty is neutralized to 3 until the strictly pre-origin team-strength model activates.",
    "Players absent from all pre-origin rows cannot be projected; later debutants are included only after their first recorded appearance.",
    "Season-to-date totals are aggregated exactly as the live bootstrap would supply them at that deadline.",
    "Defensive-contribution scoring is era-aware: it is disabled for historical datasets before the rule/field exists, while 2025/26 uses strictly pre-origin cumulative defensive-contribution inputs. Aggregate cross-season metrics therefore span different official scoring regimes.",
]


def _int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_season(directory, label=None):
    """Load one fetched season directory into normalized in-memory structures."""
    directory = Path(directory)
    with open(directory / "teams.csv", newline="", encoding="utf-8-sig") as handle:
        teams = [
            {
                "id": _int(row.get("id")),
                "name": row.get("name"),
                "short_name": row.get("short_name"),
            }
            for row in csv.DictReader(handle)
        ]
    with open(directory / "fixtures.csv", newline="", encoding="utf-8-sig") as handle:
        fixtures = []
        for row in csv.DictReader(handle):
            event = _int(row.get("event"), 0)
            if not event:
                continue
            finished = (row.get("finished") or "").strip().lower() == "true"
            fixtures.append(
                {
                    "event": event,
                    "team_h": _int(row.get("team_h")),
                    "team_a": _int(row.get("team_a")),
                    "team_h_difficulty": _int(row.get("team_h_difficulty"), 3),
                    "team_a_difficulty": _int(row.get("team_a_difficulty"), 3),
                    "team_h_score": _int(row.get("team_h_score")) if finished else None,
                    "team_a_score": _int(row.get("team_a_score")) if finished else None,
                }
            )
    with open(directory / "merged_gw.csv", newline="", encoding="utf-8-sig") as handle:
        rows = []
        reader = csv.DictReader(handle)
        defensive_contribution_scoring_enabled = "defensive_contribution" in (reader.fieldnames or [])
        for row in reader:
            gameweek = _int(row.get("GW"), 0)
            position_id = _POSITION_IDS.get((row.get("position") or "").strip().upper())
            if not gameweek or not position_id:
                continue
            rows.append(
                {
                    "element": _int(row.get("element")),
                    "name": row.get("name"),
                    "position_id": position_id,
                    "team_name": row.get("team"),
                    "gameweek": gameweek,
                    "minutes": _int(row.get("minutes")),
                    "starts": _int(row.get("starts")),
                    "total_points": _int(row.get("total_points")),
                    "now_cost": _int(row.get("value"), 40),
                    "expected_goals": _float(row.get("expected_goals")),
                    "expected_assists": _float(row.get("expected_assists")),
                    "expected_goals_conceded": _float(row.get("expected_goals_conceded")),
                    "saves": _int(row.get("saves")),
                    "bonus": _int(row.get("bonus")),
                    "defensive_contribution": _float(row.get("defensive_contribution")),
                }
            )
    return {
        "label": label or directory.name,
        "teams": teams,
        "fixtures": fixtures,
        "rows": rows,
        "defensive_contribution_scoring_enabled": defensive_contribution_scoring_enabled,
    }


def build_origin_inputs(season, origin_gw):
    """Build a bootstrap-shaped payload from strictly pre-origin history.

    Only rows with ``gameweek < origin_gw`` are aggregated, so a snapshot
    for a given origin gameweek is identical whether or not later
    gameweeks have even been recorded yet -- the no-lookahead guarantee.
    """
    team_id_by_name = {team["name"]: team["id"] for team in season["teams"]}
    aggregates = {}
    for row in season["rows"]:
        if row["gameweek"] >= origin_gw:
            continue
        record = aggregates.setdefault(
            row["element"],
            {
                "id": row["element"],
                "web_name": row["name"],
                "element_type": row["position_id"],
                "team": team_id_by_name.get(row["team_name"], 0),
                "now_cost": row["now_cost"],
                "total_points": 0,
                "minutes": 0,
                "starts": 0,
                "bonus": 0,
                "_expected_goals": 0.0,
                "_expected_assists": 0.0,
                "_expected_goals_conceded": 0.0,
                "_saves": 0,
                "_defensive_contribution": 0.0,
                "_recent_rows_by_gw": {},
                "_last_gameweek": 0,
            },
        )
        record["total_points"] += row["total_points"]
        record["minutes"] += row["minutes"]
        record["starts"] += row["starts"]
        record["bonus"] += row["bonus"]
        record["_expected_goals"] += row["expected_goals"]
        record["_expected_assists"] += row["expected_assists"]
        record["_expected_goals_conceded"] += row["expected_goals_conceded"]
        record["_saves"] += row["saves"]
        record["_defensive_contribution"] += row.get("defensive_contribution", 0.0)
        # ~1.4% of (element, gameweek) rows in the historical dataset are duplicated: a
        # double-gameweek fixture is recorded as two separate rows under one shared "GW" label.
        # Summing here (rather than the last-row-seen overwrite an earlier prototype used) keeps
        # a double-gameweek's full minutes/starts in the per-gameweek recency window below instead
        # of silently dropping one of the two fixtures. See plans/issue-65-ml-shadow-model.md.
        gw_bucket = record["_recent_rows_by_gw"].setdefault(
            row["gameweek"], {"gameweek": row["gameweek"], "minutes": 0, "starts": 0}
        )
        gw_bucket["minutes"] += row["minutes"]
        gw_bucket["starts"] += row["starts"]
        if row["gameweek"] >= record["_last_gameweek"]:
            record["_last_gameweek"] = row["gameweek"]
            record["now_cost"] = row["now_cost"]
            record["team"] = team_id_by_name.get(row["team_name"], record["team"])
    elements = []
    for record in aggregates.values():
        record.pop("_last_gameweek")
        minutes = record["minutes"]
        # Per-90 rates mirror the live bootstrap-static's own derived fields,
        # computed here from season-to-date cumulative totals only.
        per_90 = (lambda total: round(total * 90 / minutes, 3)) if minutes > 0 else (lambda total: 0.0)
        recent_rows = sorted(record.pop("_recent_rows_by_gw").values(), key=lambda row: row["gameweek"])
        elements.append(
            {
                **record,
                "expected_goals_per_90": per_90(record.pop("_expected_goals")),
                "expected_assists_per_90": per_90(record.pop("_expected_assists")),
                "expected_goals_conceded_per_90": per_90(record.pop("_expected_goals_conceded")),
                "saves_per_90": per_90(record.pop("_saves")),
                "defensive_contribution_per_90": per_90(record.pop("_defensive_contribution")),
                "defensive_contribution_scoring_enabled": bool(
                    season.get("defensive_contribution_scoring_enabled", False)
                ),
                "recent_history": [
                    {"minutes": row["minutes"], "started": row["starts"] > 0} for row in recent_rows
                ],
                "status": "a",
                "chance_of_playing_next_round": None,
                "ep_next": 0,
                "selected_by_percent": 0,
                "news": "",
                "can_select": True,
                "removed": False,
            }
        )
    return {"elements": elements, "teams": season["teams"]}


def _actual_index(season):
    """Index actual (points, minutes) sums by gameweek and element."""
    index = {}
    for row in season["rows"]:
        bucket = index.setdefault(row["gameweek"], {})
        points, minutes = bucket.get(row["element"], (0, 0))
        bucket[row["element"]] = (points + row["total_points"], minutes + row["minutes"])
    return index


def _horizon_actuals(index, element_id, origin_gw, horizon):
    points = 0
    minutes = 0
    for gameweek in range(origin_gw, origin_gw + horizon):
        row_points, row_minutes = index.get(gameweek, {}).get(element_id, (0, 0))
        points += row_points
        minutes += row_minutes
    return points, minutes


def season_comparisons(season, horizons=_HORIZONS, first_origin=2, last_origin=38):
    """Return one comparison row per player, origin gameweek, and horizon.

    A player enters the comparison set for a horizon only if the model
    projected some chance of playing or the player actually played --
    this keeps hundreds of untouched fringe players from diluting the
    error metrics with trivial correct-zero predictions.
    """
    comparisons = []
    actual_index = _actual_index(season)
    # The historical dataset exposes only one FDR value per fixture, not the
    # value available at each past deadline. Treating it as point-in-time data
    # leaks later revisions. Neutral difficulty is used until the model can fit
    # opponent strength from strictly pre-origin scores.
    point_in_time_fixtures = [
        {
            **fixture,
            "team_h_difficulty": 3,
            "team_a_difficulty": 3,
        }
        for fixture in season["fixtures"]
    ]
    max_horizon = max(horizons)
    for origin_gw in range(first_origin, last_origin + 1):
        open_horizons = [horizon for horizon in horizons if origin_gw + horizon - 1 <= last_origin]
        if not open_horizons:
            continue
        bootstrap = build_origin_inputs(season, origin_gw)
        if not bootstrap["elements"]:
            continue
        projections = project_players(
            bootstrap, point_in_time_fixtures, horizon=max_horizon, start_event=origin_gw
        )
        top_pool_ids = {
            row["id"]
            for row in sorted(projections, key=lambda row: row["xp_5"], reverse=True)[:_TOP_POOL_SIZE]
        }
        for projection in projections:
            for horizon in open_horizons:
                actual_points, actual_minutes = _horizon_actuals(
                    actual_index, projection["id"], origin_gw, horizon
                )
                if projection["expected_minutes"] <= 0 and actual_minutes <= 0:
                    continue
                suffix = str(horizon)
                modeled = projection[f"xp_{suffix}"]
                lower = projection[f"lower_{suffix}"]
                upper = projection[f"upper_{suffix}"]
                comparisons.append(
                    {
                        "season": season["label"],
                        "origin_gw": origin_gw,
                        "horizon": horizon,
                        "element_id": projection["id"],
                        "position": projection["position_short"],
                        "confidence": projection["confidence"],
                        "top_pool": projection["id"] in top_pool_ids,
                        "modeled_points": modeled,
                        "actual_points": actual_points,
                        "error": round(actual_points - modeled, 2),
                        "lower_points": lower,
                        "upper_points": upper,
                        "inside_range": lower <= actual_points <= upper,
                    }
                )
    return comparisons


def build_backtest_report(seasons, model_version, horizons=_HORIZONS, first_origin=2, last_origin=38):
    """Score the projection model across seasons and return a summary report.

    ``model_version`` has no default -- every caller must say which model
    version a report is labeling, rather than risk a stale-literal default
    silently mislabeling a report at some future model version.
    """
    comparisons = []
    for season in seasons:
        comparisons.extend(season_comparisons(season, horizons, first_origin, last_origin))
    by_horizon = {
        str(horizon): _summarize([row for row in comparisons if row["horizon"] == horizon])
        for horizon in horizons
    }
    by_position = {
        position: _summarize([row for row in comparisons if row["position"] == position])
        for position in ("GKP", "DEF", "MID", "FWD")
    }
    by_confidence = {
        confidence: _summarize([row for row in comparisons if row["confidence"] == confidence])
        for confidence in ("high", "medium", "low")
    }
    by_season = {
        season["label"]: _summarize([row for row in comparisons if row["season"] == season["label"]])
        for season in seasons
    }
    return {
        "model_version": model_version,
        "method": (
            "Player-level projections replayed at each historical gameweek deadline from "
            "strictly pre-origin season-to-date data, scored against official results already "
            "recorded in the historical dataset."
        ),
        "limitations": list(_BACKTEST_LIMITATIONS),
        "seasons": [season["label"] for season in seasons],
        "first_origin_gw": first_origin,
        "last_origin_gw": last_origin,
        "horizons": list(horizons),
        "cohort_definition": (
            "Players with projected expected minutes above zero, or any actual minutes inside "
            "the horizon window."
        ),
        "completed_comparisons": len(comparisons),
        "summary": _summarize(comparisons),
        "top_pool_summary": _summarize([row for row in comparisons if row["top_pool"]]),
        "by_horizon": by_horizon,
        "by_position": by_position,
        "by_confidence": by_confidence,
        "by_season": by_season,
        "comparisons": comparisons,
    }
