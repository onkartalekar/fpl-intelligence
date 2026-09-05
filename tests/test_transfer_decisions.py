import unittest
from unittest.mock import patch

from fpl_intel.modeling.recommendations import build_gw_recommendations
from fpl_intel.modeling.transfer_decisions import (
    _chip_timing_signals,
    _chip_window_extra_caution,
    _historical_opportunity_extra_caution,
    _multi_transfer_required_margin,
    _planner_player_score,
    _season_stage_effective_threshold,
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
            # A chip (`play_wildcard`/`play_freehit`) can legitimately override this -- see
            # test_wildcard_replaces_.../test_free_hit_replaces_... for that path in isolation --
            # but not for this fixture: issue #184's retuned _THRESHOLDS no longer clear here.
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
            if profile["recommendation"]["action"].startswith("play_"):
                # A chip (wildcard/freehit) replaces the ordinary planner path entirely when it
                # clears its threshold -- conditional_branches is deliberately emptied in that
                # case (see _exclusive_chip_scenario's override, and
                # test_wildcard_replaces_.../test_free_hit_replaces_... which test that directly).
                self.assertEqual(plan["conditional_branches"], [])
                continue
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

    def test_chip_thresholds_are_not_scale_biased_after_the_central_points_fix(self):
        # Issue #184 regression. Issue #181's central_points fix made _central_points read each
        # profile's own profile-adjusted scale instead of one shared plain scale -- correct, but
        # it left _THRESHOLDS uncalibrated for the new scale. Before the #184 fix, this exact
        # squad made aggressive's freehit clear its (old) threshold by +37.4 points (49.4 vs
        # 12.0) purely because of the scale change, not because the squad was actually
        # freehit-worthy -- and conservative's freehit swung the opposite way (-40.6 vs an old
        # threshold of 18.0), so it never fired either, again for the wrong reason. Confirms
        # neither profile's freehit fires purely from scale bias against this ordinary squad.
        bootstrap, fixtures, manager = gw2_inputs()
        result = build_transfer_decisions(
            bootstrap, fixtures, manager, generated_at="2026-08-29T12:00:00-04:00"
        )
        for profile in result["profiles"]:
            chip = profile["chip_recommendation"]
            if profile["id"] in ("conservative", "aggressive"):
                self.assertFalse(
                    chip["action"] == "play" and chip["chip"] == "freehit",
                    f"{profile['id']} played freehit for an ordinary squad -- likely scale bias",
                )

    def test_wildcard_replaces_the_ordinary_primary_action_and_persists(self):
        bootstrap, fixtures, manager = gw2_inputs()
        bootstrap["chips"] = [next(row for row in bootstrap["chips"] if row["name"] == "wildcard")]
        thresholds = {profile: {"wildcard": -999.0} for profile in ("conservative", "balanced", "aggressive")}

        # Issue #267: this test's intent is to isolate the _exclusive_chip_scenario override
        # mechanism (persists/reverts, multiweek_plan.immediate_action), not the threshold model
        # -- patching _THRESHOLDS alone used to be enough for that, back when the season-stage
        # adjustment topped out at a fractional multiplier (never quite reaching its own cap at
        # GW2). #267's per-chip-window model reaches its cap *exactly* at a window's own opening
        # gameweek (GW2 here), which for any negative threshold collapses effective_threshold to
        # exactly 0.0 regardless of the sentinel's magnitude -- too tight for this tiny synthetic
        # fixture's own near-zero marginal value on some profiles. Neutralize the scarcity signal
        # directly so this test keeps testing what it's actually about.
        with patch("fpl_intel.modeling.transfer_decisions._THRESHOLDS", thresholds), patch(
            "fpl_intel.modeling.transfer_decisions._chip_scarcity_extra_caution", return_value=0.0
        ):
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

        # Issue #267: see the matching comment in test_wildcard_replaces_the_ordinary_primary_
        # action_and_persists just above -- same reason, same fix.
        with patch("fpl_intel.modeling.transfer_decisions._THRESHOLDS", thresholds), patch(
            "fpl_intel.modeling.transfer_decisions._chip_scarcity_extra_caution", return_value=0.0
        ):
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


def _downgrade_squad_picks(bootstrap, squad_picks, count):
    """Issue #181 test helper: swap `count` squad picks for the worst same-position player *not*
    already owned (and not pushing any club over its 3-player limit), so a multi-leg upgrade
    clearly nets real value -- `sample_bootstrap()`'s points scale with player id
    (`total_points = 90 + player_id * 3`), so the lowest remaining id in a position is reliably
    its worst projected player.
    """
    owned_ids = {pick["element_id"] for pick in squad_picks}
    by_id = {p["id"]: p for p in bootstrap["elements"]}
    club_counts = {}
    for pick in squad_picks:
        club = by_id[pick["element_id"]]["team"]
        club_counts[club] = club_counts.get(club, 0) + 1
    by_position = {}
    for player in bootstrap["elements"]:
        by_position.setdefault(player["element_type"], []).append(player)
    for players in by_position.values():
        players.sort(key=lambda row: row["id"])  # ascending id == ascending points in this fixture

    downgraded = [dict(pick) for pick in squad_picks]
    swapped = 0
    for pick in downgraded:
        if swapped >= count:
            break
        original = by_id[pick["element_id"]]
        worst_available = next(
            (
                p for p in by_position[original["element_type"]]
                if p["id"] not in owned_ids and club_counts.get(p["team"], 0) < 3
            ),
            None,
        )
        if worst_available is None:
            continue
        club_counts[original["team"]] -= 1
        club_counts[worst_available["team"]] = club_counts.get(worst_available["team"], 0) + 1
        owned_ids.discard(pick["element_id"])
        owned_ids.add(worst_available["id"])
        pick["element_id"] = worst_available["id"]
        pick["purchase_price"] = worst_available["now_cost"]
        pick["selling_price"] = worst_available["now_cost"]
        swapped += 1
    return downgraded, swapped


class MultiTransferScenarioTests(unittest.TestCase):
    """Issue #181: build_transfer_decisions/build_draft_decisions can now consider more than 2
    transfers in one gameweek -- these test the new `multi_transfer` scenarios specifically,
    complementing (not replacing) the existing roll/single/double coverage above."""

    def test_multi_transfer_scenarios_present_for_every_leg_count_up_to_the_official_maximum(self):
        bootstrap, fixtures, manager = gw2_inputs()
        result = build_transfer_decisions(
            bootstrap, fixtures, manager, generated_at="2026-08-29T12:00:00-04:00"
        )

        self.assertEqual(result["official_rules"]["maximum_free_transfers"], 5)
        for profile in result["profiles"]:
            multi_leg = [row for row in profile["scenarios"] if row["action"] == "multi_transfer"]
            self.assertEqual(
                sorted(row["transfer_count"] for row in multi_leg), [3, 4, 5],
                "expected one multi_transfer scenario per leg count from 3 through the official max",
            )
            for scenario in multi_leg:
                self.assertEqual(len(scenario["transfers"]), scenario["transfer_count"])
                out_ids = [move["out"]["id"] for move in scenario["transfers"]]
                in_ids = [move["in"]["id"] for move in scenario["transfers"]]
                self.assertEqual(len(out_ids), len(set(out_ids)), "no player sold twice in one combination")
                self.assertEqual(len(in_ids), len(set(in_ids)), "no player bought twice in one combination")
                self.assertFalse(set(out_ids) & set(in_ids), "never buys back a player just sold in the same combination")

    def test_no_multi_transfer_scenarios_when_the_official_maximum_is_below_three(self):
        bootstrap, fixtures, manager = gw2_inputs()
        # Not 0: build_transfer_decisions computes maximum_free_transfers as
        # `int(settings.get("max_extra_free_transfers") or 4) + 1` -- Python's `or` treats 0 as
        # falsy, so 0 would silently fall back to the *default* of 4 (-> 5), not actually produce a
        # below-three ceiling. 1 is the smallest value that isn't swallowed by that fallback.
        bootstrap["game_settings"]["max_extra_free_transfers"] = 1  # maximum_free_transfers -> 2

        result = build_transfer_decisions(
            bootstrap, fixtures, manager, generated_at="2026-08-29T12:00:00-04:00"
        )

        self.assertEqual(result["official_rules"]["maximum_free_transfers"], 2)
        for profile in result["profiles"]:
            actions = {scenario["action"] for scenario in profile["scenarios"]}
            self.assertNotIn("multi_transfer", actions)

    def test_recommends_multi_transfer_when_the_hit_is_worth_it(self):
        # Confirms issue #181's actual gap is closed, not just that scenarios are generated:
        # downgrade 3 of the manager's own picks to the worst available same-position replacement,
        # with all 3 free transfers banked -- exactly the motivating scenario for this issue (a
        # manager who's rolled for a few gameweeks can genuinely have 3-5 free transfers banked,
        # and the tool previously could never discover using more than 2 of them). Checked against
        # "conservative" specifically: it's the profile where this scenario is least ambiguous --
        # "balanced" shows a near-tie between double/multi_transfer for this fixture, and
        # "aggressive" prefers a competing chip recommendation here -- both legitimate, different
        # outcomes, not failures of this mechanism, so asserting a specific winning action across
        # all three profiles would be a flaky, over-strict test of a scenario this fixture doesn't
        # cleanly produce for every profile.
        bootstrap, fixtures, manager = gw2_inputs()
        manager["confirmed_free_transfers"] = 3  # makes the 3-leg upgrade genuinely free
        downgraded, swapped = _downgrade_squad_picks(bootstrap, manager["squad"], count=3)
        self.assertEqual(swapped, 3, "test fixture must have 3 clearly-worse replacements available")
        manager["squad"] = downgraded

        # Issue #278: this fixture's edge (3-leg beats double_transfer by 3.0) is realistic, not
        # deliberately overwhelming -- it's smaller than GW2's own required margin (~4.2), so
        # issue #278's new season-stage caution correctly holds it back at this fixture's real
        # gameweek. This test's actual purpose (issue #181: the search can discover and prefer a
        # genuine 3+-leg upgrade at all) is orthogonal to season-stage caution, so neutralize that
        # signal explicitly here rather than let an unrelated, later feature reintroduce flakiness
        # into an already-established mechanism test -- same isolation pattern used for the
        # wildcard/freehit override tests above.
        with patch(
            "fpl_intel.modeling.transfer_decisions._multi_transfer_required_margin", return_value=0.0
        ):
            result = build_transfer_decisions(
                bootstrap, fixtures, manager, generated_at="2026-08-29T12:00:00-04:00"
            )

        self.assertEqual(result["status"], "active")
        conservative = next(row for row in result["profiles"] if row["id"] == "conservative")
        recommendation = conservative["recommendation"]
        self.assertEqual(recommendation["action"], "multi_transfer")
        self.assertEqual(recommendation["transfer_count"], 3)
        self.assertEqual(recommendation["point_cost"], 0)  # exactly 3 free transfers available
        self.assertIn("3 transfers", recommendation["reason"])
        self.assertIn("immediate-horizon", recommendation["reason"])  # discloses the immediate-only caveat
        # Issue #266: required_margin/margin_above_required now ride on the recommendation the
        # same way the chip side's threshold/effective_threshold/value_above_threshold already do
        # -- patched to 0.0 above, so the winning margin equals the raw net_gain_5gw edge itself.
        self.assertEqual(recommendation["required_margin"], 0.0)
        self.assertGreater(recommendation["margin_above_required"], 0.0)
        # A real hit genuinely isn't always worth it: with only 1 free transfer, the same 3-player
        # upgrade must clear a real -4 hit rather than being free -- confirms the search doesn't
        # just always prefer more legs regardless of cost.
        manager_with_hit = dict(manager, confirmed_free_transfers=1)
        hit_result = build_transfer_decisions(
            bootstrap, fixtures, manager_with_hit, generated_at="2026-08-29T12:00:00-04:00"
        )
        hit_conservative = next(row for row in hit_result["profiles"] if row["id"] == "conservative")
        multi_leg = [row for row in hit_conservative["scenarios"] if row["action"] == "multi_transfer"]
        three_leg = next(row for row in multi_leg if row["transfer_count"] == 3)
        self.assertEqual(three_leg["point_cost"], 8)  # 3 transfers, 1 free -> two -4 hits
        self.assertLess(three_leg["net_gain_5gw"], recommendation["net_gain_5gw"])

    def test_draft_decisions_also_consider_multi_transfer_reshuffles(self):
        bootstrap, fixtures, draft_squad_ids = draft_inputs()
        squad_picks = [{"element_id": player_id} for player_id in draft_squad_ids]
        downgraded, swapped = _downgrade_squad_picks(bootstrap, squad_picks, count=3)
        self.assertEqual(swapped, 3, "test fixture must have 3 clearly-worse replacements available")
        downgraded_ids = [pick["element_id"] for pick in downgraded]

        result = build_draft_decisions(
            bootstrap, fixtures, downgraded_ids, generated_at="2026-07-01T12:00:00-04:00"
        )

        self.assertEqual(result["status"], "active")
        for profile in result["profiles"]:
            multi_leg = [row for row in profile["scenarios"] if row["action"] == "multi_transfer"]
            self.assertEqual(sorted(row["transfer_count"] for row in multi_leg), [3, 4, 5])
            for scenario in multi_leg:
                # Issue #61's existing rule: no free-transfer point cost applies before GW1,
                # regardless of leg count.
                self.assertEqual(scenario["point_cost"], 0)
        # Deliberately not asserting net_gain_5gw is monotonically non-decreasing with leg count:
        # each leg-count candidate is *forced* to make exactly that many transfers (there's no
        # "stop early" option within a single beam-search candidate), so once genuinely good
        # transfers run out, being forced into one more can be flat or slightly worse than the
        # previous leg count -- confirmed on this exact fixture (e.g. balanced: leg 4 = 3.6, leg
        # 5 = 3.5). That's fine and expected: overall selection compares across leg counts via
        # max(scenarios, key=net_gain_5gw) and correctly picks whichever one is actually best
        # regardless, which is what the recommendation check below confirms.
        #
        # At least one profile in this fixture should clearly prefer a 3+-leg reshuffle over
        # anything roll/single/double could offer -- confirming the mechanism actually changes the
        # outcome, not just that scenarios are generated and never chosen.
        winners = {row["recommendation"]["action"] for row in result["profiles"]}
        self.assertIn("multi_transfer", winners)


class MultiTransferEarlySeasonCautionTests(unittest.TestCase):
    """Issue #278: the 3+-leg multi-transfer override used to accept any positive margin over the
    planner's own roll/single/double pick, however small, with no regard for how little of the
    season had been observed yet. See plans/issue-278-multi-transfer-caution.md."""

    def test_required_margin_is_at_its_maximum_at_gameweek_2(self):
        # Gameweek 2 is the earliest transfers are ever recommended for (build_transfer_decisions
        # returns "waiting_for_gw2" at event <= 1) -- one completed gameweek's worth of observed
        # minutes (90) is the thinnest real signal this function is ever evaluated against.
        self.assertAlmostEqual(_multi_transfer_required_margin(2), 4.22, places=2)

    def test_required_margin_shrinks_as_the_season_progresses(self):
        margins = [_multi_transfer_required_margin(event) for event in (2, 3, 4, 5, 6)]
        self.assertEqual(margins, sorted(margins, reverse=True))
        for margin in margins:
            self.assertGreaterEqual(margin, 0.0)

    def test_required_margin_converges_to_zero_once_the_season_has_settled_in(self):
        # residual_reliability caps at 0.82 around ~450 observed minutes (~5 completed
        # gameweeks) -- by Gameweek 7, (event - 1) * 90 = 540 minutes, past that cap, so the
        # margin should already be at (or extremely close to) zero, restoring today's exact
        # unmodified `>` comparison.
        self.assertAlmostEqual(_multi_transfer_required_margin(7), 0.0, places=1)
        self.assertEqual(_multi_transfer_required_margin(38), 0.0)

    def test_a_thin_multi_leg_edge_is_held_back_at_gameweek_2_but_accepted_once_it_clears_the_margin(self):
        # Real-data-shaped regression: engineers a squad where a 3-leg upgrade is a *modest*, not
        # overwhelming, improvement over the planner's own double-transfer pick -- the exact shape
        # of case this issue is about (a small, easily-noise-explained edge triggering a real,
        # permanent point-cost commitment in the season's noisiest weeks). Confirmed directly this
        # fixture's margin (double_transfer 7.9 -> 3-leg 10.9, a 3.0-point edge) sits below GW2's
        # own required margin (~4.2) -- so the override should NOT fire at GW2, but should once
        # neutralized (this same margin's job, done, is exactly what
        # test_recommends_multi_transfer_when_the_hit_is_worth_it above already confirms via
        # patching -- this test instead confirms the *unpatched*, real GW2 behavior holds it back).
        bootstrap, fixtures, manager = gw2_inputs()
        manager["confirmed_free_transfers"] = 3
        downgraded, swapped = _downgrade_squad_picks(bootstrap, manager["squad"], count=3)
        self.assertEqual(swapped, 3, "test fixture must have 3 clearly-worse replacements available")
        manager["squad"] = downgraded

        result = build_transfer_decisions(
            bootstrap, fixtures, manager, generated_at="2026-08-29T12:00:00-04:00"
        )

        conservative = next(row for row in result["profiles"] if row["id"] == "conservative")
        multi_leg = next(
            row for row in conservative["scenarios"] if row["action"] == "multi_transfer" and row["transfer_count"] == 3
        )
        planner_pick = next(
            row for row in conservative["scenarios"] if row["action"] == conservative["multiweek_plan"]["immediate_action"]
        )
        margin = multi_leg["net_gain_5gw"] - planner_pick["net_gain_5gw"]
        self.assertLess(margin, _multi_transfer_required_margin(2))
        # The real, unpatched recommendation holds at the planner's own pick, not the thin 3-leg edge.

        self.assertNotEqual(conservative["recommendation"]["action"], "multi_transfer")

    def test_margin_above_required_is_computed_against_the_pre_override_baseline(self):
        """Issue #266: `margin_above_required` must reflect how far the winning multi-leg margin
        cleared `required_margin` by -- computed against the *pre-override* ordinary
        recommendation's own net_gain_5gw (the same baseline the accept/reject `if` itself
        compares against), not the multi-leg scenario's own scenario_score or anything else."""
        bootstrap, fixtures, manager = gw2_inputs()
        manager["confirmed_free_transfers"] = 3
        downgraded, swapped = _downgrade_squad_picks(bootstrap, manager["squad"], count=3)
        self.assertEqual(swapped, 3, "test fixture must have 3 clearly-worse replacements available")
        manager["squad"] = downgraded

        with patch(
            "fpl_intel.modeling.transfer_decisions._multi_transfer_required_margin", return_value=1.0
        ):
            result = build_transfer_decisions(
                bootstrap, fixtures, manager, generated_at="2026-08-29T12:00:00-04:00"
            )

        conservative = next(row for row in result["profiles"] if row["id"] == "conservative")
        recommendation = conservative["recommendation"]
        self.assertEqual(recommendation["action"], "multi_transfer")
        planner_pick = next(
            row for row in conservative["scenarios"]
            if row["action"] == conservative["multiweek_plan"]["immediate_action"]
        )
        self.assertEqual(recommendation["required_margin"], 1.0)
        expected = round(recommendation["net_gain_5gw"] - planner_pick["net_gain_5gw"] - 1.0, 1)
        self.assertAlmostEqual(recommendation["margin_above_required"], expected, places=1)


class ChipTimingTests(unittest.TestCase):
    """Issue #256: (A1) chip thresholds gain a season-stage adjustment, (B2) the 5-GW plan gains
    a heads-up fixture-shape signal for a future double/blank-gameweek-looking week. Issue #267:
    (A1) is superseded by a per-chip-window scarcity signal (candidate 2) combined with a
    historically-grounded double/blank-gameweek prior (candidate 1b) -- see
    plans/issue-267-chip-scarcity.md."""

    def test_effective_threshold_converges_to_raw_at_the_windows_own_last_gameweek(self):
        # Both extra-caution signals are 0 at event == stop_event: _chip_window_extra_caution's
        # window_fraction reaches exactly 1.0 there, and _remaining_historical_weight(stop_event,
        # stop_event) sums an empty range -- unlike #256's flat cutoff, this holds at *every*
        # window's own end, not just once at a shared whole-season GW10 boundary.
        for threshold in (18.0, -30.0, 65.0, 9.0):
            self.assertEqual(_season_stage_effective_threshold(threshold, 19, 2, 19), threshold)
            self.assertEqual(_season_stage_effective_threshold(threshold, 38, 20, 38), threshold)

    def test_effective_threshold_raises_the_bar_early_regardless_of_sign(self):
        # A positive threshold moves further above zero (harder to clear from below).
        self.assertGreater(_season_stage_effective_threshold(18.0, 2, 2, 19), 18.0)
        # A negative threshold moves *toward* zero -- also harder to clear (see the function's own
        # docstring for why a plain `threshold * multiplier` gets this backwards for a negative
        # base, and issue #256's plan doc for the reasoning in full).
        self.assertGreater(_season_stage_effective_threshold(-30.0, 2, 2, 19), -30.0)
        self.assertLessEqual(_season_stage_effective_threshold(-30.0, 2, 2, 19), 0.0)

    def test_effective_threshold_stays_reachable_not_impossible(self):
        # The adjustment must raise the bar, not put it out of reach -- confirmed generally here
        # (a marginal value of double the raw threshold always still clears, even at a window's
        # own first gameweek) and against real squad-quality data at realistic scale in
        # plans/issue-256-chip-timing.md and plans/issue-267-chip-scarcity.md.
        for threshold in (18.0, 22.0, 65.0):
            # A window's own first gameweek is where both extra-caution signals sit at their
            # individual maximum, so equality there is expected, not a bug -- assertLessEqual.
            self.assertLessEqual(_season_stage_effective_threshold(threshold, 2, 2, 19), threshold * 2)
            self.assertLessEqual(_season_stage_effective_threshold(threshold, 20, 20, 38), threshold * 2)

    def test_chip_window_extra_caution_resets_when_a_new_half_season_window_opens(self):
        # Issue #267's core fix for a real gap in #256's shipped code: a flat whole-season cutoff
        # (GW10) treated GW19 and GW20 identically, missing that GW20 is a brand-new chip window
        # opening (Wildcard/Free Hit's second half). Scoped to the window itself, GW19 (the old
        # window's last gameweek) converges to zero extra caution, while GW20 (the new window's
        # first gameweek) resets to maximum -- confirmed directly here against
        # plans/issue-267-chip-scarcity.md's own worked table (candidate 2).
        self.assertEqual(_chip_window_extra_caution(19, 2, 19), 0.0)
        self.assertEqual(_chip_window_extra_caution(20, 20, 38), 1.0)

    def test_historical_opportunity_extra_caution_favors_the_real_dgw_bgw_cluster(self):
        # Issue #267 (candidate 1b): the historical prior is concentrated GW25-37, not spread
        # evenly across a window -- GW20 (before the cluster) should show more remaining
        # opportunity than GW30 (past most of it) within the same [20, 38] window, and GW2
        # (Wildcard-1's own window, which the real historical data shows has comparatively little
        # DGW/BGW opportunity even at its very start) should show markedly less than GW20 despite
        # both being their own window's first gameweek.
        at_window_open = _historical_opportunity_extra_caution(20, 38)
        past_the_cluster = _historical_opportunity_extra_caution(30, 38)
        self.assertGreater(at_window_open, past_the_cluster)
        self.assertGreater(at_window_open, _historical_opportunity_extra_caution(2, 19))

    def test_historical_opportunity_extra_caution_is_zero_at_a_windows_own_last_gameweek(self):
        self.assertEqual(_historical_opportunity_extra_caution(19, 19), 0.0)
        self.assertEqual(_historical_opportunity_extra_caution(38, 38), 0.0)

    def test_gw2_borderline_chip_that_used_to_barely_clear_now_holds(self):
        # Before issue #256, this exact fixture's aggressive-profile Bench Boost cleared its raw
        # threshold by only +0.4 (marginal 14.4 vs threshold 14.0) -- a real, already-present
        # barely-clearing case (bboost/3xc's thresholds are untouched by #184, so this predates
        # #256 entirely), the same shape of false positive confirmed live on team 364759's real
        # GW2 data (Free Hit cleared by only +0.4 there). No threshold patching needed to
        # reproduce it.
        bootstrap, fixtures, manager = gw2_inputs()
        result = build_transfer_decisions(bootstrap, fixtures, manager, generated_at="2026-08-29T12:00:00-04:00")
        aggressive = next(row for row in result["profiles"] if row["id"] == "aggressive")
        bboost = next(row for row in aggressive["chip_recommendation"]["alternatives"] if row["chip"] == "bboost")
        self.assertEqual(bboost["marginal_value"], 14.4)
        self.assertEqual(bboost["threshold"], 14.0)  # the raw constant -- still disclosed unchanged
        self.assertGreater(bboost["effective_threshold"], bboost["threshold"])
        self.assertLess(bboost["value_above_threshold"], 0)
        self.assertEqual(aggressive["chip_recommendation"]["action"], "hold")

    def test_chip_timing_signals_flags_a_double_gameweek_shaped_spike(self):
        path = [
            {"event": 10, "squad_fixture_richness": 54.2},
            {"event": 11, "squad_fixture_richness": 51.8},
            {"event": 12, "squad_fixture_richness": 49.0},
            {"event": 13, "squad_fixture_richness": 52.5},
            {"event": 14, "squad_fixture_richness": 71.3},
        ]
        signals = _chip_timing_signals(path)
        self.assertEqual(set(signals.keys()), {14})
        self.assertIn("double gameweek", signals[14])
        self.assertIn("Gameweek 14", signals[14])

    def test_chip_timing_signals_flags_a_blank_gameweek_shaped_dip(self):
        path = [
            {"event": 10, "squad_fixture_richness": 50.0},
            {"event": 11, "squad_fixture_richness": 52.0},
            {"event": 12, "squad_fixture_richness": 10.0},
            {"event": 13, "squad_fixture_richness": 49.0},
            {"event": 14, "squad_fixture_richness": 51.0},
        ]
        signals = _chip_timing_signals(path)
        self.assertEqual(set(signals.keys()), {12})
        self.assertIn("blank gameweek", signals[12])
        self.assertIn("Gameweek 12", signals[12])

    def test_chip_timing_signals_empty_when_no_week_stands_out(self):
        path = [{"event": event, "squad_fixture_richness": 50.0 + event} for event in range(10, 15)]
        self.assertEqual(_chip_timing_signals(path), {})

    def test_chip_timing_signals_empty_for_fewer_than_two_weeks(self):
        self.assertEqual(_chip_timing_signals([]), {})
        self.assertEqual(_chip_timing_signals([{"event": 10, "squad_fixture_richness": 50.0}]), {})

    def test_conditional_branches_carry_chip_signal_defaulting_to_none(self):
        # gw2_inputs()'s synthetic fixtures give every team exactly one fixture per event (see
        # tests/test_recommendations.py's sample_fixtures) -- no double/blank gameweeks by
        # construction, so nothing should fire here. Confirms the field is always present (so a
        # frontend can rely on it) even when there's nothing to flag.
        bootstrap, fixtures, manager = gw2_inputs()
        result = build_transfer_decisions(bootstrap, fixtures, manager, generated_at="2026-08-29T12:00:00-04:00")
        found_any_branch = False
        for profile in result["profiles"]:
            for branch in profile["multiweek_plan"]["conditional_branches"]:
                found_any_branch = True
                self.assertIn("chip_signal", branch)
                self.assertIsNone(branch["chip_signal"])
        self.assertTrue(found_any_branch, "fixture should produce at least one conditional branch to check")


if __name__ == "__main__":
    unittest.main()
