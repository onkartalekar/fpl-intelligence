---
name: dev-loop
description: Run one full turn of the issue → PR development cycle by hand — pick the next issue from a ranked backlog (or take one you name), route it to the right skill (file-issue / investigate-and-issue / plan-issue / ship-issue), self-review the branch, and stop at an open PR. Never merges. Files follow-up issues for out-of-scope gaps found along the way. Usage: /dev-loop [<issue-number> | "<free-text idea>"]
---

# dev-loop

Orchestrates the skills that already exist into one repeatable cycle you drive by hand. It does **not** replace them — each step hands off to the real skill. One invocation = one item carried from raw input or backlog to an open PR (or to a filed issue / plan doc, when that is the right place to stop). Run it again after the PR merges for the next turn.

The loop both **consumes** issues (ranks the backlog, ships the top one) and **produces** them (files follow-ups for gaps found while shipping). Prioritization and filing are first-class steps, not side effects.

## 0. Preconditions — check before doing anything

- `git fetch origin && git status` — the primary working tree must be clean and on `main`. If not, stop and report; don't clobber uncommitted work.
- **One dev-loop PR in flight at a time.** `gh pr list --state open --json number,headRefName,title` — if an open PR is already on an `issue-*` branch, stop and name it: finish that one (review + merge) before starting another worktree and PR. Override with `--stack` only if the user explicitly wants parallel branches.
- `ls .claude/worktrees/` — a leftover worktree from an earlier turn means the last one didn't finish cleanly. Surface it; don't just add another alongside.

## 1. Intake — what seeds this turn

Resolve the argument, if any:

- **A number** (`/dev-loop 42`) — an existing issue. Skip to step 3.
- **Free text** (`/dev-loop "decision-center cache never invalidates on profile edit"`) — a raw idea or a gap, not yet tracked. Go to step 2.
- **No argument** — rank the backlog and pick (step 1a).

## 1a. Prioritize the backlog

Only when no argument was given. The point of this step is a *reasoned* pick the user signs off on — never silently start on whatever issue is numerically first.

- `gh issue list --state open --json number,title,labels,createdAt,body`
- Rank the open issues using these signals, roughly in this order of weight:
  1. **Correctness and user-facing breakage first.** Data-accuracy bugs, wrong output, broken dashboard behaviour outrank any enhancement. This repo's bug history (stale data, wrong club attribution, filter edge cases) is exactly this shape.
  2. **Time-sensitivity.** FPL is deadline-driven — anything tied to a deadline-reminder window, gameweek rollover, or a feature only verifiable inside a specific gameweek climbs when that window is near.
  3. **Unblocks other work.** Read each issue's `## Dependency` section. An issue named as a blocker for others ranks up; an issue with its own unmet dependency ranks down (or is skipped this turn and noted as blocked).
  4. **Already planned.** A `plans/issue-<N>-<slug>.md` doc with a "build" recommendation means the design work is done and ambiguity is low — that's a cheap, high-confidence turn.
  5. **Scoped and small** over large and ambiguous, when nothing above separates them.
  6. **Age** as a weak tiebreaker only — a stale issue isn't automatically a priority.
- Present the top 3–5 as a short ranked list, one line of reasoning each, then **recommend the top pick and wait.** The user confirms, picks a different one, or names something not on the list. Don't proceed on the recommendation alone.
- Nothing eligible (empty backlog, or everything blocked) → stop and say so.

## 2. Untracked idea or gap → file it first

Route by shape — the same fork the standalone skills use:

- A **confusing symptom** with no confirmed cause ("the count is wrong", "X doesn't show up") → [[investigate-and-issue]]: reproduce against live data, trace to the actual mechanism, *then* open the issue.
- A **design / architecture gap** surfaced from code or conversation → [[file-issue]]: ground it in real `file.py:line` citations, structure it Context / Request / Not in scope / Dependency, leave genuinely open design questions marked open.

Filing the issue is a valid place for the turn to end. Continue to step 3 (implement it now) only if **both** are true: the user asked to, and the issue is clear-cut — one obvious fix, no open design questions. Otherwise stop; a freshly filed design issue usually wants a beat for the user to react before anyone builds it.

## 3. Route the issue

`gh issue view <N> --json number,title,body,labels,url,comments` and read it **fully**, comments included.

- Open-ended research / design question with more than one viable direction → [[plan-issue]]. It writes `plans/issue-<N>-<slug>.md`, presents candidates, and **stops for the user to pick a direction.** The loop turn ends there.
- Bug-shaped symptom not yet root-caused → [[investigate-and-issue]] to confirm the mechanism against live data, then continue.
- Clear-cut, uncontested implementation → [[ship-issue]].

## 4. Ship (when step 3 lands on ship-issue)

Run [[ship-issue]] as written — isolated worktree under `.claude/worktrees/issue-<N>-<slug>`, full suite via [[run-full-tests]] **as a blocking foreground call** (this repo's single most repeated failure mode is an agent backgrounding the suite, saying it will wait for a notification, and then stalling — don't), live check via [[verify-dashboard]] when the change is observable in the dashboard, PR with a `Fixes #<N>` closing keyword.

### 4a. Out-of-scope discoveries → file, don't fix

While implementing you will notice adjacent problems — dead code, a second bug nearby, a stale doc, a missing test elsewhere. **Keep the diff scoped strictly to this issue.** For each real one, [[file-issue]] a follow-up (or `spawn_task`) instead of widening the PR. This is the half of the loop that refills the backlog. List any new issue numbers in the PR body and in your final report.

## 5. Self-review the branch

- Run `/code-review` (default effort) on the branch before handing back.
- Fold high-confidence correctness findings into the same branch; re-run the affected tests ([[run-full-tests]] fallback command for a single file).
- Uncertain or out-of-scope findings → step 4a (file, don't fix).
- Push the review fixes.

## 6. Stop — do not merge

- Report: the PR link, one line on what changed and how it was verified, and any follow-up issues filed in 4a.
- **No merge.** [[merge-pr]] runs only on a later, separate, explicit instruction naming the PR — never chained from here, exactly as [[ship-issue]] step 7 and [[merge-pr]] step 1 require.
- The next turn is a fresh `/dev-loop` invocation, after this PR is merged (or the user passes `--stack`). Don't roll straight into the next issue on your own.
