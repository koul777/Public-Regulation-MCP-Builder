from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from scripts.build_review_queue_triage import DEFAULT_CATEGORIES, build_triage_packet


class ReviewQueueTriageTests(unittest.TestCase):
    def test_build_triage_packet_groups_representative_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review_csv = root / "review.csv"
            out_csv = root / "triage.csv"
            out_md = root / "triage.md"
            self._write_review_csv(
                review_csv,
                [
                    {
                        "review_category": "table_extraction_blocker",
                        "review_severity_rank": "1",
                        "priority_tier": "blocking_review",
                        "review_group_key": "group-a",
                        "review_group_primary": "True",
                        "review_group_duplicate_count": "2",
                        "institution_name": "기관A",
                        "filename": "a.hwp",
                        "document_id": "doc-a",
                        "chunk_id": "chunk-a1",
                        "chunk_type": "appendix",
                        "table_review_flags": "probable_extraction_failed",
                        "table_classification": "probable_table_extraction_failed",
                        "table_review_reason": "raw_table_like_rows_without_cell_rows",
                        "table_structured_row_count": "0",
                        "table_record_count": "0",
                        "table_header_cells": "Grade | Rate",
                        "snippet": "별표 표 구조",
                    },
                    {
                        "review_category": "table_extraction_blocker",
                        "review_severity_rank": "1",
                        "priority_tier": "blocking_review",
                        "review_group_key": "group-a",
                        "review_group_primary": "False",
                        "review_group_duplicate_count": "2",
                        "institution_name": "기관A",
                        "filename": "a.hwp",
                        "document_id": "doc-a",
                        "chunk_id": "chunk-a2",
                        "chunk_type": "appendix",
                        "table_review_flags": "probable_extraction_failed",
                        "snippet": "중복 표 구조",
                    },
                    {
                        "review_category": "hwp_binary_geometry_review",
                        "review_severity_rank": "6",
                        "priority_tier": "domain_attention",
                        "review_group_key": "group-b",
                        "review_group_primary": "True",
                        "review_group_duplicate_count": "1",
                        "institution_name": "기관B",
                        "filename": "b.hwp",
                        "document_id": "doc-b",
                        "chunk_id": "chunk-b1",
                        "chunk_type": "form",
                        "source_hwp_extraction_modes": "legacy_ole_para_text_only",
                        "source_hwp_native_table_geometry": "false",
                        "snippet": "별지 서식",
                    },
                    {
                        "review_category": "supplementary_effective_date_review",
                        "review_severity_rank": "8",
                        "priority_tier": "domain_attention",
                        "review_group_key": "group-c",
                        "review_group_primary": "True",
                        "review_group_duplicate_count": "1",
                        "institution_name": "기관C",
                        "filename": "c.pdf",
                        "document_id": "doc-c",
                        "chunk_id": "chunk-c1",
                        "chunk_type": "supplementary",
                        "snippet": "부칙",
                    },
                ],
            )

            build_triage_packet(
                review_csv=review_csv,
                out_csv=out_csv,
                out_md=out_md,
                categories=["table_extraction_blocker", "hwp_binary_geometry_review"],
                max_per_category=20,
                generated_at="20260709-test",
            )

            rows = self._read_csv(out_csv)
            self.assertEqual(2, len(rows))
            self.assertEqual("table_extraction_blocker", rows[0]["review_category"])
            self.assertEqual("2", rows[0]["group_size"])
            self.assertEqual("chunk-a1", rows[0]["chunk_id"])
            self.assertEqual("probable_table_extraction_failed", rows[0]["table_classification"])
            self.assertEqual("raw_table_like_rows_without_cell_rows", rows[0]["table_review_reason"])
            self.assertEqual("0", rows[0]["table_structured_row_count"])
            self.assertEqual("0", rows[0]["table_record_count"])
            self.assertEqual("Grade | Rate", rows[0]["table_header_cells"])
            self.assertIn("true_extraction_failure", rows[0]["label_options"])
            self.assertEqual("", rows[0]["human_label"])
            self.assertEqual("hwp_binary_geometry_review", rows[1]["review_category"])
            self.assertIn("real_table_geometry", rows[1]["label_options"])
            markdown = out_md.read_text(encoding="utf-8")
            self.assertIn("Selected groups: 2", markdown)
            self.assertIn("This packet does not approve or downgrade", markdown)

    def test_build_triage_packet_limits_each_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review_csv = root / "review.csv"
            out_csv = root / "triage.csv"
            out_md = root / "triage.md"
            self._write_review_csv(
                review_csv,
                [
                    {
                        "review_category": "table_extraction_blocker",
                        "review_severity_rank": "1",
                        "review_group_key": f"group-{index}",
                        "review_group_primary": "True",
                        "review_group_duplicate_count": "1",
                        "document_id": f"doc-{index}",
                        "chunk_id": f"chunk-{index}",
                    }
                    for index in range(3)
                ],
            )

            build_triage_packet(
                review_csv=review_csv,
                out_csv=out_csv,
                out_md=out_md,
                categories=["table_extraction_blocker"],
                max_per_category=2,
                generated_at="20260709-test",
            )

            rows = self._read_csv(out_csv)
            self.assertEqual(2, len(rows))
            self.assertEqual(["1", "2"], [row["triage_rank"] for row in rows])

    def test_default_categories_include_temporal_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review_csv = root / "review.csv"
            out_csv = root / "triage.csv"
            out_md = root / "triage.md"
            self._write_review_csv(
                review_csv,
                [
                    {
                        "review_category": "parser_uncertainty_blocker",
                        "review_severity_rank": "5",
                        "priority_tier": "blocking_review",
                        "review_group_key": "parser-group",
                        "review_group_primary": "True",
                        "review_group_duplicate_count": "1",
                        "institution_name": "기관P",
                        "filename": "p.pdf",
                        "document_id": "doc-p",
                        "chunk_id": "chunk-p1",
                        "chunk_type": "article",
                        "parser_uncertainty_source": "pdf",
                        "parser_uncertainty_risk_level": "high",
                        "parser_uncertainty_flags": "ocr_required",
                        "parser_uncertainty_recommendation": "run_ocr",
                        "snippet": "parser uncertainty sample",
                    },
                    {
                        "review_category": "supplementary_effective_date_review",
                        "review_severity_rank": "8",
                        "priority_tier": "domain_attention",
                        "review_group_key": "temporal-group",
                        "review_group_primary": "True",
                        "review_group_duplicate_count": "3",
                        "institution_name": "湲곌?C",
                        "filename": "c.pdf",
                        "document_id": "doc-c",
                        "chunk_id": "chunk-c1",
                        "chunk_type": "supplementary",
                        "snippet": "supplementary effective date sample",
                    }
                ],
            )

            build_triage_packet(
                review_csv=review_csv,
                out_csv=out_csv,
                out_md=out_md,
                categories=list(DEFAULT_CATEGORIES),
                max_per_category=20,
                generated_at="20260709-test",
            )

            rows = self._read_csv(out_csv)
            self.assertEqual(2, len(rows))
            self.assertEqual("parser_uncertainty_blocker", rows[0]["review_category"])
            self.assertEqual("high", rows[0]["parser_uncertainty_risk_level"])
            self.assertIn("true_parser_blocker", rows[0]["label_options"])
            self.assertIn("parser uncertainty", rows[0]["suggested_next_action"])
            self.assertEqual("supplementary_effective_date_review", rows[1]["review_category"])
            self.assertEqual("3", rows[1]["group_size"])
            self.assertIn("true_effective_date_issue", rows[1]["label_options"])
            self.assertIn("effective dates", rows[1]["suggested_next_action"])

    def test_build_triage_packet_prefers_document_and_institution_diversity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review_csv = root / "review.csv"
            out_csv = root / "triage.csv"
            out_md = root / "triage.md"
            self._write_review_csv(
                review_csv,
                [
                    {
                        "review_category": "hwp_binary_geometry_review",
                        "review_severity_rank": "6",
                        "review_group_key": "group-a",
                        "review_group_primary": "True",
                        "review_group_duplicate_count": "1",
                        "institution_name": "기관A",
                        "document_id": "doc-a",
                        "chunk_id": "chunk-a1",
                    },
                    {
                        "review_category": "hwp_binary_geometry_review",
                        "review_severity_rank": "6",
                        "review_group_key": "group-b",
                        "review_group_primary": "True",
                        "review_group_duplicate_count": "1",
                        "institution_name": "기관A",
                        "document_id": "doc-a",
                        "chunk_id": "chunk-a2",
                    },
                    {
                        "review_category": "hwp_binary_geometry_review",
                        "review_severity_rank": "6",
                        "review_group_key": "group-c",
                        "review_group_primary": "True",
                        "review_group_duplicate_count": "1",
                        "institution_name": "기관B",
                        "document_id": "doc-b",
                        "chunk_id": "chunk-b1",
                    },
                ],
            )

            build_triage_packet(
                review_csv=review_csv,
                out_csv=out_csv,
                out_md=out_md,
                categories=["hwp_binary_geometry_review"],
                max_per_category=2,
                generated_at="20260709-test",
            )

            rows = self._read_csv(out_csv)
            self.assertEqual(["chunk-a1", "chunk-b1"], [row["chunk_id"] for row in rows])

    def _write_review_csv(self, path: Path, rows: list[dict[str, str]]) -> None:
        fieldnames = sorted({key for row in rows for key in row})
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def _read_csv(self, path: Path) -> list[dict[str, str]]:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]


if __name__ == "__main__":
    unittest.main()
