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
existing logic -- confirming #257's own "Not in scope" framing holds. Ready to hand off to
`ship-issue` once confirmed.
