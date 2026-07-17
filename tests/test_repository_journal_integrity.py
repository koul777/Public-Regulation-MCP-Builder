from __future__ import annotations

import json
import multiprocessing
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api import routes_rag
from app.core.config import Settings
from app.core.security import AuthContext
from app.schemas.document import Document
from app.storage.repository import (
    JournalIntegrityError,
    JsonRepository,
    _REPOSITORY_LOCK,
)


def _approval_record(*, approved_by: str = "reviewer") -> dict:
    return {
        "approval_record_id": "approval-record-1",
        "approval_id": "approval-1",
        "document_id": "document-1",
        "chunk_ids": ["chunk-1"],
        "approved_content_hashes": {"chunk-1": "hash-1"},
        "approved_by": approved_by,
        "approved_at": "2026-07-13T00:00:00+00:00",
        "tenant_id": "tenant-a",
    }


def _write_partial_approval_journal(
    data_dir: str,
    partial_written,
    finish_write,
    result_queue,
) -> None:
    try:
        repository = JsonRepository(Settings(data_dir=Path(data_dir)))
        journal_path = repository.root / "journals" / "approvals.jsonl"
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(_approval_record(), separators=(",", ":")) + "\n"
        split_at = len(serialized) // 2
        with repository._repository_write_lock():
            with journal_path.open("a", encoding="utf-8") as handle:
                handle.write(serialized[:split_at])
                handle.flush()
                partial_written.set()
                if not finish_write.wait(timeout=5):
                    raise TimeoutError("test writer was not released")
                handle.write(serialized[split_at:])
                handle.flush()
    except Exception as exc:  # pragma: no cover - exercised in child process
        result_queue.put(f"writer:{type(exc).__name__}:{exc}")
    else:
        result_queue.put("writer:ok")


def _read_approval_journal(data_dir: str, reader_started, reader_done, result_queue) -> None:
    try:
        repository = JsonRepository(Settings(data_dir=Path(data_dir)))
        reader_started.set()
        records = repository.list_approval_journal_records()
    except Exception as exc:  # pragma: no cover - exercised in child process
        result_queue.put(f"reader:{type(exc).__name__}:{exc}")
    else:
        result_queue.put(f"reader:ok:{len(records)}")
    finally:
        reader_done.set()


class JsonRepositoryJournalIntegrityTests(unittest.TestCase):
    def test_malformed_line_invalidates_whole_approval_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = JsonRepository(Settings(data_dir=Path(tmp)))
            repository.upsert_document(
                Document(
                    document_id="document-1",
                    filename="regulation.pdf",
                    file_type="pdf",
                    file_hash="hash-1",
                    tenant_id="tenant-a",
                )
            )
            repository.append_approval_record(_approval_record())
            journal_path = repository.root / "journals" / "approvals.jsonl"
            with journal_path.open("a", encoding="utf-8") as handle:
                handle.write('{"approval_record_id":"truncated"\n')

            with self.assertRaisesRegex(JournalIntegrityError, "malformed JSON at line 2"):
                repository.list_approval_journal_records()
            with self.assertRaises(JournalIntegrityError):
                repository.list_approval_records()
            with self.assertRaises(JournalIntegrityError):
                routes_rag._build_approval_snapshot(
                    repository,
                    ["document-1"],
                    AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin"),
                )

    def test_non_object_line_invalidates_audit_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = JsonRepository(Settings(data_dir=Path(tmp)))
            journal_path = repository.root / "journals" / "maintenance_events.jsonl"
            journal_path.parent.mkdir(parents=True, exist_ok=True)
            journal_path.write_text(
                json.dumps(
                    {
                        "event_id": "event-1",
                        "event_type": "approval_vector_sync_outcome",
                        "created_at": "2026-07-13T00:00:00+00:00",
                    }
                )
                + "\n[]\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(JournalIntegrityError, "non-object record at line 2"):
                repository.list_maintenance_events()

    def test_duplicate_json_object_key_invalidates_audit_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = JsonRepository(Settings(data_dir=Path(tmp)))
            journal_path = repository.root / "journals" / "maintenance_events.jsonl"
            journal_path.parent.mkdir(parents=True, exist_ok=True)
            journal_path.write_text(
                '{"event_id":"event-1","event_id":"event-2","event_type":"sync"}\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(JournalIntegrityError, "duplicate JSON key at line 1"):
                repository.list_maintenance_events()

    def test_conflicting_duplicate_approval_id_is_rejected_before_manifest_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = JsonRepository(Settings(data_dir=Path(tmp)))
            original = _approval_record(approved_by="reviewer-a")
            conflicting = _approval_record(approved_by="reviewer-b")
            repository.append_approval_record(original)

            with self.assertRaisesRegex(JournalIntegrityError, "conflicting record"):
                repository.append_approval_record(conflicting)

            recovered = JsonRepository(Settings(data_dir=Path(tmp))).list_approval_records()
            journal = repository.list_approval_journal_records()
            self.assertEqual("reviewer-a", recovered[0]["approved_by"])
            self.assertEqual([original], journal)

    def test_exact_duplicate_approval_append_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = JsonRepository(Settings(data_dir=Path(tmp)))
            record = _approval_record()

            repository.append_approval_record(record)
            repository.append_approval_record(dict(record))

            self.assertEqual([record], repository.list_approval_journal_records())
            self.assertEqual([record], repository.list_approval_records())

    def test_manifest_backed_security_journals_reject_conflicting_id_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = JsonRepository(Settings(data_dir=Path(tmp)))
            cases = [
                (
                    "review",
                    repository.append_review_record,
                    repository.list_review_records,
                    {"review_id": "review-1", "document_id": "document-1"},
                ),
                (
                    "indexing",
                    repository.append_indexing_job,
                    repository.list_indexing_jobs,
                    {"indexing_job_id": "index-1", "document_id": "document-1"},
                ),
                (
                    "feedback",
                    repository.append_rag_feedback,
                    repository.list_rag_feedback,
                    {"feedback_id": "feedback-1", "trace_id": "trace-1"},
                ),
                (
                    "security scan",
                    repository.append_security_scan_record,
                    repository.list_security_scan_records,
                    {"scan_id": "scan-1", "document_id": "document-1"},
                ),
                (
                    "maintenance",
                    repository.append_maintenance_event,
                    repository.list_maintenance_events,
                    {"event_id": "event-1", "event_type": "sync"},
                ),
            ]

            for label, append, list_records, record in cases:
                with self.subTest(journal=label):
                    append(record)
                    append(dict(record))
                    with self.assertRaises(JournalIntegrityError):
                        append(dict(record, conflicting_value=True))
                    self.assertEqual([record], list_records())

    def test_conflicting_duplicate_already_on_disk_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = JsonRepository(Settings(data_dir=Path(tmp)))
            original = _approval_record(approved_by="reviewer-a")
            repository.append_approval_record(original)
            journal_path = repository.root / "journals" / "approvals.jsonl"
            with journal_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(_approval_record(approved_by="reviewer-b")) + "\n")

            with self.assertRaisesRegex(JournalIntegrityError, "conflicting records"):
                repository.list_approval_journal_records()

    def test_reader_waits_for_repository_writer_instead_of_skipping_partial_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = JsonRepository(Settings(data_dir=Path(tmp)))
            journal_path = repository.root / "journals" / "approvals.jsonl"
            journal_path.parent.mkdir(parents=True, exist_ok=True)
            serialized = json.dumps(_approval_record(), separators=(",", ":")) + "\n"
            split_at = len(serialized) // 2
            partial_written = threading.Event()
            finish_write = threading.Event()
            reader_done = threading.Event()
            reader_result: list[dict] = []

            def write_in_two_steps() -> None:
                with _REPOSITORY_LOCK, repository._repository_write_lock():
                    with journal_path.open("a", encoding="utf-8") as handle:
                        handle.write(serialized[:split_at])
                        handle.flush()
                        partial_written.set()
                        if not finish_write.wait(timeout=2):
                            raise TimeoutError("test writer was not released")
                        handle.write(serialized[split_at:])
                        handle.flush()

            def read_records() -> None:
                reader_result.extend(repository.list_approval_journal_records())
                reader_done.set()

            writer = threading.Thread(target=write_in_two_steps)
            writer.start()
            self.assertTrue(partial_written.wait(timeout=2))
            reader = threading.Thread(target=read_records)
            reader.start()
            time.sleep(0.05)
            self.assertFalse(reader_done.is_set())
            finish_write.set()
            writer.join(timeout=2)
            reader.join(timeout=2)

            self.assertFalse(writer.is_alive())
            self.assertFalse(reader.is_alive())
            self.assertEqual([_approval_record()], reader_result)

    def test_cross_process_reader_waits_for_complete_journal_append(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            JsonRepository(Settings(data_dir=Path(tmp)))
            context = multiprocessing.get_context("spawn")
            partial_written = context.Event()
            finish_write = context.Event()
            reader_started = context.Event()
            reader_done = context.Event()
            result_queue = context.Queue()
            writer = context.Process(
                target=_write_partial_approval_journal,
                args=(tmp, partial_written, finish_write, result_queue),
            )
            writer.start()
            self.assertTrue(partial_written.wait(timeout=5))
            reader = context.Process(
                target=_read_approval_journal,
                args=(tmp, reader_started, reader_done, result_queue),
            )
            reader.start()
            self.assertTrue(reader_started.wait(timeout=5))
            time.sleep(0.1)
            self.assertFalse(reader_done.is_set())
            finish_write.set()
            writer.join(timeout=10)
            reader.join(timeout=10)

            self.assertEqual(0, writer.exitcode)
            self.assertEqual(0, reader.exitcode)
            self.assertEqual(
                {"writer:ok", "reader:ok:1"},
                {result_queue.get(timeout=2), result_queue.get(timeout=2)},
            )

    def test_read_only_repository_without_lock_file_can_still_be_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = JsonRepository(Settings(data_dir=Path(tmp)))
            repository.append_approval_record(_approval_record())
            lock_path = repository.root / ".write.lock"
            lock_path.unlink()
            original_open = Path.open

            def reject_lock_creation(path: Path, mode: str = "r", *args, **kwargs):
                if path == lock_path and mode == "a+b":
                    raise PermissionError("simulated read-only repository")
                return original_open(path, mode, *args, **kwargs)

            with patch.object(Path, "open", reject_lock_creation):
                self.assertEqual([_approval_record()], repository.list_approval_journal_records())

    def test_manifest_only_approval_after_append_failure_has_no_journal_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repository = JsonRepository(settings)
            record = _approval_record()
            repository.upsert_document(
                Document(
                    document_id="document-1",
                    filename="regulation.pdf",
                    file_type="pdf",
                    file_hash="hash-1",
                    tenant_id="tenant-a",
                )
            )

            with patch.object(repository, "_append_journal_record", side_effect=OSError("simulated crash")):
                with self.assertRaises(OSError):
                    repository.append_approval_record(record)

            fresh = JsonRepository(settings)
            self.assertEqual([record], fresh.list_approval_records())
            self.assertEqual([], fresh.list_approval_journal_records())
            self.assertEqual(
                {},
                routes_rag._build_approval_snapshot(
                    fresh,
                    ["document-1"],
                    AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin"),
                ),
            )


if __name__ == "__main__":
    unittest.main()
