from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from app.ingestion.vector_adapter import ALLOWED_SECURITY_LEVELS, APPROVED_CHUNK_STATUS, stable_content_hash
from app.schemas.chunk import Chunk
from app.services.review_decision_service import (
    APPROVAL_WORKLIST_METADATA_KEYS,
    NON_APPROVABLE_CHUNK_STATUSES,
    approval_worklist_metadata,
    approved_content_hash,
    chunk_hashes,
    department_acl_set,
)


class ReviewWorkflowError(ValueError):
    def __init__(self, detail: str, *, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class ApprovalUpdate:
    updated_chunks: list[Chunk]
    approval_id: str
    approved_by: str
    approved_at: str
    requested_security_level: str
    before_content_hashes: dict[str, str]
    approved_content_hashes: dict[str, str]
    approved_chunks: list[dict[str, Any]]
    review_attention_chunk_count: int
    review_attention_flags: list[str]
    review_attention_samples: list[dict[str, Any]]


@dataclass(frozen=True)
class RejectionUpdate:
    updated_chunks: list[Chunk]
    reviewed_by: str
    reviewed_at: str
    before_content_hashes: dict[str, str]
    after_content_hashes: dict[str, str]


@dataclass(frozen=True)
class SecurityScanUpdate:
    updated_chunks: list[Chunk]
    selected_ids: set[str]
    findings: list[dict[str, Any]]
    blocked_chunk_ids: set[str]
    rule_ids: list[str]


@dataclass(frozen=True)
class ApprovalPreconditions:
    requested_ids: set[str]
    review_attention: dict[str, list[str]]


@dataclass(frozen=True)
class ApprovalDecisionPreparation:
    requested_ids: set[str]
    worklist_evidence: dict[str, str]
    preconditions: ApprovalPreconditions
    approval_update: ApprovalUpdate


@dataclass(frozen=True)
class RejectionDecisionPreparation:
    requested_ids: set[str]
    reason: str
    rejection_update: RejectionUpdate


OFFICIAL_APPROVAL_REQUIRED_EVIDENCE_FIELDS = (
    "worklist_report_path",
    "worklist_report_sha256",
    "review_batch_manifest_path",
    "review_batch_id",
    "review_batch_chunk_fingerprint",
)


SECURITY_SCAN_RULES = (
    ("resident_registration_number", "high", re.compile(r"\b\d{6}-[1-4]\d{6}\b")),
    ("account_number", "medium", re.compile(r"\b\d{2,6}-\d{2,6}-\d{2,8}\b")),
    ("phone_number", "medium", re.compile(r"\b01[016789]-\d{3,4}-\d{4}\b")),
    (
        "prompt_injection",
        "high",
        re.compile(r"(ignore\s+(?:previous|system)\s+instructions|reveal\s+the\s+system\s+prompt)", re.IGNORECASE),
    ),
    ("local_path", "medium", re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/][^\s\"'<>`]+")),
)


REVIEW_ATTENTION_BOOL_METADATA_KEYS = (
    "review_required",
    "table_review_required",
    "manual_review_required",
    "requires_manual_review",
)
REVIEW_ATTENTION_LIST_METADATA_KEYS = (
    "review_flags",
    "table_review_flags",
    "row_quality_flags",
    "quality_flags",
)
PARSER_UNCERTAINTY_ACK_RISKS = {"medium", "high", "critical"}
REVIEW_ATTENTION_WARNING_KEYWORDS = (
    "table",
    "row",
    "ocr",
    "mojibake",
    "encoding",
    "caption",
    "footnote",
    "endnote",
    "appendix",
    "image",
)


def normalize_security_level(value: str | None) -> str:
    security_level = str(value or "").strip().lower()
    if security_level not in ALLOWED_SECURITY_LEVELS:
        allowed = ", ".join(sorted(ALLOWED_SECURITY_LEVELS))
        raise ReviewWorkflowError(f"security_level must be one of: {allowed}.")
    return security_level


def chunk_review_attention_reasons(chunk: Chunk) -> list[str]:
    metadata = chunk.metadata or {}
    reasons: list[str] = []
    for key in REVIEW_ATTENTION_BOOL_METADATA_KEYS:
        if metadata.get(key):
            reasons.append(key)
    for key in REVIEW_ATTENTION_LIST_METADATA_KEYS:
        values = metadata.get(key)
        if isinstance(values, str) and values.strip():
            reasons.append(f"{key}:{values.strip()}")
        elif isinstance(values, list):
            for value in values:
                if str(value or "").strip():
                    reasons.append(f"{key}:{str(value).strip()}")
    parser_uncertainty = metadata.get("parser_uncertainty") if isinstance(metadata.get("parser_uncertainty"), dict) else {}
    uncertainty_risk = str(
        metadata.get("parser_uncertainty_risk_level") or parser_uncertainty.get("risk_level") or ""
    ).strip().lower()
    if uncertainty_risk in PARSER_UNCERTAINTY_ACK_RISKS:
        reasons.append(f"parser_uncertainty_risk_level:{uncertainty_risk}")
        uncertainty_flags = metadata.get("parser_uncertainty_flags", parser_uncertainty.get("flags", []))
        if isinstance(uncertainty_flags, str):
            uncertainty_values = [uncertainty_flags]
        else:
            uncertainty_values = list(uncertainty_flags) if isinstance(uncertainty_flags, (list, tuple, set)) else []
        for value in uncertainty_values:
            flag = str(value or "").strip()
            if flag:
                reasons.append(f"parser_uncertainty_flags:{flag}")
        recommendation = str(
            metadata.get("parser_uncertainty_recommendation") or parser_uncertainty.get("recommendation") or ""
        ).strip()
        if recommendation and recommendation != "none":
            reasons.append(f"parser_uncertainty_recommendation:{recommendation}")
    for warning in chunk.warnings or []:
        warning_text = str(warning or "").strip()
        if warning_text and _warning_requires_review(warning_text):
            reasons.append(f"warning:{warning_text}")
    return sorted(dict.fromkeys(reasons))


def review_attention_by_chunk(chunks: Sequence[Chunk], chunk_ids: set[str]) -> dict[str, list[str]]:
    return {
        chunk.chunk_id: reasons
        for chunk in chunks
        if chunk.chunk_id in chunk_ids
        for reasons in [chunk_review_attention_reasons(chunk)]
        if reasons
    }


def require_chunk_ids(chunks: Sequence[Chunk], chunk_ids: Sequence[str]) -> set[str]:
    requested_ids = set(chunk_ids)
    if not requested_ids:
        raise ReviewWorkflowError("chunk_ids is required.", status_code=400)
    existing_ids = {chunk.chunk_id for chunk in chunks}
    missing = sorted(requested_ids - existing_ids)
    if missing:
        raise ReviewWorkflowError(f"Chunk not found: {', '.join(missing)}", status_code=404)
    return requested_ids


def validate_approval_preconditions(
    *,
    chunks: Sequence[Chunk],
    chunk_ids: Sequence[str],
    review_flags_acknowledged: bool,
    approval_override_reason: str | None = None,
) -> ApprovalPreconditions:
    requested_ids = require_chunk_ids(chunks, chunk_ids)
    non_approvable_chunks = [
        chunk
        for chunk in chunks
        if chunk.chunk_id in requested_ids and chunk.approval_status in NON_APPROVABLE_CHUNK_STATUSES
    ]
    if non_approvable_chunks:
        sample = ", ".join(
            f"{chunk.chunk_id}:{chunk.approval_status}"
            for chunk in sorted(non_approvable_chunks, key=lambda item: item.chunk_id)[:20]
        )
        raise ReviewWorkflowError(f"Chunks require review before approval: {sample}", status_code=400)
    review_attention = review_attention_by_chunk(chunks, requested_ids)
    override_reason = str(approval_override_reason or "").strip()
    if review_attention and not review_flags_acknowledged and not override_reason:
        sample = ", ".join(
            f"{chunk_id}({';'.join(reasons[:3])})"
            for chunk_id, reasons in sorted(review_attention.items())[:10]
        )
        raise ReviewWorkflowError(f"Review flags must be acknowledged before approval: {sample}", status_code=400)
    return ApprovalPreconditions(requested_ids=requested_ids, review_attention=review_attention)


def _warning_requires_review(warning: str) -> bool:
    normalized = warning.lower()
    return any(keyword in normalized for keyword in REVIEW_ATTENTION_WARNING_KEYWORDS)


def scan_chunk(chunk: Chunk) -> list[dict[str, Any]]:
    text = "\n".join(value for value in [chunk.text, chunk.normalized_text or "", chunk.retrieval_text or ""] if value)
    findings: list[dict[str, Any]] = []
    for rule_id, severity, pattern in SECURITY_SCAN_RULES:
        for match in pattern.finditer(text):
            findings.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "rule_id": rule_id,
                    "severity": severity,
                    "match_hash": stable_content_hash(match.group(0), {"rule_id": rule_id}),
                    "start": match.start(),
                    "end": match.end(),
                }
            )
            break
    return findings


def prepare_security_scan_update(
    *,
    chunks: Sequence[Chunk],
    block_high_risk: bool,
    chunk_ids: set[str] | None = None,
) -> SecurityScanUpdate:
    selected_ids = set(chunk_ids) if chunk_ids else {chunk.chunk_id for chunk in chunks}
    selected_chunks = [chunk for chunk in chunks if chunk.chunk_id in selected_ids]
    findings = [finding for chunk in selected_chunks for finding in scan_chunk(chunk)]
    blocked_ids = {
        str(finding["chunk_id"])
        for finding in findings
        if block_high_risk and finding.get("severity") == "high"
    }
    updated_chunks = [
        chunk.model_copy(
            update={
                "approval_status": "security_blocked",
                "approval_id": None,
                "approved_by": None,
                "approved_at": None,
                "approved_content_hash": None,
            }
        )
        if chunk.chunk_id in blocked_ids
        else chunk
        for chunk in chunks
    ]
    return SecurityScanUpdate(
        updated_chunks=updated_chunks,
        selected_ids=selected_ids,
        findings=findings,
        blocked_chunk_ids=blocked_ids,
        rule_ids=[rule_id for rule_id, _severity, _pattern in SECURITY_SCAN_RULES],
    )


def build_security_scan_record(
    *,
    update: SecurityScanUpdate,
    scan_id: str,
    document_id: str,
    tenant_id: str,
    created_at: str,
    scanned_by: str,
    scan_reason: str,
    vector_sync: dict[str, Any],
) -> dict[str, Any]:
    return {
        "scan_id": scan_id,
        "document_id": document_id,
        "tenant_id": tenant_id,
        "created_at": created_at,
        "scanned_by": scanned_by,
        "scan_reason": scan_reason,
        "scanned_chunk_ids": sorted(update.selected_ids),
        "finding_count": len(update.findings),
        "blocked_chunk_ids": sorted(update.blocked_chunk_ids),
        "findings": update.findings,
        "rules": update.rule_ids,
        "vector_sync": vector_sync,
    }


def clean_evidence_text(value: str | None, *, field_name: str, max_length: int) -> str:
    text = str(value or "").strip()
    if len(text) > max_length:
        raise ReviewWorkflowError(f"{field_name} is too long.")
    return text


def approval_worklist_evidence(
    *,
    worklist_report_path: str | None = None,
    worklist_report_sha256: str | None = None,
    review_batch_manifest_path: str | None = None,
    review_batch_manifest_sha256: str | None = None,
    review_batch_id: str | None = None,
    review_batch_chunk_fingerprint: str | None = None,
    review_strategy: str | None = None,
) -> dict[str, str]:
    evidence = {
        "worklist_report_path": normalize_evidence_artifact_path(
            worklist_report_path,
            field_name="worklist_report_path",
        ),
        "worklist_report_sha256": normalize_optional_sha256(
            worklist_report_sha256,
            field_name="worklist_report_sha256",
        ),
        "review_batch_manifest_path": normalize_evidence_artifact_path(
            review_batch_manifest_path,
            field_name="review_batch_manifest_path",
        ),
        "review_batch_manifest_sha256": normalize_optional_sha256(
            review_batch_manifest_sha256,
            field_name="review_batch_manifest_sha256",
        ),
        "review_batch_id": normalize_evidence_identifier(
            review_batch_id,
            field_name="review_batch_id",
            max_length=120,
        ),
        "review_batch_chunk_fingerprint": normalize_optional_sha256(
            review_batch_chunk_fingerprint,
            field_name="review_batch_chunk_fingerprint",
        ),
        "review_strategy": normalize_evidence_identifier(
            review_strategy,
            field_name="review_strategy",
            max_length=120,
        ),
    }
    return {key: value for key, value in evidence.items() if value}


def prepare_approval_decision(
    *,
    chunks: Sequence[Chunk],
    chunk_ids: Sequence[str],
    review_flags_acknowledged: bool,
    preapproval_scan: dict[str, Any],
    artifact_root: Path,
    runtime_data_dir: Path,
    tenant_id: str,
    document_id: str,
    approval_id: str,
    approved_by: str,
    approved_at: str,
    requested_security_level: str | None,
    worklist_evidence: dict[str, str],
    approval_override_reason: str | None = None,
) -> ApprovalDecisionPreparation:
    if not isinstance(preapproval_scan, dict) or not preapproval_scan.get("scan_id"):
        raise ReviewWorkflowError("preapproval_security_scan is required before approval.")
    blocked_ids = [
        str(chunk_id)
        for chunk_id in preapproval_scan.get("blocked_chunk_ids") or []
        if str(chunk_id or "").strip()
    ]
    if blocked_ids:
        blocked_sample = ", ".join(sorted(blocked_ids)[:20])
        raise ReviewWorkflowError(f"Security scan blocked chunks before approval: {blocked_sample}")

    preconditions = validate_approval_preconditions(
        chunks=chunks,
        chunk_ids=chunk_ids,
        review_flags_acknowledged=review_flags_acknowledged,
        approval_override_reason=approval_override_reason,
    )
    verify_approval_evidence(
        artifact_root=artifact_root,
        runtime_data_dir=runtime_data_dir,
        tenant_id=tenant_id,
        document_id=document_id,
        chunks=chunks,
        requested_ids=preconditions.requested_ids,
        evidence=worklist_evidence,
    )
    approval_update = prepare_approval_update(
        chunks=chunks,
        requested_ids=preconditions.requested_ids,
        approval_id=approval_id,
        approved_by=approved_by,
        approved_at=approved_at,
        requested_security_level=requested_security_level,
        worklist_evidence=worklist_evidence,
        review_attention=preconditions.review_attention,
    )
    return ApprovalDecisionPreparation(
        requested_ids=preconditions.requested_ids,
        worklist_evidence=dict(worklist_evidence),
        preconditions=preconditions,
        approval_update=approval_update,
    )


def prepare_approval_update(
    *,
    chunks: Sequence[Chunk],
    requested_ids: set[str],
    approval_id: str,
    approved_by: str,
    approved_at: str,
    requested_security_level: str | None,
    worklist_evidence: dict[str, str],
    review_attention: dict[str, list[str]],
) -> ApprovalUpdate:
    normalized_requested_security_level = (
        normalize_security_level(requested_security_level) if requested_security_level else ""
    )
    worklist_metadata = approval_worklist_metadata(worklist_evidence)
    before_hashes = chunk_hashes(list(chunks), requested_ids)
    approved_hashes: dict[str, str] = {}
    approved_chunk_snapshots: list[dict[str, Any]] = []
    updated_chunks: list[Chunk] = []
    for chunk in chunks:
        if chunk.chunk_id not in requested_ids:
            updated_chunks.append(chunk)
            continue
        security_level = normalized_requested_security_level or normalize_security_level(chunk.security_level)
        updated_metadata = dict(chunk.metadata)
        for key in APPROVAL_WORKLIST_METADATA_KEYS:
            updated_metadata.pop(key, None)
        updated_metadata.update(worklist_metadata)
        chunk_for_hash = chunk.model_copy(update={"metadata": updated_metadata})
        approved_hash = approved_content_hash(chunk_for_hash, security_level=security_level)
        approved_hashes[chunk.chunk_id] = approved_hash
        chunk_snapshot = {
            "chunk_id": chunk.chunk_id,
            "approval_id": approval_id,
            "previous_approval_status": chunk.approval_status,
            "approved_content_hash": approved_hash,
            "security_level": security_level,
            "department_acl": department_acl_set(chunk.department_acl),
            "review_attention_reasons": review_attention.get(chunk.chunk_id, []),
            "approved_by": approved_by,
            "approved_at": approved_at,
        }
        if worklist_evidence:
            chunk_snapshot["worklist_evidence"] = dict(worklist_evidence)
        approved_chunk_snapshots.append(chunk_snapshot)
        updated_chunks.append(
            chunk.model_copy(
                update={
                    "metadata": updated_metadata,
                    "approval_status": APPROVED_CHUNK_STATUS,
                    "approval_id": approval_id,
                    "approved_by": approved_by,
                    "approved_at": approved_at,
                    "approved_content_hash": approved_hash,
                    "security_level": security_level,
                }
            )
        )
    return ApprovalUpdate(
        updated_chunks=updated_chunks,
        approval_id=approval_id,
        approved_by=approved_by,
        approved_at=approved_at,
        requested_security_level=normalized_requested_security_level,
        before_content_hashes=before_hashes,
        approved_content_hashes=approved_hashes,
        approved_chunks=approved_chunk_snapshots,
        review_attention_chunk_count=len(review_attention),
        review_attention_flags=sorted({reason for reasons in review_attention.values() for reason in reasons}),
        review_attention_samples=[
            {"chunk_id": chunk_id, "reasons": reasons}
            for chunk_id, reasons in sorted(review_attention.items())[:20]
        ],
    )


def build_approval_record(
    *,
    update: ApprovalUpdate,
    approval_record_id: str,
    document_id: str,
    requested_ids: set[str],
    tenant_id: str,
    worklist_evidence: dict[str, str],
    review_flags_acknowledged: bool,
    preapproval_scan: dict[str, Any],
    note: str,
    snapshot: str,
    artifacts: dict[str, str],
    vector_sync: dict[str, Any],
) -> dict[str, Any]:
    return {
        "approval_record_id": approval_record_id,
        "approval_id": update.approval_id,
        "document_id": document_id,
        "chunk_ids": sorted(requested_ids),
        "before_content_hashes": update.before_content_hashes,
        "approved_content_hashes": update.approved_content_hashes,
        "approved_chunks": update.approved_chunks,
        "approved_by": update.approved_by,
        "approved_at": update.approved_at,
        "tenant_id": tenant_id,
        "security_level": update.requested_security_level,
        "worklist_evidence": worklist_evidence,
        "review_flags_acknowledged": bool(review_flags_acknowledged),
        "review_attention_chunk_count": update.review_attention_chunk_count,
        "review_attention_flags": update.review_attention_flags,
        "review_attention_samples": update.review_attention_samples,
        "preapproval_security_scan_id": preapproval_scan["scan_id"],
        "preapproval_finding_count": preapproval_scan["finding_count"],
        "note": note,
        "snapshot": snapshot,
        "artifacts": artifacts,
        "vector_sync": vector_sync,
    }


def prepare_rejection_update(
    *,
    chunks: Sequence[Chunk],
    requested_ids: set[str],
    reason: str,
    reviewed_by: str,
    reviewed_at: str,
) -> RejectionUpdate:
    before_hashes = chunk_hashes(list(chunks), requested_ids)
    updated_chunks: list[Chunk] = []
    for chunk in chunks:
        if chunk.chunk_id not in requested_ids:
            updated_chunks.append(chunk)
            continue
        metadata = {
            **chunk.metadata,
            "review_rejection_reason": reason,
            "review_rejected_by": reviewed_by,
            "review_rejected_at": reviewed_at,
        }
        updated_chunks.append(
            chunk.model_copy(
                update={
                    "approval_status": "rejected",
                    "approval_id": None,
                    "approved_by": None,
                    "approved_at": None,
                    "approved_content_hash": None,
                    "metadata": metadata,
                }
            )
        )
    return RejectionUpdate(
        updated_chunks=updated_chunks,
        reviewed_by=reviewed_by,
        reviewed_at=reviewed_at,
        before_content_hashes=before_hashes,
        after_content_hashes=chunk_hashes(updated_chunks, requested_ids),
    )


def prepare_rejection_decision(
    *,
    chunks: Sequence[Chunk],
    chunk_ids: Sequence[str],
    reason: str,
    reviewed_by: str,
    reviewed_at: str,
) -> RejectionDecisionPreparation:
    requested_ids = require_chunk_ids(chunks, chunk_ids)
    clean_reason = str(reason or "").strip()
    if not clean_reason:
        raise ReviewWorkflowError("rejection reason is required.")
    rejection_update = prepare_rejection_update(
        chunks=chunks,
        requested_ids=requested_ids,
        reason=clean_reason,
        reviewed_by=reviewed_by,
        reviewed_at=reviewed_at,
    )
    return RejectionDecisionPreparation(
        requested_ids=requested_ids,
        reason=clean_reason,
        rejection_update=rejection_update,
    )


def build_rejection_record(
    *,
    update: RejectionUpdate,
    review_id: str,
    document_id: str,
    requested_ids: set[str],
    tenant_id: str,
    reason: str,
    note: str,
    snapshot: str,
    artifacts: dict[str, str],
    vector_sync: dict[str, Any],
) -> dict[str, Any]:
    return {
        "review_id": review_id,
        "document_id": document_id,
        "chunk_ids": sorted(requested_ids),
        "action": "reject",
        "reviewed_by": update.reviewed_by,
        "reviewed_at": update.reviewed_at,
        "tenant_id": tenant_id,
        "status": "rejected",
        "reason": reason,
        "before_content_hashes": update.before_content_hashes,
        "after_content_hashes": update.after_content_hashes,
        "note": note,
        "snapshot": snapshot,
        "artifacts": artifacts,
        "vector_sync": vector_sync,
    }


def normalize_evidence_artifact_path(value: str | None, *, field_name: str) -> str:
    text = clean_evidence_text(value, field_name=field_name, max_length=260)
    if not text:
        return ""
    normalized = text.replace("\\", "/")
    if (
        normalized.startswith("/")
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", normalized)
        or ".." in normalized.split("/")
    ):
        raise ReviewWorkflowError(f"{field_name} must be a safe relative artifact path.")
    return normalized


def normalize_optional_sha256(value: str | None, *, field_name: str) -> str:
    text = clean_evidence_text(value, field_name=field_name, max_length=64).lower()
    if not text:
        return ""
    if not re.fullmatch(r"[a-f0-9]{64}", text):
        raise ReviewWorkflowError(f"{field_name} must be a SHA-256 hex digest.")
    return text


def normalize_evidence_identifier(value: str | None, *, field_name: str, max_length: int) -> str:
    text = clean_evidence_text(value, field_name=field_name, max_length=max_length)
    if not text:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", text):
        raise ReviewWorkflowError(
            f"{field_name} must contain only letters, numbers, dot, underscore, or hyphen."
        )
    return text


def verify_approval_evidence(
    *,
    artifact_root: Path,
    runtime_data_dir: Path,
    tenant_id: str,
    document_id: str,
    chunks: Sequence[Chunk],
    requested_ids: set[str],
    evidence: dict[str, str],
) -> None:
    require_evidence_fields(
        evidence,
        OFFICIAL_APPROVAL_REQUIRED_EVIDENCE_FIELDS,
        detail="Official RAG/MCP approval evidence is required before approval.",
    )

    require_evidence_pair(evidence, "worklist_report_path", "worklist_report_sha256")
    require_evidence_pair(evidence, "review_batch_manifest_path", "review_batch_manifest_sha256", require_right=False)
    batch_keys = {
        "review_batch_manifest_path",
        "review_batch_manifest_sha256",
        "review_batch_id",
        "review_batch_chunk_fingerprint",
        "review_strategy",
    }
    if any(evidence.get(key) for key in batch_keys):
        require_evidence_fields(
            evidence,
            (
                "worklist_report_path",
                "worklist_report_sha256",
                "review_batch_manifest_path",
                "review_batch_id",
                "review_batch_chunk_fingerprint",
            ),
            detail="Batch approval evidence requires worklist path/SHA, review batch manifest path, and review batch id/fingerprint.",
        )
        verify_review_batch_identifier(evidence)

    worklist = load_verified_json_evidence(
        artifact_root,
        path_value=evidence["worklist_report_path"],
        expected_sha256=evidence["worklist_report_sha256"],
        field_name="worklist_report_path",
    )
    verify_worklist_scope(
        worklist,
        runtime_data_dir=runtime_data_dir,
        tenant_id=tenant_id,
        document_id=document_id,
    )

    if evidence.get("review_batch_manifest_path"):
        if not evidence.get("review_batch_manifest_sha256"):
            manifest_path = resolve_evidence_artifact_path(
                artifact_root,
                evidence["review_batch_manifest_path"],
                field_name="review_batch_manifest_path",
            )
            evidence["review_batch_manifest_sha256"] = sha256_file(manifest_path)
        manifest = load_verified_json_evidence(
            artifact_root,
            path_value=evidence["review_batch_manifest_path"],
            expected_sha256=evidence["review_batch_manifest_sha256"],
            field_name="review_batch_manifest_path",
        )
        verify_review_batch_manifest(
            manifest,
            worklist_sha256=evidence["worklist_report_sha256"],
            tenant_id=tenant_id,
            runtime_data_dir=runtime_data_dir,
            document_id=document_id,
            chunks=chunks,
            requested_ids=requested_ids,
            evidence=evidence,
        )


def require_evidence_pair(
    evidence: dict[str, str],
    left_key: str,
    right_key: str,
    *,
    require_right: bool = True,
) -> None:
    has_left = bool(evidence.get(left_key))
    has_right = bool(evidence.get(right_key))
    if has_left and require_right and not has_right:
        raise ReviewWorkflowError(f"{right_key} is required when {left_key} is provided.")
    if has_right and not has_left:
        raise ReviewWorkflowError(f"{left_key} is required when {right_key} is provided.")


def require_evidence_fields(evidence: dict[str, str], fields: Sequence[str], *, detail: str) -> None:
    missing = [field for field in fields if not evidence.get(field)]
    if missing:
        raise ReviewWorkflowError(f"{detail} Missing: {', '.join(missing)}.")


def verify_review_batch_identifier(evidence: dict[str, str]) -> None:
    batch_id = evidence.get("review_batch_id") or ""
    worklist_sha256 = evidence.get("worklist_report_sha256") or ""
    fingerprint = evidence.get("review_batch_chunk_fingerprint") or ""
    expected_prefix = f"approval-{worklist_sha256[:12]}-"
    expected_suffix = fingerprint[:12]
    if not batch_id.startswith(expected_prefix) or not batch_id.endswith(expected_suffix):
        raise ReviewWorkflowError(
            "review_batch_id must match the generated approval batch id pattern for the "
            "provided worklist_report_sha256 and review_batch_chunk_fingerprint."
        )


def load_verified_json_evidence(
    artifact_root: Path,
    *,
    path_value: str,
    expected_sha256: str | None,
    field_name: str,
) -> dict[str, Any]:
    path = resolve_evidence_artifact_path(artifact_root, path_value, field_name=field_name)
    actual_sha256 = sha256_file(path)
    if expected_sha256 and actual_sha256 != expected_sha256:
        raise ReviewWorkflowError(
            f"{field_name} SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReviewWorkflowError(f"{field_name} must point to a valid JSON artifact.") from exc
    if not isinstance(payload, dict):
        raise ReviewWorkflowError(f"{field_name} must point to a JSON object artifact.")
    return payload


def resolve_evidence_artifact_path(artifact_root: Path, path_value: str, *, field_name: str) -> Path:
    root = artifact_root.resolve()
    candidate = (root / path_value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ReviewWorkflowError(f"{field_name} must stay within artifact_root.") from exc
    if not candidate.is_file():
        raise ReviewWorkflowError(f"{field_name} does not exist: {path_value}.")
    return candidate


def verify_worklist_scope(
    worklist: dict[str, Any],
    *,
    runtime_data_dir: Path,
    tenant_id: str,
    document_id: str,
) -> None:
    if str(worklist.get("report_type") or "") != "approval_worklist":
        raise ReviewWorkflowError("worklist_report_path must point to an approval_worklist report.")
    worklist_tenant = str(worklist.get("tenant_id") or "").strip()
    if worklist_tenant and worklist_tenant != tenant_id:
        raise ReviewWorkflowError("worklist_report_path tenant_id does not match the approval tenant.")
    worklist_effective_dir = str(worklist.get("effective_data_dir") or "").strip()
    if worklist_effective_dir:
        try:
            same_runtime = Path(worklist_effective_dir).resolve() == runtime_data_dir.resolve()
        except OSError:
            same_runtime = False
        if not same_runtime:
            raise ReviewWorkflowError(
                "worklist_report_path effective_data_dir does not match the approval runtime data_dir."
            )
    documents = worklist.get("documents") if isinstance(worklist.get("documents"), list) else []
    if documents and not any(str(row.get("document_id") or "") == document_id for row in documents if isinstance(row, dict)):
        raise ReviewWorkflowError("worklist_report_path does not contain the approval document_id.")


def verify_review_batch_manifest(
    manifest: dict[str, Any],
    *,
    worklist_sha256: str,
    tenant_id: str,
    runtime_data_dir: Path,
    document_id: str,
    chunks: Sequence[Chunk],
    requested_ids: set[str],
    evidence: dict[str, str],
) -> None:
    if str(manifest.get("report_type") or "") != "approval_review_batch_manifest":
        raise ReviewWorkflowError("review_batch_manifest_path must point to an approval_review_batch_manifest report.")
    manifest_tenant = str(manifest.get("tenant_id") or "").strip()
    if manifest_tenant and manifest_tenant != tenant_id:
        raise ReviewWorkflowError("review_batch_manifest_path tenant_id does not match the approval tenant.")
    manifest_effective_dir = str(manifest.get("effective_data_dir") or "").strip()
    if manifest_effective_dir:
        try:
            same_runtime = Path(manifest_effective_dir).resolve() == runtime_data_dir.resolve()
        except OSError:
            same_runtime = False
        if not same_runtime:
            raise ReviewWorkflowError(
                "review_batch_manifest_path effective_data_dir does not match the approval runtime data_dir."
            )
    manifest_worklist = manifest.get("worklist_report") if isinstance(manifest.get("worklist_report"), dict) else {}
    if str(manifest_worklist.get("sha256") or "") != worklist_sha256:
        raise ReviewWorkflowError("review_batch_manifest_path does not match worklist_report_sha256.")

    batches = manifest.get("batches") if isinstance(manifest.get("batches"), list) else []
    batch = next(
        (
            item
            for item in batches
            if isinstance(item, dict) and str(item.get("review_batch_id") or "") == evidence["review_batch_id"]
        ),
        None,
    )
    if batch is None:
        raise ReviewWorkflowError("review_batch_id was not found in review_batch_manifest_path.")
    if str(batch.get("review_batch_chunk_fingerprint") or "") != evidence["review_batch_chunk_fingerprint"]:
        raise ReviewWorkflowError("review_batch_chunk_fingerprint does not match review_batch_manifest_path.")
    if evidence.get("review_strategy") and str(batch.get("review_strategy") or "") != evidence["review_strategy"]:
        raise ReviewWorkflowError("review_strategy does not match review_batch_manifest_path.")
    if str(batch.get("document_id") or "") != document_id:
        raise ReviewWorkflowError("review_batch_manifest_path batch document_id does not match.")
    batch_chunk_ids = {str(chunk_id) for chunk_id in batch.get("chunk_ids") or [] if str(chunk_id or "").strip()}
    if batch_chunk_ids != set(requested_ids):
        raise ReviewWorkflowError("review_batch_manifest_path chunk_ids must exactly match the approval request chunk_ids.")

    batch_chunks = batch.get("chunks") if isinstance(batch.get("chunks"), list) else []
    batch_review_type = str(batch.get("review_type") or "")
    recomputed_fingerprint = review_batch_chunk_fingerprint(batch_chunks, batch_review_type)
    if recomputed_fingerprint != evidence["review_batch_chunk_fingerprint"]:
        raise ReviewWorkflowError(
            "review_batch_manifest_path has a stale or inconsistent review_batch_chunk_fingerprint."
        )
    chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks if chunk.chunk_id in requested_ids}
    manifest_chunks_by_id = {
        str(item.get("chunk_id") or ""): item
        for item in batch_chunks
        if isinstance(item, dict) and str(item.get("chunk_id") or "").strip()
    }
    for chunk_id in sorted(requested_ids):
        manifest_chunk = manifest_chunks_by_id.get(chunk_id)
        if manifest_chunk is None:
            raise ReviewWorkflowError(f"review_batch_manifest_path is missing chunk_id {chunk_id}.")
        current_chunk = chunks_by_id[chunk_id]
        expected_review_hash = str(manifest_chunk.get("review_content_hash") or "")
        actual_review_hash = review_content_hash(current_chunk)
        if expected_review_hash != actual_review_hash:
            raise ReviewWorkflowError(
                f"review_batch_manifest_path review_content_hash mismatch for chunk_id {chunk_id}."
            )
        manifest_status = str(manifest_chunk.get("approval_status") or "").strip().lower()
        if manifest_status and manifest_status != str(current_chunk.approval_status or "").strip().lower():
            raise ReviewWorkflowError(
                f"review_batch_manifest_path approval_status mismatch for chunk_id {chunk_id}."
            )


def review_batch_chunk_fingerprint(chunk_items: Sequence[dict[str, Any]], review_type: str) -> str:
    payload = [
        {
            "chunk_id": clean_evidence_value(item.get("chunk_id")),
            "review_content_hash": clean_evidence_value(item.get("review_content_hash")),
            "approval_status": clean_evidence_value(item.get("approval_status")),
            "review_type": review_type,
            "review_priority_tier": clean_evidence_value(item.get("review_priority_tier")),
            "review_category": clean_evidence_value(item.get("review_category")),
            "attention_reasons": sorted(str(reason) for reason in item.get("attention_reasons") or []),
        }
        for item in chunk_items
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def review_content_hash(chunk: Chunk) -> str:
    row = chunk.model_dump(mode="json")
    text_basis, text = review_text_basis(row)
    payload = {
        "schema_version": "approval-review-content-v1",
        "chunk_type": clean_evidence_value(row.get("chunk_type")),
        "source_page_start": row.get("source_page_start"),
        "source_page_end": row.get("source_page_end"),
        "text_basis": text_basis,
        "text": text,
        "metadata": json_safe(row.get("metadata") or {}),
        "warnings": json_safe(row.get("warnings") or []),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def review_text_basis(row: dict[str, Any]) -> tuple[str, str]:
    for field_name in ("retrieval_text", "normalized_text", "text"):
        value = row.get(field_name)
        if isinstance(value, str) and value.strip():
            return field_name, value.strip()
    return "text", ""


def json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    return str(value)


def clean_evidence_value(value: Any) -> str:
    return str(value or "").strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
