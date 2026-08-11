# Issue #102 -- Generalize per-team forecast archiving

## Context

The issue's own body already resolves most of the design (checkpoints, key format, uniform
per-team application via "Decided"/"Superseded" amendments) -- what's left is implementation. But
tracing the actual code to implement it surfaces a structural finding the issue's own framing
doesn't quite anticipate, worth flagging before writing code.

## Structural finding: `archive_forecast` archives the wrong shape of data for what this issue actually needs

The issue's Request section says: "For each registered team, compute and archive its live
recommendation (via `compute_manager_view`, the same function `_default_team_view_action` already
uses)." `compute_manager_view` returns `(manager_state, weekly_decisions)` -- `weekly_decisions`
being the real, personal roll/transfer/captaincy decision for *that team's actual squad*
(`transfer_decisions.py`'s `build_transfer_decisions`, status `"active"`/`"waiting_for_gw2"`/etc,
shaped as `{action, transfer_count, point_cost, gross_gain_5gw, net_gain_5gw, squad, lineup, ...}`
per risk profile).

**`archive_forecast` (`model_performance.py:52`) does not archive that.** Tracing
`_refresh_project_unlocked` (`refresh.py:341-379`): `decision_center` is `build_gw_recommendations`'s
output -- the *generic, no-squad-required* "what would an optimal fresh squad look like this
gameweek" recommendation (status `"active_preliminary"`/`"active"`, shaped as
`{profile_recommendations: [{horizons: {...lineup_player_ids, event_lineups...}}]}`).
`weekly_decisions` is attached as a *sibling* key inside `decision_center` at line 356 --
`decision_center["weekly_decisions"] = build_transfer_decisions(...)` -- but `archive_forecast`
only ever reads `decision.get("profile_recommendations")`/`decision.get("player_forecasts")`,
never `decision.get("weekly_decisions")`. **The one existing archive today captures the shared,
generic squad-construction recommendation (tagged with the legacy team's `risk_profile` as
`default_profile`), not any team's actual weekly transfer decision -- not even the legacy team's.**
Forecast archiving of the thing a manager actually gets told to do each week ("roll," "take a
hit," "which two players") doesn't exist at all today, for anyone.

This means "generalize per-team forecast archiving" isn't a loop-and-widen-the-key change over
existing logic (unlike #64's `manager_picks` precedent, which really was that shape) -- it needs a
**new archiving function** built for `weekly_decisions`' own shape, since `archive_forecast` as
written structurally cannot accept it (different status values, no `profile_recommendations`/
`horizons` keys at all). `archive_forecast` itself stays as-is, continuing to archive the one
shared preliminary-squad recommendation exactly as it does today -- that's a real, separate thing
worth keeping, not something this issue should touch or remove.

## Scope, given the finding

- New function, e.g. `archive_team_forecast(store, team_id, weekly_decisions, checkpoint_lead_hours,
  deadline_time=None)` in `model_performance.py`, built for `weekly_decisions`' actual shape:
  gate on `status == "active"` (the only status representing a real, complete decision -- every
  other status is a not-yet-actionable state, nothing to freeze), extract each profile's chosen
  `action`/`transfer_count`/`point_cost`/`net_gain_5gw`/squad+lineup, key
  `f"gw{event}:{lead_hours}"` per the issue's decided key-widening (model_version doesn't apply
  here the way it does to `decision_center`'s squad-construction output, since
  `build_transfer_decisions` isn't itself model-versioned the same way -- worth confirming this
  during implementation rather than assuming the exact same three-part key format applies
  unchanged).
- Storage: `model_performance.json["forecasts"]` today is a flat list (the one shared recommendation
  archive). Per-team weekly-decision archives need their own, separately-keyed store --
  `store["team_forecasts"] = {team_id: {checkpoint_key: {...}}}`, mirroring `manager_picks`' own
  `{team_id: {...}}` shape (issue #64's precedent), not reusing/renaming `forecasts` (which stays
  exactly what it is today: the one shared recommendation's archive).
- New script `scripts/archive_team_forecasts.py`, mirroring `trigger_scheduled_refresh.py`'s shape
  (env-var-driven, `--dry-run`, checks `in_send_window` against all three of
  `_ALLOWED_REMINDER_LEAD_HOURS = {3, 12, 24}` per the issue's own decided checkpoints), fetching
  each registered team's `weekly_decisions` the way `send_deadline_reminder.py` now does --
  `/api/manager-view?team_id=` over HTTP (issue #125's pattern), **not** a local
  `compute_manager_view` call -- this script would run on GitHub Actions like every other
  scheduled script this session built, with the same no-shared-filesystem-with-Railway constraint.
  This also means the archive write itself has to happen server-side (a new operator-only
  endpoint, or folded into the existing `/api/refresh` per-team loop) since a GitHub-Actions-hosted
  script has no direct write access to `model-performance.json` on Railway's volume either --
  the exact same structural gap #105/#122/#125 already closed for reads, now showing up for a
  write this issue needs. **This wasn't anticipated by the issue's own "Dependency" section**,
  which only names #101's scheduling as a prerequisite -- it doesn't account for the
  read-vs-write asymmetry #125 introduced, since #125 postdates this issue's original filing.
- Team list to archive for: reuse `profiles.list_team_ids` (all registered teams) capped by an
  analogous constant to `_MANAGER_PICKS_TEAM_CAP` (issue #64's own precedent for bounding
  per-refresh cost against a growing team population) -- exact cap value not decided here.

## Recommendation

This is larger than the issue's own text suggests, for two compounding reasons found above: (1)
no existing archiving path for per-team weekly decisions to generalize from -- a new function and
a new store shape are needed, not a widened key on existing logic; (2) the write itself needs a
new server-side surface, since this now has to run the same GitHub-Actions-over-HTTP way every
other scheduled script in this repo does, and nothing in the codebase today lets an external
caller write to `model-performance.json`.

Given the size and the number of new decisions this surfaces (the exact key format for
`weekly_decisions` archives, the new write endpoint's auth model, the per-run team cap value),
this warrants its own focused pass rather than folding into this already-long session. Flagging
for your decision on how to proceed rather than guessing at the remaining specifics and shipping
something under-verified.

## Decided and implemented (2026-08-11)

Resumed in a fresh session, on top of #105/#125 (both merged since this doc was first written).

- **New function `archive_team_forecast`** (`model_performance.py`), not a generalization of
  `archive_forecast` -- confirmed the two decision shapes really can't share one function (see
  the structural finding above). Gated on `weekly_decisions.status == "active"`, key
  `f"gw{event}:{lead_hours}"`, first-checkpoint-wins, stores player IDs not full objects.
- **New store shape**: `store["team_forecasts"] = {team_id: {checkpoint_key: {...}}}`, mirroring
  `manager_picks`' own `{team_id: {...}}` shape (issue #64's precedent), left entirely separate
  from `forecasts`/`champion_forecasts` (`archive_forecast`'s own store keys, untouched).
- **New write endpoint `POST /api/archive-team-forecast`**: computes `weekly_decisions` via the
  exact same `_resolve_team_lookup` helper `/api/manager-view` uses (so this can never drift from
  what a visitor's own dashboard or a script's `/api/manager-view` call would see), then writes
  under `project_refresh_lock` -- the same cross-process file lock `/api/refresh`'s subprocess
  acquires via `refresh_project`, preventing a concurrent full refresh from silently clobbering
  this endpoint's incremental update (or vice versa). Gated by the same `X-Refresh-Token`
  `/api/refresh` already requires -- not a dedicated token like #105's `/api/reminder-teams`,
  since this endpoint exposes no PII at all (only player IDs and recommendation metadata),
  carrying the same sensitivity as `/api/refresh` itself.
- **New read endpoint `GET /api/registered-teams`**: found during implementation that "every
  registered team" (this issue's own scope) has no existing way to be discovered from a
  GitHub-Actions-hosted script -- `/api/reminder-teams` only covers the reminder-opted-in subset.
  Bare team IDs only, no PII, gated the same way as `/api/archive-team-forecast`, capped at
  `_REGISTERED_TEAMS_CAP = 25` (reusing `refresh.py`'s existing `_MANAGER_PICKS_TEAM_CAP` value,
  per this issue's own "worth sizing against that precedent" note).
- **New script `scripts/archive_team_forecasts.py`**: checks all three checkpoints
  (`CHECKPOINT_LEAD_HOURS = (3, 12, 24)`) via `deadline_windows.in_send_window`, fetches the
  registered-team list once per matching tick, calls the archive endpoint once per team per
  matching checkpoint. `--dry-run` requires no env vars at all (matching
  `trigger_scheduled_refresh.py`'s exemption pattern, not `send_deadline_reminder.py`'s
  unconditional one -- this script's dry-run has nothing that needs a live fetch to preview).
- **Reuses `scheduled-refresh.yml`'s existing hourly tick** (a second step in the same job,
  `if: always()` so one script's failure never silently skips the other) rather than a new
  workflow with its own `schedule:`, per this issue's own dependency note.

Verified live end-to-end against a real running server with a real saved draft squad (not just
mocked tests): confirmed `/api/registered-teams` returns the real team, `/api/archive-team-
forecast` correctly no-ops for a `waiting_for_gw2` team, correctly archives a real `active`
decision with real player IDs/formation/captain once a draft squad made the decision real, is
idempotent on a repeat call for the same checkpoint, and archives independently for a different
checkpoint -- and confirmed the write lands exactly where `resolve_artifact` resolves for every
future reader (the current generation directory, not a stale flat-file copy).

## Not in scope

- Issue #65's ML minutes shadow model -- confirmed unaffected, entirely offline.
- The actual-outcome half (`manager_picks`, issue #64) -- confirmed backfillable anytime via FPL's
  own `/transfers/` endpoint, not at risk the way the forecast half is.
- Any change to `archive_forecast`'s existing behavior for the one shared `decision_center`
  recommendation -- stays exactly as-is.

## Dependency

#101 (shipped) and #125 (shipped, and newly relevant per the finding above -- the read pattern
this new write surface should mirror) are both satisfied. No remaining blocker, only remaining
scope to size and decide.
