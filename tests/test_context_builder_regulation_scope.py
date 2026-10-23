from __future__ import annotations

import unittest

from app.rag.context_builder import ContextBuilder


class ContextBuilderRegulationScopeTests(unittest.TestCase):
    @staticmethod
    def record(chunk_id: str, title: str, text: str, **overrides) -> dict:
        return {
            "chunk_id": chunk_id,
            "document_id": "synthetic-combined-book",
            "regulation_title": title,
            "regulation_version": "current",
            "article_no": "제7조",
            "article_title": "지급 기준",
            "approval_status": "approved",
            "approval_id": "approval-" + chunk_id,
            "approved_content_hash": "hash-" + chunk_id,
            "text": text,
            "score": 1.0,
            "source_page_start": 1,
            "source_page_end": 1,
            **overrides,
        }

    def test_equal_article_numbers_in_different_regulations_do_not_merge(self) -> None:
        first = self.record("first", "연구지원규정", "지원금은 12일에 지급한다.")
        second = self.record("second", "장비관리규정", "대여료는 25일에 납부한다.",
                             source_page_start=90, source_page_end=90)
        context = ContextBuilder().build([first, second])
        self.assertEqual(2, len(context.items))
        self.assertEqual([("first",), ("second",)], [item.evidence_ids for item in context.items])
        self.assertEqual([(1, 1), (90, 90)], [(item.source_page_start, item.source_page_end) for item in context.items])
        self.assertNotIn("대여료", context.items[0].text)
        self.assertNotIn("지원금", context.items[1].text)

    def test_same_title_with_different_regulation_ids_does_not_merge(self) -> None:
        first = self.record("first", "운영규정", "제1센터 운영 본문", regulation_id="synthetic-center-one")
        second = self.record("second", "운영규정", "제2센터 운영 본문", regulation_id="synthetic-center-two")
        context = ContextBuilder().build([first, second])
        self.assertEqual(2, len(context.items))

    def test_book_level_id_does_not_erase_regulation_title_or_number(self) -> None:
        for second_title in ("장비관리규정", "연구지원규정"):
            records = [self.record("first", "연구지원규정", "지원금 기준", regulation_id="synthetic-book",
                                   regulation_no="1-1"),
                       self.record("second", second_title, "별도 규정 기준", regulation_id="synthetic-book",
                                   regulation_no="2-1")]
            with self.subTest(second_title=second_title):
                self.assertEqual(2, len(ContextBuilder().build(records).items))

    def test_identical_content_in_different_regulations_keeps_each_citation(self) -> None:
        records = [self.record("first", "연구지원규정", "공통 문구", approved_content_hash="same-hash"),
                   self.record("second", "장비관리규정", "공통 문구", approved_content_hash="same-hash")]
        context = ContextBuilder().build(records)
        self.assertEqual(2, context.deduplicated_evidence_count)
        self.assertEqual([("first",), ("second",)], [item.evidence_ids for item in context.items])

    def test_identical_content_with_different_article_locators_is_not_deduplicated(self) -> None:
        records = [self.record("first", "연구지원규정", "공통 문구", approved_content_hash="same-hash"),
                   self.record("second", "연구지원규정", "공통 문구", article_no="제8조", approved_content_hash="same-hash")]
        context = ContextBuilder().build(records)
        self.assertEqual(2, len(context.items))
        self.assertEqual(["제7조", "제8조"], [item.article_no for item in context.items])

    def test_continuations_with_same_regulation_scope_still_merge(self) -> None:
        first = self.record("first", "연구 지원 규정", "첫 번째 항목", regulation_id="synthetic-support")
        second = self.record("second", "연구지원규정", "두 번째 항목", regulation_id="synthetic-support")
        context = ContextBuilder().build([first, second])
        self.assertEqual(1, len(context.items))
        self.assertEqual(("first", "second"), context.items[0].evidence_ids)
        self.assertIn("첫 번째 항목", context.items[0].text)
        self.assertIn("두 번째 항목", context.items[0].text)

    def test_unknown_regulation_scope_does_not_infer_continuations(self) -> None:
        context = ContextBuilder().build([
            self.record("first", "", "첫 번째 근거"),
            self.record("second", "", "두 번째 근거"),
        ])
        self.assertEqual(2, len(context.items))

    def test_null_regulation_identity_does_not_infer_continuations(self) -> None:
        records = []
        for chunk_id in ("first", "second"):
            metadata = self.record(chunk_id, "", "별도 근거 " + chunk_id)
            metadata.update(regulation_id=None, regulation_no=None,
                            regulation_title=None, document_name=None)
            records.append({"text": metadata.pop("text"), "metadata": metadata})
        context = ContextBuilder().build(records)
        self.assertEqual(2, len(context.items))
        self.assertEqual([("first",), ("second",)], [item.evidence_ids for item in context.items])

    def test_null_article_locator_does_not_infer_continuations(self) -> None:
        records = []
        for chunk_id in ("first", "second"):
            metadata = self.record(chunk_id, "연구지원규정", "별도 근거 " + chunk_id)
            metadata.update(article_no=None, governing_article_no=None)
            records.append({"text": metadata.pop("text"), "metadata": metadata})
        context = ContextBuilder().build(records)
        self.assertEqual(2, len(context.items))
        self.assertTrue(all(item.article_no == "" for item in context.items))

    def test_null_content_hash_does_not_deduplicate_distinct_evidence(self) -> None:
        records = []
        for chunk_id in ("first", "second"):
            metadata = self.record(chunk_id, "연구지원규정", "연속 근거 " + chunk_id)
            metadata.update(approved_content_hash=None, content_hash=None)
            records.append({"text": metadata.pop("text"), "metadata": metadata})
        context = ContextBuilder().build(records)
        self.assertEqual(2, context.deduplicated_evidence_count)
        self.assertEqual(("first", "second"), context.items[0].evidence_ids)
        self.assertEqual((), context.items[0].content_hashes)


if __name__ == "__main__":
    unittest.main()
