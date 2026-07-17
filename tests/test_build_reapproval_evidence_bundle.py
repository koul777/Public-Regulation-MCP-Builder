from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from app.core.config import Settings
from app.ingestion.vector_adapter import build_vector_records
from app.processors.chunker import CHUNKER_VERSION
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository
from scripts.build_reapproval_evidence_bundle import build_reapproval_evidence_bundle, main


class BuildReapprovalEvidenceBundleTests(unittest.TestCase):
    def test_builds_reapproval_evidence_without_reapproving_or_indexing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            reports_dir = root / "reports"
            _seed_document(data_dir, chunker_version="0.1.0")
            _write_vectors_from_repository(data_dir)

            report = build_reapproval_evidence_bundle(
                data_dir=data_dir,
                reports_dir=reports_dir,
                label="current",
                review_batch_size=1,
                max_chunks_per_batch=1,
                min_sample_chunks_per_tier=1,
            )
            worklist = json.loads((reports_dir / "reapproval_worklist_current.json").read_text(encoding="utf-8"))
            batches = json.loads((reports_dir / "reapproval_review_batches_current.json").read_text(encoding="utf-8"))
            burden = json.loads((reports_dir / "reapproval_review_burden_current.json").read_text(encoding="utf-8"))
            validation = json.loads(
                (reports_dir / "reapproval_decision_validation_current.json").read_text(encoding="utf-8")
            )
            with (reports_dir / "reapproval_review_batch_decisions_current.csv").open(encoding="utf-8-sig") as handle:
                decision_rows = list(csv.DictReader(handle))
            runtime_drift_exists = (reports_dir / "runtime_version_drift_current.json").is_file()
            bundle_markdown_exists = (reports_dir / "reapproval_evidence_bundle_current.md").is_file()

        self.assertEqual("reapproval_evidence_bundle", report["report_type"])
        self.assertTrue(report["passed"])
        self.assertEqual("ready_for_operator_decisions", report["status"])
        self.assertEqual("blocked_pending_operator_decisions", report["release_gate_status"])
        self.assertEqual(1, report["summary"]["reapproval_candidate_chunks"])
        self.assertEqual(1, report["summary"]["review_batch_count"])
        self.assertEqual(1, report["summary"]["decision_template_operator_decision_blank_count"])
        self.assertTrue(report["summary"]["runtime_version_drift_generated"])
        self.assertEqual("reapproval_worklist", worklist["report_type"])
        self.assertEqual(1, worklist["reapproval_candidate_chunks"])
        self.assertEqual("reapproval_review_batch_manifest", batches["report_type"])
        self.assertEqual(1, batches["reapproval_chunk_count"])
        self.assertEqual("", decision_rows[0]["operator_decision"])
        self.assertEqual("reapproval_review_burden", burden["report_type"])
        self.assertEqual("blocked_pending_operator_decisions", burden["release_gate_status"])
        self.assertEqual("reapproval_decision_validation", validation["report_type"])
        self.assertEqual("blocked_pending_operator_decisions", validation["release_gate_status"])
        self.assertEqual(1, report["summary"]["decision_validation_blocking_count"])
        self.assertIn("build_reapproval_apply_plan", {step["step"] for step in report["next_steps"]})
        self.assertIn("does not reprocess files, approve chunks", report["safety_note"])
        self.assertEqual(
            str(reports_dir / "reapproval_worklist_current.json"),
            report["product_readiness_inputs"]["reapproval_worklist_report"],
        )
        self.assertEqual(
            str(reports_dir / "reapproval_decision_validation_current.json"),
            report["product_readiness_inputs"]["reapproval_decision_validation_report"],
        )
        self.assertTrue(runtime_drift_exists)
        self.assertTrue(bundle_markdown_exists)
        self.assertEqual(64, len(report["artifacts"]["reapproval_worklist_json"]["sha256"]))

    def test_uses_supplied_runtime_drift_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            reports_dir = root / "reports"
            drift = root / "existing_runtime_drift.json"
            _seed_document(data_dir, chunker_version=CHUNKER_VERSION)
            _write_vectors_from_repository(data_dir)
            drift.write_text(
                json.dumps(
                    {
                        "report_type": "runtime_version_drift",
                        "generated_at": "2026-07-10T00:00:00+00:00",
                        "passed": True,
                        "warning_count": 0,
                        "blocker_count": 0,
                        "vector_integrity": {"failure_count": 0},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            report = build_reapproval_evidence_bundle(
                data_dir=data_dir,
                reports_dir=reports_dir,
                label="supplied",
                runtime_version_drift_report=drift,
            )

        self.assertFalse(report["summary"]["runtime_version_drift_generated"])
        self.assertIsNone(report["summary"]["runtime_version_drift_passed"])
        self.assertEqual(str(drift), report["product_readiness_inputs"]["runtime_version_drift_report"])
        self.assertFalse((reports_dir / "runtime_version_drift_supplied.json").exists())
        self.assertEqual("no_reapproval_candidates", report["status"])
        self.assertEqual("ready_for_release_gate", report["release_gate_status"])

    def test_cli_writes_bundle_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            reports_dir = root / "reports"
            out_json = root / "custom_bundle.json"
            _seed_document(data_dir, chunker_version="0.1.0")
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
            burden_exists = (reports_dir / "reapproval_review_burden_cli.json").is_file()
            validation_exists = (reports_dir / "reapproval_decision_validation_cli.json").is_file()

        self.assertEqual(0, exit_code)
        self.assertEqual("reapproval_evidence_bundle", payload["report_type"])
        self.assertIn('"reapproval_evidence_bundle"', stdout.getvalue())
        self.assertTrue(burden_exists)
        self.assertTrue(validation_exists)


def _seed_document(data_dir: Path, *, chunker_version: str) -> JsonRepository:
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
            Chunk(
                chunk_id="chunk-1",
                document_id="doc-demo",
                chunk_type="article",
                text="Demo text",
                retrieval_text="Demo text",
                approval_status="approved",
                approval_id="approval-1",
                approved_content_hash="approved-hash-1",
                security_level="internal",
                metadata={
                    "document_id": "doc-demo",
                    "chunk_id": "chunk-1",
                    "tenant_id": "default",
                    "source_system": "PUBLIC_PORTAL",
                    "apba_id": "C0001",
                    "profile_id": "public_portal-c0001",
                    "parser_version": "0.1.0",
                    "chunker_version": chunker_version,
                    "answer_profile_version": "reg-rag-answer-profile-v1",
                    "approval_status": "approved",
                    "approval_id": "approval-1",
                    "approved_content_hash": "approved-hash-1",
                    "security_level": "internal",
                    "approval_worklist_report_path": "reports/approval_worklist_current.json",
                    "approval_worklist_report_sha256": "a" * 64,
                    "approval_review_batch_manifest_path": "reports/approval_review_batches_current.json",
                    "approval_review_batch_manifest_sha256": "b" * 64,
                    "approval_review_batch_id": "approval-batch-1",
                    "approval_review_batch_chunk_fingerprint": "c" * 64,
                    "approval_review_strategy": "human_bulk_review",
                },
            )
        ],
        [],
    )
    return repository


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
