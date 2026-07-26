"""Recency-weighted expected-minutes model (Phase 4).

Replaces the season-average ``_expected_minutes`` estimate with one that
weights recent appearances more than older ones. The previous model treats
a player who has started every recent match identically to one who
started early in the season and has since been benched, as long as their
SEASON-LONG minutes total is similar -- this model targets exactly that
gap: rotation risk.

Falls back to the season-average method
(``recommendations._expected_minutes``) when insufficient recent
per-gameweek history is available -- true preseason, or a player with too
few recorded appearances -- see ``should_use_recency_model()``.

Fixture-congestion adjustment (from the original plan) is not implemented:
it needs days-since-last-match, which the historical per-gameweek dataset
used for backtesting does not expose reliably at gameweek granularity.
Scoped out rather than half-built -- see IMPLEMENTATION_PLAN.md.
"""

import math

from .coefficients import load_coefficients


_COEFFICIENTS = load_coefficients()

MIN_APPEARANCES = int(_COEFFICIENTS["minutes_min_appearances"])
_DEFAULT_HALF_LIFE_MATCHES = _COEFFICIENTS["minutes_half_life_matches"]
_ROTATION_VOLATILITY_THRESHOLD = _COEFFICIENTS["minutes_rotation_volatility_threshold"]
_DEFAULT_MINUTES_WHEN_STARTED = 75.0
_DEFAULT_MINUTES_WHEN_SUB = 20.0


def should_use_recency_model(history, min_appearances=None):
    """True once enough recent per-gameweek rows exist to fit reliably."""
    threshold = MIN_APPEARANCES if min_appearances is None else min_appearances
    return len(history) >= threshold


def _decayed_shares(history, half_life_matches):
    """Return (start_share, sub_share, avg_minutes_started, avg_minutes_sub)
    from a recency-weighted view of per-gameweek history.

    ``history``: list of {"minutes": int, "started": bool}, ordered oldest
    to newest -- the last entry is the most recent.
    """
    if not history:
        return 0.0, 0.0, _DEFAULT_MINUTES_WHEN_STARTED, _DEFAULT_MINUTES_WHEN_SUB

    decay = math.log(2) / half_life_matches
    n = len(history)
    weights = [math.exp(-decay * (n - 1 - i)) for i in range(n)]
    total_weight = sum(weights)

    start_weight = sum(w for w, row in zip(weights, history) if row["started"])
    sub_weight = sum(w for w, row in zip(weights, history) if not row["started"] and row["minutes"] > 0)

    start_share = start_weight / total_weight if total_weight else 0.0
    sub_share = sub_weight / total_weight if total_weight else 0.0

    started_minutes_weight = sum(w * row["minutes"] for w, row in zip(weights, history) if row["started"])
    sub_minutes_weight = sum(
        w * row["minutes"] for w, row in zip(weights, history) if not row["started"] and row["minutes"] > 0
    )
    avg_minutes_started = started_minutes_weight / start_weight if start_weight > 0 else _DEFAULT_MINUTES_WHEN_STARTED
    avg_minutes_sub = sub_minutes_weight / sub_weight if sub_weight > 0 else _DEFAULT_MINUTES_WHEN_SUB

    return start_share, sub_share, avg_minutes_started, avg_minutes_sub


def is_rotation_risk(history, half_life_matches=None):
    """Flag volatile starting patterns: recent share differs a lot from the
    player's own season-long share (a real rotation signal, not just a
    generically low start rate)."""
    half_life_matches = _DEFAULT_HALF_LIFE_MATCHES if half_life_matches is None else half_life_matches
    if len(history) < MIN_APPEARANCES:
        return False
    recent_share, _, _, _ = _decayed_shares(history, half_life_matches)
    season_long_share = sum(1 for row in history if row["started"]) / len(history)
    return abs(recent_share - season_long_share) >= _ROTATION_VOLATILITY_THRESHOLD


def expected_minutes_from_history(history, half_life_matches=None, availability_multiplier=1.0):
    """Recency-weighted expected minutes for a player's next match."""
    half_life_matches = _DEFAULT_HALF_LIFE_MATCHES if half_life_matches is None else half_life_matches
    if not availability_multiplier:
        return 0.0
    start_share, sub_share, avg_minutes_started, avg_minutes_sub = _decayed_shares(history, half_life_matches)
    expected = start_share * avg_minutes_started + sub_share * avg_minutes_sub
    return round(min(90.0, expected) * availability_multiplier, 1)


def minutes_scenarios_from_history(history, half_life_matches=None, availability_multiplier=1.0):
    """Conservative/balanced/aggressive expected-minutes scenarios shaped by
    how volatile this specific player's recent starts have been, replacing
    the fixed 0.62/0.78/0.92 multipliers (previously applied only to
    role-transition players) with a per-player quantile spread.
    """
    half_life_matches = _DEFAULT_HALF_LIFE_MATCHES if half_life_matches is None else half_life_matches
    balanced = expected_minutes_from_history(history, half_life_matches, availability_multiplier)
    if not history:
        return {"conservative": balanced, "balanced": balanced, "aggressive": balanced}
    start_share, _, _, _ = _decayed_shares(history, half_life_matches)
    volatile = is_rotation_risk(history, half_life_matches)
    spread = (0.5 if volatile else 0.15) * (1 - start_share)
    conservative = round(balanced * max(0.0, 1 - spread), 1)
    aggressive = round(min(90.0, balanced * (1 + spread * 0.4)), 1)
    return {"conservative": conservative, "balanced": balanced, "aggressive": aggressive}
