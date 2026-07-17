from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

from tests.test_chunker import parsed_fixture
from app.processors.chunker import Chunker
from app.processors.exporter import CSV_COLUMNS, TABLE_CSV_COLUMNS, Exporter
from app.processors.structure_detector import StructureDetector
from app.schemas.chunk import Chunk, ChunkOptions


class ExporterTests(unittest.TestCase):
    def setUp(self) -> None:
        parsed = parsed_fixture()
        nodes = StructureDetector().detect(parsed)
        self.chunks = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=False))
        self.exporter = Exporter()

    def test_jsonl_is_valid_line_delimited_json(self) -> None:
        content = self.exporter.to_jsonl(self.chunks)
        rows = [json.loads(line) for line in content.splitlines()]

        self.assertEqual(len(rows), len(self.chunks))
        self.assertIn("chunk_id", rows[0])
        self.assertIn("source_file", rows[0])
        self.assertIn("hierarchy_path", rows[0])

    def test_streaming_file_writers_match_in_memory_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl_path = root / "chunks.jsonl"
            csv_path = root / "chunks.csv"
            markdown_path = root / "chunks.md"
            table_jsonl_path = root / "tables.jsonl"
            table_csv_path = root / "tables.csv"
            progress: list[tuple[int, int]] = []

            self.exporter.write_jsonl(jsonl_path, self.chunks, progress_callback=lambda current, total: progress.append((current, total)))
            self.exporter.write_csv(csv_path, self.chunks)
            self.exporter.write_markdown(markdown_path, self.chunks)
            self.exporter.write_tables_jsonl(table_jsonl_path, self.chunks)
            self.exporter.write_tables_csv(table_csv_path, self.chunks)

            jsonl_rows = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            markdown_content = markdown_path.read_text(encoding="utf-8")
            table_jsonl_exists = table_jsonl_path.is_file()
            table_csv_exists = table_csv_path.is_file()

        self.assertEqual(len(self.chunks), len(jsonl_rows))
        self.assertEqual(len(self.chunks), len(csv_rows))
        self.assertIn(self.chunks[0].text, markdown_content)
        self.assertTrue(table_jsonl_exists)
        self.assertTrue(table_csv_exists)
        self.assertEqual((len(self.chunks), len(self.chunks)), progress[-1])

    def test_csv_contains_required_columns(self) -> None:
        content = self.exporter.to_csv(self.chunks)
        reader = csv.DictReader(io.StringIO(content))

        self.assertEqual(reader.fieldnames, CSV_COLUMNS)
        first = next(reader)
        self.assertEqual(first["source_file"], "sample_regulation.md")
        self.assertTrue(first["text"])

    def test_csv_and_jsonl_include_temporal_metadata(self) -> None:
        chunk = Chunk(
            chunk_id="chunk_supplementary",
            document_id="doc_temporal",
            chunk_type="supplementary_provision",
            text="부칙",
            metadata={
                "document_name": "Temporal Regulation",
                "source_file": "temporal.hwp",
                "hierarchy_path": "Temporal Regulation > 부칙",
                "effective_date": "2026-01-01",
                "revision_date": "2025-12-30",
                "valid_from": "2026-01-01",
                "revision_history_spans": [{"start_line": 1, "end_line": 1}],
                "article_validity_windows": [
                    {"article_ref": "*", "valid_from": "2026-01-01", "valid_to": None}
                ],
                "temporal_metadata_inherited": True,
                "temporal_metadata_scope": "regulation",
                "temporal_metadata_inherited_fields": ["effective_date"],
                "temporal_metadata_normalized_fields": ["valid_from"],
                "temporal_metadata_source_chunk_ids": ["chunk_source"],
                "revision_history": [{"event_type": "개정", "date": "2025-12-30"}],
                "article_effective_overrides": [
                    {"article_ref": "제17조제1항제1호", "effective_date": "2026-01-02"}
                ],
                "is_supplementary_provision": True,
                "supplementary_label": "부칙",
                "supplementary_identifier_date": "2025-12-30",
                "supplementary_paragraph_label": "시행일",
                "supplementary_boilerplate": True,
            },
        )

        json_row = json.loads(self.exporter.to_jsonl([chunk]).strip())
        csv_row = next(csv.DictReader(io.StringIO(self.exporter.to_csv([chunk]))))

        self.assertEqual(json_row["valid_from"], "2026-01-01")
        self.assertEqual(json_row["supplementary_identifier_date"], "2025-12-30")
        self.assertEqual(json_row["supplementary_paragraph_label"], "시행일")
        self.assertTrue(json_row["supplementary_boilerplate"])
        self.assertEqual(json_row["article_effective_overrides"][0]["effective_date"], "2026-01-02")
        self.assertIn("revision_history", csv_row)
        self.assertIn("revision_history_spans", csv_row)
        self.assertIn("article_effective_overrides", csv_row)
        self.assertIn("article_validity_windows", csv_row)
        self.assertIn("2026-01-02", csv_row["article_effective_overrides"])
        self.assertIn("2026-01-01", csv_row["article_validity_windows"])
        self.assertEqual(json_row["temporal_metadata_scope"], "regulation")
        self.assertIn("valid_from", csv_row["temporal_metadata_normalized_fields"])
        self.assertIn("chunk_source", csv_row["temporal_metadata_source_chunk_ids"])
        self.assertEqual(csv_row["supplementary_paragraph_label"], "시행일")
        self.assertEqual(csv_row["supplementary_boilerplate"], "True")

    def test_export_does_not_fabricate_valid_from_from_effective_date(self) -> None:
        chunk = Chunk(
            chunk_id="chunk_effective_only",
            document_id="doc_temporal",
            chunk_type="article",
            text="제1조",
            metadata={
                "document_name": "Temporal Regulation",
                "source_file": "temporal.hwp",
                "effective_date": "2026-01-01",
            },
        )

        json_row = json.loads(self.exporter.to_jsonl([chunk]).strip())
        csv_row = next(csv.DictReader(io.StringIO(self.exporter.to_csv([chunk]))))

        self.assertEqual(json_row["effective_date"], "2026-01-01")
        self.assertIsNone(json_row["valid_from"])
        self.assertEqual(csv_row["valid_from"], "")

    def test_markdown_contains_document_and_article_headings(self) -> None:
        content = self.exporter.to_markdown(self.chunks)

        self.assertIn("# 복무규정", content)
        self.assertIn("## 제1조 목적", content)
        self.assertIn("메타데이터:", content)

    def test_table_exports_flatten_structured_rows(self) -> None:
        chunk = Chunk(
            chunk_id="chunk_table",
            document_id="doc_table",
            chunk_type="appendix",
            text="\uc7ac\uc0b0\uba85 \uc218\ub7c9 \ud3c9\uac00\uc561",
            metadata={
                "hierarchy_path": "\uc0d8\ud50c > \ubcc4\ud45c",
                "table_cell_rows": [
                    {
                        "row_index": 0,
                        "cells": ["\uc7ac\uc0b0\uba85", "\uc218\ub7c9", "\ud3c9\uac00\uc561"],
                        "raw": "\uc7ac\uc0b0\uba85 \uc218\ub7c9 \ud3c9\uac00\uc561",
                    },
                    {
                        "row_index": 1,
                        "cells": ["\ud1a0\uc9c0", "1", "100"],
                        "raw": "\ud1a0\uc9c0 1 100",
                    },
                ],
                "table_header_cells": ["\uc7ac\uc0b0\uba85", "\uc218\ub7c9", "\ud3c9\uac00\uc561"],
                "table_records": [
                    {
                        "row_index": 1,
                        "header_cells": ["\uc7ac\uc0b0\uba85", "\uc218\ub7c9", "\ud3c9\uac00\uc561"],
                        "record": {"\uc7ac\uc0b0\uba85": "\ud1a0\uc9c0", "\uc218\ub7c9": "1", "\ud3c9\uac00\uc561": "100"},
                    },
                ],
                "table_classification": "structured_table",
                "table_confidence": 0.88,
                "table_review_reason": "header_and_numeric_rows",
                "table_source": "kordoc",
                "table_geometry_source": "kordoc",
                "primary_parser_table_source": "hwp_parser",
                "kordoc_table_parser_status": "parsed",
                "kordoc_table_count": 3,
                "kordoc_table_promoted": True,
                "kordoc_table_promotion_review_required": True,
                "kordoc_table_unmatched_source": False,
                "table_appendix_no": "별표1",
                "table_appendix_title": "재산 평가표",
                "table_citation_label": "별표1 재산 평가표",
                "table_review_required": True,
                "table_review_flags": ["row_review_required"],
                "table_header_hits": 2,
                "table_numeric_rows": 1,
                "table_delimiter_rows": 0,
            },
            source_page_start=7,
            source_page_end=7,
        )

        json_rows = [json.loads(line) for line in self.exporter.to_tables_jsonl([chunk]).splitlines()]
        csv_reader = csv.DictReader(io.StringIO(self.exporter.to_tables_csv([chunk])))
        csv_row = next(csv_reader)

        self.assertEqual(len(json_rows), 2)
        self.assertEqual(json_rows[0]["row_kind"], "cell")
        self.assertEqual(json_rows[0]["cell_count"], 3)
        self.assertEqual(json_rows[1]["record"]["\uc7ac\uc0b0\uba85"], "\ud1a0\uc9c0")
        self.assertEqual(json_rows[1]["header_cells"], ["\uc7ac\uc0b0\uba85", "\uc218\ub7c9", "\ud3c9\uac00\uc561"])
        self.assertEqual(json_rows[0]["table_classification"], "structured_table")
        self.assertEqual(json_rows[0]["table_confidence"], 0.88)
        self.assertEqual(json_rows[0]["table_source"], "kordoc")
        self.assertEqual(json_rows[0]["table_geometry_source"], "kordoc")
        self.assertEqual(json_rows[0]["primary_parser_table_source"], "hwp_parser")
        self.assertEqual(json_rows[0]["kordoc_table_parser_status"], "parsed")
        self.assertEqual(json_rows[0]["kordoc_table_count"], 3)
        self.assertTrue(json_rows[0]["kordoc_table_promoted"])
        self.assertTrue(json_rows[0]["kordoc_table_promotion_review_required"])
        self.assertFalse(json_rows[0]["kordoc_table_unmatched_source"])
        self.assertEqual(json_rows[0]["table_header_hits"], 2)
        self.assertEqual(json_rows[0]["appendix_no"], "별표1")
        self.assertEqual(json_rows[0]["appendix_title"], "재산 평가표")
        self.assertEqual(json_rows[0]["citation_label"], "별표1 재산 평가표")
        self.assertTrue(json_rows[0]["review_required"])
        self.assertEqual(json_rows[0]["review_flags"], ["row_review_required"])
        self.assertEqual(csv_reader.fieldnames, TABLE_CSV_COLUMNS)
        self.assertIn("\uc7ac\uc0b0\uba85", csv_row["cells"])
        second_csv_row = next(csv_reader)
        self.assertIn('"\uc7ac\uc0b0\uba85": "\ud1a0\uc9c0"', second_csv_row["record_json"])
        self.assertIn("\uc7ac\uc0b0\uba85", second_csv_row["header_cells"])
        self.assertEqual(csv_row["citation_label"], "별표1 재산 평가표")
        self.assertIn("row_review_required", csv_row["review_flags"])
        self.assertEqual(csv_row["table_review_reason"], "header_and_numeric_rows")
        self.assertEqual(csv_row["table_source"], "kordoc")
        self.assertEqual(csv_row["kordoc_table_parser_status"], "parsed")
        self.assertEqual(csv_row["kordoc_table_count"], "3")
        self.assertEqual(csv_row["kordoc_table_promoted"], "True")

    def test_table_exports_preserve_raw_rows_when_cell_rows_are_empty(self) -> None:
        chunk = Chunk(
            chunk_id="chunk_raw_table",
            document_id="doc_table",
            chunk_type="form",
            text="\uc2e0\uccad\uc11c",
            metadata={
                "hierarchy_path": "\uc0d8\ud50c > \ubcc4\uc9c0",
                "table_like": True,
                "table_rows": ["\uc2e0\uccad\uc790 \uc131\uba85", "\ub144 \uc6d4 \uc77c", "\uc2e0\uccad\uc778: (\uc778)"],
                "table_cell_rows": [],
            },
            source_page_start=9,
            source_page_end=9,
        )

        json_rows = [json.loads(line) for line in self.exporter.to_tables_jsonl([chunk]).splitlines()]
        manifest = self.exporter.manifest([chunk], [])

        self.assertEqual(len(json_rows), 3)
        self.assertTrue(all(row["row_kind"] == "raw" for row in json_rows))
        self.assertEqual(json_rows[0]["cells"], ["\uc2e0\uccad\uc790 \uc131\uba85"])
        self.assertEqual(manifest["table_row_count"], 3)
        self.assertEqual(manifest["structured_table_row_count"], 0)
        self.assertEqual(manifest["raw_table_row_count"], 3)

    def test_jsonl_appends_table_markdown_to_retrieval_text(self) -> None:
        chunk = Chunk(
            chunk_id="chunk_table",
            document_id="doc_table",
            chunk_type="appendix",
            text="구분 내용\nA B",
            retrieval_text="[문서명] 표규정\n[본문]\n구분 내용",
            metadata={
                "table_markdown": "| 구분 | 내용 |\n| --- | --- |\n| A | B |",
            },
        )

        row = json.loads(self.exporter.to_jsonl([chunk]).strip())

        self.assertIn("[표]", row["retrieval_text"])
        self.assertIn("| 구분 | 내용 |", row["retrieval_text"])


if __name__ == "__main__":
    unittest.main()
