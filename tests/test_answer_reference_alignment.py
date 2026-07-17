from __future__ import annotations

import hashlib
import unittest

from app.rag.extractive_answer import (
    NO_EVIDENCE_ANSWER,
    build_structured_extractive_answer,
    select_supporting_answer_results,
)
from app.retrieval.bm25_index import Bm25Index
from app.retrieval.searcher import search


class AnswerReferenceAlignmentTests(unittest.TestCase):
    def test_table_only_appendix_is_used_as_answer_evidence_and_citation(self) -> None:
        appendix = {
            "chunk_id": "appendix-1",
            "chunk_type": "appendix",
            "regulation_title": "계약업무규정",
            "appendix_refs": ["별표1"],
            "article_refs": ["제21조제3항"],
            "answer_outline": [
                "예산액별 평가위원 수(제21조제3항 관련) 예산액 1억원미만 1억~5억원 미만 "
                "5억~10억원 미만 10억원 이상 평가위원 수 4명 이상 5명 이상 6명 이상 7명 이상"
            ],
            "text": (
                "[본문]\n<별표1> 예산액별 평가위원 수(제21조제3항 관련)\n"
                "예산액\n1억원미만\n평가위원 수\n4명 이상\n[표]\n| 예산액 | 평가위원 수 |"
            ),
            "approval_id": "approval-table",
        }
        governing_article = {
            "chunk_id": "article-21",
            "chunk_type": "article",
            "regulation_title": "계약업무규정",
            "article_no": "제21조",
            "article_title": "평가위원회 구성",
            "appendix_refs": ["별표1"],
            "text": "③ 평가위원회는 별표 1과 같이 구성한다.",
            "approval_id": "approval-article",
        }
        query = "계약업무규정의 별표1 예산액별 평가위원 수(제21조제3항 관련)에는 어떤 기준이 있는가?"

        answer = build_structured_extractive_answer(query, [appendix, governing_article])
        supporting = select_supporting_answer_results(query, [appendix, governing_article])

        self.assertIn("별표1 (제21조제3항 관련)", answer)
        self.assertIn("1억원미만", answer)
        self.assertIn("4명 이상", answer)
        self.assertIn("계약업무규정 별표1 (제21조제3항 관련)", answer)
        self.assertIs(appendix, supporting[0])

    def test_exact_form_and_governing_reference_outrank_split_form_fragments(self) -> None:
        query = (
            "계약업무규정의 별지 제28호서식 (제41조의3제4항 관련)"
            "(신설 2024.07.31., 개정 2025.07.11.)에는 어떤 항목이 있는가?"
        )
        target = _record(
            "doc:form-28",
            "표준 미연동계약서와 미연동 사유를 작성한다.",
            chunk_type="form",
            form_refs=["별지제28호서식"],
            article_refs=["제41조의3제4항"],
        )
        governing = _record(
            "doc:article-41-3",
            "제41조의3 납품대금 연동제 계약과 조정은 별지 제28호서식을 사용한다.",
            chunk_type="article",
            article_no="제41조의3",
            form_refs=["별지제28호서식"],
        )
        distractors = [
            _record(
                f"doc:form-27-fragment-{index}",
                "별지 서식 신설 2024.07.31. 개정 2025.07.11. 계약서 항목 기준 관련 " * 3,
                chunk_type="form",
            )
            for index in range(7)
        ]
        records = [*distractors, governing, target]
        index = Bm25Index.build(records)

        scored, _metadata = search(query, records, index, top_k=5)

        self.assertEqual("doc:form-28", scored[0][1]["id"])
        self.assertIn("doc:article-41-3", [row[1]["id"] for row in scored])

    def test_supplementary_reference_ranking_is_independent_of_record_order(self) -> None:
        query = "계약업무규정의 부칙에서 제44조제2항 관련 시행일 또는 적용 내용은 무엇인가?"
        target = _record(
            "doc:supplementary-44-2",
            "부칙: 제44조제2항은 2026년 7월 1일부터 시행한다.",
            chunk_type="supplementary",
            article_refs=["제44조제2항"],
        )
        generic_a = _record("doc:effective-a", "제1조(시행일) 이 규정은 공포한 날부터 시행한다.")
        generic_b = _record("doc:effective-b", "제1조(시행일) 이 규정은 공포한 날부터 시행한다.")

        orders = ([generic_b, target, generic_a], [generic_a, target, generic_b])
        for records in orders:
            index = Bm25Index.build(records)
            first, _metadata = search(query, records, index, top_k=3)
            second, _metadata = search(query, records, index, top_k=3)

            self.assertEqual("doc:supplementary-44-2", first[0][1]["id"])
            self.assertEqual(
                [row[1]["id"] for row in first],
                [row[1]["id"] for row in second],
            )

    def test_reference_boost_does_not_create_evidence_for_unrelated_query(self) -> None:
        records = [
            _record(
                "doc:form-28",
                "표준 미연동계약서",
                chunk_type="form",
                form_refs=["별지제28호서식"],
                article_refs=["제41조의3제4항"],
            )
        ]
        index = Bm25Index.build(records)

        scored, _metadata = search("제타플라즈마 항법수당 네뷸라승인", records, index, top_k=5)

        self.assertEqual([], scored)
        self.assertEqual(NO_EVIDENCE_ANSWER, build_structured_extractive_answer("없는 질문", []))


def _record(
    record_id: str,
    text: str,
    *,
    chunk_type: str = "article",
    article_no: str = "",
    article_refs: list[str] | None = None,
    appendix_refs: list[str] | None = None,
    form_refs: list[str] | None = None,
) -> dict:
    chunk_id = record_id.split(":", 1)[-1]
    return {
        "id": record_id,
        "document_id": "doc",
        "chunk_id": chunk_id,
        "text": text,
        "content_hash": hashlib.sha256(f"{record_id}\n{text}".encode("utf-8")).hexdigest(),
        "metadata": {
            "document_id": "doc",
            "chunk_id": chunk_id,
            "chunk_type": chunk_type,
            "article_no": article_no,
            "article_refs": article_refs or [],
            "appendix_refs": appendix_refs or [],
            "form_refs": form_refs or [],
            "regulation_title": "계약업무규정",
        },
    }


if __name__ == "__main__":
    unittest.main()
