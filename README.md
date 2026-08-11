# FPL Intelligence

A local, source-backed foundation for 2026/27 FPL (Fantasy Premier
League) decisions.

For a plain-English tour of what the dashboard actually does today, see
[RELEASE_NOTES.md](RELEASE_NOTES.md).

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
the `backtest-baseline-*` fixtures are generated locally and gitignored --
a fresh clone won't have them yet. Run a refresh once to create them:

(`data/history/` -- four seasons of prior-year data, committed in full --
is not one of those generated files, but you don't need to do anything
with it either. It exists solely so `config/model-coefficients.json` can
be re-fitted and backtested by someone changing the model; the live
dashboard never reads it. See [MODEL.md](MODEL.md#coefficients-and-validation).)

**`data-seed/`** holds a handful of git-tracked starter copies of files
that otherwise live at `data/<filename>` and are gitignored
(`confirmed-transfers.json`, `official-transfers-latest.json`,
`fpl-fixtures-latest.json`). `scripts/start_dashboard.py` copies each one
into `data/` on startup, but only if it isn't already there -- so a fresh
local clone gets something reasonable to start from before the first
refresh runs, and so does a fresh Railway deploy, where a persistent
volume mounted at `data/` shadows the whole directory (including these
git-tracked files, which are still in the image layer but unreachable
from that path once the volume takes over). Once a real refresh has run,
the files in `data/` are the live ones and `data-seed/`'s copies are no
longer consulted. See `scripts/start_dashboard.py`'s
`seed_missing_data_files` and `plans/issue-27-cloud-hosting.md`'s
2026-08-10 addendum for the full mechanism.

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

There is no in-page refresh control -- see `## Refresh manually from Terminal` below for the only
way to pull new data, locally or hosted. `POST /api/refresh` still exists on the server as an
operator-only HTTP endpoint (for future scripting/automation), gated by `FPL_INTEL_REFRESH_TOKEN`;
it is never reachable from the dashboard UI.

The standalone file remains available at:

```text
<path-to-clone>/fpl-intelligence/dashboard.html
```

When opened as a standalone file, the Manager profile, Draft squad, and deadline-reminder forms
are disabled, since a static HTML file has no server behind it to save anything to.

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
the hosted deployment (issue #27) and its three GitHub Actions workflows, or for the still-unwired
LLM-based news parsing feature.

None of these are read from a `.env` file -- this codebase has no dotenv loading, by the same
stdlib-only rule as everything else (see `## Dependencies`). Locally, set one by exporting it in
the shell before running the affected script/server, e.g. `export FPL_INTEL_SMTP_HOST=...`; it
isn't picked up any other way.

To generate a new high-entropy token value for any of the `*_TOKEN` variables below:
`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.

### Railway (Project -> your service -> Variables tab)

Local (`scripts/start_dashboard.py`) behavior is unaffected either way: none of these are set by
a plain local checkout, so it keeps binding `127.0.0.1` and behaving exactly as before #27.

| Variable | Required | Purpose |
|---|---|---|
| `PORT` | Auto-injected by Railway -- no action needed | The server binds here instead of the local default `8877`; its presence is also what switches the bind host from `127.0.0.1` to `0.0.0.0`. |
| `FPL_INTEL_REFRESH_TOKEN` | Yes | Operator-only secret gating `POST /api/refresh`, and (issue #125) `POST /api/manager-view`'s rate-limit exemption and `POST /api/archive-team-forecast`/`GET /api/registered-teams` (issue #102). **Must be the same value** set as the `FPL_INTEL_REFRESH_TOKEN` GitHub Actions secret below -- every scheduled workflow authenticates to Railway with it. |
| `FPL_INTEL_REMINDER_TEAMS_TOKEN` (issue #105) | Yes, if the reminder job's self-serve roster (below) is used | Gates `GET /api/reminder-teams`, which returns every opted-in manager's email in bulk -- deliberately a separate secret from `FPL_INTEL_REFRESH_TOKEN`, not a reuse of it. **Must match** the same-named GitHub Actions secret. |
| `FPL_INTEL_ALLOWED_ORIGIN` | Yes | The full trusted origin, scheme included, e.g. `https://fpl-intelligence.up.railway.app`. Its host:port becomes the trusted `Host` header value, and the full string becomes the trusted `Origin` header value. Defaults to `http://127.0.0.1:{port}` locally when unset. |
| `FPL_INTEL_SERVER_SMTP_HOST` / `FPL_INTEL_SERVER_SMTP_PORT` / `FPL_INTEL_SERVER_SMTP_USER` / `FPL_INTEL_SERVER_SMTP_PASSWORD` | Yes, to send the double-opt-in reminder confirmation email (issue #79) and Contact Us operator notifications (issue #110) | Read at request time by the live server. Separate credentials from the offline reminder script's own `FPL_INTEL_SMTP_*` below, by design, so each can be rotated independently. A Gmail account with an [app password](https://myaccount.google.com/apppasswords) works well here (`smtp.gmail.com`, port `587`). |

See [plans/issue-27-cloud-hosting.md](plans/issue-27-cloud-hosting.md) for the full hosting design.

### GitHub Actions (Settings -> Secrets and variables -> Actions)

Three scheduled workflows run on GitHub's own runners regardless of where the dashboard is
hosted, and all call back into the live Railway server over HTTP rather than sharing its
filesystem (see [ARCHITECTURE.md](ARCHITECTURE.md)) -- so their secrets live here, not on Railway.

| Variable | Required by | Notes |
|---|---|---|
| `FPL_INTEL_REFRESH_TOKEN` | `scheduled-refresh.yml`, `deadline-reminder.yml` | **Same value** as Railway's `FPL_INTEL_REFRESH_TOKEN` above. Not needed by `live-regression-check.yml`, which deliberately never calls `/api/refresh` with a valid token at all (see that workflow's own comments). |
| `FPL_INTEL_DASHBOARD_BASE_URL` | `scheduled-refresh.yml`, `deadline-reminder.yml`, `live-regression-check.yml` | The live Railway origin, e.g. `https://fpl-intelligence.up.railway.app` (no trailing slash). `live-regression-check.yml` maps this same secret into `FPL_INTEL_LIVE_CHECK_BASE_URL`. |
| `FPL_INTEL_REMINDER_TEAMS` | `deadline-reminder.yml` | JSON list of `{"team_id", "email", "lead_hours"}` objects, manually maintained. Optional if `FPL_INTEL_REMINDER_PROFILES_DB` (below) resolves at least one team instead -- at least one of the two must resolve a non-empty team list, or the script raises `ConfigError`. |
| `FPL_INTEL_REMINDER_PROFILES_DB` (issue #80, source changed by #105) | `deadline-reminder.yml` | Set to any non-blank value (e.g. `1`) to additionally source opted-in teams live from Railway's `GET /api/reminder-teams` (`reminder_status == "enabled"`). No longer a filesystem path -- a GitHub Actions runner has no shared filesystem with Railway to read one from. Unset by default. Unioned with `FPL_INTEL_REMINDER_TEAMS` by `team_id`; the explicit-secret entry wins on collision. |
| `FPL_INTEL_REMINDER_TEAMS_TOKEN` (issue #105) | `deadline-reminder.yml`, only when `FPL_INTEL_REMINDER_PROFILES_DB` is enabled | **Same value** as Railway's `FPL_INTEL_REMINDER_TEAMS_TOKEN` above. |
| `FPL_INTEL_SMTP_HOST` / `FPL_INTEL_SMTP_PORT` / `FPL_INTEL_SMTP_USER` / `FPL_INTEL_SMTP_PASSWORD` | `deadline-reminder.yml`, to actually send mail (a `--dry-run` `workflow_dispatch` input exists for previewing without them) | The offline script's own send credentials -- deliberately separate from Railway's `FPL_INTEL_SERVER_SMTP_*` above, so each can be rotated independently. |
| `FPL_INTEL_SERVER_SMTP_USER` / `FPL_INTEL_SERVER_SMTP_PASSWORD` (issue #119) | `live-regression-check.yml` | **Same values** as Railway's `FPL_INTEL_SERVER_SMTP_USER`/`_PASSWORD` above -- reused to read back the Contact Us notification over IMAP (the same account it's sent to), not a new secret. Without these, the workflow still runs but skips its email-delivery checks. |

`scripts/trigger_scheduled_refresh.py` (`scheduled-refresh.yml`) and `scripts/archive_team_forecasts.py`
(issue #102, also run from `scheduled-refresh.yml`) need only `FPL_INTEL_REFRESH_TOKEN` and
`FPL_INTEL_DASHBOARD_BASE_URL` from the table above -- both already covered by the first two rows.

`live-regression-check.yml` also has three optional variables, not repo secrets since none of
them are sensitive: `FPL_INTEL_LIVE_CHECK_PUBLIC_TEAM_ID` (defaults to `364759`),
`FPL_INTEL_LIVE_CHECK_IMAP_HOST` (defaults to `imap.gmail.com`), and
`FPL_INTEL_LIVE_CHECK_IMAP_PORT` (defaults to `993`) -- set as plain repository variables
(Settings -> Secrets and variables -> Actions -> Variables tab) only if the SMTP account above
isn't Gmail, or you want the live check to look up a different public team.

### LLM-based news parsing (optional, not yet wired into any live pipeline)

Unset by default; the feature silently no-ops (no error) when its provider's key is missing.
`news_signals.py` isn't called from the refresh pipeline, the server, or any script yet, only its
own unit tests exercise it -- so these have no real hosting placement to worry about until it's
actually wired into something.

| Variable | Required when | Notes |
|---|---|---|
| `FPL_INTEL_LLM_PROVIDER` | Never | Selects `"claude"` (default) or `"openai_compatible"`. |
| `ANTHROPIC_API_KEY` | Provider is `claude` and you want live parsing | Standard Anthropic API key. |
| `FPL_INTEL_LLM_API_BASE` | Provider is `openai_compatible` | Base URL of any OpenAI Chat Completions-shaped endpoint -- no vendor is hardcoded. |
| `FPL_INTEL_LLM_MODEL` | Provider is `openai_compatible` | Model name at that endpoint. |
| `FPL_INTEL_LLM_API_KEY` | Provider is `openai_compatible` | API key for that endpoint. |

## Tests

```bash
cd <path-to-clone>/fpl-intelligence
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Important evidence rule

An official Premier League or club announcement can confirm a move before FPL launches. The move remains labeled `pending_new_season_fpl` until the new FPL player and club records can be reconciled. No projections are generated from the old-season FPL feed.

## Architecture

A visual diagram of how the runtime pieces above (Railway server, refresh pipeline, GitHub
Actions workflows, external dependencies) and the model pipeline fit together: see
[ARCHITECTURE.md](ARCHITECTURE.md).

## Specification

See [SPECIFICATION.md](SPECIFICATION.md).
