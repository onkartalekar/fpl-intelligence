---
name: merge-pr
description: Merge a PR that was built on a worktree branch — handle the expected worktree-blocks-branch-deletion failure correctly, independently confirm the merge, and clean up the worktree/branch afterward. Referenced by ship-issue's merge step; also usable directly for any worktree-backed PR that didn't go through ship-issue (e.g. a docs branch).
---

# merge-pr

Only run this on an explicit, separate instruction naming the PR (e.g. "merge #<N>") — never chain it from an earlier approval, even for a related issue in the same session, and never merge as an assumed next step after opening a PR.

## 1. Merge

- `gh pr merge <N> --squash --delete-branch`
- This reliably fails with something like `failed to run git: error: cannot delete branch '<branch>' used by worktree at '.claude/worktrees/<...>'` whenever the branch is checked out in a worktree. **This is expected, not a real failure** — the merge itself still goes through on GitHub's side; only the local branch-deletion half of the command fails.

## 2. Independently confirm

- Don't trust step 1's exit code alone: `gh pr view <N> --json state,mergedAt,mergeCommit` and check `state` is `MERGED` before doing any cleanup.
- If it's not merged because of a real conflict with something merged in the meantime: resolve the conflict on the branch, push, re-run the full suite ([[run-full-tests]]), and re-request the merge instruction rather than assuming it still applies once resolved.

## 3. Clean up the worktree and branch

```bash
git worktree remove .claude/worktrees/<slug> --force
git branch -D <branch>
git fetch origin --prune && git checkout main && git reset --hard origin/main
```

## 4. Keep main clean

If a genuinely tracked-but-refresh-generated file shows a diff from ad-hoc verification during this work and isn't itself the deliverable of the merged PR, `git checkout -- <file>`. Note: `data/official-transfers-latest.json`, `data/confirmed-transfers.json`, and `data/fpl-fixtures-latest.json` are gitignored as of the volume-shadowed-seed-files bugfix (their tracked reference copies now live at `data-seed/`), so a refresh during this work no longer produces a diff for those three specifically.
