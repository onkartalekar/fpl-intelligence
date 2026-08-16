"""/api/profile: save a per-team manager profile to profiles.db (issue #45, split out by #210)."""

from datetime import datetime, timezone
import json
import re
import sys
import traceback
import zoneinfo

from .. import profiles
from .common import ALLOWED_RISK_PROFILES, profiles_db_path, team_cookie_header

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


class ProfileValidationError(Exception):
    """Raised when a submitted profile payload fails validation."""


def validate_profile_payload(payload):
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
    if risk_profile not in ALLOWED_RISK_PROFILES:
        raise ProfileValidationError(_PROFILE_VALIDATION_MESSAGE)
    cleaned["risk_profile"] = risk_profile

    return cleaned


def default_profile_action(root, payload):
    """Validate and persist a per-team profile update to the SQLite store (issue #45).

    Deliberately separate from `config/user-profile.json`, which keeps its own, narrower role
    feeding `refresh.py`'s single-team forecast-accuracy history tracking (issue #64) -- this
    action never reads or writes that file.
    """
    cleaned = validate_profile_payload(payload)
    if cleaned["team_id"] is None:
        # Unlike the old single-file model, there's no "profile" identity without a team ID to
        # key storage on -- clearing a team is no longer a supported save, just don't save.
        raise ProfileValidationError(_TEAM_ID_REQUIRED_MESSAGE)

    profiles.save_profile(
        profiles_db_path(root),
        team_id=cleaned["team_id"],
        timezone=cleaned["timezone"],
        risk_profile=cleaned["risk_profile"],
        confirmed_free_transfers=cleaned["confirmed_free_transfers"],
        confirmed_free_transfers_event=cleaned["confirmed_free_transfers_event"],
        now=datetime.now(timezone.utc).isoformat(),
    )

    return cleaned


def make_handle_profile(profile_write_action, profile_write_limiter):
    """Build the POST /api/profile handler. `profile_write_action`/`profile_write_limiter` are
    `create_server`'s per-server-instance DI hooks, closed over here exactly as they were as
    local variables directly inside `DashboardHandler._handle_profile` before this split.
    """

    def handle_profile(self, body):
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
                extra_headers={"Set-Cookie": team_cookie_header(cleaned["team_id"])},
            )
        except ProfileValidationError as error:
            self._json(400, {"status": "error", "message": str(error)})
        except Exception as error:
            print(f"Profile update failed: {error!r}\n{traceback.format_exc()}", file=sys.stderr)
            self._json(500, {"status": "error", "message": "Profile update failed"})

    return handle_profile
