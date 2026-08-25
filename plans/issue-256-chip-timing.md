# Chip timing: season-stage awareness and forward-plan visibility (issue #256)

## Context

#256 has two related asks, both grounded in the same root cause: `_chip_recommendation`
(`src/fpl_intel/modeling/transfer_decisions.py:748-817`) only ever judges "should I play a chip
*this* gameweek," comparing that chip's marginal value against a fixed `_THRESHOLDS` constant
(`transfer_decisions.py:48-52`) with no notion of season stage or of what's coming later:

- **(A) Immediate-week false positives / undisclosed horizon mismatch.** A chip can clear its
  threshold one gameweek into a 38-gameweek season on exactly the same terms it would at GW30 --
  confirmed live on team 364759 at GW2 (Wildcard +15.9 over threshold, Free Hit +0.4 over its own
  threshold, balanced profile). Separately, Wildcard's `marginal_value` is a 5-GW cumulative delta
  while Free Hit/Bench Boost/Triple Captain's is a single-GW delta (`transfer_decisions.py:760-791`),
  and the `weekly-chip` UI panel lists all four side by side with no indication of that
  (`src/fpl_intel/js/dashboard/decision-center.js:809-817`).
- **(B) Chips are invisible to the 5-GW forward plan.** `build_multiweek_plan`'s beam search
  (`transfer_decisions.py:637-717`) already looks ahead 5 gameweeks and surfaces future transfer
  branches as `conditional_branches`, but its per-step candidate generation
  (`_planner_action_candidates`, `transfer_decisions.py:517-543`) only ever produces
  `roll`/`single_transfer`/`double_transfer` actions -- never a chip play. So a chip that's clearly
  the right call several gameweeks out (a double gameweek, a squad-quality cliff) is never
  surfaced now; the user only finds out once `_chip_recommendation` starts evaluating it as the
  *current* gameweek's decision.

## Structural constraints found before evaluating candidates

**This planner scope is a documented, deliberate contract, not an oversight -- and it already
names this exact gap.** `SPECIFICATION.md`'s "Weekly transfer and chip contract" section states:
"The weekly planner uses a receding five-gameweek horizon. It searches legal roll, single-transfer,
and double-transfer branches per gameweek step -- issue #181 widened the immediate-gameweek
comparison above beyond two transfers, but the planner's own per-step branching remains scoped to
roll/single/double for now, a deliberately separate follow-on decision, not yet made." Chips were
never part of that follow-on decision at all. Per #181's own precedent (its spec text had to be
amended before the immediate-gameweek search itself could widen past two transfers), building (B)
requires an explicit `SPECIFICATION.md` amendment first, not just an implementation -- the same
two-step (decide the contract, then decide the algorithm) #181 followed.

**Naively calling the existing exact chip optimizer inside the beam search is computationally
infeasible -- confirmed with real profiling, not assumed.** `_chip_recommendation`'s wildcard/
freehit evaluation calls `_optimize_squad` (`recommendations.py:786`, the full simulated-annealing
search already flagged elsewhere in this codebase as "the single hottest function in \[its]
profile," `recommendations.py:584`) twice per profile -- 6 calls total per `build_transfer_decisions`
run. Profiled directly (`scripts/benchmark_transfer_decisions.py`'s synthetic 573-player pool,
Python 3.11): those 6 calls account for roughly **1.3s of the run's ~11s total** (measured via
cProfile's proportional cumulative time; un-instrumented, real-scale). The ordinary-transfer beam
search that already exists, by contrast, expands up to 4 initial scenarios x 5 steps x 8 beam
width -- up to **160 node-visits per profile** (matching #181's own quoted arithmetic for this same
search). Naively calling `_optimize_squad` for both wildcard and freehit at every one of those node
visits would cost roughly `160 x 2 x ~0.22s ≈ 70s` *per profile*, `~210s` for all three -- about
**19x the entire current `build_transfer_decisions` runtime**, for one added capability. Not viable,
for the same reason #181 declined naive brute-force extension of the transfer search: an
already-expensive exact search cannot simply be run more often.

**Double/blank-gameweek signal is already baked into the existing per-player projection data --
confirmed reading `project_players`, not assumed.** `_fixture_by_team`
(`recommendations.py:208-221`) keys its schedule by `(team, event)` and *appends* every fixture
found for that event into a list, rather than assuming exactly one. Downstream, `project_players`
sums `component_points_for_event(...)` over every entry in that list for each relative event
(`recommendations.py:356-370`). This means a double gameweek (two fixtures in one event) already
produces a **visibly higher** `fixture_xp`/`profile_fixture_xp` entry at that relative index, and a
blank gameweek (zero fixtures) already produces a **zero** entry -- with no new fixture-congestion
detection needed. Both the ordinary planner (`_planner_player_score`,
`transfer_decisions.py:411-453`) and the chip evaluator already read these same arrays. This
significantly lowers the cost of a real chip-timing *signal*: the data needed to spot "this future
gameweek looks unusually good or bad for my squad" already exists, cheaply, without re-running any
optimizer.

**The user's own concrete example already fits inside the existing 5-GW horizon.** "GW10 now,
should see a chip need coming for GW14" is a 4-gameweek lead -- `relative_event = 4`, still inside
`horizon=5`'s existing `range(0, 5)`. Nothing about the ask requires extending the planning horizon
itself (a separate, larger product decision -- `SPECIFICATION.md`'s "Primary horizon: Rolling five
gameweeks," line 152, is its own deliberate scope). A same-horizon, cheaper signal (see candidate
B2 below) directly answers what was asked without touching that boundary.

## Candidate operationalizations

### Part A: immediate-week false positives / horizon disclosure

#### A1: Add a season-stage discount to the chip scoring itself -- BUILD, primary recommendation

**What:** Scale the effective bar a chip must clear by how early in the season it is -- e.g. a
factor applied to `marginal_value` (or equivalently to the threshold) derived from
`38 - event` (gameweeks remaining), shrinking toward 1.0 (no adjustment) as the season progresses
and pulling the bar up early on, reflecting that an early chip forecloses using it at a possibly
better spot later. Concretely: multiply `value_above_threshold` by a damping factor like
`min(1.0, (38 - _EARLY_SEASON_FLOOR) / (38 - event))`-shaped, or add a flat early-season penalty to
`marginal_value` that decays to zero by some cutoff (e.g. GW8-10) -- exact shape and constant is a
tuning call, same epistemic status as `_THRESHOLDS` itself (see below), not decided in this plan.

**Precedent for this being an acceptable class of change in this codebase:** #184 already
re-tuned `_THRESHOLDS`' wildcard/freehit constants as "a re-tuned heuristic, not a backtested
one," explicit that "there is no historical decision-level backtest harness in this codebase to
validate against." This is the same kind of reasoned-but-not-backtested heuristic, extended with
one more input (event number) rather than a new validated model. It must be labeled the same way
#184's constants are -- an inline comment stating the reasoning and its unvalidated status, not
presented as fitted.

**Verification approach before committing exact constants:** re-run #184's own downgrade-severity
sweep methodology (`scripts/benchmark_transfer_decisions.py`'s realistic pool, near-ideal through
deliberately weakened squads) at several `event` values (2, 10, 20, 30) per profile, confirming the
chosen shape (a) still lets a badly degraded squad clear the bar even early in the season (an early
Wildcard for a genuinely broken squad should still fire), and (b) meaningfully raises the bar for a
near-ideal squad's borderline chip case early on (this plan's own GW2 finding: Free Hit clearing by
only +0.4) without becoming unreachable.

#### A2: Disclose the 5-GW-vs-1-GW horizon mismatch in the UI -- BUILD, do regardless of A1

**What:** In the `weekly-chip` panel (`decision-center.js:809-817`) and its alternatives cards
(`decision-center.js:810-815`), label each chip's `marginal_value` with the horizon it's actually
measured over ("5-GW cumulative" for Wildcard, "this gameweek only" for Free Hit/Bench Boost/Triple
Captain) rather than listing all four numbers as if directly comparable. Cheap, no scoring change,
and independently worth doing even if A1 is deferred -- it's a factual disclosure fix, not a
judgment call.

**Worked example.** Today's real GW2 output for team 364759 (balanced profile) renders as:

```
Wildcard        33.9 marginal xPts   Threshold 18.0
Free Hit        15.4 marginal xPts   Threshold 15.0
Bench Boost     10.2 marginal xPts   Threshold 16.0
Triple Captain   7.0 marginal xPts   Threshold  8.0
```

Read top to bottom this looks like Wildcard is "roughly twice as good" as Free Hit. It isn't a fair
comparison: `33.9` is Wildcard's gain **added up across the next 5 gameweeks** (it permanently
rebuilds the squad, so the gain compounds every week), while `15.4` is Free Hit's gain **for this
one gameweek only** (it reverts after). Each candidate already carries its own `horizon` field (1
or 5, set at `transfer_decisions.py:756,759,773,789`) -- it is never rendered. After this change,
the same numbers render as:

```
Wildcard        33.9 marginal xPts  (cumulative, next 5 GWs)   Threshold 18.0
Free Hit        15.4 marginal xPts  (this gameweek only)       Threshold 15.0
Bench Boost     10.2 marginal xPts  (this gameweek only)       Threshold 16.0
Triple Captain   7.0 marginal xPts  (this gameweek only)       Threshold  8.0
```

No backend/scoring change -- `item.horizon` is already present on every candidate object returned
to the frontend; this only changes what `decision-center.js` prints next to it.

#### A3: Do nothing to scoring, disclosure-only -- DECLINE as the sole fix

**Verdict: decline as sufficient on its own.** A2 alone stops the UI from *implying* an
apples-to-apples comparison, but doesn't stop a chip from genuinely reading as "recommended" one
gameweek into the season, which is the user's actual complaint ("that makes no sense"). A2 is
necessary but not sufficient without A1.

### Part B: chip visibility inside the 5-GW forward plan

#### B1: Call the exact `_optimize_squad`-based chip evaluator at every future beam step -- DECLINE

**Verdict: decline.** Shown infeasible above (~19x current runtime for one added capability, same
shape of problem #181 already declined for naive multi-transfer brute force).

#### B2: Zero-extra-optimizer-cost fixture-shape scan over the existing horizon -- BUILD, primary recommendation

**What:** For each `relative_event` in the planner's existing `range(0, horizon)`, and for the
already-chosen best path's squad at that point (already available for free -- `_planner_step`
already records `projected_event_points` per path row, `transfer_decisions.py:556-564`), sum each
squad member's own `profile_fixture_xp[profile][relative_event]` across the *whole squad* (not
just the best XI) as a fixture-richness proxy for that gameweek, and compare it against the same
path's own other planned events. Flag a `relative_event` as a chip-timing signal when that sum
spikes well above the path's other weeks (double-gameweek-shaped: many owned players' fixture_xp
jump at once) or drops well below (blank-gameweek-shaped). This costs nothing beyond summing
numbers `project_players` already computed once, at data-prep time -- no incremental
`_optimize_squad` call, and no additional `_planner_player_score` calls beyond what the existing
beam search already makes.

**What it produces:** a heads-up only, not a firm "play chip X at GW14 with squad Y" -- e.g. a new
field on a future `conditional_branches` row, `chip_signal: "possible double gameweek -- reconsider
chip timing"`, surfaced the same way today's conditional-branch text already reads ("reconsider
\[transfer] before Gameweek Y"). The precise "should I actually play a chip, and with which squad"
verdict continues to be computed properly only once that gameweek is the *immediate* one --
exactly what `_chip_recommendation`/`_exclusive_chip_scenario` already do today, unchanged. This
is consistent with `SPECIFICATION.md`'s own disclosed limitation that "future market prices are
held constant" -- a precise far-future chip squad would be unreliable anyway; an early-warning flag
is the honest level of confidence to offer that far out.

**Directly answers the user's concrete example** (GW10 spotting a GW14 opportunity) without any
horizon extension, since `relative_event=4` is already inside `horizon=5`.

**Worked example.** Say it's GW10, and 3 of the manager's players are at a club whose rearranged
cup fixture lands them two matches in GW14 (a double gameweek). Summing
`profile_fixture_xp[profile][relative_event]` across the whole 15-player squad, for the path the
beam search already picked, at each of the 5 planned weeks:

```
GW10 (rel 0):  54.2
GW11 (rel 1):  51.8
GW12 (rel 2):  49.0
GW13 (rel 3):  52.5
GW14 (rel 4):  71.3   <- ~35% above the other 4 planned weeks
```

GW14 stands out against the path's own other weeks and gets attached to that week's row in
`conditional_branches` (the same list that already carries "reconsider \[transfer] before GW13"
text today) as something like: *"GW14 looks fixture-rich for your squad -- reconsider your chip
timing before then."* The same comparison in the other direction (several owned clubs going blank
in the same week) would flag a week to avoid a transfer/chip on, not exploit one.

Only arithmetic on numbers already sitting in memory from the existing beam search -- no new
`_optimize_squad` or `_planner_player_score` calls beyond what today's ordinary-transfer planning
already makes.

#### B3: A moderate-cost approximate re-optimizer, run only at weeks B2 flags -- worth layering on later, not required for a first version

**What:** At only the handful of `relative_event`s B2's cheap scan flags (not all 5, and not every
beam node), build a rough, non-legal-squad approximation of a chip's value using the planner's
existing cheap machinery (`_planner_single_moves`'s already-built top-8-per-position candidate
pools, `transfer_decisions.py:472-514`) rather than full simulated annealing -- closer in spirit to
how the ordinary transfer beam search itself already avoids `_candidate_moves`'s expensive,
unlimited exact search in favor of a cheaper top-8 approximation at each future step.

**Worked example, continuing GW14 from B2 above.** The squad the path has planned for GW14 projects
around 54 points that week (already computed by `_planner_event_points`). B3 additionally builds a
rough "best possible XI that week" from each position's existing top-8 shortlist -- ignoring budget
and ownership entirely, so it's an optimistic upper bound, not a real purchasable squad -- and that
sketch comes out around 74. B3 reports the gap: *"~20 points estimated upside if you free-hit around
GW14 -- rough estimate, refine closer to the week."* It sharpens B2's "something's up here" into "and
it looks like about this much," without claiming to know the exact squad.

**Verdict: real value, but not needed for a first version.** B2 alone already answers the request
(surface that a chip decision is coming); B3 would only sharpen "how big" that opportunity looks
before the gameweek is close enough for the exact evaluator to run. The real, exact squad and
marginal value still only ever get computed by today's `_chip_recommendation`/
`_exclusive_chip_scenario` once GW14 becomes the *immediate* gameweek -- B3 never replaces that,
it only previews roughly what those will likely say. Worth a follow-on if B2's flags turn out too
noisy (too many/few false positives) to be useful as-is -- not decided or built here.

#### B4: Extend the planning horizon beyond 5 gameweeks to catch chip windows further out -- DEFER, out of scope

**Verdict: defer.** `SPECIFICATION.md`'s "Primary horizon: Rolling five gameweeks" (line 152) is
its own explicit, deliberate product decision, separate from and larger than this issue's ask.
B2 already covers the concrete example given (a 4-gameweek lead, inside the existing horizon).
Only relevant if a chip opportunity more than 5 gameweeks out needs to be caught proactively --
not asked for here, and would need its own issue given how much further it reaches past today's
documented contract.

## Recommendation

Build **A1** (season-stage discount, exact shape/constants to be tuned via the sweep methodology
above once reasoning direction is confirmed) and **A2** (UI horizon disclosure, independent and
worth doing regardless). Build **B2** (zero-extra-cost fixture-shape scan surfaced as a new
`conditional_branches` signal field, scoped to the existing 5-GW horizon) as the primary answer to
the forward-visibility ask -- this requires a `SPECIFICATION.md` amendment first, following #181's
precedent, since today's spec text explicitly scopes the planner's per-step branching away from
chips. Decline **B1** (infeasible, shown with real profiled numbers). Defer **B3** (approximate
re-optimizer layered on B2's flags) as a real but non-blocking follow-on. Defer **B4** (horizon
extension) as a larger, separate product decision the user's own example doesn't actually require.

Two things to confirm with the user before implementation starts:

- **A1's exact discount shape and constants** -- this plan proposes the mechanism (a
  season-stage-dependent damping factor) but the precise curve is a judgment call in the same
  spirit as `_THRESHOLDS` itself, best pinned down with real sweep data once the direction is
  confirmed, not guessed here.
- **B2's spec amendment wording** -- since `SPECIFICATION.md`'s current text explicitly leaves
  "the planner's own per-step branching remains scoped to roll/single/double... a deliberately
  separate follow-on decision, not yet made," adding chip signals there is a product-contract
  change that should be worded and confirmed the way #181's amendment was, before code changes.

**Decided (2026-08-24): build A1, A2, and B2.** B1 stays declined, B3 and B4 stay deferred as
scoped above. Implementation proceeds on this same branch/issue (`issue-256-chip-timing`).

## A1 implementation: formula chosen and verified

**Formula.** `effective_threshold = threshold + abs(threshold) * extra`, where `extra` decays
linearly from `_EARLY_SEASON_MAX_EXTRA_MULTIPLIER` (1.0, i.e. doubling) at event 1 to `0.0` at
`_EARLY_SEASON_CUTOFF_EVENT` (10) and beyond. Adding a magnitude-proportional penalty (rather than
multiplying the signed threshold) was necessary, not stylistic: conservative's freehit threshold is
negative (-30.0), and multiplying a negative number by something greater than 1 makes it *more*
negative -- a *lower*, easier-to-clear bar, backwards from the intent. Scaling by the threshold's
own `abs()` and adding it back raises the bar correctly regardless of sign (implementation and full
reasoning: `transfer_decisions.py`'s `_season_stage_effective_threshold`).

**Verified (b): the real false-positive is suppressed.** Team 364759's real GW2 numbers now
compute (balanced profile): Wildcard effective threshold 34.0 vs marginal 33.9 (no longer clears,
was +15.9 over the raw 18.0); Free Hit effective threshold 28.3 vs marginal 15.4 (no longer clears,
was +0.4 over the raw 15.0). The exact same shape is already present in the committed unit-test
fixture (`tests/test_transfer_decisions.py`'s `gw2_inputs()`): aggressive Bench Boost cleared its
raw threshold by only +0.4 (14.4 vs 14.0) before this change -- now suppressed (effective threshold
26.4) -- used as this issue's committed regression test since it needs no threshold patching to
reproduce.

**Verified (a): a genuinely bad squad still clears, at realistic scale.** The unit-test fixture's
28-player pool is too small to demonstrate this directly (its optimizer has too little room to
improve a downgraded squad, capping Wildcard's marginal value around ~17 even at maximum downgrade
severity -- nowhere near the ~34-42 raised bars). Re-ran at `scripts/benchmark_transfer_decisions.py`'s
realistic 573-player scale instead, downgrading progressively more of a real squad's 15 picks to
the worst available same-position replacement, at GW2:

| Downgraded picks | conservative best | balanced best | aggressive best |
|---|---|---|---|
| 0 | 3xc, -12.7 | 3xc, -10.8 | 3xc, -9.0 |
| 9/15 | wildcard, -13.0 | freehit, -10.5 | wildcard, -8.1 |
| 12/15 | wildcard, -11.6 | **wildcard, +9.7** | **wildcard, +33.6** |

(Values are `marginal_value - effective_threshold` for whichever candidate is highest.) At 12/15
picks downgraded -- a genuinely broken squad -- Wildcard clears the raised GW2 bar comfortably for
balanced and aggressive. Conservative stays cautious even here, consistent with its existing
design (already the most reluctant profile to churn pre-#256). Confirms the adjustment raises the
bar without making it unreachable.
