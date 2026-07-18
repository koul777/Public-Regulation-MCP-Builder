from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.api_audit import audit_api_event
from app.core.config import Settings, get_settings
from app.core.input_limits import (
    MAX_MCP_QUERY_CHARS,
    MAX_MCP_TOP_K,
    McpDepartmentIds,
    McpOptionalIdentifier,
    McpSecurityLevels,
)
from app.core.security import (
    API_READ_ROLES,
    API_ROLE_ADMIN,
    API_ROLE_OPERATOR,
    API_ROLE_VIEWER,
    AuthContext,
    coerce_auth_context,
    get_auth_context,
    normalize_department_ids,
    require_api_role,
)
from app.core.tenant_access import resource_visible_to_tenant, settings_for_tenant, tenant_storage_key
from app.ingestion.embedding_adapter import LOCAL_HASH_EMBEDDING_MODEL
from app.ingestion.vector_adapter import APPROVED_CHUNK_STATUS, stable_content_hash, vector_record_from_chunk
from app.ingestion.vector_integrity import embedded_vector_integrity_reason
from app.ingestion.vector_upsert import validate_vector_records
from app.rag.local_llm import generate_local_llm_answer, local_llm_available, probe_local_llm
from app.rag.output_filter import sanitize_rag_answer
from app.rag.extractive_answer import build_structured_extractive_answer
from app.retrieval.bm25_index import (
    BM25_RETRIEVAL_MODEL,
    BM25_STRUCTURED_METADATA_VERSION,
    Bm25Index,
    default_bm25_index_path,
    load_bm25_index,
    source_content_hashes,
)
from app.retrieval.searcher import search as search_retrieval_records
from app.services.review_decision_service import APPROVAL_WORKLIST_METADATA_KEYS, approval_worklist_metadata
from app.services.regulation_catalog_service import filter_to_latest_active_versions, read_regulation_metadata
from app.storage.repository import JsonRepository


router = APIRouter(prefix="/api/rag", tags=["rag"])

SECURITY_LEVEL_ORDER = ("public", "internal", "sensitive", "confidential")
ROLE_SECURITY_LEVELS = {
    API_ROLE_ADMIN: frozenset(SECURITY_LEVEL_ORDER),
    API_ROLE_OPERATOR: frozenset({"public", "internal", "sensitive"}),
    API_ROLE_VIEWER: frozenset({"public"}),
}
BLOCKED_QUERY_PATTERNS = (
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|system)\s+instructions", re.IGNORECASE),
    re.compile(r"(?:show|reveal|print|dump)\s+(?:the\s+)?(?:system\s+)?prompt", re.IGNORECASE),
    re.compile(r"(?:read|open|print)\s+(?:local\s+)?(?:file|path)", re.IGNORECASE),
    re.compile(r"(?:execute|run)\s+(?:shell|cmd|powershell|command)", re.IGNORECASE),
)
_RAG_RATE_LIMIT_LOCK = threading.Lock()
_RAG_RATE_LIMIT_BUCKETS: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
_RAG_RATE_LIMIT_MAX_BUCKETS = 10_000
_RAG_VECTOR_CACHE_LOCK = threading.Lock()
_FileIdentitySignature = tuple[int, int, int, int]
_RAG_VECTOR_RECORD_CACHE: dict[Path, tuple[_FileIdentitySignature, list[dict[str, Any]]]] = {}
_RAG_BM25_INDEX_CACHE: dict[Path, tuple[_FileIdentitySignature, Any]] = {}
_RAG_REBUILT_BM25_INDEX_CACHE: dict[Path, tuple[_FileIdentitySignature, str, Any]] = {}
_RAG_VISIBLE_RECORDS_CACHE_LOCK = threading.Lock()
_RAG_VISIBLE_RECORDS_CACHE: OrderedDict[tuple[Any, ...], list[dict[str, Any]]] = OrderedDict()
_RAG_VISIBLE_RECORDS_MAX_ENTRIES = 512
_RAG_VECTOR_SOURCE_HASH_CACHE: dict[Path, tuple[_FileIdentitySignature, str]] = {}
_RAG_REPOSITORY_DOCUMENT_SIGNATURE_CACHE: dict[
    tuple[Path, tuple[str, ...]],
    tuple[tuple[Any, Any], str],
] = {}
_RAG_APPROVAL_SNAPSHOT_CACHE: dict[
    tuple[Path, str, tuple[str, ...]],
    tuple[tuple[Any, ...], dict[tuple[str, str], dict[str, Any]]],
] = {}
_RAG_RESPONSE_METADATA_PROFILES = frozenset({"full", "external", "chatgpt-data"})
_EXTERNAL_RAG_RESPONSE_METADATA_PROFILES = frozenset({"external", "chatgpt-data"})
_INTERNAL_RAG_RESPONSE_METADATA_KEYS = frozenset(
    {
        "source_record_id",
        "source_file_id",
        "approval_worklist_report_sha256",
        "approval_review_batch_manifest_path",
        "approval_review_batch_manifest_sha256",
        "approval_review_batch_id",
        "approval_review_batch_chunk_fingerprint",
        "approval_review_strategy",
    }
)


class _RagRequestRepositoryCache:
    def __init__(self, repository: JsonRepository) -> None:
        self._repository = repository
        self._documents: dict[str, Any | None] = {}
        self._chunks_by_document: dict[str, dict[str, Any]] = {}

    def get_document(self, document_id: str) -> Any | None:
        if document_id not in self._documents:
            self._documents[document_id] = self._repository.get_document(document_id)
        return self._documents[document_id]

    def get_chunk(self, document_id: str, chunk_id: str) -> Any | None:
        if document_id not in self._chunks_by_document:
            self._chunks_by_document[document_id] = {
                str(chunk.chunk_id): chunk for chunk in self._repository.get_chunks(document_id)
            }
        return self._chunks_by_document[document_id].get(chunk_id)


class RagSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=MAX_MCP_QUERY_CHARS)
    top_k: int = Field(default=5, ge=1, le=MAX_MCP_TOP_K)
    security_levels: McpSecurityLevels | None = None
    department_ids: McpDepartmentIds = Field(default_factory=list)
    document_id: McpOptionalIdentifier | None = None
    profile_id: McpOptionalIdentifier | None = None
    metadata_profile: str = Field(default="full", max_length=20)
    as_of_date: str | None = Field(default=None, max_length=20)


class RagChatRequest(RagSearchRequest):
    llm_backend: str | None = Field(default=None, max_length=40)


class RagFeedbackRequest(BaseModel):
    trace_id: str = Field(min_length=1, max_length=80)
    rating: str = Field(default="neutral", max_length=20)
    reason: str | None = Field(default=None, max_length=1000)


class RagRuntimeTestRequest(BaseModel):
    query: str = Field(default="runtime health check", min_length=1, max_length=MAX_MCP_QUERY_CHARS)
    top_k: int = Field(default=1, ge=1, le=5)


def search_rag_records(
    request: RagSearchRequest,
    auth: AuthContext,
    settings: Settings,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    total_started_at = time.perf_counter()
    timing_ms: dict[str, float] = {}
    _validate_query_policy(request.query)
    requested_department_ids = _requested_department_ids(request, auth)
    repository = JsonRepository(settings)
    repository_cache = _RagRequestRepositoryCache(repository)
    step_started_at = time.perf_counter()
    records = _load_local_vector_records(settings, auth)
    timing_ms["load_vector_records_elapsed_ms"] = _perf_elapsed_ms(step_started_at)
    step_started_at = time.perf_counter()
    approval_snapshot = _load_cached_approval_snapshot(repository, records, auth)
    timing_ms["approval_snapshot_elapsed_ms"] = _perf_elapsed_ms(step_started_at)
    step_started_at = time.perf_counter()
    visible_records = load_visible_records(
        request=request,
        auth=auth,
        settings=settings,
        repository=repository,
        repository_cache=repository_cache,
        records=records,
        approval_snapshot=approval_snapshot,
        requested_department_ids=requested_department_ids,
    )
    lifecycle_complete = sum(1 for record in visible_records if _has_complete_lifecycle_metadata(record))
    timing_ms["visibility_filter_elapsed_ms"] = _perf_elapsed_ms(step_started_at)
    step_started_at = time.perf_counter()
    scored, retrieval = _score_records(request.query, visible_records, settings=settings, auth=auth, all_records=records)
    timing_ms["scoring_elapsed_ms"] = _perf_elapsed_ms(step_started_at)
    step_started_at = time.perf_counter()
    results = [
        _public_search_result(record, score, related_records=visible_records)
        for score, record in scored[: request.top_k]
    ]
    timing_ms["public_results_elapsed_ms"] = _perf_elapsed_ms(step_started_at)
    timing_ms["total_before_trace_write_elapsed_ms"] = _perf_elapsed_ms(total_started_at)
    trace = _rag_trace(
        action="search",
        request=request,
        auth=auth,
        results=results,
        extra={
            "candidate_count": len(records),
            "visible_count": len(visible_records),
            "lifecycle_selection": {
                "mode": "latest_active_version_per_regulation",
                "as_of_date": _normalized_lifecycle_as_of(request.as_of_date),
                "selected_record_count": len(visible_records),
                "complete_lifecycle_record_count": lifecycle_complete,
                "legacy_compatibility_records_retained": len(visible_records) - lifecycle_complete,
                "historical_versions_available_via": "get_regulation_history_or_as_of_date",
            },
            "embedding_model": retrieval["retrieval_model"],
            "timing_ms": timing_ms,
            **retrieval,
        },
    )
    step_started_at = time.perf_counter()
    repository.append_rag_trace(trace)
    timing_ms["trace_write_elapsed_ms"] = _perf_elapsed_ms(step_started_at)
    timing_ms["total_elapsed_ms"] = _perf_elapsed_ms(total_started_at)
    return results, trace


@router.post("/search")
def rag_search(request: RagSearchRequest, auth_context: AuthContext = Depends(get_auth_context)):
    settings = get_settings()
    auth = coerce_auth_context(auth_context)
    request_settings = settings_for_tenant(settings, auth.tenant_id)
    try:
        require_api_role(auth, API_READ_ROLES)
        _enforce_rag_rate_limit(request_settings, auth)
        _validate_query_policy(request.query)
        _validate_security_scope(request, auth)
        metadata_profile = _validate_response_metadata_profile(request.metadata_profile)
        results, trace = search_rag_records(request, auth, request_settings)
        audit_api_event(
            request_settings,
            auth,
            action="rag.search",
            outcome="success",
            status_code=200,
            resource_type="rag",
            detail=f"trace_id={trace['trace_id']} result_count={len(results)}",
        )
        return {"trace_id": trace["trace_id"], "results": _rag_results_for_metadata_profile(results, metadata_profile)}
    except HTTPException as exc:
        audit_api_event(
            request_settings,
            auth,
            action="rag.search",
            outcome="denied" if exc.status_code == 403 else "failure",
            status_code=exc.status_code,
            resource_type="rag",
            detail=str(exc.detail),
        )
        raise


@router.post("/chat")
def rag_chat(request: RagChatRequest, auth_context: AuthContext = Depends(get_auth_context)):
    settings = get_settings()
    auth = coerce_auth_context(auth_context)
    request_settings = settings_for_tenant(settings, auth.tenant_id)
    try:
        require_api_role(auth, API_READ_ROLES)
        _enforce_rag_rate_limit(request_settings, auth)
        _validate_query_policy(request.query)
        _validate_security_scope(request, auth)
        metadata_profile = _validate_response_metadata_profile(request.metadata_profile)
        results, search_trace = search_rag_records(request, auth, request_settings)
        backend = _chat_backend(request, request_settings)
        answer = _chat_answer(backend, request_settings, request.query, results)
        trace = _rag_trace(
            action="chat",
            request=request,
            auth=auth,
            results=results,
            extra={
                "search_trace_id": search_trace["trace_id"],
                "llm_backend": backend,
                "answer_mode": "grounded_local" if backend != "extractive" else "grounded_extractive",
            },
        )
        JsonRepository(request_settings).append_rag_trace(trace)
        audit_api_event(
            request_settings,
            auth,
            action="rag.chat",
            outcome="success",
            status_code=200,
            resource_type="rag",
            detail=f"trace_id={trace['trace_id']} result_count={len(results)}",
        )
        return {
            "trace_id": trace["trace_id"],
            "answer": answer,
            "citations": [
                _rag_chat_citation_for_metadata_profile(result, metadata_profile)
                for result in results
            ],
        }
    except HTTPException as exc:
        audit_api_event(
            request_settings,
            auth,
            action="rag.chat",
            outcome="denied" if exc.status_code == 403 else "failure",
            status_code=exc.status_code,
            resource_type="rag",
            detail=str(exc.detail),
        )
        raise


@router.post("/feedback")
def rag_feedback(request: RagFeedbackRequest, auth_context: AuthContext = Depends(get_auth_context)):
    settings = get_settings()
    auth = coerce_auth_context(auth_context)
    request_settings = settings_for_tenant(settings, auth.tenant_id)
    try:
        require_api_role(auth, API_READ_ROLES)
        _enforce_rag_rate_limit(request_settings, auth)
        rating = request.rating.strip().lower()
        if rating not in {"helpful", "unhelpful", "unsafe", "incorrect", "neutral"}:
            raise HTTPException(status_code=400, detail="rating must be helpful, unhelpful, unsafe, incorrect, or neutral.")
        repository = JsonRepository(request_settings)
        trace = next((item for item in repository.list_rag_traces() if item.get("trace_id") == request.trace_id), None)
        if trace is None or trace.get("tenant_id") != auth.tenant_id:
            raise HTTPException(status_code=404, detail=f"RAG trace not found: {request.trace_id}")
        if not _feedback_allowed_for_trace(trace, auth):
            raise HTTPException(status_code=404, detail=f"RAG trace not found: {request.trace_id}")
        feedback = {
            "feedback_id": f"feedback_{uuid4().hex[:12]}",
            "trace_id": request.trace_id,
            "rating": rating,
            "reason_hash": hashlib.sha256(str(request.reason or "").encode("utf-8")).hexdigest() if request.reason else "",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "actor": auth.actor,
            "tenant_id": auth.tenant_id,
            "api_role": auth.role,
        }
        repository.append_rag_feedback(feedback)
        audit_api_event(
            request_settings,
            auth,
            action="rag.feedback",
            outcome="success",
            status_code=200,
            resource_type="rag",
            detail=f"trace_id={request.trace_id} rating={rating}",
        )
        return feedback
    except HTTPException as exc:
        audit_api_event(
            request_settings,
            auth,
            action="rag.feedback",
            outcome="denied" if exc.status_code == 403 else "failure",
            status_code=exc.status_code,
            resource_type="rag",
            detail=str(exc.detail),
        )
        raise


@router.get("/runtime/status")
def rag_runtime_status(auth_context: AuthContext = Depends(get_auth_context)):
    auth = coerce_auth_context(auth_context)
    settings = settings_for_tenant(get_settings(), auth.tenant_id)
    try:
        require_api_role(auth, API_READ_ROLES)
        vector_path = _local_vector_path(settings, auth)
        response = {
            "status": "available",
            "local_only": True,
            "external_api_calls_enabled": False,
            "default_backend": "extractive",
            "configured_backend": request_backend_status(settings),
            "local_llm_available": local_llm_available(settings),
            "embedding_model": LOCAL_HASH_EMBEDDING_MODEL,
            "retrieval_model": BM25_RETRIEVAL_MODEL,
            "vector_store_configured": vector_path.is_file(),
        }
        audit_api_event(
            settings,
            auth,
            action="rag.runtime.status",
            outcome="success",
            status_code=200,
            resource_type="rag",
            detail=f"backend={response['configured_backend']} vector_store={response['vector_store_configured']}",
        )
        return response
    except HTTPException as exc:
        audit_api_event(
            settings,
            auth,
            action="rag.runtime.status",
            outcome="denied" if exc.status_code == 403 else "failure",
            status_code=exc.status_code,
            resource_type="rag",
            detail=str(exc.detail),
        )
        raise


@router.post("/runtime/test")
def rag_runtime_test(request: RagRuntimeTestRequest, auth_context: AuthContext = Depends(get_auth_context)):
    auth = coerce_auth_context(auth_context)
    settings = settings_for_tenant(get_settings(), auth.tenant_id)
    try:
        require_api_role(auth, API_READ_ROLES)
        _enforce_rag_rate_limit(settings, auth)
        _validate_query_policy(request.query)
        records = _load_local_vector_records(settings, auth)
        visible_record_count = _visible_runtime_record_count(records, request, auth, settings)
        configured_backend = request_backend_status(settings)
        llm_probe = (
            probe_local_llm(settings)
            if configured_backend in {"ollama", "llama-cpp", "openai-compatible"}
            else {"checked": False, "available": False, "backend": configured_backend}
        )
        response = {
            "ok": configured_backend == "extractive" or bool(llm_probe.get("available")),
            "local_only": True,
            "external_api_call_count": 0,
            "backend": configured_backend,
            "configured_backend": configured_backend,
            "local_llm_probe": llm_probe,
            "vector_record_count": visible_record_count,
            "test_query_hash": hashlib.sha256(request.query.encode("utf-8")).hexdigest(),
        }
        audit_api_event(
            settings,
            auth,
            action="rag.runtime.test",
            outcome="success",
            status_code=200,
            resource_type="rag",
            detail=f"backend={configured_backend} ok={response['ok']} vector_record_count={visible_record_count}",
        )
        return response
    except HTTPException as exc:
        audit_api_event(
            settings,
            auth,
            action="rag.runtime.test",
            outcome="denied" if exc.status_code == 403 else "failure",
            status_code=exc.status_code,
            resource_type="rag",
            detail=str(exc.detail),
        )
        raise


def _visible_runtime_record_count(
    records: list[dict[str, Any]],
    request: RagRuntimeTestRequest,
    auth: AuthContext,
    settings: Settings,
) -> int:
    search_request = RagSearchRequest(query=request.query, top_k=request.top_k)
    repository = JsonRepository(settings)
    repository_cache = _RagRequestRepositoryCache(repository)
    approval_snapshot = _load_cached_approval_snapshot(repository, records, auth)
    return len(
        load_visible_records(
            request=search_request,
            auth=auth,
            settings=settings,
            repository=repository,
            repository_cache=repository_cache,
            records=records,
            approval_snapshot=approval_snapshot,
            requested_department_ids=frozenset(),
        )
    )


def load_visible_records(
    *,
    request: RagSearchRequest,
    auth: AuthContext,
    settings: Settings,
    repository: JsonRepository,
    repository_cache: _RagRequestRepositoryCache,
    records: list[dict[str, Any]],
    approval_snapshot: dict[tuple[str, str], dict[str, Any]] | None,
    requested_department_ids: frozenset[str],
    latest_only: bool = True,
) -> list[dict[str, Any]]:
    vector_path_signature = _path_signature(_local_vector_path(settings, auth))
    cache_key = (
        vector_path_signature,
        id(approval_snapshot) if approval_snapshot is not None else None,
        auth.tenant_id,
        auth.role,
        tuple(sorted(str(item) for item in auth.department_ids if str(item).strip())),
        tuple(sorted(_requested_security_levels(request, auth))),
        request.document_id or "",
        request.profile_id or "",
        request.as_of_date or "",
        tuple(sorted(requested_department_ids)),
        latest_only,
    )
    with _RAG_VISIBLE_RECORDS_CACHE_LOCK:
        cached = _RAG_VISIBLE_RECORDS_CACHE.get(cache_key)
        if cached is not None:
            _RAG_VISIBLE_RECORDS_CACHE.move_to_end(cache_key)
            return list(cached)
    visible_records = [
        record
        for record in records
        if _record_visible_to_request(
            record,
            request=request,
            auth=auth,
            repository=repository,
            repository_cache=repository_cache,
            approval_snapshot=approval_snapshot,
            requested_department_ids=requested_department_ids,
        )
    ]
    if latest_only:
        visible_records = filter_to_latest_active_versions(
            visible_records,
            as_of=request.as_of_date,
            # Approval/tenant checks above remain fail-closed.  Keep approved
            # pre-catalog records visible until lifecycle metadata is backfilled;
            # complete regulation groups still use latest-version filtering.
            include_legacy=True,
        )
    with _RAG_VISIBLE_RECORDS_CACHE_LOCK:
        _RAG_VISIBLE_RECORDS_CACHE[cache_key] = list(visible_records)
        _RAG_VISIBLE_RECORDS_CACHE.move_to_end(cache_key)
        entry_limit = max(1, int(_RAG_VISIBLE_RECORDS_MAX_ENTRIES))
        while len(_RAG_VISIBLE_RECORDS_CACHE) > entry_limit:
            _RAG_VISIBLE_RECORDS_CACHE.popitem(last=False)
    return visible_records


def _load_local_vector_records(settings: Settings, auth: AuthContext) -> list[dict[str, Any]]:
    path = _local_vector_path(settings, auth)
    if not path.is_file():
        with _RAG_VECTOR_CACHE_LOCK:
            _RAG_VECTOR_RECORD_CACHE.pop(path, None)
        return []
    signature = _path_signature(path)
    if signature is not None:
        with _RAG_VECTOR_CACHE_LOCK:
            cached = _RAG_VECTOR_RECORD_CACHE.get(path)
            if cached and cached[0] == signature:
                return list(cached[1])
            validated = _read_local_vector_records(path)
            _RAG_VECTOR_RECORD_CACHE[path] = (signature, list(validated))
            return list(validated)
    return _read_local_vector_records(path)


def _read_local_vector_records(path: Path) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    for line_no, line in _iter_local_vector_lines(path):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=500, detail=f"Invalid local vector store JSONL at line {line_no}.") from exc
        if isinstance(record, dict):
            validated_record = _validated_local_vector_record(record)
            if validated_record is not None:
                validated.append(validated_record)
    return validated


def _load_local_vector_record_by_chunk(
    settings: Settings,
    auth: AuthContext,
    *,
    document_id: str,
    chunk_id: str,
) -> dict[str, Any] | None:
    path = _local_vector_path(settings, auth)
    if not path.is_file():
        with _RAG_VECTOR_CACHE_LOCK:
            _RAG_VECTOR_RECORD_CACHE.pop(path, None)
        return None
    signature = _path_signature(path)
    if signature is not None:
        with _RAG_VECTOR_CACHE_LOCK:
            cached = _RAG_VECTOR_RECORD_CACHE.get(path)
            if cached and cached[0] == signature:
                candidate = None
                for record in cached[1]:
                    if _local_vector_record_matches_chunk(record, document_id=document_id, chunk_id=chunk_id):
                        candidate = record
                return candidate
    candidate = None
    for line_no, line in _iter_local_vector_lines(path):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=500, detail=f"Invalid local vector store JSONL at line {line_no}.") from exc
        if not isinstance(record, dict):
            continue
        if not _local_vector_record_matches_chunk(record, document_id=document_id, chunk_id=chunk_id):
            continue
        validated = _validated_local_vector_record(record)
        if validated is not None:
            candidate = validated
    return candidate


def _iter_local_vector_lines(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            yield line_no, line


def _validated_local_vector_record(record: dict[str, Any]) -> dict[str, Any] | None:
    try:
        validated_records = validate_vector_records([record])
    except ValueError:
        return None
    for validated_record in validated_records:
        metadata = validated_record.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        if stable_content_hash(str(validated_record.get("text") or ""), metadata) != str(
            validated_record.get("content_hash") or ""
        ):
            continue
        return validated_record
    return None


def _local_vector_record_matches_chunk(
    record: dict[str, Any],
    *,
    document_id: str,
    chunk_id: str,
) -> bool:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    return (
        str(record.get("document_id") or metadata.get("document_id") or "") == document_id
        and str(record.get("chunk_id") or metadata.get("chunk_id") or "") == chunk_id
    )


def _local_vector_path(settings: Settings, auth: AuthContext) -> Path:
    return settings.data_dir / "vector_db" / tenant_storage_key(auth.tenant_id) / "approved_vectors.jsonl"


def _record_visible_to_request(
    record: dict[str, Any],
    *,
    request: RagSearchRequest,
    auth: AuthContext,
    repository: JsonRepository,
    repository_cache: _RagRequestRepositoryCache | None = None,
    approval_snapshot: dict[tuple[str, str], dict[str, Any]] | None = None,
    requested_department_ids: frozenset[str],
) -> bool:
    metadata_value = record.get("metadata")
    metadata = metadata_value if isinstance(metadata_value, dict) else {}
    if metadata.get("approval_status") != APPROVED_CHUNK_STATUS or not metadata.get("approval_id"):
        return False
    document_id = str(record.get("document_id") or metadata.get("document_id") or "")
    if request.document_id and document_id != request.document_id:
        return False
    record_profile_id = str(metadata.get("profile_id") or record.get("profile_id") or "").strip()
    if request.profile_id:
        requested_profile_id = str(request.profile_id).strip().casefold()
        if record_profile_id:
            if record_profile_id.casefold() != requested_profile_id:
                return False
        else:
            document = (
                repository_cache.get_document(document_id)
                if repository_cache is not None
                else repository.get_document(document_id)
            )
            document_profile_id = str(getattr(document, "profile_id", "") or "").strip().casefold()
            if not document_profile_id or document_profile_id != requested_profile_id:
                return False
    security_level = str(metadata.get("security_level") or "").strip().lower()
    if security_level not in _requested_security_levels(request, auth):
        return False
    department_acl = _department_acl_set(metadata.get("department_acl"))
    if department_acl and requested_department_ids and not requested_department_ids.intersection(department_acl):
        return False
    if department_acl and auth.role != API_ROLE_ADMIN:
        auth_departments = set(auth.department_ids)
        if not auth_departments.intersection(department_acl):
            return False
    chunk_id = str(record.get("chunk_id") or metadata.get("chunk_id") or "")
    if approval_snapshot is not None:
        current = approval_snapshot.get((document_id, chunk_id))
        if current is None:
            return False
        if (
            current.get("approval_id") != metadata.get("approval_id")
            or current.get("approved_content_hash") != metadata.get("approved_content_hash")
            or current.get("content_hash") != str(record.get("content_hash") or "")
        ):
            return False
        if security_level != current.get("security_level"):
            return False
        if department_acl != current.get("department_acl"):
            return False
        return True
    if stable_content_hash(str(record.get("text") or ""), metadata) != str(record.get("content_hash") or ""):
        return False
    if embedded_vector_integrity_reason(record):
        return False
    document = (
        repository_cache.get_document(document_id)
        if repository_cache is not None
        else repository.get_document(document_id)
    )
    if document is None or not resource_visible_to_tenant(document, auth.tenant_id):
        return False
    chunk = _current_repository_chunk(
        repository,
        document_id,
        chunk_id,
        repository_cache=repository_cache,
    )
    if chunk is None:
        return False
    if (
        chunk.approval_status != APPROVED_CHUNK_STATUS
        or chunk.approval_id != metadata.get("approval_id")
        or chunk.approved_content_hash != metadata.get("approved_content_hash")
    ):
        return False
    if security_level != str(chunk.security_level or "").strip().lower():
        return False
    if department_acl != _department_acl_set(chunk.department_acl):
        return False
    expected_record = _expected_vector_record_for_chunk(chunk, document, auth)
    if expected_record is None or str(expected_record.get("content_hash") or "") != str(record.get("content_hash") or ""):
        return False
    return True


def _load_cached_approval_snapshot(
    repository: JsonRepository,
    records: list[dict[str, Any]],
    auth: AuthContext,
) -> dict[tuple[str, str], dict[str, Any]]:
    document_ids = sorted(
        {
            str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "")
            for record in records
            if str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "").strip()
        }
    )
    sidecar_signature = _runtime_approval_snapshot_signature(repository, document_ids)
    cache_key = (repository.root, auth.tenant_id, tuple(document_ids))
    with _RAG_VECTOR_CACHE_LOCK:
        cached = _RAG_APPROVAL_SNAPSHOT_CACHE.get(cache_key)
        if sidecar_signature is not None and cached and cached[0] == sidecar_signature:
            return cached[1]
    if sidecar_signature is not None:
        sidecar_snapshot = _load_runtime_approval_snapshot_sidecar(repository, document_ids, auth)
        if sidecar_snapshot is not None:
            with _RAG_VECTOR_CACHE_LOCK:
                _RAG_APPROVAL_SNAPSHOT_CACHE[cache_key] = (sidecar_signature, sidecar_snapshot)
            return sidecar_snapshot

    signature = _approval_snapshot_signature(repository, document_ids)
    with _RAG_VECTOR_CACHE_LOCK:
        cached = _RAG_APPROVAL_SNAPSHOT_CACHE.get(cache_key)
        if cached and cached[0] == signature:
            return cached[1]
    snapshot = _build_approval_snapshot(repository, document_ids, auth)
    with _RAG_VECTOR_CACHE_LOCK:
        _RAG_APPROVAL_SNAPSHOT_CACHE[cache_key] = (signature, snapshot)
    return snapshot


def _runtime_approval_snapshot_signature(
    repository: JsonRepository,
    document_ids: list[str],
) -> tuple[Any, ...] | None:
    sidecar_path = _runtime_approval_snapshot_path(repository)
    if not sidecar_path.is_file():
        return None
    runtime_manifest_path = repository.root.parent / "mcp_runtime_manifest.json"
    if not runtime_manifest_path.is_file():
        return None
    return (
        "runtime_approval_snapshot_sidecar",
        tuple(document_ids),
        _path_signature(runtime_manifest_path),
        _path_signature(sidecar_path),
        _runtime_approval_snapshot_file_signatures(repository),
    )


def _runtime_approval_snapshot_path(repository: JsonRepository) -> Path:
    return repository.root / "approval_snapshot.json"


def _runtime_approval_snapshot_file_signatures(
    repository: JsonRepository,
) -> dict[str, tuple[int, ...] | None]:
    return {
        "repository_manifest": _path_signature(repository.manifest_path),
        # Legacy repositories are still readable for backward compatibility;
        # a mutation there must invalidate the runtime approval sidecar too.
        "legacy_repository": _path_signature(repository.legacy_path),
        "approval_journal": _path_signature(repository.root / "journals" / "approvals.jsonl"),
        # Approval/rejection and ACL changes are persisted to per-document chunk
        # files before their audit record is appended.  Include those files in
        # the sidecar contract so a failure between the two writes cannot leave
        # a stale approval snapshot authorizing an old vector record.
        "repository_chunk_files": _repository_chunk_files_signature(repository),
    }


def _repository_chunk_files_signature(repository: JsonRepository) -> tuple[int, int]:
    file_signatures = [
        (path.name, _chunk_path_identity_signature(path))
        for path in sorted(repository.root.glob("*_chunks.json"), key=lambda candidate: candidate.name)
    ]
    digest = hashlib.sha256(
        json.dumps(file_signatures, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return (int(digest[:16], 16), int(digest[16:32], 16))


def _chunk_path_identity_signature(path: Path) -> tuple[int, int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)


def _load_runtime_approval_snapshot_sidecar(
    repository: JsonRepository,
    document_ids: list[str],
    auth: AuthContext,
) -> dict[tuple[str, str], dict[str, Any]] | None:
    sidecar_path = _runtime_approval_snapshot_path(repository)
    runtime_manifest_path = repository.root.parent / "mcp_runtime_manifest.json"
    try:
        runtime_manifest = json.loads(runtime_manifest_path.read_text(encoding="utf-8-sig"))
        payload = json.loads(sidecar_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(runtime_manifest, dict) or runtime_manifest.get("report_type") != "mcp_runtime_data_bundle":
        return None
    if not isinstance(payload, dict) or payload.get("report_type") != "mcp_runtime_approval_snapshot":
        return None
    tenant_id = str(payload.get("tenant_id") or runtime_manifest.get("tenant_id") or "")
    if tenant_id and tenant_id != auth.tenant_id:
        return None
    manifest_ids = {
        str(value or "")
        for value in (runtime_manifest.get("document_ids") or payload.get("document_ids") or [])
        if str(value or "").strip()
    }
    sidecar_ids = {
        str(value or "")
        for value in (payload.get("document_ids") or [])
        if str(value or "").strip()
    }
    requested_ids = {document_id for document_id in document_ids if document_id}
    if not requested_ids.issubset(manifest_ids or sidecar_ids):
        return None
    if not requested_ids.issubset(sidecar_ids):
        return None
    payload_signatures = payload.get("file_signatures")
    if not isinstance(payload_signatures, dict):
        return None
    for key, expected in _runtime_approval_snapshot_file_signatures(repository).items():
        actual = payload_signatures.get(key)
        if (list(expected) if expected is not None else None) != actual:
            return None

    entries = payload.get("entries")
    if not isinstance(entries, list):
        return None
    snapshot: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        document_id = str(entry.get("document_id") or "")
        chunk_id = str(entry.get("chunk_id") or "")
        if document_id not in requested_ids or not chunk_id:
            continue
        security_level = str(entry.get("security_level") or "").strip().lower()
        if security_level not in SECURITY_LEVEL_ORDER:
            continue
        snapshot[(document_id, chunk_id)] = {
            "approval_id": entry.get("approval_id"),
            "approved_content_hash": entry.get("approved_content_hash"),
            "security_level": security_level,
            "department_acl": _department_acl_set(entry.get("department_acl")),
            "content_hash": str(entry.get("content_hash") or ""),
        }
    return snapshot


def _approval_snapshot_signature(repository: JsonRepository, document_ids: list[str]) -> tuple[Any, ...]:
    chunk_signatures = tuple(
        (document_id, _chunk_path_identity_signature(repository.root / f"{document_id}_chunks.json"))
        for document_id in document_ids
    )
    return (
        _repository_documents_signature(repository, document_ids),
        _path_signature(repository.legacy_path),
        chunk_signatures,
        _approval_journal_signature(repository, document_ids),
    )


def _approval_journal_signature(repository: JsonRepository, document_ids: list[str]) -> str:
    try:
        records = [
            record
            for document_id in document_ids
            for record in repository.list_approval_journal_records(document_id)
        ]
    except Exception:
        records = []
    payload = [
        {
            "approval_record_id": record.get("approval_record_id"),
            "approval_id": record.get("approval_id"),
            "document_id": record.get("document_id"),
            "chunk_ids": record.get("chunk_ids"),
            "approved_content_hashes": record.get("approved_content_hashes"),
            "worklist_evidence": record.get("worklist_evidence"),
            "tenant_id": record.get("tenant_id"),
            "approved_at": record.get("approved_at"),
        }
        for record in records
        if isinstance(record, dict)
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _repository_documents_signature(repository: JsonRepository, document_ids: list[str]) -> str:
    cache_key = (repository.root, tuple(document_ids))
    source_signature = (_path_signature(repository.manifest_path), _path_signature(repository.legacy_path))
    with _RAG_VECTOR_CACHE_LOCK:
        cached = _RAG_REPOSITORY_DOCUMENT_SIGNATURE_CACHE.get(cache_key)
        if cached and cached[0] == source_signature:
            return cached[1]
    try:
        manifest = repository._read_manifest()
        legacy = repository._read_legacy()
    except Exception:
        payload = [[document_id, None] for document_id in document_ids]
    else:
        manifest_documents = manifest.get("documents", {}) if isinstance(manifest, dict) else {}
        legacy_documents = legacy.get("documents", {}) if isinstance(legacy, dict) else {}
        payload = [
            [
                document_id,
                manifest_documents.get(document_id)
                if document_id in manifest_documents
                else legacy_documents.get(document_id),
            ]
            for document_id in document_ids
        ]
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    with _RAG_VECTOR_CACHE_LOCK:
        _RAG_REPOSITORY_DOCUMENT_SIGNATURE_CACHE[cache_key] = (source_signature, digest)
    return digest


def _build_approval_snapshot(
    repository: JsonRepository,
    document_ids: list[str],
    auth: AuthContext,
) -> dict[tuple[str, str], dict[str, Any]]:
    snapshot: dict[tuple[str, str], dict[str, Any]] = {}
    for document_id in document_ids:
        document = repository.get_document(document_id)
        if document is None or not resource_visible_to_tenant(document, auth.tenant_id):
            continue
        approval_journal_records = repository.list_approval_journal_records(document_id)
        for chunk in repository.get_chunks(document_id):
            if chunk.approval_status != APPROVED_CHUNK_STATUS or not chunk.approval_id:
                continue
            expected_record = _expected_vector_record_for_chunk(chunk, document, auth)
            if expected_record is None:
                continue
            expected_metadata = expected_record.get("metadata")
            if not isinstance(expected_metadata, dict):
                continue
            chunk_id = str(expected_record.get("chunk_id") or expected_metadata.get("chunk_id") or "")
            security_level = str(expected_metadata.get("security_level") or "").strip().lower()
            if not chunk_id or security_level not in SECURITY_LEVEL_ORDER:
                continue
            if not _has_matching_approval_journal_record(
                approval_journal_records,
                chunk=chunk,
                chunk_id=chunk_id,
                document_id=document_id,
                tenant_id=auth.tenant_id,
                expected_metadata=expected_metadata,
            ):
                continue
            snapshot[(document_id, chunk_id)] = {
                "approval_id": expected_metadata.get("approval_id"),
                "approved_content_hash": expected_metadata.get("approved_content_hash"),
                "security_level": security_level,
                "department_acl": _department_acl_set(expected_metadata.get("department_acl")),
                "content_hash": str(expected_record.get("content_hash") or ""),
            }
    return snapshot


def _has_matching_approval_journal_record(
    records: list[dict[str, Any]],
    *,
    chunk: Any,
    chunk_id: str,
    document_id: str,
    tenant_id: str,
    expected_metadata: dict[str, Any],
) -> bool:
    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("document_id") or "") != document_id:
            continue
        if str(record.get("tenant_id") or "") != tenant_id:
            continue
        if str(record.get("approval_id") or "") != str(chunk.approval_id or ""):
            continue
        if chunk_id not in {str(value) for value in (record.get("chunk_ids") or [])}:
            continue
        if _approval_record_chunk_hash(record, chunk_id) != str(chunk.approved_content_hash or ""):
            continue
        worklist_evidence = record.get("worklist_evidence") or {}
        if not isinstance(worklist_evidence, dict):
            continue
        expected_worklist_metadata = approval_worklist_metadata(worklist_evidence)
        if set(expected_worklist_metadata) != set(APPROVAL_WORKLIST_METADATA_KEYS):
            continue
        if any(str(expected_metadata.get(key) or "") != str(value or "") for key, value in expected_worklist_metadata.items()):
            continue
        return True
    return False


def _approval_record_chunk_hash(record: dict[str, Any], chunk_id: str) -> str:
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


def _expected_vector_record_for_chunk(chunk: Any, document: Any, auth: AuthContext) -> dict[str, Any] | None:
    chunk_data = chunk.model_dump(mode="json")
    metadata = dict(chunk_data.get("metadata") or {})
    for key, value in {
        "institution_name": getattr(document, "institution_name", None),
        "apba_id": getattr(document, "apba_id", None),
        "source_system": getattr(document, "source_system", None),
        "source_url": getattr(document, "source_url", None),
        "source_record_id": getattr(document, "source_record_id", None),
        "source_file_id": getattr(document, "source_file_id", None),
        "source_disclosure_date": getattr(document, "source_disclosure_date", None),
        "source_posted_date": getattr(document, "source_posted_date", None),
        "profile_id": getattr(document, "profile_id", None),
    }.items():
        if value and not metadata.get(key):
            metadata[key] = value
    metadata["tenant_id"] = document.tenant_id or auth.tenant_id
    chunk_data["tenant_id"] = document.tenant_id or auth.tenant_id
    chunk_data["department_acl"] = sorted(_department_acl_set(chunk.department_acl))
    chunk_data["metadata"] = metadata
    return vector_record_from_chunk(chunk_data)


def _current_repository_chunk(
    repository: JsonRepository,
    document_id: str,
    chunk_id: str,
    *,
    repository_cache: _RagRequestRepositoryCache | None = None,
):
    if repository_cache is not None:
        return repository_cache.get_chunk(document_id, chunk_id)
    for chunk in repository.get_chunks(document_id):
        if chunk.chunk_id == chunk_id:
            return chunk
    return None


def _validate_security_scope(request: RagSearchRequest, auth: AuthContext) -> None:
    requested = _requested_security_levels(request, auth)
    allowed = ROLE_SECURITY_LEVELS.get(auth.role, frozenset())
    if not requested.issubset(allowed):
        raise HTTPException(status_code=403, detail="Requested security level is not allowed for this API role.")


def _feedback_allowed_for_trace(trace: dict[str, Any], auth: AuthContext) -> bool:
    if str(trace.get("actor") or "") == auth.actor:
        return True
    return auth.role == API_ROLE_ADMIN


def _requested_department_ids(request: RagSearchRequest, auth: AuthContext) -> frozenset[str]:
    requested = frozenset(_department_acl_set(request.department_ids))
    if not requested:
        return frozenset()
    if auth.role == API_ROLE_ADMIN:
        return requested
    allowed = frozenset(str(item) for item in auth.department_ids)
    if not requested.issubset(allowed):
        raise HTTPException(status_code=403, detail="Requested department is not allowed for this API token.")
    return requested


def _validate_query_policy(query: str) -> None:
    normalized = " ".join(str(query or "").split())
    for pattern in BLOCKED_QUERY_PATTERNS:
        if pattern.search(normalized):
            raise HTTPException(status_code=400, detail="Query was blocked by the local RAG input policy.")


def _enforce_rag_rate_limit(settings: Settings, auth: AuthContext) -> None:
    limit = int(settings.rag_rate_limit_requests_per_window or 0)
    window_seconds = int(settings.rag_rate_limit_window_seconds or 0)
    if limit <= 0 or window_seconds <= 0:
        return
    now = time.monotonic()
    key = (auth.tenant_id, auth.actor)
    with _RAG_RATE_LIMIT_LOCK:
        bucket_limit = max(1, int(_RAG_RATE_LIMIT_MAX_BUCKETS))
        if key not in _RAG_RATE_LIMIT_BUCKETS and len(_RAG_RATE_LIMIT_BUCKETS) >= bucket_limit:
            cutoff = now - window_seconds
            while _RAG_RATE_LIMIT_BUCKETS:
                _, oldest_timestamps = next(iter(_RAG_RATE_LIMIT_BUCKETS.items()))
                if oldest_timestamps and oldest_timestamps[-1] > cutoff:
                    break
                _RAG_RATE_LIMIT_BUCKETS.popitem(last=False)
            while len(_RAG_RATE_LIMIT_BUCKETS) >= bucket_limit:
                _RAG_RATE_LIMIT_BUCKETS.popitem(last=False)
        bucket = [timestamp for timestamp in _RAG_RATE_LIMIT_BUCKETS.get(key, []) if now - timestamp < window_seconds]
        if len(bucket) >= limit:
            _RAG_RATE_LIMIT_BUCKETS[key] = bucket
            _RAG_RATE_LIMIT_BUCKETS.move_to_end(key)
            retry_after = max(1, int(window_seconds - (now - bucket[0])))
            raise HTTPException(status_code=429, detail=f"RAG rate limit exceeded. Retry after {retry_after} seconds.")
        bucket.append(now)
        _RAG_RATE_LIMIT_BUCKETS[key] = bucket
        _RAG_RATE_LIMIT_BUCKETS.move_to_end(key)


def _requested_security_levels(request: RagSearchRequest, auth: AuthContext) -> frozenset[str]:
    allowed = ROLE_SECURITY_LEVELS.get(auth.role, frozenset())
    if not request.security_levels:
        return allowed
    return frozenset(str(level or "").strip().lower() for level in request.security_levels if str(level or "").strip())


def _department_acl_set(value: Any) -> set[str]:
    if value is None:
        return set()
    return set(normalize_department_ids(value))


def _score_records(
    query: str,
    records: list[dict[str, Any]],
    *,
    settings: Settings,
    auth: AuthContext,
    all_records: list[dict[str, Any]] | None = None,
) -> tuple[list[tuple[float, dict[str, Any]]], dict[str, Any]]:
    vector_path = _local_vector_path(settings, auth)
    index_path = default_bm25_index_path(vector_path)
    index = _load_cached_bm25_index(index_path)
    if index is not None and index.structured_metadata_version < BM25_STRUCTURED_METADATA_VERSION:
        index_records = records
        index_source_hash = source_content_hashes(index_records)
        rebuilt_index = _load_cached_rebuilt_bm25_index(index_path, index_source_hash)
        if rebuilt_index is None:
            rebuilt_index = Bm25Index.build(records)
            _store_cached_rebuilt_bm25_index(index_path, index_source_hash, rebuilt_index)
        index = rebuilt_index
    else:
        index_records = all_records or records
        index_source_hash = (
            _cached_vector_source_content_hash(vector_path, index_records)
            if all_records is not None
            else source_content_hashes(index_records)
        )
    return search_retrieval_records(
        query,
        records,
        index,
        len(records),
        index_records=index_records,
        index_source_content_hashes=index_source_hash,
    )


def _load_cached_bm25_index(path: Path):
    signature = _path_signature(path)
    if signature is None:
        with _RAG_VECTOR_CACHE_LOCK:
            _RAG_BM25_INDEX_CACHE.pop(path, None)
        return None
    with _RAG_VECTOR_CACHE_LOCK:
        cached = _RAG_BM25_INDEX_CACHE.get(path)
        if cached and cached[0] == signature:
            return cached[1]
    index = load_bm25_index(path)
    if index is not None:
        with _RAG_VECTOR_CACHE_LOCK:
            _RAG_BM25_INDEX_CACHE[path] = (signature, index)
    else:
        with _RAG_VECTOR_CACHE_LOCK:
            _RAG_BM25_INDEX_CACHE.pop(path, None)
    return index


def _store_cached_bm25_index(path: Path, index: Bm25Index) -> None:
    signature = _path_signature(path)
    if signature is None:
        return
    with _RAG_VECTOR_CACHE_LOCK:
        _RAG_BM25_INDEX_CACHE[path] = (signature, index)


def _load_cached_rebuilt_bm25_index(path: Path, source_hash: str):
    signature = _path_signature(path)
    if signature is None:
        with _RAG_VECTOR_CACHE_LOCK:
            _RAG_REBUILT_BM25_INDEX_CACHE.pop(path, None)
        return None
    with _RAG_VECTOR_CACHE_LOCK:
        cached = _RAG_REBUILT_BM25_INDEX_CACHE.get(path)
        if cached and cached[0] == signature and cached[1] == source_hash:
            return cached[2]
    return None


def _store_cached_rebuilt_bm25_index(path: Path, source_hash: str, index: Bm25Index) -> None:
    signature = _path_signature(path)
    if signature is None:
        return
    with _RAG_VECTOR_CACHE_LOCK:
        _RAG_REBUILT_BM25_INDEX_CACHE[path] = (signature, source_hash, index)


def _cached_vector_source_content_hash(vector_path: Path, records: list[dict[str, Any]]) -> str:
    signature = _path_signature(vector_path)
    if signature is not None:
        with _RAG_VECTOR_CACHE_LOCK:
            cached = _RAG_VECTOR_SOURCE_HASH_CACHE.get(vector_path)
            if cached and cached[0] == signature:
                return cached[1]
    content_hashes = source_content_hashes(records)
    if signature is not None:
        with _RAG_VECTOR_CACHE_LOCK:
            _RAG_VECTOR_SOURCE_HASH_CACHE[vector_path] = (signature, content_hashes)
    return content_hashes


def _path_signature(path: Path) -> _FileIdentitySignature | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    # Preserve the historical `(mtime_ns, size)` prefix because MCP warmup
    # accounting reads index 1 as the byte count; ctime/inode extend identity.
    return (stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns, stat.st_ino)


def _perf_elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 3)


def _validate_response_metadata_profile(metadata_profile: str) -> str:
    normalized = str(metadata_profile or "full").strip().lower()
    if not normalized:
        normalized = "full"
    if normalized not in _RAG_RESPONSE_METADATA_PROFILES:
        raise HTTPException(status_code=400, detail="metadata_profile must be full, external, or chatgpt-data.")
    return normalized


def _rag_results_for_metadata_profile(results: list[dict[str, Any]], metadata_profile: str) -> list[dict[str, Any]]:
    return [_rag_result_for_metadata_profile(result, metadata_profile) for result in results]


def _rag_result_for_metadata_profile(result: dict[str, Any], metadata_profile: str) -> dict[str, Any]:
    normalized = _validate_response_metadata_profile(metadata_profile)
    if normalized not in _EXTERNAL_RAG_RESPONSE_METADATA_PROFILES:
        return result
    return {key: value for key, value in result.items() if key not in _INTERNAL_RAG_RESPONSE_METADATA_KEYS}


def _rag_chat_citation_for_metadata_profile(result: dict[str, Any], metadata_profile: str) -> dict[str, Any]:
    citation = {
        "chunk_id": result["chunk_id"],
        "document_id": result["document_id"],
        "approval_id": result["approval_id"],
        "approval_worklist_report_sha256": result.get("approval_worklist_report_sha256") or "",
        "approval_review_batch_manifest_path": result.get("approval_review_batch_manifest_path") or "",
        "approval_review_batch_manifest_sha256": result.get("approval_review_batch_manifest_sha256") or "",
        "approval_review_batch_id": result.get("approval_review_batch_id") or "",
        "approval_review_batch_chunk_fingerprint": result.get("approval_review_batch_chunk_fingerprint") or "",
        "approval_review_strategy": result.get("approval_review_strategy") or "",
        "parser_uncertainty_source": result.get("parser_uncertainty_source") or "",
        "parser_uncertainty_risk_level": result.get("parser_uncertainty_risk_level") or "",
        "parser_uncertainty_confidence": result.get("parser_uncertainty_confidence"),
        "parser_uncertainty_flags": result.get("parser_uncertainty_flags") or [],
        "parser_uncertainty_recommendation": result.get("parser_uncertainty_recommendation") or "",
        "score": result["score"],
    }
    return _rag_result_for_metadata_profile(citation, metadata_profile)


def _public_search_result(
    record: dict[str, Any],
    score: float,
    *,
    related_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    metadata = record.get("metadata") or {}
    governing_article = _governing_article_for_reference_chunk(record, related_records or [])
    return {
        "score": score,
        "document_id": record.get("document_id") or metadata.get("document_id") or "",
        "chunk_id": record.get("chunk_id") or metadata.get("chunk_id") or "",
        "text": str(record.get("text") or ""),
        "document_name": metadata.get("document_name") or "",
        "institution_name": metadata.get("institution_name") or "",
        "apba_id": metadata.get("apba_id") or "",
        "source_system": metadata.get("source_system") or "",
        "source_url": metadata.get("source_url") or "",
        "source_record_id": metadata.get("source_record_id") or "",
        "source_file_id": metadata.get("source_file_id") or "",
        "profile_id": metadata.get("profile_id") or record.get("profile_id") or "",
        "regulation_id": metadata.get("regulation_id") or record.get("regulation_id") or "",
        "regulation_version": metadata.get("regulation_version") or record.get("regulation_version") or "",
        "regulation_status": metadata.get("regulation_status") or record.get("regulation_status") or "",
        "chunk_type": metadata.get("chunk_type") or "",
        "hierarchy_path": metadata.get("hierarchy_path") or "",
        "part_title": metadata.get("part_title") or "",
        "chapter_title": metadata.get("chapter_title") or "",
        "regulation_title": metadata.get("regulation_title") or "",
        "article_no": metadata.get("article_no") or "",
        "article_title": metadata.get("article_title") or "",
        "article_refs": metadata.get("article_refs") or [],
        "appendix_refs": metadata.get("appendix_refs") or [],
        "form_refs": metadata.get("form_refs") or [],
        "reference_edges": metadata.get("reference_edges") or [],
        "governing_article_no": governing_article.get("article_no", ""),
        "governing_article_title": governing_article.get("article_title", ""),
        "governing_article_chunk_id": governing_article.get("chunk_id", ""),
        "governing_article_match_ref": governing_article.get("match_ref", ""),
        "source_page_start": metadata.get("source_page_start"),
        "source_page_end": metadata.get("source_page_end"),
        "effective_date": metadata.get("effective_date") or "",
        "revision_date": metadata.get("revision_date") or "",
        "effective_from": metadata.get("effective_from"),
        "effective_to": metadata.get("effective_to"),
        "repealed_at": metadata.get("repealed_at"),
        "supersedes_document_id": metadata.get("supersedes_document_id") or "",
        "valid_from": metadata.get("valid_from") or "",
        "valid_to": metadata.get("valid_to") or "",
        "revision_history": metadata.get("revision_history") or [],
        "revision_history_spans": metadata.get("revision_history_spans") or [],
        "article_effective_overrides": metadata.get("article_effective_overrides") or [],
        "article_validity_windows": metadata.get("article_validity_windows") or [],
        "supplementary_identifier_date": metadata.get("supplementary_identifier_date") or "",
        "temporal_metadata_inherited": bool(metadata.get("temporal_metadata_inherited")),
        "temporal_metadata_scope": metadata.get("temporal_metadata_scope") or "",
        "temporal_metadata_inherited_fields": metadata.get("temporal_metadata_inherited_fields") or [],
        "temporal_metadata_normalized_fields": metadata.get("temporal_metadata_normalized_fields") or [],
        "temporal_metadata_conflict_fields": metadata.get("temporal_metadata_conflict_fields") or [],
        "security_level": metadata.get("security_level") or "",
        "approval_status": metadata.get("approval_status") or "",
        "approval_id": metadata.get("approval_id") or "",
        "approval_worklist_report_sha256": metadata.get("approval_worklist_report_sha256") or "",
        "approval_review_batch_manifest_path": metadata.get("approval_review_batch_manifest_path") or "",
        "approval_review_batch_manifest_sha256": metadata.get("approval_review_batch_manifest_sha256") or "",
        "approval_review_batch_id": metadata.get("approval_review_batch_id") or "",
        "approval_review_batch_chunk_fingerprint": metadata.get("approval_review_batch_chunk_fingerprint") or "",
        "approval_review_strategy": metadata.get("approval_review_strategy") or "",
        "content_hash": str(record.get("content_hash") or ""),
        "approved_content_hash": str(metadata.get("approved_content_hash") or ""),
        "answer_profile_version": metadata.get("answer_profile_version") or "",
        "answer_intents": metadata.get("answer_intents") or [],
        "answer_keywords": metadata.get("answer_keywords") or [],
        "answer_facts": metadata.get("answer_facts") or [],
        "answer_outline": metadata.get("answer_outline") or [],
        "source_hwpx_block_types": metadata.get("source_hwpx_block_types") or [],
        "source_hwpx_parser_review_flags": metadata.get("source_hwpx_parser_review_flags") or [],
        "source_hwpx_xml_block_indices": metadata.get("source_hwpx_xml_block_indices") or [],
        "source_hwpx_table_direct_captions": metadata.get("source_hwpx_table_direct_captions") or [],
        "source_hwpx_table_image_captions": metadata.get("source_hwpx_table_image_captions") or [],
        "source_hwpx_table_note_snippets": metadata.get("source_hwpx_table_note_snippets") or [],
        "source_hwpx_nested_table_text_snippets": metadata.get("source_hwpx_nested_table_text_snippets") or [],
        "source_hwp_extraction_modes": metadata.get("source_hwp_extraction_modes") or [],
        "source_hwp_streams": metadata.get("source_hwp_streams") or [],
        "source_hwp_section_indices": metadata.get("source_hwp_section_indices") or [],
        "source_hwp_native_table_geometry": metadata.get("source_hwp_native_table_geometry"),
        "table_source": metadata.get("table_source") or "",
        "table_geometry_source": metadata.get("table_geometry_source") or "",
        "primary_parser_table_source": metadata.get("primary_parser_table_source") or "",
        "kordoc_table_parser_status": metadata.get("kordoc_table_parser_status") or "",
        "kordoc_table_count": metadata.get("kordoc_table_count"),
        "kordoc_table_promoted": bool(metadata.get("kordoc_table_promoted")),
        "kordoc_table_promotion_review_required": bool(
            metadata.get("kordoc_table_promotion_review_required")
        ),
        "kordoc_table_unmatched_source": bool(metadata.get("kordoc_table_unmatched_source")),
        "kordoc_table_match": metadata.get("kordoc_table_match") or {},
        "kordoc_table_match_review_required": bool(metadata.get("kordoc_table_match_review_required")),
        "kordoc_table_match_provisional": bool(metadata.get("kordoc_table_match_provisional")),
        "parser_uncertainty_source": metadata.get("parser_uncertainty_source") or "",
        "parser_uncertainty_risk_level": metadata.get("parser_uncertainty_risk_level") or "",
        "parser_uncertainty_confidence": metadata.get("parser_uncertainty_confidence"),
        "parser_uncertainty_flags": metadata.get("parser_uncertainty_flags") or [],
        "parser_uncertainty_recommendation": metadata.get("parser_uncertainty_recommendation") or "",
        "parser_uncertainty_remediation_hint": metadata.get("parser_uncertainty_remediation_hint") or "",
    }


def _has_complete_lifecycle_metadata(record: dict[str, Any]) -> bool:
    metadata = read_regulation_metadata(record)
    return bool(metadata.regulation_id and metadata.version and metadata.effective_from)


def _normalized_lifecycle_as_of(value: str | None) -> str:
    if value and value.strip():
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError:
            return "invalid"
    return date.today().isoformat()


def _governing_article_for_reference_chunk(
    record: dict[str, Any],
    related_records: list[dict[str, Any]],
) -> dict[str, str]:
    metadata = record.get("metadata") or {}
    if metadata.get("article_no") and metadata.get("article_title"):
        return {}
    reference_labels = _normalized_reference_labels(
        [
            *(metadata.get("form_refs") or []),
            *(metadata.get("appendix_refs") or []),
        ]
    )
    if not reference_labels:
        return {}
    document_id = str(record.get("document_id") or metadata.get("document_id") or "")
    chunk_id = str(record.get("chunk_id") or metadata.get("chunk_id") or "")
    matches: dict[str, dict[str, str]] = {}
    for candidate in related_records:
        candidate_metadata = candidate.get("metadata") or {}
        candidate_document_id = str(candidate.get("document_id") or candidate_metadata.get("document_id") or "")
        candidate_chunk_id = str(candidate.get("chunk_id") or candidate_metadata.get("chunk_id") or "")
        if candidate_document_id != document_id or candidate_chunk_id == chunk_id:
            continue
        article_no = str(candidate_metadata.get("article_no") or "").strip()
        article_title = str(candidate_metadata.get("article_title") or "").strip()
        if not article_no or not article_title:
            continue
        if not _same_reference_context(metadata, candidate_metadata):
            continue
        matched_ref = _candidate_references_any_label(candidate, reference_labels)
        if not matched_ref:
            continue
        key = f"{article_no}\n{article_title}\n{candidate_chunk_id}"
        matches[key] = {
            "article_no": article_no,
            "article_title": article_title,
            "chunk_id": candidate_chunk_id,
            "match_ref": matched_ref,
        }
    if len(matches) != 1:
        return {}
    return next(iter(matches.values()))


def _same_reference_context(source_metadata: dict[str, Any], candidate_metadata: dict[str, Any]) -> bool:
    source_context = _reference_context_values(source_metadata)
    candidate_context = _reference_context_values(candidate_metadata)
    if not source_context and not candidate_context:
        return True
    return bool(source_context & candidate_context)


def _reference_context_values(metadata: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in (
        "regulation_no",
        "regulation_title",
        "chapter_title",
        "section_title",
    ):
        normalized = _normalize_reference_context(metadata.get(key))
        if normalized:
            values.add(normalized)
    return values


def _normalize_reference_context(value: Any) -> str:
    return " ".join(str(value or "").split()).lower()


def _candidate_references_any_label(record: dict[str, Any], labels: set[str]) -> str:
    metadata = record.get("metadata") or {}
    candidate_refs = _normalized_reference_labels(
        [
            *(metadata.get("form_refs") or []),
            *(metadata.get("appendix_refs") or []),
        ]
    )
    for label in sorted(labels):
        if label in candidate_refs:
            return label
    compact_text = _normalize_reference_label(
        " ".join(str(value or "") for value in (record.get("text"), metadata.get("retrieval_text")))
    )
    for label in sorted(labels):
        # Bounded match: labels are space-stripped, so a plain substring test
        # treats "별표2" as a prefix of "별표21".  Require the label not be
        # followed by another digit so numbered siblings don't collide.
        if label and re.search(re.escape(label) + r"(?!\d)", compact_text):
            return label
    return ""


def _normalized_reference_labels(values: list[Any]) -> set[str]:
    labels: set[str] = set()
    for value in values:
        normalized = _normalize_reference_label(str(value or ""))
        if normalized:
            labels.add(normalized)
    return labels


def _normalize_reference_label(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", str(value or "")).lower()


def _extractive_answer(query: str, results: list[dict[str, Any]]) -> str:
    return build_structured_extractive_answer(query, results)


def _chat_backend(request: RagChatRequest, settings: Settings) -> str:
    backend = str(request.llm_backend or settings.rag_llm_backend or "extractive").strip().lower()
    if backend == "extractive":
        return backend
    if backend in {"ollama", "llama-cpp", "openai-compatible"}:
        if not local_llm_available(settings):
            raise HTTPException(status_code=503, detail="Configured local LLM backend is not available.")
        return backend
    raise HTTPException(status_code=400, detail="Unsupported local RAG chat backend.")


def _chat_answer(backend: str, settings: Settings, query: str, results: list[dict[str, Any]]) -> str:
    if backend == "extractive":
        return _sanitize_rag_answer(_extractive_answer(query, results))
    if not results:
        return "승인된 규정 근거에서 확인할 수 없습니다."
    try:
        answer = generate_local_llm_answer(settings=settings, query=query, evidence=results)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Local LLM backend request failed: {type(exc).__name__}") from exc
    if not answer:
        return _sanitize_rag_answer(_extractive_answer(query, results))
    return _sanitize_rag_answer(answer)


def _sanitize_rag_answer(answer: str) -> str:
    return sanitize_rag_answer(answer)


def request_backend_status(settings: Settings) -> str:
    return str(settings.rag_llm_backend or "extractive").strip().lower()


def _rag_trace(
    *,
    action: str,
    request: RagSearchRequest,
    auth: AuthContext,
    results: list[dict[str, Any]],
    extra: dict[str, Any],
) -> dict[str, Any]:
    return {
        "trace_id": f"rag_{uuid4().hex[:12]}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "actor": auth.actor,
        "tenant_id": auth.tenant_id,
        "auth_mode": auth.auth_mode,
        "api_role": auth.role,
        "query_hash": hashlib.sha256(request.query.encode("utf-8")).hexdigest(),
        "top_k": request.top_k,
        "security_levels": sorted(_requested_security_levels(request, auth)),
        "department_ids": sorted(str(item) for item in auth.department_ids),
        "requested_department_ids": sorted(_requested_department_ids(request, auth)),
        "result_count": len(results),
        "result_refs": [
            {
                "document_id": result["document_id"],
                "chunk_id": result["chunk_id"],
                "approval_id": result["approval_id"],
                "score": result["score"],
            }
            for result in results
        ],
        **extra,
    }
