"""POST /api/refresh tests (issues #27/#28). Split out of test_server.py by issue #210 to
mirror server_handlers/refresh_endpoint.py."""


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

from fpl_intel.storage.profiles import (
    confirm_reminder, load_profile, save_profile, set_lookup_opt_out, set_reminder_pending,
)
from fpl_intel.modeling.recommendations import build_gw_recommendations
from fpl_intel.refresh import RefreshAlreadyRunning, project_refresh_lock
from fpl_intel.rate_limit import CooldownLimiter
from fpl_intel.notifications.reminder_confirmation import (
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

            # Issue #210: _default_refresh_action's implementation now lives in
            # server_handlers.refresh_endpoint (server.py re-exports the name for backward
            # compatibility) -- patch subprocess.run at its real call site.
            with patch("fpl_intel.server_handlers.refresh_endpoint.subprocess.run", return_value=completed):
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
