from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.core.config import Settings
from app.parsers.base import OCRRequiredError, ParserError
from app.parsers.factory import get_parser
from app.parsers.pdf_parser import PDFParser
from app.schemas.parsed import ParsedBlock
from app.utils.fitz_compat import fitz


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

    def test_mixed_text_and_image_only_pages_emit_missing_content_review_signal(self) -> None:
        class FakePdfDocument:
            def __enter__(self):
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def __iter__(self):
                return iter(
                    [
                        _FakePdfPage(
                            width=600,
                            height=800,
                            lines=[[_fake_chars("text page", x=40, y=100)]],
                        ),
                        _ImageOnlyFakePdfPage(width=600, height=800),
                    ]
                )

        fake_fitz = SimpleNamespace(open=lambda path: FakePdfDocument())
        with patch("app.utils.fitz_compat.fitz", fake_fitz):
            parsed = PDFParser().parse(Path("mixed.pdf"), "doc_mixed_pdf")

        self.assertEqual([2], parsed.metadata["blank_pages"])
        self.assertEqual([2], parsed.metadata["missing_content_pages"])
        self.assertEqual("medium", parsed.metadata["parser_uncertainty_risk_level"])
        self.assertIn("pdf_missing_content_pages", parsed.metadata["parser_uncertainty_flags"])
        self.assertEqual("review_ocr_text", parsed.metadata["parser_uncertainty_recommendation"])

    def test_text_page_with_embedded_image_emits_image_review_signal(self) -> None:
        class _TextImagePage(_FakePdfPage):
            def get_images(self, *, full: bool = False) -> list[tuple]:
                return [("image-x",)]

        class FakePdfDocument:
            def __enter__(self):
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def __iter__(self):
                return iter([_TextImagePage(width=600, height=800, lines=[[_fake_chars("text", x=40, y=100)]])])

        fake_fitz = SimpleNamespace(open=lambda path: FakePdfDocument())
        with patch("app.utils.fitz_compat.fitz", fake_fitz):
            parsed = PDFParser().parse(Path("text-image.pdf"), "doc_text_image_pdf")

        self.assertEqual([1], parsed.metadata["pdf_embedded_image_pages"])
        self.assertIn("pdf_embedded_images_detected", parsed.metadata["parser_uncertainty_flags"])
        self.assertEqual("review_embedded_images", parsed.metadata["parser_uncertainty_recommendation"])
        self.assertEqual("medium", parsed.metadata["parser_uncertainty_risk_level"])

    def test_parse_snapshots_raw_layout_and_drawings_once_per_page(self) -> None:
        page = _FakePdfPage(
            width=600,
            height=800,
            lines=[[_fake_chars("Article purpose \u2170", x=40, y=100)]],
        )

        class FakePdfDocument:
            def __enter__(self):
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def __iter__(self):
                return iter([page])

        fake_fitz = SimpleNamespace(open=lambda path: FakePdfDocument())
        with patch("app.utils.fitz_compat.fitz", fake_fitz):
            parsed = PDFParser().parse(Path("snapshot.pdf"), "doc_snapshot_pdf")

        self.assertIn("Article", parsed.raw_text)
        self.assertEqual(1, page.get_text_calls["rawdict"])
        self.assertEqual(1, page.get_drawings_calls)
        self.assertEqual(
            ["\u2170"],
            parsed.metadata["pdf_footnote_marker_references"][0]["markers"],
        )

    def test_visual_char_groups_reuses_sorted_y_values_without_recomputing_median(self) -> None:
        page = _FakePdfPage(
            width=600,
            height=800,
            lines=[
                [[
                    {"c": "B", "bbox": (45.0, 100.0, 50.0, 110.0)},
                    {"c": "A", "bbox": (40.0, 101.0, 45.0, 111.0)},
                ]],
                [[
                    {"c": "D", "bbox": (45.0, 120.0, 50.0, 130.0)},
                    {"c": "C", "bbox": (40.0, 121.0, 45.0, 131.0)},
                ]],
            ],
        )

        with patch(
            "app.parsers.pdf_parser.median",
            side_effect=AssertionError("visual grouping must not sort the same y values again"),
        ):
            groups = PDFParser()._visual_char_groups(page)

        self.assertEqual(
            [["A", "B"], ["C", "D"]],
            [[str(item["c"]) for item in group] for group in groups],
        )

    def test_cluster_positions_reuses_sorted_values_without_recomputing_median(self) -> None:
        with patch(
            "app.parsers.pdf_parser.median",
            side_effect=AssertionError("clustering must reuse sorted values"),
        ):
            clusters = PDFParser()._cluster_positions([10.0, 11.0, 11.5, 50.0, 51.0])

        self.assertEqual([11.0, 50.5], clusters)

    def test_layout_line_blocks_passes_ordered_segments_to_text_rebuilder(self) -> None:
        page = _FakePdfPage(
            width=600,
            height=800,
            lines=[[_fake_chars("ordered segment", x=40, y=100)]],
        )
        ordered_flags: list[bool] = []
        parser = PDFParser()
        original = PDFParser._text_from_chars

        def _record_ordered(self, chars, *, already_ordered: bool = False):  # type: ignore[no-untyped-def]
            ordered_flags.append(already_ordered)
            return original(self, chars, already_ordered=already_ordered)

        with patch.object(PDFParser, "_text_from_chars", _record_ordered):
            blocks = parser._layout_line_blocks(page, 1)

        self.assertTrue(blocks)
        self.assertTrue(ordered_flags)
        self.assertTrue(all(ordered_flags))

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

    def test_layout_line_blocks_keep_encoded_narrow_space_glyphs_as_word_spaces(self) -> None:
        # 10pt 한글 glyph 사이의 3.3pt 공백 glyph는 간격 휴리스틱 기준(3.5pt)보다 좁다.
        page = _FakePdfPage(
            width=600,
            height=800,
            lines=[[_fake_chars_with_glyph_spaces("제1조(목적) 이 규정은 복무를 정한다.", x=40, y=100)]],
        )

        blocks = PDFParser()._layout_line_blocks(page, 1)

        self.assertEqual(["제1조(목적) 이 규정은 복무를 정한다."], [block.text for block in blocks])
        self.assertEqual("제1조(목적) 이 규정은 복무를 정한다.", blocks[0].metadata["raw_text"])

    def test_layout_line_blocks_do_not_infer_space_without_encoded_glyph(self) -> None:
        # 같은 3.3pt 간격이라도 공백 glyph가 없으면 한글 단어 사이에 공백을 만들지 않는다.
        page = _FakePdfPage(
            width=600,
            height=800,
            lines=[[_fake_chars_with_glyph_spaces("가상공단 복무규정", x=40, y=100, space_char=None)]],
        )

        blocks = PDFParser()._layout_line_blocks(page, 1)

        self.assertEqual(["가상공단복무규정"], [block.text for block in blocks])

    def test_layout_line_blocks_ignore_synthetic_or_detached_space_glyphs(self) -> None:
        synthetic = _fake_chars_with_glyph_spaces("가상공단 복무규정", x=40, y=100, synthetic=True)
        detached = _fake_chars_with_glyph_spaces("직원복무 운영기준", x=40, y=140)
        # 스트림상 직전 glyph 뒤에 오지만 다른 위치에 그려진 공백 glyph는 근거가 아니다.
        detached[4] = {**detached[4], "bbox": (400.0, 140.0, 403.3, 150.0)}
        page = _FakePdfPage(width=600, height=800, lines=[[synthetic], [detached]])

        blocks = PDFParser()._layout_line_blocks(page, 1)

        self.assertEqual(["가상공단복무규정", "직원복무운영기준"], [block.text for block in blocks])

    def test_space_glyph_evidence_follows_char_through_visual_sorting(self) -> None:
        chars = _fake_chars_with_glyph_spaces("가나 다라", x=40, y=100)
        # 스트림 순서와 y 정렬 순서가 다른 경우에도 근거가 해당 문자와 함께 이동해야 한다.
        chars[1] = {**chars[1], "bbox": (50.0, 99.4, 60.0, 109.4)}
        chars[2] = {**chars[2], "bbox": (60.0, 99.4, 63.3, 109.4)}
        page = _FakePdfPage(width=600, height=800, lines=[[chars]])

        groups = PDFParser()._visual_char_groups(page)

        self.assertEqual([["가", "나", "다", "라"]], [[str(item["c"]) for item in group] for group in groups])
        self.assertEqual("가나 다라", PDFParser()._text_from_chars(groups[0], already_ordered=True))

    def test_pdf_with_narrow_cjk_space_glyphs_keeps_word_spaces_across_pages_and_table(self) -> None:
        try:
            cjk_font = fitz.Font("cjk")
        except Exception as exc:  # pragma: no cover - depends on the PyMuPDF build
            self.skipTest(f"PyMuPDF built-in CJK font unavailable: {type(exc).__name__}")
        if cjk_font.text_length(" ", 10.5) >= 10.5 * 0.35:
            self.skipTest("PyMuPDF built-in CJK font does not have a narrow space glyph")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "narrow-space.pdf"
            doc = fitz.open()
            for page_index, lines in enumerate(_NARROW_SPACE_PAGES):
                page = doc.new_page(width=595, height=842)
                page.insert_font(fontname="K0", fontbuffer=cjk_font.buffer)
                y = 80.0
                for line in lines:
                    page.insert_text((56, y), line, fontname="K0", fontsize=10.5)
                    y += 20
                if page_index == 0:
                    word_width = cjk_font.text_length("가상공단", 10.5)
                    page.insert_text((56, y), "가상공단", fontname="K0", fontsize=10.5)
                    page.insert_text((56 + word_width + 2, y), "복무규정", fontname="K0", fontsize=10.5)
                    y += 30
                    for row in _NARROW_SPACE_TABLE:
                        for x, cell in zip(_NARROW_SPACE_TABLE_COLUMNS, row):
                            page.insert_text((x, y), cell, fontname="K0", fontsize=10.5)
                        y += 20
            doc.subset_fonts()
            doc.save(path, garbage=3, deflate=True)
            doc.close()
            _skip_unless_rawdict_marks_encoded_spaces(self, path)

            parsed = PDFParser().parse(path, "doc_narrow_space_pdf")

        self.assertEqual(_expected_narrow_space_pages(), [[block.text for block in page.blocks] for page in parsed.pages])
        self.assertIn("제1조(목적) 이 규정은 직원의 복무에 관한 사항을 정한다.", parsed.raw_text)
        self.assertEqual("low", parsed.metadata["parser_uncertainty_risk_level"])

    def test_reportlab_cid_font_pdf_keeps_third_em_word_spaces(self) -> None:
        try:
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.cidfonts import UnicodeCIDFont
            from reportlab.pdfgen import canvas
        except ImportError:
            self.skipTest("reportlab is not installed")
        font_name = "HYSMyeongJo-Medium"
        try:
            pdfmetrics.registerFont(UnicodeCIDFont(font_name))
        except Exception as exc:  # pragma: no cover - depends on reportlab CID data
            self.skipTest(f"reportlab CID font unavailable: {type(exc).__name__}")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cid-space.pdf"
            pdf = canvas.Canvas(str(path))
            for lines in _NARROW_SPACE_PAGES:
                y = 760
                for line in lines:
                    pdf.setFont(font_name, 10.5)
                    pdf.drawString(56, y, line)
                    y -= 20
                pdf.showPage()
            pdf.save()
            _skip_unless_rawdict_marks_encoded_spaces(self, path)

            parsed = PDFParser().parse(path, "doc_cid_space_pdf")

        self.assertEqual(
            [list(lines) for lines in _NARROW_SPACE_PAGES],
            [[block.text for block in page.blocks] for page in parsed.pages],
        )

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

    def test_windows_ocr_powershell_hides_console_window_only_on_windows(self) -> None:
        for windows in (True, False):
            with self.subTest(windows=windows), tempfile.TemporaryDirectory() as tmp:
                image = Path(tmp) / "page-1.png"
                image.write_bytes(b"synthetic")
                completed = SimpleNamespace(
                    returncode=0,
                    stdout='{"pages": [{"path": "page-1.png", "text": "제1조(목적)"}]}',
                    stderr="",
                )

                with patch("app.parsers.pdf_parser.shutil.which", return_value="powershell"), patch(
                    "app.core.hidden_process._is_windows", return_value=windows
                ), patch("app.parsers.pdf_parser.subprocess.run", return_value=completed) as run:
                    pages = PDFParser(ocr_backend="windows", ocr_timeout_seconds=5)._extract_windows_ocr_pages([image])

                self.assertEqual(["제1조(목적)"], pages)
                self.assertEqual(5, run.call_args.kwargs["timeout"])
                if windows:
                    self.assertEqual(0x08000000, run.call_args.kwargs["creationflags"])
                else:
                    self.assertNotIn("creationflags", run.call_args.kwargs)


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
        self.get_text_calls: dict[str, int] = {}
        self.get_drawings_calls = 0

    def get_text(self, mode: str):
        self.get_text_calls[mode] = self.get_text_calls.get(mode, 0) + 1
        if mode == "blocks":
            return []
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
        self.get_drawings_calls += 1
        return self._drawings

    def get_images(self, *, full: bool = False) -> list[tuple]:
        return []


class _ImageOnlyFakePdfPage(_FakePdfPage):
    def __init__(self, *, width: float, height: float) -> None:
        super().__init__(width=width, height=height, lines=[])

    def get_images(self, *, full: bool = False) -> list[tuple]:
        return [("image-x",)]


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


def _fake_chars_with_glyph_spaces(
    text: str,
    *,
    x: float,
    y: float,
    width: float = 10.0,
    space_width: float = 3.3,
    space_char: str | None = " ",
    synthetic: bool = False,
) -> list[dict]:
    """Build rawdict chars whose word spaces are narrow encoded glyphs (or plain gaps)."""

    chars = []
    cursor = x
    for char in text:
        if char == " ":
            if space_char is not None:
                chars.append(
                    {
                        "c": space_char,
                        "bbox": (cursor, y, cursor + space_width, y + 10),
                        "synthetic": synthetic,
                    }
                )
            cursor += space_width
            continue
        chars.append({"c": char, "bbox": (cursor, y, cursor + width, y + 10)})
        cursor += width
    return chars


def _skip_unless_rawdict_marks_encoded_spaces(test_case: unittest.TestCase, path: Path) -> None:
    """Skip when the installed PyMuPDF cannot tell encoded spaces from synthetic ones."""

    with fitz.open(path) as doc:
        raw = doc[0].get_text("rawdict")
    space_chars = [
        char
        for block in raw.get("blocks", [])
        for line in block.get("lines", [])
        for span in line.get("spans", [])
        for char in span.get("chars", [])
        if not str(char.get("c") or "x").strip()
    ]
    if not space_chars:
        test_case.skipTest("generated PDF has no whitespace glyphs in rawdict")
    if not all("synthetic" in char for char in space_chars):
        test_case.skipTest("installed PyMuPDF rawdict does not report the synthetic-space flag")


_NARROW_SPACE_PAGES = (
    (
        "가상기관 복무규정",
        "제1조(목적) 이 규정은 직원의 복무에 관한 사항을 정한다.",
        "제2조(정의) 이 규정에서 사용하는 용어의 뜻은 다음과 같다.",
    ),
    (
        "제3조(근무시간) ① 근무시간은 1일 8시간으로 한다.",
        "② 부서장은 필요한 경우 근무시간을 조정할 수 있다.",
    ),
)
_NARROW_SPACE_TABLE = (
    ("구분", "근무 형태", "비고 사항"),
    ("일반 직원", "주 5일 근무", "시차 출퇴근 가능"),
)
_NARROW_SPACE_TABLE_COLUMNS = (56, 200, 360)


def _expected_narrow_space_pages() -> list[list[str]]:
    return [
        [
            *_NARROW_SPACE_PAGES[0],
            # 공백 glyph 없이 따로 배치된 한글 단어는 기존 간격 휴리스틱 결과를 유지한다.
            "가상공단복무규정",
            *[" ".join(row) for row in _NARROW_SPACE_TABLE],
        ],
        list(_NARROW_SPACE_PAGES[1]),
    ]


if __name__ == "__main__":
    unittest.main()
