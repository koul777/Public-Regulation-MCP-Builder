from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import re
import time
from collections.abc import Iterable

from app.api import routes_rag
from app.services.regulation_catalog_service import (
    filter_to_latest_active_versions,
    read_regulation_metadata,
)


# Step-1 service facade for MCP consumers.
# This module preserves current behavior while removing the direct MCP import
# dependency on the FastAPI route module. Subsequent refactors can move logic
# here incrementally without forcing large MCP-side edits.

RagSearchRequest = routes_rag.RagSearchRequest
_RagRequestRepositoryCache = routes_rag._RagRequestRepositoryCache
_JsonRepository = routes_rag.JsonRepository
default_bm25_index_path = routes_rag.default_bm25_index_path
_local_vector_path = routes_rag._local_vector_path
_path_signature = routes_rag._path_signature
_load_local_vector_records = routes_rag._load_local_vector_records
_load_local_vector_record_by_chunk = routes_rag._load_local_vector_record_by_chunk
_load_cached_approval_snapshot = routes_rag._load_cached_approval_snapshot
_load_cached_bm25_index = routes_rag._load_cached_bm25_index
_score_records = routes_rag._score_records
_public_search_result = routes_rag._public_search_result
_current_repository_chunk = routes_rag._current_repository_chunk
_expected_vector_record_for_chunk = routes_rag._expected_vector_record_for_chunk
_department_acl_set = routes_rag._department_acl_set
_requested_department_ids = routes_rag._requested_department_ids
_record_visible_to_request = routes_rag._record_visible_to_request
_validate_security_scope = routes_rag._validate_security_scope
_validate_query_policy = routes_rag._validate_query_policy
_rag_trace = routes_rag._rag_trace
_perf_elapsed_ms = routes_rag._perf_elapsed_ms
_REGULATION_LIFECYCLE_FIELDS = (
    "regulation_id",
    "regulation_version",
    "regulation_status",
    "effective_from",
    "effective_to",
    "repealed_at",
)
ROLE_SECURITY_LEVELS = routes_rag.ROLE_SECURITY_LEVELS



@dataclass(frozen=True)
class RegulationQuery:
    query: str
    top_k: int = 5
    security_levels: list[str] | None = None
    department_ids: list[str] = field(default_factory=list)
    document_id: str | None = None
    profile_id: str | None = None
    as_of: date | datetime | str | None = None
    as_of_date: str | None = None


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


def search_rag_records(*args, **kwargs):
    return routes_rag.search_rag_records(*args, **kwargs)


def to_rag_search_request(query: RegulationQuery) -> routes_rag.RagSearchRequest:
    return routes_rag.RagSearchRequest(
        query=query.query,
        top_k=query.top_k,
        security_levels=query.security_levels,
        department_ids=query.department_ids,
        document_id=query.document_id,
        profile_id=query.profile_id,
        as_of_date=query.as_of_date,
    )


def search_records(*, query: RegulationQuery, auth, settings):
    request = to_rag_search_request(query)
    total_started_at = time.perf_counter()
    timing_ms: dict[str, float] = {}
    _validate_query_policy(request.query)
    requested_department_ids_value = requested_department_ids(request, auth)
    repository = _JsonRepository(settings)
    repository_cache_obj = repository_cache(repository)

    step_started_at = time.perf_counter()
    records = load_local_vector_records(settings, auth)
    timing_ms["load_vector_records_elapsed_ms"] = _perf_elapsed_ms(step_started_at)
    step_started_at = time.perf_counter()
    approval_snapshot = approval_snapshot_for_records(
        repository,
        records,
        auth,
        enabled=True,
    )
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
    lifecycle_as_of = _normalized_lifecycle_as_of(query.as_of if query.as_of is not None else query.as_of_date)
    lifecycle_complete = sum(
        1
        for record in visible_records
        if _has_complete_lifecycle_metadata(record)
    )
    timing_ms["visibility_filter_elapsed_ms"] = _perf_elapsed_ms(step_started_at)
    step_started_at = time.perf_counter()
    scored, retrieval = score_records(
        request.query,
        visible_records,
        settings=settings,
        auth=auth,
        all_records=records,
    )
    timing_ms["scoring_elapsed_ms"] = _perf_elapsed_ms(step_started_at)
    step_started_at = time.perf_counter()
    results = [
        public_search_result(record, score, related_records=visible_records)
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
    repository.append_rag_trace(trace)
    timing_ms["trace_write_elapsed_ms"] = _perf_elapsed_ms(step_started_at)
    timing_ms["total_elapsed_ms"] = _perf_elapsed_ms(total_started_at)
    return results, trace
def validate_query_security_scope(*, query: RegulationQuery, auth) -> None:
    request = to_rag_search_request(query)
    return routes_rag._validate_security_scope(request, auth)


def get_visible_records(
    *,
    query: RegulationQuery,
    auth,
    settings,
    repository,
    use_cached_approval_snapshot: bool = True,
    latest_only: bool = True,
):
    request = to_rag_search_request(query)
    repository_cache_obj = repository_cache(repository)
    requested_department_ids_value = requested_department_ids(request, auth)
    records = load_local_vector_records(settings, auth)
    approval_snapshot = approval_snapshot_for_records(
        repository,
        records,
        auth,
        enabled=use_cached_approval_snapshot,
    )
    visible_records = routes_rag.load_visible_records(
        request=request,
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
    if candidate is None:
        return None
    requested_department_ids_value = requested_department_ids(query, auth)
    repository_cache_obj = repository_cache(repository)
    approval_snapshot = approval_snapshot_for_records(
        repository,
        [candidate],
        auth,
        enabled=True,
    )
    if not is_record_visible(
        candidate,
        request=query,
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
    return routes_rag._RagRequestRepositoryCache(repository)


def repository_document(repository_cache_obj, document_id: str):
    return repository_cache_obj.get_document(document_id)


def approval_snapshot_for_records(repository, records, auth, *, enabled: bool = True):
    if not enabled:
        return None
    return load_cached_approval_snapshot(repository, records, auth)


def is_record_visible(*args, **kwargs):
    return record_visible_to_request(*args, **kwargs)


def local_vector_path(*args, **kwargs):
    return routes_rag._local_vector_path(*args, **kwargs)


def local_vector_signature(*, settings, auth):
    return path_signature(local_vector_path(settings, auth))


def bm25_index_path(*, settings, auth):
    return default_bm25_index_path(local_vector_path(settings, auth))


def path_signature(*args, **kwargs):
    return routes_rag._path_signature(*args, **kwargs)


def load_local_vector_records(*args, **kwargs):
    return routes_rag._load_local_vector_records(*args, **kwargs)


def load_local_vector_record_by_chunk(*args, **kwargs):
    return routes_rag._load_local_vector_record_by_chunk(*args, **kwargs)


def load_cached_approval_snapshot(*args, **kwargs):
    return routes_rag._load_cached_approval_snapshot(*args, **kwargs)


def load_cached_bm25_index(*args, **kwargs):
    return routes_rag._load_cached_bm25_index(*args, **kwargs)


def score_records(*args, **kwargs):
    return routes_rag._score_records(*args, **kwargs)


def public_search_result(*args, **kwargs):
    return routes_rag._public_search_result(*args, **kwargs)


def current_repository_chunk(*args, **kwargs):
    return routes_rag._current_repository_chunk(*args, **kwargs)


def expected_vector_record_for_chunk(*args, **kwargs):
    return routes_rag._expected_vector_record_for_chunk(*args, **kwargs)


def department_acl_set(*args, **kwargs):
    return routes_rag._department_acl_set(*args, **kwargs)


def requested_department_ids(*args, **kwargs):
    return routes_rag._requested_department_ids(*args, **kwargs)


def record_visible_to_request(*args, **kwargs):
    return routes_rag._record_visible_to_request(*args, **kwargs)


def validate_security_scope(*args, **kwargs):
    return routes_rag._validate_security_scope(*args, **kwargs)
