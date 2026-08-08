# Email a transfer-deadline reminder with recommendations (issue #55)

Researched 2026-08-08. Issue: send an email a configurable number of
hours before each gameweek's transfer deadline (default ~3h) containing
the current transfer recommendations, so a "sometimes available" manager
(exactly what `config/user-profile.json` records:
`"deadline_availability": "sometimes"`) doesn't miss the window.

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
- **Config precedent**: reminder settings (recipient, lead-time hours,
  enabled flag) fit naturally in gitignored `config/user-profile.json`;
  secrets follow the existing env-var pattern (`FPL_INTEL_LLM_*` in
  `news_signals.py`).

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

### (a) launchd LaunchAgent on this Mac — recommended for now

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

### (d) GitHub Actions scheduled workflow — viable alternative, not recommended as the default

A `schedule:` workflow (hourly cron in GitHub's cloud, free on public
repos) checks the deadline, runs `scripts/refresh_dashboard.py`, and
sends via SMTP credentials in Actions secrets. This is the only option
short of #27 that is **immune to the laptop-asleep problem** — it fires
from GitHub's infrastructure regardless of the user's machine state.

Costs that make it the fallback rather than the default:
- The repo is public, so workflow run logs are public. The refresh
  pipeline prints diagnostics to stdout/stderr; every run's output
  would need auditing/muting to avoid leaking profile-adjacent detail.
- The manager profile (team id 364759, risk profile, recipient email)
  and SMTP credentials all move into GitHub Actions secrets — personal
  data leaves the machine and lives in a third party's secret store,
  a meaningful step for what is today a fully-local personal tool.
- GitHub's scheduled cron is best-effort (delays of several minutes to
  occasionally an hour under load are documented behavior), and
  scheduled workflows are auto-disabled after 60 days of repo
  inactivity.

If the sleep caveat of (a) proves unacceptable in practice, this is the
right escape hatch — and because the check script itself is
trigger-agnostic (see scope below), switching later means writing a
~30-line workflow file, not reworking the feature.

### (e) Tie to the cloud-hosting work (#27/#44–#46) — deferred, not blocked on

An always-on hosted deployment is the natural permanent home for a
scheduler. But #44/#45/#46 are all open and substantial (OAuth, SQLite
profile store, refresh-pipeline split), with no implementation started.
Sequencing the reminder behind them means no reminders for the
foreseeable future, for a feature the user wants now. Build the
reminder script trigger-agnostic so the hosted deployment can invoke
the identical script when #27 lands.

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
`FPL_INTEL_SMTP_PASSWORD`, matching the existing `FPL_INTEL_LLM_*`
pattern exactly. launchd plists carry env vars natively
(`EnvironmentVariables` dict), so the same mechanism serves both
interactive and scheduled invocation. Never in a tracked file.

## Proposed shape of the build (if direction (a) is confirmed)

1. **`scripts/send_deadline_reminder.py`** — the trigger-agnostic core.
   Reads reminder config from `config/user-profile.json` (new optional
   `"reminder"` section: `enabled`, `email`, `lead_hours` default 3);
   loads or refreshes state (refresh first via the existing
   `refresh_dashboard.py` machinery, fall back to cached
   `data/dashboard-state.json` with an explicit staleness line in the
   email if the network refresh fails); checks
   `now >= deadline - lead_hours` and `now < deadline`; consults a
   dedup log; composes a plain-text email from `decision_center`
   (transfer decisions when in-season, squad + captaincy for GW1 /
   `waiting_for_gw2`); sends via `smtplib`; records the send.
   Exit 0 with a "nothing to do" message outside the window — safe to
   run as often as the timer likes.
2. **Dedup log**: `data/reminder-log.json` (added to `.gitignore`
   alongside the other generated per-user files), keyed by event id,
   written with the existing `atomic_write_text`.
3. **launchd template**: a tracked `config/com.fpl-intel.reminder.plist.example`
   (hourly `StartCalendarInterval`, `EnvironmentVariables` placeholder)
   plus a README section with the two setup commands, mirroring the
   `.githooks` documentation pattern.
4. **README/SPECIFICATION touch-up**: amend the "No scheduler" line to
   record that the reminder script is the anticipated post-verification
   scheduling exception, external to the app, opt-in.
5. **Tests**: unit tests for window/dedup logic and email composition
   in both decision states (mock SMTP; no network).

## Recommendation

- **Build (a) launchd + (stdlib smtplib over Gmail SMTP)**, with the
  core script deliberately trigger-agnostic.
- **Decline (b) cron and (c) in-process thread** — drop-in text below.
- **Hold (d) GitHub Actions in reserve** — adopt only if (a)'s
  sleep-window caveat bites in practice; the script needs no changes.
- **Defer (e)** — revisit when #27's phases land; same script runs there.
- **(f) .ics backstop** — optional separate follow-up issue if wanted.

The genuine user decision here is (a) local-with-sleep-caveat vs.
(d) cloud-reliable-but-public-repo-tradeoffs as the *initial* trigger.
Everything else (email mechanism, credentials, dedup, script shape) has
a clear winner.

## Drop-in text for IMPLEMENTATION_PLAN.md (if declines are confirmed)

## Considered and declined — cron and in-process scheduling for the deadline reminder (issue #55, 2026-08-08)

For the transfer-deadline email reminder, two trigger mechanisms were
considered and declined in favor of a user-installed launchd
LaunchAgent invoking a trigger-agnostic script. **cron**: on macOS,
cron silently skips any run that falls while the machine is asleep
(this Mac sleeps; `pmset -g` confirms), whereas launchd coalesces
missed `StartCalendarInterval` runs and fires them on wake — for a
deadline-relative reminder, "late on wake" strictly beats "never."
**An in-process scheduler thread in `server.py`**: it would only be
alive while the local dashboard service happens to be running — the
inverse of the reliability a reminder needs — and it is the only
option that would put a timer inside the app itself, breaching the
externally-triggered-only architecture that keeps
SPECIFICATION.md's scheduling posture intact. Reconsider only if the
app becomes an always-on hosted service (issue #27), at which point
the same reminder script runs under the host's scheduler instead.
