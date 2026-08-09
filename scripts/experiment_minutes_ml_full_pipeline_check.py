"""Does candidate #1's minutes-classifier win survive being wired through the
FULL scoring pipeline (points MAE via project_players), not just isolated
minutes MAE?

This check exists because isolated minutes accuracy is not the project's
actual adoption bar, and this codebase has a direct precedent for the two
diverging: Phase 4's recency-minutes model's own postmortem
(IMPLEMENTATION_PLAN.md) found its minutes estimate looked plausible in
isolation but still made full points MAE ~35% worse once wired through
scoring -- "the damage likely concentrates in how that estimate propagates
through scoring, not in the minutes estimate itself." This script runs that
exact test for candidate #1 instead of assuming the minutes-only win
(scripts/experiment_minutes_ml_prototype.py) implies a points-MAE win.

Fits ridge weights on the same fit/held-out split run_backtest.py uses
(2022-23/2023-24/2024-25 to fit, 2025-26 held out), then monkeypatches
recommendations._expected_minutes for the duration of one backtest run --
nothing is written to the live module. Investigation artifact for
plans/issue-65-ml-shadow-model.md, not part of the live model.

Run with: PYTHONPATH=src python3 scripts/experiment_minutes_ml_full_pipeline_check.py
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fpl_intel import backtest as bt
from fpl_intel import recommendations as rec
from fpl_intel.model_performance import _summarize


FIT_SEASONS = ["2022-23", "2023-24", "2024-25"]
HELD_OUT = "2025-26"
MIN_HISTORY = 3
RIDGE_LAMBDA = 5.0


def _features(player):
    history = player.get("recent_history") or []
    n = len(history)
    minutes = player["minutes"]
    starts = player["starts"]
    games = max(n, 1)
    season_start_share = starts / games
    season_minutes_per_game = minutes / games
    last3 = history[-3:]
    last3_start_rate = sum(1 for row in last3 if row["started"]) / len(last3) if last3 else season_start_share
    last3_avg_minutes = sum(row["minutes"] for row in last3) / len(last3) if last3 else season_minutes_per_game
    trend = last3_start_rate - season_start_share
    return [1.0, season_start_share, season_minutes_per_game / 90.0, last3_start_rate,
            last3_avg_minutes / 90.0, trend, min(n, 20) / 20.0]


def _fit_weights():
    rows = []
    for label in FIT_SEASONS:
        season = bt.load_season(Path("data/history") / label, label)
        actual_by_gw = {}
        for row in season["rows"]:
            actual_by_gw.setdefault(row["gameweek"], {})[row["element"]] = row["minutes"]
        for origin_gw in range(2, 39):
            bootstrap = bt.build_origin_inputs(season, origin_gw)
            actuals = actual_by_gw.get(origin_gw, {})
            for player in bootstrap["elements"]:
                if len(player.get("recent_history") or []) < MIN_HISTORY:
                    continue
                actual = actuals.get(player["id"])
                if actual is None:
                    continue
                rows.append((_features(player), float(actual)))
    X = np.asarray([row[0] for row in rows])
    y = np.asarray([row[1] for row in rows])
    reg = RIDGE_LAMBDA * np.eye(X.shape[1])
    reg[0, 0] = 0.0
    weights = np.linalg.solve(X.T @ X + reg, X.T @ y)
    print(f"Fitted ridge weights on {FIT_SEASONS}, n={len(rows)}")
    return weights


def main():
    weights = _fit_weights()
    original_expected_minutes = rec._expected_minutes

    def patched_expected_minutes(player, fixtures_played=38):
        history = player.get("recent_history") or []
        if len(history) < MIN_HISTORY:
            return original_expected_minutes(player, fixtures_played)
        availability = rec._availability_multiplier(player)
        if not availability:
            return 0.0
        prediction = float(np.dot(weights, _features(player)))
        return round(float(np.clip(prediction, 0.0, 90.0)) * availability, 1)

    season = bt.load_season(Path("data/history") / HELD_OUT, HELD_OUT)

    baseline_rows = bt.season_comparisons(season, horizons=(1, 3, 5), first_origin=2, last_origin=38)
    baseline_summary = _summarize(baseline_rows)

    rec._expected_minutes = patched_expected_minutes
    try:
        candidate_rows = bt.season_comparisons(season, horizons=(1, 3, 5), first_origin=2, last_origin=38)
    finally:
        rec._expected_minutes = original_expected_minutes
    candidate_summary = _summarize(candidate_rows)

    print(f"\nFull points-projection backtest on held-out {HELD_OUT}:")
    print(
        f"  Champion (current live minutes model):  n={baseline_summary['count']:>6}  "
        f"MAE={baseline_summary['mae']}  bias={baseline_summary['bias']}  "
        f"range_coverage={baseline_summary['range_coverage']}"
    )
    print(
        f"  Candidate #1 minutes model wired in:    n={candidate_summary['count']:>6}  "
        f"MAE={candidate_summary['mae']}  bias={candidate_summary['bias']}  "
        f"range_coverage={candidate_summary['range_coverage']}"
    )
    print(f"  Delta MAE: {candidate_summary['mae'] - baseline_summary['mae']:+.4f}")

    for horizon in (1, 3, 5):
        baseline_h = _summarize([row for row in baseline_rows if row["horizon"] == horizon])
        candidate_h = _summarize([row for row in candidate_rows if row["horizon"] == horizon])
        print(
            f"  horizon={horizon}: champion MAE={baseline_h['mae']}  "
            f"candidate MAE={candidate_h['mae']}  delta={candidate_h['mae'] - baseline_h['mae']:+.4f}"
        )


if __name__ == "__main__":
    main()
