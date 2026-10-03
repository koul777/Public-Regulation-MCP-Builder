"""채팅 답변 경로가 질문을 문맥 구성에 넘겨, 질문이 이름 붙인 조문을 E1로 쓰는지 고정한다."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from app.agents.grounded_qa import _extractive_draft
from app.api import routes_rag
from app.core.config import Settings


def _record(chunk_id: str, article_no: str, title: str, score: float, text: str) -> dict:
    return {
        "chunk_id": chunk_id,
        "document_id": "doc",
        "approval_status": "approved",
        "regulation_title": "합성 인사규정",
        "regulation_version": "1",
        "article_no": article_no,
        "article_title": title,
        "approval_id": f"apr-{chunk_id}",
        "content_hash": f"hash-{chunk_id}",
        "text": text,
        "score": score,
    }


class RagChatContextOrderTests(unittest.TestCase):
    def test_orchestrated_chat_builds_context_with_the_question(self) -> None:
        results = [
            _record("a", "제38조", "징계 절차", 0.9, "제38조(징계 절차) 징계위원회는 30일 이내에 의결한다."),
            _record("b", "제11조", "임용의 원칙", 0.8, "제11조(임용의 원칙) 공개 경쟁 채용을 원칙으로 한다."),
            _record("c", "제35조", "징계의 종류", 0.5, "제35조(징계의 종류) 징계는 다음과 같다. 1. 중징계 2. 경징계"),
        ]
        seen: dict = {}

        def fake_answer_fast(self, *, query, context, history=None, strict_model=False):
            seen["context"] = context
            return _extractive_draft(context)

        with patch.object(routes_rag.GroundedQwenAnswerAgent, "answer_fast", fake_answer_fast), patch.object(
            routes_rag, "_deterministic_chat_draft_verification", return_value={"answer": "ok", "citations": []}
        ):
            routes_rag._orchestrated_chat_answer(
                Settings(data_dir=Path("data")),
                "징계의 종류는 무엇인가요?",
                results,
                use_model_claim_audit=False,
            )

        items = seen["context"].items
        self.assertEqual("징계의 종류", items[0].article_title)
        self.assertEqual("E1", items[0].context_id)
        self.assertEqual(3, len(items))


if __name__ == "__main__":
    unittest.main()
