---
name: verify-dashboard
description: Live-verify a dashboard.py/data-pipeline change in the browser preview instead of trusting unit tests alone. Covers this repo's specific server/worktree/browser-tool quirks.
---

# verify-dashboard

Use whenever a change to `src/fpl_intel/dashboard.py`, `server.py`, or any data-refresh script is meant to be observable in the running dashboard. Several real bugs this repo has shipped (stale data, wrong club attribution, duplicate dropdown entries) passed unit tests but were only caught by actually looking at the live page.

## Starting the server
- `.claude/launch.json`-based `preview_start({name: ...})` only resolves relative to the **primary** working directory, not per-worktree. If you're working inside `.claude/worktrees/<...>`, don't rely on it.
- Instead, start the server manually in the background from the worktree: `scripts/start_dashboard.py --port <N> --no-open`, then attach with `preview_start({url: "http://127.0.0.1:<N>/dashboard.html"})`.
- Use `127.0.0.1`, not `localhost` — the server's untrusted-Host check (`create_server`/request-order checks in `server.py`) rejects `Host: localhost` and returns 421.
- Pick a port that isn't already in use by a leftover server from earlier in the session.

## Reading the page
- `read_page` has been observed to intermittently return `(empty page)` immediately after `navigate` or `resize_window` in this environment. If that happens, don't just retry `read_page` in a loop — fall back to `javascript_tool` and drive/inspect the page directly, e.g.:
  - `showView('transfers')` to switch tabs
  - `filteredRows()` to inspect the current filtered dataset
  - `byId('club-filter').innerHTML` or similar direct DOM reads
- Prefer asserting on real data values (e.g. "20 clubs, no duplicates", "a specific player appears under a specific club filter") over just checking that the page loaded.

## Refreshing data
- To pick up backend changes, run `python3 scripts/refresh_dashboard.py` from the repo root (or worktree) and check its printed summary (movements count, feed status).
- `data/official-transfers-latest.json` (along with `data/confirmed-transfers.json` and `data/fpl-fixtures-latest.json`) is gitignored and rewritten as a side effect of any refresh — a git-tracked reference copy lives at `data-seed/`, seeded into `data/` on first boot only if missing (see the volume-shadowed-seed-files bugfix). No `git checkout` cleanup is needed after ad-hoc verification; the `data/` copy isn't tracked, so a refresh during this work never shows up as a diff.

## Cleanup
- Stop any background server process you started (`preview_stop`, or kill the backgrounded shell) once verification is done, so it doesn't linger as a stray process across the session.
