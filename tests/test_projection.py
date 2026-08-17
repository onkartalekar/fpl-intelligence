import math
import unittest

from fpl_intel.modeling.projection import (
    _CLEAN_SHEET_PROBABILITY_BY_DIFFICULTY,
    _FDR_ATTACK_MULTIPLIER,
    _GOALS_CONCEDED_DIFFICULTY_MULTIPLIER,
    component_points_for_event,
    component_rate_baselines,
    player_component_rates,
)


class ComponentRateBaselinesTests(unittest.TestCase):
    def test_computes_positional_median_from_qualifying_players(self):
        players = [
            {"element_type": 4, "minutes": 1000, "expected_goals_per_90": 0.4, "expected_assists_per_90": 0.1,
             "expected_goals_conceded_per_90": 1.2, "saves_per_90": 0.0},
            {"element_type": 4, "minutes": 1200, "expected_goals_per_90": 0.6, "expected_assists_per_90": 0.2,
             "expected_goals_conceded_per_90": 1.4, "saves_per_90": 0.0},
            # Below the 900-minute qualification threshold -- must be excluded.
            {"element_type": 4, "minutes": 100, "expected_goals_per_90": 5.0, "expected_assists_per_90": 5.0,
             "expected_goals_conceded_per_90": 5.0, "saves_per_90": 5.0},
        ]
        baselines = component_rate_baselines(players)
        self.assertEqual(baselines["goal_rate"][4], 0.5)  # median of 0.4, 0.6
        self.assertAlmostEqual(baselines["assist_rate"][4], 0.15)

    def test_disables_defensive_contribution_for_pre_rule_scoring_eras(self):
        player = {
            "element_type": 2,
            "minutes": 1000,
            "total_points": 50,
            "bonus": 0,
            "expected_goals_per_90": 0.0,
            "expected_assists_per_90": 0.0,
            "expected_goals_conceded_per_90": 1.2,
            "saves_per_90": 0.0,
            "defensive_contribution_per_90": 10.0,
            "defensive_contribution_scoring_enabled": False,
        }

        baselines = component_rate_baselines([player])
        rates = player_component_rates(player, baselines)

        self.assertEqual(baselines["defensive_contribution_rate"][2], 0.0)
        self.assertEqual(rates["defensive_contribution_rate"], 0.0)

    def test_falls_back_to_default_when_no_qualifying_players(self):
        baselines = component_rate_baselines([])
        self.assertEqual(baselines["goal_rate"][2], 0.03)
        self.assertEqual(baselines["save_rate"][1], 3.0)


class PlayerComponentRatesTests(unittest.TestCase):
    def test_blends_observed_rate_with_baseline_by_reliability(self):
        baselines = {
            "goal_rate": {4: 0.2}, "assist_rate": {4: 0.1},
            "goals_conceded_rate": {4: 1.3}, "save_rate": {4: 0.0},
            "bonus_rate": {4: 0.3},
        }
        player = {
            "element_type": 4, "minutes": 900, "expected_goals_per_90": 0.6,
            "expected_assists_per_90": 0.3, "expected_goals_conceded_per_90": 1.0,
            "saves_per_90": 0.0, "bonus": 9, "total_points": 0,
        }
        rates = player_component_rates(player, baselines)
        reliability = min(0.82, 900 / (900 + 900))
        expected_goal_rate = reliability * 0.6 + (1 - reliability) * 0.2
        self.assertAlmostEqual(rates["goal_rate"], expected_goal_rate)
        expected_bonus_rate = reliability * (9 * 90 / 900) + (1 - reliability) * 0.3
        self.assertAlmostEqual(rates["bonus_rate"], expected_bonus_rate)

    def test_zero_minutes_falls_entirely_to_baseline(self):
        baselines = {
            "goal_rate": {3: 0.1}, "assist_rate": {3: 0.05},
            "goals_conceded_rate": {3: 1.3}, "save_rate": {3: 0.0},
        }
        player = {"element_type": 3, "minutes": 0, "expected_goals_per_90": 9.0, "bonus": 0}
        rates = player_component_rates(player, baselines)
        self.assertEqual(rates["goal_rate"], 0.1)
        self.assertEqual(rates["bonus_rate"], 0.3)  # default bonus baseline


class ComponentPointsForEventTests(unittest.TestCase):
    def test_zero_minutes_yields_all_zero_components(self):
        rates = {"goal_rate": 1.0, "assist_rate": 1.0, "goals_conceded_rate": 1.0, "save_rate": 1.0, "bonus_rate": 1.0}
        result = component_points_for_event(rates, position_id=4, scenario_minutes=0, difficulty=3)
        self.assertEqual(result["total"], 0.0)
        self.assertEqual(result["appearance"], 0.0)

    def test_appearance_ramps_from_zero_to_two_across_minute_thresholds(self):
        rates = {"goal_rate": 0.0, "assist_rate": 0.0, "goals_conceded_rate": 0.0, "save_rate": 0.0, "bonus_rate": 0.0}
        at_30 = component_points_for_event(rates, position_id=3, scenario_minutes=30, difficulty=3)
        at_60 = component_points_for_event(rates, position_id=3, scenario_minutes=60, difficulty=3)
        at_80 = component_points_for_event(rates, position_id=3, scenario_minutes=80, difficulty=3)
        at_90 = component_points_for_event(rates, position_id=3, scenario_minutes=90, difficulty=3)
        self.assertAlmostEqual(at_30["appearance"], 0.5)
        self.assertAlmostEqual(at_60["appearance"], 1.0)
        self.assertAlmostEqual(at_80["appearance"], 2.0)
        self.assertAlmostEqual(at_90["appearance"], 2.0)

    def test_forward_attacking_points_hand_computed_with_no_defensive_components(self):
        rates = {"goal_rate": 0.5, "assist_rate": 0.2, "goals_conceded_rate": 1.3, "save_rate": 0.0, "bonus_rate": 0.0}
        result = component_points_for_event(rates, position_id=4, scenario_minutes=90, difficulty=3)
        # Reference the module's own (fitted) table rather than duplicating a
        # literal that legitimately changes when config/model-coefficients.json is refit.
        expected_attacking = (0.5 * 4 + 0.2 * 3) * _FDR_ATTACK_MULTIPLIER[3]
        self.assertAlmostEqual(result["attacking"], round(expected_attacking, 3))
        self.assertEqual(result["clean_sheet"], 0.0)  # forwards get no clean-sheet value
        self.assertEqual(result["goals_conceded"], 0.0)  # forwards have no goals-conceded penalty
        self.assertEqual(result["saves"], 0.0)

    def test_defender_clean_sheet_and_goals_conceded_hand_computed(self):
        rates = {"goal_rate": 0.0, "assist_rate": 0.0, "goals_conceded_rate": 1.2, "save_rate": 0.0, "bonus_rate": 0.0}
        result = component_points_for_event(rates, position_id=2, scenario_minutes=90, difficulty=1)
        # Reference the module's own (fitted) tables rather than duplicating literals
        # that legitimately change when config/model-coefficients.json is refit.
        clean_sheet_probability = _CLEAN_SHEET_PROBABILITY_BY_DIFFICULTY[1]
        goals_conceded_multiplier = _GOALS_CONCEDED_DIFFICULTY_MULTIPLIER[1]
        self.assertAlmostEqual(result["clean_sheet"], round(4 * clean_sheet_probability * 1.0, 3))
        lam = 1.2 * goals_conceded_multiplier
        expected_deduction = -(lam / 2 - (1 - math.exp(-2 * lam)) / 4)
        self.assertAlmostEqual(result["goals_conceded"], round(expected_deduction, 3))

    def test_defensive_contribution_uses_position_threshold_probability(self):
        rates = {
            "goal_rate": 0.0,
            "assist_rate": 0.0,
            "goals_conceded_rate": 0.0,
            "save_rate": 0.0,
            "bonus_rate": 0.0,
            "defensive_contribution_rate": 12.0,
        }
        midfielder = component_points_for_event(rates, position_id=3, scenario_minutes=90, difficulty=3)
        goalkeeper = component_points_for_event(rates, position_id=1, scenario_minutes=90, difficulty=3)
        probability_at_least_12 = 1 - sum(math.exp(-12) * 12**count / math.factorial(count) for count in range(12))

        self.assertAlmostEqual(midfielder["defensive_contribution"], round(2 * probability_at_least_12, 3))
        self.assertEqual(goalkeeper["defensive_contribution"], 0.0)

    def test_goalkeeper_saves_points_hand_computed(self):
        rates = {"goal_rate": 0.0, "assist_rate": 0.0, "goals_conceded_rate": 1.0, "save_rate": 3.0, "bonus_rate": 0.0}
        result = component_points_for_event(rates, position_id=1, scenario_minutes=90, difficulty=3)
        self.assertAlmostEqual(result["saves"], round(3.0 * (1 / 3) * 1.0, 3))

    def test_midfielder_gets_clean_sheet_value_but_no_goals_conceded_penalty(self):
        rates = {"goal_rate": 0.0, "assist_rate": 0.0, "goals_conceded_rate": 5.0, "save_rate": 0.0, "bonus_rate": 0.0}
        result = component_points_for_event(rates, position_id=3, scenario_minutes=90, difficulty=3)
        self.assertGreater(result["clean_sheet"], 0.0)
        self.assertEqual(result["goals_conceded"], 0.0)


class TeamStrengthModeTests(unittest.TestCase):
    """When Phase 1 team-strength values are supplied, they replace the FDR
    difficulty-bucket tables entirely -- these should NOT match the
    difficulty-based results computed elsewhere in this file."""

    def test_attacking_uses_expected_goals_for_over_league_average(self):
        rates = {"goal_rate": 0.5, "assist_rate": 0.2, "goals_conceded_rate": 1.3, "save_rate": 0.0, "bonus_rate": 0.0}
        result = component_points_for_event(
            rates, position_id=4, scenario_minutes=90, difficulty=3,
            expected_goals_for=3.0, expected_goals_against=1.0, league_avg_goals=1.5,
        )
        expected_attack_multiplier = 3.0 / 1.5  # 2x a strong attacking fixture
        self.assertAlmostEqual(result["attacking"], round((0.5 * 4 + 0.2 * 3) * expected_attack_multiplier, 3))

    def test_clean_sheet_uses_poisson_probability_from_expected_goals_against(self):
        rates = {"goal_rate": 0.0, "assist_rate": 0.0, "goals_conceded_rate": 5.0, "save_rate": 0.0, "bonus_rate": 0.0}
        result = component_points_for_event(
            rates, position_id=2, scenario_minutes=90, difficulty=1,  # difficulty=1 would normally mean a HIGH clean-sheet chance
            expected_goals_for=0.5, expected_goals_against=2.5, league_avg_goals=1.5,  # but team-strength says a leaky fixture
        )
        expected_probability = math.exp(-2.5)
        self.assertAlmostEqual(result["clean_sheet"], round(4 * expected_probability * 1.0, 3))
        # Goals-conceded deduction uses the exact expected number of complete
        # two-goal pairs under a Poisson model.
        lam = 2.5
        expected_deduction = -(lam / 2 - (1 - math.exp(-2 * lam)) / 4)
        self.assertAlmostEqual(result["goals_conceded"], round(expected_deduction, 3))

    def test_missing_league_avg_goals_falls_back_to_fdr_attack_table(self):
        rates = {"goal_rate": 0.5, "assist_rate": 0.2, "goals_conceded_rate": 1.3, "save_rate": 0.0, "bonus_rate": 0.0}
        with_team_strength = component_points_for_event(
            rates, position_id=4, scenario_minutes=90, difficulty=3, expected_goals_for=3.0,
        )
        without = component_points_for_event(rates, position_id=4, scenario_minutes=90, difficulty=3)
        self.assertEqual(with_team_strength["attacking"], without["attacking"])


if __name__ == "__main__":
    unittest.main()
