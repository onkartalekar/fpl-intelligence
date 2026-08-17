"""Component-level FPL scoring projection.

Replaces the single blended points-per-90 rate with additive components --
appearance, attacking (goals/assists), clean sheet, goals conceded, saves,
bonus, and a shrunk over/under-performance residual -- built from the
official expected-goals/assists/saves-per-90 fields FPL already publishes,
per SPECIFICATION.md's Projection data model (goal/assist expectation,
clean-sheet/save expectation, bonus expectation as distinct fields).

Goal, assist, clean-sheet, and save point values follow the official FPL
scoring rules (https://fantasy.premierleague.com/en/help/rules), current
as of the 2024/25 rule change that raised the goalkeeper goal bonus to 10
points. Re-check that page if FPL changes scoring again.

Opponent strength is not yet a fitted team model -- that is Phase 1 of
IMPLEMENTATION_PLAN.md. As an interim, attacking-rate scaling reuses the
same official fixture-difficulty (FDR) signal the pre-Phase-2 model used,
and clean-sheet/goals-conceded probability use hand-picked FDR lookup
tables. All three are explicit placeholders pending Phase 1 and Phase 3's
empirical fit -- see model.limitations in build_gw_recommendations.

The residual component exists because pure xG/xA-based attacking scoring
discards a real, measurable effect: some players sustain goal/bonus output
above what their own expected-goals rate would predict (elite finishing
skill, or scoring categories -- e.g. bonus from defensive actions -- this
model does not itemize separately). ``residual_rate`` captures a player's
historical points-per-90 above what the other components alone would have
predicted for a neutral (average-difficulty, full 90 minutes) match, then
shrinks that gap toward zero by the same reliability curve as every other
rate, so a small sample of luck is not mistaken for sustained skill.
"""

import math
from statistics import median

from .coefficients import load_coefficients


_COEFFICIENTS = load_coefficients()

_GOAL_POINTS = {1: 10, 2: 6, 3: 5, 4: 4}
_ASSIST_POINTS = 3
_CLEAN_SHEET_POINTS = {1: 4, 2: 4, 3: 1, 4: 0}
_SAVE_POINTS_PER_SAVE = 1 / 3
_DEFENSIVE_CONTRIBUTION_POINTS = 2
_DEFENSIVE_CONTRIBUTION_THRESHOLD = {2: 10, 3: 12, 4: 12}
_NEUTRAL_DIFFICULTY = 3  # reference fixture difficulty used to compute the residual

# Attack/clean-sheet/goals-conceded FDR scaling is fitted (Phase 3) directly
# from real historical results conditioned on official FDR -- still an
# interim proxy pending Phase 1's fitted team-strength model, but no longer
# a hand-picked guess -- see scripts/fit_coefficients.py.
_FDR_ATTACK_MULTIPLIER = _COEFFICIENTS["attack_multiplier_by_difficulty"]
_CLEAN_SHEET_PROBABILITY_BY_DIFFICULTY = _COEFFICIENTS["clean_sheet_probability_by_difficulty"]
_GOALS_CONCEDED_DIFFICULTY_MULTIPLIER = _COEFFICIENTS["goals_conceded_multiplier_by_difficulty"]
_RELIABILITY_DENOMINATOR = _COEFFICIENTS["reliability_denominator"]
_RELIABILITY_CAP = _COEFFICIENTS["reliability_cap"]
_RESIDUAL_RELIABILITY_DENOMINATOR_BY_POSITION = _COEFFICIENTS["residual_reliability_denominator_by_position"]
_RESIDUAL_RELIABILITY_CAP = _COEFFICIENTS["residual_reliability_cap"]

_RATE_FIELDS = {
    "goal_rate": "expected_goals_per_90",
    "assist_rate": "expected_assists_per_90",
    "goals_conceded_rate": "expected_goals_conceded_per_90",
    "save_rate": "saves_per_90",
    "defensive_contribution_rate": "defensive_contribution_per_90",
}
_DEFAULT_RATE_BASELINE = {
    "goal_rate": {1: 0.01, 2: 0.03, 3: 0.08, 4: 0.22},
    "assist_rate": {1: 0.01, 2: 0.05, 3: 0.09, 4: 0.07},
    "goals_conceded_rate": {1: 1.3, 2: 1.3, 3: 1.3, 4: 1.3},
    "save_rate": {1: 3.0, 2: 0.0, 3: 0.0, 4: 0.0},
    "defensive_contribution_rate": {1: 0.0, 2: 8.0, 3: 7.0, 4: 5.0},
}
_DEFAULT_BONUS_BASELINE = {1: 0.25, 2: 0.25, 3: 0.3, 4: 0.35}


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def component_rate_baselines(players):
    """Positional medians (minutes >= 900) for each shrinkage-eligible per-90 rate.

    Bonus has no official per-90 field, so its baseline is derived here from
    cumulative bonus/minutes per player rather than read directly, but is
    otherwise treated the same as the official rate fields -- a real
    positional median, not one flat constant for every position.
    """
    samples = {rate_name: {1: [], 2: [], 3: [], 4: []} for rate_name in _RATE_FIELDS}
    bonus_samples = {1: [], 2: [], 3: [], 4: []}
    for player in players:
        minutes = _number(player.get("minutes"))
        position_id = player.get("element_type")
        if minutes < 900 or position_id not in (1, 2, 3, 4):
            continue
        for rate_name, field in _RATE_FIELDS.items():
            samples[rate_name][position_id].append(_number(player.get(field)))
        bonus_samples[position_id].append(_number(player.get("bonus")) * 90 / minutes)
    defensive_scoring_enabled = not players or all(
        player.get("defensive_contribution_scoring_enabled", True) for player in players
    )
    baselines = {}
    for rate_name, by_position in samples.items():
        baselines[rate_name] = {
            position_id: (
                median(values)
                if values and (rate_name != "defensive_contribution_rate" or any(value > 0 for value in values))
                else _DEFAULT_RATE_BASELINE[rate_name][position_id]
            )
            for position_id, values in by_position.items()
        }
    if not defensive_scoring_enabled:
        baselines["defensive_contribution_rate"] = {position_id: 0.0 for position_id in (1, 2, 3, 4)}
    baselines["defensive_contribution_data_available"] = defensive_scoring_enabled and any(
        value > 0 for values in samples["defensive_contribution_rate"].values() for value in values
    )
    baselines["bonus_rate"] = {
        position_id: median(values) if values else _DEFAULT_BONUS_BASELINE[position_id]
        for position_id, values in bonus_samples.items()
    }
    return baselines


def _neutral_reference_points(rates_without_residual, position_id):
    """The component-implied points-per-90 for an average-difficulty full match.

    Used only to compute the residual against a player's real historical
    points-per-90 -- not itself a projection output.
    """
    return component_points_for_event(
        {**rates_without_residual, "residual_rate": 0.0},
        position_id,
        scenario_minutes=90.0,
        difficulty=_NEUTRAL_DIFFICULTY,
    )["total"]


def player_component_rates(player, baselines):
    """Shrink a player's official per-90 rates toward their positional baseline.

    Uses the same reliability curve as the legacy aggregate model (more
    career minutes -> more weight on the player's own observed rate), just
    applied per component instead of to one blended points-per-90 number.
    """
    position_id = player.get("element_type")
    minutes = _number(player.get("minutes"))
    reliability = min(_RELIABILITY_CAP, minutes / (minutes + _RELIABILITY_DENOMINATOR)) if minutes else 0.0
    rates = {}
    for rate_name, field in _RATE_FIELDS.items():
        observed = _number(player.get(field))
        baseline = baselines.get(rate_name, {}).get(position_id, 0.0)
        if rate_name == "defensive_contribution_rate" and not baselines.get("defensive_contribution_data_available"):
            observed = baseline
        rates[rate_name] = reliability * observed + (1 - reliability) * baseline

    bonus_observed = _number(player.get("bonus")) * 90 / minutes if minutes > 0 else 0.0
    bonus_baseline = baselines.get("bonus_rate", {}).get(position_id, _DEFAULT_BONUS_BASELINE.get(position_id, 0.3))
    rates["bonus_rate"] = reliability * bonus_observed + (1 - reliability) * bonus_baseline

    observed_points_p90 = _number(player.get("total_points")) * 90 / minutes if minutes > 0 else 0.0
    neutral_p90 = _neutral_reference_points(rates, position_id)
    residual = observed_points_p90 - neutral_p90
    # Shrink toward zero, not toward a baseline -- with no track record, assume
    # no skill beyond what the official rate stats already predict. Uses its
    # own fitted, per-position reliability denominator: a persistent scoring
    # residual can warrant a different trust curve than a raw per-90 rate
    # stat, and by how much differs by position (see IMPLEMENTATION_PLAN.md
    # Phase 3 continuation -- MID/FWD trust the residual with less data).
    residual_denominator = _RESIDUAL_RELIABILITY_DENOMINATOR_BY_POSITION.get(position_id, 900.0)
    residual_reliability = (
        min(_RESIDUAL_RELIABILITY_CAP, minutes / (minutes + residual_denominator)) if minutes else 0.0
    )
    rates["residual_rate"] = residual_reliability * residual
    return rates


def _poisson_probability_at_least(rate, threshold):
    if rate <= 0 or threshold <= 0:
        return 0.0 if rate <= 0 else 1.0
    below = sum(math.exp(-rate) * rate**count / math.factorial(count) for count in range(threshold))
    return max(0.0, min(1.0, 1.0 - below))


def _expected_goals_conceded_deduction(rate):
    """Expected negative points for -1 per complete pair under Poisson(rate)."""
    if rate <= 0:
        return 0.0
    expected_pairs = rate / 2 - (1 - math.exp(-2 * rate)) / 4
    return -expected_pairs


def component_points_for_event(
    rates, position_id, scenario_minutes, difficulty,
    expected_goals_for=None, expected_goals_against=None, league_avg_goals=None,
):
    """Additive scoring-component breakdown for one player, one fixture.

    ``expected_goals_for``/``expected_goals_against``/``league_avg_goals``
    are optional Phase 1 team-strength inputs (see team_strength.py); when
    given, they replace the FDR difficulty-bucket tables for attacking,
    clean-sheet, and goals-conceded scaling. Omit them (or pass None) to use
    the FDR tables, e.g. before enough same-season matches exist to fit
    team strength reliably.

    ``total`` is not floored at zero here -- callers summing multiple
    fixtures in a double gameweek, or blending in the GW1 ep_next signal,
    should apply that floor once at the end.
    """
    zero = {
        "appearance": 0.0, "attacking": 0.0, "clean_sheet": 0.0,
        "goals_conceded": 0.0, "defensive_contribution": 0.0,
        "saves": 0.0, "bonus": 0.0, "residual": 0.0, "total": 0.0,
    }
    if scenario_minutes <= 0:
        return zero

    minute_share = scenario_minutes / 90.0
    played_60_probability = min(1.0, scenario_minutes / 75.0)

    # Ramp 0->1 point across 0-60 minutes, then 1->2 across 60-80 minutes,
    # rather than a discontinuous jump exactly at the 60-minute threshold.
    if scenario_minutes >= 60:
        appearance = 1.0 + min(1.0, (scenario_minutes - 60.0) / 20.0)
    else:
        appearance = scenario_minutes / 60.0

    # Team-strength mode (Phase 1): when a fitted, fixture-specific expected
    # goals for/against is available, use it directly in place of the FDR
    # difficulty-bucket tables -- a per-team Poisson rate is strictly more
    # information than a 1-5 bucket. Falls back to the (also fitted, Phase 3)
    # FDR tables when team-strength ratings aren't available for this
    # fixture (e.g. too few same-season rounds so far -- see team_strength.py).
    if expected_goals_for is not None and league_avg_goals:
        attack_multiplier = expected_goals_for / league_avg_goals
    else:
        attack_multiplier = _FDR_ATTACK_MULTIPLIER.get(difficulty, 1.0)
    attacking = (
        rates["goal_rate"] * _GOAL_POINTS.get(position_id, 4)
        + rates["assist_rate"] * _ASSIST_POINTS
    ) * minute_share * attack_multiplier

    clean_sheet = 0.0
    goals_conceded = 0.0
    if position_id in (1, 2, 3):
        if expected_goals_against is not None:
            clean_sheet_probability = math.exp(-expected_goals_against)
        else:
            clean_sheet_probability = _CLEAN_SHEET_PROBABILITY_BY_DIFFICULTY.get(difficulty, 0.30)
        clean_sheet = (
            _CLEAN_SHEET_POINTS.get(position_id, 0) * clean_sheet_probability * played_60_probability
        )
    if position_id in (1, 2):
        if expected_goals_against is not None:
            goals_conceded_expectation = expected_goals_against
        else:
            conceded_multiplier = _GOALS_CONCEDED_DIFFICULTY_MULTIPLIER.get(difficulty, 1.0)
            goals_conceded_expectation = rates["goals_conceded_rate"] * conceded_multiplier
        goals_conceded = _expected_goals_conceded_deduction(
            goals_conceded_expectation * minute_share
        )

    defensive_contribution = 0.0
    threshold = _DEFENSIVE_CONTRIBUTION_THRESHOLD.get(position_id)
    if threshold:
        contribution_rate = max(0.0, rates.get("defensive_contribution_rate", 0.0)) * minute_share
        defensive_contribution = (
            _DEFENSIVE_CONTRIBUTION_POINTS
            * _poisson_probability_at_least(contribution_rate, threshold)
        )

    saves = 0.0
    if position_id == 1:
        saves = rates["save_rate"] * _SAVE_POINTS_PER_SAVE * minute_share

    bonus = rates["bonus_rate"] * minute_share
    residual = rates.get("residual_rate", 0.0) * minute_share

    return {
        "appearance": round(appearance, 3),
        "attacking": round(attacking, 3),
        "clean_sheet": round(clean_sheet, 3),
        "goals_conceded": round(goals_conceded, 3),
        "defensive_contribution": round(defensive_contribution, 3),
        "saves": round(saves, 3),
        "bonus": round(bonus, 3),
        "residual": round(residual, 3),
        "total": (
            appearance + attacking + clean_sheet + goals_conceded
            + defensive_contribution + saves + bonus + residual
        ),
    }
