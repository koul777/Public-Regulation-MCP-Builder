from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.build_table_preprocessing_risk_report import build_table_preprocessing_risk_report, main


class BuildTablePreprocessingRiskReportTests(unittest.TestCase):
    def test_summarizes_table_risk_without_approval_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chunks_json = _seed_chunks(root)

            report = build_table_preprocessing_risk_report(
                chunks_json=chunks_json,
                out_json=root / "reports" / "table_risk.json",
                out_csv=root / "reports" / "table_risk.csv",
                out_md=root / "reports" / "table_risk.md",
                max_sample_rows=2,
                generated_at="2026-07-10T00:00:00+00:00",
            )
            payload = json.loads((root / "reports" / "table_risk.json").read_text(encoding="utf-8"))
            with (root / "reports" / "table_risk.csv").open(encoding="utf-8-sig", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            markdown_exists = (root / "reports" / "table_risk.md").is_file()

        self.assertEqual(5, report["total_chunks"])
        self.assertEqual(4, report["candidate_count"])
        self.assertEqual(2, report["table_review_required_count"])
        self.assertEqual(2, report["source_table_compare_count"])
        self.assertEqual(3, report["table_unit_count"])
        self.assertEqual(1, report["source_compare_table_unit_count"])
        self.assertEqual(2, report["table_review_flag_counts"]["row_review_required"])
        self.assertEqual(1, report["source_file_count"])
        self.assertEqual(4, report["resolved_source_path_count"])
        self.assertEqual(2, report["label_summary"]["hyphenated_label_count"])
        self.assertEqual(1, report["label_summary"]["missing_table_label_count"])
        self.assertEqual({"article": 1}, report["label_summary"]["missing_label_chunk_type_counts"])
        self.assertEqual(
            {"structured_table_spot_check": 1},
            report["label_summary"]["missing_label_risk_tier_counts"],
        )
        self.assertEqual(0, report["label_summary"]["missing_label_source_compare_count"])
        self.assertIn("Do not infer appendix/form labels", report["label_summary"]["missing_label_review_guidance"])
        self.assertEqual(2, len(payload["sample_rows"]))
        self.assertEqual(4, len(csv_rows))
        self.assertEqual("source_table_compare", csv_rows[0]["risk_tier"])
        self.assertEqual("source.pdf", csv_rows[0]["source_file"])
        self.assertTrue(csv_rows[0]["source_path"].endswith("uploads\\source.pdf") or csv_rows[0]["source_path"].endswith("uploads/source.pdf"))
        self.assertEqual("2", csv_rows[0]["table_unit_size"])
        self.assertIn("human_table_status", csv_rows[0])
        self.assertEqual("", csv_rows[0]["human_parentage_ok"])
        self.assertIn("does not approve chunks", report["safety_note"])
        self.assertTrue(markdown_exists)

    def test_cli_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chunks_json = _seed_chunks(root)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--chunks-json",
                        str(chunks_json),
                        "--out-json",
                        str(root / "reports" / "table_risk.json"),
                        "--out-csv",
                        str(root / "reports" / "table_risk.csv"),
                        "--out-md",
                        str(root / "reports" / "table_risk.md"),
                        "--max-sample-rows",
                        "1",
                    ]
                )

            payload = json.loads((root / "reports" / "table_risk.json").read_text(encoding="utf-8"))

        self.assertEqual(0, exit_code)
        self.assertIn('"ok": true', stdout.getvalue())
        self.assertEqual(1, payload["sample_count"])
        self.assertEqual(4, payload["candidate_count"])


def _seed_chunks(root: Path) -> Path:
    uploads = root / "uploads"
    uploads.mkdir()
    (uploads / "source.pdf").write_text("source", encoding="utf-8")
    chunks = [
        {
            "chunk_id": "table-review",
            "chunk_type": "appendix",
            "text": "table with unstable rows",
            "metadata": {
                "chunk_type": "appendix",
                "regulation_title": "Personnel Rule",
                "source_file": "source.pdf",
                "article_no": "Article 1",
                "source_page_start": 12,
                "table_like": True,
                "table_review_required": True,
                "table_review_flags": ["row_review_required", "unstable_column_count"],
                "table_classification": "structured_table",
                "table_structured_row_count": 4,
                "table_column_count": 5,
                "table_record_count": 3,
                "table_citation_label": "Appendix 2-1",
                "parser_uncertainty_flags": ["embedded_text_extracted"],
            },
        },
        {
            "chunk_id": "table-review-part-2",
            "chunk_type": "appendix",
            "text": "second chunk from the same source table",
            "metadata": {
                "chunk_type": "appendix",
                "regulation_title": "Personnel Rule",
                "source_file": "source.pdf",
                "article_no": "Article 1",
                "source_page_start": 12,
                "table_like": True,
                "table_review_required": True,
                "table_review_flags": ["row_review_required"],
                "table_classification": "structured_table",
                "table_structured_row_count": 2,
                "table_column_count": 5,
                "table_record_count": 1,
                "table_citation_label": "Appendix 2-1",
            },
        },
        {
            "chunk_id": "table-spot",
            "chunk_type": "article",
            "text": "structured table no blocking flags",
            "metadata": {
                "regulation_title": "Pay Rule",
                "source_file": "source.pdf",
                "table_like": True,
                "table_review_required": False,
                "table_classification": "structured_table",
                "table_structured_row_count": 8,
                "table_column_count": 3,
            },
        },
        {
            "chunk_id": "table-parent",
            "chunk_type": "form",
            "text": "form label only",
            "metadata": {
                "regulation_title": "Form Rule",
                "source_file": "source.pdf",
                "table_citation_label": "Form Table",
                "table_like": False,
            },
        },
        {
            "chunk_id": "plain",
            "chunk_type": "article",
            "text": "plain article",
            "metadata": {"regulation_title": "Plain Rule", "source_file": "source.pdf", "table_like": False},
        },
    ]
    repository = root / "repository"
    repository.mkdir()
    chunks_json = repository / "chunks.json"
    chunks_json.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
    return chunks_json


if __name__ == "__main__":
    unittest.main()
