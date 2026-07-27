# Decision Center improvement pass (issue #9)

## Context

Issue #9 asked for a layout review of the Decision Center. The big redesign
(commit 3eb2181) already shipped a sticky player-detail panel, stacked
component bars, profile range strips, and a sticky subnav — but exploration
found real gaps left behind, and the user approved this scope: **nav &
scroll fixes**, **mobile/tablet layout fixes**, and **surfacing two
already-computed-but-never-rendered data blocks** (watchlist, per-GW plan
rotation). Page-section reordering and the other unused data (minutes-risk
detail, 1/3-GW ranges) are explicitly out of scope.

All work is in `src/fpl_intel/dashboard.py` (the `_TEMPLATE` string: CSS on
physical line 22–26, Decision Center DOM on line 53, JS on lines 63–167)
plus new assertions in `tests/test_dashboard.py`. Work on a new feature
branch off `main`.

**Verified facts that shape the design:**
- `state.decision_center.watchlist` (recommendations.py:874–881) is
  `{GKP/DEF/MID/FWD: top-5 eligible players by xp_5}` — **NOT filtered
  against the squad**; entries are full projection dicts (carry `id`,
  `name`, `component_xp`, `xp_1/3/5`, `price`, `confidence`, etc.), so the
  existing breakdown inspector works on them unmodified. Squad-exclusion
  must happen client-side per selected profile.
- `profile.evaluation_horizons` (recommendations.py:694–715): keys
  `"1"/"3"/"5"`, each with `event_lineups` rows
  `{event_index, event, formation, lineup_player_ids, bench_player_ids,
  captain_id, vice_captain_id, profile_points, central_points,
  lower_points, upper_points}` and `lineup_semantics:"event_specific"`.
  All player refs are ids — resolve against `selected.squad.players`.
- `.inspector` CSS is **shared** with the Transfers view — the mobile
  `order:-1` rule affects both, so the fix must be scoped.
- `renderDecisionLegacy` (line 89) is dead code — exactly 1 occurrence,
  no callers.
- Test fixtures have no `evaluation_horizons` and a watchlist player who
  IS in the squad — both new renderers need graceful empty/hidden states.

## Item 1 — Nav & scroll fixes

1. **Chip set → 6 chips** (line 53 DOM): Summary · Weekly decision ·
   Profiles · XI + captaincy · Bench & model · Squad & player detail.
   - Insert `<button ... data-scroll-to="decision-section-weekly">Weekly decision</button>` after Summary; `<button ... data-scroll-to="decision-section-bench">Bench &amp; model</button>` between XI and Squad.
   - Give the second `.decision-layout` (bench + model panels) `id="decision-section-bench"`.
   - Update `watchedIds` in `setupDecisionSubnav` (line 114) to the full
     6-id list. No other subnav JS changes — chips are discovered via
     `[data-scroll-to]`.
2. **`scroll-margin-top`** (line 22): add
   `#decision-section-summary,...,#decision-section-breakdown{scroll-margin-top:58px}`
   (subnav sticky height ≈51px) so chip clicks and `selectPlayerCard`
   scrolls don't park headings under the sticky bar.
3. **Respect reduced motion in JS**: add a
   `prefersReducedMotion()` helper (`matchMedia('(prefers-reduced-motion: reduce)')`);
   use `behavior: prefersReducedMotion()?'auto':'smooth'` in the
   `scrollIntoView` calls at line 103 (`selectPlayerCard`) and line 112
   (subnav click). Keep the existing CSS fallback on line 26.
4. **Delete dead `renderDecisionLegacy`** (line 89) — do this first so
   later JS line references shift by exactly −1.

## Item 2 — Mobile/tablet layout

1. **Scope the mobile inspector flip** (line 25, `@media(max-width:760px)`):
   replace `.inspector{position:static;order:-1}` with
   `.inspector{position:static}.transfer-layout .inspector{order:-1}` —
   Transfers keeps evidence-above-feed; the Decision Center breakdown
   falls back to DOM order (after squad + watchlist). Tapping a card still
   reveals it via the existing `selectPlayerCard` scroll.
2. **New tablet breakpoint** (new physical line after line 24):
   `@media(max-width:980px){.decision-layout{grid-template-columns:1fr}.decision-layout .inspector{position:static}}`
   — below 980px collapse to one column instead of pinning the sticky
   column at its 300px minimum. Don't touch line 24's existing rules; the
   test-asserted literal `.decision-layout{display:grid;align-items:start;`
   on line 22 is untouched.
   Also add `.decision-layout .inspector{top:64px}` to line 22 so the
   desktop sticky breakdown clears the subnav (currently uses the
   Transfers-tuned `top:135px`).
3. **One-row scrollable mobile subnav** (append inside the 760px block):
   `.decision-subnav{flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none;padding:9px 14px;margin:0 -14px 14px}.decision-subnav::-webkit-scrollbar{display:none}.decision-subnav-chip{flex:0 0 auto;white-space:nowrap}`
   (negative margin matches the 14px content padding for edge-to-edge).

## Item 3 — Watchlist panel

- **DOM (line 53):** inside `#decision-section-squad`, wrap the existing
  "Full recommended squad" panel plus a new
  `<section class="panel" id="decision-watchlist-panel">` (heading
  "Watchlist", body `<div id="decision-watchlist">`) in a
  `<div class="overview-stack">` (existing class:
  `display:grid;gap:14px;align-content:start`). Sticky breakdown panel
  stays as the right column.
- **CSS (line 22):** `.watchlist-group` styles only (margin + muted
  uppercase h3).
- **JS (in `renderDecision`, line 148 area):** after the squad render and
  **before** `attachBreakdownHandlers`:
  - `squadIds = new Set(squad.players.map(p=>p.id))`; for each position in
    `['GKP','DEF','MID','FWD']` filter `decision.watchlist[pos]` by
    `!squadIds.has(id)` (this makes the panel react to profile tabs),
    render survivors with the **existing `card()` closure** grouped under
    `.watchlist-group` headings; empty-state div if all groups empty.
  - Extend the handler call:
    `attachBreakdownHandlers(squad.players.concat(watchlistPlayers), squad.captain, squad.selection_rationale)`
    — watchlist cards become clickable and drive the existing breakdown
    inspector for free (no rationale entry → renders empty, correct).
  - Inactive branch (line 141): add `'decision-watchlist'` to the
    empty-fill id list.

## Item 4 — Per-GW rotation view

- **DOM (line 53):** full-width third panel inside `#decision-section-xi`
  after Captaincy: `<section class="panel" id="decision-rotation-panel">`
  with h3 "Five-gameweek XI rotation", meta span
  `#decision-rotation-meta`, body `<div id="decision-rotation" class="decision-list">`.
- **CSS (line 23):** extend the existing rule to
  `#decision-bench-panel,#decision-model-panel,#decision-rotation-panel{grid-column:1/-1}`.
  Covered by the existing "XI + captaincy" chip — no new subnav entry.
- **JS:** new top-level `renderRotationPlan(selected, squad)` inserted just
  before `renderDecision`:
  - `horizon = evaluation_horizons['5'] || ['3'] || ['1']`; if no
    `event_lineups`, hide the panel (`hidden=true`) and return — covers
    test fixtures/legacy snapshots.
  - Resolve names via `Map(squad.players.map(p=>[p.id,p]))`.
  - One `.decision-row` per event: left = `GW{event} · {formation}` +
    muted `C {captain} · VC {vice} · {changes}` where changes is
    "Baseline XI" / "Unchanged XI" / `In: A, B · Out: C, D` (set diff vs
    the first row's XI); right = `central_points` + muted
    `lower–upper` range.
  - Meta: `'Event-specific lineups · ' + selected.label`.
  - Call from `renderDecision` right after the captaincy render; profile
    tabs and `restoreWorkspaceContext` already funnel through
    `renderDecision(profileId)`, so it reacts to profile switching with no
    extra wiring. Hide it on the inactive branch too.

## Hard constraints (existing test assertions — must survive)

- Keep ids: `decision-summary`, `recommended-xi`, `recommended-bench`,
  `captaincy-list`, `profile-options`, `profile-comparison`,
  `weekly-*`, `fixture-congestion-limitation`.
- Keep literal CSS prefix `.decision-layout{display:grid;align-items:start;`
  (property order matters).
- Keep literal JS/copy strings: `Number(player.xp_3).toFixed(1)`,
  `five_gameweek_advantage_over_roll`, `free_transfer_source`, the ARIA
  tabs wiring (`aria-controls="profile-panel"` etc.),
  "Roll, transfer, and chip recommendation", "5-GW range",
  Conservative/Balanced/Aggressive.
- The phrase "after launch" must not appear anywhere (case-insensitive).

## New test assertions (tests/test_dashboard.py)

Substring checks against the rendered template (finalize code text first,
then copy exact literals into the assertions):
- Subnav covers every section: the two new `data-scroll-to` values,
  `id="decision-section-bench"`, and the full 6-id `watchedIds` array
  literal.
- Scroll/motion: `scroll-margin-top:58px`, the `matchMedia` reduced-motion
  literal, `assertNotIn('renderDecisionLegacy', html)`.
- Mobile scoping: `.transfer-layout .inspector{order:-1}` present, old
  unscoped `.inspector{position:static;order:-1}` absent, the nowrap
  subnav rule, the 980px media query prefix.
- New panels: `id="decision-watchlist"`, `id="decision-rotation"`,
  `decision.watchlist`, `evaluation_horizons`,
  "Five-gameweek XI rotation", "Watchlist".

## Edit sequence

1. Delete line 89 (`renderDecisionLegacy`).
2. Line 53 DOM: chips, `decision-section-bench` id, overview-stack +
   watchlist panel, rotation panel.
3. CSS lines 22/23: scroll-margin, inspector top, watchlist-group,
   rotation grid-column.
4. New 980px media query after line 24; line 25 edits (inspector scope,
   subnav scroll row).
5. JS: `prefersReducedMotion`, scrollIntoView behaviors, `watchedIds`,
   `renderRotationPlan`, `renderDecision` additions.
6. New tests, then verification.

## Verification

1. `PYTHONPATH=src python3 -m unittest discover -s tests` — all green.
2. `node --check` on the extracted `<script>` block (substitute the
   `__TRUSTED_LINK_DOMAINS__` placeholder before checking).
3. `python3 scripts/refresh_dashboard.py` + `python3 scripts/start_dashboard.py`,
   then in the browser:
   - **1280px:** all 6 chips scroll with headings clear of the sticky bar;
     active chip tracks while scrolling through all 6 sections; rotation
     panel re-renders on profile switch; watchlist shows only non-squad
     players, changes with profile, and clicking a watchlist card fills
     the sticky breakdown; bench/model reachable via its chip.
   - **~900px:** decision layouts single-column, breakdown static and
     after squad + watchlist.
   - **375px:** subnav is one scrollable sticky row; breakdown appears
     after squad and watchlist; tapping a card scrolls to it; **Transfers
     view still shows evidence inspector above the feed** (regression
     check for the scoped `order:-1`).
   - **Reduced motion** (DevTools emulation): chip clicks jump instantly.
   - Console clean throughout.
