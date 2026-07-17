from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_reapproval_apply_plan import build_reapproval_apply_plan, main


class BuildReapprovalApplyPlanTests(unittest.TestCase):
    def test_builds_read_only_apply_plan_from_validated_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_manifest(
                root / "batches.json",
                [
                    _batch("batch-a", "doc-a", ["chunk-1", "chunk-2"]),
                    _batch("batch-b", "doc-b", ["chunk-3"]),
                    _batch("batch-c", "doc-c", ["chunk-4", "chunk-5"]),
                    _batch("batch-d", "doc-d", ["chunk-6"]),
                ],
            )
            decisions = root / "decisions.csv"
            _write_decisions(
                decisions,
                [
                    _decision("batch-a", "approve_all_reviewed"),
                    _decision("batch-b", "reject_all"),
                    _decision("batch-c", "partial_with_overrides", overrides='{"chunk-4":"reject","chunk-5":"defer"}'),
                    _decision("batch-d", "needs_reprocess"),
                ],
            )
            validation = _write_validation(root / "validation.json", manifest, decisions, expected_batch_count=4)

            report = build_reapproval_apply_plan(
                reapproval_review_batch_report=manifest,
                decision_template_csv=decisions,
                decision_validation_report=validation,
            )

        self.assertTrue(report["passed"])
        self.assertEqual("ready_for_apply_execution", report["release_gate_status"])
        self.assertEqual(0, report["blocker_count"])
        self.assertFalse(report["operator_controls"]["auto_approval"])
        self.assertFalse(report["operator_controls"]["auto_reindex"])
        self.assertFalse(report["operator_controls"]["applies_reapproval_decisions"])
        self.assertTrue(report["operator_controls"]["requires_dedicated_apply_step"])
        self.assertFalse(report["operator_controls"]["direct_approval_metadata_write_allowed"])
        self.assertTrue(report["operator_controls"]["requires_tenant_and_operator_access_control"])
        self.assertTrue(report["operator_controls"]["requires_shared_review_workflow_contract"])
        self.assertTrue(report["operator_controls"]["requires_approval_precondition_validation"])
        self.assertTrue(report["operator_controls"]["requires_rejection_decision_validation"])
        self.assertTrue(report["operator_controls"]["requires_preapproval_security_scan"])
        self.assertTrue(report["operator_controls"]["requires_review_flag_acknowledgement"])
        self.assertTrue(report["operator_controls"]["requires_approved_content_hash_recalculation"])
        self.assertTrue(report["operator_controls"]["requires_apply_audit_event"])
        self.assertTrue(report["operator_controls"]["requires_vector_sync_or_explicit_reindex"])
        self.assertTrue(report["operator_controls"]["requires_explicit_reindex_phase_by_default"])
        self.assertTrue(report["operator_controls"]["conditional_vector_sync_requires_existing_successful_index"])
        self.assertTrue(report["operator_controls"]["requires_dry_run_before_apply"])
        self.assertTrue(report["operator_controls"]["requires_confirm_apply"])
        self.assertTrue(report["operator_controls"]["mutating_executor_implemented"])
        self.assertEqual("shadow_runtime_only", report["operator_controls"]["mutating_executor_scope"])
        self.assertFalse(report["operator_controls"]["in_place_runtime_mutation_allowed"])
        self.assertFalse(report["operator_controls"]["official_runtime_promotion_implemented"])
        self.assertFalse(report["operator_controls"]["official_mcp_publish_allowed_by_this_plan"])
        self.assertEqual("reapproval-apply-plan-v2", report["execution_contract_version"])
        self.assertTrue(report["execution_plan_id"].startswith("reapproval_apply_"))
        self.assertRegex(report["plan_payload_sha256"], r"^[a-f0-9]{64}$")
        self.assertEqual(report["plan_payload_sha256"], report["idempotency_key_basis"]["plan_payload_sha256"])
        self.assertEqual(["doc-a", "doc-b", "doc-c", "doc-d"], report["idempotency_key_basis"]["affected_document_ids"])
        self.assertIn("reapproval_review_batch_manifest_report", report["idempotency_key_basis"]["source_sha256"])
        requirement_steps = {item["step"] for item in report["execution_requirements"]}
        self.assertIn("enforce_tenant_and_operator_access", requirement_steps)
        self.assertIn("use_shared_review_workflow_contract", requirement_steps)
        self.assertIn("validate_approval_preconditions", requirement_steps)
        self.assertIn("validate_rejection_decision_contract", requirement_steps)
        self.assertIn("run_preapproval_security_scan", requirement_steps)
        self.assertIn("acknowledge_review_attention_flags", requirement_steps)
        self.assertIn("record_apply_audit_event", requirement_steps)
        self.assertIn("keep_reindex_as_explicit_phase", requirement_steps)
        self.assertIn("rerun_mcp_visibility_gate", requirement_steps)
        self.assertIn("perform_dry_run_before_mutation", requirement_steps)
        self.assertIn("require_explicit_apply_confirmation", requirement_steps)
        self.assertIn("read-only", report["safety_note"])
        summary = report["summary"]
        self.assertEqual(4, summary["batch_count"])
        self.assertEqual(["doc-a", "doc-b", "doc-c"], summary["reindex_required_document_ids"])
        self.assertEqual(2, summary["approve_chunk_count"])
        self.assertEqual(2, summary["reject_chunk_count"])
        self.assertEqual(1, summary["reprocess_chunk_count"])
        self.assertEqual(1, summary["defer_chunk_count"])
        self.assertEqual({"approve": 1, "mixed": 1, "needs_reprocess": 1, "reject": 1}, summary["planned_operation_counts"])
        by_id = {plan["reapproval_batch_id"]: plan for plan in report["batch_plans"]}
        self.assertEqual(["chunk-1", "chunk-2"], by_id["batch-a"]["approve_chunk_ids"])
        self.assertTrue(by_id["batch-a"]["apply_controls"]["requires_tenant_and_operator_access_control"])
        self.assertTrue(by_id["batch-a"]["apply_controls"]["approval_requires_precondition_validation"])
        self.assertTrue(by_id["batch-a"]["apply_controls"]["approval_requires_preapproval_security_scan"])
        self.assertTrue(by_id["batch-a"]["apply_controls"]["approval_recalculates_approved_content_hash"])
        self.assertTrue(by_id["batch-a"]["apply_controls"]["requires_apply_audit_event"])
        self.assertTrue(by_id["batch-a"]["apply_controls"]["requires_explicit_reindex_phase"])
        self.assertTrue(by_id["batch-a"]["apply_controls"]["conditional_vector_sync_allowed_only_after_successful_index"])
        self.assertFalse(by_id["batch-a"]["apply_controls"]["direct_metadata_write_allowed"])
        self.assertEqual(["chunk-4"], by_id["batch-c"]["reject_chunk_ids"])
        self.assertTrue(by_id["batch-c"]["apply_controls"]["rejection_clears_approval_fields"])
        self.assertTrue(by_id["batch-c"]["apply_controls"]["rejection_requires_reason_validation"])
        self.assertEqual(["chunk-5"], by_id["batch-c"]["defer_chunk_ids"])
        self.assertFalse(by_id["batch-d"]["requires_reindex"])
        self.assertTrue(by_id["batch-d"]["apply_controls"]["requires_reprocess_queue"])
        self.assertFalse(by_id["batch-d"]["apply_controls"]["requires_tenant_and_operator_access_control"])
        self.assertFalse(by_id["batch-d"]["apply_controls"]["requires_shared_review_workflow_contract"])
        self.assertFalse(by_id["batch-d"]["apply_controls"]["approval_requires_precondition_validation"])

    def test_execution_plan_id_is_stable_for_same_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_manifest(root / "batches.json", [_batch("batch-a", "doc-a", ["chunk-1"])])
            decisions = root / "decisions.csv"
            _write_decisions(decisions, [_decision("batch-a", "approve_all_reviewed")])
            validation = _write_validation(root / "validation.json", manifest, decisions, expected_batch_count=1)

            first = build_reapproval_apply_plan(
                reapproval_review_batch_report=manifest,
                decision_template_csv=decisions,
                decision_validation_report=validation,
            )
            second = build_reapproval_apply_plan(
                reapproval_review_batch_report=manifest,
                decision_template_csv=decisions,
                decision_validation_report=validation,
            )

        self.assertEqual(first["execution_plan_id"], second["execution_plan_id"])
        self.assertEqual(first["plan_payload_sha256"], second["plan_payload_sha256"])

    def test_no_reapproval_batches_builds_passing_noop_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_manifest(root / "batches.json", [])
            decisions = root / "decisions.csv"
            _write_decisions(decisions, [])
            validation = _write_validation(
                root / "validation.json",
                manifest,
                decisions,
                expected_batch_count=0,
                release_gate_status="no_reapproval_batches",
            )

            report = build_reapproval_apply_plan(
                reapproval_review_batch_report=manifest,
                decision_template_csv=decisions,
                decision_validation_report=validation,
            )

        self.assertTrue(report["passed"])
        self.assertEqual("ready_for_apply_execution", report["release_gate_status"])
        self.assertEqual(0, report["blocker_count"])
        self.assertEqual(0, report["summary"]["batch_count"])
        self.assertEqual(0, report["summary"]["approve_chunk_count"])
        self.assertEqual(0, report["summary"]["reject_chunk_count"])
        self.assertEqual(0, report["summary"]["reprocess_chunk_count"])
        self.assertEqual(0, report["summary"]["defer_chunk_count"])
        self.assertEqual([], report["batch_plans"])
        self.assertEqual([], report["idempotency_key_basis"]["affected_document_ids"])
        self.assertEqual("no_reapproval_batches", report["validation_summary"]["release_gate_status"])

    def test_blocks_when_decision_validation_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_manifest(root / "batches.json", [_batch("batch-a", "doc-a", ["chunk-1"])])
            decisions = root / "decisions.csv"
            _write_decisions(decisions, [_decision("batch-a", "")])
            validation = _write_validation(
                root / "validation.json",
                manifest,
                decisions,
                expected_batch_count=1,
                passed=False,
                release_gate_status="blocked_pending_operator_decisions",
                blocking_count=1,
                complete_row_count=0,
                blank_or_incomplete_row_count=1,
            )

            report = build_reapproval_apply_plan(
                reapproval_review_batch_report=manifest,
                decision_template_csv=decisions,
                decision_validation_report=validation,
            )

        self.assertFalse(report["passed"])
        self.assertEqual("blocked_pending_apply_preflight", report["release_gate_status"])
        codes = {item["code"] for item in report["blockers"]}
        self.assertIn("decision-validation-not-ready", codes)
        self.assertIn("decision-validation-incomplete-rows", codes)

    def test_blocks_mismatched_validation_source_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_manifest(root / "batches.json", [_batch("batch-a", "doc-a", ["chunk-1"])])
            decisions = root / "decisions.csv"
            _write_decisions(decisions, [_decision("batch-a", "approve_all_reviewed")])
            validation = _write_validation(root / "validation.json", manifest, decisions, expected_batch_count=1)
            _write_decisions(decisions, [_decision("batch-a", "reject_all")])

            report = build_reapproval_apply_plan(
                reapproval_review_batch_report=manifest,
                decision_template_csv=decisions,
                decision_validation_report=validation,
            )

        self.assertFalse(report["passed"])
        self.assertIn("decision-validation-source-mismatch", {item["code"] for item in report["blockers"]})

    def test_blocks_failed_manifest_even_when_validation_claims_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_manifest(
                root / "batches.json",
                [_batch("batch-a", "doc-a", ["chunk-1"])],
                passed=False,
                blocker_count=1,
            )
            decisions = root / "decisions.csv"
            _write_decisions(decisions, [_decision("batch-a", "approve_all_reviewed")])
            validation = _write_validation(root / "validation.json", manifest, decisions, expected_batch_count=1)

            report = build_reapproval_apply_plan(
                reapproval_review_batch_report=manifest,
                decision_template_csv=decisions,
                decision_validation_report=validation,
            )

        self.assertFalse(report["passed"])
        self.assertEqual("blocked_pending_apply_preflight", report["release_gate_status"])
        self.assertIn("reapproval-review-batch-manifest-not-ready", {item["code"] for item in report["blockers"]})

    def test_blocks_manifest_batch_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_manifest(
                root / "batches.json",
                [_batch("batch-a", "doc-a", ["chunk-1"])],
                reported_batch_count=2,
            )
            decisions = root / "decisions.csv"
            _write_decisions(decisions, [_decision("batch-a", "approve_all_reviewed")])
            validation = _write_validation(root / "validation.json", manifest, decisions, expected_batch_count=1)

            report = build_reapproval_apply_plan(
                reapproval_review_batch_report=manifest,
                decision_template_csv=decisions,
                decision_validation_report=validation,
            )

        self.assertFalse(report["passed"])
        self.assertIn("reapproval-review-batch-count-mismatch", {item["code"] for item in report["blockers"]})

    def test_blocks_manifest_without_review_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = _batch("batch-a", "doc-a", ["chunk-1"])
            batch["chunks"][0].pop("review_content_hash")
            manifest = _write_manifest(root / "batches.json", [batch])
            decisions = root / "decisions.csv"
            _write_decisions(decisions, [_decision("batch-a", "approve_all_reviewed")])
            validation = _write_validation(root / "validation.json", manifest, decisions, expected_batch_count=1)

            report = build_reapproval_apply_plan(
                reapproval_review_batch_report=manifest,
                decision_template_csv=decisions,
                decision_validation_report=validation,
            )

        self.assertFalse(report["passed"])
        self.assertIn(
            "reapproval-review-content-hash-missing-or-invalid",
            {item["code"] for item in report["blockers"]},
        )

    def test_blocks_manifest_without_tenant_runtime_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_manifest(root / "batches.json", [_batch("batch-a", "doc-a", ["chunk-1"])])
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["worklist_report"] = {}
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            decisions = root / "decisions.csv"
            _write_decisions(decisions, [_decision("batch-a", "approve_all_reviewed")])
            validation = _write_validation(root / "validation.json", manifest, decisions, expected_batch_count=1)

            report = build_reapproval_apply_plan(
                reapproval_review_batch_report=manifest,
                decision_template_csv=decisions,
                decision_validation_report=validation,
            )

        self.assertFalse(report["passed"])
        codes = {item["code"] for item in report["blockers"]}
        self.assertIn("reapproval-review-manifest-tenant-id-missing", codes)
        self.assertIn("reapproval-review-manifest-effective-data-dir-missing", codes)

    def test_blocks_invalid_manifest_batch_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_manifest(
                root / "batches.json",
                [_batch("batch-a", "doc-a", ["chunk-1"])],
                reported_batch_count="not-a-number",
            )
            decisions = root / "decisions.csv"
            _write_decisions(decisions, [_decision("batch-a", "approve_all_reviewed")])
            validation = _write_validation(root / "validation.json", manifest, decisions, expected_batch_count=1)

            report = build_reapproval_apply_plan(
                reapproval_review_batch_report=manifest,
                decision_template_csv=decisions,
                decision_validation_report=validation,
            )

        self.assertFalse(report["passed"])
        self.assertIn("reapproval-review-batch-count-missing-or-invalid", {item["code"] for item in report["blockers"]})

    def test_cli_writes_plan_and_returns_nonzero_on_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_manifest(root / "batches.json", [_batch("batch-a", "doc-a", ["chunk-1"])])
            decisions = root / "decisions.csv"
            out_json = root / "plan.json"
            out_md = root / "plan.md"
            _write_decisions(decisions, [_decision("batch-a", "")])
            validation = _write_validation(
                root / "validation.json",
                manifest,
                decisions,
                expected_batch_count=1,
                passed=False,
                release_gate_status="blocked_pending_operator_decisions",
                blocking_count=1,
                complete_row_count=0,
                blank_or_incomplete_row_count=1,
            )

            exit_code = main(
                [
                    "--reapproval-review-batch-report",
                    str(manifest),
                    "--decision-template-csv",
                    str(decisions),
                    "--decision-validation-report",
                    str(validation),
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                    "--fail-on-blocker",
                ],
                stdout=io.StringIO(),
            )
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            markdown = out_md.read_text(encoding="utf-8")
            out_md_exists = out_md.is_file()

        self.assertEqual(2, exit_code)
        self.assertEqual("reapproval_apply_plan", payload["report_type"])
        self.assertFalse(payload["passed"])
        self.assertTrue(out_md_exists)
        self.assertIn("## Execution Requirements", markdown)
        self.assertIn("use_shared_review_workflow_contract", markdown)


def _write_manifest(
    path: Path,
    batches: list[dict[str, object]],
    *,
    passed: bool = True,
    blocker_count: int = 0,
    reported_batch_count: object | None = None,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "report_type": "reapproval_review_batch_manifest",
                "generated_at": "2026-07-10T00:00:00+00:00",
                "passed": passed,
                "blocker_count": blocker_count,
                "batch_count": len(batches) if reported_batch_count is None else reported_batch_count,
                "worklist_report": {
                    "tenant_id": "tenant-a",
                    "effective_data_dir": "data/tenant-a",
                },
                "batches": batches,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _batch(batch_id: str, document_id: str, chunk_ids: list[str]) -> dict[str, object]:
    return {
        "reapproval_batch_id": batch_id,
        "document_id": document_id,
        "document_name": f"{document_id} name",
        "filename": f"{document_id}.pdf",
        "suggested_action": "reapprove_and_reindex",
        "review_risk_tier": "medium",
        "chunk_count": len(chunk_ids),
        "chunk_ids": chunk_ids,
        "chunks": [
            {
                "chunk_id": chunk_id,
                "approval_id": f"approval-{chunk_id}",
                "approved_content_hash_short": hashlib.sha256(
                    f"approved-{chunk_id}".encode("utf-8")
                ).hexdigest()[:12],
                "review_content_hash": hashlib.sha256(f"review-{chunk_id}".encode("utf-8")).hexdigest(),
            }
            for chunk_id in chunk_ids
        ],
        "reapproval_batch_chunk_fingerprint": hashlib.sha256("|".join(chunk_ids).encode("utf-8")).hexdigest(),
        "worklist_report_path": "reports/reapproval_worklist.json",
        "worklist_report_sha256": "worklist-report-sha",
        "worklist_chunks_path": "reports/reapproval_worklist_chunks.json",
        "worklist_chunks_sha256": "worklist-chunks-sha",
    }


def _write_decisions(path: Path, rows: list[dict[str, str]]) -> None:
    fields = [
        "batch_rank",
        "reapproval_batch_id",
        "operator_decision",
        "reviewer_id",
        "reviewed_at",
        "decision_notes",
        "chunk_decision_overrides_json",
        "approval_scope_confirmation",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _decision(batch_id: str, decision: str, *, overrides: str = "[]") -> dict[str, str]:
    return {
        "batch_rank": "1",
        "reapproval_batch_id": batch_id,
        "operator_decision": decision,
        "reviewer_id": "reviewer-a" if decision else "",
        "reviewed_at": "2026-07-10T09:00:00+09:00" if decision else "",
        "decision_notes": "",
        "chunk_decision_overrides_json": overrides,
        "approval_scope_confirmation": "confirmed" if decision else "",
    }


def _write_validation(
    path: Path,
    manifest: Path,
    decisions: Path,
    *,
    expected_batch_count: int,
    passed: bool = True,
    release_gate_status: str = "ready_for_reapproval_apply",
    blocking_count: int = 0,
    complete_row_count: int | None = None,
    blank_or_incomplete_row_count: int = 0,
) -> Path:
    complete = expected_batch_count if complete_row_count is None else complete_row_count
    path.write_text(
        json.dumps(
            {
                "report_type": "reapproval_decision_validation",
                "generated_at": "2026-07-10T00:01:00+00:00",
                "passed": passed,
                "release_gate_status": release_gate_status,
                "blocking_count": blocking_count,
                "expected_batch_count": expected_batch_count,
                "decision_row_count": expected_batch_count,
                "complete_row_count": complete,
                "blank_or_incomplete_row_count": blank_or_incomplete_row_count,
                "source_report_artifacts": [
                    _artifact("reapproval_review_batch_manifest_report", manifest),
                    _artifact("reapproval_decision_template_csv", decisions),
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _artifact(role: str, path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    return {"role": role, "path": str(path), "byte_count": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


if __name__ == "__main__":
    unittest.main()
