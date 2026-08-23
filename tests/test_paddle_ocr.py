from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.parsers.paddle_ocr import (
    KOREAN_PPOCRV5_MODEL,
    PaddleKoreanOcrAdapter,
    normalize_paddle_prediction,
)
from app.parsers.pdf_parser import PDFParser
from app.schemas.parsed import ParsedBlock, ParsedPage


class _ArrayLike:
    def __init__(self, values) -> None:
        self.values = values

    def __iter__(self):
        return iter(self.values)

    def __bool__(self):
        raise ValueError("array truth value is ambiguous")

    def tolist(self):
        return self.values


class PaddleOcrContractTests(unittest.TestCase):
    def test_normalizes_array_like_boxes_without_boolean_evaluation(self) -> None:
        result = normalize_paddle_prediction(
            {
                "rec_texts": ["제1조"],
                "rec_scores": _ArrayLike([0.97]),
                "rec_boxes": _ArrayLike([_ArrayLike([10, 20, 110, 60])]),
            },
            page_no=1,
        )

        self.assertEqual("제1조", result.text)
        self.assertEqual((10.0, 20.0, 110.0, 60.0), result.lines[0].bbox)

    def test_normalizes_v3_result_and_preserves_reading_order(self) -> None:
        result = normalize_paddle_prediction(
            {
                "res": {
                    "rec_texts": ["제2조 정의", "제1조 목적", "잡음"],
                    "rec_scores": [0.91, 0.98, 0.1],
                    "rec_boxes": [[10, 50, 100, 70], [10, 10, 100, 30], [0, 80, 10, 90]],
                }
            },
            page_no=4,
            min_confidence=0.35,
        )

        self.assertEqual(4, result.page_no)
        self.assertEqual("제1조 목적\n제2조 정의", result.text)
        self.assertEqual(2, len(result.lines))
        self.assertEqual(1, result.dropped_low_confidence_lines)
        self.assertEqual(KOREAN_PPOCRV5_MODEL, result.model_id)
        self.assertNotIn("path", result.model_dump())

    def test_normalizes_legacy_result_shape(self) -> None:
        result = normalize_paddle_prediction(
            [[[[[0, 0], [20, 0], [20, 10], [0, 10]], ("제1조", 0.99)]]],
            page_no=1,
        )

        self.assertEqual("제1조", result.text)
        self.assertEqual((0.0, 0.0, 20.0, 10.0), result.lines[0].bbox)

    def test_adapter_rejects_missing_or_unsupported_images_before_model_load(self) -> None:
        adapter = PaddleKoreanOcrAdapter()
        adapter._engine = Mock()
        with self.assertRaisesRegex(ValueError, "does not exist"):
            adapter.recognize_pages([Path("missing.png")])

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.txt"
            path.write_text("not an image", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unsupported OCR image extension"):
                adapter.recognize_pages([path])

    def test_pdf_merge_fills_only_empty_page_and_keeps_native_text(self) -> None:
        source_pages = [
            ParsedPage(page_no=1, blocks=[ParsedBlock(text="native")]),
            ParsedPage(page_no=2, blocks=[]),
        ]
        ocr_result = normalize_paddle_prediction(
            {
                "rec_texts": ["제3조(책임) 담당자는 기록한다."],
                "rec_scores": [0.97],
                "rec_boxes": [[5, 10, 190, 30]],
            },
            page_no=2,
        )

        merged = PDFParser._merge_ocr_results(source_pages, [ocr_result])

        self.assertEqual("native", merged[0].blocks[0].text)
        self.assertEqual("제3조(책임) 담당자는 기록한다.", merged[1].blocks[0].text)
        self.assertEqual("paddleocr", merged[1].blocks[0].metadata["ocr_backend"])
        self.assertTrue(merged[1].blocks[0].metadata["ocr_review_required"])


if __name__ == "__main__":
    unittest.main()
