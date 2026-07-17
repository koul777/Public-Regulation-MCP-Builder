from __future__ import annotations

import unittest

from app.processors.chunker import Chunker
from app.processors.structure_detector import StructureDetector
from app.processors.validator import Validator
from app.schemas.chunk import Chunk, ChunkOptions
from app.schemas.parsed import ParsedBlock, ParsedDocument, ParsedPage


class ValidatorTests(unittest.TestCase):
    def test_detects_article_sequence_gap(self) -> None:
        text = "제1조(목적) 내용\n제3조(누락) 내용"
        parsed = ParsedDocument(
            document_id="doc_gap",
            source_file="gap.md",
            document_name="누락규정",
            file_type="text",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])],
            raw_text=text,
        )
        nodes = StructureDetector().detect(parsed)
        chunks = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=False))
        issues = Validator().validate(nodes, chunks, parsed.document_id)

        self.assertTrue(any(issue.issue_type == "article_sequence_gap" for issue in issues))

    def test_suppresses_expected_amendment_sequence_gap(self) -> None:
        text = "\n".join(
            [
                "제1조(목적) 내용",
                "제7조(다른 법령의 개정) ①부터 <10>까지 생략한다.",
                "제12조(경과조치) 이 규정 시행 전에 처리한 사항은 종전 규정에 따른다.",
            ]
        )
        parsed = ParsedDocument(
            document_id="doc_expected_gap",
            source_file="expected_gap.md",
            document_name="예상누락규정",
            file_type="text",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])],
            raw_text=text,
        )
        nodes = StructureDetector().detect(parsed)
        chunks = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=False))
        issues = Validator().validate(nodes, chunks, parsed.document_id)

        self.assertFalse(any(issue.issue_type == "article_sequence_gap" for issue in issues))

    def test_suppresses_supplementary_gap_with_spaced_transition_title(self) -> None:
        text = "\n".join(
            [
                "부칙 <2026.1.1.>",
                "제1조(시행일) 이 규정은 공포한 날부터 시행한다.",
                "제2조(다른 규정의 개정) 관련 규정을 다음과 같이 개정한다.",
                "제4조(교수직에 대한 경과 조치) 종전 규정에 따른다.",
            ]
        )
        parsed = ParsedDocument(
            document_id="doc_spaced_gap",
            source_file="spaced_gap.md",
            document_name="부칙표기규정",
            file_type="text",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])],
            raw_text=text,
        )
        nodes = StructureDetector().detect(parsed)
        chunks = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=False))
        issues = Validator().validate(nodes, chunks, parsed.document_id)

        self.assertFalse(any(issue.issue_type == "article_sequence_gap" for issue in issues))

    def test_suppresses_gaps_when_article_order_is_mixed_under_same_parent(self) -> None:
        text = "\n".join(
            [
                "제1장 병렬 법령",
                "제1조(첫째) 내용",
                "제3조(셋째) 내용",
                "제2조(둘째) 내용",
                "제4조(넷째) 내용",
            ]
        )
        parsed = ParsedDocument(
            document_id="doc_mixed_order",
            source_file="mixed_order.md",
            document_name="병렬법령",
            file_type="text",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])],
            raw_text=text,
        )
        nodes = StructureDetector().detect(parsed)
        chunks = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=False))
        issues = Validator().validate(nodes, chunks, parsed.document_id)

        self.assertFalse(any(issue.issue_type == "article_sequence_gap" for issue in issues))

    def test_detects_missing_chunks(self) -> None:
        issues = Validator().validate([], [], "doc_empty")

        self.assertTrue(any(issue.issue_type == "no_chunks" for issue in issues))
        self.assertTrue(any(issue.severity == "error" for issue in issues))

    def test_declared_unavailable_source_page_is_info_not_missing_warning(self) -> None:
        chunk = Chunk(
            chunk_id="chunk_kordoc_table",
            document_id="doc_unavailable_page",
            chunk_type="table",
            text="| A | B |\n| --- | --- |\n| 1 | 2 |",
            metadata={
                "document_name": "Rules",
                "source_file": "rules.hwp",
                "hierarchy_path": "Rules > table",
                "chunk_type": "table",
                "source_page_unavailable_reason": "kordoc_table_source_page_missing",
            },
            source_page_start=None,
            source_page_end=None,
        )

        issues = Validator().validate([], [chunk], "doc_unavailable_page")

        self.assertTrue(any(issue.issue_type == "source_page_unavailable" for issue in issues))
        self.assertTrue(any(issue.severity == "info" for issue in issues))
        self.assertFalse(any(issue.issue_type == "page_number_missing" for issue in issues))

    def test_long_kordoc_structured_table_is_info_not_length_warning(self) -> None:
        chunk = Chunk(
            chunk_id="chunk_long_kordoc_table",
            document_id="doc_long_table",
            chunk_type="table",
            text="| A | B |\n" + ("| 1 | 2 |\n" * 320),
            metadata={
                "document_name": "Rules",
                "source_file": "rules.hwp",
                "hierarchy_path": "Rules > table",
                "chunk_type": "table",
                "table_like": True,
                "kordoc_table_promoted": True,
                "table_cell_rows": [{"row_index": 0, "cells": ["A", "B"]}],
            },
            source_page_start=1,
            source_page_end=1,
        )

        issues = Validator().validate([], [chunk], "doc_long_table")

        self.assertTrue(any(issue.issue_type == "long_structured_table_chunk" for issue in issues))
        self.assertTrue(any(issue.severity == "info" for issue in issues))
        self.assertFalse(any(issue.issue_type == "very_long_chunk" for issue in issues))
        self.assertEqual(1.0, chunk.confidence)


if __name__ == "__main__":
    unittest.main()
