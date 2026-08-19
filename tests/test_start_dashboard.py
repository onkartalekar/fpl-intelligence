"""Issue #27: unit tests for `scripts/start_dashboard.py`'s pure config-resolution logic.

No real server is started here, matching this codebase's convention of keeping env-var
precedence/defaulting logic in small pure functions independently testable without spinning up a
process (see `tests/test_send_deadline_reminder.py` for the same pattern applied to
`scripts/send_deadline_reminder.py`). Issue #228 added `refresh_if_stale` to the same startup
path and the same treatment: the subprocess is mocked, so nothing here refreshes for real.
"""

from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

# scripts/ is not a package (no __init__.py, matching the rest of this repo's scripts/), so the
# module under test is loaded directly from its file path rather than imported by dotted name.
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "start_dashboard.py"
_SPEC = importlib.util.spec_from_file_location("start_dashboard", _SCRIPT_PATH)
start_dashboard = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(start_dashboard)


class ResolveServerConfigTests(unittest.TestCase):
    def test_local_default_with_no_env_and_no_cli_port(self):
        config = start_dashboard.resolve_server_config({}, cli_port=None)

        self.assertEqual(
            config,
            {
                "hosted": False,
                "host": "127.0.0.1",
                "port": 8877,
                "token": None,
                "reminder_teams_token": None,
                "allowed_origin": None,
            },
        )

    def test_port_env_var_switches_host_to_0_0_0_0_and_sets_the_port(self):
        config = start_dashboard.resolve_server_config({"PORT": "3000"}, cli_port=None)

        self.assertEqual(config["host"], "0.0.0.0")
        self.assertEqual(config["port"], 3000)

    def test_explicit_cli_port_wins_over_the_port_env_var(self):
        config = start_dashboard.resolve_server_config({"PORT": "3000"}, cli_port=9999)

        self.assertEqual(config["port"], 9999)
        # Precedence is about the *port value* only -- PORT being present is still what decides
        # the hosted-mode host, independent of which value ultimately wins for the port itself.
        self.assertEqual(config["host"], "0.0.0.0")

    def test_explicit_cli_port_without_port_env_var_keeps_localhost_host(self):
        config = start_dashboard.resolve_server_config({}, cli_port=9999)

        self.assertEqual(config["port"], 9999)
        self.assertEqual(config["host"], "127.0.0.1")

    def test_unrelated_env_vars_do_not_trigger_hosted_mode(self):
        config = start_dashboard.resolve_server_config(
            {"PATH": "/usr/bin", "HOME": "/home/user"}, cli_port=None
        )

        self.assertEqual(config["host"], "127.0.0.1")
        self.assertEqual(config["port"], 8877)

    def test_refresh_token_env_var_is_passed_through(self):
        config = start_dashboard.resolve_server_config(
            {"FPL_INTEL_REFRESH_TOKEN": "secret-value"}, cli_port=None
        )

        self.assertEqual(config["token"], "secret-value")

    def test_refresh_token_defaults_to_none_when_unset(self):
        config = start_dashboard.resolve_server_config({}, cli_port=None)

        self.assertIsNone(config["token"])

    def test_reminder_teams_token_env_var_is_passed_through(self):
        config = start_dashboard.resolve_server_config(
            {"FPL_INTEL_REMINDER_TEAMS_TOKEN": "reminder-secret"}, cli_port=None
        )

        self.assertEqual(config["reminder_teams_token"], "reminder-secret")

    def test_reminder_teams_token_defaults_to_none_when_unset(self):
        config = start_dashboard.resolve_server_config({}, cli_port=None)

        self.assertIsNone(config["reminder_teams_token"])

    def test_allowed_origin_env_var_is_passed_through(self):
        config = start_dashboard.resolve_server_config(
            {"FPL_INTEL_ALLOWED_ORIGIN": "https://example.up.railway.app"}, cli_port=None
        )

        self.assertEqual(config["allowed_origin"], "https://example.up.railway.app")

    def test_allowed_origin_defaults_to_none_when_unset(self):
        config = start_dashboard.resolve_server_config({}, cli_port=None)

        self.assertIsNone(config["allowed_origin"])

    def test_full_hosted_configuration(self):
        config = start_dashboard.resolve_server_config(
            {
                "PORT": "8080",
                "FPL_INTEL_REFRESH_TOKEN": "op-token",
                "FPL_INTEL_REMINDER_TEAMS_TOKEN": "reminder-secret",
                "FPL_INTEL_ALLOWED_ORIGIN": "https://fpl-intelligence.up.railway.app",
            },
            cli_port=None,
        )

        self.assertEqual(
            config,
            {
                "hosted": True,
                "host": "0.0.0.0",
                "port": 8080,
                "token": "op-token",
                "reminder_teams_token": "reminder-secret",
                "allowed_origin": "https://fpl-intelligence.up.railway.app",
            },
        )


class SeedMissingDataFilesTests(unittest.TestCase):
    """Fix for the live Railway bug: a volume mounted at `data/` shadows the git-tracked seed
    files that used to live directly under it. `seed_missing_data_files` is the primary fix --
    copying each one in from the sibling `data-seed/` directory (which the volume mount does not
    shadow) the first time `data/<filename>` is missing, whether that's a freshly mounted volume
    or a fresh local clone that never had these gitignored files either.
    """

    def _make_seed_dir(self, root):
        seed_dir = root / "data-seed"
        seed_dir.mkdir()
        for filename in start_dashboard.SEEDED_DATA_FILENAMES:
            (seed_dir / filename).write_text(
                json.dumps({"seed": filename}), encoding="utf-8"
            )
        return seed_dir

    def test_copies_each_seeded_file_when_data_dir_is_missing_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_seed_dir(root)

            start_dashboard.seed_missing_data_files(root)

            for filename in start_dashboard.SEEDED_DATA_FILENAMES:
                target = root / "data" / filename
                self.assertTrue(target.exists())
                self.assertEqual(
                    json.loads(target.read_text(encoding="utf-8")), {"seed": filename}
                )

    def test_is_a_no_op_and_does_not_overwrite_an_existing_target_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_seed_dir(root)
            data_dir = root / "data"
            data_dir.mkdir()
            existing_filename = start_dashboard.SEEDED_DATA_FILENAMES[0]
            (data_dir / existing_filename).write_text(
                json.dumps({"real": "refreshed-value"}), encoding="utf-8"
            )

            start_dashboard.seed_missing_data_files(root)

            self.assertEqual(
                json.loads((data_dir / existing_filename).read_text(encoding="utf-8")),
                {"real": "refreshed-value"},
            )
            # The other two were still missing, so they *should* have been seeded.
            for filename in start_dashboard.SEEDED_DATA_FILENAMES[1:]:
                self.assertTrue((data_dir / filename).exists())

    def test_logs_one_line_per_file_actually_seeded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_seed_dir(root)

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                start_dashboard.seed_missing_data_files(root)

            output = buffer.getvalue()
            for filename in start_dashboard.SEEDED_DATA_FILENAMES:
                self.assertIn(f"Seeded data/{filename} from data-seed/ (first boot)", output)
            self.assertEqual(len(output.strip().splitlines()), len(start_dashboard.SEEDED_DATA_FILENAMES))

    def test_prints_nothing_when_every_target_file_already_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_seed_dir(root)
            data_dir = root / "data"
            data_dir.mkdir()
            for filename in start_dashboard.SEEDED_DATA_FILENAMES:
                (data_dir / filename).write_text(json.dumps({"real": True}), encoding="utf-8")

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                start_dashboard.seed_missing_data_files(root)

            self.assertEqual(buffer.getvalue(), "")

    def test_is_a_no_op_when_the_seed_dir_itself_does_not_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # No data-seed/ at all -- should not raise, should not create data/.

            start_dashboard.seed_missing_data_files(root)

            self.assertFalse((root / "data").exists())

    def test_creates_the_data_dir_if_it_does_not_exist_yet(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_seed_dir(root)
            # No data/ directory at all yet -- a genuinely fresh clone/volume.

            start_dashboard.seed_missing_data_files(root)

            self.assertTrue((root / "data").is_dir())
            for filename in start_dashboard.SEEDED_DATA_FILENAMES:
                self.assertTrue((root / "data" / filename).exists())

    def test_real_data_seed_directory_actually_seeds_the_real_repo_files(self):
        """End-to-end sanity check against this repo's real `data-seed/`, not a synthetic one."""
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data-seed").symlink_to(repo_root / "data-seed", target_is_directory=True)

            start_dashboard.seed_missing_data_files(root)

            for filename in start_dashboard.SEEDED_DATA_FILENAMES:
                target = root / "data" / filename
                self.assertTrue(target.exists())
                json.loads(target.read_text(encoding="utf-8"))  # still valid JSON


NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def _root_with_state(directory, generated_at):
    """A temp root holding a `data/dashboard-state.json`, plus the `scripts/` entry point
    `refresh_if_stale` checks for before deciding it has anything to run."""
    root = Path(directory)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "refresh_dashboard.py").write_text("", encoding="utf-8")
    if generated_at is not None:
        (root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": generated_at}), encoding="utf-8"
        )
    return root


class CachedStateAgeTests(unittest.TestCase):
    """Issue #228: age of the cached generation, and the several ways it can be unusable."""

    def test_reports_age_of_a_parseable_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = _root_with_state(directory, (NOW - timedelta(hours=3)).isoformat())

            self.assertAlmostEqual(
                start_dashboard.cached_state_age_seconds(root, now=NOW), 3 * 3600, delta=1
            )

    def test_handles_a_non_utc_offset(self):
        """`generated_at` is written in the profile's own timezone, not UTC -- an offset-aware
        timestamp three hours old must read as three hours old regardless of its offset."""
        with tempfile.TemporaryDirectory() as directory:
            local = (NOW - timedelta(hours=3)).astimezone(timezone(timedelta(hours=-4)))
            root = _root_with_state(directory, local.isoformat())

            self.assertAlmostEqual(
                start_dashboard.cached_state_age_seconds(root, now=NOW), 3 * 3600, delta=1
            )

    def test_missing_state_file_reads_as_no_usable_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = _root_with_state(directory, generated_at=None)

            self.assertIsNone(start_dashboard.cached_state_age_seconds(root, now=NOW))

    def test_unusable_timestamps_never_raise(self):
        """Every one of these must return None rather than propagating: this runs on the startup
        path, where an exception would cost the user their dashboard entirely."""
        for label, payload in (
            ("malformed json", "{not json"),
            ("missing key", json.dumps({})),
            ("null value", json.dumps({"generated_at": None})),
            ("unparseable", json.dumps({"generated_at": "not-a-timestamp"})),
            ("wrong type", json.dumps({"generated_at": 1234})),
            ("naive timestamp", json.dumps({"generated_at": "2026-08-19T12:00:00"})),
        ):
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as directory:
                    root = _root_with_state(directory, generated_at=None)
                    (root / "data" / "dashboard-state.json").write_text(payload, encoding="utf-8")

                    self.assertIsNone(start_dashboard.cached_state_age_seconds(root, now=NOW))


class RefreshIfStaleTests(unittest.TestCase):
    """Issue #228: the boot refresh's decision to run, and its refusal to ever block startup."""

    def _run(self, root, hosted=False, now=NOW, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            ran = start_dashboard.refresh_if_stale(root, hosted=hosted, now=now, **kwargs)
        return ran, out.getvalue(), err.getvalue()

    def test_refreshes_when_the_cache_is_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = _root_with_state(directory, (NOW - timedelta(days=8)).isoformat())

            with patch.object(start_dashboard.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], 0)) as mock_run:
                ran, out, _ = self._run(root)

            self.assertTrue(ran)
            mock_run.assert_called_once()
            self.assertIn("8.0d old", out)

    def test_skips_when_the_cache_is_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = _root_with_state(directory, (NOW - timedelta(minutes=10)).isoformat())

            with patch.object(start_dashboard.subprocess, "run") as mock_run:
                ran, out, _ = self._run(root)

            self.assertFalse(ran)
            mock_run.assert_not_called()
            self.assertEqual(out, "")

    def test_refreshes_a_clone_that_has_never_refreshed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = _root_with_state(directory, generated_at=None)

            with patch.object(start_dashboard.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], 0)) as mock_run:
                ran, out, _ = self._run(root)

            self.assertTrue(ran)
            mock_run.assert_called_once()
            self.assertIn("no cached data", out)

    def test_hosted_never_refreshes_at_boot(self):
        """Railway runs this same script. A blocking pre-`create_server` refresh would delay port
        binding on every deploy, and the hourly workflow already covers the hosted server."""
        with tempfile.TemporaryDirectory() as directory:
            root = _root_with_state(directory, (NOW - timedelta(days=8)).isoformat())

            with patch.object(start_dashboard.subprocess, "run") as mock_run:
                ran, out, _ = self._run(root, hosted=True)

            self.assertFalse(ran)
            mock_run.assert_not_called()
            self.assertEqual(out, "")

    def test_announces_before_blocking(self):
        """The message has to be emitted *before* the subprocess call, not after: everything else
        `main()` prints happens after `create_server`, so without this the terminal is silent for
        the whole refresh and reads as a hang."""
        printed_before_subprocess = []

        def record(*args, **kwargs):
            printed_before_subprocess.append(out_stream.getvalue())
            return subprocess.CompletedProcess([], 0)

        with tempfile.TemporaryDirectory() as directory:
            root = _root_with_state(directory, (NOW - timedelta(days=8)).isoformat())
            out_stream = io.StringIO()
            with patch.object(start_dashboard.subprocess, "run", side_effect=record), \
                 redirect_stdout(out_stream):
                start_dashboard.refresh_if_stale(root, hosted=False, now=NOW)

        self.assertIn("refreshing before start", printed_before_subprocess[0])

    def test_lock_contention_is_reported_calmly_and_does_not_block_startup(self):
        """Exit 75 means another refresh already holds the project lock -- a normal outcome when
        a manual refresh, an /api/refresh, or a second start_dashboard.py is already running."""
        with tempfile.TemporaryDirectory() as directory:
            root = _root_with_state(directory, (NOW - timedelta(days=8)).isoformat())

            with patch.object(
                start_dashboard.subprocess, "run",
                return_value=subprocess.CompletedProcess([], start_dashboard.REFRESH_BUSY_EXIT_CODE),
            ):
                ran, out, err = self._run(root)

            self.assertFalse(ran)
            self.assertIn("Another refresh is already running", out)
            self.assertEqual(err, "")

    def test_no_failure_mode_prevents_startup(self):
        """The central guarantee of issue #228's ask 2. Before it, the worst case was stale data;
        it must not become a dashboard that refuses to start. `fetch_bootstrap()` in particular is
        unprotected inside the pipeline and propagates on an offline machine.
        """
        for label, side_effect, return_value in (
            ("nonzero exit", None, subprocess.CompletedProcess([], 1)),
            ("timeout", subprocess.TimeoutExpired("refresh", 180), None),
            ("interpreter missing", OSError("No such file or directory"), None),
            ("ctrl-c during refresh", KeyboardInterrupt(), None),
        ):
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as directory:
                    root = _root_with_state(directory, (NOW - timedelta(days=8)).isoformat())

                    with patch.object(start_dashboard.subprocess, "run",
                                      side_effect=side_effect, return_value=return_value):
                        ran, out, err = self._run(root)

                self.assertFalse(ran)
                self.assertIn("cached data", out + err)

    def test_missing_refresh_script_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = _root_with_state(directory, (NOW - timedelta(days=8)).isoformat())
            (root / "scripts" / "refresh_dashboard.py").unlink()

            with patch.object(start_dashboard.subprocess, "run") as mock_run:
                ran, _, _ = self._run(root)

            self.assertFalse(ran)
            mock_run.assert_not_called()

    def test_boot_refresh_is_bounded_by_a_timeout(self):
        """`refresh_dashboard.py` has no timeout of its own -- the 300s cap lives in the endpoint,
        not the script -- so the boot path must impose one rather than inheriting none."""
        with tempfile.TemporaryDirectory() as directory:
            root = _root_with_state(directory, (NOW - timedelta(days=8)).isoformat())

            with patch.object(start_dashboard.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], 0)) as mock_run:
                self._run(root)

            self.assertEqual(
                mock_run.call_args.kwargs.get("timeout"),
                start_dashboard.BOOT_REFRESH_TIMEOUT_SECONDS,
            )
            self.assertLess(start_dashboard.BOOT_REFRESH_TIMEOUT_SECONDS, 600)


if __name__ == "__main__":
    unittest.main()
