from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.export_attention_chunks import attention_report, write_markdown


class ExportAttentionChunksTests(unittest.TestCase):
    def test_collects_chunk_attention_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp)
            chunks = [
                {
                    "chunk_id": "chunk_1",
                    "chunk_type": "paragraph",
                    "normalized_text": "□ 예산 집행을 개선한다.",
                    "metadata": {
                        "hierarchy_path": "doc > p",
                        "table_probable_false_positive": True,
                        "table_classification": "probable_false_positive_budget_prose",
                        "table_review_reason": "budget_guideline_bullet_prose_dominates",
                        "table_confidence": 0.64,
                    },
                },
                {
                    "chunk_id": "chunk_2",
                    "chunk_type": "article",
                    "normalized_text": "private \uf0b1 glyph",
                    "metadata": {"hierarchy_path": "doc > article"},
                },
            ]
            (repository / "doc_1_chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            batch_report = {
                "generated_at": "2026-07-03T00:00:00Z",
                "input_count": 1,
                "rows": [
                    {
                        "document_id": "doc_1",
                        "filename": "sample.hwp",
                        "source_system": "PUBLIC_PORTAL",
                        "source_record_id": "100",
                        "source_file_id": "200",
                        "quality_score": 100,
                        "failed_info_check_count": 1,
                        "recommendation_count": 1,
                        "probable_table_false_positive_chunks": 1,
                    }
                ],
            }

            report = attention_report(batch_report, repository, max_chunks_per_doc=5)

            self.assertEqual(report["document_count"], 1)
            self.assertEqual(report["attention_document_count"], 1)
            self.assertEqual(report["stable_document_count"], 0)
            self.assertEqual(report["signal_counts"]["table_probable_false_positive"], 1)
            self.assertEqual(report["attention_signal_counts"]["table_probable_false_positive"], 1)
            self.assertEqual(report["signal_counts"]["private_use_text"], 1)
            self.assertEqual(report["table_classification_counts"]["probable_false_positive_budget_prose"], 1)
            self.assertEqual(report["documents"][0]["identity"], "PUBLIC_PORTAL:100:200")
            self.assertEqual(report["documents"][0]["sample_count"], 2)
            self.assertEqual(report["max_signal_samples"][0]["watch_key"], "table_probable_false_positive:probable_false_positive_budget_prose")

    def test_separates_stable_false_positive_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp)
            chunks = [
                {
                    "chunk_id": "chunk_stable",
                    "chunk_type": "paragraph",
                    "normalized_text": "□ 예산 집행을 개선한다.",
                    "metadata": {
                        "hierarchy_path": "doc > p",
                        "table_probable_false_positive": True,
                        "table_false_positive_stability": "stable",
                        "table_classification": "probable_false_positive_budget_prose",
                        "table_confidence": 0.9,
                        "table_header_hits": 3,
                        "table_numeric_rows": 2,
                    },
                }
            ]
            (repository / "doc_1_chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            batch_report = {
                "generated_at": "2026-07-03T00:00:00Z",
                "input_count": 1,
                "rows": [
                    {
                        "document_id": "doc_1",
                        "filename": "sample.hwp",
                        "quality_score": 100,
                        "probable_table_false_positive_chunks": 1,
                        "stable_table_false_positive_chunks": 1,
                        "table_false_positive_attention_chunks": 0,
                    }
                ],
            }

            report = attention_report(batch_report, repository, max_chunks_per_doc=5)

            self.assertEqual(report["attention_document_count"], 0)
            self.assertEqual(report["stable_document_count"], 1)
            self.assertEqual(report["stable_signal_counts"]["table_stable_false_positive"], 1)
            self.assertEqual(report["max_signal_samples"][0]["watch_key"], "table_stable_false_positive:probable_false_positive_budget_prose")
            self.assertEqual(report["max_signal_samples"][0]["watch_score"][:3], [0.9, 3.0, 2.0])

    def test_writes_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "attention.md"
            write_markdown(
                {
                    "generated_from": "now",
                    "input_count": 1,
                    "document_count": 0,
                    "signal_counts": {},
                    "attention_signal_counts": {},
                    "stable_signal_counts": {},
                    "table_classification_counts": {},
                    "documents": [],
                },
                path,
            )

            self.assertIn("Attention Chunk Report", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
