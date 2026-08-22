"""Local-only HTTP service for the FPL dashboard and explicit refresh requests.

Issue #210: this file used to hold every feature's routing, request validation, and auth/
rate-limit wiring directly -- 351 to 2,104 lines in 20 days, while the domain layer
(`transfer_decisions.py`, `profiles.py`, `release_notes.py`, ...) stayed cleanly split by concern.
It now holds only: the `DashboardHandler` class shell and its cross-cutting plumbing (`_json`/
`_send_html`, Host/Origin/cookie handling, `_resolve_team_lookup`/`_team_lookup_opted_out`, the
core `_serve_dashboard` route, and logging), `do_GET`/`do_POST`'s routing table, and
`create_server`'s dependency-injection wiring. Each feature's handler implementation, validation
exception classes, and default action now live in `server_handlers/*.py` -- see that package's
own docstring for the pattern. No behavior changed by this split; see issue #210.
"""

from datetime import datetime, timezone
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
import sys
import threading
import traceback
from urllib.parse import urlsplit

from .dashboard import APP_ICON_PNG, render_dashboard
from .generation import resolve_artifact
from .rate_limit import CooldownLimiter
from .storage import profiles
from .storage import release_notes
from .server_handlers import common
from .server_handlers import contact as contact_handlers
from .server_handlers import draft_squad as draft_squad_handlers
from .server_handlers import lookup_opt_out as lookup_opt_out_handlers
from .server_handlers import profile as profile_handlers
from .server_handlers import refresh_endpoint
from .server_handlers import release_notes_handlers
from .server_handlers import reminder as reminder_handlers
from .server_handlers import reminder_pitch
from .server_handlers import team_lookup
from .server_handlers.common import parse_team_id, parse_team_id_cookie
from .server_handlers.reminder import ReminderOptInCooldownError  # noqa: F401 (re-exported)
from .server_handlers.refresh_endpoint import build_refresh_result  # noqa: F401 (re-exported)
from .server_handlers.refresh_endpoint import default_refresh_action as _default_refresh_action
from .server_handlers.reminder import (
    default_reminder_opt_in_action as _default_reminder_opt_in_action,
)
from .server_handlers.team_lookup import default_team_view_action as _default_team_view_action
from .server_handlers.team_lookup import (
    default_visitor_profile_action as _default_visitor_profile_action,
)

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

# Issue #216: everything here is public dashboard data (the same content any visitor's browser
# already renders), so there's nothing to disallow -- this exists to give crawlers (Twitterbot,
# Applebot, search bots, ...) an explicit 200 "you may fetch anything" instead of a 404, which a
# well-behaved bot otherwise politely checks for and logs before every fetch anyway.
_ROBOTS_TXT = b"User-agent: *\nAllow: /\n"


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
    release_notes_subscribe_action=None,
    release_notes_subscribe_email_action=None,
    release_notes_notify_email_action=None,
    release_notes_subscribe_limiter=None,
    release_notes_confirm_limiter=None,
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

    `release_notes_subscribe_action`/`release_notes_subscribe_email_action`/
    `release_notes_notify_email_action`/`release_notes_subscribe_limiter`/
    `release_notes_confirm_limiter` are the equivalent DI hooks for issue #143's email
    subscription -- `release_notes_subscribe_action` for `/api/release-notes-subscribe`'s write,
    `release_notes_subscribe_email_action` for its confirmation-email send,
    `release_notes_notify_email_action` for the per-subscriber send when a new entry publishes,
    and the two limiters for its own cooldowns, mirroring `reminder_opt_in_action`/
    `reminder_email_action`'s roles above for the exact same reasons.

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

    action = refresh_action or (lambda: refresh_endpoint.default_refresh_action(root))
    profile_write_action = profile_action or (
        lambda payload: profile_handlers.default_profile_action(root, payload)
    )
    lookup_action = team_view_action or team_lookup.default_team_view_action(root)
    visitor_profile_action = profile_read_action or team_lookup.default_visitor_profile_action(root)
    draft_squad_write_action = draft_squad_action or (
        lambda payload: draft_squad_handlers.default_draft_squad_action(root, payload)
    )
    lookup_opt_out_write_action = lookup_opt_out_action or (
        lambda payload: lookup_opt_out_handlers.default_lookup_opt_out_action(root, payload)
    )
    performance_action = model_performance_action or team_lookup.default_model_performance_action(root)
    lookup_limiter = CooldownLimiter(cooldown_seconds=team_lookup.TEAM_LOOKUP_COOLDOWN_SECONDS)
    profile_write_limiter = CooldownLimiter(cooldown_seconds=common.PROFILE_WRITE_COOLDOWN_SECONDS)
    # A separate limiter instance (not the shared profile one) so saving a profile and declaring
    # a draft squad don't compete for the same cooldown window -- a manager plausibly does both
    # back-to-back while setting up before Gameweek 1.
    draft_squad_write_limiter = CooldownLimiter(cooldown_seconds=common.PROFILE_WRITE_COOLDOWN_SECONDS)
    lookup_opt_out_limiter = CooldownLimiter(
        cooldown_seconds=lookup_opt_out_handlers.LOOKUP_OPT_OUT_COOLDOWN_SECONDS
    )
    # Issue #79: two independent limiters for the reminder opt-in surface -- one ordinary
    # per-source cooldown on the endpoint itself (same pattern as every other write endpoint
    # above), plus a second, team-ID-keyed one that gates only the "enable" action's SMTP send
    # step (see `reminder.default_reminder_opt_in_action`). A third, per-source cooldown
    # separately guards the confirm-link GET endpoint below.
    reminder_opt_in_limiter = CooldownLimiter(
        cooldown_seconds=reminder_handlers.REMINDER_OPT_IN_COOLDOWN_SECONDS
    )
    reminder_confirm_send_limiter = CooldownLimiter(
        cooldown_seconds=reminder_handlers.REMINDER_CONFIRM_SEND_COOLDOWN_SECONDS
    )
    reminder_confirm_limiter = CooldownLimiter(
        cooldown_seconds=reminder_handlers.REMINDER_CONFIRM_COOLDOWN_SECONDS
    )
    reminder_send_email_action = reminder_email_action or reminder_handlers.default_reminder_email_action()
    reminder_opt_in_write_action = reminder_opt_in_action or reminder_handlers.default_reminder_opt_in_action(
        root, reminder_send_email_action, reminder_confirm_send_limiter
    )
    refresh_cooldown_limiter = refresh_limiter or CooldownLimiter(
        cooldown_seconds=refresh_endpoint.REFRESH_COOLDOWN_SECONDS
    )
    # Issue #110: own per-source cooldown for the Contact Us endpoint -- same tier-2 write-safety
    # posture as every other open endpoint's limiter above, a separate instance so submitting
    # feedback doesn't compete with, e.g., a profile save's own cooldown window.
    contact_write_limiter = contact_limiter or CooldownLimiter(
        cooldown_seconds=contact_handlers.CONTACT_COOLDOWN_SECONDS
    )
    contact_send_email_action = contact_email_action or contact_handlers.default_contact_email_action()
    contact_write_action = contact_action or contact_handlers.default_contact_action(
        root, contact_send_email_action
    )
    # Issue #143: email subscription for the What's New tab -- same two-limiter shape as
    # reminder opt-in just above (an ordinary per-source cooldown on the subscribe endpoint
    # itself, plus a separate one guarding the confirm/unsubscribe GET links against brute-force).
    # Each local below reuses its parameter's own name (self-shadowing) so the handler factories
    # further down close over the *resolved* value, not the still-`None` parameter.
    release_notes_subscribe_limiter = release_notes_subscribe_limiter or CooldownLimiter(
        cooldown_seconds=release_notes_handlers.RELEASE_NOTES_SUBSCRIBE_COOLDOWN_SECONDS
    )
    release_notes_confirm_limiter = release_notes_confirm_limiter or CooldownLimiter(
        cooldown_seconds=release_notes_handlers.RELEASE_NOTES_CONFIRM_COOLDOWN_SECONDS
    )
    release_notes_subscribe_send_email_action = (
        release_notes_subscribe_email_action
        or release_notes_handlers.default_release_notes_subscribe_email_action()
    )
    release_notes_subscribe_write_action = (
        release_notes_subscribe_action
        or release_notes_handlers.default_release_notes_subscribe_action(
            root, release_notes_subscribe_send_email_action,
        )
    )
    release_notes_notify_email_action = (
        release_notes_notify_email_action
        or release_notes_handlers.default_release_notes_notify_email_action()
    )
    refresh_lock = threading.Lock()

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "FPLDashboard/1.0"
        # Issue #28: bounds how long this connection's thread blocks waiting on a socket read
        # (StreamRequestHandler.setup(), inherited via BaseHTTPRequestHandler, calls
        # `self.connection.settimeout(self.timeout)` with this value) -- the defense against a
        # slow-loris connection that opens and then sends data very slowly or not at all. See
        # `_CONNECTION_TIMEOUT_SECONDS`'s comment above for why 20s.
        timeout = _CONNECTION_TIMEOUT_SECONDS

        def _accepts_gzip(self):
            """Issue #209: True if the request's Accept-Encoding lists gzip as one of its
            (possibly several, possibly q-valued) tokens, e.g. "gzip, deflate" or
            "gzip;q=1.0, identity;q=0.5". Only the token name before any ";q=..." matters here --
            this server only ever chooses between gzip and sending the body as-is, so a plain
            membership check is enough; it doesn't need to honor relative q-value weighting."""
            accept_encoding = self.headers.get("Accept-Encoding", "")
            tokens = (token.split(";", 1)[0].strip() for token in accept_encoding.split(","))
            return "gzip" in tokens

        def _compress_if_accepted(self, body):
            """Issue #209: gzip `body` when the client's Accept-Encoding says it can decode gzip,
            returning (possibly-compressed body, Content-Encoding value or None). Shared by
            `_json` and `_send_html` -- the two, and only, places a response body is written --
            so every response funnels through the same compress-or-not decision. Doesn't change
            whether a response is cacheable (`Cache-Control: no-store` from issue #120 is
            untouched); only how many bytes cross the wire for the same content."""
            if not self._accepts_gzip():
                return body, None
            return gzip.compress(body), "gzip"

        def _json(self, status, payload, extra_headers=None):
            body = json.dumps(payload).encode("utf-8")
            body, content_encoding = self._compress_if_accepted(body)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            if content_encoding:
                self.send_header("Content-Encoding", content_encoding)
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            # HEAD gets every header a GET would (including the real, post-compression
            # Content-Length -- per HTTP semantics, describing what the body *would* have been),
            # just never the body itself.
            if not getattr(self, "_head_request", False):
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
            body, content_encoding = self._compress_if_accepted(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if content_encoding:
                self.send_header("Content-Encoding", content_encoding)
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            # See _json's matching comment -- same HEAD-vs-GET body distinction.
            if not getattr(self, "_head_request", False):
                self.wfile.write(body)

        def _send_static(self, body, content_type):
            """Issue #216: originally the icon and robots.txt responses -- fixed bytes, chosen
            once at process start (APP_ICON_PNG at import time, _ROBOTS_TXT as a module constant),
            never per-request-generated like _json/_send_html's bodies. Skips _compress_if_accepted
            (a PNG is already compressed and gzipping it would only add overhead; robots.txt is
            a couple dozen bytes, too small for gzip's own framing to pay for itself) and allows
            caching (unlike every other response here, which sets Cache-Control: no-store) since
            neither ever changes within a running process -- a day is long enough to cut repeat-
            crawler traffic without risking a stale icon surviving a deploy that changes it.

            Issue #240: also reused for `/api/reminder-pitch.png`'s per-request-rendered PNG
            bytes -- "chosen once at process start" no longer describes every caller, but the
            caching rationale still holds: that endpoint's whole body is a deterministic function
            of its own query string, so the same request always renders the same bytes, same as
            an unchanging file.
            """
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            if not getattr(self, "_head_request", False):
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
            `_serve_dashboard`'s explicit `?team_id=` lookup and `server_handlers.team_lookup`'s
            `/api/manager-view` handler -- both represent "looking up someone else's team,"
            unlike a cookie-resolved own-team view, which never checks this. Stays here (rather
            than moving into `server_handlers/team_lookup.py`) because `_serve_dashboard`, the
            core route, needs it too -- this is exactly the "genuinely cross-cutting plumbing"
            issue #210 left in `server.py`."""
            saved_profile = profiles.load_profile(common.profiles_db_path(root), team_id)
            return bool(saved_profile and saved_profile.get("opted_out"))

        def _resolve_team_lookup(self, team_id):
            """Issue #125: the exception-handled `lookup_action` call, shared by
            `_serve_dashboard`'s team_id-resolved HTML path and `server_handlers.team_lookup`'s
            `/api/manager-view`/`/api/archive-team-forecast` handlers, so every computation of a
            team's view can never drift from any other. Returns `(manager, weekly_decisions)` on
            success, or `(None, None)` on failure (already logged here). Same "genuinely
            cross-cutting plumbing" reasoning as `_team_lookup_opted_out` above."""
            try:
                lookup_result = lookup_action(team_id)
                return lookup_result["manager"], lookup_result["weekly_decisions"]
            except Exception as error:
                print(f"Team lookup failed: {error!r}\n{traceback.format_exc()}", file=sys.stderr)
                return None, None

        def _serve_dashboard(self, query_string):
            query_team_id = parse_team_id(query_string)
            # A team_id query param is an explicit, one-off no-signup lookup (issue #46) --
            # someone else's team. A team_id from a saved cookie is "my own remembered team"
            # (issue #45): same per-request compute path, but never flagged as a one-off lookup,
            # since it's the visitor's own default view, not a look at someone else's team.
            is_explicit_lookup = query_team_id is not None
            team_id = query_team_id if query_team_id is not None else parse_team_id_cookie(
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
                    if risk in common.ALLOWED_RISK_PROFILES:
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
            if path == "/api/release-notes-confirm-subscription":
                self._handle_release_notes_confirm_subscription(split_path.query)
                return
            if path == "/api/release-notes-unsubscribe":
                self._handle_release_notes_unsubscribe(split_path.query)
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
            if path == "/api/reminder-pitch.png":
                self._handle_reminder_pitch(split_path.query)
                return
            # Issue #216: previously /favicon.ico alone answered 204 (no icon), and every other
            # icon/robots.txt path a real browser or crawler tries fell through to the generic
            # 404 below -- confirmed live via access logs (Twitterbot, Applebot, and iOS Safari's
            # own NetworkingExtension all hit these within minutes of a tweet linking the
            # dashboard). All three icon paths now serve the same brand PNG; the *-precomposed
            # variant is iOS's older, pre-iOS7 lookup that some Safari versions still try first.
            if path in {"/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"}:
                self._send_static(APP_ICON_PNG, "image/png")
                return
            if path == "/robots.txt":
                self._send_static(_ROBOTS_TXT, "text/plain; charset=utf-8")
                return
            self._json(404, {"status": "error", "message": "Not found"})

        def do_HEAD(self):
            # Bug fix: BaseHTTPRequestHandler has no default HEAD support -- only defining
            # do_GET/do_POST left every HEAD request (routine for uptime/health-check probes,
            # confirmed live: Railway's own platform health check hits "/" this way) falling
            # through to the stdlib's generic "Unsupported method" 501, spamming logs with
            # nothing actionable in them. Reuses do_GET's exact routing rather than duplicating
            # it -- every response path already funnels through _json/_send_html/_send_static, all
            # of which skip the body write (but still send the real headers, Content-Length
            # included) when this flag is set.
            self._head_request = True
            try:
                self.do_GET()
            finally:
                self._head_request = False

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
                "/api/release-notes-subscribe",
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
                # Large enough for release_notes.py's own worst case: up to
                # _MAX_CHANGES_PER_ENTRY (50) changes, each up to a ~700-char title+description,
                # plus JSON escaping overhead.
                40960 if path == "/api/release-notes"
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
            elif path == "/api/release-notes-subscribe":
                self._handle_release_notes_subscribe(body)
            else:
                self._handle_contact(body)

        def _client_ip(self):
            # `self.client_address` is set by socketserver.BaseRequestHandler.__init__ before
            # setup()/handle() ever run, so it's always available here.
            return self.client_address[0] if getattr(self, "client_address", None) else "-"

        def _client_user_agent(self):
            # `self.headers` is only populated once parse_request() succeeds, which never happens
            # for a connection that times out before sending a full request line (log_error's
            # primary TimeoutError case below), so that has to be guarded rather than assumed
            # present. User-Agent is attacker-controlled input written straight into logs --
            # collapsing whitespace neutralizes the trivial newline-injection case (a crafted
            # header forging a fake extra log line) without needing a real sanitizer for what's
            # just an access log.
            headers = getattr(self, "headers", None)
            return " ".join((headers.get("User-Agent", "-") if headers else "-").split()) or "-"

        def _client_label(self):
            # Bug fix: the override below used to print only the timestamp and message, dropping
            # the client IP the stdlib's own default log_message normally includes, and never
            # capturing User-Agent at all -- every line looked identical regardless of who sent
            # it, with no way to tell a platform health check, an uptime monitor, a crawler, or a
            # real visitor apart after the fact.
            return f'{self._client_ip()} "{self._client_user_agent()}"'

        def log_request(self, code="-", size="-"):
            # Structured access log: one JSON object per completed request/response, printed to
            # stdout instead of a plain "[date] ip "UA" "METHOD /path HTTP/1.1" status -" line.
            # Railway's Log Explorer auto-parses top-level fields of single-line JSON stdout into
            # filterable @attributes ("message"/"level" are recognized specially; every other key
            # becomes "@fieldname") -- see docs.railway.com/guides/structured-logging-production.
            # That turns "calls per API" / "status codes per API" from a substring search over
            # free text into exact filters like `@route:/api/profile AND @status:400`, which is
            # what an Observability dashboard panel's saved filter keys off of.
            #
            # Every response in this handler flows through self.send_response() (via _json/
            # _send_html, plus the one direct 204 for /favicon.ico), which is the stdlib's single
            # call site for log_request -- so this one override covers every route uniformly.
            # `code` is always a plain int here (200/204/403/404/421/...), never HTTPStatus, so no
            # extra unwrapping is needed the way the stdlib default's log_request does it.
            route = urlsplit(self.path).path
            level = "error" if isinstance(code, int) and code >= 500 else (
                "warn" if isinstance(code, int) and code >= 400 else "info"
            )
            print(json.dumps({
                "message": f'{self._client_label()} "{self.requestline}" {code} {size}',
                "level": level,
                "method": self.command,
                "route": route,
                "status": code,
                "ip": self._client_ip(),
                "user_agent": self._client_user_agent(),
            }))

        def log_message(self, message, *args):
            # Connection-level diagnostics that never reach log_request above (a connection that
            # timed out or dropped before any response was sent -- log_error's two quiet-line
            # cases below) stay plain text: there's no route/status to attach as attributes for,
            # and Railway still ingests the line fine, just without custom @fields.
            print(f"[{self.log_date_time_string()}] {self._client_label()} {message % args}")

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
            # socket.timeout is included explicitly for Python < 3.10: from 3.10 on it's just an
            # alias for the builtin TimeoutError, but on 3.9 and earlier it's a separate OSError
            # subclass, so isinstance(args[0], TimeoutError) alone silently misses it there and
            # this whole branch falls through to the generic (noisy) log_message call below.
            if args and isinstance(args[0], (TimeoutError, socket.timeout)):
                self.log_message("connection timed out (idle/slow client, %ss limit)", self.timeout)
                return
            # Bug fix: the client-went-away sibling of the timeout case above -- confirmed live,
            # a client (browser tab closed, flaky network, or a proxy/load balancer with a short
            # read timeout) disconnecting mid-response raised BrokenPipeError from wfile.write and
            # dumped a full traceback per occurrence, even though it's exactly as routine as a
            # slow-loris timeout, not a real server-side error.
            if args and isinstance(args[0], (BrokenPipeError, ConnectionResetError)):
                self.log_message("client disconnected before the response finished sending")
                return
            self.log_message(format, *args)

    # Issue #210: each feature's handler is a plain function built by that feature's own
    # `server_handlers/*.py` module, closing over exactly the actions/limiters it needs (the same
    # closure-over-create_server's-locals pattern this file always used for its `_default_*_action`
    # factories). Assigning a plain function onto a class as an attribute makes it an ordinary
    # bound method from then on, so `self._handle_profile(body)` etc. below and in `do_GET`/
    # `do_POST` above work exactly as if these were defined inline in the class body.
    DashboardHandler._handle_refresh = refresh_endpoint.make_handle_refresh(
        action, refresh_cooldown_limiter, refresh_lock
    )
    DashboardHandler._handle_profile = profile_handlers.make_handle_profile(
        profile_write_action, profile_write_limiter
    )
    DashboardHandler._handle_draft_squad = draft_squad_handlers.make_handle_draft_squad(
        draft_squad_write_action, draft_squad_write_limiter
    )
    DashboardHandler._handle_lookup_opt_out = lookup_opt_out_handlers.make_handle_lookup_opt_out(
        lookup_opt_out_write_action, lookup_opt_out_limiter
    )
    DashboardHandler._handle_reminder_opt_in = reminder_handlers.make_handle_reminder_opt_in(
        reminder_opt_in_write_action, reminder_opt_in_limiter
    )
    DashboardHandler._handle_reminder_confirm = reminder_handlers.make_handle_reminder_confirm(
        root, reminder_confirm_limiter
    )
    DashboardHandler._handle_reminder_teams = reminder_handlers.make_handle_reminder_teams(
        root, reminder_teams_token
    )
    DashboardHandler._handle_contact = contact_handlers.make_handle_contact(
        contact_write_action, contact_write_limiter
    )
    DashboardHandler._handle_release_notes = release_notes_handlers.make_handle_release_notes(
        root, release_notes_notify_email_action
    )
    DashboardHandler._handle_release_notes_subscribe = (
        release_notes_handlers.make_handle_release_notes_subscribe(
            release_notes_subscribe_write_action, release_notes_subscribe_limiter
        )
    )
    DashboardHandler._handle_release_notes_confirm_subscription = (
        release_notes_handlers.make_handle_release_notes_confirm_subscription(
            root, release_notes_confirm_limiter
        )
    )
    DashboardHandler._handle_release_notes_unsubscribe = (
        release_notes_handlers.make_handle_release_notes_unsubscribe(
            root, release_notes_confirm_limiter
        )
    )
    DashboardHandler._handle_shared_state = team_lookup.make_handle_shared_state(root)
    DashboardHandler._handle_manager_view = team_lookup.make_handle_manager_view(lookup_limiter)
    DashboardHandler._handle_registered_teams = team_lookup.make_handle_registered_teams(root, token)
    # Not a `make_handle_...` factory like its neighbors above -- see reminder_pitch.py's module
    # docstring for why this endpoint has nothing to close over.
    DashboardHandler._handle_reminder_pitch = reminder_pitch.handle_reminder_pitch
    DashboardHandler._handle_archive_team_forecast = team_lookup.make_handle_archive_team_forecast(root)

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
            #
            # Bug fix: confirmed live, this is exactly where a BrokenPipeError from wfile.write
            # (the client disconnected mid-response -- do_GET/_send_html/_json run inside the
            # request thread, so a write failure there escapes straight to here, unlike the
            # read-side TimeoutError log_error already handles) actually lands -- dumping a full
            # traceback per occurrence for something as routine as a client going away.
            error = sys.exc_info()[1]
            if isinstance(error, TimeoutError):
                timestamp = datetime.now(timezone.utc).strftime("%d/%b/%Y %H:%M:%S")
                print(
                    f"[{timestamp}] connection timed out from {client_address} (server-level)",
                    file=sys.stderr,
                )
                return
            if isinstance(error, (BrokenPipeError, ConnectionResetError)):
                timestamp = datetime.now(timezone.utc).strftime("%d/%b/%Y %H:%M:%S")
                print(
                    f"[{timestamp}] client {client_address} disconnected before the response "
                    "finished sending (server-level)",
                    file=sys.stderr,
                )
                return
            super().handle_error(request, client_address)

    server = _DashboardServer((host, port), DashboardHandler)
    server.refresh_token = token
    return server
