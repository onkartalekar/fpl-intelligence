"""What's New / release-notes feature tests (issue #143): /api/release-notes, /api/release-
notes-subscribe, /api/release-notes-confirm-subscription, /api/release-notes-unsubscribe.
Split out of test_server.py by issue #210 to mirror server_handlers/release_notes_handlers.py."""


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


class ReleaseNotesApiTests(unittest.TestCase):
    """Issue #143: POST /api/release-notes -- operator-only publish of one day's "What's New"
    entry, same X-Refresh-Token gate as /api/refresh and /api/archive-team-forecast."""

    _VALID_PAYLOAD = {
        "date": "2026-08-11",
        "headline": "Sharper filters for preseason movement tracking",
        "summary": "Club movement just got easier to scan.",
        "changes": [
            {
                "category": "Feature",
                "audience": "user",
                "title": "Club movement filters split into Direction, Movement type, and Date",
                "description": "Previously one combined control; each now narrows independently.",
            },
        ],
    }

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

    def _post(self, payload, token="test-token"):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Refresh-Token"] = token
        return urlopen(
            Request(
                self.base_url + "/api/release-notes",
                data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers,
            ),
            timeout=3,
        )

    def test_missing_token_is_a_403(self):
        with self.assertRaises(HTTPError) as error:
            self._post(self._VALID_PAYLOAD, token=None)
        self.assertEqual(error.exception.code, 403)

    def test_wrong_token_is_a_403(self):
        with self.assertRaises(HTTPError) as error:
            self._post(self._VALID_PAYLOAD, token="wrong-token")
        self.assertEqual(error.exception.code, 403)

    def test_invalid_payload_is_a_400(self):
        with self.assertRaises(HTTPError) as error:
            self._post({**self._VALID_PAYLOAD, "headline": ""})
        self.assertEqual(error.exception.code, 400)

    def test_valid_entry_is_persisted(self):
        response = self._post(self._VALID_PAYLOAD)
        payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(payload, {"status": "ok", "date": "2026-08-11"})
        stored = json.loads((self.root / "data" / "release-notes.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["entries"][0]["headline"], self._VALID_PAYLOAD["headline"])

    def test_republishing_the_same_date_overwrites_rather_than_duplicates(self):
        self._post(self._VALID_PAYLOAD)
        self._post({**self._VALID_PAYLOAD, "headline": "Revised headline"})

        stored = json.loads((self.root / "data" / "release-notes.json").read_text(encoding="utf-8"))
        self.assertEqual(len(stored["entries"]), 1)
        self.assertEqual(stored["entries"][0]["headline"], "Revised headline")

    def test_published_entry_is_spliced_into_the_served_dashboard(self):
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-08-11T12:00:00Z"}), encoding="utf-8"
        )
        self._post(self._VALID_PAYLOAD)

        with urlopen(self.base_url + "/", timeout=3) as response:
            html = response.read().decode("utf-8")
        self.assertIn(self._VALID_PAYLOAD["headline"], html)
        self.assertIn('data-view="whats-new"', html)

class ReleaseNotesSubscribeEndpointTests(unittest.TestCase):
    """Issue #143: POST /api/release-notes-subscribe -- double opt-in, same shape as
    /api/reminder-opt-in's "enable" path. All SMTP sending is mocked via an injected
    `release_notes_subscribe_email_action`; no live network call is ever made."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        self.db_path = self.root / "data" / "release-notes-subscribers.db"
        self.sent_emails = []

    def tearDown(self):
        self.directory.cleanup()

    def _fake_email_action(self):
        def action(email, confirm_url):
            self.sent_emails.append((email, confirm_url))
        return action

    def _failing_email_action(self):
        def action(email, confirm_url):
            raise ReminderEmailError("Could not send the subscription confirmation email. Try again shortly.")
        return action

    def _start(self, **kwargs):
        server = create_server(self.root, host="127.0.0.1", port=0, token="test-token", **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _post(self, base_url, payload):
        return Request(
            base_url + "/api/release-notes-subscribe",
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"},
        )

    def test_valid_email_sends_a_confirmation_and_writes_a_pending_row(self):
        server, thread = self._start(release_notes_subscribe_email_action=self._fake_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            response = urlopen(self._post(base_url, {"email": "reader@example.com"}), timeout=3)
            payload = json.loads(response.read())

            self.assertEqual(response.status, 200)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(len(self.sent_emails), 1)
            sent_email, confirm_url = self.sent_emails[0]
            self.assertEqual(sent_email, "reader@example.com")
            self.assertIn("/api/release-notes-confirm-subscription?email=reader%40example.com&token=", confirm_url)

            from fpl_intel.release_notes_subscribers import load
            saved = load(self.db_path, "reader@example.com")
            self.assertEqual(saved["status"], "pending")
            self.assertIsNotNone(saved["confirm_token_hash"])
            self.assertNotIn(saved["confirm_token_hash"], confirm_url)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_missing_at_sign_is_a_400(self):
        server, thread = self._start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            with self.assertRaises(HTTPError) as error:
                urlopen(self._post(base_url, {"email": "not-an-email"}), timeout=3)
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_send_failure_is_a_502_and_writes_nothing(self):
        server, thread = self._start(release_notes_subscribe_email_action=self._failing_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            with self.assertRaises(HTTPError) as error:
                urlopen(self._post(base_url, {"email": "reader@example.com"}), timeout=3)
            self.assertEqual(error.exception.code, 502)

            from fpl_intel.release_notes_subscribers import load
            self.assertIsNone(load(self.db_path, "reader@example.com"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_endpoint_is_open_no_refresh_token_required(self):
        # Deliberately open/unauthenticated, unlike /api/release-notes -- no X-Refresh-Token
        # required, matching /api/contact's/reminder-opt-in's visitor-facing model.
        server, thread = self._start(release_notes_subscribe_email_action=self._fake_email_action())
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            response = urlopen(self._post(base_url, {"email": "reader@example.com"}), timeout=3)
            self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

class ReleaseNotesConfirmSubscriptionEndpointTests(unittest.TestCase):
    """Issue #143: GET /api/release-notes-confirm-subscription -- the emailed confirmation link."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        self.db_path = self.root / "data" / "release-notes-subscribers.db"

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

    def test_valid_token_confirms_the_subscription(self):
        from fpl_intel.release_notes_subscribers import load, set_pending
        set_pending(
            self.db_path, "reader@example.com", self._hash("real-token"),
            "2099-01-01T00:00:00+00:00", "2026-08-11T00:00:00Z",
        )
        server, thread = self._start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            response = urlopen(
                base_url + "/api/release-notes-confirm-subscription?email=reader@example.com&token=real-token",
                timeout=3,
            )
            html = response.read().decode()

            self.assertEqual(response.status, 200)
            self.assertIn("subscribed", html.lower())
            saved = load(self.db_path, "reader@example.com")
            self.assertEqual(saved["status"], "confirmed")
            self.assertIsNotNone(saved["unsubscribe_token"])
            self.assertIsNone(saved["confirm_token_hash"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_wrong_token_is_rejected(self):
        from fpl_intel.release_notes_subscribers import load, set_pending
        set_pending(
            self.db_path, "reader@example.com", self._hash("real-token"),
            "2099-01-01T00:00:00+00:00", "2026-08-11T00:00:00Z",
        )
        server, thread = self._start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            response = urlopen(
                base_url + "/api/release-notes-confirm-subscription?email=reader@example.com&token=wrong",
                timeout=3,
            )
            html = response.read().decode()

            self.assertNotIn("you're confirmed", html.lower())
            saved = load(self.db_path, "reader@example.com")
            self.assertEqual(saved["status"], "pending")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_expired_token_is_rejected(self):
        from fpl_intel.release_notes_subscribers import load, set_pending
        set_pending(
            self.db_path, "reader@example.com", self._hash("real-token"),
            "2020-01-01T00:00:00+00:00", "2026-08-11T00:00:00Z",
        )
        server, thread = self._start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            response = urlopen(
                base_url + "/api/release-notes-confirm-subscription?email=reader@example.com&token=real-token",
                timeout=3,
            )
            html = response.read().decode()

            self.assertIn("expired", html.lower())
            saved = load(self.db_path, "reader@example.com")
            self.assertEqual(saved["status"], "pending")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

class ReleaseNotesUnsubscribeEndpointTests(unittest.TestCase):
    """Issue #143: GET /api/release-notes-unsubscribe -- the link every sent release-notes
    email's footer carries."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        self.db_path = self.root / "data" / "release-notes-subscribers.db"

    def tearDown(self):
        self.directory.cleanup()

    def _start(self):
        server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def test_valid_token_removes_the_subscriber(self):
        from fpl_intel.release_notes_subscribers import confirm, load, set_pending
        set_pending(self.db_path, "reader@example.com", "hash", "2099-01-01T00:00:00+00:00", "2026-08-11T00:00:00Z")
        confirm(self.db_path, "reader@example.com", "unsub-token-xyz", "2026-08-11T00:00:00Z")

        server, thread = self._start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            response = urlopen(
                base_url + "/api/release-notes-unsubscribe?email=reader@example.com&token=unsub-token-xyz",
                timeout=3,
            )
            html = response.read().decode()

            self.assertEqual(response.status, 200)
            self.assertIn("unsubscribed", html.lower())
            self.assertIsNone(load(self.db_path, "reader@example.com"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_wrong_token_leaves_the_subscriber_in_place(self):
        from fpl_intel.release_notes_subscribers import confirm, load, set_pending
        set_pending(self.db_path, "reader@example.com", "hash", "2099-01-01T00:00:00+00:00", "2026-08-11T00:00:00Z")
        confirm(self.db_path, "reader@example.com", "unsub-token-xyz", "2026-08-11T00:00:00Z")

        server, thread = self._start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            urlopen(
                base_url + "/api/release-notes-unsubscribe?email=reader@example.com&token=wrong-token",
                timeout=3,
            )
            self.assertIsNotNone(load(self.db_path, "reader@example.com"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

class ReleaseNotesNotifySubscribersTests(unittest.TestCase):
    """Issue #143: publishing a new entry (POST /api/release-notes) emails every confirmed
    subscriber, with an independent, best-effort send per recipient."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        self.subscribers_db_path = self.root / "data" / "release-notes-subscribers.db"
        self.notified = []

    def tearDown(self):
        self.directory.cleanup()

    def _fake_notify_action(self):
        def action(email, entry, unsubscribe_url):
            self.notified.append((email, entry["date"], unsubscribe_url))
        return action

    def _failing_first_notify_action(self):
        calls = {"count": 0}

        def action(email, entry, unsubscribe_url):
            calls["count"] += 1
            if calls["count"] == 1:
                raise ReminderEmailError("boom")
            self.notified.append((email, entry["date"], unsubscribe_url))
        return action

    def _post_entry(self, base_url, date="2026-08-11"):
        payload = {
            "date": date, "headline": "H", "summary": "S",
            "changes": [{"category": "Feature", "audience": "user", "title": "T", "description": "D"}],
        }
        return urlopen(
            Request(
                base_url + "/api/release-notes", data=json.dumps(payload).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json", "X-Refresh-Token": "test-token"},
            ),
            timeout=3,
        )

    def test_confirmed_subscribers_are_notified_on_publish(self):
        from fpl_intel.release_notes_subscribers import confirm, set_pending
        set_pending(self.subscribers_db_path, "a@example.com", "hash", "2099-01-01T00:00:00+00:00", "2026-08-11T00:00:00Z")
        confirm(self.subscribers_db_path, "a@example.com", "unsub-a", "2026-08-11T00:00:00Z")

        server = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
            release_notes_notify_email_action=self._fake_notify_action(),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            response = self._post_entry(base_url)
            self.assertEqual(response.status, 200)
            time.sleep(0.1)  # notify runs after the response is already sent

            self.assertEqual(len(self.notified), 1)
            email, date, unsubscribe_url = self.notified[0]
            self.assertEqual(email, "a@example.com")
            self.assertEqual(date, "2026-08-11")
            self.assertIn("/api/release-notes-unsubscribe?email=a%40example.com&token=unsub-a", unsubscribe_url)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_pending_not_yet_confirmed_subscribers_are_not_notified(self):
        from fpl_intel.release_notes_subscribers import set_pending
        set_pending(self.subscribers_db_path, "a@example.com", "hash", "2099-01-01T00:00:00+00:00", "2026-08-11T00:00:00Z")

        server = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
            release_notes_notify_email_action=self._fake_notify_action(),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            self._post_entry(base_url)
            time.sleep(0.1)

            self.assertEqual(self.notified, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_one_subscribers_send_failure_does_not_block_the_others_or_the_publish_response(self):
        from fpl_intel.release_notes_subscribers import confirm, set_pending
        set_pending(self.subscribers_db_path, "a@example.com", "hash", "2099-01-01T00:00:00+00:00", "2026-08-11T00:00:00Z")
        confirm(self.subscribers_db_path, "a@example.com", "unsub-a", "2026-08-11T00:00:00Z")
        set_pending(self.subscribers_db_path, "b@example.com", "hash", "2099-01-01T00:00:00+00:00", "2026-08-11T00:00:00Z")
        confirm(self.subscribers_db_path, "b@example.com", "unsub-b", "2026-08-11T00:00:00Z")

        server = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
            release_notes_notify_email_action=self._failing_first_notify_action(),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            response = self._post_entry(base_url)
            self.assertEqual(response.status, 200)
            time.sleep(0.1)

            self.assertEqual(len(self.notified), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
