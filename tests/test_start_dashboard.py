"""Issue #27: unit tests for `scripts/start_dashboard.py`'s pure config-resolution logic.

Only `resolve_server_config` is exercised here -- no real server is started, matching this
codebase's convention of keeping env-var precedence/defaulting logic in small pure functions
independently testable without spinning up a process (see `tests/test_send_deadline_reminder.py`
for the same pattern applied to `scripts/send_deadline_reminder.py`).
"""

import importlib.util
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
