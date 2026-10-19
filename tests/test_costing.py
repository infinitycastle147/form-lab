import os
import unittest
from unittest.mock import patch

from PIL import Image
from poc.core import calculate_cost, pricing_rates, run_pipeline, templates


class CostTests(unittest.TestCase):
    def test_paid_api_estimate_includes_thinking_and_cached_tokens(self):
        usage = {"api_calls": 1, "complete": True, "input_tokens": 1000,
                 "cached_input_tokens": 100, "tool_input_tokens": 0,
                 "output_tokens": 200, "thinking_tokens": 50}
        rates = {"GEMINI_INPUT_USD_PER_MILLION": "0.75",
                 "GEMINI_OUTPUT_USD_PER_MILLION": "3.75",
                 "GEMINI_CACHED_INPUT_USD_PER_MILLION": "0.075",
                 "LOCAL_COMPUTE_USD_PER_HOUR": "10"}
        with patch.dict(os.environ, rates):
            cost = calculate_cost("gemini", "gemini-3.8-flash", usage, 36000)
        self.assertAlmostEqual(cost["api_estimate_usd"], 0.00162)
        self.assertAlmostEqual(cost["compute_estimate_usd"], 0.1)
        self.assertAlmostEqual(cost["total_estimate_usd"], 0.10162)

    def test_local_method_has_zero_api_charge_and_unpriced_compute(self):
        usage = {"api_calls": 0, "complete": True, "input_tokens": 0,
                 "cached_input_tokens": 0, "tool_input_tokens": 0,
                 "output_tokens": 0, "thinking_tokens": 0}
        with patch.dict(os.environ, {"LOCAL_COMPUTE_USD_PER_HOUR": ""}):
            cost = calculate_cost("tesseract", "gemini-3.8-flash", usage, 1000)
        self.assertEqual(cost["api_estimate_usd"], 0)
        self.assertIsNone(cost["compute_estimate_usd"])
        self.assertIsNone(cost["total_estimate_usd"])

    def test_unknown_model_does_not_get_default_gemini_price(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(pricing_rates("unknown-model"))

    def test_empty_compose_rate_variables_keep_default_pricing(self):
        with patch.dict(os.environ, {"GEMINI_INPUT_USD_PER_MILLION": "",
                                    "GEMINI_OUTPUT_USD_PER_MILLION": ""}):
            self.assertIsNotNone(pricing_rates("gemini-3.8-flash"))

    def test_pipeline_uses_returned_gemini_tokens(self):
        metadata = {"input_tokens": 1000, "output_tokens": 200, "thinking_tokens": 50,
                    "cached_input_tokens": 0, "tool_input_tokens": 0, "total_tokens": 1250,
                    "model": "gemini-3.8-flash"}
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-only",
                                    "GEMINI_INPUT_USD_PER_MILLION": "0.75",
                                    "GEMINI_OUTPUT_USD_PER_MILLION": "3.75"}), \
             patch("poc.core.gemini_fields", return_value=([], metadata)):
            image = Image.new("RGB", (850, 1100))
            result = run_pipeline([image], templates()["intake"], "gemini", 1.0,
                                  aligned_pages=[(image, {"method": "test"})])
        self.assertEqual(result["usage"]["api_calls"], 1)
        self.assertEqual(result["usage"]["thinking_tokens"], 50)
        self.assertAlmostEqual(result["cost"]["api_estimate_usd"], 0.0016875)
        self.assertIsNotNone(result["cost"]["compute_estimate_usd"])


if __name__ == "__main__":
    unittest.main()
