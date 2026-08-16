"""POST /api/lookup-opt-out tests (issue #62). Split out of test_server.py by issue #210 to
mirror server_handlers/lookup_opt_out.py."""


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
