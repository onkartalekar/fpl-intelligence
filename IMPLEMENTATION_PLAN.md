# Projection Model Upgrade — Implementation Plan

Goal: replace the hand-tuned aggregate points-per-90 heuristic with a fitted,
component-level statistical model, while preserving every architectural
property the system was designed around: deterministic, line-by-line
auditable, free sources only, no silent weight changes, no account actions.

Policy decisions made 2026-07-25:

- **Betting odds remain excluded.** Re-affirmed, not inherited.
- **LLM news parsing is in scope**, but gated behind the statistical phases
  proving out (Phase 5).

Interface contract for all phases: `project_players()` keeps its output
shape (`xp_1/3/5`, `lower_*`, `upper_*`, `expected_minutes`,
`profile_fixture_xp`, `confidence`, …) so the optimizer, planner, chip
logic, and dashboard are untouched until a phase deliberately extends them.
Every phase bumps the model version string and must beat the previous
version on the Phase 0 backtest before adoption, per the model-change rule
in SPECIFICATION.md.

---

## Phase 0 — Historical data + backtest harness (the gate for everything)

**Why first:** the spec forbids adopting model changes without validation
against frozen history. `model_performance.py` grades live forecasts, but
with zero completed 2026/27 gameweeks there is nothing to grade yet. A
backtest over prior seasons is the only way to compare model versions now.

**Work:**

1. `scripts/fetch_history.py` — download 2–3 prior seasons of per-player,
   per-gameweek data (points, minutes, starts, xG/xA, opponent, was_home,
   team goals) from the public vaastav/Fantasy-Premier-League GitHub CSVs
   (free, MIT-licensed) into `data/history/<season>/`. Record source URL and
   retrieval timestamp in a manifest, consistent with the provenance rule.
2. `src/fpl_intel/backtest.py` — replay any projection function over
   historical gameweeks: for each origin GW, build the inputs the model
   would have seen (season-to-date aggregates only — no lookahead), project
   1/3/5-GW horizons, score against actuals with the same MAE / signed bias
   / RMSE / interval-coverage metrics `model_performance.py` uses.
3. `tests/test_backtest.py` — fixture-based tests including an explicit
   no-lookahead test (projections for GW n must not change when GW n+1
   data is removed).
4. Baseline run: score the current v0.3 model and commit the report to
   `data/backtest-baseline-v0.3.json`. Every later phase is measured
   against this file.

**Exit criteria:** baseline MAE/bias/coverage numbers for v0.3 exist and are
reproducible.

**Estimate:** 1–2 sessions. Stdlib only (csv, json).

**Status: complete (2026-07-25).** Implemented in
[src/fpl_intel/backtest.py](src/fpl_intel/backtest.py),
[scripts/fetch_history.py](scripts/fetch_history.py),
[scripts/run_backtest.py](scripts/run_backtest.py),
[tests/test_backtest.py](tests/test_backtest.py). History for 2022-23
through 2025-26 fetched into `data/history/`; baseline saved to
`data/backtest-baseline-v0.3.json` (raw comparisons stripped — rerun
`scripts/run_backtest.py` to regenerate). 2025-26 held out from fitting,
scored separately as a validation check.

Note: a second session was independently building the same harness in this
same directory concurrently (`src/fpl_intel/backtest.py` had different,
also-reasonable content at one point — not a git repo, so no branch
isolation caught this). This version is what's on disk now; if the other
session's changes are still needed, reconcile manually.

Headline results (n=228,153 fit comparisons across 2022-23/2023-24/2024-25;
n=81,700 held-out on 2025-26 — the two track closely, so the baseline
generalizes rather than overfitting a specific season):

| | MAE | Bias (actual − modeled) | RMSE | Range coverage |
|---|---|---|---|---|
| Overall (fit) | 2.68 | +1.63 | 5.28 | **9%** |
| Overall (held-out 2025-26) | 2.69 | +1.49 | 5.24 | 8% |
| Horizon 5 (fit) | 4.44 | +2.88 | 7.56 | 9% |
| Top-120-by-xp5 pool (fit) | 6.21 | **+4.73** | 9.06 | 15% |

Two findings, both stronger than anticipated when this plan was written:

1. **Uncertainty bands are badly miscalibrated.** Range coverage sits at
   8–9% against a spec target of 70–80% — the modeled lower/upper bounds
   are far too narrow almost everywhere, not just at the margins. This
   makes Phase 3's empirical-quantile refit higher priority than
   originally scoped, and the fixed 0.16/0.25/0.38 uncertainty constants
   should be treated as effectively non-functional until refit.
2. **The model systematically underpredicts its most important
   players.** Bias is a manageable +1.63 overall but balloons to +4.73 for
   the top-120 pool — the players who'd actually be selected.

**Root cause found and fixed (2026-07-25, v0.3.1):** concrete examples
pulled from 2024-25 (Cole Palmer/Haaland/Díaz projected ~1 point over 5
gameweeks after a 90-minute GW1 start) traced to
[`_expected_minutes`](src/fpl_intel/recommendations.py:46): the historical
term divided cumulative minutes/starts by a hardcoded `38` regardless of
how many gameweeks had actually elapsed, crushing the estimate for anyone
with a short current-season track record. Fixed by dividing by games
actually elapsed this season (`start_event - 1`, floored at 1), while
preserving the original `38` divisor for genuine preseason use (GW1,
before kickoff) where `minutes`/`starts` legitimately carry a full prior
season — see the `games_elapsed` branch in `project_players`. This was
**not** primarily the positional-shrinkage story originally hypothesized;
re-running the baseline as v0.3.1 shows the bug explains most of both
headline findings:

| | MAE | Bias | RMSE | Range coverage |
|---|---|---|---|---|
| Overall, v0.3 → v0.3.1 (fit) | 2.68 → 2.40 | +1.63 → +0.33 | 5.28 → 4.40 | 9% → 15% |
| Overall, v0.3.1 held-out (2025-26) | — | **−0.06** | 4.32 | 14% |
| Top-120 pool, v0.3 → v0.3.1 (fit) | 6.21 → 4.98 | **+4.73 → +0.54** | 9.06 → 7.00 | 15% → 30% |

One test (`test_recommends_roll_when_no_move_clears_profile_threshold` in
tests/test_transfer_decisions.py) needed loosening from `assertGreater` to
`assertGreaterEqual` on `net_gain_5gw`: its shared synthetic fixture pairs
large prior-season-style minutes with an early in-season gameweek, which
now correctly collapses several synthetic players to the same capped
expected-minutes value, producing a genuine near-zero-margin tie rather
than a logic defect (confirmed by inspecting the actual recommendation:
`gross_gain_5gw` was also 0.0, and the swap was between same-price,
same-club synthetic players).

Remaining gap after the fix: range coverage is still only 14–15%
overall (30% for the top pool) against the spec's 70–80% target — the
fixed 0.16/0.25/0.38 uncertainty-band widths are still too narrow on
their own terms, independent of the minutes bug. This keeps Phase 3
(empirical-quantile uncertainty bands) as the priority right after
Phase 2, and the model still shows real remaining error (MAE ~2.4
overall, ~5.0 for the players who'd actually be selected) that Phase 2's
xG-based components target directly.

---

## Phase 1 — Fitted team-strength model (replaces FDR)

**Why:** FDR is a coarse 1–5 label and the current
`0.45 + 0.55 × multiplier` mapping compresses fixture effects to roughly
±10%. A Poisson attack/defense model produces expected goals for and
against per fixture — the input every later component needs.

**Work:**

1. `src/fpl_intel/team_strength.py`:
   - Dixon-Coles-style Poisson model: per-team attack rating, defense
     rating, plus a global home-advantage term.
   - Fit by iterative proportional scaling (closed-form updates — no scipy
     needed) on match results with exponential time decay
     (half-life ≈ 12 matches; tuned in Phase 3).
   - Inputs: finished fixtures from the current-season feed
     (`team_h_score` / `team_a_score` are already collected), seeded
     preseason from the prior season's results in `data/history/`, with
     promoted teams assigned a promoted-team prior (average of the last
     N promoted sides).
2. Output per fixture: `expected_goals_for`, `expected_goals_against` for
   each side.
3. Wire into `project_players()` behind a season-progress guard: fewer than
   ~6 completed match rounds of current-season results → blend prior-season
   seed with current results; the blend weight shifts with sample size.
   Keep FDR as the labeled fallback if history files are absent, and
   surface which mode is active in `model.limitations`.
4. `tests/test_team_strength.py` — fit on a synthetic league with known
   ratings and verify recovery; verify decay weighting; verify the
   promoted-team prior path.

**Exit criteria:** backtest shows fixture-level improvement vs. the FDR
baseline (lower MAE on 1-GW horizon at minimum). Model version → 0.4.

**Estimate:** 2–3 sessions. Stdlib only.

**Status: built, backtested, and NOT adopted (2026-07-25) — a genuine
negative result.** Implemented in
[src/fpl_intel/team_strength.py](src/fpl_intel/team_strength.py): a
Dixon-Coles-style multiplicative attack/defense/home-advantage model fit
by iterative proportional scaling (biproportional fitting, closed-form
per-step updates, no external dependency), with exponential recency decay.
Wired into [project_players](src/fpl_intel/recommendations.py) via
`component_points_for_event`'s new optional `expected_goals_for`/
`expected_goals_against`/`league_avg_goals` parameters, which take
priority over the FDR tables when supplied.
[tests/test_team_strength.py](tests/test_team_strength.py) verifies
recovery on a synthetic league (correctly, via the fixture-level
`expected_goals()` invariant rather than raw attack/defense values, which
this model class only fixes up to a scale trade-off — see the
normalization comment in the source) and recency-decay behavior.

**Scope deviation from the original plan:** prior-season seeding with a
promoted-team prior was not built. It requires carrying team ratings
across a season boundary, which the no-lookahead backtest architecture
doesn't support without restructuring `load_season`/`build_origin_inputs`
to carry cross-season continuity. Implemented instead: fit strictly from
the current season's own completed fixtures, gated by a minimum-rounds
threshold below which the Phase 3 FDR tables are used instead.

**Backtest result:** at the plan's suggested default (6 rounds minimum,
12-match half-life), full 3-season backtest: MAE 2.44 (vs v0.6's 2.39),
RMSE 4.57 (vs 4.47) — worse on both, on both fit and held-out data,
despite bias and MID/FWD bias improving somewhat. Diagnosed rather than
discarded: MAE clearly improves through the season as more same-season
data accumulates for the fit (gw7-14 MAE 4.09 → gw25-33 MAE 3.19,
measured directly), confirming this is a sparse-data problem, not a
broken approach. `team_strength_min_rounds` and
`team_strength_half_life_matches` were moved into
`config/model-coefficients.json` and `min_rounds` was searched
(6/10/14/18/22/26) against the full 3-season backtest: MAE improved
monotonically as the threshold rose — but only by *converging toward*
v0.6's baseline, never surpassing it, even near a full season's worth of
same-season data. Confirmed the disabled state (`min_rounds=39`, higher
than any season) reproduces v0.6's numbers exactly, as a sanity check on
the fallback path.

**Conclusion:** per-origin-gameweek Poisson refitting, even at its best
supported sample size, does not beat the Phase 3 FDR tables (fit once on
three full seasons of aggregate results). Plausible reasons, not fully
disentangled: refitting from scratch each origin gameweek discards
cross-season learning the FDR tables get "for free"; official FDR may
already embed real team-quality judgment beyond what a from-scratch
same-season Poisson fit recovers at these sample sizes. Left in the
codebase, disabled by config
(`team_strength_min_rounds: 39` in `config/model-coefficients.json`) and
stated plainly in `model.limitations` — not deleted, since the
infrastructure (fitting, decay, normalization, backtest wiring) is real
and reusable if a future refinement (e.g. cross-season carry-over, a
properly fit half-life, blending with FDR rather than switching) closes
the gap. Model version stays at 0.6 — nothing here was adopted.

---

## Phase 2 — Component-level xG-based projection

**Why:** realized points-per-90 is the noisiest available signal, and the
spec's Projection data model already requires separate goal/assist,
clean-sheet, and bonus expectations — the current implementation is an
aggregate and therefore a spec-compliance gap.

**Work:**

1. Restructure the per-player, per-fixture projection in
   `recommendations.py` (or a new `src/fpl_intel/projection.py` that it
   delegates to) into additive components:
   - **Appearance:** from expected minutes (1 pt < 60 min, 2 pts ≥ 60).
   - **Attacking:** `expected_goals_per_90` and `expected_assists_per_90`
     (already in bootstrap), shrunk toward positional baselines exactly as
     the current rate is, then scaled by the opponent's expected goals
     conceded from Phase 1, and multiplied by position goal/assist point
     values from `bootstrap.game_settings` / element_types.
   - **Clean sheet:** Poisson P(opponent scores 0) from Phase 1 × position
     clean-sheet value × P(≥60 minutes).
   - **Goals conceded (DEF/GKP):** from opponent expected goals.
   - **Saves (GKP):** `saves_per_90` shrunk toward the GKP baseline.
   - **Bonus:** modest heuristic from historical bonus rate — flagged as
     the weakest component in `model.limitations`.
2. Keep `ep_next` as a first-event blend (weight re-fit in Phase 3).
3. Extend the projection dict with a `component_xp` breakdown per event and
   surface it in the dashboard player inspector — this *increases*
   auditability, the system's core property.
4. Role-transition scenario handling carries over unchanged (it operates on
   minutes, which components share).
5. `tests/test_projection.py` — component values for hand-computed cases;
   totals remain consistent with the output contract; downstream tests
   (`test_recommendations.py`, `test_transfer_decisions.py`) still pass
   unmodified.

**Exit criteria:** backtest beats Phase 1 overall and per-position (watch
DEF/GKP, where clean-sheet modeling should show the largest gain). Model
version → 0.5. Update SPECIFICATION.md Projection section status.

**Estimate:** 3–4 sessions. Stdlib only.

**Status: complete (2026-07-25), with Phase 1 skipped per explicit
decision** — implemented in
[src/fpl_intel/projection.py](src/fpl_intel/projection.py), wired into
[project_players](src/fpl_intel/recommendations.py). Since Phase 1's
Poisson opponent model doesn't exist, attacking/clean-sheet/goals-conceded
scaling reuses the existing FDR signal via hand-picked lookup tables
(later fitted in Phase 3, see below) instead of the plan's original
opponent-expected-goals approach — an explicit, documented substitution,
not an oversight.

First backtest (v0.4, hand-picked component constants) came back **net
worse than v0.3.1** on the fit seasons (MAE 2.46 vs 2.40, RMSE 4.61 vs
4.40) — not adopted per the model-change rule. Diagnosis: pure xG/xA
attacking scoring discards a real effect the old aggregate implicitly
captured (proven finishers sustaining goals above their own xG), and the
new bonus component was a flat, position-blind heuristic. Fixed by adding
a **shrunk residual** — a player's historical points-per-90 above what the
components alone would predict for a neutral fixture, shrunk toward zero
by the same reliability curve as every other rate — and by giving bonus a
real positional baseline instead of one flat constant. This narrowed the
gap (MAE 2.41 vs 2.40) without fully closing it; DEF improved as predicted
(bias +0.21 → +0.17) but FWD/MID bias got worse (+0.45 → +0.79 / +0.68).
Judged inconclusive rather than a clean win — see Phase 3 below for what
closed it.

`component_xp` is now a per-event field on every projection (appearance,
attacking, clean_sheet, goals_conceded, saves, bonus, residual), which is
a net auditability increase even before Phase 3: every point in a
projection now traces to a named, inspectable component instead of one
opaque blended rate.

---

## Phase 3 — Fit the constants

**Why:** every current coefficient (0.55/0.45 minutes blend, 900-minute
shrinkage, 0.16/0.25/0.38 uncertainty bands, ep_next 0.7/0.3 blend, FDR-era
leftovers) is a guess. Fitting them is cheap and strictly better.

**Work:**

1. `src/fpl_intel/coefficients.py` + `config/model-coefficients.json`:
   all tunable constants move into a versioned, dated config with the fit
   dataset recorded; code loads it with the current hand-picked values as
   defaults, so behavior is identical until a fitted file is adopted.
2. `scripts/fit_coefficients.py`, fitting on `data/history/` via the
   Phase 0 harness:
   - Shrinkage strengths (the 900-minute constant, positional baselines) by
     empirical Bayes / grid search on backtest MAE.
   - `ep_next` blend weight by grid search.
   - Uncertainty bands from empirical error quantiles per confidence
     bucket, targeting the spec's coverage diagnostic (≈70–80% inside
     lower/upper).
   - Team-strength decay half-life (from Phase 1) and minutes half-life
     (for Phase 4).
3. Adoption workflow honoring the spec: the fit script writes a *candidate*
   file plus its backtest report; a human reviews and renames it to
   active. Nothing auto-adopts.
4. Decision point recorded here: stay stdlib (grid search + closed-form
   regression is sufficient at this scale) — **no numpy unless Phase 3
   fitting proves painfully slow**, keeping the zero-dependency property.

**Exit criteria:** fitted coefficients beat hand-picked ones on held-out
season(s) (fit on season A, validate on season B). Interval coverage lands
in the target band. Model version → 0.6.

**Estimate:** 2 sessions.

**Status: complete (2026-07-25).** Implemented in
[src/fpl_intel/coefficients.py](src/fpl_intel/coefficients.py) (loader,
falls back to pre-Phase-3 defaults if the config is absent) and
[scripts/fit_coefficients.py](scripts/fit_coefficients.py). Adoption
happened live in-session: each candidate was compared against the current
baseline before being written to
[config/model-coefficients.json](config/model-coefficients.json), per the
model-change rule.

What actually got fitted, and what didn't:

- **`clean_sheet_probability_by_difficulty` and
  `goals_conceded_multiplier_by_difficulty`**: replaced with values
  computed *directly* from real historical results (finished fixtures'
  `team_h_score`/`team_a_score` conditioned on official FDR) across all
  three fit seasons — not a search, a measurement. The hand-picked guesses
  were meaningfully off: difficulty-5 clean-sheet probability was guessed
  at 16%, actually 9%; the goals-conceded spread was compressed versus
  reality (guessed 0.72–1.35× swing across difficulty, actual 0.51–1.54×).
- **`reliability_denominator` / `residual_reliability_denominator`**:
  coordinate-descent grid search confirmed the original hand-picked value
  (900) was already close to optimal — raw (unrounded) MAE keeps improving
  by less than 0.01 out past a denominator of 2000, while bias steadily
  worsens over that same range, so the search intentionally keeps the
  default rather than chase a sub-0.1%, single-season gradient. Worth
  noting as a process bug caught mid-session: the first search pass
  compared *rounded* MAE and silently tie-broke to the worst tested value
  by iteration order — fixed to compare raw MAE before any adoption.
- **`ep_next_blend_weight`**: **not fitted.** The backtest snapshot always
  sets `ep_next=0` (a pre-existing, documented backtest limitation), so
  the `if official_ep > 0` gate this weight controls never fires in any
  historical replay — a search over it would measure nothing. Left at its
  original 0.3, with that limitation now stated explicitly in
  `model.limitations` rather than silently "fit" to a meaningless result.
- **`uncertainty_bands`**: refit from empirical coverage on the full
  fit-season backtest, per confidence bucket, targeting 75%. This is the
  headline result — see below.

Backtest result, v0.3.1 → v0.5 (v0.4's component rework included):

| | MAE | Bias | RMSE | Range coverage |
|---|---|---|---|---|
| Overall (fit) | 2.40 → 2.39 | +0.33 → +0.52 | 4.40 → 4.50 | **15% → 80%** |
| Overall (held-out 2025-26) | 2.41 → 2.34 | −0.06 → +0.24 | 4.32 → 4.35 | **14% → 84%** |
| GKP bias | +0.08 → **−0.02** | | | 9% → 89% |
| DEF bias | +0.21 → +0.34 | | | 13% → 79% |
| MID bias | +0.45 → +0.72 | | | 17% → 79% |
| FWD bias | +0.45 → +0.79 | | | 15% → 79% |

Adopted as a net improvement: MAE/RMSE are roughly at parity or slightly
better, held-out coverage confirms the fit generalizes rather than
overfitting the fit seasons, and range coverage — the single most
persistent finding of this whole plan, stuck at 8–17% through every prior
version — finally lands in the spec's target band, on both fit and
held-out data. GKP bias is essentially resolved. The trade-off, fully
priced in rather than hidden: MID/FWD bias is measurably worse than
v0.3.1 and is the clearest remaining target — `model.limitations` states
this explicitly. A dedicated attacking-component refit (its own
coordinate-descent pass, or promoting Phase 1's fitted opponent model
ahead of Phase 4) is the natural next step, not started here.

**Not done in this pass:** Phase 1 and Phase 4 don't exist yet, so
`config/model-coefficients.json` has no team-strength decay half-life or
minutes half-life to fit — those fields are simply absent from the
schema, to be added when those phases land.

---

## Phase 3 continuation — MID/FWD attacking bias (2026-07-25)

**Why:** Phase 3 left MID/FWD attacking bias worse than the pre-Phase-2
baseline (+0.56/+0.51 vs +0.45/+0.45) as an explicit, known gap. Tackled
directly as a follow-up rather than folded into a future phase.

**Investigation, in order:**

1. **Empirically fit the attacking-side FDR multiplier** (`_FDR_ATTACK_MULTIPLIER`,
   previously hand-picked and never revisited when Phase 3 fit the
   defensive tables). Real historical goals-scored-by-difficulty is a
   2.4× spread (0.595–1.446); the hand-picked table compressed to 1.22×
   after its own dampening formula. Fit and applied directly (dampening
   removed — no longer needed once the table reflects real data). Tested
   in isolation: **no effect on bias** (weighted average across the real
   fixture-difficulty distribution was already ~1.0 either way) — this
   was a variance/RMSE fix, not a bias fix, and confirmed as such before
   moving on rather than assumed.
2. **Tested raising the residual's reliability cap** (hypothesis: even a
   proven, large-sample over-performer never earns more than 82% credit
   for their edge). Result: **the cap never binds** — a full 3420-minute
   season only reaches ~0.79 reliability under the existing denominator,
   so the 0.82 cap is structurally unreachable within a season. Confirmed
   empirically (flat result across cap=0.82 through 1.0) before discarding
   the hypothesis, rather than assumed from theory alone.
3. **Lowered the residual's reliability *denominator*** (trust the
   residual sooner, with less data) — the actual lever. A single-season
   scan showed a real trade-off: FWD bias fell from +0.20 to ~0 as the
   denominator dropped from 900 to 30, but DEF bias moved the *opposite*
   direction. Checking DEF's bias sign against the full 3-season Phase 3
   result (+0.34, opposite sign from this single season's -0.12) showed
   DEF's bias direction is unstable across seasons — so DEF (and GKP,
   untested) were deliberately left at the original 900 rather than fit
   off a single, possibly-noisy season. `residual_reliability_denominator`
   became `residual_reliability_denominator_by_position`, fit only for
   MID/FWD using the full, more robust 3-season backtest, then validated
   on the held-out 2025-26 season before adoption.
4. Uncertainty bands re-fit against the final configuration to confirm
   they hadn't gone stale — bands were unchanged (0.82/0.98/1.0), as
   expected: match-to-match variance, not systematic bias, dominates band
   width.

**Result**, v0.5 → v0.6:

| | MAE | Bias | RMSE | Range coverage |
|---|---|---|---|---|
| Overall (fit) | 2.39 → 2.39 | +0.52 → +0.42 | 4.50 → 4.47 | 80% → 81% |
| Overall (held-out) | 2.34 → 2.37 | +0.24 → **+0.12** | 4.35 → 4.39 | 84% → 85% |
| MID bias (fit) | +0.72 → **+0.56** | | | |
| FWD bias (fit) | +0.79 → **+0.51** | | | |

Adopted: MID/FWD bias improved substantially (roughly a 30–35% reduction)
on both fit and held-out data, confirming genuine generalization rather
than a fit-season artifact, at a small honest cost — held-out MAE/RMSE
tick up slightly (2.34→2.37, 4.35→4.39). GKP/DEF/coverage are essentially
unaffected, since the fix is scoped specifically to MID/FWD's residual
trust. `model.limitations` states plainly that the bias is reduced, not
eliminated, and that DEF's residual trust remains unfit pending a more
robust multi-season search of its own.

---

## Known gaps carried forward (recorded 2026-07-25; all five closed same day)

The first two items surfaced when the plan was reviewed after Phase 3; the
rest surfaced in a second verification pass (grep/inspection against the
actual repo state, not the doc's own claims) after Phases 1/4/5 concluded.
All five are now closed, in priority order (most urgent first) — kept as
a record of what was found and how each was fixed, not deleted once
resolved, consistent with how every other decision in this plan is
tracked:

1. **`scripts/fit_coefficients.py` reproducibility footgun — CLOSED
   2026-07-25.** Before fixing, verified the actual failure mode
   empirically (backed up the active config, ran the old script for real,
   diffed) rather than trusting the original write-up — which turned out
   to be partly wrong. Corrected finding: `_write_config`'s read-modify-
   write pattern *did* preserve unknown keys (`attack_multiplier_by_difficulty`,
   `residual_reliability_denominator_by_position`, the Phase 1/4 disable
   values) across a re-run — they do not silently vanish as first
   described. The real, confirmed damage was different: (a) `model_version`
   regressed 0.6 → 0.5 and the `source` field lost the record of the
   Phase 1/4/MID-FWD work, both misleading provenance; (b) a dead orphan
   key (`residual_reliability_denominator`, the old scalar name) got
   appended; (c) most importantly, its Step 3 search was a confirmed
   **silent no-op bug**, independent of the config-overwrite risk: it grid-
   searched that same dead scalar key, which nothing in `coefficients.py`
   reads since the Phase 3 continuation replaced it with the per-position
   version — all six candidate values it tried produced byte-identical
   MAE/bias (2.28510 / -0.04592), confirmed by direct measurement.

   Fixed by rewriting the script to: build a candidate from a full copy of
   the active config (so no key can ever be dropped, known or not, by
   construction rather than by a maintained preserve-list); write only to
   `config/model-coefficients.candidate.json`, restoring the real active
   file in a `finally` block even if a search step fails midway; remove
   the dead Step 3 search entirely and replace it with an explicit "not
   touched, carried forward" printout for the position-specific/Phase-1/4
   keys, honest that this script doesn't attempt to reproduce that bespoke
   investigation; and add a direct empirical fit for
   `attack_multiplier_by_difficulty` (goals-scored-by-difficulty, the same
   method already used for the two goals-conceded-side tables), which the
   original script never computed at all despite the config carrying it.
   Verified post-fix: ran the corrected script for real, confirmed via
   md5sum that the active config file is byte-identical before and after,
   and confirmed the candidate it produced matches the active config
   exactly (expected — no new historical data since the last real fit).
   **Safe to run now; still promotes only on an explicit `cp`.**

2. **`component_xp` was never wired into the dashboard — CLOSED
   2026-07-25.** Phase 2 item 3 said to extend the projection dict with a
   per-event component breakdown *and* surface it in the dashboard player
   inspector; only the data half had been done. Added a "Player scoring
   breakdown" panel to the Decision Center view: every player card in the
   full squad, bench, and starting-XI pitch view is now a focusable button
   (`data-player-id`, `tabindex="0"`, `role="button"`, keyboard-activatable
   with Enter/Space) that renders a per-gameweek table of all seven named
   components plus total, defaulting to the captain on load. Also surfaces
   `uses_team_strength`/`uses_recency_minutes` inline in the panel heading
   when true (currently never, since both are disabled) — closing the
   related, lower-stakes half of this gap at the same time. Implemented as
   pure HTML-attribute/CSS/JS additions with zero changes to existing
   element types, to avoid visual regression risk in a large, minified,
   hand-written template with no test coverage for pixel layout. Verified
   two ways: `tests/test_dashboard.py` still passes unmodified (7 tests),
   and a real refresh + live browser check (clicking both a squad-grid
   card and a pitch-formation player) confirmed the panel updates
   correctly and shows position-appropriate values (a clicked forward
   correctly shows 0.00 for clean_sheet/goals_conceded/saves).

3. **README.md is stale — CLOSED 2026-07-25.** Added a "Projection model"
   section: what the model is (deterministic, component-level, fitted
   constants, no ML/foundation model/betting odds), where its version and
   limitations show in the dashboard, and the three-command validation
   workflow (`fetch_history.py` → `fit_coefficients.py` →
   `run_backtest.py`) with a link to this plan for the full history.
   Updated the stale "Current status" bullet that claimed projections
   were still waiting on the season feed — verified live against the real
   FPL API while writing this fix (the 2026/27 feed and its GW1 deadline
   are in fact live now) rather than left unverified. Deliberately did not
   hardcode a test count in prose, since that would just go stale again;
   `## Tests` already gives the runnable command. Verified the two new
   command lines actually work exactly as written — ran both with
   `PYTHONPATH` unset (matching the doc, since both scripts self-manage
   `sys.path`) and confirmed `fit_coefficients.py` still only touches its
   candidate file, never the active config.

4. **Cosmetic — CLOSED 2026-07-25.** `backtest.py`'s `build_backtest_report`
   defaulted `model_version="0.3"`. Verified via grep that all four real
   callers (`run_backtest.py` ×2, `fit_coefficients.py`, `test_backtest.py`)
   already pass it as an explicit keyword argument, so removed the default
   entirely rather than swap in a new literal that would just go stale
   again the next time the model version moves — `model_version` is now a
   required parameter, with a docstring note explaining why. Safe to
   reorder ahead of the other keyword-only-used parameters since every
   caller passes all of them by keyword, confirmed by the same grep.

5. **SPECIFICATION.md was never updated — CLOSED 2026-07-25.** Phase 2's
   exit criteria required updating its Projection section status once the
   component model landed; this had been missed. Fixed alongside the
   Phase 1/4/5 work: the Projection section now states the component
   model's implementation status (including that team-strength and
   recency-minutes were built and rejected, not adopted), and a Phase 5
   amendment documents the news-signal extractor, its bounds, and its
   fallback, per that phase's own work item 5.

Not gaps (verified deliberate, each documented in its phase's status
section or `model.limitations`): Phase 4's live `element-summary` fetch
(deferred — model not adopted), Phase 5's live-pipeline and dashboard
wiring (gate unmet), Phase 1's cross-season seeding and Phase 4's
congestion adjustment (documented substitutions/scope-outs), DEF's
residual refit (parked pending a multi-season search).

---

## Phase 4 — Recency-weighted minutes model

**Why:** minutes prediction is the highest-value, weakest component. The
current estimate is a whole-prior-season average with no recency, rotation,
or congestion awareness.

**Work:**

1. Extend `fpl_data.py` with the official per-player
   `element-summary/{id}/` endpoint (recent match minutes/starts). Fetch
   only for planner-relevant players (owned squad + candidate pools,
   ~200–300 players), cached to `data/element-history/` with timestamps to
   keep refresh time acceptable.
2. `src/fpl_intel/minutes.py`:
   - Start probability from exponentially decayed start share
     (half-life fit in Phase 3), blended with season-long share by sample
     size.
   - Expected minutes = P(start) × E[minutes | start] + P(sub) × E[sub
     minutes], each estimated from the same decayed history.
   - Rotation flag from start-share volatility; fixture-congestion
     adjustment from days-since-last-match (kickoff times are already in
     the fixtures feed).
   - Availability multiplier and role-transition scenarios carry over.
3. Profile minutes scenarios become quantiles of the start-probability
   estimate rather than fixed 0.62/0.78/0.92 multipliers.
4. Preseason degrades gracefully to the current historical method, labeled
   in `model.limitations`.
5. `tests/test_minutes.py`.

**Exit criteria:** backtest improvement concentrated in
minutes-uncertain players (rotation-risk cohort MAE). Model version → 0.7.

**Estimate:** 2–3 sessions.

**Status: built, backtested, and NOT adopted (2026-07-25) — a second
genuine negative result.** Implemented in
[src/fpl_intel/minutes.py](src/fpl_intel/minutes.py): exponentially
decayed start-share and per-appearance minutes (started vs. sub), combined
into `expected = start_share * avg_minutes_started + sub_share *
avg_minutes_sub`; `is_rotation_risk` flags players whose recent share
diverges from their season-long share; `minutes_scenarios_from_history`
widens the conservative/aggressive spread for flagged players instead of
using a fixed multiplier only for role-transition players. Wired into
[project_players](src/fpl_intel/recommendations.py) via a new
`recent_history` field on bootstrap elements, used when present and
sufficient, falling back to `_expected_minutes` otherwise.
[tests/test_minutes.py](tests/test_minutes.py) covers nailed-on starters,
never-featured players, rotation-risk detection, and hand-computed
mixed start/sub cases (14 tests).

**Scope deviation:** fixture-congestion adjustment (days-since-last-match)
was not built — the historical dataset used for backtesting doesn't
expose reliable per-gameweek kickoff timing, so it was scoped out rather
than half-built. The live `element-summary` endpoint fetch/caching in
`fpl_data.py` was also not built in this pass, since backtest validation
(the more urgent question) only needed the per-gameweek history already
loaded by `backtest.py` — building the live fetch path for a model that
hadn't yet cleared validation would have been premature.

**Backtest result — a real regression, not a measurement artifact.**
First check (full 3-season backtest, default half-life 4.0 matches, min 3
appearances) showed the eligible comparison count drop from 228,153 to
159,456 with MAE apparently up to 3.12 — investigated rather than trusted
at face value, since a shrunk eligible population can mechanically inflate
average error by excluding easy "correctly predicted zero" cases. Redid
the comparison on matched single-season slices to isolate the real effect:
recency-enabled MAE 4.98 vs. disabled MAE 3.70 on the *same* season and
horizon — confirming a genuine ~35% degradation, not a population
artifact. Searched the half-life (4/8/12/20/30/50 matches): MAE got
steadily *worse* as half-life increased, converging toward but never
reaching the disabled baseline — the opposite of what "recency helps"
would predict, and inconsistent with a simple mistuned-decay explanation.
Inspected the worst individual regressions directly (Núñez, Lascelles,
Romero, Burn, Maguire, Trossard, Murillo, Lo Celso): the model swings
expected minutes substantially in both directions based on short-term
patterns that don't hold up over the next five gameweeks, adding variance
without a clear compensating gain. A rough minutes-only check (ignoring
points conversion) suggested the underlying minutes estimate might
actually be closer to true future minutes than the old model's — so the
damage likely concentrates in how that estimate propagates through
scoring, not in the minutes estimate itself; not fully disentangled.

**Conclusion:** the functional form (`start_share * avg_minutes_started +
sub_share * avg_minutes_sub` from a short decayed window) trades the old
model's dampened, blended formula for something that reacts faster but
overreacts to noise that doesn't persist — cup rotations, a single knock,
a manager's one-off tactical choice. Disabled by config
(`minutes_min_appearances: 39`, higher than any season) and stated
plainly in `model.limitations`. Left in the codebase as real,
reusable infrastructure: the most promising next step is not more decay
tuning but a different functional form — blending this signal as a
*correction* on top of the already-validated `_expected_minutes` estimate
(the way Phase 2's residual fix worked) rather than replacing it outright.
Model version stays at 0.6 — nothing here was adopted either.

---

## Phase 5 — LLM news parsing (gated)

**Gate:** enter only after Phases 1–4 are adopted **and** live calibration
in `model_performance.py` has reached its ≥8-comparison threshold with
stable results. This phase adds the project's first external inference
dependency; the statistical base must be proven first.

**Scope:** a foundation model is used **only as a feature extractor** —
press conferences and official injury news (already tiers 2–3 of the
source hierarchy) parsed into structured minutes signals. The projection
formula itself stays fully transparent.

**Work:**

1. Collector for official club news / PL injury pages (first-party only,
   consistent with the transfer collector's source rules).
2. `src/fpl_intel/news_signals.py` — calls the Claude API
   (claude-haiku for cost; structured JSON output) to extract per item:
   `{player, availability_signal, expected_return, role_hint, confidence,
   source_url, exact_quote}`.
3. Signals apply **bounded** adjustments to the Phase 4 minutes model
   (e.g. cap at ±25% of modeled start probability), and every applied
   adjustment renders in the dashboard with the exact quote and source
   link — the same provenance treatment transfers get.
4. Failure-safe: no API key / API down → pipeline runs identically with
   zero signals. Key from an environment variable, never stored, per the
   no-credentials rule (this is an API key for inference, not an FPL
   account credential — note the distinction in the spec amendment).
5. Spec amendment: document the extractor, its bounds, and its fallback in
   SPECIFICATION.md.
6. `tests/test_news_signals.py` with recorded fixtures (no live API in
   tests).

**Exit criteria:** measurable improvement on the rotation-risk cohort in
live calibration over ≥8 comparisons; adjustments always displayed with
provenance.

**Estimate:** 3–4 sessions.

**Status: scaffolding built and tested, deliberately NOT gate-open
(2026-07-25).** Implemented in
[src/fpl_intel/news_signals.py](src/fpl_intel/news_signals.py):
`extract_availability_signals` (raw HTTPS call to the Claude Messages API
— no `anthropic` SDK dependency, matching this project's stdlib-only
convention elsewhere — with an injectable `caller` for testing),
`bounded_minutes_adjustment` (Β±25% cap, pure function), and
`fetch_news_item` (a minimal single-URL fetch-and-strip-tags collector —
explicitly not a real per-club news scraper, which is a separately-scoped
future undertaking). [tests/test_news_signals.py](tests/test_news_signals.py)
covers success, malformed/non-list/missing-field responses, API failure,
and the adjustment bounds — 15 tests, all against recorded fixtures, zero
live API calls per the plan's own requirement.

**This phase's own gate is unmet, on both counts it names, not just the
live-calibration one:**

1. "Phases 1-4 adopted" — Phase 1 (team-strength) and Phase 4
   (recency-weighted minutes) were both built and backtested but **not**
   adopted; both are genuine negative results, disabled via config.
2. "Live calibration ≥ 8 comparisons" — the 2026/27 season has not
   started, so there are zero completed live forecasts to calibrate
   against.

Built anyway, at explicit user request, as reserve infrastructure: nothing
in this module is called by `project_players()` or `refresh.py`, and
`ANTHROPIC_API_KEY` is not required by anything in the live pipeline.
When the gate is eventually met — Phase 1/4 (or replacements) adopted, and
enough live gameweeks completed — wiring `bounded_minutes_adjustment`
into the active minutes estimate (whichever one is in production at that
point) and surfacing signals with their quote/source in the dashboard
player inspector is the remaining integration work; the extraction and
adjustment logic itself is already built and tested.

**Amendment (2026-07-25) — made provider-agnostic.** The extractor was
originally hardcoded to the Claude Messages API. At user request (wanting
to also drive this project's development with a different agent harness,
which surfaced the question of whether the *codebase itself* had any
hard vendor lock-in), `news_signals.py` was refactored to a small
provider registry (`_PROVIDERS`): the original Claude caller is unchanged
and remains the default, plus a new `_call_openai_compatible` caller for
any host implementing the OpenAI Chat Completions shape, configured
entirely through environment variables (`FPL_INTEL_LLM_API_BASE`,
`FPL_INTEL_LLM_MODEL`, `FPL_INTEL_LLM_API_KEY`) — no third-party endpoint
is guessed or hardcoded. Provider selection is `FPL_INTEL_LLM_PROVIDER`
(env var) or an explicit `provider=` argument; an unrecognized provider
fails safe to zero signals, consistent with the rest of this module's
error handling. `tests/test_news_signals.py` grew 4 tests covering
provider selection, the env-var default, and the new caller's own
env-var-missing fallback (124 tests total, all passing). This was the only
place in the codebase with any LLM-vendor-specific code — confirmed by
grepping the repo for `claude`/`anthropic` before making this change; the
projection formula, backtest harness, coefficient fitting, and dashboard
have no LLM dependency of any kind.

---

## Post-v0.7 fix — zero-track-record signings collapsed to near-zero minutes (2026-07-26)

**Context:** outside this document's own phase sequence, the model was
substantially rewritten to v0.7 (event-specific per-gameweek lineups/
captains, club-fixture-count minutes denominators, covariance-adjusted
team uncertainty ranges, exact Poisson goals-conceded deductions, and
provisional defensive-contribution scoring — see `config/model-
coefficients.json`'s `source` field for the full list). That rewrite was
not documented here at the time; this entry covers a bug found in it via
`/code-review` and fixed the same day, not the rewrite as a whole.

**Bug:** `_expected_minutes()` (`src/fpl_intel/recommendations.py`) lost
its floor derived from FPL's own `ep_next` estimate when the minutes
formula was rebased from elapsed-Gameweeks to the player's-club
fixtures-played. Any player with zero recorded minutes and zero starts
for their *current* club — a genuine Premier League debutant, a
permanent transfer, or a returning loanee, not just a fringe player —
collapsed to the model's "never started" baseline (≈3.6 expected
minutes) regardless of price, reputation, or how strongly FPL's own
`ep_next` rated them. Verified against the live 2026/27 bootstrap: a
£7.0m new-club signing (Rashford) projected at just 1.29 five-gameweek
points before the fix — about 17x below a genuinely comparable
established player — which measurably distorted squad-optimizer output,
since an entire category of real transfer-market options was never in
fair contention against established players.

**Fix:** reinstated an `ep_next`-derived floor (60/45/25/0 minutes for
`ep_next` ≥3/≥2/>0/=0, matching the pre-v0.7 thresholds), but scoped
strictly to `minutes <= 0 and starts <= 0` — i.e. only players with zero
recorded involvement for their current club. Established players (any
real minutes or starts) are untouched, so `ep_next` still never overrides
a real track record — the specific, deliberate behavior
`test_ep_next_affects_only_first_event_points_not_expected_minutes`
already locked in for the v0.7 rewrite. Two new tests
(`test_zero_track_record_signing_floors_expected_minutes_from_ep_next`,
`test_established_player_track_record_is_not_overridden_by_ep_next_floor`)
cover the new-signing floor and confirm it doesn't leak into the
established-player case. Re-verified against live data after the fix:
the same signings now project in a plausible 3-8 point five-gameweek
range (scaled by their own `ep_next`) instead of ~1.2.

---

## Considered and declined — npxG / xT / SCA / GCA as new inputs (2026-07-26)

**Context:** while documenting the model's use of `xG`/`xA`, the question
came up of whether other advanced football-analytics stats (non-penalty
xG, expected threat, shot-/goal-creating actions) could sharpen the
attacking projection further. Researched their availability rather than
guessing.

**Findings:**
- **npxG** (non-penalty xG) — Understat.com has published per-player npxG
  for the Premier League every season since 2014/15, free to view, via an
  undocumented internal JSON endpoint (`understatapi` is a community
  scraper for it). FBref carried it too until Stats Perform terminated
  the feed and had all Opta-sourced advanced stats pulled from
  FBref/Stathead on 2026-01-20 (see the Opta Analyst entry below) —
  Understat's own npxG model is unaffected and remains the free-to-view
  option. Note FPL's own official
  `expected_goals` field (already used by this model, via
  `data/history/`) is *not* penalty-adjusted, so this would be a genuinely
  new signal, not something derivable from data already on hand.
- **SCA/GCA** (shot-/goal-creating actions) — FBref publishes per-season
  Premier League tables for these back through at least 2020-21, covering
  all three fit seasons. Free to view, no bulk API.
- **xT** (expected threat) — not available as a published per-player
  historical stat anywhere. It's a possession-value model computed from
  event-level (pass-by-pass) data, and the only free event-level source
  (StatsBomb's open data) doesn't cover recent full Premier League
  seasons. Effectively unavailable without a paid data provider.

**Decision: not pursued for now.** Both npxG and SCA/GCA are technically
obtainable, but only via scraping pages FBref/Understat don't offer a
bulk download or public API for — FBref (part of the Sports-Reference
family) explicitly rate-limits bot traffic to roughly 10 requests/minute
and is scrape-averse. That would make these the project's first
dependency on unofficial, ToS-sensitive third-party data, a different
provenance category than every other input (official FPL API fields,
redistributed as-is by `data/history/`'s vaastav mirror). Not silently
dropped -- recorded here as a real option if the sourcing constraint is
ever revisited.

---

## Considered and declined — Opta Analyst (theanalyst.com) as a data source (2026-07-27)

**Context:** GitHub issue #13 asked whether Stats Perform's Opta
Analyst site (theanalyst.com/competition/premier-league/stats) could
supply advanced Opta metrics beyond the official FPL fields. Researched
what the site actually exposes rather than guessing.

**Findings:**
- The stats page is a JS-rendered shell; its data loads from an
  undocumented JSON endpoint
  (`dataviz.theanalyst.com/project-data/soccer/{compSeasonUUID}/player-stats.json`)
  found by reading the site's JS bundle. The feed is real and rich --
  per-player npxG, xG-per-shot, shot conversion, chance creation,
  carries, defending, goalkeeping -- but every row is a
  **season-aggregate total**, with no per-gameweek or per-match
  breakdown, and only the current season's UUID is exposed. That alone
  makes it unusable for the backtest harness, which needs per-player
  pre-origin-gameweek values across ~3 seasons.
- Stats Perform's Terms of Use (covering theanalyst.com) limit material
  to "personal, non-commercial use" and prohibit copying, reproduction,
  and redistribution; theanalyst.com's robots.txt disallows the whole
  site to automated agents (Scrapy, GPTBot, ClaudeBot, CCBot, etc.).
  There is no documented API, bulk download, or free license -- the
  licensed product is the paid Opta Data Feeds API.
- The free licensed route that used to exist is gone: Stats Perform
  terminated FBref's Opta feed and had all Opta-sourced advanced stats
  removed from FBref/Stathead on 2026-01-20 (reflected in the npxG
  entry above, updated same day this was researched). No free,
  licensed Opta redistribution currently exists for the Premier League.

**Decision: declined.** Fails the sourcing bar on two independent
grounds: (1) provenance -- an undocumented endpoint on a ToS-restricted,
robots-disallowed site owned by a company that actively pulled this
same data from FBref is a strictly worse dependency than the
FBref/Understat scraping already declined above; (2) backtestability --
season-aggregate current-season data can never be evaluated against the
project's out-of-sample MAE bar (SPECIFICATION.md's model-change rule),
so criterion (d) is unreachable even before the legal question. If
advanced non-FPL metrics are ever revisited, Understat npxG (recorded
above; per-match history since 2014/15, its own model rather than Opta)
dominates this option on every criterion.

---

## Phase 6 — ICT Index investigation

**Why:** FPL's official bootstrap payload includes an Influence/
Creativity/Threat composite (`ict_index`), already present in
`data/history/*/merged_gw.csv` (unlike npxG/xT/SCA/GCA — see the
"Considered and declined" entry above, this needs no new data source).
It captures shots, key passes, and general match involvement that isn't
fully reducible to `expected_goals_per_90`/`expected_assists_per_90`, so
it was worth checking whether it explains any of the projection model's
current error rather than assuming either way.

**Work:** `scripts/investigate_ict_index.py` — a standalone research
script (not part of the fit/validate pipeline; writes nothing to
`config/model-coefficients.json`). Reuses `backtest.season_comparisons()`
unmodified to get, per player/origin-gameweek/3-GW-horizon across all
three fit seasons, the model's already-computed `modeled_points`,
`actual_points`, and signed `error`. Joins in each player's own
season-to-date `ict_index` per 90 minutes, computed strictly from
gameweeks before that origin (same no-lookahead boundary as the rest of
the harness), requiring at least 180 pre-origin minutes before trusting
the rate. Two checks, in increasing order of rigor:
1. Correlates (Pearson r) that rate against the model's current error,
   its own `modeled_points`, and `actual_points` directly -- cheap, but
   only answers "is there a relationship," not "would adding this
   actually beat the current backtest."
2. Fits a single linear, position-centered ICT correction on an early
   training split (GW10-20), then measures its effect on held-out MAE
   (GW21-30) -- the same out-of-sample bar SPECIFICATION.md's
   model-change rule holds every other phase to, not just an in-sample
   correlation that could be chasing noise.

**Exit criteria:** the correction must beat baseline MAE on the held-out
split by more than 0.01 (the same real-improvement bar
`fit_coefficients.py` uses for `reliability_denominator`) to be
considered a real, actionable signal; anything at or below that bar is a
clean negative result, not further pursued.

**Status: investigated (2026-07-26) — a clean negative result on both
checks, not adopted.** n=21,278 player/origin/horizon comparisons pooled
across all three fit seasons (min. 180 pre-origin minutes):

**Correlation screen:**

| Comparison | Pearson r |
|---|---|
| `modeled_points` vs `actual_points` (sanity baseline) | 0.433 |
| Pre-origin ICT rate vs forward `actual_points` | 0.237 |
| Pre-origin ICT rate vs `modeled_points` | 0.375 |
| Pre-origin ICT rate vs model **error** (`actual` − `modeled`) | 0.019 (2022-23: 0.058, 2023-24: 0.009, 2024-25: −0.006) |

ICT Index is real signal — it does correlate with a player's forward
points (0.237) — but it correlates even more strongly with what the
model already projects (0.375), and has essentially no relationship
with where the model is currently wrong (0.019, and inconsistent in
sign across seasons individually). A high-ICT player is already being
projected well by `xG`/`xA`/bonus/residual; ICT Index mostly restates
information the model already has under a different name.

**Out-of-sample MAE check (the actual adoption bar):** fit on
GW10-20 (n=11,480) found a correction weight of essentially zero
(-0.0004). Applied to the held-out GW21-30 split (n=9,798): baseline
MAE 3.9571 → ICT-corrected MAE 3.9571, an improvement of 0.0000 --
confirming the correlation-screen finding with the same rigor as every
adopted/rejected phase above, not just an in-sample correlation.

Not incorporated. Script left in `scripts/` as reusable infrastructure
if a future model change (e.g. a different attacking-component formula)
changes what the model's error looks like enough to be worth
re-checking against ICT.

---

## Considered and declined — transfer-driven team strength / squad-value panel (issue #31, 2026-08-01)

**Context:** issue #31 asked whether summer transfer activity should
adjust team strength or fixture difficulty, since neither currently
reacts to it. Full investigation in `plans/issue-31-transfer-strength.md`.

**Candidate 1 — feed a squad-value-delta into `team_strength.py` /
the projection formula: declined.** Blocked by two independent things.
First, an architectural wall `team_strength.py` had already hit on
2026-07-25 for the closely related idea of seeding ratings from a
preseason prior: the no-lookahead backtest architecture evaluates each
season independently, with no cross-season state carry, and any
transfer-window signal inherently needs to carry from the summer into
the new season's early gameweeks. Second, `backtest.py` already
documents "no historical transfer-window feed available offline" as a
known simplification — confirmed by checking `data/history/{season}/`,
which holds only match results, never transfer records. Even if the
first blocker were solved, there is nothing to run the required
out-of-sample check against. Beyond both blockers: the underlying
mechanism this would enhance is itself a documented negative result —
Phase 1's fitted team attack/defense model lost to the static FDR
baseline (MAE 2.44 vs. 2.39) — so this is not being kept open as a
"revisit once data exists" item the way npxG/SCA/GCA above are.

**Candidate 2 — a display-only "squad changes this summer" panel
(price-proxy value delta per club from confirmed transfers): declined.**
This doesn't touch the projection formula, so it isn't bound by the
backtest rule at all — the reason for declining it is different. Mocking
it up against real data across all 20 clubs found a coverage gap far
worse than an isolated missing player: departing players drop out of the
FPL bootstrap list much faster than arriving ones are added to it, so
every club's outgoing total is *systematically* less complete than its
incoming total (e.g. Liverpool: 2 priced departures found against 15
unpriced; Arsenal: 0 against 11). That biases the net figure in the same
direction for every club, which a "directional estimate" disclaimer
doesn't fix — it isn't imprecise, it's wrong in a consistent direction.
Not shipped.

**What shipped instead:** a departure or arrival still has a real,
model-relevant effect this project already had partial infrastructure
for — the *minutes* competition it creates for the players who stayed.
`_recent_role_transitions()`/`_minutes_scenarios()` in
`recommendations.py` already widened a transferred player's own
expected-minutes scenarios; extended to their same-club, same-position
teammates (`_teammate_transfer_impacts()`), it needed no new backtest
justification, since it extends a mechanism `backtest.py` already
excludes from replay for the same lack of historical transfer data.
Shipped 2026-08-01.

---

## Considered and declined — dark-palette overhaul and general redesign (issue #48, 2026-08-08)

While adding the light/dark theme toggle (issue #48), two adjacent candidates were considered and declined:

- **Dark-palette overhaul.** A WCAG contrast audit of the 15 most-used foreground/background pairs found every pair passes AA and 11 of 15 pass AAA (worst: the deliberately de-emphasized `--low` token at 4.33:1). With no measurable defect, a palette redesign would be aesthetic churn with regression risk. The theme-toggle tokenization means any future palette change is a one-block edit, so nothing is foreclosed.
- **General page redesign.** Declined as unscoped: no reference design, named problem, or prioritized views were identified. Incremental, friction-driven UI issues (the pattern of #22/#23/#39) remain the preferred path; a broad redesign can be revisited if concrete direction emerges.

Full investigation, including the live mockup that found a variables-only toggle would break the UI, in `plans/issue-48-theme-redesign.md`.

---

## Considered and declined — serving CSS/JS as separate HTTP-served files (issue #51, 2026-08-08)

While addressing the CSS/JS-embedded-in-Python friction from #51 (surfaced during the #48/#50 theme work), serving CSS and JS as separate files alongside `dashboard.html` via new `server.py` routes was considered and declined in favor of keeping them as real source files (`src/fpl_intel/dashboard.css`, `src/fpl_intel/dashboard.js`) inlined into `dashboard.html` at generation time -- the same mechanism `__DASHBOARD_DATA__` already used. The HTTP-serving approach was live-verified as mechanically workable (extracted CSS/JS loaded and ran correctly via `<link>`/`<script src>` over a real HTTP server, zero console errors) but was declined because `server.py`'s `do_GET` would need new routes and manual MIME-type handling it doesn't have today, it introduces a generation-version-skew risk class that doesn't exist today (a stale cached asset served against a newer `dashboard.html`), and it breaks the README's documented standalone-file usage (copying or opening just `dashboard.html` elsewhere, with styling and interactivity intact) unless sibling files travel with it. The chosen approach delivers the same author-facing tooling benefits (real syntax highlighting, real diffs, a real linter for CSS) with none of those costs, since the served artifact doesn't change at all -- verified via a direct A/B render comparison against the pre-change code, semantically byte-identical output for the same input state. Full investigation in `plans/issue-51-extract-css-js.md`.

---

## Considered and declined — PL team manager playing style as a model input (issue #11, 2026-08-08)

**Context:** issue #11 asked whether a club's manager/playing style (high-
press vs. low-block, rotation policy, attacking vs. defensive setup) could
improve projections or squad recommendations. No `merger`/tactics field
exists in any data the project already uses, so every candidate was first
judged on data availability, then validated against the out-of-sample MAE
bar (SPECIFICATION.md's model-change rule) where testable. Full
investigation, including the candidate-by-candidate analysis, in
`plans/issue-11-manager-style-investigation.md`.

- **(a) Team-level attack/defense rates — declined, re-litigation.** This
  is Phase 1's Dixon-Coles-style team-strength model in different clothes
  (MAE 2.44 vs. FDR-baseline 2.39, never adopted). Not re-tested; the
  disabled infrastructure (`team_strength_min_rounds: 39`) remains
  available if a genuinely new refinement is ever proposed.
- **(b) Manager-change events as a discontinuity signal — deferred.** A
  freely available, structured source exists (Wikipedia's "List of
  Premier League managers" tenure table, CC BY-SA, ~20-30 in-season
  changes across the three fit seasons), but intersecting those events
  with the GW10-30/3-GW-horizon backtest windows leaves too small a
  sample to clear the 0.01 MAE bar with any confidence, and a hand-
  maintained dataset would be the project's first non-FPL, manually
  curated input. Revisit only if a discontinuity check is separately
  motivated.
- **(c) Team rotation propensity from lineup turnover — tested,
  declined.** `scripts/investigate_team_rotation.py` (mirrors
  `scripts/investigate_ict_index.py`'s structure and rigor): a per-team,
  season-to-date rotation index (mean starters changed between
  consecutive fixtures, `MIN_PRE_ORIGIN_FIXTURE_PAIRS = 6`), joined onto
  `backtest.season_comparisons()`'s existing modeled/actual/error via
  each player's latest pre-origin team.

  One data issue was found and corrected before trusting any result:
  2022-23's `merged_gw.csv` has the `starts` column but it is entirely
  unpopulated (0 recorded starters for every fixture) through GW15 — a
  real PL XI is never empty, so those fixtures are dropped as a missing-
  data artifact rather than treated as 0-rotation matches. Before this
  fix, 2022-23's rotation index spanned an implausible 5.6-7.0 (vs.
  1.4-4.0 for the other two seasons) and the corrupted early-origin
  training data produced a spurious "adopt" reading (+0.033 held-out MAE
  improvement); after excluding those fixtures, all three seasons land
  in a plausible 1.0-4.0 range and the result reverses to a clean
  decline.

  **Correlation screen** (n=32,935 player/origin/horizon comparisons
  pooled across all three fit seasons; sanity-baseline `modeled_points`
  vs `actual_points` r=0.630 — higher than Phase 6's 0.433 because this
  join is team-level, not gated on 180+ player minutes like the ICT
  script's, so it keeps more easy near-zero/near-zero fringe-player
  pairs; the unfiltered `season_comparisons()` population itself is
  r=0.616, confirming the gap is the join criterion, not a bug):

  | Comparison | Pearson r |
  |---|---|
  | `modeled_points` vs `actual_points` (sanity baseline) | 0.630 |
  | Pre-origin rotation index vs forward `actual_points` | -0.026 |
  | Pre-origin rotation index vs `modeled_points` | -0.053 |
  | Pre-origin rotation index vs model **error** (`actual` − `modeled`), pooled | 0.014 (2022-23: 0.016, 2023-24: 0.021, 2024-25: -0.020) |
  | Same, non-nailed cohort only (pre-origin start share in [0.25, 0.75], n=8,008) | -0.017 |

  Every correlation is near-zero and inconsistent in sign across seasons
  — including in the non-nailed cohort where the over-projection
  hypothesis predicted the effect should concentrate. Rotation index
  does not meaningfully predict a player's own points either, so this
  isn't a case of a real signal the model already captures (the ICT
  pattern) — it simply doesn't relate to outcomes at this level of
  aggregation.

  **Out-of-sample MAE check** (fit GW10-20, held out GW21-30, two
  pre-registered variants): full population fit (n=15,855) found
  weight=0.0229; held out (n=17,080), baseline MAE 2.4614 → corrected
  MAE 2.4630, improvement **-0.0017** (worse). Non-nailed cohort fit
  (n=3,656) found weight=-0.2755; held out (n=4,352), baseline MAE
  3.9602 → corrected MAE 3.9528, improvement **+0.0074** — directionally
  right but below the 0.01 bar.

  Per-team rotation indexes ranked as expected: Chelsea, Man City, and
  Liverpool (heavy cup/European-competition rotation) rank in the top
  three or four in at least one of the three seasons.

  **Decision: declined.** Neither variant clears the 0.01 held-out MAE
  bar. Script left in `scripts/` as reusable infrastructure per the
  project's convention (alongside `investigate_ict_index.py`).
- **(d) Direct classification of a manager's actual tactical style —
  declined.** No free, structured, season-consistent source exists
  across all 20 PL managers for the three fit seasons (official PL
  trend articles are unstructured editorial prose about the current
  season only; Opta Analyst sits on the same ToS/robots.txt-restricted
  endpoint already declined for issue #13; Wikipedia manager biographies
  have real qualitative prose but no structured table). The only
  alternative — hand-curated subjective style tags with no cited source
  — would be the project's first non-source-backed input. Not pursued.

**Net result:** all four operationalizations of issue #11 are declined or
deferred with no viable near-term path; issue #11 closes as a clean
negative.

---

## Considered and declined — ridge-regression residual meta-learner (issue #65, 2026-08-08)

**Context:** issue #65 asked whether a small ML model trained on
signals the projection model ignores (ICT index, bps, price, ownership,
transfer activity) could improve on the existing hand-shrunk `residual`
component by predicting the live model's own error directly.

**What was tried:** ridge regression (`numpy` closed-form solve)
predicting `actual_points - modeled_points` (horizon=1) from pre-origin
per-game ICT/bps rates, price, log-ownership, and net transfer balance,
fit on 2022-23/2023-24/2024-25 and evaluated on the held-out 2025-26
season -- the same fit/held-out split `run_backtest.py` uses for the
live model.

**Result:** held-out MAE got worse (1.0740 -> 1.1052, champion vs.
champion+residual), including in-sample on the fit seasons themselves
(1.0584 -> 1.0975). Held-out RMSE modestly improved (2.0364 -> 2.0216)
in the same runs -- the signature of a model minimizing squared error
at the expense of the metric this project actually reports and gates
adoption on (MAE, per `_summarize()` and the model-change rule).

**Decision: declined as tested**, on the project's own MAE bar. Not
recorded as "no signal" -- the RMSE improvement suggests the features
aren't pure noise, but ridge regression under a squared-error objective
is the wrong tool for an MAE-gated decision. If revisited, start from
an MAE-minimizing objective (L1/quantile-loss regression) or an
outlier-robust non-linear model, not this exact formulation.

**Also from issue #65:** a companion ML minutes/start-probability ridge
model (candidate #1) *did* clear the project's backtest bar in every
held-out season and now runs in shadow mode (`src/fpl_intel/ml_minutes.py`,
`config/ml-minutes-weights.json`) -- computed and logged every refresh
under its own `model_version`, never feeding `project_players()` or any
recommendation. See `plans/issue-65-ml-shadow-model.md` for the full
backtest/full-pipeline evidence and the shadow design.

---

## Cross-cutting rules

- **Versioning:** every phase bumps `model.version`; frozen forecasts keep
  their originating version so `model_performance.py` comparisons stay
  honest across upgrades.
- **Validation:** no phase is adopted without beating the previous version
  on the Phase 0 backtest; Phase 3+ additionally requires held-out-season
  validation. Live frozen-forecast grading continues in parallel.
- **Dependencies:** stdlib-only through Phase 4 (deliberate); Phase 5 adds
  an LLM API call as an optional runtime dependency — no vendor SDK, and
  provider-agnostic (Claude by default, or any OpenAI Chat Completions-
  compatible host via env vars — see Phase 5's 2026-07-25 amendment). Issue
  #65 (2026-08-08) adds `numpy` as a first declared third-party Python
  dependency (`requirements.txt`), scoped to offline ridge-weight fitting
  for a shadow-mode ML minutes challenger — see that issue's entry below.
- **Docs:** README and SPECIFICATION.md updated as each phase lands;
  `model.limitations` in the dashboard always reflects the active phase's
  real limitations.
- **Out of scope (re-affirmed):** betting odds; an ML model *feeding live
  recommendations* (issue #65 runs one narrow ML challenger in shadow mode
  only, computed and logged but never wired into `project_players()` —
  see that issue's entry below); any automated coefficient adoption.

## Suggested order & rough total (superseded by actual results below)

Originally: Phase 0 → 1 → 2 → 3 → 4 (each gated on the previous), Phase 5
after live validation matures, ~10–16 sessions, with Phases 0–2 expected
to deliver most of the accuracy gain.

**What actually happened (2026-07-25, all in one continuous session):**
Phase 1 was deliberately skipped first, then attempted later and rejected
(built, backtested, didn't beat the FDR baseline it was meant to replace).
Phase 2 initially regressed and needed a follow-up fix (the residual term)
before it beat baseline. Phase 3 delivered the single biggest win of the
whole plan — range coverage 9% → 81%, the metric stuck at 8–17% through
every earlier version. A user-requested continuation closed most of the
MID/FWD bias gap Phase 3 left open. Phase 4 was also built, backtested,
and rejected (a real ~35% MAE regression, root-caused rather than assumed
away). Phase 5 was built as inactive scaffolding since its own gate is
unmet on both the "Phases 1–4 adopted" and "live calibration" conditions.
Net: **two of five phases shipped as designed (0, 3), one shipped after a
fix (2), two are validated-and-rejected rather than adopted (1, 4), one is
tested but deliberately dormant (5).** The rejections are not failures of
process — they are exactly what the model-change rule in
SPECIFICATION.md is for, and each one is backed by a specific,
reproducible backtest finding rather than a guess. Final adopted model:
v0.6, MAE 2.39 (fit) / 2.34 (held-out) vs. the v0.3 starting point's 2.68 —
an ~11% error reduction anchored by the calibration fix and the two bug
fixes (minutes-formula divisor, MID/FWD residual trust), not by the two
larger modeling efforts this session also tried.

## Considered and declined — local and in-process triggers for the deadline reminder (issue #55, 2026-08-08)

For the transfer-deadline email reminder, three trigger mechanisms were
considered and declined in favor of a scheduled GitHub Actions workflow
invoking a trigger-agnostic script (`scripts/send_deadline_reminder.py`,
`.github/workflows/deadline-reminder.yml`). **launchd and cron on the
user's machine**: ruled out by the explicit direction that the reminder
be cloud-native with no local machine in the loop (a local timer also
inherits the machine's sleep schedule — cron silently skips runs during
sleep; launchd merely fires them late on wake). **An in-process
scheduler thread in `server.py`**: only alive while the dashboard
service happens to be running — the inverse of the reliability a
reminder needs — and the only option that would put a timer inside the
app itself, breaching the externally-triggered-only architecture behind
SPECIFICATION.md's scheduling posture. **Dedicated cloud compute
(VM/Fly machine) just for the reminder**: premature while issue #27's
compute choice is deliberately open; GitHub Actions provides the
always-on property with zero new infrastructure, and the reminder
script migrates unchanged onto the #27 host's scheduler when that
lands. Full analysis in `plans/issue-55-deadline-email-reminder.md`.
