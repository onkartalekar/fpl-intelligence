import json
from pathlib import Path
import tempfile
import time
import unittest

from fpl_intel.generation import GENERATION_RETENTION_COUNT, current_generation_id, publish_generation


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


class PruneOldGenerationsTests(unittest.TestCase):
    """Each publish stages a full ~5MB snapshot and never used to delete old ones --
    unbounded growth on the Railway volume. publish_generation now prunes down to
    GENERATION_RETENTION_COUNT after every successful publish."""

    def _publish_many(self, root, count):
        generation_ids = []
        for index in range(count):
            generation_ids.append(
                publish_generation(root, f"2026-08-16T{index:02d}:00:00Z", {"dashboard-state.json": {}})
            )
            time.sleep(0.01)  # keep directory mtimes distinct across filesystems
        return generation_ids

    def test_prunes_down_to_the_retention_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()

            self._publish_many(root, GENERATION_RETENTION_COUNT + 3)

            remaining = list((root / "data" / "generations").iterdir())
            self.assertEqual(len(remaining), GENERATION_RETENTION_COUNT)

    def test_deletes_the_oldest_generations_first(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()

            generation_ids = self._publish_many(root, GENERATION_RETENTION_COUNT + 3)

            remaining_names = {entry.name for entry in (root / "data" / "generations").iterdir()}
            self.assertEqual(remaining_names, set(generation_ids[-GENERATION_RETENTION_COUNT:]))
            for stale_id in generation_ids[: -GENERATION_RETENTION_COUNT]:
                self.assertNotIn(stale_id, remaining_names)

    def test_keeps_the_current_generation_reachable_after_pruning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()

            generation_ids = self._publish_many(root, GENERATION_RETENTION_COUNT + 3)

            self.assertEqual(current_generation_id(root), generation_ids[-1])

    def test_does_not_prune_when_under_the_retention_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()

            self._publish_many(root, 3)

            remaining = list((root / "data" / "generations").iterdir())
            self.assertEqual(len(remaining), 3)


if __name__ == "__main__":
    unittest.main()
