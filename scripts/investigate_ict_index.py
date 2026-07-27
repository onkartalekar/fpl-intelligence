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

from fpl_intel.backtest import load_season, season_comparisons


FIT_SEASONS = ["2022-23", "2023-24", "2024-25"]
FIRST_ORIGIN = 10
LAST_ORIGIN = 30
HORIZON = 3
MIN_PRE_ORIGIN_MINUTES = 180  # ~2 full matches of ICT signal before trusting the rate


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


def main():
    error_vs_ict = []  # (pre-origin ICT rate, model error) -- the key question
    modeled_vs_actual = []  # (modeled_points, actual_points) -- baseline: does the harness itself look sane
    ict_vs_actual = []  # (pre-origin ICT rate, forward actual points) -- does ICT predict anything at all
    ict_vs_modeled = []  # (pre-origin ICT rate, modeled_points) -- is that signal already inside the model

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
            error_vs_ict.append((ict_rate, row["error"]))
            season_pairs.append((ict_rate, row["error"]))
            modeled_vs_actual.append((row["modeled_points"], row["actual_points"]))
            ict_vs_actual.append((ict_rate, row["actual_points"]))
            ict_vs_modeled.append((ict_rate, row["modeled_points"]))
        per_season_error_vs_ict[season_label] = season_pairs
        print(f"{season_label}: {len(season_pairs)} player/origin comparisons with sufficient ICT history")

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


if __name__ == "__main__":
    main()
