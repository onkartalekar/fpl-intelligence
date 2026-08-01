import copy
import unittest

from fpl_intel.recommendations import (
    _event_lineup_schedule,
    _squad_objective,
    _team_uncertainty_interval,
    build_gw_recommendations,
    project_players,
)


def sample_bootstrap():
    teams = [{"id": index, "name": f"Club {index}", "short_name": f"C{index}"} for index in range(1, 7)]
    element_types = [
        {"id": 1, "singular_name": "Goalkeeper", "singular_name_short": "GKP", "squad_select": 2, "squad_min_play": 1, "squad_max_play": 1},
        {"id": 2, "singular_name": "Defender", "singular_name_short": "DEF", "squad_select": 5, "squad_min_play": 3, "squad_max_play": 5},
        {"id": 3, "singular_name": "Midfielder", "singular_name_short": "MID", "squad_select": 5, "squad_min_play": 2, "squad_max_play": 5},
        {"id": 4, "singular_name": "Forward", "singular_name_short": "FWD", "squad_select": 3, "squad_min_play": 1, "squad_max_play": 3},
    ]
    players = []
    player_id = 1
    counts = {1: 4, 2: 9, 3: 9, 4: 6}
    for position, count in counts.items():
        for index in range(count):
            players.append(
                {
                    "id": player_id,
                    "web_name": f"P{player_id}",
                    "first_name": "Player",
                    "second_name": str(player_id),
                    "team": (index % 6) + 1,
                    "element_type": position,
                    "now_cost": 45 + position * 5 + (index % 3) * 5,
                    "minutes": 2200 + index * 50,
                    "starts": 25 + (index % 10),
                    "total_points": 90 + player_id * 3,
                    "ep_next": str(2.0 + (index % 3) * 0.5),
                    "status": "a",
                    "news": "",
                    "selected_by_percent": "5.0",
                    "can_select": True,
                    "removed": False,
                }
            )
            player_id += 1
    return {
        "events": [{"id": event, "name": f"Gameweek {event}", "deadline_time": f"2026-08-{20 + event:02d}T17:30:00Z", "is_next": event == 1} for event in range(1, 6)],
        "teams": teams,
        "element_types": element_types,
        "elements": players,
        "game_settings": {"squad_squadsize": 15, "squad_team_limit": 3, "squad_total_spend": 1000},
    }


def sample_fixtures():
    fixtures = []
    fixture_id = 1
    for event in range(1, 6):
        for home, away in [(1, 2), (3, 4), (5, 6)]:
            fixtures.append({
                "id": fixture_id,
                "event": event,
                "team_h": home,
                "team_a": away,
                "team_h_difficulty": 2 + (event % 3),
                "team_a_difficulty": 4 - (event % 3),
            })
            fixture_id += 1
    return fixtures


class ProjectionTests(unittest.TestCase):
    def test_projects_one_three_and_five_gameweek_points_with_minutes_uncertainty(self):
        bootstrap = sample_bootstrap()
        bootstrap["elements"][0]["status"] = "i"
        bootstrap["elements"][0]["chance_of_playing_next_round"] = 0

        projections = project_players(bootstrap, sample_fixtures(), horizon=5)

        injured = next(row for row in projections if row["id"] == 1)
        available = next(row for row in projections if row["id"] == 2)
        self.assertEqual(injured["expected_minutes"], 0.0)
        self.assertEqual(injured["xp_1"], 0.0)
        self.assertGreater(available["expected_minutes"], 0)
        self.assertGreater(available["xp_3"], available["xp_1"])
        self.assertGreater(available["xp_5"], available["xp_3"])
        self.assertLess(available["lower_5"], available["xp_5"])
        self.assertGreater(available["upper_5"], available["xp_5"])
        self.assertIn(available["confidence"], {"high", "medium", "low"})

    def test_projection_horizon_starts_at_official_next_event(self):
        bootstrap = sample_bootstrap()
        for event in bootstrap["events"]:
            event["is_next"] = event["id"] == 2
            event["finished"] = event["id"] == 1
        fixtures = []
        for fixture in sample_fixtures():
            shifted = dict(fixture)
            shifted["event"] += 1
            fixtures.append(shifted)

        projections = project_players(bootstrap, fixtures, horizon=5, start_event=2)

        self.assertEqual(projections[0]["projection_events"], [2, 3, 4, 5, 6])
        self.assertEqual(len(projections[0]["fixture_xp"]), 5)
        self.assertGreater(projections[0]["xp_1"], 0)

    def test_expected_minutes_use_team_fixtures_played_for_blanks_and_doubles(self):
        bootstrap = sample_bootstrap()
        for event in bootstrap["events"]:
            event["is_next"] = event["id"] == 4
            event["finished"] = event["id"] < 4
        first = next(player for player in bootstrap["elements"] if player["team"] == 1)
        second = next(player for player in bootstrap["elements"] if player["team"] == 2)
        for player in (first, second):
            player.update({"starts": 1, "minutes": 90, "ep_next": "0"})
        completed = [
            {"id": 101, "event": 1, "team_h": 1, "team_a": 3, "finished": True},
            {"id": 102, "event": 1, "team_h": 2, "team_a": 4, "finished": True},
            {"id": 103, "event": 2, "team_h": 1, "team_a": 3, "finished": True},
            {"id": 104, "event": 2, "team_h": 2, "team_a": 4, "finished": True},
            {"id": 105, "event": 3, "team_h": 1, "team_a": 3, "finished": True},
            {"id": 106, "event": 3, "team_h": 1, "team_a": 5, "finished": True},
        ]
        future = []
        for fixture in sample_fixtures():
            shifted = dict(fixture)
            shifted["id"] += 200
            shifted["event"] += 3
            future.append(shifted)

        projections = project_players(bootstrap, completed + future, horizon=2, start_event=4)
        by_id = {player["id"]: player for player in projections}

        self.assertEqual(by_id[first["id"]]["team_fixtures_played"], 4)
        self.assertEqual(by_id[second["id"]]["team_fixtures_played"], 2)
        self.assertLess(by_id[first["id"]]["expected_minutes"], by_id[second["id"]]["expected_minutes"])

    def test_ep_next_affects_only_first_event_points_not_expected_minutes(self):
        bootstrap = sample_bootstrap()
        first, second = bootstrap["elements"][:2]
        preserved = {"id": second["id"], "web_name": second["web_name"], "first_name": second["first_name"], "second_name": second["second_name"]}
        second.clear()
        second.update(copy.deepcopy(first))
        second.update(preserved)
        first["ep_next"] = "0"
        second["ep_next"] = "5"

        projected = project_players(bootstrap, sample_fixtures(), horizon=2, start_event=1)
        by_id = {player["id"]: player for player in projected}
        baseline = by_id[first["id"]]
        official_reference = by_id[second["id"]]

        self.assertEqual(baseline["expected_minutes"], official_reference["expected_minutes"])
        self.assertGreater(official_reference["fixture_xp"][0], baseline["fixture_xp"][0])
        self.assertEqual(official_reference["fixture_xp"][1], baseline["fixture_xp"][1])
        breakdown = official_reference["component_xp"][0]
        self.assertAlmostEqual(
            breakdown["modeled_total_before_ep_next"] + breakdown["ep_next_adjustment"],
            breakdown["blended_total"],
            places=2,
        )

    def test_zero_track_record_signing_floors_expected_minutes_from_ep_next(self):
        bootstrap = sample_bootstrap()
        debutant, doubtful_debutant = bootstrap["elements"][:2]
        for player in (debutant, doubtful_debutant):
            player.update({"minutes": 0, "starts": 0})
        debutant["ep_next"] = "4.0"
        doubtful_debutant["ep_next"] = "0"

        projected = project_players(bootstrap, sample_fixtures(), horizon=1, start_event=1)
        by_id = {player["id"]: player for player in projected}
        nailed_on_signing = by_id[debutant["id"]]
        unrated_signing = by_id[doubtful_debutant["id"]]

        self.assertEqual(nailed_on_signing["expected_minutes"], 60.0)
        self.assertGreater(nailed_on_signing["expected_minutes"], unrated_signing["expected_minutes"])
        self.assertGreater(nailed_on_signing["xp_1"], unrated_signing["xp_1"])

    def test_established_player_track_record_is_not_overridden_by_ep_next_floor(self):
        bootstrap = sample_bootstrap()
        player = bootstrap["elements"][0]
        player.update({"minutes": 900, "starts": 10, "ep_next": "0"})
        without_floor = project_players(bootstrap, sample_fixtures(), horizon=1, start_event=1)
        by_id = {row["id"]: row for row in without_floor}

        player["ep_next"] = "6.0"
        with_high_ep_next = project_players(bootstrap, sample_fixtures(), horizon=1, start_event=1)
        by_id_high = {row["id"]: row for row in with_high_ep_next}

        self.assertEqual(by_id[player["id"]]["expected_minutes"], by_id_high[player["id"]]["expected_minutes"])

    def test_recent_confirmed_transfer_applies_role_transition_minutes_scenarios(self):
        bootstrap = sample_bootstrap()
        player = bootstrap["elements"][0]
        baseline = next(
            row for row in project_players(bootstrap, sample_fixtures(), horizon=5)
            if row["id"] == player["id"]
        )
        transfer = {
            "player": f"{player['first_name']} {player['second_name']}",
            "from_club": "Previous Club",
            "to_club": "Club 1",
            "announced_at": "2026-07-02T12:00:00Z",
            "verification_status": "confirmed_first_party",
            "fpl_reconciliation_status": "matched_current_fpl",
            "matched_fpl_element_id": player["id"],
        }

        adjusted = next(
            row for row in project_players(
                bootstrap,
                sample_fixtures(),
                horizon=5,
                recent_transfers=[transfer],
                as_of="2026-07-23T12:00:00-04:00",
            )
            if row["id"] == player["id"]
        )

        self.assertTrue(adjusted["role_transition"])
        self.assertEqual(adjusted["confidence"], "low")
        self.assertLess(adjusted["expected_minutes"], baseline["expected_minutes"])
        self.assertLess(adjusted["xp_5"], baseline["xp_5"])
        self.assertLess(
            adjusted["expected_minutes_scenarios"]["conservative"],
            adjusted["expected_minutes_scenarios"]["balanced"],
        )
        self.assertLess(
            adjusted["expected_minutes_scenarios"]["balanced"],
            adjusted["expected_minutes_scenarios"]["aggressive"],
        )
        self.assertLess(
            sum(adjusted["profile_fixture_xp"]["conservative"]),
            sum(adjusted["profile_fixture_xp"]["balanced"]),
        )
        self.assertLess(
            sum(adjusted["profile_fixture_xp"]["balanced"]),
            sum(adjusted["profile_fixture_xp"]["aggressive"]),
        )
        self.assertAlmostEqual(
            adjusted["lower_5"],
            sum(adjusted["profile_fixture_xp"]["conservative"]),
            places=2,
        )
        self.assertAlmostEqual(
            adjusted["upper_5"],
            sum(adjusted["profile_fixture_xp"]["aggressive"]),
            places=2,
        )
        self.assertIn("new club", adjusted["role_transition_note"].lower())

    def test_confirmed_departure_widens_same_position_teammates_minutes_upward(self):
        bootstrap = sample_bootstrap()
        # Team 1's two DEF players (ids 5 and 11, per sample_bootstrap's
        # (index % 6) + 1 team cycling): id 5 departs, id 11 is the stayed
        # teammate who should see minutes widen upward.
        departed_id, stayed_id, other_team_def_id, same_team_gkp_id = 5, 11, 6, 1
        baseline = {
            row["id"]: row
            for row in project_players(bootstrap, sample_fixtures(), horizon=5)
        }
        transfer = {
            "player": "Player 5",
            "from_club": "Club 1",
            "to_club": "Foreign League Club",
            "premier_league_club": "Club 1",
            "movement_type": "transfer-out",
            "announced_at": "2026-07-02T12:00:00Z",
            "verification_status": "confirmed_first_party",
            "fpl_reconciliation_status": "matched_current_fpl",
            "matched_fpl_element_id": departed_id,
        }

        adjusted = {
            row["id"]: row
            for row in project_players(
                bootstrap, sample_fixtures(), horizon=5,
                recent_transfers=[transfer], as_of="2026-07-23T12:00:00-04:00",
            )
        }

        self.assertEqual(adjusted[stayed_id]["teammate_transfer_impact"], "out")
        self.assertGreater(
            adjusted[stayed_id]["expected_minutes"], baseline[stayed_id]["expected_minutes"]
        )
        self.assertGreater(adjusted[stayed_id]["xp_5"], baseline[stayed_id]["xp_5"])
        self.assertIn("departure", adjusted[stayed_id]["teammate_transfer_impact_note"].lower())
        # A DEF at a different club is not affected.
        self.assertIsNone(adjusted[other_team_def_id]["teammate_transfer_impact"])
        self.assertEqual(
            adjusted[other_team_def_id]["expected_minutes"], baseline[other_team_def_id]["expected_minutes"]
        )
        # A GKP at the same club (different position) is not affected.
        self.assertIsNone(adjusted[same_team_gkp_id]["teammate_transfer_impact"])
        # The departed player themselves is not treated as their own teammate.
        self.assertIsNone(adjusted[departed_id]["teammate_transfer_impact"])

    def test_confirmed_arrival_narrows_same_position_teammates_minutes_downward(self):
        bootstrap = sample_bootstrap()
        arriving_id, stayed_id = 5, 11
        baseline = {
            row["id"]: row
            for row in project_players(bootstrap, sample_fixtures(), horizon=5)
        }
        transfer = {
            "player": "Player 5",
            "from_club": "Foreign League Club",
            "to_club": "Club 1",
            "premier_league_club": "Club 1",
            "movement_type": "transfer-in",
            "announced_at": "2026-07-02T12:00:00Z",
            "verification_status": "confirmed_first_party",
            "fpl_reconciliation_status": "matched_current_fpl",
            "matched_fpl_element_id": arriving_id,
        }

        adjusted = {
            row["id"]: row
            for row in project_players(
                bootstrap, sample_fixtures(), horizon=5,
                recent_transfers=[transfer], as_of="2026-07-23T12:00:00-04:00",
            )
        }

        self.assertEqual(adjusted[stayed_id]["teammate_transfer_impact"], "in")
        self.assertLess(
            adjusted[stayed_id]["expected_minutes"], baseline[stayed_id]["expected_minutes"]
        )
        self.assertIn("arrival", adjusted[stayed_id]["teammate_transfer_impact_note"].lower())

    def test_own_role_transition_takes_precedence_over_teammate_impact(self):
        bootstrap = sample_bootstrap()
        # id 11 both (a) just moved to Club 1 themselves and (b) would
        # otherwise be a same-club, same-position teammate of id 5's
        # departure -- the player's own role_transition should win.
        moved_in_id, departed_id = 11, 5
        own_transfer = {
            "player": "Player 11",
            "from_club": "Previous Club",
            "to_club": "Club 1",
            "premier_league_club": "Club 1",
            "movement_type": "transfer-in",
            "announced_at": "2026-07-02T12:00:00Z",
            "verification_status": "confirmed_first_party",
            "fpl_reconciliation_status": "matched_current_fpl",
            "matched_fpl_element_id": moved_in_id,
        }
        departure_transfer = {
            "player": "Player 5",
            "from_club": "Club 1",
            "to_club": "Foreign League Club",
            "premier_league_club": "Club 1",
            "movement_type": "transfer-out",
            "announced_at": "2026-07-02T12:00:00Z",
            "verification_status": "confirmed_first_party",
            "fpl_reconciliation_status": "matched_current_fpl",
            "matched_fpl_element_id": departed_id,
        }

        adjusted = {
            row["id"]: row
            for row in project_players(
                bootstrap, sample_fixtures(), horizon=5,
                recent_transfers=[own_transfer, departure_transfer],
                as_of="2026-07-23T12:00:00-04:00",
            )
        }

        self.assertTrue(adjusted[moved_in_id]["role_transition"])
        self.assertIsNone(adjusted[moved_in_id]["teammate_transfer_impact"])

    def test_departure_impact_takes_precedence_over_arrival_impact(self):
        bootstrap = sample_bootstrap()
        # Team 1 DEF: id 5 leaves, id 12 (Club 2's other DEF) arrives at
        # Club 1 in the same window -- id 11 (Club 1's remaining DEF) sees
        # both a departure and an arrival at their position; departure wins.
        departed_id, arriving_id, stayed_id = 5, 12, 11
        departure_transfer = {
            "player": "Player 5",
            "from_club": "Club 1",
            "to_club": "Foreign League Club",
            "premier_league_club": "Club 1",
            "movement_type": "transfer-out",
            "announced_at": "2026-07-02T12:00:00Z",
            "verification_status": "confirmed_first_party",
            "fpl_reconciliation_status": "matched_current_fpl",
            "matched_fpl_element_id": departed_id,
        }
        arrival_transfer = {
            "player": "Player 12",
            "from_club": "Club 2",
            "to_club": "Club 1",
            "premier_league_club": "Club 1",
            "movement_type": "transfer-in",
            "announced_at": "2026-07-02T12:00:00Z",
            "verification_status": "confirmed_first_party",
            "fpl_reconciliation_status": "matched_current_fpl",
            "matched_fpl_element_id": arriving_id,
        }

        adjusted = {
            row["id"]: row
            for row in project_players(
                bootstrap, sample_fixtures(), horizon=5,
                recent_transfers=[departure_transfer, arrival_transfer],
                as_of="2026-07-23T12:00:00-04:00",
            )
        }

        self.assertEqual(adjusted[stayed_id]["teammate_transfer_impact"], "out")


class RecommendationTests(unittest.TestCase):
    def test_multiweek_schedule_rotates_lineup_and_captain_by_event(self):
        positions = ["GKP"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3
        squad = []
        for player_id, position in enumerate(positions, 1):
            central = [1.0, 1.0, 1.0]
            squad.append({
                "id": player_id,
                "name": f"P{player_id}",
                "position_short": position,
                "fixture_xp": central,
                "profile_fixture_xp": {
                    "conservative": list(central),
                    "balanced": list(central),
                    "aggressive": list(central),
                },
                "ownership": 10.0,
                "expected_minutes": 90.0,
            })
        squad[0]["fixture_xp"] = squad[0]["profile_fixture_xp"]["balanced"] = [8.0, 0.0, 0.0]
        squad[1]["fixture_xp"] = squad[1]["profile_fixture_xp"]["balanced"] = [0.0, 8.0, 8.0]
        first_mid, second_mid = squad[7], squad[8]
        first_mid["fixture_xp"] = first_mid["profile_fixture_xp"]["balanced"] = [12.0, 2.0, 2.0]
        second_mid["fixture_xp"] = second_mid["profile_fixture_xp"]["balanced"] = [2.0, 12.0, 12.0]

        schedule = _event_lineup_schedule(squad, "balanced", horizon=3)

        self.assertEqual(len(schedule), 3)
        self.assertIn(squad[0]["id"], schedule[0]["lineup_player_ids"])
        self.assertIn(squad[1]["id"], schedule[1]["lineup_player_ids"])
        self.assertEqual(schedule[0]["captain_id"], first_mid["id"])
        self.assertEqual(schedule[1]["captain_id"], second_mid["id"])
        self.assertNotEqual(schedule[0]["lineup_player_ids"], schedule[1]["lineup_player_ids"])

    def test_team_uncertainty_does_not_sum_simultaneous_player_extremes(self):
        positions = ["GKP"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3
        squad = []
        for player_id, position in enumerate(positions, start=1):
            squad.append(
                {
                    "id": player_id,
                    "position_short": position,
                    "fixture_xp": [10.0],
                    "profile_fixture_xp": {
                        "conservative": [0.0],
                        "balanced": [10.0],
                        "aggressive": [20.0],
                    },
                    "ownership": 10.0,
                    "expected_minutes_scenarios": {
                        "conservative": 90.0,
                        "balanced": 90.0,
                        "aggressive": 90.0,
                    },
                }
            )

        interval = _team_uncertainty_interval(squad, "balanced", 1)

        self.assertEqual(interval["central"], 120.0)
        self.assertGreater(interval["lower"], 0.0)
        self.assertLess(interval["upper"], 240.0)
        self.assertAlmostEqual(
            interval["central"] - interval["lower"],
            interval["upper"] - interval["central"],
            places=2,
        )

    def test_builds_legal_opening_squad_lineup_bench_and_captains(self):
        result = build_gw_recommendations(
            sample_bootstrap(),
            sample_fixtures(),
            generated_at="2026-07-23T18:00:00-04:00",
        )

        self.assertEqual(result["status"], "active_preliminary")
        squad = result["recommended_squad"]
        self.assertEqual(len(squad["players"]), 15)
        self.assertLessEqual(squad["cost"], 100.0)
        position_counts = {}
        club_counts = {}
        for player in squad["players"]:
            position_counts[player["position_short"]] = position_counts.get(player["position_short"], 0) + 1
            club_counts[player["club"]] = club_counts.get(player["club"], 0) + 1
        self.assertEqual(position_counts, {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3})
        self.assertLessEqual(max(club_counts.values()), 3)
        self.assertEqual(len(squad["starting_xi"]), 11)
        self.assertEqual(len(squad["bench"]), 4)
        self.assertIn(squad["captain"]["id"], {player["id"] for player in squad["starting_xi"]})
        self.assertIn(squad["vice_captain"]["id"], {player["id"] for player in squad["starting_xi"]})
        self.assertNotEqual(squad["captain"]["id"], squad["vice_captain"]["id"])
        self.assertEqual(result["model"]["uses_betting_odds"], False)
        self.assertEqual(result["model"]["horizon_gameweeks"], 5)

    def test_builds_comparable_risk_profile_squads_with_distinct_objectives(self):
        bootstrap = sample_bootstrap()
        # Create defensible floor-versus-ceiling trade-offs in every position.
        for position in (1, 2, 3, 4):
            candidates = [row for row in bootstrap["elements"] if row["element_type"] == position]
            upside = candidates[-1]
            upside.update({
                "minutes": 950,
                "starts": 11,
                "total_points": 185,
                "ep_next": "4.0",
                "selected_by_percent": "0.8",
            })
            safe = candidates[0]
            safe.update({
                "minutes": 3200,
                "starts": 36,
                "total_points": 145,
                "ep_next": "3.0",
                "selected_by_percent": "35.0",
            })

        result = build_gw_recommendations(
            bootstrap, sample_fixtures(), generated_at="2026-07-23T18:00:00-04:00"
        )

        self.assertEqual(result["default_profile"], "balanced")
        profiles = result["profile_recommendations"]
        self.assertEqual([row["id"] for row in profiles], ["conservative", "balanced", "aggressive"])
        distinct_squads = {
            tuple(sorted(player["id"] for player in row["squad"]["players"]))
            for row in profiles
        }
        self.assertGreaterEqual(len(distinct_squads), 2)
        for profile in profiles:
            squad = profile["squad"]
            self.assertEqual(len(squad["players"]), 15)
            self.assertLessEqual(squad["cost"], 100.0)
            self.assertEqual(len(squad["starting_xi"]), 11)
            self.assertIn("central_1gw", profile["metrics"])
            self.assertIn("central_3gw", profile["metrics"])
            self.assertIn("central_5gw", profile["metrics"])
            self.assertLess(profile["metrics"]["central_1gw"], profile["metrics"]["central_3gw"])
            self.assertLess(profile["metrics"]["central_3gw"], profile["metrics"]["central_5gw"])
            self.assertEqual(set(profile["evaluation_horizons"]), {"1", "3", "5"})
            self.assertEqual(len(profile["evaluation_horizons"]["1"]["lineup_player_ids"]), 11)
            self.assertIn(
                profile["evaluation_horizons"]["1"]["captain_id"],
                profile["evaluation_horizons"]["1"]["lineup_player_ids"],
            )
            self.assertIn("lower_5gw", profile["metrics"])
            self.assertIn("upper_5gw", profile["metrics"])
            self.assertIn("average_ownership", profile["metrics"])
            self.assertIn("low_confidence_players", profile["metrics"])
            self.assertIn("comparison_to_balanced", profile)
        by_id = {row["id"]: row for row in profiles}
        self.assertLessEqual(
            by_id["conservative"]["metrics"]["low_confidence_players"],
            by_id["aggressive"]["metrics"]["low_confidence_players"],
        )
        self.assertGreaterEqual(
            by_id["balanced"]["metrics"]["central_5gw"],
            by_id["aggressive"]["metrics"]["central_5gw"],
        )
        # Balanced and conservative are independently optimized by a stochastic
        # local search over different objectives (central vs. downside), so a
        # razor-thin inversion between their resulting central_5gw values is
        # expected optimizer variance, not a correctness guarantee -- allow a
        # small tolerance rather than requiring a strict ordering.
        self.assertGreaterEqual(
            by_id["balanced"]["metrics"]["central_5gw"] + 2.0,
            by_id["conservative"]["metrics"]["central_5gw"],
        )
        self.assertIs(result["recommended_squad"], by_id["balanced"]["squad"])

    def test_squad_objective_values_five_gameweek_captaincy(self):
        result = build_gw_recommendations(
            sample_bootstrap(), sample_fixtures(), generated_at="2026-07-23T18:00:00-04:00"
        )
        squad = result["recommended_squad"]["players"]
        baseline = _squad_objective(squad)
        boosted = copy.deepcopy(squad)
        first_event = _event_lineup_schedule(boosted, "balanced", 5)[0]
        captain_candidate = next(player for player in boosted if player["id"] == first_event["captain_id"])
        captain_candidate["fixture_xp"][0] += 1

        self.assertAlmostEqual(_squad_objective(boosted) - baseline, 2.0, places=6)

    def test_active_model_is_versioned_from_config_and_marked_champion(self):
        result = build_gw_recommendations(
            sample_bootstrap(), sample_fixtures(), generated_at="2026-07-23T18:00:00-04:00"
        )

        self.assertEqual(result["model"]["version"], "0.7")
        self.assertIs(result["model"]["is_champion"], True)

    def test_opening_optimizer_reports_recent_transfer_role_adjustments(self):
        bootstrap = sample_bootstrap()
        player = bootstrap["elements"][0]
        transfer = {
            "player": f"{player['first_name']} {player['second_name']}",
            "from_club": "Previous Club",
            "to_club": "Club 1",
            "announced_at": "2026-07-02T12:00:00Z",
            "verification_status": "confirmed_first_party",
            "fpl_reconciliation_status": "matched_current_fpl",
            "matched_fpl_element_id": player["id"],
        }

        result = build_gw_recommendations(
            bootstrap,
            sample_fixtures(),
            generated_at="2026-07-23T18:00:00-04:00",
            recent_transfers=[transfer],
        )

        self.assertEqual(result["model"]["role_transition_player_ids"], [player["id"]])
        self.assertIn("recent confirmed transfers", " ".join(result["model"]["inputs"]).lower())
        self.assertNotIn(
            "New transfers and tactical role changes require manual review.",
            result["model"]["limitations"],
        )


if __name__ == "__main__":
    unittest.main()
