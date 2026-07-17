from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings
from app.core.api_audit import api_audit_path
from app.core.security import normalize_department_ids
from app.core.tenant_access import settings_for_tenant, tenant_storage_key
from app.ingestion.embedding_adapter import LOCAL_HASH_EMBEDDING_MODEL
from app.ingestion.vector_adapter import APPROVED_CHUNK_STATUS, stable_content_hash
from app.ingestion.vector_integrity import embedded_vector_integrity_reason
from app.rag.local_llm import probe_local_llm
from app.storage.repository import JsonRepository


APPROVAL_WORKLIST_EVIDENCE_TO_METADATA = {
    "worklist_report_path": "approval_worklist_report_path",
    "worklist_report_sha256": "approval_worklist_report_sha256",
    "review_batch_manifest_path": "approval_review_batch_manifest_path",
    "review_batch_manifest_sha256": "approval_review_batch_manifest_sha256",
    "review_batch_id": "approval_review_batch_id",
    "review_batch_chunk_fingerprint": "approval_review_batch_chunk_fingerprint",
    "review_strategy": "approval_review_strategy",
}


def build_rag_security_evidence(
    *,
    data_dir: Path,
    tenant_id: str = "default",
    tenant_storage_isolation: bool | None = None,
    rag_llm_backend: str | None = None,
    rag_llm_endpoint: str | None = None,
    rag_llm_model: str | None = None,
    rag_llm_timeout_seconds: int | None = None,
    out_json: Path | None = None,
) -> dict[str, Any]:
    tenant_key = tenant_storage_key(tenant_id)
    auto_isolated = (data_dir / "tenants" / tenant_key).is_dir()
    settings_overrides: dict[str, Any] = {}
    if rag_llm_backend is not None:
        settings_overrides["rag_llm_backend"] = rag_llm_backend
    if rag_llm_endpoint is not None:
        settings_overrides["rag_llm_endpoint"] = rag_llm_endpoint
    if rag_llm_model is not None:
        settings_overrides["rag_llm_model"] = rag_llm_model
    if rag_llm_timeout_seconds is not None:
        settings_overrides["rag_llm_timeout_seconds"] = rag_llm_timeout_seconds
    env_settings = Settings(data_dir=data_dir, **settings_overrides)
    base_settings = Settings(
        data_dir=data_dir,
        tenant_storage_isolation=(
            env_settings.tenant_storage_isolation or auto_isolated
            if tenant_storage_isolation is None
            else tenant_storage_isolation
        ),
        **settings_overrides,
    )
    settings = settings_for_tenant(base_settings, tenant_id)
    repository = JsonRepository(settings)
    vector_path = settings.data_dir / "vector_db" / tenant_key / "approved_vectors.jsonl"
    records = _load_vector_records(vector_path) if vector_path.is_file() else []
    audit_records = _load_jsonl_dicts(api_audit_path(settings))
    api_audit_action_counts = dict(
        sorted(Counter(str(record.get("action") or "") for record in audit_records if record.get("action")).items())
    )
    documents = repository.list_documents()
    all_approval_records = repository.list_approval_journal_records()
    all_approval_vector_sync_events = repository.list_maintenance_events("approval_vector_sync_outcome")
    approval_records, approval_vector_sync_events = _tenant_approval_vector_sync_scope(
        all_approval_records,
        all_approval_vector_sync_events,
        tenant_id=tenant_id,
        tenant_storage_isolation=base_settings.tenant_storage_isolation,
    )
    review_records = repository.list_review_records()
    security_scan_records = repository.list_security_scan_records()
    rag_traces = repository.list_rag_traces()
    rag_feedback = repository.list_rag_feedback()
    indexing_jobs = repository.list_indexing_jobs()
    current_chunks = {
        (chunk.document_id, chunk.chunk_id): chunk
        for document in documents
        for chunk in repository.get_chunks(document.document_id)
    }
    stale_records = []
    for record in records:
        metadata = record.get("metadata") or {}
        key = (str(record.get("document_id") or metadata.get("document_id") or ""), str(record.get("chunk_id") or ""))
        chunk = current_chunks.get(key)
        if chunk is None:
            stale_records.append({"id": record.get("id"), "reason": "missing_current_chunk"})
            continue
        if chunk.approval_status != APPROVED_CHUNK_STATUS:
            stale_records.append({"id": record.get("id"), "reason": f"chunk_status_{chunk.approval_status}"})
            continue
        if chunk.approval_id != metadata.get("approval_id"):
            stale_records.append({"id": record.get("id"), "reason": "approval_id_mismatch"})
            continue
        if str(chunk.security_level or "").strip().lower() != str(metadata.get("security_level") or "").strip().lower():
            stale_records.append({"id": record.get("id"), "reason": "security_level_mismatch"})
            continue
        if _department_acl_set(chunk.department_acl) != _department_acl_set(metadata.get("department_acl")):
            stale_records.append({"id": record.get("id"), "reason": "department_acl_mismatch"})
            continue
        if chunk.approved_content_hash != metadata.get("approved_content_hash"):
            stale_records.append({"id": record.get("id"), "reason": "approved_content_hash_mismatch"})
            continue
        if stable_content_hash(str(record.get("text") or ""), metadata) != str(record.get("content_hash") or ""):
            stale_records.append({"id": record.get("id"), "reason": "tampered_stored_vector"})
            continue
        integrity_reason = embedded_vector_integrity_reason(record)
        if integrity_reason:
            stale_records.append({"id": record.get("id"), "reason": integrity_reason})

    metadata_failures = [
        {
            "id": record.get("id"),
            "missing_fields": [
                field
                for field in ("tenant_id", "security_level", "approval_id", "approval_status", "approved_content_hash")
                if not (record.get("metadata") or {}).get(field)
            ],
        }
        for record in records
    ]
    metadata_failures = [item for item in metadata_failures if item["missing_fields"]]
    approval_chain_failures = _approval_chain_failures(records, approval_records)
    approval_vector_sync_failures = _approval_vector_sync_failures(
        approval_records,
        approval_vector_sync_events,
        tenant_id=tenant_id,
    )
    legacy_approval_count = sum(
        1 for record in approval_records if not str(record.get("vector_sync_event_id") or "").strip()
    )
    indexing_job_failures = _indexing_job_failures(records, indexing_jobs)
    vector_store_failures = _vector_store_failures(records, indexing_jobs)
    audit_control_failures = _audit_control_failures(api_audit_action_counts, rag_traces)
    component_manifest = _component_manifest(settings)
    component_integrity_failures = [
        {"path": item["path"], "reason": "missing_component_source"}
        for item in component_manifest["source_files"]
        if not item.get("exists")
    ]
    local_llm_probe = _local_llm_runtime_probe(settings)
    local_llm_runtime_failures = (
        [{"backend": local_llm_probe.get("backend"), "reason": "local_llm_unavailable"}]
        if local_llm_probe.get("checked") and not local_llm_probe.get("available")
        else []
    )
    report = {
        "report_type": "rag_security_evidence",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant_id": tenant_id,
        "tenant_storage_isolation": base_settings.tenant_storage_isolation,
        "data_dir": str(base_settings.data_dir),
        "effective_data_dir": str(settings.data_dir),
        "vector_path": str(vector_path),
        "vector_path_configured": vector_path.is_file(),
        "vector_record_count": len(records),
        "document_count": len(documents),
        "approval_record_count": len(approval_records),
        "approval_record_source": "append_only_journal",
        "approval_journal_record_count": len(approval_records),
        "approval_vector_sync_outcome_count": len(approval_vector_sync_events),
        "approval_vector_sync_legacy_approval_count": legacy_approval_count,
        "approval_vector_sync_policy": {
            "contract_version": "approval-vector-sync-outcome-v1",
            "scope": "selected_tenant_append_only_approval_journal",
            "legacy_approvals_grandfathered": False,
            "legacy_approval_policy": "fail_closed_requires_audited_backfill_or_reapproval",
        },
        "approval_vector_sync_failure_count": len(approval_vector_sync_failures),
        "approval_vector_sync_failure_samples": approval_vector_sync_failures[:20],
        "review_record_count": len(review_records),
        "security_scan_count": len(security_scan_records),
        "rag_trace_count": len(rag_traces),
        "rag_feedback_count": len(rag_feedback),
        "indexing_job_count": len(indexing_jobs),
        "api_audit_record_count": len(audit_records),
        "api_audit_action_counts": api_audit_action_counts,
        "stale_vector_record_count": len(stale_records),
        "stale_vector_record_samples": stale_records[:20],
        "metadata_failure_count": len(metadata_failures),
        "metadata_failure_samples": metadata_failures[:20],
        "approval_chain_failure_count": len(approval_chain_failures),
        "approval_chain_failure_samples": approval_chain_failures[:20],
        "indexing_job_failure_count": len(indexing_job_failures),
        "indexing_job_failure_samples": indexing_job_failures[:20],
        "vector_store_failure_count": len(vector_store_failures),
        "vector_store_failure_samples": vector_store_failures[:20],
        "audit_control_failure_count": len(audit_control_failures),
        "audit_control_failure_samples": audit_control_failures[:20],
        "component_manifest": component_manifest,
        "component_manifest_hash": _stable_json_hash(component_manifest),
        "component_integrity_failure_count": len(component_integrity_failures),
        "component_integrity_failure_samples": component_integrity_failures[:20],
        "local_llm_runtime_probe": local_llm_probe,
        "local_llm_runtime_failure_count": len(local_llm_runtime_failures),
        "local_llm_runtime_failure_samples": local_llm_runtime_failures[:20],
        "passed": (
            vector_path.is_file()
            and not stale_records
            and not metadata_failures
            and not approval_chain_failures
            and not approval_vector_sync_failures
            and not indexing_job_failures
            and not vector_store_failures
            and not audit_control_failures
            and not component_integrity_failures
            and not local_llm_runtime_failures
        ),
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _load_vector_records(path: Path) -> list[dict[str, Any]]:
    return _load_jsonl_dicts(path)


def _load_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            records.append(item)
    return records


def _department_acl_set(value: Any) -> list[str]:
    if value is None:
        return []
    return sorted(normalize_department_ids(value))


def _approval_chain_failures(records: list[dict[str, Any]], approval_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for record in records:
        metadata = record.get("metadata") or {}
        if not _has_matching_approval_journal_record(record, metadata, approval_records):
            failures.append({"id": record.get("id"), "reason": "missing_matching_approval_journal_record"})
    return failures


def _approval_vector_sync_failures(
    approval_records: list[dict[str, Any]],
    sync_events: list[dict[str, Any]],
    *,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    events_by_id: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in sync_events:
        event_id = str(event.get("event_id") or "").strip()
        if not event_id:
            failures.append({"reason": "sync_outcome_missing_event_id"})
            continue
        events_by_id[event_id].append(event)

    approvals_by_event_id: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for approval in approval_records:
        event_id = str(approval.get("vector_sync_event_id") or "").strip()
        if event_id:
            approvals_by_event_id[event_id].append(approval)

    for event_id, events in sorted(events_by_id.items()):
        if len(events) > 1:
            failures.append(
                {
                    "event_id": event_id,
                    "reason": "duplicate_sync_outcome_event",
                    "event_count": len(events),
                }
            )
    for event_id, approvals in sorted(approvals_by_event_id.items()):
        if len(approvals) > 1:
            failures.append(
                {
                    "event_id": event_id,
                    "reason": "duplicate_approval_sync_event_reference",
                    "approval_count": len(approvals),
                    "approval_record_ids": sorted(
                        str(item.get("approval_record_id") or "").strip() for item in approvals
                    ),
                }
            )

    approval_event_ids = set(approvals_by_event_id)
    for approval in approval_records:
        approval_record_id = str(approval.get("approval_record_id") or "").strip()
        approval_id = str(approval.get("approval_id") or "").strip()
        event_id = str(approval.get("vector_sync_event_id") or "").strip()
        sample = {
            "approval_record_id": approval_record_id,
            "approval_id": approval_id,
        }
        approval_tenant_id = str(approval.get("tenant_id") or "").strip()
        if tenant_id is not None and approval_tenant_id != tenant_id:
            failures.append(
                {
                    **sample,
                    "event_id": event_id,
                    "reason": "approval_sync_tenant_scope_mismatch",
                    "expected_tenant_id": tenant_id,
                    "approval_tenant_id": approval_tenant_id,
                }
            )
        missing_identity_fields = [
            field
            for field in ("approval_record_id", "approval_id", "document_id", "tenant_id", "approved_by")
            if not str(approval.get(field) or "").strip()
        ]
        invalid_payload_fields = [
            field
            for field, expected_type in (("chunk_ids", list), ("approved_content_hashes", dict))
            if not isinstance(approval.get(field), expected_type)
        ]
        if missing_identity_fields or invalid_payload_fields:
            failures.append(
                {
                    **sample,
                    "event_id": event_id,
                    "reason": "approval_sync_contract_invalid",
                    "missing_identity_fields": sorted(missing_identity_fields),
                    "invalid_payload_fields": sorted(invalid_payload_fields),
                }
            )
        if not event_id:
            failures.append({**sample, "reason": "approval_missing_vector_sync_event_id"})
            continue
        matching_events = events_by_id.get(event_id, [])
        if not matching_events:
            failures.append({**sample, "event_id": event_id, "reason": "missing_sync_outcome_event"})
            continue
        if len(matching_events) != 1:
            continue

        event = matching_events[0]
        expected_fields = {
            "approval_record_id": approval_record_id,
            "approval_id": approval_id,
            "document_id": str(approval.get("document_id") or "").strip(),
            "tenant_id": approval_tenant_id,
            "actor": str(approval.get("approved_by") or "").strip(),
            "source_action": "document.review.approve",
            "sync_action": "review_vector_sync",
        }
        mismatched_fields = [
            field
            for field, expected in expected_fields.items()
            if str(event.get(field) or "").strip() != expected
        ]
        event_chunk_ids = event.get("chunk_ids")
        approval_chunk_ids = approval.get("chunk_ids")
        if not isinstance(event_chunk_ids, list) or not isinstance(approval_chunk_ids, list) or sorted(
            str(value) for value in event_chunk_ids
        ) != sorted(str(value) for value in approval_chunk_ids):
            mismatched_fields.append("chunk_ids")
        event_hashes = event.get("approved_content_hashes")
        approval_hashes = approval.get("approved_content_hashes")
        if not isinstance(event_hashes, dict) or not isinstance(approval_hashes, dict) or event_hashes != approval_hashes:
            mismatched_fields.append("approved_content_hashes")
        if event.get("approval_persisted") is not True:
            mismatched_fields.append("approval_persisted")
        if mismatched_fields:
            failures.append(
                {
                    **sample,
                    "event_id": event_id,
                    "reason": "sync_outcome_approval_mismatch",
                    "mismatched_fields": sorted(set(mismatched_fields)),
                }
            )
            continue

        outcome = str(event.get("outcome") or "").strip()
        vector_sync = event.get("vector_sync")
        sync_status = str(vector_sync.get("status") or "").strip() if isinstance(vector_sync, dict) else ""
        valid_outcome = (
            outcome == "completed" and sync_status in {"indexed", "skipped"}
        ) or (
            outcome == "failure" and sync_status == "failed"
        )
        if not valid_outcome:
            failures.append(
                {
                    **sample,
                    "event_id": event_id,
                    "reason": "invalid_sync_outcome_status",
                    "outcome": outcome,
                    "vector_sync_status": sync_status,
                }
            )

    for event_id, events in sorted(events_by_id.items()):
        if event_id in approval_event_ids:
            continue
        for event in events:
            failures.append(
                {
                    "event_id": event_id,
                    "approval_record_id": str(event.get("approval_record_id") or "").strip(),
                    "approval_id": str(event.get("approval_id") or "").strip(),
                    "reason": "orphan_sync_outcome_event",
                }
            )
    return failures


def _tenant_approval_vector_sync_scope(
    approval_records: list[dict[str, Any]],
    sync_events: list[dict[str, Any]],
    *,
    tenant_id: str,
    tenant_storage_isolation: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if tenant_storage_isolation:
        return approval_records, sync_events
    selected_approvals = [
        record for record in approval_records if str(record.get("tenant_id") or "").strip() == tenant_id
    ]
    referenced_event_ids = {
        str(record.get("vector_sync_event_id") or "").strip()
        for record in selected_approvals
        if str(record.get("vector_sync_event_id") or "").strip()
    }
    selected_events = [
        event
        for event in sync_events
        if str(event.get("tenant_id") or "").strip() == tenant_id
        or str(event.get("event_id") or "").strip() in referenced_event_ids
    ]
    return selected_approvals, selected_events


def _has_matching_approval_journal_record(
    record: dict[str, Any],
    metadata: dict[str, Any],
    approval_records: list[dict[str, Any]],
) -> bool:
    document_id = str(record.get("document_id") or metadata.get("document_id") or "").strip()
    chunk_id = str(record.get("chunk_id") or metadata.get("chunk_id") or "").strip()
    tenant_id = str(record.get("tenant_id") or metadata.get("tenant_id") or "default").strip()
    approval_id = str(metadata.get("approval_id") or "").strip()
    approved_hash = str(metadata.get("approved_content_hash") or "").strip()
    if not all((document_id, chunk_id, tenant_id, approval_id, approved_hash)):
        return False
    for approval in approval_records:
        if str(approval.get("document_id") or "").strip() != document_id:
            continue
        if str(approval.get("tenant_id") or "").strip() != tenant_id:
            continue
        if str(approval.get("approval_id") or "").strip() != approval_id:
            continue
        if chunk_id not in {str(value).strip() for value in approval.get("chunk_ids") or []}:
            continue
        if _approval_record_chunk_hash(approval, chunk_id) != approved_hash:
            continue
        evidence_metadata = _approval_worklist_metadata(approval.get("worklist_evidence"))
        if set(evidence_metadata) != set(APPROVAL_WORKLIST_EVIDENCE_TO_METADATA.values()):
            continue
        if any(str(metadata.get(key) or "").strip() != str(value or "").strip() for key, value in evidence_metadata.items()):
            continue
        return True
    return False


def _approval_record_chunk_hash(record: dict[str, Any], chunk_id: str) -> str:
    hashes = record.get("approved_content_hashes")
    if isinstance(hashes, dict):
        value = hashes.get(chunk_id)
        if value:
            return str(value).strip()
    for snapshot in record.get("approved_chunks") or []:
        if not isinstance(snapshot, dict):
            continue
        if str(snapshot.get("chunk_id") or "").strip() == chunk_id and snapshot.get("approved_content_hash"):
            return str(snapshot.get("approved_content_hash") or "").strip()
    return ""


def _approval_worklist_metadata(value: Any) -> dict[str, str]:
    evidence = value if isinstance(value, dict) else {}
    return {
        metadata_key: str(evidence.get(evidence_key) or "").strip()
        for evidence_key, metadata_key in APPROVAL_WORKLIST_EVIDENCE_TO_METADATA.items()
        if str(evidence.get(evidence_key) or "").strip()
    }


def _indexing_job_failures(records: list[dict[str, Any]], indexing_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed_documents = {
        str(job.get("document_id") or "")
        for job in indexing_jobs
        if str(job.get("status") or "") == "indexed" and int(job.get("record_count") or 0) > 0
    }
    vector_documents = sorted(
        {
            str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "")
            for record in records
            if record.get("document_id") or (record.get("metadata") or {}).get("document_id")
        }
    )
    return [
        {"document_id": document_id, "reason": "missing_indexing_job"}
        for document_id in vector_documents
        if document_id not in indexed_documents
    ]


def _vector_store_failures(records: list[dict[str, Any]], indexing_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expected_records_by_document = {
        str(job.get("document_id") or ""): int(job.get("record_count") or 0)
        for job in indexing_jobs
        if str(job.get("status") or "") == "indexed" and int(job.get("record_count") or 0) > 0
    }
    if not expected_records_by_document:
        return []
    actual_records_by_document = Counter(
        str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "")
        for record in records
        if record.get("document_id") or (record.get("metadata") or {}).get("document_id")
    )
    failures: list[dict[str, Any]] = []
    for document_id, expected_count in sorted(expected_records_by_document.items()):
        actual_count = int(actual_records_by_document.get(document_id) or 0)
        if actual_count <= 0:
            failures.append(
                {
                    "document_id": document_id,
                    "reason": "indexed_document_missing_vector_records",
                    "expected_record_count": expected_count,
                    "actual_record_count": actual_count,
                }
            )
        elif actual_count < expected_count:
            failures.append(
                {
                    "document_id": document_id,
                    "reason": "indexed_document_vector_record_shortfall",
                    "expected_record_count": expected_count,
                    "actual_record_count": actual_count,
                }
            )
    return failures


def _audit_control_failures(api_audit_action_counts: dict[str, int], rag_traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures = [
        {"action": action, "reason": "missing_api_audit_action"}
        for action in ("document.review.approve", "document.index")
        if int(api_audit_action_counts.get(action) or 0) <= 0
    ]
    if int(api_audit_action_counts.get("rag.search") or 0) <= 0 and int(api_audit_action_counts.get("mcp.search") or 0) <= 0:
        failures.append({"action": "rag.search_or_mcp.search", "reason": "missing_api_audit_action"})
    if not rag_traces:
        failures.append({"action": "rag.trace", "reason": "missing_rag_trace"})
    return failures


def _component_manifest(settings: Settings) -> dict[str, Any]:
    source_paths = [
        "app/api/routes_documents.py",
        "app/api/routes_rag.py",
        "app/core/security.py",
        "app/ingestion/vector_adapter.py",
        "app/ingestion/vector_integrity.py",
        "app/ingestion/vector_upsert.py",
        "app/mcp_server/regulation_server.py",
        "app/mcp_server/regulation_tools.py",
        "app/rag/local_llm.py",
        "app/rag/output_filter.py",
        "app/storage/repository.py",
        "scripts/build_rag_security_evidence.py",
        "scripts/run_regulation_mcp.py",
    ]
    endpoint_host = urlparse(str(settings.rag_llm_endpoint or "")).hostname or ""
    return {
        "manifest_type": "rag_component_manifest",
        "manifest_version": 1,
        "embedding_model": LOCAL_HASH_EMBEDDING_MODEL,
        "vector_store_target": "local-jsonl",
        "rag_llm_backend": str(settings.rag_llm_backend or "extractive").strip().lower(),
        "rag_llm_model": str(settings.rag_llm_model or ""),
        "rag_llm_endpoint_host": endpoint_host,
        "source_files": [
            {
                "path": rel_path,
                "exists": (PROJECT_ROOT / rel_path).is_file(),
                "sha256": _sha256_file(PROJECT_ROOT / rel_path) if (PROJECT_ROOT / rel_path).is_file() else "",
            }
            for rel_path in source_paths
        ],
    }


def _local_llm_runtime_probe(settings: Settings) -> dict[str, Any]:
    backend = str(settings.rag_llm_backend or "extractive").strip().lower()
    if backend == "extractive":
        return {"checked": False, "available": False, "backend": backend}
    if backend not in {"ollama", "llama-cpp", "openai-compatible"}:
        return {"checked": True, "available": False, "backend": backend, "error_type": "UnsupportedBackend"}
    return probe_local_llm(settings)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_json_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a local RAG security evidence report.")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--tenant-id", default="default")
    storage = parser.add_mutually_exclusive_group()
    storage.add_argument("--tenant-storage-isolation", action="store_true")
    storage.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--rag-llm-backend", default=None)
    parser.add_argument("--rag-llm-endpoint", default=None)
    parser.add_argument("--rag-llm-model", default=None)
    parser.add_argument("--rag-llm-timeout-seconds", type=int, default=None)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    tenant_storage_isolation = None
    if args.tenant_storage_isolation:
        tenant_storage_isolation = True
    if args.flat_storage:
        tenant_storage_isolation = False
    report = build_rag_security_evidence(
        data_dir=Path(args.data_dir),
        tenant_id=args.tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
        rag_llm_backend=args.rag_llm_backend,
        rag_llm_endpoint=args.rag_llm_endpoint,
        rag_llm_model=args.rag_llm_model,
        rag_llm_timeout_seconds=args.rag_llm_timeout_seconds,
        out_json=Path(args.out_json) if args.out_json else None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.fail_on_issue and not report["passed"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
