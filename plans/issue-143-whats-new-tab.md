# Issue #143 -- Daily automated "What's New" tab

Researched 2026-08-11, updated 2026-08-11 with a UX mockup review. Issue
asks for a "What's New" tab in the live dashboard, populated by a daily
automated job that generates "visually creative" release notes for
whatever shipped the previous day, with collapsible per-date sections
(latest expanded, rest collapsed) and a no-op day producing no entry.
See the issue's "Decided (2026-08-11)" section for the 7 concrete
requirements this plan operationalizes. The "Candidate operationalizations"
section below resolves the three sub-questions originally left open; the
"## UX design" section further down parses a visual mockup the user
supplied afterward, which introduces real new scope (email subscription,
RSS feed, search/filter, unread tracking) not yet decided -- flagged
there for review, not folded in as settled.

## Context

`RELEASE_NOTES.md` exists (issue #112) but nothing in the app reads it
-- it's a GitHub-viewer-only snapshot doc, invisible from the live
Railway dashboard. Issue #143 (this issue) already decided the shape:
dated entries, a new sidebar tab, daily automated generation, no-op if
nothing shipped. What's not yet decided is *how* three pieces work:

1. What source of truth defines "what shipped yesterday."
2. What generates "visually creative" content, and what happens when
   that generation is unavailable/fails on a day something did ship.
3. Where the generated content is stored, and how the live dashboard
   picks it up -- this turned out to be the load-bearing structural
   question (see below).

## Structural constraints found before evaluating candidates

**Every existing scheduled workflow in this repo is read-only against
the repo itself.** Checked directly:

```
$ grep -n "permissions:" -A3 .github/workflows/*.yml
deadline-reminder.yml:   contents: read
live-regression-check.yml: contents: read
scheduled-refresh.yml:   contents: read
```

None of the three existing scheduled GitHub Actions jobs
(`deadline-reminder.yml`, `live-regression-check.yml`,
`scheduled-refresh.yml`) ever commits to the repo -- all three instead
call back into the live Railway server over HTTP (`ARCHITECTURE.md`'s
documented pattern: "GitHub's own runners... call back into the live
Railway server over HTTP rather than sharing its filesystem"). A daily
job that commits a new dated Markdown file straight to `main` would be
the **first** scheduled automation in this repo's history with
`contents: write`, and the first to bypass this project's otherwise
universal human-reviewed-PR discipline (every worktree/branch/PR/merge
skill in this repo -- `ship-issue`, `plan-issue`, `merge-pr` -- treats a
merge to `main` as something only an explicit human instruction
triggers). That's not disqualifying on its own -- point 1 of the
issue's decided direction already asks for a job that "just publishes"
with no review gate, so *some* form of unreviewed automated write is
inherent to the ask -- but it's a real precedent-setting choice worth
naming explicitly rather than treating as a detail of "which tool writes
the file." See Candidate C below.

**DST-aware "8 AM ET" needs the same care `deadline-reminder.yml`
already had to get right, not a hardcoded UTC cron time.** GitHub
Actions' `schedule.cron` is fixed UTC and does not itself understand
"Eastern Time" -- a naive single `cron: "0 12 * * *"` (8 AM EDT) would
silently become 7 AM ET once the US falls back to EST, and stay wrong
until manually edited twice a year. `deadline-reminder.yml` never faced
this specific problem (it runs hourly and checks each team's own
deadline window in Python via `zoneinfo`, so it's DST-correct by
construction), but the same underlying idea -- schedule broader than the
target, gate precisely in Python -- transfers directly: run the workflow
at both plausible UTC times (12:00 UTC and 13:00 UTC, i.e. 8 AM EDT and
8 AM EST) and have the script itself check the actual `America/New_York`
wall-clock hour, no-op-ing the "wrong" trigger for the current DST
regime. This reuses the exact no-op discipline point 1 of the issue
already asks for (for a second reason -- wrong DST offset -- alongside
"nothing merged yesterday"), rather than inventing a second scheduling
mechanism.

**This repo has exactly one precedent for calling an LLM, and it's
directly reusable.** `src/fpl_intel/news_signals.py` (Phase 5, gated but
built) is provider-agnostic: raw HTTPS calls, no vendor SDK, selected via
`FPL_INTEL_LLM_PROVIDER` (`"claude"` or `"openai_compatible"`), configured
via `FPL_INTEL_LLM_MODEL`/`FPL_INTEL_LLM_API_KEY`/`FPL_INTEL_LLM_API_BASE`
env vars, and fails safe -- no key configured or the call errors -> zero
signals, pipeline continues normally. That fail-safe posture doesn't
transfer as-is, though: `news_signals.py`'s "fail to nothing" is safe
*because the signals are optional enhancements* -- the projection model
runs identically without them. A release-notes generator failing on a
day something genuinely shipped has no equally safe silent option; see
Candidate B below for how this plan proposes to handle it.

## Candidate operationalizations

### A. Source of truth for "what shipped yesterday"

**A1. Merged PRs** (`gh pr list --state merged --search "merged:<date>"`,
or the GitHub API equivalent). This repo's own working convention (every
`ship-issue`/`merge-pr` run) is that a PR *is* the unit of shipped
change -- title, body, and linked issue all already exist and are
human-written, giving the generator real editorial material instead of
raw diffs. "Nothing merged that day" is a trivial, unambiguous no-op
check (point 1).

**A2. Commits on `main` in the date's UTC/ET window**
(`git log --since --until`). Captures anything landed outside the PR
flow, but this repo's actual history has none of that -- every recent
merge in this session went through `gh pr merge --squash`, which
collapses each PR to exactly one commit on `main` whose message is the
PR title/body. For this repo specifically, A2 carries the same
information as A1, just accessed one layer down, with strictly less
structure (no PR number, no linked issue, no reviewable body separate
from the commit message).

**Recommendation: A1 (merged PRs).** Richer input for the generator,
a natural no-op check, and matches how this repo already talks about
"what shipped" everywhere else (PR links in commit messages, `Fixes
#<N>` closing keywords). Needs `pull-requests: read` added to the new
workflow's permissions (a small, read-only addition, not the
`contents: write` precedent discussed above).

### B. What generates "visually creative" content, and its failure policy

**B1. Reuse `news_signals.py`'s provider-agnostic LLM caller**, pointed
at a prompt that takes the merged PRs' titles/bodies for the date and
returns structured, styled release-note copy (e.g. a short headline,
a one-line summary per change, maybe a suggested emoji/tag per category
-- concrete prompt/output-shape design is implementation, not this plan).
Matches this repo's only existing LLM precedent instead of inventing a
second one; same env-var configuration surface
(`FPL_INTEL_LLM_PROVIDER`/`_MODEL`/`_API_KEY`/`_API_BASE`) operators
already may have set for Phase 5.

**B2. Failure policy -- this is the part `news_signals.py` doesn't
already answer.** If the LLM call fails or is unconfigured on a day PRs
*did* merge, silently producing nothing (news_signals.py's approach)
means that day's real changes never get an entry at all -- a silent gap
a human would have to notice and backfill by hand. Two sub-options:
  - **B2a. Template fallback.** If the LLM call fails, fall back to a
    plain, deterministic rendering directly from the PR titles/bodies
    (no styling flourish that day, but a complete, accurate entry
    exists). Mirrors this repo's established fail-safe pattern elsewhere
    (`send_deadline_reminder.py`'s SMTP-deferred-to-point-of-send,
    `news_signals.py`'s zero-signals-not-a-crash) -- degrade gracefully,
    never lose real information.
  - **B2b. Skip and retry.** Leave the day unpublished and let the next
    day's run pick it up (would need to widen its own "yesterday" window
    to "since the last successful publish," not just the prior 24h, to
    avoid losing that day's entry entirely).

**Recommendation: B1 + B2a.** A template fallback guarantees the tab
never silently drops a real shipped day, matching this repo's
consistent "degrade, don't disappear" posture. B2b's retry-window
complexity buys nothing B2a doesn't already give more simply.

### C. Where content is stored and how the dashboard reads it

**C1. Git-committed dated file(s), read by `server.py`/`dashboard.py`
at request time (matching every other tab's live-render pattern).**
Requires the new workflow to carry `contents: write` and push a commit
to `main` -- the first scheduled automation to do so (see Structural
constraints above). If the live tab rendered directly from this
git-committed copy, viability would hinge on Railway auto-deploying on
push to `main` -- but see C3 below, which sidesteps that dependency
entirely by not using this file as the live-render source.

**C2. A new authenticated write endpoint, matching the existing
operator-token pattern.** `POST /api/refresh` and `POST
/api/archive-team-forecast` are both already gated by the same
`X-Refresh-Token` header (`server.py`, `secrets.compare_digest`) rather
than a dedicated per-endpoint secret. A new `POST /api/release-notes`
endpoint, gated the same way, would let the daily job push generated
content straight to the running server, which persists it to a new file
on the same Railway volume `profiles.db`/the JSON snapshots already use
(e.g. `data/release-notes.json`) -- no new secret to provision (reuses
`FPL_INTEL_REFRESH_TOKEN`, already on both Railway and GitHub Actions),
content is live immediately (no deploy latency), and it matches this
repo's established scheduled-workflow architecture (call back over
HTTP). Trade-off standing alone: not git-diffable, since it lives in
the gitignored, volume-persisted `data/` directory.

**C3 (decided). Both C1 and C2 -- POST for the live tab, a git-tracked
folder for durable documentation.** The user wants release notes
git-tracked as "an important piece of documentation" (not just a live
UI feature), which C2 alone doesn't give -- but also doesn't want the
live tab's correctness depending on Railway's deploy timing, which a
C1-only design would. Doing both resolves this and removes the
"confirm Railway auto-deploy" blocker entirely: the live "What's New"
tab never reads the git folder, so it doesn't matter when or whether a
given commit has actually been deployed.

Concretely, the daily job, after generating one day's content:
1. **`POST /api/release-notes`** (C2) -- the live-serving write. This
   step is required; if it fails, the job fails loudly (same posture as
   every other real misconfiguration in this repo's scheduled jobs --
   `ConfigError`, non-zero exit, visible in the Actions run).
2. **Commit a new dated file to a new top-level `release-notes/` folder**
   (e.g. `release-notes/2026-08-12.md`) **and push to `main`** -- the
   archival write. Needs `contents: write` added to this workflow's
   permissions specifically (still `contents: read` for the other three
   -- this is the first, deliberate exception, not a blanket repo change).
   Recommend treating this step as best-effort relative to step 1: log a
   warning if it fails (matching the reminder job's existing "kept out
   of the public log; see the uploaded artifact" convention for
   non-fatal detail) rather than failing the whole run, since the live
   tab already reflects the day's notes via step 1 regardless.

This does mean accepting the new bot-commit-to-`main` precedent flagged
in Structural constraints above -- the user has confirmed that's an
acceptable, intentional trade for having real git history of release
notes, not an overlooked side effect.

## UX design (mockup reviewed 2026-08-11)

The user supplied a two-page visual mockup
(`FPlIntelligence_WhatsNewDesign.pdf`) of the "What's New" tab. Parsed
in full below -- this is not implemented, and several elements are new
scope beyond the "Decided (2026-08-11)" 7 points and everything
investigated above. Flagged explicitly rather than silently folded in,
per the user's "wait for my review" instruction on this pass.

**Layout, matching what's already decided:**
- New sidebar nav item, "What's New", positioned last (after "Contact
  Us") -- consistent with `dashboard.py:32-34`'s existing `data-view`
  button list.
- Header: small-caps eyebrow "RELEASE NOTES", then an `<h1>` "What's
  New", then a one-line description: *"What shipped, day by day.
  Generated automatically each morning from the previous day's merged
  changes -- quiet days simply don't get an entry."* -- this description
  itself is good, reusable copy for the tab's static intro text.
- Dated entries render as collapsible cards, newest first, exactly
  matching the issue's points 6-7: **Mon, Aug 11 ("Yesterday") starts
  expanded; every older date starts collapsed** (Fri Aug 8, Tue Aug 5,
  Thu Jul 30, each showing "N days ago" and a collapsed chevron).
- Each card header, expanded or collapsed: relative date label
  ("Yesterday" / "N days ago"), a **synthesized one-line headline** for
  the day (not a raw PR title -- e.g. "Sharper filters for preseason
  movement tracking" spans three unrelated PRs), and a "N changes"
  count badge.
- Expanded card body: a short **intro paragraph** synthesizing the
  day's theme (2-3 sentences, reads as one coherent narrative across
  that day's PRs, not a concatenation), then one block per change:
  a **category tag** (colored chip: "Feature" / "Data" / "Fix" shown in
  the mockup), a bold one-line title, and a muted one-line description.
- Gap transparency: *"No entry for Aug 9-10 or Aug 6-7 -- nothing
  merged those days."* -- an explicit, muted note acknowledging skipped
  dates, so a gap reads as confirmed-quiet rather than possibly-broken.
  Directly reflects this plan's no-op design (point 1) back to the user
  in the UI itself, not just as an absence.
- End-of-list marker: *"That's the full history so far."*

**Worked example from the mockup, preserved verbatim as a concrete
tone/style reference** (not real data -- illustrates the target voice
for the generation step, Candidate B):

> **Mon, Aug 11** -- Yesterday -- "Sharper filters for preseason
> movement tracking" -- 3 changes
>
> Club movement just got easier to scan: the single messy filter row
> split into three focused controls, and every incoming transfer now
> carries a reconciled FPL player ID the moment it lands -- no more
> waiting for the next refresh to match a name to an official record.
>
> - **[Feature]** Club movement filters split into Direction, Movement
>   type, and Date -- Previously one combined control; each now narrows
>   independently and combines with search.
> - **[Data]** First-party transfer feed reconciles FPL player IDs on
>   ingest -- Confirmed movements match official prices, clubs, and
>   positions as soon as they arrive, not on the next scheduled refresh.
> - **[Fix]** Deadline banner no longer flashes before the 2026/27 feed
>   is live -- The banner now waits for a real deadline before rendering
>   anything.
>
> Collapsed older entries (headline + count only, per the
> expand/collapse rule): "Shadow models now score every refresh" (Fri,
> Aug 8, 2 changes), "Declare a preseason draft squad" (Tue, Aug 5, 2
> changes), "A way to reach the team, and a reminder before deadlines"
> (Thu, Jul 30, 2 changes).

**Visual style -- checked against the app's actual CSS
(`src/fpl_intel/dashboard.css`), two findings:**
- The mockup's dark navy background **already matches** this app's
  existing default theme tokens (`--bg: #08101f`, `--panel: #101b2e`,
  `:root` block) -- no new theme needed, the tab should just use the
  existing tokens like every other view does. The app also already
  supports a light theme (`:root[data-theme="light"]`) -- the new tab
  needs to work in both, same as every other view, not just the dark
  mockup shown.
- **Open discrepancy, not yet resolved:** the mockup's buttons and
  active-filter-chip state read as purple/indigo, but this app's actual
  accent color is green (`--accent: #57dfae`, used everywhere else --
  active nav item, buttons, focus rings). Also, the app already has an
  established chip/badge color-token system
  (`--badge-setup/-info/-ready-*`, `--chip-neutral/-easy/-hard-*` in
  `dashboard.css`, used for e.g. fixture-difficulty chips) that the new
  `Feature`/`Fix`/`Data`/`Docs`/`Chore` category tags should extend
  rather than inventing an unrelated ad-hoc palette. **Needs a decision
  before implementation:** match the mockup's purple/indigo accent as a
  deliberate new color, or use the app's existing green `--accent` for
  visual consistency with every other tab. Not decided in this plan.

**New scope this mockup introduces, not covered by the "Decided" list
or the candidates above.** Resolved with the user 2026-08-11:

1. **Per-change category taxonomy -- decided: `Feature` / `Fix` /
   `Data` / `Docs` / `Chore`.** The mockup's three (`Feature`/`Fix`/
   `Data`) are the starter set; the user confirmed adding `Docs` and
   `Chore` so changes like this plan doc itself, or dependency/tooling
   housekeeping, have a natural bucket instead of being force-fit into
   one of the original three. The generation step (Candidate B) needs
   to assign one of these five to every change, including in the
   **B2a template fallback path**, which has no LLM to infer one --
   needs its own deterministic rule (e.g. keyword/path matching: a PR
   touching only `*.md`/`plans/` -> `Docs`, `tests/`-only or CI-only
   changes -> `Chore`, "fix"/"bug" in the title -> `Fix`, else
   `Feature`/`Data` by some further rule TBD) so the fallback path still
   produces a taggable entry, not an uncategorized one that breaks the
   filter UI. Exact rule ordering/precedence is implementation, not this
   plan.
2. **Two levels of generated content per day**, not one: a
   whole-day **headline + intro paragraph** (synthesized across every
   change that day) *and* a **per-change title + description** (one per
   PR). B1's original scope ("a short headline, a one-line summary per
   change") undersold this -- the day-level headline/intro is a
   separate synthesis step over the *set* of that day's per-change
   entries, not just the top item's title.
3. **Client-side search and category filtering** (a search box, "All /
   Feature / Fix / Data / Docs / Chore" chips). Both operate over
   content the server already sent down (same `data/release-notes.json`
   payload C2/C3 already produce) -- matches this codebase's existing
   client-side filtering pattern used by Player Explorer
   (`#player-search`, `#player-club-filter`) and Fixtures
   (`#fixture-club-filter`), not a new server endpoint.
4. **Email subscription -- decided: double opt-in**, matching
   `reminder_confirmation.py`'s existing pattern (issue #79) rather than
   trusting a submitted address outright. A capture card ("Get release
   notes by email... one email each time a new entry publishes") with
   an email field and Subscribe button triggers a confirmation email
   (mirroring `send_confirmation_email`'s shape: a confirm link, nothing
   enabled until it's clicked, expires if unused); only confirmed
   addresses ever receive a real release-notes email. Needs, beyond
   what the mockup shows: a persisted subscriber list (new store,
   likely `profiles.db`-adjacent given that's this project's existing
   home for exactly this shape of data -- opt-in state, confirmation
   tokens, per issue #79/#102's precedent), a send step triggered
   whenever the daily job actually publishes an entry, and an
   unsubscribe mechanism (not shown in the mockup, but required --
   every confirmation/notification email in this codebase already
   carries a way to stop receiving it, and this shouldn't be the
   exception).

**Declined for now (2026-08-11), at explicit user request:**

- **Unread tracking** (nav-item dot indicator + "Mark all as read").
  Not needed right now -- drop from this pass. If wanted later, the
  original design still holds: `localStorage`-based, comparing the
  newest entry's date against a stored "last seen" date, no new
  server-side state.
- **RSS/Atom feed** ("Prefer a feed reader? Copy feed URL"). Not needed
  right now -- drop from this pass. If wanted later: a new,
  unauthenticated `GET /api/release-notes.rss`-style endpoint rendering
  the same stored entries as a standard feed format, at request time --
  same "render at request time from stored data" pattern every other
  tab already uses, just a different output format. No dependency on
  anything else in this plan, so it's a clean, independent fast-follow
  whenever it's wanted.

None of the four remaining items (1-4 above) were part of the 7 decided
points or the Candidates A/B/C investigated earlier in this doc. They're
real, buildable, and consistent with this codebase's existing patterns
(client-side filters, double opt-in via `reminder_confirmation.py`'s
established shape, request-time rendering) -- but email subscription in
particular is a meaningfully larger, separately-risked piece of work
(new persisted subscriber data, a confirmation-email flow, an
unsubscribe path) than the read-only "tab that renders dated entries"
core of this feature.

**Open question still remaining for the user's review:**
- Should email subscription (item 4) ship in the same first
  `ship-issue` pass as the core tab (points 1-7 + Candidates A/B/C +
  category taxonomy/search/filter), or split out as a fast-follow issue
  once the core tab is live? It's the one piece here that touches real
  user data and a new confirmation-email flow, a different risk profile
  from the rest of this feature (which is entirely read-only rendering
  of generated content).

## Decided (2026-08-11): final calls, and slicing the build

- **Accent color: match the app's existing green `--accent`**, not the
  mockup's purple/indigo -- visual consistency with every other tab.
- **Scope for this first `ship-issue` pass: the core read-only tab
  only.** Dated entries, collapsible per-date sections, category tags
  (`Feature`/`Fix`/`Data`/`Docs`/`Chore`), client-side search/filter,
  the daily generation job, and the dual-write storage (Candidate C3)
  all ship now. **Email subscription is deferred to its own follow-up
  issue**, not this pass -- once `server.py` was actually inspected,
  every existing write endpoint in this codebase (`/api/reminder-opt-in`
  in particular) carries its own validation-error class, cooldown
  limiter, dependency-injection hook, and dedicated test suite; matching
  that same rigor for a new double-opt-in subscriber system is a
  separately-sized, separately-risked piece of work from "a tab that
  renders generated content," not a small addition to it. The mockup's
  email-capture card and RSS feed link are both left out of this pass's
  UI as a result (RSS was already declined earlier in this doc).

## Recommendation

1. **A1** -- source "yesterday" from merged PRs.
2. **B1 + B2a** -- generate via the existing `news_signals.py`-style
   provider-agnostic LLM caller; fall back to a plain templated entry
   (no LLM styling) rather than silently skipping a day with real
   changes.
3. **C3 (decided)** -- dual write: `POST /api/release-notes` (gated by
   the existing `FPL_INTEL_REFRESH_TOKEN`) for the live tab, persisting
   to `data/release-notes.json` on the Railway volume; **and** a commit
   to a new `release-notes/` folder on `main` for git-tracked
   documentation history. The live tab reads only from the POST/volume
   path, never the git folder, so it never depends on deploy timing.
4. **Bootstrap is a separate, one-time task**, not part of the ongoing
   daily job: migrate `RELEASE_NOTES.md`'s current content into the new
   `release-notes/` folder as the first ("yesterday") dated entry, via a
   normal human-reviewed PR -- and POST that same content once, by hand
   or via a one-off script invocation, so the live tab and the git
   archive start in sync.
5. Scheduling: two workflow cron triggers (12:00 UTC and 13:00 UTC,
   covering both EDT/EST 8 AM ET), gated by a Python-side
   `America/New_York` hour check inside the script itself -- the "wrong"
   trigger for the current DST regime no-ops, reusing the same no-op
   discipline point 1 already requires for "nothing merged."

**Confirmed with the user (2026-08-11):**
- Railway auto-deploys on every push to `main` -- not load-bearing for
  this plan either way (C3's live tab reads only the POST/volume path),
  but removes any remaining doubt about C1's own viability as a concept.
- No `FPL_INTEL_LLM_*` credentials exist anywhere yet (Phase 5 has never
  been activated) -- **provision a new API key specifically for this
  job**, not shared with Phase 5. Needs its own env var name distinct
  from `news_signals.py`'s (e.g. `FPL_INTEL_RELEASE_NOTES_LLM_*`, or
  reuse the same var names but document that activating Phase 5 later
  would then share this job's key unless a second one is added at that
  point) -- exact naming is implementation, not this plan, but the
  "separate key, not shared" decision itself is made.
