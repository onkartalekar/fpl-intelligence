"""/api/contact: the Contact Us tab -- bug/feature/feedback submissions (issue #110, split by #210)."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import traceback

from .. import reminder_confirmation
from .common import REMINDER_EMAIL_MAX_LENGTH

# Issue #110: category is a fixed, closed option set, mirroring the profile form's `risk_profile`
# select -- matches the Contact Us form's `#contact-category` <option>s.
_ALLOWED_CONTACT_CATEGORIES = {"bug", "feature_request", "feedback", "other"}
_ALLOWED_CONTACT_KEYS = {"category", "message", "reply_to"}
# Generous for free-text feedback while comfortably fitting inside `do_POST`'s shared 4096-byte
# `max_body` cap even after JSON escaping/multi-byte UTF-8 overhead (unlike REMINDER_EMAIL_MAX_
# LENGTH, which bounds an address, this bounds a whole message body, so it stays well under 4096
# rather than up against it).
_CONTACT_MESSAGE_MAX_LENGTH = 2000
_CONTACT_VALIDATION_MESSAGE = "Invalid contact payload"
# Issue #110's local-log durability backstop -- see `_append_contact_log`. Plain-text, gitignored
# (matches this repo's existing blanket `*.log` .gitignore rule), append-only, never served over
# HTTP by any route.
_CONTACT_LOG_FILENAME = "contact-submissions.log"
# Issue #110: this is the app's one endpoint reachable by any visitor with nothing to identify
# them by (no team ID, no PIN, no account) and a free-text body -- the shape most attractive to
# automated spam. Stricter than the ordinary profile-write cooldown (common.
# PROFILE_WRITE_COOLDOWN_SECONDS, 5s) for that reason, but shorter than lookup_opt_out.
# LOOKUP_OPT_OUT_COOLDOWN_SECONDS (30s, guards a PIN-guessing surface, a different threat model
# entirely) -- a genuine visitor submitting one piece of feedback is not meaningfully
# inconvenienced by a 30s per-source cooldown, while it still caps a single source's submission
# rate at a low, unautomatable-feeling pace.
CONTACT_COOLDOWN_SECONDS = 30


class ContactValidationError(Exception):
    """Raised when a submitted /api/contact payload fails validation."""


def _contact_log_path(root):
    return Path(root) / "data" / _CONTACT_LOG_FILENAME


def validate_contact_payload(payload):
    """Validate and normalize a /api/contact request body (issue #110).

    Returns a cleaned dict with exactly `category`/`message`/`reply_to`, or raises
    ContactValidationError with a fixed, input-free message -- same shape-only-error posture as
    `profile.validate_profile_payload`/`reminder.validate_reminder_opt_in_payload`: every error
    path returns one fixed message, never reflecting any part of the submitted payload back to
    the caller.
    """
    if not isinstance(payload, dict):
        raise ContactValidationError(_CONTACT_VALIDATION_MESSAGE)
    if not set(payload.keys()) <= _ALLOWED_CONTACT_KEYS:
        raise ContactValidationError(_CONTACT_VALIDATION_MESSAGE)

    category = payload.get("category")
    if category not in _ALLOWED_CONTACT_CATEGORIES:
        raise ContactValidationError(_CONTACT_VALIDATION_MESSAGE)

    message = payload.get("message")
    if (
        not isinstance(message, str) or not message.strip()
        or len(message) > _CONTACT_MESSAGE_MAX_LENGTH
    ):
        raise ContactValidationError(_CONTACT_VALIDATION_MESSAGE)

    reply_to = payload.get("reply_to")
    if reply_to is None or reply_to == "":
        cleaned_reply_to = None
    else:
        # Same "presence of '@', bounded length" rigor as `reminder.
        # validate_reminder_opt_in_payload`'s own `email` check -- not attempting full RFC 5322
        # validation, and reusing the same length cap since this is the same kind of field (a
        # visitor-supplied address).
        if (
            not isinstance(reply_to, str) or "@" not in reply_to or not reply_to.strip()
            or len(reply_to) > REMINDER_EMAIL_MAX_LENGTH
        ):
            raise ContactValidationError(_CONTACT_VALIDATION_MESSAGE)
        cleaned_reply_to = reply_to.strip()

    return {"category": category, "message": message.strip(), "reply_to": cleaned_reply_to}


def _append_contact_log(root, cleaned, now):
    """Append one JSON line to the local durability-backstop log (issue #110).

    Deliberately a plain `open(path, "a")` append, not `fpl_data.atomic_write_text` -- that
    helper does a whole-file atomic *replace* (write a temp file, fsync, `os.replace`), the right
    tool for a file with one current value (e.g. `dashboard-state.json`), but the wrong one here:
    applying it to a log meant to grow one line per submission over the life of a deployment
    would mean reading and rewriting the entire file on every single submission, an unbounded and
    ever-growing cost. A plain append avoids that, and POSIX guarantees a `write()` of a
    line-sized payload to a file opened in append mode (`O_APPEND`) is atomic against
    interleaving from other writers -- sufficient for "one JSON line per submission", which is
    all this backstop needs to be (per the plan doc: no query capability, no review UI, just a
    plain-text safety net an operator can `cat`/`grep` by hand).
    """
    path = _contact_log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {
            "timestamp": now,
            "category": cleaned["category"],
            "message": cleaned["message"],
            "reply_to": cleaned["reply_to"],
        },
        ensure_ascii=False,
    )
    with open(path, "a", encoding="utf-8") as log_file:
        log_file.write(line + "\n")


def default_contact_email_action():
    """Build the default Contact Us notification-email sender, thin wrapper over
    `reminder_confirmation` so `create_server` can inject a fake for tests without ever touching
    real SMTP/`smtplib` -- same role as `reminder.default_reminder_email_action`.
    """

    def action(category, message, reply_to):
        reminder_confirmation.send_contact_email(category, message, reply_to)

    return action


def default_contact_action(root, send_email_action):
    """Build the default /api/contact write action (issue #110).

    Order matters, and is the entire point of the local-log durability backstop (see the plan
    doc's "Decided" section): validate the payload, then *always* append it to the local log
    first, and only after that succeeds attempt the operator-notification email -- never the
    reverse, and the local-log write is never skipped or made conditional on the email send
    succeeding. This is the deliberate mirror image of `reminder.default_reminder_opt_in_action`'s
    "enable" path, which sends its email BEFORE writing to the DB (so a failed send never leaves
    a dangling, un-deliverable confirmation token); here, an email failure must never lose a
    visitor's already-submitted feedback, so the durable record is written first and the
    notification is best-effort on top of it.

    A `ReminderEmailError` from the email attempt is caught here, not re-raised: the visitor's
    submission was already captured in the local log by this point, so from the visitor's
    perspective this is still a successful submission -- surfacing a failure here would only
    invite a confusing, unnecessary resubmission. The failure is still logged to stderr (matching
    every other `except Exception as error: print(f"...{error!r}...", file=sys.stderr)` site in
    this codebase) so the operator can notice a broken SMTP configuration via Railway's logs.
    """

    def action(payload):
        cleaned = validate_contact_payload(payload)
        now = datetime.now(timezone.utc).isoformat()
        _append_contact_log(root, cleaned, now)
        try:
            send_email_action(cleaned["category"], cleaned["message"], cleaned["reply_to"])
        except reminder_confirmation.ReminderEmailError as error:
            print(
                f"Contact notification email failed (submission was still logged): {error!r}",
                file=sys.stderr,
            )
        return {"status": "ok"}

    return action


def make_handle_contact(contact_write_action, contact_write_limiter):
    """Build the POST /api/contact handler, same DI-closure shape as `profile.
    make_handle_profile`."""

    def handle_contact(self, body):
        # Open endpoint (issue #110, following issue #45's model, same as the other four open
        # write endpoints) -- no X-Refresh-Token, own per-source CooldownLimiter guards against
        # automated abuse.
        if not contact_write_limiter.allow(self.client_address[0]):
            self._json(429, {"status": "error", "message": "Too many messages sent. Try again shortly."})
            return
        try:
            payload = json.loads(body.decode("utf-8")) if body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"status": "error", "message": _CONTACT_VALIDATION_MESSAGE})
            return
        if not isinstance(payload, dict):
            self._json(400, {"status": "error", "message": _CONTACT_VALIDATION_MESSAGE})
            return
        try:
            contact_write_action(payload)
            # Deliberately always this same success response once validation and the local
            # log write succeed, regardless of whether the notification email itself
            # succeeded -- see `default_contact_action`'s docstring for the full reasoning:
            # the visitor's submission is durably captured either way, so a failed email is
            # never surfaced here as a reason to resubmit.
            self._json(200, {"status": "ok", "message": "Thanks -- your message has been received."})
        except ContactValidationError as error:
            self._json(400, {"status": "error", "message": str(error)})
        except Exception as error:
            print(f"Contact submission failed: {error!r}\n{traceback.format_exc()}", file=sys.stderr)
            self._json(500, {"status": "error", "message": "Could not process your message. Try again shortly."})

    return handle_contact
