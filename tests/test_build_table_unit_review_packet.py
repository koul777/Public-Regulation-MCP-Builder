from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.build_table_unit_review_packet import build_table_unit_review_packet, main


class BuildTableUnitReviewPacketTests(unittest.TestCase):
    def test_groups_table_risk_rows_into_read_only_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_csv = _seed_table_risk_csv(root)

            report = build_table_unit_review_packet(
                table_risk_csv=source_csv,
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
        self.assertEqual(2, len(payload["units"]))
        self.assertEqual("source_table_compare", csv_rows[0]["review_priority"])
        self.assertEqual("source.pdf", csv_rows[0]["source_file"])
        self.assertEqual("uploads/source.pdf", csv_rows[0]["source_path"])
        self.assertEqual("2", csv_rows[0]["chunk_count"])
        self.assertEqual("2", csv_rows[0]["source_compare_chunk_count"])
        self.assertEqual("", csv_rows[0]["human_unit_status"])
        self.assertIn("does not approve chunks", report["safety_note"])
        self.assertIn("Table Unit Review Packet", markdown)

    def test_cli_writes_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_csv = _seed_table_risk_csv(root)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--table-risk-csv",
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


def _seed_table_risk_csv(root: Path) -> Path:
    rows = [
        {
            "risk_tier": "source_table_compare",
            "table_unit_key": "Personnel Rule | Appendix 2-1 | p.12",
            "chunk_id": "chunk-a",
            "chunk_type": "appendix",
            "regulation_title": "Personnel Rule",
            "source_file": "source.pdf",
            "source_path": "uploads/source.pdf",
            "table_citation_label": "Appendix 2-1",
            "table_appendix_no": "2-1",
            "source_page_start": "12",
            "source_page_end": "12",
            "table_review_flags": "row_review_required; unstable_column_count",
        },
        {
            "risk_tier": "source_table_compare",
            "table_unit_key": "Personnel Rule | Appendix 2-1 | p.12",
            "chunk_id": "chunk-b",
            "chunk_type": "appendix",
            "regulation_title": "Personnel Rule",
            "source_file": "source.pdf",
            "source_path": "uploads/source.pdf",
            "table_citation_label": "Appendix 2-1",
            "table_appendix_no": "2-1",
            "source_page_start": "13",
            "source_page_end": "13",
            "table_review_flags": "possible_truncated_cell",
        },
        {
            "risk_tier": "structured_table_spot_check",
            "table_unit_key": "Pay Rule | Appendix 1 | p.20",
            "chunk_id": "chunk-c",
            "chunk_type": "appendix",
            "regulation_title": "Pay Rule",
            "source_file": "source.pdf",
            "source_path": "uploads/source.pdf",
            "table_citation_label": "Appendix 1",
            "table_appendix_no": "1",
            "source_page_start": "20",
            "source_page_end": "20",
            "table_review_flags": "",
        },
        {
            "risk_tier": "source_table_compare",
            "table_unit_key": "Supplement Rule | (missing-label) | p.30",
            "chunk_id": "chunk-d",
            "chunk_type": "supplementary_provision",
            "regulation_title": "Supplement Rule",
            "source_file": "source.pdf",
            "source_path": "uploads/source.pdf",
            "table_citation_label": "",
            "table_appendix_no": "",
            "source_page_start": "30",
            "source_page_end": "30",
            "table_review_flags": "unstable_column_count",
        },
    ]
    path = root / "table_risk.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


if __name__ == "__main__":
    unittest.main()
