from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from app.core.config import Settings
from app.processors.chunker import Chunker
from app.schemas.chunk import Chunk, ChunkOptions
from app.schemas.parsed import ParsedBlock, ParsedDocument, ParsedPage
from app.schemas.structure import StructureNode
from app.services.document_service import DocumentService
from app.storage.file_store import sha256_bytes
from app.storage.repository import JsonRepository


class DocumentMetadataTests(unittest.TestCase):
    def test_upload_stores_public_institution_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            service = DocumentService(settings=settings, repository=repo)

            document = service.upload(
                "regulation.pdf",
                b"%PDF-1.4",
                document_name="Clean Regulation Title",
                institution_name="Example Institution",
                source_system="PUBLIC_PORTAL",
                source_url="https://example.test/law",
                source_record_id="3533584",
                source_file_id="3050658",
                source_disclosure_date="2026.05.19",
                source_posted_date="2026.05.19",
                profile_id="default-public-institution",
                tenant_id="tenant-a",
            )
            loaded = repo.get_document(document.document_id)

            self.assertEqual(loaded.document_name, "Clean Regulation Title")
            self.assertEqual(loaded.institution_name, "Example Institution")
            self.assertEqual(loaded.source_system, "PUBLIC_PORTAL")
            self.assertEqual(loaded.source_url, "https://example.test/law")
            self.assertEqual(loaded.source_record_id, "3533584")
            self.assertEqual(loaded.source_file_id, "3050658")
            self.assertEqual(loaded.source_disclosure_date, "2026.05.19")
            self.assertEqual(loaded.source_posted_date, "2026.05.19")
            self.assertEqual(loaded.profile_id, "default-public-institution")
            self.assertEqual(loaded.tenant_id, "tenant-a")

    def test_upload_stream_stores_file_without_full_content_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            service = DocumentService(settings=settings, repository=repo)

            document = service.upload_stream(
                "large-regulation.pdf",
                io.BytesIO(b"%PDF-1.4\nstreamed"),
                profile_id="default-public-institution",
                tenant_id="tenant-a",
            )
            stored = service.path_for(document)

            self.assertTrue(stored.is_file())
            self.assertEqual(stored.read_bytes(), b"%PDF-1.4\nstreamed")
            self.assertEqual(document.file_hash, sha256_bytes(b"%PDF-1.4\nstreamed"))
            self.assertEqual(repo.get_document(document.document_id).profile_id, "default-public-institution")
            self.assertEqual(repo.get_document(document.document_id).tenant_id, "tenant-a")

    def test_upload_stream_rejects_over_limit_and_removes_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), max_upload_mb=1)
            service = DocumentService(settings=settings)

            with self.assertRaisesRegex(ValueError, "Upload exceeds"):
                service.upload_stream("too-large.pdf", io.BytesIO(b"x" * (1024 * 1024 + 1)))

            self.assertEqual(list(settings.uploads_dir.glob("*.tmp")), [])
            self.assertEqual(list(settings.uploads_dir.glob("*.pdf")), [])

    def test_upload_stream_rejects_hash_mismatch_before_document_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            service = DocumentService(settings=settings, repository=repo)

            with self.assertRaisesRegex(ValueError, "SHA-256"):
                service.upload_stream(
                    "regulation.pdf",
                    io.BytesIO(b"%PDF-1.4\nsource"),
                    expected_sha256="0" * 64,
                )

            self.assertEqual(repo.list_documents(), [])
            self.assertEqual(list(settings.uploads_dir.rglob("*.pdf")), [])

    def test_clone_as_reprocessing_draft_preserves_approved_source_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            service = DocumentService(settings=settings, repository=repo)
            source_bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy-hwp"
            source = service.upload(
                "regulation.hwp",
                source_bytes,
                document_name="Clean Regulation",
                institution_name="Example Institution",
                source_system="PUBLIC_PORTAL",
                source_record_id="record-1",
                profile_id="profile-a",
                regulation_id="reg-a",
                regulation_version="2026-01",
                revision_date="2026-01-01",
                effective_from="2026-01-01",
                tenant_id="tenant-a",
            ).model_copy(update={"status": "completed", "regulation_status": "approved"})
            repo.upsert_document(source)
            approved_chunk = Chunk(
                chunk_id="approved-source-chunk",
                document_id=source.document_id,
                chunk_type="article",
                text="approved",
                approval_status="approved",
                approval_id="approval-1",
                approved_content_hash="approved-hash",
            )
            repo.save_chunks(source.document_id, [approved_chunk])
            approval = {
                "approval_id": "approval-1",
                "document_id": source.document_id,
                "tenant_id": "tenant-a",
                "chunk_ids": [approved_chunk.chunk_id],
                "approved_at": "2026-01-02T00:00:00+00:00",
            }
            indexing_job = {
                "indexing_job_id": "index-1",
                "document_id": source.document_id,
                "tenant_id": "tenant-a",
                "status": "indexed",
                "created_at": "2026-01-02T00:00:00+00:00",
            }
            repo.append_approval_record(approval)
            repo.append_indexing_job(indexing_job)
            source_before = repo.get_document(source.document_id)
            chunks_before = repo.get_chunks(source.document_id)
            approvals_before = repo.list_approval_records(source.document_id)
            indexes_before = repo.list_indexing_jobs(source.document_id)

            draft = service.clone_as_reprocessing_draft(source.document_id)

            self.assertNotEqual(draft.document_id, source.document_id)
            self.assertEqual(draft.regulation_status, "draft")
            self.assertEqual(draft.status, "uploaded")
            self.assertEqual(draft.supersedes_document_id, source.document_id)
            self.assertEqual(draft.reprocessing_source_document_id, source.document_id)
            self.assertEqual(draft.reprocessing_reason, "kordoc_evidence_recovery")
            self.assertEqual(draft.file_hash, source.file_hash)
            self.assertEqual(service.path_for(draft).read_bytes(), source_bytes)
            self.assertEqual(service.path_for(source).read_bytes(), source_bytes)
            self.assertEqual(repo.get_document(source.document_id), source_before)
            self.assertEqual(repo.get_chunks(source.document_id), chunks_before)
            self.assertEqual(repo.list_approval_records(source.document_id), approvals_before)
            self.assertEqual(repo.list_indexing_jobs(source.document_id), indexes_before)
            self.assertEqual(repo.get_chunks(draft.document_id), [])
            self.assertEqual(repo.list_approval_records(draft.document_id), [])
            self.assertEqual(repo.list_indexing_jobs(draft.document_id), [])

    def test_clone_from_unapproved_source_does_not_create_supersede_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            service = DocumentService(settings=settings, repository=repo)
            source = service.upload(
                "regulation.pdf",
                b"%PDF-1.4\ndraft",
                regulation_id="reg-a",
                regulation_version="2026-01",
            )

            draft = service.clone_as_reprocessing_draft(source.document_id)

            self.assertIsNone(draft.supersedes_document_id)
            self.assertEqual(draft.reprocessing_source_document_id, source.document_id)

    def test_clone_from_pending_revision_preserves_existing_predecessor_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            service = DocumentService(settings=settings, repository=repo)
            source = service.upload(
                "regulation.pdf",
                b"%PDF-1.4\npending revision",
                regulation_id="reg-a",
                regulation_version="2026-02",
                regulation_status="pending_approval",
                supersedes_document_id="doc-approved-prior",
            )

            draft = service.clone_as_reprocessing_draft(source.document_id)

            self.assertEqual(draft.supersedes_document_id, "doc-approved-prior")
            self.assertEqual(draft.reprocessing_source_document_id, source.document_id)

    def test_chunker_propagates_document_metadata_to_chunks(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_meta",
            source_file="meta.pdf",
            document_name="Meta Regulation",
            file_type="pdf",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text="Article body")])],
            raw_text="Article body",
            metadata={
                "institution_name": "Example Institution",
                "source_system": "PUBLIC_PORTAL",
                "source_url": "https://example.test/law",
                "source_record_id": "3533584",
                "source_file_id": "3050658",
                "source_disclosure_date": "2026.05.19",
                "source_posted_date": "2026.05.19",
                "profile_id": "default-public-institution",
            },
        )
        node = StructureNode(
            node_id="node_article",
            document_id=parsed.document_id,
            node_type="article",
            number="1",
            title="Purpose",
            text="Article body",
            order_index=0,
        )

        chunk = Chunker().build_chunks([node], parsed, ChunkOptions(include_context_header=False))[0]

        self.assertEqual(chunk.metadata["institution_name"], "Example Institution")
        self.assertEqual(chunk.metadata["source_system"], "PUBLIC_PORTAL")
        self.assertEqual(chunk.metadata["source_url"], "https://example.test/law")
        self.assertEqual(chunk.metadata["source_record_id"], "3533584")
        self.assertEqual(chunk.metadata["source_file_id"], "3050658")
        self.assertEqual(chunk.metadata["source_disclosure_date"], "2026.05.19")
        self.assertEqual(chunk.metadata["source_posted_date"], "2026.05.19")
        self.assertEqual(chunk.metadata["profile_id"], "default-public-institution")

    def test_chunker_uses_document_title_when_no_regulation_boundary_exists(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_single",
            source_file="single.hwp",
            document_name="single",
            file_type="hwp",
            pages=[
                ParsedPage(
                    page_no=1,
                    blocks=[
                        ParsedBlock(text="Safety Management Guideline"),
                        ParsedBlock(text="Article body"),
                    ],
                )
            ],
            raw_text="Safety Management Guideline\nArticle body",
        )
        node = StructureNode(
            node_id="node_article",
            document_id=parsed.document_id,
            node_type="article",
            number="1",
            title="Purpose",
            text="Article body",
            order_index=0,
        )

        chunk = Chunker().build_chunks([node], parsed, ChunkOptions(include_context_header=False))[0]

        self.assertEqual(chunk.metadata["regulation_no"], "Safety Management Guideline")
        self.assertEqual(chunk.metadata["regulation_title"], "Safety Management Guideline")
        self.assertTrue(chunk.metadata["regulation_inferred_from_document"])


if __name__ == "__main__":
    unittest.main()
