"""Deadline-reminder feature area (issue #79/#105, split by #210):

- POST /api/reminder-opt-in -- enable/decline/disable a manager's deadline-reminder email.
- GET  /api/reminder-confirm -- the double-opt-in confirmation link an "enable" email carries.
- GET  /api/reminder-teams -- bulk roster of opted-in managers, for
  `scripts/send_deadline_reminder.py` (a GitHub-Actions-hosted script with no shared filesystem
  with Railway, issue #105).
"""

from datetime import datetime, timedelta, timezone
import json
import secrets
import sys
import traceback
from urllib.parse import parse_qs

from ..notifications import reminder_confirmation
from ..storage import profiles
from .common import (
    ALLOWED_REMINDER_LEAD_HOURS, REMINDER_EMAIL_MAX_LENGTH, coerce_team_id, hash_pin,
    profiles_db_path, render_reminder_confirm_page,
)

_ALLOWED_REMINDER_OPT_IN_ACTIONS = {"enable", "decline", "disable"}
_ALLOWED_REMINDER_OPT_IN_KEYS = {"team_id", "action", "email", "lead_hours"}
_REMINDER_OPT_IN_VALIDATION_MESSAGE = "Invalid reminder opt-in payload"
_REMINDER_CONFIRMATION_TTL_HOURS = 24
# Issue #105: same fallback `send_deadline_reminder.py`'s old `load_teams_from_profiles_db` used
# for `/api/reminder-teams`'s `lead_hours` field -- `reminder_lead_hours` is always set alongside
# `reminder_status='enabled'` by issue #79's write path, so this is defensive, not expected to
# trigger in practice.
_DEFAULT_REMINDER_LEAD_HOURS = 3

REMINDER_OPT_IN_COOLDOWN_SECONDS = 30
# Team-ID-keyed (not source-IP-keyed) and deliberately much longer than the per-source cooldown
# above -- this is the piece that bounds worst-case confirmation-email volume landing on one
# target address regardless of how many source IPs an attacker rotates through, which a
# per-source-only limiter can't cover on its own (see plans/issue-79-reminder-opt-in.md).
REMINDER_CONFIRM_SEND_COOLDOWN_SECONDS = 600
# Defense-in-depth per-source cooldown on the confirm *link* itself. Token entropy (32
# url-safe bytes from secrets.token_urlsafe) already makes brute force infeasible on its own,
# but every other sensitive check in this codebase carries a cooldown regardless of theoretical
# strength (e.g. the refresh token isn't brute-forceable either, and still lives behind
# Host/Origin checks) -- same reasoning applied here.
REMINDER_CONFIRM_COOLDOWN_SECONDS = 30


class ReminderOptInValidationError(Exception):
    """Raised when a submitted /api/reminder-opt-in payload fails validation."""


class ReminderOptInSendError(Exception):
    """Raised when an "enable" request's confirmation email could not be sent. The SMTP send is
    always attempted before any DB write (issue #79), so this error means nothing was persisted.
    """


class ReminderOptInCooldownError(Exception):
    """Raised when the team-ID-keyed SMTP-send cooldown blocks an "enable" request -- independent
    of, and in addition to, the ordinary per-source cooldown on the endpoint itself.
    """


def validate_reminder_opt_in_payload(payload):
    """Validate and normalize a /api/reminder-opt-in request body.

    Returns a cleaned dict with `team_id`/`action`, plus `email`/`lead_hours` when `action` is
    `"enable"`. Raises ReminderOptInValidationError with a fixed, input-free message otherwise --
    same shape-only-error posture as `profile.validate_profile_payload`/
    `lookup_opt_out.validate_lookup_opt_out_payload`.
    """
    if not isinstance(payload, dict):
        raise ReminderOptInValidationError(_REMINDER_OPT_IN_VALIDATION_MESSAGE)
    if not set(payload.keys()) <= _ALLOWED_REMINDER_OPT_IN_KEYS:
        raise ReminderOptInValidationError(_REMINDER_OPT_IN_VALIDATION_MESSAGE)

    team_id = payload.get("team_id")
    if isinstance(team_id, bool):
        raise ReminderOptInValidationError(_REMINDER_OPT_IN_VALIDATION_MESSAGE)
    if isinstance(team_id, int):
        team_id_value = team_id
    elif isinstance(team_id, str) and team_id.isdigit():
        team_id_value = int(team_id)
    else:
        raise ReminderOptInValidationError(_REMINDER_OPT_IN_VALIDATION_MESSAGE)
    if not (1 <= team_id_value <= 99_999_999):
        raise ReminderOptInValidationError(_REMINDER_OPT_IN_VALIDATION_MESSAGE)

    action = payload.get("action")
    if action not in _ALLOWED_REMINDER_OPT_IN_ACTIONS:
        raise ReminderOptInValidationError(_REMINDER_OPT_IN_VALIDATION_MESSAGE)

    cleaned = {"team_id": team_id_value, "action": action}

    if action == "enable":
        email = payload.get("email")
        # Simple presence-of-"@"/length check -- same rigor as
        # `send_deadline_reminder.py`'s `parse_reminder_teams` already applies today, per the
        # plan doc; this is not attempting full RFC 5322 validation.
        if (
            not isinstance(email, str) or "@" not in email or not email.strip()
            or len(email) > REMINDER_EMAIL_MAX_LENGTH
        ):
            raise ReminderOptInValidationError(_REMINDER_OPT_IN_VALIDATION_MESSAGE)
        lead_hours = payload.get("lead_hours")
        if isinstance(lead_hours, bool) or lead_hours not in ALLOWED_REMINDER_LEAD_HOURS:
            raise ReminderOptInValidationError(_REMINDER_OPT_IN_VALIDATION_MESSAGE)
        cleaned["email"] = email.strip()
        cleaned["lead_hours"] = lead_hours
    elif "email" in payload or "lead_hours" in payload:
        # "decline"/"disable" take no additional fields -- reject rather than silently ignore,
        # matching this file's general posture of never accepting extra keys unnoticed.
        raise ReminderOptInValidationError(_REMINDER_OPT_IN_VALIDATION_MESSAGE)

    return cleaned


def default_reminder_email_action():
    """Build the default confirmation-email sender, thin wrapper over `reminder_confirmation`
    so `create_server` can inject a fake for tests without ever touching real SMTP/`smtplib`.
    """

    def action(email, confirm_url, lead_hours):
        reminder_confirmation.send_confirmation_email(email, confirm_url, lead_hours)

    return action


def default_reminder_opt_in_action(root, send_email_action, confirm_send_limiter):
    """Build the default /api/reminder-opt-in write action (issue #79).

    `"enable"`: generates a random token, attempts the confirmation-email send FIRST via
    `send_email_action`, and only writes anything to the DB (`profiles.set_reminder_pending`) on
    send success -- never persists a pending-confirmation row for a token that was never actually
    emailed. Gated by `confirm_send_limiter`, keyed by `team_id` (not source IP) so rotating
    source IPs can't repeatedly re-trigger sends at the same target address.

    `"decline"`/`"disable"`: both write `reminder_status='declined'` via
    `profiles.set_reminder_decision` -- the same underlying write, `"disable"` additionally
    clearing the confirmed `email` (`clear_email=True`). Neither transition has a third-party
    victim (see the plan doc's per-transition risk table), so neither is gated by
    `confirm_send_limiter` -- only the per-source `CooldownLimiter` on the endpoint itself
    (checked by the caller, `make_handle_reminder_opt_in`) applies.
    """

    def action(payload, base_url):
        cleaned = validate_reminder_opt_in_payload(payload)
        db_path = profiles_db_path(root)
        now = datetime.now(timezone.utc).isoformat()

        if cleaned["action"] == "enable":
            if not confirm_send_limiter.allow(cleaned["team_id"]):
                raise ReminderOptInCooldownError(
                    "Too many reminder requests for this team. Try again later."
                )
            token = secrets.token_urlsafe(32)
            confirm_url = f"{base_url}/api/reminder-confirm?team_id={cleaned['team_id']}&token={token}"
            try:
                send_email_action(cleaned["email"], confirm_url, cleaned["lead_hours"])
            except reminder_confirmation.ReminderEmailError as error:
                raise ReminderOptInSendError(str(error)) from error
            expires_at = (
                datetime.now(timezone.utc) + timedelta(hours=_REMINDER_CONFIRMATION_TTL_HOURS)
            ).isoformat()
            profiles.set_reminder_pending(
                db_path,
                team_id=cleaned["team_id"],
                pending_email=cleaned["email"],
                lead_hours=cleaned["lead_hours"],
                token_hash=hash_pin(token),
                expires_at=expires_at,
                now=now,
            )
            return {"team_id": cleaned["team_id"], "reminder_status": "pending"}

        clear_email = cleaned["action"] == "disable"
        profiles.set_reminder_decision(
            db_path, team_id=cleaned["team_id"], status="declined", now=now,
            clear_email=clear_email,
        )
        return {"team_id": cleaned["team_id"], "reminder_status": "declined"}

    return action


def make_handle_reminder_opt_in(reminder_opt_in_write_action, reminder_opt_in_limiter):
    """Build the POST /api/reminder-opt-in handler, same DI-closure shape as `profile.
    make_handle_profile`."""

    def handle_reminder_opt_in(self, body):
        # Ordinary per-source cooldown on the endpoint itself (issue #79) -- deliberately
        # tighter than /api/profile's, matching /api/lookup-opt-out's own reasoning: this is
        # a third-party-affecting surface, not an ordinary preference save. The *second*,
        # team-ID-keyed cooldown that specifically bounds the "enable" action's SMTP send is
        # checked inside reminder_opt_in_write_action, not here.
        if not reminder_opt_in_limiter.allow(self.client_address[0]):
            self._json(429, {"status": "error", "message": "Too many reminder requests. Try again shortly."})
            return
        try:
            payload = json.loads(body.decode("utf-8")) if body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"status": "error", "message": _REMINDER_OPT_IN_VALIDATION_MESSAGE})
            return
        if not isinstance(payload, dict):
            self._json(400, {"status": "error", "message": _REMINDER_OPT_IN_VALIDATION_MESSAGE})
            return
        # Built from the already-validated trusted Host header (do_POST's
        # _reject_untrusted_host already ran before this handler is reached), never
        # hardcoded, so the confirmation link works unchanged before and after issue #27.
        base_url = f"http://{self.headers.get('Host')}"
        try:
            result = reminder_opt_in_write_action(payload, base_url)
            self._json(200, {"status": "ok", **result})
        except ReminderOptInValidationError as error:
            self._json(400, {"status": "error", "message": str(error)})
        except ReminderOptInCooldownError as error:
            self._json(429, {"status": "error", "message": str(error)})
        except ReminderOptInSendError as error:
            self._json(502, {"status": "error", "message": str(error)})
        except Exception as error:
            print(f"Reminder opt-in update failed: {error!r}\n{traceback.format_exc()}", file=sys.stderr)
            self._json(500, {"status": "error", "message": "Reminder opt-in update failed"})

    return handle_reminder_opt_in


def make_handle_reminder_confirm(root, reminder_confirm_limiter):
    """Build the GET /api/reminder-confirm handler -- reached by clicking a link from a
    confirmation email, so it renders `render_reminder_confirm_page` rather than returning JSON.
    """

    def handle_reminder_confirm(self, query_string):
        # Defense-in-depth per-source cooldown (see REMINDER_CONFIRM_COOLDOWN_SECONDS's
        # comment) -- an HTML response either way, matching this endpoint's own contract, not
        # the JSON 429 every other rate-limited endpoint returns.
        if not reminder_confirm_limiter.allow(self.client_address[0]):
            self._send_html(
                render_reminder_confirm_page(False, "Too many attempts. Try again shortly.")
            )
            return
        params = parse_qs(query_string)
        team_id = coerce_team_id((params.get("team_id") or [None])[0])
        raw_token = (params.get("token") or [None])[0]
        invalid_message = "This confirmation link is invalid or has already been used."
        if team_id is None or not raw_token:
            self._send_html(render_reminder_confirm_page(False, invalid_message))
            return
        db_path = profiles_db_path(root)
        saved = profiles.load_profile(db_path, team_id)
        stored_hash = saved.get("reminder_confirmation_token_hash") if saved else None
        if not stored_hash or not secrets.compare_digest(stored_hash, hash_pin(raw_token)):
            self._send_html(render_reminder_confirm_page(False, invalid_message))
            return
        expires_at = saved.get("reminder_confirmation_expires_at")
        try:
            expired = expires_at is None or datetime.fromisoformat(expires_at) < datetime.now(timezone.utc)
        except ValueError:
            expired = True
        if expired:
            self._send_html(
                render_reminder_confirm_page(
                    False, "This confirmation link has expired. Request a new one from your profile.",
                )
            )
            return
        profiles.confirm_reminder(db_path, team_id=team_id, now=datetime.now(timezone.utc).isoformat())
        self._send_html(
            render_reminder_confirm_page(True, "Deadline reminders are confirmed for this team.")
        )

    return handle_reminder_confirm


def make_handle_reminder_teams(root, reminder_teams_token):
    """Build the GET /api/reminder-teams handler (issue #105): JSON roster of every team with a
    confirmed, live reminder opt-in (`reminder_status == 'enabled'`, non-empty `email`), read
    straight from `profiles.db` by the one process that already has legitimate access to it --
    the server-side equivalent of `send_deadline_reminder.py`'s old `load_teams_from_profiles_db`,
    which could only ever work when run on Railway itself, never on a GitHub Actions runner with
    no shared filesystem (same root cause as #101/#122/#125).

    Unlike `/api/manager-view`, there is no safe unauthenticated response here at all -- this
    returns real PII (every opted-in manager's email) in bulk, not one already-public team's
    lookup result -- so a missing/invalid token always 403s outright rather than falling through
    to a rate-limited public path. Gated by its own dedicated `reminder_teams_token`, deliberately
    not `token` (`/api/refresh`'s), so a leak of either secret compromises only what that secret
    actually gates.
    """

    def handle_reminder_teams(self):
        if not secrets.compare_digest(
            self.headers.get("X-Reminder-Teams-Token", ""), reminder_teams_token
        ):
            self._json(403, {"status": "error", "message": "Invalid reminder-teams token"})
            return
        db_path = profiles_db_path(root)
        teams = []
        for team_id in profiles.list_team_ids(db_path):
            profile = profiles.load_profile(db_path, team_id)
            if profile is None or profile.get("reminder_status") != "enabled":
                continue
            email = profile.get("email")
            if not email:
                continue
            lead_hours = profile.get("reminder_lead_hours") or _DEFAULT_REMINDER_LEAD_HOURS
            teams.append({"team_id": team_id, "email": email, "lead_hours": lead_hours})
        self._json(200, {"status": "ok", "teams": teams})

    return handle_reminder_teams
