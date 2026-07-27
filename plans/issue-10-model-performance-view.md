# Model Performance: team and player forecast vs actuals (issue #10)

## Context

Issue #10 asks the Model Performance view to show, per gameweek, (a)
team-level modeled vs actual points for the manager's own squad/XI and
(b) player-level modeled vs actual points for individual players (squad
first, ideally any player) -- live in the dashboard, in the spirit of the
offline backtest (`scripts/run_backtest.py` / `src/fpl_intel/backtest.py`).

**What exists today (verified):**

- `src/fpl_intel/model_performance.py` -- `archive_forecast(store, decision,
  deadline_time)` freezes, once and only strictly pre-deadline, the three
  profile recommendations' team totals per horizon (1/3/5 GW):
  `modeled/lower/upper_points`, `lineup_player_ids`, `captain_id`,
  `event_lineups`. It freezes NO per-player modeled points and nothing
  about the manager's own squad. `build_performance_report(store)` scores
  only champion profile-squad forecasts against `store["actual_events"]`.
- `store["actual_events"][str(event)]` (data/model-performance.json) is
  already a full per-player actuals map `{element_id: total_points}` built
  by `normalize_live_event` from the official `event/{id}/live` payload,
  collected in `refresh.py`'s finished-event loop (with per-event health in
  `store["actual_event_collection"]` via `_record_actual_collection_attempt`).
  So player-level ACTUALS already exist for every player; player-level
  FORECASTS are the missing frozen data.
- `recommendations.py` `project_players` produces, per player, per-event
  central xp (`fixture_xp`, aligned with `projection_events`) plus
  conservative/aggressive per-event bands (`profile_fixture_xp`). This is
  computed on every refresh inside `build_gw_recommendations` but never
  serialized per-player into state or the performance store.
- `manager_data.py` fetches only the CURRENT event's picks
  (`entry/{id}/event/{event}/picks/`); no per-event picks history is stored
  anywhere, so the manager's real XI for finished gameweeks is not
  available to score.
- `backtest.py` `season_comparisons` is the reference comparison shape:
  one row per player/origin/horizon with `modeled_points`, `actual_points`,
  `error`, `inside_range`, cohort rule "projected minutes > 0 or actually
  played", summarized with the shared `_summarize` (imported FROM
  `model_performance`; keep that import working).
- Dashboard: `src/fpl_intel/dashboard.py` is one template string. The
  performance view DOM is physical line 58 (`<section id="view-performance">`,
  ids `performance-status/-summary/-errors/-horizons/-calibration/-history/-method`,
  classes `performance-table/-head/-row`). `renderPerformance()` is physical
  line 164; the JS bootstrap call chain is line 170; the `performance` const
  with fallback defaults is line 64; CSS is line 22 (plus 23-27 for extra
  rules and media queries); `render_dashboard` at the bottom injects
  `state` as JSON.

**Design principle to preserve (already in the view copy and
`report["method"]`):** forecasts are frozen pre-event and never backfilled
with hindsight. Frozen per-player projections are forecasts; the manager's
published picks and official points are facts -- facts may be collected
after the event, forecasts may not.

## Design

### 1. Data layer -- freeze per-player forecasts

`recommendations.py` -- in `build_gw_recommendations`, add one key to the
returned decision:

- `"player_forecasts"`: compact list over ALL `projections` rows:
  `{"id", "modeled": fixture_xp[0], "lower": profile_fixture_xp["conservative"][0],
  "upper": profile_fixture_xp["aggressive"][0]}` (rounded, first-event slice
  only -- the per-gameweek view compares each GW against the forecast whose
  origin is that GW; full 5-event vectors would grow the store ~5x for no
  current consumer). Names/positions/clubs are NOT stored -- they are facts,
  resolved at render time from `state.players`.

`model_performance.py` -- extend `archive_forecast` (same guards: status
`active_preliminary`, pre-deadline `generated_at`, champion versioning,
first-write-wins per origin+version):

- After appending the team forecast, if `decision.get("player_forecasts")`
  and the forecast is champion, write
  `store["player_forecasts"][str(origin_event)] =
  {"forecast_id", "model_version", "generated_at",
  "players": {str(id): [modeled, lower, upper]}}` only if that origin key
  is absent (immutable; never overwritten by later refreshes).

### 2. Data layer -- manager picks history (facts)

`manager_data.py` -- new `fetch_manager_event_picks(team_id, event_id,
fetch_json=None)` returning the raw `entry/{team_id}/event/{event_id}/picks/`
payload (same `_fetch_json` plumbing as `collect_public_manager`).

`model_performance.py` -- new `normalize_manager_picks(payload)` returning
`[{"element_id", "multiplier", "is_captain"}]` from `payload["picks"]`
(ints; multiplier already encodes captain x2 / triple captain x3 / bench 0 /
bench-boost 1).

`refresh.py` -- inside the existing finished-event loop (the one that
fills `actual_events`), when a `team_id` is configured also backfill
`store.setdefault("manager_picks", {})[key]` for finished events that lack
it, via `fetch_manager_event_picks` (network path only when
`bootstrap_payload is None`, mirroring `fetch_event_live`). Add an
injectable `manager_picks_payloads=None` kwarg to
`_refresh_project_unlocked`/`refresh_project` mirroring
`event_live_payloads` for tests. Failures append
`f"GW{event_id}: manager picks collection failed"` to
`actual_collection_errors` (already surfaced in `performance-errors`);
do not fail the refresh.

### 3. Report -- `build_performance_report` additions

Keep every existing key untouched. Add two sections, both tolerant of old
stores missing the new keys (use `.get(..., {})`):

- `"player_performance"`: for each origin event present in BOTH
  `store["player_forecasts"]` and `store["actual_events"]`, one row per
  player id with backtest-style cohort rule (include when `modeled > 0` or
  `actual != 0`): `{"event", "element_id", "modeled_points",
  "actual_points", "error", "absolute_error", "lower_points",
  "upper_points", "inside_range"}`. Emit
  `{"status": "active"|"waiting_for_results", "events": [scored event ids],
  "comparisons": [...], "summary": _summarize(rows)}`. Reuse `_summarize`
  verbatim (`error` and `inside_range` keys line up by construction).
- `"team_performance"` (manager's real team): for each event present in
  both `store["manager_picks"]` and `store["actual_events"]` AND with a
  frozen `player_forecasts` entry for that origin (no frozen forecast ->
  no comparison, never reconstructed), one row:
  `modeled_points = sum(multiplier * modeled[id])` (same for lower/upper;
  label as a naive band sum), `actual_points = sum(multiplier *
  actual_events[event].get(id, 0))`, plus `error`, `inside_range`. Emit
  `{"status", "comparisons", "summary": _summarize(rows), "method":
  "Published official picks and multipliers scored with pre-deadline
  frozen per-player projections; no autosubs are simulated on either
  side."}`. Missing-pick players score 0 on both sides.

### 4. UI -- extend `section id="view-performance"` (dashboard.py line 58)

Insert after the existing "Forecast history" panel, reusing existing CSS
classes only (`panel`, `section-heading`, `performance-table/-head/-row`,
`field`, `decision-summary`, `decision-metric`, `empty`, `muted`,
`status-good/wait`) -- no new CSS rules expected; if a select width tweak
is needed, extend line 22 minimally:

- Panel "My team -- modeled vs actual": heading + `<span
  id="performance-team-status">`, summary strip `<div
  id="performance-team-summary" class="decision-summary">`, and a
  `performance-table` with `<tbody id="performance-team-history">`
  (columns: Gameweek, Modeled XI, Official actual, Error, In range), plus
  `<p id="performance-team-method" class="muted">`.
- Panel "Player forecast vs actual": a `.field` with `<select
  id="performance-player-select">` (optgroups "My squad" from
  `state.manager.squad` element ids, then "All forecast players" sorted by
  name), a `performance-table` with `<tbody
  id="performance-player-history">` (columns: Gameweek, Modeled, Official
  actual, Error, In range) and a one-line `<div
  id="performance-player-summary" class="muted">` (count/MAE/bias for the
  selected player).

JS changes:

- Line 64 `performance` const: extend the fallback default with
  `team_performance:{comparisons:[]},player_performance:{comparisons:[]}`.
- New top-level functions next to `renderPerformance` (line 164):
  `renderTeamPerformance()` and `renderPlayerPerformance()`; both called
  from the line 170 bootstrap chain right after `renderPerformance()`.
- `renderTeamPerformance`: honest empty states in priority order --
  manager `not_configured` (reuse the existing profile-setup copy pattern),
  no frozen forecasts yet, no finished gameweeks yet; otherwise rows from
  `performance.team_performance.comparisons` newest first, error cell
  reusing the `positive/negative` class pattern from `renderPerformance`.
- `renderPlayerPerformance`: build `playerById` from `state.players`
  (fallback label `Player ${id}` when a frozen id is missing from the
  current catalog); populate the select; re-render the table on `change`;
  default selection = first squad player with any comparison, else first
  player overall; per-player rows filtered from
  `performance.player_performance.comparisons` by `element_id`.
- All dynamic text through the existing `esc()`; numbers through
  `Number(x).toFixed(1)`.

## Hard constraints (existing assertions that must survive)

`tests/test_dashboard.py`:
- `'data-view="performance"'`, `'id="performance-summary"'`,
  `"Modeled vs actual points"`, `"Calibration diagnostics"`,
  `"Official actual"` (test_renders_active_gw1_decision_center...).
- `'<table class="performance-table">'`, `'<tbody id="performance-history">'`,
  `'<tr class="performance-row">'` (test_player_and_performance_datasets...)
  -- assertIn, so additional tables with the same classes are safe.
- `'id="performance-errors"'`, `"performance.collection_errors||[]"`,
  `"Result collection issue"` (test_model_performance_collection_errors...).
- Decision Center literals from the issue-9 pass (subnav arrays, ARIA tabs,
  `.decision-layout{display:grid;align-items:start;` etc.) -- untouched
  since no Decision Center edits are planned.
- `"after launch"` must not appear (case-insensitive).

`tests/test_model_performance.py`: every existing test constructs stores as
`{"forecasts": [], "actual_events": {}}` and decisions WITHOUT
`player_forecasts` -- `archive_forecast` and `build_performance_report`
must keep working with those shapes and keep all current outputs
(comparison counts, champion gating, `(1/8)` calibration copy, multiweek
event-lineup scoring).

`tests/test_refresh.py::test_refresh_ingests_finished_event_points_for_model_performance`:
store without new keys, no `team_id` configured -- refresh must still
produce `completed_comparisons == 1` and persist `actual_events`. No
manager-picks fetch may fire when no team_id is set or when
`bootstrap_payload` is provided without injected payloads.

`tests/test_backtest.py` imports `_summarize` from `model_performance` --
signature and output keys (`count/mae/bias/rmse/range_coverage`) frozen.

## New test assertions

`tests/test_model_performance.py`:
- `archive_forecast` with a decision carrying `player_forecasts` writes
  `store["player_forecasts"]["1"]["players"]` once; a second archive (or a
  post-deadline `generated_at`) leaves it unchanged; non-champion decisions
  do not write it.
- `build_performance_report` with frozen player forecasts + actuals emits
  `player_performance` rows with correct `error`/`inside_range`, applies
  the cohort rule (a 0-modeled/0-actual player is excluded), and
  `summary["mae"]` matches.
- `normalize_manager_picks` maps a picks payload to
  element_id/multiplier/is_captain ints.
- `team_performance` scores multiplier-weighted modeled vs actual for a
  stored pick set (captain x2 verified), and emits no comparison for an
  event lacking frozen `player_forecasts`.
- Old store shape (`{"forecasts": [], "actual_events": {}}`) yields empty
  `player_performance`/`team_performance` with `waiting_for_results`.

`tests/test_refresh.py`:
- With a configured team_id, a finished event, injected
  `event_live_payloads` and `manager_picks_payloads`: refresh persists
  `manager_picks["1"]` and `state["model_performance"]["team_performance"]`
  has one comparison.
- Existing no-team_id test unchanged proves the no-fetch path.

`tests/test_dashboard.py` (substring checks, copy exact literals from the
final template):
- `'id="performance-team-history"'`, `'id="performance-team-summary"'`,
  `'id="performance-player-select"'`, `'id="performance-player-history"'`.
- Copy: `"My team -- modeled vs actual"` (as written in the template),
  `"Player forecast vs actual"`.
- JS wiring: `"performance.team_performance"`,
  `"performance.player_performance"`.
- Preserve-the-principle assertion: `"Results are never backfilled with
  hindsight lineups."` stays present.

## Edit sequence

1. `src/fpl_intel/recommendations.py` -- add `player_forecasts` to the
   `build_gw_recommendations` return dict.
2. `src/fpl_intel/model_performance.py` -- extend `archive_forecast`
   (freeze `store["player_forecasts"]`), add `normalize_manager_picks`,
   extend `build_performance_report` with `player_performance` and
   `team_performance`.
3. `src/fpl_intel/manager_data.py` -- add `fetch_manager_event_picks`.
4. `src/fpl_intel/refresh.py` -- add `manager_picks_payloads` kwarg;
   backfill `store["manager_picks"]` in the finished-event loop; error
   strings into `actual_collection_errors`.
5. `src/fpl_intel/dashboard.py` -- line 58 DOM (two panels), line 64
   `performance` default, new `renderTeamPerformance` /
   `renderPlayerPerformance` beside line 164, bootstrap chain line 170.
6. Tests: `tests/test_model_performance.py`, `tests/test_refresh.py`,
   `tests/test_dashboard.py` additions.

## Verification

1. `PYTHONPATH=src python3 -m unittest discover -s tests` -- all green.
2. `node --check` on the extracted `<script>` block of the rendered
   template (substitute the `__TRUSTED_LINK_DOMAINS__` placeholder first,
   as done for the issue-9 pass).
3. `python3 scripts/refresh_dashboard.py` then
   `python3 scripts/start_dashboard.py`, browser checklist:
   - Model Performance view: existing summary/horizons/calibration/history
     panels unchanged; new team panel shows the honest empty state
     ("waiting for completed Gameweeks" preseason, or the connect-profile
     copy without a team_id); player panel select lists "My squad" first.
   - With seeded store data (temporarily drop a fake
     `player_forecasts`/`manager_picks`/`actual_events` trio into
     `data/model-performance.json` and refresh): team rows show modeled vs
     actual with signed colored error; changing the player select swaps the
     history table; unknown ids render as `Player {id}` without breaking.
   - 375px width: both new tables horizontally scroll inside their
     `overflow-x:auto` wrappers; console clean throughout.
