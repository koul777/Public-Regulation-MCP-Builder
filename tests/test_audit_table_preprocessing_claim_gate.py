from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.audit_table_preprocessing_claim_gate import (
    audit_table_preprocessing_claim_gate,
    main as claim_gate_main,
)


class AuditTablePreprocessingClaimGateTests(unittest.TestCase):
    def test_blocks_quality_claim_until_review_transfer_and_answer_blockers_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review = root / "review.json"
            transfer = root / "transfer.json"
            trace = root / "trace.json"
            answer = root / "answer.json"
            _write_json(
                review,
                _review_summary(
                    selected=144,
                    completed=0,
                    pending=144,
                    ready=False,
                ),
            )
            _write_json(transfer, _transfer_validation(passed=False, blocker_count=25))
            _write_json(trace, _source_traceability(passed=True, issue_count=0))
            _write_json(answer, _answer_map(table_blockers=2))

            report = audit_table_preprocessing_claim_gate(
                table_unit_review_summary=review,
                table_count_transfer_validation=transfer,
                table_source_traceability_report=trace,
                answer_blocker_review_map=answer,
            )

        self.assertFalse(report["passed"])
        self.assertEqual("blocked_pending_human_review", report["status"])
        self.assertEqual("review_ready_not_accuracy_proven", report["claim_level"])
        self.assertEqual("feasible_with_human_review", report["feasibility_status"])
        self.assertEqual(2, report["summary"]["table_answer_blocker_count"])
        self.assertEqual(
            "table_unit_human_review_pending",
            report["summary"]["transfer_root_cause_summary"]["primary_blocker"],
        )
        self.assertEqual(
            {"verified_pdf": 1, "verified_hwpx_zip": 1},
            report["summary"]["source_format_status_counts"],
        )
        self.assertEqual(1152, report["summary"]["required_field_missing_total"])
        self.assertEqual(
            {"source_table_compare": 144},
            report["summary"]["review_priority_counts"],
        )
        self.assertEqual(
            {"missing_table_label": 144},
            report["summary"]["label_review_flag_counts"],
        )
        self.assertFalse(report["summary"]["non_review_evidence_ready"])
        self.assertFalse(report["summary"]["release_blocked_by_human_review"])
        codes = {finding["code"] for finding in report["findings"]}
        self.assertIn("table-human-review-pending", codes)
        self.assertIn("table-count-transfer-blocked", codes)
        self.assertIn("table-answer-blockers-open", codes)
        self.assertEqual(
            [
                "complete_table_unit_human_review",
                "transfer_reviewed_table_counts_to_goldset",
                "close_answer_level_table_blockers",
            ],
            [step["step"] for step in report["next_steps"]],
        )

    def test_marks_non_review_evidence_ready_when_human_review_is_remaining_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review = root / "review.json"
            transfer = root / "transfer.json"
            trace = root / "trace.json"
            answer = root / "answer.json"
            _write_json(
                review,
                _review_summary(
                    selected=20,
                    completed=0,
                    pending=20,
                    ready=False,
                ),
            )
            _write_json(transfer, _transfer_validation(passed=False, blocker_count=5))
            _write_json(trace, _source_traceability(passed=True, issue_count=0))
            _write_json(answer, _answer_map(table_blockers=0))

            report = audit_table_preprocessing_claim_gate(
                table_unit_review_summary=review,
                table_count_transfer_validation=transfer,
                table_source_traceability_report=trace,
                answer_blocker_review_map=answer,
            )

        self.assertFalse(report["passed"])
        self.assertEqual("blocked_pending_human_review", report["status"])
        self.assertTrue(report["summary"]["non_review_evidence_ready"])
        self.assertTrue(report["summary"]["release_blocked_by_human_review"])
        self.assertEqual(0, report["summary"]["table_answer_blocker_count"])

    def test_passes_when_trace_review_transfer_and_answer_map_are_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review = root / "review.json"
            transfer = root / "transfer.json"
            trace = root / "trace.json"
            answer = root / "answer.json"
            _write_json(
                review,
                _review_summary(
                    selected=5,
                    completed=5,
                    pending=0,
                    ready=True,
                ),
            )
            _write_json(transfer, _transfer_validation(passed=True, blocker_count=0))
            _write_json(trace, _source_traceability(passed=True, issue_count=0))
            _write_json(answer, _answer_map(table_blockers=0))

            report = audit_table_preprocessing_claim_gate(
                table_unit_review_summary=review,
                table_count_transfer_validation=transfer,
                table_source_traceability_report=trace,
                answer_blocker_review_map=answer,
            )

        self.assertTrue(report["passed"])
        self.assertEqual("ready_for_table_quality_claim", report["status"])
        self.assertEqual("quality_claim_ready", report["claim_level"])
        self.assertEqual(0, report["blocker_count"])
        self.assertTrue(report["summary"]["non_review_evidence_ready"])
        self.assertFalse(report["summary"]["release_blocked_by_human_review"])
        self.assertEqual([], report["next_steps"])

    def test_cli_writes_reports_and_can_fail_on_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review = root / "review.json"
            transfer = root / "transfer.json"
            trace = root / "trace.json"
            out_json = root / "claim.json"
            out_md = root / "claim.md"
            _write_json(
                review,
                _review_summary(
                    selected=1,
                    completed=0,
                    pending=1,
                    ready=False,
                ),
            )
            _write_json(transfer, _transfer_validation(passed=False, blocker_count=1))
            _write_json(trace, _source_traceability(passed=True, issue_count=0))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = claim_gate_main(
                    [
                        "--table-unit-review-summary",
                        str(review),
                        "--table-count-transfer-validation",
                        str(transfer),
                        "--table-source-traceability-report",
                        str(trace),
                        "--out-json",
                        str(out_json),
                        "--out-md",
                        str(out_md),
                        "--fail-on-blocker",
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assertIn("table_preprocessing_claim_gate", stdout.getvalue())
            self.assertTrue(out_json.is_file())
            self.assertTrue(out_md.is_file())
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual("table_preprocessing_claim_gate", payload["report_type"])
            self.assertIn("table-answer-blocker-map-missing", payload["finding_code_counts"])

    def test_blocks_before_review_when_source_traceability_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review = root / "review.json"
            transfer = root / "transfer.json"
            trace = root / "trace.json"
            answer = root / "answer.json"
            _write_json(review, _review_summary(selected=5, completed=5, pending=0, ready=True))
            _write_json(transfer, _transfer_validation(passed=True, blocker_count=0))
            _write_json(trace, _source_traceability(passed=False, issue_count=1))
            _write_json(answer, _answer_map(table_blockers=0))

            report = audit_table_preprocessing_claim_gate(
                table_unit_review_summary=review,
                table_count_transfer_validation=transfer,
                table_source_traceability_report=trace,
                answer_blocker_review_map=answer,
            )

        self.assertFalse(report["passed"])
        self.assertEqual("blocked_source_traceability", report["status"])
        self.assertEqual("blocked_before_review", report["feasibility_status"])
        self.assertIn("table-source-traceability-blocked", report["finding_code_counts"])
        self.assertFalse(report["summary"]["non_review_evidence_ready"])
        self.assertFalse(report["summary"]["release_blocked_by_human_review"])
        self.assertEqual("repair_source_traceability", report["next_steps"][0]["step"])

    def test_source_traceability_backend_issue_gets_specific_next_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review = root / "review.json"
            transfer = root / "transfer.json"
            trace = root / "trace.json"
            answer = root / "answer.json"
            out_md = root / "claim.md"
            _write_json(review, _review_summary(selected=5, completed=5, pending=0, ready=True))
            _write_json(transfer, _transfer_validation(passed=True, blocker_count=0))
            payload = _source_traceability(passed=False, issue_count=15)
            payload["issue_counts"] = {"pdf-reader-backend-unavailable": 15}
            payload["operator_next_action_counts"] = {
                "Fix the Python PDF reader backend or run traceability in the packaged project environment; the source PDF has not been proven invalid.": 15
            }
            _write_json(trace, payload)
            _write_json(answer, _answer_map(table_blockers=0))

            report = audit_table_preprocessing_claim_gate(
                table_unit_review_summary=review,
                table_count_transfer_validation=transfer,
                table_source_traceability_report=trace,
                answer_blocker_review_map=answer,
                out_md=out_md,
            )
            markdown = out_md.read_text(encoding="utf-8")

        self.assertFalse(report["passed"])
        self.assertEqual(
            {"pdf-reader-backend-unavailable": 15},
            report["summary"]["source_traceability_issue_counts"],
        )
        self.assertEqual(
            {"pdf-reader-backend-unavailable": 15},
            report["findings"][0]["issue_counts"],
        )
        self.assertIn("Python PDF reader backend", report["next_steps"][0]["detail"])
        self.assertIn("pdf-reader-backend-unavailable", markdown)

    def test_blocks_when_table_evidence_drift_check_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review = root / "review.json"
            transfer = root / "transfer.json"
            trace = root / "trace.json"
            answer = root / "answer.json"
            drift = root / "drift.json"
            _write_json(review, _review_summary(selected=5, completed=5, pending=0, ready=True))
            _write_json(transfer, _transfer_validation(passed=True, blocker_count=0))
            _write_json(trace, _source_traceability(passed=True, issue_count=0))
            _write_json(answer, _answer_map(table_blockers=0))
            _write_json(drift, _drift_check(passed=False, blocker_count=1))

            report = audit_table_preprocessing_claim_gate(
                table_unit_review_summary=review,
                table_count_transfer_validation=transfer,
                table_source_traceability_report=trace,
                answer_blocker_review_map=answer,
                table_drift_check_report=drift,
            )

        self.assertFalse(report["passed"])
        self.assertEqual("blocked_evidence_drift", report["status"])
        self.assertEqual("blocked_before_review", report["feasibility_status"])
        self.assertIn("table-evidence-drift-detected", report["finding_code_counts"])
        self.assertTrue(report["summary"]["drift_check_present"])
        self.assertFalse(report["summary"]["drift_check_passed"])
        self.assertEqual(1, report["summary"]["drift_check_blocker_count"])
        self.assertEqual("repair_table_evidence_lineage", report["next_steps"][0]["step"])

    def test_accepts_passing_drift_only_for_exact_mixed_path_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review = root / "review.json"
            transfer = root / "transfer.json"
            trace = root / "trace.json"
            answer = root / "answer.json"
            drift = root / "drift.json"
            _write_json(review, _review_summary(selected=5, completed=5, pending=0, ready=True))
            _write_json(transfer, _transfer_validation(passed=True, blocker_count=0))
            _write_json(trace, _source_traceability(passed=True, issue_count=0))
            _write_json(answer, _answer_map(table_blockers=0))
            _write_json(
                drift,
                _drift_check(
                    passed=True,
                    blocker_count=0,
                    base_dir=root,
                    source_reports={
                        "table_unit_review_summary_report": "review.json",
                        "table_count_transfer_validation_report": str(transfer.resolve()),
                        "table_source_traceability_report": str(Path("nested") / ".." / "trace.json"),
                    },
                ),
            )

            report = audit_table_preprocessing_claim_gate(
                table_unit_review_summary=review,
                table_count_transfer_validation=transfer,
                table_source_traceability_report=trace,
                answer_blocker_review_map=answer,
                table_drift_check_report=drift,
                require_table_drift_check=True,
            )

        self.assertTrue(report["passed"])
        self.assertEqual("ready_for_table_quality_claim", report["status"])
        self.assertTrue(report["summary"]["drift_check_source_reports_match"])
        self.assertEqual(0, report["summary"]["drift_check_lineage_mismatch_count"])
        self.assertNotIn(
            "table-evidence-drift-source-reports-mismatch",
            report["finding_code_counts"],
        )

    def test_blocks_stale_passing_drift_from_different_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale = root / "stale"
            stale.mkdir()
            review = root / "review.json"
            transfer = root / "transfer.json"
            trace = root / "trace.json"
            answer = root / "answer.json"
            drift = root / "drift.json"
            _write_json(review, _review_summary(selected=5, completed=5, pending=0, ready=True))
            _write_json(transfer, _transfer_validation(passed=True, blocker_count=0))
            _write_json(trace, _source_traceability(passed=True, issue_count=0))
            _write_json(answer, _answer_map(table_blockers=0))
            _write_json(stale / "review.json", _review_summary(selected=5, completed=5, pending=0, ready=True))
            _write_json(stale / "transfer.json", _transfer_validation(passed=True, blocker_count=0))
            _write_json(stale / "trace.json", _source_traceability(passed=True, issue_count=0))
            _write_json(
                drift,
                _drift_check(
                    passed=True,
                    blocker_count=0,
                    base_dir=root,
                    source_reports={
                        "table_unit_review_summary_report": str(Path("stale") / "review.json"),
                        "table_count_transfer_validation_report": str(Path("stale") / "transfer.json"),
                        "table_source_traceability_report": str(Path("stale") / "trace.json"),
                    },
                ),
            )

            report = audit_table_preprocessing_claim_gate(
                table_unit_review_summary=review,
                table_count_transfer_validation=transfer,
                table_source_traceability_report=trace,
                answer_blocker_review_map=answer,
                table_drift_check_report=drift,
                require_table_drift_check=True,
            )

        self.assertFalse(report["passed"])
        self.assertEqual("blocked_evidence_drift", report["status"])
        self.assertEqual("blocked_before_review", report["feasibility_status"])
        self.assertFalse(report["summary"]["drift_check_source_reports_match"])
        self.assertEqual(3, report["summary"]["drift_check_lineage_mismatch_count"])
        finding = next(
            item
            for item in report["findings"]
            if item["code"] == "table-evidence-drift-source-reports-mismatch"
        )
        self.assertEqual(3, finding["mismatch_count"])
        self.assertEqual(
            {
                "table_unit_review_summary_report",
                "table_count_transfer_validation_report",
                "table_source_traceability_report",
            },
            set(finding["mismatch_roles"]),
        )
        self.assertEqual("repair_table_evidence_lineage", report["next_steps"][0]["step"])

    def test_blocks_passing_drift_without_source_report_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review = root / "review.json"
            transfer = root / "transfer.json"
            trace = root / "trace.json"
            answer = root / "answer.json"
            drift = root / "drift.json"
            _write_json(review, _review_summary(selected=5, completed=5, pending=0, ready=True))
            _write_json(transfer, _transfer_validation(passed=True, blocker_count=0))
            _write_json(trace, _source_traceability(passed=True, issue_count=0))
            _write_json(answer, _answer_map(table_blockers=0))
            _write_json(drift, _drift_check(passed=True, blocker_count=0))

            report = audit_table_preprocessing_claim_gate(
                table_unit_review_summary=review,
                table_count_transfer_validation=transfer,
                table_source_traceability_report=trace,
                answer_blocker_review_map=answer,
                table_drift_check_report=drift,
                require_table_drift_check=True,
            )

        self.assertFalse(report["passed"])
        self.assertEqual("blocked_evidence_drift", report["status"])
        finding = next(
            item
            for item in report["findings"]
            if item["code"] == "table-evidence-drift-source-reports-mismatch"
        )
        self.assertEqual(3, finding["mismatch_count"])
        self.assertEqual(
            {"source_report_missing"},
            {mismatch["reason"] for mismatch in finding["mismatches"]},
        )

    def test_required_table_drift_check_missing_blocks_claim_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review = root / "review.json"
            transfer = root / "transfer.json"
            trace = root / "trace.json"
            answer = root / "answer.json"
            _write_json(review, _review_summary(selected=5, completed=5, pending=0, ready=True))
            _write_json(transfer, _transfer_validation(passed=True, blocker_count=0))
            _write_json(trace, _source_traceability(passed=True, issue_count=0))
            _write_json(answer, _answer_map(table_blockers=0))

            report = audit_table_preprocessing_claim_gate(
                table_unit_review_summary=review,
                table_count_transfer_validation=transfer,
                table_source_traceability_report=trace,
                answer_blocker_review_map=answer,
                require_table_drift_check=True,
            )

        self.assertFalse(report["passed"])
        self.assertEqual("blocked_evidence_drift", report["status"])
        self.assertEqual("blocked_before_review", report["feasibility_status"])
        self.assertIn("table-evidence-drift-check-missing", report["finding_code_counts"])
        self.assertFalse(report["summary"]["drift_check_present"])
        self.assertIsNone(report["summary"]["drift_check_passed"])
        self.assertEqual("repair_table_evidence_lineage", report["next_steps"][0]["step"])

    def test_missing_answer_map_keeps_claim_unproven_without_blocking_traceability_feasibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review = root / "review.json"
            transfer = root / "transfer.json"
            trace = root / "trace.json"
            _write_json(review, _review_summary(selected=5, completed=5, pending=0, ready=True))
            _write_json(transfer, _transfer_validation(passed=True, blocker_count=0))
            _write_json(trace, _source_traceability(passed=True, issue_count=0))

            report = audit_table_preprocessing_claim_gate(
                table_unit_review_summary=review,
                table_count_transfer_validation=transfer,
                table_source_traceability_report=trace,
            )

        self.assertFalse(report["passed"])
        self.assertEqual("blocked_answer_table_review", report["status"])
        self.assertEqual("review_ready_not_accuracy_proven", report["claim_level"])
        self.assertEqual("feasible_with_human_review", report["feasibility_status"])
        self.assertEqual(0, report["blocker_count"])
        self.assertEqual(1, report["warning_count"])
        self.assertIn("table-answer-blocker-map-missing", report["finding_code_counts"])


def _review_summary(*, selected: int, completed: int, pending: int, ready: bool) -> dict:
    required_fields = [
        "human_source_pages_checked",
        "human_unit_status",
        "human_manual_table_count",
        "human_matched_table_count",
        "human_row_column_match",
        "human_parentage_ok",
        "human_reviewer",
        "human_reviewed_at",
    ]
    return {
        "report_type": "parsing_goldset_table_unit_review_summary",
        "source_compare_only": True,
        "document_count": 2,
        "selected_unit_count": selected,
        "completed_unit_count": completed,
        "pending_unit_count": pending,
        "invalid_unit_count": 0,
        "required_field_missing_total": pending * len(required_fields),
        "required_field_missing_counts": {field: pending for field in required_fields if pending},
        "review_priority_counts": {"source_table_compare": selected},
        "label_review_flag_counts": {"missing_table_label": pending} if pending else {},
        "ready_for_table_score_transfer": ready,
    }


def _transfer_validation(*, passed: bool, blocker_count: int) -> dict:
    return {
        "report_type": "parsing_goldset_table_count_transfer_validation",
        "passed": passed,
        "blocker_count": blocker_count,
        "finding_code_counts": {"table-review-summary-not-ready": 1} if blocker_count else {},
        "root_cause_summary": {
            "primary_blocker": "table_unit_human_review_pending" if blocker_count else "none",
            "recommended_next_step": "complete_table_unit_human_review" if blocker_count else "none",
        },
    }


def _source_traceability(*, passed: bool, issue_count: int) -> dict:
    return {
        "report_type": "table_review_source_traceability",
        "traceability_passed": passed,
        "record_count": 2,
        "batch_count": 2,
        "blocked_batch_count": 0 if passed else 1,
        "issue_count": issue_count,
        "page_count_status_counts": {"verified_pdf": 2},
        "source_format_status_counts": {"verified_pdf": 1, "verified_hwpx_zip": 1},
    }


def _answer_map(*, table_blockers: int) -> dict:
    return {
        "report_type": "answer_blocker_review_map",
        "query_count": 20,
        "failed_query_count": table_blockers,
        "quality_issue_count": table_blockers,
        "blocker_category_counts": {
            "table_parentage_or_structure_review": table_blockers,
        },
    }


def _drift_check(
    *,
    passed: bool,
    blocker_count: int,
    base_dir: Path | None = None,
    source_reports: dict[str, str] | None = None,
) -> dict:
    payload = {
        "report_type": "parsing_goldset_table_drift_check",
        "passed": passed,
        "blocker_count": blocker_count,
        "warning_count": 0,
    }
    if base_dir is not None:
        payload["base_dir"] = str(base_dir)
    if source_reports is not None:
        payload["source_reports"] = source_reports
    return payload


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
