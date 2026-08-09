"""Confirmation-email composition and SMTP send for the reminder opt-in double opt-in
(issue #79) -- see plans/issue-79-reminder-opt-in.md.

Deliberately separate from `scripts/send_deadline_reminder.py`'s own SMTP sending: this module
only ever sends one short "click to confirm" email, never the full reminder digest that script
owns, and runs synchronously inside the live server process (`server.py`'s
`_handle_reminder_opt_in`) -- a different exposure profile from that script's offline, trusted
GitHub Actions cron. That's why this module reads its own, separate `FPL_INTEL_SERVER_SMTP_*`
env vars rather than reusing `FPL_INTEL_SMTP_*`: independently rotatable credentials as a matter
of blast-radius hygiene, even though nothing stops an operator from pointing both at the same
mailbox in practice.

Follows `news_signals.py`'s fail-safe posture: env vars are read at call time (never cached at
import time), never logged, and any missing configuration or network/auth/SMTP failure is turned
into a single narrow `ReminderEmailError` -- never a bare `smtplib`/`OSError`, and never silently
swallowed into a `True`/`False` return, so the caller (`server.py`) always has exactly one
exception type to catch and turn into a clean, input-free error response. Per the plan, a failed
send must never result in a DB row referencing a confirmation token that was never actually
delivered -- `server.py` is responsible for sending before writing, never the reverse.
"""

from email.message import EmailMessage
import os
import smtplib


SMTP_HOST_ENV_VAR = "FPL_INTEL_SERVER_SMTP_HOST"
SMTP_PORT_ENV_VAR = "FPL_INTEL_SERVER_SMTP_PORT"
SMTP_USER_ENV_VAR = "FPL_INTEL_SERVER_SMTP_USER"
SMTP_PASSWORD_ENV_VAR = "FPL_INTEL_SERVER_SMTP_PASSWORD"

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
    for tests; real callers should omit it and let this read `FPL_INTEL_SERVER_SMTP_*` at call
    time.
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
