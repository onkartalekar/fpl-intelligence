"""/api/draft-squad: save a preseason 15-player draft to profiles.db (issue #61, split by #210)."""

from datetime import datetime, timezone
import json
import sys
import traceback

from .. import profiles
from ..generation import resolve_artifact
from ..transfer_decisions import validate_draft_squad
from .common import profiles_db_path, team_cookie_header

_DRAFT_SQUAD_SIZE = 15
_DRAFT_SQUAD_VALIDATION_MESSAGE = "Invalid draft squad payload"


class DraftSquadValidationError(Exception):
    """Raised when a submitted draft-squad payload fails validation."""


def validate_draft_squad_shape(payload):
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


def default_draft_squad_action(root, payload):
    """Validate and persist a per-team draft-squad declaration to the SQLite store (issue #61).

    Shape validation happens first (cheap, no I/O); FPL-legality validation
    (`transfer_decisions.validate_draft_squad`) runs against the shared refresh's cached
    bootstrap next, so a malformed or illegal draft is rejected with a clear reason before
    anything is written.
    """
    team_id, player_ids = validate_draft_squad_shape(payload)
    if player_ids is not None:
        bootstrap = json.loads(
            resolve_artifact(root, "fpl-bootstrap-latest.json").read_text(encoding="utf-8")
        )
        try:
            validate_draft_squad(bootstrap, player_ids)
        except ValueError as error:
            raise DraftSquadValidationError(str(error)) from error

    profiles.save_draft_squad(
        profiles_db_path(root),
        team_id=team_id,
        draft_squad_ids=player_ids,
        now=datetime.now(timezone.utc).isoformat(),
    )

    return {"team_id": team_id, "draft_squad": player_ids}


def make_handle_draft_squad(draft_squad_write_action, draft_squad_write_limiter):
    """Build the POST /api/draft-squad handler, same DI-closure shape as `profile.
    make_handle_profile`."""

    def handle_draft_squad(self, body):
        # Same write-safety model as /api/profile (issue #45's security model, tier 2):
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
                extra_headers={"Set-Cookie": team_cookie_header(cleaned["team_id"])},
            )
        except DraftSquadValidationError as error:
            self._json(400, {"status": "error", "message": str(error)})
        except Exception as error:
            print(f"Draft squad update failed: {error!r}\n{traceback.format_exc()}", file=sys.stderr)
            self._json(500, {"status": "error", "message": "Draft squad update failed"})

    return handle_draft_squad
