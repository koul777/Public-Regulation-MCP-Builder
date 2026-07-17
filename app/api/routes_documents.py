from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Annotated, Any, Self, Sequence

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic.fields import FieldInfo
from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.api_audit import audit_api_event, redact_sensitive_paths
from app.core.config import Settings
from app.core.input_limits import (
    MAX_ARTIFACT_PATH_CHARS,
    MAX_IDENTIFIER_CHARS,
    MAX_METADATA_PATCH_JSON_ITEMS,
    MAX_METADATA_PATCH_JSON_TEXT_CHARS,
    MAX_METADATA_PATCH_KEYS,
    MAX_NOTE_CHARS,
    MAX_REVIEW_CHUNK_IDS,
    MAX_REVIEW_DECISION_EVENTS,
    MAX_REVIEW_DEPARTMENT_IDS,
    MAX_REVIEW_EVENT_JSON_ITEMS,
    MAX_REVIEW_EVENT_JSON_TEXT_CHARS,
    MAX_REVIEW_TEXT_CHARS,
    MAX_REVIEW_TEXT_TOTAL_CHARS,
    MAX_SHORT_LABEL_CHARS,
    MAX_SPLIT_PARTS,
    validate_json_value_budget,
)
from app.core.institution_profiles import apply_institution_profile_to_metadata, load_institution_profile_registry
from app.core.tenant_access import resource_visible_to_tenant, settings_for_tenant, tenant_storage_key
from app.ingestion.embedding_adapter import LOCAL_HASH_EMBEDDING_MODEL, embed_vector_records
from app.ingestion.vector_adapter import (
    APPROVED_CHUNK_STATUS,
    approval_provenance_issue_fields,
    build_vector_records,
    stable_content_hash,
)
from app.ingestion.vector_integrity import embedded_vector_integrity_reason
from app.ingestion.vector_upsert import (
    validate_vector_record_tenant_scope,
    validate_vector_target_tenant_scope,
    vector_upsert_target,
)
from app.parsers.base import ParserError
from app.processors.exporter import Exporter
from app.schemas.chunk import Chunk, ChunkOptions
from app.schemas.document import Document
from app.core.security import (
    API_ROLE_ADMIN,
    API_READ_ROLES,
    API_WRITE_ROLES,
    AuthContext,
    coerce_auth_context,
    get_auth_context,
    require_api_role,
)
from app.services.document_service import DocumentService
from app.services.processing_service import ProcessingService
from app.services.regulation_catalog_service import latest_history_version
from app.services.regulation_lifecycle_service import (
    RegulationLifecycleError,
    apply_transition as apply_regulation_transition,
)
from app.services.review_decision_service import (
    APPROVAL_WORKLIST_METADATA_KEYS,
    approval_worklist_metadata as _approval_worklist_metadata,
    approved_content_hash as _approved_content_hash,
    chunk_hashes as _chunk_hashes,
    department_acl_set as _department_acl_set,
)
from app.services.review_workflow_service import (
    ReviewWorkflowError,
    approval_worklist_evidence as _service_approval_worklist_evidence,
    build_approval_record as _build_approval_record,
    build_rejection_record as _build_rejection_record,
    build_security_scan_record as _build_security_scan_record,
    chunk_review_attention_reasons as _service_chunk_review_attention_reasons,
    clean_evidence_text as _service_clean_evidence_text,
    clean_evidence_value as _clean_evidence_value,
    json_safe as _json_safe,
    normalize_security_level as _service_normalize_security_level,
    prepare_approval_decision as _service_prepare_approval_decision,
    prepare_rejection_decision as _service_prepare_rejection_decision,
    prepare_security_scan_update as _prepare_security_scan_update,
    normalize_evidence_artifact_path as _service_normalize_evidence_artifact_path,
    normalize_evidence_identifier as _service_normalize_evidence_identifier,
    normalize_optional_sha256 as _service_normalize_optional_sha256,
    require_chunk_ids as _service_require_chunk_ids,
    review_batch_chunk_fingerprint as _review_batch_chunk_fingerprint,
    review_content_hash as _review_content_hash,
    review_text_basis as _review_text_basis,
    sha256_file as _sha256_file,
    validate_approval_preconditions as _service_validate_approval_preconditions,
    verify_approval_evidence as _service_verify_approval_evidence,
)
from app.services.approval_governance import (
    approval_state_transition as _approval_state_transition,
    sanitize_review_decision_events as _sanitize_review_decision_events,
)
from app.storage.file_store import FileStore
from app.storage.repository import JsonRepository
from app.core.config import get_settings


router = APIRouter(prefix="/api/documents", tags=["documents"])

_PROCESSING_FAILURE_DETAIL = "Document processing failed. Review server audit logs for details."
_INDEXING_FAILURE_DETAIL = "Document indexing failed. Review server audit logs for details."


def _safe_expected_error_detail(exc: Exception, *, max_chars: int = 1000) -> str:
    return redact_sensitive_paths(str(exc).strip())[:max_chars]


ReviewIdentifier = Annotated[str, Field(min_length=1, max_length=MAX_IDENTIFIER_CHARS)]
ReviewDepartmentId = Annotated[str, Field(min_length=1, max_length=MAX_SHORT_LABEL_CHARS)]
ReviewText = Annotated[str, Field(max_length=MAX_REVIEW_TEXT_CHARS)]


class ProcessRequest(BaseModel):
    parser_options: ChunkOptions | None = None


class ApprovalRequest(BaseModel):
    chunk_ids: list[ReviewIdentifier] = Field(default_factory=list, max_length=MAX_REVIEW_CHUNK_IDS)
    approval_id: str | None = Field(default=None, max_length=MAX_IDENTIFIER_CHARS)
    security_level: str | None = Field(default=None, max_length=MAX_SHORT_LABEL_CHARS)
    review_flags_acknowledged: bool = False
    worklist_report_path: str | None = Field(default=None, max_length=MAX_ARTIFACT_PATH_CHARS)
    worklist_report_sha256: str | None = Field(default=None, max_length=MAX_SHORT_LABEL_CHARS)
    review_batch_manifest_path: str | None = Field(default=None, max_length=MAX_ARTIFACT_PATH_CHARS)
    review_batch_manifest_sha256: str | None = Field(default=None, max_length=MAX_SHORT_LABEL_CHARS)
    review_batch_id: str | None = Field(default=None, max_length=MAX_IDENTIFIER_CHARS)
    review_batch_chunk_fingerprint: str | None = Field(default=None, max_length=MAX_SHORT_LABEL_CHARS)
    review_strategy: str | None = Field(default=None, max_length=MAX_SHORT_LABEL_CHARS)
    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS)
    review_decision_events: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=MAX_REVIEW_DECISION_EVENTS,
    )
    approval_override_reason: str | None = Field(default=None, max_length=1000)

    @field_validator("review_decision_events")
    @classmethod
    def validate_review_decision_event_budget(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return validate_json_value_budget(
            value,
            field_name="review_decision_events",
            max_items=MAX_REVIEW_EVENT_JSON_ITEMS,
            max_text_chars=MAX_REVIEW_EVENT_JSON_TEXT_CHARS,
        )


class ReviewChunkUpdateRequest(BaseModel):
    text: ReviewText | None = None
    normalized_text: ReviewText | None = None
    retrieval_text: ReviewText | None = None
    chunk_type: str | None = Field(default=None, max_length=MAX_SHORT_LABEL_CHARS)
    security_level: str | None = Field(default=None, max_length=MAX_SHORT_LABEL_CHARS)
    department_acl: list[ReviewDepartmentId] | None = Field(
        default=None,
        max_length=MAX_REVIEW_DEPARTMENT_IDS,
    )
    metadata_patch: dict[str, Any] | None = Field(default=None, max_length=MAX_METADATA_PATCH_KEYS)
    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS)

    @field_validator("metadata_patch")
    @classmethod
    def validate_metadata_patch_budget(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return validate_json_value_budget(
            value,
            field_name="metadata_patch",
            max_items=MAX_METADATA_PATCH_JSON_ITEMS,
            max_text_chars=MAX_METADATA_PATCH_JSON_TEXT_CHARS,
        )

    @model_validator(mode="after")
    def validate_review_text_budget(self) -> Self:
        total_chars = sum(len(value or "") for value in (self.text, self.normalized_text, self.retrieval_text))
        if total_chars > MAX_REVIEW_TEXT_TOTAL_CHARS:
            raise ValueError(
                "review text fields exceed the combined maximum of "
                f"{MAX_REVIEW_TEXT_TOTAL_CHARS} characters."
            )
        return self


class RejectRequest(BaseModel):
    chunk_ids: list[ReviewIdentifier] = Field(default_factory=list, max_length=MAX_REVIEW_CHUNK_IDS)
    reason: str = Field(default="", max_length=1000)
    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS)


class SplitChunkRequest(BaseModel):
    texts: list[ReviewText] = Field(default_factory=list, max_length=MAX_SPLIT_PARTS)
    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS)

    @field_validator("texts")
    @classmethod
    def validate_split_text_budget(cls, value: list[str]) -> list[str]:
        if sum(len(text) for text in value) > MAX_REVIEW_TEXT_TOTAL_CHARS:
            raise ValueError(
                f"texts exceed the combined maximum of {MAX_REVIEW_TEXT_TOTAL_CHARS} characters."
            )
        return value


class MergeChunksRequest(BaseModel):
    chunk_ids: list[ReviewIdentifier] = Field(default_factory=list, max_length=MAX_REVIEW_CHUNK_IDS)
    text: ReviewText | None = None
    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS)
    approval_override_reason: str | None = Field(default=None, max_length=1000)


class IndexRequest(BaseModel):
    target_type: str = Field(default="local-jsonl", max_length=MAX_SHORT_LABEL_CHARS)
    embedding_dimensions: int = Field(default=384, ge=1, le=4096)
    dry_run: bool = False
    collection_name: str | None = Field(default=None, max_length=MAX_SHORT_LABEL_CHARS)


class SecurityScanRequest(BaseModel):
    block_high_risk: bool = True


class RegulationLifecycleRequest(BaseModel):
    status: str = Field(min_length=1, max_length=32)
    reason: str = Field(default="", max_length=MAX_NOTE_CHARS)
def _repository(settings: Settings | None = None) -> JsonRepository:
    return JsonRepository(settings or get_settings())


def chunk_review_attention_reasons(chunk: Chunk) -> list[str]:
    return _service_chunk_review_attention_reasons(chunk)


def _optional_form_value(value):
    if isinstance(value, FieldInfo):
        return None
    return value


def _validate_regulation_dates(upload_metadata: dict[str, Any]) -> None:
    parsed: dict[str, date] = {}
    for field in ("revision_date", "effective_from", "effective_to", "repealed_at"):
        raw_value = str(upload_metadata.get(field) or "").strip()
        if not raw_value:
            continue
        try:
            parsed[field] = date.fromisoformat(raw_value)
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO date in YYYY-MM-DD format.") from exc
    effective_from = parsed.get("effective_from")
    if effective_from is None:
        return
    effective_to = parsed.get("effective_to")
    if effective_to is not None and effective_to < effective_from:
        raise ValueError("effective_to cannot be earlier than effective_from.")
    repealed_at = parsed.get("repealed_at")
    if repealed_at is not None and repealed_at < effective_from:
        raise ValueError("repealed_at cannot be earlier than effective_from.")


def _require_document_access(repository: JsonRepository, document_id: str, auth: AuthContext) -> Document:
    document = repository.get_document(document_id)
    if document is None or not resource_visible_to_tenant(document, auth.tenant_id):
        raise HTTPException(status_code=404, detail=f"Document not found for current tenant: document_id={document_id}")
    return document


def _has_review_chunk_access(auth: AuthContext) -> bool:
    return str(auth.role or "").strip().lower() in API_WRITE_ROLES


def _audit_document_read_exception(
    settings: Settings,
    auth: AuthContext,
    *,
    action: str,
    document_id: str,
    exc: HTTPException,
) -> None:
    audit_api_event(
        settings,
        auth,
        action=action,
        outcome="denied" if exc.status_code == 403 else "failure",
        status_code=exc.status_code,
        resource_type="document",
        document_id=document_id,
        detail=str(exc.detail),
    )


def _chunks_visible_to_auth(
    repository: JsonRepository,
    document_id: str,
    chunks: list[Chunk],
    auth: AuthContext,
) -> list[Chunk]:
    require_api_role(auth, API_READ_ROLES)
    if _has_review_chunk_access(auth):
        return chunks
    approval_journal_records = repository.list_approval_journal_records(document_id)
    return [
        chunk
        for chunk in chunks
        if chunk.approval_status == APPROVED_CHUNK_STATUS
        and _has_matching_approval_journal_record(
            approval_journal_records,
            chunk=chunk,
            document_id=document_id,
            auth=auth,
        )
    ]


def _filter_documents_for_tenant(documents: list[Document], auth: AuthContext) -> list[Document]:
    return [document for document in documents if resource_visible_to_tenant(document, auth.tenant_id)]


def _approval_worklist_evidence(request: ApprovalRequest) -> dict[str, str]:
    try:
        return _service_approval_worklist_evidence(
            worklist_report_path=request.worklist_report_path,
            worklist_report_sha256=request.worklist_report_sha256,
            review_batch_manifest_path=request.review_batch_manifest_path,
            review_batch_manifest_sha256=request.review_batch_manifest_sha256,
            review_batch_id=request.review_batch_id,
            review_batch_chunk_fingerprint=request.review_batch_chunk_fingerprint,
            review_strategy=request.review_strategy,
        )
    except ReviewWorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _normalize_evidence_artifact_path(value: str | None, *, field_name: str) -> str:
    try:
        return _service_normalize_evidence_artifact_path(value, field_name=field_name)
    except ReviewWorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _normalize_optional_sha256(value: str | None, *, field_name: str) -> str:
    try:
        return _service_normalize_optional_sha256(value, field_name=field_name)
    except ReviewWorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _normalize_evidence_identifier(value: str | None, *, field_name: str, max_length: int) -> str:
    try:
        return _service_normalize_evidence_identifier(value, field_name=field_name, max_length=max_length)
    except ReviewWorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _verify_approval_evidence(
    *,
    settings: Settings,
    auth: AuthContext,
    document_id: str,
    chunks: Sequence[Chunk],
    requested_ids: set[str],
    evidence: dict[str, str],
) -> None:
    try:
        _service_verify_approval_evidence(
            artifact_root=settings.artifact_root,
            runtime_data_dir=settings.data_dir,
            tenant_id=auth.tenant_id,
            document_id=document_id,
            chunks=chunks,
            requested_ids=requested_ids,
            evidence=evidence,
        )
    except ReviewWorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _clean_evidence_text(value: str | None, *, field_name: str, max_length: int) -> str:
    try:
        return _service_clean_evidence_text(value, field_name=field_name, max_length=max_length)
    except ReviewWorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _refresh_review_exports(settings: Settings, document_id: str, chunks: list[Chunk]) -> dict[str, str]:
    exporter = Exporter()
    file_store = FileStore(settings)
    exports = {
        "jsonl": exporter.to_jsonl(chunks),
        "csv": exporter.to_csv(chunks),
        "md": exporter.to_markdown(chunks),
        "tables.jsonl": exporter.to_tables_jsonl(chunks),
        "tables.csv": exporter.to_tables_csv(chunks),
    }
    artifacts: dict[str, str] = {}
    for extension, content in exports.items():
        path = file_store.export_path(document_id, extension)
        path.write_text(content, encoding="utf-8")
        artifacts[extension] = str(path)
    return artifacts


def _write_review_snapshot(settings: Settings, document_id: str, record_id: str, chunks: list[Chunk]) -> str:
    path = settings.data_dir / "repository" / "review_snapshots" / f"{tenant_storage_key(document_id)}.{record_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(chunk.model_dump(mode="json"), ensure_ascii=False) for chunk in chunks)
        + ("\n" if chunks else ""),
        encoding="utf-8",
    )
    return path.relative_to(settings.data_dir).as_posix()


def _load_review_chunks(repository: JsonRepository, document_id: str) -> list[Chunk]:
    chunks = repository.get_chunks(document_id)
    if not chunks:
        raise HTTPException(status_code=404, detail=f"Chunks not found for document: {document_id}")
    return chunks


def _require_chunk_ids(chunks: list[Chunk], chunk_ids: list[str]) -> set[str]:
    try:
        return _service_require_chunk_ids(chunks, chunk_ids)
    except ReviewWorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _validate_approval_preconditions(
    *,
    chunks: list[Chunk],
    chunk_ids: list[str],
    review_flags_acknowledged: bool,
    approval_override_reason: str | None = None,
):
    try:
        return _service_validate_approval_preconditions(
            chunks=chunks,
            chunk_ids=chunk_ids,
            review_flags_acknowledged=review_flags_acknowledged,
            approval_override_reason=approval_override_reason,
        )
    except ReviewWorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _clear_approval_fields() -> dict[str, str | None]:
    return {
        "approval_status": "needs_review",
        "approval_id": None,
        "approved_by": None,
        "approved_at": None,
        "approved_content_hash": None,
    }


def _review_chunk_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _review_retrieval_text(source: Chunk, text: str) -> str:
    header = source.retrieval_text or source.normalized_text or source.text
    if header and source.text in header:
        return header.replace(source.text, text, 1)
    return text


def _normalize_security_level(value: str | None) -> str:
    try:
        return _service_normalize_security_level(value)
    except ReviewWorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _safe_target_type(target_type: str) -> str:
    normalized = str(target_type or "local-jsonl").strip().lower()
    allowed = {"local-jsonl", "qdrant-local-jsonl", "pgvector-local-jsonl", "chroma-local-jsonl", "qdrant-rest-manifest"}
    if normalized not in allowed:
        allowed_label = ", ".join(sorted(allowed))
        raise HTTPException(status_code=400, detail=f"Unsupported index target_type. Allowed: {allowed_label}.")
    return normalized


def _safe_secure_rag_target_type(target_type: str) -> str:
    normalized = _safe_target_type(target_type)
    if normalized != "local-jsonl":
        raise HTTPException(status_code=400, detail="Secure RAG indexing API currently supports local-jsonl only.")
    return normalized


def _vector_artifact_dir(settings: Settings, document_id: str) -> Path:
    safe_document_id = tenant_storage_key(document_id)
    return settings.data_dir / "vector_ingestion" / safe_document_id


def _default_vector_target_path(settings: Settings, auth: AuthContext, target_type: str) -> Path:
    tenant_key = tenant_storage_key(auth.tenant_id)
    filename_by_type = {
        "local-jsonl": "approved_vectors.jsonl",
        "qdrant-local-jsonl": "approved_qdrant_points.jsonl",
        "pgvector-local-jsonl": "approved_pgvector_rows.jsonl",
        "chroma-local-jsonl": "approved_chroma_rows.jsonl",
        "qdrant-rest-manifest": "qdrant_rest_manifest.json",
    }
    return settings.data_dir / "vector_db" / tenant_key / filename_by_type[target_type]


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + ("\n" if records else ""),
        encoding="utf-8",
    )


def _public_upsert_summary(result: dict) -> dict:
    hidden = {"target_path", "local_path_leak_samples"}
    return {key: value for key, value in result.items() if key not in hidden}


def _load_jsonl_dicts(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _stored_vector_metadata(row: dict) -> dict:
    if isinstance(row.get("metadata"), dict):
        return row["metadata"]
    if isinstance(row.get("payload"), dict):
        return row["payload"]
    return {}


def _stored_vector_document_id(row: dict) -> str:
    metadata = _stored_vector_metadata(row)
    return str(row.get("document_id") or metadata.get("document_id") or "")


def _vector_consistency_summary(
    *,
    settings: Settings,
    auth: AuthContext,
    document_id: str,
    target_type: str,
    current_records: list[dict],
) -> dict:
    if target_type == "qdrant-rest-manifest":
        return {"checked": False, "reason": "manifest_target", "stale_count": 0, "samples": []}
    target_path = _default_vector_target_path(settings, auth, _safe_target_type(target_type))
    stored_rows = [row for row in _load_jsonl_dicts(target_path) if _stored_vector_document_id(row) == document_id]
    current_by_id = {str(record.get("id") or ""): record for record in current_records}
    stored_by_id = {str(row.get("id") or ""): row for row in stored_rows}
    stale: list[dict[str, str]] = []
    for record_id, record in current_by_id.items():
        stored = stored_by_id.get(record_id)
        if stored is None:
            stale.append({"id": record_id, "reason": "missing_stored_vector"})
            continue
        expected_metadata = record.get("metadata") or {}
        stored_metadata = _stored_vector_metadata(stored)
        for field in ("tenant_id", "approval_status", "approval_id", "security_level", "approved_content_hash"):
            if str(expected_metadata.get(field) or "") != str(stored_metadata.get(field) or ""):
                stale.append({"id": record_id, "reason": f"{field}_mismatch"})
                break
        else:
            if _department_acl_set(expected_metadata.get("department_acl")) != _department_acl_set(
                stored_metadata.get("department_acl")
            ):
                stale.append({"id": record_id, "reason": "department_acl_mismatch"})
            elif stable_content_hash(str(stored.get("text") or ""), stored_metadata) != str(
                stored.get("content_hash") or stored_metadata.get("content_hash") or ""
            ):
                stale.append({"id": record_id, "reason": "tampered_stored_vector"})
            elif embedded_vector_integrity_reason(stored):
                stale.append({"id": record_id, "reason": embedded_vector_integrity_reason(stored)})
            elif str(record.get("content_hash") or "") != str(
                stored.get("content_hash") or stored_metadata.get("content_hash") or ""
            ):
                stale.append({"id": record_id, "reason": "content_hash_mismatch"})
    for record_id in sorted(set(stored_by_id) - set(current_by_id)):
        stale.append({"id": record_id, "reason": "extra_stored_vector"})
    return {
        "checked": True,
        "target_type": target_type,
        "target_path_configured": target_path.is_file(),
        "stored_record_count": len(stored_rows),
        "current_record_count": len(current_records),
        "stale_count": len(stale),
        "samples": stale[:20],
    }


def _chunks_for_indexing(chunks: list[Chunk], document: Document, auth: AuthContext) -> list[dict]:
    tenant_id = document.tenant_id or auth.tenant_id
    prepared: list[dict] = []
    invalid_approved_chunks: list[str] = []
    missing_approval_hash_chunks: list[str] = []
    approval_provenance_issue_chunks: list[str] = []
    for chunk in chunks:
        chunk_data = chunk.model_dump(mode="json")
        metadata = dict(chunk_data.get("metadata") or {})
        for key, value in {
            "institution_name": document.institution_name,
            "apba_id": document.apba_id,
            "source_system": document.source_system,
            "source_url": document.source_url,
            "source_record_id": document.source_record_id,
            "source_file_id": document.source_file_id,
            "source_disclosure_date": document.source_disclosure_date,
            "source_posted_date": document.source_posted_date,
            "profile_id": document.profile_id,
        }.items():
            if value and not metadata.get(key):
                metadata[key] = value
        metadata["tenant_id"] = tenant_id
        chunk_data["tenant_id"] = tenant_id
        chunk_data["department_acl"] = _department_acl_set(chunk.department_acl)
        chunk_data["metadata"] = metadata
        if chunk.approval_status == APPROVED_CHUNK_STATUS:
            try:
                chunk_data["security_level"] = _normalize_security_level(chunk.security_level)
            except HTTPException:
                invalid_approved_chunks.append(chunk.chunk_id)
            if not str(chunk.approved_content_hash or "").strip():
                missing_approval_hash_chunks.append(chunk.chunk_id)
            provenance_issues = approval_provenance_issue_fields(chunk_data)
            if provenance_issues:
                approval_provenance_issue_chunks.append(f"{chunk.chunk_id}:{','.join(provenance_issues)}")
        prepared.append(chunk_data)
    if invalid_approved_chunks:
        sample = ", ".join(sorted(invalid_approved_chunks)[:20])
        raise HTTPException(status_code=400, detail=f"Approved chunks are missing valid security_level: {sample}")
    if missing_approval_hash_chunks:
        sample = ", ".join(sorted(missing_approval_hash_chunks)[:20])
        raise HTTPException(status_code=400, detail=f"Approved chunks are missing approved_content_hash: {sample}")
    if approval_provenance_issue_chunks:
        sample = ", ".join(sorted(approval_provenance_issue_chunks)[:20])
        raise HTTPException(status_code=400, detail=f"Approved chunks are missing approval provenance: {sample}")
    return prepared


def _require_approval_journal_records(
    repository: JsonRepository,
    *,
    document_id: str,
    chunks: list[Chunk],
    auth: AuthContext,
) -> None:
    approval_journal_records = repository.list_approval_journal_records(document_id)
    missing: list[str] = []
    for chunk in chunks:
        if chunk.approval_status != APPROVED_CHUNK_STATUS:
            continue
        if not _has_matching_approval_journal_record(approval_journal_records, chunk=chunk, document_id=document_id, auth=auth):
            missing.append(chunk.chunk_id)
    if missing:
        sample = ", ".join(sorted(missing)[:20])
        raise HTTPException(status_code=400, detail=f"Approved chunks are missing approval journal records: {sample}")


def _has_matching_approval_journal_record(
    records: list[dict],
    *,
    chunk: Chunk,
    document_id: str,
    auth: AuthContext,
) -> bool:
    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("document_id") or "") != document_id:
            continue
        if str(record.get("tenant_id") or "") != auth.tenant_id:
            continue
        if str(record.get("approval_id") or "") != str(chunk.approval_id or ""):
            continue
        if chunk.chunk_id not in {str(value) for value in (record.get("chunk_ids") or [])}:
            continue
        if _approval_record_chunk_hash(record, chunk.chunk_id) != str(chunk.approved_content_hash or ""):
            continue
        worklist_evidence = record.get("worklist_evidence") or {}
        if not isinstance(worklist_evidence, dict):
            continue
        evidence_metadata = _approval_worklist_metadata(worklist_evidence)
        if set(evidence_metadata) != set(APPROVAL_WORKLIST_METADATA_KEYS):
            continue
        if any(str(chunk.metadata.get(key) or "") != str(value or "") for key, value in evidence_metadata.items()):
            continue
        return True
    return False


def _approval_record_chunk_hash(record: dict, chunk_id: str) -> str:
    approved_hashes = record.get("approved_content_hashes")
    if isinstance(approved_hashes, dict):
        value = approved_hashes.get(chunk_id)
        if value:
            return str(value)
    for item in record.get("approved_chunks") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("chunk_id") or "") == chunk_id and item.get("approved_content_hash"):
            return str(item.get("approved_content_hash") or "")
    return ""


def _run_security_scan_record(
    *,
    settings: Settings,
    repository: JsonRepository,
    document_id: str,
    auth: AuthContext,
    chunks: list[Chunk],
    block_high_risk: bool,
    chunk_ids: set[str] | None = None,
    scan_reason: str = "manual",
) -> tuple[list[Chunk], dict]:
    scan_update = _prepare_security_scan_update(
        chunks=chunks,
        block_high_risk=block_high_risk,
        chunk_ids=chunk_ids,
    )
    updated_chunks = scan_update.updated_chunks
    if scan_update.blocked_chunk_ids:
        repository.save_chunks(document_id, updated_chunks)
        _refresh_review_exports(settings, document_id, updated_chunks)
    vector_sync = (
        _sync_vector_index_after_review_change(
            settings=settings,
            repository=repository,
            document_id=document_id,
            auth=auth,
            chunks=updated_chunks,
            action="review_vector_sync",
        )
        if scan_update.blocked_chunk_ids
        else {"status": "skipped", "reason": "no_blocked_chunks"}
    )
    scan_id = f"security_scan_{uuid.uuid4().hex[:12]}"
    record = _build_security_scan_record(
        update=scan_update,
        scan_id=scan_id,
        document_id=document_id,
        tenant_id=auth.tenant_id,
        created_at=datetime.now(timezone.utc).isoformat(),
        scanned_by=auth.actor,
        scan_reason=scan_reason,
        vector_sync=vector_sync,
    )
    repository.append_security_scan_record(record)
    return updated_chunks, record


def _run_document_indexing(
    *,
    settings: Settings,
    repository: JsonRepository,
    document_id: str,
    auth: AuthContext,
    request: IndexRequest,
    action: str,
    chunks: list[Chunk] | None = None,
) -> dict:
    document = repository.get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"Document not found: {document_id}")
    chunks = chunks if chunks is not None else _load_review_chunks(repository, document_id)
    target_type = _safe_secure_rag_target_type(request.target_type)
    prepared_chunks = _chunks_for_indexing(chunks, document, auth)
    _require_approval_journal_records(repository, document_id=document_id, chunks=chunks, auth=auth)
    records, vector_summary = build_vector_records(prepared_chunks)
    validate_vector_record_tenant_scope(records, expected_tenant_id=auth.tenant_id)
    if not records and action not in {"reindex", "review_vector_sync"}:
        raise HTTPException(status_code=400, detail="No approved chunks are available for indexing.")

    embedded_records, embedding_summary = embed_vector_records(
        records,
        dimensions=request.embedding_dimensions,
        model=LOCAL_HASH_EMBEDDING_MODEL,
    )
    target_path = _default_vector_target_path(settings, auth, target_type)
    validate_vector_target_tenant_scope(target_type, target_path, expected_tenant_id=auth.tenant_id)
    target = vector_upsert_target(target_type, target_path=target_path, collection_name=request.collection_name)
    upsert_summary = target.upsert(embedded_records, dry_run=request.dry_run, fail_on_leak=True, document_id=document_id)

    artifact_dir = _vector_artifact_dir(settings, document_id)
    records_jsonl = artifact_dir / "vector_records.jsonl"
    embedded_jsonl = artifact_dir / "embedded_records.jsonl"
    _write_jsonl(records_jsonl, records)
    _write_jsonl(embedded_jsonl, embedded_records)

    timestamp = datetime.now(timezone.utc).isoformat()
    indexing_job = {
        "indexing_job_id": f"index_{uuid.uuid4().hex[:12]}",
        "document_id": document_id,
        "tenant_id": auth.tenant_id,
        "action": action,
        "status": "not_indexed" if request.dry_run else "indexed",
        "created_at": timestamp,
        "completed_at": timestamp,
        "requested_by": auth.actor,
        "target_type": target_type,
        "collection_name": request.collection_name or "",
        "dry_run": request.dry_run,
        "record_count": len(records),
        "embedding_model": LOCAL_HASH_EMBEDDING_MODEL,
        "embedding_dimensions": request.embedding_dimensions,
        "vector_summary": vector_summary,
        "embedding_summary": embedding_summary,
        "upsert_summary": _public_upsert_summary(upsert_summary),
        "artifacts": {
            "vector_records_jsonl": records_jsonl.name,
            "embedded_records_jsonl": embedded_jsonl.name,
        },
    }
    repository.append_indexing_job(indexing_job)
    return indexing_job


def _automatically_supersede_prior_version(
    *,
    settings: Settings,
    repository: JsonRepository,
    document: Document,
    auth: AuthContext,
) -> dict[str, Any] | None:
    prior_document_id = str(document.supersedes_document_id or "").strip()
    if not prior_document_id:
        return None

    def deferred_event(status: str, *, effective_from: str | None = None) -> dict[str, Any]:
        event = {
            "event_id": f"regulation_auto_supersede_deferred_{document.document_id}_{int(datetime.now(timezone.utc).timestamp() * 1000000)}",
            "event_type": "regulation_auto_supersede_deferred",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "document_id": prior_document_id,
            "new_document_id": document.document_id,
            "tenant_id": auth.tenant_id,
            "profile_id": document.profile_id,
            "regulation_id": document.regulation_id,
            "status": status,
            "effective_from": effective_from,
            "outcome": "deferred",
            "reason": "Prior version remains current until the revision is effective and complete.",
            "actor": auth.actor,
        }
        repository.append_maintenance_event(event)
        audit_api_event(
            settings,
            auth,
            action="document.regulation.auto_supersede",
            outcome="deferred",
            status_code=200,
            resource_type="document",
            document_id=prior_document_id,
            detail=f"new_document_id={document.document_id} status={status}",
        )
        return event

    effective_from_text = str(
        document.effective_from
        or document.revision_date
        or document.created_at.date().isoformat()
    ).strip()
    if not effective_from_text:
        return deferred_event("deferred_missing_effective_from")
    try:
        effective_from = date.fromisoformat(effective_from_text)
    except ValueError as exc:
        raise ValueError("An approved revision requires a valid effective_from date.") from exc
    if effective_from > date.today():
        return deferred_event(
            "deferred_until_effective_from",
            effective_from=effective_from.isoformat(),
        )
    prior_document = repository.get_document(prior_document_id)
    if prior_document is None:
        raise ValueError("The approved revision points to a missing prior regulation document.")
    if not resource_visible_to_tenant(prior_document, auth.tenant_id):
        raise ValueError("The prior regulation document is outside the current tenant scope.")
    if str(prior_document.profile_id or "").casefold() != str(document.profile_id or "").casefold():
        raise ValueError("The prior regulation document belongs to a different institution profile.")
    if str(prior_document.regulation_id or "").casefold() != str(document.regulation_id or "").casefold():
        raise ValueError("The prior regulation document belongs to a different regulation family.")
    prior_effective_from_text = str(
        prior_document.effective_from
        or prior_document.revision_date
        or prior_document.created_at.date().isoformat()
    ).strip()
    try:
        prior_effective_from = date.fromisoformat(prior_effective_from_text)
    except ValueError as exc:
        raise ValueError("The prior regulation document has an invalid effective_from date.") from exc
    if effective_from < prior_effective_from:
        return deferred_event(
            "deferred_nonsequential_effective_date",
            effective_from=effective_from.isoformat(),
        )
    if str(prior_document.regulation_status or "").strip().casefold() == "superseded":
        return {
            "document_id": prior_document.document_id,
            "status": "already_superseded",
        }
    prior_for_transition = prior_document.model_copy(
        update={"effective_from": prior_document.effective_from or prior_effective_from.isoformat()}
    )
    updated_prior, event = apply_regulation_transition(
        prior_for_transition,
        "superseded",
        reason=f"Automatically superseded by approved revision {document.document_id}.",
        actor=auth.actor,
    )
    updated_prior = updated_prior.model_copy(
        update={
            "effective_from": prior_for_transition.effective_from,
            "effective_to": effective_from.isoformat(),
        }
    )
    event["effective_to"] = updated_prior.effective_to
    prior_chunks = repository.get_chunks(prior_document_id)
    for chunk in prior_chunks:
        chunk.metadata = {
            **dict(chunk.metadata or {}),
            "regulation_status": updated_prior.regulation_status,
            "effective_to": updated_prior.effective_to,
        }
    repository.save_chunks(prior_document_id, prior_chunks)
    repository.upsert_document(updated_prior)
    try:
        vector_sync = _sync_vector_index_after_review_change(
            settings=settings,
            repository=repository,
            document_id=prior_document_id,
            auth=auth,
            chunks=prior_chunks,
            action="automatic_supersede_vector_sync",
        )
    except Exception as exc:
        vector_sync = _failed_vector_sync_payload(exc)
    event["vector_sync"] = vector_sync
    event["outcome"] = "completed" if vector_sync.get("status") != "failed" else "failed_reindex_required"
    repository.append_maintenance_event(event)
    audit_outcome = "failure" if event["outcome"] == "failed_reindex_required" else "success"
    audit_api_event(
        settings,
        auth,
        action="document.regulation.auto_supersede",
        outcome=audit_outcome,
        status_code=500 if event["outcome"] == "failed_reindex_required" else 200,
        resource_type="document",
        document_id=prior_document_id,
        detail=f"superseded_by={document.document_id}",
    )
    if vector_sync.get("status") == "failed":
        raise RuntimeError(
            f"Prior regulation vector synchronization failed for document_id={prior_document_id}."
        )
    return event


def _sync_vector_index_after_review_change(
    *,
    settings: Settings,
    repository: JsonRepository,
    document_id: str,
    auth: AuthContext,
    chunks: list[Chunk],
    action: str,
) -> dict:
    latest_job = _latest_indexed_job(repository, document_id)
    if latest_job is None:
        return {"status": "skipped", "reason": "no_prior_indexed_job"}
    request = IndexRequest(
        target_type=str(latest_job.get("target_type") or "local-jsonl"),
        embedding_dimensions=_indexed_job_embedding_dimensions(latest_job),
        collection_name=str(latest_job.get("collection_name") or "") or None,
    )
    return _run_document_indexing(
        settings=settings,
        repository=repository,
        document_id=document_id,
        auth=auth,
        request=request,
        action=action,
        chunks=chunks,
    )


def _append_approval_vector_sync_outcome(
    *,
    repository: JsonRepository,
    document_id: str,
    auth: AuthContext,
    approval_record: dict[str, Any],
    vector_sync: dict[str, Any],
    outcome: str,
) -> dict[str, Any]:
    event_id = str(approval_record.get("vector_sync_event_id") or "").strip()
    if not event_id:
        event_id = f"approval_vector_sync_{uuid.uuid4().hex[:12]}"
    event = {
        "event_id": event_id,
        "event_type": "approval_vector_sync_outcome",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_action": "document.review.approve",
        "sync_action": "review_vector_sync",
        "document_id": document_id,
        "tenant_id": auth.tenant_id,
        "actor": auth.actor,
        "approval_record_id": str(approval_record.get("approval_record_id") or ""),
        "approval_id": str(approval_record.get("approval_id") or ""),
        "chunk_ids": sorted(str(chunk_id) for chunk_id in (approval_record.get("chunk_ids") or [])),
        "approved_content_hashes": dict(approval_record.get("approved_content_hashes") or {}),
        "outcome": outcome,
        "approval_persisted": True,
        "vector_sync": dict(vector_sync),
    }
    repository.append_maintenance_event(event)
    return event


def _failed_vector_sync_payload(exc: Exception) -> dict[str, Any]:
    detail = redact_sensitive_paths(str(exc).strip())
    return {
        "status": "failed",
        "reason": "vector_sync_exception",
        "exception_type": type(exc).__name__,
        "detail": detail[:1000],
        "reindex_required": True,
    }


def _latest_indexed_job(repository: JsonRepository, document_id: str) -> dict | None:
    for job in reversed(repository.list_indexing_jobs(document_id)):
        if str(job.get("status") or "") == "indexed" and not bool(job.get("dry_run")):
            return job
    return None


def _indexed_job_embedding_dimensions(job: dict) -> int:
    try:
        dimensions = int(job.get("embedding_dimensions") or 384)
    except (TypeError, ValueError):
        dimensions = 384
    return max(1, min(4096, dimensions))


@router.post("")
async def upload_document(
    file: UploadFile = File(...),
    document_name: str | None = Form(default=None),
    institution_name: str | None = Form(default=None),
    apba_id: str | None = Form(default=None),
    source_system: str | None = Form(default=None),
    source_url: str | None = Form(default=None),
    source_record_id: str | None = Form(default=None),
    source_file_id: str | None = Form(default=None),
    source_disclosure_date: str | None = Form(default=None),
    source_posted_date: str | None = Form(default=None),
    profile_id: str | None = Form(default=None),
    regulation_id: str | None = Form(default=None),
    regulation_version: str | None = Form(default=None),
    revision_date: str | None = Form(default=None),
    effective_from: str | None = Form(default=None),
    effective_to: str | None = Form(default=None),
    repealed_at: str | None = Form(default=None),
    regulation_status: str | None = Form(default="draft"),
    supersedes_document_id: str | None = Form(default=None),
    auth_context: AuthContext = Depends(get_auth_context),
):
    settings = get_settings()
    auth = coerce_auth_context(auth_context)
    request_settings = settings_for_tenant(settings, auth.tenant_id)
    try:
        require_api_role(auth, API_WRITE_ROLES)
    except HTTPException as exc:
        audit_api_event(
            request_settings,
            auth,
            action="document.upload",
            outcome="denied",
            status_code=exc.status_code,
            resource_type="document",
            filename=file.filename or "document",
            detail=str(exc.detail),
        )
        raise
    upload_metadata = {
        "document_name": _optional_form_value(document_name),
        "institution_name": _optional_form_value(institution_name),
        "apba_id": _optional_form_value(apba_id),
        "source_system": _optional_form_value(source_system),
        "source_url": _optional_form_value(source_url),
        "source_record_id": _optional_form_value(source_record_id),
        "source_file_id": _optional_form_value(source_file_id),
        "source_disclosure_date": _optional_form_value(source_disclosure_date),
        "source_posted_date": _optional_form_value(source_posted_date),
        "profile_id": _optional_form_value(profile_id),
        "regulation_id": _optional_form_value(regulation_id),
        "regulation_version": _optional_form_value(regulation_version),
        "revision_date": _optional_form_value(revision_date),
        "effective_from": _optional_form_value(effective_from),
        "effective_to": _optional_form_value(effective_to),
        "repealed_at": _optional_form_value(repealed_at),
        "regulation_status": _optional_form_value(regulation_status) or "draft",
        "supersedes_document_id": _optional_form_value(supersedes_document_id),
    }
    try:
        _validate_regulation_dates(upload_metadata)
        upload_settings = request_settings
        if settings.institution_profiles_path:
            registry = load_institution_profile_registry(settings.institution_profiles_path)
            upload_metadata = apply_institution_profile_to_metadata(
                upload_metadata,
                registry,
                strict=settings.institution_profiles_strict,
                enforce_required=settings.institution_profiles_strict,
            )
            profile = registry.resolve(upload_metadata.get("profile_id"), strict=settings.institution_profiles_strict)
            if profile is not None and profile.tenant_id and profile.tenant_id != auth.tenant_id:
                raise ValueError("The selected institution profile is not assigned to the current tenant.")
            if profile is not None and profile.max_upload_mb:
                upload_settings = replace(request_settings, max_upload_mb=profile.max_upload_mb)
        supersedes_id = str(upload_metadata.get("supersedes_document_id") or "").strip()
        regulation_id_value = str(upload_metadata.get("regulation_id") or "").strip()
        regulation_version_value = str(upload_metadata.get("regulation_version") or "").strip()
        if regulation_id_value and not str(upload_metadata.get("profile_id") or "").strip():
            raise ValueError("A regulation family must be assigned to an institution profile.")
        if regulation_version_value and not regulation_id_value:
            raise ValueError("A regulation version requires a regulation family identifier.")
        if regulation_id_value and regulation_version_value:
            existing_versions = JsonRepository(request_settings).find_documents_by_regulation(
                regulation_id_value,
                profile_id=upload_metadata.get("profile_id"),
                tenant_id=auth.tenant_id,
            )
            if any(
                str(getattr(existing, "regulation_version", "") or "").strip().casefold()
                == regulation_version_value.casefold()
                for existing in existing_versions
            ):
                raise ValueError(
                    "The same regulation version already exists for the selected institution. "
                    "Register a new version instead of overwriting the existing document."
                )
        if supersedes_id:
            if not upload_metadata.get("profile_id") or not upload_metadata.get("regulation_id"):
                raise ValueError("A revision upload requires profile_id and regulation_id.")
            previous_document = JsonRepository(request_settings).get_document(supersedes_id)
            if previous_document is None:
                raise ValueError("The superseded document is not available for the current tenant.")
            if str(previous_document.tenant_id or "").strip() != str(auth.tenant_id or "").strip():
                raise ValueError("A revision cannot link to a document from another tenant.")
            if str(previous_document.profile_id or "").casefold() != str(upload_metadata.get("profile_id") or "").casefold():
                raise ValueError("A revision must remain within the same institution profile.")
            if str(previous_document.regulation_id or "").casefold() != str(upload_metadata.get("regulation_id") or "").casefold():
                raise ValueError("A revision must remain within the same regulation family.")
            if str(previous_document.regulation_status or "").strip().casefold() != "approved":
                raise ValueError("A revision must supersede an approved prior regulation version.")
        await file.seek(0)
        service = DocumentService(settings=upload_settings, repository=JsonRepository(upload_settings))
        document = service.upload_stream(
            file.filename or "document",
            file.file,
            document_name=upload_metadata.get("document_name"),
            institution_name=upload_metadata.get("institution_name"),
            apba_id=upload_metadata.get("apba_id"),
            source_system=upload_metadata.get("source_system"),
            source_url=upload_metadata.get("source_url"),
            source_record_id=upload_metadata.get("source_record_id"),
            source_file_id=upload_metadata.get("source_file_id"),
            source_disclosure_date=upload_metadata.get("source_disclosure_date"),
            source_posted_date=upload_metadata.get("source_posted_date"),
            profile_id=upload_metadata.get("profile_id"),
            regulation_id=upload_metadata.get("regulation_id"),
            regulation_version=upload_metadata.get("regulation_version"),
            revision_date=upload_metadata.get("revision_date"),
            effective_from=upload_metadata.get("effective_from"),
            effective_to=upload_metadata.get("effective_to"),
            repealed_at=upload_metadata.get("repealed_at"),
            regulation_status=upload_metadata.get("regulation_status") or "draft",
            supersedes_document_id=upload_metadata.get("supersedes_document_id"),
            tenant_id=auth.tenant_id,
        )
        audit_api_event(
            upload_settings,
            auth,
            action="document.upload",
            outcome="success",
            status_code=200,
            resource_type="document",
            document_id=document.document_id,
            filename=document.filename,
            source_system=document.source_system or "",
            source_record_id=document.source_record_id or "",
            source_file_id=document.source_file_id or "",
        )
        return document.model_dump(mode="json")
    except ValueError as exc:
        safe_detail = _safe_expected_error_detail(exc)
        audit_api_event(
            request_settings,
            auth,
            action="document.upload",
            outcome="failure",
            status_code=400,
            resource_type="document",
            filename=file.filename or "document",
            source_system=str(upload_metadata.get("source_system") or ""),
            source_record_id=str(upload_metadata.get("source_record_id") or ""),
            source_file_id=str(upload_metadata.get("source_file_id") or ""),
            detail=safe_detail,
        )
        raise HTTPException(status_code=400, detail=safe_detail) from exc


@router.get("")
def list_documents(auth_context: AuthContext = Depends(get_auth_context)):
    auth = coerce_auth_context(auth_context)
    settings = settings_for_tenant(get_settings(), auth.tenant_id)
    documents = DocumentService(settings=settings, repository=_repository(settings)).list()
    return [document.model_dump(mode="json") for document in _filter_documents_for_tenant(documents, auth)]


@router.get("/{document_id}/versions")
def list_document_versions(
    document_id: str,
    as_of_date: str | None = Query(default=None, max_length=20),
    auth_context: AuthContext = Depends(get_auth_context),
):
    """Return the version history for one institution-scoped regulation."""
    auth = coerce_auth_context(auth_context)
    settings = settings_for_tenant(get_settings(), auth.tenant_id)
    repository = _repository(settings)
    document = _require_document_access(repository, document_id, auth)
    if not document.regulation_id:
        versions = [document]
    else:
        versions = repository.find_documents_by_regulation(
            document.regulation_id,
            profile_id=document.profile_id,
            tenant_id=auth.tenant_id,
        )
    reference_date = date.today()
    if as_of_date and as_of_date.strip():
        try:
            reference_date = date.fromisoformat(as_of_date.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="as_of_date must be an ISO date in YYYY-MM-DD format.") from exc
    current_document = latest_history_version(
        versions,
        as_of=reference_date,
    )
    current_document_id = current_document.document_id if current_document else None
    version_document_ids = {item.document_id for item in versions}
    lifecycle_events = [
        event
        for event in repository.list_maintenance_events(event_type="regulation_lifecycle_transition")
        if str(event.get("document_id") or "") in version_document_ids
        and str(event.get("tenant_id") or auth.tenant_id or "").casefold()
        == str(auth.tenant_id or "").casefold()
    ]
    return {
        "regulation_id": document.regulation_id,
        "profile_id": document.profile_id,
        "as_of_date": reference_date.isoformat(),
        "current_document_id": current_document_id,
        "lifecycle_events": lifecycle_events,
        "versions": [
            {
                **item.model_dump(mode="json"),
                "is_current": item.document_id == current_document_id,
            }
            for item in versions
        ],
    }


@router.patch("/{document_id}/regulation-status")
def transition_regulation_status(
    document_id: str,
    request: RegulationLifecycleRequest,
    auth_context: AuthContext = Depends(get_auth_context),
):
    """Apply an audited manual lifecycle transition to one regulation version."""
    settings = get_settings()
    auth = coerce_auth_context(auth_context)
    request_settings = settings_for_tenant(settings, auth.tenant_id)
    repository = _repository(request_settings)
    try:
        require_api_role(auth, {API_ROLE_ADMIN})
        document = _require_document_access(repository, document_id, auth)
        updated_document, event = apply_regulation_transition(
            document,
            request.status,
            reason=request.reason,
            actor=auth.actor,
        )
        chunks = repository.get_chunks(document_id)
        for chunk in chunks:
            chunk.metadata = {
                **dict(chunk.metadata or {}),
                "regulation_status": updated_document.regulation_status,
            }
        repository.save_chunks(document_id, chunks)
        repository.upsert_document(updated_document)
        try:
            vector_sync = _sync_vector_index_after_review_change(
                settings=request_settings,
                repository=repository,
                document_id=document_id,
                auth=auth,
                chunks=chunks,
                action="regulation_lifecycle_vector_sync",
            )
        except Exception as exc:
            vector_sync = _failed_vector_sync_payload(exc)
        event["vector_sync"] = vector_sync
        event["outcome"] = "completed" if vector_sync.get("status") != "failed" else "failed_reindex_required"
        repository.append_maintenance_event(event)
        audit_api_event(
            request_settings,
            auth,
            action="document.regulation.lifecycle",
            outcome=event["outcome"],
            status_code=500 if event["outcome"] == "failed_reindex_required" else 200,
            resource_type="document",
            document_id=document_id,
            detail=f"{document.regulation_status}->{updated_document.regulation_status}",
        )
        if vector_sync.get("status") == "failed":
            raise HTTPException(
                status_code=500,
                detail={
                    "message": "Regulation lifecycle state was saved, but vector index synchronization failed.",
                    "document_id": document_id,
                    "lifecycle_event": event,
                },
            )
        return {
            "document": updated_document.model_dump(mode="json"),
            "lifecycle_event": event,
        }
    except RegulationLifecycleError as exc:
        audit_api_event(
            request_settings,
            auth,
            action="document.regulation.lifecycle",
            outcome="failure",
            status_code=400,
            resource_type="document",
            document_id=document_id,
            detail=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/{document_id}/process",
    summary="Synchronously process an uploaded document",
    description=(
        "Runs parsing, chunking, validation, quality evaluation, and export generation inline before returning. "
        "The returned job describes the completed or failed processing attempt; this endpoint does not enqueue "
        "background work."
    ),
)
def process_document(
    document_id: str,
    request: ProcessRequest | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
):
    settings = get_settings()
    auth = coerce_auth_context(auth_context)
    request_settings = settings_for_tenant(settings, auth.tenant_id)
    try:
        require_api_role(auth, API_WRITE_ROLES)
        repository = _repository(request_settings)
        _require_document_access(repository, document_id, auth)
        options = request.parser_options if request else None
        job = ProcessingService(settings=request_settings, repository=repository).process(document_id, options)
        audit_api_event(
            request_settings,
            auth,
            action="document.process",
            outcome="success",
            status_code=200,
            resource_type="document",
            document_id=document_id,
            job_id=job.job_id,
        )
        return job.model_dump(mode="json")
    except HTTPException as exc:
        audit_api_event(
            request_settings,
            auth,
            action="document.process",
            outcome="denied" if exc.status_code == 403 else "failure",
            status_code=exc.status_code,
            resource_type="document",
            document_id=document_id,
            detail=str(exc.detail),
        )
        raise
    except KeyError as exc:
        safe_detail = _safe_expected_error_detail(exc)
        audit_api_event(
            request_settings,
            auth,
            action="document.process",
            outcome="failure",
            status_code=404,
            resource_type="document",
            document_id=document_id,
            detail=safe_detail,
        )
        raise HTTPException(status_code=404, detail=safe_detail) from exc
    except ParserError as exc:
        safe_detail = _safe_expected_error_detail(exc)
        audit_api_event(
            request_settings,
            auth,
            action="document.process",
            outcome="failure",
            status_code=400,
            resource_type="document",
            document_id=document_id,
            detail=safe_detail,
        )
        raise HTTPException(status_code=400, detail=safe_detail) from exc
    except Exception as exc:
        audit_api_event(
            request_settings,
            auth,
            action="document.process",
            outcome="failure",
            status_code=500,
            resource_type="document",
            document_id=document_id,
            detail=str(exc),
        )
        raise HTTPException(status_code=500, detail=_PROCESSING_FAILURE_DETAIL) from exc


@router.get("/{document_id}/chunks")
def get_chunks(
    document_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int | None = Query(default=None, ge=1, le=5000),
    auth_context: AuthContext = Depends(get_auth_context),
):
    auth = coerce_auth_context(auth_context)
    request_settings = settings_for_tenant(get_settings(), auth.tenant_id)
    repository = _repository(request_settings)
    try:
        _require_document_access(repository, document_id, auth)
        chunks = _chunks_visible_to_auth(repository, document_id, repository.get_chunks(document_id), auth)
        window = chunks[offset : offset + limit] if limit is not None else chunks[offset:]
        result = [chunk.model_dump(mode="json") for chunk in window]
        audit_api_event(
            request_settings,
            auth,
            action="document.read.chunks",
            outcome="success",
            status_code=200,
            resource_type="document",
            document_id=document_id,
            detail=f"count={len(result)} offset={offset}",
        )
        return result
    except HTTPException as exc:
        _audit_document_read_exception(
            request_settings,
            auth,
            action="document.read.chunks",
            document_id=document_id,
            exc=exc,
        )
        raise


@router.get("/{document_id}/issues")
def get_issues(document_id: str, auth_context: AuthContext = Depends(get_auth_context)):
    auth = coerce_auth_context(auth_context)
    request_settings = settings_for_tenant(get_settings(), auth.tenant_id)
    repository = _repository(request_settings)
    try:
        require_api_role(auth, API_WRITE_ROLES)
        _require_document_access(repository, document_id, auth)
        result = [issue.model_dump(mode="json") for issue in repository.get_issues(document_id)]
        audit_api_event(
            request_settings,
            auth,
            action="document.read.issues",
            outcome="success",
            status_code=200,
            resource_type="document",
            document_id=document_id,
            detail=f"count={len(result)}",
        )
        return result
    except HTTPException as exc:
        _audit_document_read_exception(
            request_settings,
            auth,
            action="document.read.issues",
            document_id=document_id,
            exc=exc,
        )
        raise


@router.get("/{document_id}/quality")
def get_quality(document_id: str, auth_context: AuthContext = Depends(get_auth_context)):
    auth = coerce_auth_context(auth_context)
    request_settings = settings_for_tenant(get_settings(), auth.tenant_id)
    repository = _repository(request_settings)
    try:
        require_api_role(auth, API_WRITE_ROLES)
        _require_document_access(repository, document_id, auth)
        report = repository.get_quality_report(document_id)
        if report is None:
            raise HTTPException(status_code=404, detail=f"Quality report not found for document: {document_id}")
        result = report.model_dump(mode="json")
        audit_api_event(
            request_settings,
            auth,
            action="document.read.quality",
            outcome="success",
            status_code=200,
            resource_type="document",
            document_id=document_id,
        )
        return result
    except HTTPException as exc:
        _audit_document_read_exception(
            request_settings,
            auth,
            action="document.read.quality",
            document_id=document_id,
            exc=exc,
        )
        raise


@router.get("/{document_id}/runs")
def get_runs(document_id: str, auth_context: AuthContext = Depends(get_auth_context)):
    auth = coerce_auth_context(auth_context)
    request_settings = settings_for_tenant(get_settings(), auth.tenant_id)
    repository = _repository(request_settings)
    try:
        require_api_role(auth, API_WRITE_ROLES)
        _require_document_access(repository, document_id, auth)
        result = [run.model_dump(mode="json") for run in repository.list_runs(document_id)]
        audit_api_event(
            request_settings,
            auth,
            action="document.read.runs",
            outcome="success",
            status_code=200,
            resource_type="document",
            document_id=document_id,
            detail=f"count={len(result)}",
        )
        return result
    except HTTPException as exc:
        _audit_document_read_exception(
            request_settings,
            auth,
            action="document.read.runs",
            document_id=document_id,
            exc=exc,
        )
        raise


@router.get("/{document_id}/security-review")
def get_security_review(document_id: str, auth_context: AuthContext = Depends(get_auth_context)):
    auth = coerce_auth_context(auth_context)
    request_settings = settings_for_tenant(get_settings(), auth.tenant_id)
    repository = _repository(request_settings)
    try:
        require_api_role(auth, API_WRITE_ROLES)
        _require_document_access(repository, document_id, auth)
        result = repository.list_security_scan_records(document_id)
        audit_api_event(
            request_settings,
            auth,
            action="document.read.security_review",
            outcome="success",
            status_code=200,
            resource_type="document",
            document_id=document_id,
            detail=f"count={len(result)}",
        )
        return result
    except HTTPException as exc:
        _audit_document_read_exception(
            request_settings,
            auth,
            action="document.read.security_review",
            document_id=document_id,
            exc=exc,
        )
        raise


@router.post("/{document_id}/security-scan")
def security_scan_document(
    document_id: str,
    request: SecurityScanRequest | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
):
    settings = get_settings()
    auth = coerce_auth_context(auth_context)
    request_settings = settings_for_tenant(settings, auth.tenant_id)
    repository = _repository(request_settings)
    request = request or SecurityScanRequest()
    try:
        require_api_role(auth, API_WRITE_ROLES)
        _require_document_access(repository, document_id, auth)
        chunks = _load_review_chunks(repository, document_id)
        _updated_chunks, record = _run_security_scan_record(
            settings=request_settings,
            repository=repository,
            document_id=document_id,
            auth=auth,
            chunks=chunks,
            block_high_risk=request.block_high_risk,
        )
        audit_api_event(
            request_settings,
            auth,
            action="document.security_scan",
            outcome="success",
            status_code=200,
            resource_type="document",
            document_id=document_id,
            detail=f"findings={record['finding_count']} blocked={len(record['blocked_chunk_ids'])}",
        )
        return record
    except HTTPException as exc:
        audit_api_event(
            request_settings,
            auth,
            action="document.security_scan",
            outcome="denied" if exc.status_code == 403 else "failure",
            status_code=exc.status_code,
            resource_type="document",
            document_id=document_id,
            detail=str(exc.detail),
        )
        raise


@router.get("/{document_id}/review/chunks")
def get_review_chunks(document_id: str, auth_context: AuthContext = Depends(get_auth_context)):
    auth = coerce_auth_context(auth_context)
    request_settings = settings_for_tenant(get_settings(), auth.tenant_id)
    repository = _repository(request_settings)
    try:
        require_api_role(auth, API_WRITE_ROLES)
        _require_document_access(repository, document_id, auth)
        result = [chunk.model_dump(mode="json") for chunk in repository.get_chunks(document_id)]
        audit_api_event(
            request_settings,
            auth,
            action="document.read.review_chunks",
            outcome="success",
            status_code=200,
            resource_type="document",
            document_id=document_id,
            detail=f"count={len(result)}",
        )
        return result
    except HTTPException as exc:
        _audit_document_read_exception(
            request_settings,
            auth,
            action="document.read.review_chunks",
            document_id=document_id,
            exc=exc,
        )
        raise


@router.patch("/{document_id}/review/chunks/{chunk_id}")
def update_review_chunk(
    document_id: str,
    chunk_id: str,
    request: ReviewChunkUpdateRequest,
    auth_context: AuthContext = Depends(get_auth_context),
):
    settings = get_settings()
    auth = coerce_auth_context(auth_context)
    request_settings = settings_for_tenant(settings, auth.tenant_id)
    repository = _repository(request_settings)
    try:
        require_api_role(auth, API_WRITE_ROLES)
        _require_document_access(repository, document_id, auth)
        chunks = _load_review_chunks(repository, document_id)
        _require_chunk_ids(chunks, [chunk_id])
        before_hashes = _chunk_hashes(chunks, {chunk_id})

        allowed_fields = {
            "text",
            "normalized_text",
            "retrieval_text",
            "chunk_type",
            "security_level",
            "department_acl",
        }
        update_fields: dict = {}
        for field_name in sorted(request.model_fields_set & allowed_fields):
            update_fields[field_name] = getattr(request, field_name)
        if "security_level" in update_fields and update_fields["security_level"] is not None:
            update_fields["security_level"] = _normalize_security_level(update_fields["security_level"])
        if "department_acl" in update_fields and update_fields["department_acl"] is not None:
            update_fields["department_acl"] = _department_acl_set(update_fields["department_acl"])
        if request.metadata_patch:
            target = next(chunk for chunk in chunks if chunk.chunk_id == chunk_id)
            update_fields["metadata"] = {**target.metadata, **request.metadata_patch}
        if not update_fields:
            raise HTTPException(status_code=400, detail="No review chunk updates were provided.")
        update_fields.update(_clear_approval_fields())

        updated_chunk: Chunk | None = None
        updated_chunks: list[Chunk] = []
        for chunk in chunks:
            if chunk.chunk_id != chunk_id:
                updated_chunks.append(chunk)
                continue
            updated_chunk = chunk.model_copy(update=update_fields)
            updated_chunks.append(updated_chunk)

        repository.save_chunks(document_id, updated_chunks)
        artifacts = _refresh_review_exports(request_settings, document_id, updated_chunks)
        vector_sync = _sync_vector_index_after_review_change(
            settings=request_settings,
            repository=repository,
            document_id=document_id,
            auth=auth,
            chunks=updated_chunks,
            action="review_vector_sync",
        )
        reviewed_at = datetime.now(timezone.utc).isoformat()
        review_id = f"review_{uuid.uuid4().hex[:12]}"
        snapshot = _write_review_snapshot(request_settings, document_id, review_id, updated_chunks)
        review_record = {
            "review_id": review_id,
            "document_id": document_id,
            "chunk_ids": [chunk_id],
            "action": "update",
            "reviewed_by": auth.actor,
            "reviewed_at": reviewed_at,
            "tenant_id": auth.tenant_id,
            "status": "needs_review",
            "updated_fields": sorted(update_fields),
            "before_content_hashes": before_hashes,
            "after_content_hashes": _chunk_hashes(updated_chunks, {chunk_id}),
            "note": request.note or "",
            "snapshot": snapshot,
            "artifacts": artifacts,
            "vector_sync": vector_sync,
        }
        repository.append_review_record(review_record)
        audit_api_event(
            request_settings,
            auth,
            action="document.review.chunk.update",
            outcome="success",
            status_code=200,
            resource_type="document",
            document_id=document_id,
            detail=f"chunk_id={chunk_id} updated_fields={','.join(sorted(update_fields))}",
        )
        return {
            "review": review_record,
            "chunk": updated_chunk.model_dump(mode="json") if updated_chunk else {},
        }
    except HTTPException as exc:
        audit_api_event(
            request_settings,
            auth,
            action="document.review.chunk.update",
            outcome="denied" if exc.status_code == 403 else "failure",
            status_code=exc.status_code,
            resource_type="document",
            document_id=document_id,
            detail=str(exc.detail),
        )
        raise


@router.post("/{document_id}/review/chunks/{chunk_id}/split")
def split_review_chunk(
    document_id: str,
    chunk_id: str,
    request: SplitChunkRequest,
    auth_context: AuthContext = Depends(get_auth_context),
):
    settings = get_settings()
    auth = coerce_auth_context(auth_context)
    request_settings = settings_for_tenant(settings, auth.tenant_id)
    repository = _repository(request_settings)
    try:
        require_api_role(auth, API_WRITE_ROLES)
        _require_document_access(repository, document_id, auth)
        chunks = _load_review_chunks(repository, document_id)
        _require_chunk_ids(chunks, [chunk_id])
        split_texts = [text.strip() for text in request.texts if text.strip()]
        if len(split_texts) < 2:
            raise HTTPException(status_code=400, detail="At least two non-empty split texts are required.")
        before_hashes = _chunk_hashes(chunks, {chunk_id})
        source = next(chunk for chunk in chunks if chunk.chunk_id == chunk_id)
        new_chunks = [
            source.model_copy(
                update={
                    "chunk_id": _review_chunk_id(f"{chunk_id}_split_{index}"),
                    "text": text,
                    "normalized_text": text,
                    "retrieval_text": _review_retrieval_text(source, text),
                    "approval_status": "needs_review",
                    "approval_id": None,
                    "approved_by": None,
                    "approved_at": None,
                    "approved_content_hash": None,
                    "metadata": {
                        **source.metadata,
                        "review_parent_chunk_id": source.chunk_id,
                        "review_operation": "split",
                        "review_part_index": index,
                        "review_part_count": len(split_texts),
                    },
                }
            )
            for index, text in enumerate(split_texts, start=1)
        ]
        updated_chunks: list[Chunk] = []
        for chunk in chunks:
            if chunk.chunk_id == chunk_id:
                updated_chunks.append(
                    chunk.model_copy(
                        update={
                            "approval_status": "superseded",
                            "approval_id": None,
                            "approved_by": None,
                            "approved_at": None,
                            "approved_content_hash": None,
                        }
                    )
                )
                updated_chunks.extend(new_chunks)
            else:
                updated_chunks.append(chunk)
        repository.save_chunks(document_id, updated_chunks)
        artifacts = _refresh_review_exports(request_settings, document_id, updated_chunks)
        vector_sync = _sync_vector_index_after_review_change(
            settings=request_settings,
            repository=repository,
            document_id=document_id,
            auth=auth,
            chunks=updated_chunks,
            action="review_vector_sync",
        )
        review_id = f"review_{uuid.uuid4().hex[:12]}"
        snapshot = _write_review_snapshot(request_settings, document_id, review_id, updated_chunks)
        record = {
            "review_id": review_id,
            "document_id": document_id,
            "chunk_ids": [chunk_id],
            "created_chunk_ids": [chunk.chunk_id for chunk in new_chunks],
            "action": "split",
            "reviewed_by": auth.actor,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "tenant_id": auth.tenant_id,
            "status": "needs_review",
            "before_content_hashes": before_hashes,
            "after_content_hashes": _chunk_hashes(updated_chunks, {chunk.chunk_id for chunk in new_chunks}),
            "note": request.note or "",
            "snapshot": snapshot,
            "artifacts": artifacts,
            "vector_sync": vector_sync,
        }
        repository.append_review_record(record)
        audit_api_event(
            request_settings,
            auth,
            action="document.review.chunk.split",
            outcome="success",
            status_code=200,
            resource_type="document",
            document_id=document_id,
            detail=f"chunk_id={chunk_id} created={len(new_chunks)}",
        )
        return record
    except HTTPException as exc:
        audit_api_event(
            request_settings,
            auth,
            action="document.review.chunk.split",
            outcome="denied" if exc.status_code == 403 else "failure",
            status_code=exc.status_code,
            resource_type="document",
            document_id=document_id,
            detail=str(exc.detail),
        )
        raise


@router.post("/{document_id}/review/chunks/merge")
def merge_review_chunks(
    document_id: str,
    request: MergeChunksRequest,
    auth_context: AuthContext = Depends(get_auth_context),
):
    settings = get_settings()
    auth = coerce_auth_context(auth_context)
    request_settings = settings_for_tenant(settings, auth.tenant_id)
    repository = _repository(request_settings)
    try:
        require_api_role(auth, API_WRITE_ROLES)
        if request.approval_override_reason:
            require_api_role(auth, {API_ROLE_ADMIN})
        _require_document_access(repository, document_id, auth)
        chunks = _load_review_chunks(repository, document_id)
        requested_ids = _require_chunk_ids(chunks, request.chunk_ids)
        if len(requested_ids) < 2:
            raise HTTPException(status_code=400, detail="At least two chunk_ids are required for merge.")
        before_hashes = _chunk_hashes(chunks, requested_ids)
        selected = [chunk for chunk in chunks if chunk.chunk_id in requested_ids]
        base = selected[0]
        merged_text = (request.text or "\n\n".join(chunk.text for chunk in selected)).strip()
        merged_chunk = base.model_copy(
            update={
                "chunk_id": _review_chunk_id("merged_chunk"),
                "text": merged_text,
                "normalized_text": merged_text,
                "retrieval_text": _review_retrieval_text(base, merged_text),
                "approval_status": "needs_review",
                "approval_id": None,
                "approved_by": None,
                "approved_at": None,
                "approved_content_hash": None,
                "metadata": {
                    **base.metadata,
                    "review_source_chunk_ids": sorted(requested_ids),
                    "review_operation": "merge",
                },
            }
        )
        updated_chunks: list[Chunk] = []
        inserted = False
        for chunk in chunks:
            if chunk.chunk_id in requested_ids:
                if not inserted:
                    updated_chunks.append(merged_chunk)
                    inserted = True
                updated_chunks.append(
                    chunk.model_copy(
                        update={
                            "approval_status": "superseded",
                            "approval_id": None,
                            "approved_by": None,
                            "approved_at": None,
                            "approved_content_hash": None,
                        }
                    )
                )
            else:
                updated_chunks.append(chunk)
        repository.save_chunks(document_id, updated_chunks)
        artifacts = _refresh_review_exports(request_settings, document_id, updated_chunks)
        vector_sync = _sync_vector_index_after_review_change(
            settings=request_settings,
            repository=repository,
            document_id=document_id,
            auth=auth,
            chunks=updated_chunks,
            action="review_vector_sync",
        )
        review_id = f"review_{uuid.uuid4().hex[:12]}"
        snapshot = _write_review_snapshot(request_settings, document_id, review_id, updated_chunks)
        record = {
            "review_id": review_id,
            "document_id": document_id,
            "chunk_ids": sorted(requested_ids),
            "created_chunk_ids": [merged_chunk.chunk_id],
            "action": "merge",
            "reviewed_by": auth.actor,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "tenant_id": auth.tenant_id,
            "status": "needs_review",
            "before_content_hashes": before_hashes,
            "after_content_hashes": _chunk_hashes(updated_chunks, {merged_chunk.chunk_id}),
            "note": request.note or "",
            "snapshot": snapshot,
            "artifacts": artifacts,
            "vector_sync": vector_sync,
        }
        repository.append_review_record(record)
        audit_api_event(
            request_settings,
            auth,
            action="document.review.chunks.merge",
            outcome="success",
            status_code=200,
            resource_type="document",
            document_id=document_id,
            detail=f"merged_chunks={len(requested_ids)} created={merged_chunk.chunk_id}",
        )
        return record
    except HTTPException as exc:
        audit_api_event(
            request_settings,
            auth,
            action="document.review.chunks.merge",
            outcome="denied" if exc.status_code == 403 else "failure",
            status_code=exc.status_code,
            resource_type="document",
            document_id=document_id,
            detail=str(exc.detail),
        )
        raise


@router.post("/{document_id}/review/approve")
def approve_review_chunks(
    document_id: str,
    request: ApprovalRequest,
    auth_context: AuthContext = Depends(get_auth_context),
):
    settings = get_settings()
    auth = coerce_auth_context(auth_context)
    request_settings = settings_for_tenant(settings, auth.tenant_id)
    repository = _repository(request_settings)
    try:
        require_api_role(auth, API_WRITE_ROLES)
        _require_document_access(repository, document_id, auth)
        chunks = _load_review_chunks(repository, document_id)
        requested_ids = _require_chunk_ids(chunks, request.chunk_ids)
        chunks, preapproval_scan = _run_security_scan_record(
            settings=request_settings,
            repository=repository,
            document_id=document_id,
            auth=auth,
            chunks=chunks,
            block_high_risk=True,
            chunk_ids=requested_ids,
            scan_reason="pre_approval",
        )
        approval_id = request.approval_id or f"approval_{uuid.uuid4().hex[:12]}"
        worklist_evidence = _approval_worklist_evidence(request)
        approved_at = datetime.now(timezone.utc).isoformat()
        try:
            approval_decision = _service_prepare_approval_decision(
                chunks=chunks,
                chunk_ids=sorted(requested_ids),
                review_flags_acknowledged=bool(request.review_flags_acknowledged),
                preapproval_scan=preapproval_scan,
                artifact_root=request_settings.artifact_root,
                runtime_data_dir=request_settings.data_dir,
                tenant_id=auth.tenant_id,
                document_id=document_id,
                approval_id=approval_id,
                approved_by=auth.actor,
                approved_at=approved_at,
                requested_security_level=request.security_level,
                worklist_evidence=worklist_evidence,
                approval_override_reason=request.approval_override_reason,
            )
        except ReviewWorkflowError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        requested_ids = approval_decision.requested_ids
        approval_update = approval_decision.approval_update
        updated_chunks = approval_update.updated_chunks

        document = repository.get_document(document_id)
        if document is not None and document.regulation_id:
            current_regulation_status = str(document.regulation_status or "draft").strip().casefold()
            if current_regulation_status not in {"superseded", "repealed"}:
                document.regulation_status = (
                    "approved"
                    if updated_chunks and all(chunk.approval_status == APPROVED_CHUNK_STATUS for chunk in updated_chunks)
                    else "pending_approval"
                )
            for chunk in updated_chunks:
                chunk.metadata = {
                    **dict(chunk.metadata or {}),
                    "regulation_status": document.regulation_status,
                }

        repository.save_chunks(document_id, updated_chunks)
        if document is not None and document.regulation_id:
            repository.upsert_document(document)
        artifacts = _refresh_review_exports(request_settings, document_id, updated_chunks)
        approval_record_id = f"approval_record_{uuid.uuid4().hex[:12]}"
        snapshot = _write_review_snapshot(request_settings, document_id, approval_record_id, updated_chunks)
        approval_record = _build_approval_record(
            update=approval_update,
            approval_record_id=approval_record_id,
            document_id=document_id,
            requested_ids=requested_ids,
            tenant_id=auth.tenant_id,
            worklist_evidence=worklist_evidence,
            review_flags_acknowledged=bool(request.review_flags_acknowledged),
            preapproval_scan=preapproval_scan,
            note=request.note or "",
            snapshot=snapshot,
            artifacts=artifacts,
            vector_sync={"status": "pending", "reason": "approval_journal_append_before_vector_sync"},
        )
        if document is not None and document.profile_id:
            approval_record["profile_id"] = document.profile_id
        approval_record["vector_sync_event_id"] = f"approval_vector_sync_{uuid.uuid4().hex[:12]}"
        review_decision_events = _sanitize_review_decision_events(request.review_decision_events)
        if request.approval_override_reason and not any(
            event.get("event") == "approved_without_review" for event in review_decision_events
        ):
            review_decision_events.append(
                {
                    "event": "approved_without_review",
                    "timestamp": approved_at,
                    "actor": auth.actor,
                    "chunk_id": ",".join(sorted(requested_ids)[:20]),
                    "override_reason": request.approval_override_reason,
                }
            )
        if review_decision_events:
            approval_record["review_decision_events"] = review_decision_events
            approval_record["review_decision_event_counts"] = {
                event_name: sum(1 for event in review_decision_events if event.get("event") == event_name)
                for event_name in sorted({str(event.get("event") or "") for event in review_decision_events})
                if event_name
            }
            approval_record["ai_review_confirmed"] = any(
                event.get("event") == "ai_review_confirmed" for event in review_decision_events
            )
            approval_record["human_review_confirmed"] = any(
                event.get("event") == "human_review_confirmed" for event in review_decision_events
            )
        if request.approval_override_reason:
            approval_record["approval_override_reason"] = request.approval_override_reason
        approval_record["approval_state_transition"] = _approval_state_transition(
            [
                chunk.approval_status
                for chunk in chunks
                if chunk.chunk_id in requested_ids
            ]
        )
        repository.append_approval_record(approval_record)
        try:
            vector_sync = _sync_vector_index_after_review_change(
                settings=request_settings,
                repository=repository,
                document_id=document_id,
                auth=auth,
                chunks=updated_chunks,
                action="review_vector_sync",
            )
        except Exception as exc:
            vector_sync = _failed_vector_sync_payload(exc)
            vector_sync_event = _append_approval_vector_sync_outcome(
                repository=repository,
                document_id=document_id,
                auth=auth,
                approval_record=approval_record,
                vector_sync=vector_sync,
                outcome="failure",
            )
            raise HTTPException(
                status_code=500,
                detail={
                    "message": "Approval was persisted, but vector index synchronization failed.",
                    "approval_id": approval_id,
                    "approval_record_id": approval_record_id,
                    "vector_sync_event_id": vector_sync_event["event_id"],
                    "reindex_required": True,
                },
            ) from exc
        vector_sync_event = _append_approval_vector_sync_outcome(
            repository=repository,
            document_id=document_id,
            auth=auth,
            approval_record=approval_record,
            vector_sync=vector_sync,
            outcome="completed",
        )
        automatic_supersede_event = None
        if document is not None and str(document.regulation_status or "").strip().casefold() == "approved":
            try:
                automatic_supersede_event = _automatically_supersede_prior_version(
                    settings=request_settings,
                    repository=repository,
                    document=document,
                    auth=auth,
                )
            except Exception as exc:
                audit_api_event(
                    request_settings,
                    auth,
                    action="document.regulation.auto_supersede",
                    outcome="failure",
                    status_code=500,
                    resource_type="document",
                    document_id=document_id,
                    detail=str(exc),
                )
                raise HTTPException(
                    status_code=500,
                    detail={
                        "message": "Revision approval succeeded, but automatic supersede synchronization failed.",
                        "approval_id": approval_id,
                        "approval_record_id": approval_record_id,
                        "reindex_required": True,
                    },
                ) from exc
        response_record = dict(approval_record)
        response_record["vector_sync"] = vector_sync
        response_record["vector_sync_event_id"] = vector_sync_event["event_id"]
        if automatic_supersede_event is not None:
            response_record["automatic_supersede_event"] = automatic_supersede_event
        audit_detail_parts = [
            f"approved_chunks={len(requested_ids)}",
            f"approval_id={approval_id}",
            f"vector_sync_event_id={vector_sync_event['event_id']}",
            f"vector_sync_status={vector_sync.get('status') or 'unknown'}",
        ]
        if automatic_supersede_event is not None:
            audit_detail_parts.append(
                f"automatic_supersede_document_id={automatic_supersede_event.get('document_id') or ''}"
            )
        for key in (
            "worklist_report_path",
            "worklist_report_sha256",
            "review_batch_manifest_path",
            "review_batch_manifest_sha256",
            "review_batch_id",
            "review_batch_chunk_fingerprint",
            "review_strategy",
        ):
            value = worklist_evidence.get(key)
            if value:
                audit_detail_parts.append(f"{key}={value}")
        audit_api_event(
            request_settings,
            auth,
            action="document.review.approve",
            outcome="success",
            status_code=200,
            resource_type="document",
            document_id=document_id,
            detail=" ".join(audit_detail_parts),
        )
        return response_record
    except HTTPException as exc:
        audit_api_event(
            request_settings,
            auth,
            action="document.review.approve",
            outcome="denied" if exc.status_code == 403 else "failure",
            status_code=exc.status_code,
            resource_type="document",
            document_id=document_id,
            detail=str(exc.detail),
        )
        raise


@router.post("/{document_id}/review/reject")
def reject_review_chunks(
    document_id: str,
    request: RejectRequest,
    auth_context: AuthContext = Depends(get_auth_context),
):
    settings = get_settings()
    auth = coerce_auth_context(auth_context)
    request_settings = settings_for_tenant(settings, auth.tenant_id)
    repository = _repository(request_settings)
    try:
        require_api_role(auth, API_WRITE_ROLES)
        _require_document_access(repository, document_id, auth)
        chunks = _load_review_chunks(repository, document_id)
        reviewed_at = datetime.now(timezone.utc).isoformat()
        try:
            rejection_decision = _service_prepare_rejection_decision(
                chunks=chunks,
                chunk_ids=request.chunk_ids,
                reason=request.reason,
                reviewed_by=auth.actor,
                reviewed_at=reviewed_at,
            )
        except ReviewWorkflowError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        requested_ids = rejection_decision.requested_ids
        rejection_update = rejection_decision.rejection_update
        updated_chunks = rejection_update.updated_chunks

        repository.save_chunks(document_id, updated_chunks)
        artifacts = _refresh_review_exports(request_settings, document_id, updated_chunks)
        vector_sync = _sync_vector_index_after_review_change(
            settings=request_settings,
            repository=repository,
            document_id=document_id,
            auth=auth,
            chunks=updated_chunks,
            action="review_vector_sync",
        )
        review_id = f"review_{uuid.uuid4().hex[:12]}"
        snapshot = _write_review_snapshot(request_settings, document_id, review_id, updated_chunks)
        review_record = _build_rejection_record(
            update=rejection_update,
            review_id=review_id,
            document_id=document_id,
            requested_ids=requested_ids,
            tenant_id=auth.tenant_id,
            reason=rejection_decision.reason,
            note=request.note or "",
            snapshot=snapshot,
            artifacts=artifacts,
            vector_sync=vector_sync,
        )
        repository.append_review_record(review_record)
        audit_api_event(
            request_settings,
            auth,
            action="document.review.reject",
            outcome="success",
            status_code=200,
            resource_type="document",
            document_id=document_id,
            detail=f"rejected_chunks={len(requested_ids)} review_id={review_record['review_id']}",
        )
        return review_record
    except HTTPException as exc:
        audit_api_event(
            request_settings,
            auth,
            action="document.review.reject",
            outcome="denied" if exc.status_code == 403 else "failure",
            status_code=exc.status_code,
            resource_type="document",
            document_id=document_id,
            detail=str(exc.detail),
        )
        raise


@router.get("/{document_id}/index-status")
def get_index_status(document_id: str, auth_context: AuthContext = Depends(get_auth_context)):
    auth = coerce_auth_context(auth_context)
    settings = settings_for_tenant(get_settings(), auth.tenant_id)
    repository = _repository(settings)
    require_api_role(auth, API_WRITE_ROLES)
    _require_document_access(repository, document_id, auth)
    document = repository.get_document(document_id)
    chunks = repository.get_chunks(document_id)
    validation_error = None
    try:
        if document:
            prepared_chunks = _chunks_for_indexing(chunks, document, auth)
            _require_approval_journal_records(repository, document_id=document_id, chunks=chunks, auth=auth)
        else:
            prepared_chunks = []
        current_records, vector_summary = build_vector_records(prepared_chunks)
    except HTTPException as exc:
        if exc.status_code != 400:
            raise
        current_records = []
        vector_summary = {"record_count": 0}
        validation_error = str(exc.detail)
    jobs = repository.list_indexing_jobs(document_id)
    latest_job = jobs[-1] if jobs else None
    indexing_status = "review_required" if validation_error else (latest_job.get("status") if latest_job else "not_indexed")
    vector_consistency = {"checked": False, "stale_count": 0, "samples": []}
    if latest_job and latest_job.get("status") == "indexed" and not validation_error:
        if int(latest_job.get("record_count") or 0) != int(vector_summary.get("record_count") or 0):
            indexing_status = "reindex_required"
        vector_consistency = _vector_consistency_summary(
            settings=settings,
            auth=auth,
            document_id=document_id,
            target_type=str(latest_job.get("target_type") or "local-jsonl"),
            current_records=current_records,
        )
        if vector_consistency.get("checked") and (
            vector_consistency.get("stale_count") or not vector_consistency.get("target_path_configured")
        ):
            indexing_status = "reindex_required"
    return {
        "document_id": document_id,
        "indexing_status": indexing_status,
        "latest_job": latest_job,
        "job_count": len(jobs),
        "vector_summary": vector_summary,
        "vector_consistency": vector_consistency,
        "validation_error": validation_error,
    }


@router.post("/{document_id}/index")
def index_document(
    document_id: str,
    request: IndexRequest | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
):
    settings = get_settings()
    auth = coerce_auth_context(auth_context)
    request_settings = settings_for_tenant(settings, auth.tenant_id)
    repository = _repository(request_settings)
    request = request or IndexRequest()
    try:
        require_api_role(auth, API_WRITE_ROLES)
        _require_document_access(repository, document_id, auth)
        indexing_job = _run_document_indexing(
            settings=request_settings,
            repository=repository,
            document_id=document_id,
            auth=auth,
            request=request,
            action="index",
        )
        audit_api_event(
            request_settings,
            auth,
            action="document.index",
            outcome="success",
            status_code=200,
            resource_type="document",
            document_id=document_id,
            detail=f"status={indexing_job['status']} record_count={indexing_job['record_count']}",
        )
        return indexing_job
    except HTTPException as exc:
        audit_api_event(
            request_settings,
            auth,
            action="document.index",
            outcome="denied" if exc.status_code == 403 else "failure",
            status_code=exc.status_code,
            resource_type="document",
            document_id=document_id,
            detail=str(exc.detail),
        )
        raise
    except Exception as exc:
        audit_api_event(
            request_settings,
            auth,
            action="document.index",
            outcome="failure",
            status_code=500,
            resource_type="document",
            document_id=document_id,
            detail=str(exc),
        )
        raise HTTPException(status_code=500, detail=_INDEXING_FAILURE_DETAIL) from exc


@router.post("/{document_id}/reindex")
def reindex_document(
    document_id: str,
    request: IndexRequest | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
):
    settings = get_settings()
    auth = coerce_auth_context(auth_context)
    request_settings = settings_for_tenant(settings, auth.tenant_id)
    repository = _repository(request_settings)
    request = request or IndexRequest()
    try:
        require_api_role(auth, API_WRITE_ROLES)
        _require_document_access(repository, document_id, auth)
        indexing_job = _run_document_indexing(
            settings=request_settings,
            repository=repository,
            document_id=document_id,
            auth=auth,
            request=request,
            action="reindex",
        )
        audit_api_event(
            request_settings,
            auth,
            action="document.reindex",
            outcome="success",
            status_code=200,
            resource_type="document",
            document_id=document_id,
            detail=f"status={indexing_job['status']} record_count={indexing_job['record_count']}",
        )
        return indexing_job
    except HTTPException as exc:
        audit_api_event(
            request_settings,
            auth,
            action="document.reindex",
            outcome="denied" if exc.status_code == 403 else "failure",
            status_code=exc.status_code,
            resource_type="document",
            document_id=document_id,
            detail=str(exc.detail),
        )
        raise
    except Exception as exc:
        audit_api_event(
            request_settings,
            auth,
            action="document.reindex",
            outcome="failure",
            status_code=500,
            resource_type="document",
            document_id=document_id,
            detail=str(exc),
        )
        raise HTTPException(status_code=500, detail=_INDEXING_FAILURE_DETAIL) from exc
