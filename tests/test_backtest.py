import copy
import csv
from pathlib import Path
import tempfile
import unittest

from fpl_intel.backtest import (
    build_backtest_report,
    build_origin_inputs,
    load_season,
    season_comparisons,
)


def _write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _write_season_directory(root):
    _write_csv(
        root / "teams.csv",
        ["id", "name", "short_name"],
        [[1, "Alpha FC", "ALP"], [2, "Beta FC", "BET"]],
    )
    _write_csv(
        root / "fixtures.csv",
        ["event", "team_h", "team_a", "team_h_difficulty", "team_a_difficulty"],
        [
            [1, 1, 2, 3, 3],
            [2, 2, 1, 3, 3],
            ["", 1, 2, 3, 3],  # unscheduled fixture, must be skipped
        ],
    )
    _write_csv(
        root / "merged_gw.csv",
        [
            "GW", "element", "name", "team", "position", "minutes", "starts", "total_points", "value",
            "expected_goals", "expected_assists", "expected_goals_conceded", "saves", "bonus",
            "defensive_contribution",
        ],
        [
            [1, 10, "Player Ten", "Alpha FC", "MID", 90, 1, 6, 55, 0.4, 0.1, 1.0, 0, 1, 8],
            [2, 10, "Player Ten", "Alpha FC", "MID", 45, 0, 2, 55, 0.1, 0.0, 0.5, 0, 0, 3],
            [1, 11, "Coach Bot", "Beta FC", "AM", 0, 0, 0, 40, 0.0, 0.0, 0.0, 0, 0, 0],  # unrecognized position, must be skipped
        ],
    )


def _row(element, name, position_id, team_name, gameweek, minutes, starts, total_points, now_cost=45,
         expected_goals=0.0, expected_assists=0.0, expected_goals_conceded=0.0, saves=0, bonus=0):
    return {
        "element": element, "name": name, "position_id": position_id, "team_name": team_name,
        "gameweek": gameweek, "minutes": minutes, "starts": starts, "total_points": total_points,
        "now_cost": now_cost, "expected_goals": expected_goals, "expected_assists": expected_assists,
        "expected_goals_conceded": expected_goals_conceded, "saves": saves, "bonus": bonus,
    }


class LoadSeasonTests(unittest.TestCase):
    def test_load_season_parses_and_filters_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_season_directory(root)
            season = load_season(root, label="test-season")

            self.assertEqual(season["label"], "test-season")
            self.assertEqual({team["name"] for team in season["teams"]}, {"Alpha FC", "Beta FC"})
            self.assertEqual(len(season["fixtures"]), 2)  # unscheduled fixture dropped
            self.assertEqual(len(season["rows"]), 2)  # AM row dropped
            self.assertEqual({row["element"] for row in season["rows"]}, {10})
            first_row = next(row for row in season["rows"] if row["gameweek"] == 1)
            self.assertAlmostEqual(first_row["expected_goals"], 0.4)
            self.assertEqual(first_row["bonus"], 1)
            self.assertEqual(first_row["defensive_contribution"], 8)
            self.assertTrue(season["defensive_contribution_scoring_enabled"])
            snapshot = build_origin_inputs(season, origin_gw=2)
            player = snapshot["elements"][0]
            self.assertEqual(player["defensive_contribution_per_90"], 8.0)
            self.assertTrue(player["defensive_contribution_scoring_enabled"])


class BuildOriginInputsTests(unittest.TestCase):
    def _season(self):
        return {
            "label": "test",
            "teams": [
                {"id": 1, "name": "Alpha FC", "short_name": "ALP"},
                {"id": 2, "name": "Beta FC", "short_name": "BET"},
            ],
            "fixtures": [
                {"event": gw, "team_h": 1, "team_a": 2, "team_h_difficulty": 3, "team_a_difficulty": 3}
                for gw in range(1, 6)
            ],
            "rows": [
                _row(10, "Player Ten", 3, "Alpha FC", gw, 90, 1, points, now_cost=55)
                for gw, points in ((1, 6), (2, 8), (3, 2))
            ],
        }

    def test_aggregates_only_strictly_prior_gameweeks(self):
        season = self._season()
        snapshot = build_origin_inputs(season, origin_gw=3)
        player = snapshot["elements"][0]
        self.assertEqual(player["minutes"], 180)  # gw1 + gw2 only
        self.assertEqual(player["total_points"], 14)  # 6 + 8, gw3 excluded
        self.assertEqual(player["element_type"], 3)
        self.assertEqual(player["team"], 1)

    def test_recent_history_is_ordered_oldest_to_newest_and_excludes_future(self):
        season = self._season()
        snapshot = build_origin_inputs(season, origin_gw=3)
        player = snapshot["elements"][0]
        self.assertEqual(player["recent_history"], [
            {"minutes": 90, "started": True},
            {"minutes": 90, "started": True},
        ])  # gw1, gw2 only -- gw3 is not yet in the past relative to origin_gw=3

    def test_snapshot_is_unaffected_by_later_rows_no_lookahead(self):
        season = self._season()
        before = build_origin_inputs(season, origin_gw=2)
        season["rows"] = [row for row in season["rows"] if row["gameweek"] < 2]
        after = build_origin_inputs(season, origin_gw=2)
        self.assertEqual(before, after)

    def test_origin_gw_with_no_history_yields_no_elements(self):
        season = self._season()
        snapshot = build_origin_inputs(season, origin_gw=1)
        self.assertEqual(snapshot["elements"], [])


class SeasonComparisonsTests(unittest.TestCase):
    def _season(self, label="test"):
        teams = [
            {"id": 1, "name": "Alpha FC", "short_name": "ALP"},
            {"id": 2, "name": "Beta FC", "short_name": "BET"},
        ]
        fixtures = [
            {"event": gw, "team_h": 1, "team_a": 2, "team_h_difficulty": 3, "team_a_difficulty": 3}
            for gw in range(1, 10)
        ]
        rows = []
        for gw in range(1, 9):
            rows.append(_row(10, "Midfielder", 3, "Alpha FC", gw, 90, 1, 5, now_cost=60,
                              expected_goals=0.2, expected_assists=0.15))
            rows.append(_row(11, "Keeper", 1, "Beta FC", gw, 90, 1, 3, now_cost=45,
                              expected_goals_conceded=1.1, saves=3))
        return {"label": label, "teams": teams, "fixtures": fixtures, "rows": rows}

    def test_produces_comparisons_within_requested_horizons_and_origins(self):
        season = self._season()
        comparisons = season_comparisons(season, horizons=(1, 3), first_origin=3, last_origin=5)
        self.assertTrue(comparisons)
        for row in comparisons:
            self.assertIn(row["horizon"], (1, 3))
            self.assertGreaterEqual(row["origin_gw"], 3)
            self.assertLessEqual(row["origin_gw"], 5)
            self.assertIn("error", row)
            self.assertIn("inside_range", row)
            self.assertIn(row["position"], ("GKP", "MID"))

    def test_historical_fixture_difficulty_is_neutralized_when_point_in_time_fdr_is_unavailable(self):
        low_fdr = self._season()
        high_fdr = copy.deepcopy(low_fdr)
        for fixture in low_fdr["fixtures"]:
            fixture["team_h_difficulty"] = fixture["team_a_difficulty"] = 1
        for fixture in high_fdr["fixtures"]:
            fixture["team_h_difficulty"] = fixture["team_a_difficulty"] = 5

        low = season_comparisons(low_fdr, horizons=(1,), first_origin=3, last_origin=4)
        high = season_comparisons(high_fdr, horizons=(1,), first_origin=3, last_origin=4)

        self.assertEqual(low, high)

    def test_truncates_horizons_that_would_run_past_last_origin(self):
        season = self._season()
        comparisons = season_comparisons(season, horizons=(1, 5), first_origin=8, last_origin=8)
        # horizon 5 from origin 8 would need gw12, which does not exist within last_origin=8
        self.assertTrue(all(row["horizon"] == 1 for row in comparisons))


class BuildBacktestReportTests(unittest.TestCase):
    def test_aggregates_across_seasons_with_expected_breakdowns(self):
        season_a = SeasonComparisonsTests()._season(label="season-a")
        season_b = SeasonComparisonsTests()._season(label="season-b")
        report = build_backtest_report(
            [season_a, season_b], horizons=(1, 3), first_origin=3, last_origin=6, model_version="0.3-test"
        )

        self.assertEqual(report["model_version"], "0.3-test")
        self.assertEqual(set(report["seasons"]), {"season-a", "season-b"})
        self.assertEqual(report["completed_comparisons"], len(report["comparisons"]))
        self.assertGreater(report["completed_comparisons"], 0)
        self.assertEqual(report["summary"]["count"], report["completed_comparisons"])
        self.assertIn("1", report["by_horizon"])
        self.assertIn("3", report["by_horizon"])
        self.assertIn("MID", report["by_position"])
        self.assertIn("GKP", report["by_position"])
        self.assertIn("season-a", report["by_season"])
        self.assertIn("season-b", report["by_season"])
        self.assertIn("top_pool_summary", report)
        self.assertTrue(
            any("defensive-contribution scoring" in item.lower() for item in report["limitations"])
        )


if __name__ == "__main__":
    unittest.main()
