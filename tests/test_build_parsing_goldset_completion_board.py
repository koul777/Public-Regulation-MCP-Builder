from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.build_parsing_goldset_completion_board import (
    build_parsing_goldset_completion_board,
    main,
)


class BuildParsingGoldsetCompletionBoardTests(unittest.TestCase):
    def test_prioritizes_table_heavy_unreviewed_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels_csv = _seed_labels(root)
            packet_dir = root / "packets"
            packet_dir.mkdir()
            (packet_dir / "01_doc_table_heavy.md").write_text("packet", encoding="utf-8")
            (packet_dir / "02_doc_complete.md").write_text("packet", encoding="utf-8")

            report = build_parsing_goldset_completion_board(
                labels_csv=labels_csv,
                packet_dir=packet_dir,
                out_json=root / "reports" / "board.json",
                out_csv=root / "reports" / "board.csv",
                out_md=root / "reports" / "board.md",
                generated_at="2026-07-10T00:00:00+00:00",
            )
            payload = json.loads((root / "reports" / "board.json").read_text(encoding="utf-8"))
            with (root / "reports" / "board.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            markdown = (root / "reports" / "board.md").read_text(encoding="utf-8")

        self.assertEqual(2, report["document_count"])
        self.assertEqual(1, report["ready_document_count"])
        self.assertFalse(report["ready_for_quality_claim"])
        self.assertEqual(14, report["expected_structure_score_rows"])
        self.assertEqual(7, report["completed_structure_score_rows"])
        self.assertEqual(7, report["missing_structure_score_rows"])
        self.assertEqual(7, report["missing_manual_field_count"])
        self.assertEqual(7, report["missing_matched_field_count"])
        self.assertEqual("blocked_pending_human_labels", report["completion_gate_status"])
        self.assertEqual(2, report["quality_claim_document_count"])
        self.assertEqual(7, report["quality_claim_completed_structure_score_rows"])
        self.assertEqual(7, report["quality_claim_missing_matched_field_count"])
        self.assertEqual(0, report["excluded_document_count"])
        self.assertEqual(1, report["priority_tier_counts"]["table_heavy_first"])
        table_completion = report["structure_completion_summary"]["table"]
        self.assertEqual(2, table_completion["expected_document_count"])
        self.assertEqual(1, table_completion["score_rows_complete"])
        self.assertEqual(1, table_completion["missing_manual_count"])
        self.assertEqual(1, table_completion["missing_matched_count"])
        self.assertEqual(56, table_completion["pipeline_total"])
        self.assertFalse(table_completion["ready_for_structure_f1"])
        self.assertEqual("doc_table_heavy", payload["rows"][0]["document_id"])
        self.assertEqual(table_completion, payload["structure_completion_summary"]["table"])
        self.assertEqual("table_heavy_first", rows[0]["priority_tier"])
        self.assertEqual("0", rows[0]["score_rows_complete"])
        self.assertIn("doc_table_heavy.md", rows[0]["packet_path"])
        self.assertEqual("", rows[0]["human_progress_notes"])
        self.assertEqual("quality_claim", rows[0]["score_scope"])
        self.assertEqual("false", rows[0]["excluded_from_quality_claim"])
        self.assertIn("does not fill human labels", report["safety_note"])
        self.assertIn("Parsing Goldset Completion Board", markdown)
        self.assertIn("Completion gate status: `blocked_pending_human_labels`", markdown)
        self.assertIn("Structure Completion", markdown)
        self.assertIn("| table | 56 | 1/2 | 1 | 1 | false |", markdown)

    def test_marks_non_article_scope_as_excluded_from_quality_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [
                _label_row(
                    review_order="1",
                    label_status="pending_human_review",
                    document_id="doc_handbook",
                    extension=".pdf",
                    pipeline_article_count=0,
                    pipeline_paragraph_item_count=0,
                    pipeline_appendix_form_count=0,
                    pipeline_table_count=0,
                    pipeline_nested_table_count=0,
                    pipeline_supplementary_effective_date_count=0,
                    pipeline_footnote_caption_count=0,
                    fill_scores=False,
                )
            ]
            rows[0].update(
                {
                    "manual_article_count": "0",
                    "manual_paragraph_item_count": "0",
                    "manual_appendix_form_count": "0",
                    "manual_table_count": "0",
                    "manual_nested_table_count": "0",
                    "manual_supplementary_effective_date_count": "0",
                    "manual_footnote_caption_count": "0",
                    "table_preservation_notes": "not article",
                }
            )
            labels_csv = root / "labels.csv"
            with labels_csv.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)

            report = build_parsing_goldset_completion_board(
                labels_csv=labels_csv,
                out_json=root / "reports" / "board.json",
                out_csv=root / "reports" / "board.csv",
                out_md=root / "reports" / "board.md",
                generated_at="2026-07-10T00:00:00+00:00",
            )
            payload = json.loads((root / "reports" / "board.json").read_text(encoding="utf-8"))
            markdown = (root / "reports" / "board.md").read_text(encoding="utf-8")

        self.assertEqual(0, report["quality_claim_document_count"])
        self.assertEqual(1, report["excluded_document_count"])
        self.assertEqual("manual_non_article_form", payload["rows"][0]["score_scope"])
        self.assertEqual("true", payload["rows"][0]["excluded_from_quality_claim"])
        self.assertIn("Excluded documents: 1", markdown)

    def test_preserves_korean_display_fields_from_utf8_csv_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            institution_name = "국가철도공단"
            filename = "4860_235589_지식재산권관리규정(2026년도 04월 30일 개정).pdf"
            rows = [
                _label_row(
                    review_order="1",
                    label_status="reviewed",
                    document_id="doc_korean",
                    extension=".pdf",
                    pipeline_article_count=1,
                    pipeline_paragraph_item_count=1,
                    pipeline_appendix_form_count=0,
                    pipeline_table_count=0,
                    pipeline_nested_table_count=0,
                    pipeline_supplementary_effective_date_count=0,
                    pipeline_footnote_caption_count=0,
                    fill_scores=True,
                )
            ]
            rows[0].update(
                {
                    "institution_name": institution_name,
                    "filename": filename,
                    "source_path": f"data\\public_portal\\C0001_{institution_name}\\{filename}",
                }
            )
            labels_csv = root / "수동라벨.csv"
            with labels_csv.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)

            build_parsing_goldset_completion_board(
                labels_csv=labels_csv,
                out_json=root / "reports" / "board.json",
                out_csv=root / "reports" / "board.csv",
                out_md=root / "reports" / "board.md",
                generated_at="2026-07-10T00:00:00+00:00",
            )
            payload = json.loads((root / "reports" / "board.json").read_text(encoding="utf-8"))
            with (root / "reports" / "board.csv").open(encoding="utf-8-sig", newline="") as handle:
                board_rows = list(csv.DictReader(handle))
            markdown = (root / "reports" / "board.md").read_text(encoding="utf-8")
            csv_bytes = (root / "reports" / "board.csv").read_bytes()

        self.assertTrue(csv_bytes.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(institution_name, payload["rows"][0]["institution_name"])
        self.assertEqual(filename, payload["rows"][0]["filename"])
        self.assertEqual(institution_name, board_rows[0]["institution_name"])
        self.assertEqual(filename, board_rows[0]["filename"])
        self.assertIn(institution_name, markdown)
        self.assertIn(filename, markdown)
        self.assertNotIn("怨듬", json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("怨듬", markdown)

    def test_recovers_mojibake_korean_display_fields_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            institution_name = "근로복지공단"
            filename = "3488_237887_권한위임전결규정(2026년도 7월 1일 개정).hwp"
            rows = [
                _label_row(
                    review_order="1",
                    label_status="pending_human_review",
                    document_id="doc_mojibake",
                    extension=".hwp",
                    pipeline_article_count=1,
                    pipeline_paragraph_item_count=1,
                    pipeline_appendix_form_count=0,
                    pipeline_table_count=0,
                    pipeline_nested_table_count=0,
                    pipeline_supplementary_effective_date_count=0,
                    pipeline_footnote_caption_count=0,
                    fill_scores=False,
                )
            ]
            rows[0].update(
                {
                    "institution_name": institution_name.encode("utf-8").decode("cp949", errors="replace"),
                    "filename": filename.encode("utf-8").decode("cp949", errors="replace"),
                    "source_path": f"data\\public_portal\\C0035_{institution_name}\\{filename}",
                }
            )
            labels_csv = root / "labels.csv"
            with labels_csv.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)

            build_parsing_goldset_completion_board(
                labels_csv=labels_csv,
                out_json=root / "reports" / "board.json",
                out_csv=root / "reports" / "board.csv",
                out_md=root / "reports" / "board.md",
                generated_at="2026-07-10T00:00:00+00:00",
            )
            payload = json.loads((root / "reports" / "board.json").read_text(encoding="utf-8"))
            with (root / "reports" / "board.csv").open(encoding="utf-8-sig", newline="") as handle:
                board_rows = list(csv.DictReader(handle))
            markdown = (root / "reports" / "board.md").read_text(encoding="utf-8")

        self.assertEqual(institution_name, payload["rows"][0]["institution_name"])
        self.assertEqual(filename, payload["rows"][0]["filename"])
        self.assertEqual(institution_name, board_rows[0]["institution_name"])
        self.assertEqual(filename, board_rows[0]["filename"])
        self.assertIn(institution_name, markdown)
        self.assertIn(filename, markdown)
        self.assertNotIn("\ufffd", json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("怨듬", json.dumps(payload, ensure_ascii=False))

    def test_cli_writes_board(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels_csv = _seed_labels(root)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--labels-csv",
                        str(labels_csv),
                        "--out-json",
                        str(root / "reports" / "board.json"),
                        "--out-csv",
                        str(root / "reports" / "board.csv"),
                        "--out-md",
                        str(root / "reports" / "board.md"),
                    ]
                )

            payload = json.loads((root / "reports" / "board.json").read_text(encoding="utf-8"))

        self.assertEqual(0, exit_code)
        self.assertIn('"ok": true', stdout.getvalue())
        self.assertIn('"completion_gate_status": "blocked_pending_human_labels"', stdout.getvalue())
        self.assertEqual(2, payload["document_count"])
        self.assertFalse(payload["ready_for_quality_claim"])

    def test_cli_can_fail_on_incomplete_board(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels_csv = _seed_labels(root)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--labels-csv",
                        str(labels_csv),
                        "--out-json",
                        str(root / "reports" / "board.json"),
                        "--out-csv",
                        str(root / "reports" / "board.csv"),
                        "--out-md",
                        str(root / "reports" / "board.md"),
                        "--fail-on-incomplete",
                    ]
                )

            payload = json.loads((root / "reports" / "board.json").read_text(encoding="utf-8"))

        self.assertEqual(2, exit_code)
        self.assertEqual("blocked_pending_human_labels", payload["completion_gate_status"])
        self.assertFalse(payload["ready_for_quality_claim"])

    def test_derives_zero_match_completion_when_manual_or_pipeline_is_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels_csv = root / "labels.csv"
            rows = [
                _label_row(
                    review_order="1",
                    label_status="pending_human_review",
                    document_id="doc_zero_bound",
                    extension=".pdf",
                    pipeline_article_count=3,
                    pipeline_paragraph_item_count=0,
                    pipeline_appendix_form_count=0,
                    pipeline_table_count=2,
                    pipeline_nested_table_count=0,
                    pipeline_supplementary_effective_date_count=0,
                    pipeline_footnote_caption_count=0,
                    fill_scores=False,
                )
            ]
            rows[0].update(
                {
                    "manual_article_count": "0",
                    "manual_paragraph_item_count": "5",
                    "manual_appendix_form_count": "0",
                    "manual_table_count": "2",
                    "manual_nested_table_count": "0",
                    "manual_supplementary_effective_date_count": "0",
                    "manual_footnote_caption_count": "0",
                }
            )
            with labels_csv.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)

            report = build_parsing_goldset_completion_board(
                labels_csv=labels_csv,
                out_json=root / "reports" / "board.json",
                out_csv=root / "reports" / "board.csv",
                out_md=root / "reports" / "board.md",
                generated_at="2026-07-10T00:00:00+00:00",
            )
            with (root / "reports" / "board.csv").open(encoding="utf-8-sig", newline="") as handle:
                board_rows = list(csv.DictReader(handle))

        self.assertEqual(6, report["completed_structure_score_rows"])
        self.assertEqual(1, report["missing_matched_field_count"])
        self.assertEqual(1, report["structure_completion_summary"]["table"]["missing_matched_count"])
        self.assertEqual(1, report["structure_completion_summary"]["article"]["derived_zero_matched_count"])
        self.assertIn("matched_table_count", board_rows[0]["missing_matched_fields"])
        self.assertNotIn("matched_article_count", board_rows[0]["missing_matched_fields"])
        self.assertIn("derived matched=0", board_rows[0]["human_progress_notes"])


def _seed_labels(root: Path) -> Path:
    rows = [
        _label_row(
            review_order="1",
            label_status="pending_human_review",
            document_id="doc_table_heavy",
            extension=".hwp",
            pipeline_article_count=10,
            pipeline_paragraph_item_count=2,
            pipeline_appendix_form_count=60,
            pipeline_table_count=55,
            pipeline_nested_table_count=1,
            pipeline_supplementary_effective_date_count=5,
            pipeline_footnote_caption_count=0,
            fill_scores=False,
        ),
        _label_row(
            review_order="2",
            label_status="reviewed",
            document_id="doc_complete",
            extension=".pdf",
            pipeline_article_count=2,
            pipeline_paragraph_item_count=3,
            pipeline_appendix_form_count=1,
            pipeline_table_count=1,
            pipeline_nested_table_count=0,
            pipeline_supplementary_effective_date_count=1,
            pipeline_footnote_caption_count=1,
            fill_scores=True,
        ),
    ]
    path = root / "labels.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _label_row(
    *,
    review_order: str,
    label_status: str,
    document_id: str,
    extension: str,
    pipeline_article_count: int,
    pipeline_paragraph_item_count: int,
    pipeline_appendix_form_count: int,
    pipeline_table_count: int,
    pipeline_nested_table_count: int,
    pipeline_supplementary_effective_date_count: int,
    pipeline_footnote_caption_count: int,
    fill_scores: bool,
) -> dict[str, str]:
    row = {
        "review_order": review_order,
        "label_status": label_status,
        "document_id": document_id,
        "extension": extension,
        "institution_name": "Institution",
        "filename": f"{document_id}.pdf",
        "reviewer": "reviewer" if fill_scores else "",
        "reviewed_at": "2026-07-10" if fill_scores else "",
        "pipeline_article_count": str(pipeline_article_count),
        "pipeline_paragraph_item_count": str(pipeline_paragraph_item_count),
        "pipeline_appendix_form_count": str(pipeline_appendix_form_count),
        "pipeline_table_count": str(pipeline_table_count),
        "pipeline_nested_table_count": str(pipeline_nested_table_count),
        "pipeline_supplementary_effective_date_count": str(pipeline_supplementary_effective_date_count),
        "pipeline_footnote_caption_count": str(pipeline_footnote_caption_count),
        "manual_article_count": str(pipeline_article_count) if fill_scores else "",
        "matched_article_count": str(pipeline_article_count) if fill_scores else "",
        "manual_paragraph_item_count": str(pipeline_paragraph_item_count) if fill_scores else "",
        "matched_paragraph_item_count": str(pipeline_paragraph_item_count) if fill_scores else "",
        "manual_appendix_form_count": str(pipeline_appendix_form_count) if fill_scores else "",
        "matched_appendix_form_count": str(pipeline_appendix_form_count) if fill_scores else "",
        "manual_table_count": str(pipeline_table_count) if fill_scores else "",
        "matched_table_count": str(pipeline_table_count) if fill_scores else "",
        "manual_nested_table_count": str(pipeline_nested_table_count) if fill_scores else "",
        "matched_nested_table_count": str(pipeline_nested_table_count) if fill_scores else "",
        "manual_supplementary_effective_date_count": (
            str(pipeline_supplementary_effective_date_count) if fill_scores else ""
        ),
        "matched_supplementary_effective_date_count": (
            str(pipeline_supplementary_effective_date_count) if fill_scores else ""
        ),
        "manual_footnote_caption_count": str(pipeline_footnote_caption_count) if fill_scores else "",
        "matched_footnote_caption_count": str(pipeline_footnote_caption_count) if fill_scores else "",
    }
    return row


if __name__ == "__main__":
    unittest.main()
