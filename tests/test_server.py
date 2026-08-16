"""Core DashboardHandler tests: server creation, Host/Origin trust, `_serve_dashboard`'s
own resolution logic (cookie/query-param team, PII filtering, connection-status splice),
connection-timeout handling, and access logging.

Issue #210 split every feature-specific endpoint's tests out into tests/test_server_*.py
files mirroring server_handlers/*.py -- this file keeps only what tests server.py's own
remaining cross-cutting plumbing rather than one feature's handler. See:
test_server_profile.py, test_server_draft_squad.py, test_server_lookup_opt_out.py,
test_server_reminder.py, test_server_release_notes.py, test_server_contact.py,
test_server_refresh.py, test_server_team_lookup.py.
"""


from contextlib import redirect_stderr, redirect_stdout
import gzip
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
            now="2026-08-08T00:00:00Z",
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
        self.assertIn('"risk_profile": "aggressive"', html)

    def test_the_visitors_own_cookie_resolved_team_still_sees_its_own_reminder_fields(self):
        request = Request(self.base_url + "/", headers={"Cookie": "fpl_team_id=364759"})
        html = urlopen(request, timeout=3).read().decode()

        self.assertIn("owner@example.com", html)
        self.assertIn('"reminder_status": "enabled"', html)
        self.assertIn('"reminder_lead_hours": 3', html)

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
                "confirmed_free_transfers_event": None, "risk_profile": "balanced",
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
        # logging, e.g. the structured JSON access-log line every request already produces via
        # log_request, as opposed to stderr, which every genuine-error call site in
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

    def test_a_server_level_handle_error_for_a_broken_pipe_logs_just_one_quiet_line(self):
        # Bug fix, confirmed live: a client disconnecting mid-response (browser tab closed, flaky
        # network, or a proxy/load balancer with a short read timeout) raises BrokenPipeError
        # from wfile.write inside do_GET, which escapes the request thread and lands exactly
        # here (ThreadingMixIn.process_request_thread routes it to _DashboardServer.handle_error)
        # -- same reasoning as the existing TimeoutError case just above, extended to the client-
        # went-away sibling exceptions.
        server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        try:
            for exception_cls in (BrokenPipeError, ConnectionResetError):
                with self.subTest(exception=exception_cls.__name__):
                    captured = io.StringIO()
                    try:
                        raise exception_cls("simulated client disconnect")
                    except exception_cls:
                        with redirect_stderr(captured):
                            server.handle_error(request=None, client_address=("203.0.113.1", 12345))

                    output = captured.getvalue()
                    self.assertIn("disconnected before the response finished sending", output)
                    self.assertNotIn("Traceback", output)
        finally:
            server.server_close()

    def test_log_error_for_a_broken_pipe_logs_just_one_quiet_line(self):
        # log_error's counterpart to the handle_error test above -- same DashboardHandler class
        # ConnectionTimeoutTests reaches via server.RequestHandlerClass, constructed bare (no live
        # socket needed) since log_message/log_date_time_string don't touch connection state.
        server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        try:
            handler = object.__new__(server.RequestHandlerClass)
            for exception_cls in (BrokenPipeError, ConnectionResetError):
                with self.subTest(exception=exception_cls.__name__):
                    captured = io.StringIO()
                    with redirect_stdout(captured):
                        handler.log_error("%s", exception_cls("simulated client disconnect"))

                    self.assertIn(
                        "disconnected before the response finished sending", captured.getvalue()
                    )
        finally:
            server.server_close()

class HeadRequestTests(unittest.TestCase):
    """Bug fix: BaseHTTPRequestHandler has no default HEAD support -- only defining do_GET/
    do_POST left every HEAD request (routine for uptime/health-check probes -- confirmed live,
    Railway's own platform health check hits "/" this way) falling through to the stdlib's
    generic "Unsupported method" 501, spamming logs with nothing actionable."""

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

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def test_head_on_the_dashboard_returns_200_with_no_body_but_the_real_content_length(self):
        get_response = urlopen(f"http://127.0.0.1:{self.server.server_port}/", timeout=3)
        get_body = get_response.read()

        head_request = Request(f"http://127.0.0.1:{self.server.server_port}/", method="HEAD")
        head_response = urlopen(head_request, timeout=3)
        head_body = head_response.read()

        self.assertEqual(head_response.status, 200)
        self.assertEqual(head_body, b"")
        # Per HTTP semantics, HEAD's Content-Length describes what the GET body would have been,
        # even though it's never actually sent.
        self.assertEqual(head_response.headers["Content-Length"], str(len(get_body)))
        self.assertEqual(head_response.headers["Content-Type"], get_response.headers["Content-Type"])

    def test_head_on_a_json_endpoint_returns_200_with_no_body(self):
        head_request = Request(f"http://127.0.0.1:{self.server.server_port}/api/status", method="HEAD")
        head_response = urlopen(head_request, timeout=3)

        self.assertEqual(head_response.status, 200)
        self.assertEqual(head_response.read(), b"")
        self.assertEqual(head_response.headers["Content-Type"], "application/json; charset=utf-8")

    def test_head_on_an_unknown_path_still_404s_with_no_body(self):
        head_request = Request(f"http://127.0.0.1:{self.server.server_port}/nope", method="HEAD")
        with self.assertRaises(HTTPError) as raised:
            urlopen(head_request, timeout=3)

        self.assertEqual(raised.exception.code, 404)
        self.assertEqual(raised.exception.read(), b"")

    def test_a_head_request_never_appears_as_an_unsupported_method_501(self):
        captured = io.StringIO()
        with redirect_stdout(captured):
            urlopen(Request(f"http://127.0.0.1:{self.server.server_port}/", method="HEAD"), timeout=3)

        self.assertNotIn("501", captured.getvalue())
        self.assertNotIn("Unsupported method", captured.getvalue())


class ResponseCompressionTests(unittest.TestCase):
    """Issue #209: `_json`/`_send_html` gzip the response body when the request's
    Accept-Encoding says the client can decode gzip, and leave it uncompressed otherwise --
    every response funnels through one of these two methods, so covering both covers every
    route. Doesn't touch `Cache-Control: no-store` (issue #120) or the inlined-CSS/JS shape
    (issue #51) -- transport-only."""

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

    def test_dashboard_html_is_gzipped_and_byte_identical_once_decompressed(self):
        plain = urlopen(self.base_url + "/dashboard.html", timeout=3)
        plain_body = plain.read()
        self.assertNotIn("Content-Encoding", plain.headers)

        request = Request(self.base_url + "/dashboard.html", headers={"Accept-Encoding": "gzip"})
        compressed = urlopen(request, timeout=3)
        compressed_body = compressed.read()

        self.assertEqual(compressed.headers["Content-Encoding"], "gzip")
        self.assertEqual(compressed.headers["Content-Length"], str(len(compressed_body)))
        self.assertLess(len(compressed_body), len(plain_body))
        self.assertEqual(gzip.decompress(compressed_body), plain_body)

    def test_json_endpoint_is_gzipped_too(self):
        plain_body = urlopen(self.base_url + "/api/status", timeout=3).read()

        request = Request(self.base_url + "/api/status", headers={"Accept-Encoding": "gzip"})
        compressed = urlopen(request, timeout=3)
        compressed_body = compressed.read()

        self.assertEqual(compressed.headers["Content-Encoding"], "gzip")
        self.assertEqual(gzip.decompress(compressed_body), plain_body)

    def test_a_client_that_omits_accept_encoding_gets_the_plain_body(self):
        response = urlopen(self.base_url + "/api/status", timeout=3)

        self.assertNotIn("Content-Encoding", response.headers)
        self.assertEqual(json.loads(response.read())["status"], "ok")

    def test_accept_encoding_with_other_tokens_and_q_values_still_matches_gzip(self):
        # Real browsers send something like "gzip, deflate, br" -- gzip is rarely the only or
        # first token, and q-values are optional and may appear on any token.
        request = Request(
            self.base_url + "/api/status",
            headers={"Accept-Encoding": "deflate, gzip;q=0.8, br"},
        )
        response = urlopen(request, timeout=3)

        self.assertEqual(response.headers["Content-Encoding"], "gzip")

    def test_vary_accept_encoding_is_always_present(self):
        with_gzip = urlopen(
            Request(self.base_url + "/api/status", headers={"Accept-Encoding": "gzip"}), timeout=3
        )
        without_gzip = urlopen(self.base_url + "/api/status", timeout=3)

        self.assertEqual(with_gzip.headers["Vary"], "Accept-Encoding")
        self.assertEqual(without_gzip.headers["Vary"], "Accept-Encoding")

    def test_head_request_with_gzip_reports_the_compressed_content_length(self):
        get_response = urlopen(
            Request(self.base_url + "/", headers={"Accept-Encoding": "gzip"}), timeout=3
        )
        get_body = get_response.read()

        head_response = urlopen(
            Request(self.base_url + "/", method="HEAD", headers={"Accept-Encoding": "gzip"}),
            timeout=3,
        )

        self.assertEqual(head_response.read(), b"")
        self.assertEqual(head_response.headers["Content-Encoding"], "gzip")
        self.assertEqual(head_response.headers["Content-Length"], str(len(get_body)))

    def test_cache_control_no_store_is_unaffected_by_compression(self):
        # Issue #120's always-render-fresh fix is explicitly out of scope for #209 -- confirm
        # compression didn't accidentally touch it.
        response = urlopen(
            Request(self.base_url + "/dashboard.html", headers={"Accept-Encoding": "gzip"}),
            timeout=3,
        )

        self.assertEqual(response.headers["Cache-Control"], "no-store")


class AccessLogClientLabelTests(unittest.TestCase):
    """Bug fix: DashboardHandler.log_message's override used to print only the timestamp and
    message, dropping the client IP the stdlib's own default normally includes and never
    capturing User-Agent at all -- every access-log line looked identical regardless of who sent
    it. Reported live: repeating unexplained "HEAD / ... 501" bursts with no way to tell a
    platform health check, an uptime monitor, or a crawler apart after the fact."""

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

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def test_a_normal_request_logs_the_client_ip_and_its_real_user_agent(self):
        request = Request(
            f"http://127.0.0.1:{self.server.server_port}/",
            headers={"User-Agent": "HealthCheckBot/2.1"},
        )
        captured = io.StringIO()
        with redirect_stdout(captured):
            urlopen(request, timeout=3)

        output = captured.getvalue()
        self.assertIn("127.0.0.1", output)
        self.assertIn("HealthCheckBot/2.1", output)

    def test_a_crafted_user_agent_cannot_forge_extra_log_lines(self):
        # Log injection: User-Agent is attacker-controlled input now written straight into logs
        # for the first time in this file. A newline inside it must not let a client fabricate
        # what looks like a second, independent log line. Tested via direct construction, not a
        # real request -- http.client's own header validation already refuses to *send* a raw
        # CRLF inside a header value (ValueError: Invalid header value), so a real attacker would
        # have to reach the server some other way (a non-validating client, a raw socket, or
        # obsolete HTTP header line-folding) to get a literal embedded newline into a parsed
        # header value in the first place; _client_label's own collapsing is what has to hold
        # regardless of how that value got there.
        handler = object.__new__(self.server.RequestHandlerClass)
        handler.client_address = ("203.0.113.5", 54321)
        handler.headers = {"User-Agent": "evil\r\n[15/Aug/2026 00:00:00] FAKE ADMIN LOGIN SUCCESS"}

        label = handler._client_label()

        self.assertNotIn("\n", label)
        self.assertNotIn("\r", label)
        self.assertIn("evil [15/Aug/2026 00:00:00] FAKE ADMIN LOGIN SUCCESS", label)

    def test_client_label_survives_a_connection_with_no_headers_parsed_yet(self):
        # _client_label is called from log_error's TimeoutError path too, which fires precisely
        # when a connection times out *before* a full request line (and therefore self.headers)
        # was ever received -- must not raise AttributeError in that case.
        handler = object.__new__(self.server.RequestHandlerClass)
        handler.client_address = ("203.0.113.5", 54321)
        # Deliberately no self.headers set at all, mirroring the real pre-parse_request() state.

        label = handler._client_label()

        self.assertIn("203.0.113.5", label)
        self.assertIn("-", label)

class AccessLogIsStructuredJSONTests(unittest.TestCase):
    """log_request now prints one JSON object per completed request instead of a plain-text
    line, so Railway's Log Explorer can parse route/status/method as filterable @attributes
    (@route, @status, @method) rather than requiring substring search over free text."""

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

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def _last_log_line(self, captured):
        lines = [line for line in captured.getvalue().splitlines() if line.strip()]
        return json.loads(lines[-1])

    def test_a_successful_request_logs_route_method_status_as_json_fields(self):
        captured = io.StringIO()
        with redirect_stdout(captured):
            urlopen(f"http://127.0.0.1:{self.server.server_port}/api/status", timeout=3)

        record = self._last_log_line(captured)
        self.assertEqual(record["route"], "/api/status")
        self.assertEqual(record["method"], "GET")
        self.assertEqual(record["status"], 200)
        self.assertEqual(record["level"], "info")
        self.assertIn("127.0.0.1", record["ip"])

    def test_route_strips_the_query_string_to_keep_it_a_low_cardinality_attribute(self):
        captured = io.StringIO()
        with redirect_stdout(captured):
            urlopen(f"http://127.0.0.1:{self.server.server_port}/?team_id=364759", timeout=3)

        record = self._last_log_line(captured)
        self.assertEqual(record["route"], "/")

    def test_a_client_error_status_is_logged_at_warn_level(self):
        captured = io.StringIO()
        with redirect_stdout(captured):
            try:
                urlopen(f"http://127.0.0.1:{self.server.server_port}/api/does-not-exist", timeout=3)
            except HTTPError:
                pass

        record = self._last_log_line(captured)
        self.assertEqual(record["status"], 404)
        self.assertEqual(record["level"], "warn")


if __name__ == "__main__":
    unittest.main()
