from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.agents.model_router import QWEN3_EMBEDDING_MODEL
from app.api.routes_documents import IndexRequest, _indexed_job_embedding_model
from app.ingestion.embedding_adapter import LOCAL_HASH_EMBEDDING_MODEL


class IndexEmbeddingModelTests(unittest.TestCase):
    def test_qwen_embedding_is_an_explicit_approved_index_option(self) -> None:
        request = IndexRequest(
            embedding_model=QWEN3_EMBEDDING_MODEL,
            embedding_dimensions=1024,
        )

        self.assertEqual(QWEN3_EMBEDDING_MODEL, request.embedding_model)
        self.assertEqual(1024, request.embedding_dimensions)

    def test_unknown_embedding_model_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            IndexRequest(embedding_model="remote-or-unknown-model")

    def test_reindex_preserves_only_known_local_model(self) -> None:
        self.assertEqual(
            QWEN3_EMBEDDING_MODEL,
            _indexed_job_embedding_model({"embedding_model": QWEN3_EMBEDDING_MODEL}),
        )
        self.assertEqual(
            LOCAL_HASH_EMBEDDING_MODEL,
            _indexed_job_embedding_model({"embedding_model": "unknown"}),
        )


if __name__ == "__main__":
    unittest.main()
