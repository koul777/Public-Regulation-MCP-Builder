from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.check_preprocessing_change_guard import (
    event_pull_request_context,
    evaluate_guard,
    extract_body_fields,
    parse_changed_lines,
)


def completed_body(*, baseline_change: str = "없음") -> str:
    return f"""
<!-- preprocessing-guard:summary -->
표 셀 복원 경계 조건을 수정했습니다.
<!-- /preprocessing-guard:summary -->
<!-- preprocessing-guard:affected-formats -->
HWPX와 DOCX의 표 블록
<!-- /preprocessing-guard:affected-formats -->
<!-- preprocessing-guard:invariants -->
미승인 청크는 색인하지 않고 불확실한 표는 검수 플래그를 유지합니다.
<!-- /preprocessing-guard:invariants -->
<!-- preprocessing-guard:regression-evidence -->
python -m unittest tests.test_table_extractor -v 통과
<!-- /preprocessing-guard:regression-evidence -->
<!-- preprocessing-guard:baseline-change -->
{baseline_change}
<!-- /preprocessing-guard:baseline-change -->
"""


class PreprocessingChangeGuardTests(unittest.TestCase):
    def test_unprotected_change_passes_without_pr_contract(self) -> None:
        report = evaluate_guard(
            [{"path": "docs/operator_quickstart_ko.md", "status": "modified"}],
            pr_body="",
            labels=[],
        )

        self.assertTrue(report["passed"])
        self.assertFalse(report["protected_change"])

    def test_parser_change_fails_closed_without_test_body_or_label(self) -> None:
        report = evaluate_guard(
            [{"path": "app/parsers/hwpx_parser.py", "status": "modified"}],
            pr_body="",
            labels=[],
        )

        self.assertFalse(report["passed"])
        codes = {failure["code"] for failure in report["failures"]}
        self.assertIn("missing-focused-regression-test", codes)
        self.assertIn("missing-pr-body-field", codes)
        self.assertIn("missing-review-label", codes)

    def test_parser_change_passes_with_focused_test_contract_and_review_label(self) -> None:
        report = evaluate_guard(
            [
                {"path": "app/parsers/hwpx_parser.py", "status": "modified"},
                {"path": "tests/test_hwpx_parser.py", "status": "modified"},
            ],
            pr_body=completed_body(),
            labels=["documentation", "preprocessing-reviewed"],
        )

        self.assertTrue(report["passed"])
        self.assertEqual(["tests/test_hwpx_parser.py"], report["focused_tests"])
        self.assertTrue(report["review_label_present"])

    def test_unrelated_focused_test_cannot_satisfy_parser_change(self) -> None:
        report = evaluate_guard(
            [
                {"path": "app/parsers/hwpx_parser.py", "status": "modified"},
                {"path": "tests/test_api_security.py", "status": "modified"},
            ],
            pr_body=completed_body(),
            labels=["preprocessing-reviewed"],
        )

        self.assertFalse(report["passed"])
        failure = next(
            item
            for item in report["failures"]
            if item["code"] == "missing-mapped-regression-test"
        )
        self.assertEqual(["app/parsers/hwpx_parser.py"], failure["files"])
        self.assertEqual(
            [],
            report["mapped_tests_by_protected_file"]["app/parsers/hwpx_parser.py"],
        )

    def test_each_protected_component_requires_its_own_mapped_test(self) -> None:
        report = evaluate_guard(
            [
                {"path": "app/parsers/hwpx_parser.py", "status": "modified"},
                {"path": "app/core/security.py", "status": "modified"},
                {"path": "tests/test_hwpx_parser.py", "status": "modified"},
            ],
            pr_body=completed_body(),
            labels=["preprocessing-reviewed"],
        )

        self.assertFalse(report["passed"])
        self.assertEqual(["app/core/security.py"], report["unmatched_protected_files"])

        complete = evaluate_guard(
            [
                {"path": "app/parsers/hwpx_parser.py", "status": "modified"},
                {"path": "app/core/security.py", "status": "modified"},
                {"path": "tests/test_hwpx_parser.py", "status": "modified"},
                {"path": "tests/test_api_security.py", "status": "modified"},
            ],
            pr_body=completed_body(),
            labels=["preprocessing-reviewed"],
        )
        self.assertTrue(complete["passed"])

    def test_mcp_bundle_change_requires_focused_mcp_test_and_review_contract(self) -> None:
        unreviewed = evaluate_guard(
            [{"path": "scripts/generate_mcp_client_config.py", "status": "modified"}],
            pr_body="",
            labels=[],
        )

        self.assertFalse(unreviewed["passed"])
        self.assertIn("missing-focused-regression-test", {item["code"] for item in unreviewed["failures"]})
        reviewed = evaluate_guard(
            [
                {"path": "scripts/generate_mcp_client_config.py", "status": "modified"},
                {"path": "tests/test_generate_mcp_client_config.py", "status": "modified"},
            ],
            pr_body=completed_body(),
            labels=["preprocessing-reviewed"],
        )
        self.assertTrue(reviewed["passed"])
        self.assertEqual(["tests/test_generate_mcp_client_config.py"], reviewed["focused_tests"])

    def test_mcp_server_transport_change_is_protected(self) -> None:
        report = evaluate_guard(
            [
                {"path": "app/mcp_server/regulation_server.py", "status": "modified"},
                {"path": "tests/test_run_regulation_mcp.py", "status": "modified"},
            ],
            pr_body=completed_body(),
            labels=["preprocessing-reviewed"],
        )

        self.assertTrue(report["passed"])
        self.assertIn("app/mcp_server/regulation_server.py", report["logic_files"])

    def test_retrieval_and_vector_changes_require_focused_review_contract(self) -> None:
        cases = (
            ("app/ingestion/vector_adapter.py", "tests/test_vector_ingestion_adapter.py"),
            ("app/retrieval/bm25_index.py", "tests/test_bm25_index.py"),
            ("app/retrieval/hierarchical_index.py", "tests/test_input_packaging_parity.py"),
        )
        for logic_path, test_path in cases:
            with self.subTest(logic_path=logic_path):
                unreviewed = evaluate_guard(
                    [{"path": logic_path, "status": "modified"}],
                    pr_body="",
                    labels=[],
                )
                self.assertFalse(unreviewed["passed"])
                self.assertIn(logic_path, unreviewed["logic_files"])
                self.assertIn(
                    "missing-focused-regression-test",
                    {item["code"] for item in unreviewed["failures"]},
                )

                reviewed = evaluate_guard(
                    [
                        {"path": logic_path, "status": "modified"},
                        {"path": test_path, "status": "modified"},
                    ],
                    pr_body=completed_body(),
                    labels=["preprocessing-reviewed"],
                )
                self.assertTrue(reviewed["passed"])
                self.assertEqual([test_path], reviewed["focused_tests"])

    def test_approval_tenant_and_runtime_security_boundaries_are_protected(self) -> None:
        cases = (
            ("app/api/routes_documents.py", "tests/test_routes_documents.py"),
            ("app/api/routes_rag.py", "tests/test_routes_rag.py"),
            ("app/core/security.py", "tests/test_api_security.py"),
            ("app/core/tenant_access.py", "tests/test_tenant_access.py"),
            ("app/storage/repository.py", "tests/test_repository.py"),
            ("app/services/approval_validation.py", "tests/test_approval_validation.py"),
            ("app/services/regulation_rag_runtime.py", "tests/test_regulation_mcp_tools.py"),
            ("app/agents/execution_guard.py", "tests/test_agent_execution_guard.py"),
        )
        for logic_path, test_path in cases:
            with self.subTest(logic_path=logic_path):
                unreviewed = evaluate_guard(
                    [{"path": logic_path, "status": "modified"}],
                    pr_body="",
                    labels=[],
                )
                self.assertFalse(unreviewed["passed"])
                self.assertTrue(unreviewed["protected_change"])
                self.assertIn(logic_path, unreviewed["logic_files"])

                reviewed = evaluate_guard(
                    [
                        {"path": logic_path, "status": "modified"},
                        {"path": test_path, "status": "modified"},
                    ],
                    pr_body=completed_body(),
                    labels=["preprocessing-reviewed"],
                )
                self.assertTrue(reviewed["passed"])
                self.assertEqual([test_path], reviewed["focused_tests"])

    def test_public_release_boundary_changes_require_guard_review(self) -> None:
        for path in (
            ".github/workflows/auto-release.yml",
            "scripts/audit_release_hygiene.py",
            "scripts/audit_public_release_readiness.py",
            "scripts/run_public_release_gate.py",
            "scripts/create_public_orphan_snapshot.py",
        ):
            with self.subTest(path=path):
                report = evaluate_guard(
                    [{"path": path, "status": "modified"}],
                    pr_body="",
                    labels=[],
                )
                self.assertFalse(report["passed"])
                self.assertTrue(report["protected_change"])

    def test_tenant_runtime_scripts_require_focused_review_contract(self) -> None:
        for protected_path, focused_test in (
            ("scripts/apply_reapproval_plan_shadow.py", "tests/test_apply_reapproval_plan_shadow.py"),
            ("scripts/build_rag_security_evidence.py", "tests/test_build_rag_security_evidence.py"),
            ("scripts/run_mcp_smoke.py", "tests/test_run_mcp_smoke.py"),
        ):
            with self.subTest(protected_path=protected_path):
                report = evaluate_guard(
                    [
                        {"path": protected_path, "status": "modified"},
                        {"path": focused_test, "status": "modified"},
                    ],
                    pr_body=completed_body(),
                    labels=["preprocessing-reviewed"],
                )
                self.assertIn(protected_path, report["logic_files"])
                self.assertEqual([focused_test], report["focused_tests"])
                self.assertTrue(report["passed"])

    def test_deleted_test_does_not_count_as_regression_evidence(self) -> None:
        report = evaluate_guard(
            [
                {"path": "app/processors/chunker.py", "status": "modified"},
                {"path": "tests/test_chunker.py", "status": "removed"},
            ],
            pr_body=completed_body(),
            labels=["preprocessing-reviewed"],
        )

        self.assertFalse(report["passed"])
        self.assertEqual([], report["focused_tests"])
        self.assertIn("missing-focused-regression-test", {item["code"] for item in report["failures"]})

    def test_baseline_change_requires_specific_justification(self) -> None:
        report = evaluate_guard(
            [
                {
                    "path": "tests/fixtures/regression/integrated_pdf_quality_expectations_20260703.json",
                    "status": "modified",
                },
                {"path": "tests/test_pdf_parser.py", "status": "modified"},
            ],
            pr_body=completed_body(baseline_change="없음"),
            labels=["preprocessing-reviewed"],
        )

        self.assertFalse(report["passed"])
        self.assertIn("baseline-change-unjustified", {item["code"] for item in report["failures"]})

        justified = evaluate_guard(
            [
                {
                    "path": "tests/fixtures/regression/integrated_pdf_quality_expectations_20260703.json",
                    "status": "modified",
                },
                {"path": "tests/test_pdf_parser.py", "status": "modified"},
            ],
            pr_body=completed_body(
                baseline_change="PDF 열 순서 수정으로 coverage가 0.81에서 0.84로 변경되었고 검수 결과와 일치합니다."
            ),
            labels=["preprocessing-reviewed"],
        )
        self.assertTrue(justified["passed"])

    def test_guard_implementation_change_requires_guard_test(self) -> None:
        report = evaluate_guard(
            [{"path": "scripts/check_preprocessing_change_guard.py", "status": "modified"}],
            pr_body=completed_body(),
            labels=["preprocessing-reviewed"],
        )

        self.assertFalse(report["passed"])
        self.assertIn("missing-guard-regression-test", {item["code"] for item in report["failures"]})

    def test_name_status_parser_handles_renames_and_windows_paths(self) -> None:
        changes = parse_changed_lines(
            [
                "M\tapp\\processors\\chunker.py",
                "R100\ttests/test_old_parser.py\ttests/test_new_parser.py",
            ]
        )

        self.assertIn({"path": "app/processors/chunker.py", "status": "M"}, changes)
        self.assertIn({"path": "tests/test_old_parser.py", "status": "removed"}, changes)
        self.assertIn({"path": "tests/test_new_parser.py", "status": "renamed"}, changes)

    def test_event_context_reads_body_labels_and_refs_without_exposing_body_in_report(self) -> None:
        payload = {
            "pull_request": {
                "body": completed_body(),
                "labels": [{"name": "preprocessing-reviewed"}],
                "base": {"sha": "base-sha"},
                "head": {"sha": "head-sha"},
            }
        }

        context = event_pull_request_context(payload)

        self.assertEqual("base-sha", context["base_ref"])
        self.assertEqual("head-sha", context["head_ref"])
        self.assertEqual(["preprocessing-reviewed"], context["labels"])
        self.assertEqual(set(extract_body_fields(context["body"])), {
            "summary",
            "affected-formats",
            "invariants",
            "regression-evidence",
            "baseline-change",
        })

    def test_report_is_json_serializable(self) -> None:
        report = evaluate_guard(
            [{"path": "CONTRIBUTING.md", "status": "modified"}],
            pr_body=completed_body(),
            labels=["preprocessing-reviewed"],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "report.json"
            path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["passed"])


if __name__ == "__main__":
    unittest.main()
