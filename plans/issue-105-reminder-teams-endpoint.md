# Issue #105 -- Non-manual access to profiles.db's opted-in reminder teams

## Context

`send_deadline_reminder.py`'s `collect_teams()`/`load_teams_from_profiles_db()`/
`resolve_profiles_db_path()` already do exactly what issue #80 asked for: a zero-manual-list mode
that reads every `reminder_status == 'enabled'` team straight out of `profiles.db`. The blocker
has never been the script's logic -- it's that `resolve_profiles_db_path()` resolves to a **local
filesystem path** (`<root>/data/profiles.db`), and `.github/workflows/deadline-reminder.yml` runs
on a throwaway GitHub Actions VM with no shared filesystem with Railway's persistent volume, where
that file actually lives. Same root cause as #101/#122/#125, all resolved in this session already
by having GitHub-Actions-hosted scripts read Railway's live state over HTTP instead of the local
disk -- #105 was deliberately left out of #125's scope (`plans/issue-125-single-source-of-truth.md`,
decision #3) because a *roster* of every opted-in manager's email is a more sensitive shape of data
than any one already-public team's lookup result, and deserved its own explicit decision.

The issue itself already narrowed this to two candidates and asked for a `/plan-issue` pass rather
than picking blind:

- **(A)** Railway cron/service sharing the dashboard's volume -- viability unverified.
- **(B)** A new authenticated `GET /api/reminder-teams` endpoint, JSON, computed live by the one
  process that already has legitimate `profiles.db` access.

## Structural findings before evaluating candidates

**#125 already built and proved the exact pattern (B) needs, twice.** `/api/shared-state` and
`/api/manager-view` (`server.py:1082-1123`) established: a read endpoint gated by
`_rate_limit_exempt()`'s `X-Refresh-Token` check, a `_json` response helper, and
`send_deadline_reminder.py`'s own `fetch_shared_state`/`fetch_manager_view` HTTP-fetch pattern
(`urlopen` + `Request`, `HTTPError`/`URLError`/`OSError`/`ValueError` caught per-call). A third
endpoint of the same shape is now a small, mechanical addition, not new architecture -- this
session already committed to "read endpoints over shared filesystem" as the house pattern for
every GitHub-Actions-hosted script (#101, #122, #125 all trace through it), and (B) is a direct
continuation of that, not a fresh decision.

**(A)'s premise conflicts with a decision this repo already made and documented.**
`IMPLEMENTATION_PLAN.md`'s "Considered and declined" section (referenced again in #101's plan)
already declined moving scheduling *off* GitHub Actions and onto Railway once, for issue #55's
reminder itself, specifically to avoid tying reliability to a second billed Railway service and an
in-process/platform-specific scheduling mechanism. (A) reintroduces exactly that shape (a second
Railway service, this time for volume-sharing rather than scheduling) to solve a problem (B) already
solves without it. It also carries a real unresolved unknown -- whether Railway's volumes are
genuinely shareable across services at all -- that (B) never has to answer.

**The PII surface here is a superset of what #79/#125 already established filtering for, not a new
kind of exposure.** `load_teams_from_profiles_db()` (`send_deadline_reminder.py:137-157`) reads
`team_id`, `email`, `reminder_lead_hours` for every `reminder_status == 'enabled'` row -- the same
`email`/`reminder_lead_hours` fields #79 already flagged as sensitive-enough-to-filter from the
`?team_id=` explicit-lookup path (`server.py:1193-1200`), just returned in bulk across every
opted-in team instead of one team's own cookie-resolved view. This is exactly the shape the issue
itself calls out: "the auth model isn't optional hardening here, it's a hard requirement." Unlike
`/api/shared-state` (no rate limit, no PII) and `/api/manager-view` (rate-limited, PII-free by
construction), `/api/reminder-teams` must be **token-gated unconditionally** -- there's no
unauthenticated version of this endpoint that's safe to expose at all, unlike the other two.

## Candidate operationalizations

**(A) Railway cron/service sharing the dashboard's volume.** Declined -- see above: unverified
platform capability, reintroduces a scheduling/infra shape this repo already declined once for the
identical class of problem, and doesn't reuse any of the pattern #125 already built and proved.

**(B) `GET /api/reminder-teams`, token-gated, returning the opted-in roster as JSON.** Mirrors
`_handle_shared_state`/`_handle_manager_view`'s shape exactly: a new handler method building the
list via `profiles.list_team_ids` + `profiles.load_profile` (the same filter
`load_teams_from_profiles_db` already applies -- `reminder_status == 'enabled'` and a non-empty
`email` -- moved server-side so the script no longer needs the filtering logic itself, only the
HTTP call), gated unconditionally on `X-Refresh-Token` (**no** anonymous path at all, unlike
`/api/manager-view`'s rate-limit-exemption-only use of the token -- here a missing/invalid token
must 403, not fall through to a public but throttled response, since there is no safe public
response for this data). `send_deadline_reminder.py`'s `collect_teams`/`load_teams_from_profiles_db`/
`resolve_profiles_db_path` are replaced by one `fetch_reminder_teams(base_url, token)` HTTP call,
following the exact `fetch_shared_state`/`fetch_manager_view` pattern already in the file. The
`FPL_INTEL_REMINDER_PROFILES_DB` env var's existing truthy-sentinel/explicit-path semantics become:
still opt-in (unset = disabled, matching every other optional source in this script), but when
enabled, source from the endpoint instead of a local path -- `FPL_INTEL_REFRESH_TOKEN` (already
required by #125 for `/api/manager-view`) is reused, no new secret.

## Recommendation: (B).

(A) is not a real option for the reasons above -- it's both unverified and a direct repeat of an
infra shape already declined once for this exact script. (B) is the only candidate that reuses
#125's now-proven pattern rather than inventing a new one, closes the gap with a small, mechanical
addition (one endpoint, one fetch function, no new secret), and is consistent with every other
architectural decision made this session for this class of problem.

## Open question for the user: token reuse vs. a dedicated token

The issue itself flags this as unresolved, and it's a real tradeoff, not a formality:

- **Reuse `FPL_INTEL_REFRESH_TOKEN`** (what #125 already did for `/api/manager-view`'s rate-limit
  exemption): one fewer secret to provision/rotate across Railway + two GitHub Actions workflows.
  But this token now gates three different things of different sensitivity -- triggering a live
  refresh, exempting a single public lookup from rate-limiting, and (if reused here) revealing
  every opted-in manager's email address in bulk. A leak of one token exposes all three.
- **Mint a dedicated `FPL_INTEL_REMINDER_TEAMS_TOKEN`** (or similar): a `/api/refresh` token leak
  (e.g. accidentally logged, or a compromised GH Actions run) no longer also hands over the entire
  opted-in roster's email addresses. Costs one more secret to provision on Railway and add to
  `deadline-reminder.yml`'s existing secrets list.

I'd lean towards the dedicated token, given the issue's own emphasis that this is real PII in bulk
(strictly worse than any single leaked token exposing one team's already-public lookup), but this
is the kind of call that's genuinely the user's to make, not mine to default on.

## Decided (2026-08-11)

**Dedicated token.** A new `FPL_INTEL_REMINDER_TEAMS_TOKEN` env var/secret gates
`/api/reminder-teams` unconditionally -- distinct from `FPL_INTEL_REFRESH_TOKEN`, so a leak of
either token compromises only what that token actually gates (refresh-triggering +
`/api/manager-view` rate-limit exemption on one side, the bulk opted-in roster on the other), never
both at once. Provisioned on Railway and added to `deadline-reminder.yml`'s existing secrets list
alongside the two #125 already added.

## Not in scope

- Any change to `collect_teams()`'s union-by-`team_id` behavior with `FPL_INTEL_REMINDER_TEAMS`, or
  to `load_teams_from_profiles_db`'s filter semantics -- both already correct, only their data
  *source* moves from local disk to HTTP.
- Whether the reminder job should eventually run on GitHub Actions or Railway cron once it has real
  data access -- explicitly out of scope per the issue itself.
- Re-enabling `deadline-reminder.yml` or configuring its production secrets -- that's an
  operational step for the user once this ships, not part of this issue.

## Dependency

None remaining -- #27 and #80 (both prerequisites named in the issue) are shipped, and #125's
read-endpoint pattern (this plan's main technical dependency) is merged.
