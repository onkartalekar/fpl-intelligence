# Contact Us tab (issue #110)

## Context

Issue #110 asks for a way for a visitor to report a bug, request a feature, or leave feedback --
confirmed via grep across `dashboard.py`/`dashboard.js`/`server.py` that nothing like this exists
today; the only channel is the GitHub repo's own issue tracker, which the served app never links
to. The issue originally left open whether this should point at GitHub Issues directly, a real
in-app form, or a hybrid.

**That question is now resolved by explicit instruction:** submissions must not be connected to
GitHub at all -- the operator wants to screen and groom them personally before anything becomes a
real, public GitHub issue (if it ever does). This rules out issue #110's Candidate (A) (link
straight to GitHub's "new issue" page) and the GitHub-facing half of Candidate (C) outright. The
remaining design space is about *how* a submission reaches the operator for that personal
screening step.

## Structural constraint found before evaluating candidates

**No operator-authenticated read surface exists anywhere in this codebase today.** Checked
directly: the only operator-only mechanism in the entire app is `FPL_INTEL_REFRESH_TOKEN`
(`server.py`), and it gates exactly one thing -- `POST /api/refresh`. There is no admin dashboard,
no "review queue" concept, no precedent for an endpoint whose entire purpose is "let the operator,
and only the operator, read something." Every other piece of state in this app is either fully
public (bootstrap/fixtures/transfers), or scoped-open per #45's model (a team's own profile row,
readable/writable without a credential because the underlying FPL data is already public).

This matters because it means any candidate that involves the operator *logging in to view
submissions* is not a small addition -- it would be the first-ever admin-auth surface in this
codebase, a genuinely new category of infrastructure, not "one more table plus one more endpoint."
Worth weighing that cost explicitly against the candidates below, not discovering it mid-build.

## Candidate operationalizations

### Candidate 1 -- email only, reusing issue #79's existing SMTP pattern

The form POSTs to a new endpoint; the server composes and sends one email to the operator
containing the category, message, and optional reply-to address. No new storage, no new
admin-auth surface at all -- "screening and grooming personally" happens in the operator's own
inbox, which is already exactly a triage/grooming interface. This is the smallest possible
addition to the codebase's existing shape: `reminder_confirmation.py` (#79) is already a template
for "a request handler needs to synchronously send one short email and turn any failure into a
single clean exception type" -- this would be the same pattern, reused, not reinvented.

**Downside, stated plainly:** no durable record if the send fails, the operator's mailbox is
unreachable, or a submission needs to be found again later without having kept the original email.
No de-duplication of repeated identical reports, no query/filter/backlog view.

### Candidate 2 -- persisted storage plus a private review surface

A new store (a new SQLite file, since `profiles.db` is keyed by `team_id` and doesn't fit an
anonymous free-text submission naturally) plus a genuinely new operator-only authenticated
endpoint to read submissions. Durable, queryable, supports batch triage (mark reviewed, filter by
category) better than an inbox does.

This is the candidate that pays the structural cost named above: it requires designing an actual
admin-auth model from scratch (reuse `FPL_INTEL_REFRESH_TOKEN`? a dedicated token? skip HTTP
entirely and read the file with a local script, avoiding new auth surface but losing any
remote-review convenience?) -- there is no existing pattern in this codebase to lean on for that
decision, unlike every other design choice in this plan.

### Candidate 3 -- both: persist and email

Store every submission durably *and* email a copy immediately, combining Candidate 1's fast
visibility with Candidate 2's durability. This is strictly the union of both candidates' scope and
cost, not a shortcut between them -- worth naming, but it's "build both," not a third option.

### Candidate 4 -- a private/unlisted GitHub surface (Discussions, a private repo) -- ruled out

Named only so it's clear this was considered and explicitly excluded by the stated requirement,
not overlooked: any GitHub-hosted surface is out, full stop, regardless of its visibility settings,
per "I do not want bug reports connected to github."

## Recommendation

**Candidate 1 (email only), optionally hardened with a lightweight local-file durability
backstop, not the full Candidate 2 buildout.**

Reasoning:

- It directly satisfies the stated requirement -- an inbox the operator reads and decides what to
  do with *is* personal screening and grooming, with zero new concepts introduced.
- It reuses an established pattern (#79's synchronous single-email send) rather than inventing
  the first admin-auth surface this codebase has ever needed -- consistent with how this project
  has repeatedly favored reuse over new infrastructure throughout (issue #55's reminder script
  reusing the same SMTP-env-var shape rather than a new config mechanism; issue #64 generalizing
  #79's own per-team-loop pattern rather than writing a new one).
- Candidate 2's real advantage (durable, queryable, batch-groomable) is a genuine upgrade a
  low-traffic personal tool may never actually need -- and if it does, this is a clean additive
  change later, not a redesign of Candidate 1's work.

**The durability gap is worth closing cheaply rather than accepted outright.** A submission that
fails to send (SMTP misconfigured, transient network failure) shouldn't just vanish. Appending
each submission to a local, gitignored log file on disk -- write-only, never read back over HTTP,
no new auth surface, no new abuse exposure since nothing exposes it remotely -- closes this gap
for near-zero added cost. This is not Candidate 2: there's no review UI, no query capability, just
a plain-text safety net the operator can `cat`/`grep` by hand if an email is ever missed. Worth
building alongside Candidate 1 from the start rather than as a later addition.

## Concrete design sketch (direction, not final -- confirm before `ship-issue`)

- New tab per #110's own request: `<button data-view="contact">Contact Us</button>`, a `view-contact` section with a form -- category (bug / feature request / feedback / other), a free-text message field, an optional reply-to email.
- New endpoint, e.g. `POST /api/contact` -- **open, no `X-Refresh-Token`**, consistent with #45's model (matches the other four open write endpoints, not `/api/refresh`'s operator-only shape, since submitting feedback is a visitor action, not an operator action).
- Gated by its own `CooldownLimiter`, per-source-IP, matching `profile_write_limiter`'s existing pattern -- same defense against automated spam every other open endpoint already has.
- SMTP: **open question, worth a quick decision rather than guessing** -- reuse `FPL_INTEL_SERVER_SMTP_*` (already exists for exactly this shape: a live request handler sending one short email synchronously) versus a dedicated `FPL_INTEL_CONTACT_SMTP_*` (independently rotatable credential, matching #79's own stated reasoning for keeping its SMTP vars separate from the offline reminder script's). Leaning toward reuse, since the "why keep these separate" reasoning in #79 was about different *exposure profiles* (live server vs. offline trusted cron) -- this new endpoint has the same exposure profile as #79's confirmation-email send, not a different one.
- Basic validation matching `_validate_profile_payload`'s established style (exact-key allowlist, length caps, no user input reflected back in error responses).

## Next step

Present this to the user for direction on: (1) confirm Candidate 1 + local-log backstop over
Candidate 2/3, (2) SMTP var reuse vs. a dedicated one. Then hand off to `ship-issue`.
