from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.build_approval_review_triage import build_approval_review_triage, main


class BuildApprovalReviewTriageTests(unittest.TestCase):
    def test_selects_representative_review_rows_without_approval_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review_batches_json, chunks_json = _seed_inputs(root)

            report = build_approval_review_triage(
                review_batches_json=review_batches_json,
                chunks_json=chunks_json,
                out_csv=root / "reports" / "triage.csv",
                out_json=root / "reports" / "triage.json",
                out_md=root / "reports" / "triage.md",
                max_per_category=2,
                generated_at="2026-07-10T00:00:00+00:00",
            )
            rows = json.loads((root / "reports" / "triage.json").read_text(encoding="utf-8"))["rows"]
            with (root / "reports" / "triage.csv").open(encoding="utf-8-sig", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            markdown_exists = (root / "reports" / "triage.md").is_file()

        self.assertEqual(5, report["selected_count"])
        self.assertEqual(
            {
                "attachment_parentage": 1,
                "table_structure": 1,
                "supplementary_temporal": 1,
                "parser_uncertainty": 1,
                "no_signal_sample": 1,
            },
            report["selected_category_counts"],
        )
        self.assertEqual(len(rows), len({row["chunk_id"] for row in rows}))
        self.assertEqual(5, len(csv_rows))
        self.assertIn("does not approve chunks", report["safety_note"])
        self.assertTrue(markdown_exists)
        self.assertIn("human_label", csv_rows[0])
        self.assertIn("recommended_action", csv_rows[0])

    def test_cli_writes_report_and_allows_category_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review_batches_json, chunks_json = _seed_inputs(root)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--review-batches-json",
                        str(review_batches_json),
                        "--chunks-json",
                        str(chunks_json),
                        "--out-csv",
                        str(root / "reports" / "triage.csv"),
                        "--out-json",
                        str(root / "reports" / "triage.json"),
                        "--out-md",
                        str(root / "reports" / "triage.md"),
                        "--category",
                        "table_structure",
                        "--max-per-category",
                        "1",
                    ]
                )

            report = json.loads((root / "reports" / "triage.json").read_text(encoding="utf-8"))

        self.assertEqual(0, exit_code)
        self.assertIn('"ok": true', stdout.getvalue())
        self.assertEqual(["table_structure"], report["categories"])
        self.assertEqual(1, report["selected_count"])
        self.assertEqual("table_structure", report["rows"][0]["triage_category"])


def _seed_inputs(root: Path) -> tuple[Path, Path]:
    chunks = [
        {
            "chunk_id": "form-1",
            "chunk_type": "form",
            "text": "Form content that should preserve a governing article link.",
            "metadata": {
                "article_no": "Article 1",
                "article_title": "Leave review",
                "regulation_title": "Personnel Rule",
                "form_refs": ["Form 1"],
                "source_page_start": 10,
                "source_page_end": 11,
            },
        },
        {
            "chunk_id": "table-1",
            "chunk_type": "article",
            "text": "Table text with rows and cells.",
            "metadata": {
                "article_no": "Article 2",
                "article_title": "Allowances",
                "regulation_title": "Pay Rule",
                "table_citation_label": "Table 1",
                "table_review_flags": ["row_review_required"],
            },
        },
        {
            "chunk_id": "supp-1",
            "chunk_type": "article",
            "text": "Supplementary provision text.",
            "metadata": {"article_no": "Addenda 1", "regulation_title": "Service Rule"},
        },
        {
            "chunk_id": "parser-1",
            "chunk_type": "article",
            "text": "Parser uncertainty text.",
            "metadata": {
                "article_no": "Article 4",
                "regulation_title": "Service Rule",
                "parser_uncertainty_flags": ["heading_confidence_low"],
            },
        },
        {
            "chunk_id": "clean-1",
            "chunk_type": "article",
            "text": "Low-risk sample text.",
            "metadata": {"article_no": "Article 5", "regulation_title": "Service Rule"},
        },
    ]
    review_batches = {
        "batch_count": 2,
        "approval_chunk_count": 5,
        "manual_attention_chunks": 4,
        "batches": [
            {
                "review_batch_id": "batch-manual-1",
                "review_type": "manual_attention",
                "chunks": [
                    {
                        "chunk_id": "form-1",
                        "chunk_type": "form",
                        "article_no": "Article 1",
                        "article_title": "Leave review",
                        "review_priority_tier": "blocking_review",
                        "review_category": "appendix_form_review",
                        "attention_reasons": [
                            "form_or_appendix_candidate",
                            "review_category:appendix_form_review",
                        ],
                    },
                    {
                        "chunk_id": "table-1",
                        "chunk_type": "article",
                        "article_no": "Article 2",
                        "article_title": "Allowances",
                        "review_priority_tier": "blocking_review",
                        "attention_reasons": ["table_context_candidate", "table_review_required"],
                    },
                    {
                        "chunk_id": "supp-1",
                        "chunk_type": "article",
                        "article_no": "Addenda 1",
                        "review_priority_tier": "domain_attention",
                        "attention_reasons": ["supplementary_temporal_review"],
                    },
                    {
                        "chunk_id": "parser-1",
                        "chunk_type": "article",
                        "article_no": "Article 4",
                        "review_priority_tier": "domain_attention",
                        "attention_reasons": ["parser_uncertainty"],
                    },
                ],
            },
            {
                "review_batch_id": "batch-low-risk-1",
                "review_type": "batch_review",
                "chunks": [
                    {
                        "chunk_id": "clean-1",
                        "chunk_type": "article",
                        "article_no": "Article 5",
                        "review_priority_tier": "no_signal",
                        "attention_reasons": [],
                    }
                ],
            },
        ],
    }
    review_batches_json = root / "review_batches.json"
    chunks_json = root / "chunks.json"
    review_batches_json.write_text(json.dumps(review_batches, ensure_ascii=False), encoding="utf-8")
    chunks_json.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
    return review_batches_json, chunks_json


if __name__ == "__main__":
    unittest.main()
