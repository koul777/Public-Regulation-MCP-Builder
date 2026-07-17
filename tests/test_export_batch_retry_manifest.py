from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts.export_batch_retry_manifest import export_retry_manifest, retry_manifest_rows


class ExportBatchRetryManifestTests(unittest.TestCase):
    def test_builds_retry_manifest_rows_from_failed_rows_only(self) -> None:
        report = {
            "rows": [
                {
                    "input_path": "failed.hwp",
                    "filename": "failed.hwp",
                    "status": "failed",
                    "error": "parser failed",
                    "institution_name": "Example",
                    "source_system": "PUBLIC_PORTAL",
                    "source_url": "https://example.test/detail",
                    "source_record_id": "board-1",
                    "source_file_id": "file-1",
                    "source_disclosure_date": "2026.01.01",
                    "source_posted_date": "2026.01.02",
                    "profile_id": "public_portal-etc-law",
                },
                {"input_path": "ok.hwp", "status": "completed"},
            ]
        }

        rows, skipped = retry_manifest_rows(report)

        self.assertEqual(len(rows), 1)
        self.assertEqual(skipped, [])
        self.assertEqual(rows[0]["source_record_id"], "board-1")
        self.assertEqual(rows[0]["previous_error"], "parser failed")

    def test_skips_explicit_non_retryable_failures(self) -> None:
        rows, skipped = retry_manifest_rows(
            {
                "rows": [
                    {
                        "input_path": "scan.pdf",
                        "filename": "scan.pdf",
                        "status": "failed",
                        "error": "OCR may be required",
                        "failure_category": "ocr_required",
                        "retry_recommended": False,
                        "failure_next_action": "run_ocr_then_reprocess",
                    }
                ]
            }
        )

        self.assertEqual(rows, [])
        self.assertEqual(skipped[0]["reason"], "not_retry_recommended")
        self.assertEqual(skipped[0]["failure_category"], "ocr_required")

    def test_can_include_ocr_required_failures_for_ocr_queue(self) -> None:
        rows, skipped = retry_manifest_rows(
            {
                "rows": [
                    {
                        "input_path": "scan.pdf",
                        "filename": "scan.pdf",
                        "status": "failed",
                        "failure_category": "ocr_required",
                        "retry_recommended": False,
                        "ocr_required": True,
                    }
                ]
            },
            include_ocr_required=True,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(skipped, [])
        self.assertEqual(rows[0]["failure_category"], "ocr_required")

    def test_can_require_existing_files_and_report_skipped_rows(self) -> None:
        rows, skipped = retry_manifest_rows(
            {"rows": [{"input_path": "missing.hwp", "filename": "missing.hwp", "status": "failed"}]},
            require_existing_files=True,
        )

        self.assertEqual(rows, [])
        self.assertEqual(skipped[0]["reason"], "input_file_not_found")

    def test_exports_csv_and_json_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            failed = root / "failed.hwp"
            failed.write_bytes(b"hwp")
            batch_report = root / "batch.json"
            batch_report.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-07-03T00:00:00+00:00",
                        "input_count": 2,
                        "failed_count": 1,
                        "rows": [
                            {
                                "input_path": str(failed),
                                "filename": failed.name,
                                "status": "failed",
                                "error": "parse failed",
                                "source_system": "PUBLIC_PORTAL",
                                "source_record_id": "board-1",
                                "source_file_id": "file-1",
                                "profile_id": "public_portal-etc-law",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            out_csv = root / "retry.csv"
            out_json = root / "retry.json"

            summary = export_retry_manifest(
                batch_report,
                out_csv=out_csv,
                out_json=out_json,
                require_existing_files=True,
            )
            with out_csv.open("r", newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(summary["retryable_count"], 1)
        self.assertEqual(summary["skipped_count"], 0)
        self.assertEqual(rows[0]["source_file_id"], "file-1")
        self.assertIn("--manifest-csv", summary["next_command"])

    def test_ocr_queue_next_command_enables_windows_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scan = root / "scan.pdf"
            scan.write_bytes(b"%PDF")
            batch_report = root / "batch.json"
            batch_report.write_text(
                json.dumps(
                    {
                        "rows": [
                            {
                                "input_path": str(scan),
                                "filename": scan.name,
                                "status": "failed",
                                "failure_category": "ocr_required",
                                "ocr_required": True,
                                "retry_recommended": False,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            summary = export_retry_manifest(
                batch_report,
                out_csv=root / "ocr.csv",
                include_ocr_required=True,
                require_existing_files=True,
            )

        self.assertEqual(1, summary["retryable_count"])
        self.assertTrue(summary["include_ocr_required"])
        self.assertIn("--pdf-ocr-backend windows", summary["next_command"])


if __name__ == "__main__":
    unittest.main()
