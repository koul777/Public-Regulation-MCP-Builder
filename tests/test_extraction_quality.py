import unittest

from app.parsers.extraction_quality import build_extraction_quality_report
from app.schemas.parsed import ParsedBlock, ParsedDocument, ParsedPage


class ExtractionQualityTests(unittest.TestCase):
    def test_mixed_document_reports_image_and_table_review_signals(self) -> None:
        parsed = ParsedDocument(
            document_id="doc-1",
            source_file="rule.pdf",
            file_type="pdf",
            raw_text="제1조 목적",
            pages=[
                ParsedPage(page_no=1, blocks=[ParsedBlock(type="text", text="제1조 목적")]),
                ParsedPage(page_no=2, blocks=[ParsedBlock(type="table", text="항목 | 기준")]),
                ParsedPage(page_no=3, blocks=[ParsedBlock(type="image", text="도표")]),
            ],
            metadata={"pdf_embedded_image_pages": [3], "parser_uncertainty_flags": ["embedded_text_extracted"]},
        )

        report = build_extraction_quality_report(parsed)

        self.assertEqual("review_required", report["status"])
        self.assertTrue(report["ready_for_normalization"])
        self.assertTrue(report["review_required"])
        self.assertEqual(1, report["table_block_count"])
        self.assertEqual(1, report["image_block_count"])
        self.assertEqual([3], report["embedded_image_page_numbers"])

    def test_image_only_pdf_is_blocked_when_no_text_exists(self) -> None:
        parsed = ParsedDocument(
            document_id="doc-2",
            source_file="scan.pdf",
            file_type="pdf",
            raw_text="",
            pages=[ParsedPage(page_no=1, blocks=[])],
            metadata={"missing_content_pages": [1], "parser_uncertainty_flags": ["ocr_required"]},
        )

        report = build_extraction_quality_report(parsed)

        self.assertEqual("blocked", report["status"])
        self.assertFalse(report["ready_for_normalization"])
        self.assertEqual(["no_pages_or_text_extracted"], report["blocking_reasons"])

    def test_clean_text_document_can_continue_without_review(self) -> None:
        parsed = ParsedDocument(
            document_id="doc-3",
            source_file="rule.docx",
            file_type="docx",
            raw_text="제1조 목적",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(type="text", text="제1조 목적")])],
            metadata={},
        )

        report = build_extraction_quality_report(parsed)

        self.assertEqual("pass", report["status"])
        self.assertEqual(1.0, report["page_coverage_ratio"])
        self.assertEqual(1.0, report["text_page_coverage_ratio"])


if __name__ == "__main__":
    unittest.main()
