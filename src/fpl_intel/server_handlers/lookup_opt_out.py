"""/api/lookup-opt-out: let a manager hide their team from by-ID lookup (issue #62, split by #210)."""

from datetime import datetime, timezone
import json
import re
import secrets
import sys
import traceback

from .. import profiles
from .common import hash_pin, profiles_db_path

# Deliberately stricter than the ordinary profile-write cooldown (common.
# PROFILE_WRITE_COOLDOWN_SECONDS) -- this endpoint is the one PIN-guessing surface in the app
# (issue #62), so a would-be attacker gets far fewer attempts per unit time from a single source
# than an ordinary profile save allows.
LOOKUP_OPT_OUT_COOLDOWN_SECONDS = 30
_ALLOWED_LOOKUP_OPT_OUT_KEYS = {"team_id", "opted_out", "pin"}
# 6+ alphanumeric characters -- longer than a typical numeric PIN specifically because there's
# no email/account to fall back on for recovery or to raise the cost of guessing (issue #62's
# plan is explicit that this is proportionate, not strong crypto).
_LOOKUP_OPT_OUT_PIN_RE = re.compile(r"^[A-Za-z0-9]{6,24}$")
_LOOKUP_OPT_OUT_VALIDATION_MESSAGE = "Invalid opt-out payload"
_LOOKUP_OPT_OUT_PIN_MESSAGE = "Incorrect PIN"


class LookupOptOutValidationError(Exception):
    """Raised when a submitted /api/lookup-opt-out payload fails validation."""


class LookupOptOutPinError(Exception):
    """Raised when a submitted PIN doesn't match the one already claimed for a team ID."""


def validate_lookup_opt_out_payload(payload):
    """Validate and normalize a /api/lookup-opt-out request body.

    Returns a cleaned dict with exactly `team_id`/`opted_out`/`pin`, or raises
    LookupOptOutValidationError with a fixed, input-free message -- same shape-only-error
    posture as `profile.validate_profile_payload`, and deliberately never distinguishes "team ID
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


def default_lookup_opt_out_action(root, payload):
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
    cleaned = validate_lookup_opt_out_payload(payload)
    db_path = profiles_db_path(root)
    existing_hash = profiles.load_pin_hash(db_path, cleaned["team_id"])
    submitted_hash = hash_pin(cleaned["pin"])
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


def make_handle_lookup_opt_out(lookup_opt_out_write_action, lookup_opt_out_limiter):
    """Build the POST /api/lookup-opt-out handler, same DI-closure shape as `profile.
    make_handle_profile`."""

    def handle_lookup_opt_out(self, body):
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

    return handle_lookup_opt_out
