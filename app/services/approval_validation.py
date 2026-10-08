from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.tenant_access import settings_for_tenant
from app.core.security import AuthContext
from app.ingestion.vector_adapter import vector_record_from_chunk, vector_record_semantic_fingerprint
from app.processors.exporter import Exporter
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.services.regulation_rag_runtime import (
    approval_journal_match_index, approval_journal_match_key,
    expected_vector_record_for_chunk, journal_event_timestamp, latest_approval_journal_match_index,
)
from app.services.review_decision_service import approved_content_hash, department_acl_set
from app.storage.repository import JsonRepository


@dataclass
class ApprovalValidationContext:
    """Authority and snapshot indexes scoped to one validation invocation."""

    document_id: str
    tenant_id: str
    latest_match_index: set[tuple[Any, ...]]
    approvals_by_key: dict[tuple[Any, ...], dict[str, Any]]
    snapshot_chunks: dict[Path, dict[tuple[str, str], Chunk]] = field(default_factory=dict)


def build_repository_approval_validation_context(
    repository: JsonRepository,
    *,
    document_id: str,
    tenant_id: str,
    approval_records: list[dict[str, Any]] | None = None,
) -> ApprovalValidationContext:
    records = approval_records if approval_records is not None else repository.list_approval_journal_records(document_id)
    latest = latest_approval_journal_match_index(
        records, repository.list_review_journal_records(document_id), tenant_id=tenant_id,
    )
    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    # Latest authority already excludes missing/ambiguous timestamps and provenance.
    def event_time(record: dict[str, Any]) -> float:
        timestamp = journal_event_timestamp(record.get("approved_at"))
        return timestamp.timestamp() if timestamp is not None else float("-inf")

    ordered = sorted(records, key=event_time)
    for record in ordered:
        for key in approval_journal_match_index([record]):
            if key in latest:
                by_key[key] = record
    return ApprovalValidationContext(document_id, tenant_id, latest, by_key)


def _load_approval_snapshot(
    snapshot_path: Path,
    context: ApprovalValidationContext,
) -> dict[tuple[str, str], Chunk]:
    if snapshot_path in context.snapshot_chunks:
        return context.snapshot_chunks[snapshot_path]
    chunks: dict[tuple[str, str], Chunk] = {}
    try:
        with snapshot_path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                item = Chunk.model_validate(json.loads(line))
                key = (item.document_id, item.chunk_id)
                if key in chunks:
                    raise ValueError("Official ingestion approval snapshot contains duplicate chunks.")
                chunks[key] = item
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("Official ingestion approval snapshot is unavailable or invalid.") from exc
    context.snapshot_chunks[snapshot_path] = chunks
    return chunks


def validate_export_chunks_against_repository(
    chunks: list[dict[str, Any]],
    *,
    data_dir: Path,
    tenant_storage_isolation: bool,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    if tenant_storage_isolation and not str(tenant_id or "").strip():
        raise ValueError("tenant_id is required when tenant storage isolation is enabled.")
    settings = settings_for_tenant(
        Settings(data_dir=data_dir, tenant_storage_isolation=tenant_storage_isolation),
        tenant_id,
    )
    repository = JsonRepository(settings)
    by_document: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks:
        document_id = str(chunk.get("document_id") or "").strip()
        chunk_id = str(chunk.get("chunk_id") or "").strip()
        if not document_id or not chunk_id:
            raise ValueError("Chunk export contains a record missing document_id or chunk_id.")
        by_document.setdefault(document_id, []).append(chunk)

    for document_id, document_chunks in sorted(by_document.items()):
        repository_chunks = {str(item.chunk_id): item for item in repository.get_chunks(document_id)}
        approvals = repository.list_approval_journal_records(document_id)
        document = repository.get_document(document_id)
        canonical_tenant = str(getattr(document, "tenant_id", "") or "").strip()
        context = build_repository_approval_validation_context(
            repository, document_id=document_id, tenant_id=canonical_tenant, approval_records=approvals,
        )
        for chunk in document_chunks:
            chunk_id = str(chunk.get("chunk_id") or "").strip()
            repository_chunk = repository_chunks.get(chunk_id)
            if repository_chunk is None:
                raise ValueError(
                    f"Chunk {document_id}:{chunk_id} is not present in the repository and cannot be exported as official ingestion."
                )
            if not repository_chunk_matches_expected(
                repository_chunk,
                approval_status="approved",
                approval_id=str(chunk.get("approval_id") or "").strip(),
                approved_content_hash=str(chunk.get("approved_content_hash") or "").strip(),
                tenant_id=str(chunk.get("tenant_id") or (chunk.get("metadata") or {}).get("tenant_id") or "").strip(),
                security_level=str(chunk.get("security_level") or "").strip().lower(),
            ):
                raise ValueError(f"Chunk {document_id}:{chunk_id} does not match current repository approval state.")
            validate_repository_approval_content(
                repository, repository_chunk, document, tenant_id=canonical_tenant,
                context=context,
            )
            _validate_canonical_ingestion_payload(
                chunk, repository_chunk=repository_chunk,
                document=document,
                expected_tenant_id=tenant_id, exported_chunk=True,
            )


    return {
        "checked": True,
        "data_dir": str(data_dir.resolve()),
        "tenant_storage_isolation": tenant_storage_isolation,
        "tenant_id": str(tenant_id or ""),
        "validated_chunk_count": len(chunks),
        "document_count": len(by_document),
    }


def validate_vector_records_against_repository(
    records: list[dict[str, Any]],
    *,
    data_dir: Path,
    tenant_storage_isolation: bool,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    if tenant_storage_isolation and not str(tenant_id or "").strip():
        raise ValueError("tenant_id is required when tenant storage isolation is enabled.")
    settings = settings_for_tenant(
        Settings(data_dir=data_dir, tenant_storage_isolation=tenant_storage_isolation),
        tenant_id,
    )
    repository = JsonRepository(settings)
    by_document: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        document_id = str(record.get("document_id") or metadata.get("document_id") or "").strip()
        chunk_id = str(record.get("chunk_id") or metadata.get("chunk_id") or "").strip()
        if not document_id or not chunk_id:
            raise ValueError("Vector upsert input contains a record missing document_id or chunk_id.")
        by_document.setdefault(document_id, []).append(record)

    for document_id, document_records in sorted(by_document.items()):
        repository_chunks = {str(item.chunk_id): item for item in repository.get_chunks(document_id)}
        approvals = repository.list_approval_journal_records(document_id)
        document = repository.get_document(document_id)
        canonical_tenant = str(getattr(document, "tenant_id", "") or "").strip()
        context = build_repository_approval_validation_context(
            repository, document_id=document_id, tenant_id=canonical_tenant, approval_records=approvals,
        )
        for record in document_records:
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            chunk_id = str(record.get("chunk_id") or metadata.get("chunk_id") or "").strip()
            repository_chunk = repository_chunks.get(chunk_id)
            if repository_chunk is None:
                raise ValueError(
                    f"Vector record {document_id}:{chunk_id} is not present in the repository and cannot be upserted into an official target."
                )
            if not repository_chunk_matches_expected(
                repository_chunk,
                approval_status="approved",
                approval_id=str(metadata.get("approval_id") or "").strip(),
                approved_content_hash=str(metadata.get("approved_content_hash") or "").strip(),
                tenant_id=str(metadata.get("tenant_id") or "").strip(),
                security_level=str(metadata.get("security_level") or "").strip().lower(),
            ):
                raise ValueError(
                    f"Vector record {document_id}:{chunk_id} does not match current repository approval state."
                )
            validate_repository_approval_content(
                repository, repository_chunk, document, tenant_id=canonical_tenant,
                context=context,
            )
            _validate_canonical_ingestion_payload(
                record, repository_chunk=repository_chunk,
                document=document,
                expected_tenant_id=tenant_id, exported_chunk=False,
            )


    return {
        "checked": True,
        "data_dir": str(data_dir.resolve()),
        "tenant_storage_isolation": tenant_storage_isolation,
        "tenant_id": str(tenant_id or ""),
        "validated_record_count": len(records),
        "document_count": len(by_document),
    }


def validate_repository_approval_content(
    repository: JsonRepository,
    repository_chunk: Chunk,
    document: Document | None,
    *,
    tenant_id: str,
    context: ApprovalValidationContext | None = None,
) -> dict[str, Any]:
    """Require the latest durable decision and unchanged approved content."""
    if (repository_chunk.approval_status != "approved" or document is None
        or document.document_id != repository_chunk.document_id):
        raise ValueError("Official ingestion requires a current approved document chunk.")
    context = context or build_repository_approval_validation_context(
        repository, document_id=repository_chunk.document_id, tenant_id=tenant_id,
    )
    if context.document_id != repository_chunk.document_id or context.tenant_id != tenant_id:
        raise ValueError("Official ingestion validation context is outside the requested scope.")
    key = approval_journal_match_key(
        chunk_id=repository_chunk.chunk_id, document_id=repository_chunk.document_id,
        tenant_id=tenant_id, approval_id=str(repository_chunk.approval_id or ""),
        approved_content_hash=str(repository_chunk.approved_content_hash or ""),
        expected_metadata=dict(repository_chunk.metadata or {}),
    )
    if key not in context.latest_match_index:
        raise ValueError("Official ingestion has no unique latest matching approval journal decision.")
    approval = context.approvals_by_key.get(key)
    if approval is None or document is None or document.tenant_id != tenant_id:
        raise ValueError("Official ingestion approval is outside the canonical document tenant.")
    if approval.get("profile_id") and approval["profile_id"] != document.profile_id:
        raise ValueError("Official ingestion document profile changed after approval.")
    snapshot_name = str(approval.get("snapshot") or "")
    if not snapshot_name:
        if approved_content_hash(repository_chunk) != repository_chunk.approved_content_hash:
            raise ValueError("Official ingestion repository content no longer matches its approved hash.")
        return approval
    snapshot_path = (repository.data_dir / snapshot_name).resolve()
    snapshot_root = (repository.root / "review_snapshots").resolve()
    if snapshot_path.parent != snapshot_root or snapshot_path.suffix != ".jsonl":
        raise ValueError("Official ingestion approval snapshot path is invalid.")
    snapshots = _load_approval_snapshot(snapshot_path, context)
    snapshot = snapshots.get((repository_chunk.document_id, repository_chunk.chunk_id))
    if snapshot is None:
        raise ValueError("Official ingestion approval snapshot has no matching chunk.")
    if (
        snapshot.approval_status != "approved"
        or snapshot.approval_id != repository_chunk.approval_id
        or snapshot.approved_content_hash != repository_chunk.approved_content_hash
        or any(getattr(snapshot, field) != getattr(repository_chunk, field) for field in (
            "text", "normalized_text", "retrieval_text", "ai_preprocessed_text",
            "security_level", "department_acl",
        ))
    ):
        raise ValueError("Official ingestion repository content differs from its approval snapshot.")
    metadata = dict(repository_chunk.metadata or {})
    for field in ("regulation_status", "effective_to"):
        if metadata.get(field) == snapshot.metadata.get(field):
            continue
        if metadata.get(field) != getattr(document, field, None):
            raise ValueError("Official ingestion lifecycle metadata differs from the canonical document.")
        # Lifecycle transitions do not grant a new approval for content changes.
        if field in snapshot.metadata:
            metadata[field] = snapshot.metadata[field]
        else:
            metadata.pop(field, None)
    canonical_current = repository_chunk.model_copy(update={"metadata": metadata})
    if approved_content_hash(canonical_current) != approved_content_hash(snapshot):
        raise ValueError("Official ingestion canonical approval content is stale.")
    if dict(canonical_current.metadata or {}) != dict(snapshot.metadata or {}):
        raise ValueError("Official ingestion approval provenance or metadata is stale.")
    return approval


def _validate_canonical_ingestion_payload(
    payload: dict[str, Any],
    *,
    repository_chunk: Chunk,
    document: Document | None,
    expected_tenant_id: str | None,
    exported_chunk: bool,
) -> None:
    """Bind input to current approved repository content, not caller-supplied hashes.

    The repository model, its flattened export, and the document-enriched API
    export are the supported canonical serializers. Every candidate is built
    solely from repository data; incoming verification stamps are not authority.
    """
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    tenant_id = str((repository_chunk.metadata or {}).get("tenant_id") or "").strip()
    if (
        document is None or not tenant_id
        or str(document.tenant_id or "").strip() != tenant_id
        or (expected_tenant_id is not None and str(expected_tenant_id).strip() != tenant_id)
        or str(repository_chunk.document_id) != str(document.document_id)
    ):
        raise ValueError("Official ingestion document is outside the requested repository tenant scope.")
    expected_scope = {
        "tenant_id": tenant_id,
        "profile_id": document.profile_id or (repository_chunk.metadata or {}).get("profile_id"),
        "security_level": repository_chunk.security_level,
        "department_acl": department_acl_set(repository_chunk.department_acl),
    }
    for container in (payload, metadata):
        for field, expected in expected_scope.items():
            if field not in container:
                continue
            value = container[field]
            if field == "department_acl":
                matches = department_acl_set(value) == expected
            elif field == "security_level":
                matches = str(value or "").strip().lower() == str(expected or "").strip().lower()
            else:
                matches = str(value or "").strip() == str(expected or "").strip()
            if not matches:
                raise ValueError(f"Official ingestion {field} does not match canonical repository scope.")
    flat_chunk = Exporter()._flat_chunk(repository_chunk)
    for field in ("text", "normalized_text", "retrieval_text", "ai_preprocessed_text"):
        if field not in payload or (not exported_chunk and field == "text"):
            continue
        current = getattr(repository_chunk, field, None)
        # The official flat serializer appends approved table markdown to retrieval text.
        serialized = flat_chunk.get(field, current)
        if payload[field] != current and payload[field] != serialized:
            raise ValueError(f"Official ingestion {field} does not match approved repository content.")
    auth = AuthContext(actor="repository-validator", tenant_id=tenant_id, auth_mode="local", role="admin")
    expected_records = [expected_vector_record_for_chunk(repository_chunk, document, auth)]
    for canonical_chunk in (repository_chunk.model_dump(mode="json"), flat_chunk):
        for text_field in ("retrieval_text", "text", "normalized_text"):
            expected_records.append(vector_record_from_chunk(canonical_chunk, text_field=text_field))
    incoming_record = vector_record_from_chunk(payload) if exported_chunk else payload
    if incoming_record is None:
        raise ValueError("Official ingestion has no approved canonical content.")
    def fingerprint(record: dict[str, Any]) -> str:
        return vector_record_semantic_fingerprint({**record, "tenant_id": tenant_id})
    incoming_fingerprint = fingerprint(incoming_record)
    if not any(record is not None and fingerprint(record) == incoming_fingerprint for record in expected_records):
        raise ValueError("Official ingestion payload does not match approved canonical repository content.")


def repository_chunk_matches_expected(
    repository_chunk: Chunk,
    *,
    approval_status: str,
    approval_id: str,
    approved_content_hash: str,
    tenant_id: str,
    security_level: str,
) -> bool:
    checks = (
        ("approval_status", approval_status),
        ("approval_id", approval_id),
        ("approved_content_hash", approved_content_hash),
        ("tenant_id", tenant_id),
        ("security_level", security_level),
    )
    for field, expected in checks:
        value = (repository_chunk.metadata or {}).get(field) if field == "tenant_id" else getattr(repository_chunk, field, "")
        current = str(value or "").strip()
        if field == "security_level":
            current = current.lower()
        if not expected or current != expected:
            return False
    return True


def has_matching_approval_journal_record(
    approval_records: list[dict[str, Any]],
    *,
    document_id: str,
    chunk_id: str,
    tenant_id: str,
    approval_id: str,
    approved_hash: str,
) -> bool:
    if not all((document_id, chunk_id, tenant_id, approval_id, approved_hash)):
        return False
    for approval in approval_records:
        if str(approval.get("document_id") or "").strip() != document_id:
            continue
        if str(approval.get("tenant_id") or "").strip() != tenant_id:
            continue
        if str(approval.get("approval_id") or "").strip() != approval_id:
            continue
        if chunk_id not in {str(value).strip() for value in approval.get("chunk_ids") or []}:
            continue
        if approval_record_chunk_hash(approval, chunk_id) != approved_hash:
            continue
        return True
    return False


def approval_record_chunk_hash(record: dict[str, Any], chunk_id: str) -> str:
    hashes = record.get("approved_content_hashes")
    if isinstance(hashes, dict):
        return str(hashes.get(chunk_id) or "").strip()
    for item in record.get("approved_chunks") or []:
        if str(item.get("chunk_id") or "").strip() == chunk_id:
            return str(item.get("approved_content_hash") or "").strip()
    return ""
