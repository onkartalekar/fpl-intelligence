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
known this far out. **This directly rules out #267's candidate (1) (a season-long relative bar
built from currently-known fixture congestion) and candidate (3) (making #256's B2 heads-up
decision-changing) as useful *right now*** -- there is nothing for either mechanism to find yet.
Both remain valid ideas for later in the season once real DGW/BGW data exists; neither is buildable
today in a way that would change any current behavior.

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

## Candidate operationalizations

### (1) Season-long relative bar from currently-known fixture congestion -- DEFER, blocked on data

Compare this week's chip marginal value against a distribution built from projecting the same
scoring machinery across the *entire* remaining fixture calendar (not just the 5-GW horizon).
Technically buildable -- `project_players`'s `horizon` parameter isn't hardcoded, and the full
season's fixtures are already fetched -- but shown above to have nothing to detect right now.
Revisit once the live fixture calendar actually shows congestion (later in the season); building
it today would just always report "nothing unusual," offering no value now while adding real
computation cost (extending `project_players`'s horizon ~7x would need its own cost check via
`scripts/benchmark_transfer_decisions.py` before shipping, not measured here since there's nothing
yet to justify the cost).

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

**Build (2)** -- the per-chip-window scarcity generalization of #256's season-stage adjustment.
It is real, independently confirmed with live data (the GW20 case above), buildable now with no
new infrastructure (`_chip_inventory` already computes everything it needs), and closes a genuine
gap in already-shipped code. It should be labeled honestly as *not* resolving this issue's
original GW2 trigger case, the same way #256's own constants are labeled as a reasoned heuristic,
not a fitted one.

**Defer (1)** -- blocked on real fixture-congestion data that doesn't exist yet this early in the
season; revisit once it does (the fixtures feed already refreshes regularly, so this is a "check
back later" situation, not a design question left open).

**Defer (3)**, but recommend it explicitly as the real long-term answer to this issue's original
question ("is now the best of the ~36 remaining opportunities"), sequenced strictly *after* #266
ships, once its persisted-history shape can be designed against directly rather than guessed at
here.

None of the above claims to finally suppress the specific GW2 example that opened this whole
investigation thread (#256 -> #265 -> #267) -- that would need either (3)'s self-calibrating
history (not buildable yet) or a season-stage multiplier strong enough to risk unreachability for
genuinely deserving cases (#256's own plan doc already declined that trade-off once). This is the
honest state of the investigation, not a gap being glossed over.

## Drop-in text for IMPLEMENTATION_PLAN.md, if (1)/(3) are ever revisited later

Not drafted here since neither is being declined outright -- both are deferred pending a real
future trigger (fixture data existing, #266 shipping respectively), not ruled out. Revisit this
plan doc directly when either trigger arrives rather than writing a stale declined-entry now.
