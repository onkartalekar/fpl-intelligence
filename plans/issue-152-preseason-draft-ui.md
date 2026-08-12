# Preseason draft UI overhaul (issue #152)

## Context

Issue #152 asks for four things: (1) a dedicated tab for the preseason draft squad instead of it
living inside "My Team"; (2) explicit on-tab messaging that it's a preseason-only tool that seeds
personalized recommendations; (3) a "draft health" section (risk profile, progression); (4) a
pitch-style UI whose formation adapts to the selected players, replacing the current flat
`.player-card` list.

Most of this was already resolved in the issue thread itself, before this plan doc, through direct
back-and-forth with the user:

- **Tab and purpose framing**: uncontested, no real ambiguity -- a new `view-draft` nav entry with
  explicit "preseason only, seeds your recommendations" copy.
- **XI/bench in the pitch view**: **Decided -- the user manually designates their own XI vs bench**,
  not an auto-inferred best XI.
- **Draft health**: **Decided -- reuse `build_draft_decisions`'s existing output directly (not a
  new always-on computation)**, since it already runs pre-GW1 today (`compute_manager_view` falls
  back to it whenever `build_transfer_decisions` reports `waiting_for_gw2`, i.e. `event <= 1` --
  [refresh.py:521-525](../src/fpl_intel/refresh.py:521)), and it already prices in injury/rotation
  risk per player via `_availability_multiplier`
  ([recommendations.py:29-36](../src/fpl_intel/recommendations.py:29)). **Decided -- placement is
  split**: a condensed health summary lives in the new Draft tab next to the pitch view (active only
  once the draft is a complete, legal 15, since `build_draft_decisions` hard-requires that via
  `validate_draft_squad`); the full three-profile scenario comparison stays exclusively in Decision
  Center, linked to rather than duplicated.

**What's genuinely still open**, surfaced only once implementation questions were traced through the
actual data model: how (or whether) the user's manual XI/bench choice is persisted.

## Structural constraint found before evaluating candidates

The `draft_squad` column (`profiles.py:38`, part of `_COLUMN_DEFS`) stores exactly one thing: a flat
JSON array of 15 element IDs, unordered, with no role metadata. Every existing consumer treats it
that way --`_draft_squad` ([transfer_decisions.py:125-142](../src/fpl_intel/transfer_decisions.py:125))
just enriches all 15 IDs uniformly, and `validate_draft_squad`
([transfer_decisions.py:144-183](../src/fpl_intel/transfer_decisions.py:144)) checks squad-level
legality (count, uniqueness, quotas, budget, club limits) with no concept of a starting XI at all.

There is **no precedent anywhere in this codebase for a user-declared XI or captain**. Even for a
manager's real, published squad, captaincy/lineup comes from FPL's own API (`manager_picks`) or from
the model's own optimizer (`build_transfer_decisions`'s `recommendation.squad.captain`) -- never
from a value a user chose and the app stored. So "let the user pick their own XI in the pitch view"
is new ground for the schema, not a small tweak to something already there.

One thing this investigation did settle cleanly: **the XI/bench split is purely presentational** and
does not need to feed `build_draft_decisions` at all. That function's own captaincy/recommendation
output is computed independently by the optimizer over the full 15-player squad and has never taken
a user-declared XI as an input. So draft health (already decided above) can ship against the full
15-player draft regardless of how the pitch view's XI/bench toggle ends up being implemented --
these two pieces of the issue are fully decoupled.

Also worth flagging for implementation, not a design fork: `pitch()`
([dashboard.js:88](../src/fpl_intel/dashboard.js:88)) is currently **read-only display** -- clicking
a pitch player today opens the scoring-breakdown inspector (`selectPlayerCard`,
[dashboard.js:47-48](../src/fpl_intel/dashboard.js:47)), not a bench/XI toggle. Reusing it visually
for the draft is straightforward (it already groups by position into rows and naturally reflows);
making it *editable* means wiring a new click behavior specific to this view, not just reusing the
function as-is.

## Candidate operationalizations

### Candidate 1 -- ephemeral, client-side only, no persistence

The pitch view computes a default XI/bench split each time the Draft tab loads (e.g. best-projected
XI by `xp_1`, or simple position-then-price ordering), and the user can freely toggle players
between XI and bench, and set a captain/vice, within that session. Nothing is saved. Zero backend
changes: no new schema column, no new endpoint, no new validation.

**Downside, stated plainly:** the split resets on every reload. For a feature explicitly framed as
"declare your draft" -- something a manager is expected to return to and refine over days -- an XI
choice that evaporates on refresh undercuts the point of making it interactive at all.

### Candidate 2 -- persisted XI/bench + captain, new schema + endpoint

Add a new nullable column (e.g. `draft_starting_xi`, following `_migrate_schema`'s established
add-a-column pattern exactly -- [profiles.py:64-75](../src/fpl_intel/profiles.py:64)) storing JSON
like `{"starting_ids": [...11 ids...], "captain_id": ..., "vice_captain_id": ...}`, plus new
validation: the 11 must be a subset of the declared 15, must satisfy legal FPL XI-formation rules
(1 GKP, 3-5 DEF, 2-5 MID, 1-3 FWD summing to 11 -- a genuinely different rule set from
`validate_draft_squad`'s 15-player squad-composition check, and not implemented anywhere today for
a manually-chosen, non-optimizer lineup), and captain/vice must be two distinct members of the 11.
Extend `/api/draft-squad` (or add a sibling endpoint) to accept and persist it.

This is real, non-trivial scope: one migration-safe column, one new validation function, one
extended/new endpoint plus its own request-shape checks (mirroring
`_validate_draft_squad_shape`, [server.py:440-475](../src/fpl_intel/server.py:440)), and tests
across `profiles.py`/`server.py`/`transfer_decisions.py`-adjacent test files for all of it.

### Candidate 3 -- persisted, folded into the existing `draft_squad` field's shape

Instead of a new column, change what `draft_squad` itself stores -- e.g. an ordered list where
position within the array (or an explicit small tag) marks starting-vs-bench and captain/vice.
Avoids a second column and a second round-trip on save, but still needs the same new
formation-legality validation Candidate 2 needs, *and* it changes the meaning of an already-shipped
field that three call sites (`_draft_squad`, `build_draft_decisions`, `validate_draft_squad`) all
currently treat as an unordered set -- every one of those needs to be checked for an assumption that
no longer holds. Candidate 2's separate column is strictly safer for the same amount of new
validation logic, since it doesn't touch anything already working.

## Recommendation

**Candidate 2, if persistence is wanted -- else Candidate 1.** This isn't a call I should make
unilaterally: it's a real scope fork (a few hours of UI-only work vs. a new column + new
formation-legality validation + new/extended endpoint + tests), not a quality difference where one
option is simply better.

Reasoning for leaning toward Candidate 2 if forced to pick: "declare your draft" already implies
persistence for the 15-player squad itself (issue #61 built exactly that), and a manager who spends
time arranging a specific XI and captain likely expects that to survive a reload the same way the
15-player selection already does -- an XI that resets on every visit would be a visibly weaker
feature than the squad-selection half sitting right next to it. But Candidate 1 is a legitimate,
much cheaper choice if the XI/captain concept is meant to stay a lightweight "what would this look
like" visualization rather than a second thing to declare and maintain.

Candidate 3 is not recommended under either scope size -- it carries Candidate 2's full validation
cost while additionally risking a regression in three already-working call sites, for no offsetting
benefit.

## Not decided yet

Persist vs. ephemeral (Candidate 1 vs. Candidate 2) for the manual XI/bench/captain selection --
waiting on direction before this can move to `ship-issue`.
