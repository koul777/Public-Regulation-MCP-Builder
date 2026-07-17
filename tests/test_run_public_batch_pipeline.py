from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_public_batch_pipeline import (
    _write_public_report_artifacts,
    run,
    run_public_batch_pipeline,
)


class RunPublicBatchPipelineTests(unittest.TestCase):
    def test_public_report_artifacts_redact_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_report = {
                "generated_at": "2026-07-10T00:00:00+00:00",
                "input_count": 1,
                "successful_count": 1,
                "failed_count": 0,
                "rows": [
                    {
                        "input_path": "C:\\secret\\sample.hwp",
                        "filename": "sample.hwp",
                        "document_id": "doc-secret",
                        "source_system": "LOCAL",
                        "source_url": "local://sample",
                        "source_record_id": "local-1",
                        "source_file_id": "file-1",
                        "profile_id": "default-public-institution",
                        "status": "completed",
                    }
                ],
            }

            artifacts = _write_public_report_artifacts(
                batch_report,
                source_report_path=root / "batch_quality.json",
                reports_dir=root,
                timestamp="20260710-000000",
            )

            public_report = artifacts["report"]
            row = public_report["rows"][0]
            self.assertEqual(0, public_report["sanitization"]["sensitive_path_leak_count"])
            self.assertNotIn("input_path", row)
            self.assertNotIn("document_id", row)
            self.assertEqual("local-sample", row["source_filename"])
            self.assertTrue(artifacts["json"].is_file())
            self.assertTrue(artifacts["csv"].is_file())
            self.assertTrue(artifacts["markdown"].is_file())

    def test_pipeline_runs_ordered_reports_without_approval_or_indexing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reports = root / "reports"
            data = root / "data"
            source = root / "sample.hwp"
            source.write_bytes(b"sample")
            quality_json = root / "quality.json"
            quality_md = root / "quality.md"
            tables_csv = root / "tables.csv"
            tables_jsonl = root / "tables.jsonl"
            for path in (quality_json, quality_md, tables_csv, tables_jsonl):
                path.write_text("{}", encoding="utf-8")
            batch_summary = {
                "generated_at": "2026-07-10T00:00:00+00:00",
                "input_count": 1,
                "completed_count": 1,
                "skipped_unchanged_count": 0,
                "successful_count": 1,
                "failed_count": 0,
                "failure_category_counts": {},
                "ocr_required_count": 0,
                "ocr_required_page_count": 0,
                "retry_recommended_failed_count": 0,
                "quality_passed_count": 1,
                "average_quality_score": 100.0,
                "failed_info_check_total": 0,
                "recommendation_total": 0,
                "table_false_positive_attention_total": 0,
                "agent_review_estimated_total_tokens_total": 0,
                "agent_review_batch_budget_exceeded": False,
                "rows": [
                    {
                        "input_path": str(source),
                        "filename": source.name,
                        "document_id": "doc-1",
                        "institution_name": "Test Institution",
                        "source_system": "LOCAL",
                        "source_url": "local://sample",
                        "source_record_id": "local-1",
                        "source_file_id": "file-1",
                        "profile_id": "default-public-institution",
                        "status": "completed",
                        "quality_passed": True,
                        "quality_score": 100.0,
                        "quality_json": str(quality_json),
                        "quality_md": str(quality_md),
                        "tables_csv": str(tables_csv),
                        "tables_jsonl": str(tables_jsonl),
                    }
                ],
            }

            with patch("scripts.run_public_batch_pipeline.process_entries", return_value=batch_summary):
                report = run_public_batch_pipeline(
                    inputs=[source],
                    institution_profiles=None,
                    quality_profiles=None,
                    reports_dir=reports,
                    data_dir=data,
                    source_system="LOCAL",
                    strict_institution_profiles=False,
                    strict_quality_profiles=False,
                    alert_log=reports / "alerts.jsonl",
                    timestamp="20260710-000000",
                )

            self.assertTrue(report["passed"])
            self.assertEqual("completed", report["status"])
            self.assertIn("It does not approve chunks", report["safety_note"])
            self.assertEqual(0, report["summary"]["public_report_path_leak_count"])
            self.assertTrue(Path(report["artifacts"]["batch_report_json"]).is_file())
            self.assertTrue(Path(report["artifacts"]["public_report_json"]).is_file())
            self.assertTrue(Path(report["artifacts"]["readiness_json"]).is_file())
            self.assertTrue(Path(report["artifacts"]["alert_json"]).is_file())
            readiness = json.loads(Path(report["artifacts"]["readiness_json"]).read_text(encoding="utf-8"))
            self.assertTrue(readiness["passed"])

    def test_cli_reports_failure_when_inputs_are_missing(self) -> None:
        stdout = io.StringIO()

        exit_code = run(["--json"], stdout=stdout)

        self.assertEqual(2, exit_code)
        payload = json.loads(stdout.getvalue())
        self.assertEqual("failed", payload["status"])
        self.assertIn("Provide at least one input path", payload["error"])


if __name__ == "__main__":
    unittest.main()
