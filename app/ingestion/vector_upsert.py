from __future__ import annotations

import errno
import json
import math
import os
import shutil
import stat
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol
from uuid import uuid4

from app.ingestion.vector_adapter import (
    ALLOWED_SECURITY_LEVELS,
    APPROVED_CHUNK_STATUS,
    SUPPORTED_VECTOR_RECORD_VERIFICATION_VERSIONS,
    VECTOR_METADATA_SEMANTIC_FINGERPRINT_VERSION,
    VECTOR_RECORD_SCHEMA_VERSION,
    VECTOR_RECORD_SEMANTIC_FINGERPRINT_VERSION,
    VECTOR_RECORD_VERIFICATION_VERSION,
    approval_provenance_issue_fields,
    vector_metadata_semantic_fingerprint,
    vector_record_semantic_fingerprint,
    vector_record_verification_hash,
    vector_record_path_leaks,
    with_vector_record_verification,
)
from app.ingestion.embedding_adapter import EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION
from app.ingestion.embedding_adapter import MAX_EMBEDDING_DIMENSIONS
from app.ingestion.vector_integrity import embedded_vector_integrity_reason
from app.core.tenant_access import tenant_directory_key
from app.retrieval.bm25_index import (
    BM25_STRUCTURED_METADATA_VERSION,
    default_bm25_index_path,
    load_bm25_index,
    update_bm25_index_for_documents,
)
from app.retrieval.tokenizer import tokenizer_name


SUPPORTED_UPSERT_SCHEMA_VERSIONS = {VECTOR_RECORD_SCHEMA_VERSION, EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION}
SUPPORTED_QDRANT_TARGET_TYPES = {"qdrant-local-jsonl"}
_LOCAL_TARGET_FILENAMES = {
    "local-jsonl": "approved_vectors.jsonl",
    "qdrant-local-jsonl": "approved_qdrant_points.jsonl",
    "pgvector-local-jsonl": "approved_pgvector_rows.jsonl",
    "chroma-local-jsonl": "approved_chroma_rows.jsonl",
}
_LOCAL_INDEX_LOCK_TIMEOUT_SECONDS = 60.0
_LOCAL_INDEX_THREAD_LOCKS_GUARD = threading.Lock()
_LOCAL_INDEX_THREAD_LOCKS: dict[str, threading.RLock] = {}


def _serialized_local_index(method: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with _local_index_write_lock(self.path):
            return method(self, *args, **kwargs)

    return wrapped


@contextmanager
def _local_index_write_lock(
    vector_path: str | Path,
    *,
    timeout_seconds: float = _LOCAL_INDEX_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Serialize one local vector/BM25 pair across threads and processes."""

    target_path, _bm25_path, lock_path = _confined_local_index_paths(vector_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    lock_key = str(lock_path.resolve(strict=False))
    with _LOCAL_INDEX_THREAD_LOCKS_GUARD:
        thread_lock = _LOCAL_INDEX_THREAD_LOCKS.setdefault(lock_key, threading.RLock())

    with thread_lock:
        fd = _open_confined_lock_file(lock_path)
        acquired = False
        try:
            _acquire_platform_file_lock(fd, lock_path, timeout_seconds=timeout_seconds)
            acquired = True
            yield
        finally:
            try:
                if acquired:
                    _release_platform_file_lock(fd)
            finally:
                os.close(fd)


def _confined_local_index_paths(vector_path: str | Path) -> tuple[Path, Path, Path]:
    target_path = Path(vector_path)
    parent = target_path.parent.resolve(strict=False)
    resolved_target = target_path.resolve(strict=False)
    if resolved_target.parent != parent:
        raise ValueError(f"Vector target must remain within its declared parent: {target_path}")
    if target_path.is_symlink():
        raise ValueError(f"Vector target must not be a symbolic link: {target_path}")

    bm25_path = default_bm25_index_path(target_path)
    resolved_bm25 = bm25_path.resolve(strict=False)
    if resolved_bm25.parent != parent:
        raise ValueError(f"BM25 target must remain beside the vector target: {bm25_path}")
    if bm25_path.is_symlink():
        raise ValueError(f"BM25 target must not be a symbolic link: {bm25_path}")

    # BM25 is a directory-level sidecar, so every vector file sharing that
    # sidecar must also share one lock.
    lock_path = target_path.parent / f".{bm25_path.name}.reg-rag.lock"
    resolved_lock = lock_path.resolve(strict=False)
    if resolved_lock.parent != parent:
        raise ValueError(f"Vector lock must remain beside the vector target: {lock_path}")
    if lock_path.is_symlink():
        raise ValueError(f"Vector lock must not be a symbolic link: {lock_path}")
    return target_path, bm25_path, lock_path


def _open_confined_lock_file(lock_path: Path) -> int:
    flags = os.O_CREAT | os.O_RDWR
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if no_follow:
        flags |= no_follow
    fd = os.open(lock_path, flags, 0o600)
    file_stat = os.fstat(fd)
    if not stat.S_ISREG(file_stat.st_mode):
        os.close(fd)
        raise ValueError(f"Vector lock must be a regular file: {lock_path}")
    path_stat = os.stat(lock_path, follow_symlinks=False)
    if (file_stat.st_dev, file_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
        os.close(fd)
        raise ValueError(f"Vector lock changed while it was being opened: {lock_path}")
    if file_stat.st_size == 0:
        os.write(fd, b"\0")
        os.fsync(fd)
    return fd


def _acquire_platform_file_lock(fd: int, lock_path: Path, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            retryable = os.name == "nt" or exc.errno in {errno.EACCES, errno.EAGAIN}
            if not retryable:
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for vector index lock: {lock_path}") from exc
            time.sleep(0.05)


def _release_platform_file_lock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class VectorUpsertTarget(Protocol):
    target_type: str

    def upsert(
        self,
        records: list[dict[str, Any]],
        *,
        dry_run: bool = False,
        fail_on_leak: bool = True,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class LocalJsonlVectorTarget:
    path: Path
    target_type: str = "local-jsonl"

    @_serialized_local_index
    def upsert(
        self,
        records: list[dict[str, Any]],
        *,
        dry_run: bool = False,
        fail_on_leak: bool = True,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        validated = _with_upsert_verification(validate_vector_records(records))
        leaks = vector_record_path_leaks(validated)
        if fail_on_leak and leaks:
            raise ValueError(f"Vector upsert records contain local path leaks: {len(leaks)}")
        existing = _read_existing_records(self.path)
        existing_by_id = {record["id"]: record for record in existing}
        removed = _remove_inactive_document_items(
            existing_by_id,
            document_id=document_id,
            active_ids={record["id"] for record in validated},
            document_id_getter=_local_record_document_id,
        )
        inserted = 0
        updated = 0
        unchanged = 0
        embedding_reused = 0
        for record in validated:
            previous = existing_by_id.get(record["id"])
            if previous is None:
                inserted += 1
            elif _same_vector_semantics(previous, record):
                unchanged += 1
                # Preserve the already verified row so a no-op upsert does not
                # differ only by a freshly generated verified_at timestamp.
                record = previous
            else:
                updated += 1
                record, reused = _preserve_safe_embedding(previous, record)
                embedding_reused += int(reused)
            existing_by_id[record["id"]] = record
        has_changes = bool(inserted or updated or removed)
        vector_write_required = has_changes or not self.path.is_file()
        bm25_path = default_bm25_index_path(self.path)
        final_records = [existing_by_id[key] for key in sorted(existing_by_id)]
        bm25_write_required = not _bm25_index_is_reusable(bm25_path, final_records)
        if not dry_run:
            bm25_index, bm25_incremental = _write_local_index_transaction(
                vector_path=self.path,
                bm25_path=bm25_path,
                previous_records=existing,
                final_records=final_records,
                vector_write_required=vector_write_required,
                bm25_write_required=bm25_write_required,
                changed_document_ids=(
                    _bm25_changed_document_ids(existing, final_records)
                    if has_changes and document_id
                    else []
                ),
            )
        else:
            bm25_index = None
            bm25_incremental = False
        return {
            "target_type": self.target_type,
            "target_path": str(self.path),
            "bm25_index_path": str(bm25_path),
            "bm25_index_written": bm25_index is not None,
            "bm25_update_mode": (
                "skipped_unchanged"
                if not dry_run and not bm25_write_required
                else ("incremental" if bm25_incremental else "full")
            ),
            "full_store_write_count": 1 if not dry_run and vector_write_required else 0,
            "dry_run": dry_run,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "verification_version": VECTOR_RECORD_VERIFICATION_VERSION,
            "verification_record_count": len(validated),
            "schema_versions": sorted({str(record.get("schema_version") or "") for record in validated}),
            "input_record_count": len(validated),
            "existing_record_count": len(existing),
            "final_record_count": len(existing_by_id),
            "inserted_count": inserted,
            "updated_count": updated,
            "unchanged_count": unchanged,
            "embedding_reused_count": embedding_reused,
            "removed_count": removed,
            "local_path_leak_count": len(leaks),
            "local_path_leak_samples": leaks[:20],
        }

    @_serialized_local_index
    def upsert_documents(
        self,
        records_by_document: dict[str, list[dict[str, Any]]],
        *,
        dry_run: bool = False,
        fail_on_leak: bool = True,
    ) -> dict[str, Any]:
        normalized: dict[str, list[dict[str, Any]]] = {}
        all_validated: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for raw_document_id, records in sorted(records_by_document.items()):
            document_id = str(raw_document_id or "").strip()
            if not document_id:
                raise ValueError("Batch vector upsert requires a non-empty document_id.")
            validated = _with_upsert_verification(validate_vector_records(records))
            for record in validated:
                record_id = str(record.get("id") or "")
                record_document_id = _local_record_document_id(record)
                if record_document_id != document_id:
                    raise ValueError(
                        "Batch vector upsert record document_id does not match its document group: "
                        f"{record_id or '<missing>'}."
                    )
                if record_id in seen_ids:
                    raise ValueError(f"Batch vector upsert contains duplicate record id: {record_id}.")
                seen_ids.add(record_id)
            normalized[document_id] = validated
            all_validated.extend(validated)

        leaks = vector_record_path_leaks(all_validated)
        if fail_on_leak and leaks:
            raise ValueError(f"Vector upsert records contain local path leaks: {len(leaks)}")

        existing = _read_existing_records(self.path)
        existing_by_id = {record["id"]: record for record in existing}
        document_summaries: dict[str, dict[str, int]] = {}
        inserted = 0
        updated = 0
        unchanged = 0
        embedding_reused = 0
        removed = 0
        for document_id, validated in normalized.items():
            active_ids = {str(record["id"]) for record in validated}
            document_removed = _remove_inactive_document_items(
                existing_by_id,
                document_id=document_id,
                active_ids=active_ids,
                document_id_getter=_local_record_document_id,
            )
            document_inserted = 0
            document_updated = 0
            document_unchanged = 0
            document_embedding_reused = 0
            for record in validated:
                previous = existing_by_id.get(record["id"])
                if previous is None:
                    document_inserted += 1
                elif _same_vector_semantics(previous, record):
                    document_unchanged += 1
                    record = previous
                else:
                    document_updated += 1
                    record, reused = _preserve_safe_embedding(previous, record)
                    document_embedding_reused += int(reused)
                existing_by_id[record["id"]] = record
            document_summaries[document_id] = {
                "input_record_count": len(validated),
                "inserted_count": document_inserted,
                "updated_count": document_updated,
                "unchanged_count": document_unchanged,
                "embedding_reused_count": document_embedding_reused,
                "removed_count": document_removed,
            }
            inserted += document_inserted
            updated += document_updated
            unchanged += document_unchanged
            embedding_reused += document_embedding_reused
            removed += document_removed

        has_changes = bool(inserted or updated or removed)
        vector_write_required = has_changes or not self.path.is_file()
        bm25_path = default_bm25_index_path(self.path)
        final_records = [existing_by_id[key] for key in sorted(existing_by_id)]
        bm25_write_required = not _bm25_index_is_reusable(bm25_path, final_records)
        if not dry_run:
            bm25_index, bm25_incremental = _write_local_index_transaction(
                vector_path=self.path,
                bm25_path=bm25_path,
                previous_records=existing,
                final_records=final_records,
                vector_write_required=vector_write_required,
                bm25_write_required=bm25_write_required,
                changed_document_ids=_bm25_changed_document_ids(existing, final_records),
            )
        else:
            bm25_index = None
            bm25_incremental = False
        return {
            "target_type": self.target_type,
            "target_path": str(self.path),
            "bm25_index_path": str(bm25_path),
            "bm25_index_written": bm25_index is not None,
            "bm25_update_mode": (
                "skipped_unchanged"
                if not dry_run and not bm25_write_required
                else ("incremental" if bm25_incremental else "full")
            ),
            "dry_run": dry_run,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "verification_version": VECTOR_RECORD_VERIFICATION_VERSION,
            "verification_record_count": len(all_validated),
            "schema_versions": sorted({str(record.get("schema_version") or "") for record in all_validated}),
            "batch_document_count": len(normalized),
            "full_store_write_count": 1 if not dry_run and vector_write_required else 0,
            "input_record_count": len(all_validated),
            "existing_record_count": len(existing),
            "final_record_count": len(existing_by_id),
            "inserted_count": inserted,
            "updated_count": updated,
            "unchanged_count": unchanged,
            "embedding_reused_count": embedding_reused,
            "removed_count": removed,
            "document_summaries": document_summaries,
            "local_path_leak_count": len(leaks),
            "local_path_leak_samples": leaks[:20],
        }


@dataclass(frozen=True)
class QdrantLocalJsonlTarget:
    path: Path
    target_type: str = "qdrant-local-jsonl"

    def upsert(
        self,
        records: list[dict[str, Any]],
        *,
        dry_run: bool = False,
        fail_on_leak: bool = True,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        validated = _with_upsert_verification(validate_embedded_vector_records(records))
        leaks = vector_record_path_leaks(validated)
        if fail_on_leak and leaks:
            raise ValueError(f"Vector upsert records contain local path leaks: {len(leaks)}")
        existing_points = _read_existing_qdrant_points(self.path)
        existing_by_id = {str(point["id"]): point for point in existing_points}
        removed = _remove_inactive_document_items(
            existing_by_id,
            document_id=document_id,
            active_ids={str(record["id"]) for record in validated},
            document_id_getter=_qdrant_point_document_id,
        )
        inserted = 0
        updated = 0
        unchanged = 0
        for record in validated:
            point = qdrant_point_from_record(record)
            previous = existing_by_id.get(point["id"])
            if previous is None:
                inserted += 1
            elif previous.get("payload", {}).get("content_hash") == point.get("payload", {}).get("content_hash"):
                unchanged += 1
            else:
                updated += 1
            existing_by_id[point["id"]] = point
        if not dry_run:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _write_jsonl_atomic(self.path, [existing_by_id[key] for key in sorted(existing_by_id)])
        return {
            "target_type": self.target_type,
            "target_path": str(self.path),
            "dry_run": dry_run,
            "mode": "local_export_only",
            "api_call_count": 0,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "verification_version": VECTOR_RECORD_VERIFICATION_VERSION,
            "verification_record_count": len(validated),
            "schema_versions": sorted({str(record.get("schema_version") or "") for record in validated}),
            "embedding_models": sorted({str(record.get("embedding_model") or "") for record in validated}),
            "input_record_count": len(validated),
            "existing_record_count": len(existing_points),
            "final_record_count": len(existing_by_id),
            "inserted_count": inserted,
            "updated_count": updated,
            "unchanged_count": unchanged,
            "removed_count": removed,
            "local_path_leak_count": len(leaks),
            "local_path_leak_samples": leaks[:20],
        }


@dataclass(frozen=True)
class ChromaLocalJsonlTarget:
    path: Path
    target_type: str = "chroma-local-jsonl"

    def upsert(
        self,
        records: list[dict[str, Any]],
        *,
        dry_run: bool = False,
        fail_on_leak: bool = True,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        validated = _with_upsert_verification(validate_embedded_vector_records(records))
        leaks = vector_record_path_leaks(validated)
        if fail_on_leak and leaks:
            raise ValueError(f"Vector upsert records contain local path leaks: {len(leaks)}")
        existing_rows = _read_existing_chroma_rows(self.path)
        existing_by_id = {str(row["id"]): row for row in existing_rows}
        removed = _remove_inactive_document_items(
            existing_by_id,
            document_id=document_id,
            active_ids={str(record["id"]) for record in validated},
            document_id_getter=_metadata_row_document_id,
        )
        inserted = 0
        updated = 0
        unchanged = 0
        for record in validated:
            row = chroma_row_from_record(record)
            previous = existing_by_id.get(row["id"])
            if previous is None:
                inserted += 1
            elif previous.get("metadata", {}).get("content_hash") == row.get("metadata", {}).get("content_hash"):
                unchanged += 1
            else:
                updated += 1
            existing_by_id[row["id"]] = row
        if not dry_run:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _write_jsonl_atomic(self.path, [existing_by_id[key] for key in sorted(existing_by_id)])
        return {
            "target_type": self.target_type,
            "target_path": str(self.path),
            "dry_run": dry_run,
            "mode": "local_export_only",
            "api_call_count": 0,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "verification_version": VECTOR_RECORD_VERIFICATION_VERSION,
            "verification_record_count": len(validated),
            "schema_versions": sorted({str(record.get("schema_version") or "") for record in validated}),
            "embedding_models": sorted({str(record.get("embedding_model") or "") for record in validated}),
            "input_record_count": len(validated),
            "existing_record_count": len(existing_rows),
            "final_record_count": len(existing_by_id),
            "inserted_count": inserted,
            "updated_count": updated,
            "unchanged_count": unchanged,
            "removed_count": removed,
            "local_path_leak_count": len(leaks),
            "local_path_leak_samples": leaks[:20],
        }


@dataclass(frozen=True)
class PgvectorLocalJsonlTarget:
    path: Path
    target_type: str = "pgvector-local-jsonl"

    def upsert(
        self,
        records: list[dict[str, Any]],
        *,
        dry_run: bool = False,
        fail_on_leak: bool = True,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        validated = _with_upsert_verification(validate_embedded_vector_records(records))
        leaks = vector_record_path_leaks(validated)
        if fail_on_leak and leaks:
            raise ValueError(f"Vector upsert records contain local path leaks: {len(leaks)}")
        existing_rows = _read_existing_pgvector_rows(self.path)
        existing_by_id = {str(row["id"]): row for row in existing_rows}
        removed = _remove_inactive_document_items(
            existing_by_id,
            document_id=document_id,
            active_ids={str(record["id"]) for record in validated},
            document_id_getter=_metadata_row_document_id,
        )
        inserted = 0
        updated = 0
        unchanged = 0
        for record in validated:
            row = pgvector_row_from_record(record)
            previous = existing_by_id.get(row["id"])
            if previous is None:
                inserted += 1
            elif previous.get("content_hash") == row.get("content_hash"):
                unchanged += 1
            else:
                updated += 1
            existing_by_id[row["id"]] = row
        if not dry_run:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _write_jsonl_atomic(self.path, [existing_by_id[key] for key in sorted(existing_by_id)])
        return {
            "target_type": self.target_type,
            "target_path": str(self.path),
            "dry_run": dry_run,
            "mode": "local_export_only",
            "api_call_count": 0,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "verification_version": VECTOR_RECORD_VERIFICATION_VERSION,
            "verification_record_count": len(validated),
            "schema_versions": sorted({str(record.get("schema_version") or "") for record in validated}),
            "embedding_models": sorted({str(record.get("embedding_model") or "") for record in validated}),
            "input_record_count": len(validated),
            "existing_record_count": len(existing_rows),
            "final_record_count": len(existing_by_id),
            "inserted_count": inserted,
            "updated_count": updated,
            "unchanged_count": unchanged,
            "removed_count": removed,
            "local_path_leak_count": len(leaks),
            "local_path_leak_samples": leaks[:20],
        }


@dataclass(frozen=True)
class QdrantRestManifestTarget:
    path: Path
    target_type: str = "qdrant-rest-manifest"
    collection_name: str = "reg-rag-collection"

    def upsert(
        self,
        records: list[dict[str, Any]],
        *,
        dry_run: bool = False,
        fail_on_leak: bool = True,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        validated = _with_upsert_verification(validate_embedded_vector_records(records))
        leaks = vector_record_path_leaks(validated)
        if fail_on_leak and leaks:
            raise ValueError(f"Vector upsert records contain local path leaks: {len(leaks)}")
        points = [qdrant_point_from_record(record) for record in validated]
        dimensions = sorted({len(point.get("vector") or []) for point in points})
        manifest_body = {
            "target_type": self.target_type,
            "collection_name": self.collection_name,
            "mode": "manifest_only",
            "live_network_blocked": True,
            "api_call_count": 0,
            "dry_run": dry_run,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "verification_version": VECTOR_RECORD_VERIFICATION_VERSION,
            "verification_record_count": len(validated),
            "schema_versions": sorted({str(record.get("schema_version") or "") for record in validated}),
            "embedding_models": sorted({str(record.get("embedding_model") or "") for record in validated}),
            "embedding_dimensions": dimensions,
            "input_record_count": len(validated),
            "planned_upsert_count": len(points),
            "removed_count": 0,
            "local_path_leak_count": len(leaks),
            "local_path_leak_samples": leaks[:20],
            "approval_required_fields": [
                "budget_reference",
                "approval_reference",
                "audit_log_id",
            ],
            "sample_point_ids": [str(point.get("id") or "") for point in points[:5]],
        }
        if not dry_run:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(manifest_body, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            **manifest_body,
            "target_path": str(self.path),
        }


def vector_upsert_target(
    target_type: str,
    *,
    target_path: str | Path | None = None,
    collection_name: str | None = None,
) -> VectorUpsertTarget:
    normalized = str(target_type or "").strip().lower()
    if normalized == "local-jsonl":
        if not target_path:
            raise ValueError("local-jsonl target requires target_path.")
        return LocalJsonlVectorTarget(Path(target_path))
    if normalized == "qdrant-local-jsonl":
        if not target_path:
            raise ValueError("qdrant-local-jsonl target requires target_path.")
        return QdrantLocalJsonlTarget(Path(target_path))
    if normalized == "pgvector-local-jsonl":
        if not target_path:
            raise ValueError("pgvector-local-jsonl target requires target_path.")
        return PgvectorLocalJsonlTarget(Path(target_path))
    if normalized == "chroma-local-jsonl":
        if not target_path:
            raise ValueError("chroma-local-jsonl target requires target_path.")
        return ChromaLocalJsonlTarget(Path(target_path))
    if normalized == "qdrant-rest-manifest":
        if not target_path:
            raise ValueError("qdrant-rest-manifest target requires target_path.")
        return QdrantRestManifestTarget(Path(target_path), collection_name=collection_name or "reg-rag-collection")
    if normalized == "qdrant-rest":
        raise ValueError(
            "qdrant-rest live network upsert is blocked by default. "
            "Use qdrant-rest-manifest for manifest-only planning or qdrant-local-jsonl for offline export."
        )
    raise ValueError(
        "Unsupported vector upsert target_type. Supported: local-jsonl, qdrant-local-jsonl, "
        "pgvector-local-jsonl, chroma-local-jsonl, qdrant-rest-manifest."
    )


def load_vector_records_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid vector JSONL at {path}:{line_no}: {exc}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"Invalid vector JSONL at {path}:{line_no}: expected object")
        records.append(record)
    return validate_vector_records(records)


def validate_vector_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates: list[str] = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"Vector record {index} must be an object.")
        schema_version = record.get("schema_version")
        if schema_version not in SUPPORTED_UPSERT_SCHEMA_VERSIONS:
            raise ValueError(f"Vector record {index} has unsupported schema_version: {record.get('schema_version')}")
        record_id = str(record.get("id") or "").strip()
        if not record_id:
            raise ValueError(f"Vector record {index} is missing id.")
        if record_id in seen:
            duplicates.append(record_id)
        seen.add(record_id)
        if not str(record.get("text") or "").strip():
            raise ValueError(f"Vector record {record_id} is missing text.")
        metadata = record.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError(f"Vector record {record_id} metadata must be an object.")
        metadata = metadata or {}
        if metadata.get("approval_status") != APPROVED_CHUNK_STATUS or not metadata.get("approval_id"):
            raise ValueError(f"Vector record {record_id} is not approved for indexing.")
        if not str(metadata.get("approved_content_hash") or "").strip():
            raise ValueError(f"Vector record {record_id} is missing approved_content_hash.")
        provenance_issues = approval_provenance_issue_fields(record)
        if provenance_issues:
            raise ValueError(
                f"Vector record {record_id} is missing or has invalid approval provenance: "
                f"{', '.join(provenance_issues)}."
            )
        if not str(metadata.get("tenant_id") or "").strip():
            raise ValueError(f"Vector record {record_id} is missing tenant_id.")
        record_tenant_id = str(record.get("tenant_id") or "").strip()
        metadata_tenant_id = str(metadata.get("tenant_id") or "").strip()
        if record_tenant_id and record_tenant_id != metadata_tenant_id:
            raise ValueError(
                f"Vector record {record_id} has inconsistent tenant_id between record and metadata."
            )
        security_level = str(metadata.get("security_level") or "").strip().lower()
        if security_level not in ALLOWED_SECURITY_LEVELS:
            raise ValueError(f"Vector record {record_id} has invalid or missing security_level.")
        if not str(record.get("content_hash") or "").strip():
            raise ValueError(f"Vector record {record_id} is missing content_hash.")
        verification_version = str(record.get("verification_version") or "")
        verification_hash = str(record.get("verification_hash") or "")
        if (
            verification_version
            and verification_version not in SUPPORTED_VECTOR_RECORD_VERIFICATION_VERSIONS
        ):
            raise ValueError(f"Vector record {record_id} has unsupported verification_version: {verification_version}")
        if verification_version and not verification_hash:
            raise ValueError(f"Vector record {record_id} is missing verification_hash.")
        metadata_fingerprint_version = str(
            record.get("metadata_semantic_fingerprint_version") or ""
        )
        metadata_fingerprint = str(record.get("metadata_semantic_fingerprint") or "")
        if metadata_fingerprint and not metadata_fingerprint_version:
            raise ValueError(
                f"Vector record {record_id} is missing metadata_semantic_fingerprint_version."
            )
        if metadata_fingerprint_version and (
            metadata_fingerprint_version != VECTOR_METADATA_SEMANTIC_FINGERPRINT_VERSION
        ):
            raise ValueError(
                f"Vector record {record_id} has unsupported metadata_semantic_fingerprint_version: "
                f"{metadata_fingerprint_version}"
            )
        if metadata_fingerprint_version and not metadata_fingerprint:
            raise ValueError(
                f"Vector record {record_id} is missing metadata_semantic_fingerprint."
            )
        if metadata_fingerprint and (
            metadata_fingerprint != vector_metadata_semantic_fingerprint(metadata)
        ):
            raise ValueError(
                f"Vector record {record_id} has invalid metadata_semantic_fingerprint."
            )
        record_fingerprint_version = str(
            record.get("record_semantic_fingerprint_version") or ""
        )
        record_fingerprint = str(record.get("record_semantic_fingerprint") or "")
        if record_fingerprint and not record_fingerprint_version:
            raise ValueError(
                f"Vector record {record_id} is missing record_semantic_fingerprint_version."
            )
        if record_fingerprint_version and (
            record_fingerprint_version != VECTOR_RECORD_SEMANTIC_FINGERPRINT_VERSION
        ):
            raise ValueError(
                f"Vector record {record_id} has unsupported record_semantic_fingerprint_version: "
                f"{record_fingerprint_version}"
            )
        if record_fingerprint_version and not record_fingerprint:
            raise ValueError(
                f"Vector record {record_id} is missing record_semantic_fingerprint."
            )
        if record_fingerprint and (
            record_fingerprint != vector_record_semantic_fingerprint(record)
        ):
            raise ValueError(
                f"Vector record {record_id} has invalid record_semantic_fingerprint."
            )
        if verification_version == VECTOR_RECORD_VERIFICATION_VERSION and (
            not metadata_fingerprint_version
            or not metadata_fingerprint
            or not record_fingerprint_version
            or not record_fingerprint
        ):
            raise ValueError(
                f"Vector record {record_id} is missing semantic fingerprints required by "
                f"{VECTOR_RECORD_VERIFICATION_VERSION}."
            )
        if verification_hash and verification_hash != vector_record_verification_hash(record):
            raise ValueError(f"Vector record {record_id} has invalid verification_hash.")
        if schema_version == EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION:
            embedding = record.get("embedding")
            if not isinstance(embedding, list) or not embedding:
                raise ValueError(f"Embedded vector record {record_id} is missing embedding.")
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in embedding):
                raise ValueError(f"Embedded vector record {record_id} embedding must contain only numbers.")
            dimensions = record.get("embedding_dimensions")
            if (
                not isinstance(dimensions, int)
                or isinstance(dimensions, bool)
                or dimensions != len(embedding)
                or not 1 <= dimensions <= MAX_EMBEDDING_DIMENSIONS
            ):
                raise ValueError(
                    f"Embedded vector record {record_id} embedding_dimensions must match embedding length "
                    f"and be between 1 and {MAX_EMBEDDING_DIMENSIONS}."
                )
            if not str(record.get("embedding_model") or "").strip():
                raise ValueError(f"Embedded vector record {record_id} is missing embedding_model.")
            if not str(record.get("embedding_hash") or "").strip():
                raise ValueError(f"Embedded vector record {record_id} is missing embedding_hash.")
            integrity_reason = embedded_vector_integrity_reason(record)
            if integrity_reason:
                raise ValueError(f"Embedded vector record {record_id} failed integrity check: {integrity_reason}.")
        validated.append(record)
    if duplicates:
        sample = ", ".join(sorted(set(duplicates))[:20])
        raise ValueError(f"Vector upsert input has duplicate record ids: {sample}")
    return validated


def validate_vector_record_tenant_scope(
    records: Iterable[dict[str, Any]],
    *,
    expected_tenant_id: str | None = None,
) -> str:
    """Require vector input to represent one tenant and, when supplied, that tenant."""
    expected = str(expected_tenant_id or "").strip()
    observed: set[str] = set()
    for index, record in enumerate(records, start=1):
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        record_tenant_id = str(record.get("tenant_id") or "").strip()
        metadata_tenant_id = str(metadata.get("tenant_id") or "").strip()
        tenant_id = metadata_tenant_id or record_tenant_id
        if not tenant_id:
            raise ValueError(f"Vector record {index} is missing tenant_id.")
        if record_tenant_id and metadata_tenant_id and record_tenant_id != metadata_tenant_id:
            raise ValueError(f"Vector record {index} has inconsistent tenant_id fields.")
        observed.add(tenant_id)
        if expected and tenant_id != expected:
            raise ValueError(
                f"Vector record {index} tenant_id does not match expected tenant: {tenant_id!r} != {expected!r}."
            )
    if len(observed) > 1:
        raise ValueError(f"Vector upsert input contains multiple tenant scopes: {', '.join(sorted(observed))}")
    if expected:
        return expected
    return next(iter(observed), "")


def validate_vector_target_tenant_scope(
    target_type: str,
    target_path: str | Path,
    *,
    expected_tenant_id: str | None = None,
) -> None:
    """Reject existing target rows that would mix tenants during an upsert."""
    expected = str(expected_tenant_id or "").strip()
    if not expected or str(target_type or "").strip().lower() == "qdrant-rest-manifest":
        return
    path = Path(target_path)
    if not path.is_file():
        return
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid existing vector target JSON at {path}:{line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"Invalid existing vector target row at {path}:{line_no}: expected object")
        normalized_type = str(target_type or "").strip().lower()
        if normalized_type == "qdrant-local-jsonl":
            container = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        else:
            container = row.get("metadata") if isinstance(row.get("metadata"), dict) else row
        tenant_id = str(container.get("tenant_id") or row.get("tenant_id") or "").strip()
        if not tenant_id:
            raise ValueError(f"Existing vector target row at {path}:{line_no} is missing tenant_id.")
        if tenant_id != expected:
            raise ValueError(
                f"Existing vector target row at {path}:{line_no} belongs to tenant {tenant_id!r}, "
                f"not expected tenant {expected!r}."
            )


def canonical_vector_target_path(
    data_dir: str | Path,
    tenant_id: str,
    *,
    target_type: str,
    tenant_storage_isolation: bool,
) -> Path | None:
    """Return the official local target path for a tenant-scoped runtime."""
    filename = _LOCAL_TARGET_FILENAMES.get(str(target_type or "").strip().lower())
    if filename is None:
        return None
    base = Path(data_dir)
    if tenant_storage_isolation:
        base = base / "tenants" / tenant_directory_key(tenant_id)
    tenant_key = tenant_directory_key(tenant_id)
    return base / "vector_db" / tenant_key / filename


def _with_upsert_verification(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    verified_at = datetime.now(timezone.utc).isoformat()
    return [with_vector_record_verification(record, verified_at=verified_at) for record in records]


def validate_embedded_vector_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    validated = validate_vector_records(records)
    non_embedded = [
        str(record.get("id") or "")
        for record in validated
        if record.get("schema_version") != EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION
    ]
    if non_embedded:
        sample = ", ".join(non_embedded[:20])
        raise ValueError(
            "qdrant-local-jsonl requires embedded vector records. "
            f"Non-embedded record ids: {sample}"
        )
    return validated


def qdrant_point_from_record(record: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(record.get("metadata") or {})
    payload = {
        "content_hash": record.get("content_hash"),
        "text": record.get("text"),
        "document_id": record.get("document_id"),
        "chunk_id": record.get("chunk_id"),
        "embedding_model": record.get("embedding_model"),
        "embedding_hash": record.get("embedding_hash"),
        "metadata_semantic_fingerprint_version": record.get(
            "metadata_semantic_fingerprint_version"
        ),
        "metadata_semantic_fingerprint": record.get("metadata_semantic_fingerprint"),
        "record_semantic_fingerprint_version": record.get(
            "record_semantic_fingerprint_version"
        ),
        "record_semantic_fingerprint": record.get("record_semantic_fingerprint"),
        "verification_version": record.get("verification_version"),
        "verification_hash": record.get("verification_hash"),
        "verified_at": record.get("verified_at"),
        **metadata,
    }
    return {
        "id": str(record.get("id") or ""),
        "vector": list(record.get("embedding") or []),
        "payload": payload,
    }


def chroma_row_from_record(record: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(record.get("metadata") or {})
    payload = {
        "content_hash": record.get("content_hash"),
        "document_id": record.get("document_id"),
        "chunk_id": record.get("chunk_id"),
        "embedding_model": record.get("embedding_model"),
        "embedding_hash": record.get("embedding_hash"),
        "metadata_semantic_fingerprint_version": record.get(
            "metadata_semantic_fingerprint_version"
        ),
        "metadata_semantic_fingerprint": record.get("metadata_semantic_fingerprint"),
        "record_semantic_fingerprint_version": record.get(
            "record_semantic_fingerprint_version"
        ),
        "record_semantic_fingerprint": record.get("record_semantic_fingerprint"),
        "verification_version": record.get("verification_version"),
        "verification_hash": record.get("verification_hash"),
        "verified_at": record.get("verified_at"),
        **metadata,
    }
    return {
        "id": str(record.get("id") or ""),
        "document": record.get("text"),
        "embedding": list(record.get("embedding") or []),
        "metadata": payload,
    }


def pgvector_row_from_record(record: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(record.get("metadata") or {})
    payload = {
        "content_hash": record.get("content_hash"),
        "document_id": record.get("document_id"),
        "chunk_id": record.get("chunk_id"),
        "embedding_model": record.get("embedding_model"),
        "embedding_hash": record.get("embedding_hash"),
        "metadata_semantic_fingerprint_version": record.get(
            "metadata_semantic_fingerprint_version"
        ),
        "metadata_semantic_fingerprint": record.get("metadata_semantic_fingerprint"),
        "record_semantic_fingerprint_version": record.get(
            "record_semantic_fingerprint_version"
        ),
        "record_semantic_fingerprint": record.get("record_semantic_fingerprint"),
        "verification_version": record.get("verification_version"),
        "verification_hash": record.get("verification_hash"),
        "verified_at": record.get("verified_at"),
        **metadata,
    }
    return {
        "id": str(record.get("id") or ""),
        "content": record.get("text"),
        "embedding": list(record.get("embedding") or []),
        "embedding_dimensions": record.get("embedding_dimensions"),
        "content_hash": record.get("content_hash"),
        "metadata": payload,
    }


def _remove_inactive_document_items(
    existing_by_id: dict[str, dict[str, Any]],
    *,
    document_id: str | None,
    active_ids: set[str],
    document_id_getter,
) -> int:
    if not document_id:
        return 0
    removed = 0
    for item_id, item in list(existing_by_id.items()):
        if document_id_getter(item) == document_id and item_id not in active_ids:
            del existing_by_id[item_id]
            removed += 1
    return removed


def _preserve_safe_embedding(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Reuse the exact prior vector when only non-embedding semantics changed."""

    if (
        previous.get("schema_version") != EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION
        or current.get("schema_version") != EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION
        or str(previous.get("text") or "") != str(current.get("text") or "")
    ):
        return current, False
    embedding_fields = {
        "embedding_model",
        "embedding_dimensions",
        "embedding_hash",
    }
    if any(previous.get(field) != current.get(field) for field in embedding_fields):
        return current, False
    if embedded_vector_integrity_reason(previous):
        return current, False
    preserved = dict(current)
    preserved["embedding"] = list(previous.get("embedding") or [])
    return preserved, True


def _same_vector_semantics(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """Compare fields that determine whether a persisted vector row is reusable."""

    semantic_fields = {
        "metadata_semantic_fingerprint_version",
        "metadata_semantic_fingerprint",
        "record_semantic_fingerprint_version",
        "record_semantic_fingerprint",
        "schema_version",
        "source_schema_version",
    }
    semantic_fields.update(
        key
        for record in (previous, current)
        for key in record
        if key.startswith(("embedding_", "vector_"))
        or key in {"distance_metric", "similarity_metric"}
    )
    return all(previous.get(field) == current.get(field) for field in semantic_fields)


def _bm25_changed_document_ids(
    previous_records: Iterable[dict[str, Any]],
    final_records: Iterable[dict[str, Any]],
) -> list[str]:
    previous = {
        str(record.get("id") or ""): (
            _local_record_document_id(record),
            str(record.get("content_hash") or ""),
            str(record.get("record_semantic_fingerprint") or ""),
        )
        for record in previous_records
        if str(record.get("id") or "")
    }
    final = {
        str(record.get("id") or ""): (
            _local_record_document_id(record),
            str(record.get("content_hash") or ""),
            str(record.get("record_semantic_fingerprint") or ""),
        )
        for record in final_records
        if str(record.get("id") or "")
    }
    changed: set[str] = set()
    for record_id in previous.keys() | final.keys():
        before = previous.get(record_id)
        after = final.get(record_id)
        if before == after:
            continue
        if before and before[0]:
            changed.add(before[0])
        if after and after[0]:
            changed.add(after[0])
    return sorted(changed)


def _bm25_index_is_reusable(path: Path, records: list[dict[str, Any]]) -> bool:
    """Validate an existing BM25 index before taking the no-write fast path."""

    index = load_bm25_index(path)
    if (
        index is None
        or index.structured_metadata_version != BM25_STRUCTURED_METADATA_VERSION
        or index.tokenizer != tokenizer_name()
        or index.is_stale_for(records)
        or index.document_count != len(index.documents)
        or not math.isfinite(index.average_document_length)
        or index.average_document_length < 0.0
        or not math.isfinite(index.k1)
        or not math.isfinite(index.b)
        or index.k1 <= 0.0
        or not 0.0 <= index.b <= 1.0
    ):
        return False

    expected_by_id = {
        str(record.get("id") or ""): (
            _local_record_document_id(record),
            str(record.get("chunk_id") or (record.get("metadata") or {}).get("chunk_id") or ""),
            str(record.get("content_hash") or ""),
        )
        for record in records
        if str(record.get("id") or "")
    }
    seen_ids: set[str] = set()
    observed_document_frequencies: Counter[str] = Counter()
    total_document_length = 0
    for document in index.documents:
        record_id = str(document.get("id") or "")
        expected = expected_by_id.get(record_id)
        actual = (
            str(document.get("document_id") or ""),
            str(document.get("chunk_id") or ""),
            str(document.get("content_hash") or ""),
        )
        term_frequencies = document.get("term_frequencies")
        document_length = document.get("document_length")
        if (
            not record_id
            or record_id in seen_ids
            or expected is None
            or actual != expected
            or not isinstance(term_frequencies, dict)
            or isinstance(document_length, bool)
            or not isinstance(document_length, int)
            or document_length < 0
        ):
            return False
        normalized_terms: dict[str, int] = {}
        for raw_term, raw_frequency in term_frequencies.items():
            term = str(raw_term or "")
            if (
                not term
                or isinstance(raw_frequency, bool)
                or not isinstance(raw_frequency, int)
                or raw_frequency <= 0
            ):
                return False
            normalized_terms[term] = raw_frequency
        if sum(normalized_terms.values()) != document_length:
            return False
        seen_ids.add(record_id)
        observed_document_frequencies.update(normalized_terms.keys())
        total_document_length += document_length

    if dict(sorted(observed_document_frequencies.items())) != index.document_frequencies:
        return False
    expected_average = round(total_document_length / len(index.documents), 6) if index.documents else 0.0
    return math.isclose(index.average_document_length, expected_average, rel_tol=0.0, abs_tol=1e-6)


@dataclass
class _StagedReplacement:
    target: Path
    staged: Path
    backup: Path
    had_original: bool = False
    installed: bool = False


def _write_local_index_transaction(
    *,
    vector_path: Path,
    bm25_path: Path,
    previous_records: list[dict[str, Any]],
    final_records: list[dict[str, Any]],
    vector_write_required: bool,
    bm25_write_required: bool,
    changed_document_ids: list[str],
) -> tuple[Any | None, bool]:
    """Stage and commit a vector/BM25 pair without exposing a logical split."""

    confined_vector, confined_bm25, _lock_path = _confined_local_index_paths(vector_path)
    if confined_vector != vector_path or confined_bm25 != bm25_path:
        raise ValueError("Local vector transaction paths do not match their confined index pair.")
    if not vector_write_required and not bm25_write_required:
        return None, False

    replacements: list[_StagedReplacement] = []
    bm25_index = None
    bm25_incremental = False
    transaction_complete = False
    try:
        if vector_write_required:
            vector_stage = _transaction_sibling_path(vector_path, "stage")
            _write_jsonl_atomic(vector_stage, final_records)
            replacements.append(_staged_replacement(vector_path, vector_stage))

        if bm25_write_required:
            bm25_stage = _transaction_sibling_path(bm25_path, "stage")
            replacements.append(_staged_replacement(bm25_path, bm25_stage))
            if bm25_path.is_file():
                shutil.copyfile(bm25_path, bm25_stage)
            bm25_index, bm25_incremental = update_bm25_index_for_documents(
                bm25_stage,
                previous_records=previous_records,
                final_records=final_records,
                changed_document_ids=changed_document_ids,
            )
            if not bm25_stage.is_file():
                raise RuntimeError("BM25 staging did not produce an index file.")

        _commit_staged_replacements(replacements)
        transaction_complete = True
    finally:
        for replacement in replacements:
            transient_paths = [replacement.staged]
            if transaction_complete or not replacement.backup.is_file():
                transient_paths.append(replacement.backup)
            for transient in transient_paths:
                try:
                    transient.unlink(missing_ok=True)
                except OSError:
                    pass
    return bm25_index, bm25_incremental


def _transaction_sibling_path(target: Path, role: str) -> Path:
    candidate = target.with_name(
        f".{target.name}.{os.getpid()}.{uuid4().hex}.{role}"
    )
    if candidate.resolve(strict=False).parent != target.parent.resolve(strict=False):
        raise ValueError(f"Vector transaction path escaped its target directory: {candidate}")
    return candidate


def _staged_replacement(target: Path, staged: Path) -> _StagedReplacement:
    return _StagedReplacement(
        target=target,
        staged=staged,
        backup=_transaction_sibling_path(target, "backup"),
    )


def _commit_staged_replacements(replacements: list[_StagedReplacement]) -> None:
    completed: list[_StagedReplacement] = []
    try:
        for replacement in replacements:
            if not replacement.staged.is_file() or replacement.staged.is_symlink():
                raise RuntimeError(f"Invalid staged vector index file: {replacement.staged}")
            if replacement.target.is_symlink():
                raise RuntimeError(f"Vector index target became a symbolic link: {replacement.target}")
            replacement.had_original = replacement.target.is_file()
            if replacement.had_original:
                os.replace(replacement.target, replacement.backup)
            try:
                _replace_staged_path(replacement.staged, replacement.target)
            except Exception:
                if replacement.had_original and replacement.backup.is_file():
                    os.replace(replacement.backup, replacement.target)
                raise
            replacement.installed = True
            completed.append(replacement)
    except Exception as exc:
        rollback_errors: list[str] = []
        for replacement in reversed(completed):
            try:
                if replacement.had_original and replacement.backup.is_file():
                    os.replace(replacement.backup, replacement.target)
                elif not replacement.had_original:
                    replacement.target.unlink(missing_ok=True)
                replacement.installed = False
            except OSError as rollback_exc:
                rollback_errors.append(f"{replacement.target}: {rollback_exc}")
        if rollback_errors:
            raise RuntimeError(
                "Vector/BM25 transaction failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from exc
        raise


def _replace_staged_path(staged: Path, target: Path) -> None:
    os.replace(staged, target)


def _local_record_document_id(record: dict[str, Any]) -> str:
    metadata = record.get("metadata") or {}
    return str(record.get("document_id") or metadata.get("document_id") or "")


def _qdrant_point_document_id(point: dict[str, Any]) -> str:
    payload = point.get("payload") or {}
    return str(payload.get("document_id") or "")


def _metadata_row_document_id(row: dict[str, Any]) -> str:
    metadata = row.get("metadata") or {}
    return str(metadata.get("document_id") or "")


def _read_existing_qdrant_points(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    points: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            point = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid Qdrant JSONL at {path}:{line_no}: {exc}") from exc
        if not isinstance(point, dict):
            raise ValueError(f"Invalid Qdrant JSONL at {path}:{line_no}: expected object")
        if not str(point.get("id") or "").strip():
            raise ValueError(f"Invalid Qdrant JSONL at {path}:{line_no}: missing id")
        if not isinstance(point.get("vector"), list) or not point.get("vector"):
            raise ValueError(f"Invalid Qdrant JSONL at {path}:{line_no}: missing vector")
        if not isinstance(point.get("payload"), dict):
            raise ValueError(f"Invalid Qdrant JSONL at {path}:{line_no}: payload must be an object")
        points.append(point)
    return points


def _read_existing_chroma_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid Chroma JSONL at {path}:{line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"Invalid Chroma JSONL at {path}:{line_no}: expected object")
        if not str(row.get("id") or "").strip():
            raise ValueError(f"Invalid Chroma JSONL at {path}:{line_no}: missing id")
        if not isinstance(row.get("embedding"), list) or not row.get("embedding"):
            raise ValueError(f"Invalid Chroma JSONL at {path}:{line_no}: missing embedding")
        if not str(row.get("document") or "").strip():
            raise ValueError(f"Invalid Chroma JSONL at {path}:{line_no}: missing document")
        if not isinstance(row.get("metadata"), dict):
            raise ValueError(f"Invalid Chroma JSONL at {path}:{line_no}: metadata must be an object")
        rows.append(row)
    return rows


def _read_existing_pgvector_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid pgvector JSONL at {path}:{line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"Invalid pgvector JSONL at {path}:{line_no}: expected object")
        if not str(row.get("id") or "").strip():
            raise ValueError(f"Invalid pgvector JSONL at {path}:{line_no}: missing id")
        if not isinstance(row.get("embedding"), list) or not row.get("embedding"):
            raise ValueError(f"Invalid pgvector JSONL at {path}:{line_no}: missing embedding")
        if not str(row.get("content") or "").strip():
            raise ValueError(f"Invalid pgvector JSONL at {path}:{line_no}: missing content")
        rows.append(row)
    return rows


def _read_existing_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return load_vector_records_jsonl(path)


def _write_jsonl_atomic(path: Path, records: list[dict[str, Any]]) -> None:
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        tmp_path.write_text(
            "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + ("\n" if records else ""),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
