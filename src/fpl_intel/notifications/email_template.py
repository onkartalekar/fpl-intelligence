"""Shared primitives for this repo's table-based HTML notification emails.

Extracted from `scripts/send_deadline_reminder.py`'s issue #83 template (the first of these
emails) so the release-notes email (issue #190) doesn't duplicate the same palette/badge/
table-skeleton CSS a second time. `send_deadline_reminder.py` imports its color constants and
`_esc`/`_badge_html` helpers from here under their original names, so nothing about that
module's own public surface (or `tests/test_send_deadline_reminder.py`, which asserts against
`sdr._BADGE_INFO_BG` etc. directly) changed.

Every style here is an inline `style=""` attribute on nested `<table role="presentation">`
markup, never a layout `<style>` block, CSS custom property, or flexbox/grid rule -- that is the
only subset every major email client (including Outlook desktop) has rendered consistently for
two decades. Colors are literal hex values inline for the same reason: Outlook desktop does not
support CSS custom properties at all.

Issue #197: these emails used to be dark-only (one navy palette, `content="dark"` on both meta
tags). They now adapt to the reader's own theme via `@media (prefers-color-scheme: light)`
overrides in `shell()`'s `<head>` -- a genuinely different mechanism from the dashboard's
`:root`/`[data-theme]` custom-property system (`dashboard.css`), since inline styles can't be
handed a custom property Outlook would understand. Each element that needs to theme carries both
its literal dark `style=""` (the default, and Outlook desktop's permanent rendering, since it
doesn't support `prefers-color-scheme` at all -- issue #197 explicitly leaves that unaddressed)
*and* a `class=""` naming which token it uses; the `<style>` block below re-targets that class
with `!important` only inside the light media query, leaving every other client's `style=""`
untouched. The light values themselves aren't a naive inversion -- they mirror the closest
semantic token already designed and shipped in `dashboard.css`'s own `:root[data-theme="light"]`
block, so the emails and the dashboard read as the same brand rather than two different guesses
at "light mode."
"""

import html as _html


# Literal hex badge colors. `roll` (green): a positive/free outcome -- banking a transfer, a
# zero-point-cost transfer, or (in the release-notes email) a shipped `Feature`. `hit` (red): a
# transfer that costs points -- reserved for genuine costs to the reader, deliberately *not*
# reused for the release-notes `Fix` category (a shipped fix is good news, not a cost). `info`
# (blue): a hold, an informational header, or a `Data` change. `amber`/`slate` were added for
# issue #190's release-notes email, which needs more than the three-color triad #83 defined;
# `docs` (purple) likewise -- issue #197 moved it here from `release_notes_email.py`'s own
# `_CATEGORY_BADGE_COLORS`, alongside its new light counterpart, rather than leaving one category
# color out of the shared light-mode `<style>` block this module now owns.
#
# Each dark value's `_LIGHT` counterpart mirrors the closest-matching token in `dashboard.css`'s
# `:root[data-theme="light"]` block (roll/hit/info are exact matches to that file's
# `--badge-ready`/`--chip-hard`/`--badge-info` pairs) -- see this module's docstring.
BADGE_ROLL_BG, BADGE_ROLL_FG = "#164b3a", "#94efcb"
BADGE_ROLL_BG_LIGHT, BADGE_ROLL_FG_LIGHT = "#d3f3e4", "#0d6b46"
BADGE_HIT_BG, BADGE_HIT_FG = "#573040", "#ffc1cb"
BADGE_HIT_BG_LIGHT, BADGE_HIT_FG_LIGHT = "#fbdde2", "#8a2036"
BADGE_INFO_BG, BADGE_INFO_FG = "#203b59", "#b9dcff"
BADGE_INFO_BG_LIGHT, BADGE_INFO_FG_LIGHT = "#dbe9fb", "#164a86"
BADGE_AMBER_BG, BADGE_AMBER_FG = "#3a2f12", "#f0d98c"
BADGE_AMBER_BG_LIGHT, BADGE_AMBER_FG_LIGHT = "#fdecc8", "#6b4a06"
BADGE_SLATE_BG, BADGE_SLATE_FG = "#26303f", "#b8c4d1"
BADGE_SLATE_BG_LIGHT, BADGE_SLATE_FG_LIGHT = "#e6e9f0", "#485064"
BADGE_DOCS_BG, BADGE_DOCS_FG = "#3a2f57", "#d9c8ff"
BADGE_DOCS_BG_LIGHT, BADGE_DOCS_FG_LIGHT = "#ece4fb", "#5b3fa0"

EMAIL_BG = "#0d1b2a"
EMAIL_BG_LIGHT = "#eef1f8"
CARD_BG = "#13233a"
CARD_BG_LIGHT = "#ffffff"
CARD_BORDER = "#28405c"
CARD_BORDER_LIGHT = "#c7d0e0"
TEXT_PRIMARY = "#f4f7fb"
TEXT_PRIMARY_LIGHT = "#141c2c"
TEXT_MUTED = "#9fb0c3"
TEXT_MUTED_LIGHT = "#54617a"

# A subtly-raised row background distinct from CARD_BG -- send_deadline_reminder.py's transfer
# rows. Mirrors dashboard.css's `--surface-inset` (the same "one step up from the panel"
# role there).
SURFACE_INSET_BG = "#1a2c42"
SURFACE_INSET_BG_LIGHT = "#f3f6fc"

# The stale-data notice's border in send_deadline_reminder.py -- a mid-tone between
# BADGE_AMBER_BG and BADGE_AMBER_FG, on both the dark and light sides.
AMBER_NOTE_BORDER = "#6b5420"
AMBER_NOTE_BORDER_LIGHT = "#e3c48a"

# Every (class name -> light-mode CSS) pair the emails built from this module use. Each entry
# names the *inline* property it overrides, matching whatever that class's elements actually set
# inline (e.g. a `border-bottom` element still only needs `border-color` here -- CSS lets a color
# override apply to a shorthand set on any side). `!important` is required: without it, this
# stylesheet rule (equal-or-lower specificity, and always earlier in document order than an
# element's own `style=""`) would never beat the inline dark default.
_LIGHT_MODE_RULES = {
    "email-bg": f"background-color:{EMAIL_BG_LIGHT} !important",
    "card-bg": f"background-color:{CARD_BG_LIGHT} !important",
    "card-border": f"border-color:{CARD_BORDER_LIGHT} !important",
    "text-primary": f"color:{TEXT_PRIMARY_LIGHT} !important",
    "text-muted": f"color:{TEXT_MUTED_LIGHT} !important",
    "surface-inset": f"background-color:{SURFACE_INSET_BG_LIGHT} !important",
    "amber-note-border": f"border-color:{AMBER_NOTE_BORDER_LIGHT} !important",
    "badge-roll": f"background-color:{BADGE_ROLL_BG_LIGHT} !important;color:{BADGE_ROLL_FG_LIGHT} !important",
    "badge-roll-fg": f"color:{BADGE_ROLL_FG_LIGHT} !important",
    "badge-hit": f"background-color:{BADGE_HIT_BG_LIGHT} !important;color:{BADGE_HIT_FG_LIGHT} !important",
    "badge-hit-fg": f"color:{BADGE_HIT_FG_LIGHT} !important",
    "badge-info": f"background-color:{BADGE_INFO_BG_LIGHT} !important;color:{BADGE_INFO_FG_LIGHT} !important",
    "badge-amber": f"background-color:{BADGE_AMBER_BG_LIGHT} !important;color:{BADGE_AMBER_FG_LIGHT} !important",
    "badge-slate": f"background-color:{BADGE_SLATE_BG_LIGHT} !important;color:{BADGE_SLATE_FG_LIGHT} !important",
    "badge-docs": f"background-color:{BADGE_DOCS_BG_LIGHT} !important;color:{BADGE_DOCS_FG_LIGHT} !important",
}

_BADGE_VARIANTS = {
    "roll": (BADGE_ROLL_BG, BADGE_ROLL_FG),
    "hit": (BADGE_HIT_BG, BADGE_HIT_FG),
    "info": (BADGE_INFO_BG, BADGE_INFO_FG),
    "amber": (BADGE_AMBER_BG, BADGE_AMBER_FG),
    "slate": (BADGE_SLATE_BG, BADGE_SLATE_FG),
    "docs": (BADGE_DOCS_BG, BADGE_DOCS_FG),
}


def esc(value):
    return _html.escape(str(value if value is not None else ""), quote=True)


def badge_html(label, variant):
    """A pill badge in one of `_BADGE_VARIANTS`' colors, themed via the matching `badge-{variant}`
    class. `variant` replaces the old direct `(bg, fg)` pair every caller used to pass -- every
    badge color this module knows about already has a name, and passing that name instead of its
    literal hex values is what lets this function attach the right light-mode class on its own,
    rather than every caller having to also know and pass a class name alongside its colors."""
    bg, fg = _BADGE_VARIANTS[variant]
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0"><tr>'
        f'<td class="badge-{variant}" style="background:{bg};color:{fg};font-weight:bold;'
        'font-size:12px;padding:4px 10px;border-radius:4px;font-family:Arial,Helvetica,sans-serif;'
        f'letter-spacing:.3px">{esc(label)}</td></tr></table>'
    )


def _light_mode_style_block():
    rules = "".join(f".{name}{{{css}}}" for name, css in _LIGHT_MODE_RULES.items())
    return f"<style>@media (prefers-color-scheme:light){{{rules}}}</style>"


def shell(title, inner_html):
    """Wrap `inner_html` (already-built `<tr>` rows -- header, body, footer, all of it) in the
    600px table skeleton every email built from this module shares: doctype/head/body, an outer
    full-width centering table, and an inner `max-width:600px` table that actually holds the
    content. `title` is escaped into the `<title>` (not shown to the reader; some clients surface
    it in a preview pane).

    The `color-scheme`/`supported-color-schemes` meta tags declare `"light dark"` (issue #197 --
    previously `"dark"` only, before a light palette existed). Confirmed live: without at least
    one of these values, some mobile mail apps (Gmail's app in particular) apply their own
    automatic dark-mode reprocessing to a message when the OS is in dark mode, and -- with no
    signal that this email already handles its own theming -- can relight what these inline
    styles/light-mode `<style>` overrides together already produce, while desktop clients
    (which generally trust inline styles and `<style>` blocks as authored) render the same email
    correctly either way. Declaring both means "this email adapts itself -- don't reprocess it,"
    the same signal `"dark"` alone gave before, just no longer limited to one theme.
    """
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="supported-color-schemes" content="light dark">'
        f"<title>{esc(title)}</title>"
        + _light_mode_style_block()
        + "</head>"
        f'<body class="email-bg" style="margin:0;padding:0;background:{EMAIL_BG};'
        'font-family:Arial,Helvetica,sans-serif">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'class="email-bg" style="background:{EMAIL_BG}"><tr><td align="center">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:600px">'
        + inner_html
        + "</table></td></tr></table></body></html>"
    )
