import json
from pathlib import Path
import tempfile
import unittest

from fpl_intel.coefficients import _DEFAULTS, load_coefficients


class LoadCoefficientsTests(unittest.TestCase):
    def test_falls_back_to_defaults_when_file_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            missing_path = Path(directory) / "does-not-exist.json"
            coefficients = load_coefficients(missing_path)
            self.assertEqual(coefficients["reliability_denominator"], _DEFAULTS["reliability_denominator"])
            self.assertEqual(coefficients["uncertainty_bands"], _DEFAULTS["uncertainty_bands"])

    def test_partial_file_merges_over_defaults_without_dropping_other_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coefficients.json"
            path.write_text(json.dumps({"reliability_denominator": 1234.0}), encoding="utf-8")
            coefficients = load_coefficients(path)
            self.assertEqual(coefficients["reliability_denominator"], 1234.0)
            # Untouched keys keep their default values rather than disappearing.
            self.assertEqual(coefficients["ep_next_blend_weight"], _DEFAULTS["ep_next_blend_weight"])
            self.assertEqual(coefficients["uncertainty_bands"], _DEFAULTS["uncertainty_bands"])

    def test_rejects_out_of_range_coefficients(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coefficients.json"
            path.write_text(json.dumps({"ep_next_blend_weight": 1.5}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "ep_next_blend_weight"):
                load_coefficients(path)

    def test_difficulty_table_keys_are_coerced_to_int(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coefficients.json"
            path.write_text(
                json.dumps({"clean_sheet_probability_by_difficulty": {"1": 0.5}}), encoding="utf-8"
            )
            coefficients = load_coefficients(path)
            self.assertIn(1, coefficients["clean_sheet_probability_by_difficulty"])
            self.assertEqual(coefficients["clean_sheet_probability_by_difficulty"][1], 0.5)
            # Other difficulty bands still present from defaults.
            self.assertIn(5, coefficients["clean_sheet_probability_by_difficulty"])

    def test_corrupt_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coefficients.json"
            path.write_text("{not valid json", encoding="utf-8")
            coefficients = load_coefficients(path)
            self.assertEqual(coefficients["reliability_denominator"], _DEFAULTS["reliability_denominator"])


if __name__ == "__main__":
    unittest.main()
