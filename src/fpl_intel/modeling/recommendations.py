"""Transparent preseason projections and legal opening-squad recommendations."""

import math
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone

from . import minutes as minutes_model
from . import team_strength
from .coefficients import load_coefficients
from .projection import component_points_for_event, component_rate_baselines, player_component_rates
from ..sources.transfers import canonical_club


_COEFFICIENTS = load_coefficients()
_POSITION_CODES = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
_UNAVAILABLE = {"i", "s", "u", "n"}
_UNCERTAINTY_BANDS = _COEFFICIENTS["uncertainty_bands"]
_EP_NEXT_BLEND_WEIGHT = _COEFFICIENTS["ep_next_blend_weight"]


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _availability_multiplier(player):
    status = player.get("status") or "a"
    if status in _UNAVAILABLE:
        return 0.0
    if status == "d":
        chance = player.get("chance_of_playing_next_round")
        return _number(chance, 75.0) / 100
    return 1.0


def _expected_minutes(player, fixtures_played=38):
    """Estimate expected minutes from starts and minutes per team fixture played.

    The denominator is the number of completed fixtures for the player's club,
    not the number of elapsed Gameweeks. This preserves legitimate double-Gameweek
    appearances and avoids treating blanks as matches in which the player was benched.

    A player with zero recorded minutes AND zero starts for their current club --
    a genuine PL debutant, a permanent transfer, or a returning loanee, not merely
    a fringe player with a thin record -- has no track record for the 0.55/0.45
    blend above to work with, so it collapses to the "never started" baseline
    regardless of price or reputation (e.g. a marquee summer signing). For that
    specific zero-record population only, floor the estimate using FPL's own
    ep_next, which already reflects official expectations about the player's
    likely role. Anyone with any real minutes or starts keeps using their own
    observed rate untouched, so ep_next still never overrides an established
    player's track record (see test_ep_next_affects_only_first_event_points_not_expected_minutes).
    """
    availability = _availability_multiplier(player)
    if not availability:
        return 0.0
    fixtures_played = max(1.0, min(38.0, _number(fixtures_played, 38.0)))
    starts = min(fixtures_played, _number(player.get("starts")))
    minutes = min(90.0 * fixtures_played, _number(player.get("minutes")))
    historical = 0.55 * (minutes / fixtures_played) + 0.45 * (8 + 78 * starts / fixtures_played)
    if minutes <= 0 and starts <= 0:
        official_ep = _number(player.get("ep_next"))
        ep_floor = 60.0 if official_ep >= 3 else 45.0 if official_ep >= 2 else 25.0 if official_ep > 0 else 0.0
        historical = max(historical, ep_floor)
    return round(min(86.0, historical) * availability, 1)


def _confirmed_matched_transfers_within_window(recent_transfers, as_of, window_days):
    """Confirmed, FPL-matched transfers announced within window_days of as_of."""
    if not recent_transfers:
        return []
    reference = datetime.fromisoformat(str(as_of).replace("Z", "+00:00")) if as_of else datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    qualifying = []
    for transfer in recent_transfers:
        player_id = transfer.get("matched_fpl_element_id")
        announced_at = transfer.get("announced_at")
        if not player_id or not announced_at:
            continue
        if transfer.get("verification_status") != "confirmed_first_party":
            continue
        if transfer.get("fpl_reconciliation_status") != "matched_current_fpl":
            continue
        announced = datetime.fromisoformat(str(announced_at).replace("Z", "+00:00"))
        if announced.tzinfo is None:
            announced = announced.replace(tzinfo=timezone.utc)
        age_days = (reference.astimezone(timezone.utc) - announced.astimezone(timezone.utc)).total_seconds() / 86400
        if 0 <= age_days <= window_days:
            qualifying.append(transfer)
    return qualifying


def _recent_role_transitions(recent_transfers, as_of, window_days=60):
    return {
        int(transfer["matched_fpl_element_id"]): transfer
        for transfer in _confirmed_matched_transfers_within_window(recent_transfers, as_of, window_days)
    }


def _minutes_scenarios(base_minutes, role_transition):
    if not role_transition:
        value = round(base_minutes, 1)
        return {"conservative": value, "balanced": value, "aggressive": value}
    return {
        "conservative": round(min(55.0, base_minutes * 0.62), 1),
        "balanced": round(min(72.0, base_minutes * 0.78), 1),
        "aggressive": round(min(82.0, base_minutes * 0.92), 1),
    }


_TEAMMATE_DEPARTURE_MINUTES_MULTIPLIERS = {"conservative": 1.0, "balanced": 1.08, "aggressive": 1.18}
_TEAMMATE_ARRIVAL_MINUTES_MULTIPLIERS = {"conservative": 0.75, "balanced": 0.90, "aggressive": 1.0}
_TEAMMATE_MINUTES_CAP = 90.0
_TEAMMATE_IMPACT_CONSERVATIVE_MULTIPLIER = 0.93
_TEAMMATE_IMPACT_AGGRESSIVE_MULTIPLIER = 1.10


def _teammate_transfer_impacts(recent_transfers, bootstrap, as_of, window_days=60):
    """Same-club, same-position teammates of a player who recently left or joined.

    A confirmed departure or arrival changes the real competition for minutes
    at that position for the players who *stayed* -- not just for the
    transferred player themselves, who instead gets the stronger
    _recent_role_transitions() treatment (see project_players(), which skips
    this for any player already flagged there). Returns {player_id: "out" |
    "in"}. A player affected by both an arrival and a departure in the same
    window keeps "out": a confirmed departure frees a specific minutes share,
    while a new arrival's actual effect on the XI is comparatively less
    certain, so departure is the stronger signal when both apply.
    """
    qualifying = _confirmed_matched_transfers_within_window(recent_transfers, as_of, window_days)
    if not qualifying:
        return {}

    elements = bootstrap.get("elements", [])
    position_by_id = {
        int(player["id"]): player.get("element_type") for player in elements if player.get("id") is not None
    }
    team_id_by_canonical_name = {
        canonical_club(team.get("name")): team.get("id")
        for team in bootstrap.get("teams", [])
        if team.get("name") and team.get("id") is not None
    }
    teammates_by_team_and_position = defaultdict(list)
    for player in elements:
        team_id, position_id, player_id = player.get("team"), player.get("element_type"), player.get("id")
        if team_id is not None and position_id is not None and player_id is not None:
            teammates_by_team_and_position[(team_id, position_id)].append(int(player_id))

    departures, arrivals = set(), set()
    for transfer in qualifying:
        moved_player_id = int(transfer["matched_fpl_element_id"])
        position_id = position_by_id.get(moved_player_id)
        if position_id is None:
            continue
        from_team_id = team_id_by_canonical_name.get(canonical_club(transfer.get("from_club")))
        to_team_id = team_id_by_canonical_name.get(canonical_club(transfer.get("to_club")))
        # Derive both sides directly from from_club/to_club rather than a
        # single movement_type/premier_league_club: an intra-Premier-League
        # move is typically reported by *both* clubs' own transfer-centre
        # feeds (the selling club's "out" and the buying club's "in"), but
        # refresh.py's cross-source dedup (_merge_transfer_candidates) keeps
        # only one merged record per move, so which single movement_type
        # survives is not reliable. from_club/to_club both remain on the
        # merged record regardless, so a club only counts as affected once
        # its own name resolves to a real bootstrap team, independent of
        # which side happened to survive the merge.
        if from_team_id is not None and from_team_id != to_team_id:
            for teammate_id in teammates_by_team_and_position.get((from_team_id, position_id), []):
                if teammate_id != moved_player_id:
                    departures.add(teammate_id)
        if to_team_id is not None and to_team_id != from_team_id:
            for teammate_id in teammates_by_team_and_position.get((to_team_id, position_id), []):
                if teammate_id != moved_player_id:
                    arrivals.add(teammate_id)

    impacts = {player_id: "in" for player_id in arrivals}
    impacts.update({player_id: "out" for player_id in departures})
    return impacts


def _teammate_minutes_scenarios(base_minutes, teammate_impact):
    if not teammate_impact:
        value = round(base_minutes, 1)
        return {"conservative": value, "balanced": value, "aggressive": value}
    multipliers = (
        _TEAMMATE_DEPARTURE_MINUTES_MULTIPLIERS if teammate_impact == "out" else _TEAMMATE_ARRIVAL_MINUTES_MULTIPLIERS
    )
    return {
        profile: round(min(_TEAMMATE_MINUTES_CAP, base_minutes * multiplier), 1)
        for profile, multiplier in multipliers.items()
    }


def _next_event_id(bootstrap):
    events = bootstrap.get("events", [])
    explicit = next((event.get("id") for event in events if event.get("is_next")), None)
    if explicit:
        return int(explicit)
    unfinished = [int(event["id"]) for event in events if event.get("id") and not event.get("finished")]
    return min(unfinished) if unfinished else 1


def _fixture_by_team(fixtures, start_event, horizon):
    schedule = {}
    stop_event = start_event + horizon
    for fixture in fixtures:
        event = fixture.get("event")
        if not event or event < start_event or event >= stop_event:
            continue
        schedule.setdefault((fixture.get("team_h"), event), []).append(
            {"difficulty": fixture.get("team_h_difficulty") or 3, "opponent": fixture.get("team_a"), "is_home": True}
        )
        schedule.setdefault((fixture.get("team_a"), event), []).append(
            {"difficulty": fixture.get("team_a_difficulty") or 3, "opponent": fixture.get("team_h"), "is_home": False}
        )
    return schedule


def _fixtures_played_by_team(fixtures, start_event):
    counts = Counter()
    for fixture in fixtures:
        event = int(fixture.get("event") or 0)
        if not event or event >= start_event:
            continue
        completed = (
            fixture.get("finished") is True
            or fixture.get("started") is True
            or fixture.get("team_h_score") is not None
            or fixture.get("team_a_score") is not None
        )
        if not completed:
            continue
        counts[int(fixture.get("team_h") or 0)] += 1
        counts[int(fixture.get("team_a") or 0)] += 1
    return counts


def project_players(
    bootstrap, fixtures, horizon=5, start_event=None, recent_transfers=None, as_of=None,
    expected_minutes_override=None,
):
    """Project players over a rolling official-event horizon without betting markets.

    Scoring is additive by component (see .projection): appearance, attacking
    (goals/assists), clean sheet, goals conceded, saves, and bonus, each shrunk
    toward a positional baseline and adjusted by official FDR. Expected minutes
    are estimated conservatively from season-to-date starts and minutes. The
    first projected event also blends in the official FPL ``ep_next`` estimate.

    ``expected_minutes_override`` (default ``None``, preserving existing behavior
    exactly): an optional ``(player, fixtures_played, availability_multiplier) -> float``
    callable that fully replaces the champion's own expected-minutes estimate (both the
    season-average and Phase 4 recency-weighted branches) for every player, wherever it is
    supplied. Nothing in this codebase passes it today except `ml_minutes.build_shadow_forecast`
    (issue #65) computing its own, separate, never-live-facing forecast -- see that module's
    docstring. Everything downstream of expected minutes (opponent strength, component
    scoring, bonus/residual, uncertainty bands, `component_xp`) is untouched either way.
    """
    start_event = int(start_event or _next_event_id(bootstrap))
    projection_events = list(range(start_event, start_event + horizon))
    players = bootstrap.get("elements", [])
    team_by_id = {team.get("id"): team for team in bootstrap.get("teams", [])}
    played_by_team = _fixtures_played_by_team(fixtures, start_event)
    preseason_fixtures = 38 if start_event <= 1 else None
    component_baselines = component_rate_baselines(players)
    schedule = _fixture_by_team(fixtures, start_event, horizon)
    role_transitions = _recent_role_transitions(recent_transfers, as_of)
    teammate_transfer_impacts = _teammate_transfer_impacts(recent_transfers, bootstrap, as_of)

    # Phase 1: fitted team-strength ratings replace the FDR difficulty-bucket
    # tables once enough same-season matches exist to fit reliably; before
    # that (early season), fall back to the Phase 3 FDR tables -- see
    # team_strength.py for why cross-season seeding isn't done here.
    use_team_strength = team_strength.should_use_team_strength(
        team_strength.completed_rounds(fixtures, start_event)
    )
    team_strength_ratings = (
        team_strength.fit_team_strength(team_strength.matches_from_fixtures(fixtures, start_event))
        if use_team_strength else None
    )

    def _team_strength_inputs(team_id, fixture_info):
        if not use_team_strength:
            return None, None, None
        home_team, away_team = (
            (team_id, fixture_info["opponent"]) if fixture_info["is_home"] else (fixture_info["opponent"], team_id)
        )
        expected_home, expected_away = team_strength.expected_goals(team_strength_ratings, home_team, away_team)
        expected_for, expected_against = (
            (expected_home, expected_away) if fixture_info["is_home"] else (expected_away, expected_home)
        )
        return expected_for, expected_against, team_strength_ratings["league_avg_goals"]

    projections = []
    for player in players:
        position_id = player.get("element_type")
        minutes = _number(player.get("minutes"))
        rates = player_component_rates(player, component_baselines)
        recent_history = player.get("recent_history")
        # Phase 4: recency-weighted minutes when enough recent per-gameweek
        # history exists; otherwise fall back to the season-average estimate
        # (true preseason, or a player with too few recorded appearances).
        use_recency_minutes = bool(recent_history) and minutes_model.should_use_recency_model(recent_history)
        team_fixtures_played = (
            preseason_fixtures
            if preseason_fixtures is not None
            else int(played_by_team.get(int(player.get("team") or 0), max(1, start_event - 1)))
        )
        if expected_minutes_override is not None:
            # issue #65 shadow challenger: fully replaces both branches below, never used
            # for a live recommendation -- see this parameter's docstring above.
            base_expected_minutes = expected_minutes_override(
                player, team_fixtures_played, _availability_multiplier(player)
            )
        elif use_recency_minutes:
            base_expected_minutes = minutes_model.expected_minutes_from_history(
                recent_history, availability_multiplier=_availability_multiplier(player)
            )
        else:
            base_expected_minutes = _expected_minutes(player, team_fixtures_played)
        role_transition = role_transitions.get(int(player.get("id") or 0))
        # A player who moved themselves already gets the stronger role-transition
        # treatment below; only apply the softer teammate-impact adjustment to
        # players whose *own* situation is otherwise unchanged.
        teammate_impact = None if role_transition else teammate_transfer_impacts.get(int(player.get("id") or 0))
        if role_transition:
            # A recent confirmed move to a new club is a different, more severe
            # kind of uncertainty than in-club rotation risk -- keep the
            # existing role-transition scenario treatment regardless of
            # whether a recency-weighted minutes history is also available.
            expected_minutes_scenarios = _minutes_scenarios(base_expected_minutes, role_transition)
        elif teammate_impact:
            # Same precedence reasoning as role_transition above: a same-
            # position teammate's confirmed move is more specific, current
            # information than a season-to-date recency-weighted history.
            expected_minutes_scenarios = _teammate_minutes_scenarios(base_expected_minutes, teammate_impact)
        elif use_recency_minutes:
            expected_minutes_scenarios = minutes_model.minutes_scenarios_from_history(
                recent_history, availability_multiplier=_availability_multiplier(player)
            )
        else:
            expected_minutes_scenarios = _minutes_scenarios(base_expected_minutes, role_transition)
        expected_minutes = expected_minutes_scenarios["balanced"]
        fixture_points = []
        scenario_fixture_points = {
            profile: [] for profile in ("conservative", "balanced", "aggressive")
        }
        component_xp = []
        difficulties = []
        for relative_index, event in enumerate(projection_events):
            event_fixtures = schedule.get((player.get("team"), event), [])
            event_scenarios = {}
            for profile, scenario_minutes in expected_minutes_scenarios.items():
                scenario_points = 0.0
                for fixture_info in event_fixtures:
                    if scenario_minutes == 0:
                        continue
                    expected_for, expected_against, league_avg_goals = _team_strength_inputs(
                        player.get("team"), fixture_info
                    )
                    scenario_points += component_points_for_event(
                        rates, position_id, scenario_minutes, fixture_info["difficulty"],
                        expected_goals_for=expected_for, expected_goals_against=expected_against,
                        league_avg_goals=league_avg_goals,
                    )["total"]
                if relative_index == 0 and scenario_points > 0:
                    official_ep = _number(player.get("ep_next"))
                    if official_ep > 0:
                        minutes_ratio = scenario_minutes / expected_minutes if expected_minutes else 0.0
                        blend = _EP_NEXT_BLEND_WEIGHT
                        scenario_points = (1 - blend) * scenario_points + blend * official_ep * minutes_ratio
                event_scenarios[profile] = max(0.0, scenario_points)
            difficulties.append([fixture_info["difficulty"] for fixture_info in event_fixtures])
            fixture_points.append(round(event_scenarios["balanced"], 2))
            for profile in scenario_fixture_points:
                scenario_fixture_points[profile].append(round(event_scenarios[profile], 2))

            # Auditability breakdown (balanced profile only) of what the total is made of.
            event_components = {
                "appearance": 0.0, "attacking": 0.0, "clean_sheet": 0.0,
                "goals_conceded": 0.0, "defensive_contribution": 0.0,
                "saves": 0.0, "bonus": 0.0, "residual": 0.0,
            }
            for fixture_info in event_fixtures:
                if expected_minutes == 0:
                    continue
                expected_for, expected_against, league_avg_goals = _team_strength_inputs(
                    player.get("team"), fixture_info
                )
                fixture_components = component_points_for_event(
                    rates, position_id, expected_minutes, fixture_info["difficulty"],
                    expected_goals_for=expected_for, expected_goals_against=expected_against,
                    league_avg_goals=league_avg_goals,
                )
                for key in event_components:
                    event_components[key] += fixture_components[key]
            modeled_total = sum(event_components.values())
            blended_total = fixture_points[-1]
            opponents = [
                {
                    "club_short": team_by_id.get(fixture_info["opponent"], {}).get("short_name", "UNK"),
                    "is_home": fixture_info["is_home"],
                    "difficulty": fixture_info["difficulty"],
                }
                for fixture_info in event_fixtures
            ]
            component_xp.append({
                **{key: round(value, 2) for key, value in event_components.items()},
                "modeled_total_before_ep_next": round(modeled_total, 2),
                "ep_next_adjustment": round(blended_total - modeled_total, 2),
                "blended_total": round(blended_total, 2),
                "opponents": opponents,
            })
        xp_1 = sum(fixture_points[:1])
        xp_3 = sum(fixture_points[:3])
        xp_5 = sum(fixture_points[:5])
        confidence = "low" if role_transition else "high" if minutes >= 2400 and player.get("status") == "a" else "medium" if minutes >= 1200 and player.get("status") not in _UNAVAILABLE else "low"
        uncertainty = _UNCERTAINTY_BANDS[confidence]
        if role_transition:
            profile_fixture_xp = {
                "conservative": [round(points * 0.84, 2) for points in scenario_fixture_points["conservative"]],
                "balanced": fixture_points,
                "aggressive": [round(points * 1.16, 2) for points in scenario_fixture_points["aggressive"]],
            }
        elif teammate_impact:
            # Softer than the role_transition band above -- this is a second-
            # order effect (a teammate's move, not the player's own), so the
            # range widens less.
            profile_fixture_xp = {
                "conservative": [
                    round(points * _TEAMMATE_IMPACT_CONSERVATIVE_MULTIPLIER, 2) for points in fixture_points
                ],
                "balanced": fixture_points,
                "aggressive": [
                    round(points * _TEAMMATE_IMPACT_AGGRESSIVE_MULTIPLIER, 2) for points in fixture_points
                ],
            }
        else:
            profile_fixture_xp = {
                "conservative": [round(points * (1 - uncertainty), 2) for points in fixture_points],
                "balanced": fixture_points,
                "aggressive": [round(points * (1 + uncertainty), 2) for points in fixture_points],
            }
        lower_1 = sum(profile_fixture_xp["conservative"][:1])
        lower_3 = sum(profile_fixture_xp["conservative"][:3])
        lower_5 = sum(profile_fixture_xp["conservative"][:5])
        upper_1 = sum(profile_fixture_xp["aggressive"][:1])
        upper_3 = sum(profile_fixture_xp["aggressive"][:3])
        upper_5 = sum(profile_fixture_xp["aggressive"][:5])
        team = team_by_id.get(player.get("team"), {})
        projections.append(
            {
                "id": player.get("id"),
                "name": player.get("web_name"),
                "full_name": " ".join(part for part in [player.get("first_name"), player.get("second_name")] if part),
                "club": team.get("name"),
                "club_short": team.get("short_name"),
                "team_id": player.get("team"),
                "position_id": position_id,
                "position_short": _POSITION_CODES.get(position_id, "UNK"),
                "price": _number(player.get("now_cost")) / 10,
                "ownership": _number(player.get("selected_by_percent")),
                "status": player.get("status"),
                "news": player.get("news") or "",
                "expected_minutes": expected_minutes,
                "team_fixtures_played": team_fixtures_played,
                "expected_minutes_before_role_adjustment": base_expected_minutes,
                "expected_minutes_scenarios": expected_minutes_scenarios,
                "role_transition": bool(role_transition),
                "role_transition_note": (
                    "Recent confirmed move to a new club; minutes are scenario-adjusted until the role is established."
                    if role_transition else ""
                ),
                "teammate_transfer_impact": teammate_impact,
                "teammate_transfer_impact_note": (
                    "A same-position teammate's confirmed departure may open up minutes; scenarios are adjusted accordingly."
                    if teammate_impact == "out" else
                    "A same-position teammate's confirmed arrival adds competition for minutes; scenarios are adjusted accordingly."
                    if teammate_impact == "in" else ""
                ),
                "fixture_difficulties": difficulties,
                "uses_team_strength": use_team_strength,
                "uses_recency_minutes": use_recency_minutes,
                "projection_events": projection_events,
                "fixture_xp": fixture_points,
                "profile_fixture_xp": profile_fixture_xp,
                "component_xp": component_xp,
                "xp_1": round(xp_1, 2),
                "xp_3": round(xp_3, 2),
                "xp_5": round(xp_5, 2),
                "lower_1": round(lower_1, 2),
                "upper_1": round(upper_1, 2),
                "lower_3": round(lower_3, 2),
                "upper_3": round(upper_3, 2),
                "lower_5": round(lower_5, 2),
                "upper_5": round(upper_5, 2),
                "confidence": confidence,
                "historical_minutes": int(minutes),
                "historical_points": int(_number(player.get("total_points"))),
                "official_ep_next": _number(player.get("ep_next")),
                "can_select": player.get("can_select", True) is not False and not player.get("removed", False),
            }
        )
    return projections


_PROFILE_DEFINITIONS = {
    "conservative": {
        "label": "Conservative",
        "summary": "Minutes security and downside protection",
        "risk_note": "May give up ceiling and differential exposure for a more dependable floor.",
        "objective": "Lower projection, expected minutes, role confidence, and stronger reserves",
    },
    "balanced": {
        "label": "Balanced",
        "summary": "Central projection with moderate uncertainty",
        "risk_note": "Best default for the overall-rank objective, but still exposed to preseason role changes.",
        "objective": "Central five-gameweek projection, captaincy value, and usable bench cover",
    },
    "aggressive": {
        "label": "Aggressive",
        "summary": "Upper projection and greater variance",
        "risk_note": "Accepts more variance and some role uncertainty; low ownership is never sufficient by itself.",
        "objective": "Upper projection plus bounded differential value, with a severe-minutes penalty",
    },
}


def _profile_player_score(player, profile, horizon=5):
    suffix = str(horizon)
    central = player[f"xp_{suffix}"]
    if profile == "conservative":
        # The uncertainty-adjusted lower estimate already reflects minutes confidence.
        # Keep the objective directly comparable to the displayed downside metric.
        return player[f"lower_{suffix}"]
    if profile == "aggressive":
        differential_bonus = min(0.3, max(0.0, 20.0 - player["ownership"]) * 0.015)
        minutes_penalty = max(0.0, 50.0 - player["expected_minutes"]) * 0.045
        return player[f"upper_{suffix}"] + differential_bonus - minutes_penalty
    return central


def _best_xi(squad, key):
    score = key if callable(key) else lambda player: player[key]
    by_position = {
        code: sorted(
            (player for player in squad if player["position_short"] == code),
            key=score,
            reverse=True,
        )
        for code in _POSITION_CODES.values()
    }
    best = None
    for defenders in range(3, 6):
        for midfielders in range(2, 6):
            forwards = 10 - defenders - midfielders
            if forwards < 1 or forwards > 3:
                continue
            outfield = (
                by_position["DEF"][:defenders]
                + by_position["MID"][:midfielders]
                + by_position["FWD"][:forwards]
            )
            if len(outfield) != 10:
                continue
            lineup = by_position["GKP"][:1] + outfield
            lineup_score = sum(score(player) for player in lineup)
            if best is None or lineup_score > best[0]:
                best = (lineup_score, lineup, f"{defenders}-{midfielders}-{forwards}")
    return best[1], best[2]


def _profile_event_score(player, profile, event_index, horizon, cache=None):
    # Issue #176: pure for a given (player id, profile, event_index, horizon) -- reads only
    # static per-player projection fields that don't change within one build_gw_recommendations/
    # build_transfer_decisions/build_draft_decisions call. _optimize_squad's simulated-annealing
    # search alone calls this (via _squad_objective -> _event_lineup_schedule) up to tens of
    # thousands of times per profile with no caching anywhere -- profiled at ~33.5M calls for one
    # real-scale build_transfer_decisions call, the single hottest function in that profile.
    # cache=None (every call site that doesn't opt in) keeps today's uncached behavior exactly.
    cache_key = None
    if cache is not None:
        cache_key = (player["id"], profile, event_index, horizon)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    profile_points = (player.get("profile_fixture_xp") or {}).get(profile)
    if profile_points is None or event_index >= len(profile_points):
        profile_points = player.get("fixture_xp") or []
    points = float(profile_points[event_index]) if event_index < len(profile_points) else 0.0
    if profile == "aggressive":
        differential = min(0.3, max(0.0, 20.0 - player.get("ownership", 0.0)) * 0.015)
        minutes_penalty = max(0.0, 50.0 - player.get("expected_minutes", 0.0)) * 0.045
        points += (differential - minutes_penalty) / max(1, horizon)
    if cache_key is not None:
        cache[cache_key] = points
    return points


def _event_lineup_schedule(squad, profile, horizon=5, include_value_bands=True, sort_bench=True, cache=None):
    """Optimize XI, bench order, captain, and vice-captain independently by event.

    ``include_value_bands`` and ``sort_bench`` default to True so every existing
    caller's output is unchanged. The simulated-annealing search in
    _optimize_squad() calls this many tens of thousands of times per
    build_gw_recommendations() run and only ever reads ``profile_points`` and
    ``bench_player_ids`` (as an unordered set, for a sum) from the result -- the
    central/lower/upper value bands and the bench sort order were being computed
    and thrown away on every single one of those calls. Both are skippable there
    without changing which squad the search converges on, since neither affects
    ``profile_points`` or which player IDs end up in the lineup or on the bench.
    """
    schedule = []
    for event_index in range(horizon):
        score = lambda player, index=event_index: _profile_event_score(player, profile, index, horizon, cache=cache)
        lineup, formation = _best_xi(squad, score)
        lineup_ids = {player["id"] for player in lineup}
        bench_candidates = [player for player in squad if player["id"] not in lineup_ids]
        if sort_bench:
            bench = sorted(
                (player for player in bench_candidates if player["position_short"] != "GKP"),
                key=score,
                reverse=True,
            ) + sorted(
                (player for player in bench_candidates if player["position_short"] == "GKP"),
                key=score,
                reverse=True,
            )
        else:
            bench = bench_candidates
        captaincy = sorted(lineup, key=score, reverse=True)
        captain, vice_captain = captaincy[:2]

        row = {
            "event_index": event_index,
            "formation": formation,
            "lineup_player_ids": [player["id"] for player in lineup],
            "bench_player_ids": [player["id"] for player in bench],
            "captain_id": captain["id"],
            "vice_captain_id": vice_captain["id"],
            "profile_points": sum(score(player) for player in lineup) + score(captain),
        }
        if include_value_bands:
            def total(series):
                values = []
                for player in lineup:
                    points = series(player)
                    values.append(float(points[event_index]) if event_index < len(points) else 0.0)
                captain_points = series(captain)
                captain_value = float(captain_points[event_index]) if event_index < len(captain_points) else 0.0
                return sum(values) + captain_value

            # Issue #181: central_points now equals profile_points above -- the same
            # profile-adjusted score (_profile_event_score) the lineup/transfer search itself
            # ranks candidates by, not the plain, risk-blind fixture_xp it used to read. Before
            # this, a transfer the search correctly favored (e.g. conservative trading a little
            # raw upside for a lot more reliability) could still be *reported* to the user as a
            # point loss, because this number never reflected the profile's own risk view at all --
            # confirmed on real data while investigating #181's multi-leg search: for a squad with
            # a low-confidence, high-projection player, conservative's search valued swapping him
            # out at +0.73/GW (0.09 -> 0.82, on the discounted scale it actually optimizes), while
            # this field reported the identical swap as a 0.17/GW *loss* (4.74 -> 4.57 on the plain
            # scale it used to read) -- the same decision, judged as a win by one number and a loss
            # by the other. lower_points/upper_points intentionally still read the fixed
            # conservative/aggressive bounds regardless of `profile` -- those describe the full
            # plausible outcome range for the uncertainty-interval display, not "what this profile
            # expects," a different and still-valid use. Bench value remains excluded here (XI
            # only, matching this field's pre-existing scope) even though _squad_objective (the
            # search's own ranking metric) does credit bench depth -- a separate, smaller gap left
            # as-is.
            row["central_points"] = row["profile_points"]
            row["lower_points"] = total(lambda player: (player.get("profile_fixture_xp") or {}).get("conservative") or [])
            row["upper_points"] = total(lambda player: (player.get("profile_fixture_xp") or {}).get("aggressive") or [])
        schedule.append(row)
    return schedule


def _team_uncertainty_interval(squad, profile, horizon=5, schedule=None, cache=None):
    """Aggregate marginal player bands with declared covariance assumptions.

    Player downside/upside values are marginal ranges, so summing every extreme
    assumes all errors happen together. Instead, aggregate squared half-widths
    with modest same-event and cross-event correlations. This remains a modeled
    interval until enough immutable team forecasts exist for empirical calibration.
    """
    schedule = schedule or _event_lineup_schedule(squad, profile, horizon, cache=cache)
    by_id = {player["id"]: player for player in squad}
    same_event_correlation = 0.12
    cross_event_correlation = 0.08
    event_variances = []
    central = 0.0
    for event in schedule:
        event_index = event["event_index"]
        weights = Counter(event["lineup_player_ids"])
        weights[event["captain_id"]] += 1
        marginal_widths = []
        for player_id, weight in weights.items():
            player = by_id[player_id]
            central_series = player.get("fixture_xp") or []
            lower_series = (player.get("profile_fixture_xp") or {}).get("conservative") or []
            upper_series = (player.get("profile_fixture_xp") or {}).get("aggressive") or []
            center = float(central_series[event_index]) if event_index < len(central_series) else 0.0
            lower = float(lower_series[event_index]) if event_index < len(lower_series) else center
            upper = float(upper_series[event_index]) if event_index < len(upper_series) else center
            central += weight * center
            marginal_widths.append(weight * max(center - lower, upper - center, 0.0))
        independent = sum(width * width for width in marginal_widths)
        shared = sum(marginal_widths) ** 2
        event_variances.append(
            (1 - same_event_correlation) * independent + same_event_correlation * shared
        )
    independent_events = sum(event_variances)
    shared_events = sum(math.sqrt(value) for value in event_variances) ** 2
    variance = (
        (1 - cross_event_correlation) * independent_events
        + cross_event_correlation * shared_events
    )
    half_width = math.sqrt(max(0.0, variance))
    return {
        "central": round(central, 2),
        "lower": round(max(0.0, central - half_width), 2),
        "upper": round(central + half_width, 2),
        "half_width": round(half_width, 2),
        "method": "covariance_adjusted_marginal_ranges",
        "same_event_correlation": same_event_correlation,
        "cross_event_correlation": cross_event_correlation,
    }


def _squad_objective(squad, profile="balanced", horizon=5, cache=None):
    # Called from _optimize_squad()'s simulated-annealing inner loop -- up to
    # tens of thousands of times per profile -- so skip the value-band totals
    # and the bench sort that _event_lineup_schedule would otherwise compute
    # and this function never reads (bench_player_ids is only ever summed
    # here, never displayed in order).
    schedule = _event_lineup_schedule(squad, profile, horizon, include_value_bands=False, sort_bench=False, cache=cache)
    bench_weight = {"conservative": 0.20, "balanced": 0.16, "aggressive": 0.08}[profile]
    value = 0.0
    by_id = {player["id"]: player for player in squad}
    for event in schedule:
        value += event["profile_points"]
        value += bench_weight * sum(
            _profile_event_score(by_id[player_id], profile, event["event_index"], horizon, cache=cache)
            for player_id in event["bench_player_ids"]
        )
    return value


def _legal(squad, quotas, budget, club_limit):
    if len({player["id"] for player in squad}) != sum(quotas.values()):
        return False
    counts = Counter(player["position_short"] for player in squad)
    if any(counts.get(position, 0) != count for position, count in quotas.items()):
        return False
    if sum(player["price"] for player in squad) > budget + 1e-9:
        return False
    clubs = Counter(player["club"] for player in squad)
    return not clubs or max(clubs.values()) <= club_limit


def _cheap_legal_squad(eligible, quotas, budget, club_limit):
    squad = []
    club_counts = Counter()
    for position, count in quotas.items():
        candidates = sorted(
            (player for player in eligible if player["position_short"] == position),
            key=lambda row: (row["price"], -row["xp_5"]),
        )
        for player in candidates:
            if club_counts[player["club"]] >= club_limit:
                continue
            squad.append(player)
            club_counts[player["club"]] += 1
            if sum(row["position_short"] == position for row in squad) == count:
                break
    if not _legal(squad, quotas, budget, club_limit):
        raise ValueError("Unable to construct a legal baseline squad from available players")
    return squad


def _optimize_squad(
    eligible, quotas, budget, club_limit, profile="balanced", initial_squad=None,
    horizon=5, runs=10, steps=5000, cache=None,
):
    baseline = list(initial_squad) if initial_squad and _legal(initial_squad, quotas, budget, club_limit) else _cheap_legal_squad(eligible, quotas, budget, club_limit)
    candidates_by_position = {
        position: [player for player in eligible if player["position_short"] == position]
        for position in quotas
    }
    best_squad = list(baseline)
    best_score = _squad_objective(best_squad, profile, horizon, cache=cache)
    profile_seed = {"conservative": 1000, "balanced": 2000, "aggressive": 3000}[profile]
    for seed in range(runs):
        rng = random.Random(260700 + profile_seed + seed)
        current = list(baseline)
        current_score = _squad_objective(current, profile, horizon, cache=cache)
        for step in range(steps):
            selected_index = rng.randrange(len(current))
            old = current[selected_index]
            replacement = rng.choice(candidates_by_position[old["position_short"]])
            if any(player["id"] == replacement["id"] for player in current):
                continue
            proposal = list(current)
            proposal[selected_index] = replacement
            if not _legal(proposal, quotas, budget, club_limit):
                continue
            score = _squad_objective(proposal, profile, horizon, cache=cache)
            temperature = 1.5 * (1 - step / steps) + 0.03
            if score >= current_score or rng.random() < math.exp((score - current_score) / temperature):
                current, current_score = proposal, score
                if score > best_score:
                    best_squad, best_score = list(proposal), score
    return best_squad


def _selection_rationale(squad, eligible, alternatives_per_player=2):
    """For each squad player, the top not-selected same-position alternatives by xp_5.

    This surfaces the budget/value trade-offs behind a pick -- e.g. a cheaper
    player was chosen even though a pricier one projects more points, freeing
    budget spent elsewhere in the squad -- rather than leaving that judgment
    implicit in the optimizer's search.
    """
    squad_ids = {player["id"] for player in squad}
    pool_by_position = {}
    for player in eligible:
        if player["id"] in squad_ids:
            continue
        pool_by_position.setdefault(player["position_short"], []).append(player)
    for pool in pool_by_position.values():
        pool.sort(key=lambda row: row["xp_5"], reverse=True)
    rationale = {}
    for player in squad:
        pool = pool_by_position.get(player["position_short"], [])
        rationale[player["id"]] = [
            {
                "id": alternative["id"],
                "name": alternative["name"],
                "price": alternative["price"],
                "xp_5": alternative["xp_5"],
                "price_delta": round(alternative["price"] - player["price"], 1),
                "xp_5_delta": round(alternative["xp_5"] - player["xp_5"], 1),
            }
            for alternative in pool[:alternatives_per_player]
        ]
    return rationale


def _profile_metrics_for_squad(squad, profile, event=1, cache=None):
    """Per-profile uncertainty bands and squad-composition stats for a *fixed* squad.

    Issue #158: extracted from `_build_profile_recommendation` below so the same computation can
    run against a squad that was never optimized here at all -- a manager's own declared draft or
    real squad, evaluated under three risk lenses rather than three different optimized squads.
    `_event_lineup_schedule`/`_team_uncertainty_interval` already take an arbitrary `squad` list
    and never touch `_optimize_squad`, so nothing here actually required the squad to have come
    from it -- see plans/issue-158-personalized-risk-profiles.md.
    """
    horizon_totals = {}
    evaluation_horizons = {}
    for horizon in (1, 3, 5):
        schedule = _event_lineup_schedule(squad, profile, horizon, cache=cache)
        serialized_schedule = []
        for row in schedule:
            event_interval = _team_uncertainty_interval(squad, profile, horizon, [row])
            serialized_schedule.append(
                {
                    **row,
                    "event": event + row["event_index"],
                    "profile_points": round(row["profile_points"], 2),
                    "central_points": event_interval["central"],
                    "lower_points": event_interval["lower"],
                    "upper_points": event_interval["upper"],
                }
            )
        evaluation_horizons[str(horizon)] = {
            "lineup_player_ids": serialized_schedule[0]["lineup_player_ids"],
            "captain_id": serialized_schedule[0]["captain_id"],
            "event_lineups": serialized_schedule,
            "lineup_semantics": "event_specific",
        }
        horizon_totals[horizon] = _team_uncertainty_interval(squad, profile, horizon, schedule)
    return {
        "evaluation_horizons": evaluation_horizons,
        "metrics": {
            "central_1gw": round(horizon_totals[1]["central"], 1),
            "lower_1gw": round(horizon_totals[1]["lower"], 1),
            "upper_1gw": round(horizon_totals[1]["upper"], 1),
            "central_3gw": round(horizon_totals[3]["central"], 1),
            "lower_3gw": round(horizon_totals[3]["lower"], 1),
            "upper_3gw": round(horizon_totals[3]["upper"], 1),
            "central_5gw": round(horizon_totals[5]["central"], 1),
            "lower_5gw": round(horizon_totals[5]["lower"], 1),
            "upper_5gw": round(horizon_totals[5]["upper"], 1),
            "uncertainty_method": horizon_totals[5]["method"],
            "uncertainty_same_event_correlation": horizon_totals[5]["same_event_correlation"],
            "uncertainty_cross_event_correlation": horizon_totals[5]["cross_event_correlation"],
            "average_ownership": round(sum(player["ownership"] for player in squad) / len(squad), 1),
            "average_expected_minutes": round(sum(player["expected_minutes"] for player in squad) / len(squad), 1),
            "low_confidence_players": sum(player["confidence"] == "low" for player in squad),
            "medium_confidence_players": sum(player["confidence"] == "medium" for player in squad),
        },
    }


def _build_profile_recommendation(profile, eligible, quotas, budget, club_limit, initial_squad=None, event=1, cache=None):
    squad = _optimize_squad(eligible, quotas, budget, club_limit, profile, initial_squad, cache=cache)
    by_id = {player["id"]: player for player in squad}
    one_gameweek_score = lambda player: _profile_player_score(player, profile, 1)
    first_schedule = _event_lineup_schedule(squad, profile, 1, cache=cache)[0]
    starting_xi = [by_id[player_id] for player_id in first_schedule["lineup_player_ids"]]
    formation = first_schedule["formation"]
    bench = [by_id[player_id] for player_id in first_schedule["bench_player_ids"]]
    captain = by_id[first_schedule["captain_id"]]
    vice_captain = by_id[first_schedule["vice_captain_id"]]
    captaincy = sorted(starting_xi, key=one_gameweek_score, reverse=True)
    profile_metrics = _profile_metrics_for_squad(squad, profile, event, cache=cache)
    gw1_points = profile_metrics["metrics"]["central_1gw"]
    definition = _PROFILE_DEFINITIONS[profile]
    return {
        "id": profile,
        **definition,
        "squad": {
            "players": sorted(squad, key=lambda row: (row["position_id"], -row["xp_5"])),
            "cost": round(sum(player["price"] for player in squad), 1),
            "money_remaining": round(budget - sum(player["price"] for player in squad), 1),
            "selection_rationale": _selection_rationale(squad, eligible),
            "starting_xi": starting_xi,
            "formation": formation,
            "bench": bench,
            "captain": captain,
            "vice_captain": vice_captain,
            "projected_event": event,
            "projected_event_points_including_captain": round(gw1_points, 1),
            "projected_gw1_points_including_captain": round(gw1_points, 1),
            "starting_xi_xp_5": round(sum(player["xp_5"] for player in starting_xi), 1),
        },
        "captaincy": captaincy[:5],
        **profile_metrics,
    }


def build_gw_recommendations(
    bootstrap, fixtures, generated_at, horizon=5, recent_transfers=None,
):
    """Return three legal rolling-horizon squads with explicit risk objectives."""
    event = _next_event_id(bootstrap)
    projections = project_players(
        bootstrap,
        fixtures,
        horizon=horizon,
        start_event=event,
        recent_transfers=recent_transfers,
        as_of=generated_at,
    )
    role_transition_player_ids = sorted(
        player["id"] for player in projections if player["role_transition"]
    )
    teammate_transfer_impact_player_ids = sorted(
        player["id"] for player in projections if player["teammate_transfer_impact"]
    )
    type_by_id = {item.get("id"): item for item in bootstrap.get("element_types", [])}
    quotas = {
        _POSITION_CODES[position_id]: int(item.get("squad_select") or 0)
        for position_id, item in type_by_id.items()
        if position_id in _POSITION_CODES
    }
    settings = bootstrap.get("game_settings", {})
    budget = _number(settings.get("squad_total_spend"), 1000) / 10
    club_limit = int(settings.get("squad_team_limit") or 3)
    eligible = [
        player for player in projections
        if player["can_select"] and player["expected_minutes"] > 0 and player["xp_5"] > 0
    ]
    # Issue #176: one cache for this whole call -- _optimize_squad's simulated-annealing search
    # alone calls _squad_objective (and, through it, _profile_event_score) up to tens of
    # thousands of times per profile; see _profile_event_score's own comment for what this
    # eliminates and why it's safe (pure per-(player id, profile, event_index, horizon), doesn't
    # depend on which squad a player is being evaluated within).
    cache = {}
    balanced = _build_profile_recommendation(
        "balanced", eligible, quotas, budget, club_limit, event=event, cache=cache
    )
    balanced_players = balanced["squad"]["players"]
    conservative = _build_profile_recommendation(
        "conservative", eligible, quotas, budget, club_limit, balanced_players, event, cache=cache
    )
    aggressive = _build_profile_recommendation(
        "aggressive", eligible, quotas, budget, club_limit, balanced_players, event, cache=cache
    )
    profile_recommendations = [conservative, balanced, aggressive]
    balanced_ids = {player["id"] for player in balanced["squad"]["players"]}
    balanced_names = {player["id"]: player["name"] for player in balanced["squad"]["players"]}
    for recommendation in profile_recommendations:
        profile_ids = {player["id"] for player in recommendation["squad"]["players"]}
        profile_names = {player["id"]: player["name"] for player in recommendation["squad"]["players"]}
        recommendation["comparison_to_balanced"] = {
            "shared_players": len(profile_ids & balanced_ids),
            "changed_players": {
                "in": sorted(profile_names[player_id] for player_id in profile_ids - balanced_ids),
                "out": sorted(balanced_names[player_id] for player_id in balanced_ids - profile_ids),
            },
            "central_5gw_delta": round(
                recommendation["metrics"]["central_5gw"] - balanced["metrics"]["central_5gw"], 1
            ),
            "lower_5gw_delta": round(
                recommendation["metrics"]["lower_5gw"] - balanced["metrics"]["lower_5gw"], 1
            ),
            "upper_5gw_delta": round(
                recommendation["metrics"]["upper_5gw"] - balanced["metrics"]["upper_5gw"], 1
            ),
        }
    return {
        "status": "active_preliminary",
        "event": event,
        "generated_at": generated_at,
        "default_profile": "balanced",
        "model": {
            "name": "Official-data preseason multi-profile baseline",
            "version": str(_COEFFICIENTS["model_version"]),
            "is_champion": True,
            "horizon_gameweeks": horizon,
            "uses_betting_odds": False,
            "inputs": [
                "Official FPL prices, positions, availability, prior-season points and minutes",
                "Official FPL expected-goals/assists/goals-conceded/saves and defensive-contribution rates",
                "Official FPL ep_next estimate for GW1",
                "Official FPL fixture difficulty ratings for GW1-GW5",
                "Recent confirmed transfers with new-club role and minutes scenarios",
                "Distinct downside, central, and upside optimization objectives",
                "Attack/clean-sheet/goals-conceded FDR tables and uncertainty bands fitted from 3 seasons of historical results",
                "Position-specific residual trust (MID/FWD) selected from fit-season diagnostics; 2025/26 is a reviewed post-tuning check, not independent validation",
            ],
            "limitations": [
                "Expected minutes are inferred from season-to-date starts and minutes, not predicted lineups. A "
                "recency-weighted rotation model (Phase 4) was built and backtested but made projections measurably "
                "worse (higher MAE on matched-population comparisons), not better -- so it is present in the code "
                "but disabled (minutes_min_appearances set unreachably high in config/model-coefficients.json). "
                "See IMPLEMENTATION_PLAN.md Phase 4.",
                "Opponent strength uses official fixture-difficulty ratings. A fitted per-team Poisson attack/defense "
                "model (Phase 1) was built and backtested but did not beat the FDR tables at any tested same-season "
                "data threshold, so it is present in the code but disabled (team_strength_min_rounds set unreachably "
                "high in config/model-coefficients.json). See IMPLEMENTATION_PLAN.md Phase 1.",
                "Bonus points use a flat shrunk heuristic; there is no official bonus-per-90 field to shrink toward.",
                "Recent transfers receive role-transition scenarios, but predicted lineups and tactical fit still require review.",
                "Ownership only supplies a bounded aggressive-profile tiebreak and never overrides projection quality.",
                "Midfielder/forward attacking projection still runs a positive bias versus historical backtests (smaller than "
                "before this fix but not zero); goalkeeper/defender bias is small. See IMPLEMENTATION_PLAN.md Phase 3.",
                "Defender residual trust was left at its original value: a single-season test showed DEF's bias direction "
                "is unstable, so it was not refit without a more robust multi-season search.",
                "The ep_next blend weight (GW1 only) could not be validated by the historical backtest, since ep_next "
                "is unavailable historically; it remains at its original hand-picked value.",
                "Team-level ranges aggregate marginal player uncertainty with declared same-event and cross-event "
                "correlations. They are modeled intervals, not empirically calibrated team ranges, until enough immutable forecasts complete.",
                "Defensive-contribution projections use position baselines when the retained feed has no usable historical "
                "contribution rates; this component is provisional until current-season observations accumulate.",
                "Projections use official Premier League fixtures and FPL fixture difficulty. European and domestic-cup "
                "schedules are not yet modeled directly, so their travel, fatigue, and rotation effects are not included explicitly.",
                "Projections are preliminary and have not yet been calibrated on 2026/27 results.",
                "An ML-based minutes model is being evaluated in shadow this season; it never affects your "
                "recommendations.",
            ],
            "role_transition_player_ids": role_transition_player_ids,
            "teammate_transfer_impact_player_ids": teammate_transfer_impact_player_ids,
        },
        "profile_recommendations": profile_recommendations,
        "recommended_squad": balanced["squad"],
        "captaincy": balanced["captaincy"],
        "player_forecasts": [
            {
                "id": player["id"],
                "modeled": round(player["fixture_xp"][0], 2) if player["fixture_xp"] else 0.0,
                "lower": (
                    round(player["profile_fixture_xp"]["conservative"][0], 2)
                    if player["profile_fixture_xp"]["conservative"] else 0.0
                ),
                "upper": (
                    round(player["profile_fixture_xp"]["aggressive"][0], 2)
                    if player["profile_fixture_xp"]["aggressive"] else 0.0
                ),
            }
            for player in projections
        ],
        "watchlist": {
            position: sorted(
                (player for player in eligible if player["position_short"] == position),
                key=lambda row: row["xp_5"],
                reverse=True,
            )[:5]
            for position in quotas
        },
    }
