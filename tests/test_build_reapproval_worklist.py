from __future__ import annotations

import io
import csv
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
from scripts.build_reapproval_worklist import build_reapproval_worklist, main


class BuildReapprovalWorklistTests(unittest.TestCase):
    def test_groups_stale_approved_chunks_without_changing_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            repository = _seed_document(
                data_dir,
                chunks=[
                    _chunk("current", chunker_version=CHUNKER_VERSION),
                    _chunk("stale", chunker_version="0.1.0", effective_date="2026-01-01"),
                ],
            )
            chunks = [chunk.model_dump(mode="json") for chunk in repository.get_chunks("doc-demo")]
            vector_records, _summary = build_vector_records(chunks)
            _write_vectors(data_dir, vector_records)
            drift_report = _write_drift_report(root / "runtime_version_drift.json")

            report = build_reapproval_worklist(
                data_dir=data_dir,
                runtime_version_drift_report=drift_report,
                review_batch_size=1,
                review_seconds_per_chunk=30,
            )

        self.assertEqual("reapproval_worklist", report["report_type"])
        self.assertEqual(1, report["document_count"])
        self.assertEqual(2, report["total_approved_chunks"])
        self.assertEqual(1, report["reapproval_candidate_chunks"])
        self.assertEqual(1, report["vector_records_for_candidates"])
        self.assertEqual(1, report["vector_stale_for_candidates"])
        self.assertEqual(1, report["temporal_metadata_chunks"])
        self.assertEqual({"high": 0, "medium": 1, "low": 0}, report["review_triage_counts"])
        self.assertEqual(0, report["high_risk_candidate_chunks"])
        self.assertEqual(1, report["temporal_sample_candidate_chunks"])
        self.assertEqual(0, report["low_risk_candidate_chunks"])
        self.assertEqual(1, report["recommended_initial_review_chunks"])
        self.assertEqual(1, report["estimated_review_batches"])
        self.assertEqual(1, report["estimated_review_minutes"])
        self.assertEqual(1, report["estimated_initial_review_minutes"])
        self.assertEqual("temporal_metadata_sample_review", report["documents"][0]["review_strategy"])
        self.assertEqual("reprocess_then_reapprove_and_reindex", report["documents"][0]["suggested_action"])
        self.assertEqual("medium", report["documents"][0]["chunk_samples"][0]["review_risk_tier"])
        self.assertIn("chunker_version_stale", report["documents"][0]["top_reapproval_reasons"])
        self.assertIn("vector_chunker_version_stale", report["documents"][0]["top_reapproval_reasons"])
        self.assertEqual("runtime_version_drift", report["source_runtime_version_drift_report"]["report_type"])
        self.assertEqual(0, report["source_vector_integrity_failure_count"])
        self.assertEqual(0, report["source_runtime_version_drift_report"]["vector_integrity"]["failure_count"])
        self.assertIn("This worklist is read-only", report["safety_note"])

    def test_vector_gap_is_prioritized_before_reapproval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            _seed_document(data_dir, chunks=[_chunk("stale", chunker_version="0.1.0")])

            report = build_reapproval_worklist(data_dir=data_dir)

        self.assertEqual(1, report["document_count"])
        self.assertEqual(1, report["vector_missing_for_candidates"])
        self.assertEqual(1, report["high_risk_candidate_chunks"])
        self.assertEqual(1, report["recommended_initial_review_chunks"])
        self.assertEqual("reconcile_vector_gap_then_reapprove", report["documents"][0]["suggested_action"])
        self.assertEqual("full_review_high_risk_then_sample_remaining", report["documents"][0]["review_strategy"])
        self.assertIn("vector_record_missing", report["documents"][0]["top_reapproval_reasons"])

    def test_vector_approval_provenance_gap_is_reapproval_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository = _seed_document(
                data_dir,
                chunks=[
                    _chunk(
                        "current",
                        chunker_version=CHUNKER_VERSION,
                        include_approval_provenance=False,
                    )
                ],
            )
            chunks = [chunk.model_dump(mode="json") for chunk in repository.get_chunks("doc-demo")]
            vector_records, _summary = build_vector_records(chunks)
            _write_vectors(data_dir, vector_records)

            report = build_reapproval_worklist(data_dir=data_dir)

        self.assertEqual(1, report["reapproval_candidate_chunks"])
        self.assertEqual(1, report["approval_provenance_missing_chunks"])
        self.assertEqual(1, report["approval_provenance_only_chunks"])
        self.assertEqual(
            {
                "approval_worklist_report_path",
                "approval_worklist_report_sha256",
                "approval_review_batch_manifest_path",
                "approval_review_batch_manifest_sha256",
                "approval_review_batch_id",
                "approval_review_batch_chunk_fingerprint",
                "approval_review_strategy",
            },
            set(report["approval_provenance_missing_field_counts"]),
        )
        self.assertEqual(
            1,
            report["approval_provenance_missing_field_counts"]["approval_worklist_report_sha256"],
        )
        self.assertEqual("reapprove_and_reindex", report["documents"][0]["suggested_action"])
        self.assertEqual(1, report["documents"][0]["approval_provenance_missing_chunks"])
        self.assertEqual("low", report["documents"][0]["chunk_samples"][0]["review_risk_tier"])
        self.assertEqual(
            "version_only_sample_then_operator_reapproval",
            report["documents"][0]["chunk_samples"][0]["review_strategy"],
        )
        self.assertIn(
            "approval_worklist_report_sha256",
            report["documents"][0]["chunk_samples"][0]["approval_provenance_missing_fields"],
        )
        self.assertIn(
            "approval_provenance_approval_worklist_report_sha256_missing",
            report["documents"][0]["chunk_samples"][0]["reapproval_reasons"],
        )

    def test_vector_approval_hash_mismatch_is_high_risk_reapproval_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository = _seed_document(
                data_dir,
                chunks=[_chunk("current", chunker_version=CHUNKER_VERSION)],
            )
            chunks = [chunk.model_dump(mode="json") for chunk in repository.get_chunks("doc-demo")]
            vector_records, _summary = build_vector_records(chunks)
            vector_records[0]["metadata"]["approved_content_hash"] = "stale-approved-hash"
            _write_vectors(data_dir, vector_records)

            report = build_reapproval_worklist(data_dir=data_dir)

        self.assertEqual(1, report["reapproval_candidate_chunks"])
        self.assertEqual(1, report["high_risk_candidate_chunks"])
        self.assertEqual("reprocess_then_reapprove_and_reindex", report["documents"][0]["suggested_action"])
        self.assertEqual("full_review_high_risk_then_sample_remaining", report["documents"][0]["review_strategy"])
        self.assertEqual("high", report["documents"][0]["chunk_samples"][0]["review_risk_tier"])
        self.assertIn(
            "repository_vector_approved_content_hash_mismatch",
            report["documents"][0]["chunk_samples"][0]["reapproval_reasons"],
        )

    def test_reapproval_action_order_prioritizes_hash_vector_and_provenance_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            _seed_document(
                data_dir,
                document_id="doc-hash",
                chunks=[
                    _chunk(
                        "missing-hash",
                        document_id="doc-hash",
                        chunker_version=CHUNKER_VERSION,
                        approved_content_hash=None,
                    ),
                ],
            )
            _seed_document(
                data_dir,
                document_id="doc-vector",
                chunks=[_chunk("vector-missing", document_id="doc-vector", chunker_version=CHUNKER_VERSION)],
            )
            _seed_document(
                data_dir,
                document_id="doc-provenance",
                chunks=[
                    _chunk(
                        "provenance-only",
                        document_id="doc-provenance",
                        chunker_version=CHUNKER_VERSION,
                        include_approval_provenance=False,
                    )
                ],
            )
            _seed_document(
                data_dir,
                document_id="doc-stale",
                chunks=[_chunk("stale", document_id="doc-stale", chunker_version="0.1.0")],
            )
            repository = JsonRepository(Settings(data_dir=data_dir))
            chunks = []
            for document_id in ("doc-hash", "doc-provenance", "doc-stale"):
                chunks.extend(chunk.model_dump(mode="json") for chunk in repository.get_chunks(document_id))
            vector_records, _summary = build_vector_records(chunks)
            _write_vectors(data_dir, vector_records)

            report = build_reapproval_worklist(data_dir=data_dir)

        self.assertEqual(
            [
                "inspect_missing_approval_hash_first",
                "reconcile_vector_gap_then_reapprove",
                "reapprove_and_reindex",
                "reprocess_then_reapprove_and_reindex",
            ],
            [row["suggested_action"] for row in report["documents"]],
        )

    def test_triage_separates_high_temporal_and_low_risk_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository = _seed_document(
                data_dir,
                chunks=[
                    _chunk("low", chunker_version="0.1.0"),
                    _chunk("temporal", chunker_version="0.1.0", effective_date="2026-01-01"),
                    _chunk("missing-hash", chunker_version="0.1.0", approved_content_hash=None),
                ],
            )
            chunks = [chunk.model_dump(mode="json") for chunk in repository.get_chunks("doc-demo")]
            vector_records, _summary = build_vector_records(chunks)
            _write_vectors(data_dir, vector_records)

            report = build_reapproval_worklist(
                data_dir=data_dir,
                low_risk_sample_rate=0.1,
                temporal_sample_rate=0.2,
                min_sample_chunks_per_tier=1,
            )

        self.assertEqual(3, report["reapproval_candidate_chunks"])
        self.assertEqual({"high": 1, "medium": 1, "low": 1}, report["review_triage_counts"])
        self.assertEqual(1, report["high_risk_candidate_chunks"])
        self.assertEqual(1, report["temporal_sample_candidate_chunks"])
        self.assertEqual(1, report["low_risk_candidate_chunks"])
        self.assertEqual(3, report["recommended_initial_review_chunks"])
        self.assertEqual("full_review_high_risk_then_sample_remaining", report["documents"][0]["review_strategy"])
        tiers = {row["chunk_id"]: row["review_risk_tier"] for row in report["documents"][0]["chunk_samples"]}
        self.assertEqual("low", tiers["low"])
        self.assertEqual("medium", tiers["temporal"])
        self.assertEqual("high", tiers["missing-hash"])

    def test_source_vector_integrity_failure_blocks_reapproval_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            repository = _seed_document(data_dir, chunks=[_chunk("stale", chunker_version="0.1.0")])
            chunks = [chunk.model_dump(mode="json") for chunk in repository.get_chunks("doc-demo")]
            vector_records, _summary = build_vector_records(chunks)
            _write_vectors(data_dir, vector_records)
            drift_report = _write_drift_report(root / "runtime_version_drift.json", vector_integrity_failure_count=1)

            report = build_reapproval_worklist(
                data_dir=data_dir,
                runtime_version_drift_report=drift_report,
            )

        self.assertEqual(1, report["source_vector_integrity_failure_count"])
        self.assertEqual(1, report["source_runtime_version_drift_report"]["vector_integrity"]["failure_count"])
        self.assertEqual("fix_vector_integrity_before_reapproval", report["documents"][0]["suggested_action"])
        self.assertEqual("source-vector-integrity-failure", report["pre_reapproval_blockers"][0]["code"])

    def test_cli_writes_json_csv_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            repository = _seed_document(data_dir, chunks=[_chunk("stale", chunker_version="0.1.0")])
            chunks = [chunk.model_dump(mode="json") for chunk in repository.get_chunks("doc-demo")]
            vector_records, _summary = build_vector_records(chunks)
            _write_vectors(data_dir, vector_records)
            out_json = root / "worklist.json"
            out_csv = root / "worklist.csv"
            out_chunks_csv = root / "worklist_chunks.csv"
            out_chunks_json = root / "worklist_chunks.json"
            out_md = root / "worklist.md"

            exit_code = main(
                [
                    "--data-dir",
                    str(data_dir),
                    "--out-json",
                    str(out_json),
                    "--out-csv",
                    str(out_csv),
                    "--out-chunks-csv",
                    str(out_chunks_csv),
                    "--out-chunks-json",
                    str(out_chunks_json),
                    "--out-md",
                    str(out_md),
                ],
                stdout=io.StringIO(),
            )
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            chunks_payload = json.loads(out_chunks_json.read_text(encoding="utf-8"))
            csv_exists = out_csv.is_file()
            with out_chunks_csv.open(encoding="utf-8-sig") as handle:
                chunk_rows = list(csv.DictReader(handle))
            md_exists = out_md.is_file()

        self.assertEqual(0, exit_code)
        self.assertTrue(csv_exists)
        self.assertEqual(1, len(chunk_rows))
        self.assertEqual("stale", chunk_rows[0]["chunk_id"])
        self.assertEqual("1", chunk_rows[0]["document_rank"])
        self.assertEqual("reprocess_then_reapprove_and_reindex", chunk_rows[0]["suggested_action"])
        self.assertIn("chunker_version_stale", chunk_rows[0]["reapproval_reasons"])
        self.assertTrue(md_exists)
        self.assertEqual(1, payload["reapproval_candidate_chunks"])
        self.assertEqual(1, payload["chunk_candidate_export_count"])
        self.assertIn("reapproval_reasons", payload["chunk_candidate_export_fields"])
        self.assertIn("recommended_initial_review_chunks", payload)
        self.assertEqual("reapproval_worklist_chunk_candidates", chunks_payload["report_type"])
        self.assertEqual(1, chunks_payload["candidate_count"])
        self.assertIn("chunk_id", chunks_payload["fields"])
        self.assertEqual("stale", chunks_payload["candidates"][0]["chunk_id"])
        self.assertRegex(chunks_payload["candidates"][0]["review_content_hash"], r"^[a-f0-9]{64}$")
        self.assertIn("chunker_version_stale", chunks_payload["candidates"][0]["reapproval_reasons"])


def _seed_document(data_dir: Path, *, chunks: list[Chunk], document_id: str = "doc-demo") -> JsonRepository:
    settings = Settings(data_dir=data_dir)
    repository = JsonRepository(settings)
    repository.upsert_document(
        Document(
            document_id=document_id,
            filename=f"{document_id}.pdf",
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
    repository.save_processing_result(document_id, [], chunks, [])
    return repository


def _chunk(
    chunk_id: str,
    *,
    chunker_version: str,
    document_id: str = "doc-demo",
    effective_date: str | None = None,
    approved_content_hash: str | None = "default",
    include_approval_provenance: bool = True,
) -> Chunk:
    approved_hash = f"approved-hash-{chunk_id}" if approved_content_hash == "default" else approved_content_hash
    metadata = {
        "document_id": document_id,
        "chunk_id": chunk_id,
        "tenant_id": "default",
        "source_system": "PUBLIC_PORTAL",
        "apba_id": "C0001",
        "profile_id": "public_portal-c0001",
        "chunk_type": "article",
        "regulation_title": "Demo Regulation",
        "article_no": "Article 1",
        "parser_version": "0.1.0",
        "chunker_version": chunker_version,
        "answer_profile_version": "reg-rag-answer-profile-v1",
        "approval_status": "approved",
        "approval_id": f"approval-{chunk_id}",
        "security_level": "internal",
    }
    if approved_hash is not None:
        metadata["approved_content_hash"] = approved_hash
    if include_approval_provenance:
        metadata.update(
            {
                "approval_worklist_report_path": "reports/approval_worklist.json",
                "approval_worklist_report_sha256": "worklist-sha256",
                "approval_review_batch_manifest_path": "reports/approval_review_batches.json",
                "approval_review_batch_manifest_sha256": "manifest-sha256",
                "approval_review_batch_id": f"approval-batch-{chunk_id}",
                "approval_review_batch_chunk_fingerprint": f"fingerprint-{chunk_id}",
                "approval_review_strategy": "document",
            }
        )
    if effective_date:
        metadata["effective_date"] = effective_date
    chunk_kwargs = {
        "chunk_id": chunk_id,
        "document_id": document_id,
        "chunk_type": "article",
        "text": f"Text for {chunk_id}",
        "retrieval_text": f"Text for {chunk_id}",
        "metadata": metadata,
        "approval_status": "approved",
        "approval_id": f"approval-{chunk_id}",
        "security_level": "internal",
    }
    if approved_hash is not None:
        chunk_kwargs["approved_content_hash"] = approved_hash
    return Chunk(
        **chunk_kwargs,
    )


def _write_vectors(data_dir: Path, records: list[dict]) -> None:
    vector_path = data_dir / "vector_db" / "default" / "approved_vectors.jsonl"
    vector_path.parent.mkdir(parents=True)
    vector_path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )


def _write_drift_report(path: Path, *, vector_integrity_failure_count: int = 0) -> Path:
    path.write_text(
        json.dumps(
            {
                "report_type": "runtime_version_drift",
                "generated_at": "2026-07-09T12:00:00+00:00",
                "passed": True,
                "warning_count": 1,
                "blocker_count": 0,
                "approved_repository_stale_chunker_count": 1,
                "vector_stale_chunker_count": 1,
                "vector_integrity": {
                    "failure_count": vector_integrity_failure_count,
                    "content_hash_mismatch_count": vector_integrity_failure_count,
                },
                "reapproval_scope": {
                    "reprocess_requires_reapproval": True,
                    "approved_chunks_with_stale_chunker_count": 1,
                },
                "current_versions": {"chunker_version": CHUNKER_VERSION},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


if __name__ == "__main__":
    unittest.main()
