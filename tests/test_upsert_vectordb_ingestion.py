from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api import routes_rag
from app.core.config import Settings
from app.core.security import AuthContext
from app.ingestion.vector_adapter import VECTOR_RECORD_SCHEMA_VERSION, stable_content_hash
from app.retrieval.bm25_index import BM25_RETRIEVAL_MODEL
from scripts.upsert_vectordb_ingestion import main, upsert_vectordb_ingestion


class UpsertVectorDbIngestionTests(unittest.TestCase):
    def test_rejects_mixed_tenant_input_before_writing_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records_jsonl = root / "records.jsonl"
            first = _record("doc:chunk-1", "text")
            second = _record("doc:chunk-2", "text")
            second["tenant_id"] = "tenant-b"
            second["metadata"]["tenant_id"] = "tenant-b"
            records_jsonl.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in (first, second)) + "\n",
                encoding="utf-8",
            )
            target_path = root / "target.jsonl"

            with self.assertRaisesRegex(ValueError, "multiple tenant scopes"):
                upsert_vectordb_ingestion(
                    records_jsonl,
                    target_type="local-jsonl",
                    target_path=target_path,
                    out_manifest=root / "manifest.json",
                    require_repository_approval=False,
                )

        self.assertFalse(target_path.exists())

    def test_isolated_upsert_requires_explicit_tenant_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records_jsonl = root / "records.jsonl"
            records_jsonl.write_text(json.dumps(_record("doc:chunk-1", "text")) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "tenant_id is required"):
                upsert_vectordb_ingestion(
                    records_jsonl,
                    target_type="local-jsonl",
                    target_path=root / "target.jsonl",
                    out_manifest=root / "manifest.json",
                    tenant_storage_isolation=True,
                    require_repository_approval=False,
                )

    def test_isolated_upsert_requires_canonical_target_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records_jsonl = root / "records.jsonl"
            records_jsonl.write_text(json.dumps(_record("doc:chunk-1", "text")) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "canonical path"):
                upsert_vectordb_ingestion(
                    records_jsonl,
                    target_type="local-jsonl",
                    target_path=root / "wrong-target.jsonl",
                    out_manifest=root / "manifest.json",
                    data_dir=root / "data",
                    tenant_storage_isolation=True,
                    tenant_id="tenant-a",
                    require_repository_approval=False,
                )

    def test_existing_foreign_tenant_target_is_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            target_path = data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            target_path.parent.mkdir(parents=True)
            foreign = _record("doc:foreign", "foreign")
            foreign["tenant_id"] = "tenant-b"
            foreign["metadata"]["tenant_id"] = "tenant-b"
            target_path.write_text(json.dumps(foreign) + "\n", encoding="utf-8")
            records_jsonl = root / "records.jsonl"
            records_jsonl.write_text(json.dumps(_record("doc:chunk-1", "text")) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "belongs to tenant"):
                upsert_vectordb_ingestion(
                    records_jsonl,
                    target_type="local-jsonl",
                    target_path=target_path,
                    out_manifest=root / "manifest.json",
                    data_dir=data_dir,
                    tenant_id="tenant-a",
                    require_repository_approval=False,
                )

            self.assertIn('"foreign"', target_path.read_text(encoding="utf-8"))

    def test_upserts_records_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records_jsonl = root / "records.jsonl"
            records_jsonl.write_text(json.dumps(_record("doc:chunk-1", "text"), ensure_ascii=False) + "\n", encoding="utf-8")
            target_path = root / "target.jsonl"
            out_manifest = root / "manifest.json"

            manifest = upsert_vectordb_ingestion(
                records_jsonl,
                target_type="local-jsonl",
                target_path=target_path,
                out_manifest=out_manifest,
                require_repository_approval=False,
            )

            stored = [json.loads(line) for line in target_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(manifest["report_type"], "vectordb_upsert")
        self.assertEqual(manifest["inserted_count"], 1)
        self.assertEqual(stored[0]["id"], "doc:chunk-1")

    def test_document_id_removes_stale_same_document_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records_jsonl = root / "records.jsonl"
            target_path = root / "target.jsonl"
            existing = [_record("doc:chunk-1", "old"), _record("doc:chunk-stale", "stale")]
            target_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in existing) + "\n",
                encoding="utf-8",
            )
            records_jsonl.write_text(json.dumps(_record("doc:chunk-1", "new"), ensure_ascii=False) + "\n", encoding="utf-8")

            manifest = upsert_vectordb_ingestion(
                records_jsonl,
                target_type="local-jsonl",
                target_path=target_path,
                out_manifest=root / "manifest.json",
                document_id="doc",
                require_repository_approval=False,
            )

            stored = [json.loads(line) for line in target_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(manifest["document_id"], "doc")
        self.assertEqual(manifest["updated_count"], 1)
        self.assertEqual(manifest["removed_count"], 1)
        self.assertEqual([record["id"] for record in stored], ["doc:chunk-1"])

    def test_local_jsonl_upsert_writes_bm25_index_consumed_by_rag_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            records_jsonl = root / "records.jsonl"
            records = [
                _record("doc:alpha", "alpha leave policy"),
                _record("doc:beta", "beta pay policy"),
            ]
            records_jsonl.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                encoding="utf-8",
            )

            manifest = upsert_vectordb_ingestion(
                records_jsonl,
                target_type="local-jsonl",
                target_path=vector_path,
                out_manifest=root / "manifest.json",
                require_repository_approval=False,
            )
            routes_rag._RAG_VECTOR_RECORD_CACHE.clear()
            routes_rag._RAG_BM25_INDEX_CACHE.clear()
            routes_rag._RAG_VECTOR_SOURCE_HASH_CACHE.clear()
            loaded_records = routes_rag._load_local_vector_records(settings, auth)
            scored, metadata = routes_rag._score_records(
                "alpha",
                loaded_records,
                settings=settings,
                auth=auth,
                all_records=loaded_records,
            )
            bm25_index_exists = Path(manifest["bm25_index_path"]).exists()

        self.assertTrue(manifest["bm25_index_written"])
        self.assertTrue(bm25_index_exists)
        self.assertEqual(BM25_RETRIEVAL_MODEL, metadata["retrieval_model"])
        self.assertFalse(metadata["retrieval_fallback"])
        self.assertEqual("ready", metadata["bm25_index_status"])
        self.assertEqual("doc:alpha", scored[0][1]["id"])

    def test_main_returns_error_for_invalid_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records_jsonl = root / "records.jsonl"
            records_jsonl.write_text("{}\n", encoding="utf-8")
            with patch(
                "sys.argv",
                [
                    "upsert_vectordb_ingestion.py",
                    "--records-jsonl",
                    str(records_jsonl),
                    "--target-path",
                    str(root / "target.jsonl"),
                    "--out-manifest",
                    str(root / "manifest.json"),
                ],
            ):
                exit_code = main()

        self.assertEqual(exit_code, 2)


def _record(record_id: str, text: str) -> dict:
    metadata = {
        "document_id": "doc",
        "tenant_id": "tenant-a",
        "chunk_id": record_id.rsplit(":", 1)[-1],
        "approval_status": "approved",
        "approval_id": f"approval-{record_id.rsplit(':', 1)[-1]}",
        "approved_content_hash": f"approved-{record_id.rsplit(':', 1)[-1]}",
        "security_level": "internal",
        "approval_worklist_report_path": "reports/approval_worklist_current.json",
        "approval_worklist_report_sha256": "a" * 64,
        "approval_review_batch_manifest_path": "reports/approval_review_batches_current.json",
        "approval_review_batch_manifest_sha256": "b" * 64,
        "approval_review_batch_id": "approval-batch-001",
        "approval_review_batch_chunk_fingerprint": "c" * 64,
        "approval_review_strategy": "human_bulk_review",
    }
    return {
        "schema_version": VECTOR_RECORD_SCHEMA_VERSION,
        "id": record_id,
        "document_id": "doc",
        "tenant_id": "tenant-a",
        "chunk_id": metadata["chunk_id"],
        "text": text,
        "metadata": metadata,
        "content_hash": stable_content_hash(text, metadata),
    }


if __name__ == "__main__":
    unittest.main()
