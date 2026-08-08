# Split refresh pipeline: shared data vs. per-user computation (issue #46)

Investigated 2026-08-08. Issue #46 already carries substantial prior
direction from `plans/issue-27-cloud-hosting.md` (ships first, ahead of
#44/#45; branches on guest-vs-authenticated) — that prior work settled
the *strategic* shape but not *how* to actually wire per-request
computation into the existing `server.py`/`dashboard.js` architecture.
That's a real implementation-shape question with more than one
reasonable answer, which is what this plan resolves.

## Context

`_refresh_project_unlocked()` in [refresh.py](../src/fpl_intel/refresh.py)
today computes everything — shared FPL data and one configured
manager's personalized recommendations — in a single pass, gated by
`config/user-profile.json`'s single `team_id`. #46's job is separating
"the same for everyone" from "specific to whichever team ID a guest
supplies," and serving the latter per-request without a full pipeline
re-run.

## Structural findings before evaluating candidates

1. **`decision_center` is not monolithically per-user — verified against
   the actual function signatures.** `build_gw_recommendations(bootstrap,
   fixtures, generated_at, horizon, recent_transfers)` in
   [recommendations.py:883](../src/fpl_intel/recommendations.py) takes
   no manager argument at all — `recommended_squad`, `captaincy`,
   `player_forecasts`, and `watchlist` are the same for every visitor,
   computed purely from shared data. Only
   `decision_center["weekly_decisions"]`, built by
   `build_transfer_decisions(bootstrap, fixtures, manager, generated_at,
   recent_transfers)` in
   [transfer_decisions.py:649](../src/fpl_intel/transfer_decisions.py),
   is genuinely per-manager — it needs a specific manager's current
   squad to recommend rolls/transfers/chips *for that squad*. This is
   the actual, narrow per-request boundary, not "all of decision_center."
2. **Model-performance history for guests is structurally unavailable,
   not a gap to fix.** `performance_store` mixes a shared
   `actual_events` history (real gameweek results, same for everyone)
   with per-manager `manager_picks` history (what squad a specific
   manager actually fielded, used to backtest the model against real
   decisions) in one store, written only during the shared refresh for
   whichever `team_id` is in `config/user-profile.json`. A stateless
   guest has no persisted picks history anywhere to backtest against —
   the "how did the model do against what I actually did" view simply
   doesn't apply to a one-off guest lookup. Worth stating explicitly as
   an accepted scope boundary now, so it isn't rediscovered as a bug
   later.
3. **The `127.0.0.1`-only bind stays as-is.** `create_server()` in
   [server.py:201](../src/fpl_intel/server.py) hard-rejects any other
   host. Per the confirmed #27 plan, lifting this is Axis B's job,
   sequenced after #44/#45/#46 — #46 should build and test against
   localhost unchanged, not fold hosting into this issue.
4. **A guest lookup is expensive and unguarded by default.**
   `collect_public_manager(team_id)` in
   [manager_data.py:16](../src/fpl_intel/manager_data.py) makes **four
   sequential live HTTP calls** to the official FPL API (entry,
   history, transfers, current-event picks), each with a 30s timeout
   and no built-in error handling — an invalid team ID or an FPL API
   hiccup raises an uncaught exception today; `_refresh_project_unlocked`
   only survives this because it wraps the call in try/except
   (refresh.py:253-266), a pattern the new path must replicate for a
   clean "team not found" message instead of a 500.
5. **`ThreadingHTTPServer` spins up an unbounded thread per request.**
   Combined with (4), an *unthrottled* guest endpoint is a concrete,
   already-verified availability risk today — not a hypothetical
   deferred entirely to #28 — since a burst of guest lookups can pile
   up slow threads and risks getting the app's own outbound IP
   rate-limited by the official FPL API, degrading the service for
   every visitor, not just the guest making the requests.
6. **No existing precedent for in-place client-side re-rendering.**
   Checked both existing POST flows in `dashboard.js`: `runRefresh()`
   (`/api/refresh`) and `setupProfileForm()`'s submit handler
   (`/api/profile`) **both resolve by doing a full `window.location.reload()`**,
   not by merging the response into the live `state` object and
   re-invoking render functions. `renderManager()`/`renderWeeklyDecision()`
   etc. read a single page-load-time `state` variable
   (`state=JSON.parse(document.getElementById('dashboard-data').textContent)`)
   by closure — there is no established "patch state, re-render"
   pathway anywhere in this codebase today. This is a real fork to
   decide, not a foregone conclusion.

## Candidate operationalizations

### API shape: how a guest's team ID reaches the server and results reach the page

- **(A) In-place fetch, no reload.** A new `/api/team-view` JSON
  endpoint; new JS that fetches it, merges the response into `state`,
  and calls `renderManager()`/`renderWeeklyDecision()` directly.
  Pro: snappier, no full-page reload for a lookup. Con: genuinely new
  architecture for this codebase (finding 6) — first client-side merge
  pathway ever written here, more surface for state-consistency bugs
  (what does the rest of the page look like mid-fetch, on error,
  on a second lookup overwriting the first).
- **(B) Reload-based, matching the existing house pattern exactly —
  recommended.** The guest's team ID becomes a `?team_id=` query
  parameter on `GET /`. `do_GET` (which currently discards query
  strings entirely — `self.path.split("?", 1)[0]`) parses it, runs the
  per-request computation server-side (reusing the shared refresh's
  already-cached `fpl-bootstrap-latest.json`/`fpl-fixtures-latest.json`/
  `official-transfers-latest.json`, plus one live
  `collect_public_manager` call), and splices the result into
  `__DASHBOARD_DATA__` before serving — the same substitution
  mechanism `do_GET` already uses for `__REFRESH_TOKEN__`. A "look up a
  team" form simply navigates to `?team_id=...`; the page loads with
  `state` already containing that guest's `manager`/`weekly_decisions`,
  exactly like every page load does today, so `renderManager()`/
  `renderWeeklyDecision()` need **zero changes**. Bonus: the URL is
  shareable/bookmarkable ("here's my team's recommendations").

**Recommendation: (B).** It matches a pattern this codebase has now
used twice (not assumed — verified in both `runRefresh` and
`setupProfileForm`), and it needs no new client-side merge logic at
all — only a query-param branch in `do_GET` plus extracting the
per-user compute step into one reusable function. (A)'s no-reload UX
is a real but secondary win, and nothing about building (B) forecloses
it later: the compute-and-return-data function this issue needs to
write is shared by both designs regardless of which one serves it.

### Guardrail for the live-FPL-API-triggering path

Given findings 4-5 are a **verified-today risk**, not a speculative
future one, #46 should ship a minimal, stdlib-only guard alongside the
feature itself — e.g. a simple `time.monotonic()`-based per-source-IP
cooldown with a capped in-memory dict — rather than leaving the new
endpoint completely unguarded until #28 gets scheduled. This does not
replace #28's more complete rate-limiting/abuse-protection pass; it's
the minimum needed to ship #46 responsibly on its own.

## Recommendation

1. **Extract the per-user computation into one function** (e.g.
   `compute_manager_view(bootstrap, fixtures, transfers, generated_at,
   team_id)` in `refresh.py` or a new module) that loads the shared
   refresh's cached JSON artifacts, calls `collect_public_manager` +
   `summarize_manager` + `build_transfer_decisions`, and returns
   `{"manager": ..., "weekly_decisions": ...}` — with the try/except
   error handling `_refresh_project_unlocked` already has, adapted to
   return a clean "team not found" style result instead of raising.
2. **Wire it into `do_GET`'s `/` and `/dashboard.html` routes via a
   `?team_id=` query parameter (candidate B)** — parse it, call the
   function above, splice the result into the served `__DASHBOARD_DATA__`
   JSON before the existing token substitution. No new JS render logic
   needed; a lookup form just navigates to the query-param URL.
3. **Ship a minimal per-IP cooldown** on this new compute path in the
   same PR — not deferred to #28.
4. **Explicitly out of scope, left to later issues**: lifting the
   `127.0.0.1` bind (Axis B, post-#44/#45/#46 per the #27 plan);
   anything resembling model-performance/backtest history for guests
   (structurally requires persisted picks history, which only exists
   once #44/#45 land for an authenticated user); the registration/
   session-backed second source of a team ID (#44/#45 add this later
   by populating the same `team_id` input the query-param path already
   established, per #46's existing "design the interface so a second
   source can be added later" scope note).
