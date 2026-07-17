from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.core.config import Settings
from app.schemas.chunk import Chunk
from app.storage.repository import JsonRepository
from scripts.refresh_table_exports import refresh_table_exports


class RefreshTableExportsTests(unittest.TestCase):
    def test_refreshes_table_exports_from_stored_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data")
            repo = JsonRepository(settings)
            chunk = Chunk(
                chunk_id="chunk_table",
                document_id="doc_table",
                source_node_ids=[],
                chunk_type="appendix",
                text="A B",
                metadata={
                    "table_cell_rows": [{"row_index": 0, "cells": ["A", "B"], "raw": "A B"}],
                    "table_classification": "structured_table",
                    "table_confidence": 0.9,
                    "table_review_reason": "header_and_numeric_rows",
                },
            )
            repo.save_processing_result("doc_table", [], [chunk], [])
            tables_jsonl = root / "doc_table.tables.jsonl"
            tables_csv = root / "doc_table.tables.csv"
            batch_report = {
                "input_count": 1,
                "rows": [
                    {
                        "document_id": "doc_table",
                        "filename": "sample.hwp",
                        "tables_jsonl": str(tables_jsonl),
                        "tables_csv": str(tables_csv),
                    }
                ],
            }

            report = refresh_table_exports(batch_report, settings=settings)
            rows = [json.loads(line) for line in tables_jsonl.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(report["refreshed_count"], 1)
        self.assertEqual(report["table_row_count"], 1)
        self.assertEqual(rows[0]["table_classification"], "structured_table")
        self.assertEqual(rows[0]["table_review_reason"], "header_and_numeric_rows")

    def test_refresh_creates_distinct_output_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data")
            repo = JsonRepository(settings)
            chunk = Chunk(
                chunk_id="chunk_table",
                document_id="doc_table",
                source_node_ids=[],
                chunk_type="appendix",
                text="A B",
                metadata={"table_cell_rows": [{"row_index": 0, "cells": ["A", "B"], "raw": "A B"}]},
            )
            repo.save_processing_result("doc_table", [], [chunk], [])
            tables_jsonl = root / "jsonl" / "doc_table.tables.jsonl"
            tables_csv = root / "csv" / "doc_table.tables.csv"
            batch_report = {
                "input_count": 1,
                "rows": [
                    {
                        "document_id": "doc_table",
                        "filename": "sample.hwp",
                        "tables_jsonl": str(tables_jsonl),
                        "tables_csv": str(tables_csv),
                    }
                ],
            }

            report = refresh_table_exports(batch_report, settings=settings)

            self.assertEqual(report["refreshed_count"], 1)
            self.assertTrue(tables_jsonl.is_file())
            self.assertTrue(tables_csv.is_file())


if __name__ == "__main__":
    unittest.main()
