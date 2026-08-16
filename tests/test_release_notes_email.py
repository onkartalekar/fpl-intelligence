"""Tests for `release_notes_email.py`'s subscription-confirmation and release-notes digest email
composition/SMTP send logic (issue #143, issue #190) -- split out of `test_reminder_confirmation.py`
when the module itself split (see `release_notes_email.py`'s module docstring for why). Mocks
`smtplib.SMTP` so no real network call is ever made.
"""

import smtplib
import unittest
from unittest.mock import MagicMock, patch

from fpl_intel.reminder_confirmation import ReminderEmailError
from fpl_intel.release_notes_email import (
    compose_release_notes_email,
    compose_release_notes_subscription_email,
    send_release_notes_email,
    send_release_notes_subscription_email,
)


_SMTP_CONFIG = {
    "host": "smtp.example.com", "port": 587,
    "user": "operator@example.com", "password": "hunter2",
}


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
        with patch("fpl_intel.release_notes_email.smtplib.SMTP", return_value=fake_smtp):
            send_release_notes_subscription_email(
                "visitor@example.com", "https://example.com/confirm", smtp_config=_SMTP_CONFIG,
            )
        sent_message = fake_smtp.send_message.call_args[0][0]
        self.assertEqual(sent_message["To"], "visitor@example.com")

    def test_smtp_failure_is_turned_into_reminder_email_error(self):
        with patch(
            "fpl_intel.release_notes_email.smtplib.SMTP",
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
        {"category": "Feature", "audience": "user", "title": "Split filters", "description": "Three controls now."},
    ],
}

# A mixed entry for the #196 audience-based split -- deliberately chosen so category alone would
# route these wrong: a `Fix` marked `developer` (must land in "Under the hood" despite `Fix`
# being #190's original "for you" category) and a `Chore` marked `user` (must land in "What's
# new for you" despite `Chore` being #190's original "under the hood" category). Proves the
# split now keys on `audience`, not `category`.
_MIXED_ENTRY = {
    "date": "2026-08-15",
    "headline": "Streamlined interface and enhanced functionality",
    "summary": "Smarter caching, and a new multi-transfer feature.",
    "changes": [
        {
            "category": "Feature", "audience": "user",
            "title": "Support multiple free transfers", "description": "Search now allows more transfers.",
        },
        {
            "category": "Fix", "audience": "developer",
            "title": "Catch socket timeout errors more effectively", "description": "Internal robustness only.",
        },
        {
            "category": "Chore", "audience": "user",
            "title": "Speed up dashboard page loads", "description": "Managers will notice faster loads.",
        },
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
        self.assertIn("Catch socket timeout errors more effectively", html_body)
        self.assertIn("Speed up dashboard page loads", html_body)
        self.assertIn('href="https://example.com/unsub?token=xyz"', html_body)

    def test_html_body_splits_by_audience_not_category(self):
        """Issue #196: the split keys on each change's own `audience`, not `category`. Proven
        with `_MIXED_ENTRY`'s deliberately-crossed fixture -- a `Fix`/`developer` change lands in
        "Under the hood" (despite `Fix` being #190's original "for you" category), and a
        `Chore`/`user` change lands in "What's new for you" (despite `Chore` being #190's
        original "under the hood" category)."""
        _, _, html_body = compose_release_notes_email(_MIXED_ENTRY, "https://example.com/unsub")
        for_you_index = html_body.index("What's new for you")
        under_the_hood_index = html_body.index("Under the hood")
        feature_user_index = html_body.index("Support multiple free transfers")
        fix_developer_index = html_body.index("Catch socket timeout errors more effectively")
        chore_user_index = html_body.index("Speed up dashboard page loads")

        # Feature/user and Chore/user both land in "for you" -- category doesn't matter here.
        self.assertLess(for_you_index, feature_user_index)
        self.assertLess(for_you_index, chore_user_index)
        self.assertLess(feature_user_index, under_the_hood_index)
        self.assertLess(chore_user_index, under_the_hood_index)
        # Fix/developer lands in "under the hood" despite being a Fix.
        self.assertLess(under_the_hood_index, fix_developer_index)

    def test_html_body_omits_a_section_with_no_matching_changes(self):
        """An entry with only `user`-audience changes must not render an empty "Under the
        hood" section."""
        _, _, html_body = compose_release_notes_email(_SAMPLE_ENTRY, "https://example.com/unsub")
        self.assertIn("What's new for you", html_body)
        self.assertNotIn("Under the hood", html_body)

    def test_html_body_defaults_missing_audience_to_user(self):
        """Entries published before issue #196 (all of history, by explicit decision -- not
        worth backfilling) never carry `audience` at all. A missing field must default to `user`
        rather than raising or silently dropping the change."""
        entry = {
            "date": "2026-08-11",
            "headline": "Headline",
            "summary": "Summary",
            "changes": [
                {"category": "Feature", "title": "Pre-#196 change", "description": "No audience field."},
            ],
        }
        _, _, html_body = compose_release_notes_email(entry, "https://example.com/unsub")
        self.assertIn("What's new for you", html_body)
        self.assertIn("Pre-#196 change", html_body)
        self.assertNotIn("Under the hood", html_body)

    def test_html_body_escapes_change_titles_and_descriptions(self):
        entry = {
            "date": "2026-08-11",
            "headline": "Headline",
            "summary": "Summary",
            "changes": [
                {"category": "Feature", "audience": "user", "title": "<script>alert(1)</script>", "description": "safe & sound"},
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
        with patch("fpl_intel.release_notes_email.smtplib.SMTP", return_value=fake_smtp):
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
        with patch("fpl_intel.release_notes_email.smtplib.SMTP", return_value=fake_smtp):
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
            "fpl_intel.release_notes_email.smtplib.SMTP",
            side_effect=smtplib.SMTPException("boom"),
        ):
            with self.assertRaises(ReminderEmailError):
                send_release_notes_email(
                    "subscriber@example.com", _SAMPLE_ENTRY, "https://example.com/unsub",
                    smtp_config=_SMTP_CONFIG,
                )


if __name__ == "__main__":
    unittest.main()
