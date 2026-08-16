"""What's New / release-notes feature area (issue #143, split by #210):

- POST /api/release-notes -- operator-only, publish one day's entry (the daily generation job).
- POST /api/release-notes-subscribe -- open, double opt-in email subscription.
- GET  /api/release-notes-confirm-subscription -- the confirmation link a subscribe email carries.
- GET  /api/release-notes-unsubscribe -- the link every sent release-notes email carries.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import secrets
import sys
import traceback
from urllib.parse import parse_qs, quote

from .. import release_notes
from .. import release_notes_email
from .. import release_notes_subscribers
from .. import reminder_confirmation
from .common import REMINDER_EMAIL_MAX_LENGTH, hash_pin, render_reminder_confirm_page

_RELEASE_NOTES_CONFIRMATION_TTL_HOURS = 24
_ALLOWED_RELEASE_NOTES_SUBSCRIBE_KEYS = {"email"}
_RELEASE_NOTES_SUBSCRIBE_VALIDATION_MESSAGE = "Invalid release-notes subscribe payload"
# Issue #143: own separate cooldowns from reminder.py's -- same double-opt-in shape, but a
# distinct feature with its own abuse surface, not reused from there.
RELEASE_NOTES_SUBSCRIBE_COOLDOWN_SECONDS = 30
RELEASE_NOTES_CONFIRM_COOLDOWN_SECONDS = 30


class ReleaseNotesSubscribeValidationError(Exception):
    """Raised when a submitted /api/release-notes-subscribe payload fails validation."""


class ReleaseNotesSubscribeSendError(Exception):
    """Raised when the subscription confirmation email could not be sent -- the send is always
    attempted before any DB write (same principle as /api/reminder-opt-in's "enable" path), so
    this means nothing was persisted."""


def _release_notes_subscribers_db_path(root):
    # A separate file from profiles.db, not another table there -- see release_notes_subscribers.
    # py's own module docstring for why (email-keyed, unrelated to the per-team profile schema).
    return Path(root) / "data" / "release-notes-subscribers.db"


def validate_release_notes_subscribe_payload(payload):
    """Validate and normalize a /api/release-notes-subscribe request body. Same email-shape check
    as `reminder.validate_reminder_opt_in_payload`'s "enable" path -- presence-of-"@"/length only,
    not full RFC 5322 validation."""
    if not isinstance(payload, dict):
        raise ReleaseNotesSubscribeValidationError(_RELEASE_NOTES_SUBSCRIBE_VALIDATION_MESSAGE)
    if not set(payload.keys()) <= _ALLOWED_RELEASE_NOTES_SUBSCRIBE_KEYS:
        raise ReleaseNotesSubscribeValidationError(_RELEASE_NOTES_SUBSCRIBE_VALIDATION_MESSAGE)
    email = payload.get("email")
    if (
        not isinstance(email, str) or "@" not in email or not email.strip()
        or len(email) > REMINDER_EMAIL_MAX_LENGTH
    ):
        raise ReleaseNotesSubscribeValidationError(_RELEASE_NOTES_SUBSCRIBE_VALIDATION_MESSAGE)
    return {"email": email.strip().lower()}


def default_release_notes_subscribe_email_action():
    """Thin wrapper over `release_notes_email`, same pattern as `reminder.
    default_reminder_email_action` -- lets `create_server` inject a fake for tests without ever
    touching real SMTP."""

    def action(email, confirm_url):
        release_notes_email.send_release_notes_subscription_email(email, confirm_url)

    return action


def default_release_notes_notify_email_action():
    """Thin wrapper for sending one published entry to one confirmed subscriber -- the
    send-on-publish step in `make_handle_release_notes`."""

    def action(email, entry, unsubscribe_url):
        release_notes_email.send_release_notes_email(email, entry, unsubscribe_url)

    return action


def default_release_notes_subscribe_action(root, send_email_action):
    """Build the default /api/release-notes-subscribe write action (issue #143): double opt-in,
    mirroring `reminder.default_reminder_opt_in_action`'s "enable" path -- generates a token,
    attempts the confirmation-email send FIRST, and only writes a pending row on send success, so
    a pending confirmation is never persisted for a token that was never actually emailed. No
    per-team-ID-equivalent cooldown here (that pattern exists in reminder-opt-in to bound repeated
    sends *at the same target*, keyed by team_id) -- `release_notes_subscribers.set_pending` is
    itself idempotent per email (refreshes the token rather than erroring), and the endpoint's own
    per-source `CooldownLimiter` (checked by the caller, `make_handle_release_notes_subscribe`)
    already bounds repeated submissions.
    """

    def action(payload, base_url):
        cleaned = validate_release_notes_subscribe_payload(payload)
        db_path = _release_notes_subscribers_db_path(root)
        token = secrets.token_urlsafe(32)
        confirm_url = (
            f"{base_url}/api/release-notes-confirm-subscription"
            f"?email={quote(cleaned['email'])}&token={token}"
        )
        try:
            send_email_action(cleaned["email"], confirm_url)
        except reminder_confirmation.ReminderEmailError as error:
            raise ReleaseNotesSubscribeSendError(str(error)) from error
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=_RELEASE_NOTES_CONFIRMATION_TTL_HOURS)
        ).isoformat()
        release_notes_subscribers.set_pending(
            db_path, cleaned["email"], hash_pin(token), expires_at,
            datetime.now(timezone.utc).isoformat(),
        )
        return {"email": cleaned["email"]}

    return action


def _notify_release_notes_subscribers(root, release_notes_notify_email_action, host, entry):
    """Send `entry` to every confirmed subscriber -- the "one email each time a new entry
    publishes" half of issue #143's email subscription. Called only after the entry is already
    durably stored (after the 200 response for the publish itself has already been sent), so a
    send failure here can never lose or roll back the publish -- same "durability first,
    notification best-effort" posture as Contact Us. Each recipient's send is independent: one
    failing (or the whole step failing) never blocks or retries the others, and is only logged,
    never surfaced to the daily job that triggered the publish.
    """
    try:
        db_path = _release_notes_subscribers_db_path(root)
        subscribers = release_notes_subscribers.list_confirmed(db_path)
    except Exception as error:
        print(f"Release-notes subscriber lookup failed: {error!r}\n{traceback.format_exc()}", file=sys.stderr)
        return
    if not subscribers:
        return
    base_url = f"http://{host}"
    for subscriber in subscribers:
        unsubscribe_url = (
            f"{base_url}/api/release-notes-unsubscribe"
            f"?email={quote(subscriber['email'])}&token={subscriber['unsubscribe_token']}"
        )
        try:
            release_notes_notify_email_action(subscriber["email"], entry, unsubscribe_url)
        except Exception as error:
            print(
                f"Release-notes notify failed for one subscriber: {error!r}\n{traceback.format_exc()}",
                file=sys.stderr,
            )


def make_handle_release_notes(root, release_notes_notify_email_action):
    """Build the POST /api/release-notes handler (issue #143): publish one day's "What's New"
    entry.

    Operator-only, same as /api/refresh/api/archive-team-forecast -- gated on the same
    X-Refresh-Token (checked by `do_POST` before dispatch), no per-source cooldown of its own,
    since only the daily generation job (`scripts/publish_release_notes.py`) and a human operator
    ever hold that token. Idempotent by date: re-publishing the same date overwrites that date's
    entry rather than duplicating it (`release_notes.upsert_entry`'s own docstring) -- safe for
    the daily job to retry.
    """

    def handle_release_notes(self, body):
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
        _notify_release_notes_subscribers(
            root, release_notes_notify_email_action, self.headers.get("Host"), stored,
        )

    return handle_release_notes


def make_handle_release_notes_subscribe(release_notes_subscribe_write_action, release_notes_subscribe_limiter):
    """Build the POST /api/release-notes-subscribe handler (issue #143): open, rate-limited,
    double opt-in -- same posture as /api/reminder-opt-in's "enable" path."""

    def handle_release_notes_subscribe(self, body):
        if not release_notes_subscribe_limiter.allow(self.client_address[0]):
            self._json(429, {"status": "error", "message": "Too many subscribe requests. Try again shortly."})
            return
        try:
            payload = json.loads(body.decode("utf-8")) if body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"status": "error", "message": _RELEASE_NOTES_SUBSCRIBE_VALIDATION_MESSAGE})
            return
        if not isinstance(payload, dict):
            self._json(400, {"status": "error", "message": _RELEASE_NOTES_SUBSCRIBE_VALIDATION_MESSAGE})
            return
        base_url = f"http://{self.headers.get('Host')}"
        try:
            release_notes_subscribe_write_action(payload, base_url)
            self._json(200, {"status": "ok", "message": "Check your email to confirm."})
        except ReleaseNotesSubscribeValidationError as error:
            self._json(400, {"status": "error", "message": str(error)})
        except ReleaseNotesSubscribeSendError as error:
            self._json(502, {"status": "error", "message": str(error)})
        except Exception as error:
            print(f"Release-notes subscribe failed: {error!r}\n{traceback.format_exc()}", file=sys.stderr)
            self._json(500, {"status": "error", "message": "Release-notes subscribe failed"})

    return handle_release_notes_subscribe


def make_handle_release_notes_confirm_subscription(root, release_notes_confirm_limiter):
    """Build the GET /api/release-notes-confirm-subscription handler (issue #143) -- reached by
    clicking the confirmation email's link, same shape as `reminder.make_handle_reminder_confirm`.
    """

    def handle_release_notes_confirm_subscription(self, query_string):
        if not release_notes_confirm_limiter.allow(self.client_address[0]):
            self._send_html(
                render_reminder_confirm_page(False, "Too many attempts. Try again shortly.")
            )
            return
        params = parse_qs(query_string)
        raw_email = (params.get("email") or [None])[0]
        raw_token = (params.get("token") or [None])[0]
        invalid_message = "This confirmation link is invalid or has already been used."
        if not raw_email or not raw_token:
            self._send_html(render_reminder_confirm_page(False, invalid_message))
            return
        email = raw_email.strip().lower()
        db_path = _release_notes_subscribers_db_path(root)
        saved = release_notes_subscribers.load(db_path, email)
        stored_hash = saved.get("confirm_token_hash") if saved else None
        if not stored_hash or not secrets.compare_digest(stored_hash, hash_pin(raw_token)):
            self._send_html(render_reminder_confirm_page(False, invalid_message))
            return
        expires_at = saved.get("confirm_expires_at")
        try:
            expired = expires_at is None or datetime.fromisoformat(expires_at) < datetime.now(timezone.utc)
        except ValueError:
            expired = True
        if expired:
            self._send_html(
                render_reminder_confirm_page(
                    False, "This confirmation link has expired. Subscribe again from the What's New tab.",
                )
            )
            return
        unsubscribe_token = secrets.token_urlsafe(32)
        release_notes_subscribers.confirm(
            db_path, email, unsubscribe_token, datetime.now(timezone.utc).isoformat(),
        )
        self._send_html(
            render_reminder_confirm_page(True, "You're subscribed to FPL Intelligence release notes.")
        )

    return handle_release_notes_confirm_subscription


def make_handle_release_notes_unsubscribe(root, release_notes_confirm_limiter):
    """Build the GET /api/release-notes-unsubscribe handler (issue #143) -- the link every sent
    release-notes email carries in its footer. Deletes the subscriber row entirely rather than
    leaving a tombstone, same posture as declining/disabling a reminder opt-in."""

    def handle_release_notes_unsubscribe(self, query_string):
        if not release_notes_confirm_limiter.allow(self.client_address[0]):
            self._send_html(
                render_reminder_confirm_page(False, "Too many attempts. Try again shortly.")
            )
            return
        params = parse_qs(query_string)
        raw_email = (params.get("email") or [None])[0]
        raw_token = (params.get("token") or [None])[0]
        invalid_message = "This unsubscribe link is invalid."
        if not raw_email or not raw_token:
            self._send_html(render_reminder_confirm_page(False, invalid_message))
            return
        email = raw_email.strip().lower()
        db_path = _release_notes_subscribers_db_path(root)
        saved = release_notes_subscribers.load(db_path, email)
        stored_token = saved.get("unsubscribe_token") if saved else None
        if not stored_token or not secrets.compare_digest(stored_token, raw_token):
            self._send_html(render_reminder_confirm_page(False, invalid_message))
            return
        release_notes_subscribers.unsubscribe(db_path, email)
        self._send_html(
            render_reminder_confirm_page(True, "You've been unsubscribed from FPL Intelligence release notes.")
        )

    return handle_release_notes_unsubscribe
