"""Loads fitted model coefficients from config/model-coefficients.json.

Per SPECIFICATION.md's model-change rule, coefficient changes must remain
reviewable and validated against frozen history rather than adopted
automatically. scripts/fit_coefficients.py computes candidate values from
data/history/ and the Phase 0 backtest harness; a human compares the
resulting backtest report against the currently active coefficients before
overwriting config/model-coefficients.json. Nothing here fits or adopts
anything at import time -- this module only loads whatever is currently
checked in, falling back to the pre-Phase-3 hand-picked defaults if the
file is absent so the system still runs identically without it.
"""

import json
from pathlib import Path


_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "model-coefficients.json"

_DEFAULTS = {
    "model_version": "0.7",
    "reliability_denominator": 900.0,
    "reliability_cap": 0.82,
    # Per-position: MID/FWD's over/under-performance residual is a real,
    # quickly-detectable signal that benefits from being trusted with less
    # data (see IMPLEMENTATION_PLAN.md); DEF's residual bias direction was
    # unstable across seasons in the same search, so it and GKP stay at the
    # original global value pending a more robust (multi-season) refit.
    "residual_reliability_denominator_by_position": {"1": 900.0, "2": 900.0, "3": 900.0, "4": 900.0},
    "residual_reliability_cap": 0.82,
    "ep_next_blend_weight": 0.3,
    "uncertainty_bands": {"high": 0.16, "medium": 0.25, "low": 0.38},
    "clean_sheet_probability_by_difficulty": {"1": 0.42, "2": 0.36, "3": 0.30, "4": 0.24, "5": 0.16},
    "goals_conceded_multiplier_by_difficulty": {"1": 0.72, "2": 0.86, "3": 1.0, "4": 1.16, "5": 1.35},
    "attack_multiplier_by_difficulty": {
        "1": 1.099, "2": 1.0495, "3": 1.0, "4": 0.9505, "5": 0.901
    },
    "team_strength_min_rounds": 6,
    "team_strength_half_life_matches": 12.0,
    "minutes_min_appearances": 3,
    "minutes_half_life_matches": 4.0,
    "minutes_rotation_volatility_threshold": 0.35,
}


def _with_int_keys(mapping):
    return {int(key): value for key, value in mapping.items()}


def _validate_coefficients(coefficients):
    bounded = {
        "reliability_cap": (0, 1),
        "residual_reliability_cap": (0, 1),
        "ep_next_blend_weight": (0, 1),
        "minutes_rotation_volatility_threshold": (0, 1),
    }
    for key, (minimum, maximum) in bounded.items():
        value = coefficients.get(key)
        if not isinstance(value, (int, float)) or not minimum <= value <= maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}")
    for key in (
        "reliability_denominator", "team_strength_half_life_matches",
        "minutes_half_life_matches", "team_strength_min_rounds", "minutes_min_appearances",
    ):
        value = coefficients.get(key)
        if not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{key} must be positive")
    for key in ("clean_sheet_probability_by_difficulty",):
        table = coefficients.get(key, {})
        if set(table) != set(range(1, 6)) or any(not 0 <= value <= 1 for value in table.values()):
            raise ValueError(f"{key} must contain difficulty keys 1-5 with probabilities between 0 and 1")
    for key in ("goals_conceded_multiplier_by_difficulty", "attack_multiplier_by_difficulty"):
        table = coefficients.get(key, {})
        if set(table) != set(range(1, 6)) or any(value <= 0 for value in table.values()):
            raise ValueError(f"{key} must contain difficulty keys 1-5 with positive values")
    bands = coefficients.get("uncertainty_bands", {})
    if set(bands) != {"high", "medium", "low"} or any(
        not isinstance(value, (int, float)) or not 0 <= value <= 2 for value in bands.values()
    ):
        raise ValueError("uncertainty_bands must define high, medium, and low values between 0 and 2")


def load_coefficients(path=None):
    """Return the active coefficient set, falling back to pre-Phase-3 defaults."""
    config_path = Path(path) if path else _CONFIG_PATH
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        loaded = {}
    merged = {**_DEFAULTS, **loaded}
    merged["uncertainty_bands"] = {**_DEFAULTS["uncertainty_bands"], **loaded.get("uncertainty_bands", {})}
    merged["clean_sheet_probability_by_difficulty"] = _with_int_keys(
        {**_DEFAULTS["clean_sheet_probability_by_difficulty"], **loaded.get("clean_sheet_probability_by_difficulty", {})}
    )
    merged["goals_conceded_multiplier_by_difficulty"] = _with_int_keys(
        {**_DEFAULTS["goals_conceded_multiplier_by_difficulty"], **loaded.get("goals_conceded_multiplier_by_difficulty", {})}
    )
    merged["attack_multiplier_by_difficulty"] = _with_int_keys(
        {**_DEFAULTS["attack_multiplier_by_difficulty"], **loaded.get("attack_multiplier_by_difficulty", {})}
    )
    merged["residual_reliability_denominator_by_position"] = _with_int_keys(
        {
            **_DEFAULTS["residual_reliability_denominator_by_position"],
            **loaded.get("residual_reliability_denominator_by_position", {}),
        }
    )
    _validate_coefficients(merged)
    return merged
