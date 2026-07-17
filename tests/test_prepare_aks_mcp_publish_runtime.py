from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository
from scripts.prepare_aks_mcp_publish_runtime import (
    DEFAULT_AKS_PROFILE_ID,
    DEFAULT_AKS_SOURCE_URL,
    _infer_article,
    _parse_args,
    _prepare_chunk,
    _prepare_document,
    _reset_target_tenant_runtime,
    _validate_publish_runtime_approval_evidence,
    prepare_aks_mcp_publish_runtime,
)
from scripts.check_mcp_connection_readiness import check_mcp_connection_readiness


class PrepareAksMcpPublishRuntimeTests(unittest.TestCase):
    def test_reset_target_tenant_runtime_removes_only_selected_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_data_dir = root / "aks_runtime"
            tenant_dir = target_data_dir / "tenants" / "tenant-aks-publish"
            sibling_tenant_dir = target_data_dir / "tenants" / "tenant-other"
            smoke_file = tenant_dir / "vector_db" / "bm25_index.json"
            sibling_file = sibling_tenant_dir / "repository" / "keep.json"
            smoke_file.parent.mkdir(parents=True)
            sibling_file.parent.mkdir(parents=True)
            smoke_file.write_text("doc_mcp_smoke_v1", encoding="utf-8")
            sibling_file.write_text("keep", encoding="utf-8")

            removed = _reset_target_tenant_runtime(
                target_data_dir=target_data_dir,
                tenant_data_dir=tenant_dir,
            )

            self.assertTrue(removed)
            self.assertFalse(tenant_dir.exists())
            self.assertTrue(sibling_file.exists())

    def test_reset_target_tenant_runtime_rejects_path_outside_target_tenants_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_data_dir = root / "aks_runtime"
            unsafe_tenant_dir = root / "repository"
            unsafe_tenant_dir.mkdir(parents=True)

            with self.assertRaises(ValueError):
                _reset_target_tenant_runtime(
                    target_data_dir=target_data_dir,
                    tenant_data_dir=unsafe_tenant_dir,
                )

    def test_cli_defaults_to_full_corpus_publish(self) -> None:
        with patch("sys.argv", ["prepare_aks_mcp_publish_runtime.py"]):
            args = _parse_args()

        self.assertTrue(args.include_all_chunks)

    def test_cli_can_publish_keyword_subset_for_quick_samples(self) -> None:
        with patch("sys.argv", ["prepare_aks_mcp_publish_runtime.py", "--keyword-subset"]):
            args = _parse_args()

        self.assertFalse(args.include_all_chunks)

    def test_cli_accepts_draft_only_mode(self) -> None:
        with patch("sys.argv", ["prepare_aks_mcp_publish_runtime.py", "--draft-only"]):
            args = _parse_args()

        self.assertTrue(args.draft_only)

    def test_prepare_document_and_chunk_preserve_aks_profile_metadata(self) -> None:
        document = Document(
            document_id="doc-source",
            filename="aks.pdf",
            file_type="pdf",
            file_hash="hash-source",
            status="completed",
        )
        prepared_document = _prepare_document(document, tenant_id="tenant-aks")
        chunk = _prepare_chunk(
            Chunk(
                chunk_id="chunk-1",
                document_id="doc-source",
                chunk_type="article",
                text="sample regulation text",
                retrieval_text="sample regulation text",
                metadata={},
            ).model_dump(mode="json"),
            tenant_id="tenant-aks",
            security_level="internal",
        )

        self.assertEqual(DEFAULT_AKS_PROFILE_ID, prepared_document.profile_id)
        self.assertEqual(DEFAULT_AKS_SOURCE_URL, prepared_document.source_url)
        self.assertEqual(DEFAULT_AKS_PROFILE_ID, chunk.metadata["profile_id"])
        self.assertEqual(DEFAULT_AKS_SOURCE_URL, chunk.metadata["source_url"])

    def test_prepare_aks_runtime_writes_approval_evidence_to_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_data_dir = root / "source"
            target_data_dir = root / "target"
            _seed_source_runtime(source_data_dir)
            stale_root_manifest = target_data_dir / "repository" / "manifest.json"
            stale_root_manifest.parent.mkdir(parents=True)
            stale_root_manifest.write_text(json.dumps({"documents": {"stale": {}}}) + "\n", encoding="utf-8")

            with patch(
                "scripts.prepare_aks_mcp_publish_runtime._run_mcp_smoke_queries",
                return_value=[{"query": "sample", "result_count": 1, "fetch_has_text": True}],
            ):
                report = prepare_aks_mcp_publish_runtime(
                    source_data_dir=source_data_dir,
                    target_data_dir=target_data_dir,
                    source_document_id="doc-aks",
                    tenant_id="tenant-aks-test",
                    operator_approval_reference="aks-human-review-ticket-001",
                    operator_reviewer_id="aks-reviewer",
                )

            tenant_dir = target_data_dir / "tenants" / "tenant-aks-test"
            vector_path = (
                tenant_dir
                / "vector_db"
                / "tenant-aks-test"
                / "approved_vectors.jsonl"
            )
            vector = json.loads(vector_path.read_text(encoding="utf-8").splitlines()[0])
            approval_journal = (
                tenant_dir / "repository" / "journals" / "approvals.jsonl"
            )
            approval = json.loads(approval_journal.read_text(encoding="utf-8").splitlines()[0])
            evidence = report["approval_evidence"]
            worklist_exists = (target_data_dir / evidence["worklist_report_path"]).is_file()
            manifest_exists = (target_data_dir / evidence["review_batch_manifest_path"]).is_file()
            runtime_manifest_path = tenant_dir / "mcp_runtime_manifest.json"
            runtime_manifest = json.loads(runtime_manifest_path.read_text(encoding="utf-8"))
            approval_snapshot_path = tenant_dir / "repository" / "approval_snapshot.json"
            approval_snapshot = json.loads(approval_snapshot_path.read_text(encoding="utf-8"))
            readiness = check_mcp_connection_readiness(
                client_profile="bundle",
                data_dir=target_data_dir,
                tenant_id="tenant-aks-test",
                tenant_storage_isolation=True,
                check_cli=False,
                audit_index_visibility=True,
                require_indexed=True,
            )

        self.assertTrue(report["passed"])
        self.assertEqual(1, report["approval_record_count"])
        self.assertEqual("mcp_runtime_data_bundle", runtime_manifest["report_type"])
        self.assertEqual(["doc-aks"], runtime_manifest["document_ids"])
        self.assertEqual(1, runtime_manifest["record_count"])
        self.assertEqual(1, runtime_manifest["chunk_count"])
        self.assertEqual("ready", runtime_manifest["bm25_index_status"])
        self.assertEqual(str(runtime_manifest_path), report["runtime_manifest"]["files"]["runtime_manifest"])
        self.assertEqual("mcp_runtime_approval_snapshot", approval_snapshot["report_type"])
        self.assertEqual(1, approval_snapshot["snapshot_count"])
        self.assertEqual(evidence["worklist_report_path"], vector["metadata"]["approval_worklist_report_path"])
        self.assertEqual(
            evidence["review_batch_manifest_path"],
            vector["metadata"]["approval_review_batch_manifest_path"],
        )
        self.assertEqual("human_bulk_review", vector["metadata"]["approval_review_strategy"])
        self.assertEqual("operator_confirmed_human_review", evidence["approval_input_mode"])
        self.assertEqual("aks-human-review-ticket-001", evidence["operator_approval_reference"])
        self.assertEqual("aks-reviewer", evidence["operator_reviewer_id"])
        self.assertFalse(evidence["auto_approval_performed"])
        self.assertTrue(report["approval_evidence_validation"]["passed"])
        self.assertEqual(
            evidence["worklist_report_sha256"],
            vector["metadata"]["approval_worklist_report_sha256"],
        )
        self.assertEqual(
            evidence["review_batch_manifest_sha256"],
            vector["metadata"]["approval_review_batch_manifest_sha256"],
        )
        self.assertEqual(evidence["worklist_report_path"], approval["worklist_evidence"]["worklist_report_path"])
        self.assertEqual(
            evidence["worklist_report_sha256"],
            approval["worklist_evidence"]["worklist_report_sha256"],
        )
        self.assertEqual(
            evidence["review_batch_manifest_path"],
            approval["worklist_evidence"]["review_batch_manifest_path"],
        )
        self.assertEqual(
            evidence["review_batch_manifest_sha256"],
            approval["worklist_evidence"]["review_batch_manifest_sha256"],
        )
        self.assertTrue(worklist_exists)
        self.assertTrue(manifest_exists)
        self.assertFalse((target_data_dir / "repository").exists())
        self.assertFalse((tenant_dir / "repository" / "doc-aks_nodes.json").exists())
        self.assertFalse((tenant_dir / "repository" / "doc-aks_issues.json").exists())
        self.assertTrue(readiness["passed"], readiness["findings"])

    def test_prepare_aks_runtime_draft_only_does_not_approve_or_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_data_dir = root / "source"
            target_data_dir = root / "target"
            _seed_source_runtime(source_data_dir)

            report = prepare_aks_mcp_publish_runtime(
                source_data_dir=source_data_dir,
                target_data_dir=target_data_dir,
                source_document_id="doc-aks",
                tenant_id="tenant-aks-test",
                draft_only=True,
            )

            tenant_dir = target_data_dir / "tenants" / "tenant-aks-test"
            vector_path = tenant_dir / "vector_db" / "tenant-aks-test" / "approved_vectors.jsonl"
            approval_journal = tenant_dir / "repository" / "journals" / "approvals.jsonl"
            repository = JsonRepository(Settings(data_dir=tenant_dir))
            chunks = repository.get_chunks("doc-aks")

        self.assertTrue(report["passed"])
        self.assertTrue(report["draft_only"])
        self.assertFalse(report["ready_for_official_mcp"])
        self.assertEqual(0, report["approved_chunk_count"])
        self.assertEqual(0, report["approval_record_count"])
        self.assertEqual("skipped_draft_only", report["index_status"])
        self.assertFalse(vector_path.exists())
        self.assertFalse(approval_journal.exists())
        self.assertEqual(["draft"], [chunk.approval_status for chunk in chunks])
        self.assertIn("does not approve chunks", report["safety_note"])

    def test_prepare_aks_runtime_requires_human_review_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_data_dir = root / "source"
            target_data_dir = root / "target"
            _seed_source_runtime(source_data_dir)

            with self.assertRaisesRegex(ValueError, "operator_approval_reference is required"):
                prepare_aks_mcp_publish_runtime(
                    source_data_dir=source_data_dir,
                    target_data_dir=target_data_dir,
                    source_document_id="doc-aks",
                    tenant_id="tenant-aks-test",
                )

    def test_prepare_aks_runtime_preflight_failure_preserves_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_data_dir = root / "source"
            target_data_dir = root / "target"
            tenant_dir = target_data_dir / "tenants" / "tenant-aks-test"
            existing_vector = tenant_dir / "vector_db" / "tenant-aks-test" / "approved_vectors.jsonl"
            existing_vector.parent.mkdir(parents=True)
            existing_vector.write_text("keep-existing-vector\n", encoding="utf-8")
            root_manifest = target_data_dir / "repository" / "manifest.json"
            root_manifest.parent.mkdir(parents=True)
            root_manifest.write_text('{"documents":{}}\n', encoding="utf-8")
            _seed_source_runtime(source_data_dir)

            with patch(
                "scripts.prepare_aks_mcp_publish_runtime.build_publish_approval_evidence",
                side_effect=ValueError("preflight failed"),
            ):
                with self.assertRaisesRegex(ValueError, "preflight failed"):
                    prepare_aks_mcp_publish_runtime(
                        source_data_dir=source_data_dir,
                        target_data_dir=target_data_dir,
                        source_document_id="doc-aks",
                        tenant_id="tenant-aks-test",
                        operator_approval_reference="review-ticket",
                        operator_reviewer_id="reviewer",
                    )

            self.assertEqual("keep-existing-vector\n", existing_vector.read_text(encoding="utf-8"))
            self.assertTrue(root_manifest.exists())

    def test_approval_evidence_validation_blocks_runtime_sha_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vector_path = root / "approved_vectors.jsonl"
            approval_journal_path = root / "approvals.jsonl"
            evidence = {
                "worklist_report_sha256": "a" * 64,
                "review_batch_manifest_sha256": "b" * 64,
            }
            vector_path.write_text(
                json.dumps(
                    {
                        "metadata": {
                            "approval_worklist_report_sha256": "0" * 64,
                            "approval_review_batch_manifest_sha256": "b" * 64,
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            approval_journal_path.write_text(
                json.dumps(
                    {
                        "worklist_evidence": {
                            "worklist_report_sha256": "a" * 64,
                            "review_batch_manifest_sha256": "b" * 64,
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            validation = _validate_publish_runtime_approval_evidence(
                vector_path=vector_path,
                approval_journal_path=approval_journal_path,
                approval_evidence=evidence,
            )

        self.assertFalse(validation["passed"])
        vector_worklist_check = next(
            check
            for check in validation["checks"]
            if check["scope"] == "vector_metadata" and check["field"] == "approval_worklist_report_sha256"
        )
        self.assertEqual(1, vector_worklist_check["mismatch_count"])
        self.assertEqual({"0" * 64: 1}, vector_worklist_check["observed_sha256_counts"])

    def test_infers_deleted_or_omitted_article_title(self) -> None:
        self.assertEqual(("제12조", "삭제"), _infer_article("제12조삭제 <2024.1.1.>", {"article_no": "제12조"}))
        self.assertEqual(("제2조", "생략"), _infer_article("제2조 생략", {"article_no": "제2조"}))


def _seed_source_runtime(data_dir: Path) -> None:
    repository = JsonRepository(Settings(data_dir=data_dir))
    document = Document(
        document_id="doc-aks",
        filename="aks.pdf",
        file_type="pdf",
        file_hash="hash-aks",
        status="completed",
    )
    chunks = [
        Chunk(
            chunk_id="chunk-aks",
            document_id="doc-aks",
            chunk_type="article",
            text="sample regulation text",
            retrieval_text="sample regulation text",
            metadata={"article_no": "A1", "article_title": "Purpose"},
        )
    ]
    repository.upsert_document(document)
    repository.save_processing_result(document.document_id, [], chunks, [])


if __name__ == "__main__":
    unittest.main()
