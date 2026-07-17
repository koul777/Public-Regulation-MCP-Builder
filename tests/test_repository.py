from __future__ import annotations

import multiprocessing
import json
import os
import tempfile
import time
import traceback
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.core.pipeline import processing_options_payload
from app.schemas.chunk import ChunkOptions
from app.schemas.chunk import Chunk
from app.schemas.document import Document, ProcessingJob
from app.schemas.quality import QualityReport
from app.schemas.run import ProcessingRun
from app.schemas.structure import StructureNode
from app.schemas.validation import ValidationIssue
from app.storage.repository import JsonRepository


def _write_repository_records(data_dir: str, prefix: str, count: int, queue) -> None:
    try:
        repo = JsonRepository(Settings(data_dir=Path(data_dir)))
        for index in range(count):
            document = Document(
                document_id=f"{prefix}_doc_{index}",
                filename=f"{prefix}_{index}.pdf",
                file_type="pdf",
                file_hash=f"{prefix}_hash_{index}",
            )
            repo.upsert_document(document)
            repo.upsert_run(
                ProcessingRun(
                    run_id=f"{prefix}_run_{index}",
                    document_id=document.document_id,
                    job_id=f"{prefix}_job_{index}",
                    status="completed",
                    started_at=datetime.now(timezone.utc),
                    elapsed_seconds=0.01,
                )
            )
        queue.put(None)
    except Exception:  # pragma: no cover - surfaced in parent process
        queue.put(traceback.format_exc())


def _save_reusable_outputs(settings: Settings, repo: JsonRepository, document_id: str) -> dict[str, str]:
    node = StructureNode(
        node_id=f"{document_id}_node_1",
        document_id=document_id,
        node_type="article",
        number="1",
        title="Purpose",
        text="Article 1 Purpose",
        order_index=0,
    )
    chunk = Chunk(
        chunk_id=f"{document_id}_chunk_1",
        document_id=document_id,
        source_node_ids=[node.node_id],
        chunk_type="article",
        text="Article 1 Purpose",
    )
    repo.save_processing_result(document_id, [node], [chunk], [])
    repo.save_quality_report(
        document_id,
        QualityReport(
            document_id=document_id,
            passed=True,
            score=100.0,
            node_count=1,
            chunk_count=1,
            issue_count=0,
            error_count=0,
            warning_count=0,
            duplicate_chunk_id_count=0,
            empty_chunk_count=0,
            missing_page_count=0,
            missing_required_metadata_count=0,
        ),
    )
    settings.exports_dir.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, str] = {}
    for artifact_name in (
        "jsonl",
        "csv",
        "md",
        "tables.jsonl",
        "tables.csv",
        "manifest.json",
        "quality.json",
        "quality.md",
        "agent_review_plan.json",
        "ai_review_draft.json",
    ):
        path = settings.exports_dir / f"{document_id}.{artifact_name}"
        path.write_text("{}\n", encoding="utf-8")
        artifacts[artifact_name] = str(path)
    return artifacts


class JsonRepositoryTests(unittest.TestCase):
    def test_large_processing_result_streams_json_without_full_document_dumps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            chunks = [
                Chunk(
                    chunk_id=f"chunk-{index}",
                    document_id="doc-large",
                    chunk_type="article",
                    text=(f"제{index}조 대용량 규정 본문 " + "가나다라마바사" * 2000),
                )
                for index in range(120)
            ]
            progress: list[tuple[str, int, int]] = []

            with patch("app.storage.repository.json.dumps", side_effect=MemoryError("full JSON copy forbidden")):
                repo.save_processing_result(
                    "doc-large",
                    [],
                    chunks,
                    [],
                    progress_callback=lambda phase, current, total: progress.append((phase, current, total)),
                )

            loaded = repo.get_chunks("doc-large")

        self.assertEqual(120, len(loaded))
        self.assertEqual(chunks[-1].text, loaded[-1].text)
        self.assertIn(("chunks", 120, 120), progress)
        self.assertIn(("nodes", 0, 0), progress)
        self.assertIn(("issues", 0, 0), progress)

    def test_append_records_are_recoverable_from_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            record = {
                "approval_record_id": "approval_record_journal",
                "approval_id": "approval-journal",
                "document_id": "doc_journal",
                "chunk_ids": ["chunk-1"],
                "approved_by": "tester",
                "approved_at": "2026-07-08T00:00:00+00:00",
                "tenant_id": "tenant-a",
            }

            repo.append_approval_record(record)
            journal_path = settings.data_dir / "repository" / "journals" / "approvals.jsonl"
            manifest_path = settings.data_dir / "repository" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["approvals"] = {}
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            recovered = JsonRepository(settings).list_approval_records("doc_journal")
            journal_only = JsonRepository(settings).list_approval_journal_records("doc_journal")
            self.assertTrue(journal_path.is_file())
            self.assertEqual(recovered[0]["approval_record_id"], "approval_record_journal")
            self.assertEqual(journal_only[0]["approval_record_id"], "approval_record_journal")

    def test_rag_trace_append_is_recoverable_from_journal_without_manifest_growth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            record = {
                "trace_id": "rag_journal_only",
                "created_at": "2026-07-08T00:00:00+00:00",
                "action": "search",
                "actor": "tester",
                "tenant_id": "tenant-a",
                "auth_mode": "api_token",
                "api_role": "admin",
                "query_hash": "hash",
                "top_k": 1,
                "security_levels": ["internal"],
                "department_ids": [],
                "result_count": 0,
                "result_refs": [],
            }

            repo.append_rag_trace(record)
            manifest_path = settings.data_dir / "repository" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            recovered = JsonRepository(settings).list_rag_traces()

        self.assertEqual({}, manifest.get("rag_traces"))
        self.assertEqual("rag_journal_only", recovered[0]["trace_id"])

    def test_stores_manifest_and_document_results_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            document = Document(
                document_id="doc_test",
                filename="sample.pdf",
                file_type="pdf",
                file_hash="abc",
            )
            job = ProcessingJob(job_id="job_test", document_id=document.document_id)
            node = StructureNode(
                node_id="node_1",
                document_id=document.document_id,
                node_type="article",
                number="제1조",
                title="목적",
                text="제1조(목적) 본문",
                order_index=0,
            )
            chunk = Chunk(
                chunk_id="chunk_1",
                document_id=document.document_id,
                source_node_ids=[node.node_id],
                chunk_type="article",
                text="본문",
                metadata={"source_file": "sample.pdf", "hierarchy_path": "sample > 제1조"},
            )
            issue = ValidationIssue(
                issue_id="issue_1",
                document_id=document.document_id,
                severity="warning",
                issue_type="sample",
                message="sample",
            )

            repo.upsert_document(document)
            repo.upsert_job(job)
            repo.save_processing_result(document.document_id, [node], [chunk], [issue])
            repo.save_quality_report(
                document.document_id,
                QualityReport(
                    document_id=document.document_id,
                    passed=True,
                    score=100.0,
                    node_count=1,
                    chunk_count=1,
                    issue_count=1,
                    error_count=0,
                    warning_count=1,
                    duplicate_chunk_id_count=0,
                    empty_chunk_count=0,
                    missing_page_count=0,
                    missing_required_metadata_count=0,
                ),
            )
            repo.upsert_run(
                ProcessingRun(
                    run_id="run_test",
                    document_id=document.document_id,
                    job_id=job.job_id,
                    status="completed",
                    started_at=datetime.now(timezone.utc),
                    elapsed_seconds=1.25,
                    stats={"chunk_count": 1},
                    artifacts={"jsonl": "data/exports/doc_test.jsonl"},
                )
            )

            self.assertTrue((Path(tmp) / "repository" / "manifest.json").exists())
            self.assertTrue((Path(tmp) / "repository" / "doc_test_nodes.json").exists())
            self.assertEqual(repo.get_document("doc_test").filename, "sample.pdf")
            self.assertEqual(repo.get_job("job_test").document_id, "doc_test")
            self.assertEqual(repo.get_nodes("doc_test")[0].node_id, "node_1")
            self.assertEqual(repo.get_chunks("doc_test")[0].chunk_id, "chunk_1")
            self.assertEqual(repo.get_issues("doc_test")[0].issue_type, "sample")
            self.assertEqual(repo.get_quality_report("doc_test").score, 100.0)
            self.assertEqual(repo.get_run("run_test").stats["chunk_count"], 1)
            self.assertEqual(repo.list_runs("doc_test")[0].run_id, "run_test")

    def test_finds_reusable_completed_run_by_source_hash_and_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            document = Document(
                document_id="doc_reusable",
                filename="sample.hwp",
                file_type="hwp",
                file_hash="same-hash",
                source_system="PUBLIC_PORTAL",
                source_record_id="board-1",
                source_file_id="file-1",
                profile_id="public_portal-etc-law",
            )
            options = {"chunk_mode": "article", "max_chunk_chars": 1800}
            repo.upsert_document(document)
            artifacts = _save_reusable_outputs(settings, repo, document.document_id)
            repo.upsert_run(
                ProcessingRun(
                    run_id="run_reusable",
                    document_id=document.document_id,
                    job_id="job_reusable",
                    status="completed",
                    started_at=datetime.now(timezone.utc),
                    elapsed_seconds=0.5,
                    options=options,
                    artifacts=artifacts,
                )
            )

            reusable = repo.find_reusable_run(
                file_hash="same-hash",
                options={"max_chunk_chars": 1800, "chunk_mode": "article"},
                source_system="PUBLIC_PORTAL",
                source_record_id="board-1",
                source_file_id="file-1",
                profile_id="public_portal-etc-law",
            )

            self.assertIsNotNone(reusable)
            self.assertEqual(reusable[0].document_id, "doc_reusable")
            self.assertEqual(reusable[1].run_id, "run_reusable")

    def test_reusable_run_rejects_completed_run_with_missing_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            options = {"chunk_mode": "article"}
            document = Document(
                document_id="doc_incomplete",
                filename="sample.hwp",
                file_type="hwp",
                file_hash="same-hash",
                profile_id="public_portal-etc-law",
            )
            repo.upsert_document(document)
            repo.upsert_run(
                ProcessingRun(
                    run_id="run_incomplete",
                    document_id=document.document_id,
                    job_id="job_incomplete",
                    status="completed",
                    started_at=datetime.now(timezone.utc),
                    elapsed_seconds=0.5,
                    options=options,
                )
            )

            reusable = repo.find_reusable_run(
                file_hash="same-hash",
                options=options,
                profile_id="public_portal-etc-law",
            )

            self.assertIsNone(reusable)

    def test_manifest_cache_reloads_when_another_repository_instance_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo1 = JsonRepository(settings)
            repo2 = JsonRepository(settings)
            doc1 = Document(document_id="doc_1", filename="one.pdf", file_type="pdf", file_hash="hash-1")
            doc2 = Document(document_id="doc_2", filename="two.pdf", file_type="pdf", file_hash="hash-2")

            repo1.upsert_document(doc1)
            self.assertEqual(repo2.get_document("doc_1").document_id, "doc_1")
            time.sleep(0.01)
            repo1.upsert_document(doc2)
            repo2.upsert_job(ProcessingJob(job_id="job_2", document_id="doc_2"))

            fresh = JsonRepository(settings)
            self.assertEqual(fresh.get_document("doc_1").document_id, "doc_1")
            self.assertEqual(fresh.get_document("doc_2").document_id, "doc_2")
            self.assertEqual(fresh.get_job("job_2").document_id, "doc_2")

    def test_manifest_writes_are_safe_across_processes(self) -> None:
        self.maxDiff = None
        with tempfile.TemporaryDirectory() as tmp:
            ctx = multiprocessing.get_context("spawn")
            queue = ctx.Queue()
            processes = [
                ctx.Process(target=_write_repository_records, args=(tmp, f"proc{index}", 3, queue))
                for index in range(3)
            ]

            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=20)

            errors = [queue.get(timeout=5) for _ in processes]
            for process in processes:
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(errors, [None, None, None])

            repo = JsonRepository(Settings(data_dir=Path(tmp)))
            document_ids = {document.document_id for document in repo.list_documents()}
            run_ids = {run.run_id for run in repo.list_runs()}
            self.assertEqual(len([document_id for document_id in document_ids if document_id.startswith("proc")]), 9)
            self.assertEqual(len([run_id for run_id in run_ids if run_id.startswith("proc")]), 9)

    def test_manifest_write_retries_transient_replace_permission_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            original_replace = Path.replace
            calls = 0

            def flaky_replace(source: Path, target: Path) -> Path:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise PermissionError("transient file lock")
                return original_replace(source, target)

            with patch.object(Path, "replace", flaky_replace):
                repo.upsert_document(
                    Document(
                        document_id="doc_retry",
                        filename="retry.pdf",
                        file_type="pdf",
                        file_hash="retry-hash",
                    )
                )

            self.assertGreaterEqual(calls, 2)
            self.assertEqual(JsonRepository(settings).get_document("doc_retry").filename, "retry.pdf")

    def test_failed_manifest_replace_does_not_leak_uncommitted_approval_from_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            record = {
                "approval_record_id": "approval_record_failed",
                "approval_id": "approval-failed",
                "document_id": "doc-failed",
                "approved_at": "2026-07-13T00:00:00+00:00",
            }

            with patch(
                "app.storage.repository._replace_with_retry",
                side_effect=PermissionError("persistent file lock"),
            ):
                with self.assertRaises(PermissionError):
                    repo.append_approval_record(record)

            self.assertEqual(repo.list_approval_records("doc-failed"), [])
            self.assertEqual(JsonRepository(settings).list_approval_records("doc-failed"), [])

    def test_manifest_update_reads_disk_even_when_cached_mtime_appears_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo1 = JsonRepository(settings)
            repo2 = JsonRepository(settings)
            repo1.upsert_document(
                Document(document_id="doc_1", filename="one.pdf", file_type="pdf", file_hash="hash-1")
            )
            self.assertIsNotNone(repo1.get_document("doc_1"))

            repo2.upsert_document(
                Document(document_id="doc_2", filename="two.pdf", file_type="pdf", file_hash="hash-2")
            )
            # Simulate a stale read cache whose identity already appears to
            # match the latest disk file. Writers must still read from disk.
            repo1._manifest_identity = repo1._file_identity(repo1.manifest_path)
            repo1.upsert_job(ProcessingJob(job_id="job_2", document_id="doc_2"))

            fresh = JsonRepository(settings)
            self.assertIsNotNone(fresh.get_document("doc_1"))
            self.assertIsNotNone(fresh.get_document("doc_2"))
            self.assertEqual(fresh.get_job("job_2").document_id, "doc_2")

    def test_manifest_read_cache_detects_same_size_same_mtime_atomic_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            repo.upsert_document(
                Document(document_id="doc_1", filename="one.pdf", file_type="pdf", file_hash="hash-1")
            )
            self.assertEqual("one.pdf", repo.get_document("doc_1").filename)

            original_stat = repo.manifest_path.stat()
            payload = json.loads(repo.manifest_path.read_text(encoding="utf-8"))
            payload["documents"]["doc_1"]["filename"] = "two.pdf"
            replacement = repo.manifest_path.with_suffix(".replacement")
            replacement.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            self.assertEqual(original_stat.st_size, replacement.stat().st_size)
            replacement.replace(repo.manifest_path)
            os.utime(
                repo.manifest_path,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )

            self.assertEqual(original_stat.st_mtime_ns, repo.manifest_path.stat().st_mtime_ns)
            self.assertEqual(original_stat.st_size, repo.manifest_path.stat().st_size)
            self.assertEqual("two.pdf", repo.get_document("doc_1").filename)

    def test_reusable_run_falls_back_to_hash_when_source_identity_is_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            options = {"chunk_mode": "article"}
            document = Document(
                document_id="doc_existing",
                filename="sample.hwp",
                file_type="hwp",
                file_hash="same-hash",
                source_system="PUBLIC_PORTAL",
                source_record_id="board-1",
                source_file_id="file-1",
                profile_id="public_portal-etc-law",
            )
            repo.upsert_document(document)
            artifacts = _save_reusable_outputs(settings, repo, document.document_id)
            repo.upsert_run(
                ProcessingRun(
                    run_id="run_existing",
                    document_id=document.document_id,
                    job_id="job_existing",
                    status="completed",
                    started_at=datetime.now(timezone.utc),
                    elapsed_seconds=0.5,
                    options=options,
                    artifacts=artifacts,
                )
            )

            reusable = repo.find_reusable_run(
                file_hash="same-hash",
                options=options,
                source_system="PUBLIC_PORTAL",
                profile_id="public_portal-etc-law",
            )

            self.assertIsNotNone(reusable)
            self.assertEqual(reusable[0].document_id, "doc_existing")
    def test_reusable_run_allows_source_file_identity_with_matching_hash_and_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            options = {"chunk_mode": "article"}
            document = Document(
                document_id="doc_source_file",
                filename="sample.hwp",
                file_type="hwp",
                file_hash="same-hash",
                source_system="PUBLIC_PORTAL",
                source_file_id="file-1",
                profile_id="public_portal-etc-law",
            )
            repo.upsert_document(document)
            artifacts = _save_reusable_outputs(settings, repo, document.document_id)
            repo.upsert_run(
                ProcessingRun(
                    run_id="run_source_file",
                    document_id=document.document_id,
                    job_id="job_source_file",
                    status="completed",
                    started_at=datetime.now(timezone.utc),
                    elapsed_seconds=0.5,
                    options=options,
                    artifacts=artifacts,
                )
            )

            reusable = repo.find_reusable_run(
                file_hash="same-hash",
                options=options,
                source_system="PUBLIC_PORTAL",
                source_file_id="file-1",
                profile_id="public_portal-etc-law",
            )

            self.assertIsNotNone(reusable)
            self.assertEqual(reusable[0].document_id, "doc_source_file")

    def test_reusable_run_allows_hash_match_when_only_profile_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            options = {"chunk_mode": "article"}
            document = Document(
                document_id="doc_profile",
                filename="sample.pdf",
                file_type="pdf",
                file_hash="same-hash",
                profile_id="default-public-institution",
            )
            repo.upsert_document(document)
            artifacts = _save_reusable_outputs(settings, repo, document.document_id)
            repo.upsert_run(
                ProcessingRun(
                    run_id="run_profile",
                    document_id=document.document_id,
                    job_id="job_profile",
                    status="completed",
                    started_at=datetime.now(timezone.utc),
                    elapsed_seconds=0.5,
                    options=options,
                    artifacts=artifacts,
                )
            )

            reusable = repo.find_reusable_run(
                file_hash="same-hash",
                options=options,
                profile_id="default-public-institution",
            )

            self.assertIsNotNone(reusable)
            self.assertEqual(reusable[0].document_id, "doc_profile")

    def test_reusable_run_rejects_old_options_without_pipeline_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            options = ChunkOptions()
            document = Document(
                document_id="doc_old_options",
                filename="sample.pdf",
                file_type="pdf",
                file_hash="same-hash",
                profile_id="default-public-institution",
            )
            repo.upsert_document(document)
            repo.upsert_run(
                ProcessingRun(
                    run_id="run_old_options",
                    document_id=document.document_id,
                    job_id="job_old_options",
                    status="completed",
                    started_at=datetime.now(timezone.utc),
                    elapsed_seconds=0.5,
                    options=options.model_dump(mode="json"),
                )
            )

            self.assertIsNone(
                repo.find_reusable_run(
                    file_hash="same-hash",
                    options=processing_options_payload(options),
                    profile_id="default-public-institution",
                )
            )

    def test_reusable_run_rejects_different_pipeline_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            options = ChunkOptions()
            document = Document(
                document_id="doc_old_pipeline",
                filename="sample.pdf",
                file_type="pdf",
                file_hash="same-hash",
                profile_id="default-public-institution",
            )
            old_options = processing_options_payload(options)
            old_options["pipeline_version"] = "older"
            repo.upsert_document(document)
            repo.upsert_run(
                ProcessingRun(
                    run_id="run_old_pipeline",
                    document_id=document.document_id,
                    job_id="job_old_pipeline",
                    status="completed",
                    started_at=datetime.now(timezone.utc),
                    elapsed_seconds=0.5,
                    options=old_options,
                )
            )

            self.assertIsNone(
                repo.find_reusable_run(
                    file_hash="same-hash",
                    options=processing_options_payload(options),
                    profile_id="default-public-institution",
                )
            )

    def test_reusable_run_rejects_old_run_after_later_different_options_overwrite_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            document = Document(
                document_id="doc_multi_run",
                filename="sample.pdf",
                file_type="pdf",
                file_hash="same-hash",
                profile_id="default-public-institution",
            )
            repo.upsert_document(document)
            old_options = {"chunk_mode": "article"}
            new_options = {"chunk_mode": "paragraph"}
            old_started = datetime(2026, 1, 1, tzinfo=timezone.utc)
            new_started = datetime(2026, 1, 2, tzinfo=timezone.utc)
            old_artifacts = _save_reusable_outputs(settings, repo, document.document_id)
            repo.upsert_run(
                ProcessingRun(
                    run_id="run_old",
                    document_id=document.document_id,
                    job_id="job_old",
                    status="completed",
                    started_at=old_started,
                    elapsed_seconds=0.5,
                    options=old_options,
                    artifacts=old_artifacts,
                )
            )
            new_artifacts = _save_reusable_outputs(settings, repo, document.document_id)
            repo.upsert_run(
                ProcessingRun(
                    run_id="run_new",
                    document_id=document.document_id,
                    job_id="job_new",
                    status="completed",
                    started_at=new_started,
                    elapsed_seconds=0.5,
                    options=new_options,
                    artifacts=new_artifacts,
                )
            )

            reusable_old = repo.find_reusable_run(
                file_hash="same-hash",
                options=old_options,
                profile_id="default-public-institution",
            )
            reusable_new = repo.find_reusable_run(
                file_hash="same-hash",
                options=new_options,
                profile_id="default-public-institution",
            )

            self.assertIsNone(reusable_old)
            self.assertIsNotNone(reusable_new)
            self.assertEqual(reusable_new[1].run_id, "run_new")

    def test_reusable_run_requires_matching_provenance_when_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            options = {"chunk_mode": "article"}
            document = Document(
                document_id="doc_provenance",
                filename="sample.hwp",
                file_type="hwp",
                file_hash="same-hash",
                source_system="PUBLIC_PORTAL",
                source_record_id="board-1",
                source_file_id="file-1",
                institution_name="Old Institution",
                source_url="https://example.test/old",
                source_disclosure_date="2026.01.01",
                source_posted_date="2026.01.02",
                profile_id="public_portal-etc-law",
            )
            repo.upsert_document(document)
            artifacts = _save_reusable_outputs(settings, repo, document.document_id)
            repo.upsert_run(
                ProcessingRun(
                    run_id="run_provenance",
                    document_id=document.document_id,
                    job_id="job_provenance",
                    status="completed",
                    started_at=datetime.now(timezone.utc),
                    elapsed_seconds=0.5,
                    options=options,
                    artifacts=artifacts,
                )
            )

            reusable = repo.find_reusable_run(
                file_hash="same-hash",
                options=options,
                source_system="PUBLIC_PORTAL",
                source_record_id="board-1",
                source_file_id="file-1",
                institution_name="New Institution",
                source_url="https://example.test/new",
                source_disclosure_date="2026.05.01",
                source_posted_date="2026.05.02",
                profile_id="public_portal-etc-law",
            )

            self.assertIsNone(reusable)


if __name__ == "__main__":
    unittest.main()
