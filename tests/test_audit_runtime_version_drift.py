from __future__ import annotations

import json
import io
import tempfile
import unittest
from pathlib import Path

from app.ingestion.embedding_adapter import embed_vector_record
from app.ingestion.vector_adapter import build_vector_records, stable_content_hash
from app.processors.chunker import CHUNKER_VERSION
from scripts.audit_runtime_version_drift import build_runtime_version_drift_report, run


class AuditRuntimeVersionDriftTests(unittest.TestCase):
    def test_reports_stale_approved_chunker_versions_and_reapproval_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            repository = data_dir / "repository"
            repository.mkdir(parents=True)
            chunks = [
                _chunk("chunk-current", chunker_version=CHUNKER_VERSION),
                _chunk("chunk-stale", chunker_version="0.1.0"),
            ]
            (repository / "doc-demo_chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            vector_records, _summary = build_vector_records(chunks)
            vector_path = data_dir / "vector_db" / "default" / "approved_vectors.jsonl"
            vector_path.parent.mkdir(parents=True)
            vector_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in vector_records) + "\n",
                encoding="utf-8",
            )

            report = build_runtime_version_drift_report(
                data_dir=data_dir,
                tenant_storage_isolation=False,
            )

        self.assertTrue(report["passed"])
        self.assertIn("runtime-chunker-version-stale", [item["code"] for item in report["findings"]])
        self.assertEqual(1, report["approved_repository_stale_chunker_count"])
        self.assertEqual(1, report["vector_stale_chunker_count"])
        self.assertTrue(report["reapproval_scope"]["reprocess_requires_reapproval"])
        self.assertEqual(1, report["reapproval_scope"]["approved_chunks_with_stale_chunker_count"])
        self.assertEqual(0, report["version_loss"]["loss_count"])
        sample = report["stale_chunker_samples"][0]
        self.assertEqual("approval-chunk-stale", sample["approval_id"])
        self.assertEqual("internal", sample["security_level"])
        self.assertEqual("approved-has", sample["approved_content_hash_short"])

    def test_reports_repository_vector_version_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            repository = data_dir / "repository"
            repository.mkdir(parents=True)
            chunks = [_chunk("chunk-demo", chunker_version=CHUNKER_VERSION)]
            (repository / "doc-demo_chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            vector_records, _summary = build_vector_records([_chunk("chunk-demo", chunker_version="0.1.0")])
            vector_path = data_dir / "vector_db" / "default" / "approved_vectors.jsonl"
            vector_path.parent.mkdir(parents=True)
            vector_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in vector_records) + "\n",
                encoding="utf-8",
            )

            report = build_runtime_version_drift_report(
                data_dir=data_dir,
                tenant_storage_isolation=False,
            )

        self.assertTrue(report["passed"])
        self.assertIn("repository-vector-version-mismatch", [item["code"] for item in report["findings"]])
        self.assertEqual(1, report["version_loss"]["mismatch_count"])
        self.assertEqual(0, report["vector_integrity"]["failure_count"])

    def test_reports_clean_vector_integrity_for_valid_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            repository = data_dir / "repository"
            repository.mkdir(parents=True)
            chunks = [_chunk("chunk-demo", chunker_version=CHUNKER_VERSION)]
            (repository / "doc-demo_chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            vector_records, _summary = build_vector_records(chunks)
            vector_path = data_dir / "vector_db" / "default" / "approved_vectors.jsonl"
            vector_path.parent.mkdir(parents=True)
            vector_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in vector_records) + "\n",
                encoding="utf-8",
            )

            report = build_runtime_version_drift_report(
                data_dir=data_dir,
                tenant_storage_isolation=False,
            )

        self.assertTrue(report["passed"])
        self.assertEqual(0, report["vector_integrity"]["failure_count"])
        self.assertEqual(0, report["vector_integrity"]["content_hash_mismatch_count"])
        self.assertEqual(0, report["vector_integrity"]["verification_hash_mismatch_count"])

    def test_blocks_tampered_vector_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            repository = data_dir / "repository"
            repository.mkdir(parents=True)
            chunks = [_chunk("chunk-demo", chunker_version=CHUNKER_VERSION)]
            (repository / "doc-demo_chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            vector_records, _summary = build_vector_records(chunks)
            vector_records[0]["content_hash"] = "0" * 64
            vector_path = data_dir / "vector_db" / "default" / "approved_vectors.jsonl"
            vector_path.parent.mkdir(parents=True)
            vector_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in vector_records) + "\n",
                encoding="utf-8",
            )

            report = build_runtime_version_drift_report(
                data_dir=data_dir,
                tenant_storage_isolation=False,
            )

        self.assertFalse(report["passed"])
        self.assertIn("vector-integrity-failure", [item["code"] for item in report["findings"]])
        self.assertEqual(1, report["blocker_count"])
        self.assertEqual(1, report["vector_integrity"]["content_hash_mismatch_count"])
        self.assertEqual(1, report["vector_integrity"]["verification_hash_mismatch_count"])

    def test_blocks_invalid_vector_policy_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            repository = data_dir / "repository"
            repository.mkdir(parents=True)
            chunks = [_chunk("chunk-demo", chunker_version=CHUNKER_VERSION)]
            (repository / "doc-demo_chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            vector_records, _summary = build_vector_records(
                [
                    _chunk(
                        "chunk-demo",
                        chunker_version=CHUNKER_VERSION,
                        approval_status="draft",
                        security_level="secret",
                    )
                ],
                require_approval=False,
            )
            vector_path = data_dir / "vector_db" / "default" / "approved_vectors.jsonl"
            vector_path.parent.mkdir(parents=True)
            vector_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in vector_records) + "\n",
                encoding="utf-8",
            )

            report = build_runtime_version_drift_report(
                data_dir=data_dir,
                tenant_storage_isolation=False,
            )

        self.assertFalse(report["passed"])
        self.assertEqual(1, report["vector_integrity"]["invalid_approval_status_count"])
        self.assertEqual(1, report["vector_integrity"]["invalid_security_level_count"])
        self.assertEqual(0, report["vector_integrity"]["content_hash_mismatch_count"])

    def test_blocks_embedded_vector_dimension_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            repository = data_dir / "repository"
            repository.mkdir(parents=True)
            chunks = [_chunk("chunk-demo", chunker_version=CHUNKER_VERSION)]
            (repository / "doc-demo_chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            vector_records, _summary = build_vector_records(chunks)
            embedded = embed_vector_record(vector_records[0], dimensions=8)
            embedded["embedding_dimensions"] = 7
            vector_path = data_dir / "vector_db" / "default" / "approved_vectors.jsonl"
            vector_path.parent.mkdir(parents=True)
            vector_path.write_text(json.dumps(embedded, ensure_ascii=False) + "\n", encoding="utf-8")

            report = build_runtime_version_drift_report(
                data_dir=data_dir,
                tenant_storage_isolation=False,
            )

        self.assertFalse(report["passed"])
        self.assertEqual(1, report["vector_integrity"]["embedded_dimension_mismatch_count"])
        self.assertEqual(1, report["vector_integrity"]["embedded_integrity_failure_count"])

    def test_blocks_vector_path_leaks_and_missing_required_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            repository = data_dir / "repository"
            repository.mkdir(parents=True)
            chunks = [
                _chunk(
                    "chunk-demo",
                    chunker_version=CHUNKER_VERSION,
                    source_file="C:" + "\\Users" + "\\dd" + "\\secret.pdf",
                )
            ]
            (repository / "doc-demo_chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            vector_records, _summary = build_vector_records(chunks)
            vector_records[0]["metadata"].pop("approved_content_hash", None)
            vector_records[0]["content_hash"] = stable_content_hash(
                vector_records[0]["text"],
                vector_records[0]["metadata"],
            )
            vector_path = data_dir / "vector_db" / "default" / "approved_vectors.jsonl"
            vector_path.parent.mkdir(parents=True)
            vector_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in vector_records) + "\n",
                encoding="utf-8",
            )

            report = build_runtime_version_drift_report(
                data_dir=data_dir,
                tenant_storage_isolation=False,
            )

        self.assertFalse(report["passed"])
        self.assertIn("vector-integrity-failure", [item["code"] for item in report["findings"]])
        self.assertEqual(1, report["vector_integrity"]["metadata_missing_required_count"])
        self.assertEqual(1, report["vector_integrity"]["local_path_leak_count"])
        self.assertEqual(0, report["vector_integrity"]["content_hash_mismatch_count"])

    def test_flat_storage_filters_repository_chunks_by_tenant_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            repository = data_dir / "repository"
            repository.mkdir(parents=True)
            tenant_a_chunk = _chunk("tenant-a-current", chunker_version=CHUNKER_VERSION, tenant_id="tenant-a")
            tenant_b_chunk = _chunk("tenant-b-stale", chunker_version="0.1.0", tenant_id="tenant-b")
            (repository / "mixed_chunks.json").write_text(
                json.dumps([tenant_a_chunk, tenant_b_chunk], ensure_ascii=False),
                encoding="utf-8",
            )
            vector_records, _summary = build_vector_records([tenant_a_chunk])
            vector_path = data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            vector_path.parent.mkdir(parents=True)
            vector_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in vector_records) + "\n",
                encoding="utf-8",
            )

            report = build_runtime_version_drift_report(
                data_dir=data_dir,
                tenant_id="tenant-a",
                tenant_storage_isolation=False,
            )

        self.assertTrue(report["passed"])
        self.assertEqual(1, report["repository_chunk_count"])
        self.assertEqual(1, report["approved_repository_chunk_count"])
        self.assertEqual(1, report["vector_record_count"])
        self.assertEqual(0, report["approved_repository_stale_chunker_count"])
        self.assertEqual(0, report["vector_stale_chunker_count"])
        self.assertEqual([], report["stale_chunker_samples"])

    def test_cli_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            repository = data_dir / "repository"
            repository.mkdir(parents=True)
            chunks = [_chunk("chunk-demo", chunker_version=CHUNKER_VERSION)]
            (repository / "doc-demo_chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            vector_records, _summary = build_vector_records(chunks)
            vector_path = data_dir / "vector_db" / "default" / "approved_vectors.jsonl"
            vector_path.parent.mkdir(parents=True)
            vector_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in vector_records) + "\n",
                encoding="utf-8",
            )
            out_json = root / "report.json"
            out_md = root / "report.md"

            exit_code = run(
                [
                    "--data-dir",
                    str(data_dir),
                    "--flat-storage",
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                    "--fail-on-blocker",
                ],
                stdout=io.StringIO(),
            )
            out_json_exists = out_json.is_file()
            out_md_exists = out_md.is_file()
            written_payload = json.loads(out_json.read_text(encoding="utf-8"))

        self.assertEqual(0, exit_code)
        self.assertTrue(out_json_exists)
        self.assertTrue(out_md_exists)
        self.assertTrue(written_payload["passed"])


def _chunk(
    chunk_id: str,
    *,
    chunker_version: str,
    tenant_id: str = "default",
    approved_content_hash: str | None = "default",
    approval_status: str = "approved",
    security_level: str = "internal",
    source_file: str | None = None,
) -> dict:
    approved_hash = f"approved-hash-{chunk_id}" if approved_content_hash == "default" else approved_content_hash
    metadata = {
        "document_id": "doc-demo",
        "chunk_id": chunk_id,
        "tenant_id": tenant_id,
        "chunk_type": "article",
        "regulation_title": "Demo Regulation",
        "article_no": "Article 1",
        "article_title": "Purpose",
        "parser_version": "0.1.0",
        "chunker_version": chunker_version,
        "answer_profile_version": "reg-rag-answer-profile-v1",
        "approval_status": approval_status,
        "approval_id": f"approval-{chunk_id}",
        "security_level": security_level,
    }
    if approved_hash is not None:
        metadata["approved_content_hash"] = approved_hash
    if source_file is not None:
        metadata["source_file"] = source_file
    chunk = {
        "document_id": "doc-demo",
        "chunk_id": chunk_id,
        "tenant_id": tenant_id,
        "chunk_type": "article",
        "text": f"Text for {chunk_id}",
        "normalized_text": f"Text for {chunk_id}",
        "retrieval_text": f"Text for {chunk_id}",
        "metadata": metadata,
        "approval_status": approval_status,
        "approval_id": f"approval-{chunk_id}",
        "security_level": security_level,
    }
    if approved_hash is not None:
        chunk["approved_content_hash"] = approved_hash
    if source_file is not None:
        chunk["source_file"] = source_file
    return chunk


if __name__ == "__main__":
    unittest.main()
