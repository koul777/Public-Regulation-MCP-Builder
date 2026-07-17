from __future__ import annotations

import unittest
from pathlib import Path

from app.parsers.hwp_parser import HwpParser
from app.processors.chunker import Chunker
from app.processors.normalizer import TextNormalizer
from app.processors.structure_detector import StructureDetector
from scripts.analyze_regulation_corpus import summarize_pipeline_counts
from scripts.verify_real_parser_regression_fixtures import fixture_skip_reason, resolve_named_fixture_for_tests


def hwp_record(tag_id: int, payload: bytes) -> bytes:
    size = len(payload)
    header = tag_id | (0 << 10) | (size << 20)
    return header.to_bytes(4, byteorder="little") + payload


REAL_HWP_FIXTURES = {
    fixture_id: resolve_named_fixture_for_tests(fixture_id)
    for fixture_id in (
        "authority_delegation_hwp_3488_237887",
        "contract_regulation_hwp_27297_237280",
    )
}


def require_real_hwp_fixture(test_case: unittest.TestCase, fixture_id: str) -> Path:
    result = REAL_HWP_FIXTURES[fixture_id]
    if not result["passed"]:
        test_case.skipTest(fixture_skip_reason(result))
    return Path(str(result["matched_path"]))


class HwpInventoryRegressionTests(unittest.TestCase):
    def test_hwp_inventory_counts_compact_numbered_items_without_counting_dates(self) -> None:
        parser = HwpParser()

        hierarchy = parser._hierarchy_inventory(
            [
                "제1조(정의) 본문",
                "① 항 본문",
                "1.첫째 항목",
                "2)둘째 항목",
                "1.2.세부 항목",
                "2026.7.1. 시행한다",
            ]
        )

        self.assertEqual(1, hierarchy["articles"])
        self.assertEqual(1, hierarchy["paragraphs"])
        self.assertEqual(3, hierarchy["numbered_items"])

    def test_authority_delegation_regulation_inventory(self) -> None:
        fixture = require_real_hwp_fixture(self, "authority_delegation_hwp_3488_237887")

        parsed = HwpParser().parse(fixture, "doc_hwp_inventory")
        inventory = parsed.metadata["document_inventory"]

        hierarchy = inventory["hierarchy"]
        self.assertEqual(12, hierarchy["articles"])
        self.assertEqual(11, hierarchy["paragraphs"])
        self.assertEqual(13, hierarchy["numbered_items"])
        self.assertEqual(7, hierarchy["hangul_items"])
        self.assertEqual(17, hierarchy["parenthesized_items"])

        attachments = inventory["attachments"]
        self.assertEqual(1, attachments["annexes"])
        self.assertEqual(0, attachments["forms"])
        self.assertEqual(0, attachments["sheets"])

        tables = inventory["tables"]
        self.assertEqual(96, tables["total"])
        self.assertEqual(96, tables["top_level"])
        self.assertEqual(0, tables["nested"])

        supplements = inventory["supplements"]
        self.assertEqual(60, supplements["blocks"])
        self.assertEqual(60, supplements["blocks_with_effective_date"])
        self.assertEqual(15, supplements["explicit_effective_articles"])
        self.assertEqual(45, supplements["direct_effective_clauses"])
        self.assertEqual(7, supplements["application_clauses"])

        self.assertEqual(0, inventory["footnotes"])
        self.assertEqual(0, inventory["endnotes"])
        self.assertEqual(1, inventory["attachment_caption_count"])
        self.assertEqual(28, inventory["note_line_count"])
        self.assertEqual(29, inventory["captions"])

    def test_authority_delegation_inventory_drives_goldset_pipeline_counts(self) -> None:
        fixture = require_real_hwp_fixture(self, "authority_delegation_hwp_3488_237887")

        parsed = HwpParser().parse(fixture, "doc_hwp_inventory")
        normalized = TextNormalizer().normalize_document(parsed)
        nodes = StructureDetector().detect(normalized)
        chunks = Chunker().build_chunks(nodes, normalized)
        chunk_rows = [chunk.model_dump() for chunk in chunks]

        counts = summarize_pipeline_counts(chunk_rows, {"extension": ".hwp"})

        self.assertEqual(12, counts["article_count_distinct_article_no"])
        self.assertEqual(48, counts["paragraph_or_item_chunk_count"])
        self.assertEqual(1, counts["appendix_or_form_candidate_count"])
        self.assertEqual(96, counts["table_like_chunk_count"])
        self.assertEqual(0, counts["nested_table_candidate_count"])
        self.assertEqual(60, counts["supplementary_or_effective_date_candidate_count"])
        self.assertEqual(0, counts["footnote_or_caption_candidate_count"])
        self.assertEqual(1, counts["attachment_caption_count"])
        self.assertEqual(28, counts["note_line_count"])

    def test_contract_regulation_inventory_boundaries(self) -> None:
        fixture = require_real_hwp_fixture(self, "contract_regulation_hwp_27297_237280")

        parsed = HwpParser().parse(fixture, "doc_hwp_contract_inventory")
        inventory = parsed.metadata["document_inventory"]

        hierarchy = inventory["hierarchy"]
        self.assertEqual(67, hierarchy["articles"])
        self.assertEqual(100, hierarchy["paragraphs"])
        self.assertEqual(143, hierarchy["numbered_items"])
        self.assertEqual(8, hierarchy["hangul_items"])
        self.assertEqual(0, hierarchy["parenthesized_items"])

        attachments = inventory["attachments"]
        self.assertEqual(3, attachments["annexes"])
        self.assertEqual(29, attachments["forms"])
        self.assertEqual(0, attachments["sheets"])
        self.assertEqual(32, attachments["total"])
        self.assertEqual(1, attachments["deleted_count"])

        tables = inventory["tables"]
        self.assertEqual(63, tables["total"])
        self.assertEqual(56, tables["top_level"])
        self.assertEqual(7, tables["nested"])

        supplements = inventory["supplements"]
        self.assertEqual(21, supplements["blocks"])
        self.assertEqual(21, supplements["blocks_with_effective_date"])
        self.assertEqual(6, supplements["explicit_effective_articles"])
        self.assertEqual(15, supplements["direct_effective_clauses"])

        self.assertEqual(0, inventory["footnotes"])
        self.assertEqual(0, inventory["endnotes"])
        self.assertEqual(32, inventory["attachment_caption_count"])
        self.assertEqual(23, inventory["note_line_count"])
        self.assertEqual(55, inventory["captions"])
        self.assertEqual([], inventory["warnings"])

    def test_contract_regulation_inventory_drives_goldset_pipeline_counts(self) -> None:
        fixture = require_real_hwp_fixture(self, "contract_regulation_hwp_27297_237280")

        parsed = HwpParser().parse(fixture, "doc_hwp_contract_inventory")
        normalized = TextNormalizer().normalize_document(parsed)
        nodes = StructureDetector().detect(normalized)
        chunks = Chunker().build_chunks(nodes, normalized)
        chunk_rows = [chunk.model_dump() for chunk in chunks]

        counts = summarize_pipeline_counts(chunk_rows, {"extension": ".hwp"})

        self.assertEqual(67, counts["article_count_distinct_article_no"])
        self.assertEqual(251, counts["paragraph_or_item_chunk_count"])
        self.assertEqual(32, counts["appendix_or_form_candidate_count"])
        self.assertEqual(63, counts["table_like_chunk_count"])
        self.assertEqual(7, counts["nested_table_candidate_count"])
        self.assertEqual(21, counts["supplementary_or_effective_date_candidate_count"])
        self.assertEqual(0, counts["footnote_or_caption_candidate_count"])
        self.assertEqual(32, counts["attachment_caption_count"])
        self.assertEqual(23, counts["note_line_count"])

    def test_table_control_flag_variants_are_counted_by_structure(self) -> None:
        parser = HwpParser()
        section = b"".join(
            hwp_record(71, b" lbt" + raw_flags.to_bytes(4, byteorder="little") + b"\x00" * 8)
            + hwp_record(77, b"\x00" * 24)
            for raw_flags in (0x082A2210, 0x082A2211, 0x182A2210, 0x182A2310)
        )

        inventory = parser._table_inventory_from_section(section, "BodyText/Section0")

        self.assertEqual(4, len(inventory["tables"]))
        self.assertEqual(
            {
                "0x082A2210": 1,
                "0x082A2211": 1,
                "0x182A2210": 1,
                "0x182A2310": 1,
            },
            dict(inventory["table_control_flags"]),
        )

    def test_attachment_inventory_ignores_references_and_business_terms(self) -> None:
        parser = HwpParser()
        texts = [
            "별표1의 개정규정",
            "[별표1]의 지역본부",
            "별지1을 각 적용한다",
            "서식관리",
            "서식승인",
            "승인서식 관리",
            "진료서식 제ㆍ개정",
            "【 별 표 1 】<개정 2026.7.1.>",
            "[별지 제2호서식]",
            "별지 3",
            "붙임 1",
        ]

        inventory = parser._attachment_inventory(texts)

        self.assertEqual(1, inventory["annexes"])
        self.assertEqual(2, inventory["forms"])
        self.assertEqual(1, inventory["sheets"])
        self.assertEqual(4, inventory["total"])


if __name__ == "__main__":
    unittest.main()
