from __future__ import annotations

import unittest

from scripts.check_regression_expectations import check_expectations


class CheckRegressionExpectationsTests(unittest.TestCase):
    def test_passes_matching_expected_metrics_with_tolerance(self) -> None:
        batch = {
            "rows": [
                {
                    "source_system": "PUBLIC_PORTAL",
                    "source_record_id": "1",
                    "source_file_id": "10",
                    "quality_score": 100.0,
                    "warning_count": 0,
                    "chunk_to_source_char_ratio": 1.002,
                }
            ]
        }
        expectations = {
            "fixtures": [
                {
                    "identity": "PUBLIC_PORTAL:1:10",
                    "metrics": {
                        "quality_score": 100.0,
                        "warning_count": 0,
                        "chunk_to_source_char_ratio": 1.0,
                    },
                    "tolerances": {"chunk_to_source_char_ratio": 0.005},
                }
            ]
        }

        result = check_expectations(batch, expectations)

        self.assertTrue(result["passed"])
        self.assertEqual(result["checked_count"], 1)

    def test_reports_missing_and_metric_mismatch(self) -> None:
        batch = {
            "rows": [
                {
                    "source_system": "PUBLIC_PORTAL",
                    "source_record_id": "1",
                    "source_file_id": "10",
                    "quality_score": 98.0,
                }
            ]
        }
        expectations = {
            "fixtures": [
                {"identity": "PUBLIC_PORTAL:1:10", "metrics": {"quality_score": 100.0}},
                {"identity": "PUBLIC_PORTAL:2:20", "metrics": {"quality_score": 100.0}},
            ]
        }

        result = check_expectations(batch, expectations)

        self.assertFalse(result["passed"])
        self.assertEqual(result["failure_count"], 2)
        self.assertEqual({failure["reason"] for failure in result["failures"]}, {"metric_mismatch", "missing_fixture_row"})


if __name__ == "__main__":
    unittest.main()
