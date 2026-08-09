import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fpl_intel.profiles import load_profile, set_lookup_opt_out
from fpl_intel.refresh import RefreshAlreadyRunning, project_refresh_lock
from fpl_intel.server import _default_refresh_action, build_refresh_result, create_server


class RefreshResultTests(unittest.TestCase):
    def test_reports_source_health_without_claiming_inactive_fixtures(self):
        state = {
            "generated_at": "2026-07-19T12:00:00Z",
            "transfers": [{"player": "One"}],
            "fpl": {"season_status": "target_season_ready"},
        }

        result = build_refresh_result(state)

        self.assertEqual(result["source_statuses"]["fpl"], "ok")
        self.assertEqual(result["source_statuses"]["transfers"], "ok")
        self.assertEqual(result["source_statuses"]["fixtures"], "not_active")

    def test_reports_available_fixture_source(self):
        result = build_refresh_result(
            {
                "generated_at": "2026-07-23T13:00:00-04:00",
                "transfers": [],
                "fixtures": [{"id": 1}],
                "fixture_summary": {"status": "ready", "fixture_count": 1},
                "fpl": {"season_status": "target_season_ready"},
            }
        )

        self.assertEqual(result["source_statuses"]["fixtures"], "ok")

    def test_reports_connected_manager_source(self):
        result = build_refresh_result(
            {
                "generated_at": "2026-07-22T12:00:00-04:00",
                "transfers": [],
                "fpl": {"season_status": "target_season_ready"},
                "manager": {"connection_status": "registered_preseason"},
            }
        )

        self.assertEqual(result["source_statuses"]["manager"], "ok")

    def test_reports_degraded_source_names_without_raw_internal_errors(self):
        result = build_refresh_result(
            {
                "generated_at": "2026-07-22T12:00:00-04:00",
                "transfers": [],
                "fpl": {"season_status": "target_season_ready"},
                "source_health": {
                    "transfers": {
                        "status": "stale",
                        "error": "/Users/example/private/path: upstream failed",
                    }
                },
            }
        )

        self.assertEqual(result["degraded_sources"], ["transfers"])
        self.assertNotIn("source_errors", result)
        self.assertNotIn("private/path", json.dumps(result))


class DefaultRefreshActionTests(unittest.TestCase):
    def test_real_refresh_script_exits_busy_without_network_or_internal_paths_when_lock_is_held(self):
        root = Path(__file__).resolve().parents[1]

        with project_refresh_lock(root):
            completed = subprocess.run(
                [sys.executable, str(root / "scripts" / "refresh_dashboard.py")],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=10,
            )

        self.assertEqual(completed.returncode, 75)
        self.assertEqual(completed.stderr.strip(), "A dashboard refresh is already running.")
        self.assertNotIn(str(root), completed.stdout + completed.stderr)

    def test_maps_refresh_script_busy_exit_to_domain_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts" / "refresh_dashboard.py").write_text("pass\n", encoding="utf-8")
            completed = type("Completed", (), {"returncode": 75, "stderr": "busy", "stdout": ""})()

            with patch("fpl_intel.server.subprocess.run", return_value=completed):
                with self.assertRaises(RefreshAlreadyRunning):
                    _default_refresh_action(root)

    def test_runs_refresh_script_in_a_fresh_python_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "data").mkdir()
            state = {
                "generated_at": "2026-07-23T13:20:00-04:00",
                "transfers": [],
                "fpl": {"season_status": "target_season_ready"},
                "fixture_summary": {"status": "ready"},
                "manager": {"connection_status": "registered_preseason"},
            }
            (root / "scripts" / "refresh_dashboard.py").write_text(
                "from pathlib import Path\n"
                "import json\n"
                f"state = {state!r}\n"
                "Path('data/dashboard-state.json').write_text(json.dumps(state))\n"
                "Path('fresh-process-marker').write_text('fresh')\n",
                encoding="utf-8",
            )

            with patch(
                "fpl_intel.refresh.refresh_project",
                side_effect=AssertionError("in-process refresh must not run"),
            ):
                result = _default_refresh_action(root)

            self.assertEqual((root / "fresh-process-marker").read_text(), "fresh")
            self.assertEqual(result["fpl_status"], "target_season_ready")


class DashboardServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "dashboard.html").write_text(
            '<meta name="refresh-token" content="__REFRESH_TOKEN__"><h1>Dashboard</h1>',
            encoding="utf-8",
        )
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-07-18T12:00:00Z"}), encoding="utf-8"
        )
        self.calls = []

        def refresh_action():
            self.calls.append("refresh")
            return {
                "generated_at": "2026-07-19T12:00:00Z",
                "confirmed_movements": 7,
                "fpl_status": "target_season_ready",
            }

        self.server = create_server(
            self.root,
            host="127.0.0.1",
            port=0,
            token="test-token",
            refresh_action=refresh_action,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def test_dashboard_injects_server_token(self):
        html = urlopen(self.base_url + "/dashboard.html", timeout=3).read().decode()

        self.assertIn('content="test-token"', html)
        self.assertNotIn("__REFRESH_TOKEN__", html)

    def test_rejects_untrusted_host_before_serving_token_bearing_dashboard(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.putrequest("GET", "/dashboard.html", skip_host=True)
        connection.putheader("Host", "attacker.example")
        connection.endheaders()
        response = connection.getresponse()
        body = response.read().decode()
        connection.close()

        self.assertEqual(response.status, 421)
        self.assertNotIn("test-token", body)

    def test_refresh_rejects_cross_origin_request_even_with_valid_token(self):
        request = Request(
            self.base_url + "/api/refresh",
            data=b"{}",
            method="POST",
            headers={
                "X-Refresh-Token": "test-token",
                "Content-Type": "application/json",
                "Origin": "https://attacker.example",
            },
        )

        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)

        self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.calls, [])

    def test_refresh_rejects_missing_token(self):
        request = Request(self.base_url + "/api/refresh", data=b"{}", method="POST")

        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)

        self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.calls, [])

    def test_refresh_rejects_malformed_content_length_with_controlled_400(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(
            "POST",
            "/api/refresh",
            body=b"",
            headers={"X-Refresh-Token": "test-token", "Content-Length": "not-a-number"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()

        self.assertEqual(response.status, 400)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(self.calls, [])

    def test_server_rejects_non_loopback_binding(self):
        with self.assertRaises(ValueError):
            create_server(self.root, host="0.0.0.0", port=0, token="test-token")

    def test_cross_process_refresh_contention_returns_stable_busy_response(self):
        busy_server = create_server(
            self.root,
            host="127.0.0.1",
            port=0,
            token="test-token",
            refresh_action=lambda: (_ for _ in ()).throw(
                BlockingIOError("another process holds /private/path/.refresh.lock")
            ),
        )
        thread = threading.Thread(target=busy_server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{busy_server.server_port}/api/refresh",
                data=b"{}",
                method="POST",
                headers={"X-Refresh-Token": "test-token", "Content-Type": "application/json"},
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)
            payload = json.loads(error.exception.read())

            self.assertEqual(error.exception.code, 409)
            self.assertEqual(payload, {"status": "busy", "message": "A refresh is already running"})
        finally:
            busy_server.shutdown()
            busy_server.server_close()
            thread.join(timeout=2)

    def test_refresh_returns_generic_browser_error_without_internal_details(self):
        failing_server = create_server(
            self.root,
            host="127.0.0.1",
            port=0,
            token="test-token",
            refresh_action=lambda: (_ for _ in ()).throw(
                RuntimeError("/Users/example/private/path: upstream secret detail")
            ),
        )
        thread = threading.Thread(target=failing_server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{failing_server.server_port}/api/refresh",
                data=b"{}",
                method="POST",
                headers={"X-Refresh-Token": "test-token", "Content-Type": "application/json"},
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)
            payload = json.loads(error.exception.read())

            self.assertEqual(error.exception.code, 500)
            self.assertEqual(payload["message"], "Dashboard refresh failed")
            self.assertNotIn("private/path", json.dumps(payload))
        finally:
            failing_server.shutdown()
            failing_server.server_close()
            thread.join(timeout=2)

    def test_refresh_runs_once_and_returns_json(self):
        request = Request(
            self.base_url + "/api/refresh",
            data=b"{}",
            method="POST",
            headers={"X-Refresh-Token": "test-token", "Content-Type": "application/json"},
        )

        response = urlopen(request, timeout=3)
        payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["confirmed_movements"], 7)
        self.assertEqual(self.calls, ["refresh"])


class TeamLookupTests(unittest.TestCase):
    """The unauthenticated no-signup lookup path added for issue #46."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "dashboard.html").write_text(
            '<meta name="refresh-token" content="__REFRESH_TOKEN__"><h1>Dashboard</h1>',
            encoding="utf-8",
        )
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-07-18T12:00:00Z", "profile": {"team_id": None}}),
            encoding="utf-8",
        )
        self.lookup_calls = []

        def team_view_action(team_id):
            self.lookup_calls.append(team_id)
            return {
                "manager": {"connection_status": "connected", "team_id": team_id, "team_name": "BrunoMans", "squad": []},
                "weekly_decisions": {"status": "waiting_for_gw2", "event": 1},
            }

        self.team_view_action = team_view_action
        self.server = create_server(
            self.root,
            host="127.0.0.1",
            port=0,
            token="test-token",
            team_view_action=team_view_action,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def test_valid_team_id_splices_the_looked_up_manager_into_the_rendered_page(self):
        html = urlopen(self.base_url + "/?team_id=364759", timeout=3).read().decode()

        self.assertEqual(self.lookup_calls, [364759])
        self.assertIn("BrunoMans", html)
        self.assertIn('content="test-token"', html)
        self.assertNotIn('content="__REFRESH_TOKEN__"', html)
        self.assertNotIn("__DASHBOARD_DATA__", html)

    def test_absent_team_id_serves_the_shared_cached_dashboard_unmodified(self):
        html = urlopen(self.base_url + "/dashboard.html", timeout=3).read().decode()

        self.assertEqual(self.lookup_calls, [])
        self.assertIn("<h1>Dashboard</h1>", html)

    def test_malformed_team_id_falls_back_to_the_shared_cached_dashboard(self):
        html = urlopen(self.base_url + "/?team_id=not-a-number", timeout=3).read().decode()

        self.assertEqual(self.lookup_calls, [])
        self.assertIn("<h1>Dashboard</h1>", html)

    def test_out_of_range_team_id_falls_back_to_the_shared_cached_dashboard(self):
        html = urlopen(self.base_url + "/?team_id=999999999", timeout=3).read().decode()

        self.assertEqual(self.lookup_calls, [])
        self.assertIn("<h1>Dashboard</h1>", html)

    def test_repeated_lookups_from_the_same_source_are_rate_limited(self):
        first = urlopen(self.base_url + "/?team_id=364759", timeout=3)
        self.assertEqual(first.status, 200)

        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/?team_id=100001", timeout=3)

        self.assertEqual(error.exception.code, 429)
        self.assertEqual(self.lookup_calls, [364759])

    def test_lookup_failure_is_reported_cleanly_instead_of_a_server_error(self):
        failing_server = create_server(
            self.root,
            host="127.0.0.1",
            port=0,
            token="test-token",
            team_view_action=lambda team_id: (_ for _ in ()).throw(RuntimeError("upstream down")),
        )
        thread = threading.Thread(target=failing_server.serve_forever, daemon=True)
        thread.start()
        try:
            html = urlopen(
                f"http://127.0.0.1:{failing_server.server_port}/?team_id=364759", timeout=3
            ).read().decode()

            self.assertIn('"status": "error"', html)
            self.assertNotIn("upstream down", html)
        finally:
            failing_server.shutdown()
            failing_server.server_close()
            thread.join(timeout=2)

    def test_missing_dashboard_state_returns_404_for_a_lookup(self):
        (self.root / "data" / "dashboard-state.json").unlink()

        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/?team_id=364759", timeout=3)

        self.assertEqual(error.exception.code, 404)


class LookupOptOutGateTests(unittest.TestCase):
    """Issue #62: an opted-out team blocks the explicit ?team_id= lookup before any live call."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "dashboard.html").write_text(
            '<meta name="refresh-token" content="__REFRESH_TOKEN__"><h1>Dashboard</h1>',
            encoding="utf-8",
        )
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-07-18T12:00:00Z", "profile": {"team_id": None}}),
            encoding="utf-8",
        )
        self.db_path = self.root / "data" / "profiles.db"
        self.lookup_calls = []

        def team_view_action(team_id):
            self.lookup_calls.append(team_id)
            return {
                "manager": {"connection_status": "connected", "team_id": team_id, "team_name": "BrunoMans", "squad": []},
                "weekly_decisions": {"status": "waiting_for_gw2", "event": 1},
            }

        self.team_view_action = team_view_action
        self.server = create_server(
            self.root,
            host="127.0.0.1",
            port=0,
            token="test-token",
            team_view_action=team_view_action,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def test_opted_out_team_blocks_the_lookup_without_calling_the_live_action(self):
        set_lookup_opt_out(
            self.db_path, team_id=364759, opted_out=True, pin_hash="some-hash",
            now="2026-08-08T00:00:00Z",
        )

        html = urlopen(self.base_url + "/?team_id=364759", timeout=3).read().decode()

        self.assertEqual(self.lookup_calls, [])
        self.assertIn('"status": "opted_out"', html)
        self.assertIn('"team_id": 364759', html)
        self.assertNotIn("BrunoMans", html)

    def test_non_opted_out_team_still_looks_up_normally(self):
        set_lookup_opt_out(
            self.db_path, team_id=364759, opted_out=False, pin_hash="some-hash",
            now="2026-08-08T00:00:00Z",
        )

        html = urlopen(self.base_url + "/?team_id=364759", timeout=3).read().decode()

        self.assertEqual(self.lookup_calls, [364759])
        self.assertIn('"status": "ok"', html)
        self.assertIn("BrunoMans", html)

    def test_team_with_no_saved_profile_still_looks_up_normally(self):
        html = urlopen(self.base_url + "/?team_id=999999", timeout=3).read().decode()

        self.assertEqual(self.lookup_calls, [999999])
        self.assertIn('"status": "ok"', html)


class CookieResolvedTeamTests(unittest.TestCase):
    """Issue #45: a saved-team cookie is a second source for the per-request team view."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "dashboard.html").write_text(
            '<meta name="refresh-token" content="__REFRESH_TOKEN__"><h1>Dashboard</h1>',
            encoding="utf-8",
        )
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-07-18T12:00:00Z"}), encoding="utf-8"
        )
        self.lookup_calls = []
        self.profile_read_calls = []

        def team_view_action(team_id):
            self.lookup_calls.append(team_id)
            return {
                "manager": {"connection_status": "connected", "team_id": team_id, "team_name": "Cookie Team", "squad": []},
                "weekly_decisions": {"status": "waiting_for_gw2", "event": 1},
            }

        def profile_read_action(team_id):
            self.profile_read_calls.append(team_id)
            return {
                "team_id": team_id, "timezone": "UTC", "confirmed_free_transfers": None,
                "confirmed_free_transfers_event": None, "risk_profile": "aggressive",
            }

        self.server = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
            team_view_action=team_view_action, profile_read_action=profile_read_action,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.db_path = self.root / "data" / "profiles.db"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def _get_with_cookie(self, path, cookie_value):
        request = Request(self.base_url + path, headers={"Cookie": f"fpl_team_id={cookie_value}"})
        return urlopen(request, timeout=3)

    def test_opted_out_flag_does_not_affect_the_cookie_driven_own_team_path(self):
        """Issue #62: opting out only gates the explicit ?team_id= lookup of someone else's
        team, never a visitor's own remembered team resolved from their cookie."""
        set_lookup_opt_out(
            self.db_path, team_id=42, opted_out=True, pin_hash="some-hash",
            now="2026-08-08T00:00:00Z",
        )

        html = self._get_with_cookie("/", "42").read().decode()

        self.assertEqual(self.lookup_calls, [42])
        self.assertIn("Cookie Team", html)
        self.assertNotIn('"lookup":', html.replace(" ", ""))

    def test_cookie_resolves_the_team_when_no_query_param_is_present(self):
        html = self._get_with_cookie("/", "42").read().decode()

        self.assertEqual(self.lookup_calls, [42])
        self.assertIn("Cookie Team", html)

    def test_cookie_resolved_team_is_not_flagged_as_a_one_off_lookup(self):
        """Unlike an explicit ?team_id= lookup, this is the visitor's own remembered team."""
        html = self._get_with_cookie("/", "42").read().decode()

        self.assertNotIn('"lookup":', html.replace(" ", ""))

    def test_query_param_takes_precedence_over_the_cookie(self):
        request = Request(
            self.base_url + "/?team_id=99", headers={"Cookie": "fpl_team_id=42"}
        )
        html = urlopen(request, timeout=3).read().decode()

        self.assertEqual(self.lookup_calls, [99])
        self.assertIn('"lookup":', html.replace(" ", ""))

    def test_malformed_cookie_falls_back_to_the_shared_cached_dashboard(self):
        html = self._get_with_cookie("/", "not-a-number").read().decode()

        self.assertEqual(self.lookup_calls, [])
        self.assertIn("<h1>Dashboard</h1>", html)

    def test_saved_profile_is_spliced_in_and_drives_the_default_risk_profile(self):
        html = self._get_with_cookie("/", "42").read().decode()

        self.assertEqual(self.profile_read_calls, [42])
        self.assertIn('"risk_profile": "aggressive"', html)


class ModelPerformanceSpliceTests(unittest.TestCase):
    """Issue #64: team_performance/player_performance computed and spliced at request time."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "dashboard.html").write_text(
            '<meta name="refresh-token" content="__REFRESH_TOKEN__"><h1>Dashboard</h1>',
            encoding="utf-8",
        )
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({
                "generated_at": "2026-07-18T12:00:00Z",
                "model_performance": {"status": "waiting_for_results", "comparisons": []},
            }),
            encoding="utf-8",
        )
        self.performance_calls = []

        def team_view_action(team_id):
            return {
                "manager": {"connection_status": "connected", "team_id": team_id, "team_name": f"Team {team_id}", "squad": []},
                "weekly_decisions": {"status": "waiting_for_gw2", "event": 1},
            }

        def model_performance_action(team_id):
            self.performance_calls.append(team_id)
            return {
                "team_performance": {
                    "status": "active",
                    "comparisons": [{"event": 1, "modeled_points": float(team_id)}],
                },
                "player_performance": {"status": "waiting_for_results", "comparisons": []},
            }

        self.model_performance_action = model_performance_action
        self.server = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
            team_view_action=team_view_action,
            model_performance_action=model_performance_action,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def test_splices_the_resolved_teams_model_performance(self):
        html = urlopen(self.base_url + "/?team_id=111", timeout=3).read().decode()

        self.assertEqual(self.performance_calls, [111])
        self.assertIn('"modeled_points": 111.0', html)

    def test_two_different_teams_get_distinct_model_performance_slices(self):
        # Two independent servers (rather than two sequential requests to one) so the per-IP
        # lookup cooldown limiter doesn't block the second request -- that's issue #46's own
        # concern, not what this test is verifying.
        first_calls = []
        second_calls = []

        def model_performance_action_for(calls):
            def action(team_id):
                calls.append(team_id)
                return {
                    "team_performance": {
                        "status": "active",
                        "comparisons": [{"event": 1, "modeled_points": float(team_id)}],
                    },
                    "player_performance": {"status": "waiting_for_results", "comparisons": []},
                }
            return action

        def team_view_action(team_id):
            return {
                "manager": {"connection_status": "connected", "team_id": team_id, "squad": []},
                "weekly_decisions": {"status": "waiting_for_gw2", "event": 1},
            }

        server_a = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
            team_view_action=team_view_action,
            model_performance_action=model_performance_action_for(first_calls),
        )
        server_b = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
            team_view_action=team_view_action,
            model_performance_action=model_performance_action_for(second_calls),
        )
        thread_a = threading.Thread(target=server_a.serve_forever, daemon=True)
        thread_b = threading.Thread(target=server_b.serve_forever, daemon=True)
        thread_a.start()
        thread_b.start()
        try:
            first = urlopen(
                f"http://127.0.0.1:{server_a.server_port}/?team_id=111", timeout=3
            ).read().decode()
            second = urlopen(
                f"http://127.0.0.1:{server_b.server_port}/?team_id=222", timeout=3
            ).read().decode()

            self.assertEqual(first_calls, [111])
            self.assertEqual(second_calls, [222])
            self.assertIn('"modeled_points": 111.0', first)
            self.assertNotIn('"modeled_points": 222.0', first)
            self.assertIn('"modeled_points": 222.0', second)
            self.assertNotIn('"modeled_points": 111.0', second)
        finally:
            server_a.shutdown()
            server_a.server_close()
            thread_a.join(timeout=2)
            server_b.shutdown()
            server_b.server_close()
            thread_b.join(timeout=2)

    def test_absent_team_id_serves_the_cached_dashboard_without_computing_performance(self):
        html = urlopen(self.base_url + "/dashboard.html", timeout=3).read().decode()

        self.assertEqual(self.performance_calls, [])
        self.assertIn("<h1>Dashboard</h1>", html)

    def test_model_performance_failure_is_reported_cleanly_instead_of_a_server_error(self):
        failing_server = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
            team_view_action=lambda team_id: {
                "manager": {"connection_status": "connected", "team_id": team_id, "squad": []},
                "weekly_decisions": {"status": "waiting_for_gw2", "event": 1},
            },
            model_performance_action=lambda team_id: (_ for _ in ()).throw(RuntimeError("store corrupt")),
        )
        thread = threading.Thread(target=failing_server.serve_forever, daemon=True)
        thread.start()
        try:
            html = urlopen(
                f"http://127.0.0.1:{failing_server.server_port}/?team_id=364759", timeout=3
            ).read().decode()

            self.assertIn('"status": "error"', html)
            self.assertNotIn("store corrupt", html)
        finally:
            failing_server.shutdown()
            failing_server.server_close()
            thread.join(timeout=2)


class ProfileEndpointTests(unittest.TestCase):
    VALID_PAYLOAD = {
        "team_id": 364759,
        "timezone": "America/New_York",
        "risk_profile": "balanced",
        "confirmed_free_transfers": None,
        "confirmed_free_transfers_event": None,
    }

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "dashboard.html").write_text(
            '<meta name="refresh-token" content="__REFRESH_TOKEN__"><h1>Dashboard</h1>',
            encoding="utf-8",
        )
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-07-18T12:00:00Z"}), encoding="utf-8"
        )
        self.calls = []

        def refresh_action():
            self.calls.append("refresh")
            return {
                "generated_at": "2026-07-19T12:00:00Z",
                "confirmed_movements": 7,
                "fpl_status": "target_season_ready",
            }

        self.server = create_server(
            self.root,
            host="127.0.0.1",
            port=0,
            token="test-token",
            refresh_action=refresh_action,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.db_path = self.root / "data" / "profiles.db"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def _post_profile(self, payload, headers=None, base_url=None, raw_body=None):
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        data = raw_body if raw_body is not None else json.dumps(payload).encode("utf-8")
        request = Request(
            (base_url or self.base_url) + "/api/profile",
            data=data,
            method="POST",
            headers=request_headers,
        )
        return request

    def test_rejects_missing_token_and_saves_nothing(self):
        request = self._post_profile(self.VALID_PAYLOAD)

        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)

        self.assertEqual(error.exception.code, 403)
        self.assertFalse(self.db_path.exists())

    def test_rejects_cross_origin_request_even_with_valid_token(self):
        request = self._post_profile(
            self.VALID_PAYLOAD,
            headers={"X-Refresh-Token": "test-token", "Origin": "https://attacker.example"},
        )

        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)

        self.assertEqual(error.exception.code, 403)
        self.assertFalse(self.db_path.exists())

    def test_valid_payload_is_saved_and_returned_and_sets_the_team_cookie(self):
        request = self._post_profile(
            self.VALID_PAYLOAD, headers={"X-Refresh-Token": "test-token"}
        )

        response = urlopen(request, timeout=3)
        payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "ok")
        saved = load_profile(self.db_path, 364759)
        self.assertIsNotNone(saved)
        self.assertEqual(saved["timezone"], "America/New_York")
        self.assertEqual(saved["risk_profile"], "balanced")
        set_cookie = response.headers.get("Set-Cookie")
        self.assertIn("fpl_team_id=364759", set_cookie)
        self.assertIn("HttpOnly", set_cookie)

    def test_saving_again_for_the_same_team_updates_in_place(self):
        # A fresh server (own write-rate limiter) for the second save -- the two saves are
        # otherwise indistinguishable from a single client rapidly resaving, which is exactly
        # what the write cooldown (tested separately below) exists to bound.
        urlopen(
            self._post_profile(self.VALID_PAYLOAD, headers={"X-Refresh-Token": "test-token"}),
            timeout=3,
        )
        second_server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        thread = threading.Thread(target=second_server.serve_forever, daemon=True)
        thread.start()
        try:
            urlopen(
                self._post_profile(
                    {**self.VALID_PAYLOAD, "risk_profile": "aggressive"},
                    headers={"X-Refresh-Token": "test-token"},
                    base_url=f"http://127.0.0.1:{second_server.server_port}",
                ),
                timeout=3,
            )
        finally:
            second_server.shutdown()
            second_server.server_close()
            thread.join(timeout=2)

        saved = load_profile(self.db_path, 364759)
        self.assertEqual(saved["risk_profile"], "aggressive")

    def test_rejects_null_team_id_with_a_dedicated_message(self):
        request = self._post_profile(
            {**self.VALID_PAYLOAD, "team_id": None}, headers={"X-Refresh-Token": "test-token"}
        )

        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)

        self.assertEqual(error.exception.code, 400)
        payload = json.loads(error.exception.read())
        self.assertEqual(payload["message"], "A team ID is required to save settings")
        self.assertFalse(self.db_path.exists())

    def _assert_rejected(self, payload):
        request = self._post_profile(payload, headers={"X-Refresh-Token": "test-token"})

        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)

        self.assertEqual(error.exception.code, 400)
        body = error.exception.read().decode()
        self.assertFalse(self.db_path.exists())
        return body

    def test_rejects_non_numeric_team_id(self):
        body = self._assert_rejected({**self.VALID_PAYLOAD, "team_id": "abc"})
        self.assertNotIn("abc", body)

    def test_rejects_negative_team_id(self):
        self._assert_rejected({**self.VALID_PAYLOAD, "team_id": -5})

    def test_rejects_float_team_id(self):
        self._assert_rejected({**self.VALID_PAYLOAD, "team_id": 1.5})

    def test_rejects_unknown_timezone(self):
        self._assert_rejected({**self.VALID_PAYLOAD, "timezone": "Not/A_Zone"})

    def test_rejects_path_like_timezone(self):
        self._assert_rejected({**self.VALID_PAYLOAD, "timezone": "../etc/passwd"})

    def test_rejects_invalid_risk_profile(self):
        self._assert_rejected({**self.VALID_PAYLOAD, "risk_profile": "yolo"})

    def test_rejects_free_transfer_count_without_event(self):
        self._assert_rejected({**self.VALID_PAYLOAD, "confirmed_free_transfers": 3})

    def test_rejects_out_of_range_free_transfer_count(self):
        self._assert_rejected(
            {**self.VALID_PAYLOAD, "confirmed_free_transfers": 9, "confirmed_free_transfers_event": 2}
        )

    def test_rejects_unknown_key_without_writing_it(self):
        body = self._assert_rejected({**self.VALID_PAYLOAD, "password": "x"})
        self.assertNotIn("password", body)

    def test_rejects_oversized_body(self):
        oversized = json.dumps({**self.VALID_PAYLOAD, "padding": "x" * 5000}).encode("utf-8")
        request = self._post_profile(
            self.VALID_PAYLOAD,
            headers={"X-Refresh-Token": "test-token"},
            raw_body=oversized,
        )

        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)

        self.assertEqual(error.exception.code, 413)
        self.assertFalse(self.db_path.exists())

    def test_rejects_non_json_body(self):
        request = self._post_profile(
            None,
            headers={"X-Refresh-Token": "test-token"},
            raw_body=b"not json",
        )

        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)

        self.assertEqual(error.exception.code, 400)
        payload = json.loads(error.exception.read())
        self.assertEqual(payload["status"], "error")

    def test_profile_save_is_not_blocked_by_a_running_shared_refresh(self):
        """Issue #45: profile saves write to their own store, decoupled from refresh_lock."""
        release = threading.Event()
        entered = threading.Event()

        def blocking_refresh_action():
            entered.set()
            release.wait(timeout=5)
            return {}

        server = create_server(
            self.root,
            host="127.0.0.1",
            port=0,
            token="test-token",
            refresh_action=blocking_refresh_action,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            refresh_thread = threading.Thread(
                target=lambda: urlopen(
                    Request(
                        base_url + "/api/refresh",
                        data=b"{}",
                        method="POST",
                        headers={"X-Refresh-Token": "test-token", "Content-Type": "application/json"},
                    ),
                    timeout=5,
                )
            )
            refresh_thread.start()
            entered.wait(timeout=5)

            request = self._post_profile(
                self.VALID_PAYLOAD, headers={"X-Refresh-Token": "test-token"}, base_url=base_url
            )
            response = urlopen(request, timeout=3)

            self.assertEqual(response.status, 200)
        finally:
            release.set()
            refresh_thread.join(timeout=5)
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_repeated_saves_from_the_same_source_are_rate_limited(self):
        first = urlopen(
            self._post_profile(self.VALID_PAYLOAD, headers={"X-Refresh-Token": "test-token"}),
            timeout=3,
        )
        self.assertEqual(first.status, 200)

        with self.assertRaises(HTTPError) as error:
            urlopen(
                self._post_profile(
                    {**self.VALID_PAYLOAD, "team_id": 100001},
                    headers={"X-Refresh-Token": "test-token"},
                ),
                timeout=3,
            )

        self.assertEqual(error.exception.code, 429)

    def test_returns_generic_error_without_internal_details(self):
        failing_server = create_server(
            self.root,
            host="127.0.0.1",
            port=0,
            token="test-token",
            profile_action=lambda payload: (_ for _ in ()).throw(
                RuntimeError("/private/path secret")
            ),
        )
        thread = threading.Thread(target=failing_server.serve_forever, daemon=True)
        thread.start()
        try:
            request = self._post_profile(
                self.VALID_PAYLOAD,
                headers={"X-Refresh-Token": "test-token"},
                base_url=f"http://127.0.0.1:{failing_server.server_port}",
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)
            payload = json.loads(error.exception.read())

            self.assertEqual(error.exception.code, 500)
            self.assertEqual(payload["message"], "Profile update failed")
            self.assertNotIn("private/path", json.dumps(payload))
        finally:
            failing_server.shutdown()
            failing_server.server_close()
            thread.join(timeout=2)


class LookupOptOutEndpointTests(unittest.TestCase):
    """Issue #62: POST /api/lookup-opt-out and its first-claim PIN semantics."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "dashboard.html").write_text(
            '<meta name="refresh-token" content="__REFRESH_TOKEN__"><h1>Dashboard</h1>',
            encoding="utf-8",
        )
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-07-18T12:00:00Z"}), encoding="utf-8"
        )
        self.server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.db_path = self.root / "data" / "profiles.db"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def _post_opt_out(self, payload, headers=None, base_url=None, raw_body=None):
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        data = raw_body if raw_body is not None else json.dumps(payload).encode("utf-8")
        return Request(
            (base_url or self.base_url) + "/api/lookup-opt-out",
            data=data,
            method="POST",
            headers=request_headers,
        )

    def test_first_claim_sets_the_flag_and_stores_a_hashed_pin(self):
        request = self._post_opt_out(
            {"team_id": 364759, "opted_out": True, "pin": "abc123"},
            headers={"X-Refresh-Token": "test-token"},
        )

        response = urlopen(request, timeout=3)
        payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(payload, {"status": "ok", "team_id": 364759, "opted_out": True})
        saved = load_profile(self.db_path, 364759)
        self.assertTrue(saved["opted_out"])
        self.assertIsNotNone(saved["pin_hash"])
        self.assertNotEqual(saved["pin_hash"], "abc123")  # never the raw PIN

    def test_reclaiming_with_the_wrong_pin_is_rejected_and_leaves_the_flag_unchanged(self):
        first_server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        first_thread = threading.Thread(target=first_server.serve_forever, daemon=True)
        first_thread.start()
        try:
            urlopen(
                self._post_opt_out(
                    {"team_id": 364759, "opted_out": True, "pin": "abc123"},
                    headers={"X-Refresh-Token": "test-token"},
                    base_url=f"http://127.0.0.1:{first_server.server_port}",
                ),
                timeout=3,
            )
        finally:
            first_server.shutdown()
            first_server.server_close()
            first_thread.join(timeout=2)

        # A fresh server for its own independent rate limiter -- otherwise this second request
        # would be indistinguishable from a rapid resubmission by the same legitimate caller.
        second_server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        second_thread = threading.Thread(target=second_server.serve_forever, daemon=True)
        second_thread.start()
        try:
            request = self._post_opt_out(
                {"team_id": 364759, "opted_out": False, "pin": "wrongpin"},
                headers={"X-Refresh-Token": "test-token"},
                base_url=f"http://127.0.0.1:{second_server.server_port}",
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)
            payload = json.loads(error.exception.read())

            self.assertEqual(error.exception.code, 403)
            self.assertEqual(payload["message"], "Incorrect PIN")
        finally:
            second_server.shutdown()
            second_server.server_close()
            second_thread.join(timeout=2)

        # The flag stays exactly as the first, legitimate request left it.
        self.assertTrue(load_profile(self.db_path, 364759)["opted_out"])

    def test_reclaiming_with_the_correct_pin_allows_toggling(self):
        first_server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        first_thread = threading.Thread(target=first_server.serve_forever, daemon=True)
        first_thread.start()
        try:
            urlopen(
                self._post_opt_out(
                    {"team_id": 364759, "opted_out": True, "pin": "abc123"},
                    headers={"X-Refresh-Token": "test-token"},
                    base_url=f"http://127.0.0.1:{first_server.server_port}",
                ),
                timeout=3,
            )
        finally:
            first_server.shutdown()
            first_server.server_close()
            first_thread.join(timeout=2)

        second_server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        second_thread = threading.Thread(target=second_server.serve_forever, daemon=True)
        second_thread.start()
        try:
            response = urlopen(
                self._post_opt_out(
                    {"team_id": 364759, "opted_out": False, "pin": "abc123"},
                    headers={"X-Refresh-Token": "test-token"},
                    base_url=f"http://127.0.0.1:{second_server.server_port}",
                ),
                timeout=3,
            )
            payload = json.loads(response.read())

            self.assertEqual(response.status, 200)
            self.assertEqual(payload["opted_out"], False)
        finally:
            second_server.shutdown()
            second_server.server_close()
            second_thread.join(timeout=2)

        self.assertFalse(load_profile(self.db_path, 364759)["opted_out"])

    def test_rejects_missing_token(self):
        request = self._post_opt_out({"team_id": 364759, "opted_out": True, "pin": "abc123"})

        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)

        self.assertEqual(error.exception.code, 403)
        self.assertIsNone(load_profile(self.db_path, 364759))

    def test_rejects_a_pin_that_is_too_short(self):
        request = self._post_opt_out(
            {"team_id": 364759, "opted_out": True, "pin": "123"},
            headers={"X-Refresh-Token": "test-token"},
        )

        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)

        self.assertEqual(error.exception.code, 400)
        self.assertIsNone(load_profile(self.db_path, 364759))

    def test_rejects_non_boolean_opted_out(self):
        request = self._post_opt_out(
            {"team_id": 364759, "opted_out": "yes", "pin": "abc123"},
            headers={"X-Refresh-Token": "test-token"},
        )

        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)

        self.assertEqual(error.exception.code, 400)

    def test_rejects_unknown_keys(self):
        request = self._post_opt_out(
            {"team_id": 364759, "opted_out": True, "pin": "abc123", "extra": "x"},
            headers={"X-Refresh-Token": "test-token"},
        )

        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)

        self.assertEqual(error.exception.code, 400)

    def test_repeated_attempts_from_the_same_source_are_rate_limited(self):
        first = urlopen(
            self._post_opt_out(
                {"team_id": 364759, "opted_out": True, "pin": "abc123"},
                headers={"X-Refresh-Token": "test-token"},
            ),
            timeout=3,
        )
        self.assertEqual(first.status, 200)

        with self.assertRaises(HTTPError) as error:
            urlopen(
                self._post_opt_out(
                    {"team_id": 100001, "opted_out": True, "pin": "abc123"},
                    headers={"X-Refresh-Token": "test-token"},
                ),
                timeout=3,
            )

        self.assertEqual(error.exception.code, 429)


if __name__ == "__main__":
    unittest.main()
