"""Per-team read/write endpoints built on `compute_manager_view` (issues #46/#64/#79/#102/#125,
split by #210):

- GET  /api/shared-state -- the entire base dashboard-state.json, unfiltered.
- GET  /api/manager-view -- one team's manager summary + weekly decision, JSON.
- GET  /api/registered-teams -- every team_id with a saved profile (operator-only).
- POST /api/archive-team-forecast -- archive one team's real weekly decision (operator-only).

Also owns the `default_*_action` factories that build `lookup_action`/`visitor_profile_action`/
`performance_action` -- the closures `_serve_dashboard` (still in `server.py`, the core route)
and the handlers here both consume via `DashboardHandler._resolve_team_lookup`/
`_team_lookup_opted_out`, which stay in `server.py` as cross-cutting plumbing since both this
module's handlers and `_serve_dashboard` need them.
"""

from datetime import datetime, timezone
import json
import secrets

from ..decision_cache import WeeklyDecisionCache, make_cached_weekly_decisions_builder
from ..generation import resolve_artifact
from ..modeling.model_performance import archive_team_forecast, build_team_model_performance
from ..refresh import RefreshAlreadyRunning, compute_manager_view, project_refresh_lock
from ..sources.fpl_data import save_json
from ..storage import profiles
from .common import ALLOWED_REMINDER_LEAD_HOURS, parse_team_id, profiles_db_path

# Issue #102: same per-run bound `refresh.py`'s `_MANAGER_PICKS_TEAM_CAP` (issue #64) already
# established for "loop over every registered team from a scheduled trigger" -- reused here
# rather than inventing a second cap value for the same class of cost.
_REGISTERED_TEAMS_CAP = 25

TEAM_LOOKUP_COOLDOWN_SECONDS = 15

_DEFAULT_VISITOR_PROFILE = {
    "timezone": "America/New_York",
    "confirmed_free_transfers": None,
    "confirmed_free_transfers_event": None,
    "risk_profile": "balanced",
    "draft_squad": None,
    # Issue #79: these three are never defaulted to a non-null placeholder -- None/null means
    # "no reminder ever requested", a real, distinct state (see
    # `profiles._row_to_dict`'s matching comment). `reminder_pending_email` is included for the
    # same reason `email` is: it's personal contact information, filtered out of
    # `state["profile"]` on an explicit lookup of someone else's team, same as `email` itself
    # (see `server.py`'s `_serve_dashboard`).
    "email": None,
    "reminder_status": None,
    "reminder_lead_hours": None,
    "reminder_pending_email": None,
}


def default_team_view_action(root):
    """Build the default per-request team-lookup action from the shared refresh's cached artifacts.

    Issue #208: `cache`/`build_weekly_decisions` below are created once per call to this factory
    -- i.e. once per `create_server`, so once per server process (or per test instance) -- and
    then reused by every request `action` handles. That single `WeeklyDecisionCache` is what lets
    a second lookup of the same team, against the same shared-data generation and an unchanged
    manager/profile, skip `build_transfer_decisions`/`build_draft_decisions` entirely instead of
    recomputing from scratch. See `decision_cache.py`'s module docstring for the full design.
    """
    cache = WeeklyDecisionCache()
    build_weekly_decisions = make_cached_weekly_decisions_builder(root, cache)

    def action(team_id):
        bootstrap = json.loads(
            resolve_artifact(root, "fpl-bootstrap-latest.json").read_text(encoding="utf-8")
        )
        # Defense-in-depth (see `scripts/start_dashboard.py`'s `seed_missing_data_files`, the
        # primary fix): both files are meant to always exist -- either seeded on first boot from
        # `data-seed/` or git-tracked in a plain local checkout, then kept current by every
        # successful refresh -- so these fallbacks should essentially never fire in practice.
        # They exist only so a team lookup degrades gracefully rather than hard-failing if one is
        # ever transiently missing anyway (a corrupted volume, a manual `rm`, ...). Fallback
        # shapes match `refresh.py`'s own: `[]` mirrors `_load_current_json(root,
        # "fpl-fixtures-latest.json", [])` in `_refresh_project_unlocked`; `{"transfers": []}`
        # mirrors what `transfers_artifact.get("transfers", [])` below already treats a missing
        # key as.
        fixtures_path = resolve_artifact(root, "fpl-fixtures-latest.json")
        raw_fixtures = (
            json.loads(fixtures_path.read_text(encoding="utf-8")) if fixtures_path.exists() else []
        )
        transfers_path = resolve_artifact(root, "official-transfers-latest.json")
        transfers_artifact = (
            json.loads(transfers_path.read_text(encoding="utf-8"))
            if transfers_path.exists() else {"transfers": []}
        )
        saved = profiles.load_profile(profiles_db_path(root), team_id)
        generated_at = datetime.now(timezone.utc).isoformat()
        return compute_manager_view(
            bootstrap,
            raw_fixtures,
            transfers_artifact.get("transfers", []),
            generated_at,
            team_id,
            confirmed_free_transfers=saved["confirmed_free_transfers"] if saved else None,
            confirmed_free_transfers_event=saved["confirmed_free_transfers_event"] if saved else None,
            draft_squad_ids=saved["draft_squad"] if saved else None,
            build_weekly_decisions=build_weekly_decisions,
        )

    return action


def default_model_performance_action(root):
    """Build the default per-team model-performance reader, for splicing into a served page.

    Reads the shared, per-team-keyed `model-performance.json` (issue #64) and scores just the
    resolved team's slice at request time -- mirrors `default_team_view_action`'s role for
    `state["manager"]`/weekly decisions.
    """

    def action(team_id):
        performance_path = resolve_artifact(root, "model-performance.json")
        store = (
            json.loads(performance_path.read_text(encoding="utf-8"))
            if performance_path.exists() else {}
        )
        return build_team_model_performance(store, team_id)

    return action


def default_visitor_profile_action(root):
    """Build the default per-team saved-profile reader, for splicing into a served page.

    Issue #79: `email`/`reminder_status`/`reminder_lead_hours`/`reminder_pending_email` ARE
    personal contact information, unlike every other field returned here -- this function still
    always returns them (so the visitor's own-team view has everything it needs), but
    `server.py`'s `_serve_dashboard` filters them back out of `state["profile"]` whenever the
    request is an explicit `?team_id=` lookup of someone else's team. Fixing that filtering here
    instead of at every call site would require every injected `profile_read_action` (tests, and
    any future caller) to independently know to leave them out; doing it once at the single
    splice site in `_serve_dashboard` is the fix the plan calls for.
    """

    def action(team_id):
        saved = profiles.load_profile(profiles_db_path(root), team_id)
        if saved is None:
            return {"team_id": team_id, **_DEFAULT_VISITOR_PROFILE}
        return {
            "team_id": saved["team_id"],
            "timezone": saved["timezone"],
            "confirmed_free_transfers": saved["confirmed_free_transfers"],
            "confirmed_free_transfers_event": saved["confirmed_free_transfers_event"],
            "risk_profile": saved["risk_profile"],
            "draft_squad": saved["draft_squad"],
            "email": saved["email"],
            "reminder_status": saved["reminder_status"],
            "reminder_lead_hours": saved["reminder_lead_hours"],
            "reminder_pending_email": saved["reminder_pending_email"],
        }

    return action


def make_handle_shared_state(root):
    """Build the GET /api/shared-state handler (issue #125): JSON equivalent of the no-team_id
    dashboard view -- the entire base `dashboard-state.json`, unfiltered. Not new exposure:
    byte-for-byte what a no-team_id visitor already gets embedded in the rendered page (fresh per
    request since #120), and the shared refresh's default `manager` state on the hosted
    deployment is always `{"status": "not_configured", ...}` (`refresh.py:193`) -- no per-visitor
    PII is ever baked into the shared state to begin with. Public, no rate limit: identical cost/
    exposure profile to the existing public `/`/`/dashboard.html` route.
    """

    def handle_shared_state(self):
        state_path = resolve_artifact(root, "dashboard-state.json")
        if not state_path.exists():
            self._json(404, {"status": "error", "message": "Dashboard has not been generated"})
            return
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self._json(200, state)

    return handle_shared_state


def make_handle_manager_view(lookup_limiter):
    """Build the GET /api/manager-view handler (issue #125): JSON equivalent of `_serve_dashboard`
    's explicit `?team_id=` lookup -- same opt-out (#62) and rate-limiting (#46) rules, minus the
    HTML-only splices (`visitor_profile`/`model_performance`) that carry PII #79 already had to
    filter for the HTML path; this endpoint never returns those fields at all, so there's nothing
    to filter. Built so `send_deadline_reminder.py` (and any future GitHub-Actions-hosted script)
    can fetch exactly what `compute_manager_view` already computes server-side -- including
    profile overrides read from Railway's real `profiles.db` -- instead of trying to read local
    files that don't exist wherever the script happens to run.
    """

    def handle_manager_view(self, query_string):
        team_id = parse_team_id(query_string)
        if team_id is None:
            self._json(400, {"status": "error", "message": "A valid team_id query parameter is required."})
            return
        if not self._rate_limit_exempt() and not lookup_limiter.allow(self.client_address[0]):
            self._json(429, {"status": "error", "message": "Too many team lookups. Try again shortly."})
            return
        if self._team_lookup_opted_out(team_id):
            self._json(200, {"status": "opted_out", "team_id": team_id})
            return
        manager, weekly_decisions = self._resolve_team_lookup(team_id)
        if manager is None:
            self._json(500, {"status": "error", "message": "Team lookup failed"})
            return
        self._json(
            200,
            {"status": "ok", "team_id": team_id, "manager": manager, "weekly_decisions": weekly_decisions},
        )

    return handle_manager_view


def make_handle_registered_teams(root, token):
    """Build the GET /api/registered-teams handler (issue #102): every team_id with a saved
    profile, capped at `_REGISTERED_TEAMS_CAP` -- the read counterpart
    `scripts/archive_team_forecasts.py` needs to discover which teams to archive a forecast for,
    the same structural gap #105's `/api/reminder-teams` closed for the (much smaller)
    reminder-opted-in subset. Deliberately a separate, broader endpoint rather than widening
    `/api/reminder-teams`'s own meaning -- "every registered team" and "every team that opted
    into reminder emails" are different, independently-useful sets.

    Gated by the same `X-Refresh-Token` `/api/refresh` and `/api/archive-team-forecast` already
    require, not a dedicated token like `/api/reminder-teams` -- this returns bare team IDs only,
    no email/PII of any kind, so it carries the same sensitivity as those two operator-only,
    non-PII-exposing endpoints, not #105's bulk-PII case.
    """

    def handle_registered_teams(self):
        if not secrets.compare_digest(self.headers.get("X-Refresh-Token", ""), token):
            self._json(403, {"status": "error", "message": "Invalid refresh token"})
            return
        team_ids = profiles.list_team_ids(profiles_db_path(root))[:_REGISTERED_TEAMS_CAP]
        self._json(200, {"status": "ok", "team_ids": team_ids})

    return handle_registered_teams


def make_handle_archive_team_forecast(root):
    """Build the POST /api/archive-team-forecast handler (issue #102): archive one team's real
    weekly decision at one deadline checkpoint into the shared model-performance.json.

    Gated on the same `X-Refresh-Token` `/api/refresh` already requires, not a dedicated token
    like #105's `/api/reminder-teams` -- unlike that endpoint, this one returns no PII at all
    (only player IDs and recommendation metadata), so it carries the same sensitivity as
    `/api/refresh` itself (an operator-only action that mutates shared server state), not a
    bulk-PII-exposure risk needing its own secret.

    Computes the team's live `weekly_decisions` via the exact same `_resolve_team_lookup` helper
    `/api/manager-view` uses, so this can never drift from what a visitor's own dashboard view or
    a GitHub-Actions-hosted script's `/api/manager-view` call would see. The archive write itself
    is guarded by `project_refresh_lock` -- the same cross-process file lock `/api/refresh`'s
    subprocess eventually acquires via `refresh_project` -- so a concurrent full refresh (which
    loads, mutates, and wholesale republishes `model-performance.json` as one of several
    artifacts) can never silently clobber this endpoint's incremental update, or vice versa.

    Token already checked by `do_POST` before dispatch, same as `/api/refresh`.
    """

    def handle_archive_team_forecast(self, body):
        try:
            payload = json.loads(body.decode("utf-8")) if body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"status": "error", "message": "Invalid archive-team-forecast payload"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"status": "error", "message": "Invalid archive-team-forecast payload"})
            return
        team_id = payload.get("team_id")
        if isinstance(team_id, bool) or not isinstance(team_id, int) or not (1 <= team_id <= 99_999_999):
            self._json(400, {"status": "error", "message": "A valid team_id is required"})
            return
        lead_hours = payload.get("lead_hours")
        if lead_hours not in ALLOWED_REMINDER_LEAD_HOURS:
            self._json(
                400,
                {"status": "error", "message": "lead_hours must be one of 3, 12, 24"},
            )
            return
        manager, weekly_decisions = self._resolve_team_lookup(team_id)
        if manager is None:
            self._json(500, {"status": "error", "message": "Team lookup failed"})
            return
        try:
            with project_refresh_lock(root):
                store_path = resolve_artifact(root, "model-performance.json")
                store = (
                    json.loads(store_path.read_text(encoding="utf-8")) if store_path.exists() else {}
                )
                before = json.dumps(store.get("team_forecasts", {}).get(str(team_id), {}), sort_keys=True)
                archive_team_forecast(store, team_id, weekly_decisions, lead_hours)
                after = json.dumps(store.get("team_forecasts", {}).get(str(team_id), {}), sort_keys=True)
                archived = before != after
                if archived:
                    save_json(store_path, store)
        except RefreshAlreadyRunning:
            self._json(409, {"status": "busy", "message": "A refresh is already running"})
            return
        self._json(200, {"status": "ok", "team_id": team_id, "archived": archived})

    return handle_archive_team_forecast
