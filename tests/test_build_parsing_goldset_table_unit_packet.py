from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.build_parsing_goldset_table_unit_packet import (
    build_parsing_goldset_table_unit_packet,
    main,
)


class BuildParsingGoldsetTableUnitPacketTests(unittest.TestCase):
    def test_groups_goldset_table_candidates_into_read_only_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_csv = _seed_table_candidates_csv(root)
            labels_csv = _seed_labels_csv(root)

            report = build_parsing_goldset_table_unit_packet(
                table_candidates_csv=source_csv,
                labels_csv=labels_csv,
                out_json=root / "reports" / "units.json",
                out_csv=root / "reports" / "units.csv",
                out_md=root / "reports" / "units.md",
                source_compare_only=True,
                max_md_rows=2,
                generated_at="2026-07-10T00:00:00+00:00",
            )
            payload = json.loads((root / "reports" / "units.json").read_text(encoding="utf-8"))
            with (root / "reports" / "units.csv").open(encoding="utf-8-sig", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            markdown = (root / "reports" / "units.md").read_text(encoding="utf-8")

        self.assertEqual(4, report["row_count"])
        self.assertEqual(3, report["unit_count"])
        self.assertEqual(2, report["source_compare_unit_count"])
        self.assertEqual(2, report["selected_unit_count"])
        self.assertEqual({"source_table_compare": 2}, report["review_priority_counts"])
        self.assertEqual(
            {
                "article_reference_fragment_loss_candidate": 1,
                "embedded_table_parentage_candidate": 1,
                "missing_table_label": 1,
            },
            report["label_review_flag_counts"],
        )
        self.assertEqual(2, len(payload["units"]))
        self.assertEqual("source_table_compare", csv_rows[0]["review_priority"])
        self.assertEqual("2", csv_rows[0]["candidate_count"])
        self.assertEqual("2", csv_rows[0]["source_compare_candidate_count"])
        self.assertEqual("0", csv_rows[0]["missing_label_candidate_count"])
        self.assertEqual("article_reference_fragment_loss_candidate", csv_rows[0]["table_label_review_flags"])
        self.assertEqual(".hwpx", csv_rows[0]["extension"])
        self.assertEqual("sources/doc_a.hwpx", csv_rows[0]["source_path"])
        self.assertEqual("missing_table_label; embedded_table_parentage_candidate", csv_rows[1]["table_label_review_flags"])
        self.assertEqual("sources/doc_b.hwp", csv_rows[1]["source_path"])
        self.assertEqual("", csv_rows[0]["human_unit_status"])
        self.assertEqual("", csv_rows[0]["human_reviewer"])
        self.assertEqual("", csv_rows[0]["human_reviewed_at"])
        self.assertIn("reviewed", csv_rows[0]["allowed_human_unit_statuses"])
        self.assertIn("human_reviewer", csv_rows[0]["required_complete_fields"])
        self.assertIn("confirmed", csv_rows[0]["accepted_confirmation_values"])
        self.assertIn("source pages", csv_rows[0]["review_entry_guidance"])
        self.assertIn("reviewed", payload["review_contract"]["allowed_human_unit_statuses"])
        self.assertIn("human_reviewed_at", payload["review_contract"]["required_complete_fields"])
        self.assertIn("does not fill goldset labels", report["safety_note"])
        self.assertIn("Label Review Flags", markdown)
        self.assertIn("Review Entry Contract", markdown)
        self.assertIn("Source labels CSV", markdown)
        self.assertIn("Parsing Goldset Table Unit Packet", markdown)

    def test_cli_writes_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_csv = _seed_table_candidates_csv(root)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--table-candidates-csv",
                        str(source_csv),
                        "--out-json",
                        str(root / "reports" / "units.json"),
                        "--out-csv",
                        str(root / "reports" / "units.csv"),
                        "--out-md",
                        str(root / "reports" / "units.md"),
                        "--source-compare-only",
                    ]
                )

            payload = json.loads((root / "reports" / "units.json").read_text(encoding="utf-8"))

        self.assertEqual(0, exit_code)
        self.assertIn('"ok": true', stdout.getvalue())
        self.assertEqual(2, payload["selected_unit_count"])

    def test_duplicate_label_flag_ignores_appendix_creation_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_csv = _write_rows(
                root / "table_candidates.csv",
                [
                    _candidate_row(
                        chunk_id="chunk-a",
                        table_citation_label="별지 제15호 서식] <별지서식신설 2026. 6. 16.>",
                        table_appendix_no="별지제15호서식",
                        source_page_start="1",
                    ),
                    _candidate_row(
                        chunk_id="chunk-b",
                        table_citation_label="별표4 별표4",
                        table_appendix_no="별표4",
                        source_page_start="2",
                    ),
                ],
            )

            build_parsing_goldset_table_unit_packet(
                table_candidates_csv=source_csv,
                out_json=root / "reports" / "units.json",
                out_csv=root / "reports" / "units.csv",
                out_md=root / "reports" / "units.md",
                source_compare_only=True,
                generated_at="2026-07-10T00:00:00+00:00",
            )
            payload = json.loads((root / "reports" / "units.json").read_text(encoding="utf-8"))

        by_key = {unit["table_citation_label"]: unit["table_label_review_flags"] for unit in payload["units"]}
        self.assertEqual("", by_key["별지 제15호 서식] <별지서식신설 2026. 6. 16.>"])
        self.assertEqual("duplicated_appendix_label_candidate", by_key["별표4 별표4"])
        self.assertEqual({"duplicated_appendix_label_candidate": 1}, payload["label_review_flag_counts"])


def _seed_table_candidates_csv(root: Path) -> Path:
    rows = [
        {
            "review_order": "1",
            "document_id": "doc_a",
            "institution_name": "Institution A",
            "filename": "doc_a.hwpx",
            "chunk_artifact": "data/repository/doc_a_chunks.json",
            "review_priority": "source_table_compare",
            "chunk_id": "chunk-a",
            "chunk_type": "appendix",
            "source_page_start": "12",
            "source_page_end": "12",
            "article_no": "",
            "article_title": "",
            "table_citation_label": "별표 제조제항 관련 8 1",
            "table_appendix_no": "별표",
            "table_review_flags": "row_review_required; unstable_column_count",
            "source_parser_flags": "",
        },
        {
            "review_order": "1",
            "document_id": "doc_a",
            "institution_name": "Institution A",
            "filename": "doc_a.hwpx",
            "chunk_artifact": "data/repository/doc_a_chunks.json",
            "review_priority": "source_table_compare",
            "chunk_id": "chunk-b",
            "chunk_type": "appendix",
            "source_page_start": "12",
            "source_page_end": "12",
            "article_no": "",
            "article_title": "",
            "table_citation_label": "별표 제조제항 관련 8 1",
            "table_appendix_no": "별표",
            "table_review_flags": "possible_truncated_cell",
            "source_parser_flags": "nested_table",
        },
        {
            "review_order": "1",
            "document_id": "doc_a",
            "institution_name": "Institution A",
            "filename": "doc_a.hwpx",
            "chunk_artifact": "data/repository/doc_a_chunks.json",
            "review_priority": "structured_spot_check",
            "chunk_id": "chunk-c",
            "chunk_type": "appendix",
            "source_page_start": "20",
            "source_page_end": "20",
            "article_no": "",
            "article_title": "",
            "table_citation_label": "Appendix 3",
            "table_appendix_no": "3",
            "table_review_flags": "",
            "source_parser_flags": "",
        },
        {
            "review_order": "2",
            "document_id": "doc_b",
            "institution_name": "Institution B",
            "filename": "doc_b.hwp",
            "chunk_artifact": "data/repository/doc_b_chunks.json",
            "review_priority": "source_table_compare",
            "chunk_id": "chunk-d",
            "chunk_type": "paragraph",
            "source_page_start": "30",
            "source_page_end": "30",
            "article_no": "Article 4",
            "article_title": "Supplement",
            "table_citation_label": "",
            "table_appendix_no": "",
            "table_review_flags": "unstable_column_count",
            "source_parser_flags": "",
        },
    ]
    return _write_rows(root / "table_candidates.csv", rows)


def _seed_labels_csv(root: Path) -> Path:
    rows = [
        {
            "document_id": "doc_a",
            "extension": ".hwpx",
            "source_path": "sources/doc_a.hwpx",
        },
        {
            "document_id": "doc_b",
            "extension": ".hwp",
            "source_path": "sources/doc_b.hwp",
        },
    ]
    return _write_rows(root / "labels.csv", rows)


def _candidate_row(
    *,
    chunk_id: str,
    table_citation_label: str,
    table_appendix_no: str,
    source_page_start: str,
) -> dict[str, str]:
    return {
        "review_order": "1",
        "document_id": "doc_a",
        "institution_name": "Institution A",
        "filename": "doc_a.hwpx",
        "chunk_artifact": "data/repository/doc_a_chunks.json",
        "review_priority": "source_table_compare",
        "chunk_id": chunk_id,
        "chunk_type": "appendix",
        "source_page_start": source_page_start,
        "source_page_end": source_page_start,
        "article_no": "",
        "article_title": "",
        "table_citation_label": table_citation_label,
        "table_appendix_no": table_appendix_no,
        "table_review_flags": "",
        "source_parser_flags": "",
    }


def _write_rows(path: Path, rows: list[dict[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


if __name__ == "__main__":
    unittest.main()
