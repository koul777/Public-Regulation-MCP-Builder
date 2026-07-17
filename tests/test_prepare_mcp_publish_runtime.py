from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.core.config import Settings
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository
from scripts.prepare_mcp_publish_runtime import _infer_article, prepare_mcp_publish_runtime


class PrepareMcpPublishRuntimeTests(unittest.TestCase):
    def test_prepare_mcp_publish_runtime_approves_and_indexes_full_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_data_dir = root / "source"
            target_data_dir = root / "target"
            _seed_source_runtime(source_data_dir)

            report = prepare_mcp_publish_runtime(
                source_data_dir=source_data_dir,
                target_data_dir=target_data_dir,
                source_document_id="doc-source",
                tenant_id="tenant-demo",
                operator_approval_reference="human-review-ticket-001",
                operator_reviewer_id="reviewer-a",
                institution_name="테스트기관",
                document_name="테스트기관 규정집",
                smoke_queries=["휴직 절차"],
            )

            vector_path = target_data_dir / "tenants" / "tenant-demo" / "vector_db" / "tenant-demo" / "approved_vectors.jsonl"
            vectors = [line for line in vector_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            first_vector = json.loads(vectors[0])
            approval_journal = (
                target_data_dir
                / "tenants"
                / "tenant-demo"
                / "repository"
                / "journals"
                / "approvals.jsonl"
            )
            approvals = [json.loads(line) for line in approval_journal.read_text(encoding="utf-8").splitlines()]
            evidence = report["approval_evidence"]
            worklist_exists = (target_data_dir / evidence["worklist_report_path"]).is_file()
            manifest_exists = (target_data_dir / evidence["review_batch_manifest_path"]).is_file()

        self.assertTrue(report["passed"])
        self.assertEqual(2, report["source_chunk_count"])
        self.assertEqual(2, report["selected_chunk_count"])
        self.assertEqual(2, report["approved_chunk_count"])
        self.assertEqual(1, report["approval_record_count"])
        self.assertEqual(2, report["indexed_record_count"])
        self.assertEqual(2, len(vectors))
        self.assertEqual(evidence["worklist_report_path"], first_vector["metadata"]["approval_worklist_report_path"])
        self.assertEqual(evidence["worklist_report_sha256"], first_vector["metadata"]["approval_worklist_report_sha256"])
        self.assertEqual(
            evidence["review_batch_manifest_path"],
            first_vector["metadata"]["approval_review_batch_manifest_path"],
        )
        self.assertEqual(
            evidence["review_batch_manifest_sha256"],
            first_vector["metadata"]["approval_review_batch_manifest_sha256"],
        )
        self.assertEqual("operator_confirmed_human_review", evidence["approval_input_mode"])
        self.assertEqual("human-review-ticket-001", evidence["operator_approval_reference"])
        self.assertEqual("reviewer-a", evidence["operator_reviewer_id"])
        self.assertFalse(evidence["auto_approval_performed"])
        self.assertTrue(first_vector["metadata"]["approval_review_batch_id"].startswith("approval-"))
        self.assertEqual("human_bulk_review", first_vector["metadata"]["approval_review_strategy"])
        self.assertTrue(worklist_exists)
        self.assertTrue(manifest_exists)
        self.assertEqual(evidence["worklist_report_path"], approvals[0]["worklist_evidence"]["worklist_report_path"])
        self.assertEqual(
            evidence["review_batch_manifest_path"],
            approvals[0]["worklist_evidence"]["review_batch_manifest_path"],
        )
        self.assertEqual("public_portal-test-profile", first_vector["metadata"]["profile_id"])
        self.assertEqual("https://example.test/rules", first_vector["metadata"]["source_url"])
        self.assertEqual("테스트기관", report["institution_name"])

    def test_prepare_mcp_publish_runtime_requires_human_review_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_data_dir = root / "source"
            target_data_dir = root / "target"
            _seed_source_runtime(source_data_dir)

            with self.assertRaisesRegex(ValueError, "operator_approval_reference is required"):
                prepare_mcp_publish_runtime(
                    source_data_dir=source_data_dir,
                    target_data_dir=target_data_dir,
                    source_document_id="doc-source",
                    tenant_id="tenant-demo",
                )

    def test_prepare_mcp_publish_runtime_refuses_manual_attention_auto_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_data_dir = root / "source"
            target_data_dir = root / "target"
            _seed_source_runtime(source_data_dir, manual_attention=True)

            with self.assertRaisesRegex(ValueError, "Manual-attention approval batches cannot be auto-approved"):
                prepare_mcp_publish_runtime(
                    source_data_dir=source_data_dir,
                    target_data_dir=target_data_dir,
                    source_document_id="doc-source",
                    tenant_id="tenant-demo",
                    operator_approval_reference="human-review-ticket-003",
                    operator_reviewer_id="reviewer-a",
                )

    def test_prepare_mcp_publish_runtime_can_publish_keyword_subset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_data_dir = root / "source"
            target_data_dir = root / "target"
            _seed_source_runtime(source_data_dir)

            report = prepare_mcp_publish_runtime(
                source_data_dir=source_data_dir,
                target_data_dir=target_data_dir,
                source_document_id="doc-source",
                tenant_id="tenant-demo",
                include_all_chunks=False,
                operator_approval_reference="human-review-ticket-002",
                operator_reviewer_id="reviewer-a",
                selection_keywords=["성과연봉"],
            )

        self.assertTrue(report["passed"])
        self.assertEqual(2, report["source_chunk_count"])
        self.assertEqual(1, report["selected_chunk_count"])
        self.assertEqual(1, report["indexed_record_count"])

    def test_infers_deleted_or_omitted_article_title(self) -> None:
        self.assertEqual(("제4조의2", "삭제"), _infer_article("제4조의2 <삭 제>", {"article_no": "제4조의2"}))
        self.assertEqual(("제3조", "생략"), _infer_article("제3조 생략", {}))


def _seed_source_runtime(data_dir: Path, *, manual_attention: bool = False) -> None:
    repository = JsonRepository(Settings(data_dir=data_dir))
    document = Document(
        document_id="doc-source",
        filename="rules.pdf",
        document_name="원규집",
        file_type="pdf",
        file_hash="hash-source",
        source_url="https://example.test/rules",
        profile_id="public_portal-test-profile",
        institution_name="원본기관",
        status="completed",
    )
    chunks = [
        Chunk(
            chunk_id="chunk-leave",
            document_id="doc-source",
            chunk_type="article",
            text="제31조(휴직의 운영) 휴직 사유가 소멸된 때에는 30일 이내에 신고하여야 한다.",
            retrieval_text="제31조(휴직의 운영) 휴직 사유가 소멸된 때에는 30일 이내에 신고하여야 한다.",
            metadata={
                "article_no": "제31조",
                "article_title": "휴직의 운영",
                **({"review_required": True, "review_flags": ["parser_uncertainty_review"]} if manual_attention else {}),
            },
        ),
        Chunk(
            chunk_id="chunk-pay",
            document_id="doc-source",
            chunk_type="article",
            text="제24조(성과연봉 지급) 성과연봉은 6월 및 12월에 지급한다.",
            retrieval_text="제24조(성과연봉 지급) 성과연봉은 6월 및 12월에 지급한다.",
            metadata={"article_no": "제24조", "article_title": "성과연봉 지급"},
        ),
    ]
    repository.upsert_document(document)
    repository.save_processing_result(document.document_id, [], chunks, [])


if __name__ == "__main__":
    unittest.main()
