from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from app.core.config import Settings
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository
from scripts.build_approval_review_batches import build_approval_review_batches, main
from scripts.build_approval_worklist import build_approval_worklist


class BuildApprovalReviewBatchesTests(unittest.TestCase):
    def test_builds_chunk_id_batches_with_worklist_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data")
            _seed_document(
                settings,
                [
                    Chunk(
                        chunk_id="clean-1",
                        document_id="doc-a",
                        chunk_type="article",
                        text="clean one",
                        metadata={"article_no": "1", "article_title": "Purpose"},
                    ),
                    Chunk(
                        chunk_id="clean-2",
                        document_id="doc-a",
                        chunk_type="paragraph",
                        text="preamble",
                        warnings=["orphan_preamble_text"],
                    ),
                    Chunk(
                        chunk_id="attention-1",
                        document_id="doc-a",
                        chunk_type="table",
                        text="table",
                        metadata={"table_review_required": True, "table_review_flags": ["row_review_required"]},
                    ),
                ],
            )
            worklist_path = root / "reports" / "approval_worklist.json"
            worklist_path.parent.mkdir(parents=True)
            worklist = build_approval_worklist(data_dir=settings.data_dir, source_system="PUBLIC_PORTAL")
            worklist_path.write_text(json.dumps(worklist, ensure_ascii=False, indent=2), encoding="utf-8")
            expected_sha = hashlib.sha256(worklist_path.read_bytes()).hexdigest()

            report = build_approval_review_batches(
                data_dir=settings.data_dir,
                worklist_report=worklist_path,
                worklist_report_artifact_path="reports/approval_worklist.json",
                max_chunks_per_batch=1,
            )
            out_json = root / "reports" / "approval_review_batches.json"
            out_csv = root / "reports" / "approval_review_batches.csv"
            out_md = root / "reports" / "approval_review_batches.md"
            stdout = io.StringIO()
            exit_code = main(
                [
                    "--data-dir",
                    str(settings.data_dir),
                    "--worklist-report",
                    str(worklist_path),
                    "--worklist-report-artifact-path",
                    "reports/approval_worklist.json",
                    "--max-chunks-per-batch",
                    "2",
                    "--out-json",
                    str(out_json),
                    "--out-csv",
                    str(out_csv),
                    "--out-md",
                    str(out_md),
                    "--fail-on-issue",
                ],
                stdout=stdout,
            )
            out_json_exists = out_json.is_file()
            out_csv_exists = out_csv.is_file()
            out_md_exists = out_md.is_file()
            stdout_value = stdout.getvalue()

        self.assertTrue(report["passed"])
        self.assertEqual(3, report["batch_count"])
        self.assertEqual(1, report["manual_attention_chunks"])
        self.assertEqual(2, report["low_risk_batch_review_candidate_chunks"])
        self.assertEqual(expected_sha, report["worklist_report"]["sha256"])
        self.assertEqual("reports/approval_worklist.json", report["worklist_report"]["approval_request_path"])
        by_type = {batch["review_type"]: batch for batch in report["batches"]}
        self.assertEqual("operator_manual_review", by_type["manual_attention"]["review_strategy"])
        self.assertTrue(by_type["manual_attention"]["review_flags_acknowledged_required"])
        self.assertEqual("human_bulk_review", by_type["low_risk_batch"]["review_strategy"])
        for batch in report["batches"]:
            template = batch["approval_request_template"]
            self.assertEqual(64, len(batch["review_batch_chunk_fingerprint"]))
            self.assertTrue(batch["review_batch_id"].endswith(batch["review_batch_chunk_fingerprint"][:12]))
            self.assertEqual("reports/approval_worklist.json", template["worklist_report_path"])
            self.assertEqual(expected_sha, template["worklist_report_sha256"])
            self.assertEqual(batch["review_batch_id"], template["review_batch_id"])
            self.assertEqual(
                batch["review_batch_chunk_fingerprint"],
                template["review_batch_chunk_fingerprint"],
            )
            self.assertEqual(batch["review_strategy"], template["review_strategy"])
            self.assertEqual(batch["chunk_ids"], template["chunk_ids"])
        self.assertEqual(0, exit_code)
        self.assertTrue(out_json_exists)
        self.assertTrue(out_csv_exists)
        self.assertTrue(out_md_exists)
        self.assertIn("approval_review_batch_manifest", stdout_value)

    def test_blocks_when_worklist_no_longer_matches_runtime_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data")
            _seed_document(
                settings,
                [
                    Chunk(
                        chunk_id="clean-1",
                        document_id="doc-a",
                        chunk_type="article",
                        text="clean one",
                        metadata={"article_no": "1", "article_title": "Purpose"},
                    )
                ],
            )
            repository = JsonRepository(settings)
            worklist_path = root / "approval_worklist.json"
            worklist = build_approval_worklist(data_dir=settings.data_dir, source_system="PUBLIC_PORTAL")
            worklist_path.write_text(json.dumps(worklist, ensure_ascii=False, indent=2), encoding="utf-8")
            approved_chunk = repository.get_chunks("doc-a")[0].model_copy(update={"approval_status": "approved"})
            repository.save_chunks("doc-a", [approved_chunk])

            report = build_approval_review_batches(
                data_dir=settings.data_dir,
                worklist_report=worklist_path,
                worklist_report_artifact_path="reports/approval_worklist.json",
            )
            stdout = io.StringIO()
            exit_code = main(
                [
                    "--data-dir",
                    str(settings.data_dir),
                    "--worklist-report",
                    str(worklist_path),
                    "--worklist-report-artifact-path",
                    "reports/approval_worklist.json",
                    "--fail-on-issue",
                ],
                stdout=stdout,
            )

        self.assertFalse(report["passed"])
        self.assertEqual(2, report["blocker_count"])
        self.assertIn("worklist-runtime-count-mismatch", {item["code"] for item in report["findings"]})
        self.assertIn("worklist-document-fingerprint-mismatch", {item["code"] for item in report["findings"]})
        self.assertEqual(2, exit_code)

    def test_blocks_same_count_chunk_identity_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data")
            _seed_document(
                settings,
                [
                    Chunk(
                        chunk_id="clean-old",
                        document_id="doc-a",
                        chunk_type="article",
                        text="clean one",
                        metadata={"article_no": "1", "article_title": "Purpose"},
                    )
                ],
            )
            repository = JsonRepository(settings)
            worklist_path = root / "approval_worklist.json"
            worklist = build_approval_worklist(data_dir=settings.data_dir, source_system="PUBLIC_PORTAL")
            worklist_path.write_text(json.dumps(worklist, ensure_ascii=False, indent=2), encoding="utf-8")
            repository.save_chunks(
                "doc-a",
                [
                    Chunk(
                        chunk_id="clean-new",
                        document_id="doc-a",
                        chunk_type="article",
                        text="clean replacement",
                        metadata={"article_no": "1", "article_title": "Purpose"},
                    )
                ],
            )

            report = build_approval_review_batches(
                data_dir=settings.data_dir,
                worklist_report=worklist_path,
                worklist_report_artifact_path="reports/approval_worklist.json",
            )

        self.assertFalse(report["passed"])
        self.assertIn("worklist-document-fingerprint-mismatch", {item["code"] for item in report["findings"]})

    def test_blocks_same_count_same_chunk_id_content_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data")
            _seed_document(
                settings,
                [
                    Chunk(
                        chunk_id="clean-1",
                        document_id="doc-a",
                        chunk_type="article",
                        text="clean one",
                        metadata={"article_no": "1", "article_title": "Purpose"},
                    )
                ],
            )
            repository = JsonRepository(settings)
            worklist_path = root / "approval_worklist.json"
            worklist = build_approval_worklist(data_dir=settings.data_dir, source_system="PUBLIC_PORTAL")
            worklist_path.write_text(json.dumps(worklist, ensure_ascii=False, indent=2), encoding="utf-8")
            repository.save_chunks(
                "doc-a",
                [
                    Chunk(
                        chunk_id="clean-1",
                        document_id="doc-a",
                        chunk_type="article",
                        text="changed clean one",
                        metadata={"article_no": "1", "article_title": "Purpose"},
                    )
                ],
            )

            report = build_approval_review_batches(
                data_dir=settings.data_dir,
                worklist_report=worklist_path,
                worklist_report_artifact_path="reports/approval_worklist.json",
            )

        self.assertFalse(report["passed"])
        self.assertIn("worklist-document-fingerprint-mismatch", {item["code"] for item in report["findings"]})

    def test_blocks_cross_runtime_worklist_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_settings = Settings(data_dir=root / "source-data")
            target_settings = Settings(data_dir=root / "target-data")
            chunks = [
                Chunk(
                    chunk_id="clean-1",
                    document_id="doc-a",
                    chunk_type="article",
                    text="clean one",
                    metadata={"article_no": "1", "article_title": "Purpose"},
                )
            ]
            _seed_document(source_settings, chunks)
            _seed_document(target_settings, chunks)
            worklist_path = root / "source_worklist.json"
            worklist = build_approval_worklist(data_dir=source_settings.data_dir, source_system="PUBLIC_PORTAL")
            worklist_path.write_text(json.dumps(worklist, ensure_ascii=False, indent=2), encoding="utf-8")

            report = build_approval_review_batches(
                data_dir=target_settings.data_dir,
                worklist_report=worklist_path,
                worklist_report_artifact_path="reports/approval_worklist.json",
            )

        self.assertFalse(report["passed"])
        self.assertIn("worklist-effective-data-dir-mismatch", {item["code"] for item in report["findings"]})

    def test_include_review_type_validates_only_selected_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data")
            _seed_document(
                settings,
                [
                    Chunk(
                        chunk_id="clean-1",
                        document_id="doc-a",
                        chunk_type="article",
                        text="clean one",
                        metadata={"article_no": "1", "article_title": "Purpose"},
                    ),
                    Chunk(
                        chunk_id="attention-1",
                        document_id="doc-a",
                        chunk_type="table",
                        text="table",
                        metadata={"table_review_required": True, "table_review_flags": ["row_review_required"]},
                    ),
                ],
            )
            worklist_path = root / "approval_worklist.json"
            worklist = build_approval_worklist(data_dir=settings.data_dir, source_system="PUBLIC_PORTAL")
            worklist_path.write_text(json.dumps(worklist, ensure_ascii=False, indent=2), encoding="utf-8")

            report = build_approval_review_batches(
                data_dir=settings.data_dir,
                worklist_report=worklist_path,
                worklist_report_artifact_path="reports/approval_worklist.json",
                include_review_types=["low_risk_batch"],
            )

        self.assertTrue(report["passed"])
        self.assertEqual(1, report["batch_count"])
        self.assertEqual(0, report["manual_attention_chunks"])
        self.assertEqual(1, report["low_risk_batch_review_candidate_chunks"])
        self.assertEqual(["low_risk_batch"], report["include_review_types"])


def _seed_document(settings: Settings, chunks: list[Chunk]) -> None:
    repository = JsonRepository(settings)
    repository.upsert_document(
        Document(
            document_id="doc-a",
            filename="a.pdf",
            document_name="A",
            file_type="pdf",
            file_hash="hash-a",
            institution_name="Institution A",
            apba_id="C0001",
            source_system="PUBLIC_PORTAL",
            source_record_id="record-a",
            profile_id="public_portal-c0001",
        )
    )
    repository.save_processing_result("doc-a", [], chunks, [])


if __name__ == "__main__":
    unittest.main()
