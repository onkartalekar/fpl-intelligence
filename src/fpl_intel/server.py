"""Local-only HTTP service for the FPL dashboard and explicit refresh requests."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import subprocess
import sys
import threading

from .generation import resolve_artifact
from .refresh import RefreshAlreadyRunning


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


def create_server(root, host="127.0.0.1", port=8877, token=None, refresh_action=None):
    """Create a localhost dashboard server with a token-protected refresh endpoint."""
    root = Path(root).resolve()
    if host != "127.0.0.1":
        raise ValueError("Dashboard server must bind only to 127.0.0.1")
    token = token or secrets.token_urlsafe(32)
    action = refresh_action or (lambda: _default_refresh_action(root))
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

        def do_GET(self):
            if self._reject_untrusted_host():
                return
            path = self.path.split("?", 1)[0]
            if path in {"/", "/dashboard.html"}:
                dashboard = resolve_artifact(root, "dashboard.html")
                if not dashboard.exists():
                    self._json(404, {"status": "error", "message": "Dashboard has not been generated"})
                    return
                html = dashboard.read_text(encoding="utf-8").replace(
                    'content="__REFRESH_TOKEN__"', f'content="{token}"', 1
                )
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)
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
            if path != "/api/refresh":
                self._json(404, {"status": "error", "message": "Not found"})
                return
            if not secrets.compare_digest(self.headers.get("X-Refresh-Token", ""), token):
                self._json(403, {"status": "error", "message": "Invalid refresh token"})
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0") or 0)
            except (TypeError, ValueError):
                self._json(400, {"status": "error", "message": "Invalid Content-Length"})
                return
            if content_length < 0:
                self._json(400, {"status": "error", "message": "Invalid Content-Length"})
                return
            if content_length > 1024:
                self._json(413, {"status": "error", "message": "Request body too large"})
                return
            if content_length:
                self.rfile.read(content_length)
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

        def log_message(self, message, *args):
            print(f"[{self.log_date_time_string()}] {message % args}")

    server = ThreadingHTTPServer((host, port), DashboardHandler)
    server.refresh_token = token
    return server
