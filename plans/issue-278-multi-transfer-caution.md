# Paid multi-transfer early-season caution (issue #278)

## Context

#278 was filed from direct user feedback on real, live data (team 364759, GW3): all three risk
profiles proposed a drastic squad overhaul in the third gameweek -- conservative Free Hit,
balanced 5 simultaneous transfers for a -16 point hit, aggressive Wildcard. Investigation before
filing traced this to two separate, independently-real facts:

1. Bruno Fernandes and Mbeumo (Man Utd, £12.0m/£8.0m) have combined for 2 total points from one
   90-minute start each this season -- a real, if thin-sample, blank statline for two premium
   players. `player_component_rates` (`src/fpl_intel/modeling/projection.py:142-178`) weighs a
   MID/FWD's residual against a positional baseline at `residual_reliability = min(0.82, minutes /
   (minutes + 100))` -- already 47% at 90 minutes, by deliberate, backtested design (the Phase 3
   continuation lowered this denominator specifically because MID/FWD projections were previously
   too slow to react and persistently overprojected; validated across 3 seasons,
   `IMPLEMENTATION_PLAN.md`'s "Phase 3 continuation" section). Not something this issue proposes
   changing.
2. The ordinary multi-transfer path has no equivalent of #256/#267's "don't commit a scarce,
   hard-to-reverse resource this early" caution. The bluntest instance: `transfer_decisions.py`'s
   3+-leg override compares purely on raw `net_gain_5gw` with zero margin --
   ```python
   best_multi_leg = max(multi_leg_scenarios, key=lambda row: row["net_gain_5gw"], default=None)
   if best_multi_leg is not None and best_multi_leg["net_gain_5gw"] > ordinary_recommendation["net_gain_5gw"]:
   ```
   (`transfer_decisions.py:1185-1187`). Any positive margin, however small, overrides the
   roll/single/double planner's own pick. This path is already documented as deliberately *not*
   planner-aware (issue #181's own scoping, `transfer_decisions.py:1159-1168`) -- the natural,
   already-flagged place to add a guard without touching the more validated planner core.

## Open questions from the filed issue, investigated with real data

### (A) Season-stage signal: calendar/event-based, or keyed to the specific players' own minutes?

Pulled real minutes for every player on both sides of the triggering GW3 example (team 364759's
actual squad and the 5-transfer scenario's actual targets):

| Player | Minutes (GW1-2) | Total points | Residual reliability (MID/FWD, denom=100) |
|---|---|---|---|
| B.Fernandes (out) | 90 | 2 | 0.474 |
| Mbeumo (out) | 90 | 2 | 0.474 |
| Palmer (in) | 82 | 13 | 0.451 |
| Cherki (in) | 108 | 22 | 0.519 |
| Stach (in) | 90 | 13 | 0.474 |
| Gakpo (in) | 160 | 17 | 0.615 |
| Emersonn (in) | 65 | 9 | 0.394 |

**Finding: a player-specific minutes signal wouldn't differentiate anything right now.** Every
player involved -- both the ones being sold *and* the ones being bought -- sits in essentially the
same thin-sample band (0.39-0.62 reliability), because the whole league is early-season right now,
not because these particular players are unusually under-observed relative to their peers. A
per-player signal would only earn its complexity in a case a calendar signal structurally can't
see -- e.g. GW20, most players well-established, but one specific transfer target just returned
from injury or just signed in January. That's a real, valuable refinement, but it needs new
plumbing: season-to-date `minutes`/`total_points` aren't threaded into the projection payload
`project_players` builds (`recommendations.py:456-494` has `expected_minutes` -- a forward-looking
estimate -- but not season-to-date observed minutes or a reliability figure) or into the scenario
dicts `transfer_decisions.py` works with. Out of scope for this pass; noted as a follow-on below.

**Decided: calendar/event-based for this pass**, matching what's actually available today with no
new plumbing, and matching the real driver of the current problem (the whole season being
early, not any one player being unusually unobserved relative to others right now).

### (B) Reuse #267's `_chip_scarcity_extra_caution`, or build something new?

**Decided: something new, borrowing the shape.** #267's mechanism is fundamentally about a scarce
resource with its own limited half-season windows (two Wildcards, two Free Hits, ever) -- the
`start_event`/`stop_event` window concept has no analogue for ordinary transfers, which regenerate
every week and aren't scarce in that sense at all. The actual risk here is different: not "you're
foreclosing a better future use of a limited resource," but "you're paying real, permanent points
to act on a signal that's still noisy because not enough of *this season* has been observed yet."
That's the same shape #256's original (pre-#267) flat season-wide cutoff was built for, before
#267 refined it into something chip-window-specific -- reused here in spirit, not by import.

**Concretely: tie the decay rate to the same constant already governing why this problem exists.**
Rather than inventing an unrelated new cutoff event, the margin required to accept a 3+-leg
override decays at the same rate `projection.py`'s own MID/FWD residual-reliability system
stabilizes (denominator 100, cap 0.82) -- estimating "how much of the season has been observed" as
`(event - 1) * 90` minutes (one full match per completed gameweek), then requiring less margin as
that estimate's own reliability climbs toward the cap:

```
observed_minutes(event) = max(0, event - 1) * 90
reliability(event)      = min(0.82, observed_minutes / (observed_minutes + 100))
extra_caution(event)    = 1 - reliability(event) / 0.82        # 1.0 down to 0.0
required_margin(event)  = _MULTI_TRANSFER_EARLY_SEASON_MARGIN * extra_caution(event)
```

| Event | Observed minutes (est.) | Reliability | Extra caution | Required margin (base=10.0) |
|---|---|---|---|---|
| GW2 | 90 | 0.474 | 0.422 | 4.2 |
| GW3 | 180 | 0.643 | 0.216 | 2.2 |
| GW4 | 270 | 0.730 | 0.110 | 1.1 |
| GW6 | 450 | 0.818 | 0.002 | 0.0 |
| GW7+ | 540+ | 0.82 (capped) | 0.0 | 0.0 |

Converges to exactly today's unmodified `>` comparison by ~GW6-7 -- the same convergence guarantee
#256/#267 already give their own thresholds, here reaching zero specifically once the underlying
signal this whole problem stems from (MID/FWD residual trust) has itself stabilized, rather than
at an arbitrary unrelated date.

**`_MULTI_TRANSFER_EARLY_SEASON_MARGIN = 10.0`**, reasoned (not backtested, same epistemic status
as `_THRESHOLDS`/`_EARLY_SEASON_MAX_EXTRA_MULTIPLIER`): the fitted model's own overall backtest
RMSE is ~4.4 points per player-gameweek (`IMPLEMENTATION_PLAN.md`'s Phase 3 table) -- a margin
requirement on the order of two to three times that (10 points, decaying) means a multi-leg
override has to be more than plausibly explained by ordinary single-player projection noise before
it's accepted at maximum caution, without demanding an implausibly large edge.

**Honest limit, checked directly against the real triggering case, not assumed:** this does *not*
flip #278's own triggering example. Balanced's `multiweek_plan.immediate_action` at GW3 was
`double_transfer` (net_gain_5gw 57.7); the 5-leg scenario's 100.8 beats it by 43.1, and even the
3-leg scenario's 74.1 beats it by 16.4 -- both comfortably clear the GW3 required margin of 2.2.
This is the same honest outcome #267 reported for its own triggering GW2/Wildcard case: the
underlying edge here is large enough that it isn't explainable as early-season noise, so a
principled margin correctly declines to suppress it. What this *does* catch is a genuinely
borderline multi-leg override in the season's first few gameweeks -- exactly the failure mode this
issue is about (a large, hard-to-reverse commitment triggered by an arbitrarily small edge), not a
guarantee that every early multi-transfer recommendation becomes conservative regardless of how
strong its case actually is.

### (C) Does the roll/single/double planner path need the same treatment?

**Deferred, not solved here.** `build_multiweek_plan`'s beam search already weighs hit cost against
future flexibility (a more sophisticated objective than the 3+-leg override's raw `net_gain_5gw`
comparison), and touching its internal value function safely is a larger, separate piece of work.
The 3+-leg override is the directly-implicated, already-flagged-as-cruder mechanism (issue #181's
own scoping comment) and the natural place to start. Revisit whether the planner path shows the
same failure mode with its own real examples before extending this further.

## Recommendation

Ship (B) as scoped: a new `_multi_transfer_required_margin(event)` function in
`transfer_decisions.py`, applied only at the existing 3+-leg override comparison
(`transfer_decisions.py:1186`). No change to the roll/single/double planner path, no change to any
projection-model coefficient, no new plumbing for player-specific minutes (deferred as a real,
valuable follow-on once there's a concrete case a calendar signal can't see).
