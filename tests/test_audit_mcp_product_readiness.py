from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.audit_mcp_product_readiness import (
    _runtime_latest_only_summary,
    build_mcp_product_readiness_audit,
    main as audit_main,
)


class AuditMcpProductReadinessTests(unittest.TestCase):
    def test_runtime_latest_only_summary_treats_legacy_runtime_without_regulation_metadata_as_latest_only(self) -> None:
        summary = _runtime_latest_only_summary(
            vector_records=[
                {"id": "record-1", "document_id": "doc-1", "chunk_id": "chunk-1"},
                {"id": "record-2", "document_id": "doc-1", "chunk_id": "chunk-2"},
            ],
            vector_metadata=[
                {"document_id": "doc-1", "chunk_id": "chunk-1", "approval_status": "approved"},
                {"document_id": "doc-1", "chunk_id": "chunk-2", "approval_status": "approved"},
            ],
        )

        self.assertTrue(summary["legacy_runtime_without_regulation_metadata"])
        self.assertTrue(summary["latest_only_passed"])
        self.assertEqual(0, summary["duplicate_active_version_group_count"])

    def test_runtime_latest_only_summary_still_requires_complete_metadata_when_any_lifecycle_identity_exists(self) -> None:
        summary = _runtime_latest_only_summary(
            vector_records=[
                {"id": "record-1", "document_id": "doc-1", "chunk_id": "chunk-1"},
                {"id": "record-2", "document_id": "doc-2", "chunk_id": "chunk-2"},
            ],
            vector_metadata=[
                {
                    "document_id": "doc-1",
                    "chunk_id": "chunk-1",
                    "regulation_id": "reg-1",
                    "regulation_version": "v1",
                    "effective_from": "2026-01-01",
                    "effective_to": "",
                    "repealed_at": "",
                    "regulation_status": "approved",
                },
                {
                    "document_id": "doc-2",
                    "chunk_id": "chunk-2",
                    "approval_status": "approved",
                },
            ],
        )

        self.assertFalse(summary["legacy_runtime_without_regulation_metadata"])
        self.assertFalse(summary["latest_only_passed"])
        self.assertEqual(0, summary["duplicate_active_version_group_count"])

    def test_runtime_latest_only_summary_normalizes_legacy_regulation_aliases(self) -> None:
        summary = _runtime_latest_only_summary(
            vector_records=[
                {"id": "record-1", "document_id": "doc-1", "chunk_id": "chunk-1"},
                {"id": "record-2", "document_id": "doc-2", "chunk_id": "chunk-2"},
            ],
            vector_metadata=[
                {
                    "document_id": "doc-1",
                    "chunk_id": "chunk-1",
                    "regulation_no": "reg-1",
                    "revision_date": "2024-01-01",
                    "regulation_status": "approved",
                },
                {
                    "document_id": "doc-2",
                    "chunk_id": "chunk-2",
                    "regulation_no": "reg-1",
                    "revision_date": "2025-01-01",
                    "regulation_status": "approved",
                },
            ],
        )

        self.assertFalse(summary["legacy_runtime_without_regulation_metadata"])
        self.assertTrue(summary["latest_only_passed"])
        self.assertEqual(1, summary["regulation_group_count"])
        self.assertEqual(1, summary["latest_selected_record_count"])
        self.assertEqual(1, summary["non_latest_record_count"])
        self.assertEqual(0, summary["duplicate_active_version_group_count"])
        self.assertEqual(0, summary["lifecycle_incomplete_count"])

    def test_ready_runtime_passes_five_product_gates_without_api_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            batch_report = root / "batch.json"
            rag_eval_report = root / "rag_eval.json"
            mcp_demo_answer_report = root / "mcp_demo_answers.json"
            accuracy_comparison_report = root / "accuracy_comparison.json"
            profile_provenance_report = root / "profile_provenance.json"
            parser_goldset_report = root / "parser_goldset.json"
            mcp_readiness_report = root / "mcp_readiness.json"
            mcp_transport_report = root / "mcp_transport.json"
            approval_worklist_report = root / "approval_worklist.json"
            approval_review_batch_report = root / "approval_review_batches.json"
            reapproval_worklist_report = root / "reapproval_worklist.json"
            reapproval_review_batch_report = root / "reapproval_review_batches.json"
            reapproval_decision_validation_report = root / "reapproval_decision_validation.json"
            reapproval_apply_plan_report = root / "reapproval_apply_plan.json"
            _write_json(
                batch_report,
                {
                    "successful_count": 2,
                    "failed_count": 0,
                    "ocr_required_count": 0,
                    "rows": [
                        {
                            "filename": "agency_a_rules.hwp",
                            "file_type": "hwp",
                            "quality_score": 100.0,
                            "quality_passed": True,
                            "profile_id": "public_portal-agency-a",
                            "institution_name": "기관A",
                        },
                        {
                            "filename": "agency_b_rules.pdf",
                            "file_type": "pdf",
                            "quality_score": 99.0,
                            "quality_passed": True,
                            "profile_id": "public_portal-agency-b",
                            "institution_name": "기관B",
                        },
                        {
                            "filename": "agency_c_rules.hwpx",
                            "file_type": "hwpx",
                            "quality_score": 99.0,
                            "quality_passed": True,
                            "profile_id": "public_portal-agency-c",
                            "institution_name": "기관C",
                        },
                    ],
                },
            )
            _write_json(
                rag_eval_report,
                {
                    "query_count": 3,
                    "answerable_count": 3,
                    "answerable_ratio": 1.0,
                    "relation_supported_ratio": 1.0,
                    "quality_warning_chunk_count": 0,
                    "api_call_count": 0,
                },
            )
            _write_json(mcp_demo_answer_report, _mcp_demo_answer_payload())
            _write_json(accuracy_comparison_report, _accuracy_comparison_payload())
            _write_json(profile_provenance_report, _profile_provenance_payload())
            _write_json(parser_goldset_report, _parser_goldset_score_payload())
            _write_json(
                mcp_readiness_report,
                {
                    "passed": True,
                    "deploy_ready": False,
                    "high_count": 0,
                    "medium_count": 0,
                    "finding_count": 0,
                },
            )
            _write_json(mcp_transport_report, _mcp_transport_payload())
            _write_clean_approval_evidence_reports(
                approval_worklist_report=approval_worklist_report,
                approval_review_batch_report=approval_review_batch_report,
                reapproval_worklist_report=reapproval_worklist_report,
                reapproval_review_batch_report=reapproval_review_batch_report,
                reapproval_decision_validation_report=reapproval_decision_validation_report,
            )
            _write_reapproval_apply_plan(reapproval_apply_plan_report)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                batch_reports=[batch_report],
                rag_eval_report=rag_eval_report,
                mcp_demo_answer_report=mcp_demo_answer_report,
                accuracy_comparison_report=accuracy_comparison_report,
                profile_provenance_report=profile_provenance_report,
                parser_goldset_score_report=parser_goldset_report,
                mcp_readiness_report=mcp_readiness_report,
                mcp_transport_smoke_report=mcp_transport_report,
                approval_worklist_reports=[approval_worklist_report],
                approval_review_batch_reports=[approval_review_batch_report],
                reapproval_worklist_reports=[reapproval_worklist_report],
                reapproval_review_batch_reports=[reapproval_review_batch_report],
                reapproval_decision_validation_reports=[reapproval_decision_validation_report],
                reapproval_apply_plan_reports=[reapproval_apply_plan_report],
            )

        self.assertTrue(report["passed"])
        self.assertEqual(0, report["blocking_count"])
        self.assertEqual(0, report["warning_count"])
        self.assertEqual(0, report["api_call_count"])
        self.assertEqual([str(reapproval_apply_plan_report)], report["source_reports"]["reapproval_apply_plan_reports"])
        self.assertTrue(report["reapproval_apply_plan_summary"]["passed"])
        self.assertEqual(0, report["reapproval_apply_plan_summary"]["unsafe_contract_violation_count"])
        self.assertEqual(1, report["reapproval_apply_plan_summary"]["batch_apply_control_count"])
        self.assertEqual(1, report["reapproval_apply_plan_summary"]["batch_requires_explicit_reindex_phase_count"])
        self.assertEqual(1, report["reapproval_apply_plan_summary"]["batch_conditional_vector_sync_guard_count"])
        self.assertIn(
            "reapproval_apply_plan_report",
            {artifact["role"] for artifact in report["source_report_artifacts"]},
        )
        self.assertEqual(0, report["runtime_summary"]["review_attention_chunk_count"])
        self.assertEqual({}, report["runtime_summary"]["review_attention_flag_counts"])
        approval_provenance = report["runtime_summary"]["approval_provenance_coverage"]
        self.assertEqual(2, approval_provenance["record_count"])
        self.assertEqual(2, approval_provenance["complete_record_count"])
        self.assertEqual(1.0, approval_provenance["complete_ratio"])
        self.assertEqual(0, approval_provenance["missing_field_counts"]["approval_worklist_report_sha256"])
        self.assertEqual(0, approval_provenance["missing_field_counts"]["approval_review_batch_manifest_sha256"])
        approval_journal = report["runtime_summary"]["approval_journal_coverage"]
        self.assertEqual(2, approval_journal["eligible_record_count"])
        self.assertEqual(2, approval_journal["matched_record_count"])
        self.assertEqual(0, approval_journal["missing_record_count"])
        self.assertEqual(
            {
                "parsing_accuracy",
                "revision_response",
                "generality",
                "answer_accuracy",
                "operations",
            },
            set(report["gates"]),
        )
        self.assertTrue(all(gate["status"] == "ready" for gate in report["gates"].values()))
        self.assertTrue(report["mcp_transport_smoke_summary"]["source_metadata_complete"])
        self.assertEqual(
            {
                "list_tools_elapsed_ms": 11.0,
                "search_elapsed_ms": 22.0,
                "fetch_elapsed_ms": 33.0,
                "warm_search_elapsed_ms": 8.0,
                "total_elapsed_ms": 100.0,
            },
            report["mcp_transport_smoke_summary"]["full_profile_timing_ms"],
        )
        self.assertEqual(
            {
                "load_vector_records_elapsed_ms": 1.0,
                "approval_snapshot_elapsed_ms": 2.0,
                "visibility_filter_elapsed_ms": 3.0,
                "scoring_elapsed_ms": 4.0,
            },
            report["mcp_transport_smoke_summary"]["full_profile_search_timing_ms"],
        )
        self.assertEqual(
            {
                "list_tools_elapsed_ms": 7.0,
                "search_elapsed_ms": 9.0,
                "fetch_elapsed_ms": 13.0,
                "warm_search_elapsed_ms": 5.0,
                "total_elapsed_ms": 50.0,
            },
            report["mcp_transport_smoke_summary"]["chatgpt_data_profile_timing_ms"],
        )
        self.assertTrue(report["mcp_demo_answer_summary"]["passed"])
        self.assertEqual(2, report["mcp_demo_answer_summary"]["answerable_query_count"])
        self.assertEqual(0, report["mcp_demo_answer_summary"]["expect_no_evidence_query_count"])
        self.assertEqual(0, report["mcp_demo_answer_summary"]["missing_supporting_result_count"])
        self.assertEqual(0, report["mcp_demo_answer_summary"]["no_evidence_with_citation_count"])
        self.assertEqual(2, report["mcp_demo_answer_summary"]["expected_term_query_count"])
        self.assertEqual([1.0, 0.5], report["mcp_demo_answer_summary"]["expected_term_hit_ratios"])
        self.assertEqual(0.5, report["mcp_demo_answer_summary"]["expected_term_min_hit_ratio"])
        self.assertEqual(0.75, report["mcp_demo_answer_summary"]["expected_term_average_hit_ratio"])
        self.assertEqual(1, report["mcp_demo_answer_summary"]["expected_term_partial_hit_count"])
        self.assertEqual(0, report["mcp_demo_answer_summary"]["expected_term_low_hit_count"])
        self.assertEqual(2, report["mcp_demo_answer_summary"]["expected_article_no_query_count"])
        self.assertEqual([1.0, 0.5], report["mcp_demo_answer_summary"]["expected_article_no_hit_ratios"])
        self.assertEqual(0.5, report["mcp_demo_answer_summary"]["expected_article_no_min_hit_ratio"])
        self.assertEqual(1, report["mcp_demo_answer_summary"]["expected_article_title_query_count"])
        self.assertEqual([1.0], report["mcp_demo_answer_summary"]["expected_article_title_hit_ratios"])
        self.assertEqual(1.0, report["mcp_demo_answer_summary"]["expected_article_title_min_hit_ratio"])
        self.assertEqual(10, report["mcp_demo_answer_summary"]["top_k"])
        self.assertEqual("config/query_specs.json", report["mcp_demo_answer_summary"]["query_spec_path"])
        self.assertEqual("a" * 64, report["mcp_demo_answer_summary"]["query_spec_sha256"])
        self.assertEqual(2, report["accuracy_comparison_summary"]["query_count"])
        self.assertEqual(0, report["accuracy_comparison_summary"]["mcp_regression_count"])
        self.assertEqual(0.15, report["accuracy_comparison_summary"]["avg_score_delta"])
        self.assertEqual(10, report["accuracy_comparison_summary"]["top_k"])
        self.assertEqual("config/query_specs.json", report["accuracy_comparison_summary"]["query_spec_path"])
        self.assertEqual("a" * 64, report["accuracy_comparison_summary"]["query_spec_sha256"])
        self.assertTrue(report["profile_provenance_summary"]["passed"])
        self.assertEqual(3, report["profile_provenance_summary"]["matched_profile_count"])
        self.assertEqual({}, report["profile_provenance_summary"]["unknown_profile_counts"])
        self.assertEqual(3, report["profile_provenance_summary"]["apba_id_count"])
        self.assertEqual({"C0147": 1, "C0165": 1, "C0247": 1}, report["profile_provenance_summary"]["apba_id_counts"])
        self.assertEqual(96.0, report["parser_goldset_score_summary"]["overall_f1"])
        self.assertEqual(0, report["parser_goldset_score_summary"]["issue_count"])
        self.assertTrue(report["parser_goldset_score_summary"]["ready_for_quality_claim"])

    def test_missing_approval_artifacts_warns_operations_gate_when_approved_runtime_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
            )

        self.assertTrue(report["passed"])
        self.assertIn("approval-worklist-evidence-missing", report["warning_codes"])
        self.assertIn("approval-review-batch-evidence-missing", report["warning_codes"])
        self.assertIn("reapproval-worklist-evidence-missing", report["warning_codes"])
        self.assertEqual("needs_review", report["gates"]["operations"]["status"])

    def test_mcp_readiness_bundle_runtime_mismatch_blocks_operations_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            mcp_readiness_report = root / "mcp_readiness.json"
            _write_json(
                mcp_readiness_report,
                {
                    "passed": True,
                    "client_profile": "bundle",
                    "bundle_dir": str(root / "other_bundle"),
                    "medium_count": 0,
                    "finding_count": 0,
                },
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                mcp_readiness_report=mcp_readiness_report,
            )

        self.assertFalse(report["passed"])
        self.assertIn("mcp-readiness-runtime-lineage-mismatch", report["blocking_codes"])
        self.assertEqual(1, report["mcp_evidence_lineage_summary"]["blocker_count"])

    def test_mcp_readiness_index_visibility_tenant_and_record_mismatch_blocks_operations_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            mcp_readiness_report = root / "mcp_readiness.json"
            _write_json(
                mcp_readiness_report,
                {
                    "passed": True,
                    "tenant_id": "tenant-b",
                    "medium_count": 0,
                    "finding_count": 0,
                    "mcp_index_visibility_summary": {
                        "tenant_id": "tenant-b",
                        "total_indexable_record_count": 1,
                        "total_mcp_visible_records": 1,
                    },
                },
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_id="tenant-a",
                tenant_storage_isolation=False,
                mcp_readiness_report=mcp_readiness_report,
            )

        self.assertFalse(report["passed"])
        self.assertIn("mcp-readiness-tenant-mismatch", report["blocking_codes"])
        self.assertIn("mcp-readiness-record-count-mismatch", report["blocking_codes"])

    def test_mcp_readiness_uses_indexable_not_visible_count_for_runtime_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            mcp_readiness_report = root / "mcp_readiness.json"
            _write_json(
                mcp_readiness_report,
                {
                    "passed": True,
                    "tenant_id": "default",
                    "medium_count": 0,
                    "finding_count": 0,
                    "mcp_index_visibility_summary": {
                        "tenant_id": "default",
                        "total_indexable_record_count": 2,
                        "total_mcp_visible_records": 1,
                    },
                },
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                mcp_readiness_report=mcp_readiness_report,
            )

        self.assertNotIn("mcp-readiness-record-count-mismatch", report["blocking_codes"])
        self.assertEqual(1, report["mcp_evidence_lineage_summary"]["mcp_readiness_visible_record_count"])
        self.assertEqual(2, report["mcp_evidence_lineage_summary"]["mcp_readiness_indexable_record_count"])

    def test_runtime_scoped_approval_evidence_mismatch_blocks_operations_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            other_data_dir = root / "other-data"
            _seed_runtime(data_dir)
            approval_worklist_report = root / "approval_worklist.json"
            _write_json(
                approval_worklist_report,
                {
                    "report_type": "approval_worklist",
                    "generated_at": "2026-01-01T00:00:00+00:00",
                    "tenant_id": "tenant-b",
                    "data_dir": str(other_data_dir),
                    "effective_data_dir": str(other_data_dir),
                    "total_chunks": 999,
                    "approved_chunks": 999,
                    "document_count": 1,
                },
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                approval_worklist_reports=[approval_worklist_report],
            )

        self.assertFalse(report["passed"])
        self.assertIn("source-report-runtime-lineage-mismatch", report["blocking_codes"])
        self.assertIn("source-report-tenant-mismatch", report["blocking_codes"])
        self.assertIn("source-report-record-count-mismatch", report["blocking_codes"])
        self.assertEqual(1, report["source_report_scope_summary"]["scoped_artifact_count"])

    def test_mcp_transport_smoke_tenant_mismatch_blocks_operations_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            mcp_transport_report = root / "mcp_transport.json"
            payload = _mcp_transport_payload()
            payload["tenant_id"] = "tenant-b"
            _write_json(mcp_transport_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_id="tenant-a",
                tenant_storage_isolation=False,
                mcp_transport_smoke_report=mcp_transport_report,
            )

        self.assertFalse(report["passed"])
        self.assertIn("mcp-transport-tenant-mismatch", report["blocking_codes"])
        self.assertEqual("tenant-b", report["mcp_transport_smoke_summary"]["tenant_id"])

    def test_mcp_transport_observed_result_tenant_mismatch_blocks_operations_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            mcp_transport_report = root / "mcp_transport.json"
            payload = _mcp_transport_payload()
            payload["tenant_id"] = "default"
            payload["full_profile"]["first_result_metadata"]["tenant_id"] = ""
            payload["full_profile"]["search_metadata"]["tenant_id"] = "tenant-b"
            payload["chatgpt_data_profile"]["first_result_metadata"]["tenant_id"] = "default"
            _write_json(mcp_transport_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                mcp_transport_smoke_report=mcp_transport_report,
            )

        self.assertFalse(report["passed"])
        self.assertIn("mcp-transport-tenant-mismatch", report["blocking_codes"])
        self.assertEqual(
            ["default", "tenant-b"],
            report["mcp_transport_smoke_summary"]["observed_result_tenant_ids"],
        )
        self.assertEqual(
            ["default", "tenant-b"],
            report["mcp_evidence_lineage_summary"]["mcp_transport_observed_tenant_ids"],
        )

    def test_vector_approval_provenance_gaps_warn_operations_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir, include_approval_provenance=False)
            mcp_readiness_report = root / "mcp_readiness.json"
            mcp_transport_report = root / "mcp_transport.json"
            approval_worklist_report = root / "approval_worklist.json"
            approval_review_batch_report = root / "approval_review_batches.json"
            reapproval_worklist_report = root / "reapproval_worklist.json"
            reapproval_review_batch_report = root / "reapproval_review_batches.json"
            reapproval_decision_validation_report = root / "reapproval_decision_validation.json"
            _write_json(mcp_readiness_report, {"passed": True, "medium_count": 0})
            _write_json(mcp_transport_report, _mcp_transport_payload())
            _write_clean_approval_evidence_reports(
                approval_worklist_report=approval_worklist_report,
                approval_review_batch_report=approval_review_batch_report,
                reapproval_worklist_report=reapproval_worklist_report,
                reapproval_review_batch_report=reapproval_review_batch_report,
                reapproval_decision_validation_report=reapproval_decision_validation_report,
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                mcp_readiness_report=mcp_readiness_report,
                mcp_transport_smoke_report=mcp_transport_report,
                approval_worklist_reports=[approval_worklist_report],
                approval_review_batch_reports=[approval_review_batch_report],
                reapproval_worklist_reports=[reapproval_worklist_report],
                reapproval_review_batch_reports=[reapproval_review_batch_report],
                reapproval_decision_validation_reports=[reapproval_decision_validation_report],
            )

        coverage = report["runtime_summary"]["approval_provenance_coverage"]
        self.assertEqual(2, coverage["record_count"])
        self.assertEqual(0, coverage["complete_record_count"])
        self.assertEqual(2, coverage["missing_field_counts"]["approval_worklist_report_path"])
        self.assertEqual(2, coverage["missing_field_counts"]["approval_worklist_report_sha256"])
        self.assertEqual(2, coverage["missing_field_counts"]["approval_review_batch_manifest_path"])
        self.assertEqual(2, coverage["missing_field_counts"]["approval_review_batch_manifest_sha256"])
        self.assertIn("approval-provenance-vector-evidence-incomplete", report["warning_codes"])
        self.assertEqual("needs_review", report["gates"]["operations"]["status"])

    def test_vector_approval_journal_gaps_block_operations_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir, include_approval_journal=False)
            mcp_readiness_report = root / "mcp_readiness.json"
            mcp_transport_report = root / "mcp_transport.json"
            approval_worklist_report = root / "approval_worklist.json"
            approval_review_batch_report = root / "approval_review_batches.json"
            reapproval_worklist_report = root / "reapproval_worklist.json"
            reapproval_review_batch_report = root / "reapproval_review_batches.json"
            reapproval_decision_validation_report = root / "reapproval_decision_validation.json"
            _write_json(mcp_readiness_report, {"passed": True, "medium_count": 0})
            _write_json(mcp_transport_report, _mcp_transport_payload())
            _write_clean_approval_evidence_reports(
                approval_worklist_report=approval_worklist_report,
                approval_review_batch_report=approval_review_batch_report,
                reapproval_worklist_report=reapproval_worklist_report,
                reapproval_review_batch_report=reapproval_review_batch_report,
                reapproval_decision_validation_report=reapproval_decision_validation_report,
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                mcp_readiness_report=mcp_readiness_report,
                mcp_transport_smoke_report=mcp_transport_report,
                approval_worklist_reports=[approval_worklist_report],
                approval_review_batch_reports=[approval_review_batch_report],
                reapproval_worklist_reports=[reapproval_worklist_report],
                reapproval_review_batch_reports=[reapproval_review_batch_report],
                reapproval_decision_validation_reports=[reapproval_decision_validation_report],
            )

        coverage = report["runtime_summary"]["approval_journal_coverage"]
        self.assertEqual(0, coverage["journal_record_count"])
        self.assertEqual(2, coverage["eligible_record_count"])
        self.assertEqual(0, coverage["matched_record_count"])
        self.assertEqual(2, coverage["missing_record_count"])
        self.assertIn("approval-journal-vector-evidence-missing", report["blocking_codes"])
        self.assertEqual("blocked", report["gates"]["operations"]["status"])
        self.assertFalse(report["passed"])

    def test_profile_scoped_approval_journal_records_still_match_without_profile_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            _seed_runtime(data_dir)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                profile_id="public_portal-public-rule",
            )

        coverage = report["runtime_summary"]["approval_journal_coverage"]
        self.assertEqual(2, coverage["journal_record_count"])
        self.assertEqual(2, coverage["eligible_record_count"])
        self.assertEqual(2, coverage["matched_record_count"])
        self.assertNotIn("approval-journal-vector-evidence-missing", report["blocking_codes"])

    def test_incomplete_approval_journal_review_events_block_operations_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            journal_path = data_dir / "repository" / "journals" / "approvals.jsonl"
            records = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
            records[0]["human_review_confirmed"] = True
            records[0]["ai_review_confirmed"] = True
            records[0]["review_decision_events"] = []
            journal_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in records) + "\n",
                encoding="utf-8",
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
            )

        coverage = report["runtime_summary"]["approval_journal_review_event_coverage"]
        self.assertEqual(1, coverage["incomplete_record_count"])
        self.assertEqual(1, coverage["missing_event_chunk_counts"]["approved"])
        self.assertEqual(1, coverage["missing_event_chunk_counts"]["human_review_confirmed"])
        self.assertEqual(1, coverage["missing_event_chunk_counts"]["ai_review_confirmed"])
        self.assertIn("approval-journal-review-events-incomplete", report["blocking_codes"])

    def test_foreign_tenant_review_event_gaps_do_not_block_current_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            journal_path = data_dir / "repository" / "journals" / "approvals.jsonl"
            records = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
            records.append(
                {
                    "approval_record_id": "foreign-approval-record",
                    "approval_id": "foreign-approval",
                    "document_id": "foreign-doc",
                    "tenant_id": "foreign-tenant",
                    "chunk_ids": ["foreign-chunk"],
                    "human_review_confirmed": True,
                    "ai_review_confirmed": True,
                    "review_decision_events": [],
                }
            )
            journal_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in records) + "\n",
                encoding="utf-8",
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
            )

        coverage = report["runtime_summary"]["approval_journal_review_event_coverage"]
        self.assertEqual(1, coverage["scoped_out_record_count"])
        self.assertEqual(0, coverage["incomplete_record_count"])
        self.assertNotIn("approval-journal-review-events-incomplete", report["blocking_codes"])

    def test_superseded_approval_journal_review_events_do_not_block_operations_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            journal_path = data_dir / "repository" / "journals" / "approvals.jsonl"
            records = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
            source = records[0]
            source["human_review_confirmed"] = True
            source["ai_review_confirmed"] = True
            source["review_decision_events"] = []
            correction = dict(source)
            correction["approval_record_id"] = "approval-record-correction"
            correction["supersedes_approval_record_ids"] = [source["approval_record_id"]]
            correction["review_decision_events"] = [
                {
                    "event": event_name,
                    "timestamp": "2026-07-12T00:00:00+00:00",
                    "actor": "operator",
                    "chunk_id": source["chunk_ids"][0],
                }
                for event_name in ("ai_review_confirmed", "human_review_confirmed", "approved")
            ]
            journal_path.write_text(
                "\n".join(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                    for record in (source, correction)
                )
                + "\n",
                encoding="utf-8",
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
            )

        coverage = report["runtime_summary"]["approval_journal_review_event_coverage"]
        self.assertEqual(2, coverage["journal_record_count"])
        self.assertEqual(1, coverage["active_journal_record_count"])
        self.assertEqual(1, coverage["superseded_record_count"])
        self.assertEqual(0, coverage["incomplete_record_count"])
        self.assertEqual(0, sum(coverage["missing_event_chunk_counts"].values()))
        self.assertNotIn("approval-journal-review-events-incomplete", report["blocking_codes"])

    def test_source_report_artifacts_include_file_fingerprint_and_payload_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            batch_report = root / "batch.json"
            public_readiness_report = root / "public_readiness.json"
            _write_json(
                batch_report,
                {
                    "generated_at": "2026-07-09T01:00:00+00:00",
                    "successful_count": 1,
                    "failed_count": 0,
                    "ocr_required_count": 0,
                    "rows": [
                        {
                            "filename": "agency_a_rules.hwp",
                            "file_type": "hwp",
                            "quality_score": 100.0,
                            "quality_passed": True,
                            "profile_id": "public_portal-agency-a",
                            "institution_name": "Agency A",
                        }
                    ],
                },
            )
            _write_json(
                public_readiness_report,
                {
                    "generated_from": "2026-07-09T02:00:00Z",
                    "status": "public_batch_ready",
                    "passed": True,
                    "summary": {
                        "input_count": 1,
                        "successful_count": 1,
                        "failed_count": 0,
                        "ocr_required_count": 0,
                        "recommendation_total": 0,
                    },
                    "checks": [{"name": "all_inputs_successful", "passed": True}],
                },
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                batch_reports=[batch_report],
                public_readiness_report=public_readiness_report,
            )
            expected_batch_sha = hashlib.sha256(batch_report.read_bytes()).hexdigest()
            expected_batch_size = batch_report.stat().st_size

        artifacts = report["source_report_artifacts"]
        by_role = {artifact["role"]: artifact for artifact in artifacts}
        self.assertEqual(2, report["source_report_artifact_summary"]["provided_count"])
        self.assertEqual(2, report["source_report_artifact_summary"]["payload_generated_at_count"])
        self.assertEqual(2, report["source_report_artifact_summary"]["sha256_count"])
        self.assertEqual("2026-07-09T01:00:00+00:00", by_role["batch_report"]["payload_generated_at"])
        self.assertEqual("2026-07-09T02:00:00Z", by_role["public_readiness_report"]["payload_generated_at"])
        self.assertEqual(expected_batch_sha, by_role["batch_report"]["sha256"])
        self.assertEqual(expected_batch_size, by_role["batch_report"]["byte_count"])
        self.assertIsNotNone(by_role["batch_report"]["payload_age_hours"])

    def test_parser_goldset_completion_board_is_fingerprinted_and_summarized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            completion_board_report = root / "parser_completion_board.json"
            _write_json(completion_board_report, _parser_goldset_completion_board_payload())

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                parser_goldset_completion_board_report=completion_board_report,
            )
            expected_sha = hashlib.sha256(completion_board_report.read_bytes()).hexdigest()

        by_role = {artifact["role"]: artifact for artifact in report["source_report_artifacts"]}
        summary = report["parser_goldset_completion_board_summary"]
        self.assertEqual(str(completion_board_report), report["source_reports"]["parser_goldset_completion_board_report"])
        self.assertIn("parser_goldset_completion_board_report", by_role)
        self.assertEqual(expected_sha, by_role["parser_goldset_completion_board_report"]["sha256"])
        self.assertEqual("blocked_pending_human_labels", summary["completion_gate_status"])
        self.assertFalse(summary["ready_for_quality_claim"])
        self.assertEqual(84, summary["expected_structure_score_rows"])
        self.assertEqual(0, summary["completed_structure_score_rows"])
        self.assertEqual(394, summary["structure_completion_summary"]["table"]["pipeline_total"])
        self.assertEqual(12, summary["structure_completion_summary"]["table"]["missing_manual_count"])
        self.assertNotIn("parser-goldset-quality-claim-not-ready", report["blocking_codes"])

    def test_table_preprocessing_claim_gate_is_fingerprinted_and_summarized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            claim_gate_report = root / "table_claim_gate.json"
            _write_json(claim_gate_report, _table_preprocessing_claim_gate_payload())

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                table_preprocessing_claim_gate_report=claim_gate_report,
            )
            expected_sha = hashlib.sha256(claim_gate_report.read_bytes()).hexdigest()

        by_role = {artifact["role"]: artifact for artifact in report["source_report_artifacts"]}
        summary = report["table_preprocessing_claim_gate_summary"]
        self.assertEqual(str(claim_gate_report), report["source_reports"]["table_preprocessing_claim_gate_report"])
        self.assertIn("table_preprocessing_claim_gate_report", by_role)
        self.assertEqual(expected_sha, by_role["table_preprocessing_claim_gate_report"]["sha256"])
        self.assertFalse(summary["passed"])
        self.assertEqual("blocked_pending_human_review", summary["status"])
        self.assertEqual("review_ready_not_accuracy_proven", summary["claim_level"])
        self.assertEqual(255, summary["pending_unit_count"])
        self.assertEqual(2040, summary["required_field_missing_total"])
        self.assertEqual({"source_table_compare": 144}, summary["review_priority_counts"])
        self.assertEqual({"missing_table_label": 60}, summary["label_review_flag_counts"])
        self.assertEqual(25, summary["transfer_blocker_count"])
        self.assertIsNone(summary["source_traceability_require_page_count_verification"])
        self.assertEqual({"verified_pdf": 10, "verified_hwpx_zip": 5}, summary["source_format_status_counts"])
        self.assertEqual(0, summary["table_answer_blocker_count"])
        self.assertTrue(summary["non_review_evidence_ready"])
        self.assertTrue(summary["release_blocked_by_human_review"])
        self.assertNotIn("table-preprocessing-claim-not-ready", report["blocking_codes"])
        self.assertIn("table-preprocessing-claim-not-ready", report["warning_codes"])

    def test_required_table_preprocessing_claim_blocks_parsing_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            claim_gate_report = root / "table_claim_gate.json"
            _write_json(claim_gate_report, _table_preprocessing_claim_gate_payload())

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                table_preprocessing_claim_gate_report=claim_gate_report,
                require_table_preprocessing_claim=True,
            )

        self.assertFalse(report["passed"])
        self.assertIn("table-preprocessing-claim-not-ready", report["blocking_codes"])
        self.assertEqual("blocked", report["gates"]["parsing_accuracy"]["status"])
        finding = next(
            item
            for item in report["gates"]["parsing_accuracy"]["findings"]
            if item["code"] == "table-preprocessing-claim-not-ready"
        )
        self.assertIn("pending_unit_count=255", finding["detail"])
        self.assertIn("required_field_missing_total=2040", finding["detail"])
        self.assertIn("transfer_blocker_count=25", finding["detail"])

    def test_required_table_preprocessing_claim_detail_includes_traceability_issue_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            claim_gate_report = root / "table_claim_gate.json"
            payload = _table_preprocessing_claim_gate_payload()
            payload["status"] = "blocked_source_traceability"
            payload["feasibility_status"] = "blocked_before_review"
            payload["summary"]["source_traceability_passed"] = False
            payload["summary"]["source_traceability_issue_count"] = 15
            payload["summary"]["source_traceability_issue_counts"] = {
                "pdf-reader-backend-unavailable": 15
            }
            payload["summary"]["source_traceability_operator_next_action_counts"] = {
                "Fix the Python PDF reader backend or run traceability in the packaged project environment; the source PDF has not been proven invalid.": 15
            }
            _write_json(claim_gate_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                table_preprocessing_claim_gate_report=claim_gate_report,
                require_table_preprocessing_claim=True,
            )

        summary = report["table_preprocessing_claim_gate_summary"]
        self.assertEqual(
            {"pdf-reader-backend-unavailable": 15},
            summary["source_traceability_issue_counts"],
        )
        finding = next(
            item
            for item in report["gates"]["parsing_accuracy"]["findings"]
            if item["code"] == "table-preprocessing-claim-not-ready"
        )
        self.assertIn("source_traceability_issue_count=15", finding["detail"])
        self.assertIn("pdf-reader-backend-unavailable", finding["detail"])

    def test_required_table_preprocessing_claim_missing_blocks_parsing_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            _seed_runtime(data_dir)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                require_table_preprocessing_claim=True,
            )

        self.assertFalse(report["passed"])
        self.assertIn("table-preprocessing-claim-missing", report["blocking_codes"])
        self.assertEqual("blocked", report["gates"]["parsing_accuracy"]["status"])

    def test_ready_table_preprocessing_claim_clears_required_parsing_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            claim_gate_report = root / "table_claim_gate.json"
            payload = _table_preprocessing_claim_gate_payload()
            payload["passed"] = True
            payload["status"] = "ready_for_table_quality_claim"
            payload["claim_level"] = "quality_claim_ready"
            payload["feasibility_status"] = "feasible_with_human_review"
            payload["blocker_count"] = 0
            payload["summary"]["completed_unit_count"] = 255
            payload["summary"]["pending_unit_count"] = 0
            payload["summary"]["ready_for_table_score_transfer"] = True
            payload["summary"]["transfer_passed"] = True
            payload["summary"]["transfer_blocker_count"] = 0
            payload["summary"]["source_traceability_require_page_count_verification"] = True
            payload["summary"]["drift_check_present"] = True
            payload["summary"]["drift_check_passed"] = True
            payload["summary"]["drift_check_blocker_count"] = 0
            payload["summary"]["non_review_evidence_ready"] = True
            payload["summary"]["release_blocked_by_human_review"] = False
            _write_json(claim_gate_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                table_preprocessing_claim_gate_report=claim_gate_report,
                require_table_preprocessing_claim=True,
            )

        self.assertNotIn("table-preprocessing-claim-not-ready", report["blocking_codes"])
        self.assertNotIn("table-preprocessing-claim-not-ready", report["warning_codes"])
        self.assertEqual("needs_review", report["gates"]["parsing_accuracy"]["status"])

    def test_strict_table_preprocessing_claim_requires_drift_and_page_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            claim_gate_report = root / "table_claim_gate.json"
            payload = _table_preprocessing_claim_gate_payload()
            payload["passed"] = True
            payload["status"] = "ready_for_table_quality_claim"
            payload["claim_level"] = "quality_claim_ready"
            payload["feasibility_status"] = "feasible_with_human_review"
            payload["blocker_count"] = 0
            payload["summary"]["completed_unit_count"] = 255
            payload["summary"]["pending_unit_count"] = 0
            payload["summary"]["ready_for_table_score_transfer"] = True
            payload["summary"]["transfer_passed"] = True
            payload["summary"]["transfer_blocker_count"] = 0
            payload["summary"]["drift_check_present"] = True
            payload["summary"]["drift_check_passed"] = True
            payload["summary"]["drift_check_blocker_count"] = 0
            payload["summary"]["non_review_evidence_ready"] = True
            payload["summary"]["release_blocked_by_human_review"] = False
            _write_json(claim_gate_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                table_preprocessing_claim_gate_report=claim_gate_report,
                require_table_preprocessing_claim=True,
            )

        self.assertFalse(report["passed"])
        self.assertIn("table-preprocessing-claim-not-ready", report["blocking_codes"])
        finding = next(
            item
            for item in report["gates"]["parsing_accuracy"]["findings"]
            if item["code"] == "table-preprocessing-claim-not-ready"
        )
        self.assertIn("source_traceability_require_page_count_verification=false", finding["detail"])

    def test_accuracy_comparison_regression_blocks_answer_accuracy_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            accuracy_comparison_report = root / "accuracy_comparison.json"
            payload = _accuracy_comparison_payload()
            payload["passed"] = False
            payload["summary"]["mcp_regression_count"] = 1
            payload["summary"]["mcp_not_worse_count"] = 1
            _write_json(accuracy_comparison_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                accuracy_comparison_report=accuracy_comparison_report,
            )

        self.assertFalse(report["passed"])
        self.assertIn("mcp-accuracy-comparison-failed", report["blocking_codes"])
        self.assertIn("mcp-accuracy-comparison-regression", report["blocking_codes"])
        self.assertEqual("blocked", report["gates"]["answer_accuracy"]["status"])

    def test_missing_accuracy_comparison_warns_answer_accuracy_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            rag_eval_report = root / "rag_eval.json"
            mcp_demo_answer_report = root / "mcp_demo_answers.json"
            _write_json(
                rag_eval_report,
                {
                    "query_count": 2,
                    "answerable_count": 2,
                    "answerable_ratio": 1.0,
                    "quality_warning_chunk_count": 0,
                    "api_call_count": 0,
                },
            )
            _write_json(mcp_demo_answer_report, _mcp_demo_answer_payload())

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                rag_eval_report=rag_eval_report,
                mcp_demo_answer_report=mcp_demo_answer_report,
            )

        answer_warning_codes = {
            finding["code"]
            for finding in report["gates"]["answer_accuracy"]["findings"]
            if finding["severity"] == "warning"
        }
        self.assertTrue(report["passed"])
        self.assertIn("mcp-accuracy-comparison-report-missing", report["warning_codes"])
        self.assertEqual({"mcp-accuracy-comparison-report-missing"}, answer_warning_codes)
        self.assertEqual("needs_review", report["gates"]["answer_accuracy"]["status"])

    def test_smoke_citation_in_demo_answer_blocks_answer_accuracy_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            mcp_demo_answer_report = root / "mcp_demo_answers.json"
            payload = _mcp_demo_answer_payload()
            payload["items"][0]["smoke_citation_count"] = 1
            payload["items"][0]["citations"][0]["document_id"] = "doc_mcp_smoke_v1"
            _write_json(mcp_demo_answer_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                mcp_demo_answer_report=mcp_demo_answer_report,
            )

        self.assertFalse(report["passed"])
        self.assertIn("mcp-demo-smoke-citations", report["blocking_codes"])
        self.assertEqual("blocked", report["gates"]["answer_accuracy"]["status"])

    def test_no_evidence_demo_answer_does_not_require_supporting_citation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            mcp_demo_answer_report = root / "mcp_demo_answers.json"
            payload = _mcp_demo_answer_payload()
            payload["query_count"] = 3
            payload["items"].append(
                {
                    "query": "존재하지 않는 규정은 있나요?",
                    "expect_no_evidence": True,
                    "passed": True,
                    "search_result_count": 0,
                    "fetch_result_count": 0,
                    "supporting_result_count": 0,
                    "answer": "승인된 근거를 찾을 수 없습니다.",
                    "smoke_citation_count": 0,
                    "quality_issue_count": 0,
                    "quality_issues": [],
                    "citations": [],
                }
            )
            _write_json(mcp_demo_answer_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                mcp_demo_answer_report=mcp_demo_answer_report,
            )

        self.assertTrue(report["mcp_demo_answer_summary"]["passed"])
        self.assertEqual(2, report["mcp_demo_answer_summary"]["answerable_query_count"])
        self.assertEqual(1, report["mcp_demo_answer_summary"]["expect_no_evidence_query_count"])
        self.assertEqual(0, report["mcp_demo_answer_summary"]["missing_supporting_result_count"])
        self.assertEqual(0, report["mcp_demo_answer_summary"]["no_evidence_with_citation_count"])
        self.assertNotIn("mcp-demo-supporting-citations-missing", report["blocking_codes"])
        self.assertNotIn("mcp-demo-no-evidence-citations", report["blocking_codes"])

    def test_no_evidence_demo_answer_with_citation_blocks_answer_accuracy_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            mcp_demo_answer_report = root / "mcp_demo_answers.json"
            payload = _mcp_demo_answer_payload()
            payload["query_count"] = 3
            payload["items"].append(
                {
                    "query": "존재하지 않는 규정은 있나요?",
                    "expect_no_evidence": True,
                    "passed": True,
                    "search_result_count": 1,
                    "fetch_result_count": 1,
                    "supporting_result_count": 1,
                    "answer": "승인된 근거를 찾을 수 없습니다.",
                    "smoke_citation_count": 0,
                    "quality_issue_count": 0,
                    "quality_issues": [],
                    "citations": [
                        {
                            "document_id": "doc-public-regulation",
                            "chunk_id": "chunk-leave",
                            "approval_id": "approval-chunk-leave",
                        }
                    ],
                }
            )
            _write_json(mcp_demo_answer_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                mcp_demo_answer_report=mcp_demo_answer_report,
            )

        self.assertFalse(report["mcp_demo_answer_summary"]["passed"])
        self.assertEqual(1, report["mcp_demo_answer_summary"]["no_evidence_with_citation_count"])
        self.assertIn("mcp-demo-no-evidence-citations", report["blocking_codes"])
        self.assertEqual("blocked", report["gates"]["answer_accuracy"]["status"])

    def test_quality_issue_in_demo_answer_blocks_answer_accuracy_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            mcp_demo_answer_report = root / "mcp_demo_answers.json"
            payload = _mcp_demo_answer_payload()
            payload["quality_issue_count"] = 1
            payload["items"][0]["quality_issue_count"] = 1
            payload["items"][0]["quality_issues"] = [{"code": "answer-fragment-line"}]
            _write_json(mcp_demo_answer_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                mcp_demo_answer_report=mcp_demo_answer_report,
            )

        self.assertFalse(report["passed"])
        self.assertIn("mcp-demo-quality-issues", report["blocking_codes"])
        self.assertEqual("blocked", report["gates"]["answer_accuracy"]["status"])

    def test_missing_profile_id_mcp_source_metadata_warns_operations_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            mcp_transport_report = root / "mcp_transport.json"
            payload = _mcp_transport_payload()
            payload["profile_id"] = "aks-korean-studies"
            payload["full_profile"]["first_result_metadata"].pop("profile_id")
            _write_json(mcp_transport_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                mcp_transport_smoke_report=mcp_transport_report,
            )

        self.assertTrue(report["passed"])
        self.assertNotIn("mcp-source-metadata-missing", report["blocking_codes"])
        self.assertIn("mcp-source-metadata-missing", report["warning_codes"])
        self.assertEqual("needs_review", report["gates"]["operations"]["status"])
        self.assertEqual(["profile_id"], report["mcp_transport_smoke_summary"]["missing_source_metadata_fields"])

    def test_appendix_mcp_source_metadata_allows_missing_article_no(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            mcp_transport_report = root / "mcp_transport.json"
            payload = _mcp_transport_payload()
            payload["full_profile"]["first_result_metadata"]["chunk_type"] = "appendix"
            payload["full_profile"]["first_result_metadata"]["article_no"] = ""
            payload["chatgpt_data_profile"]["first_result_metadata"]["chunk_type"] = "appendix"
            payload["chatgpt_data_profile"]["first_result_metadata"]["article_no"] = ""
            _write_json(mcp_transport_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                mcp_transport_smoke_report=mcp_transport_report,
            )

        self.assertTrue(report["mcp_transport_smoke_summary"]["source_metadata_complete"])
        self.assertNotIn("article_no", report["mcp_transport_smoke_summary"]["required_source_metadata_fields"])
        self.assertNotIn("mcp-source-metadata-missing", report["blocking_codes"])

    def test_article_mcp_source_metadata_requires_article_no(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            mcp_transport_report = root / "mcp_transport.json"
            payload = _mcp_transport_payload()
            payload["profile_id"] = "aks-korean-studies"
            payload["full_profile"]["first_result_metadata"]["chunk_type"] = "article"
            payload["full_profile"]["first_result_metadata"]["article_no"] = ""
            _write_json(mcp_transport_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                mcp_transport_smoke_report=mcp_transport_report,
            )

        self.assertFalse(report["mcp_transport_smoke_summary"]["source_metadata_complete"])
        self.assertIn("article_no", report["mcp_transport_smoke_summary"]["required_source_metadata_fields"])
        self.assertEqual(["article_no"], report["mcp_transport_smoke_summary"]["missing_source_metadata_fields"])
        self.assertIn("mcp-source-metadata-missing", report["blocking_codes"])

    def test_article_mcp_source_metadata_is_validated_per_tool_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            mcp_transport_report = root / "mcp_transport.json"
            payload = _mcp_transport_payload()
            payload["profile_id"] = "aks-korean-studies"
            payload["full_profile"]["first_result_metadata"]["chunk_type"] = "appendix"
            payload["full_profile"]["first_result_metadata"]["article_no"] = ""
            payload["chatgpt_data_profile"]["first_result_metadata"]["chunk_type"] = "article"
            payload["chatgpt_data_profile"]["first_result_metadata"]["article_no"] = ""
            _write_json(mcp_transport_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                mcp_transport_smoke_report=mcp_transport_report,
            )

        self.assertFalse(report["mcp_transport_smoke_summary"]["source_metadata_complete"])
        self.assertEqual(["article_no"], report["mcp_transport_smoke_summary"]["missing_source_metadata_fields"])
        self.assertIn("mcp-source-metadata-missing", report["blocking_codes"])
        profile_summaries = report["mcp_transport_smoke_summary"]["profile_source_metadata"]
        self.assertEqual(
            ["chatgpt_data_profile"],
            [item["profile"] for item in profile_summaries if item["missing_fields"]],
        )

    def test_synthetic_transport_smoke_without_history_tool_warns_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            mcp_transport_report = root / "mcp_transport.json"
            payload = _mcp_transport_payload()
            payload["full_profile"]["tool_names"] = ["fetch", "search", "list_documents"]
            payload["full_profile"].pop("history_tool_available", None)
            payload["full_profile"].pop("history_attempted", None)
            payload["full_profile"].pop("history_passed", None)
            payload["full_profile"].pop("history_version_count", None)
            _write_json(mcp_transport_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                mcp_transport_smoke_report=mcp_transport_report,
            )

        self.assertTrue(report["passed"])
        self.assertIn("mcp-transport-history-missing", report["warning_codes"])
        self.assertNotIn("mcp-transport-history-missing", report["blocking_codes"])
        self.assertEqual("needs_review", report["gates"]["operations"]["status"])

    def test_partial_index_blocks_operations_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            _seed_runtime(data_dir, vector_count=1)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
            )

        self.assertFalse(report["passed"])
        self.assertIn("runtime-not-fully-indexed", report["blocking_codes"])
        self.assertEqual("blocked", report["gates"]["operations"]["status"])

    def test_flat_storage_filters_repository_chunks_by_tenant_id_before_index_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository_dir = data_dir / "repository"
            vector_dir = data_dir / "vector_db" / "tenant-a"
            repository_dir.mkdir(parents=True)
            vector_dir.mkdir(parents=True)
            tenant_a_chunk = _chunk(
                document_id="doc-tenant-a",
                chunk_id="chunk-tenant-a",
                article_no="Article 1",
                article_title="Tenant A",
                text="Tenant A approved text",
                tenant_id="tenant-a",
            )
            tenant_b_chunk = _chunk(
                document_id="doc-tenant-b",
                chunk_id="chunk-tenant-b",
                article_no="Article 2",
                article_title="Tenant B",
                text="Tenant B approved text",
                tenant_id="tenant-b",
            )
            _write_json(repository_dir / "mixed_chunks.json", [tenant_a_chunk, tenant_b_chunk])
            (vector_dir / "approved_vectors.jsonl").write_text(
                json.dumps(_vector_record(tenant_a_chunk), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_id="tenant-a",
                tenant_storage_isolation=False,
            )

        self.assertEqual(1, report["runtime_summary"]["repository_chunk_count"])
        self.assertEqual(1, report["runtime_summary"]["approved_repository_chunk_count"])
        self.assertEqual(1, report["runtime_summary"]["vector_record_count"])
        self.assertTrue(report["runtime_summary"]["full_index_match"])
        self.assertNotIn("runtime-not-fully-indexed", report["blocking_codes"])

    def test_partial_temporal_metadata_warns_revision_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            _seed_runtime(data_dir)
            vector_path = data_dir / "vector_db" / "default" / "approved_vectors.jsonl"
            records = [json.loads(line) for line in vector_path.read_text(encoding="utf-8").splitlines()]
            records[1]["metadata"].pop("effective_date")
            records[1]["metadata"].pop("revision_date")
            vector_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                encoding="utf-8",
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
            )

        self.assertTrue(report["passed"])
        self.assertIn("temporal-metadata-partial", report["warning_codes"])
        self.assertEqual("needs_review", report["gates"]["revision_response"]["status"])
        self.assertEqual(1, report["runtime_summary"]["temporal_metadata_loss_count"])

    def test_partial_temporal_coverage_without_vector_loss_warns_revision_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            _seed_runtime(data_dir)
            chunk_path = data_dir / "repository" / "doc-public-regulation_chunks.json"
            chunks = json.loads(chunk_path.read_text(encoding="utf-8"))
            chunks[1]["metadata"].pop("effective_date")
            chunks[1]["metadata"].pop("revision_date")
            chunk_path.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            vector_path = data_dir / "vector_db" / "default" / "approved_vectors.jsonl"
            records = [json.loads(line) for line in vector_path.read_text(encoding="utf-8").splitlines()]
            records[1]["metadata"].pop("effective_date")
            records[1]["metadata"].pop("revision_date")
            vector_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                encoding="utf-8",
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
            )

        self.assertTrue(report["passed"])
        self.assertNotIn("temporal-metadata-partial", report["warning_codes"])
        self.assertIn("temporal-metadata-coverage-partial", report["warning_codes"])
        self.assertEqual("needs_review", report["gates"]["revision_response"]["status"])
        self.assertEqual(0, report["runtime_summary"]["temporal_metadata_loss_count"])

    def test_article_validity_windows_count_as_temporal_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            _seed_runtime(data_dir)
            chunk_path = data_dir / "repository" / "doc-public-regulation_chunks.json"
            chunks = json.loads(chunk_path.read_text(encoding="utf-8"))
            for chunk in chunks:
                metadata = chunk["metadata"]
                metadata.pop("effective_date")
                metadata.pop("revision_date")
                metadata["article_validity_windows"] = [{"valid_from": "2026-01-01", "valid_to": None}]
            chunk_path.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            vector_path = data_dir / "vector_db" / "default" / "approved_vectors.jsonl"
            records = [json.loads(line) for line in vector_path.read_text(encoding="utf-8").splitlines()]
            for record in records:
                metadata = record["metadata"]
                metadata.pop("effective_date")
                metadata.pop("revision_date")
                metadata["article_validity_windows"] = [{"valid_from": "2026-01-01", "valid_to": None}]
            vector_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                encoding="utf-8",
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
            )

        self.assertTrue(report["passed"])
        self.assertNotIn("temporal-metadata-not-evidenced", report["warning_codes"])
        self.assertNotIn("temporal-metadata-coverage-partial", report["warning_codes"])
        self.assertEqual(2, report["runtime_summary"]["temporal_metadata_count"])
        self.assertEqual(1.0, report["runtime_summary"]["temporal_metadata_ratio"])

    def test_temporal_reports_are_fingerprinted_and_explain_partial_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            chunk_path = data_dir / "repository" / "doc-public-regulation_chunks.json"
            chunks = json.loads(chunk_path.read_text(encoding="utf-8"))
            chunks[1]["metadata"].pop("effective_date")
            chunks[1]["metadata"].pop("revision_date")
            chunk_path.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            vector_path = data_dir / "vector_db" / "default" / "approved_vectors.jsonl"
            records = [json.loads(line) for line in vector_path.read_text(encoding="utf-8").splitlines()]
            records[1]["metadata"].pop("effective_date")
            records[1]["metadata"].pop("revision_date")
            vector_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                encoding="utf-8",
            )
            temporal_coverage_report = root / "temporal_coverage.json"
            temporal_backfill_report = root / "temporal_backfill.json"
            _write_json(
                temporal_coverage_report,
                {
                    "report_type": "temporal_metadata_coverage",
                    "generated_at": "2026-07-09T03:00:00+00:00",
                    "passed": True,
                    "record_count": 2,
                    "with_temporal_metadata_count": 1,
                    "without_temporal_metadata_count": 1,
                    "temporal_metadata_ratio": 0.5,
                    "inheritance_opportunities": {
                        "candidate_scope_count": 1,
                        "candidate_missing_record_count": 1,
                    },
                    "blocker_count": 0,
                    "warning_count": 1,
                    "finding_count": 2,
                    "api_call_count": 0,
                },
            )
            _write_json(
                temporal_backfill_report,
                {
                    "report_type": "temporal_backfill_shadow_runtime",
                    "generated_at": "2026-07-09T03:05:00+00:00",
                    "passed": False,
                    "chunk_file_count": 1,
                    "input_chunk_count": 2,
                    "output_chunk_count": 2,
                    "vector_record_count": 2,
                    "before": {
                        "temporal_metadata_count": 1,
                        "temporal_metadata_ratio": 0.5,
                    },
                    "after": {
                        "temporal_metadata_count": 2,
                        "temporal_metadata_ratio": 1.0,
                        "inherited_chunk_count": 1,
                        "normalized_chunk_count": 1,
                        "conflict_chunk_count": 1,
                    },
                    "delta": {
                        "temporal_metadata_count": 1,
                        "conflict_chunk_count": 1,
                    },
                    "write_blocked": True,
                    "shadow_runtime_written": False,
                    "api_call_count": 0,
                },
            )
            expected_temporal_coverage_sha = hashlib.sha256(temporal_coverage_report.read_bytes()).hexdigest()

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                temporal_coverage_report=temporal_coverage_report,
                temporal_backfill_shadow_report=temporal_backfill_report,
            )

        by_role = {artifact["role"]: artifact for artifact in report["source_report_artifacts"]}
        finding = next(
            item
            for item in report["gates"]["revision_response"]["findings"]
            if item["code"] == "temporal-metadata-coverage-partial"
        )
        self.assertIn("temporal_coverage_report", by_role)
        self.assertIn("temporal_backfill_shadow_report", by_role)
        self.assertEqual(expected_temporal_coverage_sha, by_role["temporal_coverage_report"]["sha256"])
        self.assertEqual(1, report["temporal_coverage_summary"]["candidate_missing_record_count"])
        self.assertEqual(1, report["temporal_backfill_shadow_summary"]["delta_temporal_metadata_count"])
        self.assertEqual(1, report["temporal_backfill_shadow_summary"]["conflict_chunk_count"])
        self.assertTrue(report["temporal_backfill_shadow_summary"]["write_blocked"])
        self.assertIn("delta_temporal_metadata_count=1", finding["detail"])
        self.assertIn("conflict_chunk_count=1", finding["detail"])

    def test_temporal_ambiguity_shadow_requires_scope_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            temporal_backfill_report = root / "temporal_backfill.json"
            _write_json(temporal_backfill_report, _temporal_backfill_payload(ambiguous_count=2))

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                temporal_backfill_shadow_report=temporal_backfill_report,
            )

        self.assertFalse(report["passed"])
        self.assertIn("temporal-ambiguity-scope-missing", report["blocking_codes"])
        self.assertEqual("blocked", report["gates"]["revision_response"]["status"])
        self.assertEqual(2, report["temporal_backfill_shadow_summary"]["ambiguous_chunk_count"])

    def test_empty_temporal_shadow_vector_projection_blocks_revision_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            temporal_backfill_report = root / "temporal_backfill.json"
            payload = _temporal_backfill_payload(ambiguous_count=0)
            payload["runtime_copy_scope"] = "repository_chunks_and_approved_vectors_only"
            payload["shadow_runtime_runnable"] = False
            payload["vector_record_count"] = 0
            _write_json(temporal_backfill_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                temporal_backfill_shadow_report=temporal_backfill_report,
            )

        summary = report["temporal_backfill_shadow_summary"]
        self.assertFalse(report["passed"])
        self.assertIn("temporal-backfill-vector-projection-empty", report["blocking_codes"])
        self.assertEqual("blocked", report["gates"]["revision_response"]["status"])
        self.assertEqual(0, summary["vector_record_count"])
        self.assertFalse(summary["shadow_runtime_runnable"])
        self.assertFalse(summary["shadow_vector_projection_ready"])
        finding = next(
            item
            for item in report["gates"]["revision_response"]["findings"]
            if item["code"] == "temporal-backfill-vector-projection-empty"
        )
        self.assertIn("vector_record_count=2", finding["detail"])
        self.assertIn("shadow_vector_record_count=0", finding["detail"])

    def test_temporal_ambiguity_scope_policy_blocks_revision_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            temporal_backfill_report = root / "temporal_backfill.json"
            ambiguity_scope_report = root / "temporal_ambiguity_scope.json"
            _write_json(temporal_backfill_report, _temporal_backfill_payload(ambiguous_count=2))
            _write_json(ambiguity_scope_report, _temporal_ambiguity_scope_payload(ambiguous_count=2))
            expected_sha = hashlib.sha256(ambiguity_scope_report.read_bytes()).hexdigest()

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                temporal_backfill_shadow_report=temporal_backfill_report,
                temporal_ambiguity_scope_report=ambiguity_scope_report,
            )

        by_role = {artifact["role"]: artifact for artifact in report["source_report_artifacts"]}
        self.assertFalse(report["passed"])
        self.assertIn("temporal_ambiguity_scope_report", by_role)
        self.assertEqual(expected_sha, by_role["temporal_ambiguity_scope_report"]["sha256"])
        self.assertIn("temporal-ambiguity-policy-required", report["blocking_codes"])
        self.assertEqual("temporal_ambiguity_policy_required", report["temporal_ambiguity_scope_summary"]["status"])
        self.assertEqual(2, report["temporal_ambiguity_scope_summary"]["blocking_decision_count"])
        self.assertEqual("blocked", report["gates"]["revision_response"]["status"])

    def test_validated_temporal_ambiguity_policy_decisions_clear_revision_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            temporal_backfill_report = root / "temporal_backfill.json"
            ambiguity_scope_report = root / "temporal_ambiguity_scope.json"
            decision_validation_report = root / "temporal_policy_validation.json"
            _write_json(temporal_backfill_report, _temporal_backfill_payload(ambiguous_count=2))
            _write_json(ambiguity_scope_report, _temporal_ambiguity_scope_payload(ambiguous_count=2))
            _write_json(decision_validation_report, _temporal_policy_decision_validation_payload(passed=True))

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                temporal_backfill_shadow_report=temporal_backfill_report,
                temporal_ambiguity_scope_report=ambiguity_scope_report,
                temporal_ambiguity_policy_decision_validation_report=decision_validation_report,
            )

        by_role = {artifact["role"]: artifact for artifact in report["source_report_artifacts"]}
        self.assertTrue(report["passed"])
        self.assertNotIn("temporal-ambiguity-policy-required", report["blocking_codes"])
        self.assertEqual("ready", report["gates"]["revision_response"]["status"])
        self.assertTrue(report["temporal_ambiguity_policy_decision_validation_summary"]["passed"])
        self.assertIn("temporal_ambiguity_policy_decision_validation_report", by_role)

    def test_clear_temporal_ambiguity_scope_is_not_a_release_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            temporal_backfill_report = root / "temporal_backfill.json"
            ambiguity_scope_report = root / "temporal_ambiguity_scope.json"
            temporal_backfill_payload = _temporal_backfill_payload(ambiguous_count=0)
            temporal_backfill_payload["runtime_copy_scope"] = "repository_chunks_and_approved_vectors_only"
            temporal_backfill_payload["shadow_runtime_runnable"] = False
            _write_json(temporal_backfill_report, temporal_backfill_payload)
            _write_json(ambiguity_scope_report, _temporal_ambiguity_scope_payload(ambiguous_count=0, status="temporal_ambiguity_clear"))

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                temporal_backfill_shadow_report=temporal_backfill_report,
                temporal_ambiguity_scope_report=ambiguity_scope_report,
            )

        self.assertTrue(report["passed"])
        self.assertNotIn("temporal-ambiguity-scope-missing", report["blocking_codes"])
        self.assertNotIn("temporal-ambiguity-policy-required", report["blocking_codes"])
        self.assertEqual("ready", report["gates"]["revision_response"]["status"])
        self.assertEqual("temporal_ambiguity_clear", report["temporal_ambiguity_scope_summary"]["status"])
        self.assertFalse(report["temporal_backfill_shadow_summary"]["shadow_runtime_runnable"])
        self.assertTrue(report["temporal_backfill_shadow_summary"]["shadow_vector_projection_ready"])

    def test_temporal_evidence_runtime_lineage_mismatch_is_warned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            other_data_dir = root / "other-data"
            _seed_runtime(data_dir)
            temporal_coverage_report = root / "temporal_coverage.json"
            drift_report = root / "runtime_version_drift.json"
            _write_json(
                temporal_coverage_report,
                _temporal_coverage_payload(data_dir=data_dir),
            )
            _write_json(
                drift_report,
                _runtime_version_drift_payload(data_dir=other_data_dir),
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                temporal_coverage_report=temporal_coverage_report,
                runtime_version_drift_report=drift_report,
            )

        self.assertTrue(report["passed"])
        self.assertIn("temporal-evidence-runtime-lineage-mismatch", report["warning_codes"])
        self.assertEqual("needs_review", report["gates"]["revision_response"]["status"])
        self.assertEqual(2, report["temporal_evidence_guard_summary"]["runtime_lineage_mismatch_count"])

    def test_strict_temporal_evidence_runtime_lineage_mismatch_blocks_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            other_data_dir = root / "other-data"
            _seed_runtime(data_dir)
            drift_report = root / "runtime_version_drift.json"
            _write_json(drift_report, _runtime_version_drift_payload(data_dir=other_data_dir))

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                runtime_version_drift_report=drift_report,
                strict_temporal_evidence=True,
            )

        self.assertFalse(report["passed"])
        self.assertIn("temporal-evidence-runtime-lineage-mismatch", report["blocking_codes"])
        self.assertEqual("blocked", report["gates"]["revision_response"]["status"])

    def test_temporal_evidence_stale_age_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            temporal_backfill_report = root / "temporal_backfill.json"
            ambiguity_scope_report = root / "temporal_ambiguity_scope.json"
            backfill_payload = _temporal_backfill_payload(ambiguous_count=0)
            backfill_payload["generated_at"] = "2000-01-01T00:00:00+00:00"
            ambiguity_payload = _temporal_ambiguity_scope_payload(
                ambiguous_count=0,
                status="temporal_ambiguity_clear",
            )
            ambiguity_payload["generated_at"] = "2000-01-01T00:30:00+00:00"
            _write_json(temporal_backfill_report, backfill_payload)
            _write_json(ambiguity_scope_report, ambiguity_payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                temporal_backfill_shadow_report=temporal_backfill_report,
                temporal_ambiguity_scope_report=ambiguity_scope_report,
                max_source_report_age_hours=1.0,
            )

        self.assertTrue(report["passed"])
        self.assertIn("temporal-evidence-older-than-threshold", report["warning_codes"])
        self.assertEqual(2, report["temporal_evidence_guard_summary"]["stale_artifact_count"])
        self.assertFalse(report["temporal_evidence_guard_summary"]["strict_temporal_evidence"])

    def test_runtime_version_drift_report_warns_operations_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            drift_report = root / "runtime_version_drift.json"
            _write_json(
                drift_report,
                {
                    "report_type": "runtime_version_drift",
                    "generated_at": "2026-07-09T04:00:00+00:00",
                    "passed": True,
                    "current_versions": {"chunker_version": "0.1.5"},
                    "approved_repository_chunk_count": 2,
                    "vector_record_count": 2,
                    "approved_repository_stale_chunker_count": 2,
                    "approved_repository_stale_chunker_ratio": 1.0,
                    "vector_stale_chunker_count": 2,
                    "vector_stale_chunker_ratio": 1.0,
                    "version_loss": {"loss_count": 0, "mismatch_count": 0},
                    "vector_integrity": {
                        "failure_count": 0,
                        "content_hash_mismatch_count": 0,
                        "verification_hash_mismatch_count": 0,
                        "metadata_missing_required_count": 0,
                        "invalid_approval_status_count": 0,
                        "invalid_security_level_count": 0,
                        "embedded_dimension_mismatch_count": 0,
                        "embedded_integrity_failure_count": 0,
                        "local_path_leak_count": 0,
                    },
                    "reapproval_scope": {
                        "reprocess_requires_reapproval": True,
                        "approved_chunks_with_stale_chunker_count": 2,
                        "approved_chunks_with_approved_hash_count": 2,
                    },
                    "blocker_count": 0,
                    "warning_count": 2,
                    "finding_count": 2,
                    "api_call_count": 0,
                },
            )
            expected_sha = hashlib.sha256(drift_report.read_bytes()).hexdigest()

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                runtime_version_drift_report=drift_report,
            )

        by_role = {artifact["role"]: artifact for artifact in report["source_report_artifacts"]}
        self.assertIn("runtime_version_drift_report", by_role)
        self.assertEqual(expected_sha, by_role["runtime_version_drift_report"]["sha256"])
        self.assertEqual(2, report["runtime_version_drift_summary"]["approved_repository_stale_chunker_count"])
        self.assertEqual(0, report["runtime_version_drift_summary"]["vector_integrity_failure_count"])
        self.assertTrue(report["runtime_version_drift_summary"]["reprocess_requires_reapproval"])
        self.assertIn("runtime-version-drift-evidence", report["warning_codes"])
        self.assertEqual("needs_review", report["gates"]["operations"]["status"])

    def test_revision_impact_reports_are_fingerprinted_and_summarized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            revision_report = root / "revision_impact.json"
            out_md = root / "readiness.md"
            _write_json(revision_report, _revision_impact_payload())
            expected_sha = hashlib.sha256(revision_report.read_bytes()).hexdigest()

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                revision_impact_reports=[revision_report],
                out_md=out_md,
            )
            markdown = out_md.read_text(encoding="utf-8")

        by_role = {artifact["role"]: artifact for artifact in report["source_report_artifacts"]}
        self.assertTrue(report["passed"])
        self.assertEqual([str(revision_report)], report["source_reports"]["revision_impact_reports"])
        self.assertIn("revision_impact_report", by_role)
        self.assertEqual(expected_sha, by_role["revision_impact_report"]["sha256"])
        self.assertEqual(1, report["revision_impact_summary"]["report_count"])
        self.assertEqual(4, report["revision_impact_summary"]["approval_required_count"])
        self.assertEqual(1, report["revision_impact_summary"]["metadata_only_changed_count"])
        self.assertEqual(1, report["revision_impact_summary"]["deindex_required_count"])
        self.assertIn("Revision Impact Evidence", markdown)
        self.assertIn("Approval-required units: 4", markdown)

    def test_runtime_version_drift_blocker_blocks_operations_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            drift_report = root / "runtime_version_drift.json"
            _write_json(
                drift_report,
                {
                    "report_type": "runtime_version_drift",
                    "generated_at": "2026-07-09T04:00:00+00:00",
                    "passed": False,
                    "current_versions": {"chunker_version": "0.1.5"},
                    "approved_repository_chunk_count": 2,
                    "vector_record_count": 2,
                    "approved_repository_stale_chunker_count": 0,
                    "approved_repository_stale_chunker_ratio": 0.0,
                    "vector_stale_chunker_count": 0,
                    "vector_stale_chunker_ratio": 0.0,
                    "version_loss": {"loss_count": 0, "mismatch_count": 0},
                    "vector_integrity": {
                        "failure_count": 1,
                        "content_hash_mismatch_count": 1,
                        "verification_hash_mismatch_count": 1,
                        "metadata_missing_required_count": 0,
                        "invalid_approval_status_count": 0,
                        "invalid_security_level_count": 0,
                        "embedded_dimension_mismatch_count": 0,
                        "embedded_integrity_failure_count": 0,
                        "local_path_leak_count": 0,
                    },
                    "reapproval_scope": {
                        "reprocess_requires_reapproval": False,
                        "approved_chunks_with_stale_chunker_count": 0,
                        "approved_chunks_with_approved_hash_count": 0,
                    },
                    "blocker_count": 1,
                    "warning_count": 0,
                    "finding_count": 1,
                    "api_call_count": 0,
                },
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                runtime_version_drift_report=drift_report,
            )

        self.assertFalse(report["passed"])
        self.assertEqual(1, report["runtime_version_drift_summary"]["vector_integrity_failure_count"])
        self.assertEqual(1, report["runtime_version_drift_summary"]["vector_integrity_content_hash_mismatch_count"])
        self.assertIn("runtime-version-drift-blocker", report["blocking_codes"])
        self.assertEqual("blocked", report["gates"]["operations"]["status"])

    def test_approval_worklist_summary_warns_operations_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            approval_worklist = root / "approval_worklist.json"
            _write_json(
                approval_worklist,
                {
                    "report_type": "approval_worklist",
                    "generated_at": "2026-07-09T05:00:00+00:00",
                    "document_count": 1,
                    "total_chunks": 10,
                    "manual_attention_chunks": 2,
                    "bulk_review_candidate_chunks": 8,
                    "low_risk_batch_review_candidate_chunks": 8,
                    "blocking_review_chunks": 1,
                    "domain_attention_chunks": 1,
                    "stable_false_positive_chunks": 1,
                    "informational_chunks": 4,
                    "no_signal_chunks": 3,
                    "review_priority_tier_counts": {
                        "blocking_review": 1,
                        "domain_attention": 1,
                        "stable_false_positive": 1,
                        "informational": 4,
                        "no_signal": 3,
                    },
                    "action_counts": {"manual_review_first": 1},
                },
            )
            expected_sha = hashlib.sha256(approval_worklist.read_bytes()).hexdigest()

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                approval_worklist_reports=[approval_worklist],
            )

        by_role = {artifact["role"]: artifact for artifact in report["source_report_artifacts"]}
        self.assertIn("approval_worklist_report", by_role)
        self.assertEqual(expected_sha, by_role["approval_worklist_report"]["sha256"])
        self.assertEqual(2, report["approval_workload_summary"]["manual_attention_chunks"])
        self.assertEqual(8, report["approval_workload_summary"]["low_risk_batch_review_candidate_chunks"])
        self.assertEqual(20.0, report["approval_workload_summary"]["manual_attention_rate"])
        self.assertEqual(80.0, report["approval_workload_summary"]["low_risk_batch_review_candidate_rate"])
        self.assertIn("pending-approval-manual-attention", report["warning_codes"])
        self.assertEqual(report["blocking_count"], report["blocker_count"])

    def test_streamlit_document_approval_worklist_counts_pending_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            approval_worklist = root / "approval_worklist.json"
            _write_json(
                approval_worklist,
                {
                    "report_type": "approval_worklist",
                    "generated_at": "2026-07-12T05:00:00+00:00",
                    "document_count": 1,
                    "total_chunks": 208,
                    "approval_status_totals": {"draft": 208},
                    "documents": [
                        {
                            "rank": 1,
                            "suggested_action": "manual_review_first",
                            "document_id": "doc-streamlit",
                            "total_chunks": 208,
                            "approved_chunks": 0,
                            "draft_chunks": 208,
                            "needs_review_chunks": 0,
                            "pending_approval_chunks": 208,
                        }
                    ],
                },
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                approval_worklist_reports=[approval_worklist],
            )

        summary = report["approval_workload_summary"]
        self.assertEqual(208, summary["pending_approval_chunks"])
        self.assertEqual(208, summary["manual_attention_chunks"])
        self.assertEqual(1, summary["manual_review_first_document_count"])
        self.assertEqual({"manual_review_first": 1}, summary["document_suggested_action_counts"])
        self.assertIn("approval-worklist-pending-chunks", report["warning_codes"])
        self.assertIn("pending-approval-manual-attention", report["warning_codes"])

    def test_approval_review_batch_manifest_blocks_operations_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            approval_batches = root / "approval_review_batches.json"
            _write_json(
                approval_batches,
                {
                    "report_type": "approval_review_batch_manifest",
                    "generated_at": "2026-07-09T05:30:00+00:00",
                    "passed": False,
                    "batch_count": 2,
                    "approval_chunk_count": 10,
                    "manual_attention_chunks": 2,
                    "low_risk_batch_review_candidate_chunks": 8,
                    "blocker_count": 1,
                    "warning_count": 0,
                    "review_type_batch_counts": {"manual_attention": 1, "low_risk_batch": 1},
                },
            )
            expected_sha = hashlib.sha256(approval_batches.read_bytes()).hexdigest()

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                approval_review_batch_reports=[approval_batches],
            )

        by_role = {artifact["role"]: artifact for artifact in report["source_report_artifacts"]}
        self.assertIn("approval_review_batch_manifest_report", by_role)
        self.assertEqual(expected_sha, by_role["approval_review_batch_manifest_report"]["sha256"])
        self.assertEqual(2, report["approval_review_batch_summary"]["batch_count"])
        self.assertEqual(10, report["approval_review_batch_summary"]["approval_chunk_count"])
        self.assertIn("approval-review-batch-manifest-blockers", report["blocking_codes"])

    def test_reapproval_worklist_blocker_blocks_operations_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            reapproval_worklist = root / "reapproval_worklist.json"
            _write_json(
                reapproval_worklist,
                {
                    "report_type": "reapproval_worklist",
                    "generated_at": "2026-07-09T05:00:00+00:00",
                    "document_count": 1,
                    "reapproval_candidate_chunks": 100,
                    "high_risk_candidate_chunks": 5,
                    "temporal_sample_candidate_chunks": 20,
                    "low_risk_candidate_chunks": 75,
                    "recommended_initial_review_chunks": 25,
                    "estimated_initial_review_minutes": 9,
                    "approval_provenance_missing_chunks": 7,
                    "approval_provenance_only_chunks": 3,
                    "approval_provenance_missing_field_counts": {
                        "approval_worklist_report_path": 7,
                        "approval_worklist_report_sha256": 7,
                    },
                    "source_vector_integrity_failure_count": 1,
                    "pre_reapproval_blockers": ["source-vector-integrity-failure"],
                    "review_strategy": "fix_vector_integrity_before_reapproval",
                },
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                reapproval_worklist_reports=[reapproval_worklist],
            )

        self.assertFalse(report["passed"])
        self.assertEqual(1, report["reapproval_workload_summary"]["source_vector_integrity_failure_count"])
        self.assertEqual(1, report["reapproval_workload_summary"]["pre_reapproval_blocker_count"])
        self.assertEqual(7, report["reapproval_workload_summary"]["approval_provenance_missing_chunks"])
        self.assertEqual(3, report["reapproval_workload_summary"]["approval_provenance_only_chunks"])
        self.assertEqual(
            {"approval_worklist_report_path": 7, "approval_worklist_report_sha256": 7},
            report["reapproval_workload_summary"]["approval_provenance_missing_field_counts"],
        )
        self.assertEqual(0.75, report["reapproval_workload_summary"]["initial_review_reduction_ratio"])
        self.assertIn("reapproval-worklist-blockers", report["blocking_codes"])

    def test_reapproval_review_batch_manifest_blocks_operations_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            reapproval_worklist = root / "reapproval_worklist.json"
            reapproval_batches = root / "reapproval_review_batches.json"
            _write_json(
                reapproval_worklist,
                {
                    "report_type": "reapproval_worklist",
                    "generated_at": "2026-07-09T05:00:00+00:00",
                    "document_count": 1,
                    "reapproval_candidate_chunks": 10,
                    "recommended_initial_review_chunks": 0,
                    "estimated_initial_review_minutes": 0,
                    "source_vector_integrity_failure_count": 0,
                    "pre_reapproval_blockers": [],
                    "review_strategy": "reapprove_and_reindex",
                },
            )
            _write_json(
                reapproval_batches,
                {
                    "report_type": "reapproval_review_batch_manifest",
                    "generated_at": "2026-07-09T06:00:00+00:00",
                    "passed": False,
                    "candidate_count": 10,
                    "selected_candidate_count": 10,
                    "batch_count": 2,
                    "reapproval_chunk_count": 10,
                    "max_chunks_per_batch": 5,
                    "blocker_count": 1,
                    "warning_count": 0,
                    "action_batch_counts": {"reapprove_and_reindex": 2},
                    "action_chunk_counts": {"reapprove_and_reindex": 10},
                    "risk_tier_batch_counts": {"high": 2},
                    "risk_tier_chunk_counts": {"high": 10},
                },
            )
            expected_sha = hashlib.sha256(reapproval_batches.read_bytes()).hexdigest()

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                reapproval_worklist_reports=[reapproval_worklist],
                reapproval_review_batch_reports=[reapproval_batches],
            )

        by_role = {artifact["role"]: artifact for artifact in report["source_report_artifacts"]}
        self.assertIn("reapproval_review_batch_manifest_report", by_role)
        self.assertEqual(expected_sha, by_role["reapproval_review_batch_manifest_report"]["sha256"])
        self.assertEqual(2, report["reapproval_review_batch_summary"]["batch_count"])
        self.assertEqual(10, report["reapproval_review_batch_summary"]["selected_candidate_count"])
        self.assertEqual({"high": 10}, report["reapproval_review_batch_summary"]["risk_tier_chunk_counts"])
        self.assertIn("reapproval-review-batch-manifest-blockers", report["blocking_codes"])

    def test_reapproval_review_batch_count_mismatch_warns_operations_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            reapproval_worklist = root / "reapproval_worklist.json"
            reapproval_batches = root / "reapproval_review_batches.json"
            _write_json(
                reapproval_worklist,
                {
                    "report_type": "reapproval_worklist",
                    "generated_at": "2026-07-09T05:00:00+00:00",
                    "document_count": 1,
                    "reapproval_candidate_chunks": 10,
                    "recommended_initial_review_chunks": 0,
                    "estimated_initial_review_minutes": 0,
                    "source_vector_integrity_failure_count": 0,
                    "pre_reapproval_blockers": [],
                    "review_strategy": "reapprove_and_reindex",
                },
            )
            _write_json(
                reapproval_batches,
                {
                    "report_type": "reapproval_review_batch_manifest",
                    "generated_at": "2026-07-09T06:00:00+00:00",
                    "passed": True,
                    "candidate_count": 12,
                    "selected_candidate_count": 10,
                    "batch_count": 2,
                    "reapproval_chunk_count": 10,
                    "blocker_count": 0,
                    "warning_count": 0,
                },
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                reapproval_worklist_reports=[reapproval_worklist],
                reapproval_review_batch_reports=[reapproval_batches],
            )

        self.assertIn("reapproval-review-batch-incomplete", report["warning_codes"])

    def test_reapproval_decision_validation_missing_blocks_operations_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            reapproval_worklist = root / "reapproval_worklist.json"
            reapproval_batches = root / "reapproval_review_batches.json"
            _write_json(
                reapproval_worklist,
                {
                    "report_type": "reapproval_worklist",
                    "generated_at": "2026-07-09T05:00:00+00:00",
                    "document_count": 1,
                    "reapproval_candidate_chunks": 2,
                    "recommended_initial_review_chunks": 0,
                    "estimated_initial_review_minutes": 0,
                    "source_vector_integrity_failure_count": 0,
                    "pre_reapproval_blockers": [],
                    "review_strategy": "reapprove_and_reindex",
                },
            )
            _write_json(
                reapproval_batches,
                {
                    "report_type": "reapproval_review_batch_manifest",
                    "generated_at": "2026-07-09T06:00:00+00:00",
                    "passed": True,
                    "candidate_count": 2,
                    "selected_candidate_count": 2,
                    "batch_count": 1,
                    "reapproval_chunk_count": 2,
                    "blocker_count": 0,
                    "warning_count": 0,
                },
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                reapproval_worklist_reports=[reapproval_worklist],
                reapproval_review_batch_reports=[reapproval_batches],
            )

        self.assertFalse(report["passed"])
        self.assertIn("reapproval-decision-validation-missing", report["blocking_codes"])
        self.assertEqual("blocked", report["gates"]["operations"]["status"])

    def test_reapproval_decision_validation_blockers_block_operations_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            reapproval_worklist = root / "reapproval_worklist.json"
            reapproval_batches = root / "reapproval_review_batches.json"
            reapproval_validation = root / "reapproval_decision_validation.json"
            _write_json(
                reapproval_worklist,
                {
                    "report_type": "reapproval_worklist",
                    "generated_at": "2026-07-09T05:00:00+00:00",
                    "document_count": 1,
                    "reapproval_candidate_chunks": 2,
                    "recommended_initial_review_chunks": 0,
                    "estimated_initial_review_minutes": 0,
                    "source_vector_integrity_failure_count": 0,
                    "pre_reapproval_blockers": [],
                    "review_strategy": "reapprove_and_reindex",
                },
            )
            _write_json(
                reapproval_batches,
                {
                    "report_type": "reapproval_review_batch_manifest",
                    "generated_at": "2026-07-09T06:00:00+00:00",
                    "passed": True,
                    "candidate_count": 2,
                    "selected_candidate_count": 2,
                    "batch_count": 1,
                    "reapproval_chunk_count": 2,
                    "blocker_count": 0,
                    "warning_count": 0,
                },
            )
            _write_json(
                reapproval_validation,
                {
                    "report_type": "reapproval_decision_validation",
                    "generated_at": "2026-07-09T06:30:00+00:00",
                    "passed": False,
                    "release_gate_status": "blocked_pending_operator_decisions",
                    "blocking_count": 1,
                    "warning_count": 0,
                    "expected_batch_count": 1,
                    "decision_row_count": 1,
                    "complete_row_count": 0,
                    "blank_or_incomplete_row_count": 1,
                    "operator_decision_counts": {"blank": 1},
                },
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                reapproval_worklist_reports=[reapproval_worklist],
                reapproval_review_batch_reports=[reapproval_batches],
                reapproval_decision_validation_reports=[reapproval_validation],
            )

        self.assertFalse(report["passed"])
        self.assertEqual(1, report["reapproval_decision_validation_summary"]["blocking_count"])
        self.assertEqual(
            {"blocked_pending_operator_decisions": 1},
            report["reapproval_decision_validation_summary"]["release_gate_status_counts"],
        )
        self.assertIn("reapproval-decision-validation-blockers", report["blocking_codes"])

    def test_cli_accepts_reapproval_review_batch_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            approval_worklist_report = root / "approval_worklist.json"
            approval_review_batch_report = root / "approval_review_batches.json"
            reapproval_worklist_report = root / "reapproval_worklist.json"
            reapproval_review_batch_report = root / "reapproval_review_batches.json"
            reapproval_decision_validation_report = root / "reapproval_decision_validation.json"
            reapproval_apply_plan_report = root / "reapproval_apply_plan.json"
            temporal_ambiguity_scope_report = root / "temporal_ambiguity_scope.json"
            revision_impact_report = root / "revision_impact.json"
            _write_clean_approval_evidence_reports(
                approval_worklist_report=approval_worklist_report,
                approval_review_batch_report=approval_review_batch_report,
                reapproval_worklist_report=reapproval_worklist_report,
                reapproval_review_batch_report=reapproval_review_batch_report,
                reapproval_decision_validation_report=reapproval_decision_validation_report,
            )
            _write_reapproval_apply_plan(reapproval_apply_plan_report)
            _write_json(
                temporal_ambiguity_scope_report,
                _temporal_ambiguity_scope_payload(ambiguous_count=0, status="temporal_ambiguity_clear"),
            )
            _write_json(revision_impact_report, _revision_impact_payload())
            output = io.StringIO()

            exit_code = audit_main(
                [
                    "--runtime-data-dir",
                    str(data_dir),
                    "--flat-storage",
                    "--approval-worklist-report",
                    str(approval_worklist_report),
                    "--approval-review-batch-report",
                    str(approval_review_batch_report),
                    "--reapproval-worklist-report",
                    str(reapproval_worklist_report),
                    "--reapproval-review-batch-report",
                    str(reapproval_review_batch_report),
                    "--reapproval-decision-validation-report",
                    str(reapproval_decision_validation_report),
                    "--reapproval-apply-plan-report",
                    str(reapproval_apply_plan_report),
                    "--temporal-ambiguity-scope-report",
                    str(temporal_ambiguity_scope_report),
                    "--revision-impact-report",
                    str(revision_impact_report),
                    "--max-source-report-age-hours",
                    "999999",
                    "--strict-temporal-evidence",
                ],
                stdout=output,
            )
            report = json.loads(output.getvalue())

        self.assertEqual(0, exit_code)
        self.assertEqual(
            [str(reapproval_review_batch_report)],
            report["source_reports"]["reapproval_review_batch_reports"],
        )
        self.assertEqual(
            [str(reapproval_decision_validation_report)],
            report["source_reports"]["reapproval_decision_validation_reports"],
        )
        self.assertEqual([str(reapproval_apply_plan_report)], report["source_reports"]["reapproval_apply_plan_reports"])
        self.assertEqual(
            str(temporal_ambiguity_scope_report),
            report["source_reports"]["temporal_ambiguity_scope_report"],
        )
        self.assertEqual([str(revision_impact_report)], report["source_reports"]["revision_impact_reports"])
        self.assertEqual(1, report["reapproval_review_batch_summary"]["batch_count"])
        self.assertIn(
            "reapproval_review_batch_manifest_report",
            {artifact["role"] for artifact in report["source_report_artifacts"]},
        )
        self.assertIn(
            "temporal_ambiguity_scope_report",
            {artifact["role"] for artifact in report["source_report_artifacts"]},
        )
        self.assertIn(
            "revision_impact_report",
            {artifact["role"] for artifact in report["source_report_artifacts"]},
        )
        self.assertIn(
            "reapproval_decision_validation_report",
            {artifact["role"] for artifact in report["source_report_artifacts"]},
        )
        self.assertIn(
            "reapproval_apply_plan_report",
            {artifact["role"] for artifact in report["source_report_artifacts"]},
        )
        self.assertEqual(0, report["reapproval_apply_plan_summary"]["unsafe_contract_violation_count"])
        self.assertTrue(report["temporal_evidence_guard_summary"]["strict_temporal_evidence"])
        self.assertEqual(0, report["temporal_evidence_guard_summary"]["stale_artifact_count"])

    def test_reapproval_apply_plan_safety_contract_blocks_operations_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            approval_worklist_report = root / "approval_worklist.json"
            approval_review_batch_report = root / "approval_review_batches.json"
            reapproval_worklist_report = root / "reapproval_worklist.json"
            reapproval_review_batch_report = root / "reapproval_review_batches.json"
            reapproval_decision_validation_report = root / "reapproval_decision_validation.json"
            reapproval_apply_plan_report = root / "reapproval_apply_plan.json"
            _write_clean_approval_evidence_reports(
                approval_worklist_report=approval_worklist_report,
                approval_review_batch_report=approval_review_batch_report,
                reapproval_worklist_report=reapproval_worklist_report,
                reapproval_review_batch_report=reapproval_review_batch_report,
                reapproval_decision_validation_report=reapproval_decision_validation_report,
            )
            _write_reapproval_apply_plan(reapproval_apply_plan_report, unsafe=True)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                approval_worklist_reports=[approval_worklist_report],
                approval_review_batch_reports=[approval_review_batch_report],
                reapproval_worklist_reports=[reapproval_worklist_report],
                reapproval_review_batch_reports=[reapproval_review_batch_report],
                reapproval_decision_validation_reports=[reapproval_decision_validation_report],
                reapproval_apply_plan_reports=[reapproval_apply_plan_report],
            )

        self.assertFalse(report["passed"])
        self.assertIn("reapproval-apply-plan-safety-contract-missing", report["blocking_codes"])
        self.assertGreater(report["reapproval_apply_plan_summary"]["unsafe_contract_violation_count"], 0)

    def test_smoke_documents_block_product_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            _seed_runtime(data_dir, smoke_document=True)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
            )

        self.assertFalse(report["passed"])
        self.assertIn("smoke-docs-in-runtime", report["blocking_codes"])

    def test_failed_public_batch_readiness_blocks_parsing_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            public_readiness_report = root / "public_readiness.json"
            _write_json(
                public_readiness_report,
                {
                    "status": "needs_attention",
                    "passed": False,
                    "summary": {
                        "input_count": 2,
                        "successful_count": 1,
                        "failed_count": 1,
                        "ocr_required_count": 1,
                        "recommendation_total": 3,
                    },
                    "checks": [
                        {"name": "all_inputs_successful", "passed": False},
                        {"name": "no_ocr_required_rows", "passed": False},
                    ],
                },
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                public_readiness_report=public_readiness_report,
            )

        self.assertFalse(report["passed"])
        self.assertIn("public-batch-readiness-failed", report["blocking_codes"])
        self.assertEqual("blocked", report["gates"]["parsing_accuracy"]["status"])
        self.assertEqual(["all_inputs_successful", "no_ocr_required_rows"], report["public_readiness_summary"]["failed_checks"])

    def test_review_tolerance_public_readiness_warns_parsing_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            public_readiness_report = root / "public_batch_readiness_review_tolerance.json"
            _write_json(
                public_readiness_report,
                {
                    "readiness_profile": "review_tolerance",
                    "strict_release_evidence": False,
                    "status": "public_batch_ready",
                    "passed": True,
                    "thresholds": {
                        "min_average_quality": 98.0,
                        "max_failed_info": 0,
                        "max_recommendations": 6,
                        "max_table_attention": 0,
                        "max_current_ai_tokens": 0,
                    },
                    "summary": {
                        "input_count": 2,
                        "successful_count": 2,
                        "failed_count": 0,
                        "ocr_required_count": 0,
                        "recommendation_total": 6,
                    },
                    "checks": [
                        {"name": "all_inputs_successful", "passed": True},
                        {"name": "recommendation_count_within_tolerance", "passed": True},
                    ],
                },
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                public_readiness_report=public_readiness_report,
            )

        self.assertTrue(report["passed"])
        self.assertTrue(report["public_readiness_summary"]["review_tolerance_evidence"])
        self.assertEqual("review_tolerance", report["public_readiness_summary"]["readiness_profile"])
        self.assertFalse(report["public_readiness_summary"]["strict_release_evidence"])
        self.assertEqual(6, report["public_readiness_summary"]["thresholds"]["max_recommendations"])
        self.assertIn("public-readiness-review-tolerance-evidence", report["warning_codes"])
        self.assertEqual("needs_review", report["gates"]["parsing_accuracy"]["status"])
        finding = next(
            item
            for item in report["gates"]["parsing_accuracy"]["findings"]
            if item["code"] == "public-readiness-review-tolerance-evidence"
        )
        self.assertIn("recommendations<=6", finding["detail"])

    def test_low_parser_goldset_f1_blocks_parsing_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            parser_goldset_report = root / "parser_goldset.json"
            payload = _parser_goldset_score_payload()
            payload["overall"]["f1"] = 72.5
            _write_json(parser_goldset_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                parser_goldset_score_report=parser_goldset_report,
            )

        self.assertFalse(report["passed"])
        self.assertIn("parser-goldset-f1-low", report["blocking_codes"])
        self.assertEqual("blocked", report["gates"]["parsing_accuracy"]["status"])

    def test_parser_goldset_score_issues_warn_parsing_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            parser_goldset_report = root / "parser_goldset.json"
            payload = _parser_goldset_score_payload()
            payload["summary"]["issue_count"] = 2
            _write_json(parser_goldset_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                parser_goldset_score_report=parser_goldset_report,
            )

        self.assertIn("parser-goldset-score-issues", report["warning_codes"])
        self.assertEqual("needs_review", report["gates"]["parsing_accuracy"]["status"])
        finding = next(
            item
            for item in report["gates"]["parsing_accuracy"]["findings"]
            if item["code"] == "parser-goldset-score-issues"
        )
        self.assertIn("issue_count=2", finding["detail"])
        self.assertIn("pending_document_count=0", finding["detail"])

    def test_parser_goldset_scope_exclusions_warn_parsing_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            parser_goldset_report = root / "parser_goldset.json"
            payload = _parser_goldset_score_payload()
            payload["summary"]["document_count"] = 3
            payload["summary"]["scored_document_count"] = 2
            payload["summary"]["excluded_document_count"] = 1
            payload["completion"]["completed_document_count"] = 3
            payload["completion"]["scored_document_count"] = 2
            payload["completion"]["excluded_document_count"] = 1
            _write_json(parser_goldset_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                parser_goldset_score_report=parser_goldset_report,
            )

        self.assertIn("parser-goldset-scope-exclusions", report["warning_codes"])
        self.assertEqual(1, report["parser_goldset_score_summary"]["excluded_document_count"])
        self.assertEqual("needs_review", report["gates"]["parsing_accuracy"]["status"])

    def test_required_parser_goldset_score_missing_blocks_parsing_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            _seed_runtime(data_dir)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                require_parser_goldset_score=True,
            )

        self.assertFalse(report["passed"])
        self.assertIn("parser-goldset-score-missing", report["blocking_codes"])
        self.assertEqual("blocked", report["gates"]["parsing_accuracy"]["status"])

    def test_required_parser_goldset_f1_missing_detail_names_root_cause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            parser_goldset_report = root / "parser_goldset.json"
            payload = _parser_goldset_score_payload()
            payload["overall"].pop("f1")
            _write_json(parser_goldset_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                parser_goldset_score_report=parser_goldset_report,
                require_parser_goldset_score=True,
            )

        self.assertFalse(report["passed"])
        self.assertIn("parser-goldset-f1-missing", report["blocking_codes"])
        finding = next(
            item
            for item in report["gates"]["parsing_accuracy"]["findings"]
            if item["code"] == "parser-goldset-f1-missing"
        )
        self.assertIn("overall_f1=missing", finding["detail"])
        self.assertIn("missing_structure_score_count=0", finding["detail"])

    def test_required_parser_goldset_score_issues_block_parsing_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            parser_goldset_report = root / "parser_goldset.json"
            payload = _parser_goldset_score_payload()
            payload["summary"]["issue_count"] = 2
            payload["completion"]["missing_structure_score_count"] = 3
            payload["completion"]["blocking_issue_codes"] = {"matched-count-missing": 2}
            _write_json(parser_goldset_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                parser_goldset_score_report=parser_goldset_report,
                require_parser_goldset_score=True,
            )

        self.assertFalse(report["passed"])
        self.assertIn("parser-goldset-score-issues", report["blocking_codes"])
        self.assertEqual("blocked", report["gates"]["parsing_accuracy"]["status"])
        finding = next(
            item
            for item in report["gates"]["parsing_accuracy"]["findings"]
            if item["code"] == "parser-goldset-score-issues"
        )
        self.assertIn("issue_count=2", finding["detail"])
        self.assertIn("missing_structure_score_count=3", finding["detail"])
        self.assertIn("blocking_issue_codes=matched-count-missing=2", finding["detail"])

    def test_required_parser_goldset_quality_claim_not_ready_blocks_parsing_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            parser_goldset_report = root / "parser_goldset.json"
            payload = _parser_goldset_score_payload()
            payload["summary"]["ready_for_quality_claim"] = False
            payload["completion"]["ready_for_quality_claim"] = False
            payload["completion"]["pending_document_count"] = 1
            _write_json(parser_goldset_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                parser_goldset_score_report=parser_goldset_report,
                require_parser_goldset_score=True,
            )

        self.assertFalse(report["passed"])
        self.assertIn("parser-goldset-quality-claim-not-ready", report["blocking_codes"])
        self.assertEqual("blocked", report["gates"]["parsing_accuracy"]["status"])
        finding = next(
            item
            for item in report["gates"]["parsing_accuracy"]["findings"]
            if item["code"] == "parser-goldset-quality-claim-not-ready"
        )
        self.assertIn("pending_document_count=1", finding["detail"])
        self.assertIn("overall_f1=96.0", finding["detail"])

    def test_parser_review_flags_warn_parsing_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            _seed_runtime(data_dir, review_attention=True)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
            )

        self.assertTrue(report["passed"])
        self.assertIn("parser-review-flags-present", report["warning_codes"])
        self.assertEqual("needs_review", report["gates"]["parsing_accuracy"]["status"])
        self.assertEqual(1, report["runtime_summary"]["review_attention_chunk_count"])
        self.assertEqual(0, report["runtime_summary"]["review_attention_acknowledged_chunk_count"])
        self.assertEqual(1, report["runtime_summary"]["review_attention_unacknowledged_chunk_count"])
        self.assertEqual(["chunk-leave"], report["runtime_summary"]["review_attention_sample_chunk_ids"])
        self.assertEqual(
            {
                "table_review_flags:row_review_required": 1,
                "table_review_required": 1,
            },
            report["runtime_summary"]["review_attention_flag_counts"],
        )

    def test_acknowledged_parser_review_flags_do_not_warn_parsing_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            _seed_runtime(data_dir, review_attention=True)
            journal_path = data_dir / "repository" / "journals" / "approvals.jsonl"
            records = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
            for record in records:
                record["human_review_confirmed"] = True
                record["ai_review_confirmed"] = True
                events = []
                for chunk_id in record.get("chunk_ids") or []:
                    events.extend(
                        [
                            {"event": "approved", "chunk_id": chunk_id},
                            {"event": "human_review_confirmed", "chunk_id": chunk_id},
                            {"event": "ai_review_confirmed", "chunk_id": chunk_id},
                        ]
                    )
                record["review_decision_events"] = events
            journal_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                encoding="utf-8",
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
            )

        self.assertTrue(report["passed"])
        self.assertNotIn("parser-review-flags-present", report["warning_codes"])
        self.assertEqual("needs_review", report["gates"]["parsing_accuracy"]["status"])
        self.assertEqual(1, report["runtime_summary"]["review_attention_chunk_count"])
        self.assertEqual(1, report["runtime_summary"]["review_attention_acknowledged_chunk_count"])
        self.assertEqual(0, report["runtime_summary"]["review_attention_unacknowledged_chunk_count"])

    def test_unflagged_review_events_do_not_acknowledge_flagged_parser_review_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            _seed_runtime(data_dir, review_attention=True)
            journal_path = data_dir / "repository" / "journals" / "approvals.jsonl"
            records = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
            for record in records:
                if record.get("chunk_ids") != ["chunk-pay"]:
                    continue
                record["human_review_confirmed"] = True
                record["ai_review_confirmed"] = True
                record["review_decision_events"] = [
                    {"event": "approved", "chunk_id": "chunk-pay"},
                    {"event": "human_review_confirmed", "chunk_id": "chunk-pay"},
                    {"event": "ai_review_confirmed", "chunk_id": "chunk-pay"},
                ]
            journal_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                encoding="utf-8",
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
            )

        self.assertIn("parser-review-flags-present", report["warning_codes"])
        self.assertEqual(1, report["runtime_summary"]["review_attention_chunk_count"])
        self.assertEqual(0, report["runtime_summary"]["review_attention_acknowledged_chunk_count"])
        self.assertEqual(1, report["runtime_summary"]["review_attention_unacknowledged_chunk_count"])
        self.assertEqual(
            ["doc-public-regulation:chunk-leave"],
            report["runtime_summary"]["review_attention_unacknowledged_chunk_keys"],
        )

    def test_parser_uncertainty_warns_parsing_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            _seed_runtime(data_dir)
            chunk_path = data_dir / "repository" / "doc-public-regulation_chunks.json"
            chunks = json.loads(chunk_path.read_text(encoding="utf-8"))
            chunks[0]["metadata"].update(
                {
                    "parser_uncertainty_source": "pdf",
                    "parser_uncertainty_risk_level": "high",
                    "parser_uncertainty_flags": ["ocr_required"],
                    "parser_uncertainty_recommendation": "run_ocr",
                }
            )
            chunk_path.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
            )

        self.assertTrue(report["passed"])
        self.assertIn("parser-review-flags-present", report["warning_codes"])
        self.assertEqual("needs_review", report["gates"]["parsing_accuracy"]["status"])
        self.assertEqual(1, report["runtime_summary"]["review_attention_chunk_count"])
        self.assertEqual(1, report["runtime_summary"]["review_attention_unacknowledged_chunk_count"])
        self.assertEqual(["chunk-leave"], report["runtime_summary"]["review_attention_sample_chunk_ids"])
        self.assertEqual(
            {
                "parser_uncertainty_flags:ocr_required": 1,
                "parser_uncertainty_recommendation:run_ocr": 1,
                "parser_uncertainty_risk_level:high": 1,
            },
            report["runtime_summary"]["review_attention_flag_counts"],
        )

    def test_draft_repository_chunks_do_not_block_index_or_review_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            _seed_runtime(data_dir, vector_count=1)
            chunk_path = data_dir / "repository" / "doc-public-regulation_chunks.json"
            chunks = json.loads(chunk_path.read_text(encoding="utf-8"))
            chunks[1]["metadata"]["approval_status"] = "draft"
            chunks[1]["metadata"].pop("approval_id", None)
            chunks[1]["metadata"]["table_review_required"] = True
            chunks[1]["metadata"]["table_review_flags"] = ["row_review_required"]
            chunk_path.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
            )

        self.assertTrue(report["runtime_summary"]["full_index_match"])
        self.assertEqual(1, report["runtime_summary"]["approved_repository_chunk_count"])
        self.assertEqual(1, report["runtime_summary"]["unapproved_repository_chunk_count"])
        self.assertEqual(0, report["runtime_summary"]["review_attention_chunk_count"])
        self.assertNotIn("runtime-not-fully-indexed", report["blocking_codes"])
        self.assertNotIn("parser-review-flags-present", report["warning_codes"])

    def test_generic_only_profile_warns_generality_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            batch_report = root / "batch.json"
            _write_json(
                batch_report,
                {
                    "successful_count": 2,
                    "failed_count": 0,
                    "ocr_required_count": 0,
                    "rows": [
                        {
                            "filename": "agency_a_rules.hwp",
                            "file_type": "hwp",
                            "quality_score": 100.0,
                            "quality_passed": True,
                            "profile_id": "public_institution",
                            "institution_name": "기관A",
                        },
                        {
                            "filename": "agency_b_rules.pdf",
                            "file_type": "pdf",
                            "quality_score": 100.0,
                            "quality_passed": True,
                            "profile_id": "public_institution",
                            "institution_name": "기관B",
                        },
                    ],
                },
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                batch_reports=[batch_report],
            )

        self.assertIn("institution-profile-generic-only", report["warning_codes"])
        self.assertEqual("needs_review", report["gates"]["generality"]["status"])

    def test_default_public_profile_only_warns_generality_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            batch_report = root / "batch.json"
            _write_json(
                batch_report,
                {
                    "successful_count": 1,
                    "failed_count": 0,
                    "ocr_required_count": 0,
                    "rows": [
                        {
                            "filename": "agency_a_rules.hwp",
                            "file_type": "hwp",
                            "quality_score": 100.0,
                            "quality_passed": True,
                            "profile_id": "default-public-institution",
                            "institution_name": "Agency A",
                        }
                    ],
                },
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                batch_reports=[batch_report],
            )

        self.assertIn("institution-profile-generic-only", report["warning_codes"])
        self.assertEqual("needs_review", report["gates"]["generality"]["status"])

    def test_missing_hwpx_warns_generality_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            batch_report = root / "batch.json"
            _write_json(
                batch_report,
                {
                    "successful_count": 2,
                    "failed_count": 0,
                    "ocr_required_count": 0,
                    "rows": [
                        {
                            "filename": "agency_a_rules.hwp",
                            "file_type": "hwp",
                            "quality_score": 100.0,
                            "quality_passed": True,
                            "profile_id": "public_portal-agency-a",
                            "institution_name": "기관A",
                        },
                        {
                            "filename": "agency_b_rules.pdf",
                            "file_type": "pdf",
                            "quality_score": 100.0,
                            "quality_passed": True,
                            "profile_id": "public_portal-agency-b",
                            "institution_name": "기관B",
                        },
                    ],
                },
            )

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                batch_reports=[batch_report],
            )

        self.assertTrue(report["passed"])
        self.assertIn("file-format-diversity-low", report["warning_codes"])
        self.assertEqual("needs_review", report["gates"]["generality"]["status"])

    def test_profile_provenance_unknown_profile_blocks_generality_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            profile_provenance_report = root / "profile_provenance.json"
            payload = _profile_provenance_payload()
            payload["passed"] = False
            payload["finding_count"] = 1
            payload["blocker_count"] = 1
            payload["unknown_profile_counts"] = {"public_institution": 2}
            payload["findings"] = [
                {
                    "severity": "blocker",
                    "code": "unknown-batch-profile-id",
                    "detail": "Some batch rows reference profile ids absent from the institution registry.",
                }
            ]
            _write_json(profile_provenance_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                profile_provenance_report=profile_provenance_report,
            )

        self.assertFalse(report["passed"])
        self.assertIn("profile-provenance-failed", report["blocking_codes"])
        self.assertIn("profile-provenance-unknown", report["blocking_codes"])
        self.assertEqual("blocked", report["gates"]["generality"]["status"])

    def test_profile_provenance_warning_only_warns_generality_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_runtime(data_dir)
            profile_provenance_report = root / "profile_provenance.json"
            payload = _profile_provenance_payload()
            payload["finding_count"] = 1
            payload["warning_count"] = 1
            payload["findings"] = [
                {
                    "severity": "warning",
                    "code": "generic-profile-only",
                    "detail": "Batch rows use only the generic public_institution profile id.",
                }
            ]
            _write_json(profile_provenance_report, payload)

            report = build_mcp_product_readiness_audit(
                runtime_data_dir=data_dir,
                tenant_storage_isolation=False,
                profile_provenance_report=profile_provenance_report,
            )

        self.assertTrue(report["passed"])
        self.assertIn("profile-provenance-warnings", report["warning_codes"])
        self.assertEqual("needs_review", report["gates"]["generality"]["status"])


def _seed_runtime(
    data_dir: Path,
    *,
    vector_count: int = 2,
    smoke_document: bool = False,
    review_attention: bool = False,
    include_approval_provenance: bool = True,
    include_approval_journal: bool = True,
) -> None:
    repository_dir = data_dir / "repository"
    vector_dir = data_dir / "vector_db" / "default"
    repository_dir.mkdir(parents=True)
    vector_dir.mkdir(parents=True)
    document_id = "doc_mcp_smoke_v1" if smoke_document else "doc-public-regulation"
    chunks = [
        _chunk(
            document_id=document_id,
            chunk_id="chunk-leave",
            article_no="제31조",
            article_title="휴직의 운영",
            text="제31조(휴직의 운영) 휴직 사유가 소멸된 때에는 30일 이내에 신고하여야 한다.",
            metadata_patch=(
                {
                    "table_review_required": True,
                    "table_review_flags": ["row_review_required"],
                }
                if review_attention
                else None
            ),
            include_approval_provenance=include_approval_provenance,
        ),
        _chunk(
            document_id=document_id,
            chunk_id="chunk-pay",
            include_approval_provenance=include_approval_provenance,
            article_no="제24조",
            article_title="연봉의 지급 방법",
            text="제24조(연봉의 지급 방법) 성과연봉은 6월과 12월에 지급한다.",
        ),
    ]
    _write_json(repository_dir / f"{document_id}_chunks.json", chunks)
    records = [_vector_record(chunk) for chunk in chunks[:vector_count]]
    vector_lines = [json.dumps(record, ensure_ascii=False) for record in records]
    (vector_dir / "approved_vectors.jsonl").write_text("\n".join(vector_lines) + "\n", encoding="utf-8")
    if include_approval_journal and include_approval_provenance:
        journal_dir = repository_dir / "journals"
        journal_dir.mkdir(parents=True, exist_ok=True)
        journal_records = [_approval_journal_record(chunk) for chunk in chunks[:vector_count]]
        journal_lines = [json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in journal_records]
        (journal_dir / "approvals.jsonl").write_text("\n".join(journal_lines) + "\n", encoding="utf-8")


def _chunk(
    *,
    document_id: str,
    chunk_id: str,
    article_no: str,
    article_title: str,
    text: str,
    tenant_id: str = "default",
    metadata_patch: dict | None = None,
    include_approval_provenance: bool = True,
) -> dict:
    approval_provenance = (
        {
            "approval_worklist_report_path": "reports/approval_worklist.json",
            "approval_worklist_report_sha256": "a" * 64,
            "approval_review_batch_manifest_path": "reports/approval_review_batches.json",
            "approval_review_batch_manifest_sha256": "b" * 64,
            "approval_review_batch_id": "approval-batch-001",
            "approval_review_batch_chunk_fingerprint": "c" * 64,
            "approval_review_strategy": "low_risk_batch_review",
        }
        if include_approval_provenance
        else {}
    )
    return {
        "document_id": document_id,
        "chunk_id": chunk_id,
        "chunk_type": "article",
        "text": text,
        "retrieval_text": text,
        "metadata": {
            "document_id": document_id,
            "chunk_id": chunk_id,
            "tenant_id": tenant_id,
            "document_name": "공공기관 규정집",
            "institution_name": "테스트기관",
            "profile_id": "public_portal-public-rule",
            "source_system": "PUBLIC_PORTAL",
            "source_url": "https://example.test/public_portal/doc-public-regulation",
            "chunk_type": "article",
            "article_no": article_no,
            "article_title": article_title,
            "approval_status": "approved",
            "approval_id": f"approval-{chunk_id}",
            "approved_content_hash": f"approved-hash-{chunk_id}",
            "regulation_id": "reg-public-rule",
            "regulation_version": "v1",
            "regulation_status": "approved",
            "effective_from": "2026-01-01",
            "effective_to": "",
            "repealed_at": "",
            **approval_provenance,
            "security_level": "internal",
            "answer_profile_version": "answer-profile-v1",
            "answer_intents": ["rule_qa"],
            "answer_keywords": [article_title],
            "effective_date": "2026-01-01",
            "revision_date": "2026-01-01",
            **(metadata_patch or {}),
        },
    }


def _vector_record(chunk: dict) -> dict:
    metadata = dict(chunk["metadata"])
    return {
        "id": f"{chunk['document_id']}:{chunk['chunk_id']}",
        "document_id": chunk["document_id"],
        "chunk_id": chunk["chunk_id"],
        "text": chunk["retrieval_text"],
        "metadata": metadata,
        "content_hash": f"content-hash-{chunk['chunk_id']}",
    }


def _approval_journal_record(chunk: dict) -> dict:
    metadata = dict(chunk["metadata"])
    chunk_id = str(chunk["chunk_id"])
    worklist_evidence = {
        "worklist_report_path": metadata["approval_worklist_report_path"],
        "worklist_report_sha256": metadata["approval_worklist_report_sha256"],
        "review_batch_manifest_path": metadata["approval_review_batch_manifest_path"],
        "review_batch_manifest_sha256": metadata["approval_review_batch_manifest_sha256"],
        "review_batch_id": metadata["approval_review_batch_id"],
        "review_batch_chunk_fingerprint": metadata["approval_review_batch_chunk_fingerprint"],
        "review_strategy": metadata["approval_review_strategy"],
    }
    return {
        "approval_record_id": f"approval-record-{chunk_id}",
        "approval_id": metadata["approval_id"],
        "document_id": chunk["document_id"],
        "chunk_ids": [chunk_id],
        "approved_content_hashes": {chunk_id: metadata["approved_content_hash"]},
        "approved_chunks": [
            {
                "chunk_id": chunk_id,
                "approved_content_hash": metadata["approved_content_hash"],
                "worklist_evidence": worklist_evidence,
            }
        ],
        "approved_by": "auditor",
        "approved_at": "2026-07-10T00:00:00+00:00",
        "tenant_id": metadata.get("tenant_id", "default"),
        "worklist_evidence": worklist_evidence,
    }


def _mcp_transport_payload() -> dict:
    metadata = {
        "document_id": "doc-public-regulation",
        "chunk_id": "chunk-leave",
        "approval_id": "approval-chunk-leave",
        "content_hash": "content-hash-chunk-leave",
        "approved_content_hash": "approved-hash-chunk-leave",
        "institution_name": "agency-a",
        "profile_id": "public_portal-public-rule",
        "regulation_id": "reg-public-rule",
        "regulation_version": "v1",
        "approval_status": "approved",
        "regulation_status": "approved",
        "effective_from": "2026-01-01",
        "effective_to": "",
        "repealed_at": "",
        "source_system": "PUBLIC_PORTAL",
        "source_url": "https://example.test/public_portal/doc-public-regulation",
        "regulation_title": "Personnel Rule",
        "article_no": "Article 1",
        "source_page_start": 1,
        "security_level": "internal",
    }
    return {
        "passed": True,
        "transport": "stdio",
        "full_profile": {
            "passed": True,
            "tool_names": ["fetch", "search", "list_documents", "get_regulation_history"],
            "search_result_count": 1,
            "warm_search_result_count": 1,
            "fetch_has_text": True,
            "history_tool_available": True,
            "history_attempted": True,
            "history_passed": True,
            "history_version_count": 1,
            "first_result_metadata": dict(metadata),
            "search_metadata": {
                "timing_ms": {
                    "load_vector_records_elapsed_ms": 1.0,
                    "approval_snapshot_elapsed_ms": 2.0,
                    "visibility_filter_elapsed_ms": 3.0,
                    "scoring_elapsed_ms": 4.0,
                }
            },
            "list_tools_elapsed_ms": 11.0,
            "search_elapsed_ms": 22.0,
            "fetch_elapsed_ms": 33.0,
            "warm_search_elapsed_ms": 8.0,
            "total_elapsed_ms": 100.0,
        },
        "chatgpt_data_profile": {
            "passed": True,
            "tool_names": ["fetch", "search"],
            "search_result_count": 1,
            "warm_search_result_count": 1,
            "fetch_has_text": True,
            "first_result_metadata": dict(metadata),
            "search_metadata": {"timing_ms": {"scoring_elapsed_ms": 2.0}},
            "list_tools_elapsed_ms": 7.0,
            "search_elapsed_ms": 9.0,
            "fetch_elapsed_ms": 13.0,
            "warm_search_elapsed_ms": 5.0,
            "total_elapsed_ms": 50.0,
        },
    }


def _mcp_demo_answer_payload() -> dict:
    return {
        "report_type": "mcp_demo_answers",
        "passed": True,
        "query_count": 2,
        "top_k": 10,
        "query_spec_path": "config/query_specs.json",
        "query_spec_sha256": "a" * 64,
        "query_spec_byte_count": 1234,
        "query_spec_item_count": 2,
        "quality_issue_count": 0,
        "api_call_count": 0,
        "items": [
            {
                "query": "휴직 절차는?",
                "passed": True,
                "search_result_count": 2,
                "fetch_result_count": 2,
                "supporting_result_count": 1,
                "answer": "승인된 규정 근거 기준입니다.",
                "smoke_citation_count": 0,
                "quality_issue_count": 0,
                "quality_issues": [],
                "expected_terms": ["leave", "period"],
                "expected_term_hits": ["leave", "period"],
                "expected_term_hit_ratio": 1.0,
                "expected_article_nos": ["제10조"],
                "expected_article_no_hits": ["제10조"],
                "expected_article_no_hit_ratio": 1.0,
                "expected_article_titles": ["휴직"],
                "expected_article_title_hits": ["휴직"],
                "expected_article_title_hit_ratio": 1.0,
                "citations": [
                    {
                        "document_id": "doc-public-regulation",
                        "chunk_id": "chunk-leave",
                        "approval_id": "approval-chunk-leave",
                    }
                ],
            },
            {
                "query": "성과연봉은?",
                "passed": True,
                "search_result_count": 1,
                "fetch_result_count": 1,
                "supporting_result_count": 1,
                "answer": "승인된 규정 근거 기준입니다.",
                "smoke_citation_count": 0,
                "quality_issue_count": 0,
                "quality_issues": [],
                "expected_terms": ["pay", "grade"],
                "expected_term_hits": ["pay"],
                "expected_term_hit_ratio": 0.5,
                "expected_article_nos": ["제24조", "제27조"],
                "expected_article_no_hits": ["제24조"],
                "expected_article_no_hit_ratio": 0.5,
                "citations": [
                    {
                        "document_id": "doc-public-regulation",
                        "chunk_id": "chunk-pay",
                        "approval_id": "approval-chunk-pay",
                    }
                ],
            },
        ],
    }


def _accuracy_comparison_payload() -> dict:
    return {
        "report_type": "simple_rag_vs_mcp_accuracy",
        "passed": True,
        "query_count": 2,
        "top_k": 10,
        "query_spec_path": "config/query_specs.json",
        "query_spec_sha256": "a" * 64,
        "query_spec_byte_count": 1234,
        "query_spec_item_count": 2,
        "api_call_count": 0,
        "summary": {
            "baseline_passed_count": 1,
            "mcp_passed_count": 2,
            "mcp_better_count": 1,
            "mcp_not_worse_count": 2,
            "mcp_regression_count": 0,
            "baseline_avg_quality_score": 0.85,
            "mcp_avg_quality_score": 1.0,
            "avg_score_delta": 0.15,
        },
    }


def _parser_goldset_score_payload() -> dict:
    return {
        "report_type": "parsing_goldset_score",
        "summary": {
            "document_count": 2,
            "scored_document_count": 2,
            "excluded_document_count": 0,
            "structure_type_count": 7,
            "scorable_structure_count": 14,
            "issue_count": 0,
            "ready_for_quality_claim": True,
        },
        "completion": {
            "ready_for_quality_claim": True,
            "accepted_label_statuses": ["approved", "completed", "human_reviewed", "reviewed"],
            "label_status_counts": {"reviewed": 2},
            "completed_document_count": 2,
            "pending_document_count": 0,
            "scored_document_count": 2,
            "excluded_document_count": 0,
            "expected_structure_score_count": 14,
            "completed_structure_score_count": 14,
            "missing_structure_score_count": 0,
            "blocking_issue_codes": {},
        },
        "overall": {
            "precision": 96.0,
            "recall": 96.0,
            "f1": 96.0,
        },
        "by_structure": {
            "article": {"f1": 100.0, "missing_match_count": 0},
            "table": {"f1": 92.0, "missing_match_count": 0},
            "nested_table": {"f1": 96.0, "missing_match_count": 0},
            "appendix_form": {"f1": 94.0, "missing_match_count": 0},
        },
    }


def _parser_goldset_completion_board_payload() -> dict:
    return {
        "report_type": "parsing_goldset_completion_board",
        "generated_at": "2026-07-10T00:00:00+00:00",
        "document_count": 12,
        "ready_document_count": 0,
        "pending_document_count": 12,
        "expected_structure_score_rows": 84,
        "completed_structure_score_rows": 0,
        "missing_structure_score_rows": 84,
        "missing_manual_field_count": 84,
        "missing_matched_field_count": 84,
        "missing_reviewer_metadata_count": 12,
        "ready_for_quality_claim": False,
        "completion_gate_status": "blocked_pending_human_labels",
        "priority_tier_counts": {"table_heavy_first": 6},
        "structure_completion_summary": {
            "table": {
                "expected_document_count": 12,
                "pipeline_total": 394,
                "score_rows_complete": 0,
                "missing_manual_count": 12,
                "missing_matched_count": 12,
                "ready_for_structure_f1": False,
            }
        },
    }


def _table_preprocessing_claim_gate_payload() -> dict:
    return {
        "report_type": "table_preprocessing_claim_gate",
        "generated_at": "2026-07-10T00:00:00+00:00",
        "passed": False,
        "status": "blocked_pending_human_review",
        "claim_level": "review_ready_not_accuracy_proven",
        "feasibility_status": "feasible_with_human_review",
        "blocker_count": 3,
        "warning_count": 0,
        "summary": {
            "selected_unit_count": 255,
            "completed_unit_count": 0,
            "pending_unit_count": 255,
            "invalid_unit_count": 0,
            "required_field_missing_total": 2040,
            "required_field_missing_counts": {
                "human_source_pages_checked": 255,
                "human_unit_status": 255,
                "human_manual_table_count": 255,
                "human_matched_table_count": 255,
                "human_row_column_match": 255,
                "human_parentage_ok": 255,
                "human_reviewer": 255,
                "human_reviewed_at": 255,
            },
            "review_priority_counts": {"source_table_compare": 144},
            "label_review_flag_counts": {"missing_table_label": 60},
            "ready_for_table_score_transfer": False,
            "transfer_passed": False,
            "transfer_blocker_count": 25,
            "source_traceability_passed": True,
            "source_traceability_issue_count": 0,
            "source_traceability_record_count": 20,
            "source_page_count_status_counts": {
                "verified_pdf": 10,
                "not_checked_for_format": 10,
            },
            "source_format_status_counts": {
                "verified_pdf": 10,
                "verified_hwpx_zip": 5,
            },
            "answer_query_count": 20,
            "table_answer_blocker_count": 0,
            "non_review_evidence_ready": True,
            "release_blocked_by_human_review": True,
        },
        "finding_code_counts": {
            "table-human-review-pending": 1,
            "table-review-summary-not-ready": 1,
            "table-count-transfer-blocked": 1,
        },
    }


def _temporal_coverage_payload(*, data_dir: Path) -> dict:
    return {
        "report_type": "temporal_metadata_coverage",
        "generated_at": "2026-07-09T03:00:00+00:00",
        "runtime_data_dir": str(data_dir),
        "effective_runtime_data_dir": str(data_dir),
        "tenant_id": "default",
        "record_count": 2,
        "with_temporal_metadata_count": 2,
        "without_temporal_metadata_count": 0,
        "temporal_metadata_ratio": 1.0,
        "inheritance_opportunities": {
            "candidate_scope_count": 0,
            "candidate_missing_record_count": 0,
        },
        "blocker_count": 0,
        "warning_count": 0,
        "finding_count": 0,
        "passed": True,
        "latest_only_passed": True,
        "api_call_count": 0,
    }


def _temporal_backfill_payload(*, ambiguous_count: int = 0, conflict_count: int = 0) -> dict:
    return {
        "report_type": "temporal_backfill_shadow_runtime",
        "generated_at": "2026-07-09T03:05:00+00:00",
        "passed": conflict_count == 0,
        "chunk_file_count": 1,
        "input_chunk_count": 2,
        "output_chunk_count": 2,
        "vector_record_count": 2,
        "before": {
            "temporal_metadata_count": 2,
            "temporal_metadata_ratio": 1.0,
            "conflict_chunk_count": 0,
            "ambiguous_chunk_count": 0,
        },
        "after": {
            "temporal_metadata_count": 2,
            "temporal_metadata_ratio": 1.0,
            "inherited_chunk_count": 0,
            "normalized_chunk_count": 0,
            "conflict_chunk_count": conflict_count,
            "ambiguous_chunk_count": ambiguous_count,
        },
        "delta": {
            "temporal_metadata_count": 0,
            "conflict_chunk_count": conflict_count,
            "ambiguous_chunk_count": ambiguous_count,
        },
        "write_blocked": conflict_count > 0,
        "shadow_runtime_written": conflict_count == 0,
        "api_call_count": 0,
    }


def _runtime_version_drift_payload(*, data_dir: Path) -> dict:
    return {
        "report_type": "runtime_version_drift",
        "generated_at": "2026-07-09T04:00:00+00:00",
        "runtime_data_dir": str(data_dir),
        "effective_runtime_data_dir": str(data_dir),
        "tenant_id": "default",
        "tenant_storage_isolation": False,
        "passed": True,
        "current_versions": {"chunker_version": "0.1.5"},
        "approved_repository_chunk_count": 2,
        "vector_record_count": 2,
        "approved_repository_stale_chunker_count": 0,
        "approved_repository_stale_chunker_ratio": 0.0,
        "vector_stale_chunker_count": 0,
        "vector_stale_chunker_ratio": 0.0,
        "version_loss": {"loss_count": 0, "mismatch_count": 0},
        "vector_integrity": {
            "failure_count": 0,
            "content_hash_mismatch_count": 0,
            "verification_hash_mismatch_count": 0,
            "metadata_missing_required_count": 0,
            "invalid_approval_status_count": 0,
            "invalid_security_level_count": 0,
            "embedded_dimension_mismatch_count": 0,
            "embedded_integrity_failure_count": 0,
            "local_path_leak_count": 0,
        },
        "reapproval_scope": {
            "reprocess_requires_reapproval": False,
            "approved_chunks_with_stale_chunker_count": 0,
            "approved_chunks_with_approved_hash_count": 0,
        },
        "blocker_count": 0,
        "warning_count": 0,
        "finding_count": 0,
        "api_call_count": 0,
    }


def _temporal_ambiguity_scope_payload(*, ambiguous_count: int, status: str = "temporal_ambiguity_policy_required") -> dict:
    clear = status == "temporal_ambiguity_clear"
    return {
        "report_type": "temporal_ambiguity_review_scope",
        "generated_at": "2026-07-09T03:10:00+00:00",
        "status": status,
        "passed": clear,
        "summary": {
            "chunk_count": 2,
            "conflict_chunk_count": 0,
            "ambiguous_chunk_count": ambiguous_count,
            "ambiguous_chunk_ratio": ambiguous_count / 2,
        },
        "record_analysis": {
            "vector_record_count": 2,
            "ambiguous_record_count": ambiguous_count,
            "review_slice_count": 1 if ambiguous_count else 0,
        },
        "decision_requirements": (
            [
                {
                    "decision_id": "temporal_ambiguity_index_policy",
                    "blocks_product_release": True,
                },
                {
                    "decision_id": "temporal_ambiguity_answer_policy",
                    "blocks_product_release": True,
                },
            ]
            if ambiguous_count
            else [
                {
                    "decision_id": "temporal_ambiguity_clear",
                    "blocks_product_release": False,
                }
            ]
        ),
        "api_call_count": 0,
    }


def _temporal_policy_decision_validation_payload(*, passed: bool) -> dict:
    return {
        "report_type": "temporal_ambiguity_policy_decision_validation",
        "generated_at": "2026-07-09T03:20:00+00:00",
        "scope_report": "reports/temporal_ambiguity_scope.json",
        "scope_passed": False,
        "status": "policy_decisions_valid" if passed else "blocked_pending_policy_decisions",
        "passed": passed,
        "decision_row_count": 2,
        "release_blocking_row_count": 2,
        "operator_decision_counts": (
            {
                "approve_index_with_disclosure": 1,
                "approve_with_disclosure": 1,
            }
            if passed
            else {"blank": 2}
        ),
        "blocking_count": 0 if passed else 2,
        "warning_count": 0,
        "findings": [],
        "api_call_count": 0,
    }


def _revision_impact_payload() -> dict:
    return {
        "report_type": "revision_impact",
        "generated_at": "2026-07-09T04:30:00+00:00",
        "before_label": "2026-06-01",
        "after_label": "2026-07-01",
        "summary": {
            "before_unit_count": 10,
            "after_unit_count": 11,
            "changed_count": 2,
            "added_count": 1,
            "removed_count": 1,
            "unchanged_count": 8,
            "metadata_only_changed_count": 1,
            "approval_required_count": 4,
            "approval_reuse_candidate_count": 8,
            "deindex_required_count": 1,
        },
    }


def _profile_provenance_payload() -> dict:
    return {
        "report_type": "profile_provenance",
        "passed": True,
        "row_count": 3,
        "institution_count": 3,
        "apba_id_count": 3,
        "apba_id_counts": {"C0147": 1, "C0165": 1, "C0247": 1},
        "file_type_counts": {"hwp": 1, "hwpx": 1, "pdf": 1},
        "batch_profile_counts": {"public_portal-agency-a": 1, "public_portal-agency-b": 1, "public_portal-agency-c": 1},
        "registry_summary": {
            "profile_count": 3,
            "profile_ids": ["public_portal-agency-a", "public_portal-agency-b", "public_portal-agency-c"],
            "sha256": "profile-registry-sha",
        },
        "matched_profile_ids": ["public_portal-agency-a", "public_portal-agency-b", "public_portal-agency-c"],
        "unknown_profile_counts": {},
        "blocker_count": 0,
        "warning_count": 0,
        "finding_count": 0,
        "api_call_count": 0,
    }


def _write_clean_approval_evidence_reports(
    *,
    approval_worklist_report: Path,
    approval_review_batch_report: Path,
    reapproval_worklist_report: Path,
    reapproval_review_batch_report: Path,
    reapproval_decision_validation_report: Path | None = None,
) -> None:
    _write_json(
        approval_worklist_report,
        {
            "report_type": "approval_worklist",
            "generated_at": "2026-07-09T05:00:00+00:00",
            "document_count": 1,
            "total_chunks": 2,
            "manual_attention_chunks": 0,
            "bulk_review_candidate_chunks": 0,
            "low_risk_batch_review_candidate_chunks": 0,
            "blocking_review_chunks": 0,
            "domain_attention_chunks": 0,
            "stable_false_positive_chunks": 0,
            "informational_chunks": 0,
            "no_signal_chunks": 0,
            "review_priority_tier_counts": {
                "blocking_review": 0,
                "domain_attention": 0,
                "stable_false_positive": 0,
                "informational": 0,
                "no_signal": 0,
            },
            "action_counts": {"already_approved_or_empty": 1},
        },
    )
    _write_json(
        approval_review_batch_report,
        {
            "report_type": "approval_review_batch_manifest",
            "generated_at": "2026-07-09T05:30:00+00:00",
            "passed": True,
            "batch_count": 0,
            "approval_chunk_count": 0,
            "manual_attention_chunks": 0,
            "low_risk_batch_review_candidate_chunks": 0,
            "blocker_count": 0,
            "warning_count": 0,
            "review_type_batch_counts": {},
        },
    )
    _write_json(
        reapproval_worklist_report,
        {
            "report_type": "reapproval_worklist",
            "generated_at": "2026-07-09T05:45:00+00:00",
            "document_count": 1,
            "reapproval_candidate_chunks": 2,
            "high_risk_candidate_chunks": 0,
            "temporal_sample_candidate_chunks": 0,
            "low_risk_candidate_chunks": 2,
            "recommended_initial_review_chunks": 0,
            "estimated_initial_review_minutes": 0,
            "source_vector_integrity_failure_count": 0,
            "pre_reapproval_blockers": [],
            "review_strategy": "no_reapproval_required",
        },
    )
    _write_json(
        reapproval_review_batch_report,
        {
            "report_type": "reapproval_review_batch_manifest",
            "generated_at": "2026-07-09T06:00:00+00:00",
            "passed": True,
            "candidate_count": 2,
            "selected_candidate_count": 2,
            "batch_count": 1,
            "reapproval_chunk_count": 2,
            "max_chunks_per_batch": 100,
            "blocker_count": 0,
            "warning_count": 0,
            "action_batch_counts": {"no_reapproval_required": 1},
            "action_chunk_counts": {"no_reapproval_required": 2},
            "risk_tier_batch_counts": {"low": 1},
            "risk_tier_chunk_counts": {"low": 2},
        },
    )
    if reapproval_decision_validation_report is not None:
        _write_json(
            reapproval_decision_validation_report,
            {
                "report_type": "reapproval_decision_validation",
                "generated_at": "2026-07-09T06:30:00+00:00",
                "passed": True,
                "release_gate_status": "ready_for_reapproval_apply",
                "blocking_count": 0,
                "warning_count": 0,
                "expected_batch_count": 1,
                "decision_row_count": 1,
                "complete_row_count": 1,
                "blank_or_incomplete_row_count": 0,
                "operator_decision_counts": {"approve_all_reviewed": 1},
                "api_call_count": 0,
            },
        )


def _write_reapproval_apply_plan(path: Path, *, unsafe: bool = False) -> None:
    direct_write_allowed = bool(unsafe)
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
    _write_json(
        path,
        {
            "report_type": "reapproval_apply_plan",
            "generated_at": "2026-07-09T06:45:00+00:00",
            "passed": True,
            "release_gate_status": "ready_for_apply_execution",
            "blocker_count": 0,
            "summary": {
                "batch_count": 1,
                "approve_chunk_count": 2,
                "reject_chunk_count": 0,
                "reprocess_chunk_count": 0,
                "defer_chunk_count": 0,
            },
            "operator_controls": {
                "auto_approval": False,
                "auto_reindex": False,
                "applies_reapproval_decisions": False,
                "requires_dedicated_apply_step": True,
                "direct_approval_metadata_write_allowed": direct_write_allowed,
                "requires_tenant_and_operator_access_control": not unsafe,
                "requires_shared_review_workflow_contract": True,
                "requires_approval_precondition_validation": True,
                "requires_rejection_decision_validation": True,
                "requires_preapproval_security_scan": True,
                "requires_review_flag_acknowledgement": True,
                "requires_approved_content_hash_recalculation": True,
                "requires_review_journal_append": True,
                "requires_apply_audit_event": not unsafe,
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
                    "document_id": "doc1",
                    "planned_operation": "approve",
                    "approve_chunk_ids": ["chunk1", "chunk2"],
                    "reject_chunk_ids": [],
                    "requires_reindex": True,
                    "apply_controls": {
                        "direct_metadata_write_allowed": direct_write_allowed,
                        "requires_tenant_and_operator_access_control": not unsafe,
                        "requires_shared_review_workflow_contract": True,
                        "approval_requires_precondition_validation": True,
                        "approval_requires_preapproval_security_scan": True,
                        "approval_requires_review_flag_acknowledgement_if_attention_present": True,
                        "approval_recalculates_approved_content_hash": True,
                        "rejection_clears_approval_fields": False,
                        "rejection_requires_reason_validation": False,
                        "requires_review_journal_append": True,
                        "requires_apply_audit_event": not unsafe,
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
        },
    )


def _write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
