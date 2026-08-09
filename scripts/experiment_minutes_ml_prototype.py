"""Backtest-only prototype for issue #65 candidate #1: an ML minutes model.

Compares the live season-average expected-minutes formula
(``recommendations._expected_minutes``) against a small ridge-regression
model trained on the same strictly pre-origin per-gameweek history already
exposed by the no-lookahead backtest harness (``backtest.build_origin_inputs``).

This is an investigation artifact for plans/issue-65-ml-shadow-model.md, not
part of the live model. It reuses backtest.py's loader so the no-lookahead
guarantee and cohort rules match the existing harness exactly -- only the
minutes prediction itself is swapped, everything else (data loading, season
boundaries) is identical to what fit_coefficients.py / run_backtest.py use.

Run with: PYTHONPATH=src python3 scripts/experiment_minutes_ml_prototype.py
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fpl_intel import backtest as bt
from fpl_intel.recommendations import _expected_minutes


SEASONS = ["2022-23", "2023-24", "2024-25", "2025-26"]
MIN_HISTORY = 3
RIDGE_LAMBDA = 5.0


def load_all_seasons():
    return {label: bt.load_season(Path("data/history") / label, label) for label in SEASONS}


def _features(player):
    """Strictly pre-origin features: season-long shares plus a short-window
    recency signal -- the same information the rejected fixed-decay model
    used, but left for a fitted model to weigh instead of one hand-picked
    half-life."""
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
    return [
        1.0,  # intercept
        season_start_share,
        season_minutes_per_game / 90.0,
        last3_start_rate,
        last3_avg_minutes / 90.0,
        trend,
        min(n, 20) / 20.0,  # sample-size / maturity signal
    ]


def build_rows(seasons):
    """One row per (season, player, origin_gw) with >=MIN_HISTORY pre-origin
    appearances, baseline prediction, feature vector, and actual next-gw
    minutes -- mirrors backtest.season_comparisons' cohort/no-lookahead
    structure but scoped to the minutes-only question."""
    rows = []
    for label, season in seasons.items():
        actual_by_gw = {}
        for row in season["rows"]:
            actual_by_gw.setdefault(row["gameweek"], {})[row["element"]] = row["minutes"]
        for origin_gw in range(2, 39):
            bootstrap = bt.build_origin_inputs(season, origin_gw)
            actuals = actual_by_gw.get(origin_gw, {})
            for player in bootstrap["elements"]:
                history = player.get("recent_history") or []
                if len(history) < MIN_HISTORY:
                    continue
                actual_minutes = actuals.get(player["id"])
                if actual_minutes is None:
                    continue  # team had no fixture that GW -- not a prediction target
                baseline = _expected_minutes(player, fixtures_played=max(1, origin_gw - 1))
                rows.append(
                    {
                        "season": label,
                        "origin_gw": origin_gw,
                        "features": _features(player),
                        "actual": float(actual_minutes),
                        "baseline": baseline,
                    }
                )
    return rows


def fit_ridge(X, y, lam=RIDGE_LAMBDA):
    X = np.asarray(X)
    y = np.asarray(y)
    n_features = X.shape[1]
    reg = lam * np.eye(n_features)
    reg[0, 0] = 0.0  # do not shrink the intercept
    weights = np.linalg.solve(X.T @ X + reg, X.T @ y)
    return weights


def mae(preds, actuals):
    preds = np.asarray(preds)
    actuals = np.asarray(actuals)
    return float(np.mean(np.abs(preds - actuals)))


def main():
    seasons = load_all_seasons()
    rows = build_rows(seasons)
    print(f"Total rows across {len(SEASONS)} seasons: {len(rows)}\n")

    overall_baseline_errors = []
    overall_learned_errors = []

    for held_out in SEASONS:
        train_rows = [row for row in rows if row["season"] != held_out]
        test_rows = [row for row in rows if row["season"] == held_out]
        X_train = [row["features"] for row in train_rows]
        y_train = [row["actual"] for row in train_rows]
        weights = fit_ridge(X_train, y_train)

        baseline_errors = [abs(row["baseline"] - row["actual"]) for row in test_rows]
        learned_preds = [float(np.clip(np.dot(weights, row["features"]), 0.0, 90.0)) for row in test_rows]
        learned_errors = [abs(pred - row["actual"]) for pred, row in zip(learned_preds, test_rows)]

        overall_baseline_errors.extend(baseline_errors)
        overall_learned_errors.extend(learned_errors)

        print(
            f"Held out {held_out}: n={len(test_rows):>6}  "
            f"baseline MAE={np.mean(baseline_errors):.3f}  "
            f"learned MAE={np.mean(learned_errors):.3f}  "
            f"delta={np.mean(learned_errors) - np.mean(baseline_errors):+.3f}"
        )

    print(
        f"\nPooled across all 4 held-out folds: "
        f"baseline MAE={np.mean(overall_baseline_errors):.3f}  "
        f"learned MAE={np.mean(overall_learned_errors):.3f}  "
        f"delta={np.mean(overall_learned_errors) - np.mean(overall_baseline_errors):+.3f}"
    )

    # Also report feature weights from a fit on all 4 seasons, for interpretability.
    X_all = [row["features"] for row in rows]
    y_all = [row["actual"] for row in rows]
    weights_all = fit_ridge(X_all, y_all)
    names = [
        "intercept", "season_start_share", "season_min_per_game/90",
        "last3_start_rate", "last3_avg_min/90", "trend", "maturity",
    ]
    print("\nFull-data ridge weights (interpretability only, not used in the CV above):")
    for name, weight in zip(names, weights_all):
        print(f"  {name:<26} {weight:+.2f}")


if __name__ == "__main__":
    main()
