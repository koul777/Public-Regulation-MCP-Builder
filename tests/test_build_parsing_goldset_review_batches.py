import csv
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts.build_parsing_goldset_review_batches import (
    build_parsing_goldset_review_batches,
    main,
)


FIELDS = [
    "priority_rank",
    "document_id",
    "filename",
    "item_kind",
    "column_name",
    "structure",
    "pipeline_count",
    "label_status",
    "recommended_action",
    "source_path",
]


def _write_worklist(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


class BuildParsingGoldsetReviewBatchesTests(unittest.TestCase):
    def test_groups_open_items_by_document_and_marks_first_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worklist = root / "open_items.csv"
            _write_worklist(
                worklist,
                [
                    {
                        "priority_rank": "1",
                        "document_id": "doc_a",
                        "filename": "a.hwp",
                        "item_kind": "matched_count",
                        "column_name": "matched_table_count",
                        "structure": "table",
                        "pipeline_count": "7",
                        "label_status": "pending",
                        "recommended_action": "fill",
                        "source_path": "C:\\tmp\\a.hwp",
                    },
                    {
                        "priority_rank": "1",
                        "document_id": "doc_a",
                        "filename": "a.hwp",
                        "item_kind": "label_status",
                        "column_name": "label_status",
                        "structure": "",
                        "pipeline_count": "",
                        "label_status": "pending",
                        "recommended_action": "status",
                        "source_path": "C:\\tmp\\a.hwp",
                    },
                    {
                        "priority_rank": "2",
                        "document_id": "doc_b",
                        "filename": "b.pdf",
                        "item_kind": "matched_count",
                        "column_name": "matched_paragraph_item_count",
                        "structure": "paragraph_item",
                        "pipeline_count": "13",
                        "label_status": "pending",
                        "recommended_action": "fill",
                        "source_path": "C:\\tmp\\b.pdf",
                    },
                ],
            )

            report = build_parsing_goldset_review_batches(
                open_item_worklist_csv=worklist,
                first_batch_document_count=1,
            )

        self.assertEqual("parsing_goldset_review_batches", report["report_type"])
        self.assertEqual(2, report["document_batch_count"])
        self.assertEqual(3, report["open_item_count"])
        self.assertEqual(0, report["malformed_open_item_count"])
        self.assertEqual(1, report["first_batch_document_count"])
        self.assertEqual(2, report["first_batch_open_item_count"])
        first = report["first_review_batch"][0]
        self.assertEqual("doc_a", first["document_id"])
        self.assertEqual(2, first["open_item_count"])
        self.assertEqual(1, first["matched_count_items"])
        self.assertEqual(1, first["label_status_items"])
        self.assertEqual("table", first["structures"])
        self.assertIn("table/form", first["recommended_action"])

    def test_cli_writes_json_csv_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worklist = root / "open_items.csv"
            out_json = root / "batches.json"
            out_csv = root / "batches.csv"
            out_md = root / "batches.md"
            _write_worklist(
                worklist,
                [
                    {
                        "priority_rank": "1",
                        "document_id": "doc_a",
                        "filename": "a.hwp",
                        "item_kind": "label_status",
                        "column_name": "label_status",
                        "structure": "",
                        "pipeline_count": "",
                        "label_status": "pending",
                        "recommended_action": "status",
                        "source_path": "C:\\tmp\\a.hwp",
                    }
                ],
            )

            with redirect_stdout(StringIO()):
                exit_code = main(
                    [
                        "--open-item-worklist-csv",
                        str(worklist),
                        "--first-batch-document-count",
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
        self.assertEqual(1, payload["first_batch_open_item_count"])
        self.assertIn("Parsing Goldset Review Batches", markdown)
        self.assertIn("| Rank | Document | Items | Matched | Label Status | Structures | Pipeline Total | First Action |", markdown)
        self.assertIn("batch_rank", csv_text)

    def test_missing_document_id_is_surfaced_as_malformed_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worklist = root / "open_items.csv"
            _write_worklist(
                worklist,
                [
                    {
                        "priority_rank": "1",
                        "document_id": "",
                        "filename": "missing.hwp",
                        "item_kind": "matched_count",
                        "column_name": "matched_table_count",
                        "structure": "table",
                        "pipeline_count": "5",
                        "label_status": "pending",
                        "recommended_action": "fix",
                        "source_path": "C:\\tmp\\missing.hwp",
                    },
                    {
                        "priority_rank": "2",
                        "document_id": "doc_a",
                        "filename": "a.hwp",
                        "item_kind": "label_status",
                        "column_name": "label_status",
                        "structure": "",
                        "pipeline_count": "",
                        "label_status": "pending",
                        "recommended_action": "status",
                        "source_path": "C:\\tmp\\a.hwp",
                    },
                ],
            )

            report = build_parsing_goldset_review_batches(
                open_item_worklist_csv=worklist,
                first_batch_document_count=2,
            )

        self.assertEqual(2, report["open_item_count"])
        self.assertEqual(1, report["malformed_open_item_count"])
        self.assertEqual(2, report["document_batch_count"])
        self.assertEqual(2, report["first_batch_open_item_count"])
        malformed = [
            batch
            for batch in report["document_batches"]
            if batch["document_id"] == "__missing_document_id__"
        ]
        self.assertEqual(1, len(malformed))
        self.assertEqual("missing_document_id", malformed[0]["malformed_reason"])
        self.assertIn("Fix missing document_id", malformed[0]["recommended_action"])


if __name__ == "__main__":
    unittest.main()
