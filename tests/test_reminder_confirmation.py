"""Tests for `reminder_confirmation.py`'s Contact Us email composition and SMTP send logic
(issue #110) -- `compose_contact_email`/`send_contact_email` reuse this module's
`_read_smtp_config`/`ReminderEmailError`/`_SEND_TIMEOUT_SECONDS`, the same machinery the reminder
opt-in confirmation-email functions above them use. Those confirmation-email functions are
exercised indirectly through `test_server.py`'s `reminder_email_action` dependency injection
(there is no dedicated smtplib-mocking unit test for them); this file adds direct coverage for
the newer compose/send logic, mocking `smtplib.SMTP` so no real network call is ever made.

Release-notes subscription/digest email tests live in `test_release_notes_email.py`, matching
that code's own move out of `reminder_confirmation.py` -- see `release_notes_email.py`'s module
docstring for why.
"""

import os
import smtplib
import unittest
from unittest.mock import MagicMock, patch

from fpl_intel.notifications.reminder_confirmation import (
    ReminderEmailError,
    SMTP_HOST_ENV_VAR, SMTP_PASSWORD_ENV_VAR, SMTP_PORT_ENV_VAR, SMTP_USER_ENV_VAR,
    compose_contact_email,
    send_contact_email,
)


_SMTP_CONFIG = {
    "host": "smtp.example.com", "port": 587,
    "user": "operator@example.com", "password": "hunter2",
}


class ComposeContactEmailTests(unittest.TestCase):
    def test_known_category_uses_its_readable_label(self):
        subject, body = compose_contact_email("feature_request", "Please add dark mode", None)
        self.assertIn("Feature request", subject)
        self.assertIn("Category: Feature request", body)

    def test_message_is_included_verbatim(self):
        _, body = compose_contact_email("bug", "The squad grid is broken on mobile", None)
        self.assertIn("The squad grid is broken on mobile", body)

    def test_reply_to_is_included_and_clearly_labeled_when_given(self):
        _, body = compose_contact_email("feedback", "Nice work", "visitor@example.com")
        self.assertIn("Reply-to: visitor@example.com", body)

    def test_reply_to_is_labeled_not_provided_when_absent(self):
        _, body = compose_contact_email("other", "Just saying hi", None)
        self.assertIn("Reply-to: (not provided)", body)

    def test_never_includes_server_internals_or_visitor_metadata(self):
        """Only category/message/reply-to may appear in the composed email -- no IP address, no
        other visitor metadata beyond what was explicitly submitted, per the plan doc's explicit
        scope for this email."""
        subject, body = compose_contact_email("bug", "A plain bug report", "visitor@example.com")
        combined = subject + body
        self.assertNotIn("127.0.0.1", combined)
        self.assertNotIn("client_address", combined)


class SendContactEmailTests(unittest.TestCase):
    def _fake_smtp(self):
        instance = MagicMock()
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        return instance

    def test_sends_to_the_configured_smtp_user_and_sets_reply_to_header(self):
        fake_smtp = self._fake_smtp()
        with patch(
            "fpl_intel.notifications.reminder_confirmation.smtplib.SMTP", return_value=fake_smtp,
        ) as smtp_ctor:
            send_contact_email(
                "bug", "Something broke", "visitor@example.com", smtp_config=_SMTP_CONFIG,
            )

        smtp_ctor.assert_called_once_with("smtp.example.com", 587, timeout=10)
        fake_smtp.starttls.assert_called_once()
        fake_smtp.login.assert_called_once_with("operator@example.com", "hunter2")
        self.assertEqual(fake_smtp.send_message.call_count, 1)
        sent_message = fake_smtp.send_message.call_args[0][0]
        self.assertEqual(sent_message["From"], "operator@example.com")
        # Sent to the operator's own configured mailbox -- there is no separate recipient
        # env var by design (see the module docstring).
        self.assertEqual(sent_message["To"], "operator@example.com")
        self.assertEqual(sent_message["Reply-To"], "visitor@example.com")

    def test_no_reply_to_header_when_none_was_given(self):
        fake_smtp = self._fake_smtp()
        with patch("fpl_intel.notifications.reminder_confirmation.smtplib.SMTP", return_value=fake_smtp):
            send_contact_email("other", "General note", None, smtp_config=_SMTP_CONFIG)

        sent_message = fake_smtp.send_message.call_args[0][0]
        self.assertIsNone(sent_message["Reply-To"])

    def test_smtp_failure_is_turned_into_reminder_email_error_without_leaking_details(self):
        with patch(
            "fpl_intel.notifications.reminder_confirmation.smtplib.SMTP",
            side_effect=smtplib.SMTPException("boom, credentials xyz"),
        ):
            with self.assertRaises(ReminderEmailError) as context:
                send_contact_email("bug", "Something broke", None, smtp_config=_SMTP_CONFIG)

        self.assertNotIn("boom", str(context.exception))
        self.assertNotIn("xyz", str(context.exception))

    def test_missing_smtp_configuration_raises_reminder_email_error(self):
        with patch.dict("os.environ", {}, clear=False):
            for var in (
                SMTP_HOST_ENV_VAR, SMTP_PORT_ENV_VAR, SMTP_USER_ENV_VAR, SMTP_PASSWORD_ENV_VAR,
            ):
                os.environ.pop(var, None)
            with self.assertRaises(ReminderEmailError):
                send_contact_email("bug", "Something broke", None)



if __name__ == "__main__":
    unittest.main()
