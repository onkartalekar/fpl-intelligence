"""Release-notes email composition and SMTP send (issue #143's subscription confirmation, issue
#190's HTML digest) -- split out of `reminder_confirmation.py`, where this code originally lived.

It landed in `reminder_confirmation.py` first because issue #143's plan doc said the subscription
confirmation should match "`reminder_confirmation.py`'s existing pattern" (issue #79's double
opt-in), and reusing that module's `_read_smtp_config()`/`ReminderEmailError`/
`_SEND_TIMEOUT_SECONDS` avoided a second copy of that plumbing -- a reasonable call when this was
still two short functions. Issue #190 then added a genuinely separate concern on top: a ~200-line
HTML-templating engine for `release_notes.py`'s data model (category badges, the "for you"/"under
the hood" section split, card layout) that has nothing to do with confirmation-email plumbing and
made `reminder_confirmation.py` mostly *this* by line count. This module is that code moved back
out, still importing `reminder_confirmation`'s `_read_smtp_config`/`ReminderEmailError`/
`_SEND_TIMEOUT_SECONDS` rather than duplicating them -- the original reuse rationale still holds,
it just no longer justifies living in the *same file* as the opt-in/Contact Us emails.

Reuses `email_template` (extracted from `scripts/send_deadline_reminder.py` for issue #83's
dark-navy, table-based template) rather than inventing a new visual language for the HTML body --
see the reviewed mockup at `plans/assets/issue-190-release-notes-email-mockup.html`.
"""

from datetime import datetime
from email.message import EmailMessage
import smtplib

from . import email_template
from .release_notes import CATEGORIES as _RELEASE_NOTES_CATEGORIES
from .reminder_confirmation import ReminderEmailError, _read_smtp_config, _SEND_TIMEOUT_SECONDS


def compose_release_notes_subscription_email(confirm_url):
    """Return (subject, body) for the "What's New" email-subscription confirmation (issue #143)
    -- content only. Same double-opt-in shape as `reminder_confirmation.compose_confirmation_
    email`: nothing is enabled until this link is clicked.
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
    `reminder_confirmation.send_confirmation_email`: returns True on success, raises
    `ReminderEmailError` on any configuration or send failure."""
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
# the intent).
#
# Issue #196: originally this was a static `category` -> section lookup table (`Fix` always
# "for you", `Chore` always "under the hood"), which misrouted real changes -- confirmed live on
# the 2026-08-15 entry, whose `Fix` changes mixed genuinely user-visible ones with purely internal
# ones, both routed into "for you" because the split keyed on category, not the individual
# change. The split now keys on each change's own `audience` field (`release_notes.AUDIENCES`)
# instead -- see `_compose_release_notes_html_body` below, which partitions changes by `audience`
# before ever grouping by `category`.


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


def _section_html(heading_html, changes_by_category, divider_above):
    """One "What's new for you"/"Under the hood" section: an optional divider, an uppercase
    heading, then one `_category_section_html` block per non-empty category present in
    `changes_by_category` (already filtered to this section's `audience` by the caller),
    iterated in `release_notes.CATEGORIES`' declared order. Returns "" if `changes_by_category`
    is empty, so e.g. an all-`user`-audience entry never renders an empty "Under the hood"
    section."""
    blocks = []
    top_padding = 12
    for category in _RELEASE_NOTES_CATEGORIES:
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
    for_you_by_category = {}
    under_the_hood_by_category = {}
    for change in entry.get("changes", []):
        # `.get(..., "user")` rather than a hard requirement: entries published before issue
        # #196 (all of history so far, by explicit decision -- not worth backfilling) never
        # carry `audience` at all. Defaulting a missing field to "user" is a trivial safety net
        # against a KeyError, not an attempt to correctly reclassify old entries.
        bucket = for_you_by_category if change.get("audience", "user") == "user" else under_the_hood_by_category
        bucket.setdefault(change["category"], []).append(change)

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
        "What's new for you", for_you_by_category, divider_above=False,
    )
    under_the_hood_heading = (
        'Under the hood <span style="color:%s;font-weight:normal;text-transform:none;'
        'letter-spacing:normal">(for the developer in you)</span>' % email_template.TEXT_MUTED
    )
    under_the_hood_html = _section_html(
        under_the_hood_heading, under_the_hood_by_category, divider_above=True,
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
    up the first/least-preferred part). Same contract as `reminder_confirmation.send_confirmation
    _email`: returns True on success, raises `ReminderEmailError` on any configuration or send
    failure -- callers (`server.py`'s `_handle_release_notes`) treat a per-recipient failure as
    non-fatal to the overall publish, since the entry itself is already durably stored by the
    time sends begin (same "durability first, notification best-effort" posture as Contact Us's
    own durability backstop)."""
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
