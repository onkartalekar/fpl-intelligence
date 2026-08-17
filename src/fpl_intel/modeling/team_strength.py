"""Fitted Dixon-Coles-style team attack/defense ratings.

Replaces the difficulty-bucket (FDR) opponent-adjustment used since Phase 2/3
with per-fixture expected goals for and against, fit from actual match
results via iterative proportional scaling -- a biproportional-fitting
method using only closed-form per-step updates, no numerical optimizer or
external dependency.

Scope decision (2026-07-25): the original plan called for seeding ratings
preseason from the prior season's results, with a promoted-team prior
averaged from historical newly-promoted sides. That requires carrying team
ratings across a season boundary, which the current no-lookahead backtest
architecture does not support cleanly (each season is loaded and evaluated
independently -- see backtest.py). Implemented instead: fit strictly from
the current season's own completed fixtures. Before MIN_ROUNDS completed
rounds there is not enough same-season data to fit reliably, so callers
should keep using the Phase 3 FDR tables -- see should_use_team_strength().
This is a documented substitution, not an oversight, matching how Phase 2
substituted for the (also skipped) opponent model this phase now provides.
"""

import math

from .coefficients import load_coefficients


_COEFFICIENTS = load_coefficients()

MIN_ROUNDS = _COEFFICIENTS["team_strength_min_rounds"]
_DEFAULT_HALF_LIFE_MATCHES = _COEFFICIENTS["team_strength_half_life_matches"]
_DEFAULT_ITERATIONS = 30
_DEFAULT_HOME_ADVANTAGE = 1.2
_DEFAULT_LEAGUE_AVG_GOALS = 1.5
_DEFAULT_RATING = {"attack": 1.0, "defense": 1.0}


def should_use_team_strength(completed_rounds, min_rounds=MIN_ROUNDS):
    """True once enough same-season rounds have completed to fit reliably."""
    return completed_rounds >= min_rounds


def fit_team_strength(matches, half_life_matches=_DEFAULT_HALF_LIFE_MATCHES, iterations=_DEFAULT_ITERATIONS):
    """Fit attack/defense ratings and home advantage from historical matches.

    ``matches``: iterable of dicts with ``home_team``, ``away_team``,
    ``home_goals``, ``away_goals``, ``age`` (matches-ago; 0 = most recent),
    used for exponential recency weighting.

    Ratings are normalized so each team's attack/defense average to ~1.0
    across the league; ``home_advantage`` and ``league_avg_goals`` capture
    the shared baseline scoring rate and home-field effect.
    """
    teams = set()
    for match in matches:
        teams.add(match["home_team"])
        teams.add(match["away_team"])
    if not matches or not teams:
        return {"teams": {}, "home_advantage": _DEFAULT_HOME_ADVANTAGE, "league_avg_goals": _DEFAULT_LEAGUE_AVG_GOALS}

    decay = math.log(2) / half_life_matches
    weights = [math.exp(-decay * match["age"]) for match in matches]

    total_weight = sum(weights)
    total_goals = sum(w * (m["home_goals"] + m["away_goals"]) for w, m in zip(weights, matches))
    league_avg_goals = total_goals / (2 * total_weight) if total_weight else _DEFAULT_LEAGUE_AVG_GOALS

    attack = {team: 1.0 for team in teams}
    defense = {team: 1.0 for team in teams}
    home_advantage = _DEFAULT_HOME_ADVANTAGE

    for _ in range(iterations):
        new_attack = {}
        for team in teams:
            scored, expected_denominator = 0.0, 0.0
            for weight, match in zip(weights, matches):
                if match["home_team"] == team:
                    scored += weight * match["home_goals"]
                    expected_denominator += weight * defense[match["away_team"]] * home_advantage * league_avg_goals
                elif match["away_team"] == team:
                    scored += weight * match["away_goals"]
                    expected_denominator += weight * defense[match["home_team"]] * league_avg_goals
            new_attack[team] = scored / expected_denominator if expected_denominator > 0 else attack[team]
        attack = new_attack

        new_defense = {}
        for team in teams:
            conceded, expected_denominator = 0.0, 0.0
            for weight, match in zip(weights, matches):
                if match["home_team"] == team:
                    conceded += weight * match["away_goals"]
                    expected_denominator += weight * attack[match["away_team"]] * league_avg_goals
                elif match["away_team"] == team:
                    conceded += weight * match["home_goals"]
                    expected_denominator += weight * attack[match["home_team"]] * home_advantage * league_avg_goals
            new_defense[team] = conceded / expected_denominator if expected_denominator > 0 else defense[team]
        defense = new_defense

        home_numerator = sum(w * m["home_goals"] for w, m in zip(weights, matches))
        home_denominator = sum(
            w * attack[m["home_team"]] * defense[m["away_team"]] * league_avg_goals
            for w, m in zip(weights, matches)
        )
        if home_denominator > 0:
            home_advantage = home_numerator / home_denominator

    # This class of multiplicative model is only identifiable up to a scale
    # trade-off between attack and defense (attack_i -> k*attack_i,
    # defense_j -> defense_j/k leaves every expected_goals() prediction
    # unchanged). Pin a canonical scale -- mean(attack) = mean(defense) = 1 --
    # by folding each rescaling into league_avg_goals, which exactly
    # preserves every fixture's expected goals (see tests/test_team_strength.py).
    mean_attack = sum(attack.values()) / len(attack)
    if mean_attack > 0:
        attack = {team: value / mean_attack for team, value in attack.items()}
        league_avg_goals *= mean_attack
    mean_defense = sum(defense.values()) / len(defense)
    if mean_defense > 0:
        defense = {team: value / mean_defense for team, value in defense.items()}
        league_avg_goals *= mean_defense

    return {
        "teams": {team: {"attack": attack[team], "defense": defense[team]} for team in teams},
        "home_advantage": home_advantage,
        "league_avg_goals": league_avg_goals,
    }


def expected_goals(ratings, home_team, away_team):
    """Return (expected_home_goals, expected_away_goals) for a fixture.

    Falls back to league-average, home-advantage-adjusted rates for any
    team missing from ``ratings`` (e.g. this fit has too few matches to
    have seen every team yet).
    """
    teams = ratings["teams"]
    league_avg = ratings["league_avg_goals"]
    home_advantage = ratings["home_advantage"]
    home_rating = teams.get(home_team, _DEFAULT_RATING)
    away_rating = teams.get(away_team, _DEFAULT_RATING)
    expected_home = home_rating["attack"] * away_rating["defense"] * home_advantage * league_avg
    expected_away = away_rating["attack"] * home_rating["defense"] * league_avg
    return expected_home, expected_away


def clean_sheet_probability(expected_goals_against):
    """Poisson P(zero goals conceded) given an expected-goals-against rate."""
    return math.exp(-expected_goals_against)


def matches_from_fixtures(fixtures, before_event):
    """Adapt this project's fixture-dict shape into fit_team_strength's match
    format, using only fixtures strictly before ``before_event`` -- the same
    no-lookahead boundary the rest of the projection model uses.
    """
    matches = []
    for fixture in fixtures:
        event = fixture.get("event")
        if not event or event >= before_event:
            continue
        home_goals = fixture.get("team_h_score")
        away_goals = fixture.get("team_a_score")
        if home_goals is None or away_goals is None:
            continue
        matches.append({
            "home_team": fixture.get("team_h"),
            "away_team": fixture.get("team_a"),
            "home_goals": float(home_goals),
            "away_goals": float(away_goals),
            "age": max(0, before_event - 1 - event),
        })
    return matches


def completed_rounds(fixtures, before_event):
    """Count of distinct events with at least one scored fixture before ``before_event``."""
    events = {
        fixture.get("event")
        for fixture in fixtures
        if fixture.get("event") and fixture.get("event") < before_event
        and fixture.get("team_h_score") is not None and fixture.get("team_a_score") is not None
    }
    return len(events)
