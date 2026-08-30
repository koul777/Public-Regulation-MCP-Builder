from __future__ import annotations

import unittest
import zlib
import tempfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from app.parsers.base import ParserError
from app.parsers.factory import get_parser
from app.parsers.hwp_parser import (
    HWP_LEGACY_EXTRACTION_MODE,
    HWP_TAG_PARA_TEXT,
    HWPML_EXTRACTION_MODE,
    HWP_DROPPED_EQUATION_DIAGNOSTIC,
    HWP_INLINE_OBJECT_DIAGNOSTIC,
    HWP_TRUNCATED_RECORD_DIAGNOSTIC,
    HWP_UTF16_DECODE_DIAGNOSTIC,
    HwpParser,
)


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

    def test_reports_malformed_utf16_payload_instead_of_silently_decoding(self) -> None:
        parser = HwpParser()
        section = hwp_record(HWP_TAG_PARA_TEXT, "valid".encode("utf-16le")) + hwp_record(HWP_TAG_PARA_TEXT, b"\x00")
        diagnostics: dict[str, int] = {}

        self.assertEqual(parser._paragraph_texts(section, diagnostics=diagnostics), ["valid"])
        self.assertEqual(diagnostics[HWP_UTF16_DECODE_DIAGNOSTIC], 1)

    def test_reports_truncated_record_header_or_payload(self) -> None:
        parser = HwpParser()
        extended_header = (HWP_TAG_PARA_TEXT | (0xFFF << 20)).to_bytes(4, byteorder="little") + b"\x01"
        diagnostics: dict[str, int] = {}

        self.assertEqual(list(parser._record_infos(extended_header, diagnostics=diagnostics)), [])
        self.assertEqual(diagnostics[HWP_TRUNCATED_RECORD_DIAGNOSTIC], 1)

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

    def test_preserves_legal_hanja_prefix_before_korean_text(self) -> None:
        parser = HwpParser()

        self.assertEqual(parser._clean_text("施行 規則 개정"), "施行 規則 개정")
        self.assertEqual(parser._clean_text("職務 遂行 중 발생한"), "職務 遂行 중 발생한")

    def test_strips_unregistered_ascii_packed_hwp_mojibake_prefix(self) -> None:
        parser = HwpParser()

        self.assertEqual(parser._clean_text("晦潬 敭湵 공공기관 지침"), "공공기관 지침")

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

    def test_parses_utf16_hwpml_without_replacement_characters(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-16"?>
<HWPML Version="2.1">
  <HEAD><DOCSUMMARY><TITLE>합성 복무규정</TITLE></DOCSUMMARY></HEAD>
  <BODY><P><TEXT><CHAR>제1조 목적과 적용 범위</CHAR></TEXT></P></BODY>
</HWPML>
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "utf16-hwpml.hwp"
            path.write_bytes(xml.encode("utf-16"))

            parsed = HwpParser().parse(path, "doc_utf16_hwpml")

        self.assertEqual("합성 복무규정", parsed.document_name)
        self.assertIn("제1조 목적과 적용 범위", parsed.raw_text)
        self.assertNotIn("\ufffd", parsed.raw_text)

    def test_parses_utf32_hwpml_without_replacement_characters(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-32"?>
<HWPML Version="2.1">
  <HEAD><DOCSUMMARY><TITLE>합성 복무규정</TITLE></DOCSUMMARY></HEAD>
  <BODY><P><TEXT><CHAR>제2조 휴가 신청 절차</CHAR></TEXT></P></BODY>
</HWPML>
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "utf32-hwpml.hwp"
            path.write_bytes(xml.encode("utf-32"))

            parsed = HwpParser().parse(path, "doc_utf32_hwpml")

        self.assertEqual("합성 복무규정", parsed.document_name)
        self.assertIn("제2조 휴가 신청 절차", parsed.raw_text)
        self.assertNotIn("\ufffd", parsed.raw_text)

    def test_parses_explicit_big_endian_hwpml_without_replacement_characters(self) -> None:
        template = """<?xml version="1.0" encoding="{declaration}"?>
<HWPML Version="2.1">
  <HEAD><DOCSUMMARY><TITLE>합성 복무규정</TITLE></DOCSUMMARY></HEAD>
  <BODY><P><TEXT><CHAR>{article}</CHAR></TEXT></P></BODY>
</HWPML>
"""
        for encoding, declaration, article in (
            ("utf-16-be", "UTF-16-BE", "제3조 출장 절차"),
            ("utf-32-be", "UTF-32-BE", "제4조 휴가 절차"),
        ):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"{encoding}-hwpml.hwp"
                path.write_bytes(
                    template.format(declaration=declaration, article=article).encode(encoding)
                )

                parsed = HwpParser().parse(path, f"doc_{encoding}_hwpml")

                self.assertEqual("합성 복무규정", parsed.document_name)
                self.assertIn(article, parsed.raw_text)
                self.assertNotIn("\ufffd", parsed.raw_text)

    def test_rejects_truncated_utf32_hwpml_without_replacement_decode(self) -> None:
        xml = (
            '<?xml version="1.0" encoding="UTF-32"?>'
            '<HWPML><BODY><P><TEXT><CHAR>제1조 목적</CHAR></TEXT></P></BODY></HWPML>'
        )
        payload = xml.encode("utf-32")[:-1]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "truncated-utf32-hwpml.hwp"
            path.write_bytes(payload)

            with self.assertRaisesRegex(ParserError, "not valid XML"):
                HwpParser().parse(path, "doc_truncated_utf32_hwpml")

    def test_rejects_hwpml_dtd_and_entity_declarations(self) -> None:
        xml = """<?xml version=\"1.0\"?>
<!DOCTYPE HWPML [<!ENTITY injected \"blocked\">]>
<HWPML><BODY><P><TEXT><CHAR>&injected;</CHAR></TEXT></P></BODY></HWPML>
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unsafe.hwp"
            path.write_text(xml, encoding="utf-8")

            with self.assertRaisesRegex(ParserError, "DTD and entity declarations"):
                HwpParser().parse(path, "doc_unsafe_hwpml")

    def test_rejects_utf16_hwpml_dtd_and_entity_declarations(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-16"?>
<!DOCTYPE HWPML [<!ENTITY injected "blocked">]>
<HWPML><BODY><P><TEXT><CHAR>&injected;</CHAR></TEXT></P></BODY></HWPML>
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unsafe-utf16.hwp"
            path.write_bytes(xml.encode("utf-16"))

            with self.assertRaisesRegex(ParserError, "DTD and entity declarations"):
                HwpParser().parse(path, "doc_unsafe_utf16_hwpml")

    def test_rejects_utf32_hwpml_dtd_and_entity_declarations(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-32"?>
<!DOCTYPE HWPML [<!ENTITY injected "blocked">]>
<HWPML><BODY><P><TEXT><CHAR>&injected;</CHAR></TEXT></P></BODY></HWPML>
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unsafe-utf32.hwp"
            path.write_bytes(xml.encode("utf-32"))

            with self.assertRaisesRegex(ParserError, "DTD and entity declarations"):
                HwpParser().parse(path, "doc_unsafe_utf32_hwpml")

    def test_rejects_utf16_big_endian_hwpml_dtd_and_entity_declarations(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-16-BE"?>
<!DOCTYPE HWPML [<!ENTITY injected "blocked">]>
<HWPML><BODY><P><TEXT><CHAR>&injected;</CHAR></TEXT></P></BODY></HWPML>
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unsafe-utf16-be.hwp"
            path.write_bytes(xml.encode("utf-16-be"))

            with self.assertRaisesRegex(ParserError, "DTD and entity declarations"):
                HwpParser().parse(path, "doc_unsafe_utf16_be_hwpml")

    def test_rejects_unknown_hwpml_declared_encoding(self) -> None:
        payload = (
            b'<?xml version="1.0" encoding="x-unknown"?>'
            b'<HWPML><BODY><P><TEXT><CHAR>body</CHAR></TEXT></P></BODY></HWPML>'
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unknown-encoding.hwp"
            path.write_bytes(payload)

            with self.assertRaisesRegex(ParserError, "not valid XML"):
                HwpParser().parse(path, "doc_unknown_hwpml_encoding")

    def test_rejects_hwpml_payload_past_document_limit(self) -> None:
        xml = '<?xml version="1.0"?><HWPML><BODY><P><TEXT><CHAR>oversized</CHAR></TEXT></P></BODY></HWPML>'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "oversized.hwp"
            path.write_text(xml, encoding="utf-8")

            with self.assertRaisesRegex(ParserError, "HWPML XML exceeds"):
                HwpParser(max_decompressed_document_bytes=16).parse(path, "doc_oversized_hwpml")


class HwpInlineControlRecordTests(unittest.TestCase):
    """문단 중간의 개체 레코드를 글자로 읽지 않는다.

    HWP5에서 문단 안의 개체는 8글자 레코드다: 시작 표시 + 컨트롤 ID 2글자 +
    정보 4글자 + 끝 표시. 이걸 글자로 읽으면 ID가 본문에 새어 나온다. 실제
    성과급 세칙에서 "기본연봉 × 지급률"의 곱셈 기호 자리에 있던 수식 개체가
    ``敤敱``(=eqed)로 찍혔고, 그 상태로 색인됐다.
    """

    @staticmethod
    def _record(control_id: str) -> str:
        """8글자짜리 확장 컨트롤 레코드를 만든다(ID는 뒤집혀 저장된다)."""
        marker = control_id.encode("ascii")[::-1].decode("utf-16-le")
        return "" + marker + "\x00\x00\x00\x00" + ""

    def test_equation_object_id_does_not_leak_into_the_text(self) -> None:
        text = "1. 원장: 기본연봉 " + self._record("eqed") + " 경영평가 지급률"

        cleaned, objects, equations = HwpParser._strip_control_records(text)

        self.assertNotIn("敤", cleaned)
        self.assertNotIn("敱", cleaned)
        self.assertEqual("1. 원장: 기본연봉   경영평가 지급률", cleaned)
        self.assertEqual(1, objects)
        self.assertEqual(1, equations)

    def test_layout_objects_are_removed_without_being_called_content_loss(self) -> None:
        """표·단·머리말 개체는 실측 26건 전부에 있었다. 세면 신호가 되지 않는다."""
        for control_id in ("tbl ", "cold", "gso ", "head", "secd", "pgnp"):
            with self.subTest(control_id=control_id):
                cleaned, objects, equations = HwpParser._strip_control_records(
                    "제1조(목적) " + self._record(control_id) + " 정한다."
                )

                self.assertEqual("제1조(목적)   정한다.", cleaned)
                self.assertEqual(1, objects)
                self.assertEqual(0, equations)

    def test_plain_text_is_untouched(self) -> None:
        text = "제1조(목적) 이 규정은 인사에 관한 사항을 정한다."

        self.assertEqual((text, 0, 0), HwpParser._strip_control_records(text))

    def test_line_break_controls_stay_line_breaks(self) -> None:
        cleaned, _objects, _equations = HwpParser._strip_control_records("제1조\r\n제2조")

        self.assertEqual("제1조\n\n제2조", cleaned)

    def test_a_truncated_record_at_the_end_does_not_crash(self) -> None:
        cleaned, objects, equations = HwpParser._strip_control_records("본문 敤")

        self.assertEqual("본문  ", cleaned)
        self.assertEqual(1, objects)
        self.assertEqual(0, equations)

    def test_dropped_equations_raise_the_parser_risk_level(self) -> None:
        parser = HwpParser()
        clean = parser._diagnostic_flags({})
        with_equation = parser._diagnostic_flags({HWP_DROPPED_EQUATION_DIAGNOSTIC: 2})
        with_layout_only = parser._diagnostic_flags({HWP_INLINE_OBJECT_DIAGNOSTIC: 30})

        self.assertEqual([], clean)
        self.assertIn("hwp_equation_dropped", with_equation)
        self.assertEqual([], with_layout_only)


if __name__ == "__main__":
    unittest.main()
