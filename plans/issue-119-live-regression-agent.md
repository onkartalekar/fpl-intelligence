# Issue #119 -- Live regression-test agent for the hosted Railway app

## Context

Confirmed the issue's core claim: nothing today exercises the real deployed origin
(`https://web-production-1b285.up.railway.app/`) end-to-end. `tests/*.py` runs entirely
in-process; `.claude/skills/verify-dashboard/SKILL.md` explicitly targets a locally-started
server. The motivating incident (Railway's outbound-IPv6 route breaking `smtplib` -> `smtp.gmail.com`,
fixed earlier this session) passed every existing check and only reproduced inside the real
deployed container -- a live-environment-only failure mode by definition, which no amount of
local coverage can catch.

The issue itself leaves three real design questions open rather than assuming answers. This plan
addresses each.

## Structural findings

**This repo already has the exact scaffolding this needs, twice over.** `.github/workflows/
scheduled-refresh.yml` (issue #101) and `.github/workflows/deadline-reminder.yml` (issue #55/#125)
are both: a GitHub Actions cron job, calling the live Railway origin over HTTP, with a `dry_run`/
`workflow_dispatch` input for on-demand runs, secrets for anything sensitive, and a Python script
under `scripts/` doing the real work (`scripts/trigger_scheduled_refresh.py`, `scripts/
send_deadline_reminder.py`). A live-regression check is the same shape a third time: cron +
`workflow_dispatch` + a `scripts/` Python script hitting the real origin, not new infrastructure.

**"Agent" in the issue title doesn't imply an LLM-driven agent.** Every other script in this
codebase that runs unattended against live infrastructure (`trigger_scheduled_refresh.py`,
`send_deadline_reminder.py`) is a deterministic Python script with plain assertions, not an LLM
call -- consistent with `SPECIFICATION.md`'s scoping of LLM use to the one explicitly-gated
Phase 5 news-signal extractor, nowhere else. A deterministic script here is cheaper, faster,
produces a stable pass/fail with no model-output variance to account for, and is the pattern
already established twice. Recommended: build it as `scripts/live_regression_check.py`, not an
LLM-driven agent.

**The empty-state gating check (issue #108) needs two distinct requests, not two distinct visitors.**
`decisions-empty-state`/`performance-empty-state` key off whether a `team_id` is resolved
(cookie or query param) -- so "pre-profile" is simply a request with neither, and "post-profile"
is `?team_id=<a real, already-public team>`. No account/session state needs to be created to
exercise both branches; a public team ID already covers "populated," and an absent one already
covers "empty."

## Candidate answers to the issue's three open questions

### (a) How does the agent verify email-dependent flows?

**The two email-sending endpoints behave oppositely on SMTP failure -- checked both directly, and
this asymmetry is the whole answer to this question, not a detail.**

- `/api/reminder-opt-in`'s `"enable"` action (`_default_reminder_opt_in_action`, `server.py:690-705`):
  sends the confirmation email *synchronously* and re-raises any `ReminderEmailError` as
  `ReminderOptInSendError`, which `_handle_reminder_opt_in` (`server.py:1478-1506`) turns into an
  error response to the caller. **(ii) -- asserting only the API response -- already catches an
  SMTP failure here.**
- `/api/contact` (`_default_contact_action`, `server.py:816-850`): **deliberately swallows**
  `ReminderEmailError` and always returns `{"status": "ok"}` -- this is not an oversight, it's
  issue #110's durability-backstop design (`plans/issue-110-contact-us-tab.md:93`: "a submission
  ... fails to send ... shouldn't just vanish"), so a visitor's feedback is never lost just because
  SMTP is broken. **(ii) is blind to an SMTP failure on this endpoint by construction** -- and
  this is the exact endpoint (Contact Us) whose real SMTP failure motivated this issue in the
  first place. A live check that only asserts `{"status": "ok"}` on `/api/contact` would have
  passed straight through the actual incident this issue exists to prevent.

Given that, the options are:

- **(i) Poll a dedicated test mailbox over IMAP** -- the only way to actually close the
  `/api/contact` gap, since the endpoint's own response can never reveal an SMTP failure by
  design. Costs: a real mailbox, IMAP credentials as a new secret, `imaplib` polling with a
  timeout/retry (delivery isn't instant). Simplest setup: reuse the same address the deployed
  app's contact notifications already go to (whatever `FPL_INTEL_SERVER_SMTP_*`/the contact
  recipient is configured to on Railway today) rather than standing up a second inbox -- the
  agent's IMAP credentials just need read access to that same mailbox to confirm its own
  clearly-marked test message actually arrived.
- **(ii) API-response-only** -- sufficient for `/api/reminder-opt-in`, provably insufficient for
  `/api/contact`.
- **(iii) Split by endpoint**: (ii) for `/api/reminder-opt-in` (already closes that gap for free),
  (i) for `/api/contact` specifically (the one endpoint where nothing short of mailbox-polling can
  detect the exact class of bug that prompted this issue).

**Recommendation: (iii).** Treating both endpoints the same either over-invests (full IMAP polling
for `/api/reminder-opt-in`, which doesn't need it) or under-delivers (skipping IMAP entirely means
`/api/contact` -- the endpoint that actually broke -- stays unverified for the one failure mode
this issue is about). This does mean provisioning IMAP infrastructure is not avoidable if this
issue is to actually close the gap it names, contrary to what a cheaper "check the response only"
reading of the issue might suggest at first.

### (b) Cadence

- **On-demand only (`workflow_dispatch`, no `schedule:`)**: matches "regression check I run after
  a deploy," the issue's own framing ("a human noticing a broken form days or weeks later" is the
  problem -- an on-demand check run right after every deploy closes that gap directly).
- **Scheduled (hourly/daily cron)**: catches drift between deploys too (an expired credential, a
  Railway network change with no code change involved) -- the issue's own list of causes
  ("stale env vars, a bad deploy, Railway network/DNS changes, an expired credential") includes
  several that aren't deploy-triggered at all.
- **Both**: `workflow_dispatch` for the deploy-triggered case, plus a daily (not hourly --
  this app has no continuous-deployment pipeline that redeploys many times a day, and a daily
  cadence is already an order of magnitude more coverage than "a human notices days or weeks
  later") `schedule:` for drift. Mirrors `scheduled-refresh.yml`'s own reasoning for choosing a
  cadence looser than "every possible moment" while still closing the real gap.

**Recommendation: both**, daily cron + on-demand. Trivial marginal cost (same script, two
triggers) once the script itself exists, and covers both failure classes the issue names.

### (c) Where it runs, and test-data isolation

- **Environment**: GitHub Actions, matching `scheduled-refresh.yml`/`deadline-reminder.yml` --
  no new infrastructure, same secrets-management story (repo secrets), same actor identity
  (`github.token`/an explicit PAT if ever needed) this repo already trusts for unattended runs.
- **Synthetic data marking, not a separate environment.** A second Railway environment would
  duplicate the volume/deployment/secrets story this session's #125 work just finished
  consolidating into one source of truth -- disproportionate for a test agent. Instead: a fixed,
  clearly-synthetic team ID range/pattern (e.g. reserve `90000000`-`90000099`, comfortably
  outside FPL's real team ID space -- current real team IDs are 8 digits, well under 90000000)
  and a clearly-marked test email address (e.g. `fpl-intel-live-check+<run-id>@<test-domain>`)
  for every write the agent makes, so a human auditing `profiles.db`/the contact log can
  immediately tell a synthetic row from a real visitor's.
- **Cleanup**: best-effort, not required for correctness. `/api/lookup-opt-out`/`/api/draft-squad`/
  `/api/profile` writes for a synthetic team ID are inert (no real manager is ever looked up by
  that ID, since it's outside FPL's real range) -- they don't need deleting to stop mattering, only
  to avoid quietly accumulating rows forever. A periodic manual/scripted cleanup (delete profiles.db
  rows for the reserved ID range older than N days) is a reasonable follow-up, not a blocker for
  landing this issue.
- **`/api/contact` has no synthetic-safe path** -- every submission triggers a real operator
  notification email and a real append to `contact-submissions.log`, regardless of team ID (it
  isn't even team-ID-scoped). Recommendation: mark the test submission's `message` field with an
  unambiguous, greppable prefix (e.g. `"[live-regression-check]"`) so both the operator's inbox
  and `contact-submissions.log` clearly show it as synthetic, not a real visitor's feedback. This
  submission is a real, deliverable email by necessity (per (a) above -- confirming it *arrives*,
  via the IMAP mailbox, is the entire point for this endpoint), sent once per run.

## Not yet decided -- deferred to the user, not resolved by the plan doc alone

- **Whether a daily real (clearly-marked) email to the actual operator/test mailbox is
  acceptable**, given (a)'s finding that `/api/contact` needs IMAP verification specifically to
  close the gap this issue is about. The alternative -- skip mailbox verification and cover only
  `/api/contact`'s input-validation rejection paths -- is real but partial coverage that would not
  have caught the incident that motivated this issue. This is a judgment call about inbox noise
  vs. actually closing the gap, not a technical constraint.
- **Which mailbox the IMAP check reads from** -- reusing the existing configured
  `FPL_INTEL_SERVER_SMTP_*` recipient (simplest, no new inbox) vs. a dedicated separate test
  address (cleaner separation from real operator traffic, costs one more account to provision).
- **The reserved synthetic team ID range** -- concrete value needs picking (this plan proposes
  `90000000`-`90000099` as a starting point, comfortably outside FPL's real ~8-digit team ID
  space).

## Recommendation

Build `scripts/live_regression_check.py` (mirroring `trigger_scheduled_refresh.py`'s/
`send_deadline_reminder.py`'s shape: env-var-driven config, `--dry-run`, no secrets logged) plus
`.github/workflows/live-regression-check.yml` (daily cron + `workflow_dispatch`), covering:
dashboard shell load (all nine tabs present in the rendered HTML), `/api/status` shape, each
visitor-writable endpoint's accept/reject behavior using the reserved synthetic team ID, the
empty-state vs. populated Decision Center/Model Performance check (no-team_id vs. a real public
team ID), `/api/refresh` correctly 403ing without a valid token (never called with a valid one),
and email verification split by endpoint per (a): API-response-only for `/api/reminder-opt-in`
(already sufficient), IMAP mailbox polling for `/api/contact` (the only way to close the gap this
issue is actually about).

## Not in scope

- The existing local unit suite and `verify-dashboard` skill -- unaffected.
- Any change to `server.py`/`dashboard.py` application code.
- Load/performance/soak testing.

## Dependency

None remaining.
