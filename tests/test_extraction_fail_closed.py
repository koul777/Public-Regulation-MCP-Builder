from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.parsers.base import ParserError
from app.schemas.chunk import ChunkOptions
from app.schemas.document import Document
from app.schemas.parsed import ParsedDocument
from app.services.processing_service import ProcessingService
from app.storage.repository import JsonRepository


class _EmptyParser:
    def parse(self, path: Path, document_id: str) -> ParsedDocument:
        return ParsedDocument(
            document_id=document_id,
            source_file=path.name,
            file_type="pdf",
            pages=[],
            raw_text="",
        )


class ExtractionFailClosedTests(unittest.TestCase):
    def test_empty_extraction_blocks_before_normalization_and_chunking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings(data_dir=Path(temporary) / "data", enable_kordoc_table_parser=False)
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc-empty",
                    filename="empty.pdf",
                    file_type="pdf",
                    file_hash="fixture-hash",
                    tenant_id="tenant-a",
                    status="uploaded",
                )
            )
            service = ProcessingService(settings=settings, repository=repository)
            with patch("app.services.processing_service.get_parser", return_value=_EmptyParser()):
                with self.assertRaises(ParserError):
                    service.process("doc-empty", ChunkOptions(enable_agent_review=False))

            run = repository.list_runs("doc-empty")[-1]
            chunks = repository.get_chunks("doc-empty")
            stored_document = repository.get_document("doc-empty")

        self.assertEqual("failed", stored_document.status)
        self.assertEqual([], chunks)
        trace = run.stats["pipeline_trace"]
        self.assertEqual("blocked", trace["stages"][-1]["status"])
        self.assertEqual(
            "extraction_not_ready_for_normalization",
            trace["stages"][-1]["reason_code"],
        )


if __name__ == "__main__":
    unittest.main()
