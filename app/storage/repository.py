from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import socket
from threading import Lock
import time
from typing import TextIO, TypeVar
from uuid import uuid4

from app.core.config import Settings
from app.schemas.chunk import Chunk
from app.schemas.document import Document, ProcessingJob
from app.schemas.quality import QualityReport
from app.schemas.run import ProcessingRun
from app.schemas.structure import StructureNode
from app.schemas.validation import ValidationIssue


_REPOSITORY_LOCK = Lock()
_LOCK_POLL_SECONDS = 0.05
_LOCK_TIMEOUT_SECONDS = 30.0
_REPLACE_RETRY_SECONDS = 2.0
_REPLACE_RETRY_INTERVAL_SECONDS = 0.05
_LEGACY_PROGRESS_STALE_SECONDS = 24 * 60 * 60
_INTERRUPTED_PROCESSING_ERROR = (
    "Processing was interrupted because its worker process is no longer active."
)
try:
    _PROGRESS_OWNER_HOST = socket.gethostname().strip().casefold()
except OSError:
    _PROGRESS_OWNER_HOST = str(
        os.environ.get("COMPUTERNAME")
        or os.environ.get("HOSTNAME")
        or ""
    ).strip().casefold()

_FileIdentity = tuple[int, int, int, int]
_JournalFileIdentity = tuple[_FileIdentity | None, _FileIdentity | None]
_CacheValue = TypeVar("_CacheValue")

_JOURNAL_ID_FIELDS: dict[str, tuple[str, ...]] = {
    "runs": ("run_id",),
    "approvals": ("approval_record_id", "approval_id"),
    "review_decisions": ("review_id",),
    "indexing_jobs": ("indexing_job_id",),
    "rag_traces": ("trace_id",),
    "rag_feedback": ("feedback_id",),
    "security_scans": ("scan_id",),
    "maintenance_events": ("event_id",),
}
_MANIFEST_JOURNAL_MIRRORS: dict[str, str] = {
    "runs": "runs",
    "approvals": "approvals",
    "review_decisions": "review_decisions",
    "indexing_jobs": "indexing_jobs",
    "rag_feedback": "rag_feedback",
    "security_scans": "security_scans",
}
_JOURNAL_IDENTITY_CACHE_MAX_ENTRIES = 64
_JOURNAL_RECORD_CACHE_MAX_ENTRIES = 16
_JOURNAL_IDENTITY_CACHE: OrderedDict[
    str, tuple[_JournalFileIdentity, dict[str, str]]
] = OrderedDict()
_JOURNAL_RECORD_CACHE: OrderedDict[
    str, tuple[_JournalFileIdentity, list[dict]]
] = OrderedDict()
_CURRENT_PROCESS_IDENTITY: tuple[int, str | None] | None = None
_TERMINAL_DOCUMENT_STATUSES = frozenset(
    {"completed", "failed", "approved", "rejected", "superseded"}
)
_JSON_BUFFERED_ENCODE_MAX_STRING_CHARS = 256 * 1024
_JSON_BUFFERED_ENCODE_MAX_TOTAL_STRING_CHARS = 1024 * 1024
_JSON_BUFFERED_ENCODE_MAX_CONTAINER_ITEMS = 4096
_JSON_BUFFERED_ENCODE_MAX_INSPECTED_VALUES = 4096
_JOURNAL_READ_CHUNK_CHARS = 1024 * 1024


def _journal_cache_get(
    cache: OrderedDict[str, _CacheValue],
    key: str,
) -> _CacheValue | None:
    value = cache.get(key)
    if value is not None:
        cache.move_to_end(key)
    return value


def _journal_cache_set(
    cache: OrderedDict[str, _CacheValue],
    key: str,
    value: _CacheValue,
    *,
    max_entries: int,
) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > max_entries:
        cache.popitem(last=False)


def _iter_journal_lines(handle: TextIO) -> Iterator[str]:
    """Yield JSONL records with bounded read buffers and C-level splitting."""

    pending = ""
    while True:
        block = handle.read(_JOURNAL_READ_CHUNK_CHARS)
        if not block:
            break
        lines = f"{pending}{block}".split("\n")
        pending = lines.pop()
        yield from lines
    if pending:
        yield pending


def _json_value_needs_buffered_encoding(value: object) -> bool:
    """Detect exceptional JSON shapes with a bounded iterative traversal."""

    pending: list[object] = [value]
    seen_containers: set[int] = set()
    inspected_values = 0
    total_string_chars = 0
    while pending:
        current = pending.pop()
        inspected_values += 1
        if inspected_values > _JSON_BUFFERED_ENCODE_MAX_INSPECTED_VALUES:
            return True
        if isinstance(current, str):
            string_chars = len(current)
            total_string_chars += string_chars
            if (
                string_chars > _JSON_BUFFERED_ENCODE_MAX_STRING_CHARS
                or total_string_chars
                > _JSON_BUFFERED_ENCODE_MAX_TOTAL_STRING_CHARS
            ):
                return True
        elif isinstance(current, dict):
            if len(current) > _JSON_BUFFERED_ENCODE_MAX_CONTAINER_ITEMS:
                return True
            container_id = id(current)
            if container_id in seen_containers:
                continue
            seen_containers.add(container_id)
            pending.extend(child for pair in current.items() for child in pair)
        elif isinstance(current, (list, tuple)):
            if len(current) > _JSON_BUFFERED_ENCODE_MAX_CONTAINER_ITEMS:
                return True
            container_id = id(current)
            if container_id in seen_containers:
                continue
            seen_containers.add(container_id)
            pending.extend(current)
    return False


class JournalIntegrityError(RuntimeError):
    """Raised when an append-only repository journal is structurally ambiguous."""


@dataclass(frozen=True)
class ProcessingClaim:
    """Result of atomically claiming one document's mutable output namespace."""

    acquired: bool
    run_id: str
    job_id: str
    previous_owner_run_id: str | None = None


class _DuplicateJournalJsonKey(ValueError):
    pass


def _journal_json_object(pairs: list[tuple[str, object]]) -> dict:
    item: dict = {}
    for key, value in pairs:
        if key in item:
            raise _DuplicateJournalJsonKey(key)
        item[key] = value
    return item


class JsonRepository:
    def __init__(self, settings: Settings):
        self.data_dir = settings.data_dir
        self.legacy_path = settings.data_dir / "repository.json"
        self.root = settings.data_dir / "repository"
        self.manifest_path = self.root / "manifest.json"
        self.job_progress_root = self.root / "job_progress"
        self.document_progress_root = self.root / "document_progress"
        self.processing_owner_root = self.root / "processing_owners"
        self._manifest_cache: dict | None = None
        self._manifest_identity: _FileIdentity | None = None
        self._legacy_cache: dict | None = None
        self._legacy_identity: _FileIdentity | None = None
        self._enforce_regulation_version_admission = False
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.manifest_path.exists():
            with _REPOSITORY_LOCK, self._repository_write_lock():
                if not self.manifest_path.exists():
                    self._write_json(self.manifest_path, self._empty_manifest())

    def enforce_unique_regulation_version_admission(self) -> JsonRepository:
        """Make new regulation-version inserts fail atomically on duplicates.

        The check runs inside the same cross-process repository write lock as
        the manifest update. Existing document IDs remain freely updatable.
        """

        self._enforce_regulation_version_admission = True
        return self

    def upsert_document(self, document: Document) -> None:
        with _REPOSITORY_LOCK, self._repository_write_lock():
            data = self._read_manifest_for_update()
            data.setdefault("documents", {})
            if (
                self._enforce_regulation_version_admission
                and document.document_id not in data["documents"]
            ):
                self._require_unique_regulation_version(data, document)
            data["documents"][document.document_id] = document.model_dump(mode="json")
            self._write_json(self.manifest_path, data)
            self._document_progress_path(document.document_id).unlink(missing_ok=True)

    def upsert_document_progress(self, document: Document) -> None:
        """Persist in-flight document metadata without rewriting the manifest."""

        with _REPOSITORY_LOCK, self._repository_write_lock():
            self.document_progress_root.mkdir(parents=True, exist_ok=True)
            payload = document.model_dump(mode="json")
            payload.update(self._progress_owner_metadata())
            self._write_json(
                self._document_progress_path(document.document_id),
                payload,
            )

    def get_document(self, document_id: str) -> Document | None:
        manifest_raw = self._read_manifest()["documents"].get(document_id)
        progress_raw = self._read_document_progress_path(
            self._document_progress_path(document_id)
        )
        if (
            progress_raw is not None
            and self._progress_sidecar_is_stale(
                self._document_progress_path(document_id),
                progress_raw,
            )
        ):
            self.recover_stale_processing_progress(document_id=document_id)
            manifest_raw = self._read_manifest()["documents"].get(document_id)
            progress_raw = self._read_document_progress_path(
                self._document_progress_path(document_id)
            )
        if (
            manifest_raw is not None
            and str(manifest_raw.get("status") or "").strip().casefold()
            in _TERMINAL_DOCUMENT_STATUSES
        ):
            raw = manifest_raw
        else:
            raw = progress_raw or manifest_raw
        if raw is None:
            raw = self._read_legacy().get("documents", {}).get(document_id)
        return Document.model_validate(raw) if raw else None

    def delete_document(self, document_id: str) -> bool:
        """Remove a document manifest entry, its processing jobs, and result artifacts."""
        document_id = str(document_id or "").strip()
        if not document_id:
            return False
        removed = False
        with _REPOSITORY_LOCK, self._repository_write_lock():
            data = self._read_manifest_for_update()
            documents = data.setdefault("documents", {})
            if document_id in documents:
                del documents[document_id]
                removed = True
            jobs = data.setdefault("jobs", {})
            for job_id, raw in list(jobs.items()):
                if str(raw.get("document_id") or "") == document_id:
                    del jobs[job_id]
            self._write_json(self.manifest_path, data)
            for path in self.job_progress_root.glob("*.json"):
                raw = self._read_job_progress_path(path)
                if raw is not None and str(raw.get("document_id") or "") == document_id:
                    path.unlink(missing_ok=True)
            self._document_progress_path(document_id).unlink(missing_ok=True)
            self._processing_output_state_path(document_id).unlink(missing_ok=True)
            for result_type in ("nodes", "chunks", "issues", "quality"):
                path = self._result_path(document_id, result_type)
                if path.exists():
                    path.unlink()
                    removed = True
        return removed

    def purge_document_records(self, document_ids: Iterable[str]) -> dict[str, int]:
        """Erase every journal and manifest row belonging to the given documents.

        ``delete_document`` only removes the document row, its jobs, and its result
        files. 실행 기록·승인 기록·색인 작업 기록은 저널에 그대로 남는다. 기관을
        통째로 지울 때 그것들을 남겨 두면, 지운 규정이 감사 기록과 재처리 캐시에서
        되살아난다. 되돌릴 수 없는 삭제이므로 호출하는 쪽에서 확인을 받는다.
        """

        targets = {str(document_id or "").strip() for document_id in document_ids}
        targets.discard("")
        removed: dict[str, int] = {}
        if not targets:
            return removed
        with _REPOSITORY_LOCK, self._repository_write_lock():
            for journal_name in _JOURNAL_ID_FIELDS:
                path = self._journal_path(journal_name)
                if not path.is_file() and not Path(f"{path}.gz").is_file():
                    continue
                records = self._read_journal_records(journal_name)
                kept = [
                    record
                    for record in records
                    if str(record.get("document_id") or "").strip() not in targets
                ]
                if len(kept) == len(records):
                    continue
                removed[journal_name] = len(records) - len(kept)
                self._rewrite_journal_unlocked(journal_name, kept)
            data = self._read_manifest_for_update()
            for manifest_key in _MANIFEST_JOURNAL_MIRRORS:
                mirrored = data.get(manifest_key)
                if not isinstance(mirrored, dict):
                    continue
                for record_key, record in list(mirrored.items()):
                    if (
                        isinstance(record, dict)
                        and str(record.get("document_id") or "").strip() in targets
                    ):
                        del mirrored[record_key]
                        removed[manifest_key] = removed.get(manifest_key, 0) + 1
            self._write_json(self.manifest_path, data)
        return removed

    def _rewrite_journal_unlocked(self, journal_name: str, records: list[dict]) -> None:
        """Replace a journal with the kept rows and drop its cached views.

        저널은 평소 덧붙이기만 한다. 여기서만 다시 쓰므로, 압축된 과거 구간까지
        합쳐 한 파일로 만들고 캐시를 비워 다음 읽기가 새 내용을 보게 한다.
        """

        path = self._journal_path(journal_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                    handle.write("\n")
            _replace_with_retry(tmp_path, path)
        finally:
            tmp_path.unlink(missing_ok=True)
        # 압축 구간을 남겨 두면 방금 지운 기록이 다음 읽기에서 되살아난다.
        Path(f"{path}.gz").unlink(missing_ok=True)
        cache_key = str(path.resolve())
        _JOURNAL_RECORD_CACHE.pop(cache_key, None)
        _JOURNAL_IDENTITY_CACHE.pop(cache_key, None)

    def list_documents(self) -> list[Document]:
        self.recover_stale_processing_progress()
        docs = dict(self._read_legacy().get("documents", {}))
        docs.update(self._read_manifest()["documents"])
        if self.document_progress_root.is_dir():
            for path in self.document_progress_root.glob("*.json"):
                raw = self._read_document_progress_path(path)
                if raw is not None and str(raw.get("document_id") or "").strip():
                    document_id = str(raw["document_id"])
                    committed = docs.get(document_id)
                    if (
                        isinstance(committed, dict)
                        and str(committed.get("status") or "").strip().casefold()
                        in _TERMINAL_DOCUMENT_STATUSES
                    ):
                        continue
                    docs[document_id] = raw
        return [Document.model_validate(raw) for raw in docs.values()]

    def find_documents_by_source(
        self,
        *,
        source_system: str | None = None,
        source_record_id: str | None = None,
        source_file_id: str | None = None,
        profile_id: str | None = None,
    ) -> list[Document]:
        documents = self.list_documents()
        for field_name, expected in {
            "source_system": source_system,
            "source_record_id": source_record_id,
            "source_file_id": source_file_id,
            "profile_id": profile_id,
        }.items():
            if expected:
                documents = [
                    document
                    for document in documents
                    if self._normalize_key(getattr(document, field_name)) == self._normalize_key(expected)
                ]
        return sorted(documents, key=lambda document: document.created_at)

    def find_documents_by_hash(self, file_hash: str) -> list[Document]:
        return sorted(
            [document for document in self.list_documents() if document.file_hash == file_hash],
            key=lambda document: document.created_at,
        )

    def find_documents_by_regulation(
        self,
        regulation_id: str,
        *,
        profile_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list[Document]:
        """Return versions explicitly assigned to one institution's regulation."""
        normalized_regulation_id = self._normalize_key(regulation_id)
        if not normalized_regulation_id:
            return []
        documents = [
            document
            for document in self.list_documents()
            if self._normalize_key(getattr(document, "regulation_id", None)) == normalized_regulation_id
        ]
        if profile_id:
            normalized_profile_id = self._normalize_key(profile_id)
            documents = [
                document
                for document in documents
                if self._normalize_key(document.profile_id) == normalized_profile_id
            ]
        if tenant_id:
            normalized_tenant_id = self._normalize_key(tenant_id)
            documents = [
                document
                for document in documents
                if self._normalize_key(document.tenant_id) == normalized_tenant_id
            ]
        return sorted(
            documents,
            key=lambda document: (
                str(getattr(document, "effective_from", "") or ""),
                _regulation_version_sort_key(getattr(document, "regulation_version", None)),
                document.created_at,
            ),
        )

    def _require_unique_regulation_version(
        self,
        data: dict,
        document: Document,
    ) -> None:
        regulation_id = self._normalize_key(document.regulation_id)
        regulation_version = self._normalize_key(document.regulation_version)
        profile_id = self._normalize_key(document.profile_id)
        tenant_id = self._normalize_key(document.tenant_id)
        if not regulation_id or not regulation_version:
            return
        candidates: dict[str, dict] = {}
        legacy_documents = self._read_legacy().get("documents", {})
        if isinstance(legacy_documents, dict):
            candidates.update(
                {
                    str(document_id): raw
                    for document_id, raw in legacy_documents.items()
                    if isinstance(raw, dict)
                }
            )
        candidates.update(
            {
                str(document_id): raw
                for document_id, raw in data.get("documents", {}).items()
                if isinstance(raw, dict)
            }
        )
        for existing_document_id, raw in candidates.items():
            if existing_document_id == document.document_id:
                continue
            if (
                self._normalize_key(raw.get("tenant_id")) == tenant_id
                and self._normalize_key(raw.get("profile_id")) == profile_id
                and self._normalize_key(raw.get("regulation_id")) == regulation_id
                and self._normalize_key(raw.get("regulation_version"))
                == regulation_version
            ):
                raise ValueError(
                    "The same regulation version already exists for the selected institution. "
                    "Register a new version instead of overwriting the existing document."
                )

    def upsert_job(self, job: ProcessingJob) -> None:
        with _REPOSITORY_LOCK, self._repository_write_lock():
            if job.status == "processing":
                self.job_progress_root.mkdir(parents=True, exist_ok=True)
                payload = job.model_dump(mode="json")
                payload.update(self._progress_owner_metadata())
                self._write_json(
                    self._job_progress_path(job.job_id),
                    payload,
                )
                return
            data = self._read_manifest_for_update()
            data.setdefault("jobs", {})
            data["jobs"][job.job_id] = job.model_dump(mode="json")
            self._write_json(self.manifest_path, data)
            self._job_progress_path(job.job_id).unlink(missing_ok=True)

    def get_job(self, job_id: str) -> ProcessingJob | None:
        manifest_raw = self._read_manifest()["jobs"].get(job_id)
        progress_raw = self._read_job_progress_path(self._job_progress_path(job_id))
        if (
            progress_raw is not None
            and self._progress_sidecar_is_stale(
                self._job_progress_path(job_id),
                progress_raw,
            )
        ):
            self.recover_stale_processing_progress(job_id=job_id)
            manifest_raw = self._read_manifest()["jobs"].get(job_id)
            progress_raw = self._read_job_progress_path(self._job_progress_path(job_id))
        if (
            manifest_raw is not None
            and str(manifest_raw.get("status") or "").strip().casefold()
            in {"completed", "failed"}
        ):
            return ProcessingJob.model_validate(manifest_raw)
        if progress_raw is not None:
            return ProcessingJob.model_validate(progress_raw)
        raw = manifest_raw
        if raw is None:
            raw = self._read_legacy().get("jobs", {}).get(job_id)
        return ProcessingJob.model_validate(raw) if raw else None

    def recover_stale_processing_progress(
        self,
        *,
        job_id: str | None = None,
        document_id: str | None = None,
    ) -> int:
        """Commit orphaned processing sidecars as failed terminal state.

        New sidecars carry the writer process identity, so a crashed local
        worker is detected immediately while a live long-running parser is
        never expired by elapsed time. Legacy or remote-host sidecars use a
        conservative 24-hour heartbeat timeout.
        """

        normalized_job_id = str(job_id or "").strip()
        normalized_document_id = str(document_id or "").strip()
        preflight_job_paths = (
            [self._job_progress_path(normalized_job_id)]
            if normalized_job_id
            else list(self.job_progress_root.glob("*.json"))
        )
        stale_candidate = False
        for path in preflight_job_paths:
            raw = self._read_job_progress_path(path)
            if raw is None:
                continue
            if (
                normalized_document_id
                and str(raw.get("document_id") or "").strip()
                != normalized_document_id
            ):
                continue
            if self._progress_sidecar_is_stale(path, raw):
                stale_candidate = True
                break
        if not stale_candidate:
            preflight_document_paths = (
                [self._document_progress_path(normalized_document_id)]
                if normalized_document_id
                else list(self.document_progress_root.glob("*.json"))
            )
            for path in preflight_document_paths:
                raw = self._read_document_progress_path(path)
                if raw is not None and self._progress_sidecar_is_stale(path, raw):
                    stale_candidate = True
                    break
        if not stale_candidate:
            return 0

        recovered_jobs = 0
        cleanup_paths: set[Path] = set()
        now = datetime.now(timezone.utc)
        failure_message = _INTERRUPTED_PROCESSING_ERROR
        with _REPOSITORY_LOCK, self._repository_write_lock():
            data = self._read_manifest_for_update()
            job_paths = (
                [self._job_progress_path(normalized_job_id)]
                if normalized_job_id
                else list(self.job_progress_root.glob("*.json"))
            )
            sidecars: list[tuple[Path, dict]] = []
            live_document_ids: set[str] = set()
            for path in job_paths:
                raw = self._read_job_progress_path(path)
                if raw is None:
                    continue
                raw_job_id = str(raw.get("job_id") or "").strip()
                raw_document_id = str(raw.get("document_id") or "").strip()
                if normalized_job_id and raw_job_id != normalized_job_id:
                    continue
                if normalized_document_id and raw_document_id != normalized_document_id:
                    continue
                sidecars.append((path, raw))
                if not self._progress_sidecar_is_stale(path, raw):
                    live_document_ids.add(raw_document_id)

            # A stale older attempt must not fail the document while another
            # process is actively retrying the same document.
            target_document_ids = {
                str(raw.get("document_id") or "").strip()
                for _path, raw in sidecars
                if str(raw.get("document_id") or "").strip()
            }
            if normalized_document_id:
                target_document_ids.add(normalized_document_id)
            if target_document_ids:
                for path in self.job_progress_root.glob("*.json"):
                    raw = self._read_job_progress_path(path)
                    if (
                        raw is not None
                        and str(raw.get("document_id") or "").strip()
                        in target_document_ids
                        and not self._progress_sidecar_is_stale(path, raw)
                    ):
                        live_document_ids.add(
                            str(raw.get("document_id") or "").strip()
                        )

            failed_document_ids: set[str] = set()
            for path, raw in sidecars:
                raw_job_id = str(raw.get("job_id") or "").strip()
                raw_document_id = str(raw.get("document_id") or "").strip()
                committed_job = data.setdefault("jobs", {}).get(raw_job_id)
                if (
                    isinstance(committed_job, dict)
                    and str(committed_job.get("status") or "").strip().casefold()
                    in {"completed", "failed"}
                ):
                    cleanup_paths.add(path)
                    continue
                if not self._progress_sidecar_is_stale(path, raw):
                    continue
                try:
                    failed_job = ProcessingJob.model_validate(raw).model_copy(
                        update={
                            "status": "failed",
                            "message": failure_message,
                            "completed_at": now,
                            "error": failure_message,
                        }
                    )
                except Exception:
                    cleanup_paths.add(path)
                    if raw_document_id not in live_document_ids:
                        failed_document_ids.add(raw_document_id)
                    continue
                data.setdefault("jobs", {})[failed_job.job_id] = failed_job.model_dump(
                    mode="json"
                )
                recovered_jobs += 1
                cleanup_paths.add(path)
                if raw_document_id not in live_document_ids:
                    failed_document_ids.add(raw_document_id)

            document_paths = (
                [self._document_progress_path(normalized_document_id)]
                if normalized_document_id
                else list(self.document_progress_root.glob("*.json"))
            )
            for progress_path in document_paths:
                progress_raw = self._read_document_progress_path(progress_path)
                progress_document_id = str(
                    (progress_raw or {}).get("document_id") or ""
                ).strip()
                if (
                    progress_raw is not None
                    and progress_document_id
                    and progress_document_id not in live_document_ids
                    and self._progress_sidecar_is_stale(progress_path, progress_raw)
                ):
                    failed_document_ids.add(progress_document_id)

            for failed_document_id in failed_document_ids:
                if not failed_document_id:
                    continue
                progress_path = self._document_progress_path(failed_document_id)
                raw_document = self._read_document_progress_path(progress_path)
                if raw_document is None:
                    raw_document = data.setdefault("documents", {}).get(
                        failed_document_id
                    )
                if not isinstance(raw_document, dict):
                    cleanup_paths.add(progress_path)
                    continue
                committed_document = data.setdefault("documents", {}).get(
                    failed_document_id
                )
                if (
                    isinstance(committed_document, dict)
                    and str(committed_document.get("status") or "").strip().casefold()
                    in {"completed", "failed"}
                ):
                    cleanup_paths.add(progress_path)
                    continue
                try:
                    failed_document = Document.model_validate(raw_document).model_copy(
                        update={
                            "status": "failed",
                            "processed_at": now,
                            "error": failure_message,
                        }
                    )
                except Exception:
                    cleanup_paths.add(progress_path)
                    continue
                data.setdefault("documents", {})[
                    failed_document.document_id
                ] = failed_document.model_dump(mode="json")
                cleanup_paths.add(progress_path)

            if recovered_jobs or failed_document_ids:
                self._write_json(self.manifest_path, data)
            for path in cleanup_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        return recovered_jobs

    def begin_processing_claim(
        self,
        *,
        document_id: str,
        run_id: str,
        job_id: str,
        previous_owner_run_id: str | None = None,
    ) -> ProcessingClaim:
        """Atomically claim the shared result files for one document.

        The claim is durable so readers fail closed while a writer is active,
        but it is not a long-held file lock. Process identity metadata lets a
        later worker recover a claim immediately after a local process crash.
        """

        normalized_document_id = str(document_id or "").strip()
        normalized_run_id = str(run_id or "").strip()
        normalized_job_id = str(job_id or "").strip()
        if not normalized_document_id or not normalized_run_id or not normalized_job_id:
            raise ValueError("document_id, run_id, and job_id are required for processing claims.")

        path = self._processing_output_state_path(normalized_document_id)
        with _REPOSITORY_LOCK, self._repository_write_lock():
            raw = self._read_processing_output_state_path(path)
            if (
                raw is not None
                and str(raw.get("state") or "").strip().casefold() == "processing"
                and not self._progress_sidecar_is_stale(path, raw)
            ):
                return ProcessingClaim(
                    acquired=False,
                    run_id=str(raw.get("claim_run_id") or "").strip(),
                    job_id=str(raw.get("job_id") or "").strip(),
                    previous_owner_run_id=self._optional_identifier(
                        raw.get("previous_owner_run_id")
                    ),
                )

            prior_owner: str | None = None
            state = str((raw or {}).get("state") or "").strip().casefold()
            if state == "committed":
                prior_owner = self._optional_identifier((raw or {}).get("owner_run_id"))
            elif state == "processing" and not bool((raw or {}).get("outputs_dirty")):
                # A process that died before its first output write did not
                # disturb the previous reusable result set.
                prior_owner = self._optional_identifier(
                    (raw or {}).get("previous_owner_run_id")
                )
            elif raw is None and not path.exists():
                # Backward-compatible adoption for repositories created before
                # durable output ownership was introduced.
                prior_owner = self._optional_identifier(previous_owner_run_id)

            payload: dict[str, object] = {
                "schema_version": "processing-output-owner-v1",
                "document_id": normalized_document_id,
                "state": "processing",
                "claim_run_id": normalized_run_id,
                "job_id": normalized_job_id,
                "previous_owner_run_id": prior_owner,
                "outputs_dirty": False,
            }
            payload.update(self._progress_owner_metadata())
            self._write_processing_output_state(path, payload)
            return ProcessingClaim(
                acquired=True,
                run_id=normalized_run_id,
                job_id=normalized_job_id,
                previous_owner_run_id=prior_owner,
            )

    def finish_processing_claim(
        self,
        *,
        document_id: str,
        run_id: str,
        owner_run_id: str | None,
    ) -> None:
        """Publish a reusable owner, or invalidate outputs, for an active claim."""

        normalized_document_id = str(document_id or "").strip()
        normalized_run_id = str(run_id or "").strip()
        if not normalized_document_id or not normalized_run_id:
            raise ValueError("document_id and run_id are required to finish a processing claim.")
        with _REPOSITORY_LOCK, self._repository_write_lock():
            path = self._processing_output_state_path(normalized_document_id)
            raw = self._require_processing_claim_unlocked(
                path,
                document_id=normalized_document_id,
                run_id=normalized_run_id,
            )
            self._publish_processing_owner_unlocked(
                path,
                document_id=normalized_document_id,
                owner_run_id=self._optional_identifier(owner_run_id),
                invalidated_by_run_id=normalized_run_id,
                prior_state=raw,
            )

    def mark_processing_outputs_dirty(
        self,
        *,
        document_id: str,
        run_id: str,
    ) -> None:
        """Durably invalidate the prior owner before the first output write."""

        with _REPOSITORY_LOCK, self._repository_write_lock():
            self._prepare_processing_output_write_unlocked(
                document_id,
                processing_claim_id=run_id,
            )

    def save_processing_result(
        self,
        document_id: str,
        nodes: list[StructureNode],
        chunks: list[Chunk],
        issues: list[ValidationIssue],
        *,
        processing_claim_id: str | None = None,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> None:
        with _REPOSITORY_LOCK, self._repository_write_lock():
            self._prepare_processing_output_write_unlocked(
                document_id,
                processing_claim_id=processing_claim_id,
            )
            self._write_json_array(
                self._result_path(document_id, "nodes"),
                (node.model_dump(mode="json") for node in nodes),
                total=len(nodes),
                phase="nodes",
                progress_callback=progress_callback,
            )
            self._write_json_array(
                self._result_path(document_id, "chunks"),
                (chunk.model_dump(mode="json") for chunk in chunks),
                total=len(chunks),
                phase="chunks",
                progress_callback=progress_callback,
            )
            self._write_json_array(
                self._result_path(document_id, "issues"),
                (issue.model_dump(mode="json") for issue in issues),
                total=len(issues),
                phase="issues",
                progress_callback=progress_callback,
            )

    def save_chunks(
        self,
        document_id: str,
        chunks: list[Chunk],
        *,
        processing_claim_id: str | None = None,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> None:
        with _REPOSITORY_LOCK, self._repository_write_lock():
            self._prepare_processing_output_write_unlocked(
                document_id,
                processing_claim_id=processing_claim_id,
            )
            self._write_json_array(
                self._result_path(document_id, "chunks"),
                (chunk.model_dump(mode="json") for chunk in chunks),
                total=len(chunks),
                phase="chunks",
                progress_callback=progress_callback,
            )

    def append_approval_record(self, record: dict) -> None:
        approval_id = str(record.get("approval_id") or "").strip()
        if not approval_id:
            raise ValueError("approval_id is required.")
        with _REPOSITORY_LOCK, self._repository_write_lock():
            append_required = self._require_journal_append_compatible("approvals", record)
            if append_required:
                self._append_journal_record("approvals", record, identity_validated=True)

    def list_approval_records(self, document_id: str | None = None) -> list[dict]:
        approvals = self._list_records_with_journal("approvals", "approvals", ("approval_record_id", "approval_id"))
        if document_id:
            approvals = [record for record in approvals if record.get("document_id") == document_id]
        return sorted(approvals, key=lambda record: str(record.get("approved_at") or ""))

    def list_approval_journal_records(self, document_id: str | None = None) -> list[dict]:
        with _REPOSITORY_LOCK, self._repository_read_lock():
            approvals = self._read_journal_records("approvals")
        if document_id:
            approvals = [record for record in approvals if record.get("document_id") == document_id]
        return sorted(approvals, key=lambda record: str(record.get("approved_at") or ""))

    def append_review_record(self, record: dict) -> None:
        review_id = str(record.get("review_id") or "").strip()
        if not review_id:
            raise ValueError("review_id is required.")
        with _REPOSITORY_LOCK, self._repository_write_lock():
            append_required = self._require_journal_append_compatible("review_decisions", record)
            if append_required:
                self._append_journal_record("review_decisions", record, identity_validated=True)

    def list_review_records(self, document_id: str | None = None) -> list[dict]:
        records = self._list_records_with_journal("review_decisions", "review_decisions", ("review_id",))
        if document_id:
            records = [record for record in records if record.get("document_id") == document_id]
        return sorted(records, key=lambda record: str(record.get("reviewed_at") or ""))

    def list_review_journal_records(self, document_id: str | None = None) -> list[dict]:
        """Read review decisions exclusively from the append-only journal."""

        with _REPOSITORY_LOCK, self._repository_read_lock():
            records = self._read_journal_records("review_decisions")
        if document_id:
            records = [record for record in records if record.get("document_id") == document_id]
        return sorted(records, key=lambda record: str(record.get("reviewed_at") or ""))

    def append_indexing_job(self, record: dict) -> None:
        job_id = str(record.get("indexing_job_id") or "").strip()
        if not job_id:
            raise ValueError("indexing_job_id is required.")
        with _REPOSITORY_LOCK, self._repository_write_lock():
            append_required = self._require_journal_append_compatible("indexing_jobs", record)
            if append_required:
                self._append_journal_record("indexing_jobs", record, identity_validated=True)

    def list_indexing_jobs(self, document_id: str | None = None) -> list[dict]:
        jobs = self._list_records_with_journal("indexing_jobs", "indexing_jobs", ("indexing_job_id",))
        if document_id:
            jobs = [record for record in jobs if record.get("document_id") == document_id]
        return sorted(jobs, key=lambda record: str(record.get("created_at") or ""))

    def append_rag_trace(self, record: dict) -> None:
        trace_id = str(record.get("trace_id") or "").strip()
        if not trace_id:
            raise ValueError("trace_id is required.")
        with _REPOSITORY_LOCK, self._repository_write_lock():
            # Search traces are high-volume. Avoid an O(n) pre-append scan;
            # strict readers still reject any conflicting trace identity.
            self._append_journal_record("rag_traces", record, identity_validated=True)

    def list_rag_traces(self, document_id: str | None = None) -> list[dict]:
        traces = self._list_records_with_journal("rag_traces", "rag_traces", ("trace_id",))
        if document_id:
            traces = [
                record
                for record in traces
                if document_id in {item.get("document_id") for item in record.get("result_refs", []) if isinstance(item, dict)}
            ]
        return sorted(traces, key=lambda record: str(record.get("created_at") or ""))

    def append_rag_feedback(self, record: dict) -> None:
        feedback_id = str(record.get("feedback_id") or "").strip()
        if not feedback_id:
            raise ValueError("feedback_id is required.")
        with _REPOSITORY_LOCK, self._repository_write_lock():
            append_required = self._require_journal_append_compatible("rag_feedback", record)
            if append_required:
                self._append_journal_record("rag_feedback", record, identity_validated=True)

    def list_rag_feedback(self, trace_id: str | None = None) -> list[dict]:
        feedback = self._list_records_with_journal("rag_feedback", "rag_feedback", ("feedback_id",))
        if trace_id:
            feedback = [record for record in feedback if record.get("trace_id") == trace_id]
        return sorted(feedback, key=lambda record: str(record.get("created_at") or ""))

    def append_security_scan_record(self, record: dict) -> None:
        scan_id = str(record.get("scan_id") or "").strip()
        if not scan_id:
            raise ValueError("scan_id is required.")
        with _REPOSITORY_LOCK, self._repository_write_lock():
            append_required = self._require_journal_append_compatible("security_scans", record)
            if append_required:
                self._append_journal_record("security_scans", record, identity_validated=True)

    def list_security_scan_records(self, document_id: str | None = None) -> list[dict]:
        records = self._list_records_with_journal("security_scans", "security_scans", ("scan_id",))
        if document_id:
            records = [record for record in records if record.get("document_id") == document_id]
        return sorted(records, key=lambda record: str(record.get("created_at") or ""))

    def append_maintenance_event(self, record: dict) -> None:
        event_id = str(record.get("event_id") or "").strip()
        if not event_id:
            raise ValueError("event_id is required.")
        with _REPOSITORY_LOCK, self._repository_write_lock():
            self._append_journal_record("maintenance_events", record)

    def list_maintenance_events(self, event_type: str | None = None) -> list[dict]:
        with _REPOSITORY_LOCK, self._repository_read_lock():
            records = self._read_journal_records("maintenance_events")
        if event_type:
            records = [record for record in records if record.get("event_type") == event_type]
        return sorted(records, key=lambda record: str(record.get("created_at") or ""))

    def get_nodes(self, document_id: str) -> list[StructureNode]:
        return [StructureNode.model_validate(raw) for raw in self._read_result(document_id, "nodes")]

    def get_chunks(self, document_id: str) -> list[Chunk]:
        return [Chunk.model_validate(raw) for raw in self._read_result(document_id, "chunks")]

    def get_chunk_records(self, document_id: str) -> list[dict]:
        """Chunks as stored, without schema validation.

        조항 수나 승인 상태만 세면 되는 화면이 있다. 규정 100여 개를 한꺼번에 세는데
        전부 검증까지 하면 첫 화면이 10초 넘게 멈춘다. 세기만 할 때는 이쪽을 쓴다.
        """
        return [raw for raw in self._read_result(document_id, "chunks") if isinstance(raw, dict)]

    def get_issues(self, document_id: str) -> list[ValidationIssue]:
        return [ValidationIssue.model_validate(raw) for raw in self._read_result(document_id, "issues")]

    def save_quality_report(
        self,
        document_id: str,
        report: QualityReport,
        *,
        processing_claim_id: str | None = None,
    ) -> None:
        with _REPOSITORY_LOCK, self._repository_write_lock():
            self._prepare_processing_output_write_unlocked(
                document_id,
                processing_claim_id=processing_claim_id,
            )
            self._write_json(self._result_path(document_id, "quality"), report.model_dump(mode="json"))

    def get_quality_report(self, document_id: str) -> QualityReport | None:
        raw = self._read_result(document_id, "quality")
        return QualityReport.model_validate(raw) if raw else None

    def upsert_run(self, run: ProcessingRun) -> None:
        with _REPOSITORY_LOCK, self._repository_write_lock():
            record = run.model_dump(mode="json")
            append_required = self._require_journal_append_compatible("runs", record)
            state_path = self._processing_output_state_path(run.document_id)
            self._publish_processing_owner_unlocked(
                state_path,
                document_id=run.document_id,
                owner_run_id=run.run_id if run.status == "completed" else None,
                invalidated_by_run_id=run.run_id,
                prior_state=self._read_processing_output_state_path(state_path),
            )
            if append_required:
                self._append_journal_record("runs", record, identity_validated=True)

    def commit_processing_outcome(
        self,
        *,
        document: Document,
        job: ProcessingJob,
        run: ProcessingRun,
        processing_claim_id: str | None = None,
    ) -> None:
        """Exception-atomically commit one terminal document, job, and run outcome."""

        if document.document_id != job.document_id or document.document_id != run.document_id:
            raise ValueError("Processing outcome document identifiers must match.")
        if job.job_id != run.job_id:
            raise ValueError("Processing outcome job identifiers must match.")
        if job.status not in {"completed", "failed"} or run.status not in {"completed", "failed"}:
            raise ValueError("Processing outcome must be terminal.")
        with _REPOSITORY_LOCK, self._repository_write_lock():
            run_journal_path = self._journal_path("runs")
            state_path = self._processing_output_state_path(document.document_id)
            manifest_snapshot = _capture_file_snapshot(self.manifest_path)
            run_journal_snapshot = _capture_file_snapshot(run_journal_path)
            state_snapshot = _capture_file_snapshot(state_path)
            run_record = run.model_dump(mode="json")
            append_required = self._require_journal_append_compatible("runs", run_record)
            prior_state = self._read_processing_output_state_path(state_path)
            if processing_claim_id is not None:
                prior_state = self._require_processing_claim_unlocked(
                    state_path,
                    document_id=document.document_id,
                    run_id=processing_claim_id,
                )
            data = self._read_manifest_for_update()
            data.setdefault("documents", {})[document.document_id] = document.model_dump(mode="json")
            data.setdefault("jobs", {})[job.job_id] = job.model_dump(mode="json")
            try:
                # Publish terminal document/job state before the run marker so
                # a process crash cannot leave a false completed run that is
                # later reused while the manifest still says "processing".
                self._write_json(self.manifest_path, data)
                if append_required:
                    self._append_journal_record(
                        "runs",
                        run_record,
                        identity_validated=True,
                    )
                self._publish_processing_owner_unlocked(
                    state_path,
                    document_id=document.document_id,
                    owner_run_id=run.run_id if run.status == "completed" else None,
                    invalidated_by_run_id=run.run_id,
                    prior_state=prior_state,
                )
            except BaseException as exc:
                rollback_errors: list[str] = []
                for path, snapshot in (
                    (state_path, state_snapshot),
                    (run_journal_path, run_journal_snapshot),
                    (self.manifest_path, manifest_snapshot),
                ):
                    try:
                        _restore_file_snapshot(path, snapshot)
                    except Exception as rollback_exc:
                        rollback_errors.append(f"{path.name}: {rollback_exc}")
                self._manifest_cache = None
                self._manifest_identity = None
                journal_cache_key = str(run_journal_path.resolve())
                _JOURNAL_RECORD_CACHE.pop(journal_cache_key, None)
                _JOURNAL_IDENTITY_CACHE.pop(journal_cache_key, None)
                if rollback_errors and hasattr(exc, "add_note"):
                    exc.add_note(
                        "Processing outcome rollback also failed: "
                        + "; ".join(rollback_errors)
                    )
                raise
            for progress_path in (
                self._job_progress_path(job.job_id),
                self._document_progress_path(document.document_id),
            ):
                try:
                    progress_path.unlink(missing_ok=True)
                except Exception:
                    # Terminal manifest state takes precedence over stale
                    # progress sidecars in all readers, so cleanup failure must
                    # not turn a successful commit into a false failure.
                    pass

    def get_run(self, run_id: str) -> ProcessingRun | None:
        raw = next(
            (
                record
                for record in self._list_records_with_journal("runs", "runs", ("run_id",))
                if str(record.get("run_id") or "") == run_id
            ),
            None,
        )
        return ProcessingRun.model_validate(raw) if raw else None

    def list_runs(self, document_id: str | None = None) -> list[ProcessingRun]:
        runs = [
            ProcessingRun.model_validate(raw)
            for raw in self._list_records_with_journal("runs", "runs", ("run_id",))
        ]
        if document_id:
            runs = [run for run in runs if run.document_id == document_id]
        return sorted(runs, key=lambda run: run.started_at)

    def latest_completed_run(
        self,
        document_id: str,
        *,
        options: dict | None = None,
        require_outputs: bool = False,
        processing_claim_id: str | None = None,
    ) -> ProcessingRun | None:
        document_runs = self.list_runs(document_id)
        return self._latest_completed_run_from_document_runs(
            document_id,
            document_runs,
            options=options,
            require_outputs=require_outputs,
            processing_claim_id=processing_claim_id,
        )

    def _latest_completed_run_from_document_runs(
        self,
        document_id: str,
        document_runs: list[ProcessingRun],
        *,
        options: dict | None = None,
        require_outputs: bool = False,
        processing_claim_id: str | None = None,
    ) -> ProcessingRun | None:
        if require_outputs:
            owner_run_id = self._reusable_output_owner_run_id(
                document_id,
                document_runs=document_runs,
                processing_claim_id=processing_claim_id,
            )
            if owner_run_id is None:
                return None
            candidate = next(
                (run for run in document_runs if run.run_id == owner_run_id),
                None,
            )
            if candidate is None:
                return None
            if candidate.status != "completed":
                return None
            if options is not None and self._canonical_json(
                candidate.options
            ) != self._canonical_json(options):
                return None
            return (
                candidate
                if self._has_reusable_outputs(
                    document_id,
                    candidate,
                    expected_owner_run_id=owner_run_id,
                )
                else None
            )

        runs = [run for run in document_runs if run.status == "completed"]
        if options is not None:
            expected = self._canonical_json(options)
            runs = [run for run in runs if self._canonical_json(run.options) == expected]
        return runs[-1] if runs else None

    def has_reusable_outputs(self, document_id: str, run: ProcessingRun) -> bool:
        document_runs = self.list_runs(document_id)
        owner_run_id = self._reusable_output_owner_run_id(
            document_id,
            document_runs=document_runs,
        )
        return self._has_reusable_outputs(
            document_id,
            run,
            expected_owner_run_id=owner_run_id,
        )

    def _has_reusable_outputs(
        self,
        document_id: str,
        run: ProcessingRun,
        *,
        expected_owner_run_id: str | None,
    ) -> bool:
        if run.status != "completed" or run.document_id != document_id:
            return False
        if not self._run_outputs_still_match_document_results(
            run,
            expected_owner_run_id=expected_owner_run_id,
        ):
            return False
        return self._stored_results_are_reusable(document_id) and self._run_artifacts_are_reusable(run)

    def find_reusable_run(
        self,
        *,
        file_hash: str,
        options: dict,
        tenant_id: str | None = None,
        source_system: str | None = None,
        source_record_id: str | None = None,
        source_file_id: str | None = None,
        profile_id: str | None = None,
        document_name: str | None = None,
        institution_name: str | None = None,
        source_url: str | None = None,
        source_disclosure_date: str | None = None,
        source_posted_date: str | None = None,
    ) -> tuple[Document, ProcessingRun] | None:
        has_full_source_identity = bool(source_system and source_record_id and source_file_id)
        if has_full_source_identity:
            candidates = self.find_documents_by_source(
                source_system=source_system,
                source_record_id=source_record_id,
                source_file_id=source_file_id,
                profile_id=profile_id,
            )
            candidates = [document for document in candidates if document.file_hash == file_hash]
        elif source_system and source_file_id:
            candidates = self.find_documents_by_source(
                source_system=source_system,
                source_file_id=source_file_id,
                profile_id=profile_id,
            )
            candidates = [document for document in candidates if document.file_hash == file_hash]
        elif source_system or source_record_id or source_file_id:
            candidates = self._find_documents_by_hash_and_profile(file_hash, profile_id)
        else:
            candidates = self._find_documents_by_hash_and_profile(file_hash, profile_id)
        if tenant_id:
            normalized_tenant_id = self._normalize_key(tenant_id)
            candidates = [
                document
                for document in candidates
                if self._normalize_key(document.tenant_id) == normalized_tenant_id
            ]
        candidates = self._filter_documents_by_provenance(
            candidates,
            document_name=document_name,
            institution_name=institution_name,
            source_url=source_url,
            source_disclosure_date=source_disclosure_date,
            source_posted_date=source_posted_date,
        )
        runs_by_document: dict[str, list[ProcessingRun]] = {}
        for run in self.list_runs():
            runs_by_document.setdefault(run.document_id, []).append(run)
        for document in reversed(candidates):
            run = self._latest_completed_run_from_document_runs(
                document.document_id,
                runs_by_document.get(document.document_id, []),
                options=options,
                require_outputs=True,
            )
            if run is not None:
                return document, run
        return None

    def _run_outputs_still_match_document_results(
        self,
        run: ProcessingRun,
        *,
        expected_owner_run_id: str | None,
    ) -> bool:
        """Return whether ``run`` exclusively owns the current result files."""

        return bool(expected_owner_run_id and expected_owner_run_id == run.run_id)

    def _filter_documents_by_provenance(
        self,
        documents: list[Document],
        *,
        document_name: str | None = None,
        institution_name: str | None = None,
        source_url: str | None = None,
        source_disclosure_date: str | None = None,
        source_posted_date: str | None = None,
    ) -> list[Document]:
        expected_fields = {
            "document_name": document_name,
            "institution_name": institution_name,
            "source_url": source_url,
            "source_disclosure_date": source_disclosure_date,
            "source_posted_date": source_posted_date,
        }
        for field_name, expected in expected_fields.items():
            if expected not in (None, ""):
                documents = [
                    document
                    for document in documents
                    if self._normalize_key(getattr(document, field_name)) == self._normalize_key(expected)
                ]
        return documents

    def _stored_results_are_reusable(self, document_id: str) -> bool:
        required_json = {
            "nodes": list,
            "chunks": list,
            "issues": list,
            "quality": dict,
        }
        for result_type, expected_type in required_json.items():
            path = self._result_path(document_id, result_type)
            if not path.is_file():
                return False
            try:
                if expected_type is list:
                    count = sum(1 for _item in self._iter_json_array(path))
                    if result_type in {"nodes", "chunks"} and count == 0:
                        return False
                    continue
                with path.open("r", encoding="utf-8") as handle:
                    raw = json.load(handle)
            except (OSError, json.JSONDecodeError):
                return False
            if not isinstance(raw, expected_type):
                return False
        return self.get_quality_report(document_id) is not None

    def _run_artifacts_are_reusable(self, run: ProcessingRun) -> bool:
        required_artifacts = (
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
        )
        for artifact_name in required_artifacts:
            raw_path = (run.artifacts or {}).get(artifact_name)
            if not raw_path or not self._artifact_exists(raw_path):
                return False
        return True

    def _artifact_exists(self, raw_path: str) -> bool:
        path = Path(raw_path)
        if path.is_absolute():
            return path.is_file()
        candidates = [
            path,
            self.data_dir / path,
            self.data_dir.parent / path,
        ]
        return any(candidate.is_file() for candidate in candidates)

    def _find_documents_by_hash_and_profile(self, file_hash: str, profile_id: str | None = None) -> list[Document]:
        candidates = self.find_documents_by_hash(file_hash)
        if profile_id:
            candidates = [
                document
                for document in candidates
                if self._normalize_key(document.profile_id) == self._normalize_key(profile_id)
            ]
        return candidates

    def _empty_manifest(self) -> dict:
        return {
            "documents": {},
            "jobs": {},
            "runs": {},
            "approvals": {},
            "review_decisions": {},
            "indexing_jobs": {},
            "rag_traces": {},
            "rag_feedback": {},
            "security_scans": {},
        }

    def _read_manifest(self) -> dict:
        current_identity = self._file_identity(self.manifest_path)
        if self._manifest_cache is not None and self._manifest_identity == current_identity:
            return self._manifest_cache
        if current_identity is None:
            self._manifest_cache = self._empty_manifest()
            self._manifest_identity = None
            return self._manifest_cache
        self._manifest_cache = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self._manifest_identity = self._file_identity(self.manifest_path)
        return self._manifest_cache

    def _read_manifest_for_update(self) -> dict:
        """Read committed state while holding the repository write lock.

        Writers must not mutate the shared read cache before an atomic replace
        succeeds, and must not base a write on a stale cache when filesystem
        timestamp granularity hides another process's recent replace.
        """

        if not self.manifest_path.exists():
            return self._empty_manifest()
        data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Repository manifest must contain a JSON object.")
        self._migrate_manifest_runs_to_journal(data)
        self._remove_exact_journal_mirrors(data)
        return data

    def _migrate_manifest_runs_to_journal(self, data: dict) -> None:
        """Copy legacy run rows to their journal before removing exact mirrors."""

        runs = data.get("runs")
        if not isinstance(runs, dict) or not runs:
            return
        for record in runs.values():
            if not isinstance(record, dict):
                continue
            append_required = self._require_journal_append_compatible("runs", record)
            if append_required:
                self._append_journal_record("runs", record, identity_validated=True)

    def _remove_exact_journal_mirrors(self, data: dict) -> None:
        """Drop legacy manifest copies only when the journal has the same row."""

        for manifest_key, journal_name in _MANIFEST_JOURNAL_MIRRORS.items():
            mirrored = data.get(manifest_key)
            if not isinstance(mirrored, dict) or not mirrored:
                continue
            id_fields = _JOURNAL_ID_FIELDS[journal_name]
            journal_index = self._journal_identity_index(journal_name)
            if not journal_index:
                continue
            for manifest_record_key, record in list(mirrored.items()):
                if not isinstance(record, dict):
                    continue
                record_id = self._record_identity(record, id_fields)
                if record_id and journal_index.get(record_id) == self._record_digest(record):
                    del mirrored[manifest_record_key]

    def _read_legacy(self) -> dict:
        current_identity = self._file_identity(self.legacy_path)
        if self._legacy_cache is not None and self._legacy_identity == current_identity:
            return self._legacy_cache
        if current_identity is None:
            self._legacy_cache = {"documents": {}, "jobs": {}, "nodes": {}, "chunks": {}, "issues": {}}
            self._legacy_identity = None
            return self._legacy_cache
        try:
            self._legacy_cache = json.loads(self.legacy_path.read_text(encoding="utf-8"))
            self._legacy_identity = self._file_identity(self.legacy_path)
            return self._legacy_cache
        except json.JSONDecodeError:
            self._legacy_cache = {"documents": {}, "jobs": {}, "nodes": {}, "chunks": {}, "issues": {}}
            self._legacy_identity = self._file_identity(self.legacy_path)
            return self._legacy_cache

    def _result_path(self, document_id: str, result_type: str) -> Path:
        return self.root / f"{document_id}_{result_type}.json"

    def _processing_output_state_path(self, document_id: str) -> Path:
        digest = hashlib.sha256(str(document_id or "").encode("utf-8")).hexdigest()
        return self.processing_owner_root / f"{digest}.json"

    def _read_processing_output_state_path(self, path: Path) -> dict | None:
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return raw if isinstance(raw, dict) else None

    def _write_processing_output_state(self, path: Path, payload: dict) -> None:
        self.processing_owner_root.mkdir(parents=True, exist_ok=True)
        self._write_json(path, payload)

    @staticmethod
    def _optional_identifier(value: object) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    def _require_processing_claim_unlocked(
        self,
        path: Path,
        *,
        document_id: str,
        run_id: str,
    ) -> dict:
        raw = self._read_processing_output_state_path(path)
        if (
            raw is None
            or str(raw.get("state") or "").strip().casefold() != "processing"
            or str(raw.get("document_id") or "").strip() != document_id
            or str(raw.get("claim_run_id") or "").strip() != run_id
        ):
            raise RuntimeError(
                "The processing output claim is missing or belongs to another writer."
            )
        return raw

    def _prepare_processing_output_write_unlocked(
        self,
        document_id: str,
        *,
        processing_claim_id: str | None,
    ) -> None:
        normalized_document_id = str(document_id or "").strip()
        if not normalized_document_id:
            raise ValueError("document_id is required when writing processing outputs.")
        path = self._processing_output_state_path(normalized_document_id)
        normalized_claim_id = self._optional_identifier(processing_claim_id)
        if normalized_claim_id is not None:
            raw = self._require_processing_claim_unlocked(
                path,
                document_id=normalized_document_id,
                run_id=normalized_claim_id,
            )
            if bool(raw.get("outputs_dirty")):
                return
            updated = dict(raw)
            updated["outputs_dirty"] = True
            updated.update(self._progress_owner_metadata())
            self._write_processing_output_state(path, updated)
            return

        raw = self._read_processing_output_state_path(path)
        if (
            raw is not None
            and str(raw.get("state") or "").strip().casefold() == "processing"
            and not self._progress_sidecar_is_stale(path, raw)
        ):
            raise RuntimeError(
                "Processing outputs cannot be changed while another writer is active."
            )
        # Unclaimed maintenance edits cannot keep an earlier processing run as
        # the reusable owner, even when the edited files remain valid JSON.
        self._publish_processing_owner_unlocked(
            path,
            document_id=normalized_document_id,
            owner_run_id=None,
            invalidated_by_run_id="unclaimed-output-write",
            prior_state=raw,
        )

    def _publish_processing_owner_unlocked(
        self,
        path: Path,
        *,
        document_id: str,
        owner_run_id: str | None,
        invalidated_by_run_id: str,
        prior_state: dict | None,
    ) -> None:
        if (
            prior_state is not None
            and str(prior_state.get("state") or "").strip().casefold()
            == "processing"
            and not self._progress_sidecar_is_stale(path, prior_state)
            and str(prior_state.get("claim_run_id") or "").strip()
            != invalidated_by_run_id
        ):
            raise RuntimeError(
                "The document output namespace is owned by another processing run."
            )
        normalized_owner = self._optional_identifier(owner_run_id)
        payload: dict[str, object] = {
            "schema_version": "processing-output-owner-v1",
            "document_id": document_id,
            "state": "committed" if normalized_owner else "invalid",
            "owner_run_id": normalized_owner,
            "invalidated_by_run_id": invalidated_by_run_id,
        }
        payload.update(self._progress_owner_metadata())
        self._write_processing_output_state(path, payload)

    def _reusable_output_owner_run_id(
        self,
        document_id: str,
        *,
        document_runs: list[ProcessingRun],
        processing_claim_id: str | None = None,
    ) -> str | None:
        path = self._processing_output_state_path(document_id)
        with _REPOSITORY_LOCK, self._repository_read_lock():
            marker_exists = path.is_file()
            raw = self._read_processing_output_state_path(path)
        if marker_exists:
            if (
                raw is None
                or str(raw.get("document_id") or "").strip() != document_id
            ):
                return None
            state = str(raw.get("state") or "").strip().casefold()
            if state == "committed":
                return self._optional_identifier(raw.get("owner_run_id"))
            if (
                state == "processing"
                and not bool(raw.get("outputs_dirty"))
                and self._optional_identifier(processing_claim_id)
                == self._optional_identifier(raw.get("claim_run_id"))
            ):
                return self._optional_identifier(raw.get("previous_owner_run_id"))
            return None

        # Legacy repositories have no owner marker. Their best available
        # evidence is the final run by started_at, matching the old behavior.
        if not document_runs or document_runs[-1].status != "completed":
            return None
        return document_runs[-1].run_id

    def _job_progress_path(self, job_id: str) -> Path:
        digest = hashlib.sha256(str(job_id or "").encode("utf-8")).hexdigest()
        return self.job_progress_root / f"{digest}.json"

    def _document_progress_path(self, document_id: str) -> Path:
        digest = hashlib.sha256(str(document_id or "").encode("utf-8")).hexdigest()
        return self.document_progress_root / f"{digest}.json"

    def _progress_owner_metadata(self) -> dict[str, object]:
        return {
            "_progress_owner_host": _PROGRESS_OWNER_HOST,
            "_progress_owner_pid": os.getpid(),
            "_progress_owner_identity": _own_process_identity() or "",
            "_progress_updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _progress_sidecar_is_stale(self, path: Path, raw: dict) -> bool:
        owner_host = str(raw.get("_progress_owner_host") or "").strip().casefold()
        try:
            owner_pid = int(raw.get("_progress_owner_pid") or 0)
        except (TypeError, ValueError):
            owner_pid = 0
        if owner_host and owner_host == _PROGRESS_OWNER_HOST and owner_pid > 0:
            current_identity = (
                _own_process_identity()
                if owner_pid == os.getpid()
                else _process_identity(owner_pid)
            )
            if current_identity is None:
                return True
            expected_identity = str(
                raw.get("_progress_owner_identity") or ""
            ).strip()
            if (
                current_identity.startswith("live:")
                or expected_identity.startswith("live:")
            ):
                return False
            return bool(
                expected_identity and current_identity != expected_identity
            )
        try:
            age_seconds = max(0.0, time.time() - path.stat().st_mtime)
        except OSError:
            return False
        return age_seconds >= _LEGACY_PROGRESS_STALE_SECONDS

    def _read_job_progress_path(self, path: Path) -> dict | None:
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return raw if isinstance(raw, dict) else None

    def _read_document_progress_path(self, path: Path) -> dict | None:
        return self._read_job_progress_path(path)

    def _read_result(self, document_id: str, result_type: str) -> list | dict:
        path = self._result_path(document_id, result_type)
        if path.exists():
            if result_type in {"nodes", "chunks", "issues"}:
                return list(self._iter_json_array(path))
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        compressed_path = Path(f"{path}.gz")
        if compressed_path.is_file():
            with gzip.open(compressed_path, "rt", encoding="utf-8") as handle:
                return json.load(handle)
        legacy = self._read_legacy()
        return legacy.get(result_type, {}).get(document_id, [])

    def _journal_path(self, journal_name: str) -> Path:
        return self.root / "journals" / f"{journal_name}.jsonl"

    def _journal_file_identity(self, path: Path) -> _JournalFileIdentity:
        """Identify the immutable gzip base and mutable JSONL tail together."""

        compressed_path = Path(f"{path}.gz")
        return (
            self._file_identity(compressed_path) if compressed_path.is_file() else None,
            self._file_identity(path) if path.is_file() else None,
        )

    def _append_journal_record(
        self,
        journal_name: str,
        record: dict,
        *,
        identity_validated: bool = False,
    ) -> None:
        if not identity_validated and not self._require_journal_append_compatible(journal_name, record):
            return
        path = self._journal_path(journal_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        before_identity = self._journal_file_identity(path)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        cache_key = str(path.resolve())
        cached = _journal_cache_get(_JOURNAL_IDENTITY_CACHE, cache_key)
        if cached is not None and cached[0] == before_identity:
            updated_index = dict(cached[1])
            id_fields = _JOURNAL_ID_FIELDS.get(journal_name, ())
            record_id = self._record_identity(record, id_fields) if id_fields else ""
            if record_id:
                updated_index[record_id] = self._record_digest(record)
            _journal_cache_set(
                _JOURNAL_IDENTITY_CACHE,
                cache_key,
                (self._journal_file_identity(path), updated_index),
                max_entries=_JOURNAL_IDENTITY_CACHE_MAX_ENTRIES,
            )
        else:
            _JOURNAL_IDENTITY_CACHE.pop(cache_key, None)
        cached_records = _journal_cache_get(_JOURNAL_RECORD_CACHE, cache_key)
        if cached_records is not None and cached_records[0] == before_identity:
            updated_records = list(cached_records[1])
            updated_records.append(record)
            _journal_cache_set(
                _JOURNAL_RECORD_CACHE,
                cache_key,
                (self._journal_file_identity(path), updated_records),
                max_entries=_JOURNAL_RECORD_CACHE_MAX_ENTRIES,
            )
        else:
            _JOURNAL_RECORD_CACHE.pop(cache_key, None)

    def _read_journal_records(self, journal_name: str) -> list[dict]:
        path = self._journal_path(journal_name)
        compressed_path = Path(f"{path}.gz")
        identity = self._journal_file_identity(path)
        if not any(identity):
            return []
        cache_key = str(path.resolve())
        cached = _journal_cache_get(_JOURNAL_RECORD_CACHE, cache_key)
        if cached is not None and cached[0] == identity:
            return list(cached[1])
        records: list[dict] = []
        records_by_id: dict[str, dict] = {}
        id_fields = _JOURNAL_ID_FIELDS.get(journal_name, ())
        try:
            line_number = 0
            for journal_path, is_compressed in (
                (compressed_path, True),
                (path, False),
            ):
                if not journal_path.is_file():
                    continue
                journal_handle = (
                    gzip.open(journal_path, "rt", encoding="utf-8")
                    if is_compressed
                    else journal_path.open("r", encoding="utf-8")
                )
                with journal_handle as handle:
                    for line in _iter_journal_lines(handle):
                        line_number += 1
                        if not line.strip():
                            continue
                        try:
                            item = json.loads(line, object_pairs_hook=_journal_json_object)
                        except _DuplicateJournalJsonKey as exc:
                            raise JournalIntegrityError(
                                f"Journal '{journal_name}' contains a duplicate JSON key at line {line_number}."
                            ) from exc
                        except json.JSONDecodeError as exc:
                            raise JournalIntegrityError(
                                f"Journal '{journal_name}' contains malformed JSON at line {line_number}."
                            ) from exc
                        if not isinstance(item, dict):
                            raise JournalIntegrityError(
                                f"Journal '{journal_name}' contains a non-object record at line {line_number}."
                            )
                        record_id = self._record_identity(item, id_fields) if id_fields else ""
                        if id_fields and not record_id:
                            raise JournalIntegrityError(
                                f"Journal '{journal_name}' is missing its record identity at line {line_number}."
                            )
                        previous = records_by_id.get(record_id) if record_id else None
                        if previous is not None and previous != item:
                            raise JournalIntegrityError(
                                f"Journal '{journal_name}' contains conflicting records for identity "
                                f"'{record_id[:128]}' at line {line_number}."
                            )
                        if record_id and previous is None:
                            records_by_id[record_id] = item
                        records.append(item)
        except (OSError, UnicodeError) as exc:
            raise JournalIntegrityError(f"Journal '{journal_name}' could not be read as UTF-8 JSONL.") from exc
        _journal_cache_set(
            _JOURNAL_RECORD_CACHE,
            cache_key,
            (identity, records),
            max_entries=_JOURNAL_RECORD_CACHE_MAX_ENTRIES,
        )
        return list(records)

    def _require_journal_append_compatible(self, journal_name: str, record: dict) -> bool:
        id_fields = _JOURNAL_ID_FIELDS.get(journal_name, ())
        if not id_fields:
            return True
        record_id = self._record_identity(record, id_fields)
        if not record_id:
            raise ValueError(f"Journal '{journal_name}' record identity is required.")
        existing_digest = self._journal_identity_index(journal_name).get(record_id)
        if existing_digest is not None:
            if existing_digest != self._record_digest(record):
                raise JournalIntegrityError(
                    f"Journal '{journal_name}' already contains a conflicting record for identity "
                    f"'{record_id[:128]}'."
                )
            return False
        return True

    def _journal_identity_index(self, journal_name: str) -> dict[str, str]:
        path = self._journal_path(journal_name)
        identity = self._journal_file_identity(path)
        cache_key = str(path.resolve())
        cached = _journal_cache_get(_JOURNAL_IDENTITY_CACHE, cache_key)
        if cached is not None and cached[0] == identity:
            return cached[1]
        id_fields = _JOURNAL_ID_FIELDS.get(journal_name, ())
        index: dict[str, str] = {}
        if id_fields and any(identity):
            for record in self._read_journal_records(journal_name):
                record_id = self._record_identity(record, id_fields)
                if record_id:
                    index[record_id] = self._record_digest(record)
        _journal_cache_set(
            _JOURNAL_IDENTITY_CACHE,
            cache_key,
            (identity, index),
            max_entries=_JOURNAL_IDENTITY_CACHE_MAX_ENTRIES,
        )
        return index

    def _record_digest(self, record: dict) -> str:
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _list_records_with_journal(
        self,
        manifest_key: str,
        journal_name: str,
        id_fields: tuple[str, ...],
    ) -> list[dict]:
        with _REPOSITORY_LOCK, self._repository_read_lock():
            records_by_id: dict[str, dict] = {}
            for record in self._read_manifest().get(manifest_key, {}).values():
                record_id = self._record_identity(record, id_fields)
                if record_id:
                    records_by_id[record_id] = record
            for record in self._read_journal_records(journal_name):
                record_id = self._record_identity(record, id_fields)
                if record_id:
                    records_by_id[record_id] = record
        return list(records_by_id.values())

    def _record_identity(self, record: dict, id_fields: tuple[str, ...]) -> str:
        for field in id_fields:
            value = str(record.get(field) or "").strip()
            if value:
                return value
        return ""

    def _write_json(self, path: Path, data: dict | list) -> None:
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
        try:
            with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
                for piece in encoder.iterencode(data):
                    handle.write(piece)
            _replace_with_retry(tmp_path, path)
        finally:
            tmp_path.unlink(missing_ok=True)
        if path == self.manifest_path:
            self._manifest_cache = deepcopy(data) if isinstance(data, dict) else None
            self._manifest_identity = self._file_identity(path)

    def _write_json_array(
        self,
        path: Path,
        records: Iterable[object],
        *,
        total: int,
        phase: str,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> None:
        """Atomically write a JSON array without materializing the full payload."""

        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
        write_buffer_chars = 64 * 1024
        progress_interval = max(1, (total + 99) // 100) if total else 1
        try:
            with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write("[")
                written = 0
                for written, record in enumerate(records, start=1):
                    if written > 1:
                        handle.write(",")
                    if not _json_value_needs_buffered_encoding(record):
                        # The usual small record stays on the fast one-write path.
                        handle.write(encoder.encode(record))
                    else:
                        # Coalesce the encoder's pieces into bounded writes.
                        # This avoids a second full-record string for exceptional
                        # large tables or parser inventory rows.
                        pieces: list[str] = []
                        buffered_chars = 0
                        for piece in encoder.iterencode(record):
                            pieces.append(piece)
                            buffered_chars += len(piece)
                            if buffered_chars >= write_buffer_chars:
                                handle.write("".join(pieces))
                                pieces.clear()
                                buffered_chars = 0
                        if pieces:
                            handle.write("".join(pieces))
                    if progress_callback is not None and (
                        written == 1 or written == total or written % progress_interval == 0
                    ):
                        progress_callback(phase, written, total)
                handle.write("]")
                if progress_callback is not None and written == 0:
                    progress_callback(phase, 0, total)
            _replace_with_retry(tmp_path, path)
        finally:
            tmp_path.unlink(missing_ok=True)

    def _iter_json_array(self, path: Path, *, read_size: int = 64 * 1024) -> Iterator[object]:
        """Incrementally decode a JSON array while retaining only one record buffer."""

        decoder = json.JSONDecoder()
        with path.open("r", encoding="utf-8") as handle:
            buffer = ""

            def fill(requested_size: int | None = None) -> bool:
                nonlocal buffer
                part = handle.read(
                    read_size if requested_size is None else requested_size
                )
                if not part:
                    return False
                buffer += part
                return True

            while not buffer.strip() and fill():
                pass
            buffer = buffer.lstrip()
            if not buffer or buffer[0] != "[":
                raise json.JSONDecodeError("Expected a JSON array", buffer, 0)
            buffer = buffer[1:]
            state = "first_or_end"

            while True:
                while not buffer.strip():
                    if not fill():
                        raise json.JSONDecodeError("Unterminated JSON array", buffer, len(buffer))
                buffer = buffer.lstrip()

                if state in {"first_or_end", "comma_or_end"} and buffer.startswith("]"):
                    buffer = buffer[1:]
                    remainder = buffer + handle.read()
                    if remainder.strip():
                        raise json.JSONDecodeError("Trailing data after JSON array", remainder, 0)
                    return

                if state == "comma_or_end":
                    if not buffer.startswith(","):
                        raise json.JSONDecodeError("Expected ',' or ']'", buffer, 0)
                    buffer = buffer[1:]
                    state = "value"
                    continue

                if state == "value" and buffer.startswith("]"):
                    raise json.JSONDecodeError("Trailing comma in JSON array", buffer, 0)

                next_decode_read_size = max(1, read_size)
                while True:
                    try:
                        value, end = decoder.raw_decode(buffer)
                        break
                    except json.JSONDecodeError:
                        if not fill(next_decode_read_size):
                            raise
                        # A large string, table, or nested inventory is still one
                        # top-level JSON value. Retrying after every fixed 64 KiB
                        # chunk reparses its entire prefix and becomes quadratic.
                        # Geometric refills bound the repeated prefix work while
                        # retaining only this record and any small read-ahead.
                        next_decode_read_size *= 2
                yield value
                buffer = buffer[end:]
                state = "comma_or_end"

    def _file_identity(self, path: Path) -> _FileIdentity | None:
        """Return a cache identity that survives coarse or restored mtimes.

        Size, ctime, and inode close the stale-read hole where an atomic
        replacement deliberately preserves both the old mtime and byte count.
        """

        try:
            stat = path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns, stat.st_ino)

    def _canonical_json(self, value: dict) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _normalize_key(self, value: str | None) -> str:
        return str(value or "").strip().lower()

    @contextmanager
    def _repository_read_lock(self):
        lock_path = self.root / ".write.lock"
        try:
            handle = lock_path.open("rb")
        except FileNotFoundError:
            try:
                handle = lock_path.open("a+b")
            except OSError as exc:
                if not isinstance(exc, PermissionError) and exc.errno not in {
                    errno.EACCES,
                    errno.EPERM,
                    errno.EROFS,
                }:
                    raise
                # A read-only repository without a lock file cannot be changed
                # by this process. Strict parsing still fails closed if an
                # external writer exposes an incomplete record.
                yield
                return
        with handle:
            _lock_handle(handle)
            try:
                yield
            finally:
                _unlock_handle(handle)

    @contextmanager
    def _repository_write_lock(self):
        lock_path = self.root / ".write.lock"
        with lock_path.open("a+b") as handle:
            _lock_handle(handle)
            try:
                yield
            finally:
                _unlock_handle(handle)


def _capture_file_snapshot(path: Path) -> tuple[bool, bytes]:
    return (True, path.read_bytes()) if path.is_file() else (False, b"")


def _restore_file_snapshot(path: Path, snapshot: tuple[bool, bytes]) -> None:
    existed, payload = snapshot
    if not existed:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.rollback.tmp")
    try:
        with tmp_path.open("wb") as handle:
            handle.write(payload)
        _replace_with_retry(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _own_process_identity() -> str | None:
    global _CURRENT_PROCESS_IDENTITY
    pid = os.getpid()
    if (
        _CURRENT_PROCESS_IDENTITY is None
        or _CURRENT_PROCESS_IDENTITY[0] != pid
    ):
        _CURRENT_PROCESS_IDENTITY = (pid, _process_identity(pid))
    return _CURRENT_PROCESS_IDENTITY[1]


def _process_identity(pid: int) -> str | None:
    """Return a process-start identity, or ``None`` when the PID is not live."""

    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(
                process_query_limited_information,
                False,
                pid,
            )
            if handle:
                try:
                    created = wintypes.FILETIME()
                    exited = wintypes.FILETIME()
                    kernel = wintypes.FILETIME()
                    user = wintypes.FILETIME()
                    if kernel32.GetProcessTimes(
                        handle,
                        ctypes.byref(created),
                        ctypes.byref(exited),
                        ctypes.byref(kernel),
                        ctypes.byref(user),
                    ):
                        exit_ticks = (
                            (exited.dwHighDateTime << 32)
                            | exited.dwLowDateTime
                        )
                        if exit_ticks:
                            return None
                        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
                        return f"windows:{ticks}"
                finally:
                    kernel32.CloseHandle(handle)
        except (AttributeError, OSError, ValueError):
            pass
    else:
        proc_stat = Path(f"/proc/{pid}/stat")
        if proc_stat.is_file():
            try:
                text = proc_stat.read_text(encoding="utf-8")
                closing_paren = text.rfind(")")
                fields = text[closing_paren + 2 :].split()
                if closing_paren >= 0 and len(fields) > 19:
                    if fields[0] == "Z":
                        return None
                    return f"proc:{fields[19]}"
            except (OSError, UnicodeError):
                pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return f"live:{pid}"
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return None
        return f"live:{pid}"
    return f"live:{pid}"


def _lock_handle(handle) -> None:
    if os.name == "nt":
        import msvcrt

        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for repository write lock: {handle.name}")
                time.sleep(_LOCK_POLL_SECONDS)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _replace_with_retry(source: Path, target: Path) -> None:
    deadline = time.monotonic() + _REPLACE_RETRY_SECONDS
    while True:
        try:
            source.replace(target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_REPLACE_RETRY_INTERVAL_SECONDS)


def _unlock_handle(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def delete_repository(path: Path) -> None:
    if path.exists():
        path.unlink()


def _regulation_version_sort_key(value: str | None) -> tuple[tuple[tuple[int, object], ...], str]:
    normalized = str(value or "").strip().casefold()
    tokens = tuple(
        (0, int(token)) if token.isdigit() else (1, token)
        for token in re.findall(r"\d+|[a-z]+", normalized)
    )
    return tokens, normalized
