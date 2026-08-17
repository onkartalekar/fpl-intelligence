import unittest

from fpl_intel.modeling.minutes import (
    MIN_APPEARANCES,
    expected_minutes_from_history,
    is_rotation_risk,
    minutes_scenarios_from_history,
    should_use_recency_model,
)


def _row(minutes, started):
    return {"minutes": minutes, "started": started}


class ShouldUseRecencyModelTests(unittest.TestCase):
    def test_gated_by_min_appearances(self):
        self.assertFalse(should_use_recency_model([_row(90, True)] * (MIN_APPEARANCES - 1)))
        self.assertTrue(should_use_recency_model([_row(90, True)] * MIN_APPEARANCES))

    def test_empty_history_is_false(self):
        self.assertFalse(should_use_recency_model([]))


class ExpectedMinutesFromHistoryTests(unittest.TestCase):
    def test_nailed_on_starter_gets_near_full_minutes(self):
        history = [_row(90, True)] * 6
        expected = expected_minutes_from_history(history, half_life_matches=4.0)
        self.assertGreater(expected, 85.0)

    def test_never_featured_gets_zero(self):
        history = [_row(0, False)] * 6
        expected = expected_minutes_from_history(history, half_life_matches=4.0)
        self.assertEqual(expected, 0.0)

    def test_recently_benched_player_drops_below_a_stale_season_average(self):
        # Started every match early, then benched for the last 4 -- a season
        # total/38 average would still look decent; recency weighting should not.
        history = [_row(90, True)] * 10 + [_row(0, False)] * 4
        recency_weighted = expected_minutes_from_history(history, half_life_matches=3.0)
        season_average = sum(row["minutes"] for row in history) / len(history)
        self.assertLess(recency_weighted, season_average)

    def test_zero_availability_multiplier_yields_zero(self):
        history = [_row(90, True)] * 6
        self.assertEqual(expected_minutes_from_history(history, availability_multiplier=0.0), 0.0)

    def test_empty_history_yields_zero(self):
        self.assertEqual(expected_minutes_from_history([]), 0.0)

    def test_hand_computed_mixed_starts_and_sub_appearances(self):
        # Use a very long half-life so weights are effectively uniform, making
        # the expected value easy to hand-verify.
        history = [_row(90, True), _row(90, True), _row(20, False)]
        expected = expected_minutes_from_history(history, half_life_matches=1000.0)
        # start_share = 2/3, sub_share = 1/3, avg_started = 90, avg_sub = 20
        hand_computed = (2 / 3) * 90 + (1 / 3) * 20
        self.assertAlmostEqual(expected, round(hand_computed, 1), places=1)


class IsRotationRiskTests(unittest.TestCase):
    def test_stable_starter_is_not_flagged(self):
        history = [_row(90, True)] * 8
        self.assertFalse(is_rotation_risk(history, half_life_matches=4.0))

    def test_recently_dropped_starter_is_flagged(self):
        # Length scales with the currently-configured MIN_APPEARANCES so this
        # test is meaningful even if that threshold is set high (e.g. Phase 4
        # disabled via config -- see IMPLEMENTATION_PLAN.md).
        appearances = max(MIN_APPEARANCES + 6, 14)
        history = [_row(90, True)] * (appearances - 6) + [_row(0, False)] * 6
        self.assertTrue(is_rotation_risk(history, half_life_matches=3.0))

    def test_too_few_appearances_is_never_flagged(self):
        history = [_row(90, True)] * (MIN_APPEARANCES - 1)
        self.assertFalse(is_rotation_risk(history))


class MinutesScenariosFromHistoryTests(unittest.TestCase):
    def test_empty_history_gives_flat_scenarios(self):
        scenarios = minutes_scenarios_from_history([])
        self.assertEqual(scenarios["conservative"], scenarios["balanced"])
        self.assertEqual(scenarios["balanced"], scenarios["aggressive"])

    def test_rotation_risk_player_gets_wider_spread_than_stable_starter(self):
        stable = [_row(90, True)] * 10
        volatile = [_row(90, True)] * 6 + [_row(0, False)] * 6

        stable_scenarios = minutes_scenarios_from_history(stable, half_life_matches=4.0)
        volatile_scenarios = minutes_scenarios_from_history(volatile, half_life_matches=4.0)

        stable_spread = stable_scenarios["aggressive"] - stable_scenarios["conservative"]
        volatile_spread = volatile_scenarios["aggressive"] - volatile_scenarios["conservative"]
        self.assertGreaterEqual(stable_spread, 0)
        self.assertGreater(volatile_spread, stable_spread)

    def test_aggressive_never_exceeds_ninety_minutes(self):
        history = [_row(90, True)] * 10
        scenarios = minutes_scenarios_from_history(history, half_life_matches=100.0)
        self.assertLessEqual(scenarios["aggressive"], 90.0)


if __name__ == "__main__":
    unittest.main()
