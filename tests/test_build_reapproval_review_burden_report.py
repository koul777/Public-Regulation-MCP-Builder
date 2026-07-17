from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_reapproval_review_burden_report import (
    build_reapproval_review_burden_report,
    main,
)


COMMIT = "a" * 40


class BuildReapprovalReviewBurdenReportTests(unittest.TestCase):
    def test_summarizes_initial_review_burden_without_approving_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worklist = root / "reapproval_worklist.json"
            batches = root / "reapproval_batches.json"
            decisions = root / "reapproval_decisions.csv"
            _write_json(worklist, _worklist_payload())
            _write_json(batches, _batch_payload())
            _write_decision_template(
                decisions,
                [
                    {"batch_rank": "1", "reapproval_batch_id": "batch-a"},
                    {"batch_rank": "2", "reapproval_batch_id": "batch-b"},
                ],
            )

            report = build_reapproval_review_burden_report(
                reapproval_worklist_report=worklist,
                reapproval_review_batch_report=batches,
                decision_template_csv=decisions,
            )

        self.assertEqual("reapproval_review_burden", report["report_type"])
        self.assertTrue(report["passed"])
        self.assertEqual("ready_for_operator_review", report["status"])
        self.assertEqual(0, report["blocking_count"])
        self.assertEqual(1, report["warning_count"])
        self.assertEqual("blocked_pending_operator_decisions", report["release_gate_status"])
        self.assertEqual(2, report["release_blocker_count"])
        self.assertEqual("reapproval-decisions-not-complete", report["release_blockers"][0]["code"])
        self.assertEqual("reapproval-decision-evidence-incomplete", report["release_blockers"][1]["code"])
        self.assertEqual(100, report["workload_summary"]["baseline_full_review_chunks"])
        self.assertEqual(20, report["workload_summary"]["recommended_initial_review_chunks"])
        self.assertEqual(80, report["workload_summary"]["deferred_after_initial_review_chunks"])
        self.assertEqual(0.8, report["workload_summary"]["initial_review_reduction_ratio"])
        self.assertEqual(10, report["tier_initial_review_breakdown"]["tiers"]["medium"]["initial_review_chunks"])
        self.assertEqual(10, report["tier_initial_review_breakdown"]["tiers"]["low"]["initial_review_chunks"])
        self.assertTrue(report["tier_initial_review_breakdown"]["matches_reported_initial_review_chunks"])
        self.assertEqual(2, report["decision_template_summary"]["row_count"])
        self.assertEqual(0, report["decision_template_operator_decision_complete_count"])
        self.assertEqual(2, report["decision_template_summary"]["operator_decision_blank_count"])
        self.assertEqual(2, report["decision_template_summary"]["approval_scope_confirmation_blank_count"])
        self.assertFalse(report["operator_controls"]["auto_approval"])
        self.assertFalse(report["operator_controls"]["auto_reindex"])
        self.assertEqual(
            ["reapproval_worklist_report", "reapproval_review_batch_manifest_report", "reapproval_decision_template_csv"],
            [item["role"] for item in report["source_report_artifacts"]],
        )

    def test_batch_coverage_mismatch_blocks_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worklist = root / "reapproval_worklist.json"
            batches = root / "reapproval_batches.json"
            batch_payload = _batch_payload()
            batch_payload["selected_candidate_count"] = 90
            _write_json(worklist, _worklist_payload(approval_provenance_missing_chunks=0))
            _write_json(batches, batch_payload)

            report = build_reapproval_review_burden_report(
                reapproval_worklist_report=worklist,
                reapproval_review_batch_report=batches,
            )

        self.assertFalse(report["passed"])
        self.assertEqual("blocked", report["status"])
        self.assertEqual(1, report["blocking_count"])
        self.assertEqual("reapproval-batch-coverage-mismatch", report["findings"][0]["code"])
        self.assertEqual(90, report["findings"][0]["mismatched_fields"]["selected_candidate_count"])

    def test_completed_decision_requires_reviewer_timestamp_and_scope_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worklist = root / "reapproval_worklist.json"
            batches = root / "reapproval_batches.json"
            decisions = root / "reapproval_decisions.csv"
            _write_json(worklist, _worklist_payload(approval_provenance_missing_chunks=0))
            _write_json(batches, _batch_payload())
            _write_decision_template(
                decisions,
                [
                    {
                        "batch_rank": "1",
                        "reapproval_batch_id": "batch-a",
                        "operator_decision": "approve_all_reviewed",
                    }
                ],
            )

            report = build_reapproval_review_burden_report(
                reapproval_worklist_report=worklist,
                reapproval_review_batch_report=batches,
                decision_template_csv=decisions,
            )

        self.assertEqual("blocked_pending_operator_decisions", report["release_gate_status"])
        self.assertEqual(1, report["release_blocker_count"])
        blocker = report["release_blockers"][0]
        self.assertEqual("reapproval-decision-evidence-incomplete", blocker["code"])
        self.assertEqual(1, blocker["reviewer_id_blank_count"])
        self.assertEqual(1, blocker["reviewed_at_blank_count"])
        self.assertEqual(1, blocker["approval_scope_confirmation_blank_count"])
        self.assertEqual(0, report["decision_template_operator_decision_complete_count"])

    def test_invalid_operator_decision_blocks_release_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worklist = root / "reapproval_worklist.json"
            batches = root / "reapproval_batches.json"
            decisions = root / "reapproval_decisions.csv"
            _write_json(worklist, _worklist_payload(approval_provenance_missing_chunks=0))
            _write_json(batches, _batch_payload())
            _write_decision_template(
                decisions,
                [
                    {
                        "batch_rank": "1",
                        "reapproval_batch_id": "batch-a",
                        "operator_decision": "approved",
                        "reviewer_id": "reviewer-a",
                        "reviewed_at": "2026-07-10T09:00:00+09:00",
                        "approval_scope_confirmation": "confirmed",
                    }
                ],
            )

            report = build_reapproval_review_burden_report(
                reapproval_worklist_report=worklist,
                reapproval_review_batch_report=batches,
                decision_template_csv=decisions,
            )

        self.assertEqual("blocked_pending_operator_decisions", report["release_gate_status"])
        self.assertEqual(1, report["release_blocker_count"])
        blocker = report["release_blockers"][0]
        self.assertEqual("reapproval-decisions-invalid", blocker["code"])
        self.assertEqual(["approved"], blocker["invalid_operator_decisions"])
        self.assertIn("approve_all_reviewed", blocker["allowed_operator_decisions"])

    def test_complete_operator_decisions_clear_release_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worklist = root / "reapproval_worklist.json"
            batches = root / "reapproval_batches.json"
            decisions = root / "reapproval_decisions.csv"
            _write_json(worklist, _worklist_payload(approval_provenance_missing_chunks=0))
            _write_json(batches, _batch_payload())
            _write_decision_template(
                decisions,
                [
                    {
                        "batch_rank": "1",
                        "reapproval_batch_id": "batch-a",
                        "operator_decision": "approve_all_reviewed",
                        "reviewer_id": "reviewer-a",
                        "reviewed_at": "2026-07-10T09:00:00+09:00",
                        "approval_scope_confirmation": "confirmed",
                    },
                    {
                        "batch_rank": "2",
                        "reapproval_batch_id": "batch-b",
                        "operator_decision": "needs_reprocess",
                        "reviewer_id": "reviewer-b",
                        "reviewed_at": "2026-07-10T09:05:00+09:00",
                        "approval_scope_confirmation": "confirmed",
                    },
                ],
            )

            report = build_reapproval_review_burden_report(
                reapproval_worklist_report=worklist,
                reapproval_review_batch_report=batches,
                decision_template_csv=decisions,
            )

        self.assertEqual("ready_for_release_gate", report["release_gate_status"])
        self.assertEqual(0, report["release_blocker_count"])
        self.assertEqual(2, report["decision_template_operator_decision_complete_count"])
        self.assertEqual(0, report["decision_template_summary"]["invalid_operator_decision_count"])

    def test_cli_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worklist = root / "reapproval_worklist.json"
            out_json = root / "burden.json"
            out_md = root / "burden.md"
            _write_json(worklist, _worklist_payload(approval_provenance_missing_chunks=0))

            exit_code = main(
                [
                    "--reapproval-worklist-report",
                    str(worklist),
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                ],
                stdout=io.StringIO(),
            )
            report = json.loads(out_json.read_text(encoding="utf-8"))
            markdown = out_md.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertEqual("reapproval_review_burden", report["report_type"])
        self.assertEqual("blocked_pending_operator_decisions", report["release_gate_status"])
        self.assertIn("Reapproval Review Burden", markdown)
        self.assertIn("Auto approval", markdown)


def _worklist_payload(*, approval_provenance_missing_chunks: int = 12) -> dict[str, object]:
    return {
        "report_type": "reapproval_worklist",
        "generated_at": "2026-07-09T15:00:00+00:00",
        "repo_commit": COMMIT,
        "document_count": 2,
        "total_approved_chunks": 100,
        "reapproval_candidate_chunks": 100,
        "reapproval_candidate_ratio": 1.0,
        "review_seconds_per_chunk": 20,
        "estimated_review_minutes": 34,
        "recommended_initial_review_chunks": 20,
        "estimated_initial_review_minutes": 7,
        "initial_review_reduction_ratio": 0.8,
        "low_risk_sample_rate": 0.05,
        "temporal_sample_rate": 0.15,
        "min_sample_chunks_per_tier": 10,
        "source_vector_integrity_failure_count": 0,
        "pre_reapproval_blockers": [],
        "approval_provenance_missing_chunks": approval_provenance_missing_chunks,
        "approval_provenance_only_chunks": 4,
        "approval_provenance_missing_field_counts": {"approval_worklist_report_path": 12},
        "review_triage_counts": {"high": 0, "medium": 20, "low": 80},
        "documents": [
            {
                "document_id": "doc-a",
                "temporal_sample_candidate_chunks": 20,
                "low_risk_candidate_chunks": 0,
                "high_risk_candidate_chunks": 0,
            },
            {
                "document_id": "doc-b",
                "temporal_sample_candidate_chunks": 0,
                "low_risk_candidate_chunks": 80,
                "high_risk_candidate_chunks": 0,
            },
        ],
    }


def _batch_payload() -> dict[str, object]:
    return {
        "report_type": "reapproval_review_batch_manifest",
        "generated_at": "2026-07-09T15:05:00+00:00",
        "repo_commit": COMMIT,
        "passed": True,
        "candidate_count": 100,
        "selected_candidate_count": 100,
        "batch_count": 2,
        "reapproval_chunk_count": 100,
        "max_chunks_per_batch": 50,
        "blocker_count": 0,
        "warning_count": 0,
        "risk_tier_chunk_counts": {"medium": 20, "low": 80},
        "action_chunk_counts": {"reprocess_then_reapprove_and_reindex": 100},
    }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_decision_template(path: Path, rows: list[dict[str, str]]) -> None:
    fields = [
        "batch_rank",
        "reapproval_batch_id",
        "operator_decision",
        "reviewer_id",
        "reviewed_at",
        "approval_scope_confirmation",
    ]
    lines = [",".join(fields)]
    for row in rows:
        lines.append(",".join(row.get(field, "") for field in fields))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
