from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts.audit_parser_goldset_batch_scenarios import (
    build_parser_goldset_batch_scenario_audit,
    count_upper_bound,
)


class AuditParserGoldsetBatchScenariosTests(unittest.TestCase):
    def test_count_upper_bound_is_not_limited_to_strict_scorable_rows(self) -> None:
        payload = {
            "documents": [
                {
                    "excluded_from_quality_claim": False,
                    "scores": {
                        "article": {"manual_count": 2, "pipeline_count": 3},
                        "paragraph_item": {"manual_count": 5, "pipeline_count": 4},
                    },
                },
                {
                    "excluded_from_quality_claim": True,
                    "scores": {
                        "article": {"manual_count": 100, "pipeline_count": 100},
                    },
                },
            ]
        }

        report = count_upper_bound(payload)

        self.assertEqual("count_only_upper_bound_not_release_claim", report["measurement_kind"])
        self.assertEqual(7, report["overall"]["manual_total"])
        self.assertEqual(7, report["overall"]["pipeline_total"])
        self.assertEqual(6, report["overall"]["matched_total"])
        self.assertEqual(85.71, report["overall"]["f1"])
        self.assertIn("do not use it as a public accuracy claim", report["claim_safety_note"])

    def test_build_audit_flags_stale_batch_and_writes_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels = _write_labels(
                root,
                [
                    _label(
                        document_id="doc_a",
                        filename="a.pdf",
                        pipeline_paragraph="1",
                        manual_paragraph="2",
                        matched_paragraph="1",
                    ),
                ],
            )
            batch_report = _write_batch(root, "doc_a", "a.pdf")
            _write_chunks(
                root,
                "doc_a",
                [
                    {
                        "chunk_type": "article",
                        "text": "제1조 목적\n① 항목\n② 항목",
                        "metadata": {
                            "chunk_type": "article",
                            "article_no": "제1조",
                            "paragraph_item_unit_count": 3,
                        },
                    }
                ],
            )

            report = build_parser_goldset_batch_scenario_audit(
                labels_csv=labels,
                batch_reports=[batch_report],
                workspace=root,
                reports_dir=root / "reports",
                min_f1=90.0,
                out_json=root / "reports" / "audit.json",
                out_md=root / "reports" / "audit.md",
                generated_at="2026-07-13T00:00:00+00:00",
            )
            payload = json.loads((root / "reports" / "audit.json").read_text(encoding="utf-8"))
            markdown = (root / "reports" / "audit.md").read_text(encoding="utf-8")

        self.assertEqual("parser_goldset_batch_scenario_audit", report["report_type"])
        self.assertEqual(1, report["scenario_count"])
        scenario = report["scenarios"][0]
        self.assertEqual("parser_improvement_required", scenario["status"])
        self.assertEqual(1, scenario["issue_summary"]["stale_after_reprocess_count"])
        self.assertEqual(85.71, scenario["count_upper_bound"]["overall"]["f1"])
        self.assertEqual(report["best_upper_bound_f1"], payload["best_upper_bound_f1"])
        self.assertIn("Count-only upper bound F1", markdown)


def _write_labels(root: Path, rows: list[dict[str, str]]) -> Path:
    path = root / "labels.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _label(
    *,
    document_id: str,
    filename: str,
    pipeline_paragraph: str,
    manual_paragraph: str,
    matched_paragraph: str,
) -> dict[str, str]:
    return {
        "document_id": document_id,
        "label_status": "reviewed",
        "reviewer": "reviewer-a",
        "reviewed_at": "2026-07-13",
        "filename": filename,
        "source_path": filename,
        "pipeline_article_count": "1",
        "manual_article_count": "1",
        "matched_article_count": "1",
        "pipeline_paragraph_item_count": pipeline_paragraph,
        "manual_paragraph_item_count": manual_paragraph,
        "matched_paragraph_item_count": matched_paragraph,
        "pipeline_appendix_form_count": "0",
        "manual_appendix_form_count": "0",
        "matched_appendix_form_count": "0",
        "pipeline_table_count": "0",
        "manual_table_count": "0",
        "matched_table_count": "0",
        "pipeline_nested_table_count": "0",
        "manual_nested_table_count": "0",
        "matched_nested_table_count": "0",
        "pipeline_supplementary_effective_date_count": "0",
        "manual_supplementary_effective_date_count": "0",
        "matched_supplementary_effective_date_count": "0",
        "pipeline_footnote_caption_count": "0",
        "manual_footnote_caption_count": "0",
        "matched_footnote_caption_count": "0",
    }


def _write_batch(root: Path, document_id: str, filename: str) -> Path:
    path = root / "reports" / "batch_quality_fixture.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "document_id": document_id,
                        "status": "completed",
                        "filename": filename,
                        "input_path": filename,
                        "chunk_count": 1,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _write_chunks(root: Path, document_id: str, chunks: list[dict[str, object]]) -> None:
    path = root / "data" / "repository" / f"{document_id}_chunks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
