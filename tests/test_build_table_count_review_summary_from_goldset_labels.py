from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.build_table_count_review_summary_from_goldset_labels import (
    build_table_count_review_summary_from_goldset_labels,
    main,
)
from scripts.validate_parsing_goldset_table_count_transfer import (
    validate_parsing_goldset_table_count_transfer,
)


class BuildTableCountReviewSummaryFromGoldsetLabelsTests(unittest.TestCase):
    def test_ready_reviewed_labels_convert_to_transfer_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels = _write_labels(
                root,
                [
                    _label("doc_a", "reviewed", "2", "2"),
                    _label("doc_b", "confirmed", "1", "0"),
                ],
            )

            report = build_table_count_review_summary_from_goldset_labels(
                labels_csv=labels,
                out_json=root / "reports" / "summary.json",
                out_csv=root / "reports" / "summary.csv",
                out_md=root / "reports" / "summary.md",
                generated_at="2026-07-13T00:00:00+00:00",
            )
            payload = json.loads((root / "reports" / "summary.json").read_text(encoding="utf-8"))
            with (root / "reports" / "summary.csv").open(encoding="utf-8-sig", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            markdown = (root / "reports" / "summary.md").read_text(encoding="utf-8")

            validation = validate_parsing_goldset_table_count_transfer(
                labels_csv=labels,
                table_review_summary_json=root / "reports" / "summary.json",
                out_json=root / "reports" / "validation.json",
                out_csv=root / "reports" / "validation.csv",
                out_md=root / "reports" / "validation.md",
            )

        self.assertTrue(report["ready_for_table_score_transfer"])
        self.assertEqual("manual_goldset_labels", report["source_review_basis"])
        self.assertTrue(report["derived_from_goldset_labels"])
        self.assertFalse(report["source_compare_only"])
        self.assertEqual(2, report["selected_unit_count"])
        self.assertEqual(2, report["completed_unit_count"])
        self.assertEqual(0, report["pending_unit_count"])
        self.assertEqual(0, report["invalid_unit_count"])
        self.assertEqual({"reviewed": 1, "confirmed": 1}, report["status_counts"])
        self.assertEqual({}, report["required_field_missing_counts"])
        self.assertEqual(2, len(payload["document_summaries"]))
        self.assertEqual("2", csv_rows[0]["manual_table_count_from_completed_units"])
        self.assertIn("Ready for table score transfer: true", markdown)
        self.assertTrue(validation["passed"])
        self.assertEqual("none", validation["root_cause_summary"]["primary_blocker"])

    def test_blocks_pending_invalid_or_mismatched_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels = _write_labels(
                root,
                [
                    _label("doc_pending", "pending", "1", "1"),
                    _label("doc_bad_status", "needs_fix", "1", "1"),
                    _label("doc_exceeds", "reviewed", "1", "2"),
                    _label("doc_missing", "reviewed", "", ""),
                ],
            )

            report = build_table_count_review_summary_from_goldset_labels(
                labels_csv=labels,
                out_json=root / "reports" / "summary.json",
                out_csv=root / "reports" / "summary.csv",
                out_md=root / "reports" / "summary.md",
            )
            payload = json.loads((root / "reports" / "summary.json").read_text(encoding="utf-8"))

        self.assertFalse(report["ready_for_table_score_transfer"])
        self.assertEqual(1, report["pending_unit_count"])
        self.assertEqual(3, report["invalid_unit_count"])
        issue_codes = {issue["issue_code"] for issue in payload["issues"]}
        self.assertIn("label-status-not-accepted", issue_codes)
        self.assertIn("matched-table-count-exceeds-manual", issue_codes)
        self.assertIn("manual-table-count-missing-or-invalid", issue_codes)
        by_doc = {row["document_id"]: row for row in payload["document_summaries"]}
        self.assertEqual("1", by_doc["doc_pending"]["pending_unit_count"])
        self.assertEqual("1", by_doc["doc_exceeds"]["invalid_unit_count"])

    def test_cli_writes_outputs_and_fail_on_issue_returns_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels = _write_labels(root, [_label("doc_a", "reviewed", "1", "2")])
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--labels-csv",
                        str(labels),
                        "--out-json",
                        str(root / "reports" / "summary.json"),
                        "--out-csv",
                        str(root / "reports" / "summary.csv"),
                        "--out-md",
                        str(root / "reports" / "summary.md"),
                        "--fail-on-issue",
                    ]
                )

            self.assertEqual(2, exit_code)
            self.assertIn('"ok": false', stdout.getvalue())
            self.assertTrue((root / "reports" / "summary.json").is_file())
            self.assertTrue((root / "reports" / "summary.csv").is_file())
            self.assertTrue((root / "reports" / "summary.md").is_file())


def _write_labels(root: Path, rows: list[dict[str, str]]) -> Path:
    path = root / "labels.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _label(document_id: str, status: str, manual: str, matched: str) -> dict[str, str]:
    return {
        "document_id": document_id,
        "label_status": status,
        "manual_table_count": manual,
        "matched_table_count": matched,
        "reviewer": "reviewer-a",
        "reviewed_at": "2026-07-13",
    }


if __name__ == "__main__":
    unittest.main()
