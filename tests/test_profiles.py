from pathlib import Path
import tempfile
import unittest

from fpl_intel.profiles import (
    load_pin_hash, load_profile, save_draft_squad, save_profile, set_lookup_opt_out,
)


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
            now="2026-08-08T00:00:00Z", goal="top_50k",
        )

        self.assertEqual(row["team_id"], 364759)
        self.assertEqual(row["timezone"], "America/New_York")
        self.assertEqual(row["risk_profile"], "balanced")
        self.assertIsNone(row["confirmed_free_transfers"])
        self.assertIsNone(row["email"])
        self.assertIsNone(row["draft_squad"])
        self.assertEqual(row["goal"], "top_50k")
        self.assertEqual(row["created_at"], "2026-08-08T00:00:00Z")
        self.assertEqual(row["updated_at"], "2026-08-08T00:00:00Z")
        self.assertEqual(load_profile(self.db_path, 364759), row)

    def test_saving_again_updates_in_place_and_preserves_created_at(self):
        first = save_profile(
            self.db_path, team_id=1, timezone="UTC", risk_profile="balanced",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T00:00:00Z", goal="top_50k",
        )

        second = save_profile(
            self.db_path, team_id=1, timezone="Europe/London", risk_profile="aggressive",
            confirmed_free_transfers=2, confirmed_free_transfers_event=5,
            now="2026-08-09T00:00:00Z", goal="top_10k",
        )

        self.assertEqual(second["created_at"], first["created_at"])
        self.assertEqual(second["updated_at"], "2026-08-09T00:00:00Z")
        self.assertEqual(second["timezone"], "Europe/London")
        self.assertEqual(second["risk_profile"], "aggressive")
        self.assertEqual(second["confirmed_free_transfers"], 2)
        self.assertEqual(second["confirmed_free_transfers_event"], 5)
        self.assertEqual(second["goal"], "top_10k")

    def test_different_team_ids_are_kept_fully_independent(self):
        save_profile(
            self.db_path, team_id=1, timezone="UTC", risk_profile="conservative",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T00:00:00Z", goal="top_10k",
        )
        save_profile(
            self.db_path, team_id=2, timezone="Asia/Tokyo", risk_profile="aggressive",
            confirmed_free_transfers=3, confirmed_free_transfers_event=4,
            now="2026-08-08T00:00:00Z", goal="just_for_fun",
        )

        self.assertEqual(load_profile(self.db_path, 1)["risk_profile"], "conservative")
        self.assertEqual(load_profile(self.db_path, 2)["risk_profile"], "aggressive")
        self.assertEqual(load_profile(self.db_path, 1)["goal"], "top_10k")
        self.assertEqual(load_profile(self.db_path, 2)["goal"], "just_for_fun")

    def test_saving_never_writes_email_which_stays_null_until_a_future_opt_in(self):
        row = save_profile(
            self.db_path, team_id=1, timezone="UTC", risk_profile="balanced",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T00:00:00Z", goal="top_50k",
        )

        self.assertIsNone(row["email"])

    def test_creates_the_database_file_and_parent_directory_on_first_use(self):
        nested_db_path = Path(self.directory.name) / "nested" / "profiles.db"

        save_profile(
            nested_db_path, team_id=1, timezone="UTC", risk_profile="balanced",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T00:00:00Z", goal="top_50k",
        )

        self.assertTrue(nested_db_path.exists())

    def test_saves_and_loads_a_draft_squad_for_a_brand_new_team_id(self):
        draft_ids = list(range(1, 16))

        row = save_draft_squad(self.db_path, team_id=99, draft_squad_ids=draft_ids, now="2026-08-08T00:00:00Z")

        self.assertEqual(row["team_id"], 99)
        self.assertEqual(row["draft_squad"], draft_ids)
        # Seeded with the same defaults the dashboard already applies for an unconfigured visitor.
        self.assertEqual(row["timezone"], "America/New_York")
        self.assertEqual(row["risk_profile"], "balanced")
        # No profile has ever been saved for this team -- `goal` resolves to the read-time default.
        self.assertEqual(row["goal"], "top_50k")
        self.assertEqual(load_profile(self.db_path, 99), row)

    def test_saving_a_draft_squad_preserves_an_existing_profile(self):
        save_profile(
            self.db_path, team_id=5, timezone="Europe/London", risk_profile="aggressive",
            confirmed_free_transfers=2, confirmed_free_transfers_event=3,
            now="2026-08-01T00:00:00Z", goal="top_10k",
        )

        row = save_draft_squad(self.db_path, team_id=5, draft_squad_ids=list(range(1, 16)), now="2026-08-08T00:00:00Z")

        self.assertEqual(row["timezone"], "Europe/London")
        self.assertEqual(row["risk_profile"], "aggressive")
        self.assertEqual(row["confirmed_free_transfers"], 2)
        self.assertEqual(row["confirmed_free_transfers_event"], 3)
        self.assertEqual(row["created_at"], "2026-08-01T00:00:00Z")
        self.assertEqual(row["draft_squad"], list(range(1, 16)))
        # Issue #78: saving a draft squad must not silently clobber a previously chosen goal.
        self.assertEqual(row["goal"], "top_10k")

    def test_saving_a_profile_preserves_an_existing_draft_squad(self):
        save_draft_squad(self.db_path, team_id=7, draft_squad_ids=list(range(1, 16)), now="2026-08-01T00:00:00Z")

        row = save_profile(
            self.db_path, team_id=7, timezone="UTC", risk_profile="conservative",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T00:00:00Z", goal="beat_last_season",
        )

        self.assertEqual(row["draft_squad"], list(range(1, 16)))
        self.assertEqual(row["risk_profile"], "conservative")
        self.assertEqual(row["goal"], "beat_last_season")

    def test_saving_none_clears_a_previously_saved_draft_squad(self):
        save_draft_squad(self.db_path, team_id=8, draft_squad_ids=list(range(1, 16)), now="2026-08-01T00:00:00Z")

        row = save_draft_squad(self.db_path, team_id=8, draft_squad_ids=None, now="2026-08-08T00:00:00Z")

        self.assertIsNone(row["draft_squad"])

    def test_new_teams_have_no_opt_out_flag_or_pin_until_touched(self):
        row = save_profile(
            self.db_path, team_id=1, timezone="UTC", risk_profile="balanced",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T00:00:00Z", goal="top_50k",
        )

        self.assertIsNone(row["opted_out"])
        self.assertIsNone(row["pin_hash"])
        self.assertIsNone(load_pin_hash(self.db_path, 1))


class GoalFieldTests(unittest.TestCase):
    """Issue #78: a manager's stated season objective, metadata-only for now.

    Covers the read-time default substitution and -- the change's main risk, per the issue's
    own review -- that the two *other* write paths (`save_draft_squad`, `set_lookup_opt_out`)
    never silently null out a goal that was set through the ordinary profile save.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "profiles.db"

    def tearDown(self):
        self.directory.cleanup()

    def test_a_brand_new_row_with_no_goal_ever_saved_defaults_to_top_50k(self):
        # Created via the opt-out endpoint, never touching the profile form -- exactly the case
        # `_row_to_dict`'s read-time default exists for.
        row = set_lookup_opt_out(
            self.db_path, team_id=1, opted_out=True, pin_hash="hash-one",
            now="2026-08-08T00:00:00Z",
        )

        self.assertEqual(row["goal"], "top_50k")

    def test_goal_survives_a_later_draft_squad_save_that_does_not_touch_it(self):
        save_profile(
            self.db_path, team_id=5, timezone="UTC", risk_profile="balanced",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-01T00:00:00Z", goal="top_10k",
        )

        row = save_draft_squad(
            self.db_path, team_id=5, draft_squad_ids=list(range(1, 16)), now="2026-08-08T00:00:00Z"
        )

        self.assertEqual(row["goal"], "top_10k")
        self.assertEqual(load_profile(self.db_path, 5)["goal"], "top_10k")

    def test_goal_survives_a_later_lookup_opt_out_toggle_that_does_not_touch_it(self):
        save_profile(
            self.db_path, team_id=6, timezone="UTC", risk_profile="balanced",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-01T00:00:00Z", goal="beat_last_season",
        )

        row = set_lookup_opt_out(
            self.db_path, team_id=6, opted_out=True, pin_hash="hash-one",
            now="2026-08-08T00:00:00Z",
        )

        self.assertEqual(row["goal"], "beat_last_season")
        self.assertEqual(load_profile(self.db_path, 6)["goal"], "beat_last_season")

    def test_saving_a_profile_again_updates_goal_directly(self):
        save_profile(
            self.db_path, team_id=9, timezone="UTC", risk_profile="balanced",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-01T00:00:00Z", goal="just_for_fun",
        )

        row = save_profile(
            self.db_path, team_id=9, timezone="UTC", risk_profile="balanced",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T00:00:00Z", goal="top_100k",
        )

        self.assertEqual(row["goal"], "top_100k")


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
            now="2026-08-08T00:00:00Z", goal="top_50k",
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
            now="2026-08-08T00:00:00Z", goal="top_50k",
        )
        set_lookup_opt_out(
            self.db_path, team_id=1, opted_out=True, pin_hash="hash-one",
            now="2026-08-08T01:00:00Z",
        )

        row = save_profile(
            self.db_path, team_id=1, timezone="UTC", risk_profile="aggressive",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T02:00:00Z", goal="top_50k",
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
