#!/usr/bin/env python3
"""Archive each registered team's real weekly decision at each deadline checkpoint (issue #102).

Forecast-accuracy tracking existed for exactly one legacy team (`archive_forecast`, gated on
`config/user-profile.json`'s `config_team_id`) and only archived the shared, generic squad-
construction recommendation -- never any team's actual weekly transfer/captaincy decision. This
script closes that gap for every registered team, at three fixed checkpoints per gameweek.

A recommendation computed after a deadline has passed is not a valid stand-in for what would have
been recommended before it (hindsight contamination -- final prices, resolved injuries, played
fixtures all leak into a post-deadline computation). So this has to run on a schedule, not
on-visit, and has to capture each snapshot strictly before the deadline it's about.

Trigger-agnostic like `send_deadline_reminder.py`/`trigger_scheduled_refresh.py`: invoked hourly
by `.github/workflows/scheduled-refresh.yml` (issue #102's own dependency note: this reuses that
workflow's existing hourly tick rather than introducing a second independent scheduler), but takes
no opinion on what invokes it.

Deadline-window resolution reuses `fpl_intel.sources.deadline_windows` -- the same live-bootstrap-fetch +
stateless-window-check arithmetic `send_deadline_reminder.py`/`trigger_scheduled_refresh.py`
already use, checked against all three of `CHECKPOINT_LEAD_HOURS` (the same 3/12/24-hour values
issue #79 already exposes to visitors as their reminder-timing choice, `server.py`'s
`_ALLOWED_REMINDER_LEAD_HOURS`) rather than inventing new checkpoint values, instead of the single
lead_hours value `send_deadline_reminder.py` checks.

Configuration, entirely environment-variable driven, matching this repo's other scripts:

- `FPL_INTEL_DASHBOARD_BASE_URL` (required): the live dashboard's public origin.
- `FPL_INTEL_REFRESH_TOKEN` (required): the same operator secret `/api/refresh` already requires
  -- reused for `/api/registered-teams` and `/api/archive-team-forecast` too, since neither
  exposes any PII (bare team IDs and recommendation metadata only), unlike issue #105's
  `/api/reminder-teams`, which needed its own dedicated token specifically because it returns
  every opted-in manager's email address in bulk.

`--dry-run` reports which checkpoint(s) matched and how many teams would be archived for, without
calling `/api/registered-teams` or `/api/archive-team-forecast` at all.
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

# Issue #102's decided checkpoints -- the same three values issue #79 already exposes to visitors
# as their reminder-timing choice (server.py's _ALLOWED_REMINDER_LEAD_HOURS), not new ones.
CHECKPOINT_LEAD_HOURS = (3, 12, 24)


def _require_dashboard_base_url():
    raw = os.environ.get(DASHBOARD_BASE_URL_ENV_VAR)
    if not raw or not raw.strip():
        raise ConfigError(
            f"{DASHBOARD_BASE_URL_ENV_VAR} is required (used to reach /api/registered-teams and "
            "/api/archive-team-forecast)."
        )
    return raw.strip().rstrip("/")


def _require_refresh_token():
    raw = os.environ.get(REFRESH_TOKEN_ENV_VAR)
    if not raw or not raw.strip():
        raise ConfigError(f"{REFRESH_TOKEN_ENV_VAR} is required.")
    return raw.strip()


def fetch_registered_teams(base_url, token, timeout=30):
    """GET /api/registered-teams (issue #102): every team_id with a saved profile, capped
    server-side at `_REGISTERED_TEAMS_CAP`."""
    request = Request(
        f"{base_url}/api/registered-teams", method="GET", headers={"X-Refresh-Token": token},
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())["team_ids"]


def archive_team_forecast(base_url, token, team_id, lead_hours, timeout=30):
    """POST /api/archive-team-forecast (issue #102): archive one team's real weekly decision at
    one checkpoint. Returns the parsed response body, e.g. `{"status": "ok", "archived": bool}`.
    """
    request = Request(
        f"{base_url}/api/archive-team-forecast",
        method="POST",
        headers={"X-Refresh-Token": token, "Content-Type": "application/json"},
        data=json.dumps({"team_id": team_id, "lead_hours": lead_hours}).encode("utf-8"),
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def run(dry_run, base_url, token, root=ROOT, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        bootstrap, _fixtures, _stale = load_bootstrap_and_fixtures(root)
    except DeadlineDataError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 1

    event = next_unfinished_event(bootstrap)
    if event is None or not event.get("deadline_time"):
        print("No upcoming gameweek deadline found -- nothing to check.")
        return 0

    matching_checkpoints = [
        lead_hours for lead_hours in CHECKPOINT_LEAD_HOURS
        if in_send_window(event["deadline_time"], now, lead_hours)
    ]
    if not matching_checkpoints:
        print("checked: outside every archive window")
        return 0

    if dry_run:
        print(
            f"would archive at checkpoint(s) {matching_checkpoints} for GW{event.get('id')} "
            "(--dry-run: not fetching the team list or calling the archive endpoint)"
        )
        return 0

    try:
        team_ids = fetch_registered_teams(base_url, token)
    except (HTTPError, URLError, OSError, ValueError) as error:
        print(f"Failed to fetch registered teams: {error!r}", file=sys.stderr)
        return 1

    archived_count = 0
    attempted_count = 0
    for team_id in team_ids:
        for lead_hours in matching_checkpoints:
            attempted_count += 1
            try:
                result = archive_team_forecast(base_url, token, team_id, lead_hours)
            except (HTTPError, URLError, OSError, ValueError) as error:
                print(f"Archive call failed for team {team_id} at {lead_hours}h: {error!r}", file=sys.stderr)
                continue
            if result.get("archived"):
                archived_count += 1

    print(
        f"archived {archived_count}/{attempted_count} team-checkpoint forecast(s) for "
        f"GW{event.get('id')} across {len(team_ids)} registered team(s)"
    )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report which checkpoint(s) match, without calling /api/registered-teams or "
        "/api/archive-team-forecast or requiring FPL_INTEL_REFRESH_TOKEN/FPL_INTEL_DASHBOARD_BASE_URL.",
    )
    args = parser.parse_args(argv)

    base_url = token = None
    if not args.dry_run:
        try:
            base_url = _require_dashboard_base_url()
            token = _require_refresh_token()
        except ConfigError as error:
            print(f"Configuration error: {error}", file=sys.stderr)
            return 1

    return run(args.dry_run, base_url, token)


if __name__ == "__main__":
    raise SystemExit(main())
