import unittest

from fpl_intel.modeling import ml_minutes
from fpl_intel.modeling.ml_minutes import (
    FEATURE_NAMES,
    WEIGHTS,
    extract_features,
    predict_expected_minutes,
)


class ExtractFeaturesTests(unittest.TestCase):
    def test_returns_one_value_per_feature_name(self):
        player = {"minutes": 900, "starts": 10, "recent_history": []}
        features = extract_features(player, fixtures_played=10)
        self.assertEqual(len(features), len(FEATURE_NAMES))
        self.assertEqual(features[0], 1.0)  # intercept

    def test_season_long_shares_use_fixtures_played_as_denominator(self):
        player = {"minutes": 900, "starts": 10, "recent_history": []}
        features = extract_features(player, fixtures_played=10)
        season_start_share = features[FEATURE_NAMES.index("season_start_share")]
        season_minutes_per_game = features[FEATURE_NAMES.index("season_minutes_per_game_90")]
        self.assertAlmostEqual(season_start_share, 1.0)  # 10 starts / 10 fixtures
        self.assertAlmostEqual(season_minutes_per_game, 1.0)  # 90 min/game / 90

    def test_recency_window_falls_back_to_season_long_values_when_history_is_empty(self):
        """Every live player today has no recent_history (see module docstring) -- this
        must degrade gracefully rather than divide by zero or produce an unbounded value."""
        player = {"minutes": 450, "starts": 5, "recent_history": []}
        features = extract_features(player, fixtures_played=5)
        season_start_share = features[FEATURE_NAMES.index("season_start_share")]
        last3_start_rate = features[FEATURE_NAMES.index("last3_start_rate")]
        trend = features[FEATURE_NAMES.index("trend")]
        self.assertAlmostEqual(last3_start_rate, season_start_share)
        self.assertAlmostEqual(trend, 0.0)

    def test_recency_window_uses_last_three_recorded_gameweeks(self):
        history = [
            {"minutes": 90, "started": True},
            {"minutes": 0, "started": False},
            {"minutes": 60, "started": True},
            {"minutes": 90, "started": True},
        ]
        player = {"minutes": 240, "starts": 3, "recent_history": history}
        features = extract_features(player, fixtures_played=4)
        last3_start_rate = features[FEATURE_NAMES.index("last3_start_rate")]
        last3_avg_minutes_90 = features[FEATURE_NAMES.index("last3_avg_minutes_90")]
        # Last 3 entries only: 0/False, 60/True, 90/True.
        self.assertAlmostEqual(last3_start_rate, 2 / 3)
        self.assertAlmostEqual(last3_avg_minutes_90, (0 + 60 + 90) / 3 / 90.0)

    def test_fixtures_played_is_clamped_into_a_sane_range(self):
        player = {"minutes": 0, "starts": 0, "recent_history": []}
        # Neither zero nor absurdly large fixtures_played should raise or divide by zero.
        low = extract_features(player, fixtures_played=0)
        high = extract_features(player, fixtures_played=1000)
        self.assertTrue(all(isinstance(value, float) for value in low))
        self.assertTrue(all(isinstance(value, float) for value in high))

    def test_maturity_rises_with_fixtures_played_and_caps_at_one(self):
        player = {"minutes": 0, "starts": 0, "recent_history": []}
        low_maturity = extract_features(player, fixtures_played=2)[FEATURE_NAMES.index("maturity")]
        capped_maturity = extract_features(player, fixtures_played=30)[FEATURE_NAMES.index("maturity")]
        self.assertLess(low_maturity, capped_maturity)
        self.assertAlmostEqual(capped_maturity, 1.0)


class PredictExpectedMinutesTests(unittest.TestCase):
    def test_zero_availability_returns_zero_without_evaluating_features(self):
        player = {"minutes": 900, "starts": 10, "recent_history": []}
        self.assertEqual(predict_expected_minutes(player, fixtures_played=10, availability_multiplier=0), 0.0)

    def test_prediction_is_bounded_between_zero_and_ninety(self):
        everyday_starter = {"minutes": 3420, "starts": 38, "recent_history": []}
        never_played = {"minutes": 0, "starts": 0, "recent_history": []}
        high = predict_expected_minutes(everyday_starter, fixtures_played=38)
        low = predict_expected_minutes(never_played, fixtures_played=38)
        self.assertLessEqual(high, 90.0)
        self.assertGreaterEqual(low, 0.0)

    def test_availability_multiplier_scales_the_prediction(self):
        player = {"minutes": 900, "starts": 10, "recent_history": []}
        full = predict_expected_minutes(player, fixtures_played=10, availability_multiplier=1.0)
        halved = predict_expected_minutes(player, fixtures_played=10, availability_multiplier=0.5)
        self.assertAlmostEqual(halved, round(full * 0.5, 1), delta=0.15)

    def test_regular_starter_predicted_higher_than_a_fringe_player(self):
        starter = {"minutes": 3420, "starts": 38, "recent_history": []}
        fringe = {"minutes": 45, "starts": 0, "recent_history": []}
        self.assertGreater(
            predict_expected_minutes(starter, fixtures_played=38),
            predict_expected_minutes(fringe, fixtures_played=38),
        )

    def test_matches_manual_dot_product_of_weights_and_features(self):
        player = {"minutes": 900, "starts": 10, "recent_history": []}
        features = extract_features(player, fixtures_played=10)
        expected = round(min(90.0, max(0.0, sum(w * f for w, f in zip(WEIGHTS, features)))), 1)
        self.assertEqual(predict_expected_minutes(player, fixtures_played=10), expected)


class BuildShadowForecastTests(unittest.TestCase):
    def test_returns_none_when_bootstrap_has_no_elements(self):
        self.assertIsNone(
            ml_minutes.build_shadow_forecast({"elements": [], "element_types": []}, [], "2026-08-20T12:00:00Z")
        )

    def test_returns_none_without_fixtures(self):
        bootstrap = {
            "elements": [{"id": 1, "element_type": 1, "team": 1, "minutes": 90, "starts": 1, "status": "a"}],
            "element_types": [{"id": 1}],
            "teams": [{"id": 1, "name": "Alpha"}],
        }
        self.assertIsNone(ml_minutes.build_shadow_forecast(bootstrap, [], "2026-08-20T12:00:00Z"))


if __name__ == "__main__":
    unittest.main()
