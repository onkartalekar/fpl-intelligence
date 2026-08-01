# Transfer-driven squad strength investigation (issue #31)

## Context

Issue #31 asks whether summer transfer activity should adjust team
strength / fixture difficulty -- today neither mechanism reacts to it:

- `home_difficulty`/`away_difficulty` (`src/fpl_intel/catalog.py`) are
  FPL's own static `team_h_difficulty`/`team_a_difficulty`, set
  independently of squad changes.
- The fitted team-strength model (`src/fpl_intel/team_strength.py`) is
  fit purely from this season's own completed match results (goals
  scored/conceded). It has no transfer-data dependency, and is gated off
  entirely (`should_use_team_strength`) until `MIN_ROUNDS` of the current
  season have been played -- meaning the exact early-season window where
  a transfer signal would matter most is when the model falls back
  hardest to the static, transfer-blind FDR.

This was investigated per the same standard as every other model-input
candidate in this project: confirm data availability, then require an
out-of-sample backtest before treating it as a projection input
(`SPECIFICATION.md`: "Any model change must preserve the old model
version and be validated against frozen historical forecasts before
adoption").

## Two structural constraints found before evaluating candidates

**1. `team_strength.py` already declined the adjacent "preseason prior"
idea, for an architectural reason that applies here too.** Its own
2026-07-25 scope-decision comment:

> "the original plan called for seeding ratings preseason from the prior
> season's results... That requires carrying team ratings across a
> season boundary, which the current no-lookahead backtest architecture
> does not support cleanly (each season is loaded and evaluated
> independently -- see backtest.py)."

A transfer-driven strength adjustment is the same shape of problem: it's
inherently a preseason/summer-transfer-window signal that needs to carry
into the new season's early gameweeks, crossing the same season boundary
`backtest.py` doesn't support.

**2. `backtest.py` already documents that no historical transfer feed
exists.** Its own "Known simplifications" list (present before this
investigation, not discovered by it):

> "Recent-transfer role-transition scenarios are not replayed (no
> historical transfer-window feed available offline)."

Confirmed by checking `data/history/{season}/` directly: each season
folder holds only `merged_gw.csv` (results and per-player stats), no
transfer records. The live `data/official-transfers-latest.json` this
project does collect (see #29/#30) is a rolling "latest" snapshot that
gets overwritten on each refresh -- there is no persisted, dated archive
of any prior transfer window to fit or validate against.

Together: even if the season-boundary architecture problem in (1) were
solved, there is no historical transfer data in (2) to run the mandatory
out-of-sample check against. Both blockers must clear before this can
become a real model input.

## Data-quality check: is a squad-value-delta signal even computable today?

Tested directly against the live data this project already collects
(`data/official-transfers-latest.json` x `data/fpl-bootstrap-latest.json`
price lookup, matching confirmed transfer player names to bootstrap
`now_cost`):

- Several confirmed incoming signings do resolve cleanly (e.g. Newcastle's
  Aladji Bamba, Sean Steur both matched by name to a bootstrap entry with
  a price).
- At least one confirmed outgoing move does **not**: Anthony Gordon's
  transfer-out from Newcastle has no matching entry anywhere in the
  current bootstrap player list, under any name variant. FPL's own player
  database evidently lags the Premier League transfer centre's own
  confirmations, so a price-based value proxy has real, unpredictable
  coverage gaps precisely in the early transfer window when it would be
  most useful.

This doesn't block a display-only use of the signal (below), but it does
rule out treating it as a precise, complete number -- and would need
handling (partial coverage, no silent zero-fill) before feeding any
model formula.

## Candidate operationalizations

### (a) Squad-value-delta as a team-strength / projection input -- DECLINE, not worth revisiting

**What:** sum of incoming transfer players' bootstrap price minus
outgoing, per club, blended into or replacing the early-season
attack/defense prior before `should_use_team_strength`'s min-rounds gate
is satisfied.

**Verdict: decline, and not just for lack of data.** Blocked by both
structural constraints above simultaneously (the season-boundary
carry-over problem `team_strength.py` already declined once, and the
complete absence of historical transfer data `backtest.py` already
documents as missing) -- but there's a second, independent reason not to
chase this even if that data existed: **the mechanism it would enhance is
itself a documented negative result.** Per
`plans/issue-11-manager-style-investigation.md`, Phase 1 built and
backtested exactly this style of fitted team attack/defense model and it
*lost* to the static FDR baseline (MAE 2.44 vs. 2.39). Layering an
unvalidatable transfer signal on top of a mechanism that already
underperforms what it's meant to replace isn't worth the effort of
solving either blocker above. Unlike (c) below, this is not being kept
open as a "revisit once data exists" item.

### (b) Teammate minutes-impact from confirmed departures/arrivals -- BUILD, primary recommendation

**What:** the model already has a live-only mechanism for exactly this
category of signal -- `_recent_role_transitions()` /
`_minutes_scenarios()` in `recommendations.py` widen a *transferred
player's own* expected-minutes scenarios (conservative/balanced/
aggressive at 62%/78%/92% of the base estimate) and downgrade their
confidence to "low," gated on the transfer being confirmed
(`verification_status == "confirmed_first_party"`) and matched to a
current FPL element (`fpl_reconciliation_status ==
"matched_current_fpl"`). It says nothing about the players who stayed.
When a positional rival departs (transfer-out, released, or end-of-loan)
or a new signing arrives at a club, the remaining players who compete
for that position see their own real competition for minutes change --
the dashboard's own UI copy already gestures at exactly this
(`whyMatters()` in `dashboard.py`: "Departure may change minutes for the
remaining squad") without anything in the projection pipeline acting on
it. This candidate closes that gap: apply the same kind of scenario
widening to a departed/arrived player's same-club, same-position
teammates, not just to the transferred player themselves.

**Why this doesn't need a backtest, same as the existing mechanism it
extends:** `backtest.py` already excludes "recent-transfer
role-transition scenarios" from replay, by its own documentation, purely
because no historical transfer-window feed exists offline -- this is an
established, already-shipped exemption for this exact category of
live-only heuristic, not a new argument to make. Extending it to
teammates is the same kind of honest uncertainty-widening in response to
a real roster event, not a new predictive model claiming to beat a
baseline.

**Design note, not a blocker:** identifying "which teammates are
affected" needs the departing/arriving player's position and club. For
arrivals this is straightforward once `matched_fpl_element_id` resolves
(same gate the existing mechanism already uses). For departures it's
slightly less certain -- a player who has fully left the Premier League
may drop out of the bootstrap feed's `elements` list by the time a
reconciled match is available, taking their position with them. Where
that happens, the affected-teammates adjustment simply doesn't fire for
that specific departure (degrade gracefully, no silent guess at
position) -- consistent with how every other "insufficient data" case in
this project is handled, and acceptable per your note that it's fine to
only pick this up once FPL's own database has caught up.

**Verdict: build.** Extends an existing, already-precedented mechanism
rather than introducing a new one; no backtest obligation.

### (c) Squad-value-delta as an informational panel, not a projection input -- BUILD, secondary

**What:** a display-only "Squad changes this summer" note per club
(price-proxy squad-value delta from confirmed transfers, best-effort
name matching against the bootstrap feed), shown as context alongside
existing projections -- it does not change any player's `modeled_points`
or any fixture's difficulty number.

**Why this sidesteps the backtest requirement:** `SPECIFICATION.md`'s
out-of-sample rule binds changes to the projection *formula*
(`MODEL.md`'s documented model). A contextual, non-scoring note is the
same category as the existing `#fixture-congestion-limitation` panel
already in Decision Center -- informational, not a scoring factor, so it
carries no backtest obligation.

**Must be framed honestly:** given the confirmed Anthony Gordon gap
above, copy must say this is a directional, best-effort estimate (some
transfers may be missing from the total), not present it as a complete
or precise figure -- consistent with this project's "no fabricated
confidence" stance elsewhere (e.g. the manager-status "not yet
available" pattern, never a silent zero).

**Verdict: build.** No backtest blocker; the only work is the
transfer-to-price matching and a clearly-labeled panel.

### (d) Archive dated transfer-window snapshots -- OPTIONAL, low priority

**What:** persist a dated snapshot of `data/official-transfers-latest.json`
per season (e.g. written once when a transfer window closes) instead of
only ever holding the current rolling "latest" file.

**Why it's optional now:** this was originally the groundwork to unblock
candidate (a) in a future season, but (a) is declined outright above, not
deferred -- so that motivation is gone. Still cheap and mildly useful on
its own (e.g. a season-over-season view of candidate (c)'s squad-value
deltas), but not load-bearing for anything in this plan. Worth doing
opportunistically, not worth prioritizing.

**Verdict: optional.** No dependency from (b) or (c); skip unless it's
convenient to add alongside them.

## Recommendation

1. Ship (b): extend the existing `_recent_role_transitions()` /
   `_minutes_scenarios()` mechanism in `recommendations.py` so a
   departed/arrived player's same-club, same-position teammates get the
   same kind of minutes-scenario widening the transferred player already
   gets themselves -- this is the primary recommendation, since it plugs
   a real gap in an existing, already-shipped, already-backtest-exempt
   mechanism.
2. Ship (c): a secondary, lower-priority "squad changes this summer"
   informational panel, computed from data already collected
   (`official-transfers-latest.json` x `fpl-bootstrap-latest.json`),
   explicitly labeled as a directional estimate given the known
   name-matching gaps.
3. Record (a) as declined in `IMPLEMENTATION_PLAN.md`'s "Considered and
   declined" section, same house style as the npxG/xT/SCA/GCA entry --
   declined outright (weak even if the data existed), not deferred
   pending more data.
4. (d) is opportunistic, not scheduled.
5. Leave issue #31 open until (b) ships -- unlike #11/#13, which stayed
   open as pure declines with nothing to implement, #31 has a real,
   buildable near-term piece.
