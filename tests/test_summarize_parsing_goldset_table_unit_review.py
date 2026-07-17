from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.summarize_parsing_goldset_table_unit_review import (
    main,
    summarize_parsing_goldset_table_unit_review,
)


class SummarizeParsingGoldsetTableUnitReviewTests(unittest.TestCase):
    def test_summarizes_completed_pending_and_invalid_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_csv = _seed_table_units_csv(root)

            report = summarize_parsing_goldset_table_unit_review(
                table_units_csv=source_csv,
                out_json=root / "reports" / "summary.json",
                out_csv=root / "reports" / "summary.csv",
                out_md=root / "reports" / "summary.md",
                generated_at="2026-07-10T00:00:00+00:00",
            )
            payload = json.loads((root / "reports" / "summary.json").read_text(encoding="utf-8"))
            with (root / "reports" / "summary.csv").open(encoding="utf-8-sig", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            markdown = (root / "reports" / "summary.md").read_text(encoding="utf-8")

        self.assertEqual(4, report["selected_unit_count"])
        self.assertEqual(1, report["completed_unit_count"])
        self.assertEqual(1, report["attention_unit_count"])
        self.assertEqual(1, report["pending_unit_count"])
        self.assertEqual(1, report["invalid_unit_count"])
        self.assertEqual(11, report["required_field_missing_total"])
        self.assertEqual({"source_table_compare": 3, "structured_spot_check": 1}, report["review_priority_counts"])
        self.assertEqual(
            {"article_reference_fragment_loss_candidate": 1, "missing_table_label": 1},
            report["label_review_flag_counts"],
        )
        self.assertEqual(1, report["required_field_missing_counts"]["human_unit_status"])
        self.assertEqual(2, report["required_field_missing_counts"]["human_source_pages_checked"])
        self.assertFalse(report["ready_for_table_score_transfer"])
        self.assertEqual(4, report["issue_count"])
        self.assertIn("does not fill goldset labels", report["safety_note"])
        self.assertIn("reviewed", report["review_contract"]["allowed_human_unit_statuses"])
        self.assertIn("human_reviewer", report["review_contract"]["required_complete_fields"])
        self.assertIn("confirmed", report["review_contract"]["accepted_confirmation_values"])

        by_doc = {row["document_id"]: row for row in csv_rows}
        self.assertEqual("2", by_doc["doc_a"]["unit_count"])
        self.assertEqual("1", by_doc["doc_a"]["completed_unit_count"])
        self.assertEqual("1", by_doc["doc_a"]["invalid_unit_count"])
        self.assertEqual("2", by_doc["doc_a"]["manual_table_count_from_completed_units"])
        self.assertEqual("2", by_doc["doc_a"]["matched_table_count_from_completed_units"])
        self.assertIn("matched-table-count-exceeds-manual", by_doc["doc_a"]["issue_codes"])
        self.assertEqual("1", by_doc["doc_b"]["pending_unit_count"])
        self.assertEqual("1", by_doc["doc_c"]["attention_unit_count"])
        self.assertEqual(4, len(payload["issues"]))
        self.assertIn("human_reviewed_at", payload["review_contract"]["required_complete_fields"])
        self.assertIn("Ready for table score transfer: false", markdown)
        self.assertIn("Review Workload", markdown)
        self.assertIn("Required field missing total: 11", markdown)
        self.assertIn("Review Entry Contract", markdown)

    def test_cli_writes_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_csv = _seed_table_units_csv(root)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--table-units-csv",
                        str(source_csv),
                        "--out-json",
                        str(root / "reports" / "summary.json"),
                        "--out-csv",
                        str(root / "reports" / "summary.csv"),
                        "--out-md",
                        str(root / "reports" / "summary.md"),
                        "--source-compare-only",
                    ]
                )

            payload = json.loads((root / "reports" / "summary.json").read_text(encoding="utf-8"))

        self.assertEqual(0, exit_code)
        self.assertIn('"ok": true', stdout.getvalue())
        self.assertEqual(3, payload["selected_unit_count"])
        self.assertEqual(1, payload["completed_unit_count"])
        self.assertEqual(11, payload["required_field_missing_total"])
        self.assertFalse(payload["ready_for_table_score_transfer"])

    def test_cli_returns_nonzero_with_fail_on_issue_when_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_csv = _seed_table_units_csv(root)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--table-units-csv",
                        str(source_csv),
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
        self.assertIn('"ready_for_table_score_transfer": false', stdout.getvalue())

    def test_completed_unit_requires_reviewer_and_reviewed_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_csv = _write_rows(
                root / "table_units.csv",
                [
                    _row(
                        document_id="doc_a",
                        unit_rank="1",
                        table_unit_key="doc_a | table | p.1-1",
                        review_priority="source_table_compare",
                        status="reviewed",
                        source_checked="true",
                        manual="1",
                        matched="1",
                        row_column_match="true",
                        parentage_ok="true",
                        reviewer="",
                        reviewed_at="not-a-date",
                        label_flags="",
                    )
                ],
            )

            report = summarize_parsing_goldset_table_unit_review(
                table_units_csv=source_csv,
                out_json=root / "reports" / "summary.json",
                out_csv=root / "reports" / "summary.csv",
                out_md=root / "reports" / "summary.md",
                generated_at="2026-07-10T00:00:00+00:00",
            )
            payload = json.loads((root / "reports" / "summary.json").read_text(encoding="utf-8"))

        self.assertEqual(0, report["completed_unit_count"])
        self.assertEqual(1, report["invalid_unit_count"])
        self.assertFalse(report["ready_for_table_score_transfer"])
        self.assertEqual(
            {"reviewer-missing", "reviewed-at-missing-or-invalid"},
            {issue["issue_code"] for issue in payload["issues"]},
        )


def _seed_table_units_csv(root: Path) -> Path:
    rows = [
        _row(
            document_id="doc_a",
            unit_rank="1",
            table_unit_key="doc_a | 별표1 | p.1-1",
            review_priority="source_table_compare",
            status="reviewed",
            source_checked="true",
            manual="2",
            matched="2",
            row_column_match="true",
            parentage_ok="true",
            reviewer="reviewer-a",
            reviewed_at="2026-07-10",
            label_flags="",
        ),
        _row(
            document_id="doc_a",
            unit_rank="2",
            table_unit_key="doc_a | 별표2 | p.2-2",
            review_priority="source_table_compare",
            status="reviewed",
            source_checked="",
            manual="2",
            matched="3",
            row_column_match="",
            parentage_ok="",
            reviewer="reviewer-a",
            reviewed_at="2026-07-10T09:00:00+09:00",
            label_flags="article_reference_fragment_loss_candidate",
        ),
        _row(
            document_id="doc_b",
            unit_rank="3",
            table_unit_key="doc_b | (missing-table-label) | p.3-3",
            review_priority="source_table_compare",
            status="",
            source_checked="",
            manual="",
            matched="",
            row_column_match="",
            parentage_ok="",
            reviewer="",
            reviewed_at="",
            label_flags="missing_table_label",
        ),
        _row(
            document_id="doc_c",
            unit_rank="4",
            table_unit_key="doc_c | 별표4 | p.4-4",
            review_priority="structured_spot_check",
            status="needs_fix",
            source_checked="true",
            manual="1",
            matched="0",
            row_column_match="false",
            parentage_ok="false",
            reviewer="reviewer-c",
            reviewed_at="2026-07-10",
            label_flags="",
        ),
    ]
    return _write_rows(root / "table_units.csv", rows)


def _write_rows(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _row(
    *,
    document_id: str,
    unit_rank: str,
    table_unit_key: str,
    review_priority: str,
    status: str,
    source_checked: str,
    manual: str,
    matched: str,
    row_column_match: str,
    parentage_ok: str,
    reviewer: str,
    reviewed_at: str,
    label_flags: str,
) -> dict[str, str]:
    return {
        "unit_rank": unit_rank,
        "review_priority": review_priority,
        "table_unit_key": table_unit_key,
        "document_id": document_id,
        "source_compare_candidate_count": "1" if review_priority == "source_table_compare" else "0",
        "table_label_review_flags": label_flags,
        "human_source_pages_checked": source_checked,
        "human_unit_status": status,
        "human_manual_table_count": manual,
        "human_matched_table_count": matched,
        "human_row_column_match": row_column_match,
        "human_parentage_ok": parentage_ok,
        "human_reviewer": reviewer,
        "human_reviewed_at": reviewed_at,
    }


if __name__ == "__main__":
    unittest.main()
