import re
import unittest

from fpl_intel.dashboard import render_dashboard


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
        self.assertIn("localStorage.getItem('fpl-theme')", html)
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
        self.assertIn('id="weekly-profile-options"', html)
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
        self.assertIn(
            "['decision-section-summary','decision-section-weekly',"
            "'decision-section-profiles','decision-section-xi',"
            "'decision-section-bench','decision-section-squad']",
            html,
        )

    def test_decision_scroll_targets_clear_sticky_subnav(self):
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertRegex(html, r"scroll-margin-top:\s*58px")
        self.assertIn("matchMedia('(prefers-reduced-motion: reduce)')", html)
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
        self.assertIn('aria-controls="weekly-profile-panel"', html)
        self.assertIn("tabindex=\"${profile.id===selected.id?'0':'-1'}\"", html)
        self.assertIn("['ArrowLeft','ArrowRight','Home','End']", html)
        self.assertIn("next.focus()", html)

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
        self.assertIn("deadline <= Date.now()", html)

    def test_page_load_restores_the_previously_saved_view_profile_and_filters(self):
        # Issue #27: `captureWorkspaceContext()`/`restoreWorkspaceContext()` predate the
        # now-removed in-browser refresh flow (their only caller was `runRefresh()`, which
        # captured context right before the client-side reload it triggered) -- both function
        # definitions remain, and `restoreWorkspaceContext()` is still invoked on every page
        # load, so a workspace-context snapshot from a previous session is still restored.
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertIn("function captureWorkspaceContext()", html)
        self.assertIn("function restoreWorkspaceContext()", html)
        self.assertIn("'fpl-workspace-context'", html)
        self.assertIn("restoreWorkspaceContext();", html)

    def test_model_performance_collection_errors_are_visible(self):
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertIn('id="performance-errors"', html)
        self.assertIn("performance.collection_errors||[]", html)
        self.assertIn("Result collection issue", html)

    def test_invalid_or_untrusted_source_urls_render_as_text_not_links(self):
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertIn("function safeLink(url,label)", html)
        self.assertIn("parsed.protocol==='https:'", html)
        self.assertIn("trustedLinkDomains.has(host)", html)
        self.assertNotIn("['http:','https:'].includes(parsed.protocol)", html)
        self.assertIn("safeLink(source.url,source.name)", html)
        self.assertIn("safeLink(url,`Source ${index+1}`)", html)
        self.assertIn("safeLink(rules.source,'Official FPL rules')", html)

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
        self.assertIn("showView(titles[context.view]?context.view:'squad')", html)

    def test_renders_manager_profile_form(self):
        html = render_dashboard({"fpl": {}, "transfers": [], "sources": []})

        self.assertIn('id="profile-settings"', html)
        self.assertIn('id="view-profile" class="view"', html)
        self.assertIn('data-view="profile">My Profile</button>', html)
        self.assertIn('id="profile-form"', html)
        self.assertIn('id="profile-team-id"', html)
        self.assertIn('id="profile-timezone"', html)
        self.assertIn('id="profile-risk"', html)
        self.assertIn('id="profile-free-transfers"', html)
        self.assertIn('id="profile-free-transfers-event"', html)
        self.assertIn('id="profile-save"', html)
        self.assertIn('id="profile-message"', html)
        self.assertIn("fetch('/api/profile'", html)
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


if __name__ == "__main__":
    unittest.main()
