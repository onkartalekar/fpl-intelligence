#!/usr/bin/env python3
"""Fit ridge-regression weights for the ML minutes shadow challenger (issue #65).

Writes config/ml-minutes-weights.json, the checked-in constants
src/fpl_intel/ml_minutes.py loads at import time -- mirroring
scripts/fit_coefficients.py's precedent for the champion model: weights are
fitted offline and reviewed in, never refit implicitly inside a live refresh.

Uses src/fpl_intel/ml_minutes.py's own extract_features() to build every
training row, so the fitted weights are guaranteed to match the exact
feature definition predict_expected_minutes() applies at request time --
no separate, possibly-drifted copy of the feature logic.

Also reports leave-one-season-out cross-validation MAE against the live
`_expected_minutes` baseline, the same methodology
scripts/experiment_minutes_ml_prototype.py used, so a re-fit stays
comparable to the numbers recorded in plans/issue-65-ml-shadow-model.md.

Sums minutes/starts across duplicated (element, gameweek) rows -- double-
gameweek fixtures recorded as two rows under one shared GW label in the
historical dataset -- via backtest.py's own build_origin_inputs/_actual_index,
both of which already do this correctly (see backtest.py's
_recent_rows_by_gw and _actual_index). An earlier prototype
(scripts/experiment_minutes_ml_prototype.py) took the last row seen instead;
this script does not repeat that.

Run with: PYTHONPATH=src python3 scripts/fit_ml_minutes_weights.py
"""

import json
import sys
from datetime import date, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fpl_intel import backtest as bt
from fpl_intel.ml_minutes import FEATURE_NAMES, MODEL_VERSION, extract_features
from fpl_intel.recommendations import _expected_minutes


SEASONS = ["2022-23", "2023-24", "2024-25", "2025-26"]
RIDGE_LAMBDA = 5.0
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "config" / "ml-minutes-weights.json"


def load_all_seasons():
    return {label: bt.load_season(Path("data/history") / label, label) for label in SEASONS}


def build_rows(seasons):
    """One row per (season, player, origin_gw) with a real fixture in that gameweek --
    every player, not just those with >=3 pre-origin gameweeks, since production code
    predicts for everyone (see ml_minutes.py's module docstring on the graceful
    season-long fallback). Feature extraction and the baseline both use the same
    fixtures_played convention (origin_gw - 1) run_backtest.py's own harness uses."""
    rows = []
    for label, season in seasons.items():
        actual_by_gw = bt._actual_index(season)
        for origin_gw in range(2, 39):
            bootstrap = bt.build_origin_inputs(season, origin_gw)
            actuals = actual_by_gw.get(origin_gw, {})
            fixtures_played = max(1, origin_gw - 1)
            for player in bootstrap["elements"]:
                actual = actuals.get(player["id"])
                if actual is None:
                    continue
                _, actual_minutes = actual
                baseline = _expected_minutes(player, fixtures_played=fixtures_played)
                rows.append(
                    {
                        "season": label,
                        "features": extract_features(player, fixtures_played=fixtures_played),
                        "actual": float(actual_minutes),
                        "baseline": baseline,
                    }
                )
    return rows


def fit_ridge(X, y, lam=RIDGE_LAMBDA):
    X = np.asarray(X)
    y = np.asarray(y)
    reg = lam * np.eye(X.shape[1])
    reg[0, 0] = 0.0  # do not shrink the intercept
    return np.linalg.solve(X.T @ X + reg, X.T @ y)


def mae(values):
    return float(np.mean(np.abs(values))) if values else 0.0


def main():
    seasons = load_all_seasons()
    rows = build_rows(seasons)
    print(f"Total rows across {len(SEASONS)} seasons: {len(rows)}\n")

    pooled_baseline_errors = []
    pooled_learned_errors = []
    for held_out in SEASONS:
        train_rows = [row for row in rows if row["season"] != held_out]
        test_rows = [row for row in rows if row["season"] == held_out]
        weights = fit_ridge([row["features"] for row in train_rows], [row["actual"] for row in train_rows])

        baseline_errors = [row["baseline"] - row["actual"] for row in test_rows]
        learned_errors = [
            float(np.clip(np.dot(weights, row["features"]), 0.0, 90.0)) - row["actual"] for row in test_rows
        ]
        pooled_baseline_errors.extend(baseline_errors)
        pooled_learned_errors.extend(learned_errors)
        print(
            f"Held out {held_out}: n={len(test_rows):>6}  "
            f"baseline MAE={mae(baseline_errors):.3f}  learned MAE={mae(learned_errors):.3f}  "
            f"delta={mae(learned_errors) - mae(baseline_errors):+.3f}"
        )

    print(
        f"\nPooled across all 4 held-out folds: baseline MAE={mae(pooled_baseline_errors):.3f}  "
        f"learned MAE={mae(pooled_learned_errors):.3f}  "
        f"delta={mae(pooled_learned_errors) - mae(pooled_baseline_errors):+.3f}"
    )

    # Production weights: fit on all 4 seasons, the same way fit_coefficients.py fits the
    # champion's checked-in constants once cross-validation has already confirmed the
    # approach generalizes across every held-out season above.
    weights_all = fit_ridge([row["features"] for row in rows], [row["actual"] for row in rows])
    print("\nFinal ridge weights (fit on all 4 seasons, checked in to config/ml-minutes-weights.json):")
    for name, weight in zip(FEATURE_NAMES, weights_all):
        print(f"  {name:<28} {weight:+.3f}")

    payload = {
        "model_version": MODEL_VERSION,
        "fitted_at": date.today().isoformat(),
        "ridge_lambda": RIDGE_LAMBDA,
        "fit_seasons": SEASONS,
        "feature_names": list(FEATURE_NAMES),
        "weights": [round(float(weight), 4) for weight in weights_all],
        "validation": {
            "method": "leave-one-season-out cross-validation, pooled minutes MAE vs. the live "
            "_expected_minutes baseline",
            "pooled_baseline_mae": round(mae(pooled_baseline_errors), 3),
            "pooled_learned_mae": round(mae(pooled_learned_errors), 3),
        },
    }
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
