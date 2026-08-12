# Issue #143 -- Daily automated "What's New" tab

Researched 2026-08-11. Issue asks for a "What's New" tab in the live
dashboard, populated by a daily automated job that generates "visually
creative" release notes for whatever shipped the previous day, with
collapsible per-date sections (latest expanded, rest collapsed) and a
no-op day producing no entry. See the issue's "Decided (2026-08-11)"
section for the 7 concrete requirements this plan operationalizes; three
sub-questions were left explicitly open there and are what this doc
investigates.

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
constraints above). Assumes Railway's GitHub integration auto-deploys on
push to `main`, which this session's own history is consistent with
(every merged PR this session took effect on the live server with no
separate manual deploy step ever performed) but was not independently
re-verified for this plan -- **confirm this assumption before building**,
since C1's entire viability depends on it. If true, this keeps release
notes exactly where all this project's other prose already lives (git,
diffable, `MODEL.md`/`SPECIFICATION.md`/`IMPLEMENTATION_PLAN.md`-style),
at the cost of introducing the new bot-commit precedent.

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
repo's actual established scheduled-workflow architecture (call back
over HTTP, never touch git) with zero exceptions. Trade-off: release
notes are no longer git-history-diffable the way the rest of this
project's docs are -- they'd live in the gitignored, volume-persisted
`data/` directory instead (same durability caveat already documented for
`profiles.db`: a real, persistent-but-not-version-controlled Railway
volume, not ephemeral, but not `git log`-able either).

**Recommendation: C2, with one caveat.** C2 is a strictly smaller
change (no new repo-write precedent, no new secret, reuses the
established `X-Refresh-Token`-gated-POST-then-persist-to-volume shape
`/api/refresh`/`/api/archive-team-forecast` already use) and is
immediately live with no dependency on confirming Railway's deploy
behavior. The caveat: this is a genuine, if minor, departure from "every
piece of this project's prose lives in git" -- worth the user's explicit
sign-off given issue #143's own point 3 ("move/re-create... in repo")
frames the *bootstrap* migration as an in-repo, human-reviewed PR (which
this plan treats as a one-time, separately-scoped task, not the ongoing
daily job's mechanism -- see Recommendation below). If the user would
rather keep the ongoing daily entries git-tracked too for auditability,
C1 is the fallback, contingent on confirming Railway's auto-deploy
behavior first.

## Recommendation

1. **A1** -- source "yesterday" from merged PRs.
2. **B1 + B2a** -- generate via the existing `news_signals.py`-style
   provider-agnostic LLM caller; fall back to a plain templated entry
   (no LLM styling) rather than silently skipping a day with real
   changes.
3. **C2** -- new `POST /api/release-notes` endpoint, gated by the
   existing `FPL_INTEL_REFRESH_TOKEN`, persisting to a new
   `data/release-notes.json` on the same Railway volume `profiles.db`
   already uses. Dashboard's new "What's New" tab renders from that file
   at request time, same as every other tab.
4. **Bootstrap is a separate, one-time task**, not part of the ongoing
   daily job: migrate `RELEASE_NOTES.md`'s current content into the new
   dated-entry format as the first ("yesterday") entry, via a normal
   human-reviewed PR -- matching issue #143 point 3's "in repo" framing
   for that one-time step specifically, distinct from how the recurring
   job persists content (C2, not git).
5. Scheduling: two workflow cron triggers (12:00 UTC and 13:00 UTC,
   covering both EDT/EST 8 AM ET), gated by a Python-side
   `America/New_York` hour check inside the script itself -- the "wrong"
   trigger for the current DST regime no-ops, reusing the same no-op
   discipline point 1 already requires for "nothing merged."

**Before building, confirm with the user:**
- Railway's auto-deploy-on-push-to-`main` behavior (only load-bearing if
  C1 is chosen instead of C2).
- Whether C2's git-untracked, volume-persisted storage is an acceptable
  trade for avoiding the new bot-commit-to-`main` precedent, or whether
  git-tracked history matters enough here to prefer C1.
- The LLM provider/credentials to actually use for B1 (whether existing
  `FPL_INTEL_LLM_*` env vars, if any are already set for Phase 5
  scaffolding, should be reused, or a separate credential is wanted so
  this job's usage/cost is distinguishable from any future Phase 5
  activation).
