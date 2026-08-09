"""Local-only HTTP service for the FPL dashboard and explicit refresh requests."""

from datetime import datetime, timezone
from hashlib import sha256
from http import cookies as http_cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlsplit
import zoneinfo

from . import profiles
from .dashboard import render_dashboard
from .generation import resolve_artifact
from .model_performance import build_team_model_performance
from .rate_limit import CooldownLimiter
from .refresh import RefreshAlreadyRunning, compute_manager_view
from .transfer_decisions import validate_draft_squad


_ALLOWED_RISK_PROFILES = {"conservative", "balanced", "aggressive"}
# Issue #78: a manager's stated season objective -- metadata only, does not drive
# `risk_profile` selection, model behavior, or any recommendation/copy (see
# plans/issue-78-manager-goal.md). Keys mirror the profile form's `#profile-goal` <option>s.
_ALLOWED_GOALS = {"top_10k", "top_50k", "top_100k", "beat_last_season", "just_for_fun"}
_ALLOWED_PROFILE_KEYS = {
    "team_id",
    "timezone",
    "confirmed_free_transfers",
    "confirmed_free_transfers_event",
    "risk_profile",
    "goal",
}
_TIMEZONE_SHAPE_RE = re.compile(r"^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+){0,2}$")
_PROFILE_VALIDATION_MESSAGE = "Invalid profile payload"
_TEAM_ID_REQUIRED_MESSAGE = "A team ID is required to save settings"
_TEAM_ID_RE = re.compile(r"^[0-9]{1,8}$")
_TEAM_LOOKUP_COOLDOWN_SECONDS = 15
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
    # Matches `profiles._DEFAULT_GOAL` (issue #78) -- re-literaled here the same way
    # "timezone"/"risk_profile" above already duplicate `profiles._DEFAULT_TIMEZONE`/
    # `profiles._DEFAULT_RISK_PROFILE` rather than reaching into another module's private
    # constant.
    "goal": "top_50k",
}


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
        raw_fixtures = json.loads(
            resolve_artifact(root, "fpl-fixtures-latest.json").read_text(encoding="utf-8")
        )
        transfers_artifact = json.loads(
            resolve_artifact(root, "official-transfers-latest.json").read_text(encoding="utf-8")
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

    Note (issue #78): this reader's output is spliced into `state["profile"]` on *both* the
    visitor's own cookie-resolved team path and the explicit `?team_id=` lookup-of-someone-else's
    -team path (see `_serve_dashboard` below) -- so `goal`, like the `risk_profile` already
    returned here, becomes visible to anyone who looks a team ID up. That's an intentional,
    considered choice, not an oversight: `goal` is a low-sensitivity, five-option self-declared
    target (comparable in sensitivity to `risk_profile`, which this same code path already
    exposes today), not personal contact information like `email` -- so no extra gating is added
    for it here.
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
            "goal": saved["goal"],
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

    # Issue #78: metadata-only stated season objective, validated the same way as
    # `risk_profile` above -- a fixed, closed option set, rejecting anything else.
    goal = payload.get("goal")
    if goal not in _ALLOWED_GOALS:
        raise ProfileValidationError(_PROFILE_VALIDATION_MESSAGE)
    cleaned["goal"] = goal

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
        goal=cleaned["goal"],
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
    refresh_action=None,
    profile_action=None,
    team_view_action=None,
    profile_read_action=None,
    draft_squad_action=None,
    lookup_opt_out_action=None,
    model_performance_action=None,
):
    """Create a localhost dashboard server with token-protected refresh and profile endpoints."""
    root = Path(root).resolve()
    if host != "127.0.0.1":
        raise ValueError("Dashboard server must bind only to 127.0.0.1")
    token = token or secrets.token_urlsafe(32)
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
    refresh_lock = threading.Lock()

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "FPLDashboard/1.0"

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

        def _has_trusted_host(self):
            return self.headers.get("Host", "") == f"127.0.0.1:{self.server.server_port}"

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
                dashboard = resolve_artifact(root, "dashboard.html")
                if not dashboard.exists():
                    self._json(404, {"status": "error", "message": "Dashboard has not been generated"})
                    return
                html = dashboard.read_text(encoding="utf-8").replace(
                    'content="__REFRESH_TOKEN__"', f'content="{token}"', 1
                )
                self._send_html(html)
                return
            # Compute this one team's view at request time and splice it into a copy of the
            # shared state, without touching the persisted dashboard-state.json/dashboard.html.
            if not lookup_limiter.allow(self.client_address[0]):
                self._json(429, {"status": "error", "message": "Too many team lookups. Try again shortly."})
                return
            state_path = resolve_artifact(root, "dashboard-state.json")
            if not state_path.exists():
                self._json(404, {"status": "error", "message": "Dashboard has not been generated"})
                return
            state = json.loads(state_path.read_text(encoding="utf-8"))
            # Issue #62: a manager can opt their team out of showing derived recommendations to
            # anyone who looks it up by ID. Checked only for the explicit query-param lookup path
            # (never the visitor's own cookie-driven view) and before `lookup_action` -- a local
            # `profiles.load_profile` read, not the live-FPL-API-hitting call it would otherwise
            # trigger, per issue #28's already-flagged unthrottled-lookup-cost risk.
            if is_explicit_lookup:
                saved_profile = profiles.load_profile(_profiles_db_path(root), team_id)
                if saved_profile and saved_profile.get("opted_out"):
                    state["lookup"] = {"active": True, "team_id": team_id, "status": "opted_out"}
                    html = render_dashboard(state).replace(
                        'content="__REFRESH_TOKEN__"', f'content="{token}"', 1
                    )
                    self._send_html(html)
                    return
            try:
                lookup_result = lookup_action(team_id)
                state["manager"] = lookup_result["manager"]
                decision_center = dict(state.get("decision_center") or {})
                decision_center["weekly_decisions"] = lookup_result["weekly_decisions"]
                state["decision_center"] = decision_center
                if is_explicit_lookup:
                    state["lookup"] = {"active": True, "team_id": team_id, "status": "ok"}
                visitor_profile = visitor_profile_action(team_id)
                state["profile"] = visitor_profile
                risk = visitor_profile.get("risk_profile")
                if risk in _ALLOWED_RISK_PROFILES:
                    if decision_center.get("profile_recommendations"):
                        decision_center["default_profile"] = risk
                    weekly = decision_center.get("weekly_decisions")
                    if isinstance(weekly, dict) and weekly.get("profiles"):
                        weekly["default_profile"] = risk
                # Issue #64: this team's team_performance/player_performance, computed fresh from
                # the shared model-performance.json at request time -- same splice pattern as
                # state["manager"]/state["profile"] above, not precomputed for every saved profile.
                model_performance = dict(state.get("model_performance") or {})
                model_performance.update(performance_action(team_id))
                state["model_performance"] = model_performance
            except Exception as error:
                print(f"Team lookup failed: {error!r}", file=sys.stderr)
                if is_explicit_lookup:
                    state["lookup"] = {"active": True, "team_id": team_id, "status": "error"}
            html = render_dashboard(state).replace(
                'content="__REFRESH_TOKEN__"', f'content="{token}"', 1
            )
            self._send_html(html)

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
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            self._json(404, {"status": "error", "message": "Not found"})

        def do_POST(self):
            if self._reject_untrusted_host():
                return
            origin = self.headers.get("Origin")
            expected_origin = f"http://127.0.0.1:{self.server.server_port}"
            if origin is not None and origin != expected_origin:
                self._json(403, {"status": "error", "message": "Untrusted Origin header"})
                return
            path = self.path.split("?", 1)[0]
            if path not in {
                "/api/refresh", "/api/profile", "/api/draft-squad", "/api/lookup-opt-out",
            }:
                self._json(404, {"status": "error", "message": "Not found"})
                return
            if not secrets.compare_digest(self.headers.get("X-Refresh-Token", ""), token):
                self._json(403, {"status": "error", "message": "Invalid refresh token"})
                return
            max_body = 1024 if path == "/api/refresh" else 4096
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
            elif path == "/api/profile":
                self._handle_profile(body)
            elif path == "/api/draft-squad":
                self._handle_draft_squad(body)
            else:
                self._handle_lookup_opt_out(body)

        def _handle_refresh(self):
            if not refresh_lock.acquire(blocking=False):
                self._json(409, {"status": "busy", "message": "A refresh is already running"})
                return
            try:
                result = action() or {}
                self._json(200, {"status": "ok", **result})
            except (BlockingIOError, RefreshAlreadyRunning):
                self._json(409, {"status": "busy", "message": "A refresh is already running"})
            except Exception as error:
                print(f"Dashboard refresh failed: {error!r}", file=sys.stderr)
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
                print(f"Profile update failed: {error!r}", file=sys.stderr)
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
                print(f"Draft squad update failed: {error!r}", file=sys.stderr)
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
                print(f"Lookup opt-out update failed: {error!r}", file=sys.stderr)
                self._json(500, {"status": "error", "message": "Lookup opt-out update failed"})

        def log_message(self, message, *args):
            print(f"[{self.log_date_time_string()}] {message % args}")

    server = ThreadingHTTPServer((host, port), DashboardHandler)
    server.refresh_token = token
    return server
