---
name: ship-issue
description: Implement a GitHub issue end-to-end on an isolated worktree branch — investigate, code, test, verify live, and open a PR. Stops before merging; merge only happens on a later, separate explicit instruction. Usage: /ship-issue <issue-number>
---

# ship-issue

Input: a single GitHub issue number, e.g. `/ship-issue 42`. Nothing else should be required from the user.

## 1. Resolve the issue
- `gh issue view <N> --json number,title,body,labels,url,comments`
- Read the body and comments fully. If the issue is a research/investigation question rather than a clear-cut bug, follow the [[investigate-and-issue]] pattern first and confirm the approach before writing code (open questions or multiple viable directions are worth surfacing to the user — don't guess which one they want).

## 2. Set up an isolated worktree
- Make sure `main` is current first: `git fetch origin && git checkout main && git reset --hard origin/main` (only if the working tree is clean — don't clobber uncommitted work without asking).
- Derive a short kebab-case slug from the issue title (3-5 words max).
- `git worktree add .claude/worktrees/issue-<N>-<slug> -b issue-<N>-<slug> origin/main`
- `cd` into that worktree for all subsequent work. Never mix two issues into one worktree/branch.

## 3. Implement
- Investigate root cause with real/live data before writing code — don't guess at the mechanism.
- Keep the diff scoped strictly to this issue. Resist scope creep even if you notice other things worth fixing (flag those separately instead, e.g. via a follow-up issue or a spawned task).
- Add or extend regression tests that would actually have caught the bug/would lock in the new behavior.

## 4. Test
- Run the full suite per [[run-full-tests]]. It must pass before continuing. Do not skip this because "it's a small change."

## 5. Verify live
- If the change is observable in the dashboard, verify it live per [[verify-dashboard]] rather than trusting unit tests alone. Several real bugs in this repo's history were only caught this way.
- If the change isn't observable in a running preview (pure backend logic, tooling, docs), state that explicitly and skip this step rather than starting a server that proves nothing.

## 6. Push and open the PR
- `git push -u origin issue-<N>-<slug>`
- `gh pr create` with a body that includes a GitHub closing keyword (`Fixes #<N>` / `Closes #<N>` / `Resolves #<N>`) so the merge will auto-close the issue — unless the user's plan for this issue explicitly calls for leaving it open.
- Confirm the link actually took: `gh pr view <N> --json closingIssuesReferences`. If it comes back empty, re-query once after a short pause before concluding it's actually not linked — GitHub's linking isn't always instant.

## 7. Stop
- Report the PR link and a one-line summary of what changed and how it was verified.
- **Do not merge.** Do not proceed to another issue. Do not assume approval carries over from a previous issue's merge instruction, even in the same session.
- Wait for an explicit instruction naming this PR/issue (e.g. "merge #<N>") before doing anything further.

## 8. On explicit merge instruction only
- `gh pr merge <N> --squash --delete-branch`
- If it conflicts with something merged in the meantime, resolve the conflict on the branch, push, re-run the full test suite, and re-request the merge instruction rather than assuming it still applies.
- Sync main: `git fetch origin --prune && git checkout main && git reset --hard origin/main`
- Clean up: `git worktree remove .claude/worktrees/issue-<N>-<slug> --force && git branch -D issue-<N>-<slug>`
- If `data/official-transfers-latest.json` (or any other tracked-but-refresh-generated file) shows a diff from ad-hoc verification and isn't itself the deliverable, `git checkout -- <file>` to keep main clean.
