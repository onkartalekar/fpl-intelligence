"""Shared primitives for this repo's dark-navy, table-based HTML notification emails.

Extracted from `scripts/send_deadline_reminder.py`'s issue #83 template (the first of these
emails) so the release-notes email (issue #190) doesn't duplicate the same palette/badge/
table-skeleton CSS a second time. `send_deadline_reminder.py` imports its color constants and
`_esc`/`_badge_html` helpers from here under their original names, so nothing about that
module's own public surface (or `tests/test_send_deadline_reminder.py`, which asserts against
`sdr._BADGE_INFO_BG` etc. directly) changed.

Every style here is an inline `style=""` attribute on nested `<table role="presentation">`
markup, never a `<style>` block, CSS custom property, or flexbox/grid rule -- that is the only
subset every major email client (including Outlook desktop) has rendered consistently for two
decades. Colors are literal hex values for the same reason: Outlook desktop does not support CSS
custom properties at all.
"""

import html as _html


# Literal hex badge colors. `roll` (green): a positive/free outcome -- banking a transfer, a
# zero-point-cost transfer, or (in the release-notes email) a shipped `Feature`. `hit` (red): a
# transfer that costs points -- reserved for genuine costs to the reader, deliberately *not*
# reused for the release-notes `Fix` category (a shipped fix is good news, not a cost). `info`
# (blue): a hold, an informational header, or a `Data` change. `amber`/`slate` were added for
# issue #190's release-notes email, which needs more than the three-color triad #83 defined.
BADGE_ROLL_BG, BADGE_ROLL_FG = "#164b3a", "#94efcb"
BADGE_HIT_BG, BADGE_HIT_FG = "#573040", "#ffc1cb"
BADGE_INFO_BG, BADGE_INFO_FG = "#203b59", "#b9dcff"
BADGE_AMBER_BG, BADGE_AMBER_FG = "#3a2f12", "#f0d98c"
BADGE_SLATE_BG, BADGE_SLATE_FG = "#26303f", "#b8c4d1"

EMAIL_BG = "#0d1b2a"
CARD_BG = "#13233a"
CARD_BORDER = "#28405c"
TEXT_PRIMARY = "#f4f7fb"
TEXT_MUTED = "#9fb0c3"


def esc(value):
    return _html.escape(str(value if value is not None else ""), quote=True)


def badge_html(label, bg, fg):
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0"><tr>'
        f'<td style="background:{bg};color:{fg};font-weight:bold;font-size:12px;'
        'padding:4px 10px;border-radius:4px;font-family:Arial,Helvetica,sans-serif;'
        f'letter-spacing:.3px">{esc(label)}</td></tr></table>'
    )


def shell(title, inner_html):
    """Wrap `inner_html` (already-built `<tr>` rows -- header, body, footer, all of it) in the
    600px dark-navy table skeleton every email built from this module shares: doctype/head/body,
    an outer full-width centering table, and an inner `max-width:600px` table that actually holds
    the content. `title` is escaped into the `<title>` (not shown to the reader; some clients
    surface it in a preview pane)."""
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)}</title></head>"
        f'<body style="margin:0;padding:0;background:{EMAIL_BG};'
        'font-family:Arial,Helvetica,sans-serif">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{EMAIL_BG}"><tr><td align="center">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:600px">'
        + inner_html
        + "</table></td></tr></table></body></html>"
    )
