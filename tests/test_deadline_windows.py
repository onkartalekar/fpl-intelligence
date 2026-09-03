from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fpl_intel.sources.deadline_windows import (
    DeadlineDataError, hours_until, in_send_window, load_bootstrap_and_fixtures,
    next_unfinished_event, within_capture_window,
)


class WindowArithmeticTests(unittest.TestCase):
    """Issue #101: this module was extracted from send_deadline_reminder.py (issue #55) so a
    second caller (the new scheduled-refresh trigger) doesn't duplicate this arithmetic. These
    mirror test_send_deadline_reminder.py's own SendWindowArithmeticTests, which continues to
    exercise the same functions through the reminder script's re-export."""

    def setUp(self):
        self.deadline = "2026-08-21T17:30:00Z"
        self.deadline_dt = datetime(2026, 8, 21, 17, 30, tzinfo=timezone.utc)

    def test_exactly_at_lead_hours_is_in_window(self):
        now = self.deadline_dt - timedelta(hours=3)
        self.assertTrue(in_send_window(self.deadline, now, 3))

    def test_lower_boundary_is_exclusive(self):
        now = self.deadline_dt - timedelta(hours=2)
        self.assertFalse(in_send_window(self.deadline, now, 3))

    def test_beyond_lead_hours_is_out_of_window(self):
        now = self.deadline_dt - timedelta(hours=3, minutes=30)
        self.assertFalse(in_send_window(self.deadline, now, 3))

    def test_hours_until_is_positive_before_the_deadline(self):
        now = self.deadline_dt - timedelta(hours=5)
        self.assertAlmostEqual(hours_until(self.deadline, now), 5.0)

    def test_each_of_the_four_scheduled_refresh_checkpoints_has_its_own_window(self):
        """Issue #101's requested checkpoints -- confirms they're independent, non-overlapping
        one-hour bands, not just that the underlying arithmetic works for an arbitrary value."""
        for lead_hours in (48, 24, 12, 3):
            now = self.deadline_dt - timedelta(hours=lead_hours)
            self.assertTrue(in_send_window(self.deadline, now, lead_hours))
            # An hour before this checkpoint's own window opens, it must not have fired yet.
            too_early = self.deadline_dt - timedelta(hours=lead_hours + 1)
            self.assertFalse(in_send_window(self.deadline, too_early, lead_hours))


class WithinCaptureWindowTests(unittest.TestCase):
    """Issue #286: the catch-up window used by `archive_team_forecasts.py` -- open from a
    checkpoint's lead time right up to the deadline, so a delayed cron tick still captures it,
    but never after the deadline."""

    def setUp(self):
        self.deadline = "2026-08-21T17:30:00Z"
        self.deadline_dt = datetime(2026, 8, 21, 17, 30, tzinfo=timezone.utc)

    def test_open_at_exactly_the_lead_time(self):
        now = self.deadline_dt - timedelta(hours=12)
        self.assertTrue(within_capture_window(self.deadline, now, 12))

    def test_still_open_hours_after_the_missed_hour(self):
        # `in_send_window` would already be False here (only the (11, 12] hour); this must not be.
        now = self.deadline_dt - timedelta(hours=8)
        self.assertFalse(in_send_window(self.deadline, now, 12))
        self.assertTrue(within_capture_window(self.deadline, now, 12))

    def test_open_right_up_to_the_deadline(self):
        now = self.deadline_dt - timedelta(minutes=5)
        self.assertTrue(within_capture_window(self.deadline, now, 3))

    def test_not_open_before_the_lead_time(self):
        now = self.deadline_dt - timedelta(hours=3, minutes=1)
        self.assertFalse(within_capture_window(self.deadline, now, 3))

    def test_closed_exactly_at_the_deadline(self):
        self.assertFalse(within_capture_window(self.deadline, self.deadline_dt, 24))

    def test_closed_after_the_deadline(self):
        # The load-bearing guard: between the deadline and FPL flagging the GW finished,
        # `hours_until` is negative -- the window must stay shut so no post-deadline
        # (hindsight-contaminated) recommendation is ever archived.
        now = self.deadline_dt + timedelta(hours=2)
        self.assertFalse(within_capture_window(self.deadline, now, 24))
        self.assertFalse(within_capture_window(self.deadline, now, 3))


class NextUnfinishedEventTests(unittest.TestCase):
    def test_prefers_the_explicit_is_next_flag(self):
        bootstrap = {
            "events": [
                {"id": 1, "finished": True},
                {"id": 2, "finished": False, "is_next": True},
                {"id": 3, "finished": False},
            ]
        }
        self.assertEqual(next_unfinished_event(bootstrap)["id"], 2)

    def test_falls_back_to_the_lowest_id_unfinished_event(self):
        bootstrap = {"events": [{"id": 3, "finished": False}, {"id": 2, "finished": False}]}
        self.assertEqual(next_unfinished_event(bootstrap)["id"], 2)

    def test_no_unfinished_events_returns_none(self):
        bootstrap = {"events": [{"id": 1, "finished": True}]}
        self.assertIsNone(next_unfinished_event(bootstrap))


class LoadBootstrapAndFixturesTests(unittest.TestCase):
    def test_live_fetch_success_is_not_marked_stale(self):
        with patch("fpl_intel.sources.deadline_windows.fetch_bootstrap", return_value={"events": []}), \
             patch("fpl_intel.sources.deadline_windows.fetch_fixtures", return_value=[]):
            bootstrap, fixtures, stale = load_bootstrap_and_fixtures(Path("/tmp/unused"))

        self.assertEqual(bootstrap, {"events": []})
        self.assertFalse(stale)

    def test_falls_back_to_cached_bootstrap_on_live_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data" / "fpl-bootstrap-latest.json").write_text('{"events": [{"id": 1}]}')

            with patch("fpl_intel.sources.deadline_windows.fetch_bootstrap", side_effect=RuntimeError("down")), \
                 patch("fpl_intel.sources.deadline_windows.fetch_fixtures", return_value=[]):
                bootstrap, _fixtures, stale = load_bootstrap_and_fixtures(root)

        self.assertEqual(bootstrap, {"events": [{"id": 1}]})
        self.assertTrue(stale)

    def test_raises_when_live_fetch_fails_and_no_cache_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()

            with patch("fpl_intel.sources.deadline_windows.fetch_bootstrap", side_effect=RuntimeError("down")):
                with self.assertRaises(DeadlineDataError):
                    load_bootstrap_and_fixtures(root)


if __name__ == "__main__":
    unittest.main()
