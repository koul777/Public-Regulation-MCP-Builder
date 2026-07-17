from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.filter_batch_quality_report import filter_batch_quality_report


class FilterBatchQualityReportTest(unittest.TestCase):
    def test_excludes_ocr_required_rows_and_preserves_source_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "batch_quality.json"
            source_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-07-12T00:00:00+00:00",
                        "input_count": 2,
                        "successful_count": 1,
                        "failed_count": 1,
                        "rows": [
                            {
                                "filename": "good.hwp",
                                "document_id": "doc_good",
                                "status": "completed",
                                "quality_passed": True,
                                "quality_score": 91.5,
                                "chunk_count": 3,
                            },
                            {
                                "filename": "scan.pdf",
                                "input_path": str(root / "scan.pdf"),
                                "status": "failed",
                                "failure_category": "ocr_required",
                                "ocr_required": True,
                                "error": "No text blocks were extracted.",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            filtered = filter_batch_quality_report(source_path, exclude_ocr_required=True)

        self.assertEqual("batch_quality_filtered", filtered["report_type"])
        self.assertEqual(1, filtered["input_count"])
        self.assertEqual(1, filtered["successful_count"])
        self.assertEqual(0, filtered["failed_count"])
        self.assertEqual(0, filtered["ocr_required_count"])
        self.assertEqual(1, filtered["excluded_count"])
        self.assertEqual(1, filtered["excluded_ocr_required_count"])
        self.assertEqual({"ocr_required": 1}, filtered["excluded_failure_category_counts"])
        self.assertEqual(64, len(filtered["source_batch_report_sha256"]))
        self.assertEqual("scan.pdf", filtered["excluded_rows_sample"][0]["filename"])


if __name__ == "__main__":
    unittest.main()
