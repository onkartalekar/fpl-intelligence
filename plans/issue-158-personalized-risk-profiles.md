# Personalize the risk-profile comparison panel (issue #158)

## Context

Decision Center's "Compare risk profiles" panel (three cards + range strip + six-tile stat row,
`decision-section-profiles` in `dashboard.py`, rendered by `renderDecision()` in
[dashboard.js:85-92](../src/fpl_intel/dashboard.js:85)) is always built from a **freshly optimized**
squad per profile -- `recommendations.py`'s
[`_build_profile_recommendation`](../src/fpl_intel/recommendations.py:821) calls
`squad = _optimize_squad(...)` as its first line, independent of anything the visitor has declared.

The visitor's own draft/squad is evaluated separately by
[`build_draft_decisions`](../src/fpl_intel/transfer_decisions.py:869) (preseason) and
[`build_transfer_decisions`](../src/fpl_intel/transfer_decisions.py:716) (GW2+), but both only
produce a [`_scenario`](../src/fpl_intel/transfer_decisions.py:273) per profile -- a single
`net_gain_5gw` number and a lineup view, never the rich `metrics` dict (uncertainty bands, average
ownership/minutes, low-confidence count) the panel actually renders. Reported live: *"I'd like this
risk profile comparison for my draft in the decision center - it is available only for the
comparison only squad. doesn't help user."* Full trace in issue #158.

## Finding that changes the shape of this plan: the backend extraction is cheap, not a fork

The issue write-up flagged the metrics computation as "real backend work across two files." Having
now read the actual functions it calls, that's true but smaller than it sounded:
[`_event_lineup_schedule(squad, profile, horizon, ...)`](../src/fpl_intel/recommendations.py:590)
and [`_team_uncertainty_interval(squad, profile, horizon, schedule)`](../src/fpl_intel/recommendations.py:650)
are **already squad-agnostic** -- both take a plain `squad` list as a parameter and do not touch
`_optimize_squad` internally. `_build_profile_recommendation` only ever calls them on a squad it just
optimized, but nothing about their own code requires that.

So the metrics block inside `_build_profile_recommendation` (the `horizon_totals`/`evaluation_horizons`
loop over `(1, 3, 5)`, roughly [recommendations.py:832-856](../src/fpl_intel/recommendations.py:832),
down through the `metrics` dict it returns) can be extracted verbatim into a new function --
e.g. `_profile_metrics_for_squad(squad, profile, event=1)` -- that takes any fixed squad. Both
`_build_profile_recommendation` (passing its freshly optimized squad) and
`build_draft_decisions`/`build_transfer_decisions` (passing the visitor's own squad, already
available as the `squad` local in both) call the same function. No new schema, no new endpoint, no
new validation -- this part is a mechanical extraction, not a design fork.

**Performance check, since `_event_lineup_schedule`'s own docstring flags it as expensive in a
search loop:** that cost is specific to `_optimize_squad`'s simulated-annealing search calling it
tens of thousands of times. Here it would run 9 times per request (3 horizons x 3 profiles) against
one fixed squad -- negligible next to the `_candidate_moves`/`_best_double` transfer search that
`build_draft_decisions`/`build_transfer_decisions` already run synchronously today on the same live
`/api/manager-view` request path ([server.py:242](../src/fpl_intel/server.py:242) ->
`compute_manager_view`).

**Framing consequence, not a technical blocker:** the visitor's own squad is identical across all
three profiles -- only captaincy/rotation assumptions vary, never membership. The existing panel's
"changed players in/out" comparison language is meaningless here and needs different copy (see
Candidate B below).

With the backend side settled, what's actually still open is placement, framing, and scope --
product questions, not implementation ones.

## Candidate operationalizations

### (a) Placement: where does the personalized panel live?

**Candidate A1 -- replace the benchmark panel in place, once a squad exists.** Same
`decision-section-profiles` DOM, same `profile-options`/`profile-range-strips`/`profile-comparison`
IDs; `renderDecision()` swaps its data source from `decision.profile_recommendations` to
`weekly.profiles` (now carrying the same `metrics` shape) whenever `weekly.status==='active'`,
mirroring the `weekly-priority` demote/collapse logic already shipped for the surrounding sections in
PR #156. Cheapest change (no new DOM, no new render function), and consistent with the site's
existing rule: personalized data takes priority over the benchmark the moment it exists.

**Candidate A2 -- new panel alongside, under "Weekly decision."** Keep the benchmark panel exactly
as-is; add a second, visually identical panel (new DOM IDs, e.g. `weekly-profile-comparison`) inside
`decision-section-weekly`, next to the existing plain `weekly-profile-options` tabs. More visible
duplication of "here are 3 risk profiles" UI on one page, and a second render function
(`renderWeeklyProfileComparison()`) to build and keep in sync -- real ongoing maintenance cost for
two structurally identical widgets.

**Recommendation: A1.** It reuses the exact reorganization this session already built in PR #155/#156
(demote/replace the generic benchmark once something personal exists) instead of adding a second
copy of the same widget. The benchmark panel already collapses into "for comparison only" once a
draft/squad exists ([dashboard.js's `renderWeeklyDecision`](../src/fpl_intel/dashboard.js:110)) --
swapping its data source when personalized data is available, rather than leaving it live but wrong
and bolting on a duplicate elsewhere, is the smaller and more consistent change.

### (b) Framing: what replaces "changed players in/out"?

Since squad membership never varies for the visitor's own comparison, `comparison_to_balanced`'s
`shared_players`/`changed_players` fields (used today to build the panel's explanatory sentence)
would report 15/15 shared and an empty diff every time -- true, but says nothing.

**Recommendation:** replace that sentence for the personalized case with a captaincy/lineup delta
instead -- e.g. "Conservative captains {X}; Aggressive captains {Y} instead" when
`evaluation_horizons["1"].captain_id` differs between profiles, falling back to "Same captain and
lineup across all three profiles for Gameweek {N}" when they don't. This data is already present in
each profile's own `evaluation_horizons` (no new computation), just not currently surfaced this way.

### (c) Scope: draft-only, or also the real in-season case?

`build_transfer_decisions` (GW2+) has the identical gap -- same per-profile `_scenario` loop, same
missing rich `metrics`. Since the extracted `_profile_metrics_for_squad` helper is squad-agnostic by
construction, calling it from both `build_draft_decisions` and `build_transfer_decisions` is the same
amount of code either way -- there's no cheaper "draft-only" version of the backend change.

**Recommendation: ship both at once.** Scoping to draft-only would mean deliberately leaving the
exact same gap in place for every real in-season squad, for no implementation savings -- the
extraction is identical either way, only the call site differs by one line in each of the two
functions.

## Recommendation summary

Build `_profile_metrics_for_squad` once in `recommendations.py`, call it from both
`build_draft_decisions` and `build_transfer_decisions`'s existing per-profile loops, and have
`renderDecision()`/`renderWeeklyDecision()` swap `decision-section-profiles`'s data source to
`weekly.profiles` whenever `weekly.status==='active'` (Candidate A1) -- with the captaincy-delta
sentence (b) replacing the meaningless "changed players" copy whenever the data source is the
visitor's own squad rather than the benchmark's.

## Ready for `ship-issue`

No blocking open questions remain once the above is confirmed with the user -- the backend
extraction is mechanical, and A1/the captaincy-delta framing/both-call-sites scope are this plan's
concrete recommendations, not multi-way forks left for implementation time.
