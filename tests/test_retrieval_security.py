from __future__ import annotations

import unittest

from app.retrieval.bm25_index import Bm25Index
from app.retrieval.searcher import search


class RetrievalSecurityTests(unittest.TestCase):
    def test_bm25_search_only_returns_pre_filtered_visible_records(self) -> None:
        approved = _record("doc:approved", "병가 승인 규정", tenant_id="tenant-a", security_level="internal")
        other_tenant = _record("doc:other", "병가 비공개 타기관 규정", tenant_id="tenant-b", security_level="internal")
        confidential = _record("doc:confidential", "병가 비밀 규정", tenant_id="tenant-a", security_level="confidential")
        all_records = [approved, other_tenant, confidential]
        visible_records = [approved]
        index = Bm25Index.build(all_records)

        scored, metadata = search("병가 규정", visible_records, index, top_k=10, index_records=all_records)

        self.assertEqual("kiwi-bm25-v1", metadata["retrieval_model"])
        self.assertEqual(["doc:approved"], [record["id"] for _score, record in scored])


def _record(record_id: str, text: str, *, tenant_id: str, security_level: str) -> dict:
    chunk_id = record_id.rsplit(":", 1)[-1]
    metadata = {
        "tenant_id": tenant_id,
        "document_id": "doc",
        "chunk_id": chunk_id,
        "approval_status": "approved",
        "approval_id": f"approval-{chunk_id}",
        "security_level": security_level,
        "article_title": "병가",
    }
    return {
        "id": record_id,
        "document_id": "doc",
        "chunk_id": chunk_id,
        "text": text,
        "metadata": metadata,
        "content_hash": f"hash-{record_id}-{text}-{security_level}",
        "embedding": [1.0, 0.0],
    }


if __name__ == "__main__":
    unittest.main()
