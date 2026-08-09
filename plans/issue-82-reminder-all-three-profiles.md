# Include all three risk profiles in the deadline reminder email (issue #82)

Researched 2026-08-09. One clear, uncontested implementation for both
weekly-decisions states the reminder handles -- no plan-doc-worthy
ambiguity, per `plan-issue`'s own convention of not forcing a full plan
doc when the direction is uncontested. This is a short confirmation
note, not a design doc.

## What was verified

Read `scripts/send_deadline_reminder.py`'s `compose_email()`,
`_compose_active_section()`, and `_compose_gw1_section()` in full
(lines 164-286), plus `run()`'s call sites (lines 320-341), and the
producers of the two structures those functions consume:
`build_transfer_decisions`/`build_draft_decisions` in
`src/fpl_intel/transfer_decisions.py` (lines 716-975) and
`build_gw_recommendations` in `src/fpl_intel/recommendations.py`
(lines 899-1040+, with its `_build_profile_recommendation` helper at
512-546 and 821-896).

**`active` state (`_compose_active_section`, in-season transfer
decisions):** `weekly_decisions["profiles"]` is a list of exactly
three dicts (`build_transfer_decisions` lines 779-842, and the
identically-shaped `build_draft_decisions` lines 912-951 for the
preseason draft-feedback variant), one per `"conservative"`,
`"balanced"`, `"aggressive"`, each already carrying everything the
issue asks for: `id`, `label` (via `_PROFILE_DEFINITIONS` spread),
`recommendation.action`, `recommendation.captain`,
`recommendation.projected_event_points_including_captain`,
`recommendation.point_cost`, `recommendation.bank_after`,
`recommendation.free_transfers_next_event`. All three are computed
unconditionally inside `build_transfer_decisions`/`build_draft_decisions`
before that dict is ever returned -- `compose_email` currently just
throws two of the three away by picking `default_profile` and calling
`next(...)` to find only that one row (`_compose_active_section` lines
202-206). Confirmed today's `_compose_active_section` code only reads
`weekly.get("default_profile", "balanced")` and discards `profiles`
entirely aside from that one lookup.

**`waiting_for_gw2` state (`_compose_gw1_section`, pre-GW1 squad
selection):** the issue's own body speculated this state "currently
only surfaces `decision_center.recommended_squad`/`captaincy` with no
profile breakdown" -- checked directly, and that speculation does not
hold. `build_gw_recommendations` (called from `run()` at lines
334-337, unconditionally before `compose_email` runs whenever
`status == "waiting_for_gw2"`) already returns a
`"profile_recommendations"` key: a list of exactly three dicts, again
one per `conservative`/`balanced`/`aggressive`, built by the same
`_build_profile_recommendation` helper that produces the single
`recommended_squad`/`captaincy` fields `_compose_gw1_section` reads
today (`recommended_squad`/`captaincy` at lines 1017-1018 are just the
`"balanced"` entry's fields, copied out to the top level for backward
compatibility with older callers). Each of the three entries carries
`id`, `label`, `squad.captain`, `squad.vice_captain`, `squad.formation`,
`squad.projected_event_points_including_captain`, and `captaincy`
(top-5 captaincy options for that profile). So the per-profile data
is fully computed and available at the exact point `compose_email`
runs, symmetric with the `active` case -- this is not a state that
needs new computation added.

**One real (and expected) asymmetry, not a gap:** the GW1 state has no
natural equivalent of `action` or `point_cost`. Preseason squad
selection isn't a transfer decision -- there is no "roll / transfer /
double transfer" choice and no free-transfer economy yet, so there is
nothing to compare against and nothing to charge a hit for. That's a
difference in what the two states *mean*, not missing data: the GW1
section's compact per-profile block naturally shows label + captain +
formation + `projected_event_points_including_captain` instead of
action/point-cost, using the fields that already exist for that state.

## Concrete change (uncontested)

Purely a composition/formatting change inside
`scripts/send_deadline_reminder.py`; no new computation, no changes to
`transfer_decisions.py` or `recommendations.py`.

1. **`_compose_active_section(weekly)`**: replace the single
   `default_profile` lookup with a loop over all three entries in
   `weekly["profiles"]`. Emit one compact block per profile (e.g.
   `"{label}: {action} | Captain: {name} | Points: {points} | Cost: {point_cost}"`
   on one or two lines each), not three copies of the current
   full-verbosity section (which prints multi-line transfer lists,
   reasons, bank state, etc. per profile -- keep that fuller detail
   only for the default/balanced profile, or trim it uniformly across
   all three, to keep the email scannable per the issue's own ask).
2. **`_compose_gw1_section(decision_center)`**: replace the single
   `recommended_squad`/`captaincy` read with a loop over
   `decision_center["profile_recommendations"]`, emitting one compact
   block per profile (label, captain, formation, projected points),
   again keeping the fuller starting-XI/bench detail for one profile
   (or trimming uniformly) so the email doesn't balloon into three
   full squad listings.
3. Both functions keep their existing fallback branches (`profile is
   None`, `decision_center` missing/wrong status, etc.) unchanged --
   only the "happy path" body composition changes.

## Tests

`tests/test_send_deadline_reminder.py`'s `EmailCompositionTests` already
exercises both states end-to-end against real `build_transfer_decisions`
output (`test_active_transfer_decision_state_composes_the_recommendation`,
via `gw2_inputs()`) and real `build_gw_recommendations` output
(`test_waiting_for_gw2_state_composes_the_recommended_squad`, via
`sample_bootstrap()`/`sample_fixtures()`). No new fixtures needed --
extend those two tests to assert all three profile labels
(`"Conservative"`, `"Balanced"`, `"Aggressive"`) and each profile's
captain name appear in the composed body, instead of only checking for
one `"Recommended action"` / `"Starting XI"` occurrence.

## Recommendation

Build as described above -- no candidate directions to weigh, no open
design question for either state. Both `weekly_decisions["profiles"]`
(active) and `decision_center["profile_recommendations"]` (GW1) are
already fully computed, three-entries-per-state, before `compose_email`
is called; this is a straightforward extension of the existing
per-profile loop pattern already used elsewhere in the codebase (e.g.
`recommendations.py`'s own `profile_recommendations` construction),
not a research question. Hand off directly to `ship-issue` when ready
to implement.
