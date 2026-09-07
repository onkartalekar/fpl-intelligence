import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

# scripts/ is not a package, matching trigger_scheduled_refresh.py's own test setup.
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "purge_release_notes_entries.py"
_SPEC = importlib.util.spec_from_file_location("purge_release_notes_entries", _SCRIPT_PATH)
prne = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(prne)


class CleanDatesTests(unittest.TestCase):
    def test_dedupes_and_sorts(self):
        self.assertEqual(
            prne._clean_dates(["2026-08-23", "2026-08-20", "2026-08-23"]),
            ["2026-08-20", "2026-08-23"],
        )

    def test_rejects_a_malformed_date(self):
        with self.assertRaises(prne.ConfigError):
            prne._clean_dates(["2026-8-20"])

    def test_rejects_an_empty_list(self):
        with self.assertRaises(prne.ConfigError):
            prne._clean_dates([])


class RunTests(unittest.TestCase):
    def test_dry_run_never_calls_the_server(self):
        with patch.object(prne, "purge_entries") as mock_purge, \
             patch("sys.stdout", new=io.StringIO()) as out:
            code = prne.run(dry_run=True, base_url=None, token=None, dates=["2026-08-20"])

        self.assertEqual(code, 0)
        mock_purge.assert_not_called()
        self.assertIn('"delete"', out.getvalue())
        self.assertIn("2026-08-20", out.getvalue())

    def test_reports_what_the_server_deleted(self):
        with patch.object(prne, "purge_entries",
                          return_value={"status": "ok", "deleted": ["2026-08-20"], "not_found": ["2026-08-21"]}) as mock_purge, \
             patch("sys.stdout", new=io.StringIO()) as out:
            code = prne.run(dry_run=False, base_url="https://example.com", token="tok",
                            dates=["2026-08-20", "2026-08-21"])

        self.assertEqual(code, 0)
        mock_purge.assert_called_once_with("https://example.com", "tok", ["2026-08-20", "2026-08-21"])
        self.assertIn("purged 1 entry", out.getvalue())
        self.assertIn("already gone", out.getvalue())


class PurgeEntriesTests(unittest.TestCase):
    def test_posts_the_delete_payload(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"status": "ok", "deleted": ["2026-08-20"], "not_found": []}).encode()

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        with patch.object(prne, "urlopen", side_effect=fake_urlopen):
            result = prne.purge_entries("https://example.com", "secret-token", ["2026-08-20"])

        self.assertEqual(result["deleted"], ["2026-08-20"])
        self.assertEqual(captured["url"], "https://example.com/api/release-notes")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["headers"].get("x-refresh-token"), "secret-token")
        self.assertEqual(captured["body"], {"delete": ["2026-08-20"]})

    def test_http_error_is_wrapped_with_server_message(self):
        def fake_urlopen(request, timeout=None):
            raise HTTPError(
                request.full_url, 400, "Bad Request", None,
                io.BytesIO(b'{"message": "delete must be a non-empty list"}'),
            )

        with patch.object(prne, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(RuntimeError) as context:
                prne.purge_entries("https://example.com", "tok", ["2026-08-20"])

        self.assertIn("400", str(context.exception))
        self.assertIn("delete must be a non-empty list", str(context.exception))

    def test_url_error_is_wrapped(self):
        with patch.object(prne, "urlopen", side_effect=URLError("Name or service not known")):
            with self.assertRaises(RuntimeError) as context:
                prne.purge_entries("https://example.com", "tok", ["2026-08-20"])

        self.assertIn("Name or service not known", str(context.exception))


class MainConfigValidationTests(unittest.TestCase):
    def test_missing_token_fails_before_any_network_call(self):
        with patch.dict("os.environ", {prne.DASHBOARD_BASE_URL_ENV_VAR: "https://example.com"}, clear=True), \
             patch.object(prne, "run") as mock_run, \
             patch("sys.stderr", new=io.StringIO()):
            code = prne.main(["2026-08-20"])

        self.assertEqual(code, 1)
        mock_run.assert_not_called()

    def test_malformed_date_fails_before_env_check(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(prne, "run") as mock_run, \
             patch("sys.stderr", new=io.StringIO()):
            code = prne.main(["2026-8-20"])

        self.assertEqual(code, 1)
        mock_run.assert_not_called()

    def test_dry_run_requires_no_env_vars(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(prne, "purge_entries") as mock_purge, \
             patch("sys.stdout", new=io.StringIO()):
            code = prne.main(["--dry-run", "2026-08-20"])

        self.assertEqual(code, 0)
        mock_purge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
