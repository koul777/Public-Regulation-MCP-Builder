from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import build_release_evidence_index as evidence


GENERATED_AT = "2026-07-07T00:00:00+00:00"


class BuildReleaseEvidenceIndexTests(unittest.TestCase):
    def test_default_artifacts_cover_private_release_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dist = root / "dist"
            dist.mkdir()
            (dist / "reg_rag_preprocessor-0.2.0-py3-none-any.whl").write_text("wheel", encoding="utf-8")
            (dist / "reg_rag_preprocessor-0.2.0.tar.gz").write_text("sdist", encoding="utf-8")

            index = evidence.build_release_evidence_index(
                root,
                generated_at=GENERATED_AT,
            )

        self.assertEqual(
            [
                "reports/private_release_gate_current.json",
                "reports/private_release_readiness_current.json",
                "reports/private_release_manifest_current.json",
                "reports/github_private_visibility_current.json",
                "reports/release_hygiene_current.json",
                "reports/private_release_smoke_current.json",
                "dist/reg_rag_preprocessor-0.2.0-py3-none-any.whl",
                "dist/reg_rag_preprocessor-0.2.0.tar.gz",
            ],
            [artifact["artifact_path"] for artifact in index["artifacts"]],
        )

    def test_indexes_existing_and_missing_artifacts_without_running_release_tools(self) -> None:
        artifact_bytes = b'{"report_type": "private_release_gate", "passed": true}\n'
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact = root / "reports" / "private_release_gate.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(artifact_bytes)

            index = evidence.build_release_evidence_index(
                root,
                artifact_paths=[
                    Path("reports/private_release_gate.json"),
                    Path("reports/missing_private_release_readiness.json"),
                ],
                generated_at=GENERATED_AT,
            )

        self.assertEqual("release_evidence_index", index["index_type"])
        self.assertEqual(1, index["index_version"])
        self.assertEqual(GENERATED_AT, index["generated_at"])
        self.assertEqual(2, index["artifact_count"])

        present = index["artifacts"][0]
        self.assertEqual("reports/private_release_gate.json", present["artifact_path"])
        self.assertTrue(present["exists"])
        self.assertEqual(len(artifact_bytes), present["size_bytes"])
        self.assertEqual(hashlib.sha256(artifact_bytes).hexdigest(), present["sha256"])
        self.assertEqual(GENERATED_AT, present["generated_at"])
        self.assertEqual(
            {"report_type": "private_release_gate", "passed": True},
            present["json_summary"],
        )

        missing = index["artifacts"][1]
        self.assertEqual("reports/missing_private_release_readiness.json", missing["artifact_path"])
        self.assertFalse(missing["exists"])
        self.assertIsNone(missing["size_bytes"])
        self.assertIsNone(missing["sha256"])
        self.assertEqual(GENERATED_AT, missing["generated_at"])

    def test_connection_readiness_summary_includes_approval_journal_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report_path = root / "reports" / "mcp_connection_readiness.json"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(
                json.dumps(
                    {
                        "report_type": "mcp_connection_readiness",
                        "passed": True,
                        "mcp_index_visibility_summary": {
                            "passed": True,
                            "tenant_id": "tenant-a",
                            "document_count": 2,
                            "total_approved_chunks": 8,
                            "total_mcp_visible_records": 8,
                            "finding_count": 0,
                            "approval_provenance_coverage": {
                                "record_count": 8,
                                "complete_record_count": 8,
                                "complete_ratio": 1.0,
                                "missing_field_counts": {"approval_worklist_report_path": 0},
                            },
                            "approval_journal_coverage": {
                                "journal_record_count": 2,
                                "record_count": 8,
                                "eligible_record_count": 8,
                                "matched_record_count": 8,
                                "missing_record_count": 0,
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            index = evidence.build_release_evidence_index(
                root,
                artifact_paths=[Path("reports/mcp_connection_readiness.json")],
                generated_at=GENERATED_AT,
            )

        summary = index["artifacts"][0]["json_summary"]
        self.assertEqual(8, summary["mcp_index_visibility_total_mcp_visible_records"])
        self.assertEqual(8, summary["mcp_index_visibility_approval_provenance_coverage_complete_record_count"])
        self.assertEqual(
            0,
            summary[
                "mcp_index_visibility_approval_provenance_coverage_missing_approval_worklist_report_path_count"
            ],
        )
        self.assertEqual(8, summary["mcp_index_visibility_approval_journal_coverage_matched_record_count"])
        self.assertEqual(0, summary["mcp_index_visibility_approval_journal_coverage_missing_record_count"])

    def test_cli_writes_json_for_repeated_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = root / "private_release_readiness.json"
            manifest = root / "private_release_manifest.json"
            out_json = root / "release_evidence_index.json"
            readiness.write_text('{"report_type": "private_release_readiness"}\n', encoding="utf-8")
            manifest.write_text('{"manifest_type": "private_release_handoff"}\n', encoding="utf-8")
            stdout = io.StringIO()

            exit_code = evidence.main(
                [
                    "--repo-root",
                    str(root),
                    "--out-json",
                    str(out_json),
                    "--artifact",
                    "private_release_readiness.json",
                    "--artifact",
                    "private_release_manifest.json",
                ],
                stdout=stdout,
            )

            written = json.loads(out_json.read_text(encoding="utf-8"))
            printed = json.loads(stdout.getvalue())

        self.assertEqual(0, exit_code)
        self.assertEqual(written, printed)
        self.assertEqual(
            ["private_release_readiness.json", "private_release_manifest.json"],
            [artifact["artifact_path"] for artifact in written["artifacts"]],
        )
        self.assertTrue(all(artifact["exists"] for artifact in written["artifacts"]))

    def test_out_of_repo_absolute_artifact_path_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(repo_dir)
            outside = Path(outside_dir) / "operator-secret-evidence.json"
            outside.write_text('{"report_type": "private_release_gate", "passed": true}\n', encoding="utf-8")

            index = evidence.build_release_evidence_index(
                root,
                artifact_paths=[outside],
                generated_at=GENERATED_AT,
            )

        artifact = index["artifacts"][0]
        self.assertEqual("outside-repo/operator-secret-evidence.json", artifact["artifact_path"])
        self.assertNotIn(str(Path(outside_dir)), json.dumps(index))
        self.assertTrue(artifact["exists"])

    def test_private_smoke_summary_includes_handoff_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            smoke = root / "reports" / "private_release_smoke_current.json"
            smoke.parent.mkdir(parents=True)
            smoke.write_text(
                json.dumps(
                    {
                        "report_type": "private_release_smoke",
                        "passed": True,
                        "repo_commit": "a" * 40,
                        "data_dir_mode": "explicit",
                        "handoff_evidence": True,
                    }
                ),
                encoding="utf-8",
            )

            index = evidence.build_release_evidence_index(
                root,
                artifact_paths=["reports/private_release_smoke_current.json"],
                generated_at=GENERATED_AT,
            )

        self.assertEqual("a" * 40, index["repo_commit"])
        self.assertEqual(
            {
                "report_type": "private_release_smoke",
                "passed": True,
                "repo_commit": "a" * 40,
                "data_dir_mode": "explicit",
                "handoff_evidence": True,
            },
            index["artifacts"][0]["json_summary"],
        )

    def test_mcp_report_summary_includes_handoff_and_answer_quality_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reports = root / "reports"
            reports.mkdir()
            handoff = reports / "mcp_handoff_current.json"
            demo = reports / "mcp_demo_answers_current.json"
            handoff.write_text(
                json.dumps(
                    {
                        "report_type": "mcp_handoff_report",
                        "server_name": "aks-regulation-mcp",
                        "decision": "ready_for_local_claude_desktop_mvp",
                        "handoff_ready": True,
                        "passed": True,
                        "blocking_count": 0,
                        "warning_count": 0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            demo.write_text(
                json.dumps(
                    {
                        "report_type": "mcp_demo_answers",
                        "tenant_id": "tenant-aks-publish",
                        "passed": True,
                        "top_k": 10,
                        "query_count": 3,
                        "query_spec_path": "config/aks_mcp_demo_query_specs_ko.json",
                        "query_spec_sha256": "a" * 64,
                        "query_spec_byte_count": 1234,
                        "query_spec_item_count": 3,
                        "quality_issue_count": 0,
                        "items": [
                            {
                                "expected_terms": ["leave", "period"],
                                "expected_term_hit_ratio": 1.0,
                            },
                            {
                                "expected_terms": ["appointment", "notice"],
                                "expected_term_hit_ratio": 0.5,
                            },
                            {
                                "expected_terms": ["pay"],
                                "expected_term_hit_ratio": 1.0,
                            },
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            index = evidence.build_release_evidence_index(
                root,
                artifact_paths=[
                    "reports/mcp_handoff_current.json",
                    "reports/mcp_demo_answers_current.json",
                ],
                generated_at=GENERATED_AT,
            )

        self.assertEqual(
            {
                "report_type": "mcp_handoff_report",
                "passed": True,
                "handoff_ready": True,
                "decision": "ready_for_local_claude_desktop_mvp",
                "server_name": "aks-regulation-mcp",
                "blocking_count": 0,
                "warning_count": 0,
            },
            index["artifacts"][0]["json_summary"],
        )
        self.assertEqual(
            {
                "report_type": "mcp_demo_answers",
                "passed": True,
                "tenant_id": "tenant-aks-publish",
                "top_k": 10,
                "query_count": 3,
                "query_spec_path": "config/aks_mcp_demo_query_specs_ko.json",
                "query_spec_sha256": "a" * 64,
                "query_spec_byte_count": 1234,
                "query_spec_item_count": 3,
                "quality_issue_count": 0,
                "expected_term_min_hit_ratio": 0.5,
                "expected_term_average_hit_ratio": 0.833,
                "expected_term_low_hit_count": 0,
            },
            index["artifacts"][1]["json_summary"],
        )

    def test_mcp_product_readiness_profile_defaults_and_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reports = root / "reports"
            reports.mkdir()
            (reports / "mcp_readiness_authority_current.json").write_text(
                json.dumps(
                    {
                        "report_type": "mcp_readiness_authority",
                        "authority_version": 1,
                        "passed": True,
                        "blocking_count": 0,
                        "authoritative_artifacts": [{"role": "product_readiness"}],
                        "supersedes": [{"path": "reports/old.json", "reason": "replaced"}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (reports / "mcp_readiness_authority_current.md").write_text("# Authority\n", encoding="utf-8")
            (reports / "mcp_product_readiness_current.json").write_text(
                json.dumps(
                    {
                        "report_type": "mcp_product_readiness",
                        "passed": True,
                        "blocking_count": 0,
                        "warning_count": 1,
                        "source_report_artifacts": [{"path": "reports/batch.json", "sha256": "a" * 64}],
                        "runtime_summary": {
                            "approval_provenance_coverage": {
                                "record_count": 5997,
                                "complete_record_count": 5800,
                                "complete_ratio": 0.9672,
                                "missing_field_counts": {
                                    "approval_id": 0,
                                    "approved_content_hash": 0,
                                    "approval_worklist_report_path": 197,
                                    "approval_worklist_report_sha256": 197,
                                    "approval_review_batch_manifest_path": 197,
                                    "approval_review_batch_manifest_sha256": 197,
                                },
                            }
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
                        "table_preprocessing_claim_gate_summary": {
                            "passed": False,
                            "status": "blocked_evidence_drift",
                            "feasibility_status": "blocked_before_review",
                            "blocker_count": 5,
                            "selected_unit_count": 255,
                            "completed_unit_count": 0,
                            "pending_unit_count": 255,
                            "invalid_unit_count": 0,
                            "transfer_blocker_count": 25,
                            "source_traceability_issue_count": 10,
                            "source_traceability_record_count": 20,
                            "source_traceability_require_page_count_verification": False,
                            "drift_check_present": True,
                            "drift_check_passed": False,
                            "drift_check_blocker_count": 1,
                            "table_answer_blocker_count": 0,
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
                            "approval_provenance_missing_chunks": 197,
                            "approval_provenance_only_chunks": 50,
                            "approval_provenance_missing_field_counts": {
                                "approval_worklist_report_path": 197,
                                "approval_worklist_report_sha256": 197,
                            },
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
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (reports / "mcp_product_readiness_current.md").write_text("# Product\n", encoding="utf-8")
            for name, payload in {
                "table_review_source_traceability_current.json": {
                    "report_type": "table_review_source_traceability",
                    "repo_commit": "a" * 40,
                    "traceability_passed": False,
                    "record_count": 20,
                    "issue_count": 10,
                    "require_page_count_verification": True,
                },
                "parsing_goldset_table_drift_check_current.json": {
                    "report_type": "parsing_goldset_table_drift_check",
                    "repo_commit": "a" * 40,
                    "passed": False,
                    "blocker_count": 1,
                },
                "table_preprocessing_claim_gate_current.json": {
                    "report_type": "table_preprocessing_claim_gate",
                    "repo_commit": "a" * 40,
                    "passed": False,
                    "status": "blocked_evidence_drift",
                },
            }.items():
                (reports / name).write_text(json.dumps(payload) + "\n", encoding="utf-8")
            for name in (
                "table_review_source_traceability_current.md",
                "parsing_goldset_table_drift_check_current.md",
                "table_preprocessing_claim_gate_current.md",
            ):
                (reports / name).write_text("# Table Evidence\n", encoding="utf-8")
            for name, report_type in {
                "mcp_demo_answers_current.json": "mcp_demo_answers",
                "simple_rag_vs_mcp_accuracy_current.json": "simple_rag_vs_mcp_accuracy",
                "mcp_transport_smoke_current.json": "mcp_transport_smoke",
                "mcp_handoff_current.json": "mcp_handoff_report",
            }.items():
                (reports / name).write_text(
                    json.dumps({"report_type": report_type, "passed": True, "blocking_count": 0}) + "\n",
                    encoding="utf-8",
                )
            for name, payload in {
                "mcp_query_benchmark_current.json": {
                    "report_type": "mcp_query_benchmark",
                    "passed": True,
                    "query_count": 5,
                    "min_warm_records": 5000,
                    "min_record_count": 5997,
                },
                "mcp_answer_evidence_bundle_current.json": {
                    "report_type": "mcp_answer_evidence_bundle",
                    "passed": True,
                    "query_count": 5,
                },
                "mcp_performance_load_evidence_current.json": {
                    "report_type": "mcp_performance_load_evidence",
                    "passed": True,
                    "evidence_ready": True,
                    "min_warm_records": 5000,
                    "min_record_count": 5997,
                },
                "mcp_cold_start_benchmark_current.json": {
                    "report_type": "mcp_cold_start_benchmark",
                    "passed": True,
                    "iterations": 3,
                    "max_process_elapsed_ms": 1000,
                    "min_record_count": 5997,
                },
                "mcp_concurrent_benchmark_current.json": {
                    "report_type": "mcp_concurrent_query_benchmark",
                    "passed": True,
                    "rounds": 2,
                    "concurrency": 3,
                    "task_count": 10,
                    "max_task_total_ms": 400,
                    "max_batch_elapsed_ms": 1000,
                },
            }.items():
                (reports / name).write_text(json.dumps(payload) + "\n", encoding="utf-8")
            for name in (
                "mcp_query_benchmark_current.md",
                "mcp_answer_evidence_bundle_current.md",
                "mcp_performance_load_evidence_current.md",
                "mcp_cold_start_benchmark_current.md",
                "mcp_concurrent_benchmark_current.md",
            ):
                (reports / name).write_text("# Evidence\n", encoding="utf-8")
            (reports / "aks_mcp_publish_runtime_report.json").write_text(
                json.dumps(
                    {
                        "report_type": "aks_mcp_publish_runtime",
                        "passed": True,
                        "tenant_id": "tenant-aks-publish",
                        "source_chunk_count": 5997,
                        "selected_chunk_count": 5997,
                        "approved_chunk_count": 5997,
                        "approval_record_count": 2,
                        "indexed_record_count": 5997,
                        "approval_evidence": {
                            "worklist_report_path": "reports/aks_mcp_publish_tenant-aks-publish_approval_worklist.json",
                            "worklist_report_sha256": "b" * 64,
                            "review_batch_manifest_path": (
                                "reports/aks_mcp_publish_tenant-aks-publish_approval_review_batches.json"
                            ),
                            "review_batch_manifest_sha256": "c" * 64,
                            "approval_request_count": 2,
                            "approval_chunk_count": 5997,
                            "manual_attention_batch_count": 1,
                            "manual_attention_chunk_count": 1993,
                            "review_type_batch_counts": {
                                "low_risk_batch": 1,
                                "manual_attention": 1,
                            },
                            "artifacts": {
                                "worklist_json": (
                                    "data\\aks_mcp_publish_runtime\\reports\\"
                                    "aks_mcp_publish_tenant-aks-publish_approval_worklist.json"
                                ),
                                "review_batch_manifest_json": (
                                    "data\\aks_mcp_publish_runtime\\reports\\"
                                    "aks_mcp_publish_tenant-aks-publish_approval_review_batches.json"
                                ),
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            publish_reports = root / "data" / "aks_mcp_publish_runtime" / "reports"
            publish_reports.mkdir(parents=True)
            (publish_reports / "aks_mcp_publish_tenant-aks-publish_approval_worklist.json").write_text(
                json.dumps(
                    {
                        "report_type": "approval_worklist",
                        "tenant_id": "tenant-aks-publish",
                        "total_chunks": 5997,
                        "manual_attention_chunks": 1993,
                        "low_risk_batch_review_candidate_chunks": 4004,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (publish_reports / "aks_mcp_publish_tenant-aks-publish_approval_review_batches.json").write_text(
                json.dumps(
                    {
                        "report_type": "approval_review_batch_manifest",
                        "passed": True,
                        "tenant_id": "tenant-aks-publish",
                        "batch_count": 2,
                        "approval_chunk_count": 5997,
                        "manual_attention_chunks": 1993,
                        "low_risk_batch_review_candidate_chunks": 4004,
                        "blocker_count": 0,
                        "warning_count": 0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (reports / "reapproval_worklist_current.json").write_text(
                json.dumps(
                    {
                        "report_type": "reapproval_worklist",
                        "document_count": 199,
                        "reapproval_candidate_chunks": 5997,
                        "recommended_initial_review_chunks": 419,
                        "approval_provenance_missing_chunks": 197,
                        "approval_provenance_only_chunks": 50,
                        "approval_provenance_missing_field_counts": {
                            "approval_worklist_report_path": 197,
                            "approval_review_batch_manifest_path": 197,
                        },
                        "source_vector_integrity_failure_count": 0,
                        "initial_review_reduction_ratio": 0.9301,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (reports / "reapproval_worklist_current_chunks.csv").write_text(
                "document_rank,chunk_id,reapproval_reasons\n1,chunk-a,approval_provenance_missing\n",
                encoding="utf-8",
            )
            (reports / "reapproval_worklist_current_chunks.json").write_text(
                json.dumps(
                    {
                        "report_type": "reapproval_worklist_chunk_candidates",
                        "candidate_count": 5997,
                        "approval_provenance_missing_chunks": 197,
                        "candidates": [{"document_rank": 1, "chunk_id": "chunk-a"}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (reports / "reapproval_worklist_current.md").write_text("# Reapproval\n", encoding="utf-8")
            (reports / "reapproval_review_batches_current.json").write_text(
                json.dumps(
                    {
                        "report_type": "reapproval_review_batch_manifest",
                        "passed": True,
                        "candidate_count": 5997,
                        "selected_candidate_count": 5997,
                        "batch_count": 60,
                        "reapproval_chunk_count": 5997,
                        "max_chunks_per_batch": 100,
                        "risk_tier_chunk_counts": {"high": 5997},
                        "action_chunk_counts": {"reprocess_then_reapprove_and_reindex": 5997},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (reports / "reapproval_review_batches_current.csv").write_text(
                "batch_rank,reapproval_batch_id,chunk_count\n1,batch-a,100\n",
                encoding="utf-8",
            )
            (reports / "reapproval_review_batches_current.md").write_text("# Reapproval Batches\n", encoding="utf-8")
            (reports / "reapproval_review_batch_decisions_current.csv").write_text(
                (
                    "batch_rank,reapproval_batch_id,operator_decision,reviewer_id,reviewed_at,"
                    "chunk_decision_overrides_json,approval_scope_confirmation\n"
                    "1,batch-a,approve_all_reviewed,reviewer-a,2026-07-10T09:00:00+09:00,[],confirmed\n"
                ),
                encoding="utf-8",
            )
            (reports / "reapproval_decision_validation_current.json").write_text(
                json.dumps(
                    {
                        "report_type": "reapproval_decision_validation",
                        "repo_commit": "a" * 40,
                        "passed": True,
                        "release_gate_status": "ready_for_reapproval_apply",
                        "blocking_count": 0,
                        "warning_count": 0,
                        "expected_batch_count": 60,
                        "decision_row_count": 60,
                        "complete_row_count": 60,
                        "blank_or_incomplete_row_count": 0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (reports / "reapproval_decision_validation_current.md").write_text("# Decision Validation\n", encoding="utf-8")
            (reports / "reapproval_apply_plan_current.json").write_text(
                json.dumps(
                    {
                        "report_type": "reapproval_apply_plan",
                        "repo_commit": "a" * 40,
                        "passed": True,
                        "release_gate_status": "ready_for_apply_execution",
                        "blocker_count": 0,
                        "summary": {
                            "batch_count": 60,
                            "approve_chunk_count": 5997,
                            "reject_chunk_count": 0,
                        },
                        "operator_controls": {
                            "auto_approval": False,
                            "direct_approval_metadata_write_allowed": False,
                            "requires_shared_review_workflow_contract": True,
                            "requires_approval_precondition_validation": True,
                            "requires_preapproval_security_scan": True,
                            "official_mcp_publish_allowed_by_this_plan": False,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (reports / "reapproval_apply_plan_current.md").write_text("# Apply Plan\n", encoding="utf-8")
            (reports / "reapproval_review_burden_current.json").write_text(
                json.dumps(
                    {
                        "report_type": "reapproval_review_burden",
                        "repo_commit": "a" * 40,
                        "passed": True,
                        "status": "ready_for_operator_review",
                        "blocking_count": 0,
                        "warning_count": 0,
                        "release_gate_status": "ready_for_release_gate",
                        "release_blocker_count": 0,
                        "reapproval_candidate_chunks": 5997,
                        "baseline_full_review_minutes": 1999,
                        "recommended_initial_review_chunks": 419,
                        "estimated_initial_review_minutes": 140,
                        "initial_review_reduction_ratio": 0.9301,
                        "decision_template_row_count": 60,
                        "decision_template_operator_decision_complete_count": 60,
                        "decision_template_operator_decision_blank_count": 0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (reports / "reapproval_review_burden_current.md").write_text("# Burden\n", encoding="utf-8")
            (reports / "mcp_readiness_remediation_plan_current.json").write_text(
                json.dumps({"report_type": "mcp_readiness_remediation_plan", "passed": True}) + "\n",
                encoding="utf-8",
            )
            (reports / "mcp_readiness_remediation_plan_current.md").write_text("# Remediation\n", encoding="utf-8")
            (reports / "github_publish_readiness_current.json").write_text(
                json.dumps({"report_type": "github_publish_readiness_summary", "overall_status": "blocked"}) + "\n",
                encoding="utf-8",
            )
            (reports / "github_publish_readiness_current.md").write_text("# GitHub Publish\n", encoding="utf-8")
            (reports / "github_publish_execution_plan_current.json").write_text(
                json.dumps({"report_type": "github_publish_execution_plan", "overall_status": "blocked"}) + "\n",
                encoding="utf-8",
            )
            (reports / "github_publish_execution_plan_current.md").write_text("# Execution Plan\n", encoding="utf-8")
            (reports / "strict_public_readiness_gap_summary_current.json").write_text(
                json.dumps({"report_type": "strict_public_readiness_gap_summary", "passed": False}) + "\n",
                encoding="utf-8",
            )
            (reports / "strict_public_readiness_gap_summary_current.md").write_text("# Strict Gaps\n", encoding="utf-8")
            (reports / "strict_public_readiness_gap_worklist_current.csv").write_text(
                "issue_id,issue_type,severity,operator_action,filename,file_format,document_id,"
                "institution_name,profile_id,source_record_id,source_file_id,apba_id,"
                "failed_info_check_count,recommendation_count,top_recommendation,missing_fields\n"
                "strict-0001,failed_info_check,high,review_parser_evidence,rule.pdf,pdf,doc-1,"
                "기관,profile-1,record-1,file-1,apba-1,1,0,,\n",
                encoding="utf-8",
            )
            (reports / "strict_public_readiness_gap_worklist_current.md").write_text(
                "# Strict Gap Worklist\n",
                encoding="utf-8",
            )
            (reports / "temporal_ambiguity_review_scope_current.json").write_text(
                json.dumps(
                    {
                        "report_type": "temporal_ambiguity_review_scope",
                        "status": "temporal_ambiguity_policy_required",
                        "passed": False,
                        "summary": {
                            "chunk_count": 5997,
                            "before_temporal_metadata_count": 1174,
                            "after_temporal_metadata_count": 1584,
                            "delta_temporal_metadata_count": 410,
                            "conflict_chunk_count": 0,
                            "ambiguous_chunk_count": 4451,
                            "ambiguous_chunk_ratio": 0.7422,
                            "shadow_runtime_written": True,
                            "write_blocked": False,
                        },
                        "record_analysis": {
                            "vector_record_count": 5997,
                            "ambiguous_record_count": 4451,
                            "review_slice_count": 8,
                            "ambiguous_by_chunk_type": {"article": 2406, "form": 867},
                            "ambiguous_by_field_from_records": {"effective_date": 4227, "revision_date": 2203},
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (reports / "temporal_ambiguity_review_scope_current.md").write_text("# Temporal Scope\n", encoding="utf-8")
            (reports / "mcp_index_visibility_current.json").write_text(
                json.dumps(
                    {
                        "report_type": "mcp_index_visibility_audit",
                        "passed": True,
                        "blocking_count": 0,
                        "parser_uncertainty_summary": {
                            "record_count": 5997,
                            "parser_uncertainty_record_count": 17,
                            "missing_parser_uncertainty_count": 5980,
                            "risk_level_counts": {"medium": 12, "high": 5},
                            "flag_counts": {"hwp_table_geometry_uncertain": 5},
                        },
                        "approval_provenance_coverage": {
                            "record_count": 5997,
                            "complete_record_count": 5800,
                            "missing_field_counts": {
                                "approval_worklist_report_path": 197,
                                "approval_worklist_report_sha256": 197,
                                "approval_review_batch_manifest_path": 197,
                                "approval_review_batch_manifest_sha256": 197,
                            },
                        },
                        "approval_journal_coverage": {
                            "journal_record_count": 5997,
                            "record_count": 5997,
                            "eligible_record_count": 5997,
                            "matched_record_count": 5997,
                            "missing_record_count": 0,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (reports / "mcp_connection_readiness_current.json").write_text(
                json.dumps(
                    {
                        "report_type": "mcp_connection_readiness",
                        "passed": True,
                        "mcp_index_visibility_summary": {
                            "passed": True,
                            "tenant_id": "tenant-a",
                            "document_count": 1,
                            "total_approved_chunks": 5997,
                            "total_mcp_visible_records": 5997,
                            "approval_provenance_coverage": {
                                "record_count": 5997,
                                "complete_record_count": 5800,
                                "complete_ratio": 0.9672,
                                "missing_field_counts": {
                                    "approval_worklist_report_path": 197,
                                },
                            },
                            "approval_journal_coverage": {
                                "journal_record_count": 5997,
                                "record_count": 5997,
                                "eligible_record_count": 5997,
                                "matched_record_count": 5997,
                                "missing_record_count": 0,
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            index = evidence.build_release_evidence_index(
                root,
                evidence_profile="mcp-product-readiness",
                generated_at=GENERATED_AT,
            )

        self.assertEqual("mcp-product-readiness", index["evidence_profile"])
        self.assertEqual(
            [
                "reports/mcp_readiness_authority_current.json",
                "reports/mcp_readiness_authority_current.md",
                "reports/mcp_product_readiness_current.json",
                "reports/mcp_product_readiness_current.md",
                "reports/table_review_source_traceability_current.json",
                "reports/table_review_source_traceability_current.md",
                "reports/parsing_goldset_table_drift_check_current.json",
                "reports/parsing_goldset_table_drift_check_current.md",
                "reports/table_preprocessing_claim_gate_current.json",
                "reports/table_preprocessing_claim_gate_current.md",
                "reports/mcp_demo_answers_current.json",
                "reports/simple_rag_vs_mcp_accuracy_current.json",
                "reports/mcp_transport_smoke_current.json",
                "reports/mcp_index_visibility_current.json",
                "reports/mcp_connection_readiness_current.json",
                "reports/mcp_handoff_current.json",
                "reports/mcp_query_benchmark_current.json",
                "reports/mcp_query_benchmark_current.md",
                "reports/mcp_answer_evidence_bundle_current.json",
                "reports/mcp_answer_evidence_bundle_current.md",
                "reports/mcp_performance_load_evidence_current.json",
                "reports/mcp_performance_load_evidence_current.md",
                "reports/mcp_cold_start_benchmark_current.json",
                "reports/mcp_cold_start_benchmark_current.md",
                "reports/mcp_concurrent_benchmark_current.json",
                "reports/mcp_concurrent_benchmark_current.md",
                "reports/aks_mcp_publish_runtime_report.json",
                "data/aks_mcp_publish_runtime/reports/aks_mcp_publish_tenant-aks-publish_approval_worklist.json",
                "data/aks_mcp_publish_runtime/reports/aks_mcp_publish_tenant-aks-publish_approval_review_batches.json",
                "reports/approval_worklist_current.json",
                "reports/approval_worklist_current.md",
                "reports/approval_review_batches_current.json",
                "reports/approval_review_batches_current.md",
                "reports/reapproval_worklist_current.json",
                "reports/reapproval_worklist_current_chunks.csv",
                "reports/reapproval_worklist_current_chunks.json",
                "reports/reapproval_worklist_current.md",
                "reports/reapproval_review_batches_current.json",
                "reports/reapproval_review_batches_current.csv",
                "reports/reapproval_review_batches_current.md",
                "reports/reapproval_review_batch_decisions_current.csv",
                "reports/reapproval_decision_validation_current.json",
                "reports/reapproval_decision_validation_current.md",
                "reports/reapproval_apply_plan_current.json",
                "reports/reapproval_apply_plan_current.md",
                "reports/reapproval_review_burden_current.json",
                "reports/reapproval_review_burden_current.md",
                "reports/mcp_readiness_remediation_plan_current.json",
                "reports/mcp_readiness_remediation_plan_current.md",
                "reports/github_publish_readiness_current.json",
                "reports/github_publish_readiness_current.md",
                "reports/github_publish_execution_plan_current.json",
                "reports/github_publish_execution_plan_current.md",
                "reports/strict_public_readiness_gap_summary_current.json",
                "reports/strict_public_readiness_gap_summary_current.md",
                "reports/strict_public_readiness_gap_worklist_current.csv",
                "reports/strict_public_readiness_gap_worklist_current.md",
                "reports/temporal_ambiguity_review_scope_current.json",
                "reports/temporal_ambiguity_review_scope_current.md",
            ],
            [artifact["artifact_path"] for artifact in index["artifacts"]],
        )
        authority_summary = index["artifacts"][0]["json_summary"]
        product_summary = index["artifacts"][2]["json_summary"]
        artifact_summaries = {artifact["artifact_path"]: artifact.get("json_summary", {}) for artifact in index["artifacts"]}
        visibility_summary = artifact_summaries["reports/mcp_index_visibility_current.json"]
        connection_summary = artifact_summaries["reports/mcp_connection_readiness_current.json"]
        benchmark_summary = artifact_summaries["reports/mcp_query_benchmark_current.json"]
        performance_load_summary = artifact_summaries["reports/mcp_performance_load_evidence_current.json"]
        cold_start_summary = artifact_summaries["reports/mcp_cold_start_benchmark_current.json"]
        concurrent_summary = artifact_summaries["reports/mcp_concurrent_benchmark_current.json"]
        publish_runtime_summary = artifact_summaries["reports/aks_mcp_publish_runtime_report.json"]
        publish_approval_worklist_summary = artifact_summaries[
            "data/aks_mcp_publish_runtime/reports/aks_mcp_publish_tenant-aks-publish_approval_worklist.json"
        ]
        publish_approval_batches_summary = artifact_summaries[
            "data/aks_mcp_publish_runtime/reports/aks_mcp_publish_tenant-aks-publish_approval_review_batches.json"
        ]
        reapproval_summary = artifact_summaries["reports/reapproval_worklist_current.json"]
        reapproval_chunks_summary = artifact_summaries["reports/reapproval_worklist_current_chunks.json"]
        reapproval_batches_summary = artifact_summaries["reports/reapproval_review_batches_current.json"]
        reapproval_decision_summary = artifact_summaries["reports/reapproval_decision_validation_current.json"]
        reapproval_apply_plan_summary = artifact_summaries["reports/reapproval_apply_plan_current.json"]
        reapproval_burden_summary = artifact_summaries["reports/reapproval_review_burden_current.json"]
        temporal_ambiguity_summary = artifact_summaries["reports/temporal_ambiguity_review_scope_current.json"]
        self.assertEqual(1, authority_summary["authority_version"])
        self.assertEqual(1, authority_summary["authoritative_artifact_count"])
        self.assertEqual(1, authority_summary["supersedes_count"])
        self.assertEqual(1, product_summary["source_report_artifact_count"])
        self.assertEqual(1174, product_summary["temporal_coverage_summary_with_temporal_metadata_count"])
        self.assertEqual(410, product_summary["temporal_backfill_shadow_summary_delta_temporal_metadata_count"])
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
        self.assertEqual(
            "blocked_evidence_drift",
            product_summary["table_preprocessing_claim_gate_summary_status"],
        )
        self.assertEqual(
            255,
            product_summary["table_preprocessing_claim_gate_summary_pending_unit_count"],
        )
        self.assertEqual(
            10,
            product_summary["table_preprocessing_claim_gate_summary_source_traceability_issue_count"],
        )
        self.assertFalse(
            product_summary[
                "table_preprocessing_claim_gate_summary_source_traceability_require_page_count_verification"
            ]
        )
        self.assertEqual(
            1,
            product_summary["table_preprocessing_claim_gate_summary_drift_check_blocker_count"],
        )
        self.assertEqual(5800, product_summary["runtime_summary_approval_provenance_complete_record_count"])
        self.assertEqual(197, product_summary["runtime_summary_approval_provenance_missing_approval_worklist_report_path_count"])
        self.assertEqual(197, product_summary["runtime_summary_approval_provenance_missing_approval_worklist_report_sha256_count"])
        self.assertEqual(197, product_summary["runtime_summary_approval_provenance_missing_approval_review_batch_manifest_path_count"])
        self.assertEqual(197, product_summary["runtime_summary_approval_provenance_missing_approval_review_batch_manifest_sha256_count"])
        self.assertEqual(69, product_summary["approval_workload_summary_manual_attention_chunks"])
        self.assertEqual(1153, product_summary["approval_workload_summary_low_risk_batch_review_candidate_chunks"])
        self.assertEqual(18, product_summary["approval_review_batch_summary_batch_count"])
        self.assertEqual(1222, product_summary["approval_review_batch_summary_approval_chunk_count"])
        self.assertEqual(419, product_summary["reapproval_workload_summary_recommended_initial_review_chunks"])
        self.assertEqual(197, product_summary["reapproval_workload_summary_approval_provenance_missing_chunks"])
        self.assertEqual(50, product_summary["reapproval_workload_summary_approval_provenance_only_chunks"])
        self.assertEqual(
            197,
            product_summary[
                "reapproval_workload_summary_approval_provenance_missing_approval_worklist_report_path_count"
            ],
        )
        self.assertEqual(0.9301, product_summary["reapproval_workload_summary_initial_review_reduction_ratio"])
        self.assertEqual(4451, temporal_ambiguity_summary["temporal_ambiguity_ambiguous_chunk_count"])
        self.assertEqual(0.7422, temporal_ambiguity_summary["temporal_ambiguity_ambiguous_chunk_ratio"])
        self.assertEqual(5997, temporal_ambiguity_summary["temporal_ambiguity_vector_record_count"])
        self.assertEqual("mcp_query_benchmark", benchmark_summary["report_type"])
        self.assertEqual(5, benchmark_summary["query_count"])
        self.assertEqual("mcp_performance_load_evidence", performance_load_summary["report_type"])
        self.assertTrue(performance_load_summary["evidence_ready"])
        self.assertEqual("mcp_cold_start_benchmark", cold_start_summary["report_type"])
        self.assertEqual(3, cold_start_summary["iterations"])
        self.assertEqual("mcp_concurrent_query_benchmark", concurrent_summary["report_type"])
        self.assertEqual(10, concurrent_summary["task_count"])
        self.assertEqual(
            {"article": 2406, "form": 867},
            temporal_ambiguity_summary["temporal_ambiguity_ambiguous_by_chunk_type"],
        )
        self.assertEqual(60, product_summary["reapproval_review_batch_summary_batch_count"])
        self.assertEqual(5997, product_summary["reapproval_review_batch_summary_selected_candidate_count"])
        self.assertEqual(5997, product_summary["reapproval_review_batch_summary_reapproval_chunk_count"])
        self.assertEqual(
            {"high": 5997},
            product_summary["reapproval_review_batch_summary_risk_tier_chunk_counts"],
        )
        self.assertEqual(
            5997,
            product_summary["reapproval_review_batch_summary_risk_tier_chunk_counts_high_count"],
        )
        self.assertEqual(17, visibility_summary["parser_uncertainty_summary_parser_uncertainty_record_count"])
        self.assertEqual({"medium": 12, "high": 5}, visibility_summary["parser_uncertainty_summary_risk_level_counts"])
        self.assertEqual(5800, visibility_summary["approval_provenance_coverage_complete_record_count"])
        self.assertEqual(197, visibility_summary["approval_provenance_coverage_missing_approval_worklist_report_path_count"])
        self.assertEqual(197, visibility_summary["approval_provenance_coverage_missing_approval_review_batch_manifest_path_count"])
        self.assertEqual(197, visibility_summary["approval_provenance_coverage_missing_approval_review_batch_manifest_sha256_count"])
        self.assertEqual(5997, visibility_summary["approval_journal_coverage_matched_record_count"])
        self.assertEqual(0, visibility_summary["approval_journal_coverage_missing_record_count"])
        self.assertEqual(5997, connection_summary["mcp_index_visibility_approval_journal_coverage_matched_record_count"])
        self.assertEqual(0, connection_summary["mcp_index_visibility_approval_journal_coverage_missing_record_count"])
        self.assertEqual(5800, connection_summary["mcp_index_visibility_approval_provenance_coverage_complete_record_count"])
        self.assertEqual("aks_mcp_publish_runtime", publish_runtime_summary["report_type"])
        self.assertEqual(5997, publish_runtime_summary["source_chunk_count"])
        self.assertEqual(5997, publish_runtime_summary["selected_chunk_count"])
        self.assertEqual(5997, publish_runtime_summary["approved_chunk_count"])
        self.assertEqual(5997, publish_runtime_summary["indexed_record_count"])
        self.assertEqual(2, publish_runtime_summary["approval_record_count"])
        self.assertEqual(
            "reports/aks_mcp_publish_tenant-aks-publish_approval_worklist.json",
            publish_runtime_summary["approval_evidence_worklist_report_path"],
        )
        self.assertEqual("b" * 64, publish_runtime_summary["approval_evidence_worklist_report_sha256"])
        self.assertEqual(5997, publish_runtime_summary["approval_evidence_approval_chunk_count"])
        self.assertEqual(1993, publish_runtime_summary["approval_evidence_manual_attention_chunk_count"])
        self.assertEqual(1, publish_runtime_summary["approval_evidence_review_type_batch_counts_manual_attention_count"])
        self.assertEqual(5997, publish_approval_worklist_summary["total_chunks"])
        self.assertEqual(1993, publish_approval_worklist_summary["manual_attention_chunks"])
        self.assertEqual(4004, publish_approval_worklist_summary["low_risk_batch_review_candidate_chunks"])
        self.assertEqual(2, publish_approval_batches_summary["batch_count"])
        self.assertEqual(5997, publish_approval_batches_summary["approval_chunk_count"])
        self.assertEqual(0, publish_approval_batches_summary["blocker_count"])
        self.assertEqual(5997, reapproval_summary["reapproval_candidate_chunks"])
        self.assertEqual(197, reapproval_summary["approval_provenance_missing_chunks"])
        self.assertEqual(50, reapproval_summary["approval_provenance_only_chunks"])
        self.assertEqual(197, reapproval_summary["approval_provenance_missing_approval_worklist_report_path_count"])
        self.assertEqual("reapproval_worklist_chunk_candidates", reapproval_chunks_summary["report_type"])
        self.assertEqual(5997, reapproval_chunks_summary["candidate_count"])
        self.assertEqual("reapproval_review_batch_manifest", reapproval_batches_summary["report_type"])
        self.assertEqual(60, reapproval_batches_summary["batch_count"])
        self.assertEqual(5997, reapproval_batches_summary["reapproval_chunk_count"])
        self.assertEqual("reapproval_decision_validation", reapproval_decision_summary["report_type"])
        self.assertEqual("ready_for_reapproval_apply", reapproval_decision_summary["release_gate_status"])
        self.assertEqual(60, reapproval_decision_summary["complete_row_count"])
        self.assertEqual("reapproval_apply_plan", reapproval_apply_plan_summary["report_type"])
        self.assertEqual("ready_for_apply_execution", reapproval_apply_plan_summary["release_gate_status"])
        self.assertEqual("reapproval_review_burden", reapproval_burden_summary["report_type"])
        self.assertEqual(5997, reapproval_burden_summary["reapproval_candidate_chunks"])
        self.assertEqual(419, reapproval_burden_summary["recommended_initial_review_chunks"])
        self.assertEqual(0.9301, reapproval_burden_summary["initial_review_reduction_ratio"])
        self.assertEqual("ready_for_release_gate", reapproval_burden_summary["release_gate_status"])
        self.assertEqual(0, reapproval_burden_summary["release_blocker_count"])
        self.assertEqual(60, reapproval_burden_summary["decision_template_operator_decision_complete_count"])
        self.assertEqual(0, reapproval_burden_summary["decision_template_operator_decision_blank_count"])

    def test_default_artifacts_include_allowlist_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".release-hygiene-allowlist.json").write_text(
                json.dumps({"allowed_findings": []}) + "\n",
                encoding="utf-8",
            )

            index = evidence.build_release_evidence_index(root, generated_at=GENERATED_AT)

        self.assertIn(".release-hygiene-allowlist.json", [artifact["artifact_path"] for artifact in index["artifacts"]])

    def test_repo_worktree_state_records_dirty_counts_without_paths(self) -> None:
        def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
            if args[-2:] == ["rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(args, 0, stdout=(b"f" * 40) + b"\n", stderr=b"")
            return subprocess.CompletedProcess(args, 0, stdout=b" M scripts/tool.py\n?? docs/\n", stderr=b"")

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(evidence.subprocess, "run", side_effect=fake_run):
            index = evidence.build_release_evidence_index(
                Path(temp_dir),
                artifact_paths=[],
                generated_at=GENERATED_AT,
            )

        self.assertEqual("f" * 40, index["repo_commit"])
        self.assertEqual(
            {
                "state": "dirty",
                "dirty": True,
                "tracked_change_count": 1,
                "untracked_change_count": 1,
            },
            index["repo_worktree"],
        )
        self.assertNotIn("scripts/tool.py", json.dumps(index))
        self.assertNotIn("docs/", json.dumps(index))


if __name__ == "__main__":
    unittest.main()
