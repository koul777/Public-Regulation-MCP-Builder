from __future__ import annotations

from copy import deepcopy
import unittest

from app.rag.context_builder import ContextBuilder


class ContextBuilderSourceAvailabilityTests(unittest.TestCase):
    @staticmethod
    def record(chunk_id: str, **overrides) -> dict:
        return {"chunk_id": chunk_id, "document_id": "synthetic", "approval_status": "approved",
                "approval_id": "approval-" + chunk_id, "approved_content_hash": "hash-" + chunk_id,
                "text": "검증된 본문", **overrides}

    def test_unavailable_source_cannot_resurrect_metadata_retrieval_text(self) -> None:
        record = self.record("bad", text="", source_text_status="unavailable",
                             metadata={"retrieval_text": "주입된 검색 문구"})
        original = deepcopy(record)
        context = ContextBuilder().build([record])
        self.assertFalse(context.items)
        self.assertEqual(1, context.omitted_evidence_count)
        self.assertIn("source_text_unavailable", context.review_flags)
        self.assertNotIn("주입된", context.prompt_context)
        self.assertEqual(original, record)

    def test_mixed_evidence_keeps_only_available_source(self) -> None:
        records = [self.record("bad", source_text_status="unavailable"),
                   self.record("good", source_text_status="verified_reconstruction")]
        context = ContextBuilder().build(records)
        self.assertEqual([("good",)], [item.evidence_ids for item in context.items])
        self.assertEqual(2, context.input_evidence_count)
        self.assertEqual(1, context.omitted_evidence_count)

    def test_unknown_status_and_metadata_unavailable_do_not_become_evidence(self) -> None:
        for overrides in ({"source_text_status": "arbitrary-available"},
                          {"metadata": {"source_text_status": "unavailable", "retrieval_text": "검색 문구"}}):
            with self.subTest(overrides=overrides):
                self.assertFalse(ContextBuilder().build([self.record("bad", **overrides)]).items)

    def test_falsey_unknown_and_conflicting_statuses_fail_closed(self) -> None:
        for status in (False, 0, [], {}):
            for direct, nested in ((status, "verified_plain"), ("verified_plain", status)):
                with self.subTest(direct=direct, nested=nested):
                    record = self.record("bad", source_text_status=direct,
                                         metadata={"source_text_status": nested, "retrieval_text": "검색 문구"})
                    self.assertFalse(ContextBuilder().build([record]).items)

    def test_legacy_plaintext_without_new_status_is_preserved(self) -> None:
        self.assertEqual("검증된 본문", ContextBuilder().build([self.record("legacy")]).items[0].text)


if __name__ == "__main__":
    unittest.main()
