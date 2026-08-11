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
- `FPL_INTEL_REMINDER_PROFILES_DB` (issue #80): optional, unset by default -- explicit opt-in,
  matching the rest of this module's env-var-driven config (nothing here is ever auto-detected
  from a file's mere presence). When set, adds recipients from `profiles.db`'s self-serve opt-in
  data (issue #79): every team with `reminder_status == 'enabled'` and a confirmed `email`, using
  each row's own `reminder_lead_hours`. Set it to a truthy sentinel ("1"/"true"/"yes",
  case-insensitive) to use the default location `<root>/data/profiles.db`, or to an explicit path
  to point elsewhere (e.g. in a test). Unioned with `FPL_INTEL_REMINDER_TEAMS` by `team_id`: when
  the same team_id appears in both sources, the `FPL_INTEL_REMINDER_TEAMS` entry wins -- an
  operator explicitly hand-configuring the secret for a team is a stronger, more deliberate signal
  than an opportunistic profiles.db read, so it should not be silently overridden by one. Neither
  source is required on its own, but leaving both unset/empty still fails loudly with a
  `ConfigError`, exactly as an unset `FPL_INTEL_REMINDER_TEAMS` always has -- this script never
  silently does nothing because nothing at all was configured.
- `FPL_INTEL_SMTP_HOST` / `FPL_INTEL_SMTP_PORT` / `FPL_INTEL_SMTP_USER` / `FPL_INTEL_SMTP_PASSWORD`:
  SMTP credentials (e.g. Gmail's `smtp.gmail.com:587` with an app password). Not required when
  `--dry-run` is passed.

Log hygiene: this script never prints recipient email addresses or SMTP credentials to stdout or
stderr during normal (non-dry-run) operation -- only generic status lines. `--dry-run` is the one
exception, by design, since its entire purpose is showing a human what would be sent.
"""

import argparse
from datetime import datetime, timezone
from email.message import EmailMessage
import html
import json
import os
from pathlib import Path
import smtplib
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fpl_intel import profiles
from fpl_intel.fpl_data import fetch_bootstrap, fetch_fixtures
from fpl_intel.generation import resolve_artifact
from fpl_intel.recommendations import build_gw_recommendations
from fpl_intel.refresh import compute_manager_view


DEFAULT_LEAD_HOURS = 3

REMINDER_TEAMS_ENV_VAR = "FPL_INTEL_REMINDER_TEAMS"
REMINDER_PROFILES_DB_ENV_VAR = "FPL_INTEL_REMINDER_PROFILES_DB"
_PROFILES_DB_TRUTHY_SENTINELS = {"1", "true", "yes"}
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

_DIVIDER = "-" * 60


class ConfigError(RuntimeError):
    """Malformed or missing configuration. Messages never include the parsed email addresses."""


def _load_json_or(path, default):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


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


def resolve_profiles_db_path(raw_value, root=ROOT):
    """Resolve `FPL_INTEL_REMINDER_PROFILES_DB` to a concrete `profiles.db` path, or `None`.

    `None` (unset or blank) means the profiles.db source is disabled -- explicit opt-in only, no
    auto-detection of the file's presence. A truthy sentinel ("1"/"true"/"yes", case-insensitive)
    resolves to the default location `<root>/data/profiles.db`; any other non-empty value is
    treated as an explicit path to the database file (handy for tests, or a non-default layout).
    """
    if raw_value is None or not raw_value.strip():
        return None
    value = raw_value.strip()
    if value.lower() in _PROFILES_DB_TRUTHY_SENTINELS:
        return Path(root) / "data" / "profiles.db"
    return Path(value)


def load_teams_from_profiles_db(db_path):
    """Build reminder-team entries from `profiles.db`'s self-serve opt-in data (issues #79/#80).

    Yields one entry per team with a confirmed, *live* opt-in: `reminder_status == 'enabled'` and
    a non-empty `email`. Rows that are `'pending'` (confirmation email sent but link not yet
    clicked), `'declined'`, or `NULL` (never decided) are excluded -- only `confirm_reminder`
    promotes a row to `'enabled'`, and it always copies the confirmed address into `email` at the
    same time, so `reminder_status == 'enabled'` and a present `email` should always travel
    together. `reminder_lead_hours` is always set alongside `reminder_status='enabled'` by #79's
    write path (`set_reminder_pending` requires it as an argument, and `confirm_reminder` never
    touches it), so the `DEFAULT_LEAD_HOURS` fallback below is defensive, not expected to trigger
    in practice.
    """
    teams = []
    for team_id in profiles.list_team_ids(db_path):
        profile = profiles.load_profile(db_path, team_id)
        if profile is None or profile.get("reminder_status") != "enabled":
            continue
        email = profile.get("email")
        if not email:
            continue
        lead_hours = profile.get("reminder_lead_hours") or DEFAULT_LEAD_HOURS
        teams.append({"team_id": team_id, "email": email, "lead_hours": lead_hours})
    return teams


def collect_teams(reminder_teams_raw, profiles_db_raw, root=ROOT):
    """Build the final `teams` list `run()` operates on (issue #80).

    `FPL_INTEL_REMINDER_TEAMS` and `FPL_INTEL_REMINDER_PROFILES_DB` are each independently
    optional; either alone is sufficient, and both together are unioned by `team_id`.

    When `FPL_INTEL_REMINDER_PROFILES_DB` is unset/blank, this is byte-identical to today's
    behavior: it calls `parse_reminder_teams(reminder_teams_raw)` directly and returns (or raises)
    exactly what that always has, with the profiles.db path never even touched.

    When it is set, `FPL_INTEL_REMINDER_TEAMS` (if also set and non-blank) is parsed the same way,
    then merged with `load_teams_from_profiles_db`'s rows. On a `team_id` collision, the
    `FPL_INTEL_REMINDER_TEAMS` entry wins -- an operator explicitly hand-configuring the secret for
    a team is a stronger, more deliberate signal than an opportunistic profiles.db read, so a
    manually pinned entry is never silently overridden by one. If the union is still empty (both
    sources unset/empty, or profiles.db configured but matched zero `'enabled'` rows and the env
    var is also unset/empty), this raises `ConfigError` -- nothing configured must fail loudly, not
    silently do nothing.
    """
    profiles_db_path = resolve_profiles_db_path(profiles_db_raw, root)
    if profiles_db_path is None:
        return parse_reminder_teams(reminder_teams_raw)

    secret_teams = []
    if reminder_teams_raw is not None and reminder_teams_raw.strip():
        secret_teams = parse_reminder_teams(reminder_teams_raw)

    db_teams = load_teams_from_profiles_db(profiles_db_path)
    secret_team_ids = {team["team_id"] for team in secret_teams}
    merged = secret_teams + [team for team in db_teams if team["team_id"] not in secret_team_ids]

    if not merged:
        raise ConfigError(
            f"No reminder recipients configured: {REMINDER_TEAMS_ENV_VAR} is not set or empty, "
            f"and {REMINDER_PROFILES_DB_ENV_VAR} is set but matched no team with "
            "reminder_status='enabled' in profiles.db."
        )
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
    """
    stale = False
    try:
        bootstrap = fetch_bootstrap()
    except Exception:
        bootstrap = _load_json_or(root / "data" / "fpl-bootstrap-latest.json", None)
        stale = True
        if bootstrap is None:
            raise ConfigError(
                "Live bootstrap fetch failed and no cached data/fpl-bootstrap-latest.json exists."
            )
    try:
        fixtures = fetch_fixtures()
    except Exception:
        fixtures = _load_json_or(root / "data" / "fpl-fixtures-latest.json", [])
        stale = True
    return bootstrap, fixtures, stale


def load_official_transfers(root):
    """Read `official-transfers-latest.json`'s current confirmed-transfer list (issue #122).

    Mirrors `server.py`'s `_default_team_view_action` exactly (`resolve_artifact` -- the
    generation-aware helper, not a raw file read that could resolve the wrong generation -- with
    the same `{"transfers": []}`-shaped fallback) so this script's recommendations are computed
    with the same transfer-signal awareness the live dashboard's per-team lookup always has.
    A missing file is not treated as `stale` the way a failed live bootstrap fetch is -- this is a
    local artifact read, not a live-fetch failure, a different failure mode with a different
    likely cause (an unrefreshed/fresh checkout, not an upstream outage) -- so it degrades
    silently to `[]`, matching `server.py`'s own "should essentially never fire in practice,
    defense-in-depth only" treatment of the identical file.
    """
    transfers_path = resolve_artifact(root, "official-transfers-latest.json")
    if not transfers_path.exists():
        return []
    transfers_artifact = json.loads(transfers_path.read_text(encoding="utf-8"))
    return transfers_artifact.get("transfers", [])


def next_unfinished_event(bootstrap):
    """The next gameweek event dict (with `deadline_time`) the same way `_next_event_id` resolves it."""
    events = bootstrap.get("events", [])
    explicit = next((event for event in events if event.get("is_next")), None)
    if explicit is not None:
        return explicit
    unfinished = [event for event in events if event.get("id") and not event.get("finished")]
    if not unfinished:
        return None
    return min(unfinished, key=lambda event: event["id"])


def hours_until(deadline_iso, now):
    deadline = datetime.fromisoformat(deadline_iso.replace("Z", "+00:00"))
    return (deadline - now).total_seconds() / 3600


def in_send_window(deadline_iso, now, lead_hours):
    """True for exactly one hourly tick per gameweek: `(lead_hours - 1, lead_hours]` hours out."""
    hours_left = hours_until(deadline_iso, now)
    return (lead_hours - 1) < hours_left <= lead_hours


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

# Literal hex badge colors, reused as-is from the plan's own research and the mockup review --
# not re-derived here. `roll` (green): banking a transfer, or transferring at zero point cost.
# `hit` (red): a transfer that costs points. `info` (blue): a hold, or an informational header.
_BADGE_ROLL_BG, _BADGE_ROLL_FG = "#164b3a", "#94efcb"
_BADGE_HIT_BG, _BADGE_HIT_FG = "#573040", "#ffc1cb"
_BADGE_INFO_BG, _BADGE_INFO_FG = "#203b59", "#b9dcff"

_EMAIL_BG = "#0d1b2a"
_CARD_BG = "#13233a"
_CARD_BORDER = "#28405c"
_TEXT_PRIMARY = "#f4f7fb"
_TEXT_MUTED = "#9fb0c3"

# Pitch diagram layout. Row order top-to-bottom mirrors dashboard.js's weeklyPitch()/pitch()
# grouping exactly (`['FWD','MID','DEF','GKP'].map(...)` inside a `flex-direction: column`
# container renders FWD first/top, GKP last/bottom) -- see dashboard.js and the mockup PDF.
_PITCH_ROW_ORDER = ["FWD", "MID", "DEF", "GKP"]
_PITCH_WIDTH = 400
_PITCH_HEIGHT = 500
_PITCH_BOX_W = 86
_PITCH_BOX_H = 56


def _dashboard_base_url():
    """Resolve the base URL for the footer's "Manage reminder settings" link. See
    `DASHBOARD_BASE_URL_ENV_VAR`'s module-level comment for why this differs from server.py's
    live-request `Host`-header approach."""
    raw = os.environ.get(DASHBOARD_BASE_URL_ENV_VAR)
    if raw and raw.strip():
        return raw.strip().rstrip("/")
    return _DEFAULT_DASHBOARD_BASE_URL


def _esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def _badge_for_recommendation(recommendation):
    """Map an action + point_cost to (label, bg, fg) using the plan's literal hex badge palette.

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
        return "HOLD", _BADGE_INFO_BG, _BADGE_INFO_FG
    if action == "roll":
        return "ROLL", _BADGE_ROLL_BG, _BADGE_ROLL_FG
    if action in ("single_transfer", "double_transfer"):
        if point_cost > 0:
            return f"TRANSFER · −{point_cost}", _BADGE_HIT_BG, _BADGE_HIT_FG
        return "TRANSFER", _BADGE_ROLL_BG, _BADGE_ROLL_FG
    label = (action or "N/A").replace("_", " ").upper()
    return label, _BADGE_INFO_BG, _BADGE_INFO_FG


def _badge_html(label, bg, fg):
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0"><tr>'
        f'<td style="background:{bg};color:{fg};font-weight:bold;font-size:12px;'
        'padding:4px 10px;border-radius:4px;font-family:Arial,Helvetica,sans-serif;'
        f'letter-spacing:.3px">{_esc(label)}</td></tr></table>'
    )


def _profile_eyebrow(profile_id, label, default_profile_id):
    text = (label or profile_id or "").upper()
    if profile_id == default_profile_id:
        text = f"{text} · DEFAULT"
    return text


def _pitch_svg(starting_xi, captain_id):
    """Build the inline-<svg> starting-XI pitch diagram.

    Mirrors dashboard.js's weeklyPitch()/pitch() grouping logic (one row per position, players
    spread evenly left-to-right within a row) but emits literal SVG coordinates instead of
    flexbox rows, since none of the dashboard's actual pitch CSS (custom properties, flexbox,
    gradients, :before/:after pseudo-elements) is email-safe -- see
    plans/issue-83-reminder-html-email.md. The captain is shown as an outlined/bordered box
    rather than a filled one: an email-safe stand-in for the dashboard's box-shadow captain glow,
    which does not survive into email.
    """
    starting_xi = starting_xi or []
    row_height = _PITCH_HEIGHT / len(_PITCH_ROW_ORDER)
    boxes = []
    for row_index, position in enumerate(_PITCH_ROW_ORDER):
        players = [player for player in starting_xi if player.get("position_short") == position]
        if not players:
            continue
        row_center_y = row_height * row_index + row_height / 2
        cell_width = _PITCH_WIDTH / len(players)
        for player_index, player in enumerate(players):
            cx = cell_width * player_index + cell_width / 2
            x = cx - _PITCH_BOX_W / 2
            y = row_center_y - _PITCH_BOX_H / 2
            is_captain = player.get("id") == captain_id
            name = _esc(player.get("name"))
            if is_captain:
                name = f"{name} (C)"
                fill, stroke, stroke_width = "#0b3d24", "#94efcb", "2.5"
            else:
                fill, stroke, stroke_width = "#1f5c3f", "none", "0"
            club = _esc(player.get("club_short") or player.get("club"))
            boxes.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{_PITCH_BOX_W}" height="{_PITCH_BOX_H}" '
                f'rx="6" fill="{fill}" stroke="{stroke}" stroke-width="{stroke_width}"/>'
                f'<text x="{cx:.1f}" y="{y + 23:.1f}" text-anchor="middle" font-size="13" '
                f'font-weight="bold" fill="#ffffff" font-family="Arial,Helvetica,sans-serif">{name}</text>'
                f'<text x="{cx:.1f}" y="{y + 41:.1f}" text-anchor="middle" font-size="11" '
                f'fill="#c9e8d8" font-family="Arial,Helvetica,sans-serif">{club}</text>'
            )
    body = "".join(boxes)
    return (
        f'<svg viewBox="0 0 {_PITCH_WIDTH} {_PITCH_HEIGHT}" width="100%" height="auto" '
        'xmlns="http://www.w3.org/2000/svg" role="img" '
        'aria-label="Recommended starting XI, grouped by position, captain outlined">'
        f'<rect x="0" y="0" width="{_PITCH_WIDTH}" height="{_PITCH_HEIGHT}" fill="#0b3d24"/>'
        f'<line x1="0" y1="{_PITCH_HEIGHT / 2:.1f}" x2="{_PITCH_WIDTH}" y2="{_PITCH_HEIGHT / 2:.1f}" '
        'stroke="#1f5c3f" stroke-width="2"/>'
        f'<circle cx="{_PITCH_WIDTH / 2:.1f}" cy="{_PITCH_HEIGHT / 2:.1f}" r="45" fill="none" '
        'stroke="#1f5c3f" stroke-width="2"/>'
        f'{body}</svg>'
    )


def _pitch_section_html(starting_xi, captain_id):
    """The "RECOMMENDED STARTING XI" section: the real inline <svg> for every client except
    Outlook desktop, which gets an explanatory dashed-border placeholder instead of silence, via
    MSO conditional comments (`<!--[if mso]>` / `<!--[if !mso]><!-->`) -- the standard
    Outlook-targeting technique, since a raw <svg> tag alone only degrades to *nothing shown*,
    not to an explanation. See the plan doc's "Mockup review" section.
    """
    if not starting_xi:
        return ""
    svg = _pitch_svg(starting_xi, captain_id)
    placeholder = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        '<td style="border:1px dashed #3a4a5c;border-radius:6px;padding:36px 16px;'
        'text-align:center;font-family:Arial,Helvetica,sans-serif">'
        '<span style="color:#8aa0b8;font-size:12px;font-style:italic">'
        "(starting-XI diagram not shown in this client)</span></td></tr></table>"
    )
    return (
        '<tr><td style="padding:16px 16px 0 16px">'
        f'<div style="font-size:12px;font-weight:bold;color:{_TEXT_MUTED};letter-spacing:.4px;'
        'margin-bottom:10px;font-family:Arial,Helvetica,sans-serif">RECOMMENDED STARTING XI</div>'
        "<!--[if !mso]><!-->" + svg + "<!--<![endif]-->"
        "<!--[if mso]>" + placeholder + "<![endif]-->"
        "</td></tr>"
    )


def _transfer_row_html(out_player, in_player):
    out_name = _esc(out_player.get("name"))
    in_name = _esc(in_player.get("name"))
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#1a2c42;border-radius:4px;margin-top:10px"><tr>'
        f'<td style="padding:8px 10px;font-size:13px;color:{_BADGE_HIT_FG};'
        f'font-family:Arial,Helvetica,sans-serif">OUT: {out_name}</td>'
        f'<td style="padding:8px 10px;font-size:13px;color:{_TEXT_MUTED};text-align:center;'
        'font-family:Arial,Helvetica,sans-serif">&rarr;</td>'
        f'<td style="padding:8px 10px;font-size:13px;color:{_BADGE_ROLL_FG};text-align:right;'
        f'font-family:Arial,Helvetica,sans-serif">IN: {in_name}</td>'
        "</tr></table>"
    )


def _kv_row_html(label, value_html, color=None):
    value_color = color or _TEXT_PRIMARY
    return (
        "<tr>"
        f'<td style="font-size:13px;color:{_TEXT_MUTED};padding:4px 0;'
        f'border-top:1px solid {_CARD_BORDER};font-family:Arial,Helvetica,sans-serif">'
        + _esc(label) + "</td>"
        f'<td align="right" style="font-size:13px;font-weight:bold;color:{value_color};'
        f'padding:4px 0;border-top:1px solid {_CARD_BORDER};'
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
            f'<p style="margin:0;font-size:13px;color:{_TEXT_PRIMARY};'
            f'font-family:Arial,Helvetica,sans-serif;line-height:1.4">{_esc(rationale)}</p>'
        )
    return (
        '<tr><td style="padding:0 16px 14px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{_CARD_BG};border:1px solid {_CARD_BORDER};border-radius:8px">'
        '<tr><td style="padding:14px 16px">'
        f'<div style="font-size:11px;font-weight:bold;letter-spacing:.5px;color:{_TEXT_MUTED};'
        f'margin-bottom:8px;font-family:Arial,Helvetica,sans-serif">{_esc(eyebrow)}</div>'
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
    badge_label, badge_bg, badge_fg = _badge_for_recommendation(recommendation)
    badge_html = (
        '<div style="margin-bottom:8px">' + _badge_html(badge_label, badge_bg, badge_fg) + "</div>"
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
        f'<div style="background:{_CARD_BG};border:1px solid {_CARD_BORDER};'
        'border-radius:8px;padding:16px;font-size:13px;color:'
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
        f'<tr><td style="padding:20px 16px 24px;border-top:1px solid {_CARD_BORDER}">'
        f'<p style="font-size:11px;color:{_TEXT_MUTED};margin:0;'
        'font-family:Arial,Helvetica,sans-serif;line-height:1.5">'
        "You're receiving this because you opted into deadline reminders for "
        f'FPL Intelligence. <a href="{_esc(manage_url)}" style="color:{_TEXT_MUTED};'
        'text-decoration:underline">Manage reminder settings</a></p>'
        "</td></tr>"
    )


def _assemble_email_html(event_id, lead_hours, stale, extra_top_html, body_html):
    stale_html = ""
    if stale:
        stale_html = (
            '<tr><td style="padding:16px 16px 0 16px">'
            '<div style="background:#3a2f12;border:1px solid #6b5420;border-radius:6px;'
            'padding:10px 12px;font-size:12px;color:#f0d98c;'
            'font-family:Arial,Helvetica,sans-serif">'
            "NOTE: the live FPL data fetch failed this run; these recommendations are from the "
            "last cached refresh and may not reflect the latest prices, injuries, or news."
            "</div></td></tr>"
        )
    header_label = f"GAMEWEEK {event_id} · DEADLINE IN {lead_hours}H"
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>FPL Intelligence deadline reminder</title></head>"
        f'<body style="margin:0;padding:0;background:{_EMAIL_BG};'
        'font-family:Arial,Helvetica,sans-serif">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{_EMAIL_BG}"><tr><td align="center">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:600px">'
        '<tr><td style="padding:16px 16px 0 16px">'
        + _badge_html(header_label, _BADGE_INFO_BG, _BADGE_INFO_FG)
        + "</td></tr>"
        + stale_html
        + extra_top_html
        + body_html
        + _footer_html()
        + "</table></td></tr></table></body></html>"
    )


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



def run(teams, dry_run, smtp_config, root=ROOT, now=None):
    """Core run loop, factored out of `main` so tests can inject `now` and avoid argv/env parsing."""
    now = now or datetime.now(timezone.utc)
    bootstrap, fixtures, stale = load_bootstrap_and_fixtures(root)
    transfers = load_official_transfers(root)
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

    in_window_teams = [team for team in teams if team["lead_hours"] in in_window_lead_hours]
    generated_at = now.isoformat()
    decision_center = None
    sent_count = 0
    for team in in_window_teams:
        saved = profiles.load_profile(root / "data" / "profiles.db", team["team_id"])
        manager_view = compute_manager_view(
            bootstrap, fixtures, transfers, generated_at, team["team_id"],
            confirmed_free_transfers=saved["confirmed_free_transfers"] if saved else None,
            confirmed_free_transfers_event=saved["confirmed_free_transfers_event"] if saved else None,
            draft_squad_ids=saved["draft_squad"] if saved else None,
        )
        status = manager_view["weekly_decisions"].get("status")
        if status == "team_not_found":
            print(
                f"warning: team {team['team_id']} not found or the FPL API was unreachable, skipping",
                file=sys.stderr,
            )
            continue
        if status == "waiting_for_gw2" and decision_center is None:
            decision_center = build_gw_recommendations(
                bootstrap, fixtures, generated_at=generated_at, recent_transfers=transfers,
            )
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
            send_email(smtp_config, team["email"], subject, body, html_body)
        sent_count += 1

    if sent_count:
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

    try:
        teams = collect_teams(
            os.environ.get(REMINDER_TEAMS_ENV_VAR),
            os.environ.get(REMINDER_PROFILES_DB_ENV_VAR),
        )
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 1

    smtp_config = None
    if not args.dry_run:
        try:
            smtp_config = parse_smtp_config()
        except ConfigError as error:
            print(f"Configuration error: {error}", file=sys.stderr)
            return 1

    try:
        return run(teams, args.dry_run, smtp_config)
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
