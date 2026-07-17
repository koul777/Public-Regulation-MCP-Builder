from __future__ import annotations

import unittest

from scripts.compare_batch_quality_reports import compare_reports


def row(file_id: str, chunk_count: int, table_rows: int, filename: str | None = None) -> dict:
    return {
        "source_system": "PUBLIC_PORTAL",
        "source_record_id": f"board-{file_id}",
        "source_file_id": file_id,
        "filename": filename or f"{file_id}.hwp",
        "document_id": f"doc_{file_id}",
        "status": "completed",
        "quality_score": 100.0,
        "warning_count": 0,
        "failed_info_check_count": 0,
        "recommendation_count": 0,
        "node_count": 10,
        "chunk_count": chunk_count,
        "chunk_to_source_char_ratio": 1.0,
        "table_like_chunks": 1,
        "table_like_without_cell_rows": 0,
        "table_cell_row_count": table_rows,
        "probable_table_false_positive_chunks": 0,
        "stable_table_false_positive_chunks": 0,
        "table_false_positive_attention_chunks": 0,
        "probable_table_extraction_failed_chunks": 0,
        "chunks_missing_regulation_no": 0,
        "article_chunks_missing_regulation_no": 0,
    }


class CompareBatchQualityReportsTests(unittest.TestCase):
    def test_compares_batch_rows_by_source_identity(self) -> None:
        before = {"rows": [row("10", 5, 2), row("20", 7, 3), row("30", 9, 4)]}
        after = {"rows": [row("10", 5, 2), row("20", 8, 6), row("40", 1, 0)]}

        result = compare_reports(before, after)

        self.assertEqual(result["counts"]["added"], 1)
        self.assertEqual(result["counts"]["removed"], 1)
        self.assertEqual(result["counts"]["metric_changed"], 1)
        self.assertEqual(result["counts"]["unchanged"], 1)
        changed = result["metric_changed"][0]
        self.assertEqual(changed["identity"], "PUBLIC_PORTAL:board-20:20")
        self.assertIn("chunk_count", changed["changes"])
        self.assertIn("table_cell_row_count", changed["changes"])

    def test_compares_info_and_false_positive_stability_metrics(self) -> None:
        before_row = row("10", 5, 2)
        before_row["failed_info_check_count"] = 1
        before_row["recommendation_count"] = 1
        before_row["probable_table_false_positive_chunks"] = 3
        before_row["table_false_positive_attention_chunks"] = 3
        after_row = row("10", 5, 2)
        after_row["probable_table_false_positive_chunks"] = 3
        after_row["stable_table_false_positive_chunks"] = 3

        result = compare_reports({"rows": [before_row]}, {"rows": [after_row]})

        changes = result["metric_changed"][0]["changes"]
        self.assertIn("failed_info_check_count", changes)
        self.assertIn("recommendation_count", changes)
        self.assertIn("stable_table_false_positive_chunks", changes)
        self.assertIn("table_false_positive_attention_chunks", changes)


if __name__ == "__main__":
    unittest.main()
