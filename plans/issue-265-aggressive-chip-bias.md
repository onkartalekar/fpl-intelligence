# Aggressive-profile chip marginal value: re-investigated (issue #265)

## Context

#265 was filed after live-checking #256's shipped fix on team 364759: even under #256's raised
early-season bar, aggressive Wildcard cleared by +45.6 (later re-checked at +15.9/+53.7 on
different live pulls as real data drifted). #265's original diagnosis: the aggressive profile's
`differential = min(0.3, max(0.0, 20.0 - ownership) * 0.015)` term (`recommendations.py:596-599`,
mirrored in `transfer_decisions.py`) rewards low ownership per player, and summed across a
0-of-15-overlap full squad rebuild, was assumed to be the dominant inflator.

**Re-investigating with real numbers substantially undermines that diagnosis.** This plan
documents the fuller investigation, corrects course per two dead ends, and lands on a
significantly narrower (and much less certain) conclusion than #265 as filed claims.

## Investigation, in the order it happened (including two dead ends)

### Dead end 1: the differential/ownership term is not the dominant driver

Patched `_profile_event_score` to strip the aggressive-only `differential`/`minutes_penalty`
term entirely (simulating a maximal version of #265's proposed fix) and re-ran
`build_transfer_decisions` on team 364759's real live squad:

| | Wildcard marginal (aggressive) |
|---|---|
| Unpatched (today's code) | 53.7 |
| Differential/minutes_penalty term fully removed | 52.8 |

A ~2% change. #265's central claim -- that this term is *the* structural inflator -- does not
hold up under direct testing. Whatever is producing the ~53-point gap, it is not primarily this
term.

### Dead end 2: confidence-band amplification doesn't differ between the two squads either

A second hypothesis: aggressive's `profile_fixture_xp` upper-bound construction
(`recommendations.py:443-448`: `aggressive = fixture_points * (1 + uncertainty)`, where
`uncertainty` is 0.16/0.25/0.38 by confidence) might inflate the gap if the optimizer's chosen
squad skews toward lower-confidence (bigger-multiplier) players versus the manager's real squad.
Checked directly: **both the manager's real squad and the wildcard-optimal squad are 100% "low"
confidence** (`Counter({'low': 15})` for both) -- this early in the season, `minutes >= 2400`/
`>= 1200`'s season-accumulated thresholds are unreachable for literally every player, so the
uncertainty multiplier (0.38 for "low") applies uniformly to both squads. This cannot be the
differentiator either.

### The apparent "chip overrides a strictly better ordinary plan" alarm -- also not a bug, on closer reading

While investigating, the same live pull showed aggressive's own *ordinary* 5-transfer scenario
projecting `net_gain_5gw = 227.2` (`transfer_decisions.py`'s `_scenario`/`_beam_multi_transfer`
path) -- nearly 4x wildcard's marginal_value (53.7) -- yet `chip.get("action") == "play"`
unconditionally overrides `ordinary_recommendation` (`transfer_decisions.py:1120-1122`) without
ever comparing the two numbers. This looked, at first, like a second, cleaner, structural bug:
a chip beating a far worse alternative it was never actually compared against.

**Reading `_chip_recommendation` more carefully shows this alarm is unfounded.** `squad` inside
`_chip_recommendation` is `no_chip_scenario["squad"]` -- and the caller passes
`ordinary_recommendation` (already resolved to the *best* ordinary scenario, including any
winning 5-leg multi-transfer) as `no_chip_scenario`. So:

```
marginal = _central_points(wildcard_squad, 5, profile) - _central_points(squad, 5, profile) + no_chip_scenario["point_cost"]
```

is **already** "how much better does Wildcard's full rebuild do than the best ordinary scenario
already found, crediting back that scenario's own hit cost" -- not a comparison against the
original, untouched squad. Backing out the real numbers: the winning 5-transfer scenario's
`point_cost` was 16 (4 hits beyond 1 free transfer); `53.7 - 16 = 37.7` is the *raw* central-points
gap between the Wildcard squad and the already-optimized 5-transfer squad. That's a real, sensible
number -- Wildcard reaches a fully rebuilt 15-player squad (all 15 slots) at zero hit cost, which
should indeed beat a budget/leg-constrained 5-transfer scenario by some real margin. The two
numbers (`marginal_value` and `net_gain_5gw`) are reported on different baselines (one incremental
over the best ordinary scenario, one over the original squad) by design, not by oversight, and
comparing them at face value (as this investigation initially did) is the actual mistake, not the
code.

**This is exactly the kind of thing plan-issue's own guidance warns about** ("build a concrete
mockup with real numbers before trusting it" -- a candidate that looks wrong in the abstract can
turn out to be structurally sound once traced through with real numbers, the same lesson issue
#31's plan doc drew from a different angle). Filing an issue over this would have been wrong;
caught here instead, before propagating it into a second GitHub issue.

## What's left, honestly

After both hypotheses failed to hold up, the residual explanation for aggressive's larger number
is unglamorous: **team 364759's real squad has made zero transfers all season
(`transfers_made: 0`, `current_event: 1`)**, and the model finds substantial real room to improve
it under *every* profile -- balanced's own ordinary 5-transfer scenario already nets +105.4 over
the untouched squad; aggressive's nets +227.2. Aggressive's chip marginal being the largest of the
three is consistent with aggressive's explicit "upper projection, greater variance" framing
(`decision-center.js`'s own profile description) applied on top of a squad the model already
believes has a lot of headroom under *any* lens -- not obviously a bug, more likely the model
correctly identifying a genuinely under-managed squad and reporting a bigger number for the
profile whose whole purpose is showing the upper end of that opportunity.

**The one confirmed, real, but minor finding**: the differential/ownership term does add a small,
real amount (~1 point on this squad, ~2% of the total) that has no equivalent in
conservative/balanced's scoring, and per `SPECIFICATION.md`'s own "Differentials: Allowed only
when projection-supported" line, arguably shouldn't inflate the *headline number compared against
a fixed points-shaped threshold* even at that magnitude -- but at ~2% of the total gap, removing
it would not have prevented, or even meaningfully softened, the actual behavior the user flagged.

## Candidate directions

### (a) Build #265 as originally scoped (strip the differential term from the reported marginal value) -- DECLINE as insufficient

Real, defensible on `SPECIFICATION.md`'s own terms, cheap to do -- but shown above to move the
number by only ~2%. Would not address the behavior that prompted #265 in any visible way. Not
worth doing *as the fix for #265*, though see (c) below for a smaller-scoped version of this same
idea under a different justification.

### (b) Investigate further (backtesting, more squads) before concluding anything -- not recommended as a blocking requirement

Testing against more real squads (differently-managed, some template-heavy, some
already-transferred) would give more confidence in "is 364759's squad just genuinely
under-optimized" vs. "aggressive systematically overstates opportunity for most squads" -- a real
open question. But this needs either a backtest harness (which #184's plan doc already confirmed
doesn't exist for chip decisions) or a lot of manual spot-checking. Not proposed as a prerequisite
to closing out this plan -- flagging as a legitimate follow-on if the pattern recurs on other real
teams, not a blocker.

### (c) Retract #265's diagnosis, close/narrow it, and stop here -- BUILD (as a documentation/issue-hygiene action, not a code change)

The honest outcome of this investigation is that #265's central claim doesn't hold up, and no
other clear bug was found in its place (the second alarm resolved as correct-by-design). The
right action is to **amend #265 on GitHub** with this investigation's findings (not silently
close it, and not leave the original, now-contradicted diagnosis standing unchallenged), and
either:
- narrow it to the confirmed-but-minor differential-term cleanup (candidate (a)'s code change,
  now justified purely by `SPECIFICATION.md` consistency rather than by "this fixes the big
  number"), or
- close it as "investigated, root cause not confirmed as originally stated; no fix identified
  that would change the observed behavior" and let a *future* report (ideally against a
  different real squad, to separate "this one squad is unusual" from "aggressive is generally
  miscalibrated") reopen the question with better evidence.

This is not a decision to make unilaterally -- see Recommendation below.

## Recommendation

**Do not build a code fix from this plan.** The investigation that #265 was based on does not
hold up under direct, real-data testing (both proposed mechanisms tested essentially null), and
the one alternative bug candidate found along the way turned out to be correct-by-design once
traced through with real numbers. Recommend (c): amend #265 with this finding rather than
building against a diagnosis that's been shown not to hold.

This needs the user's explicit call, not mine, on which of (c)'s two sub-options to take (narrow
to the minor differential-term cleanup vs. close outright) -- see the two options presented back
in chat.

**Decided (2026-08-28): close #265 outright, no code fix.** Confirmed empirically along the way
(prompted by the user directly asking "have you tried checking if making one or two transfers
makes a difference") that the real, more consequential finding here isn't about aggressive's
scoring at all: a single ordinary transfer on this same real squad already nets +65.6 (5-GW), a
completely normal weekly action; Wildcard's much larger total edge (+280.9 over the original
squad) mostly reflects that an *unconstrained* full rebuild structurally dominates any
budget/leg-constrained transfer plan, not that transfers alone are insufficient. That's a
scarcity/opportunity-cost gap in `_chip_recommendation` itself (no notion of "is this the best of
the ~36 remaining gameweeks to spend one of two Wildcards"), not an aggressive-profile scoring
bug -- scoped into #267 instead. #265 closed with this session's full findings; see also
`IMPLEMENTATION_PLAN.md`'s "Considered and declined -- 'fix' aggressive-profile chip marginal
value directly (2026-08-28)" entry.
