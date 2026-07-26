import unittest

from fpl_intel.model_performance import (
    archive_forecast,
    build_performance_report,
    normalize_live_event,
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


if __name__ == "__main__":
    unittest.main()
