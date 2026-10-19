import unittest

from poc.evaluate import evaluate


class EvaluationTests(unittest.TestCase):
    def test_missing_fields_count_toward_text_errors_and_choice_recall(self):
        truth = {"template": "intake", "pages": [{"number": 1, "fields": {
            "full_name": "Ada", "date": "Today", "reference": "R-1",
            "contact_method": "email", "priority": "urgent",
        }}]}
        result = {"pages": [{"number": 1, "fields": []}],
                  "elapsed_ms": 0, "cpu_ms": 0, "process_peak_ram_mb": 0}

        metrics = evaluate(result, truth)

        self.assertEqual(metrics["missed_fields"], 5)
        self.assertEqual(metrics["selection_false_negatives"], 2)
        self.assertEqual(metrics["selection_recall"], 0)
        self.assertEqual(metrics["selection_f1"], 0)
        self.assertEqual(metrics["cer_wer"]["handwriting"]["cer"], 1)
        self.assertEqual(metrics["cer_wer"]["printed"]["cer"], 1)

    def test_independent_checkbox_counts_as_selection(self):
        from unittest.mock import patch
        template = {"pages": [{"fields": [{"name": "consent", "kind": "checkbox"}]}]}
        truth = {"template": "checkbox_form", "pages": [{"number": 1, "fields": {"consent": "yes"}}]}
        result = {"pages": [{"number": 1, "fields": [{"name": "consent", "value": "no", "uncertain": False}]}],
                  "elapsed_ms": 0, "cpu_ms": 0, "process_peak_ram_mb": 0}
        with patch("poc.evaluate.templates", return_value={"checkbox_form": template}):
            metrics = evaluate(result, truth)
        self.assertEqual(metrics["selection_false_positives"], 1)
        self.assertEqual(metrics["selection_false_negatives"], 1)


if __name__ == "__main__":
    unittest.main()
