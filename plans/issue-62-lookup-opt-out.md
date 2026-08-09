# No-signup lookup: opt a team out of derived recommendations (issue #62)

## Context

#46's no-signup lookup computes and shows a derived recommendation
(roll/transfer/chip suggestions) for any team ID a visitor supplies. The
underlying manager data is already public via FPL's own API -- this issue
is only about whether a manager can opt *the derived analysis* out, not
about hiding anything FPL itself doesn't already show. Already corrected
once (2026-08-09, in the issue body) after #44/#45 shipped without any
identity system: this can't lean on a "registered account" the way
originally assumed, because that concept doesn't exist. It needs its own,
narrow mechanism.

## Candidate operationalizations

### Proof of ownership: a self-chosen PIN, scoped to this one action

- **(P1) First-claim PIN, stored alongside the team's row -- recommended.**
  Extend #45's `profiles` table with two nullable columns: `opted_out`
  (boolean) and `pin_hash` (SHA-256 of a self-chosen PIN, never the raw
  value -- same principle already used nowhere else in this app since
  #44/#45 dropped credentials entirely, but appropriate here specifically
  because this one action has real third-party stakes). The *first* person
  to toggle opt-out for a team ID sets the PIN in the same request; from
  then on, changing it requires the same PIN. No PIN exists until someone
  first uses this feature -- teams that never touch it stay exactly as
  they are today, no new column populated.
- **(P2) No proof at all, first-come toggle -- declined.** Would let
  anyone flip anyone else's opt-out flag, which is worse than not having
  the feature: it becomes a griefing vector (repeatedly re-enabling
  someone's recommendations right after they disable them, or vice versa)
  rather than a manager's own control over their own team.
- **Honest limitation, stated plainly rather than glossed over:** a short
  PIN plus per-IP rate limiting is *adequate* for this feature's actual
  stakes (interference with a visibility preference, not data exposure --
  the underlying data stays exactly as public as it's always been), but
  it is not a strong security boundary. A determined attacker rotating
  source IPs could still brute-force a 4-6 digit numeric PIN. Worth
  requiring a slightly longer PIN (6+ digits, or allow letters) specifically
  because there's no email/account to fall back on for recovery or to
  raise the cost of guessing -- but this is proportionate to what's being
  protected, not pretending to be strong crypto.

### Where the check happens

Only on the **explicit query-param lookup path** (`?team_id=`, #46's
"someone else is looking at this team" case) -- never on the cookie-driven
"this is my own remembered team" path (#45). Opting out means "don't show
*other people* my derived recommendations," not "hide my own data from
myself." `_serve_dashboard` in `server.py` already distinguishes these two
cases (`is_explicit_lookup`); the opt-out check gates only the branch
where that flag is true.

Check the flag **before** calling the (live-API-hitting, per #28's
findings) `lookup_action(team_id)`, not after -- an opted-out team's
lookup should cost nothing beyond a local `profiles.load_profile` read,
both for responsiveness and to avoid the exact unthrottled-FPL-call cost
#28 already flagged as a concrete risk.

## New endpoint

`POST /api/lookup-opt-out` -- body `{"team_id", "opted_out", "pin"}`:
- Rate-limited more strictly than `/api/profile`'s existing write
  cooldown (mirroring `CooldownLimiter`, but shorter-window/lower-count
  given the PIN-guessing surface noted above).
- No existing `pin_hash` for `team_id`: any PIN meeting the length/shape
  rule sets it, saves `opted_out`.
- Existing `pin_hash`: submitted PIN's hash must match
  (`secrets.compare_digest`, same pattern already used for the refresh
  token) before `opted_out` changes; on mismatch, a fixed, input-free
  rejection message -- never confirm or deny whether a PIN exists for a
  given team ID, so the endpoint itself doesn't become a way to probe
  which teams have opted out.

## Read-side change

`_serve_dashboard`'s explicit-lookup branch: if
`profiles.load_profile(...).get("opted_out")`, skip `lookup_action`
entirely and set `state["lookup"] = {"active": True, "team_id": team_id,
"status": "opted_out"}` -- a third status value alongside the existing
`"ok"`/`"error"`. `dashboard.js`'s `renderLookupBanner()` needs one new
branch for it (`"This manager has opted out of lookup recommendations."`),
matching the existing pattern for `"error"`.

## Recommendation

1. Add `opted_out`/`pin_hash` nullable columns to #45's `profiles` table.
2. Add `POST /api/lookup-opt-out` with the first-claim PIN semantics
   above, rate-limited tighter than ordinary profile saves.
3. Gate `_serve_dashboard`'s explicit-lookup branch on the flag, checked
   before the live-API call, not after.
4. Add the UI: a small opt-out toggle + PIN field, most naturally on the
   My Team view (alongside the "Look up a team" panel #46 already added)
   since that's where a manager already interacts with their own team ID.
5. State the PIN's real security level honestly in the UI copy itself
   (e.g. "a short PIN, not a password -- protects against casual
   interference, not a determined attacker") rather than implying
   stronger protection than a short PIN can actually provide.

No remaining open design questions block starting -- buildable as a
single `ship-issue` pass.
