from html.parser import HTMLParser
import json
import re
import unittest

from fpl_intel.dashboard import render_dashboard

# Issue #222: dashboard.js went from a hand-minified single-line-per-function file to real,
# Prettier-formatted multi-line JS with its own quote-style conventions (double quotes by
# default, spaces around operators/braces). A long tail of tests below were written as exact
# substring/offset lookups against the old minified text -- those need to keep checking the same
# *semantic* JS content without being coupled to either formatting style. `_js_pattern`/
# `js_search`/`js_contains` do that: they tokenize a snippet into identifiers/numbers/punctuation
# and glue the pieces back together with `\s*`, so the same snippet string matches the code
# regardless of how much whitespace a formatter put between tokens, and quote characters match
# either `'` or `"`. Mirrors the whitespace/quote-tolerant regex migration already done for the
# CSS-side assertions when dashboard.css was reformatted (commit 5108610).
_JS_TOKEN_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*|[0-9]+|['\"]|\S")


def _js_pattern(snippet):
    tokens = _JS_TOKEN_RE.findall(snippet)
    parts = []
    for index, token in enumerate(tokens):
        if index > 0:
            # Prettier adds a trailing comma before a closing ), ], or } when it reflows a
            # call/array/object literal onto multiple lines -- optional here since the snippet
            # was written against the old single-line minified text, which never had one.
            parts.append(r",?\s*" if token in (")", "]", "}") else r"\s*")
        parts.append(r"['\"]" if token in ("'", '"') else re.escape(token))
    return "".join(parts)


def js_search(html, snippet, start=0):
    """Whitespace/quote-tolerant equivalent of `html.index(snippet, start)` for JS source
    snippets. Raises ValueError like str.index() does when the snippet isn't found."""
    match = re.compile(_js_pattern(snippet)).search(html, start)
    if match is None:
        raise ValueError(f"JS snippet not found: {snippet!r}")
    return match.start()


def js_contains(html, snippet):
    """Whitespace/quote-tolerant equivalent of `snippet in html` for JS source snippets."""
    return re.search(_js_pattern(snippet), html) is not None


def js_span(html, snippet, start=0):
    """Like js_search, but returns (match_start, match_end) -- for callers that used to compute
    an end offset as `html.index(snippet, start) + len(snippet)` against the old minified text."""
    match = re.compile(_js_pattern(snippet)).search(html, start)
    if match is None:
        raise ValueError(f"JS snippet not found: {snippet!r}")
    return match.start(), match.end()


class WhatsNewTabRenderTests(unittest.TestCase):
    """Issue #143: the "What's New" tab's markup and embedded data."""

    _BASE_STATE = {
        "generated_at": "2026-08-11T12:00:00-04:00",
        "timezone": "America/New_York",
        "fpl": {
            "season_status": "prior_season_data", "ready_for_2026_27": False,
            "player_count": 841, "team_count": 20, "event_count": 38,
        },
        "transfers": [], "sources": [],
    }

    def test_nav_button_and_static_shell_render(self):
        html = render_dashboard(self._BASE_STATE)

        self.assertIn('data-view="whats-new"', html)
        self.assertIn('id="view-whats-new"', html)
        self.assertIn("What's New", html)
        self.assertIn('id="whats-new-search"', html)
        self.assertIn('data-whats-new-filter="Feature"', html)
        self.assertIn('data-whats-new-filter="Fix"', html)
        self.assertIn('data-whats-new-filter="Data"', html)
        self.assertIn('data-whats-new-filter="Docs"', html)
        self.assertIn('data-whats-new-filter="Chore"', html)

    def test_email_subscribe_card_renders(self):
        html = render_dashboard(self._BASE_STATE)

        self.assertIn('id="whats-new-subscribe-form"', html)
        self.assertIn('id="whats-new-subscribe-email"', html)
        self.assertIn("Get release notes by email", html)

    def test_github_contribute_callout_renders_next_to_subscribe_card(self):
        html = render_dashboard(self._BASE_STATE)

        self.assertIn('id="whats-new-contribute-panel"', html)
        self.assertIn("Interested in contributing?", html)
        self.assertIn('href="https://github.com/onkartalekar/fpl-intelligence"', html)
        self.assertIn('target="_blank"', html)
        self.assertIn('rel="noopener noreferrer"', html)
        # Sits beside the subscribe panel in the same two-column .grid, not below it -- that's
        # the empty-space gap it was added to fill.
        subscribe_start = html.index('id="whats-new-subscribe-panel"')
        contribute_start = html.index('id="whats-new-contribute-panel"')
        grid_start = html.rindex('<div class="grid"', 0, subscribe_start)
        grid_end = html.index("</div>", contribute_start)
        self.assertLess(grid_start, subscribe_start)
        self.assertLess(contribute_start, grid_end)

    def test_release_notes_entries_are_embedded_in_dashboard_data(self):
        state = {
            **self._BASE_STATE,
            "release_notes": [
                {
                    "date": "2026-08-11",
                    "headline": "Sharper filters for preseason movement tracking",
                    "summary": "Club movement just got easier to scan.",
                    "changes": [
                        {"category": "Feature", "audience": "user", "title": "Split filters", "description": "Three controls now."},
                    ],
                },
            ],
        }

        html = render_dashboard(state)

        match = re.search(
            r'<script id="dashboard-data" type="application/json">(.*?)</script>', html, re.DOTALL,
        )
        self.assertIsNotNone(match)
        embedded = json.loads(match.group(1))
        self.assertEqual(embedded["release_notes"][0]["headline"], "Sharper filters for preseason movement tracking")

    def test_entry_summary_drops_the_per_entry_change_count_badge(self):
        # Per request: the "N changes" badge on each release-note entry's summary row -- useless
        # to the customer, so dropped. The date/headline pair stays; only the trailing count is
        # gone. (The page-level "N entries" count at the top of the tab is a different element,
        # #whats-new-count, and is untouched.)
        html = render_dashboard(self._BASE_STATE)

        entry_start = js_search(html, "function renderWhatsNew(){")
        entry_end = js_search(html, "}\nfunction ", entry_start)
        entry_body = html[entry_start:entry_end]
        self.assertIn("whats-new-date", entry_body)
        self.assertIn("whats-new-headline", entry_body)
        self.assertNotIn("change${changes.length===1?'':'s'}", entry_body)
        self.assertNotIn("${changes.length} change", entry_body)

    def test_renders_without_release_notes_key_present(self):
        # Mirrors every other new-view addition to this template: absence of the key (a fresh
        # install that has never had /api/release-notes POSTed to it) must not raise or leave
        # "undefined" anywhere in the served page -- dashboard.js's own `state.release_notes||[]`
        # fallback handles that at the JS layer, but the Python render must not choke either.
        html = render_dashboard(self._BASE_STATE)

        self.assertNotIn("undefined", html)


class TransferFilterPerspectiveTests(unittest.TestCase):
    """Issue #232: the transfers view's direction/movement filters must be read relative to the
    selected club, not to whichever club's transfer-centre playlist a record happened to come
    from. Filtering CLUB=Man Utd + Outgoing used to return Youri Tielemans (Aston Villa -> Man
    Utd), because his Aston-Villa-relative `movement_direction: "out"` was compared verbatim
    while the club predicate had matched him on `to_club`.

    The JS itself is not executed by this suite, so these pin the shape of the fix against a
    silent revert; the behaviour is verified in a real browser as part of shipping.
    """

    def _js(self):
        return render_dashboard({"fpl": {}, "transfers": [], "sources": []})

    def test_direction_and_movement_filters_read_the_selected_club_perspective(self):
        html = self._js()

        # The filter predicate must consult the derived perspective, never the stored fields.
        self.assertTrue(js_contains(html, "const view=perspectiveOf(row,club)"))
        self.assertTrue(js_contains(html, "direction==='all'||view.direction===direction"))
        self.assertTrue(js_contains(html, "movement==='all'||view.movement===movement"))
        self.assertNotIn("row.movement_direction===direction", re.sub(r"\s+", "", html))
        self.assertNotIn("row.movement_type===movement", re.sub(r"\s+", "", html))

    def test_club_predicate_stays_relational(self):
        """Narrowing it to premier_league_club would be simpler but wrong -- most intra-PL moves
        are reported by only one of the two clubs, so the buying club's arrivals would vanish
        from its own view rather than merely being mislabelled."""
        html = self._js()

        self.assertTrue(js_contains(html, "row.premier_league_club===club"))
        self.assertTrue(js_contains(html, "row.from_club===club"))
        self.assertTrue(js_contains(html, "row.to_club===club"))

    def test_counterparty_movement_types_are_mirrored(self):
        html = self._js()

        self.assertTrue(js_contains(html, "'transfer-in':'transfer-out'"))
        self.assertTrue(js_contains(html, "'transfer-out':'transfer-in'"))
        self.assertTrue(js_contains(html, "'loan-in':'loan-out'"))
        self.assertTrue(js_contains(html, "'loan-out':'loan-in'"))
        # end-of-loan has no opposite type, so it must fall through to its own value rather than
        # to undefined.
        self.assertTrue(
            js_contains(html, "MIRRORED_MOVEMENT[row.movement_type]||row.movement_type")
        )

    def test_a_release_keeps_its_own_bucket_only_for_the_releasing_club(self):
        """"Released" is a departure that deliberately does not fold into Outgoing -- but only
        for the club doing the releasing. Where the write-up names where the player went (Fulham
        released Harry Wilson, who joined Leeds), the receiving club sees an ordinary arrival."""
        html = self._js()

        self.assertTrue(js_contains(html, "if(isOrigin&&stored.direction==='released')return stored"))

    def test_unrelated_club_match_falls_back_to_the_records_own_framing(self):
        """A record can match the club filter via premier_league_club alone, with the club on
        neither side of the move -- there is no counterparty perspective to take there."""
        html = self._js()

        self.assertTrue(js_contains(html, "if(isOrigin===isDestination)return stored"))
        self.assertTrue(js_contains(html, "if(club==='all')return stored"))


class DashboardRenderTests(unittest.TestCase):
    def test_renders_honest_empty_transfer_state_and_source(self):
        state = {
            "generated_at": "2026-07-18T12:00:00-04:00",
            "timezone": "America/New_York",
            "fpl": {
                "season_status": "prior_season_data",
                "ready_for_2026_27": False,
                "player_count": 841,
                "team_count": 20,
                "event_count": 38,
            },
            "transfers": [],
            "sources": [
                {
                    "name": "Official FPL",
                    "url": "https://fantasy.premierleague.com/api/bootstrap-static/",
                }
            ],
        }

        html = render_dashboard(state)

        self.assertIn("No confirmed transfers have been loaded yet", html)
        self.assertIn("prior_season_data", html)
        self.assertIn("https://fantasy.premierleague.com/api/bootstrap-static/", html)
        self.assertNotIn("undefined", html)

    def test_evidence_inspector_resets_when_filters_change(self):
        """Issue #233: the evidence inspector is a persistent panel written only by `inspect()`.
        Before this fix, `applyFilters()` (run on every search/filter/reset/page change) never
        touched it, so a previously-inspected transfer's evidence stayed pinned even after it
        dropped out of the filtered result set. `applyFilters()` must reset it back to the same
        placeholder markup the template ships initially, so a stale row is never shown as current."""
        state = {
            "generated_at": "2026-07-18T12:00:00-04:00",
            "timezone": "America/New_York",
            "fpl": {"season_status": "prior_season_data", "ready_for_2026_27": False, "player_count": 0, "team_count": 20},
            "transfers": [],
            "sources": [],
        }

        html = render_dashboard(state)

        # Initial server-rendered placeholder (dashboard-shell.html).
        placeholder = "Select a result to inspect its source, classification, and FPL reconciliation state."
        self.assertIn(f'id="inspector" class="empty" aria-live="polite">{placeholder}', html)

        # `applyFilters()` must reset the inspector to that same placeholder/class on every call,
        # not just leave whatever `inspect(row)` last wrote in place.
        self.assertTrue(
            js_contains(html, "function resetInspector() {"),
            "no resetInspector() helper found in dashboard JS",
        )
        self.assertTrue(
            js_contains(html, f'"{placeholder}"'),
            "resetInspector() placeholder text doesn't match the template's initial copy",
        )
        self.assertTrue(
            js_contains(html, "function applyFilters() { resetInspector();"),
            "applyFilters() must call resetInspector() first, so a stale inspected row can't "
            "survive a filter/search/reset/page change",
        )

    def test_renders_preseason_workspace_and_manageable_transfer_controls(self):
        state = {
            "generated_at": "2026-07-18T12:00:00-04:00",
            "timezone": "America/New_York",
            "fpl": {
                "season_status": "prior_season_data",
                "ready_for_2026_27": False,
                "player_count": 841,
                "team_count": 20,
                "event_count": 38,
            },
            "transfers": [
                {
                    "player": "Example Player",
                    "from_club": "Ajax",
                    "to_club": "Arsenal",
                    "premier_league_club": "Arsenal",
                    "announced_at": "2026-07-18T12:00:00Z",
                    "source_url": "https://www.arsenal.com/example",
                    "verification_status": "confirmed_first_party",
                    "fpl_reconciliation_status": "pending_new_season_fpl",
                    "fpl_relevance": "medium",
                    "movement_direction": "in",
                    "movement_type": "transfer-in",
                    "freshness": "new_7d",
                }
            ],
            "transfer_summary": {"total": 1, "high": 0, "medium": 1, "low": 0, "actionable": 1},
            "club_summaries": [
                {"club": "Arsenal", "arrivals": 1, "departures": 0, "relevant_moves": 1, "latest_at": "2026-07-18T12:00:00Z"}
            ],
            "sources": [],
        }

        html = render_dashboard(state)

        self.assertIn("Preseason overview", html)
        self.assertIn('data-view="transfers"', html)
        self.assertIn('id="transfer-search"', html)
        self.assertIn('id="club-filter"', html)
        self.assertIn('id="relevance-filter"', html)
        self.assertIn('id="direction-filter"', html)
        self.assertIn('id="movement-filter"', html)
        self.assertIn('id="freshness-filter"', html)
        self.assertIn('id="prev-page"', html)
        self.assertIn('id="next-page"', html)
        self.assertIn("20 per page", html)
        self.assertIn("My Team", html)
        self.assertIn("Decision Center", html)
        self.assertIn("Player Explorer", html)
        self.assertIn("Fixtures", html)
        self.assertIn("Model Status", html)
        self.assertIn("Since last refresh", html)
        self.assertIn('class="overview-stack"', html)
        # Issue #27: there is no in-page refresh control or refresh token anymore -- /api/refresh
        # is operator-only. `#refresh-message`/`#refresh-source-status` remain (they still show
        # the "last refreshed" status), just without the button or the token that used to sit
        # alongside them.
        self.assertNotIn('id="refresh-now"', html)
        self.assertNotIn('name="refresh-token"', html)
        self.assertIn('id="refresh-message"', html)

    def test_renders_stage_aware_accessible_controls(self):
        state = {
            "generated_at": "2026-07-18T12:00:00-04:00",
            "timezone": "America/New_York",
            "fpl": {
                "season_status": "prior_season_data",
                "season_phase": "feed_pending",
                "ready_for_2026_27": False,
                "next_deadline": None,
                "player_count": 841,
                "team_count": 20,
                "event_count": 38,
            },
            "transfers": [],
            "sources": [],
        }

        html = render_dashboard(state)

        self.assertIn("Waiting for 2026/27 FPL launch", html)
        self.assertIn("Eastern Time (New York)", html)
        self.assertIn('id="season-readiness"', html)
        self.assertIn('id="deadline-status"', html)
        self.assertIn('id="mobile-nav"', html)
        self.assertIn('aria-current="page"', html)
        self.assertIn('id="more-filters"', html)
        self.assertIn('id="active-filters"', html)
        self.assertIn('id="reset-filters"', html)
        self.assertIn('id="preseason-workflow"', html)
        self.assertIn('id="refresh-source-status"', html)
        self.assertIn('aria-live="polite"', html)

    def test_renders_player_explorer_controls_and_official_price_fields(self):
        state = {
            "generated_at": "2026-07-23T13:00:00-04:00",
            "timezone": "America/New_York",
            "fpl": {"season_status": "target_season_ready", "season_phase": "preseason", "ready_for_2026_27": True, "player_count": 1, "team_count": 20},
            "players": [{"id": 1, "name": "Raya", "club": "Arsenal", "position": "Goalkeeper", "price": 6.0, "ownership": 26.9, "status": "a", "news": ""}],
            "fixtures": [], "fixture_summary": {"status": "not_active"},
            "manager": {"connection_status": "not_configured", "squad": []},
            "transfers": [], "club_summaries": [], "sources": [],
        }

        html = render_dashboard(state)

        self.assertIn('id="player-search"', html)
        self.assertIn('id="player-club-filter"', html)
        self.assertIn('id="player-position-filter"', html)
        self.assertIn('id="player-sort"', html)
        self.assertIn('id="player-results"', html)
        self.assertIn("Official FPL prices", html)
        self.assertIn("£", html)

    def test_renders_fixture_gameweek_and_club_filters(self):
        state = {
            "generated_at": "2026-07-23T13:00:00-04:00",
            "timezone": "America/New_York",
            "fpl": {"season_status": "target_season_ready", "season_phase": "preseason", "ready_for_2026_27": True, "next_event_id": 1, "player_count": 0, "team_count": 20},
            "players": [],
            "fixtures": [{"id": 1, "event": 1, "home_team": "Arsenal", "away_team": "Chelsea", "home_difficulty": 2, "away_difficulty": 5, "kickoff_time": "2026-08-21T19:00:00Z"}],
            "fixture_summary": {"status": "ready", "fixture_count": 1, "gameweek_count": 1},
            "manager": {"connection_status": "not_configured", "squad": []},
            "transfers": [], "club_summaries": [], "sources": [],
        }

        html = render_dashboard(state)

        self.assertIn('id="fixture-gameweek"', html)
        self.assertIn('id="fixture-club-filter"', html)
        self.assertIn('id="fixture-results"', html)
        self.assertIn("Official FPL fixtures", html)
        self.assertIn("Difficulty", html)

        # Gameweek stepping is prev/next navigation, not a dropdown (issue #39).
        self.assertIn('id="fixture-gameweek-prev"', html)
        self.assertIn('id="fixture-gameweek-next"', html)
        self.assertIn('id="fixture-gameweek-value"', html)
        self.assertIn('<select id="fixture-gameweek" hidden', html)

    def test_pitch_card_projection_swaps_to_a_single_number_below_the_mobile_breakpoint(self):
        """Issue 133: the mobile @media(max-width:760px) block must hide the full 1/3/5-GW
        string and show the compact single-number fallback -- the CSS-side half of the fix
        (test_renders_active_gw1_decision_center's markup assertions cover the JS-side half)."""
        state = {
            "generated_at": "2026-07-18T12:00:00-04:00",
            "timezone": "America/New_York",
            "fpl": {"season_status": "prior_season_data", "ready_for_2026_27": False, "player_count": 0, "team_count": 20},
            "players": [], "fixtures": [], "fixture_summary": {"status": "not_active"},
            "manager": {"connection_status": "not_configured", "squad": []},
            "transfers": [], "club_summaries": [], "sources": [],
        }

        html = render_dashboard(state)
        style_block = html[html.index(":root {"):html.index("</style>")]
        breakpoint_block = style_block[style_block.index("@media(max-width:760px)"):]

        self.assertIn(".pitch-player .projection-full {", breakpoint_block)
        self.assertIn(".pitch-player .projection-compact {", breakpoint_block)
        self.assertIn(".pitch-player[data-player-id] {", breakpoint_block)

    def test_renders_light_dark_theme_toggle(self):
        state = {
            "generated_at": "2026-07-18T12:00:00-04:00",
            "timezone": "America/New_York",
            "fpl": {"season_status": "prior_season_data", "ready_for_2026_27": False, "player_count": 0, "team_count": 20},
            "players": [], "fixtures": [], "fixture_summary": {"status": "not_active"},
            "manager": {"connection_status": "not_configured", "squad": []},
            "transfers": [], "club_summaries": [], "sources": [],
        }

        html = render_dashboard(state)
        style_block = html[html.index(":root {"):html.index("</style>")]

        # Toggle control is an accessible slider switch (sun/moon icons,
        # sliding thumb) with its pre-paint theme script (issue #48).
        self.assertIn('id="theme-toggle"', html)
        self.assertIn('role="switch"', html)
        self.assertIn('aria-checked="false"', html)
        self.assertIn("☀️", html)
        self.assertIn("🌙", html)
        self.assertIn('class="theme-toggle-thumb"', html)
        self.assertTrue(js_contains(html, "localStorage.getItem('fpl-theme')"))
        self.assertIn("prefers-color-scheme: light", html)

        # A real light-theme override block exists, keyed off a data attribute.
        self.assertIn(':root[data-theme="light"] {', style_block)

        # No hardcoded hex/rgba color literals remain in the CSS body -- only
        # inside the two :root variable-definition blocks (theming regression
        # guard: any new color a future change adds must be a variable).
        light_block_end = style_block.index("}", style_block.index(':root[data-theme="light"] {'))
        css_body = style_block[light_block_end + 1:]
        literal_colors = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)", css_body)
        self.assertEqual(literal_colors, [], f"raw color literals found outside :root: {literal_colors}")

    def test_renders_active_gw1_decision_center_without_stale_launch_copy(self):
        player = {
            "id": 411, "name": "Haaland", "club": "Man City", "position_short": "FWD",
            "price": 15.5, "expected_minutes": 77.7, "xp_1": 5.3, "xp_3": 17.2,
            "xp_5": 28.9, "lower_5": 24.3, "upper_5": 33.5, "confidence": "high",
            "fixture_difficulties": [3, 3, 2, 4, 2], "news": "",
        }
        state = {
            "generated_at": "2026-07-23T18:00:00-04:00",
            "timezone": "America/New_York",
            "fpl": {"season_status": "target_season_ready", "season_phase": "preseason", "ready_for_2026_27": True},
            "decision_center": {
                "status": "active_preliminary", "event": 1,
                "model": {"name": "Official-data preseason baseline", "version": "0.1", "limitations": ["Preliminary and not yet calibrated."], "inputs": ["Official FPL data"]},
                "recommended_squad": {
                    "players": [player], "starting_xi": [player], "bench": [], "captain": player,
                    "vice_captain": player, "formation": "3-5-2", "cost": 100.0,
                    "money_remaining": 0.0, "projected_gw1_points_including_captain": 61.2,
                    "starting_xi_xp_5": 250.0,
                },
                "captaincy": [player], "watchlist": {"FWD": [player]},
            },
            "manager": {"connection_status": "registered_preseason", "squad": []},
            "model_performance": {
                "status": "active", "completed_comparisons": 1, "pending_comparisons": 2,
                "actual_events_collected": 1,
                "summary": {"count": 1, "mae": 4.0, "bias": -4.0, "rmse": 4.0, "range_coverage": 1.0},
                "by_horizon": {}, "by_profile": {},
                "calibration": {"ready": False, "status": "More completed forecasts are needed.", "recommendations": ["Keep collecting results."]},
                "comparisons": [{"origin_event": 1, "through_event": 1, "profile_label": "Balanced", "horizon": 1, "modeled_points": 50.0, "actual_points": 46, "error": -4.0, "inside_range": True}],
                "method": "Frozen forecasts compared with official FPL points.", "collection_errors": [],
            },
            "players": [], "fixtures": [], "transfers": [], "club_summaries": [],
            "sources": [
                {"name": "Official FPL fixtures", "url": "https://fantasy.premierleague.com/api/fixtures/"},
                {"name": "Public FPL manager entry", "url": "https://fantasy.premierleague.com/api/entry/364759/"},
            ],
        }

        base_squad = state["decision_center"]["recommended_squad"]
        state["decision_center"]["default_profile"] = "balanced"
        state["decision_center"]["profile_recommendations"] = [
            {
                "id": profile_id,
                "label": label,
                "summary": summary,
                "risk_note": risk,
                "squad": base_squad,
                "captaincy": [player],
                "metrics": {
                    "central_1gw": round(central * 0.2, 1),
                    "central_3gw": round(central * 0.6, 1),
                    "central_5gw": central,
                    "lower_5gw": central - 20,
                    "upper_5gw": central + 20,
                    "average_ownership": ownership,
                    "average_expected_minutes": 78.0,
                    "low_confidence_players": low_confidence,
                },
                "comparison_to_balanced": {
                    "shared_players": 15,
                    "changed_players": [],
                    "central_5gw_delta": central - 250,
                },
            }
            for profile_id, label, summary, risk, central, ownership, low_confidence in [
                ("conservative", "Conservative", "Minutes security and downside protection", "Lower ceiling", 246, 24, 0),
                ("balanced", "Balanced", "Central projection with flexibility", "Moderate uncertainty", 250, 18, 1),
                ("aggressive", "Aggressive", "Upper projection and differential exposure", "Higher role risk", 255, 9, 3),
            ]
        ]

        html = render_dashboard(state)

        self.assertIn('id="decision-summary"', html)
        self.assertIn('id="recommended-xi"', html)
        self.assertIn('class="formation-pitch"', html)
        self.assertIn("pitch-row", html)
        self.assertIn("pitch-player", html)
        self.assertIn("Number(player.xp_3).toFixed(1)", html)
        self.assertNotIn("1 / 3 / 5 GW xPts", html)
        # Issue 133: a compact, single-number fallback exists alongside the full 1/3/5-GW string,
        # so a narrow viewport can show one complete number instead of truncating the full string
        # mid-digit -- both classes must be present in the pitch-card markup.
        self.assertIn('class="projection projection-full"', html)
        self.assertIn('class="projection projection-compact"', html)
        self.assertIn('id="recommended-bench"', html)
        self.assertIn('id="captaincy-list"', html)
        self.assertIn('id="profile-options"', html)
        self.assertIn('id="profile-comparison"', html)
        self.assertIn('data-view="performance"', html)
        self.assertIn('id="performance-summary"', html)
        self.assertIn("Modeled vs actual points", html)
        self.assertIn("Calibration diagnostics", html)
        self.assertIn("Official actual", html)
        self.assertIn("Conservative", html)
        self.assertIn("Balanced", html)
        self.assertIn("Aggressive", html)
        # Bug fix: this section used to build its own separate Conservative/Balanced/Aggressive
        # tab strip -- a second, independent profile selector stacked directly below the rich
        # renderProfileComparison() panel above it, both driving the same three profiles. Removed
        # in favor of the one already-relocated selector; weekly-profile-comparison-mount is where
        # it lands.
        self.assertNotIn('id="weekly-profile-options"', html)
        self.assertIn('id="weekly-profile-comparison-mount"', html)
        self.assertIn('id="weekly-scenarios"', html)
        self.assertIn('id="weekly-plan"', html)
        self.assertIn('id="weekly-branches"', html)
        self.assertIn("Conditional future branches", html)
        self.assertIn("five_gameweek_advantage_over_roll", html)
        self.assertIn("free_transfer_source", html)
        self.assertIn('id="weekly-pitch"', html)
        self.assertIn("Recommended post-decision XI", html)
        self.assertIn("Roll, transfer, and chip recommendation", html)
        self.assertIn("use the personalized weekly decision below for actual transfers", html)
        self.assertNotIn("use the personalized weekly decision above for actual transfers", html)
        self.assertIn("Preliminary recommendation", html)
        self.assertIn("5-GW range", html)
        self.assertIn("1-GW modeled xPts", html)
        self.assertIn("3-GW modeled xPts", html)
        self.assertIn("5-GW modeled xPts", html)
        self.assertIn("1 / 3 / 5 GW central", html)
        self.assertNotIn("Waiting for the official 2026/27 feed", html)
        self.assertNotIn("after launch", html.lower())
        self.assertIn("Official FPL fixtures", html)
        self.assertIn("Public FPL manager entry", html)
        self.assertIn('id="fixture-congestion-limitation"', html)
        self.assertIn("Projections use official Premier League fixtures and FPL fixture difficulty", html)
        self.assertIn("European and domestic-cup schedules are not yet modeled directly", html)

    def test_decision_center_grid_does_not_stretch_short_panels(self):
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertRegex(
            html,
            r"\.decision-layout\s*\{\s*display:\s*grid;\s*align-items:\s*start;",
        )
        self.assertRegex(html, r"\.formation-pitch\s*\{")

    def test_decision_subnav_covers_every_section(self):
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertIn('data-scroll-to="decision-section-weekly"', html)
        self.assertIn('data-scroll-to="decision-section-bench"', html)
        self.assertIn('id="decision-section-bench"', html)
        self.assertTrue(
            js_contains(
                html,
                "['decision-section-summary','decision-section-weekly',"
                "'decision-section-profiles','decision-section-xi',"
                "'decision-section-bench','decision-section-squad']",
            )
        )

    def test_fresh_squad_benchmark_is_one_unified_group_open_by_default(self):
        # Decision Center reorganization, corrected after live feedback: the fresh-squad
        # benchmark -- summary, risk profiles, XI/captaincy, bench & model, squad & player detail
        # -- is ALL non-personalized content and must collapse/expand together as one single
        # unit, not as three independently-toggling pieces (the original cut of this wrapped only
        # summary on `weekly.draft`, and bench/squad separately on `decision.event`, which read as
        # disjointed -- profiles and XI/captaincy weren't wrapped at all). Open by default: with
        # no more relevant personalized recommendation available yet, it's the only useful thing
        # on the page.
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        details_start = html.index('<details class="decision-details" id="decision-benchmark-details"')
        details_tag = html[details_start:details_start + 120]
        self.assertIn(" open", details_tag.split(">")[0])
        self.assertIn('id="decision-benchmark-details-label">Preliminary recommendation<', html)

        # Every one of the five benchmark sub-sections lives inside this one wrapper, in this
        # order, and closes before the next `</details>` -- not their own separate wrappers.
        details_end = html.index("</details>", details_start)
        benchmark_block = html[details_start:details_end]
        for marker in [
            'id="decision-section-summary"', 'id="decision-section-profiles"',
            'id="decision-section-xi"', 'id="decision-section-bench"', 'id="decision-section-squad"',
        ]:
            self.assertIn(marker, benchmark_block)
        # No stale per-section wrapper ids from the original (disjointed) cut remain anywhere.
        for stale_id in ["decision-summary-details", "decision-section-bench-details", "decision-section-squad-details"]:
            self.assertNotIn(f'id="{stale_id}"', html)

        # The personalized weekly section sits outside the benchmark wrapper, as its own sibling.
        self.assertNotIn('id="decision-section-weekly"', benchmark_block)

    def test_weekly_priority_reorder_and_unified_demote_js_present(self):
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        # Gated on `weekly.status==='active'`, not `weekly.draft` alone -- true both for a
        # declared preseason draft and for a real published squad once GW1 has passed, so the
        # benchmark stays demoted post-season-start too, instead of snapping back open the moment
        # `weekly.draft` goes false again.
        self.assertTrue(js_contains(html, "const weeklyPersonalized=weekly.status==='active'"))
        self.assertTrue(js_contains(html, "classList.toggle('weekly-priority',weeklyPersonalized)"))
        self.assertTrue(js_contains(html, "benchmarkDetails.open=!weeklyPersonalized"))
        self.assertTrue(js_contains(html, "'Reference: from-scratch squad (ignores your draft)'"))
        self.assertTrue(
            js_contains(html, "'Reference: from-scratch squad (see your weekly decision above)'")
        )
        self.assertIn("#decisions-content.weekly-priority > .decision-subnav", html)
        self.assertIn("#decisions-content.weekly-priority > #decision-section-weekly", html)

        # Clicking a subnav chip must open a collapsed ancestor `<details>` before scrolling to
        # it, or it would scroll to an invisible (zero-height, collapsed) target.
        self.assertTrue(js_contains(html, "const collapsedAncestor=target.closest('details')"))
        self.assertTrue(js_contains(html, "collapsedAncestor.open=true"))

    def test_decision_scroll_targets_clear_sticky_subnav(self):
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertRegex(html, r"scroll-margin-top:\s*58px")
        self.assertTrue(js_contains(html, "matchMedia('(prefers-reduced-motion: reduce)')"))
        self.assertNotIn("renderDecisionLegacy", html)

    def test_mobile_inspector_order_is_scoped_to_transfers(self):
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertRegex(html, r"\.transfer-layout \.inspector\s*\{\s*order:\s*-1;")

        # The mobile-breakpoint bare `.inspector { position: static; }` rule
        # must NOT also carry `order` -- that's scoped only to the more
        # specific `.transfer-layout .inspector` selector above, not merged
        # into every `.inspector` on mobile.
        bare_inspector_bodies = [
            match.group(1)
            for match in re.finditer(r"(?m)^\s*\.inspector \{\n(.*?)\n\s*\}", html, re.DOTALL)
        ]
        mobile_bodies = [body for body in bare_inspector_bodies if "position: static" in body]
        self.assertTrue(mobile_bodies, "no bare .inspector rule with position: static found")
        for body in mobile_bodies:
            self.assertNotIn("order", body)

        self.assertRegex(
            html,
            r"\.decision-subnav\s*\{[^}]*flex-wrap:\s*nowrap[^}]*overflow-x:\s*auto",
        )
        self.assertRegex(
            html,
            r"@media\(max-width:980px\)\s*\{\s*\.decision-layout\s*\{\s*grid-template-columns:\s*1fr;",
        )

    def test_decision_center_surfaces_watchlist_and_rotation(self):
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertIn('id="decision-watchlist"', html)
        self.assertIn('id="decision-rotation"', html)
        self.assertIn("decision.watchlist", html)
        self.assertIn("evaluation_horizons", html)
        self.assertIn("Five-gameweek XI rotation", html)
        self.assertIn("Watchlist", html)

    def test_risk_profile_controls_follow_aria_tabs_keyboard_pattern(self):
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertIn('id="profile-panel" role="tabpanel"', html)
        self.assertIn('id="weekly-profile-panel" role="tabpanel"', html)
        self.assertIn('aria-controls="profile-panel"', html)
        # Bug fix: weekly-profile-panel's own local tab strip (the one that used to produce
        # aria-controls="weekly-profile-panel") was removed as a duplicate profile selector --
        # it's now labelled by the surviving, relocated tab strip's buttons instead.
        self.assertTrue(
            js_contains(
                html, "byId('weekly-profile-panel').setAttribute('aria-labelledby',`profile-tab-"
            )
        )
        self.assertTrue(js_contains(html, "tabindex=\"${profile.id===selected.id?'0':'-1'}\""))
        self.assertTrue(js_contains(html, "['ArrowLeft','ArrowRight','Home','End']"))
        self.assertTrue(js_contains(html, "next.focus()"))

    def test_compare_risk_profiles_panel_can_switch_to_the_visitors_own_squad(self):
        # Issue #158: "Compare risk profiles" used to always be built inline inside
        # renderDecision() from decision.profile_recommendations alone -- a freshly optimized,
        # generic squad, never the visitor's own declared draft or real squad. Split into its own
        # function so it can independently source weekly.profiles (now carrying the same
        # metrics/evaluation_horizons shape) once weekly.status==='active'.
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertTrue(js_contains(html, "function renderProfileComparison(profileId=null)"))
        self.assertIn('id="profile-comparison-heading">Compare risk profiles</h2>', html)
        self.assertIn('id="profile-comparison-subtitle">', html)
        comparison_start = js_search(html, "function renderProfileComparison(profileId=null)")
        comparison_end = js_search(
            html, "\nfunction renderDecision(profileId=null){", comparison_start
        )
        comparison_body = html[comparison_start:comparison_end]
        self.assertTrue(js_contains(comparison_body, "weekly.status==='active'"))
        self.assertTrue(js_contains(comparison_body, "weekly.profiles"))
        # Captaincy-delta framing replaces the benchmark's "changed players in/out" sentence for
        # the personalized case, since squad membership never varies across profiles there.
        self.assertIn("captains", comparison_body)
        self.assertIn("Same captain and lineup across all three profiles", comparison_body)
        # renderDecision() calls the extracted function so every existing call site keeps working.
        decision_start = js_search(html, "function renderDecision(profileId=null){")
        self.assertTrue(
            js_contains(html[decision_start:decision_start + 400], "renderProfileComparison(profileId)")
        )

    def test_personalized_compare_risk_profiles_panel_relocates_out_of_the_collapsed_benchmark(self):
        # Bug fix, live-reported: decision-section-profiles lives by default inside <details
        # id="decision-benchmark-details">, which collapses shut (the weekly-priority demote) the
        # instant personalized data exists -- silently burying the one panel inside it that had
        # just become personalized and relevant. "I do not see enhanced risk profile either for
        # draft in the decision center. See second screenshot from the comparison only section."
        # Fix: relocate the actual section node (not a copy) into the always-visible personalized
        # weekly section when personalized, and back to its home spot otherwise.
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertIn('<div id="decision-section-profiles-home"></div>', html)
        self.assertIn('<div id="weekly-profile-comparison-mount"></div>', html)
        # The home anchor sits inside the collapsible benchmark details, right before the section
        # it anchors -- the mount point sits inside the always-visible weekly section.
        details_start = html.index('id="decision-benchmark-details"')
        details_end = html.index("</details>", details_start)
        self.assertIn("decision-section-profiles-home", html[details_start:details_end])
        weekly_start = html.index('id="decision-section-weekly"')
        self.assertIn("weekly-profile-comparison-mount", html[weekly_start:weekly_start + 600])

        comparison_start = js_search(html, "function renderProfileComparison(profileId=null)")
        comparison_end = js_search(
            html, "\nfunction renderDecision(profileId=null){", comparison_start
        )
        comparison_body = html[comparison_start:comparison_end]
        self.assertTrue(js_contains(comparison_body, "weeklyMount.appendChild(profilesSection)"))
        self.assertTrue(js_contains(comparison_body, "homeAnchor.after(profilesSection)"))

    def test_player_search_folds_diacritics_in_both_search_boxes(self):
        # Reported live: searching "guehi" found nothing for Marc Guéhi, while "guimar" found
        # Bruno Guimarães -- not because one accented name worked and another didn't, but because
        # a plain substring match can never span *through* an accented character at all. "guimar"
        # only "worked" because it happened to end before Guimarães' accented "ã"; any query
        # reaching an accent (like "guehi" needing to match through "é") always failed. Both the
        # Player Explorer and the Draft tab's "Add players" search share this same query logic.
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertTrue(
            js_contains(
                html,
                # Prettier's default `arrowParens: always` wraps this single-argument arrow
                # function's param in parens -- `value=>` became `(value) =>` -- a real syntax
                # addition, not just whitespace, so the snippet reflects it explicitly.
                "const foldDiacritics=(value)=>String(value??'').normalize('NFD')"
                ".replace(/[\\u0300-\\u036f]/g,'');",
            )
        )
        player_search_start = js_search(html, "function renderPlayers(){")
        self.assertTrue(
            js_contains(
                html[player_search_start:player_search_start + 500],
                "foldDiacritics(byId('player-search').value.trim().toLocaleLowerCase())",
            )
        )
        self.assertTrue(
            js_contains(
                html[player_search_start:player_search_start + 1000],
                "foldDiacritics(`${player.name} ${player.full_name||''}`.toLocaleLowerCase())"
                ".includes(query)",
            )
        )
        draft_search_start = js_search(html, "function draftResultRows(){")
        self.assertTrue(
            js_contains(
                html[draft_search_start:draft_search_start + 500],
                "foldDiacritics(byId('draft-search').value.trim().toLocaleLowerCase())",
            )
        )

    def test_player_and_performance_datasets_use_semantic_tables(self):
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertIn('<table class="player-table">', html)
        self.assertIn('<tbody id="player-results">', html)
        self.assertIn('<table class="performance-table">', html)
        self.assertIn('<tbody id="performance-history">', html)
        self.assertIn('<tr class="player-row">', html)
        self.assertIn('<tr class="performance-row">', html)

    def test_model_performance_view_surfaces_team_and_player_forecast_panels(self):
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertIn('id="performance-team-history"', html)
        self.assertIn('id="performance-team-summary"', html)
        self.assertIn('id="performance-player-select"', html)
        self.assertIn('id="performance-player-history"', html)
        self.assertIn("My team -- modeled vs actual", html)
        self.assertIn("Player forecast vs actual", html)
        self.assertIn("performance.team_performance", html)
        self.assertIn("performance.player_performance", html)
        self.assertIn("Results are never backfilled with hindsight lineups.", html)

    def test_deadline_passed_state_requests_an_explicit_refresh(self):
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertIn("Deadline passed", html)
        self.assertIn("Refresh required to load the next deadline", html)
        self.assertTrue(js_contains(html, "deadline <= Date.now()"))

    def test_page_load_restores_the_previously_saved_view_profile_and_filters(self):
        # Issue #27: `captureWorkspaceContext()`/`restoreWorkspaceContext()` predate the
        # now-removed in-browser refresh flow (their only caller was `runRefresh()`, which
        # captured context right before the client-side reload it triggered) -- both function
        # definitions remain, and `restoreWorkspaceContext()` is still invoked on every page
        # load, so a workspace-context snapshot from a previous session is still restored.
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertTrue(js_contains(html, "function captureWorkspaceContext()"))
        self.assertTrue(js_contains(html, "function restoreWorkspaceContext()"))
        self.assertTrue(js_contains(html, "'fpl-workspace-context'"))
        self.assertTrue(js_contains(html, "restoreWorkspaceContext();"))

    def test_model_performance_collection_errors_are_visible(self):
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertIn('id="performance-errors"', html)
        self.assertTrue(js_contains(html, "performance.collection_errors||[]"))
        self.assertIn("Result collection issue", html)

    def test_invalid_or_untrusted_source_urls_render_as_text_not_links(self):
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertTrue(js_contains(html, "function safeLink(url,label)"))
        self.assertTrue(js_contains(html, "parsed.protocol==='https:'"))
        self.assertTrue(js_contains(html, "trustedLinkDomains.has(host)"))
        self.assertFalse(js_contains(html, "['http:','https:'].includes(parsed.protocol)"))
        self.assertTrue(js_contains(html, "safeLink(source.url,source.name)"))
        self.assertTrue(js_contains(html, "safeLink(url,`Source ${index+1}`)"))
        self.assertTrue(js_contains(html, "safeLink(rules.source,'Official FPL rules')"))

    def test_renders_connected_my_team_workspace(self):
        state = {
            "generated_at": "2026-07-22T12:00:00-04:00",
            "timezone": "America/New_York",
            "fpl": {"season_status": "target_season_ready", "season_phase": "preseason", "ready_for_2026_27": True},
            "manager": {
                "team_id": 364759,
                "team_name": "BrunoMans",
                "manager_name": "Onkar Talekar",
                "connection_status": "registered_preseason",
                "squad_publicly_available": False,
                "squad": [],
            },
            "transfers": [],
            "sources": [],
        }

        html = render_dashboard(state)

        self.assertIn('id="my-team-summary"', html)
        self.assertIn('id="squad-grid"', html)
        self.assertIn("Public GW1 squad is hidden until the deadline", html)
        self.assertIn("Team ID", html)

    def test_default_view_is_my_team(self):
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertIn('id="view-squad" class="view active"', html)
        self.assertNotIn('id="view-overview" class="view active"', html)
        self.assertIn('data-view="squad">My Team</button>', html)
        self.assertTrue(js_contains(html, "showView(titles[context.view]?context.view:'squad')"))

    def test_renders_manager_profile_form(self):
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertIn('id="profile-settings"', html)
        self.assertIn('id="view-profile" class="view"', html)
        self.assertIn('data-view="profile">My Profile</button>', html)
        self.assertIn('id="profile-form"', html)
        self.assertIn('id="profile-team-id"', html)
        self.assertIn('id="profile-timezone"', html)
        self.assertIn('id="profile-risk"', html)
        self.assertIn('id="profile-save"', html)
        self.assertIn('id="profile-message"', html)
        self.assertTrue(js_contains(html, "fetch('/api/profile'"))
        # Per request: the "Confirmed free transfers"/"Free transfers gameweek" override was
        # dropped from the form entirely -- UI-only, the backend fields stay nullable and untouched
        # (transfer_decisions.py's derive_free_transfers fallback already handles them being unset).
        self.assertNotIn('id="profile-free-transfers"', html)
        self.assertNotIn('id="profile-free-transfers-event"', html)
        self.assertNotIn('for="profile-free-transfers">Confirmed free transfers</label>', html)
        self.assertNotIn('for="profile-free-transfers-event">Free transfers gameweek</label>', html)
        # Per request: only the Team ID field is actually required to save (setupProfileForm's
        # own validation only blocks submission on a missing/invalid team ID -- timezone always
        # has a value because it's a <select>). Removed the reassurance copy since it read as a
        # claim about the fields themselves, not just this app's own auth.
        self.assertNotIn("no password, no account required", html)

    def test_no_account_no_password_reassurance_copy_removed_everywhere(self):
        # Follow-up per request: the same "no account required"/"no password" reassurance existed,
        # worded slightly differently, on four other panels beyond Manager profile -- Team lookup,
        # Deadline reminders, Contact Us, What's New's subscribe card -- plus a standalone "Account
        # boundary" panel on Model Status that existed solely to make this claim. Removed all of it.
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertNotIn("No account needed", html)
        self.assertNotIn("no account required", html)
        self.assertNotIn("No password is stored", html)
        self.assertNotIn("Account boundary", html)
        # The genuinely informative half of each subtitle survives -- only the reassurance clause
        # was stripped, not the whole line.
        self.assertIn('<h2>Look up a team</h2><span class="muted">Nothing is saved</span>', html)
        self.assertIn(
            '<h2>Deadline reminders</h2><span class="muted">One email before each gameweek deadline</span>',
            html,
        )
        self.assertIn(
            '<h2>Contact Us</h2><span class="muted">Report a bug, request a feature, or leave feedback</span>',
            html,
        )
        self.assertIn(
            'Get release notes by email</h2><span class="muted">One email each time a new entry publishes</span>',
            html,
        )
        # Issue #27: /api/profile is one of the four endpoints the shared refresh token no
        # longer gates -- the save request must not send it.
        self.assertNotIn("X-Refresh-Token", html)
        self.assertIn("Start the local dashboard service to edit your profile.", html)
        self.assertIn("Manager profile form", html)
        self.assertNotIn("Copy config/user-profile.example.json", html)

    def test_no_in_browser_refresh_control_or_token_remain(self):
        """Issue #27: the in-page "Refresh now" button, its wiring, and the refresh token were
        removed entirely -- /api/refresh is operator-only now, never reachable from the UI."""
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertNotIn("function runRefresh()", html)
        self.assertNotIn("function refreshToken()", html)
        self.assertNotIn("function refreshAvailable()", html)
        self.assertNotIn('id="refresh-now"', html)
        self.assertNotIn('meta[name="refresh-token"]', html)
        self.assertNotIn("X-Refresh-Token", html)


class DraftSquadTabRenderTests(unittest.TestCase):
    """Issue #152: the Draft Squad tab -- its own nav entry (moved out of "My Team"), explicit
    preseason-only purpose framing, a draft-health summary, and a session-only pitch view."""

    _STATE = {"fpl": {}, "transfers": [], "sources": []}

    def test_nav_button_and_view_section_render(self):
        html = render_dashboard(self._STATE)

        self.assertIn('data-view="draft">Draft Squad</button>', html)
        self.assertIn('id="view-draft" class="view"', html)
        self.assertIn('<option value="draft">Draft Squad</option>', html)

    def test_draft_squad_panel_moved_out_of_my_team_tab_into_its_own(self):
        html = render_dashboard(self._STATE)

        squad_start = html.index('<section id="view-squad"')
        draft_start = html.index('<section id="view-draft"')
        profile_start = html.index('<section id="view-profile"')
        self.assertLess(squad_start, draft_start)
        self.assertLess(draft_start, profile_start)
        self.assertNotIn('id="draft-squad-panel"', html[squad_start:draft_start])
        self.assertIn('id="draft-squad-panel"', html[draft_start:profile_start])

    def test_purpose_framing_present(self):
        html = render_dashboard(self._STATE)

        self.assertIn('id="draft-purpose-banner"', html)
        self.assertIn("Preseason only", html)
        self.assertIn("baselined off whatever you declare", html)

    def test_draft_health_panel_renders(self):
        html = render_dashboard(self._STATE)

        self.assertIn('id="draft-health-panel"', html)
        self.assertIn('id="draft-health-empty"', html)
        self.assertIn('id="draft-health-content" hidden', html)
        self.assertIn('id="draft-health-progression"', html)
        self.assertIn('id="draft-health-risks"', html)
        self.assertIn('id="draft-health-profiles"', html)
        self.assertIn('data-go="decisions"', html)

    def test_pitch_view_renders_with_always_visible_non_persistence_notice(self):
        # Follow-up to the initial ship: the pitch view and the squad builder merged into one
        # panel (players land on the pitch as they're added, instead of a separate flat
        # "selected squad" list above a separate pitch section) -- the notice moved with it, but
        # must stay just as visible, not a one-time toast, since the 15-player squad IS persisted.
        html = render_dashboard(self._STATE)

        self.assertIn(
            '<div id="draft-pitch-session-notice" class="limitation-note" role="note">'
            "Players land straight on the pitch below",
            html,
        )
        self.assertIn('id="draft-pitch-empty"', html)
        self.assertIn('id="draft-pitch"', html)
        self.assertIn('id="draft-bench"', html)
        # No longer a separate flat squad-grid list redundant with the pitch view below it.
        self.assertNotIn('id="draft-selected"', html)

    def test_draft_squad_editor_ids_unchanged_from_issue_61(self):
        html = render_dashboard(self._STATE)

        for element_id in [
            "draft-count", "draft-budget", "draft-quota", "draft-warnings",
            "draft-save-form", "draft-team-id", "draft-save", "draft-clear", "draft-message",
            "draft-search", "draft-club-filter", "draft-position-filter",
            "draft-locked-note",
        ]:
            self.assertIn(f'id="{element_id}"', html)

    def test_add_players_list_is_paginated_with_a_price_sort(self):
        # Follow-up: the "Add players" table silently truncated to 30 matches with no way to see
        # the rest, and had no way to sort by price. Now a real paginated list beside the pitch.
        html = render_dashboard(self._STATE)

        self.assertIn('id="draft-results-list"', html)
        self.assertIn('id="draft-results-count"', html)
        self.assertIn('id="draft-results-prev"', html)
        self.assertIn('id="draft-results-next"', html)
        self.assertIn('id="draft-results-page"', html)
        self.assertIn('id="draft-sort"', html)
        self.assertIn('value="price-asc"', html)
        self.assertIn('value="price-desc"', html)
        # And the add-players list sits beside the pitch, not below a separate squad list.
        self.assertIn('class="draft-builder-grid"', html)

    def test_save_and_clear_form_sits_above_the_pitch_grid(self):
        # Follow-up per live feedback: the save form (team ID + Save/Clear) used to sit at the
        # very bottom, below the whole pitch/add-players grid -- moved directly above the
        # "Starting XI" heading, right after the session notice.
        html = render_dashboard(self._STATE)

        save_form_start = html.index('<form id="draft-save-form"')
        grid_start = html.index('class="draft-builder-grid"')
        starting_xi_start = html.index('>Starting XI<')
        self.assertLess(save_form_start, grid_start)
        self.assertLess(save_form_start, starting_xi_start)

    def test_removing_the_last_player_clears_both_pitch_and_bench(self):
        # Regression guard: renderDraftPitchBuilding's early return for an empty squad used to
        # hide the pitch but never clear #draft-bench -- removing the very last player left a
        # stale bench card (and a stale Remove-button listener pointing at an id no longer in
        # draftSelection) on screen, so a second click on it was a silent no-op. Reported live as
        # "add a player and team is incomplete, you can not remove player even after clicking
        # remove."
        html = render_dashboard(self._STATE)

        early_return_start = js_search(html, "if(!squadPlayers.length){")
        _, early_return_end = js_span(html, "return;}", early_return_start)
        early_return_body = html[early_return_start:early_return_end]
        self.assertTrue(js_contains(early_return_body, "byId('draft-pitch').hidden=true"))
        self.assertTrue(js_contains(early_return_body, "byId('draft-pitch').innerHTML=''"))
        self.assertTrue(js_contains(early_return_body, "byId('draft-bench').innerHTML=''"))

    def test_saved_pitch_only_used_when_local_selection_matches_the_last_save(self):
        # Regression guard: renderDraftPitchSaved can only render players present in the last-
        # saved roll.squad -- a player added (or removed) locally since that save has no entry
        # there, so it used to silently vanish from both pitch and bench instead of appearing
        # without projections. Reported live as "I removed pedro and added watkins but it does
        # not show up on the pitch." Fix: only take the projected/saved render path when
        # draftSelection still matches what roll.squad actually contains; any local drift falls
        # back to the no-projections builder view, which reads live off draftSelection.
        html = render_dashboard(self._STATE)

        self.assertTrue(js_contains(html, "function draftSquadMatchesSaved(roll)"))
        dispatcher_start = js_search(html, "function renderDraftPitch(){")
        dispatcher_end = html.index(
            "}", js_search(html, "renderDraftPitchBuilding();", dispatcher_start)
        ) + 1
        dispatcher_body = html[dispatcher_start:dispatcher_end]
        self.assertTrue(js_contains(dispatcher_body, "roll&&draftSquadMatchesSaved(roll)"))

    def test_benching_the_only_starting_gkp_auto_promotes_the_other_one(self):
        # Live feedback: "when I bench GK, other one should automatically added in playing XI -
        # does not happen today." draftQuotas always carries GKP:2 with draftXiMax.GKP:1, so
        # there's never more than one benched GKP to choose between -- benching the starter
        # should auto-promote the other one instead of leaving the XI with zero goalkeepers.
        html = render_dashboard(self._STATE)

        self.assertTrue(
            js_contains(html, "function draftAutoFillAfterBench(benchedPosition,benchedId,squadById)")
        )
        bench_handler_start = js_search(html, "document.querySelectorAll('[data-draft-bench]')")
        bench_handler_end = js_search(html, "renderDraftPitch();}));", bench_handler_start)
        bench_handler_body = html[bench_handler_start:bench_handler_end]
        self.assertTrue(
            js_contains(bench_handler_body, "draftAutoFillAfterBench(benched.position_short,id,squadById)")
        )

    def test_add_and_remove_place_players_directly_on_the_pitch(self):
        html = render_dashboard(self._STATE)

        self.assertTrue(js_contains(html, "function addDraftPlayer(id)"))
        self.assertTrue(js_contains(html, "function removeDraftPlayer(id)"))
        self.assertTrue(js_contains(html, "function renderDraftBuilder()"))
        self.assertTrue(js_contains(html, "function renderDraftPitchBuilding()"))
        self.assertTrue(js_contains(html, "function renderDraftPitchSaved(roll)"))

    def test_no_reload_save_success_path_refreshes_draft_health_too(self):
        # Regression guard: the no-reload save success path called renderDraftBuilder() (pitch +
        # summary metrics + results list) but never renderDraftHealth() -- so after saving a
        # squad materially different from whatever was loaded at page-load time, the health
        # panel silently kept showing stale (or empty) data until the user left and re-entered
        # the Draft tab. `renderDraftBuilder()` alone doesn't touch #draft-health-* at all.
        html = render_dashboard(self._STATE)

        success_start = js_search(html, "refreshPayload.status==='ok'")
        success_end = js_search(html, "else{", success_start)
        success_branch = html[success_start:success_end]
        self.assertTrue(js_contains(success_branch, "renderDraftHealth()"))
        self.assertTrue(js_contains(success_branch, "renderDraftBuilder()"))

    def test_save_success_path_does_not_force_reseed_of_local_pitch_choices(self):
        # Issue #220 regression guard: the no-reload save success path used to force
        # `draftPitchSeededFor=null` right before re-rendering, which made `seedDraftPitch()`
        # unconditionally overwrite the user's local `draftStartingIds`/`draftCaptainId`/
        # `draftViceId` with the model's own recommendation on every single save -- reported live
        # as a vice-captain pick silently reverting the instant "Draft squad saved." appeared,
        # with no page reload involved. `seedDraftPitch`'s own squad-id-set key already reseeds
        # correctly on its own whenever the saved squad's membership actually changes, so the
        # success path shouldn't force it open itself.
        html = render_dashboard(self._STATE)

        success_start = js_search(html, "refreshPayload.status==='ok'")
        success_end = js_search(html, "else{", success_start)
        success_branch = html[success_start:success_end]
        self.assertFalse(js_contains(success_branch, "draftPitchSeededFor=null"))
        self.assertTrue(js_contains(success_branch, "renderDraftBuilder()"))

        # The guard itself must still be intact -- this is what makes a *membership* change
        # (not just a same-squad re-save) correctly reseed from the fresh model recommendation.
        seed_start = js_search(html, "function seedDraftPitch(roll)")
        seed_end = html.index("}", seed_start) + 1
        seed_body = html[seed_start:seed_end]
        self.assertTrue(js_contains(seed_body, "draftPitchSeededFor===key"))
        self.assertTrue(js_contains(seed_body, "draftPitchSeededFor=key"))

    def test_draft_pitch_session_notice_does_not_overclaim_reset_on_reload_only(self):
        # Issue #220: the old copy ("...reset on reload -- only the 15-player squad itself is
        # saved") implied local C/VC/XI choices persisted through a save, which was already false
        # before this fix's own bug and is still not literally true after it (they're still
        # client-only, never sent to /api/draft-squad) -- only *that* they now correctly survive
        # a same-squad re-save rather than resetting on every save.
        html = render_dashboard(self._STATE)

        notice_start = html.index('id="draft-pitch-session-notice"')
        notice_end = html.index("</div>", notice_start)
        notice_text = html[notice_start:notice_end]
        self.assertIn("not saved to your account", notice_text)
        self.assertIn("reset if you reload the page or change who's in the squad", notice_text)

    def test_js_pitch_and_health_helpers_present(self):
        html = render_dashboard(self._STATE)

        self.assertIn("function draftRollScenario()", html)
        self.assertIn("function renderDraftHealth()", html)
        self.assertIn("function renderDraftPitch()", html)
        self.assertIn("function seedDraftPitch(roll)", html)
        self.assertIn("draftXiMin", html)
        self.assertIn("draftXiMax", html)


class ProfileGatedTabsTests(unittest.TestCase):
    """Issue #108: Decision Center and Model Performance are each gated, at the tab level,
    behind an empty-state panel linking to My Profile when no profile is resolved for the
    visitor."""

    def test_decisions_and_performance_tabs_have_an_empty_state_and_a_content_wrapper(self):
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertIn('id="decisions-empty-state" class="placeholder" hidden', html)
        self.assertIn('id="decisions-content"', html)
        self.assertIn('id="performance-empty-state" class="placeholder" hidden', html)
        self.assertIn('id="performance-content"', html)

        # The empty-state wrapper opens right after the tab's own <section>, and the tab's
        # normal content is nested one level inside it -- so the gate can hide/show the whole
        # tab body in one place instead of every render function separately.
        decisions_start = html.index('<section id="view-decisions"')
        self.assertLess(
            decisions_start, html.index('id="decisions-empty-state"'),
        )
        self.assertLess(
            html.index('id="decisions-empty-state"'), html.index('id="decisions-content"'),
        )
        performance_start = html.index('<section id="view-performance"')
        self.assertLess(
            performance_start, html.index('id="performance-empty-state"'),
        )
        self.assertLess(
            html.index('id="performance-empty-state"'), html.index('id="performance-content"'),
        )

    def test_empty_states_link_clearly_to_my_profile(self):
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertIn(
            '<button class="refresh-button" type="button" data-go="profile" '
            'style="margin-top:12px">Go to My Profile</button></div><div id="decisions-content">',
            html,
        )
        self.assertIn(
            '<button class="refresh-button" type="button" data-go="profile" '
            'style="margin-top:12px">Go to My Profile</button></div><div id="performance-content">',
            html,
        )
        self.assertIn(
            "Decision Center recommendations are personalized to your team", html,
        )
        self.assertIn(
            "Model Performance tracks how this season's recommendations for your team compared "
            "with real results",
            html,
        )

    def test_gate_toggles_the_content_wrapper_off_the_same_not_configured_signal_used_elsewhere(self):
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertTrue(js_contains(html, "function applyProfileGates()"))
        self.assertTrue(
            js_contains(
                html, "const gated=(state.manager||{}).connection_status==='not_configured';"
            )
        )
        self.assertTrue(js_contains(html, "byId('decisions-content').hidden=gated;"))
        self.assertTrue(js_contains(html, "byId('decisions-empty-state').hidden=!gated;"))
        self.assertTrue(js_contains(html, "byId('performance-content').hidden=gated;"))
        self.assertTrue(js_contains(html, "byId('performance-empty-state').hidden=!gated;"))
        self.assertTrue(js_contains(html, "applyProfileGates();setupDecisionSubnav();"))

    def test_team_lookup_panel_on_my_team_is_untouched(self):
        """Issue #46's zero-commitment "Look up a team" form is explicitly out of scope."""
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertIn('id="team-lookup-panel"', html)
        self.assertNotIn('id="team-lookup-panel" class="placeholder"', html)
        # Not wrapped or hidden by anything -- still a direct child of view-squad, same as before.
        squad_start = html.index('<section id="view-squad"')
        lookup_start = html.index('id="team-lookup-panel"')
        between = html[squad_start:lookup_start]
        self.assertNotIn("empty-state", between)

    def test_profile_independent_tabs_have_no_gate(self):
        """Player Explorer, Fixtures, Transfers & News, and Model Status stay ungated."""
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertNotIn('id="players-empty-state"', html)
        self.assertNotIn('id="fixtures-empty-state"', html)
        self.assertNotIn('id="transfers-empty-state"', html)
        self.assertNotIn('id="model-empty-state"', html)

    def test_document_tags_stay_properly_nested_around_the_new_gate_wrappers(self):
        """Regression guard: an earlier version of this gate closed the new
        #decisions-content/#performance-content wrapper divs with a mismatched extra
        </div></section> pair, which silently popped </main>/.app/</body> closed early in real
        browser parsing (view-performance ended up a direct child of <body>, outside the
        sidebar layout entirely, rendering blank) while every plain substring-membership
        assertion above still passed. This walks the actual tag tree with html.parser and fails
        loudly on any open/close mismatch anywhere in the document, not just near the gate."""
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})
        void_elements = {
            "area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr",
        }

        class NestingChecker(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []
                self.errors = []

            def handle_starttag(self, tag, attrs):
                if tag not in void_elements:
                    self.stack.append((tag, self.getpos()))

            def handle_endtag(self, tag):
                if not self.stack:
                    self.errors.append(f"Unexpected closing </{tag}> at {self.getpos()}")
                    return
                top_tag, pos = self.stack[-1]
                if top_tag == tag:
                    self.stack.pop()
                else:
                    self.errors.append(
                        f"Expected </{top_tag}> (opened at {pos}) but got </{tag}> at {self.getpos()}"
                    )
                    for i in range(len(self.stack) - 1, -1, -1):
                        if self.stack[i][0] == tag:
                            self.stack = self.stack[:i]
                            break

        checker = NestingChecker()
        checker.feed(html)

        self.assertEqual(checker.errors, [])
        self.assertEqual(checker.stack, [])

        # And specifically: both view sections stay nested inside main.content, not popped out
        # to be direct children of <body> the way the mismatched-tag bug produced.
        main_start = html.index('<main class="content">')
        main_end = html.index("</main>")
        self.assertGreater(html.index('<section id="view-decisions"'), main_start)
        self.assertLess(html.index('<section id="view-decisions"'), main_end)
        self.assertGreater(html.index('<section id="view-performance"'), main_start)
        self.assertLess(html.index('<section id="view-performance"'), main_end)


if __name__ == "__main__":
    unittest.main()
