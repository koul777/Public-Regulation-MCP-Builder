from __future__ import annotations

import unittest
import zlib
import tempfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from app.parsers.base import ParserError
from app.parsers.factory import get_parser
from app.parsers.hwp_parser import HWP_LEGACY_EXTRACTION_MODE, HWP_TAG_PARA_TEXT, HWPML_EXTRACTION_MODE, HwpParser


def hwp_record(tag_id: int, payload: bytes) -> bytes:
    size = len(payload)
    if size < 0xFFF:
        header = tag_id | (0 << 10) | (size << 20)
        return header.to_bytes(4, byteorder="little") + payload
    header = tag_id | (0 << 10) | (0xFFF << 20)
    return header.to_bytes(4, byteorder="little") + size.to_bytes(4, byteorder="little") + payload


class HwpParserTests(unittest.TestCase):
    def test_factory_supports_legacy_hwp_extension(self) -> None:
        self.assertIsInstance(get_parser(Path("sample.hwp")), HwpParser)

    def test_factory_passes_hwp_decompressed_section_limit(self) -> None:
        parser = get_parser(
            Path("sample.hwp"),
            settings=SimpleNamespace(
                hwp_max_decompressed_section_mb=3,
                hwp_max_decompressed_document_mb=7,
            ),
        )

        self.assertIsInstance(parser, HwpParser)
        self.assertEqual(parser.max_decompressed_section_bytes, 3 * 1024 * 1024)
        self.assertEqual(parser.max_decompressed_document_bytes, 7 * 1024 * 1024)

    def test_extracts_paragraph_text_records(self) -> None:
        parser = HwpParser()
        payload = "제1조 목적\n본문".encode("utf-16le")
        section = hwp_record(10, b"ignored") + hwp_record(HWP_TAG_PARA_TEXT, payload)

        self.assertEqual(parser._paragraph_texts(section), ["제1조 목적\n본문"])

    def test_extracts_extended_size_records(self) -> None:
        parser = HwpParser()
        payload = ("가" * 3000).encode("utf-16le")
        section = hwp_record(HWP_TAG_PARA_TEXT, payload)

        self.assertEqual(parser._paragraph_texts(section), ["가" * 3000])

    def test_decompresses_raw_deflate_section(self) -> None:
        parser = HwpParser()
        section = hwp_record(HWP_TAG_PARA_TEXT, "본문".encode("utf-16le"))
        compressor = zlib.compressobj(wbits=-15)
        compressed = compressor.compress(section) + compressor.flush()

        decompressed = parser._decompress_section(compressed, "BodyText/Section0")

        self.assertEqual(decompressed, section)

    def test_decompresses_zlib_wrapped_section(self) -> None:
        parser = HwpParser()
        section = hwp_record(HWP_TAG_PARA_TEXT, "Body".encode("utf-16le"))

        decompressed = parser._decompress_section(
            zlib.compress(section),
            "BodyText/Section0",
        )

        self.assertEqual(decompressed, section)

    def test_rejects_section_that_expands_past_configured_limit(self) -> None:
        parser = HwpParser(max_decompressed_section_bytes=64)
        compressor = zlib.compressobj(wbits=-15)
        compressed = compressor.compress(b"A" * 4096) + compressor.flush()

        with self.assertRaisesRegex(
            ParserError,
            r"exceeds the configured decompressed size limit \(64 bytes\): BodyText/Section9",
        ):
            parser._decompress_section(compressed, "BodyText/Section9")

    def test_rejects_uncompressed_section_past_configured_limit(self) -> None:
        parser = HwpParser(max_decompressed_section_bytes=64)

        with self.assertRaisesRegex(
            ParserError,
            r"exceeds the configured decompressed size limit \(64 bytes\): BodyText/Section3",
        ):
            parser._enforce_section_size_limit(b"A" * 65, "BodyText/Section3")

    def test_rejects_total_sections_past_configured_document_limit(self) -> None:
        parser = HwpParser(max_decompressed_document_bytes=128)

        with self.assertRaisesRegex(
            ParserError,
            r"document sections exceed the configured total decompressed size limit \(128 bytes\)",
        ):
            parser._enforce_document_size_limit(129)

    def test_rejects_non_positive_decompressed_section_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be greater than zero"):
            HwpParser(max_decompressed_section_bytes=0)

        with self.assertRaisesRegex(ValueError, "must be greater than zero"):
            HwpParser(max_decompressed_document_bytes=0)

    def test_legacy_hwp_parse_marks_paragraph_only_extraction_mode(self) -> None:
        class FakeStream:
            def __init__(self, payload: bytes) -> None:
                self.payload = payload

            def read(self) -> bytes:
                return self.payload

        class FakeOle:
            def __enter__(self) -> "FakeOle":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def exists(self, name: str) -> bool:
                return name == "FileHeader"

            def openstream(self, name: str) -> FakeStream:
                if name == "FileHeader":
                    return FakeStream(bytes(40))
                return FakeStream(hwp_record(HWP_TAG_PARA_TEXT, "Article text".encode("utf-16le")))

            def listdir(self, streams: bool = True, storages: bool = False) -> list[list[str]]:
                return [["BodyText", "Section0"]]

        fake_olefile = SimpleNamespace(isOleFile=lambda path: True, OleFileIO=lambda path: FakeOle())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.hwp"
            path.write_bytes(b"HWP")
            with patch("app.parsers.hwp_parser.olefile", fake_olefile):
                parsed = HwpParser().parse(path, "doc_hwp")

        block_metadata = parsed.pages[0].blocks[0].metadata
        self.assertEqual(parsed.metadata["hwp_extraction_mode"], HWP_LEGACY_EXTRACTION_MODE)
        self.assertFalse(parsed.metadata["hwp_native_table_geometry"])
        self.assertEqual(parsed.metadata["parser_uncertainty_schema_version"], "reg-rag-parser-uncertainty-v1")
        self.assertEqual(parsed.metadata["parser_uncertainty_source"], "hwp")
        self.assertEqual(parsed.metadata["parser_uncertainty_risk_level"], "medium")
        self.assertIn("native_table_geometry_unavailable", parsed.metadata["parser_uncertainty_flags"])
        self.assertEqual(block_metadata["hwp_extraction_mode"], HWP_LEGACY_EXTRACTION_MODE)
        self.assertEqual(block_metadata["hwp_stream"], "BodyText/Section0")
        self.assertEqual(block_metadata["section_index"], 1)
        self.assertFalse(block_metadata["hwp_native_table_geometry"])

    def test_cleans_hwp_control_characters(self) -> None:
        parser = HwpParser()

        self.assertEqual(parser._clean_text("본문\x00\x01  내용\r\n끝"), "본문 내용\n끝")

    def test_strips_short_hwp_mojibake_prefix_before_korean_title(self) -> None:
        parser = HwpParser()

        self.assertEqual(parser._clean_text("捤獥 汤捯 湰灧 공공기관 지침"), "공공기관 지침")

    def test_strips_standalone_hwp_mojibake_lines_when_other_text_exists(self) -> None:
        parser = HwpParser()

        self.assertEqual(parser._clean_text("捤獥 汤捯 氠瑢\n공공기관 지침"), "공공기관 지침")
        self.assertEqual(parser._clean_text("2021. 7. 28.\n桤灧"), "2021. 7. 28.")
        self.assertEqual(parser._clean_text("汤捯 □ 경영평가 성과급\n湯慴 (예시) 성과급 등급\n湯湷"), "□ 경영평가 성과급\n(예시) 성과급 등급")
        self.assertTrue(parser._looks_like_hwp_mojibake_block("捤獥 汤捯 氠瑢"))
        self.assertTrue(parser._looks_like_hwp_mojibake_block("桤灧"))
        self.assertTrue(parser._looks_like_hwp_mojibake_block("湯湷"))
        self.assertFalse(parser._looks_like_hwp_mojibake_block("공공기관 지침"))

    def test_parses_hwpml_xml_with_hwp_extension(self) -> None:
        xml = """<?xml version="1.0" encoding="utf-8"?>
<HWPML Version="2.1">
  <HEAD><DOCSUMMARY><TITLE>Sample Regulation</TITLE></DOCSUMMARY></HEAD>
  <BODY>
    <P><TEXT><CHAR>Sample Regulation</CHAR></TEXT></P>
    <P><TEXT><CHAR>Article 1 Purpose</CHAR></TEXT></P>
  </BODY>
</HWPML>
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.hwp"
            path.write_text(xml, encoding="utf-8")

            parsed = HwpParser().parse(path, "doc_hwpml")

        self.assertEqual(parsed.file_type, "hwp")
        self.assertEqual(parsed.document_name, "Sample Regulation")
        self.assertEqual(parsed.metadata["hwp_encoding"], "hwpml")
        self.assertEqual(parsed.metadata["hwp_extraction_mode"], HWPML_EXTRACTION_MODE)
        self.assertFalse(parsed.metadata["hwp_native_table_geometry"])
        self.assertEqual(parsed.metadata["parser_uncertainty_schema_version"], "reg-rag-parser-uncertainty-v1")
        self.assertEqual(parsed.metadata["parser_uncertainty_source"], "hwp")
        self.assertEqual(parsed.metadata["parser_uncertainty_risk_level"], "medium")
        self.assertIn("hwpml_xml_text_only", parsed.metadata["parser_uncertainty_flags"])
        self.assertEqual(parsed.pages[0].blocks[0].metadata["hwp_extraction_mode"], HWPML_EXTRACTION_MODE)
        self.assertEqual([block.text for block in parsed.pages[0].blocks], ["Sample Regulation", "Article 1 Purpose"])


if __name__ == "__main__":
    unittest.main()
