from pathlib import Path
import tempfile
import unittest

from fpl_intel.profiles import (
    confirm_reminder, load_pin_hash, load_profile, save_draft_squad, save_profile,
    set_lookup_opt_out, set_reminder_decision, set_reminder_pending,
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
            now="2026-08-08T00:00:00Z",
        )

        self.assertEqual(row["team_id"], 364759)
        self.assertEqual(row["timezone"], "America/New_York")
        self.assertEqual(row["risk_profile"], "balanced")
        self.assertIsNone(row["confirmed_free_transfers"])
        self.assertIsNone(row["email"])
        self.assertIsNone(row["draft_squad"])
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

    def test_saves_and_loads_a_draft_squad_for_a_brand_new_team_id(self):
        draft_ids = list(range(1, 16))

        row = save_draft_squad(self.db_path, team_id=99, draft_squad_ids=draft_ids, now="2026-08-08T00:00:00Z")

        self.assertEqual(row["team_id"], 99)
        self.assertEqual(row["draft_squad"], draft_ids)
        # Seeded with the same defaults the dashboard already applies for an unconfigured visitor.
        self.assertEqual(row["timezone"], "America/New_York")
        self.assertEqual(row["risk_profile"], "balanced")
        self.assertEqual(load_profile(self.db_path, 99), row)

    def test_saving_a_draft_squad_preserves_an_existing_profile(self):
        save_profile(
            self.db_path, team_id=5, timezone="Europe/London", risk_profile="aggressive",
            confirmed_free_transfers=2, confirmed_free_transfers_event=3,
            now="2026-08-01T00:00:00Z",
        )

        row = save_draft_squad(self.db_path, team_id=5, draft_squad_ids=list(range(1, 16)), now="2026-08-08T00:00:00Z")

        self.assertEqual(row["timezone"], "Europe/London")
        self.assertEqual(row["risk_profile"], "aggressive")
        self.assertEqual(row["confirmed_free_transfers"], 2)
        self.assertEqual(row["confirmed_free_transfers_event"], 3)
        self.assertEqual(row["created_at"], "2026-08-01T00:00:00Z")
        self.assertEqual(row["draft_squad"], list(range(1, 16)))

    def test_saving_a_profile_preserves_an_existing_draft_squad(self):
        save_draft_squad(self.db_path, team_id=7, draft_squad_ids=list(range(1, 16)), now="2026-08-01T00:00:00Z")

        row = save_profile(
            self.db_path, team_id=7, timezone="UTC", risk_profile="conservative",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T00:00:00Z",
        )

        self.assertEqual(row["draft_squad"], list(range(1, 16)))
        self.assertEqual(row["risk_profile"], "conservative")

    def test_saving_none_clears_a_previously_saved_draft_squad(self):
        save_draft_squad(self.db_path, team_id=8, draft_squad_ids=list(range(1, 16)), now="2026-08-01T00:00:00Z")

        row = save_draft_squad(self.db_path, team_id=8, draft_squad_ids=None, now="2026-08-08T00:00:00Z")

        self.assertIsNone(row["draft_squad"])

    def test_new_teams_have_no_opt_out_flag_or_pin_until_touched(self):
        row = save_profile(
            self.db_path, team_id=1, timezone="UTC", risk_profile="balanced",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T00:00:00Z",
        )

        self.assertIsNone(row["opted_out"])
        self.assertIsNone(row["pin_hash"])
        self.assertIsNone(load_pin_hash(self.db_path, 1))

    def test_new_teams_have_no_reminder_decision_until_touched(self):
        """Issue #79: `reminder_status` is None (never decided) -- an ordinary /api/profile save
        must never populate reminder fields."""
        row = save_profile(
            self.db_path, team_id=1, timezone="UTC", risk_profile="balanced",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T00:00:00Z",
        )

        self.assertIsNone(row["reminder_status"])
        self.assertIsNone(row["reminder_lead_hours"])
        self.assertIsNone(row["reminder_pending_email"])
        self.assertIsNone(row["reminder_confirmation_token_hash"])
        self.assertIsNone(row["reminder_confirmation_expires_at"])


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


class ReminderStoreTests(unittest.TestCase):
    """Issue #79's reminder-opt-in schema and its three write functions, layered onto #45's
    profiles table the same way #62's opted_out/pin_hash already are."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "profiles.db"

    def tearDown(self):
        self.directory.cleanup()

    def test_set_reminder_pending_creates_a_row_with_default_preferences(self):
        row = set_reminder_pending(
            self.db_path, team_id=364759, pending_email="manager@example.com", lead_hours=3,
            token_hash="tokenhash", expires_at="2026-08-10T00:00:00+00:00",
            now="2026-08-09T00:00:00+00:00",
        )

        self.assertEqual(row["reminder_status"], "pending")
        self.assertEqual(row["reminder_pending_email"], "manager@example.com")
        self.assertEqual(row["reminder_lead_hours"], 3)
        self.assertEqual(row["reminder_confirmation_token_hash"], "tokenhash")
        self.assertEqual(row["reminder_confirmation_expires_at"], "2026-08-10T00:00:00+00:00")
        # `email` stays untouched -- only ever holds a *confirmed* address.
        self.assertIsNone(row["email"])
        self.assertEqual(row["timezone"], "America/New_York")
        self.assertEqual(row["risk_profile"], "balanced")

    def test_set_reminder_pending_preserves_an_existing_profile(self):
        save_profile(
            self.db_path, team_id=5, timezone="Europe/London", risk_profile="aggressive",
            confirmed_free_transfers=2, confirmed_free_transfers_event=3,
            now="2026-08-01T00:00:00Z",
        )

        row = set_reminder_pending(
            self.db_path, team_id=5, pending_email="a@b.com", lead_hours=12,
            token_hash="hash", expires_at="2026-08-10T00:00:00+00:00",
            now="2026-08-08T00:00:00Z",
        )

        self.assertEqual(row["timezone"], "Europe/London")
        self.assertEqual(row["risk_profile"], "aggressive")
        self.assertEqual(row["confirmed_free_transfers"], 2)
        self.assertEqual(row["reminder_status"], "pending")

    def test_set_reminder_pending_overwrites_a_previous_pending_request(self):
        set_reminder_pending(
            self.db_path, team_id=1, pending_email="first@example.com", lead_hours=3,
            token_hash="hash-one", expires_at="2026-08-10T00:00:00+00:00",
            now="2026-08-08T00:00:00Z",
        )

        row = set_reminder_pending(
            self.db_path, team_id=1, pending_email="second@example.com", lead_hours=24,
            token_hash="hash-two", expires_at="2026-08-11T00:00:00+00:00",
            now="2026-08-09T00:00:00Z",
        )

        self.assertEqual(row["reminder_pending_email"], "second@example.com")
        self.assertEqual(row["reminder_lead_hours"], 24)
        self.assertEqual(row["reminder_confirmation_token_hash"], "hash-two")

    def test_confirm_reminder_promotes_pending_email_and_clears_token_fields(self):
        set_reminder_pending(
            self.db_path, team_id=1, pending_email="a@b.com", lead_hours=3,
            token_hash="hash", expires_at="2026-08-10T00:00:00+00:00",
            now="2026-08-08T00:00:00Z",
        )

        row = confirm_reminder(self.db_path, team_id=1, now="2026-08-08T01:00:00Z")

        self.assertEqual(row["reminder_status"], "enabled")
        self.assertEqual(row["email"], "a@b.com")
        self.assertIsNone(row["reminder_pending_email"])
        self.assertIsNone(row["reminder_confirmation_token_hash"])
        self.assertIsNone(row["reminder_confirmation_expires_at"])
        # The lead-time choice survives confirmation.
        self.assertEqual(row["reminder_lead_hours"], 3)

    def test_confirm_reminder_returns_none_when_there_is_no_pending_request(self):
        self.assertIsNone(confirm_reminder(self.db_path, team_id=999, now="2026-08-08T00:00:00Z"))

        save_profile(
            self.db_path, team_id=2, timezone="UTC", risk_profile="balanced",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-08T00:00:00Z",
        )
        self.assertIsNone(confirm_reminder(self.db_path, team_id=2, now="2026-08-08T00:00:00Z"))

    def test_confirm_reminder_is_not_reusable_once_already_confirmed(self):
        """A confirm link can't be clicked twice -- the second attempt finds no pending request
        left to promote (the pending fields were already cleared by the first confirm)."""
        set_reminder_pending(
            self.db_path, team_id=1, pending_email="a@b.com", lead_hours=3,
            token_hash="hash", expires_at="2026-08-10T00:00:00+00:00",
            now="2026-08-08T00:00:00Z",
        )
        confirm_reminder(self.db_path, team_id=1, now="2026-08-08T01:00:00Z")

        second_attempt = confirm_reminder(self.db_path, team_id=1, now="2026-08-08T02:00:00Z")

        self.assertIsNone(second_attempt)
        self.assertEqual(load_profile(self.db_path, 1)["reminder_status"], "enabled")

    def test_set_reminder_decision_declines_without_touching_email(self):
        row = set_reminder_decision(
            self.db_path, team_id=1, status="declined", now="2026-08-08T00:00:00Z",
        )

        self.assertEqual(row["reminder_status"], "declined")
        self.assertIsNone(row["email"])

    def test_set_reminder_decision_clears_pending_confirmation_fields(self):
        set_reminder_pending(
            self.db_path, team_id=1, pending_email="a@b.com", lead_hours=3,
            token_hash="hash", expires_at="2026-08-10T00:00:00+00:00",
            now="2026-08-08T00:00:00Z",
        )

        row = set_reminder_decision(
            self.db_path, team_id=1, status="declined", now="2026-08-08T01:00:00Z",
        )

        self.assertEqual(row["reminder_status"], "declined")
        self.assertIsNone(row["reminder_pending_email"])
        self.assertIsNone(row["reminder_confirmation_token_hash"])
        self.assertIsNone(row["reminder_confirmation_expires_at"])

    def test_disable_clears_the_confirmed_email_but_decline_does_not(self):
        set_reminder_pending(
            self.db_path, team_id=1, pending_email="a@b.com", lead_hours=3,
            token_hash="hash", expires_at="2026-08-10T00:00:00+00:00",
            now="2026-08-08T00:00:00Z",
        )
        confirm_reminder(self.db_path, team_id=1, now="2026-08-08T01:00:00Z")

        disabled = set_reminder_decision(
            self.db_path, team_id=1, status="declined", now="2026-08-08T02:00:00Z",
            clear_email=True,
        )

        self.assertEqual(disabled["reminder_status"], "declined")
        self.assertIsNone(disabled["email"])

    def test_disable_preserves_the_remembered_lead_hours_for_a_future_re_enable(self):
        set_reminder_pending(
            self.db_path, team_id=1, pending_email="a@b.com", lead_hours=24,
            token_hash="hash", expires_at="2026-08-10T00:00:00+00:00",
            now="2026-08-08T00:00:00Z",
        )
        confirm_reminder(self.db_path, team_id=1, now="2026-08-08T01:00:00Z")

        disabled = set_reminder_decision(
            self.db_path, team_id=1, status="declined", now="2026-08-08T02:00:00Z",
            clear_email=True,
        )

        self.assertEqual(disabled["reminder_lead_hours"], 24)

    def test_set_reminder_decision_preserves_an_existing_profile(self):
        save_profile(
            self.db_path, team_id=5, timezone="Europe/London", risk_profile="aggressive",
            confirmed_free_transfers=2, confirmed_free_transfers_event=3,
            now="2026-08-01T00:00:00Z",
        )

        row = set_reminder_decision(
            self.db_path, team_id=5, status="declined", now="2026-08-08T00:00:00Z",
        )

        self.assertEqual(row["timezone"], "Europe/London")
        self.assertEqual(row["risk_profile"], "aggressive")

    def test_reminder_writes_never_disturb_opt_out_flag_or_pin(self):
        set_lookup_opt_out(
            self.db_path, team_id=1, opted_out=True, pin_hash="hash-one",
            now="2026-08-08T00:00:00Z",
        )

        row = set_reminder_pending(
            self.db_path, team_id=1, pending_email="a@b.com", lead_hours=3,
            token_hash="hash", expires_at="2026-08-10T00:00:00+00:00",
            now="2026-08-08T01:00:00Z",
        )

        self.assertTrue(row["opted_out"])
        self.assertEqual(row["pin_hash"], "hash-one")

    def test_different_team_ids_keep_independent_reminder_state(self):
        set_reminder_pending(
            self.db_path, team_id=1, pending_email="one@example.com", lead_hours=3,
            token_hash="hash-one", expires_at="2026-08-10T00:00:00+00:00",
            now="2026-08-08T00:00:00Z",
        )
        set_reminder_pending(
            self.db_path, team_id=2, pending_email="two@example.com", lead_hours=24,
            token_hash="hash-two", expires_at="2026-08-10T00:00:00+00:00",
            now="2026-08-08T00:00:00Z",
        )

        self.assertEqual(load_profile(self.db_path, 1)["reminder_pending_email"], "one@example.com")
        self.assertEqual(load_profile(self.db_path, 2)["reminder_pending_email"], "two@example.com")


class SchemaMigrationTests(unittest.TestCase):
    """Regression coverage for the 2026-08-09 incident: a `profiles.db` created by code older than
    #61 kept running against every PR since, silently missing nine columns (`draft_squad` #61,
    `opted_out`/`pin_hash` #62, `goal` #78, five `reminder_*` columns #79), until a live dashboard
    server hit `OperationalError: no such column: draft_squad` on a real `/api/profile` write.

    `CREATE TABLE IF NOT EXISTS` alone never catches this -- every other test in this file creates
    a brand-new temp file, which gets the full current schema in one shot and never exercises the
    "upgrade an existing older file" path. These tests build that older file by hand instead.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "profiles.db"

    def tearDown(self):
        self.directory.cleanup()

    def _create_pre_61_schema(self):
        """Recreate the exact 8-column schema #45 originally shipped, before #61/#62/#78/#79."""
        import sqlite3

        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                """
                CREATE TABLE profiles (
                    team_id INTEGER PRIMARY KEY,
                    timezone TEXT NOT NULL,
                    risk_profile TEXT NOT NULL,
                    confirmed_free_transfers INTEGER,
                    confirmed_free_transfers_event INTEGER,
                    email TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO profiles (team_id, timezone, risk_profile, email, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (364758, "America/New_York", "balanced", None, "2026-07-01T00:00:00Z",
                 "2026-07-01T00:00:00Z"),
            )
            connection.commit()
        finally:
            connection.close()

    def test_reading_a_pre_61_database_self_heals_and_preserves_existing_data(self):
        self._create_pre_61_schema()

        row = load_profile(self.db_path, 364758)

        # The old row's own data survives the migration untouched.
        self.assertEqual(row["timezone"], "America/New_York")
        self.assertEqual(row["risk_profile"], "balanced")
        # Every column added since #45's original schema reads back as a clean, unset default --
        # not an error, not silently absent from the returned dict.
        self.assertIsNone(row["draft_squad"])
        self.assertIsNone(row["opted_out"])
        self.assertIsNone(row["reminder_status"])

    def test_writing_to_a_pre_61_database_does_not_raise(self):
        self._create_pre_61_schema()

        # This exact call is what threw `OperationalError: no such column: draft_squad` in the
        # live incident -- saving a draft squad against a database that predates that column.
        row = save_draft_squad(self.db_path, 364758, [1, 2, 3], now="2026-08-09T00:00:00Z")

        self.assertEqual(row["draft_squad"], [1, 2, 3])
        # The pre-existing row's own data is untouched by the migration + write.
        self.assertEqual(row["timezone"], "America/New_York")

    def test_migration_is_idempotent_across_repeated_connections(self):
        self._create_pre_61_schema()

        load_profile(self.db_path, 364758)
        load_profile(self.db_path, 364758)
        row = save_profile(
            self.db_path, team_id=364758, timezone="Europe/London", risk_profile="aggressive",
            confirmed_free_transfers=None, confirmed_free_transfers_event=None,
            now="2026-08-09T00:00:00Z",
        )

        self.assertEqual(row["timezone"], "Europe/London")


if __name__ == "__main__":
    unittest.main()
