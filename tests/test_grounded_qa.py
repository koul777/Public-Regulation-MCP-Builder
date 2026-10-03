from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.agents.claim_auditor import ClaimAuditAgent
from app.agents.grounded_qa import AnswerClaim, GroundedAnswerDraft, GroundedQwenAnswerAgent
from app.rag.context_builder import ContextBuilder
from app.rag.extractive_answer import NO_EVIDENCE_ANSWER


class _Runtime:
    def __init__(self, payload: dict, *, available: bool = True) -> None:
        self.payload = payload
        self.available = available
        self.last_kwargs: dict = {}

    def model_available(self, model: str) -> bool:
        return self.available

    def generate_json(self, **kwargs):
        self.last_kwargs = dict(kwargs)
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
    def test_missing_information_is_abstention_even_with_valid_citation_id(self) -> None:
        for answer in (
            "이 규정에서는 보증금 금액을 명시하고 있지 않습니다. [E1]",
            "지급 기한이 명시되어 있지 않습니다. [E1]",
            "제공된 근거가 부족합니다. [E1]",
            "금액을 확인할 수 없습니다. [E1]",
            "The amount is not specified. [E1]",
        ):
            for fast in (False, True):
                with self.subTest(answer=answer, fast=fast):
                    payload = {"answer": answer, "abstained": False}
                    if fast:
                        payload["evidence_context_ids"] = ["E1"]
                    else:
                        payload["claims"] = [{"claim_id": "C1", "text": answer, "evidence_context_ids": ["E1"]}]
                    agent = GroundedQwenAnswerAgent(runtime=_Runtime(payload))
                    result = (agent.answer_fast if fast else agent.answer)(query="보증금은 얼마인가요?", context=_context())
                    self.assertTrue(result.abstained)
                    self.assertEqual(NO_EVIDENCE_ANSWER, result.answer)
                    self.assertEqual((), result.claims)
                    self.assertEqual("abstained", result.answer_mode)

    def test_substantive_negative_rule_is_not_missing_information(self) -> None:
        context = _context()
        item = context.items[0].model_copy(update={"text": "보증금을 징수하지 않는다."})
        context = context.model_copy(update={"items": (item,)})
        agent = GroundedQwenAnswerAgent(runtime=_Runtime({
            "answer": "보증금을 징수하지 않습니다. [E1]",
            "evidence_context_ids": ["E1"], "abstained": False,
        }))
        result = agent.answer_fast(query="보증금을 내나요?", context=context)
        self.assertFalse(result.abstained)
        self.assertEqual(("E1",), result.claims[0].evidence_context_ids)

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

    def test_fast_qwen_answer_uses_compact_schema_and_known_evidence(self) -> None:
        runtime = _Runtime(
            {
                "answer": "접근권한은 분기마다 검토합니다. [E1]",
                "evidence_context_ids": ["E1"],
                "abstained": False,
            }
        )

        result = GroundedQwenAnswerAgent(runtime=runtime).answer_fast(
            query="접근권한은 언제 검토하나요?",
            context=_context(),
        )

        self.assertEqual("grounded_local", result.answer_mode)
        self.assertEqual(("E1",), result.claims[0].evidence_context_ids)
        self.assertEqual(420, runtime.last_kwargs["max_output_tokens"])
        schema_properties = runtime.last_kwargs["schema"]["properties"]
        self.assertEqual(
            {"answer", "evidence_context_ids", "abstained"},
            set(schema_properties),
        )

    def test_fast_qwen_abstention_never_returns_model_authored_claims(self) -> None:
        runtime = _Runtime(
            {
                "answer": "근거 없이 단정한 내용입니다.",
                "evidence_context_ids": [],
                "abstained": True,
            }
        )

        result = GroundedQwenAnswerAgent(runtime=runtime).answer_fast(
            query="질문",
            context=_context(),
        )

        self.assertTrue(result.abstained)
        self.assertEqual("승인된 규정 근거에서 확인할 수 없습니다.", result.answer)

    def test_fast_qwen_answer_rejects_undeclared_evidence_markers(self) -> None:
        runtime = _Runtime(
            {
                "answer": "검토 주기는 분기입니다. [E1] 추가 근거 [E9]",
                "evidence_context_ids": ["E1"],
                "abstained": False,
            }
        )

        result = GroundedQwenAnswerAgent(runtime=runtime).answer_fast(
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


class ExtractiveExcerptTests(unittest.TestCase):
    """Fallback 발췌가 '다음과 같다'에서 끊겨 정작 답이 빠지던 문제를 고정한다."""

    def test_list_introduction_keeps_the_numbered_items(self) -> None:
        from app.agents.grounded_qa import _extractive_excerpt

        text = (
            "제35조(징계의 종류) ① 교직원에 대한 징계는 중징계와 경징계로 구분하며, 그 종류는 다음과 같다. "
            "1. 중징계라 함은 파면, 해임, 강등 또는 정직을 말한다. 2. 경징계라 함은 감봉 또는 견책을 말한다."
        )
        excerpt = _extractive_excerpt(text)
        self.assertIn("파면, 해임, 강등 또는 정직", excerpt)
        self.assertIn("감봉 또는 견책", excerpt)
        self.assertTrue(text.startswith(excerpt.rstrip(" …")))

    def test_plain_article_still_uses_first_sentence(self) -> None:
        from app.agents.grounded_qa import _extractive_excerpt

        text = "제5조(임용 일자 소급 금지) 교직원의 임용은 그 일자를 소급하여서는 안 된다. 다른 문장이 이어진다."
        self.assertEqual("제5조(임용 일자 소급 금지) 교직원의 임용은 그 일자를 소급하여서는 안 된다.", _extractive_excerpt(text))

    def test_answer_classification_metadata_is_not_shown(self) -> None:
        from app.agents.grounded_qa import _extractive_excerpt

        text = "제1조(목적) 이 규정은 인사를 정한다. [답변분류] 의도: duration 키워드: 정년"
        self.assertNotIn("답변분류", _extractive_excerpt(text))

    def test_long_list_is_bounded(self) -> None:
        from app.agents.grounded_qa import _extractive_excerpt

        text = "제28조(직권 면직) 다음 각 호의 어느 하나에 해당될 때에는 면직시킬 수 있다. " + " ".join(
            f"{number}. 사유 {number}에 해당할 때." for number in range(1, 80)
        )
        excerpt = _extractive_excerpt(text)
        self.assertLessEqual(len(excerpt), 702)
        self.assertTrue(excerpt.endswith("…"))
