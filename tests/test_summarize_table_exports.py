from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.summarize_table_exports import summarize_table_exports


class SummarizeTableExportsTests(unittest.TestCase):
    def test_summarizes_table_exports_from_batch_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            table_path = root / "doc.tables.jsonl"
            table_rows = [
                {
                    "row_kind": "cell",
                    "cell_count": 3,
                    "table_classification": "structured_table",
                },
                {
                    "row_kind": "raw",
                    "cell_count": 1,
                    "table_classification": "",
                },
            ]
            table_path.write_text("\n".join(json.dumps(row) for row in table_rows) + "\n", encoding="utf-8")
            batch_report = {
                "generated_at": "2026-07-03T00:00:00Z",
                "input_count": 1,
                "rows": [
                    {
                        "document_id": "doc_1",
                        "filename": "sample.hwp",
                        "tables_jsonl": str(table_path),
                        "table_cell_row_count": 1,
                    }
                ],
            }

            report = summarize_table_exports(batch_report, base_dir=root)

        self.assertEqual(report["documents_with_tables"], 1)
        self.assertEqual(report["table_row_count"], 2)
        self.assertEqual(report["row_kind_counts"], {"cell": 1, "raw": 1})
        self.assertEqual(report["table_classification_counts"]["structured_table"], 1)
        self.assertEqual(report["table_classification_counts"]["unclassified"], 1)
        self.assertEqual(report["cell_count_histogram"], {"1": 1, "3": 1})
        self.assertEqual(report["documents"][0]["max_cell_count"], 3)
        self.assertEqual(report["expected_table_cell_row_count"], 1)
        self.assertEqual(report["actual_structured_table_row_count"], 1)
        self.assertEqual(report["table_cell_row_mismatch_count"], 0)

    def test_fails_when_table_export_path_is_missing(self) -> None:
        batch_report = {
            "input_count": 1,
            "rows": [
                {
                    "document_id": "doc_missing",
                    "filename": "missing.hwp",
                    "tables_jsonl": "missing.tables.jsonl",
                }
            ],
        }

        with self.assertRaisesRegex(FileNotFoundError, "Missing table export files"):
            summarize_table_exports(batch_report, base_dir=Path("does-not-exist"))

    def test_can_report_missing_exports_when_allowed(self) -> None:
        batch_report = {
            "input_count": 1,
            "rows": [
                {
                    "document_id": "doc_missing",
                    "filename": "missing.hwp",
                    "tables_jsonl": "missing.tables.jsonl",
                }
            ],
        }

        report = summarize_table_exports(batch_report, base_dir=Path("does-not-exist"), allow_missing_exports=True)

        self.assertEqual(report["table_row_count"], 0)
        self.assertEqual(report["missing_export_count"], 1)
        self.assertEqual(report["missing_exports"][0]["document_id"], "doc_missing")

    def test_reports_table_cell_row_count_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            table_path = root / "doc.tables.jsonl"
            table_path.write_text(
                json.dumps({"row_kind": "cell", "cell_count": 2}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            batch_report = {
                "input_count": 1,
                "rows": [
                    {
                        "document_id": "doc_1",
                        "filename": "sample.hwp",
                        "tables_jsonl": str(table_path),
                        "table_cell_row_count": 2,
                    }
                ],
            }

            report = summarize_table_exports(batch_report, base_dir=root)

        self.assertEqual(report["expected_table_cell_row_count"], 2)
        self.assertEqual(report["actual_structured_table_row_count"], 1)
        self.assertEqual(report["table_cell_row_mismatch_count"], 1)
        self.assertEqual(report["table_cell_row_mismatch_samples"][0]["table_cell_row_delta"], -1)


if __name__ == "__main__":
    unittest.main()
