from __future__ import annotations

import unittest
import weakref
from types import SimpleNamespace
from unittest.mock import patch

from app.api import routes_documents
from app.core.config import Settings
from app.core.security_primitives import AuthContext


class _TrackedList(list):
    pass


class DocumentIndexingMemoryTests(unittest.TestCase):
    def test_review_and_serialized_copies_are_released_before_embedding(self) -> None:
        references = {}

        def load_chunks(*args):
            chunks = _TrackedList([object()])
            references["chunks"] = weakref.ref(chunks)
            return chunks

        def prepare(*args):
            prepared = _TrackedList([{}])
            references["prepared"] = weakref.ref(prepared)
            return prepared

        class EmbeddingReached(Exception):
            pass

        def embed(records, **kwargs):
            self.assertIsNone(references["chunks"]())
            self.assertIsNone(references["prepared"]())
            self.assertEqual([{"document_id": "doc", "metadata": {"tenant_id": "default"}}], records)
            raise EmbeddingReached

        with patch.object(routes_documents, "_load_review_chunks", new=load_chunks), \
             patch.object(routes_documents, "_pending_revision_recovery_context", new=lambda **kwargs: None), \
             patch.object(routes_documents, "_chunks_for_indexing", new=prepare), \
             patch.object(routes_documents, "_require_approval_journal_records", new=lambda *args, **kwargs: None), \
             patch.object(routes_documents, "build_vector_records", new=lambda prepared: (
                 [{"document_id": "doc", "metadata": {"tenant_id": "default"}}], {})), \
             patch.object(routes_documents, "embed_vector_records", new=embed):
            with self.assertRaises(EmbeddingReached):
                routes_documents._run_document_indexing(
                    settings=Settings(), repository=SimpleNamespace(get_document=lambda doc_id: object()),
                    document_id="doc", auth=AuthContext(actor="test", tenant_id="default", auth_mode="test", role="admin"),
                    request=routes_documents.IndexRequest(), action="index",
                )


if __name__ == "__main__":
    unittest.main()
