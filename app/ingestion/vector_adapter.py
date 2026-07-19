from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable


VECTOR_RECORD_SCHEMA_VERSION = "reg-rag-vector-record-v1"
VECTOR_RECORD_VERIFICATION_VERSION = "reg-rag-vector-verification-v1"
APPROVED_CHUNK_STATUS = "approved"
ALLOWED_SECURITY_LEVELS = {"public", "internal", "sensitive", "confidential"}
APPROVAL_PROVENANCE_METADATA_FIELDS = (
    "approval_worklist_report_path",
    "approval_worklist_report_sha256",
    "approval_review_batch_manifest_path",
    "approval_review_batch_manifest_sha256",
    "approval_review_batch_id",
    "approval_review_batch_chunk_fingerprint",
    "approval_review_strategy",
)
APPROVAL_PROVENANCE_SHA256_FIELDS = (
    "approval_worklist_report_sha256",
    "approval_review_batch_manifest_sha256",
    "approval_review_batch_chunk_fingerprint",
)

VECTOR_METADATA_FIELDS = (
    "chunk_id",
    "document_id",
    "tenant_id",
    "document_name",
    "source_file",
    "source_page_start",
    "source_page_end",
    "institution_name",
    "apba_id",
    "source_system",
    "source_url",
    "source_record_id",
    "source_file_id",
    "source_disclosure_date",
    "source_posted_date",
    "profile_id",
    "regulation_id",
    "regulation_version",
    "revision_date",
    "effective_from",
    "effective_to",
    "repealed_at",
    "regulation_status",
    "supersedes_document_id",
    "chunk_type",
    "hierarchy_path",
    "part_no",
    "part_title",
    "chapter_no",
    "chapter_title",
    "section_no",
    "section_title",
    "regulation_no",
    "regulation_title",
    "article_no",
    "article_title",
    "paragraph_no",
    "paragraph_label",
    "item_no",
    "references",
    "article_refs",
    "internal_regulation_refs",
    "regulation_article_refs",
    "appendix_refs",
    "form_refs",
    "external_law_refs",
    "effective_date",
    "revision_date",
    "valid_from",
    "valid_to",
    "revision_history",
    "revision_history_spans",
    "article_effective_overrides",
    "article_validity_windows",
    "temporal_metadata_inherited",
    "temporal_metadata_scope",
    "temporal_metadata_inherited_fields",
    "temporal_metadata_normalized_fields",
    "temporal_metadata_source_chunk_ids",
    "temporal_metadata_conflict_fields",
    "temporal_metadata_ambiguous_fields",
    "temporal_metadata_ambiguous_scope",
    "temporal_metadata_ambiguous_source_chunk_ids",
    "is_supplementary_provision",
    "supplementary_label",
    "supplementary_identifier_date",
    "supplementary_paragraph_label",
    "supplementary_paragraph_labels",
    "supplementary_boilerplate",
    "table_like",
    "table_header_cells",
    "table_column_count",
    "table_structured_row_count",
    "table_records",
    "table_record_count",
    "table_classification",
    "table_confidence",
    "table_review_reason",
    "table_review_required",
    "table_review_flags",
    "table_source",
    "table_geometry_source",
    "table_appendix_no",
    "table_appendix_title",
    "table_citation_label",
    "table_false_positive_stability",
    "kordoc_table_parser_status",
    "kordoc_table_count",
    "kordoc_table_match",
    "kordoc_table_match_review_required",
    "kordoc_table_match_provisional",
    "kordoc_table_promoted",
    "kordoc_table_promotion_review_required",
    "source_hwpx_block_types",
    "source_xml_files",
    "source_xml_roles",
    "source_hwpx_block_type_count",
    "source_hwpx_parser_review_flags",
    "source_hwpx_image_caption_count",
    "source_hwpx_table_row_count",
    "source_hwpx_table_cell_count",
    "source_hwpx_table_caption_count",
    "source_hwpx_nested_table_count",
    "source_hwpx_table_image_count",
    "source_hwpx_table_note_count",
    "source_hwpx_merged_cell_count",
    "source_hwpx_table_direct_captions",
    "source_hwpx_table_image_captions",
    "source_hwpx_table_note_snippets",
    "source_hwpx_nested_table_text_snippets",
    "source_hwpx_xml_block_indices",
    "source_hwp_extraction_modes",
    "source_hwp_streams",
    "source_hwp_section_indices",
    "source_hwp_native_table_geometry",
    "pdf_embedded_image_pages",
    "reference_edges",
    "resolved_reference_count",
    "unresolved_reference_count",
    "answer_profile_version",
    "answer_intents",
    "answer_keywords",
    "answer_facts",
    "answer_outline",
    "confidence",
    "warnings",
    "parser_uncertainty",
    "parser_uncertainty_schema_version",
    "parser_uncertainty_source",
    "parser_uncertainty_risk_level",
    "parser_uncertainty_confidence",
    "parser_uncertainty_flags",
    "parser_uncertainty_recommendation",
    "parser_uncertainty_remediation_hint",
    "parser_version",
    "chunker_version",
    "approval_status",
    "approval_id",
    "approved_by",
    "approved_at",
    "approved_content_hash",
    "approval_worklist_report_path",
    "approval_worklist_report_sha256",
    "approval_review_batch_manifest_path",
    "approval_review_batch_manifest_sha256",
    "approval_review_batch_id",
    "approval_review_batch_chunk_fingerprint",
    "approval_review_strategy",
    "security_level",
    "department_acl",
)


def _normalize_public_lifecycle_metadata(chunk: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(metadata)

    if not str(normalized.get("regulation_id") or "").strip():
        regulation_id = next(
            (
                str(value).strip()
                for value in (
                    _chunk_value(chunk, "regulation_id"),
                    _chunk_value(chunk, "regulation_no"),
                )
                if str(value or "").strip()
            ),
            "",
        )
        if regulation_id:
            normalized["regulation_id"] = regulation_id

    if not str(normalized.get("regulation_version") or "").strip():
        regulation_version = next(
            (
                str(value).strip()
                for value in (
                    _chunk_value(chunk, "regulation_version"),
                    _chunk_value(chunk, "version"),
                    _chunk_value(chunk, "revision_date"),
                    _chunk_value(chunk, "valid_from"),
                    _chunk_value(chunk, "effective_date"),
                )
                if str(value or "").strip()
            ),
            "",
        )
        if regulation_version:
            normalized["regulation_version"] = regulation_version

    if _is_empty(normalized.get("effective_from")):
        effective_from = next(
            (
                value
                for value in (
                    _chunk_value(chunk, "effective_from"),
                    _chunk_value(chunk, "valid_from"),
                    _chunk_value(chunk, "effective_date"),
                    _chunk_value(chunk, "revision_date"),
                )
                if value not in (None, "")
            ),
            None,
        )
        if effective_from is not None or "effective_from" not in normalized:
            normalized["effective_from"] = _json_safe(effective_from)

    for field in ("effective_to", "repealed_at"):
        if field not in normalized:
            normalized[field] = _json_safe(_chunk_value(chunk, field))

    return normalized

LOCAL_PATH_PATTERNS = (
    re.compile(r"(?i)\b[A-Z]:[\\/][^\s\"']+"),
    re.compile(r"(?i)\bfile://[^\s\"']+"),
    re.compile(r"(?i)(?:^|[\s\"'])(?:/app/|/data/|/home/|/mnt/|/tmp/|/usr/src/app/|/users/|/var/|/workspace/)[^\s\"']+"),
    re.compile(r"(?i)(?:^|[\s\"'])\\\\[^\\/\s]+[\\/][^\s\"']+"),
)


def vector_record_from_chunk(
    chunk: dict[str, Any],
    *,
    text_field: str = "retrieval_text",
    metadata_fields: Iterable[str] = VECTOR_METADATA_FIELDS,
    require_approval: bool = True,
) -> dict[str, Any] | None:
    if require_approval and not is_chunk_approved_for_indexing(chunk):
        return None
    text = _select_text(chunk, text_field)
    if not text:
        return None
    metadata = public_vector_metadata(chunk, metadata_fields=metadata_fields)
    chunk_id = str(chunk.get("chunk_id") or "")
    document_id = str(chunk.get("document_id") or "")
    record_id = stable_vector_id(document_id, chunk_id)
    content_hash = stable_content_hash(text, metadata)
    record = {
        "schema_version": VECTOR_RECORD_SCHEMA_VERSION,
        "id": record_id,
        "document_id": document_id,
        "chunk_id": chunk_id,
        "text": text,
        "metadata": metadata,
        "content_hash": content_hash,
    }
    return with_vector_record_verification(record)


def public_vector_metadata(
    chunk: dict[str, Any],
    *,
    metadata_fields: Iterable[str] = VECTOR_METADATA_FIELDS,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for field in metadata_fields:
        value = _chunk_value(chunk, field)
        preserve_optional_lifecycle_field = field in {"effective_to", "repealed_at"}
        if value is None and not preserve_optional_lifecycle_field:
            continue
        value = _json_safe(value)
        if _is_empty(value) and not (preserve_optional_lifecycle_field and value is None):
            continue
        metadata[field] = value
    return _normalize_public_lifecycle_metadata(chunk, metadata)


def build_vector_records(
    chunks: Iterable[dict[str, Any]],
    *,
    text_field: str = "retrieval_text",
    require_approval: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records: list[dict[str, Any]] = []
    skipped_empty_text_count = 0
    skipped_unapproved_count = 0
    approval_status_counts: Counter[str] = Counter()
    for chunk in chunks:
        approval_status = chunk_approval_status(chunk)
        approval_status_counts[approval_status] += 1
        if not _select_text(chunk, text_field):
            skipped_empty_text_count += 1
            continue
        if require_approval and not is_chunk_approved_for_indexing(chunk):
            skipped_unapproved_count += 1
            continue
        record = vector_record_from_chunk(chunk, text_field=text_field, require_approval=require_approval)
        if record is None:
            skipped_empty_text_count += 1
            continue
        records.append(record)
    return records, summarize_vector_records(
        records,
        skipped_empty_text_count=skipped_empty_text_count,
        skipped_unapproved_count=skipped_unapproved_count,
        approval_status_counts=dict(sorted(approval_status_counts.items())),
    )


def summarize_vector_records(
    records: list[dict[str, Any]],
    *,
    skipped_empty_text_count: int = 0,
    skipped_unapproved_count: int = 0,
    approval_status_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    ids = [record["id"] for record in records]
    duplicate_ids = sorted([record_id for record_id, count in Counter(ids).items() if count > 1])
    metadata_fields = sorted({field for record in records for field in record.get("metadata", {})})
    path_leaks = vector_record_path_leaks(records)
    return {
        "schema_version": VECTOR_RECORD_SCHEMA_VERSION,
        "record_count": len(records),
        "skipped_empty_text_count": skipped_empty_text_count,
        "skipped_unapproved_count": skipped_unapproved_count,
        "approval_status_counts": approval_status_counts or {},
        "document_count": len({record.get("document_id") for record in records if record.get("document_id")}),
        "duplicate_id_count": len(duplicate_ids),
        "duplicate_id_samples": duplicate_ids[:20],
        "metadata_fields": metadata_fields,
        "chunk_type_counts": dict(sorted(Counter(_metadata_value(record, "chunk_type") for record in records).items())),
        "source_system_counts": dict(sorted(Counter(_metadata_value(record, "source_system") for record in records).items())),
        "profile_id_counts": dict(sorted(Counter(_metadata_value(record, "profile_id") for record in records).items())),
        "temporal_metadata_counts": {
            "effective_date": sum(1 for record in records if record.get("metadata", {}).get("effective_date")),
            "revision_date": sum(1 for record in records if record.get("metadata", {}).get("revision_date")),
            "valid_from": sum(1 for record in records if record.get("metadata", {}).get("valid_from")),
            "valid_to": sum(1 for record in records if record.get("metadata", {}).get("valid_to")),
            "revision_history": sum(1 for record in records if record.get("metadata", {}).get("revision_history")),
            "revision_history_spans": sum(
                1 for record in records if record.get("metadata", {}).get("revision_history_spans")
            ),
            "article_effective_overrides": sum(
                1 for record in records if record.get("metadata", {}).get("article_effective_overrides")
            ),
            "article_validity_windows": sum(
                1 for record in records if record.get("metadata", {}).get("article_validity_windows")
            ),
            "supplementary_identifier_date": sum(
                1 for record in records if record.get("metadata", {}).get("supplementary_identifier_date")
            ),
            "supplementary_boilerplate": sum(
                1 for record in records if record.get("metadata", {}).get("supplementary_boilerplate")
            ),
            "temporal_metadata_inherited": sum(
                1 for record in records if record.get("metadata", {}).get("temporal_metadata_inherited")
            ),
            "temporal_metadata_normalized": sum(
                1 for record in records if record.get("metadata", {}).get("temporal_metadata_normalized_fields")
            ),
            "temporal_metadata_ambiguous": sum(
                1 for record in records if record.get("metadata", {}).get("temporal_metadata_ambiguous_fields")
            ),
        },
        "hwpx_metadata_counts": {
            "source_hwpx_xml_block_indices": sum(
                1 for record in records if record.get("metadata", {}).get("source_hwpx_xml_block_indices")
            ),
            "source_hwpx_nested_table_text_snippets": sum(
                1 for record in records if record.get("metadata", {}).get("source_hwpx_nested_table_text_snippets")
            ),
            "source_hwpx_table_direct_captions": sum(
                1 for record in records if record.get("metadata", {}).get("source_hwpx_table_direct_captions")
            ),
            "source_hwpx_table_image_captions": sum(
                1 for record in records if record.get("metadata", {}).get("source_hwpx_table_image_captions")
            ),
            "source_hwpx_table_note_snippets": sum(
                1 for record in records if record.get("metadata", {}).get("source_hwpx_table_note_snippets")
            ),
        },
        "hwp_metadata_counts": {
            "source_hwp_extraction_modes": sum(
                1 for record in records if record.get("metadata", {}).get("source_hwp_extraction_modes")
            ),
            "source_hwp_streams": sum(
                1 for record in records if record.get("metadata", {}).get("source_hwp_streams")
            ),
            "source_hwp_section_indices": sum(
                1 for record in records if record.get("metadata", {}).get("source_hwp_section_indices")
            ),
            "source_hwp_native_table_geometry_false": sum(
                1
                for record in records
                if record.get("metadata", {}).get("source_hwp_native_table_geometry") is False
            ),
        },
        "kordoc_metadata_counts": {
            "kordoc_table_parser_status": sum(
                1 for record in records if record.get("metadata", {}).get("kordoc_table_parser_status")
            ),
            "kordoc_table_count": sum(
                1 for record in records if record.get("metadata", {}).get("kordoc_table_count") is not None
            ),
            "kordoc_table_match": sum(
                1 for record in records if record.get("metadata", {}).get("kordoc_table_match")
            ),
            "kordoc_table_promoted": sum(
                1 for record in records if record.get("metadata", {}).get("kordoc_table_promoted")
            ),
        },
        "local_path_leak_count": len(path_leaks),
        "local_path_leak_samples": path_leaks[:20],
    }


def is_chunk_approved_for_indexing(chunk: dict[str, Any]) -> bool:
    return (
        chunk_approval_status(chunk) == APPROVED_CHUNK_STATUS
        and bool(_chunk_value(chunk, "approval_id"))
        and bool(str(_chunk_value(chunk, "approved_content_hash") or "").strip())
        and bool(str(_chunk_value(chunk, "tenant_id") or "").strip())
        and _valid_security_level(_chunk_value(chunk, "security_level"))
    )


def approval_provenance_issue_fields(chunk: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for field in APPROVAL_PROVENANCE_METADATA_FIELDS:
        if not str(_chunk_value(chunk, field) or "").strip():
            issues.append(field)
    for field in APPROVAL_PROVENANCE_SHA256_FIELDS:
        value = str(_chunk_value(chunk, field) or "").strip().lower()
        if value and not re.fullmatch(r"[a-f0-9]{64}", value):
            issues.append(field)
    return sorted(set(issues))


def with_vector_record_verification(record: dict[str, Any], *, verified_at: str | None = None) -> dict[str, Any]:
    stamped = dict(record)
    stamped["verification_version"] = VECTOR_RECORD_VERIFICATION_VERSION
    stamped["verification_hash"] = vector_record_verification_hash(record)
    stamped["verified_at"] = verified_at or datetime.now(timezone.utc).isoformat()
    return stamped


def vector_record_verification_hash(record: dict[str, Any]) -> str:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    payload = {
        "id": str(record.get("id") or ""),
        "document_id": str(record.get("document_id") or metadata.get("document_id") or ""),
        "chunk_id": str(record.get("chunk_id") or metadata.get("chunk_id") or ""),
        "tenant_id": str(record.get("tenant_id") or metadata.get("tenant_id") or ""),
        "content_hash": str(record.get("content_hash") or ""),
        "approval_status": str(metadata.get("approval_status") or "").strip().lower(),
        "approval_id": str(metadata.get("approval_id") or ""),
        "approved_content_hash": str(metadata.get("approved_content_hash") or ""),
        "security_level": str(metadata.get("security_level") or "").strip().lower(),
        "department_acl": _stable_list(metadata.get("department_acl")),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def chunk_approval_status(chunk: dict[str, Any]) -> str:
    return str(_chunk_value(chunk, "approval_status") or "missing").strip().lower() or "missing"


def _chunk_value(chunk: dict[str, Any], key: str) -> Any:
    if key in chunk:
        return chunk.get(key)
    metadata = chunk.get("metadata")
    if isinstance(metadata, dict):
        return metadata.get(key)
    return None


def _valid_security_level(value: Any) -> bool:
    return str(value or "").strip().lower() in ALLOWED_SECURITY_LEVELS


def _stable_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return sorted(str(item) for item in value if str(item).strip())
    return [str(value)]


def vector_record_path_leaks(records: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    leaks: list[dict[str, str]] = []
    for record in records:
        for path, value in _iter_string_values(record):
            if _looks_like_local_path(value):
                leaks.append({"id": str(record.get("id", "")), "field_path": path, "value": value[:240]})
    return leaks


def stable_vector_id(document_id: str, chunk_id: str) -> str:
    if document_id and chunk_id:
        return f"{document_id}:{chunk_id}"
    return hashlib.sha256(f"{document_id}\n{chunk_id}".encode("utf-8")).hexdigest()


def stable_content_hash(text: str, metadata: dict[str, Any]) -> str:
    payload = json.dumps({"text": text, "metadata": metadata}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _select_text(chunk: dict[str, Any], text_field: str) -> str:
    preferred_fields = {
        "retrieval_text": ("retrieval_text", "text", "normalized_text"),
        "text": ("text", "retrieval_text", "normalized_text"),
        "normalized_text": ("normalized_text", "text", "retrieval_text"),
    }.get(text_field)
    if preferred_fields is None:
        raise ValueError("text_field must be one of: retrieval_text, text, normalized_text")
    for field in preferred_fields:
        value = chunk.get(field)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            if text_field == "retrieval_text":
                return _with_table_markdown(text, chunk)
            return text
    return ""


def _with_table_markdown(text: str, chunk: dict[str, Any]) -> str:
    table_markdown = chunk.get("table_markdown")
    if not table_markdown and isinstance(chunk.get("metadata"), dict):
        table_markdown = chunk["metadata"].get("table_markdown")
    table_markdown = str(table_markdown or "").strip()
    if not table_markdown or "[표]" in text:
        return text
    return f"{text.rstrip()}\n[표]\n{table_markdown}"


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_json_safe(item) for item in value if not _is_empty(_json_safe(item))]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value if not _is_empty(_json_safe(item))]
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            safe_item = _json_safe(item)
            if not _is_empty(safe_item):
                cleaned[str(key)] = safe_item
        return cleaned
    return str(value)


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _metadata_value(record: dict[str, Any], key: str) -> str:
    value = record.get("metadata", {}).get(key)
    return str(value or "")


def _iter_string_values(value: Any, *, path: str = "$") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_string_values(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_string_values(item, path=f"{path}.{key}")


def _looks_like_local_path(value: str) -> bool:
    return any(pattern.search(value) for pattern in LOCAL_PATH_PATTERNS)
