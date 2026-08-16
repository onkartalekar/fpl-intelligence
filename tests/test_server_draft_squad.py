"""POST /api/draft-squad tests (issue #61). Split out of test_server.py by issue #210 to
mirror server_handlers/draft_squad.py."""


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
