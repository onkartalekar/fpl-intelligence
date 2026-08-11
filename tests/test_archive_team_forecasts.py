from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from tests.test_recommendations import sample_bootstrap

# scripts/ is not a package, matching trigger_scheduled_refresh.py's own test setup.
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "archive_team_forecasts.py"
_SPEC = importlib.util.spec_from_file_location("archive_team_forecasts", _SCRIPT_PATH)
atf = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(atf)


def _bootstrap_with_deadline(hours_from_now, now, event_id=7):
    bootstrap = sample_bootstrap()
    deadline = now + timedelta(hours=hours_from_now)
    bootstrap["events"] = [
        {"id": event_id, "name": f"Gameweek {event_id}", "deadline_time": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"), "is_next": True},
    ]
    return bootstrap


class RunLoopTests(unittest.TestCase):
    """Exercises `run()` with load_bootstrap_and_fixtures/fetch_registered_teams/
    archive_team_forecast mocked -- no real network calls, matching
    trigger_scheduled_refresh.py's own RunLoopTests convention."""

    def test_outside_every_window_exits_quietly(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        bootstrap = _bootstrap_with_deadline(hours_from_now=48, now=now)

        with patch.object(atf, "load_bootstrap_and_fixtures", return_value=(bootstrap, [], False)), \
             patch.object(atf, "fetch_registered_teams") as mock_fetch, \
             patch.object(atf, "archive_team_forecast") as mock_archive, \
             patch("sys.stdout", new=io.StringIO()) as out:
            code = atf.run(dry_run=False, base_url="https://example.com", token="tok", now=now)

        self.assertEqual(code, 0)
        mock_fetch.assert_not_called()
        mock_archive.assert_not_called()
        self.assertIn("outside every archive window", out.getvalue())

    def test_no_upcoming_deadline_exits_quietly(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        bootstrap = sample_bootstrap()
        bootstrap["events"] = [{"id": 1, "finished": True}]

        with patch.object(atf, "load_bootstrap_and_fixtures", return_value=(bootstrap, [], False)), \
             patch.object(atf, "fetch_registered_teams") as mock_fetch, \
             patch("sys.stdout", new=io.StringIO()) as out:
            code = atf.run(dry_run=False, base_url="https://example.com", token="tok", now=now)

        self.assertEqual(code, 0)
        mock_fetch.assert_not_called()
        self.assertIn("No upcoming gameweek deadline", out.getvalue())

    def test_each_checkpoint_fetches_teams_and_archives_every_one(self):
        for lead_hours in (24, 12, 3):
            with self.subTest(lead_hours=lead_hours):
                now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
                bootstrap = _bootstrap_with_deadline(hours_from_now=lead_hours, now=now)

                with patch.object(atf, "load_bootstrap_and_fixtures", return_value=(bootstrap, [], False)), \
                     patch.object(atf, "fetch_registered_teams", return_value=[1, 2]) as mock_fetch, \
                     patch.object(atf, "archive_team_forecast", return_value={"status": "ok", "archived": True}) as mock_archive, \
                     patch("sys.stdout", new=io.StringIO()):
                    code = atf.run(dry_run=False, base_url="https://example.com", token="tok", now=now)

                self.assertEqual(code, 0)
                mock_fetch.assert_called_once_with("https://example.com", "tok")
                mock_archive.assert_any_call("https://example.com", "tok", 1, lead_hours)
                mock_archive.assert_any_call("https://example.com", "tok", 2, lead_hours)
                self.assertEqual(mock_archive.call_count, 2)

    def test_dry_run_never_fetches_teams_or_archives(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        bootstrap = _bootstrap_with_deadline(hours_from_now=3, now=now)

        with patch.object(atf, "load_bootstrap_and_fixtures", return_value=(bootstrap, [], False)), \
             patch.object(atf, "fetch_registered_teams") as mock_fetch, \
             patch.object(atf, "archive_team_forecast") as mock_archive, \
             patch("sys.stdout", new=io.StringIO()) as out:
            code = atf.run(dry_run=True, base_url=None, token=None, now=now)

        self.assertEqual(code, 0)
        mock_fetch.assert_not_called()
        mock_archive.assert_not_called()
        self.assertIn("would archive", out.getvalue())

    def test_one_teams_archive_failure_does_not_stop_the_others(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        bootstrap = _bootstrap_with_deadline(hours_from_now=3, now=now)

        def fake_archive(base_url, token, team_id, lead_hours):
            if team_id == 1:
                raise HTTPError("https://example.com/api/archive-team-forecast", 500, "Server Error", None, io.BytesIO(b"{}"))
            return {"status": "ok", "archived": True}

        with patch.object(atf, "load_bootstrap_and_fixtures", return_value=(bootstrap, [], False)), \
             patch.object(atf, "fetch_registered_teams", return_value=[1, 2]), \
             patch.object(atf, "archive_team_forecast", side_effect=fake_archive), \
             patch("sys.stdout", new=io.StringIO()), patch("sys.stderr", new=io.StringIO()) as err:
            code = atf.run(dry_run=False, base_url="https://example.com", token="tok", now=now)

        self.assertEqual(code, 0)
        self.assertIn("Archive call failed for team 1", err.getvalue())

    def test_fetch_registered_teams_failure_exits_non_zero(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        bootstrap = _bootstrap_with_deadline(hours_from_now=3, now=now)

        with patch.object(atf, "load_bootstrap_and_fixtures", return_value=(bootstrap, [], False)), \
             patch.object(atf, "fetch_registered_teams", side_effect=URLError("unreachable")), \
             patch("sys.stderr", new=io.StringIO()) as err:
            code = atf.run(dry_run=False, base_url="https://example.com", token="tok", now=now)

        self.assertEqual(code, 1)
        self.assertIn("Failed to fetch registered teams", err.getvalue())


class HttpFunctionTests(unittest.TestCase):
    """`fetch_registered_teams`/`archive_team_forecast` themselves, with urlopen mocked --
    confirms the actual HTTP request shape, independent of run()'s window logic above."""

    def test_fetch_registered_teams_sends_the_token_header(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"status": "ok", "team_ids": [1, 2, 3]}).encode()

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
            return FakeResponse()

        with patch.object(atf, "urlopen", side_effect=fake_urlopen):
            result = atf.fetch_registered_teams("https://example.com", "secret-token")

        self.assertEqual(result, [1, 2, 3])
        self.assertEqual(captured["url"], "https://example.com/api/registered-teams")
        self.assertEqual(captured["headers"].get("X-refresh-token".lower()), "secret-token")

    def test_archive_team_forecast_posts_team_id_and_lead_hours(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"status": "ok", "archived": True}).encode()

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["body"] = json.loads(request.data)
            captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
            return FakeResponse()

        with patch.object(atf, "urlopen", side_effect=fake_urlopen):
            result = atf.archive_team_forecast("https://example.com", "secret-token", 364759, 24)

        self.assertEqual(result, {"status": "ok", "archived": True})
        self.assertEqual(captured["url"], "https://example.com/api/archive-team-forecast")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["body"], {"team_id": 364759, "lead_hours": 24})
        self.assertEqual(captured["headers"].get("X-refresh-token".lower()), "secret-token")


class MainConfigValidationTests(unittest.TestCase):
    def test_missing_token_fails_before_any_network_call(self):
        with patch.dict("os.environ", {atf.DASHBOARD_BASE_URL_ENV_VAR: "https://example.com"}, clear=True), \
             patch.object(atf, "run") as mock_run, \
             patch("sys.stderr", new=io.StringIO()):
            code = atf.main([])

        self.assertEqual(code, 1)
        mock_run.assert_not_called()

    def test_missing_base_url_fails_before_any_network_call(self):
        with patch.dict("os.environ", {atf.REFRESH_TOKEN_ENV_VAR: "tok"}, clear=True), \
             patch.object(atf, "run") as mock_run, \
             patch("sys.stderr", new=io.StringIO()):
            code = atf.main([])

        self.assertEqual(code, 1)
        mock_run.assert_not_called()

    def test_dry_run_requires_no_env_vars(self):
        now_bootstrap = sample_bootstrap()
        now_bootstrap["events"] = [{"id": 1, "finished": True}]

        with patch.dict("os.environ", {}, clear=True), \
             patch.object(atf, "load_bootstrap_and_fixtures", return_value=(now_bootstrap, [], False)), \
             patch("sys.stdout", new=io.StringIO()):
            code = atf.main(["--dry-run"])

        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
