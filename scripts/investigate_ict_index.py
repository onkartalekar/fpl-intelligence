#!/usr/bin/env python3
"""Investigate whether FPL's official ICT Index explains current model error.

One-off research script, not part of the fit/validate pipeline
(fit_coefficients.py / run_backtest.py) and never adopted automatically --
it writes nothing to config/model-coefficients.json. See
IMPLEMENTATION_PLAN.md's "ICT Index investigation" entry for context and
findings.

Method: reuse backtest.py's existing, already-validated no-lookahead
replay (season_comparisons()) to get, per player/origin-gameweek/horizon,
the current model's modeled points, actual points, and signed error. Join
in each player's own pre-origin (strictly gameweek < origin_gw) ICT Index
rate (per 90 minutes), then correlate that rate against the model's error.

A correlation check alone answers "is there a relationship" but not "would
actually adding this to the model beat the current backtest" -- the bar
every other phase in IMPLEMENTATION_PLAN.md is held to per SPECIFICATION.md's
model-change rule. So this script also fits a simple linear ICT-based
correction (position-centered, since raw ICT scale differs hugely by
position) on an early-origin training split (GW10-20) and measures its
effect on held-out MAE (GW21-30) -- an honest out-of-sample check, not an
in-sample fit that could just be chasing noise given how weak the raw
correlation already is.

Rationale: `ict_index` is an official FPL field (the sum of its
Influence/Creativity/Threat sub-scores) already present in
data/history/*/merged_gw.csv, capturing goal threat, chance creation,
and general match involvement that isn't fully reducible to xG/xA/bonus.
If a player's ICT rate
correlates with the *error* the current model already makes (not just
with their points, which xG/xA-derived rates already predict reasonably
well), that is a real, actionable signal the model could incorporate.
Near-zero correlation with error, despite a real correlation with raw
points, would mean ICT Index is redundant with what the model already
uses -- a genuine negative result, exactly like Phase 1 and Phase 4 in
IMPLEMENTATION_PLAN.md.
"""

import csv
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fpl_intel.modeling.backtest import load_season, season_comparisons


FIT_SEASONS = ["2022-23", "2023-24", "2024-25"]
FIRST_ORIGIN = 10
LAST_ORIGIN = 30
TRAIN_LAST_ORIGIN = 20  # GW10-20 fits the correction weight; GW21-30 is the held-out MAE check
HORIZON = 3
MIN_PRE_ORIGIN_MINUTES = 180  # ~2 full matches of ICT signal before trusting the rate
MIN_REAL_IMPROVEMENT = 0.01  # same "beat by more than this, or it's noise" bar as fit_coefficients.py


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def load_ict_by_gameweek(season_label):
    """Return {element_id: [(gameweek, minutes, ict_index), ...]}."""
    path = ROOT / "data" / "history" / season_label / "merged_gw.csv"
    by_player = {}
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            gameweek = _int(row.get("GW"), 0)
            element = _int(row.get("element"))
            if not gameweek or not element:
                continue
            by_player.setdefault(element, []).append(
                (gameweek, _int(row.get("minutes")), _float(row.get("ict_index")))
            )
    return by_player


def pre_origin_ict_rate(rows, origin_gw):
    """Season-to-date ICT Index per 90, strictly before origin_gw, plus the minutes sample it's built on."""
    minutes_total = 0
    ict_total = 0.0
    for gameweek, minutes, ict_index in rows:
        if gameweek >= origin_gw:
            continue
        minutes_total += minutes
        ict_total += ict_index
    if minutes_total <= 0:
        return None, 0
    return ict_total * 90 / minutes_total, minutes_total


def pearson_r(pairs):
    n = len(pairs)
    if n < 2:
        return None
    mean_x = sum(x for x, _ in pairs) / n
    mean_y = sum(y for _, y in pairs) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    var_x = sum((x - mean_x) ** 2 for x, _ in pairs)
    var_y = sum((y - mean_y) ** 2 for _, y in pairs)
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / (var_x**0.5 * var_y**0.5)


def _format_r(r):
    return f"{r:.3f}" if r is not None else "n/a"


def _mean(values):
    return sum(values) / len(values) if values else 0.0


def _mae(values):
    return _mean([abs(v) for v in values])


def fit_position_centered_weight(train_records):
    """OLS weight for a single linear correction on position-centered ICT rate.

    ``adjusted_error = error - weight * (ict_rate - position_mean_ict_rate)``.
    Returns (weight, position_means); weight is the covariance/variance
    estimate that minimizes squared adjusted error on the training split
    (a least-squares fit, evaluated for its effect on MAE afterward --
    MAE-optimal and squared-error-optimal aren't identical, but with a
    near-zero raw correlation the two won't meaningfully disagree here).
    """
    position_ict_values = {}
    for record in train_records:
        position_ict_values.setdefault(record["position"], []).append(record["ict_rate"])
    position_means = {position: _mean(values) for position, values in position_ict_values.items()}

    pairs = [
        (record["ict_rate"] - position_means.get(record["position"], 0.0), record["error"])
        for record in train_records
    ]
    weight = None
    n = len(pairs)
    if n >= 2:
        mean_x = _mean([x for x, _ in pairs])
        mean_y = _mean([y for _, y in pairs])
        cov = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
        var_x = sum((x - mean_x) ** 2 for x, _ in pairs)
        weight = cov / var_x if var_x > 0 else 0.0
    return weight or 0.0, position_means


def main():
    records = []  # one dict per player/origin/horizon comparison with sufficient ICT history
    per_season_error_vs_ict = {}

    for season_label in FIT_SEASONS:
        season = load_season(ROOT / "data" / "history" / season_label, label=season_label)
        ict_by_player = load_ict_by_gameweek(season_label)
        comparisons = season_comparisons(
            season, horizons=(HORIZON,), first_origin=FIRST_ORIGIN, last_origin=LAST_ORIGIN
        )
        season_pairs = []
        for row in comparisons:
            rows = ict_by_player.get(row["element_id"])
            if not rows:
                continue
            ict_rate, minutes_sample = pre_origin_ict_rate(rows, row["origin_gw"])
            if ict_rate is None or minutes_sample < MIN_PRE_ORIGIN_MINUTES:
                continue
            records.append(
                {
                    "origin_gw": row["origin_gw"],
                    "position": row["position"],
                    "ict_rate": ict_rate,
                    "error": row["error"],
                    "modeled_points": row["modeled_points"],
                    "actual_points": row["actual_points"],
                }
            )
            season_pairs.append((ict_rate, row["error"]))
        per_season_error_vs_ict[season_label] = season_pairs
        print(f"{season_label}: {len(season_pairs)} player/origin comparisons with sufficient ICT history")

    error_vs_ict = [(r["ict_rate"], r["error"]) for r in records]
    modeled_vs_actual = [(r["modeled_points"], r["actual_points"]) for r in records]
    ict_vs_actual = [(r["ict_rate"], r["actual_points"]) for r in records]
    ict_vs_modeled = [(r["ict_rate"], r["modeled_points"]) for r in records]

    print("\n--- Correlations (Pearson r) ---")
    print(
        f"Sanity baseline -- modeled_points vs actual_points: "
        f"r={_format_r(pearson_r(modeled_vs_actual))} (n={len(modeled_vs_actual)})"
    )
    overall_r = pearson_r(error_vs_ict)
    print(
        f"Pre-origin ICT rate vs model error (actual - modeled), all seasons pooled: "
        f"r={_format_r(overall_r)} (n={len(error_vs_ict)})"
    )
    for season_label, pairs in per_season_error_vs_ict.items():
        r = pearson_r(pairs)
        print(f"  {season_label}: r={_format_r(r)} (n={len(pairs)})")

    print(
        f"\nPre-origin ICT rate vs forward actual_points (does ICT predict anything at all): "
        f"r={_format_r(pearson_r(ict_vs_actual))} (n={len(ict_vs_actual)})"
    )
    print(
        f"Pre-origin ICT rate vs modeled_points (is that signal already inside the model): "
        f"r={_format_r(pearson_r(ict_vs_modeled))} (n={len(ict_vs_modeled)})"
    )

    print(
        "\nInterpretation: a near-zero r means the model's current errors are not "
        "explained by a player's ICT rate -- ICT Index would be redundant with what "
        "xG/xA/bonus/residual already capture. A meaningfully positive r means "
        "high-ICT players are systematically under-projected by the current model, "
        "which would be a real, actionable signal."
    )

    print("\n--- Out-of-sample MAE check (the actual adoption bar) ---")
    train_records = [r for r in records if r["origin_gw"] <= TRAIN_LAST_ORIGIN]
    test_records = [r for r in records if r["origin_gw"] > TRAIN_LAST_ORIGIN]
    weight, position_means = fit_position_centered_weight(train_records)
    print(
        f"Fit on GW{FIRST_ORIGIN}-{TRAIN_LAST_ORIGIN} (n={len(train_records)}): "
        f"weight={weight:.4f}, position means={ {p: round(v, 2) for p, v in position_means.items()} }"
    )

    baseline_errors = [r["error"] for r in test_records]
    adjusted_errors = [
        r["error"] - weight * (r["ict_rate"] - position_means.get(r["position"], 0.0))
        for r in test_records
    ]
    baseline_mae, adjusted_mae = _mae(baseline_errors), _mae(adjusted_errors)
    baseline_bias, adjusted_bias = _mean(baseline_errors), _mean(adjusted_errors)
    improvement = baseline_mae - adjusted_mae
    print(
        f"Held out on GW{TRAIN_LAST_ORIGIN + 1}-{LAST_ORIGIN} (n={len(test_records)}): "
        f"baseline MAE={baseline_mae:.4f} (bias={baseline_bias:+.4f}) -> "
        f"ICT-corrected MAE={adjusted_mae:.4f} (bias={adjusted_bias:+.4f}), "
        f"improvement={improvement:+.4f}"
    )
    if improvement > MIN_REAL_IMPROVEMENT:
        print(f"-> beats baseline by more than {MIN_REAL_IMPROVEMENT} MAE: a real improvement.")
    else:
        print(
            f"-> does not beat baseline by more than {MIN_REAL_IMPROVEMENT} MAE: "
            "not a real improvement, consistent with the near-zero raw correlation above."
        )


if __name__ == "__main__":
    main()
