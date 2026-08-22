"""Tests for issue #240's fix: the deadline reminder email's Starting XI pitch diagram, rendered
server-side as a PNG (`fpl_intel.notifications.pitch_image`) and served at
`/api/reminder-pitch.png` (`server_handlers/reminder_pitch.py`), replacing the inline `<svg>`
Gmail's HTML sanitizer used to mangle into garbled unstyled text.
"""

import base64
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs
from urllib.request import urlopen

from PIL import Image, ImageDraw

from fpl_intel.notifications import pitch_image
from fpl_intel.server import create_server

_SAMPLE_XI = [
    {"id": 1, "name": "Watkins", "position_short": "FWD", "club_short": "AVL"},
    {"id": 2, "name": "B.Fernandes", "position_short": "MID", "club_short": "MUN"},
    {"id": 6, "name": "Guéhi", "position_short": "DEF", "club_short": "CRY"},
    {"id": 11, "name": "Raya", "position_short": "GKP", "club_short": "ARS"},
]


def _extract_d(query):
    """Pulls the `d` param out of a `build_query()` result the same way the real server route
    does (`server_handlers/reminder_pitch.py`'s `parse_qs(query_string).get("d")`) -- not a naive
    string split, since #248's `&v=<_RENDER_VERSION>` suffix means `d` is no longer the whole
    query string."""
    return parse_qs(query)["d"][0]


class PitchImageEncodeDecodeTests(unittest.TestCase):
    def test_round_trips_id_name_position_and_club(self):
        query = pitch_image.build_query(_SAMPLE_XI, captain_id=2)
        d_param = _extract_d(query)

        starting_xi, captain_id = pitch_image.decode_query(d_param)

        self.assertEqual(captain_id, 2)
        self.assertEqual(
            [(p["id"], p["name"], p["position_short"], p["club_short"]) for p in starting_xi],
            [(1, "Watkins", "FWD", "AVL"), (2, "B.Fernandes", "MID", "MUN"),
             (6, "Guéhi", "DEF", "CRY"), (11, "Raya", "GKP", "ARS")],
        )

    def test_falls_back_to_club_when_club_short_is_absent(self):
        """Mirrors the old `_pitch_svg()`'s `player.get("club_short") or player.get("club")`
        fallback -- not every caller's player dicts carry `club_short`."""
        query = pitch_image.build_query([{"id": 1, "name": "Watkins", "position_short": "FWD", "club": "AVL"}], None)
        starting_xi, _ = pitch_image.decode_query(_extract_d(query))
        self.assertEqual(starting_xi[0]["club_short"], "AVL")

    def test_empty_starting_xi_round_trips_to_an_empty_list(self):
        query = pitch_image.build_query([], None)
        starting_xi, captain_id = pitch_image.decode_query(_extract_d(query))
        self.assertEqual(starting_xi, [])
        self.assertIsNone(captain_id)

    def test_rejects_a_missing_d_param(self):
        with self.assertRaises(pitch_image.InvalidPitchQuery):
            pitch_image.decode_query(None)

    def test_rejects_an_oversized_d_param(self):
        with self.assertRaises(pitch_image.InvalidPitchQuery):
            pitch_image.decode_query("a" * 4001)

    def test_rejects_undecodable_base64(self):
        with self.assertRaises(pitch_image.InvalidPitchQuery):
            pitch_image.decode_query("not-valid-base64!!!")

    def test_rejects_a_payload_that_is_not_a_json_object(self):
        encoded = base64.urlsafe_b64encode(json.dumps([1, 2, 3]).encode()).decode()
        with self.assertRaises(pitch_image.InvalidPitchQuery):
            pitch_image.decode_query(encoded)

    def test_rejects_more_than_fifteen_players(self):
        oversized_xi = [
            {"id": i, "name": f"Player{i}", "position_short": "MID", "club_short": "AVL"}
            for i in range(16)
        ]
        query = pitch_image.build_query(oversized_xi, None)
        with self.assertRaises(pitch_image.InvalidPitchQuery):
            pitch_image.decode_query(_extract_d(query))

    def test_rejects_an_unknown_position(self):
        encoded = base64.urlsafe_b64encode(
            json.dumps({"xi": [[1, "Watkins", "STRIKER", "AVL"]], "cap": None}).encode()
        ).decode()
        with self.assertRaises(pitch_image.InvalidPitchQuery):
            pitch_image.decode_query(encoded)

    def test_truncates_overlong_name_and_club_fields(self):
        long_name = "A" * 500
        query = pitch_image.build_query(
            [{"id": 1, "name": long_name, "position_short": "FWD", "club_short": "AVL"}], None,
        )
        starting_xi, _ = pitch_image.decode_query(_extract_d(query))
        self.assertLessEqual(len(starting_xi[0]["name"]), 40)


class RenderVersionCacheBusterTests(unittest.TestCase):
    """Issue #248: `/api/reminder-pitch.png` is served with a 24h public Cache-Control, safe only
    as long as a given URL always renders the same bytes -- confirmed broken live across #245's
    deploy, when Gmail's own image proxy kept serving a pre-#245 cached copy of an unchanged
    starting XI's URL well after the origin had already shipped the fix. `_RENDER_VERSION` exists
    so a rendering-code change automatically changes the URL for an otherwise-identical starting
    XI, busting any such downstream cache -- and unlike a hand-maintained version counter (this
    module's first attempt, which required remembering to bump an integer on every rendering
    change -- exactly the forgettable step that caused #245's own staleness), it's derived
    automatically by `_compute_render_fingerprint()` from the actual constants/source that
    determine `render_png()`'s output, so there's nothing to remember."""

    def test_query_carries_the_current_render_version(self):
        query = pitch_image.build_query(_SAMPLE_XI, captain_id=2)
        self.assertEqual(parse_qs(query)["v"], [pitch_image._RENDER_VERSION])

    def test_render_version_matches_a_fresh_fingerprint_computation(self):
        self.assertEqual(pitch_image._RENDER_VERSION, pitch_image._compute_render_fingerprint())

    def test_changing_a_layout_constant_changes_the_fingerprint(self):
        """The actual regression test for issue #248's real fix: a rendering-affecting change
        automatically changes the cache-buster with no separate step, unlike the hand-maintained
        counter this replaced."""
        original = pitch_image._compute_render_fingerprint()
        with patch.object(pitch_image, "_BOX_GAP", pitch_image._BOX_GAP + 1):
            changed = pitch_image._compute_render_fingerprint()
        self.assertNotEqual(original, changed)

    def test_changing_a_color_constant_changes_the_fingerprint(self):
        original = pitch_image._compute_render_fingerprint()
        with patch.object(pitch_image, "_TURF", (0, 0, 0)):
            changed = pitch_image._compute_render_fingerprint()
        self.assertNotEqual(original, changed)

    def test_a_no_op_recomputation_is_stable(self):
        """Guards against the fingerprint accidentally depending on anything nondeterministic
        (e.g. object identity/memory addresses) -- two computations with nothing changed between
        them must agree, or the cache-buster would spuriously churn on every process restart."""
        self.assertEqual(
            pitch_image._compute_render_fingerprint(), pitch_image._compute_render_fingerprint(),
        )

    def test_different_render_version_produces_a_different_url_for_the_same_starting_xi(self):
        query_v1 = pitch_image.build_query(_SAMPLE_XI, captain_id=2)
        with patch.object(pitch_image, "_RENDER_VERSION", "a-different-fingerprint"):
            query_v2 = pitch_image.build_query(_SAMPLE_XI, captain_id=2)

        self.assertNotEqual(query_v1, query_v2)
        # The starting XI itself is unchanged -- only `v`, and therefore the `d` payload it's
        # concatenated after, differs; decoding either still recovers the same starting XI, so
        # `v`'s presence hasn't altered what the image actually contains.
        self.assertEqual(
            pitch_image.decode_query(_extract_d(query_v1)),
            pitch_image.decode_query(_extract_d(query_v2)),
        )


class BoxWidthForRowTests(unittest.TestCase):
    """Issue #245: the fixed 86px box width used to overlap for a 5-player row (400/5=80 < 86,
    confirmed in a real sent email -- the DEF row's five boxes touched/overlapped edge-to-edge)
    and left a 1-player row's box stranded in mostly-empty space. Box width is now a function of
    the row's own player count, clamped to `[_BOX_W_MIN, _BOX_W_MAX]` with a `_BOX_GAP` margin."""

    def test_five_player_row_leaves_a_visible_gap_instead_of_overlapping(self):
        box_w = pitch_image._box_width_for_row(5)
        cell_width = pitch_image._WIDTH / 5
        self.assertLess(box_w, cell_width)  # strictly less, not just <=, so a real gap remains

    def test_five_player_row_box_width_matches_the_expected_clamp_result(self):
        # 400/5 - 8 = 72, within [_BOX_W_MIN, _BOX_W_MAX] -- exercised directly, not just via <.
        self.assertEqual(pitch_image._box_width_for_row(5), 72)

    def test_one_player_row_grows_past_the_old_fixed_width_but_stays_capped(self):
        box_w = pitch_image._box_width_for_row(1)
        self.assertGreater(box_w, 86)  # the old fixed width -- confirms it actually grew
        self.assertEqual(box_w, pitch_image._BOX_W_MAX)  # capped, not sprawling across all 400px

    def test_box_width_never_drops_below_the_floor_for_an_unrealistically_packed_row(self):
        self.assertEqual(pitch_image._box_width_for_row(15), pitch_image._BOX_W_MIN)


class PitchImageRenderTests(unittest.TestCase):
    def test_renders_a_valid_png_for_a_full_starting_xi(self):
        png_bytes = pitch_image.render_png(_SAMPLE_XI, captain_id=2)
        self.assertTrue(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_renders_without_crashing_for_an_empty_starting_xi(self):
        png_bytes = pitch_image.render_png([], captain_id=None)
        self.assertTrue(png_bytes.startswith(b"\x89PNG"))

    def test_renders_an_accented_name_without_crashing(self):
        """Issue #240: Pillow's bundled default font lacks accented-glyph coverage, so accented
        names have to survive `fold_ascii()` before drawing rather than raising or tofu-boxing."""
        png_bytes = pitch_image.render_png(
            [{"id": 6, "name": "Guéhi", "position_short": "DEF", "club_short": "CRY"}], captain_id=6,
        )
        self.assertTrue(png_bytes.startswith(b"\x89PNG"))


class FitTextTests(unittest.TestCase):
    """Issue #240: `_fit_text()` was added after live verification showed a long captain name
    ("B.Fernandes (C)") overflowing past its box at the diagram's original fixed font size --
    inherited unchanged from the old SVG's own fixed `font-size="13"`, just far more visible on a
    filled raster box than it ever was in an SVG-rendering client."""

    def setUp(self):
        self.draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    def test_short_text_keeps_the_largest_candidate_size(self):
        font, text, bbox = pitch_image._fit_text(self.draw, "Raya", pitch_image._FONT_NAME_SIZES, 86)
        self.assertEqual(text, "Raya")
        self.assertLessEqual(bbox[2] - bbox[0], 86)

    def test_long_text_shrinks_rather_than_overflows(self):
        long_name = "B.Fernandes (C)"
        max_width = 86 - pitch_image._TEXT_MARGIN  # a representative box width, not tied to any particular row size

        font, text, bbox = pitch_image._fit_text(self.draw, long_name, pitch_image._FONT_NAME_SIZES, max_width)

        self.assertEqual(text, long_name)  # shrinking alone is enough here, no truncation needed
        self.assertLessEqual(bbox[2] - bbox[0], max_width)

    def test_text_too_long_for_any_size_is_truncated_with_an_ellipsis_and_still_fits(self):
        absurdly_long_name = "A" * 60 + " (C)"
        max_width = 86 - pitch_image._TEXT_MARGIN  # a representative box width, not tied to any particular row size

        font, text, bbox = pitch_image._fit_text(
            self.draw, absurdly_long_name, pitch_image._FONT_NAME_SIZES, max_width,
        )

        self.assertTrue(text.endswith("…"))
        self.assertLess(len(text), len(absurdly_long_name))
        self.assertLessEqual(bbox[2] - bbox[0], max_width)


class ReminderPitchEndpointTests(unittest.TestCase):
    """Issue #240: GET /api/reminder-pitch.png, mirroring test_server_team_lookup.py's
    SharedStateApiTests server-lifecycle pattern."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        self.server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def test_valid_query_returns_a_png_image(self):
        query = pitch_image.build_query(_SAMPLE_XI, captain_id=2)

        response = urlopen(f"{self.base_url}/api/reminder-pitch.png?{query}", timeout=3)

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get("Content-Type"), "image/png")
        self.assertTrue(response.read().startswith(b"\x89PNG"))

    def test_missing_d_param_returns_400(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/api/reminder-pitch.png", timeout=3)
        self.assertEqual(error.exception.code, 400)

    def test_malformed_d_param_returns_400(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/api/reminder-pitch.png?d=not-valid-base64!!!", timeout=3)
        self.assertEqual(error.exception.code, 400)

    def test_is_not_gated_by_the_operator_token(self):
        """Public and unauthenticated by design, same as /api/shared-state -- the endpoint is an
        <img src> a mail client fetches directly, which can never attach an X-Refresh-Token."""
        query = pitch_image.build_query(_SAMPLE_XI, captain_id=None)
        response = urlopen(f"{self.base_url}/api/reminder-pitch.png?{query}", timeout=3)
        self.assertEqual(response.status, 200)

    def test_ignores_v_and_still_renders_correctly_from_d_alone(self):
        """Issue #248: `v` only exists to change the URL string across a rendering-code change --
        the server route never reads it. A request carrying a stale/arbitrary `v` alongside a
        valid `d` must render exactly as if `v` were absent, since that's precisely what an
        already-sent email's URL (built under a lower `_RENDER_VERSION`, but never revisited by
        this test suite once shipped) needs to keep doing forever."""
        d_param = _extract_d(pitch_image.build_query(_SAMPLE_XI, captain_id=2))

        response_stale_v = urlopen(f"{self.base_url}/api/reminder-pitch.png?d={d_param}&v=0", timeout=3)
        response_no_v = urlopen(f"{self.base_url}/api/reminder-pitch.png?d={d_param}", timeout=3)

        self.assertEqual(response_stale_v.status, 200)
        self.assertEqual(response_stale_v.read(), response_no_v.read())


if __name__ == "__main__":
    unittest.main()
