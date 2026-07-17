from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_mcp_readiness_authority import build_mcp_readiness_authority


class BuildMcpReadinessAuthorityTests(unittest.TestCase):
    def test_builds_authority_manifest_with_authoritative_sha_and_supersedes_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = root / "reports" / "mcp_product_readiness_current.json"
            demo = root / "reports" / "mcp_demo_answers_current.json"
            transport = root / "reports" / "mcp_transport_smoke_current.json"
            visibility = root / "reports" / "mcp_index_visibility_current.json"
            connection = root / "reports" / "mcp_connection_readiness_current.json"
            old_readiness = root / "reports" / "mcp_product_readiness_old.json"
            out_json = root / "reports" / "mcp_readiness_authority_current.json"
            _write_json(readiness, _product_readiness_payload())
            _write_json(
                demo,
                {
                    "report_type": "mcp_demo_answers",
                    "passed": True,
                    "top_k": 10,
                    "query_count": 2,
                    "query_spec_path": "config/query_specs.json",
                    "query_spec_sha256": "b" * 64,
                    "query_spec_byte_count": 1234,
                    "query_spec_item_count": 2,
                },
            )
            _write_json(transport, {"report_type": "mcp_transport_smoke", "passed": True})
            _write_json(visibility, _index_visibility_payload())
            _write_json(connection, _connection_readiness_payload())
            _write_json(old_readiness, {"report_type": "mcp_product_readiness", "passed": True, "blocking_count": 0})

            report = build_mcp_readiness_authority(
                repo_root=root,
                authoritative_artifacts=[
                    ("product_readiness", Path("reports/mcp_product_readiness_current.json")),
                    ("mcp_demo_answers", Path("reports/mcp_demo_answers_current.json")),
                    ("mcp_transport_smoke", Path("reports/mcp_transport_smoke_current.json")),
                    ("mcp_index_visibility", Path("reports/mcp_index_visibility_current.json")),
                    ("mcp_connection_readiness", Path("reports/mcp_connection_readiness_current.json")),
                ],
                supersedes=[
                    (Path("reports/mcp_product_readiness_old.json"), "replaced by fingerprinted top10 readiness"),
                ],
                out_json=out_json,
            )
            expected_readiness_sha = hashlib.sha256(readiness.read_bytes()).hexdigest()
            out_json_exists = out_json.is_file()

        by_role = {artifact["role"]: artifact for artifact in report["authoritative_artifacts"]}
        self.assertTrue(report["passed"])
        self.assertEqual(0, report["blocking_count"])
        self.assertEqual("mcp_readiness_authority", report["report_type"])
        self.assertEqual(
            expected_readiness_sha,
            by_role["product_readiness"]["sha256"],
        )
        self.assertEqual(
            5,
            by_role["product_readiness"]["product_readiness_contract"]["source_report_artifact_count"],
        )
        self.assertIn(
            "reapproval_apply_plan_report",
            by_role["product_readiness"]["product_readiness_contract"]["source_report_roles"],
        )
        demo_summary = by_role["mcp_demo_answers"]["json_summary"]
        connection_summary = by_role["mcp_connection_readiness"]["json_summary"]
        product_summary = by_role["product_readiness"]["json_summary"]
        self.assertEqual(10, demo_summary["top_k"])
        self.assertEqual("config/query_specs.json", demo_summary["query_spec_path"])
        self.assertEqual("b" * 64, demo_summary["query_spec_sha256"])
        self.assertEqual(2, demo_summary["query_spec_item_count"])
        self.assertEqual(
            5997,
            connection_summary["mcp_index_visibility_approval_journal_coverage_matched_record_count"],
        )
        self.assertEqual(
            0,
            connection_summary["mcp_index_visibility_approval_journal_coverage_missing_record_count"],
        )
        self.assertEqual(1174, product_summary["temporal_coverage_summary_with_temporal_metadata_count"])
        self.assertEqual(410, product_summary["temporal_backfill_shadow_summary_delta_temporal_metadata_count"])
        self.assertEqual(4451, product_summary["temporal_backfill_shadow_summary_ambiguous_chunk_count"])
        self.assertEqual(4451, product_summary["temporal_ambiguity_scope_summary_ambiguous_chunk_count"])
        self.assertEqual(2, product_summary["temporal_ambiguity_scope_summary_blocking_decision_count"])
        self.assertEqual(1, product_summary["temporal_evidence_guard_summary_stale_artifact_count"])
        self.assertEqual(2, product_summary["temporal_evidence_guard_summary_runtime_lineage_mismatch_count"])
        self.assertTrue(product_summary["temporal_evidence_guard_summary_strict_temporal_evidence"])
        self.assertEqual(25, product_summary["revision_impact_summary_approval_required_count"])
        self.assertEqual(3, product_summary["revision_impact_summary_metadata_only_changed_count"])
        self.assertEqual(5997, product_summary["runtime_version_drift_summary_approved_repository_stale_chunker_count"])
        self.assertEqual(0, product_summary["runtime_version_drift_summary_vector_integrity_failure_count"])
        self.assertTrue(product_summary["runtime_version_drift_summary_reprocess_requires_reapproval"])
        self.assertEqual(69, product_summary["approval_workload_summary_manual_attention_chunks"])
        self.assertEqual(1153, product_summary["approval_workload_summary_low_risk_batch_review_candidate_chunks"])
        self.assertEqual(18, product_summary["approval_review_batch_summary_batch_count"])
        self.assertEqual(1222, product_summary["approval_review_batch_summary_approval_chunk_count"])
        self.assertEqual(419, product_summary["reapproval_workload_summary_recommended_initial_review_chunks"])
        self.assertEqual(0.9301, product_summary["reapproval_workload_summary_initial_review_reduction_ratio"])
        self.assertEqual(60, product_summary["reapproval_review_batch_summary_batch_count"])
        self.assertEqual(5997, product_summary["reapproval_review_batch_summary_selected_candidate_count"])
        self.assertEqual(60, product_summary["reapproval_decision_validation_summary_complete_row_count"])
        self.assertEqual(1, product_summary["reapproval_apply_plan_summary_ready_plan_count"])
        self.assertEqual(0, product_summary["reapproval_apply_plan_summary_unsafe_contract_violation_count"])
        self.assertEqual("replaced by fingerprinted top10 readiness", report["supersedes"][0]["reason"])
        self.assertTrue(out_json_exists)

    def test_missing_product_readiness_source_fingerprints_block_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = root / "reports" / "mcp_product_readiness_current.json"
            payload = _product_readiness_payload()
            payload.pop("source_report_artifacts")
            payload.pop("source_report_artifact_summary")
            _write_json(readiness, payload)

            report = build_mcp_readiness_authority(
                repo_root=root,
                authoritative_artifacts=[
                    ("product_readiness", Path("reports/mcp_product_readiness_current.json")),
                ],
            )

        self.assertFalse(report["passed"])
        self.assertIn(
            "product-readiness-source-fingerprints-missing",
            {item["code"] for item in report["findings"]},
        )
        self.assertIn(
            "authority-required-roles-missing",
            {item["code"] for item in report["findings"]},
        )

    def test_reapproval_summary_requires_matching_source_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = root / "reports" / "mcp_product_readiness_current.json"
            payload = _product_readiness_payload()
            payload["source_report_artifacts"] = payload["source_report_artifacts"][:1]
            payload["source_report_artifact_summary"] = {
                "provided_count": 1,
                "sha256_count": 1,
                "payload_generated_at_count": 1,
            }
            _write_json(readiness, payload)

            report = build_mcp_readiness_authority(
                repo_root=root,
                authoritative_artifacts=[
                    ("product_readiness", Path("reports/mcp_product_readiness_current.json")),
                ],
            )

        findings = {item["code"]: item for item in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("product-readiness-reapproval-source-roles-missing", findings)
        self.assertEqual(
            [
                "reapproval_apply_plan_report",
                "reapproval_decision_validation_report",
                "reapproval_review_batch_manifest_report",
                "reapproval_worklist_report",
            ],
            findings["product-readiness-reapproval-source-roles-missing"]["roles"],
        )

    def test_required_roles_must_point_to_expected_report_types_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = root / "reports" / "mcp_product_readiness_current.json"
            transport = root / "reports" / "mcp_transport_smoke_current.json"
            visibility = root / "reports" / "mcp_index_visibility_current.json"
            connection = root / "reports" / "mcp_connection_readiness_current.json"
            _write_json(readiness, _product_readiness_payload())
            _write_json(transport, {"report_type": "mcp_transport_smoke", "passed": True})
            _write_json(visibility, _index_visibility_payload())
            _write_json(connection, _connection_readiness_payload())

            report = build_mcp_readiness_authority(
                repo_root=root,
                authoritative_artifacts=[
                    ("product_readiness", Path("reports/mcp_product_readiness_current.json")),
                    ("mcp_demo_answers", Path("reports/mcp_product_readiness_current.json")),
                    ("mcp_transport_smoke", Path("reports/mcp_transport_smoke_current.json")),
                    ("mcp_index_visibility", Path("reports/mcp_index_visibility_current.json")),
                    ("mcp_connection_readiness", Path("reports/mcp_connection_readiness_current.json")),
                ],
            )

        self.assertFalse(report["passed"])
        codes = {item["code"] for item in report["findings"]}
        self.assertIn("authority-artifact-role-path", codes)
        self.assertIn("authority-artifact-role-report-type", codes)

    def test_superseded_artifacts_need_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = root / "reports" / "mcp_product_readiness_current.json"
            old_readiness = root / "reports" / "mcp_product_readiness_old.json"
            _write_json(readiness, _product_readiness_payload())
            _write_json(old_readiness, {"report_type": "mcp_product_readiness", "passed": True, "blocking_count": 0})

            report = build_mcp_readiness_authority(
                repo_root=root,
                authoritative_artifacts=[
                    ("product_readiness", Path("reports/mcp_product_readiness_current.json")),
                ],
                supersedes=[(Path("reports/mcp_product_readiness_old.json"), "")],
            )

        self.assertFalse(report["passed"])
        self.assertIn("superseded-reason-missing", {item["code"] for item in report["findings"]})

    def test_approval_journal_gap_blocks_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = root / "reports" / "mcp_product_readiness_current.json"
            demo = root / "reports" / "mcp_demo_answers_current.json"
            transport = root / "reports" / "mcp_transport_smoke_current.json"
            visibility = root / "reports" / "mcp_index_visibility_current.json"
            connection = root / "reports" / "mcp_connection_readiness_current.json"
            visibility_payload = _index_visibility_payload()
            visibility_payload["approval_journal_coverage"]["matched_record_count"] = 5996
            visibility_payload["approval_journal_coverage"]["missing_record_count"] = 1
            _write_json(readiness, _product_readiness_payload())
            _write_json(demo, {"report_type": "mcp_demo_answers", "passed": True})
            _write_json(transport, {"report_type": "mcp_transport_smoke", "passed": True})
            _write_json(visibility, visibility_payload)
            _write_json(connection, _connection_readiness_payload())

            report = build_mcp_readiness_authority(
                repo_root=root,
                authoritative_artifacts=[
                    ("product_readiness", Path("reports/mcp_product_readiness_current.json")),
                    ("mcp_demo_answers", Path("reports/mcp_demo_answers_current.json")),
                    ("mcp_transport_smoke", Path("reports/mcp_transport_smoke_current.json")),
                    ("mcp_index_visibility", Path("reports/mcp_index_visibility_current.json")),
                    ("mcp_connection_readiness", Path("reports/mcp_connection_readiness_current.json")),
                ],
            )

        findings = [item for item in report["findings"] if item["code"] == "authority-approval-journal-coverage-incomplete"]
        self.assertFalse(report["passed"])
        self.assertTrue(findings)
        self.assertEqual("mcp_index_visibility", findings[0]["role"])
        self.assertEqual(1, findings[0]["missing_record_count"])


def _product_readiness_payload() -> dict:
    return {
        "report_type": "mcp_product_readiness",
        "generated_at": "2026-07-09T11:30:05+00:00",
        "passed": True,
        "blocking_count": 0,
        "warning_count": 2,
        "tenant_id": "tenant-aks-publish",
        "source_report_artifacts": [
            {
                "role": "batch_report",
                "path": "reports/batch_quality.json",
                "byte_count": 123,
                "sha256": "a" * 64,
                "modified_at": "2026-07-09T10:00:00+00:00",
                "payload_generated_at": "2026-07-09T09:00:00+00:00",
            },
            {
                "role": "reapproval_worklist_report",
                "path": "reports/reapproval_worklist_current.json",
                "byte_count": 234,
                "sha256": "b" * 64,
                "modified_at": "2026-07-09T10:01:00+00:00",
                "payload_generated_at": "2026-07-09T09:01:00+00:00",
            },
            {
                "role": "reapproval_review_batch_manifest_report",
                "path": "reports/reapproval_review_batches_current.json",
                "byte_count": 345,
                "sha256": "c" * 64,
                "modified_at": "2026-07-09T10:02:00+00:00",
                "payload_generated_at": "2026-07-09T09:02:00+00:00",
            },
            {
                "role": "reapproval_decision_validation_report",
                "path": "reports/reapproval_decision_validation_current.json",
                "byte_count": 456,
                "sha256": "d" * 64,
                "modified_at": "2026-07-09T10:03:00+00:00",
                "payload_generated_at": "2026-07-09T09:03:00+00:00",
            },
            {
                "role": "reapproval_apply_plan_report",
                "path": "reports/reapproval_apply_plan_current.json",
                "byte_count": 567,
                "sha256": "e" * 64,
                "modified_at": "2026-07-09T10:04:00+00:00",
                "payload_generated_at": "2026-07-09T09:04:00+00:00",
            },
        ],
        "source_report_artifact_summary": {
            "provided_count": 5,
            "sha256_count": 5,
            "payload_generated_at_count": 5,
        },
        "temporal_coverage_summary": {
            "record_count": 5997,
            "with_temporal_metadata_count": 1174,
            "without_temporal_metadata_count": 4823,
            "temporal_metadata_ratio": 0.1958,
            "candidate_missing_record_count": 4735,
        },
        "temporal_backfill_shadow_summary": {
            "delta_temporal_metadata_count": 410,
            "after_temporal_metadata_ratio": 0.2641,
            "conflict_chunk_count": 4451,
            "ambiguous_chunk_count": 4451,
            "write_blocked": True,
            "shadow_runtime_written": False,
        },
        "temporal_ambiguity_scope_summary": {
            "status": "temporal_ambiguity_policy_required",
            "ambiguous_chunk_count": 4451,
            "ambiguous_chunk_ratio": 0.7422,
            "vector_record_count": 5997,
            "ambiguous_record_count": 4451,
            "review_slice_count": 8,
            "blocking_decision_count": 2,
        },
        "temporal_evidence_guard_summary": {
            "source_count": 4,
            "stale_artifact_count": 1,
            "payload_generated_at_span_hours": 26.5,
            "payload_generated_at_span_exceeds_threshold": True,
            "runtime_lineage_mismatch_count": 2,
            "runtime_lineage_value_count": 6,
            "strict_temporal_evidence": True,
            "passed": False,
        },
        "revision_impact_summary": {
            "report_count": 1,
            "before_unit_count": 6000,
            "after_unit_count": 6005,
            "changed_count": 12,
            "added_count": 9,
            "removed_count": 4,
            "metadata_only_changed_count": 3,
            "approval_required_count": 25,
            "approval_reuse_candidate_count": 5980,
            "deindex_required_count": 4,
        },
        "runtime_version_drift_summary": {
            "current_chunker_version": "0.1.5",
            "approved_repository_stale_chunker_count": 5997,
            "vector_stale_chunker_count": 5997,
            "vector_integrity_failure_count": 0,
            "vector_integrity_content_hash_mismatch_count": 0,
            "vector_integrity_verification_hash_mismatch_count": 0,
            "vector_integrity_metadata_missing_required_count": 0,
            "vector_integrity_invalid_approval_status_count": 0,
            "vector_integrity_invalid_security_level_count": 0,
            "vector_integrity_embedded_dimension_mismatch_count": 0,
            "vector_integrity_embedded_failure_count": 0,
            "vector_integrity_local_path_leak_count": 0,
            "reprocess_requires_reapproval": True,
            "approved_chunks_with_approved_hash_count": 5997,
        },
        "approval_workload_summary": {
            "report_count": 1,
            "document_count": 5,
            "total_chunks": 1222,
            "manual_attention_chunks": 69,
            "manual_attention_rate": 5.65,
            "low_risk_batch_review_candidate_chunks": 1153,
            "low_risk_batch_review_candidate_rate": 94.35,
            "blocking_review_chunks": 47,
            "domain_attention_chunks": 22,
        },
        "approval_review_batch_summary": {
            "report_count": 1,
            "batch_count": 18,
            "approval_chunk_count": 1222,
            "manual_attention_chunks": 69,
            "low_risk_batch_review_candidate_chunks": 1153,
            "blocker_count": 0,
            "warning_count": 0,
        },
        "reapproval_workload_summary": {
            "report_count": 1,
            "document_count": 199,
            "reapproval_candidate_chunks": 5997,
            "high_risk_candidate_chunks": 0,
            "temporal_sample_candidate_chunks": 1174,
            "low_risk_candidate_chunks": 4823,
            "recommended_initial_review_chunks": 419,
            "estimated_initial_review_minutes": 140,
            "source_vector_integrity_failure_count": 0,
            "pre_reapproval_blocker_count": 0,
            "initial_review_reduction_ratio": 0.9301,
        },
        "reapproval_review_batch_summary": {
            "report_count": 1,
            "candidate_count": 5997,
            "selected_candidate_count": 5997,
            "batch_count": 60,
            "reapproval_chunk_count": 5997,
            "blocker_count": 0,
            "warning_count": 0,
            "max_chunks_per_batch": 100,
            "risk_tier_chunk_counts": {"high": 5997},
            "action_chunk_counts": {"reprocess_then_reapprove_and_reindex": 5997},
        },
        "reapproval_decision_validation_summary": {
            "report_count": 1,
            "expected_batch_count": 60,
            "complete_row_count": 60,
            "blank_or_incomplete_row_count": 0,
            "blocking_count": 0,
            "passed": True,
            "release_gate_status_counts": {"ready_for_reapproval_apply": 1},
            "operator_decision_counts": {"approve": 60},
        },
        "reapproval_apply_plan_summary": {
            "report_count": 1,
            "passed": True,
            "blocker_count": 0,
            "ready_plan_count": 1,
            "batch_count": 60,
            "approve_chunk_count": 5997,
            "reject_chunk_count": 0,
            "reprocess_chunk_count": 5997,
            "defer_chunk_count": 0,
            "batch_apply_control_count": 60,
            "batch_requires_shared_review_workflow_contract_count": 60,
            "batch_requires_explicit_reindex_phase_count": 60,
            "batch_conditional_vector_sync_guard_count": 60,
            "direct_metadata_write_allowed_count": 0,
            "mcp_publish_allowed_count": 0,
            "unsafe_contract_violation_count": 0,
            "release_gate_status_counts": {"ready_for_apply_execution": 1},
            "observed_execution_step_counts": {
                "enforce_tenant_and_operator_access": 1,
                "use_shared_review_workflow_contract": 1,
                "validate_approval_preconditions": 1,
                "validate_rejection_decision_contract": 1,
                "run_preapproval_security_scan": 1,
                "acknowledge_review_attention_flags": 1,
                "recalculate_approval_hashes": 1,
                "append_review_journals_and_snapshots": 1,
                "keep_reindex_as_explicit_phase": 1,
                "record_apply_audit_event": 1,
                "refresh_exports_and_vector_state": 1,
                "rerun_mcp_visibility_gate": 1,
            },
        },
    }


def _connection_readiness_payload() -> dict:
    return {
        "report_type": "mcp_connection_readiness",
        "passed": True,
        "finding_count": 0,
        "tenant_id": "tenant-aks-publish",
        "mcp_index_visibility_summary": {
            "passed": True,
            "tenant_id": "tenant-aks-publish",
            "document_count": 1,
            "total_approved_chunks": 5997,
            "total_mcp_visible_records": 5997,
            "approval_journal_coverage": {
                "journal_record_count": 5997,
                "record_count": 5997,
                "eligible_record_count": 5997,
                "matched_record_count": 5997,
                "missing_record_count": 0,
            },
        },
    }


def _index_visibility_payload() -> dict:
    return {
        "report_type": "mcp_index_visibility_audit",
        "passed": True,
        "approval_journal_coverage": {
            "journal_record_count": 5997,
            "record_count": 5997,
            "eligible_record_count": 5997,
            "matched_record_count": 5997,
            "missing_record_count": 0,
        },
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
