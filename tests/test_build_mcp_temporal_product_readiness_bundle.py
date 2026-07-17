from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_mcp_temporal_product_readiness_bundle import (
    build_mcp_temporal_product_readiness_bundle,
    main,
)


class BuildMcpTemporalProductReadinessBundleTests(unittest.TestCase):
    def test_builds_temporal_reports_and_threads_them_into_product_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            reports_dir = root / "reports"
            shadow_dir = root / "shadow"
            reapproval_apply_plan_report = root / "reapproval_apply_plan.json"
            _seed_runtime(data_dir)
            _write_reapproval_apply_plan(reapproval_apply_plan_report)

            report = build_mcp_temporal_product_readiness_bundle(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                reports_dir=reports_dir,
                shadow_data_dir=shadow_dir,
                timestamp="20260710-010000",
                reapproval_apply_plan_reports=[reapproval_apply_plan_report],
            )
            product_path = Path(report["artifacts"]["product_readiness_json"])
            product = json.loads(product_path.read_text(encoding="utf-8"))
            temporal_coverage_exists = Path(report["artifacts"]["temporal_coverage_json"]).is_file()
            temporal_backfill_exists = Path(report["artifacts"]["temporal_backfill_shadow_json"]).is_file()
            temporal_ambiguity_exists = Path(report["artifacts"]["temporal_ambiguity_scope_json"]).is_file()
            runtime_drift_exists = Path(report["artifacts"]["runtime_version_drift_json"]).is_file()
            product_exists = product_path.is_file()

        self.assertEqual("mcp_temporal_product_readiness_bundle", report["report_type"])
        self.assertTrue(report["passed"])
        self.assertTrue(temporal_coverage_exists)
        self.assertTrue(temporal_backfill_exists)
        self.assertTrue(temporal_ambiguity_exists)
        self.assertTrue(runtime_drift_exists)
        self.assertTrue(product_exists)
        self.assertEqual(
            report["artifacts"]["temporal_coverage_json"],
            product["source_reports"]["temporal_coverage_report"],
        )
        self.assertEqual(
            report["artifacts"]["temporal_backfill_shadow_json"],
            product["source_reports"]["temporal_backfill_shadow_report"],
        )
        self.assertEqual(
            report["artifacts"]["temporal_ambiguity_scope_json"],
            product["source_reports"]["temporal_ambiguity_scope_report"],
        )
        self.assertEqual(
            report["artifacts"]["runtime_version_drift_json"],
            product["source_reports"]["runtime_version_drift_report"],
        )
        self.assertEqual(
            [str(reapproval_apply_plan_report)],
            product["source_reports"]["reapproval_apply_plan_reports"],
        )
        roles = {artifact["role"] for artifact in product["source_report_artifacts"]}
        self.assertIn("temporal_coverage_report", roles)
        self.assertIn("temporal_backfill_shadow_report", roles)
        self.assertIn("temporal_ambiguity_scope_report", roles)
        self.assertIn("runtime_version_drift_report", roles)
        self.assertIn("reapproval_apply_plan_report", roles)
        self.assertEqual(4, product["temporal_evidence_guard_summary"]["source_count"])
        self.assertEqual(0, product["reapproval_apply_plan_summary"]["unsafe_contract_violation_count"])
        self.assertEqual(0, report["summary"]["reapproval_apply_plan_unsafe_contract_violation_count"])
        self.assertFalse(report["summary"]["shadow_runtime_runnable"])
        self.assertEqual(1, report["summary"]["shadow_vector_record_count"])
        self.assertTrue(report["summary"]["shadow_vector_projection_ready"])
        self.assertIn("does not approve chunks", report["safety_note"])

    def test_empty_shadow_vector_projection_blocks_bundle_and_product_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            reports_dir = root / "reports"
            _seed_runtime(data_dir)
            chunk_path = data_dir / "repository" / "doc-rule_chunks.json"
            chunks = json.loads(chunk_path.read_text(encoding="utf-8"))
            chunks[0]["metadata"].pop("tenant_id")
            chunk_path.write_text(json.dumps(chunks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            report = build_mcp_temporal_product_readiness_bundle(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                reports_dir=reports_dir,
                shadow_data_dir=root / "shadow",
                timestamp="20260710-020000",
            )
            product = json.loads(
                Path(report["artifacts"]["product_readiness_json"]).read_text(encoding="utf-8")
            )

        self.assertFalse(report["passed"])
        self.assertEqual("blocked", report["status"])
        self.assertIn(
            "temporal-backfill-vector-projection-empty",
            {blocker["code"] for blocker in report["blockers"]},
        )
        self.assertEqual(0, report["summary"]["shadow_vector_record_count"])
        self.assertFalse(report["summary"]["shadow_vector_projection_ready"])
        self.assertFalse(report["summary"]["shadow_runtime_runnable"])
        self.assertIn("temporal-backfill-vector-projection-empty", product["blocking_codes"])

    def test_cli_writes_bundle_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            out_json = root / "bundle.json"
            out_md = root / "bundle.md"
            reapproval_apply_plan_report = root / "reapproval_apply_plan.json"
            _seed_runtime(data_dir)
            _write_reapproval_apply_plan(reapproval_apply_plan_report)
            stdout = io.StringIO()

            exit_code = main(
                [
                    "--runtime-data-dir",
                    str(data_dir),
                    "--flat-storage",
                    "--reports-dir",
                    str(root / "reports"),
                    "--shadow-data-dir",
                    str(root / "shadow"),
                    "--timestamp",
                    "20260710-010000",
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                    "--reapproval-apply-plan-report",
                    str(reapproval_apply_plan_report),
                ],
                stdout=stdout,
            )
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            product = json.loads(Path(payload["artifacts"]["product_readiness_json"]).read_text(encoding="utf-8"))
            markdown = out_md.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertEqual("mcp_temporal_product_readiness_bundle", payload["report_type"])
        self.assertIn("MCP Temporal Product Readiness Bundle", markdown)
        self.assertIn("product_readiness_json", payload["artifacts"])
        self.assertEqual(
            [str(reapproval_apply_plan_report)],
            product["source_reports"]["reapproval_apply_plan_reports"],
        )
        self.assertEqual(0, product["reapproval_apply_plan_summary"]["unsafe_contract_violation_count"])
        self.assertIn('"mcp_temporal_product_readiness_bundle"', stdout.getvalue())


def _seed_runtime(data_dir: Path) -> None:
    repository_dir = data_dir / "repository"
    vector_dir = data_dir / "vector_db" / "default"
    repository_dir.mkdir(parents=True)
    vector_dir.mkdir(parents=True)
    chunk = {
        "document_id": "doc-rule",
        "chunk_id": "chunk-1",
        "chunk_type": "article",
        "text": "Article 1. A reviewed regulation chunk with temporal metadata.",
        "retrieval_text": "Article 1. A reviewed regulation chunk with temporal metadata.",
        "approval_status": "approved",
        "approval_id": "approval-1",
        "approved_content_hash": "approved-hash-1",
        "security_level": "internal",
        "metadata": {
            "tenant_id": "default",
            "document_id": "doc-rule",
            "document_name": "Public Institution Rule",
            "chunk_id": "chunk-1",
            "chunk_type": "article",
            "institution_name": "Test Institution",
            "profile_id": "test-profile",
            "source_system": "unit-test",
            "source_url": "https://example.test/rule",
            "regulation_title": "Test Rule",
            "article_no": "Article 1",
            "article_title": "Purpose",
            "source_page_start": 1,
            "security_level": "internal",
            "approval_status": "approved",
            "approval_id": "approval-1",
            "approved_content_hash": "approved-hash-1",
            "effective_date": "2026-01-01",
            "revision_date": "2026-01-01",
        },
    }
    (repository_dir / "doc-rule_chunks.json").write_text(
        json.dumps([chunk], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    record = {
        "id": "doc-rule:chunk-1",
        "document_id": "doc-rule",
        "chunk_id": "chunk-1",
        "text": chunk["retrieval_text"],
        "content_hash": "content-hash-1",
        "metadata": {
            **chunk["metadata"],
            "content_hash": "content-hash-1",
        },
    }
    (vector_dir / "approved_vectors.jsonl").write_text(
        json.dumps(record, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_reapproval_apply_plan(path: Path) -> None:
    required_steps = [
        "load_current_review_chunks",
        "enforce_tenant_and_operator_access",
        "use_shared_review_workflow_contract",
        "validate_approval_preconditions",
        "validate_rejection_decision_contract",
        "run_preapproval_security_scan",
        "acknowledge_review_attention_flags",
        "recalculate_approval_hashes",
        "append_review_journals_and_snapshots",
        "record_apply_audit_event",
        "refresh_exports_and_vector_state",
        "keep_reindex_as_explicit_phase",
        "rerun_mcp_visibility_gate",
    ]
    payload = {
        "report_type": "reapproval_apply_plan",
        "generated_at": "2026-07-09T06:45:00+00:00",
        "passed": True,
        "release_gate_status": "ready_for_apply_execution",
        "blocker_count": 0,
        "summary": {
            "batch_count": 1,
            "approve_chunk_count": 1,
            "reject_chunk_count": 0,
            "reprocess_chunk_count": 0,
            "defer_chunk_count": 0,
        },
        "operator_controls": {
            "auto_approval": False,
            "auto_reindex": False,
            "applies_reapproval_decisions": False,
            "requires_dedicated_apply_step": True,
            "direct_approval_metadata_write_allowed": False,
            "requires_tenant_and_operator_access_control": True,
            "requires_shared_review_workflow_contract": True,
            "requires_approval_precondition_validation": True,
            "requires_rejection_decision_validation": True,
            "requires_preapproval_security_scan": True,
            "requires_review_flag_acknowledgement": True,
            "requires_approved_content_hash_recalculation": True,
            "requires_review_journal_append": True,
            "requires_apply_audit_event": True,
            "requires_export_refresh": True,
            "requires_vector_sync_or_explicit_reindex": True,
            "requires_explicit_reindex_phase_by_default": True,
            "conditional_vector_sync_requires_existing_successful_index": True,
            "official_mcp_publish_allowed_by_this_plan": False,
        },
        "execution_requirements": [{"step": step, "required": True, "detail": step} for step in required_steps],
        "batch_plans": [
            {
                "reapproval_batch_id": "batch-a",
                "document_id": "doc-rule",
                "planned_operation": "approve",
                "approve_chunk_ids": ["chunk-1"],
                "reject_chunk_ids": [],
                "requires_reindex": True,
                "apply_controls": {
                    "direct_metadata_write_allowed": False,
                    "requires_tenant_and_operator_access_control": True,
                    "requires_shared_review_workflow_contract": True,
                    "approval_requires_precondition_validation": True,
                    "approval_requires_preapproval_security_scan": True,
                    "approval_requires_review_flag_acknowledgement_if_attention_present": True,
                    "approval_recalculates_approved_content_hash": True,
                    "rejection_clears_approval_fields": False,
                    "rejection_requires_reason_validation": False,
                    "requires_review_journal_append": True,
                    "requires_apply_audit_event": True,
                    "requires_export_refresh": True,
                    "requires_vector_sync_or_explicit_reindex": True,
                    "requires_explicit_reindex_phase": True,
                    "conditional_vector_sync_allowed_only_after_successful_index": True,
                    "requires_reprocess_queue": False,
                    "official_mcp_publish_allowed_by_batch_plan": False,
                },
            }
        ],
        "safety_note": "This plan is read-only.",
        "api_call_count": 0,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
