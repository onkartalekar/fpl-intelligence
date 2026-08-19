"""Issue #229: `scripts/refresh_dashboard.py`'s reporting must describe what it actually did.

The bug these lock down: the script's summary line named `dashboard.html`, a file it had not
written since issue #120 removed the HTML snapshot from `publish_generation`. On a machine where
that file was days stale -- or had never existed at all, since it is gitignored -- the line read
as positive confirmation that it was current.

The pipeline itself is mocked here; this is about the script's own output contract, not about
refreshing (`tests/test_refresh.py` covers that).
"""

from contextlib import nullcontext, redirect_stdout
import importlib.util
import io
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

# scripts/ is not a package, matching the other script tests' setup.
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "refresh_dashboard.py"
_SPEC = importlib.util.spec_from_file_location("refresh_dashboard", _SCRIPT_PATH)
rd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rd)

_STATE = {"transfers": [{"player": "A"}], "fpl": {"season_status": "target_season_ready"}}


class RefreshReportingTests(unittest.TestCase):
    def _run_with_fake_pipeline(self, root):
        """Run `main()` against a stand-in pipeline that writes what the real one writes.

        `publish_generation` writes `data/dashboard-state.json` and deliberately does not write
        `dashboard.html` (issue #120), so the fake mirrors exactly that -- which is what makes
        the "every path it prints exists" assertion below meaningful rather than circular.
        """
        def fake_refresh(*args, **kwargs):
            (root / "data").mkdir(parents=True, exist_ok=True)
            (root / "data" / "dashboard-state.json").write_text("{}", encoding="utf-8")
            return _STATE

        out = io.StringIO()
        with patch.object(rd, "ROOT", root), \
             patch.object(rd, "project_refresh_lock", lambda _root: nullcontext()), \
             patch.object(rd, "fetch_confirmed_transfers", return_value=[]), \
             patch.object(rd, "_refresh_project_unlocked", side_effect=fake_refresh), \
             redirect_stdout(out):
            code = rd.main()
        return code, out.getvalue()

    def test_every_path_it_prints_actually_exists(self):
        """The general invariant, which the #229 bug violated: naming a path in the success
        summary is a claim that the path is there. Any future line that names a file the script
        doesn't write fails here, not just the `dashboard.html` case specifically."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, output = self._run_with_fake_pipeline(root)

            self.assertEqual(code, 0)
            printed_paths = re.findall(rf"{re.escape(str(root))}\S*", output)
            self.assertTrue(printed_paths, "expected the summary to name at least one path")
            for path in printed_paths:
                self.assertTrue(Path(path).exists(), f"reported a path it never wrote: {path}")

    def test_does_not_claim_to_have_written_dashboard_html(self):
        """The specific regression. `scripts/rebuild_dashboard.py` is the only writer of that
        file; this script must not imply otherwise in its output or its docstring."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, output = self._run_with_fake_pipeline(root)

            self.assertNotIn("dashboard.html", output)
            self.assertFalse((root / "dashboard.html").exists())

        self.assertNotIn("then rebuild dashboard.html", rd.__doc__ or "")

    def test_busy_exit_code_is_unchanged(self):
        """Issue #228's boot refresh treats 75 as "another refresh holds the lock" rather than a
        failure, so this script's contract with `start_dashboard.py` must not drift."""
        self.assertEqual(rd._BUSY_EXIT_CODE, 75)

        def raise_busy(_root):
            raise rd.RefreshAlreadyRunning("already running")

        with patch.object(rd, "project_refresh_lock", raise_busy), \
             patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(rd.main(), 75)


if __name__ == "__main__":
    unittest.main()
