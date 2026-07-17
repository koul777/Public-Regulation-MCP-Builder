from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from app.core.config import Settings
from app.ingestion.vector_adapter import build_vector_records
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository
from scripts.build_human_review_evidence_bundle import build_human_review_evidence_bundle, main


class BuildHumanReviewEvidenceBundleTests(unittest.TestCase):
    def test_builds_approval_and_reapproval_evidence_in_one_read_only_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            reports_dir = root / "reports"
            _seed_runtime(data_dir)
            _write_vectors_from_repository(data_dir)

            report = build_human_review_evidence_bundle(
                data_dir=data_dir,
                reports_dir=reports_dir,
                label="current",
                approval_max_chunks_per_batch=1,
                reapproval_max_chunks_per_batch=1,
                min_sample_chunks_per_tier=1,
            )
            approval_worklist = json.loads(
                (reports_dir / "approval_worklist_current.json").read_text(encoding="utf-8")
            )
            approval_batches = json.loads(
                (reports_dir / "approval_review_batches_current.json").read_text(encoding="utf-8")
            )
            reapproval_worklist = json.loads(
                (reports_dir / "reapproval_worklist_current.json").read_text(encoding="utf-8")
            )
            reapproval_burden = json.loads(
                (reports_dir / "reapproval_review_burden_current.json").read_text(encoding="utf-8")
            )
            reapproval_validation = json.loads(
                (reports_dir / "reapproval_decision_validation_current.json").read_text(encoding="utf-8")
            )
            bundle_markdown_path = reports_dir / "human_review_evidence_bundle_current.md"
            bundle_markdown_exists = bundle_markdown_path.is_file()
            bundle_markdown = bundle_markdown_path.read_text(encoding="utf-8")

        self.assertEqual("human_review_evidence_bundle", report["report_type"])
        self.assertTrue(report["passed"])
        self.assertEqual("ready_for_human_review", report["status"])
        self.assertEqual("blocked_pending_operator_decisions", report["release_gate_status"])
        self.assertEqual(1, report["summary"]["approval_chunk_count"])
        self.assertEqual(1, report["summary"]["approval_batch_count"])
        self.assertEqual(1, report["summary"]["reapproval_candidate_chunks"])
        self.assertEqual(1, report["summary"]["reapproval_review_batch_count"])
        self.assertEqual("approval_worklist", approval_worklist["report_type"])
        self.assertEqual(1, approval_worklist["manual_attention_chunks"])
        self.assertEqual("approval_review_batch_manifest", approval_batches["report_type"])
        self.assertEqual(1, approval_batches["approval_chunk_count"])
        self.assertEqual(1, approval_batches["manual_attention_chunks"])
        self.assertEqual("reapproval_worklist", reapproval_worklist["report_type"])
        self.assertEqual(1, reapproval_worklist["reapproval_candidate_chunks"])
        self.assertEqual("reapproval_review_burden", reapproval_burden["report_type"])
        self.assertEqual("reapproval_decision_validation", reapproval_validation["report_type"])
        self.assertEqual(
            "blocked_pending_operator_decisions",
            report["summary"]["reapproval_decision_validation_status"],
        )
        self.assertTrue(report["approval_journal_contract"]["required_for_official_rag_mcp"])
        self.assertFalse(report["approval_journal_contract"]["bundle_writes_journal_records"])
        self.assertEqual(
            str(data_dir / "repository" / "journals" / "approvals.jsonl"),
            report["approval_journal_contract"]["journal_path"],
        )
        self.assertIn("append-only approval journal records", report["approval_journal_contract"]["vector_indexing_requirement"])
        self.assertIn("reg-rag-reapproval-apply-plan", json.dumps(report["next_steps"], ensure_ascii=False))
        self.assertIn("does not approve chunks", report["safety_note"])
        self.assertIn("write append-only approval journal records", report["safety_note"])
        self.assertTrue(bundle_markdown_exists)
        self.assertIn("Approval Journal Contract", bundle_markdown)
        self.assertEqual(
            str(reports_dir / "approval_worklist_current.json"),
            report["product_readiness_inputs"]["approval_worklist_report"],
        )
        self.assertEqual(
            str(reports_dir / "reapproval_review_batches_current.json"),
            report["product_readiness_inputs"]["reapproval_review_batch_report"],
        )
        self.assertEqual(
            str(reports_dir / "reapproval_decision_validation_current.json"),
            report["product_readiness_inputs"]["reapproval_decision_validation_report"],
        )
        self.assertEqual(64, len(report["artifacts"]["approval_worklist_json"]["sha256"]))

    def test_cli_writes_custom_bundle_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            reports_dir = root / "reports"
            out_json = root / "human_bundle.json"
            _seed_runtime(data_dir)
            _write_vectors_from_repository(data_dir)
            stdout = io.StringIO()

            exit_code = main(
                [
                    "--data-dir",
                    str(data_dir),
                    "--reports-dir",
                    str(reports_dir),
                    "--label",
                    "cli",
                    "--out-json",
                    str(out_json),
                    "--fail-on-technical-blocker",
                ],
                stdout=stdout,
            )
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            approval_worklist_exists = (reports_dir / "approval_worklist_cli.json").is_file()
            reapproval_bundle_exists = (reports_dir / "reapproval_evidence_bundle_cli.json").is_file()
            reapproval_validation_exists = (reports_dir / "reapproval_decision_validation_cli.json").is_file()

        self.assertEqual(0, exit_code)
        self.assertEqual("human_review_evidence_bundle", payload["report_type"])
        self.assertIn('"human_review_evidence_bundle"', stdout.getvalue())
        self.assertTrue(approval_worklist_exists)
        self.assertTrue(reapproval_bundle_exists)
        self.assertTrue(reapproval_validation_exists)


def _seed_runtime(data_dir: Path) -> None:
    repository = JsonRepository(Settings(data_dir=data_dir))
    repository.upsert_document(
        Document(
            document_id="doc-demo",
            filename="demo.pdf",
            document_name="Demo Regulation",
            file_type="pdf",
            file_hash="hash-demo",
            institution_name="Institution",
            apba_id="C0001",
            source_system="PUBLIC_PORTAL",
            source_record_id="record-demo",
            source_file_id="file-demo",
            profile_id="public_portal-c0001",
        )
    )
    repository.save_processing_result(
        "doc-demo",
        [],
        [
            _chunk("draft-1", approval_status="draft", chunker_version="0.1.5"),
            _chunk("approved-stale", approval_status="approved", chunker_version="0.1.0"),
        ],
        [],
    )


def _chunk(chunk_id: str, *, approval_status: str, chunker_version: str) -> Chunk:
    metadata = {
        "document_id": "doc-demo",
        "chunk_id": chunk_id,
        "tenant_id": "default",
        "source_system": "PUBLIC_PORTAL",
        "apba_id": "C0001",
        "profile_id": "public_portal-c0001",
        "parser_version": "0.1.0",
        "chunker_version": chunker_version,
        "answer_profile_version": "reg-rag-answer-profile-v1",
        "approval_status": approval_status,
        "security_level": "internal",
    }
    kwargs = {
        "chunk_id": chunk_id,
        "document_id": "doc-demo",
        "chunk_type": "article",
        "text": f"Text for {chunk_id}",
        "retrieval_text": f"Text for {chunk_id}",
        "approval_status": approval_status,
        "security_level": "internal",
        "metadata": metadata,
    }
    if approval_status == "approved":
        metadata.update(
            {
                "approval_id": f"approval-{chunk_id}",
                "approved_content_hash": f"approved-hash-{chunk_id}",
                "approval_worklist_report_path": "reports/approval_worklist_current.json",
                "approval_worklist_report_sha256": "a" * 64,
                "approval_review_batch_manifest_path": "reports/approval_review_batches_current.json",
                "approval_review_batch_manifest_sha256": "b" * 64,
                "approval_review_batch_id": f"approval-batch-{chunk_id}",
                "approval_review_batch_chunk_fingerprint": "c" * 64,
                "approval_review_strategy": "human_bulk_review",
            }
        )
        kwargs["approval_id"] = f"approval-{chunk_id}"
        kwargs["approved_content_hash"] = f"approved-hash-{chunk_id}"
    return Chunk(**kwargs)


def _write_vectors_from_repository(data_dir: Path) -> None:
    repository = JsonRepository(Settings(data_dir=data_dir))
    chunks = [chunk.model_dump(mode="json") for chunk in repository.get_chunks("doc-demo")]
    vector_records, _summary = build_vector_records(chunks)
    vector_path = data_dir / "vector_db" / "default" / "approved_vectors.jsonl"
    vector_path.parent.mkdir(parents=True)
    vector_path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in vector_records) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
