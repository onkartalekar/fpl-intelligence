# Chip recommendation threshold recalibration (issue #184)

## Context

Issue #181's fix made `_central_points` (`src/fpl_intel/recommendations.py:676`, inside
`_event_lineup_schedule`) read each player's profile-adjusted score (`_profile_event_score`,
the same number the search itself ranks candidates by) instead of the plain, risk-blind
`fixture_xp` it read before. That fix is correct and not being revisited here.

`_chip_recommendation` (`src/fpl_intel/transfer_decisions.py:733`) calls `_central_points`
for its wildcard/freehit marginal-value calculation and compares the result against fixed
constants in `_THRESHOLDS` (`transfer_decisions.py:33-37`). Those constants never moved when
`_central_points`'s scale did, so they are now being compared against a different scale than
the one they were set for.

## Structural constraints found before evaluating candidates

**There is no prior "correctly calibrated" baseline to restore.** `_THRESHOLDS` predates this
repo's own git history -- it arrived whole in the initial `50961c6 Add existing local project
files` import, with no derivation, comment, or `IMPLEMENTATION_PLAN.md` entry explaining how
the numbers were chosen. It was never backtest-validated even before #181. This changes the
shape of the problem: this issue isn't "restore a validated calibration," it's "this heuristic
was hand-picked once, and now needs to be hand-picked again for the new scale" -- unless a
different mechanism entirely is preferred (see candidate (d)).

**No infrastructure exists to backtest a chip *decision* against real history.**
`SPECIFICATION.md`'s model-change rule ("must preserve the old model version and be validated
against frozen historical forecasts") governs the *projection model* section of MODEL.md --
`fixture_xp`/`profile_fixture_xp` coefficients, scored by `backtest.py` via
`scripts/run_backtest.py`. `backtest.py` says so explicitly about its own scope: "Scoring is
per player, not per frozen-squad-and-captain like `model_performance.py`, since historical
manager squads do not exist. This measures projection accuracy, not squad-selection value."
There is no historical-squad replay, no counterfactual "chip played vs. not" simulator, and no
per-gameweek historical chip-availability feed. A literal backtest-validated threshold would
require building that from scratch -- a separate, much larger infrastructure project, not a
threshold tuning fix.

**bboost/3xc are structurally unaffected by the #181 fix -- confirmed, not assumed.** They use
`_lineup_view`'s `xp_1` field (`transfer_decisions.py:740,743`), which comes from
`_profile_player_score` (`recommendations.py:534`), a separate function `_central_points`
never calls and #181 never touched. `_profile_player_score` was already profile-adjusted
before and after #181 (`lower_1` for conservative, `upper_1` + bonus/penalty for aggressive,
`xp_1` for balanced) -- its scale hasn't moved. Confirmed empirically below: bboost/3xc
marginal values stay within a few points of their thresholds and track squad quality sensibly,
unlike wildcard/freehit. This closes the issue's own open question -- **only wildcard/freehit
need attention.**

## Findings (real data)

**On `tests/test_transfer_decisions.py`'s `gw2_inputs()` fixture** (unit-test scale, run via
`build_transfer_decisions`):

| Profile | chip | marginal_value | threshold | clears? |
|---|---|---|---|---|
| conservative | wildcard | 4.4 | 22.0 | no |
| conservative | freehit | -40.6 | 18.0 | no (badly) |
| conservative | bboost | 14.5 | 18.0 | no (close) |
| conservative | 3xc | 4.7 | 9.0 | no (close) |
| balanced | wildcard | -0.1 | 18.0 | no |
| balanced | freehit | 0.1 | 15.0 | no |
| balanced | bboost | 14.0 | 16.0 | no (close) |
| aggressive | wildcard | 10.1 | 14.0 | no (close) |
| aggressive | **freehit** | **49.4** | 12.0 | **yes, by +37.4** |
| aggressive | bboost | 14.4 | 14.0 | yes (by +0.4, close) |

bboost/3xc land close to their thresholds in both directions (a few points, sometimes over,
sometimes under) -- exactly what a working near-the-margin signal looks like. wildcard/freehit
for conservative and aggressive do not.

**At realistic scale** (573-player pool from `scripts/benchmark_transfer_decisions.py`,
comparing the tool's own already-recommended "near-ideal" squad against a deliberately
downgraded 6-player-weaker squad -- a genuine quality gap a manager would recognize):

| Profile | chip | near-ideal squad | weak squad (6 downgrades) | threshold |
|---|---|---|---|---|
| conservative | freehit | -39.8 | -36.5 | 18.0 |
| conservative | wildcard | 3.4 | 19.8 | 22.0 |
| balanced | freehit | -0.3 | 9.4 | 15.0 |
| balanced | wildcard | -1.2 | 11.4 | 18.0 |
| aggressive | freehit | **57.2** | **63.0** | 12.0 |
| aggressive | wildcard | 15.1 | 24.1 | 14.0 |

Balanced and (to a lesser extent) wildcard for the other two profiles move in the right
direction as squad quality drops. **`freehit` does not, for either conservative or
aggressive**: it clears aggressive's threshold regardless of squad quality (57.2 and 63.0, both
far above 12.0) and never clears conservative's regardless of squad quality (-39.8 and -36.5,
both far below 18.0). This is not merely "off-scale" -- for two of three profiles, freehit is
currently direction-blind. It would recommend (or refuse to recommend) the same way for a
squad that badly needs it and one that doesn't.

**A naive fixed-ratio rescale does not generalize -- checked directly, not assumed.**
Reproducing what the *old* (pre-#181) plain-`fixture_xp` central_points would have reported for
the exact same optimized squads (`_event_lineup_schedule`'s `lineup_player_ids`/`captain_id`
identify who was selected; only the value read for them changed):

| Profile | old-style marginal (wildcard) | old-style marginal (freehit) | new-style marginal (wildcard) | new-style marginal (freehit) |
|---|---|---|---|---|
| conservative | 0.86 | 0.06 | 4.4 | -40.6 |
| balanced | -0.11 | 0.08 | -0.1 | 0.1 |
| aggressive | -0.95 | -0.04 | 10.1 | 49.4 |

Under the old scale, wildcard/freehit essentially never had a real edge for this fixture
(everything near zero, nowhere close to the 12-22 thresholds) -- the old metric was mostly
measuring "does the optimizer's squad beat the current squad on raw points," and for a squad
`build_gw_recommendations` already built well, that gap is small almost by construction. The
new metric measures something different: "does the optimizer's *profile-optimized* squad beat
a squad that (for conservative/aggressive) was never built with that profile's own lens in the
first place" -- and that gap's size depends on how mismatched the current squad's construction
is from the candidate profile, not on a stable per-profile constant. A single derived ratio
(e.g. ~0.15x for conservative, ~1.95x for aggressive, both measured above) would not reproduce
old behavior in general, only for squads that happen to have the same mismatch level as this
one fixture. Confirms this is a genuine metric-meaning change, not just a scale factor to
divide back out.

## Candidate operationalizations

### (a) Derive a fixed rescaling ratio per profile and multiply the existing thresholds -- DECLINE

**What:** Compute `new_scale / old_scale` per profile from representative squads, multiply
`_THRESHOLDS`'s wildcard/freehit entries by it.

**Verdict: decline.** Shown directly above -- the ratio isn't a stable per-profile constant.
It depends on how mismatched the *current* squad is from the chip candidate's profile lens,
which varies per manager and per week. A ratio derived from any one benchmark would misfire
in exactly the cases that matter (a squad that already happens to fit its profile's lens well
would show a small ratio; one that doesn't would show a large one) -- there's no single number
that generalizes. Dressing this up as "derived from data" would overstate its reliability.

### (b) Build historical decision-replay infrastructure, then fit thresholds against frozen history -- DEFER, own project

**What:** True to `SPECIFICATION.md`'s model-change rule as literally written: reconstruct real
historical squads at each gameweek, simulate the counterfactual "chip played vs. held," score
outcomes against actual results.

**Verdict: worth doing eventually, out of scope here.** `backtest.py` doesn't have historical
squad reconstruction, chip-availability feeds, or multi-week counterfactual replay -- building
all three is a substantial, separate infrastructure project (comparable in scope to `backtest.py`
itself), not a threshold-tuning fix. If pursued, it deserves its own issue and plan, not to be
folded into #184 silently.

### (c) Re-hand-tune `_THRESHOLDS`' wildcard/freehit constants against the new scale -- BUILD, primary recommendation

**What:** Pick new constants for the affected entries, using the same kind of
representative-sample judgment call the original constants were almost certainly set by --
this time using real observed marginal values across a spread of squad quality (near-ideal
through deliberately weak, at both unit-test and realistic scale, the same technique used to
produce the findings above) so the new numbers at least restore correct *direction* (weak squad
clears more easily than a strong one) and land in a sane range (roughly "clearly worth it" vs.
"clearly not," judged by inspection of the new-scale numbers, the same way the old ones plainly
were).

**Correction found while implementing: only 4 entries actually need new values, not 6.**
`recommendations.py` builds `profile_fixture_xp["balanced"]` from the *exact same array* as
plain `fixture_xp` in every branch (role-transition, teammate-impact, and the default case) --
confirmed directly in the source, and matches the ratio table above showing balanced's
old-style and new-style `_central_points` values as identical (ratio 1.000) on every sample
checked. Balanced's scale never moved, so `balanced`'s `wildcard`/`freehit` thresholds (18.0,
15.0) are untouched. Only `conservative` and `aggressive`'s `wildcard`/`freehit` entries change.

**Values chosen (from a realistic-scale downgrade-severity sweep, 0-12 of 15 squad slots
swapped for the worst available same-position replacement):**

| Profile | Chip | Old | New | Why |
|---|---|---|---|---|
| conservative | wildcard | 22.0 | 22.0 (unchanged) | Already discriminates sanely post-#181: 3.4 for a near-ideal squad, only clears around 9-12/15 downgrades (30.0). |
| conservative | freehit | 18.0 | **-30.0** | Compressed scale runs deeply negative even for a bad squad (-39.8 near-ideal to -28.4 at 12 downgrades) and never approaches positive within any realistic degradation -- a positive threshold is unreachable by construction. -30.0 keeps freehit reachable only for near-catastrophic squads, consistent with a risk-averse profile rarely wanting to burn a one-week reshuffle. |
| aggressive | wildcard | 14.0 | **20.0** | Old threshold already cleared for a near-ideal squad (15.1 vs 14.0) -- fired unconditionally. 20.0 requires real degradation (~4-6/15 downgrades, 21.5-24.1) before it fires. |
| aggressive | freehit | 12.0 | **65.0** | Old threshold cleared for a near-ideal squad by a wide margin (57.2 vs 12.0) -- fired unconditionally, growing only modestly with real degradation (57.2 to 70.4 across the full sweep). 65.0 requires severe degradation (~9-12/15 downgrades) before it fires, keeping freehit a rare, high-conviction recommendation rather than a standing nag. |

Re-ran the full downgrade sweep with these values: no chip fires for a near-ideal squad on
either profile; wildcard/freehit only start firing at meaningfully degraded squads; wildcard
correctly outranks freehit once a squad is bad enough that a permanent rebuild beats a one-week
reshuffle. `bboost`/`3xc` and `balanced`'s `wildcard`/`freehit` are unchanged.

**Honestly labeled, not oversold.** This is not backtest-validated and won't claim to be --
it has exactly the same epistemic status the original `_THRESHOLDS` values already had (a
reasoned heuristic, not a fitted parameter). The difference from (a) is that it's tuned
directly against the new scale's own observed behavior across many squads, rather than
computed as a single conversion factor assumed to generalize.

**Cost:** small -- no new infrastructure, a handful of constant changes plus updated/added
tests asserting the corrected direction (weak squad clears more readily than a strong one,
where before neither did or a badly-fitting one always did).

### (d) Replace fixed absolute thresholds with a relative, self-normalizing measure -- interesting, not now

**What:** The issue's own open question -- express marginal value as a multiple of the squad's
already-computed uncertainty band width (`upper_points`/`lower_points`, `recommendations.py:677-678`)
or similar per-profile variance proxy, instead of a raw point difference, so a future change to
`profile_fixture_xp`'s scale can't silently break the thresholds again the way #181's fix did.

**Verdict: real merit, but a bigger redesign than this issue needs right now, and it doesn't
fully solve the "guessed constant" problem either -- it just relocates the guess to "how many
multiples of variance counts as worth it," which is still a judgment call, not a backtested
number. Worth tracking as a follow-on idea if threshold-scale drift recurs, not blocking a fix
for the currently direction-blind freehit behavior.**

## Recommendation

**Built.** (c): re-tuned `conservative.freehit` (18.0 -> -30.0), `aggressive.wildcard`
(14.0 -> 20.0), and `aggressive.freehit` (12.0 -> 65.0); left `conservative.wildcard` (22.0,
already sane), all of `balanced` (scale never moved), and `bboost`/`3xc` (separate, unaffected
code path) unchanged. Decline (a) as unreliable (shown not to generalize). Defer (b) as a
legitimate but much larger future project, not part of this issue. Note (d) as a worthwhile
structural idea for later, not built now.

This directly fixes the previously-broken behavior (freehit cleared or missed its threshold
regardless of squad quality for conservative and aggressive) without pretending to a rigor
level (backtest-validated) the codebase has no infrastructure to actually provide yet. Added
`test_chip_thresholds_are_not_scale_biased_after_the_central_points_fix` in
`tests/test_transfer_decisions.py` as a regression test, and tightened
`test_builds_roll_single_and_double_scenarios_for_three_profiles`'s recommendation-action
assertion back to its pre-#181 strict form now that this fixture's chip no longer spuriously
overrides it.
