# Issue #101 -- Automatic/self-serve refresh trigger for the hosted dashboard

## Context

Since #27/[PR #100](https://github.com/onkartalekar/fpl-intelligence/pull/100), `POST
/api/refresh` is operator-only, gated by `X-Refresh-Token` and never shipped to the browser --
the correct fix for #28's token-leak finding, but it means the shared market data (prices,
projections, ownership, injury/news status, fixture difficulty, official transfer records, model
performance -- everything in `dashboard-state.json`) now only updates when a human remembers to
`curl` the endpoint by hand. This session alone needed two manual refreshes after code deploys
before #120 fixed the unrelated stale-static-HTML half of that problem. The issue asks two
separable questions: (1) should the shared refresh run on some automatic cadence, and (2) should
any visitor be able to trigger one themselves, safely.

## Structural findings before evaluating candidates

**Ask 1 does not hit issue #105's volume-access wall.** #105 established that GitHub Actions
can't read `data/profiles.db` directly -- there's no shared filesystem between a GitHub-hosted
runner and Railway's persistent volume. But triggering a refresh doesn't require reading
anything from that volume: it's one `POST /api/refresh` HTTP call, and all the file I/O happens
server-side, inside the already-running Railway container. Any caller that can reach the public
URL and hold the token can trigger it -- a GitHub Actions runner is no different from the
operator's own laptop in this respect. This is a materially different problem from #105's, even
though both involve "GitHub Actions + Railway."

**A refresh is a real, non-trivial operation, not a cheap toggle.** `_default_refresh_action`
(`server.py:893-902`) shells out to `scripts/refresh_dashboard.py` with a 5-minute subprocess
timeout; `_refresh_project_unlocked` (`refresh.py:167`) calls the live FPL bootstrap/fixtures
APIs and scrapes/merges official transfer sources (`refresh.py:406`). This matters directly for
ask 2's risk calculus: it's nothing like the other open-write endpoints (`/api/profile`,
`/api/draft-squad`, `/api/contact`), which only touch local SQLite/log files.

**#28's "still-open rate limiting" item is no longer open.** Issue #101's own Dependency section
flags overlap with "#28's still-open rate limiting / abuse protection on the refresh endpoint,"
but that was closed by [#104](https://github.com/onkartalekar/fpl-intelligence/commit/92d8603)
before this issue was even filed: `/api/refresh` already has a 90-second **global** (not
per-source) `CooldownLimiter` (`_REFRESH_COOLDOWN_SECONDS`, `server.py:57`) plus the
pre-existing `refresh_lock` concurrency-of-1 (`server.py:1001`). Any new trigger path (scheduled
or self-serve) has a proven, in-repo template to reuse rather than a green-field design.

**Per-team decision-center output was never part of the staleness problem to begin with.** The
issue's own Context section already establishes this, and it's worth keeping sharp for evaluating
ask 2: `compute_manager_view()` calls `collect_public_manager(team_id)` live, on every page
load/lookup (#46) -- a visitor's own personalized recommendation is never frozen. Only the shared
market data underneath it (prices, fixtures, transfers, model performance) goes stale between
refreshes. That narrows exactly what "staleness" costs a visitor: not "my recommendation is old,"
but "the market inputs feeding it are up to N hours old."

**Direct in-repo precedent already settled the general trigger-mechanism question.**
`IMPLEMENTATION_PLAN.md`'s "Considered and declined -- local and in-process triggers for the
deadline reminder (issue #55, 2026-08-08)" ruled out local cron/launchd (inherits the operator's
machine's sleep schedule, and the explicit direction was cloud-native with no local machine in
the loop), an in-process scheduler thread in `server.py` (only alive while the dashboard happens
to be running, and breaches `SPECIFICATION.md`'s "the app never triggers its own actions"
posture -- see `SPECIFICATION.md:228`), and dedicated cloud compute (premature, zero-new-infra
GitHub Actions already does the job). All three reasons apply here nearly unchanged -- this
isn't a fresh design question, it's the same settled answer applied to a second scheduled job.

**#27's "keep on-demand, not cron" choice was a security fix, not a considered rejection of
automation.** Re-reading `plans/issue-27-cloud-hosting.md`'s 2026-08-10 addendum: the decision to
make `/api/refresh` operator-only was specifically to close the token-leaked-via-view-source hole
(#28's sharpest finding) -- it wasn't a judgment that automatic refresh is undesirable on the
merits. The issue itself asks to revisit this explicitly now that "on-demand" in practice means
"an operator has to remember," and the investigation above supports revisiting it: nothing about
keeping `/api/refresh` operator-only-and-token-gated conflicts with also triggering it on a
schedule from a trusted, secret-holding caller.

## Ask 1: scheduled/automatic refresh

### Revised requirement: deadline-relative, not flat-interval

The user wants fresh data at four specific checkpoints relative to each gameweek's own deadline
-- T-2d, T-1d, T-12h, T-3h -- not a flat "every N hours." This is a materially different
scheduling shape than a plain interval: the deadline moves every gameweek, so "am I at T-3h right
now" can't be answered by a fixed clock time, it needs to be computed against the live deadline
each time the check runs.

**This exact problem is already solved in this repo**, for the same reason, one lead-time at a
time: `scripts/send_deadline_reminder.py`'s `in_send_window(deadline_iso, now, lead_hours)`
(`send_deadline_reminder.py:266-269`) is a **stateless**, single-tick window check -- true for
exactly one hourly tick per gameweek, `(lead_hours - 1, lead_hours]` hours out. It already
supports multiple simultaneous lead times per run (`distinct_lead_hours`,
`send_deadline_reminder.py:899-903`, since different teams can have different
`reminder_lead_hours`). And critically, it resolves the deadline itself via a **live, unauthenticated
`fetch_bootstrap()` call** (`load_bootstrap_and_fixtures`, `send_deadline_reminder.py:225-244`,
with a cached-file fallback if that live call fails) -- so the caller never needs to trust
Railway's own (possibly stale) data to know when the next deadline is. The whole "am I in a
trigger window" decision is self-contained and requires nothing from the app being refreshed.

This directly resolves the GH-Actions-vs-Railway-cron question, addressed below.

### Candidates

**(A) Railway cron service sharing the volume.** To hit deadline-relative windows, this service
would still need to independently fetch live bootstrap data and run the same window-check logic
above -- volume access buys it nothing, since the deadline-resolution step doesn't touch the
volume either. So (A) either duplicates (B)'s exact mechanism on a second, separately-billed
Railway service for no benefit, or -- if it instead computed the check in-process on the same box
as the dashboard server -- reintroduces precisely the "in-process scheduler thread" pattern
`IMPLEMENTATION_PLAN.md` already declined for issue #55, breaching `SPECIFICATION.md`'s
never-self-triggering posture and tying trigger reliability to the dashboard process's own uptime
(a crash or redeploy near a window could silently miss it, where an independent GitHub Actions
run cannot).

**(B) A new scheduled GitHub Actions workflow, hourly, reusing `in_send_window`'s window-check
logic for four lead times (48, 24, 12, 3).** Same hourly cadence `deadline-reminder.yml` already
runs at (`cron: "0 * * * *"`), for the same reason: hourly ticks are the natural resolution for a
`(lead_hours - 1, lead_hours]`-style window check. Each tick: fetch live bootstrap (cheap, public,
no auth), resolve the next deadline, check whether now falls in any of the four windows, and if
so call `POST /api/refresh` with `FPL_INTEL_REFRESH_TOKEN`. Zero new infrastructure, reuses an
exact in-repo pattern, sidesteps #105's volume-access wall entirely (the window check needs no
Railway data at all, and the refresh trigger itself is just one HTTP call).

**(C) A third-party scheduler.** Same objection as before -- a real third-party dependency
buying nothing (B) doesn't already provide for free.

### Recommendation: (B), decisively over Railway cron.

Two implementation options worth deciding between, not four-way-open:

1. **A new standalone script** (e.g. `scripts/trigger_scheduled_refresh.py`) with its own copy of
   the window-check logic, run by a new workflow (e.g.
   `.github/workflows/scheduled-refresh.yml`).
2. **Factor `hours_until`/`in_send_window`/`next_unfinished_event`/`load_bootstrap_and_fixtures`
   out of `send_deadline_reminder.py`** into a small shared module (e.g.
   `fpl_intel/deadline_windows.py`), imported by both the reminder script and the new trigger
   script. Avoids duplicating the window-check logic in two places that would otherwise need to
   stay in sync by hand.

Leaning toward (2) -- it's a small, mechanical extraction (four pure functions, no behavior
change to the existing reminder), and duplicating deadline-window arithmetic across two scripts
is exactly the kind of drift that's cheap to prevent now and annoying to catch later. Worth
confirming before implementation, not a blocker to the overall recommendation.

Either way: a new workflow (not folded into `deadline-reminder.yml` -- different purpose,
independently schedulable/disableable), `on: schedule: cron: "0 * * * *"`, calling `POST
/api/refresh` with `FPL_INTEL_REFRESH_TOKEN` as a repo secret whenever the live-resolved deadline
falls in the T-2d/T-1d/T-12h/T-3h window. No new dedup-marker layer is needed the way the
reminder's has one (`REMINDER_LAST_EVENT_ID`) -- that marker exists to stop a human from being
emailed twice, a much worse failure than a refresh firing twice, and the existing 90-second
`_REFRESH_COOLDOWN_SECONDS` global cooldown on `/api/refresh` itself already absorbs any
duplicate call within the same hour for free.

## Ask 2: a safe, self-serve visitor-triggered refresh -- findings and recommendation

Weighed against what ask 1 actually leaves unsolved: once a deadline-relative scheduled refresh
runs at T-2d/T-1d/T-12h/T-3h, the shared-data staleness window this issue opened with shrinks to
"at most 12 hours old at any point, and 3 hours old right before the moment it matters most,"
and per-team recommendations were never stale to begin with (see above). The remaining case ask 2
would serve is narrower still: a visitor wants shared market data fresher than the nearest
scheduled checkpoint, right now, badly enough to justify exposing a real, expensive,
external-API-calling operation to public unauthenticated traffic (global-cooldown-gated or not).

Building it would mean real new design surface the issue itself flags as unresolved: a new
open endpoint, a much longer global cooldown than the operator path's 90s (public traffic changes
the threat model the 90s cooldown was explicitly *not* designed for -- see its comment at
`server.py:50-56`), and the open question of whether to gate it to a registered/looked-up team
or leave it fully anonymous. None of that is hard, but it's all unnecessary if (B) above already
closes the gap that motivated it.

**Recommendation: decline, given ask 1 ships.** If (B) is built, ask 2 isn't solving a problem
that still exists at meaningful scale -- it would be new public attack surface on an expensive
endpoint, justified only by shrinking an already-small staleness window further, and the window
is already narrowest exactly when it matters most (T-3h). Worth building later only if real usage
shows the deadline-relative cadence is genuinely insufficient -- at that point, adding a fifth
checkpoint (e.g. T-1h) is a smaller, safer change than opening a new public endpoint.

### If declined: text for `IMPLEMENTATION_PLAN.md`

```markdown
## Considered and declined -- self-serve visitor-triggered refresh endpoint (issue #101, <date>)

A new, unauthenticated `POST /api/refresh-request` endpoint (rate-limited by a long global
cooldown, coexisting with the existing `refresh_lock`) was considered as a safe way for any
visitor to request a shared-data refresh without reintroducing #28's leaked-token problem.
Declined once issue #101's automatic scheduled refresh (a GitHub Actions workflow calling the
existing operator-only `/api/refresh` at T-2d/T-1d/T-12h/T-3h before each gameweek deadline)
shipped: per-team decision-center output was already always computed live (#46) and never subject
to staleness, so the only thing a self-serve trigger would freshen is the shared market data
underneath it -- and the deadline-relative schedule already bounds that staleness to at most 3
hours at the moment it matters most. Building a new public endpoint that calls a real,
external-API-hitting, up-to-5-minute operation to shrink an already-small window further wasn't
worth the added attack surface. Revisit if real usage shows the scheduled checkpoints are
genuinely insufficient -- adding a finer-grained checkpoint is the smaller change to reach for
first. See `plans/issue-101-refresh-trigger.md`.
```
