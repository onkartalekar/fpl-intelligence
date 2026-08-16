"""Tests for `reminder_confirmation.py`'s Contact Us email composition and SMTP send logic
(issue #110) -- `compose_contact_email`/`send_contact_email` reuse this module's
`_read_smtp_config`/`ReminderEmailError`/`_SEND_TIMEOUT_SECONDS`, the same machinery the reminder
opt-in confirmation-email functions above them use. Those confirmation-email functions are
exercised indirectly through `test_server.py`'s `reminder_email_action` dependency injection
(there is no dedicated smtplib-mocking unit test for them); this file adds direct coverage for
the newer compose/send logic, mocking `smtplib.SMTP` so no real network call is ever made.
"""

import os
import smtplib
import unittest
from unittest.mock import MagicMock, patch

from fpl_intel.reminder_confirmation import (
    ReminderEmailError,
    SMTP_HOST_ENV_VAR, SMTP_PASSWORD_ENV_VAR, SMTP_PORT_ENV_VAR, SMTP_USER_ENV_VAR,
    compose_contact_email,
    compose_release_notes_email,
    compose_release_notes_subscription_email,
    send_contact_email,
    send_release_notes_email,
    send_release_notes_subscription_email,
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
            "fpl_intel.reminder_confirmation.smtplib.SMTP", return_value=fake_smtp,
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
        with patch("fpl_intel.reminder_confirmation.smtplib.SMTP", return_value=fake_smtp):
            send_contact_email("other", "General note", None, smtp_config=_SMTP_CONFIG)

        sent_message = fake_smtp.send_message.call_args[0][0]
        self.assertIsNone(sent_message["Reply-To"])

    def test_smtp_failure_is_turned_into_reminder_email_error_without_leaking_details(self):
        with patch(
            "fpl_intel.reminder_confirmation.smtplib.SMTP",
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


class ComposeReleaseNotesSubscriptionEmailTests(unittest.TestCase):
    def test_confirm_url_is_included(self):
        _, body = compose_release_notes_subscription_email("https://example.com/confirm?token=abc")
        self.assertIn("https://example.com/confirm?token=abc", body)

    def test_says_nothing_happens_until_the_link_is_clicked(self):
        _, body = compose_release_notes_subscription_email("https://example.com/confirm")
        self.assertIn("nothing is sent to this address until this link is clicked", body)


class SendReleaseNotesSubscriptionEmailTests(unittest.TestCase):
    def _fake_smtp(self):
        instance = MagicMock()
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        return instance

    def test_sends_to_the_submitted_address(self):
        fake_smtp = self._fake_smtp()
        with patch("fpl_intel.reminder_confirmation.smtplib.SMTP", return_value=fake_smtp):
            send_release_notes_subscription_email(
                "visitor@example.com", "https://example.com/confirm", smtp_config=_SMTP_CONFIG,
            )
        sent_message = fake_smtp.send_message.call_args[0][0]
        self.assertEqual(sent_message["To"], "visitor@example.com")

    def test_smtp_failure_is_turned_into_reminder_email_error(self):
        with patch(
            "fpl_intel.reminder_confirmation.smtplib.SMTP",
            side_effect=smtplib.SMTPException("boom"),
        ):
            with self.assertRaises(ReminderEmailError):
                send_release_notes_subscription_email(
                    "visitor@example.com", "https://example.com/confirm", smtp_config=_SMTP_CONFIG,
                )


_SAMPLE_ENTRY = {
    "date": "2026-08-11",
    "headline": "Sharper filters for preseason movement tracking",
    "summary": "Club movement just got easier to scan.",
    "changes": [
        {"category": "Feature", "title": "Split filters", "description": "Three controls now."},
    ],
}

# A mixed entry -- one change in each of #190's two email sections -- for tests that need both
# "What's new for you" (Feature/Fix/Data) and "Under the hood" (Docs/Chore) populated at once.
_MIXED_ENTRY = {
    "date": "2026-08-15",
    "headline": "Streamlined interface and enhanced functionality",
    "summary": "Smarter caching, and a new multi-transfer feature.",
    "changes": [
        {"category": "Feature", "title": "Support multiple free transfers", "description": "Search now allows more transfers."},
        {"category": "Chore", "title": "Introduce CI for automated testing", "description": "Runs tests on every pull request."},
    ],
}


class ComposeReleaseNotesEmailTests(unittest.TestCase):
    def test_headline_is_in_the_subject(self):
        subject, _, _ = compose_release_notes_email(_SAMPLE_ENTRY, "https://example.com/unsub")
        self.assertIn(_SAMPLE_ENTRY["headline"], subject)

    def test_every_change_and_the_unsubscribe_link_appear_in_the_text_body(self):
        _, text_body, _ = compose_release_notes_email(
            _SAMPLE_ENTRY, "https://example.com/unsub?token=xyz"
        )
        self.assertIn("Split filters", text_body)
        self.assertIn("Three controls now.", text_body)
        self.assertIn("[Feature]", text_body)
        self.assertIn("https://example.com/unsub?token=xyz", text_body)

    def test_html_body_is_well_formed_and_carries_every_change_plus_the_unsubscribe_link(self):
        _, _, html_body = compose_release_notes_email(
            _MIXED_ENTRY, "https://example.com/unsub?token=xyz"
        )
        self.assertTrue(html_body.startswith("<!doctype html>"))
        self.assertIn("Support multiple free transfers", html_body)
        self.assertIn("Introduce CI for automated testing", html_body)
        self.assertIn('href="https://example.com/unsub?token=xyz"', html_body)

    def test_html_body_splits_changes_into_the_for_you_and_under_the_hood_sections(self):
        """Issue #190's decided design: Feature/Fix/Data render under "What's new for you",
        Docs/Chore render under "Under the hood" -- both with the same badge/card weight, per
        the review feedback that an earlier de-emphasized pass wrongly read as "unimportant"."""
        _, _, html_body = compose_release_notes_email(_MIXED_ENTRY, "https://example.com/unsub")
        for_you_index = html_body.index("What's new for you")
        under_the_hood_index = html_body.index("Under the hood")
        feature_index = html_body.index("Support multiple free transfers")
        chore_index = html_body.index("Introduce CI for automated testing")
        self.assertLess(for_you_index, feature_index)
        self.assertLess(feature_index, under_the_hood_index)
        self.assertLess(under_the_hood_index, chore_index)

    def test_html_body_omits_a_section_with_no_matching_changes(self):
        """An all-Feature entry (no Docs/Chore changes) must not render an empty "Under the
        hood" section."""
        _, _, html_body = compose_release_notes_email(_SAMPLE_ENTRY, "https://example.com/unsub")
        self.assertIn("What's new for you", html_body)
        self.assertNotIn("Under the hood", html_body)

    def test_html_body_escapes_change_titles_and_descriptions(self):
        entry = {
            "date": "2026-08-11",
            "headline": "Headline",
            "summary": "Summary",
            "changes": [
                {"category": "Feature", "title": "<script>alert(1)</script>", "description": "safe & sound"},
            ],
        }
        _, _, html_body = compose_release_notes_email(entry, "https://example.com/unsub")
        self.assertNotIn("<script>alert(1)</script>", html_body)
        self.assertIn("&lt;script&gt;", html_body)
        self.assertIn("safe &amp; sound", html_body)


class SendReleaseNotesEmailTests(unittest.TestCase):
    def _fake_smtp(self):
        instance = MagicMock()
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        return instance

    def test_sends_to_the_subscriber(self):
        fake_smtp = self._fake_smtp()
        with patch("fpl_intel.reminder_confirmation.smtplib.SMTP", return_value=fake_smtp):
            send_release_notes_email(
                "subscriber@example.com", _SAMPLE_ENTRY, "https://example.com/unsub",
                smtp_config=_SMTP_CONFIG,
            )
        sent_message = fake_smtp.send_message.call_args[0][0]
        self.assertEqual(sent_message["To"], "subscriber@example.com")

    def test_sent_message_is_multipart_alternative_with_both_parts_present(self):
        """Issue #190: same `set_content()` then `add_alternative()` contract as #83's
        `send_deadline_reminder.py.send_email()` -- `text/plain` first (the fallback), then
        `text/html`."""
        fake_smtp = self._fake_smtp()
        with patch("fpl_intel.reminder_confirmation.smtplib.SMTP", return_value=fake_smtp):
            send_release_notes_email(
                "subscriber@example.com", _SAMPLE_ENTRY, "https://example.com/unsub",
                smtp_config=_SMTP_CONFIG,
            )
        sent_message = fake_smtp.send_message.call_args[0][0]
        self.assertTrue(sent_message.is_multipart())
        content_types = [part.get_content_type() for part in sent_message.walk()]
        self.assertIn("text/plain", content_types)
        self.assertIn("text/html", content_types)
        self.assertLess(content_types.index("text/plain"), content_types.index("text/html"))

    def test_smtp_failure_is_turned_into_reminder_email_error(self):
        with patch(
            "fpl_intel.reminder_confirmation.smtplib.SMTP",
            side_effect=smtplib.SMTPException("boom"),
        ):
            with self.assertRaises(ReminderEmailError):
                send_release_notes_email(
                    "subscriber@example.com", _SAMPLE_ENTRY, "https://example.com/unsub",
                    smtp_config=_SMTP_CONFIG,
                )


if __name__ == "__main__":
    unittest.main()
