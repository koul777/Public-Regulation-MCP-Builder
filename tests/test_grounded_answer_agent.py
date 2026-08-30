from __future__ import annotations

from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app.agents.grounded_answer_agent import GroundedAnswerAgent
from app.core.config import Settings


class GroundedAnswerAgentTests(TestCase):
    def _agent(self, **overrides: object) -> GroundedAnswerAgent:
        values: dict[str, object] = {
            "data_dir": Path("data"),
            "rag_llm_backend": "extractive",
            "rag_llm_model": "qwen3:8b",
        }
        values.update(overrides)
        return GroundedAnswerAgent(Settings(**values))

    def _evidence(self) -> list[dict[str, object]]:
        return [
            {
                "document_id": "doc-1",
                "chunk_id": "chunk-1",
                "approval_status": "approved",
                "approval_id": "approval-1",
                "regulation_title": "정보보안업무규정",
                "article_no": "제12조",
                "article_title": "접근권한 관리",
                "source_page_start": 14,
                "text": "정보시스템 관리자는 접근권한을 관리해야 한다.",
            }
        ]

    def test_extracts_answer_from_approved_evidence_without_model(self) -> None:
        result = self._agent().run({"query": "접근권한은 누가 관리해야 하나요?", "evidence": self._evidence()})

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["answer_mode"], "grounded_extractive")
        self.assertEqual(result["evidence_ids"], ["chunk-1"])
        self.assertFalse(result["abstained"])

    def test_answer_evidence_ids_exclude_retrieved_but_unused_chunks(self) -> None:
        evidence = [
            {
                "document_id": "doc-leave",
                "chunk_id": "chunk-leave",
                "approval_status": "approved",
                "approval_id": "approval-leave",
                "regulation_title": "인사규정",
                "article_no": "제30조",
                "article_title": "휴직 기간",
                "text": "제30조 육아휴직 기간은 2년 이내로 한다.",
            },
            {
                "document_id": "doc-trip",
                "chunk_id": "chunk-trip",
                "approval_status": "approved",
                "approval_id": "approval-trip",
                "regulation_title": "여비규정",
                "article_no": "제40조",
                "article_title": "출장 여비",
                "text": "출장 여비는 별도로 지급한다.",
            },
        ]

        result = self._agent().run({"query": "육아휴직 기간", "evidence": evidence})

        self.assertEqual(["chunk-leave"], result["evidence_ids"])
        self.assertEqual(["chunk-leave"], result["supporting_evidence_ids"])
        self.assertEqual(
            ["chunk-leave", "chunk-trip"],
            result["retrieved_evidence_ids"],
        )

    def test_empty_evidence_abstains_without_calling_model(self) -> None:
        with patch("app.agents.grounded_answer_agent.generate_local_llm_answer") as generate:
            result = self._agent(rag_llm_backend="ollama").run(
                {"query": "확인해줘", "evidence": []}
            )

        self.assertEqual(result["status"], "abstained")
        self.assertTrue(result["abstained"])
        generate.assert_not_called()

    def test_rejects_explicitly_unapproved_evidence(self) -> None:
        evidence = self._evidence()
        evidence[0]["approval_status"] = "pending_review"

        with self.assertRaisesRegex(ValueError, "non-approved"):
            self._agent().run({"query": "질문", "evidence": evidence})

    @patch("app.agents.grounded_answer_agent.local_llm_available", return_value=True)
    @patch(
        "app.agents.grounded_answer_agent.generate_local_llm_answer",
        return_value="근거에 따른 답변입니다. C:\\private\\secret.txt",
    )
    def test_qwen_backend_uses_local_answer_and_sanitizes_output(self, generate, available) -> None:
        result = self._agent(
            rag_llm_backend="ollama",
            rag_llm_endpoint="http://127.0.0.1:11434",
        ).run({"query": "질문", "evidence": self._evidence()})

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["answer_mode"], "grounded_local")
        self.assertEqual(result["backend"], "ollama")
        self.assertEqual(result["model"], "qwen3:8b")
        self.assertNotIn("C:\\private", result["answer"])
        available.assert_called_once()
        generate.assert_called_once()

    @patch("app.agents.grounded_answer_agent.local_llm_available", return_value=False)
    def test_unavailable_qwen_backend_falls_back_by_default(self, available) -> None:
        result = self._agent(rag_llm_backend="ollama").run(
            {"query": "질문", "evidence": self._evidence()}
        )

        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["answer_mode"], "grounded_extractive")
        self.assertEqual(result["fallback_reason"], "local_backend_not_available")
        available.assert_called_once()
    @patch("app.agents.grounded_answer_agent.local_llm_available", return_value=False)
    def test_unavailable_qwen_backend_can_be_strict(self, available) -> None:
        result = self._agent(rag_llm_backend="ollama").run(
            {"query": "질문", "evidence": self._evidence(), "allow_fallback": False}
        )

        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(result["abstained"])
        available.assert_called_once()
