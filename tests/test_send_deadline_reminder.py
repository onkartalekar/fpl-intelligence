from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from fpl_intel.refresh import compute_manager_view
from tests.test_recommendations import sample_bootstrap, sample_fixtures
from tests.test_transfer_decisions import gw2_inputs


# scripts/ is not a package (no __init__.py, matching the rest of this repo's scripts/), so the
# module under test is loaded directly from its file path rather than imported by dotted name.
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "send_deadline_reminder.py"
_SPEC = importlib.util.spec_from_file_location("send_deadline_reminder", _SCRIPT_PATH)
sdr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sdr)


def _raw_manager_payload(manager):
    return {
        "entry": {
            "id": 364759, "name": "BrunoMans", "player_first_name": "Test",
            "player_last_name": "Manager", "current_event": 1, "started_event": 1,
        },
        "history": {"current": [], "past": [], "chips": []},
        "transfers": [],
        "picks": {
            "active_chip": None,
            "entry_history": {"event": 1, "bank": 0, "value": 1000},
            "picks": [
                {
                    "element": row["element_id"], "position": index + 1,
                    "multiplier": 1 if index < 11 else 0,
                    "is_captain": index == 0, "is_vice_captain": index == 1,
                    "purchase_price": row["purchase_price"],
                    "selling_price": row["selling_price"],
                }
                for index, row in enumerate(manager["squad"])
            ],
        },
    }


class SendWindowArithmeticTests(unittest.TestCase):
    def setUp(self):
        self.deadline = "2026-08-21T17:30:00Z"
        self.deadline_dt = datetime(2026, 8, 21, 17, 30, tzinfo=timezone.utc)

    def test_exactly_at_lead_hours_is_in_window(self):
        now = self.deadline_dt - timedelta(hours=3)
        self.assertTrue(sdr.in_send_window(self.deadline, now, 3))

    def test_comfortably_inside_the_band_is_in_window(self):
        now = self.deadline_dt - timedelta(hours=2, minutes=30)
        self.assertTrue(sdr.in_send_window(self.deadline, now, 3))

    def test_lower_boundary_is_exclusive(self):
        now = self.deadline_dt - timedelta(hours=2)
        self.assertFalse(sdr.in_send_window(self.deadline, now, 3))

    def test_beyond_lead_hours_is_out_of_window(self):
        now = self.deadline_dt - timedelta(hours=3, minutes=30)
        self.assertFalse(sdr.in_send_window(self.deadline, now, 3))

    def test_after_the_deadline_is_out_of_window(self):
        now = self.deadline_dt + timedelta(hours=1)
        self.assertFalse(sdr.in_send_window(self.deadline, now, 3))

    def test_a_different_lead_hours_shifts_the_band(self):
        now = self.deadline_dt - timedelta(hours=1)
        self.assertFalse(sdr.in_send_window(self.deadline, now, 3))
        self.assertTrue(sdr.in_send_window(self.deadline, now, 1))

    def test_hours_until_is_positive_before_the_deadline(self):
        now = self.deadline_dt - timedelta(hours=5)
        self.assertAlmostEqual(sdr.hours_until(self.deadline, now), 5.0)


class ReminderTeamsParsingTests(unittest.TestCase):
    def test_missing_or_empty_env_var_fails_loudly(self):
        with self.assertRaises(sdr.ConfigError):
            sdr.parse_reminder_teams(None)
        with self.assertRaises(sdr.ConfigError):
            sdr.parse_reminder_teams("   ")

    def test_invalid_json_fails_loudly(self):
        with self.assertRaises(sdr.ConfigError):
            sdr.parse_reminder_teams("{not json")

    def test_non_list_json_is_rejected(self):
        with self.assertRaises(sdr.ConfigError):
            sdr.parse_reminder_teams(json.dumps({"team_id": 1, "email": "a@example.com"}))

    def test_empty_list_is_rejected(self):
        with self.assertRaises(sdr.ConfigError):
            sdr.parse_reminder_teams("[]")

    def test_missing_team_id_is_rejected(self):
        with self.assertRaises(sdr.ConfigError):
            sdr.parse_reminder_teams(json.dumps([{"email": "manager@example.com"}]))

    def test_missing_email_is_rejected(self):
        with self.assertRaises(sdr.ConfigError):
            sdr.parse_reminder_teams(json.dumps([{"team_id": 123}]))

    def test_malformed_email_is_rejected(self):
        with self.assertRaises(sdr.ConfigError):
            sdr.parse_reminder_teams(json.dumps([{"team_id": 123, "email": "not-an-email"}]))

    def test_non_positive_lead_hours_is_rejected(self):
        with self.assertRaises(sdr.ConfigError):
            sdr.parse_reminder_teams(json.dumps([{"team_id": 123, "email": "a@example.com", "lead_hours": 0}]))
        with self.assertRaises(sdr.ConfigError):
            sdr.parse_reminder_teams(json.dumps([{"team_id": 123, "email": "a@example.com", "lead_hours": "3"}]))

    def test_error_message_never_contains_the_supplied_email(self):
        secret_email = "very-secret-address@example.com"
        try:
            sdr.parse_reminder_teams(json.dumps([{"email": secret_email}]))
            self.fail("expected ConfigError")
        except sdr.ConfigError as error:
            self.assertNotIn(secret_email, str(error))

    def test_valid_entry_defaults_lead_hours_to_three(self):
        teams = sdr.parse_reminder_teams(json.dumps([{"team_id": 123456, "email": "manager@example.com"}]))
        self.assertEqual(teams, [{"team_id": 123456, "email": "manager@example.com", "lead_hours": 3}])

    def test_valid_entry_respects_a_custom_lead_hours(self):
        teams = sdr.parse_reminder_teams(
            json.dumps([{"team_id": 42, "email": "a@example.com", "lead_hours": 6}])
        )
        self.assertEqual(teams[0]["lead_hours"], 6)

    def test_multiple_teams_are_all_parsed(self):
        raw = json.dumps([
            {"team_id": 1, "email": "one@example.com"},
            {"team_id": 2, "email": "two@example.com", "lead_hours": 1},
        ])
        teams = sdr.parse_reminder_teams(raw)
        self.assertEqual(len(teams), 2)
        self.assertEqual(teams[1]["lead_hours"], 1)


class SmtpConfigParsingTests(unittest.TestCase):
    def test_missing_variables_are_named_without_leaking_values(self):
        with patch.dict("os.environ", {}, clear=False):
            for var in (
                sdr.SMTP_HOST_ENV_VAR, sdr.SMTP_PORT_ENV_VAR,
                sdr.SMTP_USER_ENV_VAR, sdr.SMTP_PASSWORD_ENV_VAR,
            ):
                os.environ.pop(var, None)
            with self.assertRaises(sdr.ConfigError) as context:
                sdr.parse_smtp_config()
            self.assertIn(sdr.SMTP_HOST_ENV_VAR, str(context.exception))

    def test_invalid_port_is_rejected(self):
        env = {
            sdr.SMTP_HOST_ENV_VAR: "smtp.gmail.com",
            sdr.SMTP_PORT_ENV_VAR: "not-a-number",
            sdr.SMTP_USER_ENV_VAR: "user@example.com",
            sdr.SMTP_PASSWORD_ENV_VAR: "hunter2",
        }
        with patch.dict("os.environ", env, clear=False):
            with self.assertRaises(sdr.ConfigError):
                sdr.parse_smtp_config()

    def test_valid_config_is_parsed(self):
        env = {
            sdr.SMTP_HOST_ENV_VAR: "smtp.gmail.com",
            sdr.SMTP_PORT_ENV_VAR: "587",
            sdr.SMTP_USER_ENV_VAR: "user@example.com",
            sdr.SMTP_PASSWORD_ENV_VAR: "hunter2",
        }
        with patch.dict("os.environ", env, clear=False):
            config = sdr.parse_smtp_config()
        self.assertEqual(config, {
            "host": "smtp.gmail.com", "port": 587, "user": "user@example.com", "password": "hunter2",
        })


class EmailCompositionTests(unittest.TestCase):
    def test_waiting_for_gw2_state_composes_the_recommended_squad(self):
        bootstrap, fixtures = sample_bootstrap(), sample_fixtures()
        generated_at = "2026-08-18T12:00:00-04:00"
        decision_center = sdr.build_gw_recommendations(bootstrap, fixtures, generated_at=generated_at)
        manager_view = {
            "manager": {"connection_status": "connected"},
            "weekly_decisions": {"status": "waiting_for_gw2", "event": 1},
        }
        team = {"team_id": 1, "email": "manager@example.com", "lead_hours": 3}

        subject, body = sdr.compose_email(
            team, event_id=1, deadline_iso="2026-08-21T17:30:00Z", hours_left=2.7,
            manager_view=manager_view, decision_center=decision_center, stale=False,
        )

        self.assertIn("GW1", subject)
        self.assertIn("Starting XI", body)
        captain_name = decision_center["recommended_squad"]["captain"]["name"]
        self.assertIn(captain_name, body)
        self.assertNotIn("manager@example.com", body)

        # Regression coverage for issue #82: all three risk profiles must appear in the
        # composed body, not just the default/balanced one used for the full squad detail above.
        self.assertIn("Conservative", body)
        self.assertIn("Balanced", body)
        self.assertIn("Aggressive", body)
        for profile in decision_center["profile_recommendations"]:
            self.assertIn(profile["squad"]["captain"]["name"], body)

    def test_active_transfer_decision_state_composes_the_recommendation(self):
        bootstrap, fixtures, manager = gw2_inputs()
        with patch("fpl_intel.refresh.collect_public_manager", return_value=_raw_manager_payload(manager)):
            manager_view = compute_manager_view(
                bootstrap, fixtures, transfers=[], generated_at="2026-08-29T12:00:00-04:00", team_id=364759,
            )
        self.assertEqual(manager_view["weekly_decisions"]["status"], "active")
        team = {"team_id": 364759, "email": "manager@example.com", "lead_hours": 3}

        subject, body = sdr.compose_email(
            team, event_id=2, deadline_iso="2026-09-02T17:30:00Z", hours_left=2.1,
            manager_view=manager_view, decision_center=None, stale=False,
        )

        self.assertIn("GW2", subject)
        self.assertIn("Recommended action", body)
        self.assertIn("Point cost:", body)
        self.assertNotIn("manager@example.com", body)

        # Regression coverage for issue #82: all three risk profiles must appear in the
        # composed body, not just the default/balanced one used for the full-detail section above.
        self.assertIn("Conservative", body)
        self.assertIn("Balanced", body)
        self.assertIn("Aggressive", body)
        for profile in manager_view["weekly_decisions"]["profiles"]:
            recommendation = profile["recommendation"]
            captain_name = (recommendation.get("captain") or {}).get("name")
            if captain_name:
                self.assertIn(captain_name, body)

    def test_stale_data_adds_an_explicit_staleness_line(self):
        manager_view = {"weekly_decisions": {"status": "manager_not_configured", "reason": "No team configured."}}
        team = {"team_id": 1, "email": "a@example.com", "lead_hours": 3}

        _, body = sdr.compose_email(
            team, event_id=5, deadline_iso="2026-09-20T17:30:00Z", hours_left=1.0,
            manager_view=manager_view, decision_center=None, stale=True,
        )

        self.assertIn("last cached refresh", body)

    def test_unhandled_status_falls_back_to_the_reason_text(self):
        manager_view = {
            "weekly_decisions": {"status": "manager_squad_unavailable", "reason": "Squad not published yet."},
        }
        team = {"team_id": 1, "email": "a@example.com", "lead_hours": 3}

        _, body = sdr.compose_email(
            team, event_id=5, deadline_iso="2026-09-20T17:30:00Z", hours_left=1.0,
            manager_view=manager_view, decision_center=None, stale=False,
        )

        self.assertIn("manager_squad_unavailable", body)
        self.assertIn("Squad not published yet.", body)


class RunLoopTests(unittest.TestCase):
    """Exercises `run()` (the internals `main()` delegates to) with all network calls mocked."""

    def _bootstrap_with_deadline(self, hours_from_now, now):
        bootstrap = sample_bootstrap()
        deadline = now + timedelta(hours=hours_from_now)
        bootstrap["events"] = [
            {"id": 1, "name": "Gameweek 1", "deadline_time": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"), "is_next": True},
        ]
        return bootstrap

    def test_outside_window_exits_quietly_with_no_sends(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        bootstrap = self._bootstrap_with_deadline(hours_from_now=10, now=now)
        teams = [{"team_id": 1, "email": "a@example.com", "lead_hours": 3}]

        with patch.object(sdr, "load_bootstrap_and_fixtures", return_value=(bootstrap, [], False)), \
             patch.object(sdr, "send_email") as mock_send:
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                exit_code = sdr.run(teams, dry_run=False, smtp_config=None, now=now)

        self.assertEqual(exit_code, 0)
        mock_send.assert_not_called()
        self.assertIn("outside window", captured.getvalue())

    def test_team_not_found_is_skipped_with_a_generic_warning_and_no_email_in_the_log(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        bootstrap = self._bootstrap_with_deadline(hours_from_now=2.5, now=now)
        teams = [{"team_id": 999999, "email": "secret@example.com", "lead_hours": 3}]

        with patch.object(sdr, "load_bootstrap_and_fixtures", return_value=(bootstrap, [], False)), \
             patch.object(sdr, "compute_manager_view", return_value={
                 "manager": {"connection_status": "lookup_failed"},
                 "weekly_decisions": {"status": "team_not_found", "reason": "Team not found."},
             }), \
             patch.object(sdr, "send_email") as mock_send:
            captured_out, captured_err = io.StringIO(), io.StringIO()
            with patch("sys.stdout", captured_out), patch("sys.stderr", captured_err):
                exit_code = sdr.run(teams, dry_run=False, smtp_config=None, now=now)

        self.assertEqual(exit_code, 0)
        mock_send.assert_not_called()
        self.assertNotIn("secret@example.com", captured_err.getvalue())
        self.assertNotIn("secret@example.com", captured_out.getvalue())
        self.assertIn("999999", captured_err.getvalue())

    def test_in_window_active_team_sends_exactly_one_email(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        bootstrap = self._bootstrap_with_deadline(hours_from_now=2.5, now=now)
        teams = [{"team_id": 42, "email": "manager@example.com", "lead_hours": 3}]
        manager_view = {
            "manager": {"connection_status": "connected"},
            "weekly_decisions": {"status": "manager_not_configured", "reason": "No team configured."},
        }
        smtp_config = {"host": "smtp.gmail.com", "port": 587, "user": "u@example.com", "password": "x"}

        with patch.object(sdr, "load_bootstrap_and_fixtures", return_value=(bootstrap, [], False)), \
             patch.object(sdr, "compute_manager_view", return_value=manager_view), \
             patch.object(sdr, "send_email") as mock_send:
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                exit_code = sdr.run(teams, dry_run=False, smtp_config=smtp_config, now=now)

        self.assertEqual(exit_code, 0)
        mock_send.assert_called_once()
        sent_to = mock_send.call_args.args[1]
        self.assertEqual(sent_to, "manager@example.com")
        self.assertNotIn("manager@example.com", captured.getvalue())
        self.assertIn("reminder sent for GW1 to 1 team(s)", captured.getvalue())

    def test_different_lead_hours_are_evaluated_independently(self):
        """Two teams configured with different lead_hours: only the in-window one is emailed."""
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        bootstrap = self._bootstrap_with_deadline(hours_from_now=2.5, now=now)
        teams = [
            {"team_id": 1, "email": "three-hour@example.com", "lead_hours": 3},
            {"team_id": 2, "email": "one-hour@example.com", "lead_hours": 1},
        ]
        manager_view = {
            "manager": {"connection_status": "connected"},
            "weekly_decisions": {"status": "manager_not_configured", "reason": "No team configured."},
        }
        smtp_config = {"host": "smtp.gmail.com", "port": 587, "user": "u@example.com", "password": "x"}

        with patch.object(sdr, "load_bootstrap_and_fixtures", return_value=(bootstrap, [], False)), \
             patch.object(sdr, "compute_manager_view", return_value=manager_view), \
             patch.object(sdr, "send_email") as mock_send:
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                sdr.run(teams, dry_run=False, smtp_config=smtp_config, now=now)

        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args.args[1], "three-hour@example.com")

    def test_saved_profile_overrides_are_passed_through_to_compute_manager_view(self):
        """Issue #81 regression: a team with a saved confirmed-free-transfer/draft-squad
        profile must have those values reach `compute_manager_view`, not silently be
        dropped in favor of whatever the public FPL API currently reports."""
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        bootstrap = self._bootstrap_with_deadline(hours_from_now=2.5, now=now)
        teams = [{"team_id": 42, "email": "manager@example.com", "lead_hours": 3}]
        manager_view = {
            "manager": {"connection_status": "connected"},
            "weekly_decisions": {"status": "manager_not_configured", "reason": "No team configured."},
        }
        saved_profile = {
            "team_id": 42, "confirmed_free_transfers": 2, "confirmed_free_transfers_event": 3,
            "draft_squad": [1, 2, 3],
        }
        smtp_config = {"host": "smtp.gmail.com", "port": 587, "user": "u@example.com", "password": "x"}

        with patch.object(sdr, "load_bootstrap_and_fixtures", return_value=(bootstrap, [], False)), \
             patch.object(sdr.profiles, "load_profile", return_value=saved_profile) as mock_load_profile, \
             patch.object(sdr, "compute_manager_view", return_value=manager_view) as mock_compute, \
             patch.object(sdr, "send_email"):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                sdr.run(teams, dry_run=False, smtp_config=smtp_config, now=now)

        mock_load_profile.assert_called_once()
        self.assertEqual(mock_load_profile.call_args.args[1], 42)
        mock_compute.assert_called_once()
        self.assertEqual(mock_compute.call_args.kwargs["confirmed_free_transfers"], 2)
        self.assertEqual(mock_compute.call_args.kwargs["confirmed_free_transfers_event"], 3)
        self.assertEqual(mock_compute.call_args.kwargs["draft_squad_ids"], [1, 2, 3])

    def test_no_saved_profile_passes_none_overrides_matching_prior_behavior(self):
        """Regression for the "must not change existing no-profile behavior" requirement:
        a team that has never saved a profile (`load_profile` returns None) must still pass
        all three overrides as None, exactly as before this fix."""
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        bootstrap = self._bootstrap_with_deadline(hours_from_now=2.5, now=now)
        teams = [{"team_id": 999, "email": "manager@example.com", "lead_hours": 3}]
        manager_view = {
            "manager": {"connection_status": "connected"},
            "weekly_decisions": {"status": "manager_not_configured", "reason": "No team configured."},
        }
        smtp_config = {"host": "smtp.gmail.com", "port": 587, "user": "u@example.com", "password": "x"}

        with patch.object(sdr, "load_bootstrap_and_fixtures", return_value=(bootstrap, [], False)), \
             patch.object(sdr.profiles, "load_profile", return_value=None), \
             patch.object(sdr, "compute_manager_view", return_value=manager_view) as mock_compute, \
             patch.object(sdr, "send_email"):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                sdr.run(teams, dry_run=False, smtp_config=smtp_config, now=now)

        mock_compute.assert_called_once()
        self.assertIsNone(mock_compute.call_args.kwargs["confirmed_free_transfers"])
        self.assertIsNone(mock_compute.call_args.kwargs["confirmed_free_transfers_event"])
        self.assertIsNone(mock_compute.call_args.kwargs["draft_squad_ids"])

    def test_dry_run_prints_composed_email_and_never_calls_smtp(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        bootstrap = self._bootstrap_with_deadline(hours_from_now=2.5, now=now)
        teams = [{"team_id": 42, "email": "manager@example.com", "lead_hours": 3}]
        manager_view = {
            "manager": {"connection_status": "connected"},
            "weekly_decisions": {"status": "manager_not_configured", "reason": "No team configured."},
        }

        with patch.object(sdr, "load_bootstrap_and_fixtures", return_value=(bootstrap, [], False)), \
             patch.object(sdr, "compute_manager_view", return_value=manager_view), \
             patch.object(sdr, "send_email") as mock_send:
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                exit_code = sdr.run(teams, dry_run=True, smtp_config=None, now=now)

        self.assertEqual(exit_code, 0)
        mock_send.assert_not_called()
        self.assertIn("manager@example.com", captured.getvalue())


class MainCliTests(unittest.TestCase):
    def test_malformed_reminder_teams_env_var_exits_non_zero(self):
        with patch.dict("os.environ", {sdr.REMINDER_TEAMS_ENV_VAR: "{not json"}, clear=False):
            captured_err = io.StringIO()
            with patch("sys.stderr", captured_err):
                exit_code = sdr.main([])
        self.assertNotEqual(exit_code, 0)
        self.assertIn("Configuration error", captured_err.getvalue())

    def test_dry_run_does_not_require_smtp_env_vars(self):
        env_updates = {sdr.REMINDER_TEAMS_ENV_VAR: json.dumps([{"team_id": 1, "email": "a@example.com"}])}
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        bootstrap = sample_bootstrap()
        bootstrap["events"] = [
            {
                "id": 1, "name": "Gameweek 1",
                "deadline_time": (now + timedelta(hours=100)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "is_next": True,
            },
        ]
        with patch.dict("os.environ", env_updates, clear=False):
            for var in (
                sdr.SMTP_HOST_ENV_VAR, sdr.SMTP_PORT_ENV_VAR,
                sdr.SMTP_USER_ENV_VAR, sdr.SMTP_PASSWORD_ENV_VAR,
            ):
                os.environ.pop(var, None)
            with patch.object(sdr, "load_bootstrap_and_fixtures", return_value=(bootstrap, [], False)):
                captured = io.StringIO()
                with patch("sys.stdout", captured):
                    exit_code = sdr.main(["--dry-run"])

        self.assertEqual(exit_code, 0)
        self.assertIn("outside window", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
