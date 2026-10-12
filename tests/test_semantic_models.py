from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

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
    def test_long_appendix_pools_all_windows_and_preserves_document_order(self) -> None:
        texts = ["가" * 2048 + "나" * 1024, "제1조 목적"]
        model = SimpleNamespace(device="cuda:0", encode=Mock(return_value=[[1.0, 0.0], [0.0, 1.0], [0.0, 1.0]]))
        result = Qwen3EmbeddingAdapter(model=model).encode_documents(texts)
        inputs = model.encode.call_args.args[0]
        self.assertEqual(texts[0], "".join(inputs[:2]))
        self.assertEqual(texts[1], inputs[2])
        self.assertLessEqual(max(map(len, inputs)), 2048)
        self.assertAlmostEqual(2 / (5 ** .5), result[0][0])
        self.assertAlmostEqual(1 / (5 ** .5), result[0][1])
        self.assertEqual([0.0, 1.0], result[1])

    def test_missing_window_vectors_fail_instead_of_dropping_text(self) -> None:
        model = SimpleNamespace(encode=Mock(return_value=[[1.0, 0.0]]))
        with self.assertRaisesRegex(ValueError, "number of window"):
            Qwen3EmbeddingAdapter(model=model).encode_documents(["가" * 3000])

    def test_non_finite_window_vector_is_rejected(self) -> None:
        model = SimpleNamespace(encode=Mock(return_value=[[float("nan"), 1.0]]))
        with self.assertRaisesRegex(ValueError, "non-finite"):
            Qwen3EmbeddingAdapter(model=model).encode_documents(["제1조 목적"])

    def test_cuda_memory_retry_preserves_every_input_in_order(self) -> None:
        texts = ["긴 별표", "제1조 목적", "제2조 적용"]
        model = SimpleNamespace(device="cuda:0", encode=Mock(side_effect=[
            RuntimeError("CUDA out of memory"), RuntimeError("CUDA out of memory"),
            [[3.0, 4.0], [0.0, 5.0], [5.0, 0.0]],
        ]))
        empty_cache = Mock()
        with patch.dict("sys.modules", {"torch": SimpleNamespace(cuda=SimpleNamespace(empty_cache=empty_cache))}):
            result = Qwen3EmbeddingAdapter(device="auto", model=model).encode_documents(texts)
        self.assertEqual([[0.6, 0.8], [0.0, 1.0], [1.0, 0.0]], result)
        self.assertEqual([8, 4, 2], [call.kwargs["batch_size"] for call in model.encode.call_args_list])
        self.assertTrue(all(call.args[0] == texts for call in model.encode.call_args_list))
        self.assertEqual(2, empty_cache.call_count)

    def test_single_item_cuda_memory_failure_is_not_hidden(self) -> None:
        model = SimpleNamespace(device="cuda:0", encode=Mock(side_effect=RuntimeError("CUDA out of memory")))
        with self.assertRaisesRegex(RuntimeError, "out of memory"):
            Qwen3EmbeddingAdapter(model=model).encode_documents(["긴 별표"], batch_size=1)
        model.encode.assert_called_once()

    def test_unrelated_cuda_error_is_not_retried(self) -> None:
        model = SimpleNamespace(device="cuda:0", encode=Mock(side_effect=RuntimeError("invalid device function")))
        with self.assertRaisesRegex(RuntimeError, "invalid device function"):
            Qwen3EmbeddingAdapter(model=model).encode_documents(["제1조 목적"])
        model.encode.assert_called_once()

    def test_auto_embedding_uses_cuda_half_precision_when_available(self) -> None:
        factory = Mock(return_value=_EmbeddingModel())
        torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True))
        with patch.dict("sys.modules", {"torch": torch, "sentence_transformers": SimpleNamespace(SentenceTransformer=factory)}), \
             patch("app.retrieval.semantic_models.semantic_runtime_available", return_value=True):
            adapter = Qwen3EmbeddingAdapter(device="auto", truncate_dim=384, local_files_only=True)
            self.assertEqual([[0.6, 0.8]], adapter.encode_documents(["제1조 목적"]))
        self.assertEqual("cuda", factory.call_args.kwargs["device"])
        self.assertEqual({"torch_dtype": "float16"}, factory.call_args.kwargs["model_kwargs"])
        self.assertTrue(factory.call_args.kwargs["local_files_only"])

    def test_auto_embedding_keeps_cpu_without_cuda(self) -> None:
        factory = Mock(return_value=_EmbeddingModel())
        torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
        with patch.dict("sys.modules", {"torch": torch, "sentence_transformers": SimpleNamespace(SentenceTransformer=factory)}), \
             patch("app.retrieval.semantic_models.semantic_runtime_available", return_value=True):
            Qwen3EmbeddingAdapter(device="auto").encode_documents(["제1조 목적"])
        self.assertEqual("cpu", factory.call_args.kwargs["device"])
        self.assertNotIn("model_kwargs", factory.call_args.kwargs)

    def test_explicit_cpu_embedding_does_not_select_cuda(self) -> None:
        factory = Mock(return_value=_EmbeddingModel())
        with patch.dict("sys.modules", {"sentence_transformers": SimpleNamespace(SentenceTransformer=factory)}), \
             patch("app.retrieval.semantic_models.semantic_runtime_available", return_value=True):
            Qwen3EmbeddingAdapter(device="cpu").encode_queries(["목적"])
        self.assertEqual("cpu", factory.call_args.kwargs["device"])
        self.assertNotIn("model_kwargs", factory.call_args.kwargs)

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
