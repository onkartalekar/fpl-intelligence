# Issue #125 -- One consistent way for GitHub-Actions-hosted scripts to read Railway's live state

## Context

Three separate bugs in this session, all the same root cause: a script assumes it can read
Railway-hosted live data (`profiles.db`, `official-transfers-latest.json`) from the local
filesystem, exactly like `server.py` correctly does -- except `server.py` runs *on* Railway,
inside the same container/volume the refresh pipeline writes to, and these scripts run on a
GitHub Actions runner with no shared filesystem at all:

1. #105 (open): `collect_teams()`'s `FPL_INTEL_REMINDER_PROFILES_DB` path.
2. #122 (shipped in PR #124, incompletely): the new `load_official_transfers(root)`.
3. New, found while investigating this issue: `run()`'s direct `profiles.load_profile(root /
   "data" / "profiles.db", team["team_id"])` call (`send_deadline_reminder.py:935`), fetching
   `confirmed_free_transfers`/`draft_squad_ids` overrides -- broken the same way, never
   previously flagged.

The user's framing directly names the fix shape: stop patching individual local-file-reads one at
a time, and give every GitHub-Actions-hosted script one consistent way to read Railway's live,
already-computed state over HTTP -- the same way the dashboard itself does it in-process.

## Structural findings before evaluating candidates

**The generic (non-personalized) recommendation data these scripts need already exists,
server-side, in one place.** `_refresh_project_unlocked`'s `decision_center` (`refresh.py:341-360`)
-- built from `build_gw_recommendations` + `build_transfer_decisions` -- is published into
`dashboard-state.json["decision_center"]` on every refresh, and (since #120) is exactly what a
no-`team_id` dashboard visitor is served, freshly rendered on every request. Nothing new needs to
be computed for this half; it needs to be *readable* as JSON, not just embedded in rendered HTML.

**The per-team computation these scripts need also already exists, server-side, but only reachable
via HTML today.** `server.py`'s `_default_team_view_action` (`server.py:203-238`) -- the same code
powering the public, unauthenticated `?team_id=` lookup (#46) -- already does everything
`send_deadline_reminder.py` currently tries to reimplement: reads bootstrap/fixtures/transfers
from the live Railway state, reads the team's saved profile (`confirmed_free_transfers`/
`draft_squad_ids` overrides, issue #81) from the *real* `profiles.db`, and calls
`compute_manager_view`. There's no JSON API for this result today -- only `_serve_dashboard`'s
full-page HTML render with the result embedded as `__DASHBOARD_DATA__`.

**Existing precedent already answers most of the hard questions a new endpoint would raise.**
- **Auth model**: `?team_id=` lookups are already public and unauthenticated, rate-limited per-IP
  via `lookup_limiter` (`_TEAM_LOOKUP_COOLDOWN_SECONDS = 15`, `server.py:61,969`). A JSON
  equivalent returning the *same* data isn't a new exposure -- it's the same data in a different
  content-type.
- **PII filtering**: issue #79 already established the exact fields (`email`,
  `reminder_status`, `reminder_lead_hours`, `reminder_pending_email`) that must never appear in an
  explicit `?team_id=` lookup of someone else's team (`server.py:1107-1114`). Any new JSON
  endpoint needs the identical filter.
- **Opt-out**: issue #62's `opted_out` check (`server.py:1093-1094`) already gates the explicit
  lookup path before any live call. Same rule should apply to a JSON equivalent.

**Issue #101's scheduled-refresh trigger is a deliberate, correct exception, not another instance
of this bug.** It needs a live, Railway-independent bootstrap fetch specifically to decide *whether
Railway's own cached state needs refreshing* -- it cannot ask Railway that question via its own
state, since that's circular. This issue doesn't change #101's design.

## Candidate operationalizations

**(A) Keep patching individual local-file-reads as each one is discovered (status quo).**
This is literally what's already happened three times, producing two shipped-but-incomplete
fixes and one previously-unnoticed bug found only by tracing through the same script again.
Guaranteed to recur a fourth time. Declined -- not a real candidate, included for completeness.

**(B) Expose the shared *raw inputs* (bootstrap, fixtures, transfers) via a new read endpoint;
GH-Actions scripts keep calling `compute_manager_view`/`build_gw_recommendations` themselves,
just fed by remote data instead of a broken local read.**
Fixes the "can't read the file" problem, but keeps the actual model-computation code running in
two places (Railway's process, and every GitHub Actions runner) from the same inputs. Doesn't
fully close the drift risk this session already hit twice -- e.g. `run()`'s local
`profiles.load_profile` call for per-team overrides would still need its own separate fix (a third
piece of remote data to fetch), since it's not one of these three raw inputs.

**(C) Expose the *already-computed* output as JSON: shared `decision_center` (or a documented
subset of `dashboard-state.json`) and a per-team `GET /api/manager-view?team_id=<id>` mirroring
`_default_team_view_action`'s exact result.**
GitHub-Actions-hosted scripts stop computing anything themselves -- `send_deadline_reminder.py`'s
`fetch_bootstrap`/`fetch_fixtures`/`load_official_transfers`/local-`profiles.load_profile`/
`compute_manager_view`/`build_gw_recommendations` calls are all replaced by two HTTP calls per run
(one shared-state fetch, one manager-view fetch per opted-in team). Since the per-team endpoint
would be the exact same code path `_default_team_view_action` already runs -- including its
existing `profiles.load_profile` call against Railway's *real* `profiles.db` -- this closes the
newly-found `run()` bug and #122's incompleteness *for free*, without either script needing local
`profiles.db`/artifact access at all. One computation path, everywhere, permanently -- not just a
fix for the three bugs found so far, but removal of the entire class.

## Recommendation: (C), matching the user's own framing.

(A) is not a real option. (B) fixes today's three bugs but leaves the underlying "two independent
compute pipelines that can silently drift" problem in place -- which is the actual mechanism that
produced #122 and the newly-found `run()` bug in the first place, not just an inconvenience. (C)
collapses to one compute path, reuses every existing precedent (#46's public+rate-limited model,
#79's PII filter, #62's opt-out) rather than inventing new rules, and structurally prevents this
class of bug from recurring for any future GitHub-Actions-hosted script (including anything #101
or a future issue might need).

### Open questions for the user (genuinely undecided, not a default-and-move-on)

1. **Endpoint shape**: one endpoint (`GET /api/manager-view?team_id=<id>`, shared data folded into
   its response even for anonymous/no-team_id calls) vs. two separate endpoints (a shared-state
   endpoint plus a per-team one). Two separate endpoints avoids re-sending the (larger, mostly
   static) shared payload on every per-team call in a loop over N reminder recipients -- likely
   the better shape, but worth confirming rather than assuming.
2. **Rate-limiting mechanics for script callers specifically**: `lookup_limiter` is keyed per
   source IP (`server.py:1078`), tuned for real visitors. GitHub Actions runners don't have a
   stable IP (shared, rotating egress ranges) -- reusing the same per-IP limiter unmodified could
   make a legitimate scheduled job flaky (a coincidental IP collision with unrelated traffic) or,
   if the limiter is too permissive to accommodate that, weaken the protection it exists for. Needs
   its own decision: a separate, token-authenticated path exempt from the visitor rate limit (bear
   in mind `FPL_INTEL_REFRESH_TOKEN` is operator-only and reused elsewhere; a new dedicated
   read-token might be cleaner than overloading that one), vs. accepting the existing per-IP limiter
   as-is and treating any flakiness as acceptable for a best-effort scheduled job.
3. **Scope relative to #105**: #105's team-*list* discovery (which team_ids are opted into
   reminders) is a different shape of data than a single team's public recommendation -- a roster
   of who's opted in is arguably more sensitive than any one team's already-public lookup result.
   This plan doesn't resolve #105 by itself; worth deciding whether to fold a roster-read endpoint
   into this same effort (reusing whatever auth model gets chosen above) or keep it a fully
   separate follow-up.

## Not in scope

- Issue #101's scheduled-refresh trigger's own bootstrap-fetch design -- unaffected, see above.
- Deciding #105's team-list-discovery mechanism outright -- flagged as an open question (#3 above),
  not decided here.
- Any change to `_serve_dashboard`'s existing HTML-embedded response shape -- this is additive (a
  new JSON-returning read path), not a replacement for how the dashboard itself is served.

## Dependency

None remaining to *start* this (the code paths to reuse already exist and are well understood).
Whether it also resolves #105 depends on the answer to open question #3 above.
