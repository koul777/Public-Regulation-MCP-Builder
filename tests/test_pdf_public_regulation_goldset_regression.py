from __future__ import annotations

import re
import unittest
from pathlib import Path

from app.parsers.pdf_parser import PDFParser
from app.processors.chunker import Chunker
from app.processors.normalizer import TextNormalizer
from app.processors.structure_detector import StructureDetector
from scripts.verify_real_parser_regression_fixtures import fixture_skip_reason, resolve_named_fixture_for_tests


ARTICLE_8 = "\uc81c8\uc870"
BRANCH_ARTICLES = [
    "\uc81c9\uc870\uc7582",
    "\uc81c30\uc870\uc7582",
    "\uc81c30\uc870\uc7583",
    "\uc81c37\uc870\uc7582",
    "\uc81c37\uc870\uc7583",
    "\uc81c37\uc870\uc7584",
]
FORM_1 = "\ubcc4\uc9c0\uc81c1\ud638\uc11c\uc2dd"
FORM_7 = "\ubcc4\uc9c0\uc81c7\ud638\uc11c\uc2dd"
COMPENSATION_TABLE_TITLE = "\uc9c1\ubb34\ubc1c\uba85 \ubcf4\uc0c1\uae08 \uc9c0\uae09\uae30\uc900"
SUCCESSION_TABLE_TITLE = "\uad8c\ub9ac\uc2b9\uacc4 \uacb0\uc815\uae30\uc900"
ROMAN_MARKERS = {"\u2170", "\u2171", "\u2172", "\u2173", "\u2174", "\u2175", "\u2176", "\u2177"}


PDF_FIXTURE_RESULT = resolve_named_fixture_for_tests("public_regulation_pdf_4860_235589")
PDF_FIXTURE = Path(str(PDF_FIXTURE_RESULT["matched_path"])) if PDF_FIXTURE_RESULT["passed"] else None


@unittest.skipUnless(PDF_FIXTURE is not None, fixture_skip_reason(PDF_FIXTURE_RESULT))
class PublicRegulationPdfGoldsetRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert PDF_FIXTURE is not None
        parsed = PDFParser().parse(PDF_FIXTURE, "doc_pdf_goldset_regression")
        normalized = TextNormalizer().normalize_document(parsed)
        nodes = StructureDetector().detect(normalized)
        chunks = Chunker().build_chunks(nodes, normalized)

        cls.parsed = parsed
        cls.normalized = normalized
        cls.nodes = nodes
        cls.chunks = chunks

    def test_main_regulation_articles_are_detected_as_entities(self) -> None:
        articles = [node for node in self.nodes if node.node_type == "article" and not self._has_ancestor_type(node, "supplementary")]
        article_numbers = [node.number for node in articles]

        self.assertEqual(len(articles), 55)
        for branch_article in BRANCH_ARTICLES:
            self.assertIn(branch_article, article_numbers)
        self.assertTrue(
            all(re.fullmatch("\uc81c\\d+\uc870(?:\uc758\\d+)?", str(number or "")) for number in article_numbers)
        )
        self.assertFalse(any("\ub530\ub77c" in str(node.title or "") for node in articles))

    def test_appendix_and_form_entities_are_separate_from_references(self) -> None:
        appendices = [node for node in self.nodes if node.node_type == "appendix"]
        forms = [node for node in self.nodes if node.node_type == "form"]

        self.assertEqual(len(appendices) + len(forms), 25)
        self.assertEqual(len(appendices), 6)
        self.assertEqual(len(forms), 19)
        self.assertFalse([node for node in forms if node.page_start == 9])
        self.assertTrue(any(node.page_start == 27 and node.number == FORM_1 for node in forms))

        page9_refs = [
            ref
            for node in self.nodes
            if (node.page_start or 0) <= 9 <= (node.page_end or 0)
            for ref in node.metadata.get("attachment_references", [])
        ]
        self.assertIn({"type": "form", "label": FORM_7, "source_page": 9}, page9_refs)

    def test_pdf_table_classification_uses_layout_evidence(self) -> None:
        article8_chunks = [
            chunk
            for chunk in self.chunks
            if chunk.metadata.get("article_no") == ARTICLE_8
            and (chunk.source_page_start or 0) <= 5 <= (chunk.source_page_end or 0)
        ]
        self.assertTrue(article8_chunks)
        self.assertFalse(any(chunk.metadata.get("table_like") for chunk in article8_chunks))

        compensation_tables = self._table_chunks_on_page(17, COMPENSATION_TABLE_TITLE)
        self.assertTrue(compensation_tables)
        self.assertTrue(any(chunk.metadata.get("table_column_count") == 3 for chunk in compensation_tables))

        succession_tables = self._table_chunks_on_page(21, SUCCESSION_TABLE_TITLE)
        self.assertTrue(succession_tables)
        self.assertTrue(any(chunk.metadata.get("table_column_count") == 6 for chunk in succession_tables))

    def test_page_23_roman_marker_footnotes_are_linked_to_source_spans(self) -> None:
        links = [
            link
            for chunk in self.chunks
            for link in chunk.metadata.get("footnote_links", [])
            if link.get("source_page") == 23
        ]

        self.assertEqual(len(links), 8)
        self.assertEqual({link.get("marker") for link in links}, ROMAN_MARKERS)
        self.assertTrue(all(link.get("marker_bbox") and link.get("footnote_bbox") for link in links))

    def test_original_blank_page_is_marked_blank_not_missing(self) -> None:
        page39 = next(page for page in self.parsed.pages if page.page_no == 39)

        self.assertEqual(page39.blocks, [])
        self.assertIn(39, self.parsed.metadata.get("blank_pages", []))
        self.assertNotIn(39, self.parsed.metadata.get("missing_content_pages", []))

    def _table_chunks_on_page(self, page_no: int, title: str):
        return [
            chunk
            for chunk in self.chunks
            if chunk.metadata.get("table_like")
            and (chunk.source_page_start or 0) <= page_no <= (chunk.source_page_end or 0)
            and (
                title in str(chunk.metadata.get("table_title") or "")
                or title in str(chunk.metadata.get("table_citation_label") or "")
                or title in (chunk.normalized_text or "")
            )
        ]

    def _has_ancestor_type(self, node, node_type: str) -> bool:
        by_id = {item.node_id: item for item in self.nodes}
        parent_id = node.parent_id
        while parent_id:
            parent = by_id.get(parent_id)
            if parent is None:
                return False
            if parent.node_type == node_type:
                return True
            parent_id = parent.parent_id
        return False


if __name__ == "__main__":
    unittest.main()
