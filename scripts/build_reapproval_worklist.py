from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings
from app.core.tenant_access import settings_for_tenant, tenant_storage_key
from app.ingestion.vector_adapter import ALLOWED_SECURITY_LEVELS, stable_vector_id
from app.processors.chunker import CHUNKER_VERSION
from app.services.review_workflow_service import review_content_hash
from app.storage.repository import JsonRepository
from scripts.report_metadata import current_repo_commit


VERSION_FIELDS = (
    "parser_version",
    "chunker_version",
    "answer_profile_version",
)

TEMPORAL_METADATA_KEYS = (
    "effective_date",
    "revision_date",
    "valid_from",
    "valid_to",
    "revision_history",
    "revision_history_spans",
    "article_effective_overrides",
    "article_validity_windows",
    "supplementary_identifier_date",
    "supplementary_boilerplate",
    "temporal_metadata_inherited",
    "temporal_metadata_normalized_fields",
    "temporal_metadata_conflict_fields",
    "is_supplementary_provision",
)

APPROVAL_PROVENANCE_VECTOR_FIELDS = (
    "approval_worklist_report_path",
    "approval_worklist_report_sha256",
    "approval_review_batch_manifest_path",
    "approval_review_batch_manifest_sha256",
    "approval_review_batch_id",
    "approval_review_batch_chunk_fingerprint",
    "approval_review_strategy",
)
APPROVAL_VECTOR_ALIGNMENT_FIELDS = (
    "approval_id",
    "approved_content_hash",
    "security_level",
)

CSV_FIELDS = [
    "rank",
    "suggested_action",
    "document_id",
    "document_name",
    "filename",
    "institution_name",
    "apba_id",
    "profile_id",
    "source_system",
    "source_record_id",
    "source_file_id",
    "approved_chunks",
    "reapproval_candidate_chunks",
    "vector_records_for_candidates",
    "vector_missing_for_candidates",
    "vector_stale_for_candidates",
    "approval_provenance_missing_chunks",
    "approval_provenance_missing_fields",
    "missing_approved_content_hash_chunks",
    "source_vector_integrity_failure_count",
    "temporal_metadata_chunks",
    "high_risk_candidate_chunks",
    "temporal_sample_candidate_chunks",
    "low_risk_candidate_chunks",
    "recommended_initial_review_chunks",
    "estimated_review_batches",
    "estimated_review_minutes",
    "estimated_initial_review_minutes",
    "review_strategy",
    "chunker_versions",
    "parser_versions",
    "top_reapproval_reasons",
]

CHUNK_CSV_FIELDS = [
    "document_rank",
    "suggested_action",
    "document_id",
    "document_name",
    "filename",
    "institution_name",
    "apba_id",
    "profile_id",
    "source_system",
    "source_record_id",
    "source_file_id",
    "chunk_id",
    "chunk_type",
    "regulation_title",
    "article_no",
    "source_page_start",
    "approval_id",
    "approved_content_hash_short",
    "review_content_hash",
    "security_level",
    "chunker_version",
    "parser_version",
    "vector_record_present",
    "vector_chunker_version",
    "vector_content_hash_short",
    "approval_provenance_missing_fields",
    "temporal_metadata_present",
    "review_risk_tier",
    "review_strategy",
    "reapproval_reasons",
]

HIGH_RISK_REAPPROVAL_REASONS = {
    "approval_id_missing",
    "approved_content_hash_missing",
    "parser_version_missing",
    "security_level_invalid_or_missing",
    "vector_record_missing",
}


def build_reapproval_worklist(
    *,
    data_dir: str | Path,
    tenant_id: str = "default",
    tenant_storage_isolation: bool | None = None,
    source_system: str | None = None,
    apba_id: str | None = None,
    runtime_version_drift_report: str | Path | None = None,
    review_batch_size: int = 100,
    review_seconds_per_chunk: int = 20,
    low_risk_sample_rate: float = 0.05,
    temporal_sample_rate: float = 0.15,
    min_sample_chunks_per_tier: int = 10,
    sample_limit: int = 10,
    include_chunk_candidates: bool = False,
) -> dict[str, Any]:
    effective_isolation = _tenant_storage_isolation(Path(data_dir), tenant_storage_isolation)
    settings = Settings(data_dir=Path(data_dir), tenant_storage_isolation=effective_isolation)
    effective_settings = settings_for_tenant(settings, tenant_id)
    repository = JsonRepository(effective_settings)
    vector_records = _load_vector_records(effective_settings.data_dir, tenant_id=tenant_id)
    vector_by_key = {_row_key(record): record for record in vector_records if _row_key(record)}
    filters = {
        "source_system": _clean_text(source_system),
        "apba_id": _clean_text(apba_id),
    }
    source_report = _source_report_summary(runtime_version_drift_report)
    source_vector_integrity_failure_count = _source_vector_integrity_failure_count(source_report)

    documents: list[dict[str, Any]] = []
    reason_totals: Counter[str] = Counter()
    version_totals: dict[str, Counter[str]] = {field: Counter() for field in VERSION_FIELDS}
    total_approved_chunks = 0
    total_candidate_chunks = 0
    total_vector_records_for_candidates = 0
    total_vector_missing_for_candidates = 0
    total_vector_stale_for_candidates = 0
    total_approval_provenance_missing = 0
    total_approval_provenance_only = 0
    total_approval_provenance_missing_fields: Counter[str] = Counter()
    total_missing_approved_hash = 0
    total_temporal_metadata_chunks = 0
    total_high_risk_candidates = 0
    total_temporal_sample_candidates = 0
    total_low_risk_candidates = 0
    total_initial_review_chunks = 0
    total_initial_review_minutes = 0
    chunk_candidate_rows: list[dict[str, Any]] = []

    for document in repository.list_documents():
        chunks = repository.get_chunks(document.document_id)
        if not _document_matches_filters(document, chunks, filters):
            continue
        approved_chunks = [chunk for chunk in chunks if _is_approved(chunk)]
        total_approved_chunks += len(approved_chunks)
        candidate_rows = []
        document_reason_totals: Counter[str] = Counter()
        chunker_versions: Counter[str] = Counter()
        parser_versions: Counter[str] = Counter()
        for chunk in approved_chunks:
            metadata = _metadata(chunk)
            for field in VERSION_FIELDS:
                version_totals[field][_metadata_version(metadata, field)] += 1
            chunker_versions[_metadata_version(metadata, "chunker_version")] += 1
            parser_versions[_metadata_version(metadata, "parser_version")] += 1
            reasons = _chunk_reapproval_reasons(chunk, vector_by_key)
            if not reasons:
                continue
            row = _chunk_sample_row(chunk, reasons=reasons, vector_by_key=vector_by_key)
            row.update(
                {
                    "document_name": document.document_name or "",
                    "filename": document.filename,
                    "institution_name": document.institution_name or "",
                    "apba_id": _metadata_value(document, chunks, "apba_id"),
                    "profile_id": _metadata_value(document, chunks, "profile_id"),
                    "source_system": _metadata_value(document, chunks, "source_system"),
                    "source_record_id": document.source_record_id or "",
                    "source_file_id": document.source_file_id or "",
                }
            )
            candidate_rows.append(row)
            document_reason_totals.update(reasons)
        if not candidate_rows:
            continue

        reason_totals.update(document_reason_totals)
        candidate_count = len(candidate_rows)
        vector_records_for_candidates = sum(1 for row in candidate_rows if row["vector_record_present"])
        vector_missing = sum(1 for row in candidate_rows if not row["vector_record_present"])
        vector_stale = sum(1 for row in candidate_rows if row["vector_chunker_version"] != CHUNKER_VERSION)
        approval_provenance_missing_fields: Counter[str] = Counter(
            field for row in candidate_rows for field in row["approval_provenance_missing_fields"]
        )
        approval_provenance_missing = sum(1 for row in candidate_rows if row["approval_provenance_missing_fields"])
        approval_provenance_only = sum(
            1 for row in candidate_rows if _only_approval_provenance_reasons(row["reapproval_reasons"])
        )
        missing_hash = sum(1 for row in candidate_rows if not row["approved_content_hash_short"])
        temporal_count = sum(1 for row in candidate_rows if row["temporal_metadata_present"])
        triage_counts = Counter(str(row["review_risk_tier"]) for row in candidate_rows)
        high_risk_count = triage_counts.get("high", 0)
        temporal_sample_count = triage_counts.get("medium", 0)
        low_risk_count = triage_counts.get("low", 0)
        initial_review_chunks = _recommended_initial_review_chunks(
            high_risk_count=high_risk_count,
            temporal_sample_count=temporal_sample_count,
            low_risk_count=low_risk_count,
            low_risk_sample_rate=low_risk_sample_rate,
            temporal_sample_rate=temporal_sample_rate,
            min_sample_chunks_per_tier=min_sample_chunks_per_tier,
        )
        initial_review_minutes = _estimated_minutes(initial_review_chunks, review_seconds_per_chunk)
        total_candidate_chunks += candidate_count
        total_vector_records_for_candidates += vector_records_for_candidates
        total_vector_missing_for_candidates += vector_missing
        total_vector_stale_for_candidates += vector_stale
        total_approval_provenance_missing += approval_provenance_missing
        total_approval_provenance_only += approval_provenance_only
        total_approval_provenance_missing_fields.update(approval_provenance_missing_fields)
        total_missing_approved_hash += missing_hash
        total_temporal_metadata_chunks += temporal_count
        total_high_risk_candidates += high_risk_count
        total_temporal_sample_candidates += temporal_sample_count
        total_low_risk_candidates += low_risk_count
        total_initial_review_chunks += initial_review_chunks
        total_initial_review_minutes += initial_review_minutes
        chunk_candidate_rows.extend(candidate_rows)
        documents.append(
            {
                "suggested_action": _suggested_action(
                    missing_approved_content_hash_count=missing_hash,
                    vector_missing_count=vector_missing,
                    candidate_count=candidate_count,
                    approval_provenance_only_count=approval_provenance_only,
                    source_vector_integrity_failure_count=source_vector_integrity_failure_count,
                ),
                "document_id": document.document_id,
                "document_name": document.document_name or "",
                "filename": document.filename,
                "institution_name": document.institution_name or "",
                "apba_id": _metadata_value(document, chunks, "apba_id"),
                "profile_id": _metadata_value(document, chunks, "profile_id"),
                "source_system": _metadata_value(document, chunks, "source_system"),
                "source_record_id": document.source_record_id or "",
                "source_file_id": document.source_file_id or "",
                "approved_chunks": len(approved_chunks),
                "reapproval_candidate_chunks": candidate_count,
                "vector_records_for_candidates": vector_records_for_candidates,
                "vector_missing_for_candidates": vector_missing,
                "vector_stale_for_candidates": vector_stale,
                "approval_provenance_missing_chunks": approval_provenance_missing,
                "approval_provenance_only_chunks": approval_provenance_only,
                "approval_provenance_missing_fields": _format_counter(approval_provenance_missing_fields),
                "missing_approved_content_hash_chunks": missing_hash,
                "source_vector_integrity_failure_count": source_vector_integrity_failure_count,
                "temporal_metadata_chunks": temporal_count,
                "high_risk_candidate_chunks": high_risk_count,
                "temporal_sample_candidate_chunks": temporal_sample_count,
                "low_risk_candidate_chunks": low_risk_count,
                "recommended_initial_review_chunks": initial_review_chunks,
                "estimated_review_batches": _ceil_div(candidate_count, review_batch_size),
                "estimated_review_minutes": _estimated_minutes(candidate_count, review_seconds_per_chunk),
                "estimated_initial_review_minutes": initial_review_minutes,
                "review_strategy": _document_review_strategy(
                    high_risk_count=high_risk_count,
                    temporal_sample_count=temporal_sample_count,
                    low_risk_count=low_risk_count,
                ),
                "chunker_versions": _format_counter(chunker_versions),
                "parser_versions": _format_counter(parser_versions),
                "top_reapproval_reasons": _format_counter(document_reason_totals, limit=6),
                "chunk_samples": candidate_rows[:sample_limit],
            }
        )

    action_order = {
        "fix_vector_integrity_before_reapproval": 0,
        "inspect_missing_approval_hash_first": 1,
        "reconcile_vector_gap_then_reapprove": 2,
        "reapprove_and_reindex": 3,
        "reprocess_then_reapprove_and_reindex": 4,
    }
    documents.sort(
        key=lambda row: (
            action_order.get(str(row["suggested_action"]), 99),
            -int(row["missing_approved_content_hash_chunks"]),
            -int(row["vector_missing_for_candidates"]),
            -int(row["reapproval_candidate_chunks"]),
            str(row["apba_id"]),
            str(row["filename"]),
        )
    )
    for index, row in enumerate(documents, start=1):
        row["rank"] = index
        for candidate in chunk_candidate_rows:
            if candidate.get("document_id") == row.get("document_id"):
                candidate["document_rank"] = index
                candidate["suggested_action"] = row.get("suggested_action")

    risk_order = {"high": 0, "medium": 1, "low": 2}
    chunk_candidate_rows.sort(
        key=lambda row: (
            _int(row.get("document_rank")),
            risk_order.get(str(row.get("review_risk_tier") or ""), 99),
            str(row.get("chunk_id") or ""),
        )
    )

    total_review_batches = sum(int(row["estimated_review_batches"]) for row in documents)
    total_review_minutes = sum(int(row["estimated_review_minutes"]) for row in documents)
    report = {
        "report_type": "reapproval_worklist",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "data_dir": str(Path(data_dir)),
        "effective_data_dir": str(effective_settings.data_dir),
        "tenant_id": tenant_id,
        "tenant_storage_isolation": effective_isolation,
        "filters": {key: value for key, value in filters.items() if value},
        "current_chunker_version": CHUNKER_VERSION,
        "review_batch_size": review_batch_size,
        "review_seconds_per_chunk": review_seconds_per_chunk,
        "low_risk_sample_rate": low_risk_sample_rate,
        "temporal_sample_rate": temporal_sample_rate,
        "min_sample_chunks_per_tier": min_sample_chunks_per_tier,
        "document_count": len(documents),
        "total_approved_chunks": total_approved_chunks,
        "reapproval_candidate_chunks": total_candidate_chunks,
        "reapproval_candidate_ratio": _ratio(total_candidate_chunks, total_approved_chunks),
        "vector_record_count": len(vector_records),
        "vector_records_for_candidates": total_vector_records_for_candidates,
        "vector_missing_for_candidates": total_vector_missing_for_candidates,
        "vector_stale_for_candidates": total_vector_stale_for_candidates,
        "approval_provenance_missing_chunks": total_approval_provenance_missing,
        "approval_provenance_only_chunks": total_approval_provenance_only,
        "approval_provenance_missing_field_counts": dict(sorted(total_approval_provenance_missing_fields.items())),
        "missing_approved_content_hash_chunks": total_missing_approved_hash,
        "source_vector_integrity_failure_count": source_vector_integrity_failure_count,
        "temporal_metadata_chunks": total_temporal_metadata_chunks,
        "high_risk_candidate_chunks": total_high_risk_candidates,
        "temporal_sample_candidate_chunks": total_temporal_sample_candidates,
        "low_risk_candidate_chunks": total_low_risk_candidates,
        "recommended_initial_review_chunks": total_initial_review_chunks,
        "estimated_review_batches": total_review_batches,
        "estimated_review_minutes": total_review_minutes,
        "estimated_initial_review_minutes": total_initial_review_minutes,
        "chunk_candidate_export_count": len(chunk_candidate_rows),
        "chunk_candidate_export_fields": list(CHUNK_CSV_FIELDS),
        "initial_review_reduction_ratio": _ratio(
            max(total_candidate_chunks - total_initial_review_chunks, 0),
            total_candidate_chunks,
        ),
        "version_counts": {field: dict(sorted(counter.items())) for field, counter in version_totals.items()},
        "reapproval_reason_totals": dict(reason_totals.most_common(20)),
        "review_triage_counts": {
            "high": total_high_risk_candidates,
            "medium": total_temporal_sample_candidates,
            "low": total_low_risk_candidates,
        },
        "action_counts": dict(sorted(Counter(str(row["suggested_action"]) for row in documents).items())),
        "pre_reapproval_blockers": _pre_reapproval_blockers(source_report),
        "source_runtime_version_drift_report": source_report,
        "documents": documents,
        "safety_note": (
            "This worklist is read-only. It does not reprocess files, approve chunks, or write Vector DB records. "
            "Rows identify approved chunks whose parser/chunker-derived retrieval metadata should be reviewed again "
            "before any reindexing. Low-risk and temporal sample counts are triage guidance only; a human approval "
            "action is still required before indexing refreshed chunks."
        ),
    }
    if include_chunk_candidates:
        report["_chunk_candidate_rows"] = chunk_candidate_rows
    return report


def _tenant_storage_isolation(data_dir: Path, tenant_storage_isolation: bool | None) -> bool:
    if tenant_storage_isolation is not None:
        return tenant_storage_isolation
    return Settings(data_dir=data_dir).tenant_storage_isolation


def _load_vector_records(effective_dir: Path, *, tenant_id: str) -> list[dict[str, Any]]:
    vector_path = effective_dir / "vector_db" / tenant_storage_key(tenant_id) / "approved_vectors.jsonl"
    if not vector_path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in vector_path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _chunk_reapproval_reasons(chunk: Any, vector_by_key: dict[str, dict[str, Any]]) -> list[str]:
    metadata = _metadata(chunk)
    reasons: list[str] = []
    chunker_version = _metadata_version(metadata, "chunker_version")
    if chunker_version != CHUNKER_VERSION:
        reasons.append("chunker_version_stale")
    if not _has_value(metadata.get("parser_version")):
        reasons.append("parser_version_missing")
    if not _has_value(_field_value(chunk, "approval_id")):
        reasons.append("approval_id_missing")
    if not _has_value(_field_value(chunk, "approved_content_hash")):
        reasons.append("approved_content_hash_missing")
    security_level = str(_field_value(chunk, "security_level") or "").strip().lower()
    if security_level not in ALLOWED_SECURITY_LEVELS:
        reasons.append("security_level_invalid_or_missing")

    vector = vector_by_key.get(_chunk_key(chunk))
    if vector is None:
        reasons.append("vector_record_missing")
    else:
        vector_metadata = vector.get("metadata") if isinstance(vector.get("metadata"), dict) else {}
        if str(vector_metadata.get("chunker_version") or "missing") != CHUNKER_VERSION:
            reasons.append("vector_chunker_version_stale")
        for field in VERSION_FIELDS:
            repository_value = metadata.get(field)
            vector_value = vector_metadata.get(field)
            if _has_value(repository_value) and _has_value(vector_value) and repository_value != vector_value:
                reasons.append(f"repository_vector_{field}_mismatch")
        for field in APPROVAL_VECTOR_ALIGNMENT_FIELDS:
            repository_value = _field_value(chunk, field)
            vector_value = vector_metadata.get(field)
            if (
                _has_value(repository_value)
                and _has_value(vector_value)
                and _normalized_alignment_value(field, repository_value)
                != _normalized_alignment_value(field, vector_value)
            ):
                reasons.append(f"repository_vector_{field}_mismatch")
        for field in _approval_provenance_missing_fields(vector_metadata):
            reasons.append(f"approval_provenance_{field}_missing")
    return sorted(dict.fromkeys(reasons))


def _chunk_sample_row(
    chunk: Any,
    *,
    reasons: Sequence[str],
    vector_by_key: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    metadata = _metadata(chunk)
    vector = vector_by_key.get(_chunk_key(chunk))
    vector_metadata = vector.get("metadata") if isinstance(vector, dict) and isinstance(vector.get("metadata"), dict) else {}
    approval_provenance_missing_fields = (
        _approval_provenance_missing_fields(vector_metadata) if vector is not None else []
    )
    temporal_metadata_present = any(_has_value(metadata.get(key)) for key in TEMPORAL_METADATA_KEYS)
    triage = _chunk_review_triage(reasons, temporal_metadata_present=temporal_metadata_present)
    return {
        "document_id": _field_value(chunk, "document_id"),
        "chunk_id": _field_value(chunk, "chunk_id"),
        "chunk_type": _field_value(chunk, "chunk_type") or metadata.get("chunk_type") or "unknown",
        "regulation_title": metadata.get("regulation_title") or metadata.get("document_name") or "",
        "article_no": metadata.get("article_no") or "",
        "source_page_start": _field_value(chunk, "source_page_start") or metadata.get("source_page_start"),
        "approval_id": _field_value(chunk, "approval_id") or metadata.get("approval_id") or "",
        "approved_content_hash_short": _short_hash(_field_value(chunk, "approved_content_hash")),
        "review_content_hash": review_content_hash(chunk),
        "security_level": _field_value(chunk, "security_level") or metadata.get("security_level") or "",
        "chunker_version": metadata.get("chunker_version") or "",
        "parser_version": metadata.get("parser_version") or "",
        "vector_record_present": vector is not None,
        "vector_chunker_version": vector_metadata.get("chunker_version") or "",
        "vector_content_hash_short": _short_hash(vector.get("content_hash") if isinstance(vector, dict) else ""),
        "approval_provenance_missing_fields": approval_provenance_missing_fields,
        "temporal_metadata_present": temporal_metadata_present,
        "review_risk_tier": triage["risk_tier"],
        "review_strategy": triage["review_strategy"],
        "reapproval_reasons": list(reasons),
    }


def _chunk_review_triage(reasons: Sequence[str], *, temporal_metadata_present: bool) -> dict[str, str]:
    reason_set = set(reasons)
    if reason_set & HIGH_RISK_REAPPROVAL_REASONS or any(reason.startswith("repository_vector_") for reason in reason_set):
        return {"risk_tier": "high", "review_strategy": "full_manual_review"}
    if temporal_metadata_present:
        return {"risk_tier": "medium", "review_strategy": "temporal_metadata_sample_then_operator_reapproval"}
    return {"risk_tier": "low", "review_strategy": "version_only_sample_then_operator_reapproval"}


def _source_report_summary(path_value: str | Path | None) -> dict[str, Any] | None:
    if path_value is None:
        return None
    path = Path(path_value)
    summary: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
    }
    if not path.is_file():
        return summary
    data = path.read_bytes()
    summary.update(
        {
            "byte_count": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    )
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        summary["parse_error"] = "invalid_json"
        return summary
    summary.update(
        {
            "report_type": payload.get("report_type"),
            "generated_at": payload.get("generated_at"),
            "passed": payload.get("passed"),
            "warning_count": payload.get("warning_count"),
            "blocker_count": payload.get("blocker_count"),
            "approved_repository_stale_chunker_count": payload.get("approved_repository_stale_chunker_count"),
            "vector_stale_chunker_count": payload.get("vector_stale_chunker_count"),
            "vector_integrity": _vector_integrity_source_summary(payload.get("vector_integrity")),
            "reapproval_scope": payload.get("reapproval_scope"),
            "current_versions": payload.get("current_versions"),
        }
    )
    return summary


def _vector_integrity_source_summary(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    keys = (
        "failure_count",
        "content_hash_mismatch_count",
        "verification_hash_mismatch_count",
        "metadata_missing_required_count",
        "invalid_approval_status_count",
        "invalid_security_level_count",
        "embedded_dimension_mismatch_count",
        "embedded_integrity_failure_count",
        "local_path_leak_count",
    )
    return {key: _int(value.get(key)) for key in keys if key in value}


def _source_vector_integrity_failure_count(source_report: dict[str, Any] | None) -> int:
    if not isinstance(source_report, dict):
        return 0
    vector_integrity = source_report.get("vector_integrity")
    if not isinstance(vector_integrity, dict):
        return 0
    return _int(vector_integrity.get("failure_count"))


def _pre_reapproval_blockers(source_report: dict[str, Any] | None) -> list[dict[str, Any]]:
    failure_count = _source_vector_integrity_failure_count(source_report)
    if failure_count <= 0:
        return []
    return [
        {
            "code": "source-vector-integrity-failure",
            "severity": "blocker",
            "failure_count": failure_count,
            "detail": "Resolve vector integrity failures in the runtime drift audit before reapproval or reindex planning.",
        }
    ]


def _document_matches_filters(document: Any, chunks: Sequence[Any], filters: dict[str, str]) -> bool:
    source_system = _clean_text(filters.get("source_system"))
    apba_id = _clean_text(filters.get("apba_id"))
    if source_system and _metadata_value(document, chunks, "source_system").upper() != source_system.upper():
        return False
    if apba_id and _metadata_value(document, chunks, "apba_id") != apba_id:
        return False
    return True


def _metadata_value(document: Any, chunks: Sequence[Any], key: str) -> str:
    value = _clean_text(getattr(document, key, None))
    if value:
        return value
    for chunk in chunks:
        value = _clean_text(_metadata(chunk).get(key))
        if value:
            return value
    return ""


def _is_approved(chunk: Any) -> bool:
    return str(_field_value(chunk, "approval_status") or _metadata(chunk).get("approval_status") or "").strip().lower() == "approved"


def _chunk_key(chunk: Any) -> str:
    return stable_vector_id(str(_field_value(chunk, "document_id") or ""), str(_field_value(chunk, "chunk_id") or ""))


def _row_key(record: dict[str, Any]) -> str:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    return stable_vector_id(
        str(record.get("document_id") or metadata.get("document_id") or ""),
        str(record.get("chunk_id") or metadata.get("chunk_id") or ""),
    )


def _metadata(chunk: Any) -> dict[str, Any]:
    metadata = getattr(chunk, "metadata", None)
    if isinstance(metadata, dict):
        return metadata
    if isinstance(chunk, dict) and isinstance(chunk.get("metadata"), dict):
        return chunk["metadata"]
    return {}


def _field_value(chunk: Any, field: str) -> Any:
    if isinstance(chunk, dict):
        if field in chunk:
            return chunk.get(field)
        return _metadata(chunk).get(field)
    value = getattr(chunk, field, None)
    if value is not None:
        return value
    return _metadata(chunk).get(field)


def _metadata_version(metadata: dict[str, Any], field: str) -> str:
    return str(metadata.get(field) or "missing")


def _approval_provenance_missing_fields(vector_metadata: dict[str, Any]) -> list[str]:
    return [field for field in APPROVAL_PROVENANCE_VECTOR_FIELDS if not _has_value(vector_metadata.get(field))]


def _only_approval_provenance_reasons(reasons: Sequence[str]) -> bool:
    return bool(reasons) and all(str(reason).startswith("approval_provenance_") for reason in reasons)


def _suggested_action(
    *,
    missing_approved_content_hash_count: int,
    vector_missing_count: int,
    candidate_count: int,
    approval_provenance_only_count: int,
    source_vector_integrity_failure_count: int,
) -> str:
    if source_vector_integrity_failure_count:
        return "fix_vector_integrity_before_reapproval"
    if missing_approved_content_hash_count:
        return "inspect_missing_approval_hash_first"
    if vector_missing_count:
        return "reconcile_vector_gap_then_reapprove"
    if approval_provenance_only_count and approval_provenance_only_count == candidate_count:
        return "reapprove_and_reindex"
    if candidate_count:
        return "reprocess_then_reapprove_and_reindex"
    return "no_reapproval_needed"


def _document_review_strategy(
    *,
    high_risk_count: int,
    temporal_sample_count: int,
    low_risk_count: int,
) -> str:
    if high_risk_count:
        return "full_review_high_risk_then_sample_remaining"
    if temporal_sample_count and low_risk_count:
        return "temporal_and_version_sample_review"
    if temporal_sample_count:
        return "temporal_metadata_sample_review"
    if low_risk_count:
        return "version_only_sample_review"
    return "no_reapproval_needed"


def _recommended_initial_review_chunks(
    *,
    high_risk_count: int,
    temporal_sample_count: int,
    low_risk_count: int,
    low_risk_sample_rate: float,
    temporal_sample_rate: float,
    min_sample_chunks_per_tier: int,
) -> int:
    return (
        high_risk_count
        + _sample_count(temporal_sample_count, rate=temporal_sample_rate, minimum=min_sample_chunks_per_tier)
        + _sample_count(low_risk_count, rate=low_risk_sample_rate, minimum=min_sample_chunks_per_tier)
    )


def _sample_count(count: int, *, rate: float, minimum: int) -> int:
    if count <= 0:
        return 0
    bounded_rate = min(max(rate, 0.0), 1.0)
    target = int(math.ceil(count * bounded_rate))
    return min(count, max(max(minimum, 1), target))


def _estimated_minutes(candidate_count: int, seconds_per_chunk: int) -> int:
    if candidate_count <= 0:
        return 0
    return int(math.ceil(candidate_count * max(seconds_per_chunk, 1) / 60))


def _ceil_div(value: int, denominator: int) -> int:
    if value <= 0:
        return 0
    return int(math.ceil(value / max(denominator, 1)))


def _format_counter(counter: Counter[str], *, limit: int | None = None) -> str:
    items = counter.most_common(limit) if limit else sorted(counter.items())
    return "; ".join(f"{key}={value}" for key, value in items)


def _ratio(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _short_hash(value: Any) -> str:
    text = str(value or "")
    return text[:12] if len(text) > 12 else text


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def _normalized_alignment_value(field: str, value: Any) -> str:
    text = _clean_text(value)
    if field == "security_level":
        return text.lower()
    return text


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_chunk_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CHUNK_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_cell(row.get(field)) for field in CHUNK_CSV_FIELDS})


def write_chunk_json(path: Path, report: dict[str, Any], rows: Sequence[dict[str, Any]]) -> None:
    payload = {
        "report_type": "reapproval_worklist_chunk_candidates",
        "generated_at": report.get("generated_at"),
        "repo_commit": report.get("repo_commit"),
        "source_report_type": report.get("report_type"),
        "data_dir": report.get("data_dir"),
        "effective_data_dir": report.get("effective_data_dir"),
        "tenant_id": report.get("tenant_id"),
        "tenant_storage_isolation": report.get("tenant_storage_isolation"),
        "filters": report.get("filters") or {},
        "current_chunker_version": report.get("current_chunker_version"),
        "candidate_count": len(rows),
        "fields": list(CHUNK_CSV_FIELDS),
        "reapproval_candidate_chunks": report.get("reapproval_candidate_chunks"),
        "approval_provenance_missing_chunks": report.get("approval_provenance_missing_chunks"),
        "approval_provenance_only_chunks": report.get("approval_provenance_only_chunks"),
        "approval_provenance_missing_field_counts": report.get("approval_provenance_missing_field_counts") or {},
        "review_triage_counts": report.get("review_triage_counts") or {},
        "action_counts": report.get("action_counts") or {},
        "candidates": [{field: row.get(field) for field in CHUNK_CSV_FIELDS} for row in rows],
        "safety_note": (
            "This file is a read-only export of reapproval candidate chunks. "
            "It does not reprocess files, approve chunks, or write Vector DB records."
        ),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _csv_cell(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return "; ".join(f"{key}={value[key]}" for key in sorted(value))
    if isinstance(value, (list, tuple, set)):
        return "; ".join(str(item) for item in value)
    return "" if value is None else value


def write_markdown(report: dict[str, Any], path: Path) -> None:
    source_report = report.get("source_runtime_version_drift_report") or {}
    lines = [
        "# Reapproval Worklist",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Data dir: `{report.get('data_dir')}`",
        f"- Effective data dir: `{report.get('effective_data_dir')}`",
        f"- Tenant: `{report.get('tenant_id')}`",
        f"- Filters: `{report.get('filters') or {}}`",
        f"- Current chunker version: `{report.get('current_chunker_version')}`",
        f"- Documents requiring action: `{report.get('document_count')}`",
        f"- Approved chunks: `{report.get('total_approved_chunks')}`",
        f"- Reapproval candidate chunks: `{report.get('reapproval_candidate_chunks')}` ({report.get('reapproval_candidate_ratio')})",
        f"- Vector records for candidates: `{report.get('vector_records_for_candidates')}`",
        f"- Vector gaps for candidates: `{report.get('vector_missing_for_candidates')}`",
        f"- Approval provenance gaps for candidates: `{report.get('approval_provenance_missing_chunks')}`",
        f"- Approval provenance missing fields: `{report.get('approval_provenance_missing_field_counts') or {}}`",
        f"- Source vector integrity failures: `{report.get('source_vector_integrity_failure_count')}`",
        f"- Missing approved content hashes: `{report.get('missing_approved_content_hash_chunks')}`",
        f"- Temporal metadata candidate chunks: `{report.get('temporal_metadata_chunks')}`",
        f"- High-risk / temporal-sample / low-risk chunks: `{report.get('high_risk_candidate_chunks')}` / `{report.get('temporal_sample_candidate_chunks')}` / `{report.get('low_risk_candidate_chunks')}`",
        f"- Recommended initial review chunks: `{report.get('recommended_initial_review_chunks')}`",
        f"- Estimated review batches: `{report.get('estimated_review_batches')}`",
        f"- Estimated review minutes: `{report.get('estimated_review_minutes')}`",
        f"- Estimated initial review minutes: `{report.get('estimated_initial_review_minutes')}`",
        f"- Initial review reduction ratio: `{report.get('initial_review_reduction_ratio')}`",
        f"- Runtime drift source: `{source_report.get('path') or ''}`",
        f"- Runtime drift source sha256: `{source_report.get('sha256') or ''}`",
        "",
        f"Safety: {report.get('safety_note')}",
        "",
        "## Top Documents",
        "",
        "| Rank | Action | Strategy | Document | APBA | Candidates | Provenance gaps | High | Temporal sample | Low | Initial review | Full minutes | Initial minutes | Reasons |",
        "| ---: | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in (report.get("documents") or [])[:75]:
        name = row.get("document_name") or row.get("filename") or row.get("document_id")
        lines.append(
            "| {rank} | {action} | {strategy} | {document} | {apba} | {candidates} | {provenance} | {high} | {temporal} | {low} | {initial} | {minutes} | {initial_minutes} | {reasons} |".format(
                rank=row.get("rank"),
                action=_md_cell(_reapproval_action_label(row.get("suggested_action"))),
                strategy=_md_cell(row.get("review_strategy")),
                document=_md_cell(name),
                apba=_md_cell(row.get("apba_id")),
                candidates=row.get("reapproval_candidate_chunks"),
                provenance=row.get("approval_provenance_missing_chunks"),
                high=row.get("high_risk_candidate_chunks"),
                temporal=row.get("temporal_sample_candidate_chunks"),
                low=row.get("low_risk_candidate_chunks"),
                initial=row.get("recommended_initial_review_chunks"),
                minutes=row.get("estimated_review_minutes"),
                initial_minutes=row.get("estimated_initial_review_minutes"),
                reasons=_md_cell(row.get("top_reapproval_reasons")),
            )
        )
    lines.extend(["", "## Reapproval Reasons", "", "| Reason | Count |", "| --- | ---: |"])
    for reason, count in (report.get("reapproval_reason_totals") or {}).items():
        lines.append(f"| {_md_cell(reason)} | {count} |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _reapproval_action_label(value: Any) -> str:
    labels = {
        "fix_vector_integrity_before_reapproval": "fix vector integrity before human reapproval",
        "inspect_missing_approval_hash_first": "inspect missing approval hash before reapproval",
        "reconcile_vector_gap_then_reapprove": "reconcile vector gap before reapproval",
        "reprocess_then_reapprove_and_reindex": "operator reprocess, reapprove, then reindex",
        "reapprove_and_reindex": "operator reapprove, then reindex",
        "no_reapproval_needed": "no reapproval action",
    }
    return labels.get(str(value or ""), str(value or ""))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a read-only reapproval worklist for approved chunks with runtime version drift.")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--tenant-id", default="default")
    storage = parser.add_mutually_exclusive_group()
    storage.add_argument("--tenant-storage-isolation", action="store_true")
    storage.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--source-system")
    parser.add_argument("--apba-id")
    parser.add_argument("--runtime-version-drift-report", type=Path)
    parser.add_argument("--review-batch-size", type=int, default=100)
    parser.add_argument("--review-seconds-per-chunk", type=int, default=20)
    parser.add_argument("--low-risk-sample-rate", type=float, default=0.05)
    parser.add_argument("--temporal-sample-rate", type=float, default=0.15)
    parser.add_argument("--min-sample-chunks-per-tier", type=int, default=10)
    parser.add_argument("--sample-limit", type=int, default=10)
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-csv", type=Path)
    parser.add_argument("--out-chunks-csv", type=Path)
    parser.add_argument("--out-chunks-json", type=Path)
    parser.add_argument("--out-md", type=Path)
    return parser


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    args = _build_parser().parse_args(argv)
    tenant_storage_isolation = None
    if args.tenant_storage_isolation:
        tenant_storage_isolation = True
    if args.flat_storage:
        tenant_storage_isolation = False
    report = build_reapproval_worklist(
        data_dir=args.data_dir,
        tenant_id=args.tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
        source_system=args.source_system,
        apba_id=args.apba_id,
        runtime_version_drift_report=args.runtime_version_drift_report,
        review_batch_size=args.review_batch_size,
        review_seconds_per_chunk=args.review_seconds_per_chunk,
        low_risk_sample_rate=args.low_risk_sample_rate,
        temporal_sample_rate=args.temporal_sample_rate,
        min_sample_chunks_per_tier=args.min_sample_chunks_per_tier,
        sample_limit=args.sample_limit,
        include_chunk_candidates=bool(args.out_chunks_csv or args.out_chunks_json),
    )
    chunk_candidate_rows = report.pop("_chunk_candidate_rows", [])
    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.out_csv:
        write_csv(args.out_csv, report["documents"])
    if args.out_chunks_csv:
        write_chunk_csv(args.out_chunks_csv, chunk_candidate_rows)
    if args.out_chunks_json:
        write_chunk_json(args.out_chunks_json, report, chunk_candidate_rows)
    if args.out_md:
        write_markdown(report, args.out_md)
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stdout or sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
