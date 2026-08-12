from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
import io
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError
import zoneinfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publish_release_notes as prn  # noqa: E402


class _FrozenDateTime(datetime):
    """Same pattern as tests/test_send_deadline_reminder.py's own frozen-clock helper --
    overrides only `.now()`, since `main()`/`is_correct_scheduled_hour` don't expose a `now`
    parameter the way `run()`/`target_date` do."""

    _frozen = None

    @classmethod
    def now(cls, tz=None):
        return cls._frozen.astimezone(tz) if tz else cls._frozen


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8") if not isinstance(self._payload, bytes) else self._payload


class TargetDateTests(unittest.TestCase):
    def test_returns_the_previous_et_calendar_day(self):
        now = datetime(2026, 8, 11, 8, 0, tzinfo=zoneinfo.ZoneInfo("America/New_York"))

        self.assertEqual(prn.target_date(now), date(2026, 8, 10))

    def test_uses_et_not_utc_near_midnight(self):
        # 00:30 ET on Aug 11 is 04:30 UTC -- a UTC-naive implementation would compute "yesterday"
        # from the UTC date (still Aug 11) and get the same answer here by coincidence, so this
        # specifically checks the ET-anchored calculation, not just any calculation.
        now = datetime(2026, 8, 11, 0, 30, tzinfo=zoneinfo.ZoneInfo("America/New_York"))

        self.assertEqual(prn.target_date(now), date(2026, 8, 10))


class IsCorrectScheduledHourTests(unittest.TestCase):
    def test_true_at_the_target_hour(self):
        now = datetime(2026, 8, 11, 8, 15, tzinfo=zoneinfo.ZoneInfo("America/New_York"))

        self.assertTrue(prn.is_correct_scheduled_hour(now))

    def test_false_outside_the_target_hour(self):
        now = datetime(2026, 8, 11, 9, 0, tzinfo=zoneinfo.ZoneInfo("America/New_York"))

        self.assertFalse(prn.is_correct_scheduled_hour(now))


class CategorizePrTests(unittest.TestCase):
    def test_fix_keyword(self):
        self.assertEqual(prn.categorize_pr({"title": "Fix the stale banner"}), "Fix")

    def test_docs_keyword(self):
        self.assertEqual(prn.categorize_pr({"title": "Update README env vars"}), "Docs")

    def test_chore_keyword(self):
        self.assertEqual(prn.categorize_pr({"title": "Clean up test fixtures"}), "Chore")

    def test_data_keyword(self):
        self.assertEqual(prn.categorize_pr({"title": "Reconcile transfer feed IDs"}), "Data")

    def test_defaults_to_feature(self):
        self.assertEqual(prn.categorize_pr({"title": "Split movement filters into three controls"}), "Feature")


class BuildTemplateEntryTests(unittest.TestCase):
    def test_single_pr_entry(self):
        prs = [{"title": "Fix deadline banner flash", "body": "The banner now waits for a real deadline.\n\nMore detail."}]

        entry = prn.build_template_entry(date(2026, 8, 11), prs)

        self.assertEqual(entry["date"], "2026-08-11")
        self.assertEqual(len(entry["changes"]), 1)
        self.assertEqual(entry["changes"][0]["category"], "Fix")
        self.assertIn("Fix deadline banner flash", entry["headline"])

    def test_multi_pr_entry_headline_mentions_count(self):
        prs = [{"title": "A"}, {"title": "B"}, {"title": "C"}]

        entry = prn.build_template_entry(date(2026, 8, 11), prs)

        self.assertIn("3 changes", entry["headline"])
        self.assertEqual(len(entry["changes"]), 3)

    def test_empty_body_falls_back_to_generic_description(self):
        entry = prn.build_template_entry(date(2026, 8, 11), [{"title": "A change", "body": ""}])

        self.assertEqual(entry["changes"][0]["description"], "See the linked pull request for details.")


class BuildLlmEntryTests(unittest.TestCase):
    def test_returns_none_when_unconfigured(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(prn.build_llm_entry(date(2026, 8, 11), [{"title": "A"}]))

    def test_valid_caller_response_is_used(self):
        response = json.dumps({
            "headline": "Sharper filters",
            "summary": "Filters got sharper.",
            "changes": [{"category": "Feature", "title": "Split filters", "description": "Three controls now."}],
        })

        with patch.dict(
            "os.environ",
            {prn.LLM_PROVIDER_ENV_VAR: "claude", prn.LLM_API_KEY_ENV_VAR: "key"}, clear=True,
        ):
            entry = prn.build_llm_entry(date(2026, 8, 11), [{"title": "A"}], caller=lambda prompt: response)

        self.assertEqual(entry["headline"], "Sharper filters")
        self.assertEqual(entry["changes"][0]["category"], "Feature")

    def test_malformed_json_returns_none(self):
        with patch.dict(
            "os.environ",
            {prn.LLM_PROVIDER_ENV_VAR: "claude", prn.LLM_API_KEY_ENV_VAR: "key"}, clear=True,
        ):
            entry = prn.build_llm_entry(date(2026, 8, 11), [{"title": "A"}], caller=lambda prompt: "not json")

        self.assertIsNone(entry)

    def test_invalid_category_returns_none(self):
        response = json.dumps({
            "headline": "H", "summary": "S",
            "changes": [{"category": "Vibes", "title": "T", "description": "D"}],
        })
        with patch.dict(
            "os.environ",
            {prn.LLM_PROVIDER_ENV_VAR: "claude", prn.LLM_API_KEY_ENV_VAR: "key"}, clear=True,
        ):
            entry = prn.build_llm_entry(date(2026, 8, 11), [{"title": "A"}], caller=lambda prompt: response)

        self.assertIsNone(entry)

    def test_caller_network_exception_returns_none_not_raise(self):
        def raising_caller(prompt):
            raise URLError("boom")

        with patch.dict(
            "os.environ",
            {prn.LLM_PROVIDER_ENV_VAR: "claude", prn.LLM_API_KEY_ENV_VAR: "key"}, clear=True,
        ):
            entry = prn.build_llm_entry(date(2026, 8, 11), [{"title": "A"}], caller=raising_caller)

        self.assertIsNone(entry)


class GenerateEntryTests(unittest.TestCase):
    def test_falls_back_to_template_when_llm_unconfigured(self):
        with patch.dict("os.environ", {}, clear=True):
            entry, source = prn.generate_entry(date(2026, 8, 11), [{"title": "A fix here"}])

        self.assertEqual(source, "template")
        self.assertEqual(entry["date"], "2026-08-11")

    def test_uses_llm_when_it_succeeds(self):
        response = json.dumps({
            "headline": "H", "summary": "S",
            "changes": [{"category": "Feature", "title": "T", "description": "D"}],
        })
        with patch.dict(
            "os.environ",
            {prn.LLM_PROVIDER_ENV_VAR: "claude", prn.LLM_API_KEY_ENV_VAR: "key"}, clear=True,
        ):
            entry, source = prn.generate_entry(date(2026, 8, 11), [{"title": "A"}], llm_caller=lambda prompt: response)

        self.assertEqual(source, "llm")


class FetchMergedPrsTests(unittest.TestCase):
    def test_returns_items_from_the_search_response(self):
        def fake_urlopen(request, timeout=None):
            self.assertIn("is%3Amerged", request.full_url)
            return _FakeResponse({"items": [{"title": "A"}, {"title": "B"}]})

        with patch.object(prn, "urlopen", fake_urlopen):
            prs = prn.fetch_merged_prs("owner/repo", date(2026, 8, 11))

        self.assertEqual(len(prs), 2)

    def test_no_merges_returns_empty_list(self):
        def fake_urlopen(request, timeout=None):
            return _FakeResponse({"items": []})

        with patch.object(prn, "urlopen", fake_urlopen):
            prs = prn.fetch_merged_prs("owner/repo", date(2026, 8, 11))

        self.assertEqual(prs, [])


class WriteArchiveFileTests(unittest.TestCase):
    def test_writes_markdown_to_release_notes_folder(self):
        entry = {
            "date": "2026-08-11", "headline": "H", "summary": "S",
            "changes": [{"category": "Feature", "title": "T", "description": "D"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            path = prn.write_archive_file(entry, root=root)

            self.assertTrue(path.exists())
            self.assertEqual(path, root / "release-notes" / "2026-08-11.md")
            self.assertIn("# 2026-08-11", path.read_text(encoding="utf-8"))


class RunTests(unittest.TestCase):
    def test_no_merged_prs_is_a_quiet_no_op(self):
        def fake_urlopen(request, timeout=None):
            return _FakeResponse({"items": []})

        captured = io.StringIO()
        with patch.object(prn, "urlopen", fake_urlopen), \
             patch.dict("os.environ", {prn.GITHUB_REPOSITORY_ENV_VAR: "owner/repo"}, clear=True), \
             redirect_stdout(captured):
            exit_code = prn.run(dry_run=True, now=datetime(2026, 8, 11, 8, 0, tzinfo=zoneinfo.ZoneInfo("America/New_York")))

        self.assertEqual(exit_code, 0)
        self.assertIn("nothing merged", captured.getvalue())

    def test_dry_run_prints_entry_without_publishing_or_writing(self):
        calls = {"publish": 0}

        def fake_urlopen(request, timeout=None):
            if "search/issues" in request.full_url:
                return _FakeResponse({"items": [{"title": "A change here"}]})
            calls["publish"] += 1
            return _FakeResponse({"status": "ok"})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captured = io.StringIO()
            with patch.object(prn, "urlopen", fake_urlopen), \
                 patch.dict("os.environ", {prn.GITHUB_REPOSITORY_ENV_VAR: "owner/repo"}, clear=True), \
                 redirect_stdout(captured):
                exit_code = prn.run(
                    dry_run=True,
                    now=datetime(2026, 8, 11, 8, 0, tzinfo=zoneinfo.ZoneInfo("America/New_York")),
                    root=root,
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(calls["publish"], 0)
            self.assertFalse((root / "release-notes").exists())
            self.assertIn('"headline"', captured.getvalue())

    def test_real_run_publishes_and_writes_archive_file(self):
        def fake_urlopen(request, timeout=None):
            if "search/issues" in request.full_url:
                return _FakeResponse({"items": [{"title": "A change here"}]})
            self.assertEqual(request.headers.get("X-refresh-token"), "test-token")
            return _FakeResponse({"status": "ok", "date": "2026-08-10"})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(prn, "urlopen", fake_urlopen), \
                 patch.dict("os.environ", {prn.GITHUB_REPOSITORY_ENV_VAR: "owner/repo"}, clear=True), \
                 redirect_stdout(io.StringIO()):
                exit_code = prn.run(
                    dry_run=False,
                    now=datetime(2026, 8, 11, 8, 0, tzinfo=zoneinfo.ZoneInfo("America/New_York")),
                    dashboard_base_url="https://example.up.railway.app",
                    refresh_token="test-token",
                    root=root,
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue((root / "release-notes" / "2026-08-10.md").exists())


class MainTests(unittest.TestCase):
    def test_wrong_hour_trigger_is_a_quiet_no_op_without_requiring_config(self):
        with patch.object(prn, "datetime", _FrozenDateTime):
            _FrozenDateTime._frozen = datetime(2026, 8, 11, 9, 0, tzinfo=zoneinfo.ZoneInfo("America/New_York"))
            captured = io.StringIO()
            with patch.dict("os.environ", {}, clear=True), redirect_stdout(captured):
                exit_code = prn.main([])

        self.assertEqual(exit_code, 0)
        self.assertIn("outside the 8 AM ET", captured.getvalue())

    def test_missing_config_on_a_real_run_at_the_correct_hour_is_an_error(self):
        with patch.object(prn, "datetime", _FrozenDateTime):
            _FrozenDateTime._frozen = datetime(2026, 8, 11, 8, 0, tzinfo=zoneinfo.ZoneInfo("America/New_York"))
            captured_err = io.StringIO()
            with patch.dict("os.environ", {}, clear=True), redirect_stdout(io.StringIO()), \
                 patch("sys.stderr", captured_err):
                exit_code = prn.main([])

        self.assertEqual(exit_code, 1)
        self.assertIn("Configuration error", captured_err.getvalue())

    def test_dry_run_skips_config_requirement(self):
        def fake_urlopen(request, timeout=None):
            return _FakeResponse({"items": []})

        with patch.object(prn, "datetime", _FrozenDateTime):
            _FrozenDateTime._frozen = datetime(2026, 8, 11, 8, 0, tzinfo=zoneinfo.ZoneInfo("America/New_York"))
            captured = io.StringIO()
            with patch.object(prn, "urlopen", fake_urlopen), \
                 patch.dict("os.environ", {prn.GITHUB_REPOSITORY_ENV_VAR: "owner/repo"}, clear=True), \
                 redirect_stdout(captured):
                exit_code = prn.main(["--dry-run"])

        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
