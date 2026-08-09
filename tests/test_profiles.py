from pathlib import Path
import tempfile
import unittest

from fpl_intel.profiles import load_pin_hash, load_profile, save_profile, set_lookup_opt_out


class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "profiles.db"

    def tearDown(self):
        self.directory.cleanup()

    def test_loading_an_unsaved_team_id_returns_none(self):
        self.assertIsNone(load_profile(self.db_path, 12345))

    def test_saves_and_loads_a_new_profile(self):
        row = save_profile(
            self.db_path, team_id=364759, timezone="America/New_York", risk_profile="balanced",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T00:00:00Z",
        )

        self.assertEqual(row["team_id"], 364759)
        self.assertEqual(row["timezone"], "America/New_York")
        self.assertEqual(row["risk_profile"], "balanced")
        self.assertIsNone(row["confirmed_free_transfers"])
        self.assertIsNone(row["email"])
        self.assertEqual(row["created_at"], "2026-08-08T00:00:00Z")
        self.assertEqual(row["updated_at"], "2026-08-08T00:00:00Z")
        self.assertEqual(load_profile(self.db_path, 364759), row)

    def test_saving_again_updates_in_place_and_preserves_created_at(self):
        first = save_profile(
            self.db_path, team_id=1, timezone="UTC", risk_profile="balanced",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T00:00:00Z",
        )

        second = save_profile(
            self.db_path, team_id=1, timezone="Europe/London", risk_profile="aggressive",
            confirmed_free_transfers=2, confirmed_free_transfers_event=5,
            now="2026-08-09T00:00:00Z",
        )

        self.assertEqual(second["created_at"], first["created_at"])
        self.assertEqual(second["updated_at"], "2026-08-09T00:00:00Z")
        self.assertEqual(second["timezone"], "Europe/London")
        self.assertEqual(second["risk_profile"], "aggressive")
        self.assertEqual(second["confirmed_free_transfers"], 2)
        self.assertEqual(second["confirmed_free_transfers_event"], 5)

    def test_different_team_ids_are_kept_fully_independent(self):
        save_profile(
            self.db_path, team_id=1, timezone="UTC", risk_profile="conservative",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T00:00:00Z",
        )
        save_profile(
            self.db_path, team_id=2, timezone="Asia/Tokyo", risk_profile="aggressive",
            confirmed_free_transfers=3, confirmed_free_transfers_event=4,
            now="2026-08-08T00:00:00Z",
        )

        self.assertEqual(load_profile(self.db_path, 1)["risk_profile"], "conservative")
        self.assertEqual(load_profile(self.db_path, 2)["risk_profile"], "aggressive")

    def test_saving_never_writes_email_which_stays_null_until_a_future_opt_in(self):
        row = save_profile(
            self.db_path, team_id=1, timezone="UTC", risk_profile="balanced",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T00:00:00Z",
        )

        self.assertIsNone(row["email"])

    def test_creates_the_database_file_and_parent_directory_on_first_use(self):
        nested_db_path = Path(self.directory.name) / "nested" / "profiles.db"

        save_profile(
            nested_db_path, team_id=1, timezone="UTC", risk_profile="balanced",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T00:00:00Z",
        )

        self.assertTrue(nested_db_path.exists())

    def test_new_teams_have_no_opt_out_flag_or_pin_until_touched(self):
        row = save_profile(
            self.db_path, team_id=1, timezone="UTC", risk_profile="balanced",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T00:00:00Z",
        )

        self.assertIsNone(row["opted_out"])
        self.assertIsNone(row["pin_hash"])
        self.assertIsNone(load_pin_hash(self.db_path, 1))


class LookupOptOutStoreTests(unittest.TestCase):
    """Issue #62's opt-out flag/PIN storage, layered onto #45's profiles table."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "profiles.db"

    def tearDown(self):
        self.directory.cleanup()

    def test_load_pin_hash_is_none_for_a_team_with_no_row_at_all(self):
        self.assertIsNone(load_pin_hash(self.db_path, 999))

    def test_first_claim_creates_a_row_with_default_preferences(self):
        row = set_lookup_opt_out(
            self.db_path, team_id=364759, opted_out=True, pin_hash="abc123",
            now="2026-08-08T00:00:00Z",
        )

        self.assertEqual(row["team_id"], 364759)
        self.assertTrue(row["opted_out"])
        self.assertEqual(row["pin_hash"], "abc123")
        self.assertEqual(row["timezone"], "America/New_York")
        self.assertEqual(row["risk_profile"], "balanced")
        self.assertIsNone(row["email"])
        self.assertEqual(row["created_at"], "2026-08-08T00:00:00Z")
        self.assertEqual(load_pin_hash(self.db_path, 364759), "abc123")

    def test_toggling_again_preserves_the_pin_hash_and_other_preferences(self):
        save_profile(
            self.db_path, team_id=1, timezone="Europe/London", risk_profile="aggressive",
            confirmed_free_transfers=2, confirmed_free_transfers_event=5,
            now="2026-08-08T00:00:00Z",
        )
        set_lookup_opt_out(
            self.db_path, team_id=1, opted_out=True, pin_hash="hash-one",
            now="2026-08-08T01:00:00Z",
        )

        row = set_lookup_opt_out(
            self.db_path, team_id=1, opted_out=False, pin_hash="hash-one",
            now="2026-08-08T02:00:00Z",
        )

        self.assertFalse(row["opted_out"])
        self.assertEqual(row["pin_hash"], "hash-one")
        self.assertEqual(row["timezone"], "Europe/London")
        self.assertEqual(row["risk_profile"], "aggressive")
        self.assertEqual(row["confirmed_free_transfers"], 2)
        self.assertEqual(row["updated_at"], "2026-08-08T02:00:00Z")

    def test_does_not_disturb_a_team_saved_by_the_ordinary_profile_endpoint(self):
        """A normal /api/profile save (`save_profile`) must never populate opt-out columns."""
        save_profile(
            self.db_path, team_id=1, timezone="UTC", risk_profile="balanced",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T00:00:00Z",
        )
        set_lookup_opt_out(
            self.db_path, team_id=1, opted_out=True, pin_hash="hash-one",
            now="2026-08-08T01:00:00Z",
        )

        row = save_profile(
            self.db_path, team_id=1, timezone="UTC", risk_profile="aggressive",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T02:00:00Z",
        )

        self.assertTrue(row["opted_out"])
        self.assertEqual(row["pin_hash"], "hash-one")

    def test_different_team_ids_keep_independent_pins(self):
        set_lookup_opt_out(
            self.db_path, team_id=1, opted_out=True, pin_hash="hash-one",
            now="2026-08-08T00:00:00Z",
        )
        set_lookup_opt_out(
            self.db_path, team_id=2, opted_out=True, pin_hash="hash-two",
            now="2026-08-08T00:00:00Z",
        )

        self.assertEqual(load_pin_hash(self.db_path, 1), "hash-one")
        self.assertEqual(load_pin_hash(self.db_path, 2), "hash-two")


if __name__ == "__main__":
    unittest.main()
