from __future__ import annotations

import json
import io
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from scripts.estimate_agent_review_cost import estimate_agent_review_cost, main, to_markdown


class EstimateAgentReviewCostTests(unittest.TestCase):
    def test_estimates_tokens_without_model_price(self) -> None:
        report = {
            "input_count": 2,
            "completed_count": 1,
            "skipped_unchanged_count": 1,
            "agent_review_selected_total": 3,
            "agent_review_estimated_input_tokens_total": 1200,
        }

        estimate = estimate_agent_review_cost(report)

        self.assertEqual(estimate["agent_review_selected_chunks"], 3)
        self.assertEqual(estimate["agent_review_estimated_input_tokens"], 1200)
        self.assertEqual(estimate["agent_review_estimated_output_tokens"], 0)
        self.assertEqual(estimate["agent_review_estimated_total_tokens"], 1200)
        self.assertIsNone(estimate["estimated_total_cost"])
        self.assertFalse(estimate["budget_exceeded"])

    def test_marks_budget_unknown_when_price_is_missing_for_billable_tokens(self) -> None:
        report = {
            "input_count": 1,
            "agent_review_estimated_input_tokens_total": 2000,
            "agent_review_estimated_total_tokens_total": 2000,
        }

        estimate = estimate_agent_review_cost(report, budget=Decimal("0.01"))

        self.assertIsNone(estimate["estimated_total_cost"])
        self.assertIsNone(estimate["budget_exceeded"])
        self.assertEqual(estimate["budget_evaluation_status"], "unknown_price")
        self.assertIn("- Budget evaluation status: unknown_price", to_markdown(estimate))

    def test_fail_over_budget_returns_failure_when_budget_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "batch.json"
            report_path.write_text(
                json.dumps(
                    {
                        "input_count": 1,
                        "agent_review_estimated_input_tokens_total": 2000,
                        "agent_review_estimated_total_tokens_total": 2000,
                    }
                ),
                encoding="utf-8",
            )

            with patch(
                "sys.argv",
                [
                    "estimate_agent_review_cost.py",
                    "--batch-report",
                    str(report_path),
                    "--budget",
                    "0.01",
                    "--fail-over-budget",
                ],
            ), patch("sys.stdout", new_callable=io.StringIO):
                exit_code = main()

        self.assertEqual(exit_code, 2)

    def test_allows_zero_token_budget_check_without_model_price(self) -> None:
        report = {
            "input_count": 1,
            "agent_review_estimated_input_tokens_total": 0,
            "agent_review_estimated_output_tokens_total": 0,
            "agent_review_estimated_total_tokens_total": 0,
        }

        estimate = estimate_agent_review_cost(report, budget=Decimal("0.01"))

        self.assertEqual(estimate["estimated_total_cost"], "0")
        self.assertFalse(estimate["budget_exceeded"])
        self.assertEqual(estimate["budget_evaluation_status"], "estimated")

    def test_estimates_cost_and_budget_from_operator_price(self) -> None:
        report = {
            "input_count": 1,
            "rows": [
                {"agent_review_selected_count": 2, "agent_review_estimated_input_tokens": 1_500_000},
            ],
        }

        estimate = estimate_agent_review_cost(
            report,
            input_price_per_1m_tokens=Decimal("2.00"),
            currency="USD",
            budget=Decimal("2.50"),
        )

        self.assertEqual(estimate["agent_review_selected_chunks"], 2)
        self.assertEqual(estimate["estimated_input_cost"], "3")
        self.assertTrue(estimate["budget_exceeded"])
        self.assertIn("- Budget exceeded: True", to_markdown(estimate))

    def test_estimates_input_and_output_costs(self) -> None:
        report = {
            "input_count": 1,
            "agent_review_selected_total": 2,
            "agent_review_estimated_input_tokens_total": 1_000_000,
            "agent_review_estimated_output_tokens_total": 500_000,
            "agent_review_estimated_total_tokens_total": 1_875_000,
        }

        estimate = estimate_agent_review_cost(
            report,
            input_price_per_1m_tokens=Decimal("1.00"),
            output_price_per_1m_tokens=Decimal("4.00"),
            budget=Decimal("4.00"),
        )

        self.assertEqual(estimate["estimated_input_cost"], "1")
        self.assertEqual(estimate["estimated_output_cost"], "2")
        self.assertEqual(estimate["estimated_total_cost"], "3")
        self.assertFalse(estimate["budget_exceeded"])

    def test_historical_reused_tokens_are_reported_but_not_charged(self) -> None:
        report = {
            "input_count": 1,
            "completed_count": 0,
            "skipped_unchanged_count": 1,
            "agent_review_selected_total": 0,
            "agent_review_estimated_input_tokens_total": 0,
            "agent_review_estimated_output_tokens_total": 0,
            "agent_review_estimated_total_tokens_total": 0,
            "historical_agent_review_selected_total": 3,
            "historical_agent_review_estimated_input_tokens_total": 1_200_000,
            "historical_agent_review_estimated_output_tokens_total": 300_000,
            "historical_agent_review_estimated_total_tokens_total": 1_875_000,
        }

        estimate = estimate_agent_review_cost(
            report,
            input_price_per_1m_tokens=Decimal("2.00"),
            output_price_per_1m_tokens=Decimal("4.00"),
            budget=Decimal("1.00"),
        )

        self.assertEqual(estimate["agent_review_selected_chunks"], 0)
        self.assertEqual(estimate["agent_review_estimated_total_tokens"], 0)
        self.assertEqual(estimate["estimated_total_cost"], "0")
        self.assertFalse(estimate["budget_exceeded"])
        self.assertEqual(estimate["historical_agent_review_selected_chunks_on_reused_runs"], 3)
        self.assertEqual(estimate["historical_agent_review_estimated_total_tokens_on_reused_runs"], 1_875_000)
        self.assertIn("audit exposure only", to_markdown(estimate))


if __name__ == "__main__":
    unittest.main()
