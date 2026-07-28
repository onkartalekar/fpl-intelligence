# FPL Intelligence

A local, source-backed foundation for 2026/27 FPL (Fantasy Premier
League) decisions.

## Current status

- Manager profile timezone is configurable per user (defaults to Eastern Time)
- Official Premier League transfer-center collection enabled
- Rumors and secondary reports excluded
- Official FPL feed collection enabled
- 2026/27 feed is live: projections and the legal-squad optimizer are active (see below)
- No credentials or account actions

## Configure your manager

The easiest way is in the dashboard itself: start the local server (see
below), open the **My Team** view, and fill in the **Manager profile**
form there. Enter your FPL team ID (found in your FPL entry URL,
`fantasy.premierleague.com/entry/<team_id>/...`), pick your timezone and
risk profile, and optionally confirm your free-transfer count for the
current gameweek. Saving writes straight to `config/user-profile.json` on
your machine and triggers a refresh -- no password or account access is
ever requested.

Editing the file by hand still works and remains a supported fallback,
including when running from the standalone `dashboard.html` file (the
in-UI form is disabled there, since it needs the local server):

```bash
cp config/user-profile.example.json config/user-profile.json
```

Then edit `config/user-profile.json` and set `manager.team_id` to your own
ID. Without it, the dashboard's "My Team" panel stays in a
`not_configured` state and no public manager data is fetched.

Only a few fields in this file are read by the dashboard right now:
`manager.team_id`, `manager.timezone`, `manager.risk_profile`, and
`manager.confirmed_free_transfers` (with
`manager.confirmed_free_transfers_event`). The rest --
`deadline_availability`, `weekly_time_budget_minutes`, `primary_goal`,
`mini_leagues`, and `experience.previous_entry_id` -- are recorded for
your own reference only; editing them doesn't change model behavior yet,
and they aren't exposed in the in-UI form.

## Open the dashboard

`dashboard.html` and everything under `data/` except `data/history/` and
the `backtest-baseline-*`/`confirmed-transfers.json` fixtures are generated
locally and gitignored -- a fresh clone won't have them yet. Run a refresh
once to create them:

(`data/history/` -- four seasons of prior-year data, committed in full --
is not one of those generated files, but you don't need to do anything
with it either. It exists solely so `config/model-coefficients.json` can
be re-fitted and backtested by someone changing the model; the live
dashboard never reads it. See [MODEL.md](MODEL.md#coefficients-and-validation).)

```bash
cd <path-to-clone>/fpl-intelligence
python3 scripts/refresh_dashboard.py
```

Then start the local dashboard service:

```bash
cd <path-to-clone>/fpl-intelligence
python3 scripts/start_dashboard.py
```

This opens:

```text
http://127.0.0.1:8877/dashboard.html
```

Use the **Refresh now** button whenever you want new data. Refreshes are never scheduled and do not run automatically. The button is token-protected, available only through the localhost service, and rebuilds the dashboard before reloading it.

The standalone file remains available at:

```text
<path-to-clone>/fpl-intelligence/dashboard.html
```

When opened as a standalone file, its Refresh button is disabled because a static HTML file cannot securely start the local Python collector.

## Refresh manually from Terminal

From Terminal:

```bash
cd <path-to-clone>/fpl-intelligence
python3 scripts/refresh_dashboard.py
```

The refresh retrieves:

1. Official Premier League transfer-centre playlists
2. Each club's detailed first-party transfer records
3. The official FPL `bootstrap-static` feed
4. Target-season readiness status

It writes:

- `data/official-transfers-latest.json`
- `data/fpl-bootstrap-latest.json`
- `data/dashboard-state.json`
- `dashboard.html`

The generated dashboard file remains self-contained. The localhost service supplies the secure refresh endpoint used by the button. No scheduler is configured, by explicit choice.

## Projection model

Player projections are a deterministic, component-level formula (goal/
assist/clean-sheet/save/bonus expectation, not one blended rate) with its
tunable constants fitted from historical results rather than hand-picked.
No machine learning, no foundation model, no betting odds. The active
model version and its documented limitations are shown in the dashboard's
Decision Center ("Model basis and risks") and Model Status views.

For how each component is computed, what's live versus built-and-not-
adopted, and how squad construction works, see [MODEL.md](MODEL.md).

Coefficients live in `config/model-coefficients.json`. To validate a model
change before adopting it:

```bash
cd <path-to-clone>/fpl-intelligence
python3 scripts/fetch_history.py     # once: pull prior-season data into data/history/
python3 scripts/fit_coefficients.py  # writes a *candidate* file, never overwrites the active config
python3 scripts/run_backtest.py      # scores a model version against 3 prior seasons + a held-out season
```

`fit_coefficients.py` only ever writes `config/model-coefficients.candidate.json`;
promoting a candidate to active is a manual `cp`, reviewed against the
printed before/after backtest comparison first. Full history of what was
tried, adopted, and rejected (including two deliberately-built-and-not-
adopted models) is in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## Tests

```bash
cd <path-to-clone>/fpl-intelligence
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Important evidence rule

An official Premier League or club announcement can confirm a move before FPL launches. The move remains labeled `pending_new_season_fpl` until the new FPL player and club records can be reconciled. No projections are generated from the old-season FPL feed.

## Specification

See [SPECIFICATION.md](SPECIFICATION.md).
