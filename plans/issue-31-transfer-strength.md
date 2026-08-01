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

### (a) Squad-value-delta as a team-strength / projection input -- DECLINE

**What:** sum of incoming transfer players' bootstrap price minus
outgoing, per club, blended into or replacing the early-season
attack/defense prior before `should_use_team_strength`'s min-rounds gate
is satisfied.

**Verdict: decline, on the same grounds as npxG/Opta/manager style --
no viable path to the required out-of-sample backtest today.** Blocked
by both structural constraints above simultaneously: the season-boundary
carry-over problem `team_strength.py` already declined once, and the
complete absence of historical transfer data `backtest.py` already
documents as missing. Revisit only once (c) below has accumulated
several seasons of archived transfer data.

### (b) Squad-value-delta as an informational panel, not a projection input -- BUILD

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

### (c) Archive dated transfer-window snapshots -- BUILD

**What:** persist a dated snapshot of `data/official-transfers-latest.json`
per season (e.g. written once when a transfer window closes) instead of
only ever holding the current rolling "latest" file.

**Why:** this is the missing ingredient both blockers under (a) point to.
After 2-3 seasons of archived, point-in-time transfer data exist, a
genuine out-of-sample backtest of candidate (a) becomes possible, using
the same `FIT_SEASONS` pattern `fit_coefficients.py` and
`investigate_ict_index.py` already use.

**Verdict: build.** Pure data collection, no model or formula change, so
no backtest obligation either -- it's the groundwork that unblocks (a)
being revisited honestly in a future season, rather than staying
permanently declined for lack of data no one ever started collecting.

## Recommendation

1. Ship (b): a "squad changes this summer" informational panel, computed
   from data already collected (`official-transfers-latest.json` x
   `fpl-bootstrap-latest.json`), explicitly labeled as a directional
   estimate given the known name-matching gaps.
2. Ship (c): start archiving a dated transfer-window snapshot per season
   now, so this doesn't require reconstructing history that will
   otherwise already be gone by the time it's wanted.
3. Record (a) as declined in `IMPLEMENTATION_PLAN.md`'s "Considered and
   declined" section, same house style as the npxG/xT/SCA/GCA entry, with
   an explicit reopening condition: revisit once (c) has accumulated
   >=2-3 seasons of archived transfer data.
4. Leave issue #31 open until (b) and (c) ship -- unlike #11/#13, which
   stayed open as pure declines with nothing to implement, #31 has a
   real, buildable near-term piece.
