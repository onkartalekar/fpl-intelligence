from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fpl_intel.decision_cache import WeeklyDecisionCache, make_cached_weekly_decisions_builder
from fpl_intel.generation import publish_generation


def _manager_state(**overrides):
    state = {
        "team_id": 364759,
        "team_name": "BrunoMans",
        "manager_name": "Test Manager",
        "connection_status": "connected",
        "squad_publicly_available": True,
        "squad": [{"element_id": 1, "position": 1}],
        "bank": 5,
        "public_transfers": [],
        "chips_used": [],
        "active_chip": None,
        "overall_points": 100,
        "overall_rank": 500000,
    }
    state.update(overrides)
    return state


class WeeklyDecisionCacheTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def _compute(self, label):
        def compute():
            self.calls.append(label)
            return {"status": "active", "label": label}

        return compute

    def test_a_second_call_with_identical_inputs_is_served_from_cache(self):
        cache = WeeklyDecisionCache()
        manager = _manager_state()

        first = cache.build("gen-1", 364759, manager, None, None, None, self._compute("a"))
        second = cache.build("gen-1", 364759, manager, None, None, None, self._compute("b"))

        self.assertEqual(first, second)
        self.assertEqual(self.calls, ["a"])

    def test_a_new_generation_forces_recomputation(self):
        cache = WeeklyDecisionCache()
        manager = _manager_state()

        cache.build("gen-1", 364759, manager, None, None, None, self._compute("a"))
        cache.build("gen-2", 364759, manager, None, None, None, self._compute("b"))

        self.assertEqual(self.calls, ["a", "b"])

    def test_a_changed_squad_forces_recomputation_within_the_same_generation(self):
        """The core correctness guarantee: a manager who transfers on the official FPL site
        between two page loads must see that reflected immediately, not at the next refresh."""
        cache = WeeklyDecisionCache()
        original = _manager_state()
        transferred = _manager_state(squad=[{"element_id": 2, "position": 1}])

        cache.build("gen-1", 364759, original, None, None, None, self._compute("a"))
        cache.build("gen-1", 364759, transferred, None, None, None, self._compute("b"))

        self.assertEqual(self.calls, ["a", "b"])

    def test_a_changed_confirmed_free_transfers_override_forces_recomputation(self):
        cache = WeeklyDecisionCache()
        manager = _manager_state()

        cache.build("gen-1", 364759, manager, None, None, None, self._compute("a"))
        cache.build("gen-1", 364759, manager, 3, 2, None, self._compute("b"))

        self.assertEqual(self.calls, ["a", "b"])

    def test_a_changed_draft_squad_forces_recomputation(self):
        cache = WeeklyDecisionCache()
        manager = _manager_state()

        cache.build("gen-1", 364759, manager, None, None, [1, 2, 3], self._compute("a"))
        cache.build("gen-1", 364759, manager, None, None, [4, 5, 6], self._compute("b"))

        self.assertEqual(self.calls, ["a", "b"])

    def test_display_only_fields_do_not_invalidate_the_cache(self):
        """overall_points/overall_rank/team_name/manager_name update continuously through a live
        gameweek without ever changing what build_transfer_decisions computes -- including them
        in the fingerprint would defeat caching exactly when it matters most."""
        cache = WeeklyDecisionCache()
        original = _manager_state()
        rank_ticked = _manager_state(overall_points=101, overall_rank=499999, team_name="Renamed")

        cache.build("gen-1", 364759, original, None, None, None, self._compute("a"))
        cache.build("gen-1", 364759, rank_ticked, None, None, None, self._compute("b"))

        self.assertEqual(self.calls, ["a"])

    def test_different_teams_get_independent_cache_entries(self):
        cache = WeeklyDecisionCache()
        manager_a = _manager_state(team_id=1)
        manager_b = _manager_state(team_id=2)

        result_a = cache.build("gen-1", 1, manager_a, None, None, None, self._compute("a"))
        result_b = cache.build("gen-1", 2, manager_b, None, None, None, self._compute("b"))

        self.assertEqual(self.calls, ["a", "b"])
        self.assertNotEqual(result_a, result_b)

    def test_a_generation_change_evicts_every_teams_entries_not_just_one(self):
        cache = WeeklyDecisionCache()
        manager = _manager_state()

        cache.build("gen-1", 1, manager, None, None, None, self._compute("a1"))
        cache.build("gen-1", 2, manager, None, None, None, self._compute("b1"))
        cache.build("gen-2", 1, manager, None, None, None, self._compute("a2"))
        cache.build("gen-2", 2, manager, None, None, None, self._compute("b2"))

        self.assertEqual(self.calls, ["a1", "b1", "a2", "b2"])


class MakeCachedWeeklyDecisionsBuilderTests(unittest.TestCase):
    """Covers the seam that `server_handlers/team_lookup.py` actually wires up: a
    `build_weekly_decisions` callable matching `refresh.compute_manager_view`'s signature,
    reading the live generation id from `root` on every call rather than caching it."""

    def test_reuses_the_cached_result_when_nothing_relevant_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            publish_generation(root, "2026-08-16T12:00:00Z", {"dashboard-state.json": {}})

            cache = WeeklyDecisionCache()
            build = make_cached_weekly_decisions_builder(root, cache)
            calls = []

            def fake_default(team_id, bootstrap, fixtures, manager_state, generated_at, horizon, transfers, draft_squad_ids):
                calls.append(team_id)
                return {"status": "active", "event": 2}

            manager = _manager_state()
            with patch("fpl_intel.decision_cache.default_build_weekly_decisions", side_effect=fake_default):
                first = build(364759, {}, [], manager, "2026-08-16T12:00:01Z", 5, [], None)
                second = build(364759, {}, [], manager, "2026-08-16T12:00:02Z", 5, [], None)

            self.assertEqual(first, second)
            self.assertEqual(calls, [364759])

    def test_a_real_refresh_invalidates_the_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            publish_generation(root, "2026-08-16T12:00:00Z", {"dashboard-state.json": {}})

            cache = WeeklyDecisionCache()
            build = make_cached_weekly_decisions_builder(root, cache)
            calls = []

            def fake_default(team_id, bootstrap, fixtures, manager_state, generated_at, horizon, transfers, draft_squad_ids):
                calls.append(team_id)
                return {"status": "active", "computed_for": generated_at}

            manager = _manager_state()
            with patch("fpl_intel.decision_cache.default_build_weekly_decisions", side_effect=fake_default):
                build(364759, {}, [], manager, "2026-08-16T12:00:01Z", 5, [], None)
                publish_generation(root, "2026-08-16T13:00:00Z", {"dashboard-state.json": {}})
                build(364759, {}, [], manager, "2026-08-16T13:00:01Z", 5, [], None)

            self.assertEqual(calls, [364759, 364759])


if __name__ == "__main__":
    unittest.main()
