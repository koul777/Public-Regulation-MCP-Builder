from __future__ import annotations

import hashlib
import math
import unittest
from unittest.mock import patch

from app.agents.model_router import QWEN3_EMBEDDING_MODEL
from app.ingestion import embedding_adapter
from app.ingestion.embedding_adapter import (
    EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION,
    LOCAL_HASH_EMBEDDING_MODEL,
    embed_vector_record,
    embed_vector_records,
    local_hash_embedding,
    stable_embedding_hash,
)
from app.ingestion.vector_adapter import VECTOR_RECORD_SCHEMA_VERSION, stable_content_hash
from app.ingestion.vector_integrity import embedded_vector_integrity_reason


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


class LocalHashEmbeddingTokenCacheTests(unittest.TestCase):
    REPEATED_TEXT = "제1조(목적) 이 규정은 예산 집행 기준을 정한다. 제2조(적용) 이 규정은 예산 집행에 적용한다. 예산 예산 예산"

    def setUp(self) -> None:
        embedding_adapter._cached_token_bucket.cache_clear()

    def tearDown(self) -> None:
        embedding_adapter._cached_token_bucket.cache_clear()

    def test_repeated_tokens_match_cache_cleared_and_uncached_computation(self) -> None:
        for dimensions in (1, 8, 384, 4096):
            with self.subTest(dimensions=dimensions):
                embedding_adapter._cached_token_bucket.cache_clear()
                cold = local_hash_embedding(self.REPEATED_TEXT, dimensions=dimensions)
                warm = local_hash_embedding(self.REPEATED_TEXT, dimensions=dimensions)
                embedding_adapter._cached_token_bucket.cache_clear()
                recleared = local_hash_embedding(self.REPEATED_TEXT, dimensions=dimensions)

                self.assertEqual(_uncached_reference_embedding(self.REPEATED_TEXT, dimensions), cold)
                self.assertEqual(cold, warm)
                self.assertEqual(cold, recleared)

        info = embedding_adapter._cached_token_bucket.cache_info()
        self.assertGreater(info.hits, 0)

    def test_different_dimensions_do_not_share_cache_entries(self) -> None:
        local_hash_embedding("예산", dimensions=8)
        after_first = embedding_adapter._cached_token_bucket.cache_info()
        local_hash_embedding("예산", dimensions=16)
        after_second = embedding_adapter._cached_token_bucket.cache_info()

        self.assertEqual(1, after_first.currsize)
        self.assertEqual(2, after_second.currsize)
        self.assertEqual(0, after_second.hits)
        self.assertEqual(_uncached_reference_embedding("예산", 16), local_hash_embedding("예산", dimensions=16))
        self.assertEqual(_uncached_reference_embedding("예산", 8), local_hash_embedding("예산", dimensions=8))

    def test_token_cache_is_bounded_and_skips_long_tokens(self) -> None:
        self.assertEqual(65_536, embedding_adapter._cached_token_bucket.cache_info().maxsize)
        long_token = "가" * (embedding_adapter._TOKEN_BUCKET_CACHE_MAX_CHARS + 1)
        punctuation_only = "!!! ... ---"

        self.assertEqual(_uncached_reference_embedding(long_token, 8), local_hash_embedding(long_token, dimensions=8))
        self.assertEqual(
            _uncached_reference_embedding(punctuation_only, 8),
            local_hash_embedding(punctuation_only, dimensions=8),
        )
        self.assertEqual(1, embedding_adapter._cached_token_bucket.cache_info().currsize)

    def test_tampered_rows_are_still_rejected_with_a_warm_token_cache(self) -> None:
        clean = embed_vector_record(_record("doc:chunk-1", self.REPEATED_TEXT), dimensions=8)
        self.assertEqual("", embedded_vector_integrity_reason(clean))
        self.assertGreater(embedding_adapter._cached_token_bucket.cache_info().currsize, 0)

        text_changed = dict(clean)
        text_changed["text"] = self.REPEATED_TEXT.replace("집행 기준", "집행 예외")
        swapped_cached_vocabulary = dict(clean)
        swapped_cached_vocabulary["text"] = "예산 예산 예산 제1조 목적 규정"
        embedding_replaced = dict(clean)
        embedding_replaced["embedding"] = local_hash_embedding("예산 예산", dimensions=8)
        embedding_replaced["embedding_hash"] = stable_embedding_hash(embedding_replaced["embedding"])

        self.assertEqual("embedding_vector_mismatch", embedded_vector_integrity_reason(text_changed))
        self.assertEqual("embedding_vector_mismatch", embedded_vector_integrity_reason(swapped_cached_vocabulary))
        self.assertEqual("embedding_vector_mismatch", embedded_vector_integrity_reason(embedding_replaced))
        self.assertEqual("", embedded_vector_integrity_reason(clean))


def _uncached_reference_embedding(text: str, dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    tokens = embedding_adapter._tokens(text) or [text]
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        vector[int.from_bytes(digest[:4], "big") % dimensions] += -1.0 if digest[4] & 1 else 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    if norm:
        vector = [round(value / norm, 8) for value in vector]
    return vector


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


def _reference_local_hash_embedding(text: str, *, dimensions: int = 384) -> list[float]:
    """Verbatim copy of the dense algorithm local_hash_embedding used before it went sparse."""

    vector = [0.0] * dimensions
    tokens = embedding_adapter._tokens(text)
    if not tokens:
        tokens = [text]
    for token in tokens:
        index, sign = embedding_adapter._token_bucket(token, dimensions)
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm:
        vector = [round(value / norm, 8) for value in vector]
    return vector


class LocalHashEmbeddingEquivalenceTests(unittest.TestCase):
    WORDS = ["제1조", "목적", "휴직", "절차", "신청", "budget", "Rule", "x", "a1", "인사규정", "지급", "수당", "of", "the"]

    def test_sparse_embedding_is_bit_identical_to_the_dense_algorithm(self) -> None:
        import random

        rng = random.Random(20260110)
        compared = 0
        for _ in range(400):
            words = [rng.choice(self.WORDS) for _ in range(rng.randint(0, 60))]
            text = " ".join(words)
            if rng.random() < 0.2:
                text += " " + "가" * rng.choice([5, 64, 65, 200])
            for dimensions in (1, 2, 3, 8, 16, 384, 1000, 4096):
                with self.subTest(text=text[:30], dimensions=dimensions):
                    self.assertEqual(
                        _reference_local_hash_embedding(text, dimensions=dimensions),
                        local_hash_embedding(text, dimensions=dimensions),
                    )
                compared += 1
        self.assertEqual(3200, compared)

    def test_texts_without_word_characters_and_cancelling_buckets_match(self) -> None:
        for text in ("", "   ", "!!! ??? ...", "---", "\n", "한", "x" * 500):
            for dimensions in (1, 4, 384):
                self.assertEqual(
                    _reference_local_hash_embedding(text, dimensions=dimensions),
                    local_hash_embedding(text, dimensions=dimensions),
                )
        # one dimension: opposite-sign tokens cancel to an all-zero vector (norm == 0)
        positive = next(w for w in ("a", "b", "c", "d", "e", "f") if embedding_adapter._token_bucket(w, 1)[1] > 0)
        negative = next(w for w in ("a", "b", "c", "d", "e", "f", "g", "h") if embedding_adapter._token_bucket(w, 1)[1] < 0)
        cancelled = f"{positive} {negative}"
        self.assertEqual([0.0], local_hash_embedding(cancelled, dimensions=1))
        self.assertEqual(
            _reference_local_hash_embedding(cancelled, dimensions=1),
            local_hash_embedding(cancelled, dimensions=1),
        )

    def test_result_is_a_fresh_list_of_floats(self) -> None:
        first = local_hash_embedding("제1조 목적", dimensions=32)
        first[0] = 99.0
        second = local_hash_embedding("제1조 목적", dimensions=32)
        self.assertNotEqual(99.0, second[0])
        self.assertTrue(all(type(value) is float for value in second))

    def test_dimension_validation_is_unchanged(self) -> None:
        for dimensions in (0, -1, 4097, True, 3.5):
            with self.assertRaises(ValueError):
                local_hash_embedding("text", dimensions=dimensions)  # type: ignore[arg-type]

    def test_embed_vector_records_collects_garbage_normally_afterwards(self) -> None:
        import gc

        was_enabled = gc.isenabled()
        gc.enable()
        try:
            embedded, summary = embed_vector_records([_record("doc:chunk-1", "제1조 목적")], dimensions=8)
            self.assertTrue(gc.isenabled())
            self.assertEqual(1, summary["record_count"])
            self.assertEqual(1, len(embedded))
        finally:
            if not was_enabled:
                gc.disable()


if __name__ == "__main__":
    unittest.main()
