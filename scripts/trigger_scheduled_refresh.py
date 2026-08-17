#!/usr/bin/env python3
"""Trigger POST /api/refresh at fixed lead times before each gameweek deadline (issue #101).

Trigger-agnostic like `send_deadline_reminder.py`: invoked hourly by
`.github/workflows/scheduled-refresh.yml`, but takes no opinion on what invokes it. Deliberately a
separate script/workflow from the deadline reminder -- different purpose (keeping the *shared*
market data fresh, not emailing anyone) and a different cost profile (a refresh can take up to 5
minutes and calls live FPL/transfer-source APIs, see `server.py`'s `_default_refresh_action`),
so the two stay independently schedulable/disableable.

Deadline-window resolution (how many hours until the next gameweek's deadline, and whether "now"
falls in one of the configured lead-time windows) reuses `fpl_intel.sources.deadline_windows` -- the exact
same live-bootstrap-fetch + stateless-window-check arithmetic `send_deadline_reminder.py` already
uses for issue #55, extracted to `fpl_intel/deadline_windows.py` when this script needed it too,
rather than re-deriving or copy-pasting it.

Configuration is entirely environment-variable driven, matching this repo's other scripts:

- `FPL_INTEL_REFRESH_TOKEN` (required): the same operator-only bearer token `/api/refresh` has
  always required (issue #27) -- this script is just another trusted, secret-holding caller, not
  a new grant of public access.
- `FPL_INTEL_DASHBOARD_BASE_URL` (required): the live dashboard's public origin, e.g.
  `https://web-production-1b285.up.railway.app`. Unlike `send_deadline_reminder.py`'s use of the
  same env var (an email footer link, safe to fall back to a placeholder), this script actually
  calls the URL -- there's no safe default, so it's required here.

No `--dry-run` email-preview concept applies here (there's no email); `--dry-run` instead skips
the actual `POST /api/refresh` call and just reports whether this run would have triggered one.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fpl_intel.sources.deadline_windows import (
    DeadlineDataError, in_send_window, load_bootstrap_and_fixtures, next_unfinished_event,
)


class ConfigError(RuntimeError):
    """Malformed or missing configuration. Messages never include the refresh token."""


REFRESH_TOKEN_ENV_VAR = "FPL_INTEL_REFRESH_TOKEN"
DASHBOARD_BASE_URL_ENV_VAR = "FPL_INTEL_DASHBOARD_BASE_URL"

# The four checkpoints requested in issue #101: T-2d, T-1d, T-12h, T-3h before each gameweek
# deadline. Each is independently checked every hourly tick via the same `(lead_hours - 1,
# lead_hours]` window `send_deadline_reminder.py` uses -- multiple checkpoints landing in the
# same tick (not possible here since they're hours apart, but the logic doesn't assume that)
# would still only trigger one refresh call, per `_REFRESH_COOLDOWN_SECONDS`'s existing global
# cooldown on the server side.
TRIGGER_LEAD_HOURS = (48, 24, 12, 3)


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


def run(dry_run, base_url, token, root=ROOT, now=None):
    """Core run loop, factored out of `main` so tests can inject `now` and avoid argv/env parsing.
    `base_url`/`token` are None in `--dry-run` mode -- never read unless a checkpoint actually
    matches and a real call is about to be made."""
    now = now or datetime.now(timezone.utc)
    bootstrap, _fixtures, stale = load_bootstrap_and_fixtures(root)
    event = next_unfinished_event(bootstrap)
    if event is None or not event.get("deadline_time"):
        print("checked: no upcoming gameweek deadline found")
        return 0

    deadline_iso = event["deadline_time"]
    event_id = event.get("id")
    matched_lead_hours = sorted(
        (lead_hours for lead_hours in TRIGGER_LEAD_HOURS if in_send_window(deadline_iso, now, lead_hours)),
        reverse=True,
    )
    if not matched_lead_hours:
        print("checked: outside window")
        return 0

    checkpoint = matched_lead_hours[0]
    if stale:
        print(
            f"warning: live bootstrap fetch failed, deadline resolved from cached data "
            f"(GW{event_id}, T-{checkpoint}h checkpoint)",
            file=sys.stderr,
        )
    if dry_run:
        print(f"dry-run: would trigger refresh for GW{event_id} at T-{checkpoint}h checkpoint")
        return 0

    result = trigger_refresh(base_url, token)
    print(f"refresh triggered for GW{event_id} at T-{checkpoint}h checkpoint: {result.get('status')}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report whether this run would trigger a refresh, without calling /api/refresh or "
        "requiring FPL_INTEL_REFRESH_TOKEN/FPL_INTEL_DASHBOARD_BASE_URL.",
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
    except DeadlineDataError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 1
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
