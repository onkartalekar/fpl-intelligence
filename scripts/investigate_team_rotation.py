#!/usr/bin/env python3
"""Investigate whether team-level rotation propensity explains current model error.

One-off research script for issue #11 (PL manager playing style as a model
input), not part of the fit/validate pipeline (fit_coefficients.py /
run_backtest.py) and never adopted automatically -- it writes nothing to
config/model-coefficients.json. Mirrors scripts/investigate_ict_index.py in
structure, constants, and rigor. See plans/issue-11-manager-style-investigation.md
and IMPLEMENTATION_PLAN.md's "Team rotation index investigation" entry for
context and findings.

Candidate under test (candidate (c) in the plan): a per-team, season-to-date
rotation index computed from starting-XI turnover between consecutive
matches -- the directly observable footprint of a manager's rotation
policy. Hypothesis: the model's minutes estimate is team-context-blind, so
players at high-rotation clubs are systematically over-projected, and the
effect should concentrate in non-nailed players (pre-origin start share in
[0.25, 0.75]).

This is deliberately the opposite construction to Phase 4's short, decayed,
per-player minutes signal (which made projections worse by overreacting to
short-window noise): the rotation index is season-to-date, team-level, and
pools ~11 starters over every pre-origin match, making it a stable
statistic by construction rather than a noisy one.

Method: reuse backtest.py's existing, already-validated no-lookahead
replay (season_comparisons()) to get, per player/origin-gameweek/horizon,
the current model's modeled points, actual points, and signed error. Join
in the player's own strictly-pre-origin team rotation index (via each
player's latest pre-origin team, to handle January transfers) and
pre-origin start share, then correlate the rotation index against the
model's error -- pooled, per season, and restricted to the non-nailed
cohort. As with the ICT investigation, a correlation check alone doesn't
answer whether adding this to the model would actually beat the current
backtest, so this script also fits a simple linear rotation-index-based
correction (centered on the rotation index, since it is a single team-level
covariate rather than something that varies by position) on an early-origin
training split (GW10-20) and measures its effect on held-out MAE (GW21-30)
-- an honest out-of-sample check, repeated once on the full population and
once on the non-nailed cohort alone (two pre-registered variants, both
reported regardless of outcome).
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
MIN_REAL_IMPROVEMENT = 0.01  # same "beat by more than this, or it's noise" bar as fit_coefficients.py
MIN_PRE_ORIGIN_FIXTURE_PAIRS = 6  # minimum consecutive-fixture pairs before trusting a team's rotation index
NON_NAILED_START_SHARE = (0.25, 0.75)  # pre-origin start-share band the mechanism should concentrate in


def _int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _mean(values):
    return sum(values) / len(values) if values else 0.0


def _mae(values):
    return _mean([abs(v) for v in values])


def load_team_fixture_sequences(season_label):
    """Return {team_name: [fixture, ...]} sorted by kickoff_time.

    Each fixture is {"gw": int, "kickoff_time": str, "xi": frozenset(element_ids)}.
    Grouped by (team, fixture) rather than (team, GW) so double gameweeks
    contribute two separate fixtures, not one merged one.

    Data check performed while building this script: 2022-23's merged_gw.csv
    has the ``starts`` column, but it is entirely unpopulated (every row 0)
    for GW1-15 -- FPL's own ``starts`` stat wasn't backfilled that far back
    in that season's data, unlike 2023-24/2024-25 which have it from GW1. A
    real Premier League XI is never empty, so a fixture with zero recorded
    starters is a missing-data artifact, not a 0-rotation match; including
    it would silently corrupt the rotation index (confirmed: it inflates
    2022-23's index to an implausible 5.6-7.0 range vs. 1.4-4.0 for the
    other two seasons). Such fixtures are dropped here rather than treated
    as data.
    """
    path = ROOT / "data" / "history" / season_label / "merged_gw.csv"
    groups = {}
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            gameweek = _int(row.get("GW"), 0)
            team_name = row.get("team")
            fixture_id = row.get("fixture")
            if not gameweek or not team_name or not fixture_id:
                continue
            key = (team_name, fixture_id)
            entry = groups.setdefault(
                key, {"gw": gameweek, "kickoff_time": row.get("kickoff_time") or "", "xi": set()}
            )
            if _int(row.get("starts")) == 1:
                entry["xi"].add(_int(row.get("element")))

    by_team = {}
    for (team_name, _fixture_id), entry in groups.items():
        if not entry["xi"]:  # no recorded starters -- missing-data artifact, not a real 0-rotation match
            continue
        by_team.setdefault(team_name, []).append(entry)
    for team_name, fixtures in by_team.items():
        fixtures.sort(key=lambda f: (f["kickoff_time"], f["gw"]))
    return by_team


def load_player_rows(season_label):
    """Return {element_id: [row, ...]} sorted by kickoff_time.

    Each row is {"gw": int, "team": str, "starts": int, "kickoff_time": str}.
    """
    path = ROOT / "data" / "history" / season_label / "merged_gw.csv"
    by_player = {}
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            gameweek = _int(row.get("GW"), 0)
            element = _int(row.get("element"))
            if not gameweek or not element:
                continue
            by_player.setdefault(element, []).append(
                {
                    "gw": gameweek,
                    "team": row.get("team"),
                    "starts": _int(row.get("starts")),
                    "kickoff_time": row.get("kickoff_time") or "",
                }
            )
    for element, rows in by_player.items():
        rows.sort(key=lambda r: (r["kickoff_time"], r["gw"]))
    return by_player


def team_rotation_index(team_fixtures, origin_gw):
    """Season-to-date rotation index for a team, strictly before origin_gw.

    mean(11 - |XI_t intersect XI_{t-1}|) over all consecutive pre-origin
    fixture pairs (team_fixtures is already sorted by kickoff_time).
    Returns (index_or_None, fixtures_played_pre_origin); None if fewer than
    MIN_PRE_ORIGIN_FIXTURE_PAIRS consecutive pairs are available yet.
    """
    pre_origin = [f for f in team_fixtures if f["gw"] < origin_gw]
    fixtures_played = len(pre_origin)
    if fixtures_played - 1 < MIN_PRE_ORIGIN_FIXTURE_PAIRS:
        return None, fixtures_played
    turnovers = [
        11 - len(curr["xi"] & prev["xi"]) for prev, curr in zip(pre_origin, pre_origin[1:])
    ]
    return _mean(turnovers), fixtures_played


def player_pre_origin_context(player_rows, team_sequences, origin_gw):
    """Per player: rotation index of their latest pre-origin team, plus start share.

    Latest pre-origin team handles mid-season transfers (e.g. January window).
    Start share = player's own pre-origin starts / that team's pre-origin
    fixtures played -- the moderator the correlation screen's non-nailed
    cohort slice needs. Returns None if there is no pre-origin history for
    the player, or if their latest team doesn't yet have enough pre-origin
    fixture pairs to trust a rotation index.
    """
    pre_origin_rows = [row for row in player_rows if row["gw"] < origin_gw]
    if not pre_origin_rows:
        return None
    latest_team = pre_origin_rows[-1]["team"]
    starts_total = sum(row["starts"] for row in pre_origin_rows)
    team_fixtures = team_sequences.get(latest_team, [])
    rotation_index, fixtures_played = team_rotation_index(team_fixtures, origin_gw)
    if rotation_index is None:
        return None
    start_share = starts_total / fixtures_played if fixtures_played > 0 else None
    return {"rotation_index": rotation_index, "start_share": start_share, "team": latest_team}


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


def fit_centered_weight(train_records):
    """OLS weight for a single linear correction on centered rotation index.

    ``adjusted_error = error - weight * (rotation_index - mean_rotation_index)``.
    Global centering, not position centering -- the covariate is team-level,
    not per-position, so there is no position-scale difference to correct for.
    Returns (weight, mean_rotation_index).
    """
    if not train_records:
        return 0.0, 0.0
    mean_rotation = _mean([r["rotation_index"] for r in train_records])
    pairs = [(r["rotation_index"] - mean_rotation, r["error"]) for r in train_records]
    weight = 0.0
    n = len(pairs)
    if n >= 2:
        mean_x = _mean([x for x, _ in pairs])
        mean_y = _mean([y for _, y in pairs])
        cov = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
        var_x = sum((x - mean_x) ** 2 for x, _ in pairs)
        weight = cov / var_x if var_x > 0 else 0.0
    return weight, mean_rotation


def evaluate_variant(label, train_records, test_records):
    """Fit on train_records, evaluate held-out MAE/bias on test_records. Returns improvement."""
    weight, mean_rotation = fit_centered_weight(train_records)
    print(
        f"  [{label}] fit on GW{FIRST_ORIGIN}-{TRAIN_LAST_ORIGIN} (n={len(train_records)}): "
        f"weight={weight:.4f}, mean rotation index={mean_rotation:.3f}"
    )
    if not test_records:
        print(f"  [{label}] no held-out records -- skipping MAE comparison.")
        return None

    baseline_errors = [r["error"] for r in test_records]
    adjusted_errors = [
        r["error"] - weight * (r["rotation_index"] - mean_rotation) for r in test_records
    ]
    baseline_mae, adjusted_mae = _mae(baseline_errors), _mae(adjusted_errors)
    baseline_bias, adjusted_bias = _mean(baseline_errors), _mean(adjusted_errors)
    improvement = baseline_mae - adjusted_mae
    print(
        f"  [{label}] held out on GW{TRAIN_LAST_ORIGIN + 1}-{LAST_ORIGIN} (n={len(test_records)}): "
        f"baseline MAE={baseline_mae:.4f} (bias={baseline_bias:+.4f}) -> "
        f"rotation-corrected MAE={adjusted_mae:.4f} (bias={adjusted_bias:+.4f}), "
        f"improvement={improvement:+.4f}"
    )
    if improvement > MIN_REAL_IMPROVEMENT:
        print(f"  [{label}] -> beats baseline by more than {MIN_REAL_IMPROVEMENT} MAE: a real improvement.")
    else:
        print(
            f"  [{label}] -> does not beat baseline by more than {MIN_REAL_IMPROVEMENT} MAE: "
            "not a real improvement."
        )
    return improvement


def print_rotation_sanity_table(season_label, team_sequences, origin_gw):
    """Per-team rotation index at the latest evaluated origin -- range/ranking sanity check."""
    rows = []
    for team_name, fixtures in team_sequences.items():
        index, fixtures_played = team_rotation_index(fixtures, origin_gw)
        if index is not None:
            rows.append((team_name, index, fixtures_played))
    rows.sort(key=lambda r: r[1], reverse=True)
    print(f"  {season_label} (as of GW{origin_gw}, {len(rows)} teams with enough pre-origin fixture pairs):")
    for team_name, index, fixtures_played in rows:
        print(f"    {team_name:<18} rotation_index={index:.2f}  (fixtures_played={fixtures_played})")


def main():
    records = []  # one dict per player/origin/horizon comparison with a trustworthy team rotation index
    per_season_error_vs_rotation = {}

    for season_label in FIT_SEASONS:
        season = load_season(ROOT / "data" / "history" / season_label, label=season_label)
        team_sequences = load_team_fixture_sequences(season_label)
        player_rows = load_player_rows(season_label)
        comparisons = season_comparisons(
            season, horizons=(HORIZON,), first_origin=FIRST_ORIGIN, last_origin=LAST_ORIGIN
        )
        season_pairs = []
        for row in comparisons:
            rows_for_player = player_rows.get(row["element_id"])
            if not rows_for_player:
                continue
            context = player_pre_origin_context(rows_for_player, team_sequences, row["origin_gw"])
            if context is None:
                continue
            records.append(
                {
                    "season": season_label,
                    "origin_gw": row["origin_gw"],
                    "position": row["position"],
                    "rotation_index": context["rotation_index"],
                    "start_share": context["start_share"],
                    "error": row["error"],
                    "modeled_points": row["modeled_points"],
                    "actual_points": row["actual_points"],
                }
            )
            season_pairs.append((context["rotation_index"], row["error"]))
        per_season_error_vs_rotation[season_label] = season_pairs
        print(f"{season_label}: {len(season_pairs)} player/origin comparisons with a trustworthy team rotation index")

    non_nailed = [
        r
        for r in records
        if r["start_share"] is not None
        and NON_NAILED_START_SHARE[0] <= r["start_share"] <= NON_NAILED_START_SHARE[1]
    ]
    print(f"\nNon-nailed cohort (pre-origin start share in {NON_NAILED_START_SHARE}): n={len(non_nailed)}")

    error_vs_rotation = [(r["rotation_index"], r["error"]) for r in records]
    modeled_vs_actual = [(r["modeled_points"], r["actual_points"]) for r in records]
    rotation_vs_actual = [(r["rotation_index"], r["actual_points"]) for r in records]
    rotation_vs_modeled = [(r["rotation_index"], r["modeled_points"]) for r in records]
    non_nailed_error_vs_rotation = [(r["rotation_index"], r["error"]) for r in non_nailed]

    print("\n--- Correlations (Pearson r) ---")
    print(
        f"Sanity baseline -- modeled_points vs actual_points: "
        f"r={_format_r(pearson_r(modeled_vs_actual))} (n={len(modeled_vs_actual)})"
    )
    print(
        "  (Expect this noticeably above investigate_ict_index.py's 0.433: that script's join "
        "additionally requires 180+ pre-origin player minutes, which drops many near-zero/"
        "near-zero fringe-player pairs that correlate trivially well. This script's join is "
        "team-level (a trustworthy rotation index), not player-minutes-level, by design -- the "
        "plan doesn't gate the rotation join on individual player minutes -- so more of those "
        "easy pairs remain and the unfiltered season_comparisons() baseline itself is r=0.616.)"
    )
    overall_r = pearson_r(error_vs_rotation)
    print(
        f"Pre-origin team rotation index vs model error (actual - modeled), all seasons pooled: "
        f"r={_format_r(overall_r)} (n={len(error_vs_rotation)})"
    )
    for season_label, pairs in per_season_error_vs_rotation.items():
        r = pearson_r(pairs)
        print(f"  {season_label}: r={_format_r(r)} (n={len(pairs)})")

    print(
        f"\nPre-origin team rotation index vs forward actual_points (does rotation predict anything at all): "
        f"r={_format_r(pearson_r(rotation_vs_actual))} (n={len(rotation_vs_actual)})"
    )
    print(
        f"Pre-origin team rotation index vs modeled_points (is that signal already inside the model): "
        f"r={_format_r(pearson_r(rotation_vs_modeled))} (n={len(rotation_vs_modeled)})"
    )
    print(
        f"\nNon-nailed cohort only -- rotation index vs model error (where minutes over-projection "
        f"should concentrate if the hypothesis is right): "
        f"r={_format_r(pearson_r(non_nailed_error_vs_rotation))} (n={len(non_nailed_error_vs_rotation)})"
    )

    print(
        "\nInterpretation: a near-zero r means the model's current errors are not "
        "explained by a team's rotation index -- team-context rotation would be redundant "
        "with what the model's minutes/confidence machinery already captures. A meaningfully "
        "positive r (players at high-rotation clubs under-projected) or negative r (over-"
        "projected, matching the hypothesis) would be a real, actionable signal, especially "
        "if it strengthens in the non-nailed cohort rather than only appearing pooled."
    )

    print("\n--- Per-team rotation index sanity table (range + known heavy-rotation clubs) ---")
    for season_label in FIT_SEASONS:
        team_sequences = load_team_fixture_sequences(season_label)
        print_rotation_sanity_table(season_label, team_sequences, LAST_ORIGIN)

    print("\n--- Out-of-sample MAE check (the actual adoption bar) ---")
    train_records = [r for r in records if r["origin_gw"] <= TRAIN_LAST_ORIGIN]
    test_records = [r for r in records if r["origin_gw"] > TRAIN_LAST_ORIGIN]
    full_improvement = evaluate_variant("full population", train_records, test_records)

    train_non_nailed = [r for r in non_nailed if r["origin_gw"] <= TRAIN_LAST_ORIGIN]
    test_non_nailed = [r for r in non_nailed if r["origin_gw"] > TRAIN_LAST_ORIGIN]
    non_nailed_improvement = evaluate_variant("non-nailed cohort", train_non_nailed, test_non_nailed)

    print(f"\n--- Verdict (bar: improvement > {MIN_REAL_IMPROVEMENT} MAE on either pre-registered variant) ---")
    best_improvement = max(
        [v for v in (full_improvement, non_nailed_improvement) if v is not None], default=None
    )
    if best_improvement is not None and best_improvement > MIN_REAL_IMPROVEMENT:
        print(
            f"PASS/FAIL: ADOPT -- best held-out improvement {best_improvement:+.4f} MAE beats the "
            f"{MIN_REAL_IMPROVEMENT} bar. Green-lights a follow-up model-change task; does not, by "
            f"itself, change the model."
        )
    else:
        shown = f"{best_improvement:+.4f}" if best_improvement is not None else "n/a"
        print(
            f"PASS/FAIL: DECLINE -- best held-out improvement {shown} MAE does not beat the "
            f"{MIN_REAL_IMPROVEMENT} bar. Clean negative result for candidate (c)."
        )


if __name__ == "__main__":
    main()
