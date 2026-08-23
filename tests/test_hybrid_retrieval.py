import hashlib
import unittest

from app.retrieval.bm25_index import Bm25Index
from app.retrieval.searcher import search


class HybridRetrievalTests(unittest.TestCase):
    def test_ready_bm25_and_local_embeddings_are_fused(self) -> None:
        records = [
            self._record("doc:leave", "병가 신청 절차와 승인 기준", [1.0, 0.0]),
            self._record("doc:travel", "출장 여비 신청 절차", [0.0, 1.0]),
        ]
        index = Bm25Index.build(records)

        scored, metadata = search("병가 신청", records, index, top_k=2)

        self.assertTrue(scored)
        self.assertEqual("hybrid-bm25-hash-v1", metadata["retrieval_model"])
        self.assertFalse(metadata["retrieval_fallback"])
        self.assertEqual(0.65, metadata["hybrid_keyword_weight"])
        self.assertEqual(0.35, metadata["hybrid_vector_weight"])
        self.assertEqual({"doc:leave", "doc:travel"}, {item[1]["id"] for item in scored})

    @staticmethod
    def _record(record_id: str, text: str, embedding: list[float]) -> dict:
        chunk_id = record_id.rsplit(":", 1)[-1]
        metadata = {
            "tenant_id": "tenant-a",
            "document_id": "doc",
            "chunk_id": chunk_id,
            "approval_status": "approved",
            "approval_id": f"approval-{chunk_id}",
            "security_level": "internal",
            "regulation_title": "복무규정",
        }
        return {
            "id": record_id,
            "document_id": "doc",
            "chunk_id": chunk_id,
            "text": text,
            "metadata": metadata,
            "content_hash": hashlib.sha256(f"{record_id}\n{text}".encode("utf-8")).hexdigest(),
            "embedding": embedding,
        }


if __name__ == "__main__":
    unittest.main()
