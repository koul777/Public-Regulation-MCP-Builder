from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from app.core.config import Settings
from app.processors.chunker import Chunker
from app.schemas.chunk import ChunkOptions
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
