from contextlib import redirect_stderr, redirect_stdout
import http.client
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fpl_intel.profiles import (
    confirm_reminder, load_profile, save_profile, set_lookup_opt_out, set_reminder_pending,
)
from fpl_intel.recommendations import build_gw_recommendations
from fpl_intel.refresh import RefreshAlreadyRunning, project_refresh_lock
from fpl_intel.rate_limit import CooldownLimiter
from fpl_intel.reminder_confirmation import (
    ReminderEmailError,
    SMTP_HOST_ENV_VAR, SMTP_PASSWORD_ENV_VAR, SMTP_PORT_ENV_VAR, SMTP_USER_ENV_VAR,
)
from fpl_intel.server import (
    ReminderOptInCooldownError,
    _default_refresh_action,
    _default_reminder_opt_in_action,
    _default_team_view_action,
    _default_visitor_profile_action,
    build_refresh_result,
    create_server,
)
from tests.test_recommendations import sample_bootstrap, sample_fixtures


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

    def test_dashboard_never_ships_the_refresh_token(self):
        # Issue #27: the refresh token is an operator-only secret now -- it must never appear
        # anywhere in a served page, unlike before #27's token-in-markup design.
        html = urlopen(self.base_url + "/dashboard.html", timeout=3).read().decode()

        self.assertNotIn("test-token", html)
        self.assertNotIn("refresh-token", html)

    def test_no_team_id_view_reflects_a_state_change_with_no_republish_step(self):
        """Issue #120: before this fix, a no-team_id visitor was served a static dashboard.html
        baked at the last /api/refresh -- updating dashboard-state.json alone (what a fresh code
        deploy's next refresh, or in this test, a direct rewrite, produces) would not have been
        picked up without also regenerating that file. Now the no-team_id path renders fresh from
        dashboard-state.json on every request, so a state change alone is enough."""
        first = urlopen(self.base_url + "/dashboard.html", timeout=3).read().decode()
        self.assertIn('"generated_at": "2026-07-18T12:00:00Z"', first)

        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-07-20T09:00:00Z"}), encoding="utf-8"
        )

        second = urlopen(self.base_url + "/dashboard.html", timeout=3).read().decode()
        self.assertIn('"generated_at": "2026-07-20T09:00:00Z"', second)
        self.assertNotIn('"generated_at": "2026-07-18T12:00:00Z"', second)

    def test_rejects_untrusted_host_before_serving_dashboard(self):
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

    def test_accepts_a_non_loopback_binding(self):
        # Issue #27: the old 127.0.0.1-only restriction is gone -- create_server accepts any
        # bindable host (e.g. the 0.0.0.0 a hosting platform like Railway expects) and starts
        # cleanly, without raising.
        hosted_server = create_server(self.root, host="0.0.0.0", port=0, token="test-token")
        try:
            self.assertEqual(hosted_server.server_address[0], "0.0.0.0")
        finally:
            hosted_server.server_close()

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

    def test_error_logging_includes_a_traceback(self):
        # Issue #27: the six `except Exception` call sites in server.py used to print only the
        # exception's one-line repr, dropping exactly where it happened -- now every one of them
        # also includes `traceback.format_exc()`.
        failing_server = create_server(
            self.root,
            host="127.0.0.1",
            port=0,
            token="test-token",
            refresh_action=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        thread = threading.Thread(target=failing_server.serve_forever, daemon=True)
        thread.start()
        captured = io.StringIO()
        try:
            request = Request(
                f"http://127.0.0.1:{failing_server.server_port}/api/refresh",
                data=b"{}",
                method="POST",
                headers={"X-Refresh-Token": "test-token", "Content-Type": "application/json"},
            )
            with redirect_stderr(captured):
                with self.assertRaises(HTTPError):
                    urlopen(request, timeout=3)
            self.assertIn("Traceback", captured.getvalue())
            self.assertIn("RuntimeError: boom", captured.getvalue())
        finally:
            failing_server.shutdown()
            failing_server.server_close()
            thread.join(timeout=2)


class AllowedOriginTests(unittest.TestCase):
    """Issue #27: a custom `allowed_origin` is the single source of truth for both the trusted
    Host header (its netloc) and the trusted Origin header (its full value), replacing the
    hardcoded `127.0.0.1:{port}` default every other test class in this file exercises."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-07-18T12:00:00Z"}), encoding="utf-8"
        )
        self.server = create_server(
            self.root,
            host="127.0.0.1",
            port=0,
            token="test-token",
            allowed_origin="https://example.up.railway.app",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def _get_with_host(self, host_header):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.putrequest("GET", "/dashboard.html", skip_host=True)
        connection.putheader("Host", host_header)
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        connection.close()
        return response.status

    def _post_profile_with_headers(self, headers):
        body = json.dumps({
            "team_id": 364759,
            "timezone": "America/New_York",
            "risk_profile": "balanced",
            "goal": "top_50k",
            "confirmed_free_transfers": None,
            "confirmed_free_transfers_event": None,
        }).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.putrequest("POST", "/api/profile", skip_host=True)
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders()
        connection.send(body)
        response = connection.getresponse()
        status = response.status
        response.read()
        connection.close()
        return status

    def test_the_configured_origin_host_is_trusted(self):
        self.assertEqual(self._get_with_host("example.up.railway.app"), 200)

    def test_the_old_default_localhost_host_is_now_rejected(self):
        self.assertEqual(self._get_with_host(f"127.0.0.1:{self.server.server_port}"), 421)

    def test_the_configured_origin_header_passes_the_post_check(self):
        status = self._post_profile_with_headers({
            "Host": "example.up.railway.app",
            "Origin": "https://example.up.railway.app",
            "Content-Type": "application/json",
        })
        self.assertEqual(status, 200)

    def test_the_old_default_localhost_origin_header_is_now_rejected(self):
        status = self._post_profile_with_headers({
            "Host": "example.up.railway.app",
            "Origin": f"http://127.0.0.1:{self.server.server_port}",
            "Content-Type": "application/json",
        })
        self.assertEqual(status, 403)


class TeamLookupTests(unittest.TestCase):
    """The unauthenticated no-signup lookup path added for issue #46."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
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
        self.assertNotIn("test-token", html)
        self.assertNotIn("__DASHBOARD_DATA__", html)

    def test_absent_team_id_serves_the_shared_dashboard_state_unmodified(self):
        html = urlopen(self.base_url + "/dashboard.html", timeout=3).read().decode()

        self.assertEqual(self.lookup_calls, [])
        self.assertIn('"generated_at": "2026-07-18T12:00:00Z"', html)

    def test_malformed_team_id_falls_back_to_the_shared_dashboard_state(self):
        html = urlopen(self.base_url + "/?team_id=not-a-number", timeout=3).read().decode()

        self.assertEqual(self.lookup_calls, [])
        self.assertIn('"generated_at": "2026-07-18T12:00:00Z"', html)

    def test_out_of_range_team_id_falls_back_to_the_shared_dashboard_state(self):
        html = urlopen(self.base_url + "/?team_id=999999999", timeout=3).read().decode()

        self.assertEqual(self.lookup_calls, [])
        self.assertIn('"generated_at": "2026-07-18T12:00:00Z"', html)

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

    def test_missing_dashboard_state_returns_404_with_no_team_id(self):
        """Issue #120: the no-team_id path renders dashboard-state.json fresh on every request
        now, instead of serving a static dashboard.html -- it must 404 the same way the
        team_id-resolved path already does when that state hasn't been generated yet."""
        (self.root / "data" / "dashboard-state.json").unlink()

        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/dashboard.html", timeout=3)

        self.assertEqual(error.exception.code, 404)


class VolumeShadowedTeamLookupTests(unittest.TestCase):
    """Regression coverage for the live Railway bug: a volume mounted at `data/` shadows the
    git-tracked seed files that used to live directly under it. `_default_team_view_action`'s
    per-request closure reads `fpl-fixtures-latest.json`/`official-transfers-latest.json` off
    disk with no existence check -- this exercises that read against a `data/` directory built to
    look exactly like a freshly mounted (nothing seeded) volume, and would have raised
    `FileNotFoundError` against the pre-fix code.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "fpl-bootstrap-latest.json").write_text(
            json.dumps(sample_bootstrap()), encoding="utf-8"
        )
        # Deliberately no fpl-fixtures-latest.json / official-transfers-latest.json here.

    def tearDown(self):
        self.directory.cleanup()

    def test_team_lookup_against_a_shadowed_data_dir_degrades_gracefully(self):
        with patch(
            "fpl_intel.refresh.collect_public_manager",
            return_value={"entry": {"id": 364759, "name": "BrunoMans"}, "picks": []},
        ):
            result = _default_team_view_action(self.root)(364759)

        # No FileNotFoundError raised -- fixtures/transfers both degraded to empty defaults, and
        # the rest of compute_manager_view still ran to completion on top of them.
        self.assertIn("manager", result)
        self.assertIn("weekly_decisions", result)


class LookupOptOutGateTests(unittest.TestCase):
    """Issue #62: an opted-out team blocks the explicit ?team_id= lookup before any live call."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
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


class SharedStateApiTests(unittest.TestCase):
    """Issue #125: GET /api/shared-state -- the JSON equivalent of the no-team_id dashboard view,
    for GitHub-Actions-hosted scripts that can't reach Railway's local filesystem."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        self.server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def test_returns_the_full_dashboard_state_as_json(self):
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({
                "generated_at": "2026-08-11T12:00:00Z",
                "decision_center": {"status": "active", "recommended_squad": {"formation": "3-4-3"}},
            }),
            encoding="utf-8",
        )

        response = urlopen(self.base_url + "/api/shared-state", timeout=3)
        payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get("Content-Type"), "application/json; charset=utf-8")
        self.assertEqual(payload["generated_at"], "2026-08-11T12:00:00Z")
        self.assertEqual(payload["decision_center"]["recommended_squad"]["formation"], "3-4-3")

    def test_missing_dashboard_state_returns_404(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/api/shared-state", timeout=3)

        self.assertEqual(error.exception.code, 404)

    def test_is_not_rate_limited(self):
        """Plain file read, same cost profile as the existing public / route -- no per-request
        cost to protect, unlike the live-FPL-API-hitting team lookup."""
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-08-11T12:00:00Z"}), encoding="utf-8",
        )

        for _ in range(5):
            response = urlopen(self.base_url + "/api/shared-state", timeout=3)
            self.assertEqual(response.status, 200)


class ManagerViewApiTests(unittest.TestCase):
    """Issue #125: GET /api/manager-view?team_id=<id> -- the JSON equivalent of the explicit
    ?team_id= HTML lookup (#46), sharing the same opt-out (#62) and rate-limiting rules, minus the
    HTML-only profile splice (#79) this endpoint never returns in the first place."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-08-11T12:00:00Z"}), encoding="utf-8",
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
            self.root, host="127.0.0.1", port=0, token="test-token",
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

    def test_valid_team_id_returns_manager_and_weekly_decisions(self):
        response = urlopen(self.base_url + "/api/manager-view?team_id=364759", timeout=3)
        payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(self.lookup_calls, [364759])
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["manager"]["team_name"], "BrunoMans")
        self.assertEqual(payload["weekly_decisions"]["status"], "waiting_for_gw2")
        # Never returns the HTML path's separate visitor_profile splice -- nothing to filter.
        self.assertNotIn("profile", payload)

    def test_missing_team_id_is_a_400(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/api/manager-view", timeout=3)

        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.lookup_calls, [])

    def test_malformed_team_id_is_a_400(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/api/manager-view?team_id=not-a-number", timeout=3)

        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.lookup_calls, [])

    def test_opted_out_team_blocks_the_lookup_without_calling_the_live_action(self):
        set_lookup_opt_out(
            self.db_path, team_id=364759, opted_out=True, pin_hash="some-hash",
            now="2026-08-08T00:00:00Z",
        )

        response = urlopen(self.base_url + "/api/manager-view?team_id=364759", timeout=3)
        payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(self.lookup_calls, [])
        self.assertEqual(payload, {"status": "opted_out", "team_id": 364759})

    def test_lookup_failure_is_a_500(self):
        def failing_team_view_action(team_id):
            raise RuntimeError("upstream unavailable")

        failing_server = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
            team_view_action=failing_team_view_action,
        )
        thread = threading.Thread(target=failing_server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(f"http://127.0.0.1:{failing_server.server_port}/api/manager-view?team_id=364759", timeout=3)
            self.assertEqual(error.exception.code, 500)
        finally:
            failing_server.shutdown()
            failing_server.server_close()
            thread.join(timeout=2)

    def test_repeated_calls_from_the_same_source_are_rate_limited(self):
        first = urlopen(self.base_url + "/api/manager-view?team_id=364759", timeout=3)
        self.assertEqual(first.status, 200)

        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/api/manager-view?team_id=100001", timeout=3)

        self.assertEqual(error.exception.code, 429)
        self.assertEqual(self.lookup_calls, [364759])

    def test_a_valid_refresh_token_is_exempt_from_the_rate_limit(self):
        """Issue #125: a trusted script (e.g. the deadline reminder, looping over several teams
        in one run) must not trip the visitor-tuned per-IP cooldown on its own repeat calls."""
        first = urlopen(
            Request(self.base_url + "/api/manager-view?team_id=364759", headers={"X-Refresh-Token": "test-token"}),
            timeout=3,
        )
        second = urlopen(
            Request(self.base_url + "/api/manager-view?team_id=100001", headers={"X-Refresh-Token": "test-token"}),
            timeout=3,
        )

        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertEqual(self.lookup_calls, [364759, 100001])

    def test_an_invalid_refresh_token_does_not_exempt_from_the_rate_limit(self):
        first = urlopen(
            Request(self.base_url + "/api/manager-view?team_id=364759", headers={"X-Refresh-Token": "wrong-token"}),
            timeout=3,
        )
        self.assertEqual(first.status, 200)

        with self.assertRaises(HTTPError) as error:
            urlopen(
                Request(self.base_url + "/api/manager-view?team_id=100001", headers={"X-Refresh-Token": "wrong-token"}),
                timeout=3,
            )

        self.assertEqual(error.exception.code, 429)


class CookieResolvedTeamTests(unittest.TestCase):
    """Issue #45: a saved-team cookie is a second source for the per-request team view."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
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

    def test_malformed_cookie_falls_back_to_the_shared_dashboard_state(self):
        html = self._get_with_cookie("/", "not-a-number").read().decode()

        self.assertEqual(self.lookup_calls, [])
        self.assertIn('"generated_at": "2026-07-18T12:00:00Z"', html)

    def test_saved_profile_is_spliced_in_and_drives_the_default_risk_profile(self):
        html = self._get_with_cookie("/", "42").read().decode()

        self.assertEqual(self.profile_read_calls, [42])
        self.assertIn('"risk_profile": "aggressive"', html)


class ConnectionStatusResolutionTests(unittest.TestCase):
    """Issue #108: dashboard.js's Decision Center/Model Performance empty-state gate keys off
    `state.manager.connection_status === 'not_configured'`. This confirms `_serve_dashboard`
    resolves that field correctly for the three shapes the gate needs to tell apart -- no team at
    all (gate must fire), the visitor's own team via cookie (gate must not fire), and an explicit
    `?team_id=` lookup of someone else's team (gate must not fire either -- that's the whole point
    of issue #46's no-signup lookup, and it must not be mistaken for "no profile")."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        # Issue #120: a visitor with no team_id at all (no cookie, no query param) is now
        # rendered fresh from dashboard-state.json on every request, not served a pre-generated
        # dashboard.html. `_refresh_project_unlocked` sets `connection_status: "not_configured"`
        # (see refresh.py) whenever no team is configured for that shared refresh -- this
        # fixture stands in for that shared state.
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({
                "generated_at": "2026-08-10T12:00:00Z",
                "manager": {"connection_status": "not_configured", "squad": []},
            }),
            encoding="utf-8",
        )

        def team_view_action(team_id):
            return {
                "manager": {
                    "connection_status": "connected", "team_id": team_id,
                    "team_name": f"Team {team_id}", "squad": [],
                },
                "weekly_decisions": {"status": "waiting_for_gw2", "event": 1},
            }

        self.server = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
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

    def test_no_team_at_all_resolves_to_not_configured(self):
        html = urlopen(self.base_url + "/dashboard.html", timeout=3).read().decode()

        self.assertIn('"connection_status": "not_configured"', html)

    def test_own_team_via_cookie_never_resolves_to_not_configured(self):
        request = Request(self.base_url + "/", headers={"Cookie": "fpl_team_id=364759"})
        html = urlopen(request, timeout=3).read().decode()

        self.assertIn('"connection_status": "connected"', html)
        self.assertNotIn('"connection_status": "not_configured"', html)
        # This is the visitor's own remembered team, not a one-off lookup of someone else's.
        self.assertNotIn('"lookup":', html.replace(" ", ""))

    def test_someone_elses_team_via_explicit_lookup_never_resolves_to_not_configured(self):
        html = urlopen(self.base_url + "/?team_id=999001", timeout=3).read().decode()

        self.assertIn('"connection_status": "connected"', html)
        self.assertNotIn('"connection_status": "not_configured"', html)
        # Explicit lookup IS flagged (drives the "one-off lookup" banner, separate from the
        # profile-gate signal this test class is about).
        self.assertIn('"lookup":', html.replace(" ", ""))
        self.assertIn('"status": "ok"', html)


class ModelPerformanceSpliceTests(unittest.TestCase):
    """Issue #64: team_performance/player_performance computed and spliced at request time."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
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

    def test_absent_team_id_serves_the_dashboard_state_without_computing_performance(self):
        html = urlopen(self.base_url + "/dashboard.html", timeout=3).read().decode()

        self.assertEqual(self.performance_calls, [])
        self.assertIn('"status": "waiting_for_results"', html)

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
        "goal": "top_50k",
        "confirmed_free_transfers": None,
        "confirmed_free_transfers_event": None,
    }

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
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

    def test_succeeds_without_any_refresh_token_header(self):
        # Issue #27: /api/profile is one of the four endpoints the shared refresh token no
        # longer gates -- issue #45's own CooldownLimiter is the real protection here now.
        request = self._post_profile(self.VALID_PAYLOAD)

        response = urlopen(request, timeout=3)
        payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertIsNotNone(load_profile(self.db_path, 364759))

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
        self.assertEqual(payload["profile"]["goal"], "top_50k")
        saved = load_profile(self.db_path, 364759)
        self.assertIsNotNone(saved)
        self.assertEqual(saved["timezone"], "America/New_York")
        self.assertEqual(saved["risk_profile"], "balanced")
        self.assertEqual(saved["goal"], "top_50k")
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

    def test_saving_again_updates_goal_in_place(self):
        second_server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        thread = threading.Thread(target=second_server.serve_forever, daemon=True)
        thread.start()
        try:
            urlopen(
                self._post_profile(self.VALID_PAYLOAD, headers={"X-Refresh-Token": "test-token"}),
                timeout=3,
            )
            urlopen(
                self._post_profile(
                    {**self.VALID_PAYLOAD, "goal": "top_10k"},
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
        self.assertEqual(saved["goal"], "top_10k")

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

    def test_rejects_invalid_goal(self):
        self._assert_rejected({**self.VALID_PAYLOAD, "goal": "top_1k"})

    def test_rejects_missing_goal(self):
        payload = dict(self.VALID_PAYLOAD)
        del payload["goal"]
        self._assert_rejected(payload)

    def test_accepts_every_allowed_goal(self):
        for index, goal in enumerate(
            ["top_10k", "top_50k", "top_100k", "beat_last_season", "just_for_fun"]
        ):
            with self.subTest(goal=goal):
                server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    team_id = 100000 + index
                    request = self._post_profile(
                        {**self.VALID_PAYLOAD, "team_id": team_id, "goal": goal},
                        headers={"X-Refresh-Token": "test-token"},
                        base_url=f"http://127.0.0.1:{server.server_port}",
                    )
                    urlopen(request, timeout=3)
                    self.assertEqual(load_profile(self.db_path, team_id)["goal"], goal)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

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


class DefaultVisitorProfileGoalTests(unittest.TestCase):
    """Issue #78: `_default_visitor_profile_action` (the real, non-mocked implementation) reads
    and defaults `goal` the same way it already does `risk_profile`, for both the "no row at
    all" and "saved row" branches."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        self.db_path = self.root / "data" / "profiles.db"

    def tearDown(self):
        self.directory.cleanup()

    def test_defaults_to_top_50k_when_no_profile_was_ever_saved(self):
        action = _default_visitor_profile_action(self.root)

        profile = action(999)

        self.assertEqual(profile["team_id"], 999)
        self.assertEqual(profile["goal"], "top_50k")

    def test_returns_a_saved_non_default_goal(self):
        save_profile(
            self.db_path, team_id=42, timezone="UTC", risk_profile="balanced",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T00:00:00Z", goal="top_10k",
        )
        action = _default_visitor_profile_action(self.root)

        profile = action(42)

        self.assertEqual(profile["goal"], "top_10k")


class LookupOptOutEndpointTests(unittest.TestCase):
    """Issue #62: POST /api/lookup-opt-out and its first-claim PIN semantics."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
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

    def test_succeeds_without_any_refresh_token_header(self):
        # Issue #27: /api/lookup-opt-out is one of the four endpoints the shared refresh token
        # no longer gates -- its own PIN check plus rate limiting are the real protection here.
        request = self._post_opt_out({"team_id": 364759, "opted_out": True, "pin": "abc123"})

        response = urlopen(request, timeout=3)
        payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(payload, {"status": "ok", "team_id": 364759, "opted_out": True})
        self.assertTrue(load_profile(self.db_path, 364759)["opted_out"])

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


class DraftSquadEndpointTests(unittest.TestCase):
    """Issue #61: declaring a preseason draft squad through /api/draft-squad."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-07-18T12:00:00Z"}), encoding="utf-8"
        )
        bootstrap = sample_bootstrap()
        (self.root / "data" / "fpl-bootstrap-latest.json").write_text(
            json.dumps(bootstrap), encoding="utf-8"
        )
        opening = build_gw_recommendations(bootstrap, sample_fixtures(), "2026-07-01T12:00:00-04:00")
        self.legal_player_ids = [player["id"] for player in opening["recommended_squad"]["players"]]
        self.valid_payload = {"team_id": 364759, "player_ids": self.legal_player_ids}
        self.db_path = self.root / "data" / "profiles.db"
        self.server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def _post_draft(self, payload, headers=None, base_url=None, raw_body=None):
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        data = raw_body if raw_body is not None else json.dumps(payload).encode("utf-8")
        return Request(
            (base_url or self.base_url) + "/api/draft-squad",
            data=data,
            method="POST",
            headers=request_headers,
        )

    def test_succeeds_without_any_refresh_token_header(self):
        # Issue #27: /api/draft-squad is one of the four endpoints the shared refresh token no
        # longer gates -- issue #45's own CooldownLimiter is the real protection here now.
        response = urlopen(self._post_draft(self.valid_payload), timeout=3)
        payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertIsNotNone(load_profile(self.db_path, 364759))

    def test_valid_legal_squad_is_saved_and_sets_the_team_cookie(self):
        request = self._post_draft(self.valid_payload, headers={"X-Refresh-Token": "test-token"})

        response = urlopen(request, timeout=3)
        payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(sorted(payload["draft_squad"]), sorted(self.legal_player_ids))
        saved = load_profile(self.db_path, 364759)
        self.assertEqual(sorted(saved["draft_squad"]), sorted(self.legal_player_ids))
        set_cookie = response.headers.get("Set-Cookie")
        self.assertIn("fpl_team_id=364759", set_cookie)

    def test_rejects_a_squad_with_the_wrong_number_of_players(self):
        request = self._post_draft(
            {"team_id": 364759, "player_ids": self.legal_player_ids[:14]},
            headers={"X-Refresh-Token": "test-token"},
        )

        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)

        self.assertEqual(error.exception.code, 400)
        self.assertFalse(self.db_path.exists())

    def test_rejects_an_illegal_squad_that_violates_the_club_limit(self):
        bootstrap = json.loads((self.root / "data" / "fpl-bootstrap-latest.json").read_text())
        same_club_ids = [row["id"] for row in bootstrap["elements"] if row["team"] == 1][:4]
        illegal_ids = same_club_ids + self.legal_player_ids[4:15]

        request = self._post_draft(
            {"team_id": 364759, "player_ids": illegal_ids}, headers={"X-Refresh-Token": "test-token"}
        )

        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)

        self.assertEqual(error.exception.code, 400)
        self.assertFalse(self.db_path.exists())

    def test_null_player_ids_clears_a_previously_saved_draft(self):
        urlopen(
            self._post_draft(self.valid_payload, headers={"X-Refresh-Token": "test-token"}), timeout=3
        )

        # A fresh server (its own write-rate limiter) for the clear request -- otherwise it's
        # indistinguishable from a single client rapidly resaving, which the cooldown (tested
        # separately below) exists to bound.
        second_server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        thread = threading.Thread(target=second_server.serve_forever, daemon=True)
        thread.start()
        try:
            request = self._post_draft(
                {"team_id": 364759, "player_ids": None},
                headers={"X-Refresh-Token": "test-token"},
                base_url=f"http://127.0.0.1:{second_server.server_port}",
            )
            response = urlopen(request, timeout=3)
            payload = json.loads(response.read())
        finally:
            second_server.shutdown()
            second_server.server_close()
            thread.join(timeout=2)

        self.assertEqual(response.status, 200)
        self.assertIsNone(payload["draft_squad"])
        self.assertIsNone(load_profile(self.db_path, 364759)["draft_squad"])

    def test_rejects_unknown_key_without_writing_it(self):
        request = self._post_draft(
            {**self.valid_payload, "password": "x"}, headers={"X-Refresh-Token": "test-token"}
        )

        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)

        self.assertEqual(error.exception.code, 400)
        self.assertFalse(self.db_path.exists())

    def test_repeated_saves_from_the_same_source_are_rate_limited(self):
        first = urlopen(
            self._post_draft(self.valid_payload, headers={"X-Refresh-Token": "test-token"}), timeout=3
        )
        self.assertEqual(first.status, 200)

        with self.assertRaises(HTTPError) as error:
            urlopen(
                self._post_draft(self.valid_payload, headers={"X-Refresh-Token": "test-token"}),
                timeout=3,
            )

        self.assertEqual(error.exception.code, 429)

    def test_saving_a_profile_does_not_block_a_draft_squad_save_on_the_same_cooldown(self):
        """A separate limiter instance (not shared with /api/profile) backs this endpoint."""
        server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            profile_request = Request(
                base_url + "/api/profile",
                data=json.dumps(
                    {
                        "team_id": 364759,
                        "timezone": "UTC",
                        "risk_profile": "balanced",
                        "goal": "top_50k",
                        "confirmed_free_transfers": None,
                        "confirmed_free_transfers_event": None,
                    }
                ).encode("utf-8"),
                method="POST",
                headers={"X-Refresh-Token": "test-token", "Content-Type": "application/json"},
            )
            urlopen(profile_request, timeout=3)

            draft_request = self._post_draft(
                self.valid_payload, headers={"X-Refresh-Token": "test-token"}, base_url=base_url
            )
            response = urlopen(draft_request, timeout=3)

            self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_injected_action_bypasses_default_validation(self):
        recorded = []

        def draft_squad_action(payload):
            recorded.append(payload)
            return {"team_id": payload["team_id"], "draft_squad": payload.get("player_ids")}

        server = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
            draft_squad_action=draft_squad_action,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = self._post_draft(
                {"team_id": 1, "player_ids": [1, 2, 3]},
                headers={"X-Refresh-Token": "test-token"},
                base_url=f"http://127.0.0.1:{server.server_port}",
            )
            response = urlopen(request, timeout=3)
            payload = json.loads(response.read())

            self.assertEqual(response.status, 200)
            self.assertEqual(payload["draft_squad"], [1, 2, 3])
            self.assertEqual(recorded, [{"team_id": 1, "player_ids": [1, 2, 3]}])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class ReminderOptInEndpointTests(unittest.TestCase):
    """Issue #79: POST /api/reminder-opt-in -- enable (confirmation-link double opt-in),
    decline, and disable. All SMTP sending is mocked via an injected `reminder_email_action`;
    no live network call is ever made in this test class."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-07-18T12:00:00Z"}), encoding="utf-8"
        )
        self.db_path = self.root / "data" / "profiles.db"
        self.sent_emails = []

    def tearDown(self):
        self.directory.cleanup()

    def _fake_email_action(self):
        def action(email, confirm_url, lead_hours):
            self.sent_emails.append((email, confirm_url, lead_hours))
        return action

    def _failing_email_action(self, message="Could not send the confirmation email. Try again shortly."):
        def action(email, confirm_url, lead_hours):
            raise ReminderEmailError(message)
        return action

    def _start(self, **kwargs):
        server = create_server(self.root, host="127.0.0.1", port=0, token="test-token", **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _post(self, base_url, payload, headers=None, raw_body=None):
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        data = raw_body if raw_body is not None else json.dumps(payload).encode("utf-8")
        return Request(base_url + "/api/reminder-opt-in", data=data, method="POST", headers=request_headers)

    def test_enable_sends_a_confirmation_email_and_writes_a_pending_row(self):
        server, thread = self._start(reminder_email_action=self._fake_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            request = self._post(
                base_url,
                {"team_id": 364759, "action": "enable", "email": "manager@example.com", "lead_hours": 12},
                headers={"X-Refresh-Token": "test-token"},
            )
            response = urlopen(request, timeout=3)
            payload = json.loads(response.read())

            self.assertEqual(response.status, 200)
            self.assertEqual(payload, {"status": "ok", "team_id": 364759, "reminder_status": "pending"})
            self.assertEqual(len(self.sent_emails), 1)
            sent_email, confirm_url, lead_hours = self.sent_emails[0]
            self.assertEqual(sent_email, "manager@example.com")
            self.assertEqual(lead_hours, 12)
            self.assertIn("/api/reminder-confirm?team_id=364759&token=", confirm_url)
            self.assertTrue(confirm_url.startswith(f"http://127.0.0.1:{server.server_port}"))

            saved = load_profile(self.db_path, 364759)
            self.assertEqual(saved["reminder_status"], "pending")
            self.assertEqual(saved["reminder_pending_email"], "manager@example.com")
            self.assertEqual(saved["reminder_lead_hours"], 12)
            self.assertIsNotNone(saved["reminder_confirmation_token_hash"])
            # Never the raw token from the URL.
            self.assertNotIn(saved["reminder_confirmation_token_hash"], confirm_url)
            self.assertIsNone(saved["email"])  # not confirmed yet
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_enable_never_writes_to_the_db_when_the_send_fails(self):
        """The SMTP send is attempted BEFORE any DB write (issue #79) -- a send failure must
        leave no row referencing a token that was never actually emailed."""
        server, thread = self._start(reminder_email_action=self._failing_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            request = self._post(
                base_url,
                {"team_id": 364759, "action": "enable", "email": "victim@example.com", "lead_hours": 3},
                headers={"X-Refresh-Token": "test-token"},
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)
            payload = json.loads(error.exception.read())

            self.assertEqual(error.exception.code, 502)
            self.assertEqual(payload["status"], "error")
            self.assertIsNone(load_profile(self.db_path, 364759))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_decline_sets_status_without_sending_any_email(self):
        server, thread = self._start(reminder_email_action=self._fake_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            request = self._post(
                base_url, {"team_id": 364759, "action": "decline"},
                headers={"X-Refresh-Token": "test-token"},
            )
            response = urlopen(request, timeout=3)
            payload = json.loads(response.read())

            self.assertEqual(payload, {"status": "ok", "team_id": 364759, "reminder_status": "declined"})
            self.assertEqual(self.sent_emails, [])
            saved = load_profile(self.db_path, 364759)
            self.assertEqual(saved["reminder_status"], "declined")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_disable_clears_a_previously_confirmed_email(self):
        set_reminder_pending(
            self.db_path, team_id=364759, pending_email="manager@example.com", lead_hours=3,
            token_hash="hash", expires_at="2026-08-10T00:00:00+00:00", now="2026-08-08T00:00:00Z",
        )
        confirm_reminder(self.db_path, team_id=364759, now="2026-08-08T01:00:00Z")

        server, thread = self._start(reminder_email_action=self._fake_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            request = self._post(
                base_url, {"team_id": 364759, "action": "disable"},
                headers={"X-Refresh-Token": "test-token"},
            )
            response = urlopen(request, timeout=3)
            payload = json.loads(response.read())

            self.assertEqual(payload["reminder_status"], "declined")
            self.assertEqual(self.sent_emails, [])
            saved = load_profile(self.db_path, 364759)
            self.assertEqual(saved["reminder_status"], "declined")
            self.assertIsNone(saved["email"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_succeeds_without_any_refresh_token_header(self):
        # Issue #27: /api/reminder-opt-in is one of the four endpoints the shared refresh token
        # no longer gates -- its own two CooldownLimiters are the real protection here now.
        server, thread = self._start(reminder_email_action=self._fake_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            request = self._post(
                base_url, {"team_id": 364759, "action": "enable", "email": "a@b.com", "lead_hours": 3},
            )
            response = urlopen(request, timeout=3)
            payload = json.loads(response.read())

            self.assertEqual(response.status, 200)
            self.assertEqual(payload, {"status": "ok", "team_id": 364759, "reminder_status": "pending"})
            self.assertEqual(len(self.sent_emails), 1)
            self.assertIsNotNone(load_profile(self.db_path, 364759))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_rejects_an_invalid_email_shape(self):
        server, thread = self._start(reminder_email_action=self._fake_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            request = self._post(
                base_url,
                {"team_id": 364759, "action": "enable", "email": "not-an-email", "lead_hours": 3},
                headers={"X-Refresh-Token": "test-token"},
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)

            self.assertEqual(error.exception.code, 400)
            self.assertEqual(self.sent_emails, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_rejects_an_out_of_range_lead_hours(self):
        server, thread = self._start(reminder_email_action=self._fake_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            request = self._post(
                base_url,
                {"team_id": 364759, "action": "enable", "email": "a@b.com", "lead_hours": 6},
                headers={"X-Refresh-Token": "test-token"},
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)

            self.assertEqual(error.exception.code, 400)
            self.assertEqual(self.sent_emails, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_rejects_an_unknown_action(self):
        server, thread = self._start(reminder_email_action=self._fake_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            request = self._post(
                base_url, {"team_id": 364759, "action": "unsubscribe"},
                headers={"X-Refresh-Token": "test-token"},
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)

            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_rejects_decline_carrying_an_email_field(self):
        server, thread = self._start(reminder_email_action=self._fake_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            request = self._post(
                base_url, {"team_id": 364759, "action": "decline", "email": "a@b.com"},
                headers={"X-Refresh-Token": "test-token"},
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)

            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_repeated_requests_from_the_same_source_are_rate_limited(self):
        server, thread = self._start(reminder_email_action=self._fake_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            first = urlopen(
                self._post(base_url, {"team_id": 364759, "action": "decline"}, headers={"X-Refresh-Token": "test-token"}),
                timeout=3,
            )
            self.assertEqual(first.status, 200)

            with self.assertRaises(HTTPError) as error:
                urlopen(
                    self._post(base_url, {"team_id": 100001, "action": "decline"}, headers={"X-Refresh-Token": "test-token"}),
                    timeout=3,
                )

            self.assertEqual(error.exception.code, 429)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class ReminderOptInTeamKeyedCooldownTests(unittest.TestCase):
    """Issue #79: the team-ID-keyed SMTP-send cooldown, exercised directly against
    `_default_reminder_opt_in_action` (bypassing the HTTP layer) so the endpoint's own ordinary
    per-source `CooldownLimiter` -- a separate, unrelated cooldown checked earlier by
    `_handle_reminder_opt_in` -- can't interfere with isolating this one. This is the limiter
    that bounds worst-case confirmation-email volume landing on one target team ID regardless of
    how many source IPs a caller uses."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        self.db_path = self.root / "data" / "profiles.db"
        self.sent = []

    def tearDown(self):
        self.directory.cleanup()

    def _email_action(self):
        def action(email, confirm_url, lead_hours):
            self.sent.append((email, confirm_url, lead_hours))
        return action

    def _make_action(self, cooldown_seconds=600):
        limiter = CooldownLimiter(cooldown_seconds=cooldown_seconds)
        return _default_reminder_opt_in_action(self.root, self._email_action(), limiter)

    def test_a_second_enable_for_the_same_team_is_blocked_by_the_team_keyed_cooldown(self):
        action = self._make_action()

        first = action(
            {"team_id": 1, "action": "enable", "email": "a@b.com", "lead_hours": 3},
            "http://127.0.0.1:8877",
        )
        self.assertEqual(first["reminder_status"], "pending")

        with self.assertRaises(ReminderOptInCooldownError):
            action(
                {"team_id": 1, "action": "enable", "email": "different@example.com", "lead_hours": 3},
                "http://127.0.0.1:8877",
            )

        # The second, blocked attempt never reached the send step at all.
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(load_profile(self.db_path, 1)["reminder_pending_email"], "a@b.com")

    def test_a_different_team_id_is_not_blocked_by_another_teams_cooldown(self):
        action = self._make_action()

        action(
            {"team_id": 1, "action": "enable", "email": "a@b.com", "lead_hours": 3},
            "http://127.0.0.1:8877",
        )
        second = action(
            {"team_id": 2, "action": "enable", "email": "b@c.com", "lead_hours": 3},
            "http://127.0.0.1:8877",
        )

        self.assertEqual(second["reminder_status"], "pending")
        self.assertEqual(len(self.sent), 2)

    def test_decline_and_disable_are_never_gated_by_the_team_keyed_cooldown(self):
        """Only "enable" carries a third-party-affecting SMTP send -- decline/disable stay open,
        per the plan's per-transition risk table."""
        action = self._make_action()
        action(
            {"team_id": 1, "action": "enable", "email": "a@b.com", "lead_hours": 3},
            "http://127.0.0.1:8877",
        )

        result = action({"team_id": 1, "action": "decline"}, "http://127.0.0.1:8877")

        self.assertEqual(result["reminder_status"], "declined")


class ReminderConfirmEndpointTests(unittest.TestCase):
    """Issue #79: GET /api/reminder-confirm -- the emailed confirmation link."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-07-18T12:00:00Z"}), encoding="utf-8"
        )
        self.db_path = self.root / "data" / "profiles.db"

    def tearDown(self):
        self.directory.cleanup()

    def _start(self):
        server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _hash(self, raw_token):
        from hashlib import sha256
        return sha256(raw_token.encode("utf-8")).hexdigest()

    def test_valid_token_confirms_and_promotes_the_pending_email(self):
        set_reminder_pending(
            self.db_path, team_id=364759, pending_email="manager@example.com", lead_hours=12,
            token_hash=self._hash("real-token"), expires_at="2099-01-01T00:00:00+00:00",
            now="2026-08-08T00:00:00Z",
        )
        server, thread = self._start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            response = urlopen(
                base_url + "/api/reminder-confirm?team_id=364759&token=real-token", timeout=3
            )
            html = response.read().decode()

            self.assertEqual(response.status, 200)
            self.assertIn("confirmed", html.lower())
            saved = load_profile(self.db_path, 364759)
            self.assertEqual(saved["reminder_status"], "enabled")
            self.assertEqual(saved["email"], "manager@example.com")
            self.assertIsNone(saved["reminder_confirmation_token_hash"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_wrong_token_is_rejected_and_leaves_the_row_pending(self):
        set_reminder_pending(
            self.db_path, team_id=364759, pending_email="manager@example.com", lead_hours=12,
            token_hash=self._hash("real-token"), expires_at="2099-01-01T00:00:00+00:00",
            now="2026-08-08T00:00:00Z",
        )
        server, thread = self._start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            response = urlopen(
                base_url + "/api/reminder-confirm?team_id=364759&token=wrong-token", timeout=3
            )
            html = response.read().decode()

            self.assertEqual(response.status, 200)
            self.assertNotIn("you're confirmed", html.lower())
            saved = load_profile(self.db_path, 364759)
            self.assertEqual(saved["reminder_status"], "pending")
            self.assertIsNone(saved["email"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_expired_token_is_rejected(self):
        set_reminder_pending(
            self.db_path, team_id=364759, pending_email="manager@example.com", lead_hours=12,
            token_hash=self._hash("real-token"), expires_at="2020-01-01T00:00:00+00:00",
            now="2026-08-08T00:00:00Z",
        )
        server, thread = self._start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            response = urlopen(
                base_url + "/api/reminder-confirm?team_id=364759&token=real-token", timeout=3
            )
            html = response.read().decode()

            self.assertIn("expired", html.lower())
            saved = load_profile(self.db_path, 364759)
            self.assertEqual(saved["reminder_status"], "pending")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_already_used_token_cannot_be_reused(self):
        set_reminder_pending(
            self.db_path, team_id=364759, pending_email="manager@example.com", lead_hours=12,
            token_hash=self._hash("real-token"), expires_at="2099-01-01T00:00:00+00:00",
            now="2026-08-08T00:00:00Z",
        )
        server, thread = self._start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            first = urlopen(
                base_url + "/api/reminder-confirm?team_id=364759&token=real-token", timeout=3
            )
            self.assertIn("confirmed", first.read().decode().lower())

            second = urlopen(
                base_url + "/api/reminder-confirm?team_id=364759&token=real-token", timeout=3
            )
            second_html = second.read().decode()

            self.assertNotIn("you're confirmed", second_html.lower())
            saved = load_profile(self.db_path, 364759)
            self.assertEqual(saved["reminder_status"], "enabled")  # unchanged by the reuse attempt
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_missing_team_id_or_token_is_rejected_without_a_server_error(self):
        server, thread = self._start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            response = urlopen(base_url + "/api/reminder-confirm", timeout=3)

            self.assertEqual(response.status, 200)
            self.assertIn("invalid", response.read().decode().lower())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_confirming_a_team_with_no_saved_row_at_all_is_rejected(self):
        server, thread = self._start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            response = urlopen(
                base_url + "/api/reminder-confirm?team_id=999999&token=whatever", timeout=3
            )

            self.assertIn("invalid", response.read().decode().lower())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class ReminderProfileLeakFixTests(unittest.TestCase):
    """Issue #79: email/reminder_status/reminder_lead_hours must never appear in
    state["profile"] on an explicit ?team_id= lookup of someone else's team -- only on the
    visitor's own cookie-resolved team."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-07-18T12:00:00Z"}), encoding="utf-8"
        )
        self.db_path = self.root / "data" / "profiles.db"
        save_profile(
            self.db_path, team_id=364759, timezone="UTC", risk_profile="aggressive",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T00:00:00Z", goal="top_10k",
        )
        set_reminder_pending(
            self.db_path, team_id=364759, pending_email="owner@example.com", lead_hours=3,
            token_hash="hash", expires_at="2099-01-01T00:00:00+00:00", now="2026-08-08T00:00:00Z",
        )
        confirm_reminder(self.db_path, team_id=364759, now="2026-08-08T01:00:00Z")

        def team_view_action(team_id):
            return {
                "manager": {"connection_status": "connected", "team_id": team_id, "team_name": "T", "squad": []},
                "weekly_decisions": {"status": "waiting_for_gw2", "event": 1},
            }

        self.server = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token", team_view_action=team_view_action,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def test_explicit_lookup_of_someone_elses_team_never_includes_email_or_reminder_fields(self):
        html = urlopen(self.base_url + "/?team_id=364759", timeout=3).read().decode()

        self.assertNotIn("owner@example.com", html)
        self.assertNotIn('"reminder_status": "enabled"', html)
        self.assertNotIn('"reminder_lead_hours": 3', html)
        # Every other field this splice carries stays visible on an explicit lookup, unchanged.
        self.assertIn('"goal": "top_10k"', html)
        self.assertIn('"risk_profile": "aggressive"', html)

    def test_the_visitors_own_cookie_resolved_team_still_sees_its_own_reminder_fields(self):
        request = Request(self.base_url + "/", headers={"Cookie": "fpl_team_id=364759"})
        html = urlopen(request, timeout=3).read().decode()

        self.assertIn("owner@example.com", html)
        self.assertIn('"reminder_status": "enabled"', html)
        self.assertIn('"reminder_lead_hours": 3', html)


class ContactEndpointTests(unittest.TestCase):
    """Issue #110: POST /api/contact -- validation, the cooldown limiter, and the
    log-then-attempt-email durability backstop. All SMTP sending is mocked via an injected
    `contact_email_action`; no live network call is ever made in this test class (except the
    one dedicated test below that exercises the real default action against unset SMTP env
    vars, which never reaches the network either -- it fails fast on missing configuration).
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-07-18T12:00:00Z"}), encoding="utf-8"
        )
        self.log_path = self.root / "data" / "contact-submissions.log"
        self.sent_emails = []

    def tearDown(self):
        self.directory.cleanup()

    def _fake_email_action(self):
        def action(category, message, reply_to):
            self.sent_emails.append((category, message, reply_to))
        return action

    def _failing_email_action(self, message="Could not send the contact notification email."):
        def action(category, message_text, reply_to):
            raise ReminderEmailError(message)
        return action

    def _start(self, **kwargs):
        server = create_server(self.root, host="127.0.0.1", port=0, token="test-token", **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _post(self, base_url, payload, headers=None, raw_body=None):
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        data = raw_body if raw_body is not None else json.dumps(payload).encode("utf-8")
        return Request(base_url + "/api/contact", data=data, method="POST", headers=request_headers)

    def _log_lines(self):
        if not self.log_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def test_a_valid_submission_succeeds_logs_and_sends_the_notification_email(self):
        server, thread = self._start(contact_email_action=self._fake_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            request = self._post(
                base_url,
                {"category": "bug", "message": "Something is broken", "reply_to": "a@b.com"},
            )
            response = urlopen(request, timeout=3)
            payload = json.loads(response.read())

            self.assertEqual(response.status, 200)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(len(self.sent_emails), 1)
            category, message, reply_to = self.sent_emails[0]
            self.assertEqual(category, "bug")
            self.assertEqual(message, "Something is broken")
            self.assertEqual(reply_to, "a@b.com")

            lines = self._log_lines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["category"], "bug")
            self.assertEqual(lines[0]["message"], "Something is broken")
            self.assertEqual(lines[0]["reply_to"], "a@b.com")
            self.assertIn("timestamp", lines[0])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_succeeds_without_any_refresh_token_header(self):
        # Issue #110: /api/contact is open like the other four write endpoints -- its own
        # CooldownLimiter is the real protection here, not the shared refresh token.
        server, thread = self._start(contact_email_action=self._fake_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            request = self._post(base_url, {"category": "feedback", "message": "Nice app"})
            response = urlopen(request, timeout=3)
            payload = json.loads(response.read())

            self.assertEqual(response.status, 200)
            self.assertEqual(payload["status"], "ok")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_reply_to_is_optional(self):
        server, thread = self._start(contact_email_action=self._fake_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            request = self._post(base_url, {"category": "other", "message": "General note"})
            response = urlopen(request, timeout=3)

            self.assertEqual(response.status, 200)
            self.assertIsNone(self.sent_emails[0][2])
            self.assertIsNone(self._log_lines()[0]["reply_to"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_rejects_an_unknown_category(self):
        server, thread = self._start(contact_email_action=self._fake_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            request = self._post(base_url, {"category": "complaint", "message": "hi"})
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)

            self.assertEqual(error.exception.code, 400)
            self.assertEqual(self.sent_emails, [])
            self.assertEqual(self._log_lines(), [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_rejects_an_empty_or_whitespace_only_message(self):
        server, thread = self._start(contact_email_action=self._fake_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            request = self._post(base_url, {"category": "bug", "message": "   "})
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)

            self.assertEqual(error.exception.code, 400)
            self.assertEqual(self._log_lines(), [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_rejects_an_oversized_message(self):
        server, thread = self._start(contact_email_action=self._fake_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            request = self._post(base_url, {"category": "bug", "message": "x" * 2001})
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)

            self.assertEqual(error.exception.code, 400)
            self.assertEqual(self._log_lines(), [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_rejects_a_malformed_reply_to_email(self):
        server, thread = self._start(contact_email_action=self._fake_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            request = self._post(
                base_url, {"category": "bug", "message": "hi", "reply_to": "not-an-email"},
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)

            self.assertEqual(error.exception.code, 400)
            self.assertEqual(self._log_lines(), [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_rejects_an_unknown_extra_key(self):
        server, thread = self._start(contact_email_action=self._fake_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            request = self._post(base_url, {"category": "bug", "message": "hi", "ip": "1.2.3.4"})
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)

            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_a_second_rapid_submission_from_the_same_source_is_rate_limited(self):
        limiter = CooldownLimiter(cooldown_seconds=30, clock=lambda: 0.0)
        server, thread = self._start(
            contact_email_action=self._fake_email_action(), contact_limiter=limiter,
        )
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            first = urlopen(self._post(base_url, {"category": "bug", "message": "first"}), timeout=3)
            self.assertEqual(first.status, 200)

            with self.assertRaises(HTTPError) as error:
                urlopen(self._post(base_url, {"category": "bug", "message": "second"}), timeout=3)

            self.assertEqual(error.exception.code, 429)
            # Only the first submission actually got through -- one email, one log line.
            self.assertEqual(len(self.sent_emails), 1)
            self.assertEqual(len(self._log_lines()), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_the_cooldown_does_not_block_a_different_source_key(self):
        """Contrast case, run with no HTTP layer at all (mirrors RefreshCooldownTests's own
        contrast test) -- confirms the limiter really is per-source, not global."""
        limiter = CooldownLimiter(cooldown_seconds=30, clock=lambda: 0.0)
        self.assertTrue(limiter.allow("203.0.113.1"))
        self.assertTrue(limiter.allow("198.51.100.7"))

    def test_submission_is_still_logged_and_still_returns_a_clean_response_when_the_email_send_fails(self):
        """The actual regression test for the durability backstop's whole reason to exist: a
        naive implementation that only wrote the local log on email success (or skipped logging
        entirely when the send failed) would fail this test."""
        server, thread = self._start(contact_email_action=self._failing_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            request = self._post(
                base_url, {"category": "bug", "message": "SMTP is down for this one"},
            )
            response = urlopen(request, timeout=3)
            payload = json.loads(response.read())

            # Still reads as success to the visitor -- their feedback WAS captured.
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["status"], "ok")

            lines = self._log_lines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["message"], "SMTP is down for this one")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_a_missing_smtp_configuration_still_logs_and_returns_success(self):
        """Same regression as the test above, but through the real default email action (no
        injected fake), by clearing the SMTP env vars this process happens to have set --
        confirms the default `_default_contact_email_action` path also respects the
        log-before-email ordering, not only the injected-fake path used by every other test in
        this class. This is the realistic local-dev scenario: no SMTP configured at all."""
        with patch.dict("os.environ", {}, clear=False):
            for var in (SMTP_HOST_ENV_VAR, SMTP_PORT_ENV_VAR, SMTP_USER_ENV_VAR, SMTP_PASSWORD_ENV_VAR):
                os.environ.pop(var, None)
            server, thread = self._start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                request = self._post(
                    base_url, {"category": "feedback", "message": "no smtp configured locally"},
                )
                response = urlopen(request, timeout=3)
                payload = json.loads(response.read())

                self.assertEqual(response.status, 200)
                self.assertEqual(payload["status"], "ok")
                lines = self._log_lines()
                self.assertEqual(len(lines), 1)
                self.assertEqual(lines[0]["message"], "no smtp configured locally")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class RefreshCooldownTests(unittest.TestCase):
    """Issue #28: /api/refresh's own time-based cooldown, checked before `_handle_refresh`
    attempts `refresh_lock.acquire()` -- previously only concurrency-of-1 (the lock) protected
    this endpoint, so nothing stopped immediate, repeated sequential calls once one refresh
    finished. A fake clock is injected via `refresh_limiter` (create_server's DI hook added for
    exactly this) so the tests don't need to wait out the real 90-second cooldown.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
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

        self.refresh_action = refresh_action
        self.clock = {"now": 0.0}

    def tearDown(self):
        self.directory.cleanup()

    def _make_server(self, limiter):
        server = create_server(
            self.root,
            host="127.0.0.1",
            port=0,
            token="test-token",
            refresh_action=self.refresh_action,
            refresh_limiter=limiter,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _post_refresh(self, base_url):
        request = Request(
            base_url + "/api/refresh",
            data=b"{}",
            method="POST",
            headers={"X-Refresh-Token": "test-token", "Content-Type": "application/json"},
        )
        try:
            response = urlopen(request, timeout=3)
            return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_a_second_call_within_the_cooldown_returns_429_with_a_correct_token_both_times(self):
        limiter = CooldownLimiter(cooldown_seconds=90, clock=lambda: self.clock["now"])
        server, thread = self._make_server(limiter)
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            first_status, first_payload = self._post_refresh(base_url)
            second_status, second_payload = self._post_refresh(base_url)

            self.assertEqual(first_status, 200)
            self.assertEqual(first_payload["status"], "ok")
            self.assertEqual(second_status, 429)
            self.assertEqual(second_payload["status"], "error")
            # The lock-based concurrency guard would report 409 ("busy"), not 429 -- confirming
            # this really is the new cooldown, not the pre-existing refresh_lock.
            self.assertNotEqual(second_status, 409)
            # Only the first call actually ran the (real) refresh action.
            self.assertEqual(self.calls, ["refresh"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_first_call_is_never_blocked_and_the_cooldown_resets_after_the_window_elapses(self):
        limiter = CooldownLimiter(cooldown_seconds=90, clock=lambda: self.clock["now"])
        server, thread = self._make_server(limiter)
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            first_status, _ = self._post_refresh(base_url)
            self.assertEqual(first_status, 200)

            self.clock["now"] = 90.0  # exactly the cooldown window elapsed
            second_status, second_payload = self._post_refresh(base_url)

            self.assertEqual(second_status, 200)
            self.assertEqual(second_payload["status"], "ok")
            self.assertEqual(self.calls, ["refresh", "refresh"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_the_cooldown_is_keyed_globally_not_by_source_ip(self):
        """Proves the mechanism that makes the cooldown global: `_handle_refresh` always checks
        the limiter with the same fixed key, never `self.client_address[0]` -- unlike every other
        `CooldownLimiter` in server.py. A real two-different-source-IP HTTP test isn't practical
        in this sandboxed environment (binding a second loopback alias such as 127.0.0.2 needs
        privileges this environment doesn't grant test processes), so this spies on the key(s)
        actually passed to `.allow()` across two requests -- both necessarily arriving from the
        same 127.0.0.1 test-client source at the socket level, which is exactly the point: if the
        server were keying by `self.client_address[0]` instead, that fact would be invisible to a
        same-source test like this one, but the *keys observed* prove it isn't happening -- the
        same fixed key is used regardless of who's asking, which is precisely what makes two
        genuinely different real-world source IPs share one cooldown.
        """
        observed_keys = []
        real_limiter = CooldownLimiter(cooldown_seconds=90, clock=lambda: self.clock["now"])

        class SpyLimiter:
            def allow(self, key):
                observed_keys.append(key)
                return real_limiter.allow(key)

        server, thread = self._make_server(SpyLimiter())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            self._post_refresh(base_url)
            self._post_refresh(base_url)

            self.assertEqual(len(observed_keys), 2)
            # The same literal key both times -- not, e.g., two different client_address values.
            self.assertEqual(observed_keys[0], observed_keys[1])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_a_per_source_keyed_limiter_would_not_have_shared_this_cooldown(self):
        """Contrast case, run with no HTTP layer at all: a per-source-keyed `CooldownLimiter` --
        the pattern every *other* limiter in server.py uses, and the one this endpoint
        deliberately does not -- allows two different source keys independently. This is the
        behavior the global keying in `_handle_refresh` deliberately avoids.
        """
        per_source_limiter = CooldownLimiter(cooldown_seconds=90, clock=lambda: self.clock["now"])

        self.assertTrue(per_source_limiter.allow("203.0.113.1"))
        self.assertTrue(per_source_limiter.allow("198.51.100.7"))

    def test_refresh_token_check_still_runs_before_the_cooldown_and_is_unaffected_by_it(self):
        """An invalid token is still rejected with 403 even while a valid-token call would be
        within the cooldown window -- the two checks are independent, and this one is unchanged
        from before this issue's ship."""
        limiter = CooldownLimiter(cooldown_seconds=90, clock=lambda: self.clock["now"])
        server, thread = self._make_server(limiter)
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            self._post_refresh(base_url)  # consumes the cooldown with a valid token

            request = Request(
                base_url + "/api/refresh",
                data=b"{}",
                method="POST",
                headers={"X-Refresh-Token": "wrong-token", "Content-Type": "application/json"},
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)

            self.assertEqual(error.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class OtherEndpointsUnaffectedByRefreshCooldownTests(unittest.TestCase):
    """Issue #28: the four already-open write endpoints (issue #45's model) must be completely
    unaffected by the new refresh-only cooldown -- no shared limiter, no collateral effect."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-07-18T12:00:00Z"}), encoding="utf-8"
        )
        self.server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def test_a_refresh_call_does_not_block_a_following_profile_save(self):
        refresh_request = Request(
            self.base_url + "/api/refresh",
            data=b"{}",
            method="POST",
            headers={"X-Refresh-Token": "test-token", "Content-Type": "application/json"},
        )
        # No refresh_action override -- the real _default_refresh_action will fail (500) in this
        # empty temp root (no scripts/refresh_dashboard.py), which is fine: this test only cares
        # that the refresh *attempt*, whatever its own outcome, never gates /api/profile, an
        # entirely separate, untouched-by-this-issue limiter.
        with self.assertRaises(HTTPError) as error:
            urlopen(refresh_request, timeout=5)
        self.assertEqual(error.exception.code, 500)

        profile_request = Request(
            self.base_url + "/api/profile",
            data=json.dumps({
                "team_id": 364759, "timezone": "Europe/London", "confirmed_free_transfers": None,
                "confirmed_free_transfers_event": None, "risk_profile": "balanced", "goal": "top_50k",
            }).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        response = urlopen(profile_request, timeout=3)
        payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "ok")


class ConnectionTimeoutTests(unittest.TestCase):
    """Issue #28: a per-connection socket-read timeout, the defense against a slow-loris-style
    connection that opens and then sends data very slowly or not at all, tying up a thread
    indefinitely. These use a real raw socket against the real running server -- not just an
    assertion on the class attribute's value -- so the mechanism itself is proven to work, not
    merely configured.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-07-18T12:00:00Z"}), encoding="utf-8"
        )
        self.server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        # DashboardHandler is defined locally inside create_server; server.RequestHandlerClass
        # (set by socketserver.BaseServer.__init__) is the supported way to reach it from
        # outside. Saved/restored around each test so the fast value used here for a quick test
        # never leaks into the production default or any other test.
        self.handler_cls = self.server.RequestHandlerClass
        self.original_timeout = self.handler_cls.timeout
        self.handler_cls.timeout = 0.2
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.handler_cls.timeout = self.original_timeout
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def test_a_connection_that_sends_nothing_is_closed_within_the_timeout(self):
        sock = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5)
        try:
            start = time.monotonic()
            data = sock.recv(100)  # blocks until the server closes the idle connection
            elapsed = time.monotonic() - start

            self.assertEqual(data, b"")  # empty read == the peer (server) closed the connection
            self.assertLess(elapsed, 3.0)  # comfortably bounded, nowhere near "hangs forever"
        finally:
            sock.close()

    def test_a_connection_with_an_incomplete_request_is_also_closed(self):
        sock = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5)
        try:
            sock.sendall(b"GET /dashboard.html HTTP/1.1\r\n")  # no blank line -- headers never end
            start = time.monotonic()
            data = sock.recv(100)
            elapsed = time.monotonic() - start

            self.assertEqual(data, b"")
            self.assertLess(elapsed, 3.0)
        finally:
            sock.close()

    def test_a_normal_fast_request_is_unaffected_by_the_short_timeout(self):
        # The timeout only bounds socket *reads*; a request that arrives promptly and completely
        # must still be served normally, even with an aggressively short 0.2s timeout.
        response = urlopen(f"http://127.0.0.1:{self.server.server_port}/dashboard.html", timeout=3)

        self.assertEqual(response.status, 200)
        self.assertIn('"generated_at": "2026-07-18T12:00:00Z"', response.read().decode())

    def test_timeout_is_logged_as_one_quiet_labeled_line_not_a_traceback(self):
        # log_error (like this file's pre-existing log_message override it delegates to) writes
        # to stdout -- this file's established convention for routine per-request/operational
        # logging, e.g. the "GET /dashboard.html HTTP/1.1 200" access-log style lines every
        # request already produces, as opposed to stderr, which every genuine-error call site in
        # this file (`except Exception: print(..., file=sys.stderr)`) and the base
        # `handle_error`'s traceback dump both use. So the "quiet line instead of a traceback"
        # contrast is proven by: the quiet line appears on stdout, and stderr stays clean.
        captured_out = io.StringIO()
        captured_err = io.StringIO()
        with redirect_stdout(captured_out), redirect_stderr(captured_err):
            sock = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5)
            try:
                sock.recv(100)  # blocks until the server closes the idle connection
                # The server thread's log_error print happens strictly before it actually closes
                # the socket, but isn't guaranteed to already be flushed the instant our recv()
                # unblocks -- poll briefly rather than a single fixed sleep, to keep this test
                # fast on a quiet machine and non-flaky on a loaded one.
                deadline = time.monotonic() + 2.0
                while (
                    "connection timed out" not in captured_out.getvalue()
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)
            finally:
                sock.close()

        self.assertIn("connection timed out", captured_out.getvalue())
        self.assertEqual(captured_err.getvalue(), "")


class QuietTimeoutLoggingDoesNotSwallowRealErrorsTests(unittest.TestCase):
    """Issue #28: the quiet-timeout logging change (DashboardHandler.log_error's TimeoutError
    special case, and _DashboardServer.handle_error's matching one) must only suppress the
    traceback for that specific timeout case -- a genuine unrelated error must still surface its
    full traceback, exactly as issue #27's traceback-logging fix intended.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-07-18T12:00:00Z"}), encoding="utf-8"
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_a_genuine_refresh_failure_still_logs_its_full_traceback(self):
        # This already exercises `_handle_refresh`'s own `except Exception` (unchanged by this
        # issue), not the timeout path -- included here specifically to contrast against the new
        # ConnectionTimeoutTests.test_timeout_is_logged_as_one_quiet_labeled_line_not_a_traceback,
        # proving the two paths behave differently on purpose.
        failing_server = create_server(
            self.root,
            host="127.0.0.1",
            port=0,
            token="test-token",
            refresh_action=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        thread = threading.Thread(target=failing_server.serve_forever, daemon=True)
        thread.start()
        captured = io.StringIO()
        try:
            request = Request(
                f"http://127.0.0.1:{failing_server.server_port}/api/refresh",
                data=b"{}",
                method="POST",
                headers={"X-Refresh-Token": "test-token", "Content-Type": "application/json"},
            )
            with redirect_stderr(captured):
                with self.assertRaises(HTTPError):
                    urlopen(request, timeout=3)
            self.assertIn("Traceback", captured.getvalue())
            self.assertIn("RuntimeError: boom", captured.getvalue())
        finally:
            failing_server.shutdown()
            failing_server.server_close()
            thread.join(timeout=2)

    def test_a_server_level_handle_error_for_a_genuine_exception_still_prints_a_full_traceback(self):
        # Directly exercises _DashboardServer.handle_error -- the server-level override added
        # alongside the timeout -- since a genuine (non-timeout) exception essentially never
        # naturally reaches this level in practice (BaseHTTPRequestHandler.handle_one_request's
        # own except Exception* sites, unchanged by this issue, catch almost everything first).
        # Reached instead through server.RequestHandlerClass's request/server construction, same
        # access pattern ConnectionTimeoutTests uses to reach DashboardHandler from outside
        # create_server.
        server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        try:
            captured = io.StringIO()
            try:
                raise RuntimeError("a genuine unrelated bug, not a timeout")
            except RuntimeError:
                with redirect_stderr(captured):
                    server.handle_error(request=None, client_address=("203.0.113.1", 12345))

            output = captured.getvalue()
            self.assertIn("Traceback", output)
            self.assertIn("RuntimeError: a genuine unrelated bug, not a timeout", output)
            self.assertNotIn("connection timed out", output)
        finally:
            server.server_close()

    def test_a_server_level_handle_error_for_a_timeout_still_logs_just_one_quiet_line(self):
        server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        try:
            captured = io.StringIO()
            try:
                raise TimeoutError("timed out")
            except TimeoutError:
                with redirect_stderr(captured):
                    server.handle_error(request=None, client_address=("203.0.113.1", 12345))

            output = captured.getvalue()
            self.assertIn("connection timed out", output)
            self.assertNotIn("Traceback", output)
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
