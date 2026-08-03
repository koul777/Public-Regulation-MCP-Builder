from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.retrieval.bm25_index import (
    BM25_STRUCTURED_METADATA_VERSION,
    Bm25Index,
    load_bm25_index,
    write_bm25_index,
)


class Bm25StructuredMetadataTests(unittest.TestCase):
    def test_legacy_combined_and_standalone_wrappers_have_equal_term_frequencies(self) -> None:
        common_metadata = {
            "regulation_no": "1",
            "regulation_title": "인사규정",
            "article_no": "제1조",
            "article_title": "목적",
            "chunker_version": "0.1.8",
        }
        combined = {
            "id": "combined:article",
            "document_id": "combined",
            "chunk_id": "article",
            "text": (
                "[문서명] 기관 통합 규정집\n"
                "[위치] 기관 통합 규정집 > 제1편 일반규정 > 제1장 인사 > 1. 인사규정 > 제1장 총칙 > 제1조 목적\n"
                "[본문]\n제1조(목적) 인사 운영 기준을 정한다."
            ),
            "metadata": {
                **common_metadata,
                "hierarchy_path": "기관 통합 규정집 > 제1편 일반규정 > 제1장 인사 > 1. 인사규정 > 제1장 총칙 > 제1조 목적",
                "part_no": "제1편",
                "chapter_no": "제1장",
                "section_no": "",
            },
            "content_hash": "combined-hash",
        }
        standalone = {
            "id": "standalone:article",
            "document_id": "standalone",
            "chunk_id": "article",
            "text": (
                "[문서명] 인사규정\n"
                "[위치] 인사규정 > 제1장 총칙 > 제1조 목적\n"
                "[본문]\n제1조(목적) 인사 운영 기준을 정한다."
            ),
            "metadata": {
                **common_metadata,
                "hierarchy_path": "인사규정 > 제1장 총칙 > 제1조 목적",
                "chapter_no": "제1장",
            },
            "content_hash": "standalone-hash",
        }

        combined_index = Bm25Index.build([combined])
        standalone_index = Bm25Index.build([standalone])

        self.assertEqual(
            combined_index.documents[0]["term_frequencies"],
            standalone_index.documents[0]["term_frequencies"],
        )
        self.assertEqual(
            next(iter(combined_index.score("인사 운영 목적").values())),
            next(iter(standalone_index.score("인사 운영 목적").values())),
        )

    def test_structured_metadata_fields_contribute_to_scores(self) -> None:
        records = [
            {
                "id": "doc:form",
                "document_id": "doc",
                "chunk_id": "form",
                "text": "request form",
                "metadata": {
                    "document_id": "doc",
                    "chunk_id": "form",
                    "article_refs": ["article 5"],
                    "appendix_refs": ["appendix 2"],
                    "form_refs": ["form 12"],
                },
                "content_hash": "hash-form",
            },
            {
                "id": "doc:noise",
                "document_id": "doc",
                "chunk_id": "noise",
                "text": "random memo",
                "metadata": {
                    "document_id": "doc",
                    "chunk_id": "noise",
                },
                "content_hash": "hash-noise",
            },
        ]
        index = Bm25Index.build(records)

        scores = index.score("article 5")

        self.assertIn("doc:form", scores)
        self.assertGreater(scores["doc:form"], 0.0)
        self.assertNotIn("doc:noise", scores)

    def test_structured_metadata_version_is_serialized(self) -> None:
        records = [
            {
                "id": "doc:form",
                "document_id": "doc",
                "chunk_id": "form",
                "text": "request form",
                "metadata": {
                    "document_id": "doc",
                    "chunk_id": "form",
                    "article_refs": ["article 5"],
                },
                "content_hash": "hash-form",
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "bm25_index.json"
            write_bm25_index(index_path, records)
            loaded = load_bm25_index(index_path)
            raw = index_path.read_text(encoding="utf-8")

        self.assertIsNotNone(loaded)
        self.assertIn("structured_metadata_version", raw)
        self.assertEqual(BM25_STRUCTURED_METADATA_VERSION, loaded.structured_metadata_version)


if __name__ == "__main__":
    unittest.main()
