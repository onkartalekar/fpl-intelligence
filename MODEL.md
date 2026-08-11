# Projection model

This is the current mechanics of the FPL Intelligence projection model:
what it computes, where each number comes from, and which parts of the
codebase are live versus built-and-not-adopted. For the chronological
history of what was tried, adopted, and rejected (including the two
negative results below), see [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).
For the behavioral contract the model must satisfy, see
[SPECIFICATION.md](SPECIFICATION.md).

No machine learning, no foundation model, no betting odds *in the live projection
that drives recommendations*. Every number a projection is built from is either an
official FPL field or a constant fitted from historical results by
`scripts/fit_coefficients.py`, and every projection can be decomposed back into named
components in the dashboard's player inspector. The one deliberate, narrow exception
(issue #65): a ridge-regression ML minutes/start-probability model runs in **shadow
mode** every refresh (`src/fpl_intel/ml_minutes.py`) -- computed and logged under its
own `model_version`, scored the same way as the champion, but never feeding
`project_players()` or any dashboard-visible recommendation. See
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)'s issue #65 entry and
`plans/issue-65-ml-shadow-model.md` for the backtest evidence and shadow design.

**Terms used throughout:** `GW` = gameweek; `GKP`/`DEF`/`MID`/`FWD` =
goalkeeper/defender/midfielder/forward; `XI` = starting eleven; `xG`/`xA`
= expected goals/expected assists (official per-90 stats FPL publishes
for every player); `FDR` = fixture difficulty rating, FPL's official 1-5
opponent-strength score; `MAE` = Mean Absolute Error, the average size of
a projection's miss regardless of direction, used to score backtests.

## Component scoring

The core idea (`src/fpl_intel/projection.py`) is additive: instead of one
blended points-per-90 rate, each player's expected points for a fixture is
the sum of seven independent components, each mapped from an official FPL
per-90 field using the real FPL scoring rules:

| Component | Built from | Applies to |
|---|---|---|
| `appearance` | minutes played (ramped 0→1 pt across 0-60 min, 1→2 pt across 60-80 min) | everyone |
| `attacking` | `expected_goals_per_90`, `expected_assists_per_90` × goal/assist point values × opponent attack scaling | everyone |
| `clean_sheet` | clean-sheet probability × position's clean-sheet points | GKP/DEF/MID |
| `goals_conceded` | `expected_goals_conceded_per_90` × -0.5 pts/goal | GKP/DEF |
| `saves` | `saves_per_90` × ⅓ pt/save | GKP |
| `bonus` | player's own bonus-per-90, shrunk to a positional baseline (no official bonus-per-90 field exists, so this is derived from cumulative bonus/minutes) | everyone |
| `residual` | player's actual points-per-90 minus what the other six components alone would predict for a neutral fixture, shrunk toward zero | everyone |

`component_points_for_event()` returns all seven plus `total` for one
player, one fixture, one minutes scenario. The dashboard's player-inspector
panel renders exactly this breakdown per gameweek — nothing is projected
that can't be traced back to a labeled component.

### Why a residual term exists

Pure xG/xA-based attacking scoring throws away a real, measurable effect:
some players sustain points above what their own expected-goals rate would
predict (elite finishing, or scoring categories this model doesn't itemize
separately, like bonus from defensive actions). `residual_rate` is that
gap, shrunk toward zero — with no track record, the model assumes no
skill beyond what the official rate stats already predict.

### Shrinkage (reliability weighting)

Every per-90 rate — including the residual — is blended between the
player's own observed rate and a positional baseline (the median of that
rate across players with 900+ minutes at that position):

```
reliability = min(cap, minutes / (minutes + denominator))
rate = reliability * observed + (1 - reliability) * baseline
```

More career minutes → more weight on the player's own numbers; a player
with a handful of substitute appearances is mostly pulled to the
positional baseline. The residual has its own reliability curve, fitted
per position (`residual_reliability_denominator_by_position`) rather than
one flat curve — MID/FWD trust their residual with less data than
GKP/DEF, fitted to correct a historical attacking-position bias (see
IMPLEMENTATION_PLAN.md, "Phase 3 continuation").

## Opponent strength

Two implementations exist; only one is active.

**Live: fixture-difficulty (FDR) tables.** `attack_multiplier_by_difficulty`,
`clean_sheet_probability_by_difficulty`, and
`goals_conceded_multiplier_by_difficulty` are lookup tables keyed by
official FPL FDR (1–5), fitted directly from three seasons of historical
results (not hand-picked) by `scripts/fit_coefficients.py`.

**Built, backtested, not adopted: fitted team-strength model**
(`src/fpl_intel/team_strength.py`). A Dixon-Coles-style Poisson
attack/defense rating per team, fit from the current season's own
completed fixtures via iterative proportional scaling, with exponential
recency weighting and a scale-identifiability normalization
(mean(attack) = mean(defense) = 1, folded into `league_avg_goals`). It
computes fixture-specific expected goals for/against instead of a 1-5
FDR bucket — strictly more information — but backtesting found it did not
beat the FDR tables at any tested same-season data threshold. It's wired
into `project_players()` and switches on automatically once enough
same-season rounds exist (`should_use_team_strength`), but that threshold
(`team_strength_min_rounds`) is set to `39` in the active config —
one more than a season has — so it never actually activates. Left in
the code, disabled by config, not deleted, so it can be reconsidered with
more data (e.g. cross-season seeding) without rebuilding it.

## Expected minutes

Two implementations exist; only one is active.

**Live: season-average estimate** (`recommendations._expected_minutes`).
Blends season-to-date minutes/starts (55%/45%) with a floor derived from
the official `ep_next` estimate, scaled by games actually elapsed (not a
fixed 38-game season) so early-season and short-track-record players
aren't crushed toward zero.

**Built, backtested, not adopted: recency-weighted minutes model**
(`src/fpl_intel/minutes.py`). Exponentially decay-weights each player's
own recent per-gameweek start/sub history (half-life in matches) instead
of using season-long totals, explicitly targeting rotation risk — a
player benched for the last month but with a healthy season total. It
also derives per-player conservative/aggressive minutes scenarios shaped
by that player's own volatility rather than one fixed multiplier.
Backtesting found it made projections measurably *worse* (higher MAE on
matched-population, same-season comparisons), and MAE got worse, not
better, as the half-life was lengthened — ruling out simple mistuning.
Wired into `project_players()` behind `should_use_recency_model()`, gated
by `minutes_min_appearances`, which is set to `39` in the active config
(more per-gameweek rows than a season has) so it never activates.

Both disabled features are visible in the API/dashboard as
`uses_team_strength` / `uses_recency_minutes` flags on each player (always
`false` today), and are called out explicitly in the dashboard's "Model
basis and risks" limitations list.

### Shadow only, not disabled: ML minutes challenger (issue #65)

A third expected-minutes implementation, `src/fpl_intel/ml_minutes.py`, exists alongside the two
above but occupies a different status from either: not the live estimate, and not a disabled,
inert branch like Phase 1/Phase 4 -- a **running, scored challenger**. A ridge-regression model
(fitted weights checked into `config/ml-minutes-weights.json`, refit offline by
`scripts/fit_ml_minutes_weights.py`, same "computed once, reviewed in" precedent as
`fit_coefficients.py`) predicting expected minutes from the same signals Phase 4 targeted --
season-long start share/minutes-per-game, a 3-game recency window, and the gap between them --
but with learned weights instead of one hand-picked decay speed. Unlike Phase 1/Phase 4, this one
beat the champion in a 4-season leave-one-season-out backtest *and* in a full points-scoring
pipeline check (not just isolated minutes MAE) -- see `plans/issue-65-ml-shadow-model.md`.

It still never feeds a live recommendation. `refresh.py` computes a separate forecast every
refresh via `ml_minutes.build_shadow_forecast` (reusing `project_players()`'s
`expected_minutes_override` parameter, default `None`, so nothing changes for the champion path),
archived under its own `model_version` (`archive_shadow_forecast`) and scored the same way as the
champion but in a fully separate report key (`model_performance.py`'s
`build_shadow_performance_report`/`shadow_models`) -- visible in the dashboard's "Forecast
accountability" tab under "Experimental models in shadow", never blended into the champion's own
numbers. The plan is to observe a full 2026-27 season of live comparisons before any promotion
decision, which -- unlike a backtest replay of settled history -- can exercise real-time failure
modes (breaking news, brand-new signings with no training rows) this candidate is specifically
meant to react to.

## GW1-only: official `ep_next` blend

For the very first projected gameweek only, if FPL's own `ep_next`
estimate is available, the model blends 30% of it in
(`ep_next_blend_weight`) against the model's own component total, scaled
by the minutes ratio for that scenario. This blend is not backtestable —
`ep_next` isn't available in the historical dataset — so it stays at its
original hand-picked weight rather than a fitted one.

## Minutes scenarios and uncertainty bands

Every player gets three minutes scenarios (`conservative` / `balanced` /
`aggressive`), which flow into three xP scenarios. For most players
these scenarios collapse to the same value; they diverge for:

- **Role transitions** — a recent confirmed transfer to a new club
  (first-party sources only) gets wider, fixed scenario multipliers
  (0.62/0.78/0.92 of the base estimate) since role/minutes are genuinely
  unclear.
- **Recency-model players** — would get per-player volatility-shaped
  scenarios, but this path is currently unreachable (see above).

Confidence (`high`/`medium`/`low`, from career minutes and availability
status) selects an uncertainty band (`uncertainty_bands` in config) applied
multiplicatively to widen the lower/upper projection around the central
estimate for non-role-transition players.

## Squad construction

`build_gw_recommendations()` runs three independent simulated-annealing
searches over legal squads (budget, position quotas, 3-per-club limit),
one per risk profile, each optimizing a different objective built from the
same underlying projections:

- **conservative** — optimizes the *lower* bound (downside protection)
- **balanced** — optimizes the *central* estimate (default)
- **aggressive** — optimizes the *upper* bound plus a bounded low-ownership
  differential bonus and a minutes-security penalty

Each profile also picks its own starting XI/formation, captain/vice-captain,
and bench ordering by re-scoring the same squad under 1/3/5-gameweek
horizons.

### `goal` is recorded, not read (issue #117/#118)

A manager's saved profile also carries a `goal` field (top 10k / 50k /
100k / beat last season / just for fun, set from the dashboard's Profile
tab). It is validated and persisted, but nothing above reads it — only
`risk_profile` selects a search objective. `goal` doesn't currently
change which squad, XI, or captain any profile recommends. This is true
of all five values, not only `beat_last_season`.

## Phase 5 (scaffolding, not wired in): LLM news-signal extraction

`src/fpl_intel/news_signals.py` uses an LLM API (raw HTTPS, no vendor SDK
dependency) as a **feature extractor only** — never as the projection
itself — to pull structured availability signals (injured / doubtful /
returning / rotation_risk / nailed_on) out of first-party club and
Premier League news text. Every extracted signal carries the exact
supporting quote and source URL, and any resulting minutes adjustment is
capped at 25% of the pre-adjustment estimate
(`bounded_minutes_adjustment`), so a single misread headline can't swing a
projection. Built and unit-tested against recorded fixtures (zero live API
calls in tests), but not called anywhere in `project_players()` or the
refresh pipeline — the plan's own gate for turning it on (Phases 1-4
adopted AND ≥8 stable live-calibration comparisons) isn't met, since
Phases 1 and 4 were rejected and the 2026/27 season hasn't produced live
comparisons yet.

**Provider-agnostic.** Nothing here is tied to one LLM vendor. Two
callers ship built in, selected by the `FPL_INTEL_LLM_PROVIDER` env var
(default `claude`):

| Provider | Env vars | Notes |
|---|---|---|
| `claude` | `ANTHROPIC_API_KEY` | Claude Messages API, model hardcoded to a small/cheap Claude model |
| `openai_compatible` | `FPL_INTEL_LLM_API_KEY`, `FPL_INTEL_LLM_API_BASE`, `FPL_INTEL_LLM_MODEL` | Any host implementing the OpenAI Chat Completions shape — no endpoint is guessed or hardcoded, so this covers third-party hosts of other models (e.g. Hermes) once you supply their base URL and model name |

Adding another provider is a small, local change: a new `_call_*(news_text,
api_key, timeout=30) -> str | None` function plus one entry in the
`_PROVIDERS` registry — `extract_availability_signals()` itself doesn't
change.

## Coefficients and validation

All tunable constants load once at import time from
`config/model-coefficients.json` via `src/fpl_intel/coefficients.py`
(falling back to `_DEFAULTS` for any missing key) -- a plain file read,
not a computation. `data/history/`'s prior-season CSVs and the scripts
below are only involved in *producing* that file; nothing the live
dashboard does on a normal refresh reads `data/history/` or recomputes a
coefficient. You only need this section if you're changing the model
itself.

### Where each coefficient comes from

| Coefficient | How it's set | Used in |
|---|---|---|
| `clean_sheet_probability_by_difficulty`, `goals_conceded_multiplier_by_difficulty`, `attack_multiplier_by_difficulty` | Fitted: computed directly (empirically) from real 3-season fixture results conditioned on official FDR -- `fit_coefficients.py`'s `_empirical_fdr_tables()` | `projection.component_points_for_event()` -- see "Opponent strength" above |
| `reliability_denominator` | Fitted: grid search against a reduced single-season backtest, kept only if it beats the current value by a real margin -- `fit_coefficients.py`'s `_search_reliability_denominator()` | `projection.player_component_rates()` -- the shrinkage curve, see "Shrinkage" above |
| `uncertainty_bands` (`high`/`medium`/`low`) | Fitted: searched for ~75% empirical outcome coverage against a full fit-season backtest -- `fit_coefficients.py`'s `_fit_uncertainty_band()` | `recommendations.py` -- widens the lower/upper projection range by confidence tier |
| `reliability_cap`, `residual_reliability_cap` | Hand-set, not touched by `fit_coefficients.py` | `projection.player_component_rates()` -- caps on the shrinkage curve above |
| `residual_reliability_denominator_by_position` | Hand-set from bespoke multi-step investigation (not a mechanical fit -- see IMPLEMENTATION_PLAN.md Phase 3 continuation) | `projection.player_component_rates()` -- per-position residual shrinkage, see "Shrinkage" above |
| `ep_next_blend_weight` | Hand-set; not fittable since `ep_next` isn't in the historical dataset | `recommendations.py` -- GW1-only blend, see "GW1-only" section above |
| `team_strength_min_rounds`, `team_strength_half_life_matches` | Hand-set from a dedicated backtest study (currently a disabling gate) | `team_strength.py` -- see "Opponent strength" above |
| `minutes_min_appearances`, `minutes_half_life_matches`, `minutes_rotation_volatility_threshold` | Hand-set from a dedicated backtest study (currently a disabling gate) | `minutes.py` -- see "Expected minutes" above |
| `model_version` | Hand-set label, bumped on each promoted candidate | Display only -- shown in the dashboard's "Model basis and risks" panel |

`fitted_at` and `source` are provenance notes for humans (when/why the
active file was last promoted) -- neither is read by any model logic.

### What each coefficient means

**`attack_multiplier_by_difficulty`** -- how much more or less than a
league-average team scores against an opponent at that FDR tier (1.45x at
FDR 1, 0.60x at FDR 5 in the current fit). This is the single biggest
lever on a player's `attacking` component: it directly scales the
goal/assist points every outfield player projects, so it's the main
reason a mid-table midfielder's projection swings hard between an
easy and a hard fixture.

**`clean_sheet_probability_by_difficulty`** -- the empirical chance a team
keeps a clean sheet against an opponent at that FDR tier (43% at FDR 1,
9% at FDR 5). Scales `clean_sheet` points for GKP/DEF/MID directly --
it's why a "good fixture" matters more for a nailed-on defender than for
a rotation risk (the probability only pays off if `played_60_probability`
is also high).

**`goals_conceded_multiplier_by_difficulty`** -- how much more or less
than average a team concedes against that FDR tier (0.51x at FDR 1,
1.54x at FDR 5). Scales the expected-goals-against rate feeding the
`goals_conceded` deduction for GKP/DEF -- the flip side of the clean-sheet
table: an easy fixture both raises clean-sheet odds and lowers the
expected penalty when a goal does go in.

**`reliability_denominator`** -- the minutes value at which a player's
own observed per-90 rate and the positional baseline are trusted equally
(50/50). Below that many career minutes, a player's projection leans more
on "what a typical player at this position does" than on their own small
sample; above it, their own numbers dominate. This is the knob that
stops a hot two-game streak from a fringe player being read as a
sustained rate.

**`reliability_cap`** -- the ceiling that trust in a player's own numbers
can reach, even with a full season+ of minutes (0.82, so at least 18%
weight always stays on the positional baseline). Exists so no player,
however large their sample, is ever projected purely off their own
history with zero regression toward the position norm.

**`residual_reliability_denominator_by_position` / `residual_reliability_cap`**
-- the same trust-curve mechanics as above, but for the `residual`
component (a player's real over/under-performance vs. what their own
rate stats predict) rather than the raw per-90 rates. Set separately
per position because a dedicated investigation found MID/FWD's residual
is a faster, more trustworthy signal (their denominator is 100 vs.
GKP/DEF's 900) -- elite finishing shows up in a smaller sample than a
defense's clean-sheet variance does.

**`uncertainty_bands`** (`high`/`medium`/`low`) -- how wide the lower/upper
projection range is around the central estimate, one width per confidence
tier. This is what actually separates the conservative, balanced, and
aggressive squad profiles: a low-confidence player (short track record,
flagged availability) has a wide band, so the conservative optimizer
punishes them far more than the aggressive one does for the same central
projection.

**`ep_next_blend_weight`** -- the 30% weight given to FPL's own official
`ep_next` estimate, blended in only for the very first projected
gameweek. A deliberate concession that FPL's number can reflect very
recent team news (press conferences, training reports) that the model's
rate-stats approach has no way to see before a deadline.

**`team_strength_min_rounds` / `team_strength_half_life_matches`** and
**`minutes_min_appearances` / `minutes_half_life_matches` /
`minutes_rotation_volatility_threshold`** -- gate thresholds and recency
decay rates for the two built-but-disabled models (see "Opponent
strength" and "Expected minutes" above for why they're off). The
`_min_*` values are currently set one unit past what a season can reach,
so they function purely as an off switch; the `_half_life_*`/threshold
values would only matter if that gate were ever lowered.

To refit and validate a change before adopting it:

```bash
cd <path-to-clone>/fpl-intelligence
python3 scripts/fetch_history.py     # once: pull prior-season data into data/history/
python3 scripts/fit_coefficients.py  # writes a *candidate* file, never overwrites the active config
python3 scripts/run_backtest.py      # scores a model version against 3 prior seasons + a held-out season
```

`fit_coefficients.py` only ever writes `config/model-coefficients.candidate.json`;
promoting a candidate to active is a manual `cp`, reviewed against the
printed before/after backtest comparison first. This is the same
model-change rule SPECIFICATION.md requires: no change is adopted without
beating the previous version's backtest.
