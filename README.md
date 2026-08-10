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
below), open the **My Profile** view, and fill in the **Manager profile**
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

The generated dashboard file remains self-contained. The localhost service supplies the secure refresh endpoint used by the button. No scheduler is configured for the app itself, by explicit choice.

The one anticipated exception is issue #55's opt-in deadline-email reminder: `.github/workflows/deadline-reminder.yml` is a scheduled GitHub Actions workflow (hourly) that invokes the trigger-agnostic `scripts/send_deadline_reminder.py` to email current transfer recommendations a configurable number of hours before each gameweek's deadline. It is admin/secrets-configured (recipient team IDs, emails, and SMTP credentials live in Actions secrets, not in this repo), runs entirely outside `server.py` and the refresh pipeline -- neither of which gains any new self-triggered behavior -- and is expected to move onto issue #27's hosted deployment's own scheduler once that lands.

## Keep dashboard.html in sync with dashboard.py

`dashboard.html` is gitignored (see above) and is not regenerated
automatically just because `src/fpl_intel/dashboard.py`'s template changes
-- pulling a template/CSS/JS change onto `main` leaves the local
`dashboard.html` stale until something rebuilds it.

For a one-off rebuild without hitting any live API (fast, uses the last
cached `data/dashboard-state.json`):

```bash
python3 scripts/rebuild_dashboard.py
```

To do this automatically after every merge and branch checkout, activate
the repo's tracked git hooks once per clone:

```bash
git config core.hooksPath .githooks
```

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

## Dependencies

The dashboard, refresh pipeline, and tests are stdlib-only. The single exception is `numpy`,
needed only by `scripts/fit_ml_minutes_weights.py`, which fits the ridge-regression weights
for the ML minutes shadow challenger (issue #65, see [MODEL.md](MODEL.md)) offline:

```bash
pip install -r requirements.txt
```

## Environment variables

Everything below is optional for local use -- the dashboard, refresh pipeline, and tests all
run with none of these set, exactly as `## Open the dashboard` describes. They only matter for
specific opt-in features (LLM-based news parsing, the deadline-reminder email) or for the
hosted deployment tracked in [issue #27](https://github.com/onkartalekar/fpl-intelligence/issues/27).

None of these are read from a `.env` file -- this codebase has no dotenv loading, by the same
stdlib-only rule as everything else (see `## Dependencies`). Locally, set one by exporting it in
the shell before running the affected script/server, e.g. `export FPL_INTEL_SMTP_HOST=...`; it
isn't picked up any other way.

### LLM-based news parsing (optional)

Unset by default; the feature silently no-ops (no error) when its provider's key is missing.
**Where to set:** nowhere, currently -- `news_signals.py` isn't called from the refresh pipeline,
the server, or any script yet (only its own unit tests exercise it), so these have no live
placement to worry about until it's actually wired into something.

| Variable | Required when | Notes |
|---|---|---|
| `FPL_INTEL_LLM_PROVIDER` | Never | Selects `"claude"` (default) or `"openai_compatible"`. |
| `ANTHROPIC_API_KEY` | Provider is `claude` and you want live parsing | Standard Anthropic API key. |
| `FPL_INTEL_LLM_API_BASE` | Provider is `openai_compatible` | Base URL of any OpenAI Chat Completions-shaped endpoint -- no vendor is hardcoded. |
| `FPL_INTEL_LLM_MODEL` | Provider is `openai_compatible` | Model name at that endpoint. |
| `FPL_INTEL_LLM_API_KEY` | Provider is `openai_compatible` | API key for that endpoint. |

### Deadline-reminder email -- offline script (`scripts/send_deadline_reminder.py`, issue #55)

Not used by the dashboard server itself; only by the separate script the (currently disabled)
`.github/workflows/deadline-reminder.yml` GitHub Actions workflow invokes.
**Where to set:** **GitHub Actions repo secrets** (Settings -> Secrets and variables -> Actions on
the GitHub repo), not Railway -- this workflow runs on GitHub's own runners, unrelated to wherever
the dashboard server is hosted. That stays true even after #27 ships Railway hosting, *unless* you
later choose to move this script onto a Railway cron service against the same `data/` volume
instead of GitHub Actions -- an alternative worth considering once the workflow is re-enabled, but
not yet decided; if you do, these move to Railway's Variables tab like the vars below instead.

| Variable | Required when | Notes |
|---|---|---|
| `FPL_INTEL_REMINDER_TEAMS` | At least one of this or `FPL_INTEL_REMINDER_PROFILES_DB` must resolve at least one team, or the script raises `ConfigError` | JSON list of `{"team_id", "email", "lead_hours"}` objects, manually maintained. |
| `FPL_INTEL_REMINDER_PROFILES_DB` (issue #80) | Never | Path to a `profiles.db` to additionally source opted-in teams from (`reminder_status == "enabled"`). Unset by default. Unioned with `FPL_INTEL_REMINDER_TEAMS` by `team_id`; the explicit-secret entry wins on collision. |
| `FPL_INTEL_SMTP_HOST` / `FPL_INTEL_SMTP_PORT` / `FPL_INTEL_SMTP_USER` / `FPL_INTEL_SMTP_PASSWORD` | To actually send mail (a `--dry-run` flag exists for previewing without them) | Deliberately separate credentials from the server's own SMTP vars below, so each can be rotated independently. |
| `FPL_INTEL_DASHBOARD_BASE_URL` (issue #83) | Never | Base URL used to build the email footer's "manage reminder settings" link. Defaults to `http://localhost:8877`. Once hosted on Railway, set this to the real `https://<app>.up.railway.app` so the footer link isn't a dead `localhost` URL in emails sent from the offline script. |

### Reminder opt-in confirmation email -- live server (`src/fpl_intel/reminder_confirmation.py`, issue #79)

**Where to set:** wherever `server.py`'s process actually runs -- your local shell for local
testing, and **Railway's project Variables tab** once hosted, since these are read at request
time by the same process serving the dashboard. This is the one reminder-email group that
directly matters for the Railway setup, unlike the offline-script group above.

| Variable | Required when | Notes |
|---|---|---|
| `FPL_INTEL_SERVER_SMTP_HOST` / `FPL_INTEL_SERVER_SMTP_PORT` / `FPL_INTEL_SERVER_SMTP_USER` / `FPL_INTEL_SERVER_SMTP_PASSWORD` | To send the double-opt-in confirmation email when a visitor enables reminders from the Profile tab | Separate credentials from the offline script's `FPL_INTEL_SMTP_*` above by design. |

### Hosted deployment (issue #27 -- planned, not yet implemented)

Local (`scripts/start_dashboard.py`) behavior is unaffected either way: the plan calls for these
to default to today's localhost-only behavior when unset, so a plain local checkout keeps working
exactly as it does now once #27 ships.
**Where to set:** all three go in **Railway's project Variables tab** (Project -> your service ->
Variables). `PORT` needs no action -- Railway injects it into the service's environment
automatically, it isn't something you type in yourself. The other two are secrets you set by hand
there; Railway also supports per-environment variable scoping (e.g. separate values for a staging
vs. production environment) if that's ever needed, though a single Hobby-tier service doesn't need
it today.

| Variable | Purpose |
|---|---|
| `PORT` | Railway-injected; the server binds here instead of the hardcoded `8877`. |
| `FPL_INTEL_REFRESH_TOKEN` | Operator-only secret gating `POST /api/refresh`. Read from this env var instead of the random per-process token generated today, and never rendered into any served page. |
| Allowed-host value (exact env var name not yet finalized) | Replaces the hardcoded `127.0.0.1:{port}` check in `_has_trusted_host` / the `Origin` check, so the deployed hostname (e.g. `*.up.railway.app`) is trusted instead of only localhost. |

See [plans/issue-27-cloud-hosting.md](plans/issue-27-cloud-hosting.md) for the full design.

## Tests

```bash
cd <path-to-clone>/fpl-intelligence
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Important evidence rule

An official Premier League or club announcement can confirm a move before FPL launches. The move remains labeled `pending_new_season_fpl` until the new FPL player and club records can be reconciled. No projections are generated from the old-season FPL feed.

## Specification

See [SPECIFICATION.md](SPECIFICATION.md).
