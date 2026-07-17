from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts.export_ocr_manifest import export_ocr_manifest, ocr_manifest_rows


class ExportOCRManifestTests(unittest.TestCase):
    def test_builds_manifest_rows_from_ocr_failures_only(self) -> None:
        rows = ocr_manifest_rows(
            {
                "rows": [
                    {
                        "input_path": "scan.pdf",
                        "filename": "scan.pdf",
                        "document_id": "doc_scan",
                        "status": "failed",
                        "error": "OCR may be required",
                        "failure_category": "ocr_required",
                        "ocr_required": True,
                        "ocr_page_count": 3,
                        "failure_next_action": "run_ocr_then_reprocess",
                    },
                    {"input_path": "bad.hwp", "status": "failed", "failure_category": "parser_error"},
                ]
            }
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["filename"], "scan.pdf")
        self.assertEqual(rows[0]["ocr_page_count"], 3)
        self.assertEqual(rows[0]["previous_error"], "OCR may be required")

    def test_exports_cost_summary_without_calling_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_report = root / "batch.json"
            batch_report.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-07-03T00:00:00+00:00",
                        "input_count": 2,
                        "failed_count": 1,
                        "rows": [
                            {
                                "input_path": str(root / "scan.pdf"),
                                "filename": "scan.pdf",
                                "document_id": "doc_scan",
                                "status": "failed",
                                "error": "OCR may be required",
                                "failure_category": "ocr_required",
                                "ocr_required": True,
                                "ocr_page_count": 5,
                                "source_system": "PUBLIC_PORTAL",
                                "source_record_id": "board-1",
                                "source_file_id": "file-1",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            out_csv = root / "ocr.csv"
            out_json = root / "ocr.json"

            summary = export_ocr_manifest(
                batch_report,
                out_csv=out_csv,
                out_json=out_json,
                price_per_page=0.03,
                budget=1.0,
            )
            with out_csv.open("r", newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(summary["ocr_required_count"], 1)
        self.assertEqual(summary["known_page_count"], 1)
        self.assertEqual(summary["estimated_ocr_pages"], 5)
        self.assertEqual(summary["estimated_total_cost"], 0.15)
        self.assertFalse(summary["budget_exceeded"])
        self.assertEqual(summary["api_call_count"], 0)
        self.assertEqual(rows[0]["source_file_id"], "file-1")


if __name__ == "__main__":
    unittest.main()
