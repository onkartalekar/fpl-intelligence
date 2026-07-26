import json
import os
import unittest

from fpl_intel.news_signals import (
    bounded_minutes_adjustment,
    extract_availability_signals,
    fetch_news_item,
)


def _fixed_response(text):
    """A stand-in for the real Claude API call -- returns recorded text
    instead of making a live request, per the no-live-API-in-tests rule."""
    def caller(news_text, api_key):
        return text
    return caller


def _failing_caller(news_text, api_key):
    return None


class ExtractAvailabilitySignalsTests(unittest.TestCase):
    def test_returns_empty_list_without_an_api_key(self):
        news_item = {"source_url": "https://example.com/news", "text": "Player X is injured."}
        signals = extract_availability_signals(news_item, api_key=None, caller=_fixed_response("[]"))
        self.assertEqual(signals, [])

    def test_returns_empty_list_for_empty_news_text(self):
        news_item = {"source_url": "https://example.com/news", "text": ""}
        signals = extract_availability_signals(news_item, api_key="fake-key", caller=_fixed_response("[]"))
        self.assertEqual(signals, [])

    def test_parses_a_recorded_well_formed_response(self):
        recorded_response = json.dumps([
            {
                "player": "Erling Haaland",
                "availability_signal": "doubtful",
                "expected_return": "assessed after the international break",
                "role_hint": None,
                "confidence": "medium",
                "exact_quote": "Haaland was withdrawn late in training and will be assessed.",
            }
        ])
        news_item = {"source_url": "https://example.com/club-news/haaland", "text": "some article text"}
        signals = extract_availability_signals(news_item, api_key="fake-key", caller=_fixed_response(recorded_response))

        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertEqual(signal["player"], "Erling Haaland")
        self.assertEqual(signal["availability_signal"], "doubtful")
        self.assertEqual(signal["source_url"], "https://example.com/club-news/haaland")
        self.assertIn("assessed", signal["exact_quote"])

    def test_empty_array_response_yields_no_signals(self):
        news_item = {"source_url": "https://example.com/news", "text": "Routine matchday preview, no injury news."}
        signals = extract_availability_signals(news_item, api_key="fake-key", caller=_fixed_response("[]"))
        self.assertEqual(signals, [])

    def test_malformed_json_response_fails_safe_to_empty_list(self):
        news_item = {"source_url": "https://example.com/news", "text": "some text"}
        signals = extract_availability_signals(news_item, api_key="fake-key", caller=_fixed_response("not valid json"))
        self.assertEqual(signals, [])

    def test_non_list_response_fails_safe_to_empty_list(self):
        news_item = {"source_url": "https://example.com/news", "text": "some text"}
        response = json.dumps({"player": "not a list"})
        signals = extract_availability_signals(news_item, api_key="fake-key", caller=_fixed_response(response))
        self.assertEqual(signals, [])

    def test_item_missing_required_fields_is_skipped_not_fabricated(self):
        response = json.dumps([
            {"player": "Some Player"},  # missing exact_quote -- must be dropped, not defaulted
            {"exact_quote": "quote with no player"},  # missing player
            {
                "player": "Valid Player", "availability_signal": "injured",
                "expected_return": None, "role_hint": None, "confidence": "high",
                "exact_quote": "Valid Player will miss the next fixture.",
            },
        ])
        news_item = {"source_url": "https://example.com/news", "text": "some text"}
        signals = extract_availability_signals(news_item, api_key="fake-key", caller=_fixed_response(response))
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["player"], "Valid Player")

    def test_api_failure_fails_safe_to_empty_list(self):
        news_item = {"source_url": "https://example.com/news", "text": "some text"}
        signals = extract_availability_signals(news_item, api_key="fake-key", caller=_failing_caller)
        self.assertEqual(signals, [])

    def test_unrecognized_provider_fails_safe_to_empty_list(self):
        news_item = {"source_url": "https://example.com/news", "text": "some text"}
        signals = extract_availability_signals(news_item, api_key="fake-key", provider="not_a_real_provider")
        self.assertEqual(signals, [])

    def test_openai_compatible_provider_uses_its_own_caller(self):
        # No explicit caller -- proves provider selection, not the caller
        # override, is what resolves the right backend.
        import fpl_intel.news_signals as module

        recorded_response = json.dumps([
            {
                "player": "Some Player",
                "availability_signal": "injured",
                "expected_return": None,
                "role_hint": None,
                "confidence": "high",
                "exact_quote": "Some Player will miss several weeks.",
            }
        ])
        original_caller = module._PROVIDERS["openai_compatible"]["caller"]
        module._PROVIDERS["openai_compatible"]["caller"] = _fixed_response(recorded_response)
        try:
            news_item = {"source_url": "https://example.com/news", "text": "some text"}
            signals = extract_availability_signals(news_item, api_key="fake-key", provider="openai_compatible")
        finally:
            module._PROVIDERS["openai_compatible"]["caller"] = original_caller

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["player"], "Some Player")

    def test_provider_env_var_selects_provider_when_none_passed_explicitly(self):
        import fpl_intel.news_signals as module

        original_caller = module._PROVIDERS["openai_compatible"]["caller"]
        module._PROVIDERS["openai_compatible"]["caller"] = _fixed_response("[]")
        original_env = os.environ.get("FPL_INTEL_LLM_PROVIDER")
        os.environ["FPL_INTEL_LLM_PROVIDER"] = "openai_compatible"
        try:
            news_item = {"source_url": "https://example.com/news", "text": "some text"}
            signals = extract_availability_signals(news_item, api_key="fake-key")
        finally:
            module._PROVIDERS["openai_compatible"]["caller"] = original_caller
            if original_env is None:
                os.environ.pop("FPL_INTEL_LLM_PROVIDER", None)
            else:
                os.environ["FPL_INTEL_LLM_PROVIDER"] = original_env
        self.assertEqual(signals, [])


class OpenAICompatibleCallerTests(unittest.TestCase):
    def test_returns_none_when_api_base_or_model_env_vars_are_unset(self):
        import fpl_intel.news_signals as module

        for var in ("FPL_INTEL_LLM_API_BASE", "FPL_INTEL_LLM_MODEL"):
            os.environ.pop(var, None)
        self.assertIsNone(module._call_openai_compatible("some text", "fake-key"))


class BoundedMinutesAdjustmentTests(unittest.TestCase):
    def test_injured_signal_reduces_minutes_but_stays_bounded(self):
        adjusted = bounded_minutes_adjustment(80.0, "injured", max_adjustment_fraction=0.25)
        self.assertLess(adjusted, 80.0)
        self.assertGreaterEqual(adjusted, 80.0 * 0.75 - 1e-9)  # never more than 25% down

    def test_returning_signal_increases_minutes_but_stays_bounded(self):
        adjusted = bounded_minutes_adjustment(40.0, "returning", max_adjustment_fraction=0.25)
        self.assertGreater(adjusted, 40.0)
        self.assertLessEqual(adjusted, 40.0 * 1.25 + 1e-9)

    def test_unrecognized_signal_leaves_estimate_unchanged(self):
        self.assertEqual(bounded_minutes_adjustment(60.0, "unknown_signal_type"), 60.0)

    def test_zero_expected_minutes_stays_zero(self):
        self.assertEqual(bounded_minutes_adjustment(0.0, "returning"), 0.0)

    def test_adjustment_never_exceeds_ninety_minutes(self):
        adjusted = bounded_minutes_adjustment(85.0, "nailed_on", max_adjustment_fraction=0.5)
        self.assertLessEqual(adjusted, 90.0)

    def test_adjustment_never_goes_negative(self):
        adjusted = bounded_minutes_adjustment(10.0, "injured", max_adjustment_fraction=2.0)
        self.assertGreaterEqual(adjusted, 0.0)


class FetchNewsItemTests(unittest.TestCase):
    def test_strips_tags_and_scripts_to_plain_text(self):
        # This exercises the parsing logic directly against a fixed HTML
        # string rather than making a live request.
        import fpl_intel.news_signals as module

        html = b"<html><head><style>.x{color:red}</style></head><body><script>evil()</script><p>Player is fit.</p></body></html>"

        class _FakeResponse:
            def __enter__(self_inner):
                return self_inner
            def __exit__(self_inner, *args):
                return False
            def read(self_inner):
                return html

        original_urlopen = module.urlopen
        module.urlopen = lambda request, timeout=30: _FakeResponse()
        try:
            result = fetch_news_item("https://www.premierleague.com/news")
        finally:
            module.urlopen = original_urlopen

        self.assertEqual(result["source_url"], "https://www.premierleague.com/news")
        self.assertIn("Player is fit.", result["text"])
        self.assertNotIn("evil()", result["text"])
        self.assertNotIn("color:red", result["text"])

    def test_rejects_non_first_party_or_local_urls_before_network_access(self):
        import fpl_intel.news_signals as module

        original_urlopen = module.urlopen
        module.urlopen = lambda *args, **kwargs: self.fail("network must not be reached")
        try:
            with self.assertRaises(ValueError):
                fetch_news_item("http://127.0.0.1:8080/private")
            with self.assertRaises(ValueError):
                fetch_news_item("https://attacker.example/news")
        finally:
            module.urlopen = original_urlopen


if __name__ == "__main__":
    unittest.main()
