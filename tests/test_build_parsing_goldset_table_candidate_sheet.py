from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.build_parsing_goldset_table_candidate_sheet import (
    build_parsing_goldset_table_candidate_sheet,
    main,
)


class BuildParsingGoldsetTableCandidateSheetTests(unittest.TestCase):
    def test_exports_read_only_table_candidates_from_goldset_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels_csv = _seed_goldset(root)

            report = build_parsing_goldset_table_candidate_sheet(
                workspace=root,
                labels_csv=labels_csv,
                out_json=root / "reports" / "table_candidates.json",
                out_csv=root / "reports" / "table_candidates.csv",
                out_md=root / "reports" / "table_candidates.md",
                generated_at="2026-07-10T00:00:00+00:00",
            )
            payload = json.loads((root / "reports" / "table_candidates.json").read_text(encoding="utf-8"))
            with (root / "reports" / "table_candidates.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            markdown = (root / "reports" / "table_candidates.md").read_text(encoding="utf-8")

        self.assertEqual(2, report["document_count"])
        self.assertEqual(3, report["candidate_count"])
        self.assertEqual(1, report["review_required_count"])
        self.assertEqual(1, report["missing_label_candidate_count"])
        self.assertEqual(1, report["priority_counts"]["source_table_compare"])
        self.assertEqual(1, report["priority_counts"]["parser_structure_review"])
        self.assertEqual(1, report["priority_counts"]["structured_spot_check"])
        self.assertEqual(3, len(payload["rows"]))
        self.assertEqual("source_table_compare", rows[0]["review_priority"])
        self.assertEqual("doc_a_table_required", rows[0]["chunk_id"])
        self.assertIn("unstable_column_count", rows[0]["table_review_flags"])
        self.assertEqual("nested_table", rows[0]["source_parser_flags"])
        self.assertEqual("", rows[0]["human_match_decision"])
        self.assertIn("does not fill goldset labels", report["safety_note"])
        self.assertIn("Parsing Goldset Table Candidate Sheet", markdown)

    def test_cli_writes_sheet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels_csv = _seed_goldset(root)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--workspace",
                        str(root),
                        "--labels-csv",
                        str(labels_csv),
                        "--out-json",
                        "reports/table_candidates.json",
                        "--out-csv",
                        "reports/table_candidates.csv",
                        "--out-md",
                        "reports/table_candidates.md",
                    ]
                )

            payload = json.loads((root / "reports" / "table_candidates.json").read_text(encoding="utf-8"))

        self.assertEqual(0, exit_code)
        self.assertIn('"ok": true', stdout.getvalue())
        self.assertEqual(3, payload["candidate_count"])


def _seed_goldset(root: Path) -> Path:
    chunks_dir = root / "data" / "repository"
    chunks_dir.mkdir(parents=True)
    (chunks_dir / "doc_a_chunks.json").write_text(
        json.dumps(
            [
                {
                    "chunk_id": "doc_a_table_required",
                    "chunk_type": "table",
                    "text": "Required table",
                    "metadata": {
                        "source_page_start": 3,
                        "source_page_end": 4,
                        "table_like": True,
                        "table_review_required": True,
                        "table_citation_label": "Appendix 1",
                        "table_review_flags": ["unstable_column_count"],
                        "table_structured_row_count": 3,
                        "table_column_count": 4,
                        "source_hwpx_parser_review_flags": ["nested_table"],
                    },
                },
                {
                    "chunk_id": "doc_a_table_parser_flag",
                    "chunk_type": "paragraph",
                    "text": "Parser flagged table shape",
                    "metadata": {
                        "source_page_start": 5,
                        "table_like": False,
                        "table_review_flags": ["possible_truncated_cell"],
                    },
                },
                {
                    "chunk_id": "doc_a_text",
                    "chunk_type": "paragraph",
                    "text": "Not a table",
                    "metadata": {},
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (chunks_dir / "doc_b_chunks.json").write_text(
        json.dumps(
            [
                {
                    "chunk_id": "doc_b_structured",
                    "chunk_type": "appendix",
                    "text": "Structured appendix table",
                    "metadata": {
                        "source_page_start": 2,
                        "table_like": True,
                        "table_citation_label": "Appendix 2",
                        "table_structured_row_count": 2,
                        "table_column_count": 2,
                    },
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "review_order": "1",
            "label_status": "pending_human_review",
            "document_id": "doc_a",
            "extension": ".hwpx",
            "institution_name": "Institution A",
            "filename": "doc_a.hwpx",
            "chunk_artifact": "data/repository/doc_a_chunks.json",
        },
        {
            "review_order": "2",
            "label_status": "pending_human_review",
            "document_id": "doc_b",
            "extension": ".pdf",
            "institution_name": "Institution B",
            "filename": "doc_b.pdf",
            "chunk_artifact": "data/repository/doc_b_chunks.json",
        },
    ]
    labels_csv = root / "labels.csv"
    with labels_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return labels_csv


if __name__ == "__main__":
    unittest.main()
