from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_mcp_answer_evidence_bundle import build_mcp_answer_evidence_bundle, main


class BuildMcpAnswerEvidenceBundleTests(unittest.TestCase):
    def test_bundles_passing_answer_evidence_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accuracy = _write_json(
                root / "accuracy.json",
                {
                    "report_type": "simple_rag_vs_mcp_accuracy",
                    "passed": True,
                    "query_count": 2,
                    "query_spec_path": "config/queries.json",
                    "query_spec_sha256": "a" * 64,
                    "query_spec_item_count": 2,
                    "summary": {"mcp_regression_count": 0},
                },
            )
            benchmark = _write_json(
                root / "benchmark.json",
                {
                    "report_type": "mcp_query_benchmark",
                    "passed": True,
                    "query_count": 2,
                    "query_spec_path": "config/queries.json",
                    "query_spec_sha256": "a" * 64,
                    "query_spec_item_count": 2,
                    "finding_count": 0,
                },
            )
            demo = _write_json(
                root / "demo.json",
                {
                    "report_type": "mcp_demo_answers",
                    "passed": True,
                    "query_count": 2,
                    "query_spec_path": "config/queries.json",
                    "query_spec_sha256": "a" * 64,
                    "query_spec_item_count": 2,
                    "quality_issue_count": 0,
                },
            )
            rag_eval = _write_json(
                root / "rag_eval.json",
                {"report_type": "rag_retrieval_eval", "answerable_ratio": 0.8},
            )
            product = _write_json(
                root / "product.json",
                {
                    "report_type": "mcp_product_readiness",
                    "passed": True,
                    "gates": {"answer_accuracy": {"status": "passed", "blocker_count": 0, "warning_count": 0}},
                },
            )

            report = build_mcp_answer_evidence_bundle(
                accuracy_comparison_report=accuracy,
                query_benchmark_report=benchmark,
                demo_answer_report=demo,
                rag_eval_report=rag_eval,
                product_readiness_report=product,
                require_shared_query_spec=True,
            )

        self.assertTrue(report["passed"])
        self.assertTrue(report["bundle_ready"])
        self.assertEqual(0, report["finding_count"])
        self.assertEqual(5, report["artifact_count"])
        self.assertEqual(1, report["query_spec_summary"]["unique_query_spec_sha256_count"])
        self.assertEqual(2, report["query_count_summary"]["min_query_count"])
        self.assertEqual(0, report["answer_accuracy_summary"]["mcp_regression_count"])
        self.assertEqual(0, report["answer_accuracy_summary"]["benchmark_finding_count"])
        self.assertEqual(0, report["answer_accuracy_summary"]["demo_quality_issue_count"])

    def test_flags_accuracy_regression_and_missing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accuracy = _write_json(
                root / "accuracy.json",
                {
                    "report_type": "simple_rag_vs_mcp_accuracy",
                    "passed": False,
                    "summary": {"mcp_regression_count": 1},
                },
            )

            report = build_mcp_answer_evidence_bundle(
                accuracy_comparison_report=accuracy,
                demo_answer_report=root / "missing_demo.json",
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("artifact-not-passed", codes)
        self.assertIn("mcp-accuracy-regression", codes)
        self.assertIn("artifact-missing", codes)

    def test_product_readiness_failure_does_not_block_when_answer_accuracy_gate_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accuracy = _write_json(
                root / "accuracy.json",
                {
                    "report_type": "simple_rag_vs_mcp_accuracy",
                    "passed": True,
                    "query_count": 5,
                    "summary": {"mcp_regression_count": 0},
                },
            )
            product = _write_json(
                root / "product.json",
                {
                    "report_type": "mcp_product_readiness",
                    "passed": False,
                    "gates": {"answer_accuracy": {"status": "ready", "blocker_count": 0, "warning_count": 0}},
                },
            )

            report = build_mcp_answer_evidence_bundle(
                accuracy_comparison_report=accuracy,
                product_readiness_report=product,
            )

        self.assertTrue(report["passed"])
        self.assertEqual([], report["findings"])

    def test_product_answer_accuracy_gate_blocker_blocks_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = _write_json(
                root / "product.json",
                {
                    "report_type": "mcp_product_readiness",
                    "passed": False,
                    "gates": {"answer_accuracy": {"status": "blocked", "blocker_count": 1, "warning_count": 0}},
                },
            )

            report = build_mcp_answer_evidence_bundle(product_readiness_report=product)

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("artifact-not-passed", codes)
        self.assertIn("product-answer-accuracy-gate-blocked", codes)

    def test_shared_query_spec_mismatch_is_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accuracy = _write_json(
                root / "accuracy.json",
                {
                    "report_type": "simple_rag_vs_mcp_accuracy",
                    "passed": True,
                    "summary": {"mcp_regression_count": 0},
                    "query_spec_sha256": "a" * 64,
                },
            )
            benchmark = _write_json(
                root / "benchmark.json",
                {
                    "report_type": "mcp_query_benchmark",
                    "passed": True,
                    "finding_count": 0,
                    "query_spec_sha256": "b" * 64,
                },
            )

            report = build_mcp_answer_evidence_bundle(
                accuracy_comparison_report=accuracy,
                query_benchmark_report=benchmark,
                require_shared_query_spec=True,
            )

        self.assertTrue(report["passed"])
        self.assertFalse(report["bundle_ready"])
        self.assertEqual(1, report["warning_count"])
        self.assertEqual("query-spec-fingerprint-mismatch", report["findings"][0]["code"])

    def test_shared_query_spec_missing_metadata_is_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accuracy = _write_json(
                root / "accuracy.json",
                {
                    "report_type": "simple_rag_vs_mcp_accuracy",
                    "passed": True,
                    "query_count": 2,
                    "summary": {"mcp_regression_count": 0},
                    "query_spec_sha256": "a" * 64,
                },
            )
            benchmark = _write_json(
                root / "benchmark.json",
                {
                    "report_type": "mcp_query_benchmark",
                    "passed": True,
                    "query_count": 2,
                    "finding_count": 0,
                },
            )

            report = build_mcp_answer_evidence_bundle(
                accuracy_comparison_report=accuracy,
                query_benchmark_report=benchmark,
                require_shared_query_spec=True,
            )

        self.assertTrue(report["passed"])
        self.assertFalse(report["bundle_ready"])
        self.assertEqual(1, report["warning_count"])
        self.assertEqual("query-spec-fingerprint-missing", report["findings"][0]["code"])
        self.assertEqual(["query_benchmark"], report["findings"][0]["roles"])
        self.assertEqual(["query_benchmark"], report["query_spec_summary"]["missing_query_spec_roles"])

    def test_min_query_count_blocks_small_evidence_sets_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accuracy = _write_json(
                root / "accuracy.json",
                {
                    "report_type": "simple_rag_vs_mcp_accuracy",
                    "passed": True,
                    "query_count": 5,
                    "summary": {"mcp_regression_count": 0},
                },
            )
            demo = _write_json(
                root / "demo.json",
                {
                    "report_type": "mcp_demo_answers",
                    "passed": True,
                    "query_count": 12,
                    "quality_issue_count": 0,
                },
            )

            report = build_mcp_answer_evidence_bundle(
                accuracy_comparison_report=accuracy,
                demo_answer_report=demo,
                min_query_count=10,
            )

        findings = report["findings"]
        self.assertFalse(report["passed"])
        self.assertEqual(1, report["blocking_count"])
        self.assertEqual("query-count-below-minimum", findings[0]["code"])
        self.assertEqual("accuracy_comparison", findings[0]["role"])
        self.assertEqual(5, findings[0]["query_count"])
        self.assertEqual(10, findings[0]["min_query_count"])

    def test_min_query_count_blocks_missing_query_count_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accuracy = _write_json(
                root / "accuracy.json",
                {
                    "report_type": "simple_rag_vs_mcp_accuracy",
                    "passed": True,
                    "summary": {"mcp_regression_count": 0},
                },
            )

            report = build_mcp_answer_evidence_bundle(
                accuracy_comparison_report=accuracy,
                min_query_count=10,
            )

        self.assertFalse(report["passed"])
        self.assertEqual(1, report["blocking_count"])
        self.assertEqual("query-count-missing", report["findings"][0]["code"])
        self.assertEqual("accuracy_comparison", report["findings"][0]["role"])
        self.assertEqual(10, report["findings"][0]["min_query_count"])

    def test_cli_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accuracy = _write_json(
                root / "accuracy.json",
                {
                    "report_type": "simple_rag_vs_mcp_accuracy",
                    "passed": True,
                    "query_count": 1,
                    "summary": {"mcp_regression_count": 0},
                },
            )
            out_json = root / "bundle.json"
            out_md = root / "bundle.md"

            exit_code = main(
                [
                    "--accuracy-comparison-report",
                    str(accuracy),
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                    "--fail-on-issue",
                ],
                stdout=io.StringIO(),
            )
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            markdown = out_md.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertEqual("mcp_answer_evidence_bundle", payload["report_type"])
        self.assertIn("MCP Answer Evidence Bundle", markdown)


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main()
