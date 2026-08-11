import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fpl_intel.generation import publish_generation, resolve_artifact
from fpl_intel.refresh import (
    _merge_transfer_candidates,
    _record_actual_collection_attempt,
    compute_manager_view,
    refresh_project,
)
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

    def _refresh_with_shadow(self, root):
        (root / "data").mkdir()
        (root / "config").mkdir()
        (root / "data" / "confirmed-transfers.json").write_text(json.dumps({"transfers": []}), encoding="utf-8")
        (root / "config" / "sources.json").write_text(json.dumps({"sources": []}), encoding="utf-8")
        return refresh_project(
            root,
            bootstrap_payload=sample_bootstrap(),
            fixture_payload=sample_fixtures(),
            official_transfer_records=[],
            generated_at="2026-07-23T18:00:00-04:00",
        )

    def test_refresh_computes_and_archives_the_ml_minutes_shadow_forecast(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = self._refresh_with_shadow(root)

            self.assertIn("ml-minutes-ridge-v1", state["model_performance"]["shadow_models"])
            persisted = json.loads((root / "data" / "model-performance.json").read_text(encoding="utf-8"))
            shadow = persisted["shadow_forecasts"]["ml-minutes-ridge-v1"]["1"]
            self.assertGreater(len(shadow["players"]), 0)
            # Player forecasts are [modeled, lower, upper] triples, same shape as the
            # champion's own frozen player_forecasts (model_performance.py).
            first_player_forecast = next(iter(shadow["players"].values()))
            self.assertEqual(len(first_player_forecast), 3)

    def test_shadow_forecast_computation_never_changes_the_champion_recommendation(self):
        """Wiring the issue #65 shadow challenger into the refresh pipeline must be purely
        additive -- the champion's own recommended squad/xp is computed by
        build_gw_recommendations before the shadow hook ever runs, so it cannot be affected,
        but this locks that in as an explicit regression test."""
        with tempfile.TemporaryDirectory() as first_directory, tempfile.TemporaryDirectory() as second_directory:
            with_shadow_state = self._refresh_with_shadow(Path(first_directory))

            # A second, independent refresh with the exact same inputs -- if the shadow hook
            # ever mutated shared bootstrap/player data in place, the two runs would diverge.
            other_state = self._refresh_with_shadow(Path(second_directory))

        self.assertEqual(
            with_shadow_state["decision_center"]["recommended_squad"],
            other_state["decision_center"]["recommended_squad"],
        )
        self.assertEqual(
            with_shadow_state["decision_center"]["model"]["version"],
            other_state["decision_center"]["model"]["version"],
        )
        self.assertNotEqual(
            with_shadow_state["decision_center"]["model"]["version"], "ml-minutes-ridge-v1"
        )

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

    def test_refresh_applies_confirmed_risk_profile_as_default_profile(self):
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
            (root / "config" / "user-profile.json").write_text(
                json.dumps(
                    {
                        "manager": {
                            "team_id": 364759,
                            "confirmed_free_transfers": 3,
                            "confirmed_free_transfers_event": 2,
                            "risk_profile": "aggressive",
                        }
                    }
                ),
                encoding="utf-8",
            )

            state = refresh_project(
                root,
                bootstrap_payload=bootstrap,
                fixture_payload=fixtures,
                manager_payload=raw_manager,
                official_transfer_records=[],
                generated_at="2026-08-29T12:00:00-04:00",
            )

        self.assertEqual(state["decision_center"]["default_profile"], "aggressive")
        self.assertEqual(state["decision_center"]["weekly_decisions"]["default_profile"], "aggressive")

    def test_refresh_publishes_whitelisted_profile_fields_only(self):
        """`primary_goal` (issue #12's config/user-profile.json field, read by nothing in
        `src/`) must stay excluded from the whitelist. `goal` (issue #78's *different*,
        profiles.db-backed field -- unrelated despite the similar name) is a deliberate,
        wired addition to this same whitelist, so it's expected here, unlike `primary_goal`.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "config").mkdir()
            (root / "data" / "confirmed-transfers.json").write_text(json.dumps({"transfers": []}), encoding="utf-8")
            (root / "config" / "sources.json").write_text(json.dumps({"sources": []}), encoding="utf-8")
            (root / "config" / "user-profile.json").write_text(
                json.dumps(
                    {
                        "manager": {
                            "team_id": 364759,
                            "timezone": "America/New_York",
                            "confirmed_free_transfers": 2,
                            "confirmed_free_transfers_event": 3,
                            "risk_profile": "conservative",
                            "primary_goal": "overall_rank_below_50000",
                        }
                    }
                ),
                encoding="utf-8",
            )

            state = refresh_project(
                root,
                bootstrap_payload={"events": [], "elements": [], "teams": []},
                generated_at="2026-07-23T12:00:00-04:00",
            )

        self.assertEqual(
            set(state["profile"].keys()),
            {
                "team_id",
                "timezone",
                "confirmed_free_transfers",
                "confirmed_free_transfers_event",
                "risk_profile",
                "goal",
            },
        )
        self.assertNotIn("primary_goal", state["profile"])
        self.assertNotIn("primary_goal", json.dumps(state["profile"]))
        # No "goal" key was set in config/user-profile.json's "manager" object (only the
        # unrelated "primary_goal") -- so issue #78's default applies here.
        self.assertEqual(state["profile"]["goal"], "top_50k")

    def test_refresh_defaults_profile_when_no_profile_file_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "config").mkdir()
            (root / "data" / "confirmed-transfers.json").write_text(json.dumps({"transfers": []}), encoding="utf-8")
            (root / "config" / "sources.json").write_text(json.dumps({"sources": []}), encoding="utf-8")

            state = refresh_project(
                root,
                bootstrap_payload={"events": [], "elements": [], "teams": []},
                generated_at="2026-07-23T12:00:00-04:00",
            )

        self.assertEqual(state["profile"]["timezone"], "America/New_York")
        self.assertIsNone(state["profile"]["team_id"])

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
                manager_picks_payloads={364759: {1: picks}},
                generated_at="2026-08-22T12:00:00-04:00",
            )

            persisted = json.loads((root / "data" / "model-performance.json").read_text(encoding="utf-8"))
            self.assertIn("364759", persisted["manager_picks"])
            self.assertIn("1", persisted["manager_picks"]["364759"])
            self.assertEqual(persisted["manager_picks"]["364759"]["1"][0]["element_id"], 1)
            # team_performance/player_performance no longer live in refresh-time state (issue #64)
            # -- they're spliced in per request instead, see ServerTests/TeamModelPerformanceTests.
            self.assertNotIn("team_performance", state["model_performance"])
            self.assertNotIn("player_performance", state["model_performance"])

            from fpl_intel.model_performance import build_team_model_performance
            team_performance = build_team_model_performance(persisted, team_id=364759)["team_performance"]
            self.assertEqual(len(team_performance["comparisons"]), 1)
            comparison = team_performance["comparisons"][0]
            self.assertEqual(comparison["modeled_points"], 10.0)
            self.assertEqual(comparison["actual_points"], 6)


class VolumeShadowedSeedFilesRegressionTests(unittest.TestCase):
    """Regression coverage for the live Railway bug: a volume mounted at `data/` shadows the
    git-tracked seed files that used to live directly under it (`confirmed-transfers.json`,
    `official-transfers-latest.json`, `fpl-fixtures-latest.json`), leaving `data/` looking exactly
    like these tests' `root / "data"` -- present, but with none of those three files in it.

    These tests build that same "freshly mounted, nothing seeded" `data/` directly (no
    `data-seed/` involved -- that's the primary fix, covered separately in
    `tests/test_start_dashboard.py`) so they exercise the defense-in-depth fallback on its own and
    would have failed against the pre-fix code with a raw `FileNotFoundError`.
    """

    def _bootstrap(self):
        return {
            "events": [{"id": 1, "deadline_time": "2025-08-15T17:30:00Z"}],
            "elements": [{"id": 1}],
            "teams": [{"id": 1}],
        }

    def test_refresh_no_longer_raises_filenotfounderror_for_missing_confirmed_transfers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "config").mkdir()
            # Deliberately no data/confirmed-transfers.json -- simulates the shadowed volume.
            (root / "config" / "sources.json").write_text(
                json.dumps({"sources": []}), encoding="utf-8"
            )

            state = refresh_project(
                root, bootstrap_payload=self._bootstrap(), generated_at="2026-08-10T12:00:00Z"
            )

            self.assertEqual(state["transfers"], [])

    def test_missing_confirmed_transfers_falls_back_to_an_empty_transfers_list(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "config").mkdir()
            (root / "config" / "sources.json").write_text(
                json.dumps({"sources": []}), encoding="utf-8"
            )

            state = refresh_project(
                root, bootstrap_payload=self._bootstrap(), generated_at="2026-08-10T12:00:00Z"
            )

            self.assertEqual(state["transfer_summary"]["total"], 0)


class ManagerPicksMultiTeamCollectionTests(unittest.TestCase):
    """Issue #64: the refresh loop iterates every saved #45 profile's team, capped per run (C1)."""

    def _bootstrap(self):
        return {
            "events": [
                {"id": 1, "name": "Gameweek 1", "deadline_time": "2026-08-14T17:30:00Z", "finished": True},
                {"id": 2, "name": "Gameweek 2", "deadline_time": "2026-08-21T17:30:00Z", "is_next": True, "finished": False},
            ],
            "elements": [{"id": 1}],
            "teams": [{"id": 1}],
        }

    def _seed_root(self, directory, team_ids):
        from fpl_intel.profiles import save_profile

        root = Path(directory)
        (root / "data").mkdir()
        (root / "config").mkdir()
        (root / "data" / "confirmed-transfers.json").write_text(json.dumps({"transfers": []}), encoding="utf-8")
        (root / "config" / "sources.json").write_text(json.dumps({"sources": []}), encoding="utf-8")
        for team_id in team_ids:
            save_profile(
                root / "data" / "profiles.db", team_id=team_id, timezone="UTC",
                risk_profile="balanced", confirmed_free_transfers=None,
                confirmed_free_transfers_event=None, now="2026-08-08T00:00:00Z",
                goal="top_50k",
            )
        return root

    def test_refresh_collects_picks_for_every_saved_profiles_team(self):
        live = {"elements": [{"id": 1, "stats": {"total_points": 3}}]}
        picks = {"picks": [{"element": 1, "multiplier": 1, "is_captain": False, "is_vice_captain": False}]}
        with tempfile.TemporaryDirectory() as directory:
            root = self._seed_root(directory, [100, 200])

            state = refresh_project(
                root,
                bootstrap_payload=self._bootstrap(),
                event_live_payloads={1: live},
                manager_picks_payloads={100: {1: picks}, 200: {1: picks}},
                generated_at="2026-08-15T12:00:00-04:00",
            )

            persisted = json.loads((root / "data" / "model-performance.json").read_text(encoding="utf-8"))
            self.assertEqual(set(persisted["manager_picks"].keys()), {"100", "200"})
            self.assertEqual(persisted["manager_picks"]["100"]["1"][0]["element_id"], 1)
            self.assertEqual(persisted["manager_picks"]["200"]["1"][0]["element_id"], 1)
            self.assertNotIn("team_performance", state["model_performance"])

    def test_refresh_caps_the_number_of_teams_collected_in_one_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._seed_root(directory, [100, 200, 300])

            with patch("fpl_intel.refresh._MANAGER_PICKS_TEAM_CAP", 2), \
                 patch("fpl_intel.refresh.fetch_bootstrap", return_value=self._bootstrap()), \
                 patch("fpl_intel.refresh.fetch_fixtures", return_value=[]), \
                 patch("fpl_intel.refresh.fetch_manager_event_picks", return_value=None) as mock_fetch:
                live = {"elements": [{"id": 1, "stats": {"total_points": 3}}]}
                refresh_project(
                    root,
                    event_live_payloads={1: live},
                    generated_at="2026-08-15T12:00:00-04:00",
                )

            called_team_ids = sorted(call.args[0] for call in mock_fetch.call_args_list)
            self.assertEqual(called_team_ids, [100, 200])

            persisted = json.loads((root / "data" / "model-performance.json").read_text(encoding="utf-8"))
            self.assertEqual(set(persisted["manager_picks"].keys()), {"100", "200"})

    def test_teams_already_caught_up_do_not_consume_the_cap(self):
        """A team with every finished event's picks already collected costs nothing this run,
        leaving cap headroom for a team that still needs collecting."""
        with tempfile.TemporaryDirectory() as directory:
            root = self._seed_root(directory, [100, 200])
            (root / "data" / "model-performance.json").write_text(
                json.dumps({
                    "forecasts": [], "actual_events": {},
                    "manager_picks": {"100": {"1": [{"element_id": 1, "multiplier": 1, "is_captain": False}]}},
                }),
                encoding="utf-8",
            )

            with patch("fpl_intel.refresh._MANAGER_PICKS_TEAM_CAP", 1), \
                 patch("fpl_intel.refresh.fetch_bootstrap", return_value=self._bootstrap()), \
                 patch("fpl_intel.refresh.fetch_fixtures", return_value=[]), \
                 patch("fpl_intel.refresh.fetch_manager_event_picks", return_value=None) as mock_fetch:
                live = {"elements": [{"id": 1, "stats": {"total_points": 3}}]}
                refresh_project(
                    root,
                    event_live_payloads={1: live},
                    generated_at="2026-08-15T12:00:00-04:00",
                )

            called_team_ids = sorted(call.args[0] for call in mock_fetch.call_args_list)
            self.assertEqual(called_team_ids, [200])

    def test_refresh_migrates_pre_issue_64_flat_manager_picks_store(self):
        """A model-performance.json written before #64 has flat manager_picks ({event: picks})
        for whichever single team config/user-profile.json configured -- refresh migrates it to
        the per-team shape ({team_id: {event: picks}}) on load, using that same team ID."""
        with tempfile.TemporaryDirectory() as directory:
            root = self._seed_root(directory, [])
            (root / "config" / "user-profile.json").write_text(
                json.dumps({"manager": {"team_id": 364759}}), encoding="utf-8"
            )
            (root / "data" / "model-performance.json").write_text(
                json.dumps({
                    "forecasts": [], "actual_events": {},
                    "manager_picks": {"1": [{"element_id": 1, "multiplier": 1, "is_captain": False}]},
                }),
                encoding="utf-8",
            )
            live = {"elements": [{"id": 1, "stats": {"total_points": 3}}]}

            with patch("fpl_intel.refresh.collect_public_manager", return_value={
                "entry": {"id": 364759, "name": "Solo", "player_first_name": "Solo",
                          "player_last_name": "Manager", "current_event": None, "started_event": 1},
                "history": {"current": [], "past": [], "chips": []},
                "transfers": [], "picks": None,
            }):
                refresh_project(
                    root,
                    bootstrap_payload=self._bootstrap(),
                    event_live_payloads={1: live},
                    generated_at="2026-08-15T12:00:00-04:00",
                )

            persisted = json.loads((root / "data" / "model-performance.json").read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["manager_picks"]["364759"],
                {"1": [{"element_id": 1, "multiplier": 1, "is_captain": False}]},
            )

    def test_config_team_id_is_included_alongside_saved_profiles_without_duplication(self):
        picks = {"picks": [{"element": 1, "multiplier": 1, "is_captain": False, "is_vice_captain": False}]}
        with tempfile.TemporaryDirectory() as directory:
            root = self._seed_root(directory, [100])
            (root / "config" / "user-profile.json").write_text(
                json.dumps({"manager": {"team_id": 100}}), encoding="utf-8"
            )
            live = {"elements": [{"id": 1, "stats": {"total_points": 3}}]}

            with patch("fpl_intel.refresh._MANAGER_PICKS_TEAM_CAP", 5), \
                 patch("fpl_intel.refresh.collect_public_manager", return_value={
                     "entry": {"id": 100, "name": "Solo", "player_first_name": "Solo",
                               "player_last_name": "Manager", "current_event": None, "started_event": 1},
                     "history": {"current": [], "past": [], "chips": []},
                     "transfers": [], "picks": None,
                 }):
                refresh_project(
                    root,
                    bootstrap_payload=self._bootstrap(),
                    event_live_payloads={1: live},
                    manager_picks_payloads={100: {1: picks}},
                    generated_at="2026-08-15T12:00:00-04:00",
                )

            persisted = json.loads((root / "data" / "model-performance.json").read_text(encoding="utf-8"))
            self.assertEqual(set(persisted["manager_picks"].keys()), {"100"})


class ComputeManagerViewTests(unittest.TestCase):
    """The per-team half split out of _refresh_project_unlocked for issue #46."""

    def test_computes_weekly_decision_for_a_request_supplied_team_id(self):
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

        with patch("fpl_intel.refresh.collect_public_manager", return_value=raw_manager) as mock_collect:
            result = compute_manager_view(
                bootstrap, fixtures, transfers=[], generated_at="2026-08-29T12:00:00-04:00", team_id=364759,
            )

        mock_collect.assert_called_once_with(364759)
        self.assertEqual(result["manager"]["team_id"], 364759)
        self.assertEqual(result["manager"]["connection_status"], "connected")
        self.assertEqual(result["weekly_decisions"]["status"], "active")
        self.assertEqual(result["weekly_decisions"]["event"], 2)

    def test_unknown_or_unreachable_team_id_returns_a_clean_result_instead_of_raising(self):
        bootstrap, fixtures = sample_bootstrap(), sample_fixtures()

        with patch("fpl_intel.refresh.collect_public_manager", side_effect=OSError("not found")):
            result = compute_manager_view(
                bootstrap, fixtures, transfers=[], generated_at="2026-08-29T12:00:00-04:00", team_id=99999999,
            )

        self.assertEqual(result["manager"]["connection_status"], "lookup_failed")
        self.assertEqual(result["manager"]["team_id"], 99999999)
        self.assertEqual(result["manager"]["squad"], [])
        self.assertEqual(result["weekly_decisions"]["status"], "team_not_found")
        self.assertIn("reason", result["weekly_decisions"])

    def test_does_not_mutate_or_depend_on_a_persisted_profile(self):
        """This is the request-supplied-team-id path -- it must not read config/user-profile.json."""
        bootstrap, fixtures = sample_bootstrap(), sample_fixtures()

        with patch("fpl_intel.refresh.collect_public_manager", side_effect=OSError("unreachable")) as mock_collect:
            compute_manager_view(
                bootstrap, fixtures, transfers=[], generated_at="2026-08-29T12:00:00-04:00", team_id=42,
            )

        mock_collect.assert_called_once_with(42)

    def test_applies_a_saved_confirmed_free_transfers_override_before_computing_the_decision(self):
        """Issue #45: a per-team saved override, mirroring the config-file-driven equivalent."""
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

        with patch("fpl_intel.refresh.collect_public_manager", return_value=raw_manager):
            result = compute_manager_view(
                bootstrap, fixtures, transfers=[], generated_at="2026-08-29T12:00:00-04:00", team_id=364759,
                confirmed_free_transfers=3, confirmed_free_transfers_event=2,
            )

        self.assertEqual(result["weekly_decisions"]["free_transfers"], 3)
        self.assertEqual(result["weekly_decisions"]["free_transfer_source"], "confirmed_local")

    def test_confirmed_free_transfers_override_is_ignored_by_default(self):
        """Omitting the override keeps today's estimate-from-public-history behavior."""
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

        with patch("fpl_intel.refresh.collect_public_manager", return_value=raw_manager):
            result = compute_manager_view(
                bootstrap, fixtures, transfers=[], generated_at="2026-08-29T12:00:00-04:00", team_id=364759,
            )

        self.assertEqual(result["weekly_decisions"]["free_transfer_source"], "estimated_public_history")


if __name__ == "__main__":
    unittest.main()
