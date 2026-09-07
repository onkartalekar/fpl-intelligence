from pathlib import Path
import json
import tempfile
import unittest

from fpl_intel.storage.release_notes import (
    AUDIENCES,
    CATEGORIES,
    ReleaseNotesValidationError,
    delete_entries,
    is_archival_bookkeeping_change,
    load_entries,
    prune_archival_changes,
    release_notes_path,
    render_entry_markdown,
    upsert_entry,
    validate_delete_payload,
    validate_entry_payload,
)


_VALID_PAYLOAD = {
    "date": "2026-08-11",
    "headline": "Sharper filters for preseason movement tracking",
    "summary": "Club movement just got easier to scan.",
    "changes": [
        {
            "category": "Feature",
            "audience": "user",
            "title": "Club movement filters split into Direction, Movement type, and Date",
            "description": "Previously one combined control; each now narrows independently.",
        },
        {
            "category": "Fix",
            "audience": "developer",
            "title": "Deadline banner no longer flashes before the feed is live",
            "description": "The banner now waits for a real deadline before rendering anything.",
        },
    ],
}


class ValidateEntryPayloadTests(unittest.TestCase):
    def test_accepts_a_well_formed_entry(self):
        cleaned = validate_entry_payload(_VALID_PAYLOAD)

        self.assertEqual(cleaned["date"], "2026-08-11")
        self.assertEqual(len(cleaned["changes"]), 2)
        self.assertEqual(cleaned["changes"][0]["category"], "Feature")

    def test_rejects_non_dict_payload(self):
        with self.assertRaises(ReleaseNotesValidationError):
            validate_entry_payload(["not", "a", "dict"])

    def test_rejects_malformed_date(self):
        payload = {**_VALID_PAYLOAD, "date": "08/11/2026"}
        with self.assertRaises(ReleaseNotesValidationError):
            validate_entry_payload(payload)

    def test_rejects_missing_headline(self):
        payload = {**_VALID_PAYLOAD, "headline": ""}
        with self.assertRaises(ReleaseNotesValidationError):
            validate_entry_payload(payload)

    def test_rejects_empty_changes_list(self):
        payload = {**_VALID_PAYLOAD, "changes": []}
        with self.assertRaises(ReleaseNotesValidationError):
            validate_entry_payload(payload)

    def test_rejects_unknown_category(self):
        payload = {**_VALID_PAYLOAD, "changes": [{**_VALID_PAYLOAD["changes"][0], "category": "Vibes"}]}
        with self.assertRaises(ReleaseNotesValidationError):
            validate_entry_payload(payload)

    def test_every_decided_category_is_accepted(self):
        for category in CATEGORIES:
            payload = {
                **_VALID_PAYLOAD,
                "changes": [{**_VALID_PAYLOAD["changes"][0], "category": category}],
            }
            validate_entry_payload(payload)  # must not raise

    def test_categories_are_exactly_the_five_decided_in_the_plan_doc(self):
        self.assertEqual(CATEGORIES, ("Feature", "Fix", "Data", "Docs", "Chore"))

    def test_rejects_unknown_audience(self):
        payload = {**_VALID_PAYLOAD, "changes": [{**_VALID_PAYLOAD["changes"][0], "audience": "robot"}]}
        with self.assertRaises(ReleaseNotesValidationError):
            validate_entry_payload(payload)

    def test_rejects_missing_audience(self):
        change = {k: v for k, v in _VALID_PAYLOAD["changes"][0].items() if k != "audience"}
        payload = {**_VALID_PAYLOAD, "changes": [change]}
        with self.assertRaises(ReleaseNotesValidationError):
            validate_entry_payload(payload)

    def test_every_decided_audience_is_accepted(self):
        for audience in AUDIENCES:
            payload = {
                **_VALID_PAYLOAD,
                "changes": [{**_VALID_PAYLOAD["changes"][0], "audience": audience}],
            }
            validate_entry_payload(payload)  # must not raise

    def test_audiences_are_exactly_user_and_developer(self):
        """Issue #196: `audience` is a per-change signal independent of `category` -- see
        `release_notes_email.py`'s docstring for why a category-level split misrouted real
        changes."""
        self.assertEqual(AUDIENCES, ("user", "developer"))


class UpsertEntryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_writes_a_new_entry(self):
        upsert_entry(self.root, _VALID_PAYLOAD)

        entries = load_entries(self.root)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["date"], "2026-08-11")

    def test_republishing_the_same_date_overwrites_rather_than_duplicates(self):
        upsert_entry(self.root, _VALID_PAYLOAD)
        upsert_entry(self.root, {**_VALID_PAYLOAD, "headline": "Revised headline"})

        entries = load_entries(self.root)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["headline"], "Revised headline")

    def test_entries_are_returned_newest_first(self):
        upsert_entry(self.root, {**_VALID_PAYLOAD, "date": "2026-08-09"})
        upsert_entry(self.root, {**_VALID_PAYLOAD, "date": "2026-08-11"})
        upsert_entry(self.root, {**_VALID_PAYLOAD, "date": "2026-08-10"})

        entries = load_entries(self.root)
        self.assertEqual([entry["date"] for entry in entries], ["2026-08-11", "2026-08-10", "2026-08-09"])

    def test_invalid_payload_raises_and_writes_nothing(self):
        with self.assertRaises(ReleaseNotesValidationError):
            upsert_entry(self.root, {**_VALID_PAYLOAD, "headline": ""})

        self.assertFalse(release_notes_path(self.root).exists())


class ValidateDeletePayloadTests(unittest.TestCase):
    """Issue #300: `{"delete": [...]}` on POST /api/release-notes -- operator cleanup path."""

    def test_accepts_and_sorts_and_dedupes(self):
        self.assertEqual(
            validate_delete_payload({"delete": ["2026-08-23", "2026-08-20", "2026-08-23"]}),
            ["2026-08-20", "2026-08-23"],
        )

    def test_rejects_non_dict(self):
        with self.assertRaises(ReleaseNotesValidationError):
            validate_delete_payload(["2026-08-20"])

    def test_rejects_empty_list(self):
        with self.assertRaises(ReleaseNotesValidationError):
            validate_delete_payload({"delete": []})

    def test_rejects_non_list(self):
        with self.assertRaises(ReleaseNotesValidationError):
            validate_delete_payload({"delete": "2026-08-20"})

    def test_rejects_a_non_string_date(self):
        with self.assertRaises(ReleaseNotesValidationError):
            validate_delete_payload({"delete": ["2026-08-20", 20260821]})

    def test_rejects_a_malformed_date(self):
        with self.assertRaises(ReleaseNotesValidationError):
            validate_delete_payload({"delete": ["2026-8-20"]})


class DeleteEntriesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def _seed(self, *dates):
        for entry_date in dates:
            upsert_entry(self.root, {**_VALID_PAYLOAD, "date": entry_date})

    def test_removes_named_entries_and_reports_what_it_did(self):
        self._seed("2026-08-20", "2026-08-21", "2026-08-22")

        result = delete_entries(self.root, ["2026-08-20", "2026-08-22"])

        self.assertEqual(result, {"deleted": ["2026-08-20", "2026-08-22"], "not_found": []})
        self.assertEqual([entry["date"] for entry in load_entries(self.root)], ["2026-08-21"])

    def test_unknown_dates_are_reported_not_raised(self):
        self._seed("2026-08-21")

        result = delete_entries(self.root, ["2026-08-20", "2026-08-21"])

        self.assertEqual(result, {"deleted": ["2026-08-21"], "not_found": ["2026-08-20"]})
        self.assertEqual(load_entries(self.root), [])

    def test_no_matching_dates_leaves_the_file_untouched(self):
        self._seed("2026-08-21")
        before = release_notes_path(self.root).read_text(encoding="utf-8")

        result = delete_entries(self.root, ["2026-08-20"])

        self.assertEqual(result, {"deleted": [], "not_found": ["2026-08-20"]})
        self.assertEqual(release_notes_path(self.root).read_text(encoding="utf-8"), before)

    def test_missing_file_is_a_quiet_no_op(self):
        result = delete_entries(self.root, ["2026-08-20"])

        self.assertEqual(result, {"deleted": [], "not_found": ["2026-08-20"]})
        self.assertFalse(release_notes_path(self.root).exists())

    def test_remaining_entries_stay_newest_first(self):
        self._seed("2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23")

        delete_entries(self.root, ["2026-08-22"])

        self.assertEqual(
            [entry["date"] for entry in load_entries(self.root)],
            ["2026-08-23", "2026-08-21", "2026-08-20"],
        )


class IsArchivalBookkeepingChangeTests(unittest.TestCase):
    """Issue #303: the tight rule for spotting the daily job's own "we archived the release
    notes" change, without touching real changes that merely mention archiving or release
    notes (every negative below is a real change taken from live data on 2026-09-07)."""

    def _change(self, category, title):
        return {"category": category, "audience": "developer", "title": title, "description": "x"}

    def test_matches_the_wording_variants_the_generator_produced(self):
        for title in (
            "Archive previous release notes",
            "Archive old release notes",
            "Archive outdated release notes",
            "Archive past release notes",
            "Archive older release notes",
            "Archive the latest release notes",
            "Archive release notes for 2026-09-05",
            "Archive 2026-08-16 release notes",
            "archive release notes",
        ):
            self.assertTrue(is_archival_bookkeeping_change(self._change("Chore", title)), title)

    def test_keeps_real_changes_that_look_similar(self):
        for category, title in (
            ("Fix", "Enhance team forecast archiving mechanism"),
            ("Feature", "Generalize team forecast archiving process"),
            ("Fix", "Add fine-grained PAT for archival PR"),
            ("Fix", "Ensure unique names for archival branches"),
            ("Fix", "Fix release-notes workflow"),
            ("Fix", "Revert release-notes/2026-08-15.md drift"),
            ("Docs", "Add email mockup for release notes"),
            ("Feature", "Launch the What's New tab for release notes"),
            ("Chore", "Archive team forecasts"),  # no "release note" in the title
            ("Chore", "Remove change count from What's New entries"),
        ):
            self.assertFalse(is_archival_bookkeeping_change(self._change(category, title)), title)

    def test_non_chore_archival_wording_is_not_a_match(self):
        self.assertFalse(is_archival_bookkeeping_change(self._change("Feature", "Archive old release notes")))

    def test_handles_malformed_input(self):
        self.assertFalse(is_archival_bookkeeping_change(None))
        self.assertFalse(is_archival_bookkeeping_change({}))
        self.assertFalse(is_archival_bookkeeping_change({"category": "Chore"}))


class PruneArchivalChangesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    _ARCHIVAL = {
        "category": "Chore", "audience": "developer",
        "title": "Archive previous release notes",
        "description": "Automated archival of the previous day's release notes.",
    }

    def _seed(self, date, changes):
        upsert_entry(self.root, {**_VALID_PAYLOAD, "date": date, "changes": changes})

    def test_strips_the_archival_change_from_a_mixed_day(self):
        real = _VALID_PAYLOAD["changes"][0]
        self._seed("2026-08-27", [real, self._ARCHIVAL])

        result = prune_archival_changes(self.root)

        self.assertEqual(result, {
            "removed": [{"date": "2026-08-27", "title": self._ARCHIVAL["title"]}],
            "modified": ["2026-08-27"], "emptied": [],
        })
        stored = load_entries(self.root)
        self.assertEqual([change["title"] for change in stored[0]["changes"]], [real["title"]])

    def test_deletes_an_entry_left_with_no_changes(self):
        self._seed("2026-08-28", [self._ARCHIVAL])

        result = prune_archival_changes(self.root)

        self.assertEqual(result, {
            "removed": [{"date": "2026-08-28", "title": self._ARCHIVAL["title"]}],
            "modified": [], "emptied": ["2026-08-28"],
        })
        self.assertEqual(load_entries(self.root), [])

    def test_reports_every_removed_change_and_both_buckets(self):
        real = _VALID_PAYLOAD["changes"][0]
        self._seed("2026-08-16", [real, self._ARCHIVAL, self._ARCHIVAL])
        self._seed("2026-08-28", [self._ARCHIVAL])
        self._seed("2026-08-29", [real])

        result = prune_archival_changes(self.root)

        self.assertEqual(result, {
            "removed": [
                {"date": "2026-08-16", "title": self._ARCHIVAL["title"]},
                {"date": "2026-08-16", "title": self._ARCHIVAL["title"]},
                {"date": "2026-08-28", "title": self._ARCHIVAL["title"]},
            ],
            "modified": ["2026-08-16"], "emptied": ["2026-08-28"],
        })
        self.assertEqual(
            [entry["date"] for entry in load_entries(self.root)], ["2026-08-29", "2026-08-16"],
        )

    def test_no_archival_changes_leaves_the_file_untouched(self):
        self._seed("2026-08-29", [_VALID_PAYLOAD["changes"][0]])
        before = release_notes_path(self.root).read_text(encoding="utf-8")

        result = prune_archival_changes(self.root)

        self.assertEqual(result, {"removed": [], "modified": [], "emptied": []})
        self.assertEqual(release_notes_path(self.root).read_text(encoding="utf-8"), before)

    def test_missing_file_is_a_quiet_no_op(self):
        result = prune_archival_changes(self.root)

        self.assertEqual(result, {"removed": [], "modified": [], "emptied": []})
        self.assertFalse(release_notes_path(self.root).exists())


class LoadEntriesTests(unittest.TestCase):
    def test_missing_file_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_entries(Path(directory)), [])

    def test_malformed_json_returns_empty_list_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = release_notes_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not json", encoding="utf-8")

            self.assertEqual(load_entries(root), [])


class RenderEntryMarkdownTests(unittest.TestCase):
    def test_renders_heading_summary_and_bulleted_changes(self):
        cleaned = validate_entry_payload(_VALID_PAYLOAD)

        markdown = render_entry_markdown(cleaned)

        self.assertIn("# 2026-08-11 -- Sharper filters for preseason movement tracking", markdown)
        self.assertIn(cleaned["summary"], markdown)
        self.assertIn("- **[Feature]**", markdown)
        self.assertIn("- **[Fix]**", markdown)


if __name__ == "__main__":
    unittest.main()
