from __future__ import annotations

import unittest

from app.rag.context_builder import ContextBuilder


class ContextBuilderAttachmentScopeTests(unittest.TestCase):
    @staticmethod
    def record(chunk_id: str, text: str = "승인된 근거", **overrides) -> dict:
        return {
            "chunk_id": chunk_id,
            "document_id": "synthetic-combined-book",
            "regulation_id": "synthetic-book",
            "regulation_no": "1-2",
            "regulation_title": "연구지원규정",
            "regulation_version": "current",
            "article_no": "제7조",
            "article_title": "신청 절차",
            "approval_status": "approved",
            "approval_id": "approval-" + chunk_id,
            "approved_content_hash": "hash-" + chunk_id,
            "text": text,
            "score": 1.0,
            "source_page_start": 1,
            "source_page_end": 1,
            **overrides,
        }

    def test_related_form_is_not_an_article_continuation(self) -> None:
        article = self.record("article", "제7조 신청 절차", chunk_type="article", direct_article_no="제7조")
        form = self.record("form", "별지 제1호서식 신청서", chunk_type="form", direct_article_no="",
                           governing_article_no="제7조", governing_article_match_ref="별지제1호서식",
                           source_page_start=30, source_page_end=30)
        context = ContextBuilder().build([article, form])
        self.assertEqual([("article",), ("form",)], [item.evidence_ids for item in context.items])
        self.assertEqual([(1, 1), (30, 30)], [(item.source_page_start, item.source_page_end) for item in context.items])

    def test_identical_article_and_form_content_keep_both_citations(self) -> None:
        article = self.record("article", chunk_type="article", approved_content_hash="shared-hash")
        form = self.record("form", chunk_type="form", approved_content_hash="shared-hash",
                           governing_article_match_ref="별지제1호서식")
        context = ContextBuilder().build([article, form])
        self.assertEqual(2, context.deduplicated_evidence_count)
        self.assertEqual([("article",), ("form",)], [item.evidence_ids for item in context.items])

    def test_different_forms_with_same_governing_article_do_not_merge(self) -> None:
        first = self.record("first", "신청서", chunk_type="form", governing_article_match_ref="별지제1호서식")
        second = self.record("second", "승인서", chunk_type="form", governing_article_match_ref="별지제2호서식")
        self.assertEqual(2, len(ContextBuilder().build([first, second]).items))

    def test_same_explicit_form_locator_continuations_merge(self) -> None:
        first = self.record("first", "첫째 항목", chunk_type="form", article_no="", form_no="별지 제1호서식")
        second = self.record("second", "둘째 항목", chunk_type="form", article_no="", form_no="별지제1호서식",
                             source_page_start=2, source_page_end=2)
        context = ContextBuilder().build([first, second])
        self.assertEqual(1, len(context.items))
        self.assertEqual(("first", "second"), context.items[0].evidence_ids)
        self.assertEqual((1, 2), (context.items[0].source_page_start, context.items[0].source_page_end))
        self.assertIn("첫째 항목", context.items[0].text)
        self.assertIn("둘째 항목", context.items[0].text)

    def test_form_number_punctuation_is_not_erased(self) -> None:
        first = self.record("first", chunk_type="form", form_no="별지제1-1호서식")
        second = self.record("second", chunk_type="form", form_no="별지제11호서식")
        self.assertEqual(2, len(ContextBuilder().build([first, second]).items))

    def test_unlocated_forms_do_not_infer_continuations(self) -> None:
        first = self.record("first", chunk_type="form", governing_article_no="제7조")
        second = self.record("second", chunk_type="form", governing_article_no="제7조")
        self.assertEqual(2, len(ContextBuilder().build([first, second]).items))

    def test_individual_tables_keep_each_identity(self) -> None:
        first = self.record("first", chunk_type="table", table_appendix_no="별표1", approved_content_hash="shared-hash")
        second = self.record("second", chunk_type="table", table_appendix_no="별표1", approved_content_hash="shared-hash")
        context = ContextBuilder().build([first, second])
        self.assertEqual(2, context.deduplicated_evidence_count)
        self.assertEqual([("first",), ("second",)], [item.evidence_ids for item in context.items])

    def test_explicit_appendix_continuations_keep_their_own_scope(self) -> None:
        article = self.record("article", "조문", chunk_type="article")
        first = self.record("first", "별표 첫째 부분", chunk_type="appendix", table_appendix_no="별표 1")
        second = self.record("second", "별표 둘째 부분", chunk_type="appendix", table_appendix_no="별표1")
        context = ContextBuilder().build([article, first, second])
        self.assertEqual([("article",), ("first", "second")], [item.evidence_ids for item in context.items])

    def test_same_article_continuations_still_merge(self) -> None:
        first = self.record("first", "첫째 부분", chunk_type="article")
        second = self.record("second", "둘째 부분", chunk_type="article")
        context = ContextBuilder().build([first, second])
        self.assertEqual(1, len(context.items))
        self.assertEqual(("first", "second"), context.items[0].evidence_ids)

    def test_nested_mcp_metadata_preserves_attachment_scope(self) -> None:
        article = self.record("article", "조문", chunk_type="article")
        form = self.record("form", "신청서", chunk_type="form", direct_article_no="",
                           governing_article_no="제7조", governing_article_match_ref="별지제1호서식")
        wrapped = [{"text": record["text"], "metadata": {k: v for k, v in record.items() if k != "text"}}
                   for record in (article, form)]
        self.assertEqual(2, len(ContextBuilder().build(wrapped).items))

    def test_identical_text_from_different_documents_keeps_each_citation(self) -> None:
        first = self.record("first", document_id="source-one", approved_content_hash="shared-hash")
        second = self.record("second", document_id="source-two", approved_content_hash="shared-hash")
        context = ContextBuilder().build([first, second])
        self.assertEqual(2, context.deduplicated_evidence_count)
        self.assertEqual(["source-one", "source-two"], [item.document_id for item in context.items])


if __name__ == "__main__":
    unittest.main()
