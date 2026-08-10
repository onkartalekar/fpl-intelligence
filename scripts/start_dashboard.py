#!/usr/bin/env python3
"""Start the FPL dashboard service, locally or on a hosting platform (issue #27).

Local use (no `PORT` in the environment) is unchanged from before #27: binds `127.0.0.1`,
opens the browser, refresh token is a fresh random-per-process value nobody can use out of
band. Hosted use (Railway injects `PORT`) binds `0.0.0.0`, skips opening a browser, and reads
`FPL_INTEL_REFRESH_TOKEN`/`FPL_INTEL_ALLOWED_ORIGIN` from the environment so an operator can
actually use `/api/refresh` and so the Host/Origin allowlist matches the real deployed hostname.
"""

import argparse
import os
from pathlib import Path
import sys
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fpl_intel.server import create_server


DEFAULT_PORT = 8877
PORT_ENV_VAR = "PORT"  # Railway's own convention -- injected automatically, no setup needed.
REFRESH_TOKEN_ENV_VAR = "FPL_INTEL_REFRESH_TOKEN"
ALLOWED_ORIGIN_ENV_VAR = "FPL_INTEL_ALLOWED_ORIGIN"


def resolve_server_config(env, cli_port=None):
    """Resolve host/port/token/allowed_origin from the environment and an optional CLI port.

    Pure and side-effect-free (no server started, nothing printed) so the precedence/defaulting
    logic below is directly unit-testable, matching this codebase's convention of keeping
    env-var resolution in small pure functions (see `scripts/send_deadline_reminder.py`'s
    `parse_reminder_teams`/`resolve_profiles_db_path`).

    Port precedence, most to least specific: an explicit `--port` CLI flag, then the `PORT`
    env var (Railway injects this), then the historical local default of 8877.

    Host: `0.0.0.0` only when `PORT` is actually present in `env` -- this is the signal that
    we're running under a PaaS expecting to receive its own proxied traffic, not a plain local
    shell that happens to have some unrelated `PORT` variable set for another purpose. Absent
    `PORT`, host stays `127.0.0.1`, exactly as every local run has always behaved.
    """
    hosted = PORT_ENV_VAR in env
    if cli_port is not None:
        port = cli_port
    elif hosted:
        port = int(env[PORT_ENV_VAR])
    else:
        port = DEFAULT_PORT
    return {
        "host": "0.0.0.0" if hosted else "127.0.0.1",
        "port": port,
        "token": env.get(REFRESH_TOKEN_ENV_VAR),
        "allowed_origin": env.get(ALLOWED_ORIGIN_ENV_VAR),
    }


def main():
    parser = argparse.ArgumentParser(description="Start the FPL dashboard service")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically")
    args = parser.parse_args()

    config = resolve_server_config(os.environ, cli_port=args.port)
    server = create_server(
        ROOT,
        host=config["host"],
        port=config["port"],
        token=config["token"],
        allowed_origin=config["allowed_origin"],
    )
    bound_host, bound_port = config["host"], server.server_port
    if bound_host == "0.0.0.0":
        print(f"FPL dashboard: listening on 0.0.0.0:{bound_port} (hosted mode)")
    else:
        url = f"http://{bound_host}:{bound_port}/dashboard.html"
        print(f"FPL dashboard: {url}")
    print("Refreshes run only when POST /api/refresh is called (e.g. from a script or")
    print("`python3 scripts/refresh_dashboard.py`). No schedule is configured.")
    print("Press Control-C to stop the service.")
    # No local browser to open in a hosted container -- and nothing sensible to open it to
    # anyway, since 0.0.0.0 isn't a browsable address.
    if not args.no_open and bound_host != "0.0.0.0":
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping FPL dashboard service.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
