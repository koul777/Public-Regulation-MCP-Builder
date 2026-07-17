from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts.summarize_review_triage_labels import (
    build_review_triage_label_summary,
    summarize_review_triage_labels,
)


class SummarizeReviewTriageLabelsTests(unittest.TestCase):
    def test_summarizes_completed_labels_and_unlabeled_groups(self) -> None:
        rows = [
            {
                "review_category": "table_extraction_blocker",
                "human_label": "needs_parser_fix",
                "human_notes": "lost table columns",
                "document_id": "doc-a",
                "institution_name": "Institution A",
                "group_size": "3",
                "label_options": "needs_parser_fix | acceptable_linearized_table",
            },
            {
                "review_category": "table_extraction_blocker",
                "human_label": "acceptable_linearized_table",
                "human_notes": "",
                "document_id": "doc-b",
                "institution_name": "Institution B",
                "group_size": "1",
                "label_options": "needs_parser_fix | acceptable_linearized_table",
            },
            {
                "review_category": "table_extraction_blocker",
                "human_label": "",
                "human_notes": "",
                "document_id": "doc-c",
                "institution_name": "Institution C",
                "group_size": "2",
                "label_options": "needs_parser_fix | acceptable_linearized_table",
            },
            {
                "review_category": "hwp_binary_geometry_review",
                "human_label": " needs_parser_fix ",
                "human_notes": "form geometry lost",
                "document_id": "doc-d",
                "institution_name": "Institution D",
                "group_size": "4",
                "label_options": "real_table_geometry | needs_parser_fix",
            },
            {
                "review_category": "hwp_binary_geometry_review",
                "human_label": "",
                "human_notes": "",
                "document_id": "doc-e",
                "institution_name": "Institution E",
                "group_size": "",
                "label_options": "real_table_geometry | needs_parser_fix",
            },
        ]

        report = summarize_review_triage_labels(
            rows,
            source_csv=Path("triage.csv"),
            generated_at="2026-07-09T00:00:00Z",
        )

        self.assertEqual("review_triage_label_summary", report["report_type"])
        self.assertEqual(5, report["row_count"])
        self.assertEqual(3, report["labeled_count"])
        self.assertEqual(2, report["unlabeled_count"])
        self.assertEqual(11, report["total_group_size"])
        self.assertEqual(8, report["labeled_group_size"])
        self.assertEqual(3, report["unlabeled_group_size"])
        self.assertEqual(
            {"needs_parser_fix": 2, "acceptable_linearized_table": 1},
            report["human_label_counts"],
        )
        self.assertEqual(
            {"needs_parser_fix": 7, "acceptable_linearized_table": 1},
            report["group_size_by_label"],
        )
        self.assertEqual(
            {"needs_parser_fix": 1, "acceptable_linearized_table": 1},
            report["category_label_counts"]["table_extraction_blocker"],
        )
        table_summary = self._summary_for(report, "table_extraction_blocker")
        self.assertEqual(1, table_summary["unlabeled_count"])
        self.assertEqual(2, table_summary["unlabeled_group_size"])
        self.assertEqual(3, table_summary["document_count"])
        self.assertIn("does not approve chunks", report["safety_note"])
        self.assertTrue(report["next_recommended_actions"])
        self.assertIn("table_extraction_blocker", report["next_recommended_actions"][1])
        self.assertIn("group_size 2", report["next_recommended_actions"][1])

    def test_build_summary_writes_json_and_markdown_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            triage_csv = root / "triage.csv"
            out_json = root / "summary.json"
            out_md = root / "summary.md"
            self._write_csv(
                triage_csv,
                [
                    {
                        "review_category": "ocr_or_encoding_blocker",
                        "human_label": "true_ocr_or_encoding",
                        "human_notes": "scan-only source",
                        "document_id": "doc-1",
                        "institution_name": "Institution A",
                        "group_size": "5",
                        "label_options": "true_ocr_or_encoding | false_positive",
                    },
                    {
                        "review_category": "ocr_or_encoding_blocker",
                        "human_label": "",
                        "human_notes": "",
                        "document_id": "doc-2",
                        "institution_name": "Institution B",
                        "group_size": "2",
                        "label_options": "true_ocr_or_encoding | false_positive",
                    },
                ],
            )

            report = build_review_triage_label_summary(
                triage_csv=triage_csv,
                out_json=out_json,
                out_md=out_md,
                generated_at="2026-07-09T00:00:00Z",
            )

            loaded = json.loads(out_json.read_text(encoding="utf-8"))
            markdown = out_md.read_text(encoding="utf-8")
            self.assertEqual(report["human_label_counts"], loaded["human_label_counts"])
            self.assertEqual({"true_ocr_or_encoding": 5}, loaded["group_size_by_label"])
            self.assertIn("# Review Triage Label Summary", markdown)
            self.assertIn("true_ocr_or_encoding", markdown)
            self.assertIn("VectorDB", markdown)
            self.assertEqual(
                {"triage.csv", "summary.json", "summary.md"},
                {path.name for path in root.iterdir()},
            )

    def test_missing_optional_columns_are_handled_as_unlabeled_or_uncategorized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            triage_csv = root / "triage.csv"
            self._write_csv(
                triage_csv,
                [
                    {
                        "review_category": "",
                        "human_label": "false_positive",
                    },
                    {
                        "review_category": "table_extraction_blocker",
                        "human_label": "",
                    },
                ],
            )

            report = summarize_review_triage_labels(
                self._read_csv(triage_csv),
                generated_at="2026-07-09T00:00:00Z",
            )

        self.assertEqual(2, report["row_count"])
        self.assertEqual(1, report["labeled_count"])
        self.assertEqual(1, report["unlabeled_count"])
        self.assertEqual({"false_positive": 1}, report["group_size_by_label"])
        self.assertEqual({"false_positive": 1}, report["category_label_counts"]["uncategorized"])

    def _summary_for(self, report: dict[str, object], category: str) -> dict[str, object]:
        for summary in report["category_summaries"]:  # type: ignore[index]
            if summary["review_category"] == category:
                return summary
        raise AssertionError(f"Missing category summary: {category}")

    def _write_csv(self, path: Path, rows: list[dict[str, str]]) -> None:
        fieldnames = sorted({key for row in rows for key in row})
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def _read_csv(self, path: Path) -> list[dict[str, str]]:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]


if __name__ == "__main__":
    unittest.main()
