from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.agents.claim_auditor import ClaimAuditAgent
from app.agents.grounded_qa import AnswerClaim, GroundedAnswerDraft, GroundedQwenAnswerAgent
from app.rag.context_builder import ContextBuilder


class _Runtime:
    def __init__(self, payload: dict, *, available: bool = True) -> None:
        self.payload = payload
        self.available = available

    def model_available(self, model: str) -> bool:
        return self.available

    def generate_json(self, **kwargs):
        return self.payload, SimpleNamespace(duration_ms=21.0)


def _context():
    return ContextBuilder().build(
        [
            {
                "chunk_id": "chunk-1",
                "document_id": "doc-1",
                "text": "제2조(권한관리) 정보시스템 관리자는 접근권한을 분기마다 검토하여야 한다.",
                "score": 0.98,
                "approval_status": "approved",
                "approval_id": "approval-1",
                "approved_content_hash": "sha256:approved",
                "regulation_title": "정보보안업무규정",
                "regulation_version": "3.2",
                "part_title": "제1편 총칙",
                "chapter_title": "제2장 접근통제",
                "article_no": "제2조",
                "article_title": "권한관리",
                "paragraph_no": "제1항",
                "source_page_start": 14,
                "source_page_end": 14,
            }
        ]
    )


class GroundedQATests(unittest.TestCase):
    def test_extractively_answers_with_context_marker_when_model_not_requested(self) -> None:
        result = GroundedQwenAnswerAgent(runtime=_Runtime({})).answer(
            query="접근권한은 언제 검토하나요?",
            context=_context(),
            prefer_model=False,
        )

        self.assertEqual("grounded_extractive", result.answer_mode)
        self.assertIn("[E1]", result.answer)
        self.assertEqual(("E1",), result.claims[0].evidence_context_ids)

    def test_qwen_answer_accepts_only_known_marked_evidence(self) -> None:
        runtime = _Runtime(
            {
                "answer": "접근권한은 분기마다 검토해야 합니다. [E1]",
                "claims": [
                    {
                        "claim_id": "C1",
                        "text": "접근권한은 분기마다 검토해야 합니다.",
                        "evidence_context_ids": ["E1"],
                    }
                ],
                "abstained": False,
            }
        )

        result = GroundedQwenAnswerAgent(runtime=runtime).answer(
            query="접근권한은 언제 검토하나요?",
            context=_context(),
        )

        self.assertEqual("grounded_local", result.answer_mode)
        self.assertEqual("qwen3:8b", result.model)

    def test_qwen_answer_with_unknown_evidence_falls_back(self) -> None:
        runtime = _Runtime(
            {
                "answer": "답변 [E9]",
                "claims": [
                    {"claim_id": "C1", "text": "답변", "evidence_context_ids": ["E9"]}
                ],
                "abstained": False,
            }
        )

        result = GroundedQwenAnswerAgent(runtime=runtime).answer(
            query="질문",
            context=_context(),
        )

        self.assertEqual("grounded_extractive", result.answer_mode)
        self.assertEqual("model_ValueError", result.fallback_reason)

    def test_claim_auditor_binds_exact_quote_and_full_locator(self) -> None:
        draft = GroundedAnswerDraft(
            answer="접근권한은 분기마다 검토해야 합니다. [E1]",
            claims=(
                AnswerClaim(
                    claim_id="C1",
                    text="접근권한은 분기마다 검토해야 합니다.",
                    evidence_context_ids=("E1",),
                ),
            ),
            answer_mode="grounded_local",
            model="qwen3:8b",
        )
        runtime = _Runtime(
            {
                "findings": [
                    {
                        "claim_id": "C1",
                        "supported": True,
                        "evidence_context_ids": ["E1"],
                        "support_quote": "접근권한을 분기마다 검토하여야 한다.",
                        "reason_code": "",
                    }
                ]
            }
        )

        result = ClaimAuditAgent(runtime=runtime).audit(draft=draft, context=_context())

        self.assertEqual("verified", result.status)
        self.assertEqual("qwen3:4b", result.model)
        self.assertEqual("정보보안업무규정", result.citations[0].regulation_title)
        self.assertEqual("제2장 접근통제", result.citations[0].chapter_title)
        self.assertEqual("제2조", result.citations[0].article_no)
        self.assertEqual("제1항", result.citations[0].paragraph_no)
        self.assertEqual(14, result.citations[0].source_page_start)
        self.assertEqual(("chunk-1",), result.citations[0].evidence_ids)

    def test_claim_auditor_requires_exact_support_quote(self) -> None:
        draft = GroundedAnswerDraft(
            answer="접근권한을 검토합니다. [E1]",
            claims=(
                AnswerClaim(
                    claim_id="C1",
                    text="접근권한을 검토합니다.",
                    evidence_context_ids=("E1",),
                ),
            ),
            answer_mode="grounded_local",
        )
        runtime = _Runtime(
            {
                "findings": [
                    {
                        "claim_id": "C1",
                        "supported": True,
                        "evidence_context_ids": ["E1"],
                        "support_quote": "원문에 없는 인용",
                        "reason_code": "",
                    }
                ]
            }
        )

        result = ClaimAuditAgent(runtime=runtime).audit(draft=draft, context=_context())

        self.assertEqual("review_required", result.status)
        self.assertEqual("support_quote_not_exact", result.reason_code)

    def test_claim_auditor_rejects_answer_missing_marker_before_model(self) -> None:
        draft = GroundedAnswerDraft(
            answer="접근권한을 검토합니다.",
            claims=(
                AnswerClaim(
                    claim_id="C1",
                    text="접근권한을 검토합니다.",
                    evidence_context_ids=("E1",),
                ),
            ),
            answer_mode="grounded_local",
        )

        result = ClaimAuditAgent(runtime=_Runtime({})).audit(draft=draft, context=_context())

        self.assertEqual("rejected", result.status)
        self.assertEqual("answer_marker_missing", result.findings[0].reason_code)


if __name__ == "__main__":
    unittest.main()
