from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.build_parsing_goldset_table_review_batches import (
    build_parsing_goldset_table_review_batches,
    main,
)


class BuildParsingGoldsetTableReviewBatchesTests(unittest.TestCase):
    def test_batches_table_units_by_document_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_csv = _seed_table_units_csv(root)

            report = build_parsing_goldset_table_review_batches(
                table_units_csv=source_csv,
                out_json=root / "reports" / "batches.json",
                out_csv=root / "reports" / "batches.csv",
                out_md=root / "reports" / "batches.md",
                source_compare_only=True,
                max_units_per_batch=2,
                generated_at="2026-07-10T00:00:00+00:00",
            )
            payload = json.loads((root / "reports" / "batches.json").read_text(encoding="utf-8"))
            with (root / "reports" / "batches.csv").open(encoding="utf-8-sig", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            markdown = (root / "reports" / "batches.md").read_text(encoding="utf-8")

        self.assertEqual(4, report["selected_unit_count"])
        self.assertEqual(3, report["batch_count"])
        self.assertEqual(2, report["document_count"])
        self.assertEqual({"doc_a": 2, "doc_b": 1}, report["document_batch_counts"])
        self.assertEqual({"doc_a": 3, "doc_b": 1}, report["document_unit_counts"])
        self.assertEqual(4, report["burndown_summary"]["high_attention_unit_count"])
        self.assertEqual({"source_table_compare": 4}, report["burndown_summary"]["priority_unit_counts"])
        self.assertEqual({"missing_table_label": 1}, report["burndown_summary"]["label_flag_unit_counts"])
        self.assertEqual({".pdf": 4}, report["burndown_summary"]["extension_unit_counts"])
        self.assertEqual(
            {"document_id": "doc_a", "unit_count": 3},
            report["burndown_summary"]["top_documents_by_unit_count"][0],
        )
        self.assertIn("does not fill goldset labels", report["safety_note"])
        self.assertEqual(3, len(payload["batches"]))
        self.assertEqual(report["burndown_summary"], payload["burndown_summary"])
        self.assertEqual("doc_a", csv_rows[0]["document_id"])
        self.assertEqual("2", csv_rows[0]["unit_count"])
        self.assertTrue(csv_rows[0]["table_review_batch_id"].startswith("table-review-doc-a-001-"))
        self.assertEqual("sources/doc_a.pdf", csv_rows[0]["source_path"])
        self.assertEqual("1-1; 2-2", csv_rows[0]["source_page_ranges"])
        self.assertIn("missing_table_label=1", csv_rows[0]["label_review_flag_counts"])
        self.assertEqual("", csv_rows[0]["human_batch_status"])
        self.assertIn("Parsing Goldset Table Review Batches", markdown)
        self.assertIn("Burndown Summary", markdown)

    def test_cli_writes_batches(self) -> None:
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
                        str(root / "reports" / "batches.json"),
                        "--out-csv",
                        str(root / "reports" / "batches.csv"),
                        "--out-md",
                        str(root / "reports" / "batches.md"),
                        "--source-compare-only",
                        "--max-units-per-batch",
                        "2",
                    ]
                )

            payload = json.loads((root / "reports" / "batches.json").read_text(encoding="utf-8"))

        self.assertEqual(0, exit_code)
        self.assertIn('"ok": true', stdout.getvalue())
        self.assertEqual(3, payload["batch_count"])


def _seed_table_units_csv(root: Path) -> Path:
    rows = [
        _row("1", "doc_a", "source_table_compare", "doc_a | missing | p.1-1", "missing_table_label", "1", "1"),
        _row("2", "doc_a", "source_table_compare", "doc_a | 별표1 | p.2-2", "", "2", "2"),
        _row("3", "doc_a", "source_table_compare", "doc_a | 별표2 | p.3-3", "", "3", "3"),
        _row("4", "doc_b", "source_table_compare", "doc_b | 별표1 | p.4-4", "", "4", "4"),
        _row("5", "doc_b", "structured_spot_check", "doc_b | 별표2 | p.5-5", "", "5", "5"),
    ]
    path = root / "table_units.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _row(
    unit_rank: str,
    document_id: str,
    priority: str,
    key: str,
    label_flags: str,
    page_start: str,
    page_end: str,
) -> dict[str, str]:
    return {
        "unit_rank": unit_rank,
        "review_priority": priority,
        "table_unit_key": key,
        "document_id": document_id,
        "filename": f"{document_id}.pdf",
        "extension": ".pdf",
        "source_path": f"sources/{document_id}.pdf",
        "source_page_start": page_start,
        "source_page_end": page_end,
        "table_label_review_flags": label_flags,
    }


if __name__ == "__main__":
    unittest.main()
