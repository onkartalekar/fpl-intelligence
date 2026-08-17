"""POST /api/profile tests (issue #45). Split out of test_server.py by issue #210 to mirror
server_handlers/profile.py."""


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
