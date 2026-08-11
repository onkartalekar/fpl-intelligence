# FPL Intelligence System, 2026/27 Foundation Specification

## Objective

Maximize the probability of an overall rank below 50,000 while keeping weekly manager time below 15 minutes. Mini-leagues are secondary. The system recommends actions but never changes the FPL account.

## Manager profile

- Time zone: `America/New_York`
- Deadline availability: Sometimes available in the final two hours
- Weekly time budget: Under 15 minutes
- Interface: Local dashboard
- Delivery: One final deadline report
- Data budget: Free sources only
- Betting odds: Excluded
- Social and predicted lineups: Only high-confidence reports
- Risk: Balanced
- Recommendation depth: Full scenario analysis
- Storage: Local Mac

## Current-transfer policy

Transfer-window movements are a first-class input. Before the new FPL game launches, the old FPL API may still contain the prior season's clubs and players. Therefore, a move is handled in two stages:

1. **Confirmed externally:** Record only a first-party club announcement or an official Premier League announcement. Rumours and unattributed reports are excluded.
2. **Reconciled in FPL:** When the new `bootstrap-static` feed lists the player at the destination club, attach the FPL player and club identifiers, position, and price.

Every transfer record must retain:

- Player name
- Origin and destination club
- Announcement timestamp
- Effective date when stated
- Original first-party URL
- Source type
- Verification status
- FPL reconciliation status
- Notes affecting expected minutes, role, position, set pieces, or competition

A confirmed transfer triggers projection review for:

- The transferred player
- Players competing for the same role at the destination
- Players replacing the departure at the origin
- Set-piece and penalty order
- Formation and expected-minutes assumptions

## Source hierarchy

1. Official FPL API for prices, positions, clubs, fixtures, deadlines, and FPL statistics
2. Official Premier League and club announcements for transfers, injuries, suspensions, and manager statements
3. Direct manager press conferences
4. High-confidence predicted-lineup sources, clearly labeled as predictions
5. Other social posts are excluded

The system will not use betting odds.

**Spec amendment (2026-07-25) — LLM news extraction, built but not
active.** `src/fpl_intel/news_signals.py` implements an optional
extractor over tier-2/3 sources above (official club/PL news, manager
press conferences): an LLM API call (raw HTTPS, no vendor SDK dependency)
reads a first-party news item and returns structured availability signals
(`player`, `availability_signal`, `expected_return`, `role_hint`,
`confidence`, `exact_quote`), never a projection or a point value itself.
Provider-agnostic (amended 2026-07-25): the extractor is not tied to one
LLM vendor — it ships with a Claude Messages API caller (default) and a
generic OpenAI Chat Completions-compatible caller usable with any host
implementing that shape, selected via the `FPL_INTEL_LLM_PROVIDER`
environment variable (`claude` or `openai_compatible`). Bounds: any
resulting adjustment to expected minutes is capped at Β±25% of the
pre-adjustment estimate (`bounded_minutes_adjustment`), so a single news
item nudges a projection rather than overriding it. Provenance: every
signal carries its source URL and the exact supporting quote, the same
treatment confirmed transfers already receive. Fallback: no API key, a
network failure, an unrecognized provider, or a malformed response all
resolve to zero signals — the pipeline runs identically either way, never
raising and never fabricating a claim. Per IMPLEMENTATION_PLAN.md Phase
5's own gate (Phases 1-4 adopted, plus β‰₯8 live calibration comparisons),
this extractor is **not wired into the live refresh pipeline** — the gate
is unmet on both counts (Phase 1 and Phase 4 were built and backtested but
not adopted; the 2026/27 season has not started, so there is no live
calibration data yet). This is documented, tested infrastructure, held in
reserve.

The API key referenced above (`ANTHROPIC_API_KEY` for the default Claude
provider, or `FPL_INTEL_LLM_API_KEY` for the generic provider) is for LLM
inference, not an FPL account credential — it is read from an environment
variable, never stored, and is unrelated to the "no password is stored"
rule for manager state below, which concerns the FPL account itself.

## Core data model

### Player snapshot

- FPL element ID
- Player and club
- FPL position and price
- Availability and official news
- Minutes and historical FPL statistics
- Expected minutes with confidence
- Role and set-piece metadata
- Last collection timestamp
- Provenance

### Projection

For each player and future gameweek:

- Start probability
- Expected minutes
- Appearance points
- Goal and assist expectation
- Clean-sheet and save expectation
- Bonus expectation
- Expected FPL points
- Lower, central, and upper scenarios
- Confidence and evidence timestamp

No projection may be presented as live unless its source data is current and the collection time is visible.

**Implementation status (updated 2026-07-25, see IMPLEMENTATION_PLAN.md
for the full history):** every field above is now a distinct, named
component (`appearance`, `attacking`, `clean_sheet`, `goals_conceded`,
`saves`, `bonus`, plus a `residual` term for historical over/under-
performance versus the other components), computed from official
expected-goals/assists/saves-per-90 rates rather than one blended
points-per-90 rate, with a per-event `component_xp` breakdown on every
projection. Clean-sheet probability, goals-conceded rate, and attacking
scaling are fitted from three seasons of historical results (see
`config/model-coefficients.json`); uncertainty bands (the lower/central/
upper scenarios above) are likewise empirically fit, achieving 79-89%
backtested interval coverage against a 70-80% target. A fitted per-team
opponent-strength model (replacing the fixture-difficulty-bucket
approximation) and a recency-weighted expected-minutes model (replacing
the season-average estimate) were both built and backtested but are
**not** currently active: both were measurably worse than the fitted
FDR-bucket / season-average baselines they were meant to replace, a
result treated as valid and left documented rather than forced. See
`model.limitations` in the dashboard for the live, current-version detail.

- Squad and bench
- Purchase and selling prices
- Bank
- Free transfers
- Chips
- Rank and mini-league context

No password is stored. Private state is entered manually unless a separately approved local browser integration is added. Because the public manager API does not publish an authoritative remaining-free-transfer field, the planner reconstructs an estimated balance from public history. An exact local value may be supplied as `manager.confirmed_free_transfers` with `manager.confirmed_free_transfers_event` in `config/user-profile.json`; it must be between zero and the current official cap and is labeled as confirmed local state in the dashboard. An override tagged for another event is ignored to prevent stale transfer state from changing advice.

## Optimization policy

- Primary horizon: Rolling five gameweeks
- Also report one-gameweek and three-gameweek impact
- Default action: Roll when no transfer has meaningful net value
- Hits: Deduct hit cost before ranking scenarios
- Captaincy: Highest projection, safest option, and higher-variance alternative
- Differentials: Allowed only when projection-supported
- Goalkeeper: Prefer set-and-forget plus inexpensive backup
- Bench: Maintain cover without creating persistent selection dilemmas
- Free Hit: Compare against the no-chip squad
- Wildcard: Require a structural five-gameweek case
- Bench Boost and Triple Captain: Evaluate marginal chip value only

### Weekly transfer and chip contract

Starting with GW2, every explicitly triggered refresh must identify the official next event and project that event plus the following four. For Conservative, Balanced, and Aggressive profiles, compare rolling the transfer, one transfer, and two transfers. Use published selling prices and bank; subtract four points for each transfer beyond the reconstructed free-transfer allowance. Rolling is a real scenario with future flexibility value.

The weekly planner uses a receding five-gameweek horizon. It searches legal roll, single-transfer, and double-transfer branches, includes immediate and future hit costs, tracks the actual available free-transfer balance through every state, caps accumulation at the current official maximum, and assigns terminal value to retained squad quality, bank, and transfer flexibility. It recommends only the immediate action. Later transfers are displayed as provisional conditional branches and are rebuilt after every explicit refresh, never as commitments.

The first implemented planner is intentionally transparent about its limits: future market prices are held constant and expected minutes are not yet gameweek-specific. These limitations must be visible in the dashboard.

Chip availability comes from official `bootstrap-static` start/stop windows and published manager chip history. Chip definitions and transfer rules come from the official FPL rules page. Every chip recommendation must include a no-chip counterfactual, modeled marginal value, profile-specific threshold, and hold recommendation when no chip clears that threshold. Unpublished in-gameweek transfers are never inferred.

The team view uses a responsive pitch formation for the starting XI, with bench order shown separately.

### Forecast accountability and calibration

Before a Gameweek starts, the system stores an immutable local forecast snapshot keyed by origin Gameweek, model version, profile, and horizon. A later refresh must not overwrite that snapshot. Each Conservative, Balanced, and Aggressive snapshot records cumulative modeled points, downside and upside bounds, the frozen starting XI, and captain for 1, 3, and 5-Gameweek horizons.

After an official Gameweek is marked finished, an explicit **Refresh now** action may collect player points from the official FPL event-live endpoint. Actual profile points use the frozen XI plus the frozen captain's additional score for every event in the completed horizon. Conditional future transfer branches are not treated as committed forecasts. Hindsight substitutions and autosubs are excluded from this opening-profile benchmark and the limitation is displayed.

The Model Performance view reports modeled and actual points, mean absolute error, signed bias (`actual - modeled`), root mean squared error, and downside/upside interval coverage overall and by horizon and profile. Diagnostics require at least eight completed profile-horizon comparisons before calibration advice is considered usable. The system may recommend reviewing minutes, scoring-rate, or uncertainty assumptions, but it does not silently modify production weights from a small sample. Any model change must preserve the old model version and be validated against frozen historical forecasts before adoption.

## Dashboard

The local dashboard is a dense monitor, not a decorative marketing page. It will show:

1. Data freshness and source status
2. Transfer-window feed with verification and FPL-reconciliation state
3. Current squad and constraints after launch
4. Ranked transfer scenarios
5. Starting XI, bench, captain, and vice-captain
6. Expected-points scenarios and uncertainty
7. Injury, rotation, and role warnings
8. Original source links
9. Decision log and forecast review

**Spec amendment (2026-08-10) — alpha release notes, documentation
only.** The dashboard itself is the operational surface described above;
a separate, human-authored `RELEASE_NOTES.md` at the repo root is the
user-facing companion document for alpha testers, describing only
features live on `main` at time of writing, never planned or
in-progress work (issue #112, `plans/issue-112-release-notes-hosting.md`).
It is plain Markdown rendered by GitHub's own file viewer today; GitHub
Pages hosting was considered and deferred (see
`IMPLEMENTATION_PLAN.md`'s "Considered and declined" entry for the same
date). This does not add a new data source, dashboard view, or
recommendation input — it is documentation of what already exists above.

A quiet or not-yet-launched dataset is shown honestly. No fake player projections, confidence scores, trends, or transfer claims will be generated.

## Operating cadence

### Preseason

- Refresh official FPL data on demand
- Track confirmed transfers and reconcile them with FPL
- Update roles and expected-minutes assumptions
- Produce multiple opening-squad structures
- Produce the final GW1 recommendation near the deadline

### In season

- Collect throughout the week when the dashboard is refreshed
- Produce one final deadline report
- If the manager is unavailable near the deadline, show the safest actionable plan and conditional alternatives

No scheduler is created in this foundation phase. Scheduling will be considered only after an interactive refresh has been verified. That verification has since happened (the Refresh button → `/api/refresh` path), and issue #55's opt-in, admin-configured deadline-reminder GitHub Actions workflow (`.github/workflows/deadline-reminder.yml`, invoking the trigger-agnostic `scripts/send_deadline_reminder.py`) is the anticipated post-verification scheduling exception: it lives outside `server.py` and the refresh pipeline, which still never act on their own, and is slated to move onto issue #27's eventual hosted deployment's own scheduler once that lands.

## Decision-report contract

Every final report must include:

- Recommended action
- No-transfer counterfactual
- One, three, and five-gameweek expected impact
- Hit cost
- Expected-minutes risk
- Captain and vice-captain
- Starting XI and bench order
- Conservative, balanced, and aggressive scenarios
- Source freshness
- Conditions that would change the recommendation

## Implementation phases

1. Transfer-aware collector and normalized local state
2. Initial source-backed dashboard
3. Player role and expected-minutes layer after launch
4. Projection model
5. Legal squad and transfer optimizer
6. Manual preseason and early-gameweek calibration
7. Optional scheduled final-deadline report after verification
