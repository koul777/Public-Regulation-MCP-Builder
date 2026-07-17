from __future__ import annotations

import unittest
from pathlib import Path

from app.core.config import Settings
from app.processors.answer_profile import ANSWER_PROFILE_MARKER, build_answer_profile
from app.processors.chunker import CHUNKER_VERSION, Chunker, REFERENCE_SECTION_MARKER
from app.processors.exporter import Exporter
from app.processors.structure_detector import StructureDetector
from app.schemas.chunk import Chunk, ChunkOptions
from app.schemas.parsed import ParsedBlock, ParsedDocument, ParsedPage
from app.schemas.structure import StructureNode


FIXTURE = Path(__file__).parent / "fixtures" / "sample_regulation.md"


def parsed_fixture() -> ParsedDocument:
    text = FIXTURE.read_text(encoding="utf-8")
    return ParsedDocument(
        document_id="doc_test",
        source_file="sample_regulation.md",
        document_name="복무규정",
        file_type="text",
        pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])],
        raw_text=text,
    )


class ChunkerTests(unittest.TestCase):
    def test_reports_each_regulation_while_chunking_integrated_book(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_integrated",
            source_file="integrated.txt",
            document_name="통합 규정집",
            file_type="text",
            raw_text="인사규정\n제1조 내용\n회계규정\n제1조 내용",
        )
        nodes = [
            StructureNode(
                node_id="reg-1",
                document_id=parsed.document_id,
                node_type="regulation",
                number="1-1",
                title="인사규정",
                text="",
                order_index=0,
            ),
            StructureNode(
                node_id="article-1",
                document_id=parsed.document_id,
                node_type="article",
                number="제1조",
                title="목적",
                text="제1조 내용",
                parent_id="reg-1",
                order_index=1,
            ),
            StructureNode(
                node_id="reg-2",
                document_id=parsed.document_id,
                node_type="regulation",
                number="1-2",
                title="회계규정",
                text="",
                order_index=2,
            ),
            StructureNode(
                node_id="article-2",
                document_id=parsed.document_id,
                node_type="article",
                number="제1조",
                title="목적",
                text="제1조 내용",
                parent_id="reg-2",
                order_index=3,
            ),
        ]
        progress: list[tuple[int, int, str]] = []

        chunks = Chunker().build_chunks(
            nodes,
            parsed,
            ChunkOptions(include_context_header=False),
            regulation_progress_callback=lambda current, total, label: progress.append((current, total, label)),
        )

        self.assertEqual(2, len(chunks))
        self.assertEqual([(1, 2, "인사규정"), (2, 2, "회계규정")], progress)

    def test_kordoc_main_mode_tolerates_missing_inventory(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_missing_kordoc",
            source_file="rules.hwp",
            document_name="Rules",
            file_type="hwp",
            metadata={},
        )
        node = StructureNode(
            node_id="node_table",
            document_id=parsed.document_id,
            node_type="table",
            number="",
            title="Delegation table",
            text="| A | B |\n| --- | --- |\n| 1 | 2 |",
            page_start=1,
            page_end=1,
            order_index=1,
            metadata={"table_like": True},
        )

        chunks = Chunker(Settings(kordoc_table_as_main=True)).build_chunks(
            [node],
            parsed,
            ChunkOptions(include_context_header=False),
        )

        self.assertEqual(1, len(chunks))
        self.assertNotEqual("kordoc", chunks[0].metadata.get("table_source"))
        self.assertNotIn("kordoc_table_promoted", chunks[0].metadata)

    def test_aks_kordoc_grid_replaces_flattened_primary_table(self) -> None:
        chunk = Chunk(
            chunk_id="chunk_aks_flattened_table",
            document_id="doc_aks_kordoc_main",
            chunk_type="appendix",
            text="연구직 경력기간 환산율표 경력종별 환산율 1. 대학 연구기관에서 연 구에 종사한 경력 100%",
            normalized_text="연구직 경력기간 환산율표 경력종별 환산율 1. 대학 연구기관에서 연 구에 종사한 경력 100%",
            retrieval_text="연구직 경력기간 환산율표 경력종별 환산율 1. 대학 연구기관에서 연 구에 종사한 경력 100%",
            metadata={
                "table_like": True,
                "table_source": "hwp_parser",
                "table_cell_rows": [
                    {"row_index": 0, "cells": ["경력종별", "환산율", "1."]},
                ],
                "table_classification": "probable_table_extraction_failed",
            },
        )
        parsed = ParsedDocument(
            document_id="doc_aks_kordoc_main",
            source_file="aks.hwp",
            document_name="AKS 규정",
            file_type="hwp",
            metadata={
                "kordoc_table_inventory": {
                    "status": "parsed",
                    "table_count": 1,
                    "tables": [
                        {
                            "table_index": 12,
                            "row_count": 2,
                            "column_count": 2,
                            "cell_count": 4,
                            "title": "연구직 경력기간 환산율표",
                            "grid_has_header": True,
                            "grid_rows": [
                                {"cells": ["경력종별", "환산율"]},
                                {"cells": ["대학 연구기관에서 연구에 종사한 경력", "100%"]},
                            ],
                        }
                    ],
                }
            },
        )

        Chunker(Settings(kordoc_table_as_main=True))._promote_kordoc_main_tables(
            [chunk], parsed, ChunkOptions(include_context_header=False)
        )

        self.assertEqual("kordoc", chunk.metadata["table_source"])
        self.assertEqual("kordoc", chunk.metadata["table_geometry_source"])
        self.assertEqual(
            ["경력종별", "환산율"], chunk.metadata["table_cell_rows"][0]["cells"]
        )
        self.assertEqual("hwp_parser", chunk.metadata["primary_parser_table_source"])
        self.assertIn("primary_parser_table_hint", chunk.metadata)
        self.assertNotIn("probable_table_extraction_failed", chunk.metadata)

    def test_pdf_table_region_duplicate_is_attached_to_existing_form_chunk(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_pdf_overlap",
            source_file="rules.pdf",
            document_name="Rules",
            file_type="pdf",
            metadata={
                "pdf_table_regions": [
                    {
                        "source_page": 2,
                        "source_bbox": [10, 10, 90, 90],
                        "title": "Form table",
                        "text": "Form table text",
                        "column_count": 2,
                        "row_count": 3,
                    }
                ]
            },
        )
        node = StructureNode(
            node_id="node_form_1",
            document_id=parsed.document_id,
            node_type="form",
            number="Form 1",
            title="Application",
            text="Existing form text",
            page_start=2,
            page_end=2,
            order_index=1,
            metadata={"source_bbox": [0, 0, 100, 100]},
        )

        chunks = Chunker().build_chunks([node], parsed, ChunkOptions(include_context_header=False))

        self.assertEqual(1, len(chunks))
        self.assertEqual("form", chunks[0].chunk_type)
        self.assertTrue(chunks[0].metadata["pdf_table_region_duplicate_suppressed"])
        self.assertEqual(1, chunks[0].metadata["suppressed_pdf_table_region_count"])
        self.assertEqual("Form table", chunks[0].metadata["suppressed_pdf_table_regions"][0]["title"])
        self.assertEqual(2, chunks[0].metadata["table_column_count"])
        self.assertEqual(3, chunks[0].metadata["table_structured_row_count"])
        self.assertEqual("pdf_ruling_line_table", chunks[0].metadata["table_classification"])
        self.assertTrue(chunks[0].metadata["pdf_table_region_layout_evidence"])

    def test_pdf_table_region_without_existing_overlap_stays_table_chunk(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_pdf_separate",
            source_file="rules.pdf",
            document_name="Rules",
            file_type="pdf",
            metadata={
                "pdf_table_regions": [
                    {
                        "source_page": 2,
                        "source_bbox": [200, 200, 300, 300],
                        "title": "Separate table",
                        "text": "Separate table text",
                        "column_count": 2,
                        "row_count": 3,
                    }
                ]
            },
        )
        node = StructureNode(
            node_id="node_form_1",
            document_id=parsed.document_id,
            node_type="form",
            number="Form 1",
            title="Application",
            text="Existing form text",
            page_start=2,
            page_end=2,
            order_index=1,
            metadata={"source_bbox": [0, 0, 100, 100]},
        )

        chunks = Chunker().build_chunks([node], parsed, ChunkOptions(include_context_header=False))

        self.assertEqual(2, len(chunks))
        self.assertEqual(["form", "table"], [chunk.chunk_type for chunk in chunks])
        self.assertFalse(chunks[0].metadata.get("pdf_table_region_duplicate_suppressed", False))
        self.assertEqual("pdf_ruling_line_table", chunks[1].metadata["table_classification"])

    def test_pdf_footnote_marker_references_attach_once_to_covering_chunk(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_pdf_footnotes",
            source_file="rules.pdf",
            document_name="Rules",
            file_type="pdf",
            metadata={
                "pdf_footnote_links": [
                    {"source_page": 23, "marker": "ⅰ"},
                    {"source_page": 24, "marker": "ⅶ"},
                ],
                "pdf_footnote_marker_references": [
                    {"source_page": 23, "marker_count": 8, "markers": ["ⅰ", "ⅱ"]},
                    {"source_page": 24, "marker_count": 7, "markers": ["ⅰ", "ⅲ", "ⅴ"]},
                ],
            },
        )
        first = StructureNode(
            node_id="node_appendix",
            document_id=parsed.document_id,
            node_type="appendix",
            number="별표4",
            title="보상대상",
            text="별표 본문",
            page_start=23,
            page_end=24,
            order_index=1,
        )
        second = StructureNode(
            node_id="node_form",
            document_id=parsed.document_id,
            node_type="form",
            number="별지",
            title="다음 서식",
            text="서식 본문",
            page_start=24,
            page_end=24,
            order_index=2,
        )

        chunks = Chunker().build_chunks([first, second], parsed, ChunkOptions(include_context_header=False))

        self.assertEqual(15, chunks[0].metadata["footnote_marker_reference_count"])
        self.assertEqual(2, len(chunks[0].metadata["footnote_marker_references"]))
        self.assertEqual(2, len(chunks[0].metadata["footnote_links"]))
        self.assertNotIn("footnote_marker_reference_count", chunks[1].metadata)
        self.assertNotIn("footnote_links", chunks[1].metadata)

    def test_document_inventory_metadata_targets_table_like_chunk(self) -> None:
        chunks = [
            Chunk(
                chunk_id="chunk_intro",
                document_id="doc_inventory",
                source_node_ids=["node_intro"],
                chunk_type="article",
                text="intro text",
                retrieval_text="intro text",
                metadata={},
            ),
            Chunk(
                chunk_id="chunk_table",
                document_id="doc_inventory",
                source_node_ids=["node_table"],
                chunk_type="article",
                text="table text",
                retrieval_text="table text",
                metadata={"table_like": True},
            ),
        ]
        parsed = ParsedDocument(
            document_id="doc_inventory",
            source_file="inventory.hwp",
            file_type="hwp",
            metadata={
                "document_inventory": {"tables": {"total": 1}},
                "kordoc_table_inventory": {"status": "parsed", "table_count": 1, "tables": []},
            },
        )

        Chunker()._attach_document_inventory_metadata(chunks, parsed)

        self.assertNotIn("document_inventory", chunks[0].metadata)
        self.assertNotIn("kordoc_table_inventory", chunks[0].metadata)
        self.assertEqual(chunks[1].metadata["document_inventory"]["tables"]["total"], 1)
        self.assertEqual(chunks[1].metadata["kordoc_table_inventory"]["table_count"], 1)
        self.assertEqual(chunks[1].metadata["kordoc_table_count"], 1)

    def test_kordoc_promotion_uses_cell_rows_when_markdown_is_missing(self) -> None:
        chunk = Chunk(
            chunk_id="chunk_table",
            document_id="doc_kordoc_fallback",
            chunk_type="article",
            text="Item Standard Manager 10",
            normalized_text="Item Standard Manager 10",
            retrieval_text="Item Standard Manager 10",
            metadata={
                "table_like": True,
                "table_rows": ["OLD ROW"],
                "table_header_cells": ["OLD_H1", "OLD_H2"],
                "table_records": [{"row_index": 1, "record": {"OLD_H1": "stale"}}],
                "table_probable_false_positive": True,
                "table_confidence": 0.1,
                "table_false_positive_stability": "stable",
            },
        )
        parsed = ParsedDocument(
            document_id="doc_kordoc_fallback",
            source_file="rules.hwp",
            document_name="Rules",
            file_type="hwp",
            metadata={
                "kordoc_table_inventory": {
                    "status": "parsed",
                    "table_count": 1,
                    "tables": [
                        {
                            "table_index": 1,
                            "row_count": 2,
                            "column_count": 2,
                            "cell_count": 4,
                            "cell_rows": [
                                {"cells": ["Item", "Standard"]},
                                {"cells": ["Manager", "10"]},
                            ],
                        }
                    ],
                }
            },
        )

        Chunker()._promote_kordoc_main_tables([chunk], parsed, ChunkOptions(include_context_header=False))

        self.assertTrue(chunk.metadata["kordoc_table_promoted"])
        self.assertEqual(chunk.metadata["table_source"], "kordoc")
        self.assertEqual(chunk.metadata["kordoc_table_parser_status"], "parsed")
        self.assertEqual(chunk.metadata["kordoc_table_count"], 1)
        self.assertEqual(
            [row["cells"] for row in chunk.metadata["table_cell_rows"]],
            [["Item", "Standard"], ["Manager", "10"]],
        )
        self.assertEqual(chunk.metadata["table_cell_rows_raw"], [["Item", "Standard"], ["Manager", "10"]])
        self.assertEqual(
            chunk.metadata["table_rows"],
            [
                {"row_index": 0, "cells": ["Item", "Standard"], "raw": "Item Standard"},
                {"row_index": 1, "cells": ["Manager", "10"], "raw": "Manager 10"},
            ],
        )
        self.assertEqual(chunk.metadata["table_header_cells"], [])
        self.assertEqual(chunk.metadata["table_records"], [])
        self.assertEqual(chunk.metadata["table_record_count"], 0)
        self.assertIn("| Item | Standard |", chunk.text)
        self.assertNotIn("| --- | --- |", chunk.text)
        self.assertIn("primary_parser_table_hint", chunk.metadata)
        self.assertEqual(chunk.metadata["primary_parser_table_source"], "hwp_parser")
        self.assertTrue(chunk.metadata["primary_parser_table_hint"]["table_probable_false_positive"])
        self.assertNotIn("table_probable_false_positive", chunk.metadata)
        self.assertNotIn("table_false_positive_stability", chunk.metadata)
        self.assertNotEqual(chunk.metadata.get("table_confidence"), 0.1)
        exported_rows = Exporter().table_rows([chunk])
        self.assertEqual([row["record"] for row in exported_rows], [{}, {}])
        self.assertEqual([row["header_cells"] for row in exported_rows], [[], []])
        self.assertNotEqual(chunk.metadata.get("approval_status"), "approved")

    def test_kordoc_promotion_does_not_duplicate_one_kordoc_table_across_local_chunks(self) -> None:
        chunks = [
            Chunk(
                chunk_id=f"chunk_table_{index}",
                document_id="doc_kordoc_duplicate",
                chunk_type="article",
                text="Item Standard Manager 10",
                normalized_text="Item Standard Manager 10",
                retrieval_text="Item Standard Manager 10",
                metadata={"table_like": True},
            )
            for index in range(2)
        ]
        parsed = ParsedDocument(
            document_id="doc_kordoc_duplicate",
            source_file="rules.hwp",
            document_name="Rules",
            file_type="hwp",
            metadata={
                "kordoc_table_inventory": {
                    "status": "parsed",
                    "table_count": 1,
                    "tables": [
                        {
                            "table_index": 1,
                            "row_count": 2,
                            "column_count": 2,
                            "cell_count": 4,
                            "cell_rows": [
                                {"cells": ["Item", "Standard"]},
                                {"cells": ["Manager", "10"]},
                            ],
                        }
                    ],
                }
            },
        )

        Chunker()._promote_kordoc_main_tables(chunks, parsed, ChunkOptions(include_context_header=False))

        self.assertEqual(1, sum(1 for chunk in chunks if chunk.metadata.get("kordoc_table_promoted")))
        self.assertEqual(1, sum(1 for chunk in chunks if chunk.metadata.get("table_source") == "kordoc"))

    def test_kordoc_promotion_treats_table_chunk_as_candidate_when_table_like_false(self) -> None:
        chunks = [
            Chunk(
                chunk_id="chunk_native_table",
                document_id="doc_kordoc_native_table",
                chunk_type="table",
                text="Item Standard Manager 10",
                normalized_text="Item Standard Manager 10",
                retrieval_text="Item Standard Manager 10",
                metadata={"table_like": False, "table_rows": ["OLD ROW"], "table_confidence": 0.0},
            )
        ]
        parsed = ParsedDocument(
            document_id="doc_kordoc_native_table",
            source_file="rules.hwp",
            document_name="Rules",
            file_type="hwp",
            metadata={
                "kordoc_table_inventory": {
                    "status": "parsed",
                    "table_count": 1,
                    "tables": [
                        {
                            "table_index": 1,
                            "row_count": 2,
                            "column_count": 2,
                            "cell_count": 4,
                            "cell_rows": [
                                {"cells": ["Item", "Standard"], "raw": "Item Standard"},
                                {"cells": ["Manager", "10"], "raw": "Manager 10"},
                            ],
                            "grid_has_header": True,
                        }
                    ],
                }
            },
        )

        Chunker()._promote_kordoc_main_tables(chunks, parsed, ChunkOptions(include_context_header=False))

        self.assertEqual(1, len(chunks))
        self.assertTrue(chunks[0].metadata["table_like"])
        self.assertTrue(chunks[0].metadata["kordoc_table_promoted"])
        self.assertEqual("kordoc", chunks[0].metadata["table_source"])
        self.assertEqual("kordoc", chunks[0].metadata["table_geometry_source"])
        self.assertEqual("parsed", chunks[0].metadata["kordoc_table_parser_status"])
        self.assertEqual(1, chunks[0].metadata["kordoc_table_count"])
        self.assertEqual("hwp_parser", chunks[0].metadata["primary_parser_table_source"])
        self.assertFalse(chunks[0].metadata.get("kordoc_table_unmatched_source", False))

    def test_kordoc_strong_match_can_promote_after_earlier_medium_hint(self) -> None:
        chunks = [
            Chunk(
                chunk_id="chunk_table_medium",
                document_id="doc_kordoc_strict",
                chunk_type="article",
                text="Item abc def ghi jkl mno pqr 10",
                normalized_text="Item abc def ghi jkl mno pqr 10",
                retrieval_text="Item abc def ghi jkl mno pqr 10",
                metadata={"table_like": True},
            ),
            Chunk(
                chunk_id="chunk_table_strong",
                document_id="doc_kordoc_strict",
                chunk_type="article",
                text="Item Standard Manager 10",
                normalized_text="Item Standard Manager 10",
                retrieval_text="Item Standard Manager 10",
                metadata={"table_like": True},
            ),
        ]
        parsed = ParsedDocument(
            document_id="doc_kordoc_strict",
            source_file="rules.hwp",
            document_name="Rules",
            file_type="hwp",
            metadata={
                "kordoc_table_inventory": {
                    "status": "parsed",
                    "table_count": 1,
                    "tables": [
                        {
                            "table_index": 1,
                            "row_count": 2,
                            "column_count": 2,
                            "cell_count": 4,
                            "cell_rows": [
                                {"cells": ["Item", "Standard"], "raw": "Item Standard"},
                                {"cells": ["Manager", "10"], "raw": "Manager 10"},
                            ],
                        }
                    ],
                }
            },
        )

        Chunker(Settings(kordoc_table_promote_min_match="strong_review_match"))._promote_kordoc_main_tables(
            chunks,
            parsed,
            ChunkOptions(include_context_header=False),
        )

        self.assertEqual("medium_review_match", chunks[0].metadata["kordoc_table_match"]["match_label"])
        self.assertTrue(chunks[0].metadata["kordoc_table_match_provisional"])
        self.assertFalse(chunks[0].metadata.get("kordoc_table_promoted", False))
        self.assertTrue(chunks[1].metadata["kordoc_table_promoted"])
        self.assertEqual("strong_review_match", chunks[1].metadata["kordoc_table_match"]["match_label"])
        self.assertEqual(1, sum(1 for chunk in chunks if chunk.metadata.get("table_source") == "kordoc"))

    def test_below_threshold_kordoc_match_creates_kordoc_only_main_chunk(self) -> None:
        chunks = [
            Chunk(
                chunk_id="chunk_table_medium",
                document_id="doc_kordoc_below_threshold",
                chunk_type="article",
                text="Item abc def ghi jkl mno pqr 10",
                normalized_text="Item abc def ghi jkl mno pqr 10",
                retrieval_text="Item abc def ghi jkl mno pqr 10",
                metadata={"table_like": True},
            )
        ]
        parsed = ParsedDocument(
            document_id="doc_kordoc_below_threshold",
            source_file="rules.hwp",
            document_name="Rules",
            file_type="hwp",
            metadata={
                "kordoc_table_inventory": {
                    "status": "parsed",
                    "table_count": 1,
                    "tables": [
                        {
                            "table_index": 1,
                            "row_count": 2,
                            "column_count": 2,
                            "cell_count": 4,
                            "cell_rows": [
                                {"cells": ["Item", "Standard"], "raw": "Item Standard"},
                                {"cells": ["Manager", "10"], "raw": "Manager 10"},
                            ],
                        }
                    ],
                }
            },
        )

        Chunker(Settings(kordoc_table_promote_min_match="strong_review_match"))._promote_kordoc_main_tables(
            chunks,
            parsed,
            ChunkOptions(include_context_header=False),
        )

        self.assertEqual(2, len(chunks))
        primary_chunk, kordoc_chunk = chunks
        self.assertEqual("medium_review_match", primary_chunk.metadata["kordoc_table_match"]["match_label"])
        self.assertTrue(primary_chunk.metadata["kordoc_table_match_provisional"])
        self.assertFalse(primary_chunk.metadata.get("kordoc_table_unmatched_source", False))
        self.assertFalse(primary_chunk.metadata.get("kordoc_table_promoted", False))
        self.assertEqual("kordoc", kordoc_chunk.metadata["table_source"])
        self.assertEqual("kordoc_only", kordoc_chunk.metadata["kordoc_table_match"]["match_label"])
        self.assertTrue(kordoc_chunk.metadata["kordoc_table_unmatched_source"])
        self.assertTrue(kordoc_chunk.metadata["kordoc_table_promoted"])

    def test_kordoc_promotion_records_primary_parser_source_by_file_type(self) -> None:
        for file_type in ("hwp", "hwpx", "pdf", "docx"):
            with self.subTest(file_type=file_type):
                chunk = Chunk(
                    chunk_id=f"chunk_{file_type}_table",
                    document_id=f"doc_{file_type}_promote",
                    chunk_type="article",
                    text="Item Standard Manager 10",
                    normalized_text="Item Standard Manager 10",
                    retrieval_text="Item Standard Manager 10",
                    metadata={"table_like": True, "table_rows": ["OLD ROW"]},
                )
                parsed = ParsedDocument(
                    document_id=f"doc_{file_type}_promote",
                    source_file=f"rules.{file_type}",
                    document_name="Rules",
                    file_type=file_type,
                    metadata={
                        "kordoc_table_inventory": {
                            "status": "parsed",
                            "table_count": 1,
                            "tables": [
                                {
                                    "table_index": 1,
                                    "row_count": 2,
                                    "column_count": 2,
                                    "cell_count": 4,
                                    "cell_rows": [
                                        {"cells": ["Item", "Standard"]},
                                        {"cells": ["Manager", "10"]},
                                    ],
                                }
                            ],
                        }
                    },
                )

                Chunker()._promote_kordoc_main_tables(
                    [chunk],
                    parsed,
                    ChunkOptions(include_context_header=False),
                )

                self.assertTrue(chunk.metadata["kordoc_table_promoted"])
                self.assertEqual("kordoc", chunk.metadata["table_source"])
                self.assertEqual(f"{file_type}_parser", chunk.metadata["primary_parser_table_source"])
                self.assertEqual(["OLD ROW"], chunk.metadata["primary_parser_table_hint"]["table_rows"])
                self.assertNotIn("OLD ROW", chunk.text)
                self.assertIn("| Item | Standard |", chunk.text)

    def test_kordoc_inventory_creates_draft_table_chunk_when_primary_parser_misses_table(self) -> None:
        text = "제1조(목적) 이 규정은 목적을 정한다."
        parsed = ParsedDocument(
            document_id="doc_kordoc_only",
            source_file="rules.hwp",
            document_name="Rules",
            file_type="hwp",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])],
            raw_text=text,
            metadata={
                "kordoc_table_inventory": {
                    "status": "parsed",
                    "parser": "kordoc",
                    "table_count": 1,
                    "tables": [
                        {
                            "table_index": 7,
                            "source_page": 3,
                            "title": "평가 기준",
                            "row_count": 2,
                            "column_count": 2,
                            "cell_count": 4,
                            "cell_rows": [
                                {"cells": ["항목", "기준"]},
                                {"cells": ["교육", "80점"]},
                            ],
                            "grid_has_header": True,
                        }
                    ],
                }
            },
        )
        nodes = StructureDetector().detect(parsed)

        chunks = Chunker(Settings(kordoc_table_as_main=True)).build_chunks(
            nodes,
            parsed,
            ChunkOptions(include_context_header=False),
        )

        kordoc_chunk = next(chunk for chunk in chunks if chunk.metadata.get("kordoc_table_unmatched_source"))
        self.assertEqual(kordoc_chunk.chunk_type, "table")
        self.assertEqual(kordoc_chunk.approval_status, "draft")
        self.assertEqual(kordoc_chunk.metadata["table_source"], "kordoc")
        self.assertTrue(kordoc_chunk.metadata["table_review_required"])
        self.assertTrue(kordoc_chunk.metadata["kordoc_table_promoted"])
        self.assertIn("| 항목 | 기준 |", kordoc_chunk.text)
        self.assertEqual(kordoc_chunk.source_page_start, 3)

    def test_kordoc_only_table_without_page_records_declared_source_page_gap(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_kordoc_no_page",
            source_file="rules.hwp",
            document_name="Rules",
            file_type="hwp",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text="Article text.")])],
            raw_text="Article text.",
            metadata={
                "kordoc_table_inventory": {
                    "status": "parsed",
                    "parser": "kordoc",
                    "table_count": 1,
                    "tables": [
                        {
                            "table_index": 7,
                            "row_count": 2,
                            "column_count": 2,
                            "cell_count": 4,
                            "cell_rows": [
                                {"cells": ["Item", "Standard"]},
                                {"cells": ["Training", "80"]},
                            ],
                            "grid_has_header": True,
                        }
                    ],
                }
            },
        )

        chunks = Chunker(Settings(kordoc_table_as_main=True)).build_chunks(
            StructureDetector().detect(parsed),
            parsed,
            ChunkOptions(include_context_header=False),
        )

        kordoc_chunk = next(chunk for chunk in chunks if chunk.metadata.get("kordoc_table_unmatched_source"))
        self.assertIsNone(kordoc_chunk.source_page_start)
        self.assertEqual(
            "kordoc_table_source_page_missing",
            kordoc_chunk.metadata["source_page_unavailable_reason"],
        )
        self.assertEqual("kordoc", kordoc_chunk.metadata["source_page_unavailable_parser"])

    def test_kordoc_inventory_creates_draft_table_chunk_for_docx_when_primary_parser_misses_table(self) -> None:
        text = "Article text without primary parser table chunks."
        parsed = ParsedDocument(
            document_id="doc_docx_kordoc_only",
            source_file="rules.docx",
            document_name="Rules",
            file_type="docx",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])],
            raw_text=text,
            metadata={
                "kordoc_table_inventory": {
                    "status": "parsed",
                    "parser": "kordoc",
                    "table_count": 1,
                    "tables": [
                        {
                            "table_index": 3,
                            "source_page": 2,
                            "title": "Approval standards",
                            "row_count": 2,
                            "column_count": 2,
                            "cell_count": 4,
                            "cell_rows": [
                                {"cells": ["Item", "Standard"]},
                                {"cells": ["Manager", "10"]},
                            ],
                            "grid_has_header": True,
                        }
                    ],
                }
            },
        )
        nodes = StructureDetector().detect(parsed)

        chunks = Chunker(Settings(kordoc_table_as_main=True)).build_chunks(
            nodes,
            parsed,
            ChunkOptions(include_context_header=False),
        )

        kordoc_chunk = next(chunk for chunk in chunks if chunk.metadata.get("kordoc_table_unmatched_source"))
        self.assertEqual(kordoc_chunk.chunk_type, "table")
        self.assertEqual(kordoc_chunk.metadata["table_source"], "kordoc")
        self.assertTrue(kordoc_chunk.metadata["kordoc_table_promoted"])
        self.assertEqual("parsed", kordoc_chunk.metadata["kordoc_table_parser_status"])
        self.assertEqual(1, kordoc_chunk.metadata["kordoc_table_count"])
        self.assertIn("| Item | Standard |", kordoc_chunk.text)
        self.assertEqual(kordoc_chunk.source_page_start, 2)

    def test_kordoc_only_table_chunks_are_created_for_pdf(self) -> None:
        text = "제1조(목적) 이 규정은 목적을 정한다."
        parsed = ParsedDocument(
            document_id="doc_pdf_kordoc_only",
            source_file="rules.pdf",
            document_name="Rules",
            file_type="pdf",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])],
            raw_text=text,
            metadata={
                "kordoc_table_inventory": {
                    "status": "parsed",
                    "parser": "kordoc",
                    "table_count": 1,
                    "tables": [
                        {
                            "table_index": 7,
                            "source_page": 3,
                            "title": "평가 기준",
                            "row_count": 2,
                            "column_count": 2,
                            "cell_count": 4,
                            "cell_rows": [
                                {"cells": ["항목", "기준"]},
                                {"cells": ["교육", "80점"]},
                            ],
                            "grid_has_header": True,
                        }
                    ],
                }
            },
        )
        nodes = StructureDetector().detect(parsed)

        chunks = Chunker(Settings(kordoc_table_as_main=True)).build_chunks(
            nodes,
            parsed,
            ChunkOptions(include_context_header=False),
        )

        kordoc_chunk = next(chunk for chunk in chunks if chunk.metadata.get("kordoc_table_unmatched_source"))
        self.assertEqual(kordoc_chunk.chunk_type, "table")
        self.assertEqual(kordoc_chunk.metadata["table_source"], "kordoc")
        self.assertTrue(kordoc_chunk.metadata["kordoc_table_promoted"])
        self.assertEqual("parsed", kordoc_chunk.metadata["kordoc_table_parser_status"])
        self.assertEqual(1, kordoc_chunk.metadata["kordoc_table_count"])
        self.assertTrue(kordoc_chunk.metadata["kordoc_table_promotion_review_required"])
        self.assertEqual(kordoc_chunk.source_page_start, 3)

    def test_builds_article_chunks_with_metadata(self) -> None:
        parsed = parsed_fixture()
        nodes = StructureDetector().detect(parsed)
        chunks = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=True))

        first = next(chunk for chunk in chunks if chunk.metadata.get("article_no") == "제1조")

        self.assertEqual(first.chunk_type, "article")
        self.assertIn("[위치]", first.text)
        self.assertIn("복무규정 > 제1장 총칙 > 제1절 목적 > 제1조 목적", first.metadata["hierarchy_path"])
        self.assertEqual(first.metadata["source_file"], "sample_regulation.md")
        self.assertEqual(first.metadata["article_title"], "목적")
        self.assertIn("[문서명] 복무규정", first.retrieval_text)

    def test_article_chunk_includes_child_paragraph_items(self) -> None:
        parsed = parsed_fixture()
        nodes = StructureDetector().detect(parsed)
        chunks = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=False))
        definitions = next(chunk for chunk in chunks if chunk.metadata.get("article_no") == "제2조")

        self.assertIn("① 이 규정에서 사용하는 용어", definitions.text)
        self.assertIn('"임직원"', definitions.text)
        self.assertIn("가. 정규직 직원을 포함한다.", definitions.text)

    def test_article_chunk_records_structural_child_unit_counts(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_structural_counts",
            source_file="rules.pdf",
            document_name="Rules",
            file_type="pdf",
        )
        nodes = [
            StructureNode(
                node_id="article_1",
                document_id=parsed.document_id,
                node_type="article",
                number="Article 1",
                title="Purpose",
                text="Article body",
                page_start=1,
                page_end=1,
                order_index=1,
            ),
            StructureNode(
                node_id="paragraph_1",
                document_id=parsed.document_id,
                node_type="paragraph",
                number="1",
                title="",
                text="Paragraph body",
                page_start=1,
                page_end=1,
                parent_id="article_1",
                order_index=2,
            ),
            StructureNode(
                node_id="item_1",
                document_id=parsed.document_id,
                node_type="item",
                number="1.",
                title="",
                text="Item body",
                page_start=1,
                page_end=1,
                parent_id="paragraph_1",
                order_index=3,
            ),
            StructureNode(
                node_id="subitem_1",
                document_id=parsed.document_id,
                node_type="subitem",
                number="a.",
                title="",
                text="Subitem body",
                page_start=1,
                page_end=1,
                parent_id="item_1",
                order_index=4,
            ),
        ]

        chunks = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=False))
        article = next(chunk for chunk in chunks if chunk.chunk_type == "article")

        self.assertEqual("structure_detector", article.metadata["structural_child_count_source"])
        self.assertEqual(1, article.metadata["paragraph_unit_count"])
        self.assertEqual(1, article.metadata["item_unit_count"])
        self.assertEqual(1, article.metadata["subitem_unit_count"])
        self.assertEqual(3, article.metadata["paragraph_item_unit_count"])
        self.assertEqual(3, article.metadata["paragraph_item_traceable_unit_count"])
        self.assertEqual(["paragraph_1", "item_1", "subitem_1"], article.metadata["paragraph_item_unit_ids"])
        self.assertEqual("paragraph_1", article.metadata["paragraph_item_unit_sample"][0]["node_id"])
        self.assertEqual("Paragraph body", article.metadata["paragraph_item_unit_sample"][0]["text_preview"])

    def test_article_structural_counts_exclude_table_descendants(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_table_child_counts",
            source_file="rules.pdf",
            document_name="Rules",
            file_type="pdf",
        )
        nodes = [
            StructureNode(
                node_id="article_1",
                document_id=parsed.document_id,
                node_type="article",
                number="Article 1",
                text="Article body",
                order_index=1,
            ),
            StructureNode(
                node_id="paragraph_1",
                document_id=parsed.document_id,
                node_type="paragraph",
                number="1",
                text="Body paragraph",
                parent_id="article_1",
                order_index=2,
            ),
            StructureNode(
                node_id="table_1",
                document_id=parsed.document_id,
                node_type="table",
                text="Table body",
                parent_id="article_1",
                order_index=3,
            ),
            StructureNode(
                node_id="table_item_1",
                document_id=parsed.document_id,
                node_type="item",
                number="1.",
                text="Table row marker",
                parent_id="table_1",
                order_index=4,
            ),
        ]

        chunks = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=False))
        article = next(chunk for chunk in chunks if chunk.chunk_type == "article")

        self.assertEqual(1, article.metadata["paragraph_item_unit_count"])
        self.assertEqual(["paragraph_1"], article.metadata["paragraph_item_unit_ids"])

    def test_supplementary_article_does_not_emit_main_body_structural_counts(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_supplementary_article_counts",
            source_file="rules.pdf",
            document_name="Rules",
            file_type="pdf",
        )
        nodes = [
            StructureNode(
                node_id="supplementary_1",
                document_id=parsed.document_id,
                node_type="supplementary",
                number="Addenda",
                text="Addenda",
                order_index=1,
            ),
            StructureNode(
                node_id="article_1",
                document_id=parsed.document_id,
                node_type="article",
                number="Article 1",
                text="Quoted amendment article",
                parent_id="supplementary_1",
                order_index=2,
            ),
            StructureNode(
                node_id="paragraph_1",
                document_id=parsed.document_id,
                node_type="paragraph",
                number="1",
                text="Quoted amendment paragraph",
                parent_id="article_1",
                order_index=3,
            ),
        ]

        chunks = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=False))
        article = next(chunk for chunk in chunks if chunk.chunk_type == "article")

        self.assertNotIn("paragraph_item_unit_count", article.metadata)
        self.assertNotIn("paragraph_item_unit_ids", article.metadata)

    def test_clause_after_form_is_chunked_under_governing_article(self) -> None:
        text = "\n".join(
            [
                "제31조(휴직의 운영) 휴직 운영 기준을 정한다.",
                "【별지 제3호 서식】",
                "근무상황부",
                "④ 휴직자는 별지 제15호서식에 따른 휴직자 복무상황 보고서를 제출해야 한다.",
            ]
        )
        parsed = ParsedDocument(
            document_id="doc_form_boundary",
            source_file="form_boundary.md",
            document_name="복무규정",
            file_type="text",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])],
            raw_text=text,
        )

        nodes = StructureDetector().detect(parsed)
        chunks = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=False))
        article_chunk = next(chunk for chunk in chunks if chunk.metadata.get("article_no") == "제31조")
        form_chunk = next(chunk for chunk in chunks if chunk.chunk_type == "form")

        self.assertIn("④ 휴직자는", article_chunk.text)
        self.assertNotIn("④ 휴직자는", form_chunk.text)
        self.assertIn("별지제15호서식", article_chunk.metadata["form_refs"])
        self.assertEqual(article_chunk.metadata["article_title"], "휴직의 운영")
        self.assertIn("attachment_container_boundary_inferred", article_chunk.warnings)

    def test_builds_answer_profile_for_mcp_retrieval(self) -> None:
        text = (
            "제8조(신규임용 후보자 심사) 임용분야, 임용인원, 지원자격을 확인하고 "
            "신규임용 후보자에 대하여 단계별로 다음 각 호의 사항을 심사한다. "
            "1. 기초심사 2. 연구실적심사 3. 공개발표심사 4. 면접심사"
        )
        parsed = ParsedDocument(
            document_id="doc_answer_profile",
            source_file="answer.md",
            document_name="교원 임용 세칙",
            file_type="text",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])],
            raw_text=text,
        )
        node = StructureNode(
            node_id="node_article",
            document_id=parsed.document_id,
            node_type="article",
            number="제8조",
            title="신규임용 후보자 심사",
            text=text,
            page_start=1,
            page_end=1,
            order_index=0,
        )

        chunk = Chunker().build_chunks([node], parsed, ChunkOptions(include_context_header=False))[0]

        self.assertIn("procedure", chunk.metadata["answer_intents"])
        self.assertNotIn("payment", chunk.metadata["answer_intents"])
        self.assertTrue(any("심사" in keyword for keyword in chunk.metadata["answer_keywords"]))
        self.assertTrue(
            any(fact["type"] == "procedure_step" and "기초심사" in fact["value"] for fact in chunk.metadata["answer_facts"])
        )
        self.assertIn(ANSWER_PROFILE_MARKER, chunk.retrieval_text)
        self.assertIn("의도: procedure", chunk.retrieval_text)

    def test_answer_profile_normalizes_common_hwp_spacing_artifacts(self) -> None:
        profile = build_answer_profile(
            "③원장은 지원 마감일 전까지 15일이상 지원자격 등 에 관한 사항을 효과적인 방법 으로 공고한다. "
            "신규임용은 3년이내에 하는 것을 원칙으 로 한다.<2011.11.10.>"
        )

        rendered = "\n".join(
            [
                *(profile.get("answer_outline") or []),
                *(fact["sentence"] for fact in profile.get("answer_facts") or []),
            ]
        )
        self.assertIn("③ 원장은", rendered)
        self.assertIn("15일 이상", rendered)
        self.assertIn("등에", rendered)
        self.assertIn("방법으로", rendered)
        self.assertIn("원칙으로", rendered)
        self.assertNotIn("③원장은", rendered)
        self.assertNotIn("15일이상", rendered)
        self.assertNotIn("등 에", rendered)
        self.assertNotIn("방법 으로", rendered)
        self.assertNotIn("원칙으 로", rendered)
        self.assertNotIn("<2011", rendered)

    def test_chunk_metadata_keeps_hwpx_caption_and_note_sources(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_hwpx_sources",
            source_file="sample.hwpx",
            document_name="HWPX규정",
            file_type="hwpx",
            pages=[
                ParsedPage(
                    page_no=1,
                    blocks=[
                        ParsedBlock(
                            text="제1조(목적) 본문",
                            metadata={"hwpx_block_type": "paragraph", "xml_file": "Contents/section0.xml"},
                        ),
                        ParsedBlock(
                            text="각주 설명",
                            metadata={"hwpx_block_type": "footnote", "xml_file": "Contents/section0.xml"},
                        ),
                        ParsedBlock(
                            type="image",
                            text="그림 1. 처리 흐름",
                            metadata={
                                "hwpx_block_type": "image",
                                "caption_count": 1,
                                "xml_file": "Contents/section0.xml",
                            },
                        ),
                    ],
                )
            ],
            raw_text="제1조(목적) 본문\n각주 설명\n그림 1. 처리 흐름",
        )
        nodes = StructureDetector().detect(parsed)

        chunk = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=False))[0]

        self.assertEqual(chunk.metadata["source_hwpx_block_types"], ["paragraph", "footnote", "image"])
        self.assertEqual(chunk.metadata["source_hwpx_block_type_count"], 3)
        self.assertEqual(chunk.metadata["source_xml_files"], ["Contents/section0.xml"])
        self.assertEqual(chunk.metadata["source_caption_count"], 1)

    def test_chunk_metadata_keeps_hwpx_complex_table_sources(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_hwpx_complex",
            source_file="complex.hwpx",
            document_name="HWPX Complex",
            file_type="hwpx",
            pages=[
                ParsedPage(
                    page_no=1,
                    blocks=[
                        ParsedBlock(
                            type="table",
                            text="Outer A | Nested A Nested B Figure Caption Cell Note\nOuter B",
                            metadata={
                                "hwpx_block_type": "table",
                                "xml_file": "Contents/section0.xml",
                                "hwpx_table_row_count": 2,
                                "hwpx_table_cell_count": 3,
                                "hwpx_table_caption_count": 1,
                                "hwpx_nested_table_count": 1,
                                "hwpx_table_image_count": 1,
                                "hwpx_table_note_count": 1,
                                "hwpx_merged_cell_count": 2,
                                "hwpx_parser_review_flags": [
                                    "table_caption",
                                    "nested_table",
                                    "table_image",
                                    "table_note",
                                    "merged_cell",
                                ],
                            },
                        )
                    ],
                )
            ],
            raw_text="Outer A | Nested A Nested B Figure Caption Cell Note\nOuter B",
        )
        nodes = StructureDetector().detect(parsed)

        chunk = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=False))[0]

        self.assertEqual(chunk.metadata["source_hwpx_block_types"], ["table"])
        self.assertEqual(
            chunk.metadata["source_hwpx_parser_review_flags"],
            ["table_caption", "nested_table", "table_image", "table_note", "merged_cell"],
        )
        self.assertEqual(chunk.metadata["source_hwpx_table_row_count"], 2)
        self.assertEqual(chunk.metadata["source_hwpx_table_cell_count"], 3)
        self.assertEqual(chunk.metadata["source_hwpx_table_caption_count"], 1)
        self.assertEqual(chunk.metadata["source_hwpx_nested_table_count"], 1)
        self.assertEqual(chunk.metadata["source_hwpx_table_image_count"], 1)
        self.assertEqual(chunk.metadata["source_hwpx_table_note_count"], 1)
        self.assertEqual(chunk.metadata["source_hwpx_merged_cell_count"], 2)

    def test_chunk_metadata_keeps_hwp_extraction_mode_sources(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_hwp_source",
            source_file="source.hwp",
            document_name="HWP Source",
            file_type="hwp",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text="Form text")])],
            raw_text="Form text",
        )
        nodes = [
            StructureNode(
                node_id="doc_hwp_source_form_0001",
                document_id="doc_hwp_source",
                node_type="form",
                number="form-1",
                title="Legacy HWP Form",
                text="Form text",
                page_start=1,
                page_end=1,
                order_index=0,
                metadata={
                    "source_hwp_extraction_modes": ["legacy_ole_para_text_only"],
                    "source_hwp_streams": ["BodyText/Section0"],
                    "source_hwp_section_indices": [1],
                    "source_hwp_native_table_geometry": False,
                },
            )
        ]

        chunk = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=False))[0]

        self.assertEqual(chunk.metadata["source_hwp_extraction_modes"], ["legacy_ole_para_text_only"])
        self.assertEqual(chunk.metadata["source_hwp_streams"], ["BodyText/Section0"])
        self.assertEqual(chunk.metadata["source_hwp_section_indices"], [1])
        self.assertFalse(chunk.metadata["source_hwp_native_table_geometry"])

    def test_long_article_splits_on_paragraph_boundary(self) -> None:
        text = "제1조(긴 조문)\n" + "\n".join(
            [
                f"① {'가' * 80}",
                f"② {'나' * 80}",
                f"③ {'다' * 80}",
            ]
        )
        parsed = ParsedDocument(
            document_id="doc_long",
            source_file="long.md",
            document_name="긴규정",
            file_type="text",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])],
            raw_text=text,
        )
        nodes = StructureDetector().detect(parsed)
        chunks = Chunker().build_chunks(nodes, parsed, ChunkOptions(max_chunk_chars=120, include_context_header=False))

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.metadata["article_no"] == "제1조" for chunk in chunks))

    def test_long_article_fallback_splits_without_regex_error(self) -> None:
        text = "제1조(긴 조문) " + " ".join(
            [
                "이 문장은 fallback 분할 경로를 검증한다.",
                "조문 내부에 항 번호가 없어도 처리되어야 한다.",
                "정규식 lookbehind 오류 없이 chunk를 생성해야 한다.",
            ]
            * 20
        )
        parsed = ParsedDocument(
            document_id="doc_sentence",
            source_file="sentence.md",
            document_name="문장규정",
            file_type="text",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)]),],
            raw_text=text,
        )
        nodes = StructureDetector().detect(parsed)
        chunks = Chunker().build_chunks(nodes, parsed, ChunkOptions(max_chunk_chars=180, overlap_chars=20, include_context_header=False))

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk.text) <= 220 for chunk in chunks))

    def test_table_like_appendix_gets_table_metadata(self) -> None:
        text = "\n".join(
            [
                "[별표 1]",
                "재산명 수량 평가액(원) 비고",
                "토지 181,425.08 2,646,882,423",
                "건물 65,386.94 93,368,436,537",
                "총계 96,015,318,960",
            ]
        )
        parsed = ParsedDocument(
            document_id="doc_table",
            source_file="table.md",
            document_name="표규정",
            file_type="text",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])],
            raw_text=text,
        )
        nodes = StructureDetector().detect(parsed)
        chunks = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=False))

        self.assertEqual(chunks[0].chunk_type, "appendix")
        self.assertTrue(chunks[0].metadata["table_like"])
        self.assertIn("table_markdown", chunks[0].metadata)
        self.assertEqual(chunks[0].metadata["chunker_version"], CHUNKER_VERSION)
        self.assertTrue(chunks[0].metadata["table_records"])
        self.assertEqual(chunks[0].metadata["table_records"][0]["record"]["재산명"], "토지")
        self.assertIn("[표]", chunks[0].retrieval_text)
        self.assertIn("| 재산명 | 수량 | 평가액(원) | 비고 |", chunks[0].retrieval_text)

    def test_split_appendix_table_inherits_label_from_hierarchy(self) -> None:
        text = "\n".join(
            [
                "구분 평가항목 적용기준 상한점수 비고",
                "기본 직무 활동 책임 직무 60 40 100",
                "연구 교육 활동 연구 과제 20 10 30",
                "봉사 활동 위원회 활동 10 5 15",
            ]
            * 8
        )
        parsed = ParsedDocument(
            document_id="doc_split_table",
            source_file="split_table.md",
            document_name="연구직 직무수행 평가규정",
            file_type="text",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])],
            raw_text=text,
        )
        node = StructureNode(
            node_id="appendix-1",
            document_id="doc_split_table",
            node_type="appendix",
            number="별표1",
            title="평가 항목별 배점표 <2022.12.14.>",
            text=text,
            page_start=1,
            page_end=2,
            order_index=1,
        )

        chunks = Chunker().build_chunks(
            [node],
            parsed,
            ChunkOptions(max_chunk_chars=220, overlap_chars=0, include_context_header=False),
        )
        table_chunks = [chunk for chunk in chunks if chunk.metadata.get("table_like")]

        self.assertGreater(len(table_chunks), 1)
        for chunk in table_chunks:
            self.assertEqual("별표1", chunk.metadata["table_appendix_no"])
            self.assertEqual("별표1 평가 항목별 배점표", chunk.metadata["table_citation_label"])
            self.assertTrue(chunk.metadata["table_label_inferred_from_hierarchy"])

    def test_demoted_table_false_positive_keeps_review_metadata(self) -> None:
        text = "\n".join(
            [
                "[별표 1]",
                "개정 2010. 1. 29. 규정 제780호",
                "개정 2015. 1. 5. 규정 제907호",
                "개정 2024. 12. 27. 규정 제1233호",
                "제1조(목적) 이 규정은 이사회의 운영을 정한다.",
                "제2조(적용) 이 규정은 법령에 따라 적용한다.",
            ]
        )
        parsed = ParsedDocument(
            document_id="doc_false_table",
            source_file="false_table.md",
            document_name="샘플",
            file_type="text",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])],
            raw_text=text,
        )
        nodes = StructureDetector().detect(parsed)
        chunks = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=False))

        self.assertFalse(chunks[0].metadata["table_like"])
        self.assertTrue(chunks[0].metadata["table_probable_false_positive"])
        self.assertEqual(chunks[0].metadata["table_classification"], "probable_false_positive_prose_revision")
        self.assertEqual(chunks[0].metadata["table_false_positive_stability"], "stable")
        self.assertIn("table_header_hits", chunks[0].metadata)
        self.assertIn("table_numeric_rows", chunks[0].metadata)
        self.assertIn("table_delimiter_rows", chunks[0].metadata)

    def test_hard_windows_do_not_skip_tokens(self) -> None:
        tokens = [f"tok{i:04d}" for i in range(300)]
        text = " ".join(tokens)

        chunks = Chunker()._hard_windows(text, max_chars=120, overlap_chars=25)
        combined = " ".join(chunks)

        self.assertGreater(len(chunks), 1)
        for token in tokens:
            self.assertIn(token, combined)

    def test_hard_windows_stop_after_final_tail(self) -> None:
        text = "x" * 250

        chunks = Chunker()._hard_windows(text, max_chars=100, overlap_chars=40)

        self.assertEqual([len(chunk) for chunk in chunks], [100, 100, 100, 70])

    def test_chunk_ids_are_unique_when_article_numbers_repeat(self) -> None:
        text = "\n".join(
            [
                "제1장 첫번째",
                "제1조(목적) 첫 번째 목적",
                "제2장 두번째",
                "제1조(목적) 두 번째 목적",
            ]
        )
        parsed = ParsedDocument(
            document_id="doc_repeat",
            source_file="repeat.md",
            document_name="반복규정",
            file_type="text",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])],
            raw_text=text,
        )
        nodes = StructureDetector().detect(parsed)
        chunks = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=False))
        chunk_ids = [chunk.chunk_id for chunk in chunks]

        self.assertEqual(len(chunk_ids), len(set(chunk_ids)))

    def test_uses_regulation_metadata_from_detected_hierarchy(self) -> None:
        text = "\n".join(
            [
                "1-2-1. 한국학중앙연구원정관",
                "제1장 총칙",
                "제1조(목적) 본문",
            ]
        )
        parsed = ParsedDocument(
            document_id="doc_infer_reg",
            source_file="infer.md",
            document_name="통합규정",
            file_type="text",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])],
            raw_text=text,
        )
        nodes = StructureDetector().detect(parsed)
        chunk = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=False))[0]

        self.assertEqual(chunk.metadata["regulation_no"], "1-2-1")
        self.assertEqual(chunk.metadata["regulation_title"], "한국학중앙연구원정관")
        self.assertNotIn("regulation_inferred_from_order", chunk.metadata)

    def test_integrated_book_hierarchy_path_includes_regulation_and_internal_chapter(self) -> None:
        text = "\n".join(
            [
                "제1편 기본법령 및 법인일반",
                "제2장 법인 및 조직",
                "1-2-1. 한국학중앙연구원정관",
                "제1장 총칙",
                "제1조(목적) 본문",
            ]
        )
        parsed = ParsedDocument(
            document_id="doc_integrated_hierarchy",
            source_file="integrated.pdf",
            document_name="규정집통합본",
            file_type="pdf",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])],
            raw_text=text,
        )
        nodes = StructureDetector().detect(parsed)
        chunk = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=False))[0]

        self.assertEqual(chunk.metadata["part_no"], "제1편")
        self.assertEqual(chunk.metadata["chapter_no"], "제1장")
        self.assertEqual(chunk.metadata["regulation_no"], "1-2-1")
        self.assertIn("제2장 법인 및 조직 > 1-2-1 한국학중앙연구원정관 > 제1장 총칙", chunk.metadata["hierarchy_path"])
        self.assertNotIn("regulation_inferred_from_order", chunk.metadata)

    def test_infers_regulation_metadata_from_nearest_preceding_regulation_for_legacy_nodes(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_infer_reg",
            source_file="infer.md",
            document_name="통합규정",
            file_type="text",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text="1-2-1. 한국학중앙연구원정관\n제1조(목적) 본문")])],
            raw_text="1-2-1. 한국학중앙연구원정관\n제1조(목적) 본문",
        )
        regulation = StructureNode(
            node_id="node_reg",
            document_id=parsed.document_id,
            node_type="regulation",
            number="1-2-1",
            title="한국학중앙연구원정관",
            text="1-2-1. 한국학중앙연구원정관",
            page_start=1,
            page_end=1,
            order_index=0,
        )
        article = StructureNode(
            node_id="node_article",
            document_id=parsed.document_id,
            node_type="article",
            number="제1조",
            title="목적",
            text="제1조(목적) 본문",
            page_start=1,
            page_end=1,
            order_index=1,
        )
        chunk = Chunker().build_chunks([regulation, article], parsed, ChunkOptions(include_context_header=False))[0]

        self.assertEqual(chunk.metadata["regulation_no"], "1-2-1")
        self.assertEqual(chunk.metadata["regulation_title"], "한국학중앙연구원정관")
        self.assertTrue(chunk.metadata["regulation_inferred_from_order"])
        self.assertEqual(chunk.metadata["regulation_source_node_id"], regulation.node_id)

    def test_resolves_internal_regulation_and_article_references(self) -> None:
        text = "\n".join(
            [
                "1-2-1. 인사규정",
                "제1조(목적) 이 규정은 인사 운영을 정하며 제2조에 따른다.",
                "제2조(정의) 직원이란 공사에 근무하는 사람을 말한다.",
                "1-2-2. 보수규정",
                "제1조(목적) 이 규정은 「인사규정」 및 1-2-1. 인사규정을 준용한다.",
            ]
        )
        parsed = ParsedDocument(
            document_id="doc_refs",
            source_file="refs.md",
            document_name="통합규정집",
            file_type="text",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])],
            raw_text=text,
        )

        chunks = Chunker().build_chunks(
            StructureDetector().detect(parsed),
            parsed,
            ChunkOptions(include_context_header=False),
        )

        first_article = next(
            chunk
            for chunk in chunks
            if chunk.metadata.get("regulation_no") == "1-2-1" and chunk.metadata.get("article_no") == "제1조"
        )
        second_article = next(
            chunk
            for chunk in chunks
            if chunk.metadata.get("regulation_no") == "1-2-1" and chunk.metadata.get("article_no") == "제2조"
        )
        pay_article = next(chunk for chunk in chunks if chunk.metadata.get("regulation_no") == "1-2-2")

        article_edges = [edge for edge in first_article.metadata["reference_edges"] if edge["type"] == "article"]
        regulation_edges = [edge for edge in pay_article.metadata["reference_edges"] if edge["type"] == "regulation"]

        self.assertEqual(article_edges[0]["target_chunk_id"], second_article.chunk_id)
        self.assertTrue(regulation_edges)
        self.assertTrue(all(edge["resolved"] for edge in regulation_edges))
        self.assertTrue(any(edge["target_regulation_no"] == "1-2-1" for edge in regulation_edges))
        self.assertIn(REFERENCE_SECTION_MARKER, pay_article.retrieval_text)
        self.assertGreaterEqual(pay_article.metadata["resolved_reference_count"], 1)

    def test_document_title_inference_skips_hwp_mojibake_title_line(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_mojibake_title",
            source_file="mojibake.hwp",
            document_name="공기업 지침",
            file_type="hwp",
            pages=[
                ParsedPage(page_no=1, blocks=[ParsedBlock(text="捤獥 汤捯 氠瑢")]),
                ParsedPage(page_no=1, blocks=[ParsedBlock(text="공기업 지침")]),
            ],
            raw_text="捤獥 汤捯 氠瑢\n공기업 지침",
        )
        node = StructureNode(
            node_id="node_article",
            document_id=parsed.document_id,
            node_type="article",
            number="제1조",
            title="목적",
            text="제1조(목적) 본문",
            page_start=1,
            page_end=1,
            order_index=0,
        )

        chunk = Chunker().build_chunks([node], parsed, ChunkOptions(include_context_header=False))[0]

        self.assertEqual(chunk.metadata["regulation_no"], "공기업 지침")
        self.assertEqual(chunk.metadata["regulation_title"], "공기업 지침")

    def test_document_title_inference_strips_revision_suffix_from_ocr_title_line(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_ocr_title",
            source_file="5449_236510_수의계약 집행기준(2026년도 06월 01일 개정).pdf",
            document_name="5449_236510_수의계약 집행기준(2026년도 06월 01일 개정)",
            file_type="pdf",
            pages=[
                ParsedPage(
                    page_no=1,
                    blocks=[
                        ParsedBlock(
                            text="수의 계약 집행기준 (제정 2011.10.31.>, 〈최종개정일 2026 06.01.〉"
                        )
                    ],
                )
            ],
            raw_text="수의 계약 집행기준 (제정 2011.10.31.>, 〈최종개정일 2026 06.01.〉",
        )
        node = StructureNode(
            node_id="node_article",
            document_id=parsed.document_id,
            node_type="article",
            number="제1조",
            title="목적",
            text="제1조(목적) 본문",
            page_start=1,
            page_end=1,
            order_index=0,
        )

        chunk = Chunker().build_chunks([node], parsed, ChunkOptions(include_context_header=False))[0]

        self.assertEqual(chunk.metadata["regulation_no"], "수의 계약 집행기준")
        self.assertEqual(chunk.metadata["regulation_title"], "수의 계약 집행기준")

    def test_top_level_paragraph_nodes_become_chunks(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_paragraph",
            source_file="paragraph.hwp",
            document_name="Paragraph Guideline",
            file_type="hwp",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text="Paragraph body")])],
            raw_text="Paragraph body",
        )
        node = StructureNode(
            node_id="node_para",
            document_id=parsed.document_id,
            node_type="paragraph",
            number="(1)",
            title=None,
            text="Paragraph body",
            page_start=1,
            page_end=1,
            order_index=0,
        )

        chunk = Chunker().build_chunks([node], parsed, ChunkOptions(include_context_header=False))[0]

        self.assertEqual(chunk.chunk_type, "paragraph")
        self.assertEqual(chunk.metadata["paragraph_no"], "(1)")

    def test_fallback_document_chunk_when_structure_has_no_nodes(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_fallback",
            source_file="fallback.hwp",
            document_name="Fallback Guideline",
            file_type="hwp",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text="Unstructured guideline body")])],
            raw_text="Unstructured guideline body",
        )

        chunk = Chunker().build_chunks([], parsed, ChunkOptions(include_context_header=False))[0]

        self.assertEqual(chunk.chunk_type, "document")
        self.assertTrue(chunk.metadata["structure_fallback"])
        self.assertEqual(chunk.metadata["hierarchy_path"], "Fallback Guideline")
        self.assertIn("structure_fallback_document_chunk", chunk.warnings)

    def test_root_item_with_subitems_becomes_recoverable_chunk(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_root_item",
            source_file="items.hwp",
            document_name="Item Guideline",
            file_type="hwp",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text="1. Root item\na. Detail")])],
            raw_text="1. Root item\na. Detail",
        )
        item = StructureNode(
            node_id="node_item",
            document_id=parsed.document_id,
            node_type="item",
            number="1.",
            title=None,
            text="1. Root item",
            page_start=1,
            page_end=1,
            order_index=0,
        )
        subitem = StructureNode(
            node_id="node_subitem",
            document_id=parsed.document_id,
            node_type="subitem",
            number="a.",
            title=None,
            text="a. Detail",
            page_start=1,
            page_end=1,
            parent_id=item.node_id,
            order_index=1,
        )

        chunk = Chunker().build_chunks([item, subitem], parsed, ChunkOptions(include_context_header=False))[0]

        self.assertEqual(chunk.chunk_type, "item")
        self.assertEqual(chunk.source_node_ids, ["node_item", "node_subitem"])
        self.assertIn("Root item", chunk.text)
        self.assertIn("Detail", chunk.text)
        self.assertEqual(chunk.metadata["item_no"], "1.")

    def test_container_orphan_body_becomes_regulation_chunk_without_children(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_orphan_body",
            source_file="orphan.hwp",
            document_name="Orphan Guideline",
            file_type="hwp",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text="1-2-1. Regulation\nIntro\nArticle text")])],
            raw_text="1-2-1. Regulation\nIntro\nArticle text",
        )
        regulation = StructureNode(
            node_id="node_reg",
            document_id=parsed.document_id,
            node_type="regulation",
            number="1-2-1",
            title="Regulation",
            text="1-2-1. Regulation\nIntroductory orphan body",
            page_start=1,
            page_end=1,
            order_index=0,
        )
        article = StructureNode(
            node_id="node_article",
            document_id=parsed.document_id,
            node_type="article",
            number="Article1",
            title="Purpose",
            text="Article text",
            page_start=1,
            page_end=1,
            parent_id=regulation.node_id,
            order_index=1,
        )

        chunks = Chunker().build_chunks([regulation, article], parsed, ChunkOptions(include_context_header=False))
        regulation_chunk = next(chunk for chunk in chunks if chunk.chunk_type == "regulation")

        self.assertEqual(regulation_chunk.source_node_ids, ["node_reg"])
        self.assertIn("Introductory orphan body", regulation_chunk.text)
        self.assertNotIn("Article text", regulation_chunk.text)

    def test_regulation_level_subitems_become_chunks(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_reg_subitems",
            source_file="reg-subitems.hwp",
            document_name="Regulation Subitems",
            file_type="hwp",
            pages=[
                ParsedPage(
                    page_no=1,
                    blocks=[ParsedBlock(text="1-2-1. Regulation\n\uac00. first\n\ub098. second")],
                )
            ],
            raw_text="1-2-1. Regulation\n\uac00. first\n\ub098. second",
        )

        nodes = StructureDetector().detect(parsed)
        chunks = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=False))

        self.assertEqual(2, len(chunks))
        self.assertEqual(["subitem", "subitem"], [chunk.chunk_type for chunk in chunks])
        self.assertEqual(["\uac00. first", "\ub098. second"], [chunk.text for chunk in chunks])

    def test_supplementary_direct_items_become_chunks(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_supp_items",
            source_file="supp-items.hwp",
            document_name="Supplementary Items",
            file_type="hwp",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text="부칙\n1. 시행일\na. detail")])],
            raw_text="부칙\n1. 시행일\na. detail",
        )
        supplementary = StructureNode(
            node_id="node_supp",
            document_id=parsed.document_id,
            node_type="supplementary",
            number="부칙",
            title=None,
            text="부칙",
            page_start=1,
            page_end=1,
            order_index=0,
        )
        item = StructureNode(
            node_id="node_item",
            document_id=parsed.document_id,
            node_type="item",
            number="1.",
            title=None,
            text="1. 시행일",
            page_start=1,
            page_end=1,
            parent_id=supplementary.node_id,
            order_index=1,
        )
        subitem = StructureNode(
            node_id="node_subitem",
            document_id=parsed.document_id,
            node_type="subitem",
            number="a.",
            title=None,
            text="a. detail",
            page_start=1,
            page_end=1,
            parent_id=item.node_id,
            order_index=2,
        )

        chunks = Chunker().build_chunks([supplementary, item, subitem], parsed, ChunkOptions(include_context_header=False))
        item_chunk = next(chunk for chunk in chunks if chunk.chunk_type == "item")

        self.assertEqual(item_chunk.source_node_ids, ["node_item", "node_subitem"])
        self.assertIn("시행일", item_chunk.text)
        self.assertIn("detail", item_chunk.text)

    def test_supplementary_chunk_keeps_effective_override_metadata(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_supp_effective",
            source_file="supp-effective.hwp",
            document_name="Supplementary Effective",
            file_type="hwp",
            pages=[
                ParsedPage(
                    page_no=1,
                    blocks=[
                        ParsedBlock(
                            text=(
                                "부칙 <2025. 12. 30.>\n"
                                "제1조(시행일) 이 규정은 2026년 1월 1일부터 시행한다. "
                                "다만, 제17조제1항제1호의 개정규정은 2026년 1월 2일부터 시행한다."
                            )
                        )
                    ],
                )
            ],
            raw_text="",
        )
        supplementary = StructureNode(
            node_id="node_supp",
            document_id=parsed.document_id,
            node_type="supplementary",
            number="부칙",
            title=None,
            text=(
                "부칙 <2025. 12. 30.>\n"
                "제1조(시행일) 이 규정은 2026년 1월 1일부터 시행한다. "
                "다만, 제17조제1항제1호의 개정규정은 2026년 1월 2일부터 시행한다."
            ),
            page_start=1,
            page_end=1,
            order_index=0,
        )

        chunk = Chunker().build_chunks([supplementary], parsed, ChunkOptions(include_context_header=False))[0]

        self.assertEqual(chunk.chunk_type, "supplementary_provision")
        self.assertTrue(chunk.metadata["is_supplementary_provision"])
        self.assertEqual(chunk.metadata["supplementary_identifier_date"], "2025-12-30")
        self.assertEqual(chunk.metadata["effective_date"], "2026-01-01")
        self.assertEqual(chunk.metadata["valid_from"], "2026-01-01")
        self.assertEqual(chunk.metadata["article_effective_overrides"][0]["article_ref"], "제17조제1항제1호")
        self.assertEqual(chunk.metadata["article_effective_overrides"][0]["effective_date"], "2026-01-02")

    def test_supplementary_paragraph_label_and_boilerplate_metadata_reach_chunk(self) -> None:
        text = "부칙 <2026.1.1.>\n①(시행일) 이 규정은 공포한 날부터 시행한다."
        parsed = ParsedDocument(
            document_id="doc_supp_label",
            source_file="supp-label.hwp",
            document_name="Supplementary Label",
            file_type="hwp",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])],
            raw_text=text,
        )
        nodes = StructureDetector().detect(parsed)

        chunks = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=False))
        paragraph_chunk = next(chunk for chunk in chunks if chunk.chunk_type == "paragraph")

        self.assertTrue(paragraph_chunk.metadata["is_supplementary_provision"])
        self.assertEqual(paragraph_chunk.metadata["paragraph_label"], "시행일")
        self.assertEqual(paragraph_chunk.metadata["supplementary_paragraph_label"], "시행일")
        self.assertTrue(paragraph_chunk.metadata["supplementary_boilerplate"])
        self.assertEqual(paragraph_chunk.metadata["effective_date"], "promulgation_date")

    def test_supplementary_child_inherits_identifier_date_from_parent_context(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_supp_context_date",
            source_file="supp-context-date.pdf",
            document_name="Supplementary Context Date",
            file_type="pdf",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text="Addenda (2025. 12. 30.)\n1. Effective date")])],
            raw_text="Addenda (2025. 12. 30.)\n1. Effective date",
        )
        supplementary = StructureNode(
            node_id="node_supp",
            document_id=parsed.document_id,
            node_type="supplementary",
            number="Addenda (2025. 12. 30.)",
            title=None,
            text="Addenda (2025. 12. 30.)",
            page_start=1,
            page_end=1,
            order_index=0,
        )
        paragraph = StructureNode(
            node_id="node_para",
            document_id=parsed.document_id,
            node_type="paragraph",
            number="1.",
            title="Effective date",
            text="1. Effective date\nThis rule takes effect on promulgation.",
            page_start=1,
            page_end=1,
            parent_id=supplementary.node_id,
            order_index=1,
        )

        chunks = Chunker().build_chunks(
            [supplementary, paragraph],
            parsed,
            ChunkOptions(include_context_header=False),
        )
        paragraph_chunk = next(chunk for chunk in chunks if chunk.chunk_type == "paragraph")

        self.assertTrue(paragraph_chunk.metadata["is_supplementary_provision"])
        self.assertEqual(paragraph_chunk.metadata["supplementary_no"], "Addenda (2025. 12. 30.)")
        self.assertEqual(paragraph_chunk.metadata["supplementary_identifier_date"], "2025-12-30")

    def test_long_standalone_paragraph_splits_without_overlap_duplication(self) -> None:
        tokens = [f"tok{i:03d}" for i in range(80)]
        text = " ".join(tokens)
        parsed = ParsedDocument(
            document_id="doc_long_paragraph",
            source_file="long-paragraph.hwp",
            document_name="Long Paragraph",
            file_type="hwp",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])],
            raw_text=text,
        )
        paragraph = StructureNode(
            node_id="node_long_paragraph",
            document_id=parsed.document_id,
            node_type="paragraph",
            number="preamble",
            title=None,
            text=text,
            page_start=1,
            page_end=1,
            order_index=0,
        )

        chunks = Chunker().build_chunks(
            [paragraph],
            parsed,
            ChunkOptions(max_chunk_chars=120, overlap_chars=40, include_context_header=False),
        )
        combined = " ".join(chunk.text for chunk in chunks)

        self.assertGreater(len(chunks), 1)
        self.assertEqual(combined.count("tok010"), 1)
        self.assertEqual(combined.count("tok050"), 1)

    def test_inherits_unambiguous_temporal_metadata_within_regulation_scope(self) -> None:
        dated = Chunk(
            chunk_id="chunk_dated",
            document_id="doc_temporal_inherit",
            chunk_type="supplementary_provision",
            text="dated",
            metadata={
                "document_id": "doc_temporal_inherit",
                "regulation_no": "1-1",
                "regulation_title": "Temporal Rule",
                "effective_date": "2026-01-01",
                "revision_date": "2025-12-30",
                "valid_from": "2026-01-01",
                "revision_history": [{"event_type": "revision", "date": "2025-12-30"}],
                "article_validity_windows": [
                    {
                        "article_ref": "*",
                        "valid_from": "2026-01-01",
                        "valid_to": None,
                        "source": "document_effective_date",
                    }
                ],
            },
        )
        target = Chunk(
            chunk_id="chunk_target",
            document_id="doc_temporal_inherit",
            chunk_type="article",
            text="target",
            metadata={
                "document_id": "doc_temporal_inherit",
                "regulation_no": "1-1",
                "regulation_title": "Temporal Rule",
                "article_no": "Article2",
                "warnings": [],
            },
        )

        Chunker()._inherit_temporal_metadata_from_chunks([dated, target])

        self.assertEqual(target.metadata["effective_date"], "2026-01-01")
        self.assertEqual(target.metadata["revision_date"], "2025-12-30")
        self.assertEqual(target.metadata["valid_from"], "2026-01-01")
        self.assertEqual(target.metadata["revision_history"][0]["date"], "2025-12-30")
        self.assertEqual(target.metadata["article_validity_windows"][0]["article_ref"], "*")
        self.assertTrue(target.metadata["temporal_metadata_inherited"])
        self.assertEqual(target.metadata["temporal_metadata_scope"], "regulation")
        self.assertIn("chunk_dated", target.metadata["temporal_metadata_source_chunk_ids"])

    def test_inherited_article_override_requires_exact_article_match(self) -> None:
        dated = Chunk(
            chunk_id="chunk_override",
            document_id="doc_temporal_override",
            chunk_type="supplementary_provision",
            text="dated",
            metadata={
                "document_id": "doc_temporal_override",
                "regulation_no": "1-1",
                "regulation_title": "Temporal Rule",
                "effective_date": "2026-01-01",
                "valid_from": "2026-01-01",
                "article_effective_overrides": [
                    {"article_ref": "Article7", "effective_date": "2026-01-02"},
                ],
                "article_validity_windows": [
                    {
                        "article_ref": "*",
                        "valid_from": "2026-01-01",
                        "valid_to": None,
                        "source": "document_effective_date",
                    },
                    {
                        "article_ref": "Article7",
                        "valid_from": "2026-01-02",
                        "valid_to": None,
                        "source": "article_effective_override",
                    },
                ],
            },
        )
        target = Chunk(
            chunk_id="chunk_article7",
            document_id="doc_temporal_override",
            chunk_type="article",
            text="target",
            metadata={
                "document_id": "doc_temporal_override",
                "regulation_no": "1-1",
                "regulation_title": "Temporal Rule",
                "article_no": "Article7",
                "warnings": [],
            },
        )

        Chunker()._inherit_temporal_metadata_from_chunks([dated, target])

        self.assertEqual(target.metadata["valid_from"], "2026-01-02")
        self.assertEqual(target.metadata["article_effective_overrides"][0]["article_ref"], "Article7")
        self.assertEqual(
            [window["article_ref"] for window in target.metadata["article_validity_windows"]],
            ["*", "Article7"],
        )

    def test_article_local_temporal_metadata_does_not_bleed_to_other_articles(self) -> None:
        source = Chunk(
            chunk_id="chunk_article7",
            document_id="doc_article_temporal_local",
            chunk_type="article",
            text="article 7",
            metadata={
                "document_id": "doc_article_temporal_local",
                "regulation_no": "1-1",
                "regulation_title": "Temporal Rule",
                "article_no": "Article7",
                "effective_date": "2026-01-01",
                "valid_to": "2026-12-31",
                "article_validity_windows": [
                    {"article_ref": "Article7", "valid_from": "2026-01-01", "valid_to": "2026-12-31"}
                ],
                "warnings": [],
            },
        )
        target = Chunk(
            chunk_id="chunk_article8",
            document_id="doc_article_temporal_local",
            chunk_type="article",
            text="article 8",
            metadata={
                "document_id": "doc_article_temporal_local",
                "regulation_no": "1-1",
                "regulation_title": "Temporal Rule",
                "article_no": "Article8",
                "warnings": [],
            },
        )

        Chunker()._inherit_temporal_metadata_from_chunks([source, target])

        self.assertNotIn("effective_date", target.metadata)
        self.assertNotIn("valid_from", target.metadata)
        self.assertNotIn("valid_to", target.metadata)
        self.assertNotIn("article_validity_windows", target.metadata)

    def test_supplementary_temporal_ambiguity_sets_review_flag_without_guessing(self) -> None:
        first = Chunk(
            chunk_id="chunk_first_date",
            document_id="doc_temporal_conflict",
            chunk_type="supplementary_provision",
            text="first",
            metadata={
                "document_id": "doc_temporal_conflict",
                "regulation_no": "1-1",
                "regulation_title": "Temporal Rule",
                "effective_date": "2026-01-01",
                "warnings": [],
            },
        )
        second = Chunk(
            chunk_id="chunk_second_date",
            document_id="doc_temporal_conflict",
            chunk_type="supplementary_provision",
            text="second",
            metadata={
                "document_id": "doc_temporal_conflict",
                "regulation_no": "1-1",
                "regulation_title": "Temporal Rule",
                "effective_date": "2026-02-01",
                "warnings": [],
            },
        )
        target = Chunk(
            chunk_id="chunk_conflict_target",
            document_id="doc_temporal_conflict",
            chunk_type="article",
            text="target",
            metadata={
                "document_id": "doc_temporal_conflict",
                "regulation_no": "1-1",
                "regulation_title": "Temporal Rule",
                "warnings": [],
            },
        )

        Chunker()._inherit_temporal_metadata_from_chunks([first, second, target])

        self.assertNotIn("effective_date", target.metadata)
        self.assertNotIn("temporal_metadata_conflict_fields", target.metadata)
        self.assertEqual(target.metadata["temporal_metadata_ambiguous_fields"], ["effective_date"])
        self.assertEqual(target.metadata["temporal_metadata_ambiguous_scope"], "regulation")
        self.assertIn("chunk_first_date", target.metadata["temporal_metadata_ambiguous_source_chunk_ids"])
        self.assertEqual(target.metadata["warnings"], [])
        self.assertEqual(target.warnings, [])

    def test_primary_scope_temporal_conflict_still_blocks_without_guessing(self) -> None:
        first = Chunk(
            chunk_id="chunk_first_date",
            document_id="doc_temporal_conflict",
            chunk_type="regulation",
            text="first",
            metadata={
                "document_id": "doc_temporal_conflict",
                "regulation_no": "1-1",
                "regulation_title": "Temporal Rule",
                "effective_date": "2026-01-01",
                "warnings": [],
            },
        )
        second = Chunk(
            chunk_id="chunk_second_date",
            document_id="doc_temporal_conflict",
            chunk_type="regulation",
            text="second",
            metadata={
                "document_id": "doc_temporal_conflict",
                "regulation_no": "1-1",
                "regulation_title": "Temporal Rule",
                "effective_date": "2026-02-01",
                "warnings": [],
            },
        )
        target = Chunk(
            chunk_id="chunk_conflict_target",
            document_id="doc_temporal_conflict",
            chunk_type="article",
            text="target",
            metadata={
                "document_id": "doc_temporal_conflict",
                "regulation_no": "1-1",
                "regulation_title": "Temporal Rule",
                "warnings": [],
            },
        )

        Chunker()._inherit_temporal_metadata_from_chunks([first, second, target])

        self.assertNotIn("effective_date", target.metadata)
        self.assertEqual(target.metadata["temporal_metadata_conflict_fields"], ["effective_date"])
        self.assertNotIn("temporal_metadata_ambiguous_fields", target.metadata)


if __name__ == "__main__":
    unittest.main()
