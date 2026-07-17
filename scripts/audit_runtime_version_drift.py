from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings
from app.core.pipeline import PREPROCESSOR_PIPELINE_VERSION
from app.core.tenant_access import settings_for_tenant, tenant_storage_key
from app.ingestion.embedding_adapter import EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION
from app.ingestion.vector_adapter import (
    ALLOWED_SECURITY_LEVELS,
    APPROVED_CHUNK_STATUS,
    VECTOR_RECORD_SCHEMA_VERSION,
    VECTOR_RECORD_VERIFICATION_VERSION,
    stable_content_hash,
    vector_record_path_leaks,
    vector_record_verification_hash,
)
from app.ingestion.vector_integrity import embedded_vector_integrity_reason
from app.processors.answer_profile import ANSWER_PROFILE_VERSION
from app.processors.chunker import CHUNKER_VERSION


VERSION_FIELDS = (
    "parser_version",
    "chunker_version",
    "answer_profile_version",
)


def build_runtime_version_drift_report(
    *,
    data_dir: Path,
    tenant_id: str = "default",
    tenant_storage_isolation: bool | None = None,
    sample_limit: int = 25,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    effective_isolation = _tenant_storage_isolation(data_dir, tenant_storage_isolation)
    effective_dir = _effective_runtime_dir(
        data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=effective_isolation,
    )
    chunks = _load_runtime_chunks(effective_dir)
    if not effective_isolation:
        chunks = _filter_chunks_for_tenant(chunks, tenant_id=tenant_id)
    records = _load_vector_records(effective_dir, tenant_id=tenant_id)
    approved_chunks = [chunk for chunk in chunks if _is_approved(chunk)]
    repository_rows = [_version_row(chunk) for chunk in chunks]
    approved_rows = [_version_row(chunk) for chunk in approved_chunks]
    vector_rows = [_version_row(record) for record in records]
    repository_chunker_stale = [
        row for row in approved_rows if row["metadata"].get("chunker_version") != CHUNKER_VERSION
    ]
    repository_parser_missing = [
        row for row in approved_rows if not _has_value(row["metadata"].get("parser_version"))
    ]
    vector_chunker_stale = [
        row for row in vector_rows if row["metadata"].get("chunker_version") != CHUNKER_VERSION
    ]
    version_loss = _version_loss(approved_rows, vector_rows)
    vector_integrity = _vector_integrity_summary(records, sample_limit=sample_limit)
    findings = []
    if not chunks:
        findings.append(_finding("blocker", "runtime-chunks-missing", "No repository chunks were available for version drift audit."))
    if not records:
        findings.append(_finding("blocker", "vector-records-missing", "No approved vector records were available for version drift audit."))
    if repository_chunker_stale:
        findings.append(
            _finding(
                "warning",
                "runtime-chunker-version-stale",
                "Approved repository chunks were built with a different chunker_version than the current code.",
            )
        )
    if vector_chunker_stale:
        findings.append(
            _finding(
                "warning",
                "vector-chunker-version-stale",
                "Approved vector records were built with a different chunker_version than the current code.",
            )
        )
    if repository_parser_missing:
        findings.append(
            _finding(
                "info",
                "runtime-parser-version-missing",
                "Some approved repository chunks do not carry parser_version metadata.",
            )
        )
    if version_loss["loss_count"] or version_loss["mismatch_count"]:
        findings.append(
            _finding(
                "warning",
                "repository-vector-version-mismatch",
                "Version metadata in approved repository chunks is not fully preserved in approved vector records.",
            )
        )
    if vector_integrity["failure_count"]:
        findings.append(
            _finding(
                "blocker",
                "vector-integrity-failure",
                "Approved vector records have invalid hashes, missing required metadata, embedded-vector integrity failures, duplicate ids, or local path leaks.",
            )
        )

    report = {
        "report_type": "runtime_version_drift",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime_data_dir": str(data_dir),
        "effective_runtime_data_dir": str(effective_dir),
        "tenant_id": tenant_id,
        "tenant_storage_isolation": effective_isolation,
        "current_versions": {
            "preprocessor_pipeline_version": PREPROCESSOR_PIPELINE_VERSION,
            "chunker_version": CHUNKER_VERSION,
            "answer_profile_version": ANSWER_PROFILE_VERSION,
            "vector_record_schema_version": VECTOR_RECORD_SCHEMA_VERSION,
            "vector_record_verification_version": VECTOR_RECORD_VERIFICATION_VERSION,
        },
        "repository_chunk_count": len(chunks),
        "approved_repository_chunk_count": len(approved_chunks),
        "vector_record_count": len(records),
        "repository_version_counts": _version_counts(repository_rows),
        "approved_repository_version_counts": _version_counts(approved_rows),
        "vector_version_counts": _version_counts(vector_rows),
        "approved_repository_stale_chunker_count": len(repository_chunker_stale),
        "approved_repository_stale_chunker_ratio": _ratio(len(repository_chunker_stale), len(approved_rows)),
        "vector_stale_chunker_count": len(vector_chunker_stale),
        "vector_stale_chunker_ratio": _ratio(len(vector_chunker_stale), len(vector_rows)),
        "approved_repository_missing_parser_version_count": len(repository_parser_missing),
        "version_loss": version_loss,
        "vector_integrity": vector_integrity,
        "reapproval_scope": {
            "reprocess_requires_reapproval": bool(repository_chunker_stale),
            "approved_chunks_with_stale_chunker_count": len(repository_chunker_stale),
            "approved_chunks_with_approved_hash_count": sum(1 for row in repository_chunker_stale if row["approved_content_hash"]),
            "reason": "approved_content_hash covers approved retrieval text and metadata, so changing parser/chunker-derived metadata requires review and reapproval before reindexing.",
        },
        "stale_chunker_samples": _sample_rows(repository_chunker_stale, limit=sample_limit),
        "vector_stale_chunker_samples": _sample_rows(vector_chunker_stale, limit=sample_limit),
        "finding_count": len(findings),
        "blocker_count": sum(1 for item in findings if item["severity"] == "blocker"),
        "warning_count": sum(1 for item in findings if item["severity"] == "warning"),
        "findings": findings,
        "passed": not any(item["severity"] == "blocker" for item in findings),
        "api_call_count": 0,
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _effective_runtime_dir(data_dir: Path, *, tenant_id: str, tenant_storage_isolation: bool | None) -> Path:
    settings = Settings(
        data_dir=data_dir,
        tenant_storage_isolation=_tenant_storage_isolation(data_dir, tenant_storage_isolation),
    )
    return settings_for_tenant(settings, tenant_id).data_dir


def _tenant_storage_isolation(data_dir: Path, tenant_storage_isolation: bool | None) -> bool:
    if tenant_storage_isolation is not None:
        return tenant_storage_isolation
    return Settings(data_dir=data_dir).tenant_storage_isolation


def _load_runtime_chunks(effective_dir: Path) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    repository_dir = effective_dir / "repository"
    if not repository_dir.is_dir():
        return chunks
    for path in sorted(repository_dir.glob("*_chunks.json")):
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, list):
            chunks.extend(item for item in payload if isinstance(item, dict))
        elif isinstance(payload, dict) and isinstance(payload.get("chunks"), list):
            chunks.extend(item for item in payload["chunks"] if isinstance(item, dict))
    return chunks


def _filter_chunks_for_tenant(chunks: list[dict[str, Any]], *, tenant_id: str) -> list[dict[str, Any]]:
    return [chunk for chunk in chunks if _chunk_belongs_to_tenant(chunk, tenant_id)]


def _chunk_belongs_to_tenant(chunk: dict[str, Any], tenant_id: str) -> bool:
    metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
    chunk_tenant = str(chunk.get("tenant_id") or metadata.get("tenant_id") or "").strip()
    if tenant_id == "default":
        return chunk_tenant in {"", "default"}
    return chunk_tenant == tenant_id


def _load_vector_records(effective_dir: Path, *, tenant_id: str) -> list[dict[str, Any]]:
    vector_path = effective_dir / "vector_db" / tenant_storage_key(tenant_id) / "approved_vectors.jsonl"
    if not vector_path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in vector_path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
    return rows


def _version_row(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return {
        "document_id": str(item.get("document_id") or metadata.get("document_id") or ""),
        "chunk_id": str(item.get("chunk_id") or metadata.get("chunk_id") or ""),
        "chunk_type": str(item.get("chunk_type") or metadata.get("chunk_type") or "unknown"),
        "regulation_title": str(metadata.get("regulation_title") or metadata.get("document_name") or ""),
        "article_no": str(metadata.get("article_no") or ""),
        "source_page_start": item.get("source_page_start") or metadata.get("source_page_start"),
        "approval_id": str(item.get("approval_id") or metadata.get("approval_id") or ""),
        "approved_content_hash": str(item.get("approved_content_hash") or metadata.get("approved_content_hash") or ""),
        "content_hash": str(item.get("content_hash") or metadata.get("content_hash") or ""),
        "security_level": str(item.get("security_level") or metadata.get("security_level") or ""),
        "metadata": metadata,
    }


def _is_approved(chunk: dict[str, Any]) -> bool:
    metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
    return str(chunk.get("approval_status") or metadata.get("approval_status") or "").strip().lower() == "approved"


def _version_counts(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, int]]:
    counters: dict[str, Counter[str]] = {field: Counter() for field in VERSION_FIELDS}
    for row in rows:
        metadata = row["metadata"]
        for field in VERSION_FIELDS:
            counters[field][str(metadata.get(field) or "missing")] += 1
    return {field: dict(sorted(counter.items())) for field, counter in counters.items()}


def _version_loss(repository_rows: list[dict[str, Any]], vector_rows: list[dict[str, Any]]) -> dict[str, Any]:
    vector_by_key = {_row_key(row): row for row in vector_rows if _row_key(row)}
    losses = []
    mismatches = []
    for row in repository_rows:
        key = _row_key(row)
        if not key:
            continue
        vector = vector_by_key.get(key)
        if vector is None:
            continue
        for field in VERSION_FIELDS:
            repository_value = row["metadata"].get(field)
            vector_value = vector["metadata"].get(field)
            if _has_value(repository_value) and not _has_value(vector_value):
                losses.append({"key": key, "field": field})
            elif _has_value(repository_value) and _has_value(vector_value) and repository_value != vector_value:
                mismatches.append(
                    {
                        "key": key,
                        "field": field,
                        "repository_value": repository_value,
                        "vector_value": vector_value,
                    }
                )
    return {
        "loss_count": len(losses),
        "mismatch_count": len(mismatches),
        "loss_samples": losses[:25],
        "mismatch_samples": mismatches[:25],
    }


def _vector_integrity_summary(records: list[dict[str, Any]], *, sample_limit: int) -> dict[str, Any]:
    ids = [str(record.get("id") or "") for record in records if str(record.get("id") or "")]
    duplicate_ids = sorted(record_id for record_id, count in Counter(ids).items() if count > 1)
    schema_version_counts = Counter(str(record.get("schema_version") or "missing") for record in records)
    verification_version_counts = Counter(str(record.get("verification_version") or "missing") for record in records)
    supported_schema_versions = {VECTOR_RECORD_SCHEMA_VERSION, EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION}
    unsupported_schema_version_samples = []
    missing_id_samples = []
    missing_text_samples = []
    invalid_approval_status_samples = []
    invalid_security_level_samples = []
    missing_content_hash_samples = []
    content_hash_mismatch_samples = []
    missing_verification_hash_samples = []
    verification_hash_mismatch_samples = []
    unsupported_verification_version_samples = []
    metadata_shape_samples = []
    metadata_missing_samples = []
    embedded_dimension_samples = []
    embedded_integrity_samples = []
    embedded_integrity_reasons: Counter[str] = Counter()
    required_metadata_fields = ("tenant_id", "security_level", "approval_status", "approval_id", "approved_content_hash")
    for record in records:
        record_id = str(record.get("id") or "")
        schema_version = str(record.get("schema_version") or "")
        if schema_version not in supported_schema_versions:
            unsupported_schema_version_samples.append({"id": record_id, "schema_version": schema_version or "missing"})
        if not record_id:
            missing_id_samples.append({"id": record_id})
        text = str(record.get("text") or "")
        metadata = record.get("metadata")
        if not isinstance(metadata, dict):
            metadata_shape_samples.append({"id": record_id, "reason": "metadata_not_object"})
            metadata = {}
        missing_fields = [field for field in required_metadata_fields if not _has_value(metadata.get(field))]
        if missing_fields:
            metadata_missing_samples.append({"id": record_id, "missing_fields": missing_fields})
        approval_status = str(metadata.get("approval_status") or "").strip().lower()
        if approval_status != APPROVED_CHUNK_STATUS:
            invalid_approval_status_samples.append({"id": record_id, "approval_status": approval_status or "missing"})
        security_level = str(metadata.get("security_level") or "").strip().lower()
        if security_level not in ALLOWED_SECURITY_LEVELS:
            invalid_security_level_samples.append({"id": record_id, "security_level": security_level or "missing"})
        if not text.strip():
            missing_text_samples.append({"id": record_id})
        content_hash = str(record.get("content_hash") or "")
        if not content_hash:
            missing_content_hash_samples.append({"id": record_id})
        elif stable_content_hash(text, metadata) != content_hash:
            content_hash_mismatch_samples.append({"id": record_id, "content_hash_short": _short_hash(content_hash)})
        verification_version = str(record.get("verification_version") or "")
        verification_hash = str(record.get("verification_hash") or "")
        if verification_version and verification_version != VECTOR_RECORD_VERIFICATION_VERSION:
            unsupported_verification_version_samples.append(
                {"id": record_id, "verification_version": verification_version}
            )
        if verification_version == VECTOR_RECORD_VERIFICATION_VERSION and not verification_hash:
            missing_verification_hash_samples.append({"id": record_id})
        if verification_hash and verification_hash != vector_record_verification_hash(record):
            verification_hash_mismatch_samples.append(
                {"id": record_id, "verification_hash_short": _short_hash(verification_hash)}
            )
        if schema_version == EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION:
            embedding = record.get("embedding")
            embedding_dimensions = record.get("embedding_dimensions")
            if isinstance(embedding, list) and (
                not isinstance(embedding_dimensions, int) or embedding_dimensions != len(embedding)
            ):
                embedded_dimension_samples.append(
                    {"id": record_id, "embedding_dimensions": embedding_dimensions, "actual_dimensions": len(embedding)}
                )
        embedded_reason = embedded_vector_integrity_reason(record)
        if embedded_reason:
            embedded_integrity_reasons[embedded_reason] += 1
            embedded_integrity_samples.append({"id": record_id, "reason": embedded_reason})
    path_leaks = vector_record_path_leaks(records)
    failure_count = (
        len(duplicate_ids)
        + len(unsupported_schema_version_samples)
        + len(missing_id_samples)
        + len(missing_text_samples)
        + len(invalid_approval_status_samples)
        + len(invalid_security_level_samples)
        + len(missing_content_hash_samples)
        + len(content_hash_mismatch_samples)
        + len(missing_verification_hash_samples)
        + len(verification_hash_mismatch_samples)
        + len(unsupported_verification_version_samples)
        + len(metadata_shape_samples)
        + len(metadata_missing_samples)
        + len(embedded_dimension_samples)
        + len(embedded_integrity_samples)
        + len(path_leaks)
    )
    return {
        "record_count": len(records),
        "schema_version_counts": dict(sorted(schema_version_counts.items())),
        "verification_version_counts": dict(sorted(verification_version_counts.items())),
        "unsupported_schema_version_count": len(unsupported_schema_version_samples),
        "missing_id_count": len(missing_id_samples),
        "duplicate_id_count": len(duplicate_ids),
        "duplicate_id_samples": duplicate_ids[:sample_limit],
        "missing_text_count": len(missing_text_samples),
        "invalid_approval_status_count": len(invalid_approval_status_samples),
        "invalid_security_level_count": len(invalid_security_level_samples),
        "missing_content_hash_count": len(missing_content_hash_samples),
        "content_hash_mismatch_count": len(content_hash_mismatch_samples),
        "missing_verification_hash_count": len(missing_verification_hash_samples),
        "verification_hash_mismatch_count": len(verification_hash_mismatch_samples),
        "unsupported_verification_version_count": len(unsupported_verification_version_samples),
        "metadata_shape_failure_count": len(metadata_shape_samples),
        "metadata_missing_required_count": len(metadata_missing_samples),
        "required_metadata_fields": list(required_metadata_fields),
        "embedded_dimension_mismatch_count": len(embedded_dimension_samples),
        "embedded_integrity_failure_count": len(embedded_integrity_samples),
        "embedded_integrity_reason_counts": dict(sorted(embedded_integrity_reasons.items())),
        "local_path_leak_count": len(path_leaks),
        "local_path_leak_samples": path_leaks[:sample_limit],
        "failure_count": failure_count,
        "failure_samples": (
            [
                {"reason": "duplicate_id", "id": record_id}
                for record_id in duplicate_ids[:sample_limit]
            ]
            + [{"reason": "unsupported_schema_version", **item} for item in unsupported_schema_version_samples[:sample_limit]]
            + [{"reason": "missing_id", **item} for item in missing_id_samples[:sample_limit]]
            + [{"reason": "missing_text", **item} for item in missing_text_samples[:sample_limit]]
            + [{"reason": "invalid_approval_status", **item} for item in invalid_approval_status_samples[:sample_limit]]
            + [{"reason": "invalid_security_level", **item} for item in invalid_security_level_samples[:sample_limit]]
            + [{"reason": "missing_content_hash", **item} for item in missing_content_hash_samples[:sample_limit]]
            + [{"reason": "content_hash_mismatch", **item} for item in content_hash_mismatch_samples[:sample_limit]]
            + [{"reason": "missing_verification_hash", **item} for item in missing_verification_hash_samples[:sample_limit]]
            + [{"reason": "verification_hash_mismatch", **item} for item in verification_hash_mismatch_samples[:sample_limit]]
            + [{"reason": "unsupported_verification_version", **item} for item in unsupported_verification_version_samples[:sample_limit]]
            + [{"reason": "metadata_shape_failure", **item} for item in metadata_shape_samples[:sample_limit]]
            + [{"reason": "metadata_missing_required", **item} for item in metadata_missing_samples[:sample_limit]]
            + [{"reason": "embedded_dimension_mismatch", **item} for item in embedded_dimension_samples[:sample_limit]]
            + [{"reason": "embedded_integrity_failure", **item} for item in embedded_integrity_samples[:sample_limit]]
            + [{"reason": "local_path_leak", **item} for item in path_leaks[:sample_limit]]
        )[:sample_limit],
    }


def _row_key(row: dict[str, Any]) -> str:
    document_id = row.get("document_id")
    chunk_id = row.get("chunk_id")
    if document_id and chunk_id:
        return f"{document_id}:{chunk_id}"
    return str(chunk_id or "")


def _sample_rows(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    return [
        {
            "document_id": row["document_id"],
            "chunk_id": row["chunk_id"],
            "chunk_type": row["chunk_type"],
            "regulation_title": row["regulation_title"],
            "article_no": row["article_no"],
            "source_page_start": row["source_page_start"],
            "approval_id": row["approval_id"],
            "approved_content_hash_short": _short_hash(row["approved_content_hash"]),
            "content_hash_short": _short_hash(row["content_hash"]),
            "security_level": row["security_level"],
            "chunker_version": row["metadata"].get("chunker_version") or "",
            "parser_version": row["metadata"].get("parser_version") or "",
        }
        for row in rows[:limit]
    ]


def _finding(severity: str, code: str, detail: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "detail": detail}


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def _ratio(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def _short_hash(value: Any) -> str:
    text = str(value or "")
    return text[:12] if len(text) > 12 else text


def _to_markdown(report: dict[str, Any]) -> str:
    current = report.get("current_versions") or {}
    reapproval = report.get("reapproval_scope") or {}
    integrity = report.get("vector_integrity") or {}
    lines = [
        "# Runtime Version Drift",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Tenant: `{report.get('tenant_id')}`",
        f"- Runtime: `{report.get('effective_runtime_data_dir')}`",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Blockers: {report.get('blocker_count')}",
        f"- Warnings: {report.get('warning_count')}",
        f"- Repository chunks: {report.get('repository_chunk_count')}",
        f"- Approved repository chunks: {report.get('approved_repository_chunk_count')}",
        f"- Vector records: {report.get('vector_record_count')}",
        f"- Current chunker version: `{current.get('chunker_version')}`",
        f"- Approved stale chunker chunks: {report.get('approved_repository_stale_chunker_count')} ({report.get('approved_repository_stale_chunker_ratio')})",
        f"- Vector stale chunker records: {report.get('vector_stale_chunker_count')} ({report.get('vector_stale_chunker_ratio')})",
        f"- Vector integrity failures: {integrity.get('failure_count', 0)}",
        f"- Vector content hash mismatches: {integrity.get('content_hash_mismatch_count', 0)}",
        f"- Vector verification hash mismatches: {integrity.get('verification_hash_mismatch_count', 0)}",
        f"- Vector local path leaks: {integrity.get('local_path_leak_count', 0)}",
        f"- Reprocess requires reapproval: `{str(reapproval.get('reprocess_requires_reapproval')).lower()}`",
        f"- Reapproval scope chunks: {reapproval.get('approved_chunks_with_stale_chunker_count')}",
        f"- API calls: {report.get('api_call_count')}",
        "",
        "## Findings",
        "",
    ]
    for finding in report.get("findings") or []:
        lines.append(f"- {finding.get('severity')} `{finding.get('code')}`: {finding.get('detail')}")
    if not report.get("findings"):
        lines.append("- None.")
    lines.extend(["", "## Version Counts", ""])
    for label, counts in (
        ("Approved Repository", report.get("approved_repository_version_counts") or {}),
        ("Vector", report.get("vector_version_counts") or {}),
    ):
        lines.extend([f"### {label}", "", "| Field | Version | Count |", "| --- | --- | ---: |"])
        for field, counter in counts.items():
            for version, count in (counter or {}).items():
                lines.append(f"| {field} | `{version}` | {count} |")
        lines.append("")
    if integrity:
        lines.extend(
            [
                "## Vector Integrity",
                "",
                f"- Unsupported schema versions: {integrity.get('unsupported_schema_version_count')}",
                f"- Missing ids: {integrity.get('missing_id_count')}",
                f"- Duplicate ids: {integrity.get('duplicate_id_count')}",
                f"- Invalid approval status: {integrity.get('invalid_approval_status_count')}",
                f"- Invalid security level: {integrity.get('invalid_security_level_count')}",
                f"- Missing content hash: {integrity.get('missing_content_hash_count')}",
                f"- Content hash mismatch: {integrity.get('content_hash_mismatch_count')}",
                f"- Missing verification hash: {integrity.get('missing_verification_hash_count')}",
                f"- Verification hash mismatch: {integrity.get('verification_hash_mismatch_count')}",
                f"- Required metadata missing: {integrity.get('metadata_missing_required_count')}",
                f"- Embedded dimension mismatches: {integrity.get('embedded_dimension_mismatch_count')}",
                f"- Embedded integrity failures: {integrity.get('embedded_integrity_failure_count')}",
                f"- Local path leaks: {integrity.get('local_path_leak_count')}",
                "",
            ]
        )
    lines.extend(
        [
            "## Stale Chunker Samples",
            "",
            "| Chunk | Type | Regulation | Article | Page | Security | Approval | Approved hash | Content hash | Version |",
            "| --- | --- | --- | --- | ---: | --- | --- | --- | --- | --- |",
        ]
    )
    for row in report.get("stale_chunker_samples") or []:
        lines.append(
            f"| {_md_cell(row.get('chunk_id'))} | {_md_cell(row.get('chunk_type'))} | {_md_cell(row.get('regulation_title'))} | {_md_cell(row.get('article_no'))} | {_md_cell(row.get('source_page_start'))} | {_md_cell(row.get('security_level'))} | {_md_cell(row.get('approval_id'))} | `{_md_cell(row.get('approved_content_hash_short'))}` | `{_md_cell(row.get('content_hash_short'))}` | `{_md_cell(row.get('chunker_version'))}` |"
        )
    return "\n".join(lines).rstrip() + "\n"


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit runtime parser/chunker version drift for approved chunks and vectors.")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--tenant-id", default="default")
    storage = parser.add_mutually_exclusive_group()
    storage.add_argument("--tenant-storage-isolation", action="store_true")
    storage.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--sample-limit", type=int, default=25)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--fail-on-blocker", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    if stdout is sys.stdout and hasattr(stdout, "reconfigure"):
        stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    tenant_storage_isolation = None
    if args.tenant_storage_isolation:
        tenant_storage_isolation = True
    if args.flat_storage:
        tenant_storage_isolation = False
    report = build_runtime_version_drift_report(
        data_dir=Path(args.data_dir),
        tenant_id=args.tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
        sample_limit=args.sample_limit,
        out_json=Path(args.out_json) if args.out_json else None,
        out_md=Path(args.out_md) if args.out_md else None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stdout)
    if args.fail_on_blocker and report["blocker_count"] > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
