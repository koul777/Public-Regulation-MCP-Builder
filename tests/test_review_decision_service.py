from __future__ import annotations

import unittest

from app.schemas.chunk import Chunk
from app.services.review_decision_service import (
    NON_APPROVABLE_CHUNK_STATUSES,
    approval_worklist_metadata,
    approved_content_hash,
    chunk_hashes,
    department_acl_set,
)


class ReviewDecisionServiceTests(unittest.TestCase):
    def test_approved_content_hash_ignores_approval_evidence_bookkeeping(self) -> None:
        base = Chunk(
            chunk_id="chunk-1",
            document_id="doc-1",
            chunk_type="article",
            text="approved text",
            retrieval_text="approved text",
            metadata={"article_no": "1"},
            security_level="internal",
        )
        with_evidence = base.model_copy(
            update={
                "metadata": {
                    "article_no": "1",
                    "approval_worklist_report_path": "reports/approval_worklist_current.json",
                    "approval_worklist_report_sha256": "a" * 64,
                    "approval_review_batch_manifest_path": "reports/approval_review_batches_current.json",
                    "approval_review_batch_manifest_sha256": "b" * 64,
                    "approval_review_batch_id": "approval-batch",
                    "approval_review_batch_chunk_fingerprint": "c" * 64,
                    "approval_review_strategy": "human_bulk_review",
                }
            }
        )
        changed_text = with_evidence.model_copy(update={"retrieval_text": "changed approved text"})

        self.assertEqual(
            approved_content_hash(base, security_level="internal"),
            approved_content_hash(with_evidence, security_level="internal"),
        )
        self.assertNotEqual(
            approved_content_hash(base, security_level="internal"),
            approved_content_hash(changed_text, security_level="internal"),
        )

    def test_approval_worklist_evidence_maps_to_chunk_metadata(self) -> None:
        evidence = {
            "worklist_report_path": "reports/approval_worklist_current.json",
            "worklist_report_sha256": "a" * 64,
            "review_batch_manifest_path": "reports/approval_review_batches_current.json",
            "review_batch_manifest_sha256": "b" * 64,
            "review_batch_id": "approval-batch",
            "review_batch_chunk_fingerprint": "c" * 64,
            "review_strategy": "human_bulk_review",
        }

        metadata = approval_worklist_metadata(evidence)

        self.assertEqual("reports/approval_worklist_current.json", metadata["approval_worklist_report_path"])
        self.assertEqual("a" * 64, metadata["approval_worklist_report_sha256"])
        self.assertEqual("approval-batch", metadata["approval_review_batch_id"])
        self.assertEqual("human_bulk_review", metadata["approval_review_strategy"])

    def test_chunk_hashes_and_status_contract_are_reusable_by_apply_paths(self) -> None:
        chunks = [
            Chunk(chunk_id="chunk-1", document_id="doc-1", chunk_type="article", text="one"),
            Chunk(chunk_id="chunk-2", document_id="doc-1", chunk_type="article", text="two"),
        ]

        hashes = chunk_hashes(chunks, {"chunk-2"})

        self.assertEqual(["chunk-2"], sorted(hashes))
        self.assertEqual({"rejected", "security_blocked", "superseded"}, set(NON_APPROVABLE_CHUNK_STATUSES))
        self.assertEqual(["dept-a", "dept-b"], department_acl_set(["dept-b", "dept-a", "dept-a"]))


if __name__ == "__main__":
    unittest.main()
