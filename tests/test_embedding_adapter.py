from __future__ import annotations

import unittest
from unittest.mock import patch

from app.agents.model_router import QWEN3_EMBEDDING_MODEL
from app.ingestion.embedding_adapter import (
    EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION,
    LOCAL_HASH_EMBEDDING_MODEL,
    embed_vector_record,
    embed_vector_records,
    local_hash_embedding,
)
from app.ingestion.vector_adapter import VECTOR_RECORD_SCHEMA_VERSION, stable_content_hash


class EmbeddingAdapterTests(unittest.TestCase):
    def test_local_hash_embedding_is_deterministic_and_normalized(self) -> None:
        first = local_hash_embedding("??0議??덉궛 吏묓뻾 湲곗?", dimensions=16)
        second = local_hash_embedding("??0議??덉궛 吏묓뻾 湲곗?", dimensions=16)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)
        self.assertAlmostEqual(sum(value * value for value in first), 1.0, places=6)

    def test_embed_vector_record_adds_embedding_contract_fields(self) -> None:
        record = _record("doc:chunk-1", "??議?紐⑹쟻")

        embedded = embed_vector_record(record, dimensions=8)

        self.assertEqual(embedded["schema_version"], EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION)
        self.assertEqual(embedded["source_schema_version"], VECTOR_RECORD_SCHEMA_VERSION)
        self.assertEqual(embedded["embedding_model"], LOCAL_HASH_EMBEDDING_MODEL)
        self.assertEqual(embedded["embedding_dimensions"], 8)
        self.assertEqual(len(embedded["embedding"]), 8)
        self.assertTrue(embedded["embedding_hash"])
        self.assertTrue(embedded["content_hash"])

    def test_embed_vector_record_rejects_invalid_dimensions(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 1 and 4096"):
            embed_vector_record(_record("doc:chunk-1", "text"), dimensions=0)
        with self.assertRaisesRegex(ValueError, "between 1 and 4096"):
            embed_vector_record(_record("doc:chunk-1", "text"), dimensions=4097)
        with self.assertRaisesRegex(ValueError, "between 1 and 4096"):
            local_hash_embedding("text", dimensions=True)

    def test_embed_vector_records_summary_flags_duplicates_and_path_leaks(self) -> None:
        first = _record("same", "text")
        second = _record("same", "text")
        second["metadata"]["source_file"] = r"C:\Users\example\secret.pdf"

        _embedded, summary = embed_vector_records([first, second], dimensions=8)

        self.assertEqual(summary["record_count"], 2)
        self.assertEqual(summary["duplicate_id_count"], 1)
        self.assertEqual(summary["local_path_leak_count"], 1)

    def test_embed_vector_records_batches_qwen_documents_once_and_preserves_records(self) -> None:
        first = _record("doc:chunk-1", " first text ")
        second = _record("doc:chunk-2", "second text")
        adapter = _RecordingEmbeddingAdapter([[1.0, 0.0], [0.0, 1.0]])

        with patch(
            "app.ingestion.embedding_adapter._qwen_embedding_adapter",
            return_value=adapter,
        ) as adapter_factory:
            embedded, summary = embed_vector_records(
                [first, second],
                dimensions=2,
                model=QWEN3_EMBEDDING_MODEL,
            )

        adapter_factory.assert_called_once_with(2)
        self.assertEqual([["first text", "second text"]], adapter.calls)
        self.assertEqual(["doc:chunk-1", "doc:chunk-2"], [item["id"] for item in embedded])
        self.assertEqual([[1.0, 0.0], [0.0, 1.0]], [item["embedding"] for item in embedded])
        self.assertIs(first["metadata"], embedded[0]["metadata"])
        self.assertIs(second["metadata"], embedded[1]["metadata"])
        self.assertTrue(all(item["embedding_semantic"] for item in embedded))
        self.assertEqual(2, summary["record_count"])
        self.assertEqual(0, summary["invalid_embedding_dimension_count"])

    def test_embed_vector_records_rejects_qwen_result_count_mismatch(self) -> None:
        adapter = _RecordingEmbeddingAdapter([[1.0, 0.0]])

        with patch(
            "app.ingestion.embedding_adapter._qwen_embedding_adapter",
            return_value=adapter,
        ):
            with self.assertRaisesRegex(ValueError, "1 embeddings for 2 vector records"):
                embed_vector_records(
                    [_record("doc:chunk-1", "first"), _record("doc:chunk-2", "second")],
                    dimensions=2,
                    model=QWEN3_EMBEDDING_MODEL,
                )

        self.assertEqual([["first", "second"]], adapter.calls)

    def test_embed_vector_records_skips_qwen_adapter_for_empty_input(self) -> None:
        with patch("app.ingestion.embedding_adapter._qwen_embedding_adapter") as adapter_factory:
            embedded, summary = embed_vector_records([], model=QWEN3_EMBEDDING_MODEL)

        adapter_factory.assert_not_called()
        self.assertEqual([], embedded)
        self.assertEqual(0, summary["record_count"])


class _RecordingEmbeddingAdapter:
    def __init__(self, embeddings: list[list[float]]) -> None:
        self.embeddings = embeddings
        self.calls: list[list[str]] = []

    def encode_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return self.embeddings


def _record(record_id: str, text: str) -> dict:
    metadata = {
        "document_id": "doc",
        "tenant_id": "tenant-a",
        "chunk_id": record_id.rsplit(":", 1)[-1],
        "profile_id": "public_portal",
        "approval_status": "approved",
        "approval_id": f"approval-{record_id.rsplit(':', 1)[-1]}",
        "security_level": "internal",
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
