"""Backtest-only prototype for issue #65 candidate #2: a residual meta-learner.

Trains a small ridge-regression model to predict the gap between the live
model's own projection and the actual outcome (``actual_points -
modeled_points``, horizon=1), using signals the current model never looks
at: ownership, price, recent transfer activity, and ICT/bps. If the
augmented prediction (``modeled + predicted_residual``) beats the current
model's own MAE on a truly held-out season, that's real evidence for the
candidate; if not, it isn't.

Mirrors scripts/run_backtest.py's fit/held-out season split exactly
(2022-23/2023-24/2024-25 to fit, 2025-26 as the held-out check) rather than
inventing a new split, so results are directly comparable to the live
model's own reported backtest numbers.

Investigation artifact for plans/issue-65-ml-shadow-model.md, not part of
the live model.

Run with: PYTHONPATH=src python3 scripts/experiment_residual_ml_prototype.py
"""

import csv
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fpl_intel.modeling.backtest import load_season, season_comparisons


FIT_SEASONS = ["2022-23", "2023-24", "2024-25"]
HELD_OUT_SEASONS = ["2025-26"]
RIDGE_LAMBDA = 5.0
_POSITIONS = ("GKP", "DEF", "MID", "FWD")
_POSITION_DUMMIES = _POSITIONS[1:]  # drop GKP as the reference level -- avoids the
# intercept + one-hot collinearity trap (is_GKP+is_DEF+is_MID+is_FWD == 1 == intercept)


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_extra_rows(label):
    """Per-element, per-gameweek ict_index/bps/value/selected/transfers_balance,
    read directly from merged_gw.csv (fields backtest.load_season doesn't parse)."""
    path = Path("data/history") / label / "merged_gw.csv"
    rows = {}
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            gw = row.get("GW")
            element = row.get("element")
            if not gw or not element:
                continue
            rows.setdefault(int(element), []).append(
                {
                    "gameweek": int(gw),
                    "ict_index": _num(row.get("ict_index")),
                    "bps": _num(row.get("bps")),
                    "value": _num(row.get("value"), 50.0),
                    "selected": _num(row.get("selected")),
                    "transfers_balance": _num(row.get("transfers_balance")),
                }
            )
    for element in rows:
        rows[element].sort(key=lambda r: r["gameweek"])
    return rows


def _pre_origin_features(extra_rows_for_element, origin_gw, modeled_points, position):
    pre = [row for row in extra_rows_for_element if row["gameweek"] < origin_gw]
    n = max(len(pre), 1)
    ict_per_game = sum(row["ict_index"] for row in pre) / n
    bps_per_game = sum(row["bps"] for row in pre) / n
    latest = pre[-1] if pre else None
    price = (latest["value"] if latest else 50.0) / 10.0
    log_selected = np.log1p(latest["selected"] if latest else 0.0)
    transfers_balance_scaled = (latest["transfers_balance"] if latest else 0.0) / 100_000.0
    position_dummies = [1.0 if position == pos else 0.0 for pos in _POSITION_DUMMIES]
    return [1.0, ict_per_game / 10.0, bps_per_game / 10.0, price, log_selected, transfers_balance_scaled, modeled_points] + position_dummies


def build_rows(labels):
    rows = []
    for label in labels:
        season = load_season(Path("data/history") / label, label)
        extra = load_extra_rows(label)
        comparisons = season_comparisons(season, horizons=(1,), first_origin=2, last_origin=38)
        for comparison in comparisons:
            element_rows = extra.get(comparison["element_id"], [])
            features = _pre_origin_features(
                element_rows, comparison["origin_gw"], comparison["modeled_points"], comparison["position"]
            )
            rows.append(
                {
                    "season": label,
                    "features": features,
                    "modeled": comparison["modeled_points"],
                    "actual": comparison["actual_points"],
                }
            )
    return rows


def fit_ridge(X, y, lam=RIDGE_LAMBDA):
    X = np.asarray(X)
    y = np.asarray(y)
    n_features = X.shape[1]
    reg = lam * np.eye(n_features)
    reg[0, 0] = 0.0
    return np.linalg.solve(X.T @ X + reg, X.T @ y)


def main():
    train_rows = build_rows(FIT_SEASONS)
    test_rows = build_rows(HELD_OUT_SEASONS)
    print(f"Train rows (fit seasons {FIT_SEASONS}): {len(train_rows)}")
    print(f"Test rows (held-out season {HELD_OUT_SEASONS}): {len(test_rows)}\n")

    X_train = [row["features"] for row in train_rows]
    y_train = [row["actual"] - row["modeled"] for row in train_rows]
    weights = fit_ridge(X_train, y_train)

    modeled_errors = [abs(row["actual"] - row["modeled"]) for row in test_rows]
    augmented_preds = [row["modeled"] + float(np.dot(weights, row["features"])) for row in test_rows]
    augmented_errors = [abs(row["actual"] - pred) for pred, row in zip(augmented_preds, test_rows)]

    print(f"Champion-only MAE on held-out {HELD_OUT_SEASONS}: {np.mean(modeled_errors):.4f}")
    print(f"Champion + learned residual MAE:              {np.mean(augmented_errors):.4f}")
    print(f"Delta: {np.mean(augmented_errors) - np.mean(modeled_errors):+.4f}")

    names = ["intercept", "ict_per_game/10", "bps_per_game/10", "price/10", "log1p(selected)",
              "transfers_balance/100k", "modeled_points"] + [f"is_{pos}" for pos in _POSITION_DUMMIES]
    print("\nLearned residual-model weights:")
    for name, weight in zip(names, weights):
        print(f"  {name:<24} {weight:+.3f}")


if __name__ == "__main__":
    main()
