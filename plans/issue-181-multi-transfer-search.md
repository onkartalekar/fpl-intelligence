# Multi-transfer (3+) search (issue #181)

## Context

Issue #181 (surfaced while discussing #176) confirms the transfer search never generates a candidate with more than 2 transfers in one gameweek: grepping every `"action"` value ever produced in `src/fpl_intel/transfer_decisions.py` finds exactly three -- `"roll"`, `"single_transfer"`, `"double_transfer"` -- for both the immediate-gameweek recommendation (`build_transfer_decisions`) and every step of the 5-gameweek planner. `maximum_free_transfers` defaults to 5 (`transfer_decisions.py:792`), matching FPL's real 2024/25+ banking rule, so a manager can genuinely have 3-5 free transfers banked with no way for this tool to ever discover or recommend using them together.

## Structural constraint found before evaluating candidates

**This is a documented product specification, not an oversight.** `SPECIFICATION.md`'s "Weekly transfer and chip contract" section states the scope explicitly:

> "For Conservative, Balanced, and Aggressive profiles, compare rolling the transfer, one transfer, and two transfers."
>
> "The weekly planner ... searches legal roll, single-transfer, and double-transfer branches..."

No rationale is given in the spec text for the ceiling of two -- most likely an original tractability-driven scoping decision from early in the project (brute-force enumeration genuinely can't reach a third leg with the current approach, confirmed below), never revisited since. There's no "Considered and declined" entry in `IMPLEMENTATION_PLAN.md` about this either -- it was never previously deliberated and rejected, it's simply the scope as originally written.

**This means building anything here requires amending `SPECIFICATION.md`'s documented contract first, not just picking an implementation.** That's a product decision (does the spec's scope change), separate from the algorithmic question of *how* to search a wider space once it does. This plan treats both as open, in that order.

## Why brute-force enumeration can't just be extended (confirmed with real numbers)

`_best_double`'s existing double-transfer search (`transfer_decisions.py:234-273`) already nests `single_moves[:35]` (first leg) x squad (15 slots) x `candidates_by_position` (≤35 per position) -- 35 x 15 x 35 = **18,375** combinations, each fully evaluated via `_squad_objective`. This is already near the edge of what's practical (per #176, this loop alone was ~2.7s of real per-call time even after the #177/#180 memoization fixes). Naively adding a third leg the same way multiplies this by roughly another `x15 x35` factor -- on the order of **9.6 million** combinations, fully brute-forced, per profile. Not viable with the current enumerate-and-score-everything strategy; any 3+-leg search needs a fundamentally different approach.

## Candidate operationalizations

Evaluated assuming the spec question resolves to "yes, extend the contract" -- these are the *how*, not the *whether*.

### (a) Full brute-force to N legs -- DECLINE

**What:** Extend `_best_double`'s exact nested-loop shape by one more level per additional leg.

**Verdict: decline.** Already shown above to be computationally infeasible past 2 legs (~9.6M combinations for a third leg alone, growing combinatorially for each additional one). Not a viable path regardless of the spec question's answer.

### (b) Greedy incremental leg-by-leg construction -- viable, but has a real blind spot

**What:** Reuse `_candidate_moves` (already a general "find the best single transfer from an arbitrary squad state" function, not specific to the starting squad) as a repeatable building block. Starting from the current squad, run it once to find the best single transfer, apply that transfer, then run it *again* on the resulting squad to find the best next transfer, repeating up to the leg count being evaluated. At each leg count (1 through `maximum_free_transfers`), compute `net_gain_5gw` the same way `_scenario` already does today (gross gain minus accumulated hit cost) and keep whichever leg-count nets out best -- exactly generalizing how `roll`/`single`/`double` are already three separate candidates compared today, just with more of them.

**Cost:** `_candidate_moves` truncates to 45 candidates per position and evaluates up to 15 x 45 = 675 combinations per call (confirmed reading the code, `transfer_decisions.py:208`). A greedy search up to 5 legs costs roughly `5 x 675 = 3,375` evaluations -- **cheaper than today's existing double-transfer search (18,375)**, not more expensive.

**The real risk, and it's not the same risk as the pruning idea already declined on #176.** Greedy construction has no backtracking: the locally-best first transfer might consume budget or a position slot in a way that blocks a better *combined* first+second choice a joint search would have found. This is a real quality gap versus an exhaustive search. But -- important distinction from the pruning idea declined on #176 -- that idea would have made an *already-working, exhaustive* 2-transfer search worse to save time. This is going from *zero capability* (nothing recommended for 3+ transfers today) to *an approximate but real* recommendation. The downside of greedy is "might not find the mathematically optimal N-transfer combination," not "might recommend something worse than what the tool already tells a visitor today."

### (c) Beam search over transfer legs -- BUILD, primary recommendation

**What:** The same idea as (b), but instead of collapsing to a single best candidate after each leg, keep a beam of the top-B partial candidates and expand every one of them by one more leg before re-truncating to the top-B again. This directly generalizes `_best_double`'s own existing shape (its `single_moves[:35]` truncation *is* effectively a beam of width 35 for the first leg, before searching second legs) and mirrors the 5-gameweek planner's already-working `_best_planner_continuation` beam search (`beam_width=8` there) -- both real, proven precedents already in this codebase to build from, not a new algorithmic paradigm being introduced.

**Cost:** roughly `B x legs x 675` evaluations. At a beam width of 10 and up to 5 legs: `10 x 5 x 675 = 33,750` -- about 1.8x today's existing double-transfer search cost, still a small fraction of `build_transfer_decisions`'s total per-call time. At B=20: `67,500`, about 3.7x -- still reasonable. Exact beam width is a tuning knob, not decided here (see Open questions).

**Why this over (b):** for modest additional cost -- still cheap relative to what the tool already spends today -- beam search substantially closes (b)'s blind spot by keeping multiple promising partial combinations alive instead of committing early to one. There's no meaningful reason to accept greedy's quality gap to save a cost difference this small; recommending beam over greedy is an engineering call I'm comfortable making, unlike the deeper "should we risk quality for speed" trade-off already declined on #176 (that traded away an existing exhaustive baseline's guarantees; this only ever compares against "nothing," so there's no equivalent baseline being weakened).

### (d) Meet-in-the-middle / exact combinatorial optimization -- not recommended, noted for completeness

**What:** A more sophisticated exact technique (split the legs into two halves, enumerate each half's combinations separately, merge to find the true optimum) that could in principle guarantee the mathematically best N-leg combination without full brute force.

**Verdict: not recommended.** Meaningfully more complex to implement and reason about correctly than (c), for a guarantee (finding the *provably* optimal combination, not just a good one) that the beam-search approach's cost/quality trade-off likely doesn't need. Worth knowing this exists if beam search's quality turns out to be insufficient in practice, but not the starting point.

## Interaction check: does this touch anything else?

Confirmed no interaction needed with wildcard/free-hit evaluation (`_chip_recommendation`/`_exclusive_chip_scenario`) -- those are a separate, mutually-exclusive mechanism (`_optimize_squad`'s full-squad reoptimization) already evaluated independently of the ordinary-transfer search, unaffected by how many ordinary-transfer legs get searched.

Confirmed the hit-cost economics genuinely don't need to change (per #181's own body): `point_cost = max(0, transfer_count - free_transfers) * 4` and `net_gain_5gw = gross_gain_5gw - point_cost` are already written generically for any `transfer_count`.

## Open questions

- ~~The spec question itself: does `SPECIFICATION.md`'s "Weekly transfer and chip contract" get amended to allow more than 2 transfers?~~ **Decided (2026-08-15): yes.** `SPECIFICATION.md`'s immediate-gameweek comparison now reads "through the current official maximum number of banked free transfers" (issue #181 cited inline). The planner's per-step branching was deliberately left at roll/single/double, with an explicit note that widening it is a separate, not-yet-made decision -- matching this plan's scoping recommendation below.
- **Immediate recommendation only, or also the 5-gameweek planner?** Still open, and now also reflected in the spec text itself. The planner calls its per-step search far more often (up to 4 continuations x 5 steps x up to 8 beam nodes), so extending multi-leg search there multiplies cost much further than the immediate-only case. `_planner_single_moves` already uses a much smaller candidate cap (top-8 per position, `limit=6` results) than `_candidate_moves` (top-45, unlimited) specifically because it's called so much more often -- an existing precedent for tuning per-call cost down when call volume is high, directly relevant if/when this extends into the planner. Recommend starting with the immediate-gameweek recommendation only, measuring with #179's benchmark, and treating planner integration as a deliberately separate follow-on decision once real numbers exist.
- **Beam width and max legs to actually support.** `maximum_free_transfers` (today defaulting to 5) is the natural ceiling, but whether to search all the way to 5 or cap lower (e.g. 3-4, on the theory that 5-transfer-banked scenarios are rare) is a product/cost tuning call, not decided here.

## Recommendation

1. ~~Get an explicit decision on the spec question.~~ **Done -- `SPECIFICATION.md` updated 2026-08-15.**
2. ~~Build (c), beam search over transfer legs...~~ **Done -- `_leg_moves`/`_beam_multi_transfer` in `transfer_decisions.py`, wired into `build_transfer_decisions` and `build_draft_decisions`, plus `dashboard.js` display support.**
3. ~~Scope the first version to the immediate-gameweek recommendation only...~~ **Done as scoped -- the 5-gameweek planner's own per-step branching is untouched.**
4. ~~Verify (don't just assume) that the existing hit-cost/net-gain formulas hold unchanged...~~ **Done, and this step found a real, separate bug along the way** (see below) -- `net_gain_5gw` itself was subtly broken for *all* transfer counts, not just 3+, just invisible enough with 1-2 legs to go unnoticed until testing 3-5 exposed it clearly.
5. ~~Tune beam width and max-legs-to-search empirically...~~ Shipped with `beam_width=10` (the module-level default), untuned beyond confirming it produces correct, sensible results on realistic data. Revisit if real-world use shows it's too narrow or unnecessarily wide.

## Unplanned finding: `_squad_objective`/`_central_points` divergence (fixed as part of this work)

While building test coverage (step 4 above), a real, pre-existing bug surfaced: `_squad_objective` (what `_candidate_moves`/`_best_double`/this beam search rank candidates by) and `_central_points` (what `net_gain_5gw` -- the number that actually decides which scenario wins, and what the dashboard displays as "5-GW gain" -- is built from) used *different* scoring rulers. `_squad_objective` read each player's profile-adjusted score; `_central_points` always read the plain, risk-blind central estimate regardless of profile. A transfer the search correctly favored (e.g. conservative trading a little raw upside for a lot more reliability) could be *reported* as a point loss, because the reporting number never reflected the profile's own risk view at all.

Confirmed this was pre-existing, not introduced by the beam search: the *same* mismatch was already present (just small enough to go unnoticed) on the existing single-transfer path -- for one squad, a single transfer that improved `_squad_objective` by +4.21 was already reported as a -0.2 loss in `net_gain_5gw`, before this issue touched anything. Extending to 3-5 legs made the same latent bug compound into something clearly visible (up to -15 points) instead of a rounding-sized discrepancy.

**Fixed**: `_event_lineup_schedule`'s `central_points` field now equals `profile_points` (`recommendations.py`) -- both already the same profile-adjusted score, just computed for two different purposes before. Verified: `_squad_objective` and `_central_points` now move together (within the small, separate, and unchanged bench-exclusion gap -- `_squad_objective` credits bench value, `_central_points` still doesn't, left as-is).

**A second-order consequence, spun into its own issue rather than fixed here**: `_chip_recommendation`'s wildcard/freehit marginal-value calculation also calls `_central_points`, and compares it against fixed `_THRESHOLDS` constants that were calibrated against the old, profile-blind scale. Confirmed on real data: aggressive's freehit marginal value jumped to 49.4 against a threshold of 12.0 (a huge, not marginal, margin) once the scale changed; conservative's swung to -40.6. This needs its own recalibration, validated against historical data per `SPECIFICATION.md`'s own model-change rule -- tracked as #184, not decided or fixed here.

## Real-scale performance (measured with #179's benchmark, Python 3.11)

| | Before #181 | After #181 |
|---|---|---|
| `build_transfer_decisions` | 7.66s | 11.01s |
| `build_draft_decisions` | (not separately tracked pre-#181) | 7.67s |

The added cost is the beam search's own work (up to `maximum_free_transfers - 2` additional legs, each expanding a beam of candidates) -- an expected, real trade-off for a genuinely new capability, not a regression in the existing roll/single/double path (verified byte-identical when no multi-leg scenario wins). Whether this needs its own optimization pass is a question for a future #176-style investigation once real usage data exists, not decided here.
