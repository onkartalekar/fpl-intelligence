from pathlib import Path
import tempfile
import unittest

from fpl_intel.release_notes_subscribers import confirm, list_confirmed, load, set_pending, unsubscribe


_NOW = "2026-08-11T12:00:00+00:00"


class ReleaseNotesSubscribersTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "subscribers.db"

    def tearDown(self):
        self.directory.cleanup()

    def test_set_pending_creates_a_pending_row(self):
        set_pending(self.db_path, "a@example.com", "hash1", "2026-08-12T12:00:00+00:00", _NOW)

        row = load(self.db_path, "a@example.com")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["confirm_token_hash"], "hash1")

    def test_set_pending_twice_refreshes_the_token(self):
        set_pending(self.db_path, "a@example.com", "hash1", "2026-08-12T12:00:00+00:00", _NOW)
        set_pending(self.db_path, "a@example.com", "hash2", "2026-08-13T12:00:00+00:00", _NOW)

        row = load(self.db_path, "a@example.com")
        self.assertEqual(row["confirm_token_hash"], "hash2")

    def test_set_pending_never_downgrades_an_already_confirmed_row(self):
        set_pending(self.db_path, "a@example.com", "hash1", "2026-08-12T12:00:00+00:00", _NOW)
        confirm(self.db_path, "a@example.com", "unsub-token", _NOW)

        set_pending(self.db_path, "a@example.com", "hash-new", "2026-08-20T12:00:00+00:00", _NOW)

        row = load(self.db_path, "a@example.com")
        self.assertEqual(row["status"], "confirmed")
        self.assertIsNone(row["confirm_token_hash"])

    def test_confirm_sets_status_and_stores_unsubscribe_token(self):
        set_pending(self.db_path, "a@example.com", "hash1", "2026-08-12T12:00:00+00:00", _NOW)

        confirm(self.db_path, "a@example.com", "unsub-token-xyz", _NOW)

        row = load(self.db_path, "a@example.com")
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(row["unsubscribe_token"], "unsub-token-xyz")
        self.assertIsNone(row["confirm_token_hash"])
        self.assertIsNone(row["confirm_expires_at"])

    def test_load_missing_email_returns_none(self):
        self.assertIsNone(load(self.db_path, "nobody@example.com"))

    def test_unsubscribe_removes_the_row_entirely(self):
        set_pending(self.db_path, "a@example.com", "hash1", "2026-08-12T12:00:00+00:00", _NOW)
        confirm(self.db_path, "a@example.com", "unsub-token", _NOW)

        unsubscribe(self.db_path, "a@example.com")

        self.assertIsNone(load(self.db_path, "a@example.com"))

    def test_list_confirmed_only_returns_confirmed_rows(self):
        set_pending(self.db_path, "pending@example.com", "hash1", "2026-08-12T12:00:00+00:00", _NOW)
        set_pending(self.db_path, "confirmed@example.com", "hash2", "2026-08-12T12:00:00+00:00", _NOW)
        confirm(self.db_path, "confirmed@example.com", "unsub-token", _NOW)

        confirmed = list_confirmed(self.db_path)

        self.assertEqual(confirmed, [{"email": "confirmed@example.com", "unsubscribe_token": "unsub-token"}])

    def test_list_confirmed_returns_empty_list_with_no_subscribers(self):
        self.assertEqual(list_confirmed(self.db_path), [])


if __name__ == "__main__":
    unittest.main()
