from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.parsers.docx_parser import DocxParser
from app.services.synthetic_sample_service import (
    SYNTHETIC_SAMPLE_FILENAME,
    build_synthetic_regulation_docx,
)


class SyntheticSampleServiceTests(unittest.TestCase):
    def test_generated_sample_is_parseable_and_clearly_synthetic(self) -> None:
        payload = build_synthetic_regulation_docx()
        self.assertTrue(payload.startswith(b"PK"))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / SYNTHETIC_SAMPLE_FILENAME
            path.write_bytes(payload)
            parsed = DocxParser().parse(path, "synthetic-doc")

        self.assertIn("합성 복무규정", parsed.raw_text)
        self.assertIn("실제 기관 규정이 아닙니다", parsed.raw_text)
        self.assertIn("제1조", parsed.raw_text)
        self.assertIn("긴급 휴가", parsed.raw_text)
        self.assertNotIn("C:\\", parsed.raw_text)
        self.assertNotIn("/Users/", parsed.raw_text)

    def test_generated_sample_is_cached_as_immutable_bytes(self) -> None:
        first = build_synthetic_regulation_docx()
        second = build_synthetic_regulation_docx()
        self.assertIs(first, second)


if __name__ == "__main__":
    unittest.main()
