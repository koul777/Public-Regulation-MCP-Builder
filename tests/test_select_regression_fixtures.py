from __future__ import annotations

import unittest

from scripts.select_regression_fixtures import select_fixtures


def batch_row(document_id: str, filename: str, false_pos: int, table_rows: int, coverage: float) -> dict:
    return {
        "document_id": document_id,
        "filename": filename,
        "quality_score": 100.0,
        "warning_count": 0,
        "node_count": 10,
        "chunk_count": 5,
        "chunk_to_source_char_ratio": coverage,
        "table_like_chunks": 2,
        "table_cell_row_count": table_rows,
        "probable_table_false_positive_chunks": false_pos,
        "probable_table_extraction_failed_chunks": 0,
    }


class SelectRegressionFixturesTests(unittest.TestCase):
    def test_selects_fixture_buckets_from_batch_and_catalog(self) -> None:
        batch = {
            "generated_at": "2026-01-01T00:00:00+00:00",
            "rows": [
                batch_row("doc_a", "a.hwp", 12, 20, 0.99),
                batch_row("doc_b", "b.pdf", 0, 100, 1.05),
                batch_row("doc_c", "c.hwpx", 7, 10, 0.88),
            ],
        }
        catalog = {
            "generated_at": "2026-01-01T00:00:00+00:00",
            "rows": [
                {"board_no": "1", "file_no": "10", "file_name": "main.hwp"},
                {"board_no": "1", "file_no": "11", "file_name": "compare.hwp"},
                {"board_no": "2", "file_no": "20", "file_name": "single.hwp"},
            ],
        }

        report = select_fixtures(batch, catalog, top_n=2)
        selected = report["selected"]

        self.assertEqual([row["document_id"] for row in selected["table_false_positive_top"]], ["doc_a", "doc_c"])
        self.assertEqual(selected["table_heavy_top"][0]["document_id"], "doc_b")
        self.assertEqual(selected["coverage_low"][0]["document_id"], "doc_c")
        self.assertEqual({row["filename"] for row in selected["format_diversity"]}, {"a.hwp", "b.pdf", "c.hwpx"})
        self.assertEqual(selected["duplicate_board_attachments"][0]["board_no"], "1")
        self.assertEqual(selected["duplicate_board_attachments"][0]["attachment_count"], 2)


if __name__ == "__main__":
    unittest.main()
