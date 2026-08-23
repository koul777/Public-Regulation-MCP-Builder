from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.agents.claim_auditor import ClaimAuditResult, ClaimFinding, ExactCitation
from app.agents.grounded_qa import AnswerClaim, GroundedAnswerDraft
from app.agents.query_agents import deterministic_query_rewrite
from app.api import routes_rag
from app.core.config import Settings
from app.core.security import AuthContext


class OrchestratedRagTests(unittest.TestCase):
    def test_chat_history_contextualizes_follow_up_retrieval_without_assistant_text(self) -> None:
        history = routes_rag._chat_history_payload(
            [
                routes_rag.RagChatMessage(role="user", content="출장비 정산 기한은 언제인가요?"),
                routes_rag.RagChatMessage(role="assistant", content="정산은 7일 이내입니다."),
            ]
        )

        query = routes_rag._contextualized_chat_query("연장할 수 있나요?", history)

        self.assertIn("출장비 정산 기한", query)
        self.assertIn("연장할 수 있나요?", query)
        self.assertNotIn("정산은 7일 이내", query)

    def test_request_backend_cannot_override_extractively_configured_runtime(self) -> None:
        request = routes_rag.RagChatRequest(query="질문", llm_backend="ollama")
        settings = Settings(
            data_dir=Path("data"),
            rag_llm_backend="extractive",
            rag_llm_endpoint="http://127.0.0.1:11434",
        )

        with self.assertRaises(routes_rag.HTTPException) as raised:
            routes_rag._chat_backend(request, settings)

        self.assertEqual(403, raised.exception.status_code)
        self.assertIn("override", str(raised.exception.detail).lower())

    def test_contextualized_query_never_truncates_the_current_question(self) -> None:
        current = "현재 질문의 핵심"
        query = routes_rag._contextualized_chat_query(
            current,
            [{"role": "user", "content": "이전 질문 " + ("가" * 6000)}],
        )

        self.assertLessEqual(len(query), routes_rag.MAX_MCP_QUERY_CHARS)
        self.assertTrue(query.endswith(current))

    def test_qa_pipeline_trace_records_role_statuses_and_model_fallback(self) -> None:
        trace = routes_rag._qa_search_pipeline_trace(
            retrieval={
                "analysis_mode": "model",
                "rewrite_mode": "model",
                "query_model": "qwen3:1.7b",
                "reranker_status": "degraded",
                "reranker_reason": "reranker_unavailable",
                "context_status": "completed",
            },
            candidate_count=4,
            visible_count=3,
            result_count=2,
            tenant_id="tenant-a",
        )
        rerank_stage = next(
            stage for stage in trace["stages"] if stage["stage_id"] == "rerank_filter"
        )
        reranker = rerank_stage["agent_role_statuses"][0]
        self.assertEqual("reranker", reranker["role_id"])
        self.assertEqual("degraded", reranker["status"])
        self.assertEqual("reranker_unavailable", reranker["reason_code"])
        self.assertNotIn("tenant-a", str(trace))

    def test_qa_role_trace_exposes_each_role_and_assigned_model(self) -> None:
        trace = routes_rag._qa_orchestration_role_trace(
            search_trace={
                "pipeline_trace": {
                    "stages": [
                        {"stage_id": "query_analysis", "status": "completed"},
                        {"stage_id": "query_correction", "status": "completed"},
                        {"stage_id": "hybrid_retrieval", "status": "completed"},
                        {"stage_id": "rerank_filter", "status": "completed"},
                        {"stage_id": "context_build", "status": "completed"},
                    ]
                }
            },
            orchestration={
                "claim_audit_status": "verified",
                "citation_verification_status": "verified",
            },
        )

        by_role = {item["role_id"]: item for item in trace}
        self.assertEqual("qwen3:1.7b", by_role["query_analyst"]["primary_model"])
        self.assertEqual("qwen3:4b", by_role["claim_auditor"]["primary_model"])
        self.assertEqual("qwen3:8b", by_role["grounded_answerer"]["primary_model"])
        self.assertTrue(by_role["grounded_answerer"]["purpose"])
        self.assertEqual("verified", by_role["citation_verifier"]["status"])
        security_steps = [item for item in trace if item["role_id"] == "security_guard"]
        self.assertEqual(
            ["security_gate_input", "security_gate_output"],
            [item["stage_id"] for item in security_steps],
        )
        self.assertFalse(any("tenant_id" in str(item) for item in trace))

    def test_auto_mode_selects_multi_model_only_for_qwen3_8b_ollama(self) -> None:
        request = routes_rag.RagChatRequest(query="질문")

        self.assertTrue(
            routes_rag._use_multi_model_orchestration(
                request,
                Settings(data_dir=Path("data"), rag_llm_backend="ollama", rag_llm_model="qwen3:8b"),
            )
        )
        self.assertFalse(
            routes_rag._use_multi_model_orchestration(
                request,
                Settings(data_dir=Path("data"), rag_llm_backend="ollama", rag_llm_model="local-llama"),
            )
        )
        forced = routes_rag.RagChatRequest(query="질문", orchestration_mode="multi_model")
        self.assertTrue(
            routes_rag._use_multi_model_orchestration(
                forced,
                Settings(data_dir=Path("data"), rag_llm_backend="extractive"),
            )
        )

    def test_exact_article_locator_matches_only_the_exact_visible_article(self) -> None:
        analysis = routes_rag.deterministic_query_analysis("제1조에 대해서 알려줘")
        records = [
            {"id": "article-1", "metadata": {"article_no": "제1조"}},
            {"id": "article-11", "metadata": {"article_no": "제11조"}},
            {
                "id": "tampered-top-level",
                "article_no": "제1조",
                "metadata": {"article_no": "제11조"},
            },
        ]

        matches = routes_rag._exact_article_locator_matches(analysis, records)

        self.assertEqual(["article-1"], [record["id"] for _, record in matches])

    def test_exact_article_fast_path_rejects_ambiguous_or_temporal_locators(self) -> None:
        self.assertTrue(
            routes_rag._is_exact_article_locator_query(
                routes_rag.deterministic_query_analysis("제1조에 대해서 알려줘")
            )
        )
        for query in (
            "제1조와 제2조를 비교해줘",
            "제1항에 대해서 알려줘",
            "2025년 1월 1일 기준 제1조를 알려줘",
            "적용 대상을 알려줘",
            "제1조와 관련된 별표를 알려줘",
            "제1조 위반 시 제재는 어느 조문이야",
            "제1조의 마지막 내용을 알려줘",
        ):
            with self.subTest(query=query):
                self.assertFalse(
                    routes_rag._is_exact_article_locator_query(
                        routes_rag.deterministic_query_analysis(query)
                    )
                )

    def test_document_scoped_exact_article_search_skips_all_retrieval_models(self) -> None:
        records = [
            {
                "id": "doc-1:chunk-1",
                "document_id": "doc-1",
                "chunk_id": "chunk-1",
                "text": "제1조(목적) 이 규정은 승인된 근거만 사용한다.",
                "metadata": {
                    "document_id": "doc-1",
                    "chunk_id": "chunk-1",
                    "article_no": "제1조",
                    "article_title": "목적",
                    "regulation_title": "샘플규정",
                    "approval_status": "approved",
                    "approval_id": "approval-1",
                },
            },
            {
                "id": "doc-1:chunk-11",
                "document_id": "doc-1",
                "chunk_id": "chunk-11",
                "text": "제11조 다른 내용",
                "metadata": {
                    "document_id": "doc-1",
                    "chunk_id": "chunk-11",
                    "article_no": "제11조",
                    "regulation_title": "샘플규정",
                    "approval_status": "approved",
                    "approval_id": "approval-11",
                },
            },
        ]
        request = routes_rag.RagSearchRequest(
            query="제1조에 대해서 알려줘",
            document_id="doc-1",
            top_k=5,
            orchestration_mode="multi_model",
        )
        auth = AuthContext(
            actor="tester",
            tenant_id="tenant-a",
            auth_mode="api_token",
            role="admin",
        )
        settings = Settings(data_dir=Path("data"), rag_trace_enabled=False)

        with patch.object(routes_rag, "_load_local_vector_records", return_value=records), patch.object(
            routes_rag,
            "_load_cached_approval_snapshot",
            return_value={},
        ), patch.object(routes_rag, "load_visible_records", return_value=records), patch.object(
            routes_rag.QueryAnalysisAgent,
            "analyze",
        ) as analyze, patch.object(
            routes_rag.QueryRewriteAgent,
            "rewrite",
        ) as rewrite, patch.object(
            routes_rag,
            "_score_records_for_queries",
        ) as score, patch.object(
            routes_rag,
            "_cached_qwen3_reranker",
        ) as reranker:
            results, trace = routes_rag.search_rag_records(request, auth, settings)
            missing_results, missing_trace = routes_rag.search_rag_records(
                request.model_copy(update={"query": "제999조에 대해서 알려줘"}),
                auth,
                settings,
            )

        self.assertEqual(["chunk-1"], [result["chunk_id"] for result in results])
        self.assertTrue(trace["exact_locator_fast_path"])
        self.assertEqual("deterministic-exact-article-v1", trace["retrieval_model"])
        self.assertEqual("not_requested", trace["reranker_status"])
        self.assertEqual([], missing_results)
        self.assertTrue(missing_trace["exact_locator_fast_path"])
        self.assertEqual(0, missing_trace["exact_locator_match_count"])
        analyze.assert_not_called()
        rewrite.assert_not_called()
        score.assert_not_called()
        reranker.assert_not_called()

    def test_cached_reranker_reuses_one_loaded_adapter(self) -> None:
        routes_rag._cached_qwen3_reranker.cache_clear()
        adapter = object()
        try:
            with patch.object(routes_rag, "Qwen3RerankerAdapter", return_value=adapter) as factory:
                first = routes_rag._cached_qwen3_reranker("")
                second = routes_rag._cached_qwen3_reranker("")
        finally:
            routes_rag._cached_qwen3_reranker.cache_clear()

        self.assertIs(first, second)
        factory.assert_called_once()

    def test_general_query_reranks_only_a_bounded_candidate_set(self) -> None:
        records = [
            {
                "id": f"doc-1:chunk-{index}",
                "document_id": "doc-1",
                "chunk_id": f"chunk-{index}",
                "text": f"승인된 일반 규정 내용 {index}",
                "metadata": {
                    "document_id": "doc-1",
                    "chunk_id": f"chunk-{index}",
                    "article_no": f"제{index + 1}조",
                    "regulation_title": "샘플규정",
                    "approval_status": "approved",
                    "approval_id": f"approval-{index}",
                },
            }
            for index in range(30)
        ]
        scored = [(1.0 - index * 0.01, record) for index, record in enumerate(records)]
        analysis = routes_rag.deterministic_query_analysis("휴가 신청 방법을 알려줘")
        rewrite = deterministic_query_rewrite(analysis)
        adapter = Mock()
        adapter.rerank.side_effect = (
            lambda query, candidates, *, top_k: list(candidates)[:top_k]
        )
        request = routes_rag.RagSearchRequest(
            query="휴가 신청 방법을 알려줘",
            document_id="doc-1",
            top_k=5,
            orchestration_mode="multi_model",
        )
        auth = AuthContext(
            actor="tester",
            tenant_id="tenant-a",
            auth_mode="api_token",
            role="admin",
        )
        settings = Settings(data_dir=Path("data"), rag_trace_enabled=False)

        with patch.object(routes_rag, "_load_local_vector_records", return_value=records), patch.object(
            routes_rag,
            "_load_cached_approval_snapshot",
            return_value={},
        ), patch.object(routes_rag, "load_visible_records", return_value=records), patch.object(
            routes_rag.QueryAnalysisAgent,
            "analyze",
            return_value=analysis,
        ), patch.object(
            routes_rag.QueryRewriteAgent,
            "rewrite",
            return_value=rewrite,
        ), patch.object(
            routes_rag,
            "_score_records_for_queries",
            return_value=(scored, {"retrieval_model": "test-hybrid"}),
        ), patch.object(
            routes_rag,
            "_cached_qwen3_reranker",
            return_value=adapter,
        ):
            results, trace = routes_rag.search_rag_records(request, auth, settings)

        rerank_candidates = adapter.rerank.call_args.args[1]
        self.assertEqual(10, len(rerank_candidates))
        self.assertEqual(5, len(results))
        self.assertEqual("completed", trace["reranker_status"])

    def test_general_query_never_sends_more_than_twenty_candidates_to_reranker(self) -> None:
        records = [
            {
                "id": f"doc-1:chunk-{index}",
                "document_id": "doc-1",
                "chunk_id": f"chunk-{index}",
                "text": f"승인된 일반 규정 내용 {index}",
                "metadata": {
                    "document_id": "doc-1",
                    "chunk_id": f"chunk-{index}",
                    "article_no": f"제{index + 1}조",
                    "regulation_title": "샘플규정",
                    "approval_status": "approved",
                    "approval_id": f"approval-{index}",
                },
            }
            for index in range(50)
        ]
        scored = [(1.0 - index * 0.01, record) for index, record in enumerate(records)]
        analysis = routes_rag.deterministic_query_analysis("휴가 신청 방법을 알려줘")
        rewrite = deterministic_query_rewrite(analysis)
        adapter = Mock()
        adapter.rerank.side_effect = (
            lambda query, candidates, *, top_k: list(candidates)[:top_k]
        )
        request = routes_rag.RagSearchRequest(
            query="휴가 신청 방법을 알려줘",
            document_id="doc-1",
            top_k=20,
            orchestration_mode="multi_model",
        )
        auth = AuthContext(
            actor="tester",
            tenant_id="tenant-a",
            auth_mode="api_token",
            role="admin",
        )
        settings = Settings(data_dir=Path("data"), rag_trace_enabled=False)

        with patch.object(routes_rag, "_load_local_vector_records", return_value=records), patch.object(
            routes_rag,
            "_load_cached_approval_snapshot",
            return_value={},
        ), patch.object(routes_rag, "load_visible_records", return_value=records), patch.object(
            routes_rag.QueryAnalysisAgent,
            "analyze",
            return_value=analysis,
        ), patch.object(
            routes_rag.QueryRewriteAgent,
            "rewrite",
            return_value=rewrite,
        ), patch.object(
            routes_rag,
            "_score_records_for_queries",
            return_value=(scored, {"retrieval_model": "test-hybrid"}),
        ), patch.object(
            routes_rag,
            "_cached_qwen3_reranker",
            return_value=adapter,
        ):
            results, trace = routes_rag.search_rag_records(request, auth, settings)

        self.assertEqual(20, len(adapter.rerank.call_args.args[1]))
        self.assertEqual(20, len(results))
        self.assertEqual("completed", trace["reranker_status"])

    @patch("app.api.routes_rag._score_records")
    def test_multi_query_scoring_uses_weighted_rrf_without_expanding_records(self, score_records) -> None:
        record_a = {"id": "a", "text": "A"}
        record_b = {"id": "b", "text": "B"}
        score_records.side_effect = [
            ([(1.0, record_a), (0.5, record_b)], {"retrieval_model": "model-a"}),
            ([(1.0, record_b)], {"retrieval_model": "model-b"}),
        ]
        auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")

        scored, metadata = routes_rag._score_records_for_queries(
            ["query one", "query two"],
            [record_a, record_b],
            settings=Settings(data_dir=Path("data")),
            auth=auth,
        )

        self.assertEqual({"a", "b"}, {record["id"] for _, record in scored})
        self.assertEqual("weighted_rrf-v1", metadata["multi_query_fusion"])
        self.assertEqual(2, metadata["search_query_count"])
        self.assertEqual(2, score_records.call_count)

    def test_orchestrated_chat_returns_claim_audited_exact_citation(self) -> None:
        results = [
            {
                "chunk_id": "chunk-1",
                "document_id": "doc-1",
                "text": "제2조 접근권한은 분기마다 검토한다.",
                "approval_status": "approved",
                "approval_id": "approval-1",
                "regulation_title": "정보보안업무규정",
                "regulation_version": "3.2",
                "chapter_title": "제2장 접근통제",
                "article_no": "제2조",
                "paragraph_no": "제1항",
                "source_page_start": 14,
            }
        ]
        draft = GroundedAnswerDraft(
            answer="접근권한은 분기마다 검토합니다. [E1]",
            claims=(
                AnswerClaim(
                    claim_id="C1",
                    text="접근권한은 분기마다 검토합니다.",
                    evidence_context_ids=("E1",),
                ),
            ),
            answer_mode="grounded_local",
            model="qwen3:8b",
        )
        audit = ClaimAuditResult(
            status="verified",
            verified_claim_ids=("C1",),
            citations=(
                ExactCitation(
                    context_id="E1",
                    evidence_ids=("chunk-1",),
                    document_id="doc-1",
                    regulation_title="정보보안업무규정",
                    regulation_version="3.2",
                    chapter_title="제2장 접근통제",
                    article_no="제2조",
                    paragraph_no="제1항",
                    source_page_start=14,
                    approval_ids=("approval-1",),
                    support_quote="접근권한은 분기마다 검토한다.",
                ),
            ),
            model="qwen3:4b",
            audit_mode="local_model",
        )
        progress_events: list[dict[str, object]] = []
        with patch("app.api.routes_rag.GroundedQwenAnswerAgent.answer", return_value=draft), patch(
            "app.api.routes_rag.ClaimAuditAgent.audit", return_value=audit
        ), routes_rag.rag_chat_progress(progress_events.append):
            result = routes_rag._orchestrated_chat_answer(
                Settings(data_dir=Path("data"), rag_llm_endpoint="http://127.0.0.1:11434"),
                "검토 주기는?",
                results,
            )

        self.assertEqual("verified", result["claim_audit_status"])
        self.assertEqual("qwen3:8b", result["answer_model"])
        self.assertEqual("제2조", result["citations"][0]["article_no"])
        self.assertEqual("제1항", result["citations"][0]["paragraph_no"])
        self.assertTrue(result["citations"][0]["support_quote"])
        self.assertEqual(
            ["context_build", "answer_generation", "claim_audit", "citation_verify"],
            [event["stage"] for event in progress_events],
        )
        self.assertEqual([44, 55, 76, 90], [event["progress"] for event in progress_events])

    def test_progress_callback_failure_never_breaks_the_rag_security_path(self) -> None:
        def broken_callback(_event: dict[str, object]) -> None:
            raise RuntimeError("UI disappeared")

        with routes_rag.rag_chat_progress(broken_callback):
            routes_rag._emit_rag_chat_progress("retrieval", 150, "검색 중")

        captured: list[dict[str, object]] = []
        with routes_rag.rag_chat_progress(captured.append):
            routes_rag._emit_rag_chat_progress("retrieval", 150, "검색 중")
        self.assertEqual(100, captured[0]["progress"])

    def test_rejected_qwen_draft_falls_back_to_verified_extractive_answer(self) -> None:
        results = [
            {
                "chunk_id": "chunk-1",
                "document_id": "doc-1",
                "text": "제2조 접근권한은 분기마다 검토한다.",
                "approval_status": "approved",
                "approval_id": "approval-1",
                "article_no": "제2조",
            }
        ]
        model_draft = GroundedAnswerDraft(
            answer="접근권한은 매달 검토합니다. [E1]",
            claims=(
                AnswerClaim(
                    claim_id="C1",
                    text="접근권한은 매달 검토합니다.",
                    evidence_context_ids=("E1",),
                ),
            ),
            answer_mode="grounded_local",
            model="qwen3:8b",
        )
        fallback_draft = GroundedAnswerDraft(
            answer="- 제2조: 접근권한은 분기마다 검토한다. [E1]",
            claims=(
                AnswerClaim(
                    claim_id="C1",
                    text="접근권한은 분기마다 검토한다.",
                    evidence_context_ids=("E1",),
                ),
            ),
            answer_mode="grounded_extractive",
            fallback_reason="model_not_requested",
        )
        audit = ClaimAuditResult(
            status="rejected",
            findings=(
                ClaimFinding(
                    claim_id="C1",
                    status="unsupported",
                    evidence_context_ids=("E1",),
                    reason_code="not_entailed",
                ),
            ),
            rejected_claim_ids=("C1",),
            model="qwen3:4b",
            audit_mode="local_model",
            reason_code="unsupported_claims",
        )
        with patch(
            "app.api.routes_rag.GroundedQwenAnswerAgent.answer",
            side_effect=[model_draft, fallback_draft],
        ), patch("app.api.routes_rag.ClaimAuditAgent.audit", return_value=audit):
            result = routes_rag._orchestrated_chat_answer(
                Settings(data_dir=Path("data"), rag_llm_endpoint="http://127.0.0.1:11434"),
                "접근권한 검토 주기는?",
                results,
            )

        self.assertEqual("grounded_extractive", result["answer_mode"])
        self.assertEqual("fallback_from_rejected", result["claim_audit_status"])
        self.assertEqual("qwen3:8b", result["attempted_answer_model"])
        self.assertEqual("chunk-1", result["citations"][0]["chunk_id"])


if __name__ == "__main__":
    unittest.main()
