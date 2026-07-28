# PL manager playing-style investigation (issue #11)

## Context

Issue #11 asks whether a PL club's manager/playing style (high-press vs.
low-block, rotation policy, attacking vs. defensive setup) could improve
projections or squad recommendations. Per the issue, this is a
research/investigation task in the mold of the Phase 6 ICT Index
investigation and the npxG/xT/SCA/GCA "Considered and declined" entry in
IMPLEMENTATION_PLAN.md -- confirm a viable, freely available data source
first, and validate any candidate signal with an out-of-sample backtest
before any model change.

Three prior results tightly constrain what is worth testing:

- **Phase 1 (team-strength model) is a documented negative result.**
  A Dixon-Coles-style Poisson attack/defense/home-advantage model --
  which is precisely the statistical shadow of "attacking vs. defensive
  setup" -- was built, backtested, and NOT adopted: MAE 2.44 vs. the
  FDR-baseline 2.39, and a `min_rounds` search (6..26) only ever
  converged toward the baseline, never beat it. Any "manager style"
  proxy that reduces to team-level scoring rates is re-litigating this
  result and needs a reason to expect a different outcome.
- **Phase 4 (recency-weighted minutes model) is a second negative
  result.** Per-player decayed start-share reacted to exactly the
  short-term patterns a manager's rotation produces -- cup rotations, a
  single knock, one-off tactical choices -- and made projections ~35%
  worse because those patterns don't persist. Any rotation-flavored
  candidate must explain why it avoids Phase 4's failure mode
  (overreaction to short-window per-player noise).
- **The npxG/SCA/GCA decline sets the sourcing bar.** Signals that are
  only obtainable by scraping ToS-sensitive third-party sites were
  declined on provenance grounds, not on merit. "Manager style" has no
  column in any data the project already uses (`data/history/*/
  merged_gw.csv`, the official FPL API), so every candidate below is
  first judged on data availability.

Data note, checked and dismissed: the 2024-25 `merged_gw.csv` gains
`mng_*` columns (`mng_win`, `mng_clean_sheets`, ...). These are FPL's
Assistant Manager chip scoring fields for manager *elements*, exist in
only one of the three fit seasons, and describe results, not tactics --
they do not operationalize playing style.

The adoption standard is fixed by precedent: the Phase 6 script
(`scripts/investigate_ict_index.py`) runs a correlation screen against
the model's existing error, then an out-of-sample MAE check (fit
GW10-20, held out GW21-30, 3-GW horizon, all three fit seasons pooled),
with `MIN_REAL_IMPROVEMENT = 0.01` MAE as the bar. Anything at or below
the bar is a clean negative result, recorded and not pursued.

## Candidate operationalizations

### (a) Team-level attack/defense rates -- DECLINE (already tested)

- **What:** per-team expected goals for/against as a stand-in for
  attacking vs. defensive setup, possibly with home/away splits.
- **Data:** already on hand (match scores in `data/history/` and the
  fixtures feed).
- **Overlap with prior negatives:** near-total. This *is* Phase 1,
  which was built properly (decay, normalization, min-rounds gating,
  parameter search) and still lost to the FDR tables. Home/away splits
  are a minor variation of the same model class, with half the sample
  per parameter -- the sparse-data diagnosis from Phase 1 gets worse,
  not better.
- **Verdict:** not worth re-testing. The infrastructure is still in the
  codebase (disabled by `team_strength_min_rounds: 39`) if a genuinely
  different refinement (cross-season carry-over, FDR blending) is ever
  proposed; "manager style" adds no such refinement.

### (b) Manager-change events as a discontinuity signal -- DEFER

- **What:** flag the gameweeks following an in-season managerial change
  and test whether the model's error shifts systematically (the
  "new-manager bounce" hypothesis), or simply widen uncertainty ranges
  for affected teams.
- **Data:** no FPL field exists. Checked externally: Wikipedia's "List
  of Premier League managers" page has a structured table (club, From,
  Until dates, caretaker flags) covering all fit seasons through the
  present, freely licensed (CC BY-SA). Tenure windows for the three fit
  seasons amount to roughly 20-30 in-season changes -- small enough to
  hand-curate into a static CSV rather than scrape, which keeps this
  out of the FBref/Understat provenance category. So the data is
  *available*, at the cost of the project's first manually maintained
  non-FPL input.
- **Overlap with prior negatives:** low -- neither Phase 1 nor Phase 4
  tested event discontinuities.
- **Why defer anyway:** the effective sample is tiny. Intersecting
  ~20-30 change events with the GW10-30 origin windows and the 3-GW
  horizon leaves a handful of team-origin cells per season; a fitted
  correction on that sample cannot clear the 0.01 held-out MAE bar
  with any confidence, and the football-analytics literature mostly
  attributes the new-manager bounce to regression to the mean
  (managers are sacked at performance troughs). A hand-maintained
  dataset also creates a permanent freshness obligation for a signal
  that fires a few times a season.
- **Verdict:** defer. Recorded here as a revisit option if candidate
  (c) finds that team-context error exists but isn't explained by
  rotation -- a discontinuity check is then the natural next probe, and
  the hand-curated CSV is cheap to build at that point.

### (c) Team rotation propensity from lineup turnover -- TEST

- **What:** a per-team, season-to-date rotation index computed from
  starting-XI turnover between consecutive matches -- the directly
  observable footprint of a manager's rotation policy. Hypothesis: the
  model's minutes estimate is team-context-blind, so players at
  high-rotation clubs are systematically over-projected (their
  expected minutes assume more stability than their manager provides),
  and the effect should concentrate in non-nailed players.
- **Data:** fully on hand. `merged_gw.csv` for all three fit seasons
  carries `starts`, `minutes`, `team`, and `kickoff_time` per player
  per fixture (verified: `starts` present in 2022-23, 2023-24, and
  2024-25), and `backtest.py` already maps team names to ids. No new
  source, no new dependency.
- **Overlap with prior negatives:** distinct from Phase 1 (minutes
  allocation, not goal rates) and structurally different from Phase 4:
  Phase 4 failed because *per-player short-window* signals overreact to
  noise; a *team-level season-to-date* aggregate pools ~11 starters
  over every pre-origin match, making it a far more stable statistic
  by construction. It answers a question Phase 4 never asked -- not
  "has this player's role just changed" but "does this club's manager
  churn the XI more than others" -- and it is used here only as an
  error-correction covariate, not as a replacement minutes model.
- **Honest prior:** modest. The projection error is dominated by
  scoring variance, and the model's confidence/scenario machinery
  already penalizes individually volatile players
  (`is_rotation_risk`, news `rotation_risk` signals). The likely
  outcome is a Phase-6-style clean negative -- which the project
  values. But it is the only candidate that is simultaneously free,
  untested, and mechanistically plausible, so it is the one to test.
- **Verdict:** test, as the sole candidate. If it fails, the issue
  closes with a clean negative covering all three operationalizations:
  (a) already refuted, (b) not viably testable at this sample size,
  (c) tested and refuted.

### (d) Direct classification of a manager's actual tactical style -- DECLINE (2026-07-27)

- **What:** issue #11's original intent, clarified after (a)-(c) were
  already drafted -- not a statistical proxy for style, but the real
  thing: a categorical read of each PL manager's actual playing
  philosophy (high press vs. low block, possession vs. direct,
  back-three vs. back-four preference, etc.), sourced and joined per
  club per season.
- **Data search performed:** checked for a free, structured, season-
  consistent source covering all 20 PL managers across the three fit
  seasons.
  - **Official Premier League trend articles** (premierleague.com) --
    free, first-party, legitimate in principle, but published as
    editorial prose about league-wide trends for the current season
    (e.g. "4-2-3-1 used by 11 of 20 teams in 2024-25"), not a
    structured per-manager, per-season dataset with stable categories
    to join against.
  - **Opta Analyst / theanalyst.com** -- the same site investigated for
    issue #13. Publishes a "Playing Styles" article per season, but
    it sits on the identical undocumented endpoint and ToS/robots.txt
    restrictions already found there, and (per that investigation)
    only exposes current-season aggregates even where reachable.
  - **Wikipedia manager biographies** -- contain real qualitative
    prose ("Guardiola's tiki-taka," "Mourinho's park the bus"), but no
    structured table analogous to the tenure-dates page used for
    candidate (b). Turning this into a usable input means someone
    hand-labeling ~20-30 managers into style buckets from personal
    football knowledge, not extracting verifiable data from a source.
  - **Third-party tactical analytics** (Total Football Analysis,
    Wyscout-based writeups) -- subscription/ToS-restricted, the same
    category as the already-declined FBref/Understat sources in the
    npxG entry.
- **Verdict:** decline, on the same data-availability grounds as the
  npxG/xT/SCA/GCA entry and the issue #13 Opta investigation -- nothing
  freely available, structured, and verifiable exists. The alternative
  (hand-curated subjective style tags with no cited source) would be
  the project's first non-source-backed input, a real departure from
  every other coefficient in the model, which are all either fitted
  from history or hand-set from a documented rationale -- not pursued.

## Proposed investigation

One standalone, stdlib-only research script,
`scripts/investigate_team_rotation.py`, mirroring
`scripts/investigate_ict_index.py` exactly in structure, constants, and
rigor. Not part of the fit/validate pipeline; writes nothing to
`config/model-coefficients.json`.

Constants (identical to the ICT script unless noted):

- `FIT_SEASONS = ["2022-23", "2023-24", "2024-25"]`
- `FIRST_ORIGIN = 10`, `LAST_ORIGIN = 30`, `TRAIN_LAST_ORIGIN = 20`
- `HORIZON = 3`
- `MIN_REAL_IMPROVEMENT = 0.01`
- `MIN_PRE_ORIGIN_FIXTURE_PAIRS = 6` -- minimum consecutive-fixture
  pairs before trusting a team's rotation index (the analogue of the
  ICT script's 180-minute floor; FIRST_ORIGIN=10 guarantees at least 8
  pre-origin gameweeks, so this only trims postponement-heavy edge
  cases).

Method:

1. **Load per-team lineup sequences.** From each season's
   `merged_gw.csv`, group rows by team and fixture (a team can have two
   fixtures in a double gameweek, so key on `fixture`, not `GW`), order
   fixtures by `kickoff_time`, and record each fixture's starting XI as
   the set of elements with `starts == 1`. Also record, per player, the
   latest pre-origin team (handles January transfers) and the
   pre-origin start share (starts / team fixtures played), which the
   moderator slice in step 3 needs.
2. **Rotation index.** For a team at origin gameweek G: over all
   consecutive fixture pairs strictly before G, the mean number of
   starters changed, `mean(11 - |XI_t intersect XI_{t-1}|)`. Season-to-
   date, no decay -- deliberately the opposite of Phase 4's short
   decayed window, and no-lookahead by construction (same boundary as
   the rest of the harness). `None` below
   `MIN_PRE_ORIGIN_FIXTURE_PAIRS`.
3. **Correlation screen** (Pearson r, pooled and per season, using
   `season_comparisons()` unmodified for `modeled_points` /
   `actual_points` / `error`):
   - team rotation index vs. the player's signed model error -- the
     actionable question;
   - rotation index vs. `actual_points` and vs. `modeled_points` -- the
     redundancy check (is any signal already inside the model);
   - the same error correlation restricted to the mechanism's target
     cohort: players with pre-origin start share in [0.25, 0.75]
     (non-nailed, non-fringe), where minutes over-projection should
     concentrate if the hypothesis is right. A signal that appears
     only pooled but not in this cohort is suspect.
4. **Out-of-sample MAE check (the adoption bar).** Fit a single linear
   correction weight on the centered rotation index over origins
   GW10-20 (`adjusted_error = error - weight * (rotation_index -
   mean_rotation_index)`; global centering -- position centering is
   unnecessary since the covariate is team-level). Apply to held-out
   origins GW21-30 and report baseline vs. adjusted MAE and bias, and
   the same fit/evaluation repeated on the non-nailed cohort alone
   (two pre-registered variants, both reported regardless of outcome).

Estimated effort: one session. No changes to `src/`, no new data files,
no config writes.

Explicitly out of scope for this investigation: pressing/possession
style metrics (PPDA, field tilt -- only available from the scrape-averse
sources already declined in the npxG entry), candidate (a) in any form,
and building candidate (b)'s manager-change CSV.

## Exit criteria

Matching the project's documented standard (SPECIFICATION.md
model-change rule, as applied in Phase 6):

- **Adopt path:** a variant beats the held-out GW21-30 baseline MAE by
  more than 0.01. That result alone still does not change the model --
  it green-lights a follow-up model-change task that wires the
  correction (or a team-rotation-aware minutes adjustment) into
  `project_players` behind config, validated by the full 3-season
  backtest before adoption, with a model version bump.
- **Decline path:** improvement <= 0.01 on both variants, or a
  correlation screen showing the rotation index correlates with what
  the model already projects but not with its error (the ICT pattern).
  Record the result as a dated entry in IMPLEMENTATION_PLAN.md --
  covering candidates (a) re-litigation-declined, (b) deferred with the
  confirmed-available Wikipedia source noted, and (c)'s measured
  numbers -- close issue #11 as a clean negative, and leave the script
  in `scripts/` as reusable infrastructure.
- **Either way:** the investigation writes nothing to
  `config/model-coefficients.json` and changes no runtime behavior.

## Verification

- `python3 scripts/investigate_team_rotation.py` runs stdlib-only from
  a clean checkout and prints per-season sample counts, the correlation
  table, and the train/held-out MAE comparison with an explicit
  pass/fail line against the 0.01 bar.
- Sanity checks inside the run: the `modeled_points` vs.
  `actual_points` baseline correlation should land near Phase 6's
  0.433 (same comparison population); per-team rotation indexes should
  span a plausible 1-4 starters-changed-per-match range; teams with
  known heavy cup rotation should rank high in at least one season.
- `git diff main --stat` shows only this plan file (and later, only the
  new script) -- no `src/`, `config/`, or `data/` changes.
- `python3 -m pytest` remains green and untouched by the investigation.
