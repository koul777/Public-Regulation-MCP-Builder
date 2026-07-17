from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.ingestion.embedding_adapter import EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION
from app.ingestion.vector_adapter import VECTOR_RECORD_SCHEMA_VERSION, stable_content_hash
from scripts.embed_vectordb_records import embed_vectordb_records, main


class EmbedVectorDbRecordsTests(unittest.TestCase):
    def test_embeds_records_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records_jsonl = root / "records.jsonl"
            records_jsonl.write_text(json.dumps(_record("doc:chunk-1", "text"), ensure_ascii=False) + "\n", encoding="utf-8")
            out_jsonl = root / "embedded.jsonl"
            out_manifest = root / "manifest.json"

            manifest = embed_vectordb_records(
                records_jsonl,
                out_jsonl=out_jsonl,
                out_manifest=out_manifest,
                dimensions=8,
                fail_on_leak=True,
            )

            embedded = [json.loads(line) for line in out_jsonl.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(manifest["report_type"], "vectordb_embedding")
        self.assertEqual(manifest["summary"]["record_count"], 1)
        self.assertEqual(embedded[0]["schema_version"], EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION)
        self.assertEqual(len(embedded[0]["embedding"]), 8)

    def test_fail_on_leak_rejects_local_path_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = _record("doc:chunk-1", "text")
            record["metadata"]["source_file"] = "C:" + "\\Users" + "\\dd" + "\\secret.pdf"
            records_jsonl = root / "records.jsonl"
            records_jsonl.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "local path leaks"):
                embed_vectordb_records(
                    records_jsonl,
                    out_jsonl=root / "embedded.jsonl",
                    out_manifest=root / "manifest.json",
                    dimensions=8,
                    fail_on_leak=True,
                )

    def test_main_returns_error_for_invalid_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records_jsonl = root / "records.jsonl"
            records_jsonl.write_text("{}\n", encoding="utf-8")
            with patch(
                "sys.argv",
                [
                    "embed_vectordb_records.py",
                    "--records-jsonl",
                    str(records_jsonl),
                    "--out-jsonl",
                    str(root / "embedded.jsonl"),
                    "--out-manifest",
                    str(root / "manifest.json"),
                ],
            ):
                exit_code = main()

        self.assertEqual(exit_code, 2)


def _record(record_id: str, text: str) -> dict:
    metadata = {
        "document_id": "doc",
        "tenant_id": "tenant-a",
        "chunk_id": record_id.rsplit(":", 1)[-1],
        "profile_id": "public_portal",
        "approval_status": "approved",
        "approval_id": f"approval-{record_id.rsplit(':', 1)[-1]}",
        "approved_content_hash": "d" * 64,
        "security_level": "internal",
        "approval_worklist_report_path": "reports/approval_worklist_current.json",
        "approval_worklist_report_sha256": "a" * 64,
        "approval_review_batch_manifest_path": "reports/approval_review_batches_current.json",
        "approval_review_batch_manifest_sha256": "b" * 64,
        "approval_review_batch_id": "approval-batch-001",
        "approval_review_batch_chunk_fingerprint": "c" * 64,
        "approval_review_strategy": "human_bulk_review",
    }
    return {
        "schema_version": VECTOR_RECORD_SCHEMA_VERSION,
        "id": record_id,
        "document_id": "doc",
        "tenant_id": "tenant-a",
        "chunk_id": metadata["chunk_id"],
        "text": text,
        "metadata": metadata,
        "content_hash": stable_content_hash(text, metadata),
    }


if __name__ == "__main__":
    unittest.main()
