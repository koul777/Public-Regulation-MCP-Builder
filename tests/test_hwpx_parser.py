from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from app.processors.chunker import Chunker
from app.processors.structure_detector import StructureDetector
from app.schemas.chunk import ChunkOptions
from app.parsers.base import ParserError
from app.parsers.factory import get_parser
from app.parsers.hwpx_parser import HwpxParser


class HwpxParserTests(unittest.TestCase):
    def test_factory_supports_hwpx_extension(self) -> None:
        self.assertIsInstance(get_parser(Path("sample.hwpx")), HwpxParser)

    def test_preserves_paragraph_table_order_without_text_duplication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ordered.hwpx"
            self._write_hwpx(
                path,
                """
                <root xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">
                  <hp:p><hp:run><hp:t>Article One</hp:t></hp:run></hp:p>
                  <hp:tbl>
                    <hp:tr>
                      <hp:tc><hp:p><hp:run><hp:t>Header A</hp:t></hp:run></hp:p></hp:tc>
                      <hp:tc><hp:p><hp:run><hp:t>Header B</hp:t></hp:run></hp:p></hp:tc>
                    </hp:tr>
                    <hp:tr>
                      <hp:tc><hp:p><hp:run><hp:t>Value A</hp:t></hp:run></hp:p></hp:tc>
                      <hp:tc><hp:p><hp:run><hp:t>Value B</hp:t></hp:run></hp:p></hp:tc>
                    </hp:tr>
                  </hp:tbl>
                  <hp:p><hp:run><hp:t>Article Two</hp:t></hp:run></hp:p>
                </root>
                """,
            )

            parsed = HwpxParser().parse(path, "doc_hwpx")

        blocks = parsed.pages[0].blocks
        self.assertEqual([block.type for block in blocks], ["text", "table", "text"])
        self.assertEqual(blocks[0].text, "Article One")
        self.assertEqual(blocks[1].text, "Header A | Header B\nValue A | Value B")
        self.assertEqual(blocks[1].metadata["hwpx_block_type"], "table")
        self.assertEqual(blocks[0].metadata["source_xml_role"], "body")
        self.assertEqual(blocks[1].metadata["source_xml_role"], "body")
        self.assertEqual(blocks[2].text, "Article Two")
        self.assertEqual(parsed.raw_text.count("Article One"), 1)
        self.assertEqual(parsed.raw_text.count("Header A"), 1)

    def test_extracts_table_embedded_inside_paragraph_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "embedded-table.hwpx"
            self._write_hwpx(
                path,
                """
                <root xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">
                  <hp:p>
                    <hp:run>
                      <hp:t>Before inline object</hp:t>
                      <hp:tbl>
                        <hp:tr>
                          <hp:tc><hp:p><hp:run><hp:t>Outer A</hp:t></hp:run></hp:p></hp:tc>
                          <hp:tc>
                            <hp:p>
                              <hp:run>
                                <hp:tbl>
                                  <hp:tr>
                                    <hp:tc><hp:p><hp:run><hp:t>Nested A</hp:t></hp:run></hp:p></hp:tc>
                                    <hp:tc><hp:p><hp:run><hp:t>Nested B</hp:t></hp:run></hp:p></hp:tc>
                                  </hp:tr>
                                </hp:tbl>
                              </hp:run>
                            </hp:p>
                          </hp:tc>
                        </hp:tr>
                      </hp:tbl>
                      <hp:t>After inline object</hp:t>
                    </hp:run>
                  </hp:p>
                </root>
                """,
            )

            parsed = HwpxParser().parse(path, "doc_hwpx")

        blocks = parsed.pages[0].blocks
        self.assertEqual([block.type for block in blocks], ["text", "table"])
        self.assertEqual(blocks[0].text, "Before inline object After inline object")
        table = blocks[1]
        self.assertEqual(table.metadata["hwpx_nested_table_count"], 1)
        self.assertEqual(table.metadata["hwpx_nested_table_text_snippets"], ["Nested A Nested B"])
        self.assertIn("nested_table", table.metadata["hwpx_parser_review_flags"])
        self.assertNotIn("Nested A", blocks[0].text)
        self.assertEqual(parsed.metadata["parser_uncertainty_schema_version"], "reg-rag-parser-uncertainty-v1")
        self.assertEqual(parsed.metadata["parser_uncertainty_source"], "hwpx")
        self.assertEqual(parsed.metadata["parser_uncertainty_risk_level"], "medium")
        self.assertIn("hwpx_nested_table", parsed.metadata["parser_uncertainty_flags"])

    def test_falls_back_to_loose_text_runs_when_no_paragraph_nodes_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "loose-text.hwpx"
            self._write_hwpx(
                path,
                """
                <root xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">
                  <hp:run><hp:t>Loose Text One</hp:t></hp:run>
                  <hp:run><hp:t>Loose Text Two</hp:t></hp:run>
                </root>
                """,
            )

            parsed = HwpxParser().parse(path, "doc_hwpx")

        blocks = parsed.pages[0].blocks
        self.assertEqual([block.text for block in blocks], ["Loose Text One", "Loose Text Two"])
        self.assertEqual(blocks[0].metadata["hwpx_block_type"], "loose_text")
        self.assertEqual(parsed.metadata["parser_uncertainty_risk_level"], "low")
        self.assertEqual(parsed.metadata["parser_uncertainty_recommendation"], "none")

    def test_preserves_captions_notes_and_image_caption_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rich.hwpx"
            self._write_hwpx(
                path,
                """
                <root xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">
                  <hp:p><hp:run><hp:t>본문 조항</hp:t></hp:run></hp:p>
                  <hp:footNote><hp:p><hp:run><hp:t>각주 설명</hp:t></hp:run></hp:p></hp:footNote>
                  <hp:tbl>
                    <hp:caption><hp:p><hp:run><hp:t>표 1. 심사 기준</hp:t></hp:run></hp:p></hp:caption>
                    <hp:tr>
                      <hp:tc><hp:p><hp:run><hp:t>구분</hp:t></hp:run></hp:p></hp:tc>
                      <hp:tc><hp:p><hp:run><hp:t>기준</hp:t></hp:run></hp:p></hp:tc>
                    </hp:tr>
                    <hp:tr>
                      <hp:tc><hp:p><hp:run><hp:t>A</hp:t></hp:run></hp:p></hp:tc>
                      <hp:tc><hp:p><hp:run><hp:t>80점 이상</hp:t></hp:run></hp:p></hp:tc>
                    </hp:tr>
                  </hp:tbl>
                  <hp:endNote><hp:p><hp:run><hp:t>미주 설명</hp:t></hp:run></hp:p></hp:endNote>
                  <hp:pic>
                    <hp:caption><hp:p><hp:run><hp:t>그림 1. 처리 흐름</hp:t></hp:run></hp:p></hp:caption>
                  </hp:pic>
                </root>
                """,
            )

            parsed = HwpxParser().parse(path, "doc_hwpx")

        blocks = parsed.pages[0].blocks
        self.assertEqual(
            [block.metadata["hwpx_block_type"] for block in blocks],
            ["paragraph", "footnote", "caption", "table", "endnote", "image"],
        )
        self.assertEqual([block.type for block in blocks], ["text", "text", "text", "table", "text", "image"])
        self.assertEqual(blocks[2].metadata["caption_parent"], "table")
        self.assertEqual(blocks[3].text, "구분 | 기준\nA | 80점 이상")
        self.assertEqual(blocks[5].metadata["caption_count"], 1)
        self.assertIn("그림 1. 처리 흐름", parsed.raw_text)

    def test_marks_complex_table_structures_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "complex-table.hwpx"
            self._write_hwpx(
                path,
                """
                <root xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">
                  <hp:tbl>
                    <hp:caption><hp:p><hp:run><hp:t>Table Caption</hp:t></hp:run></hp:p></hp:caption>
                    <hp:tr>
                      <hp:tc rowSpan="2"><hp:p><hp:run><hp:t>Outer A</hp:t></hp:run></hp:p></hp:tc>
                      <hp:tc>
                        <hp:tbl>
                          <hp:tr>
                            <hp:tc><hp:p><hp:run><hp:t>Nested A</hp:t></hp:run></hp:p></hp:tc>
                            <hp:tc><hp:p><hp:run><hp:t>Nested B</hp:t></hp:run></hp:p></hp:tc>
                          </hp:tr>
                        </hp:tbl>
                        <hp:pic>
                          <hp:caption><hp:p><hp:run><hp:t>Figure Caption</hp:t></hp:run></hp:p></hp:caption>
                        </hp:pic>
                        <hp:footNote><hp:p><hp:run><hp:t>Cell Note</hp:t></hp:run></hp:p></hp:footNote>
                      </hp:tc>
                    </hp:tr>
                    <hp:tr>
                      <hp:tc colSpan="2"><hp:p><hp:run><hp:t>Outer B</hp:t></hp:run></hp:p></hp:tc>
                    </hp:tr>
                  </hp:tbl>
                </root>
                """,
            )

            parsed = HwpxParser().parse(path, "doc_hwpx")

        table = next(block for block in parsed.pages[0].blocks if block.type == "table")
        metadata = table.metadata
        self.assertEqual(table.text.count("\n"), 1)
        self.assertEqual(metadata["hwpx_table_row_count"], 2)
        self.assertEqual(metadata["hwpx_table_cell_count"], 3)
        self.assertEqual(metadata["hwpx_table_caption_count"], 2)
        self.assertEqual(metadata["hwpx_nested_table_count"], 1)
        self.assertEqual(metadata["hwpx_table_image_count"], 1)
        self.assertEqual(metadata["hwpx_table_note_count"], 1)
        self.assertEqual(metadata["hwpx_merged_cell_count"], 2)
        self.assertEqual(metadata["hwpx_table_direct_captions"], ["Table Caption"])
        self.assertEqual(metadata["hwpx_table_image_captions"], ["Figure Caption"])
        self.assertEqual(metadata["hwpx_table_note_snippets"], ["Cell Note"])
        self.assertEqual(metadata["hwpx_nested_table_text_snippets"], ["Nested A Nested B"])
        self.assertIsInstance(metadata["hwpx_xml_block_index"], int)
        self.assertIn("nested_table", metadata["hwpx_parser_review_flags"])
        self.assertIn("table_image", metadata["hwpx_parser_review_flags"])
        self.assertIn("table_note", metadata["hwpx_parser_review_flags"])
        self.assertIn("merged_cell", metadata["hwpx_parser_review_flags"])

    def test_complex_hwpx_table_evidence_reaches_chunk_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chunk-evidence.hwpx"
            self._write_hwpx(
                path,
                """
                <root xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">
                  <hp:p><hp:run><hp:t>제1조 목적</hp:t></hp:run></hp:p>
                  <hp:tbl>
                    <hp:caption><hp:p><hp:run><hp:t>Direct Table Caption</hp:t></hp:run></hp:p></hp:caption>
                    <hp:tr>
                      <hp:tc><hp:p><hp:run><hp:t>Header</hp:t></hp:run></hp:p></hp:tc>
                      <hp:tc>
                        <hp:tbl>
                          <hp:tr><hp:tc><hp:p><hp:run><hp:t>Nested Cell</hp:t></hp:run></hp:p></hp:tc></hp:tr>
                        </hp:tbl>
                        <hp:pic>
                          <hp:caption><hp:p><hp:run><hp:t>Image Caption</hp:t></hp:run></hp:p></hp:caption>
                        </hp:pic>
                        <hp:endNote><hp:p><hp:run><hp:t>End Note Text</hp:t></hp:run></hp:p></hp:endNote>
                      </hp:tc>
                    </hp:tr>
                  </hp:tbl>
                </root>
                """,
            )

            parsed = HwpxParser().parse(path, "doc_hwpx")
            nodes = StructureDetector().detect(parsed)
            chunks = Chunker().build_chunks(nodes, parsed, ChunkOptions(include_context_header=False))

        table_chunk = next(chunk for chunk in chunks if chunk.chunk_type == "table")
        metadata = table_chunk.metadata
        self.assertEqual(metadata["source_hwpx_table_direct_captions"], ["Direct Table Caption"])
        self.assertEqual(metadata["source_hwpx_table_image_captions"], ["Image Caption"])
        self.assertEqual(metadata["source_hwpx_table_note_snippets"], ["End Note Text"])
        self.assertEqual(metadata["source_hwpx_nested_table_text_snippets"], ["Nested Cell"])
        self.assertEqual(metadata["source_hwpx_xml_block_indices"], [4])
        self.assertIn("nested_table", metadata["source_hwpx_parser_review_flags"])

    def test_sections_are_read_in_numeric_not_lexicographic_order(self) -> None:
        def _section(label: str) -> str:
            return (
                '<root xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
                f"<hp:p><hp:run><hp:t>{label}</hp:t></hp:run></hp:p></root>"
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "multi_section.hwpx"
            with zipfile.ZipFile(path, "w") as archive:
                # Written out of order on purpose; reading order must follow the number.
                archive.writestr("Contents/section10.xml", _section("Section Ten"))
                archive.writestr("Contents/section2.xml", _section("Section Two"))
                archive.writestr("Contents/section0.xml", _section("Section Zero"))

            parsed = HwpxParser().parse(path, "doc_multi_section")

        self.assertEqual(
            ["Section Zero", "Section Two", "Section Ten"],
            [block.text for block in parsed.pages[0].blocks],
        )

    def test_unparsable_body_section_stops_instead_of_dropping_regulation_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken_section.hwpx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "Contents/section0.xml",
                    '<root xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
                    "<hp:p><hp:run><hp:t>정상 섹션</hp:t></hp:run></hp:p></root>",
                )
                archive.writestr("Contents/section1.xml", "<root><hp:p>깨진 XML")

            with self.assertRaisesRegex(
                ParserError,
                "processing stopped to prevent missing regulation content",
            ):
                HwpxParser().parse(path, "doc_broken_section")

    def test_unparsable_bodytext_section_path_also_stops_processing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken_bodytext_section.hwpx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("BodyText/Section0.xml", "<root><hp:p>malformed XML")

            with self.assertRaisesRegex(
                ParserError,
                "processing stopped to prevent missing regulation content",
            ):
                HwpxParser().parse(path, "doc_broken_bodytext_section")

    def test_unknown_body_xml_encoding_stops_with_parser_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unknown-body-encoding.hwpx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "Contents/section0.xml",
                    b'<?xml version="1.0" encoding="x-unknown"?><root/>',
                )

            with self.assertRaisesRegex(
                ParserError,
                "processing stopped to prevent missing regulation content",
            ):
                HwpxParser().parse(path, "doc_unknown_body_encoding")

    def test_parses_explicit_big_endian_body_xml(self) -> None:
        template = (
            '<?xml version="1.0" encoding="{declaration}"?>'
            '<root xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
            '<hp:p><hp:run><hp:t>{article}</hp:t></hp:run></hp:p></root>'
        )
        for encoding, declaration, article in (
            ("utf-16-be", "UTF-16-BE", "제3조 출장 절차"),
            ("utf-32-be", "UTF-32-BE", "제4조 휴가 절차"),
        ):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"body-{encoding}.hwpx"
                payload = template.format(declaration=declaration, article=article).encode(encoding)
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("Contents/section0.xml", payload)

                parsed = HwpxParser().parse(path, f"doc_body_{encoding}")

                self.assertIn(article, parsed.raw_text)
                self.assertNotIn("\ufffd", parsed.raw_text)

    def test_unparsable_non_body_xml_is_flagged_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken_metadata.hwpx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "Contents/section0.xml",
                    '<root xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
                    "<hp:p><hp:run><hp:t>정상 본문</hp:t></hp:run></hp:p></root>",
                )
                archive.writestr("Contents/header.xml", "<root><hp:p>깨진 XML")

            parsed = HwpxParser().parse(path, "doc_broken_metadata")

        self.assertIn("정상 본문", parsed.raw_text)
        self.assertEqual("medium", parsed.metadata["parser_uncertainty_risk_level"])
        self.assertIn("hwpx_section_parse_error", parsed.metadata["parser_uncertainty_flags"])
        self.assertEqual(
            "review_dropped_sections",
            parsed.metadata["parser_uncertainty_recommendation"],
        )

    def test_unknown_non_body_xml_encoding_is_flagged_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unknown-metadata-encoding.hwpx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "Contents/section0.xml",
                    '<root xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
                    "<hp:p><hp:run><hp:t>정상 본문</hp:t></hp:run></hp:p></root>",
                )
                archive.writestr(
                    "Contents/header.xml",
                    b'<?xml version="1.0" encoding="x-unknown"?><root/>',
                )

            parsed = HwpxParser().parse(path, "doc_unknown_metadata_encoding")

        self.assertIn("정상 본문", parsed.raw_text)
        self.assertIn("hwpx_section_parse_error", parsed.metadata["parser_uncertainty_flags"])
        self.assertEqual(
            "review_dropped_sections",
            parsed.metadata["parser_uncertainty_recommendation"],
        )

    def test_parses_big_endian_non_body_xml_as_reviewable_content(self) -> None:
        header_xml = (
            '<?xml version="1.0" encoding="UTF-16-BE"?>'
            '<root xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
            '<hp:p><hp:run><hp:t>검수할 머리말</hp:t></hp:run></hp:p></root>'
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big-endian-metadata.hwpx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "Contents/section0.xml",
                    '<root xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
                    '<hp:p><hp:run><hp:t>정상 본문</hp:t></hp:run></hp:p></root>',
                )
                archive.writestr("Contents/header.xml", header_xml.encode("utf-16-be"))

            parsed = HwpxParser().parse(path, "doc_big_endian_metadata")

        self.assertIn("정상 본문", parsed.raw_text)
        self.assertIn("검수할 머리말", parsed.raw_text)
        self.assertIn("hwpx_non_body_xml_content", parsed.metadata["parser_uncertainty_flags"])
        self.assertNotIn("hwpx_section_parse_error", parsed.metadata["parser_uncertainty_flags"])

    def test_rejects_dtd_and_entity_declarations_before_xml_parse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unsafe.hwpx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "Contents/section0.xml",
                    '<!DOCTYPE root [<!ENTITY injected "blocked">]>'
                    '<root><hp:p xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
                    '<hp:run><hp:t>&injected;</hp:t></hp:run></hp:p></root>',
                )

            with self.assertRaisesRegex(ParserError, "DTD and entity declarations"):
                HwpxParser().parse(path, "doc_unsafe_hwpx")

    def test_rejects_utf16_and_utf32_encoded_dtd_declarations(self) -> None:
        unsafe_xml = (
            '<?xml version="1.0" encoding="{encoding}"?>'
            '<!DOCTYPE root [<!ENTITY injected "blocked">]>'
            '<root><hp:p xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
            '<hp:run><hp:t>&injected;</hp:t></hp:run></hp:p></root>'
        )
        for encoding, declaration_encoding in (
            ("utf-16", "UTF-16"),
            ("utf-32", "UTF-32"),
        ):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"unsafe-{encoding}.hwpx"
                payload = unsafe_xml.format(encoding=declaration_encoding).encode(encoding)
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("Contents/section0.xml", payload)

                with self.assertRaisesRegex(ParserError, "DTD and entity declarations"):
                    HwpxParser().parse(path, f"doc_unsafe_{encoding}")

    def test_marks_non_body_xml_role_for_review_instead_of_mixing_silently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "header-role.hwpx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "Contents/header.xml",
                    '<root xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
                    "<hp:p><hp:run><hp:t>Header metadata</hp:t></hp:run></hp:p></root>",
                )
                archive.writestr(
                    "Contents/section0.xml",
                    '<root xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
                    "<hp:p><hp:run><hp:t>Body text</hp:t></hp:run></hp:p></root>",
                )

            parsed = HwpxParser().parse(path, "doc_header_role")

        self.assertEqual({"body": 1, "metadata": 1}, parsed.metadata["hwpx_xml_role_counts"])
        header = next(block for block in parsed.pages[0].blocks if block.text == "Header metadata")
        self.assertEqual("metadata", header.metadata["source_xml_role"])
        self.assertIn("hwpx_non_body_xml_content", parsed.metadata["parser_uncertainty_flags"])
        self.assertEqual("review_non_body_xml", parsed.metadata["parser_uncertainty_recommendation"])

    def _write_hwpx(self, path: Path, section_xml: str) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Contents/section0.xml", section_xml)


if __name__ == "__main__":
    unittest.main()
