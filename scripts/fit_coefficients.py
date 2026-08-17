#!/usr/bin/env python3
"""Fit a candidate config/model-coefficients.json from historical data.

Per SPECIFICATION.md's model-change rule, this script computes candidate
values and writes them to a *candidate* file
(config/model-coefficients.candidate.json) -- it never overwrites the
active config/model-coefficients.json. Review the printed before/after
backtest comparison, then promote explicitly:

    cp config/model-coefficients.candidate.json config/model-coefficients.json

Every key already in the active config is carried into the candidate
unchanged unless this script recomputes it -- see PRESERVED_KEYS below.
This script does NOT touch the position-specific residual trust
(residual_reliability_denominator_by_position) or the Phase 1/4 gate
values (team_strength_*, minutes_*): those were the product of bespoke,
multi-step investigation (including catching a DEF cross-season
instability and a since-removed dead key -- see IMPLEMENTATION_PLAN.md's
Phase 3 continuation and the "Known gaps" section) that this general
script does not attempt to reproduce mechanically. Re-running it will
never regress those values or the model_version/fitted_at/source
provenance fields, because it starts from a full copy of the active
config rather than a blank slate.

Method:
1. clean_sheet_probability_by_difficulty, goals_conceded_multiplier_by_difficulty,
   and attack_multiplier_by_difficulty are computed directly (empirically)
   from real historical fixture results conditioned on official FDR -- not
   a search, a direct calculation.
2. reliability_denominator is fit by grid search on a reduced single-season
   backtest, for speed. Finds a reasonable local value, not a guaranteed
   global optimum.
3. uncertainty_bands are refit from the empirical coverage of a full
   fit-season backtest, per confidence bucket, targeting ~75% coverage.
"""

import csv
import importlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import fpl_intel.modeling.coefficients as coefficients_module
import fpl_intel.modeling.projection as projection_module
import fpl_intel.modeling.recommendations as recommendations_module
import fpl_intel.modeling.backtest as backtest_module


ACTIVE_CONFIG_PATH = ROOT / "config" / "model-coefficients.json"
CANDIDATE_CONFIG_PATH = ROOT / "config" / "model-coefficients.candidate.json"
FIT_SEASONS = ["2022-23", "2023-24", "2024-25"]
SEARCH_SEASON = "2023-24"  # used for the fast coordinate-descent search only
TARGET_COVERAGE = 0.75

# Keys this script recomputes. Everything else in the active config is
# carried into the candidate byte-for-byte -- see main().
FITTED_KEYS = {
    "clean_sheet_probability_by_difficulty",
    "goals_conceded_multiplier_by_difficulty",
    "attack_multiplier_by_difficulty",
    "reliability_denominator",
    "uncertainty_bands",
}


def _write_candidate(config):
    CANDIDATE_CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def _reload_model_modules_from(config):
    # The model modules load coefficients from ACTIVE_CONFIG_PATH at import
    # time, so the search steps below write trial values to the *active*
    # file temporarily to measure their effect, then this function restores
    # whatever the caller passes in. main() always restores the untouched
    # original active config before returning control, and the final
    # candidate is written only to CANDIDATE_CONFIG_PATH, never active.
    ACTIVE_CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    importlib.reload(coefficients_module)
    importlib.reload(projection_module)
    importlib.reload(recommendations_module)
    importlib.reload(backtest_module)


def _empirical_fdr_tables():
    clean_sheet = {difficulty: [0, 0] for difficulty in range(1, 6)}
    goals_conceded = {difficulty: [0.0, 0] for difficulty in range(1, 6)}
    goals_scored = {difficulty: [0.0, 0] for difficulty in range(1, 6)}
    for season in FIT_SEASONS:
        path = ROOT / "data" / "history" / season / "fixtures.csv"
        with open(path, newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                if row.get("finished") != "True":
                    continue
                try:
                    home_score = int(row["team_h_score"])
                    away_score = int(row["team_a_score"])
                    home_difficulty = int(row["team_h_difficulty"])
                    away_difficulty = int(row["team_a_difficulty"])
                except (TypeError, ValueError):
                    continue
                clean_sheet[home_difficulty][0] += 1 if away_score == 0 else 0
                clean_sheet[home_difficulty][1] += 1
                goals_conceded[home_difficulty][0] += away_score
                goals_conceded[home_difficulty][1] += 1
                goals_scored[home_difficulty][0] += home_score
                goals_scored[home_difficulty][1] += 1
                clean_sheet[away_difficulty][0] += 1 if home_score == 0 else 0
                clean_sheet[away_difficulty][1] += 1
                goals_conceded[away_difficulty][0] += home_score
                goals_conceded[away_difficulty][1] += 1
                goals_scored[away_difficulty][0] += away_score
                goals_scored[away_difficulty][1] += 1
    overall_avg_conceded = (
        sum(total for total, _ in goals_conceded.values()) / sum(n for _, n in goals_conceded.values())
    )
    overall_avg_scored = (
        sum(total for total, _ in goals_scored.values()) / sum(n for _, n in goals_scored.values())
    )
    clean_sheet_probability = {
        str(difficulty): round(made / n, 3) if n else 0.30
        for difficulty, (made, n) in clean_sheet.items()
    }
    goals_conceded_multiplier = {
        str(difficulty): round((total / n) / overall_avg_conceded, 3) if n else 1.0
        for difficulty, (total, n) in goals_conceded.items()
    }
    attack_multiplier = {
        str(difficulty): round((total / n) / overall_avg_scored, 3) if n else 1.0
        for difficulty, (total, n) in goals_scored.items()
    }
    return clean_sheet_probability, goals_conceded_multiplier, attack_multiplier


def _quick_backtest_summary(season):
    # Raw, unrounded MAE/bias -- the pre-rounded report["summary"] figures
    # are too coarse to compare candidates whose true difference is well
    # under 0.01, and comparing on rounded values silently tie-breaks on
    # iteration order rather than a real signal (caught the hard way once
    # already -- see IMPLEMENTATION_PLAN.md Phase 3).
    rows = backtest_module.season_comparisons(season, first_origin=10, last_origin=30)
    mae = sum(abs(row["error"]) for row in rows) / len(rows)
    bias = sum(row["error"] for row in rows) / len(rows)
    return mae, bias


def _search_reliability_denominator(working_config, candidates, season, default_value, min_improvement=0.01):
    """Grid search, but only adopt a candidate if it beats the default by
    more than ``min_improvement`` MAE -- otherwise keep the default rather
    than chase noise-level differences (a lower-MAE candidate at this
    parameter can trade away visibly worse bias; see printed results)."""
    results = []
    for value in candidates:
        trial = {**working_config, "reliability_denominator": value}
        _reload_model_modules_from(trial)
        mae, bias = _quick_backtest_summary(season)
        results.append((value, mae, bias))
    best_value, best_mae, _ = min(results, key=lambda row: row[1])
    default_row = next((row for row in results if row[0] == default_value), None)
    default_mae = default_row[1] if default_row else best_mae
    chosen = best_value if (default_mae - best_mae) > min_improvement else default_value
    return chosen, results


def _fit_uncertainty_band(rows, target_coverage=TARGET_COVERAGE):
    if not rows:
        return 0.30
    best_u, best_gap = 0.30, float("inf")
    candidate = 0.10
    while candidate <= 1.50:
        inside = sum(
            1 for row in rows
            if row["modeled_points"] * (1 - candidate) <= row["actual_points"] <= row["modeled_points"] * (1 + candidate)
        )
        coverage = inside / len(rows)
        gap = abs(coverage - target_coverage)
        if gap < best_gap:
            best_gap, best_u = gap, round(candidate, 2)
        candidate += 0.02
    return best_u


def main():
    if not ACTIVE_CONFIG_PATH.exists():
        raise SystemExit(f"No active config at {ACTIVE_CONFIG_PATH} -- nothing to base a candidate on.")
    original_active_config = json.loads(ACTIVE_CONFIG_PATH.read_text(encoding="utf-8"))
    working_config = dict(original_active_config)
    print(f"Starting from active config: {sorted(working_config.keys())}")

    try:
        print("\nStep 1: empirical FDR-conditioned tables from real historical results")
        clean_sheet_probability, goals_conceded_multiplier, attack_multiplier = _empirical_fdr_tables()
        working_config["clean_sheet_probability_by_difficulty"] = clean_sheet_probability
        working_config["goals_conceded_multiplier_by_difficulty"] = goals_conceded_multiplier
        working_config["attack_multiplier_by_difficulty"] = attack_multiplier
        print("  clean_sheet_probability_by_difficulty:", clean_sheet_probability)
        print("  goals_conceded_multiplier_by_difficulty:", goals_conceded_multiplier)
        print("  attack_multiplier_by_difficulty:", attack_multiplier)

        search_season = backtest_module.load_season(ROOT / "data" / "history" / SEARCH_SEASON, label=SEARCH_SEASON)

        print(f"\nStep 2: search reliability_denominator (reduced backtest on {SEARCH_SEASON}, gw10-30)")
        default_denominator = original_active_config.get("reliability_denominator", 900.0)
        best_denominator, results = _search_reliability_denominator(
            working_config, [400.0, 600.0, 900.0, 1200.0, 1600.0, 2000.0], search_season,
            default_value=default_denominator,
        )
        for value, mae, bias in results:
            print(f"    {value:>7.0f} -> mae={mae:.5f} bias={bias:.5f}")
        print("  chosen:", best_denominator)
        working_config["reliability_denominator"] = best_denominator

        print("\nStep 3: position-specific residual trust, team-strength, and minutes gates -- NOT touched")
        print("  These were set by bespoke multi-step investigation (a DEF cross-season")
        print("  instability check, and two full model-vs-baseline backtest studies for")
        print("  Phase 1/4) that this script does not attempt to reproduce mechanically.")
        print("  Carried forward unchanged from the active config:")
        for key in (
            "residual_reliability_denominator_by_position", "team_strength_min_rounds",
            "team_strength_half_life_matches", "minutes_min_appearances", "minutes_half_life_matches",
        ):
            if key in working_config:
                print(f"    {key}: {working_config[key]}")

        print("\nStep 4: ep_next_blend_weight -- NOT fitted here")
        print("  The backtest snapshot always sets ep_next=0 (undocumented historically),")
        print("  so this parameter's `if official_ep > 0` gate never fires in any backtest")
        print("  replay -- a search over it cannot measure anything real. Carried forward")
        print("  unchanged:", working_config.get("ep_next_blend_weight"))

        print("\nStep 5: full fit-season backtest with fitted constants so far, then refit uncertainty bands")
        _reload_model_modules_from(working_config)
        fit_seasons = [backtest_module.load_season(ROOT / "data" / "history" / s, label=s) for s in FIT_SEASONS]
        full_report = backtest_module.build_backtest_report(fit_seasons, model_version="fitting")
        bands = {}
        for confidence in ("high", "medium", "low"):
            rows = [row for row in full_report["comparisons"] if row.get("confidence") == confidence]
            bands[confidence] = _fit_uncertainty_band(rows)
            print(f"  {confidence}: n={len(rows)} fitted_band={bands[confidence]}")
        working_config["uncertainty_bands"] = bands
    finally:
        # Always leave the active config exactly as found, regardless of
        # how the search steps above left it mid-run.
        ACTIVE_CONFIG_PATH.write_text(json.dumps(original_active_config, indent=2), encoding="utf-8")

    print("\nChanges vs. the active config:")
    any_change = False
    for key in FITTED_KEYS:
        if working_config.get(key) != original_active_config.get(key):
            any_change = True
            print(f"  {key}:")
            print(f"    active:    {original_active_config.get(key)}")
            print(f"    candidate: {working_config.get(key)}")
    if not any_change:
        print("  (none -- candidate matches the active config on every fitted key)")

    _write_candidate(working_config)
    print(f"\nCandidate written to {CANDIDATE_CONFIG_PATH} -- the active config was not modified.")
    print("Review the changes above, then promote explicitly if they're an improvement:")
    print(f"  cp {CANDIDATE_CONFIG_PATH.relative_to(ROOT)} {ACTIVE_CONFIG_PATH.relative_to(ROOT)}")
    print("...and re-run scripts/run_backtest.py for the official before/after record.")


if __name__ == "__main__":
    main()
