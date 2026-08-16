import json
from pathlib import Path
import tempfile
import unittest

from fpl_intel.generation import current_generation_id, publish_generation


class CurrentGenerationIdTests(unittest.TestCase):
    """Issue #208: `current_generation_id` is the invalidation signal the request-level decision
    cache keys on -- must track `resolve_artifact`'s own notion of "the authoritative generation"
    exactly, including its None fallback."""

    def test_returns_none_before_any_generation_has_been_published(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()

            self.assertIsNone(current_generation_id(root))

    def test_returns_the_published_generation_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()

            generation_id = publish_generation(
                root, "2026-08-16T12:00:00Z", {"dashboard-state.json": {"generated_at": "2026-08-16T12:00:00Z"}},
            )

            self.assertEqual(current_generation_id(root), generation_id)

    def test_changes_after_a_second_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()

            first = publish_generation(root, "2026-08-16T12:00:00Z", {"dashboard-state.json": {}})
            second = publish_generation(root, "2026-08-16T13:00:00Z", {"dashboard-state.json": {}})

            self.assertNotEqual(first, second)
            self.assertEqual(current_generation_id(root), second)

    def test_returns_none_for_a_malformed_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data" / "current-generation.json").write_text("not json", encoding="utf-8")

            self.assertIsNone(current_generation_id(root))


if __name__ == "__main__":
    unittest.main()
