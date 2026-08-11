"""Issue #27: unit tests for `scripts/start_dashboard.py`'s pure config-resolution logic.

Only `resolve_server_config` is exercised here -- no real server is started, matching this
codebase's convention of keeping env-var precedence/defaulting logic in small pure functions
independently testable without spinning up a process (see `tests/test_send_deadline_reminder.py`
for the same pattern applied to `scripts/send_deadline_reminder.py`).
"""

from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

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
            {"host": "127.0.0.1", "port": 8877, "token": None, "allowed_origin": None},
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
                "FPL_INTEL_ALLOWED_ORIGIN": "https://fpl-intelligence.up.railway.app",
            },
            cli_port=None,
        )

        self.assertEqual(
            config,
            {
                "host": "0.0.0.0",
                "port": 8080,
                "token": "op-token",
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


if __name__ == "__main__":
    unittest.main()
