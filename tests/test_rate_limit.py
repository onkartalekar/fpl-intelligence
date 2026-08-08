import unittest

from fpl_intel.rate_limit import CooldownLimiter


class CooldownLimiterTests(unittest.TestCase):
    def test_allows_the_first_hit_then_blocks_until_cooldown_elapses(self):
        clock = {"now": 0.0}
        limiter = CooldownLimiter(cooldown_seconds=10, clock=lambda: clock["now"])

        self.assertTrue(limiter.allow("1.2.3.4"))
        self.assertFalse(limiter.allow("1.2.3.4"))

        clock["now"] = 9.9
        self.assertFalse(limiter.allow("1.2.3.4"))

        clock["now"] = 10.0
        self.assertTrue(limiter.allow("1.2.3.4"))

    def test_tracks_each_source_independently(self):
        clock = {"now": 0.0}
        limiter = CooldownLimiter(cooldown_seconds=10, clock=lambda: clock["now"])

        self.assertTrue(limiter.allow("1.2.3.4"))
        self.assertFalse(limiter.allow("1.2.3.4"))
        self.assertTrue(limiter.allow("5.6.7.8"))

    def test_caps_memory_footprint_instead_of_growing_unboundedly(self):
        clock = {"now": 0.0}
        limiter = CooldownLimiter(cooldown_seconds=10, max_tracked=3, clock=lambda: clock["now"])

        for source in range(3):
            self.assertTrue(limiter.allow(f"source-{source}"))
        self.assertEqual(len(limiter._next_allowed_at), 3)

        # A brand-new source past the cap clears the table rather than growing it further.
        self.assertTrue(limiter.allow("source-overflow"))
        self.assertEqual(len(limiter._next_allowed_at), 1)


if __name__ == "__main__":
    unittest.main()
