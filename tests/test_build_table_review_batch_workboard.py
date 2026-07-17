import csv
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts.build_table_review_batch_workboard import (
    build_table_review_batch_workboard,
    main,
)


FIELDS = [
    "batch_rank",
    "table_review_batch_id",
    "document_id",
    "review_priority",
    "unit_count",
    "unit_ranks",
    "unit_key_fingerprint",
    "source_path",
    "filename",
    "extension",
    "source_page_ranges",
    "review_priority_counts",
    "label_review_flag_counts",
    "table_unit_packet_csv",
    "human_batch_status",
    "human_reviewer",
    "human_reviewed_at",
    "human_notes",
]


def _write_batches(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class BuildTableReviewBatchWorkboardTests(unittest.TestCase):
    def test_summarizes_first_batches_and_traceability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batches_csv = root / "table_batches.csv"
            traceability = root / "traceability.json"
            _write_batches(
                batches_csv,
                [
                    {
                        "batch_rank": "1",
                        "table_review_batch_id": "batch-1",
                        "document_id": "doc_a",
                        "review_priority": "source_table_compare",
                        "unit_count": "10",
                        "source_path": "data\\a.pdf",
                        "source_page_ranges": "1-2; 3-4",
                        "review_priority_counts": "source_table_compare=8; parentage_review=2",
                        "label_review_flag_counts": "missing_table_label=3",
                    },
                    {
                        "batch_rank": "2",
                        "table_review_batch_id": "batch-2",
                        "document_id": "doc_b",
                        "review_priority": "structured_spot_check",
                        "unit_count": "5",
                        "source_path": "data\\b.hwp",
                        "source_page_ranges": "1-1",
                        "review_priority_counts": "structured_spot_check=5",
                        "label_review_flag_counts": "",
                    },
                ],
            )
            _write_json(
                traceability,
                {
                    "traceability_passed": True,
                    "issue_count": 0,
                    "blocked_batch_count": 0,
                    "page_count_status_counts": {"verified_pdf": 1},
                    "source_format_status_counts": {"verified_hwp_ole": 1},
                },
            )

            report = build_table_review_batch_workboard(
                table_review_batches_csv=batches_csv,
                source_traceability_report=traceability,
                first_batch_count=1,
            )

        self.assertEqual("table_review_batch_workboard", report["report_type"])
        self.assertEqual(2, report["batch_count"])
        self.assertEqual(15, report["unit_count"])
        self.assertEqual(1, report["first_batch_count"])
        self.assertEqual(10, report["first_batch_unit_count"])
        self.assertEqual(2, report["human_status_missing_batch_count"])
        self.assertTrue(report["traceability_summary"]["passed"])
        self.assertEqual(8, report["review_priority_counts"]["source_table_compare"])
        self.assertEqual(3, report["label_review_flag_counts"]["missing_table_label"])

    def test_cli_writes_json_csv_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batches_csv = root / "table_batches.csv"
            out_json = root / "workboard.json"
            out_csv = root / "workboard.csv"
            out_md = root / "workboard.md"
            _write_batches(
                batches_csv,
                [
                    {
                        "batch_rank": "1",
                        "table_review_batch_id": "batch-1",
                        "document_id": "doc_a",
                        "review_priority": "source_table_compare",
                        "unit_count": "1",
                        "source_path": "data\\a.pdf",
                        "source_page_ranges": "1-1",
                        "review_priority_counts": "source_table_compare=1",
                        "label_review_flag_counts": "",
                    }
                ],
            )

            with redirect_stdout(StringIO()):
                exit_code = main(
                    [
                        "--table-review-batches-csv",
                        str(batches_csv),
                        "--first-batch-count",
                        "1",
                        "--out-json",
                        str(out_json),
                        "--out-csv",
                        str(out_csv),
                        "--out-md",
                        str(out_md),
                    ]
                )

            payload = json.loads(out_json.read_text(encoding="utf-8"))
            markdown = out_md.read_text(encoding="utf-8")
            csv_text = out_csv.read_text(encoding="utf-8-sig")

        self.assertEqual(0, exit_code)
        self.assertEqual(1, payload["first_batch_unit_count"])
        self.assertIn("Table Review Batch Workboard", markdown)
        self.assertIn("table_review_batch_id", csv_text)


if __name__ == "__main__":
    unittest.main()
