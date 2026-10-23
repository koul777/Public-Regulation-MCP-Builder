from __future__ import annotations

"""Bounded read-only source-body witnesses for already authorized vector records."""

from collections import OrderedDict
from dataclasses import dataclass
import json
from pathlib import Path
import threading
from typing import Any, Iterator

from app.core.config import Settings
from app.core.security_primitives import AuthContext
from app.services.approved_source_text import (
    SOURCE_UNAVAILABLE, SourceText, looks_like_augmented_text, source_text_from_verified_record,
)
from app.services.regulation_rag_runtime import path_signature, repository_path_descriptor


_SOURCE_WITNESS_CACHE_MAX_ENTRIES = 8
_SOURCE_WITNESS_CACHE_MAX_BYTES = 64 * 1024 * 1024
_SOURCE_WITNESS_FILE_MAX_BYTES = 256 * 1024 * 1024
_SOURCE_WITNESS_RECORD_MAX_CHARS = 8 * 1024 * 1024
_SOURCE_WITNESS_CACHE_LOCK = threading.Lock()
_SOURCE_WITNESS_CACHE: OrderedDict[Path, tuple[Any, dict[str, "_SourceBodyWitness"], int]] = OrderedDict()


@dataclass(frozen=True)
class _SourceBodyWitness:
    body: str | None
    table: str | None
    approval_status: str
    approval_id: str
    approved_content_hash: str
    tenant_id: str
    profile_id: str


def _unique_witness_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate source witness JSON key.")
        result[key] = value
    return result


def _iter_source_witness_rows(path: Path) -> Iterator[object]:
    """Stream one JSON array without constructing a full repository object graph."""
    decoder = json.JSONDecoder(object_pairs_hook=_unique_witness_object)
    with path.open("r", encoding="utf-8") as stream:
        buffer = ""
        cursor = 0
        eof = False
        read_chars = 0

        def fill() -> None:
            nonlocal buffer, cursor, eof, read_chars
            tail = stream.read(64 * 1024)
            read_chars += len(tail)
            if read_chars > _SOURCE_WITNESS_FILE_MAX_BYTES:
                raise ValueError("Source witness file limit exceeded.")
            buffer = buffer[cursor:] + tail
            cursor = 0
            eof = not tail

        def skip_space() -> None:
            nonlocal cursor
            while True:
                while cursor < len(buffer) and buffer[cursor].isspace():
                    cursor += 1
                if cursor < len(buffer) or eof:
                    return
                fill()

        skip_space()
        if cursor >= len(buffer) or buffer[cursor] != "[":
            raise ValueError("Source witness file must be a JSON array.")
        cursor += 1
        first = True
        while True:
            skip_space()
            if cursor < len(buffer) and buffer[cursor] == "]":
                cursor += 1
                break
            if not first:
                if cursor >= len(buffer) or buffer[cursor] != ",":
                    raise ValueError("Source witness array separator missing.")
                cursor += 1
                skip_space()
            while True:
                try:
                    row, end = decoder.raw_decode(buffer, cursor)
                    break
                except json.JSONDecodeError:
                    if eof or len(buffer) - cursor > _SOURCE_WITNESS_RECORD_MAX_CHARS:
                        raise ValueError("Invalid source witness record.") from None
                    fill()
            if end - cursor > _SOURCE_WITNESS_RECORD_MAX_CHARS:
                raise ValueError("Source witness record limit exceeded.")
            cursor = end
            first = False
            yield row
        skip_space()
        if cursor != len(buffer) or not eof:
            raise ValueError("Unexpected content after source witness array.")


def _load_source_body_witnesses(path: Path, document_id: str) -> tuple[dict[str, _SourceBodyWitness], int]:
    witnesses: dict[str, _SourceBodyWitness] = {}
    retained_bytes = 0
    for row in _iter_source_witness_rows(path):
        if not isinstance(row, dict) or row.get("document_id") != document_id:
            raise ValueError("Source witness document mismatch.")
        chunk_id = row.get("chunk_id")
        if not isinstance(chunk_id, str) or not chunk_id or chunk_id in witnesses:
            raise ValueError("Source witness chunk identity is invalid.")
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        body = row.get("normalized_text")
        table = metadata.get("table_markdown")
        body = body if isinstance(body, str) else None
        table = table if isinstance(table, str) else None
        witness = _SourceBodyWitness(
            body=body, table=table,
            approval_status=str(row.get("approval_status") or ""),
            approval_id=str(row.get("approval_id") or ""),
            approved_content_hash=str(row.get("approved_content_hash") or ""),
            tenant_id=str(row.get("tenant_id") or metadata.get("tenant_id") or ""),
            profile_id=str(metadata.get("profile_id") or ""),
        )
        retained_bytes += sum(len(value.encode("utf-8")) for value in (
            chunk_id, body or "", table or "", witness.approval_status,
            witness.approval_id, witness.approved_content_hash, witness.tenant_id, witness.profile_id,
        ))
        if retained_bytes > _SOURCE_WITNESS_CACHE_MAX_BYTES or len(witnesses) >= 100_000:
            raise ValueError("Source witness cache limit exceeded.")
        witnesses[chunk_id] = witness
    return witnesses, retained_bytes


def source_text_from_current_witness(*, settings: Settings, auth: AuthContext, record: dict[str, Any]) -> SourceText:
    """Resolve a body only after existing visibility gates, without JsonRepository."""
    if not looks_like_augmented_text(str(record.get("text") or "")):
        return source_text_from_verified_record(record, expected_tenant_id=auth.tenant_id)
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    document_id = str(record.get("document_id") or metadata.get("document_id") or "")
    chunk_id = str(record.get("chunk_id") or metadata.get("chunk_id") or "")
    if not document_id or not chunk_id or any(value in document_id for value in ("/", "\\", ":")):
        return SOURCE_UNAVAILABLE
    root = repository_path_descriptor(settings).root
    path = root / f"{document_id}_chunks.json"
    try:
        if (root.is_symlink() or getattr(root, "is_junction", lambda: False)()
                or path.is_symlink() or getattr(path, "is_junction", lambda: False)()
                or path.resolve().parent != root.resolve()):
            return SOURCE_UNAVAILABLE
        identity = path_signature(path)
        if identity is None or identity[1] > _SOURCE_WITNESS_FILE_MAX_BYTES:
            return SOURCE_UNAVAILABLE
        key = path.resolve()
        with _SOURCE_WITNESS_CACHE_LOCK:
            entry = _SOURCE_WITNESS_CACHE.get(key)
            if entry is None or entry[0] != identity:
                _SOURCE_WITNESS_CACHE.pop(key, None)
                witnesses, retained_bytes = _load_source_body_witnesses(path, document_id)
                if path_signature(path) != identity:
                    return SOURCE_UNAVAILABLE
                entry = (identity, witnesses, retained_bytes)
                _SOURCE_WITNESS_CACHE[key] = entry
                while (len(_SOURCE_WITNESS_CACHE) > _SOURCE_WITNESS_CACHE_MAX_ENTRIES
                       or sum(value[2] for value in _SOURCE_WITNESS_CACHE.values()) > _SOURCE_WITNESS_CACHE_MAX_BYTES):
                    _SOURCE_WITNESS_CACHE.popitem(last=False)
            else:
                _SOURCE_WITNESS_CACHE.move_to_end(key)
            witness = entry[1].get(chunk_id)
        if witness is None or path_signature(path) != identity:
            return SOURCE_UNAVAILABLE
        if (witness.approval_status != "approved" or witness.approval_id != metadata.get("approval_id")
                or witness.approved_content_hash != metadata.get("approved_content_hash")
                or (witness.tenant_id and witness.tenant_id != auth.tenant_id)
                or (witness.profile_id and witness.profile_id != metadata.get("profile_id"))):
            return SOURCE_UNAVAILABLE
        result = source_text_from_verified_record(
            record, body_witness=witness.body, table_witness=witness.table,
            expected_tenant_id=auth.tenant_id,
        )
        return result if path_signature(path) == identity else SOURCE_UNAVAILABLE
    except (OSError, ValueError, RuntimeError):
        return SOURCE_UNAVAILABLE

