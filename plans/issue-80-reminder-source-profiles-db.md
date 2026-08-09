# Reminder recipients from `profiles.db`, not only the static secret (issue #80)

Researched 2026-08-09.

## Context

`scripts/send_deadline_reminder.py` (#55) sources its entire recipient list from a single
hand-edited `FPL_INTEL_REMINDER_TEAMS` GitHub Actions secret -- a deliberate scoping decision
made because no self-serve opt-in existed yet, and because `profiles.py`'s `email` column was
already reserved with a docstring pointing at exactly this follow-up. #79 is the still-open issue
that builds that opt-in (email field + T-3h/T-12h/T-24h picker on the Profile tab, writing
`email`/`reminder_enabled`/`reminder_lead_hours` into `profiles.db`). This issue is the next step:
make the reminder script actually read that data instead of (or alongside) the secret.

Issue #80's own body already names the hard question -- **how does a GitHub Actions runner read
`data/profiles.db`, which is gitignored and local-only?** -- and suggests the answer likely lives
in #27's cloud-hosting plan rather than a bespoke sync mechanism. This doc verifies that against
the current state of #79, #27, and the reminder script itself, and gives a recommendation.

## Structural constraints, verified

1. **`data/profiles.db` is genuinely inaccessible to a GitHub Actions runner today, with no
   workaround short of moving it.** `.gitignore` lines 235-237 exclude `data/profiles.db`,
   `data/profiles.db-wal`, and `data/profiles.db-shm`. A `schedule`-triggered workflow run gets a
   fresh `actions/checkout` of the repo on an ephemeral runner (`.github/workflows/deadline-reminder.yml`
   line 30, `runs-on: ubuntu-latest`) -- there is no persistent volume, no prior run's filesystem
   state, and the gitignored file was never committed in the first place. There is nothing to
   fetch even if the workflow tried; the data doesn't exist anywhere the runner can reach.

2. **#79 has not shipped, so there is nothing to read yet regardless of the access question.**
   Confirmed directly in `src/fpl_intel/profiles.py`: the `profiles` table has an `email` column
   (line 28) but no `reminder_enabled` or `reminder_lead_hours` columns, and `save_profile`'s
   docstring is explicit that `email` "is never written here -- it stays whatever it already was
   ... populated only by #55's explicit reminder opt-in" -- a write path that doesn't exist yet.
   `gh issue view 79` confirms it is still `OPEN`, with its own unresolved design question (does
   the opt-in write endpoint need double opt-in / a confirmation link before enabling recurring
   email to an arbitrary address). #80 is a hard, sequential blocker on #79 shipping first, not
   just a nice-to-have prerequisite.

3. **#27's plan already answers the access question, and the reminder script was already built
   anticipating that answer.** `plans/issue-27-cloud-hosting.md`'s "Decision so far" settles Phase
   2 storage on **"a single SQLite file (`data/profiles.db` or similar) ... the disk under it must
   be real"** -- i.e., wherever #27 ultimately lands the dashboard app (own hardware, a Hetzner/
   Lightsail VM, Fly.io/Railway with a mounted volume), `profiles.db` lives as an ordinary local
   file on that same box, not behind a separate database service. `scripts/send_deadline_reminder.py`'s
   own module docstring (lines 4-6) already says this out loud: "Trigger-agnostic by design ...
   [t]oday it is invoked hourly by [GitHub Actions]; if/when issue #27's hosted deployment lands,
   the host's own scheduler can invoke this unchanged." And `IMPLEMENTATION_PLAN.md`'s "Considered
   and declined" entry for #55 (2026-08-08) already rejected standing up dedicated cloud compute
   just for the reminder as "premature while issue #27's compute choice is deliberately open,"
   explicitly planning for the reminder to "migrate[] unchanged onto the #27 host's scheduler when
   that lands." Nothing new needs to be decided here -- #27 already decided it, twice, in
   independent docs written for different reasons.

4. **#27 itself is unstarted and substantial, and "Axis A" (who can reach the dashboard at all)
   has not shipped either.** `src/fpl_intel/server.py` still hard-raises `ValueError` if asked to
   bind anywhere but `127.0.0.1` (lines 515-516) -- today literally nobody but the machine running
   the process can reach the dashboard, let alone opt into reminders through it. #27's plan doc
   frames picking real hosting compute ("Axis B") as a step that comes only "after 1-2 ship" (the
   refresh-pipeline split and per-team storage, both already shipped as #46/#45) -- Axis B itself
   is still an open decision with no date attached, and Axis A (private-only vs. named-few vs.
   fully-public) gates it. This is a real, multi-phase, unstarted piece of work, not a rounding
   error.

## Candidate operationalizations

### A. Blocked-until-#27: delete the secret path, read `profiles.db` directly once the host is real (recommended)

Once #79 ships (opt-in data exists) and #27 lands the dashboard on real persistent compute (so
the reminder's invoking process and `data/profiles.db` are on the same box), `scripts/send_deadline_reminder.py`
changes from parsing `FPL_INTEL_REMINDER_TEAMS` to building `teams` from
`profiles.list_team_ids()` plus each row's `email` / `reminder_enabled` / `reminder_lead_hours`,
filtered to `reminder_enabled` rows. `.github/workflows/deadline-reminder.yml` is retired in favor
of whatever scheduler the #27 host runs (cron/systemd timer invoking the same script, matching the
"trigger-agnostic by design" framing already in the script's docstring). This is close to a
mechanical change at that point: `run()`'s signature already takes a plain `teams` list, so the
only work is a new `teams` constructor function reading SQLite instead of `os.environ`, plus
retiring the GitHub Actions workflow and its four SMTP/team-list secrets in favor of whatever the
host's own secret storage looks like.

**Cost of waiting:** none of substance. Until #27 ships, the dashboard has exactly one possible
recipient anyway (the person running it locally at `127.0.0.1`), who can already get reminders
today by hand-editing `FPL_INTEL_REMINDER_TEAMS` -- the exact workflow #55 was built for. No
capability is lost by waiting; the static secret already covers the only reachable audience.

### B. Stopgap: periodic export/sync of reminder-relevant columns into a GitHub secret/variable

Considered, as the issue itself asks to weigh. Shape: a small script (run manually, or by a
separate low-frequency workflow) reads `team_id`/`email`/`reminder_enabled`/`reminder_lead_hours`
from `data/profiles.db` and pushes a refreshed JSON blob into `FPL_INTEL_REMINDER_TEAMS` (or a new
secret) via `gh secret set`, so the existing GitHub Actions reminder keeps working without waiting
for #27.

**Rejected**, for three independent reasons, any one of which is sufficient:

1. **There is nothing to sync yet.** #79 hasn't shipped -- no `reminder_enabled`/`reminder_lead_hours`
   columns exist, and `email` is never written by any current endpoint. Building the sync mechanism
   now has no data to move.
2. **Even once #79 ships, there is nothing *else* to sync than what's already in the secret.**
   `server.py` binds only to `127.0.0.1` today, and that won't change until #27's Axis A (audience)
   ships. Until then, the only person who can ever load the Profile tab and opt in is the same
   person who already controls the `FPL_INTEL_REMINDER_TEAMS` secret directly. A sync mechanism
   built now would, in practice, sync exactly one row -- pure overhead over hand-editing the
   secret, which is what the current design already does.
3. **The sync step itself would have to run from wherever `profiles.db` actually lives today: the
   user's own local machine.** That reintroduces precisely the dependency `plans/issue-55-deadline-email-reminder.md`
   already ruled out by name -- "launchd and cron on the user's machine: ruled out by the explicit
   direction that the reminder be cloud-native with no local machine in the loop" (a local
   timer/manual step also inherits the machine's sleep/availability, so opt-ins go stale exactly
   when the laptop is closed). A "manual command, run occasionally" framing doesn't remove this --
   it just makes the staleness unpredictable rather than scheduled.

There's also a standing cost, independent of the three points above: any sync path adds a second
place email addresses are written to and transit through (a GitHub secret/variable, likely visible
in plaintext to anyone with repo-admin access, versus today's single hand-entered secret), for a
capability that -- per point 2 -- has no real recipient to serve before #27 ships anyway. Weighed
against #27 being "substantial" and unstarted (true, verified above), the stopgap still isn't
worth it, because its value only exists once *both* #79 has shipped *and* the dashboard is
reachable by someone other than its own operator -- and the second of those is itself gated by
#27's Axis A, so the stopgap can't actually get ahead of #27 by much even in principle.

## Recommendation

**Declare this issue blocked on #79 (hard prerequisite -- no data exists to read) and, for the
mechanism itself, on #27 (specifically Axis A / audience, not just Axis B / compute) -- do not
build a stopgap sync.** This is candidate A above. #27's own plan doc has already settled where
`profiles.db` will live once real hosting exists (an ordinary local file on whatever box runs the
app), and `scripts/send_deadline_reminder.py` was already written anticipating exactly this
migration path. The work that remains once both blockers clear is small and mechanical: swap the
`FPL_INTEL_REMINDER_TEAMS`-parsing constructor for one that reads `profiles.list_team_ids()` +
each row's `email`/`reminder_enabled`/`reminder_lead_hours`, and retire the GitHub Actions
workflow in favor of the host's own scheduler. No design work is left undone by waiting -- the
"how" is already answered; only the "when" (namely, after #79 and #27's audience decision ship)
remains.

Practically: leave #80 open and unassigned, with its existing "Blocked on #79 ... likely also
blocked on #27" label already accurate. Re-visit as a normal `/ship-issue` (not another
`/plan-issue`) once both #79 has shipped and #27 has landed a real host serving the dashboard to
more than just `127.0.0.1`.

## Considered and declined -- text for IMPLEMENTATION_PLAN.md

---

## Considered and declined — stopgap sync of `profiles.db` into a GitHub secret ahead of #27 (issue #80, 2026-08-09)

**Context:** GitHub issue #80 asked how `scripts/send_deadline_reminder.py` (#55) should read
opted-in recipients from `data/profiles.db` (built by #79) once a GitHub Actions runner can't
reach that gitignored, local-only file. Weighed building a periodic export of just the
reminder-relevant columns (`team_id`/`email`/`reminder_enabled`/`reminder_lead_hours`) into a
GitHub secret as a stopgap ahead of #27's hosting decision, versus simply waiting for #27.

**Findings:**
- #79 (the opt-in UI/write-path) has not shipped, so there is no data to sync yet.
- `src/fpl_intel/server.py` still hard-binds to `127.0.0.1` only -- until #27's audience question
  (Axis A) ships, the only person who can ever open the Profile tab and opt in is the same person
  who already controls `FPL_INTEL_REMINDER_TEAMS` directly, so a sync mechanism would move exactly
  one row versus hand-editing the secret as today's design already does.
- `plans/issue-27-cloud-hosting.md` already settled where `profiles.db` will live once real
  hosting exists (an ordinary file on whatever box runs the app, no separate database service),
  and `scripts/send_deadline_reminder.py`'s own docstring already anticipates migrating unchanged
  onto that host's scheduler -- the same conclusion `IMPLEMENTATION_PLAN.md`'s prior #55 "local and
  in-process triggers" entry (2026-08-08) already reached independently.
- Any sync step would have to run from the only place `profiles.db` currently exists -- the user's
  own machine -- reintroducing the "local machine in the loop" dependency #55's plan explicitly
  ruled out (sleep/availability makes opt-ins go stale unpredictably), while adding a second,
  less-audited hop that email addresses transit through.

**Decision: declined.** Wait for #79 (hard data prerequisite) and #27's audience rollout (the
actual capability gap), then swap the reminder script's `FPL_INTEL_REMINDER_TEAMS`-parsing
constructor for one reading `profiles.list_team_ids()` + each row's opt-in fields directly, and
retire the GitHub Actions workflow in favor of the host's own scheduler -- mechanical work once
both land, not a design gap to close early. Full analysis in `plans/issue-80-reminder-source-profiles-db.md`.

---
