# Week-over-week recommendation diff (issue #266)

## Context

Issue #266: every weekly refresh recomputes the Decision Center's recommendation and 5-GW plan
purely from live projections and the manager's current squad -- confirmed directly against the
code, nothing anywhere reads a prior refresh's own output back. `SPECIFICATION.md`'s planner
contract already discloses this as partly deliberate: "Later transfers are displayed as
provisional conditional branches and are rebuilt after every explicit refresh, never as
commitments." What the issue asks for is narrower than overturning that: not making a future
branch binding, but letting a manager see *how* this week's plan differs from what last week's
plan already anticipated for this same gameweek -- "the chip recommendation appearing now was
already flagged last week" vs. "this is a brand-new signal."

The issue's own 2026-08-29 amendment adds a second wrinkle: since it was filed, #267 and #278 gave
the model a real reason to change its mind about an *unchanged* squad -- a chip's
`effective_threshold` and the multi-transfer override's `required_margin` both decay purely with
the calendar, so a recommendation can flip without the manager's situation changing at all. It
also flagged a concrete asymmetry: chip fields (`threshold`/`effective_threshold`/
`value_above_threshold`) are already on the live API payload; the multi-transfer override's
equivalent (`required_margin`) is not attached anywhere.

This plan investigates how to close both the storage gap and the "where does a manager see it"
question, and finds a third gap along the way (below) that the issue's own request doesn't
mention but any real display of this would immediately expose.

## Structural constraints and findings, before evaluating candidates

**1. The comparison this issue wants is a cross-checkpoint lookup, not a same-key lookup -- this
shapes both the persistence and the read side.** `archive_team_forecast` keys each frozen
snapshot as `team_forecasts[team_id]["gw{event}:{lead_hours}"]`, where `event` is the gameweek
*being decided* at that checkpoint, not a future gameweek. There is no single frozen record
anywhere of "what did we predict about GW6" independent of *when* it was predicted -- the only
place a future gameweek's provisional action ever appears is inside an *earlier* checkpoint's own
`multiweek_plan.conditional_branches` list (e.g., GW5's checkpoint might carry a branch for
GW6/7/8/9). So "what did last week's plan already anticipate about this week" means: find the
most recent earlier checkpoint (`origin_event < current_event`) and search *its*
`conditional_branches` for the row whose `event` matches the current gameweek. This is materially
different from a simple "read my own last value" cache lookup, and confirms the issue's own ask
that `conditional_branches` (not just the headline `action`) must be part of what's frozen going
forward.

**2. `chip_signal` -- the exact mechanism the issue names as what a week-over-week comparison
would need "on both sides" -- is computed today but never rendered anywhere in the live UI.**
Confirmed: `_conditional_branches` (`transfer_decisions.py:839`) attaches `chip_signal` to every
branch, but `grep -rn "chip_signal" src/fpl_intel/js/` returns nothing. The "Conditional future
branches" panel (`decision-center.js:916-923`, `#weekly-branches`) renders `branch.event`,
`branch.action`, `branch.condition`, `branch.point_cost`, and the free-transfer counts -- never
`branch.chip_signal`. A manager today has no live way to see "GW14 looks double-gameweek-shaped"
even in the *current* week's plan, only in the raw API response. Surfacing a week-over-week diff
that says "this chip signal was already flagged last week" would be showing a manager, for the
first time, a signal that was silently dropped from the UI both weeks -- worth fixing together,
not a separate follow-up, since the diff feature's own credibility depends on the underlying
signal being visible at all.

**3. The same live-rendering gap exists for `effective_threshold`/`value_above_threshold` --
directly relevant to the amendment's "the bar came down, not your squad" narrative.** Confirmed:
`grep -rn "effective_threshold\|value_above_threshold" src/fpl_intel/js/` returns nothing. The
chip panel (`decision-center.js:961-968`, `#weekly-chip`) renders each alternative's raw static
`threshold` only -- never the scarcity-decayed `effective_threshold` actually being compared
against, nor `value_above_threshold`. So even today, before any week-over-week diff exists, a
manager cannot see *why* a chip newly cleared the bar (bar moved vs. marginal value moved) -- the
data already exists on the payload, it just isn't shown. The amendment's core "the bar came down"
story cannot be told at all -- this week or in a week-over-week comparison -- until this is fixed.

**4. The multi-transfer/chip asymmetry the amendment flagged is confirmed exactly as described.**
`_multi_transfer_required_margin(event)` (`transfer_decisions.py:183`) computes a real value used
in the accept/reject comparison at line 1229, but `required_margin` (line 1226) is a bare local
variable inside `build_transfer_decisions` -- never attached to `ordinary_recommendation`, any
scenario dict, or the returned profile -- so it's invisible to the frontend today and cannot be
persisted or diffed without exposing it first, exactly as the amendment anticipated.

**5. `compute_manager_view`/`build_transfer_decisions` have no access to `model-performance.json`
today, and the codebase already has an established pattern for exactly this situation --
extending it, not proposing a new architecture.** Confirmed: `compute_manager_view`'s only inputs
are the live bootstrap/fixtures/transfers payloads, `generated_at`, `team_id`, and profile
overrides -- it never reads the model-performance store. `model_performance.py` already solves
the identical problem twice: `build_team_model_performance`/`build_team_transfer_adherence`
(issues #64/#285) are both separate, store-reading, *request-time* functions, spliced into served
state after `compute_manager_view` already ran (`server.py:454-456` / `team_lookup.py`'s
`default_model_performance_action`) -- deliberately keeping `transfer_decisions.py` a pure
function of live data. A week-over-week diff function belongs in that same layer: a new
`build_team_plan_diff(store, team_id, weekly_decisions)` in `model_performance.py`, spliced in at
the same request-time point, needing zero changes to `transfer_decisions.py`,
`compute_manager_view`, or `build_transfer_decisions` themselves.

**6. Old archived checkpoints (before this ships) will lack the new fields -- must degrade to "no
prior data," never a fabricated comparison.** Same forward-only discipline issue #285 already
established for its own new `transfers` field on `archive_team_forecast`: a checkpoint archived
before this lands simply has no `chip_recommendation`/`conditional_branches` key, and the diff
function must treat that as "nothing to compare" (nothing shown), not backfill or guess.

## Candidate operationalizations

### (a) Prerequisite: attach `required_margin` to the multi-transfer recommendation -- BUILD, do first

**What:** when the multi-leg override wins (`transfer_decisions.py:1227-1231`), add
`"required_margin": required_margin` (and, for symmetry with the chip side's
`value_above_threshold`, `"margin_above_required": round(best_multi_leg["net_gain_5gw"] -
ordinary_recommendation["net_gain_5gw"] - required_margin, 1)` computed before the override
overwrites `ordinary_recommendation`) onto the resulting recommendation dict.

**Why first:** everything downstream -- persistence (b), the diff (c), and any live rendering --
needs this field to exist on the payload the same way the chip side's already does. Small,
mechanical, no design risk; doing it as its own first commit keeps (b)/(c) from having to route
around its absence mid-implementation, which the amendment already flagged as a risk.

### (b) Freeze `chip_recommendation` + `conditional_branches` (with `chip_signal`) + the threshold/margin fields into `archive_team_forecast` -- BUILD

**What:** extend `archive_team_forecast`'s per-profile frozen record (`model_performance.py`,
same function issue #285 already extended once for `transfers`) to also capture:
- `chip_recommendation`: `action`, `chip`, `marginal_value`, `threshold`, `effective_threshold`,
  `value_above_threshold` (all already on the live payload per finding 3 above -- no upstream
  change needed beyond (a) for the multi-transfer side).
- `multiweek_plan.conditional_branches`, trimmed to `event`/`action`/`chip_signal` per the issue's
  own request (dropping `condition`/`point_cost`/free-transfer counts, which are re-derivable
  narrative text, not needed for the diff itself, and would only grow the frozen payload for no
  comparison benefit).
- The multi-transfer recommendation's `required_margin`/`margin_above_required` from (a), when the
  headline action is a multi-transfer override.

**Why forward-only, land immediately:** identical reasoning to every other archive field in this
module (issues #102/#286/#285) -- a recommendation cannot be reconstructed after its deadline, so
every checkpoint archived before this lands is a permanent, disclosed gap (finding 6), not a
blocker to shipping.

### (c) Compute the diff at request time via a new store-reading function -- BUILD

**What:** `build_team_plan_diff(store, team_id, weekly_decisions)` in `model_performance.py`,
spliced in alongside `build_team_model_performance` (finding 5). Per profile, per the current
event:
1. Find the most recent `team_forecasts[team_id]` entry with `origin_event < current_event` (any
   checkpoint, not just T-24h -- a manager who last looked T-3h before last week's deadline should
   still get a comparison).
2. Search that entry's frozen `conditional_branches` for the row where `event == current_event`.
3. If found, compare: was last week's branch `action` the same shape as this week's actual
   `recommendation.action`/`chip_recommendation.action`? Was a `chip_signal` present for this
   event last week, and is a chip actually being recommended now (or vice versa -- flagged then
   silent now)?
4. No matching prior branch (never planned this far ahead, or the checkpoint gap from #288's
   cron-reliability issues) -> no comparison for that profile, not a fabricated "nothing changed."

**Output shape (sketch, not final):** a short list of plain-language notes per profile, e.g. `{
"type": "action_changed", "text": "Last week's plan expected you to roll here; this week
recommends a transfer instead." }` / `{ "type": "chip_signal_confirmed", "text": "This chip
signal for GW14 was already flagged last week." }` / `{ "type": "chip_signal_new", "text": "This
chip signal for GW14 is new since last week." }` -- exact wording and taxonomy is an
implementation detail for `ship-issue`, not this plan.

### (d) Where to surface it -- recommend a small note under the headline recommendation, but this is the one real product call

Two live-UI gaps (findings 2, 3) sit directly in the path of showing this well, and are worth
fixing as part of the same change rather than a separate follow-up, since the diff's own "already
flagged" language is meaningless if the underlying signal was never shown in the first place:
- Render `branch.chip_signal` in the existing `#weekly-branches` conditional-branches list
  (currently silently dropped).
- Render `effective_threshold`/`value_above_threshold` on the chip panel's alternative cards
  (`#weekly-chip`, currently only the static `threshold` is shown).

For the diff note itself, three placements were considered:
- **Recommended: a small "What changed since last week" note directly under `#weekly-recommendation`**
  (the headline card), one or two lines, only rendered when there's something to say. First thing
  a manager reads; both narratives from the issue (action changed, chip signal continuity) read
  naturally as a single short note here rather than being split across two locations.
- A dedicated new subsection (its own `<section>`, like `#weekly-plan`). More visible, but adds a
  fourth thing to scan on a page that issue #108 already gates hard on relevance; not recommended
  unless the note ends up carrying more than a sentence or two per profile.
  - Folding into the existing "Conditional future branches" list itself (annotating each resolved
  branch in place as "confirmed"/"new"). Attractive for chip-signal continuity specifically, but
  doesn't fit the "headline action changed" narrative at all, since a resolved branch for *this*
  week's event isn't itself in that list anymore (the list only shows *future* branches beyond the
  current event).

This is presented as a recommendation, not a decision -- unlike (a)-(c), which are load-bearing
engineering findings, this is a product/UX call the user should confirm or override before
`ship-issue` starts on it.

## Recommendation

Build all of (a)-(c) together as one forward-only change (a is a small, mechanical prerequisite
for c; they'd otherwise need to be sequenced or worked around). For (d), proceed with the
recommended placement (a short note under the headline recommendation, plus fixing the two silent
live-rendering gaps it depends on) unless you'd rather see it as its own section or folded into
the branches list -- happy to adjust before handing off to `ship-issue`.

Nothing here is a decline/defer candidate; there's no data-availability or architecture blocker
like issue #31 hit -- every piece needed already exists in the live computation, either on the
payload already or one small exposure away, and the request-time-splice pattern this needs is
already established and working for two prior features (#64, #285).
