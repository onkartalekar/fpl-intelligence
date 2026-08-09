from pathlib import Path
import tempfile
import unittest

from fpl_intel.profiles import load_profile, save_profile


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


if __name__ == "__main__":
    unittest.main()
