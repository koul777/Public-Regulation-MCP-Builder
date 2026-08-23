from __future__ import annotations

import unittest
from unittest.mock import patch

from app.agents.model_router import QWEN3_EMBEDDING_MODEL, QWEN3_RERANKER_MODEL
from app.ingestion.embedding_adapter import embed_vector_record
from app.ingestion.vector_adapter import VECTOR_RECORD_SCHEMA_VERSION
from app.retrieval.bm25_index import BM25_RETRIEVAL_MODEL, Bm25Index
from app.retrieval.searcher import search
from app.retrieval.semantic_models import Qwen3EmbeddingAdapter, Qwen3RerankerAdapter


class _EmbeddingModel:
    def __init__(self) -> None:
        self.inputs: list[list[str]] = []

    def encode(self, texts, **kwargs):
        self.inputs.append(list(texts))
        return [[3.0, 4.0] for _ in texts]


class _EmbeddingAdapter:
    def encode_documents(self, texts):
        return [[0.6, 0.8] for _ in texts]

    def encode_queries(self, texts):
        return [[1.0, 0.0] for _ in texts]


class SemanticModelTests(unittest.TestCase):
    def test_embedding_adapter_instructs_queries_but_not_documents(self) -> None:
        model = _EmbeddingModel()
        adapter = Qwen3EmbeddingAdapter(model=model, truncate_dim=128)

        documents = adapter.encode_documents(["제1조 목적"])
        queries = adapter.encode_queries(["목적 조문"])

        self.assertEqual([0.6, 0.8], documents[0])
        self.assertEqual([0.6, 0.8], queries[0])
        self.assertEqual("제1조 목적", model.inputs[0][0])
        self.assertIn("Instruct:", model.inputs[1][0])
        self.assertIn("Query: 목적 조문", model.inputs[1][0])

    @patch("app.ingestion.embedding_adapter._qwen_embedding_adapter", return_value=_EmbeddingAdapter())
    def test_vector_record_can_use_real_semantic_model_contract(self, adapter) -> None:
        record = {
            "schema_version": VECTOR_RECORD_SCHEMA_VERSION,
            "id": "doc:chunk",
            "document_id": "doc",
            "chunk_id": "chunk",
            "text": "제1조 목적",
            "metadata": {"approval_status": "approved"},
            "content_hash": "hash",
        }

        embedded = embed_vector_record(record, dimensions=2, model=QWEN3_EMBEDDING_MODEL)

        self.assertEqual(QWEN3_EMBEDDING_MODEL, embedded["embedding_model"])
        self.assertTrue(embedded["embedding_semantic"])
        self.assertEqual("sentence_transformers", embedded["embedding_runtime"])
        self.assertEqual([0.6, 0.8], embedded["embedding"])
        adapter.assert_called_once_with(2)

    @patch("app.retrieval.searcher.semantic_runtime_available", return_value=True)
    @patch("app.retrieval.searcher._semantic_query_adapter", return_value=_EmbeddingAdapter())
    def test_hybrid_search_uses_qwen_query_embedding_for_semantic_records(
        self,
        adapter,
        _available,
    ) -> None:
        records = [
            {
                "id": "doc:a",
                "document_id": "doc",
                "chunk_id": "a",
                "text": "접근 권한",
                "embedding": [1.0, 0.0],
                "embedding_model": QWEN3_EMBEDDING_MODEL,
                "metadata": {"approval_status": "approved", "article_no": "제1조"},
            },
            {
                "id": "doc:b",
                "document_id": "doc",
                "chunk_id": "b",
                "text": "출장 여비",
                "embedding": [0.0, 1.0],
                "embedding_model": QWEN3_EMBEDDING_MODEL,
                "metadata": {"approval_status": "approved", "article_no": "제2조"},
            },
        ]
        index = Bm25Index.build(records)

        scored, metadata = search("권한", records, index, top_k=2)

        self.assertEqual("hybrid-bm25-qwen3-v1", metadata["retrieval_model"])
        self.assertEqual(QWEN3_EMBEDDING_MODEL, metadata["semantic_embedding_model"])
        self.assertEqual("doc:a", scored[0][1]["id"])
        adapter.assert_called_once_with(2)

    @patch("app.retrieval.searcher._semantic_query_adapter")
    def test_fast_search_uses_bm25_without_loading_query_embedding(self, adapter) -> None:
        records = [
            {
                "id": "doc:a",
                "document_id": "doc",
                "chunk_id": "a",
                "text": "휴가 신청 절차",
                "embedding": [1.0, 0.0],
                "embedding_model": QWEN3_EMBEDDING_MODEL,
                "metadata": {"approval_status": "approved", "article_no": "제5조"},
            }
        ]
        index = Bm25Index.build(records)

        scored, metadata = search(
            "휴가 신청",
            records,
            index,
            top_k=1,
            prefer_semantic=False,
        )

        self.assertEqual("doc:a", scored[0][1]["id"])
        self.assertEqual(BM25_RETRIEVAL_MODEL, metadata["retrieval_model"])
        adapter.assert_not_called()

    @patch("app.retrieval.searcher.semantic_runtime_available", return_value=False)
    def test_qwen_semantic_records_fall_back_to_bm25_when_runtime_is_missing(self, _available) -> None:
        records = [
            {
                "id": "doc:a",
                "document_id": "doc",
                "chunk_id": "a",
                "text": "접근 권한 관리 절차",
                "embedding": [1.0, 0.0],
                "embedding_model": QWEN3_EMBEDDING_MODEL,
                "metadata": {"approval_status": "approved", "article_no": "제1조"},
            },
            {
                "id": "doc:b",
                "document_id": "doc",
                "chunk_id": "b",
                "text": "출장 여비 지급 절차",
                "embedding": [0.0, 1.0],
                "embedding_model": QWEN3_EMBEDDING_MODEL,
                "metadata": {"approval_status": "approved", "article_no": "제2조"},
            },
        ]
        index = Bm25Index.build(records)

        scored, metadata = search("접근 권한", records, index, top_k=2)

        self.assertEqual("doc:a", scored[0][1]["id"])
        self.assertEqual(BM25_RETRIEVAL_MODEL, metadata["retrieval_model"])
        self.assertTrue(metadata["retrieval_fallback"])
        self.assertEqual("ready_semantic_query_fallback", metadata["bm25_index_status"])
        self.assertEqual("semantic_runtime_unavailable", metadata["semantic_fallback_reason"])

    def test_reranker_keeps_original_score_and_adds_model_provenance(self) -> None:
        adapter = Qwen3RerankerAdapter(tokenizer=object(), model=object())
        candidates = [(0.8, {"id": "a", "text": "A"}), (0.9, {"id": "b", "text": "B"})]

        with patch.object(adapter, "score", return_value=[0.95, 0.1]):
            reranked = adapter.rerank("query", candidates, top_k=2)

        self.assertEqual("a", reranked[0][1]["id"])
        self.assertEqual(0.8, reranked[0][1]["retrieval_score"])
        self.assertEqual(QWEN3_RERANKER_MODEL, reranked[0][1]["reranker_model"])


if __name__ == "__main__":
    unittest.main()
