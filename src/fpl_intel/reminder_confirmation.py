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

from datetime import datetime
from email.message import EmailMessage
import os
import smtplib

from . import email_template
from .release_notes import CATEGORIES as _RELEASE_NOTES_CATEGORIES


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


def compose_release_notes_subscription_email(confirm_url):
    """Return (subject, body) for the "What's New" email-subscription confirmation (issue #143)
    -- content only. Same double-opt-in shape as `compose_confirmation_email` above: nothing is
    enabled until this link is clicked.
    """
    subject = "Confirm your FPL Intelligence release notes subscription"
    body = (
        "Someone (hopefully you) asked to receive FPL Intelligence's \"What's New\" release "
        "notes by email, one email each time a new entry publishes.\n\n"
        "Confirm by opening this link:\n"
        f"{confirm_url}\n\n"
        "If you didn't request this, you can ignore this email -- nothing is sent to this "
        "address until this link is clicked, and this link expires automatically if it isn't "
        "used.\n\n"
        "-- FPL Intelligence automated release-notes subscription (issue #143)"
    )
    return subject, body


def send_release_notes_subscription_email(to_email, confirm_url, smtp_config=None):
    """Send the release-notes subscription confirmation email. Same contract as
    `send_confirmation_email`: returns True on success, raises `ReminderEmailError` on any
    configuration or send failure."""
    smtp_config = smtp_config or _read_smtp_config()
    subject, body = compose_release_notes_subscription_email(confirm_url)
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
            "Could not send the subscription confirmation email. Try again shortly."
        ) from error
    return True


# ---------------------------------------------------------------------------------------------
# Release-notes HTML email body (issue #190). Reuses #83's dark-navy, table-based template
# (`email_template`, extracted from `scripts/send_deadline_reminder.py` for exactly this reuse)
# rather than inventing a new visual language -- see the reviewed mockup at
# `plans/assets/issue-190-release-notes-email-mockup.html`.
# ---------------------------------------------------------------------------------------------

# Category badge colors. `Feature` reuses `email_template`'s ROLL green and `Data` reuses its
# INFO blue -- both already mean "a good/informational outcome" in #83's palette, and the same
# reading holds here. `Fix` deliberately reuses the AMBER color from #83's stale-data notice, not
# the HIT red also available: a shipped fix is good news for the reader, not a cost the way a
# transfer hit is. `Docs` gets a new purple not part of #83's original three-color triad (#83
# never needed a fourth category); `Chore` reuses `email_template`'s SLATE, added for this email.
_CATEGORY_BADGE_COLORS = {
    "Feature": (email_template.BADGE_ROLL_BG, email_template.BADGE_ROLL_FG),
    "Fix": (email_template.BADGE_AMBER_BG, email_template.BADGE_AMBER_FG),
    "Data": (email_template.BADGE_INFO_BG, email_template.BADGE_INFO_FG),
    "Docs": ("#3a2f57", "#d9c8ff"),
    "Chore": (email_template.BADGE_SLATE_BG, email_template.BADGE_SLATE_FG),
}

# The two-section split decided during issue #190's mockup review: not every subscriber is a
# developer, so changes are grouped by who they're actually for. Both sections use the identical
# badge/card treatment -- this is an organizational split, not a visual-importance one (an
# earlier de-emphasized "Under the hood" pass read as "this section doesn't matter", which wasn't
# the intent). `Fix` is a known imperfect fit here -- the real 2026-08-15 sample entry's Fix
# changes mix genuinely user-visible fixes with purely internal ones, and a category-level split
# can't tell those apart (flagged as open question (b) in issue #190, not resolved by this pass).
_FOR_YOU_CATEGORIES = ("Feature", "Fix", "Data")
_UNDER_THE_HOOD_CATEGORIES = ("Docs", "Chore")
assert set(_FOR_YOU_CATEGORIES) | set(_UNDER_THE_HOOD_CATEGORIES) == set(_RELEASE_NOTES_CATEGORIES), (
    "every release_notes.CATEGORIES value must be assigned to exactly one email section"
)


def _change_card_row_html(change, is_last):
    border = "" if is_last else f"border-bottom:1px solid {email_template.CARD_BORDER};"
    return (
        f'<tr><td style="padding:14px 16px;{border}">'
        f'<p style="margin:0 0 3px;color:{email_template.TEXT_PRIMARY};font-size:14px;'
        f'font-weight:bold;font-family:Arial,Helvetica,sans-serif">'
        f'{email_template.esc(change["title"])}</p>'
        f'<p style="margin:0;color:{email_template.TEXT_MUTED};font-size:13px;line-height:1.5;'
        f'font-family:Arial,Helvetica,sans-serif">{email_template.esc(change["description"])}</p>'
        '</td></tr>'
    )


def _category_section_html(category, changes, top_padding):
    """One badge + one bordered card listing every change in `category`, matching #83's
    per-item-card style. `top_padding` is 12px for the first category card in a section (right
    below that section's own heading) and 20px between two category cards within the same
    section -- the same spacing #83's stacked profile cards use."""
    bg, fg = _CATEGORY_BADGE_COLORS[category]
    badge = email_template.badge_html(f"{category.upper()} · {len(changes)}", bg, fg)
    rows = "".join(
        _change_card_row_html(change, is_last=(index == len(changes) - 1))
        for index, change in enumerate(changes)
    )
    card = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{email_template.CARD_BG};border:1px solid {email_template.CARD_BORDER};'
        f'border-radius:8px">{rows}</table>'
    )
    return (
        f'<tr><td style="padding:{top_padding}px 16px 0 16px">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 10px">'
        f'<tr><td>{badge}</td></tr></table>{card}</td></tr>'
    )


def _section_html(heading_html, categories, changes_by_category, divider_above):
    """One "What's new for you"/"Under the hood" section: an optional divider, an uppercase
    heading, then one `_category_section_html` block per non-empty category in `categories`
    (`release_notes.CATEGORIES`' declared order). Returns "" if every category in this section
    is empty that day, so an all-Feature entry never renders an empty "Under the hood" section."""
    blocks = []
    top_padding = 12
    for category in categories:
        changes = changes_by_category.get(category)
        if not changes:
            continue
        blocks.append(_category_section_html(category, changes, top_padding))
        top_padding = 20
    if not blocks:
        return ""
    divider = (
        f'<tr><td style="padding:26px 16px 0 16px">'
        f'<div style="border-top:1px solid {email_template.CARD_BORDER}"></div></td></tr>'
        if divider_above else ""
    )
    heading = (
        f'<tr><td style="padding:20px 16px 0 16px">'
        f'<p style="margin:0;color:{email_template.TEXT_PRIMARY};font-size:13px;font-weight:bold;'
        'letter-spacing:.4px;font-family:Arial,Helvetica,sans-serif;text-transform:uppercase">'
        f'{heading_html}</p></td></tr>'
    )
    return divider + heading + "".join(blocks)


def _release_notes_footer_html(unsubscribe_url):
    return (
        f'<tr><td style="padding:20px 16px 24px;border-top:1px solid {email_template.CARD_BORDER}">'
        f'<p style="font-size:11px;color:{email_template.TEXT_MUTED};margin:16px 0 0;'
        'font-family:Arial,Helvetica,sans-serif;line-height:1.5">'
        "You're receiving this because you subscribed to FPL Intelligence's \"What's New\" "
        "updates. See the full history in the dashboard's What's New tab. "
        f'<a href="{email_template.esc(unsubscribe_url)}" style="color:{email_template.TEXT_MUTED};'
        'text-decoration:underline">Unsubscribe</a></p></td></tr>'
    )


def _format_release_notes_badge_date(date_iso):
    """`"2026-08-15"` -> `"SAT, AUG 15"`, matching the mockup's header-badge date format."""
    try:
        return datetime.strptime(date_iso, "%Y-%m-%d").strftime("%a, %b %-d").upper()
    except ValueError:
        return date_iso


def _compose_release_notes_html_body(entry, unsubscribe_url):
    """HTML counterpart to `compose_release_notes_email`'s plain-text body."""
    changes_by_category = {}
    for change in entry.get("changes", []):
        changes_by_category.setdefault(change["category"], []).append(change)

    header_badge = email_template.badge_html(
        f"WHAT'S NEW · {_format_release_notes_badge_date(entry['date'])}",
        email_template.BADGE_INFO_BG, email_template.BADGE_INFO_FG,
    )
    header_html = (
        f'<tr><td style="padding:16px 16px 0 16px">{header_badge}</td></tr>'
        f'<tr><td style="padding:14px 16px 0 16px">'
        f'<h1 style="margin:0;color:{email_template.TEXT_PRIMARY};font-size:20px;line-height:1.35;'
        f'font-family:Arial,Helvetica,sans-serif">{email_template.esc(entry["headline"])}</h1>'
        '</td></tr>'
        f'<tr><td style="padding:8px 16px 0 16px">'
        f'<p style="margin:0;color:{email_template.TEXT_MUTED};font-size:14px;line-height:1.55;'
        f'font-family:Arial,Helvetica,sans-serif">{email_template.esc(entry["summary"])}</p>'
        '</td></tr>'
        f'<tr><td style="padding:16px 16px 0 16px">'
        f'<div style="border-top:1px solid {email_template.CARD_BORDER}"></div></td></tr>'
    )

    for_you_html = _section_html(
        "What's new for you", _FOR_YOU_CATEGORIES, changes_by_category, divider_above=False,
    )
    under_the_hood_heading = (
        'Under the hood <span style="color:%s;font-weight:normal;text-transform:none;'
        'letter-spacing:normal">(for the developer in you)</span>' % email_template.TEXT_MUTED
    )
    under_the_hood_html = _section_html(
        under_the_hood_heading, _UNDER_THE_HOOD_CATEGORIES, changes_by_category, divider_above=True,
    )

    inner_html = (
        header_html + for_you_html + under_the_hood_html
        + _release_notes_footer_html(unsubscribe_url)
    )
    return email_template.shell("FPL Intelligence: What's New", inner_html)


def compose_release_notes_email(entry, unsubscribe_url):
    """Return (subject, text_body, html_body) for one release-notes entry, sent to every
    confirmed subscriber when the daily job publishes it (issue #143 first shipped this
    plain-text-only; issue #190 added the HTML alternative). Content only -- no send side effect.

    `text_body` is the plain-text `text/plain` fallback (sent first, per RFC 2046's part-ordering
    convention -- see `send_release_notes_email` below). `html_body` is the `text/html`
    alternative, splitting into a "What's new for you"/"Under the hood" section per issue #190's
    mockup review. Every change's category/title/description is included in both, plus an
    unsubscribe link every sent email carries (issue #143's plan doc: every notification email in
    this codebase already carries a way to stop receiving it)."""
    subject = f"FPL Intelligence: {entry['headline']}"
    lines = [entry["headline"], "", entry["summary"], ""]
    for change in entry["changes"]:
        lines.append(f"[{change['category']}] {change['title']}")
        lines.append(f"  {change['description']}")
        lines.append("")
    lines.append(f"See the full history in the dashboard's What's New tab.")
    lines.append("")
    lines.append(f"Unsubscribe: {unsubscribe_url}")
    lines.append("")
    lines.append("-- FPL Intelligence automated release notes (issue #143)")
    text_body = "\n".join(lines)
    html_body = _compose_release_notes_html_body(entry, unsubscribe_url)
    return subject, text_body, html_body


def send_release_notes_email(to_email, entry, unsubscribe_url, smtp_config=None):
    """Send one release-notes entry to one confirmed subscriber, as a `multipart/alternative`
    message -- plain text first, then the HTML alternative (issue #190), same `set_content()`
    then `add_alternative()` call order as #83's `send_deadline_reminder.py`'s `send_email()`
    (order matters: `set_content()` must run before `add_alternative()` for `text/plain` to end
    up the first/least-preferred part). Same contract as `send_confirmation_email`: returns True
    on success, raises `ReminderEmailError` on any configuration or send failure -- callers
    (`server.py`'s `_handle_release_notes`) treat a per-recipient failure as non-fatal to the
    overall publish, since the entry itself is already durably stored by the time sends begin
    (same "durability first, notification best-effort" posture as Contact Us's own durability
    backstop)."""
    smtp_config = smtp_config or _read_smtp_config()
    subject, text_body, html_body = compose_release_notes_email(entry, unsubscribe_url)
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp_config["user"]
    message["To"] = to_email
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    try:
        with smtplib.SMTP(
            smtp_config["host"], smtp_config["port"], timeout=_SEND_TIMEOUT_SECONDS
        ) as smtp:
            smtp.starttls()
            smtp.login(smtp_config["user"], smtp_config["password"])
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError, TimeoutError) as error:
        raise ReminderEmailError(
            "Could not send the release notes email."
        ) from error
    return True
