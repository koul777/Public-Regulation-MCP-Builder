from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.validate_parsing_goldset_table_count_transfer import (
    main,
    validate_parsing_goldset_table_count_transfer,
)


class ValidateParsingGoldsetTableCountTransferTests(unittest.TestCase):
    def test_passes_when_goldset_counts_match_ready_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels = _write_labels(root, [("doc_a", "2", "2"), ("doc_b", "1", "0")])
            summary = _write_summary(
                root,
                ready=True,
                source_compare_only=False,
                rows=[
                    _summary_row("doc_a", manual="2", matched="2", units="2", completed="2"),
                    _summary_row("doc_b", manual="1", matched="0", units="1", completed="1"),
                ],
            )

            report = validate_parsing_goldset_table_count_transfer(
                labels_csv=labels,
                table_review_summary_json=summary,
                out_json=root / "reports" / "validation.json",
                out_csv=root / "reports" / "validation.csv",
                out_md=root / "reports" / "validation.md",
                generated_at="2026-07-10T00:00:00+00:00",
            )
            markdown = (root / "reports" / "validation.md").read_text(encoding="utf-8")

        self.assertTrue(report["passed"])
        self.assertEqual(0, report["blocker_count"])
        self.assertIn("Passed: true", markdown)

    def test_blocks_not_ready_summary_and_mismatched_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels = _write_labels(root, [("doc_a", "", ""), ("doc_b", "2", "2")])
            summary = _write_summary(
                root,
                ready=False,
                source_compare_only=False,
                rows=[
                    _summary_row("doc_a", manual="2", matched="2", units="2", completed="0"),
                    _summary_row("doc_b", manual="1", matched="0", units="1", completed="1"),
                ],
            )

            report = validate_parsing_goldset_table_count_transfer(
                labels_csv=labels,
                table_review_summary_json=summary,
                out_json=root / "reports" / "validation.json",
                out_csv=root / "reports" / "validation.csv",
                out_md=root / "reports" / "validation.md",
            )
            payload = json.loads((root / "reports" / "validation.json").read_text(encoding="utf-8"))

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("table-review-summary-not-ready", codes)
        self.assertIn("manual-table-count-missing-or-invalid", codes)
        self.assertIn("matched-table-count-mismatch", codes)
        self.assertEqual("table_unit_human_review_pending", report["root_cause_summary"]["primary_blocker"])
        self.assertEqual("complete_table_unit_human_review", report["root_cause_summary"]["recommended_next_step"])
        self.assertEqual(0, report["root_cause_summary"]["source_invalid_unit_count"])
        self.assertEqual(2, report["root_cause_summary"]["blocked_row_count"])
        self.assertEqual(1, report["root_cause_summary"]["missing_label_document_count"])
        self.assertEqual(1, report["root_cause_summary"]["mismatch_document_count"])
        self.assertEqual(2, len(payload["rows"]))
        self.assertEqual(report["root_cause_summary"], payload["root_cause_summary"])

    def test_blocks_source_compare_only_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels = _write_labels(root, [("doc_a", "2", "2")])
            summary = _write_summary(
                root,
                ready=True,
                source_compare_only=True,
                rows=[_summary_row("doc_a", manual="2", matched="2", units="2", completed="2")],
            )

            report = validate_parsing_goldset_table_count_transfer(
                labels_csv=labels,
                table_review_summary_json=summary,
                out_json=root / "reports" / "validation.json",
                out_csv=root / "reports" / "validation.csv",
                out_md=root / "reports" / "validation.md",
            )

        self.assertFalse(report["passed"])
        self.assertIn("table-summary-source-compare-only", {finding["code"] for finding in report["findings"]})

    def test_blocks_label_document_missing_from_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels = _write_labels(root, [("doc_a", "2", "2"), ("doc_b", "1", "1")])
            summary = _write_summary(
                root,
                ready=True,
                source_compare_only=False,
                rows=[_summary_row("doc_a", manual="2", matched="2", units="2", completed="2")],
            )

            report = validate_parsing_goldset_table_count_transfer(
                labels_csv=labels,
                table_review_summary_json=summary,
                out_json=root / "reports" / "validation.json",
                out_csv=root / "reports" / "validation.csv",
                out_md=root / "reports" / "validation.md",
            )
            payload = json.loads((root / "reports" / "validation.json").read_text(encoding="utf-8"))

        self.assertFalse(report["passed"])
        codes = {finding["code"] for finding in report["findings"]}
        self.assertIn("summary-document-missing", codes)
        rows = {row["document_id"]: row for row in payload["rows"]}
        self.assertEqual("blocked", rows["doc_b"]["status"])
        self.assertIn("summary-document-missing", rows["doc_b"]["issues"])
        self.assertEqual(1, report["root_cause_summary"]["summary_invalid_document_count"])

    def test_cli_returns_nonzero_with_fail_on_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels = _write_labels(root, [("doc_a", "", "")])
            summary = _write_summary(
                root,
                ready=False,
                source_compare_only=False,
                rows=[_summary_row("doc_a", manual="0", matched="0", units="1", completed="0")],
            )
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--labels-csv",
                        str(labels),
                        "--table-review-summary-json",
                        str(summary),
                        "--out-json",
                        str(root / "reports" / "validation.json"),
                        "--out-csv",
                        str(root / "reports" / "validation.csv"),
                        "--out-md",
                        str(root / "reports" / "validation.md"),
                        "--fail-on-issue",
                    ]
                )

        self.assertEqual(2, exit_code)
        self.assertIn('"ok": false', stdout.getvalue())


def _write_labels(root: Path, rows: list[tuple[str, str, str]]) -> Path:
    path = root / "labels.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["document_id", "manual_table_count", "matched_table_count"])
        writer.writeheader()
        for document_id, manual, matched in rows:
            writer.writerow(
                {
                    "document_id": document_id,
                    "manual_table_count": manual,
                    "matched_table_count": matched,
                }
            )
    return path


def _write_summary(
    root: Path,
    *,
    ready: bool,
    source_compare_only: bool,
    rows: list[dict[str, str]],
) -> Path:
    payload = {
        "report_type": "parsing_goldset_table_unit_review_summary",
        "ready_for_table_score_transfer": ready,
        "source_compare_only": source_compare_only,
        "pending_unit_count": 0 if ready else 1,
        "invalid_unit_count": 0,
        "document_summaries": rows,
    }
    path = root / "summary.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _summary_row(document_id: str, *, manual: str, matched: str, units: str, completed: str) -> dict[str, str]:
    return {
        "document_id": document_id,
        "manual_table_count_from_completed_units": manual,
        "matched_table_count_from_completed_units": matched,
        "unit_count": units,
        "completed_unit_count": completed,
    }


if __name__ == "__main__":
    unittest.main()
