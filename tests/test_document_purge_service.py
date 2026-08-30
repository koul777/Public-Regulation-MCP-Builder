from __future__ import annotations

from datetime import datetime, timezone
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.retrieval.bm25_index import default_bm25_index_path, load_bm25_index, write_bm25_index
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.schemas.run import ProcessingRun
from app.services.document_purge_service import DocumentPurgeService
from app.services.document_service import DocumentService
from app.storage.repository import JsonRepository


class DocumentPurgeServiceTests(unittest.TestCase):
    def _seed(self, root: Path) -> tuple[Settings, JsonRepository, Path, Path]:
        settings = Settings(data_dir=root / "data", artifact_root=root)
        repository = JsonRepository(settings)
        document = Document(
            document_id="doc_delete",
            filename="delete.pdf",
            document_name="삭제 규정",
            file_type="pdf",
            file_hash="delete-hash",
            tenant_id="default",
            profile_id="test-profile",
            status="completed",
        )
        repository.upsert_document(document)
        repository.save_chunks(
            document.document_id,
            [
                Chunk(
                    chunk_id="chunk_delete",
                    document_id=document.document_id,
                    chunk_type="article",
                    text="제1조 삭제 대상",
                    approval_status="approved",
                )
            ],
        )
        repository.upsert_run(
            ProcessingRun(
                run_id="run_delete",
                document_id=document.document_id,
                job_id="job_delete",
                tenant_id="default",
                status="completed",
                started_at=datetime.now(timezone.utc),
                elapsed_seconds=1.0,
            )
        )
        repository.append_approval_record(
            {
                "approval_id": "approval_delete",
                "document_id": document.document_id,
                "approved_at": datetime.now(timezone.utc).isoformat(),
                "approved_content_hashes": {"chunk_delete": "sha256:" + "a" * 64},
            }
        )
        source_path = DocumentService(settings=settings, repository=repository).path_for(document)
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(b"synthetic source")
        export_path = settings.data_dir / "exports" / "doc_delete.jsonl"
        export_path.parent.mkdir(parents=True, exist_ok=True)
        export_path.write_text("{}\n", encoding="utf-8")
        vector_path = settings.data_dir / "vector_db" / "default" / "approved_vectors.jsonl"
        vector_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_version": "reg-rag-vector-record-v1",
            "id": "chunk_delete",
            "document_id": "doc_delete",
            "chunk_id": "chunk_delete",
            "text": "제1조 삭제 대상",
            "metadata": {
                "document_id": "doc_delete",
                "chunk_id": "chunk_delete",
                "approval_status": "approved",
                "approval_id": "approval_delete",
                "approved_content_hash": "sha256:" + "a" * 64,
                "approved_at": datetime.now(timezone.utc).isoformat(),
                "approved_by": "operator",
                "tenant_id": "default",
            },
        }
        vector_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        write_bm25_index(default_bm25_index_path(vector_path), [record])
        return settings, repository, source_path, export_path

    def test_purge_removes_document_source_vectors_bm25_exports_and_journals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repository, source_path, export_path = self._seed(Path(tmp))
            vector_path = settings.data_dir / "vector_db" / "default" / "approved_vectors.jsonl"

            result = DocumentPurgeService(settings=settings, repository=repository).purge(
                ["doc_delete"]
            )

            self.assertEqual(1, result.deleted_document_count)
            self.assertEqual(1, result.deindexed_record_count)
            self.assertIsNone(repository.get_document("doc_delete"))
            self.assertFalse(source_path.exists())
            self.assertFalse(export_path.exists())
            self.assertEqual([], repository.list_runs())
            self.assertEqual([], repository.list_approval_records())
            self.assertEqual("", vector_path.read_text(encoding="utf-8").strip())
            self.assertEqual([], load_bm25_index(default_bm25_index_path(vector_path)).documents)

    def test_deindex_failure_aborts_before_document_or_source_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repository, source_path, _export_path = self._seed(Path(tmp))
            service = DocumentPurgeService(settings=settings, repository=repository)

            def fail_deindex(_documents, result):
                result.failures.append("색인 해제 실패")
                return 0

            with patch.object(service._delegate, "_deindex_documents", side_effect=fail_deindex):
                result = service.purge(["doc_delete"])

            self.assertEqual(0, result.deleted_document_count)
            self.assertIsNotNone(repository.get_document("doc_delete"))
            self.assertTrue(source_path.exists())
            self.assertEqual(1, len(repository.list_approval_records()))


if __name__ == "__main__":
    unittest.main()
