import csv
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts.build_publish_first_review_packet import (
    build_publish_first_review_packet,
    main,
)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class BuildPublishFirstReviewPacketTests(unittest.TestCase):
    def test_filters_parser_and_table_first_review_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parser_csv = root / "parser.csv"
            parser_batches = root / "parser_batches.json"
            table_csv = root / "table.csv"
            table_batches = root / "table_batches.json"
            _write_csv(
                parser_csv,
                [
                    {"document_id": "doc_a", "column_name": "matched_table_count"},
                    {"document_id": "doc_b", "column_name": "label_status"},
                    {"document_id": "doc_c", "column_name": "matched_article_count"},
                ],
            )
            _write_json(
                parser_batches,
                {
                    "first_batch_open_item_count": 2,
                    "first_review_batch": [
                        {"document_id": "doc_a"},
                        {"document_id": "doc_b"},
                    ],
                },
            )
            _write_csv(
                table_csv,
                [
                    {
                        "document_id": "doc_t",
                        "source_page_start": "1",
                        "source_page_end": "1",
                        "table_unit_key": "u1",
                    },
                    {
                        "document_id": "doc_t",
                        "source_page_start": "2",
                        "source_page_end": "3",
                        "table_unit_key": "u2",
                    },
                    {
                        "document_id": "doc_x",
                        "source_page_start": "1",
                        "source_page_end": "1",
                        "table_unit_key": "u3",
                    },
                ],
            )
            _write_json(
                table_batches,
                {
                    "first_review_batches": [
                        {
                            "table_review_batch_id": "batch-1",
                            "document_id": "doc_t",
                            "unit_count": 2,
                            "source_page_ranges": "1-1; 2-3",
                        }
                    ]
                },
            )

            report = build_publish_first_review_packet(
                parser_open_item_worklist_csv=parser_csv,
                parser_review_batches_report=parser_batches,
                table_unit_worklist_csv=table_csv,
                table_review_batches_report=table_batches,
            )

        self.assertEqual(2, report["parser_packet_row_count"])
        self.assertTrue(report["parser_packet_rows_match_expected"])
        self.assertEqual(2, report["table_packet_row_count"])
        self.assertTrue(report["table_packet_rows_match_expected"])
        self.assertEqual(["doc_a", "doc_b"], report["parser_first_document_ids"])
        self.assertEqual(["batch-1"], report["table_first_batch_ids"])

    def test_cli_writes_packets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parser_csv = root / "parser.csv"
            parser_batches = root / "parser_batches.json"
            table_csv = root / "table.csv"
            table_batches = root / "table_batches.json"
            out_json = root / "packet.json"
            out_md = root / "packet.md"
            out_parser_csv = root / "parser_packet.csv"
            out_table_csv = root / "table_packet.csv"
            _write_csv(parser_csv, [{"document_id": "doc_a", "column_name": "label_status"}])
            _write_json(
                parser_batches,
                {"first_batch_open_item_count": 1, "first_review_batch": [{"document_id": "doc_a"}]},
            )
            _write_csv(
                table_csv,
                [
                    {
                        "document_id": "doc_t",
                        "source_page_start": "4",
                        "source_page_end": "5",
                        "table_unit_key": "u1",
                    }
                ],
            )
            _write_json(
                table_batches,
                {
                    "first_review_batches": [
                        {
                            "table_review_batch_id": "batch-1",
                            "document_id": "doc_t",
                            "unit_count": 1,
                            "source_page_ranges": "4-5",
                        }
                    ]
                },
            )

            with redirect_stdout(StringIO()):
                exit_code = main(
                    [
                        "--parser-open-item-worklist-csv",
                        str(parser_csv),
                        "--parser-review-batches-report",
                        str(parser_batches),
                        "--table-unit-worklist-csv",
                        str(table_csv),
                        "--table-review-batches-report",
                        str(table_batches),
                        "--out-json",
                        str(out_json),
                        "--out-md",
                        str(out_md),
                        "--out-parser-csv",
                        str(out_parser_csv),
                        "--out-table-csv",
                        str(out_table_csv),
                    ]
                )

            self.assertEqual(0, exit_code)
            self.assertIn("Publish First Review Packet", out_md.read_text(encoding="utf-8"))
            self.assertIn("doc_a", out_parser_csv.read_text(encoding="utf-8-sig"))
            self.assertIn("doc_t", out_table_csv.read_text(encoding="utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
