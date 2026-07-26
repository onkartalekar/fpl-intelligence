import json
from pathlib import Path
import tempfile
import unittest
from fpl_intel.generation import resolve_artifact
from fpl_intel.refresh import RefreshAlreadyRunning, project_refresh_lock, refresh_project


class RefreshSafetyTests(unittest.TestCase):
    def test_project_refresh_lock_blocks_a_second_process_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with project_refresh_lock(root):
                with self.assertRaises(RefreshAlreadyRunning):
                    with project_refresh_lock(root):
                        pass

    def test_refresh_publishes_complete_authoritative_generation(self):
        bootstrap = {"events": [], "elements": [], "teams": []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "config").mkdir()
            (root / "data" / "confirmed-transfers.json").write_text(json.dumps({"transfers": []}))
            (root / "config" / "sources.json").write_text(json.dumps({"sources": []}))

            refresh_project(root, bootstrap_payload=bootstrap, generated_at="2026-07-18T12:00:00Z")

            pointer = json.loads((root / "data" / "current-generation.json").read_text())
            self.assertTrue(pointer["generation_id"])
            self.assertTrue(resolve_artifact(root, "dashboard.html").exists())
            self.assertTrue(resolve_artifact(root, "dashboard-state.json").exists())


if __name__ == "__main__":
    unittest.main()