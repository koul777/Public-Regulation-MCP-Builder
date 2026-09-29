from types import SimpleNamespace
import unittest

from frontend.ai_review_display import review_display


class AIReviewDisplayTests(unittest.TestCase):
    def test_stored_rewrite_is_visible_without_replacing_original(self):
        chunk = SimpleNamespace(chunk_id="a", text="기한 3일", ai_preprocessed_text="기한 5일", metadata={})
        view = review_display(chunk)
        self.assertTrue(view.changed)
        self.assertIn("<del>3</del><ins>5</ins>", view.diff_html)
        self.assertEqual("기한 3일", chunk.text)

    def test_document_markup_is_escaped_in_diff(self):
        chunk = SimpleNamespace(text="원문", ai_preprocessed_text='<script>alert(1)</script>', metadata={})
        self.assertNotIn("<script>", review_display(chunk).diff_html)
        self.assertIn("&lt;script&gt;", review_display(chunk).diff_html)

    def test_matching_provider_findings_remain_visible_when_metadata_missing(self):
        chunk = SimpleNamespace(chunk_id="a", text="원문", metadata={})
        summary = {"provider_review_json": {"items": [
            {"chunk_id": "other", "issues": ["다른 조항"]},
            {"chunk_id": "a", "issues": ["표 연결 확인"], "recommended_human_check": "원문 표 대조"},
        ]}}
        self.assertEqual(("표 연결 확인",), review_display(chunk, summary).issues)
        self.assertEqual("원문 표 대조", review_display(chunk, summary).recommendation)

    def test_equal_and_absent_proposals_have_distinct_states(self):
        same = review_display(SimpleNamespace(text="원문", ai_preprocessed_text="원문", metadata={}))
        absent = review_display(SimpleNamespace(text="원문", metadata={}))
        self.assertFalse(same.changed)
        self.assertTrue(same.proposal)
        self.assertFalse(absent.proposal)


if __name__ == "__main__":
    unittest.main()
