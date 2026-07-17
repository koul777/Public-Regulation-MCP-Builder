from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.parsers.base import OCRRequiredError, ParserError
from app.parsers.factory import get_parser
from app.parsers.pdf_parser import PDFParser
from app.schemas.parsed import ParsedBlock


class PDFParserTests(unittest.TestCase):
    def test_factory_supports_pdf_extension(self) -> None:
        self.assertIsInstance(get_parser(Path("sample.pdf")), PDFParser)

    def test_factory_passes_pdf_ocr_settings(self) -> None:
        settings = Settings(
            pdf_ocr_backend="windows",
            pdf_ocr_language="ko",
            pdf_ocr_render_scale=1.5,
            pdf_ocr_timeout_seconds=30,
            pdf_ocr_max_pages=2,
        )

        parser = get_parser(Path("sample.pdf"), settings=settings)

        self.assertIsInstance(parser, PDFParser)
        self.assertEqual(parser.ocr_backend, "windows")
        self.assertEqual(parser.ocr_language, "ko")
        self.assertEqual(parser.ocr_render_scale, 1.5)
        self.assertEqual(parser.ocr_timeout_seconds, 30)
        self.assertEqual(parser.ocr_max_pages, 2)

    def test_invalid_pdf_raises_parser_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid.pdf"
            path.write_bytes(b"not a pdf")

            with self.assertRaisesRegex(ParserError, "Failed to parse PDF file"):
                PDFParser().parse(path, "doc_invalid_pdf")

    def test_text_pdf_emits_low_risk_uncertainty_report(self) -> None:
        import fitz

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "text.pdf"
            doc = fitz.open()
            page = doc.new_page(width=200, height=200)
            page.insert_text((20, 50), "Article purpose text")
            doc.save(path)
            doc.close()

            parsed = PDFParser().parse(path, "doc_text_pdf")

        self.assertEqual(parsed.metadata["parser_uncertainty_schema_version"], "reg-rag-parser-uncertainty-v1")
        self.assertEqual(parsed.metadata["parser_uncertainty_source"], "pdf")
        self.assertEqual(parsed.metadata["parser_uncertainty_risk_level"], "low")
        self.assertIn("embedded_text_extracted", parsed.metadata["parser_uncertainty_flags"])

    def test_ambiguous_two_column_page_emits_medium_uncertainty(self) -> None:
        import fitz

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ambiguous-columns.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.insert_text((40, 100), "embedded text")
            doc.save(path)
            doc.close()

            block = ParsedBlock(
                text="embedded text",
                bbox=(40, 100, 120, 110),
                metadata={
                    "raw_text": "embedded text",
                    "pdf_layout_reading_order_review": True,
                },
            )
            with patch.object(PDFParser, "_layout_line_blocks", return_value=[block]):
                parsed = PDFParser().parse(path, "doc_ambiguous_columns")

        self.assertEqual("medium", parsed.metadata["parser_uncertainty_risk_level"])
        self.assertIn(
            "pdf_two_column_reading_order_ambiguous",
            parsed.metadata["parser_uncertainty_flags"],
        )
        self.assertEqual("manual_review", parsed.metadata["parser_uncertainty_recommendation"])
        self.assertEqual([1], parsed.metadata["pdf_two_column_reading_order_ambiguous_pages"])

    def test_confirmed_two_column_page_keeps_low_uncertainty(self) -> None:
        import fitz

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "confirmed-columns.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.insert_text((40, 100), "embedded text")
            doc.save(path)
            doc.close()

            block = ParsedBlock(
                text="embedded text",
                bbox=(40, 100, 120, 110),
                metadata={
                    "raw_text": "embedded text",
                    "pdf_layout_reading_order": "column_major_two_column",
                },
            )
            with patch.object(PDFParser, "_layout_line_blocks", return_value=[block]):
                parsed = PDFParser().parse(path, "doc_confirmed_columns")

        self.assertEqual("low", parsed.metadata["parser_uncertainty_risk_level"])
        self.assertNotIn(
            "pdf_two_column_reading_order_ambiguous",
            parsed.metadata["parser_uncertainty_flags"],
        )
        self.assertEqual([], parsed.metadata["pdf_two_column_reading_order_ambiguous_pages"])

    def test_layout_line_blocks_split_wide_two_column_lines(self) -> None:
        page = _FakePdfPage(
            width=600,
            height=800,
            lines=[
                [
                    _fake_chars("left column article", x=40, y=100),
                    _fake_chars("right column item", x=340, y=100),
                ]
            ],
        )

        blocks = PDFParser()._layout_line_blocks(page, 1)

        self.assertEqual(["left column article", "right column item"], [block.text for block in blocks])
        self.assertEqual([1, 2], [block.metadata["pdf_layout_column_segment_index"] for block in blocks])
        self.assertEqual([2, 2], [block.metadata["pdf_layout_column_segment_count"] for block in blocks])

    def test_layout_line_blocks_orders_strong_two_column_page_column_major(self) -> None:
        body_lines = [
            [
                _fake_chars(f"left column line {index}", x=40, y=100 + index * 20),
                _fake_chars("R1" if index == 1 else f"right column line {index}", x=340, y=100 + index * 20),
            ]
            for index in range(1, 9)
        ]
        lines = [
            [_fake_chars("full width header", x=230, y=50)],
            *body_lines,
            [_fake_chars("full width footer", x=230, y=760)],
        ]
        page = _FakePdfPage(width=600, height=800, lines=lines)

        blocks = PDFParser()._layout_line_blocks(page, 1)

        self.assertEqual("full width header", blocks[0].text)
        self.assertEqual(
            [f"left column line {index}" for index in range(1, 9)],
            [block.text for block in blocks[1:9]],
        )
        self.assertEqual(
            ["R1", *[f"right column line {index}" for index in range(2, 9)]],
            [block.text for block in blocks[9:17]],
        )
        self.assertEqual("full width footer", blocks[-1].text)
        self.assertEqual(
            [0] + [1] * 8 + [2] * 8 + [0],
            [block.metadata["pdf_layout_column_index"] for block in blocks],
        )
        self.assertEqual("header", blocks[0].metadata["pdf_layout_reading_order_band"])
        self.assertEqual("footer", blocks[-1].metadata["pdf_layout_reading_order_band"])
        self.assertEqual((40, 120, 130, 130), blocks[1].bbox)
        self.assertEqual((340, 120, 350, 130), blocks[9].bbox)

    def test_layout_line_blocks_flags_ambiguous_mid_page_spanning_block(self) -> None:
        lines = [
            [
                _fake_chars(f"left column line {index}", x=40, y=100 + index * 20),
                _fake_chars(f"right column line {index}", x=340, y=100 + index * 20),
            ]
            for index in range(1, 9)
        ]
        lines.insert(4, [_fake_chars("full width section", x=230, y=190)])
        page = _FakePdfPage(width=600, height=800, lines=lines)

        blocks = PDFParser()._layout_line_blocks(page, 1)

        self.assertEqual("left column line 1", blocks[0].text)
        self.assertEqual("right column line 1", blocks[1].text)
        self.assertTrue(all(block.metadata["pdf_layout_reading_order_review"] for block in blocks))
        self.assertTrue(
            all(
                block.metadata["pdf_layout_reading_order"] == "visual_review_required_two_column"
                for block in blocks
            )
        )

    def test_layout_line_blocks_keeps_single_column_order_and_bboxes(self) -> None:
        page = _FakePdfPage(
            width=600,
            height=800,
            lines=[[_fake_chars(f"single column line {index}", x=40, y=100 + index * 20)] for index in range(8)],
        )

        blocks = PDFParser()._layout_line_blocks(page, 1)

        self.assertEqual([f"single column line {index}" for index in range(8)], [block.text for block in blocks])
        self.assertEqual((40, 100, 140, 110), blocks[0].bbox)
        self.assertNotIn("pdf_layout_reading_order", blocks[0].metadata)

    def test_layout_line_blocks_does_not_column_reorder_dense_table_page(self) -> None:
        page = _FakePdfPage(
            width=600,
            height=800,
            lines=[
                [
                    _fake_chars(f"left table cell {index}", x=40, y=100 + index * 20),
                    _fake_chars(f"right table cell {index}", x=340, y=100 + index * 20),
                ]
                for index in range(1, 9)
            ],
            drawings=[{} for _ in range(16)],
        )

        blocks = PDFParser()._layout_line_blocks(page, 1)

        self.assertEqual("left table cell 1", blocks[0].text)
        self.assertEqual("right table cell 1", blocks[1].text)
        self.assertEqual("left table cell 2", blocks[2].text)
        self.assertNotIn("pdf_layout_reading_order", blocks[0].metadata)

    def test_footnote_marker_references_count_unique_roman_markers_without_bottom_notes(self) -> None:
        page = _FakePdfPage(
            width=600,
            height=800,
            lines=[
                [
                    _fake_chars("보상대상 ⅰ 본문 ⅲ 계속 ⅴ", x=40, y=160),
                    _fake_chars("다른 열 ⅰ 중복", x=340, y=160),
                ]
            ],
        )

        references = PDFParser()._footnote_marker_references(page, 24)

        self.assertEqual(1, len(references))
        self.assertEqual(24, references[0]["source_page"])
        self.assertEqual(3, references[0]["marker_count"])
        self.assertEqual(["ⅰ", "ⅲ", "ⅴ"], references[0]["markers"])
        self.assertEqual(0, references[0]["bottom_marker_count"])
        self.assertEqual(3, references[0]["body_marker_count"])

    def test_blank_pdf_raises_ocr_required_error(self) -> None:
        import fitz

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blank.pdf"
            doc = fitz.open()
            doc.new_page(width=200, height=200)
            doc.save(path)
            doc.close()

            with self.assertRaisesRegex(OCRRequiredError, "OCR may be required") as ctx:
                PDFParser().parse(path, "doc_blank_pdf")

        self.assertTrue(ctx.exception.ocr_required)
        self.assertEqual(ctx.exception.page_count, 1)
        self.assertEqual(ctx.exception.file_type, "pdf")
        self.assertEqual(ctx.exception.uncertainty_report["schema_version"], "reg-rag-parser-uncertainty-v1")
        self.assertEqual(ctx.exception.uncertainty_report["risk_level"], "high")
        self.assertIn("ocr_required", ctx.exception.uncertainty_report["flags"])

    def test_blank_pdf_uses_windows_ocr_backend_when_enabled(self) -> None:
        import fitz

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scanned.pdf"
            doc = fitz.open()
            doc.new_page(width=200, height=200)
            doc.save(path)
            doc.close()

            with patch.object(
                PDFParser,
                "_extract_windows_ocr_pages",
                return_value=["제1조(목적) 이 기준은 수의계약 집행기준을 정한다."],
            ) as ocr_pages:
                parsed = PDFParser(ocr_backend="windows", ocr_render_scale=1, ocr_timeout_seconds=5).parse(
                    path,
                    "doc_scanned_pdf",
                )

        ocr_pages.assert_called_once()
        self.assertIn("제1조(목적)", parsed.raw_text)
        self.assertEqual(parsed.metadata["ocr_backend"], "windows")
        self.assertEqual(parsed.metadata["ocr_language"], "ko")
        self.assertEqual(parsed.metadata["parser_uncertainty_risk_level"], "medium")
        self.assertIn("ocr_text_extracted", parsed.metadata["parser_uncertainty_flags"])
        self.assertEqual(parsed.pages[0].blocks[0].metadata["ocr_backend"], "windows")


class _FakeRect:
    def __init__(self, width: float, height: float) -> None:
        self.width = width
        self.height = height


class _FakePdfPage:
    def __init__(
        self,
        *,
        width: float,
        height: float,
        lines: list[list[list[dict]]],
        drawings: list[dict] | None = None,
    ) -> None:
        self.rect = _FakeRect(width, height)
        self._lines = lines
        self._drawings = drawings or []

    def get_text(self, mode: str):
        if mode != "rawdict":
            raise AssertionError(f"unexpected mode: {mode}")
        return {
            "blocks": [
                {
                    "lines": [
                        {
                            "spans": [
                                {
                                    "size": 10,
                                    "font": "Fake",
                                    "chars": [char for segment in line for char in segment],
                                }
                            ]
                        }
                        for line in self._lines
                    ]
                }
            ]
        }

    def get_drawings(self) -> list[dict]:
        return self._drawings


def _fake_chars(text: str, *, x: float, y: float, width: float = 5.0) -> list[dict]:
    chars = []
    cursor = x
    for char in text:
        if char == " ":
            cursor += width
            continue
        chars.append({"c": char, "bbox": (cursor, y, cursor + width, y + 10)})
        cursor += width
    return chars


if __name__ == "__main__":
    unittest.main()
