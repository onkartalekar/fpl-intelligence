"""Shadow-mode ML minutes/start-probability challenger (issue #65, candidate #1).

Ridge regression predicting a player's next-fixture minutes from strictly pre-origin
features -- season-long start share, season-long minutes/game, a 3-game recency window
(start rate, average minutes), the gap between the recency window and the season-long
share, and a sample-size/maturity term. This targets the exact defect diagnosed in
`minutes.py`'s Phase 4 postmortem (IMPLEMENTATION_PLAN.md): one fixed, hand-picked decay
speed overreacting to short-term noise. A learned weight on the same recency signal,
instead of one hand-picked half-life, beat the live `_expected_minutes` baseline in every
held-out season of a 4-season leave-one-season-out backtest (an original prototype,
`scripts/experiment_minutes_ml_prototype.py`, found pooled minutes MAE 14.359 vs. the
baseline's 18.425; this production module's own fit -- same approach, but
`fixtures_played`-denominated features so it also degrades gracefully for players with no
recorded recency history, see below -- finds 14.953 vs. 18.797, still a win in every
individual held-out season, see `scripts/fit_ml_minutes_weights.py`). The original
prototype's isolated-minutes win was also confirmed to survive being wired through the
full points-scoring pipeline, not just isolated minutes MAE (2.23 vs. 2.41 points MAE,
`scripts/experiment_minutes_ml_full_pipeline_check.py`). See
`plans/issue-65-ml-shadow-model.md` for the complete evidence.

**Shadow only.** This module is never imported by `recommendations.py`, and nothing here
runs inside `project_players()` by default -- `refresh.py` computes a separate challenger
forecast every refresh (see `build_shadow_forecast` below) that is scored the same way as
the champion (`model_performance.py`'s per-model-version tracking) but never feeds a
live recommendation. Same "disabled/shadow, not deleted" discipline this codebase already
uses for `team_strength.py` (Phase 1) and the recency-weighted branch of `minutes.py`
(Phase 4) -- except this one is additive and scored, not gated off by an unreachable
config threshold.

**Weights are fitted offline, not on every refresh.** Mirrors `fit_coefficients.py`'s own
precedent for the champion model: `scripts/fit_ml_minutes_weights.py` fits ridge weights
against all four seasons in `data/history/` and writes them to
`config/ml-minutes-weights.json`, which this module loads at import time (falling back to
the weights last validated in the plan doc if the file is ever absent, so the system still
runs identically without a fresh fit -- same fallback philosophy as `coefficients.py`).
Re-fitting is a deliberate, reviewed step, not something that happens implicitly inside a
live refresh.

**Feature parity between fit time and predict time.** `extract_features` below is the
single source of truth for both `scripts/fit_ml_minutes_weights.py` (which builds its
training rows by calling this same function against `backtest.build_origin_inputs`
snapshots) and this module's own `predict_expected_minutes` -- there is no separate,
possibly-drifted copy of the feature logic anywhere else.

**Live-data caveat, honestly stated:** the live refresh pipeline does not currently fetch
a per-gameweek current-season history feed for arbitrary players (`recent_history` is
only ever populated by `backtest.py`'s historical-CSV loader, never by the live FPL
bootstrap -- the same gap that already makes `minutes.py`'s Phase 4 branch inert live).
Until a future issue wires that up, live shadow predictions use the graceful season-long
fallback below (recency terms collapse to season-long values), not the full recency
signal the backtest evidence demonstrates -- still a different, independently-fitted
formula from the champion's hand-picked 0.55/0.45 blend, so it is not a no-op copy of the
champion even without live recency data.
"""

import json
from pathlib import Path


MODEL_VERSION = "ml-minutes-ridge-v1"

FEATURE_NAMES = (
    "intercept",
    "season_start_share",
    "season_minutes_per_game_90",
    "last3_start_rate",
    "last3_avg_minutes_90",
    "trend",
    "maturity",
)

# Fitted by scripts/fit_ml_minutes_weights.py on all 4 seasons in data/history/
# (2022-23..2025-26, ridge lambda=5.0) as of 2026-08-08, using this module's own
# extract_features() so fit-time and predict-time features can never drift apart. Held-out
# leave-one-season-out cross-validation: pooled minutes MAE 14.953 vs. the live
# _expected_minutes baseline's 18.797, a win in every individual held-out season (see the
# script's own printed per-season breakdown and config/ml-minutes-weights.json's
# "validation" block). This exact tuple is also the safety-net default `_load_weights`
# falls back to if config/ml-minutes-weights.json is ever missing or unreadable.
_DEFAULT_WEIGHTS = (
    2.0431,
    -1.6527,
    21.4790,
    -3.8711,
    65.6249,
    -2.2183,
    0.7429,
)

# Issue: package reorg moved this file one level deeper (src/fpl_intel/ -> src/fpl_intel/
# modeling/), so this needs parents[3] to still reach the repo root, not parents[2].
_WEIGHTS_PATH = Path(__file__).resolve().parents[3] / "config" / "ml-minutes-weights.json"

_MATURITY_CAP_FIXTURES = 20.0


def _load_weights(path=None):
    """Return (weights tuple, model_version) from config/ml-minutes-weights.json.

    Mirrors coefficients.py's load-at-import, fall-back-to-defaults-if-absent pattern:
    nothing here fits or adopts anything -- it only loads whatever
    scripts/fit_ml_minutes_weights.py last wrote and reviewed in.
    """
    config_path = Path(path) if path else _WEIGHTS_PATH
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        payload = {}
    raw_weights = payload.get("weights")
    if isinstance(raw_weights, list) and len(raw_weights) == len(FEATURE_NAMES):
        weights = tuple(float(value) for value in raw_weights)
    else:
        weights = _DEFAULT_WEIGHTS
    model_version = payload.get("model_version") or MODEL_VERSION
    return weights, model_version


WEIGHTS, _FITTED_MODEL_VERSION = _load_weights()


def extract_features(player, fixtures_played=38):
    """Strictly pre-origin feature vector, matching FEATURE_NAMES's order.

    ``fixtures_played`` -- the player's club's completed-fixture count -- is used as the
    games denominator (not ``len(recent_history)``, which the original backtest prototype
    used): it is the same denominator `recommendations._expected_minutes` already receives
    from every caller, so this function stays well-behaved (bounded season_start_share,
    no divide-by-history-length blowup) even when ``recent_history`` is absent, which is
    always true for live players today (see module docstring). In backtest snapshots
    ``fixtures_played`` (``origin_gw - 1``) and ``len(recent_history)`` track each other
    closely for continuously-selected players, so this keeps the validated backtest
    evidence representative of what this function actually computes.

    The 3-game recency window falls back to the season-long values when fewer than 3
    pre-origin gameweeks are recorded (true preseason, a recent debutant, or -- today --
    any live player, since ``recent_history`` is not yet populated live) -- the same
    graceful degradation the rejected prototype already used, just generalized to n=0.
    """
    history = player.get("recent_history") or []
    fixtures_played = max(1.0, min(38.0, float(fixtures_played or 1)))
    starts = min(fixtures_played, float(player.get("starts") or 0))
    minutes = min(90.0 * fixtures_played, float(player.get("minutes") or 0))
    season_start_share = starts / fixtures_played
    season_minutes_per_game = minutes / fixtures_played
    last3 = history[-3:]
    if last3:
        last3_start_rate = sum(1 for row in last3 if row.get("started")) / len(last3)
        last3_avg_minutes = sum(float(row.get("minutes") or 0) for row in last3) / len(last3)
    else:
        last3_start_rate = season_start_share
        last3_avg_minutes = season_minutes_per_game
    trend = last3_start_rate - season_start_share
    maturity = min(fixtures_played, _MATURITY_CAP_FIXTURES) / _MATURITY_CAP_FIXTURES
    return [
        1.0,
        season_start_share,
        season_minutes_per_game / 90.0,
        last3_start_rate,
        last3_avg_minutes / 90.0,
        trend,
        maturity,
    ]


def predict_expected_minutes(player, fixtures_played=38, availability_multiplier=1.0):
    """This challenger's own expected-minutes estimate for one player.

    Mirrors `recommendations._expected_minutes`'s signature/availability convention (an
    externally supplied ``availability_multiplier`` baked into the return value, matching
    how `minutes.expected_minutes_from_history` already does this) so it is a drop-in
    alternative wherever the champion path is -- see `project_players`'s
    ``expected_minutes_override`` parameter -- but is never wired there for live
    recommendations. Only `build_shadow_forecast` below calls it.
    """
    if not availability_multiplier:
        return 0.0
    features = extract_features(player, fixtures_played)
    raw = sum(weight * value for weight, value in zip(WEIGHTS, features))
    return round(min(90.0, max(0.0, raw)) * availability_multiplier, 1)


def build_shadow_forecast(bootstrap, fixtures, generated_at, recent_transfers=None, horizon=5, start_event=None):
    """Compute this challenger's own per-player forecast for the current origin event.

    Reuses `project_players()`'s full scoring pipeline unchanged -- opponent strength,
    component scoring, bonus/residual, uncertainty bands, and the per-event `component_xp`
    breakdown SPECIFICATION.md requires -- with only expected-minutes swapped for
    `predict_expected_minutes`, the same substitution
    `scripts/experiment_minutes_ml_full_pipeline_check.py` validated by monkeypatching (this
    uses `project_players`'s `expected_minutes_override` parameter instead, so no module
    global is ever mutated). `recommendations` is imported locally, not at module level, to
    keep the dependency strictly one-way: `recommendations.py` never imports this module.

    Returns ``None`` when there is nothing to project against yet (mirrors
    `build_gw_recommendations`'s own "model_unavailable" gate) rather than raising.
    """
    elements = bootstrap.get("elements", [])
    if not elements or not bootstrap.get("element_types") or not fixtures:
        return None
    from . import recommendations as rec

    event = int(start_event or rec._next_event_id(bootstrap))
    projections = rec.project_players(
        bootstrap,
        fixtures,
        horizon=horizon,
        start_event=event,
        recent_transfers=recent_transfers,
        as_of=generated_at,
        expected_minutes_override=predict_expected_minutes,
    )
    player_forecasts = [
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
    ]
    return {"event": event, "model_version": MODEL_VERSION, "player_forecasts": player_forecasts}
