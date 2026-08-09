# Extend forecast-accuracy history tracking beyond one team (issue #64)

## Context

Today `_refresh_project_unlocked()` accumulates season-long history (frozen
pre-deadline forecasts vs. official results, plus a manager's actual
picks) for exactly one team -- whichever `team_id` is in
`config/user-profile.json`. #45 introduced per-team-ID saved profiles with
no registration gate; this issue asks whether every team with a saved
profile should get this same tracking, not just one hardcoded team.

## Structural finding: the real cost is smaller than originally framed, because most of the pipeline is already shared

Reading `refresh.py` and `model_performance.py` directly (not assumed from
the issue's original framing) shows the per-manager tracking loop
interleaves two genuinely different things:

- **`actual_events`** (official post-gameweek results) and
  **`player_forecasts`** (the frozen, pre-deadline per-*player* point
  projections) are both **already shared** -- keyed only by gameweek, not
  by team. One fetch/one archive per finished gameweek serves every team,
  today and after this change.
- **`manager_picks`** is the *only* genuinely team-specific input --
  one `fetch_manager_event_picks(team_id, event_id)` call per team per
  newly-finished gameweek.

And critically, `_team_performance()` (the function behind "My team --
modeled vs actual") doesn't score against a per-manager *forecast* at
all -- it scores a team's actual submitted picks against the **shared**
frozen `player_forecasts`, multiplier by multiplier. So extending this to
many teams does **not** mean running the forecast/archival machinery once
per team -- only `manager_picks` collection scales with team count. The
issue's original "multiplying the live-FPL-API-call cost of every refresh
by the number of registered teams" was accurate in direction but
overstated the surface: it's one extra call type, not the whole pipeline,
per additional team.

## Candidate operationalizations

### Collection (refresh-time, must stay on the refresh cadence)

- **(C1) Automatic for every team with a saved #45 profile, capped per
  refresh -- recommended.** Now that the real added cost is understood
  (one `fetch_manager_event_picks` call per team per newly-finished
  gameweek, not a full per-team re-run), opt-in friction isn't clearly
  worth it. Still worth a hard cap on how many distinct team IDs get
  picks collected in a single refresh run (e.g. process N per run,
  carry the rest to the next refresh) so refresh duration stays bounded
  regardless of how many teams have saved a profile -- directly relevant
  to #28's finding that `/api/refresh` has no time-based rate limit yet.
- **(C2) Opt-in only -- declined as the default, worth keeping as an
  escape hatch.** The issue's original cost/relevance worry mostly
  dissolves once the real (smaller) cost is known. Still worth exposing
  as a setting for someone who explicitly doesn't want their picks
  fetched every gameweek, but shouldn't be the default given the low
  actual cost.

### Storage

`manager_picks` in `model-performance.json` is currently `{event_key:
picks}` -- becomes `{team_id: {event_key: picks}}`. `actual_events` and
`player_forecasts` stay exactly as they are (already correctly shared,
confirmed above -- no schema change needed there).

### Scoring and serving

- **(S1) Compute `team_performance` at request time, not refresh time --
  recommended, matching the pattern #45/#46 already established.**
  `_team_performance()` is cheap (pure filtering/arithmetic over already-
  collected `manager_picks`/`actual_events`/`player_forecasts`, no live
  API call) -- there's no reason to precompute and store a report for
  every team eagerly. `server.py`'s existing per-request splice
  (`_serve_dashboard`, which already splices `state["manager"]`/
  `state["profile"]` for whichever team ID is resolved via query param or
  cookie) is the natural place to also splice `state["model_performance"]
  ["team_performance"]`/`["player_performance"]` for that same team ID,
  reading its slice of the now-per-team-keyed `manager_picks`.
  This also means a team with a saved profile but *no* collected picks
  yet just sees today's existing "waiting for results" state -- no new
  empty-state handling needed, `_team_performance` already returns that
  shape when its input is empty.
- **(S2) Precompute and store a report per team at refresh time --
  declined.** Would mean writing and maintaining N reports on every
  refresh (most never viewed that cycle) instead of computing the one
  actually requested, for no benefit given how cheap the computation is.

## Recommendation

1. Change `manager_picks`' shape to `{team_id: {event_key: picks}}`.
2. In the refresh loop, iterate every team ID with a saved #45 profile
   (not just `config/user-profile.json`'s), capped per run (C1), fetching
   `manager_picks` for each against already-shared `actual_events`/
   `player_forecasts` -- no change needed to those two.
3. Move `team_performance`/`player_performance` computation from
   refresh-time (baked into the single shared `dashboard-state.json`) to
   request-time, spliced per resolved team ID alongside `state["manager"]`
   the same way #45/#46 already do (S1).
4. Leave `config/user-profile.json`'s own single-team tracking role
   alone during the transition -- it can simply become "one more team ID
   that happens to have picks collected," not a special case to migrate.

No remaining open design questions block starting -- buildable as a
single `ship-issue` pass, coordinated with #28's refresh-rate-limiting
fix landing around the same time given the direct cost interaction.
