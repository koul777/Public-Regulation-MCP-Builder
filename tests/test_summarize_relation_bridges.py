from __future__ import annotations

import unittest

from scripts.summarize_relation_bridges import summarize_relation_bridges


class SummarizeRelationBridgesTests(unittest.TestCase):
    def test_summarizes_bridge_edges_with_chunk_context(self) -> None:
        chunks = [
            {
                "chunk_id": "chunk_1",
                "document_name": "계약규정",
                "institution_name": "기관",
                "normalized_text": "제5조는 인사규정 제10조와 국가계약법 시행령 제35조를 따른다.",
            }
        ]
        edges = [
            {
                "relation_type": "article_cites_regulation_article",
                "document_id": "doc_1",
                "chunk_id": "chunk_1",
                "source_label": "계약규정 제5조",
                "target_label": "인사규정 제10조",
                "evidence_text": "인사규정 제10조",
                "confidence": 0.9,
            },
            {
                "relation_type": "article_cites_law_article",
                "document_id": "doc_1",
                "chunk_id": "chunk_1",
                "source_label": "계약규정 제5조",
                "target_label": "국가계약법 시행령 제35조",
                "evidence_text": "국가계약법 시행령 제35조",
                "confidence": 0.85,
            },
            {
                "relation_type": "term_cooccurs_with_term",
                "document_id": "doc_1",
                "chunk_id": "chunk_1",
                "source_label": "계약",
                "target_label": "입찰",
            },
        ]

        report = summarize_relation_bridges(edges, chunks, limit=10)

        self.assertEqual(report["bridge_edge_count"], 2)
        self.assertEqual(report["relation_type_counts"]["article_cites_regulation_article"], 1)
        self.assertEqual(report["relation_type_counts"]["article_cites_law_article"], 1)
        self.assertEqual(report["unique_target_count"], 2)
        self.assertEqual(report["samples"][0]["source_document"], "계약규정")
        self.assertIn("인사규정 제10조", {item["target_label"] for item in report["top_targets"]})


if __name__ == "__main__":
    unittest.main()
