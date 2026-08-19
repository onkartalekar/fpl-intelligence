#!/usr/bin/env python3
"""Trigger POST /api/refresh on every invocation (issues #101, #228).

Trigger-agnostic like `send_deadline_reminder.py`: invoked hourly by
`.github/workflows/scheduled-refresh.yml`, but takes no opinion on what invokes it. Deliberately a
separate script/workflow from the deadline reminder -- different purpose (keeping the *shared*
market data fresh, not emailing anyone) and a different cost profile (a refresh can take up to 5
minutes and calls live FPL/transfer-source APIs, see `server.py`'s `_default_refresh_action`),
so the two stay independently schedulable/disableable.

Issue #228 removed this script's deadline-window gate. It previously fired only inside four
narrow windows before each gameweek deadline (T-48h/T-24h/T-12h/T-3h), so the hosted dashboard
got roughly four refreshes per *gameweek* and nothing in between -- transfer news landing
mid-week stayed invisible for days. Every tick now triggers a refresh outright. A refresh costs
~30 seconds against the live FPL/PL APIs, which at an hourly cadence is affordable, and the
server's own `REFRESH_COOLDOWN_SECONDS` still guards against a genuine stampede.

Two deliberate consequences of dropping the gate, stated here so neither reads as an oversight:

- **This script no longer resolves the gameweek deadline at all**, so it no longer fetches the
  FPL bootstrap -- one fewer live API dependency in the trigger path. `deadline_windows.py` stays
  in the tree; it is still load-bearing for `send_deadline_reminder.py` and
  `archive_team_forecasts.py`, which keep their own independent windows.
- **Refreshes continue through the off-season**, when there is no upcoming deadline to be
  relative to. That is intentional, not an accident of deleting the short-circuit: transfer
  activity is heaviest precisely when no gameweek is imminent, which is the case this issue
  exists to fix.

Configuration is entirely environment-variable driven, matching this repo's other scripts:

- `FPL_INTEL_REFRESH_TOKEN` (required): the same operator-only bearer token `/api/refresh` has
  always required (issue #27) -- this script is just another trusted, secret-holding caller, not
  a new grant of public access.
- `FPL_INTEL_DASHBOARD_BASE_URL` (required): the live dashboard's public origin, e.g.
  `https://web-production-1b285.up.railway.app`. Unlike `send_deadline_reminder.py`'s use of the
  same env var (an email footer link, safe to fall back to a placeholder), this script actually
  calls the URL -- there's no safe default, so it's required here.

No `--dry-run` email-preview concept applies here (there's no email). Since #228 every run
triggers, so `--dry-run` no longer reports a conditional decision -- it skips the actual
`POST /api/refresh` call and serves as a config/plumbing check that needs no secrets.
"""

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ConfigError(RuntimeError):
    """Malformed or missing configuration. Messages never include the refresh token."""


REFRESH_TOKEN_ENV_VAR = "FPL_INTEL_REFRESH_TOKEN"
DASHBOARD_BASE_URL_ENV_VAR = "FPL_INTEL_DASHBOARD_BASE_URL"


def _dashboard_base_url():
    raw = os.environ.get(DASHBOARD_BASE_URL_ENV_VAR)
    if not raw or not raw.strip():
        raise ConfigError(f"{DASHBOARD_BASE_URL_ENV_VAR} is required and was not set.")
    return raw.strip().rstrip("/")


def _refresh_token():
    raw = os.environ.get(REFRESH_TOKEN_ENV_VAR)
    if not raw or not raw.strip():
        raise ConfigError(f"{REFRESH_TOKEN_ENV_VAR} is required and was not set.")
    return raw.strip()


def trigger_refresh(base_url, token, timeout=310):
    """POST /api/refresh. Raises RuntimeError with the server's own status/message on failure --
    the timeout is deliberately a little over the server's own 300s subprocess timeout
    (`server.py`'s `_default_refresh_action`), so a slow-but-succeeding refresh isn't cut off
    client-side first."""
    request = Request(
        f"{base_url}/api/refresh",
        data=b"{}",
        method="POST",
        headers={"X-Refresh-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"refresh request failed: HTTP {error.code} {body}") from error
    except URLError as error:
        raise RuntimeError(f"refresh request failed: {error.reason}") from error


def run(dry_run, base_url, token):
    """Core run loop, factored out of `main` so tests can drive it without argv/env parsing.

    Issue #228: unconditional -- there is no window check and no deadline lookup left to make,
    so this is now just "call the endpoint, or say that you would have". `base_url`/`token` are
    None in `--dry-run` mode and are never read on that path.
    """
    if dry_run:
        print("dry-run: would trigger refresh (every run triggers; no window check since #228)")
        return 0

    result = trigger_refresh(base_url, token)
    print(f"refresh triggered: {result.get('status')}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip the actual /api/refresh call (and the "
        "FPL_INTEL_REFRESH_TOKEN/FPL_INTEL_DASHBOARD_BASE_URL requirement). Since #228 every "
        "run triggers a refresh, so this is a plumbing check rather than a report on whether "
        "this particular run would have fired.",
    )
    args = parser.parse_args(argv)

    base_url = token = None
    if not args.dry_run:
        try:
            base_url = _dashboard_base_url()
            token = _refresh_token()
        except ConfigError as error:
            print(f"Configuration error: {error}", file=sys.stderr)
            return 1

    try:
        return run(args.dry_run, base_url, token)
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
