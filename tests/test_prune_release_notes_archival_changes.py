import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

# scripts/ is not a package, matching purge_release_notes_entries.py's own test setup.
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "prune_release_notes_archival_changes.py"
_SPEC = importlib.util.spec_from_file_location("prune_release_notes_archival_changes", _SCRIPT_PATH)
prac = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(prac)


class RunTests(unittest.TestCase):
    def test_dry_run_never_calls_the_server(self):
        with patch.object(prac, "prune_changes") as mock_prune, \
             patch("sys.stdout", new=io.StringIO()) as out:
            code = prac.run(dry_run=True, base_url=None, token=None)

        self.assertEqual(code, 0)
        mock_prune.assert_not_called()
        self.assertIn("prune_archival_changes", out.getvalue())

    def test_reports_the_server_summary(self):
        with patch.object(prac, "prune_changes",
                          return_value={"status": "ok",
                                        "removed": [{"date": "2026-08-16", "title": "Archive 2026-08-16 release notes"},
                                                    {"date": "2026-08-28", "title": "Archive old release notes"}],
                                        "modified": ["2026-08-16"], "emptied": ["2026-08-28"]}) as mock_prune, \
             patch("sys.stdout", new=io.StringIO()) as out:
            code = prac.run(dry_run=False, base_url="https://example.com", token="tok")

        self.assertEqual(code, 0)
        mock_prune.assert_called_once_with("https://example.com", "tok")
        self.assertIn("pruned 2 archival changes", out.getvalue())
        self.assertIn("2026-08-16: Archive 2026-08-16 release notes", out.getvalue())
        self.assertIn("2026-08-28: Archive old release notes", out.getvalue())


class PruneChangesTests(unittest.TestCase):
    def test_posts_the_prune_payload(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"status": "ok", "removed": [], "modified": ["2026-08-27"], "emptied": []}).encode()

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        with patch.object(prac, "urlopen", side_effect=fake_urlopen):
            result = prac.prune_changes("https://example.com", "secret-token")

        self.assertEqual(result["modified"], ["2026-08-27"])
        self.assertEqual(captured["url"], "https://example.com/api/release-notes")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["headers"].get("x-refresh-token"), "secret-token")
        self.assertEqual(captured["body"], {"prune_archival_changes": True})

    def test_http_error_is_wrapped_with_server_message(self):
        def fake_urlopen(request, timeout=None):
            raise HTTPError(
                request.full_url, 400, "Bad Request", None,
                io.BytesIO(b'{"message": "prune_archival_changes must be true"}'),
            )

        with patch.object(prac, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(RuntimeError) as context:
                prac.prune_changes("https://example.com", "tok")

        self.assertIn("400", str(context.exception))
        self.assertIn("prune_archival_changes must be true", str(context.exception))

    def test_url_error_is_wrapped(self):
        with patch.object(prac, "urlopen", side_effect=URLError("Name or service not known")):
            with self.assertRaises(RuntimeError) as context:
                prac.prune_changes("https://example.com", "tok")

        self.assertIn("Name or service not known", str(context.exception))


class MainConfigValidationTests(unittest.TestCase):
    def test_missing_token_fails_before_any_network_call(self):
        with patch.dict("os.environ", {prac.DASHBOARD_BASE_URL_ENV_VAR: "https://example.com"}, clear=True), \
             patch.object(prac, "run") as mock_run, \
             patch("sys.stderr", new=io.StringIO()):
            code = prac.main([])

        self.assertEqual(code, 1)
        mock_run.assert_not_called()

    def test_dry_run_requires_no_env_vars(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(prac, "prune_changes") as mock_prune, \
             patch("sys.stdout", new=io.StringIO()):
            code = prac.main(["--dry-run"])

        self.assertEqual(code, 0)
        mock_prune.assert_not_called()


if __name__ == "__main__":
    unittest.main()
