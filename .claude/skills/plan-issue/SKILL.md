---
name: plan-issue
description: Investigate an open-ended research/design question filed as a GitHub issue and produce a written implementation plan with candidate directions, evidence, and tradeoffs — for questions with more than one viable direction, not straightforward bugs. Usage: /plan-issue <issue-number>
---

# plan-issue

Input: a single GitHub issue number, e.g. `/plan-issue 31`.

Use this when the issue is a genuine research/design question — "should we do X", "investigate whether Y matters," something with real ambiguity about the right direction. If, once you actually read it, the issue turns out to have one clear, uncontested implementation, don't force a plan doc — hand off straight to [[ship-issue]] instead. This repo already has six precedents of this shape under `plans/` (e.g. `plans/issue-31-transfer-strength.md`, `plans/issue-13-opta-feasibility.md`) — read one before writing a new one to match the house style.

## 1. Resolve the issue
- `gh issue view <N> --json number,title,body,labels,url,comments`
- Read fully. Identify the actual question being asked, not just the title — issues in this shape often bundle a specific trigger ("with all the transfers, does it impact X") with a broader open question.

## 2. Set up an isolated worktree
- Make sure `main` is current: `git fetch origin && git checkout main && git reset --hard origin/main` (only if clean).
- `git worktree add .claude/worktrees/issue-<N>-<slug> -b issue-<N>-<slug> origin/main`, `cd` into it. The branch may end up holding only the plan doc, or later grow to hold the chosen implementation too — that's fine, don't create a second branch just because the work has two phases.

## 3. Investigate candidate directions
- Enumerate the plausible directions — don't stop at the first idea, and don't stop at the one the issue title implies.
- Gather real evidence per candidate: read the actual code paths involved, pull live data samples, check for prior related decisions that constrain the options (e.g. `SPECIFICATION.md`'s model-change rule that any model change must preserve the old model version and be validated against frozen historical forecasts before adoption; `backtest.py`'s documented "Known simplifications"; an existing declined precedent in `plans/` or `IMPLEMENTATION_PLAN.md`).
- If a candidate looks viable on paper, build a concrete mockup with real numbers before trusting it. The issue #31 squad-value panel looked fine in the abstract but a real per-club delta computation showed departures were systematically far less price-matched than arrivals — that asymmetry would have biased every club's number in the same direction. Paper reasoning missed it; real numbers caught it.

## 4. Write the plan doc
- `plans/issue-<N>-<slug>.md`, matching the established structure:
  - `## Context` — what prompted the investigation.
  - Structural constraints or data-quality checks found before evaluating candidates, if any (worth surfacing early if they rule things out cheaply).
  - `## Candidate operationalizations` / `## Findings` — one subsection per candidate, with the evidence gathered.
  - `## Recommendation` — a clear build/decline/defer per candidate, with reasoning, not a menu without a pick.
  - If a candidate is being declined: include the exact drop-in text for `IMPLEMENTATION_PLAN.md`'s "Considered and declined" section under its own heading (see `plans/issue-13-opta-feasibility.md` for the pattern), so it doesn't need to be redrafted later.

## 5. Present to the user and wait
- Summarize the candidates and your recommendation. Do not unilaterally pick a direction when there's genuine ambiguity — that's the point of writing the plan doc instead of just implementing something.
- Expect direction to change after the user reacts, possibly more than once (issue #31 changed direction twice: once on the user's own instinct plus supporting evidence, once after a mockup revealed a candidate was actively misleading). Re-investigate and update the plan doc rather than defending the original recommendation.

## 6. On explicit direction from the user
- For the chosen candidate: hand off to [[ship-issue]] to implement — confirm with the user whether it continues on this same branch/issue or is scoped as a fresh issue.
- For declined candidates: add the pre-drafted text to `IMPLEMENTATION_PLAN.md`'s "Considered and declined" section (matching the existing entries' style) and update the plan doc to reflect the final state.

## 7. Stop and don't merge
- Same discipline as [[ship-issue]]: push, opening a PR, and merging all happen only on separate, later, explicit instructions — never assumed, never chained from a previous approval.
