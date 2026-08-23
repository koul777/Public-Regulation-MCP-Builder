from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.agents.query_agents import (
    QueryAnalysisAgent,
    QueryRewriteAgent,
    deterministic_query_analysis,
    deterministic_query_rewrite,
)


class _Runtime:
    def __init__(self, payload: dict | None = None, *, available: bool = True) -> None:
        self.payload = payload or {}
        self.available = available
        self.calls: list[dict] = []

    def model_available(self, model: str) -> bool:
        self.calls.append({"operation": "available", "model": model})
        return self.available

    def generate_json(self, **kwargs):
        self.calls.append({"operation": "generate", **kwargs})
        return self.payload, SimpleNamespace(duration_ms=12.5)


class QueryAgentTests(unittest.TestCase):
    def test_deterministic_analysis_extracts_exact_locator_and_temporal_condition(self) -> None:
        result = deterministic_query_analysis(
            "정보보안업무규정 제 12 조의 2를 2026년 8월 1일 기준으로 알려줘"
        )

        self.assertEqual("exact_locator", result.intent)
        self.assertEqual("제12조의2", result.locators[0].canonical)
        self.assertEqual(("2026년 8월 1일",), result.date_conditions)
        self.assertTrue(result.requires_temporal_filter)
        self.assertEqual("deterministic_fallback", result.analysis_mode)

    def test_model_analysis_cannot_drop_locator_or_invent_regulation_name(self) -> None:
        runtime = _Runtime(
            {
                "intent": "general",
                "regulation_names": ["존재하지않는규정", "정보보안업무규정"],
                "date_conditions": [],
                "version_conditions": [],
                "keywords": ["접근권한", "관리"],
                "requires_temporal_filter": False,
                "confidence": 0.94,
            }
        )
        result = QueryAnalysisAgent(runtime=runtime).analyze(
            "정보보안업무규정 제2조 접근권한 관리"
        )

        self.assertEqual("local_model", result.analysis_mode)
        self.assertEqual("exact_locator", result.intent)
        self.assertEqual("제2조", result.locators[0].canonical)
        self.assertIn("정보보안업무규정", result.regulation_names)
        self.assertNotIn("존재하지않는규정", result.regulation_names)
        self.assertEqual("qwen3:1.7b", result.model)

    def test_unavailable_model_falls_back_with_safe_reason(self) -> None:
        result = QueryAnalysisAgent(runtime=_Runtime(available=False)).analyze("제3조는 무엇인가요")

        self.assertEqual("deterministic_fallback", result.analysis_mode)
        self.assertEqual("required_model_not_installed", result.fallback_reason)
        self.assertEqual("제3조", result.locators[0].canonical)

    def test_rewriter_preserves_locator_even_when_model_omits_it(self) -> None:
        analysis = deterministic_query_analysis("인사규정 제 7 조 승진 요건")
        runtime = _Runtime(
            {
                "normalized_query": "인사규정 승진 요건",
                "search_queries": ["승진 자격 조건"],
            }
        )

        result = QueryRewriteAgent(runtime=runtime).rewrite(analysis)

        self.assertEqual("local_model", result.rewrite_mode)
        self.assertIn("제7조", result.preserved_locators)
        self.assertTrue(any("제7조" in query.replace(" ", "") for query in result.search_queries))
        self.assertLessEqual(len(result.search_queries), 8)

    def test_deterministic_rewrite_compacts_spaced_article_notation(self) -> None:
        result = deterministic_query_rewrite(deterministic_query_analysis("복무규정 제 10 조 휴가"))

        self.assertIn("복무규정 제10조 휴가", result.search_queries)
        self.assertEqual(("제10조",), result.preserved_locators)


if __name__ == "__main__":
    unittest.main()
