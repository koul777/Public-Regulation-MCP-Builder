from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.reapproval_decision_contract import normalize_operator_decision, normalize_override_decision
from scripts.report_metadata import current_repo_commit


EXECUTION_CONTRACT_VERSION = "reapproval-apply-plan-v2"


def build_reapproval_apply_plan(
    *,
    reapproval_review_batch_report: Path,
    decision_template_csv: Path,
    decision_validation_report: Path,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    manifest = _load_manifest(reapproval_review_batch_report)
    decisions = _load_decision_rows(decision_template_csv)
    validation = _load_validation(decision_validation_report)
    decision_by_batch_id = {
        str(row.get("reapproval_batch_id") or "").strip(): row
        for row in decisions
        if str(row.get("reapproval_batch_id") or "").strip()
    }
    blockers = _preflight_blockers(
        validation,
        reapproval_review_batch_report=reapproval_review_batch_report,
        decision_template_csv=decision_template_csv,
    )
    blockers.extend(_manifest_preflight_blockers(manifest))
    batch_plans: list[dict[str, Any]] = []
    plan_counts: Counter[str] = Counter()
    affected_document_ids: set[str] = set()
    reindex_document_ids: set[str] = set()

    for batch in manifest.get("batches") or []:
        if not isinstance(batch, dict):
            continue
        row = decision_by_batch_id.get(str(batch.get("reapproval_batch_id") or "").strip(), {})
        plan, plan_blockers = _batch_plan(batch, row)
        batch_plans.append(plan)
        blockers.extend(plan_blockers)
        plan_counts[plan["planned_operation"]] += 1
        document_id = str(plan.get("document_id") or "").strip()
        if document_id:
            affected_document_ids.add(document_id)
            if plan.get("requires_reindex"):
                reindex_document_ids.add(document_id)

    chunk_totals = {
        "approve_chunk_count": sum(len(plan.get("approve_chunk_ids") or []) for plan in batch_plans),
        "reject_chunk_count": sum(len(plan.get("reject_chunk_ids") or []) for plan in batch_plans),
        "reprocess_chunk_count": sum(len(plan.get("reprocess_chunk_ids") or []) for plan in batch_plans),
        "defer_chunk_count": sum(len(plan.get("defer_chunk_ids") or []) for plan in batch_plans),
        "unresolved_chunk_count": sum(len(plan.get("unresolved_chunk_ids") or []) for plan in batch_plans),
    }
    source_report_artifacts = [
        _source_artifact("reapproval_review_batch_manifest_report", reapproval_review_batch_report),
        _source_artifact("reapproval_decision_template_csv", decision_template_csv),
        _source_artifact("reapproval_decision_validation_report", decision_validation_report),
    ]
    execution_gate = {
        "passed": not blockers,
        "blocker_count": len(blockers),
        "release_gate_status": "ready_for_apply_execution" if not blockers else "blocked_pending_apply_preflight",
    }
    plan_payload_sha256 = _stable_sha256(
        {
            "execution_contract_version": EXECUTION_CONTRACT_VERSION,
            "source_report_artifacts": source_report_artifacts,
            "batch_plans": batch_plans,
            "execution_gate": execution_gate,
        }
    )
    report = {
        "report_type": "reapproval_apply_plan",
        "execution_contract_version": EXECUTION_CONTRACT_VERSION,
        "execution_plan_id": f"reapproval_apply_{plan_payload_sha256[:16]}",
        "plan_payload_sha256": plan_payload_sha256,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        **execution_gate,
        "execution_gate": execution_gate,
        "blockers": blockers,
        "source_reports": {
            "reapproval_review_batch_report": str(reapproval_review_batch_report),
            "decision_template_csv": str(decision_template_csv),
            "decision_validation_report": str(decision_validation_report),
        },
        "source_report_artifacts": source_report_artifacts,
        "idempotency_key_basis": {
            "execution_contract_version": EXECUTION_CONTRACT_VERSION,
            "plan_payload_sha256": plan_payload_sha256,
            "source_sha256": {
                str(item["role"]): str(item["sha256"])
                for item in source_report_artifacts
            },
            "affected_document_ids": sorted(affected_document_ids),
        },
        "validation_summary": {
            "passed": bool(validation.get("passed")),
            "release_gate_status": validation.get("release_gate_status"),
            "expected_batch_count": _int(validation.get("expected_batch_count")),
            "decision_row_count": _int(validation.get("decision_row_count")),
            "complete_row_count": _int(validation.get("complete_row_count")),
            "blank_or_incomplete_row_count": _int(validation.get("blank_or_incomplete_row_count")),
            "blocking_count": _int(validation.get("blocking_count")),
        },
        "summary": {
            "batch_count": len(batch_plans),
            "affected_document_count": len(affected_document_ids),
            "reindex_required_document_count": len(reindex_document_ids),
            "reindex_required_document_ids": sorted(reindex_document_ids),
            "planned_operation_counts": dict(sorted(plan_counts.items())),
            **chunk_totals,
        },
        "batch_plans": batch_plans,
        "operator_controls": {
            "auto_approval": False,
            "auto_reindex": False,
            "applies_reapproval_decisions": False,
            "requires_dedicated_apply_step": True,
            "direct_approval_metadata_write_allowed": False,
            "requires_tenant_and_operator_access_control": True,
            "requires_shared_review_workflow_contract": True,
            "requires_approval_precondition_validation": True,
            "requires_rejection_decision_validation": True,
            "requires_preapproval_security_scan": True,
            "requires_review_flag_acknowledgement": True,
            "requires_approved_content_hash_recalculation": True,
            "requires_review_journal_append": True,
            "requires_apply_audit_event": True,
            "requires_export_refresh": True,
            "requires_vector_sync_or_explicit_reindex": True,
            "requires_explicit_reindex_phase_by_default": True,
            "conditional_vector_sync_requires_existing_successful_index": True,
            "official_mcp_publish_allowed_by_this_plan": False,
            "requires_dry_run_before_apply": True,
            "requires_confirm_apply": True,
            "mutating_executor_implemented": True,
            "mutating_executor_scope": "shadow_runtime_only",
            "in_place_runtime_mutation_allowed": False,
            "official_runtime_promotion_implemented": False,
        },
        "execution_requirements": _execution_requirements(),
        "next_steps": _next_steps(blockers=blockers, reindex_document_ids=sorted(reindex_document_ids)),
        "safety_note": (
            "This plan is read-only. It does not approve chunks, reject chunks, reprocess files, "
            "write Vector DB records, reindex, or publish MCP artifacts."
        ),
        "api_call_count": 0,
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("report_type") != "reapproval_review_batch_manifest":
        raise ValueError("reapproval_review_batch_report must be a reapproval_review_batch_manifest JSON report.")
    return payload


def _load_validation(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("report_type") != "reapproval_decision_validation":
        raise ValueError("decision_validation_report must be a reapproval_decision_validation JSON report.")
    return payload


def _load_decision_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _preflight_blockers(
    validation: dict[str, Any],
    *,
    reapproval_review_batch_report: Path,
    decision_template_csv: Path,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    validation_status = str(validation.get("release_gate_status") or "")
    allowed_validation_statuses = {
        "ready_for_reapproval_apply",
        "no_reapproval_batches",
    }
    if not bool(validation.get("passed")) or validation_status not in allowed_validation_statuses:
        blockers.append(
            {
                "code": "decision-validation-not-ready",
                "detail": (
                    "Decision validation must pass with release_gate_status=ready_for_reapproval_apply "
                    "or no_reapproval_batches before apply planning can proceed."
                ),
                "release_gate_status": validation.get("release_gate_status"),
                "blocking_count": validation.get("blocking_count"),
            }
        )
    if _int(validation.get("blank_or_incomplete_row_count")) > 0:
        blockers.append(
            {
                "code": "decision-validation-incomplete-rows",
                "detail": "Decision validation still reports blank or incomplete rows.",
                "blank_or_incomplete_row_count": validation.get("blank_or_incomplete_row_count"),
            }
        )
    blockers.extend(
        _source_consistency_blockers(
            validation,
            reapproval_review_batch_report=reapproval_review_batch_report,
            decision_template_csv=decision_template_csv,
        )
    )
    return blockers


def _manifest_preflight_blockers(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    batches = [batch for batch in manifest.get("batches") or [] if isinstance(batch, dict)]
    batch_ids = [
        str(batch.get("reapproval_batch_id") or "").strip()
        for batch in batches
        if str(batch.get("reapproval_batch_id") or "").strip()
    ]
    blockers: list[dict[str, Any]] = []
    worklist_report = manifest.get("worklist_report") if isinstance(manifest.get("worklist_report"), dict) else {}
    if not str(worklist_report.get("tenant_id") or "").strip():
        blockers.append(
            {
                "code": "reapproval-review-manifest-tenant-id-missing",
                "detail": "The v2 apply contract requires the reviewed worklist tenant_id.",
            }
        )
    if not str(worklist_report.get("effective_data_dir") or "").strip():
        blockers.append(
            {
                "code": "reapproval-review-manifest-effective-data-dir-missing",
                "detail": "The v2 apply contract requires the reviewed worklist effective_data_dir.",
            }
        )
    manifest_blocker_count = _int_or_none(manifest.get("blocker_count"))
    if manifest_blocker_count is None:
        blockers.append(
            {
                "code": "reapproval-review-batch-blocker-count-missing-or-invalid",
                "detail": "Manifest blocker_count must be present and numeric before apply planning.",
            }
        )
    if not bool(manifest.get("passed")) or (manifest_blocker_count is not None and manifest_blocker_count > 0):
        blockers.append(
            {
                "code": "reapproval-review-batch-manifest-not-ready",
                "detail": "Reapproval review batch manifest must pass before apply planning can proceed.",
                "manifest_passed": bool(manifest.get("passed")),
                "manifest_blocker_count": manifest_blocker_count,
            }
        )
    reported_batch_count = _int_or_none(manifest.get("batch_count"))
    if reported_batch_count is None:
        blockers.append(
            {
                "code": "reapproval-review-batch-count-missing-or-invalid",
                "detail": "Manifest batch_count must be present and numeric before apply planning.",
                "actual_batch_count": len(batch_ids),
            }
        )
    elif reported_batch_count != len(batch_ids):
        blockers.append(
            {
                "code": "reapproval-review-batch-count-mismatch",
                "detail": "Manifest batch_count must match the number of concrete reapproval batch ids before apply planning.",
                "reported_batch_count": reported_batch_count,
                "actual_batch_count": len(batch_ids),
            }
        )
    duplicate_ids = sorted(batch_id for batch_id, count in Counter(batch_ids).items() if count > 1)
    if duplicate_ids:
        blockers.append(
            {
                "code": "reapproval-review-batch-duplicate-id",
                "detail": "Manifest contains duplicate reapproval batch ids.",
                "duplicate_batch_ids": duplicate_ids,
            }
        )
    for batch in batches:
        batch_id = str(batch.get("reapproval_batch_id") or "").strip()
        chunk_ids = {str(chunk_id) for chunk_id in batch.get("chunk_ids") or [] if str(chunk_id or "").strip()}
        chunk_rows = {
            str(item.get("chunk_id") or ""): item
            for item in batch.get("chunks") or []
            if isinstance(item, dict) and str(item.get("chunk_id") or "").strip()
        }
        missing_rows = sorted(chunk_ids - set(chunk_rows))
        if missing_rows:
            blockers.append(
                {
                    "code": "reapproval-review-content-evidence-missing",
                    "detail": "Every planned chunk must have a review evidence row before apply planning.",
                    "reapproval_batch_id": batch_id,
                    "chunk_ids": missing_rows,
                }
            )
        invalid_hash_ids = sorted(
            chunk_id
            for chunk_id in chunk_ids & set(chunk_rows)
            if not _is_sha256(chunk_rows[chunk_id].get("review_content_hash"))
        )
        if invalid_hash_ids:
            blockers.append(
                {
                    "code": "reapproval-review-content-hash-missing-or-invalid",
                    "detail": "Every planned chunk must be bound to a full review_content_hash.",
                    "reapproval_batch_id": batch_id,
                    "chunk_ids": invalid_hash_ids,
                }
            )
    return blockers


def _source_consistency_blockers(
    validation: dict[str, Any],
    *,
    reapproval_review_batch_report: Path,
    decision_template_csv: Path,
) -> list[dict[str, Any]]:
    expected = {
        "reapproval_review_batch_manifest_report": _sha256(reapproval_review_batch_report),
        "reapproval_decision_template_csv": _sha256(decision_template_csv),
    }
    artifacts = {
        str(item.get("role") or ""): item
        for item in validation.get("source_report_artifacts") or []
        if isinstance(item, dict)
    }
    blockers: list[dict[str, Any]] = []
    for role, expected_sha in expected.items():
        artifact = artifacts.get(role)
        actual_sha = str(artifact.get("sha256") or "") if artifact else ""
        if actual_sha != expected_sha:
            blockers.append(
                {
                    "code": "decision-validation-source-mismatch",
                    "detail": "Decision validation report must match the exact reapproval batch manifest and decision CSV inputs.",
                    "role": role,
                    "validation_sha256": actual_sha,
                    "input_sha256": expected_sha,
                }
            )
    return blockers


def _batch_plan(batch: dict[str, Any], decision_row: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    batch_id = str(batch.get("reapproval_batch_id") or "").strip()
    chunk_ids = [str(chunk_id) for chunk_id in batch.get("chunk_ids") or [] if str(chunk_id or "").strip()]
    decision = normalize_operator_decision(decision_row.get("operator_decision"))
    overrides, override_blockers = _parse_overrides(decision_row.get("chunk_decision_overrides_json"), chunk_ids)
    blockers.extend(_batch_blocker(batch_id, item) for item in override_blockers)
    approve_ids: set[str] = set()
    reject_ids: set[str] = set()
    reprocess_ids: set[str] = set()
    defer_ids: set[str] = set()
    unresolved_ids: set[str] = set()

    if not decision:
        unresolved_ids.update(chunk_ids)
        blockers.append(
            _batch_blocker(
                batch_id,
                {
                    "code": "operator-decision-missing",
                    "detail": "Batch has no operator_decision in the decision CSV.",
                },
            )
        )
    elif decision == "approve_all_reviewed":
        approve_ids.update(chunk_ids)
    elif decision == "reject_all":
        reject_ids.update(chunk_ids)
    elif decision == "needs_reprocess":
        reprocess_ids.update(chunk_ids)
    elif decision == "defer":
        defer_ids.update(chunk_ids)
    elif decision == "partial_with_overrides":
        approve_ids.update(chunk_ids)
        for chunk_id, action in overrides.items():
            approve_ids.discard(chunk_id)
            reject_ids.discard(chunk_id)
            reprocess_ids.discard(chunk_id)
            defer_ids.discard(chunk_id)
            if action == "approve":
                approve_ids.add(chunk_id)
            elif action == "reject":
                reject_ids.add(chunk_id)
            elif action == "needs_reprocess":
                reprocess_ids.add(chunk_id)
            elif action == "defer":
                defer_ids.add(chunk_id)
            else:
                unresolved_ids.add(chunk_id)
    else:
        unresolved_ids.update(chunk_ids)
        blockers.append(
            _batch_blocker(
                batch_id,
                {
                    "code": "operator-decision-unsupported",
                    "detail": "Batch operator_decision is not supported by the apply planner.",
                    "operator_decision": decision,
                },
            )
        )

    planned_operation = _planned_operation(
        approve_ids=approve_ids,
        reject_ids=reject_ids,
        reprocess_ids=reprocess_ids,
        defer_ids=defer_ids,
        unresolved_ids=unresolved_ids,
    )
    requires_reindex = bool(approve_ids or reject_ids)
    plan = {
        "reapproval_batch_id": batch_id,
        "document_id": str(batch.get("document_id") or ""),
        "document_name": str(batch.get("document_name") or ""),
        "filename": str(batch.get("filename") or ""),
        "suggested_action": str(batch.get("suggested_action") or ""),
        "review_risk_tier": str(batch.get("review_risk_tier") or ""),
        "operator_decision": decision,
        "reviewer_id": str(decision_row.get("reviewer_id") or ""),
        "reviewed_at": str(decision_row.get("reviewed_at") or ""),
        "approval_scope_confirmation": str(decision_row.get("approval_scope_confirmation") or ""),
        "planned_operation": planned_operation,
        "chunk_count": len(chunk_ids),
        "approve_chunk_ids": sorted(approve_ids),
        "reject_chunk_ids": sorted(reject_ids),
        "reprocess_chunk_ids": sorted(reprocess_ids),
        "defer_chunk_ids": sorted(defer_ids),
        "unresolved_chunk_ids": sorted(unresolved_ids),
        "requires_reindex": requires_reindex,
        "requires_dedicated_reapproval_apply": True,
        "apply_controls": _batch_apply_controls(
            approve_ids=approve_ids,
            reject_ids=reject_ids,
            reprocess_ids=reprocess_ids,
            requires_reindex=requires_reindex,
        ),
        "reapproval_batch_chunk_fingerprint": str(batch.get("reapproval_batch_chunk_fingerprint") or ""),
        "worklist_report_path": str(batch.get("worklist_report_path") or ""),
        "worklist_report_sha256": str(batch.get("worklist_report_sha256") or ""),
        "worklist_chunks_path": str(batch.get("worklist_chunks_path") or ""),
        "worklist_chunks_sha256": str(batch.get("worklist_chunks_sha256") or ""),
    }
    return plan, blockers


def _parse_overrides(raw_value: Any, batch_chunk_ids: Sequence[str]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    text = str(raw_value or "").strip()
    if not text or text == "[]":
        return {}, []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}, [{"code": "override-json-invalid", "detail": "Override JSON could not be parsed."}]
    raw_items: list[tuple[str, Any]] = []
    if isinstance(payload, dict):
        raw_items = [(str(key), value) for key, value in payload.items()]
    elif isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                raw_items.append(("", None))
                continue
            chunk_id = str(item.get("chunk_id") or "").strip()
            action = item.get("operator_decision", item.get("decision", item.get("action")))
            raw_items.append((chunk_id, action))
    else:
        return {}, [{"code": "override-json-unsupported-shape", "detail": "Override JSON must be an object or list."}]
    batch_ids = set(batch_chunk_ids)
    overrides: dict[str, str] = {}
    blockers: list[dict[str, Any]] = []
    for chunk_id, raw_action in raw_items:
        action = _normalize_override_action(raw_action)
        if not chunk_id:
            blockers.append({"code": "override-chunk-id-missing", "detail": "Override row is missing chunk_id."})
            continue
        if chunk_id not in batch_ids:
            blockers.append(
                {
                    "code": "override-chunk-id-outside-batch",
                    "detail": "Override chunk_id is not present in the reapproval batch.",
                    "chunk_id": chunk_id,
                }
            )
            continue
        if not action:
            blockers.append(
                {
                    "code": "override-action-unsupported",
                    "detail": "Override action is not supported by the apply planner.",
                    "chunk_id": chunk_id,
                    "raw_action": raw_action,
                }
            )
            continue
        overrides[chunk_id] = action
    return overrides, blockers


def _normalize_override_action(value: Any) -> str:
    return normalize_override_decision(value)


def _planned_operation(
    *,
    approve_ids: set[str],
    reject_ids: set[str],
    reprocess_ids: set[str],
    defer_ids: set[str],
    unresolved_ids: set[str],
) -> str:
    active = [
        label
        for label, ids in (
            ("approve", approve_ids),
            ("reject", reject_ids),
            ("needs_reprocess", reprocess_ids),
            ("defer", defer_ids),
            ("unresolved", unresolved_ids),
        )
        if ids
    ]
    if not active:
        return "no_chunk_action"
    if len(active) == 1:
        return active[0]
    return "mixed"


def _batch_apply_controls(
    *,
    approve_ids: set[str],
    reject_ids: set[str],
    reprocess_ids: set[str],
    requires_reindex: bool,
) -> dict[str, Any]:
    has_approval = bool(approve_ids)
    has_rejection = bool(reject_ids)
    has_review_mutation = bool(approve_ids or reject_ids)
    return {
        "direct_metadata_write_allowed": False,
        "requires_tenant_and_operator_access_control": has_review_mutation,
        "requires_shared_review_workflow_contract": has_review_mutation,
        "approval_requires_precondition_validation": has_approval,
        "approval_requires_preapproval_security_scan": has_approval,
        "approval_requires_review_flag_acknowledgement_if_attention_present": has_approval,
        "approval_recalculates_approved_content_hash": has_approval,
        "rejection_clears_approval_fields": has_rejection,
        "rejection_requires_reason_validation": has_rejection,
        "requires_review_journal_append": has_review_mutation,
        "requires_apply_audit_event": has_review_mutation,
        "requires_export_refresh": has_review_mutation,
        "requires_vector_sync_or_explicit_reindex": bool(requires_reindex),
        "requires_explicit_reindex_phase": bool(requires_reindex),
        "conditional_vector_sync_allowed_only_after_successful_index": bool(requires_reindex),
        "requires_reprocess_queue": bool(reprocess_ids),
        "official_mcp_publish_allowed_by_batch_plan": False,
    }


def _batch_blocker(batch_id: str, blocker: dict[str, Any]) -> dict[str, Any]:
    return {"reapproval_batch_id": batch_id, **blocker}


def _source_artifact(role: str, path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    return {
        "role": role,
        "path": str(path),
        "byte_count": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stable_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _execution_requirements() -> list[dict[str, Any]]:
    return [
        {
            "step": "load_current_review_chunks",
            "required": True,
            "detail": "Load current repository chunks and re-check reviewed batch chunk ids before mutation.",
        },
        {
            "step": "enforce_tenant_and_operator_access",
            "required": True,
            "detail": "Enforce tenant scope and operator/reviewer authorization before any apply mutation.",
        },
        {
            "step": "use_shared_review_workflow_contract",
            "required": True,
            "detail": "Apply approve/reject decisions through review workflow service contracts; direct metadata writes are not allowed.",
        },
        {
            "step": "validate_approval_preconditions",
            "required": True,
            "detail": "Validate chunk ids, non-approvable statuses, and review-attention acknowledgement before approval.",
        },
        {
            "step": "validate_rejection_decision_contract",
            "required": True,
            "detail": "Validate reject chunk ids and non-empty rejection reasons through the shared review workflow service before rejection.",
        },
        {
            "step": "run_preapproval_security_scan",
            "required": True,
            "detail": "Run the shared security scan before approval and block high-risk chunks from approval.",
        },
        {
            "step": "acknowledge_review_attention_flags",
            "required": True,
            "detail": "Require human acknowledgement when parser/table/OCR/manual review attention reasons are present.",
        },
        {
            "step": "recalculate_approval_hashes",
            "required": True,
            "detail": "Recalculate approved_content_hash from the approved chunk content and approval scope.",
        },
        {
            "step": "append_review_journals_and_snapshots",
            "required": True,
            "detail": "Append approval/review/security-scan records and write review snapshots for traceability.",
        },
        {
            "step": "record_apply_audit_event",
            "required": True,
            "detail": "Record an API or CLI audit event for the apply operation, including tenant, operator, document ids, and affected chunk counts.",
        },
        {
            "step": "refresh_exports_and_vector_state",
            "required": True,
            "detail": "Refresh review exports and sync or explicitly reindex approved Vector DB records before MCP use.",
        },
        {
            "step": "keep_reindex_as_explicit_phase",
            "required": True,
            "detail": "Treat reindex as an explicit post-apply phase by default; conditional vector sync may be reused only when the document already has a successful index state.",
        },
        {
            "step": "rerun_mcp_visibility_gate",
            "required": True,
            "detail": "Rerun MCP index visibility with smoke-doc filtering before any client handoff.",
        },
        {
            "step": "perform_dry_run_before_mutation",
            "required": True,
            "detail": "Run the executor in dry-run mode against the exact execution_plan_id before any mutating apply.",
        },
        {
            "step": "require_explicit_apply_confirmation",
            "required": True,
            "detail": "Require operator confirmation tied to the exact execution_plan_id and plan_payload_sha256.",
        },
    ]


def _next_steps(*, blockers: Sequence[dict[str, Any]], reindex_document_ids: Sequence[str]) -> list[dict[str, Any]]:
    if blockers:
        return [
            {
                "step": "fix_decision_preflight",
                "detail": "Resolve plan blockers and rerun reapproval decision validation before any apply step.",
            }
        ]
    return [
        {
            "step": "run_dedicated_reapproval_apply",
            "detail": "Apply the reviewed decisions with a dedicated reapproval apply command or endpoint that uses the shared review workflow contract and records approval provenance.",
        },
        {
            "step": "reindex_affected_documents",
            "detail": "Reindex documents whose approved/rejected chunks changed, then rerun MCP index visibility.",
            "document_ids": list(reindex_document_ids),
        },
    ]


def _to_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "# Reapproval Apply Plan",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Execution plan id: `{report.get('execution_plan_id')}`",
        f"- Plan payload sha256: `{report.get('plan_payload_sha256')}`",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Release gate status: `{report.get('release_gate_status')}`",
        f"- Blockers: {report.get('blocker_count')}",
        f"- Batches: {summary.get('batch_count')}",
        f"- Affected documents: {summary.get('affected_document_count')}",
        f"- Reindex required documents: {summary.get('reindex_required_document_count')}",
        f"- Planned operation counts: `{summary.get('planned_operation_counts')}`",
        f"- Approve / reject / reprocess / defer chunks: {summary.get('approve_chunk_count')} / {summary.get('reject_chunk_count')} / {summary.get('reprocess_chunk_count')} / {summary.get('defer_chunk_count')}",
        "",
    ]
    blockers = [item for item in report.get("blockers") or [] if isinstance(item, dict)]
    if blockers:
        lines.extend(["## Blockers", ""])
        for blocker in blockers:
            lines.append(
                f"- `{blocker.get('code')}`"
                f"{' batch=' + str(blocker.get('reapproval_batch_id')) if blocker.get('reapproval_batch_id') else ''}: "
                f"{blocker.get('detail')}"
            )
        lines.append("")
    lines.extend(["## Batch Plans", "", "| Batch | Document | Decision | Operation | Approve | Reject | Reprocess | Defer | Reindex |", "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- |"])
    for plan in report.get("batch_plans") or []:
        if not isinstance(plan, dict):
            continue
        lines.append(
            "| {batch} | {document} | {decision} | {operation} | {approve} | {reject} | {reprocess} | {defer} | {reindex} |".format(
                batch=_md_cell(plan.get("reapproval_batch_id")),
                document=_md_cell(plan.get("document_id")),
                decision=_md_cell(plan.get("operator_decision")),
                operation=_md_cell(plan.get("planned_operation")),
                approve=len(plan.get("approve_chunk_ids") or []),
                reject=len(plan.get("reject_chunk_ids") or []),
                reprocess=len(plan.get("reprocess_chunk_ids") or []),
                defer=len(plan.get("defer_chunk_ids") or []),
                reindex=str(bool(plan.get("requires_reindex"))).lower(),
            )
        )
    requirements = [item for item in report.get("execution_requirements") or [] if isinstance(item, dict)]
    if requirements:
        lines.extend(["", "## Execution Requirements", ""])
        for item in requirements:
            lines.append(f"- `{item.get('step')}`: {item.get('detail')}")
    lines.extend(["", "## Safety", "", str(report.get("safety_note") or "")])
    return "\n".join(lines).rstrip() + "\n"


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ").strip() or "-"


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a read-only apply plan from validated reapproval decisions.")
    parser.add_argument("--reapproval-review-batch-report", type=Path, required=True)
    parser.add_argument("--decision-template-csv", type=Path, required=True)
    parser.add_argument("--decision-validation-report", type=Path, required=True)
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-md", type=Path)
    parser.add_argument("--fail-on-blocker", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    args = parse_args(argv)
    report = build_reapproval_apply_plan(
        reapproval_review_batch_report=args.reapproval_review_batch_report,
        decision_template_csv=args.decision_template_csv,
        decision_validation_report=args.decision_validation_report,
        out_json=args.out_json,
        out_md=args.out_md,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stdout)
    if args.fail_on_blocker and report["blocker_count"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
