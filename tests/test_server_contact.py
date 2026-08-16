"""POST /api/contact tests (issue #110). Split out of test_server.py by issue #210 to mirror
server_handlers/contact.py."""


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
