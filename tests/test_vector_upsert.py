from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.ingestion.embedding_adapter import embed_vector_record, local_hash_embedding, stable_embedding_hash
from app.ingestion.vector_adapter import (
    VECTOR_RECORD_SCHEMA_VERSION,
    VECTOR_RECORD_VERIFICATION_VERSION,
    stable_content_hash,
)
from app.ingestion.vector_upsert import (
    ChromaLocalJsonlTarget,
    LocalJsonlVectorTarget,
    PgvectorLocalJsonlTarget,
    QdrantLocalJsonlTarget,
    QdrantRestManifestTarget,
    chroma_row_from_record,
    load_vector_records_jsonl,
    pgvector_row_from_record,
    qdrant_point_from_record,
    validate_vector_record_tenant_scope,
    vector_upsert_target,
)
from app.retrieval.bm25_index import load_bm25_index


class VectorUpsertTests(unittest.TestCase):
    def test_tenant_scope_rejects_mixed_records(self) -> None:
        first = _record("doc:chunk-1", "text")
        second = _record("doc:chunk-2", "text")
        second["tenant_id"] = "tenant-b"
        second["metadata"]["tenant_id"] = "tenant-b"

        with self.assertRaisesRegex(ValueError, "multiple tenant scopes"):
            validate_vector_record_tenant_scope([first, second])

    def test_tenant_scope_rejects_expected_tenant_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match expected tenant"):
            validate_vector_record_tenant_scope([_record("doc:chunk-1", "text")], expected_tenant_id="tenant-b")

    def test_tenant_scope_rejects_record_metadata_mismatch(self) -> None:
        record = _record("doc:chunk-1", "text")
        record["tenant_id"] = "tenant-b"

        with self.assertRaisesRegex(ValueError, "inconsistent tenant_id"):
            validate_vector_record_tenant_scope([record])

    def test_local_jsonl_target_inserts_updates_and_skips_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "store.jsonl"
            target = LocalJsonlVectorTarget(target_path)

            first = _record("doc:chunk-1", "text v1")
            second = _record("doc:chunk-2", "text v1")
            first_result = target.upsert([first, second])
            second_result = target.upsert([first, _record("doc:chunk-2", "text v2")])
            stored = load_vector_records_jsonl(target_path)

        self.assertEqual(first_result["inserted_count"], 2)
        self.assertEqual(second_result["unchanged_count"], 1)
        self.assertEqual(second_result["updated_count"], 1)
        self.assertEqual(len(stored), 2)
        self.assertEqual(stored[1]["text"], "text v2")
        self.assertEqual(stored[0]["verification_version"], VECTOR_RECORD_VERIFICATION_VERSION)
        self.assertEqual(len(stored[0]["verification_hash"]), 64)
        self.assertEqual(second_result["verification_record_count"], 2)

    def test_local_jsonl_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "store.jsonl"

            result = LocalJsonlVectorTarget(target_path).upsert([_record("doc:chunk-1", "text")], dry_run=True)

        self.assertEqual(result["inserted_count"], 1)
        self.assertFalse(target_path.exists())

    def test_local_jsonl_accepts_embedded_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "store.jsonl"
            embedded = embed_vector_record(_record("doc:chunk-1", "text"), dimensions=8)

            result = LocalJsonlVectorTarget(target_path).upsert([embedded])
            stored = load_vector_records_jsonl(target_path)
            bm25_index = load_bm25_index(target_path.parent / "bm25_index.json")

        self.assertEqual(result["inserted_count"], 1)
        self.assertTrue(result["bm25_index_written"])
        self.assertEqual(result["schema_versions"], [embedded["schema_version"]])
        self.assertEqual(stored[0]["embedding_dimensions"], 8)
        self.assertIsNotNone(bm25_index)
        self.assertEqual(1, bm25_index.document_count if bm25_index else 0)

    def test_local_jsonl_rejects_malformed_embedded_records(self) -> None:
        embedded = embed_vector_record(_record("doc:chunk-1", "text"), dimensions=8)
        embedded["embedding_dimensions"] = 7

        with tempfile.TemporaryDirectory() as tmp:
            target = LocalJsonlVectorTarget(Path(tmp) / "store.jsonl")
            with self.assertRaisesRegex(ValueError, "embedding_dimensions"):
                target.upsert([embedded])

    def test_local_jsonl_rejects_non_finite_boolean_and_oversized_embeddings(self) -> None:
        invalid_records: list[tuple[str, object, str]] = [
            ("nan", float("nan"), "embedding_non_finite"),
            ("positive-infinity", float("inf"), "embedding_non_finite"),
            ("negative-infinity", float("-inf"), "embedding_non_finite"),
            ("boolean", True, "only numbers"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            target = LocalJsonlVectorTarget(Path(tmp) / "store.jsonl")
            for label, invalid_value, expected_error in invalid_records:
                with self.subTest(label=label):
                    embedded = embed_vector_record(_record(f"doc:{label}", "text"), dimensions=8)
                    embedded["embedding_model"] = "external-test-model"
                    embedded["embedding"][0] = invalid_value
                    embedded["embedding_hash"] = stable_embedding_hash(embedded["embedding"])
                    with self.assertRaisesRegex(ValueError, expected_error):
                        target.upsert([embedded])

            oversized = embed_vector_record(_record("doc:oversized", "text"), dimensions=8)
            oversized["embedding_model"] = "external-test-model"
            oversized["embedding"] = [0.0] * 4097
            oversized["embedding_dimensions"] = 4097
            oversized["embedding_hash"] = stable_embedding_hash(oversized["embedding"])
            with self.assertRaisesRegex(ValueError, "between 1 and 4096"):
                target.upsert([oversized])

    def test_local_jsonl_rejects_tampered_embedded_vectors(self) -> None:
        hash_mismatch = embed_vector_record(_record("doc:chunk-1", "text"), dimensions=8)
        hash_mismatch["embedding"][0] = float(hash_mismatch["embedding"][0]) + 0.25

        vector_mismatch = embed_vector_record(_record("doc:chunk-2", "text"), dimensions=8)
        vector_mismatch["embedding"] = local_hash_embedding("different text", dimensions=8)
        vector_mismatch["embedding_hash"] = stable_embedding_hash(vector_mismatch["embedding"])

        with tempfile.TemporaryDirectory() as tmp:
            target = LocalJsonlVectorTarget(Path(tmp) / "store.jsonl")
            with self.assertRaisesRegex(ValueError, "embedding_hash_mismatch"):
                target.upsert([hash_mismatch])
            with self.assertRaisesRegex(ValueError, "embedding_vector_mismatch"):
                target.upsert([vector_mismatch])

    def test_upsert_rejects_invalid_existing_verification_hash(self) -> None:
        record = _record("doc:chunk-1", "text")
        record["verification_version"] = VECTOR_RECORD_VERIFICATION_VERSION
        record["verification_hash"] = "0" * 64

        with tempfile.TemporaryDirectory() as tmp:
            target = LocalJsonlVectorTarget(Path(tmp) / "store.jsonl")
            with self.assertRaisesRegex(ValueError, "invalid verification_hash"):
                target.upsert([record])

    def test_upsert_rejects_duplicate_input_ids_and_local_path_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = LocalJsonlVectorTarget(Path(tmp) / "store.jsonl")
            with self.assertRaisesRegex(ValueError, "duplicate record ids"):
                target.upsert([_record("same", "a"), _record("same", "b")])
            with self.assertRaisesRegex(ValueError, "local path leaks"):
                record = _record("doc:chunk-1", "text")
                record["metadata"]["source_file"] = "C:" + "\\Users" + "\\dd" + "\\secret.pdf"
                target.upsert([record])
            with self.assertRaisesRegex(ValueError, "local path leaks"):
                record = _record("doc:chunk-2", "text")
                record["metadata"]["source_file"] = "/usr/src/app/secret.pdf"
                target.upsert([record])

    def test_upsert_rejects_approved_record_without_approved_content_hash(self) -> None:
        record = _record("doc:chunk-1", "text")
        record["metadata"].pop("approved_content_hash")
        record["content_hash"] = stable_content_hash(record["text"], record["metadata"])

        with tempfile.TemporaryDirectory() as tmp:
            target = LocalJsonlVectorTarget(Path(tmp) / "store.jsonl")
            with self.assertRaisesRegex(ValueError, "approved_content_hash"):
                target.upsert([record])

    def test_upsert_rejects_approved_record_without_approval_provenance(self) -> None:
        record = _record("doc:chunk-1", "text")
        record["metadata"].pop("approval_review_batch_chunk_fingerprint")
        record["content_hash"] = stable_content_hash(record["text"], record["metadata"])

        with tempfile.TemporaryDirectory() as tmp:
            target = LocalJsonlVectorTarget(Path(tmp) / "store.jsonl")
            with self.assertRaisesRegex(ValueError, "approval provenance"):
                target.upsert([record])

    def test_upsert_rejects_unreviewed_preview_record(self) -> None:
        record = _record("doc:chunk-1", "text")
        record["metadata"]["approval_status"] = "UNREVIEWED_PREVIEW"
        record["content_hash"] = stable_content_hash(record["text"], record["metadata"])

        with tempfile.TemporaryDirectory() as tmp:
            target = LocalJsonlVectorTarget(Path(tmp) / "store.jsonl")
            with self.assertRaisesRegex(ValueError, "not approved for indexing"):
                target.upsert([record])

    def test_factory_rejects_unknown_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(vector_upsert_target("local-jsonl", target_path=Path(tmp) / "store.jsonl").target_type, "local-jsonl")
            self.assertEqual(
                vector_upsert_target("qdrant-local-jsonl", target_path=Path(tmp) / "qdrant.jsonl").target_type,
                "qdrant-local-jsonl",
            )
            self.assertEqual(
                vector_upsert_target("pgvector-local-jsonl", target_path=Path(tmp) / "pg.jsonl").target_type,
                "pgvector-local-jsonl",
            )
            self.assertEqual(
                vector_upsert_target("chroma-local-jsonl", target_path=Path(tmp) / "chroma.jsonl").target_type,
                "chroma-local-jsonl",
            )
            with self.assertRaisesRegex(ValueError, "Unsupported vector upsert"):
                vector_upsert_target("opensearch", target_path=Path(tmp) / "unused")
            with self.assertRaisesRegex(ValueError, "live network upsert is blocked"):
                vector_upsert_target("qdrant-rest", target_path=Path(tmp) / "unused")

    def test_qdrant_local_jsonl_exports_points_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "qdrant.jsonl"
            target = QdrantLocalJsonlTarget(target_path)
            embedded = embed_vector_record(_record("doc:chunk-1", "text"), dimensions=8)

            result = target.upsert([embedded])
            lines = target_path.read_text(encoding="utf-8").strip().splitlines()

        self.assertEqual(result["inserted_count"], 1)
        self.assertEqual(result["api_call_count"], 0)
        self.assertEqual(result["mode"], "local_export_only")
        self.assertEqual(len(lines), 1)
        point = json.loads(lines[0])
        self.assertEqual(point["id"], "doc:chunk-1")
        self.assertEqual(len(point["vector"]), 8)
        self.assertEqual(point["payload"]["text"], "text")
        self.assertEqual(point["payload"]["verification_version"], VECTOR_RECORD_VERIFICATION_VERSION)
        self.assertEqual(len(point["payload"]["verification_hash"]), 64)

    def test_qdrant_local_jsonl_requires_embedded_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = QdrantLocalJsonlTarget(Path(tmp) / "qdrant.jsonl")
            with self.assertRaisesRegex(ValueError, "requires embedded vector records"):
                target.upsert([_record("doc:chunk-1", "text")])

    def test_qdrant_point_from_record_merges_metadata(self) -> None:
        record = embed_vector_record(_record("doc:chunk-1", "text"), dimensions=4)
        record["metadata"]["profile_id"] = "public_portal-etc-law"
        point = qdrant_point_from_record(record)
        self.assertEqual(point["payload"]["profile_id"], "public_portal-etc-law")
        self.assertEqual(point["payload"]["embedding_model"], "local-hash-embedding-v1")

    def test_pgvector_local_jsonl_exports_rows_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "pg.jsonl"
            embedded = embed_vector_record(_record("doc:chunk-1", "text"), dimensions=8)

            result = PgvectorLocalJsonlTarget(target_path).upsert([embedded])
            row = json.loads(target_path.read_text(encoding="utf-8").strip())

        self.assertEqual(result["inserted_count"], 1)
        self.assertEqual(result["api_call_count"], 0)
        self.assertEqual(row["content"], "text")
        self.assertEqual(len(row["embedding"]), 8)
        self.assertEqual(row["metadata"]["verification_version"], VECTOR_RECORD_VERIFICATION_VERSION)
        self.assertEqual(len(row["metadata"]["verification_hash"]), 64)

    def test_pgvector_row_from_record_merges_metadata(self) -> None:
        record = embed_vector_record(_record("doc:chunk-1", "text"), dimensions=4)
        record["metadata"]["profile_id"] = "public_portal-etc-law"
        row = pgvector_row_from_record(record)
        self.assertEqual(row["metadata"]["profile_id"], "public_portal-etc-law")
        self.assertEqual(row["embedding_dimensions"], 4)

    def test_chroma_local_jsonl_exports_rows_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "chroma.jsonl"
            embedded = embed_vector_record(_record("doc:chunk-1", "text"), dimensions=8)

            result = ChromaLocalJsonlTarget(target_path).upsert([embedded])
            row = json.loads(target_path.read_text(encoding="utf-8").strip())

        self.assertEqual(result["inserted_count"], 1)
        self.assertEqual(result["api_call_count"], 0)
        self.assertEqual(row["document"], "text")
        self.assertEqual(len(row["embedding"]), 8)
        self.assertEqual(row["metadata"]["verification_version"], VECTOR_RECORD_VERIFICATION_VERSION)
        self.assertEqual(len(row["metadata"]["verification_hash"]), 64)

    def test_chroma_row_from_record_merges_metadata(self) -> None:
        record = embed_vector_record(_record("doc:chunk-1", "text"), dimensions=4)
        record["metadata"]["profile_id"] = "public_portal-etc-law"
        row = chroma_row_from_record(record)
        self.assertEqual(row["metadata"]["profile_id"], "public_portal-etc-law")

    def test_qdrant_rest_manifest_plans_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "qdrant_rest.manifest.json"
            embedded = embed_vector_record(_record("doc:chunk-1", "text"), dimensions=8)

            result = QdrantRestManifestTarget(manifest_path, collection_name="demo").upsert([embedded])

            self.assertEqual(result["api_call_count"], 0)
            self.assertTrue(result["live_network_blocked"])
            self.assertEqual(result["planned_upsert_count"], 1)
            self.assertTrue(manifest_path.is_file())


def _record(record_id: str, text: str) -> dict:
    metadata = {
        "document_id": "doc",
        "tenant_id": "tenant-a",
        "chunk_id": record_id.rsplit(":", 1)[-1],
        "profile_id": "public_portal",
        "approval_status": "approved",
        "approval_id": f"approval-{record_id.rsplit(':', 1)[-1]}",
        "approved_content_hash": f"approved-hash-{record_id.rsplit(':', 1)[-1]}",
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
