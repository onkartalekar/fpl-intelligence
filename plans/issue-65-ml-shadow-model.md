# Issue #65 -- ML challengers to the projection model (issue #65)

Researched 2026-08-08. Issue body: explore ML-based challengers to the
current projection model, backtest them first against existing history,
and -- only for a candidate that clearly beats the champion -- run it in
shadow mode through the 2026-27 season for a possible 2027-28 promotion
decision.

## Context

MODEL.md states, deliberately: *"No machine learning, no foundation
model, no betting odds."* This issue is a considered, narrow departure
from that for two specific components, not a proposal to rewrite the
model. The current model is more precisely a **hand-designed additive
structure with statistically fitted coefficients** -- a human chose the
seven components and the shrinkage curve's shape; `fit_coefficients.py`
fits the numbers inside that shape from three seasons of results. What
ML adds that this can't: learned feature interactions and weights,
instead of one human-chosen combination rule.

Two prior ML-adjacent attempts are directly relevant precedent, each
with a **specific**, diagnosed cause of failure (not just "didn't work"):

- **Phase 1 -- fitted team-strength** (`team_strength.py`): lost to the
  static FDR tables at every tested threshold. Fit purely from goals
  scored/conceded, no richer per-fixture signal.
- **Phase 4 -- recency-weighted minutes** (`minutes.py`): MAE ~35%
  *worse*. Root cause per its own postmortem
  (IMPLEMENTATION_PLAN.md): one fixed, hand-picked decay curve
  overreacting to short-term noise (cup rotations, a single knock) that
  doesn't hold up over 5 gameweeks -- a structural defect in *how one
  speed was chosen*, not a mistuned constant (MAE got worse, not better,
  as the half-life was searched in every direction).

## Data and methodology

`data/history/` holds **four complete seasons**
(2022-23, 2023-24, 2024-25, 2025-26 -- confirmed complete: 2025-26 has
380 fixtures and 29,758 player-gameweek rows, manifest dated
2026-07-25). ~118k player-gameweek rows total. Since 2025-26 is now
finished and 2026-27 hasn't started, prototyping did not need to wait
for any live data -- both candidates below were backtested immediately
against this existing corpus, reusing `backtest.py`'s no-lookahead
harness (`build_origin_inputs`, `season_comparisons`) so the same
strict pre-origin-only guarantee the live model is validated under
applies to these prototypes too.

**Library choice for the prototypes:** `numpy` is already present on
the development machine (2.0.2) though not a declared project
dependency; `scikit-learn` is not installed. Rather than add a new
dependency before any candidate had proven itself, both prototypes are
plain ridge regression via `numpy`'s closed-form solve
(`(XᵀX + λI)⁻¹Xᵀy`) -- real learned per-feature weights (not a hand-picked
formula), cheap enough that dependency choice doesn't gate the go/no-go
decision. See "Recommendation" for what this means going forward.

**Validation split:** candidate #1 uses full leave-one-season-out
cross-validation (train on 3 seasons, test on the 4th, rotated across
all 4) for a stronger, season-robustness read. Candidate #2 mirrors
`run_backtest.py`'s own fit/held-out split exactly (fit on 2022-23/
2023-24/2024-25, held out on 2025-26) so its number is directly
comparable to the live model's own reported backtest MAE.

Both prototype scripts are committed alongside this plan
(`scripts/experiment_minutes_ml_prototype.py`,
`scripts/experiment_residual_ml_prototype.py`) as investigation
artifacts, reproducible with `PYTHONPATH=src python3 scripts/experiment_*.py`.
Neither is wired into the live pipeline.

## Candidate operationalizations / Findings

### (1) ML minutes/start-probability model -- BUILD

**What:** ridge regression predicting a player's next-gameweek minutes
from strictly pre-origin features: season-long start share, season-long
minutes/game, a 3-game recency window (start rate, average minutes),
the gap between the recency window and the season-long share (the same
signal Phase 4's fixed decay tried to capture), and a sample-size/
maturity term. Directly targets Phase 4's diagnosed defect: a learned
weight on the recency signal instead of one hand-picked decay speed.

**Result -- a real, consistent win, not a single-season fluke:**

| Held-out season | n | baseline (live) MAE | learned MAE | delta |
|---|---|---|---|---|
| 2022-23 | 22,653 | 21.154 | 14.752 | -6.402 |
| 2023-24 | 26,170 | 17.903 | 14.146 | -3.757 |
| 2024-25 | 24,576 | 17.971 | 14.856 | -3.115 |
| 2025-26 | 26,824 | 17.045 | 13.779 | -3.267 |
| **Pooled** | **100,223** | **18.425** | **14.359** | **-4.066** |

The learned model beats the live `_expected_minutes` baseline in
**every** held-out season individually -- not an average masking a
reversal somewhere. The dominant learned weight (+76.7, standardized
against a 0-90 scale) is on 3-game average minutes when started -- i.e.
"what did this player just do" carries far more signal than season-long
totals once a model is allowed to weigh it properly, which is the exact
mechanism Phase 4 reached for and got the execution of wrong.

**Known caveats, not yet resolved:**
- ~1.4% of (element, gameweek) rows in the historical dataset are
  duplicated (double-gameweek fixtures recorded as two rows under one
  `GW` label); the prototype's target extraction takes the last row
  seen rather than summing both. Adds noise symmetrically to both
  models being compared, not a directional bug, but should be fixed
  before this becomes real shadow code.
- `fixtures_played` for the baseline formula is approximated as
  `origin_gw - 1` rather than the live pipeline's actual per-team
  completed-fixture count (matches an existing simplification
  `backtest.py` already documents elsewhere).
- Real-time signals this prototype can't test at all -- breaking news,
  press-conference availability, brand-new signings with zero rows --
  are exactly why this candidate still needs a live shadow season
  before promotion, not just a strong backtest. A season-holdout replay
  is still retrospective; it can't stress-test the live data feed's
  timing/completeness the way an actual season does.

### (2) Residual meta-learner -- DECLINE AS TESTED

**What:** ridge regression predicting the live model's own error
(`actual_points - modeled_points`, horizon=1) from signals the current
model never sees: pre-origin ICT-index and bps per game, price, log
ownership (`selected`), and net transfer activity (`transfers_balance`)
-- all confirmed present in `merged_gw.csv` (unlike ownership/price,
which are not available for the minutes prototype's needs but are for
this one). Evaluated as: does `modeled + predicted_residual` beat
`modeled` alone on the held-out season.

**Result -- fails on the project's own adopted metric:**

| | Champion-only | Champion + learned residual | Delta |
|---|---|---|---|
| Held-out 2025-26 MAE | 1.0740 | 1.1052 | +0.0312 (worse) |
| Held-out 2025-26 RMSE | 2.0364 | 2.0216 | -0.0148 (better) |
| In-sample (fit seasons) MAE | 1.0584 | 1.0975 | +0.0391 (worse) |

**Why, specifically -- not just "no signal found":** RMSE improves
(both held-out and in-sample) while MAE gets worse, including
*in-sample* on the very data the model was fit on. That combination is
the signature of a real, diagnosable mismatch: ridge regression
minimizes squared error, which rewards catching a few large outlier
hauls at the expense of very slightly worsening the bulk of ordinary
predictions -- exactly the trade RMSE rewards and MAE penalizes. This
project's model-change rule and every reported metric (`_summarize()`,
`run_backtest.py`) are phrased in MAE, so this candidate, as tested,
does not clear the bar. This is a specific, addressable mismatch
(wrong loss function for the metric that matters here), not evidence
the underlying features carry no signal -- worth stating precisely so a
future revisit under an MAE-minimizing objective (L1/quantile-loss
regression, or a small tree ensemble which is less outlier-sensitive
than ridge) isn't starting from "we tried ML residual correction and it
didn't work."

### (3) ML opponent-strength model -- DEFER, not prototyped

**Feature audit (the pre-check this issue asked for before spending
prototype effort):** Phase 1's fitted team-strength model was trained
on actual goals scored/conceded only. `merged_gw.csv` also carries
per-player `expected_goals` and `expected_goals_conceded` -- the same
fields `projection.py`'s live `attacking`/`goals_conceded` components
already use -- which *can* be aggregated to team level for a
lower-variance signal than raw goals. That means this candidate is
**not** guaranteed to repeat Phase 1's exact failure the way, say,
resubmitting the Opta/ICT features already declined elsewhere would be
-- genuinely different information is available.

**Why not prototyped now:** unlike candidates #1/#2, this isn't a
standalone script against existing output -- it requires re-deriving
`team_strength.py`'s Dixon-Coles-style iterative proportional scaling
with xG-weighted inputs instead of goals, and re-wiring it through
`project_players()`'s fixture-difficulty path to backtest fairly. That
is a materially larger scope than this issue budgeted for. Recommend a
**separate follow-up issue** scoped specifically to "team-strength
model fit from aggregate team xG/xGC instead of goals" rather than
folding a half-built version into this one.

## Recommendation

1. **Build candidate #1's shadow-mode pipeline.** Evidence is strong
   and consistent across all four held-out seasons. Concretely:
   - Extend `model_performance.py`'s `build_performance_report` (and
     the `player_forecasts` freeze in `archive_forecast`, currently
     gated on `is_champion`) to track and score every non-champion
     `model_version`, not just the champion -- additive to the existing
     `champion_forecasts` design, not a rewrite.
   - Fix the duplicate-gameweek-row caveat above before this touches
     real code (sum minutes across same-labeled rows, don't overwrite).
   - Wire the minutes classifier into the refresh pipeline as a
     challenger `model_version` that computes and logs every refresh
     but never feeds `project_players()` or any dashboard-visible
     recommendation -- same "disabled, not deleted" discipline already
     used for `team_strength.py`/`minutes.py`.
   - Formalize `numpy` as a declared project dependency (already
     present on the dev machine, small, no compiled-model-format
     baggage); `scikit-learn`/`xgboost`/`lightgbm` are not justified by
     this candidate's win margin or this project's data volume.
   - Run through the 2026-27 season; revisit for promotion at season
     end against the model's own existing rule (beat the previous
     version's backtest) plus the live shadow read, given this
     candidate's specific sensitivity to real-time signals a backtest
     can't fully stress-test.
2. **Decline candidate #2 as tested.** Drop-in `IMPLEMENTATION_PLAN.md`
   text below. Not shadow-worthy in its current form; a future revisit
   under a different loss function is a well-scoped, separate
   consideration, not blocked by anything found here.
3. **Defer candidate #3.** Feature audit clears it for a future
   prototype (genuinely richer inputs exist than Phase 1 used), but
   scope it as its own issue rather than this one.
4. **Open items before #1 ships** (for `/ship-issue` to resolve, not
   decided here): exact refresh-pipeline hook point; whether shadow
   performance surfaces in the dashboard's existing "Model basis and
   risks" panel this season or stays a developer-only report; the
   auditability path if #1 is ever promoted -- SPECIFICATION.md
   requires a per-event `component_xp` breakdown on every live
   projection, so a promoted ML minutes model still needs to produce a
   labeled contribution the same way `team_strength.py`'s replacement
   candidacy would.

## If declined: text for IMPLEMENTATION_PLAN.md

To be added alongside the existing Phase 1/Phase 4/Phase 6
"considered and declined"-style entries:

---

## Considered and declined -- ridge-regression residual meta-learner (issue #65, 2026-08-08)

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

---
