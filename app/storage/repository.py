from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from copy import deepcopy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
from threading import Lock
import time
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

_FileIdentity = tuple[int, int, int, int]

_JOURNAL_ID_FIELDS: dict[str, tuple[str, ...]] = {
    "approvals": ("approval_record_id", "approval_id"),
    "review_decisions": ("review_id",),
    "indexing_jobs": ("indexing_job_id",),
    "rag_traces": ("trace_id",),
    "rag_feedback": ("feedback_id",),
    "security_scans": ("scan_id",),
    "maintenance_events": ("event_id",),
}


class JournalIntegrityError(RuntimeError):
    """Raised when an append-only repository journal is structurally ambiguous."""


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
        self._manifest_cache: dict | None = None
        self._manifest_identity: _FileIdentity | None = None
        self._legacy_cache: dict | None = None
        self._legacy_identity: _FileIdentity | None = None
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.manifest_path.exists():
            with _REPOSITORY_LOCK, self._repository_write_lock():
                if not self.manifest_path.exists():
                    self._write_json(self.manifest_path, self._empty_manifest())

    def upsert_document(self, document: Document) -> None:
        with _REPOSITORY_LOCK, self._repository_write_lock():
            data = self._read_manifest_for_update()
            data.setdefault("documents", {})
            data["documents"][document.document_id] = document.model_dump(mode="json")
            self._write_json(self.manifest_path, data)

    def get_document(self, document_id: str) -> Document | None:
        raw = self._read_manifest()["documents"].get(document_id)
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
            for result_type in ("nodes", "chunks", "issues", "quality"):
                path = self._result_path(document_id, result_type)
                if path.exists():
                    path.unlink()
                    removed = True
        return removed

    def list_documents(self) -> list[Document]:
        docs = dict(self._read_legacy().get("documents", {}))
        docs.update(self._read_manifest()["documents"])
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

    def upsert_job(self, job: ProcessingJob) -> None:
        with _REPOSITORY_LOCK, self._repository_write_lock():
            data = self._read_manifest_for_update()
            data.setdefault("jobs", {})
            data["jobs"][job.job_id] = job.model_dump(mode="json")
            self._write_json(self.manifest_path, data)

    def get_job(self, job_id: str) -> ProcessingJob | None:
        raw = self._read_manifest()["jobs"].get(job_id)
        if raw is None:
            raw = self._read_legacy().get("jobs", {}).get(job_id)
        return ProcessingJob.model_validate(raw) if raw else None

    def save_processing_result(
        self,
        document_id: str,
        nodes: list[StructureNode],
        chunks: list[Chunk],
        issues: list[ValidationIssue],
        *,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> None:
        with _REPOSITORY_LOCK, self._repository_write_lock():
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
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> None:
        with _REPOSITORY_LOCK, self._repository_write_lock():
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
        record_key = str(record.get("approval_record_id") or approval_id).strip()
        with _REPOSITORY_LOCK, self._repository_write_lock():
            append_required = self._require_journal_append_compatible("approvals", record)
            data = self._read_manifest_for_update()
            data.setdefault("approvals", {})
            data["approvals"][record_key] = record
            self._write_json(self.manifest_path, data)
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
            data = self._read_manifest_for_update()
            data.setdefault("review_decisions", {})
            data["review_decisions"][review_id] = record
            self._write_json(self.manifest_path, data)
            if append_required:
                self._append_journal_record("review_decisions", record, identity_validated=True)

    def list_review_records(self, document_id: str | None = None) -> list[dict]:
        records = self._list_records_with_journal("review_decisions", "review_decisions", ("review_id",))
        if document_id:
            records = [record for record in records if record.get("document_id") == document_id]
        return sorted(records, key=lambda record: str(record.get("reviewed_at") or ""))

    def append_indexing_job(self, record: dict) -> None:
        job_id = str(record.get("indexing_job_id") or "").strip()
        if not job_id:
            raise ValueError("indexing_job_id is required.")
        with _REPOSITORY_LOCK, self._repository_write_lock():
            append_required = self._require_journal_append_compatible("indexing_jobs", record)
            data = self._read_manifest_for_update()
            data.setdefault("indexing_jobs", {})
            data["indexing_jobs"][job_id] = record
            self._write_json(self.manifest_path, data)
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
            data = self._read_manifest_for_update()
            data.setdefault("rag_feedback", {})
            data["rag_feedback"][feedback_id] = record
            self._write_json(self.manifest_path, data)
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
            data = self._read_manifest_for_update()
            data.setdefault("security_scans", {})
            data["security_scans"][scan_id] = record
            self._write_json(self.manifest_path, data)
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

    def get_issues(self, document_id: str) -> list[ValidationIssue]:
        return [ValidationIssue.model_validate(raw) for raw in self._read_result(document_id, "issues")]

    def save_quality_report(self, document_id: str, report: QualityReport) -> None:
        with _REPOSITORY_LOCK, self._repository_write_lock():
            self._write_json(self._result_path(document_id, "quality"), report.model_dump(mode="json"))

    def get_quality_report(self, document_id: str) -> QualityReport | None:
        raw = self._read_result(document_id, "quality")
        return QualityReport.model_validate(raw) if raw else None

    def upsert_run(self, run: ProcessingRun) -> None:
        with _REPOSITORY_LOCK, self._repository_write_lock():
            data = self._read_manifest_for_update()
            data.setdefault("runs", {})
            data["runs"][run.run_id] = run.model_dump(mode="json")
            self._write_json(self.manifest_path, data)

    def get_run(self, run_id: str) -> ProcessingRun | None:
        raw = self._read_manifest().get("runs", {}).get(run_id)
        return ProcessingRun.model_validate(raw) if raw else None

    def list_runs(self, document_id: str | None = None) -> list[ProcessingRun]:
        runs = [ProcessingRun.model_validate(raw) for raw in self._read_manifest().get("runs", {}).values()]
        if document_id:
            runs = [run for run in runs if run.document_id == document_id]
        return sorted(runs, key=lambda run: run.started_at)

    def latest_completed_run(
        self,
        document_id: str,
        *,
        options: dict | None = None,
        require_outputs: bool = False,
    ) -> ProcessingRun | None:
        runs = [run for run in self.list_runs(document_id) if run.status == "completed"]
        if options is not None:
            expected = self._canonical_json(options)
            runs = [run for run in runs if self._canonical_json(run.options) == expected]
        if require_outputs:
            runs = [run for run in runs if self.has_reusable_outputs(document_id, run)]
        return runs[-1] if runs else None

    def has_reusable_outputs(self, document_id: str, run: ProcessingRun) -> bool:
        if run.status != "completed" or run.document_id != document_id:
            return False
        if not self._run_outputs_still_match_document_results(document_id, run):
            return False
        return self._stored_results_are_reusable(document_id) and self._run_artifacts_are_reusable(run)

    def find_reusable_run(
        self,
        *,
        file_hash: str,
        options: dict,
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
        candidates = self._filter_documents_by_provenance(
            candidates,
            document_name=document_name,
            institution_name=institution_name,
            source_url=source_url,
            source_disclosure_date=source_disclosure_date,
            source_posted_date=source_posted_date,
        )
        for document in reversed(candidates):
            run = self.latest_completed_run(document.document_id, options=options, require_outputs=True)
            if run is not None:
                return document, run
        return None

    def _run_outputs_still_match_document_results(self, document_id: str, run: ProcessingRun) -> bool:
        """Document-level result files are overwritten by later completed runs."""

        expected_options = self._canonical_json(run.options)
        completed_runs = [candidate for candidate in self.list_runs(document_id) if candidate.status == "completed"]
        seen_target = False
        for candidate in completed_runs:
            if candidate.run_id == run.run_id:
                seen_target = True
                continue
            if not seen_target:
                continue
            if self._canonical_json(candidate.options) != expected_options:
                return False
        return seen_target

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
        return data

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

    def _read_result(self, document_id: str, result_type: str) -> list | dict:
        path = self._result_path(document_id, result_type)
        if path.exists():
            if result_type in {"nodes", "chunks", "issues"}:
                return list(self._iter_json_array(path))
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        legacy = self._read_legacy()
        return legacy.get(result_type, {}).get(document_id, [])

    def _journal_path(self, journal_name: str) -> Path:
        return self.root / "journals" / f"{journal_name}.jsonl"

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
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _read_journal_records(self, journal_name: str) -> list[dict]:
        path = self._journal_path(journal_name)
        if not path.is_file():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise JournalIntegrityError(f"Journal '{journal_name}' could not be read as UTF-8 JSONL.") from exc
        records: list[dict] = []
        records_by_id: dict[str, dict] = {}
        id_fields = _JOURNAL_ID_FIELDS.get(journal_name, ())
        for line_number, line in enumerate(lines, start=1):
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
        return records

    def _require_journal_append_compatible(self, journal_name: str, record: dict) -> bool:
        id_fields = _JOURNAL_ID_FIELDS.get(journal_name, ())
        if not id_fields:
            return True
        record_id = self._record_identity(record, id_fields)
        if not record_id:
            raise ValueError(f"Journal '{journal_name}' record identity is required.")
        for existing in self._read_journal_records(journal_name):
            if self._record_identity(existing, id_fields) != record_id:
                continue
            if existing != record:
                raise JournalIntegrityError(
                    f"Journal '{journal_name}' already contains a conflicting record for identity "
                    f"'{record_id[:128]}'."
                )
            return False
        return True

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
        progress_interval = max(1, total // 100) if total else 1
        try:
            with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write("[")
                written = 0
                for written, record in enumerate(records, start=1):
                    if written > 1:
                        handle.write(",")
                    for piece in encoder.iterencode(record):
                        handle.write(piece)
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

            def fill() -> bool:
                nonlocal buffer
                part = handle.read(read_size)
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

                while True:
                    try:
                        value, end = decoder.raw_decode(buffer)
                        break
                    except json.JSONDecodeError:
                        if not fill():
                            raise
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
            except PermissionError:
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
