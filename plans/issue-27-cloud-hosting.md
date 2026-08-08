# Cloud hosting for the FPL dashboard (issue #27)

## Context

The dashboard currently only runs on `127.0.0.1`: `create_server()` in [src/fpl_intel/server.py](../src/fpl_intel/server.py) raises `ValueError` if asked to bind anywhere else, and the whole app leans on that boundary — `do_GET`/`do_POST` trust the `Host`/`Origin` headers only because nothing outside the machine can reach the port at all. Issue #27 asks what it would take to make the dashboard reachable without a laptop running it locally, and issue #28 (security review) already reviewed `server.py` against that goal and is blocked on this issue's decision for two of its six review areas (storage/multi-tenancy, and whether the bearer token needs to become real per-user auth).

The app itself, verified directly from the code:
- **Zero third-party dependencies.** `src/fpl_intel/*.py` imports only stdlib (`http.server`, `urllib.request`, `json`, `fcntl`, etc.) — no `requirements.txt`/`pyproject.toml` exists because none is needed today.
- **Flat-file, single-tenant storage.** `config/user-profile.json` (gitignored, one file, one manager profile) and `data/*.json` (mix of tracked reference data and gitignored generated snapshots, see [generation.py](../src/fpl_intel/generation.py)'s `publish_generation`/`resolve_artifact`) are the entire persistence layer. There is no concept of a user account anywhere in the code.
- **`fcntl`-based file locking** (`refresh.py`'s `project_refresh_lock`) assumes a real POSIX filesystem shared across requests — this rules out ephemeral/serverless compute without a rewrite, not just as a style preference but structurally.
- **#28's sharpest finding**: `/dashboard.html`'s `do_GET` embeds the live refresh token directly into the HTML with only a `Host`-header check gating it — not real authentication. Today that's safe purely because of the OS-level localhost boundary; the moment this is reachable from outside the machine, that token is visible to anyone who loads the page.

## Structural questions found before evaluating hosting candidates

1. **Any option requiring a real datastore (not files-on-disk) introduces this app's first-ever third-party dependency.** That's a meaningfully bigger step than picking a VM, worth weighing deliberately rather than defaulting into it.
2. **"Reachable without running locally" does not necessarily mean "reachable by the public internet."** The issue as filed jumps straight to comparing public cloud hosts, but a private-network solution (nobody outside your own devices can reach it at all) satisfies the literal ask just as well, for a fraction of the cost and almost none of #28's public-hosting risk. This turned out to be the single biggest fork in the decision tree, and the issue doesn't resolve it — see "Open question," below.

## Candidate operationalizations

Two independent axes: **who can reach it** (A), and **where the always-on compute/storage lives** (B). Every A choice pairs with every B choice.

### Axis A — audience / exposure model

- **A1. Private-only, via Tailscale.** A WireGuard mesh VPN between your own devices and the box running the app; nothing outside your tailnet can reach it at all, full stop. As of April 2026 Tailscale's free "Personal" tier covers 6 users and **unlimited devices**, genuinely free indefinitely, no credit card. Code change needed: update the `Host`/`Origin` allowlist in `server.py` to the tailnet hostname instead of `127.0.0.1` — the token-in-markup design stays exactly as safe as it is today, because the network boundary is still doing the real work, just a bigger boundary than "this one laptop."
- **A2. Named few, via Cloudflare Tunnel + Access.** Cloudflare Tunnel exposes a local service to the internet without opening a port, free on every plan including unlimited tunnels/custom domains on Cloudflare DNS. Cloudflare Access sits in front of it and requires a login (Google/GitHub SSO, or email one-time-PIN with zero setup) before any request reaches your origin — free for up to 50 users. This gets **real per-person authentication for $0 in engineering effort**, directly fixing #28's unauthenticated-token finding without writing a login system, because Cloudflare's edge does the auth, not the app. Reachable from any browser, no VPN client needed, unlike A1.
- **A3. Fully public, no login gate — anyone with the URL.** Only option that actually needs #28's full "needs work before public hosting" list built: real per-user auth in the app itself (a static bearer token in markup isn't safe once truly public), plus likely a move to per-user storage since `config/user-profile.json` has no concept of "whose profile." This is the option the issue's original research implicitly assumed, but it's also the only one of the three that requires a real rewrite rather than a hosting choice.

### Axis B — where the always-on compute/storage lives (compatible with A1 or A2; A3 more plausibly wants B6)

- **B1. Hardware you already own** (home PC, Raspberry Pi). $0 marginal cost. Storage is just local disk, unchanged from today. Uptime depends on your home power/internet; not "cloud hosting" in the literal sense, but fully satisfies "reachable without your laptop running it."
- **B2. Fly.io — small VM + persistent volume.** The issue's original recommendation, but verified pricing is higher than what it cited: shared-cpu-1x/256MB is $2.02/mo alone, but a realistic single-app setup (1 CPU/1GB + a 10GB volume + dedicated IPv4) runs **$10-20/mo**, not the originally estimated $2-5/mo + $0.15/GB — dedicated IP and bandwidth aren't optional line items in practice. Volume mount means the app's file-based writes work basically unchanged.
- **B3. Raw VM — Hetzner or AWS Lightsail.** Cheapest cloud compute with storage bundled in (no separate volume billing for a tiny app): Hetzner CX23 is **€5.49/mo (≈$6)** as of August 2026 (up from ~€4 after a June 2026 price increase) with 40GB included; Lightsail's cheapest nano is **$3.50/mo** IPv6-only ($5/mo with a public IPv4). Trade-off unchanged from the issue's original framing: you own OS patching, the process supervisor (systemd unit), and TLS (Caddy/nginx + Let's Encrypt) yourself — though under A1/A2 you may not need public TLS at all (Tailscale handles its own encryption; Cloudflare Tunnel terminates TLS at Cloudflare's edge), which removes one whole piece of that ops burden.
- **B4. Railway — Hobby tier, $5/mo.** Supports volumes, comparable fit to B2, no free tier since 2023/2026 pricing changes.
- **B5. Render.** Free tier **cannot attach a persistent disk at all** — confirmed this means `config/user-profile.json` and every generated `data/*.json` snapshot would not survive a restart or the 15-minute spin-down/redeploy cycle, i.e. the free tier structurally breaks this app's storage model, not just "inconvenient cold starts." Starter ($7/mo) is required just to get a disk.
- **B6. Serverless (Cloud Run, Lambda).** Not recommended as a first choice, unchanged from the issue's original framing and reinforced by the dependency-count finding above: `fcntl` file locking and local JSON files don't survive between invocations, so this requires moving all state to a real datastore — the biggest rewrite of any option, and this app's first-ever third-party dependency, for a tool the issue itself repeatedly describes as low-traffic and single/few-user.

## Recommendation (superseded — see "Decision so far" below)

**Answer the audience question (Axis A) first — it determines almost everything else, including how much of #28 needs to be resolved before shipping anything.**

- If the goal is personal remote access only (e.g. checking from your phone, nobody else ever needs in): **A1 (Tailscale) + B1 if you already have an always-on device at home, otherwise B3 (cheapest VM, ~$4-6/mo)**. Near-zero code change (just the Host/Origin allowlist), no new auth to build, and most of #28's "needs work before public hosting" list becomes moot because the network itself was never public — it can stay open as a lower-priority hardening pass rather than a blocker.
- If the goal is sharing with a specific small group (e.g. a mini-league): **A2 (Cloudflare Tunnel + Access) + B1 or B3**. Still $0 in engineering effort for real per-person login (Cloudflare's edge does it), reachable from any browser without installing anything, and it directly closes #28's sharpest finding without touching `server.py`'s auth model at all. Keep #28's other items (rate limiting, per-connection timeouts, Host/Origin updated to the tunnel hostname) as real hardening, since Access limits *who* reaches the origin but doesn't replace basic origin-side protections.
- **A3 (fully public, no login) is not recommended at this project's current scope.** It's the only path that forces both a real in-app auth system and likely a per-user storage rewrite — disproportionate for a tool the issue itself frames as low-traffic and single/few-user throughout. Worth revisiting only if the actual goal changes to "share broadly with strangers," which is a different project.
- Whichever B (compute) is picked, it's a separate, lower-stakes decision from A — B1 for $0 if you have a spare always-on device, otherwise B3 (Hetzner/Lightsail) for the cheapest cloud option with bundled storage, ahead of B2/B4 (comparable cost, more moving parts with a separate volume) and well ahead of B5 (free tier structurally incompatible with this app) or B6 (disproportionate rewrite).

## Decision so far (2026-08-08): each person gets their own team, to allow scaling to everyone

The user confirmed the goal is real per-user profiles ("each person wants their own team, so that there is flexibility to scale this to everyone"), not one shared profile behind a login gate. That rules A1 (Tailscale) out — a private VPN still only protects the *network*, it does nothing about the app having exactly one manager profile — and means A2 (Cloudflare Access) can only be a partial answer, addressed below. It also surfaces a structural finding the original issue didn't anticipate:

### What "per-user" actually requires — verified against the refresh pipeline

`_refresh_project_unlocked()` in [refresh.py](../src/fpl_intel/refresh.py) does not treat "the shared FPL universe" and "this manager's team" as separable today — it reads the single `config/user-profile.json`, uses its `team_id` to call `collect_public_manager`/`fetch_manager_event_picks` against the *real FPL API for that one manager*, and bakes the result into the same `dashboard-state.json`/`dashboard.html` as the shared bootstrap/fixtures/transfers/model-performance data. One refresh call produces one complete generation for one manager. There is no per-request or per-visitor concept anywhere in the server.

So real per-user support is a genuine second phase of work, not a hosting choice:

- **Phase 1 — identity: self-serve OAuth, confirmed.** The user chose open self-serve signup over an invite/allowlist model, which rules out Cloudflare Access as the identity layer (it's allowlist-based, not self-serve) and means "Sign in with Google" (or GitHub) gets built into the app: an OAuth callback + a signed, stateless session cookie in `server.py`. No passwords are ever stored — Google/GitHub does the actual authentication, the app just receives a verified email back. The session cookie itself needs no server-side storage (HMAC-signed JSON with an expiry, verified on each request) — this is orthogonal to Phase 2's profile storage question below.
- **Phase 2 — per-user profile storage: where the data actually lives.**
  - **Recommended: a single SQLite file** (`data/profiles.db` or similar), one row per user keyed by the email OAuth hands back. Python's stdlib includes `sqlite3` — this keeps the "zero third-party dependencies" property completely intact, while fixing two real problems flat files would hit at self-serve scale: (a) SQLite handles per-row locking properly, so concurrent writes from *different* users no longer contend on one global lock the way today's single `config/user-profile.json` + one `fcntl` lock does; (b) it's one file to back up/migrate instead of an unbounded number of small ones. Still lives on ordinary local disk — same persistent-volume requirement as today, on whichever Axis B compute is picked (still rules out Render's free tier and pure serverless, same constraint as before).
  - **Not recommended yet: a hosted database** (Postgres via Supabase/Neon/RDS, etc.). This is the app's first real third-party dependency and a new piece of infrastructure to run and pay for. It's the right move *if* this later needs multiple app instances writing concurrently (horizontal scaling) or genuinely high write concurrency — neither applies to "a personal tool that grew via self-serve signup" at a scale one small box can serve. Worth revisiting only when SQLite's single-file-on-one-box model actually becomes the bottleneck, not upfront.
- **Phase 3 — decouple the refresh pipeline.** Split `_refresh_project_unlocked()`: the shared half (bootstrap/fixtures/transfers/model-performance) keeps refreshing on the existing on-demand/manual cadence, unchanged, and produces one shared `dashboard-state.json` as it does today. The manager-specific half (`collect_public_manager`, `fetch_manager_event_picks`, the risk-profile-driven decision-center recommendations) moves to compute-at-request-time for whichever user is logged in, using the already-fetched shared data as input, rather than being baked into one static generation. This is the biggest engineering piece of the whole plan — bigger than anything in the original Axis B (hosting) comparison.
Phases 1-3 need to happen regardless of which Axis B hosting choice is picked — they're app changes, not infrastructure changes. Axis B (Fly.io / raw VM / Railway / Render / own hardware) still stands as written above once the app itself supports multiple users; Render's B5 free tier is now decisively out regardless of that finding since it can't hold persistent per-user files at all.

## Decisions confirmed (2026-08-08)

- **Growth model: open self-serve signup**, not an invite list — Phase 1 is "Sign in with Google/GitHub" built into the app, not Cloudflare Access.
- **Profile storage: a single SQLite file**, one row per user, on ordinary local disk — no hosted database, no new dependency, until it's demonstrably outgrown.

No blocking open questions remain. Next step is scoping Phases 1-3 as implementable issues (see "Next steps," below) and, separately, picking Axis B compute once the app itself supports multiple users.

## Next steps

This plan intentionally stops short of implementation-ready detail for Phases 1-3 — each is substantial enough to deserve its own issue and its own `ship-issue` pass rather than being built as one large, hard-to-review change:

1. **Auth**: OAuth ("Sign in with Google") + signed session cookie in `server.py`.
2. **Storage**: SQLite-backed profile store, replacing the single `config/user-profile.json` / `_default_profile_action`.
3. **Refresh pipeline split**: shared generation (unchanged cadence) vs. per-user, request-time manager computation.
4. Only after 1-3 ship: pick Axis B compute (own hardware vs. Fly.io vs. raw VM vs. Railway) and stand up real hosting.

Recommend filing 1-3 as separate issues once ready to start building, in that order — each is a real dependency of the next (storage needs identity to key on; the refresh split needs somewhere to read/write per-user state).

## Interaction with #28 (security review)

- A1/A2 resolve #28's "sharpest finding" (unauthenticated token in the GET response) **without any change to `server.py`'s auth model** — the network/edge boundary does the job the OS-level localhost boundary does today, just at a different scope.
- Regardless of A/B choice, `server.py`'s Host/Origin allowlist (`_has_trusted_host`, the Origin check in `do_POST`) must move from hardcoded `127.0.0.1:{port}` to whatever the real reachable hostname becomes (tailnet name for A1, tunnel hostname for A2) — this was already flagged in #28 as needing to move in lockstep with #27's decision, not just get re-confirmed.
- #28's remaining "needs work" items (no time-based rate limit on `/api/refresh` beyond the concurrency-of-1 lock, no per-connection timeout on `ThreadingHTTPServer`) are cheap, generically-good hardening independent of which A/B is chosen, and worth doing regardless — just not necessarily a hard blocker under A1/A2 the way they would be under A3.

## Sources

Pricing verified 2026-08-08 (issue's original research was dated July 2026; Fly.io and Hetzner both moved since):
- [Fly.io Resource Pricing](https://fly.io/docs/about/pricing/)
- [Render: platforms with a real free tier for developers in 2026](https://render.com/articles/platforms-with-a-real-free-tier-for-developers-in-2026)
- [Tailscale free plan changes, April 2026](https://pbxscience.com/tailscale-overhauls-pricing-free-plan-now-supports-six-users-with-unlimited-devices/)
- [Cloudflare Tunnel + Access, self-hosted app authentication](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)
- [Hetzner Cloud pricing after the April/June 2026 increase](https://www.bitdoze.com/hetzner-cloud-cost-optimized-plans/)
- [AWS Lightsail pricing 2026](https://www.cloudzero.com/blog/amazon-lightsail-pricing/)
