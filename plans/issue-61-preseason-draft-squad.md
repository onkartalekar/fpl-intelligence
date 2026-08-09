# Preseason draft-squad recommendations (issue #61)

## Context

Before Gameweek 1's deadline, every visitor sees the same shared,
non-personalized "fresh-squad benchmark" (`build_gw_recommendations()`),
because personalized recommendations (`build_transfer_decisions()`) only
start at Gameweek 2 -- gated on `event <= 1`, which reflects a real
external constraint: FPL's public API doesn't reveal a manager's picks for
a gameweek until that gameweek's deadline passes. #61 asks for a way to
give tailored feedback on a manager's *self-declared* draft squad before
that data exists anywhere else.

## Structural finding: the reusable computation already exists, and it's a better fit than the issue's own framing assumed

The issue's earlier framing worried that draft-squad feedback would need
new squad-construction logic "closer to `build_gw_recommendations()`'s
own optimizer... applied against a fixed starting point instead of
building from scratch." Reading `transfer_decisions.py` directly shows
something better already exists: `_candidate_moves(squad, eligible, cash,
quotas, club_limit, profile)` and `_best_double(...)` are exactly "given
an existing 15-player squad, a pool of eligible replacements, remaining
budget, and quotas, find the best single/double transfer" -- this is
already the GW2+ weekly-decision engine's core, and it is *exactly* the
shape a declared draft needs, not a different one.

The one real difference: a GW2+ transfer suggestion respects a **free-
transfer budget** (rolling over, costing points beyond the allowance) --
`derive_free_transfers()`, `_scenario()`'s point-cost accounting. A
preseason draft has no such constraint: nothing has been "spent" yet, a
visitor can freely reshuffle their declared squad any number of times
before the deadline with no cost. So draft feedback is `_candidate_moves`/
`_best_double` running with the free-transfer machinery switched off
entirely (every suggested change is "free"), not a new optimizer.

`_public_squad(manager, projection_by_id)` converts a real manager's raw
FPL picks (with `purchase_price`/`selling_price` from the API) into the
enriched row shape `_candidate_moves` expects. A declared draft has no
purchase history -- a draft-specific equivalent would set both
`purchase_price` and `selling_price` to each player's *current* price
(nothing bought yet, so no profit/loss to account for), otherwise
identical.

## Candidate operationalizations

### Squad-declaration UI

- **(U1) A minimal picker reusing Player Explorer's existing search/filter
  UI -- recommended.** `view-players`'s search/club/position filters and
  `player-table` rendering already exist in `dashboard.js`/`dashboard.py`;
  a draft-builder needs the same search surface plus a running 15-slot
  selection with live budget/quota totals (£100.0m, 2 GKP/5 DEF/5 MID/3
  FWD, max 3/club -- the same constants `build_gw_recommendations` already
  reads from `bootstrap.game_settings`/`element_types`). Reusing the
  existing table/search markup and filters is materially less new UI than
  it sounds.
- **(U2) A from-scratch squad-builder view -- declined.** Would duplicate
  Player Explorer's search/filter logic in a second place for no benefit
  over reusing it directly.

### Storage

- **Extend #45's `profiles` table directly -- recommended.** Add a
  nullable `draft_squad` column (JSON-encoded list of 15 element IDs) to
  the existing per-team-ID `profiles` table (`src/fpl_intel/profiles.py`).
  No new storage concept, no account/identity question -- #45 already made
  this trivial by removing the registration gate entirely; any team ID can
  have a draft the moment someone declares one, exactly like it can have
  saved preferences today.
- A separate table was considered and declined: nothing about a draft
  squad needs different access patterns than the rest of a team's saved
  row, and a second table would just be an extra join for no isolation
  benefit (both are already scoped `WHERE team_id = ?`).

### Serving the recommendation

- Reuse #46's existing `?team_id=`-driven per-request compute path in
  `server.py`. When a team ID resolved via query param or cookie has a
  saved `draft_squad` *and* the season hasn't reached GW2 yet
  (`_next_event_id(bootstrap) <= 1`, the same check `build_transfer_
  decisions` already makes), splice in a new `draft_decisions` result
  computed via a new function (see below) instead of leaving the Weekly
  Decision panel in its `waiting_for_gw2` state. Once GW2 arrives, the
  real `build_transfer_decisions` path takes back over automatically
  (same `event <= 1` boundary already governs the switch) --
  no explicit "reconciliation" step needed, the existing gate already
  does it.

### New function shape

`build_draft_decisions(bootstrap, fixtures, draft_squad_ids, generated_at,
horizon=5, recent_transfers=None)` in `transfer_decisions.py`, sibling to
`build_transfer_decisions`: builds the enriched squad from
`draft_squad_ids` (draft-adapted `_public_squad` equivalent), calls
`_candidate_moves`/`_best_double` against it with no free-transfer-count
constraint (every suggested move reported as available, no point cost),
and returns single/double suggested changes plus the resulting squad's
projected points -- reusing `_scenario()`'s output shape minus its point-
cost fields, so the dashboard's existing weekly-decision rendering needs
minimal changes to display it.

## Reconciliation after GW1

Once the deadline passes and FPL starts reporting real picks,
`build_transfer_decisions`'s own `event <= 1` gate already stops applying
and the real GW2+ path takes over on the next refresh/request -- no
special handling needed. Whether to keep the declared draft around for a
"did I follow my own plan" comparison is worth a small follow-up, not a
blocker for shipping the core feature: the `draft_squad` column doesn't
need to be cleared automatically, so that comparison is possible to add
later without a schema change.

## Recommendation

1. Add `draft_squad` (nullable JSON column) to #45's `profiles` table.
2. Build a minimal draft-picker UI on top of Player Explorer's existing
   search/filter/table components (U1).
3. Add `build_draft_decisions()` to `transfer_decisions.py`, reusing
   `_candidate_moves`/`_best_double` with free-transfer accounting
   disabled.
4. Wire it into `server.py`'s existing per-request team-view path,
   gated on `event <= 1` and a saved `draft_squad` being present --
   the same boundary that already exists for the real GW2+ path takes
   over automatically once the season starts, no new reconciliation
   logic required.
5. Defer the "did I follow my own plan" post-GW1 comparison -- possible
   later without a schema change, not needed for the first version.

This is buildable as a single `ship-issue` pass -- no remaining open
design questions block starting.
