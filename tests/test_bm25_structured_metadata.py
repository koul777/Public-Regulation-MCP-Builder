from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.retrieval.bm25_index import Bm25Index, load_bm25_index, write_bm25_index


class Bm25StructuredMetadataTests(unittest.TestCase):
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
        self.assertEqual(2, loaded.structured_metadata_version)


if __name__ == "__main__":
    unittest.main()
