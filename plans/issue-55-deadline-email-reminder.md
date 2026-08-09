# Email a transfer-deadline reminder with recommendations (issue #55)

Researched 2026-08-08, re-planned 2026-08-08 (later same day). Issue:
send an email a configurable number of hours before each gameweek's
transfer deadline (default ~3h) containing the current transfer
recommendations, so a "sometimes available" manager doesn't miss the
window.

## Re-plan note (2026-08-08)

The first pass of this plan (below) was written against the
then-current single-team `config/user-profile.json` model. Since then,
issues #61/#62/#64 landed a per-team SQLite profile store
(`src/fpl_intel/profiles.py`, `data/profiles.db`, gitignored/local-only)
and — critically — issue #46's `refresh.compute_manager_view(bootstrap,
fixtures, transfers, generated_at, team_id, ...)` now computes any
team's personalized `weekly_decisions` at request time, decoupled from
the single shared `dashboard-state.json` refresh. That function is a
strict upgrade over the original plan's approach (invoking
`scripts/refresh_dashboard.py` for one hardcoded team): the reminder
script can now fetch bootstrap/fixtures/transfers **once** per run and
call `compute_manager_view` per recipient team, so supporting several
teams in one workflow run is nearly free.

Two things this upgrade does **not** change, checked explicitly so this
doesn't over-claim multi-tenancy it isn't building:
- `profiles.py` already reserves an `email` column with a docstring
  pointing at this exact issue ("populated only by #55's explicit
  reminder opt-in") — but there is no public write path to it yet (no
  self-serve signup UI/endpoint exists, and none is proposed here: every
  current mutating endpoint, e.g. `/api/profile` and `/api/draft-squad`,
  is unauthenticated-but-rate-limited on a publicly-known team ID, which
  is an acceptable trust model for team-scoped *preference* data but
  not for "cause a real inbox to receive recurring email," a
  fundamentally different abuse surface gated by the *email address*,
  not the team ID). Building that self-serve flow (plus the double
  opt-in / confirmation-link step it would need to avoid becoming a
  spam vector) is out of scope for this issue and is better sequenced
  after #27/#28 settle the hosting and security posture. This build
  stays admin/secrets-configured, matching the original plan's
  single-operator scope (this is currently a personal tool for one
  user), just widened to a *list* of teams instead of exactly one.
- `data/profiles.db` is local-only and gitignored; a GitHub Actions
  runner still cannot read it (this was already true of
  `config/user-profile.json` in the original plan — nothing regresses
  here, the recipient list was always going to be a manually-mirrored
  secret, not a live read of local state).

**Net change to the build:** replace the single `FPL_INTEL_USER_PROFILE_JSON`
secret (one team's full profile-JSON contents) with `FPL_INTEL_REMINDER_TEAMS`
(a JSON list, one object per recipient team:
`{"team_id": ..., "email": ..., "lead_hours": 3}`, `lead_hours` optional,
defaulting to 3). The script fetches bootstrap/fixtures/transfers once,
then loops the list calling `compute_manager_view` per team — this is
the only structural change; every trigger-mechanism, dedup, and
log-hygiene finding below is unaffected by the schema change and still
holds.

## Context

Everything the email needs already exists as computed output — this is
purely a new delivery channel plus a trigger:

- **Deadline**: `state.fpl.next_deadline` (e.g. `2026-08-21T17:30:00Z`
  in the current cached state) comes from the FPL bootstrap's
  `events[].deadline_time` via `summarize_bootstrap()`
  (`src/fpl_intel/fpl_data.py:16`). The bootstrap carries deadlines for
  **all 38 gameweeks up front**, not just the next one.
- **Recommendations**: `state.decision_center` already holds
  `recommended_squad` (with `starting_xi`, `captain`, `vice_captain`,
  `selection_rationale`), `captaincy` (top-5 with projections), and
  `weekly_decisions`. Note `weekly_decisions` is currently
  `{"status": "waiting_for_gw2", "reason": "Transfer recommendations
  begin at Gameweek 2."}` — the email composer must handle both the
  GW1 squad-selection state and the in-season transfer-decision state.
- **Headless refresh**: `scripts/refresh_dashboard.py` already does a
  full lock-aware network refresh with no browser involved — a reminder
  script can call the same entry point to guarantee the emailed
  recommendations are current as of send time, not as of whenever the
  user last clicked Refresh.
- **Config precedent**: reminder settings (recipient team(s), lead-time
  hours) are admin-configured via a GitHub Actions secret (see re-plan
  note above), not the gitignored per-team `data/profiles.db` or the
  single-team `config/user-profile.json`; SMTP secrets follow the
  existing env-var pattern (`FPL_INTEL_LLM_*` in `news_signals.py`).

## Structural findings before evaluating candidates

**The "no scheduler" principle is phase-scoped, not permanent.**
README.md:111 says "No scheduler is configured, by explicit choice" —
but the underlying decision, SPECIFICATION.md:216, reads: "No scheduler
is created in this foundation phase. **Scheduling will be considered
only after an interactive refresh has been verified.**" The interactive
refresh (Refresh button → `/api/refresh`) has been built and verified
for a while. So this feature is not a violation of the principle; it is
the moment the principle itself said scheduling could be revisited.
Additionally, every candidate below keeps the scheduler **outside the
app**: `server.py` and the refresh pipeline stay untouched and still
never act on their own — only an opt-in reminder script, invoked by an
external timer, does.

**This machine sleeps.** `pmset -g` shows `sleep 1` (display-idle sleep
after ~1 min, currently prevented only while powered/active). Any
purely-local timer can therefore fire late: if the Mac is asleep at
deadline-minus-3h, the best a local mechanism can do is fire on next
wake. This is the honest ceiling on local reliability and the reason
the issue body flagged hosting (#27) as the natural long-term home.

**The repo is public.** Relevant to the GitHub Actions candidate below:
Actions secrets are supported and encrypted, but **workflow run logs on
a public repo are publicly visible**, and the manager profile
(team id, risk profile) would have to live in Actions secrets/variables
rather than the gitignored local file.

## Candidate trigger mechanisms

> **Direction change (2026-08-08):** after the first presentation of
> this plan (which recommended (a) launchd locally), the user redirected:
> *"think of this as a cloud native solution — don't worry about local
> app, it is not needed locally anyway."* Local-machine triggers (a)/(b)
> are therefore out by direction, not by technical failure; the analysis
> below is kept for the record. The recommendation now centers on (d)
> GitHub Actions as the cloud-native trigger available today, converging
> on (e) the hosted deployment when #27 lands.

### (a) launchd LaunchAgent on this Mac — declined by user direction

A user-installed `~/Library/LaunchAgents/com.fpl-intel.reminder.plist`
with `StartCalendarInterval` firing an hourly check script. The script:
is a deadline within `lead_hours` of now, and has this event's reminder
not been sent yet (dedup log)? If so: refresh, compose, send.

- launchd is the macOS-native mechanism (the user already has Google
  updater agents in `~/Library/LaunchAgents/`). Unlike cron, launchd
  **coalesces missed `StartCalendarInterval` runs and fires them on
  wake** — so a Mac asleep at reminder time still sends the reminder
  the moment it wakes, rather than silently skipping it.
- Zero new dependencies, zero new services, zero cloud footprint.
- Limitation (must be documented honestly): if the Mac stays asleep
  from deadline-minus-3h through the deadline itself, the email arrives
  too late or not at all. "Fires on wake" mitigates but cannot
  eliminate this.
- One-time setup by the user (copy a provided plist template, run
  `launchctl load`), matching the repo's existing pattern of documented
  one-time setup steps (`git config core.hooksPath .githooks`).

### (b) cron on this Mac — declined

Strictly dominated by (a) on macOS: cron silently skips runs that fall
during sleep (no coalescing), and Apple has deprecated cron in favor of
launchd for years. `crontab -l` confirms no existing crontab either, so
there's no incumbent to match. No advantage over (a) on any axis.

### (c) In-process scheduler thread in `server.py` — declined

Only alive while the local dashboard service happens to be running,
which is exactly the wrong reliability profile for a deadline reminder
(the point is to be reachable when the user *isn't* engaged with the
dashboard). Also the only candidate that would actually breach the
architecture (a timer inside the app). The issue body already
identified this weakness; investigation confirms nothing rescues it.

### (d) GitHub Actions scheduled workflow — recommended (the cloud-native trigger available today)

A `schedule:` workflow (hourly cron in GitHub's cloud, free with
unlimited minutes on public repos) checks the deadline, runs the
existing headless refresh, and sends via SMTP credentials held in
Actions secrets. It is fully cloud-native — nothing depends on any
local machine being awake or even existing — and it runs the repo's
Python code exactly as-is (stdlib-only, so no dependency install step
beyond checking out the repo and picking a Python).

Concrete design:

- **Trigger**: `on: schedule: cron: "0 * * * *"` (hourly) plus
  `workflow_dispatch` for manual test sends. GitHub's cron is
  best-effort — documented delays of minutes (occasionally longer)
  under load — which an hourly cadence against a 3-hour lead absorbs
  comfortably.
- **Secrets** (encrypted; GitHub auto-masks their values in run logs):
  - `FPL_INTEL_SMTP_HOST` / `FPL_INTEL_SMTP_PORT` /
    `FPL_INTEL_SMTP_USER` / `FPL_INTEL_SMTP_PASSWORD` — the Gmail app
    password setup below.
  - `FPL_INTEL_REMINDER_TEAMS` — JSON list of recipients, e.g.
    `[{"team_id": 123456, "email": "onkar.talekar@gmail.com",
    "lead_hours": 3}]`. Replaces the original single-team
    `FPL_INTEL_USER_PROFILE_JSON` design (see re-plan note above): each
    entry is resolved independently via `compute_manager_view`, so the
    same workflow run covers every configured team from one shared
    bootstrap/fixtures/transfers fetch.
- **Public-repo log hygiene**: workflow run logs are publicly visible,
  so the workflow redirects the refresh/send output to a file and
  prints only a generic status line ("checked: outside window" /
  "reminder sent for GW<N>"). Note the underlying exposure is modest —
  an FPL team id is already publicly queryable via the FPL API — but
  the recipient email and credentials must never be printed, and
  secret-masking plus muted output covers that with margin.
- **De-duplication without a persistent runner**: Actions runners are
  ephemeral, so `data/reminder-log.json` can't live there. Two layers:
  1. *Stateless window*: send only when
     `lead_hours - 1 < (deadline - now)/1h <= lead_hours` — exactly one
     hourly tick falls in that band, so duplicate sends can only happen
     if GitHub fires the same tick twice (it doesn't).
  2. *Belt-and-braces marker*: a repo Actions variable
     (`REMINDER_LAST_EVENT_ID`, updated via `gh variable set` with the
     workflow's `GITHUB_TOKEN` granted `actions: write`) checked before
     sending. If the variable write turns out to need more permission
     than `GITHUB_TOKEN` allows, layer 1 alone is sufficient and layer
     2 is dropped — to be verified live at ship time, not assumed.
  The stateless band has one honest failure mode: if GitHub *drops*
  (not delays) the one in-band tick, that gameweek's reminder is
  missed. Mitigation if wanted later: widen the band to 2 ticks and
  rely on layer 2 for dedup.
- **Known platform caveat**: scheduled workflows are auto-disabled
  after 60 days without repo activity. This repo is very active;
  if that ever changes, GitHub emails a warning before disabling.

### (d′) Other standalone cloud schedulers (Fly.io scheduled machine, small VM cron, Cloudflare Workers) — declined for now

Standing up dedicated compute just for an hourly check is premature
while #27's Axis B compute choice is deliberately still open — it would
pre-empt that decision for the smallest workload in the system.
Cloudflare Workers additionally can't reasonably run the existing
refresh pipeline (it's a full Python/stdlib program, not a
Workers-shaped function). GitHub Actions gives the same always-on
property with zero new infrastructure; when #27 picks real compute,
(e) supersedes both.

### (e) Tie to the cloud-hosting work (#27/#44–#46) — the eventual home, not blocked on

An always-on hosted deployment is the natural permanent home for the
scheduler, and with the cloud-native direction confirmed it is where
this converges: once #27 picks compute, the host's own scheduler
(e.g. a Fly.io scheduled machine or plain cron on the chosen box)
invokes the identical trigger-agnostic script and the GitHub Actions
workflow is deleted. But #44/#45/#46 are all open and substantial
(OAuth, SQLite profile store, refresh-pipeline split) with no
implementation started — sequencing the reminder behind them means no
reminders for the foreseeable future. (d) delivers the cloud-native
property now with zero new infrastructure and a clean migration path.

### (f) Season calendar file (.ics) — optional zero-scheduler complement, out of scope here

Since the bootstrap carries all 38 deadlines up front, a refresh could
also emit `data/fpl-deadlines.ics` with a `VALARM` at deadline-minus-3h
per gameweek; imported once into Google/Apple Calendar, it produces
reliable phone-native alerts with **no scheduler, no email credentials,
no wake dependency at all**. It cannot carry current recommendations
(static content), so it doesn't satisfy this issue by itself — but it's
a near-free reliability backstop. Noted for a possible follow-up issue;
not part of this build.

## Email mechanism

**stdlib `smtplib` over SMTP — recommended.** Preserves the repo's
zero-third-party-dependency property outright. The user's address is a
Gmail address, and Gmail's SMTP (`smtp.gmail.com:587`, STARTTLS) works
with a one-time **app password** (requires 2FA on the Google account) —
no new account, no sender-domain verification, sending from the user to
the user.

**HTTP email API (Resend/SendGrid/etc.) — declined.** Callable via
stdlib `urllib` so it wouldn't technically add a code dependency, but
it adds a new third-party account, sender verification, and an API key
— all to send one email to oneself that Gmail SMTP already handles.
No axis on which it wins for this use case.

**Credentials:** environment variables `FPL_INTEL_SMTP_HOST` /
`FPL_INTEL_SMTP_PORT` / `FPL_INTEL_SMTP_USER` /
`FPL_INTEL_SMTP_PASSWORD` / `FPL_INTEL_REMINDER_EMAIL`, matching the
existing `FPL_INTEL_LLM_*` pattern exactly. In CI they are populated
from GitHub Actions secrets; on the future #27 host, from the host's
secret store. Never in a tracked file, never printed.

## Proposed shape of the build (if direction (d) is confirmed)

1. **`scripts/send_deadline_reminder.py`** — the trigger-agnostic core,
   identical no matter what timer invokes it (Actions today, the #27
   host's scheduler later). Parses `FPL_INTEL_REMINDER_TEAMS` (JSON
   list, `lead_hours` per entry defaulting to 3) and SMTP settings from
   the `FPL_INTEL_*` env vars; determines the next unfinished
   gameweek's deadline from a fresh `fetch_bootstrap()` (falling back to
   cached `data/fpl-bootstrap-latest.json` with an explicit staleness
   line in the email if the network fetch fails); applies the
   send-window check once per gameweek deadline (not per team, so all
   configured teams for the same in-window deadline share one
   bootstrap/fixtures/transfers fetch); for each team in-window, calls
   `refresh.compute_manager_view(...)` — falling back to
   `decision_center`'s `recommended_squad`/`captaincy` for the GW1 /
   `waiting_for_gw2` state, same as `weekly_decisions.status` already
   distinguishes elsewhere — composes one plain-text email per team, and
   sends via `smtplib`; exits 0 with a quiet "outside window" message
   otherwise — safe to run as often as the timer likes. A `--dry-run`
   flag prints the composed email(s) instead of sending, for the
   `workflow_dispatch` test path and local debugging.
2. **`.github/workflows/deadline-reminder.yml`** — hourly `schedule` +
   `workflow_dispatch`; passes `FPL_INTEL_REMINDER_TEAMS` and the SMTP
   secrets straight through as env vars (no local file to write —
   `data/profiles.db` is never touched by the workflow); runs the
   script with output muted per the log-hygiene design above; layer-2
   dedup marker if the `GITHUB_TOKEN` permission proves sufficient when
   verified live.
3. **README/SPECIFICATION touch-up**: amend the "No scheduler" line to
   record that the reminder workflow is the anticipated
   post-verification scheduling exception — external to the app,
   opt-in, and slated to move onto the #27 host's scheduler.
4. **Tests**: unit tests for the send-window arithmetic and email
   composition in both decision states (mock SMTP; no network), plus a
   live `workflow_dispatch --dry-run` run in CI as the ship-time
   verification that the workflow, secrets wiring, and log hygiene all
   actually work.

## Recommendation

- **Build (d): GitHub Actions hourly workflow + trigger-agnostic
  `send_deadline_reminder.py` + stdlib `smtplib` over Gmail SMTP.**
  Cloud-native today with zero new infrastructure; nothing local in the
  loop.
- **Decline (a) launchd and (b) cron** — local-machine triggers, ruled
  out by the cloud-native direction (and (b) was dominated anyway).
- **Decline (c) in-process thread** — wrong reliability profile and the
  only option that breaches the app's externally-triggered-only
  architecture.
- **Decline (d′) dedicated cloud compute for now** — don't pre-empt
  #27's open compute decision for the system's smallest workload.
- **(e) is the migration target, not a blocker** — when #27 lands, the
  host's scheduler invokes the same script and the workflow is deleted.
- **(f) .ics backstop** — optional separate follow-up issue if wanted.

## Decision so far

- **Cloud-native trigger confirmed by the user (2026-08-08)**: build
  this so no local machine is in the loop; the original launchd-local
  recommendation is superseded. GitHub Actions (d) is the recommended
  concrete mechanism pending the user's confirmation to implement.

## Drop-in text for IMPLEMENTATION_PLAN.md (if declines are confirmed)

## Considered and declined — local and in-process triggers for the deadline reminder (issue #55, 2026-08-08)

For the transfer-deadline email reminder, three trigger mechanisms
were considered and declined in favor of a scheduled GitHub Actions
workflow invoking a trigger-agnostic script. **launchd and cron on
the user's machine**: ruled out by the explicit direction that the
reminder be cloud-native with no local machine in the loop (a local
timer also inherits the machine's sleep schedule — cron silently
skips runs during sleep; launchd merely fires them late on wake).
**An in-process scheduler thread in `server.py`**: only alive while
the dashboard service happens to be running — the inverse of the
reliability a reminder needs — and the only option that would put a
timer inside the app itself, breaching the externally-triggered-only
architecture behind SPECIFICATION.md's scheduling posture.
**Dedicated cloud compute (VM/Fly machine) just for the reminder**:
premature while issue #27's compute choice is deliberately open;
GitHub Actions provides the always-on property with zero new
infrastructure, and the reminder script migrates unchanged onto the
#27 host's scheduler when that lands.
