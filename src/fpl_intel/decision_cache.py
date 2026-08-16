"""Issue #208: in-process cache for `build_transfer_decisions`/`build_draft_decisions` results.

Revisits a question issue #176 explicitly left open ("can be revisited later"): every dashboard
page load with a resolved team, and every `/api/manager-view` call, recomputes that team's
weekly decision from scratch on the request thread -- 11.5-24s at the search-space #181 widened
to, byte-identical if nothing about the team or the shared data changed since the last time it
was computed. Reload the same team twice and pay the full cost twice.

The tempting simplest key is `(team_id, generation)` -- `generation` being whatever
`generation.current_generation_id` returns for the shared refresh's current data snapshot.
That alone isn't safe: `refresh.compute_manager_view` also live-fetches the manager's current
public squad (`collect_public_manager`, a few HTTP calls to the official FPL API) and reads the
visitor's own saved profile overrides (`confirmed_free_transfers`, `draft_squad`) on every call --
both can change *within* one generation window, which can span hours. A manager who transfers on
the official site, or edits a saved override here, must see that reflected on their very next
page load, not at the next shared refresh.

So the cache key also folds in a fingerprint of exactly the fields
`build_transfer_decisions`/`build_draft_decisions` actually read from the manager -- see
`_fingerprint` below. `collect_public_manager` and the profile read still happen on every
request (cheap: a few HTTP round-trips and a local file read, not the combinatorial search this
issue is about); only the expensive computation itself is skipped when the fingerprint, along
with the generation, matches what's already cached.

Storage is a plain in-process dict, one per server process (see `make_cached_weekly_decisions_builder`
-- it's built once per `default_team_view_action(root)` call, i.e. once per `create_server`, so
each server/test instance gets its own cache and nothing leaks across process restarts or
between tests). This matches the repo's stdlib-only policy and this issue's own framing: a cold
cache after a Railway restart pays exactly today's existing per-request cost once, no worse than
before. Persisting across restarts (e.g. alongside `profiles.db`) was the issue's own open
question and is left for later -- no evidence yet that Railway restarts are frequent enough, or
this cold-start cost high enough, to justify the added complexity.

Considered and declined: locking per-cache-key to prevent two concurrent requests for the same
team_id racing into a simultaneous double-compute on a cache miss (a "thundering herd"/
single-flight pattern). This can only happen the first time a team is looked up after a
generation change, from more than one connection at once (e.g. two open tabs) -- a narrow,
self-limiting case that, worst case, costs exactly what every request already costs today. Adding
per-key locks to close it would serialize otherwise-independent teams' cache-miss computations
against each other for no correctness benefit, so it's left alone.
"""

import hashlib
import json
import threading

from .generation import current_generation_id
from .refresh import default_build_weekly_decisions

# Issue #208: the exact manager/profile fields build_transfer_decisions/build_draft_decisions
# actually read (see transfer_decisions.py:864-1067) -- deliberately excludes display-only fields
# like overall_points/overall_rank/team_name/manager_name, which update live throughout a
# gameweek (FPL recalculates rank continuously) without ever changing the computed recommendation.
# Including them would invalidate the cache constantly, right when live traffic peaks -- exactly
# the case this cache exists to help with.
_RELEVANT_MANAGER_FIELDS = (
    "connection_status",
    "squad_publicly_available",
    "squad",
    "bank",
    "public_transfers",
    "chips_used",
    "active_chip",
)


def _fingerprint(manager_state, confirmed_free_transfers, confirmed_free_transfers_event, draft_squad_ids):
    """A stable hash of every input that can change the computed weekly decision for one team,
    independent of the shared refresh generation. Two calls with the same fingerprint are
    guaranteed to produce the same output as `default_build_weekly_decisions` would."""
    relevant = {field: manager_state.get(field) for field in _RELEVANT_MANAGER_FIELDS}
    relevant["confirmed_free_transfers"] = confirmed_free_transfers
    relevant["confirmed_free_transfers_event"] = confirmed_free_transfers_event
    relevant["draft_squad_ids"] = draft_squad_ids
    blob = json.dumps(relevant, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class WeeklyDecisionCache:
    """Memoizes one `(generation, fingerprint) -> weekly_decisions` result per team_id.

    Swaps its entire entry dict out whenever `generation` changes, rather than evicting
    individual stale entries -- simpler, and bounded by "how many distinct teams were looked up
    this generation", which already matches this app's real traffic scale.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._generation = None
        self._entries = {}  # team_id -> (fingerprint, weekly_decisions)

    def build(
        self, generation, team_id, manager_state, confirmed_free_transfers,
        confirmed_free_transfers_event, draft_squad_ids, compute,
    ):
        """Return the cached `weekly_decisions` for this team/generation/fingerprint if present,
        else call `compute()` (expected to be `default_build_weekly_decisions` bound to this
        request's arguments) and cache its result. `compute` is always called outside the lock --
        it can take upwards of ten seconds, and holding the lock across that would serialize every
        other team's cache lookups behind it, defeating the point of a threaded server.
        """
        fingerprint = _fingerprint(
            manager_state, confirmed_free_transfers, confirmed_free_transfers_event, draft_squad_ids,
        )
        with self._lock:
            if self._generation != generation:
                self._entries = {}
                self._generation = generation
            cached = self._entries.get(team_id)
            if cached is not None and cached[0] == fingerprint:
                return cached[1]

        weekly_decisions = compute()

        with self._lock:
            # A refresh (or another thread's cache-miss for a newer generation) may have landed
            # while `compute()` was running -- if so, don't seed the new generation's cache with a
            # result computed against the old one.
            if self._generation == generation:
                self._entries[team_id] = (fingerprint, weekly_decisions)
        return weekly_decisions


def make_cached_weekly_decisions_builder(root, cache):
    """Build a `build_weekly_decisions` callable for `refresh.compute_manager_view`'s caching
    seam, backed by `cache`. `root` is used only to read the current generation id at call time
    (never cached itself) -- see `generation.current_generation_id`.
    """

    def build(team_id, bootstrap, fixtures, manager_state, generated_at, horizon, transfers, draft_squad_ids):
        generation = current_generation_id(root)
        confirmed_free_transfers = manager_state.get("confirmed_free_transfers")
        confirmed_free_transfers_event = manager_state.get("confirmed_free_transfers_event")

        def compute():
            return default_build_weekly_decisions(
                team_id, bootstrap, fixtures, manager_state, generated_at, horizon, transfers,
                draft_squad_ids,
            )

        return cache.build(
            generation, team_id, manager_state, confirmed_free_transfers,
            confirmed_free_transfers_event, draft_squad_ids, compute,
        )

    return build
