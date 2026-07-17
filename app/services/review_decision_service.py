from __future__ import annotations

from app.core.security import normalize_department_ids
from app.ingestion.vector_adapter import stable_content_hash
from app.schemas.chunk import Chunk


NON_APPROVABLE_CHUNK_STATUSES = frozenset({"security_blocked", "rejected", "superseded"})
APPROVAL_WORKLIST_METADATA_KEYS = frozenset(
    {
        "approval_worklist_report_path",
        "approval_worklist_report_sha256",
        "approval_review_batch_manifest_path",
        "approval_review_batch_manifest_sha256",
        "approval_review_batch_id",
        "approval_review_batch_chunk_fingerprint",
        "approval_review_strategy",
    }
)


def department_acl_set(value) -> list[str]:
    if value is None:
        return []
    return sorted(normalize_department_ids(value))


def approved_content_hash(
    chunk: Chunk,
    *,
    security_level: str | None = None,
    department_acl: list[str] | tuple[str, ...] | None = None,
) -> str:
    text = chunk.retrieval_text or chunk.normalized_text or chunk.text
    metadata = dict(chunk.metadata)
    for key in APPROVAL_WORKLIST_METADATA_KEYS:
        metadata.pop(key, None)
    scope_security_level = security_level if security_level is not None else chunk.security_level
    scope_department_acl = department_acl if department_acl is not None else chunk.department_acl
    if scope_security_level:
        metadata["security_level"] = str(scope_security_level).strip().lower()
    if scope_department_acl:
        metadata["department_acl"] = department_acl_set(scope_department_acl)
    return stable_content_hash(text, metadata)


def approval_worklist_metadata(evidence: dict[str, str]) -> dict[str, str]:
    mapping = {
        "worklist_report_path": "approval_worklist_report_path",
        "worklist_report_sha256": "approval_worklist_report_sha256",
        "review_batch_manifest_path": "approval_review_batch_manifest_path",
        "review_batch_manifest_sha256": "approval_review_batch_manifest_sha256",
        "review_batch_id": "approval_review_batch_id",
        "review_batch_chunk_fingerprint": "approval_review_batch_chunk_fingerprint",
        "review_strategy": "approval_review_strategy",
    }
    return {metadata_key: evidence[key] for key, metadata_key in mapping.items() if key in evidence}


def chunk_hashes(chunks: list[Chunk], chunk_ids: set[str] | None = None) -> dict[str, str]:
    selected_ids = chunk_ids or {chunk.chunk_id for chunk in chunks}
    return {chunk.chunk_id: approved_content_hash(chunk) for chunk in chunks if chunk.chunk_id in selected_ids}
