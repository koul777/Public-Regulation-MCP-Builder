from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.core.config import Settings
from app.mcp_server import regulation_tools
from app.mcp_server.regulation_tools import _canonical_article_no, get_article, mcp_auth_context
from tests.test_regulation_mcp_tools import _prepare_mcp_indexed_document


class CanonicalArticleNoTests(unittest.TestCase):
    def test_toc_labels_and_citations_reduce_to_the_article_locator(self) -> None:
        cases = {
            "제16조": "제16조",
            "제16조 목적": "제16조",
            "제16조(목적)": "제16조",
            "제 16 조": "제16조",
            "16조": "제16조",
            "16": "제16조",
            "제16조의2": "제16조의2",
            "제16조의 2(특례)": "제16조의2",
            "제16조제2항": "제16조",
            "제１６조": "제16조",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(expected, _canonical_article_no(value))

    def test_values_without_a_leading_article_locator_stay_unchanged(self) -> None:
        for value in ("부칙", "부칙 제1조", "별표 1", "Article 1", "목적"):
            with self.subTest(value=value):
                self.assertEqual(value, _canonical_article_no(value))


class ArticleLookupLocatorFallbackTests(unittest.TestCase):
    def test_get_article_accepts_toc_label_and_citation_forms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            auth = mcp_auth_context(tenant_id="tenant-a")
            exact = get_article(
                settings=settings,
                auth=auth,
                document_id="doc_mcp",
                article_no="제10조",
                security_levels=["internal"],
            )["articles"]
            self.assertTrue(exact)
            for value in ("제10조(육아휴직)", "제10조 육아휴직", "10"):
                with self.subTest(value=value):
                    articles = get_article(
                        settings=settings,
                        auth=auth,
                        document_id="doc_mcp",
                        article_no=value,
                        security_levels=["internal"],
                    )["articles"]
                    self.assertEqual([item["id"] for item in exact], [item["id"] for item in articles])

    def test_get_regulation_article_retries_with_the_canonical_locator_only_on_a_miss(self) -> None:
        settings = Settings(data_dir=Path(tempfile.gettempdir()) / "unused-article-locator", api_audit_enabled=False)
        auth = mcp_auth_context(tenant_id="tenant-a")
        record = {
            "document_id": "doc-a",
            "chunk_id": "chunk-a",
            "text": "approved article",
            "content_hash": "content-hash-a",
            "metadata": {
                "document_id": "doc-a",
                "chunk_id": "chunk-a",
                "approval_status": "approved",
                "approval_id": "approval-a",
                "approved_content_hash": "approved-hash-a",
                "security_level": "internal",
                "regulation_unit_id": "unit-a",
                "article_no": "제1조",
                "article_title": "목적",
            },
        }
        requested: list[str] = []

        def load_records(*_args, article_no: str, **_kwargs):
            requested.append(article_no)
            return [record] if article_no == "제1조" else []

        with (
            patch.object(regulation_tools, "_resolve_mcp_profile_scope", return_value=None),
            patch.object(
                regulation_tools,
                "_verified_hierarchical_runtime_paths",
                return_value=(Path("index.sqlite3"), Path("vectors.jsonl")),
            ),
            patch.object(
                regulation_tools,
                "_verified_hierarchical_runtime_token",
                return_value=SimpleNamespace(is_current=lambda: True),
            ),
            patch.object(
                regulation_tools,
                "_hierarchical_authorization_source_identity",
                return_value=("runtime_sidecar", "same"),
            ),
            patch.object(regulation_tools, "_fully_visible_regulation_units", return_value={"unit-a"}),
            patch.object(regulation_tools, "load_hierarchical_article_records", side_effect=load_records),
        ):
            label = regulation_tools.get_regulation_article(
                settings=settings,
                auth=auth,
                regulation_unit_id="unit-a",
                article_no="제1조 목적",
                security_levels=["internal"],
            )
            self.assertEqual(["제1조 목적", "제1조"], requested)
            self.assertEqual("제1조", label["article_no"])
            self.assertEqual(1, len(label["articles"]))

            requested.clear()
            exact = regulation_tools.get_regulation_article(
                settings=settings,
                auth=auth,
                regulation_unit_id="unit-a",
                article_no="제1조",
                security_levels=["internal"],
            )
            self.assertEqual(["제1조"], requested)
            self.assertEqual("제1조", exact["article_no"])


if __name__ == "__main__":
    unittest.main()
