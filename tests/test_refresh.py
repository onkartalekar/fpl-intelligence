import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fpl_intel.generation import publish_generation, resolve_artifact
from fpl_intel.refresh import _merge_transfer_candidates, _record_actual_collection_attempt, refresh_project
from tests.test_recommendations import sample_bootstrap, sample_fixtures


class TransferProvenanceTests(unittest.TestCase):
    def test_duplicate_transfer_preserves_source_type_for_each_supporting_url(self):
        records = [
            {
                "player": "Example Player", "from_club": "Alpha", "to_club": "Arsenal",
                "announced_at": "2026-07-01T12:00:00Z",
                "source_url": "https://premierleague.com/news/example",
                "source_type": "official_premier_league",
            },
            {
                "player": "Example Player", "from_club": "Alpha", "to_club": "Arsenal",
                "announced_at": "2026-07-01T13:00:00Z",
                "source_url": "https://arsenal.com/news/example-player",
                "source_type": "official_club",
                "official_club_domain": "arsenal.com",
            },
        ]

        merged = _merge_transfer_candidates(records)

        self.assertEqual(len(merged), 1)
        self.assertEqual(
            {(item["url"], item["source_type"]) for item in merged[0]["supporting_sources"]},
            {
                ("https://premierleague.com/news/example", "official_premier_league"),
                ("https://arsenal.com/news/example-player", "official_club"),
            },
        )


class ActualCollectionHealthTests(unittest.TestCase):
    def test_failure_history_persists_after_a_later_success_without_raw_error_text(self):
        store = {}
        _record_actual_collection_attempt(store, 1, "2026-08-22T12:00:00Z", RuntimeError("/secret/path"))
        _record_actual_collection_attempt(store, 1, "2026-08-22T12:05:00Z", TimeoutError("private host"))
        _record_actual_collection_attempt(store, 1, "2026-08-22T12:10:00Z")

        health = store["actual_event_collection"]["1"]
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["attempt_count"], 3)
        self.assertEqual(health["failure_count"], 2)
        self.assertEqual(health["last_failure_at"], "2026-08-22T12:05:00Z")
        self.assertEqual(health["last_success_at"], "2026-08-22T12:10:00Z")
        self.assertNotIn("/secret/path", json.dumps(health))
        self.assertNotIn("private host", json.dumps(health))


class GenerationPublicationTests(unittest.TestCase):
    def test_failed_compatibility_publish_does_not_switch_authoritative_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            publish_generation(
                root,
                generated_at="2026-08-01T12:00:00Z",
                json_artifacts={"dashboard-state.json": {"generation": "old"}},
                dashboard_html="old dashboard",
            )
            pointer_before = (root / "data" / "current-generation.json").read_text(encoding="utf-8")

            from fpl_intel import generation
            original_write = generation.atomic_write_text

            resolved_root = root.resolve()

            def fail_root_dashboard(path, content):
                if Path(path) == resolved_root / "dashboard.html":
                    raise OSError("simulated publication failure")
                return original_write(path, content)

            with patch("fpl_intel.generation.atomic_write_text", side_effect=fail_root_dashboard):
                with self.assertRaises(OSError):
                    publish_generation(
                        root,
                        generated_at="2026-08-01T13:00:00Z",
                        json_artifacts={"dashboard-state.json": {"generation": "new"}},
                        dashboard_html="new dashboard",
                    )

            pointer_after = (root / "data" / "current-generation.json").read_text(encoding="utf-8")
            state_path = resolve_artifact(root, "dashboard-state.json")
            self.assertEqual(pointer_after, pointer_before)
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["generation"], "old")


class RefreshProjectTests(unittest.TestCase):
    def test_refresh_writes_state_snapshot_and_dashboard(self):
        bootstrap = {
            "events": [{"id": 1, "deadline_time": "2025-08-15T17:30:00Z"}],
            "elements": [{"id": 1}],
            "teams": [{"id": 1}],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "config").mkdir()
            (root / "data" / "confirmed-transfers.json").write_text(
                json.dumps({"transfers": []}), encoding="utf-8"
            )
            (root / "config" / "sources.json").write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Official FPL",
                                "url": "https://fantasy.premierleague.com/api/bootstrap-static/",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            state = refresh_project(
                root,
                bootstrap_payload=bootstrap,
                generated_at="2026-07-18T12:00:00-04:00",
            )

            self.assertEqual(state["fpl"]["season_status"], "prior_season_data")
            self.assertTrue((root / "data" / "dashboard-state.json").exists())
            self.assertTrue((root / "data" / "fpl-bootstrap-latest.json").exists())
            self.assertTrue((root / "dashboard.html").exists())

    def test_refresh_connects_configured_public_manager(self):
        bootstrap = {
            "events": [{"id": 1, "deadline_time": "2026-08-14T17:30:00Z"}],
            "elements": [],
            "teams": [],
        }
        manager_payload = {
            "entry": {
                "id": 364759, "name": "BrunoMans", "player_first_name": "Test",
                "player_last_name": "Manager", "current_event": None, "started_event": 1,
            },
            "history": {"current": [], "past": [], "chips": []},
            "transfers": [],
            "picks": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "config").mkdir()
            (root / "data" / "confirmed-transfers.json").write_text(json.dumps({"transfers": []}), encoding="utf-8")
            (root / "config" / "sources.json").write_text(json.dumps({"sources": []}), encoding="utf-8")
            (root / "config" / "user-profile.json").write_text(
                json.dumps({"manager": {"team_id": 364759}}), encoding="utf-8"
            )

            state = refresh_project(
                root,
                bootstrap_payload=bootstrap,
                manager_payload=manager_payload,
                generated_at="2026-07-22T12:00:00-04:00",
            )

            self.assertEqual(state["manager"]["team_id"], 364759)
            self.assertEqual(state["manager"]["team_name"], "BrunoMans")
            self.assertEqual(state["manager"]["connection_status"], "registered_preseason")
            self.assertTrue((root / "data" / "fpl-manager-latest.json").exists())

    def test_manager_failure_retains_previous_state_and_reports_stale_source(self):
        bootstrap = {
            "events": [{"id": 1, "deadline_time": "2026-08-14T17:30:00Z"}],
            "elements": [],
            "teams": [],
        }
        previous_manager = {
            "team_id": 364759,
            "team_name": "BrunoMans",
            "connection_status": "registered_preseason",
            "squad": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "config").mkdir()
            (root / "data" / "confirmed-transfers.json").write_text(json.dumps({"transfers": []}), encoding="utf-8")
            (root / "data" / "dashboard-state.json").write_text(json.dumps({"manager": previous_manager}), encoding="utf-8")
            (root / "config" / "sources.json").write_text(json.dumps({"sources": []}), encoding="utf-8")
            (root / "config" / "user-profile.json").write_text(json.dumps({"manager": {"team_id": 364759}}), encoding="utf-8")

            with patch("fpl_intel.refresh.collect_public_manager", side_effect=OSError("manager unavailable")):
                state = refresh_project(root, bootstrap_payload=bootstrap, generated_at="2026-07-25T12:00:00-04:00")

        self.assertEqual(state["manager"], previous_manager)
        self.assertEqual(state["source_health"]["manager"]["status"], "stale")
        self.assertEqual(state["source_health"]["manager"]["error"], "Manager source refresh failed")
        self.assertNotIn("manager unavailable", json.dumps(state))

    def test_refresh_merges_official_transfer_records(self):
        bootstrap = {
            "events": [{"id": 1, "deadline_time": "2025-08-15T17:30:00Z"}],
            "elements": [],
            "teams": [],
        }
        official = [
            {
                "player": "Example Player",
                "from_club": "Club A",
                "to_club": "Club B",
                "announced_at": "2026-07-18T12:00:00Z",
                "source_url": "https://www.premierleague.com/example",
                "source_type": "official_premier_league",
                "verification_status": "confirmed_first_party",
                "fpl_reconciliation_status": "pending_new_season_fpl",
                "movement_type": "transfer-in",
                "premier_league_club": "Club B",
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "config").mkdir()
            (root / "data" / "confirmed-transfers.json").write_text(
                json.dumps({"transfers": []}), encoding="utf-8"
            )
            (root / "config" / "sources.json").write_text(
                json.dumps({"sources": []}), encoding="utf-8"
            )

            state = refresh_project(
                root,
                bootstrap_payload=bootstrap,
                official_transfer_records=official,
                generated_at="2026-07-18T12:00:00-04:00",
            )

            self.assertEqual(len(state["transfers"]), 1)
            self.assertEqual(state["transfers"][0]["player"], "Example Player")
            self.assertEqual(state["transfers"][0]["fpl_relevance"], "medium")
            self.assertEqual(state["transfer_summary"]["medium"], 1)
            self.assertEqual(state["club_summaries"][0]["club"], "Club B")

    def test_refresh_publishes_current_player_prices_and_fixtures(self):
        bootstrap = {
            "events": [{"id": 1, "name": "Gameweek 1", "deadline_time": "2026-08-21T17:30:00Z", "is_next": True}],
            "teams": [
                {"id": 1, "name": "Arsenal", "short_name": "ARS"},
                {"id": 7, "name": "Chelsea", "short_name": "CHE"},
            ],
            "element_types": [{"id": 1, "singular_name": "Goalkeeper", "singular_name_short": "GKP"}],
            "elements": [{
                "id": 1, "first_name": "David", "second_name": "Raya", "web_name": "Raya",
                "team": 1, "element_type": 1, "now_cost": 60,
                "selected_by_percent": "26.9", "status": "a", "news": "",
                "form": "0.0", "total_points": 0, "minutes": 0, "starts": 0,
            }],
        }
        fixtures = [{
            "id": 1, "event": 1, "team_h": 1, "team_a": 7,
            "kickoff_time": "2026-08-21T19:00:00Z",
            "team_h_difficulty": 2, "team_a_difficulty": 5,
            "finished": False, "started": False,
            "team_h_score": None, "team_a_score": None,
        }]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "config").mkdir()
            (root / "data" / "confirmed-transfers.json").write_text(
                json.dumps({"transfers": []}), encoding="utf-8"
            )
            (root / "config" / "sources.json").write_text(
                json.dumps({"sources": []}), encoding="utf-8"
            )

            state = refresh_project(
                root,
                bootstrap_payload=bootstrap,
                fixture_payload=fixtures,
                official_transfer_records=[],
                generated_at="2026-07-23T13:00:00-04:00",
            )

            self.assertEqual(state["players"][0]["price"], 6.0)
            self.assertEqual(state["fixtures"][0]["home_team"], "Arsenal")
            self.assertEqual(state["fixture_summary"]["fixture_count"], 1)
            self.assertTrue((root / "data" / "fpl-fixtures-latest.json").exists())

    def test_refresh_publishes_gw1_recommendations_from_complete_current_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "config").mkdir()
            (root / "data" / "confirmed-transfers.json").write_text(
                json.dumps({"transfers": []}), encoding="utf-8"
            )
            (root / "config" / "sources.json").write_text(
                json.dumps({"sources": []}), encoding="utf-8"
            )

            state = refresh_project(
                root,
                bootstrap_payload=sample_bootstrap(),
                fixture_payload=sample_fixtures(),
                official_transfer_records=[],
                generated_at="2026-07-23T18:00:00-04:00",
            )

        self.assertEqual(state["decision_center"]["status"], "active_preliminary")
        self.assertEqual(len(state["decision_center"]["recommended_squad"]["players"]), 15)
        self.assertEqual(state["decision_center"]["event"], 1)
        self.assertEqual(state["decision_center"]["weekly_decisions"]["status"], "waiting_for_gw2")

    def test_refresh_feeds_recent_confirmed_transfers_into_projection_model(self):
        bootstrap = sample_bootstrap()
        player = bootstrap["elements"][0]
        transfer = {
            "player": f"{player['first_name']} {player['second_name']}",
            "from_club": "Previous Club",
            "to_club": "Club 1",
            "announced_at": "2026-07-02T12:00:00Z",
            "source_url": "https://www.premierleague.com/confirmed-transfer",
            "source_type": "official_premier_league",
            "verification_status": "confirmed_first_party",
            "movement_type": "transfer-in",
            "premier_league_club": "Club 1",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "config").mkdir()
            (root / "data" / "confirmed-transfers.json").write_text(
                json.dumps({"transfers": []}), encoding="utf-8"
            )
            (root / "config" / "sources.json").write_text(
                json.dumps({"sources": []}), encoding="utf-8"
            )

            state = refresh_project(
                root,
                bootstrap_payload=bootstrap,
                fixture_payload=sample_fixtures(),
                official_transfer_records=[transfer],
                generated_at="2026-07-23T18:00:00-04:00",
            )

        self.assertEqual(
            state["decision_center"]["model"]["role_transition_player_ids"],
            [player["id"]],
        )
        self.assertEqual(state["transfers"][0]["matched_fpl_element_id"], player["id"])

    def test_refresh_publishes_gw2_roll_transfer_and_chip_decisions(self):
        from tests.test_transfer_decisions import gw2_inputs

        bootstrap, fixtures, manager = gw2_inputs()
        raw_manager = {
            "entry": {
                "id": 364759, "name": "BrunoMans", "player_first_name": "Test",
                "player_last_name": "Manager", "current_event": 1, "started_event": 1,
            },
            "history": {"current": [], "past": [], "chips": []},
            "transfers": [],
            "picks": {
                "active_chip": None,
                "entry_history": {"event": 1, "bank": 0, "value": 1000},
                "picks": [
                    {
                        "element": row["element_id"], "position": index + 1,
                        "multiplier": 1 if index < 11 else 0,
                        "is_captain": index == 0, "is_vice_captain": index == 1,
                        "purchase_price": row["purchase_price"],
                        "selling_price": row["selling_price"],
                    }
                    for index, row in enumerate(manager["squad"])
                ],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "config").mkdir()
            (root / "data" / "confirmed-transfers.json").write_text(json.dumps({"transfers": []}), encoding="utf-8")
            (root / "config" / "sources.json").write_text(json.dumps({"sources": []}), encoding="utf-8")
            (root / "config" / "user-profile.json").write_text(json.dumps({"manager": {"team_id": 364759, "confirmed_free_transfers": 3, "confirmed_free_transfers_event": 2}}), encoding="utf-8")

            state = refresh_project(
                root,
                bootstrap_payload=bootstrap,
                fixture_payload=fixtures,
                manager_payload=raw_manager,
                official_transfer_records=[],
                generated_at="2026-08-29T12:00:00-04:00",
            )

        weekly = state["decision_center"]["weekly_decisions"]
        self.assertEqual(weekly["status"], "active")
        self.assertEqual(weekly["event"], 2)
        self.assertEqual(weekly["free_transfers"], 3)
        self.assertEqual(weekly["free_transfer_source"], "confirmed_local")
        self.assertEqual(len(weekly["profiles"]), 3)
        self.assertIn("chip_recommendation", weekly["profiles"][1])

    def test_refresh_deduplicates_same_move_across_club_aliases(self):
        bootstrap = {"events": [], "elements": [], "teams": []}
        base = {
            "player": "Example Player",
            "announced_at": "2026-07-18T12:00:00Z",
            "source_type": "official_premier_league",
            "verification_status": "confirmed_first_party",
            "fpl_reconciliation_status": "pending_new_season_fpl",
        }
        official = [
            {**base, "from_club": "Spurs", "to_club": "Brighton", "source_url": "https://www.premierleague.com/spurs/a"},
            {**base, "from_club": "Tottenham Hotspur", "to_club": "Brighton & Hove Albion", "source_url": "https://www.premierleague.com/brighton/b"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "config").mkdir()
            (root / "data" / "confirmed-transfers.json").write_text(json.dumps({"transfers": []}), encoding="utf-8")
            (root / "config" / "sources.json").write_text(json.dumps({"sources": []}), encoding="utf-8")

            state = refresh_project(root, bootstrap_payload=bootstrap, official_transfer_records=official)

            self.assertEqual(len(state["transfers"]), 1)

    def test_refresh_reports_changes_since_previous_snapshot(self):
        previous_bootstrap = {
            "events": [],
            "teams": [{"id": 1}, {"id": 2}],
            "elements": [{"id": 1, "first_name": "Known", "second_name": "Player", "web_name": "Known", "team": 1, "status": "a"}],
        }
        current_bootstrap = {
            "events": [],
            "teams": [{"id": 1}, {"id": 2}],
            "elements": [
                {"id": 1, "first_name": "Known", "second_name": "Player", "web_name": "Known", "team": 2, "status": "d"},
                {"id": 2, "first_name": "New", "second_name": "Player", "web_name": "New", "team": 1, "status": "a"},
            ],
        }
        old_move = {
            "player": "Old Move", "from_club": "Ajax", "to_club": "Arsenal",
            "announced_at": "2026-07-10T12:00:00Z", "source_url": "https://www.premierleague.com/old",
            "source_type": "official_premier_league", "movement_type": "transfer-in",
            "premier_league_club": "Arsenal",
        }
        new_move = {
            "player": "New Move", "from_club": "Roma", "to_club": "Arsenal",
            "announced_at": "2026-07-18T12:00:00Z", "source_url": "https://www.premierleague.com/new",
            "source_type": "official_premier_league", "movement_type": "transfer-in",
            "premier_league_club": "Arsenal",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "config").mkdir()
            (root / "data" / "confirmed-transfers.json").write_text(json.dumps({"transfers": []}), encoding="utf-8")
            (root / "config" / "sources.json").write_text(json.dumps({"sources": []}), encoding="utf-8")
            (root / "data" / "fpl-bootstrap-latest.json").write_text(json.dumps(previous_bootstrap), encoding="utf-8")
            (root / "data" / "dashboard-state.json").write_text(json.dumps({"transfers": [old_move]}), encoding="utf-8")

            state = refresh_project(
                root,
                bootstrap_payload=current_bootstrap,
                official_transfer_records=[old_move, new_move],
                generated_at="2026-07-18T12:00:00Z",
            )

            changes = state["changes_since_last_refresh"]
            self.assertEqual(changes["new_confirmed_transfers"], 1)
            self.assertEqual(changes["new_fpl_players"], 1)
            self.assertEqual(changes["club_mapping_changes"], 1)
            self.assertEqual(changes["availability_changes"], 1)

    def test_refresh_ingests_finished_event_points_for_model_performance(self):
        bootstrap = {
            "events": [
                {"id": 1, "name": "Gameweek 1", "deadline_time": "2026-08-14T17:30:00Z", "finished": True},
                {"id": 2, "name": "Gameweek 2", "deadline_time": "2026-08-21T17:30:00Z", "is_next": True, "finished": False},
            ],
            "elements": [{"id": 1}],
            "teams": [{"id": 1}],
        }
        performance_store = {
            "forecasts": [{
                "origin_event": 1,
                "forecast_id": "gw1:0.3",
                "generated_at": "2026-08-13T12:00:00-04:00",
                "model_version": "0.3",
                "profiles": [{
                    "profile_id": "balanced", "label": "Balanced",
                    "horizons": {"1": {
                        "modeled_points": 5.0, "lower_points": 2.0, "upper_points": 8.0,
                        "lineup_player_ids": [1], "captain_id": 1,
                    }},
                }],
            }],
            "champion_forecasts": {"1": "gw1:0.3"},
            "actual_events": {},
        }
        live = {"elements": [{"id": 1, "stats": {"total_points": 3}}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "config").mkdir()
            (root / "data" / "confirmed-transfers.json").write_text(json.dumps({"transfers": []}), encoding="utf-8")
            (root / "data" / "model-performance.json").write_text(json.dumps(performance_store), encoding="utf-8")
            (root / "config" / "sources.json").write_text(json.dumps({"sources": []}), encoding="utf-8")

            state = refresh_project(
                root,
                bootstrap_payload=bootstrap,
                event_live_payloads={1: live},
                generated_at="2026-08-22T12:00:00-04:00",
            )

            self.assertEqual(state["model_performance"]["completed_comparisons"], 1)
            comparison = state["model_performance"]["comparisons"][0]
            self.assertEqual(comparison["modeled_points"], 5.0)
            self.assertEqual(comparison["actual_points"], 6)
            persisted = json.loads((root / "data" / "model-performance.json").read_text(encoding="utf-8"))
            self.assertIn("1", persisted["actual_events"])
            self.assertNotIn("manager_picks", persisted)

    def test_refresh_backfills_manager_picks_and_scores_team_performance_when_configured(self):
        bootstrap = {
            "events": [
                {"id": 1, "name": "Gameweek 1", "deadline_time": "2026-08-14T17:30:00Z", "finished": True},
                {"id": 2, "name": "Gameweek 2", "deadline_time": "2026-08-21T17:30:00Z", "is_next": True, "finished": False},
            ],
            "elements": [{"id": 1}],
            "teams": [{"id": 1}],
        }
        performance_store = {
            "forecasts": [{
                "origin_event": 1,
                "forecast_id": "gw1:0.3",
                "generated_at": "2026-08-13T12:00:00-04:00",
                "model_version": "0.3",
                "profiles": [{
                    "profile_id": "balanced", "label": "Balanced",
                    "horizons": {"1": {
                        "modeled_points": 5.0, "lower_points": 2.0, "upper_points": 8.0,
                        "lineup_player_ids": [1], "captain_id": 1,
                    }},
                }],
            }],
            "champion_forecasts": {"1": "gw1:0.3"},
            "actual_events": {},
            "player_forecasts": {
                "1": {
                    "forecast_id": "gw1:0.3", "model_version": "0.3",
                    "generated_at": "2026-08-13T12:00:00-04:00",
                    "players": {"1": [5.0, 2.0, 8.0]},
                }
            },
        }
        live = {"elements": [{"id": 1, "stats": {"total_points": 3}}]}
        picks = {"picks": [{"element": 1, "multiplier": 2, "is_captain": True, "is_vice_captain": False}]}
        manager_payload = {
            "entry": {
                "id": 364759, "name": "BrunoMans", "player_first_name": "Test",
                "player_last_name": "Manager", "current_event": None, "started_event": 1,
            },
            "history": {"current": [], "past": [], "chips": []},
            "transfers": [],
            "picks": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "config").mkdir()
            (root / "data" / "confirmed-transfers.json").write_text(json.dumps({"transfers": []}), encoding="utf-8")
            (root / "data" / "model-performance.json").write_text(json.dumps(performance_store), encoding="utf-8")
            (root / "config" / "sources.json").write_text(json.dumps({"sources": []}), encoding="utf-8")
            (root / "config" / "user-profile.json").write_text(
                json.dumps({"manager": {"team_id": 364759}}), encoding="utf-8"
            )

            state = refresh_project(
                root,
                bootstrap_payload=bootstrap,
                manager_payload=manager_payload,
                event_live_payloads={1: live},
                manager_picks_payloads={1: picks},
                generated_at="2026-08-22T12:00:00-04:00",
            )

            persisted = json.loads((root / "data" / "model-performance.json").read_text(encoding="utf-8"))
            self.assertIn("1", persisted["manager_picks"])
            self.assertEqual(persisted["manager_picks"]["1"][0]["element_id"], 1)
            self.assertEqual(len(state["model_performance"]["team_performance"]["comparisons"]), 1)
            comparison = state["model_performance"]["team_performance"]["comparisons"][0]
            self.assertEqual(comparison["modeled_points"], 10.0)
            self.assertEqual(comparison["actual_points"], 6)


if __name__ == "__main__":
    unittest.main()
