"""Local-only HTTP service for the FPL dashboard and explicit refresh requests."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import escape as html_escape
from http import cookies as http_cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import traceback
from urllib.parse import parse_qs, urlsplit
import zoneinfo

from . import profiles
from . import release_notes
from . import reminder_confirmation
from .dashboard import render_dashboard
from .fpl_data import save_json
from .generation import resolve_artifact
from .model_performance import archive_team_forecast, build_team_model_performance
from .rate_limit import CooldownLimiter
from .refresh import RefreshAlreadyRunning, compute_manager_view, project_refresh_lock
from .transfer_decisions import validate_draft_squad


_ALLOWED_RISK_PROFILES = {"conservative", "balanced", "aggressive"}
_ALLOWED_PROFILE_KEYS = {
    "team_id",
    "timezone",
    "confirmed_free_transfers",
    "confirmed_free_transfers_event",
    "risk_profile",
}
_TIMEZONE_SHAPE_RE = re.compile(r"^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+){0,2}$")
_PROFILE_VALIDATION_MESSAGE = "Invalid profile payload"
_TEAM_ID_REQUIRED_MESSAGE = "A team ID is required to save settings"
_TEAM_ID_RE = re.compile(r"^[0-9]{1,8}$")
# Issue #28: unlike every cooldown below (all keyed by source IP or team ID, protecting a
# resource fairly attributed to one visitor/team at a time), /api/refresh refreshes one *shared*
# generation used by everyone, and the cost being guarded against -- calling out to the real
# FPL/Premier League APIs -- doesn't shrink just because requests arrive from different source
# IPs. See `_REFRESH_COOLDOWN_KEY`'s comment at its point of use in `create_server` for why this
# limiter is keyed globally instead. 90 seconds: since issue #27 this endpoint is operator-only
# (gated by `X-Refresh-Token`, never shipped to the browser), so this cooldown isn't throttling
# routine public traffic -- there isn't any -- it's defense-in-depth against a leaked/misused
# token or an operator's own accidental rapid double-trigger. 90s is comfortably longer than any
# realistic accidental double-click/retry gap, while still short enough that a legitimate operator
# who genuinely needs to re-run a refresh isn't meaningfully inconvenienced.
_REFRESH_COOLDOWN_SECONDS = 90
# The single key every /api/refresh request shares, making its CooldownLimiter global instead of
# per-source -- see the comment at its point of use in `_handle_refresh` for the full reasoning.
_REFRESH_COOLDOWN_KEY = "refresh"
_TEAM_LOOKUP_COOLDOWN_SECONDS = 15
# Issue #105: same fallback `send_deadline_reminder.py`'s old `load_teams_from_profiles_db` used
# for `/api/reminder-teams`'s `lead_hours` field -- `reminder_lead_hours` is always set alongside
# `reminder_status='enabled'` by issue #79's write path, so this is defensive, not expected to
# trigger in practice.
_DEFAULT_REMINDER_LEAD_HOURS = 3
# Issue #102: same per-run bound `refresh.py`'s `_MANAGER_PICKS_TEAM_CAP` (issue #64) already
# established for "loop over every registered team from a scheduled trigger" -- reused here
# rather than inventing a second cap value for the same class of cost.
_REGISTERED_TEAMS_CAP = 25
_PROFILE_WRITE_COOLDOWN_SECONDS = 5
# Deliberately stricter than the ordinary profile-write cooldown above -- this endpoint is the
# one PIN-guessing surface in the app (issue #62), so a would-be attacker gets far fewer
# attempts per unit time from a single source than an ordinary profile save allows.
_LOOKUP_OPT_OUT_COOLDOWN_SECONDS = 30
_ALLOWED_LOOKUP_OPT_OUT_KEYS = {"team_id", "opted_out", "pin"}
# 6+ alphanumeric characters -- longer than a typical numeric PIN specifically because there's
# no email/account to fall back on for recovery or to raise the cost of guessing (issue #62's
# plan is explicit that this is proportionate, not strong crypto).
_LOOKUP_OPT_OUT_PIN_RE = re.compile(r"^[A-Za-z0-9]{6,24}$")
_LOOKUP_OPT_OUT_VALIDATION_MESSAGE = "Invalid opt-out payload"
_LOOKUP_OPT_OUT_PIN_MESSAGE = "Incorrect PIN"
_TEAM_COOKIE_NAME = "fpl_team_id"
_TEAM_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 300  # ~300 days, comfortably spans a season
_DEFAULT_VISITOR_PROFILE = {
    "timezone": "America/New_York",
    "confirmed_free_transfers": None,
    "confirmed_free_transfers_event": None,
    "risk_profile": "balanced",
    "draft_squad": None,
    # Issue #79: these three are never defaulted to a non-null placeholder -- None/null means
    # "no reminder ever requested", a real, distinct state (see
    # `profiles._row_to_dict`'s matching comment). `reminder_pending_email` is included for the
    # same reason `email` is: it's personal contact information, filtered out of
    # `state["profile"]` on an explicit lookup of someone else's team, same as `email` itself
    # (see `_serve_dashboard` below).
    "email": None,
    "reminder_status": None,
    "reminder_lead_hours": None,
    "reminder_pending_email": None,
}
_REMINDER_OPT_IN_COOLDOWN_SECONDS = 30
# Team-ID-keyed (not source-IP-keyed) and deliberately much longer than the per-source cooldown
# above -- this is the piece that bounds worst-case confirmation-email volume landing on one
# target address regardless of how many source IPs an attacker rotates through, which a
# per-source-only limiter can't cover on its own (see plans/issue-79-reminder-opt-in.md).
_REMINDER_CONFIRM_SEND_COOLDOWN_SECONDS = 600
# Defense-in-depth per-source cooldown on the confirm *link* itself. Token entropy (32
# url-safe bytes from secrets.token_urlsafe) already makes brute force infeasible on its own,
# but every other sensitive check in this codebase carries a cooldown regardless of theoretical
# strength (e.g. the refresh token isn't brute-forceable either, and still lives behind
# Host/Origin checks) -- same reasoning applied here.
_REMINDER_CONFIRM_COOLDOWN_SECONDS = 30
_REMINDER_CONFIRMATION_TTL_HOURS = 24
_ALLOWED_REMINDER_OPT_IN_ACTIONS = {"enable", "decline", "disable"}
_ALLOWED_REMINDER_LEAD_HOURS = {3, 12, 24}
_ALLOWED_REMINDER_OPT_IN_KEYS = {"team_id", "action", "email", "lead_hours"}
_REMINDER_OPT_IN_VALIDATION_MESSAGE = "Invalid reminder opt-in payload"
_REMINDER_EMAIL_MAX_LENGTH = 254  # RFC 5321's practical maximum total address length
# Issue #110: category is a fixed, closed option set, mirroring the profile form's `risk_profile`
# select -- matches the Contact Us form's `#contact-category` <option>s.
_ALLOWED_CONTACT_CATEGORIES = {"bug", "feature_request", "feedback", "other"}
_ALLOWED_CONTACT_KEYS = {"category", "message", "reply_to"}
# Generous for free-text feedback while comfortably fitting inside `do_POST`'s shared 4096-byte
# `max_body` cap even after JSON escaping/multi-byte UTF-8 overhead (unlike `_REMINDER_EMAIL_MAX_
# LENGTH` above, which bounds an address, this bounds a whole message body, so it stays well
# under 4096 rather than up against it).
_CONTACT_MESSAGE_MAX_LENGTH = 2000
_CONTACT_VALIDATION_MESSAGE = "Invalid contact payload"
# Issue #110: this is the app's one endpoint reachable by any visitor with nothing to identify
# them by (no team ID, no PIN, no account) and a free-text body -- the shape most attractive to
# automated spam. Stricter than the ordinary profile-write cooldown (`_PROFILE_WRITE_COOLDOWN_
# SECONDS`, 5s) for that reason, but shorter than `_LOOKUP_OPT_OUT_COOLDOWN_SECONDS` (30s, guards
# a PIN-guessing surface, a different threat model entirely) -- a genuine visitor submitting one
# piece of feedback is not meaningfully inconvenienced by a 30s per-source cooldown, while it
# still caps a single source's submission rate at a low, unautomatable-feeling pace.
_CONTACT_COOLDOWN_SECONDS = 30
# Issue #110's local-log durability backstop -- see `_append_contact_log`. Plain-text, gitignored
# (matches this repo's existing blanket `*.log` .gitignore rule), append-only, never served over
# HTTP by any route.
_CONTACT_LOG_FILENAME = "contact-submissions.log"
# Issue #28: bounds how long `ThreadingHTTPServer` lets one connection's thread block waiting on
# a *socket read* (the request line, headers, or body arriving) -- set as `DashboardHandler.timeout`
# below, the classic defense against a slow-loris connection that opens and then sends data very
# slowly or not at all, tying up a thread indefinitely. 20 seconds is comfortably longer than any
# legitimate client needs to finish sending a small request (this app's largest body cap is 4KB,
# `_handle_profile`/etc.'s `max_body`) even over a slow/lossy connection, while still bounding
# worst-case thread pileup to a low number of stalled connections at a time. This only bounds
# socket reads -- once a full request has already been read, it does not apply to how long
# `_handle_refresh`'s own processing (including its own separate subprocess `timeout=300` in
# `_default_refresh_action`) takes.
_CONNECTION_TIMEOUT_SECONDS = 20


def _coerce_team_id(raw):
    """Validate a raw team-ID string (from a query param or cookie), or None if invalid."""
    if raw is None or not _TEAM_ID_RE.match(raw):
        return None
    team_id = int(raw)
    if not (1 <= team_id <= 99_999_999):
        return None
    return team_id


def _parse_team_id(query_string):
    """Extract a valid `team_id` query parameter, or None if absent/malformed.

    Malformed input (not the expected shape) is treated the same as absent -- a mistyped URL
    falls back to the normal shared dashboard rather than surfacing a hard error, since this is
    a query param a person may hand-edit in the address bar.
    """
    values = parse_qs(query_string).get("team_id")
    if not values:
        return None
    return _coerce_team_id(values[0])


def _parse_team_id_cookie(cookie_header):
    """Extract a valid `fpl_team_id` cookie value, or None if absent/malformed."""
    if not cookie_header:
        return None
    parsed = http_cookies.SimpleCookie()
    try:
        parsed.load(cookie_header)
    except http_cookies.CookieError:
        return None
    morsel = parsed.get(_TEAM_COOKIE_NAME)
    if morsel is None:
        return None
    return _coerce_team_id(morsel.value)


def _team_cookie_header(team_id):
    """Build the Set-Cookie header value that remembers `team_id` for this browser.

    Plain (unsigned) on purpose -- issue #45's security model treats this as convenience, not a
    credential: a manager's FPL data is already public, so there's nothing this cookie needs to
    keep secret, only something worth remembering across visits. `Secure` is safe to set even for
    local http://127.0.0.1 testing -- browsers already treat loopback as a trustworthy origin.
    """
    return (
        f"{_TEAM_COOKIE_NAME}={team_id}; Max-Age={_TEAM_COOKIE_MAX_AGE_SECONDS}; "
        "Path=/; HttpOnly; Secure; SameSite=Lax"
    )


def _default_team_view_action(root):
    """Build the default per-request team-lookup action from the shared refresh's cached artifacts."""

    def action(team_id):
        bootstrap = json.loads(
            resolve_artifact(root, "fpl-bootstrap-latest.json").read_text(encoding="utf-8")
        )
        # Defense-in-depth (see `scripts/start_dashboard.py`'s `seed_missing_data_files`, the
        # primary fix): both files are meant to always exist -- either seeded on first boot from
        # `data-seed/` or git-tracked in a plain local checkout, then kept current by every
        # successful refresh -- so these fallbacks should essentially never fire in practice.
        # They exist only so a team lookup degrades gracefully rather than hard-failing if one is
        # ever transiently missing anyway (a corrupted volume, a manual `rm`, ...). Fallback
        # shapes match `refresh.py`'s own: `[]` mirrors `_load_current_json(root,
        # "fpl-fixtures-latest.json", [])` in `_refresh_project_unlocked`; `{"transfers": []}`
        # mirrors what `transfers_artifact.get("transfers", [])` below already treats a missing
        # key as.
        fixtures_path = resolve_artifact(root, "fpl-fixtures-latest.json")
        raw_fixtures = (
            json.loads(fixtures_path.read_text(encoding="utf-8")) if fixtures_path.exists() else []
        )
        transfers_path = resolve_artifact(root, "official-transfers-latest.json")
        transfers_artifact = (
            json.loads(transfers_path.read_text(encoding="utf-8"))
            if transfers_path.exists() else {"transfers": []}
        )
        saved = profiles.load_profile(_profiles_db_path(root), team_id)
        generated_at = datetime.now(timezone.utc).isoformat()
        return compute_manager_view(
            bootstrap,
            raw_fixtures,
            transfers_artifact.get("transfers", []),
            generated_at,
            team_id,
            confirmed_free_transfers=saved["confirmed_free_transfers"] if saved else None,
            confirmed_free_transfers_event=saved["confirmed_free_transfers_event"] if saved else None,
            draft_squad_ids=saved["draft_squad"] if saved else None,
        )

    return action


def _profiles_db_path(root):
    return Path(root) / "data" / "profiles.db"


def _contact_log_path(root):
    return Path(root) / "data" / _CONTACT_LOG_FILENAME


def _default_model_performance_action(root):
    """Build the default per-team model-performance reader, for splicing into a served page.

    Reads the shared, per-team-keyed `model-performance.json` (issue #64) and scores just the
    resolved team's slice at request time -- mirrors `_default_team_view_action`'s role for
    `state["manager"]`/weekly decisions.
    """

    def action(team_id):
        performance_path = resolve_artifact(root, "model-performance.json")
        store = (
            json.loads(performance_path.read_text(encoding="utf-8"))
            if performance_path.exists() else {}
        )
        return build_team_model_performance(store, team_id)

    return action


def _default_visitor_profile_action(root):
    """Build the default per-team saved-profile reader, for splicing into a served page.

    Issue #79: `email`/`reminder_status`/`reminder_lead_hours`/`reminder_pending_email` ARE
    personal contact information, unlike every other field returned here -- this function still
    always returns them (so the visitor's own-team view has everything it needs), but
    `_serve_dashboard` filters them back out of `state["profile"]` whenever the request is an
    explicit `?team_id=` lookup of someone else's team. Fixing that filtering here instead of at
    every call site would require every injected `profile_read_action` (tests, and any future
    caller) to independently know to leave them out; doing it once at the single splice site in
    `_serve_dashboard` is the fix the plan calls for.
    """

    def action(team_id):
        saved = profiles.load_profile(_profiles_db_path(root), team_id)
        if saved is None:
            return {"team_id": team_id, **_DEFAULT_VISITOR_PROFILE}
        return {
            "team_id": saved["team_id"],
            "timezone": saved["timezone"],
            "confirmed_free_transfers": saved["confirmed_free_transfers"],
            "confirmed_free_transfers_event": saved["confirmed_free_transfers_event"],
            "risk_profile": saved["risk_profile"],
            "draft_squad": saved["draft_squad"],
            "email": saved["email"],
            "reminder_status": saved["reminder_status"],
            "reminder_lead_hours": saved["reminder_lead_hours"],
            "reminder_pending_email": saved["reminder_pending_email"],
        }

    return action


class ProfileValidationError(Exception):
    """Raised when a submitted profile payload fails validation."""


def _validate_profile_payload(payload):
    """Validate and normalize a /api/profile request body.

    Returns a cleaned dict with exactly the six live manager keys, or
    raises ProfileValidationError with a fixed, input-free message.
    """
    if not isinstance(payload, dict):
        raise ProfileValidationError(_PROFILE_VALIDATION_MESSAGE)
    if not set(payload.keys()) <= _ALLOWED_PROFILE_KEYS:
        raise ProfileValidationError(_PROFILE_VALIDATION_MESSAGE)

    cleaned = {}

    team_id = payload.get("team_id")
    if team_id is None or team_id == "":
        cleaned["team_id"] = None
    else:
        if isinstance(team_id, bool):
            raise ProfileValidationError(_PROFILE_VALIDATION_MESSAGE)
        if isinstance(team_id, int):
            team_id_value = team_id
        elif isinstance(team_id, str) and team_id.isdigit():
            team_id_value = int(team_id)
        else:
            raise ProfileValidationError(_PROFILE_VALIDATION_MESSAGE)
        if not (1 <= team_id_value <= 99_999_999):
            raise ProfileValidationError(_PROFILE_VALIDATION_MESSAGE)
        cleaned["team_id"] = team_id_value

    timezone_name = payload.get("timezone")
    if not isinstance(timezone_name, str) or not timezone_name or len(timezone_name) > 64:
        raise ProfileValidationError(_PROFILE_VALIDATION_MESSAGE)
    if not _TIMEZONE_SHAPE_RE.match(timezone_name):
        raise ProfileValidationError(_PROFILE_VALIDATION_MESSAGE)
    if timezone_name not in zoneinfo.available_timezones():
        raise ProfileValidationError(_PROFILE_VALIDATION_MESSAGE)
    cleaned["timezone"] = timezone_name

    confirmed_free_transfers = payload.get("confirmed_free_transfers")
    if confirmed_free_transfers is None or confirmed_free_transfers == "":
        cleaned["confirmed_free_transfers"] = None
    else:
        if isinstance(confirmed_free_transfers, bool):
            raise ProfileValidationError(_PROFILE_VALIDATION_MESSAGE)
        if isinstance(confirmed_free_transfers, int):
            count_value = confirmed_free_transfers
        elif isinstance(confirmed_free_transfers, str) and confirmed_free_transfers.isdigit():
            count_value = int(confirmed_free_transfers)
        else:
            raise ProfileValidationError(_PROFILE_VALIDATION_MESSAGE)
        if not (0 <= count_value <= 5):
            raise ProfileValidationError(_PROFILE_VALIDATION_MESSAGE)
        cleaned["confirmed_free_transfers"] = count_value

    event = payload.get("confirmed_free_transfers_event")
    if cleaned["confirmed_free_transfers"] is None:
        if event is not None and event != "":
            raise ProfileValidationError(_PROFILE_VALIDATION_MESSAGE)
        cleaned["confirmed_free_transfers_event"] = None
    else:
        if event is None or event == "" or isinstance(event, bool):
            raise ProfileValidationError(_PROFILE_VALIDATION_MESSAGE)
        if isinstance(event, int):
            event_value = event
        elif isinstance(event, str) and event.isdigit():
            event_value = int(event)
        else:
            raise ProfileValidationError(_PROFILE_VALIDATION_MESSAGE)
        if not (1 <= event_value <= 38):
            raise ProfileValidationError(_PROFILE_VALIDATION_MESSAGE)
        cleaned["confirmed_free_transfers_event"] = event_value

    risk_profile = payload.get("risk_profile")
    if risk_profile not in _ALLOWED_RISK_PROFILES:
        raise ProfileValidationError(_PROFILE_VALIDATION_MESSAGE)
    cleaned["risk_profile"] = risk_profile

    return cleaned


def _default_profile_action(root, payload):
    """Validate and persist a per-team profile update to the SQLite store (issue #45).

    Deliberately separate from `config/user-profile.json`, which keeps its own, narrower role
    feeding `refresh.py`'s single-team forecast-accuracy history tracking (issue #64) -- this
    action never reads or writes that file.
    """
    cleaned = _validate_profile_payload(payload)
    if cleaned["team_id"] is None:
        # Unlike the old single-file model, there's no "profile" identity without a team ID to
        # key storage on -- clearing a team is no longer a supported save, just don't save.
        raise ProfileValidationError(_TEAM_ID_REQUIRED_MESSAGE)

    profiles.save_profile(
        _profiles_db_path(root),
        team_id=cleaned["team_id"],
        timezone=cleaned["timezone"],
        risk_profile=cleaned["risk_profile"],
        confirmed_free_transfers=cleaned["confirmed_free_transfers"],
        confirmed_free_transfers_event=cleaned["confirmed_free_transfers_event"],
        now=datetime.now(timezone.utc).isoformat(),
    )

    return cleaned


class DraftSquadValidationError(Exception):
    """Raised when a submitted draft-squad payload fails validation."""


_DRAFT_SQUAD_SIZE = 15
_DRAFT_SQUAD_VALIDATION_MESSAGE = "Invalid draft squad payload"


def _validate_draft_squad_shape(payload):
    """Validate the request shape of a /api/draft-squad payload -- not FPL legality.

    Returns `(team_id, player_ids)`, where `player_ids` is either a 15-element list of ints
    (a declared draft) or None (an explicit clear of a previously saved draft). FPL-specific
    legality (formation quotas, club limit, budget) is checked separately by
    `transfer_decisions.validate_draft_squad`, which needs the current bootstrap and is checked
    by the caller -- this function only validates the shape a client could plausibly send.
    """
    if not isinstance(payload, dict) or not set(payload.keys()) <= {"team_id", "player_ids"}:
        raise DraftSquadValidationError(_DRAFT_SQUAD_VALIDATION_MESSAGE)

    team_id = payload.get("team_id")
    if isinstance(team_id, bool):
        raise DraftSquadValidationError(_DRAFT_SQUAD_VALIDATION_MESSAGE)
    if isinstance(team_id, int):
        team_id_value = team_id
    elif isinstance(team_id, str) and team_id.isdigit():
        team_id_value = int(team_id)
    else:
        raise DraftSquadValidationError(_DRAFT_SQUAD_VALIDATION_MESSAGE)
    if not (1 <= team_id_value <= 99_999_999):
        raise DraftSquadValidationError(_DRAFT_SQUAD_VALIDATION_MESSAGE)

    player_ids = payload.get("player_ids")
    if player_ids is None:
        return team_id_value, None
    if not isinstance(player_ids, list) or len(player_ids) != _DRAFT_SQUAD_SIZE:
        raise DraftSquadValidationError(f"A draft squad needs exactly {_DRAFT_SQUAD_SIZE} players")
    cleaned_ids = []
    for value in player_ids:
        if isinstance(value, bool) or not isinstance(value, int):
            raise DraftSquadValidationError(_DRAFT_SQUAD_VALIDATION_MESSAGE)
        cleaned_ids.append(value)
    return team_id_value, cleaned_ids


def _default_draft_squad_action(root, payload):
    """Validate and persist a per-team draft-squad declaration to the SQLite store (issue #61).

    Shape validation happens first (cheap, no I/O); FPL-legality validation
    (`transfer_decisions.validate_draft_squad`) runs against the shared refresh's cached
    bootstrap next, so a malformed or illegal draft is rejected with a clear reason before
    anything is written.
    """
    team_id, player_ids = _validate_draft_squad_shape(payload)
    if player_ids is not None:
        bootstrap = json.loads(
            resolve_artifact(root, "fpl-bootstrap-latest.json").read_text(encoding="utf-8")
        )
        try:
            validate_draft_squad(bootstrap, player_ids)
        except ValueError as error:
            raise DraftSquadValidationError(str(error)) from error

    profiles.save_draft_squad(
        _profiles_db_path(root),
        team_id=team_id,
        draft_squad_ids=player_ids,
        now=datetime.now(timezone.utc).isoformat(),
    )

    return {"team_id": team_id, "draft_squad": player_ids}


class LookupOptOutValidationError(Exception):
    """Raised when a submitted /api/lookup-opt-out payload fails validation."""


class LookupOptOutPinError(Exception):
    """Raised when a submitted PIN doesn't match the one already claimed for a team ID."""


def _validate_lookup_opt_out_payload(payload):
    """Validate and normalize a /api/lookup-opt-out request body.

    Returns a cleaned dict with exactly `team_id`/`opted_out`/`pin`, or raises
    LookupOptOutValidationError with a fixed, input-free message -- same shape-only-error
    posture as `_validate_profile_payload`, and deliberately never distinguishes "team ID
    doesn't exist" from any other rejection reason at this stage.
    """
    if not isinstance(payload, dict):
        raise LookupOptOutValidationError(_LOOKUP_OPT_OUT_VALIDATION_MESSAGE)
    if set(payload.keys()) != _ALLOWED_LOOKUP_OPT_OUT_KEYS:
        raise LookupOptOutValidationError(_LOOKUP_OPT_OUT_VALIDATION_MESSAGE)

    team_id = payload.get("team_id")
    if isinstance(team_id, bool):
        raise LookupOptOutValidationError(_LOOKUP_OPT_OUT_VALIDATION_MESSAGE)
    if isinstance(team_id, int):
        team_id_value = team_id
    elif isinstance(team_id, str) and team_id.isdigit():
        team_id_value = int(team_id)
    else:
        raise LookupOptOutValidationError(_LOOKUP_OPT_OUT_VALIDATION_MESSAGE)
    if not (1 <= team_id_value <= 99_999_999):
        raise LookupOptOutValidationError(_LOOKUP_OPT_OUT_VALIDATION_MESSAGE)

    opted_out = payload.get("opted_out")
    if not isinstance(opted_out, bool):
        raise LookupOptOutValidationError(_LOOKUP_OPT_OUT_VALIDATION_MESSAGE)

    pin = payload.get("pin")
    if not isinstance(pin, str) or not _LOOKUP_OPT_OUT_PIN_RE.match(pin):
        raise LookupOptOutValidationError(_LOOKUP_OPT_OUT_VALIDATION_MESSAGE)

    return {"team_id": team_id_value, "opted_out": opted_out, "pin": pin}


def _hash_pin(pin):
    return sha256(pin.encode("utf-8")).hexdigest()


def _default_lookup_opt_out_action(root, payload):
    """Validate and apply a /api/lookup-opt-out request (issue #62).

    First-claim PIN semantics: no `pin_hash` yet stored for `team_id` (including when the
    team has no row at all) means any PIN meeting the shape rule claims it, in the same
    request that sets `opted_out`. Once a PIN is claimed, every later request -- whether
    turning opt-out on or back off -- must submit the same PIN, checked with
    `secrets.compare_digest` (same pattern already used for the refresh token) so timing
    can't leak a partial match. On mismatch, the rejection message is fixed and input-free:
    it never confirms or denies whether a PIN already exists for a given team ID, so this
    endpoint itself can't be used to probe which teams have opted out.
    """
    cleaned = _validate_lookup_opt_out_payload(payload)
    db_path = _profiles_db_path(root)
    existing_hash = profiles.load_pin_hash(db_path, cleaned["team_id"])
    submitted_hash = _hash_pin(cleaned["pin"])
    if existing_hash is None:
        stored_hash = submitted_hash
    elif secrets.compare_digest(existing_hash, submitted_hash):
        stored_hash = existing_hash
    else:
        raise LookupOptOutPinError(_LOOKUP_OPT_OUT_PIN_MESSAGE)

    profiles.set_lookup_opt_out(
        db_path,
        team_id=cleaned["team_id"],
        opted_out=cleaned["opted_out"],
        pin_hash=stored_hash,
        now=datetime.now(timezone.utc).isoformat(),
    )

    return {"team_id": cleaned["team_id"], "opted_out": cleaned["opted_out"]}


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


def _validate_reminder_opt_in_payload(payload):
    """Validate and normalize a /api/reminder-opt-in request body.

    Returns a cleaned dict with `team_id`/`action`, plus `email`/`lead_hours` when `action` is
    `"enable"`. Raises ReminderOptInValidationError with a fixed, input-free message otherwise --
    same shape-only-error posture as `_validate_profile_payload`/`_validate_lookup_opt_out_payload`.
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
            or len(email) > _REMINDER_EMAIL_MAX_LENGTH
        ):
            raise ReminderOptInValidationError(_REMINDER_OPT_IN_VALIDATION_MESSAGE)
        lead_hours = payload.get("lead_hours")
        if isinstance(lead_hours, bool) or lead_hours not in _ALLOWED_REMINDER_LEAD_HOURS:
            raise ReminderOptInValidationError(_REMINDER_OPT_IN_VALIDATION_MESSAGE)
        cleaned["email"] = email.strip()
        cleaned["lead_hours"] = lead_hours
    elif "email" in payload or "lead_hours" in payload:
        # "decline"/"disable" take no additional fields -- reject rather than silently ignore,
        # matching this file's general posture of never accepting extra keys unnoticed.
        raise ReminderOptInValidationError(_REMINDER_OPT_IN_VALIDATION_MESSAGE)

    return cleaned


def _default_reminder_email_action():
    """Build the default confirmation-email sender, thin wrapper over `reminder_confirmation`
    so `create_server` can inject a fake for tests without ever touching real SMTP/`smtplib`.
    """

    def action(email, confirm_url, lead_hours):
        reminder_confirmation.send_confirmation_email(email, confirm_url, lead_hours)

    return action


def _default_reminder_opt_in_action(root, send_email_action, confirm_send_limiter):
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
    (checked by the caller, `_handle_reminder_opt_in`) applies.
    """

    def action(payload, base_url):
        cleaned = _validate_reminder_opt_in_payload(payload)
        db_path = _profiles_db_path(root)
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
                token_hash=_hash_pin(token),
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


class ContactValidationError(Exception):
    """Raised when a submitted /api/contact payload fails validation."""


def _validate_contact_payload(payload):
    """Validate and normalize a /api/contact request body (issue #110).

    Returns a cleaned dict with exactly `category`/`message`/`reply_to`, or raises
    ContactValidationError with a fixed, input-free message -- same shape-only-error posture as
    `_validate_profile_payload`/`_validate_reminder_opt_in_payload`: every error path returns
    one fixed message, never reflecting any part of the submitted payload back to the caller.
    """
    if not isinstance(payload, dict):
        raise ContactValidationError(_CONTACT_VALIDATION_MESSAGE)
    if not set(payload.keys()) <= _ALLOWED_CONTACT_KEYS:
        raise ContactValidationError(_CONTACT_VALIDATION_MESSAGE)

    category = payload.get("category")
    if category not in _ALLOWED_CONTACT_CATEGORIES:
        raise ContactValidationError(_CONTACT_VALIDATION_MESSAGE)

    message = payload.get("message")
    if (
        not isinstance(message, str) or not message.strip()
        or len(message) > _CONTACT_MESSAGE_MAX_LENGTH
    ):
        raise ContactValidationError(_CONTACT_VALIDATION_MESSAGE)

    reply_to = payload.get("reply_to")
    if reply_to is None or reply_to == "":
        cleaned_reply_to = None
    else:
        # Same "presence of '@', bounded length" rigor as `_validate_reminder_opt_in_payload`'s
        # own `email` check above -- not attempting full RFC 5322 validation, and reusing the
        # same length cap since this is the same kind of field (a visitor-supplied address).
        if (
            not isinstance(reply_to, str) or "@" not in reply_to or not reply_to.strip()
            or len(reply_to) > _REMINDER_EMAIL_MAX_LENGTH
        ):
            raise ContactValidationError(_CONTACT_VALIDATION_MESSAGE)
        cleaned_reply_to = reply_to.strip()

    return {"category": category, "message": message.strip(), "reply_to": cleaned_reply_to}


def _append_contact_log(root, cleaned, now):
    """Append one JSON line to the local durability-backstop log (issue #110).

    Deliberately a plain `open(path, "a")` append, not `fpl_data.atomic_write_text` -- that
    helper does a whole-file atomic *replace* (write a temp file, fsync, `os.replace`), the right
    tool for a file with one current value (e.g. `dashboard-state.json`), but the wrong one here:
    applying it to a log meant to grow one line per submission over the life of a deployment
    would mean reading and rewriting the entire file on every single submission, an unbounded and
    ever-growing cost. A plain append avoids that, and POSIX guarantees a `write()` of a
    line-sized payload to a file opened in append mode (`O_APPEND`) is atomic against
    interleaving from other writers -- sufficient for "one JSON line per submission", which is
    all this backstop needs to be (per the plan doc: no query capability, no review UI, just a
    plain-text safety net an operator can `cat`/`grep` by hand).
    """
    path = _contact_log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {
            "timestamp": now,
            "category": cleaned["category"],
            "message": cleaned["message"],
            "reply_to": cleaned["reply_to"],
        },
        ensure_ascii=False,
    )
    with open(path, "a", encoding="utf-8") as log_file:
        log_file.write(line + "\n")


def _default_contact_email_action():
    """Build the default Contact Us notification-email sender, thin wrapper over
    `reminder_confirmation` so `create_server` can inject a fake for tests without ever touching
    real SMTP/`smtplib` -- same role as `_default_reminder_email_action` above.
    """

    def action(category, message, reply_to):
        reminder_confirmation.send_contact_email(category, message, reply_to)

    return action


def _default_contact_action(root, send_email_action):
    """Build the default /api/contact write action (issue #110).

    Order matters, and is the entire point of the local-log durability backstop (see the plan
    doc's "Decided" section): validate the payload, then *always* append it to the local log
    first, and only after that succeeds attempt the operator-notification email -- never the
    reverse, and the local-log write is never skipped or made conditional on the email send
    succeeding. This is the deliberate mirror image of `_default_reminder_opt_in_action`'s
    "enable" path above, which sends its email BEFORE writing to the DB (so a failed send never
    leaves a dangling, un-deliverable confirmation token); here, an email failure must never
    lose a visitor's already-submitted feedback, so the durable record is written first and the
    notification is best-effort on top of it.

    A `ReminderEmailError` from the email attempt is caught here, not re-raised: the visitor's
    submission was already captured in the local log by this point, so from the visitor's
    perspective this is still a successful submission -- surfacing a failure here would only
    invite a confusing, unnecessary resubmission. The failure is still logged to stderr (matching
    every other `except Exception as error: print(f"...{error!r}...", file=sys.stderr)` site in
    this file) so the operator can notice a broken SMTP configuration via Railway's logs.
    """

    def action(payload):
        cleaned = _validate_contact_payload(payload)
        now = datetime.now(timezone.utc).isoformat()
        _append_contact_log(root, cleaned, now)
        try:
            send_email_action(cleaned["category"], cleaned["message"], cleaned["reply_to"])
        except reminder_confirmation.ReminderEmailError as error:
            print(
                f"Contact notification email failed (submission was still logged): {error!r}",
                file=sys.stderr,
            )
        return {"status": "ok"}

    return action


def _render_reminder_confirm_page(ok, message):
    """A small, self-contained HTML confirmation page (issue #79) -- reached by clicking a link
    from an email client, not a fetch call, so unlike every other endpoint in this file it can't
    return JSON. No cookie/session context is assumed; the only affordance is a link back to `/`.
    """
    heading = "You're confirmed" if ok else "Couldn't confirm"
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>FPL Intelligence -- reminder confirmation</title>"
        "<style>body{font-family:system-ui,-apple-system,sans-serif;max-width:480px;"
        "margin:80px auto;padding:0 20px;color:#1a1a1a;line-height:1.5}"
        "a{color:#1a56db}</style></head><body>"
        f"<h1>{html_escape(heading)}</h1><p>{html_escape(message)}</p>"
        "<p><a href=\"/\">Back to FPL Intelligence</a></p></body></html>"
    )


def build_refresh_result(state):
    """Summarize a completed manual refresh for the browser UI."""
    health = state.get("source_health") or {}
    fallback = {
        "fpl": "ok",
        "transfers": "ok",
        "fixtures": "ok" if state.get("fixture_summary", {}).get("status") == "ready" else "not_active",
        "manager": "ok" if state.get("manager", {}).get("connection_status") in {"connected", "registered_preseason"} else "not_configured",
    }
    statuses = {
        source: (health.get(source) or {}).get("status", status)
        for source, status in fallback.items()
    }
    degraded_sources = sorted(
        source
        for source, details in health.items()
        if details.get("error")
    )
    return {
        "generated_at": state["generated_at"],
        "confirmed_movements": len(state.get("transfers", [])),
        "fpl_status": state["fpl"]["season_status"],
        "source_statuses": statuses,
        "degraded_sources": degraded_sources,
    }


def _default_refresh_action(root):
    script = root / "scripts" / "refresh_dashboard.py"
    if not script.exists():
        raise FileNotFoundError(f"Refresh script not found: {script}")
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if completed.returncode == 75:
        raise RefreshAlreadyRunning("A refresh is already running")
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(detail or "Dashboard refresh failed")
    state_path = resolve_artifact(root, "dashboard-state.json")
    if not state_path.exists():
        raise RuntimeError("Refresh completed without generating dashboard state")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    return build_refresh_result(state)


def create_server(
    root,
    host="127.0.0.1",
    port=8877,
    token=None,
    reminder_teams_token=None,
    allowed_origin=None,
    refresh_action=None,
    profile_action=None,
    team_view_action=None,
    profile_read_action=None,
    draft_squad_action=None,
    lookup_opt_out_action=None,
    model_performance_action=None,
    reminder_opt_in_action=None,
    reminder_email_action=None,
    refresh_limiter=None,
    contact_action=None,
    contact_email_action=None,
    contact_limiter=None,
):
    """Create a dashboard server with a token-protected /api/refresh and open, rate-limited
    per-team write endpoints (issue #45's model).

    `host`/`port` may be any bindable value -- issue #27 lifted the old 127.0.0.1-only
    restriction so this can run on a hosting platform (e.g. Railway, which injects `PORT` and
    expects a `0.0.0.0` bind). `allowed_origin`, when set, is the single source of truth for
    both the trusted `Host` header (its netloc) and the trusted `Origin` header (its full value)
    -- see `_has_trusted_host`/`do_POST` below. Left `None` (the default, used by every existing
    caller/test), both checks fall back to today's exact `127.0.0.1:{port}` behavior, byte-for-
    byte unchanged.

    `refresh_limiter`, when set, replaces the default global-cooldown `CooldownLimiter` gating
    `/api/refresh` (issue #28) -- exists so tests can inject one built with `rate_limit.
    CooldownLimiter`'s `clock` parameter, the same way every other dependency here is injectable,
    without needing to wait out a real 90-second cooldown.

    `contact_action`/`contact_email_action`/`contact_limiter` are the equivalent DI hooks for
    `/api/contact` (issue #110), mirroring `reminder_opt_in_action`/`reminder_email_action`
    above's roles for `/api/reminder-opt-in` -- `contact_limiter` in particular exists so tests
    can inject a fake-clock `CooldownLimiter` for `/api/contact`'s own cooldown, same reasoning
    as `refresh_limiter`.

    `reminder_teams_token` (issue #105) gates `/api/reminder-teams` -- a **separate** secret from
    `token`, not another use of the existing operator token. That endpoint returns every opted-in
    manager's email address in bulk, a strictly more sensitive shape of data than anything `token`
    already gates (triggering a refresh, or exempting one already-public per-team lookup from rate
    limiting) -- a leaked `token` must not also hand over the whole reminder roster. Defaults to a
    fresh random-per-process value, same pattern as `token` itself, so the endpoint is never
    reachable by anyone who wasn't handed the real configured value.
    """
    root = Path(root).resolve()
    token = token or secrets.token_urlsafe(32)
    reminder_teams_token = reminder_teams_token or secrets.token_urlsafe(32)
    action = refresh_action or (lambda: _default_refresh_action(root))
    profile_write_action = profile_action or (lambda payload: _default_profile_action(root, payload))
    lookup_action = team_view_action or _default_team_view_action(root)
    visitor_profile_action = profile_read_action or _default_visitor_profile_action(root)
    draft_squad_write_action = draft_squad_action or (lambda payload: _default_draft_squad_action(root, payload))
    lookup_opt_out_write_action = lookup_opt_out_action or (
        lambda payload: _default_lookup_opt_out_action(root, payload)
    )
    performance_action = model_performance_action or _default_model_performance_action(root)
    lookup_limiter = CooldownLimiter(cooldown_seconds=_TEAM_LOOKUP_COOLDOWN_SECONDS)
    profile_write_limiter = CooldownLimiter(cooldown_seconds=_PROFILE_WRITE_COOLDOWN_SECONDS)
    # A separate limiter instance (not the shared profile one) so saving a profile and declaring
    # a draft squad don't compete for the same cooldown window -- a manager plausibly does both
    # back-to-back while setting up before Gameweek 1.
    draft_squad_write_limiter = CooldownLimiter(cooldown_seconds=_PROFILE_WRITE_COOLDOWN_SECONDS)
    lookup_opt_out_limiter = CooldownLimiter(cooldown_seconds=_LOOKUP_OPT_OUT_COOLDOWN_SECONDS)
    # Issue #79: two independent limiters for the reminder opt-in surface -- one ordinary
    # per-source cooldown on the endpoint itself (same pattern as every other write endpoint
    # above), plus a second, team-ID-keyed one that gates only the "enable" action's SMTP send
    # step (see `_default_reminder_opt_in_action`). A third, per-source cooldown separately
    # guards the confirm-link GET endpoint below.
    reminder_opt_in_limiter = CooldownLimiter(cooldown_seconds=_REMINDER_OPT_IN_COOLDOWN_SECONDS)
    reminder_confirm_send_limiter = CooldownLimiter(
        cooldown_seconds=_REMINDER_CONFIRM_SEND_COOLDOWN_SECONDS
    )
    reminder_confirm_limiter = CooldownLimiter(cooldown_seconds=_REMINDER_CONFIRM_COOLDOWN_SECONDS)
    reminder_send_email_action = reminder_email_action or _default_reminder_email_action()
    reminder_opt_in_write_action = reminder_opt_in_action or _default_reminder_opt_in_action(
        root, reminder_send_email_action, reminder_confirm_send_limiter
    )
    refresh_cooldown_limiter = refresh_limiter or CooldownLimiter(
        cooldown_seconds=_REFRESH_COOLDOWN_SECONDS
    )
    # Issue #110: own per-source cooldown for the Contact Us endpoint -- same tier-2 write-safety
    # posture as every other open endpoint's limiter above, a separate instance so submitting
    # feedback doesn't compete with, e.g., a profile save's own cooldown window.
    contact_write_limiter = contact_limiter or CooldownLimiter(
        cooldown_seconds=_CONTACT_COOLDOWN_SECONDS
    )
    contact_send_email_action = contact_email_action or _default_contact_email_action()
    contact_write_action = contact_action or _default_contact_action(root, contact_send_email_action)
    refresh_lock = threading.Lock()

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "FPLDashboard/1.0"
        # Issue #28: bounds how long this connection's thread blocks waiting on a socket read
        # (StreamRequestHandler.setup(), inherited via BaseHTTPRequestHandler, calls
        # `self.connection.settimeout(self.timeout)` with this value) -- the defense against a
        # slow-loris connection that opens and then sends data very slowly or not at all. See
        # `_CONNECTION_TIMEOUT_SECONDS`'s comment above for why 20s.
        timeout = _CONNECTION_TIMEOUT_SECONDS

        def _json(self, status, payload, extra_headers=None):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _expected_origin(self):
            # Per-request, not cached at server-creation time: the default branch reads
            # `self.server.server_port`, the *actual* bound port -- important for tests, which
            # pass `port=0` for a dynamic OS-assigned port. `allowed_origin`, when set, carries
            # its own scheme and (real deployments almost always omit a port on HTTPS's default
            # 443) omits the port entirely -- so this is never built by substituting a hostname
            # into a hardcoded `http://{host}:{port}` shape.
            return allowed_origin or f"http://127.0.0.1:{self.server.server_port}"

        def _has_trusted_host(self):
            expected_netloc = urlsplit(self._expected_origin()).netloc
            return self.headers.get("Host", "") == expected_netloc

        def _reject_untrusted_host(self):
            if self._has_trusted_host():
                return False
            self._json(421, {"status": "error", "message": "Untrusted Host header"})
            return True

        def _send_html(self, html):
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _rate_limit_exempt(self):
            """Issue #125: a caller holding the operator token is exempt from the visitor-tuned
            per-IP `lookup_limiter` cooldown -- otherwise a trusted script looping over several
            teams in one run (e.g. the deadline reminder) would trip its own limiter on the very
            first repeat call from its own IP. Reuses the existing operator secret rather than
            adding a new one: it isn't gating the *data* here (already publicly reachable via
            `?team_id=` either way), only whether the visitor-facing throttle applies."""
            return secrets.compare_digest(self.headers.get("X-Refresh-Token", ""), token)

        def _team_lookup_opted_out(self, team_id):
            """Issue #62: True if this team has opted out of being looked up by ID. Shared by
            `_serve_dashboard`'s explicit `?team_id=` lookup and `/api/manager-view` -- both
            represent "looking up someone else's team," unlike a cookie-resolved own-team view,
            which never checks this."""
            saved_profile = profiles.load_profile(_profiles_db_path(root), team_id)
            return bool(saved_profile and saved_profile.get("opted_out"))

        def _resolve_team_lookup(self, team_id):
            """Issue #125: the exception-handled `lookup_action` call, shared by
            `_serve_dashboard`'s team_id-resolved HTML path and `/api/manager-view`'s JSON
            equivalent, so the two computations can never drift. Returns `(manager,
            weekly_decisions)` on success, or `(None, None)` on failure (already logged here)."""
            try:
                lookup_result = lookup_action(team_id)
                return lookup_result["manager"], lookup_result["weekly_decisions"]
            except Exception as error:
                print(f"Team lookup failed: {error!r}\n{traceback.format_exc()}", file=sys.stderr)
                return None, None

        def _handle_shared_state(self):
            """Issue #125: JSON equivalent of the no-team_id dashboard view -- the entire base
            `dashboard-state.json`, unfiltered. Not new exposure: byte-for-byte what a no-team_id
            visitor already gets embedded in the rendered page (fresh per request since #120), and
            the shared refresh's default `manager` state on the hosted deployment is always
            `{"status": "not_configured", ...}` (`refresh.py:193`) -- no per-visitor PII is ever
            baked into the shared state to begin with. Public, no rate limit: identical cost/
            exposure profile to the existing public `/`/`/dashboard.html` route."""
            state_path = resolve_artifact(root, "dashboard-state.json")
            if not state_path.exists():
                self._json(404, {"status": "error", "message": "Dashboard has not been generated"})
                return
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self._json(200, state)

        def _handle_manager_view(self, query_string):
            """Issue #125: JSON equivalent of `_serve_dashboard`'s explicit `?team_id=` lookup --
            same opt-out (#62) and rate-limiting (#46) rules, minus the HTML-only splices
            (`visitor_profile`/`model_performance`) that carry PII #79 already had to filter for
            the HTML path; this endpoint never returns those fields at all, so there's nothing to
            filter. Built so `send_deadline_reminder.py` (and any future GitHub-Actions-hosted
            script) can fetch exactly what `compute_manager_view` already computes server-side --
            including profile overrides read from Railway's real `profiles.db` -- instead of
            trying to read local files that don't exist wherever the script happens to run."""
            team_id = _parse_team_id(query_string)
            if team_id is None:
                self._json(400, {"status": "error", "message": "A valid team_id query parameter is required."})
                return
            if not self._rate_limit_exempt() and not lookup_limiter.allow(self.client_address[0]):
                self._json(429, {"status": "error", "message": "Too many team lookups. Try again shortly."})
                return
            if self._team_lookup_opted_out(team_id):
                self._json(200, {"status": "opted_out", "team_id": team_id})
                return
            manager, weekly_decisions = self._resolve_team_lookup(team_id)
            if manager is None:
                self._json(500, {"status": "error", "message": "Team lookup failed"})
                return
            self._json(
                200,
                {"status": "ok", "team_id": team_id, "manager": manager, "weekly_decisions": weekly_decisions},
            )

        def _handle_reminder_teams(self):
            """Issue #105: JSON roster of every team with a confirmed, live reminder opt-in
            (`reminder_status == 'enabled'`, non-empty `email`), read straight from `profiles.db`
            by the one process that already has legitimate access to it -- the server-side
            equivalent of `send_deadline_reminder.py`'s old `load_teams_from_profiles_db`, which
            could only ever work when run on Railway itself, never on a GitHub Actions runner
            with no shared filesystem (same root cause as #101/#122/#125).

            Unlike `/api/manager-view`, there is no safe unauthenticated response here at all --
            this returns real PII (every opted-in manager's email) in bulk, not one already-public
            team's lookup result -- so a missing/invalid token always 403s outright rather than
            falling through to a rate-limited public path. Gated by its own dedicated
            `reminder_teams_token`, deliberately not `token` (`/api/refresh`'s), so a leak of
            either secret compromises only what that secret actually gates."""
            if not secrets.compare_digest(
                self.headers.get("X-Reminder-Teams-Token", ""), reminder_teams_token
            ):
                self._json(403, {"status": "error", "message": "Invalid reminder-teams token"})
                return
            db_path = _profiles_db_path(root)
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

        def _handle_registered_teams(self):
            """GET /api/registered-teams (issue #102): every team_id with a saved profile,
            capped at `_REGISTERED_TEAMS_CAP` -- the read counterpart `scripts/
            archive_team_forecasts.py` needs to discover which teams to archive a forecast for,
            the same structural gap #105's `/api/reminder-teams` closed for the (much smaller)
            reminder-opted-in subset. Deliberately a separate, broader endpoint rather than
            widening `/api/reminder-teams`'s own meaning -- "every registered team" and "every
            team that opted into reminder emails" are different, independently-useful sets.

            Gated by the same `X-Refresh-Token` `/api/refresh` and `/api/archive-team-forecast`
            already require, not a dedicated token like `/api/reminder-teams` -- this returns
            bare team IDs only, no email/PII of any kind, so it carries the same sensitivity as
            those two operator-only, non-PII-exposing endpoints, not #105's bulk-PII case.
            """
            if not secrets.compare_digest(self.headers.get("X-Refresh-Token", ""), token):
                self._json(403, {"status": "error", "message": "Invalid refresh token"})
                return
            team_ids = profiles.list_team_ids(_profiles_db_path(root))[:_REGISTERED_TEAMS_CAP]
            self._json(200, {"status": "ok", "team_ids": team_ids})

        def _handle_archive_team_forecast(self, body):
            """POST /api/archive-team-forecast (issue #102): archive one team's real weekly
            decision at one deadline checkpoint into the shared model-performance.json.

            Gated on the same `X-Refresh-Token` `/api/refresh` already requires, not a dedicated
            token like #105's `/api/reminder-teams` -- unlike that endpoint, this one returns no
            PII at all (only player IDs and recommendation metadata), so it carries the same
            sensitivity as `/api/refresh` itself (an operator-only action that mutates shared
            server state), not a bulk-PII-exposure risk needing its own secret.

            Computes the team's live `weekly_decisions` via the exact same `_resolve_team_lookup`
            helper `/api/manager-view` uses, so this can never drift from what a visitor's own
            dashboard view or a GitHub-Actions-hosted script's `/api/manager-view` call would see.
            The archive write itself is guarded by `project_refresh_lock` -- the same cross-
            process file lock `/api/refresh`'s subprocess eventually acquires via
            `refresh_project` -- so a concurrent full refresh (which loads, mutates, and wholesale
            republishes `model-performance.json` as one of several artifacts) can never silently
            clobber this endpoint's incremental update, or vice versa.

            Token already checked by `do_POST` before dispatch, same as `/api/refresh`.
            """
            try:
                payload = json.loads(body.decode("utf-8")) if body else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"status": "error", "message": "Invalid archive-team-forecast payload"})
                return
            if not isinstance(payload, dict):
                self._json(400, {"status": "error", "message": "Invalid archive-team-forecast payload"})
                return
            team_id = payload.get("team_id")
            if isinstance(team_id, bool) or not isinstance(team_id, int) or not (1 <= team_id <= 99_999_999):
                self._json(400, {"status": "error", "message": "A valid team_id is required"})
                return
            lead_hours = payload.get("lead_hours")
            if lead_hours not in _ALLOWED_REMINDER_LEAD_HOURS:
                self._json(
                    400,
                    {"status": "error", "message": "lead_hours must be one of 3, 12, 24"},
                )
                return
            manager, weekly_decisions = self._resolve_team_lookup(team_id)
            if manager is None:
                self._json(500, {"status": "error", "message": "Team lookup failed"})
                return
            try:
                with project_refresh_lock(root):
                    store_path = resolve_artifact(root, "model-performance.json")
                    store = (
                        json.loads(store_path.read_text(encoding="utf-8")) if store_path.exists() else {}
                    )
                    before = json.dumps(store.get("team_forecasts", {}).get(str(team_id), {}), sort_keys=True)
                    archive_team_forecast(store, team_id, weekly_decisions, lead_hours)
                    after = json.dumps(store.get("team_forecasts", {}).get(str(team_id), {}), sort_keys=True)
                    archived = before != after
                    if archived:
                        save_json(store_path, store)
            except RefreshAlreadyRunning:
                self._json(409, {"status": "busy", "message": "A refresh is already running"})
                return
            self._json(200, {"status": "ok", "team_id": team_id, "archived": archived})

        def _handle_release_notes(self, body):
            """POST /api/release-notes (issue #143): publish one day's "What's New" entry.

            Operator-only, same as `/api/refresh`/`/api/archive-team-forecast` -- gated on the
            same `X-Refresh-Token` (checked by `do_POST` before dispatch), no per-source cooldown
            of its own, since only the daily generation job (`scripts/publish_release_notes.py`)
            and a human operator ever hold that token. Idempotent by date: re-publishing the same
            date overwrites that date's entry rather than duplicating it (`release_notes.
            upsert_entry`'s own docstring) -- safe for the daily job to retry.
            """
            try:
                payload = json.loads(body.decode("utf-8")) if body else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"status": "error", "message": "Invalid release-notes payload"})
                return
            try:
                stored = release_notes.upsert_entry(root, payload)
            except release_notes.ReleaseNotesValidationError as error:
                self._json(400, {"status": "error", "message": str(error)})
                return
            except Exception as error:
                print(f"Release-notes publish failed: {error!r}\n{traceback.format_exc()}", file=sys.stderr)
                self._json(500, {"status": "error", "message": "Release-notes publish failed"})
                return
            self._json(200, {"status": "ok", "date": stored["date"]})

        def _serve_dashboard(self, query_string):
            query_team_id = _parse_team_id(query_string)
            # A team_id query param is an explicit, one-off no-signup lookup (issue #46) --
            # someone else's team. A team_id from a saved cookie is "my own remembered team"
            # (issue #45): same per-request compute path, but never flagged as a one-off lookup,
            # since it's the visitor's own default view, not a look at someone else's team.
            is_explicit_lookup = query_team_id is not None
            team_id = query_team_id if query_team_id is not None else _parse_team_id_cookie(
                self.headers.get("Cookie")
            )
            if team_id is None:
                # Issue #120: render fresh from the persisted dashboard-state.json on every
                # request, exactly like the team_id-resolved path below, rather than serving a
                # static dashboard.html baked at the last /api/refresh. A code deploy alone (no
                # refresh) used to leave every no-team_id visitor on stale markup/CSS/JS -- this
                # closes that gap structurally instead of adding a staleness check.
                state_path = resolve_artifact(root, "dashboard-state.json")
                if not state_path.exists():
                    self._json(404, {"status": "error", "message": "Dashboard has not been generated"})
                    return
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["release_notes"] = release_notes.load_entries(root)
                self._send_html(render_dashboard(state))
                return
            # Compute this one team's view at request time and splice it into a copy of the
            # shared state, without touching the persisted dashboard-state.json.
            if not lookup_limiter.allow(self.client_address[0]):
                self._json(429, {"status": "error", "message": "Too many team lookups. Try again shortly."})
                return
            state_path = resolve_artifact(root, "dashboard-state.json")
            if not state_path.exists():
                self._json(404, {"status": "error", "message": "Dashboard has not been generated"})
                return
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["release_notes"] = release_notes.load_entries(root)
            # Issue #62: a manager can opt their team out of showing derived recommendations to
            # anyone who looks it up by ID. Checked only for the explicit query-param lookup path
            # (never the visitor's own cookie-driven view) and before `lookup_action` -- a local
            # `profiles.load_profile` read, not the live-FPL-API-hitting call it would otherwise
            # trigger, per issue #28's already-flagged unthrottled-lookup-cost risk. Issue #125:
            # shares `_team_lookup_opted_out` with `/api/manager-view`, which always applies this
            # check (an API caller is always "looking up a team," equivalent to an explicit lookup).
            if is_explicit_lookup and self._team_lookup_opted_out(team_id):
                state["lookup"] = {"active": True, "team_id": team_id, "status": "opted_out"}
                self._send_html(render_dashboard(state))
                return
            try:
                # Issue #125: shares `_resolve_team_lookup` with `/api/manager-view`'s JSON
                # equivalent, so the two computations can never drift.
                manager, weekly_decisions = self._resolve_team_lookup(team_id)
                if manager is None:
                    if is_explicit_lookup:
                        state["lookup"] = {"active": True, "team_id": team_id, "status": "error"}
                else:
                    state["manager"] = manager
                    decision_center = dict(state.get("decision_center") or {})
                    decision_center["weekly_decisions"] = weekly_decisions
                    state["decision_center"] = decision_center
                    if is_explicit_lookup:
                        state["lookup"] = {"active": True, "team_id": team_id, "status": "ok"}
                    visitor_profile = visitor_profile_action(team_id)
                    # Issue #79: email/reminder_status/reminder_lead_hours/reminder_pending_email
                    # are personal contact information, unlike every other field this splice
                    # carries (timezone, risk_profile, draft_squad) -- they must never be
                    # visible to an explicit ?team_id= lookup of someone else's team, only the
                    # visitor's own cookie-resolved team. Filtered here, at the single splice site,
                    # rather than in `_default_visitor_profile_action` (or any injected replacement
                    # of it) so the fix applies uniformly regardless of which reader produced
                    # `visitor_profile`. `/api/manager-view` never returns this field at all, so it
                    # has nothing to filter.
                    if is_explicit_lookup:
                        visitor_profile = {
                            key: value for key, value in visitor_profile.items()
                            if key not in {
                                "email", "reminder_status", "reminder_lead_hours",
                                "reminder_pending_email",
                            }
                        }
                    state["profile"] = visitor_profile
                    risk = visitor_profile.get("risk_profile")
                    if risk in _ALLOWED_RISK_PROFILES:
                        if decision_center.get("profile_recommendations"):
                            decision_center["default_profile"] = risk
                        weekly = decision_center.get("weekly_decisions")
                        if isinstance(weekly, dict) and weekly.get("profiles"):
                            weekly["default_profile"] = risk
                    # Issue #64: this team's team_performance/player_performance, computed fresh
                    # from the shared model-performance.json at request time -- same splice
                    # pattern as state["manager"]/state["profile"] above, not precomputed for
                    # every saved profile.
                    model_performance = dict(state.get("model_performance") or {})
                    model_performance.update(performance_action(team_id))
                    state["model_performance"] = model_performance
            except Exception as error:
                print(f"Team lookup failed: {error!r}\n{traceback.format_exc()}", file=sys.stderr)
                if is_explicit_lookup:
                    state["lookup"] = {"active": True, "team_id": team_id, "status": "error"}
            self._send_html(render_dashboard(state))

        def do_GET(self):
            if self._reject_untrusted_host():
                return
            split_path = urlsplit(self.path)
            path = split_path.path
            if path in {"/", "/dashboard.html"}:
                self._serve_dashboard(split_path.query)
                return
            if path == "/api/status":
                state_path = resolve_artifact(root, "dashboard-state.json")
                state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
                self._json(
                    200,
                    {
                        "status": "ok",
                        "refreshing": refresh_lock.locked(),
                        "generated_at": state.get("generated_at"),
                        "fpl_status": state.get("fpl", {}).get("season_status"),
                    },
                )
                return
            if path == "/api/reminder-confirm":
                self._handle_reminder_confirm(split_path.query)
                return
            if path == "/api/shared-state":
                self._handle_shared_state()
                return
            if path == "/api/manager-view":
                self._handle_manager_view(split_path.query)
                return
            if path == "/api/reminder-teams":
                self._handle_reminder_teams()
                return
            if path == "/api/registered-teams":
                self._handle_registered_teams()
                return
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            self._json(404, {"status": "error", "message": "Not found"})

        def do_POST(self):
            if self._reject_untrusted_host():
                return
            origin = self.headers.get("Origin")
            expected_origin = self._expected_origin()
            if origin is not None and origin != expected_origin:
                self._json(403, {"status": "error", "message": "Untrusted Origin header"})
                return
            path = self.path.split("?", 1)[0]
            if path not in {
                "/api/refresh", "/api/archive-team-forecast", "/api/profile", "/api/draft-squad",
                "/api/lookup-opt-out", "/api/reminder-opt-in", "/api/contact", "/api/release-notes",
            }:
                self._json(404, {"status": "error", "message": "Not found"})
                return
            # Issue #27: the shared bearer token gates operator-only actions never shipped to the
            # browser (see _serve_dashboard) -- /api/refresh, and (issue #102) /api/archive-team-
            # forecast, which mutates shared server state the same way /api/refresh does (no PII
            # exposure, unlike #105's /api/reminder-teams, which needed its own dedicated token).
            # The other five paths (issue #110 adds /api/contact to the original four) are open,
            # rate-limited per-visitor writes by design (issue #45) -- each already has its own
            # CooldownLimiter (and /api/lookup-opt-out its own separate PIN check), so re-gating
            # them behind one shared secret was redundant and, once public, actively broken (the
            # token was visible via view-source on every served page).
            if path in {"/api/refresh", "/api/archive-team-forecast", "/api/release-notes"}:
                if not secrets.compare_digest(self.headers.get("X-Refresh-Token", ""), token):
                    self._json(403, {"status": "error", "message": "Invalid refresh token"})
                    return
            max_body = (
                # Large enough for release_notes.py's own worst case: up to 20 changes, each up
                # to a ~700-char title+description, plus JSON escaping overhead.
                16384 if path == "/api/release-notes"
                else 1024 if path in {"/api/refresh", "/api/archive-team-forecast"}
                else 4096
            )
            try:
                content_length = int(self.headers.get("Content-Length", "0") or 0)
            except (TypeError, ValueError):
                self._json(400, {"status": "error", "message": "Invalid Content-Length"})
                return
            if content_length < 0:
                self._json(400, {"status": "error", "message": "Invalid Content-Length"})
                return
            if content_length > max_body:
                self._json(413, {"status": "error", "message": "Request body too large"})
                return
            body = self.rfile.read(content_length) if content_length else b""
            if path == "/api/refresh":
                self._handle_refresh()
            elif path == "/api/archive-team-forecast":
                self._handle_archive_team_forecast(body)
            elif path == "/api/profile":
                self._handle_profile(body)
            elif path == "/api/draft-squad":
                self._handle_draft_squad(body)
            elif path == "/api/lookup-opt-out":
                self._handle_lookup_opt_out(body)
            elif path == "/api/reminder-opt-in":
                self._handle_reminder_opt_in(body)
            elif path == "/api/release-notes":
                self._handle_release_notes(body)
            else:
                self._handle_contact(body)

        def _handle_refresh(self):
            # Issue #28: keyed on a single constant, deliberately *not* `self.client_address[0]`
            # like every other limiter in this file. Those are all keyed per-source because they
            # each guard a resource fairly attributed to one visitor/team at a time. This one
            # guards a *shared* resource -- one refresh generation used by everyone, backed by
            # real calls to the FPL/Premier League APIs -- so the risk being limited doesn't
            # shrink just because requests come from different source IPs. And since /api/refresh
            # is operator-only now (gated by `X-Refresh-Token`, issue #27, never shipped to the
            # browser), a per-IP-keyed cooldown here would be trivially bypassed by calling from a
            # second IP with the same (leaked, or legitimately shared) token -- defeating the
            # actual point of the limiter. A single constant key makes this a genuinely global
            # cooldown, regardless of source.
            if not refresh_cooldown_limiter.allow(_REFRESH_COOLDOWN_KEY):
                self._json(
                    429,
                    {"status": "error", "message": "Refresh requested too recently. Try again shortly."},
                )
                return
            if not refresh_lock.acquire(blocking=False):
                self._json(409, {"status": "busy", "message": "A refresh is already running"})
                return
            try:
                result = action() or {}
                self._json(200, {"status": "ok", **result})
            except (BlockingIOError, RefreshAlreadyRunning):
                self._json(409, {"status": "busy", "message": "A refresh is already running"})
            except Exception as error:
                print(f"Dashboard refresh failed: {error!r}\n{traceback.format_exc()}", file=sys.stderr)
                self._json(500, {"status": "error", "message": "Dashboard refresh failed"})
            finally:
                refresh_lock.release()

        def _handle_profile(self, body):
            # No longer gated on refresh_lock (issue #45): a per-team profile save writes to its
            # own SQLite store, unrelated to the shared refresh's own files -- blocking every
            # visitor's save on whether an unrelated shared refresh happens to be running would
            # directly undercut this issue's goal of letting every visitor independently save
            # their own settings. SQLite's own transaction handles write safety instead; a
            # per-source cooldown guards against automated abuse of the now-open write endpoint
            # (issue #45's security model, tier 2).
            if not profile_write_limiter.allow(self.client_address[0]):
                self._json(429, {"status": "error", "message": "Too many profile saves. Try again shortly."})
                return
            try:
                payload = json.loads(body.decode("utf-8")) if body else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"status": "error", "message": "Invalid profile payload"})
                return
            if not isinstance(payload, dict):
                self._json(400, {"status": "error", "message": "Invalid profile payload"})
                return
            try:
                cleaned = profile_write_action(payload)
                self._json(
                    200,
                    {"status": "ok", "profile": cleaned},
                    extra_headers={"Set-Cookie": _team_cookie_header(cleaned["team_id"])},
                )
            except ProfileValidationError as error:
                self._json(400, {"status": "error", "message": str(error)})
            except Exception as error:
                print(f"Profile update failed: {error!r}\n{traceback.format_exc()}", file=sys.stderr)
                self._json(500, {"status": "error", "message": "Profile update failed"})

        def _handle_draft_squad(self, body):
            # Same write-safety model as _handle_profile (issue #45's security model, tier 2):
            # SQLite's own transaction handles concurrent-write safety, a per-source cooldown
            # guards against automated abuse of the open write endpoint.
            if not draft_squad_write_limiter.allow(self.client_address[0]):
                self._json(429, {"status": "error", "message": "Too many draft squad saves. Try again shortly."})
                return
            try:
                payload = json.loads(body.decode("utf-8")) if body else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"status": "error", "message": "Invalid draft squad payload"})
                return
            if not isinstance(payload, dict):
                self._json(400, {"status": "error", "message": "Invalid draft squad payload"})
                return
            try:
                cleaned = draft_squad_write_action(payload)
                self._json(
                    200,
                    {"status": "ok", **cleaned},
                    extra_headers={"Set-Cookie": _team_cookie_header(cleaned["team_id"])},
                )
            except DraftSquadValidationError as error:
                self._json(400, {"status": "error", "message": str(error)})
            except Exception as error:
                print(f"Draft squad update failed: {error!r}\n{traceback.format_exc()}", file=sys.stderr)
                self._json(500, {"status": "error", "message": "Draft squad update failed"})

        def _handle_lookup_opt_out(self, body):
            # Rate-limited tighter than /api/profile's own write cooldown (issue #62) -- this
            # endpoint is the app's one PIN-guessing surface, so a source gets far fewer
            # attempts per unit time here than an ordinary profile save allows.
            if not lookup_opt_out_limiter.allow(self.client_address[0]):
                self._json(429, {"status": "error", "message": "Too many opt-out attempts. Try again shortly."})
                return
            try:
                payload = json.loads(body.decode("utf-8")) if body else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"status": "error", "message": _LOOKUP_OPT_OUT_VALIDATION_MESSAGE})
                return
            if not isinstance(payload, dict):
                self._json(400, {"status": "error", "message": _LOOKUP_OPT_OUT_VALIDATION_MESSAGE})
                return
            try:
                result = lookup_opt_out_write_action(payload)
                self._json(200, {"status": "ok", **result})
            except LookupOptOutPinError as error:
                self._json(403, {"status": "error", "message": str(error)})
            except LookupOptOutValidationError as error:
                self._json(400, {"status": "error", "message": str(error)})
            except Exception as error:
                print(f"Lookup opt-out update failed: {error!r}\n{traceback.format_exc()}", file=sys.stderr)
                self._json(500, {"status": "error", "message": "Lookup opt-out update failed"})

        def _handle_reminder_opt_in(self, body):
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

        def _handle_contact(self, body):
            # Open endpoint (issue #110, following issue #45's model, same as the other four
            # open write endpoints above) -- no X-Refresh-Token, own per-source CooldownLimiter
            # guards against automated abuse.
            if not contact_write_limiter.allow(self.client_address[0]):
                self._json(429, {"status": "error", "message": "Too many messages sent. Try again shortly."})
                return
            try:
                payload = json.loads(body.decode("utf-8")) if body else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"status": "error", "message": _CONTACT_VALIDATION_MESSAGE})
                return
            if not isinstance(payload, dict):
                self._json(400, {"status": "error", "message": _CONTACT_VALIDATION_MESSAGE})
                return
            try:
                contact_write_action(payload)
                # Deliberately always this same success response once validation and the local
                # log write succeed, regardless of whether the notification email itself
                # succeeded -- see `_default_contact_action`'s docstring for the full reasoning:
                # the visitor's submission is durably captured either way, so a failed email is
                # never surfaced here as a reason to resubmit.
                self._json(200, {"status": "ok", "message": "Thanks -- your message has been received."})
            except ContactValidationError as error:
                self._json(400, {"status": "error", "message": str(error)})
            except Exception as error:
                print(f"Contact submission failed: {error!r}\n{traceback.format_exc()}", file=sys.stderr)
                self._json(500, {"status": "error", "message": "Could not process your message. Try again shortly."})

        def _handle_reminder_confirm(self, query_string):
            # Defense-in-depth per-source cooldown (see _REMINDER_CONFIRM_COOLDOWN_SECONDS's
            # comment) -- an HTML response either way, matching this endpoint's own contract, not
            # the JSON 429 every other rate-limited endpoint in this file returns.
            if not reminder_confirm_limiter.allow(self.client_address[0]):
                self._send_html(
                    _render_reminder_confirm_page(False, "Too many attempts. Try again shortly.")
                )
                return
            params = parse_qs(query_string)
            team_id = _coerce_team_id((params.get("team_id") or [None])[0])
            raw_token = (params.get("token") or [None])[0]
            invalid_message = "This confirmation link is invalid or has already been used."
            if team_id is None or not raw_token:
                self._send_html(_render_reminder_confirm_page(False, invalid_message))
                return
            db_path = _profiles_db_path(root)
            saved = profiles.load_profile(db_path, team_id)
            stored_hash = saved.get("reminder_confirmation_token_hash") if saved else None
            if not stored_hash or not secrets.compare_digest(stored_hash, _hash_pin(raw_token)):
                self._send_html(_render_reminder_confirm_page(False, invalid_message))
                return
            expires_at = saved.get("reminder_confirmation_expires_at")
            try:
                expired = expires_at is None or datetime.fromisoformat(expires_at) < datetime.now(timezone.utc)
            except ValueError:
                expired = True
            if expired:
                self._send_html(
                    _render_reminder_confirm_page(
                        False, "This confirmation link has expired. Request a new one from your profile.",
                    )
                )
                return
            profiles.confirm_reminder(db_path, team_id=team_id, now=datetime.now(timezone.utc).isoformat())
            self._send_html(
                _render_reminder_confirm_page(True, "Deadline reminders are confirmed for this team.")
            )

        def log_message(self, message, *args):
            print(f"[{self.log_date_time_string()}] {message % args}")

        def log_error(self, format, *args):
            # Issue #28: this is the actual interception point for the `timeout` set above --
            # BaseHTTPRequestHandler.handle_one_request() already catches a per-connection
            # socket-read timeout internally (as a TimeoutError) and reports it by calling
            # exactly this hook with `args[0]` set to the exception instance, rather than letting
            # it propagate as an unhandled exception. That's expected, routine defensive behavior
            # against a slow/stalled client, not a real error -- issue #27's traceback-logging
            # fix (six `except Exception` sites now printing `traceback.format_exc()`) was about
            # making logs more useful, and spamming a full traceback per timed-out connection
            # under a slow-loris attempt would do the opposite. So it's downgraded here to one
            # clearly-labeled line via log_message (the override just above) instead of the
            # generic default message, which would otherwise read like a real per-request
            # failure. Anything else (a genuine error) still goes through log_message unchanged.
            if args and isinstance(args[0], TimeoutError):
                self.log_message("connection timed out (idle/slow client, %ss limit)", self.timeout)
                return
            self.log_message(format, *args)

    class _DashboardServer(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            # Issue #28: defense-in-depth companion to DashboardHandler.log_error above. Verified
            # against this stdlib's actual behavior: a per-connection socket-read timeout never
            # actually reaches this method in practice (handle_one_request's internal catch,
            # described in log_error's comment, already handles the ordinary case) -- but
            # ThreadingMixIn.process_request_thread routes *any* exception that does escape a
            # request thread to here, printing a full traceback by default (BaseServer.
            # handle_error). Should a timeout-flavored exception ever reach this level instead
            # (a stdlib behavior change, or a timeout while writing a response rather than
            # reading a request), it gets the same one-line quiet treatment rather than a
            # traceback dump; every other exception still gets the full traceback via the base
            # implementation, so a genuine unexpected bug stays fully visible.
            if isinstance(sys.exc_info()[1], TimeoutError):
                timestamp = datetime.now(timezone.utc).strftime("%d/%b/%Y %H:%M:%S")
                print(
                    f"[{timestamp}] connection timed out from {client_address} (server-level)",
                    file=sys.stderr,
                )
                return
            super().handle_error(request, client_address)

    server = _DashboardServer((host, port), DashboardHandler)
    server.refresh_token = token
    return server
