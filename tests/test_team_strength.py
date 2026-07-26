import math
import unittest

from fpl_intel.team_strength import (
    MIN_ROUNDS,
    clean_sheet_probability,
    completed_rounds,
    expected_goals,
    fit_team_strength,
    matches_from_fixtures,
    should_use_team_strength,
)


def _synthetic_matches(true_ratings, home_advantage, league_avg_goals, age=0):
    """Generate matches whose goals are exactly the model's own formula,
    so a correct fitter should recover the true ratings closely."""
    teams = list(true_ratings)
    matches = []
    for home in teams:
        for away in teams:
            if home == away:
                continue
            home_goals = true_ratings[home]["attack"] * true_ratings[away]["defense"] * home_advantage * league_avg_goals
            away_goals = true_ratings[away]["attack"] * true_ratings[home]["defense"] * league_avg_goals
            matches.append({
                "home_team": home, "away_team": away,
                "home_goals": home_goals, "away_goals": away_goals, "age": age,
            })
    return matches


class FitTeamStrengthRecoveryTests(unittest.TestCase):
    def test_recovers_known_ratings_from_synthetic_league(self):
        # This class of multiplicative model is only identifiable up to a
        # scale trade-off between attack and defense (see team_strength.py's
        # normalization comment) -- so the correctness criterion is that
        # expected_goals() reproduces the true fixture-level rates exactly,
        # not that raw attack/defense values match some particular true
        # rating's own (arbitrary) scale.
        true_ratings = {
            "Strong": {"attack": 1.4, "defense": 1.3},   # good attack, poor defense
            "Weak": {"attack": 0.7, "defense": 0.6},     # poor attack, good defense
            "Average": {"attack": 1.0, "defense": 1.0},
        }
        home_advantage, league_avg_goals = 1.2, 1.5
        matches = _synthetic_matches(true_ratings, home_advantage, league_avg_goals)

        fitted = fit_team_strength(matches, half_life_matches=1000.0, iterations=60)

        for home in true_ratings:
            for away in true_ratings:
                if home == away:
                    continue
                true_home_xg = true_ratings[home]["attack"] * true_ratings[away]["defense"] * home_advantage * league_avg_goals
                true_away_xg = true_ratings[away]["attack"] * true_ratings[home]["defense"] * league_avg_goals
                fitted_home_xg, fitted_away_xg = expected_goals(fitted, home, away)
                self.assertAlmostEqual(fitted_home_xg, true_home_xg, places=2)
                self.assertAlmostEqual(fitted_away_xg, true_away_xg, places=2)

        self.assertAlmostEqual(fitted["home_advantage"], home_advantage, places=2)
        # Normalization pins mean(attack) = mean(defense) = 1, so the relative
        # ordering (not the absolute values) should match the true ratings.
        self.assertGreater(fitted["teams"]["Strong"]["attack"], fitted["teams"]["Average"]["attack"])
        self.assertGreater(fitted["teams"]["Average"]["attack"], fitted["teams"]["Weak"]["attack"])
        self.assertGreater(fitted["teams"]["Strong"]["defense"], fitted["teams"]["Average"]["defense"])
        self.assertGreater(fitted["teams"]["Average"]["defense"], fitted["teams"]["Weak"]["defense"])
        self.assertAlmostEqual(sum(t["attack"] for t in fitted["teams"].values()) / 3, 1.0, places=6)
        self.assertAlmostEqual(sum(t["defense"] for t in fitted["teams"].values()) / 3, 1.0, places=6)

    def test_empty_matches_returns_defaults(self):
        fitted = fit_team_strength([])
        self.assertEqual(fitted["teams"], {})
        self.assertGreater(fitted["home_advantage"], 0)
        self.assertGreater(fitted["league_avg_goals"], 0)


class DecayWeightingTests(unittest.TestCase):
    def test_recent_matches_dominate_a_short_half_life_fit(self):
        # Old matches say "Team" is weak; recent matches say "Team" is now strong.
        old_ratings = {"Team": {"attack": 0.6, "defense": 1.0}, "Rival": {"attack": 1.0, "defense": 1.0}}
        new_ratings = {"Team": {"attack": 1.6, "defense": 1.0}, "Rival": {"attack": 1.0, "defense": 1.0}}
        old_matches = _synthetic_matches(old_ratings, home_advantage=1.0, league_avg_goals=1.5, age=50)
        new_matches = _synthetic_matches(new_ratings, home_advantage=1.0, league_avg_goals=1.5, age=0)

        fitted_short_half_life = fit_team_strength(old_matches + new_matches, half_life_matches=3.0, iterations=60)
        fitted_long_half_life = fit_team_strength(old_matches + new_matches, half_life_matches=500.0, iterations=60)

        # Compare Team's rating relative to Rival's, not raw values -- the
        # mean(attack)=1 normalization means raw values depend on Rival too,
        # but the ratio between them is invariant to that normalization and
        # is exactly what "how much stronger is Team than Rival" means.
        short_ratio = fitted_short_half_life["teams"]["Team"]["attack"] / fitted_short_half_life["teams"]["Rival"]["attack"]
        long_ratio = fitted_long_half_life["teams"]["Team"]["attack"] / fitted_long_half_life["teams"]["Rival"]["attack"]

        # A short half-life should land much closer to the recent (strong) rating
        # than a long half-life, which treats old and new matches almost equally.
        self.assertGreater(short_ratio, long_ratio)
        self.assertGreater(short_ratio, 1.3)


class ExpectedGoalsTests(unittest.TestCase):
    def test_expected_goals_uses_attack_defense_and_home_advantage(self):
        ratings = {
            "teams": {
                "Home": {"attack": 1.2, "defense": 1.0},
                "Away": {"attack": 0.8, "defense": 1.1},
            },
            "home_advantage": 1.3,
            "league_avg_goals": 1.5,
        }
        home_xg, away_xg = expected_goals(ratings, "Home", "Away")
        self.assertAlmostEqual(home_xg, 1.2 * 1.1 * 1.3 * 1.5)
        self.assertAlmostEqual(away_xg, 0.8 * 1.0 * 1.5)

    def test_missing_team_falls_back_to_neutral_rating(self):
        ratings = {"teams": {}, "home_advantage": 1.2, "league_avg_goals": 1.5}
        home_xg, away_xg = expected_goals(ratings, "Unknown1", "Unknown2")
        self.assertAlmostEqual(home_xg, 1.0 * 1.0 * 1.2 * 1.5)
        self.assertAlmostEqual(away_xg, 1.0 * 1.0 * 1.5)


class CleanSheetProbabilityTests(unittest.TestCase):
    def test_matches_poisson_zero_probability(self):
        self.assertAlmostEqual(clean_sheet_probability(0.0), 1.0)
        self.assertAlmostEqual(clean_sheet_probability(1.5), math.exp(-1.5))


class ShouldUseTeamStrengthTests(unittest.TestCase):
    def test_gated_by_min_rounds(self):
        self.assertFalse(should_use_team_strength(MIN_ROUNDS - 1))
        self.assertTrue(should_use_team_strength(MIN_ROUNDS))


class FixtureAdapterTests(unittest.TestCase):
    def _fixtures(self):
        return [
            {"event": 1, "team_h": 1, "team_a": 2, "team_h_score": 2, "team_a_score": 1},
            {"event": 2, "team_h": 2, "team_a": 1, "team_h_score": 0, "team_a_score": 0},
            {"event": 3, "team_h": 1, "team_a": 2, "team_h_score": None, "team_a_score": None},  # not yet played
            {"event": 4, "team_h": 1, "team_a": 2, "team_h_score": 3, "team_a_score": 0},  # not before_event=3
        ]

    def test_matches_from_fixtures_excludes_future_and_unscored(self):
        matches = matches_from_fixtures(self._fixtures(), before_event=3)
        self.assertEqual(len(matches), 2)
        first = next(m for m in matches if m["home_team"] == 1)
        self.assertEqual(first["home_goals"], 2.0)
        self.assertEqual(first["away_goals"], 1.0)
        self.assertEqual(first["age"], 3 - 1 - 1)  # before_event - 1 - event

    def test_completed_rounds_counts_distinct_scored_events_before_cutoff(self):
        self.assertEqual(completed_rounds(self._fixtures(), before_event=3), 2)
        self.assertEqual(completed_rounds(self._fixtures(), before_event=1), 0)


if __name__ == "__main__":
    unittest.main()
