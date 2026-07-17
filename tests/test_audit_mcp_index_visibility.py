from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api import routes_documents
from app.core.config import Settings
from app.core.security import AuthContext
from app.core.tenant_access import settings_for_tenant
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository
from scripts.audit_mcp_index_visibility import audit_mcp_index_visibility


class AuditMcpIndexVisibilityTests(unittest.TestCase):
    def test_report_passes_for_indexed_approved_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp), tenant_storage_isolation=True)
            _prepare_document(settings, document_id="doc_real", filename="real.pdf")

            report = audit_mcp_index_visibility(
                data_dir=settings.data_dir,
                tenant_id="tenant-a",
                tenant_storage_isolation=True,
                min_visible_records=1,
                forbid_smoke_docs=True,
                require_indexed=True,
            )

        self.assertTrue(report["passed"])
        self.assertEqual(report["document_count"], 1)
        self.assertEqual(report["approval_status_totals"], {"approved": 1})
        self.assertEqual(report["total_approved_chunks"], 1)
        self.assertEqual(report["total_indexable_record_count"], 1)
        self.assertEqual(report["total_mcp_visible_records"], 1)
        self.assertEqual(report["auth_scope"], {"role": "operator", "department_ids": []})
        self.assertEqual(report["preapproval_visibility_guard"]["status"], "approved_runtime")
        self.assertEqual(report["status_counts"], {"indexed": 1})
        self.assertEqual(report["smoke_like_document_count"], 0)
        self.assertEqual(report["parser_evidence_summary"]["hwpx_evidence_document_count"], 0)
        self.assertEqual(report["approval_journal_coverage"]["eligible_record_count"], 1)
        self.assertEqual(report["approval_journal_coverage"]["matched_record_count"], 1)
        self.assertEqual(report["approval_journal_coverage"]["missing_record_count"], 0)

    def test_preapproval_visibility_guard_passes_when_drafts_are_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp), tenant_storage_isolation=True)
            _prepare_document(settings, document_id="doc_draft", filename="draft.pdf", approve_and_index=False)

            report = audit_mcp_index_visibility(
                data_dir=settings.data_dir,
                tenant_id="tenant-a",
                tenant_storage_isolation=True,
                min_visible_records=0,
                require_indexed=False,
            )

        self.assertTrue(report["passed"])
        self.assertEqual(report["approval_status_totals"], {"draft": 1})
        self.assertEqual(0, report["total_approved_chunks"])
        self.assertEqual(0, report["total_mcp_visible_records"])
        self.assertEqual(1, report["total_skipped_unapproved_count"])
        self.assertEqual(
            {
                "passed": True,
                "status": "no_approved_chunks_no_visible_records",
                "approved_chunks": 0,
                "mcp_visible_records": 0,
                "skipped_unapproved_count": 1,
            },
            report["preapproval_visibility_guard"],
        )

    def test_report_fails_when_smoke_document_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp), tenant_storage_isolation=True)
            _prepare_document(settings, document_id="doc_mcp_smoke_v1", filename="mcp_smoke.pdf")

            report = audit_mcp_index_visibility(
                data_dir=settings.data_dir,
                tenant_id="tenant-a",
                tenant_storage_isolation=True,
                min_visible_records=1,
                forbid_smoke_docs=True,
                require_indexed=True,
            )

        self.assertFalse(report["passed"])
        self.assertEqual(report["smoke_like_document_count"], 1)
        self.assertIn("smoke-documents-visible", {finding["code"] for finding in report["findings"]})

    def test_report_fails_for_wrong_tenant_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp), tenant_storage_isolation=True)
            _prepare_document(settings, document_id="doc_real", filename="real.pdf")

            report = audit_mcp_index_visibility(
                data_dir=settings.data_dir,
                tenant_id="tenant-b",
                tenant_storage_isolation=True,
                min_visible_records=1,
                require_indexed=True,
            )

        self.assertFalse(report["passed"])
        self.assertEqual(report["document_count"], 0)
        self.assertIn("no-documents", {finding["code"] for finding in report["findings"]})
        self.assertIn("too-few-visible-records", {finding["code"] for finding in report["findings"]})

    def test_report_fails_when_approval_journal_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp), tenant_storage_isolation=True)
            _prepare_document(settings, document_id="doc_no_journal", filename="real.pdf")
            journal_path = (
                settings_for_tenant(settings, "tenant-a").data_dir
                / "repository"
                / "journals"
                / "approvals.jsonl"
            )
            journal_path.unlink()

            report = audit_mcp_index_visibility(
                data_dir=settings.data_dir,
                tenant_id="tenant-a",
                tenant_storage_isolation=True,
                min_visible_records=1,
                require_indexed=True,
            )

        self.assertFalse(report["passed"])
        self.assertEqual(report["approval_journal_coverage"]["eligible_record_count"], 1)
        self.assertEqual(report["approval_journal_coverage"]["matched_record_count"], 0)
        self.assertEqual(report["approval_journal_coverage"]["missing_record_count"], 1)
        self.assertIn("approval-journal-evidence-missing", {finding["code"] for finding in report["findings"]})

    def test_report_summarizes_parser_evidence_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp), tenant_storage_isolation=True)
            _prepare_document(
                settings,
                document_id="doc_hwpx_hwp",
                filename="rules.hwpx",
                chunk_metadata={
                    "source_hwpx_xml_block_indices": [10],
                    "source_hwpx_nested_table_text_snippets": ["nested cell text"],
                    "source_hwp_extraction_modes": ["legacy_ole_para_text_only"],
                    "source_hwp_native_table_geometry": False,
                    "parser_uncertainty_risk_level": "medium",
                    "parser_uncertainty_flags": ["hwp_table_geometry_uncertain", "nested_table_text"],
                },
            )

            report = audit_mcp_index_visibility(
                data_dir=settings.data_dir,
                tenant_id="tenant-a",
                tenant_storage_isolation=True,
                min_visible_records=1,
                require_indexed=True,
            )

        summary = report["parser_evidence_summary"]
        self.assertEqual(summary["hwpx_evidence_document_count"], 1)
        self.assertEqual(summary["hwp_extraction_mode_document_count"], 1)
        self.assertEqual(summary["hwp_native_table_geometry_review_document_count"], 1)
        self.assertEqual(summary["hwpx_metadata_counts"]["source_hwpx_xml_block_indices"], 1)
        self.assertEqual(summary["hwpx_metadata_counts"]["source_hwpx_nested_table_text_snippets"], 1)
        self.assertEqual(summary["hwp_metadata_counts"]["source_hwp_extraction_modes"], 1)
        self.assertEqual(summary["hwp_metadata_counts"]["source_hwp_native_table_geometry_false"], 1)
        self.assertEqual(report["documents"][0]["hwpx_metadata_counts"]["source_hwpx_xml_block_indices"], 1)
        uncertainty = report["parser_uncertainty_summary"]
        self.assertEqual(uncertainty["record_count"], 1)
        self.assertEqual(uncertainty["parser_uncertainty_record_count"], 1)
        self.assertEqual(uncertainty["missing_parser_uncertainty_count"], 0)
        self.assertEqual(uncertainty["risk_level_counts"], {"medium": 1})
        self.assertEqual(
            uncertainty["flag_counts"],
            {"hwp_table_geometry_uncertain": 1, "nested_table_text": 1},
        )
        self.assertEqual(report["documents"][0]["parser_uncertainty_summary"]["risk_level_counts"], {"medium": 1})
        coverage = report["approval_provenance_coverage"]
        self.assertEqual(coverage["record_count"], 1)
        self.assertEqual(coverage["field_counts"]["approval_id"], 1)
        self.assertEqual(coverage["field_counts"]["approved_content_hash"], 1)
        self.assertEqual(coverage["missing_field_counts"]["approval_worklist_report_path"], 0)
        self.assertEqual(coverage["missing_field_counts"]["approval_worklist_report_sha256"], 0)
        self.assertEqual(coverage["missing_field_counts"]["approval_review_batch_manifest_path"], 0)
        self.assertEqual(coverage["missing_field_counts"]["approval_review_batch_manifest_sha256"], 0)
        self.assertEqual(coverage["complete_record_count"], 1)
        self.assertEqual(report["documents"][0]["approval_provenance_coverage"]["field_counts"]["approval_id"], 1)
        journal_coverage = report["approval_journal_coverage"]
        self.assertEqual(journal_coverage["eligible_record_count"], 1)
        self.assertEqual(journal_coverage["matched_record_count"], 1)
        self.assertEqual(journal_coverage["missing_record_count"], 0)

    def test_report_does_not_warn_when_all_indexed_vectors_lack_parser_uncertainty_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp), tenant_storage_isolation=True)
            _prepare_document(settings, document_id="doc_missing_uncertainty", filename="rules.pdf")

            report = audit_mcp_index_visibility(
                data_dir=settings.data_dir,
                tenant_id="tenant-a",
                tenant_storage_isolation=True,
                min_visible_records=1,
                require_indexed=True,
            )

        self.assertTrue(report["passed"])
        self.assertNotIn("parser-uncertainty-metadata-missing", {finding["code"] for finding in report["findings"]})
        self.assertEqual(1, report["parser_uncertainty_summary"]["missing_parser_uncertainty_count"])

    def test_report_warns_when_only_some_indexed_vectors_have_parser_uncertainty_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp), tenant_storage_isolation=True)
            tenant_settings = settings_for_tenant(settings, "tenant-a")
            repository = JsonRepository(tenant_settings)
            document_id = "doc_partial_uncertainty"
            repository.upsert_document(
                Document(
                    document_id=document_id,
                    filename="rules.pdf",
                    document_name="rules.pdf",
                    file_type="pdf",
                    file_hash=f"hash-{document_id}",
                    institution_name="Test Institution",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            chunks = [
                Chunk(
                    chunk_id=f"{document_id}:chunk-1",
                    document_id=document_id,
                    chunk_type="article",
                    text="approved regulation text",
                    retrieval_text="approved regulation text",
                    security_level="internal",
                    metadata={
                        "parser_uncertainty_risk_level": "medium",
                        "parser_uncertainty_flags": ["nested_table_text"],
                    },
                ),
                Chunk(
                    chunk_id=f"{document_id}:chunk-2",
                    document_id=document_id,
                    chunk_type="article",
                    text="approved regulation text",
                    retrieval_text="approved regulation text",
                    security_level="internal",
                ),
            ]
            repository.save_processing_result(document_id, [], chunks, [])
            evidence = _write_approval_evidence(settings, tenant_settings=tenant_settings, document_id=document_id, chunks=chunks)
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            with patch.object(routes_documents, "get_settings", return_value=settings):
                routes_documents.approve_review_chunks(
                    document_id,
                    routes_documents.ApprovalRequest(
                        chunk_ids=[chunk.chunk_id for chunk in chunks],
                        approval_id=f"approval-{document_id}",
                        review_flags_acknowledged=True,
                        **evidence,
                    ),
                    auth,
                )
                routes_documents.index_document(
                    document_id,
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    auth,
                )

            report = audit_mcp_index_visibility(
                data_dir=settings.data_dir,
                tenant_id="tenant-a",
                tenant_storage_isolation=True,
                min_visible_records=1,
                require_indexed=True,
            )

        self.assertTrue(report["passed"])
        self.assertIn(
            "parser-uncertainty-metadata-missing",
            {finding["code"] for finding in report["findings"]},
        )
        self.assertEqual(1, report["parser_uncertainty_summary"]["parser_uncertainty_record_count"])
        self.assertEqual(1, report["parser_uncertainty_summary"]["missing_parser_uncertainty_count"])

    def test_source_filters_scope_approval_visibility_totals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp), tenant_storage_isolation=True)
            _prepare_document(
                settings,
                document_id="doc_public_portal",
                filename="public_portal.pdf",
                apba_id="C9999",
                source_system="PUBLIC_PORTAL",
            )
            _prepare_document(
                settings,
                document_id="doc_local",
                filename="local.pdf",
                apba_id="LOCAL",
                source_system="LOCAL",
            )

            report = audit_mcp_index_visibility(
                data_dir=settings.data_dir,
                tenant_id="tenant-a",
                tenant_storage_isolation=True,
                min_visible_records=1,
                source_system="PUBLIC_PORTAL",
                apba_id="C9999",
            )

        self.assertTrue(report["passed"])
        self.assertEqual(report["filters"], {"apba_id": "C9999", "source_system": "PUBLIC_PORTAL"})
        self.assertEqual(report["document_count"], 1)
        self.assertEqual(report["documents"][0]["document_id"], "doc_public_portal")
        self.assertEqual(report["documents"][0]["apba_id"], "C9999")
        self.assertEqual(report["approval_status_totals"], {"approved": 1})
        self.assertEqual(report["total_mcp_visible_records"], 1)

    def test_public_portal_runtime_without_apba_id_warns_for_institution_evidence_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp), tenant_storage_isolation=True)
            _prepare_document(
                settings,
                document_id="doc_public_portal_missing_apba",
                filename="public_portal.pdf",
                source_system="PUBLIC_PORTAL",
                approve_and_index=False,
            )

            report = audit_mcp_index_visibility(
                data_dir=settings.data_dir,
                tenant_id="tenant-a",
                tenant_storage_isolation=True,
                min_visible_records=0,
                source_system="PUBLIC_PORTAL",
            )

        self.assertTrue(report["passed"])
        self.assertEqual(report["source_identity_summary"]["source_system_counts"], {"PUBLIC_PORTAL": 1})
        self.assertEqual(report["source_identity_summary"]["apba_id_counts"], {"missing": 1})
        self.assertEqual(report["source_identity_summary"]["public_portal_missing_apba_id_count"], 1)
        self.assertIn("public_portal-apba-id-missing", {finding["code"] for finding in report["findings"]})

    def test_department_acl_requires_matching_mcp_visibility_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp), tenant_storage_isolation=True)
            _prepare_document(
                settings,
                document_id="doc_acl_hr",
                filename="acl.pdf",
                department_acl=["hr"],
            )

            hidden_report = audit_mcp_index_visibility(
                data_dir=settings.data_dir,
                tenant_id="tenant-a",
                tenant_storage_isolation=True,
                min_visible_records=0,
                require_indexed=True,
            )
            visible_report = audit_mcp_index_visibility(
                data_dir=settings.data_dir,
                tenant_id="tenant-a",
                tenant_storage_isolation=True,
                min_visible_records=1,
                require_indexed=True,
                department_ids=["hr"],
            )

        self.assertTrue(hidden_report["passed"])
        self.assertEqual(hidden_report["total_indexable_record_count"], 1)
        self.assertEqual(hidden_report["total_mcp_visible_records"], 0)
        self.assertEqual(hidden_report["documents"][0]["indexable_record_count"], 1)
        self.assertEqual(hidden_report["documents"][0]["mcp_visible_record_count"], 0)
        self.assertIn("mcp-scope-hidden-records", {finding["code"] for finding in hidden_report["findings"]})

        self.assertTrue(visible_report["passed"])
        self.assertEqual(visible_report["auth_scope"], {"role": "operator", "department_ids": ["hr"]})
        self.assertEqual(visible_report["total_indexable_record_count"], 1)
        self.assertEqual(visible_report["total_mcp_visible_records"], 1)
        self.assertNotIn("mcp-scope-hidden-records", {finding["code"] for finding in visible_report["findings"]})


def _prepare_document(
    settings: Settings,
    *,
    document_id: str,
    filename: str,
    chunk_metadata: dict | None = None,
    approve_and_index: bool = True,
    apba_id: str | None = None,
    source_system: str | None = None,
    security_level: str = "internal",
    department_acl: list[str] | None = None,
) -> None:
    tenant_settings = settings_for_tenant(settings, "tenant-a")
    repository = JsonRepository(tenant_settings)
    repository.upsert_document(
        Document(
            document_id=document_id,
            filename=filename,
            document_name=filename,
            file_type="pdf",
            file_hash=f"hash-{document_id}",
            institution_name="Test Institution",
            apba_id=apba_id,
            source_system=source_system,
            tenant_id="tenant-a",
            status="completed",
        )
    )
    chunk = Chunk(
        chunk_id=f"{document_id}:chunk-1",
        document_id=document_id,
        chunk_type="article",
        text="approved regulation text",
        retrieval_text="approved regulation text",
        security_level=security_level,
        department_acl=department_acl or [],
        metadata={
            **(chunk_metadata or {}),
            **({"apba_id": apba_id} if apba_id else {}),
            **({"source_system": source_system} if source_system else {}),
        },
    )
    repository.save_processing_result(
        document_id,
        [],
        [chunk],
        [],
    )
    if not approve_and_index:
        return
    evidence = _write_approval_evidence(settings, tenant_settings=tenant_settings, document_id=document_id, chunks=[chunk])
    auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
    with patch.object(routes_documents, "get_settings", return_value=settings):
        routes_documents.approve_review_chunks(
            document_id,
            routes_documents.ApprovalRequest(
                chunk_ids=[f"{document_id}:chunk-1"],
                approval_id=f"approval-{document_id}",
                security_level=security_level,
                review_flags_acknowledged=True,
                **evidence,
            ),
            auth,
        )
        routes_documents.index_document(
            document_id,
            routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
            auth,
        )


def _write_approval_evidence(
    settings: Settings,
    *,
    tenant_settings: Settings,
    document_id: str,
    chunks: list[Chunk],
) -> dict[str, str]:
    reports = settings.artifact_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    worklist_relative = f"reports/{document_id}_approval_worklist.json"
    batch_relative = f"reports/{document_id}_approval_review_batches.json"
    worklist_path = settings.artifact_root / worklist_relative
    batch_path = settings.artifact_root / batch_relative
    worklist = {
        "report_type": "approval_worklist",
        "generated_at": "2026-07-10T00:00:00+00:00",
        "data_dir": str(settings.data_dir),
        "effective_data_dir": str(tenant_settings.data_dir),
        "tenant_id": "tenant-a",
        "tenant_storage_isolation": True,
        "document_count": 1,
        "total_chunks": len(chunks),
        "manual_attention_chunks": 0,
        "low_risk_batch_review_candidate_chunks": len(chunks),
        "documents": [{"document_id": document_id, "total_chunks": len(chunks)}],
    }
    worklist_path.write_text(json.dumps(worklist, ensure_ascii=False, indent=2), encoding="utf-8")
    worklist_sha256 = _sha256_file(worklist_path)
    review_type = "low_risk_batch"
    batch_chunks = [
        {
            "chunk_id": chunk.chunk_id,
            "review_content_hash": routes_documents._review_content_hash(chunk),
            "approval_status": chunk.approval_status,
            "review_priority_tier": "no_signal",
            "review_category": "low_risk_batch_review_candidate",
            "attention_reasons": [],
        }
        for chunk in chunks
    ]
    fingerprint = routes_documents._review_batch_chunk_fingerprint(batch_chunks, review_type)
    batch_id = f"approval-{worklist_sha256[:12]}-001-low-risk-batch-001-{fingerprint[:12]}"
    manifest = {
        "report_type": "approval_review_batch_manifest",
        "generated_at": "2026-07-10T00:00:01+00:00",
        "data_dir": str(settings.data_dir),
        "effective_data_dir": str(tenant_settings.data_dir),
        "tenant_id": "tenant-a",
        "tenant_storage_isolation": True,
        "worklist_report": {
            "path": str(worklist_path),
            "approval_request_path": worklist_relative,
            "sha256": worklist_sha256,
            "effective_data_dir": str(tenant_settings.data_dir),
            "tenant_id": "tenant-a",
            "tenant_storage_isolation": True,
            "document_count": 1,
            "total_chunks": len(chunks),
        },
        "batch_count": 1,
        "approval_chunk_count": len(chunks),
        "batches": [
            {
                "batch_rank": 1,
                "review_batch_id": batch_id,
                "review_batch_chunk_fingerprint": fingerprint,
                "review_type": review_type,
                "review_strategy": "human_bulk_review",
                "document_id": document_id,
                "chunk_count": len(chunks),
                "chunk_ids": [chunk.chunk_id for chunk in chunks],
                "chunks": batch_chunks,
                "review_flags_acknowledged_required": False,
            }
        ],
    }
    batch_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "worklist_report_path": worklist_relative,
        "worklist_report_sha256": worklist_sha256,
        "review_batch_manifest_path": batch_relative,
        "review_batch_manifest_sha256": _sha256_file(batch_path),
        "review_batch_id": batch_id,
        "review_batch_chunk_fingerprint": fingerprint,
        "review_strategy": "human_bulk_review",
    }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
