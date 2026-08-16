"""POST /api/refresh: operator-only trigger for the shared data pipeline (issue #27/#28, split
by #210). Named `refresh_endpoint`, not `refresh`, to avoid shadowing the domain module
`fpl_intel.refresh` this file imports from.
"""

import json
import subprocess
import sys
import traceback

from ..generation import resolve_artifact
from ..refresh import RefreshAlreadyRunning

# Issue #28: unlike every other cooldown in this codebase (all keyed by source IP or team ID,
# protecting a resource fairly attributed to one visitor/team at a time), /api/refresh refreshes
# one *shared* generation used by everyone, and the cost being guarded against -- calling out to
# the real FPL/Premier League APIs -- doesn't shrink just because requests arrive from different
# source IPs. See `_REFRESH_COOLDOWN_KEY`'s comment below for why this limiter is keyed globally
# instead. 90 seconds: since issue #27 this endpoint is operator-only (gated by
# `X-Refresh-Token`, never shipped to the browser), so this cooldown isn't throttling routine
# public traffic -- there isn't any -- it's defense-in-depth against a leaked/misused token or an
# operator's own accidental rapid double-trigger. 90s is comfortably longer than any realistic
# accidental double-click/retry gap, while still short enough that a legitimate operator who
# genuinely needs to re-run a refresh isn't meaningfully inconvenienced.
REFRESH_COOLDOWN_SECONDS = 90
# The single key every /api/refresh request shares, making its CooldownLimiter global instead of
# per-source -- see `make_handle_refresh`'s use of it for the full reasoning.
_REFRESH_COOLDOWN_KEY = "refresh"


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


def default_refresh_action(root):
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


def make_handle_refresh(action, refresh_cooldown_limiter, refresh_lock):
    """Build the POST /api/refresh handler. `refresh_lock` is the same `threading.Lock`
    `create_server` also exposes to `/api/status` (`refresh_lock.locked()`), so both stay in
    sync about whether a refresh is currently running.
    """

    def handle_refresh(self):
        # Issue #28: keyed on a single constant, deliberately *not* `self.client_address[0]`
        # like every other limiter in this codebase. Those are all keyed per-source because they
        # each guard a resource fairly attributed to one visitor/team at a time. This one guards
        # a *shared* resource -- one refresh generation used by everyone, backed by real calls to
        # the FPL/Premier League APIs -- so the risk being limited doesn't shrink just because
        # requests come from different source IPs. And since /api/refresh is operator-only now
        # (gated by `X-Refresh-Token`, issue #27, never shipped to the browser), a per-IP-keyed
        # cooldown here would be trivially bypassed by calling from a second IP with the same
        # (leaked, or legitimately shared) token -- defeating the actual point of the limiter. A
        # single constant key makes this a genuinely global cooldown, regardless of source.
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

    return handle_refresh
