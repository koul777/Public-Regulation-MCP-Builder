from __future__ import annotations

import multiprocessing
import errno
import gzip
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
from app.storage import repository as repository_module
from app.storage.repository import JournalIntegrityError, JsonRepository


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


def _leave_processing_sidecars(
    data_dir: str,
    ready,
    release=None,
) -> None:
    try:
        repo = JsonRepository(Settings(data_dir=Path(data_dir)))
        document = Document(
            document_id="doc_crashed_processing",
            filename="crashed.hwp",
            file_type="hwp",
            file_hash="crashed-hash",
            status="uploaded",
        )
        repo.upsert_document(document)
        repo.upsert_document_progress(
            document.model_copy(update={"status": "processing"})
        )
        repo.upsert_job(
            ProcessingJob(
                job_id="job_crashed_processing",
                document_id=document.document_id,
                status="processing",
                progress=55,
                message="Parsing",
            )
        )
        ready.set()
        if release is not None:
            release.wait(timeout=10)
    except Exception:  # pragma: no cover - surfaced by process exit code
        traceback.print_exc()
        raise


def _admit_same_regulation_version(
    data_dir: str,
    document_id: str,
    ready,
    start,
    queue,
) -> None:
    try:
        repo = JsonRepository(
            Settings(data_dir=Path(data_dir))
        ).enforce_unique_regulation_version_admission()
        ready.put(document_id)
        if not start.wait(timeout=10):
            raise TimeoutError("admission race was not released")
        repo.upsert_document(
            Document(
                document_id=document_id,
                filename=f"{document_id}.pdf",
                file_type="pdf",
                file_hash=f"hash-{document_id}",
                profile_id="profile-a",
                regulation_id="reg-personnel",
                regulation_version="rev-20260729",
                tenant_id="tenant-a",
            )
        )
        queue.put(("admitted", document_id))
    except ValueError as exc:
        queue.put(("duplicate", str(exc)))
    except Exception:  # pragma: no cover - surfaced in parent process
        queue.put(("error", traceback.format_exc()))


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
    def test_processing_document_metadata_uses_sidecar_until_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            uploaded = Document(
                document_id="doc_metadata_progress",
                filename="metadata-progress.hwp",
                document_name="업로드 이름",
                file_type="hwp",
                file_hash="hash-metadata-progress",
                status="uploaded",
            )
            repo.upsert_document(uploaded)
            processing = uploaded.model_copy(
                update={
                    "document_name": "본문에서 확인한 규정명",
                    "regulation_id": "reg-confirmed",
                    "status": "processing",
                }
            )

            repo.upsert_document_progress(processing)

            manifest = json.loads(repo.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual("업로드 이름", manifest["documents"][uploaded.document_id]["document_name"])
            self.assertEqual("본문에서 확인한 규정명", repo.get_document(uploaded.document_id).document_name)
            self.assertEqual(
                "본문에서 확인한 규정명",
                next(
                    document.document_name
                    for document in repo.list_documents()
                    if document.document_id == uploaded.document_id
                ),
            )
            self.assertEqual(1, len(list(repo.document_progress_root.glob("*.json"))))

            completed = processing.model_copy(update={"status": "completed"})
            repo.upsert_document(completed)

            self.assertEqual("completed", repo.get_document(uploaded.document_id).status)
            self.assertEqual([], list(repo.document_progress_root.glob("*.json")))

    def test_processing_job_progress_uses_sidecar_until_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            document = Document(
                document_id="doc_progress",
                filename="progress.hwp",
                file_type="hwp",
                file_hash="hash-progress",
            )
            repo.upsert_document(document)
            processing = ProcessingJob(
                job_id="job_progress",
                document_id=document.document_id,
                status="processing",
                progress=45,
                message="청크 생성 중",
            )

            repo.upsert_job(processing)

            manifest = json.loads(repo.manifest_path.read_text(encoding="utf-8"))
            self.assertNotIn(processing.job_id, manifest["jobs"])
            self.assertEqual(1, len(list(repo.job_progress_root.glob("*.json"))))
            reloaded = JsonRepository(settings).get_job(processing.job_id)
            self.assertIsNotNone(reloaded)
            self.assertEqual(45, reloaded.progress)

            completed = processing.model_copy(
                update={
                    "status": "completed",
                    "progress": 100,
                    "message": "전처리 완료",
                    "completed_at": datetime.now(timezone.utc),
                }
            )
            repo.upsert_job(completed)

            manifest = json.loads(repo.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual("completed", manifest["jobs"][processing.job_id]["status"])
            self.assertEqual([], list(repo.job_progress_root.glob("*.json")))
            self.assertEqual("completed", JsonRepository(settings).get_job(processing.job_id).status)

    def test_crashed_processing_worker_is_recovered_as_atomic_failed_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            worker = context.Process(
                target=_leave_processing_sidecars,
                args=(tmp, ready),
            )
            worker.start()
            self.assertTrue(ready.wait(timeout=10))
            worker.join(timeout=10)
            self.assertEqual(0, worker.exitcode)

            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            recovered_job = repo.get_job("job_crashed_processing")
            recovered_document = repo.get_document("doc_crashed_processing")
            manifest = json.loads(repo.manifest_path.read_text(encoding="utf-8"))

            self.assertEqual("failed", recovered_job.status)
            self.assertIsNotNone(recovered_job.completed_at)
            self.assertIn("worker process is no longer active", recovered_job.error)
            self.assertEqual("failed", recovered_document.status)
            self.assertEqual(
                "failed",
                manifest["jobs"]["job_crashed_processing"]["status"],
            )
            self.assertEqual(
                "failed",
                manifest["documents"]["doc_crashed_processing"]["status"],
            )
            self.assertEqual([], list(repo.job_progress_root.glob("*.json")))
            self.assertEqual([], list(repo.document_progress_root.glob("*.json")))

            fresh = JsonRepository(settings)
            self.assertEqual("failed", fresh.get_job("job_crashed_processing").status)
            self.assertEqual(
                "failed",
                fresh.get_document("doc_crashed_processing").status,
            )

    def test_legacy_progress_sidecar_recovers_after_conservative_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            document = Document(
                document_id="doc-legacy-stale",
                filename="legacy.hwp",
                file_type="hwp",
                file_hash="hash-legacy-stale",
            )
            job = ProcessingJob(
                job_id="job-legacy-stale",
                document_id=document.document_id,
                status="processing",
                progress=40,
            )
            repo.upsert_document(document)
            repo.document_progress_root.mkdir(parents=True, exist_ok=True)
            repo.job_progress_root.mkdir(parents=True, exist_ok=True)
            repo._write_json(
                repo._document_progress_path(document.document_id),
                document.model_copy(update={"status": "processing"}).model_dump(
                    mode="json"
                ),
            )
            repo._write_json(
                repo._job_progress_path(job.job_id),
                job.model_dump(mode="json"),
            )
            stale_timestamp = time.time() - (25 * 60 * 60)
            os.utime(
                repo._document_progress_path(document.document_id),
                (stale_timestamp, stale_timestamp),
            )
            os.utime(
                repo._job_progress_path(job.job_id),
                (stale_timestamp, stale_timestamp),
            )

            recovered = JsonRepository(settings).get_job(job.job_id)

            self.assertEqual("failed", recovered.status)
            self.assertEqual(
                "failed",
                JsonRepository(settings).get_document(document.document_id).status,
            )

    def test_progress_owner_identity_distinguishes_pid_reuse_from_live_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            document = Document(
                document_id="doc-owner-identity",
                filename="owner.hwp",
                file_type="hwp",
                file_hash="hash-owner-identity",
            )
            job = ProcessingJob(
                job_id="job-owner-identity",
                document_id=document.document_id,
                status="processing",
                progress=30,
            )
            repo.upsert_document(document)
            repo.upsert_document_progress(
                document.model_copy(update={"status": "processing"})
            )
            repo.upsert_job(job)

            with patch.object(
                repository_module,
                "_own_process_identity",
                return_value=f"live:{os.getpid()}",
            ):
                self.assertEqual("processing", repo.get_job(job.job_id).status)

            with patch.object(
                repository_module,
                "_own_process_identity",
                return_value="windows:reused-pid-start",
            ):
                self.assertEqual("failed", repo.get_job(job.job_id).status)

    def test_stale_older_job_does_not_fail_document_with_live_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            document = Document(
                document_id="doc-live-retry",
                filename="retry.hwp",
                file_type="hwp",
                file_hash="hash-live-retry",
            )
            stale_job = ProcessingJob(
                job_id="job-stale-attempt",
                document_id=document.document_id,
                status="processing",
                progress=60,
            )
            live_job = ProcessingJob(
                job_id="job-live-retry",
                document_id=document.document_id,
                status="processing",
                progress=20,
            )
            repo.upsert_document(document)
            repo.upsert_document_progress(
                document.model_copy(update={"status": "processing"})
            )
            repo.upsert_job(stale_job)
            repo.upsert_job(live_job)
            stale_path = repo._job_progress_path(stale_job.job_id)

            with patch.object(
                repo,
                "_progress_sidecar_is_stale",
                side_effect=lambda path, _raw: path == stale_path,
            ):
                recovered_count = repo.recover_stale_processing_progress(
                    job_id=stale_job.job_id
                )

            self.assertEqual(1, recovered_count)
            self.assertEqual("failed", repo.get_job(stale_job.job_id).status)
            self.assertEqual("processing", repo.get_job(live_job.job_id).status)
            self.assertEqual("processing", repo.get_document(document.document_id).status)

    def test_live_processing_worker_is_not_expired_by_elapsed_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            release = context.Event()
            worker = context.Process(
                target=_leave_processing_sidecars,
                args=(tmp, ready, release),
            )
            worker.start()
            self.assertTrue(ready.wait(timeout=10))

            repo = JsonRepository(Settings(data_dir=Path(tmp)))
            self.assertEqual(
                "processing",
                repo.get_job("job_crashed_processing").status,
            )
            self.assertEqual(
                "processing",
                repo.get_document("doc_crashed_processing").status,
            )

            release.set()
            worker.join(timeout=10)
            self.assertEqual(0, worker.exitcode)
            self.assertEqual(
                "failed",
                repo.get_job("job_crashed_processing").status,
            )

    def test_stale_progress_recovery_keeps_sidecars_when_terminal_commit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            document = Document(
                document_id="doc-stale-rollback",
                filename="stale.hwp",
                file_type="hwp",
                file_hash="hash-stale-rollback",
            )
            job = ProcessingJob(
                job_id="job-stale-rollback",
                document_id=document.document_id,
                status="processing",
                progress=70,
            )
            repo.upsert_document(document)
            repo.upsert_document_progress(
                document.model_copy(update={"status": "processing"})
            )
            repo.upsert_job(job)
            manifest_before = repo.manifest_path.read_bytes()

            with patch.object(
                repo,
                "_progress_sidecar_is_stale",
                return_value=True,
            ), patch.object(
                repo,
                "_write_json",
                side_effect=OSError("forced stale recovery commit failure"),
            ), self.assertRaisesRegex(
                OSError,
                "forced stale recovery commit failure",
            ):
                repo.recover_stale_processing_progress(job_id=job.job_id)

            self.assertEqual(manifest_before, repo.manifest_path.read_bytes())
            self.assertTrue(repo._job_progress_path(job.job_id).is_file())
            self.assertTrue(
                repo._document_progress_path(document.document_id).is_file()
            )

    def test_regulation_version_admission_is_atomic_across_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = multiprocessing.get_context("spawn")
            ready = context.Queue()
            start = context.Event()
            results = context.Queue()
            workers = [
                context.Process(
                    target=_admit_same_regulation_version,
                    args=(tmp, f"doc-race-{index}", ready, start, results),
                )
                for index in range(2)
            ]
            for worker in workers:
                worker.start()
            self.assertEqual(
                {"doc-race-0", "doc-race-1"},
                {ready.get(timeout=10), ready.get(timeout=10)},
            )
            start.set()
            for worker in workers:
                worker.join(timeout=15)
                self.assertEqual(0, worker.exitcode)

            outcomes = [results.get(timeout=5), results.get(timeout=5)]
            self.assertEqual(
                ["admitted", "duplicate"],
                sorted(outcome for outcome, _detail in outcomes),
            )
            duplicate_detail = next(
                detail
                for outcome, detail in outcomes
                if outcome == "duplicate"
            )
            self.assertIn("same regulation version already exists", duplicate_detail)
            stored = JsonRepository(Settings(data_dir=Path(tmp))).find_documents_by_regulation(
                "reg-personnel",
                profile_id="profile-a",
                tenant_id="tenant-a",
            )
            self.assertEqual(1, len(stored))

    def test_regulation_version_admission_is_scoped_by_tenant_and_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = JsonRepository(
                Settings(data_dir=Path(tmp))
            ).enforce_unique_regulation_version_admission()

            def regulation_document(
                document_id: str,
                *,
                tenant_id: str,
                profile_id: str,
            ) -> Document:
                return Document(
                    document_id=document_id,
                    filename=f"{document_id}.pdf",
                    file_type="pdf",
                    file_hash=f"hash-{document_id}",
                    tenant_id=tenant_id,
                    profile_id=profile_id,
                    regulation_id="reg-shared",
                    regulation_version="v1",
                )

            first = regulation_document(
                "doc-scope-a",
                tenant_id="tenant-a",
                profile_id="profile-a",
            )
            repo.upsert_document(first)
            repo.upsert_document(
                regulation_document(
                    "doc-scope-b",
                    tenant_id="tenant-b",
                    profile_id="profile-a",
                )
            )
            repo.upsert_document(
                regulation_document(
                    "doc-scope-c",
                    tenant_id="tenant-a",
                    profile_id="profile-b",
                )
            )
            repo.upsert_document(first.model_copy(update={"status": "completed"}))

            with self.assertRaisesRegex(
                ValueError,
                "same regulation version already exists",
            ):
                repo.upsert_document(
                    regulation_document(
                        "doc-scope-duplicate",
                        tenant_id="tenant-a",
                        profile_id="profile-a",
                    )
                )

            self.assertEqual(3, len(repo.list_documents()))

    def test_regulation_version_conflict_does_not_rewrite_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = JsonRepository(
                Settings(data_dir=Path(tmp))
            ).enforce_unique_regulation_version_admission()
            common = {
                "file_type": "pdf",
                "profile_id": "profile-a",
                "regulation_id": "reg-immutable-admission",
                "regulation_version": "Rev-1",
                "tenant_id": "tenant-a",
            }
            repo.upsert_document(
                Document(
                    document_id="doc-admission-first",
                    filename="first.pdf",
                    file_hash="hash-first",
                    **common,
                )
            )
            manifest_before = repo.manifest_path.read_bytes()

            with self.assertRaisesRegex(
                ValueError,
                "same regulation version already exists",
            ):
                repo.upsert_document(
                    Document(
                        document_id="doc-admission-second",
                        filename="second.pdf",
                        file_hash="hash-second",
                        **{
                            **common,
                            "regulation_version": " rev-1 ",
                        },
                    )
                )

            self.assertEqual(manifest_before, repo.manifest_path.read_bytes())

    def test_delete_document_removes_transient_processing_job_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            document = Document(
                document_id="doc_delete_progress",
                filename="delete-progress.hwp",
                file_type="hwp",
                file_hash="hash-delete-progress",
            )
            repo.upsert_document(document)
            repo.upsert_job(
                ProcessingJob(
                    job_id="job_delete_progress",
                    document_id=document.document_id,
                    status="processing",
                    progress=20,
                )
            )
            repo.upsert_document_progress(
                document.model_copy(update={"status": "processing"})
            )

            self.assertTrue(repo.delete_document(document.document_id))

            self.assertEqual([], list(repo.job_progress_root.glob("*.json")))
            self.assertEqual([], list(repo.document_progress_root.glob("*.json")))
            self.assertIsNone(repo.get_job("job_delete_progress"))

    def test_commit_processing_outcome_updates_document_job_and_run_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            document = Document(
                document_id="doc_outcome",
                filename="outcome.hwp",
                file_type="hwp",
                file_hash="hash-outcome",
                status="completed",
                processed_at=datetime.now(timezone.utc),
            )
            job = ProcessingJob(
                job_id="job_outcome",
                document_id=document.document_id,
                status="completed",
                progress=100,
                message="전처리 완료",
                completed_at=datetime.now(timezone.utc),
            )
            run = ProcessingRun(
                run_id="run_outcome",
                document_id=document.document_id,
                job_id=job.job_id,
                status="completed",
                started_at=datetime.now(timezone.utc),
                completed_at=job.completed_at,
                elapsed_seconds=1.0,
            )
            repo.upsert_job(
                job.model_copy(
                    update={
                        "status": "processing",
                        "progress": 95,
                        "completed_at": None,
                    }
                )
            )

            with patch.object(repo, "_write_json", wraps=repo._write_json) as write_json:
                repo.commit_processing_outcome(document=document, job=job, run=run)

            self.assertEqual(1, write_json.call_count)
            self.assertEqual("completed", repo.get_document(document.document_id).status)
            self.assertEqual("completed", repo.get_job(job.job_id).status)
            self.assertEqual(run.run_id, repo.get_run(run.run_id).run_id)
            self.assertEqual([], list(repo.job_progress_root.glob("*.json")))

    def test_commit_processing_outcome_rolls_back_manifest_and_run_on_failure(self) -> None:
        for failure_point in ("manifest", "run_journal"):
            with self.subTest(failure_point=failure_point), tempfile.TemporaryDirectory() as tmp:
                settings = Settings(data_dir=Path(tmp))
                repo = JsonRepository(settings)
                uploaded = Document(
                    document_id="doc_outcome_rollback",
                    filename="rollback.hwp",
                    file_type="hwp",
                    file_hash="hash-rollback",
                    status="uploaded",
                )
                processing_document = uploaded.model_copy(update={"status": "processing"})
                processing_job = ProcessingJob(
                    job_id="job_outcome_rollback",
                    document_id=uploaded.document_id,
                    status="processing",
                    progress=90,
                )
                repo.upsert_document(uploaded)
                repo.upsert_document_progress(processing_document)
                repo.upsert_job(processing_job)
                completed_at = datetime.now(timezone.utc)
                completed_document = processing_document.model_copy(
                    update={"status": "completed", "processed_at": completed_at}
                )
                completed_job = processing_job.model_copy(
                    update={
                        "status": "completed",
                        "progress": 100,
                        "completed_at": completed_at,
                    }
                )
                completed_run = ProcessingRun(
                    run_id="run_outcome_rollback",
                    document_id=uploaded.document_id,
                    job_id=processing_job.job_id,
                    status="completed",
                    started_at=completed_at,
                    completed_at=completed_at,
                    elapsed_seconds=1.0,
                )
                manifest_before = repo.manifest_path.read_bytes()
                run_journal_path = (
                    settings.data_dir
                    / "repository"
                    / "journals"
                    / "runs.jsonl"
                )
                run_journal_before = (
                    run_journal_path.read_bytes()
                    if run_journal_path.is_file()
                    else None
                )

                if failure_point == "manifest":
                    failure_patch = patch.object(
                        repo,
                        "_write_json",
                        side_effect=RuntimeError("forced terminal manifest failure"),
                    )
                    expected_message = "forced terminal manifest failure"
                else:
                    failure_patch = patch.object(
                        repo,
                        "_append_journal_record",
                        side_effect=RuntimeError("forced terminal run failure"),
                    )
                    expected_message = "forced terminal run failure"

                with failure_patch, self.assertRaisesRegex(
                    RuntimeError,
                    expected_message,
                ):
                    repo.commit_processing_outcome(
                        document=completed_document,
                        job=completed_job,
                        run=completed_run,
                    )

                self.assertEqual(manifest_before, repo.manifest_path.read_bytes())
                self.assertEqual(
                    run_journal_before,
                    run_journal_path.read_bytes()
                    if run_journal_path.is_file()
                    else None,
                )
                self.assertIsNone(repo.get_run(completed_run.run_id))
                self.assertEqual("processing", repo.get_document(uploaded.document_id).status)
                self.assertEqual("processing", repo.get_job(processing_job.job_id).status)

    def test_terminal_manifest_wins_when_progress_sidecar_cleanup_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            uploaded = Document(
                document_id="doc_outcome_cleanup",
                filename="cleanup.hwp",
                file_type="hwp",
                file_hash="hash-cleanup",
                status="uploaded",
            )
            processing_document = uploaded.model_copy(update={"status": "processing"})
            processing_job = ProcessingJob(
                job_id="job_outcome_cleanup",
                document_id=uploaded.document_id,
                status="processing",
                progress=95,
            )
            repo.upsert_document(uploaded)
            repo.upsert_document_progress(processing_document)
            repo.upsert_job(processing_job)
            completed_at = datetime.now(timezone.utc)
            completed_document = processing_document.model_copy(
                update={"status": "completed", "processed_at": completed_at}
            )
            completed_job = processing_job.model_copy(
                update={
                    "status": "completed",
                    "progress": 100,
                    "completed_at": completed_at,
                }
            )
            completed_run = ProcessingRun(
                run_id="run_outcome_cleanup",
                document_id=uploaded.document_id,
                job_id=processing_job.job_id,
                status="completed",
                started_at=completed_at,
                completed_at=completed_at,
                elapsed_seconds=1.0,
            )
            progress_paths = {
                repo._job_progress_path(processing_job.job_id),
                repo._document_progress_path(uploaded.document_id),
            }
            original_unlink = Path.unlink

            def fail_progress_cleanup(path: Path, *args, **kwargs):
                if path in progress_paths:
                    raise OSError("forced progress cleanup failure")
                return original_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", new=fail_progress_cleanup):
                repo.commit_processing_outcome(
                    document=completed_document,
                    job=completed_job,
                    run=completed_run,
                )

            self.assertTrue(all(path.is_file() for path in progress_paths))
            self.assertEqual("completed", repo.get_document(uploaded.document_id).status)
            self.assertEqual("completed", repo.get_job(processing_job.job_id).status)
            self.assertEqual(
                "completed",
                next(
                    document.status
                    for document in repo.list_documents()
                    if document.document_id == uploaded.document_id
                ),
            )
            self.assertEqual("completed", repo.get_run(completed_run.run_id).status)

    def test_processing_runs_use_journal_and_migrate_legacy_manifest_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            legacy_run = ProcessingRun(
                run_id="run_legacy",
                document_id="doc_legacy",
                job_id="job_legacy",
                status="completed",
                started_at=datetime.now(timezone.utc),
                elapsed_seconds=1.0,
            )
            manifest = json.loads(repo.manifest_path.read_text(encoding="utf-8"))
            manifest["runs"][legacy_run.run_id] = legacy_run.model_dump(mode="json")
            repo.manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
            )

            repo.upsert_document(
                Document(
                    document_id="doc_legacy",
                    filename="legacy.hwp",
                    file_type="hwp",
                    file_hash="hash-legacy",
                )
            )
            current_run = ProcessingRun(
                run_id="run_current",
                document_id="doc_legacy",
                job_id="job_current",
                status="completed",
                started_at=datetime.now(timezone.utc),
                elapsed_seconds=2.0,
            )
            repo.upsert_run(current_run)

            compacted = json.loads(repo.manifest_path.read_text(encoding="utf-8"))
            journal_path = repo.root / "journals" / "runs.jsonl"
            journal_exists = journal_path.is_file()
            runs = repo.list_runs("doc_legacy")

        self.assertEqual({}, compacted["runs"])
        self.assertTrue(journal_exists)
        self.assertEqual({"run_legacy", "run_current"}, {run.run_id for run in runs})

    def test_processing_run_journal_is_idempotent_and_rejects_conflicting_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = JsonRepository(Settings(data_dir=Path(tmp)))
            run = ProcessingRun(
                run_id="run_immutable",
                document_id="doc_immutable",
                job_id="job_immutable",
                status="completed",
                started_at=datetime.now(timezone.utc),
                elapsed_seconds=1.0,
            )

            repo.upsert_run(run)
            repo.upsert_run(run.model_copy(deep=True))
            with self.assertRaises(JournalIntegrityError):
                repo.upsert_run(run.model_copy(update={"elapsed_seconds": 2.0}))

            stored = repo.list_runs("doc_immutable")

        self.assertEqual(1, len(stored))
        self.assertEqual(1.0, stored[0].elapsed_seconds)

    def test_reads_gzipped_processing_result_when_json_file_is_not_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            chunk = Chunk(
                chunk_id="doc-compressed_chunk_1",
                document_id="doc-compressed",
                chunk_type="article",
                text="Article 1 Purpose",
            )
            repo.save_chunks("doc-compressed", [chunk])
            result_path = settings.data_dir / "repository" / "doc-compressed_chunks.json"
            compressed_path = Path(f"{result_path}.gz")
            with result_path.open("rb") as source, gzip.open(compressed_path, "wb") as target:
                target.write(source.read())
            result_path.unlink()

            recovered = repo.get_chunks("doc-compressed")

            self.assertEqual([chunk.chunk_id], [item.chunk_id for item in recovered])

    def test_read_only_repository_can_read_without_creating_missing_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = JsonRepository(Settings(data_dir=Path(tmp)))
            lock_path = Path(tmp) / "repository" / ".write.lock"
            lock_path.unlink()
            original_open = Path.open

            def readonly_open(path: Path, mode: str = "r", *args, **kwargs):
                if path == lock_path and mode == "a+b":
                    raise OSError(errno.EROFS, "Read-only file system")
                return original_open(path, mode, *args, **kwargs)

            with patch.object(Path, "open", new=readonly_open):
                self.assertEqual(repo.list_approval_journal_records(), [])

    def test_document_listing_without_stale_sidecars_does_not_take_write_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = JsonRepository(Settings(data_dir=Path(tmp)))
            repo.upsert_document(
                Document(
                    document_id="doc-readonly-list",
                    filename="readonly.pdf",
                    file_type="pdf",
                    file_hash="hash-readonly-list",
                )
            )
            lock_path = repo.root / ".write.lock"
            lock_path.unlink()
            original_open = Path.open

            def readonly_open(path: Path, mode: str = "r", *args, **kwargs):
                if path == lock_path and mode == "a+b":
                    raise OSError(errno.EROFS, "Read-only file system")
                return original_open(path, mode, *args, **kwargs)

            with patch.object(Path, "open", new=readonly_open):
                documents = repo.list_documents()

            self.assertEqual(["doc-readonly-list"], [item.document_id for item in documents])

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

    def test_approval_journal_records_are_cached_and_append_updates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            first_record = {
                "approval_record_id": "approval_record_cached_1",
                "approval_id": "approval-cached-1",
                "document_id": "doc-cached",
                "chunk_ids": ["chunk-1"],
                "approved_at": "2026-07-08T00:00:00+00:00",
                "tenant_id": "tenant-a",
            }
            second_record = {
                "approval_record_id": "approval_record_cached_2",
                "approval_id": "approval-cached-2",
                "document_id": "doc-cached",
                "chunk_ids": ["chunk-2"],
                "approved_at": "2026-07-09T00:00:00+00:00",
                "tenant_id": "tenant-a",
            }
            repo.append_approval_record(first_record)
            journal_path = settings.data_dir / "repository" / "journals" / "approvals.jsonl"
            cache_key = str(journal_path.resolve())
            repository_module._JOURNAL_RECORD_CACHE.pop(cache_key, None)

            try:
                with patch.object(
                    repository_module,
                    "_journal_json_object",
                    wraps=repository_module._journal_json_object,
                ) as json_object:
                    first = repo.list_approval_journal_records()
                    parsed_call_count = json_object.call_count
                    second = repo.list_approval_journal_records()

                    self.assertGreater(parsed_call_count, 0)
                    self.assertEqual(parsed_call_count, json_object.call_count)
                    self.assertEqual(first, second)

                    repo.append_approval_record(second_record)
                    post_append_parse_count = json_object.call_count
                    after_append = repo.list_approval_journal_records()

                    self.assertEqual(post_append_parse_count, json_object.call_count)
                    self.assertEqual(
                        {"approval_record_cached_1", "approval_record_cached_2"},
                        {record["approval_record_id"] for record in after_append},
                    )
            finally:
                repository_module._JOURNAL_RECORD_CACHE.pop(cache_key, None)

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

    def test_failed_journal_append_does_not_expose_uncommitted_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            record = {
                "approval_record_id": "approval_record_failed",
                "approval_id": "approval-failed",
                "document_id": "doc-failed",
                "approved_at": "2026-07-13T00:00:00+00:00",
            }

            with patch.object(
                repo,
                "_append_journal_record",
                side_effect=PermissionError("persistent journal lock"),
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
