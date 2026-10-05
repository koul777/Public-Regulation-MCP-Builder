from __future__ import annotations

import unittest
from unittest.mock import patch

from app.ingestion.embedding_adapter import (
    LOCAL_HASH_EMBEDDING_MODEL,
    MAX_EMBEDDING_DIMENSIONS,
    local_hash_embedding,
)
from app.retrieval.searcher import rerank_fallback_candidates, rerank_structured_candidates
from app.retrieval import searcher


class StructuredCandidateRankingTests(unittest.TestCase):
    def test_supplementary_question_prefers_referenced_effective_date_over_body(self) -> None:
        records = [
            self.record("body", "article", "제7조의2 작업 기준", "제7조의2", "시행일"),
            self.record("other-date", "supplementary_provision", "제70조는 다음 날부터 시행한다."),
            self.record("target", "supplementary_provision", "제 7 조 의 2는 내년부터 시행한다."),
        ]
        scored = rerank_structured_candidates(
            "시설관리규정 부칙에서 제7조의2 관련 시행일은?",
            [(3.0, records[0]), (2.0, records[1]), (1.0, records[2])],
        )
        self.assertEqual("target", scored[0][1]["id"])
        self.assertEqual({record["id"] for record in records}, {record["id"] for _, record in scored})

    def test_body_question_does_not_promote_supplementary_reference(self) -> None:
        body = self.record("body", "article", "작업 기준", "제7조의2", "작업 기준")
        supplement = self.record("supplement", "supplementary_provision", "제7조의2 시행일")
        scored = rerank_structured_candidates(
            "시설관리규정 제7조의2 작업 기준", [(2.0, supplement), (1.0, body)]
        )
        self.assertEqual("body", scored[0][1]["id"])

    def test_supplementary_boost_respects_explicitly_named_regulation(self) -> None:
        named = self.record("named", "article", "규정의 적용 범위")
        other = self.record("other", "supplementary_provision", "내년부터 시행한다.")
        other["metadata"]["regulation_title"] = "회계관리규정"
        ranked = rerank_structured_candidates(
            "시설관리규정 부칙", [(2.0, other), (1.0, named)]
        )
        self.assertEqual("named", ranked[0][1]["id"])

    def test_empty_candidate_set_remains_empty(self) -> None:
        self.assertEqual([], rerank_structured_candidates("시설관리규정 부칙", []))

    @staticmethod
    def record(
        identifier: str,
        chunk_type: str,
        text: str,
        article_no: str = "",
        article_title: str = "",
    ) -> dict:
        return {
            "id": identifier,
            "text": text,
            "metadata": {
                "regulation_title": "시설관리규정",
                "chunk_type": chunk_type,
                "article_no": article_no,
                "article_title": article_title,
            },
        }


class FallbackCandidateRankingTests(unittest.TestCase):
    def tearDown(self) -> None:
        searcher._cached_sparse_hash_vector.cache_clear()

    def test_existing_local_vectors_resolve_lexical_tie_without_loading_models(self) -> None:
        target = self.record("target", "안전 점검 기록 보관")
        distractor = self.record("distractor", "식당 메뉴 식재료 주문")
        with (
            patch("app.retrieval.searcher.local_hash_embedding", wraps=local_hash_embedding) as encode,
            patch("app.retrieval.searcher._semantic_query_adapter", side_effect=AssertionError("model load")),
            patch("app.retrieval.tokenizer._kiwi", side_effect=AssertionError("Kiwi load")),
        ):
            ranked, mode = rerank_fallback_candidates(
                "안전 점검 기록 보관", [(1.01, distractor), (1.0, target)]
            )
        self.assertEqual("target", ranked[0][1]["id"])
        self.assertEqual("local_hash_structured_query", mode)
        self.assertEqual(1, encode.call_count)
        self.assertEqual({"target", "distractor"}, {r["id"] for _, r in ranked})
        self.assertIs(target, ranked[0][1])

    def test_generic_attachment_fallback_never_initializes_kiwi(self) -> None:
        query = "별표와 별지 서식 작성 방식"
        record = self.record("attachment", query)
        record["metadata"] = {"chunk_type": "appendix"}
        with patch("app.retrieval.tokenizer._kiwi", side_effect=AssertionError("Kiwi load")):
            ranked, mode = rerank_fallback_candidates(query, [(1.0, record)])
        self.assertEqual("attachment", ranked[0][1]["id"])
        self.assertEqual("local_hash_structured_query", mode)

    def test_exact_article_match_remains_stronger_than_vector_similarity(self) -> None:
        query = "시설관리규정 제7조 안전 점검"
        target = self.record("target", "점검 책임자의 서명을 보관한다.")
        target["metadata"] = {"regulation_title": "시설관리규정", "article_no": "제7조"}
        distractor = self.record("distractor", query)
        distractor["metadata"] = {"regulation_title": "시설관리규정", "article_no": "제70조"}
        ranked, _ = rerank_fallback_candidates(query, [(1.01, distractor), (1.0, target)])
        self.assertEqual("target", ranked[0][1]["id"])

    def test_unknown_models_and_invalid_vectors_keep_structural_fallback(self) -> None:
        vectors = [
            None, [], [float("nan")], [float("inf")], [True], ["1.0"],
            [10**1000], [2.0], [0.0], [1.0] * (MAX_EMBEDDING_DIMENSIONS + 1),
        ]
        for embedding in vectors:
            with self.subTest(embedding_type=type(embedding).__name__, size=len(embedding or [])):
                record = {"id": "invalid", "embedding_model": LOCAL_HASH_EMBEDDING_MODEL,
                          "embedding": embedding}
                with patch("app.retrieval.searcher.local_hash_embedding", side_effect=AssertionError("encode")):
                    ranked, mode = rerank_fallback_candidates("점검", [(2.0, record)])
                self.assertEqual([(2.0, record)], ranked)
                self.assertEqual("structured_query", mode)
        record = self.record("unknown", "점검")
        record["embedding_model"] = "unsupported-model"
        with patch("app.retrieval.searcher.local_hash_embedding", side_effect=AssertionError("encode")):
            ranked, mode = rerank_fallback_candidates("점검", [(2.0, record)])
        self.assertEqual([(2.0, record)], ranked)
        self.assertEqual("structured_query", mode)

    def test_empty_scope_stays_empty_and_mixed_dimensions_are_supported(self) -> None:
        self.assertEqual(([], "structured_query"), rerank_fallback_candidates("점검", []))
        records = [
            self.record("small", "점검", 64),
            self.record("normal", "점검", 384),
            self.record("uncached", "점검", 2048),
        ]
        ranked, mode = rerank_fallback_candidates("점검", [(1.0, r) for r in records])
        self.assertEqual("local_hash_structured_query", mode)
        self.assertEqual({r["id"] for r in records}, {r["id"] for _, r in ranked})
        self.assertTrue(all(1.99 < score <= 2.0 for score, _ in ranked))

    def test_numeric_cache_uses_actual_values_and_rejects_boolean_alias(self) -> None:
        embedding = [1.0, 0.0]
        self.assertEqual(((0, 1.0),), searcher._local_hash_sparse_vector(embedding))
        embedding[:] = [0.0, 1.0]
        self.assertEqual(((1, 1.0),), searcher._local_hash_sparse_vector(embedding))
        self.assertIsNone(searcher._local_hash_sparse_vector([True, 0.0]))
        self.assertIsNone(searcher._local_hash_sparse_vector([0.5, 0.0]))
        self.assertEqual(((0, 1.0),), searcher._local_hash_sparse_vector([1.0, 0.0]))

    @staticmethod
    def record(identifier: str, text: str, dimensions: int = 384) -> dict:
        return {
            "id": identifier,
            "text": text,
            "embedding_model": LOCAL_HASH_EMBEDDING_MODEL,
            "embedding": local_hash_embedding(text, dimensions=dimensions),
        }


if __name__ == "__main__":
    unittest.main()
