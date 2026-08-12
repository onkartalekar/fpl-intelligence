from pathlib import Path
import json
import tempfile
import unittest

from fpl_intel.release_notes import (
    CATEGORIES,
    ReleaseNotesValidationError,
    load_entries,
    release_notes_path,
    render_entry_markdown,
    upsert_entry,
    validate_entry_payload,
)


_VALID_PAYLOAD = {
    "date": "2026-08-11",
    "headline": "Sharper filters for preseason movement tracking",
    "summary": "Club movement just got easier to scan.",
    "changes": [
        {
            "category": "Feature",
            "title": "Club movement filters split into Direction, Movement type, and Date",
            "description": "Previously one combined control; each now narrows independently.",
        },
        {
            "category": "Fix",
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
