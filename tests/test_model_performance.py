import unittest

from fpl_intel.modeling.model_performance import (
    archive_forecast,
    archive_shadow_forecast,
    archive_team_forecast,
    build_performance_report,
    build_shadow_performance_report,
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


def _weekly_decisions(status="active", event=2, action="roll"):
    """A minimal `build_transfer_decisions`/`build_draft_decisions`-shaped fixture -- the actual
    shape `archive_team_forecast` (issue #102) archives, structurally different from
    `_decision()`'s `decision_center`-shaped fixture above (`archive_forecast`'s own target)."""
    if status != "active":
        return {"status": status, "event": event, "reason": "not available"}
    recommendation = {
        "action": action,
        "transfers": [],
        "transfer_count": 0 if action == "roll" else 1,
        "point_cost": 0,
        "gross_gain_5gw": 3.2,
        "net_gain_5gw": 3.2,
        "bank_after": 1.5,
        "free_transfers_next_event": 2,
        "profile_score": 44.0,
        "squad": [],
        "starting_xi": [{"id": player_id} for player_id in range(1, 12)],
        "formation": "3-4-3",
        "bench": [{"id": player_id} for player_id in range(12, 16)],
        "captain": {"id": 1},
        "vice_captain": {"id": 2},
        "projected_event_points_including_captain": 55.0,
    }
    profiles = [
        {"id": profile_id, "recommendation": dict(recommendation, profile_score=score)}
        for profile_id, score in (("conservative", 40.0), ("balanced", 44.0), ("aggressive", 48.0))
    ]
    return {
        "status": "active",
        "event": event,
        "generated_at": "2026-08-20T12:00:00-04:00",
        "profiles": profiles,
    }


class ArchiveTeamForecastTests(unittest.TestCase):
    """Issue #102: archives a team's real weekly transfer/captaincy decision, structurally
    distinct from `archive_forecast`'s generic decision_center archive above (see
    `archive_team_forecast`'s own docstring for why the two can't share one function)."""

    def test_archives_a_real_decision_once_per_checkpoint(self):
        store = {}

        archive_team_forecast(store, 364759, _weekly_decisions(), lead_hours=24)
        archive_team_forecast(store, 364759, _weekly_decisions(), lead_hours=24)

        team_forecasts = store["team_forecasts"]["364759"]
        self.assertEqual(list(team_forecasts.keys()), ["gw2:24"])
        snapshot = team_forecasts["gw2:24"]
        self.assertEqual(snapshot["origin_event"], 2)
        self.assertEqual(snapshot["lead_hours"], 24)
        self.assertEqual(len(snapshot["profiles"]), 3)
        balanced = next(row for row in snapshot["profiles"] if row["profile_id"] == "balanced")
        self.assertEqual(balanced["action"], "roll")
        self.assertEqual(balanced["captain_id"], 1)
        self.assertEqual(balanced["vice_captain_id"], 2)
        self.assertEqual(len(balanced["lineup_player_ids"]), 11)
        self.assertEqual(len(balanced["bench_player_ids"]), 4)
        self.assertEqual(balanced["formation"], "3-4-3")

    def test_distinct_checkpoints_are_stored_independently(self):
        store = {}

        archive_team_forecast(store, 364759, _weekly_decisions(), lead_hours=24)
        archive_team_forecast(store, 364759, _weekly_decisions(), lead_hours=12)
        archive_team_forecast(store, 364759, _weekly_decisions(), lead_hours=3)

        self.assertEqual(
            sorted(store["team_forecasts"]["364759"].keys()), ["gw2:12", "gw2:24", "gw2:3"],
        )

    def test_a_later_checkpoint_does_not_overwrite_an_earlier_one_for_the_same_gameweek(self):
        store = {}
        archive_team_forecast(store, 364759, _weekly_decisions(action="roll"), lead_hours=24)

        archive_team_forecast(store, 364759, _weekly_decisions(action="single_transfer"), lead_hours=24)

        snapshot = store["team_forecasts"]["364759"]["gw2:24"]
        balanced = next(row for row in snapshot["profiles"] if row["profile_id"] == "balanced")
        self.assertEqual(balanced["action"], "roll")

    def test_different_teams_are_kept_fully_independent(self):
        store = {}

        archive_team_forecast(store, 1, _weekly_decisions(), lead_hours=24)
        archive_team_forecast(store, 2, _weekly_decisions(), lead_hours=24)

        self.assertEqual(set(store["team_forecasts"].keys()), {"1", "2"})

    def test_non_active_status_is_never_archived(self):
        for status in ("waiting_for_gw2", "manager_not_configured", "manager_squad_unavailable", "scenario_unavailable"):
            with self.subTest(status=status):
                store = {}

                archive_team_forecast(store, 364759, _weekly_decisions(status=status), lead_hours=24)

                self.assertNotIn("team_forecasts", store)

    def test_missing_event_is_never_archived(self):
        store = {}
        decision = _weekly_decisions()
        del decision["event"]

        archive_team_forecast(store, 364759, decision, lead_hours=24)

        self.assertNotIn("team_forecasts", store)

    def test_empty_profiles_list_is_never_archived(self):
        store = {}
        decision = _weekly_decisions()
        decision["profiles"] = []

        archive_team_forecast(store, 364759, decision, lead_hours=24)

        self.assertNotIn("team_forecasts", store)

    def test_deadline_time_after_generated_at_is_archived(self):
        """Issue #286: the server-side pre-deadline backstop. `_weekly_decisions`'s
        generated_at is 2026-08-20T16:00Z -- a later deadline means a genuine pre-deadline
        forecast, archived normally."""
        store = {}

        archive_team_forecast(
            store, 364759, _weekly_decisions(), lead_hours=24, deadline_time="2026-08-21T17:30:00Z",
        )

        self.assertIn("gw2:24", store["team_forecasts"]["364759"])

    def test_deadline_time_at_or_before_generated_at_blocks_the_archive(self):
        """Issue #286: a decision generated after its deadline is hindsight-contaminated -- the
        exact thing issue #102 exists to prevent -- so it is refused even though its status is
        `active`."""
        for deadline in ("2026-08-20T16:00:00Z", "2026-08-20T10:00:00Z"):
            with self.subTest(deadline=deadline):
                store = {}

                archive_team_forecast(
                    store, 364759, _weekly_decisions(), lead_hours=24, deadline_time=deadline,
                )

                self.assertNotIn("team_forecasts", store)

    def test_unparseable_or_naive_deadline_time_fails_closed(self):
        """Issue #286: if the endpoint supplies a deadline it can't prove the forecast predates
        (garbage, or a timezone-naive string), refuse rather than archive a possibly-hindsight
        snapshot."""
        for deadline in ("not-a-timestamp", "2026-08-21T17:30:00"):
            with self.subTest(deadline=deadline):
                store = {}

                archive_team_forecast(
                    store, 364759, _weekly_decisions(), lead_hours=24, deadline_time=deadline,
                )

                self.assertNotIn("team_forecasts", store)


class ShadowForecastTests(unittest.TestCase):
    """Issue #65: non-champion model_versions are tracked and scored additively, without
    disturbing the champion's own player_forecasts/build_performance_report numbers."""

    _shadow_forecasts = [
        {"id": 1, "modeled": 4.0, "lower": 1.0, "upper": 7.0},
        {"id": 2, "modeled": 3.0, "lower": 0.0, "upper": 6.0},
        {"id": 3, "modeled": 0.0, "lower": 0.0, "upper": 0.0},
    ]

    def test_archive_shadow_forecast_freezes_once_per_model_version_and_event(self):
        store = {"forecasts": [], "actual_events": {}}

        archive_shadow_forecast(store, "ml-minutes-ridge-v1", 1, "2026-08-20T12:00:00-04:00", self._shadow_forecasts)

        frozen = store["shadow_forecasts"]["ml-minutes-ridge-v1"]["1"]
        self.assertEqual(frozen["players"]["1"], [4.0, 1.0, 7.0])
        self.assertEqual(frozen["players"]["2"], [3.0, 0.0, 6.0])

        # Re-archiving the same (model_version, event) must not overwrite (first-write-wins,
        # matching archive_forecast's own immutability discipline).
        archive_shadow_forecast(
            store, "ml-minutes-ridge-v1", 1, "later", [{"id": 1, "modeled": 99.0, "lower": 99.0, "upper": 99.0}]
        )
        self.assertEqual(store["shadow_forecasts"]["ml-minutes-ridge-v1"]["1"]["players"]["1"], [4.0, 1.0, 7.0])

    def test_archive_shadow_forecast_keeps_distinct_model_versions_separate(self):
        store = {"forecasts": [], "actual_events": {}}

        archive_shadow_forecast(store, "ml-minutes-ridge-v1", 1, "t1", self._shadow_forecasts)
        archive_shadow_forecast(
            store, "ml-residual-v1", 1, "t2", [{"id": 1, "modeled": 8.0, "lower": 3.0, "upper": 12.0}]
        )

        self.assertEqual(set(store["shadow_forecasts"].keys()), {"ml-minutes-ridge-v1", "ml-residual-v1"})
        self.assertEqual(store["shadow_forecasts"]["ml-residual-v1"]["1"]["players"]["1"], [8.0, 3.0, 12.0])

    def test_archive_shadow_forecast_never_touches_champion_player_forecasts(self):
        store = {"forecasts": [], "actual_events": {}}
        archive_shadow_forecast(store, "ml-minutes-ridge-v1", 1, "t1", self._shadow_forecasts)
        self.assertNotIn("player_forecasts", store)

    def test_archive_shadow_forecast_ignores_empty_inputs(self):
        store = {"forecasts": [], "actual_events": {}}
        archive_shadow_forecast(store, None, 1, "t1", self._shadow_forecasts)
        archive_shadow_forecast(store, "ml-minutes-ridge-v1", None, "t1", self._shadow_forecasts)
        archive_shadow_forecast(store, "ml-minutes-ridge-v1", 1, "t1", [])
        self.assertNotIn("shadow_forecasts", store)

    def test_build_shadow_performance_report_scores_each_model_version_independently(self):
        store = {"forecasts": [], "actual_events": {"1": {"1": 6, "2": 0, "3": 0}}}
        archive_shadow_forecast(store, "ml-minutes-ridge-v1", 1, "t1", self._shadow_forecasts)
        archive_shadow_forecast(
            store, "ml-residual-v1", 1, "t2",
            [{"id": 1, "modeled": 10.0, "lower": 5.0, "upper": 15.0}],
        )

        report = build_shadow_performance_report(store)

        self.assertEqual(set(report.keys()), {"ml-minutes-ridge-v1", "ml-residual-v1"})
        minutes_report = report["ml-minutes-ridge-v1"]
        self.assertEqual(minutes_report["status"], "active")
        self.assertEqual(minutes_report["model_version"], "ml-minutes-ridge-v1")
        # player 1: modeled 4.0 vs actual 6 -> error +2.0; player 2 (0 actual too) excluded
        # by the same cohort rule the champion's own player_performance uses.
        self.assertEqual({row["element_id"] for row in minutes_report["comparisons"]}, {1, 2})
        row = next(row for row in minutes_report["comparisons"] if row["element_id"] == 1)
        self.assertEqual(row["error"], 2.0)

        residual_report = report["ml-residual-v1"]
        self.assertEqual(residual_report["summary"]["mae"], 4.0)  # |6 - 10|

    def test_build_shadow_performance_report_is_empty_without_any_shadow_forecasts(self):
        self.assertEqual(build_shadow_performance_report({"forecasts": [], "actual_events": {}}), {})

    def test_build_performance_report_includes_shadow_models_without_changing_champion_numbers(self):
        store = {"forecasts": [], "actual_events": {}}
        champion_decision = {
            "status": "active_preliminary",
            "event": 1,
            "generated_at": "2026-08-20T12:00:00-04:00",
            "model": {"version": "0.7", "is_champion": True},
            "profile_recommendations": [
                {
                    "id": "balanced",
                    "label": "Balanced",
                    "metrics": {"central_1gw": 44.0, "lower_1gw": 36.0, "upper_1gw": 52.0},
                    "evaluation_horizons": {
                        "1": {"lineup_player_ids": list(range(1, 12)), "captain_id": 1},
                    },
                }
            ],
        }
        archive_forecast(store, champion_decision, "2026-08-21T17:30:00Z")
        store["actual_events"]["1"] = {"1": 6, "2": 4, "3": 0}
        archive_shadow_forecast(store, "ml-minutes-ridge-v1", 1, "t1", self._shadow_forecasts)

        without_shadow = build_performance_report({**store, "shadow_forecasts": {}})
        with_shadow = build_performance_report(store)

        # Adding a shadow challenger must not move a single champion-report number.
        for key in ("summary", "by_horizon", "by_profile", "calibration", "completed_comparisons"):
            self.assertEqual(with_shadow[key], without_shadow[key])
        self.assertIn("ml-minutes-ridge-v1", with_shadow["shadow_models"])
        self.assertEqual(without_shadow["shadow_models"], {})


if __name__ == "__main__":
    unittest.main()
