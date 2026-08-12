"""Issue #119: unit tests for `scripts/live_regression_check.py`.

Every check function makes real HTTP/IMAP calls by design (that's the entire point of a live
regression check) -- so, matching `tests/test_trigger_scheduled_refresh.py`'s/
`tests/test_send_deadline_reminder.py`'s own convention for scripts that call out to a real
service, these tests exercise the pure config-resolution helpers directly and mock `_request`/
IMAP for the check functions themselves, rather than hitting a real server or mailbox.
"""

import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

# scripts/ is not a package, matching every other scripts/*.py test module's own setup.
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "live_regression_check.py"
_SPEC = importlib.util.spec_from_file_location("live_regression_check", _SCRIPT_PATH)
lrc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lrc)


class ConfigResolutionTests(unittest.TestCase):
    def test_require_base_url_raises_when_unset(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(lrc.ConfigError):
                lrc._require_base_url()

    def test_require_base_url_strips_trailing_slash(self):
        with patch.dict("os.environ", {lrc.BASE_URL_ENV_VAR: "https://example.com/"}, clear=True):
            self.assertEqual(lrc._require_base_url(), "https://example.com")

    def test_public_team_id_defaults_when_unset(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(lrc._public_team_id(), lrc._DEFAULT_PUBLIC_TEAM_ID)

    def test_public_team_id_reads_override(self):
        with patch.dict("os.environ", {lrc.PUBLIC_TEAM_ID_ENV_VAR: "42"}, clear=True):
            self.assertEqual(lrc._public_team_id(), 42)

    def test_public_team_id_rejects_non_integer(self):
        with patch.dict("os.environ", {lrc.PUBLIC_TEAM_ID_ENV_VAR: "not-a-number"}, clear=True):
            with self.assertRaises(lrc.ConfigError):
                lrc._public_team_id()

    def test_require_smtp_credentials_raises_when_either_is_missing(self):
        with patch.dict("os.environ", {lrc.SMTP_USER_ENV_VAR: "user@example.com"}, clear=True):
            with self.assertRaises(lrc.ConfigError):
                lrc._require_smtp_credentials()

    def test_require_smtp_credentials_returns_both_when_set(self):
        env = {lrc.SMTP_USER_ENV_VAR: "user@example.com", lrc.SMTP_PASSWORD_ENV_VAR: "secret"}
        with patch.dict("os.environ", env, clear=True):
            self.assertEqual(lrc._require_smtp_credentials(), ("user@example.com", "secret"))


class ExtractDashboardDataTests(unittest.TestCase):
    def test_extracts_the_embedded_json(self):
        html = (
            '<html><body><script id="dashboard-data" type="application/json">'
            '{"manager": {"connection_status": "not_configured"}}</script></body></html>'
        )

        data = lrc._extract_dashboard_data(html)

        self.assertEqual(data["manager"]["connection_status"], "not_configured")

    def test_missing_script_tag_raises_check_failure(self):
        with self.assertRaises(lrc.CheckFailure):
            lrc._extract_dashboard_data("<html><body>no data here</body></html>")


class CheckDashboardShellTests(unittest.TestCase):
    def _html_with_tabs(self, tabs):
        return "".join(f'<section id="{tab}"></section>' for tab in tabs).encode("utf-8")

    def test_passes_when_every_tab_is_present(self):
        with patch.object(
            lrc, "_request", return_value=(200, self._html_with_tabs(lrc._EXPECTED_TABS))
        ):
            lrc.check_dashboard_shell("https://example.com")  # no raise

    def test_raises_when_a_tab_is_missing(self):
        incomplete = [tab for tab in lrc._EXPECTED_TABS if tab != "view-contact"]
        with patch.object(lrc, "_request", return_value=(200, self._html_with_tabs(incomplete))):
            with self.assertRaises(lrc.CheckFailure):
                lrc.check_dashboard_shell("https://example.com")

    def test_raises_on_non_200(self):
        with patch.object(lrc, "_request", return_value=(500, b"")):
            with self.assertRaises(lrc.CheckFailure):
                lrc.check_dashboard_shell("https://example.com")


class CheckStatusEndpointTests(unittest.TestCase):
    def test_passes_with_the_documented_shape(self):
        body = json.dumps(
            {"status": "ok", "refreshing": False, "generated_at": "2026-08-11T00:00:00Z", "fpl_status": "ok"}
        ).encode()
        with patch.object(lrc, "_request", return_value=(200, body)):
            lrc.check_status_endpoint("https://example.com")  # no raise

    def test_raises_when_a_key_is_missing(self):
        body = json.dumps({"status": "ok"}).encode()
        with patch.object(lrc, "_request", return_value=(200, body)):
            with self.assertRaises(lrc.CheckFailure):
                lrc.check_status_endpoint("https://example.com")


class CheckRefreshRequiresTokenTests(unittest.TestCase):
    def test_passes_on_403(self):
        with patch.object(lrc, "_request", return_value=(403, b"")) as mock_request:
            lrc.check_refresh_requires_token("https://example.com")  # no raise

        # Never called with a valid token -- see the module docstring's reasoning.
        args, kwargs = mock_request.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "/api/refresh")

    def test_raises_when_not_403(self):
        with patch.object(lrc, "_request", return_value=(200, b"")):
            with self.assertRaises(lrc.CheckFailure):
                lrc.check_refresh_requires_token("https://example.com")


class CheckEmptyStateAndPopulatedGatingTests(unittest.TestCase):
    def _html(self, connection_status):
        return json.dumps(
            {"manager": {"connection_status": connection_status}}
        ).encode()

    def _wrap(self, payload_bytes):
        return (
            b'<script id="dashboard-data" type="application/json">' + payload_bytes + b"</script>"
        )

    def test_passes_when_no_team_id_is_not_configured_and_public_team_is_populated(self):
        responses = [
            (200, self._wrap(self._html("not_configured"))),
            (200, self._wrap(self._html("connected"))),
        ]
        with patch.object(lrc, "_request", side_effect=responses):
            lrc.check_empty_state_and_populated_gating("https://example.com", 364759)  # no raise

    def test_raises_when_no_team_id_is_already_populated(self):
        responses = [(200, self._wrap(self._html("connected")))]
        with patch.object(lrc, "_request", side_effect=responses):
            with self.assertRaises(lrc.CheckFailure):
                lrc.check_empty_state_and_populated_gating("https://example.com", 364759)

    def test_raises_when_public_team_stays_not_configured(self):
        responses = [
            (200, self._wrap(self._html("not_configured"))),
            (200, self._wrap(self._html("not_configured"))),
        ]
        with patch.object(lrc, "_request", side_effect=responses):
            with self.assertRaises(lrc.CheckFailure):
                lrc.check_empty_state_and_populated_gating("https://example.com", 364759)


class CheckProfileEndpointTests(unittest.TestCase):
    """`check_profile_endpoint` sleeps for real between its two calls (see the module docstring's
    cooldown-collision reasoning) -- `time.sleep` is patched out here so these tests stay fast,
    matching this codebase's convention of never letting a unit test wait out a real cooldown
    (e.g. `CooldownLimiter`'s injectable fake clock in `tests/test_server.py`)."""

    def test_passes_when_valid_accepted_and_invalid_rejected(self):
        with patch.object(lrc, "_request", side_effect=[(200, b""), (400, b"")]), \
             patch.object(lrc.time, "sleep"):
            lrc.check_profile_endpoint("https://example.com")  # no raise

    def test_raises_when_valid_submission_is_rejected(self):
        with patch.object(lrc, "_request", side_effect=[(400, b"")]), \
             patch.object(lrc.time, "sleep"):
            with self.assertRaises(lrc.CheckFailure):
                lrc.check_profile_endpoint("https://example.com")

    def test_raises_when_invalid_submission_is_accepted(self):
        with patch.object(lrc, "_request", side_effect=[(200, b""), (200, b"")]), \
             patch.object(lrc.time, "sleep"):
            with self.assertRaises(lrc.CheckFailure):
                lrc.check_profile_endpoint("https://example.com")


class CheckDraftSquadEndpointTests(unittest.TestCase):
    def test_passes_when_clear_accepted_and_bad_team_id_rejected(self):
        with patch.object(lrc, "_request", side_effect=[(200, b""), (400, b"")]), \
             patch.object(lrc.time, "sleep"):
            lrc.check_draft_squad_endpoint("https://example.com")  # no raise

    def test_raises_when_clear_is_rejected(self):
        with patch.object(lrc, "_request", side_effect=[(400, b"")]), \
             patch.object(lrc.time, "sleep"):
            with self.assertRaises(lrc.CheckFailure):
                lrc.check_draft_squad_endpoint("https://example.com")


class CheckLookupOptOutEndpointTests(unittest.TestCase):
    def test_passes_when_valid_accepted_and_short_pin_rejected(self):
        with patch.object(lrc, "_request", side_effect=[(200, b""), (400, b"")]), \
             patch.object(lrc.time, "sleep"):
            lrc.check_lookup_opt_out_endpoint("https://example.com")  # no raise

    def test_raises_when_valid_submission_is_rejected(self):
        with patch.object(lrc, "_request", side_effect=[(400, b"")]), \
             patch.object(lrc.time, "sleep"):
            with self.assertRaises(lrc.CheckFailure):
                lrc.check_lookup_opt_out_endpoint("https://example.com")


class CheckReminderOptInEndpointTests(unittest.TestCase):
    def test_passes_on_200(self):
        with patch.object(lrc, "_request", side_effect=[(200, b""), (400, b"")]), \
             patch.object(lrc.time, "sleep"):
            lrc.check_reminder_opt_in_endpoint("https://example.com", "test@example.com")  # no raise

    def test_passes_on_429_cooldown(self):
        with patch.object(lrc, "_request", side_effect=[(429, b""), (400, b"")]), \
             patch.object(lrc.time, "sleep"):
            lrc.check_reminder_opt_in_endpoint("https://example.com", "test@example.com")  # no raise

    def test_raises_on_502_smtp_failure(self):
        """A live, real SMTP failure -- exactly the class of bug this whole issue exists to catch
        -- must fail this check, not be silently swallowed as an acceptable status code."""
        with patch.object(lrc, "_request", side_effect=[(502, b"")]), \
             patch.object(lrc.time, "sleep"):
            with self.assertRaises(lrc.CheckFailure):
                lrc.check_reminder_opt_in_endpoint("https://example.com", "test@example.com")

    def test_raises_on_unexpected_status(self):
        with patch.object(lrc, "_request", side_effect=[(500, b"")]), \
             patch.object(lrc.time, "sleep"):
            with self.assertRaises(lrc.CheckFailure):
                lrc.check_reminder_opt_in_endpoint("https://example.com", "test@example.com")


class CheckContactEndpointRejectsInvalidTests(unittest.TestCase):
    def test_passes_when_invalid_category_is_rejected(self):
        with patch.object(lrc, "_request", return_value=(400, b"")):
            lrc.check_contact_endpoint_rejects_invalid("https://example.com")  # no raise

    def test_raises_when_invalid_category_is_accepted(self):
        with patch.object(lrc, "_request", return_value=(200, b"")):
            with self.assertRaises(lrc.CheckFailure):
                lrc.check_contact_endpoint_rejects_invalid("https://example.com")


class ContactEndpointDeliveryTests(unittest.TestCase):
    """Issue #119's core new capability: verifying a Contact Us submission's notification email
    actually arrives, since the endpoint's own response can never reveal an SMTP failure by
    design (issue #110's durability backstop)."""

    def test_passes_when_the_marked_email_arrives(self):
        with patch.object(lrc, "_request", return_value=(200, b"")), \
             patch.object(lrc, "_imap_poll_for_marker", return_value=True):
            lrc.check_contact_endpoint_delivery(
                "https://example.com", "imap.example.com", 993, "user@example.com", "secret",
            )  # no raise

    def test_raises_when_the_submission_itself_is_rejected(self):
        with patch.object(lrc, "_request", return_value=(400, b"")):
            with self.assertRaises(lrc.CheckFailure):
                lrc.check_contact_endpoint_delivery(
                    "https://example.com", "imap.example.com", 993, "user@example.com", "secret",
                )

    def test_raises_when_the_email_never_arrives(self):
        """The exact regression this check exists to catch: the API call succeeds (issue #110's
        backstop swallows the SMTP failure) but the email never actually shows up."""
        with patch.object(lrc, "_request", return_value=(200, b"")), \
             patch.object(lrc, "_imap_poll_for_marker", return_value=False):
            with self.assertRaises(lrc.CheckFailure):
                lrc.check_contact_endpoint_delivery(
                    "https://example.com", "imap.example.com", 993, "user@example.com", "secret",
                )


class ImapPollForMarkerTests(unittest.TestCase):
    """Exercises `_imap_poll_for_marker`'s actual IMAP search/fetch calls against a fake
    connection, rather than mocking the function away entirely -- the earlier version of this
    function searched the Subject header for a marker that `compose_contact_email`
    (`reminder_confirmation.py`) never puts there (the real Subject is always
    `"FPL Intelligence contact form: <category>"`, regardless of what's submitted; the marker and
    run_id only ever appear in the body). That bug shipped and passed review because every test
    here mocked `_imap_poll_for_marker` itself, never exercising its real IMAP search criterion --
    confirmed live: the real notification email genuinely arrived while the check still reported
    it missing. These tests are the regression guard for that class of bug specifically."""

    def _fake_connection(self, search_result, fetch_bodies):
        """`search_result`: the message-ID bytestring IMAP SEARCH would return.
        `fetch_bodies`: {message_id_str: body_text_or_None (None simulates a failed fetch)}."""
        connection = MagicMock()
        connection.search.return_value = ("OK", [search_result])

        def fake_fetch(message_id, spec):
            body = fetch_bodies.get(message_id.decode() if isinstance(message_id, bytes) else message_id)
            if body is None:
                return ("NO", [None])
            return ("OK", [(b"1 (BODY[TEXT] {n})", body.encode())])

        connection.fetch.side_effect = fake_fetch
        return connection

    def test_searches_the_body_not_the_subject(self):
        """The exact bug: SEARCH must use the BODY criterion, since the marker is never in the
        Subject header compose_contact_email actually sends."""
        connection = self._fake_connection(b"1", {"1": "[live-regression-check] run-123 -- ..."})
        with patch.object(lrc.imaplib, "IMAP4_SSL", return_value=connection):
            lrc._imap_poll_for_marker("imap.example.com", 993, "user@example.com", "secret", "[live-regression-check]", "run-123")

        args, _kwargs = connection.search.call_args
        self.assertEqual(args[1], "BODY")
        self.assertNotEqual(args[1], "SUBJECT")

    def test_finds_a_real_message_whose_marker_and_run_id_are_only_in_the_body(self):
        """The exact real-world shape that broke: Subject is generic
        ("FPL Intelligence contact form: Other"), marker+run_id live in the body only."""
        connection = self._fake_connection(
            b"1", {"1": "Category: Other\n\nMessage:\n[live-regression-check] run-1786491040 -- automated live regression check, safe to ignore/delete.\n\nReply-to: (not provided)"},
        )
        with patch.object(lrc.imaplib, "IMAP4_SSL", return_value=connection):
            found = lrc._imap_poll_for_marker(
                "imap.example.com", 993, "user@example.com", "secret",
                "[live-regression-check]", "run-1786491040",
            )

        self.assertTrue(found)

    def test_returns_false_when_run_id_does_not_match_any_candidate(self):
        # Small but non-zero timeout: guarantees the loop body runs at least once (the deadline
        # is still in the future when first checked), while time.sleep is mocked so retries
        # between iterations never actually block the test.
        connection = self._fake_connection(b"1", {"1": "[live-regression-check] run-999 -- ..."})
        with patch.object(lrc.imaplib, "IMAP4_SSL", return_value=connection), \
             patch.object(lrc, "_IMAP_POLL_TIMEOUT_SECONDS", 0.05), \
             patch.object(lrc.time, "sleep"):
            found = lrc._imap_poll_for_marker(
                "imap.example.com", 993, "user@example.com", "secret",
                "[live-regression-check]", "run-123",
            )

        self.assertFalse(found)

    def test_logs_in_and_selects_inbox(self):
        # Matches on the first attempt (deterministic -- exactly one iteration, so login/select/
        # logout each happen exactly once) rather than relying on a real match ever showing up
        # within a timing-dependent retry loop.
        connection = self._fake_connection(b"1", {"1": "[live-regression-check] run-123 -- ..."})
        with patch.object(lrc.imaplib, "IMAP4_SSL", return_value=connection):
            found = lrc._imap_poll_for_marker(
                "imap.example.com", 993, "user@example.com", "secret", "[live-regression-check]", "run-123",
            )

        self.assertTrue(found)
        connection.login.assert_called_once_with("user@example.com", "secret")
        connection.select.assert_called_once_with("INBOX")
        connection.logout.assert_called_once()


class RunTests(unittest.TestCase):
    """Exercises `run()`'s check-collection/reporting logic with every individual check mocked."""

    def _patch_all_checks(self, failing=()):
        names = [
            "check_dashboard_shell", "check_status_endpoint", "check_refresh_requires_token",
            "check_empty_state_and_populated_gating", "check_profile_endpoint",
            "check_draft_squad_endpoint", "check_lookup_opt_out_endpoint",
            "check_contact_endpoint_rejects_invalid", "check_reminder_opt_in_endpoint",
            "check_contact_endpoint_delivery",
        ]
        patches = []
        for name in names:
            if name in failing:
                patches.append(patch.object(lrc, name, side_effect=lrc.CheckFailure("boom")))
            else:
                patches.append(patch.object(lrc, name, return_value=None))
        return patches

    def _apply(self, patches):
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_all_checks_pass_including_imap_dependent_ones(self):
        self._apply(self._patch_all_checks())

        passed, failed = lrc.run(
            "https://example.com", 364759, dry_run=False,
            imap_config={"user": "u", "password": "p", "host": "h", "port": 993},
        )

        self.assertEqual(failed, [])
        self.assertIn("contact_endpoint_delivery", passed)
        self.assertIn("reminder_opt_in_endpoint", passed)

    def test_dry_run_skips_only_contact_delivery_check(self):
        self._apply(self._patch_all_checks())

        passed, failed = lrc.run(
            "https://example.com", 364759, dry_run=True,
            imap_config={"user": "u", "password": "p", "host": "h", "port": 993},
        )

        self.assertEqual(failed, [])
        self.assertNotIn("contact_endpoint_delivery", passed)
        self.assertIn("contact_endpoint_rejects_invalid", passed)
        self.assertIn("reminder_opt_in_endpoint", passed)

    def test_no_imap_config_skips_both_imap_dependent_checks(self):
        self._apply(self._patch_all_checks())

        passed, failed = lrc.run("https://example.com", 364759, dry_run=False, imap_config=None)

        self.assertEqual(failed, [])
        self.assertNotIn("contact_endpoint_delivery", passed)
        self.assertNotIn("reminder_opt_in_endpoint", passed)

    def test_one_failing_check_is_reported_without_stopping_the_others(self):
        self._apply(self._patch_all_checks(failing={"check_status_endpoint"}))

        passed, failed = lrc.run("https://example.com", 364759, dry_run=True, imap_config=None)

        self.assertEqual(failed, ["status_endpoint"])
        self.assertIn("dashboard_shell", passed)
        self.assertIn("refresh_requires_token", passed)


class MainCliTests(unittest.TestCase):
    def test_missing_base_url_exits_non_zero(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(lrc.main([]), 1)

    def test_missing_smtp_credentials_exits_non_zero_without_dry_run(self):
        env = {lrc.BASE_URL_ENV_VAR: "https://example.com"}
        with patch.dict("os.environ", env, clear=True):
            self.assertEqual(lrc.main([]), 1)

    def test_missing_smtp_credentials_continues_in_dry_run(self):
        env = {lrc.BASE_URL_ENV_VAR: "https://example.com"}
        with patch.dict("os.environ", env, clear=True), \
             patch.object(lrc, "run", return_value=([], [])) as mock_run:
            code = lrc.main(["--dry-run"])

        self.assertEqual(code, 0)
        mock_run.assert_called_once()
        self.assertIsNone(mock_run.call_args.kwargs.get("imap_config"))

    def test_exit_code_reflects_failures(self):
        env = {
            lrc.BASE_URL_ENV_VAR: "https://example.com",
            lrc.SMTP_USER_ENV_VAR: "u@example.com",
            lrc.SMTP_PASSWORD_ENV_VAR: "secret",
        }
        with patch.dict("os.environ", env, clear=True), \
             patch.object(lrc, "run", return_value=(["a"], ["b"])):
            self.assertEqual(lrc.main([]), 1)

    def test_exit_code_zero_when_nothing_failed(self):
        env = {
            lrc.BASE_URL_ENV_VAR: "https://example.com",
            lrc.SMTP_USER_ENV_VAR: "u@example.com",
            lrc.SMTP_PASSWORD_ENV_VAR: "secret",
        }
        with patch.dict("os.environ", env, clear=True), \
             patch.object(lrc, "run", return_value=(["a", "b"], [])):
            self.assertEqual(lrc.main([]), 0)


if __name__ == "__main__":
    unittest.main()
