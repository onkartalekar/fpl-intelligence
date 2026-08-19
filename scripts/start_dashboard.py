#!/usr/bin/env python3
"""Start the FPL dashboard service, locally or on a hosting platform (issue #27).

Local use (no `PORT` in the environment) is unchanged from before #27: binds `127.0.0.1`,
opens the browser, refresh token is a fresh random-per-process value nobody can use out of
band. Hosted use (Railway injects `PORT`) binds `0.0.0.0`, skips opening a browser, and reads
`FPL_INTEL_REFRESH_TOKEN`/`FPL_INTEL_ALLOWED_ORIGIN` from the environment so an operator can
actually use `/api/refresh` and so the Host/Origin allowlist matches the real deployed hostname.
Also reads `FPL_INTEL_REMINDER_TEAMS_TOKEN` (issue #105) -- a separate secret gating
`/api/reminder-teams`, deliberately not `FPL_INTEL_REFRESH_TOKEN` reused, so a leak of either
token compromises only what that token actually gates (see `server.py`'s `create_server` docstring).

Issue #228 added one piece of local-only startup behavior: a boot refresh when the cached
generation has gone stale (`refresh_if_stale`), since nothing else refreshes a local checkout --
the hourly GitHub Actions workflow calls the *hosted* origin over HTTP and cannot reach
`127.0.0.1`. This is a boot-time check, deliberately not a timer: an in-process scheduler thread
was declined in `SPECIFICATION.md` and `IMPLEMENTATION_PLAN.md`, and nothing here revisits that.
A server left running for days still will not refresh itself.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fpl_intel.generation import resolve_artifact
from fpl_intel.server import create_server


DEFAULT_PORT = 8877
PORT_ENV_VAR = "PORT"  # Railway's own convention -- injected automatically, no setup needed.
REFRESH_TOKEN_ENV_VAR = "FPL_INTEL_REFRESH_TOKEN"
REMINDER_TEAMS_TOKEN_ENV_VAR = "FPL_INTEL_REMINDER_TEAMS_TOKEN"
ALLOWED_ORIGIN_ENV_VAR = "FPL_INTEL_ALLOWED_ORIGIN"

# Files that legitimately live at `data/<filename>` (read there by `refresh.py`/`server.py`
# via `resolve_artifact`) but are also git-tracked seed data. A Railway volume mounted at
# `data/` shadows the whole directory at runtime -- the tracked copies are still in the image
# layer, just unreachable from `data/` once the volume takes over -- so a fresh volume (or a
# fresh local clone, which never had these gitignored files either) needs them copied in from
# `data-seed/`, a sibling directory the volume mount does not shadow, before the server starts
# accepting requests.
SEEDED_DATA_FILENAMES = (
    "confirmed-transfers.json",
    "official-transfers-latest.json",
    "fpl-fixtures-latest.json",
)

# Issue #228: how stale `dashboard-state.json` may be before a local boot refreshes it first.
# One hour matches the hosted cadence `.github/workflows/scheduled-refresh.yml` now runs at, so
# a local checkout and the shared deployment target the same freshness.
BOOT_REFRESH_MAX_AGE_SECONDS = 3600
# Hard ceiling on the boot refresh. `refresh_dashboard.py` has no timeout of its own -- the
# 300s cap lives in the *endpoint* (`server_handlers/refresh_endpoint.py`'s
# `default_refresh_action`), not in the script -- and its only intrinsic limits are per-HTTP-call
# (`fetch_confirmed_transfers(timeout=30)` across 21 playlist requests, plus bootstrap and
# fixtures), so an unbounded worst case runs to roughly ten minutes. A boot path must not inherit
# that: a slow upstream should cost a bounded delay and then a start with stale data, never an
# apparent hang. 180s is ~6x the ~30s a healthy refresh actually takes.
BOOT_REFRESH_TIMEOUT_SECONDS = 180
# `scripts/refresh_dashboard.py`'s "another refresh already holds the project lock" exit code.
# Not an error: a manual refresh, an `/api/refresh` call, or a second `start_dashboard.py`
# booting concurrently is already doing the work this boot wanted done.
REFRESH_BUSY_EXIT_CODE = 75


def seed_missing_data_files(root):
    """Copy each seeded file from `data-seed/` to `data/` the first time it's missing.

    Idempotent and non-destructive: never overwrites a `data/<filename>` that already exists,
    whether that's because a previous boot already seeded it or because a refresh has since
    written a real value over it -- once that happens the seed copy is irrelevant going
    forward. Silent when there's nothing to do (every ordinary local dev boot after the first,
    and every warm redeploy with an already-populated volume), one line printed per file
    actually seeded.
    """
    root = Path(root)
    data_dir = root / "data"
    seed_dir = root / "data-seed"
    for filename in SEEDED_DATA_FILENAMES:
        target = data_dir / filename
        if target.exists():
            continue
        source = seed_dir / filename
        if not source.exists():
            continue
        data_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        print(f"Seeded data/{filename} from data-seed/ (first boot)")


def cached_state_age_seconds(root, now=None):
    """Age of the cached `dashboard-state.json`, or None when there is no usable timestamp.

    None is deliberately not an error condition -- it collapses three cases that all warrant the
    same response (refresh): a clone that has never refreshed and so has no state file at all, a
    file that can't be parsed, and a `generated_at` that is missing, malformed, or naive. Reading
    a broken cache is exactly when fresh data is most wanted, and none of these may raise: this
    runs on the startup path, where any exception would cost the user their dashboard.
    """
    now = now or datetime.now(timezone.utc)
    try:
        state_path = resolve_artifact(root, "dashboard-state.json")
        if not state_path.is_file():
            return None
        generated_at = json.loads(state_path.read_text(encoding="utf-8")).get("generated_at")
        generated = datetime.fromisoformat(generated_at) if generated_at else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    # A naive timestamp can't be compared against an aware `now`, and guessing a timezone for it
    # would be worse than just refreshing.
    if generated is None or generated.tzinfo is None:
        return None
    return (now - generated).total_seconds()


def _describe_age(age_seconds):
    if age_seconds is None:
        return "no cached data"
    if age_seconds < 90:
        return f"cached data is {int(age_seconds)}s old"
    if age_seconds < 5400:
        return f"cached data is {age_seconds / 60:.0f}m old"
    if age_seconds < 172800:
        return f"cached data is {age_seconds / 3600:.1f}h old"
    return f"cached data is {age_seconds / 86400:.1f}d old"


def refresh_if_stale(
    root,
    hosted,
    max_age_seconds=BOOT_REFRESH_MAX_AGE_SECONDS,
    timeout=BOOT_REFRESH_TIMEOUT_SECONDS,
    now=None,
):
    """Refresh live data before the server starts, when the local cache has gone stale (#228).

    Returns True when a refresh actually ran to completion, False otherwise. **Never raises**,
    and its return value is advisory only: nothing about starting the server is conditional on
    it. That is the whole design constraint here -- before this existed, the worst case was
    stale data; it must not become a dashboard that refuses to start because an upstream API was
    down or the machine was offline. `refresh.py`'s pipeline degrades gracefully for two of its
    three sources (fixtures fall back to cache, transfers are caught by `refresh_dashboard.py`),
    but `fetch_bootstrap()` is unprotected and propagates, so failure is caught here at the call
    site rather than assumed away.

    Hosted deployments skip this entirely. Railway runs this same script (`Procfile`), where a
    blocking pre-`create_server` refresh would delay port binding on every deploy and restart --
    and since #228 the hosted server is refreshed hourly by
    `.github/workflows/scheduled-refresh.yml` anyway, so a boot refresh there buys nothing.
    Binding first and refreshing after is not a fix for that: connections would queue in the
    listen backlog and hang rather than being refused.
    """
    if hosted:
        return False

    age_seconds = cached_state_age_seconds(root, now=now)
    if age_seconds is not None and age_seconds <= max_age_seconds:
        return False

    script = Path(root) / "scripts" / "refresh_dashboard.py"
    if not script.is_file():
        return False

    # Printed *before* blocking, and flushed: every other sign of life in `main()` happens after
    # `create_server`, so without this the terminal sits silent for ~30s with no URL and no
    # browser -- indistinguishable from a hang, and an invitation to Ctrl-C the very refresh
    # being waited on. stdout is block-buffered when piped or redirected, so `flush` is load-
    # bearing rather than decorative.
    print(
        f"{_describe_age(age_seconds)} -- refreshing before start (~30s, Ctrl-C to skip).",
        flush=True,
    )
    try:
        completed = subprocess.run([sys.executable, str(script)], cwd=str(root), timeout=timeout)
    except KeyboardInterrupt:
        # Turns the hazard above into a deliberate escape hatch: skip the wait, start on stale
        # data. A second Ctrl-C still stops the server itself.
        print("Refresh skipped -- starting with cached data.", flush=True)
        return False
    except subprocess.TimeoutExpired:
        print(
            f"Refresh timed out after {timeout}s -- starting with cached data.",
            file=sys.stderr, flush=True,
        )
        return False
    except OSError as error:
        print(f"Refresh could not run ({error}) -- starting with cached data.", file=sys.stderr, flush=True)
        return False

    if completed.returncode == REFRESH_BUSY_EXIT_CODE:
        # Someone else is already doing this work; nothing is wrong and nothing needs retrying.
        print("Another refresh is already running -- starting with its data.", flush=True)
        return False
    if completed.returncode:
        print(
            f"Refresh failed (exit {completed.returncode}) -- starting with cached data.",
            file=sys.stderr, flush=True,
        )
        return False
    return True


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
        # Returned rather than re-derived by callers: issue #228's boot refresh needs the same
        # local-vs-hosted distinction the host choice is already made from, and one source for
        # it beats two copies of `PORT_ENV_VAR in env` drifting apart.
        "hosted": hosted,
        "host": "0.0.0.0" if hosted else "127.0.0.1",
        "port": port,
        "token": env.get(REFRESH_TOKEN_ENV_VAR),
        "reminder_teams_token": env.get(REMINDER_TEAMS_TOKEN_ENV_VAR),
        "allowed_origin": env.get(ALLOWED_ORIGIN_ENV_VAR),
    }


def main():
    parser = argparse.ArgumentParser(description="Start the FPL dashboard service")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically")
    args = parser.parse_args()

    config = resolve_server_config(os.environ, cli_port=args.port)
    seed_missing_data_files(ROOT)
    # Issue #228, after seeding (so a first boot has a baseline to compare) and before the
    # server exists (so nobody is served the stale generation this is about to replace).
    refresh_if_stale(ROOT, hosted=config["hosted"])
    server = create_server(
        ROOT,
        host=config["host"],
        port=config["port"],
        token=config["token"],
        reminder_teams_token=config["reminder_teams_token"],
        allowed_origin=config["allowed_origin"],
    )
    bound_host, bound_port = config["host"], server.server_port
    if bound_host == "0.0.0.0":
        print(f"FPL dashboard: listening on 0.0.0.0:{bound_port} (hosted mode)")
    else:
        url = f"http://{bound_host}:{bound_port}/dashboard.html"
        print(f"FPL dashboard: {url}")
    if config["hosted"]:
        print("Refreshes run when POST /api/refresh is called -- hourly from")
        print("`.github/workflows/scheduled-refresh.yml`, or manually by an operator.")
    else:
        print("Data older than 1h is refreshed at startup. While the service keeps running,")
        print("refresh with `python3 scripts/refresh_dashboard.py` or POST /api/refresh.")
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
