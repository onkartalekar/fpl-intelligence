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

## Ask 1: scheduled/automatic refresh -- candidates

**(A) Railway cron service sharing the volume.** Would require a second Railway service in the
same project on a cron schedule. Since ask 1 doesn't need volume access at all (see above), this
adds real infrastructure (a second service to configure, monitor, and pay for) for no capability
a simple HTTP call doesn't already have. Not worth it.

**(B) A new scheduled GitHub Actions workflow calling `POST /api/refresh`.** Same shape as
`.github/workflows/deadline-reminder.yml`: `on: schedule`, a `curl`/small script hitting the
public Railway URL with `FPL_INTEL_REFRESH_TOKEN` from a GitHub secret. Zero new infrastructure,
reuses a pattern already proven in this exact repo, matches `SPECIFICATION.md`'s
externally-triggered-only posture, and sidesteps #105's constraint entirely since it's an HTTP
call, not a file read. GitHub's schedule cron is best-effort (documented delays of minutes under
load) -- fine here, since a refresh cadence has no deadline-precision requirement the way #55's
reminder does.

**(C) A third-party scheduler (cron-job.org, EasyCron, etc.) hitting the same endpoint.** Same
mechanism as (B) with a real third-party dependency and no offsetting benefit -- (B) is already
free, in-repo, and battle-tested.

### Recommendation: (B).

New workflow (not folded into `deadline-reminder.yml` -- different purpose, different cost
profile, keep them independently schedulable/disableable), calling `POST /api/refresh` with
`FPL_INTEL_REFRESH_TOKEN` as a repo secret, on a coarser cadence than the reminder's hourly check
-- proposing every 4 hours as a starting point (frequent enough to catch same-day price changes
and injury news without meaningfully increasing load on FPL's/the transfer sources' APIs, given
each run costs a real subprocess up to 5 minutes). This is a tunable default, not a researched
optimum -- easy to change once real usage shows whether 4h is too eager or too lax.

## Ask 2: a safe, self-serve visitor-triggered refresh -- findings and recommendation

Weighed against what ask 1 actually leaves unsolved: once a scheduled refresh runs every few
hours, the shared-data staleness window this issue opened with shrinks to "at most ~4 hours old,"
and per-team recommendations were never stale to begin with (see above). The remaining case ask 2
would serve is narrow: a visitor wants shared market data that's less than 4 hours old, right now,
badly enough to justify exposing a real, expensive, external-API-calling operation to public
unauthenticated traffic (global-cooldown-gated or not).

Building it would mean real new design surface the issue itself flags as unresolved: a new
open endpoint, a much longer global cooldown than the operator path's 90s (public traffic changes
the threat model the 90s cooldown was explicitly *not* designed for -- see its comment at
`server.py:50-56`), and the open question of whether to gate it to a registered/looked-up team
or leave it fully anonymous. None of that is hard, but it's all unnecessary if (B) above already
closes the gap that motivated it.

**Recommendation: decline, given ask 1 ships.** If (B) is built, ask 2 isn't solving a problem
that still exists at meaningful scale -- it would be new public attack surface on an expensive
endpoint, justified only by shrinking an already-small staleness window further. Worth building
later only if real usage shows 4-hourly staleness is genuinely a problem for visitors near a
deadline -- at that point, tightening ask 1's cadence (or scheduling extra runs around gameweek
deadlines specifically) is a smaller, safer change than opening a new public endpoint.

### If declined: text for `IMPLEMENTATION_PLAN.md`

```markdown
## Considered and declined -- self-serve visitor-triggered refresh endpoint (issue #101, <date>)

A new, unauthenticated `POST /api/refresh-request` endpoint (rate-limited by a long global
cooldown, coexisting with the existing `refresh_lock`) was considered as a safe way for any
visitor to request a shared-data refresh without reintroducing #28's leaked-token problem.
Declined once issue #101's automatic scheduled refresh (a GitHub Actions workflow calling the
existing operator-only `/api/refresh` every 4 hours) shipped: per-team decision-center output was
already always computed live (#46) and never subject to staleness, so the only thing a self-serve
trigger would freshen is the shared market data underneath it -- and a 4-hourly scheduled refresh
already bounds that staleness to a small window. Building a new public endpoint that calls a real,
external-API-hitting, up-to-5-minute operation to shrink an already-small window further wasn't
worth the added attack surface. Revisit if real usage shows the scheduled cadence is genuinely
insufficient near gameweek deadlines specifically -- tightening the schedule is the smaller change
to reach for first. See `plans/issue-101-refresh-trigger.md`.
```
