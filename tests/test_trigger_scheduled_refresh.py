import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from fpl_intel.sources import deadline_windows

# scripts/ is not a package, matching send_deadline_reminder.py's own test setup.
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "trigger_scheduled_refresh.py"
_SPEC = importlib.util.spec_from_file_location("trigger_scheduled_refresh", _SCRIPT_PATH)
tsr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tsr)


class RunLoopTests(unittest.TestCase):
    """Exercises `run()` with trigger_refresh mocked -- no real network calls, matching
    send_deadline_reminder.py's own RunLoopTests convention.

    Issue #228 replaced this class's original subject. It used to assert the deadline-window
    gate (fire only at T-48h/T-24h/T-12h/T-3h, stay quiet otherwise); the contract is now simply
    that every run triggers, which is what the tests below pin down.
    """

    def test_every_run_triggers_a_refresh(self):
        with patch.object(tsr, "trigger_refresh", return_value={"status": "ok"}) as mock_trigger, \
             patch("sys.stdout", new=io.StringIO()) as out:
            code = tsr.run(dry_run=False, base_url="https://example.com", token="tok")

        self.assertEqual(code, 0)
        mock_trigger.assert_called_once_with("https://example.com", "tok")
        self.assertIn("refresh triggered", out.getvalue())

    def test_triggers_with_no_gameweek_context_available(self):
        """Regression test for issue #228's core change.

        Both of the situations that previously produced a quiet no-op -- being outside every
        deadline window, and there being no upcoming deadline at all (the off-season) -- are now
        indistinguishable from any other run, because no deadline is consulted. This is exactly
        the case that left the hosted dashboard stale for days at a time.
        """
        with patch.object(tsr, "trigger_refresh", return_value={"status": "ok"}) as mock_trigger, \
             patch("sys.stdout", new=io.StringIO()) as out:
            code = tsr.run(dry_run=False, base_url="https://example.com", token="tok")

        self.assertEqual(code, 0)
        mock_trigger.assert_called_once()
        self.assertNotIn("outside window", out.getvalue())
        self.assertNotIn("no upcoming gameweek deadline", out.getvalue())

    def test_no_deadline_machinery_is_reachable_from_this_script(self):
        """The window check is gone, not merely bypassed -- and with it this script's only
        dependency on the live FPL bootstrap feed. `deadline_windows.py` itself must survive,
        though: `send_deadline_reminder.py` and `archive_team_forecasts.py` still import it.
        """
        for name in ("TRIGGER_LEAD_HOURS", "in_send_window", "load_bootstrap_and_fixtures",
                     "next_unfinished_event"):
            self.assertFalse(hasattr(tsr, name), f"{name} should no longer exist on this module")

        self.assertTrue(hasattr(deadline_windows, "in_send_window"))

    def test_dry_run_never_calls_trigger_refresh(self):
        with patch.object(tsr, "trigger_refresh") as mock_trigger, \
             patch("sys.stdout", new=io.StringIO()) as out:
            code = tsr.run(dry_run=True, base_url=None, token=None)

        self.assertEqual(code, 0)
        mock_trigger.assert_not_called()
        self.assertIn("dry-run: would trigger refresh", out.getvalue())

    def test_http_failure_propagates_as_nonzero_exit(self):
        """A refresh that fails must be a visibly failed workflow run, not a silent success --
        with no window check left, a failed POST is the only signal something is wrong."""
        with patch.object(tsr, "trigger_refresh", side_effect=RuntimeError("refresh request failed: HTTP 502 ")), \
             patch.dict("os.environ",
                        {tsr.REFRESH_TOKEN_ENV_VAR: "tok",
                         tsr.DASHBOARD_BASE_URL_ENV_VAR: "https://example.com"}, clear=True), \
             patch("sys.stderr", new=io.StringIO()) as err:
            code = tsr.main([])

        self.assertEqual(code, 1)
        self.assertIn("502", err.getvalue())


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
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(tsr, "trigger_refresh") as mock_trigger, \
             patch("sys.stdout", new=io.StringIO()):
            code = tsr.main(["--dry-run"])

        self.assertEqual(code, 0)
        mock_trigger.assert_not_called()


if __name__ == "__main__":
    unittest.main()
