# Chip scarcity / opportunity-cost model (issue #267)

## Context

#267 was scoped out of #265's investigation: chip recommendations only ever ask "is a chip better
than the best transfer-only plan *this* gameweek," never "is this the best of the ~36 remaining
gameweeks to spend one of exactly two Wildcards." #267's own body sketched three unconfirmed
candidate directions. This plan investigates each with real data before committing to one.

## Structural constraints found before evaluating candidates

**No known double or blank gameweeks exist anywhere in the currently-published fixture
calendar.** Checked directly: `fetch_fixtures()` already returns all 380 fixtures for all 38
gameweeks (every fixture has a real, non-null `event`), but counting fixtures per team per event
across the whole season finds **zero** teams with more than 1 or fewer than 1 fixture in any
gameweek, right now. This matches how the Premier League calendar actually works -- cup-driven
reschedulings that create real DGWs/BGWs are announced progressively through the season, not
known this far out. **This rules out building candidate (1) from the *current* season's own
not-yet-revealed calendar** -- there's nothing there yet to detect. It does not rule out modeling
double/blank gameweeks at all: per explicit direction, (1) is revisited below as (1b), built from
real historical data instead of the live (and currently empty) calendar. Candidate (3) (making
#256's B2 heads-up decision-changing) is still blocked on the *current* season's calendar
specifically, since B2 only ever looks 5 gameweeks ahead at real, not historical, fixtures --
deferred, see its own section below.

**#256's shipped season-stage adjustment has a real, previously-unnoticed gap of its own, found
while investigating this issue.** `_season_stage_effective_threshold` (`transfer_decisions.py`)
raises the bar based purely on `event` against a single flat `_EARLY_SEASON_CUTOFF_EVENT = 10`,
with no reference to each chip's own `start_event`/`stop_event` (already computed per chip by
`_chip_inventory`, just never consulted by the threshold adjustment). Real chip windows (from live
`bootstrap["chips"]`): Wildcard/Free Hit run GW2-19 then GW20-38; Bench Boost/Triple Captain run
GW1-19 then GW20-38 -- **two independent per-season-half windows**, not one continuous season.
Since `_EARLY_SEASON_CUTOFF_EVENT` (10) sits in the *middle* of the first window, #256's discount
already goes to zero from GW10 onward even though 9 more gameweeks of Wildcard-1's own runway
remain -- and it never reapplies *any* caution when the second-half window opens at GW20, despite
that being structurally identical to a fresh season start for that chip.

## Is the "chip potential vs. transfer potential" comparison itself right?

Checked directly, per explicit direction, rather than assumed: **yes, and real data shows the
existing comparison already favors the chip even more than the numbers previously quoted suggest**
-- so this isn't the axis to change. On the same real GW2 pull (team 364759, aggressive profile):

| Baseline | Net gain over the *original* squad |
|---|---|
| 1 free transfer (no hit) -- a realistic, sustainable weekly action | +65.6 |
| Wildcard (full rebuild, zero hit cost) | **+280.9** |

`_chip_recommendation`'s `marginal_value` (53.7) is *not* measured against this +65.6 realistic
baseline -- it's measured incrementally against whatever the *best* ordinary scenario turned out
to be that week (here, an aggressive 5-transfer/16-point-hit plan netting +227.2), crediting back
that scenario's own hit cost. Re-based against the more realistic 1-transfer action instead, the
Wildcard's real edge is +215.3, not +53.7 -- *bigger*, not smaller. Comparing against a more
"realistic" transfer baseline does not make the case for holding the chip any stronger; if
anything it undercuts it further. **This confirms the raw "is chip potential bigger than transfer
potential" comparison will indeed be true most weeks**, exactly as anticipated -- an unconstrained
full rebuild is close to mathematically guaranteed to beat any constrained transfer plan whenever a
squad has room to improve across multiple positions, which is common. The real, useful judgment
call is not in this comparison -- it's entirely in the *wisdom* layer: given the potential is
almost always bigger, is *now* actually the right week to spend a resource you only get twice a
season? That's exactly (1b) + (2) below, not a change to how potential itself is measured.

## Candidate operationalizations

### (1) Season-long relative bar from currently-known fixture congestion -- superseded by (1b) below

Compare this week's chip marginal value against a distribution built from projecting the same
scoring machinery across the *entire* remaining fixture calendar (not just the 5-GW horizon).
Technically buildable -- `project_players`'s `horizon` parameter isn't hardcoded, and the full
season's fixtures are already fetched -- but shown above to have nothing to detect right now,
since the *current* season's fixture list has zero known doubles/blanks yet. Superseded by (1b),
which gets the same kind of signal from real historical data instead of waiting for this season's
own calendar to reveal anything.

### (1b) Historically-grounded double/blank-gameweek prior -- BUILD

**Revisited per explicit direction: model blank/double gameweeks from reasonable assumptions
rather than only the current season's not-yet-revealed calendar.** This repo already has real,
committed historical fixture data for the last 4 completed seasons
(`data/history/{2022-23,2023-24,2024-25,2025-26}/fixtures.csv`) -- mined directly rather than
guessed: for each gameweek, in how many of those 4 seasons did at least 2 teams have a double
(2+ fixtures) or a blank (0 fixtures)?

```
GW:  2   7   8  12  15  17  18  19  20  22  23  24  25  26  27  28  29  31  32  33  34  35  36  37
 w: .25 .25 .25 .25 .25 .25 .25 .25 .25 .25 .25 .25 1.0 .50 .25 .50 .75 .25 .50 .50 1.0 .25 .50 .50
```
(`w` = (seasons with a major double here + seasons with a major blank here) / 4; one clear
one-off anomaly excluded -- GW7 2022-23 was a full-round postponement for Queen Elizabeth II's
death, not fixture congestion.)

**The pattern is clean and consistent across all 4 seasons: real fixture-congestion double/blank
gameweeks essentially never occur before GW19-20, and concentrate heavily from GW25 through
GW37** (every season's largest double/blank events fall in that range). Scattered single-count
blips before GW19 (GW2, 7, 8, 12, 15, 17, 18) look like idiosyncratic one-off match postponements
(weather, individual incidents), not the structural cup-driven pile-up the big late-season cluster
represents.

**This aligns unexpectedly well with the chips' own real half-season windows** (`bootstrap["chips"]`:
Wildcard/Free Hit run GW2-19 then GW20-38): Wildcard-1's entire window sits almost entirely
*before* the real DGW/BGW cluster even starts, while Wildcard-2's window fully contains it. That's
not a coincidence worth reading too much into (both windows are simply calendar halves and the PL
season's own late-crunch is well known) but it does mean **a chip-window-aware model built on this
data will naturally, correctly treat the two halves very differently**, without needing to be told
to.

**Concrete mechanism:** for a chip whose remaining window is `[event, stop_event]`, sum the
weights above over the *remaining* gameweeks (`event+1` through `stop_event`), normalize by the
largest such sum ever observed across a real window (Wildcard-2 at its own opening, ~7.5-9.5
depending on rounding), and use that as a second, independent "extra caution" input alongside (2)'s
calendar-position input -- combine the two (e.g. take whichever is larger, rather than summing, to
avoid double-counting two views of the same "is it wise to wait" question).

**Verified with real numbers (`opportunity_ahead_fraction`, remaining weight / window's own total
weight):**

| Gameweek | Window | Window's total historical weight | Fraction of it still ahead |
|---|---|---|---|
| GW2 | Wildcard-1 [2-19] | 2.00 (small) | 0.88 |
| GW10 | Wildcard-1 [2-19] | 2.00 (small) | 0.62 |
| GW19 | Wildcard-1 [2-19] | 2.00 (small) | 0.00 |
| GW20 | Wildcard-2 [20-38] (just opened) | 7.50 (large) | 0.97 |
| GW22 | Wildcard-2 [20-38] | 7.50 (large) | 0.93 |
| GW30 | Wildcard-2 [20-38] (past the peak) | 7.50 (large) | 0.47 |
| GW36 | Wildcard-2 [20-38] (near the end) | 7.50 (large) | 0.07 |

Wildcard-1's window has a real fraction-remaining number, but a *small absolute* amount of
historical opportunity to wait for -- correctly producing only mild extra caution, on top of
whatever (2)'s calendar-position signal already contributes. Wildcard-2's early gameweeks (20-24)
show both a high fraction *and* a large absolute amount -- correctly producing much stronger "hold
this one" pressure than a pure calendar-position model would, and it correctly fades once the real
historical cluster (GW25-37) has largely passed (GW36: 0.07).

**Honest limit, checked directly rather than assumed:** re-running the combined (1b)+(2) model
against team 364759's real GW2 numbers shows the *combined* extra-caution factor for Wildcard-1
at GW2 barely exceeds what (2) alone already produced (~40.0 effective threshold, same order as
before) -- because Wildcard-1's own window genuinely has little historical opportunity to wait
for. **This does not flip the original GW2 example.** That's not a shortcoming of the model; it's
the model correctly reporting that, historically, there's little reason to expect a *fixture-driven*
jackpot before Wildcard-1 expires at GW19 -- combined with #265's own finding that this specific
squad has real, substantial room to improve right now, "play the Wildcard" is a defensible
conclusion for *this* case, not a bug. What changes is that the *reasoning* becomes concrete and
checkable ("little historical reason to expect something bigger before GW19") rather than an
unexplained constant -- and the model now correctly produces *much* stronger caution than today's
code for the case that actually deserves it: an early-Wildcard-2 (GW20-24) evaluation, which #256's
existing flat cutoff currently treats with zero extra caution at all.

### (2) Per-chip-window scarcity, generalizing #256's flat season-stage cutoff -- BUILD

**What:** Replace `_season_stage_effective_threshold`'s single event-based cutoff with a
per-chip-window position: for each chip candidate, look up its own `start_event`/`stop_event`
(already in `inventory`, passed into `_chip_recommendation`), compute how much of *that specific
window* remains (`window_fraction = (event - start_event) / (stop_event - start_event)`, 0.0 at
the window's first gameweek, 1.0 at its last), and raise the bar by an amount that shrinks as the
window's own deadline approaches -- converging to today's unmodified threshold at the window's
last gameweek, and *resetting to maximum caution* the moment a new half-season window opens,
exactly mirroring the caution #256 already applies at true season start.

**Verified with real data (team 364759, holding the same real squad fixed, varying only the
pretend gameweek -- same technique #256's own plan doc used):**

| Pretend GW | Window | Wildcard marginal | #256's effective threshold | Window-based effective threshold |
|---|---|---|---|---|
| GW2 | [2-19], 0% used | 53.7 | 37.8 (raised) | 40.0 (raised, ~same as #256 here) |
| GW10 | [2-19], 47% used | 33.3 | **20.0 (zero extra caution)** | 30.6 (still raised) |
| GW15 | [2-19], 76% used | 36.8 | **20.0 (zero extra caution)** | 24.7 (still raised) |
| GW19 | [2-19], 100% used | 38.7 | 20.0 | 20.0 (correctly converges) |
| **GW20** | **[20-38], 0% used (new window!)** | **39.3** | **20.0 (zero extra caution -- misses the reset entirely)** | **40.0 (correctly re-raised)** |
| GW25 | [20-38], 28% used | 46.7 | 20.0 | 34.4 |

**GW20 is the clean confirming case**: #256 treats it identically to GW19 (no reason to, since
it only tracks distance from season start), while the window-based model correctly notices a
brand-new Wildcard just became available and re-applies real caution -- flipping this specific
example from "clears the bar" (39.3 > 20.0) to "does not clear" (39.3 < 40.0). This is a real,
demonstrable improvement over what #256 shipped, independent of anything else in this plan.

**Honest limit, consistent with what #265's investigation already established:** at GW2 itself
(this issue's original trigger), the window-based model computes almost the same effective
threshold as #256 already does (40.0 vs 37.8) -- both are near their maximum caution already, since
GW2 sits at the very start of Wildcard-1's own window *and* at the start of the season
simultaneously. **This candidate does not, by itself, suppress the original GW2 example
(aggressive Wildcard clearing by 53.7)** -- pushing the multiplier hard enough to suppress that one
case (needs roughly 2.7x, not #256's current 2x cap) risks the same failure mode #256's own plan
doc already guarded against: making the bar unreachable for a squad that genuinely deserves an
early Wildcard. This candidate is a real fix to a real, separate gap (the GW20-reset blind spot),
not a fix for the GW2 case specifically -- see (3) for why that needs a different kind of
mechanism entirely.

### (3) Self-calibrating relative bar from #266's own persisted history -- promising, but sequenced *after* #266

**What:** #267's own candidate (1), done the right way once the infrastructure exists: instead of
guessing a season-long distribution from unavailable future fixture data, compare *this* week's
computed marginal value against the *actual trailing history of this exact squad/profile's own
previously computed marginal values* -- e.g. "only recommend playing when this week's marginal
value is meaningfully above the last N weeks' own values," self-calibrating per manager, no
synthetic distribution assumptions, no backtest infrastructure needed (matching #184's own
established limitation that no historical chip-decision backtest harness exists).

**Why this is the right long-term shape**: it directly answers the real question ("is *now*
unusually good, relative to what this squad has actually been showing") without needing to know
about DGWs in advance or to guess at a season's typical shape from a synthetic model. It also
naturally explains *why* a chip fired ("this week's opportunity is 40 points above your last 6
weeks' typical" is a legible, checkable claim -- unlike "it cleared a hand-tuned constant").

**Hard dependency, not optional sequencing**: this needs *some* week-over-week persisted history
of each team's computed marginal values to compare against, which does not exist anywhere today
(confirmed while investigating #266: `archive_team_forecast` stores lineup/action, never
`chip_recommendation`). #266 is exactly the mechanism that would need to add that. Building (3)
before #266 would mean inventing a second, redundant persistence layer -- not proposed. This
candidate should be designed once #266 ships and its stored shape is known, not now.

## Recommendation

**Build (2) + (1b) together, combined via `max()` of their two "extra caution" contributions.**
Both are real, grounded in data gathered above (live `_chip_inventory` windows for (2); 4 seasons
of `data/history/*/fixtures.csv` for (1b)), buildable now with no new infrastructure, and correct
a genuine, confirmed gap in #256's shipped code: it neither accounts for each chip's own
half-season window (the GW20-reset blind spot) nor for the real, historically lopsided
distribution of double/blank gameweeks between those two halves. Concretely:

- Extend `_chip_inventory`'s already-computed `start_event`/`stop_event` into
  `_season_stage_effective_threshold` (replacing its current flat, single `event`-vs-cutoff-10
  logic) so each chip candidate's caution is based on its own window's position, not a shared
  season-wide cutoff.
- Add the historical weight table (baked as a small module-level constant, computed once from
  `data/history/*/fixtures.csv` -- not fetched live, so no runtime cost or new data dependency) and
  fold `_remaining_historical_opportunity`'s normalized contribution into the same effective-
  threshold calculation.
- Label both honestly the same way #256's own constants are labeled -- a reasoned heuristic build
  from real historical data, not a fitted/backtested model (no infrastructure exists to backtest
  chip decisions, per #184's own established finding).

**Do not change how "chip potential vs. transfer potential" itself is measured** -- confirmed
above that the existing comparison already favors the chip by an even wider margin against a
realistic baseline than the constant it's compared to suggests. The wisdom judgment belongs
entirely in the scarcity/opportunity-cost layer, not in re-basing the potential comparison.

**Sequence (3) after #266 ships**, as the real longer-term answer once self-calibrating history is
available to design against directly.

**Honest scope of what this does and doesn't change**, confirmed with real numbers, not assumed:
this correctly and substantially raises the bar for an early-Wildcard-2 evaluation (GW20-24, where
#256 today applies zero extra caution despite a fresh chip and the real historical DGW/BGW cluster
still fully ahead) -- a genuine, previously-unaddressed gap. It does **not** flip this issue's
original GW2/Wildcard-1 trigger case, because Wildcard-1's own window has little historical
fixture-driven opportunity to wait for in the first place; per the section above, that specific
case's "play the Wildcard" conclusion is a defensible read of a squad with real, immediate room to
improve, not a bug this model should try to suppress.

## Drop-in text for IMPLEMENTATION_PLAN.md, for (1) and (3)

**(1)** superseded by (1b) -- no separate declined-entry needed, this plan doc records the
supersession directly.

**(3)** not drafted here since it isn't being declined outright -- it's deferred pending a real
future trigger (#266 shipping). Revisit this plan doc directly once #266 lands rather than writing
a stale declined-entry now.
