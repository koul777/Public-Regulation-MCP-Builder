from __future__ import annotations

import unittest
from pathlib import Path

from app.processors.structure_detector import StructureDetector
from app.schemas.parsed import ParsedBlock, ParsedDocument, ParsedPage


FIXTURE = Path(__file__).parent / "fixtures" / "sample_regulation.md"


class StructureDetectorTests(unittest.TestCase):
    def test_detects_korean_regulation_hierarchy(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        nodes = StructureDetector().detect_from_text(text)

        article_numbers = [node.number for node in nodes if node.node_type == "article"]
        node_types = [node.node_type for node in nodes]

        self.assertIn("제1장", [node.number for node in nodes])
        self.assertIn("제1절", [node.number for node in nodes])
        self.assertIn("제1조", article_numbers)
        self.assertIn("제1조의2", article_numbers)
        self.assertIn("제2조", article_numbers)
        self.assertIn("paragraph", node_types)
        self.assertIn("item", node_types)
        self.assertIn("subitem", node_types)
        self.assertIn("supplementary", node_types)
        self.assertIn("appendix", node_types)

    def test_article_parent_points_to_nearest_section(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        nodes = StructureDetector().detect_from_text(text)
        by_id = {node.node_id: node for node in nodes}
        article = next(node for node in nodes if node.node_type == "article" and node.number == "제1조")

        self.assertIsNotNone(article.parent_id)
        self.assertEqual(by_id[article.parent_id].node_type, "section")

    def test_preserves_hwpx_source_metadata_on_appended_article_lines(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_hwpx_meta",
            source_file="sample.hwpx",
            document_name="메타규정",
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
            raw_text="제1조(목적) 본문\n그림 1. 처리 흐름",
        )

        nodes = StructureDetector().detect(parsed)
        article = next(node for node in nodes if node.node_type == "article")

        self.assertEqual(article.metadata["source_hwpx_block_types"], ["paragraph", "image"])
        self.assertEqual(article.metadata["source_xml_files"], ["Contents/section0.xml"])
        self.assertEqual(article.metadata["caption_count"], 1)
        self.assertIn("그림 1. 처리 흐름", article.text)

    def test_preserves_hwp_stream_and_section_metadata_on_appended_lines(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_hwp_meta",
            source_file="source.hwp",
            document_name="HWP Source",
            file_type="hwp",
            pages=[
                ParsedPage(
                    page_no=1,
                    blocks=[
                        ParsedBlock(
                            text="\uc81c1\uc870(\ubaa9\uc801) \ubcf8\ubb38",
                            metadata={
                                "hwp_stream": "BodyText/Section0",
                                "section_index": 1,
                                "hwp_extraction_mode": "legacy_ole_para_text_only",
                                "hwp_native_table_geometry": False,
                            },
                        ),
                        ParsedBlock(
                            text="\ucd94\uac00 \ubcf8\ubb38",
                            metadata={
                                "hwp_stream": "BodyText/Section1",
                                "section_index": 2,
                                "hwp_extraction_mode": "legacy_ole_para_text_only",
                                "hwp_native_table_geometry": False,
                            },
                        ),
                    ],
                )
            ],
            raw_text="\uc81c1\uc870(\ubaa9\uc801) \ubcf8\ubb38\n\ucd94\uac00 \ubcf8\ubb38",
        )

        nodes = StructureDetector().detect(parsed)
        article = next(node for node in nodes if node.node_type == "article")

        self.assertEqual(article.metadata["source_hwp_streams"], ["BodyText/Section0", "BodyText/Section1"])
        self.assertEqual(article.metadata["source_hwp_section_indices"], [1, 2])
        self.assertEqual(article.metadata["source_hwp_extraction_modes"], ["legacy_ole_para_text_only"])
        self.assertFalse(article.metadata["source_hwp_native_table_geometry"])

    def test_hwpx_count_metadata_from_multiline_block_counts_once(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_hwpx_multiline_counts",
            source_file="sample.hwpx",
            document_name="HWPX Counts",
            file_type="hwpx",
            pages=[
                ParsedPage(
                    page_no=1,
                    blocks=[
                        ParsedBlock(
                            type="image",
                            text="\uc81c1\uc870(\ubaa9\uc801) \uadf8\ub9bc \uc124\uba85\n\ucd94\uac00 \ucea1\uc158",
                            metadata={
                                "xml_file": "Contents/section0.xml",
                                "hwpx_xml_block_index": 7,
                                "hwpx_block_type": "image",
                                "hwpx_image_caption_count": 1,
                                "hwpx_table_note_count": 2,
                            },
                        )
                    ],
                )
            ],
            raw_text="\uc81c1\uc870(\ubaa9\uc801) \uadf8\ub9bc \uc124\uba85\n\ucd94\uac00 \ucea1\uc158",
        )

        nodes = StructureDetector().detect(parsed)
        article = next(node for node in nodes if node.node_type == "article")

        self.assertEqual(article.metadata["hwpx_image_caption_count"], 1)
        self.assertEqual(article.metadata["hwpx_table_note_count"], 2)
        self.assertNotIn("_merged_hwpx_count_sources", article.metadata)

    def test_table_block_stays_single_structure_node(self) -> None:
        parsed = ParsedDocument(
            document_id="doc_table_block",
            source_file="sample.hwpx",
            document_name="Table Block",
            file_type="hwpx",
            pages=[
                ParsedPage(
                    page_no=1,
                    blocks=[
                        ParsedBlock(
                            type="table",
                            text="Header A | Header B\nValue A | Value B",
                            metadata={"hwpx_block_type": "table", "xml_file": "Contents/section0.xml"},
                        )
                    ],
                )
            ],
            raw_text="Header A | Header B\nValue A | Value B",
        )

        nodes = StructureDetector().detect(parsed)
        tables = [node for node in nodes if node.node_type == "table"]

        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].text, "Header A | Header B\nValue A | Value B")

    def test_article_references_are_not_detected_as_new_articles(self) -> None:
        text = "\n".join(
            [
                "제1조(목적) 본문",
                "제2조(정의) 본문",
                "제6조에 따라 개정되는 법률은 별도로 정한다.",
                "제12조제1항 중 일부를 다음과 같이 개정한다.",
                "제14조중 문구를 변경한다.",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)
        article_numbers = [node.number for node in nodes if node.node_type == "article"]

        self.assertEqual(article_numbers, ["제1조", "제2조"])

    def test_supplementary_articles_are_children_of_supplementary_node(self) -> None:
        text = "\n".join(
            [
                "제1장 총칙",
                "제1조(목적) 본문",
                "부칙 <2026.1.1.>",
                "제1조(시행일) 이 규정은 공포한 날부터 시행한다.",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)
        by_id = {node.node_id: node for node in nodes}
        supplementary = next(node for node in nodes if node.node_type == "supplementary")
        article = next(node for node in nodes if node.node_type == "article" and node.title == "시행일")

        self.assertEqual(article.parent_id, supplementary.node_id)
        self.assertEqual(by_id[article.parent_id].node_type, "supplementary")

    def test_articles_inside_form_are_kept_inside_form_text(self) -> None:
        text = "\n".join(
            [
                "【별지 제1호 서식】",
                "계약서",
                "제1조(목적) 이 서식의 예시 조항이다.",
                "제2조(기준) 예시 조항이다.",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)

        self.assertEqual([node.node_type for node in nodes], ["form"])
        self.assertIn("제1조(목적)", nodes[0].text)

    def test_clause_after_form_rejoins_previous_article_when_it_references_different_form(self) -> None:
        text = "\n".join(
            [
                "제31조(휴직의 운영) 휴직 운영 기준을 정한다.",
                "【별지 제3호 서식】",
                "근무상황부",
                "④ 휴직자는 별지 제15호서식에 따른 휴직자 복무상황 보고서를 제출해야 한다.",
            ]
        )

        nodes = StructureDetector().detect_from_text(text)
        article = next(node for node in nodes if node.node_type == "article")
        form = next(node for node in nodes if node.node_type == "form")
        paragraph = next(node for node in nodes if node.node_type == "paragraph")

        self.assertEqual(article.number, "제31조")
        self.assertNotIn("④ 휴직자는", form.text)
        self.assertEqual(paragraph.parent_id, article.node_id)
        self.assertIn("attachment_container_boundary_inferred", paragraph.warnings)

    def test_clause_inside_form_stays_when_it_references_same_form(self) -> None:
        text = "\n".join(
            [
                "【별지 제1호 서식】",
                "신청서",
                "① 별지 제1호 서식의 신청인은 성명을 적는다.",
            ]
        )

        nodes = StructureDetector().detect_from_text(text)

        self.assertEqual([node.node_type for node in nodes], ["form"])
        self.assertIn("① 별지 제1호 서식", nodes[0].text)

    def test_deleted_article_is_sequence_preserving_article(self) -> None:
        text = "\n".join(
            [
                "제1조(목적) 본문",
                "제2조삭제 <2026.1.1.>",
                "제3조(다음) 본문",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)
        articles = [node for node in nodes if node.node_type == "article"]

        self.assertEqual([node.number for node in articles], ["제1조", "제2조", "제3조"])
        self.assertEqual(articles[1].metadata.get("lifecycle"), "deleted")

    def test_deleted_and_omitted_articles_get_lifecycle_titles(self) -> None:
        nodes = StructureDetector().detect_from_text("제1조삭제 <2024.1.1.>\n제2조 생략")
        articles = [node for node in nodes if node.node_type == "article"]

        self.assertEqual([node.title for node in articles], ["삭제", "생략"])
        self.assertFalse(any("article_title_missing" in node.warnings for node in articles))

    def test_regulation_heading_clears_supplementary_scope(self) -> None:
        text = "\n".join(
            [
                "제1장 총칙",
                "부칙 <2026.1.1.>",
                "제1조(시행일) 이 규정은 공포한 날부터 시행한다.",
                "4-2-1. 인사규정",
                "제1조(목적) 인사에 관한 사항을 정한다.",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)
        by_id = {node.node_id: node for node in nodes}
        regulation = next(node for node in nodes if node.node_type == "regulation")
        article = next(node for node in nodes if node.node_type == "article" and node.title == "목적")

        self.assertEqual(regulation.number, "4-2-1")
        self.assertEqual(article.parent_id, regulation.node_id)
        self.assertEqual(by_id[article.parent_id].node_type, "regulation")

    def test_repeated_same_regulation_heading_is_skipped_as_running_header_outside_supplementary(self) -> None:
        text = "\n".join(
            [
                "1-2-1. 한국학중앙연구원정관",
                "제1조(목적) 본문",
                "1-2-1. 한국학중앙연구원정관",
                "제2조(정의) 본문",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)
        regulations = [node for node in nodes if node.node_type == "regulation"]
        articles = [node for node in nodes if node.node_type == "article"]

        self.assertEqual(len(regulations), 1)
        self.assertEqual([node.number for node in articles], ["제1조", "제2조"])
        self.assertTrue(all(node.parent_id == regulations[0].node_id for node in articles))

    def test_repeated_same_regulation_heading_after_internal_chapter_is_skipped(self) -> None:
        text = "\n".join(
            [
                "4-2-1. 인사규정",
                "제1장 총칙",
                "제1조(목적) 본문",
                "제2장 보칙",
                "4-2-1. 인사 규정",
                "제2조(위임) 본문",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)
        regulations = [node for node in nodes if node.node_type == "regulation"]
        articles = [node for node in nodes if node.node_type == "article"]

        self.assertEqual(len(regulations), 1)
        self.assertEqual([node.number for node in articles], ["제1조", "제2조"])

    def test_internal_chapters_after_regulation_are_regulation_children(self) -> None:
        text = "\n".join(
            [
                "제1편 기본법령 및 법인일반",
                "제2장 법인 및 조직",
                "1-2-1. 한국학중앙연구원정관",
                "제1장 총칙",
                "제1조(목적) 본문",
                "제2장 재산 및 회계",
                "제2조(회계) 본문",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)
        by_id = {node.node_id: node for node in nodes}
        regulation = next(node for node in nodes if node.node_type == "regulation")
        chapters = [node for node in nodes if node.node_type == "chapter"]
        internal_chapters = [node for node in chapters if node.parent_id == regulation.node_id]
        articles = [node for node in nodes if node.node_type == "article"]

        self.assertEqual([node.number for node in internal_chapters], ["제1장", "제2장"])
        self.assertEqual([by_id[node.parent_id].number for node in articles], ["제1장", "제2장"])
        self.assertTrue(all(by_id[by_id[node.parent_id].parent_id].node_id == regulation.node_id for node in articles))

    def test_unnumbered_prose_after_regulation_stays_with_regulation(self) -> None:
        text = "\n".join(
            [
                "제1편 기본법령 및 법인일반",
                "제2장 법인 및 조직",
                "1-2-1. 한국학중앙연구원정관",
                "이 정관은 연구원의 운영 기준을 정한다.",
                "제1장 총칙",
                "제1조(목적) 본문",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)
        regulation = next(node for node in nodes if node.node_type == "regulation")
        outer_chapter = next(node for node in nodes if node.node_type == "chapter" and node.title == "법인 및 조직")

        self.assertIn("이 정관은 연구원의 운영 기준을 정한다.", regulation.text)
        self.assertNotIn("이 정관은 연구원의 운영 기준을 정한다.", outer_chapter.text)

    def test_next_regulation_after_internal_chapter_uses_outer_parent(self) -> None:
        text = "\n".join(
            [
                "제1편 기본법령 및 법인일반",
                "제2장 법인 및 조직",
                "1-2-1. 한국학중앙연구원정관",
                "제1장 총칙",
                "제1조(목적) 본문",
                "제2장 재산 및 회계",
                "제2조(회계) 본문",
                "1-2-2. 이사회규정",
                "제1조(목적) 본문",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)
        by_id = {node.node_id: node for node in nodes}
        regulations = [node for node in nodes if node.node_type == "regulation"]

        self.assertEqual([node.number for node in regulations], ["1-2-1", "1-2-2"])
        self.assertEqual(regulations[0].parent_id, regulations[1].parent_id)
        self.assertEqual(by_id[regulations[1].parent_id].number, "제2장")

    def test_outer_chapter_after_supplementary_closes_regulation_scope(self) -> None:
        text = "\n".join(
            [
                "제1편 기본법령 및 법인일반",
                "제2장 법인 및 조직",
                "1-2-1. 한국학중앙연구원정관",
                "제1조(목적) 본문",
                "부칙 <2026.1.1.>",
                "제1조(시행일) 본문",
                "제3장 위원회",
                "1-3-1. 위원회규정",
                "제1조(목적) 본문",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)
        by_id = {node.node_id: node for node in nodes}
        outer_chapter = next(
            node for node in nodes if node.node_type == "chapter" and node.number == "제3장" and node.title == "위원회"
        )
        next_regulation = next(node for node in nodes if node.node_type == "regulation" and node.number == "1-3-1")

        self.assertEqual(by_id[outer_chapter.parent_id].node_type, "part")
        self.assertEqual(next_regulation.parent_id, outer_chapter.node_id)

    def test_same_regulation_header_inside_supplementary_is_running_header(self) -> None:
        text = "\n".join(
            [
                "4-2-1. 인사규정",
                "제1조(목적) 본문",
                "부칙 <2026.1.1.>",
                "제1조(시행일) 본문",
                "4-2-1. 인사규정",
                "계속되는 조문 문장",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)
        regulations = [node for node in nodes if node.node_type == "regulation"]
        supplementary_article = next(node for node in nodes if node.node_type == "article" and node.title == "시행일")

        self.assertEqual(len(regulations), 1)
        self.assertIn("계속되는 조문 문장", supplementary_article.text)

    def test_running_header_inside_article_does_not_split_article(self) -> None:
        text = "\n".join(
            [
                "제1조(목적) 첫 문장",
                "4-2-1. 인사규정",
                "다음 문장",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)

        self.assertEqual([node.node_type for node in nodes], ["article"])
        self.assertIn("4-2-1. 인사규정", nodes[0].text)

    def test_new_regulation_after_appendix_starts_new_boundary(self) -> None:
        text = "\n".join(
            [
                "1-1-1. 첫번째규정",
                "[별표 1]",
                "첨부 내용",
                "1-1-2. 두번째규정",
                "제1조(목적) 본문",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)
        regulations = [node for node in nodes if node.node_type == "regulation"]
        appendix = next(node for node in nodes if node.node_type == "appendix")
        article = next(node for node in nodes if node.node_type == "article")

        self.assertEqual([node.number for node in regulations], ["1-1-1", "1-1-2"])
        self.assertNotIn("1-1-2. 두번째규정", appendix.text)
        self.assertEqual(article.parent_id, regulations[1].node_id)

    def test_appendix_and_form_references_are_kept_as_prose(self) -> None:
        text = "\n".join(
            [
                "부칙 <2026.1.1.>",
                "제2조(다른 규정의 개정) 다음과 같이 개정한다.",
                "[별표 3]의 “재임용”을 “재계약”으로 한다.",
                "별지 제2호서식 중 “주소”를 “소재지”로 한다.",
                "제3조(경과조치) 종전 규정에 따른다.",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)

        self.assertNotIn("appendix", [node.node_type for node in nodes])
        self.assertNotIn("form", [node.node_type for node in nodes])
        amendment = next(node for node in nodes if node.node_type == "article" and node.title == "다른 규정의 개정")
        self.assertIn("[별표 3]의", amendment.text)
        self.assertIn("별지 제2호서식 중", amendment.text)

    def test_bare_self_reference_does_not_close_its_own_appendix(self) -> None:
        text = "\n".join(
            [
                "제5조(별표) 세부사항은 별표와 같다.",
                "[별표 1]",
                "평가 기준표",
                "1. 이 별표에서 정하지 아니한 사항은 위원회가 정한다.",
                "2. 배점은 100점을 만점으로 한다.",
            ]
        )

        nodes = StructureDetector().detect_from_text(text)
        appendix = next(node for node in nodes if node.node_type == "appendix")
        article = next(node for node in nodes if node.node_type == "article")

        self.assertIn("이 별표에서 정하지 아니한 사항은", appendix.text)
        self.assertIn("배점은 100점을 만점으로 한다.", appendix.text)
        self.assertNotIn("배점은 100점을 만점으로 한다.", article.text)

    def test_bare_appendix_reference_with_chamjo_is_kept_as_prose(self) -> None:
        text = "\n".join(
            [
                "제1조(목적) 별표 인용을 포함한다.",
                "별표 1 참조",
                "별지 제2호서식 참조",
                "[별표 1]",
                "첨부 내용",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)
        article = next(node for node in nodes if node.node_type == "article")
        appendices = [node for node in nodes if node.node_type == "appendix"]
        forms = [node for node in nodes if node.node_type == "form"]

        self.assertEqual(len(appendices), 1)
        self.assertEqual(forms, [])
        self.assertIn("별표 1 참조", article.text)
        self.assertIn("별지 제2호서식 참조", article.text)

    def test_inline_article_paragraph_item_markers_are_split_into_children(self) -> None:
        text = (
            "제5조(위임기준) ① 각 조직단위별 위임기준은 다음 각 호와 같다."
            "<개정 2010. 12. 31.> 1. 본 부 1.2. 세부 기준 가. 이사장 나. 이사"
        )
        nodes = StructureDetector().detect_from_text(text)
        article = next(node for node in nodes if node.node_type == "article")
        paragraphs = [node for node in nodes if node.node_type == "paragraph"]
        items = [node for node in nodes if node.node_type == "item"]
        subitems = [node for node in nodes if node.node_type == "subitem"]

        self.assertEqual([node.number for node in paragraphs], ["①"])
        self.assertEqual([node.number for node in items], ["1.", "1.2."])
        self.assertEqual([node.number for node in subitems], ["가.", "나."])
        self.assertEqual(paragraphs[0].parent_id, article.node_id)
        self.assertTrue(all(node.parent_id == paragraphs[0].node_id for node in items))
        self.assertTrue(all(node.parent_id == items[-1].node_id for node in subitems))

    def test_number_paren_and_hangul_paren_items_are_detected_as_children(self) -> None:
        text = "\n".join(
            [
                "\uc81c1\uc870(\ubaa9\uc801) \ubcf8\ubb38",
                "\u2460 \ud56d \ubcf8\ubb38",
                "1) \uc22b\uc790 \uad04\ud638 \ud56d\ubaa9",
                "\uac00) \ud55c\uae00 \uad04\ud638 \ud56d\ubaa9",
                "(\ub098) \ud55c\uae00 \uad04\ud638 \ud56d\ubaa9",
                "\ub2e4. \ud55c\uae00 \uc810 \ud56d\ubaa9",
                "2. \uc22b\uc790 \uc810 \ud56d\ubaa9",
            ]
        )

        nodes = StructureDetector().detect_from_text(text)

        article = next(node for node in nodes if node.node_type == "article")
        paragraphs = [node for node in nodes if node.node_type == "paragraph"]
        items = [node for node in nodes if node.node_type == "item"]
        subitems = [node for node in nodes if node.node_type == "subitem"]

        self.assertEqual([node.number for node in paragraphs], ["\u2460"])
        self.assertEqual([node.number for node in items], ["1)", "2."])
        self.assertEqual([node.number for node in subitems], ["\uac00)", "(\ub098)", "\ub2e4."])
        self.assertEqual(paragraphs[0].parent_id, article.node_id)
        self.assertTrue(all(node.parent_id == paragraphs[0].node_id for node in items))
        self.assertTrue(all(node.parent_id == items[0].node_id for node in subitems))

    def test_je_hang_and_je_ho_lines_are_detected_as_paragraph_and_item(self) -> None:
        text = "\n".join(
            [
                "\uc81c1\uc870(\ubaa9\uc801) \ubcf8\ubb38",
                "\uc81c1\ud56d \uc774 \uaddc\uc815\uc740 \uc815\ubcf4\ub97c \ubcf4\ud638\ud55c\ub2e4.",
                "\uc81c1\ud638 \ub2e4\uc74c \ud56d\ubaa9\uc740 \uc720\ud6a8\ud558\ub2e4.",
                "\uac00. \ud558\uc704 \ud56d\ubaa9",
            ]
        )

        nodes = StructureDetector().detect_from_text(text)

        paragraph_nodes = [node for node in nodes if node.node_type == "paragraph"]
        item_nodes = [node for node in nodes if node.node_type == "item"]
        subitem_nodes = [node for node in nodes if node.node_type == "subitem"]

        self.assertEqual(["\uc81c1\ud56d"], [node.number for node in paragraph_nodes])
        self.assertEqual(["\uc81c1\ud638"], [node.number for node in item_nodes])
        self.assertEqual(["\uac00."], [node.number for node in subitem_nodes])
        self.assertEqual(paragraph_nodes[0].parent_id, next(node for node in nodes if node.node_type == "article").node_id)
        self.assertEqual(item_nodes[0].parent_id, paragraph_nodes[0].node_id)
        self.assertEqual(subitem_nodes[0].parent_id, item_nodes[0].node_id)

    def test_je_hang_and_je_ho_reference_phrases_are_not_misclassified(self) -> None:
        text = "\n".join(
            [
                "\uc81c1\uc870(\ubaa9\uc801) \ubcf8\ubb38",
                "\uc81c1\ud56d\uc758 \uaddc\uc815\uc5d0 \ub530\ub77c \ucc98\ub9ac\ud55c\ub2e4.",
                "\uc81c1\ud638\uc758 \uacbd\uc6b0\uc5d0\ub294 \uc608\uc678\ub2e4.",
            ]
        )

        nodes = StructureDetector().detect_from_text(text)

        article = next(node for node in nodes if node.node_type == "article")
        self.assertEqual([], [node for node in nodes if node.node_type == "paragraph"])
        self.assertEqual([], [node for node in nodes if node.node_type == "item"])
        self.assertIn("\uc81c1\ud56d\uc758 \uaddc\uc815\uc5d0 \ub530\ub77c \ucc98\ub9ac\ud55c\ub2e4.", article.text)
        self.assertIn("\uc81c1\ud638\uc758 \uacbd\uc6b0\uc5d0\ub294 \uc608\uc678\ub2e4.", article.text)

    def test_inline_je_hang_and_je_ho_markers_are_split_into_children(self) -> None:
        text = (
            "\uc81c5\uc870(\uc704\uc784\uae30\uc900) \ubcf8\ubb38 "
            "\uc81c1\ud56d \uac01 \uc870\uc9c1\ub2e8\uc704\ubcc4 \uc704\uc784\uae30\uc900\uc740 "
            "\ub2e4\uc74c \uac01 \ud638\uc640 \uac19\ub2e4. "
            "\uc81c1\ud638 \ubcf8\ubd80 \uac00. \uc774\uc0ac\uc7a5"
        )

        nodes = StructureDetector().detect_from_text(text)

        self.assertEqual([node.node_type for node in nodes], ["article", "paragraph", "item", "subitem"])
        self.assertEqual(["\uc81c1\ud56d", "\uc81c1\ud638", "\uac00."], [node.number for node in nodes[1:]])
        self.assertEqual(nodes[1].parent_id, nodes[0].node_id)
        self.assertEqual(nodes[2].parent_id, nodes[1].node_id)
        self.assertEqual(nodes[3].parent_id, nodes[2].node_id)

    def test_inline_je_hang_and_je_ho_reference_phrases_are_not_split(self) -> None:
        detector = StructureDetector()

        parts = detector._split_inline_structure_lines(
            "\uc81c1\uc870(\ubaa9\uc801) \ubcf8\ubb38 \uc81c1\ud56d\uc758 \uaddc\uc815\uc5d0 \ub530\ub77c \uc81c1\ud638\uc758 \uacbd\uc6b0\ub3c4 \ud3ec\ud568\ub41c\ub2e4."
        )

        self.assertEqual(
            [
                "\uc81c1\uc870(\ubaa9\uc801) \ubcf8\ubb38 \uc81c1\ud56d\uc758 \uaddc\uc815\uc5d0 \ub530\ub77c \uc81c1\ud638\uc758 \uacbd\uc6b0\ub3c4 \ud3ec\ud568\ub41c\ub2e4.",
            ],
            parts,
        )

    def test_inline_spaced_je_hang_je_ho_citations_are_not_split(self) -> None:
        # A spaced citation ("\uc81c5\uc870 \uc81c1\ud56d \ubc0f \uc81c2\ud56d", "\uc885\uc804\uc758 \uc81c5\ud638 \ubc0f \uc81c6\ud638")
        # puts the \ud56d/\ud638 marker before a space, so unlike the "\uc81c1\ud56d\uc758" form it
        # matches the split pattern.  The article marker already has a reference
        # guard; the \ud56d/\ud638 markers must too, or the host clause is torn apart.
        detector = StructureDetector()

        self.assertEqual(
            ["\uc81c1\uc870(\uc608\uc2dc) \uc704\uc6d0\uc7a5\uc740 \uc81c5\uc870 \uc81c1\ud56d \ubc0f \uc81c2\ud56d\uc5d0 \ub530\ub77c \uc9c1\ubb34\ub97c \uc218\ud589\ud55c\ub2e4."],
            detector._split_inline_structure_lines(
                "\uc81c1\uc870(\uc608\uc2dc) \uc704\uc6d0\uc7a5\uc740 \uc81c5\uc870 \uc81c1\ud56d \ubc0f \uc81c2\ud56d\uc5d0 \ub530\ub77c \uc9c1\ubb34\ub97c \uc218\ud589\ud55c\ub2e4."
            ),
        )
        self.assertEqual(
            ["\uc885\uc804\uc758 \uc81c5\ud638 \ubc0f \uc81c6\ud638\ub294 \uac01\uac01 \uc81c6\ud638 \ubc0f \uc81c7\ud638\ub85c \ud55c\ub2e4."],
            detector._split_inline_structure_lines(
                "\uc885\uc804\uc758 \uc81c5\ud638 \ubc0f \uc81c6\ud638\ub294 \uac01\uac01 \uc81c6\ud638 \ubc0f \uc81c7\ud638\ub85c \ud55c\ub2e4."
            ),
        )

    def test_regulation_level_subitems_are_detected_as_nodes(self) -> None:
        text = "\n".join(
            [
                "1-2-1. Regulation",
                "\uac00. first",
                "\ub098. second",
            ]
        )

        nodes = StructureDetector().detect_from_text(text)

        self.assertEqual(["regulation", "subitem", "subitem"], [node.node_type for node in nodes])
        self.assertEqual(["\uac00.", "\ub098."], [node.number for node in nodes if node.node_type == "subitem"])

    def test_hangul_word_fragments_are_not_detected_as_subitems(self) -> None:
        text = "\n".join(
            [
                "\uc81c1\uc870(\ub0a9\ubd80) \ub0a9\ubd80 \uae30\uc900\uc744 \uc815\ud55c\ub2e4.",
                "\u2460 \uc774\uc6a9\ub8cc\ub294 \ub2e4\uc74c \uae30\uc900\uc5d0 \ub530\ub978\ub2e4.",
                "1. \uc774\uc6a9\ub8cc\ub294 \ub9e4\uc6d4 \ub0a9\ubd80\ud55c\ub2e4.",
                "\uc6d4)\ub9c8\ub2e4 \uacf5\uc0ac\uc5d0 \ub0a9\ubd80\ud558\uc5ec\uc57c \ud55c\ub2e4.",
                "(\uc2e4)\uc7a5\uc744 \uacbd\uc720\ud558\uc5ec \ubcf4\uace0\ud55c\ub2e4.",
                "\uac00. \uc815\uc0c1\uc801\uc778 \ubaa9 \ud56d\ubaa9",
            ]
        )

        nodes = StructureDetector().detect_from_text(text)

        item = next(node for node in nodes if node.node_type == "item")
        subitems = [node for node in nodes if node.node_type == "subitem"]
        self.assertEqual(["\uac00."], [node.number for node in subitems])
        self.assertIn("\uc6d4)\ub9c8\ub2e4", item.text)
        self.assertIn("(\uc2e4)\uc7a5\uc744", item.text)

    def test_inline_parenthesized_hangul_word_is_not_split_as_subitem(self) -> None:
        detector = StructureDetector()

        parts = detector._split_inline_structure_lines(
            "1. \ub2f4\ub2f9 (\uc2e4) \ubcf8\ubb38\uc740 \ubd80\uc11c\uc7a5\uc774 \ud655\uc778\ud55c\ub2e4."
        )
        valid_parts = detector._split_inline_structure_lines(
            "1. \ub2f4\ub2f9 \ud56d\ubaa9 (\ub098) \uc815\uc0c1\uc801\uc778 \ubaa9 \ud56d\ubaa9"
        )

        self.assertEqual(["1. \ub2f4\ub2f9 (\uc2e4) \ubcf8\ubb38\uc740 \ubd80\uc11c\uc7a5\uc774 \ud655\uc778\ud55c\ub2e4."], parts)
        self.assertEqual(["1. \ub2f4\ub2f9 \ud56d\ubaa9", "(\ub098) \uc815\uc0c1\uc801\uc778 \ubaa9 \ud56d\ubaa9"], valid_parts)

    def test_inline_number_paren_and_hangul_paren_items_are_split(self) -> None:
        text = (
            "\uc81c1\uc870(\ubaa9\uc801) \ubcf8\ubb38 "
            "\u2460 \ud56d \ubcf8\ubb38 "
            "1) \uc22b\uc790 \uad04\ud638 \ud56d\ubaa9 "
            "\uac00) \ud55c\uae00 \uad04\ud638 \ud56d\ubaa9 "
            "(\ub098) \ud55c\uae00 \uad04\ud638 \ud56d\ubaa9 "
            "\ub2e4. \ud55c\uae00 \uc810 \ud56d\ubaa9 "
            "2. \uc22b\uc790 \uc810 \ud56d\ubaa9"
        )

        nodes = StructureDetector().detect_from_text(text)

        paragraphs = [node for node in nodes if node.node_type == "paragraph"]
        items = [node for node in nodes if node.node_type == "item"]
        subitems = [node for node in nodes if node.node_type == "subitem"]

        self.assertEqual([node.number for node in paragraphs], ["\u2460"])
        self.assertEqual([node.number for node in items], ["1)", "2."])
        self.assertEqual([node.number for node in subitems], ["\uac00)", "(\ub098)", "\ub2e4."])

    def test_inline_person_word_before_daman_is_not_split_as_ja_subitem(self) -> None:
        text = (
            "\uc81c1\uc870(\uacb0\uaca9\uc0ac\uc720) \ub2e4\uc74c \uac01 \ud638\uc5d0 \ub530\ub978\ub2e4. "
            "1. \uc9d5\uacc4\ucc98\ubd84 \uc885\ub8cc \ud6c4 \uae30\uac04\uc774 \uacbd\uacfc\ud558\uc9c0 \uc54a\uc740 \uc790. "
            "\ub2e4\ub9cc, \uc608\uc678\ub294 \ubcc4\ub3c4\ub85c \uc815\ud55c\ub2e4."
        )

        nodes = StructureDetector().detect_from_text(text)

        item = next(node for node in nodes if node.node_type == "item")
        self.assertEqual([], [node for node in nodes if node.node_type == "subitem"])
        self.assertIn("\uc790. \ub2e4\ub9cc", item.text)

    def test_inline_ja_enumerator_with_normal_body_is_still_split(self) -> None:
        text = (
            "\uc81c1\uc870(\ubaa9\uc801) \ubcf8\ubb38 "
            "\u2460 \ud56d \ubcf8\ubb38 "
            "1. \uc22b\uc790 \ud56d\ubaa9 "
            "\uc544. \uc120\ud589 \ubaa9 \ud56d\ubaa9 "
            "\uc790. \ub2e4\ub9cc \uc801\uc6a9\ud558\ub294 \uc815\uc0c1 \ubaa9 \ud56d\ubaa9"
        )

        nodes = StructureDetector().detect_from_text(text)

        subitems = [node.number for node in nodes if node.node_type == "subitem"]
        self.assertEqual(["\uc544.", "\uc790."], subitems)

    def test_compact_numbered_item_markers_are_detected_without_space(self) -> None:
        text = "\n".join(
            [
                "\uc81c1\uc870(\ubaa9\uc801) \ubcf8\ubb38",
                "\u2460 \ud56d \ubcf8\ubb38",
                "1.\uccab\uc9f8 \ud56d\ubaa9",
                "1)\ub458\uc9f8 \ud56d\ubaa9",
                "1.2.\uc138\ubd80 \ud56d\ubaa9",
                "2026.7.1. \uc2dc\ud589\ud55c\ub2e4",
            ]
        )

        nodes = StructureDetector().detect_from_text(text)

        paragraphs = [node for node in nodes if node.node_type == "paragraph"]
        items = [node for node in nodes if node.node_type == "item"]

        self.assertEqual([node.number for node in paragraphs], ["\u2460"])
        self.assertEqual([node.number for node in items], ["1.", "1)", "1.2."])
        self.assertTrue(any("2026.7.1." in node.text for node in items))

    def test_spaced_revision_date_is_not_detected_as_numbered_item(self) -> None:
        text = "\n".join(
            [
                "2008. 12. 6 \uc804\uba74\uac1c\uc815",
                "2026-7-6 \uc77c\ubd80\uac1c\uc815",
                "\uc81c1\uc870(\ubaa9\uc801) \ubcf8\ubb38",
                "1. \uc2e4\uc81c \ud56d\ubaa9",
            ]
        )

        nodes = StructureDetector().detect_from_text(text)
        items = [node for node in nodes if node.node_type == "item"]

        self.assertEqual(["1."], [node.number for node in items])
        preamble = next(node for node in nodes if node.number == "preamble")
        self.assertIn("2008. 12. 6 \uc804\uba74\uac1c\uc815", preamble.text)
        self.assertIn("2026-7-6 \uc77c\ubd80\uac1c\uc815", preamble.text)

    def test_revision_history_numbered_lines_are_not_items(self) -> None:
        text = "\n".join(
            [
                "사규규정",
                "24. 규정 제1518호 개정 2020.1.1.",
                "25. 내규 제1600호 일부개정 2022.1.1.",
                "제1조(목적) 본문",
            ]
        )

        nodes = StructureDetector().detect_from_text(text)

        preamble = next(node for node in nodes if node.number == "preamble")
        self.assertEqual([], [node for node in nodes if node.node_type == "item"])
        self.assertIn("규정 제1518호 개정", preamble.text)
        self.assertIn("내규 제1600호 일부개정", preamble.text)
        self.assertEqual(["제1조"], [node.number for node in nodes if node.node_type == "article"])

    def test_toc_dot_leader_numbered_lines_are_not_items(self) -> None:
        text = "\n".join(
            [
                "목차",
                "1. 총칙 ........................................ 3",
                "2. 계약 ........................................ 10",
                "제1조(목적) 본문",
            ]
        )

        nodes = StructureDetector().detect_from_text(text)

        preamble = next(node for node in nodes if node.number == "preamble")
        self.assertEqual([], [node for node in nodes if node.node_type == "item"])
        self.assertIn("1. 총칙", preamble.text)
        self.assertIn("2. 계약", preamble.text)

    def test_revision_reference_numbered_item_still_detected(self) -> None:
        text = "\n".join(
            [
                "제1조(목적) 본문",
                "① 다음 각 호를 따른다.",
                "1.규정 제1518호에 따른 적용 기준",
                "2. 실제 항목",
            ]
        )

        nodes = StructureDetector().detect_from_text(text)
        items = [node for node in nodes if node.node_type == "item"]

        self.assertEqual(["1.", "2."], [node.number for node in items])
        self.assertIn("규정 제1518호에 따른", items[0].text)

    def test_inline_compact_numbered_item_markers_are_split_without_splitting_dates(self) -> None:
        text = (
            "\uc81c1\uc870(\ubaa9\uc801) \ubcf8\ubb38 "
            "\u2460 \ud56d \ubcf8\ubb38 "
            "1.\uccab\uc9f8 \ud56d\ubaa9 "
            "1)\ub458\uc9f8 \ud56d\ubaa9 "
            "1.2.\uc138\ubd80 \ud56d\ubaa9 "
            "2026.7.1. \uc2dc\ud589\ud55c\ub2e4"
        )

        nodes = StructureDetector().detect_from_text(text)

        items = [node for node in nodes if node.node_type == "item"]

        self.assertEqual([node.number for node in items], ["1.", "1)", "1.2."])
        self.assertIn("2026.7.1.", items[-1].text)

    def test_inline_article_marker_with_title_starts_new_article(self) -> None:
        text = (
            "제2조(정의) 이 규정에서 사용하는 용어의 뜻은 다음과 같다. "
            "제4조의2(인사청탁 금지 및 제재) ① 임직원은 인사청탁을 하여서는 아니 된다."
        )

        nodes = StructureDetector().detect_from_text(text)
        articles = [node for node in nodes if node.node_type == "article"]
        paragraphs = [node for node in nodes if node.node_type == "paragraph"]

        self.assertEqual([node.number for node in articles], ["제2조", "제4조의2"])
        self.assertNotIn("제4조의2", articles[0].text)
        self.assertEqual([node.number for node in paragraphs], ["①"])
        self.assertEqual(paragraphs[0].parent_id, articles[1].node_id)

    def test_inline_article_reference_with_title_is_not_split(self) -> None:
        text = "제1조(목적) 이 기준은 제5조(정의)에 따라 필요한 사항을 정한다."

        nodes = StructureDetector().detect_from_text(text)
        articles = [node for node in nodes if node.node_type == "article"]

        self.assertEqual([node.number for node in articles], ["제1조"])
        self.assertIn("제5조(정의)에 따라", articles[0].text)

    def test_explicit_caption_markers_add_caption_metadata(self) -> None:
        text = "\n".join(
            [
                "제1조(목적) 본문",
                "그림 1. 처리 흐름",
                "[별표 1]",
                "표 1. 평가 기준",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)
        article = next(node for node in nodes if node.node_type == "article")
        appendix = next(node for node in nodes if node.node_type == "appendix")

        self.assertEqual(article.metadata.get("caption_count"), 1)
        self.assertEqual(article.metadata.get("caption_parent"), "line_note")
        self.assertEqual(appendix.metadata.get("caption_count"), 1)
        self.assertEqual(appendix.metadata.get("caption_parent"), "line_note")

    def test_explicit_caption_marker_variants_add_caption_metadata(self) -> None:
        text = "\n".join(
            [
                "제1조(목적) 본문",
                "표 1) 평가 기준",
                "그림 2: 처리 흐름",
            ]
        )

        nodes = StructureDetector().detect_from_text(text)

        article = next(node for node in nodes if node.node_type == "article")
        self.assertEqual(article.metadata.get("caption_count"), 2)

    def test_bracketed_caption_marker_variants_add_caption_metadata(self) -> None:
        text = "\n".join(
            [
                "제1조(목적) 본문",
                "[표 1] 평가 기준",
                "<그림 2> 처리 흐름",
                "【그림 3】 처리 결과",
            ]
        )

        nodes = StructureDetector().detect_from_text(text)

        article = next(node for node in nodes if node.node_type == "article")
        self.assertEqual(article.metadata.get("caption_count"), 3)

    def test_caption_marker_lines_are_not_split_into_numbered_items(self) -> None:
        text = "\n".join(
            [
                "제1조(목적) 본문",
                "[표 1] 평가 기준 1. 세부 기준",
                "각주 1. 이 줄은 주석 설명이다.",
            ]
        )

        nodes = StructureDetector().detect_from_text(text)

        article = next(node for node in nodes if node.node_type == "article")
        self.assertEqual(article.metadata.get("caption_count"), 2)
        self.assertEqual([], [node for node in nodes if node.node_type == "item"])

    def test_general_note_markers_are_not_caption_metadata(self) -> None:
        text = "\n".join(
            [
                "제1조(목적) 본문",
                "※ 증빙서류는 음영처리 후 제출한다.",
                "[별표 1]",
                "* 사업의 특성에 따라 조정할 수 있다.",
                "평가위원 수 4명 이상 * 사업의 특성에 따라 조정할 수 있다.",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)
        article = next(node for node in nodes if node.node_type == "article")
        appendix = next(node for node in nodes if node.node_type == "appendix")

        self.assertNotIn("caption_count", article.metadata)
        self.assertNotIn("caption_count", appendix.metadata)

    def test_angle_bracket_appendix_after_supplementary_starts_appendix_container(self) -> None:
        text = "\n".join(
            [
                "부칙 <2016.11.7.>",
                "제3조(타당성재조사에 대한 적용례) 본문",
                "<별 표>",
                "1. 예비타당성조사 사업계획서",
                "<별표 1> 예비타당성조사 사업계획서",
                "제1조(서식 예시) 이 줄은 별표 안의 예시 조문이다.",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)
        article = next(node for node in nodes if node.node_type == "article")
        appendices = [node for node in nodes if node.node_type == "appendix"]

        self.assertEqual(len(appendices), 2)
        self.assertNotIn("<별 표>", article.text)
        self.assertEqual(appendices[0].number, "별표")
        self.assertEqual(appendices[1].number, "별표1")
        self.assertIn("1. 예비타당성조사 사업계획서", appendices[0].text)
        self.assertIn("제1조(서식 예시)", appendices[1].text)

    def test_amended_article_quote_inside_supplementary_stays_in_previous_article(self) -> None:
        text = "\n".join(
            [
                "부칙 <2026.1.1.>",
                "제1조(시행일) 이 규정은 공포한 날부터 시행한다.",
                "제2조(다른 규정의 개정) 인사규정을 다음과 같이 개정한다.",
                "제8조(부서운영평가 결과의 반영) 부서평가결과를 반영한다.",
                "제3조(경과조치) 종전 규정에 따른다.",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)
        articles = [node for node in nodes if node.node_type == "article"]

        self.assertEqual([node.title for node in articles], ["시행일", "다른 규정의 개정", "경과조치"])
        self.assertIn("제8조(부서운영평가", articles[1].text)

    def test_chapter_after_appendix_closes_appendix_container(self) -> None:
        text = "\n".join(
            [
                "1-1-1. 첫번째규정",
                "[별표 1]",
                "첨부 내용",
                "제1장 총칙",
                "제1조(목적) 본문",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)
        appendices = [node for node in nodes if node.node_type == "appendix"]
        chapters = [node for node in nodes if node.node_type == "chapter"]
        articles = [node for node in nodes if node.node_type == "article"]

        self.assertEqual(len(appendices), 1)
        self.assertEqual(len(chapters), 1)
        self.assertNotIn("제1장 총칙", appendices[0].text)
        self.assertEqual(articles[0].parent_id, chapters[0].node_id)

    def test_supplementary_circled_paragraph_label_is_preserved(self) -> None:
        text = "\n".join(
            [
                "부칙 <2026.1.1.>",
                "①(시행일) 이 규정은 공포한 날부터 시행한다.",
                "②(경과 조치) 종전 규정에 따른다.",
            ]
        )

        nodes = StructureDetector().detect_from_text(text)
        paragraphs = [node for node in nodes if node.node_type == "paragraph"]

        self.assertEqual([node.number for node in paragraphs], ["①", "②"])
        self.assertEqual([node.metadata.get("paragraph_label") for node in paragraphs], ["시행일", "경과 조치"])
        self.assertEqual([node.title for node in paragraphs], ["시행일", "경과 조치"])

    def test_square_bullet_hwp_paragraph_labels_are_preserved(self) -> None:
        text = "\n".join(
            [
                "「상임이사 휴가 운용기준」",
                "□ (적용 대상) 상임이사를 대상으로 한다.",
                "□ (연차휴가) ① 상임이사의 연차휴가는 연간 21일로 한다.",
                "② 연도중 임명된 경우 기준 휴가일수에서 월할 계산하여 부여한다.",
                "□ (병가) 병가는 취업규칙상의 병가에 준하여 운용한다.",
            ]
        )

        nodes = StructureDetector().detect_from_text(text)
        paragraphs = [
            node
            for node in nodes
            if node.node_type == "paragraph" and node.number != "preamble"
        ]

        self.assertEqual([node.number for node in paragraphs], ["□", "□", "②", "□"])
        self.assertEqual(
            [node.metadata.get("paragraph_label") for node in paragraphs],
            ["적용 대상", "연차휴가", None, "병가"],
        )
        self.assertNotIn("병가는 취업규칙", paragraphs[2].text)

    def test_preserves_leading_preamble_before_first_detected_node(self) -> None:
        text = "\n".join(
            [
                "General notice before structured content.",
                "This line used to be dropped before the first heading.",
                "1-2-1. Regulation Title",
                "Unnumbered introduction under the regulation.",
            ]
        )
        nodes = StructureDetector().detect_from_text(text)

        self.assertEqual(nodes[0].node_type, "paragraph")
        self.assertEqual(nodes[0].number, "preamble")
        self.assertIn("General notice", nodes[0].text)
        self.assertIn("orphan_preamble_text", nodes[0].warnings)
        self.assertEqual(nodes[1].node_type, "regulation")
        self.assertIn("Unnumbered introduction", nodes[1].text)


if __name__ == "__main__":
    unittest.main()
