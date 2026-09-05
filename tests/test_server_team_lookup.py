"""Per-team read/write endpoint tests (issues #46/#64/#79/#102/#125): /api/shared-state,
/api/manager-view, /api/registered-teams, /api/archive-team-forecast, plus the
ModelPerformance splice into _serve_dashboard. Split out of test_server.py by issue #210 to
mirror server_handlers/team_lookup.py."""


from contextlib import redirect_stderr, redirect_stdout
import http.client
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fpl_intel.generation import publish_generation
from fpl_intel.storage.profiles import (
    confirm_reminder, load_profile, save_profile, set_lookup_opt_out, set_reminder_pending,
)
from fpl_intel.modeling.recommendations import build_gw_recommendations
from fpl_intel.refresh import RefreshAlreadyRunning, project_refresh_lock
from fpl_intel.rate_limit import CooldownLimiter
from fpl_intel.notifications.reminder_confirmation import (
    ReminderEmailError,
    SMTP_HOST_ENV_VAR, SMTP_PASSWORD_ENV_VAR, SMTP_PORT_ENV_VAR, SMTP_USER_ENV_VAR,
)
from fpl_intel.server import (
    ReminderOptInCooldownError,
    _default_refresh_action,
    _default_reminder_opt_in_action,
    _default_team_view_action,
    _default_visitor_profile_action,
    build_refresh_result,
    create_server,
)
from tests.test_recommendations import sample_bootstrap, sample_fixtures


class TeamLookupTests(unittest.TestCase):
    """The unauthenticated no-signup lookup path added for issue #46."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-07-18T12:00:00Z", "profile": {"team_id": None}}),
            encoding="utf-8",
        )
        self.lookup_calls = []

        def team_view_action(team_id):
            self.lookup_calls.append(team_id)
            return {
                "manager": {"connection_status": "connected", "team_id": team_id, "team_name": "BrunoMans", "squad": []},
                "weekly_decisions": {"status": "waiting_for_gw2", "event": 1},
            }

        self.team_view_action = team_view_action
        self.server = create_server(
            self.root,
            host="127.0.0.1",
            port=0,
            token="test-token",
            team_view_action=team_view_action,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def test_valid_team_id_splices_the_looked_up_manager_into_the_rendered_page(self):
        html = urlopen(self.base_url + "/?team_id=364759", timeout=3).read().decode()

        self.assertEqual(self.lookup_calls, [364759])
        self.assertIn("BrunoMans", html)
        self.assertNotIn("test-token", html)
        self.assertNotIn("__DASHBOARD_DATA__", html)

    def test_absent_team_id_serves_the_shared_dashboard_state_unmodified(self):
        html = urlopen(self.base_url + "/dashboard.html", timeout=3).read().decode()

        self.assertEqual(self.lookup_calls, [])
        self.assertIn('"generated_at": "2026-07-18T12:00:00Z"', html)

    def test_malformed_team_id_falls_back_to_the_shared_dashboard_state(self):
        html = urlopen(self.base_url + "/?team_id=not-a-number", timeout=3).read().decode()

        self.assertEqual(self.lookup_calls, [])
        self.assertIn('"generated_at": "2026-07-18T12:00:00Z"', html)

    def test_out_of_range_team_id_falls_back_to_the_shared_dashboard_state(self):
        html = urlopen(self.base_url + "/?team_id=999999999", timeout=3).read().decode()

        self.assertEqual(self.lookup_calls, [])
        self.assertIn('"generated_at": "2026-07-18T12:00:00Z"', html)

    def test_repeated_lookups_from_the_same_source_are_rate_limited(self):
        first = urlopen(self.base_url + "/?team_id=364759", timeout=3)
        self.assertEqual(first.status, 200)

        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/?team_id=100001", timeout=3)

        self.assertEqual(error.exception.code, 429)
        self.assertEqual(self.lookup_calls, [364759])

    def test_lookup_failure_is_reported_cleanly_instead_of_a_server_error(self):
        failing_server = create_server(
            self.root,
            host="127.0.0.1",
            port=0,
            token="test-token",
            team_view_action=lambda team_id: (_ for _ in ()).throw(RuntimeError("upstream down")),
        )
        thread = threading.Thread(target=failing_server.serve_forever, daemon=True)
        thread.start()
        try:
            html = urlopen(
                f"http://127.0.0.1:{failing_server.server_port}/?team_id=364759", timeout=3
            ).read().decode()

            self.assertIn('"status": "error"', html)
            self.assertNotIn("upstream down", html)
        finally:
            failing_server.shutdown()
            failing_server.server_close()
            thread.join(timeout=2)

    def test_missing_dashboard_state_returns_404_for_a_lookup(self):
        (self.root / "data" / "dashboard-state.json").unlink()

        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/?team_id=364759", timeout=3)

        self.assertEqual(error.exception.code, 404)

    def test_missing_dashboard_state_returns_404_with_no_team_id(self):
        """Issue #120: the no-team_id path renders dashboard-state.json fresh on every request
        now, instead of serving a static dashboard.html -- it must 404 the same way the
        team_id-resolved path already does when that state hasn't been generated yet."""
        (self.root / "data" / "dashboard-state.json").unlink()

        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/dashboard.html", timeout=3)

        self.assertEqual(error.exception.code, 404)

class VolumeShadowedTeamLookupTests(unittest.TestCase):
    """Regression coverage for the live Railway bug: a volume mounted at `data/` shadows the
    git-tracked seed files that used to live directly under it. `_default_team_view_action`'s
    per-request closure reads `fpl-fixtures-latest.json`/`official-transfers-latest.json` off
    disk with no existence check -- this exercises that read against a `data/` directory built to
    look exactly like a freshly mounted (nothing seeded) volume, and would have raised
    `FileNotFoundError` against the pre-fix code.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "fpl-bootstrap-latest.json").write_text(
            json.dumps(sample_bootstrap()), encoding="utf-8"
        )
        # Deliberately no fpl-fixtures-latest.json / official-transfers-latest.json here.

    def tearDown(self):
        self.directory.cleanup()

    def test_team_lookup_against_a_shadowed_data_dir_degrades_gracefully(self):
        with patch(
            "fpl_intel.refresh.collect_public_manager",
            return_value={"entry": {"id": 364759, "name": "BrunoMans"}, "picks": []},
        ):
            result = _default_team_view_action(self.root)(364759)

        # No FileNotFoundError raised -- fixtures/transfers both degraded to empty defaults, and
        # the rest of compute_manager_view still ran to completion on top of them.
        self.assertIn("manager", result)
        self.assertIn("weekly_decisions", result)

class SharedStateApiTests(unittest.TestCase):
    """Issue #125: GET /api/shared-state -- the JSON equivalent of the no-team_id dashboard view,
    for GitHub-Actions-hosted scripts that can't reach Railway's local filesystem."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        self.server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def test_returns_the_full_dashboard_state_as_json(self):
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({
                "generated_at": "2026-08-11T12:00:00Z",
                "decision_center": {"status": "active", "recommended_squad": {"formation": "3-4-3"}},
            }),
            encoding="utf-8",
        )

        response = urlopen(self.base_url + "/api/shared-state", timeout=3)
        payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get("Content-Type"), "application/json; charset=utf-8")
        self.assertEqual(payload["generated_at"], "2026-08-11T12:00:00Z")
        self.assertEqual(payload["decision_center"]["recommended_squad"]["formation"], "3-4-3")

    def test_missing_dashboard_state_returns_404(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/api/shared-state", timeout=3)

        self.assertEqual(error.exception.code, 404)

    def test_is_not_rate_limited(self):
        """Plain file read, same cost profile as the existing public / route -- no per-request
        cost to protect, unlike the live-FPL-API-hitting team lookup."""
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-08-11T12:00:00Z"}), encoding="utf-8",
        )

        for _ in range(5):
            response = urlopen(self.base_url + "/api/shared-state", timeout=3)
            self.assertEqual(response.status, 200)

class ManagerViewApiTests(unittest.TestCase):
    """Issue #125: GET /api/manager-view?team_id=<id> -- the JSON equivalent of the explicit
    ?team_id= HTML lookup (#46), sharing the same opt-out (#62) and rate-limiting rules, minus the
    HTML-only profile splice (#79) this endpoint never returns in the first place."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-08-11T12:00:00Z"}), encoding="utf-8",
        )
        self.db_path = self.root / "data" / "profiles.db"
        self.lookup_calls = []

        def team_view_action(team_id):
            self.lookup_calls.append(team_id)
            return {
                "manager": {"connection_status": "connected", "team_id": team_id, "team_name": "BrunoMans", "squad": []},
                "weekly_decisions": {"status": "waiting_for_gw2", "event": 1},
            }

        self.team_view_action = team_view_action
        self.server = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
            team_view_action=team_view_action,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def test_valid_team_id_returns_manager_and_weekly_decisions(self):
        response = urlopen(self.base_url + "/api/manager-view?team_id=364759", timeout=3)
        payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(self.lookup_calls, [364759])
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["manager"]["team_name"], "BrunoMans")
        self.assertEqual(payload["weekly_decisions"]["status"], "waiting_for_gw2")
        # Never returns the HTML path's separate visitor_profile splice -- nothing to filter.
        self.assertNotIn("profile", payload)

    def test_missing_team_id_is_a_400(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/api/manager-view", timeout=3)

        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.lookup_calls, [])

    def test_malformed_team_id_is_a_400(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/api/manager-view?team_id=not-a-number", timeout=3)

        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.lookup_calls, [])

    def test_opted_out_team_blocks_the_lookup_without_calling_the_live_action(self):
        set_lookup_opt_out(
            self.db_path, team_id=364759, opted_out=True, pin_hash="some-hash",
            now="2026-08-08T00:00:00Z",
        )

        response = urlopen(self.base_url + "/api/manager-view?team_id=364759", timeout=3)
        payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(self.lookup_calls, [])
        self.assertEqual(payload, {"status": "opted_out", "team_id": 364759})

    def test_lookup_failure_is_a_500(self):
        def failing_team_view_action(team_id):
            raise RuntimeError("upstream unavailable")

        failing_server = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
            team_view_action=failing_team_view_action,
        )
        thread = threading.Thread(target=failing_server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(f"http://127.0.0.1:{failing_server.server_port}/api/manager-view?team_id=364759", timeout=3)
            self.assertEqual(error.exception.code, 500)
        finally:
            failing_server.shutdown()
            failing_server.server_close()
            thread.join(timeout=2)

    def test_repeated_calls_from_the_same_source_are_rate_limited(self):
        first = urlopen(self.base_url + "/api/manager-view?team_id=364759", timeout=3)
        self.assertEqual(first.status, 200)

        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/api/manager-view?team_id=100001", timeout=3)

        self.assertEqual(error.exception.code, 429)
        self.assertEqual(self.lookup_calls, [364759])

    def test_a_valid_refresh_token_is_exempt_from_the_rate_limit(self):
        """Issue #125: a trusted script (e.g. the deadline reminder, looping over several teams
        in one run) must not trip the visitor-tuned per-IP cooldown on its own repeat calls."""
        first = urlopen(
            Request(self.base_url + "/api/manager-view?team_id=364759", headers={"X-Refresh-Token": "test-token"}),
            timeout=3,
        )
        second = urlopen(
            Request(self.base_url + "/api/manager-view?team_id=100001", headers={"X-Refresh-Token": "test-token"}),
            timeout=3,
        )

        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertEqual(self.lookup_calls, [364759, 100001])

    def test_an_invalid_refresh_token_does_not_exempt_from_the_rate_limit(self):
        first = urlopen(
            Request(self.base_url + "/api/manager-view?team_id=364759", headers={"X-Refresh-Token": "wrong-token"}),
            timeout=3,
        )
        self.assertEqual(first.status, 200)

        with self.assertRaises(HTTPError) as error:
            urlopen(
                Request(self.base_url + "/api/manager-view?team_id=100001", headers={"X-Refresh-Token": "wrong-token"}),
                timeout=3,
            )

        self.assertEqual(error.exception.code, 429)


class RegisteredTeamsApiTests(unittest.TestCase):
    """Issue #102: GET /api/registered-teams -- every team_id with a saved profile, for
    scripts/archive_team_forecasts.py to discover which teams to archive a forecast for. Bare
    team IDs only, no PII -- gated by the ordinary token (/api/refresh's), not a dedicated one
    like #105's /api/reminder-teams."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        self.db_path = self.root / "data" / "profiles.db"
        self.now = "2026-08-11T12:00:00Z"
        self.server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def _get(self, token="test-token"):
        headers = {"X-Refresh-Token": token} if token is not None else {}
        return urlopen(Request(self.base_url + "/api/registered-teams", headers=headers), timeout=3)

    def test_missing_token_is_a_403(self):
        with self.assertRaises(HTTPError) as error:
            self._get(token=None)
        self.assertEqual(error.exception.code, 403)

    def test_invalid_token_is_a_403(self):
        with self.assertRaises(HTTPError) as error:
            self._get(token="wrong-token")
        self.assertEqual(error.exception.code, 403)

    def test_returns_every_registered_team_id_ascending(self):
        save_profile(self.db_path, 300, "America/New_York", "balanced", None, None, self.now)
        save_profile(self.db_path, 100, "America/New_York", "balanced", None, None, self.now)
        save_profile(self.db_path, 200, "America/New_York", "balanced", None, None, self.now)

        response = self._get()
        payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(payload, {"status": "ok", "team_ids": [100, 200, 300]})

    def test_no_profiles_at_all_yields_an_empty_list(self):
        response = self._get()
        payload = json.loads(response.read())

        self.assertEqual(payload, {"status": "ok", "team_ids": []})

    def test_caps_at_the_registered_teams_limit(self):
        for team_id in range(1, 30):
            save_profile(self.db_path, team_id, "America/New_York", "balanced", None, None, self.now)

        response = self._get()
        payload = json.loads(response.read())

        self.assertEqual(len(payload["team_ids"]), 25)


def _active_weekly_decisions(event=2):
    recommendation = {
        "action": "roll",
        "transfer_count": 0,
        "point_cost": 0,
        "net_gain_5gw": 3.2,
        "projected_event_points_including_captain": 55.0,
        "formation": "3-4-3",
        "starting_xi": [{"id": player_id} for player_id in range(1, 12)],
        "bench": [{"id": player_id} for player_id in range(12, 16)],
        "captain": {"id": 1},
        "vice_captain": {"id": 2},
    }
    return {
        "status": "active",
        "event": event,
        "generated_at": "2026-08-20T12:00:00-04:00",
        "profiles": [{"id": "balanced", "recommendation": recommendation}],
    }


class ArchiveTeamForecastApiTests(unittest.TestCase):
    """Issue #102: POST /api/archive-team-forecast -- archives one team's real weekly decision
    at one checkpoint into the shared model-performance.json."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()

        def team_view_action(team_id):
            return {"manager": {"connection_status": "connected"}, "weekly_decisions": _active_weekly_decisions()}

        self.team_view_action = team_view_action
        self.server = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token", team_view_action=team_view_action,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def _post(self, payload, token="test-token"):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Refresh-Token"] = token
        return urlopen(
            Request(
                self.base_url + "/api/archive-team-forecast",
                data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers,
            ),
            timeout=3,
        )

    def test_missing_token_is_a_403(self):
        with self.assertRaises(HTTPError) as error:
            self._post({"team_id": 364759, "lead_hours": 24}, token=None)
        self.assertEqual(error.exception.code, 403)

    def test_invalid_team_id_is_a_400(self):
        with self.assertRaises(HTTPError) as error:
            self._post({"team_id": -1, "lead_hours": 24})
        self.assertEqual(error.exception.code, 400)

    def test_invalid_lead_hours_is_a_400(self):
        with self.assertRaises(HTTPError) as error:
            self._post({"team_id": 364759, "lead_hours": 6})
        self.assertEqual(error.exception.code, 400)

    def test_archives_and_persists_to_model_performance_json(self):
        response = self._post({"team_id": 364759, "lead_hours": 24})
        payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(payload, {"status": "ok", "team_id": 364759, "archived": True})
        store = json.loads((self.root / "data" / "model-performance.json").read_text(encoding="utf-8"))
        self.assertIn("gw2:24", store["team_forecasts"]["364759"])

    def test_repeated_call_for_the_same_checkpoint_reports_not_archived(self):
        self._post({"team_id": 364759, "lead_hours": 24})

        response = self._post({"team_id": 364759, "lead_hours": 24})
        payload = json.loads(response.read())

        self.assertEqual(payload["archived"], False)

    def _write_bootstrap_with_gw2_deadline(self, deadline):
        (self.root / "data" / "fpl-bootstrap-latest.json").write_text(
            json.dumps({"events": [{"id": 2, "deadline_time": deadline, "is_next": True}]}),
            encoding="utf-8",
        )

    def test_pre_deadline_forecast_is_archived_when_the_deadline_is_resolvable(self):
        """Issue #286: with a real bootstrap present, a forecast generated before its event's
        deadline still archives normally -- the backstop only rejects post-deadline ones."""
        self._write_bootstrap_with_gw2_deadline("2026-08-21T17:30:00Z")  # after generated_at 16:00Z

        response = self._post({"team_id": 364759, "lead_hours": 24})
        payload = json.loads(response.read())

        self.assertEqual(payload, {"status": "ok", "team_id": 364759, "archived": True})
        store = json.loads((self.root / "data" / "model-performance.json").read_text(encoding="utf-8"))
        self.assertIn("gw2:24", store["team_forecasts"]["364759"])

    def test_post_deadline_forecast_is_refused_by_the_server_side_backstop(self):
        """Issue #286: a decision whose generated_at is past its event deadline is
        hindsight-contaminated -- the endpoint returns 200 but archives nothing."""
        self._write_bootstrap_with_gw2_deadline("2026-08-19T00:00:00Z")  # before generated_at 16:00Z

        response = self._post({"team_id": 364759, "lead_hours": 24})
        payload = json.loads(response.read())

        self.assertEqual(payload, {"status": "ok", "team_id": 364759, "archived": False})
        self.assertFalse((self.root / "data" / "model-performance.json").exists())

    def test_lookup_failure_is_a_500(self):
        def failing_team_view_action(team_id):
            raise RuntimeError("upstream unavailable")

        failing_server = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
            team_view_action=failing_team_view_action,
        )
        thread = threading.Thread(target=failing_server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(
                    Request(
                        f"http://127.0.0.1:{failing_server.server_port}/api/archive-team-forecast",
                        data=json.dumps({"team_id": 364759, "lead_hours": 24}).encode("utf-8"),
                        method="POST", headers={"X-Refresh-Token": "test-token", "Content-Type": "application/json"},
                    ),
                    timeout=3,
                )
            self.assertEqual(error.exception.code, 500)
        finally:
            failing_server.shutdown()
            failing_server.server_close()
            thread.join(timeout=2)

class ModelPerformanceSpliceTests(unittest.TestCase):
    """Issue #64: team_performance/player_performance computed and spliced at request time."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({
                "generated_at": "2026-07-18T12:00:00Z",
                "model_performance": {"status": "waiting_for_results", "comparisons": []},
            }),
            encoding="utf-8",
        )
        self.performance_calls = []

        def team_view_action(team_id):
            return {
                "manager": {"connection_status": "connected", "team_id": team_id, "team_name": f"Team {team_id}", "squad": []},
                "weekly_decisions": {"status": "waiting_for_gw2", "event": 1},
            }

        def model_performance_action(team_id):
            self.performance_calls.append(team_id)
            return {
                "team_performance": {
                    "status": "active",
                    "comparisons": [{"event": 1, "modeled_points": float(team_id)}],
                },
                "player_performance": {"status": "waiting_for_results", "comparisons": []},
            }

        self.model_performance_action = model_performance_action
        self.server = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
            team_view_action=team_view_action,
            model_performance_action=model_performance_action,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def test_splices_the_resolved_teams_model_performance(self):
        html = urlopen(self.base_url + "/?team_id=111", timeout=3).read().decode()

        self.assertEqual(self.performance_calls, [111])
        self.assertIn('"modeled_points": 111.0', html)

    def test_two_different_teams_get_distinct_model_performance_slices(self):
        # Two independent servers (rather than two sequential requests to one) so the per-IP
        # lookup cooldown limiter doesn't block the second request -- that's issue #46's own
        # concern, not what this test is verifying.
        first_calls = []
        second_calls = []

        def model_performance_action_for(calls):
            def action(team_id):
                calls.append(team_id)
                return {
                    "team_performance": {
                        "status": "active",
                        "comparisons": [{"event": 1, "modeled_points": float(team_id)}],
                    },
                    "player_performance": {"status": "waiting_for_results", "comparisons": []},
                }
            return action

        def team_view_action(team_id):
            return {
                "manager": {"connection_status": "connected", "team_id": team_id, "squad": []},
                "weekly_decisions": {"status": "waiting_for_gw2", "event": 1},
            }

        server_a = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
            team_view_action=team_view_action,
            model_performance_action=model_performance_action_for(first_calls),
        )
        server_b = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
            team_view_action=team_view_action,
            model_performance_action=model_performance_action_for(second_calls),
        )
        thread_a = threading.Thread(target=server_a.serve_forever, daemon=True)
        thread_b = threading.Thread(target=server_b.serve_forever, daemon=True)
        thread_a.start()
        thread_b.start()
        try:
            first = urlopen(
                f"http://127.0.0.1:{server_a.server_port}/?team_id=111", timeout=3
            ).read().decode()
            second = urlopen(
                f"http://127.0.0.1:{server_b.server_port}/?team_id=222", timeout=3
            ).read().decode()

            self.assertEqual(first_calls, [111])
            self.assertEqual(second_calls, [222])
            self.assertIn('"modeled_points": 111.0', first)
            self.assertNotIn('"modeled_points": 222.0', first)
            self.assertIn('"modeled_points": 222.0', second)
            self.assertNotIn('"modeled_points": 111.0', second)
        finally:
            server_a.shutdown()
            server_a.server_close()
            thread_a.join(timeout=2)
            server_b.shutdown()
            server_b.server_close()
            thread_b.join(timeout=2)

    def test_absent_team_id_serves_the_dashboard_state_without_computing_performance(self):
        html = urlopen(self.base_url + "/dashboard.html", timeout=3).read().decode()

        self.assertEqual(self.performance_calls, [])
        self.assertIn('"status": "waiting_for_results"', html)

    def test_model_performance_failure_is_reported_cleanly_instead_of_a_server_error(self):
        failing_server = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
            team_view_action=lambda team_id: {
                "manager": {"connection_status": "connected", "team_id": team_id, "squad": []},
                "weekly_decisions": {"status": "waiting_for_gw2", "event": 1},
            },
            model_performance_action=lambda team_id: (_ for _ in ()).throw(RuntimeError("store corrupt")),
        )
        thread = threading.Thread(target=failing_server.serve_forever, daemon=True)
        thread.start()
        try:
            html = urlopen(
                f"http://127.0.0.1:{failing_server.server_port}/?team_id=364759", timeout=3
            ).read().decode()

            self.assertIn('"status": "error"', html)
            self.assertNotIn("store corrupt", html)
        finally:
            failing_server.shutdown()
            failing_server.server_close()
            thread.join(timeout=2)

class PlanDiffSpliceTests(unittest.TestCase):
    """Issue #266: the week-over-week plan-diff comparison, spliced onto weekly_decisions itself
    at request time -- same DI pattern ModelPerformanceSpliceTests above already exercises for
    model_performance_action, but plan_diff_action additionally receives weekly_decisions."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-07-18T12:00:00Z"}), encoding="utf-8"
        )
        self.plan_diff_calls = []

        def team_view_action(team_id):
            return {
                "manager": {"connection_status": "connected", "team_id": team_id, "squad": []},
                "weekly_decisions": {"status": "active", "event": 3, "profiles": []},
            }

        def plan_diff_action(team_id, weekly_decisions):
            self.plan_diff_calls.append((team_id, weekly_decisions.get("event")))
            return {"event": weekly_decisions.get("event"), "profiles": [{"profile_id": "balanced", "action_changed": True}]}

        self.plan_diff_action = plan_diff_action
        self.server = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
            team_view_action=team_view_action,
            plan_diff_action=plan_diff_action,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def test_splices_plan_diff_onto_weekly_decisions(self):
        html = urlopen(self.base_url + "/?team_id=111", timeout=3).read().decode()

        self.assertEqual(self.plan_diff_calls, [(111, 3)])
        self.assertIn('"action_changed": true', html)

    def test_absent_team_id_serves_the_dashboard_without_computing_plan_diff(self):
        html = urlopen(self.base_url + "/dashboard.html", timeout=3).read().decode()

        self.assertEqual(self.plan_diff_calls, [])
        # Not a bare `assertNotIn("action_changed", html)` -- that field name also appears as a
        # plain JS identifier in the page's own inlined decision-center.js bundle regardless of
        # whether any data was ever computed. The specific *serialized value* this fake action
        # returns (matching the positive test above) is what actually distinguishes "computed and
        # embedded" from "never called."
        self.assertNotIn('"action_changed": true', html)

    def test_plan_diff_failure_is_reported_cleanly_instead_of_a_server_error(self):
        failing_server = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
            team_view_action=lambda team_id: {
                "manager": {"connection_status": "connected", "team_id": team_id, "squad": []},
                "weekly_decisions": {"status": "active", "event": 3, "profiles": []},
            },
            plan_diff_action=lambda team_id, weekly_decisions: (_ for _ in ()).throw(RuntimeError("store corrupt")),
        )
        thread = threading.Thread(target=failing_server.serve_forever, daemon=True)
        thread.start()
        try:
            html = urlopen(
                f"http://127.0.0.1:{failing_server.server_port}/?team_id=364759", timeout=3
            ).read().decode()

            self.assertIn('"status": "error"', html)
            self.assertNotIn("store corrupt", html)
        finally:
            failing_server.shutdown()
            failing_server.server_close()
            thread.join(timeout=2)


class CookieResolvedTeamTests(unittest.TestCase):
    """Issue #45: a saved-team cookie is a second source for the per-request team view."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-07-18T12:00:00Z"}), encoding="utf-8"
        )
        self.lookup_calls = []
        self.profile_read_calls = []

        def team_view_action(team_id):
            self.lookup_calls.append(team_id)
            return {
                "manager": {"connection_status": "connected", "team_id": team_id, "team_name": "Cookie Team", "squad": []},
                "weekly_decisions": {"status": "waiting_for_gw2", "event": 1},
            }

        def profile_read_action(team_id):
            self.profile_read_calls.append(team_id)
            return {
                "team_id": team_id, "timezone": "UTC", "confirmed_free_transfers": None,
                "confirmed_free_transfers_event": None, "risk_profile": "aggressive",
            }

        self.server = create_server(
            self.root, host="127.0.0.1", port=0, token="test-token",
            team_view_action=team_view_action, profile_read_action=profile_read_action,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.db_path = self.root / "data" / "profiles.db"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def _get_with_cookie(self, path, cookie_value):
        request = Request(self.base_url + path, headers={"Cookie": f"fpl_team_id={cookie_value}"})
        return urlopen(request, timeout=3)

    def test_opted_out_flag_does_not_affect_the_cookie_driven_own_team_path(self):
        """Issue #62: opting out only gates the explicit ?team_id= lookup of someone else's
        team, never a visitor's own remembered team resolved from their cookie."""
        set_lookup_opt_out(
            self.db_path, team_id=42, opted_out=True, pin_hash="some-hash",
            now="2026-08-08T00:00:00Z",
        )

        html = self._get_with_cookie("/", "42").read().decode()

        self.assertEqual(self.lookup_calls, [42])
        self.assertIn("Cookie Team", html)
        self.assertNotIn('"lookup":', html.replace(" ", ""))

    def test_cookie_resolves_the_team_when_no_query_param_is_present(self):
        html = self._get_with_cookie("/", "42").read().decode()

        self.assertEqual(self.lookup_calls, [42])
        self.assertIn("Cookie Team", html)

    def test_cookie_resolved_team_is_not_flagged_as_a_one_off_lookup(self):
        """Unlike an explicit ?team_id= lookup, this is the visitor's own remembered team."""
        html = self._get_with_cookie("/", "42").read().decode()

        self.assertNotIn('"lookup":', html.replace(" ", ""))

    def test_query_param_takes_precedence_over_the_cookie(self):
        request = Request(
            self.base_url + "/?team_id=99", headers={"Cookie": "fpl_team_id=42"}
        )
        html = urlopen(request, timeout=3).read().decode()

        self.assertEqual(self.lookup_calls, [99])
        self.assertIn('"lookup":', html.replace(" ", ""))

    def test_malformed_cookie_falls_back_to_the_shared_dashboard_state(self):
        html = self._get_with_cookie("/", "not-a-number").read().decode()

        self.assertEqual(self.lookup_calls, [])
        self.assertIn('"generated_at": "2026-07-18T12:00:00Z"', html)

    def test_saved_profile_is_spliced_in_and_drives_the_default_risk_profile(self):
        html = self._get_with_cookie("/", "42").read().decode()

        self.assertEqual(self.profile_read_calls, [42])
        self.assertIn('"risk_profile": "aggressive"', html)


class RequestLevelDecisionCacheTests(unittest.TestCase):
    """Issue #208: end-to-end coverage of the real (non-test-double) `default_team_view_action`
    caching path -- unlike every other class in this file, these deliberately do NOT pass a
    `team_view_action` override into `create_server`, so the real `WeeklyDecisionCache` wiring
    from `server_handlers/team_lookup.py` is exercised.

    Exercises the returned `action(team_id)` closure directly rather than over HTTP for the
    same-endpoint-twice cases: `_serve_dashboard`'s own `?team_id=` path shares one
    `lookup_limiter` cooldown (`TEAM_LOOKUP_COOLDOWN_SECONDS`) with no token exemption (see
    `server.py`'s `_serve_dashboard`), so two back-to-back real HTTP lookups of the same team
    would 429 on the second one regardless of caching -- calling the closure directly is both
    faster and exactly what production does, since `_serve_dashboard` and `/api/manager-view`
    both call this identical closure object.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "dashboard-state.json").write_text(
            json.dumps({"generated_at": "2026-07-18T12:00:00Z", "profile": {"team_id": None}}),
            encoding="utf-8",
        )
        (self.root / "data" / "fpl-bootstrap-latest.json").write_text(
            json.dumps(sample_bootstrap()), encoding="utf-8"
        )
        (self.root / "data" / "fpl-fixtures-latest.json").write_text(
            json.dumps(sample_fixtures()), encoding="utf-8"
        )
        (self.root / "data" / "official-transfers-latest.json").write_text(
            json.dumps({"transfers": []}), encoding="utf-8"
        )

    def tearDown(self):
        self.directory.cleanup()

    def _raw_manager(self, bank=0):
        return {
            "entry": {
                "id": 364759, "name": "BrunoMans", "player_first_name": "Test",
                "player_last_name": "Manager", "current_event": 3, "started_event": 1,
            },
            "history": {"current": [], "past": [], "chips": []},
            "transfers": [],
            "picks": {
                "active_chip": None,
                "entry_history": {"event": 3, "bank": bank, "value": 1000},
                "picks": [],
            },
        }

    def test_a_second_lookup_of_the_same_team_reuses_the_cached_result(self):
        action = _default_team_view_action(self.root)

        with patch("fpl_intel.refresh.collect_public_manager", return_value=self._raw_manager()) as mock_collect, \
                patch("fpl_intel.refresh.build_transfer_decisions", return_value={"status": "active", "event": 3}) as mock_build:
            first = action(364759)
            second = action(364759)

        # collect_public_manager still runs on every call -- a manager's own live squad/profile
        # changes must always be reflected -- only the expensive computation itself is skipped.
        self.assertEqual(mock_collect.call_count, 2)
        self.assertEqual(mock_build.call_count, 1)
        self.assertEqual(first["weekly_decisions"], second["weekly_decisions"])

    def test_both_call_sites_share_one_cache(self):
        """`_serve_dashboard`'s ?team_id= resolution and /api/manager-view both call the same
        `lookup_action`/`action` closure `create_server` builds once -- verified here over real
        HTTP, using /api/manager-view's operator-token rate-limit exemption (server.py's
        `_rate_limit_exempt`) to avoid the two calls tripping the shared `lookup_limiter`
        cooldown, which is a separate, unrelated concern from the cache under test."""
        server = create_server(self.root, host="127.0.0.1", port=0, token="test-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            with patch("fpl_intel.refresh.collect_public_manager", return_value=self._raw_manager()), \
                    patch("fpl_intel.refresh.build_transfer_decisions", return_value={"status": "active", "event": 3}) as mock_build:
                urlopen(base_url + "/?team_id=364759", timeout=3).read()
                manager_view_request = Request(
                    base_url + "/api/manager-view?team_id=364759", headers={"X-Refresh-Token": "test-token"},
                )
                urlopen(manager_view_request, timeout=3).read()

            self.assertEqual(mock_build.call_count, 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_a_changed_squad_forces_recomputation(self):
        action = _default_team_view_action(self.root)

        with patch("fpl_intel.refresh.build_transfer_decisions", return_value={"status": "active", "event": 3}) as mock_build:
            with patch("fpl_intel.refresh.collect_public_manager", return_value=self._raw_manager(bank=0)):
                action(364759)
            with patch("fpl_intel.refresh.collect_public_manager", return_value=self._raw_manager(bank=50)):
                action(364759)

        self.assertEqual(mock_build.call_count, 2)

    def test_a_real_refresh_invalidates_the_cache(self):
        action = _default_team_view_action(self.root)

        with patch("fpl_intel.refresh.collect_public_manager", return_value=self._raw_manager()), \
                patch("fpl_intel.refresh.build_transfer_decisions", return_value={"status": "active", "event": 3}) as mock_build:
            action(364759)
            publish_generation(
                self.root, "2026-07-19T12:00:00Z",
                {"dashboard-state.json": {"generated_at": "2026-07-19T12:00:00Z"}},
            )
            action(364759)

        self.assertEqual(mock_build.call_count, 2)

    def test_a_different_team_id_never_collides_with_an_unrelated_cached_entry(self):
        action = _default_team_view_action(self.root)

        with patch("fpl_intel.refresh.collect_public_manager", return_value=self._raw_manager()), \
                patch("fpl_intel.refresh.build_transfer_decisions", return_value={"status": "active", "event": 3}) as mock_build:
            action(364759)
            action(100001)

        self.assertEqual(mock_build.call_count, 2)
