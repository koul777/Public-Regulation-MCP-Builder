from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.tenant_access import settings_for_tenant
from app.storage.repository import JsonRepository


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
                tenant_id=str(chunk.get("tenant_id") or "").strip(),
                security_level=str(chunk.get("security_level") or "").strip().lower(),
            ):
                raise ValueError(f"Chunk {document_id}:{chunk_id} does not match current repository approval state.")
            if not has_matching_approval_journal_record(
                approvals,
                document_id=document_id,
                chunk_id=chunk_id,
                tenant_id=str(chunk.get("tenant_id") or "").strip(),
                approval_id=str(chunk.get("approval_id") or "").strip(),
                approved_hash=str(chunk.get("approved_content_hash") or "").strip(),
            ):
                raise ValueError(f"Chunk {document_id}:{chunk_id} has no matching approval journal record.")

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
            if not has_matching_approval_journal_record(
                approvals,
                document_id=document_id,
                chunk_id=chunk_id,
                tenant_id=str(metadata.get("tenant_id") or "").strip(),
                approval_id=str(metadata.get("approval_id") or "").strip(),
                approved_hash=str(metadata.get("approved_content_hash") or "").strip(),
            ):
                raise ValueError(f"Vector record {document_id}:{chunk_id} has no matching approval journal record.")

    return {
        "checked": True,
        "data_dir": str(data_dir.resolve()),
        "tenant_storage_isolation": tenant_storage_isolation,
        "tenant_id": str(tenant_id or ""),
        "validated_record_count": len(records),
        "document_count": len(by_document),
    }


def repository_chunk_matches_expected(
    repository_chunk: Any,
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
        current = str(getattr(repository_chunk, field, "") or "").strip()
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
