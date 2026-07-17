from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.batch_failure_alerting import build_failure_alert
from scripts.emit_batch_failure_alert import emit_batch_failure_alert


class EmitBatchFailureAlertTests(unittest.TestCase):
    def test_builds_public_alert_without_local_paths(self) -> None:
        alert = build_failure_alert(
            {
                "generated_at": "2026-07-03T00:00:00+00:00",
                "input_count": 2,
                "successful_count": 1,
                "failed_count": 1,
                "failure_category_counts": {"ocr_required": 1},
                "ocr_required_count": 1,
                "ocr_required_page_count": 3,
                "retry_recommended_failed_count": 0,
                "rows": [
                    {
                        "input_path": "C:\\secret\\scan.pdf",
                        "filename": "scan.pdf",
                        "document_name": "Scan",
                        "status": "failed",
                        "error": "No text blocks were extracted from the PDF file. OCR may be required.",
                        "failure_category": "ocr_required",
                        "ocr_required": True,
                        "ocr_page_count": 3,
                        "retry_recommended": False,
                        "failure_next_action": "run_ocr_then_reprocess",
                    },
                    {"filename": "ok.hwp", "status": "completed"},
                ],
            },
            batch_report_file="batch_quality_smoke.json",
        )

        self.assertEqual(alert["status"], "needs_attention")
        self.assertEqual(alert["severity"], "warning")
        self.assertEqual(alert["api_call_count"], 0)
        self.assertEqual(alert["summary"]["ocr_required_count"], 1)
        self.assertEqual(len(alert["items"]), 1)
        self.assertNotIn("input_path", alert["items"][0])
        self.assertEqual(alert["items"][0]["filename"], "scan.pdf")
        self.assertEqual(alert["recommended_actions"][0]["action_type"], "export_ocr_manifest")

    def test_marks_ocr_only_readiness_failure_as_warning(self) -> None:
        alert = build_failure_alert(
            {
                "failed_count": 1,
                "failure_category_counts": {"ocr_required": 1},
                "ocr_required_count": 1,
                "retry_recommended_failed_count": 0,
                "rows": [
                    {
                        "filename": "scan.pdf",
                        "status": "failed",
                        "failure_category": "ocr_required",
                        "retry_recommended": False,
                        "failure_next_action": "run_ocr_then_reprocess",
                        "error": "OCR may be required",
                    }
                ],
            },
            readiness_report={
                "status": "needs_attention",
                "passed": False,
                "checks": [
                    {"name": "no_failed_rows", "passed": False},
                    {"name": "no_ocr_required_rows", "passed": False},
                ],
            },
        )

        self.assertEqual(alert["severity"], "warning")

    def test_marks_parser_failures_as_critical(self) -> None:
        alert = build_failure_alert(
            {
                "failed_count": 1,
                "failure_category_counts": {"parser_error": 1},
                "ocr_required_count": 0,
                "retry_recommended_failed_count": 0,
                "rows": [
                    {
                        "filename": "bad.hwp",
                        "status": "failed",
                        "failure_category": "parser_error",
                        "retry_recommended": False,
                        "failure_next_action": "inspect_or_convert_source_file",
                        "error": "Failed to parse",
                    }
                ],
            }
        )

        self.assertEqual(alert["severity"], "critical")
        self.assertEqual(alert["recommended_actions"][0]["action_type"], "review_failed_rows")

    def test_surfaces_source_selection_warnings_from_readiness_report(self) -> None:
        alert = build_failure_alert(
            {
                "generated_at": "2026-07-03T00:00:00+00:00",
                "input_count": 1,
                "successful_count": 1,
                "failed_count": 0,
                "failure_category_counts": {},
                "ocr_required_count": 0,
                "retry_recommended_failed_count": 0,
                "rows": [{"filename": "ok.hwp", "status": "completed"}],
            },
            batch_report_file="batch_quality_public_portal.json",
            readiness_report={
                "status": "needs_attention",
                "passed": False,
                "checks": [
                    {
                        "name": "source_selection_has_no_warnings",
                        "passed": False,
                        "details": {"warning_count": 2},
                    }
                ],
                "failures": {
                    "source_selection_warnings": [
                        {
                            "filename": "rule.hwp",
                            "input_path": "C:\\secret\\rule.hwp",
                            "document_id": "doc-1",
                            "institution_name": "Sample Institution",
                            "apba_id": "A1234",
                            "profile_id": "public_portal-public",
                            "source_record_id": "3050658",
                            "source_file_id": "98765",
                            "selection_warning": "selected_supported_file_is_not_latest_public_portal_file",
                            "selection_policy": "latest_supported_fallback",
                            "selected_latest_file": "False",
                            "latest_file_no": "98766",
                            "latest_file_name": "latest-rules.zip",
                            "latest_file_ext": ".zip",
                        }
                    ]
                },
            },
        )

        self.assertEqual(alert["status"], "needs_attention")
        self.assertEqual(alert["severity"], "critical")
        self.assertEqual(alert["summary"]["source_selection_warning_count"], 2)
        self.assertEqual(len(alert["source_selection_warning_samples"]), 1)
        sample = alert["source_selection_warning_samples"][0]
        self.assertNotIn("input_path", sample)
        self.assertEqual(sample["source_file_id"], "98765")
        self.assertEqual(sample["latest_file_no"], "98766")
        self.assertEqual(
            sample["selection_warning"],
            "selected_supported_file_is_not_latest_public_portal_file",
        )
        self.assertIn(
            "review_source_selection_warnings",
            [action["action_type"] for action in alert["recommended_actions"]],
        )

    def test_exports_alert_and_appends_jsonl_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_report = root / "batch.json"
            batch_report.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-07-03T00:00:00+00:00",
                        "input_count": 1,
                        "failed_count": 1,
                        "failure_category_counts": {"ocr_required": 1},
                        "ocr_required_count": 1,
                        "ocr_required_page_count": 1,
                        "retry_recommended_failed_count": 0,
                        "rows": [
                            {
                                "input_path": str(root / "scan.pdf"),
                                "filename": "scan.pdf",
                                "status": "failed",
                                "failure_category": "ocr_required",
                                "ocr_required": True,
                                "ocr_page_count": 1,
                                "retry_recommended": False,
                                "failure_next_action": "run_ocr_then_reprocess",
                                "error": "OCR may be required",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            out_json = root / "alert.json"
            alert_log = root / "alerts.jsonl"

            alert = emit_batch_failure_alert(
                batch_report,
                out_json=out_json,
                alert_log=alert_log,
                include_local_paths=True,
            )

            self.assertTrue(out_json.is_file())
            self.assertTrue(alert_log.is_file())
            self.assertEqual(alert["items"][0]["input_path"], str(root / "scan.pdf"))
            lines = alert_log.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)

    @patch("scripts.emit_batch_failure_alert._post_webhook")
    def test_webhook_delivery_is_optional(self, mock_post) -> None:
        mock_post.return_value = {"delivered": True, "status_code": 200}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_report = root / "batch.json"
            batch_report.write_text(
                json.dumps(
                    {
                        "failed_count": 0,
                        "rows": [],
                    }
                ),
                encoding="utf-8",
            )
            alert = emit_batch_failure_alert(
                batch_report,
                out_json=root / "alert.json",
                webhook_url="https://example.test/hook",
            )

        self.assertEqual(alert["status"], "ok")
        self.assertEqual(alert["webhook_delivery"]["delivered"], True)
        mock_post.assert_called_once()
