"""Minimal per-source cooldown for the unauthenticated team-lookup path (issue #46).

`collect_public_manager()` makes several sequential live calls to the official FPL API with no
built-in retry/backoff, and the dashboard server spawns one thread per request -- so an
unthrottled lookup endpoint is a concrete, already-verified availability risk (thread pileup, and
risk of the app's own IP getting rate-limited upstream), not a hypothetical deferred to #28. This
is intentionally simple: a capped in-memory dict keyed by source (the requester's IP), storing the
monotonic time each source is next allowed to look up again. It resets on process restart and is
not shared across worker processes -- a fuller version (e.g. persisted, distributed) belongs to
#28 once real hosting exists.
"""

import time


class CooldownLimiter:
    """Allow one action per `cooldown_seconds` per source key, with a capped memory footprint."""

    def __init__(self, cooldown_seconds=10, max_tracked=10_000, clock=time.monotonic):
        self._cooldown_seconds = cooldown_seconds
        self._max_tracked = max_tracked
        self._clock = clock
        self._next_allowed_at = {}

    def allow(self, key):
        """Return True and start a fresh cooldown for `key`, or return False if still cooling down."""
        now = self._clock()
        next_allowed_at = self._next_allowed_at.get(key)
        if next_allowed_at is not None and now < next_allowed_at:
            return False
        if key not in self._next_allowed_at and len(self._next_allowed_at) >= self._max_tracked:
            # Crude but bounded: drop the whole table rather than let it grow unboundedly under
            # a wide-source flood. Worst case, one flood briefly resets everyone's cooldown.
            self._next_allowed_at.clear()
        self._next_allowed_at[key] = now + self._cooldown_seconds
        return True
