import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fpl_intel.sources.fpl_data import (
    EVENT_LIVE_URL,
    FIXTURES_URL,
    fetch_event_live,
    fetch_fixtures,
    save_json,
    summarize_bootstrap,
)


class BootstrapSummaryTests(unittest.TestCase):
    def test_marks_prior_season_feed_as_not_ready(self):
        payload = {
            "events": [{"id": 1, "deadline_time": "2025-08-15T17:30:00Z"}],
            "elements": [{"id": 1}],
            "teams": [{"id": 1}],
        }

        summary = summarize_bootstrap(payload, expected_first_deadline_year=2026)

        self.assertEqual(summary["season_status"], "prior_season_data")
        self.assertFalse(summary["ready_for_2026_27"])
        self.assertEqual(summary["player_count"], 1)

    def test_reports_next_target_season_deadline(self):
        payload = {
            "events": [
                {
                    "id": 1,
                    "name": "Gameweek 1",
                    "deadline_time": "2026-08-14T17:30:00Z",
                    "finished": False,
                    "is_next": True,
                }
            ],
            "elements": [{"id": 1}],
            "teams": [{"id": 1}],
        }

        summary = summarize_bootstrap(payload, expected_first_deadline_year=2026)

        self.assertTrue(summary["ready_for_2026_27"])
        self.assertEqual(summary["season_phase"], "preseason")
        self.assertEqual(summary["next_event_id"], 1)
        self.assertEqual(summary["next_event_name"], "Gameweek 1")
        self.assertEqual(summary["next_deadline"], "2026-08-14T17:30:00Z")


class FixtureFetchTests(unittest.TestCase):
    def test_fetches_official_fixture_json(self):
        seen = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        def opener(request, timeout):
            seen.append((request.full_url, timeout))
            return Response(b'[{"id": 1, "event": 1}]')

        fixtures = fetch_fixtures(timeout=12, opener=opener)

        self.assertEqual(fixtures, [{"id": 1, "event": 1}])
        self.assertEqual(seen, [(FIXTURES_URL, 12)])

    def test_fetches_official_finished_event_points(self):
        seen = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        def opener(request, timeout):
            seen.append((request.full_url, timeout))
            return Response(b'{"elements":[{"id":1,"stats":{"total_points":7}}]}')

        payload = fetch_event_live(3, timeout=9, opener=opener)

        self.assertEqual(payload["elements"][0]["stats"]["total_points"], 7)
        self.assertEqual(seen, [(EVENT_LIVE_URL.format(event=3), 9)])


class PersistenceTests(unittest.TestCase):
    def test_save_json_preserves_previous_file_when_atomic_replace_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "state.json"
            destination.write_text('{"old": true}', encoding="utf-8")

            with patch("fpl_intel.sources.fpl_data.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    save_json(destination, {"new": True})

            self.assertEqual(destination.read_text(encoding="utf-8"), '{"old": true}')
            self.assertEqual(list(destination.parent.glob(f".{destination.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
