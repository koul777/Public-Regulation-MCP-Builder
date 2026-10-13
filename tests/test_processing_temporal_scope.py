from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Callable
from unittest.mock import patch

from app.core.config import Settings
from app.core.security import AuthContext
from app.schemas.chunk import Chunk, ChunkOptions
from app.schemas.document import Document
from app.schemas.parsed import ParsedBlock, ParsedDocument, ParsedPage
from app.services.processing_service import ProcessingService
from app.services.document_service import DocumentService
from app.services.regulation_catalog_service import filter_to_latest_active_versions
from app.services.regulation_rag_runtime import expected_vector_record_for_chunk
from app.storage.repository import JsonRepository


class ProcessingTemporalScopeTests(unittest.TestCase):
    def _process(
        self, bodies: list[str], *, change_chunks: Callable[[list[Chunk]], None] | None = None,
    ) -> tuple[list, list, Document]:
        class Parser:
            def parse(self, path: Path, document_id: str) -> ParsedDocument:
                return ParsedDocument(
                    document_id=document_id,
                    source_file=path.name,
                    document_name="공개 합성 규정집",
                    file_type="pdf",
                    pages=[
                        ParsedPage(page_no=index + 1, blocks=[ParsedBlock(text=line) for line in body.splitlines()])
                        for index, body in enumerate(bodies)
                    ],
                    raw_text="\n".join(bodies),
                )

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), enable_agent_review=False)
            repository = JsonRepository(settings)
            document = Document(
                document_id="doc-temporal-scope", filename="synthetic-2099-01-01.pdf",
                document_name="공개 합성 규정집", document_name_source="user", file_type="pdf",
                file_hash="synthetic-hash", institution_name="공개 테스트기관",
                profile_id="synthetic-profile", tenant_id="synthetic-tenant",
                regulation_id="synthetic-book", regulation_version="synthetic-version",
                revision_date="2099-01-01", effective_from="2099-01-01",
                effective_to="2099-12-30", repealed_at="2099-12-31",
                source_system="local_upload", source_url="local-upload://synthetic",
                status="uploaded",
            )
            repository.upsert_document(document)
            service = ProcessingService(settings=settings, repository=repository)
            upload_path = DocumentService(settings, repository).path_for(document)
            upload_path.parent.mkdir(parents=True, exist_ok=True)
            upload_path.write_bytes(b"synthetic parser fixture")
            build_chunks = service.chunker.build_chunks

            def build(*args, **kwargs):
                chunks = build_chunks(*args, **kwargs)
                if change_chunks:
                    change_chunks(chunks)
                return chunks

            with patch("app.services.processing_service.get_parser", return_value=Parser()), patch.object(
                service.kordoc_table_parser, "parse_file",
                return_value={"status": "parsed", "parser": "kordoc", "table_count": 0, "tables": []},
            ), patch.object(
                service.chunker, "build_chunks", side_effect=build,
            ):
                job = service.process(document.document_id, ChunkOptions(enable_agent_review=False))
            self.assertEqual("completed", job.status, job.error)
            return (
                repository.get_chunks(document.document_id),
                repository.get_nodes(document.document_id),
                repository.get_document(document.document_id),
            )

    def test_combined_units_preserve_own_dates_without_document_lifecycle_fallback(self) -> None:
        chunks, nodes, document = self._process([
            "1-1-1. 인사규정\n제정 2020. 1. 2.\n제1조(목적) 직원 인사 기준을 정한다.\n"
            "부칙\n제1조(시행일) 이 규정은 2020년 2월 1일부터 시행한다.",
            "1-1-2. 보수규정\n개정 2023. 3. 4.\n제1조(목적) 보수 지급 기준을 정한다.",
            "1-1-3. 기록규정\n제1조(목적) 기록 관리 기준을 정한다.",
        ])
        self.assertEqual(3, sum(node.node_type == "regulation" for node in nodes))
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertTrue(chunk.metadata["document_lifecycle_dates_not_inherited"])
            for key in ("effective_from", "effective_to", "repealed_at"):
                self.assertIsNone(chunk.metadata.get(key), (chunk.chunk_id, key))
            self.assertEqual("synthetic-profile", chunk.metadata["profile_id"])
            self.assertEqual("synthetic-tenant", chunk.metadata["tenant_id"])
            self.assertEqual("local-upload://synthetic", chunk.metadata["source_url"])
        personnel = [chunk for chunk in chunks if chunk.metadata.get("regulation_no") == "1-1-1"]
        pay = [chunk for chunk in chunks if chunk.metadata.get("regulation_no") == "1-1-2"]
        undated = [chunk for chunk in chunks if chunk.metadata.get("regulation_no") == "1-1-3"]
        self.assertTrue(personnel and pay and undated)
        self.assertTrue(any(chunk.metadata.get("valid_from") == "2020-02-01" for chunk in personnel))
        self.assertTrue(any(chunk.metadata.get("revision_date") == "2023-03-04" for chunk in pay))
        self.assertTrue(all(not chunk.metadata.get("revision_date") for chunk in undated))
        self.assertTrue(all(not chunk.metadata.get("valid_from") for chunk in undated))
        # The uploaded document retains its original metadata for provenance.
        self.assertEqual("2099-12-30", document.effective_to)
        self.assertEqual("2099-12-31", document.repealed_at)

        # Index normalization uses the unit's own date evidence, never the
        # document's inferred date. Undated units remain ineligible as current.
        auth = AuthContext(actor="test", tenant_id="synthetic-tenant", auth_mode="local", role="admin")
        for candidates, expected_start in ((pay, "2023-03-04"), (undated, None)):
            chunk = candidates[0].model_copy(update={
                "approval_status": "approved", "approval_id": "synthetic-approval",
                "approved_content_hash": "synthetic-content-hash", "security_level": "public",
                "metadata": {**candidates[0].metadata, "regulation_status": "approved"},
            })
            record = expected_vector_record_for_chunk(chunk, document, auth)
            self.assertIsNotNone(record)
            self.assertEqual(expected_start, record["metadata"].get("effective_from"))
            self.assertIsNone(record["metadata"].get("effective_to"))
            self.assertIsNone(record["metadata"].get("repealed_at"))
            visible = filter_to_latest_active_versions([record], as_of="2026-01-01", include_legacy=False)
            self.assertEqual(bool(expected_start), bool(visible))

    def test_explicit_chunk_dates_and_unmapped_table_provenance_are_preserved(self) -> None:
        explicit = {
            "revision_date": "2018-01-02", "effective_from": "2018-02-01",
            "effective_date": "2018-02-01", "valid_from": "2018-02-01",
            "effective_to": "2018-12-30", "repealed_at": "2018-12-31",
        }

        def change(chunks: list[Chunk]) -> None:
            chunks[0].metadata.update(explicit)
            chunks.append(Chunk(
                chunk_id="unmapped-table", document_id="doc-temporal-scope",
                chunk_type="table", text="항목 | 값\n보조 | 자료",
                metadata={"review_flags": ["unmatched_table"]},
            ))

        chunks, _nodes, document = self._process([
            "1-1-1. 인사규정\n개정 2020. 1. 2.\n제1조(목적) 직원 인사 기준을 정한다.",
            "1-1-2. 보수규정\n개정 2023. 3. 4.\n제1조(목적) 보수 지급 기준을 정한다.",
        ], change_chunks=change)
        for field, value in explicit.items():
            self.assertEqual(value, chunks[0].metadata[field])
        table = next(chunk for chunk in chunks if chunk.chunk_id == "unmapped-table")
        self.assertNotIn("document_lifecycle_dates_not_inherited", table.metadata)
        self.assertNotIn("regulation_node_id", table.metadata)
        self.assertEqual(document.effective_from, table.metadata["effective_from"])
        self.assertEqual("2099-12-30", table.metadata["effective_to"])
        self.assertEqual(["unmatched_table"], table.metadata["review_flags"])

    def test_standalone_preserves_existing_document_lifecycle_fallback(self) -> None:
        chunks, nodes, document = self._process([
            "1-1-1. 인사규정\n제1조(목적) 직원 인사 기준을 정한다.",
        ])
        self.assertEqual(1, sum(node.node_type == "regulation" for node in nodes))
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertNotIn("document_lifecycle_dates_not_inherited", chunk.metadata)
            self.assertEqual(document.effective_from, chunk.metadata["effective_from"])
            self.assertEqual("2099-12-30", chunk.metadata["effective_to"])
            self.assertEqual("2099-12-31", chunk.metadata["repealed_at"])


if __name__ == "__main__":
    unittest.main()
