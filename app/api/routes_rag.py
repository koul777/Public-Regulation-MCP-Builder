from __future__ import annotations

from collections import OrderedDict, defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from datetime import date, datetime, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Iterable, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.api_audit import audit_api_event
from app.core.config import Settings, get_settings
from app.core.input_limits import (
    MAX_MCP_QUERY_CHARS,
    MAX_MCP_TOP_K,
)
from app.core.mcp_input_schemas import (
    McpDepartmentIds,
    McpOptionalIdentifier,
    McpSecurityLevels,
)
from app.core.security import (
    API_READ_ROLES,
    API_ROLE_ADMIN,
    ROLE_SECURITY_LEVELS,
    SECURITY_LEVEL_ORDER,
    AuthContext,
    coerce_auth_context,
    get_auth_context,
    normalize_department_ids,
    require_api_role,
)
from app.core.tenant_access import (
    resource_visible_to_tenant,
    settings_for_tenant,
    tenant_directory_key,
)
from app.agents.citation_verifier import CitationVerifierAgent
from app.agents.claim_auditor import ClaimAuditAgent
from app.agents.grounded_qa import GroundedAnswerDraft, GroundedQwenAnswerAgent
from app.agents.grounded_answer_agent import GroundedAnswerAgent
from app.agents.model_router import (
    QWEN3_ANSWER_MODEL,
    QWEN3_EMBEDDING_MODEL,
    QWEN3_RERANKER_MODEL,
    model_profile_manifest,
)
from app.agents.ollama_runtime import OllamaRuntime
from app.agents.query_agents import (
    QueryAnalysis,
    QueryAnalysisAgent,
    QueryRewriteAgent,
    deterministic_query_analysis,
)
from app.agents.role_registry import workflow_roles
from app.ingestion.embedding_adapter import LOCAL_HASH_EMBEDDING_MODEL
from app.ingestion.vector_adapter import APPROVED_CHUNK_STATUS, stable_content_hash, vector_record_from_chunk
from app.ingestion.vector_integrity import embedded_vector_integrity_reason
from app.ingestion.vector_upsert import validate_vector_records
from app.rag.local_llm import (
    generate_local_llm_answer as generate_local_llm_answer,
    local_llm_available,
    probe_local_llm,
)
from app.rag.context_builder import ContextBuilder, GroundingContext
from app.rag.output_filter import sanitize_rag_answer
from app.rag.extractive_answer import (
    NO_EVIDENCE_ANSWER,
    build_structured_extractive_answer,
    select_supporting_answer_results,
)
from app.pipelines.definitions import LOCAL_QA_PIPELINE_ID, PipelineStageTracker
from app.retrieval.bm25_index import (
    BM25_RETRIEVAL_MODEL,
    BM25_STRUCTURED_METADATA_VERSION,
    Bm25Index,
    default_bm25_index_path,
    load_bm25_index,
    source_content_hashes,
)
from app.retrieval.searcher import search as search_retrieval_records
from app.retrieval.semantic_models import Qwen3RerankerAdapter
from app.retrieval.semantic_models import semantic_runtime_available
from app.parsers.paddle_ocr import paddle_ocr_available
from app.services.review_decision_service import APPROVAL_WORKLIST_METADATA_KEYS, approval_worklist_metadata
from app.services.regulation_catalog_service import filter_to_latest_active_versions, read_regulation_metadata
from app.services import regulation_rag_runtime as rag_runtime
from app.storage.repository import JsonRepository


router = APIRouter(prefix="/api/rag", tags=["rag"])

RagChatProgressEvent = dict[str, Any]
RagChatProgressCallback = Callable[[RagChatProgressEvent], None]
_RAG_CHAT_PROGRESS_CALLBACK: ContextVar[RagChatProgressCallback | None] = ContextVar(
    "rag_chat_progress_callback",
    default=None,
)


@contextmanager
def rag_chat_progress(callback: RagChatProgressCallback) -> Iterator[None]:
    """Expose real chat pipeline stages to a local UI without changing the API contract."""

    token = _RAG_CHAT_PROGRESS_CALLBACK.set(callback)
    try:
        yield
    finally:
        _RAG_CHAT_PROGRESS_CALLBACK.reset(token)


def _emit_rag_chat_progress(stage: str, progress: int, label: str) -> None:
    """Report best-effort UI progress; a display callback can never alter RAG security."""

    callback = _RAG_CHAT_PROGRESS_CALLBACK.get()
    if callback is None:
        return
    try:
        callback(
            {
                "stage": str(stage),
                "progress": max(0, min(100, int(progress))),
                "label": str(label),
            }
        )
    except Exception:
        # Progress is operator feedback only. Approval, tenant and citation gates
        # must continue to run even if a UI consumer disappears mid-request.
        return

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
_RAG_RERANKER_LOCK = threading.Lock()
_RAG_VISIBLE_RECORDS_CACHE: OrderedDict[tuple[Any, ...], list[dict[str, Any]]] = OrderedDict()
_RAG_VISIBLE_RECORDS_MAX_ENTRIES = 512
_RAG_VECTOR_SOURCE_HASH_CACHE: dict[Path, tuple[_FileIdentitySignature, str]] = {}
_RAG_REPOSITORY_DOCUMENT_SIGNATURE_CACHE: dict[
    tuple[Path, tuple[str, ...]],
    tuple[tuple[Any, Any], str],
] = {}
_RAG_APPROVAL_JOURNAL_CACHE: OrderedDict[
    Path,
    tuple[_FileIdentitySignature | None, dict[str, tuple[dict[str, Any], ...]]],
] = OrderedDict()
_RAG_APPROVAL_JOURNAL_CACHE_MAX_ENTRIES = 128
_RAG_APPROVAL_SNAPSHOT_CACHE: dict[
    tuple[Path, str, tuple[str, ...]],
    tuple[tuple[Any, ...], dict[tuple[str, str], dict[str, Any]]],
] = {}
_RAG_RUNTIME_APPROVAL_IDENTITY_CACHE: OrderedDict[
    tuple[Path, str, tuple[str, ...]],
    tuple[tuple[Any, ...], dict[tuple[str, str], dict[str, Any]]],
] = OrderedDict()
_RAG_RUNTIME_APPROVAL_IDENTITY_CACHE_MAX_ENTRIES = 128
_RUNTIME_CONTENT_SIGNATURE_LOCK = threading.Lock()
_RUNTIME_CONTENT_SIGNATURE_CACHE: dict[
    Path, tuple[_FileIdentitySignature, tuple[int, str]]
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
    orchestration_mode: Literal["auto", "legacy", "multi_model"] = "auto"
    retrieval_mode: Literal["auto", "fast"] = "auto"


class RagChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=6000)


class RagChatRequest(RagSearchRequest):
    llm_backend: str | None = Field(default=None, max_length=40)
    history: list[RagChatMessage] = Field(default_factory=list, max_length=12)
    claim_audit_mode: Literal["model", "deterministic"] = "model"


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
    multi_model = _use_multi_model_orchestration(request, settings)
    fast_retrieval = request.retrieval_mode == "fast"
    multi_model_retrieval = multi_model and not fast_retrieval
    deterministic_analysis = deterministic_query_analysis(request.query)
    exact_locator_fast_path = bool(
        request.document_id and _is_exact_article_locator_query(deterministic_analysis)
    )
    exact_locator_scored = (
        _exact_article_locator_matches(deterministic_analysis, visible_records)
        if exact_locator_fast_path
        else []
    )
    query_execution: dict[str, Any] = {
        "enabled": False,
        "analysis_mode": "deterministic_existing_query",
        "rewrite_mode": "deterministic_existing_query",
        "intent": "general",
        "locator_count": 0,
        "search_query_count": 1,
        "search_queries": [request.query],
    }
    if exact_locator_fast_path:
        query_execution = {
            "enabled": False,
            "analysis_mode": "deterministic_exact_locator",
            "rewrite_mode": "deterministic_exact_locator",
            "intent": deterministic_analysis.intent,
            "locator_count": len(deterministic_analysis.locators),
            "search_query_count": 1,
            "search_queries": [request.query],
            "query_model": "deterministic",
            "query_fallback_reason": "",
            "rewrite_fallback_reason": "",
            "exact_locator_fast_path": True,
        }
        _emit_rag_chat_progress(
            "retrieval",
            34,
            "선택한 규정에서 조문 번호가 정확히 일치하는 승인 조항을 확인하는 중",
        )
    elif multi_model_retrieval:
        step_started_at = time.perf_counter()
        _emit_rag_chat_progress(
            "query_analysis",
            12,
            "Qwen3 1.7B가 질문의 검색 조건을 분석하는 중",
        )
        runtime = OllamaRuntime(settings.rag_llm_endpoint)
        analysis = QueryAnalysisAgent(runtime).analyze(request.query)
        rewrite = QueryRewriteAgent(runtime).rewrite(analysis)
        query_execution = {
            "enabled": True,
            "analysis_mode": analysis.analysis_mode,
            "rewrite_mode": rewrite.rewrite_mode,
            "intent": analysis.intent,
            "locator_count": len(analysis.locators),
            "search_query_count": len(rewrite.search_queries),
            "search_queries": list(rewrite.search_queries),
            "query_model": analysis.model or "deterministic",
            "query_fallback_reason": analysis.fallback_reason or "",
            "rewrite_fallback_reason": rewrite.fallback_reason or "",
        }
        timing_ms["query_agents_elapsed_ms"] = _perf_elapsed_ms(step_started_at)
    step_started_at = time.perf_counter()
    if exact_locator_fast_path:
        scored = exact_locator_scored
        retrieval = {
            "retrieval_model": "deterministic-exact-article-v1",
            "semantic_embedding_model": "not_used",
            "retrieval_fallback": False,
            "bm25_index_status": "bypassed_exact_locator",
            "query_expanded": False,
            "exact_locator_fast_path": True,
            "exact_locator_match_count": len(scored),
        }
    else:
        _emit_rag_chat_progress(
            "retrieval",
            24,
            "승인된 조항을 키워드와 의미 검색으로 찾는 중",
        )
        scored, retrieval = _score_records_for_queries(
            query_execution["search_queries"],
            visible_records,
            settings=settings,
            auth=auth,
            all_records=records,
            semantic_query_enabled=not fast_retrieval,
        )
    timing_ms["scoring_elapsed_ms"] = _perf_elapsed_ms(step_started_at)
    reranker_status = "not_requested"
    reranker_reason = ""
    if multi_model_retrieval and scored and not exact_locator_fast_path:
        step_started_at = time.perf_counter()
        try:
            local_reranker_path = Path("data/semantic_runtime_models/qwen3-reranker-0.6b")
            reranker_candidates = scored[
                : min(len(scored), max(10, min(request.top_k * 2, 20)))
            ]
            _emit_rag_chat_progress(
                "rerank",
                36,
                f"관련도가 높은 후보 {len(reranker_candidates)}개를 재정렬하는 중",
            )
            with _RAG_RERANKER_LOCK:
                scored = _cached_qwen3_reranker(
                    str(local_reranker_path.resolve()) if local_reranker_path.exists() else ""
                ).rerank(
                    request.query,
                    reranker_candidates,
                    top_k=min(max(request.top_k, 10), len(reranker_candidates)),
                )
            reranker_status = "completed"
        except Exception as exc:
            reranker_status = "degraded"
            reranker_reason = f"reranker_{type(exc).__name__}"[:120]
        timing_ms["reranker_elapsed_ms"] = _perf_elapsed_ms(step_started_at)
    retrieval.update(
        {
            **{key: value for key, value in query_execution.items() if key != "search_queries"},
            "reranker_model": (
                "deterministic_exact_locator"
                if exact_locator_fast_path
                else QWEN3_RERANKER_MODEL
                if multi_model_retrieval
                else "deterministic_structured_boosts"
            ),
            "reranker_status": reranker_status,
            "reranker_reason": reranker_reason,
        }
    )
    step_started_at = time.perf_counter()
    results = [
        _public_search_result(record, score, related_records=visible_records)
        for score, record in scored[: request.top_k]
    ]
    context_summary: dict[str, Any] = {"context_status": "not_requested"}
    if multi_model:
        context = ContextBuilder().build(results) if results else ContextBuilder().build([])
        context_summary = {
            "context_status": "completed",
            "context_item_count": len(context.items),
            "context_character_count": context.character_count,
            "context_estimated_tokens": context.estimated_tokens,
            "context_review_flag_count": len(context.review_flags),
        }
        retrieval.update(context_summary)
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
            "pipeline_trace": _qa_search_pipeline_trace(
                retrieval=retrieval,
                candidate_count=len(records),
                visible_count=len(visible_records),
                result_count=len(results),
                tenant_id=auth.tenant_id,
            ),
            "multi_model_orchestration": multi_model,
            **context_summary,
            **retrieval,
        },
    )
    step_started_at = time.perf_counter()
    if settings.rag_trace_enabled:
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
        _emit_rag_chat_progress(
            "query_analysis",
            8,
            "질문과 기관·문서 검색 범위를 확인하는 중",
        )
        metadata_profile = _validate_response_metadata_profile(request.metadata_profile)
        backend = _chat_backend(request, request_settings)
        execution_settings = replace(request_settings, rag_llm_backend=backend)
        chat_history = _chat_history_payload(request.history)
        _validate_chat_history_policy(chat_history)
        search_query = _chat_search_query(
            request.query,
            chat_history,
            document_id=request.document_id,
        )
        search_request = request.model_copy(update={"query": search_query})
        results, search_trace = search_rag_records(search_request, auth, execution_settings)
        _emit_rag_chat_progress(
            "context_build",
            42,
            "검색 근거를 중복 제거하고 안전한 문맥으로 구성하는 중",
        )
        multi_model = _use_multi_model_orchestration(request, execution_settings)
        qa_tracker = _qa_tracker_after_search(search_trace)
        qa_tracker.start(
            "local_llm_answer",
            detail={"backend": backend, "evidence_count": len(results), "multi_model": multi_model},
        )
        qa_tracker.set_agent_role_status(
            "local_llm_answer",
            "grounded_answerer",
            status="running",
        )
        orchestrated: dict[str, Any] | None = None
        if multi_model:
            orchestrated = _orchestrated_chat_answer(
                execution_settings,
                request.query,
                results,
                history=chat_history,
                use_model_claim_audit=request.claim_audit_mode == "model",
            )
            answer = str(orchestrated["answer"])
            citations = list(orchestrated["citations"])
        else:
            _emit_rag_chat_progress(
                "answer_generation",
                55,
                "로컬 답변 엔진이 승인된 근거만 읽어 답변을 작성하는 중",
            )
            answer, citation_evidence_ids = _chat_answer(
                backend,
                execution_settings,
                request.query,
                results,
                history=chat_history,
            )
            citation_results = _results_for_evidence_ids(
                results,
                citation_evidence_ids,
            )
            citations = [
                _rag_chat_citation_for_metadata_profile(result, metadata_profile)
                for result in citation_results
            ]
            _emit_rag_chat_progress(
                "citation_verify",
                88,
                "답변에 붙일 승인 근거와 인용 정보를 최종 확인하는 중",
            )
        qa_tracker.set_agent_role_status(
            "local_llm_answer",
            "grounded_answerer",
            status="completed"
            if orchestrated
            else "skipped"
            if backend == "extractive"
            else "degraded",
            reason_code=(
                None
                if orchestrated
                else "legacy_extractive_answer"
                if backend == "extractive"
                else "legacy_local_backend"
            ),
            detail={"answer_present": bool(answer)},
        )
        qa_tracker.complete(
            "local_llm_answer",
            detail={
                "answer_generated": bool(answer),
                "answer_model": (orchestrated or {}).get("answer_model"),
                "answer_mode": (orchestrated or {}).get("answer_mode"),
                "claim_count": (orchestrated or {}).get("claim_count", 0),
            },
        )
        qa_tracker.start("citation_verify", detail={"evidence_count": len(results)})
        qa_tracker.set_agent_role_status(
            "citation_verify",
            "claim_auditor",
            status=(
                "completed"
                if orchestrated and orchestrated.get("claim_audit_status") == "verified"
                else "skipped"
                if orchestrated
                and str(orchestrated.get("claim_audit_status") or "").startswith("deterministic_")
                else "skipped"
                if not orchestrated
                else "degraded"
            ),
            reason_code=(
                None
                if orchestrated and orchestrated.get("claim_audit_status") == "verified"
                else "deterministic_fast_chat"
                if orchestrated
                and str(orchestrated.get("claim_audit_status") or "").startswith("deterministic_")
                else "legacy_answer_path"
                if not orchestrated
                else str(orchestrated.get("claim_audit_status") or "claim_audit_not_verified")
            ),
        )
        qa_tracker.set_agent_role_status(
            "citation_verify",
            "citation_verifier",
            status="completed" if citations else "degraded" if orchestrated else "completed",
            reason_code=None if citations or not orchestrated else "no_verified_citations",
        )
        qa_tracker.complete(
            "citation_verify",
            detail={
                "citation_count": len(citations),
                "claim_audit_model": (orchestrated or {}).get("claim_audit_model"),
                "claim_audit_status": (orchestrated or {}).get("claim_audit_status"),
            },
        )
        trace = _rag_trace(
            action="chat",
            request=request,
            auth=auth,
            results=results,
            extra={
                "search_trace_id": search_trace["trace_id"],
                "llm_backend": backend,
                "answer_mode": (
                    (orchestrated or {}).get("answer_mode")
                    or ("grounded_local" if backend != "extractive" else "grounded_extractive")
                ),
                "multi_model_orchestration": multi_model,
                "claim_audit_status": (orchestrated or {}).get("claim_audit_status"),
                "history_message_count": len(chat_history),
                "contextualized_search": search_query != request.query,
                "pipeline_trace": qa_tracker.snapshot(),
            },
        )
        if request_settings.rag_trace_enabled:
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
        _emit_rag_chat_progress(
            "completed",
            100,
            "답변 생성과 근거 인용 검증 완료",
        )
        return {
            "trace_id": trace["trace_id"],
            "answer": answer,
            "citations": citations,
            "conversation": {
                "history_message_count": len(chat_history),
                "contextualized_search": search_query != request.query,
            },
            "orchestration": (
                {
                    "mode": "multi_model",
                    "answer_model": orchestrated.get("answer_model"),
                    "attempted_answer_model": orchestrated.get("attempted_answer_model"),
                    "answer_mode": orchestrated.get("answer_mode"),
                    "claim_audit_model": orchestrated.get("claim_audit_model"),
                    "claim_audit_status": orchestrated.get("claim_audit_status"),
                    "claim_audit_reason": orchestrated.get("claim_audit_reason"),
                    "roles": _qa_orchestration_role_trace(
                        search_trace=search_trace,
                        orchestration=orchestrated,
                    ),
                }
                if orchestrated
                else {"mode": "legacy"}
            ),
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
            "available_embedding_models": [LOCAL_HASH_EMBEDDING_MODEL, QWEN3_EMBEDDING_MODEL],
            "retrieval_model": BM25_RETRIEVAL_MODEL,
            "vector_store_configured": vector_path.is_file(),
            "multi_model_orchestration": {
                "enabled_when": "ollama+qwen3:8b",
                "model_profiles": model_profile_manifest(),
                "semantic_runtime_available": semantic_runtime_available(),
                "reranker_weights_available": Path(
                    "data/semantic_runtime_models/qwen3-reranker-0.6b"
                ).is_dir(),
                "paddle_ocr_runtime_available": paddle_ocr_available(),
            },
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
    return settings.data_dir / "vector_db" / tenant_directory_key(auth.tenant_id) / "approved_vectors.jsonl"


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
    sidecar_snapshot = _load_cached_runtime_approval_snapshot(
        repository,
        document_ids,
        auth,
    )
    if sidecar_snapshot is not None:
        return sidecar_snapshot

    cache_key = (repository.root, auth.tenant_id, tuple(document_ids))
    signature = _approval_snapshot_signature(repository, document_ids)
    with _RAG_VECTOR_CACHE_LOCK:
        cached = _RAG_APPROVAL_SNAPSHOT_CACHE.get(cache_key)
        if cached and cached[0] == signature:
            return cached[1]
    snapshot = _build_approval_snapshot(repository, document_ids, auth)
    with _RAG_VECTOR_CACHE_LOCK:
        _RAG_APPROVAL_SNAPSHOT_CACHE[cache_key] = (signature, snapshot)
    return snapshot


def _load_cached_runtime_approval_snapshot(
    repository: JsonRepository,
    document_ids: list[str],
    auth: AuthContext,
) -> dict[tuple[str, str], dict[str, Any]] | None:
    """Load a verified runtime sidecar without requiring vector records.

    Hierarchical runtime indexes already carry the document identities needed
    to scope this sidecar. Keeping this path independent from the large vector
    JSONL lets callers authorize indexed chunk identities before fetching a
    small number of offset records. A missing, stale, or concurrently changing
    sidecar never falls back to live repository state here.
    """

    normalized_document_ids = sorted(
        {
            str(document_id or "").strip()
            for document_id in document_ids
            if str(document_id or "").strip()
        }
    )
    cache_key = (
        repository.root,
        auth.tenant_id,
        tuple(normalized_document_ids),
    )
    source_identity = _runtime_approval_snapshot_identity(
        repository,
        normalized_document_ids,
    )
    if source_identity is None:
        return None
    with _RAG_VECTOR_CACHE_LOCK:
        identity_cached = _RAG_RUNTIME_APPROVAL_IDENTITY_CACHE.get(cache_key)
        if identity_cached and identity_cached[0] == source_identity:
            _RAG_RUNTIME_APPROVAL_IDENTITY_CACHE.move_to_end(cache_key)
            cached_snapshot = identity_cached[1]
        else:
            cached_snapshot = None
    if cached_snapshot is not None:
        if (
            _runtime_approval_snapshot_identity(
                repository,
                normalized_document_ids,
            )
            != source_identity
        ):
            return None
        return cached_snapshot

    requested_document_ids = frozenset(normalized_document_ids)
    derived_snapshot: dict[tuple[str, str], dict[str, Any]] | None = None
    with _RAG_VECTOR_CACHE_LOCK:
        for superset_key, (
            superset_identity,
            superset_snapshot,
        ) in reversed(list(_RAG_RUNTIME_APPROVAL_IDENTITY_CACHE.items())):
            if (
                superset_key[0] != repository.root
                or superset_key[1] != auth.tenant_id
                or not requested_document_ids.issubset(superset_key[2])
                or not _runtime_approval_identity_covers_scope(
                    superset_identity,
                    source_identity,
                )
            ):
                continue
            derived_snapshot = {
                key: value
                for key, value in superset_snapshot.items()
                if key[0] in requested_document_ids
            }
            _store_runtime_approval_identity_cache(
                cache_key,
                source_identity,
                derived_snapshot,
            )
            break
    if derived_snapshot is not None:
        if (
            _runtime_approval_snapshot_identity(
                repository,
                normalized_document_ids,
            )
            != source_identity
        ):
            return None
        return derived_snapshot

    signature = _runtime_approval_snapshot_signature(
        repository,
        normalized_document_ids,
    )
    if signature is None:
        return None
    with _RAG_VECTOR_CACHE_LOCK:
        cached = _RAG_APPROVAL_SNAPSHOT_CACHE.get(cache_key)
        if cached and cached[0] == signature:
            _store_runtime_approval_identity_cache(
                cache_key,
                source_identity,
                cached[1],
            )
            return cached[1]

    snapshot = _load_runtime_approval_snapshot_sidecar(
        repository,
        normalized_document_ids,
        auth,
    )
    if snapshot is None:
        return None
    final_identity = _runtime_approval_snapshot_identity(
        repository,
        normalized_document_ids,
    )
    if final_identity != source_identity:
        return None
    final_signature = _runtime_approval_snapshot_signature(
        repository,
        normalized_document_ids,
    )
    if final_signature != signature:
        return None
    with _RAG_VECTOR_CACHE_LOCK:
        _RAG_APPROVAL_SNAPSHOT_CACHE[cache_key] = (signature, snapshot)
        _store_runtime_approval_identity_cache(
            cache_key,
            source_identity,
            snapshot,
        )
    return snapshot


def _runtime_approval_snapshot_identity(
    repository: JsonRepository,
    document_ids: Iterable[str] | None = None,
) -> tuple[Any, ...] | None:
    """Return a cheap identity for files covered by the verified sidecar.

    A document-scoped caller only needs to invalidate authorization derived
    from those documents. Avoiding a scan of every unrelated chunk file keeps
    targeted fetches inexpensive while still detecting approval or ACL changes
    to every chunk file that can influence the requested snapshot. Callers
    that omit ``document_ids`` retain the repository-wide search semantics.
    """

    runtime_manifest_path = repository.root.parent / "mcp_runtime_manifest.json"
    sidecar_path = _runtime_approval_snapshot_path(repository)
    runtime_manifest_signature = _path_signature(runtime_manifest_path)
    sidecar_signature = _path_signature(sidecar_path)
    if runtime_manifest_signature is None or sidecar_signature is None:
        return None
    chunk_paths = _runtime_approval_identity_chunk_paths(
        repository,
        document_ids=document_ids,
    )
    if chunk_paths is None:
        return None
    chunk_signatures: list[tuple[str, Any]] = []
    for path in chunk_paths:
        signature = _path_signature(path)
        if signature is None:
            if document_ids is None:
                return None
            # Retain the expected filename in scoped identities. Omitting a
            # deleted file would produce an empty subset and could let an
            # older verified superset authorize the document vacuously.
            chunk_signatures.append((path.name, ("missing",)))
            continue
        chunk_signatures.append((path.name, signature))
    return (
        runtime_manifest_signature,
        sidecar_signature,
        _path_signature(repository.manifest_path),
        _path_signature(repository.legacy_path),
        _path_signature(repository.root / "journals" / "approvals.jsonl"),
        tuple(chunk_signatures),
    )


def _runtime_approval_identity_chunk_paths(
    repository: JsonRepository,
    *,
    document_ids: Iterable[str] | None,
) -> list[Path] | None:
    if document_ids is None:
        try:
            return sorted(
                repository.root.glob("*_chunks.json"),
                key=lambda candidate: candidate.name,
            )
        except OSError:
            return None

    try:
        repository_root = repository.root.resolve()
    except OSError:
        return None
    chunk_paths: list[Path] = []
    for document_id in sorted(
        {
            str(value or "").strip()
            for value in document_ids
            if str(value or "").strip()
        }
    ):
        path = repository.root / f"{document_id}_chunks.json"
        try:
            resolved_path = path.resolve()
        except OSError:
            return None
        if resolved_path.parent != repository_root:
            return None
        chunk_paths.append(path)
    return chunk_paths


def _runtime_approval_identity_covers_scope(
    cached_identity: tuple[Any, ...],
    scoped_identity: tuple[Any, ...],
) -> bool:
    """Return whether a verified superset identity still covers one scope."""

    if len(cached_identity) != 6 or len(scoped_identity) != 6:
        return cached_identity == scoped_identity
    if cached_identity[:5] != scoped_identity[:5]:
        return False
    try:
        cached_chunk_signatures = dict(cached_identity[5])
        scoped_chunk_signatures = dict(scoped_identity[5])
    except (TypeError, ValueError):
        return False
    return all(
        cached_chunk_signatures.get(name) == signature
        for name, signature in scoped_chunk_signatures.items()
    )


def _store_runtime_approval_identity_cache(
    cache_key: tuple[Path, str, tuple[str, ...]],
    source_identity: tuple[Any, ...],
    snapshot: dict[tuple[str, str], dict[str, Any]],
) -> None:
    """Store one verified runtime snapshot while holding the vector cache lock."""

    _RAG_RUNTIME_APPROVAL_IDENTITY_CACHE[cache_key] = (
        source_identity,
        snapshot,
    )
    _RAG_RUNTIME_APPROVAL_IDENTITY_CACHE.move_to_end(cache_key)
    while (
        len(_RAG_RUNTIME_APPROVAL_IDENTITY_CACHE)
        > _RAG_RUNTIME_APPROVAL_IDENTITY_CACHE_MAX_ENTRIES
    ):
        _RAG_RUNTIME_APPROVAL_IDENTITY_CACHE.popitem(last=False)


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
) -> dict[str, tuple[Any, ...] | None]:
    return {
        # Runtime bundles are routinely copied or extracted from a ZIP.  File
        # identity fields such as inode and mtime are therefore not portable
        # across handoff environments.  Content signatures keep the approval
        # sidecar valid after extraction while still invalidating it on edits.
        "repository_manifest": _portable_file_signature(repository.manifest_path),
        # Legacy repositories are still readable for backward compatibility;
        # a mutation there must invalidate the runtime approval sidecar too.
        "legacy_repository": _portable_file_signature(repository.legacy_path),
        "approval_journal": _portable_file_signature(repository.root / "journals" / "approvals.jsonl"),
        # Approval/rejection and ACL changes are persisted to per-document chunk
        # files before their audit record is appended.  Include those files in
        # the sidecar contract so a failure between the two writes cannot leave
        # a stale approval snapshot authorizing an old vector record.
        "repository_chunk_files": _repository_chunk_files_signature(repository),
    }


def _portable_file_signature(path: Path) -> tuple[int, str] | None:
    """Return a ZIP-portable content signature for a runtime file.

    The stat signature is used only as a cheap cache key.  The value written
    to the runtime approval sidecar is the byte length plus SHA-256 digest, so
    extraction to a different filesystem does not force a live snapshot
    rebuild merely because inode/timestamp values changed.
    """

    stat_signature = _path_signature(path)
    if stat_signature is None:
        return None
    with _RUNTIME_CONTENT_SIGNATURE_LOCK:
        cached = _RUNTIME_CONTENT_SIGNATURE_CACHE.get(path)
        if cached and cached[0] == stat_signature:
            return cached[1]
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                while block := handle.read(1024 * 1024):
                    digest.update(block)
        except OSError:
            return None
        signature = (int(stat_signature[1]), digest.hexdigest())
        _RUNTIME_CONTENT_SIGNATURE_CACHE[path] = (stat_signature, signature)
        return signature


def _repository_chunk_files_signature(repository: JsonRepository) -> tuple[int, str]:
    file_signatures = [
        (path.name, _portable_file_signature(path))
        for path in sorted(repository.root.glob("*_chunks.json"), key=lambda candidate: candidate.name)
    ]
    digest = hashlib.sha256(
        json.dumps(file_signatures, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    total_bytes = sum(
        int(signature[1][0])
        for signature in file_signatures
        if signature[1] is not None
    )
    return (total_bytes, digest)


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
    if payload.get("schema_version") != "mcp-runtime-approval-snapshot-v1":
        return None
    runtime_reuse = runtime_manifest.get("runtime_data_reuse")
    runtime_file_hashes = (
        runtime_reuse.get("file_sha256")
        if isinstance(runtime_reuse, dict)
        else None
    )
    if not isinstance(runtime_file_hashes, dict):
        return None
    try:
        sidecar_relative_path = sidecar_path.relative_to(
            repository.root.parent
        ).as_posix()
    except ValueError:
        return None
    expected_sidecar_hash = str(
        runtime_file_hashes.get(sidecar_relative_path) or ""
    ).strip().lower()
    sidecar_content_signature = _portable_file_signature(sidecar_path)
    if (
        not re.fullmatch(r"[a-f0-9]{64}", expected_sidecar_hash)
        or sidecar_content_signature is None
        or sidecar_content_signature[1] != expected_sidecar_hash
    ):
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
    if (
        payload.get("record_count") != len(entries)
        or payload.get("snapshot_count") != len(entries)
    ):
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
        records_by_document = _approval_journal_records_by_document(
            repository,
            document_ids,
        )
        records = [
            record
            for document_id in document_ids
            for record in records_by_document.get(document_id, ())
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
    approval_records_by_document = _approval_journal_records_by_document(
        repository,
        document_ids,
    )
    approval_match_index = _approval_journal_match_index(
        record
        for document_id in document_ids
        for record in approval_records_by_document.get(document_id, ())
    )
    for document_id in document_ids:
        document = repository.get_document(document_id)
        if document is None or not resource_visible_to_tenant(document, auth.tenant_id):
            continue
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
            if _approval_journal_match_key(
                chunk_id=chunk_id,
                document_id=document_id,
                tenant_id=auth.tenant_id,
                approval_id=str(chunk.approval_id or ""),
                approved_content_hash=str(chunk.approved_content_hash or ""),
                expected_metadata=expected_metadata,
            ) not in approval_match_index:
                continue
            snapshot[(document_id, chunk_id)] = {
                "approval_id": expected_metadata.get("approval_id"),
                "approved_content_hash": expected_metadata.get("approved_content_hash"),
                "security_level": security_level,
                "department_acl": _department_acl_set(expected_metadata.get("department_acl")),
                "content_hash": str(expected_record.get("content_hash") or ""),
            }
    return snapshot


def _approval_journal_records_by_document(
    repository: JsonRepository,
    document_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    selected_document_ids = set(document_ids)
    journal_path = _approval_journal_cache_path(repository)
    journal_signature = _path_signature(journal_path) if journal_path is not None else None
    if journal_path is not None:
        with _RAG_VECTOR_CACHE_LOCK:
            cached = _RAG_APPROVAL_JOURNAL_CACHE.get(journal_path)
            if cached and cached[0] == journal_signature:
                _RAG_APPROVAL_JOURNAL_CACHE.move_to_end(journal_path)
                return {
                    document_id: list(cached[1].get(document_id, ()))
                    for document_id in document_ids
                }

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in repository.list_approval_journal_records():
        if not isinstance(record, dict):
            continue
        document_id = str(record.get("document_id") or "")
        if document_id:
            grouped[document_id].append(record)

    if journal_path is not None and _path_signature(journal_path) == journal_signature:
        immutable_grouped = {
            document_id: tuple(records)
            for document_id, records in grouped.items()
        }
        with _RAG_VECTOR_CACHE_LOCK:
            _RAG_APPROVAL_JOURNAL_CACHE[journal_path] = (
                journal_signature,
                immutable_grouped,
            )
            _RAG_APPROVAL_JOURNAL_CACHE.move_to_end(journal_path)
            while len(_RAG_APPROVAL_JOURNAL_CACHE) > _RAG_APPROVAL_JOURNAL_CACHE_MAX_ENTRIES:
                _RAG_APPROVAL_JOURNAL_CACHE.popitem(last=False)

    return {
        document_id: list(grouped.get(document_id, ()))
        for document_id in selected_document_ids
    }


def _approval_journal_cache_path(repository: Any) -> Path | None:
    root = getattr(repository, "root", None)
    if root is None:
        return None
    return Path(root) / "journals" / "approvals.jsonl"


def _approval_journal_match_index(
    records: Iterable[dict[str, Any]],
) -> set[tuple[Any, ...]]:
    index: set[tuple[Any, ...]] = set()
    expected_worklist_keys = set(APPROVAL_WORKLIST_METADATA_KEYS)
    for record in records:
        if not isinstance(record, dict):
            continue
        document_id = str(record.get("document_id") or "")
        tenant_id = str(record.get("tenant_id") or "")
        approval_id = str(record.get("approval_id") or "")
        if not document_id or not tenant_id or not approval_id:
            continue
        worklist_evidence = record.get("worklist_evidence") or {}
        if not isinstance(worklist_evidence, dict):
            continue
        worklist_metadata = approval_worklist_metadata(worklist_evidence)
        if set(worklist_metadata) != expected_worklist_keys:
            continue
        chunk_ids = {
            str(value)
            for value in (record.get("chunk_ids") or [])
            if str(value or "")
        }
        approved_hashes = {
            str(chunk_id): str(value)
            for chunk_id, value in (record.get("approved_content_hashes") or {}).items()
            if str(chunk_id or "") and str(value or "")
        } if isinstance(record.get("approved_content_hashes"), dict) else {}
        for item in record.get("approved_chunks") or []:
            if not isinstance(item, dict):
                continue
            chunk_id = str(item.get("chunk_id") or "")
            approved_hash = str(item.get("approved_content_hash") or "")
            if chunk_id and approved_hash and chunk_id not in approved_hashes:
                approved_hashes[chunk_id] = approved_hash
        for chunk_id in chunk_ids:
            approved_content_hash = approved_hashes.get(chunk_id, "")
            if not approved_content_hash:
                continue
            index.add(
                _approval_journal_match_key(
                    chunk_id=chunk_id,
                    document_id=document_id,
                    tenant_id=tenant_id,
                    approval_id=approval_id,
                    approved_content_hash=approved_content_hash,
                    expected_metadata=worklist_metadata,
                )
            )
    return index


def _approval_journal_match_key(
    *,
    chunk_id: str,
    document_id: str,
    tenant_id: str,
    approval_id: str,
    approved_content_hash: str,
    expected_metadata: dict[str, Any],
) -> tuple[Any, ...]:
    return (
        str(document_id),
        str(tenant_id),
        str(approval_id),
        str(chunk_id),
        str(approved_content_hash),
        tuple(
            (key, str(expected_metadata.get(key) or ""))
            for key in sorted(APPROVAL_WORKLIST_METADATA_KEYS)
        ),
    )


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


def _exact_article_locator_matches(
    analysis: QueryAnalysis,
    records: list[dict[str, Any]],
) -> list[tuple[float, dict[str, Any]]]:
    """Return already-visible exact article matches without loading search models.

    This intentionally handles only one unambiguous article locator in the
    currently selected document. Comparisons, paragraph-only questions and
    temporal questions continue through the normal hybrid retrieval path.
    """

    if not _is_exact_article_locator_query(analysis):
        return []
    locator = analysis.locators[0]
    expected = _normalize_reference_label(locator.canonical)
    if not expected:
        return []

    matches: list[tuple[float, dict[str, Any]]] = []
    for position, record in enumerate(records):
        metadata = record.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        article_no = _normalize_reference_label(str(metadata.get("article_no") or ""))
        if article_no != expected:
            continue
        matches.append((round(max(0.5, 1.0 - position * 0.000001), 8), record))
    return matches


def _is_exact_article_locator_query(analysis: QueryAnalysis) -> bool:
    if (
        analysis.requires_temporal_filter
        or len(analysis.locators) != 1
        or analysis.locators[0].kind != "article"
    ):
        return False

    # Only plain requests for the named article may skip semantic retrieval.
    # Relationship, sanction, attachment and cross-article questions must keep
    # the normal analysis/search path even when they mention one article.
    remainder = analysis.normalized_query.replace(analysis.locators[0].raw, "", 1)
    compact = re.sub(r"[\s?？!.。]+", "", remainder)
    return bool(
        re.fullmatch(
            r"(?:(?:은|는|이|가|을|를|의)?"
            r"(?:내용|원문|전문|조문|목적|취지)?"
            r"(?:은|는|이|가|을|를)?"
            r"(?:에대해(?:서)?|에관해(?:서)?)?"
            r"(?:알려줘|알려주세요|설명해줘|설명해주세요|보여줘|보여주세요|"
            r"뭐야|무엇이야|무엇인가요|인가요)?)?",
            compact,
        )
    )


@lru_cache(maxsize=2)
def _cached_qwen3_reranker(local_model_path: str) -> Qwen3RerankerAdapter:
    """Reuse the CPU reranker instead of loading its weights for every question."""

    return Qwen3RerankerAdapter(
        device="cpu",
        local_files_only=True,
        model_path=Path(local_model_path) if local_model_path else None,
    )


def _use_multi_model_orchestration(request: RagSearchRequest, settings: Settings) -> bool:
    mode = str(request.orchestration_mode or "auto").strip().lower()
    if mode == "legacy":
        return False
    if mode == "multi_model":
        return True
    backend = str(settings.rag_llm_backend or "extractive").strip().lower()
    model = str(settings.rag_llm_model or "").strip().lower()
    return backend == "ollama" and model == QWEN3_ANSWER_MODEL


def _score_records_for_queries(
    queries: list[str] | tuple[str, ...],
    records: list[dict[str, Any]],
    *,
    settings: Settings,
    auth: AuthContext,
    all_records: list[dict[str, Any]] | None = None,
    semantic_query_enabled: bool = True,
) -> tuple[list[tuple[float, dict[str, Any]]], dict[str, Any]]:
    normalized = list(dict.fromkeys(" ".join(str(query or "").split()) for query in queries))
    normalized = [query for query in normalized if query][:8]
    if not normalized:
        raise ValueError("at least one search query is required")
    if len(normalized) == 1:
        return _score_records(
            normalized[0],
            records,
            settings=settings,
            auth=auth,
            all_records=all_records,
            semantic_query_enabled=semantic_query_enabled,
        )
    by_id = {
        str(record.get("id") or record.get("chunk_id") or f"record-{index}"): record
        for index, record in enumerate(records)
    }
    fused: dict[str, float] = {}
    per_query_models: list[str] = []
    primary_metadata: dict[str, Any] = {}
    for query_index, query in enumerate(normalized):
        scored, metadata = _score_records(
            query,
            records,
            settings=settings,
            auth=auth,
            all_records=all_records,
            semantic_query_enabled=semantic_query_enabled,
        )
        if query_index == 0:
            primary_metadata = dict(metadata)
        per_query_models.append(str(metadata.get("retrieval_model") or ""))
        query_weight = 1.0 if query_index == 0 else 0.85
        for rank, (_score, record) in enumerate(scored, start=1):
            record_id = str(record.get("id") or record.get("chunk_id") or "")
            if record_id in by_id:
                fused[record_id] = fused.get(record_id, 0.0) + query_weight / (60.0 + rank)
    ranked = sorted(
        [(round(score, 8), by_id[record_id]) for record_id, score in fused.items()],
        key=lambda item: item[0],
        reverse=True,
    )
    primary_metadata.update(
        {
            "multi_query_fusion": "weighted_rrf-v1",
            "search_query_count": len(normalized),
            "per_query_retrieval_models": per_query_models,
            "query_expanded": True,
        }
    )
    return ranked, primary_metadata


def _score_records(
    query: str,
    records: list[dict[str, Any]],
    *,
    settings: Settings,
    auth: AuthContext,
    all_records: list[dict[str, Any]] | None = None,
    semantic_query_enabled: bool = True,
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
        prefer_semantic=semantic_query_enabled,
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
        "source_xml_files": metadata.get("source_xml_files") or [],
        "source_xml_roles": metadata.get("source_xml_roles") or [],
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
        "pdf_embedded_image_pages": metadata.get("pdf_embedded_image_pages") or [],
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


def _qa_search_pipeline_trace(
    *,
    retrieval: dict[str, Any],
    candidate_count: int,
    visible_count: int,
    result_count: int,
    tenant_id: str | None,
) -> dict[str, Any]:
    """Publish the search half of the image's local-QA pipeline.

    Details contain only bounded counters and model/status codes, never the raw
    query, generated prompt, regulation text, tenant id, or local path.
    """

    tracker = PipelineStageTracker(LOCAL_QA_PIPELINE_ID, tenant_id=tenant_id)
    stages = (
        (
            "query_analysis",
            {
                "query_policy": "passed",
                "analysis_mode": retrieval.get("analysis_mode"),
                "query_model": retrieval.get("query_model"),
                "query_fallback_reason": retrieval.get("query_fallback_reason"),
                "intent": retrieval.get("intent"),
                "locator_count": retrieval.get("locator_count", 0),
            },
        ),
        (
            "query_correction",
            {
                "query_expanded": bool(retrieval.get("query_expanded")),
                "rewrite_mode": retrieval.get("rewrite_mode"),
                "rewrite_fallback_reason": retrieval.get("rewrite_fallback_reason"),
                "search_query_count": retrieval.get("search_query_count", 1),
            },
        ),
        (
            "hybrid_retrieval",
            {
                "retrieval_model": retrieval.get("retrieval_model"),
                "retrieval_fallback": bool(retrieval.get("retrieval_fallback")),
                "candidate_count": candidate_count,
            },
        ),
        (
            "rerank_filter",
            {
                "bm25_index_status": retrieval.get("bm25_index_status"),
                "reranker_model": retrieval.get("reranker_model"),
                "reranker_status": retrieval.get("reranker_status"),
                "reranker_reason": retrieval.get("reranker_reason"),
                "visible_count": visible_count,
            },
        ),
        (
            "context_build",
            {
                "evidence_count": result_count,
                "context_status": retrieval.get("context_status"),
                "context_item_count": retrieval.get("context_item_count", result_count),
                "context_estimated_tokens": retrieval.get("context_estimated_tokens", 0),
            },
        ),
    )
    for stage_id, detail in stages:
        tracker.start(stage_id, detail=detail)
        _mark_qa_stage_roles(tracker, stage_id, detail)
        tracker.complete(stage_id, detail=detail)
    return tracker.snapshot()


def _qa_tracker_after_search(search_trace: dict[str, Any]) -> PipelineStageTracker:
    tracker = PipelineStageTracker(
        LOCAL_QA_PIPELINE_ID,
        tenant_id=str(search_trace.get("tenant_id") or "") or None,
    )
    trace = search_trace.get("pipeline_trace")
    events = trace.get("stages") if isinstance(trace, dict) else []
    details = {
        str(event.get("stage_id")): event.get("detail") or {}
        for event in events
        if isinstance(event, dict)
    }
    for stage_id in ("query_analysis", "query_correction", "hybrid_retrieval", "rerank_filter", "context_build"):
        detail = details.get(stage_id, {})
        tracker.start(stage_id, detail=detail)
        _mark_qa_stage_roles(tracker, stage_id, detail)
        tracker.complete(stage_id, detail=detail)
    return tracker


def _mark_qa_stage_roles(
    tracker: PipelineStageTracker,
    stage_id: str,
    detail: dict[str, Any],
) -> None:
    """Translate bounded QA stage details into real role statuses."""

    role_by_stage = {
        "query_analysis": ("query_analyst",),
        "query_correction": ("query_rewriter",),
        "hybrid_retrieval": ("retrieval_guard",),
        "rerank_filter": ("reranker",),
        "context_build": ("context_builder",),
    }
    roles = role_by_stage.get(stage_id, ())
    for role_id in roles:
        raw_status = "completed"
        if role_id == "reranker":
            reranker_status = str(detail.get("reranker_status") or "").strip().lower()
            raw_status = {
                "not_requested": "skipped",
                "degraded": "degraded",
                "failed": "failed",
                "completed": "completed",
            }.get(reranker_status, "completed")
        elif role_id in {"query_analyst", "query_rewriter"}:
            fallback_reason = str(
                detail.get("query_fallback_reason") or detail.get("rewrite_fallback_reason") or ""
            ).strip()
            raw_status = "degraded" if fallback_reason else "completed"
        tracker.set_agent_role_status(
            stage_id,
            role_id,
            status=raw_status,  # type: ignore[arg-type]
            reason_code=(
                str(detail.get("reranker_reason") or "")[:120]
                if role_id == "reranker" and raw_status == "degraded"
                else None
            ),
            detail={
                key: value
                for key, value in detail.items()
                if key in {"analysis_mode", "rewrite_mode", "retrieval_fallback", "reranker_status", "context_status"}
            },
        )


def _chat_backend(request: RagChatRequest, settings: Settings) -> str:
    configured_backend = str(settings.rag_llm_backend or "extractive").strip().lower()
    requested_backend = str(request.llm_backend or "").strip().lower()
    if requested_backend and requested_backend != configured_backend:
        raise HTTPException(
            status_code=403,
            detail="Request-level local RAG backend override is not permitted.",
        )
    backend = configured_backend
    if backend == "extractive":
        return backend
    if backend in {"ollama", "llama-cpp", "openai-compatible"}:
        if not local_llm_available(replace(settings, rag_llm_backend=backend)):
            raise HTTPException(status_code=503, detail="Configured local LLM backend is not available.")
        return backend
    raise HTTPException(status_code=400, detail="Unsupported local RAG chat backend.")


def _chat_history_payload(history: list[RagChatMessage]) -> list[dict[str, str]]:
    return [
        {
            "role": message.role,
            "content": " ".join(message.content.split())[:2000],
        }
        for message in history[-12:]
        if message.content.strip()
    ]


def _contextualized_chat_query(query: str, history: list[dict[str, str]]) -> str:
    current = " ".join(str(query or "").split())
    previous_questions = [
        str(message.get("content") or "").strip()
        for message in history
        if message.get("role") == "user" and str(message.get("content") or "").strip()
    ][-2:]
    if not previous_questions:
        return current
    suffix = f"\n현재 질문:\n{current}"
    header = "이전 사용자 질문:\n"
    history_budget = MAX_MCP_QUERY_CHARS - len(header) - len(suffix)
    if history_budget <= 0:
        return current[:MAX_MCP_QUERY_CHARS]
    previous_block = "\n".join(f"- {item}" for item in previous_questions)
    return f"{header}{previous_block[:history_budget]}{suffix}"


def _validate_chat_history_policy(history: list[dict[str, str]]) -> None:
    """Apply the input policy to every untrusted history item sent by the client."""

    for message in history:
        _validate_query_policy(str(message.get("content") or ""))


def _chat_search_query(
    query: str,
    history: list[dict[str, str]],
    *,
    document_id: str | None,
) -> str:
    """Keep self-contained exact article requests independent from old turns."""

    current = " ".join(str(query or "").split())
    if document_id and _is_exact_article_locator_query(deterministic_query_analysis(current)):
        return current
    return _contextualized_chat_query(current, history)


def _orchestrated_chat_answer(
    settings: Settings,
    query: str,
    results: list[dict[str, Any]],
    *,
    history: list[dict[str, str]] | None = None,
    use_model_claim_audit: bool = True,
) -> dict[str, Any]:
    try:
        _emit_rag_chat_progress(
            "context_build",
            44,
            "Qwen3 8B가 읽을 승인 근거 문맥을 구성하는 중",
        )
        context = ContextBuilder().build(results) if results else ContextBuilder().build([])
        runtime = OllamaRuntime(settings.rag_llm_endpoint)
        _emit_rag_chat_progress(
            "answer_generation",
            55,
            "Qwen3 8B가 승인된 근거만 읽어 답변을 작성하는 중",
        )
        answer_agent = GroundedQwenAnswerAgent(runtime)
        draft = (
            answer_agent.answer(
                query=query,
                context=context,
                history=history,
                strict_model=True,
            )
            if use_model_claim_audit
            else answer_agent.answer_fast(
                query=query,
                context=context,
                history=history,
                strict_model=False,
            )
        )
        if not use_model_claim_audit:
            _emit_rag_chat_progress(
                "deterministic_audit",
                76,
                "답변의 근거 ID와 승인 인용을 빠르게 검증하는 중",
            )
            return _deterministic_chat_draft_verification(draft, context, results)
        _emit_rag_chat_progress(
            "claim_audit",
            76,
            "Qwen3 4B가 답변의 핵심 주장을 근거와 대조하는 중",
        )
        audit = ClaimAuditAgent(runtime).audit(
            draft=draft,
            context=context,
            strict_model=True,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Multi-model local QA execution failed: {type(exc).__name__}",
        ) from exc
    if audit.status == "abstained":
        _emit_rag_chat_progress(
            "citation_verify",
            90,
            "근거 부족 응답과 공개 인용 범위를 최종 확인하는 중",
        )
        return {
            "answer": _sanitize_rag_answer(draft.answer),
            "citations": [],
            "answer_model": draft.model,
            "answer_mode": draft.answer_mode,
            "claim_count": 0,
            "claim_audit_model": audit.model,
            "claim_audit_status": audit.status,
            "citation_verification_status": "abstained",
        }
    if audit.status != "verified":
        _emit_rag_chat_progress(
            "fallback_answer",
            84,
            "감사 결과에 따라 승인 근거 발췌 답변으로 안전하게 전환하는 중",
        )
        fallback = GroundedQwenAnswerAgent(runtime).answer(
            query=query,
            context=context,
            history=history,
            prefer_model=False,
        )
        supporting_context_ids = {
            context_id
            for claim in fallback.claims
            for context_id in claim.evidence_context_ids
        }
        supporting_evidence_ids = list(
            dict.fromkeys(
                evidence_id
                for item in context.items
                if item.context_id in supporting_context_ids
                for evidence_id in item.evidence_ids
            )
        )
        supporting_results = [
            result
            for result in results
            if str(result.get("chunk_id") or result.get("document_id") or "")
            in supporting_evidence_ids
        ]
        _emit_rag_chat_progress(
            "citation_verify",
            90,
            "발췌 답변의 근거 ID와 승인 기록을 최종 검증하는 중",
        )
        fallback_verification = CitationVerifierAgent().run(
            {
                "answer": fallback.answer,
                "evidence": supporting_results,
                "evidence_ids": supporting_evidence_ids,
            }
        )
        if fallback_verification.get("status") != "verified":
            raise HTTPException(
                status_code=503,
                detail=f"Multi-model answer claim audit did not verify: {audit.status}",
            )
        return {
            "answer": _sanitize_rag_answer(
                str(fallback_verification.get("verified_answer") or fallback.answer)
            ),
            "citations": list(fallback_verification.get("citations") or []),
            "answer_model": fallback.model,
            "attempted_answer_model": draft.model,
            "answer_mode": fallback.answer_mode,
            "claim_count": len(fallback.claims),
            "claim_audit_model": audit.model,
            "claim_audit_status": f"fallback_from_{audit.status}",
            "claim_audit_reason": audit.reason_code,
            "citation_verification_status": "verified",
        }
    requested_evidence_ids = list(
        dict.fromkeys(
            evidence_id
            for citation in audit.citations
            for evidence_id in citation.evidence_ids
        )
    )
    _emit_rag_chat_progress(
        "citation_verify",
        90,
        "답변의 근거 ID·승인 기록·인용 조문을 최종 검증하는 중",
    )
    identity_verification = CitationVerifierAgent().run(
        {
            "answer": draft.answer,
            "evidence": results,
            "evidence_ids": requested_evidence_ids,
        }
    )
    if identity_verification.get("status") != "verified":
        raise HTTPException(status_code=503, detail="Final evidence identity verification failed")
    citations = []
    for citation in audit.citations:
        payload = citation.model_dump(mode="json")
        payload["support_quote"] = _sanitize_rag_answer(str(payload.get("support_quote") or ""))
        citations.append(payload)
    return {
        "answer": _sanitize_rag_answer(str(identity_verification.get("verified_answer") or "")),
        "citations": citations,
        "answer_model": draft.model,
        "answer_mode": draft.answer_mode,
        "claim_count": len(draft.claims),
        "claim_audit_model": audit.model,
        "claim_audit_status": audit.status,
        "citation_verification_status": "verified",
    }


def _deterministic_chat_draft_verification(
    draft: GroundedAnswerDraft,
    context: GroundingContext,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Verify fast-chat evidence identity without loading a second Ollama model."""

    _emit_rag_chat_progress(
        "citation_verify",
        90,
        "답변에 연결된 승인 조항과 공개 인용을 최종 확인하는 중",
    )
    if draft.abstained:
        return {
            "answer": _sanitize_rag_answer(draft.answer),
            "citations": [],
            "answer_model": draft.model,
            "answer_mode": draft.answer_mode,
            "claim_count": 0,
            "claim_audit_model": None,
            "claim_audit_status": "deterministic_abstained",
            "citation_verification_status": "abstained",
        }

    context_by_id = {item.context_id: item for item in context.items}
    supporting_context_ids = {
        context_id
        for claim in draft.claims
        for context_id in claim.evidence_context_ids
    }
    requested_evidence_ids = list(
        dict.fromkeys(
            evidence_id
            for context_id in supporting_context_ids
            for evidence_id in context_by_id[context_id].evidence_ids
        )
    )
    requested_set = set(requested_evidence_ids)
    supporting_results = [
        result
        for result in results
        if str(result.get("chunk_id") or result.get("document_id") or "") in requested_set
    ]
    verification = CitationVerifierAgent().run(
        {
            "answer": draft.answer,
            "evidence": supporting_results,
            "evidence_ids": requested_evidence_ids,
        }
    )
    if verification.get("status") != "verified":
        raise HTTPException(status_code=503, detail="Fast chat evidence identity verification failed")
    return {
        "answer": _sanitize_rag_answer(
            str(verification.get("verified_answer") or draft.answer)
        ),
        "citations": list(verification.get("citations") or []),
        "answer_model": draft.model,
        "answer_mode": draft.answer_mode,
        "claim_count": len(draft.claims),
        "claim_audit_model": None,
        "claim_audit_status": "deterministic_identity_verified",
        "citation_verification_status": "verified",
    }


def _qa_orchestration_role_trace(
    *,
    search_trace: dict[str, Any],
    orchestration: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return a path-free explanation of the roles used for one QA answer."""

    stage_statuses = {
        str(stage.get("stage_id")): str(stage.get("status"))
        for stage in ((search_trace.get("pipeline_trace") or {}).get("stages") or [])
        if isinstance(stage, dict)
    }
    stage_to_role = {
        "orchestrator": "completed",
        "query_analyst": stage_statuses.get("query_analysis", "completed"),
        "query_rewriter": stage_statuses.get("query_correction", "completed"),
        "retrieval_guard": stage_statuses.get("hybrid_retrieval", "completed"),
        "reranker": stage_statuses.get("rerank_filter", "completed"),
        "context_builder": stage_statuses.get("context_build", "completed"),
        "grounded_answerer": "completed",
        "claim_auditor": (
            "skipped"
            if str(orchestration.get("claim_audit_status") or "").startswith("deterministic_")
            else str(orchestration.get("claim_audit_status") or "completed")
        ),
        "citation_verifier": str(orchestration.get("citation_verification_status") or "verified"),
    }
    role_stage_ids = {
        "query_analyst": "query_analysis",
        "query_rewriter": "query_correction",
        "retrieval_guard": "hybrid_retrieval",
        "reranker": "rerank_filter",
        "context_builder": "context_build",
        "grounded_answerer": "local_llm_answer",
        "claim_auditor": "citation_verify",
        "citation_verifier": "citation_verify",
    }
    trace: list[dict[str, Any]] = []
    security_occurrence = 0
    for role in workflow_roles("local_regulation_qa"):
        phase = "pipeline"
        stage_id = None
        if role.role_id == "security_guard":
            security_occurrence += 1
            phase = "입력 범위 검사" if security_occurrence == 1 else "출력 보안 검사"
            stage_id = "security_gate_input" if security_occurrence == 1 else "security_gate_output"
            status = (
                "completed"
                if security_occurrence == 1
                or str(orchestration.get("citation_verification_status") or "verified")
                in {"verified", "abstained"}
                else "blocked"
            )
        else:
            status = stage_to_role.get(role.role_id, "completed")
            stage_id = role_stage_ids.get(role.role_id)
        trace.append(
            {
                "role_id": role.role_id,
                "display_name": role.display_name,
                "status": status,
                "phase": phase,
                "stage_id": stage_id,
                "model_profile": role.model_profile,
                "primary_model": role.primary_model,
                "purpose": role.purpose,
                "human_decision_required": role.kind == "human_gate",
            }
        )
    return trace


def _chat_answer(
    backend: str,
    settings: Settings,
    query: str,
    results: list[dict[str, Any]],
    *,
    history: list[dict[str, str]] | None = None,
) -> tuple[str, list[str]]:
    if backend == "extractive":
        answer = _sanitize_rag_answer(_extractive_answer(query, results))
        if answer == NO_EVIDENCE_ANSWER:
            return answer, []
        supporting = select_supporting_answer_results(query, results)
        return answer, _result_evidence_ids(supporting)
    if not results:
        return NO_EVIDENCE_ANSWER, []
    try:
        result = GroundedAnswerAgent(settings).run(
            {
                "query": query,
                "evidence": results,
                "backend": backend,
                "history": history or [],
                # Preserve the API's existing explicit 503 contract for a
                # configured backend failure. The agent itself supports a
                # safe extractive fallback for other callers.
                "allow_fallback": False,
            }
        )
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Local LLM backend request failed: {type(exc).__name__}") from exc
    if result.get("status") == "unavailable":
        raise HTTPException(status_code=503, detail="Local LLM backend request failed: backend unavailable")
    try:
        verification = CitationVerifierAgent().run(
            {
                "answer": result.get("answer"),
                "evidence": results,
                "evidence_ids": result.get("evidence_ids") or [],
            }
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail="Answer citation verification failed",
        ) from exc
    if verification.get("status") == "abstained":
        return NO_EVIDENCE_ANSWER, []
    if verification.get("status") != "verified":
        raise HTTPException(status_code=503, detail="Answer citation verification failed")
    return (
        _sanitize_rag_answer(str(verification.get("verified_answer") or "")),
        [
            str(item).strip()
            for item in (verification.get("verified_evidence_ids") or [])
            if str(item).strip()
        ],
    )


def _result_evidence_ids(results: list[dict[str, Any]]) -> list[str]:
    """Return stable public evidence identities without changing result order."""

    return list(
        dict.fromkeys(
            evidence_id
            for result in results
            if (evidence_id := str(result.get("chunk_id") or result.get("document_id") or "").strip())
        )
    )


def _results_for_evidence_ids(
    results: list[dict[str, Any]],
    evidence_ids: list[str],
) -> list[dict[str, Any]]:
    """Keep only answer-supporting results while preserving retrieval order."""

    requested = {str(item).strip() for item in evidence_ids if str(item).strip()}
    if not requested:
        return []
    return [
        result
        for result in results
        if str(result.get("chunk_id") or result.get("document_id") or "").strip()
        in requested
    ]


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


# FastAPI owns request/response models and endpoint wiring. These helpers and
# their caches live in the route-free runtime backend so MCP hierarchy calls
# and API calls enforce one authorization and invalidation implementation.
BLOCKED_QUERY_PATTERNS = rag_runtime.BLOCKED_QUERY_PATTERNS
_RAG_VECTOR_CACHE_LOCK = rag_runtime._RAG_VECTOR_CACHE_LOCK
_RAG_VECTOR_RECORD_CACHE = rag_runtime._RAG_VECTOR_RECORD_CACHE
_RAG_BM25_INDEX_CACHE = rag_runtime._RAG_BM25_INDEX_CACHE
_RAG_VISIBLE_RECORDS_CACHE_LOCK = (
    rag_runtime._RAG_VISIBLE_RECORDS_CACHE_LOCK
)
_RAG_VISIBLE_RECORDS_CACHE = rag_runtime._RAG_VISIBLE_RECORDS_CACHE
_RAG_VISIBLE_RECORDS_MAX_ENTRIES = (
    rag_runtime._RAG_VISIBLE_RECORDS_MAX_ENTRIES
)
_RAG_REPOSITORY_DOCUMENT_SIGNATURE_CACHE = (
    rag_runtime._RAG_REPOSITORY_DOCUMENT_SIGNATURE_CACHE
)
_RAG_APPROVAL_JOURNAL_CACHE = rag_runtime._RAG_APPROVAL_JOURNAL_CACHE
_RAG_APPROVAL_JOURNAL_CACHE_MAX_ENTRIES = (
    rag_runtime._RAG_APPROVAL_JOURNAL_CACHE_MAX_ENTRIES
)
_RAG_APPROVAL_SNAPSHOT_CACHE = rag_runtime._RAG_APPROVAL_SNAPSHOT_CACHE
_RAG_RUNTIME_APPROVAL_IDENTITY_CACHE = (
    rag_runtime._RAG_RUNTIME_APPROVAL_IDENTITY_CACHE
)
_RAG_RUNTIME_APPROVAL_IDENTITY_CACHE_MAX_ENTRIES = (
    rag_runtime._RAG_RUNTIME_APPROVAL_IDENTITY_CACHE_MAX_ENTRIES
)
_RUNTIME_CONTENT_SIGNATURE_LOCK = (
    rag_runtime._RUNTIME_CONTENT_SIGNATURE_LOCK
)
_RUNTIME_CONTENT_SIGNATURE_CACHE = rag_runtime._RUNTIME_CONTENT_SIGNATURE_CACHE

_local_vector_path = rag_runtime.local_vector_path
_RagRequestRepositoryCache = rag_runtime.RagRequestRepositoryCache
load_visible_records = rag_runtime.load_visible_records  # noqa: F811 - compatibility hook
_load_local_vector_records = rag_runtime.load_local_vector_records
_read_local_vector_records = rag_runtime.read_local_vector_records
_load_local_vector_record_by_chunk = (
    rag_runtime.load_local_vector_record_by_chunk
)
_iter_local_vector_lines = rag_runtime.iter_local_vector_lines
_validated_local_vector_record = rag_runtime.validated_local_vector_record
_local_vector_record_matches_chunk = (
    rag_runtime.local_vector_record_matches_chunk
)
_record_visible_to_request = rag_runtime.record_visible_to_request
_expected_vector_record_for_chunk = (
    rag_runtime.expected_vector_record_for_chunk
)
_current_repository_chunk = rag_runtime.current_repository_chunk
_path_signature = rag_runtime.path_signature
_validate_query_policy = rag_runtime.validate_query_policy
_validate_security_scope = rag_runtime.validate_security_scope
_requested_security_levels = rag_runtime.requested_security_levels
_requested_department_ids = rag_runtime.requested_department_ids
_department_acl_set = rag_runtime.department_acl_set
_load_cached_bm25_index = rag_runtime.load_cached_bm25_index
_store_cached_bm25_index = rag_runtime.store_cached_bm25_index
_runtime_approval_snapshot_identity = (
    rag_runtime.runtime_approval_snapshot_identity
)
_runtime_approval_identity_chunk_paths = (
    rag_runtime.runtime_approval_identity_chunk_paths
)
_runtime_approval_identity_covers_scope = (
    rag_runtime.runtime_approval_identity_covers_scope
)
_store_runtime_approval_identity_cache = (
    rag_runtime.store_runtime_approval_identity_cache
)
_runtime_approval_snapshot_signature = (
    rag_runtime.runtime_approval_snapshot_signature
)
_runtime_approval_snapshot_path = rag_runtime.runtime_approval_snapshot_path
_runtime_approval_snapshot_file_signatures = (
    rag_runtime.runtime_approval_snapshot_file_signatures
)
_portable_file_signature = rag_runtime.portable_file_signature
_repository_chunk_files_signature = (
    rag_runtime.repository_chunk_files_signature
)
_chunk_path_identity_signature = rag_runtime.chunk_path_identity_signature
_load_runtime_approval_snapshot_sidecar = (
    rag_runtime.load_runtime_approval_snapshot_sidecar
)
_approval_snapshot_signature = rag_runtime.approval_snapshot_signature
_approval_journal_signature = rag_runtime.approval_journal_signature
_repository_documents_signature = rag_runtime.repository_documents_signature
_approval_journal_records_by_document = (
    rag_runtime.approval_journal_records_by_document
)
_approval_journal_cache_path = rag_runtime.approval_journal_cache_path
_build_approval_snapshot = rag_runtime.build_approval_snapshot
_approval_journal_match_index = rag_runtime.approval_journal_match_index
_approval_journal_match_key = rag_runtime.approval_journal_match_key
_public_search_result = rag_runtime.public_search_result
_governing_article_for_reference_chunk = (
    rag_runtime.governing_article_for_reference_chunk
)
_same_reference_context = rag_runtime.same_reference_context
_reference_context_values = rag_runtime.reference_context_values
_normalize_reference_context = rag_runtime.normalize_reference_context
_candidate_references_any_label = (
    rag_runtime.candidate_references_any_label
)
_normalized_reference_labels = rag_runtime.normalized_reference_labels
_normalize_reference_label = rag_runtime.normalize_reference_label


def _load_cached_runtime_approval_snapshot(
    repository: JsonRepository,
    document_ids: list[str],
    auth: AuthContext,
) -> dict[tuple[str, str], dict[str, Any]] | None:
    """Delegate through route globals to retain focused monkeypatch hooks."""

    return rag_runtime.load_cached_runtime_approval_snapshot(
        repository,
        document_ids,
        auth,
        identity_loader=_runtime_approval_snapshot_identity,
        signature_loader=_runtime_approval_snapshot_signature,
        sidecar_loader=_load_runtime_approval_snapshot_sidecar,
    )


def _load_cached_approval_snapshot(
    repository: JsonRepository,
    records: list[dict[str, Any]],
    auth: AuthContext,
) -> dict[tuple[str, str], dict[str, Any]]:
    return rag_runtime.load_cached_approval_snapshot(
        repository,
        records,
        auth,
        runtime_snapshot_loader=_load_cached_runtime_approval_snapshot,
        signature_loader=_approval_snapshot_signature,
        snapshot_builder=_build_approval_snapshot,
    )


def load_visible_records(**kwargs):  # noqa: F811 - retain route monkeypatch hook
    """Delegate through the route visibility hook for focused route tests."""

    return rag_runtime.load_visible_records(
        **kwargs,
        visibility_checker=_record_visible_to_request,
        lifecycle_filter=filter_to_latest_active_versions,
    )


def _raise_fastapi_http_exception(exc: Exception) -> None:
    if isinstance(exc, rag_runtime.HTTPException):
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=exc.headers,
        ) from exc
    raise exc


def _validate_query_policy(query: str) -> None:
    try:
        rag_runtime.validate_query_policy(query)
    except rag_runtime.HTTPException as exc:
        _raise_fastapi_http_exception(exc)


def _validate_security_scope(
    request: RagSearchRequest,
    auth: AuthContext,
) -> None:
    try:
        rag_runtime.validate_security_scope(request, auth)
    except rag_runtime.HTTPException as exc:
        _raise_fastapi_http_exception(exc)


def _requested_department_ids(
    request: RagSearchRequest,
    auth: AuthContext,
) -> frozenset[str]:
    try:
        return rag_runtime.requested_department_ids(request, auth)
    except rag_runtime.HTTPException as exc:
        _raise_fastapi_http_exception(exc)
    return frozenset()


def _read_local_vector_records(path: Path) -> list[dict[str, Any]]:
    return rag_runtime.read_local_vector_records(
        path,
        line_iterator=_iter_local_vector_lines,
    )


def _load_local_vector_records(
    settings: Settings,
    auth: AuthContext,
) -> list[dict[str, Any]]:
    return rag_runtime.load_local_vector_records(
        settings,
        auth,
        record_reader=_read_local_vector_records,
    )


def _load_local_vector_record_by_chunk(
    settings: Settings,
    auth: AuthContext,
    *,
    document_id: str,
    chunk_id: str,
) -> dict[str, Any] | None:
    return rag_runtime.load_local_vector_record_by_chunk(
        settings,
        auth,
        document_id=document_id,
        chunk_id=chunk_id,
        line_iterator=_iter_local_vector_lines,
    )
