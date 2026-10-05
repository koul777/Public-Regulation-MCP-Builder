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



class OfficialApprovedExportTests(unittest.TestCase):
    def test_official_export_accepts_genuine_approved_record_and_rejects_tampered_payload(self) -> None:
        import copy
        from test_approval_validation import approved_ingestion_fixture

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings, _repository, _auth, document, _chunk, exported, _record = approved_ingestion_fixture(root)
            exports = root / "exports"
            exports.mkdir()
            chunks_path = exports / f"{document.document_id}.jsonl"
            quality = exports / f"{document.document_id}.quality.json"
            quality.write_text("{}", encoding="utf-8")
            report = root / "batch.json"
            report.write_text(json.dumps({"rows": [{"status": "completed", "document_id": document.document_id,
                                                     "quality_json": str(quality)}]}), encoding="utf-8")
            chunks_path.write_text(json.dumps(exported) + "\n", encoding="utf-8")
            result = export_vectordb_ingestion(
                report, out_jsonl=root / "approved.jsonl", out_manifest=root / "approved-manifest.json",
                data_dir=settings.data_dir, tenant_id="tenant-a", require_repository_approval=True,
            )
            self.assertEqual(1, result["summary"]["record_count"])
            for field, value in {"text": "changed text", "retrieval_text": "changed retrieval",
                                 "department_acl": ["other-department"], "security_level": "public",
                                 "profile_id": "profile-b"}.items():
                with self.subTest(field=field):
                    changed = copy.deepcopy(exported)
                    if field == "profile_id":
                        changed["metadata"][field] = value
                    else:
                        changed[field] = value
                    chunks_path.write_text(json.dumps(changed) + "\n", encoding="utf-8")
                    rejected_output = root / f"rejected-{field}.jsonl"
                    with self.assertRaises(ValueError):
                        export_vectordb_ingestion(
                            report, out_jsonl=rejected_output, out_manifest=root / f"rejected-{field}.json",
                            data_dir=settings.data_dir, tenant_id="tenant-a", require_repository_approval=True,
                        )
                    self.assertFalse(rejected_output.exists())


if __name__ == "__main__":
    unittest.main()
