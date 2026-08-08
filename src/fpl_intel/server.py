"""Local-only HTTP service for the FPL dashboard and explicit refresh requests."""

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlsplit
import zoneinfo

from .dashboard import render_dashboard
from .generation import resolve_artifact
from .rate_limit import CooldownLimiter
from .refresh import RefreshAlreadyRunning, compute_manager_view


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
_TEAM_ID_RE = re.compile(r"^[0-9]{1,8}$")
_TEAM_LOOKUP_COOLDOWN_SECONDS = 15


def _parse_team_id(query_string):
    """Extract a valid `team_id` query parameter, or None if absent/malformed.

    Malformed input (not the expected shape) is treated the same as absent -- a mistyped URL
    falls back to the normal shared dashboard rather than surfacing a hard error, since this is
    a query param a person may hand-edit in the address bar.
    """
    values = parse_qs(query_string).get("team_id")
    if not values:
        return None
    raw = values[0]
    if not _TEAM_ID_RE.match(raw):
        return None
    team_id = int(raw)
    if not (1 <= team_id <= 99_999_999):
        return None
    return team_id


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
        generated_at = datetime.now(timezone.utc).isoformat()
        return compute_manager_view(
            bootstrap,
            raw_fixtures,
            transfers_artifact.get("transfers", []),
            generated_at,
            team_id,
        )

    return action


class ProfileValidationError(Exception):
    """Raised when a submitted profile payload fails validation."""


def _validate_profile_payload(payload):
    """Validate and normalize a /api/profile request body.

    Returns a cleaned dict with exactly the five live manager keys, or
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
    """Validate, merge, and atomically persist a profile update."""
    cleaned = _validate_profile_payload(payload)

    profile_path = root / "config" / "user-profile.json"
    if profile_path.exists():
        try:
            existing = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
    else:
        existing = {"manager": {}, "experience": {}}

    manager = existing.get("manager")
    if not isinstance(manager, dict):
        manager = {}
    manager["team_id"] = cleaned["team_id"]
    manager["timezone"] = cleaned["timezone"]
    manager["risk_profile"] = cleaned["risk_profile"]
    if cleaned["confirmed_free_transfers"] is None:
        manager.pop("confirmed_free_transfers", None)
        manager.pop("confirmed_free_transfers_event", None)
    else:
        manager["confirmed_free_transfers"] = cleaned["confirmed_free_transfers"]
        manager["confirmed_free_transfers_event"] = cleaned["confirmed_free_transfers_event"]
    existing["manager"] = manager

    profile_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = profile_path.with_name(profile_path.name + ".tmp")
    tmp_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, profile_path)

    return cleaned


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
):
    """Create a localhost dashboard server with token-protected refresh and profile endpoints."""
    root = Path(root).resolve()
    if host != "127.0.0.1":
        raise ValueError("Dashboard server must bind only to 127.0.0.1")
    token = token or secrets.token_urlsafe(32)
    action = refresh_action or (lambda: _default_refresh_action(root))
    profile_write_action = profile_action or (lambda payload: _default_profile_action(root, payload))
    lookup_action = team_view_action or _default_team_view_action(root)
    lookup_limiter = CooldownLimiter(cooldown_seconds=_TEAM_LOOKUP_COOLDOWN_SECONDS)
    refresh_lock = threading.Lock()

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "FPLDashboard/1.0"

        def _json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
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
            team_id = _parse_team_id(query_string)
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
            # A team_id query param means an unauthenticated, no-signup lookup (issue #46):
            # compute this one team's view at request time and splice it into a copy of the
            # shared state, without touching the persisted dashboard-state.json/dashboard.html.
            if not lookup_limiter.allow(self.client_address[0]):
                self._json(429, {"status": "error", "message": "Too many team lookups. Try again shortly."})
                return
            state_path = resolve_artifact(root, "dashboard-state.json")
            if not state_path.exists():
                self._json(404, {"status": "error", "message": "Dashboard has not been generated"})
                return
            state = json.loads(state_path.read_text(encoding="utf-8"))
            try:
                lookup_result = lookup_action(team_id)
                state["manager"] = lookup_result["manager"]
                decision_center = dict(state.get("decision_center") or {})
                decision_center["weekly_decisions"] = lookup_result["weekly_decisions"]
                state["decision_center"] = decision_center
                state["lookup"] = {"active": True, "team_id": team_id, "status": "ok"}
            except Exception as error:
                print(f"Team lookup failed: {error!r}", file=sys.stderr)
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
            if path not in {"/api/refresh", "/api/profile"}:
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
            else:
                self._handle_profile(body)

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
            try:
                payload = json.loads(body.decode("utf-8")) if body else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"status": "error", "message": "Invalid profile payload"})
                return
            if not isinstance(payload, dict):
                self._json(400, {"status": "error", "message": "Invalid profile payload"})
                return
            if not refresh_lock.acquire(blocking=False):
                self._json(409, {"status": "busy", "message": "A refresh is already running"})
                return
            try:
                cleaned = profile_write_action(payload)
                self._json(200, {"status": "ok", "profile": cleaned})
            except ProfileValidationError as error:
                self._json(400, {"status": "error", "message": str(error)})
            except (BlockingIOError, RefreshAlreadyRunning):
                self._json(409, {"status": "busy", "message": "A refresh is already running"})
            except Exception as error:
                print(f"Profile update failed: {error!r}", file=sys.stderr)
                self._json(500, {"status": "error", "message": "Profile update failed"})
            finally:
                refresh_lock.release()

        def log_message(self, message, *args):
            print(f"[{self.log_date_time_string()}] {message % args}")

    server = ThreadingHTTPServer((host, port), DashboardHandler)
    server.refresh_token = token
    return server
