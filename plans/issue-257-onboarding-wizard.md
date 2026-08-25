# Guided "Getting Started" onboarding flow (issue #257)

## Context

Issue #257 asks for a guided flow sequencing five existing tabs (My Profile, Draft Squad, Decision
Center, Player Explorer/Transfers & News, What's New) for a first-time, non-FPL-literate visitor, and
left two things explicitly open: (a) how per-step completion is tracked/persisted, and (b) whether
the flow is blocking or advisory. Both are resolved below with a recommendation, plus one additional
finding surfaced during investigation that changes the step-1 framing the issue's own draft assumed.

## Finding: neither team-ID entry point requires a real FPL account, and they already share state

Checked directly against the server handlers, since it bears on how heavy step 1 needs to be. Both
`/api/profile` (`profile.py:42-56`) and `/api/draft-squad` (`draft_squad.py:33-43`) validate `team_id`
identically -- an integer in `1..99_999_999` -- and neither fetches or checks it against FPL's public
API before accepting the save; `default_profile_action` (`profile.py:109-132`) and
`default_draft_squad_action` (`draft_squad.py:58-83`) both just key a `profiles.db` write on whatever
number was submitted. So a visitor with no real FPL account yet can complete the entire flow,
including a saved Draft Squad, with a made-up placeholder ID -- consistent with #257's own "Not in
scope: explaining FPL rules" framing, since the tool doesn't require FPL account creation as a
prerequisite either.

The two entry points also already share client state: Draft Squad's own team-ID field pre-fills from
`state.profile.team_id` (`draft-squad.js:229`), the same field My Profile's save populates. So
whichever tab a visitor completes first, the other's team-ID field is already filled in when they get
there -- nothing about the wizard needs to duplicate that wiring, just needs to make sure step 1's
save is the thing that sets `state.profile.team_id` in the first place.

**This changes the step-1 framing.** #257's draft body listed My Profile (timezone/risk profile) as
step 1 and Draft Squad ("make a team") as step 2. But My Profile's timezone/risk-profile fields are
preference-tuning for someone who already has a squad opinion -- not what "make a team" (the friend's
own words) actually refers to. Draft Squad is the literal "make a team" step; entering a team ID
there is sufficient to unlock the rest of the flow (Decision Center et al. gate on `connection_status`,
which resolves once any team_id is set -- see next section). Recommend the wizard's step 1 be framed
as "start your team" and land on Draft Squad directly, with My Profile repositioned as an optional
step for tuning preferences (timezone reminders, risk profile) once a team exists, not a hard
prerequisite before it.

## Question (a): how is per-step completion tracked/persisted?

Three existing persistence mechanisms already coexist in this codebase, each reserved for a different
kind of state:

- **`profiles.db`** (`profiles.py`, `_migrate_schema` pattern) -- reserved for state that feeds
  model behavior or squad legality: timezone, risk profile, draft squad membership, confirmed free
  transfers. Everything stored there is either read by the recommendation engine or displayed as the
  visitor's own saved data.
- **`sessionStorage`** (`workspace-context.js:22,31,34,74,81`) -- used for transient UI state that
  needs to survive the `window.location.reload()` calls this app's own save flows already trigger
  (e.g. `clearDraftSquad`'s reload, `draft-squad.js:178`/`229`), but isn't meant to outlive the tab.
- **`localStorage`** (`profile-forms.js:385`, the theme toggle) -- used for a pure browser-chrome
  preference that should persist indefinitely across visits but has no bearing on any model output or
  squad data.

### Candidate A1 -- new `profiles.db` column, keyed to team_id

Would follow #152's own precedent for adding profile state via `_migrate_schema`. Rejected: step 1
(starting the wizard, before any team_id exists) has nothing to key the row on yet, so this can only
cover steps 2-5, forcing a second, different persistence mechanism for step 1 regardless of what's
chosen here -- inconsistent for no benefit, since onboarding-step-done isn't model input or squad
data in the first place, unlike everything else this table stores.

### Candidate A2 -- `sessionStorage`

Matches the existing "survive an in-flow reload, don't outlive the tab" precedent exactly. Rejected
as the primary mechanism: this flow is explicitly meant to be resumable ("what next" -- someone who
sets up their profile today and comes back next week to build their squad), and `sessionStorage`
clearing on tab close would make the wizard re-trigger from scratch on every new visit, unlike the
theme preference it would sit next to.

### Candidate A3 -- `localStorage`, client-side only

Matches the theme-toggle precedent: durable per-browser UI-chrome state, no schema change, no new
endpoint. Covers all five steps uniformly (including step 1, unlike A1), and correctly treats
"wizard progress" as what it actually is -- decorative UI state, not squad or model data.

**Caveat, not fatal:** it's keyed to the browser, not the team. Someone who looks up a different
team via `?team_id=` (`team_lookup.py`) on the same browser would see the wizard's progress from
whichever team they last completed it for, not a fresh state for the team they're now viewing. Minor
because the wizard is advisory (see below) -- worst case is a step showing "done" prematurely, not a
blocked or broken tab -- and not worth a schema change to fix given A1's own rejection above.

**Recommendation: A3.** Store a single `localStorage` key (e.g. `fpl-onboarding-progress`, following
the `fpl-theme` naming precedent) holding the set of completed step ids.

## Question (b): blocking or advisory?

The codebase already has an established gating pattern to compare against:
`applyProfileGates()` (`gates-and-bootstrap.js:15-16`) hides Decision Center's and Model
Performance's content and swaps in a static empty-state panel whenever
`state.manager.connection_status === "not_configured"` -- but it never disables the nav buttons
themselves. Every tab stays reachable at all times; only the content inside reacts.

### Candidate B1 -- blocking (disable other nav tabs until the flow is completed)

Rejected: no precedent for disabling nav in this codebase at all -- the existing gate for the exact
same `not_configured` signal already chose content-swap over navigation-block. Would also actively
trap a returning, fully-configured manager who has no need for the wizard behind a forced flow if the
`localStorage` key were ever missing (fresh browser profile, cleared storage, different device) --
turning a UI nicety into a hard blocker for the majority of visitors who already have everything set
up server-side in `profiles.db`.

### Candidate B2 -- advisory (dismissible overlay/panel, reusing the `not_configured` signal)

Auto-surfaces using the same signal `applyProfileGates` already reads (`state.manager.connection_status
=== "not_configured"`), as a dismissible panel layered over the existing shell -- not a new gate
mechanism, just a second consumer of the one that already exists. Nav stays fully clickable
throughout, matching every other gate in this app.

**Recommendation: B2.** Consistent with the codebase's one existing precedent for this exact signal,
and avoids trapping existing managers.

## Recommendation summary

- Persistence: **`localStorage`**, one key holding completed-step ids, no backend change.
- Gating: **advisory**, auto-shown on the existing `not_configured` signal, dismissible, nav never
  disabled.
- Step order: **Draft Squad ("start your team") first**, not My Profile -- My Profile becomes an
  optional preference-tuning step reachable from within the flow rather than a hard prerequisite,
  since Draft Squad's own team-ID field is sufficient to unlock the rest of the sequence and the two
  tabs already share `state.profile.team_id`.

None of this requires new server endpoints, schema changes, or modifications to any of the five tabs'
existing logic -- confirming #257's own "Not in scope" framing holds.

## Update (2026-08-24): designed in Claude Design, implemented

A first wireframe pass (low-fi, four artboards) confirmed the shape above was viable, then a second,
higher-fidelity round -- built and iterated directly in Claude Design as a clickable prototype -- is
what actually shipped: **`Getting Started Flow v2.dc.html`**, importing **`Onboarding Shell.dc.html`**
(both mockups committed at
[`plans/assets/issue-257-onboarding/`](assets/issue-257-onboarding/) -- serve that directory
(`python3 -m http.server` from inside it) and open `Getting Started Flow v2.dc.html`; verified
working that way. The component-import mechanism fetches `Onboarding Shell.dc.html` at runtime, so
it needs http(s), not a bare `file://` double-click). The earlier wireframe pass is superseded and
not carried forward here.

**What the v2 design settled, beyond questions (a)/(b) above:**

- **Full step order**: Draft Squad ("Build a draft squad") &rarr; Decision Center ("Read the weekly
  decision") &rarr; Player Explorer ("Check prices and confirmed news") &rarr; My Profile ("Set your
  profile", optional) &rarr; What's New ("Expect a weekly cadence"). Decision Center moved to step 2,
  ahead of Player Explorer -- reading the model's own recommendation before doing manual research is
  closer to what a first-time visitor actually wants to see next than research-first.
- **Three-phase interaction model**: `welcome` (a one-time intro, auto-shown only for a genuinely
  unconfigured, never-seen-before visitor) &rarr; `card` (an expanded step card with the current
  step's "why" text and a CTA) &rarr; `pill` (a small collapsed indicator). Dismissing `welcome` by any
  path -- "Not now", the close button, Escape, backdrop -- moves straight to `pill` and the flow never
  auto-reopens itself again; the sidebar entry point and the pill are the only ways back in.
- **The team-ID step's own inline field**, with a "Continue with a placeholder" option generating a
  random in-range ID -- direct UI expression of this doc's own "neither entry point validates against
  a real FPL account" finding above.

**One place the real backend refined the mock's copy.** The v2 prototype's demo state optimistically
treats any saved `team_id` (real or placeholder) as unlocking a working Decision Center recommendation.
Tracing the real path (`compute_manager_view`, `refresh.py:489-563`) shows that isn't quite true: it
calls `collect_public_manager` -- a real fetch against FPL's public API -- *before* ever reaching the
draft-squad fallback, so a placeholder ID that doesn't correspond to a real FPL entry lands in
`connection_status: "lookup_failed"` / `weekly_decisions.status: "team_not_found"`, not
`registered_preseason`. That's already handled gracefully and legibly elsewhere in the app
(`decision-center.js:663`, `:855` -- "Team not found, or the official FPL API is temporarily
unavailable"), so nothing needed fixing, but the implementation's placeholder-save copy was written to
match reality rather than the mock's optimism: *"Saved as `<id>`. Swap in your real FPL team ID any
time from My Profile"* -- a promise about what's saved, not a claim about what Decision Center will
show.

**Two implementation-level departures from the mock, both scoped decisions rather than open
questions:**

- **Pill and card are mutually exclusive**, not simultaneously visible -- the mock's own `renderVals()`
  never actually sets a `pillVisible` key its markup reads, leaving that ambiguous; exclusive collapse/
  expand is the standard version of this pattern and avoids two floating elements stacking.
- **The mobile welcome reuses the existing shared sheet primitive directly** (`mountSheet`/`openSheet`/
  `closeSheet`, `mobile-shell.js`) rather than a second bespoke overlay, at every viewport width -- one
  `@media (min-width: 761px)` override turns the same `<dialog>` into a centered modal above the
  breakpoint mobile-shell.js already treats as the mobile/desktop line elsewhere in this file, exactly
  as the v2 design's own annotation asked for ("reusing the `mobile-shell.js` sheet pattern rather than
  a second one").

**A CSS bug worth flagging for whoever touches this next**: every new `hidden`-toggled element here
needed an explicit `.selector[hidden] { display: none; }` override, because an unconditional `display`
declaration on the *shown* state (`display: flex`/`grid`) beats the UA stylesheet's own
`[hidden] { display: none }` regardless of specificity -- author CSS always wins over UA CSS. This is
the exact same gotcha `dialog.sheet`'s own comment in `dashboard.css` already documents for `[open]`;
it bit the onboarding entry/tracker/card/pill/team-field elements here too before being caught live in
the browser (unit tests didn't catch it -- nothing asserts computed `display`, only DOM content).

**Files touched**: `templates/dashboard-shell.html` (sidebar entry, welcome dialog, tracker markup),
`css/dashboard.css` (styles + the `[hidden]` overrides above), `js/dashboard/onboarding.js` (new),
`js/dashboard/gates-and-bootstrap.js` (one `setupOnboarding();` call), `dashboard.py`
(`_DASHBOARD_JS_FILES` gains `"onboarding.js"`). No schema, endpoint, or existing-tab changes --
`/api/draft-squad`'s existing `{team_id, player_ids: null}` shape (already used by
`clearDraftSquad`, `draft-squad.js:178`) is reused as-is to register a team_id from the wizard.

Full test suite green (`scripts/run_tests_parallel.py`) and verified live in the browser at both
desktop and mobile widths: welcome auto-shows once for an unconfigured visitor, "Start with Draft
Squad" navigates and opens the card, the team-ID save (both a typed ID and the placeholder path)
correctly updates `state.manager`/`state.profile.team_id` and re-runs `applyProfileGates()`, every
step advances and persists across reload via `localStorage`, the sidebar entry and pill both reopen
the card, and nav remained clickable throughout -- advisory, per (b), holds in practice.
