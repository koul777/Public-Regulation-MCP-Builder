from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.summarize_strict_public_readiness_gaps import build_strict_public_readiness_gap_summary, run


class StrictPublicReadinessGapSummaryTests(unittest.TestCase):
    def test_summarizes_failed_checks_and_gap_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = root / "strict.json"
            _write_json(readiness, _strict_payload())

            report = build_strict_public_readiness_gap_summary(readiness_report=readiness)

        self.assertEqual("strict_public_readiness_gap_summary", report["report_type"])
        self.assertFalse(report["passed"])
        self.assertEqual("strict_public_readiness_blocked", report["status"])
        self.assertEqual(3, report["failed_check_count"])
        self.assertEqual(
            {
                "failed_info_check_row_count": 1,
                "failed_info_check_total": 1,
                "recommendation_row_count": 2,
                "recommendation_total": 5,
                "recommendation_row_count_sum": 5,
                "missing_required_field_row_count": 2,
                "missing_required_field_total": 2,
                "missing_artifact_count": 0,
                "ocr_required_row_count": 0,
                "failed_row_count": 0,
            },
            report["gap_counts"],
        )
        self.assertEqual({"apba_id": 2}, report["missing_required_field_counts"])
        self.assertEqual(
            [{"value": "Table-like chunks without structured cell rows should be reviewed.", "count": 2}],
            report["top_recommendations"],
        )
        self.assertEqual(
            ["failed_info_check_triage", "recommendation_triage", "required_metadata_backfill"],
            [item["item_id"] for item in report["remediation_work_items"]],
        )
        self.assertEqual(
            {
                "row_count": 5,
                "issue_type_counts": {
                    "failed_info_check": 1,
                    "recommendation": 2,
                    "missing_required_field": 2,
                },
            },
            report["worklist_summary"],
        )

    def test_cli_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = root / "strict.json"
            out_json = root / "summary.json"
            out_md = root / "summary.md"
            out_worklist_csv = root / "worklist.csv"
            out_worklist_md = root / "worklist.md"
            _write_json(readiness, _strict_payload())
            stdout = io.StringIO()

            exit_code = run(
                [
                    "--readiness-report",
                    str(readiness),
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                    "--out-worklist-csv",
                    str(out_worklist_csv),
                    "--out-worklist-md",
                    str(out_worklist_md),
                    "--json",
                ],
                stdout=stdout,
            )

            payload = json.loads(out_json.read_text(encoding="utf-8"))
            markdown = out_md.read_text(encoding="utf-8")
            worklist_csv = out_worklist_csv.read_text(encoding="utf-8-sig")
            worklist_md = out_worklist_md.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertIn("strict_public_readiness_blocked", stdout.getvalue())
        self.assertEqual("strict_public_readiness_gap_summary", payload["report_type"])
        self.assertIn("Strict Public Readiness Gap Summary", markdown)
        self.assertIn("recommendation_total", markdown)
        self.assertIn("issue_id,issue_type,severity,operator_action", worklist_csv)
        self.assertIn("strict-gap-0001,failed_info_check,high", worklist_csv)
        self.assertIn("strict-gap-0005,missing_required_field,high", worklist_csv)
        self.assertNotIn("input_path", worklist_csv)
        self.assertIn("Strict Public Readiness Gap Worklist", worklist_md)


def _strict_payload() -> dict[str, object]:
    return {
        "passed": False,
        "readiness_profile": "strict",
        "strict_release_evidence": False,
        "checks": [
            {"name": "failed_info_checks_within_limit", "passed": False, "details": {"failed_info_check_total": 1}},
            {"name": "recommendations_within_limit", "passed": False, "details": {"recommendation_total": 5}},
            {"name": "required_row_fields_present", "passed": False, "details": {"missing_count": 2}},
            {"name": "embedding_api_calls_zero", "passed": True, "details": {"api_call_count": 0}},
        ],
        "failures": {
            "failed_rows": [],
            "ocr_required_rows": [],
            "failed_info_check_rows": [
                {
                    "filename": "100_200_rule.pdf",
                    "document_id": "doc-a",
                    "institution_name": "Institution A",
                    "profile_id": "profile-a",
                    "source_record_id": "100",
                    "source_file_id": "200",
                    "input_path": "C:\\secret\\100_200_rule.pdf",
                    "failed_info_check_count": 1,
                }
            ],
            "recommendation_rows": [
                {
                    "filename": "101_201_rule.hwpx",
                    "document_id": "doc-b",
                    "institution_name": "Institution B",
                    "profile_id": "profile-b",
                    "recommendation_count": 3,
                    "top_recommendation": "Table-like chunks without structured cell rows should be reviewed.",
                },
                {
                    "filename": "102_202_rule.hwp",
                    "document_id": "doc-c",
                    "institution_name": "Institution B",
                    "profile_id": "profile-b",
                    "recommendation_count": 2,
                    "top_recommendation": "Table-like chunks without structured cell rows should be reviewed.",
                },
            ],
            "missing_required_fields": [
                {
                    "filename": "103_203_rule.hwp",
                    "document_id": "doc-d",
                    "institution_name": "Institution C",
                    "profile_id": "profile-c",
                    "missing_fields": ["apba_id"],
                },
                {
                    "filename": "104_204_rule.pdf",
                    "document_id": "doc-e",
                    "institution_name": "Institution C",
                    "profile_id": "profile-c",
                    "missing_fields": ["apba_id"],
                },
            ],
            "missing_artifacts": [],
            "reused_ai_evidence_leaks": [],
            "embedding_readiness": [],
        },
    }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
