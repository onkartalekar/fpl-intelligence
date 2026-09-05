import unittest

from fpl_intel.modeling.model_performance import (
    archive_forecast,
    archive_shadow_forecast,
    archive_team_forecast,
    build_performance_report,
    build_shadow_performance_report,
    build_team_model_performance,
    build_team_plan_diff,
    build_team_transfer_adherence,
    migrate_manager_picks,
    normalize_live_event,
    normalize_manager_picks,
    normalize_manager_transfers,
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


def _weekly_decisions(
    status="active", event=2, action="roll", transfers=None,
    chip_recommendation=None, conditional_branches=None, required_margin=None, margin_above_required=None,
):
    """A minimal `build_transfer_decisions`/`build_draft_decisions`-shaped fixture -- the actual
    shape `archive_team_forecast` (issue #102) archives, structurally different from
    `_decision()`'s `decision_center`-shaped fixture above (`archive_forecast`'s own target).

    `transfers` (issue #285): `_move_record`-shaped `{"out": {"id", ...}, "in": {"id", ...}}`
    entries, matching what `transfer_decisions.py`'s own scenarios actually attach.

    `chip_recommendation`/`conditional_branches`/`required_margin`/`margin_above_required`
    (issue #266): applied identically to every profile, same simplification this fixture already
    makes for `recommendation` itself -- real per-profile divergence isn't needed to exercise the
    archiving/diffing logic these fixtures feed.
    """
    if status != "active":
        return {"status": status, "event": event, "reason": "not available"}
    recommendation = {
        "action": action,
        "transfers": transfers or [],
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
    if required_margin is not None:
        recommendation["required_margin"] = required_margin
    if margin_above_required is not None:
        recommendation["margin_above_required"] = margin_above_required
    profiles = [
        {
            "id": profile_id,
            "recommendation": dict(recommendation, profile_score=score),
            "chip_recommendation": chip_recommendation or {"action": "hold", "chip": None},
            "multiweek_plan": {"conditional_branches": conditional_branches or []},
        }
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

    def test_freezes_the_recommended_transfer_in_out_pairs(self):
        """Issue #285: `recommendation["transfers"]` -> `{out_id, in_id}` pairs, needed for a
        future player-for-player comparison against the manager's actual transfers."""
        store = {}
        move = {
            "out": {"id": 7, "name": "Sold Player", "club": "AAA", "selling_price": 55},
            "in": {"id": 8, "name": "Bought Player", "club": "BBB", "price": 60},
        }

        archive_team_forecast(
            store, 364759,
            _weekly_decisions(action="single_transfer", transfers=[move]),
            lead_hours=24,
        )

        balanced = next(
            row for row in store["team_forecasts"]["364759"]["gw2:24"]["profiles"]
            if row["profile_id"] == "balanced"
        )
        self.assertEqual(balanced["transfers"], [{"out_id": 7, "in_id": 8}])

    def test_no_transfers_freezes_an_empty_list_not_a_missing_key(self):
        store = {}

        archive_team_forecast(store, 364759, _weekly_decisions(action="roll"), lead_hours=24)

        balanced = next(
            row for row in store["team_forecasts"]["364759"]["gw2:24"]["profiles"]
            if row["profile_id"] == "balanced"
        )
        self.assertEqual(balanced["transfers"], [])

    def test_freezes_chip_recommendation_scalars(self):
        """Issue #266: `chip_recommendation`'s threshold/effective_threshold/value_above_threshold
        -- already on the live payload since #267 -- are frozen as-is, no re-derivation."""
        store = {}
        chip = {
            "action": "play", "chip": "wildcard", "marginal_value": 12.5,
            "threshold": 8.0, "effective_threshold": 10.2, "value_above_threshold": 2.3,
            "alternatives": [{"chip": "freehit"}],  # must NOT be frozen (finding: scalars only)
        }

        archive_team_forecast(store, 364759, _weekly_decisions(chip_recommendation=chip), lead_hours=24)

        balanced = next(
            row for row in store["team_forecasts"]["364759"]["gw2:24"]["profiles"]
            if row["profile_id"] == "balanced"
        )
        self.assertEqual(
            balanced["chip_recommendation"],
            {
                "action": "play", "chip": "wildcard", "marginal_value": 12.5,
                "threshold": 8.0, "effective_threshold": 10.2, "value_above_threshold": 2.3,
            },
        )
        self.assertNotIn("alternatives", balanced["chip_recommendation"])

    def test_no_chip_recommendation_freezes_a_scalar_dict_of_nones_not_a_missing_key(self):
        store = {}

        archive_team_forecast(store, 364759, _weekly_decisions(), lead_hours=24)

        balanced = next(
            row for row in store["team_forecasts"]["364759"]["gw2:24"]["profiles"]
            if row["profile_id"] == "balanced"
        )
        self.assertEqual(balanced["chip_recommendation"]["action"], "hold")
        self.assertIsNone(balanced["chip_recommendation"]["chip"])

    def test_freezes_conditional_branches_trimmed_to_event_action_chip_signal(self):
        """Issue #266: `condition`/`point_cost`/free-transfer counts are deliberately dropped --
        re-derivable narrative text, not needed for a week-over-week diff."""
        store = {}
        branches = [
            {
                "event": 3, "action": "single_transfer", "chip_signal": "GW3 looks double-shaped",
                "condition": "some narrative text", "point_cost": 4,
                "free_transfers_before": 1, "free_transfers_next_event": 1,
            },
            {"event": 4, "action": "roll", "chip_signal": None},
        ]

        archive_team_forecast(
            store, 364759, _weekly_decisions(conditional_branches=branches), lead_hours=24,
        )

        balanced = next(
            row for row in store["team_forecasts"]["364759"]["gw2:24"]["profiles"]
            if row["profile_id"] == "balanced"
        )
        self.assertEqual(
            balanced["conditional_branches"],
            [
                {"event": 3, "action": "single_transfer", "chip_signal": "GW3 looks double-shaped"},
                {"event": 4, "action": "roll", "chip_signal": None},
            ],
        )

    def test_freezes_required_margin_and_margin_above_required_when_present(self):
        store = {}

        archive_team_forecast(
            store, 364759,
            _weekly_decisions(action="multi_transfer", required_margin=1.2, margin_above_required=3.4),
            lead_hours=24,
        )

        balanced = next(
            row for row in store["team_forecasts"]["364759"]["gw2:24"]["profiles"]
            if row["profile_id"] == "balanced"
        )
        self.assertEqual(balanced["required_margin"], 1.2)
        self.assertEqual(balanced["margin_above_required"], 3.4)

    def test_required_margin_is_none_when_absent_from_the_recommendation(self):
        store = {}

        archive_team_forecast(store, 364759, _weekly_decisions(action="roll"), lead_hours=24)

        balanced = next(
            row for row in store["team_forecasts"]["364759"]["gw2:24"]["profiles"]
            if row["profile_id"] == "balanced"
        )
        self.assertIsNone(balanced["required_margin"])
        self.assertIsNone(balanced["margin_above_required"])

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


class NormalizeManagerTransfersTests(unittest.TestCase):
    """Issue #285: buckets a manager's whole `/transfers/` history by gameweek in one pass."""

    def test_buckets_by_event(self):
        payload = [
            {"element_in": 10, "element_out": 20, "event": 2, "element_in_cost": 55, "element_out_cost": 50, "time": "t1"},
            {"element_in": 11, "element_out": 21, "event": 2, "element_in_cost": 60, "element_out_cost": 45, "time": "t2"},
            {"element_in": 30, "element_out": 40, "event": 3, "element_in_cost": 50, "element_out_cost": 50, "time": "t3"},
        ]

        by_event = normalize_manager_transfers(payload)

        self.assertEqual(
            by_event,
            {
                "2": [{"in_id": 10, "out_id": 20}, {"in_id": 11, "out_id": 21}],
                "3": [{"in_id": 30, "out_id": 40}],
            },
        )

    def test_ignores_rows_missing_event_or_player_ids(self):
        payload = [
            {"event": 2, "element_in": None, "element_out": 5},
            {"event": None, "element_in": 1, "element_out": 2},
        ]

        self.assertEqual(normalize_manager_transfers(payload), {})

    def test_empty_or_missing_payload_is_empty(self):
        self.assertEqual(normalize_manager_transfers([]), {})
        self.assertEqual(normalize_manager_transfers(None), {})


class TransferAdherenceTests(unittest.TestCase):
    """Issue #285: "recommended vs performed" transfer-adherence rows."""

    def _base_store(self, event=2, action="roll", lead_hours=24):
        store = {}
        archive_team_forecast(store, 364759, _weekly_decisions(event=event, action=action), lead_hours=lead_hours)
        store["actual_events"] = {str(event): {str(i): 4 for i in range(1, 16)}}
        store["manager_picks"] = {
            str(364759): {
                str(event): [
                    {"element_id": player_id, "multiplier": 2 if player_id == 1 else 1, "is_captain": player_id == 1}
                    for player_id in range(1, 12)
                ]
            }
        }
        return store

    def test_no_row_without_a_finished_gameweek(self):
        store = self._base_store()
        del store["actual_events"]["2"]
        store["manager_transfers"] = {"364759": {"2": []}}

        report = build_team_transfer_adherence(store, 364759)

        self.assertEqual(report["status"], "waiting_for_results")
        self.assertEqual(report["rows"], [])

    def test_no_row_without_backfilled_actual_transfers(self):
        """A missing `manager_transfers[event]` key means "not backfilled yet," not "zero
        transfers" -- must not fabricate a row from it."""
        store = self._base_store()
        # store["manager_transfers"] deliberately left absent entirely.

        report = build_team_transfer_adherence(store, 364759)

        self.assertEqual(report["rows"], [])

    def test_no_row_without_backfilled_actual_picks(self):
        store = self._base_store()
        store["manager_transfers"] = {"364759": {"2": []}}
        del store["manager_picks"]["364759"]["2"]

        report = build_team_transfer_adherence(store, 364759)

        self.assertEqual(report["rows"], [])

    def test_followed_yes_when_transfer_counts_match(self):
        store = self._base_store(action="roll")  # transfer_count 0
        store["manager_transfers"] = {"364759": {"2": []}}  # manager also rolled

        report = build_team_transfer_adherence(store, 364759)

        self.assertEqual(report["status"], "active")
        balanced = next(row for row in report["rows"] if row["profile_id"] == "balanced")
        self.assertEqual(balanced["recommended_transfer_count"], 0)
        self.assertEqual(balanced["actual_transfer_count"], 0)
        self.assertEqual(balanced["followed"], "yes")

    def test_followed_no_when_counts_differ(self):
        store = self._base_store(action="roll")
        store["manager_transfers"] = {"364759": {"2": [{"in_id": 99, "out_id": 1}]}}

        report = build_team_transfer_adherence(store, 364759)

        balanced = next(row for row in report["rows"] if row["profile_id"] == "balanced")
        self.assertEqual(balanced["actual_transfer_count"], 1)
        self.assertEqual(balanced["followed"], "no")

    def test_not_among_modeled_scenarios_when_actual_exceeds_the_models_own_menu(self):
        """The model always evaluates roll/1/2/3+ transfers (transfer_decisions.py's own
        scenarios) -- 4+ actual transfers had no modeled alternative to have followed."""
        store = self._base_store(action="roll")
        store["manager_transfers"] = {
            "364759": {"2": [{"in_id": player_id, "out_id": player_id + 100} for player_id in range(4)]}
        }

        report = build_team_transfer_adherence(store, 364759)

        balanced = next(row for row in report["rows"] if row["profile_id"] == "balanced")
        self.assertEqual(balanced["actual_transfer_count"], 4)
        self.assertEqual(balanced["followed"], "not among modeled scenarios")

    def test_recommended_and_actual_path_points_and_delta(self):
        store = self._base_store(action="roll")
        store["actual_events"]["2"] = {
            "1": 10, "2": 2, "3": 1, "4": 1, "5": 1, "6": 1, "7": 1, "8": 1, "9": 1, "10": 1, "11": 1,
        }
        store["manager_picks"]["364759"]["2"] = [
            {"element_id": player_id, "multiplier": 2 if player_id == 2 else 1, "is_captain": player_id == 2}
            for player_id in range(1, 12)
        ]
        store["manager_transfers"] = {"364759": {"2": []}}

        report = build_team_transfer_adherence(store, 364759)

        balanced = next(row for row in report["rows"] if row["profile_id"] == "balanced")
        # Recommended: lineup 1..11 (10 + 2 + nine 1s = 21) plus the recommended captain (id 1,
        # worth 10) counted again -- 21 + 10 = 31.
        self.assertEqual(balanced["recommended_path_points"], 31)
        # Actual: same 11 picks, but the manager's own captain is player 2 (worth 2, doubled) --
        # 10 + 2*2 + nine 1s = 10 + 4 + 9 = 23.
        self.assertEqual(balanced["actual_path_points"], 23)
        self.assertEqual(balanced["delta"], 23 - 31)

    def test_summary_excludes_not_among_modeled_scenarios_from_adherence_rate_but_not_from_mean_delta(self):
        store = self._base_store(event=2, action="roll", lead_hours=24)
        archive_team_forecast(store, 364759, _weekly_decisions(event=3, action="roll"), lead_hours=24)
        store["actual_events"]["3"] = {str(i): 4 for i in range(1, 16)}
        store["manager_picks"]["364759"]["3"] = [
            {"element_id": player_id, "multiplier": 2 if player_id == 1 else 1, "is_captain": player_id == 1}
            for player_id in range(1, 12)
        ]
        store["manager_transfers"] = {
            "364759": {
                "2": [],  # matches "roll" -> followed
                "3": [{"in_id": player_id, "out_id": player_id + 100} for player_id in range(4)],  # not modeled
            }
        }

        report = build_team_transfer_adherence(store, 364759)

        self.assertEqual(report["summary"]["count"], 6)  # 3 profiles x 2 gameweeks
        # Only the 3 GW2 rows ("yes") count toward the rate; the 3 GW3 "not among modeled
        # scenarios" rows are excluded rather than scored as failures.
        self.assertEqual(report["summary"]["adherence_rate"], 1.0)
        # Every row here has identical recommended/actual picks -> delta 0 for all 6 rows,
        # including the excluded ones -- mean_delta is NOT adherence-filtered.
        self.assertEqual(report["summary"]["mean_delta"], 0.0)

    def test_summary_is_none_when_no_rows_are_scored_at_all(self):
        store = self._base_store()
        # No manager_transfers backfilled -- zero rows produced.

        report = build_team_transfer_adherence(store, 364759)

        self.assertEqual(report["summary"], {"count": 0, "adherence_rate": None, "mean_delta": None})

    def test_gap_in_archived_checkpoints_yields_no_row_for_that_gameweek(self):
        """A gameweek whose checkpoint archiver never fired (issue #288's cron-reliability gaps)
        must show as absent, never a hindsight-filled row."""
        store = {"actual_events": {"5": {"1": 4}}, "manager_picks": {"364759": {"5": []}}}
        store["manager_transfers"] = {"364759": {"5": []}}
        # No team_forecasts entry for GW5 at all.

        report = build_team_transfer_adherence(store, 364759)

        self.assertEqual(report["rows"], [])
        self.assertEqual(report["status"], "waiting_for_results")

    def test_wired_into_build_team_model_performance(self):
        store = self._base_store(action="roll")
        store["manager_transfers"] = {"364759": {"2": []}}

        report = build_team_model_performance(store, team_id=364759)

        self.assertIn("transfer_adherence", report)
        self.assertEqual(report["transfer_adherence"]["status"], "active")


def _checkpoint(origin_event, lead_hours, profiles):
    return {"origin_event": origin_event, "lead_hours": lead_hours, "profiles": profiles}


def _archived_profile(profile_id, conditional_branches):
    return {"profile_id": profile_id, "conditional_branches": conditional_branches}


def _live_weekly_decisions(event, action="single_transfer", chip_action="hold"):
    return {
        "status": "active",
        "event": event,
        "profiles": [
            {
                "id": profile_id,
                "recommendation": {"action": action},
                "chip_recommendation": {"action": chip_action, "chip": None},
            }
            for profile_id in ("conservative", "balanced", "aggressive")
        ],
    }


class PlanDiffTests(unittest.TestCase):
    """Issue #266: week-over-week "already flagged last week" vs. "new since last week"."""

    def test_no_comparison_when_weekly_decisions_is_not_active(self):
        for status in ("waiting_for_gw2", "manager_not_configured"):
            with self.subTest(status=status):
                diff = build_team_plan_diff({}, 364759, {"status": status, "event": 3})
                self.assertEqual(diff, {"event": None, "profiles": []})

    def test_no_entry_when_no_prior_checkpoint_exists_at_all(self):
        diff = build_team_plan_diff({}, 364759, _live_weekly_decisions(event=3))

        self.assertEqual(diff, {"event": 3, "profiles": []})

    def test_no_entry_when_a_prior_checkpoint_exists_but_names_no_branch_for_this_event(self):
        store = {
            "team_forecasts": {
                "364759": {
                    "gw2:24": _checkpoint(2, 24, [
                        _archived_profile("balanced", [{"event": 5, "action": "roll", "chip_signal": None}]),
                    ]),
                },
            },
        }

        diff = build_team_plan_diff(store, 364759, _live_weekly_decisions(event=3))

        self.assertEqual(diff["profiles"], [])

    def test_action_changed_true_when_the_branch_action_differs_from_the_live_recommendation(self):
        store = {
            "team_forecasts": {
                "364759": {
                    "gw2:24": _checkpoint(2, 24, [
                        _archived_profile("balanced", [{"event": 3, "action": "roll", "chip_signal": None}]),
                    ]),
                },
            },
        }

        diff = build_team_plan_diff(store, 364759, _live_weekly_decisions(event=3, action="single_transfer"))

        entry = next(row for row in diff["profiles"] if row["profile_id"] == "balanced")
        self.assertEqual(entry["prior_action"], "roll")
        self.assertEqual(entry["current_action"], "single_transfer")
        self.assertTrue(entry["action_changed"])

    def test_action_changed_false_when_the_branch_action_matches(self):
        store = {
            "team_forecasts": {
                "364759": {
                    "gw2:24": _checkpoint(2, 24, [
                        _archived_profile("balanced", [{"event": 3, "action": "single_transfer", "chip_signal": None}]),
                    ]),
                },
            },
        }

        diff = build_team_plan_diff(store, 364759, _live_weekly_decisions(event=3, action="single_transfer"))

        entry = next(row for row in diff["profiles"] if row["profile_id"] == "balanced")
        self.assertFalse(entry["action_changed"])

    def test_chip_signal_confirmed_when_flagged_last_week_and_a_chip_is_recommended_now(self):
        store = {
            "team_forecasts": {
                "364759": {
                    "gw2:24": _checkpoint(2, 24, [
                        _archived_profile("balanced", [
                            {"event": 3, "action": "roll", "chip_signal": "GW3 looks double-shaped"},
                        ]),
                    ]),
                },
            },
        }

        diff = build_team_plan_diff(store, 364759, _live_weekly_decisions(event=3, chip_action="play"))

        entry = next(row for row in diff["profiles"] if row["profile_id"] == "balanced")
        self.assertTrue(entry["chip_signal_was_flagged"])
        self.assertTrue(entry["chip_now_recommended"])

    def test_chip_now_recommended_without_a_prior_signal_is_distinguishable_as_new(self):
        store = {
            "team_forecasts": {
                "364759": {
                    "gw2:24": _checkpoint(2, 24, [
                        _archived_profile("balanced", [{"event": 3, "action": "roll", "chip_signal": None}]),
                    ]),
                },
            },
        }

        diff = build_team_plan_diff(store, 364759, _live_weekly_decisions(event=3, chip_action="play"))

        entry = next(row for row in diff["profiles"] if row["profile_id"] == "balanced")
        self.assertFalse(entry["chip_signal_was_flagged"])
        self.assertTrue(entry["chip_now_recommended"])

    def test_uses_the_most_recent_prior_checkpoint_when_several_exist(self):
        """A GW1 checkpoint might also happen to name GW3 in its own (much longer-range, now
        stale) branch list -- the nearer GW2 checkpoint's own branch must win, not GW1's."""
        store = {
            "team_forecasts": {
                "364759": {
                    "gw1:24": _checkpoint(1, 24, [
                        _archived_profile("balanced", [{"event": 3, "action": "roll", "chip_signal": None}]),
                    ]),
                    "gw2:24": _checkpoint(2, 24, [
                        _archived_profile("balanced", [{"event": 3, "action": "double_transfer", "chip_signal": None}]),
                    ]),
                },
            },
        }

        diff = build_team_plan_diff(store, 364759, _live_weekly_decisions(event=3, action="double_transfer"))

        entry = next(row for row in diff["profiles"] if row["profile_id"] == "balanced")
        self.assertEqual(entry["prior_action"], "double_transfer")
        self.assertFalse(entry["action_changed"])

    def test_profiles_are_matched_independently(self):
        """Only 'balanced' has a matching prior branch -- the other two profiles get no entry,
        not a fabricated one borrowed from a different profile's plan."""
        store = {
            "team_forecasts": {
                "364759": {
                    "gw2:24": _checkpoint(2, 24, [
                        _archived_profile("balanced", [{"event": 3, "action": "roll", "chip_signal": None}]),
                    ]),
                },
            },
        }

        diff = build_team_plan_diff(store, 364759, _live_weekly_decisions(event=3))

        self.assertEqual([row["profile_id"] for row in diff["profiles"]], ["balanced"])

    def test_different_teams_are_kept_fully_independent(self):
        store = {
            "team_forecasts": {
                "1": {"gw2:24": _checkpoint(2, 24, [
                    _archived_profile("balanced", [{"event": 3, "action": "roll", "chip_signal": None}]),
                ])},
            },
        }

        diff = build_team_plan_diff(store, 2, _live_weekly_decisions(event=3))

        self.assertEqual(diff["profiles"], [])


if __name__ == "__main__":
    unittest.main()
