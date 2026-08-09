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
import json
import os
from pathlib import Path
import smtplib
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fpl_intel import profiles
from fpl_intel.fpl_data import fetch_bootstrap, fetch_fixtures
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
    lines.append(f"Recommended action ({label} profile): {action}")
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
        lines.append("All risk profiles at a glance:")
        for row in profiles:
            row_recommendation = row.get("recommendation") or {}
            row_label = row.get("label") or row.get("id")
            row_action = str(row_recommendation.get("action") or "").replace("_", " ")
            row_captain = row_recommendation.get("captain") or {}
            row_points = row_recommendation.get("projected_event_points_including_captain")
            lines.append(
                f"  {row_label}: {row_action}  |  Captain: {row_captain.get('name', 'n/a')}  |  "
                f"Points: {row_points if row_points is not None else 'n/a'}  |  "
                f"Cost: {row_recommendation.get('point_cost', 0)}"
            )
    return lines


def compose_email(team, event_id, deadline_iso, hours_left, manager_view, decision_center, stale):
    """Compose one plain-text reminder email for a single team. Returns (subject, body)."""
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
    if status == "waiting_for_gw2":
        lines.extend(_compose_gw1_section(decision_center))
    elif status == "active":
        lines.extend(_compose_active_section(weekly))
    else:
        lines.append(f"Status: {status}")
        reason = weekly.get("reason")
        if reason:
            lines.append(reason)
    lines.append("")
    lines.append("-- FPL Intelligence automated deadline reminder (issue #55)")
    body = "\n".join(lines)
    subject = f"FPL reminder: GW{event_id} deadline in ~{team['lead_hours']}h"
    return subject, body


def send_email(smtp_config, to_email, subject, body):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp_config["user"]
    message["To"] = to_email
    message.set_content(body)
    with smtplib.SMTP(smtp_config["host"], smtp_config["port"], timeout=30) as smtp:
        smtp.starttls()
        smtp.login(smtp_config["user"], smtp_config["password"])
        smtp.send_message(message)


def run(teams, dry_run, smtp_config, root=ROOT, now=None):
    """Core run loop, factored out of `main` so tests can inject `now` and avoid argv/env parsing."""
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

    in_window_teams = [team for team in teams if team["lead_hours"] in in_window_lead_hours]
    generated_at = now.isoformat()
    decision_center = None
    sent_count = 0
    for team in in_window_teams:
        saved = profiles.load_profile(root / "data" / "profiles.db", team["team_id"])
        manager_view = compute_manager_view(
            bootstrap, fixtures, [], generated_at, team["team_id"],
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
                bootstrap, fixtures, generated_at=generated_at, recent_transfers=[],
            )
        hours_left = hours_until(deadline_iso, now)
        subject, body = compose_email(
            team, event_id, deadline_iso, hours_left, manager_view, decision_center, stale,
        )
        if dry_run:
            print("=" * 72)
            print(f"To: {team['email']}")
            print(f"Subject: {subject}")
            print()
            print(body)
        else:
            send_email(smtp_config, team["email"], subject, body)
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
