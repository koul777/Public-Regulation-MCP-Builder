from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from app.agents.claim_auditor import ClaimAuditResult, ClaimFinding, ExactCitation
from app.agents.grounded_qa import AnswerClaim, GroundedAnswerDraft
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
        with patch("app.api.routes_rag.GroundedQwenAnswerAgent.answer", return_value=draft), patch(
            "app.api.routes_rag.ClaimAuditAgent.audit", return_value=audit
        ):
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
