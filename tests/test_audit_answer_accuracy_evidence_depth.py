import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts.audit_answer_accuracy_evidence_depth import (
    build_answer_accuracy_evidence_depth,
    main,
)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class AuditAnswerAccuracyEvidenceDepthTests(unittest.TestCase):
    def test_warns_when_passing_answer_evidence_is_too_thin_for_public_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            _write_json(
                product,
                {
                    "rag_eval_summary": {
                        "query_count": 3,
                        "expect_no_evidence_query_count": 0,
                        "relation_supported_ratio": 0.0,
                    },
                    "mcp_demo_answer_summary": {
                        "passed": True,
                        "query_count": 3,
                        "answerable_query_count": 3,
                        "expect_no_evidence_query_count": 0,
                        "smoke_citation_count": 0,
                        "missing_supporting_result_count": 0,
                        "quality_issue_count": 0,
                    },
                    "accuracy_comparison_summary": {
                        "passed": True,
                        "query_count": 3,
                        "mcp_regression_count": 0,
                    },
                },
            )

            report = build_answer_accuracy_evidence_depth(
                product_readiness_report=product,
                min_public_query_count=20,
                min_no_evidence_controls=3,
                min_relation_supported_ratio=0.5,
            )

        self.assertFalse(report["passed_for_public_release_depth"])
        self.assertEqual("usable_with_disclosure", report["pilot_evidence_status"])
        self.assertEqual(0, report["blocker_count"])
        self.assertEqual(3, report["warning_count"])
        self.assertEqual(
            [
                "answer-evidence-query-count-thin",
                "answer-evidence-no-evidence-controls-thin",
                "answer-evidence-relation-support-thin",
            ],
            [finding["code"] for finding in report["findings"]],
        )

    def test_blocks_smoke_citations_and_regressions(self) -> None:
        report = build_answer_accuracy_evidence_depth(
            min_public_query_count=1,
            min_no_evidence_controls=0,
            min_relation_supported_ratio=0.0,
        )
        self.assertEqual(0, report["blocker_count"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            _write_json(
                product,
                {
                    "rag_eval_summary": {
                        "query_count": 5,
                        "expect_no_evidence_query_count": 3,
                        "relation_supported_ratio": 1.0,
                    },
                    "mcp_demo_answer_summary": {
                        "passed": False,
                        "query_count": 5,
                        "smoke_citation_count": 1,
                        "missing_supporting_result_count": 2,
                    },
                    "accuracy_comparison_summary": {
                        "passed": False,
                        "query_count": 5,
                        "mcp_regression_count": 1,
                    },
                },
            )

            report = build_answer_accuracy_evidence_depth(
                product_readiness_report=product,
                min_public_query_count=1,
                min_no_evidence_controls=0,
                min_relation_supported_ratio=0.0,
            )

        self.assertEqual(3, report["blocker_count"])
        self.assertEqual("blocked", report["pilot_evidence_status"])
        self.assertIn(
            "answer-evidence-smoke-citations",
            [finding["code"] for finding in report["findings"]],
        )

    def test_explicit_reports_override_stale_product_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            rag_eval = root / "rag_eval.json"
            demo = root / "demo.json"
            comparison = root / "comparison.json"
            _write_json(
                product,
                {
                    "rag_eval_summary": {
                        "query_count": 3,
                        "expect_no_evidence_query_count": 0,
                        "relation_supported_ratio": 0.0,
                    },
                    "mcp_demo_answer_summary": {
                        "query_count": 3,
                        "quality_issue_count": 0,
                    },
                    "accuracy_comparison_summary": {
                        "query_count": 3,
                        "mcp_regression_count": 1,
                    },
                },
            )
            _write_json(
                rag_eval,
                {
                    "query_count": 20,
                    "expect_no_evidence_query_count": 3,
                    "relation_supported_ratio": 0.5,
                },
            )
            _write_json(
                demo,
                {
                    "passed": False,
                    "query_count": 20,
                    "quality_issue_count": 2,
                    "items": [
                        {
                            "expect_no_evidence": False,
                            "supporting_result_count": 1,
                            "citations": [{"article_no": "제1조"}],
                        }
                    ],
                },
            )
            _write_json(
                comparison,
                {
                    "passed": True,
                    "query_count": 20,
                    "summary": {"mcp_regression_count": 0},
                },
            )

            report = build_answer_accuracy_evidence_depth(
                product_readiness_report=product,
                rag_eval_report=rag_eval,
                mcp_demo_answer_report=demo,
                accuracy_comparison_report=comparison,
                min_public_query_count=20,
                min_no_evidence_controls=3,
                min_relation_supported_ratio=0.5,
            )

        self.assertEqual(20, report["evidence_counts"]["distinct_evidence_query_count"])
        self.assertEqual(0, report["blocker_count"])
        self.assertEqual(
            ["answer-evidence-demo-quality-issues"],
            [finding["code"] for finding in report["findings"]],
        )

    def test_cli_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            out_json = root / "answer_depth.json"
            out_md = root / "answer_depth.md"
            _write_json(
                product,
                {
                    "rag_eval_summary": {
                        "query_count": 20,
                        "expect_no_evidence_query_count": 3,
                        "relation_supported_ratio": 0.5,
                    },
                    "mcp_demo_answer_summary": {"passed": True, "query_count": 20},
                    "accuracy_comparison_summary": {
                        "passed": True,
                        "query_count": 20,
                        "mcp_regression_count": 0,
                    },
                },
            )

            with redirect_stdout(StringIO()):
                exit_code = main(
                    [
                        "--product-readiness-report",
                        str(product),
                        "--out-json",
                        str(out_json),
                        "--out-md",
                        str(out_md),
                    ]
                )

            self.assertEqual(0, exit_code)
            self.assertTrue(json.loads(out_json.read_text(encoding="utf-8"))["passed_for_public_release_depth"])
            self.assertIn("Answer Accuracy Evidence Depth", out_md.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
