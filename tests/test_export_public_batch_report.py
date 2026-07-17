from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.export_public_batch_report import export_public_batch_report, main, to_csv, to_markdown


class ExportPublicBatchReportTests(unittest.TestCase):
    def test_removes_local_paths_and_private_ai_evidence_but_keeps_public_provenance(self) -> None:
        report = {
            "generated_at": "2026-07-03T00:00:00+00:00",
            "input_count": 1,
            "successful_count": 1,
            "failed_count": 0,
            "average_quality_score": 100.0,
            "agent_review_estimated_total_tokens_total": 0,
            "historical_agent_review_api_call_count_total": 3,
            "rows": [
                {
                    "input_path": r"C:\Users\example\Desktop\洹쒖젙吏묓넻?⑸낯_260520.pdf",
                    "filename": "洹쒖젙吏묓넻?⑸낯_260520.pdf",
                    "document_id": "doc_1",
                    "source_system": "PUBLIC_PORTAL",
                    "source_url": "https://example.org/regulations/etc/etcLawList.do",
                    "status": "completed",
                    "quality_score": 100.0,
                    "chunk_count": 10,
                    "agent_review_status": "skipped",
                    "agent_review_estimated_total_tokens": 0,
                    "agent_review_budget_reservation_id": "budget-secret",
                    "agent_review_approval_reference": "approval-secret",
                    "agent_review_payload_hash": "sha256:secret",
                    "agent_review_actual_cost": "1.23",
                    "agent_review_provider_request_id": "request-secret",
                    "agent_review_plan_json": r"C:\example\reg-rag-preprocessor\data\exports\doc_1.agent_review_plan.json",
                    "historical_agent_review_status": "completed",
                    "historical_agent_review_api_call_count": 2,
                    "quality_json": r"C:\example\reg-rag-preprocessor\data\exports\doc_1.quality.json",
                    "quality_md": r"C:\example\reg-rag-preprocessor\data\exports\doc_1.quality.md",
                    "tables_csv": r"C:\example\reg-rag-preprocessor\data\exports\doc_1.tables.csv",
                    "tables_jsonl": r"C:\example\reg-rag-preprocessor\data\exports\doc_1.tables.jsonl",
                }
            ],
        }

        public = export_public_batch_report(report, source_report_path=Path("batch_quality.json"))
        serialized = json.dumps(public, ensure_ascii=False)

        self.assertEqual(public["report_type"], "public_batch_quality")
        self.assertEqual(public["source_generated_at"], "2026-07-03T00:00:00+00:00")
        self.assertEqual(public["rows"][0]["public_row_id"], "public-row-0001")
        self.assertEqual(public["rows"][0]["source_filename"], "洹쒖젙吏묓넻?⑸낯_260520.pdf")
        self.assertEqual(public["rows"][0]["source_url"], "https://example.org/regulations/etc/etcLawList.do")
        self.assertNotIn("document_id", public["rows"][0])
        self.assertNotIn("agent_review_estimated_total_tokens_total", public)
        self.assertNotIn("historical_agent_review_api_call_count_total", public)
        self.assertNotIn("quality_json_file", public["rows"][0])
        self.assertNotIn("tables_csv_file", public["rows"][0])
        self.assertFalse(any(key.startswith("agent_review_") for key in public["rows"][0]))
        self.assertFalse(any(key.startswith("historical_agent_review_") for key in public["rows"][0]))
        self.assertEqual(public["sanitization"]["sensitive_path_leak_count"], 0)
        self.assertNotIn(r"C:\Users\example", serialized)
        self.assertNotIn(r"C:\example", serialized)
        self.assertNotIn("doc_1", serialized)
        self.assertNotIn("budget-secret", serialized)
        self.assertNotIn("approval-secret", serialized)
        self.assertNotIn("sha256:secret", serialized)
        self.assertNotIn("request-secret", serialized)

    def test_exports_csv_and_markdown_without_sensitive_paths(self) -> None:
        public = export_public_batch_report(
            {
                "generated_at": "2026-07-03T00:00:00+00:00",
                "input_count": 1,
                "successful_count": 1,
                "failed_count": 0,
                "rows": [
                    {
                        "input_path": "/Users/example/private/sample.hwp",
                        "filename": "sample.hwp",
                        "document_id": "doc_1",
                        "status": "completed",
                        "quality_score": 99.0,
                        "chunk_count": 2,
                        "agent_review_status": "skipped",
                        "agent_review_estimated_total_tokens": 0,
                    }
                ],
            }
        )

        csv_text = to_csv(public)
        md_text = to_markdown(public)

        self.assertIn("sample.hwp", csv_text)
        self.assertIn("sample.hwp", md_text)
        self.assertIn("public-row-0001", csv_text)
        self.assertIn("public-row-0001", md_text)
        self.assertNotIn("doc_1", csv_text)
        self.assertNotIn("doc_1", md_text)
        self.assertNotIn("/Users/example/private", csv_text)
        self.assertNotIn("/Users/example/private", md_text)

    def test_redacts_local_only_public_provenance(self) -> None:
        public = export_public_batch_report(
            {
                "generated_at": "2026-07-03T00:00:00+00:00",
                "input_count": 1,
                "rows": [
                    {
                        "filename": "integrated-pdf-sample.pdf",
                        "source_system": "LOCAL",
                        "source_url": "local:integrated-pdf-sample",
                        "status": "completed",
                        "quality_score": 100.0,
                    }
                ],
            }
        )
        serialized = json.dumps(public, ensure_ascii=False)

        self.assertEqual(public["rows"][0]["source_filename"], "local-sample")
        self.assertNotIn("source_system", public["rows"][0])
        self.assertNotIn("source_url", public["rows"][0])
        self.assertNotIn("LOCAL", serialized)
        self.assertNotIn("local:integrated-pdf-sample", serialized)
        self.assertNotIn("integrated-pdf-sample.pdf", serialized)

    def test_removes_unexpected_container_path_fields(self) -> None:
        public = export_public_batch_report(
            {
                "generated_at": "2026-07-03T00:00:00+00:00",
                "input_count": 1,
                "rows": [
                    {
                        "input_path": "/app/data/private/source.pdf",
                        "filename": "source.pdf",
                        "document_id": "doc_container",
                        "status": "completed",
                        "debug_path": "/app/data/exports/secret.pdf",
                    }
                ],
            },
            source_report_path=Path("batch_quality.json"),
        )
        serialized = json.dumps(public, ensure_ascii=False)

        self.assertEqual(0, public["sanitization"]["sensitive_path_leak_count"])
        self.assertNotIn("debug_path", public["rows"][0])
        self.assertNotIn("/app/data", serialized)
        self.assertNotIn("doc_container", serialized)

    def test_keeps_public_failure_classification_without_local_path(self) -> None:
        public = export_public_batch_report(
            {
                "generated_at": "2026-07-03T00:00:00+00:00",
                "input_count": 1,
                "successful_count": 0,
                "failed_count": 1,
                "ocr_required_count": 1,
                "rows": [
                    {
                        "input_path": r"C:\example\reg-rag-preprocessor\data\ocr_smoke\blank.pdf",
                        "filename": "blank.pdf",
                        "document_id": "doc_scan",
                        "status": "failed",
                        "error": "OCR may be required",
                        "failure_category": "ocr_required",
                        "ocr_required": True,
                        "ocr_page_count": 1,
                        "retry_recommended": False,
                        "failure_next_action": "run_ocr_then_reprocess",
                    }
                ],
            }
        )

        serialized = json.dumps(public, ensure_ascii=False)

        self.assertEqual(public["rows"][0]["source_filename"], "blank.pdf")
        self.assertEqual(public["rows"][0]["failure_category"], "ocr_required")
        self.assertTrue(public["rows"][0]["ocr_required"])
        self.assertFalse(public["rows"][0]["retry_recommended"])
        self.assertEqual(public["sanitization"]["sensitive_path_leak_count"], 0)
        self.assertNotIn(r"C:\example", serialized)
        self.assertNotIn("doc_1", serialized)

    def test_main_removes_unexpected_top_level_sensitive_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "batch.json"
            out_json = Path(tmp) / "public.json"
            path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-07-03T00:00:00+00:00",
                        "debug_path": r"C:\secret\sample.pdf",
                        "rows": [{"filename": "sample.pdf"}],
                    }
                ),
                encoding="utf-8",
            )

            with patch(
                "sys.argv",
                ["export_public_batch_report.py", "--batch-report", str(path), "--out-json", str(out_json), "--fail-on-leak"],
            ):
                code = main()

            public = json.loads(out_json.read_text(encoding="utf-8-sig"))

        self.assertEqual(code, 0)
        self.assertEqual(0, public["sanitization"]["sensitive_path_leak_count"])
        self.assertNotIn("debug_path", public)


if __name__ == "__main__":
    unittest.main()
