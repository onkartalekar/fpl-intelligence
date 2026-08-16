"""Deadline-reminder feature tests (issues #79/#105): /api/reminder-opt-in, /api/reminder-
confirm, /api/reminder-teams. Split out of test_server.py by issue #210 to mirror
server_handlers/reminder.py."""


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


def _enable_reminder(db_path, team_id, email, lead_hours, now):
    """Drive a team to a genuine `reminder_status='enabled'` row via #79's real write path
    (`set_reminder_pending` then `confirm_reminder`), rather than hand-writing SQL that bypasses
    it -- exercises the real schema and the real column semantics `_handle_reminder_teams`
    depends on."""
    set_reminder_pending(
        db_path, team_id, pending_email=email, lead_hours=lead_hours,
        token_hash="deadbeef", expires_at="2099-01-01T00:00:00Z", now=now,
    )
    result = confirm_reminder(db_path, team_id, now)
    assert result is not None, "confirm_reminder unexpectedly found nothing pending"
    return result

class ReminderTeamsApiTests(unittest.TestCase):
    """Issue #105: GET /api/reminder-teams -- the opted-in reminder roster, for GitHub-Actions-
    hosted scripts that can't reach Railway's local profiles.db. Unlike /api/manager-view, there
    is no safe unauthenticated response at all (real PII in bulk), gated by its own dedicated
    reminder_teams_token, never token (/api/refresh's)."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        self.db_path = self.root / "data" / "profiles.db"
        self.now = "2026-08-11T12:00:00Z"
        self.server = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
            reminder_teams_token="reminder-secret",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def _get(self, token="reminder-secret"):
        headers = {"X-Reminder-Teams-Token": token} if token is not None else {}
        return urlopen(Request(self.base_url + "/api/reminder-teams", headers=headers), timeout=3)

    def test_missing_token_is_a_403(self):
        with self.assertRaises(HTTPError) as error:
            self._get(token=None)

        self.assertEqual(error.exception.code, 403)

    def test_invalid_token_is_a_403(self):
        with self.assertRaises(HTTPError) as error:
            self._get(token="wrong-token")

        self.assertEqual(error.exception.code, 403)

    def test_a_valid_refresh_token_does_not_substitute_for_the_reminder_teams_token(self):
        """This endpoint's PII exposure is strictly greater than /api/manager-view's -- the
        ordinary operator refresh token must not double as a bypass for it."""
        with self.assertRaises(HTTPError) as error:
            urlopen(
                Request(self.base_url + "/api/reminder-teams", headers={"X-Refresh-Token": "test-token"}),
                timeout=3,
            )

        self.assertEqual(error.exception.code, 403)

    def test_returns_only_enabled_rows_with_email_and_lead_hours(self):
        # Never decided: a plain profile save never touches reminder_status, so it stays NULL.
        save_profile(self.db_path, 100, "America/New_York", "balanced", None, None, self.now)
        # Pending: confirmation email sent, link not yet clicked.
        set_reminder_pending(
            self.db_path, 200, pending_email="pending@example.com", lead_hours=3,
            token_hash="hash", expires_at="2099-01-01T00:00:00Z", now=self.now,
        )
        # Enabled: the only row that should surface.
        _enable_reminder(self.db_path, 300, "enabled@example.com", 6, self.now)

        response = self._get()
        payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(payload, {"status": "ok", "teams": [{"team_id": 300, "email": "enabled@example.com", "lead_hours": 6}]})

    def test_no_profiles_at_all_yields_an_empty_list(self):
        response = self._get()
        payload = json.loads(response.read())

        self.assertEqual(payload, {"status": "ok", "teams": []})

    def test_is_not_rate_limited(self):
        """Unlike /api/manager-view, this endpoint has no unauthenticated path at all to rate-
        limit -- a valid token is already the full gate, so repeat calls with it are never
        throttled."""
        for _ in range(5):
            response = self._get()
            self.assertEqual(response.status, 200)

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
