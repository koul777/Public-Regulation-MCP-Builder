from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Sequence

from app.core.tenant_access import tenant_storage_key
from scripts.build_approval_review_batches import build_approval_review_batches
from scripts.build_approval_worklist import build_approval_worklist


def build_publish_approval_evidence(
    *,
    data_dir: Path,
    artifact_root: Path,
    tenant_id: str,
    tenant_storage_isolation: bool,
    document_id: str,
    chunk_ids: Sequence[str],
    security_level: str,
    artifact_prefix: str,
    operator_approval_reference: str | None = None,
    operator_reviewer_id: str | None = None,
) -> dict[str, Any]:
    approval_reference = _require_nonempty(
        operator_approval_reference,
        "operator_approval_reference",
    )
    reviewer_id = _require_nonempty(operator_reviewer_id, "operator_reviewer_id")
    requested_ids = {str(chunk_id) for chunk_id in chunk_ids if str(chunk_id or "").strip()}
    if not requested_ids:
        raise ValueError("No chunk IDs were provided for approval evidence.")

    safe_prefix = _safe_artifact_stem(artifact_prefix)
    tenant_key = tenant_storage_key(tenant_id)
    reports_dir = artifact_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    worklist_relative = f"reports/{safe_prefix}_{tenant_key}_approval_worklist.json"
    manifest_relative = f"reports/{safe_prefix}_{tenant_key}_approval_review_batches.json"
    worklist_path = artifact_root / worklist_relative
    manifest_path = artifact_root / manifest_relative

    worklist = build_approval_worklist(
        data_dir=data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
    )
    _write_json(worklist_path, worklist)

    manifest = build_approval_review_batches(
        data_dir=data_dir,
        worklist_report=worklist_path,
        worklist_report_artifact_path=worklist_relative,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
        max_chunks_per_batch=max(len(requested_ids), 1),
        default_security_level=security_level,
    )
    if not manifest.get("passed"):
        raise ValueError(f"Approval review batch manifest has blockers: {manifest.get('findings')}")

    batches = [
        batch
        for batch in manifest.get("batches", [])
        if isinstance(batch, dict) and str(batch.get("document_id") or "") == document_id
    ]
    if not batches:
        raise ValueError(f"No approval review batch was generated for document_id={document_id}.")
    covered_ids = {
        str(chunk_id)
        for batch in batches
        for chunk_id in (batch.get("chunk_ids") or [])
        if str(chunk_id or "").strip()
    }
    if covered_ids != requested_ids:
        missing = sorted(requested_ids - covered_ids)[:20]
        extra = sorted(covered_ids - requested_ids)[:20]
        raise ValueError(
            "Approval review batches do not exactly match selected chunks: "
            f"missing={missing}, extra={extra}"
        )
    manual_attention_batches = [
        batch
        for batch in batches
        if str(batch.get("review_type") or "") == "manual_attention"
        or bool(batch.get("review_flags_acknowledged_required"))
    ]
    if manual_attention_batches:
        raise ValueError(
            "Manual-attention approval batches cannot be auto-approved by prepare_mcp_publish_runtime. "
            "Use the operator UI or approval API after real human review, then attach the resulting "
            "approval journal/provenance evidence to the MCP handoff."
        )

    _write_json(manifest_path, manifest)
    manifest_sha256 = _sha256_file(manifest_path)

    approval_requests: list[dict[str, Any]] = []
    for batch in batches:
        template = dict(batch.get("approval_request_template") or {})
        template["review_batch_manifest_path"] = manifest_relative
        template["review_batch_manifest_sha256"] = manifest_sha256
        approval_requests.append(template)

    return {
        "worklist_report_path": worklist_relative,
        "worklist_report_sha256": _sha256_file(worklist_path),
        "review_batch_manifest_path": manifest_relative,
        "review_batch_manifest_sha256": manifest_sha256,
        "approval_input_mode": "operator_confirmed_human_review",
        "operator_approval_reference": approval_reference,
        "operator_reviewer_id": reviewer_id,
        "human_review_required": True,
        "auto_approval_performed": False,
        "safety_note": (
            "This publish evidence packages a human-reviewed approval decision; "
            "it must not be used as an automatic approval substitute."
        ),
        "approval_request_count": len(approval_requests),
        "approval_chunk_count": sum(len(item.get("chunk_ids") or []) for item in approval_requests),
        "manual_attention_batch_count": sum(
            1 for batch in batches if str(batch.get("review_type") or "") == "manual_attention"
        ),
        "manual_attention_chunk_count": sum(
            int(batch.get("chunk_count") or 0)
            for batch in batches
            if str(batch.get("review_type") or "") == "manual_attention"
        ),
        "review_type_batch_counts": _count_by(batches, "review_type"),
        "approval_requests": approval_requests,
        "artifacts": {
            "worklist_json": str(worklist_path),
            "review_batch_manifest_json": str(manifest_path),
        },
    }


def _safe_artifact_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._-")
    return stem or "mcp_publish"


def _require_nonempty(value: str | None, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(
            f"{field_name} is required before preparing an official MCP publish runtime. "
            "Generate review worklists/batches first, complete human review, then pass the review ticket or approval reference."
        )
    return normalized


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _count_by(rows: Sequence[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))
