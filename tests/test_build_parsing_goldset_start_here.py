from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_parsing_goldset_start_here import build_parsing_goldset_start_here, main


class BuildParsingGoldsetStartHereTests(unittest.TestCase):
    def test_builds_start_here_packet_with_open_commands_and_first_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels = _seed_labels(root)
            packet = root / "packets" / "doc_a.md"
            packet.parent.mkdir()
            packet.write_text("packet", encoding="utf-8")
            completion = root / "completion.json"
            _write_json(completion, _completion_payload(packet))
            table_batches = _seed_table_batches(root)

            report = build_parsing_goldset_start_here(
                labels_csv=Path("labels.csv"),
                completion_board_json=Path("completion.json"),
                table_review_batches_csv=Path("table_batches.csv"),
                out_json=root / "reports" / "start.json",
                out_md=root / "reports" / "start.md",
                out_worklist_csv=root / "reports" / "start_worklist.csv",
                top_doc_count=1,
                first_table_batch_count=1,
                base_dir=root,
                generated_at="2026-07-10T00:00:00+00:00",
            )
            payload = json.loads((root / "reports" / "start.json").read_text(encoding="utf-8"))
            markdown = (root / "reports" / "start.md").read_text(encoding="utf-8")
            with (root / "reports" / "start_worklist.csv").open(encoding="utf-8-sig", newline="") as handle:
                worklist_rows = list(csv.DictReader(handle))

        self.assertEqual(labels.resolve(), Path(report["source_artifacts"]["labels_csv"]["path"]))
        self.assertIn("Invoke-Item -LiteralPath", report["open_commands"]["open_label_csv"])
        self.assertIn("explorer.exe /select", report["open_commands"]["select_label_csv_in_explorer"])
        self.assertEqual(1, report["top_document_count"])
        self.assertEqual("doc_a", report["top_documents"][0]["document_id"])
        self.assertTrue(report["top_documents"][0]["source_exists"])
        self.assertTrue(report["top_documents"][0]["packet_exists"])
        self.assertEqual(7, report["top_documents"][0]["field_counts"]["missing_manual"])
        self.assertEqual(1, report["top_documents"][0]["field_counts"]["missing_label_status"])
        self.assertFalse(report["top_documents"][0]["label_status_ready"])
        self.assertEqual(17, report["open_item_summary"]["open_item_count"])
        self.assertEqual(7, report["open_item_summary"]["item_kind_counts"]["manual_count"])
        self.assertEqual(7, report["open_item_summary"]["item_kind_counts"]["matched_count"])
        self.assertEqual(2, report["open_item_summary"]["item_kind_counts"]["reviewer_metadata"])
        self.assertEqual(1, report["open_item_summary"]["item_kind_counts"]["label_status"])
        self.assertEqual(17, len(worklist_rows))
        self.assertEqual("manual_count", worklist_rows[0]["item_kind"])
        self.assertEqual("article", report["structure_review_queue"][0]["structure"])
        self.assertEqual(["doc_a"], report["structure_review_queue"][0]["first_document_ids"])
        self.assertEqual(1, report["first_table_batch_count"])
        self.assertEqual("batch-1", report["first_table_batches"][0]["table_review_batch_id"])
        self.assertTrue(report["first_table_batches"][0]["source_exists"])
        self.assertEqual("parsing_goldset_start_here", payload["report_type"])
        self.assertIn("output_artifacts", payload)
        self.assertIn("Parsing Goldset Start Here", markdown)
        self.assertIn("Open-item worklist CSV", markdown)
        self.assertIn("Structure Review Queue", markdown)
        self.assertIn("doc_a", markdown)

    def test_cli_writes_start_here_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_labels(root)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--labels-csv",
                        str(root / "labels.csv"),
                        "--out-json",
                        str(root / "reports" / "start.json"),
                        "--out-md",
                        str(root / "reports" / "start.md"),
                    ]
                )
            payload = json.loads((root / "reports" / "start.json").read_text(encoding="utf-8"))

        self.assertEqual(0, exit_code)
        self.assertIn('"ok": true', stdout.getvalue())
        self.assertEqual(2, payload["top_document_count"])
        self.assertEqual("parsing_goldset_start_here", payload["report_type"])


def _seed_labels(root: Path) -> Path:
    source_a = root / "source_a.pdf"
    source_a.write_text("source", encoding="utf-8")
    source_b = root / "source_b.pdf"
    source_b.write_text("source", encoding="utf-8")
    rows = [
        _label_row("1", "doc_a", source_a),
        _label_row("2", "doc_b", source_b),
    ]
    path = root / "labels.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _label_row(review_order: str, document_id: str, source_path: Path) -> dict[str, str]:
    return {
        "review_order": review_order,
        "label_status": "pending_human_review",
        "document_id": document_id,
        "extension": ".pdf",
        "institution_name": "Institution",
        "filename": f"{document_id}.pdf",
        "source_path": str(source_path),
        "chunk_artifact": "",
        "reviewer": "",
        "reviewed_at": "",
        "pipeline_article_count": "1",
        "manual_article_count": "",
        "matched_article_count": "",
        "pipeline_paragraph_item_count": "2",
        "manual_paragraph_item_count": "",
        "matched_paragraph_item_count": "",
        "pipeline_appendix_form_count": "3",
        "manual_appendix_form_count": "",
        "matched_appendix_form_count": "",
        "pipeline_table_count": "4",
        "manual_table_count": "",
        "matched_table_count": "",
        "pipeline_nested_table_count": "0",
        "manual_nested_table_count": "",
        "matched_nested_table_count": "",
        "pipeline_supplementary_effective_date_count": "5",
        "manual_supplementary_effective_date_count": "",
        "matched_supplementary_effective_date_count": "",
        "pipeline_footnote_caption_count": "6",
        "manual_footnote_caption_count": "",
        "matched_footnote_caption_count": "",
    }


def _completion_payload(packet: Path) -> dict:
    return {
        "report_type": "parsing_goldset_completion_board",
        "document_count": 2,
        "ready_document_count": 0,
        "pending_document_count": 2,
        "expected_structure_score_rows": 14,
        "completed_structure_score_rows": 0,
        "missing_manual_field_count": 14,
        "missing_matched_field_count": 14,
        "missing_reviewer_metadata_count": 2,
        "completion_gate_status": "blocked_pending_human_labels",
        "ready_for_quality_claim": False,
        "structure_completion_summary": {
            "article": {
                "expected_document_count": 2,
                "score_rows_complete": 0,
                "missing_matched_count": 2,
                "pipeline_total": 2,
                "ready_for_structure_f1": False,
            },
            "nested_table": {
                "expected_document_count": 2,
                "score_rows_complete": 2,
                "missing_matched_count": 0,
                "pipeline_total": 0,
                "ready_for_structure_f1": True,
            },
        },
        "rows": [
            {
                "priority_rank": "1",
                "review_order": "1",
                "document_id": "doc_a",
                "label_status": "pending_human_review",
                "ready_for_quality_claim": "false",
                "priority_tier": "table_heavy_first",
                "recommended_next_action": "Review table-heavy document first.",
                "packet_path": str(packet),
                "score_rows_complete": "0",
                "score_rows_expected": "7",
                "missing_manual_fields": (
                    "manual_article_count; manual_paragraph_item_count; manual_appendix_form_count; "
                    "manual_table_count; manual_nested_table_count; manual_supplementary_effective_date_count; "
                    "manual_footnote_caption_count"
                ),
                "missing_matched_fields": (
                    "matched_article_count; matched_paragraph_item_count; matched_appendix_form_count; "
                    "matched_table_count; matched_nested_table_count; matched_supplementary_effective_date_count; "
                    "matched_footnote_caption_count"
                ),
                "missing_reviewer_metadata": "reviewer; reviewed_at",
                "missing_structures": "article",
            }
        ],
    }


def _seed_table_batches(root: Path) -> Path:
    path = root / "table_batches.csv"
    rows = [
        {
            "batch_rank": "1",
            "table_review_batch_id": "batch-1",
            "document_id": "doc_a",
            "review_priority": "source_table_compare",
            "unit_count": "3",
            "source_page_ranges": "1-2",
            "label_review_flag_counts": "missing_table_label=3",
            "source_path": str(root / "source_a.pdf"),
            "table_unit_packet_csv": str(root / "table_units.csv"),
        }
    ]
    (root / "table_units.csv").write_text("unit\n1\n", encoding="utf-8")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
