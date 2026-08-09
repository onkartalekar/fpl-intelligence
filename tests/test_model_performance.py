import unittest

from fpl_intel.model_performance import (
    archive_forecast,
    build_performance_report,
    build_team_model_performance,
    migrate_manager_picks,
    normalize_live_event,
    normalize_manager_picks,
)


class ModelPerformanceTests(unittest.TestCase):
    _deadline = "2026-08-21T17:30:00Z"

    def _archive(self, store, decision):
        return archive_forecast(store, decision, self._deadline)

    def _decision(self):
        profiles = []
        for profile_id, modeled in (("conservative", 40.0), ("balanced", 44.0), ("aggressive", 48.0)):
            profiles.append(
                {
                    "id": profile_id,
                    "label": profile_id.title(),
                    "metrics": {
                        "central_1gw": modeled,
                        "lower_1gw": modeled - 8,
                        "upper_1gw": modeled + 8,
                        "central_3gw": modeled * 3,
                        "lower_3gw": modeled * 3 - 20,
                        "upper_3gw": modeled * 3 + 20,
                        "central_5gw": modeled * 5,
                        "lower_5gw": modeled * 5 - 30,
                        "upper_5gw": modeled * 5 + 30,
                    },
                    "evaluation_horizons": {
                        "1": {"lineup_player_ids": list(range(1, 12)), "captain_id": 1},
                        "3": {"lineup_player_ids": list(range(1, 12)), "captain_id": 1},
                        "5": {"lineup_player_ids": list(range(1, 12)), "captain_id": 1},
                    },
                }
            )
        return {
            "status": "active_preliminary",
            "event": 1,
            "generated_at": "2026-08-20T12:00:00-04:00",
            "model": {"version": "0.3", "is_champion": True},
            "profile_recommendations": profiles,
        }

    def _decision_with_player_forecasts(self, player_forecasts=None):
        decision = self._decision()
        decision["player_forecasts"] = player_forecasts if player_forecasts is not None else [
            {"id": 1, "modeled": 5.0, "lower": 2.0, "upper": 8.0},
            {"id": 2, "modeled": 4.0, "lower": 1.0, "upper": 7.0},
            {"id": 3, "modeled": 0.0, "lower": 0.0, "upper": 0.0},
        ]
        return decision

    def test_archives_pre_event_forecast_once(self):
        store = {"forecasts": [], "actual_events": {}}
        self._archive(store, self._decision())
        self._archive(store, self._decision())

        self.assertEqual(len(store["forecasts"]), 1)
        forecast = store["forecasts"][0]
        self.assertEqual(forecast["origin_event"], 1)
        self.assertEqual(forecast["profiles"][1]["horizons"]["3"]["modeled_points"], 132.0)
        self.assertEqual(len(forecast["profiles"][1]["horizons"]["5"]["lineup_player_ids"]), 11)

    def test_archives_distinct_model_versions_for_the_same_origin(self):
        store = {"forecasts": [], "actual_events": {}}
        first = self._decision()
        second = self._decision()
        second["model"] = {"version": "0.4"}

        self._archive(store, first)
        self._archive(store, second)

        self.assertEqual([row["model_version"] for row in store["forecasts"]], ["0.3", "0.4"])
        self.assertEqual(store["champion_forecasts"]["1"], store["forecasts"][0]["forecast_id"])

    def test_non_champion_forecast_is_archived_but_never_scored_implicitly(self):
        store = {"forecasts": [], "actual_events": {}}
        candidate = self._decision()
        candidate["model"] = {"version": "candidate", "is_champion": False}
        self._archive(store, candidate)
        live = {"elements": [{"id": player_id, "stats": {"total_points": 4}} for player_id in range(1, 12)]}
        store["actual_events"]["1"] = normalize_live_event(live)

        report = build_performance_report(store)

        self.assertEqual(len(store["forecasts"]), 1)
        self.assertNotIn("1", store.get("champion_forecasts", {}))
        self.assertEqual(report["completed_comparisons"], 0)

    def test_explicit_champion_version_becomes_production_forecast(self):
        store = {"forecasts": [], "actual_events": {}}
        self._archive(store, self._decision())
        champion = self._decision()
        champion["model"] = {"version": "0.4", "is_champion": True}

        self._archive(store, champion)

        self.assertEqual(store["champion_forecasts"]["1"], "gw1:0.4")

    def test_rejects_forecast_without_official_deadline(self):
        store = {"forecasts": [], "actual_events": {}}

        archive_forecast(store, self._decision())

        self.assertEqual(store["forecasts"], [])

    def test_rejects_forecast_generated_at_or_after_official_deadline(self):
        store = {"forecasts": [], "actual_events": {}}
        decision = self._decision()
        decision["generated_at"] = "2026-08-21T18:00:00Z"

        archive_forecast(store, decision, deadline_time="2026-08-21T17:30:00Z")

        self.assertEqual(store["forecasts"], [])

    def test_compares_modeled_with_actual_and_reports_error(self):
        store = {"forecasts": [], "actual_events": {}}
        self._archive(store, self._decision())
        live = {"elements": [{"id": player_id, "stats": {"total_points": 4}} for player_id in range(1, 12)]}
        store["actual_events"]["1"] = normalize_live_event(live)

        report = build_performance_report(store)

        self.assertEqual(report["status"], "active")
        self.assertEqual(report["completed_comparisons"], 3)
        balanced = next(row for row in report["comparisons"] if row["profile_id"] == "balanced")
        self.assertEqual(balanced["horizon"], 1)
        self.assertEqual(balanced["actual_points"], 48)
        self.assertEqual(balanced["error"], 4.0)
        self.assertEqual(report["summary"]["mae"], 4.0)
        self.assertEqual(report["calibration"]["completed_origin_events"], 1)
        self.assertIn("(1/8)", report["calibration"]["status"])

    def test_performance_uses_only_designated_champion_forecast(self):
        store = {"forecasts": [], "actual_events": {}}
        self._archive(store, self._decision())
        champion = self._decision()
        champion["model"] = {"version": "0.4", "is_champion": True}
        self._archive(store, champion)
        live = {"elements": [{"id": player_id, "stats": {"total_points": 4}} for player_id in range(1, 12)]}
        store["actual_events"]["1"] = normalize_live_event(live)

        report = build_performance_report(store)

        self.assertEqual(report["completed_comparisons"], 3)
        self.assertEqual({row["model_version"] for row in report["comparisons"]}, {"0.4"})

    def test_multiweek_actuals_follow_event_specific_lineups_and_captains(self):
        store = {"forecasts": [], "actual_events": {}}
        decision = self._decision()
        balanced = next(row for row in decision["profile_recommendations"] if row["id"] == "balanced")
        balanced["evaluation_horizons"]["3"]["event_lineups"] = [
            {"event": 1, "lineup_player_ids": list(range(1, 12)), "captain_id": 1},
            {"event": 2, "lineup_player_ids": list(range(12, 23)), "captain_id": 12},
            {"event": 3, "lineup_player_ids": list(range(23, 34)), "captain_id": 23},
        ]
        self._archive(store, decision)
        store["actual_events"] = {
            "1": {"1": 10},
            "2": {"12": 7},
            "3": {"23": 5},
        }

        report = build_performance_report(store)
        comparison = next(
            row for row in report["comparisons"]
            if row["profile_id"] == "balanced" and row["horizon"] == 3
        )

        self.assertEqual(comparison["actual_points"], 44)
        archived = store["forecasts"][0]["profiles"][1]["horizons"]["3"]
        self.assertEqual(len(archived["event_lineups"]), 3)

    def test_waits_until_every_event_in_a_multiweek_horizon_is_final(self):
        store = {"forecasts": [], "actual_events": {}}
        self._archive(store, self._decision())
        live = {"elements": [{"id": player_id, "stats": {"total_points": 1}} for player_id in range(1, 12)]}
        store["actual_events"]["1"] = normalize_live_event(live)
        store["actual_events"]["2"] = normalize_live_event(live)

        report = build_performance_report(store)

        self.assertEqual({row["horizon"] for row in report["comparisons"]}, {1})
        self.assertGreater(report["pending_comparisons"], 0)

    def test_archive_forecast_freezes_player_forecasts_once(self):
        store = {"forecasts": [], "actual_events": {}}
        decision = self._decision_with_player_forecasts()

        self._archive(store, decision)

        self.assertIn("1", store["player_forecasts"])
        frozen = store["player_forecasts"]["1"]
        self.assertEqual(frozen["players"]["1"], [5.0, 2.0, 8.0])
        self.assertEqual(frozen["players"]["2"], [4.0, 1.0, 7.0])

        # A second archive attempt for the same origin+version must not
        # rewrite the frozen player forecasts (first-write-wins, immutable).
        second = self._decision_with_player_forecasts([
            {"id": 1, "modeled": 99.0, "lower": 99.0, "upper": 99.0},
        ])
        self._archive(store, second)

        self.assertEqual(store["player_forecasts"]["1"]["players"]["1"], [5.0, 2.0, 8.0])

    def test_archive_forecast_does_not_freeze_player_forecasts_for_non_champion(self):
        store = {"forecasts": [], "actual_events": {}}
        decision = self._decision_with_player_forecasts()
        decision["model"] = {"version": "candidate", "is_champion": False}

        self._archive(store, decision)

        self.assertNotIn("player_forecasts", store)

    def test_player_performance_applies_cohort_rule_and_scores_error(self):
        store = {"forecasts": [], "actual_events": {}}
        self._archive(store, self._decision_with_player_forecasts())
        store["actual_events"]["1"] = {"1": 6, "2": 0, "3": 0}

        # player_performance is team-independent (issue #64) -- any team_id yields the same slice.
        report = build_team_model_performance(store, team_id=999999)
        player_performance = report["player_performance"]

        self.assertEqual(player_performance["status"], "active")
        self.assertEqual(player_performance["events"], [1])
        element_ids = {row["element_id"] for row in player_performance["comparisons"]}
        self.assertEqual(element_ids, {1, 2})  # player 3 (0 modeled, 0 actual) excluded by cohort rule

        row = next(row for row in player_performance["comparisons"] if row["element_id"] == 1)
        self.assertEqual(row["modeled_points"], 5.0)
        self.assertEqual(row["actual_points"], 6)
        self.assertEqual(row["error"], 1.0)
        self.assertTrue(row["inside_range"])
        self.assertEqual(player_performance["summary"]["mae"], 2.5)

    def test_normalize_manager_picks_maps_payload(self):
        payload = {
            "picks": [
                {"element": 1, "multiplier": 2, "is_captain": True, "is_vice_captain": False},
                {"element": 2, "multiplier": 1, "is_captain": False, "is_vice_captain": True},
                {"element": 3, "multiplier": 0, "is_captain": False, "is_vice_captain": False},
            ]
        }

        picks = normalize_manager_picks(payload)

        self.assertEqual(
            picks,
            [
                {"element_id": 1, "multiplier": 2, "is_captain": True},
                {"element_id": 2, "multiplier": 1, "is_captain": False},
                {"element_id": 3, "multiplier": 0, "is_captain": False},
            ],
        )

    def test_team_performance_scores_multiplier_weighted_picks_with_captain(self):
        store = {"forecasts": [], "actual_events": {}}
        self._archive(store, self._decision_with_player_forecasts())
        store["actual_events"]["1"] = {"1": 6, "2": 3}
        store["manager_picks"] = {
            "364759": {
                "1": [
                    {"element_id": 1, "multiplier": 2, "is_captain": True},
                    {"element_id": 2, "multiplier": 1, "is_captain": False},
                ]
            }
        }

        report = build_team_model_performance(store, team_id=364759)
        team_performance = report["team_performance"]

        self.assertEqual(team_performance["status"], "active")
        comparison = team_performance["comparisons"][0]
        # captain (multiplier 2) doubles player 1's modeled/actual contribution.
        self.assertEqual(comparison["modeled_points"], 2 * 5.0 + 1 * 4.0)
        self.assertEqual(comparison["actual_points"], 2 * 6 + 1 * 3)
        self.assertEqual(comparison["error"], comparison["actual_points"] - comparison["modeled_points"])

    def test_team_performance_reads_only_the_requested_teams_slice(self):
        """manager_picks is keyed per team ID (issue #64) -- another team's picks must not leak in."""
        store = {"forecasts": [], "actual_events": {}}
        self._archive(store, self._decision_with_player_forecasts())
        store["actual_events"]["1"] = {"1": 6, "2": 3}
        store["manager_picks"] = {
            "111": {"1": [{"element_id": 1, "multiplier": 1, "is_captain": False}]},
            "222": {"1": [{"element_id": 2, "multiplier": 1, "is_captain": False}]},
        }

        report_111 = build_team_model_performance(store, team_id=111)
        report_222 = build_team_model_performance(store, team_id=222)
        report_unknown = build_team_model_performance(store, team_id=999)

        self.assertEqual(report_111["team_performance"]["comparisons"][0]["modeled_points"], 5.0)
        self.assertEqual(report_222["team_performance"]["comparisons"][0]["modeled_points"], 4.0)
        self.assertEqual(report_unknown["team_performance"]["status"], "waiting_for_results")
        self.assertEqual(report_unknown["team_performance"]["comparisons"], [])

    def test_team_performance_emits_no_comparison_without_frozen_forecast(self):
        store = {"forecasts": [], "actual_events": {}}
        store["actual_events"]["1"] = {"1": 6}
        store["manager_picks"] = {"364759": {"1": [{"element_id": 1, "multiplier": 1, "is_captain": False}]}}

        report = build_team_model_performance(store, team_id=364759)

        self.assertEqual(report["team_performance"]["status"], "waiting_for_results")
        self.assertEqual(report["team_performance"]["comparisons"], [])

    def test_old_store_shape_yields_empty_player_and_team_performance(self):
        store = {"forecasts": [], "actual_events": {}}

        report = build_team_model_performance(store, team_id=364759)

        self.assertEqual(report["player_performance"], {
            "status": "waiting_for_results", "events": [], "comparisons": [],
            "summary": {"count": 0, "mae": None, "bias": None, "rmse": None, "range_coverage": None},
        })
        self.assertEqual(report["team_performance"]["status"], "waiting_for_results")
        self.assertEqual(report["team_performance"]["comparisons"], [])

    def test_build_performance_report_no_longer_bakes_in_per_team_fields(self):
        """Issue #64: team_performance/player_performance move to request time entirely."""
        store = {"forecasts": [], "actual_events": {}}
        self._archive(store, self._decision_with_player_forecasts())
        store["actual_events"]["1"] = {"1": 6, "2": 3}
        store["manager_picks"] = {"364759": [{"element_id": 1, "multiplier": 1, "is_captain": False}]}

        report = build_performance_report(store)

        self.assertNotIn("team_performance", report)
        self.assertNotIn("player_performance", report)

    def test_migrate_manager_picks_reshapes_pre_issue_64_flat_store(self):
        store = {
            "forecasts": [], "actual_events": {},
            "manager_picks": {"1": [{"element_id": 1, "multiplier": 1, "is_captain": False}]},
        }

        migrate_manager_picks(store, team_id=364759)

        self.assertEqual(
            store["manager_picks"],
            {"364759": {"1": [{"element_id": 1, "multiplier": 1, "is_captain": False}]}},
        )

    def test_migrate_manager_picks_is_idempotent_on_already_nested_store(self):
        already_nested = {"364759": {"1": [{"element_id": 1, "multiplier": 1, "is_captain": False}]}}
        store = {"forecasts": [], "actual_events": {}, "manager_picks": dict(already_nested)}

        migrate_manager_picks(store, team_id=364759)

        self.assertEqual(store["manager_picks"], already_nested)

    def test_migrate_manager_picks_leaves_flat_store_untouched_without_a_team_id(self):
        flat = {"1": [{"element_id": 1, "multiplier": 1, "is_captain": False}]}
        store = {"forecasts": [], "actual_events": {}, "manager_picks": dict(flat)}

        migrate_manager_picks(store, team_id=None)

        self.assertEqual(store["manager_picks"], flat)

    def test_migrate_manager_picks_no_ops_on_empty_store(self):
        store = {"forecasts": [], "actual_events": {}}

        migrate_manager_picks(store, team_id=364759)

        self.assertNotIn("manager_picks", store)


if __name__ == "__main__":
    unittest.main()
