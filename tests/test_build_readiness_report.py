from __future__ import annotations

import unittest

from scripts.build_readiness_report import build_readiness_report, to_markdown


def batch(
    *,
    inputs: int,
    completed: int,
    skipped: int = 0,
    passed: int | None = None,
    ai_tokens: int = 0,
    stable_fp: int = 0,
    attention_fp: int = 0,
    failed_info: int = 0,
    recommendations: int = 0,
) -> dict:
    return {
        "input_count": inputs,
        "completed_count": completed,
        "skipped_unchanged_count": skipped,
        "quality_passed_count": inputs if passed is None else passed,
        "average_quality_score": 100.0,
        "failed_info_check_total": failed_info,
        "recommendation_total": recommendations,
        "table_false_positive_attention_total": attention_fp,
        "stable_table_false_positive_total": stable_fp,
        "agent_review_estimated_total_tokens_total": ai_tokens,
    }


class BuildReadinessReportTests(unittest.TestCase):
    def test_builds_ready_report_when_all_gates_pass(self) -> None:
        report = build_readiness_report(
            public_portal_force=batch(inputs=83, completed=83, stable_fp=130),
            public_portal_reuse=batch(inputs=83, completed=0, skipped=83),
            integrated_force=batch(inputs=1, completed=1, stable_fp=9),
            integrated_reuse=batch(inputs=1, completed=0, skipped=1),
            cost_estimate={"budget_exceeded": False, "estimated_total_cost": "0"},
            snapshot_comparison={
                "before": {"row_count": 83, "file_sha256_coverage_count": 83},
                "after": {"row_count": 83, "file_sha256_coverage_count": 0},
                "counts": {"added": 0, "removed": 0, "metadata_changed": 0, "file_hash_changed": 0},
            },
        )

        self.assertTrue(report["passed"])
        self.assertEqual(report["status"], "data_pipeline_ready_provider_not_wired")
        self.assertEqual(report["summary"]["public_portal_inputs"], 83)
        self.assertEqual(report["summary"]["provider_execution_path"], "not_implemented")
        self.assertFalse(report["summary"]["public_portal_live_content_hash_checked"])
        self.assertEqual(report["summary"]["semantic_embedding_provider_readiness"], "not_requested")
        self.assertIn("prompt token envelope", to_markdown(report))

    def test_records_embedding_estimates_without_requiring_provider_approval(self) -> None:
        report = build_readiness_report(
            public_portal_force=batch(inputs=83, completed=83),
            public_portal_reuse=batch(inputs=83, completed=0, skipped=83),
            integrated_force=batch(inputs=1, completed=1),
            integrated_reuse=batch(inputs=1, completed=0, skipped=1),
            cost_estimate={"budget_exceeded": False, "estimated_total_cost": "0"},
            snapshot_comparison={
                "after": {"row_count": 83, "file_sha256_coverage_count": 83},
                "counts": {"added": 0, "removed": 0, "metadata_changed": 0, "file_hash_changed": 0},
            },
            embedding_cost_estimates=[
                {
                    "report_type": "embedding_cost_estimate",
                    "record_count": 10,
                    "estimated_input_tokens": 1234,
                    "provider_model": "future-semantic-embedding",
                    "budget_evaluation_status": "token_only",
                    "api_call_count": 0,
                    "mode": "estimate_only",
                }
            ],
        )

        self.assertTrue(report["passed"])
        self.assertEqual(report["summary"]["embedding_estimated_input_tokens"], 1234)
        self.assertEqual(report["summary"]["semantic_embedding_provider_readiness"], "estimate_only_local_validation")

    def test_marks_needs_attention_when_semantic_embedding_approval_is_required_without_budget(self) -> None:
        report = build_readiness_report(
            public_portal_force=batch(inputs=83, completed=83),
            public_portal_reuse=batch(inputs=83, completed=0, skipped=83),
            integrated_force=batch(inputs=1, completed=1),
            integrated_reuse=batch(inputs=1, completed=0, skipped=1),
            cost_estimate={"budget_exceeded": False, "estimated_total_cost": "0"},
            snapshot_comparison={
                "after": {"row_count": 83, "file_sha256_coverage_count": 83},
                "counts": {"added": 0, "removed": 0, "metadata_changed": 0, "file_hash_changed": 0},
            },
            embedding_cost_estimates=[
                {
                    "report_type": "embedding_cost_estimate",
                    "record_count": 10,
                    "estimated_input_tokens": 1234,
                    "provider_model": "future-semantic-embedding",
                    "price_per_1m_tokens": None,
                    "budget": None,
                    "budget_evaluation_status": "token_only",
                    "budget_exceeded": False,
                    "api_call_count": 0,
                    "mode": "estimate_only",
                }
            ],
            require_semantic_embedding_approval=True,
        )

        self.assertFalse(report["passed"])
        self.assertEqual(report["summary"]["semantic_embedding_provider_readiness"], "needs_attention")
        self.assertTrue(
            any(
                item["name"] == "semantic_embedding_approval_present_when_required" and not item["passed"]
                for item in report["checks"]
            )
        )
        self.assertIn("semantic_embedding_missing_price", {item["reason"] for item in report["failures"]["embedding_readiness"]})

    def test_marks_needs_attention_when_live_snapshot_drifts(self) -> None:
        report = build_readiness_report(
            public_portal_force=batch(inputs=83, completed=83),
            public_portal_reuse=batch(inputs=83, completed=0, skipped=83),
            integrated_force=batch(inputs=1, completed=1),
            integrated_reuse=batch(inputs=1, completed=0, skipped=1),
            cost_estimate={"budget_exceeded": False, "estimated_total_cost": "0"},
            snapshot_comparison={
                "after": {"row_count": 84, "file_sha256_coverage_count": 84},
                "counts": {"added": 1, "removed": 0, "metadata_changed": 0, "file_hash_changed": 0},
            },
        )

        self.assertFalse(report["passed"])
        self.assertEqual(report["status"], "needs_attention")
        self.assertTrue(any(item["name"] == "public_portal_live_no_drift" and not item["passed"] for item in report["checks"]))

    def test_marks_needs_attention_when_integrated_report_has_info_failures_or_recommendations(self) -> None:
        report = build_readiness_report(
            public_portal_force=batch(inputs=83, completed=83),
            public_portal_reuse=batch(inputs=83, completed=0, skipped=83),
            integrated_force=batch(inputs=1, completed=1, failed_info=5, recommendations=3),
            integrated_reuse=batch(inputs=1, completed=0, skipped=1),
            cost_estimate={"budget_exceeded": False, "estimated_total_cost": "0"},
            snapshot_comparison={
                "after": {"row_count": 83, "file_sha256_coverage_count": 83},
                "counts": {"added": 0, "removed": 0, "metadata_changed": 0, "file_hash_changed": 0},
            },
        )

        self.assertFalse(report["passed"])
        self.assertEqual(report["summary"]["integrated_failed_info_checks"], 5)
        self.assertEqual(report["summary"]["integrated_recommendations"], 3)
        self.assertTrue(any(item["name"] == "integrated_no_failed_info" and not item["passed"] for item in report["checks"]))
        self.assertTrue(any(item["name"] == "integrated_no_recommendations" and not item["passed"] for item in report["checks"]))

    def test_marks_needs_attention_when_cost_budget_is_unknown(self) -> None:
        report = build_readiness_report(
            public_portal_force=batch(inputs=83, completed=83),
            public_portal_reuse=batch(inputs=83, completed=0, skipped=83),
            integrated_force=batch(inputs=1, completed=1),
            integrated_reuse=batch(inputs=1, completed=0, skipped=1),
            cost_estimate={
                "budget_exceeded": None,
                "estimated_total_cost": None,
                "agent_review_estimated_total_tokens": 2000,
                "budget_evaluation_status": "unknown_price",
            },
            snapshot_comparison={
                "after": {"row_count": 83, "file_sha256_coverage_count": 83},
                "counts": {"added": 0, "removed": 0, "metadata_changed": 0, "file_hash_changed": 0},
            },
        )

        self.assertFalse(report["passed"])
        self.assertEqual(report["summary"]["cost_budget_evaluation_status"], "unknown_price")
        self.assertTrue(
            any(item["name"] == "cost_budget_known_and_not_exceeded" and not item["passed"] for item in report["checks"])
        )


if __name__ == "__main__":
    unittest.main()
