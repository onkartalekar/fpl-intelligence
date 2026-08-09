# Security review before public hosting (issue #28)

## Context

Issue #28 lists six areas to audit in `server.py` ahead of #27 making the
dashboard publicly reachable. Re-investigated against the code as it
actually stands today (post #45/#46 — the issue predates both, and its
"where does profile data live once it's not a single local file" question
is now answered differently than the #27 plan doc originally assumed).

## Structural finding before evaluating the areas: #27's audience-model choice (Axis A) needs revisiting, and #28's answer depends on it

The #27 plan doc's "Decision so far" section rules out A1 (Tailscale,
private-only) and treats A2 (Cloudflare Access, named-few-with-login) as
"only a partial answer," reasoning that per-user profiles need real
in-app identity (OAuth) that a network-level gate wouldn't provide. That
reasoning no longer holds: #44 was closed as superseded, and #45 shipped
per-team storage with **no identity system at all** — a manager's FPL
data is already public, so nothing in the app distinguishes "the
operator" from "a random visitor." #46's no-signup lookup and #45's
per-team saves were both built assuming exactly that — fully open,
no-login access (a shape closest to the original A3, not A1/A2).

That resolves the "per-user profile" objection to A1/A2, but surfaces a
sharper, different problem for this issue specifically: **`/api/refresh`
— the expensive, shared, operator-level action — has no way to tell the
operator apart from any other visitor**, because nothing in the shipped
design tries to. The static bearer token gating it is embedded in every
served page's `<meta>` tag (`content="{token}"` in `_serve_dashboard`),
extractable by any visitor who views source, whether they arrived via
#46's lookup or #45's cookie-remembered view. Today this doesn't matter
— only the trusted local operator can reach the server at all, since it's
127.0.0.1-only. It's the first thing that breaks the moment #27 removes
that boundary.

This is genuinely a joint #27/#28 question, not something #28 can resolve
alone: A1/A2 (restrict *who reaches the origin at all*) would trivially
solve `/api/refresh`'s operator-distinguishing problem, but doing so
reintroduces exactly the login-wall/limited-reach friction #46/#45 were
built to avoid for the rest of the app. A3 (fully public) matches what
was actually built for viewing/saving, but leaves `/api/refresh`
structurally unprotected as-is. Flagging this prominently rather than
picking a side — see "Recommendation" below for a way to avoid forcing
one audience model to cover both concerns.

## Findings per area (from the issue body)

1. **Host/Origin allowlist (`_has_trusted_host`, the `do_POST` Origin
   check).** Both hardcode `127.0.0.1:{port}`. Mechanical to update once
   a real hostname exists, but the *target value* depends entirely on
   #27's audience choice (tailnet name for A1, tunnel hostname for A2, a
   real domain for A3) — already correctly flagged as blocked-on-#27 in
   the #27 plan doc; confirmed still accurate.

2. **Token protection on `/api/refresh` and `/api/profile` — the
   sharpest finding, and now two different answers.**
   - `/api/profile`: the token check is effectively redundant today,
     not a regression. #45's security model already deliberately made
     profile writes open (rate-limited, not credential-gated) —
     confirmed live: the token embedded in the page grants no more than
     an unauthenticated visitor already has by design.
   - `/api/refresh`: **not redundant, and not safe.** The token's actual
     job — restricting an expensive, shared action to the trusted
     operator — cannot be done by a value published in every response.
     See "Recommendation" for the fix.

3. **Input validation on `/api/profile` (`_validate_profile_payload`).**
   Reviewed against the current code: strict type checks on every field
   (rejects bool-as-int, non-digit strings), a fixed allowed-key set
   (unknown keys rejected outright, never echoed), timezone validated
   against `zoneinfo.available_timezones()` (rules out path-like values
   such as `../etc/passwd` structurally, not just by pattern), numeric
   ranges enforced server-side. No gap found here; already hostile-input
   ready, not just well-behaved-browser ready.

4. **Where profile/refresh data lives now, and multi-tenant leak
   risk.** Resolved by #45's design, re-verified directly: `profiles.db`
   queries are always scoped `WHERE team_id = ?` (real row isolation,
   not application-level filtering that could be gotten wrong), and
   `_serve_dashboard` builds a fresh `state` dict from disk on every
   request with no shared mutable object across requests/threads — no
   cross-tenant contamination path found. The shared
   `dashboard-state.json`/`dashboard.html` (bootstrap/fixtures/transfers)
   is intentionally identical for everyone; that's not a leak, it's the
   design.

5. **Rate limiting / abuse protection on `/api/refresh` — confirmed
   gap, and it's sharper than the issue anticipated.** `_handle_refresh`
   only prevents *concurrent* execution (`refresh_lock.acquire(blocking=
   False)`) — nothing bounds how often one caller can trigger it
   *sequentially*. Each call spawns a subprocess with up to a 300-second
   timeout that hits the live FPL API. Combined with finding 2 (the
   token is trivially obtainable by anyone), an unthrottled `/api/refresh`
   is a concrete availability risk once public — both to this server's
   own resources and to the app's own IP getting rate-limited by FPL's
   API — the same category of risk #46 already treated as
   must-fix-before-shipping for its own lookup path (`CooldownLimiter`).
   `/api/refresh` has no equivalent today.

6. **Dependency and secrets scan.** Clean. `grep` across `src/`/`scripts/`
   for imports confirms zero third-party dependencies (stdlib only, no
   `requirements.txt`/`pyproject.toml` to have vulnerable pins in the
   first place). No hardcoded credential-shaped strings found outside
   `.example.` files. `config/user-profile.json` and `data/profiles.db`
   are both gitignored (confirmed in `.gitignore`).

## Recommendation

1. **Fix finding 5 now, independent of #27's audience decision.** Add a
   time-based per-source cooldown to `/api/refresh`, mirroring #46's
   `CooldownLimiter` pattern already proven in this codebase. Cheap,
   unambiguous, and valuable under every audience model — even A1/A2
   still benefit from it (a compromised or shared tailnet/Access session
   shouldn't be able to hammer the endpoint either).
2. **Don't try to make `/api/refresh` safe under a fully-public (A3-shaped)
   model by hardening the token — separate its boundary instead.** No
   amount of tightening the current token helps, because the fundamental
   problem is that the app has no operator concept to check the token
   *against*. Two credible directions, for #27/#28 to decide together:
   - **(a) Move `/api/refresh` off the public request path entirely.**
     Trigger it via a scheduled job or a direct script invocation using
     a real secret held only by the operator (an environment variable,
     never shipped to any browser) — matching `SPECIFICATION.md`'s own
     already-anticipated "optional scheduled final-deadline report"
     phase. The "Refresh now" browser button either goes away for public
     visitors or becomes a no-op/hidden for anyone without that
     operator-only credential.
   - **(b) Put only `/api/refresh` behind an A1/A2-shaped restricted
     boundary while leaving the rest of the app (lookups, profile saves)
     on A3.** e.g. a Cloudflare Access rule scoped to the `/api/refresh`
     path specifically, rather than the whole origin — avoids
     reintroducing a login wall for the no-signup experience #46/#45
     were built around, while still solving the operator-distinguishing
     problem for the one action that actually needs it.
   (a) is less infrastructure-dependent and fits the zero-third-party-
   dependency ethos better; (b) needs no `server.py` change at all if #27
   already lands on a platform with path-scoped access rules. Not
   picking one here — this is #27's audience decision to make, informed
   by this finding.
3. **No action needed for findings 3, 4, 6** — already solid, confirmed
   directly against the current code rather than assumed.
4. **Findings 1 (Host/Origin target) stay explicitly blocked on #27's
   audience choice**, as already correctly noted there — re-confirmed,
   not re-litigated.
5. **Flag the #27 plan doc's "Decision so far" section (Axis A
   reasoning) as needing a follow-up correction** — its stated grounds
   for ruling out A1 and downgrading A2 no longer hold now that #44/#45
   shipped without any identity system; the *conclusions* may still be
   right (open reach was the point of #46/#45, and A1/A2 both reintroduce
   friction that contradicts it), but the doc's own reasoning should say
   why, not point at a per-user-profile gap that's since been closed.
   Worth a short update pass on `plans/issue-27-cloud-hosting.md` before
   #27 is actually shipped, not urgent enough to block this issue's own
   conclusions above.
