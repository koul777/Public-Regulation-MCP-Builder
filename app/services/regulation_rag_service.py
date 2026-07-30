from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
import importlib
import re
import time
from collections.abc import Iterable
from typing import Any

from app.services.regulation_catalog_service import (
    filter_to_latest_active_versions,
    read_regulation_metadata,
)
from app.services import regulation_rag_runtime as _runtime


# Step-1 service facade for MCP consumers.
# This module preserves current behavior while removing the direct MCP import
# dependency on the FastAPI route module. Subsequent refactors can move logic
# here incrementally without forcing large MCP-side edits.
_REGULATION_LIFECYCLE_FIELDS = (
    "regulation_id",
    "regulation_version",
    "regulation_status",
    "effective_from",
    "effective_to",
    "repealed_at",
)

_ROUTES_RAG_MODULE: Any | None = None
RegulationQuery = _runtime.RegulationQuery
ROLE_SECURITY_LEVELS = _runtime.ROLE_SECURITY_LEVELS


def _load_routes_rag():
    global _ROUTES_RAG_MODULE
    if _ROUTES_RAG_MODULE is None:
        _ROUTES_RAG_MODULE = importlib.import_module("app.api.routes_rag")
    return _ROUTES_RAG_MODULE


def __getattr__(name: str) -> Any:
    if name.startswith("__"):
        raise AttributeError(name)
    return getattr(_load_routes_rag(), name)


def filter_latest_active_records(
    records: Iterable[dict],
    *,
    as_of: str | None = None,
) -> list[dict]:
    """Keep current catalog versions while retaining approved legacy evidence.

    Approval and tenant visibility are enforced before this compatibility
    filter.  Records created before lifecycle metadata was introduced may not
    have enough identity/date fields to select a latest version; dropping
    those approved records would make existing indexed data disappear.  Keep
    them visible until catalog metadata is backfilled, while still applying
    latest-version selection to complete regulation groups.
    """
    return list(
        filter_to_latest_active_versions(
            list(records),
            as_of=as_of,
            include_legacy=True,
        )
    )


def search_rag_records(request, auth, settings):
    validate_query_security_scope(query=request, auth=auth)
    runtime_request = (
        _query_with_runtime_as_of_date(request)
        if isinstance(request, RegulationQuery)
        else request
    )
    return _load_routes_rag().search_rag_records(runtime_request, auth, settings)


def to_rag_search_request(query: RegulationQuery) -> Any:
    routes_rag = _load_routes_rag()
    return routes_rag.RagSearchRequest(
        query=query.query,
        top_k=query.top_k,
        security_levels=query.security_levels,
        department_ids=query.department_ids,
        document_id=query.document_id,
        profile_id=query.profile_id,
        as_of_date=_query_as_of_date(query),
    )


def search_records(*, query: RegulationQuery, auth, settings):
    validate_query_security_scope(query=query, auth=auth)
    routes_rag = _load_routes_rag()
    request = to_rag_search_request(query)
    total_started_at = time.perf_counter()
    timing_ms: dict[str, float] = {}
    routes_rag._validate_query_policy(request.query)
    requested_department_ids_value = requested_department_ids(request, auth)
    repository = routes_rag.JsonRepository(settings)
    repository_cache_obj = repository_cache(repository)

    step_started_at = time.perf_counter()
    records = load_local_vector_records(settings, auth)
    timing_ms["load_vector_records_elapsed_ms"] = routes_rag._perf_elapsed_ms(step_started_at)
    step_started_at = time.perf_counter()
    approval_snapshot = approval_snapshot_for_records(
        repository,
        records,
        auth,
        enabled=True,
    )
    timing_ms["approval_snapshot_elapsed_ms"] = routes_rag._perf_elapsed_ms(step_started_at)
    step_started_at = time.perf_counter()
    visible_records = routes_rag.load_visible_records(
        request=request,
        auth=auth,
        settings=settings,
        repository=repository,
        repository_cache=repository_cache_obj,
        records=records,
        approval_snapshot=approval_snapshot,
        requested_department_ids=requested_department_ids_value,
    )
    lifecycle_as_of = _normalized_lifecycle_as_of(request.as_of_date)
    lifecycle_complete = sum(
        1
        for record in visible_records
        if _has_complete_lifecycle_metadata(record)
    )
    timing_ms["visibility_filter_elapsed_ms"] = routes_rag._perf_elapsed_ms(step_started_at)
    step_started_at = time.perf_counter()
    scored, retrieval = score_records(
        request.query,
        visible_records,
        settings=settings,
        auth=auth,
        all_records=records,
    )
    timing_ms["scoring_elapsed_ms"] = routes_rag._perf_elapsed_ms(step_started_at)
    step_started_at = time.perf_counter()
    results = [
        public_search_result(record, score, related_records=visible_records)
        for score, record in scored[: request.top_k]
    ]
    timing_ms["public_results_elapsed_ms"] = routes_rag._perf_elapsed_ms(step_started_at)
    timing_ms["total_before_trace_write_elapsed_ms"] = routes_rag._perf_elapsed_ms(total_started_at)
    trace = routes_rag._rag_trace(
        action="search",
        request=request,
        auth=auth,
        results=results,
        extra={
            "candidate_count": len(records),
            "visible_count": len(visible_records),
            "lifecycle_selection": {
                "mode": "latest_active_version_per_regulation",
                "as_of_date": lifecycle_as_of,
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
    if settings.rag_trace_enabled:
        repository.append_rag_trace(trace)
    timing_ms["trace_write_elapsed_ms"] = routes_rag._perf_elapsed_ms(step_started_at)
    timing_ms["total_elapsed_ms"] = routes_rag._perf_elapsed_ms(total_started_at)
    return results, trace


def validate_query_security_scope(*, query: RegulationQuery, auth) -> None:
    _runtime.validate_query_policy(query.query)
    _runtime.validate_security_scope(query, auth)
    _runtime.requested_department_ids(query, auth)


def get_visible_records(
    *,
    query: RegulationQuery,
    auth,
    settings,
    repository,
    use_cached_approval_snapshot: bool = True,
    latest_only: bool = True,
):
    validate_query_security_scope(query=query, auth=auth)
    runtime_query = _query_with_runtime_as_of_date(query)
    repository_cache_obj = repository_cache(repository)
    requested_department_ids_value = requested_department_ids(runtime_query, auth)
    records = load_local_vector_records(settings, auth)
    approval_snapshot = approval_snapshot_for_records(
        repository,
        records,
        auth,
        enabled=use_cached_approval_snapshot,
    )
    visible_records = _runtime.load_visible_records(
        request=runtime_query,
        auth=auth,
        settings=settings,
        repository=repository,
        repository_cache=repository_cache_obj,
        records=records,
        approval_snapshot=approval_snapshot,
        requested_department_ids=requested_department_ids_value,
        latest_only=latest_only,
    )
    if not latest_only:
        return visible_records
    return visible_records


def get_visible_record_by_chunk(
    *,
    query: RegulationQuery,
    auth,
    settings,
    repository,
    candidate: dict | None,
):
    validate_query_security_scope(query=query, auth=auth)
    runtime_query = _query_with_runtime_as_of_date(query)
    if candidate is None:
        return None
    requested_department_ids_value = requested_department_ids(runtime_query, auth)
    repository_cache_obj = repository_cache(repository)
    approval_snapshot = approval_snapshot_for_records(
        repository,
        [candidate],
        auth,
        enabled=True,
    )
    if not is_record_visible(
        candidate,
        request=runtime_query,
        auth=auth,
        repository=repository,
        repository_cache=repository_cache_obj,
        approval_snapshot=approval_snapshot,
        requested_department_ids=requested_department_ids_value,
    ):
        return None
    return candidate


def filter_latest_valid_regulation_records(
    records: list[dict],
    *,
    as_of: date | datetime | str | None = None,
) -> list[dict]:
    """Keep complete, approved, effective latest regulation versions.

    Missing lifecycle fields and invalid non-null dates are excluded rather
    than inferred as the latest version. None is valid for open-ended
    effective_to and repealed_at. Invalid explicit as_of yields no records.
    """
    reference_date = _lifecycle_reference_date(as_of)
    if reference_date is None:
        return []

    latest_by_regulation: dict[str, tuple[tuple, tuple[str, str]]] = {}
    normalized_records: list[
        tuple[dict, tuple[str, str, str, date, date | None, date | None]]
    ] = []
    for record in records:
        normalized = _normalized_lifecycle_metadata(record, reference_date)
        if normalized is None:
            continue
        normalized_records.append((record, normalized))
        regulation_id, version, _status, effective_from, effective_to, _repealed_at = normalized
        metadata = record.get("metadata") or {}
        document_id = str(record.get("document_id") or metadata.get("document_id") or "")
        candidate_key = (
            effective_from,
            _regulation_version_sort_key(version),
            effective_to or date.min,
            document_id.casefold(),
        )
        identity = (version.casefold(), document_id.casefold())
        current = latest_by_regulation.get(regulation_id.casefold())
        if current is None or candidate_key > current[0]:
            latest_by_regulation[regulation_id.casefold()] = (candidate_key, identity)

    return [
        record
        for record, normalized in normalized_records
        if (
            normalized[1].casefold(),
            str(
                record.get("document_id")
                or (record.get("metadata") or {}).get("document_id")
                or ""
            ).casefold(),
        )
        == latest_by_regulation[normalized[0].casefold()][1]
    ]


def _has_complete_lifecycle_metadata(record: dict) -> bool:
    metadata = read_regulation_metadata(record)
    return bool(metadata.regulation_id and metadata.version and metadata.effective_from)


def _normalized_lifecycle_as_of(value: date | datetime | str | None) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError:
            return "invalid"
    return date.today().isoformat()


def _query_as_of_date(query: RegulationQuery) -> str | None:
    """Return the runtime lifecycle date with explicit as_of_date precedence."""
    value = query.as_of_date if query.as_of_date is not None else query.as_of
    if value is None:
        return None
    parsed = _parse_lifecycle_date(value)
    if parsed is not None:
        return parsed.isoformat()
    if isinstance(value, str):
        return value.strip() or None
    return str(value)


def _query_with_runtime_as_of_date(query: RegulationQuery) -> RegulationQuery:
    as_of_date = _query_as_of_date(query)
    if query.as_of_date == as_of_date:
        return query
    return replace(query, as_of_date=as_of_date)


def _approval_visible_records(
    records: list[dict],
    *,
    request,
    auth,
    repository,
    repository_cache,
    requested_department_ids_value,
    use_cached_approval_snapshot: bool = True,
) -> list[dict]:
    approval_snapshot = approval_snapshot_for_records(
        repository,
        records,
        auth,
        enabled=use_cached_approval_snapshot,
    )
    return [
        record
        for record in records
        if is_record_visible(
            record,
            request=request,
            auth=auth,
            repository=repository,
            repository_cache=repository_cache,
            approval_snapshot=approval_snapshot,
            requested_department_ids=requested_department_ids_value,
        )
    ]


def _same_vector_record(record: dict, records: list[dict]) -> bool:
    metadata = record.get("metadata") or {}
    identity = (
        str(record.get("document_id") or ""),
        str(record.get("chunk_id") or metadata.get("chunk_id") or ""),
        str(record.get("content_hash") or ""),
    )
    return any(
        identity
        == (
            str(candidate.get("document_id") or ""),
            str(
                candidate.get("chunk_id")
                or (candidate.get("metadata") or {}).get("chunk_id")
                or ""
            ),
            str(candidate.get("content_hash") or ""),
        )
        for candidate in records
    )


def _normalized_lifecycle_metadata(
    record: dict,
    reference_date: date,
) -> tuple[str, str, str, date, date | None, date | None] | None:
    metadata = read_regulation_metadata(record)
    regulation_id = str(metadata.regulation_id or "").strip()
    version = str(metadata.version or "").strip()
    status = str(metadata.status or "").strip().casefold()
    effective_from = metadata.effective_from
    effective_to = metadata.effective_to
    repealed_at = metadata.repealed_at
    if not regulation_id or not version or status not in {"approved", "superseded"} or effective_from is None:
        return None
    if status == "superseded" and effective_to is None:
        return None
    if effective_from > reference_date:
        return None
    if effective_to is not None and reference_date > effective_to:
        return None
    if repealed_at is not None and reference_date >= repealed_at:
        return None
    return regulation_id, version, status, effective_from, effective_to, repealed_at


def _lifecycle_reference_date(value: date | datetime | str | None) -> date | None:
    if value is None:
        return date.today()
    return _parse_lifecycle_date(value)


def _parse_lifecycle_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _regulation_version_sort_key(version: str) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, int(token)) if token.isdigit() else (1, token)
        for token in re.findall(r"\d+|[a-z]+", version.casefold())
    )


def repository_cache(repository):
    return _runtime.RagRequestRepositoryCache(repository)


def repository_document(repository_cache_obj, document_id: str):
    return repository_cache_obj.get_document(document_id)


def approval_snapshot_for_records(repository, records, auth, *, enabled: bool = True):
    if not enabled:
        return None
    return load_cached_approval_snapshot(repository, records, auth)


def is_record_visible(*args, **kwargs):
    return record_visible_to_request(*args, **kwargs)


def local_vector_path(*args, **kwargs):
    return _runtime.local_vector_path(*args, **kwargs)


def local_vector_signature(*, settings, auth):
    return path_signature(local_vector_path(settings, auth))


def bm25_index_path(*, settings, auth):
    return _runtime.bm25_index_path(settings, auth)


def path_signature(*args, **kwargs):
    return _runtime.path_signature(*args, **kwargs)


def load_local_vector_records(*args, **kwargs):
    return _runtime.load_local_vector_records(*args, **kwargs)


def load_local_vector_record_by_chunk(*args, **kwargs):
    return _runtime.load_local_vector_record_by_chunk(*args, **kwargs)


def load_cached_approval_snapshot(*args, **kwargs):
    return _runtime.load_cached_approval_snapshot(*args, **kwargs)


def load_cached_runtime_approval_snapshot(*args, **kwargs):
    return _runtime.load_cached_runtime_approval_snapshot(*args, **kwargs)


def runtime_approval_snapshot_identity(*args, **kwargs):
    return _runtime.runtime_approval_snapshot_identity(*args, **kwargs)


def approval_snapshot_signature(*args, **kwargs):
    return _runtime.approval_snapshot_signature(*args, **kwargs)


def load_cached_bm25_index(*args, **kwargs):
    return _runtime.load_cached_bm25_index(*args, **kwargs)


def score_records(*args, **kwargs):
    return _load_routes_rag()._score_records(*args, **kwargs)


def public_search_result(*args, **kwargs):
    return _runtime.public_search_result(*args, **kwargs)


def current_repository_chunk(*args, **kwargs):
    return _runtime.current_repository_chunk(*args, **kwargs)


def expected_vector_record_for_chunk(*args, **kwargs):
    return _runtime.expected_vector_record_for_chunk(*args, **kwargs)


def department_acl_set(*args, **kwargs):
    return _runtime.department_acl_set(*args, **kwargs)


def requested_department_ids(*args, **kwargs):
    return _runtime.requested_department_ids(*args, **kwargs)


def record_visible_to_request(*args, **kwargs):
    return _runtime.record_visible_to_request(*args, **kwargs)


def validate_security_scope(*args, **kwargs):
    return _runtime.validate_security_scope(*args, **kwargs)
