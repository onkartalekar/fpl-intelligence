# Self-serve deadline-reminder opt-in (issue #79)

Researched 2026-08-09. The UX placement (extend the existing `view-profile`
tab, tri-state attention-banner nudge, no wizard/modal) is settled in the
issue body and is not re-litigated here. What this plan resolves is the
issue's genuinely open question: the abuse-mitigation design for the new
public write path that causes a real inbox to start receiving recurring
email.

## Context

#55 built the reminder *delivery* mechanism (GitHub Actions cron +
`smtplib` in `scripts/send_deadline_reminder.py`), but deliberately kept
the recipient list as a hand-edited `FPL_INTEL_REMINDER_TEAMS` secret and
left `profiles.py`'s `email` column unwritten by any endpoint -- #55's own
plan doc named the missing self-serve flow and explicitly deferred it
("Building that self-serve flow ... is out of scope for this issue"). #79
is that missing piece.

Every existing mutating endpoint in `server.py`
(`/api/profile`, `/api/draft-squad`, `/api/lookup-opt-out`) shares one
security model: open, unauthenticated, keyed on a publicly-guessable team
ID, protected only by a per-source `CooldownLimiter`. That model is
appropriate for team-scoped *preference* data, where the worst case of
someone else touching your row is self-directed annoyance (they can see
what you could already see, or flip a flag on data that's already
public). A reminder signup is structurally different: the party who bears
the cost of abuse is not the team owner, it's whoever's email address gets
typed into the form -- a third party who may have no relationship to the
team ID at all.

## Structural findings before evaluating candidates

**#62's PIN does not prove email ownership, even in principle.**
`_default_lookup_opt_out_action` (`server.py:416`) uses first-claim
semantics: no `pin_hash` stored yet for a team ID means *any* PIN meeting
the shape rule (`_LOOKUP_OPT_OUT_PIN_RE`, 6-24 alphanumeric chars) claims
it, in the same request. That's the right design for #62's actual
stakes (a visibility toggle with no third-party victim -- see
`plans/issue-62-lookup-opt-out.md`'s own honest-limitation section), but
it means an attacker submitting a reminder-enable request for a team ID
they don't own always succeeds on the *first* request, regardless of
whether a PIN is required, because there is nothing yet to check the
submitted PIN against. A PIN only starts constraining *later* requests
for the same team ID. So reusing #62's mechanism verbatim would not stop
the actual abuse case the issue is worried about (routing a stranger's --
or a targeted victim's -- inbox into a recurring subscription); it would
only stop a *second* attacker from then fighting the first over who
controls that team's reminder settings. This is a materially weaker
guarantee than it looks at first glance, and worth stating precisely
rather than assuming PIN-gating solves the problem by analogy to #62.

**Team IDs are exactly the low-cardinality, guessable integers the issue
worries about.** `_TEAM_ID_RE`/`_coerce_team_id` accept any `1 <=
team_id <= 99_999_999`, but real FPL team IDs today are far denser than
that range (low millions at most) and are never treated as secret
anywhere else in this app (`/api/lookup-opt-out`'s own docstring notes the
underlying manager data is "already public via FPL's own API"). Guessing
or iterating a real team ID costs an attacker nothing.

**`server.py` has zero existing outbound-email capability.** `grep -rn
smtplib src/ scripts/` confirms `smtplib` is imported only in
`scripts/send_deadline_reminder.py`, never in `src/fpl_intel/`. A
confirmation-link design is therefore new capability for the live server
process, not just a new DB write -- it means a synchronous network call
(to an SMTP server) inside a `do_POST` handler, on a codebase whose
`server.py` currently does no outbound networking of its own at all
(`_default_refresh_action` shells out to a script; everything else reads
local artifacts).

**A naive implementation would leak `email` to anyone who looks up a team
ID.** `_serve_dashboard` calls `visitor_profile_action(team_id)`
unconditionally and splices the result into `state["profile"]` on *both*
paths -- the visitor's own cookie-remembered team (issue #45) and an
explicit `?team_id=` one-off lookup of someone else's team (issue #46,
`is_explicit_lookup=True`). Every field `_default_visitor_profile_action`
returns today (`timezone`, `confirmed_free_transfers`, `risk_profile`,
`draft_squad`) is harmless to show a third-party looker-upper -- but
`email` is not. If `email`/`reminder_*` fields are added to that same
unconditional splice without a guard, any visitor who types in an
arbitrary team ID would see that team's saved email address rendered
into the page. This has to be fixed as part of this build, not treated as
an edge case: the profile-read action needs the `is_explicit_lookup` flag
threaded through (or filtering done at the `_serve_dashboard` call site)
so reminder fields are only ever included on the visitor's own view.

**The server can only run on `127.0.0.1` today.** `create_server` raises
`ValueError` if `host != "127.0.0.1"`. So none of this is live-exploitable
this week -- only the operator's own machine can reach the endpoint at
all. That is exactly the situation #62 was built under too, and #62 still
built its real PIN mechanism rather than deferring it, on the reasoning
that retrofitting abuse-resistance after #27 lifts this restriction is
harder than designing it in now. Same reasoning applies here, more so:
`/api/lookup-opt-out`'s worst case if the design were wrong is
self-directed annoyance; this endpoint's worst case is someone else's
inbox.

**`FPL_INTEL_LLM_*` (`news_signals.py`) is this app's only precedent for
an optional external-service credential**, and it deliberately keeps
provider/key config in env vars, read at call time, never persisted or
logged, with a fail-safe "return nothing usable" contract on any
network/auth error (`extract_availability_signals` returns `[]` rather
than raising). Worth carrying that same fail-safe posture into a
server-side SMTP sender: a failed confirmation-email send must produce a
clean 502 to the caller, never a stuck DB row referencing a token nobody
received.

## Candidate operationalizations

### (a) No confirmation link, PIN-gated like #62

Reuse #62's `pin_hash` column and first-claim semantics to gate writes to
`email`/`reminder_enabled`/`reminder_lead_hours`, exactly as it gates
`opted_out` today.

- **Pro:** zero new server capability. No `smtplib` in `server.py`, no new
  SMTP credentials, no confirm endpoint, no synchronous network call in
  the write path. Mechanically the smallest change, and reuses a proven,
  already-tested pattern (`load_pin_hash`, `_hash_pin`,
  `secrets.compare_digest`).
- **Con -- and this is decisive, not a minor caveat:** as established
  above, first-claim always succeeds. A PIN proves "I'm the one who gets
  to change this team's settings from now on," not "I control the inbox
  I just typed in." Submitting `{"team_id": <any public team>, "email":
  "victim@example.com", ...}` with a PIN of the attacker's own choosing
  succeeds on the very first request, and the victim has no PIN to stop
  it -- they never see the form. Every gameweek thereafter, that inbox
  receives a recurring, real email it never asked for, indistinguishable
  from spam.
- Unlike #62 -- where the honestly-stated residual risk is "a determined
  attacker could brute-force a short PIN," a threat that at least
  requires *effort* and *repetition* against the same target -- this
  candidate's core failure doesn't require an attack at all. It's the
  first, ordinary use of the feature by anyone other than the team's
  owner. That's a different severity class than #62's own declared
  residual risk, not the same risk in a new location.
- **Decline as the primary mechanism.** It solves a problem (subsequent
  tampering) that matters less than the one it doesn't solve (initial
  email-ownership proof).

### (b) Confirmation-link double opt-in

The server sends a "click to confirm" email itself before
`reminder_status` moves from a request to actually enabled, using a
random, single-use, expiring token.

- **Pro:** proves what actually matters -- that the request originated
  from someone with access to that inbox, not just someone who knows a
  team ID. This is the standard, well-understood mitigation for exactly
  this class of problem across the web, for exactly this reason. Caps the
  cost of an attacker abusing a stranger's email at "one unwanted
  confirmation email," not a recurring subscription -- a bounded,
  one-time nuisance rather than an open-ended one.
- **Con -- real, not hypothetical, new scope**, confirmed by the
  `smtplib` grep above: `server.py` gains its own outbound-email
  capability, its own SMTP credentials, a signed/random token column set,
  a new confirm endpoint, and a synchronous external network call inside
  a request handler (needs a short timeout and a fail-safe error path,
  matching the `news_signals.py` precedent). This is materially more
  build than (a) or (c).
- Needs its own new abuse bound too: rate-limiting by source IP alone
  (this app's existing `CooldownLimiter` pattern) doesn't stop an
  attacker who rotates source IPs from repeatedly re-triggering
  confirmation *sends* at the same victim address. A **second,
  team-ID-keyed** cooldown on the send action itself (independent of
  which IP requests it) is needed to bound worst-case email volume to one
  target regardless of source rotation. See proposed shape below.

### (c) Do nothing extra -- accept the current risk profile

Treat `/api/reminder-opt-in` like `/api/profile`/`/api/draft-squad`:
open, rate-limited, no ownership proof at all, matching this app's
established pattern of not gating preference writes behind identity.

- **Pro:** zero new scope, perfectly consistent with the rest of the
  codebase's stated trust model, and #28 (a dedicated security review
  before public hosting) is coming later anyway and could revisit this
  specifically -- so nothing here is unfixable-later.
- **Con:** the issue body's own framing is correct that this endpoint is
  not like the others. `/api/profile` and `/api/draft-squad` writes stay
  entirely within the team-ID's own already-public data -- worst case,
  someone edits a stranger's timezone or draft squad, which is visible
  only to that team ID's own visitors and trivially fixed by the real
  owner re-saving. This endpoint's effect lands on a party who isn't even
  in the `team_id` -> data relationship at all, has no way to discover
  the abuse short of receiving unwanted email, and (unlike a wrong
  timezone) can't self-correct by "just re-saving" -- they'd have to find
  and use this exact app's UI to stop mail they never signed up for.
  Recurring, third-party-directed side effects are a different severity
  class from every other endpoint in this file, and "we'll gate it later
  in #28" is a worse sequencing choice than gating it correctly the first
  time -- #62 already established that this codebase's practice is to
  build the real mechanism at the point a feature first creates the risk,
  not defer it to a later, unrelated review.
- **Decline outright**, not just deprioritize -- this is the one
  candidate that leaves the issue's own stated concern completely
  unaddressed.

## Recommendation

**Build (b), scoped narrowly: confirmation-link double opt-in gates only
the *enable* transition (setting a specific email address and turning
reminders on). Every other transition on this row (decline, disable,
re-request) stays open and rate-limited, matching (c)'s reasoning applied
to the parts of this action that actually share (c)'s risk profile.**

The key insight from the investigation above is that "the reminder
opt-in endpoint" isn't one action with one risk level -- it's several:

| Transition | Third-party effect? | Mechanism |
|---|---|---|
| Enable (submit email + lead-time) | **Yes** -- an inbox that may not be the requester's starts receiving recurring mail | Confirmation-link double opt-in (candidate b) |
| Decline ("no thanks") | No -- self-directed, clears the attention-banner nudge for whoever is looking at this team's profile right now | Open, rate-limited (candidate c's reasoning) |
| Disable (turn off an already-enabled reminder) | No -- worst case is the real owner's own reminders stop; trivially fixed by re-enabling (which re-confirms) | Open, rate-limited (candidate c's reasoning) |

This avoids over-building: the full weight of (b) lands only where the
issue's actual concern lives (a stranger's inbox starting to receive
mail), while decline/disable stay as cheap and consistent with the rest
of `server.py` as `/api/profile` already is. It also means **(a)'s PIN
mechanism is not needed at all** for this feature -- not because #62's
approach was wrong for its own problem, but because the two remaining
gaps PIN-reuse might otherwise be reached for (blocking a griefer from
repeatedly toggling `decline`/`disable` on someone else's row) are
low-stakes enough to fall under (c)'s already-accepted trust model, the
same as `/api/profile` accepts today. If real-world abuse of
disable/decline is ever observed, layering the existing `pin_hash`
column onto those two transitions is a small, additive follow-up --
explicitly not needed to ship this issue.

### Schema changes (`src/fpl_intel/profiles.py`)

Add to the `profiles` table (all nullable, all `ALTER TABLE ... ADD
COLUMN` on top of the existing `_SCHEMA`, same additive-migration style
`opted_out`/`pin_hash` used for #62):

- `reminder_status TEXT` -- one of `NULL` (never decided), `'pending'`
  (enable requested, confirmation email sent, not yet clicked),
  `'enabled'` (confirmed), `'declined'` (explicit "no thanks" or a
  disable of a previously-enabled reminder). This single column is what
  drives the tri-state attention-banner logic: the nudge clears whenever
  `reminder_status is not NULL`, i.e. on *any* decision, including
  `'pending'` -- a user who submitted the form has already made a
  decision, even before clicking confirm.
- `reminder_lead_hours INTEGER` -- one of `3`/`12`/`24` (the picker's
  three values; `send_deadline_reminder.py`'s `lead_hours` already
  accepts any positive integer, so no script change needed).
- `reminder_pending_email TEXT` -- the submitted-but-not-yet-confirmed
  address. Kept separate from `email` deliberately: `email` only ever
  holds a *confirmed* address (matching its existing docstring,
  "populated only by #55's explicit reminder opt-in" -- now concretely
  true), so a stale unconfirmed submission never gets treated as live by
  `send_deadline_reminder.py` if it's later wired to read from
  `profiles.db`.
- `reminder_confirmation_token_hash TEXT` -- SHA-256 of a
  `secrets.token_urlsafe(32)` token, same `_hash_pin`-style pattern
  already used for PINs; the raw token is only ever in the emailed link,
  never stored.
- `reminder_confirmation_expires_at TEXT` -- ISO8601, e.g. now + 24h.
  Checked at confirm time; no active cleanup job needed at this scale
  (an expired, unconfirmed row is inert, not a live risk).

`email` itself needs no schema change -- it already exists, per the
docstring in `save_profile`.

### New endpoints (`src/fpl_intel/server.py`)

**`POST /api/reminder-opt-in`** -- body `{"team_id", "action"}` where
`action` is one of:
- `"enable"` -- additional fields `{"email", "lead_hours"}`. Validates
  email shape (simple presence-of-`@`/length check, same rigor as
  `send_deadline_reminder.py`'s `parse_reminder_teams` uses today) and
  `lead_hours in {3, 12, 24}`. Generates a token, attempts the SMTP send
  **first**; only writes `reminder_pending_email`/
  `reminder_confirmation_token_hash`/`reminder_confirmation_expires_at`/
  `reminder_status='pending'` to the DB on send success. On send failure,
  returns a clean error and writes nothing -- avoids a DB row referencing
  a token that was never delivered, matching the `news_signals.py`
  fail-safe posture noted above.
- `"decline"` -- sets `reminder_status='declined'`, clears any pending
  token fields. No email required.
- `"disable"` -- same as `"decline"`, for a row that was previously
  `'enabled'`; also clears `email`. (Modeled as the same underlying write
  as decline -- the distinction is purely which UI button reaches it.)

Rate-limited via a new `CooldownLimiter` instance
(`_REMINDER_OPT_IN_COOLDOWN_SECONDS`, e.g. 30s, matching
`_LOOKUP_OPT_OUT_COOLDOWN_SECONDS`'s reasoning: this is a
third-party-affecting surface, not an ordinary preference save, so it
gets a tighter per-source cooldown than `/api/profile`'s 5s). **Plus** a
second limiter keyed by `team_id` rather than source IP
(`_REMINDER_CONFIRM_SEND_COOLDOWN_SECONDS`, e.g. 600s), applied only to
the `"enable"` action's SMTP send step -- this is the piece that
specifically bounds worst-case email volume landing on one target address
regardless of how many source IPs an attacker rotates through, which the
existing per-source-only `CooldownLimiter` pattern doesn't cover on its
own.

**`GET /api/reminder-confirm?team_id=&token=`** -- validates the raw
token against `reminder_confirmation_token_hash` with
`secrets.compare_digest` (same pattern as the refresh token and #62's
PIN check), checks `reminder_confirmation_expires_at` hasn't passed. On
success: copies `reminder_pending_email` -> `email`, sets
`reminder_status='enabled'`, clears the pending/token/expiry columns.
Returns a small static confirmation HTML page (this is a link a person
clicks from their email client, not a fetch call, so it can't be a JSON
response the way every other endpoint is) with a link back to `/`. Rate
this too, per-source, as defense in depth -- token entropy alone
(32 bytes url-safe) already makes brute force infeasible, but every other
sensitive check in this codebase carries a cooldown regardless of
theoretical strength (e.g. the refresh token itself isn't brute-forceable
either, and still lives behind Host/Origin checks).

**SMTP sender.** New small module, e.g.
`src/fpl_intel/reminder_confirmation.py`, holding just the confirmation
email composition + send (not the full reminder-composition logic
`send_deadline_reminder.py` owns) -- `smtplib.SMTP(...).starttls()`, a
short (e.g. 10s) timeout, returning `True`/`False`/raising a narrow
exception type the handler catches, same fail-safe contract as
`news_signals.py`'s callers. Credentials via **new, separate** env vars
-- `FPL_INTEL_SERVER_SMTP_HOST` / `_PORT` / `_USER` / `_PASSWORD` --
rather than reusing `send_deadline_reminder.py`'s `FPL_INTEL_SMTP_*`.
Reasoning: the live server process handles untrusted, attacker-reachable
input on every request (once #27 lifts the 127.0.0.1 restriction); the
offline reminder script only ever runs from a trusted GitHub Actions
cron. Different exposure profiles warrant separate, independently
rotatable credentials as a matter of blast-radius hygiene, even though
nothing stops an operator from pointing both at the same mailbox/app
password in practice -- the separation is about the *configuration
surface*, not a hard requirement for two physically distinct mailboxes.
Follows the `FPL_INTEL_LLM_*` precedent's own approach exactly:
plain env vars, read at call time, never logged, never required (a
missing/invalid config makes `"enable"` fail cleanly with a clear error,
the same way a missing `ANTHROPIC_API_KEY` makes `news_signals.py`
return `[]` rather than crash).

**Confirmation-link base URL.** Built from the already-validated
trusted `Host` header (`_has_trusted_host` already confirms it matches
`127.0.0.1:{port}` today, or the real hostname once #27 assigns one) --
never hardcoded, so the same code works unchanged before and after #27.

**Fix the profile-read leak identified above, as part of this build, not
a follow-up.** Thread `is_explicit_lookup` into (or filter at the call
site around) `_default_visitor_profile_action`: `email`, `reminder_status`,
`reminder_lead_hours` are only ever included in `state["profile"]` when
`is_explicit_lookup` is `False` (the visitor's own cookie-remembered
team). An explicit `?team_id=` lookup of someone else's team must
continue to see exactly what it sees today -- nothing reminder-related.

### Dashboard changes (`src/fpl_intel/dashboard.js` / `dashboard.html`)

Per the issue's settled UX direction:

1. **New form inside the `view-profile` tab, separate from
   `#profile-form`** (same pattern as draft-squad and lookup-opt-out
   already getting their own dedicated forms rather than folding into
   `_validate_profile_payload` -- different validation shape, different
   endpoint, different async confirmation semantics don't belong mixed
   into the five-field profile save). Renders differently per
   `state.profile.reminder_status`:
   - `null`/undecided: email input + a T-3h/T-12h/T-24h radio group +
     "Get reminders" button, plus a lower-key "No thanks" link (maps to
     `action: "decline"`).
   - `'pending'`: "Check your inbox at `<email>` to confirm" message,
     plus a "Resend" action (re-runs `"enable"` with the same values,
     subject to the same team-ID-keyed cooldown) and a "Cancel" (maps to
     `"decline"`).
   - `'enabled'`: shows the confirmed email + lead-time, a "Disable"
     button (`action: "disable"`), and a way to change email/lead-time
     (submits a fresh `"enable"`, which re-triggers confirmation --
     changing the destination address always re-proves ownership of the
     *new* address, it doesn't inherit trust from the old one).
   - `'declined'`: "You've opted out of deadline reminders" + a
     "Reconsider" link back to the undecided form.
2. **Attention-panel nudge** (`dashboard.js`'s `attention` array,
   currently built around line 12): add one more entry, gated on
   `state.manager.connection_status !== 'not_configured' &&
   !state.profile.reminder_status` (i.e., team connected, and
   `reminder_status` is `NULL`) -- `{level:'Setup', kind:'info',
   title:'Get deadline reminders', body:'Get an email before each
   gameweek deadline with your recommended moves.', action:'Open My
   Profile', view:'profile'}`. Tri-state as the issue requires: this
   condition is false (nudge cleared) once `reminder_status` is
   `'pending'`, `'enabled'`, or `'declined'` -- any decision, not only an
   opt-in -- so a user who declines doesn't keep getting re-nudged.
3. **`/api/reminder-confirm`'s landing page** is a small standalone HTML
   response (server-rendered by the new endpoint itself, per above), not
   part of `dashboard.html`/`dashboard.js` -- it's reached by clicking an
   emailed link outside the app's normal SPA-ish flow, and needs to work
   even if the visitor has no cookie/session context at all.

## Open items intentionally left to `/ship-issue`

- Exact confirmation-email copy/subject line and the confirm-page's HTML
  (content, not architecture).
- Whether `"disable"`/`"decline"` should also clear
  `reminder_lead_hours`, or just leave it as a remembered default for
  next time a user re-enables (leaning toward "leave it," least
  surprising, mirrors how `draft_squad`/`email` are already preserved
  across unrelated profile saves in `profiles.py`) -- worth a quick
  confirm at implementation time, not a design fork.
- Unit-test coverage shape for the SMTP-send-then-persist ordering
  (mock SMTP, no live network, matching `news_signals.py`'s and #55's own
  test conventions).
