from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import sys
from pathlib import Path
from typing import Any, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.api import routes_documents, routes_rag
from app.core.config import Settings
from app.core.security import AuthContext
from app.core.tenant_access import settings_for_tenant
from app.ingestion.vector_adapter import build_vector_records
from app.storage.repository import JsonRepository
from scripts.report_metadata import current_repo_commit


APPROVAL_PROVENANCE_FIELDS = (
    "approval_id",
    "approved_content_hash",
    "approval_worklist_report_path",
    "approval_worklist_report_sha256",
    "approval_review_batch_manifest_path",
    "approval_review_batch_manifest_sha256",
    "approval_review_batch_id",
    "approval_review_batch_chunk_fingerprint",
    "approval_review_strategy",
)
APPROVAL_WORKLIST_EVIDENCE_TO_METADATA = {
    "worklist_report_path": "approval_worklist_report_path",
    "worklist_report_sha256": "approval_worklist_report_sha256",
    "review_batch_manifest_path": "approval_review_batch_manifest_path",
    "review_batch_manifest_sha256": "approval_review_batch_manifest_sha256",
    "review_batch_id": "approval_review_batch_id",
    "review_batch_chunk_fingerprint": "approval_review_batch_chunk_fingerprint",
    "review_strategy": "approval_review_strategy",
}


@dataclass(frozen=True)
class VisibilityFinding:
    severity: str
    code: str
    detail: str
    remediation: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def audit_mcp_index_visibility(
    *,
    data_dir: str | Path,
    tenant_id: str,
    profile_id: str | None = None,
    tenant_storage_isolation: bool = False,
    min_visible_records: int = 1,
    forbid_smoke_docs: bool = False,
    require_indexed: bool = False,
    source_system: str | None = None,
    apba_id: str | None = None,
    role: str = "operator",
    department_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    settings = Settings(data_dir=Path(data_dir), tenant_storage_isolation=tenant_storage_isolation)
    tenant_settings = settings_for_tenant(settings, tenant_id)
    repository = JsonRepository(tenant_settings)
    normalized_role = str(role or "operator").strip().lower() or "operator"
    normalized_department_ids = tuple(str(item).strip() for item in (department_ids or []) if str(item).strip())
    auth = AuthContext(
        actor="mcp-index-visibility-audit",
        tenant_id=tenant_id,
        auth_mode="local",
        role=normalized_role,
        department_ids=normalized_department_ids,
    )

    findings: list[VisibilityFinding] = []
    documents: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    total_visible_records = 0
    total_indexable_records = 0
    total_approved_chunks = 0
    total_skipped_unapproved = 0
    approval_status_totals: Counter[str] = Counter()
    smoke_like_count = 0
    aggregate_hwpx_metadata_counts: Counter[str] = Counter()
    aggregate_hwp_metadata_counts: Counter[str] = Counter()
    source_system_counts: Counter[str] = Counter()
    apba_id_counts: Counter[str] = Counter()
    public_portal_missing_apba_id_count = 0

    filters = {
        "profile_id": _normalized_filter(profile_id),
        "source_system": _normalized_filter(source_system),
        "apba_id": _normalized_filter(apba_id),
    }
    for document in repository.list_documents():
        chunks = repository.get_chunks(document.document_id)
        if not _document_matches_filters(document, chunks, filters):
            continue
        document_source_system = _metadata_value(document, chunks, "source_system")
        document_apba_id = _metadata_value(document, chunks, "apba_id")
        source_system_counts[document_source_system or "missing"] += 1
        apba_id_counts[document_apba_id or "missing"] += 1
        if document_source_system.upper() == "PUBLIC_PORTAL" and not document_apba_id:
            public_portal_missing_apba_id_count += 1
        approval_counts = Counter(chunk.approval_status for chunk in chunks)
        approval_status_totals.update(approval_counts)
        total_approved_chunks += approval_counts.get("approved", 0)
        visibility = _document_visibility_summary(
            settings=tenant_settings,
            repository=repository,
            document_id=document.document_id,
            auth=auth,
            department_ids=normalized_department_ids,
        )
        indexing_status = str(visibility.get("indexing_status") or "unknown")
        status_counts[indexing_status] += 1
        vector_summary = visibility.get("vector_summary") if isinstance(visibility.get("vector_summary"), dict) else {}
        vector_consistency = (
            visibility.get("vector_consistency") if isinstance(visibility.get("vector_consistency"), dict) else {}
        )
        parser_uncertainty_summary = (
            visibility.get("parser_uncertainty_summary")
            if isinstance(visibility.get("parser_uncertainty_summary"), dict)
            else {}
        )
        approval_provenance_coverage = (
            visibility.get("approval_provenance_coverage")
            if isinstance(visibility.get("approval_provenance_coverage"), dict)
            else {}
        )
        approval_journal_coverage = (
            visibility.get("approval_journal_coverage")
            if isinstance(visibility.get("approval_journal_coverage"), dict)
            else {}
        )
        hwpx_metadata_counts = _int_count_dict(vector_summary.get("hwpx_metadata_counts"))
        hwp_metadata_counts = _int_count_dict(vector_summary.get("hwp_metadata_counts"))
        aggregate_hwpx_metadata_counts.update(hwpx_metadata_counts)
        aggregate_hwp_metadata_counts.update(hwp_metadata_counts)
        indexable_record_count = int(vector_summary.get("record_count") or 0)
        visible_record_count = int(visibility.get("actual_mcp_visible_record_count") or 0)
        skipped_unapproved = int(vector_summary.get("skipped_unapproved_count") or 0)
        stale_count = int(vector_consistency.get("stale_count") or 0)
        total_indexable_records += indexable_record_count
        total_visible_records += visible_record_count
        total_skipped_unapproved += skipped_unapproved
        smoke_like = _is_smoke_like_document(document, chunks)
        if smoke_like:
            smoke_like_count += 1
        missing_parser_uncertainty_count = int(
            parser_uncertainty_summary.get("missing_parser_uncertainty_count") or 0
        )
        if indexable_record_count and parser_uncertainty_summary.get("parser_uncertainty_record_count") and missing_parser_uncertainty_count:
            findings.append(
                VisibilityFinding(
                    "medium",
                    "parser-uncertainty-metadata-missing",
                    (
                        f"{document.document_id} has {missing_parser_uncertainty_count} indexed record(s) "
                        "without explicit parser uncertainty metadata."
                    ),
                    "Regenerate vectors from the current parser output and preserve uncertainty review flags before handoff.",
                )
            )
        row = {
            "document_id": document.document_id,
            "filename": document.filename,
            "document_name": document.document_name,
            "institution_name": document.institution_name,
            "apba_id": document_apba_id or document.apba_id,
            "tenant_id": document.tenant_id,
            "source_system": document_source_system or document.source_system,
            "source_record_id": document.source_record_id,
            "approval_status_counts": dict(sorted(approval_counts.items())),
            "indexing_status": indexing_status,
            "indexable_record_count": indexable_record_count,
            "mcp_visible_record_count": visible_record_count,
            "skipped_unapproved_count": skipped_unapproved,
            "stale_count": stale_count,
            "hwpx_metadata_counts": hwpx_metadata_counts,
            "hwp_metadata_counts": hwp_metadata_counts,
            "parser_uncertainty_summary": parser_uncertainty_summary,
            "approval_provenance_coverage": approval_provenance_coverage,
            "approval_journal_coverage": approval_journal_coverage,
            "latest_job_record_count": (visibility.get("latest_job") or {}).get("record_count"),
            "validation_error": visibility.get("validation_error"),
            "mcp_visibility_error": visibility.get("mcp_visibility_error"),
            "smoke_like": smoke_like,
        }
        documents.append(row)
        if indexable_record_count > visible_record_count:
            findings.append(
                VisibilityFinding(
                    "medium",
                    "mcp-scope-hidden-records",
                    (
                        f"{document.document_id} has {indexable_record_count} approved/indexable record(s), "
                        f"but {visible_record_count} are visible for role={normalized_role} "
                        f"department_ids={list(normalized_department_ids)}."
                    ),
                    "Run the audit with the same role and department scope used by the MCP client, or review the document ACLs.",
                )
            )
        if require_indexed and indexing_status != "indexed":
            findings.append(
                VisibilityFinding(
                    "high",
                    "document-not-indexed",
                    f"{document.document_id} status is {indexing_status}.",
                    "Approve intended chunks and run index or reindex before connecting the MCP client.",
                )
            )
        if stale_count:
            findings.append(
                VisibilityFinding(
                    "high",
                    "stale-vector-records",
                    f"{document.document_id} has {stale_count} stale vector records.",
                    "Run Reindex approved chunks with the same data-dir and tenant-id.",
                )
            )
        if visibility.get("validation_error"):
            findings.append(
                VisibilityFinding(
                    "high",
                    "index-validation-error",
                    f"{document.document_id}: {visibility['validation_error']}",
                    "Fix approved chunk metadata, approval hashes, or security levels before indexing.",
                )
            )
        if visibility.get("mcp_visibility_error"):
            findings.append(
                VisibilityFinding(
                    "high",
                    "mcp-visibility-validation-error",
                    f"{document.document_id}: {visibility['mcp_visibility_error']}",
                    "Fix the requested MCP role/department scope or malformed vector records before using this runtime.",
                )
            )
        if int(approval_journal_coverage.get("missing_record_count") or 0) > 0:
            findings.append(
                VisibilityFinding(
                    "high",
                    "approval-journal-evidence-missing",
                    (
                        f"{document.document_id} has "
                        f"{approval_journal_coverage.get('missing_record_count')} approved vector record(s) "
                        "without matching append-only approval journal evidence."
                    ),
                    "Approve through the review workflow and reindex; do not rely on edited chunk/vector metadata.",
                )
            )

    if not documents:
        findings.append(
            VisibilityFinding(
                "high",
                "no-documents",
                "No documents were found in the effective tenant runtime.",
                "Check --data-dir, --tenant-id, and --tenant-storage-isolation.",
            )
        )
    if total_visible_records < min_visible_records:
        findings.append(
            VisibilityFinding(
                "high",
                "too-few-visible-records",
                f"MCP-visible record count is {total_visible_records}, below required {min_visible_records}.",
                "Verify the same runtime was used for preprocessing, approval, indexing, and MCP startup.",
            )
        )
    if forbid_smoke_docs and smoke_like_count:
        findings.append(
            VisibilityFinding(
                "high",
                "smoke-documents-visible",
                f"{smoke_like_count} smoke-test-like document(s) are visible in the runtime.",
                "Use the production runtime data-dir and tenant, not a smoke-test fixture runtime.",
            )
        )
    if public_portal_missing_apba_id_count:
        findings.append(
            VisibilityFinding(
                "medium",
                "public_portal-apba-id-missing",
                f"{public_portal_missing_apba_id_count} PUBLIC_PORTAL document(s) are missing apba_id in persisted document/chunk metadata.",
                "Reprocess or migrate the runtime with apba_id persisted before using per-institution PUBLIC_PORTAL visibility evidence.",
            )
        )

    severity_counts = Counter(finding.severity for finding in findings)
    parser_evidence_summary = _parser_evidence_summary(
        documents=documents,
        hwpx_metadata_counts=aggregate_hwpx_metadata_counts,
        hwp_metadata_counts=aggregate_hwp_metadata_counts,
    )
    parser_uncertainty_summary = _aggregate_parser_uncertainty_summary(documents)
    approval_provenance_coverage = _aggregate_approval_provenance_coverage(documents)
    approval_journal_coverage = _aggregate_approval_journal_coverage(documents)
    preapproval_visibility_guard = _preapproval_visibility_guard(
        approved_chunks=total_approved_chunks,
        visible_records=total_visible_records,
        skipped_unapproved=total_skipped_unapproved,
    )
    source_identity_summary = {
        "source_system_counts": dict(sorted(source_system_counts.items())),
        "apba_id_counts": dict(sorted(apba_id_counts.items())),
        "public_portal_missing_apba_id_count": public_portal_missing_apba_id_count,
    }
    return {
        "report_type": "mcp_index_visibility_audit",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "data_dir": str(Path(data_dir)),
        "effective_data_dir": str(tenant_settings.data_dir),
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "tenant_storage_isolation": tenant_storage_isolation,
        "filters": {key: value for key, value in filters.items() if value},
        "auth_scope": {
            "role": normalized_role,
            "department_ids": list(normalized_department_ids),
        },
        "document_count": len(documents),
        "approval_status_totals": dict(sorted(approval_status_totals.items())),
        "total_approved_chunks": total_approved_chunks,
        "total_indexable_record_count": total_indexable_records,
        "total_mcp_visible_records": total_visible_records,
        "total_skipped_unapproved_count": total_skipped_unapproved,
        "status_counts": dict(sorted(status_counts.items())),
        "smoke_like_document_count": smoke_like_count,
        "preapproval_visibility_guard": preapproval_visibility_guard,
        "source_identity_summary": source_identity_summary,
        "parser_evidence_summary": parser_evidence_summary,
        "parser_uncertainty_summary": parser_uncertainty_summary,
        "approval_provenance_coverage": approval_provenance_coverage,
        "approval_journal_coverage": approval_journal_coverage,
        "documents": documents,
        "finding_count": len(findings),
        "severity_counts": dict(sorted(severity_counts.items())),
        "findings": [finding.to_dict() for finding in findings],
        "passed": not any(finding.severity == "high" for finding in findings),
    }


def _preapproval_visibility_guard(*, approved_chunks: int, visible_records: int, skipped_unapproved: int) -> dict[str, Any]:
    if approved_chunks > 0:
        status = "approved_runtime"
        passed = True
    elif visible_records == 0:
        status = "no_approved_chunks_no_visible_records"
        passed = True
    else:
        status = "visible_records_without_approved_chunks"
        passed = False
    return {
        "passed": passed,
        "status": status,
        "approved_chunks": approved_chunks,
        "mcp_visible_records": visible_records,
        "skipped_unapproved_count": skipped_unapproved,
    }


def _normalized_filter(value: str | None) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _document_matches_filters(document: Any, chunks: Sequence[Any], filters: dict[str, str | None]) -> bool:
    profile_id = filters.get("profile_id")
    source_system = filters.get("source_system")
    apba_id = filters.get("apba_id")
    if profile_id and _metadata_value(document, chunks, "profile_id").casefold() != profile_id.casefold():
        return False
    if source_system and _metadata_value(document, chunks, "source_system").upper() != source_system.upper():
        return False
    if apba_id and _metadata_value(document, chunks, "apba_id") != apba_id:
        return False
    return True


def _metadata_value(document: Any, chunks: Sequence[Any], key: str) -> str:
    document_value = getattr(document, key, None)
    if document_value:
        return str(document_value)
    for chunk in chunks:
        metadata = getattr(chunk, "metadata", {}) or {}
        if metadata.get(key):
            return str(metadata[key])
    return ""


def write_markdown_report(report: dict[str, Any], path: Path) -> None:
    parser_evidence = report.get("parser_evidence_summary") if isinstance(report.get("parser_evidence_summary"), dict) else {}
    parser_uncertainty = (
        report.get("parser_uncertainty_summary")
        if isinstance(report.get("parser_uncertainty_summary"), dict)
        else {}
    )
    approval_provenance = (
        report.get("approval_provenance_coverage")
        if isinstance(report.get("approval_provenance_coverage"), dict)
        else {}
    )
    preapproval_guard = (
        report.get("preapproval_visibility_guard")
        if isinstance(report.get("preapproval_visibility_guard"), dict)
        else {}
    )
    source_identity = (
        report.get("source_identity_summary")
        if isinstance(report.get("source_identity_summary"), dict)
        else {}
    )
    lines = [
        "# MCP Index Visibility Audit",
        "",
        f"- Passed: `{report.get('passed')}`",
        f"- Data dir: `{report.get('data_dir')}`",
        f"- Effective data dir: `{report.get('effective_data_dir')}`",
        f"- Tenant: `{report.get('tenant_id')}`",
        f"- Filters: `{report.get('filters') or {}}`",
        f"- Auth scope: `{report.get('auth_scope') or {}}`",
        f"- Documents: `{report.get('document_count')}`",
        f"- Approval status totals: `{report.get('approval_status_totals')}`",
        f"- Approved/indexable records: `{report.get('total_indexable_record_count')}`",
        f"- MCP-visible records: `{report.get('total_mcp_visible_records')}`",
        f"- Approved chunks: `{report.get('total_approved_chunks')}`",
        f"- Skipped unapproved chunks: `{report.get('total_skipped_unapproved_count')}`",
        f"- Pre-approval visibility guard: `{preapproval_guard.get('status')}` (`passed={preapproval_guard.get('passed')}`)",
        f"- Source system counts: `{source_identity.get('source_system_counts') or {}}`",
        f"- PUBLIC_PORTAL missing apba_id documents: `{source_identity.get('public_portal_missing_apba_id_count', 0)}`",
        f"- Smoke-like documents: `{report.get('smoke_like_document_count')}`",
        f"- HWPX parser evidence documents: `{parser_evidence.get('hwpx_evidence_document_count', 0)}`",
        f"- HWP extraction-mode documents: `{parser_evidence.get('hwp_extraction_mode_document_count', 0)}`",
        f"- HWP native-geometry review documents: `{parser_evidence.get('hwp_native_table_geometry_review_document_count', 0)}`",
        f"- Parser uncertainty records: `{parser_uncertainty.get('parser_uncertainty_record_count', 0)}` / missing `{parser_uncertainty.get('missing_parser_uncertainty_count', 0)}`",
        f"- Parser uncertainty risk counts: `{parser_uncertainty.get('risk_level_counts') or {}}`",
        f"- Approval provenance coverage: complete `{approval_provenance.get('complete_record_count', 0)}` / `{approval_provenance.get('record_count', 0)}`",
        f"- Approval provenance missing counts: `{approval_provenance.get('missing_field_counts') or {}}`",
        "",
        "## Documents",
        "",
        "| document_id | name | status | approved | indexable | visible | stale | hwpx_evidence | hwp_mode | hwp_geometry_review | uncertainty | provenance_complete | smoke_like |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in report.get("documents") or []:
        approval_counts = row.get("approval_status_counts") or {}
        name = row.get("document_name") or row.get("filename") or ""
        hwpx_counts = row.get("hwpx_metadata_counts") if isinstance(row.get("hwpx_metadata_counts"), dict) else {}
        hwp_counts = row.get("hwp_metadata_counts") if isinstance(row.get("hwp_metadata_counts"), dict) else {}
        row_parser_uncertainty = (
            row.get("parser_uncertainty_summary")
            if isinstance(row.get("parser_uncertainty_summary"), dict)
            else {}
        )
        row_approval_provenance = (
            row.get("approval_provenance_coverage")
            if isinstance(row.get("approval_provenance_coverage"), dict)
            else {}
        )
        lines.append(
            "| {document_id} | {name} | {status} | {approved} | {indexable} | {visible} | {stale} | {hwpx} | {hwp_mode} | {hwp_review} | {uncertainty} | {provenance_complete} | {smoke} |".format(
                document_id=row.get("document_id") or "",
                name=str(name).replace("|", "\\|"),
                status=row.get("indexing_status") or "",
                approved=approval_counts.get("approved", 0),
                indexable=row.get("indexable_record_count", 0),
                visible=row.get("mcp_visible_record_count", 0),
                stale=row.get("stale_count", 0),
                hwpx=sum(int(value or 0) for value in hwpx_counts.values()),
                hwp_mode=hwp_counts.get("source_hwp_extraction_modes", 0),
                hwp_review=hwp_counts.get("source_hwp_native_table_geometry_false", 0),
                uncertainty=row_parser_uncertainty.get("parser_uncertainty_record_count", 0),
                provenance_complete=row_approval_provenance.get("complete_record_count", 0),
                smoke=row.get("smoke_like"),
            )
        )
    if report.get("findings"):
        lines.extend(["", "## Findings", ""])
        for finding in report["findings"]:
            lines.append(f"- `{finding['severity']}` `{finding['code']}`: {finding['detail']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _document_visibility_summary(
    *,
    settings: Settings,
    repository: JsonRepository,
    document_id: str,
    auth: AuthContext,
    department_ids: Sequence[str] = (),
) -> dict[str, Any]:
    document = repository.get_document(document_id)
    chunks = repository.get_chunks(document_id)
    validation_error = None
    try:
        current_records, vector_summary = build_vector_records(
            routes_documents._chunks_for_indexing(chunks, document, auth) if document else []
        )
    except Exception as exc:
        current_records = []
        vector_summary = {"record_count": 0}
        validation_error = str(getattr(exc, "detail", exc))
    jobs = repository.list_indexing_jobs(document_id)
    latest_job = jobs[-1] if jobs else None
    indexing_status = "review_required" if validation_error else (latest_job.get("status") if latest_job else "not_indexed")
    vector_consistency = {"checked": False, "stale_count": 0, "samples": []}
    if latest_job and latest_job.get("status") == "indexed" and not validation_error:
        if int(latest_job.get("record_count") or 0) != int(vector_summary.get("record_count") or 0):
            indexing_status = "reindex_required"
        vector_consistency = routes_documents._vector_consistency_summary(
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
    actual_visible_record_count = 0
    mcp_visibility_error = None
    try:
        actual_visible_record_count = _actual_mcp_visible_record_count(
            settings=settings,
            repository=repository,
            document_id=document_id,
            auth=auth,
            department_ids=department_ids,
        )
    except Exception as exc:
        mcp_visibility_error = str(getattr(exc, "detail", exc))
    return {
        "document_id": document_id,
        "indexing_status": indexing_status,
        "latest_job": latest_job,
        "job_count": len(jobs),
        "vector_summary": vector_summary,
        "parser_uncertainty_summary": _parser_uncertainty_summary_from_records(current_records),
        "approval_provenance_coverage": _approval_provenance_coverage_from_records(current_records),
        "approval_journal_coverage": _approval_journal_coverage_from_records(
            current_records,
            repository.list_approval_journal_records(document_id),
        ),
        "vector_consistency": vector_consistency,
        "validation_error": validation_error,
        "actual_mcp_visible_record_count": actual_visible_record_count,
        "mcp_visibility_error": mcp_visibility_error,
    }


def _actual_mcp_visible_record_count(
    *,
    settings: Settings,
    repository: JsonRepository,
    document_id: str,
    auth: AuthContext,
    department_ids: Sequence[str],
) -> int:
    request = routes_rag.RagSearchRequest(
        query="mcp visibility audit",
        top_k=20,
        department_ids=list(department_ids),
        document_id=document_id,
    )
    routes_rag._validate_security_scope(request, auth)
    requested_department_ids = routes_rag._requested_department_ids(request, auth)
    records = routes_rag._load_local_vector_records(settings, auth)
    repository_cache = routes_rag._RagRequestRepositoryCache(repository)
    approval_snapshot = routes_rag._load_cached_approval_snapshot(repository, records, auth)
    return sum(
        1
        for record in records
        if routes_rag._record_visible_to_request(
            record,
            request=request,
            auth=auth,
            repository=repository,
            repository_cache=repository_cache,
            approval_snapshot=approval_snapshot,
            requested_department_ids=requested_department_ids,
        )
    )


def _is_smoke_like_document(document: Any, chunks: Sequence[Any]) -> bool:
    fields = [
        getattr(document, "document_id", ""),
        getattr(document, "filename", ""),
        getattr(document, "document_name", ""),
        getattr(document, "source_system", ""),
        getattr(document, "source_record_id", ""),
    ]
    for chunk in chunks[:10]:
        fields.append(getattr(chunk, "chunk_id", ""))
        metadata = getattr(chunk, "metadata", {}) or {}
        fields.extend(str(metadata.get(key) or "") for key in ("source_system", "source_record_id", "approval_id"))
    return any("smoke" in str(value).lower() for value in fields if value is not None)


def _int_count_dict(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    counts: dict[str, int] = {}
    for key, raw_count in value.items():
        try:
            count = int(raw_count or 0)
        except (TypeError, ValueError):
            continue
        counts[str(key)] = count
    return counts


def _parser_uncertainty_summary_from_records(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    risk_level_counts: Counter[str] = Counter()
    flag_counts: Counter[str] = Counter()
    parser_uncertainty_record_count = 0
    for record in records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        nested = metadata.get("parser_uncertainty") if isinstance(metadata.get("parser_uncertainty"), dict) else {}
        risk_level = str(
            metadata.get("parser_uncertainty_risk_level") or nested.get("risk_level") or ""
        ).strip().lower()
        flags = metadata.get("parser_uncertainty_flags")
        if flags is None:
            flags = nested.get("flags")
        normalized_flags = _normalized_list(flags)
        has_uncertainty = bool(risk_level or normalized_flags or nested)
        if not has_uncertainty:
            continue
        parser_uncertainty_record_count += 1
        if risk_level:
            risk_level_counts[risk_level] += 1
        flag_counts.update(normalized_flags)
    record_count = len(records)
    return {
        "record_count": record_count,
        "parser_uncertainty_record_count": parser_uncertainty_record_count,
        "missing_parser_uncertainty_count": max(record_count - parser_uncertainty_record_count, 0),
        "risk_level_counts": dict(sorted(risk_level_counts.items())),
        "flag_counts": dict(sorted(flag_counts.items())),
    }


def _approval_provenance_coverage_from_records(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    field_counts: Counter[str] = Counter()
    complete_record_count = 0
    for record in records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        present_fields = {
            field
            for field in APPROVAL_PROVENANCE_FIELDS
            if str(metadata.get(field) or "").strip()
        }
        field_counts.update(present_fields)
        if len(present_fields) == len(APPROVAL_PROVENANCE_FIELDS):
            complete_record_count += 1
    record_count = len(records)
    normalized_field_counts = {field: int(field_counts.get(field, 0)) for field in APPROVAL_PROVENANCE_FIELDS}
    return {
        "record_count": record_count,
        "field_counts": normalized_field_counts,
        "missing_field_counts": {
            field: max(record_count - count, 0) for field, count in normalized_field_counts.items()
        },
        "complete_record_count": complete_record_count,
    }


def _approval_journal_coverage_from_records(
    records: Sequence[dict[str, Any]],
    approval_journal_records: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    eligible_record_count = 0
    matched_record_count = 0
    missing_sample_record_ids: list[str] = []
    for record in records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        if not all(str(metadata.get(field) or "").strip() for field in APPROVAL_PROVENANCE_FIELDS):
            continue
        eligible_record_count += 1
        if _has_matching_approval_journal_record(record, metadata, approval_journal_records):
            matched_record_count += 1
            continue
        if len(missing_sample_record_ids) < 20:
            missing_sample_record_ids.append(str(record.get("id") or f"{metadata.get('document_id')}:{metadata.get('chunk_id')}"))
    missing_record_count = max(eligible_record_count - matched_record_count, 0)
    return {
        "journal_record_count": len(approval_journal_records),
        "record_count": len(records),
        "eligible_record_count": eligible_record_count,
        "matched_record_count": matched_record_count,
        "missing_record_count": missing_record_count,
        "missing_sample_record_ids": missing_sample_record_ids,
    }


def _has_matching_approval_journal_record(
    record: dict[str, Any],
    metadata: dict[str, Any],
    approval_records: Sequence[dict[str, Any]],
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


def _aggregate_parser_uncertainty_summary(documents: Sequence[dict[str, Any]]) -> dict[str, Any]:
    record_count = 0
    parser_uncertainty_record_count = 0
    missing_parser_uncertainty_count = 0
    risk_level_counts: Counter[str] = Counter()
    flag_counts: Counter[str] = Counter()
    for row in documents:
        summary = row.get("parser_uncertainty_summary") if isinstance(row.get("parser_uncertainty_summary"), dict) else {}
        record_count += int(summary.get("record_count") or 0)
        parser_uncertainty_record_count += int(summary.get("parser_uncertainty_record_count") or 0)
        missing_parser_uncertainty_count += int(summary.get("missing_parser_uncertainty_count") or 0)
        risk_level_counts.update(_int_count_dict(summary.get("risk_level_counts")))
        flag_counts.update(_int_count_dict(summary.get("flag_counts")))
    return {
        "record_count": record_count,
        "parser_uncertainty_record_count": parser_uncertainty_record_count,
        "missing_parser_uncertainty_count": missing_parser_uncertainty_count,
        "risk_level_counts": dict(sorted(risk_level_counts.items())),
        "flag_counts": dict(sorted(flag_counts.items())),
    }


def _aggregate_approval_provenance_coverage(documents: Sequence[dict[str, Any]]) -> dict[str, Any]:
    record_count = 0
    complete_record_count = 0
    field_counts: Counter[str] = Counter()
    for row in documents:
        coverage = row.get("approval_provenance_coverage") if isinstance(row.get("approval_provenance_coverage"), dict) else {}
        record_count += int(coverage.get("record_count") or 0)
        complete_record_count += int(coverage.get("complete_record_count") or 0)
        field_counts.update(_int_count_dict(coverage.get("field_counts")))
    normalized_field_counts = {field: int(field_counts.get(field, 0)) for field in APPROVAL_PROVENANCE_FIELDS}
    return {
        "record_count": record_count,
        "field_counts": normalized_field_counts,
        "missing_field_counts": {
            field: max(record_count - count, 0) for field, count in normalized_field_counts.items()
        },
        "complete_record_count": complete_record_count,
    }


def _aggregate_approval_journal_coverage(documents: Sequence[dict[str, Any]]) -> dict[str, Any]:
    record_count = 0
    journal_record_count = 0
    eligible_record_count = 0
    matched_record_count = 0
    missing_record_count = 0
    missing_sample_record_ids: list[str] = []
    for row in documents:
        coverage = row.get("approval_journal_coverage") if isinstance(row.get("approval_journal_coverage"), dict) else {}
        record_count += int(coverage.get("record_count") or 0)
        journal_record_count += int(coverage.get("journal_record_count") or 0)
        eligible_record_count += int(coverage.get("eligible_record_count") or 0)
        matched_record_count += int(coverage.get("matched_record_count") or 0)
        missing_record_count += int(coverage.get("missing_record_count") or 0)
        samples = coverage.get("missing_sample_record_ids") if isinstance(coverage.get("missing_sample_record_ids"), list) else []
        for sample in samples:
            if len(missing_sample_record_ids) < 20:
                missing_sample_record_ids.append(str(sample))
    return {
        "journal_record_count": journal_record_count,
        "record_count": record_count,
        "eligible_record_count": eligible_record_count,
        "matched_record_count": matched_record_count,
        "missing_record_count": missing_record_count,
        "missing_sample_record_ids": missing_sample_record_ids,
    }


def _normalized_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return sorted(str(item).strip() for item in value if str(item).strip())
    text = str(value).strip()
    return [text] if text else []


def _parser_evidence_summary(
    *,
    documents: Sequence[dict[str, Any]],
    hwpx_metadata_counts: Counter[str],
    hwp_metadata_counts: Counter[str],
) -> dict[str, Any]:
    hwpx_document_count = 0
    hwp_mode_document_count = 0
    hwp_geometry_review_document_count = 0
    for row in documents:
        row_hwpx_counts = row.get("hwpx_metadata_counts") if isinstance(row.get("hwpx_metadata_counts"), dict) else {}
        row_hwp_counts = row.get("hwp_metadata_counts") if isinstance(row.get("hwp_metadata_counts"), dict) else {}
        if any(int(value or 0) > 0 for value in row_hwpx_counts.values()):
            hwpx_document_count += 1
        if int(row_hwp_counts.get("source_hwp_extraction_modes") or 0) > 0:
            hwp_mode_document_count += 1
        if int(row_hwp_counts.get("source_hwp_native_table_geometry_false") or 0) > 0:
            hwp_geometry_review_document_count += 1
    return {
        "hwpx_evidence_document_count": hwpx_document_count,
        "hwp_extraction_mode_document_count": hwp_mode_document_count,
        "hwp_native_table_geometry_review_document_count": hwp_geometry_review_document_count,
        "hwpx_metadata_counts": dict(sorted(hwpx_metadata_counts.items())),
        "hwp_metadata_counts": dict(sorted(hwp_metadata_counts.items())),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit whether approved regulation records are visible to MCP clients.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="Runtime data directory.")
    parser.add_argument("--tenant-id", default="default", help="Tenant ID used by the MCP server.")
    parser.add_argument("--profile-id", default=None, help="Limit the audit to one institution profile.")
    parser.add_argument("--tenant-storage-isolation", action="store_true", help="Use tenant-isolated runtime layout.")
    parser.add_argument("--min-visible-records", type=int, default=1, help="Minimum required MCP-visible records.")
    parser.add_argument("--forbid-smoke-docs", action="store_true", help="Fail if smoke-test-like documents are visible.")
    parser.add_argument("--require-indexed", action="store_true", help="Fail if any document is not indexed.")
    parser.add_argument("--source-system", help="Limit the audit to documents/chunks from this source system.")
    parser.add_argument("--apba-id", help="Limit the audit to this PUBLIC_PORTAL apba_id.")
    parser.add_argument("--role", default="operator", help="MCP/API role used for the visibility scope.")
    parser.add_argument(
        "--department-id",
        dest="department_ids",
        action="append",
        default=[],
        help="Department ID available to the MCP/API identity. Repeat for multiple departments.",
    )
    parser.add_argument("--out-json", type=Path, help="Write JSON report to this path.")
    parser.add_argument("--out-md", type=Path, help="Write Markdown report to this path.")
    parser.add_argument("--fail-on-issue", action="store_true", help="Exit non-zero when high-severity findings exist.")
    return parser


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = audit_mcp_index_visibility(
        data_dir=args.data_dir,
        tenant_id=args.tenant_id,
        profile_id=args.profile_id,
        tenant_storage_isolation=args.tenant_storage_isolation,
        min_visible_records=args.min_visible_records,
        forbid_smoke_docs=args.forbid_smoke_docs,
        require_indexed=args.require_indexed,
        source_system=args.source_system,
        apba_id=args.apba_id,
        role=args.role,
        department_ids=args.department_ids,
    )
    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.out_md:
        write_markdown_report(report, args.out_md)
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stdout or sys.stdout)
    if args.fail_on_issue and not report["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
