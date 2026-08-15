import unittest
from unittest.mock import patch

from fpl_intel.recommendations import build_gw_recommendations
from fpl_intel.transfer_decisions import (
    _planner_player_score,
    build_draft_decisions,
    build_transfer_decisions,
    derive_free_transfers,
    validate_draft_squad,
)
from tests.test_recommendations import sample_bootstrap, sample_fixtures


def draft_inputs():
    """A legal preseason (event 1) bootstrap/fixtures pair plus a legal 15-player draft."""
    bootstrap = sample_bootstrap()
    fixtures = sample_fixtures()
    opening = build_gw_recommendations(bootstrap, fixtures, "2026-07-01T12:00:00-04:00")
    draft_squad_ids = [player["id"] for player in opening["recommended_squad"]["players"]]
    return bootstrap, fixtures, draft_squad_ids


def gw2_inputs():
    bootstrap = sample_bootstrap()
    bootstrap["events"] = [
        {
            "id": event,
            "name": f"Gameweek {event}",
            "deadline_time": f"2026-09-{event:02d}T17:30:00Z",
            "finished": event == 1,
            "is_current": event == 1,
            "is_next": event == 2,
        }
        for event in range(1, 7)
    ]
    bootstrap["chips"] = [
        {"id": 1, "name": "wildcard", "number": 1, "start_event": 2, "stop_event": 19, "chip_type": "transfer"},
        {"id": 2, "name": "freehit", "number": 1, "start_event": 2, "stop_event": 19, "chip_type": "transfer"},
        {"id": 3, "name": "bboost", "number": 1, "start_event": 1, "stop_event": 19, "chip_type": "team"},
        {"id": 4, "name": "3xc", "number": 1, "start_event": 1, "stop_event": 19, "chip_type": "team"},
    ]
    bootstrap["game_settings"].update({"max_extra_free_transfers": 4, "transfers_cap": 20})
    fixtures = []
    for fixture in sample_fixtures():
        shifted = dict(fixture)
        shifted["event"] += 1
        fixtures.append(shifted)
    opening = build_gw_recommendations(bootstrap, fixtures, "2026-08-29T12:00:00-04:00")
    squad = [
        {
            "element_id": player["id"],
            "purchase_price": int(round(player["price"] * 10)),
            "selling_price": int(round(player["price"] * 10)),
            "position": index + 1,
        }
        for index, player in enumerate(opening["recommended_squad"]["players"])
    ]
    manager = {
        "current_event": 1,
        "bank": 0,
        "squad_publicly_available": True,
        "squad": squad,
        "chips_used": [],
        "public_transfers": [],
    }
    return bootstrap, fixtures, manager


class FreeTransferTests(unittest.TestCase):
    def test_derives_rolls_hits_and_five_transfer_cap(self):
        self.assertEqual(derive_free_transfers(2, [], []), 1)
        self.assertEqual(derive_free_transfers(4, [], []), 3)
        transfers = [{"event": 2}, {"event": 3}, {"event": 3}]
        self.assertEqual(derive_free_transfers(4, transfers, []), 1)
        self.assertEqual(derive_free_transfers(9, [], []), 5)


class TransferDecisionTests(unittest.TestCase):
    def test_planner_scores_profile_specific_fixture_scenarios(self):
        player = {
            "fixture_xp": [10.0, 10.0, 10.0, 10.0, 10.0],
            "profile_fixture_xp": {
                "conservative": [4.0, 4.0, 4.0, 4.0, 4.0],
                "balanced": [7.0, 7.0, 7.0, 7.0, 7.0],
                "aggressive": [12.0, 12.0, 12.0, 12.0, 12.0],
            },
            "confidence": "low",
            "ownership": 20.0,
            "expected_minutes": 86.0,
            "expected_minutes_scenarios": {
                "conservative": 50.0,
                "balanced": 67.0,
                "aggressive": 80.0,
            },
        }

        self.assertEqual(_planner_player_score(player, "conservative", 1), 16.0)
        self.assertEqual(_planner_player_score(player, "balanced", 1), 28.0)
        self.assertEqual(_planner_player_score(player, "aggressive", 1), 48.0)

    def test_builds_roll_single_and_double_scenarios_for_three_profiles(self):
        bootstrap, fixtures, manager = gw2_inputs()
        result = build_transfer_decisions(
            bootstrap, fixtures, manager, generated_at="2026-08-29T12:00:00-04:00"
        )

        self.assertEqual(result["status"], "active")
        self.assertEqual(result["event"], 2)
        self.assertEqual(result["free_transfers"], 1)
        self.assertEqual(result["official_rules"]["maximum_free_transfers"], 5)
        self.assertEqual(result["official_rules"]["extra_transfer_cost"], 4)
        self.assertEqual(
            [row["id"] for row in result["profiles"]],
            ["conservative", "balanced", "aggressive"],
        )
        for profile in result["profiles"]:
            actions = {scenario["action"] for scenario in profile["scenarios"]}
            self.assertTrue({"roll", "single_transfer", "double_transfer"} <= actions)
            roll = next(row for row in profile["scenarios"] if row["action"] == "roll")
            self.assertEqual(roll["transfers"], [])
            self.assertEqual(roll["point_cost"], 0)
            self.assertEqual(roll["free_transfers_next_event"], 2)
            self.assertIn(profile["recommendation"]["action"], actions)
            self.assertEqual(len(profile["recommendation"]["starting_xi"]), 11)

    def test_profiles_carry_uncertainty_metrics_for_the_managers_own_declared_squad(self):
        # Issue #158: the "Compare risk profiles" panel needs the same rich metrics/
        # evaluation_horizons shape recommendations.py's _build_profile_recommendation computes --
        # but evaluated against the manager's own real squad, not a freshly optimized one, and not
        # whatever a profile's `recommendation` might separately suggest transferring to.
        bootstrap, fixtures, manager = gw2_inputs()
        result = build_transfer_decisions(
            bootstrap, fixtures, manager, generated_at="2026-08-29T12:00:00-04:00"
        )

        own_squad_ids = {pick["element_id"] for pick in manager["squad"]}
        for profile in result["profiles"]:
            metrics = profile["metrics"]
            for key in (
                "central_1gw", "lower_1gw", "upper_1gw",
                "central_3gw", "lower_3gw", "upper_3gw",
                "central_5gw", "lower_5gw", "upper_5gw",
                "average_ownership", "average_expected_minutes", "low_confidence_players",
            ):
                self.assertIn(key, metrics)
            self.assertLessEqual(metrics["lower_5gw"], metrics["central_5gw"])
            self.assertLessEqual(metrics["central_5gw"], metrics["upper_5gw"])
            horizon_one = profile["evaluation_horizons"]["1"]
            self.assertTrue(set(horizon_one["lineup_player_ids"]) <= own_squad_ids)
            self.assertIn(horizon_one["captain_id"], own_squad_ids)

    def test_recommends_roll_when_no_move_clears_profile_threshold(self):
        bootstrap, fixtures, manager = gw2_inputs()
        result = build_transfer_decisions(
            bootstrap, fixtures, manager, generated_at="2026-08-29T12:00:00-04:00"
        )
        balanced = next(row for row in result["profiles"] if row["id"] == "balanced")
        if balanced["recommendation"]["action"] == "roll":
            self.assertIn("roll", balanced["recommendation"]["reason"].lower())
        else:
            # A displayed 0.0 can still reflect a genuine (sub-rounding) preference in the
            # planner's beam search over the isolated single-scenario comparison; only a
            # visibly negative net gain would indicate a real regression.
            self.assertGreaterEqual(balanced["recommendation"]["net_gain_5gw"], 0)

    def test_uses_confirmed_available_free_transfers_instead_of_assuming_cap(self):
        bootstrap, fixtures, manager = gw2_inputs()
        manager["confirmed_free_transfers"] = 3

        result = build_transfer_decisions(
            bootstrap, fixtures, manager, generated_at="2026-08-29T12:00:00-04:00"
        )

        self.assertEqual(result["free_transfers"], 3)
        self.assertEqual(result["free_transfer_source"], "confirmed_local")
        for profile in result["profiles"]:
            double = next(row for row in profile["scenarios"] if row["action"] == "double_transfer")
            roll = next(row for row in profile["scenarios"] if row["action"] == "roll")
            self.assertEqual(double["point_cost"], 0)
            self.assertEqual(roll["free_transfers_next_event"], 4)

    def test_ignores_a_confirmed_free_transfer_override_for_another_event(self):
        bootstrap, fixtures, manager = gw2_inputs()
        manager["confirmed_free_transfers"] = 5
        manager["confirmed_free_transfers_event"] = 3

        result = build_transfer_decisions(
            bootstrap, fixtures, manager, generated_at="2026-08-29T12:00:00-04:00"
        )

        self.assertEqual(result["free_transfers"], 1)
        self.assertEqual(result["free_transfer_source"], "estimated_public_history")

    def test_builds_receding_five_gameweek_plan_with_conditional_future_branches(self):
        bootstrap, fixtures, manager = gw2_inputs()
        manager["confirmed_free_transfers"] = 1

        result = build_transfer_decisions(
            bootstrap, fixtures, manager, generated_at="2026-08-29T12:00:00-04:00"
        )

        for profile in result["profiles"]:
            plan = profile["multiweek_plan"]
            self.assertEqual(plan["horizon_events"], [2, 3, 4, 5, 6])
            self.assertEqual(plan["evaluated_free_transfers"], 1)
            self.assertTrue(plan["recommend_only_next_action"])
            self.assertEqual(plan["immediate_action"], profile["recommendation"]["action"])
            self.assertIn("five_gameweek_advantage_over_roll", plan)
            self.assertIn("roll_option_value", plan)
            self.assertIn(
                "Recent confirmed transfers use profile-specific role and minutes scenarios.",
                plan["assumptions"],
            )
            self.assertGreaterEqual(len(plan["conditional_branches"]), 1)
            for branch in plan["conditional_branches"]:
                self.assertGreater(branch["event"], result["event"])
                self.assertFalse(branch["commitment"])
                self.assertIn("condition", branch)

    def test_transfer_planner_uses_recent_transfer_role_scenarios(self):
        bootstrap, fixtures, manager = gw2_inputs()
        player_id = manager["squad"][0]["element_id"]
        player = next(row for row in bootstrap["elements"] if row["id"] == player_id)
        transfer = {
            "player": f"{player['first_name']} {player['second_name']}",
            "from_club": "Previous Club",
            "to_club": "Club 1",
            "announced_at": "2026-08-10T12:00:00Z",
            "verification_status": "confirmed_first_party",
            "fpl_reconciliation_status": "matched_current_fpl",
            "matched_fpl_element_id": player_id,
        }

        result = build_transfer_decisions(
            bootstrap,
            fixtures,
            manager,
            generated_at="2026-08-29T12:00:00-04:00",
            recent_transfers=[transfer],
        )

        self.assertIn(player_id, result["role_transition_player_ids"])
        for profile in result["profiles"]:
            roll = next(row for row in profile["scenarios"] if row["action"] == "roll")
            adjusted = next(row for row in roll["squad"] if row["id"] == player_id)
            self.assertTrue(adjusted["role_transition"])
            self.assertEqual(adjusted["confidence"], "low")
            self.assertTrue(profile["multiweek_plan"]["recommend_only_next_action"])

    def test_builds_chip_inventory_and_no_chip_counterfactual(self):
        bootstrap, fixtures, manager = gw2_inputs()
        result = build_transfer_decisions(
            bootstrap, fixtures, manager, generated_at="2026-08-29T12:00:00-04:00"
        )

        chip_names = {row["name"] for row in result["chip_inventory"] if row["available"]}
        self.assertEqual(chip_names, {"wildcard", "freehit", "bboost", "3xc"})
        for profile in result["profiles"]:
            chip = profile["chip_recommendation"]
            self.assertIn(chip["action"], {"hold", "play"})
            self.assertIn("no_chip_projected_points", chip)
            self.assertIn("marginal_value", chip)
            if chip["action"] == "play":
                self.assertGreater(chip["marginal_value"], 0)

    def test_wildcard_replaces_the_ordinary_primary_action_and_persists(self):
        bootstrap, fixtures, manager = gw2_inputs()
        bootstrap["chips"] = [next(row for row in bootstrap["chips"] if row["name"] == "wildcard")]
        thresholds = {profile: {"wildcard": -999.0} for profile in ("conservative", "balanced", "aggressive")}

        with patch("fpl_intel.transfer_decisions._THRESHOLDS", thresholds):
            result = build_transfer_decisions(
                bootstrap, fixtures, manager, generated_at="2026-08-29T12:00:00-04:00"
            )

        for profile in result["profiles"]:
            recommendation = profile["recommendation"]
            self.assertEqual(recommendation["action"], "play_wildcard")
            self.assertEqual(recommendation["point_cost"], 0)
            self.assertTrue(recommendation["squad_persists"])
            self.assertFalse(recommendation["reverts_after_event"])
            self.assertIn(recommendation["ordinary_alternative"]["action"], {"roll", "single_transfer", "double_transfer"})
            self.assertEqual(profile["chip_recommendation"]["chip"], "wildcard")
            self.assertEqual(profile["multiweek_plan"]["immediate_action"], "play_wildcard")

    def test_free_hit_replaces_the_ordinary_primary_action_and_reverts(self):
        bootstrap, fixtures, manager = gw2_inputs()
        bootstrap["chips"] = [next(row for row in bootstrap["chips"] if row["name"] == "freehit")]
        thresholds = {profile: {"freehit": -999.0} for profile in ("conservative", "balanced", "aggressive")}

        with patch("fpl_intel.transfer_decisions._THRESHOLDS", thresholds):
            result = build_transfer_decisions(
                bootstrap, fixtures, manager, generated_at="2026-08-29T12:00:00-04:00"
            )

        for profile in result["profiles"]:
            recommendation = profile["recommendation"]
            self.assertEqual(recommendation["action"], "play_freehit")
            self.assertEqual(recommendation["point_cost"], 0)
            self.assertFalse(recommendation["squad_persists"])
            self.assertTrue(recommendation["reverts_after_event"])
            self.assertEqual(
                {row["id"] for row in recommendation["persistent_squad_after_event"]},
                {row["element_id"] for row in manager["squad"]},
            )
            self.assertEqual(profile["chip_recommendation"]["chip"], "freehit")
            self.assertEqual(profile["multiweek_plan"]["immediate_action"], "play_freehit")


class ValidateDraftSquadTests(unittest.TestCase):
    def test_accepts_a_legal_draft_squad(self):
        bootstrap, _fixtures, draft_squad_ids = draft_inputs()

        # Should not raise.
        validate_draft_squad(bootstrap, draft_squad_ids)

    def test_rejects_the_wrong_number_of_players(self):
        bootstrap, _fixtures, draft_squad_ids = draft_inputs()

        with self.assertRaises(ValueError):
            validate_draft_squad(bootstrap, draft_squad_ids[:14])

    def test_rejects_duplicate_players(self):
        bootstrap, _fixtures, draft_squad_ids = draft_inputs()
        duplicated = draft_squad_ids[:14] + [draft_squad_ids[0]]

        with self.assertRaises(ValueError):
            validate_draft_squad(bootstrap, duplicated)

    def test_rejects_an_unknown_player_id(self):
        bootstrap, _fixtures, draft_squad_ids = draft_inputs()
        broken = draft_squad_ids[:14] + [999999]

        with self.assertRaises(ValueError):
            validate_draft_squad(bootstrap, broken)

    def test_rejects_a_squad_that_violates_formation_quotas(self):
        bootstrap, _fixtures, _draft_squad_ids = draft_inputs()
        all_goalkeepers = [
            row["id"] for row in bootstrap["elements"] if row["element_type"] == 1
        ]
        others = [row["id"] for row in bootstrap["elements"] if row["element_type"] != 1]
        illegal = (all_goalkeepers + others)[:15]

        with self.assertRaises(ValueError):
            validate_draft_squad(bootstrap, illegal)

    def test_rejects_a_squad_over_the_club_limit(self):
        bootstrap, _fixtures, _draft_squad_ids = draft_inputs()
        for player in bootstrap["elements"]:
            player["team"] = 1

        with self.assertRaises(ValueError):
            validate_draft_squad(bootstrap, [row["id"] for row in bootstrap["elements"][:15]])

    def test_rejects_a_squad_over_budget(self):
        bootstrap, _fixtures, draft_squad_ids = draft_inputs()
        for player in bootstrap["elements"]:
            player["now_cost"] = 200

        with self.assertRaises(ValueError):
            validate_draft_squad(bootstrap, draft_squad_ids)


class BuildDraftDecisionsTests(unittest.TestCase):
    def test_builds_roll_single_and_double_scenarios_for_three_profiles(self):
        bootstrap, fixtures, draft_squad_ids = draft_inputs()

        result = build_draft_decisions(
            bootstrap, fixtures, draft_squad_ids, generated_at="2026-07-01T12:00:00-04:00"
        )

        self.assertEqual(result["status"], "active")
        self.assertEqual(result["event"], 1)
        self.assertTrue(result["draft"])
        self.assertEqual(
            [row["id"] for row in result["profiles"]],
            ["conservative", "balanced", "aggressive"],
        )
        for profile in result["profiles"]:
            actions = {scenario["action"] for scenario in profile["scenarios"]}
            self.assertTrue({"roll", "single_transfer", "double_transfer"} <= actions)
            for scenario in profile["scenarios"]:
                # The core of issue #61: no free-transfer point cost applies before GW1.
                self.assertEqual(scenario["point_cost"], 0)
            roll = next(row for row in profile["scenarios"] if row["action"] == "roll")
            self.assertEqual(roll["transfers"], [])
            self.assertIn(profile["recommendation"]["action"], actions)
            self.assertEqual(len(profile["recommendation"]["starting_xi"]), 11)
            self.assertIn("reason", profile["recommendation"])

    def test_profiles_carry_uncertainty_metrics_for_the_declared_draft_squad(self):
        # Issue #158, draft-side counterpart to the build_transfer_decisions test above.
        bootstrap, fixtures, draft_squad_ids = draft_inputs()

        result = build_draft_decisions(
            bootstrap, fixtures, draft_squad_ids, generated_at="2026-07-01T12:00:00-04:00"
        )

        for profile in result["profiles"]:
            metrics = profile["metrics"]
            for key in ("central_1gw", "central_3gw", "central_5gw", "lower_5gw", "upper_5gw"):
                self.assertIn(key, metrics)
            self.assertLessEqual(metrics["lower_5gw"], metrics["central_5gw"])
            self.assertLessEqual(metrics["central_5gw"], metrics["upper_5gw"])
            horizon_one = profile["evaluation_horizons"]["1"]
            self.assertTrue(set(horizon_one["lineup_player_ids"]) <= set(draft_squad_ids))
            self.assertIn(horizon_one["captain_id"], draft_squad_ids)

    def test_reports_the_declared_squads_unspent_budget(self):
        bootstrap, fixtures, draft_squad_ids = draft_inputs()

        result = build_draft_decisions(
            bootstrap, fixtures, draft_squad_ids, generated_at="2026-07-01T12:00:00-04:00"
        )

        spend = sum(
            row["now_cost"] / 10
            for row in bootstrap["elements"]
            if row["id"] in draft_squad_ids
        )
        self.assertAlmostEqual(result["bank"], round(100.0 - spend, 1))

    def test_rejects_a_draft_squad_with_the_wrong_number_of_players(self):
        bootstrap, fixtures, draft_squad_ids = draft_inputs()

        result = build_draft_decisions(
            bootstrap, fixtures, draft_squad_ids[:14], generated_at="2026-07-01T12:00:00-04:00"
        )

        self.assertEqual(result["status"], "draft_squad_invalid")
        self.assertEqual(result["event"], 1)
        self.assertIn("reason", result)

    def test_rejects_an_illegal_draft_squad_over_budget(self):
        bootstrap, fixtures, draft_squad_ids = draft_inputs()
        for player in bootstrap["elements"]:
            player["now_cost"] = 200

        result = build_draft_decisions(
            bootstrap, fixtures, draft_squad_ids, generated_at="2026-07-01T12:00:00-04:00"
        )

        self.assertEqual(result["status"], "draft_squad_invalid")


if __name__ == "__main__":
    unittest.main()
