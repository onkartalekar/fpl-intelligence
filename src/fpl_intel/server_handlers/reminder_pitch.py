"""GET /api/reminder-pitch.png (issue #240): renders the deadline reminder email's Starting XI
pitch diagram as a real PNG, replacing the inline `<svg>` Gmail's HTML sanitizer used to mangle
into garbled unstyled text. See `notifications/pitch_image.py`'s module docstring for the full
mechanism and why this has to be a real rendered image rather than another markup workaround.

Deliberately not a `make_handle_...(...)` factory like this package's other handlers -- there is
nothing to close over. The endpoint is a pure function of its own `d` query parameter: the
starting XI + captain are encoded into the URL at email-compose time
(`send_deadline_reminder.py`'s `pitch_image.build_query()`), not looked up by team/event, because
a recipient's mail client fetches this image whenever the email is opened -- which can be days
after send, after that gameweek's real recommendation has moved on.
"""

from urllib.parse import parse_qs

from ..notifications import pitch_image


def handle_reminder_pitch(self, query_string):
    # `v` (pitch_image._RENDER_VERSION, issue #248) deliberately never gets read here -- its only
    # job is making the URL string itself change across a rendering-code update, so a client/
    # proxy cache holding an older `v`'s response never gets asked for this one. Reading it would
    # add nothing: `d` alone is already everything render_png() needs.
    d_param = (parse_qs(query_string).get("d") or [None])[0]
    try:
        starting_xi, captain_id = pitch_image.decode_query(d_param)
    except pitch_image.InvalidPitchQuery as error:
        self._json(400, {"status": "error", "message": f"Invalid pitch diagram request: {error}"})
        return
    png_bytes = pitch_image.render_png(starting_xi, captain_id)
    # Deterministic and cacheable like the other `_send_static` bodies (favicon, robots.txt) --
    # identical `d` always renders identical bytes *for a given version of this code* -- issue
    # #248: that's not automatically true across a rendering-code change, which is what `v`
    # above exists to guard against. Same day-long Cache-Control as those bodies is otherwise
    # just as safe here, even though (unlike those) this body is generated per request rather
    # than chosen once at process start.
    self._send_static(png_bytes, "image/png")
