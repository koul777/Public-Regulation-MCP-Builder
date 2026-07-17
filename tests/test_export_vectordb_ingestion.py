from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.export_vectordb_ingestion import export_vectordb_ingestion


class ExportVectorDbIngestionTests(unittest.TestCase):
    def test_exports_records_and_manifest_from_batch_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exports = root / "exports"
            exports.mkdir()
            document_id = "doc_1"
            chunks_path = exports / f"{document_id}.jsonl"
            chunks_path.write_text(
                json.dumps(
                    {
                        "chunk_id": "chunk-1",
                        "document_id": document_id,
                        "tenant_id": "tenant-a",
                        "retrieval_text": "??議?紐⑹쟻",
                        "document_name": "蹂듬Т洹쒖젙",
                        "source_file": "rules.pdf",
                        "chunk_type": "article",
                        "source_system": "PUBLIC_PORTAL",
                        "profile_id": "public_portal-etc-law",
                        "valid_from": "2026-01-01",
                        "approval_status": "approved",
                        "approval_id": "approval-1",
                        "approved_content_hash": "approved-chunk-1",
                        "security_level": "internal",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            quality_json = exports / f"{document_id}.quality.json"
            quality_json.write_text("{}\n", encoding="utf-8")
            report_path = root / "batch_quality.json"
            report_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-07-03T00:00:00+00:00",
                        "input_count": 1,
                        "successful_count": 1,
                        "failed_count": 0,
                        "quality_passed_count": 1,
                        "rows": [
                            {
                                "document_id": document_id,
                                "tenant_id": "tenant-a",
                                "status": "completed",
                                "quality_json": str(quality_json),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            out_jsonl = root / "vector.jsonl"
            out_manifest = root / "vector.manifest.json"

            manifest = export_vectordb_ingestion(
                report_path,
                out_jsonl=out_jsonl,
                out_manifest=out_manifest,
                fail_on_leak=True,
                require_repository_approval=False,
            )

            records = [json.loads(line) for line in out_jsonl.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(manifest["summary"]["record_count"], 1)
        self.assertEqual(manifest["summary"]["skipped_unapproved_count"], 0)
        self.assertEqual(manifest["summary"]["local_path_leak_count"], 0)
        self.assertEqual(records[0]["id"], "doc_1:chunk-1")
        self.assertEqual(records[0]["metadata"]["valid_from"], "2026-01-01")
        self.assertEqual(records[0]["metadata"]["approval_id"], "approval-1")

    def test_fail_on_leak_rejects_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exports = root / "exports"
            exports.mkdir()
            document_id = "doc_1"
            (exports / f"{document_id}.jsonl").write_text(
                json.dumps(
                    {
                        "chunk_id": "chunk-1",
                        "document_id": document_id,
                        "tenant_id": "tenant-a",
                        "retrieval_text": "??議?紐⑹쟻",
                        "source_file": "C:" + "\\Users" + "\\dd" + "\\rules.pdf",
                        "approval_status": "approved",
                        "approval_id": "approval-1",
                        "approved_content_hash": "approved-chunk-1",
                        "security_level": "internal",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            quality_json = exports / f"{document_id}.quality.json"
            quality_json.write_text("{}\n", encoding="utf-8")
            report_path = root / "batch_quality.json"
            report_path.write_text(
                json.dumps(
                    {
                        "input_count": 1,
                        "successful_count": 1,
                        "failed_count": 0,
                        "quality_passed_count": 1,
                        "rows": [
                            {
                                "document_id": document_id,
                                "tenant_id": "tenant-a",
                                "status": "completed",
                                "quality_json": str(quality_json),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "local path leaks"):
                export_vectordb_ingestion(
                    report_path,
                    out_jsonl=root / "vector.jsonl",
                    out_manifest=root / "vector.manifest.json",
                    fail_on_leak=True,
                    require_repository_approval=False,
                )
            self.assertFalse((root / "vector.jsonl").exists())
            self.assertFalse((root / "vector.manifest.json").exists())

    def test_unapproved_chunks_are_not_exported_to_vector_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exports = root / "exports"
            exports.mkdir()
            document_id = "doc_1"
            (exports / f"{document_id}.jsonl").write_text(
                json.dumps(
                    {
                        "chunk_id": "chunk-1",
                        "document_id": document_id,
                        "tenant_id": "tenant-a",
                        "retrieval_text": "??議?紐⑹쟻",
                        "approval_status": "needs_review",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            quality_json = exports / f"{document_id}.quality.json"
            quality_json.write_text("{}\n", encoding="utf-8")
            report_path = root / "batch_quality.json"
            report_path.write_text(
                json.dumps(
                    {
                        "input_count": 1,
                        "successful_count": 1,
                        "failed_count": 0,
                        "quality_passed_count": 1,
                        "rows": [
                            {
                                "document_id": document_id,
                                "tenant_id": "tenant-a",
                                "status": "completed",
                                "quality_json": str(quality_json),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            out_jsonl = root / "vector.jsonl"
            out_manifest = root / "vector.manifest.json"

            manifest = export_vectordb_ingestion(
                report_path,
                out_jsonl=out_jsonl,
                out_manifest=out_manifest,
                require_repository_approval=False,
            )
            output = out_jsonl.read_text(encoding="utf-8")

        self.assertEqual(output, "")
        self.assertEqual(manifest["summary"]["record_count"], 0)
        self.assertEqual(manifest["summary"]["skipped_unapproved_count"], 1)


if __name__ == "__main__":
    unittest.main()
