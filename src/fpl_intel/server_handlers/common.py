"""Helpers genuinely shared by 2+ feature handler modules (issue #210).

Everything here was previously module-scope in `server.py` and used by whichever handlers needed
it via a plain function call (no closures) -- unlike `server.py`'s own `_default_*_action`
factories, none of this needs per-server-instance configuration, so it moved as-is with no
signature changes.
"""

from hashlib import sha256
from html import escape as html_escape
from http import cookies as http_cookies
from pathlib import Path
import re
from urllib.parse import parse_qs

# Issue #45: a manager can save a profile identified only by their public FPL team ID -- this
# bounds it to the same shape/range FPL itself uses.
_TEAM_ID_RE = re.compile(r"^[0-9]{1,8}$")

ALLOWED_RISK_PROFILES = {"conservative", "balanced", "aggressive"}

TEAM_COOKIE_NAME = "fpl_team_id"
TEAM_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 300  # ~300 days, comfortably spans a season

# RFC 5321's practical maximum total address length -- shared by every endpoint that accepts a
# visitor-supplied email (reminder opt-in, release-notes subscribe, contact's reply_to).
REMINDER_EMAIL_MAX_LENGTH = 254

# The three lead-hours choices a reminder can be sent at -- shared by /api/reminder-opt-in's
# "enable" validation (reminder.py) and /api/archive-team-forecast's own `lead_hours` field
# (team_lookup.py), which archives a forecast at the same checkpoints the reminder email fires at.
ALLOWED_REMINDER_LEAD_HOURS = {3, 12, 24}

# Shared by /api/profile and /api/draft-squad (issue #45's tier-2 write-safety model): a separate
# `CooldownLimiter` instance per endpoint so saving a profile and declaring a draft squad don't
# compete for the same cooldown window, but both use this same per-source cooldown length --
# SQLite's own transaction already handles concurrent-write safety, so this is only about
# bounding automated abuse of an open write endpoint, not correctness.
PROFILE_WRITE_COOLDOWN_SECONDS = 5


def coerce_team_id(raw):
    """Validate a raw team-ID string (from a query param or cookie), or None if invalid."""
    if raw is None or not _TEAM_ID_RE.match(raw):
        return None
    team_id = int(raw)
    if not (1 <= team_id <= 99_999_999):
        return None
    return team_id


def parse_team_id(query_string):
    """Extract a valid `team_id` query parameter, or None if absent/malformed.

    Malformed input (not the expected shape) is treated the same as absent -- a mistyped URL
    falls back to the normal shared dashboard rather than surfacing a hard error, since this is
    a query param a person may hand-edit in the address bar.
    """
    values = parse_qs(query_string).get("team_id")
    if not values:
        return None
    return coerce_team_id(values[0])


def parse_team_id_cookie(cookie_header):
    """Extract a valid `fpl_team_id` cookie value, or None if absent/malformed."""
    if not cookie_header:
        return None
    parsed = http_cookies.SimpleCookie()
    try:
        parsed.load(cookie_header)
    except http_cookies.CookieError:
        return None
    morsel = parsed.get(TEAM_COOKIE_NAME)
    if morsel is None:
        return None
    return coerce_team_id(morsel.value)


def team_cookie_header(team_id):
    """Build the Set-Cookie header value that remembers `team_id` for this browser.

    Plain (unsigned) on purpose -- issue #45's security model treats this as convenience, not a
    credential: a manager's FPL data is already public, so there's nothing this cookie needs to
    keep secret, only something worth remembering across visits. `Secure` is safe to set even for
    local http://127.0.0.1 testing -- browsers already treat loopback as a trustworthy origin.
    """
    return (
        f"{TEAM_COOKIE_NAME}={team_id}; Max-Age={TEAM_COOKIE_MAX_AGE_SECONDS}; "
        "Path=/; HttpOnly; Secure; SameSite=Lax"
    )


def hash_pin(pin):
    return sha256(pin.encode("utf-8")).hexdigest()


def profiles_db_path(root):
    return Path(root) / "data" / "profiles.db"


def render_reminder_confirm_page(ok, message):
    """A small, self-contained HTML confirmation page -- reached by clicking a link from an
    email client, not a fetch call, so unlike every JSON endpoint it can't return JSON. No
    cookie/session context is assumed; the only affordance is a link back to `/`. Shared by the
    deadline-reminder confirm flow (issue #79) and the release-notes subscribe/unsubscribe flow
    (issue #143) -- both are "click a link from an email" flows with the same contract.
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
