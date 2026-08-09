# Per-team profile storage, no registration required (issue #45)

Absorbs #44 (originally "self-serve registration: profile-key default,
OAuth optional," closed as superseded 2026-08-08 — see that issue's
closing comment). This doc was `plans/issue-44-self-serve-registration.md`
before the merge; moved here to match its final issue number. See
[plans/issue-27-cloud-hosting.md](issue-27-cloud-hosting.md)'s "Non-OAuth
registered accounts" section for the design this doc supersedes.

## Reframed after user feedback (2026-08-08)

Asked directly, while `/plan-issue`ing #44: is OAuth actually needed, and
does a one-time secret the user must save and later retype to "restore"
their team really carry its weight? Working through it: no, on both
counts.

**The key fact, already established by #46 and #62, not new here:** a
manager's FPL data (squad, points, rank, transfer history) is already
public via FPL's own API. Nothing about registering an account would
protect that — it's not secret today and registering wouldn't make it
secret. The only things actually worth protecting are the small set of
*preferences a visitor saves on top of that public data* — risk profile,
confirmed free transfers — which aren't sensitive, just easy to mix up
between different people if there's no separation at all.

That reframes the whole problem: the FPL team ID a visitor already knows
by heart can be the tenant key for "whose settings are these," and
because it's not being used as a secret (read access to that data was
already open), there's no registration ceremony required to make it
safe to use that way.

**What replaces OAuth + profile-key:** an HttpOnly cookie, generated and
attached automatically the moment someone first saves a preference for a
team ID — never shown to the user as text, nothing to copy, nothing to
lose. In the same browser, it makes "your team" the default view all
season without retyping the team ID. This satisfies the actual ask
("access their team, history, performance, recommendations... throughout
the season") directly, without a signup step.

**What this does *not* try to do, on purpose:** strongly authenticate
"you are the real owner of this team ID" the way a password or OAuth
login would. That's a deliberate simplicity trade-off, not an oversight
— see "Security model" below for exactly what is and isn't protected,
and why the gap that matters (#55, #62) gets its own, narrower answer
instead of a blanket account system everyone has to go through. With no
separate credential/session layer left to build, #44 and this issue
became one coherent piece of work, not two.

## Security model — what's protected, what isn't, and why that's enough

Three tiers of stakes, three different answers, not one uniform gate:

1. **Reading any team's public data (squad, history, points) — no gate,
   unchanged.** This is #46's existing no-signup lookup. Nothing here
   changes it.
2. **Saving low-stakes preferences (risk profile, confirmed free
   transfers) for a team ID — cookie-based convenience, not access
   control.** Writes to a team ID's saved preferences are allowed
   regardless of whether the request's cookie matches that team ID's
   prior claim. This mirrors today's actual security posture exactly:
   the current single-operator `config/user-profile.json` has *zero*
   access control on saving your own settings either — anyone who can
   reach the running server can save a new profile. Generalizing that to
   "anyone who can reach the running server can save preferences for any
   team ID" is not a regression, and rate-limiting the write endpoint
   (mirroring #46's `CooldownLimiter`) still prevents automated mass-
   tampering across many team IDs even without per-team gating.
   - Honest cost: a stranger who knows your team ID could, in principle,
     overwrite your saved risk profile. Recoverable in one click by
     resaving it yourself; nothing sensitive changes hands.
3. **Actions with real third-party stakes (#55's email reminders, #62's
   opt-out) — get their own proportionate protection, not this issue's
   job.** #55 already requires the visitor to type an email address as
   an explicit opt-in; that's the natural place for a confirmation
   step (a magic link) before reminders actually start, independent of
   any team-ID cookie. #62's opt-out needs *some* proof "this is my
   team" specifically because suppressing recommendations for someone
   else's team ID is a real, if minor, act of interference — worth a
   light, optional, purpose-built check (e.g. a self-chosen PIN, asked
   for only when someone actually wants to use that specific feature)
   rather than folding it into a general-purpose account system nobody
   else needs.

## Mechanism

- **Storage:** a `profiles` table keyed on `team_id` (not an opaque
  account ID) — `team_id, timezone, risk_profile,
  confirmed_free_transfers, confirmed_free_transfers_event, email
  (nullable, #55 opt-in only), created_at, updated_at`. Use stdlib
  `sqlite3` — keeps the project's zero-third-party-dependency property
  intact. Single file (e.g. `data/profiles.db`), replacing
  `config/user-profile.json` / `_default_profile_action` in
  `src/fpl_intel/server.py`. Same validation rules already in
  `_validate_profile_payload` (team_id, timezone,
  confirmed_free_transfers, confirmed_free_transfers_event,
  risk_profile), just keyed per team instead of one global blob.
- **Cookie:** set the first time a visitor saves any preference for a
  team ID (`Set-Cookie: fpl_team_id=<team_id>; HttpOnly; Secure;
  SameSite=Lax`, long-lived). No signature, no hashed secret, no
  server-side session table needed — it is purely "which team ID should
  `/` default to for this browser," not a credential. Reading it back on
  `GET /`: use the existing `?team_id=` query param if present (#46,
  unchanged), else fall back to the cookie's team ID, else today's
  "not configured" empty state.
- **Saving preferences:** the existing `/api/profile` endpoint's shape
  barely changes — it already validates and writes exactly these fields
  (`_validate_profile_payload` in `server.py`) — the only structural
  change is the destination moving from a single global
  `config/user-profile.json` to a per-`team_id` row in this issue's
  store, and the response setting the `fpl_team_id` cookie alongside
  today's existing behavior.
- **Persistence requirement**: disk-backed, not `:memory:`, durable only
  if it sits on a real persistent volume that survives instance
  recycling — an Axis B (hosting) constraint carried into this issue,
  not solved here, but the storage code shouldn't assume otherwise (no
  in-memory fallback that silently loses data).
- **Single-instance-only**: SQLite here is not a safe target for
  multiple concurrent app replicas on separate disks — not attempting to
  design around future horizontal scaling; deferred to a hosted database
  if/when actually needed.

## Recommendation

1. Build the `profiles` table (stdlib `sqlite3`) and migrate
   `_default_profile_action`/`_validate_profile_payload` in
   `src/fpl_intel/server.py` to read/write it, keyed on `team_id`.
2. Wire `GET /`'s team-ID resolution order: query param (#46) → cookie →
   unconfigured default. Set the cookie on successful `/api/profile`
   saves.
3. Add write-rate-limiting on `/api/profile` mirroring #46's
   `CooldownLimiter`, so open writes (tier 2 of the security model) stay
   bounded against automated abuse.
4. **#55's email confirmation and #62's opt-out PIN are each their own,
   separate, smaller pieces of work** — not blocked on this issue, and
   not solved by it. Flag this explicitly in both issues' bodies.
5. **OAuth is dropped entirely**, not deferred — nothing in this design
   leaves a gap only OAuth could fill. If a genuinely stronger
   proof-of-identity need surfaces later (unlikely given the public-data
   reality above), that's a fresh decision then, not a shelved TODO now.
