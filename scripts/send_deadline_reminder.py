#!/usr/bin/env python3
"""Email a transfer-deadline reminder with current recommendations (issue #55).

Trigger-agnostic by design: this script takes no opinion on what invokes it. Today it is invoked
hourly by `.github/workflows/deadline-reminder.yml`; if/when issue #27's hosted deployment lands,
the host's own scheduler can invoke this unchanged (see plans/issue-55-deadline-email-reminder.md).

Configuration is entirely environment-variable driven, matching the existing `FPL_INTEL_LLM_*`
pattern in `src/fpl_intel/news_signals.py`:

- `FPL_INTEL_REMINDER_TEAMS`: a JSON list of recipients, one object per team, e.g.
  `[{"team_id": 123456, "email": "manager@example.com", "lead_hours": 3}]`. `lead_hours` is
  optional and defaults to 3.
- `FPL_INTEL_REMINDER_PROFILES_DB` (issue #80, source changed by issue #105): optional, unset by
  default -- explicit opt-in, matching the rest of this module's env-var-driven config (nothing
  here is ever auto-detected from a file's mere presence). Set it to a truthy value ("1"/"true"/
  "yes", case-insensitive) to add recipients from `profiles.db`'s self-serve opt-in data (issue
  #79): every team with `reminder_status == 'enabled'` and a confirmed `email`, using each row's
  own `reminder_lead_hours`. As of issue #105, this no longer reads a local file path -- it fetches
  the roster live from the hosted dashboard's `GET /api/reminder-teams` (see
  `FPL_INTEL_REMINDER_TEAMS_TOKEN` below), the same reason `FPL_INTEL_DASHBOARD_BASE_URL`/
  `FPL_INTEL_REFRESH_TOKEN` below exist: a GitHub Actions runner has no shared filesystem with
  Railway's `profiles.db` to read directly. Unioned with `FPL_INTEL_REMINDER_TEAMS` by `team_id`:
  when the same team_id appears in both sources, the `FPL_INTEL_REMINDER_TEAMS` entry wins -- an
  operator explicitly hand-configuring the secret for a team is a stronger, more deliberate signal
  than an opportunistic profiles.db read, so it should not be silently overridden by one. Neither
  source is required on its own, but leaving both unset/empty still fails loudly with a
  `ConfigError`, exactly as an unset `FPL_INTEL_REMINDER_TEAMS` always has -- this script never
  silently does nothing because nothing at all was configured.
- `FPL_INTEL_SMTP_HOST` / `FPL_INTEL_SMTP_PORT` / `FPL_INTEL_SMTP_USER` / `FPL_INTEL_SMTP_PASSWORD`:
  SMTP credentials (e.g. Gmail's `smtp.gmail.com:587` with an app password). Resolved lazily,
  inside `run()`, only once there's an actual in-window team to email this run -- never required
  by `--dry-run`, and not required by a real run either on a tick where nobody happens to be
  in-window, so an hourly cron doesn't fail every single tick before anyone's ever configured
  these, or before anyone is due a reminder this particular hour.
- `FPL_INTEL_DASHBOARD_BASE_URL` (issue #125): the live dashboard's public origin, e.g.
  `https://web-production-1b285.up.railway.app`. Required in both real and `--dry-run` runs --
  each in-window team's recommendation is now fetched live from the hosted dashboard's
  `/api/manager-view`/`/api/shared-state` (not computed locally; a GitHub Actions runner has no
  shared filesystem with Railway to compute it from, see issues #105/#122's findings), and these
  are plain reads, so `--dry-run` still makes them for a genuine preview.
- `FPL_INTEL_REFRESH_TOKEN` (issue #125): the same operator secret `/api/refresh` already requires
  (issue #27) -- reused here, not a new secret, to exempt this script's per-team
  `/api/manager-view` calls from the visitor-tuned rate limit that would otherwise trip on the
  second team in a loop of more than one.
- `FPL_INTEL_REMINDER_TEAMS_TOKEN` (issue #105): required only when `FPL_INTEL_REMINDER_PROFILES_DB`
  is enabled -- gates `/api/reminder-teams`, which returns every opted-in manager's email address
  in bulk. Deliberately a **separate** secret from `FPL_INTEL_REFRESH_TOKEN`, not a reuse of it: a
  leak of the refresh token must not also hand over the entire reminder roster.

Log hygiene: this script never prints recipient email addresses or SMTP credentials to stdout or
stderr during normal (non-dry-run) operation -- only generic status lines. `--dry-run` is the one
exception, by design, since its entire purpose is showing a human what would be sent.
"""

import argparse
from datetime import datetime, timezone
from email.message import EmailMessage
import json
import os
from pathlib import Path
import smtplib
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fpl_intel.notifications import email_template, pitch_image
from fpl_intel.sources.deadline_windows import DeadlineDataError, hours_until, in_send_window, next_unfinished_event
from fpl_intel.sources.deadline_windows import load_bootstrap_and_fixtures as _shared_load_bootstrap_and_fixtures


DEFAULT_LEAD_HOURS = 3

REMINDER_TEAMS_ENV_VAR = "FPL_INTEL_REMINDER_TEAMS"
REMINDER_PROFILES_DB_ENV_VAR = "FPL_INTEL_REMINDER_PROFILES_DB"
SMTP_HOST_ENV_VAR = "FPL_INTEL_SMTP_HOST"
SMTP_PORT_ENV_VAR = "FPL_INTEL_SMTP_PORT"
SMTP_USER_ENV_VAR = "FPL_INTEL_SMTP_USER"
SMTP_PASSWORD_ENV_VAR = "FPL_INTEL_SMTP_PASSWORD"

# Issue #83: the HTML email's footer "Manage reminder settings" link needs a base URL to point
# at the live dashboard's Profile tab (issue #79's reminder card). server.py's own confirmation
# link (`_default_reminder_opt_in_action`) builds this from the live request's trusted `Host`
# header -- there is no such request here, since this script is an offline cron job, not a
# request handler. `FPL_INTEL_DASHBOARD_BASE_URL` is this script's equivalent explicit
# configuration knob; unset, it falls back to the dashboard's own documented local-dev default
# port (`server.py`'s `create_server(..., port=8877)` default). A real deployment should set this
# explicitly to the dashboard's real public origin -- the fallback exists only so `--dry-run`
# produces a plausible, well-formed link out of the box.
DASHBOARD_BASE_URL_ENV_VAR = "FPL_INTEL_DASHBOARD_BASE_URL"
_DEFAULT_DASHBOARD_BASE_URL = "http://localhost:8877"

# Issue #125: the same operator secret `/api/refresh` already requires (issue #27) -- see
# `_require_refresh_token`'s docstring for why this script now needs it too.
REFRESH_TOKEN_ENV_VAR = "FPL_INTEL_REFRESH_TOKEN"

# Issue #105: a separate secret from REFRESH_TOKEN_ENV_VAR, gating /api/reminder-teams -- see
# `fetch_reminder_teams`'s docstring for why this is deliberately not a reuse of the refresh token.
REMINDER_TEAMS_TOKEN_ENV_VAR = "FPL_INTEL_REMINDER_TEAMS_TOKEN"

# Issue #288 stopgap: until now, the only thing preventing a duplicate send was the one-hour-wide
# `in_send_window` band landing at most one hourly cron tick -- true only while GitHub's schedule
# cron actually ticks roughly hourly. #288 found it degrading to 3-6h gaps, so the workflow's cron
# is being widened to `*/15 * * * *` to make sure a tick still lands inside a checkpoint's window
# even when GitHub's dispatch is badly delayed. That widening means up to ~4 ticks can now land
# inside one still-open window on a *healthy* schedule -- without this, every one of those ticks
# would re-send the same reminder. `FPL_INTEL_REMINDER_SENT_STATE` closes that gap: the workflow
# reads back the GH Actions variable it wrote after the last real send, passes it in here as JSON
# (`{"event_id": <int>, "lead_hours": [<int>, ...]}`), and `run()` skips any lead_hours already
# recorded sent for the *current* event_id. A different event_id (next gameweek) is treated as no
# prior state at all -- nothing to carry over.
REMINDER_SENT_STATE_ENV_VAR = "FPL_INTEL_REMINDER_SENT_STATE"

_DIVIDER = "-" * 60


class ConfigError(RuntimeError):
    """Malformed or missing configuration. Messages never include the parsed email addresses."""


def parse_reminder_teams(raw_value):
    """Parse and validate `FPL_INTEL_REMINDER_TEAMS`. Raises `ConfigError` with no email values in it."""
    if raw_value is None or not raw_value.strip():
        raise ConfigError(
            f"{REMINDER_TEAMS_ENV_VAR} is not set or empty. Expected a JSON list of objects like "
            '{"team_id": 123456, "email": "manager@example.com", "lead_hours": 3}.'
        )
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise ConfigError(f"{REMINDER_TEAMS_ENV_VAR} is not valid JSON: {error}") from error
    if not isinstance(parsed, list) or not parsed:
        raise ConfigError(f"{REMINDER_TEAMS_ENV_VAR} must be a non-empty JSON list.")
    teams = []
    for index, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            raise ConfigError(f"{REMINDER_TEAMS_ENV_VAR}[{index}] must be a JSON object.")
        team_id = entry.get("team_id")
        if not isinstance(team_id, int) or isinstance(team_id, bool):
            raise ConfigError(f"{REMINDER_TEAMS_ENV_VAR}[{index}].team_id must be an integer.")
        email = entry.get("email")
        if not isinstance(email, str) or "@" not in email or not email.strip():
            raise ConfigError(f"{REMINDER_TEAMS_ENV_VAR}[{index}].email must be a valid-looking email address.")
        lead_hours = entry.get("lead_hours", DEFAULT_LEAD_HOURS)
        if isinstance(lead_hours, bool) or not isinstance(lead_hours, int) or lead_hours <= 0:
            raise ConfigError(f"{REMINDER_TEAMS_ENV_VAR}[{index}].lead_hours must be a positive integer.")
        teams.append({"team_id": team_id, "email": email, "lead_hours": lead_hours})
    return teams


def parse_sent_state(raw_value):
    """Parse `FPL_INTEL_REMINDER_SENT_STATE` into `(event_id, {lead_hours, ...})`.

    Best-effort by design (issue #288 stopgap): this is a dedup optimization on top of the
    already-correct `in_send_window` check, not a new correctness requirement, so any way this can
    fail -- unset, blank, malformed JSON, a GH Actions variable that doesn't exist yet on the very
    first run -- returns `(None, set())`, i.e. "no prior send known," rather than raising. A false
    "nothing sent yet" costs at most a duplicate email, exactly the pre-existing failure mode this
    is layered on top of; raising here would turn a best-effort marker into a hard dependency and
    could fail an otherwise-good run over a corrupted variable.
    """
    if raw_value is None or not raw_value.strip():
        return None, set()
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return None, set()
    if not isinstance(parsed, dict):
        return None, set()
    event_id = parsed.get("event_id")
    lead_hours = parsed.get("lead_hours")
    if not isinstance(event_id, int) or isinstance(event_id, bool):
        return None, set()
    if not isinstance(lead_hours, list):
        return None, set()
    sent = {value for value in lead_hours if isinstance(value, int) and not isinstance(value, bool)}
    return event_id, sent


def profiles_db_source_enabled(raw_value):
    """Whether `FPL_INTEL_REMINDER_PROFILES_DB` opts into the profiles.db-sourced roster (issue
    #80). Unset/blank means disabled -- explicit opt-in only, no auto-detection.

    Issue #105: this used to resolve to a local filesystem path (a truthy sentinel meant the
    default `<root>/data/profiles.db`, anything else an explicit path). It is now a plain boolean
    flag -- the roster is always fetched from the hosted dashboard's `/api/reminder-teams` when
    enabled, never read from a local path, so "explicit path" no longer means anything. Any
    non-blank value enables it; the historical "1"/"true"/"yes" sentinel set still works (it's a
    subset of "non-blank"), so existing deployments configuring one of those values keep working
    unchanged.
    """
    return raw_value is not None and bool(raw_value.strip())


def fetch_reminder_teams(base_url, token, timeout=30):
    """GET /api/reminder-teams (issue #105): the opted-in reminder roster -- `team_id`, `email`,
    `lead_hours` per team with a confirmed, *live* opt-in (`reminder_status == 'enabled'` and a
    non-empty `email`) -- computed server-side by the one process with legitimate `profiles.db`
    access, the same reason `fetch_shared_state`/`fetch_manager_view` exist (a GitHub Actions
    runner has no shared filesystem with Railway to read `profiles.db` from directly). Gated by
    its own dedicated `X-Reminder-Teams-Token` header -- deliberately not `token`
    (`fetch_manager_view`'s `/api/refresh` secret), since this endpoint returns every opted-in
    manager's email address in bulk and has no safe unauthenticated response at all.

    Returns the parsed `teams` list directly (not the raw response envelope), already shaped as
    `{"team_id": ..., "email": ..., "lead_hours": ...}` per entry -- the same shape
    `parse_reminder_teams` produces, so callers can treat both sources identically.
    """
    request = Request(
        f"{base_url}/api/reminder-teams",
        method="GET",
        headers={"X-Reminder-Teams-Token": token},
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())["teams"]


def collect_teams(reminder_teams_raw, profiles_db_raw, dashboard_base_url=None, reminder_teams_token=None):
    """Build the final `teams` list `run()` operates on (issue #80, HTTP-sourced since #105).

    `FPL_INTEL_REMINDER_TEAMS` and `FPL_INTEL_REMINDER_PROFILES_DB` are each independently
    optional; either alone is sufficient, and both together are unioned by `team_id`.

    When `FPL_INTEL_REMINDER_PROFILES_DB` is unset/blank, this is byte-identical to today's
    behavior: it calls `parse_reminder_teams(reminder_teams_raw)` directly and returns (or raises)
    exactly what that always has, with `dashboard_base_url`/`reminder_teams_token` never even
    touched.

    When it is enabled, `FPL_INTEL_REMINDER_TEAMS` (if also set and non-blank) is parsed the same
    way, then merged with `fetch_reminder_teams`'s rows (requires both `dashboard_base_url` and
    `reminder_teams_token`, raising `ConfigError` naming whichever is missing -- there is no safe
    default for either). On a `team_id` collision, the `FPL_INTEL_REMINDER_TEAMS` entry wins -- an
    operator explicitly hand-configuring the secret for a team is a stronger, more deliberate
    signal than an opportunistic profiles.db read, so a manually pinned entry is never silently
    overridden by one.

    Unlike the "neither source configured at all" case above (a real setup mistake, still a hard
    `ConfigError`), an empty union here is *not* an error: it means the roster is genuinely empty
    right now (no one has opted into reminders yet), which is an expected, self-resolving state on
    a fresh deployment, not a misconfiguration -- returns `[]`, and `run()` already handles an
    empty teams list as a quiet no-op.
    """
    if not profiles_db_source_enabled(profiles_db_raw):
        return parse_reminder_teams(reminder_teams_raw)

    if not dashboard_base_url:
        raise ConfigError(
            f"{DASHBOARD_BASE_URL_ENV_VAR} is required when {REMINDER_PROFILES_DB_ENV_VAR} is enabled "
            "(used to fetch the opted-in roster from /api/reminder-teams)."
        )
    if not reminder_teams_token:
        raise ConfigError(
            f"{REMINDER_TEAMS_TOKEN_ENV_VAR} is required when {REMINDER_PROFILES_DB_ENV_VAR} is "
            "enabled (used to authenticate to /api/reminder-teams)."
        )

    secret_teams = []
    if reminder_teams_raw is not None and reminder_teams_raw.strip():
        secret_teams = parse_reminder_teams(reminder_teams_raw)

    db_teams = fetch_reminder_teams(dashboard_base_url, reminder_teams_token)
    secret_team_ids = {team["team_id"] for team in secret_teams}
    merged = secret_teams + [team for team in db_teams if team["team_id"] not in secret_team_ids]

    # Deliberately not a ConfigError, even if merged is empty: unlike the early-return branch
    # above (profiles_db disabled *and* FPL_INTEL_REMINDER_TEAMS unset/empty -- genuinely nothing
    # configured, a real setup mistake worth failing loudly on), reaching this point already means
    # the operator explicitly enabled the self-serve roster. Zero currently-opted-in teams is a
    # normal, expected, self-resolving state on a fresh deployment -- nobody has visited the
    # Profile tab and turned reminders on yet -- not a misconfiguration. run() already handles an
    # empty teams list gracefully (falls through its own "outside window" no-op path), so this
    # just lets that existing behavior take over instead of failing the whole scheduled run.
    return merged


def parse_smtp_config():
    """Parse SMTP settings from env vars. Raises `ConfigError` (naming the missing var, never a value)."""
    host = os.environ.get(SMTP_HOST_ENV_VAR)
    port_raw = os.environ.get(SMTP_PORT_ENV_VAR)
    user = os.environ.get(SMTP_USER_ENV_VAR)
    password = os.environ.get(SMTP_PASSWORD_ENV_VAR)
    missing = [
        name for name, value in (
            (SMTP_HOST_ENV_VAR, host), (SMTP_PORT_ENV_VAR, port_raw),
            (SMTP_USER_ENV_VAR, user), (SMTP_PASSWORD_ENV_VAR, password),
        )
        if not value
    ]
    if missing:
        raise ConfigError(f"Missing required SMTP environment variable(s): {', '.join(missing)}")
    try:
        port = int(port_raw)
    except ValueError as error:
        raise ConfigError(f"{SMTP_PORT_ENV_VAR} must be an integer.") from error
    return {"host": host, "port": port, "user": user, "password": password}


def load_bootstrap_and_fixtures(root):
    """Fetch a fresh bootstrap/fixtures pair, falling back to the last cached refresh on failure.

    Returns `(bootstrap, fixtures, stale)`. `stale` is True if either fetch fell back to disk, so
    the composed email can carry an explicit staleness line.

    Issue #101: thin wrapper around the shared `fpl_intel.sources.deadline_windows` implementation,
    translating its generic `DeadlineDataError` into this script's own `ConfigError` -- kept as a
    local module-level function (rather than a bare re-export) so `patch.object(sdr,
    "load_bootstrap_and_fixtures", ...)` in tests keeps working unchanged.
    """
    try:
        return _shared_load_bootstrap_and_fixtures(root)
    except DeadlineDataError as error:
        raise ConfigError(str(error)) from error


def fetch_shared_state(base_url, timeout=30):
    """GET /api/shared-state (issue #125): the shared dashboard state, including
    `decision_center`'s generic recommendation used for the pre-Gameweek-2 email fallback below.
    No token needed -- this returns exactly what a no-team_id dashboard visitor already sees,
    publicly, since #120. Supersedes issue #122's `load_official_transfers`, which read
    `official-transfers-latest.json` from the local filesystem -- correct for `server.py` (runs on
    Railway, shares its volume) but never actually reachable from wherever this script runs (a
    GitHub Actions runner has no shared filesystem with Railway at all, issue #105's same finding).
    """
    request = Request(f"{base_url}/api/shared-state", method="GET")
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def fetch_manager_view(base_url, team_id, token, timeout=30):
    """GET /api/manager-view?team_id=<id> (issue #125): the JSON equivalent of a `?team_id=`
    dashboard lookup, already incorporating this team's saved profile overrides (issue #81)
    server-side -- reading Railway's *real* `profiles.db`, unlike the local
    `profiles.load_profile(root / "data" / "profiles.db", ...)` call this replaces, which could
    never resolve real data on a GitHub Actions runner for the same reason `fetch_shared_state`'s
    docstring names. The `X-Refresh-Token` header exempts this call from the visitor-tuned per-IP
    rate limit (`server.py`'s `_rate_limit_exempt`) -- this script calls it once per in-window
    team, in a tight loop, from one IP, which would otherwise trip the limiter on its own second
    call.

    Returns the parsed JSON body: `{"status": "ok"/"opted_out"/"error", "team_id": ...,
    "manager": ..., "weekly_decisions": ...}` (the latter two present only when `status == "ok"`).
    """
    request = Request(
        f"{base_url}/api/manager-view?team_id={team_id}",
        method="GET",
        headers={"X-Refresh-Token": token},
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def _format_deadline(deadline_iso):
    deadline = datetime.fromisoformat(deadline_iso.replace("Z", "+00:00"))
    return deadline.strftime("%Y-%m-%d %H:%M UTC")


def _plain_text_action_tag(action):
    """A short bracketed tag ([HOLD]/[ROLL]/[TRANSFER]) for the plain-text body (issue #83, item
    (c) polish) -- a scannable equivalent of the HTML body's color-coded action badge."""
    if action == "hold":
        return "HOLD"
    if action == "roll":
        return "ROLL"
    if action in ("single_transfer", "double_transfer"):
        return "TRANSFER"
    return (action or "N/A").upper()


def _compose_gw1_section(decision_center):
    """Compose the squad-selection section for the pre-Gameweek-2 `waiting_for_gw2` state."""
    lines = [
        "The season has not reached Gameweek 2 yet, so there is no transfer decision to make --",
        "here is the model's recommended opening squad and captaincy pick instead.",
        "",
    ]
    if not decision_center or decision_center.get("status") not in {"active_preliminary", "active"}:
        lines.append("Recommendations are not currently available.")
        return lines
    squad = decision_center.get("recommended_squad") or {}
    captaincy = decision_center.get("captaincy") or []
    captain = squad.get("captain") or {}
    vice_captain = squad.get("vice_captain") or {}
    if captain:
        lines.append(f"Recommended captain: {captain.get('name')} ({captain.get('club_short') or captain.get('club')})")
    if vice_captain:
        lines.append(f"Recommended vice-captain: {vice_captain.get('name')}")
    lines.append("")
    lines.append(f"Starting XI ({squad.get('formation', 'n/a')}):")
    for player in squad.get("starting_xi") or []:
        lines.append(f"  - {player.get('name')} ({player.get('position_short')}, {player.get('club_short') or player.get('club')})")
    bench = squad.get("bench") or []
    if bench:
        lines.append("")
        lines.append("Bench:")
        for player in bench:
            lines.append(f"  - {player.get('name')} ({player.get('position_short')})")
    if captaincy:
        lines.append("")
        lines.append("Top captaincy options:")
        for player in captaincy[:5]:
            lines.append(f"  - {player.get('name')}")
    profile_recommendations = decision_center.get("profile_recommendations") or []
    if profile_recommendations:
        lines.append("")
        lines.append(_DIVIDER)
        lines.append("All risk profiles at a glance:")
        for profile in profile_recommendations:
            profile_squad = profile.get("squad") or {}
            profile_captain = profile_squad.get("captain") or {}
            label = profile.get("label") or profile.get("id")
            profile_points = profile_squad.get("projected_event_points_including_captain")
            lines.append(
                f"  {label}: Captain: {profile_captain.get('name', 'n/a')}  |  "
                f"Formation: {profile_squad.get('formation', 'n/a')}  |  "
                f"Points: {profile_points if profile_points is not None else 'n/a'}"
            )
    return lines


def _compose_active_section(weekly):
    """Compose the transfer/draft-decision section for the `active` weekly_decisions state."""
    default_profile = weekly.get("default_profile", "balanced")
    profiles = weekly.get("profiles") or []
    profile = next((row for row in profiles if row.get("id") == default_profile), None)
    if profile is None and profiles:
        profile = profiles[0]
    lines = []
    if profile is None:
        lines.append(f"Status: {weekly.get('status')}")
        reason = weekly.get("reason")
        if reason:
            lines.append(reason)
        return lines
    recommendation = profile.get("recommendation") or {}
    label = profile.get("label") or default_profile
    action = str(recommendation.get("action") or "").replace("_", " ")
    tag = _plain_text_action_tag(recommendation.get("action"))
    lines.append(f"[{tag}] Recommended action ({label} profile): {action}")
    reason = recommendation.get("reason")
    if reason:
        lines.append(f"Reason: {reason}")
    transfers = recommendation.get("transfers") or []
    if transfers:
        lines.append("")
        lines.append("Transfers:")
        for move in transfers:
            out_player = move.get("out") or {}
            in_player = move.get("in") or {}
            lines.append(
                f"  OUT: {out_player.get('name')} ({out_player.get('club')})"
                f"  ->  IN: {in_player.get('name')} ({in_player.get('club')})"
            )
    captain = recommendation.get("captain") or {}
    lines.append("")
    if captain:
        lines.append(f"Captain: {captain.get('name')}")
    points = recommendation.get("projected_event_points_including_captain")
    if points is not None:
        lines.append(f"Projected points this gameweek (incl. captain): {points}")
    net_gain = recommendation.get("net_gain_5gw")
    if net_gain is not None:
        lines.append(f"Net gain over 5 gameweeks (after any hit cost): {net_gain}")
    lines.append(
        f"Point cost: {recommendation.get('point_cost', 0)}  |  "
        f"Bank after: £{recommendation.get('bank_after')}m  |  "
        f"Free transfers next GW: {recommendation.get('free_transfers_next_event')}"
    )
    if weekly.get("draft"):
        lines.append("")
        lines.append(
            "(This is feedback on your self-declared preseason draft squad, not an official "
            "in-season transfer.)"
        )
    if profiles:
        lines.append("")
        lines.append(_DIVIDER)
        lines.append("All risk profiles at a glance:")
        for row in profiles:
            row_recommendation = row.get("recommendation") or {}
            row_label = row.get("label") or row.get("id")
            row_tag = _plain_text_action_tag(row_recommendation.get("action"))
            row_action = str(row_recommendation.get("action") or "").replace("_", " ")
            row_captain = row_recommendation.get("captain") or {}
            row_points = row_recommendation.get("projected_event_points_including_captain")
            lines.append(
                f"  [{row_tag}] {row_label}: {row_action}  |  Captain: {row_captain.get('name', 'n/a')}  |  "
                f"Points: {row_points if row_points is not None else 'n/a'}  |  "
                f"Cost: {row_recommendation.get('point_cost', 0)}"
            )
    return lines



# ---------------------------------------------------------------------------------------------
# HTML email body (issue #83).
#
# Table-based layout (candidate (a) from plans/issue-83-reminder-html-email.md) plus an inline-
# <svg> starting-XI pitch diagram (candidate (b)), built together per the plan's "Mockup review"
# section. No <style> block, no CSS custom properties, no flexbox/grid -- every style is an
# inline `style=""` attribute on nested `<table role="presentation">` markup, since that is the
# only subset every major email client (including Outlook desktop) has rendered consistently for
# two decades. Colors are literal hex values (not CSS custom properties, which Outlook desktop
# does not support at all).
# ---------------------------------------------------------------------------------------------

# Literal hex badge text colors, reused as-is from the plan's own research and the mockup review
# -- not re-derived here. `roll` (green): banking a transfer, or transferring at zero point cost.
# `hit` (red): a transfer that costs points. Only the foreground/text shades are still needed as
# module-level names -- full badges now go through `email_template.badge_html`'s `variant` names
# directly (see `_badge_for_recommendation` below), but `_transfer_row_html`'s OUT/IN text and the
# net-gain figure below color plain text with these, not a filled badge box. Sourced from
# `email_template` (issue #190 extracted this repo's shared email palette out of this module so
# the release-notes email could reuse it without duplicating the CSS) -- kept under their
# original names here so this module's own call sites and `tests/test_send_deadline_reminder.py`
# (which asserts against these names directly) are unaffected.
_BADGE_ROLL_FG = email_template.BADGE_ROLL_FG
_BADGE_HIT_FG = email_template.BADGE_HIT_FG
_BADGE_AMBER_BG = email_template.BADGE_AMBER_BG
_BADGE_AMBER_FG = email_template.BADGE_AMBER_FG

_EMAIL_BG = email_template.EMAIL_BG
_CARD_BG = email_template.CARD_BG
_CARD_BORDER = email_template.CARD_BORDER
_TEXT_PRIMARY = email_template.TEXT_PRIMARY
_TEXT_MUTED = email_template.TEXT_MUTED
_SURFACE_INSET_BG = email_template.SURFACE_INSET_BG
_AMBER_NOTE_BORDER = email_template.AMBER_NOTE_BORDER

def _dashboard_base_url():
    """Resolve the base URL for the footer's "Manage reminder settings" link. See
    `DASHBOARD_BASE_URL_ENV_VAR`'s module-level comment for why this differs from server.py's
    live-request `Host`-header approach."""
    raw = os.environ.get(DASHBOARD_BASE_URL_ENV_VAR)
    if raw and raw.strip():
        return raw.strip().rstrip("/")
    return _DEFAULT_DASHBOARD_BASE_URL


def _require_dashboard_base_url():
    """Issue #125: unlike `_dashboard_base_url()` above (a cosmetic email-footer link, safe to
    fall back to a placeholder), `run()` now actually calls this URL to fetch live recommendations
    -- there's no safe default for that, so it's required, with no fallback."""
    raw = os.environ.get(DASHBOARD_BASE_URL_ENV_VAR)
    if not raw or not raw.strip():
        raise ConfigError(
            f"{DASHBOARD_BASE_URL_ENV_VAR} is required (used to fetch live recommendations from "
            "the hosted dashboard's /api/shared-state and /api/manager-view)."
        )
    return raw.strip().rstrip("/")


def _require_refresh_token():
    """Issue #125: the same operator secret `/api/refresh` already requires (issue #27) -- reused
    here, not a new secret, to exempt `fetch_manager_view`'s per-team calls from the visitor-tuned
    rate limit (see `fetch_manager_view`'s docstring)."""
    raw = os.environ.get(REFRESH_TOKEN_ENV_VAR)
    if not raw or not raw.strip():
        raise ConfigError(f"{REFRESH_TOKEN_ENV_VAR} is required (used to fetch live recommendations).")
    return raw.strip()


_esc = email_template.esc


def _badge_for_recommendation(recommendation):
    """Map an action + point_cost to (label, variant), `variant` being one of
    `email_template.badge_html`'s known names.

    `hold` -> info (blue). `roll`, and `single_transfer`/`double_transfer` with `point_cost == 0`
    (an already-free transfer, no hit) -> roll (green) -- this resolves the plan doc's explicitly
    open zero-cost-transfer badge-color question, per the plan's own stated lean: it is not
    costing the manager anything, the same as banking the transfer. `single_transfer`/
    `double_transfer` with `point_cost > 0` -> hit (red), labeled with the point cost exactly as
    the mockup shows ("TRANSFER · −4").
    """
    action = recommendation.get("action")
    point_cost = recommendation.get("point_cost") or 0
    if action == "hold":
        return "HOLD", "info"
    if action == "roll":
        return "ROLL", "roll"
    if action in ("single_transfer", "double_transfer"):
        if point_cost > 0:
            return f"TRANSFER · −{point_cost}", "hit"
        return "TRANSFER", "roll"
    label = (action or "N/A").replace("_", " ").upper()
    return label, "info"


_badge_html = email_template.badge_html


def _profile_eyebrow(profile_id, label, default_profile_id):
    text = (label or profile_id or "").upper()
    if profile_id == default_profile_id:
        text = f"{text} · DEFAULT"
    return text


def _pitch_section_html(starting_xi, captain_id):
    """The "RECOMMENDED STARTING XI" section: a plain `<img>` pointing at the
    `/api/reminder-pitch.png` endpoint (`pitch_image.py`/`server_handlers/reminder_pitch.py`),
    rendered server-side as a real PNG.

    Issue #240: this used to be a raw inline `<svg>`, which rendered fine in Apple Mail/Yahoo but
    was silently mangled by Gmail's HTML sanitizer into unstyled, unspaced running text (Gmail
    strips the `<svg>`/`<rect>`/`<text>` tags but keeps their text content). There's no markup
    equivalent of Outlook's MSO conditional comments to target "Gmail specifically," so rather
    than add a second client-specific carve-out, this drops inline SVG entirely in favor of a
    format every mainstream client -- Gmail included -- actually displays: a real `<img>`.
    """
    if not starting_xi:
        return ""
    image_url = f"{_dashboard_base_url()}/api/reminder-pitch.png?{pitch_image.build_query(starting_xi, captain_id)}"
    img = (
        f'<img src="{_esc(image_url)}" width="400" height="500" '
        'alt="Recommended starting XI, grouped by position, captain outlined" '
        'style="display:block;width:100%;max-width:400px;height:auto;border-radius:6px;border:0">'
    )
    return (
        '<tr><td style="padding:16px 16px 0 16px">'
        f'<div class="text-muted" style="font-size:12px;font-weight:bold;color:{_TEXT_MUTED};'
        'letter-spacing:.4px;margin-bottom:10px;font-family:Arial,Helvetica,sans-serif">'
        'RECOMMENDED STARTING XI</div>'
        + img +
        "</td></tr>"
    )


def _transfer_row_html(out_player, in_player):
    out_name = _esc(out_player.get("name"))
    in_name = _esc(in_player.get("name"))
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'class="surface-inset" style="background:{_SURFACE_INSET_BG};border-radius:4px;'
        'margin-top:10px"><tr>'
        f'<td class="badge-hit-fg" style="padding:8px 10px;font-size:13px;color:{_BADGE_HIT_FG};'
        f'font-family:Arial,Helvetica,sans-serif">OUT: {out_name}</td>'
        f'<td class="text-muted" style="padding:8px 10px;font-size:13px;color:{_TEXT_MUTED};'
        'text-align:center;font-family:Arial,Helvetica,sans-serif">&rarr;</td>'
        f'<td class="badge-roll-fg" style="padding:8px 10px;font-size:13px;color:{_BADGE_ROLL_FG};'
        f'text-align:right;font-family:Arial,Helvetica,sans-serif">IN: {in_name}</td>'
        "</tr></table>"
    )


# Which light-mode class a `_kv_row_html` value color maps to -- the row-color-override case
# (the net-gain figure below) reuses a badge FG straight as plain text color, same as
# `_transfer_row_html` above; anything else falls back to `text-primary`, `_kv_row_html`'s
# own default when no override color is given.
_FG_CLASS_BY_COLOR = {
    _BADGE_ROLL_FG: "badge-roll-fg",
    _BADGE_HIT_FG: "badge-hit-fg",
}


def _kv_row_html(label, value_html, color=None):
    value_color = color or _TEXT_PRIMARY
    value_class = _FG_CLASS_BY_COLOR.get(color, "text-primary")
    return (
        "<tr>"
        f'<td class="text-muted card-border" style="font-size:13px;color:{_TEXT_MUTED};padding:4px 0;'
        f'border-top:1px solid {_CARD_BORDER};font-family:Arial,Helvetica,sans-serif">'
        + _esc(label) + "</td>"
        f'<td align="right" class="{value_class} card-border" style="font-size:13px;'
        f'font-weight:bold;color:{value_color};padding:4px 0;border-top:1px solid {_CARD_BORDER};'
        'font-family:Arial,Helvetica,sans-serif">' + value_html + "</td>"
        "</tr>"
    )


def _kv_table_html(rows):
    body = "".join(_kv_row_html(label, value, color) for label, value, color in rows)
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin-top:10px">' + body + "</table>"
    )


def _profile_card_html(eyebrow, badge_html, rationale, transfer_html, kv_html):
    rationale_html = ""
    if rationale:
        rationale_html = (
            f'<p class="text-primary" style="margin:0;font-size:13px;color:{_TEXT_PRIMARY};'
            f'font-family:Arial,Helvetica,sans-serif;line-height:1.4">{_esc(rationale)}</p>'
        )
    return (
        '<tr><td style="padding:0 16px 14px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'class="card-bg card-border" style="background:{_CARD_BG};border:1px solid {_CARD_BORDER};'
        'border-radius:8px">'
        '<tr><td style="padding:14px 16px">'
        f'<div class="text-muted" style="font-size:11px;font-weight:bold;letter-spacing:.5px;'
        f'color:{_TEXT_MUTED};margin-bottom:8px;font-family:Arial,Helvetica,sans-serif">'
        f'{_esc(eyebrow)}</div>'
        + badge_html + rationale_html + transfer_html + kv_html
        + "</td></tr></table></td></tr>"
    )


def _gw1_profile_card_html(profile, default_profile_id):
    """One profile card for the pre-Gameweek-2 `waiting_for_gw2` state.

    Adapted from the active-state card (mockup only shows the in-season transfer-decision state):
    there is no roll/hold/transfer action before Gameweek 2 -- a manager is picking an opening
    squad, not reacting to a transfer decision -- so this card has no action badge and shows
    formation/captain/points/money-remaining instead of bank-after/free-transfers/transfer rows,
    the same field substitution the existing plain-text `_compose_gw1_section` already makes
    versus `_compose_active_section`.
    """
    squad = profile.get("squad") or {}
    eyebrow = _profile_eyebrow(profile.get("id"), profile.get("label"), default_profile_id)
    rationale = profile.get("summary") or ""
    captain = squad.get("captain") or {}
    points = squad.get("projected_event_points_including_captain")
    money_remaining = squad.get("money_remaining")
    rows = [
        ("Captain", _esc(captain.get("name") or "n/a"), None),
        ("Formation", _esc(squad.get("formation") or "n/a"), None),
        ("Projected pts (incl. captain)", _esc(points if points is not None else "n/a"), None),
        (
            "Money remaining",
            _esc(f"£{money_remaining}m" if money_remaining is not None else "n/a"),
            None,
        ),
    ]
    return _profile_card_html(eyebrow, "", rationale, "", _kv_table_html(rows))


def _active_profile_card_html(profile, default_profile_id):
    """One profile card for the in-season `active` transfer-decision state -- the HTML
    counterpart to issue #82's plain-text all-three-profiles change, matching the mockup exactly.
    """
    recommendation = profile.get("recommendation") or {}
    eyebrow = _profile_eyebrow(profile.get("id"), profile.get("label"), default_profile_id)
    badge_label, badge_variant = _badge_for_recommendation(recommendation)
    badge_html = (
        '<div style="margin-bottom:8px">' + _badge_html(badge_label, badge_variant) + "</div>"
    )
    rationale = recommendation.get("reason") or ""
    transfers = recommendation.get("transfers") or []
    transfer_html = "".join(
        _transfer_row_html(move.get("out") or {}, move.get("in") or {}) for move in transfers
    )
    captain = recommendation.get("captain") or {}
    points = recommendation.get("projected_event_points_including_captain")
    rows = [
        ("Captain", _esc(captain.get("name") or "n/a"), None),
        ("Projected pts (incl. captain)", _esc(points if points is not None else "n/a"), None),
    ]
    if transfers:
        net_gain = recommendation.get("net_gain_5gw")
        if isinstance(net_gain, (int, float)):
            color = _BADGE_ROLL_FG if net_gain >= 0 else _BADGE_HIT_FG
            net_text = _esc(f"{net_gain:+.1f}")
        else:
            color, net_text = None, "n/a"
        rows.append(("Net gain vs. holding", net_text, color))
    bank_after = recommendation.get("bank_after")
    rows.append(
        ("Bank after", _esc(f"£{bank_after}m" if bank_after is not None else "n/a"), None)
    )
    free_transfers = recommendation.get("free_transfers_next_event")
    rows.append(
        (
            "Free transfers next GW",
            _esc(free_transfers if free_transfers is not None else "n/a"),
            None,
        )
    )
    return _profile_card_html(eyebrow, badge_html, rationale, transfer_html, _kv_table_html(rows))


def _status_fallback_card_html(text):
    return (
        '<tr><td style="padding:16px">'
        f'<div class="card-bg card-border text-primary" style="background:{_CARD_BG};'
        f'border:1px solid {_CARD_BORDER};border-radius:8px;padding:16px;font-size:13px;color:'
        f'{_TEXT_PRIMARY};font-family:Arial,Helvetica,sans-serif">{_esc(text)}</div>'
        "</td></tr>"
    )


def _footer_html():
    """"Manage reminder settings" links at `<base>/#profile` -- the dashboard nav's own
    `data-view="profile"` value (see dashboard.py's `<button data-view="profile">My Profile</button>`
    and the `#reminder-panel` section rendered on that view). dashboard.js does not currently read
    `location.hash` on load to auto-select a view, so this link lands a visitor on the dashboard's
    default view today, not deep-linked directly to the reminder card; the `#profile` fragment
    names the intended destination and costs nothing, and is forward-compatible if dashboard.js
    later adds hash-based view routing. Documented explicitly here and in the PR description
    rather than left as an unexplained partial link.
    """
    manage_url = f"{_dashboard_base_url()}/#profile"
    return (
        f'<tr><td class="card-border" style="padding:20px 16px 24px;'
        f'border-top:1px solid {_CARD_BORDER}">'
        f'<p class="text-muted" style="font-size:11px;color:{_TEXT_MUTED};margin:0;'
        'font-family:Arial,Helvetica,sans-serif;line-height:1.5">'
        "You're receiving this because you opted into deadline reminders for "
        f'FPL Intelligence. <a href="{_esc(manage_url)}" class="text-muted" '
        f'style="color:{_TEXT_MUTED};text-decoration:underline">Manage reminder settings</a></p>'
        "</td></tr>"
    )


def _assemble_email_html(event_id, lead_hours, stale, extra_top_html, body_html):
    stale_html = ""
    if stale:
        stale_html = (
            '<tr><td style="padding:16px 16px 0 16px">'
            '<div class="badge-amber amber-note-border" '
            f'style="background:{_BADGE_AMBER_BG};border:1px solid {_AMBER_NOTE_BORDER};'
            f'border-radius:6px;padding:10px 12px;font-size:12px;color:{_BADGE_AMBER_FG};'
            'font-family:Arial,Helvetica,sans-serif">'
            "NOTE: the live FPL data fetch failed this run; these recommendations are from the "
            "last cached refresh and may not reflect the latest prices, injuries, or news."
            "</div></td></tr>"
        )
    header_label = f"GAMEWEEK {event_id} · DEADLINE IN {lead_hours}H"
    inner_html = (
        '<tr><td style="padding:16px 16px 0 16px">'
        + _badge_html(header_label, "info")
        + "</td></tr>"
        + stale_html
        + extra_top_html
        + body_html
        + _footer_html()
    )
    return email_template.shell("FPL Intelligence deadline reminder", inner_html)


def _compose_gw1_html_body(event_id, lead_hours, decision_center, stale):
    """HTML counterpart to `_compose_gw1_section`."""
    if not decision_center or decision_center.get("status") not in {"active_preliminary", "active"}:
        return _assemble_email_html(
            event_id, lead_hours, stale, "",
            _status_fallback_card_html("Recommendations are not currently available."),
        )
    squad = decision_center.get("recommended_squad") or {}
    captain = squad.get("captain") or {}
    starting_xi = squad.get("starting_xi") or []
    pitch_html = _pitch_section_html(starting_xi, captain.get("id"))
    default_profile_id = decision_center.get("default_profile", "balanced")
    profile_recommendations = decision_center.get("profile_recommendations") or []
    cards_html = "".join(
        _gw1_profile_card_html(profile, default_profile_id) for profile in profile_recommendations
    )
    return _assemble_email_html(event_id, lead_hours, stale, pitch_html, cards_html)


def _compose_active_html_body(event_id, lead_hours, weekly, stale):
    """HTML counterpart to `_compose_active_section`. All three profiles from `weekly["profiles"]`
    render as stacked cards (issue #82's data, issue #83's HTML rendering); the starting-XI pitch
    diagram uses the default profile's recommended lineup, matching the mockup.
    """
    default_profile_id = weekly.get("default_profile", "balanced")
    profiles_list = weekly.get("profiles") or []
    default_profile = next((row for row in profiles_list if row.get("id") == default_profile_id), None)
    if default_profile is None and profiles_list:
        default_profile = profiles_list[0]
    if default_profile is None:
        reason = weekly.get("reason") or f"Status: {weekly.get('status')}"
        return _assemble_email_html(event_id, lead_hours, stale, "", _status_fallback_card_html(reason))
    default_recommendation = default_profile.get("recommendation") or {}
    starting_xi = default_recommendation.get("starting_xi") or []
    captain = default_recommendation.get("captain") or {}
    pitch_html = _pitch_section_html(starting_xi, captain.get("id"))
    cards_html = "".join(
        _active_profile_card_html(profile, default_profile_id) for profile in profiles_list
    )
    return _assemble_email_html(event_id, lead_hours, stale, pitch_html, cards_html)


def compose_email(team, event_id, deadline_iso, hours_left, manager_view, decision_center, stale):
    """Compose one reminder email for a single team. Returns (subject, text_body, html_body).

    `text_body` is the plain-text `text/plain` fallback (sent unconditionally, per RFC 2046, as
    the first/least-preferred part of the `multipart/alternative` message `send_email` builds).
    `html_body` is the `text/html` alternative (issue #83).
    """
    weekly = manager_view["weekly_decisions"]
    status = weekly.get("status")
    lines = [
        f"FPL Intelligence -- Gameweek {event_id} deadline reminder",
        f"Deadline: {_format_deadline(deadline_iso)}",
        f"Time remaining: about {hours_left:.1f} hour(s)",
        "",
    ]
    if stale:
        lines.append(
            "NOTE: the live FPL data fetch failed this run; these recommendations are from the "
            "last cached refresh and may not reflect the latest prices, injuries, or news."
        )
        lines.append("")
    lines.append(_DIVIDER)
    lines.append("")
    lead_hours = team["lead_hours"]
    if status == "waiting_for_gw2":
        lines.extend(_compose_gw1_section(decision_center))
        html_body = _compose_gw1_html_body(event_id, lead_hours, decision_center, stale)
    elif status == "active":
        lines.extend(_compose_active_section(weekly))
        html_body = _compose_active_html_body(event_id, lead_hours, weekly, stale)
    else:
        lines.append(f"Status: {status}")
        reason = weekly.get("reason")
        if reason:
            lines.append(reason)
        html_body = _assemble_email_html(
            event_id, lead_hours, stale, "",
            _status_fallback_card_html(reason or f"Status: {status}"),
        )
    lines.append("")
    lines.append(_DIVIDER)
    lines.append("-- FPL Intelligence automated deadline reminder (issue #55)")
    body = "\n".join(lines)
    subject = f"FPL reminder: GW{event_id} deadline in ~{lead_hours}h"
    return subject, body, html_body


def send_email(smtp_config, to_email, subject, text_body, html_body):
    """Send a `multipart/alternative` message: `text/plain` first (the universal fallback per
    RFC 2046's part-ordering convention), then `text/html` (issue #83). Call order matters --
    `set_content` must run before `add_alternative` for `text/plain` to end up the first/
    least-preferred part."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp_config["user"]
    message["To"] = to_email
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    with smtplib.SMTP(smtp_config["host"], smtp_config["port"], timeout=30) as smtp:
        smtp.starttls()
        smtp.login(smtp_config["user"], smtp_config["password"])
        smtp.send_message(message)



def run(
    teams, dry_run, smtp_config, root=ROOT, now=None, dashboard_base_url=None, refresh_token=None,
    already_sent_state=(None, frozenset()),
):
    """Core run loop, factored out of `main` so tests can inject `now` and avoid argv/env parsing.

    Issue #125: `dashboard_base_url`/`refresh_token` are only used for the two live fetches below
    (`fetch_manager_view`/`fetch_shared_state`) -- the deadline-window resolution above still uses
    this script's own independent live bootstrap fetch (`load_bootstrap_and_fixtures`), by design:
    that's answering "how many hours until the next deadline," which can't be answered by asking
    Railway's own (possibly-not-yet-refreshed) state, the same reasoning issue #101's scheduled-
    refresh trigger already established for the identical question.

    Issue #288 stopgap: `already_sent_state` is `(event_id, {lead_hours, ...})` for the last real
    send this workflow recorded (see `parse_sent_state`) -- any `lead_hours` in that set for the
    *current* `event_id` is treated as already handled and excluded from this run's in-window set,
    so a cron tick that lands inside a checkpoint's window a second time (now expected, since
    #288's fix widens the cron interval to catch up faster after a delayed dispatch) does not
    re-send. A different `event_id` means a new gameweek -- nothing carries over.
    """
    if not teams:
        # Distinct from "outside window" below -- this means collect_teams() found nobody
        # currently opted in (an expected, self-resolving state, not an error -- see its own
        # docstring), not that a real roster simply doesn't match this hour's checkpoint.
        print("checked: no reminder recipients configured right now")
        return 0

    now = now or datetime.now(timezone.utc)
    bootstrap, fixtures, stale = load_bootstrap_and_fixtures(root)
    event = next_unfinished_event(bootstrap)
    if event is None or not event.get("deadline_time"):
        print("checked: no upcoming gameweek deadline found")
        return 0

    deadline_iso = event["deadline_time"]
    event_id = event.get("id")

    distinct_lead_hours = sorted({team["lead_hours"] for team in teams})
    in_window_lead_hours = {
        lead_hours for lead_hours in distinct_lead_hours
        if in_send_window(deadline_iso, now, lead_hours)
    }
    if not in_window_lead_hours:
        print("checked: outside window")
        return 0

    already_sent_event_id, already_sent_lead_hours = already_sent_state
    if already_sent_event_id == event_id and already_sent_lead_hours:
        in_window_lead_hours -= already_sent_lead_hours
    if not in_window_lead_hours:
        print(f"checked: in window for GW{event_id} but already sent for every in-window lead_hours, skipping")
        return 0

    in_window_teams = [team for team in teams if team["lead_hours"] in in_window_lead_hours]
    decision_center = None
    decision_center_fetch_attempted = False
    sent_count = 0
    sent_lead_hours = set()
    for team in in_window_teams:
        try:
            lookup = fetch_manager_view(dashboard_base_url, team["team_id"], refresh_token)
        except (HTTPError, URLError, OSError, ValueError) as error:
            print(
                f"warning: manager-view fetch failed for team {team['team_id']} ({error!r}), skipping",
                file=sys.stderr,
            )
            continue
        lookup_status = lookup.get("status")
        if lookup_status == "opted_out":
            print(f"warning: team {team['team_id']} has opted out of lookups, skipping", file=sys.stderr)
            continue
        if lookup_status != "ok":
            print(f"warning: manager-view lookup failed server-side for team {team['team_id']}, skipping", file=sys.stderr)
            continue
        manager_view = {"manager": lookup["manager"], "weekly_decisions": lookup["weekly_decisions"]}
        status = manager_view["weekly_decisions"].get("status")
        if status == "team_not_found":
            print(
                f"warning: team {team['team_id']} not found or the FPL API was unreachable, skipping",
                file=sys.stderr,
            )
            continue
        if status == "waiting_for_gw2" and not decision_center_fetch_attempted:
            decision_center_fetch_attempted = True
            try:
                decision_center = fetch_shared_state(dashboard_base_url).get("decision_center")
            except (HTTPError, URLError, OSError, ValueError) as error:
                print(
                    f"warning: shared-state fetch failed ({error!r}), recommendations unavailable this run",
                    file=sys.stderr,
                )
                decision_center = None
        hours_left = hours_until(deadline_iso, now)
        subject, body, html_body = compose_email(
            team, event_id, deadline_iso, hours_left, manager_view, decision_center, stale,
        )
        if dry_run:
            print("=" * 72)
            print(f"To: {team['email']}")
            print(f"Subject: {subject}")
            print()
            print(body)
            preview_path = Path("/tmp") / f"reminder-preview-{team['team_id']}.html"
            preview_path.write_text(html_body, encoding="utf-8")
            print()
            print(f"HTML preview written to {preview_path}")
        else:
            if smtp_config is None:
                # Resolved lazily, right here at the first actual send attempt -- not just "some
                # team is in-window" (a team can be in-window and still never reach this line,
                # e.g. lookup failure/opt-out/not-found above), and not required unconditionally
                # by main() on every tick. Without this, an hourly cron would fail every single
                # tick until SMTP was configured, even on ticks where no real send was ever going
                # to be attempted. `smtp_config` stays an explicit parameter so callers/tests can
                # still inject a fake one directly, matching every other call in this module --
                # this only resolves it when the caller didn't.
                try:
                    smtp_config = parse_smtp_config()
                except ConfigError as error:
                    print(f"Configuration error: {error}", file=sys.stderr)
                    return 1
            send_email(smtp_config, team["email"], subject, body, html_body)
        sent_count += 1
        sent_lead_hours.add(team["lead_hours"])

    if sent_count:
        if not dry_run:
            # Machine-parseable for the workflow's dedup-marker step (issue #288 stopgap) --
            # printed *before* the human-summary line below so the workflow's `tail -n 1` (which
            # keeps only the last line of this script's output out of the public run log) still
            # shows the readable summary, not this JSON. Printed as the *full* new state (this
            # run's sends merged with whatever was already recorded for this same event_id) so the
            # workflow step can write it back verbatim with no JSON handling of its own -- it
            # never needs to merge anything itself, just capture this one line.
            merged_lead_hours = sent_lead_hours | (
                already_sent_lead_hours if already_sent_event_id == event_id else set()
            )
            new_state = {"event_id": event_id, "lead_hours": sorted(merged_lead_hours)}
            print(f"reminder_sent_state: {json.dumps(new_state)}")
        verb = "printed" if dry_run else "sent"
        print(f"reminder {verb} for GW{event_id} to {sent_count} team(s)")
    else:
        print(f"checked: in window for GW{event_id} but no reminders sent (all configured teams skipped)")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print composed email(s) to stdout instead of sending. Does not require SMTP env vars.",
    )
    args = parser.parse_args(argv)

    # Issue #125: required in both --dry-run and real runs -- unlike /api/refresh (a mutating
    # action, unsafe to run idly), these are plain reads, so --dry-run still makes them for a
    # genuine preview, exactly as it always has for the (now-replaced) local compute_manager_view
    # call. Resolved before collect_teams (issue #105) since it now needs dashboard_base_url too,
    # when FPL_INTEL_REMINDER_PROFILES_DB is enabled.
    try:
        dashboard_base_url = _require_dashboard_base_url()
        refresh_token = _require_refresh_token()
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 1

    try:
        teams = collect_teams(
            os.environ.get(REMINDER_TEAMS_ENV_VAR),
            os.environ.get(REMINDER_PROFILES_DB_ENV_VAR),
            dashboard_base_url=dashboard_base_url,
            reminder_teams_token=os.environ.get(REMINDER_TEAMS_TOKEN_ENV_VAR),
        )
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 1

    # Issue: SMTP is no longer required eagerly here -- `run()` resolves it lazily, only once it
    # knows there's an actual in-window team to email this run. An hourly cron with nobody
    # currently in-window would otherwise fail every single tick until SMTP was configured, even
    # though no send was ever going to be attempted -- the exact same "don't fail for a resource
    # this particular run doesn't need" reasoning already applied to an empty teams list above.
    already_sent_state = parse_sent_state(os.environ.get(REMINDER_SENT_STATE_ENV_VAR))

    try:
        return run(
            teams, args.dry_run, None,
            dashboard_base_url=dashboard_base_url, refresh_token=refresh_token,
            already_sent_state=already_sent_state,
        )
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
