"""Confirmation-email composition and SMTP send for the reminder opt-in double opt-in
(issue #79) -- see plans/issue-79-reminder-opt-in.md. Also composes and sends the Contact Us
notification email to the operator (issue #110, see plans/issue-110-contact-us-tab.md), which
reuses this module's `_read_smtp_config()`/`ReminderEmailError` rather than duplicating the
SMTP-config-reading/connection logic in a second place -- both call sites run synchronously
inside the same live server process with the same exposure profile, so there is no reason for
two copies of this logic.

Distinct from `scripts/send_deadline_reminder.py`'s own SMTP sending: this module only ever sends
one short email synchronously from a live request handler, never the full reminder digest that
script owns, and runs inside the live server process (`server.py`'s
`_handle_reminder_opt_in`/`_handle_contact`) -- a different exposure profile from that script's
offline, trusted GitHub Actions cron (live server vs. offline cron). An earlier version of this
module read its own separate `FPL_INTEL_SERVER_SMTP_*` env vars specifically so the two could be
rotated independently, but in practice both have always pointed at the same mailbox -- so it now
reads the same `FPL_INTEL_SMTP_*` vars `send_deadline_reminder.py` and `live_regression_check.py`
already use, for one credential pair to provision and rotate everywhere instead of two. Issue
#110's plan doc considered a further split (a dedicated `FPL_INTEL_CONTACT_SMTP_*`) and rejected
it for the same reason: the contact form has the same exposure profile as the reminder
confirmation send, not a different one.

Follows `news_signals.py`'s fail-safe posture: env vars are read at call time (never cached at
import time), never logged, and any missing configuration or network/auth/SMTP failure is turned
into a single narrow `ReminderEmailError` -- never a bare `smtplib`/`OSError`, and never silently
swallowed into a `True`/`False` return, so the caller (`server.py`) always has exactly one
exception type to catch and turn into a clean, input-free error response. Per the plan, a failed
reminder-confirmation send must never result in a DB row referencing a confirmation token that
was never actually delivered -- `server.py` is responsible for sending before writing, never the
reverse. The contact form's own failure handling is the opposite by design (see
plans/issue-110-contact-us-tab.md's "Decided" section and `server.py`'s `_default_contact_action`):
a failed notification email must never lose the visitor's submission, so `server.py` always
writes the local durability-backstop log FIRST and only then attempts this module's
`send_contact_email`, treating a `ReminderEmailError` from it as a server-side-only concern.
"""

from email.message import EmailMessage
import os
import smtplib


SMTP_HOST_ENV_VAR = "FPL_INTEL_SMTP_HOST"
SMTP_PORT_ENV_VAR = "FPL_INTEL_SMTP_PORT"
SMTP_USER_ENV_VAR = "FPL_INTEL_SMTP_USER"
SMTP_PASSWORD_ENV_VAR = "FPL_INTEL_SMTP_PASSWORD"

# Short on purpose: this call happens synchronously inside a request handler, unlike the offline
# reminder script's own 30s timeout -- a slow/unreachable SMTP host must not tie up a request
# thread for long.
_SEND_TIMEOUT_SECONDS = 10


class ReminderEmailError(Exception):
    """Raised when a confirmation email could not be sent -- missing/invalid SMTP configuration,
    or any network/auth/SMTP-protocol failure. The message is always safe to return to an
    unauthenticated caller: it never includes the destination address, SMTP credentials, or a
    raw exception's internal detail.
    """


def _read_smtp_config():
    host = os.environ.get(SMTP_HOST_ENV_VAR)
    port_raw = os.environ.get(SMTP_PORT_ENV_VAR)
    user = os.environ.get(SMTP_USER_ENV_VAR)
    password = os.environ.get(SMTP_PASSWORD_ENV_VAR)
    if not host or not port_raw or not user or not password:
        raise ReminderEmailError("Reminder email is not configured on this server.")
    try:
        port = int(port_raw)
    except ValueError as error:
        raise ReminderEmailError("Reminder email is not configured on this server.") from error
    return {"host": host, "port": port, "user": user, "password": password}


def compose_confirmation_email(confirm_url, lead_hours):
    """Return (subject, body) for the confirmation email. Content only -- no send side effect."""
    subject = "Confirm your FPL Intelligence deadline reminders"
    body = (
        "Someone (hopefully you) requested deadline reminders from FPL Intelligence, "
        f"{lead_hours} hour(s) before each gameweek deadline.\n\n"
        "Confirm by opening this link:\n"
        f"{confirm_url}\n\n"
        "If you didn't request this, you can ignore this email -- nothing is enabled until this "
        "link is clicked, and this link expires automatically if it isn't used.\n\n"
        "-- FPL Intelligence automated deadline reminder opt-in (issue #79)"
    )
    return subject, body


def send_confirmation_email(to_email, confirm_url, lead_hours, smtp_config=None):
    """Send the confirmation email. Returns True on success.

    Raises `ReminderEmailError` on any configuration or send failure -- never returns False, and
    never lets a raw `smtplib`/`OSError` escape to the caller. `smtp_config` is accepted mainly
    for tests; real callers should omit it and let this read `FPL_INTEL_SMTP_*` at call time.
    """
    smtp_config = smtp_config or _read_smtp_config()
    subject, body = compose_confirmation_email(confirm_url, lead_hours)
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp_config["user"]
    message["To"] = to_email
    message.set_content(body)
    try:
        with smtplib.SMTP(
            smtp_config["host"], smtp_config["port"], timeout=_SEND_TIMEOUT_SECONDS
        ) as smtp:
            smtp.starttls()
            smtp.login(smtp_config["user"], smtp_config["password"])
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError, TimeoutError) as error:
        raise ReminderEmailError(
            "Could not send the confirmation email. Try again shortly."
        ) from error
    return True


_CONTACT_CATEGORY_LABELS = {
    "bug": "Bug report",
    "feature_request": "Feature request",
    "feedback": "Feedback",
    "other": "Other",
}


def compose_contact_email(category, message, reply_to):
    """Return (subject, body) for the Contact Us notification email sent to the operator
    (issue #110). Content only -- no send side effect. Contains exactly what the visitor
    submitted (category, message, optional reply-to) and nothing else -- no server internals,
    no IP address, no other visitor metadata, per the plan doc.
    """
    label = _CONTACT_CATEGORY_LABELS.get(category, category)
    subject = f"FPL Intelligence contact form: {label}"
    lines = [
        f"Category: {label}",
        "",
        "Message:",
        message,
        "",
        f"Reply-to: {reply_to}" if reply_to else "Reply-to: (not provided)",
        "",
        "-- FPL Intelligence Contact Us form (issue #110)",
    ]
    return subject, "\n".join(lines)


def send_contact_email(category, message, reply_to, smtp_config=None):
    """Send the Contact Us notification email to the operator. Returns True on success.

    Raises `ReminderEmailError` on any configuration or send failure -- same contract as
    `send_confirmation_email` above, reusing its `_read_smtp_config()`/`_SEND_TIMEOUT_SECONDS`.
    The notification is sent to the configured SMTP account itself (there is no separate
    "operator recipient" env var, by design -- see this module's docstring): the same mailbox
    already used to send reminder confirmations is where the operator reads this. `reply_to` is
    set as the email's `Reply-To` header (when given) purely so the operator can hit reply in
    their mail client to respond directly to the visitor -- it is never used as the send target.
    """
    smtp_config = smtp_config or _read_smtp_config()
    subject, body = compose_contact_email(category, message, reply_to)
    email_message = EmailMessage()
    email_message["Subject"] = subject
    email_message["From"] = smtp_config["user"]
    email_message["To"] = smtp_config["user"]
    if reply_to:
        email_message["Reply-To"] = reply_to
    email_message.set_content(body)
    try:
        with smtplib.SMTP(
            smtp_config["host"], smtp_config["port"], timeout=_SEND_TIMEOUT_SECONDS
        ) as smtp:
            smtp.starttls()
            smtp.login(smtp_config["user"], smtp_config["password"])
            smtp.send_message(email_message)
    except (smtplib.SMTPException, OSError, TimeoutError) as error:
        raise ReminderEmailError(
            "Could not send the contact notification email."
        ) from error
    return True
