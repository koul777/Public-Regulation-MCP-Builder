from __future__ import annotations

import unittest

from app.core.failure_classification import classify_processing_failure
from app.parsers.base import OCRRequiredError


class FailureClassificationTests(unittest.TestCase):
    def test_classifies_typed_ocr_required_error(self) -> None:
        classification = classify_processing_failure(
            OCRRequiredError("No text blocks were extracted from the PDF file. OCR may be required.", page_count=12),
            filename="scan.pdf",
        )

        self.assertEqual(classification.failure_category, "ocr_required")
        self.assertTrue(classification.ocr_required)
        self.assertEqual(classification.ocr_page_count, 12)
        self.assertFalse(classification.retry_recommended)
        self.assertEqual(classification.failure_next_action, "run_ocr_then_reprocess")

    def test_classifies_unsupported_format_as_non_retryable(self) -> None:
        classification = classify_processing_failure("Unsupported file extension: .txt", filename="bad.txt")

        self.assertEqual(classification.failure_category, "unsupported_format")
        self.assertFalse(classification.retry_recommended)

    def test_defaults_unknown_errors_to_retryable(self) -> None:
        classification = classify_processing_failure("temporary storage timeout", filename="a.pdf")

        self.assertEqual(classification.failure_category, "transient_or_unknown")
        self.assertTrue(classification.retry_recommended)


if __name__ == "__main__":
    unittest.main()
