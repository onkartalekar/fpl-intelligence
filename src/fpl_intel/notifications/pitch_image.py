"""Pitch-diagram image rendering for the deadline reminder email (issue #240).

Issue #83 originally rendered the "Recommended Starting XI" section as inline `<svg>`: safe in
Apple Mail/Yahoo, but silently broken in Gmail. Gmail's HTML sanitizer strips `<svg>`/`<rect>`/
`<text>` tags (elements it doesn't allow in email bodies) but does not remove their text content,
so the diagram degraded to an unstyled, unspaced run of concatenated player names and club codes
where the pitch should be (confirmed by direct repro -- see issue #240). Outlook desktop had an
`<!--[if mso]-->`-gated placeholder for its own, older SVG gap, but there is no markup equivalent
of MSO conditional comments to target "Gmail specifically" -- MSO comments exploit a real
Word-rendering-engine parser quirk, and Gmail's sanitizer has no comparable hook.

The fix is to stop relying on inline SVG at all: render the diagram server-side as a PNG and
serve it as a plain `<img>`, the one image format every mainstream client (Gmail included)
reliably displays. This module is shared by both ends of that round trip:

- `scripts/send_deadline_reminder.py` calls `build_query()` at compose time, turning a starting
  XI + captain into a URL query string, then embeds `<img src="{base_url}/api/reminder-pitch.png?{query}">`.
- `src/fpl_intel/server.py` (via `server_handlers/reminder_pitch.py`) calls `decode_query()` then
  `render_png()` when that `<img>` tag is actually fetched -- which can happen days after send,
  whenever the recipient opens the email, potentially after that gameweek's real recommendation
  has already moved on. So the query string has to be a complete, self-contained snapshot of what
  to draw, not a lookup key into today's state -- the same reasoning `_dashboard_base_url()`'s
  callers already apply to the rest of this email's content.

Adds `Pillow` as this repo's second deliberate exception to the stdlib-only policy (the first is
`numpy`, see `requirements.txt`) -- there is no stdlib way to rasterize shapes/text into an image,
and a hand-rolled `zlib`/`struct`-only PNG encoder plus a hardcoded pixel font (the stdlib-only
alternative, considered and rejected in issue #240) would be substantially more code and more
surface for rendering bugs, for a cosmetic diagram.

Text is drawn with Pillow's own bundled default font (`ImageFont.load_default(size=...)`, which
ships inside the Pillow package itself -- no system-font path is read, so this renders
identically whether it runs on a developer's laptop or Railway's container, which has no
guaranteed font install). That bundled font's glyph coverage does not include accented Latin
letters, though -- confirmed directly: "Guéhi" renders its "é" as a tofu box. Names are folded
through `text_fold.fold_ascii()` before drawing (the same Unicode-folding this repo already uses
for player-name search, issue #239) so the image always gets plain-ASCII glyphs it can actually
render; the HTML side of the email is unaffected and keeps full accented names.
"""

import base64
import io
import json

from PIL import Image, ImageDraw, ImageFont

from ..text_fold import fold_ascii

# Row order top-to-bottom mirrors dashboard.js's weeklyPitch()/pitch() grouping exactly
# (`['FWD','MID','DEF','GKP'].map(...)` inside a `flex-direction: column` container renders FWD
# first/top, GKP last/bottom) -- see dashboard.js. Carried over unchanged from the SVG version
# this replaces (previously `_PITCH_ROW_ORDER` in scripts/send_deadline_reminder.py).
PITCH_ROW_ORDER = ["FWD", "MID", "DEF", "GKP"]

_WIDTH, _HEIGHT = 400, 500
_BOX_H = 56

# Issue #245: box width used to be a fixed `_BOX_W = 86` regardless of how many players shared a
# row -- with 5 players, `cell_width` (400/5=80) was already narrower than the box itself, so
# adjacent boxes overlapped by ~6px each side (confirmed in a real sent email: the DEF row's five
# boxes touched/overlapped edge-to-edge, reading as one continuous band, not five cards); with 1
# player (a lone FWD, or GKP's row, always exactly 1), the box stayed 86px in the middle of 400px
# of otherwise-empty turf. Box width is now computed per row from that row's own `cell_width`
# (see `render_png`'s row loop), clamped to this range: `_BOX_GAP` keeps a visible gap between
# adjacent boxes at the packed end (a 5-wide row: 400/5-8=72px boxes, no overlap); `_BOX_W_MIN`
# floors that same packed case so boxes still read as cards, not slivers; `_BOX_W_MAX` caps the
# sparse end (a 1-wide row) so a lone box grows to use its row better without sprawling absurdly
# wide across all 400px.
_BOX_GAP, _BOX_W_MIN, _BOX_W_MAX = 8, 60, 140

# Same literal colors the SVG version used (send_deadline_reminder.py's old `_pitch_svg`), just
# as RGB tuples instead of hex strings -- Pillow's draw calls take either, tuples read slightly
# clearer next to the RGB math below.
_TURF, _HALFWAY_LINE = (11, 61, 36), (31, 92, 63)
_BOX_FILL, _CAPTAIN_FILL, _CAPTAIN_OUTLINE = (31, 92, 63), (11, 61, 36), (148, 239, 203)
_NAME_FILL, _CLUB_FILL = (255, 255, 255), (201, 232, 216)

# Descending candidate sizes, tried largest-first until the text fits its (per-row, since #245)
# box width -- a fixed size alone (13/11, the old SVG's own `font-size="13"`/`"11"`) let a long
# name overflow past its box,
# confirmed live: "B.Fernandes (C)" spilled off the image's left edge. The SVG version had the
# identical risk (same box width, same fixed font-size, and SVG doesn't clip overflowing text by
# default either) -- just less visible there than on a filled raster box.
_FONT_NAME_SIZES = range(13, 8, -1)
_FONT_CLUB_SIZES = range(11, 7, -1)
_TEXT_MARGIN = 8

# This is a public, unauthenticated GET endpoint (an <img src>, not a signed link) -- these bound
# how much the query string can make the server do, independent of who's asking or why.
_MAX_PLAYERS = 15
_MAX_FIELD_LEN = 40


def build_query(starting_xi, captain_id):
    """Encode `starting_xi`/`captain_id` (the same inputs the old `_pitch_svg()` took) into a
    URL query string for the `/api/reminder-pitch.png` endpoint. A single `d` param carrying the
    whole payload, not one query param per player/field -- keeps the URL one opaque,
    self-contained snapshot rather than several separately-tamperable pieces.
    """
    payload = {
        "xi": [
            [
                player.get("id"),
                player.get("name") or "",
                player.get("position_short") or "",
                player.get("club_short") or player.get("club") or "",
            ]
            for player in (starting_xi or [])
        ],
        "cap": captain_id,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return "d=" + encoded.decode("ascii")


class InvalidPitchQuery(ValueError):
    """Raised by `decode_query()` for a malformed, oversized, or out-of-bounds `d` param."""


def decode_query(d_param):
    """Inverse of `build_query()`. Raises `InvalidPitchQuery` (never guesses at a best-effort
    partial render) for anything that doesn't decode to the expected shape -- the caller
    (`server_handlers/reminder_pitch.py`) turns that into a plain 400, matching how this server's
    other public GET endpoints already handle malformed input (e.g. `team_lookup.py`'s
    `parse_team_id`).
    """
    if not d_param or len(d_param) > 4000:
        raise InvalidPitchQuery("missing or oversized 'd' parameter")
    try:
        payload = json.loads(base64.urlsafe_b64decode(d_param.encode("ascii")))
    except Exception as error:
        raise InvalidPitchQuery(f"undecodable 'd' parameter: {error!r}") from error
    if not isinstance(payload, dict):
        raise InvalidPitchQuery("'d' must decode to a JSON object")
    xi_raw = payload.get("xi")
    if not isinstance(xi_raw, list) or len(xi_raw) > _MAX_PLAYERS:
        raise InvalidPitchQuery(f"'xi' must be a list of at most {_MAX_PLAYERS} players")
    starting_xi = []
    for entry in xi_raw:
        if not (isinstance(entry, list) and len(entry) == 4):
            raise InvalidPitchQuery("each xi entry must be [id, name, position_short, club]")
        player_id, name, position_short, club = entry
        if position_short not in PITCH_ROW_ORDER:
            raise InvalidPitchQuery(f"unknown position_short {position_short!r}")
        if not isinstance(name, str) or not isinstance(club, str):
            raise InvalidPitchQuery("name/club must be strings")
        starting_xi.append({
            "id": player_id,
            "name": name[:_MAX_FIELD_LEN],
            "position_short": position_short,
            "club_short": club[:_MAX_FIELD_LEN],
        })
    captain_id = payload.get("cap")
    return starting_xi, captain_id


def _fit_text(draw, text, font_sizes, max_width):
    """Returns `(font, display_text, bbox)` for the largest size in `font_sizes` (descending)
    that fits `text` within `max_width`. Falls back to truncating `text` with an ellipsis at the
    smallest size for a name too long to ever fit -- rare (a fifteen-plus-character surname with
    the captain's " (C)" suffix appended), but better than letting it run off the image edge.
    """
    for size in font_sizes:
        font = ImageFont.load_default(size=size)
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_width:
            return font, text, bbox
    font = ImageFont.load_default(size=font_sizes[-1])
    truncated = text
    while len(truncated) > 1:
        truncated = truncated[:-1]
        candidate = truncated.rstrip() + "…"
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width:
            return font, candidate, bbox
    return font, text, draw.textbbox((0, 0), text, font=font)


def _box_width_for_row(player_count):
    """Issue #245: box width for a row of `player_count` players, sized to fit within that row's
    own `cell_width` (`_WIDTH / player_count`) with a visible gap between adjacent boxes, rather
    than the fixed 86px every row used to get regardless of density -- which let a 5-wide row's
    boxes overlap (400/5=80 < 86) and left a 1-wide row's box stranded in mostly-empty space.
    """
    cell_width = _WIDTH / player_count
    return max(_BOX_W_MIN, min(_BOX_W_MAX, cell_width - _BOX_GAP))


def render_png(starting_xi, captain_id):
    """Draw the pitch diagram and return PNG bytes. Same row-by-position grouping/layout math as
    the SVG version this replaces (`_pitch_svg()`, removed by issue #240): four y-bands
    (`PITCH_ROW_ORDER`, FWD/MID/DEF/GKP top to bottom), players spread evenly along x within each
    band, captain shown as an outlined rather than filled box.
    """
    img = Image.new("RGB", (_WIDTH, _HEIGHT), _TURF)
    draw = ImageDraw.Draw(img)
    draw.line([(0, _HEIGHT / 2), (_WIDTH, _HEIGHT / 2)], fill=_HALFWAY_LINE, width=2)
    draw.ellipse(
        [_WIDTH / 2 - 45, _HEIGHT / 2 - 45, _WIDTH / 2 + 45, _HEIGHT / 2 + 45],
        outline=_HALFWAY_LINE, width=2,
    )

    row_height = _HEIGHT / len(PITCH_ROW_ORDER)
    for row_index, position in enumerate(PITCH_ROW_ORDER):
        players = [player for player in starting_xi if player.get("position_short") == position]
        if not players:
            continue
        row_center_y = row_height * row_index + row_height / 2
        cell_width = _WIDTH / len(players)
        box_w = _box_width_for_row(len(players))
        max_text_width = box_w - _TEXT_MARGIN
        for player_index, player in enumerate(players):
            cx = cell_width * player_index + cell_width / 2
            x0, y0 = cx - box_w / 2, row_center_y - _BOX_H / 2
            x1, y1 = cx + box_w / 2, row_center_y + _BOX_H / 2
            is_captain = player.get("id") == captain_id
            name = fold_ascii(player.get("name") or "")
            if is_captain:
                name = f"{name} (C)"
                draw.rounded_rectangle(
                    [x0, y0, x1, y1], radius=6,
                    fill=_CAPTAIN_FILL, outline=_CAPTAIN_OUTLINE, width=3,
                )
            else:
                draw.rounded_rectangle([x0, y0, x1, y1], radius=6, fill=_BOX_FILL)
            club = fold_ascii(player.get("club_short") or "")
            name_font, name_text, name_box = _fit_text(draw, name, _FONT_NAME_SIZES, max_text_width)
            draw.text(
                (cx - (name_box[2] - name_box[0]) / 2, y0 + 8), name_text,
                font=name_font, fill=_NAME_FILL,
            )
            club_font, club_text, club_box = _fit_text(draw, club, _FONT_CLUB_SIZES, max_text_width)
            draw.text(
                (cx - (club_box[2] - club_box[0]) / 2, y0 + 26), club_text,
                font=club_font, fill=_CLUB_FILL,
            )

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()
