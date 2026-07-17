from __future__ import annotations

import tempfile
import unittest
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from app.api import routes_documents, routes_rag
from app.core.config import Settings
from app.core.security import AuthContext
from app.core.tenant_access import settings_for_tenant
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository
from scripts.build_rag_security_evidence import _approval_vector_sync_failures, build_rag_security_evidence


class BuildRagSecurityEvidenceTests(unittest.TestCase):
    def test_report_passes_for_approved_current_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            _prepare_indexed_document(settings)

            report = build_rag_security_evidence(data_dir=settings.data_dir, tenant_id="tenant-a")

        self.assertTrue(report["passed"])
        self.assertEqual(report["stale_vector_record_count"], 0)
        self.assertEqual(report["metadata_failure_count"], 0)
        self.assertGreaterEqual(report["api_audit_record_count"], 2)
        self.assertEqual(report["api_audit_action_counts"]["document.review.approve"], 1)
        self.assertEqual(report["api_audit_action_counts"]["document.index"], 1)
        self.assertEqual(report["api_audit_action_counts"]["rag.search"], 1)
        self.assertEqual(report["approval_record_source"], "append_only_journal")
        self.assertEqual(report["approval_journal_record_count"], 1)
        self.assertEqual(report["approval_vector_sync_outcome_count"], 1)
        self.assertEqual(report["approval_vector_sync_failure_count"], 0)
        self.assertEqual(report["approval_chain_failure_count"], 0)
        self.assertEqual(report["indexing_job_failure_count"], 0)
        self.assertEqual(report["audit_control_failure_count"], 0)
        self.assertEqual(report["component_integrity_failure_count"], 0)
        self.assertEqual(len(report["component_manifest_hash"]), 64)
        self.assertEqual(report["component_manifest"]["vector_store_target"], "local-jsonl")
        self.assertIn("app/api/routes_rag.py", {item["path"] for item in report["component_manifest"]["source_files"]})
        self.assertTrue(all(len(item["sha256"]) == 64 for item in report["component_manifest"]["source_files"]))

    def test_report_fails_when_approval_vector_sync_outcome_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            _prepare_indexed_document(settings)
            journal_path = settings.data_dir / "repository" / "journals" / "maintenance_events.jsonl"
            journal_path.write_text("", encoding="utf-8")

            report = build_rag_security_evidence(data_dir=settings.data_dir, tenant_id="tenant-a")

        self.assertFalse(report["passed"])
        self.assertEqual(report["approval_vector_sync_outcome_count"], 0)
        self.assertEqual(report["approval_vector_sync_failure_count"], 1)
        self.assertEqual(
            report["approval_vector_sync_failure_samples"][0]["reason"],
            "missing_sync_outcome_event",
        )

    def test_report_fails_closed_for_legacy_approval_without_sync_event_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            _prepare_indexed_document(settings)
            journal_path = settings.data_dir / "repository" / "journals" / "approvals.jsonl"
            rows = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
            rows[0].pop("vector_sync_event_id")
            journal_path.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                encoding="utf-8",
            )

            report = build_rag_security_evidence(data_dir=settings.data_dir, tenant_id="tenant-a")

        self.assertFalse(report["passed"])
        self.assertEqual(1, report["approval_vector_sync_legacy_approval_count"])
        self.assertFalse(report["approval_vector_sync_policy"]["legacy_approvals_grandfathered"])
        self.assertEqual(
            "fail_closed_requires_audited_backfill_or_reapproval",
            report["approval_vector_sync_policy"]["legacy_approval_policy"],
        )
        self.assertIn(
            "approval_missing_vector_sync_event_id",
            {item["reason"] for item in report["approval_vector_sync_failure_samples"]},
        )

    def test_flat_storage_correlation_gate_ignores_other_tenant_legacy_and_orphan_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            _prepare_indexed_document(settings)
            repository = JsonRepository(settings)
            other_approval = json.loads(json.dumps(repository.list_approval_journal_records()[0]))
            other_approval.update(
                approval_record_id="approval-record-tenant-b",
                approval_id="approval-tenant-b",
                document_id="doc-tenant-b",
                tenant_id="tenant-b",
                approved_by="reviewer-b",
            )
            other_approval.pop("vector_sync_event_id")
            repository.append_approval_record(other_approval)
            repository.append_maintenance_event(
                {
                    "event_id": "orphan-tenant-b",
                    "event_type": "approval_vector_sync_outcome",
                    "tenant_id": "tenant-b",
                    "outcome": "completed",
                    "vector_sync": {"status": "indexed"},
                }
            )

            report = build_rag_security_evidence(data_dir=settings.data_dir, tenant_id="tenant-a")

        self.assertTrue(report["passed"], report)
        self.assertEqual(1, report["approval_record_count"])
        self.assertEqual(1, report["approval_vector_sync_outcome_count"])
        self.assertEqual(0, report["approval_vector_sync_legacy_approval_count"])
        self.assertEqual(0, report["approval_vector_sync_failure_count"])

    def test_correlation_gate_rejects_missing_identity_and_shared_event_reference(self) -> None:
        approval, event = _sync_correlation_pair()
        approval["approval_record_id"] = ""
        approval["approval_id"] = ""
        event["approval_record_id"] = ""
        event["approval_id"] = ""

        missing_identity = _approval_vector_sync_failures([approval], [event])
        shared_reference = _approval_vector_sync_failures(
            [_sync_correlation_pair()[0], _sync_correlation_pair()[0]],
            [_sync_correlation_pair()[1]],
        )

        invalid = next(item for item in missing_identity if item["reason"] == "approval_sync_contract_invalid")
        self.assertEqual(["approval_id", "approval_record_id"], invalid["missing_identity_fields"])
        self.assertIn(
            "duplicate_approval_sync_event_reference",
            {item["reason"] for item in shared_reference},
        )

    def test_correlation_gate_rejects_duplicate_orphan_and_field_mismatch(self) -> None:
        approval, event = _sync_correlation_pair()
        duplicate_failures = _approval_vector_sync_failures([approval], [event, dict(event)])
        orphan = dict(event)
        orphan.update(event_id="sync-orphan", approval_record_id="approval-record-orphan")
        orphan_failures = _approval_vector_sync_failures([approval], [event, orphan])
        mismatched = dict(event)
        mismatched.update(actor="other-actor", source_action="other.action", sync_action="other-sync")
        mismatch_failures = _approval_vector_sync_failures([approval], [mismatched])

        self.assertIn("duplicate_sync_outcome_event", {item["reason"] for item in duplicate_failures})
        self.assertIn("orphan_sync_outcome_event", {item["reason"] for item in orphan_failures})
        mismatch = next(item for item in mismatch_failures if item["reason"] == "sync_outcome_approval_mismatch")
        self.assertEqual(["actor", "source_action", "sync_action"], mismatch["mismatched_fields"])

    def test_correlation_gate_rejects_event_without_id(self) -> None:
        approval, event = _sync_correlation_pair()
        event.pop("event_id")

        failures = _approval_vector_sync_failures([approval], [event])

        reasons = {item["reason"] for item in failures}
        self.assertIn("sync_outcome_missing_event_id", reasons)
        self.assertIn("missing_sync_outcome_event", reasons)

    def test_correlation_gate_rejects_invalid_or_malformed_outcome_status(self) -> None:
        approval, event = _sync_correlation_pair()
        invalid_pair = dict(event)
        invalid_pair.update(outcome="completed", vector_sync={"status": "failed"})
        malformed = dict(event)
        malformed["vector_sync"] = "indexed"

        invalid_failures = _approval_vector_sync_failures([approval], [invalid_pair])
        malformed_failures = _approval_vector_sync_failures([approval], [malformed])

        self.assertIn("invalid_sync_outcome_status", {item["reason"] for item in invalid_failures})
        self.assertIn("invalid_sync_outcome_status", {item["reason"] for item in malformed_failures})

    def test_correlation_gate_rejects_cross_tenant_pair_inside_selected_runtime(self) -> None:
        approval, event = _sync_correlation_pair()
        approval["tenant_id"] = "tenant-b"
        event["tenant_id"] = "tenant-b"

        failures = _approval_vector_sync_failures([approval], [event], tenant_id="tenant-a")

        mismatch = next(item for item in failures if item["reason"] == "approval_sync_tenant_scope_mismatch")
        self.assertEqual("tenant-a", mismatch["expected_tenant_id"])
        self.assertEqual("tenant-b", mismatch["approval_tenant_id"])

    def test_correlation_gate_accepts_consistent_failure_outcome_as_auditable(self) -> None:
        approval, event = _sync_correlation_pair()
        event.update(
            outcome="failure",
            vector_sync={"status": "failed", "reindex_required": True},
        )

        failures = _approval_vector_sync_failures([approval], [event])

        self.assertEqual([], failures)

    def test_report_detects_empty_vector_after_rejection_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            auth = _prepare_indexed_document(settings)
            with patch.object(routes_documents, "get_settings", return_value=settings):
                routes_documents.reject_review_chunks(
                    "doc_evidence",
                    routes_documents.RejectRequest(chunk_ids=["chunk-1"], reason="revoked"),
                    auth,
                )

            report = build_rag_security_evidence(data_dir=settings.data_dir, tenant_id="tenant-a")

        self.assertFalse(report["passed"])
        self.assertEqual(report["stale_vector_record_count"], 0)
        self.assertEqual(report["vector_record_count"], 0)
        self.assertEqual(report["vector_store_failure_count"], 1)

    def test_report_detects_tampered_vector_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            _prepare_indexed_document(settings)
            vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            rows = [json.loads(line) for line in vector_path.read_text(encoding="utf-8").splitlines()]
            rows[0]["text"] = "tampered evidence text"
            vector_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

            report = build_rag_security_evidence(data_dir=settings.data_dir, tenant_id="tenant-a")

        self.assertFalse(report["passed"])
        self.assertEqual(report["stale_vector_record_count"], 1)
        self.assertEqual(report["stale_vector_record_samples"][0]["reason"], "tampered_stored_vector")

    def test_report_fails_when_indexed_vector_store_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            _prepare_indexed_document(settings)
            vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            vector_path.write_text("", encoding="utf-8")

            report = build_rag_security_evidence(data_dir=settings.data_dir, tenant_id="tenant-a")

        self.assertFalse(report["passed"])
        self.assertEqual(report["vector_record_count"], 0)
        self.assertEqual(report["vector_store_failure_count"], 1)
        self.assertEqual(
            report["vector_store_failure_samples"][0]["reason"],
            "indexed_document_missing_vector_records",
        )

    def test_report_detects_tampered_vector_embedding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            _prepare_indexed_document(settings)
            vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            rows = [json.loads(line) for line in vector_path.read_text(encoding="utf-8").splitlines()]
            rows[0]["embedding"][0] = float(rows[0]["embedding"][0]) + 0.25
            vector_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

            report = build_rag_security_evidence(data_dir=settings.data_dir, tenant_id="tenant-a")

        self.assertFalse(report["passed"])
        self.assertEqual(report["stale_vector_record_count"], 1)
        self.assertEqual(report["stale_vector_record_samples"][0]["reason"], "embedding_hash_mismatch")

    def test_report_requires_append_only_approval_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            _prepare_indexed_document(settings)
            journal_path = settings.data_dir / "repository" / "journals" / "approvals.jsonl"
            journal_path.unlink()

            report = build_rag_security_evidence(data_dir=settings.data_dir, tenant_id="tenant-a")

        self.assertFalse(report["passed"])
        self.assertEqual(report["approval_record_source"], "append_only_journal")
        self.assertEqual(report["approval_journal_record_count"], 0)
        self.assertEqual(report["approval_chain_failure_count"], 1)
        self.assertEqual(
            report["approval_chain_failure_samples"][0]["reason"],
            "missing_matching_approval_journal_record",
        )

    def test_report_stays_current_after_security_scope_change_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            auth = _prepare_indexed_document(settings)
            with patch.object(routes_documents, "get_settings", return_value=settings):
                chunks = JsonRepository(settings).get_chunks("doc_evidence")
                evidence = _write_approval_evidence(settings, chunks=chunks)
                routes_documents.approve_review_chunks(
                    "doc_evidence",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["chunk-1"],
                        approval_id="approval-evidence",
                        security_level="confidential",
                        **evidence,
                    ),
                    auth,
                )

            report = build_rag_security_evidence(data_dir=settings.data_dir, tenant_id="tenant-a")

        self.assertTrue(report["passed"])
        self.assertEqual(report["stale_vector_record_count"], 0)
        self.assertEqual(report["vector_record_count"], 1)

    def test_report_reads_tenant_isolated_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base_settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp), tenant_storage_isolation=True)
            tenant_settings = settings_for_tenant(base_settings, "tenant-a")
            _prepare_indexed_document(tenant_settings, route_settings=base_settings)

            report = build_rag_security_evidence(
                data_dir=base_settings.data_dir,
                tenant_id="tenant-a",
                tenant_storage_isolation=True,
            )

        self.assertTrue(report["passed"])
        self.assertTrue(report["tenant_storage_isolation"])
        self.assertIn("tenants", report["effective_data_dir"])
        self.assertEqual(report["document_count"], 1)
        self.assertEqual(report["vector_record_count"], 1)

    def test_report_fails_when_vector_path_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))

            report = build_rag_security_evidence(data_dir=settings.data_dir, tenant_id="tenant-a")

        self.assertFalse(report["passed"])
        self.assertFalse(report["vector_path_configured"])

    def test_report_fails_when_configured_local_llm_runtime_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            _prepare_indexed_document(settings)

            report = build_rag_security_evidence(
                data_dir=settings.data_dir,
                tenant_id="tenant-a",
                rag_llm_backend="ollama",
                rag_llm_endpoint="http://127.0.0.1:9",
                rag_llm_model="local-llama",
                rag_llm_timeout_seconds=1,
            )

        self.assertFalse(report["passed"])
        self.assertEqual(report["local_llm_runtime_failure_count"], 1)
        self.assertTrue(report["local_llm_runtime_probe"]["checked"])


def _prepare_indexed_document(settings: Settings, *, route_settings: Settings | None = None) -> AuthContext:
    repository = JsonRepository(settings)
    repository.upsert_document(
        Document(
            document_id="doc_evidence",
            filename="evidence.pdf",
            document_name="Evidence",
            file_type="pdf",
            file_hash="hash",
            tenant_id="tenant-a",
            status="completed",
        )
    )
    chunk = Chunk(
        chunk_id="chunk-1",
        document_id="doc_evidence",
        chunk_type="article",
        text="approved evidence",
        retrieval_text="approved evidence",
        security_level="internal",
    )
    repository.save_processing_result(
        "doc_evidence",
        [],
        [chunk],
        [],
    )
    evidence = _write_approval_evidence(settings, chunks=[chunk])
    auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
    with patch.object(routes_documents, "get_settings", return_value=route_settings or settings), patch.object(
        routes_rag, "get_settings", return_value=route_settings or settings
    ):
        routes_documents.approve_review_chunks(
            "doc_evidence",
            routes_documents.ApprovalRequest(
                chunk_ids=["chunk-1"],
                approval_id="approval-evidence",
                **evidence,
            ),
            auth,
        )
        routes_documents.index_document(
            "doc_evidence",
            routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
            auth,
        )
        routes_rag.rag_search(routes_rag.RagSearchRequest(query="approved", security_levels=["internal"]), auth)
    return auth


def _write_approval_evidence(settings: Settings, *, chunks: list[Chunk]) -> dict[str, str]:
    reports = settings.artifact_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    document_id = "doc_evidence"
    worklist_path = reports / "approval_worklist_current.json"
    batch_manifest_path = reports / "approval_review_batches_current.json"
    worklist = {
        "report_type": "approval_worklist",
        "generated_at": "2026-07-10T00:00:00+00:00",
        "data_dir": str(settings.data_dir),
        "effective_data_dir": str(settings.data_dir),
        "tenant_id": "tenant-a",
        "tenant_storage_isolation": False,
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
    batch_fingerprint = routes_documents._review_batch_chunk_fingerprint(batch_chunks, review_type)
    batch_id = f"approval-{worklist_sha256[:12]}-001-low-risk-batch-001-{batch_fingerprint[:12]}"
    manifest = {
        "report_type": "approval_review_batch_manifest",
        "generated_at": "2026-07-10T00:00:01+00:00",
        "data_dir": str(settings.data_dir),
        "effective_data_dir": str(settings.data_dir),
        "tenant_id": "tenant-a",
        "tenant_storage_isolation": False,
        "worklist_report": {
            "path": str(worklist_path),
            "approval_request_path": "reports/approval_worklist_current.json",
            "sha256": worklist_sha256,
            "effective_data_dir": str(settings.data_dir),
            "tenant_id": "tenant-a",
            "tenant_storage_isolation": False,
            "document_count": 1,
            "total_chunks": len(chunks),
        },
        "batch_count": 1,
        "approval_chunk_count": len(chunks),
        "batches": [
            {
                "batch_rank": 1,
                "review_batch_id": batch_id,
                "review_batch_chunk_fingerprint": batch_fingerprint,
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
    batch_manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "worklist_report_path": "reports/approval_worklist_current.json",
        "worklist_report_sha256": worklist_sha256,
        "review_batch_manifest_path": "reports/approval_review_batches_current.json",
        "review_batch_manifest_sha256": _sha256_file(batch_manifest_path),
        "review_batch_id": batch_id,
        "review_batch_chunk_fingerprint": batch_fingerprint,
        "review_strategy": "human_bulk_review",
    }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sync_correlation_pair() -> tuple[dict[str, object], dict[str, object]]:
    approval = {
        "approval_record_id": "approval-record-1",
        "approval_id": "approval-1",
        "document_id": "doc-1",
        "tenant_id": "tenant-a",
        "approved_by": "reviewer-a",
        "chunk_ids": ["chunk-1"],
        "approved_content_hashes": {"chunk-1": "a" * 64},
        "vector_sync_event_id": "sync-event-1",
    }
    event = {
        "event_id": "sync-event-1",
        "event_type": "approval_vector_sync_outcome",
        "source_action": "document.review.approve",
        "sync_action": "review_vector_sync",
        "approval_record_id": "approval-record-1",
        "approval_id": "approval-1",
        "document_id": "doc-1",
        "tenant_id": "tenant-a",
        "actor": "reviewer-a",
        "chunk_ids": ["chunk-1"],
        "approved_content_hashes": {"chunk-1": "a" * 64},
        "approval_persisted": True,
        "outcome": "completed",
        "vector_sync": {"status": "indexed"},
    }
    return approval, event


if __name__ == "__main__":
    unittest.main()
