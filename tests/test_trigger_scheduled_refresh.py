from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from tests.test_recommendations import sample_bootstrap

# scripts/ is not a package, matching send_deadline_reminder.py's own test setup.
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "trigger_scheduled_refresh.py"
_SPEC = importlib.util.spec_from_file_location("trigger_scheduled_refresh", _SCRIPT_PATH)
tsr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tsr)


def _bootstrap_with_deadline(hours_from_now, now):
    bootstrap = sample_bootstrap()
    deadline = now + timedelta(hours=hours_from_now)
    bootstrap["events"] = [
        {"id": 7, "name": "Gameweek 7", "deadline_time": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"), "is_next": True},
    ]
    return bootstrap


class RunLoopTests(unittest.TestCase):
    """Exercises `run()` with load_bootstrap_and_fixtures/trigger_refresh mocked -- no real
    network calls, matching send_deadline_reminder.py's own RunLoopTests convention."""

    def test_outside_window_exits_quietly_without_triggering(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        bootstrap = _bootstrap_with_deadline(hours_from_now=20, now=now)

        with patch.object(tsr, "load_bootstrap_and_fixtures", return_value=(bootstrap, [], False)), \
             patch.object(tsr, "trigger_refresh") as mock_trigger, \
             patch("sys.stdout", new=io.StringIO()) as out:
            code = tsr.run(dry_run=False, base_url="https://example.com", token="tok", now=now)

        self.assertEqual(code, 0)
        mock_trigger.assert_not_called()
        self.assertIn("outside window", out.getvalue())

    def test_no_upcoming_deadline_exits_quietly(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        bootstrap = sample_bootstrap()
        bootstrap["events"] = [{"id": 1, "finished": True}]

        with patch.object(tsr, "load_bootstrap_and_fixtures", return_value=(bootstrap, [], False)), \
             patch.object(tsr, "trigger_refresh") as mock_trigger, \
             patch("sys.stdout", new=io.StringIO()) as out:
            code = tsr.run(dry_run=False, base_url="https://example.com", token="tok", now=now)

        self.assertEqual(code, 0)
        mock_trigger.assert_not_called()
        self.assertIn("no upcoming gameweek deadline", out.getvalue())

    def test_each_of_the_four_checkpoints_triggers_a_refresh(self):
        for lead_hours in (48, 24, 12, 3):
            with self.subTest(lead_hours=lead_hours):
                now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
                bootstrap = _bootstrap_with_deadline(hours_from_now=lead_hours, now=now)

                with patch.object(tsr, "load_bootstrap_and_fixtures", return_value=(bootstrap, [], False)), \
                     patch.object(tsr, "trigger_refresh", return_value={"status": "ok"}) as mock_trigger, \
                     patch("sys.stdout", new=io.StringIO()):
                    code = tsr.run(dry_run=False, base_url="https://example.com", token="tok", now=now)

                self.assertEqual(code, 0)
                mock_trigger.assert_called_once_with("https://example.com", "tok")

    def test_dry_run_never_calls_trigger_refresh(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        bootstrap = _bootstrap_with_deadline(hours_from_now=3, now=now)

        with patch.object(tsr, "load_bootstrap_and_fixtures", return_value=(bootstrap, [], False)), \
             patch.object(tsr, "trigger_refresh") as mock_trigger, \
             patch("sys.stdout", new=io.StringIO()) as out:
            code = tsr.run(dry_run=True, base_url=None, token=None, now=now)

        self.assertEqual(code, 0)
        mock_trigger.assert_not_called()
        self.assertIn("dry-run: would trigger refresh", out.getvalue())
        self.assertIn("T-3h", out.getvalue())

    def test_stale_bootstrap_is_logged_but_still_triggers(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        bootstrap = _bootstrap_with_deadline(hours_from_now=3, now=now)

        with patch.object(tsr, "load_bootstrap_and_fixtures", return_value=(bootstrap, [], True)), \
             patch.object(tsr, "trigger_refresh", return_value={"status": "ok"}) as mock_trigger, \
             patch("sys.stderr", new=io.StringIO()) as err:
            code = tsr.run(dry_run=False, base_url="https://example.com", token="tok", now=now)

        self.assertEqual(code, 0)
        mock_trigger.assert_called_once()
        self.assertIn("cached data", err.getvalue())


class TriggerRefreshTests(unittest.TestCase):
    """`trigger_refresh` itself, with urlopen mocked -- confirms the actual HTTP request shape and
    error translation, independent of `run()`'s window logic above."""

    def test_posts_the_expected_request(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"status": "ok", "confirmed_movements": 3}).encode()

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
            captured["timeout"] = timeout
            return FakeResponse()

        with patch.object(tsr, "urlopen", side_effect=fake_urlopen):
            result = tsr.trigger_refresh("https://example.com", "secret-token")

        self.assertEqual(result, {"status": "ok", "confirmed_movements": 3})
        self.assertEqual(captured["url"], "https://example.com/api/refresh")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["headers"].get("X-refresh-token".lower()), "secret-token")

    def test_http_error_is_wrapped_with_server_message(self):
        def fake_urlopen(request, timeout=None):
            raise HTTPError(
                request.full_url, 403, "Forbidden", None, io.BytesIO(b'{"message": "Invalid refresh token"}'),
            )

        with patch.object(tsr, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(RuntimeError) as context:
                tsr.trigger_refresh("https://example.com", "bad-token")

        self.assertIn("403", str(context.exception))
        self.assertIn("Invalid refresh token", str(context.exception))

    def test_url_error_is_wrapped(self):
        def fake_urlopen(request, timeout=None):
            raise URLError("Name or service not known")

        with patch.object(tsr, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(RuntimeError) as context:
                tsr.trigger_refresh("https://example.com", "tok")

        self.assertIn("Name or service not known", str(context.exception))


class MainConfigValidationTests(unittest.TestCase):
    """main() validates FPL_INTEL_REFRESH_TOKEN/FPL_INTEL_DASHBOARD_BASE_URL eagerly (matching
    send_deadline_reminder.py's own eager SMTP-config check) unless --dry-run is passed."""

    def test_missing_token_fails_before_any_network_call(self):
        with patch.dict("os.environ", {tsr.DASHBOARD_BASE_URL_ENV_VAR: "https://example.com"}, clear=True), \
             patch.object(tsr, "run") as mock_run, \
             patch("sys.stderr", new=io.StringIO()):
            code = tsr.main([])

        self.assertEqual(code, 1)
        mock_run.assert_not_called()

    def test_missing_base_url_fails_before_any_network_call(self):
        with patch.dict("os.environ", {tsr.REFRESH_TOKEN_ENV_VAR: "tok"}, clear=True), \
             patch.object(tsr, "run") as mock_run, \
             patch("sys.stderr", new=io.StringIO()):
            code = tsr.main([])

        self.assertEqual(code, 1)
        mock_run.assert_not_called()

    def test_dry_run_requires_no_env_vars(self):
        now_bootstrap = sample_bootstrap()
        now_bootstrap["events"] = [{"id": 1, "finished": True}]

        with patch.dict("os.environ", {}, clear=True), \
             patch.object(tsr, "load_bootstrap_and_fixtures", return_value=(now_bootstrap, [], False)), \
             patch("sys.stdout", new=io.StringIO()):
            code = tsr.main(["--dry-run"])

        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
