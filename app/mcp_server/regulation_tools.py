from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException

from app.core.api_audit import audit_api_event
from app.core.config import Settings
from app.core.input_limits import (
    MAX_MCP_ARTICLE_NO_CHARS,
    MAX_MCP_IDENTIFIER_CHARS,
    MAX_MCP_QUERY_CHARS,
    MAX_MCP_RESULT_ID_CHARS,
    require_bounded_text,
)
from app.core.security import AuthContext, API_ROLE_ADMIN, API_ROLE_OPERATOR
from app.core.tenant_access import settings_for_tenant, tenant_storage_key
from app.processors.answer_profile import clean_answer_profile_text
from app.processors.exporter import Exporter
from app.services.regulation_catalog_service import latest_history_version
from app.retrieval.tokenizer import tokenize
from app.retrieval.hierarchical_index import (
    hierarchical_index_path,
    index_summary as hierarchical_index_summary,
    list_indexed_regulations,
    load_article_records as load_hierarchical_article_records,
    load_record_by_chunk as load_hierarchical_record_by_chunk,
    regulation_toc as load_regulation_toc,
    search_hierarchical_records,
)
from app.services import regulation_rag_service as routes_rag
from app.storage.repository import JsonRepository


DEFAULT_MCP_ACTOR = "mcp-regulation-server"
MCP_ANSWER_GROUNDING_GUIDANCE = (
    "Grounding rules for answering: "
    "(1) Use only returned verbatim_text/verbatim.text and regulation metadata as evidence; never add definitions, categories, "
    "day counts, or conditions from general knowledge. "
    "(2) This institution's terminology can differ from other Korean public-sector regulations "
    "(for example, the scope of special leave); always use this regulation's own definition. "
    "(3) Cite the source document and article for every statement. "
    "(4) If the returned text enumerates items without defining them, fetch the defining articles "
    "(search or get_article) before describing each item. "
    "(5) If no returned evidence covers the question, answer that the regulation does not specify it "
    "instead of guessing."
)
_FETCH_CHUNK_INDEX_CACHE_MAX_SIZE = 32
_FETCH_CHUNK_INDEX_LOCK = threading.Lock()
_FETCH_CHUNK_INDEX_CACHE: dict[Path, tuple[Any, dict[tuple[str, str], dict[str, Any]]]] = {}
_MCP_HEAVY_WARMUP_MAX_VECTOR_BYTES = 64 * 1024 * 1024
_BACKGROUND_TOKENIZER_WARMUP_LOCK = threading.Lock()
_BACKGROUND_TOKENIZER_WARMUP_STATUS: dict[str, Any] | None = None
_HIERARCHICAL_INDEX_VERIFICATION_LOCK = threading.Lock()
_HIERARCHICAL_INDEX_VERIFICATION_CACHE: dict[Path, tuple[Any, str, bool]] = {}
_EXTERNAL_MCP_METADATA_PROFILE = "chatgpt-data"
_MCP_METADATA_PROFILES = frozenset({"full", _EXTERNAL_MCP_METADATA_PROFILE})
_INTERNAL_CITATION_METADATA_KEYS = frozenset(
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


def _legacy_cp949_mojibake_label(value: str) -> str:
    try:
        return value.encode("utf-8").decode("cp949", errors="ignore")
    except UnicodeError:
        return ""


def _table_label_aliases(*labels: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            value
            for label in labels
            for value in (label, _legacy_cp949_mojibake_label(label))
            if value
        )
    )


_TABLE_LABEL = "\ud45c"
_APPENDIX_TABLE_LABEL = "\ubcc4\ud45c"
_APPENDIX_FORM_LABEL = "\ubcc4\uc9c0"
_TABLE_LABEL_ALIASES = _table_label_aliases(_TABLE_LABEL, _APPENDIX_TABLE_LABEL, _APPENDIX_FORM_LABEL)
_APPENDIX_TABLE_LABEL_ALIASES = _table_label_aliases(_APPENDIX_TABLE_LABEL)
_APPENDIX_FORM_LABEL_ALIASES = _table_label_aliases(_APPENDIX_FORM_LABEL)
_TABLE_LOOKUP_PREFIXES = (
    "table",
    "kordoc",
    "kordoctable",
    *_TABLE_LABEL_ALIASES,
)
_MCP_COMMON_QUERY_TOKENS = {
    "가능",
    "기준",
    "규정",
    "내용",
    "무엇",
    "방법",
    "서식",
    "언제",
    "요건",
    "있는가",
    "있나요",
    "대해서",
    "방식",
    "어떤",
    "어떻게",
    "알려줘",
    "절차",
    "제출",
    "항목",
}
_MCP_QUERY_PARTICLE_SUFFIXES = (
    "으로",
    "에서",
    "에게",
    "부터",
    "까지",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "의",
    "에",
    "로",
    "와",
    "과",
    "도",
    "만",
)
_MCP_QUERY_QUESTION_SUFFIXES = (
    "되나요",
    "하나요",
    "인가요",
    "나요",
    "습니까",
    "주세요",
)
_MCP_COMPOUND_QUERY_TERMS = (
    "\ubcf5\uc9c0\ud3ec\uc778\ud2b8",
    "\uc721\uc544\ud734\uc9c1",
    "\uc131\uacfc\uc5f0\ubd09",
    "\uc2e0\uaddc\uc784\uc6a9",
    "\uc804\uc784\uad50\uc6d0",
)
_MCP_RELEVANCE_TOKEN_EQUIVALENTS = {
    "\uc721\uc544\ud734\uc9c1": frozenset({"\uc721\uc544\ud734\uc9c1", "\uc721\uc544", "\ud734\uc9c1"}),
    "\uc804\uc784\uad50\uc6d0": frozenset({"\uc804\uc784\uad50\uc6d0", "\uc804\uc784", "\uad50\uc6d0"}),
    "채용": frozenset({"채용", "임용", "신규임용"}),
    "임용": frozenset({"임용", "채용", "신규임용"}),
    "신규임용": frozenset({"신규임용", "임용", "채용"}),
}


def start_background_tokenizer_warmup(
    sample_text: str = "regulation article table approval procedure",
    *,
    delay_seconds: float = 0.0,
) -> dict[str, Any]:
    """Warm the tokenizer without blocking MCP tool registration."""
    global _BACKGROUND_TOKENIZER_WARMUP_STATUS
    with _BACKGROUND_TOKENIZER_WARMUP_LOCK:
        if _BACKGROUND_TOKENIZER_WARMUP_STATUS is not None:
            return _BACKGROUND_TOKENIZER_WARMUP_STATUS
        status: dict[str, Any] = {
            "started": True,
            "completed": False,
            "failed": False,
            "warmup_mode": "background_tokenizer",
            "delay_seconds": max(float(delay_seconds or 0.0), 0.0),
        }
        _BACKGROUND_TOKENIZER_WARMUP_STATUS = status
        thread = threading.Thread(
            target=_background_tokenizer_warmup_worker,
            args=(status, sample_text, max(float(delay_seconds or 0.0), 0.0)),
            name="reg-rag-tokenizer-warmup",
            daemon=True,
        )
        thread.start()
        return status


def _background_tokenizer_warmup_worker(status: dict[str, Any], sample_text: str, delay_seconds: float) -> None:
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    started_at = time.perf_counter()
    try:
        tokens = tokenize(sample_text)
    except Exception as exc:  # pragma: no cover - defensive background path
        status.update(
            {
                "completed": True,
                "failed": True,
                "error": str(exc),
                "elapsed_ms": _elapsed_ms(started_at),
            }
        )
        return
    status.update(
        {
            "completed": True,
            "failed": False,
            "token_count": len(tokens),
            "elapsed_ms": _elapsed_ms(started_at),
        }
    )


def settings_for_mcp_project(
    *,
    data_dir: str | Path,
    tenant_id: str,
    tenant_storage_isolation: bool | None = None,
) -> Settings:
    base_dir = Path(data_dir)
    tenant_key = tenant_storage_key(tenant_id)
    manifest_isolation = _runtime_manifest_tenant_storage_isolation(base_dir)
    auto_isolated = manifest_isolation if manifest_isolation is not None else (base_dir / "tenants" / tenant_key).is_dir()
    base_settings = Settings(
        data_dir=base_dir,
        tenant_storage_isolation=auto_isolated if tenant_storage_isolation is None else tenant_storage_isolation,
    )
    return settings_for_tenant(base_settings, tenant_id)


def _runtime_manifest_tenant_storage_isolation(data_dir: Path) -> bool | None:
    manifest_path = data_dir / "mcp_runtime_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = manifest.get("tenant_storage_isolation")
    return value if isinstance(value, bool) else None


def mcp_auth_context(
    *,
    tenant_id: str,
    actor: str = DEFAULT_MCP_ACTOR,
    role: str = API_ROLE_OPERATOR,
    department_ids: list[str] | tuple[str, ...] | None = None,
) -> AuthContext:
    return AuthContext(
        actor=actor,
        tenant_id=tenant_id,
        auth_mode="mcp_internal",
        role=role,
        department_ids=tuple(department_ids or ()),
    )


def _normalize_optional_as_of_date(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError as exc:
        raise ValueError("as_of_date must be an ISO date in YYYY-MM-DD format.") from exc


def search_regulations(
    *,
    settings: Settings,
    auth: AuthContext,
    query: str,
    top_k: int = 5,
    security_levels: list[str] | None = None,
    department_ids: list[str] | None = None,
    document_id: str | None = None,
    profile_id: str | None = None,
    metadata_profile: str = "full",
    as_of_date: str | None = None,
) -> dict[str, Any]:
    try:
        normalized_metadata_profile = _normalize_mcp_metadata_profile(metadata_profile)
        normalized_as_of_date = _normalize_optional_as_of_date(as_of_date)
        profile_id = _require_unambiguous_profile_scope(
            settings=settings,
            auth=auth,
            profile_id=profile_id,
            inspect_vector_records=True,
        )
        query_request = routes_rag.RegulationQuery(
            query=query,
            top_k=max(1, min(int(top_k or 5), 20)),
            security_levels=security_levels,
            department_ids=department_ids or [],
            document_id=document_id,
            profile_id=profile_id,
            as_of_date=normalized_as_of_date,
        )
        _validate_mcp_security_scope(query_request, auth)
        hierarchical = _search_hierarchical_runtime(
            settings=settings,
            auth=auth,
            query=query_request,
        )
        if hierarchical is None:
            results, trace = routes_rag.search_records(query=query_request, auth=auth, settings=settings)
        else:
            results, trace = hierarchical
        relevance_guard = _mcp_relevance_guard(query, results)
        if relevance_guard["refused"]:
            results = []
    except HTTPException as exc:
        _audit_mcp_exception(settings, auth, action="mcp.search", exc=exc, document_id=document_id)
        raise ValueError(str(exc.detail)) from exc
    except ValueError as exc:
        _audit_mcp_exception(settings, auth, action="mcp.search", exc=exc, document_id=document_id)
        raise
    result_profiles = {
        str(result.get("profile_id") or "").strip()
        for result in results
        if str(result.get("profile_id") or "").strip()
    }
    resolved_profile_id = profile_id or (next(iter(result_profiles)) if len(result_profiles) == 1 else None)
    metadata = {
        "trace_id": trace["trace_id"],
        "tenant_id": auth.tenant_id,
        "result_count": len(results),
        "source": "approved_local_regulation_db",
        "profile_id": resolved_profile_id,
        "as_of_date": normalized_as_of_date,
        "timing_ms": trace.get("timing_ms") or {},
        "lifecycle_selection": trace.get("lifecycle_selection") or {},
        "retrieval_strategy": trace.get("retrieval_strategy") or "flat_rag",
        "candidate_regulations": trace.get("candidate_regulations") or [],
        "answer_guidance": MCP_ANSWER_GROUNDING_GUIDANCE,
    }
    if relevance_guard["refused"]:
        metadata.update(relevance_guard)
    if _is_external_metadata_profile(normalized_metadata_profile):
        metadata.pop("tenant_id", None)
    response = {
        "results": [_mcp_search_result(result, metadata_profile=normalized_metadata_profile) for result in results],
        "metadata": metadata,
    }
    audit_api_event(
        settings,
        auth,
        action="mcp.search",
        outcome="success",
        status_code=200,
        resource_type="mcp",
        document_id=document_id,
        detail=f"trace_id={trace['trace_id']} result_count={len(results)}",
    )
    return response


def lookup_regulation(
    *,
    settings: Settings,
    auth: AuthContext,
    query: str,
    document_id: str | None = None,
    article_no: str | None = None,
    top_k: int = 5,
    security_levels: list[str] | None = None,
    department_ids: list[str] | None = None,
    profile_id: str | None = None,
    as_of_date: str | None = None,
    metadata_profile: str = "full",
) -> dict[str, Any]:
    """Use exact approved lookup first and RAG search only when it misses."""
    bounded_query = require_bounded_text(query, field_name="query", max_chars=MAX_MCP_QUERY_CHARS)
    requested_document_id = None
    if document_id is not None:
        requested_document_id = require_bounded_text(
            document_id,
            field_name="document_id",
            max_chars=MAX_MCP_IDENTIFIER_CHARS,
            required=False,
        ) or None
    requested_article_no = None
    if article_no is not None:
        requested_article_no = require_bounded_text(
            article_no,
            field_name="article_no",
            max_chars=MAX_MCP_ARTICLE_NO_CHARS,
            required=False,
        ) or None
    normalized_as_of_date = _normalize_optional_as_of_date(as_of_date)

    direct_lookup_attempted = bool(requested_document_id)
    direct_lookup_type = "article" if requested_document_id and requested_article_no else "document"
    direct_lookup_hit = False
    fallback_reason = "exact_document_id_required"

    if requested_document_id and requested_article_no:
        direct = get_article(
            settings=settings,
            auth=auth,
            document_id=requested_document_id,
            article_no=requested_article_no,
            security_levels=security_levels,
            department_ids=department_ids,
            profile_id=profile_id,
            as_of_date=normalized_as_of_date,
        )
        direct_results = direct.get("articles") or []
        if direct_results:
            direct_lookup_hit = True
            response = {
                "results": direct_results,
                "metadata": {
                    "retrieval_mode": "direct_lookup",
                    "direct_lookup_attempted": True,
                    "direct_lookup_hit": True,
                    "fallback_used": False,
                    "direct_lookup_type": "article",
                    "document_id": requested_document_id,
                    "article_no": requested_article_no,
                    "as_of_date": normalized_as_of_date,
                    "answer_guidance": MCP_ANSWER_GROUNDING_GUIDANCE,
                },
            }
            _audit_lookup_success(settings, auth, requested_document_id, mode="direct_lookup")
            return response
        fallback_reason = "direct_article_not_found"
    elif requested_document_id:
        direct = get_document(
            settings=settings,
            auth=auth,
            document_id=requested_document_id,
            security_levels=security_levels,
            department_ids=department_ids,
            profile_id=profile_id,
            as_of_date=normalized_as_of_date,
        )
        direct_results = direct.get("chunks") or []
        if direct_results:
            direct_lookup_hit = True
            response = {
                "results": direct_results,
                "metadata": {
                    "retrieval_mode": "direct_lookup",
                    "direct_lookup_attempted": True,
                    "direct_lookup_hit": True,
                    "fallback_used": False,
                    "direct_lookup_type": "document",
                    "document_id": requested_document_id,
                    "as_of_date": normalized_as_of_date,
                    "answer_guidance": MCP_ANSWER_GROUNDING_GUIDANCE,
                },
            }
            _audit_lookup_success(settings, auth, requested_document_id, mode="direct_lookup")
            return response
        fallback_reason = "direct_document_not_found"

    response = search_regulations(
        settings=settings,
        auth=auth,
        query=bounded_query,
        top_k=top_k,
        security_levels=security_levels,
        department_ids=department_ids,
        profile_id=profile_id,
        as_of_date=normalized_as_of_date,
        metadata_profile=metadata_profile,
    )
    response["metadata"].update(
        {
            "retrieval_mode": "rag_fallback",
            "direct_lookup_attempted": direct_lookup_attempted,
            "direct_lookup_hit": direct_lookup_hit,
            "fallback_used": True,
            "direct_lookup_type": direct_lookup_type if direct_lookup_attempted else "",
            "direct_lookup_document_id": requested_document_id or "",
            "direct_lookup_article_no": requested_article_no or "",
            "fallback_reason": fallback_reason,
        }
    )
    _audit_lookup_success(settings, auth, requested_document_id, mode="rag_fallback")
    return response


def _audit_lookup_success(settings: Settings, auth: AuthContext, document_id: str | None, *, mode: str) -> None:
    audit_api_event(
        settings,
        auth,
        action="mcp.lookup",
        outcome="success",
        status_code=200,
        resource_type="mcp",
        document_id=document_id,
        detail=f"retrieval_mode={mode}",
    )


def _search_hierarchical_runtime(
    *,
    settings: Settings,
    auth: AuthContext,
    query: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    paths = _verified_hierarchical_runtime_paths(
        settings=settings,
        auth=auth,
        profile_id=str(query.profile_id or "").strip() or None,
    )
    if paths is None:
        return None
    started_at = time.perf_counter()
    scored, retrieval = search_hierarchical_records(
        paths[0],
        paths[1],
        query=str(query.query),
        top_k=max(int(query.top_k or 5) * 2, int(query.top_k or 5)),
        profile_id=str(query.profile_id or "").strip() or None,
        document_id=str(query.document_id or "").strip() or None,
        as_of_date=str(query.as_of_date or "").strip() or None,
    )
    visible_scored = [
        (score, record)
        for score, record in scored
        if _hierarchical_record_visible_to_request(
            record,
            auth=auth,
            security_levels=query.security_levels,
            department_ids=query.department_ids,
            profile_id=query.profile_id,
            document_id=query.document_id,
        )
    ][: int(query.top_k or 5)]
    related_records = [record for _score, record in visible_scored]
    results = [
        routes_rag.public_search_result(record, score, related_records=related_records)
        for score, record in visible_scored
    ]
    elapsed_ms = _elapsed_ms(started_at)
    trace = {
        "trace_id": f"hier-{uuid.uuid4().hex}",
        "retrieval_model": retrieval["retrieval_model"],
        "retrieval_strategy": retrieval["retrieval_strategy"],
        "retrieval_fallback": False,
        "candidate_regulations": retrieval.get("candidate_regulations") or [],
        "lifecycle_selection": {
            "mode": "latest_internal_regulation_version",
            "as_of_date": str(query.as_of_date or "").strip() or None,
            "selected_record_count": len(results),
            "historical_versions_available_via": "list_regulations_or_as_of_date",
        },
        "timing_ms": {
            "hierarchical_search_elapsed_ms": elapsed_ms,
            "scoring_elapsed_ms": elapsed_ms,
            "total_elapsed_ms": elapsed_ms,
        },
        **retrieval,
    }
    return results, trace


def _verified_hierarchical_runtime_paths(
    *,
    settings: Settings,
    auth: AuthContext,
    profile_id: str | None,
) -> tuple[Path, Path] | None:
    manifest_path = Path(settings.data_dir) / "mcp_runtime_manifest.json"
    index_path = hierarchical_index_path(settings.data_dir)
    vector_path = routes_rag.local_vector_path(settings, auth)
    if not manifest_path.is_file() or not index_path.is_file() or not vector_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if manifest.get("report_type") != "mcp_runtime_data_bundle":
        return None
    if str(manifest.get("tenant_id") or "").strip() != str(auth.tenant_id or "").strip():
        return None
    manifest_profile = str(manifest.get("profile_id") or "").strip()
    if profile_id and manifest_profile and manifest_profile.casefold() != profile_id.casefold():
        return None
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    expected_hash = str(files.get("hierarchical_index_sha256") or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
        return None
    try:
        stat = index_path.stat()
        signature = (stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns)
    except OSError:
        return None
    with _HIERARCHICAL_INDEX_VERIFICATION_LOCK:
        cached = _HIERARCHICAL_INDEX_VERIFICATION_CACHE.get(index_path)
        if cached and cached[0] == signature and cached[1] == expected_hash:
            return (index_path, vector_path) if cached[2] else None
    actual_hash = _file_sha256(index_path)
    valid = actual_hash == expected_hash
    if valid:
        summary = hierarchical_index_summary(index_path)
        valid = bool(
            summary
            and str(summary.get("schema_version") or "") == "reg-rag-hierarchical-index-v1"
            and str(summary.get("tenant_id") or "") == str(auth.tenant_id or "")
            and (not manifest_profile or str(summary.get("profile_id") or "").casefold() == manifest_profile.casefold())
        )
    with _HIERARCHICAL_INDEX_VERIFICATION_LOCK:
        _HIERARCHICAL_INDEX_VERIFICATION_CACHE[index_path] = (signature, expected_hash, valid)
    return (index_path, vector_path) if valid else None


def _hierarchical_record_visible_to_request(
    record: dict[str, Any],
    *,
    auth: AuthContext,
    security_levels: list[str] | None,
    department_ids: list[str] | None,
    profile_id: str | None,
    document_id: str | None,
) -> bool:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    if metadata.get("approval_status") != "approved" or not metadata.get("approval_id"):
        return False
    record_document_id = str(record.get("document_id") or metadata.get("document_id") or "")
    if document_id and record_document_id != str(document_id):
        return False
    record_profile = str(metadata.get("profile_id") or "").strip()
    if profile_id and record_profile.casefold() != str(profile_id).strip().casefold():
        return False
    allowed_levels = routes_rag.ROLE_SECURITY_LEVELS.get(auth.role, frozenset())
    requested_levels = {
        str(value or "").strip().lower()
        for value in (security_levels or allowed_levels)
        if str(value or "").strip()
    }
    if str(metadata.get("security_level") or "").strip().lower() not in requested_levels:
        return False
    acl = routes_rag.department_acl_set(metadata.get("department_acl"))
    requested_departments = routes_rag.department_acl_set(department_ids)
    if acl and requested_departments and not acl.intersection(requested_departments):
        return False
    if acl and auth.role != API_ROLE_ADMIN and not acl.intersection(set(auth.department_ids)):
        return False
    return True


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def warm_mcp_runtime(*, settings: Settings, auth: AuthContext) -> dict[str, Any]:
    started_at = time.perf_counter()
    timing_ms: dict[str, float] = {}
    vector_path = routes_rag.local_vector_path(settings, auth)
    vector_signature = routes_rag.local_vector_signature(settings=settings, auth=auth)
    vector_byte_count = int(vector_signature[1]) if vector_signature is not None else 0
    hierarchical_paths = _verified_hierarchical_runtime_paths(
        settings=settings,
        auth=auth,
        profile_id=None,
    )
    if hierarchical_paths is not None:
        summary = hierarchical_index_summary(hierarchical_paths[0]) or {}
        timing_ms["total_elapsed_ms"] = _elapsed_ms(started_at)
        return {
            "warmed": True,
            "record_count": summary.get("record_count"),
            "regulation_count": summary.get("regulation_count"),
            "regulation_version_count": summary.get("regulation_version_count"),
            "toc_node_count": summary.get("toc_node_count"),
            "hierarchical_index_ready": True,
            "warmup_mode": "hierarchical_sqlite",
            "vector_byte_count": vector_byte_count,
            "timing_ms": timing_ms,
        }
    if vector_byte_count > _MCP_HEAVY_WARMUP_MAX_VECTOR_BYTES:
        bm25_path = routes_rag.bm25_index_path(settings=settings, auth=auth)
        manifest_summary = _runtime_manifest_summary(settings.data_dir)
        timing_ms["total_elapsed_ms"] = _elapsed_ms(started_at)
        return {
            "warmed": False,
            "skipped": True,
            "warmup_mode": "lightweight",
            "skip_reason": "vector_store_exceeds_startup_warmup_budget",
            "record_count": manifest_summary.get("record_count"),
            "record_count_available": isinstance(manifest_summary.get("record_count"), int),
            "record_count_source": manifest_summary.get("record_count_source"),
            "manifest_path": manifest_summary.get("manifest_path"),
            "vector_path": str(vector_path),
            "vector_byte_count": vector_byte_count,
            "warmup_max_vector_bytes": _MCP_HEAVY_WARMUP_MAX_VECTOR_BYTES,
            "bm25_index_ready": routes_rag.path_signature(bm25_path) is not None,
            "timing_ms": timing_ms,
        }

    vector_started_at = time.perf_counter()
    records = routes_rag.load_local_vector_records(settings, auth)
    timing_ms["load_vector_records_elapsed_ms"] = _elapsed_ms(vector_started_at)

    repository = JsonRepository(settings)
    snapshot_started_at = time.perf_counter()
    approval_snapshot = routes_rag.load_cached_approval_snapshot(repository, records, auth)
    timing_ms["approval_snapshot_elapsed_ms"] = _elapsed_ms(snapshot_started_at)

    index_started_at = time.perf_counter()
    bm25_index = routes_rag.load_cached_bm25_index(routes_rag.bm25_index_path(settings=settings, auth=auth))
    timing_ms["bm25_index_elapsed_ms"] = _elapsed_ms(index_started_at)

    scoring_started_at = time.perf_counter()
    if records and bm25_index is not None:
        routes_rag.score_records(
            "육아휴직 전임 교원 채용 절차 성과연봉",
            records,
            settings=settings,
            auth=auth,
            all_records=records,
        )
    timing_ms["scoring_warmup_elapsed_ms"] = _elapsed_ms(scoring_started_at)
    timing_ms["total_elapsed_ms"] = _elapsed_ms(started_at)
    return {
        "warmed": True,
        "record_count": len(records),
        "approval_snapshot_count": len(approval_snapshot),
        "bm25_index_ready": bm25_index is not None,
        "timing_ms": timing_ms,
    }


def _runtime_manifest_summary(data_dir: Path) -> dict[str, Any]:
    manifest_path = data_dir / "mcp_runtime_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "record_count": None,
            "record_count_source": None,
            "manifest_path": str(manifest_path),
        }
    record_count = manifest.get("record_count")
    if isinstance(record_count, bool):
        record_count = None
    if isinstance(record_count, str) and record_count.strip().isdigit():
        record_count = int(record_count.strip())
    if not isinstance(record_count, int) or record_count < 0:
        record_count = None
    return {
        "record_count": record_count,
        "record_count_source": "mcp_runtime_manifest" if record_count is not None else None,
        "manifest_path": str(manifest_path),
    }


def fetch_regulation(
    *,
    settings: Settings,
    auth: AuthContext,
    result_id: str,
    security_levels: list[str] | None = None,
    department_ids: list[str] | None = None,
    profile_id: str | None = None,
    as_of_date: str | None = None,
    metadata_profile: str = "full",
) -> dict[str, Any]:
    document_id = ""
    try:
        normalized_metadata_profile = _normalize_mcp_metadata_profile(metadata_profile)
        normalized_as_of_date = _normalize_optional_as_of_date(as_of_date)
        bounded_result_id = require_bounded_text(
            result_id,
            field_name="result_id",
            max_chars=MAX_MCP_RESULT_ID_CHARS,
        )
        decoded = _decode_result_id(bounded_result_id)
        document_id = str(decoded.get("document_id") or "")
        chunk_id = str(decoded.get("chunk_id") or "")
        if not document_id or not chunk_id:
            raise ValueError("Invalid regulation result id.")
        record = _visible_record_by_chunk(
            settings=settings,
            auth=auth,
            document_id=document_id,
            chunk_id=chunk_id,
            security_levels=security_levels,
            department_ids=department_ids,
            profile_id=profile_id,
            as_of_date=normalized_as_of_date,
        )
        if record is None:
            raise ValueError("Regulation result is not available.")
    except ValueError as exc:
        _audit_mcp_exception(settings, auth, action="mcp.fetch", exc=exc, document_id=document_id or None)
        raise
    related_records = []
    if _needs_governing_article_enrichment(record):
        related_records = _visible_records(
            settings=settings,
            auth=auth,
            security_levels=security_levels,
            department_ids=department_ids,
            document_id=document_id,
            profile_id=profile_id,
            as_of_date=normalized_as_of_date,
        )
    public = routes_rag.public_search_result(record, score=1.0, related_records=related_records)
    response = _mcp_fetch_result(public, metadata_profile=normalized_metadata_profile)
    response["metadata"]["answer_guidance"] = MCP_ANSWER_GROUNDING_GUIDANCE
    response["metadata"]["as_of_date"] = normalized_as_of_date
    audit_api_event(
        settings,
        auth,
        action="mcp.fetch",
        outcome="success",
        status_code=200,
        resource_type="mcp",
        document_id=document_id,
        detail=f"chunk_id={chunk_id}",
    )
    return response


def list_documents(
    *,
    settings: Settings,
    auth: AuthContext,
    security_levels: list[str] | None = None,
    department_ids: list[str] | None = None,
    profile_id: str | None = None,
) -> dict[str, Any]:
    records = _visible_records(
        settings=settings,
        auth=auth,
        security_levels=security_levels,
        department_ids=department_ids,
        profile_id=profile_id,
    )
    repository = JsonRepository(settings)
    documents: dict[str, dict[str, Any]] = {}
    for record in records:
        metadata = record.get("metadata") or {}
        document_id = str(record.get("document_id") or metadata.get("document_id") or "")
        if not document_id or document_id in documents:
            continue
        document = repository.get_document(document_id)
        documents[document_id] = {
            "document_id": document_id,
            "title": str((document.document_name if document else "") or metadata.get("document_name") or document_id),
            "institution_name": str((document.institution_name if document else "") or metadata.get("institution_name") or ""),
            "profile_id": str((document.profile_id if document else "") or metadata.get("profile_id") or ""),
            "security_level": str(metadata.get("security_level") or ""),
            "url": _mcp_url(document_id=document_id),
        }
    results = sorted(documents.values(), key=lambda item: item["title"])
    audit_api_event(
        settings,
        auth,
        action="mcp.list_documents",
        outcome="success",
        status_code=200,
        resource_type="mcp",
        detail=f"result_count={len(results)}",
    )
    return {"documents": results}


def list_regulations(
    *,
    settings: Settings,
    auth: AuthContext,
    profile_id: str | None = None,
    query: str | None = None,
    include_history: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    """List institution-local regulations from the generated catalog."""
    resolved_profile = _require_unambiguous_profile_scope(
        settings=settings,
        auth=auth,
        profile_id=profile_id,
        inspect_vector_records=False,
    )
    paths = _verified_hierarchical_runtime_paths(
        settings=settings,
        auth=auth,
        profile_id=resolved_profile,
    )
    if paths is None:
        return {
            "regulations": [],
            "metadata": {
                "hierarchical_index_ready": False,
                "message": "Regenerate the institution MCP bundle to create the regulation catalog.",
            },
        }
    regulations = list_indexed_regulations(
        paths[0],
        profile_id=resolved_profile,
        query=query,
        include_history=include_history,
        limit=limit,
    )
    audit_api_event(
        settings,
        auth,
        action="mcp.list_regulations",
        outcome="success",
        status_code=200,
        resource_type="mcp",
        detail=f"result_count={len(regulations)} include_history={include_history}",
    )
    return {
        "regulations": regulations,
        "metadata": {
            "hierarchical_index_ready": True,
            "profile_id": resolved_profile,
            "result_count": len(regulations),
        },
    }


def get_regulation_toc(
    *,
    settings: Settings,
    auth: AuthContext,
    regulation_unit_id: str,
    profile_id: str | None = None,
    as_of_date: str | None = None,
    max_nodes: int = 1000,
) -> dict[str, Any]:
    """Return the chapter/section/article tree for one regulation version."""
    requested_unit_id = require_bounded_text(
        regulation_unit_id,
        field_name="regulation_unit_id",
        max_chars=MAX_MCP_IDENTIFIER_CHARS,
    )
    resolved_profile = _require_unambiguous_profile_scope(
        settings=settings,
        auth=auth,
        profile_id=profile_id,
        inspect_vector_records=False,
    )
    normalized_as_of = _normalize_optional_as_of_date(as_of_date)
    paths = _verified_hierarchical_runtime_paths(
        settings=settings,
        auth=auth,
        profile_id=resolved_profile,
    )
    if paths is None:
        raise ValueError("The hierarchical regulation index is not available. Regenerate the MCP bundle.")
    result = load_regulation_toc(
        paths[0],
        regulation_unit_id=requested_unit_id,
        as_of_date=normalized_as_of,
        max_nodes=max_nodes,
    )
    regulation = result.get("regulation") if isinstance(result.get("regulation"), dict) else None
    if regulation and resolved_profile and str(regulation.get("profile_id") or "").casefold() != resolved_profile.casefold():
        raise ValueError("The requested regulation is outside the selected institution profile.")
    audit_api_event(
        settings,
        auth,
        action="mcp.get_regulation_toc",
        outcome="success",
        status_code=200,
        resource_type="mcp",
        detail=f"regulation_unit_id={requested_unit_id} node_count={len(result.get('nodes') or [])}",
    )
    result["metadata"] = {
        "hierarchical_index_ready": True,
        "as_of_date": normalized_as_of,
        "node_count": len(result.get("nodes") or []),
    }
    return result


def get_regulation_article(
    *,
    settings: Settings,
    auth: AuthContext,
    regulation_unit_id: str,
    article_no: str,
    security_levels: list[str] | None = None,
    department_ids: list[str] | None = None,
    profile_id: str | None = None,
    as_of_date: str | None = None,
) -> dict[str, Any]:
    """Fetch an exact article inside a regulation unit without scanning the corpus."""
    requested_unit_id = require_bounded_text(
        regulation_unit_id,
        field_name="regulation_unit_id",
        max_chars=MAX_MCP_IDENTIFIER_CHARS,
    )
    requested_article_no = require_bounded_text(
        article_no,
        field_name="article_no",
        max_chars=MAX_MCP_ARTICLE_NO_CHARS,
    )
    resolved_profile = _require_unambiguous_profile_scope(
        settings=settings,
        auth=auth,
        profile_id=profile_id,
        inspect_vector_records=False,
    )
    normalized_as_of = _normalize_optional_as_of_date(as_of_date)
    query_request = routes_rag.RegulationQuery(
        query=f"{requested_unit_id} {requested_article_no}",
        security_levels=security_levels,
        department_ids=department_ids or [],
        profile_id=resolved_profile,
        as_of_date=normalized_as_of,
    )
    _validate_mcp_security_scope(query_request, auth)
    paths = _verified_hierarchical_runtime_paths(
        settings=settings,
        auth=auth,
        profile_id=resolved_profile,
    )
    if paths is None:
        raise ValueError("The hierarchical regulation index is not available. Regenerate the MCP bundle.")
    records = load_hierarchical_article_records(
        paths[0],
        paths[1],
        regulation_unit_id=requested_unit_id,
        article_no=requested_article_no,
        as_of_date=normalized_as_of,
    )
    visible = [
        record
        for record in records
        if _hierarchical_record_visible_to_request(
            record,
            auth=auth,
            security_levels=security_levels,
            department_ids=department_ids,
            profile_id=resolved_profile,
            document_id=None,
        )
    ]
    articles = [
        _mcp_fetch_result(routes_rag.public_search_result(record, score=1.0, related_records=visible))
        for record in visible
    ]
    audit_api_event(
        settings,
        auth,
        action="mcp.get_regulation_article",
        outcome="success",
        status_code=200,
        resource_type="mcp",
        detail=f"regulation_unit_id={requested_unit_id} article_no={requested_article_no} result_count={len(articles)}",
    )
    return {
        "regulation_unit_id": requested_unit_id,
        "article_no": requested_article_no,
        "as_of_date": normalized_as_of,
        "articles": articles,
    }


def get_regulation_history(
    *,
    settings: Settings,
    auth: AuthContext,
    regulation_id: str,
    profile_id: str | None = None,
    as_of_date: str | None = None,
) -> dict[str, Any]:
    """Return lifecycle metadata for one tenant-scoped regulation family."""
    requested_regulation_id = require_bounded_text(
        regulation_id,
        field_name="regulation_id",
        max_chars=MAX_MCP_IDENTIFIER_CHARS,
    )
    requested_profile_id = None
    if profile_id is not None:
        requested_profile_id = require_bounded_text(
            profile_id,
            field_name="profile_id",
            max_chars=MAX_MCP_IDENTIFIER_CHARS,
            required=False,
        ) or None
    repository = JsonRepository(settings)
    versions = repository.find_documents_by_regulation(
        requested_regulation_id,
        profile_id=requested_profile_id,
        tenant_id=auth.tenant_id,
    )
    if not versions:
        raise ValueError("Regulation history is not available for the current tenant.")
    version_profiles = {
        str(document.profile_id or "").strip()
        for document in versions
        if str(document.profile_id or "").strip()
    }
    if requested_profile_id is None and len(version_profiles) > 1:
        raise ValueError("profile_id is required when a regulation id exists in multiple institutions.")
    resolved_profile_id = requested_profile_id or (next(iter(version_profiles)) if version_profiles else None)
    visible_records = routes_rag.get_visible_records(
        query=routes_rag.RegulationQuery(
            query="mcp regulation history",
            profile_id=resolved_profile_id,
        ),
        auth=auth,
        settings=settings,
        repository=repository,
        use_cached_approval_snapshot=True,
        latest_only=False,
    )
    visible_document_ids = {
        str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "")
        for record in visible_records
    }
    versions = [document for document in versions if document.document_id in visible_document_ids]
    if not versions:
        raise ValueError("Regulation history is not available from approved indexed records.")
    normalized_as_of = date.fromisoformat(_normalize_optional_as_of_date(as_of_date) or date.today().isoformat())
    current_document = latest_history_version(
        versions,
        as_of=normalized_as_of,
    )
    current_document_id = str(getattr(current_document, "document_id", "") or "") if current_document else None
    versions = sorted(versions, key=_regulation_history_sort_key)
    version_document_ids = {document.document_id for document in versions}
    lifecycle_events = [
        event
        for event in repository.list_maintenance_events(event_type="regulation_lifecycle_transition")
        if str(event.get("document_id") or "") in version_document_ids
        and str(event.get("tenant_id") or auth.tenant_id or "").casefold()
        == str(auth.tenant_id or "").casefold()
    ]
    result = {
        "regulation_id": requested_regulation_id,
        "profile_id": resolved_profile_id,
        "as_of_date": normalized_as_of.isoformat(),
        "current_document_id": current_document_id,
        "lifecycle_events": lifecycle_events,
        "versions": [
            {
                "document_id": document.document_id,
                "document_name": document.document_name,
                "profile_id": document.profile_id,
                "regulation_version": document.regulation_version,
                "regulation_status": document.regulation_status,
                "revision_date": document.revision_date,
                "effective_from": document.effective_from,
                "effective_to": document.effective_to,
                "repealed_at": document.repealed_at,
                "supersedes_document_id": document.supersedes_document_id,
                "is_current": document.document_id == current_document_id,
                "is_repealed": str(document.regulation_status or "").casefold() == "repealed"
                or bool(document.repealed_at),
                "is_effective_on_as_of": _document_effective_on(document, normalized_as_of),
                "created_at": document.created_at.isoformat(),
            }
            for document in versions
        ],
    }
    audit_api_event(
        settings,
        auth,
        action="mcp.get_regulation_history",
        outcome="success",
        status_code=200,
        resource_type="mcp",
        detail=f"regulation_id={requested_regulation_id} version_count={len(versions)}",
    )
    return result


def _regulation_history_sort_key(document: Any) -> tuple[str, str, str]:
    return (
        str(getattr(document, "effective_from", "") or ""),
        str(getattr(document, "regulation_version", "") or ""),
        str(getattr(document, "document_id", "") or ""),
    )


def _document_effective_on(document: Any, reference_date: date) -> bool:
    def parse(value: Any) -> date | None:
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value or "").strip()) if str(value or "").strip() else None
        except ValueError:
            return None

    effective_from = parse(getattr(document, "effective_from", None))
    effective_to = parse(getattr(document, "effective_to", None))
    repealed_at = parse(getattr(document, "repealed_at", None))
    return bool(
        effective_from
        and effective_from <= reference_date
        and (effective_to is None or reference_date <= effective_to)
        and (repealed_at is None or reference_date < repealed_at)
    )


def get_document(
    *,
    settings: Settings,
    auth: AuthContext,
    document_id: str,
    security_levels: list[str] | None = None,
    department_ids: list[str] | None = None,
    profile_id: str | None = None,
    as_of_date: str | None = None,
) -> dict[str, Any]:
    requested_document_id = require_bounded_text(
        document_id,
        field_name="document_id",
        max_chars=MAX_MCP_IDENTIFIER_CHARS,
    )
    normalized_as_of_date = _normalize_optional_as_of_date(as_of_date)
    records = _visible_records(
        settings=settings,
        auth=auth,
        security_levels=security_levels,
        department_ids=department_ids,
        document_id=requested_document_id,
        profile_id=profile_id,
        as_of_date=normalized_as_of_date,
    )
    records = sorted(records, key=_document_record_sort_key)
    chunks = [
        _mcp_fetch_result(routes_rag.public_search_result(record, score=1.0, related_records=records))
        for record in records
    ]
    document_title = ""
    institution_name = ""
    resolved_profile_id = profile_id or ""
    if records:
        first_metadata = records[0].get("metadata") or {}
        document_title = str(first_metadata.get("document_name") or requested_document_id)
        institution_name = str(first_metadata.get("institution_name") or "")
        resolved_profile_id = str(first_metadata.get("profile_id") or resolved_profile_id)
    text = "\n\n".join(str(chunk.get("text") or "") for chunk in chunks if str(chunk.get("text") or "").strip())
    audit_api_event(
        settings,
        auth,
        action="mcp.get_document",
        outcome="success",
        status_code=200,
        resource_type="mcp",
        document_id=requested_document_id,
        detail=f"chunk_count={len(chunks)} text_chars={len(text)}",
    )
    return {
        "document_id": requested_document_id,
        "title": document_title,
        "institution_name": institution_name,
        "profile_id": resolved_profile_id,
        "chunk_count": len(chunks),
        "text_chars": len(text),
        "text": text,
        "chunks": chunks,
        "url": _mcp_url(document_id=requested_document_id),
    }


def get_article(
    *,
    settings: Settings,
    auth: AuthContext,
    document_id: str,
    article_no: str,
    security_levels: list[str] | None = None,
    department_ids: list[str] | None = None,
    profile_id: str | None = None,
    as_of_date: str | None = None,
) -> dict[str, Any]:
    requested_document_id = require_bounded_text(
        document_id,
        field_name="document_id",
        max_chars=MAX_MCP_IDENTIFIER_CHARS,
    )
    requested_article_no = require_bounded_text(
        article_no,
        field_name="article_no",
        max_chars=MAX_MCP_ARTICLE_NO_CHARS,
    )
    normalized_article_no = " ".join(requested_article_no.split())
    normalized_as_of_date = _normalize_optional_as_of_date(as_of_date)
    matches: list[dict[str, Any]] = []
    for record in _visible_records(
        settings=settings,
        auth=auth,
        security_levels=security_levels,
        department_ids=department_ids,
        document_id=requested_document_id,
        profile_id=profile_id,
        as_of_date=normalized_as_of_date,
    ):
        public = routes_rag.public_search_result(record, score=1.0)
        if " ".join(str(public.get("article_no") or "").split()) == normalized_article_no:
            matches.append(_mcp_fetch_result(public))
    audit_api_event(
        settings,
        auth,
        action="mcp.get_article",
        outcome="success",
        status_code=200,
        resource_type="mcp",
        document_id=requested_document_id,
        detail=f"article_no={normalized_article_no} result_count={len(matches)}",
    )
    return {"articles": matches}


def _normalize_table_lookup_id(value: object) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "").strip().lower())


def _table_lookup_number(value: object) -> int | None:
    normalized = _normalize_table_lookup_id(value)
    prefixes = "|".join(re.escape(prefix) for prefix in _TABLE_LOOKUP_PREFIXES)
    match = re.fullmatch(rf"(?:{prefixes})?(\d+)", normalized)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _contains_numbered_table_label(value: object, label: str, number: int) -> bool:
    normalized = _normalize_table_lookup_id(value)
    return bool(re.search(rf"{re.escape(label)}{number}(?!\d)", normalized))


def _chunk_mentions_numbered_table_label(chunk: Any, number: int, labels: tuple[str, ...]) -> bool:
    metadata = chunk.metadata if isinstance(getattr(chunk, "metadata", None), dict) else {}
    values = [
        getattr(chunk, "chunk_id", ""),
        metadata.get("table_id"),
        metadata.get("table_title"),
        metadata.get("table_appendix_no"),
        metadata.get("table_appendix_title"),
        metadata.get("article_title"),
        metadata.get("hierarchy_path"),
        getattr(chunk, "text", "")[:500],
    ]
    return any(
        any(_contains_numbered_table_label(value, label, number) for label in labels)
        for value in values
        if value
    )


def _chunk_mentions_numbered_table(chunk: Any, number: int) -> bool:
    return _chunk_mentions_numbered_table_label(chunk, number, _TABLE_LABEL_ALIASES)


def _numbered_table_label_aliases(labels: tuple[str, ...], number: int) -> set[str]:
    return {_normalize_table_lookup_id(f"{label}{number}") for label in labels}


def _chunk_table_aliases(chunk: Any, chunk_table_id: str) -> set[str]:
    metadata = chunk.metadata if isinstance(getattr(chunk, "metadata", None), dict) else {}
    values = [
        chunk_table_id,
        getattr(chunk, "chunk_id", ""),
        metadata.get("table_id"),
        metadata.get("table_title"),
        metadata.get("table_appendix_no"),
        metadata.get("table_appendix_title"),
        metadata.get("table_citation_label"),
    ]
    return {_normalize_table_lookup_id(value) for value in values if str(value or "").strip()}


def _chunk_table_id_matches(requested_table_id: str, chunk: Any, chunk_table_id: str) -> bool:
    requested = _normalize_table_lookup_id(requested_table_id)
    return requested in _chunk_table_aliases(chunk, chunk_table_id)


def _kordoc_inventory_tables(chunk: Any) -> list[dict[str, Any]]:
    metadata = chunk.metadata if isinstance(getattr(chunk, "metadata", None), dict) else {}
    inventory = metadata.get("kordoc_table_inventory")
    if not isinstance(inventory, dict):
        return []
    tables = inventory.get("tables")
    return [table for table in tables if isinstance(table, dict)] if isinstance(tables, list) else []


def _kordoc_inventory_table_index(table: dict[str, Any]) -> int | None:
    for key in ("table_index", "index", "table_no"):
        value = table.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _kordoc_inventory_table_id(chunk: Any, table: dict[str, Any]) -> str:
    for key in ("table_id", "id"):
        value = str(table.get(key) or "").strip()
        if value:
            return value
    table_index = _kordoc_inventory_table_index(table)
    if table_index is not None:
        return f"{chunk.chunk_id}_kordoc_table_{table_index}"
    fingerprint = hashlib.sha1(
        json.dumps(table.get("cell_rows") or [], ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:10]
    return f"{chunk.chunk_id}_kordoc_table_{fingerprint}"


def _kordoc_inventory_table_rows_fingerprint(table: dict[str, Any]) -> str:
    return hashlib.sha1(
        json.dumps(table.get("cell_rows") or [], ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _kordoc_inventory_table_dedup_key(chunk: Any, table: dict[str, Any]) -> tuple[str, str]:
    table_index = _kordoc_inventory_table_index(table)
    row_fingerprint = _kordoc_inventory_table_rows_fingerprint(table)
    if table_index is not None:
        return (str(getattr(chunk, "document_id", "") or ""), f"index:{table_index}:rows:{row_fingerprint}")
    return (str(getattr(chunk, "document_id", "") or ""), f"rows:{row_fingerprint}")


def _kordoc_inventory_table_aliases(chunk: Any, table: dict[str, Any]) -> set[str]:
    table_id = _kordoc_inventory_table_id(chunk, table)
    aliases = {_normalize_table_lookup_id(table_id)}
    table_index = _kordoc_inventory_table_index(table)
    if table_index is not None:
        aliases.update(
            _normalize_table_lookup_id(value)
            for value in (
                str(table_index),
                f"table{table_index}",
                f"kordoc{table_index}",
                f"kordoc_table_{table_index}",
                f"kordoc-table-{table_index}",
                f"{_TABLE_LABEL}{table_index}",
            )
        )
        if _chunk_mentions_numbered_table_label(chunk, table_index, _APPENDIX_TABLE_LABEL_ALIASES):
            aliases.update(_numbered_table_label_aliases(_APPENDIX_TABLE_LABEL_ALIASES, table_index))
        if _chunk_mentions_numbered_table_label(chunk, table_index, _APPENDIX_FORM_LABEL_ALIASES):
            aliases.update(_numbered_table_label_aliases(_APPENDIX_FORM_LABEL_ALIASES, table_index))
    for key in ("title", "caption", "name"):
        value = str(table.get(key) or "").strip()
        if value:
            aliases.add(_normalize_table_lookup_id(value))
    return aliases


def _kordoc_inventory_table_matches(requested_table_id: str, chunk: Any, table: dict[str, Any]) -> bool:
    return _normalize_table_lookup_id(requested_table_id) in _kordoc_inventory_table_aliases(chunk, table)


def _kordoc_inventory_table_title(requested_table_id: str, chunk: Any, table: dict[str, Any]) -> str:
    metadata = chunk.metadata if isinstance(getattr(chunk, "metadata", None), dict) else {}
    for key in ("title", "caption", "name"):
        value = str(table.get(key) or "").strip()
        if value:
            return value
    for value in (
        metadata.get("table_title"),
        metadata.get("table_appendix_title"),
        metadata.get("article_title"),
        str(requested_table_id or "").strip(),
    ):
        if str(value or "").strip():
            return str(value).strip()
    return _kordoc_inventory_table_id(chunk, table)


def _kordoc_inventory_table_rows(
    *,
    chunk: Any,
    table: dict[str, Any],
    table_id: str,
    title: str,
    inventory: dict[str, Any],
) -> list[dict[str, Any]]:
    metadata = chunk.metadata if isinstance(getattr(chunk, "metadata", None), dict) else {}
    raw_rows = table.get("cell_rows")
    if not isinstance(raw_rows, list):
        raw_rows = []
    rows: list[dict[str, Any]] = []
    for fallback_index, row in enumerate(raw_rows):
        row_mapping = row if isinstance(row, dict) else {"raw": str(row or "")}
        cells = row_mapping.get("cells") if isinstance(row_mapping.get("cells"), list) else []
        cells = [str(cell) for cell in cells]
        raw = str(row_mapping.get("raw") or " | ".join(cells)).strip()
        if not raw and not cells:
            continue
        rows.append(
            {
                "document_id": chunk.document_id,
                "chunk_id": chunk.chunk_id,
                "row_kind": "cell",
                "row_index": row_mapping.get("row_index", fallback_index),
                "cell_count": len(cells),
                "cells": cells,
                "header_cells": table.get("header_cells") or [],
                "record": {},
                "raw": raw,
                "source_page_start": chunk.source_page_start,
                "source_page_end": chunk.source_page_end,
                "chunk_type": chunk.chunk_type,
                "hierarchy_path": metadata.get("hierarchy_path"),
                "regulation_no": metadata.get("regulation_no"),
                "regulation_title": metadata.get("regulation_title"),
                "article_no": metadata.get("article_no"),
                "article_title": metadata.get("article_title"),
                "table_id": table_id,
                "table_title": title,
                "appendix_no": metadata.get("table_appendix_no"),
                "appendix_title": metadata.get("table_appendix_title"),
                "citation_label": metadata.get("table_citation_label") or title,
                "review_required": bool(
                    metadata.get("table_review_required") or row_mapping.get("review_required")
                ),
                "review_flags": metadata.get("table_review_flags") or [],
                "row_quality_flags": row_mapping.get("row_quality_flags") or [],
                "merged_from_row_indices": row_mapping.get("merged_from_row_indices") or [],
                "table_source": "kordoc",
                "table_geometry_source": "kordoc",
                "primary_parser_table_source": metadata.get("primary_parser_table_source") or "kordoc",
                "table_classification": metadata.get("table_classification"),
                "table_confidence": metadata.get("table_confidence"),
                "table_review_reason": metadata.get("table_review_reason"),
                "kordoc_table_parser_status": inventory.get("status") or metadata.get("kordoc_table_parser_status"),
                "kordoc_table_count": inventory.get("table_count") or metadata.get("kordoc_table_count"),
                "kordoc_table_promoted": bool(metadata.get("kordoc_table_promoted")),
                "kordoc_table_promotion_review_required": bool(
                    metadata.get("kordoc_table_promotion_review_required")
                ),
                "kordoc_table_unmatched_source": bool(metadata.get("kordoc_table_unmatched_source")),
                "table_false_positive_stability": metadata.get("table_false_positive_stability"),
                "table_header_hits": metadata.get("table_header_hits"),
                "table_numeric_rows": metadata.get("table_numeric_rows"),
                "table_delimiter_rows": metadata.get("table_delimiter_rows"),
            }
        )
    return rows


def _kordoc_inventory_table_result(
    *,
    requested_table_id: str,
    chunk: Any,
    table: dict[str, Any],
) -> dict[str, Any] | None:
    metadata = chunk.metadata if isinstance(getattr(chunk, "metadata", None), dict) else {}
    inventory = metadata.get("kordoc_table_inventory")
    if not isinstance(inventory, dict):
        return None
    table_id = _kordoc_inventory_table_id(chunk, table)
    title = _kordoc_inventory_table_title(requested_table_id, chunk, table)
    rows = _kordoc_inventory_table_rows(
        chunk=chunk,
        table=table,
        table_id=table_id,
        title=title,
        inventory=inventory,
    )
    if not rows:
        return None
    verbatim_text = "\n".join(str(row.get("raw") or "") for row in rows if row.get("raw"))
    return {
        "table_id": table_id,
        "document_id": chunk.document_id,
        "chunk_id": chunk.chunk_id,
        "title": title,
        "text": verbatim_text,
        "verbatim_text": verbatim_text,
        "verbatim": _mcp_table_verbatim_block(
            document_id=chunk.document_id,
            chunk_id=chunk.chunk_id,
            table_id=table_id,
            title=title,
            text=verbatim_text,
            metadata=metadata,
        ),
        "rows": rows,
        "metadata": {
            "approval_id": chunk.approval_id or "",
            "security_level": chunk.security_level or "",
            "source_page_start": chunk.source_page_start,
            "source_page_end": chunk.source_page_end,
            "table_like": True,
            "table_source": "kordoc",
            "table_geometry_source": "kordoc",
            "primary_parser_table_source": metadata.get("primary_parser_table_source") or "kordoc",
            "kordoc_table_parser_status": inventory.get("status") or metadata.get("kordoc_table_parser_status"),
            "kordoc_table_count": inventory.get("table_count") or metadata.get("kordoc_table_count"),
            "kordoc_table_index": _kordoc_inventory_table_index(table),
            "kordoc_table_inventory_fallback": True,
            "parser_uncertainty_flags": metadata.get("parser_uncertainty_flags") or [],
            "parser_uncertainty_risk_level": metadata.get("parser_uncertainty_risk_level") or "",
            "parser_uncertainty_confidence": metadata.get("parser_uncertainty_confidence"),
        },
    }


def get_table(
    *,
    settings: Settings,
    auth: AuthContext,
    table_id: str,
    document_id: str | None = None,
    security_levels: list[str] | None = None,
    department_ids: list[str] | None = None,
    profile_id: str | None = None,
    as_of_date: str | None = None,
) -> dict[str, Any]:
    requested_table_id = require_bounded_text(
        table_id,
        field_name="table_id",
        max_chars=MAX_MCP_IDENTIFIER_CHARS,
    )
    requested_document_id = None
    if document_id is not None:
        requested_document_id = require_bounded_text(
            document_id,
            field_name="document_id",
            max_chars=MAX_MCP_IDENTIFIER_CHARS,
            required=False,
        ) or None
    normalized_as_of_date = _normalize_optional_as_of_date(as_of_date)
    repository = JsonRepository(settings)
    repository_cache = routes_rag.repository_cache(repository)
    tables: list[dict[str, Any]] = []
    seen_kordoc_inventory_tables: set[tuple[str, str]] = set()
    kordoc_inventory_satisfied_aliases: set[str] = set()
    normalized_requested_table_id = _normalize_table_lookup_id(requested_table_id)
    for record in _visible_records(
        settings=settings,
        auth=auth,
        security_levels=security_levels,
        department_ids=department_ids,
        document_id=requested_document_id,
        profile_id=profile_id,
        as_of_date=normalized_as_of_date,
        use_cached_approval_snapshot=False,
    ):
        metadata = record.get("metadata") or {}
        current_document_id = str(record.get("document_id") or metadata.get("document_id") or "")
        chunk_id = str(record.get("chunk_id") or metadata.get("chunk_id") or "")
        chunk = routes_rag.current_repository_chunk(
            repository,
            current_document_id,
            chunk_id,
            repository_cache=repository_cache,
        )
        if chunk is None:
            continue
        document = routes_rag.repository_document(repository_cache, current_document_id)
        if document is None or not _repository_chunk_matches_visible_record(
            record=record,
            chunk=chunk,
            document=document,
            auth=auth,
        ):
            continue
        inventory_matched = False
        for inventory_table in _kordoc_inventory_tables(chunk):
            if not _kordoc_inventory_table_matches(requested_table_id, chunk, inventory_table):
                continue
            inventory_key = _kordoc_inventory_table_dedup_key(chunk, inventory_table)
            if inventory_key in seen_kordoc_inventory_tables:
                inventory_matched = True
                continue
            table_result = _kordoc_inventory_table_result(
                requested_table_id=requested_table_id,
                chunk=chunk,
                table=inventory_table,
            )
            if table_result is None:
                continue
            inventory_matched = True
            seen_kordoc_inventory_tables.add(inventory_key)
            kordoc_inventory_satisfied_aliases.update(_kordoc_inventory_table_aliases(chunk, inventory_table))
            tables.append(table_result)
        if inventory_matched:
            continue
        if normalized_requested_table_id in kordoc_inventory_satisfied_aliases:
            continue
        chunk_table_id = str(chunk.metadata.get("table_id") or f"{chunk.chunk_id}_table")
        if not _chunk_table_id_matches(requested_table_id, chunk, chunk_table_id):
            continue
        rows = Exporter().table_rows([chunk])
        if not rows and not chunk.metadata.get("table_like") and chunk.chunk_type not in {"appendix", "form", "table"}:
            continue
        tables.append(
            {
                "table_id": chunk_table_id,
                "document_id": chunk.document_id,
                "chunk_id": chunk.chunk_id,
                "title": str(
                    chunk.metadata.get("table_title")
                    or chunk.metadata.get("table_appendix_title")
                    or chunk.metadata.get("article_title")
                    or chunk_table_id
                ),
                "text": chunk.retrieval_text or chunk.normalized_text or chunk.text,
                "verbatim_text": chunk.retrieval_text or chunk.normalized_text or chunk.text,
                "verbatim": _mcp_table_verbatim_block(
                    document_id=chunk.document_id,
                    chunk_id=chunk.chunk_id,
                    table_id=chunk_table_id,
                    title=str(
                        chunk.metadata.get("table_title")
                        or chunk.metadata.get("table_appendix_title")
                        or chunk.metadata.get("article_title")
                        or chunk_table_id
                    ),
                    text=chunk.retrieval_text or chunk.normalized_text or chunk.text,
                    metadata=chunk.metadata,
                ),
                "rows": rows,
                "metadata": {
                    "approval_id": chunk.approval_id or "",
                    "security_level": chunk.security_level or "",
                    "source_page_start": chunk.source_page_start,
                    "source_page_end": chunk.source_page_end,
                    "table_like": bool(chunk.metadata.get("table_like")),
                },
            }
        )
    audit_api_event(
        settings,
        auth,
        action="mcp.get_table",
        outcome="success",
        status_code=200,
        resource_type="mcp",
        document_id=requested_document_id,
        detail=f"table_id={requested_table_id} result_count={len(tables)}",
    )
    return {"tables": tables}


def _mcp_table_verbatim_block(
    *,
    document_id: str,
    chunk_id: str,
    table_id: str,
    title: str,
    text: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "text": str(text or ""),
        "is_verbatim": bool(str(text or "")),
        "source": "approved_local_regulation_table",
        "document_id": str(document_id or ""),
        "chunk_id": str(chunk_id or ""),
        "table_id": str(table_id or ""),
        "title": str(title or ""),
        "source_page_start": metadata.get("source_page_start"),
        "source_page_end": metadata.get("source_page_end"),
        "approval_id": str(metadata.get("approval_id") or ""),
        "display_guidance": "Display this table text and rows as approved regulation evidence; do not invent missing cells.",
    }


def _repository_chunk_matches_visible_record(
    *,
    record: dict[str, Any],
    chunk: Any,
    document: Any,
    auth: AuthContext,
) -> bool:
    metadata = record.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    expected = routes_rag.expected_vector_record_for_chunk(chunk, document, auth)
    if expected is None:
        return False
    expected_metadata = expected.get("metadata")
    expected_metadata = expected_metadata if isinstance(expected_metadata, dict) else {}
    if str(record.get("document_id") or metadata.get("document_id") or "") != str(
        expected.get("document_id") or expected_metadata.get("document_id") or ""
    ):
        return False
    if str(record.get("chunk_id") or metadata.get("chunk_id") or "") != str(
        expected.get("chunk_id") or expected_metadata.get("chunk_id") or ""
    ):
        return False
    if str(record.get("content_hash") or "") != str(expected.get("content_hash") or ""):
        return False
    for key in ("approval_status", "security_level"):
        if str(metadata.get(key) or "").strip().lower() != str(expected_metadata.get(key) or "").strip().lower():
            return False
    for key in ("approval_id", "approved_content_hash"):
        if str(metadata.get(key) or "").strip() != str(expected_metadata.get(key) or "").strip():
            return False
    if routes_rag.department_acl_set(metadata.get("department_acl")) != routes_rag.department_acl_set(
        expected_metadata.get("department_acl")
    ):
        return False
    return True


def compare_versions(
    *,
    settings: Settings,
    auth: AuthContext,
    base_document_id: str,
    target_document_id: str,
    security_levels: list[str] | None = None,
    department_ids: list[str] | None = None,
    profile_id: str | None = None,
) -> dict[str, Any]:
    requested_base_document_id = require_bounded_text(
        base_document_id,
        field_name="base_document_id",
        max_chars=MAX_MCP_IDENTIFIER_CHARS,
    )
    requested_target_document_id = require_bounded_text(
        target_document_id,
        field_name="target_document_id",
        max_chars=MAX_MCP_IDENTIFIER_CHARS,
    )
    base_items = _comparison_items(
        settings=settings,
        auth=auth,
        document_id=requested_base_document_id,
        security_levels=security_levels,
        department_ids=department_ids,
        profile_id=profile_id,
        latest_only=False,
    )
    target_items = _comparison_items(
        settings=settings,
        auth=auth,
        document_id=requested_target_document_id,
        security_levels=security_levels,
        department_ids=department_ids,
        profile_id=profile_id,
        latest_only=False,
    )
    base_keys = set(base_items)
    target_keys = set(target_items)
    added = sorted(target_keys - base_keys)
    removed = sorted(base_keys - target_keys)
    changed = sorted(
        key for key in base_keys.intersection(target_keys) if base_items[key]["content_hash"] != target_items[key]["content_hash"]
    )
    result = {
        "base_document_id": requested_base_document_id,
        "target_document_id": requested_target_document_id,
        "summary": {
            "base_item_count": len(base_items),
            "target_item_count": len(target_items),
            "added_count": len(added),
            "removed_count": len(removed),
            "changed_count": len(changed),
        },
        "added": [target_items[key] for key in added[:20]],
        "removed": [base_items[key] for key in removed[:20]],
        "changed": [
            {
                "key": key,
                "base": base_items[key],
                "target": target_items[key],
            }
            for key in changed[:20]
        ],
    }
    audit_api_event(
        settings,
        auth,
        action="mcp.compare_versions",
        outcome="success",
        status_code=200,
        resource_type="mcp",
        document_id=requested_target_document_id,
        detail=(
            f"base_document_id={requested_base_document_id} added={len(added)} "
            f"removed={len(removed)} changed={len(changed)}"
        ),
    )
    return result


def get_citation(
    *,
    settings: Settings,
    auth: AuthContext,
    result_id: str,
    profile_id: str | None = None,
) -> dict[str, Any]:
    fetched = fetch_regulation(settings=settings, auth=auth, result_id=result_id, profile_id=profile_id)
    result = {
        "id": fetched["id"],
        "title": fetched["title"],
        "url": fetched["url"],
        "metadata": fetched["metadata"],
    }
    audit_api_event(
        settings,
        auth,
        action="mcp.get_citation",
        outcome="success",
        status_code=200,
        resource_type="mcp",
        document_id=str(fetched["metadata"].get("document_id") or ""),
        detail=f"chunk_id={fetched['metadata'].get('chunk_id') or ''}",
    )
    return result


def get_index_status(
    *,
    settings: Settings,
    auth: AuthContext,
    document_id: str | None = None,
    security_levels: list[str] | None = None,
    department_ids: list[str] | None = None,
    profile_id: str | None = None,
) -> dict[str, Any]:
    requested_document_id = None
    if document_id is not None:
        requested_document_id = require_bounded_text(
            document_id,
            field_name="document_id",
            max_chars=MAX_MCP_IDENTIFIER_CHARS,
            required=False,
        ) or None
    hierarchy_paths = _verified_hierarchical_runtime_paths(
        settings=settings,
        auth=auth,
        profile_id=profile_id,
    )
    if hierarchy_paths is not None and requested_document_id is None:
        hierarchy = hierarchical_index_summary(hierarchy_paths[0]) or {}
        regulations = list_indexed_regulations(
            hierarchy_paths[0],
            profile_id=profile_id,
            include_history=False,
            limit=1000,
        )
        audit_api_event(
            settings,
            auth,
            action="mcp.index_status",
            outcome="success",
            status_code=200,
            resource_type="mcp",
            detail=f"hierarchical_regulation_count={len(regulations)}",
        )
        return {
            "documents": [],
            "regulations": regulations,
            "summary": {
                "document_count": len({item["document_id"] for item in regulations}),
                "status_counts": {"hierarchical_indexed": len(regulations)},
                "tenant_id": auth.tenant_id,
                "profile_id": profile_id,
                "hierarchical_index_ready": True,
                "regulation_count": hierarchy.get("regulation_count"),
                "regulation_version_count": hierarchy.get("regulation_version_count"),
                "toc_node_count": hierarchy.get("toc_node_count"),
                "record_count": hierarchy.get("record_count"),
            },
        }
    repository = JsonRepository(settings)
    document_ids = _index_status_document_ids(
        repository=repository,
        settings=settings,
        auth=auth,
        document_id=requested_document_id,
        security_levels=security_levels,
        department_ids=department_ids,
        profile_id=profile_id,
    )
    statuses = [
        _document_index_status(
            repository=repository,
            settings=settings,
            auth=auth,
            document_id=current_document_id,
            security_levels=security_levels,
            department_ids=department_ids,
            profile_id=profile_id,
        )
        for current_document_id in document_ids
    ]
    status_counts: dict[str, int] = {}
    for item in statuses:
        status = str(item.get("indexing_status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    audit_api_event(
        settings,
        auth,
        action="mcp.index_status",
        outcome="success",
        status_code=200,
        resource_type="mcp",
        document_id=requested_document_id,
        detail=f"document_count={len(statuses)}",
    )
    return {
        "documents": statuses,
        "summary": {
            "document_count": len(statuses),
            "status_counts": status_counts,
            "tenant_id": auth.tenant_id,
            "profile_id": profile_id,
        },
    }


def _index_status_document_ids(
    *,
    repository: JsonRepository,
    settings: Settings,
    auth: AuthContext,
    document_id: str | None,
    security_levels: list[str] | None,
    department_ids: list[str] | None,
    profile_id: str | None,
) -> list[str]:
    records = _visible_records(
        settings=settings,
        auth=auth,
        security_levels=security_levels,
        department_ids=department_ids,
        document_id=document_id,
        profile_id=profile_id,
    )
    if document_id:
        return [document_id] if records else []
    document_ids: set[str] = set()
    document_ids.update(
        str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "")
        for record in records
    )
    return sorted(document_id for document_id in document_ids if document_id)


def _document_index_status(
    *,
    repository: JsonRepository,
    settings: Settings,
    auth: AuthContext,
    document_id: str,
    security_levels: list[str] | None,
    department_ids: list[str] | None,
    profile_id: str | None,
) -> dict[str, Any]:
    document = repository.get_document(document_id)
    approvals = repository.list_approval_journal_records(document_id)
    jobs = repository.list_indexing_jobs(document_id)
    latest_job = jobs[-1] if jobs else None
    vector_records = _visible_records(
        settings=settings,
        auth=auth,
        security_levels=security_levels,
        department_ids=department_ids,
        document_id=document_id,
        profile_id=profile_id,
    )
    indexing_status = str(latest_job.get("status") or "not_indexed") if latest_job else "not_indexed"
    if latest_job and indexing_status == "indexed" and _can_compare_full_index_count(auth, security_levels, department_ids):
        expected_count = int(latest_job.get("record_count") or 0)
        if expected_count != len(vector_records):
            indexing_status = "reindex_required"
    elif not latest_job and not approvals:
        indexing_status = "review_required"
    elif not latest_job and vector_records:
        indexing_status = "indexed_untracked"
    visible_approval_ids = {
        str((record.get("metadata") or {}).get("approval_id") or "")
        for record in vector_records
        if (record.get("metadata") or {}).get("approval_id")
    }
    visible_chunk_ids = {
        str(record.get("chunk_id") or (record.get("metadata") or {}).get("chunk_id") or "")
        for record in vector_records
    }
    visible_approvals = [
        record
        for record in approvals
        if str(record.get("approval_id") or "") in visible_approval_ids
        or bool(set(record.get("chunk_ids") or []).intersection(visible_chunk_ids))
    ]
    can_see_job_metadata = auth.role in {API_ROLE_ADMIN, API_ROLE_OPERATOR}
    return {
        "document_id": document_id,
        "title": str((document.document_name if document else "") or document_id),
        "url": _mcp_url(document_id=document_id),
        "indexing_status": indexing_status,
        "approved_record_count": len(visible_approvals),
        "vector_record_count": len(vector_records),
        "job_count": len(jobs) if can_see_job_metadata else 0,
        "latest_job": _public_index_job(latest_job, visible_record_count=len(vector_records)) if can_see_job_metadata else None,
    }


def _can_compare_full_index_count(
    auth: AuthContext,
    security_levels: list[str] | None,
    department_ids: list[str] | None,
) -> bool:
    return auth.role == API_ROLE_ADMIN and not security_levels and not department_ids


def _public_index_job(job: dict[str, Any] | None, *, visible_record_count: int) -> dict[str, Any] | None:
    if not job:
        return None
    return {
        "indexing_job_id": str(job.get("indexing_job_id") or ""),
        "status": str(job.get("status") or ""),
        "created_at": str(job.get("created_at") or ""),
        "completed_at": str(job.get("completed_at") or ""),
        "target_type": str(job.get("target_type") or ""),
        "record_count": visible_record_count,
        "embedding_model": str(job.get("embedding_model") or ""),
        "embedding_dimensions": int(job.get("embedding_dimensions") or 0),
    }


def _comparison_items(
    *,
    settings: Settings,
    auth: AuthContext,
    document_id: str,
    security_levels: list[str] | None,
    department_ids: list[str] | None,
    profile_id: str | None,
    latest_only: bool = True,
) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    repository = JsonRepository(settings)
    for record in _visible_records(
        settings=settings,
        auth=auth,
        security_levels=security_levels,
        department_ids=department_ids,
        document_id=document_id,
        profile_id=profile_id,
        latest_only=latest_only,
    ):
        public = routes_rag.public_search_result(record, score=1.0)
        key = str(public.get("article_no") or public.get("chunk_id") or "")
        chunk = routes_rag.current_repository_chunk(
            repository,
            str(public.get("document_id") or ""),
            str(public.get("chunk_id") or ""),
        )
        chunk_id = str(public.get("chunk_id") or "")
        comparison_hash = str(getattr(chunk, "approved_content_hash", "") or record.get("content_hash") or "")
        if key not in items:
            items[key] = {
                "key": key,
                "document_id": str(public.get("document_id") or ""),
                "chunk_id": chunk_id,
                "chunk_ids": [],
                "chunk_count": 0,
                "article_no": str(public.get("article_no") or ""),
                "article_title": str(public.get("article_title") or ""),
                "title": _result_title(public),
                "content_hash": "",
                "vector_content_hash": "",
                "approved_content_hash": "",
                "text_preview": "",
                "approval_id": str(public.get("approval_id") or ""),
                "_content_hash_parts": [],
                "_vector_content_hash_parts": [],
                "_approved_content_hash_parts": [],
            }
        item = items[key]
        item["chunk_ids"].append(chunk_id)
        item["chunk_count"] = len(item["chunk_ids"])
        item["_content_hash_parts"].append({"chunk_id": chunk_id, "hash": comparison_hash})
        item["_vector_content_hash_parts"].append({"chunk_id": chunk_id, "hash": str(record.get("content_hash") or "")})
        item["_approved_content_hash_parts"].append(
            {"chunk_id": chunk_id, "hash": str(getattr(chunk, "approved_content_hash", "") or "")}
        )
        text = str(public.get("text") or "")
        if text:
            item["text_preview"] = (str(item.get("text_preview") or "") + ("\n" if item.get("text_preview") else "") + text)[:300]
    for item in items.values():
        item["content_hash"] = _aggregate_comparison_hash(item.pop("_content_hash_parts", []))
        item["vector_content_hash"] = _aggregate_comparison_hash(item.pop("_vector_content_hash_parts", []))
        item["approved_content_hash"] = _aggregate_comparison_hash(item.pop("_approved_content_hash_parts", []))
    return items


def _aggregate_comparison_hash(parts: list[dict[str, str]]) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return str(parts[0].get("hash") or "")
    canonical = json.dumps(
        sorted(parts, key=lambda item: str(item.get("chunk_id") or "")),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _visible_record_by_chunk(
    *,
    settings: Settings,
    auth: AuthContext,
    document_id: str,
    chunk_id: str,
    security_levels: list[str] | None,
    department_ids: list[str] | None,
    profile_id: str | None,
    as_of_date: str | None = None,
) -> dict[str, Any] | None:
    profile_id = _require_unambiguous_profile_scope(
        settings=settings,
        auth=auth,
        profile_id=profile_id,
        inspect_vector_records=False,
    )
    query_request = routes_rag.RegulationQuery(
        query="mcp fetch",
        top_k=1,
        security_levels=security_levels,
        department_ids=department_ids or [],
        document_id=document_id,
        profile_id=profile_id,
        as_of_date=as_of_date,
    )
    _validate_mcp_security_scope(query_request, auth)
    candidate = _indexed_vector_record_by_chunk(
        settings=settings,
        auth=auth,
        document_id=document_id,
        chunk_id=chunk_id,
    )
    repository = JsonRepository(settings)
    return routes_rag.get_visible_record_by_chunk(
        query=query_request,
        auth=auth,
        settings=settings,
        repository=repository,
        candidate=candidate,
    )


def _needs_governing_article_enrichment(record: dict[str, Any]) -> bool:
    metadata = record.get("metadata") or {}
    if metadata.get("article_no") and metadata.get("article_title"):
        return False
    return bool((metadata.get("form_refs") or []) or (metadata.get("appendix_refs") or []))


def _indexed_vector_record_by_chunk(
    *,
    settings: Settings,
    auth: AuthContext,
    document_id: str,
    chunk_id: str,
) -> dict[str, Any] | None:
    hierarchical_paths = _verified_hierarchical_runtime_paths(
        settings=settings,
        auth=auth,
        profile_id=None,
    )
    if hierarchical_paths is not None:
        record = load_hierarchical_record_by_chunk(
            hierarchical_paths[0],
            hierarchical_paths[1],
            document_id=document_id,
            chunk_id=chunk_id,
        )
        if record is not None:
            return record
    vector_path = routes_rag.local_vector_path(settings, auth)
    signature = routes_rag.path_signature(vector_path)
    if signature is not None:
        with _FETCH_CHUNK_INDEX_LOCK:
            cached = _FETCH_CHUNK_INDEX_CACHE.get(vector_path)
            if cached and cached[0] == signature:
                record = cached[1].get((document_id, chunk_id))
                if record is not None:
                    return record

    record = routes_rag.load_local_vector_record_by_chunk(
        settings,
        auth,
        document_id=document_id,
        chunk_id=chunk_id,
    )
    if signature is not None:
        with _FETCH_CHUNK_INDEX_LOCK:
            if vector_path not in _FETCH_CHUNK_INDEX_CACHE and len(_FETCH_CHUNK_INDEX_CACHE) >= _FETCH_CHUNK_INDEX_CACHE_MAX_SIZE:
                _FETCH_CHUNK_INDEX_CACHE.pop(next(iter(_FETCH_CHUNK_INDEX_CACHE)), None)
            cached = _FETCH_CHUNK_INDEX_CACHE.get(vector_path)
            index = dict(cached[1]) if cached and cached[0] == signature else {}
            if record is not None:
                index[(document_id, chunk_id)] = record
            _FETCH_CHUNK_INDEX_CACHE[vector_path] = (signature, index)
    return record


def _visible_records(
    *,
    settings: Settings,
    auth: AuthContext,
    security_levels: list[str] | None = None,
    department_ids: list[str] | None = None,
    document_id: str | None = None,
    profile_id: str | None = None,
    as_of_date: str | None = None,
    use_cached_approval_snapshot: bool = True,
    latest_only: bool = True,
) -> list[dict[str, Any]]:
    profile_id = _require_unambiguous_profile_scope(
        settings=settings,
        auth=auth,
        profile_id=profile_id,
        inspect_vector_records=True,
    )
    query_request = routes_rag.RegulationQuery(
        query="mcp fetch",
        top_k=20,
        security_levels=security_levels,
        department_ids=department_ids or [],
        document_id=document_id,
        profile_id=profile_id,
        as_of_date=as_of_date,
    )
    repository = JsonRepository(settings)
    _validate_mcp_security_scope(query_request, auth)
    return routes_rag.get_visible_records(
        query=query_request,
        auth=auth,
        settings=settings,
        repository=repository,
        use_cached_approval_snapshot=use_cached_approval_snapshot,
        latest_only=latest_only,
    )


def _require_unambiguous_profile_scope(
    *,
    settings: Settings,
    auth: AuthContext,
    profile_id: str | None,
    inspect_vector_records: bool,
) -> str | None:
    """Require an explicit institution when one tenant contains several.

    MCP tool calls are often made directly in-process, so this guard lives
    below the FastMCP wrappers as well.  Repository documents and approved
    vector metadata are both considered; an unbound runtime never silently
    merges evidence from multiple institution profiles.
    """

    requested = str(profile_id or "").strip() or None
    if requested:
        return requested

    profiles = {
        str(document.profile_id or "").strip().casefold()
        for document in JsonRepository(settings).list_documents()
        if str(document.tenant_id or "").strip() == str(auth.tenant_id or "").strip()
        and str(document.profile_id or "").strip()
    }
    if inspect_vector_records and len(profiles) < 2:
        for record in routes_rag.load_local_vector_records(settings, auth):
            metadata = record.get("metadata") if isinstance(record, dict) else None
            if not isinstance(metadata, dict):
                continue
            record_profile = str(metadata.get("profile_id") or record.get("profile_id") or "").strip()
            if record_profile:
                profiles.add(record_profile.casefold())
                if len(profiles) >= 2:
                    break
    if len(profiles) > 1:
        raise ValueError(
            "profile_id is required when the tenant contains multiple institution profiles."
        )
    return requested


def _document_record_sort_key(record: dict[str, Any]) -> tuple[int, str, str]:
    metadata = record.get("metadata") or {}
    page_value = metadata.get("source_page_start")
    try:
        page = int(page_value) if page_value is not None else 999_999
    except (TypeError, ValueError):
        page = 999_999
    hierarchy_path = str(metadata.get("hierarchy_path") or "")
    chunk_id = str(record.get("chunk_id") or metadata.get("chunk_id") or "")
    return (page, hierarchy_path, chunk_id)


def _mcp_relevance_guard(query: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return {"refused": False}
    query_tokens = _mcp_meaningful_query_tokens(query)
    if not query_tokens:
        return {"refused": False}
    top_result = results[0]
    result_tokens = _mcp_result_tokens(top_result)
    overlap_tokens = [token for token in query_tokens if _mcp_token_matches(token, result_tokens)]
    primary_anchor = _mcp_primary_anchor_token(query_tokens)
    primary_anchor_hit = bool(primary_anchor and _mcp_token_matches(primary_anchor, result_tokens))
    overlap_ratio = round(len(overlap_tokens) / len(query_tokens), 3)
    refused = False
    reason = ""
    if not overlap_tokens:
        refused = True
        reason = "no_query_token_overlap"
    elif primary_anchor and not primary_anchor_hit and len(query_tokens) >= 3 and len(overlap_tokens) <= 2:
        if not _mcp_missing_anchor_tolerated(primary_anchor, overlap_tokens):
            refused = True
            reason = "missing_primary_query_anchor"
    if not refused:
        return {"refused": False}
    return {
        "refused": True,
        "refusal_reason": "insufficient_relevance",
        "refusal_detail": reason,
        "top_score": float(top_result.get("score") or 0.0),
        "query_token_count": len(query_tokens),
        "overlap_token_count": len(overlap_tokens),
        "overlap_ratio": overlap_ratio,
        "primary_anchor_token": primary_anchor or "",
        "primary_anchor_hit": primary_anchor_hit,
    }


def _mcp_meaningful_query_tokens(query: str) -> list[str]:
    tokens: list[str] = []
    compound_terms = _mcp_compound_query_terms(query)
    for term in compound_terms:
        if term not in tokens:
            tokens.append(term)
    for token in tokenize(query, prefer_regex_if_kiwi_cold=True):
        normalized = str(token or "").strip().lower()
        normalized = _mcp_normalize_query_token(normalized)
        if len(normalized) < 2 or normalized in _MCP_COMMON_QUERY_TOKENS:
            continue
        if any(normalized != term and normalized in term for term in compound_terms):
            continue
        if normalized not in tokens:
            tokens.append(normalized)
    return tokens


def _mcp_compound_query_terms(query: str) -> list[str]:
    lowered = str(query or "").lower()
    compact = re.sub(r"\s+", "", lowered)
    return [term for term in _MCP_COMPOUND_QUERY_TERMS if term in lowered or term in compact]


def _mcp_normalize_query_token(token: str) -> str:
    normalized = str(token or "").strip().lower()
    if not normalized or normalized in _MCP_COMMON_QUERY_TOKENS:
        return ""
    if any(normalized.endswith(suffix) for suffix in _MCP_QUERY_QUESTION_SUFFIXES):
        return ""
    for suffix in _MCP_QUERY_PARTICLE_SUFFIXES:
        if normalized.endswith(suffix) and len(normalized) - len(suffix) >= 2:
            stripped = normalized[: -len(suffix)]
            if stripped in _MCP_COMMON_QUERY_TOKENS:
                return ""
            return stripped
    return normalized


def _mcp_token_matches(query_token: str, result_tokens: set[str]) -> bool:
    if query_token in result_tokens:
        return True
    if _mcp_compound_token_matches(query_token, result_tokens):
        return True
    equivalent_tokens = _MCP_RELEVANCE_TOKEN_EQUIVALENTS.get(query_token)
    if not equivalent_tokens:
        return False
    return bool(equivalent_tokens.intersection(result_tokens))


def _mcp_missing_anchor_tolerated(primary_anchor: str, overlap_tokens: list[str]) -> bool:
    if len(overlap_tokens) < 2:
        return False
    if primary_anchor in {"채용", "임용", "신규임용"}:
        return True
    return False


def _mcp_compound_token_matches(query_token: str, result_tokens: set[str]) -> bool:
    normalized_query = str(query_token or "").strip().lower()
    if len(normalized_query) < 2:
        return False
    for token in result_tokens:
        normalized_result = str(token or "").strip().lower()
        if len(normalized_result) < 3:
            continue
        if normalized_query in normalized_result:
            return True
    return False


def _mcp_primary_anchor_token(tokens: list[str]) -> str:
    candidates = [token for token in tokens if token not in _MCP_COMMON_QUERY_TOKENS]
    if not candidates:
        return ""
    return sorted(candidates, key=lambda token: (len(token), token), reverse=True)[0]


def _mcp_result_tokens(result: dict[str, Any]) -> set[str]:
    values: list[Any] = [
        result.get("text"),
        result.get("document_name"),
        result.get("regulation_title"),
        result.get("article_no"),
        result.get("article_title"),
        result.get("hierarchy_path"),
    ]
    for field in ("answer_keywords", "answer_outline", "answer_facts"):
        value = result.get(field)
        if isinstance(value, list):
            values.extend(value)
    tokens: set[str] = set()
    index = 0
    while index < len(values):
        value = values[index]
        index += 1
        if isinstance(value, dict):
            values.extend(value.values())
            continue
        tokens.update(tokenize(str(value or ""), prefer_regex_if_kiwi_cold=True))
        # OCR table headers often arrive as spaced syllables (e.g. "겸 직 자
        # 명 부"). Keep a compact form for relevance guarding so a precise
        # table hit is not rejected before fetch returns its verbatim text.
        compact_value = re.sub(r"\s+", "", str(value or "").strip().lower())
        if len(compact_value) >= 2:
            tokens.add(compact_value)
    return {str(token or "").strip().lower() for token in tokens if str(token or "").strip()}


def _validate_mcp_security_scope(request: routes_rag.RegulationQuery, auth: AuthContext) -> None:
    try:
        routes_rag.validate_query_security_scope(query=request, auth=auth)
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from exc


def _audit_mcp_exception(
    settings: Settings,
    auth: AuthContext,
    *,
    action: str,
    exc: Exception,
    document_id: str | None = None,
) -> None:
    status_code, outcome = _mcp_exception_status(exc)
    audit_api_event(
        settings,
        auth,
        action=action,
        outcome=outcome,
        status_code=status_code,
        resource_type="mcp",
        document_id=document_id,
        detail=str(exc),
    )


def _mcp_exception_status(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, HTTPException):
        status_code = int(exc.status_code)
        return status_code, "denied" if status_code in {401, 403, 404} else "failure"
    message = str(exc).lower()
    if "security level" in message or "department" in message or "not allowed" in message:
        return 403, "denied"
    if "not available" in message:
        return 404, "denied"
    if (
        "invalid" in message
        or "metadata_profile" in message
        or "validation error" in message
        or "at most" in message
        or "is required" in message
    ):
        return 400, "failure"
    return 500, "failure"


def _mcp_search_result(result: dict[str, Any], *, metadata_profile: str = "full") -> dict[str, Any]:
    result_id = _encode_result_id(
        document_id=str(result.get("document_id") or ""),
        chunk_id=str(result.get("chunk_id") or ""),
    )
    title = _result_title(result)
    verbatim_text = str(result.get("text") or "")
    return {
        "id": result_id,
        "title": title,
        "url": _mcp_url(document_id=str(result.get("document_id") or ""), chunk_id=str(result.get("chunk_id") or "")),
        "text": verbatim_text[:600],
        "verbatim_text": verbatim_text,
        "verbatim": _mcp_verbatim_block(result),
        "metadata": _citation_metadata(result, include_detail=False, metadata_profile=metadata_profile),
    }


def _mcp_fetch_result(result: dict[str, Any], *, metadata_profile: str = "full") -> dict[str, Any]:
    verbatim_text = str(result.get("text") or "")
    return {
        "id": _encode_result_id(
            document_id=str(result.get("document_id") or ""),
            chunk_id=str(result.get("chunk_id") or ""),
        ),
        "title": _result_title(result),
        "text": verbatim_text,
        "verbatim_text": verbatim_text,
        "verbatim": _mcp_verbatim_block(result),
        "url": _mcp_url(document_id=str(result.get("document_id") or ""), chunk_id=str(result.get("chunk_id") or "")),
        "metadata": _citation_metadata(result, metadata_profile=metadata_profile),
    }


def _mcp_verbatim_block(result: dict[str, Any]) -> dict[str, Any]:
    """Expose approved stored chunk text separately from any generated answer."""
    text = str(result.get("text") or "")
    return {
        "text": text,
        "is_verbatim": bool(text),
        "source": "approved_local_regulation_chunk",
        "document_id": str(result.get("document_id") or ""),
        "chunk_id": str(result.get("chunk_id") or ""),
        "article_no": str(result.get("article_no") or ""),
        "article_title": str(result.get("article_title") or ""),
        "regulation_id": str(result.get("regulation_id") or ""),
        "regulation_version": str(result.get("regulation_version") or ""),
        "effective_from": result.get("effective_from"),
        "effective_to": result.get("effective_to"),
        "repealed_at": result.get("repealed_at"),
        "source_page_start": result.get("source_page_start"),
        "source_page_end": result.get("source_page_end"),
        "content_hash": str(result.get("content_hash") or ""),
        "approved_content_hash": str(result.get("approved_content_hash") or ""),
        "display_guidance": "Display this text as the approved regulation evidence; do not rewrite it as if it were a quotation-free summary.",
    }


def _citation_metadata(
    result: dict[str, Any],
    *,
    include_detail: bool = True,
    metadata_profile: str = "full",
) -> dict[str, Any]:
    article_no, article_title = _normalized_article_label(result)
    regulation_title = str(
        result.get("regulation_title")
        or result.get("chapter_title")
        or result.get("document_name")
        or ""
    )
    metadata = {
        "document_id": str(result.get("document_id") or ""),
        "chunk_id": str(result.get("chunk_id") or ""),
        "approval_id": str(result.get("approval_id") or ""),
        "content_hash": str(result.get("content_hash") or ""),
        "approved_content_hash": str(result.get("approved_content_hash") or ""),
        "document_name": str(result.get("document_name") or ""),
        "institution_name": str(result.get("institution_name") or ""),
        "apba_id": str(result.get("apba_id") or ""),
        "source_system": str(result.get("source_system") or ""),
        "source_url": str(result.get("source_url") or ""),
        "source_record_id": str(result.get("source_record_id") or ""),
        "source_file_id": str(result.get("source_file_id") or ""),
        "profile_id": str(result.get("profile_id") or ""),
        "regulation_id": str(result.get("regulation_id") or ""),
        "regulation_version": str(result.get("regulation_version") or ""),
        "regulation_status": str(result.get("regulation_status") or ""),
        "revision_date": result.get("revision_date"),
        "effective_from": result.get("effective_from"),
        "effective_to": result.get("effective_to"),
        "repealed_at": result.get("repealed_at"),
        "supersedes_document_id": str(result.get("supersedes_document_id") or ""),
        "chunk_type": str(result.get("chunk_type") or ""),
        "regulation_title": regulation_title,
        "article_no": article_no,
        "article_title": article_title,
        "direct_article_no": str(result.get("article_no") or ""),
        "direct_article_title": str(result.get("article_title") or ""),
        "article_refs": result.get("article_refs") or [],
        "appendix_refs": result.get("appendix_refs") or [],
        "form_refs": result.get("form_refs") or [],
        "governing_article_no": str(result.get("governing_article_no") or ""),
        "governing_article_title": str(result.get("governing_article_title") or ""),
        "governing_article_chunk_id": str(result.get("governing_article_chunk_id") or ""),
        "governing_article_match_ref": str(result.get("governing_article_match_ref") or ""),
        "source_page_start": result.get("source_page_start"),
        "source_page_end": result.get("source_page_end"),
        "security_level": str(result.get("security_level") or ""),
        "approval_status": str(result.get("approval_status") or ""),
        "approval_worklist_report_sha256": str(result.get("approval_worklist_report_sha256") or ""),
        "approval_review_batch_manifest_path": str(result.get("approval_review_batch_manifest_path") or ""),
        "approval_review_batch_manifest_sha256": str(result.get("approval_review_batch_manifest_sha256") or ""),
        "approval_review_batch_id": str(result.get("approval_review_batch_id") or ""),
        "approval_review_batch_chunk_fingerprint": str(
            result.get("approval_review_batch_chunk_fingerprint") or ""
        ),
        "approval_review_strategy": str(result.get("approval_review_strategy") or ""),
        "answer_profile_version": str(result.get("answer_profile_version") or ""),
        "answer_intents": result.get("answer_intents") or [],
        "source_hwpx_block_types": result.get("source_hwpx_block_types") or [],
        "source_hwpx_parser_review_flags": result.get("source_hwpx_parser_review_flags") or [],
        "source_hwp_extraction_modes": result.get("source_hwp_extraction_modes") or [],
        "source_hwp_native_table_geometry": result.get("source_hwp_native_table_geometry"),
        "table_source": str(result.get("table_source") or ""),
        "table_geometry_source": str(result.get("table_geometry_source") or ""),
        "primary_parser_table_source": str(result.get("primary_parser_table_source") or ""),
        "kordoc_table_parser_status": str(result.get("kordoc_table_parser_status") or ""),
        "kordoc_table_count": result.get("kordoc_table_count"),
        "kordoc_table_promoted": bool(result.get("kordoc_table_promoted")),
        "kordoc_table_promotion_review_required": bool(result.get("kordoc_table_promotion_review_required")),
        "kordoc_table_unmatched_source": bool(result.get("kordoc_table_unmatched_source")),
        "kordoc_table_match_review_required": bool(result.get("kordoc_table_match_review_required")),
        "kordoc_table_match_provisional": bool(result.get("kordoc_table_match_provisional")),
        "parser_uncertainty_source": str(result.get("parser_uncertainty_source") or ""),
        "parser_uncertainty_risk_level": str(result.get("parser_uncertainty_risk_level") or ""),
        "parser_uncertainty_confidence": result.get("parser_uncertainty_confidence"),
        "parser_uncertainty_flags": result.get("parser_uncertainty_flags") or [],
        "parser_uncertainty_recommendation": str(result.get("parser_uncertainty_recommendation") or ""),
    }
    if include_detail:
        metadata.update(
            {
                "reference_edges": result.get("reference_edges") or [],
                "answer_keywords": result.get("answer_keywords") or [],
                "answer_facts": _clean_mcp_answer_facts(result.get("answer_facts") or []),
                "answer_outline": _clean_mcp_answer_outline(result.get("answer_outline") or []),
                "source_hwpx_xml_block_indices": result.get("source_hwpx_xml_block_indices") or [],
                "source_hwpx_table_direct_captions": result.get("source_hwpx_table_direct_captions") or [],
                "source_hwpx_table_image_captions": result.get("source_hwpx_table_image_captions") or [],
                "source_hwpx_table_note_snippets": result.get("source_hwpx_table_note_snippets") or [],
                "source_hwpx_nested_table_text_snippets": result.get("source_hwpx_nested_table_text_snippets") or [],
                "source_hwp_streams": result.get("source_hwp_streams") or [],
                "source_hwp_section_indices": result.get("source_hwp_section_indices") or [],
                "kordoc_table_match": result.get("kordoc_table_match") or {},
                "parser_uncertainty_remediation_hint": str(result.get("parser_uncertainty_remediation_hint") or ""),
            }
        )
    return _metadata_for_profile(metadata, metadata_profile)


def _is_external_metadata_profile(metadata_profile: str) -> bool:
    return _normalize_mcp_metadata_profile(metadata_profile) == _EXTERNAL_MCP_METADATA_PROFILE


def _normalize_mcp_metadata_profile(metadata_profile: str) -> str:
    normalized = str(metadata_profile or "full").strip().lower()
    if not normalized:
        normalized = "full"
    if normalized not in _MCP_METADATA_PROFILES:
        raise ValueError("metadata_profile must be full or chatgpt-data.")
    return normalized


def _metadata_for_profile(metadata: dict[str, Any], metadata_profile: str) -> dict[str, Any]:
    normalized = _normalize_mcp_metadata_profile(metadata_profile)
    if normalized == "full":
        return metadata
    return {key: value for key, value in metadata.items() if key not in _INTERNAL_CITATION_METADATA_KEYS}


def _clean_mcp_answer_facts(facts: Any) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    for fact in facts if isinstance(facts, list) else []:
        if not isinstance(fact, dict):
            continue
        fact_type = str(fact.get("type") or "").strip()
        value = clean_answer_profile_text(str(fact.get("value") or ""))
        sentence = clean_answer_profile_text(str(fact.get("sentence") or ""))
        if fact_type and value:
            cleaned.append({"type": fact_type, "value": value, "sentence": sentence})
    return cleaned


def _clean_mcp_answer_outline(outline: Any) -> list[str]:
    if not isinstance(outline, list):
        return []
    return [cleaned for cleaned in (clean_answer_profile_text(str(value or "")) for value in outline) if cleaned]


def _result_title(result: dict[str, Any]) -> str:
    article_no, article_title = _normalized_article_label(result)
    parts = [
        result.get("regulation_title") or result.get("document_name") or result.get("document_id"),
        article_no,
        article_title,
    ]
    return " ".join(str(part) for part in parts if part)


def _normalized_article_label(result: dict[str, Any]) -> tuple[str, str]:
    article_no = str(result.get("article_no") or "")
    article_title = str(result.get("article_title") or "")
    if article_no and article_title and article_title != "삭제":
        return article_no, article_title
    governing_article_no = str(result.get("governing_article_no") or "")
    governing_article_title = str(result.get("governing_article_title") or "")
    if governing_article_no and governing_article_title and governing_article_title != "삭제":
        return governing_article_no, governing_article_title
    text = str(result.get("text") or "")
    match = re.search(r"(제\d+조(?:의\d+)?)\s*\(([^)\n]{1,80})\)", text)
    if not match:
        return article_no, article_title
    detected_no = match.group(1).strip()
    detected_title = " ".join(match.group(2).split())
    if detected_no and detected_title and detected_title != "삭제":
        return detected_no, detected_title
    return article_no, article_title


def _encode_result_id(*, document_id: str, chunk_id: str) -> str:
    payload = json.dumps({"document_id": document_id, "chunk_id": chunk_id}, ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_result_id(result_id: str) -> dict[str, Any]:
    padding = "=" * (-len(result_id) % 4)
    try:
        payload = base64.urlsafe_b64decode((result_id + padding).encode("ascii")).decode("utf-8")
        decoded = json.loads(payload)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid regulation result id.") from exc
    if not isinstance(decoded, dict):
        raise ValueError("Invalid regulation result id.")
    return decoded


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 3)


def _mcp_url(*, document_id: str, chunk_id: str | None = None) -> str:
    path = f"govreg://documents/{quote(document_id, safe='')}"
    if chunk_id:
        path += f"/chunks/{quote(chunk_id, safe='')}"
    return path
