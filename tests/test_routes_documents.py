from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.api import routes_documents, routes_rag
from app.core.api_audit import api_audit_path
from app.core.config import Settings
from app.core.security import AuthContext
from app.parsers.base import ParserError
from app.schemas.chunk import Chunk, ChunkOptions
from app.schemas.document import Document, ProcessingJob
from app.storage.repository import JsonRepository


class RoutesDocumentsTests(unittest.TestCase):
    def test_upload_document_uses_streaming_service_path(self) -> None:
        captured: dict[str, object] = {}

        class FakeUploadFile:
            filename = "large.pdf"

            def __init__(self) -> None:
                self.file = io.BytesIO(b"%PDF-1.4 streamed")

            async def seek(self, offset: int) -> None:
                self.file.seek(offset)

            async def read(self) -> bytes:
                raise AssertionError("route should not read the whole upload into memory")

        class FakeDocumentService:
            def __init__(self, settings=None, repository=None):
                pass

            def upload_stream(self, filename, source, **kwargs):
                captured["filename"] = filename
                captured["content"] = source.read()
                captured["kwargs"] = kwargs
                return Document(
                    document_id="doc_stream",
                    filename=filename,
                    document_name=kwargs.get("document_name") or "large",
                    file_type="pdf",
                    file_hash="hash",
                    profile_id=kwargs.get("profile_id"),
                    tenant_id=kwargs.get("tenant_id"),
                    status="uploaded",
                )

        with patch.object(routes_documents, "DocumentService", FakeDocumentService), patch.object(
            routes_documents, "_repository", return_value=object()
        ), tempfile.TemporaryDirectory() as tmp, patch.object(
            routes_documents, "get_settings", return_value=Settings(data_dir=Path(tmp))
        ):
            response = asyncio.run(
                routes_documents.upload_document(
                    FakeUploadFile(),
                    document_name="Large Regulation",
                    profile_id="default-public-institution",
                    auth_context=_auth_context(),
                )
            )

        self.assertEqual(response["document_id"], "doc_stream")
        self.assertEqual(captured["filename"], "large.pdf")
        self.assertEqual(captured["content"], b"%PDF-1.4 streamed")
        self.assertEqual(captured["kwargs"]["profile_id"], "default-public-institution")
        self.assertEqual(captured["kwargs"]["tenant_id"], "tenant-a")
        self.assertEqual(response["tenant_id"], "tenant-a")

    def test_upload_document_applies_institution_profile_registry_defaults(self) -> None:
        captured: dict[str, object] = {}

        class FakeUploadFile:
            filename = "public_portal.pdf"

            def __init__(self) -> None:
                self.file = io.BytesIO(b"%PDF-1.4")

            async def seek(self, offset: int) -> None:
                self.file.seek(offset)

        class FakeDocumentService:
            def __init__(self, settings=None, repository=None):
                captured["max_upload_mb"] = settings.max_upload_mb

            def upload_stream(self, filename, source, **kwargs):
                captured["kwargs"] = kwargs
                return Document(
                    document_id="doc_profile",
                    filename=filename,
                    document_name=kwargs.get("document_name") or "public_portal",
                    file_type="pdf",
                    file_hash="hash",
                    institution_name=kwargs.get("institution_name"),
                    apba_id=kwargs.get("apba_id"),
                    source_system=kwargs.get("source_system"),
                    source_url=kwargs.get("source_url"),
                    source_record_id=kwargs.get("source_record_id"),
                    profile_id=kwargs.get("profile_id"),
                    tenant_id=kwargs.get("tenant_id"),
                    status="uploaded",
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "institution_profiles.json"
            registry.write_text(
                json.dumps(
                    {
                        "profiles": {
                            "public_portal-etc-law": {
                                "institution_name": "PUBLIC_PORTAL Disclosure",
                                "apba_id": "C9999",
                                "source_system": "PUBLIC_PORTAL",
                                "source_url": "https://example.org/regulations/etc/etcLawList.do",
                                "required_row_fields": ["source_record_id", "profile_id"],
                                "max_upload_mb": 777,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            settings = Settings(
                data_dir=root / "data",
                institution_profiles_path=str(registry),
                institution_profiles_strict=True,
            )
            with patch.object(routes_documents, "DocumentService", FakeDocumentService), patch.object(
                routes_documents, "get_settings", return_value=settings
            ):
                response = asyncio.run(
                    routes_documents.upload_document(
                        FakeUploadFile(),
                        profile_id="public_portal-etc-law",
                        source_record_id="board-1",
                        auth_context=_auth_context(),
                    )
                )

        self.assertEqual(response["institution_name"], "PUBLIC_PORTAL Disclosure")
        self.assertEqual(response["apba_id"], "C9999")
        self.assertEqual(response["source_system"], "PUBLIC_PORTAL")
        self.assertEqual(response["source_url"], "https://example.org/regulations/etc/etcLawList.do")
        self.assertEqual(captured["kwargs"]["apba_id"], "C9999")
        self.assertEqual(captured["kwargs"]["source_record_id"], "board-1")
        self.assertEqual(captured["kwargs"]["profile_id"], "public_portal-etc-law")
        self.assertEqual(captured["kwargs"]["tenant_id"], "tenant-a")
        self.assertEqual(captured["max_upload_mb"], 777)

    def test_upload_document_strict_registry_rejects_missing_required_fields(self) -> None:
        class FakeUploadFile:
            filename = "public_portal.pdf"
            file = io.BytesIO(b"%PDF-1.4")

            async def seek(self, offset: int) -> None:
                self.file.seek(offset)

        class ShouldNotUpload:
            def __init__(self, settings=None, repository=None):
                pass

            def upload_stream(self, *args, **kwargs):
                raise AssertionError("upload_stream should not be called when profile requirements fail")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "institution_profiles.json"
            registry.write_text(
                json.dumps({"profiles": {"public_portal-etc-law": {"required_row_fields": ["source_record_id"]}}}),
                encoding="utf-8",
            )
            settings = Settings(
                data_dir=root / "data",
                institution_profiles_path=str(registry),
                institution_profiles_strict=True,
            )
            with patch.object(routes_documents, "DocumentService", ShouldNotUpload), patch.object(
                routes_documents, "get_settings", return_value=settings
            ):
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(
                        routes_documents.upload_document(
                            FakeUploadFile(),
                            profile_id="public_portal-etc-law",
                            auth_context=_auth_context(),
                        )
                    )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("requires fields: source_record_id", raised.exception.detail)

    def test_process_document_omitted_request_passes_none_options_to_service(self) -> None:
        captured: list[ChunkOptions | None] = []

        class FakeProcessingService:
            def __init__(self, settings=None, repository=None):
                pass

            def process(self, document_id: str, options: ChunkOptions | None = None) -> ProcessingJob:
                captured.append(options)
                return ProcessingJob(job_id="job_test", document_id=document_id, status="completed")

        with patch.object(routes_documents, "ProcessingService", FakeProcessingService), patch.object(
            routes_documents, "_repository", return_value=_repository_with_document()
        ), tempfile.TemporaryDirectory() as tmp, patch.object(
            routes_documents, "get_settings", return_value=Settings(data_dir=Path(tmp))
        ):
            response = routes_documents.process_document("doc_test", None, _auth_context())

        self.assertIsNone(captured[0])
        self.assertEqual(response["job_id"], "job_test")

    def test_process_document_empty_request_passes_none_options_to_service(self) -> None:
        captured: list[ChunkOptions | None] = []

        class FakeProcessingService:
            def __init__(self, settings=None, repository=None):
                pass

            def process(self, document_id: str, options: ChunkOptions | None = None) -> ProcessingJob:
                captured.append(options)
                return ProcessingJob(job_id="job_test", document_id=document_id, status="completed")

        with patch.object(routes_documents, "ProcessingService", FakeProcessingService), patch.object(
            routes_documents, "_repository", return_value=_repository_with_document()
        ), tempfile.TemporaryDirectory() as tmp, patch.object(
            routes_documents, "get_settings", return_value=Settings(data_dir=Path(tmp))
        ):
            routes_documents.process_document("doc_test", routes_documents.ProcessRequest(), _auth_context())

        self.assertIsNone(captured[0])

    def test_process_document_explicit_options_are_forwarded(self) -> None:
        captured: list[ChunkOptions | None] = []
        options = ChunkOptions(max_chunk_chars=80, overlap_chars=10)

        class FakeProcessingService:
            def __init__(self, settings=None, repository=None):
                pass

            def process(self, document_id: str, options: ChunkOptions | None = None) -> ProcessingJob:
                captured.append(options)
                return ProcessingJob(job_id="job_test", document_id=document_id, status="completed")

        with patch.object(routes_documents, "ProcessingService", FakeProcessingService), patch.object(
            routes_documents, "_repository", return_value=_repository_with_document()
        ), tempfile.TemporaryDirectory() as tmp, patch.object(
            routes_documents, "get_settings", return_value=Settings(data_dir=Path(tmp))
        ):
            routes_documents.process_document("doc_test", routes_documents.ProcessRequest(parser_options=options), _auth_context())

        self.assertEqual(captured[0].max_chunk_chars, 80)
        self.assertEqual(captured[0].overlap_chars, 10)

    def test_process_document_parser_error_returns_client_error(self) -> None:
        class FakeProcessingService:
            def __init__(self, settings=None, repository=None):
                pass

            def process(self, document_id: str, options: ChunkOptions | None = None) -> ProcessingJob:
                raise ParserError("Invalid parser input")

        with patch.object(routes_documents, "ProcessingService", FakeProcessingService), patch.object(
            routes_documents, "_repository", return_value=_repository_with_document()
        ), tempfile.TemporaryDirectory() as tmp, patch.object(
            routes_documents, "get_settings", return_value=Settings(data_dir=Path(tmp))
        ):
            with self.assertRaises(HTTPException) as raised:
                routes_documents.process_document("doc_test", None, _auth_context())

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "Invalid parser input")

    def test_process_document_redacts_expected_path_and_hides_unexpected_error(self) -> None:
        class PathParserFailure:
            def __init__(self, settings=None, repository=None):
                pass

            def process(self, document_id: str, options: ChunkOptions | None = None) -> ProcessingJob:
                raise ParserError(r"Unable to parse C:\private\rules.hwp")

        class UnexpectedFailure:
            def __init__(self, settings=None, repository=None):
                pass

            def process(self, document_id: str, options: ChunkOptions | None = None) -> ProcessingJob:
                raise RuntimeError(r"database secret at C:\private\repository.json")

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            with patch.object(
                routes_documents, "ProcessingService", PathParserFailure
            ), patch.object(
                routes_documents, "_repository", return_value=_repository_with_document()
            ), patch.object(routes_documents, "get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as parser_raised:
                    routes_documents.process_document("doc_test", None, _auth_context())
            with patch.object(
                routes_documents, "ProcessingService", UnexpectedFailure
            ), patch.object(
                routes_documents, "_repository", return_value=_repository_with_document()
            ), patch.object(routes_documents, "get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as unexpected_raised:
                    routes_documents.process_document("doc_test", None, _auth_context())

            audit_rows = [
                json.loads(line)
                for line in api_audit_path(settings).read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(400, parser_raised.exception.status_code)
        self.assertIn("[local-path-redacted]", parser_raised.exception.detail)
        self.assertNotIn(r"C:\private", parser_raised.exception.detail)
        self.assertEqual(500, unexpected_raised.exception.status_code)
        self.assertEqual(routes_documents._PROCESSING_FAILURE_DETAIL, unexpected_raised.exception.detail)
        self.assertNotIn("database secret", unexpected_raised.exception.detail)
        self.assertIn("[local-path-redacted]", audit_rows[-1]["detail"])
        self.assertNotIn(r"C:\private", audit_rows[-1]["detail"])

    def test_process_document_rejects_cross_tenant_document_before_service_call(self) -> None:
        class ShouldNotProcess:
            def __init__(self, repository=None):
                pass

            def process(self, *args, **kwargs):
                raise AssertionError("cross-tenant processing should not call service")

        with patch.object(routes_documents, "ProcessingService", ShouldNotProcess), patch.object(
            routes_documents, "_repository", return_value=_repository_with_document(tenant_id="tenant-b")
        ), tempfile.TemporaryDirectory() as tmp, patch.object(
            routes_documents, "get_settings", return_value=Settings(data_dir=Path(tmp))
        ):
            with self.assertRaises(HTTPException) as raised:
                routes_documents.process_document("doc_test", None, _auth_context())

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(raised.exception.detail, "Document not found for current tenant: document_id=doc_test")

    def test_approve_review_chunks_updates_chunks_exports_and_approval_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_review",
                    filename="review.pdf",
                    document_name="Review",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_review",
                [],
                [
                    Chunk(
                        chunk_id="chunk-1",
                        document_id="doc_review",
                        chunk_type="article",
                        text="제1조 목적",
                        retrieval_text="제1조 목적",
                    )
                ],
                [],
            )
            evidence = _write_approval_evidence(
                root,
                settings=settings,
                document_id="doc_review",
                chunks=repository.get_chunks("doc_review"),
            )
            request_evidence = dict(evidence)
            request_evidence.pop("review_batch_manifest_sha256")
            with patch.object(routes_documents, "get_settings", return_value=settings):
                response = routes_documents.approve_review_chunks(
                    "doc_review",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["chunk-1"],
                        approval_id="approval-test",
                        security_level="internal",
                        **request_evidence,
                    ),
                    _auth_context(),
                )
                routes_documents.index_document("doc_review", routes_documents.IndexRequest(), _auth_context())

            updated = JsonRepository(settings).get_chunks("doc_review")[0]
            export_row = json.loads((settings.exports_dir / "doc_review.jsonl").read_text(encoding="utf-8").strip())
            approvals = JsonRepository(settings).list_approval_records("doc_review")
            approval_snapshot_exists = (settings.data_dir / approvals[0]["snapshot"]).is_file()
            vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            vector_row = json.loads(vector_path.read_text(encoding="utf-8").strip())
            audit_rows = [
                json.loads(line)
                for line in api_audit_path(settings).read_text(encoding="utf-8").splitlines()
            ]
            approval_audit = [
                row for row in audit_rows if row.get("action") == "document.review.approve"
            ][0]

        self.assertEqual(response["approval_id"], "approval-test")
        self.assertEqual(updated.approval_status, "approved")
        self.assertEqual(updated.approval_id, "approval-test")
        self.assertEqual(updated.security_level, "internal")
        self.assertEqual(updated.metadata["approval_worklist_report_path"], "reports/approval_worklist_current.json")
        self.assertEqual(updated.metadata["approval_worklist_report_sha256"], evidence["worklist_report_sha256"])
        self.assertEqual(updated.metadata["approval_review_batch_manifest_path"], "reports/approval_review_batches_current.json")
        self.assertEqual(
            updated.metadata["approval_review_batch_manifest_sha256"],
            evidence["review_batch_manifest_sha256"],
        )
        self.assertEqual(updated.metadata["approval_review_batch_id"], evidence["review_batch_id"])
        self.assertEqual(
            updated.metadata["approval_review_batch_chunk_fingerprint"],
            evidence["review_batch_chunk_fingerprint"],
        )
        self.assertEqual(updated.metadata["approval_review_strategy"], "human_bulk_review")
        self.assertEqual(export_row["approval_status"], "approved")
        self.assertEqual(export_row["approval_id"], "approval-test")
        self.assertEqual(export_row["security_level"], "internal")
        self.assertEqual(approvals[0]["approval_id"], "approval-test")
        self.assertEqual(
            approvals[0]["worklist_evidence"],
            {
                "worklist_report_path": "reports/approval_worklist_current.json",
                "worklist_report_sha256": evidence["worklist_report_sha256"],
                "review_batch_manifest_path": "reports/approval_review_batches_current.json",
                "review_batch_manifest_sha256": evidence["review_batch_manifest_sha256"],
                "review_batch_id": evidence["review_batch_id"],
                "review_batch_chunk_fingerprint": evidence["review_batch_chunk_fingerprint"],
                "review_strategy": "human_bulk_review",
            },
        )
        self.assertTrue(approvals[0]["approval_record_id"].startswith("approval_record_"))
        self.assertTrue(approval_snapshot_exists)
        self.assertIn("before_content_hashes", approvals[0])
        self.assertEqual(approvals[0]["approved_chunks"][0]["chunk_id"], "chunk-1")
        self.assertEqual(
            approvals[0]["approved_chunks"][0]["worklist_evidence"]["review_batch_id"],
            evidence["review_batch_id"],
        )
        self.assertEqual(
            approvals[0]["approved_chunks"][0]["worklist_evidence"]["review_batch_chunk_fingerprint"],
            evidence["review_batch_chunk_fingerprint"],
        )
        self.assertEqual(approvals[0]["approved_chunks"][0]["security_level"], "internal")
        self.assertEqual(approvals[0]["approved_chunks"][0]["department_acl"], [])
        self.assertEqual(approvals[0]["approved_chunks"][0]["previous_approval_status"], "draft")
        self.assertFalse(approvals[0]["review_flags_acknowledged"])
        self.assertEqual(0, approvals[0]["review_attention_chunk_count"])
        self.assertEqual([], approvals[0]["approved_chunks"][0]["review_attention_reasons"])
        self.assertEqual(vector_row["metadata"]["approval_worklist_report_sha256"], evidence["worklist_report_sha256"])
        self.assertEqual(
            vector_row["metadata"]["approval_review_batch_manifest_sha256"],
            evidence["review_batch_manifest_sha256"],
        )
        self.assertEqual(vector_row["metadata"]["approval_review_batch_id"], evidence["review_batch_id"])
        self.assertEqual(
            vector_row["metadata"]["approval_review_batch_chunk_fingerprint"],
            evidence["review_batch_chunk_fingerprint"],
        )
        self.assertIn("worklist_report_path=reports/approval_worklist_current.json", approval_audit["detail"])
        self.assertIn(f"worklist_report_sha256={evidence['worklist_report_sha256']}", approval_audit["detail"])
        self.assertIn("review_batch_manifest_path=reports/approval_review_batches_current.json", approval_audit["detail"])
        self.assertIn(
            f"review_batch_manifest_sha256={evidence['review_batch_manifest_sha256']}",
            approval_audit["detail"],
        )
        self.assertIn(f"review_batch_id={evidence['review_batch_id']}", approval_audit["detail"])
        self.assertIn(
            f"review_batch_chunk_fingerprint={evidence['review_batch_chunk_fingerprint']}",
            approval_audit["detail"],
        )
        self.assertIn("review_strategy=human_bulk_review", approval_audit["detail"])
        self.assertIn(f"vector_sync_event_id={response['vector_sync_event_id']}", approval_audit["detail"])
        self.assertIn("vector_sync_status=skipped", approval_audit["detail"])
        self.assertNotIn("\\", approval_audit["detail"])

    def test_approve_review_chunks_appends_approval_journal_before_vector_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_review",
                    filename="review.pdf",
                    document_name="Review",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_review",
                [],
                [Chunk(chunk_id="chunk-1", document_id="doc_review", chunk_type="article", text="body")],
                [],
            )
            evidence = _write_approval_evidence(
                root,
                settings=settings,
                document_id="doc_review",
                chunks=repository.get_chunks("doc_review"),
            )
            observed: dict[str, object] = {}

            def _assert_journal_exists_before_sync(**kwargs):
                sync_repository = kwargs["repository"]
                sync_document_id = kwargs["document_id"]
                records = sync_repository.list_approval_journal_records(sync_document_id)
                observed["record_count"] = len(records)
                observed["chunk_ids"] = records[0]["chunk_ids"] if records else []
                observed["journal_vector_sync"] = records[0]["vector_sync"] if records else {}
                observed["journal_vector_sync_event_id"] = records[0]["vector_sync_event_id"] if records else ""
                observed["outcome_event_count_before_sync"] = len(
                    sync_repository.list_maintenance_events("approval_vector_sync_outcome")
                )
                return {"status": "skipped", "reason": "test_vector_sync_probe"}

            with patch.object(routes_documents, "get_settings", return_value=settings), patch.object(
                routes_documents,
                "_sync_vector_index_after_review_change",
                side_effect=_assert_journal_exists_before_sync,
            ):
                response = routes_documents.approve_review_chunks(
                    "doc_review",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["chunk-1"],
                        approval_id="approval-test",
                        security_level="internal",
                        **evidence,
                    ),
                    _auth_context(),
                )
            approval_records = repository.list_approval_records("doc_review")
            sync_events = repository.list_maintenance_events("approval_vector_sync_outcome")

        self.assertEqual(1, observed["record_count"])
        self.assertEqual(["chunk-1"], observed["chunk_ids"])
        self.assertEqual(
            {"status": "pending", "reason": "approval_journal_append_before_vector_sync"},
            observed["journal_vector_sync"],
        )
        self.assertEqual({"status": "skipped", "reason": "test_vector_sync_probe"}, response["vector_sync"])
        self.assertEqual(response["vector_sync_event_id"], observed["journal_vector_sync_event_id"])
        self.assertEqual(0, observed["outcome_event_count_before_sync"])
        self.assertEqual(1, len(sync_events))
        self.assertEqual(response["vector_sync_event_id"], sync_events[0]["event_id"])
        self.assertEqual("completed", sync_events[0]["outcome"])
        self.assertEqual("approval-test", sync_events[0]["approval_id"])
        self.assertEqual(["chunk-1"], sync_events[0]["chunk_ids"])
        self.assertEqual(approval_records[0]["approval_record_id"], sync_events[0]["approval_record_id"])
        self.assertEqual(approval_records[0]["vector_sync_event_id"], sync_events[0]["event_id"])
        self.assertEqual(approval_records[0]["approved_content_hashes"], sync_events[0]["approved_content_hashes"])
        self.assertEqual(response["vector_sync"], sync_events[0]["vector_sync"])

    def test_approve_review_chunks_persists_failed_sync_and_hides_stale_vector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_sync_failure",
                    filename="sync-failure.pdf",
                    document_name="Sync Failure",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_sync_failure",
                [],
                [
                    Chunk(
                        chunk_id="chunk-1",
                        document_id="doc_sync_failure",
                        chunk_type="article",
                        text="approved evidence alpha",
                        retrieval_text="approved evidence alpha",
                    )
                ],
                [],
            )
            first_evidence = _write_approval_evidence(
                root,
                settings=settings,
                document_id="doc_sync_failure",
                chunks=repository.get_chunks("doc_sync_failure"),
            )
            auth = _auth_context()
            request = routes_rag.RagSearchRequest(
                query="approved evidence alpha",
                document_id="doc_sync_failure",
                security_levels=["internal"],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                routes_documents.approve_review_chunks(
                    "doc_sync_failure",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["chunk-1"],
                        approval_id="approval-before-failure",
                        security_level="internal",
                        **first_evidence,
                    ),
                    auth,
                )
                routes_documents.index_document(
                    "doc_sync_failure",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    auth,
                )

            before_results, _ = routes_rag.search_rag_records(request, auth, settings)
            vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            vector_before_failure = vector_path.read_text(encoding="utf-8")
            second_evidence = _write_approval_evidence(
                root,
                settings=settings,
                document_id="doc_sync_failure",
                chunks=JsonRepository(settings).get_chunks("doc_sync_failure"),
            )

            with patch.object(routes_documents, "get_settings", return_value=settings), patch.object(
                routes_documents,
                "_sync_vector_index_after_review_change",
                side_effect=RuntimeError(r"simulated vector outage at C:\private\vector.jsonl"),
            ):
                with self.assertRaises(HTTPException) as raised:
                    routes_documents.approve_review_chunks(
                        "doc_sync_failure",
                        routes_documents.ApprovalRequest(
                            chunk_ids=["chunk-1"],
                            approval_id="approval-after-failure",
                            security_level="internal",
                            **second_evidence,
                        ),
                        auth,
                    )

            after_results, _ = routes_rag.search_rag_records(request, auth, settings)
            stored_repository = JsonRepository(settings)
            approval_records = stored_repository.list_approval_records("doc_sync_failure")
            indexing_jobs = stored_repository.list_indexing_jobs("doc_sync_failure")
            failure_events = [
                event
                for event in stored_repository.list_maintenance_events("approval_vector_sync_outcome")
                if event.get("outcome") == "failure"
            ]
            audit_records = [
                json.loads(line)
                for line in api_audit_path(settings).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            failure_audit = next(
                record
                for record in reversed(audit_records)
                if record.get("action") == "document.review.approve" and record.get("outcome") == "failure"
            )
            vector_after_failure = vector_path.read_text(encoding="utf-8")

        self.assertEqual(1, len(before_results))
        self.assertEqual([], after_results)
        self.assertEqual(vector_before_failure, vector_after_failure)
        self.assertEqual(500, raised.exception.status_code)
        self.assertTrue(raised.exception.detail["reindex_required"])
        self.assertEqual("approval-after-failure", approval_records[-1]["approval_id"])
        self.assertEqual("pending", approval_records[-1]["vector_sync"]["status"])
        self.assertEqual(1, len(indexing_jobs))
        self.assertEqual("index", indexing_jobs[0]["action"])
        self.assertEqual(1, len(failure_events))
        self.assertEqual("approval-after-failure", failure_events[0]["approval_id"])
        self.assertEqual(approval_records[-1]["vector_sync_event_id"], failure_events[0]["event_id"])
        self.assertEqual("failed", failure_events[0]["vector_sync"]["status"])
        self.assertEqual("RuntimeError", failure_events[0]["vector_sync"]["exception_type"])
        self.assertNotIn(r"C:\private", failure_events[0]["vector_sync"]["detail"])
        self.assertIn("[local-path-redacted]", failure_events[0]["vector_sync"]["detail"])
        self.assertTrue(failure_events[0]["vector_sync"]["reindex_required"])
        self.assertEqual(raised.exception.detail["vector_sync_event_id"], failure_events[0]["event_id"])
        self.assertIn(failure_events[0]["event_id"], failure_audit["detail"])
        self.assertNotIn("approved evidence alpha", json.dumps(failure_events[0], ensure_ascii=False))

    def test_approve_review_chunks_persists_approval_screen_review_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_review_events",
                    filename="review-events.pdf",
                    document_name="Review Events",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_review_events",
                [],
                [
                    Chunk(
                        chunk_id="chunk-1",
                        document_id="doc_review_events",
                        chunk_type="table",
                        text="approved table body",
                        retrieval_text="approved table body",
                        metadata={"table_source": "kordoc", "kordoc_table_promoted": True},
                    )
                ],
                [],
            )
            evidence = _write_approval_evidence(
                root,
                settings=settings,
                document_id="doc_review_events",
                chunks=repository.get_chunks("doc_review_events"),
            )
            review_events = [
                {
                    "event": "ai_review_confirmed",
                    "timestamp": "2026-07-12T00:00:00+00:00",
                    "actor": "tester",
                    "chunk_id": "chunk-1",
                    "ai_reflected": 1,
                    "ai_skipped": 1,
                    "ai_total": 2,
                    "ai_decisions": {"risk-a": "reflect", "risk-b": "skip"},
                    "source_of_truth": {"table_source": "kordoc", "kordoc_table_promoted": True},
                },
                {
                    "event": "human_review_confirmed",
                    "timestamp": "2026-07-12T00:00:00+00:00",
                    "actor": "tester",
                    "chunk_id": "chunk-1",
                    "source_of_truth": {"table_source": "kordoc", "kordoc_table_promoted": True},
                },
                {
                    "event": "approved_without_review",
                    "timestamp": "2026-07-12T00:00:00+00:00",
                    "actor": "tester",
                    "chunk_id": "chunk-1",
                    "override_reason": "offline director approval",
                },
            ]

            with patch.object(routes_documents, "get_settings", return_value=settings):
                response = routes_documents.approve_review_chunks(
                    "doc_review_events",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["chunk-1"],
                        approval_id="approval-events",
                        security_level="internal",
                        review_decision_events=review_events,
                        approval_override_reason="offline director approval",
                        **evidence,
                    ),
                    _auth_context(),
                )
            approvals = JsonRepository(settings).list_approval_records("doc_review_events")
            journal = JsonRepository(settings).list_approval_journal_records("doc_review_events")

        self.assertEqual("approval-events", response["approval_id"])
        self.assertEqual(["ai_review_confirmed", "human_review_confirmed", "approved_without_review"], [
            event["event"] for event in approvals[0]["review_decision_events"]
        ])
        self.assertEqual(1, approvals[0]["review_decision_event_counts"]["ai_review_confirmed"])
        self.assertTrue(approvals[0]["ai_review_confirmed"])
        self.assertTrue(approvals[0]["human_review_confirmed"])
        self.assertEqual("offline director approval", approvals[0]["approval_override_reason"])
        self.assertEqual(["pending_human_review"], approvals[0]["approval_state_transition"]["from_statuses"])
        self.assertEqual(
            ["pending_human_review", "reviewed", "approved"],
            approvals[0]["approval_state_transition"]["required_sequence"],
        )
        self.assertEqual(approvals[0]["review_decision_events"], journal[0]["review_decision_events"])
        self.assertEqual("kordoc", approvals[0]["review_decision_events"][0]["source_of_truth"]["table_source"])

    def test_approve_review_chunks_requires_ack_for_parser_review_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_review_flags",
                    filename="review-flags.pdf",
                    document_name="Review Flags",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_review_flags",
                [],
                [
                    Chunk(
                        chunk_id="chunk-flagged",
                        document_id="doc_review_flags",
                        chunk_type="table",
                        text="table text",
                        retrieval_text="table text",
                        metadata={
                            "table_review_required": True,
                            "table_review_flags": ["row_review_required"],
                        },
                    )
                ],
                [],
            )
            evidence = _write_approval_evidence(
                root,
                settings=settings,
                document_id="doc_review_flags",
                chunks=repository.get_chunks("doc_review_flags"),
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as raised:
                    routes_documents.approve_review_chunks(
                        "doc_review_flags",
                        routes_documents.ApprovalRequest(
                            chunk_ids=["chunk-flagged"],
                            approval_id="approval-review-flags",
                            security_level="internal",
                        ),
                        _auth_context(),
                    )
                response = routes_documents.approve_review_chunks(
                    "doc_review_flags",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["chunk-flagged"],
                        approval_id="approval-review-flags",
                        security_level="internal",
                        review_flags_acknowledged=True,
                        **evidence,
                    ),
                    _auth_context(),
                )
            approvals = JsonRepository(settings).list_approval_records("doc_review_flags")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Review flags must be acknowledged", raised.exception.detail)
        self.assertEqual("approval-review-flags", response["approval_id"])
        self.assertTrue(approvals[0]["review_flags_acknowledged"])
        self.assertEqual(1, approvals[0]["review_attention_chunk_count"])
        self.assertIn("table_review_required", approvals[0]["review_attention_flags"])
        self.assertIn("table_review_flags:row_review_required", approvals[0]["review_attention_flags"])
        self.assertEqual(["chunk-flagged"], [item["chunk_id"] for item in approvals[0]["review_attention_samples"]])
        self.assertIn("table_review_required", approvals[0]["approved_chunks"][0]["review_attention_reasons"])

    def test_approve_review_chunks_allows_override_without_faking_review_flag_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_review_override",
                    filename="review-override.pdf",
                    document_name="Review Override",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_review_override",
                [],
                [
                    Chunk(
                        chunk_id="chunk-flagged",
                        document_id="doc_review_override",
                        chunk_type="table",
                        text="table text",
                        retrieval_text="table text",
                        metadata={
                            "table_review_required": True,
                            "table_review_flags": ["row_review_required"],
                        },
                    )
                ],
                [],
            )
            evidence = _write_approval_evidence(
                root,
                settings=settings,
                document_id="doc_review_override",
                chunks=repository.get_chunks("doc_review_override"),
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                response = routes_documents.approve_review_chunks(
                    "doc_review_override",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["chunk-flagged"],
                        approval_id="approval-override",
                        security_level="internal",
                        review_flags_acknowledged=False,
                        approval_override_reason="offline director approval",
                        **evidence,
                    ),
                    _auth_context(),
                )
            approvals = JsonRepository(settings).list_approval_records("doc_review_override")

        self.assertEqual("approval-override", response["approval_id"])
        self.assertFalse(approvals[0]["review_flags_acknowledged"])
        self.assertEqual("offline director approval", approvals[0]["approval_override_reason"])
        self.assertEqual(["approved_without_review"], [
            event["event"] for event in approvals[0]["review_decision_events"]
        ])
        self.assertEqual(1, approvals[0]["review_attention_chunk_count"])

    def test_approve_review_chunks_requires_ack_for_parser_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_parser_uncertainty",
                    filename="parser-uncertainty.pdf",
                    document_name="Parser Uncertainty",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_parser_uncertainty",
                [],
                [
                    Chunk(
                        chunk_id="chunk-uncertain",
                        document_id="doc_parser_uncertainty",
                        chunk_type="article",
                        text="OCR fallback text",
                        retrieval_text="OCR fallback text",
                        metadata={
                            "parser_uncertainty_source": "pdf",
                            "parser_uncertainty_risk_level": "high",
                            "parser_uncertainty_flags": ["ocr_required", "no_text_extracted"],
                            "parser_uncertainty_recommendation": "run_ocr",
                        },
                    )
                ],
                [],
            )
            evidence = _write_approval_evidence(
                root,
                settings=settings,
                document_id="doc_parser_uncertainty",
                chunks=repository.get_chunks("doc_parser_uncertainty"),
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as raised:
                    routes_documents.approve_review_chunks(
                        "doc_parser_uncertainty",
                        routes_documents.ApprovalRequest(
                            chunk_ids=["chunk-uncertain"],
                            approval_id="approval-parser-uncertainty",
                            security_level="internal",
                        ),
                        _auth_context(),
                    )
                response = routes_documents.approve_review_chunks(
                    "doc_parser_uncertainty",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["chunk-uncertain"],
                        approval_id="approval-parser-uncertainty",
                        security_level="internal",
                        review_flags_acknowledged=True,
                        **evidence,
                    ),
                    _auth_context(),
                )
            approvals = JsonRepository(settings).list_approval_records("doc_parser_uncertainty")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Review flags must be acknowledged", raised.exception.detail)
        self.assertEqual("approval-parser-uncertainty", response["approval_id"])
        self.assertTrue(approvals[0]["review_flags_acknowledged"])
        self.assertIn("parser_uncertainty_risk_level:high", approvals[0]["review_attention_flags"])
        self.assertIn("parser_uncertainty_flags:ocr_required", approvals[0]["review_attention_flags"])
        self.assertIn("parser_uncertainty_recommendation:run_ocr", approvals[0]["review_attention_flags"])
        self.assertIn(
            "parser_uncertainty_risk_level:high",
            approvals[0]["approved_chunks"][0]["review_attention_reasons"],
        )

    def test_approve_review_chunks_rejects_mismatched_worklist_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_fake_worklist",
                    filename="fake-worklist.pdf",
                    document_name="Fake Worklist",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_fake_worklist",
                [],
                [
                    Chunk(
                        chunk_id="chunk-1",
                        document_id="doc_fake_worklist",
                        chunk_type="article",
                        text="approved text",
                        retrieval_text="approved text",
                    )
                ],
                [],
            )
            evidence = _write_approval_evidence(
                root,
                settings=settings,
                document_id="doc_fake_worklist",
                chunks=repository.get_chunks("doc_fake_worklist"),
            )
            tampered_evidence = dict(evidence)
            tampered_evidence["worklist_report_sha256"] = "0" * 64
            tampered_evidence["review_batch_id"] = (
                "approval-000000000000-001-low-risk-batch-001-"
                f"{tampered_evidence['review_batch_chunk_fingerprint'][:12]}"
            )
            with patch.object(routes_documents, "get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as raised:
                    routes_documents.approve_review_chunks(
                        "doc_fake_worklist",
                        routes_documents.ApprovalRequest(
                            chunk_ids=["chunk-1"],
                            security_level="internal",
                            **tampered_evidence,
                        ),
                        _auth_context(),
                    )

        self.assertEqual(400, raised.exception.status_code)
        self.assertIn("SHA-256 mismatch", str(raised.exception.detail))

    def test_approve_review_chunks_rejects_stale_batch_review_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_stale_batch",
                    filename="stale-batch.pdf",
                    document_name="Stale Batch",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_stale_batch",
                [],
                [
                    Chunk(
                        chunk_id="chunk-1",
                        document_id="doc_stale_batch",
                        chunk_type="article",
                        text="original text",
                        retrieval_text="original text",
                    )
                ],
                [],
            )
            evidence = _write_approval_evidence(
                root,
                settings=settings,
                document_id="doc_stale_batch",
                chunks=repository.get_chunks("doc_stale_batch"),
            )
            stale_chunk = repository.get_chunks("doc_stale_batch")[0].model_copy(
                update={"text": "changed text", "retrieval_text": "changed text"}
            )
            repository.save_chunks("doc_stale_batch", [stale_chunk])
            with patch.object(routes_documents, "get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as raised:
                    routes_documents.approve_review_chunks(
                        "doc_stale_batch",
                        routes_documents.ApprovalRequest(
                            chunk_ids=["chunk-1"],
                            security_level="internal",
                            **evidence,
                        ),
                        _auth_context(),
                    )

        self.assertEqual(400, raised.exception.status_code)
        self.assertIn("review_content_hash mismatch", str(raised.exception.detail))

    def test_approve_review_chunks_rejects_unsafe_worklist_evidence_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_review_worklist_path",
                    filename="review.pdf",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_review_worklist_path",
                [],
                [
                    Chunk(
                        chunk_id="chunk-1",
                        document_id="doc_review_worklist_path",
                        chunk_type="article",
                        text="approved text",
                        retrieval_text="approved text",
                    )
                ],
                [],
            )
            with patch.object(routes_documents, "get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as raised:
                    routes_documents.approve_review_chunks(
                        "doc_review_worklist_path",
                        routes_documents.ApprovalRequest(
                            chunk_ids=["chunk-1"],
                            security_level="internal",
                            worklist_report_path="C:" + "\\workspace" + "\\Rag" + "\\reports" + "\\approval_worklist_current.json",
                        ),
                        _auth_context(),
                    )
                with self.assertRaises(HTTPException) as scheme_raised:
                    routes_documents.approve_review_chunks(
                        "doc_review_worklist_path",
                        routes_documents.ApprovalRequest(
                            chunk_ids=["chunk-1"],
                            security_level="internal",
                            worklist_report_path="file://reports/approval_worklist_current.json",
                        ),
                        _auth_context(),
                    )

        self.assertEqual(400, raised.exception.status_code)
        self.assertIn("safe relative artifact path", str(raised.exception.detail))
        self.assertEqual(400, scheme_raised.exception.status_code)
        self.assertIn("safe relative artifact path", str(scheme_raised.exception.detail))

    def test_worklist_sha256_normalization_and_validation(self) -> None:
        self.assertEqual(
            "a" * 64,
            routes_documents._normalize_optional_sha256("A" * 64, field_name="worklist_report_sha256"),
        )
        with self.assertRaises(HTTPException) as raised:
            routes_documents._normalize_optional_sha256("not-a-sha", field_name="worklist_report_sha256")

        self.assertEqual(400, raised.exception.status_code)
        self.assertIn("SHA-256", str(raised.exception.detail))

    def test_worklist_evidence_identifiers_reject_path_shaped_values(self) -> None:
        evidence = routes_documents._approval_worklist_evidence(
            routes_documents.ApprovalRequest(
                chunk_ids=["chunk-1"],
                review_batch_id="approval-237faa10d2f4-001-manual-attention-001",
                review_batch_chunk_fingerprint="A" * 64,
                review_strategy="sampled_low_risk_batch_review",
            )
        )
        self.assertEqual("approval-237faa10d2f4-001-manual-attention-001", evidence["review_batch_id"])
        self.assertEqual("a" * 64, evidence["review_batch_chunk_fingerprint"])
        self.assertEqual("sampled_low_risk_batch_review", evidence["review_strategy"])

        with self.assertRaises(HTTPException) as batch_raised:
            routes_documents._approval_worklist_evidence(
                routes_documents.ApprovalRequest(
                    chunk_ids=["chunk-1"],
                    review_batch_id="C:" + "\\workspace" + "\\Rag" + "\\batch.json",
                )
            )
        with self.assertRaises(HTTPException) as strategy_raised:
            routes_documents._approval_worklist_evidence(
                routes_documents.ApprovalRequest(
                    chunk_ids=["chunk-1"],
                    review_strategy="manual review",
                )
            )
        with self.assertRaises(HTTPException) as fingerprint_raised:
            routes_documents._approval_worklist_evidence(
                routes_documents.ApprovalRequest(
                    chunk_ids=["chunk-1"],
                    review_batch_chunk_fingerprint="not-a-sha",
                )
            )

        self.assertEqual(400, batch_raised.exception.status_code)
        self.assertIn("review_batch_id", str(batch_raised.exception.detail))
        self.assertEqual(400, strategy_raised.exception.status_code)
        self.assertIn("review_strategy", str(strategy_raised.exception.detail))
        self.assertEqual(400, fingerprint_raised.exception.status_code)
        self.assertIn("review_batch_chunk_fingerprint", str(fingerprint_raised.exception.detail))

    def test_approved_content_hash_ignores_worklist_bookkeeping_metadata(self) -> None:
        base_chunk = Chunk(
            chunk_id="chunk-hash",
            document_id="doc-hash",
            chunk_type="article",
            text="approved text",
            retrieval_text="approved text",
            metadata={"article_no": "1"},
            security_level="internal",
        )
        evidence_chunk = base_chunk.model_copy(
            update={
                "metadata": {
                    "article_no": "1",
                    "approval_worklist_report_path": "reports/approval_worklist_current.json",
                    "approval_worklist_report_sha256": "b" * 64,
                    "approval_review_batch_id": "batch-20260709",
                    "approval_review_batch_chunk_fingerprint": "d" * 64,
                    "approval_review_strategy": "human_bulk_review",
                }
            }
        )
        changed_text_chunk = evidence_chunk.model_copy(update={"retrieval_text": "changed approved text"})

        self.assertEqual(
            routes_documents._approved_content_hash(base_chunk, security_level="internal"),
            routes_documents._approved_content_hash(evidence_chunk, security_level="internal"),
        )
        self.assertNotEqual(
            routes_documents._approved_content_hash(base_chunk, security_level="internal"),
            routes_documents._approved_content_hash(changed_text_chunk, security_level="internal"),
        )

    def test_approve_review_chunks_requires_security_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_review",
                    filename="review.pdf",
                    document_name="Review",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_review",
                [],
                [
                    Chunk(
                        chunk_id="chunk-1",
                        document_id="doc_review",
                        chunk_type="article",
                        text="needs classification",
                    )
                ],
                [],
            )
            evidence = _write_approval_evidence(
                root,
                settings=settings,
                document_id="doc_review",
                chunks=repository.get_chunks("doc_review"),
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as raised:
                    routes_documents.approve_review_chunks(
                        "doc_review",
                        routes_documents.ApprovalRequest(chunk_ids=["chunk-1"], **evidence),
                        _auth_context(),
                    )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("security_level", raised.exception.detail)

    def test_approve_review_chunks_requires_official_approval_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_no_evidence",
                    filename="review.pdf",
                    document_name="Review",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_no_evidence",
                [],
                [
                    Chunk(
                        chunk_id="chunk-1",
                        document_id="doc_no_evidence",
                        chunk_type="article",
                        text="safe approved content candidate",
                        security_level="internal",
                    )
                ],
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as raised:
                    routes_documents.approve_review_chunks(
                        "doc_no_evidence",
                        routes_documents.ApprovalRequest(
                            chunk_ids=["chunk-1"],
                            approval_id="approval-no-evidence",
                            security_level="internal",
                        ),
                        _auth_context(),
                    )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Official RAG/MCP approval evidence is required", raised.exception.detail)

    def test_get_review_chunks_rejects_viewer_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_review",
                    filename="review.pdf",
                    document_name="Review",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_review",
                [],
                [Chunk(chunk_id="chunk-1", document_id="doc_review", chunk_type="article", text="draft")],
                [],
            )
            viewer = AuthContext(actor="viewer", tenant_id="tenant-a", auth_mode="api_token", role="viewer")

            with patch.object(routes_documents, "get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as raised:
                    routes_documents.get_review_chunks("doc_review", viewer)

        self.assertEqual(raised.exception.status_code, 403)

    def test_index_status_rejects_viewer_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_index_status",
                    filename="review.pdf",
                    document_name="Review",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            viewer = AuthContext(actor="viewer", tenant_id="tenant-a", auth_mode="api_token", role="viewer")

            with patch.object(routes_documents, "get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as raised:
                    routes_documents.get_index_status("doc_index_status", viewer)

        self.assertEqual(raised.exception.status_code, 403)

    def test_security_scan_blocks_high_risk_chunks_without_storing_raw_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_security",
                    filename="security.pdf",
                    document_name="Security",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_security",
                [],
                [
                    Chunk(
                        chunk_id="chunk-risk",
                        document_id="doc_security",
                        chunk_type="article",
                        text="주민등록번호 900101-1234567 포함",
                        security_level="internal",
                    ),
                    Chunk(
                        chunk_id="chunk-safe",
                        document_id="doc_security",
                        chunk_type="article",
                        text="safe text",
                        security_level="internal",
                    ),
                ],
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                response = routes_documents.security_scan_document(
                    "doc_security",
                    routes_documents.SecurityScanRequest(block_high_risk=True),
                    _auth_context(),
                )
                history = routes_documents.get_security_review("doc_security", _auth_context())
                with self.assertRaises(HTTPException) as blocked_approval:
                    routes_documents.approve_review_chunks(
                        "doc_security",
                        routes_documents.ApprovalRequest(chunk_ids=["chunk-risk"], approval_id="approval-risk"),
                        _auth_context(),
                    )

            chunks = JsonRepository(settings).get_chunks("doc_security")
            risk = next(chunk for chunk in chunks if chunk.chunk_id == "chunk-risk")
            safe = next(chunk for chunk in chunks if chunk.chunk_id == "chunk-safe")

        self.assertEqual(response["finding_count"], 1)
        self.assertEqual(response["blocked_chunk_ids"], ["chunk-risk"])
        self.assertEqual(response["findings"][0]["rule_id"], "resident_registration_number")
        self.assertIn("match_hash", response["findings"][0])
        self.assertNotIn("900101", json.dumps(response, ensure_ascii=False))
        self.assertEqual(risk.approval_status, "security_blocked")
        self.assertEqual(safe.approval_status, "draft")
        self.assertEqual(history[0]["scan_id"], response["scan_id"])
        self.assertEqual(blocked_approval.exception.status_code, 400)
        self.assertIn("Security scan blocked", str(blocked_approval.exception.detail))

    def test_security_scan_removes_indexed_vectors_for_blocked_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_security_indexed",
                    filename="security.pdf",
                    document_name="Security",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_security_indexed",
                [],
                [
                    Chunk(
                        chunk_id="chunk-risk",
                        document_id="doc_security_indexed",
                        chunk_type="article",
                        text="security indexed text",
                        retrieval_text="security indexed text",
                        approval_status="approved",
                        approval_id="approval-risk",
                        approved_by="reviewer",
                        approved_at="2026-07-08T00:00:00+00:00",
                        approved_content_hash="hash-risk",
                        security_level="internal",
                        metadata=_approval_provenance_metadata(),
                    )
                ],
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                evidence = _write_approval_evidence(
                    Path(tmp),
                    settings=settings,
                    document_id="doc_security_indexed",
                    chunks=repository.get_chunks("doc_security_indexed"),
                )
                routes_documents.approve_review_chunks(
                    "doc_security_indexed",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["chunk-risk"],
                        approval_id="approval-risk",
                        security_level="internal",
                        **evidence,
                    ),
                    _auth_context(),
                )
                routes_documents.index_document(
                    "doc_security_indexed",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    _auth_context(),
                )
                indexed_chunks = repository.get_chunks("doc_security_indexed")
                indexed_chunks[0] = indexed_chunks[0].model_copy(
                    update={
                        "text": "resident id 900101-1234567",
                        "retrieval_text": "resident id 900101-1234567",
                    }
                )
                repository.save_chunks("doc_security_indexed", indexed_chunks)
                response = routes_documents.security_scan_document(
                    "doc_security_indexed",
                    routes_documents.SecurityScanRequest(block_high_risk=True),
                    _auth_context(),
                )
                status = routes_documents.get_index_status("doc_security_indexed", _auth_context())

            vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            stored = vector_path.read_text(encoding="utf-8")
            scans = JsonRepository(settings).list_security_scan_records("doc_security_indexed")
            manual_scan = next(record for record in scans if record.get("scan_reason") == "manual")

        self.assertEqual(response["blocked_chunk_ids"], ["chunk-risk"])
        self.assertEqual(response["vector_sync"]["action"], "review_vector_sync")
        self.assertEqual(response["vector_sync"]["upsert_summary"]["removed_count"], 1)
        self.assertEqual(manual_scan["vector_sync"]["upsert_summary"]["removed_count"], 1)
        self.assertEqual(status["indexing_status"], "indexed")
        self.assertEqual(status["latest_job"]["record_count"], 0)
        self.assertEqual(stored.strip(), "")

    def test_approval_runs_preapproval_security_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_preapproval_scan",
                    filename="security.pdf",
                    document_name="Security",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_preapproval_scan",
                [],
                [
                    Chunk(
                        chunk_id="chunk-risk",
                        document_id="doc_preapproval_scan",
                        chunk_type="article",
                        text="high risk identifier 900101-1234567",
                        security_level="internal",
                    )
                ],
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as blocked_approval:
                    routes_documents.approve_review_chunks(
                        "doc_preapproval_scan",
                        routes_documents.ApprovalRequest(chunk_ids=["chunk-risk"], approval_id="approval-risk"),
                        _auth_context(),
                    )
                scans = JsonRepository(settings).list_security_scan_records("doc_preapproval_scan")

            chunks = JsonRepository(settings).get_chunks("doc_preapproval_scan")

        self.assertEqual(blocked_approval.exception.status_code, 400)
        self.assertIn("Security scan blocked", str(blocked_approval.exception.detail))
        self.assertEqual(scans[0]["scan_reason"], "pre_approval")
        self.assertEqual(scans[0]["blocked_chunk_ids"], ["chunk-risk"])
        self.assertEqual(chunks[0].approval_status, "security_blocked")

    def test_split_review_chunk_supersedes_source_and_creates_review_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_review",
                    filename="review.pdf",
                    document_name="Review",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_review",
                [],
                [
                    Chunk(
                        chunk_id="chunk-1",
                        document_id="doc_review",
                        chunk_type="article",
                        text="one two",
                        retrieval_text="one two",
                        security_level="internal",
                    )
                ],
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                response = routes_documents.split_review_chunk(
                    "doc_review",
                    "chunk-1",
                    routes_documents.SplitChunkRequest(texts=["one", "two"]),
                    _auth_context(),
                )

            chunks = JsonRepository(settings).get_chunks("doc_review")
            source = next(chunk for chunk in chunks if chunk.chunk_id == "chunk-1")
            created = [chunk for chunk in chunks if chunk.chunk_id in response["created_chunk_ids"]]
            records = JsonRepository(settings).list_review_records("doc_review")

        self.assertEqual(source.approval_status, "superseded")
        self.assertEqual(len(created), 2)
        self.assertEqual({chunk.approval_status for chunk in created}, {"needs_review"})
        self.assertEqual(records[0]["action"], "split")

    def test_merge_review_chunks_supersedes_sources_and_creates_review_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_review",
                    filename="review.pdf",
                    document_name="Review",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_review",
                [],
                [
                    Chunk(
                        chunk_id="chunk-1",
                        document_id="doc_review",
                        chunk_type="article",
                        text="one",
                        security_level="internal",
                    ),
                    Chunk(
                        chunk_id="chunk-2",
                        document_id="doc_review",
                        chunk_type="article",
                        text="two",
                        security_level="internal",
                    ),
                ],
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                response = routes_documents.merge_review_chunks(
                    "doc_review",
                    routes_documents.MergeChunksRequest(chunk_ids=["chunk-1", "chunk-2"]),
                    _auth_context(),
                )

            chunks = JsonRepository(settings).get_chunks("doc_review")
            superseded = [chunk for chunk in chunks if chunk.chunk_id in {"chunk-1", "chunk-2"}]
            merged = next(chunk for chunk in chunks if chunk.chunk_id == response["created_chunk_ids"][0])
            records = JsonRepository(settings).list_review_records("doc_review")

        self.assertEqual({chunk.approval_status for chunk in superseded}, {"superseded"})
        self.assertEqual(merged.approval_status, "needs_review")
        self.assertEqual(merged.text, "one\n\ntwo")
        self.assertEqual(records[0]["action"], "merge")

    def test_update_review_chunk_invalidates_prior_approval_and_records_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_review",
                    filename="review.pdf",
                    document_name="Review",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_review",
                [],
                [
                    Chunk(
                        chunk_id="chunk-1",
                        document_id="doc_review",
                        chunk_type="article",
                        text="original text",
                        retrieval_text="original text",
                        approval_status="approved",
                        approval_id="approval-old",
                        approved_by="reviewer",
                        approved_at="2026-07-08T00:00:00+00:00",
                        approved_content_hash="hash-old",
                    )
                ],
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                response = routes_documents.update_review_chunk(
                    "doc_review",
                    "chunk-1",
                    routes_documents.ReviewChunkUpdateRequest(
                        text="reviewed text",
                        retrieval_text="reviewed text",
                        security_level="internal",
                        metadata_patch={"review_flag": "manual_edit"},
                    ),
                    _auth_context(),
                )

            updated = JsonRepository(settings).get_chunks("doc_review")[0]
            export_row = json.loads((settings.exports_dir / "doc_review.jsonl").read_text(encoding="utf-8").strip())
            review_records = JsonRepository(settings).list_review_records("doc_review")
            review_snapshot_exists = (settings.data_dir / review_records[0]["snapshot"]).is_file()

        self.assertEqual(response["chunk"]["text"], "reviewed text")
        self.assertEqual(updated.approval_status, "needs_review")
        self.assertIsNone(updated.approval_id)
        self.assertIsNone(updated.approved_content_hash)
        self.assertEqual(updated.security_level, "internal")
        self.assertEqual(updated.metadata["review_flag"], "manual_edit")
        self.assertEqual(export_row["approval_status"], "needs_review")
        self.assertEqual(review_records[0]["action"], "update")
        self.assertEqual(review_records[0]["status"], "needs_review")
        self.assertTrue(review_snapshot_exists)
        self.assertIn("before_content_hashes", review_records[0])
        self.assertIn("after_content_hashes", review_records[0])

    def test_reject_review_chunks_blocks_chunk_and_records_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_review",
                    filename="review.pdf",
                    document_name="Review",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_review",
                [],
                [
                    Chunk(
                        chunk_id="chunk-1",
                        document_id="doc_review",
                        chunk_type="article",
                        text="reject me",
                        approval_status="approved",
                        approval_id="approval-old",
                        approved_by="reviewer",
                        approved_at="2026-07-08T00:00:00+00:00",
                        approved_content_hash="hash-old",
                    )
                ],
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                response = routes_documents.reject_review_chunks(
                    "doc_review",
                    routes_documents.RejectRequest(chunk_ids=["chunk-1"], reason="contains sensitive data"),
                    _auth_context(),
                )

            updated = JsonRepository(settings).get_chunks("doc_review")[0]
            export_row = json.loads((settings.exports_dir / "doc_review.jsonl").read_text(encoding="utf-8").strip())
            review_records = JsonRepository(settings).list_review_records("doc_review")
            reject_snapshot_exists = (settings.data_dir / review_records[0]["snapshot"]).is_file()

        self.assertEqual(response["status"], "rejected")
        self.assertEqual(updated.approval_status, "rejected")
        self.assertIsNone(updated.approval_id)
        self.assertEqual(updated.metadata["review_rejection_reason"], "contains sensitive data")
        self.assertEqual(export_row["approval_status"], "rejected")
        self.assertEqual(review_records[0]["action"], "reject")
        self.assertTrue(reject_snapshot_exists)

    def test_reject_review_chunks_requires_reason_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_review",
                    filename="review.pdf",
                    document_name="Review",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_review",
                [],
                [
                    Chunk(
                        chunk_id="chunk-1",
                        document_id="doc_review",
                        chunk_type="article",
                        text="reject me",
                        approval_status="approved",
                        approval_id="approval-old",
                        approved_by="reviewer",
                        approved_at="2026-07-08T00:00:00+00:00",
                        approved_content_hash="hash-old",
                    )
                ],
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as raised:
                    routes_documents.reject_review_chunks(
                        "doc_review",
                        routes_documents.RejectRequest(chunk_ids=["chunk-1"], reason=" "),
                        _auth_context(),
                    )
            updated = JsonRepository(settings).get_chunks("doc_review")[0]
            review_records = JsonRepository(settings).list_review_records("doc_review")

        self.assertEqual(400, raised.exception.status_code)
        self.assertEqual("rejection reason is required.", raised.exception.detail)
        self.assertEqual("approved", updated.approval_status)
        self.assertEqual([], review_records)

    def test_approval_records_are_append_only_even_when_approval_id_repeats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_review",
                    filename="review.pdf",
                    document_name="Review",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_review",
                [],
                [
                    Chunk(
                        chunk_id="chunk-1",
                        document_id="doc_review",
                        chunk_type="article",
                        text="first",
                        security_level="internal",
                    ),
                    Chunk(
                        chunk_id="chunk-2",
                        document_id="doc_review",
                        chunk_type="article",
                        text="second",
                        security_level="internal",
                    ),
                ],
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                evidence_1 = _write_approval_evidence(
                    root,
                    settings=settings,
                    document_id="doc_review",
                    chunks=[repository.get_chunks("doc_review")[0]],
                )
                routes_documents.approve_review_chunks(
                    "doc_review",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["chunk-1"],
                        approval_id="same-approval",
                        **evidence_1,
                    ),
                    _auth_context(),
                )
                evidence_2 = _write_approval_evidence(
                    root,
                    settings=settings,
                    document_id="doc_review",
                    chunks=[JsonRepository(settings).get_chunks("doc_review")[1]],
                )
                routes_documents.approve_review_chunks(
                    "doc_review",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["chunk-2"],
                        approval_id="same-approval",
                        **evidence_2,
                    ),
                    _auth_context(),
                )

            stored_repository = JsonRepository(settings)
            approvals = stored_repository.list_approval_records("doc_review")
            sync_events = stored_repository.list_maintenance_events("approval_vector_sync_outcome")

        self.assertEqual(len(approvals), 2)
        self.assertEqual({record["approval_id"] for record in approvals}, {"same-approval"})
        self.assertEqual(len({record["approval_record_id"] for record in approvals}), 2)
        self.assertEqual(2, len(sync_events))
        self.assertEqual({record["approval_record_id"] for record in approvals}, {
            event["approval_record_id"] for event in sync_events
        })
        self.assertEqual({record["vector_sync_event_id"] for record in approvals}, {
            event["event_id"] for event in sync_events
        })
        self.assertEqual({"completed"}, {event["outcome"] for event in sync_events})

    def test_index_document_embeds_and_upserts_only_approved_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_index",
                    filename="index.pdf",
                    document_name="Index",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_index",
                [],
                [
                    Chunk(
                        chunk_id="approved-1",
                        document_id="doc_index",
                        chunk_type="article",
                        text="approved text",
                        retrieval_text="approved text",
                        approval_status="approved",
                        approval_id="approval-index",
                        approved_by="reviewer",
                        approved_at="2026-07-08T00:00:00+00:00",
                        approved_content_hash="hash-index",
                        security_level="internal",
                        metadata=_approval_provenance_metadata(),
                    ),
                    Chunk(
                        chunk_id="draft-1",
                        document_id="doc_index",
                        chunk_type="article",
                        text="draft text",
                        retrieval_text="draft text",
                    ),
                ],
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                evidence = _write_approval_evidence(
                    Path(tmp),
                    settings=settings,
                    document_id="doc_index",
                    chunks=[chunk for chunk in repository.get_chunks("doc_index") if chunk.chunk_id == "approved-1"],
                )
                routes_documents.approve_review_chunks(
                    "doc_index",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["approved-1"],
                        approval_id="approval-index",
                        security_level="internal",
                        **evidence,
                    ),
                    _auth_context(),
                )
                response = routes_documents.index_document(
                    "doc_index",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    _auth_context(),
                )
                status = routes_documents.get_index_status("doc_index", _auth_context())

            vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            stored_rows = [json.loads(line) for line in vector_path.read_text(encoding="utf-8").splitlines()]
            jobs = JsonRepository(settings).list_indexing_jobs("doc_index")

        self.assertEqual(response["status"], "indexed")
        self.assertEqual(response["record_count"], 1)
        self.assertEqual(response["vector_summary"]["skipped_unapproved_count"], 1)
        self.assertEqual(status["indexing_status"], "indexed")
        self.assertEqual(len(stored_rows), 1)
        self.assertEqual(stored_rows[0]["chunk_id"], "approved-1")
        self.assertEqual(stored_rows[0]["embedding_dimensions"], 8)
        self.assertEqual(jobs[0]["indexing_job_id"], response["indexing_job_id"])

    def test_index_document_rejects_approved_chunk_without_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_missing_hash",
                    filename="index.pdf",
                    document_name="Index",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_missing_hash",
                [],
                [
                    Chunk(
                        chunk_id="approved-1",
                        document_id="doc_missing_hash",
                        chunk_type="article",
                        text="approved text",
                        retrieval_text="approved text",
                        approval_status="approved",
                        approval_id="approval-index",
                        approved_by="reviewer",
                        approved_at="2026-07-08T00:00:00+00:00",
                        security_level="internal",
                    )
                ],
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as raised:
                    routes_documents.index_document(
                        "doc_missing_hash",
                        routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                        _auth_context(),
                    )
                status = routes_documents.get_index_status("doc_missing_hash", _auth_context())

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("approved_content_hash", str(raised.exception.detail))
        self.assertEqual(status["indexing_status"], "review_required")
        self.assertIn("approved_content_hash", status["validation_error"])

    def test_index_document_rejects_approved_chunk_without_approval_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_missing_provenance",
                    filename="index.pdf",
                    document_name="Index",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_missing_provenance",
                [],
                [
                    Chunk(
                        chunk_id="approved-1",
                        document_id="doc_missing_provenance",
                        chunk_type="article",
                        text="approved text",
                        retrieval_text="approved text",
                        approval_status="approved",
                        approval_id="approval-index",
                        approved_by="reviewer",
                        approved_at="2026-07-08T00:00:00+00:00",
                        approved_content_hash="hash-index",
                        security_level="internal",
                    )
                ],
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as raised:
                    routes_documents.index_document(
                        "doc_missing_provenance",
                        routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                        _auth_context(),
                    )
                status = routes_documents.get_index_status("doc_missing_provenance", _auth_context())

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("approval provenance", str(raised.exception.detail))
        self.assertEqual(status["indexing_status"], "review_required")
        self.assertIn("approval provenance", status["validation_error"])

    def test_index_status_requires_approval_journal_for_approved_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_missing_journal",
                    filename="index.pdf",
                    document_name="Index",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_missing_journal",
                [],
                [
                    Chunk(
                        chunk_id="approved-1",
                        document_id="doc_missing_journal",
                        chunk_type="article",
                        text="approved text",
                        retrieval_text="approved text",
                        approval_status="approved",
                        approval_id="approval-index",
                        approved_by="reviewer",
                        approved_at="2026-07-08T00:00:00+00:00",
                        approved_content_hash="hash-index",
                        security_level="internal",
                        metadata=_approval_provenance_metadata(),
                    )
                ],
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as raised:
                    routes_documents.index_document(
                        "doc_missing_journal",
                        routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                        _auth_context(),
                    )
                status = routes_documents.get_index_status("doc_missing_journal", _auth_context())

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("approval journal", str(raised.exception.detail))
        self.assertEqual(status["indexing_status"], "review_required")
        self.assertIn("approval journal", status["validation_error"])

    def test_index_document_does_not_write_artifacts_when_vector_metadata_leaks_local_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_leak",
                    filename="leak.pdf",
                    document_name="Leak",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_leak",
                [],
                [
                    Chunk(
                        chunk_id="approved-1",
                        document_id="doc_leak",
                        chunk_type="article",
                        text="approved text",
                        retrieval_text="approved text",
                        metadata={"source_file": r"C:\private\rules.pdf", **_approval_provenance_metadata()},
                        approval_status="approved",
                        approval_id="approval-leak",
                        approved_by="reviewer",
                        approved_at="2026-07-08T00:00:00+00:00",
                        approved_content_hash="hash-index",
                        security_level="internal",
                    )
                ],
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                evidence = _write_approval_evidence(
                    Path(tmp),
                    settings=settings,
                    document_id="doc_leak",
                    chunks=repository.get_chunks("doc_leak"),
                )
                routes_documents.approve_review_chunks(
                    "doc_leak",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["approved-1"],
                        approval_id="approval-leak",
                        security_level="internal",
                        **evidence,
                    ),
                    _auth_context(),
                )
                with self.assertRaises(HTTPException) as raised:
                    routes_documents.index_document(
                        "doc_leak",
                        routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                        _auth_context(),
                    )

            artifact_dir = settings.data_dir / "vector_ingestion" / "doc_leak"
            target_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(routes_documents._INDEXING_FAILURE_DETAIL, raised.exception.detail)
        self.assertFalse((artifact_dir / "vector_records.jsonl").exists())
        self.assertFalse((artifact_dir / "embedded_records.jsonl").exists())
        self.assertFalse(target_path.exists())

    def test_index_document_rejects_non_local_jsonl_target_for_secure_rag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_target",
                    filename="index.pdf",
                    document_name="Index",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_target",
                [],
                [
                    Chunk(
                        chunk_id="approved-1",
                        document_id="doc_target",
                        chunk_type="article",
                        text="approved text",
                        retrieval_text="approved text",
                        security_level="internal",
                    )
                ],
                [],
            )
            evidence = _write_approval_evidence(
                root,
                settings=settings,
                document_id="doc_target",
                chunks=repository.get_chunks("doc_target"),
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                routes_documents.approve_review_chunks(
                    "doc_target",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["approved-1"],
                        approval_id="approval-target",
                        security_level="internal",
                        **evidence,
                    ),
                    _auth_context(),
                )
                with self.assertRaises(HTTPException) as raised:
                    routes_documents.index_document(
                        "doc_target",
                        routes_documents.IndexRequest(target_type="qdrant-local-jsonl", embedding_dimensions=8),
                        _auth_context(),
                    )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("local-jsonl", str(raised.exception.detail))

    def test_index_document_rejects_when_no_approved_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_index",
                    filename="index.pdf",
                    document_name="Index",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_index",
                [],
                [
                    Chunk(
                        chunk_id="draft-1",
                        document_id="doc_index",
                        chunk_type="article",
                        text="draft text",
                        retrieval_text="draft text",
                    )
                ],
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as raised:
                    routes_documents.index_document("doc_index", routes_documents.IndexRequest(), _auth_context())

            jobs = JsonRepository(settings).list_indexing_jobs("doc_index")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(jobs, [])

    def test_index_status_removes_indexed_vector_after_chunk_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_index",
                    filename="index.pdf",
                    document_name="Index",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            chunks = [
                Chunk(
                    chunk_id="approved-1",
                    document_id="doc_index",
                    chunk_type="article",
                    text="approved text",
                    retrieval_text="approved text",
                )
            ]
            evidence = _write_approval_evidence(root, settings=settings, document_id="doc_index", chunks=chunks)
            repository.save_processing_result(
                "doc_index",
                [],
                chunks,
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                routes_documents.approve_review_chunks(
                    "doc_index",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["approved-1"],
                        approval_id="approval-index",
                        security_level="internal",
                        **evidence,
                    ),
                    _auth_context(),
                )
                routes_documents.index_document(
                    "doc_index",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    _auth_context(),
                )
                indexed_status = routes_documents.get_index_status("doc_index", _auth_context())
                routes_documents.reject_review_chunks(
                    "doc_index",
                    routes_documents.RejectRequest(chunk_ids=["approved-1"], reason="revoked approval"),
                    _auth_context(),
                )
                rejected_status = routes_documents.get_index_status("doc_index", _auth_context())
                jobs = JsonRepository(settings).list_indexing_jobs("doc_index")
                vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
                stored = vector_path.read_text(encoding="utf-8")

        self.assertEqual(indexed_status["indexing_status"], "indexed")
        self.assertEqual(rejected_status["indexing_status"], "indexed")
        self.assertEqual(rejected_status["latest_job"]["status"], "indexed")
        self.assertEqual(rejected_status["latest_job"]["action"], "review_vector_sync")
        self.assertEqual(rejected_status["latest_job"]["record_count"], 0)
        self.assertEqual(rejected_status["latest_job"]["upsert_summary"]["removed_count"], 1)
        self.assertEqual(rejected_status["vector_summary"]["record_count"], 0)
        self.assertEqual(rejected_status["vector_summary"]["approval_status_counts"], {"rejected": 1})
        self.assertEqual(stored.strip(), "")
        self.assertEqual([job["action"] for job in jobs], ["index", "review_vector_sync"])

    def test_index_status_requires_reindex_after_security_scope_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_scope",
                    filename="scope.pdf",
                    document_name="Scope",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            chunks = [
                Chunk(
                    chunk_id="scope-1",
                    document_id="doc_scope",
                    chunk_type="article",
                    text="scope text",
                    retrieval_text="scope text",
                )
            ]
            evidence = _write_approval_evidence(root, settings=settings, document_id="doc_scope", chunks=chunks)
            repository.save_processing_result(
                "doc_scope",
                [],
                chunks,
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                routes_documents.approve_review_chunks(
                    "doc_scope",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["scope-1"],
                        approval_id="approval-scope",
                        security_level="internal",
                        **evidence,
                    ),
                    _auth_context(),
                )
                routes_documents.index_document(
                    "doc_scope",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    _auth_context(),
                )
                indexed_status = routes_documents.get_index_status("doc_scope", _auth_context())
                updated_evidence = _write_approval_evidence(
                    root,
                    settings=settings,
                    document_id="doc_scope",
                    chunks=JsonRepository(settings).get_chunks("doc_scope"),
                )
                routes_documents.approve_review_chunks(
                    "doc_scope",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["scope-1"],
                        approval_id="approval-scope",
                        security_level="confidential",
                        **updated_evidence,
                    ),
                    _auth_context(),
                )
                changed_status = routes_documents.get_index_status("doc_scope", _auth_context())
                sync_events = JsonRepository(settings).list_maintenance_events("approval_vector_sync_outcome")

        self.assertEqual(indexed_status["indexing_status"], "indexed")
        self.assertEqual(changed_status["indexing_status"], "indexed")
        self.assertEqual(changed_status["vector_summary"]["record_count"], 1)
        self.assertEqual(changed_status["latest_job"]["action"], "review_vector_sync")
        self.assertEqual(changed_status["vector_consistency"]["stale_count"], 0)
        indexed_sync_events = [
            event
            for event in sync_events
            if event.get("vector_sync", {}).get("status") == "indexed"
        ]
        self.assertEqual(1, len(indexed_sync_events))
        self.assertEqual("completed", indexed_sync_events[0]["outcome"])
        self.assertEqual(
            changed_status["latest_job"]["indexing_job_id"],
            indexed_sync_events[0]["vector_sync"]["indexing_job_id"],
        )

    def test_index_status_detects_tampered_vector_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_tamper",
                    filename="tamper.pdf",
                    document_name="Tamper",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            chunks = [
                Chunk(
                    chunk_id="chunk-1",
                    document_id="doc_tamper",
                    chunk_type="article",
                    text="approved text",
                    retrieval_text="approved text",
                    security_level="internal",
                )
            ]
            evidence = _write_approval_evidence(root, settings=settings, document_id="doc_tamper", chunks=chunks)
            repository.save_processing_result(
                "doc_tamper",
                [],
                chunks,
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                routes_documents.approve_review_chunks(
                    "doc_tamper",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["chunk-1"],
                        approval_id="approval-tamper",
                        security_level="internal",
                        **evidence,
                    ),
                    _auth_context(),
                )
                routes_documents.index_document(
                    "doc_tamper",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    _auth_context(),
                )
                vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
                rows = [json.loads(line) for line in vector_path.read_text(encoding="utf-8").splitlines()]
                rows[0]["text"] = "tampered text"
                vector_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
                status = routes_documents.get_index_status("doc_tamper", _auth_context())

        self.assertEqual(status["indexing_status"], "reindex_required")
        self.assertEqual(status["vector_consistency"]["stale_count"], 1)
        self.assertEqual(status["vector_consistency"]["samples"][0]["reason"], "tampered_stored_vector")

    def test_index_status_detects_tampered_vector_embedding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_embedding_tamper",
                    filename="tamper.pdf",
                    document_name="Embedding Tamper",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            chunks = [
                Chunk(
                    chunk_id="chunk-1",
                    document_id="doc_embedding_tamper",
                    chunk_type="article",
                    text="approved embedding text",
                    retrieval_text="approved embedding text",
                    security_level="internal",
                )
            ]
            evidence = _write_approval_evidence(
                root,
                settings=settings,
                document_id="doc_embedding_tamper",
                chunks=chunks,
            )
            repository.save_processing_result(
                "doc_embedding_tamper",
                [],
                chunks,
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                routes_documents.approve_review_chunks(
                    "doc_embedding_tamper",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["chunk-1"],
                        approval_id="approval-embedding-tamper",
                        security_level="internal",
                        **evidence,
                    ),
                    _auth_context(),
                )
                routes_documents.index_document(
                    "doc_embedding_tamper",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    _auth_context(),
                )
                vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
                rows = [json.loads(line) for line in vector_path.read_text(encoding="utf-8").splitlines()]
                rows[0]["embedding"][0] = float(rows[0]["embedding"][0]) + 0.25
                vector_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
                status = routes_documents.get_index_status("doc_embedding_tamper", _auth_context())

        self.assertEqual(status["indexing_status"], "reindex_required")
        self.assertEqual(status["vector_consistency"]["stale_count"], 1)
        self.assertEqual(status["vector_consistency"]["samples"][0]["reason"], "embedding_hash_mismatch")

    def test_reindex_removes_vectors_for_rejected_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_index",
                    filename="index.pdf",
                    document_name="Index",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_index",
                [],
                [
                    Chunk(
                        chunk_id="approved-1",
                        document_id="doc_index",
                        chunk_type="article",
                        text="approved text",
                        retrieval_text="approved text",
                        approval_status="approved",
                        approval_id="approval-index",
                        approved_by="reviewer",
                        approved_at="2026-07-08T00:00:00+00:00",
                        approved_content_hash="hash-index",
                        security_level="internal",
                        metadata=_approval_provenance_metadata(),
                    )
                ],
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings):
                evidence = _write_approval_evidence(
                    Path(tmp),
                    settings=settings,
                    document_id="doc_index",
                    chunks=repository.get_chunks("doc_index"),
                )
                routes_documents.approve_review_chunks(
                    "doc_index",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["approved-1"],
                        approval_id="approval-index",
                        security_level="internal",
                        **evidence,
                    ),
                    _auth_context(),
                )
                routes_documents.index_document(
                    "doc_index",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    _auth_context(),
                )
                routes_documents.reject_review_chunks(
                    "doc_index",
                    routes_documents.RejectRequest(chunk_ids=["approved-1"], reason="revoked approval"),
                    _auth_context(),
                )
                reindex = routes_documents.reindex_document(
                    "doc_index",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    _auth_context(),
                )

            vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            stored = vector_path.read_text(encoding="utf-8")

        self.assertEqual(reindex["record_count"], 0)
        self.assertEqual(reindex["upsert_summary"]["removed_count"], 0)
        self.assertEqual(stored.strip(), "")

    def test_transition_regulation_status_records_valid_audit_outcome_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_lifecycle",
                    filename="lifecycle.pdf",
                    document_name="Lifecycle",
                    file_type="pdf",
                    file_hash="hash",
                    profile_id="institution-a",
                    tenant_id="tenant-a",
                    regulation_id="reg-1",
                    regulation_version="v1",
                    effective_from="2025-01-01",
                    regulation_status="approved",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_lifecycle",
                [],
                [Chunk(chunk_id="chunk-1", document_id="doc_lifecycle", chunk_type="article", text="body")],
                [],
            )

            with patch.object(routes_documents, "get_settings", return_value=settings), patch.object(
                routes_documents,
                "_sync_vector_index_after_review_change",
                return_value={"status": "completed"},
            ):
                response = routes_documents.transition_regulation_status(
                    "doc_lifecycle",
                    routes_documents.RegulationLifecycleRequest(status="repealed", reason="정기 정비"),
                    _auth_context(),
                )
            rows = [
                json.loads(line)
                for line in api_audit_path(settings).read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual("repealed", response["document"]["regulation_status"])
        lifecycle_rows = [row for row in rows if row["action"] == "document.regulation.lifecycle"]
        self.assertEqual(1, len(lifecycle_rows))
        self.assertEqual("success", lifecycle_rows[0]["outcome"])


class _RepositoryWithDocument:
    def __init__(self, document: Document):
        self.document = document

    def get_document(self, document_id: str):
        return self.document if document_id == self.document.document_id else None


def _repository_with_document(tenant_id: str | None = "tenant-a") -> _RepositoryWithDocument:
    return _RepositoryWithDocument(
        Document(
            document_id="doc_test",
            filename="doc_test.pdf",
            document_name="doc_test",
            file_type="pdf",
            file_hash="hash",
            tenant_id=tenant_id,
            status="uploaded",
        )
    )


def _approval_provenance_metadata() -> dict[str, str]:
    return {
        "approval_worklist_report_path": "reports/approval_worklist_current.json",
        "approval_worklist_report_sha256": "a" * 64,
        "approval_review_batch_manifest_path": "reports/approval_review_batches_current.json",
        "approval_review_batch_manifest_sha256": "b" * 64,
        "approval_review_batch_id": "approval-batch-001",
        "approval_review_batch_chunk_fingerprint": "c" * 64,
        "approval_review_strategy": "human_bulk_review",
    }


def _write_approval_evidence(root: Path, *, settings: Settings, document_id: str, chunks: list[Chunk]) -> dict[str, str]:
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    worklist_path = reports / "approval_worklist_current.json"
    batch_manifest_path = reports / "approval_review_batches_current.json"
    chunk_ids = [chunk.chunk_id for chunk in chunks]

    worklist = {
        "report_type": "approval_worklist",
        "generated_at": "2026-07-09T00:00:00+00:00",
        "data_dir": str(settings.data_dir),
        "effective_data_dir": str(settings.data_dir),
        "tenant_id": "tenant-a",
        "tenant_storage_isolation": False,
        "document_count": 1,
        "total_chunks": len(chunks),
        "manual_attention_chunks": 0,
        "low_risk_batch_review_candidate_chunks": len(chunks),
        "documents": [
            {
                "document_id": document_id,
                "document_name": "Review",
                "filename": "review.pdf",
                "total_chunks": len(chunks),
                "draft_chunks": len(chunks),
                "low_risk_batch_review_candidate_chunks": len(chunks),
            }
        ],
    }
    worklist_path.write_text(json.dumps(worklist, ensure_ascii=False, indent=2), encoding="utf-8")
    worklist_sha256 = _sha256_file(worklist_path)

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
    review_type = "low_risk_batch"
    batch_fingerprint = routes_documents._review_batch_chunk_fingerprint(batch_chunks, review_type)
    batch_id = f"approval-{worklist_sha256[:12]}-001-low-risk-batch-001-{batch_fingerprint[:12]}"
    manifest = {
        "report_type": "approval_review_batch_manifest",
        "generated_at": "2026-07-09T00:00:01+00:00",
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
            "manual_attention_chunks": 0,
            "low_risk_batch_review_candidate_chunks": len(chunks),
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
                "document_name": "Review",
                "filename": "review.pdf",
                "chunk_count": len(chunks),
                "chunk_ids": chunk_ids,
                "chunks": batch_chunks,
                "review_flags_acknowledged_required": False,
                "approval_request_template": {
                    "chunk_ids": chunk_ids,
                    "security_level": "internal",
                    "review_flags_acknowledged": False,
                    "worklist_report_path": "reports/approval_worklist_current.json",
                    "worklist_report_sha256": worklist_sha256,
                    "review_batch_manifest_path": "reports/approval_review_batches_current.json",
                    "review_batch_id": batch_id,
                    "review_batch_chunk_fingerprint": batch_fingerprint,
                    "review_strategy": "human_bulk_review",
                },
            }
        ],
    }
    batch_manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    batch_manifest_sha256 = _sha256_file(batch_manifest_path)
    return {
        "worklist_report_path": "reports/approval_worklist_current.json",
        "worklist_report_sha256": worklist_sha256,
        "review_batch_manifest_path": "reports/approval_review_batches_current.json",
        "review_batch_manifest_sha256": batch_manifest_sha256,
        "review_batch_id": batch_id,
        "review_batch_chunk_fingerprint": batch_fingerprint,
        "review_strategy": "human_bulk_review",
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _auth_context() -> AuthContext:
    return AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token")


if __name__ == "__main__":
    unittest.main()
