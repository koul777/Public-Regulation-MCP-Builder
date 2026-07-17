from __future__ import annotations

import unittest

from scripts.build_codex_ai_review_decision_packet import build_rows, codex_table_decision


class CodexAiReviewDecisionPacketTests(unittest.TestCase):
    def test_strong_structured_match_becomes_provisional_but_not_indexable(self) -> None:
        decision = codex_table_decision(
            {
                "match_label": "strong_review_match",
                "match_score": "55",
                "kordoc_triage_label": "structured_table_candidate",
                "table_classification": "probable_table_extraction_failed",
            }
        )

        self.assertEqual(decision["codex_decision"], "provisional_table_structure_candidate")
        self.assertEqual(decision["vector_indexing_allowed"], "false")
        self.assertEqual(decision["human_approval_required"], "true")
        self.assertEqual(decision["decision_basis"], "deterministic_heuristic_no_model_call")
        self.assertEqual(decision["model_api_called"], "false")
        self.assertNotIn("Codex accepts", decision["codex_action"])

    def test_probable_false_positive_overrides_strong_match(self) -> None:
        decision = codex_table_decision(
            {
                "match_label": "strong_review_match",
                "match_score": "72",
                "kordoc_triage_label": "structured_table_candidate",
                "table_classification": "probable_table_false_positive",
            }
        )

        self.assertEqual(decision["codex_decision"], "human_source_check_probable_false_positive")
        self.assertEqual(decision["merge_allowed_without_human"], "false")

    def test_build_rows_preserves_evidence_samples(self) -> None:
        rows = build_rows(
            [
                {
                    "institution_name": "기관",
                    "filename": "규정.pdf",
                    "document_id": "doc",
                    "chunk_id": "chunk",
                    "match_label": "medium_review_match",
                    "match_score": "34",
                    "kordoc_triage_label": "possible_table_candidate",
                    "local_text_sample": "로컬 표 후보",
                    "kordoc_sample_rows": "구분 | 기준",
                }
            ]
        )

        self.assertEqual(rows[0]["codex_decision"], "source_span_check_before_provisional_merge")
        self.assertEqual(rows[0]["decision_basis"], "deterministic_heuristic_no_model_call")
        self.assertEqual(rows[0]["model_api_called"], "false")
        self.assertEqual(rows[0]["local_text_sample"], "로컬 표 후보")
        self.assertEqual(rows[0]["kordoc_sample_rows"], "구분 | 기준")


if __name__ == "__main__":
    unittest.main()
