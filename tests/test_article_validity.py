from __future__ import annotations

import unittest

from app.processors.article_validity import build_article_validity_windows, summarize_article_validity_windows
from app.processors.metadata_extractor import MetadataExtractor


class ArticleValidityTests(unittest.TestCase):
    def test_builds_default_and_override_windows(self) -> None:
        windows = build_article_validity_windows(
            effective_date="2026-01-01",
            article_effective_overrides=[
                {"article_ref": "제17조제1항제1호", "effective_date": "2026-01-02"},
            ],
            revision_history=[
                {"event_type": "일부개정", "date": "2025-01-13", "effective_date": "2025-01-13"},
            ],
        )

        summary = summarize_article_validity_windows(windows)
        self.assertEqual(summary["window_count"], 3)
        self.assertEqual(summary["override_window_count"], 1)
        self.assertIn("제17조제1항제1호", summary["article_refs"])

    def test_metadata_extractor_includes_validity_windows(self) -> None:
        text = (
            "부칙 <2025. 12. 30.>\n"
            "제1조(시행일) 이 규정은 2026년 1월 1일부터 시행한다. "
            "다만, 제17조제1항제1호의 개정규정은 2026년 1월 2일부터 시행한다."
        )

        metadata = MetadataExtractor().extract(text)

        self.assertTrue(any(item.get("source") == "article_effective_override" for item in metadata["article_validity_windows"]))
        self.assertTrue(any(item.get("source") == "document_effective_date" for item in metadata["article_validity_windows"]))
