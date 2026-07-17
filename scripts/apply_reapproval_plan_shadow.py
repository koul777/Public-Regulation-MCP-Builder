from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings
from app.core.tenant_access import settings_for_tenant, tenant_storage_key
from app.ingestion.embedding_adapter import LOCAL_HASH_EMBEDDING_MODEL, embed_vector_records
from app.ingestion.vector_adapter import build_vector_records, is_chunk_approved_for_indexing
from app.processors.exporter import Exporter
from app.schemas.chunk import Chunk
from app.services.review_workflow_service import (
    ReviewWorkflowError,
    approval_worklist_evidence,
    build_approval_record,
    build_rejection_record,
    build_security_scan_record,
    prepare_approval_decision,
    prepare_rejection_decision,
    prepare_security_scan_update,
    review_batch_chunk_fingerprint,
    review_content_hash,
    validate_approval_preconditions,
)
from app.storage.file_store import FileStore
from app.storage.repository import JsonRepository
from scripts.report_metadata import current_repo_commit


REPORT_TYPE = "reapproval_shadow_apply"
STATE_FILENAME = ".reapproval-shadow-state.json"
SUPPORTED_PLAN_CONTRACTS = {"reapproval-apply-plan-v2"}
REQUIRED_SOURCE_ARTIFACT_ROLES = {
    "reapproval_review_batch_manifest_report",
    "reapproval_decision_template_csv",
    "reapproval_decision_validation_report",
}


def apply_reapproval_plan_shadow(
    *,
    apply_plan_path: Path,
    source_data_dir: Path,
    shadow_data_dir: Path,
    artifact_root: Path = PROJECT_ROOT,
    tenant_id: str = "default",
    operator_id: str,
    tenant_storage_isolation: bool = False,
    confirm_shadow_apply: bool = False,
    confirm_execution_plan_id: str | None = None,
    confirm_plan_payload_sha256: str | None = None,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    generated_at = _now()
    plan = _load_json(apply_plan_path)
    output_blockers, safe_out_json, safe_out_md = _report_output_boundaries(
        out_json=out_json,
        out_md=out_md,
        source_data_dir=source_data_dir,
        shadow_data_dir=shadow_data_dir,
    )
    blockers, source_artifacts = _preflight(
        plan=plan,
        apply_plan_path=apply_plan_path,
        source_data_dir=source_data_dir,
        shadow_data_dir=shadow_data_dir,
        artifact_root=artifact_root,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
    )
    blockers = [*output_blockers, *blockers]
    report = _base_report(
        plan=plan,
        apply_plan_path=apply_plan_path,
        source_data_dir=source_data_dir,
        shadow_data_dir=shadow_data_dir,
        tenant_id=tenant_id,
        operator_id=operator_id,
        tenant_storage_isolation=tenant_storage_isolation,
        generated_at=generated_at,
        source_artifacts=source_artifacts,
    )
    report["mode"] = "confirmed_shadow_apply" if confirm_shadow_apply else "dry_run"
    report["confirmation"] = {
        "execution_plan_id": str(confirm_execution_plan_id or ""),
        "plan_payload_sha256": str(confirm_plan_payload_sha256 or ""),
    }
    if not str(operator_id or "").strip():
        blockers.append(_blocker("operator-id-missing", "operator_id is required."))
    if not str(tenant_id or "").strip():
        blockers.append(_blocker("tenant-id-missing", "tenant_id is required."))
    if confirm_shadow_apply:
        expected_plan_id = str(plan.get("execution_plan_id") or "")
        expected_payload_sha = str(plan.get("plan_payload_sha256") or "")
        if str(confirm_execution_plan_id or "") != expected_plan_id:
            blockers.append(
                _blocker(
                    "execution-plan-confirmation-mismatch",
                    "Confirmed execution_plan_id must exactly match the reviewed apply plan.",
                    expected=expected_plan_id,
                    actual=str(confirm_execution_plan_id or ""),
                )
            )
        if str(confirm_plan_payload_sha256 or "") != expected_payload_sha:
            blockers.append(
                _blocker(
                    "plan-payload-confirmation-mismatch",
                    "Confirmed plan_payload_sha256 must exactly match the reviewed apply plan.",
                    expected=expected_payload_sha,
                    actual=str(confirm_plan_payload_sha256 or ""),
                )
            )
    report["blockers"] = blockers
    report["blocker_count"] = len(blockers)
    if blockers:
        report.update(passed=False, status="blocked_preflight", shadow_runtime_written=False)
        _write_report(report, out_json=safe_out_json, out_md=safe_out_md)
        return report

    mutation_chunk_count = sum(
        len(_batch_action_sets(batch)[action])
        for batch in plan.get("batch_plans") or []
        if isinstance(batch, dict)
        for action in ("approve", "reject")
    )
    report["mutation_chunk_count"] = mutation_chunk_count
    if mutation_chunk_count == 0:
        report.update(
            passed=True,
            status="no_shadow_mutations_required",
            shadow_runtime_written=False,
            idempotent_noop=True,
            ready_for_promotion_review=False,
        )
        _write_report(report, out_json=safe_out_json, out_md=safe_out_md)
        return report

    completed = _completed_shadow_state(
        shadow_data_dir,
        str(plan.get("execution_plan_id") or ""),
        plan_payload_sha256=str(plan.get("plan_payload_sha256") or ""),
        tenant_id=tenant_id,
    )
    if completed:
        report.update(
            passed=True,
            status="already_completed",
            shadow_runtime_written=True,
            idempotent_noop=True,
            completion_state=completed,
        )
        _write_report(report, out_json=safe_out_json, out_md=safe_out_md)
        return report

    if not confirm_shadow_apply:
        report.update(
            passed=True,
            status="ready_for_confirmed_shadow_apply",
            shadow_runtime_written=False,
            idempotent_noop=False,
        )
        _write_report(report, out_json=safe_out_json, out_md=safe_out_md)
        return report

    plan_id = str(plan.get("execution_plan_id") or "")
    state_path = shadow_data_dir / STATE_FILENAME
    try:
        _copy_shadow_runtime(
            source_data_dir,
            shadow_data_dir,
            tenant_id=tenant_id,
            tenant_storage_isolation=tenant_storage_isolation,
        )
        copied_runtime_blockers = _runtime_plan_blockers(
            plan=plan,
            source_data_dir=shadow_data_dir,
            tenant_id=tenant_id,
            tenant_storage_isolation=tenant_storage_isolation,
            source_artifacts=source_artifacts,
            verify_manifest_runtime=False,
        )
        if copied_runtime_blockers:
            codes = ", ".join(sorted({str(item.get("code") or "unknown") for item in copied_runtime_blockers}))
            raise ValueError(f"Copied shadow runtime failed plan revalidation: {codes}")
        _write_state(
            state_path,
            status="applying",
            plan_id=plan_id,
            tenant_id=tenant_id,
            ready_for_promotion=False,
            detail="Shadow runtime copied without the selected tenant Vector DB; applying reviewed decisions.",
            extra={
                "plan_payload_sha256": str(plan.get("plan_payload_sha256") or ""),
                "ready_for_promotion_review": False,
            },
        )
        apply_result = _apply_to_shadow(
            plan=plan,
            shadow_data_dir=shadow_data_dir,
            tenant_id=tenant_id,
            operator_id=operator_id,
            tenant_storage_isolation=tenant_storage_isolation,
        )
        _write_state(
            state_path,
            status="completed",
            plan_id=plan_id,
            tenant_id=tenant_id,
            ready_for_promotion=False,
            detail=(
                "All reviewed decisions and the single atomic Vector rebuild completed in the shadow runtime; "
                "independent promotion review is still required."
            ),
            extra={
                "completed_at": _now(),
                "plan_payload_sha256": str(plan.get("plan_payload_sha256") or ""),
                "ready_for_promotion_review": True,
                "vector_record_count": apply_result["vector_record_count"],
                "vector_relative_path": apply_result["vector_relative_path"],
                "vector_sha256": apply_result["vector_sha256"],
                "affected_document_count": apply_result["affected_document_count"],
                "approval_record_count": apply_result["approval_record_count"],
                "rejection_record_count": apply_result["rejection_record_count"],
            },
        )
        report.update(
            passed=True,
            status="shadow_apply_completed",
            shadow_runtime_written=True,
            idempotent_noop=False,
            ready_for_promotion_review=True,
            official_runtime_promoted=False,
            **apply_result,
        )
    except Exception as exc:
        if shadow_data_dir.exists():
            _write_state(
                state_path,
                status="failed",
                plan_id=plan_id,
                tenant_id=tenant_id,
                ready_for_promotion=False,
                detail=str(exc),
                extra={
                    "failed_at": _now(),
                    "plan_payload_sha256": str(plan.get("plan_payload_sha256") or ""),
                    "ready_for_promotion_review": False,
                },
            )
        report.update(
            passed=False,
            status="shadow_apply_failed",
            shadow_runtime_written=shadow_data_dir.exists(),
            ready_for_promotion_review=False,
            official_runtime_promoted=False,
            blocker_count=1,
            blockers=[{"code": "shadow-apply-failed", "detail": str(exc)}],
        )
    _write_report(report, out_json=safe_out_json, out_md=safe_out_md)
    return report


def _preflight(
    *,
    plan: dict[str, Any],
    apply_plan_path: Path,
    source_data_dir: Path,
    shadow_data_dir: Path,
    artifact_root: Path,
    tenant_id: str,
    tenant_storage_isolation: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    source_artifacts: list[dict[str, Any]] = []
    if str(plan.get("report_type") or "") != "reapproval_apply_plan":
        blockers.append(_blocker("apply-plan-type-invalid", "Input must be a reapproval_apply_plan report."))
    contract = str(plan.get("execution_contract_version") or "")
    if contract not in SUPPORTED_PLAN_CONTRACTS:
        blockers.append(
            _blocker(
                "apply-plan-contract-unsupported",
                "Apply plan execution contract is not supported by this executor.",
                contract=contract,
                supported=sorted(SUPPORTED_PLAN_CONTRACTS),
            )
        )
    if not bool(plan.get("passed")) or _int_or_default(plan.get("blocker_count"), -1) != 0:
        blockers.append(_blocker("apply-plan-not-ready", "Apply plan must pass without blockers."))
    if str(plan.get("release_gate_status") or "") != "ready_for_apply_execution":
        blockers.append(
            _blocker(
                "apply-plan-status-not-ready",
                "Apply plan release_gate_status must be ready_for_apply_execution.",
                release_gate_status=plan.get("release_gate_status"),
            )
        )
    if not str(plan.get("execution_plan_id") or "").strip():
        blockers.append(_blocker("execution-plan-id-missing", "Apply plan execution_plan_id is required."))
    if not source_data_dir.is_dir():
        blockers.append(_blocker("source-runtime-missing", "source_data_dir must exist.", path=str(source_data_dir)))
    blockers.extend(_path_boundary_blockers(source_data_dir, shadow_data_dir))

    completed = _completed_shadow_state(
        shadow_data_dir,
        str(plan.get("execution_plan_id") or ""),
        plan_payload_sha256=str(plan.get("plan_payload_sha256") or ""),
        tenant_id=tenant_id,
    )
    if shadow_data_dir.exists() and not completed:
        blockers.append(
            _blocker(
                "shadow-runtime-already-exists",
                "shadow_data_dir must not exist unless it contains a completed state for the same execution plan.",
                path=str(shadow_data_dir),
            )
        )

    expected_plan_hash = _stable_sha256(
        {
            "execution_contract_version": plan.get("execution_contract_version"),
            "source_report_artifacts": plan.get("source_report_artifacts") or [],
            "batch_plans": plan.get("batch_plans") or [],
            "execution_gate": plan.get("execution_gate") or {},
        }
    )
    if str(plan.get("plan_payload_sha256") or "") != expected_plan_hash:
        blockers.append(
            _blocker(
                "apply-plan-payload-hash-mismatch",
                "Apply plan payload no longer matches plan_payload_sha256.",
                expected=expected_plan_hash,
                actual=plan.get("plan_payload_sha256"),
            )
        )
    expected_plan_id = f"reapproval_apply_{expected_plan_hash[:16]}"
    if str(plan.get("execution_plan_id") or "") != expected_plan_id:
        blockers.append(
            _blocker(
                "execution-plan-id-hash-mismatch",
                "execution_plan_id must be derived from the validated plan payload hash.",
                expected=expected_plan_id,
                actual=plan.get("execution_plan_id"),
            )
        )
    execution_gate = plan.get("execution_gate") if isinstance(plan.get("execution_gate"), dict) else {}
    if execution_gate != {
        "passed": bool(plan.get("passed")),
        "blocker_count": _int_or_default(plan.get("blocker_count"), -1),
        "release_gate_status": str(plan.get("release_gate_status") or ""),
    }:
        blockers.append(
            _blocker(
                "apply-plan-execution-gate-mismatch",
                "Top-level apply gate fields must exactly match the hashed execution_gate.",
            )
        )

    artifact_root = artifact_root.resolve()
    for item in plan.get("source_report_artifacts") or []:
        if not isinstance(item, dict):
            blockers.append(_blocker("source-artifact-entry-invalid", "Source artifact entries must be objects."))
            continue
        checked, finding = _verify_source_artifact(item, artifact_root=artifact_root)
        source_artifacts.append(checked)
        if finding:
            blockers.append(finding)
    source_roles = [str(item.get("role") or "") for item in source_artifacts]
    missing_roles = sorted(REQUIRED_SOURCE_ARTIFACT_ROLES - set(source_roles))
    duplicate_roles = sorted(role for role in set(source_roles) if role and source_roles.count(role) > 1)
    if missing_roles:
        blockers.append(
            _blocker(
                "source-artifact-roles-missing",
                "Apply plan is missing required reviewed source artifacts.",
                roles=missing_roles,
            )
        )
    if duplicate_roles:
        blockers.append(
            _blocker(
                "source-artifact-roles-duplicated",
                "Apply plan contains duplicate required source artifact roles.",
                roles=duplicate_roles,
            )
        )
    blockers.extend(_source_validation_blockers(plan=plan, source_artifacts=source_artifacts))

    if not apply_plan_path.is_file():
        blockers.append(_blocker("apply-plan-file-missing", "Apply plan path does not exist."))

    if not blockers and not completed:
        blockers.extend(
            _runtime_plan_blockers(
                plan=plan,
                source_data_dir=source_data_dir,
                tenant_id=tenant_id,
                tenant_storage_isolation=tenant_storage_isolation,
                source_artifacts=source_artifacts,
            )
        )
    return blockers, source_artifacts


def _source_validation_blockers(
    *,
    plan: dict[str, Any],
    source_artifacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    by_role = {
        str(item.get("role") or ""): item
        for item in source_artifacts
        if isinstance(item, dict) and item.get("verified_path") and item.get("actual_sha256")
    }
    validation_item = by_role.get("reapproval_decision_validation_report")
    if validation_item is None:
        return blockers
    try:
        validation = _load_json(Path(str(validation_item["verified_path"])))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [
            _blocker(
                "source-decision-validation-unreadable",
                "The verified decision validation report could not be loaded.",
                error=str(exc),
            )
        ]
    if str(validation.get("report_type") or "") != "reapproval_decision_validation":
        blockers.append(
            _blocker(
                "source-decision-validation-type-invalid",
                "The validation artifact must be a reapproval_decision_validation report.",
            )
        )
    batch_count = len([item for item in plan.get("batch_plans") or [] if isinstance(item, dict)])
    expected_status = "ready_for_reapproval_apply" if batch_count else "no_reapproval_batches"
    if (
        not bool(validation.get("passed"))
        or _int_or_default(validation.get("blocking_count"), -1) != 0
        or str(validation.get("release_gate_status") or "") != expected_status
    ):
        blockers.append(
            _blocker(
                "source-decision-validation-not-ready",
                "The verified decision validation report is not ready for this apply scope.",
                release_gate_status=validation.get("release_gate_status"),
                expected_release_gate_status=expected_status,
            )
        )
    expected_batch_count = _int_or_default(validation.get("expected_batch_count"), -1)
    decision_row_count = _int_or_default(validation.get("decision_row_count"), -1)
    complete_row_count = _int_or_default(validation.get("complete_row_count"), -1)
    incomplete_row_count = _int_or_default(validation.get("blank_or_incomplete_row_count"), -1)
    if (
        expected_batch_count != batch_count
        or decision_row_count != batch_count
        or complete_row_count != batch_count
        or incomplete_row_count != 0
    ):
        blockers.append(
            _blocker(
                "source-decision-validation-scope-mismatch",
                "Decision validation counts must exactly match the apply plan batch scope.",
                plan_batch_count=batch_count,
                expected_batch_count=expected_batch_count,
                decision_row_count=decision_row_count,
                complete_row_count=complete_row_count,
                blank_or_incomplete_row_count=incomplete_row_count,
            )
        )
    validation_sources = {
        str(item.get("role") or ""): str(item.get("sha256") or "")
        for item in validation.get("source_report_artifacts") or []
        if isinstance(item, dict)
    }
    for role in ("reapproval_review_batch_manifest_report", "reapproval_decision_template_csv"):
        checked = by_role.get(role)
        actual_sha = str((checked or {}).get("actual_sha256") or "")
        if not actual_sha or validation_sources.get(role) != actual_sha:
            blockers.append(
                _blocker(
                    "source-decision-validation-artifact-mismatch",
                    "Decision validation is not bound to the verified source artifact.",
                    role=role,
                    validation_sha256=validation_sources.get(role),
                    actual_sha256=actual_sha,
                )
            )
    return blockers


def _runtime_plan_blockers(
    *,
    plan: dict[str, Any],
    source_data_dir: Path,
    tenant_id: str,
    tenant_storage_isolation: bool,
    source_artifacts: list[dict[str, Any]],
    verify_manifest_runtime: bool = True,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    settings = settings_for_tenant(
        Settings(data_dir=source_data_dir, tenant_storage_isolation=tenant_storage_isolation),
        tenant_id,
    )
    repository_manifest = settings.data_dir / "repository" / "manifest.json"
    legacy_repository = settings.data_dir / "repository.json"
    if not repository_manifest.is_file() and not legacy_repository.is_file():
        return [
            _blocker(
                "source-runtime-repository-missing",
                "The selected tenant runtime has no readable repository manifest; preflight will not create one.",
                effective_data_dir=str(settings.data_dir),
            )
        ]
    repository = JsonRepository(settings)
    manifest = _source_reapproval_manifest(source_artifacts)
    if not bool(manifest.get("passed")) or int(manifest.get("blocker_count") or 0) > 0:
        blockers.append(
            _blocker(
                "source-review-manifest-not-ready",
                "The verified reapproval review manifest must pass without blockers.",
            )
        )
    manifest_worklist = manifest.get("worklist_report") if isinstance(manifest.get("worklist_report"), dict) else {}
    manifest_tenant = str(manifest_worklist.get("tenant_id") or "").strip()
    if not manifest_tenant:
        blockers.append(
            _blocker(
                "source-review-manifest-tenant-missing",
                "The verified reapproval review manifest must identify the reviewed tenant.",
            )
        )
    elif manifest_tenant != tenant_id:
        blockers.append(
            _blocker(
                "source-review-manifest-tenant-mismatch",
                "The verified reapproval review manifest tenant does not match the requested tenant.",
                manifest_tenant_id=manifest_tenant,
                tenant_id=tenant_id,
            )
        )
    manifest_runtime = str(manifest_worklist.get("effective_data_dir") or "").strip()
    if not manifest_runtime:
        blockers.append(
            _blocker(
                "source-review-manifest-runtime-missing",
                "The verified reapproval review manifest must identify the reviewed effective runtime.",
            )
        )
    elif verify_manifest_runtime:
        try:
            runtime_matches = Path(manifest_runtime).resolve() == settings.data_dir.resolve()
        except OSError:
            runtime_matches = False
        if not runtime_matches:
            blockers.append(
                _blocker(
                    "source-review-manifest-runtime-mismatch",
                    "The verified reapproval review manifest belongs to a different effective runtime.",
                    manifest_effective_data_dir=manifest_runtime,
                    effective_data_dir=str(settings.data_dir.resolve()),
                )
            )
    manifest_batch_rows = [item for item in manifest.get("batches") or [] if isinstance(item, dict)]
    manifest_batch_ids = [str(item.get("reapproval_batch_id") or "") for item in manifest_batch_rows]
    duplicate_manifest_ids = sorted(
        batch_id for batch_id in set(manifest_batch_ids) if batch_id and manifest_batch_ids.count(batch_id) > 1
    )
    if duplicate_manifest_ids:
        blockers.append(
            _blocker(
                "source-review-batch-ids-duplicated",
                "The verified reapproval review manifest contains duplicate batch ids.",
                reapproval_batch_ids=duplicate_manifest_ids,
            )
        )
    if int(manifest.get("batch_count") or 0) != len(manifest_batch_rows):
        blockers.append(
            _blocker(
                "source-review-batch-count-mismatch",
                "The verified reapproval review manifest batch_count does not match its batch rows.",
                reported=manifest.get("batch_count"),
                actual=len(manifest_batch_rows),
            )
        )
    manifest_batches = {
        str(item.get("reapproval_batch_id") or ""): item
        for item in (manifest.get("batches") or [])
        if isinstance(item, dict)
    }
    seen_chunk_keys: set[tuple[str, str]] = set()
    seen_plan_batch_ids: set[str] = set()
    for batch in plan.get("batch_plans") or []:
        if not isinstance(batch, dict):
            blockers.append(_blocker("batch-plan-invalid", "Every batch plan must be an object."))
            continue
        batch_id = str(batch.get("reapproval_batch_id") or "")
        document_id = str(batch.get("document_id") or "")
        if not batch_id:
            blockers.append(_blocker("batch-id-missing", "Every batch plan must have a reapproval_batch_id."))
        elif batch_id in seen_plan_batch_ids:
            blockers.append(
                _blocker(
                    "batch-id-duplicated",
                    "A reapproval_batch_id appears more than once in the apply plan.",
                    reapproval_batch_id=batch_id,
                )
            )
        seen_plan_batch_ids.add(batch_id)
        action_sets = _batch_action_sets(batch)
        all_ids = set().union(*action_sets.values())
        overlap = _overlapping_ids(action_sets)
        if overlap:
            blockers.append(
                _blocker(
                    "batch-action-overlap",
                    "A chunk cannot have more than one planned action.",
                    reapproval_batch_id=batch_id,
                    chunk_ids=overlap,
                )
            )
        if int(batch.get("chunk_count") or 0) != len(all_ids):
            blockers.append(
                _blocker(
                    "batch-chunk-count-mismatch",
                    "Batch chunk_count must match the union of planned action chunk ids.",
                    reapproval_batch_id=batch_id,
                    reported=batch.get("chunk_count"),
                    actual=len(all_ids),
                )
            )
        if action_sets["unresolved"]:
            blockers.append(
                _blocker(
                    "batch-unresolved-actions",
                    "Unresolved chunk decisions cannot be applied.",
                    reapproval_batch_id=batch_id,
                    chunk_ids=sorted(action_sets["unresolved"]),
                )
            )
        if action_sets["reprocess"]:
            blockers.append(
                _blocker(
                    "reprocess-queue-not-supported",
                    "The shadow executor does not implement source reprocessing; split it into a separate reviewed run.",
                    reapproval_batch_id=batch_id,
                    chunk_ids=sorted(action_sets["reprocess"]),
                )
            )
        document = repository.get_document(document_id)
        if document is None:
            blockers.append(
                _blocker(
                    "batch-document-missing",
                    "Batch document does not exist in the source runtime.",
                    reapproval_batch_id=batch_id,
                    document_id=document_id,
                )
            )
            continue
        if str(document.tenant_id or "") != tenant_id:
            blockers.append(
                _blocker(
                    "batch-document-tenant-mismatch",
                    "Batch document tenant does not match the requested tenant.",
                    reapproval_batch_id=batch_id,
                    document_id=document_id,
                    document_tenant_id=document.tenant_id,
                    tenant_id=tenant_id,
                )
            )
        chunks = repository.get_chunks(document_id)
        by_id = {chunk.chunk_id: chunk for chunk in chunks}
        missing = sorted(all_ids - set(by_id))
        if missing:
            blockers.append(
                _blocker(
                    "batch-chunks-missing",
                    "Batch chunks do not exist in the source runtime.",
                    reapproval_batch_id=batch_id,
                    document_id=document_id,
                    chunk_ids=missing,
                )
            )
            continue
        duplicate_global = sorted(chunk_id for chunk_id in all_ids if (document_id, chunk_id) in seen_chunk_keys)
        if duplicate_global:
            blockers.append(
                _blocker(
                    "chunk-planned-more-than-once",
                    "A document chunk appears in more than one batch plan.",
                    document_id=document_id,
                    chunk_ids=duplicate_global,
                )
            )
        seen_chunk_keys.update((document_id, chunk_id) for chunk_id in all_ids)
        source_batch = manifest_batches.get(batch_id)
        if source_batch is None and all_ids:
            blockers.append(
                _blocker(
                    "source-review-batch-missing",
                    "The reviewed source batch is missing from the verified reapproval manifest.",
                    reapproval_batch_id=batch_id,
                )
            )
        else:
            blockers.extend(_source_evidence_blockers(batch, source_batch or {}, by_id))
        try:
            if action_sets["approve"]:
                validate_approval_preconditions(
                    chunks=chunks,
                    chunk_ids=sorted(action_sets["approve"]),
                    review_flags_acknowledged=True,
                    approval_override_reason=None,
                )
                scan = prepare_security_scan_update(
                    chunks=chunks,
                    block_high_risk=True,
                    chunk_ids=action_sets["approve"],
                )
                if scan.blocked_chunk_ids:
                    blockers.append(
                        _blocker(
                            "preapproval-security-scan-blocked",
                            "High-risk content blocks shadow approval.",
                            reapproval_batch_id=batch_id,
                            chunk_ids=sorted(scan.blocked_chunk_ids),
                        )
                    )
            if action_sets["reject"]:
                prepare_rejection_decision(
                    chunks=chunks,
                    chunk_ids=sorted(action_sets["reject"]),
                    reason=f"Reviewed reapproval decision {batch_id}",
                    reviewed_by="preflight",
                    reviewed_at=_now(),
                )
        except ReviewWorkflowError as exc:
            blockers.append(
                _blocker(
                    "shared-review-workflow-preflight-failed",
                    exc.detail,
                    reapproval_batch_id=batch_id,
                )
            )
    return blockers


def _apply_to_shadow(
    *,
    plan: dict[str, Any],
    shadow_data_dir: Path,
    tenant_id: str,
    operator_id: str,
    tenant_storage_isolation: bool,
) -> dict[str, Any]:
    base_settings = Settings(
        data_dir=shadow_data_dir,
        artifact_root=shadow_data_dir,
        tenant_storage_isolation=tenant_storage_isolation,
    )
    settings = settings_for_tenant(base_settings, tenant_id)
    repository = JsonRepository(settings)
    vector_path = settings.data_dir / "vector_db" / tenant_storage_key(tenant_id) / "approved_vectors.jsonl"
    vector_path.unlink(missing_ok=True)

    document_states: dict[str, list[Chunk]] = {}
    for batch in plan.get("batch_plans") or []:
        document_id = str(batch.get("document_id") or "")
        document_states.setdefault(document_id, repository.get_chunks(document_id))

    evidence = _write_fresh_approval_evidence(
        plan=plan,
        document_states=document_states,
        shadow_data_dir=shadow_data_dir,
        effective_data_dir=settings.data_dir,
        tenant_id=tenant_id,
    )
    approved_at = _now()
    approval_records: list[dict[str, Any]] = []
    rejection_records: list[dict[str, Any]] = []
    security_records: list[dict[str, Any]] = []
    deferred_chunk_ids: list[str] = []

    for batch_index, batch in enumerate(plan.get("batch_plans") or [], start=1):
        batch_id = str(batch.get("reapproval_batch_id") or "")
        document_id = str(batch.get("document_id") or "")
        actions = _batch_action_sets(batch)
        chunks = document_states[document_id]
        if actions["approve"]:
            token = _id_token(plan, batch_id, "approve")
            scan_update = prepare_security_scan_update(
                chunks=chunks,
                block_high_risk=True,
                chunk_ids=actions["approve"],
            )
            scan_record = build_security_scan_record(
                update=scan_update,
                scan_id=f"security_scan_{token}",
                document_id=document_id,
                tenant_id=tenant_id,
                created_at=approved_at,
                scanned_by=operator_id,
                scan_reason="shadow_reapproval",
                vector_sync={"status": "pending", "reason": "shadow_atomic_reindex"},
            )
            if scan_update.blocked_chunk_ids:
                raise ReviewWorkflowError(
                    f"Security scan blocked chunks: {', '.join(sorted(scan_update.blocked_chunk_ids))}"
                )
            batch_evidence = evidence["batches"][batch_id]
            decision = prepare_approval_decision(
                chunks=scan_update.updated_chunks,
                chunk_ids=sorted(actions["approve"]),
                review_flags_acknowledged=True,
                preapproval_scan=scan_record,
                artifact_root=shadow_data_dir,
                runtime_data_dir=settings.data_dir,
                tenant_id=tenant_id,
                document_id=document_id,
                approval_id=f"reapproval_{token}",
                approved_by=operator_id,
                approved_at=approved_at,
                requested_security_level=None,
                worklist_evidence=approval_worklist_evidence(
                    worklist_report_path=evidence["worklist_path"],
                    worklist_report_sha256=evidence["worklist_sha256"],
                    review_batch_manifest_path=evidence["batch_manifest_path"],
                    review_batch_manifest_sha256=evidence["batch_manifest_sha256"],
                    review_batch_id=batch_evidence["review_batch_id"],
                    review_batch_chunk_fingerprint=batch_evidence["review_batch_chunk_fingerprint"],
                    review_strategy="operator_reapproval",
                ),
            )
            document_states[document_id] = decision.approval_update.updated_chunks
            approval_records.append(
                build_approval_record(
                    update=decision.approval_update,
                    approval_record_id=f"approval_record_{token}",
                    document_id=document_id,
                    requested_ids=decision.requested_ids,
                    tenant_id=tenant_id,
                    worklist_evidence=decision.worklist_evidence,
                    review_flags_acknowledged=True,
                    preapproval_scan=scan_record,
                    note=f"Shadow reapproval apply plan {plan.get('execution_plan_id')}",
                    snapshot="",
                    artifacts={},
                    vector_sync={"status": "pending", "reason": "shadow_atomic_reindex"},
                )
            )
            security_records.append(scan_record)
            chunks = document_states[document_id]
        if actions["reject"]:
            token = _id_token(plan, batch_id, "reject")
            rejection = prepare_rejection_decision(
                chunks=chunks,
                chunk_ids=sorted(actions["reject"]),
                reason=f"Reviewed reapproval decision {batch_id}",
                reviewed_by=operator_id,
                reviewed_at=approved_at,
            )
            document_states[document_id] = rejection.rejection_update.updated_chunks
            rejection_records.append(
                build_rejection_record(
                    update=rejection.rejection_update,
                    review_id=f"review_{token}",
                    document_id=document_id,
                    requested_ids=rejection.requested_ids,
                    tenant_id=tenant_id,
                    reason=rejection.reason,
                    note=f"Shadow reapproval apply plan {plan.get('execution_plan_id')}",
                    snapshot="",
                    artifacts={},
                    vector_sync={"status": "pending", "reason": "shadow_atomic_reindex"},
                )
            )
        deferred_chunk_ids.extend(sorted(actions["defer"]))

    artifacts_by_document: dict[str, dict[str, str]] = {}
    for document_id, chunks in document_states.items():
        repository.save_chunks(document_id, chunks)
        artifacts_by_document[document_id] = _refresh_exports(settings, document_id, chunks)

    for record in security_records:
        repository.append_security_scan_record(record)
    for record in approval_records:
        document_id = str(record.get("document_id") or "")
        record["snapshot"] = _write_snapshot(
            settings,
            document_id,
            str(record.get("approval_record_id") or ""),
            document_states[document_id],
        )
        record["artifacts"] = artifacts_by_document[document_id]
        repository.append_approval_record(record)
    for record in rejection_records:
        document_id = str(record.get("document_id") or "")
        record["snapshot"] = _write_snapshot(
            settings,
            document_id,
            str(record.get("review_id") or ""),
            document_states[document_id],
        )
        record["artifacts"] = artifacts_by_document[document_id]
        repository.append_review_record(record)

    _require_approved_journal_alignment(repository, tenant_id=tenant_id)
    all_chunks = [
        chunk
        for document in repository.list_documents()
        if str(document.tenant_id or "") == tenant_id
        for chunk in repository.get_chunks(document.document_id)
    ]
    approved_chunks = [chunk for chunk in all_chunks if str(chunk.approval_status or "").lower() == "approved"]
    invalid_approved = [chunk.chunk_id for chunk in approved_chunks if not is_chunk_approved_for_indexing(chunk.model_dump(mode="json"))]
    if invalid_approved:
        raise ValueError(f"Approved chunks are not indexable: {', '.join(sorted(invalid_approved)[:20])}")
    vector_records, vector_summary = build_vector_records(
        [chunk.model_dump(mode="json") for chunk in all_chunks]
    )
    if len(vector_records) != len(approved_chunks):
        raise ValueError(
            f"Approved/vector count mismatch in shadow runtime: approved={len(approved_chunks)} vectors={len(vector_records)}"
        )
    embedded_records, embedding_summary = embed_vector_records(vector_records)
    _atomic_write_jsonl(vector_path, embedded_records)

    vector_sync = {
        "status": "completed",
        "mode": "single_atomic_shadow_reindex",
        "record_count": len(embedded_records),
        "target_path": str(vector_path),
    }
    plan_id = str(plan.get("execution_plan_id") or "")
    event_id = f"reapproval_shadow_complete_{_id_token(plan, plan_id, 'complete')}"
    repository.append_maintenance_event(
        {
            "event_id": event_id,
            "event_type": "reapproval_shadow_apply_completed",
            "created_at": _now(),
            "tenant_id": tenant_id,
            "operator_id": operator_id,
            "execution_plan_id": plan_id,
            "approval_record_ids": [record.get("approval_record_id") for record in approval_records],
            "rejection_record_ids": [record.get("review_id") for record in rejection_records],
            "deferred_chunk_ids": deferred_chunk_ids,
            "vector_sync": vector_sync,
            "official_runtime_promoted": False,
        }
    )
    for document_id in sorted(document_states):
        record_count = sum(
            1 for record in embedded_records if str(record.get("document_id") or record.get("metadata", {}).get("document_id") or "") == document_id
        )
        repository.append_indexing_job(
            {
                "indexing_job_id": f"index_{_id_token(plan, document_id, 'index')}",
                "document_id": document_id,
                "tenant_id": tenant_id,
                "action": "shadow_reapproval_atomic_reindex",
                "status": "indexed",
                "created_at": _now(),
                "completed_at": _now(),
                "requested_by": operator_id,
                "target_type": "local-jsonl",
                "collection_name": "",
                "dry_run": False,
                "record_count": record_count,
                "embedding_model": LOCAL_HASH_EMBEDDING_MODEL,
                "embedding_dimensions": embedding_summary.get("dimensions"),
                "vector_summary": vector_summary,
                "embedding_summary": embedding_summary,
                "upsert_summary": {"status": "completed", "record_count": record_count, "atomic_full_rebuild": True},
                "artifacts": {"approved_vectors_jsonl": str(vector_path)},
            }
        )
    return {
        "affected_document_count": len(document_states),
        "approval_record_count": len(approval_records),
        "rejection_record_count": len(rejection_records),
        "security_scan_record_count": len(security_records),
        "deferred_chunk_count": len(deferred_chunk_ids),
        "approved_chunk_count": len(approved_chunks),
        "vector_record_count": len(embedded_records),
        "vector_path": str(vector_path),
        "vector_relative_path": vector_path.relative_to(shadow_data_dir).as_posix(),
        "vector_sha256": _sha256_file(vector_path),
        "vector_summary": vector_summary,
        "embedding_summary": embedding_summary,
        "completion_event_id": event_id,
    }


def _write_fresh_approval_evidence(
    *,
    plan: dict[str, Any],
    document_states: dict[str, list[Chunk]],
    shadow_data_dir: Path,
    effective_data_dir: Path,
    tenant_id: str,
) -> dict[str, Any]:
    plan_id = str(plan.get("execution_plan_id") or "")
    root = shadow_data_dir / "reapproval_apply_artifacts" / tenant_storage_key(plan_id)
    root.mkdir(parents=True, exist_ok=True)
    worklist_path = root / "approval_worklist.json"
    worklist = {
        "report_type": "approval_worklist",
        "generated_at": _now(),
        "tenant_id": tenant_id,
        "effective_data_dir": str(effective_data_dir.resolve()),
        "documents": [{"document_id": document_id} for document_id in sorted(document_states)],
        "execution_plan_id": plan_id,
        "shadow_only": True,
    }
    _write_json(worklist_path, worklist)
    worklist_sha = _sha256_file(worklist_path)

    batch_rows: list[dict[str, Any]] = []
    batch_evidence: dict[str, dict[str, str]] = {}
    for index, plan_batch in enumerate(plan.get("batch_plans") or [], start=1):
        batch_id = str(plan_batch.get("reapproval_batch_id") or "")
        document_id = str(plan_batch.get("document_id") or "")
        approve_ids = _batch_action_sets(plan_batch)["approve"]
        if not approve_ids:
            continue
        chunks = {chunk.chunk_id: chunk for chunk in document_states[document_id]}
        chunk_items = [
            {
                "chunk_id": chunk_id,
                "review_content_hash": review_content_hash(chunks[chunk_id]),
                "approval_status": chunks[chunk_id].approval_status,
                "review_priority_tier": "reapproval",
                "review_category": "reapproval",
                "attention_reasons": [],
            }
            for chunk_id in sorted(approve_ids)
        ]
        review_type = "reapproval"
        fingerprint = review_batch_chunk_fingerprint(chunk_items, review_type)
        fresh_batch_id = f"approval-{worklist_sha[:12]}-{index:04d}-reapproval-{fingerprint[:12]}"
        batch_rows.append(
            {
                "review_batch_id": fresh_batch_id,
                "review_batch_chunk_fingerprint": fingerprint,
                "review_type": review_type,
                "review_strategy": "operator_reapproval",
                "document_id": document_id,
                "chunk_ids": sorted(approve_ids),
                "chunks": chunk_items,
                "source_reapproval_batch_id": batch_id,
            }
        )
        batch_evidence[batch_id] = {
            "review_batch_id": fresh_batch_id,
            "review_batch_chunk_fingerprint": fingerprint,
        }
    batch_manifest_path = root / "approval_review_batches.json"
    batch_manifest = {
        "report_type": "approval_review_batch_manifest",
        "generated_at": _now(),
        "tenant_id": tenant_id,
        "effective_data_dir": str(effective_data_dir.resolve()),
        "worklist_report": {"sha256": worklist_sha},
        "batches": batch_rows,
        "execution_plan_id": plan_id,
        "shadow_only": True,
    }
    _write_json(batch_manifest_path, batch_manifest)
    return {
        "worklist_path": worklist_path.relative_to(shadow_data_dir).as_posix(),
        "worklist_sha256": worklist_sha,
        "batch_manifest_path": batch_manifest_path.relative_to(shadow_data_dir).as_posix(),
        "batch_manifest_sha256": _sha256_file(batch_manifest_path),
        "batches": batch_evidence,
    }


def _source_evidence_blockers(
    plan_batch: dict[str, Any],
    source_batch: dict[str, Any],
    chunks_by_id: dict[str, Chunk],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    batch_id = str(plan_batch.get("reapproval_batch_id") or "")
    expected_ids = {
        str(chunk_id)
        for action_ids in _batch_action_sets(plan_batch).values()
        for chunk_id in action_ids
    }
    source_ids = {str(chunk_id) for chunk_id in source_batch.get("chunk_ids") or []}
    if source_ids != expected_ids:
        blockers.append(
            _blocker(
                "source-review-batch-scope-mismatch",
                "Apply plan chunk ids do not match the reviewed source batch.",
                reapproval_batch_id=batch_id,
                plan_chunk_ids=sorted(expected_ids),
                source_chunk_ids=sorted(source_ids),
            )
        )
    source_rows = {
        str(item.get("chunk_id") or ""): item
        for item in source_batch.get("chunks") or []
        if isinstance(item, dict) and str(item.get("chunk_id") or "")
    }
    for chunk_id in sorted(expected_ids & set(chunks_by_id)):
        row = source_rows.get(chunk_id)
        if row is None:
            blockers.append(
                _blocker(
                    "source-review-chunk-evidence-missing",
                    "Reviewed source batch has no chunk evidence row.",
                    reapproval_batch_id=batch_id,
                    chunk_id=chunk_id,
                )
            )
            continue
        current = chunks_by_id[chunk_id]
        expected_hash = str(row.get("approved_content_hash_short") or "").lower()
        actual_hash = str(current.approved_content_hash or "").lower()
        if expected_hash and not actual_hash.startswith(expected_hash):
            blockers.append(
                _blocker(
                    "source-review-approved-hash-mismatch",
                    "Current approved_content_hash does not match the reviewed source batch.",
                    reapproval_batch_id=batch_id,
                    chunk_id=chunk_id,
                    reviewed_hash_prefix=expected_hash,
                    current_hash_prefix=actual_hash[: len(expected_hash)],
                )
            )
        expected_approval_id = str(row.get("approval_id") or "")
        if expected_approval_id and expected_approval_id != str(current.approval_id or ""):
            blockers.append(
                _blocker(
                    "source-review-approval-id-mismatch",
                    "Current approval_id does not match the reviewed source batch.",
                    reapproval_batch_id=batch_id,
                    chunk_id=chunk_id,
                )
            )
        expected_review_hash = str(row.get("review_content_hash") or "").lower()
        actual_review_hash = review_content_hash(current)
        if len(expected_review_hash) != 64 or any(char not in "0123456789abcdef" for char in expected_review_hash):
            blockers.append(
                _blocker(
                    "source-review-content-hash-missing-or-invalid",
                    "Reviewed source evidence must include a full review_content_hash.",
                    reapproval_batch_id=batch_id,
                    chunk_id=chunk_id,
                )
            )
        elif expected_review_hash != actual_review_hash:
            blockers.append(
                _blocker(
                    "source-review-content-hash-mismatch",
                    "Current chunk content no longer matches the content reviewed for this decision.",
                    reapproval_batch_id=batch_id,
                    chunk_id=chunk_id,
                    reviewed_hash=expected_review_hash,
                    current_hash=actual_review_hash,
                )
            )
    return blockers


def _require_approved_journal_alignment(repository: JsonRepository, *, tenant_id: str) -> None:
    records = repository.list_approval_journal_records()
    for document in repository.list_documents():
        if str(document.tenant_id or "") != tenant_id:
            continue
        for chunk in repository.get_chunks(document.document_id):
            if str(chunk.approval_status or "").lower() != "approved":
                continue
            if not any(
                str(record.get("document_id") or "") == document.document_id
                and str(record.get("tenant_id") or "") == tenant_id
                and str(record.get("approval_id") or "") == str(chunk.approval_id or "")
                and chunk.chunk_id in {str(value) for value in record.get("chunk_ids") or []}
                and str((record.get("approved_content_hashes") or {}).get(chunk.chunk_id) or "")
                == str(chunk.approved_content_hash or "")
                for record in records
                if isinstance(record, dict)
            ):
                raise ValueError(
                    f"Approved chunk is missing a matching approval journal record: {document.document_id}/{chunk.chunk_id}"
                )


def _source_reapproval_manifest(source_artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    for item in source_artifacts:
        if str(item.get("role") or "") == "reapproval_review_batch_manifest_report" and item.get("verified_path"):
            payload = _load_json(Path(str(item["verified_path"])))
            if str(payload.get("report_type") or "") == "reapproval_review_batch_manifest":
                return payload
    return {"batches": []}


def _verify_source_artifact(
    item: dict[str, Any],
    *,
    artifact_root: Path,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    raw_path = str(item.get("path") or "").strip()
    role = str(item.get("role") or "")
    checked = {"role": role, "path": raw_path, "expected_sha256": str(item.get("sha256") or "")}
    if not raw_path:
        return checked, _blocker("source-artifact-path-missing", "Source artifact path is missing.", role=role)
    path = Path(raw_path)
    candidate = path.resolve() if path.is_absolute() else (artifact_root / path).resolve()
    try:
        candidate.relative_to(artifact_root)
    except ValueError:
        return checked, _blocker(
            "source-artifact-outside-root",
            "Source artifact must stay within artifact_root.",
            role=role,
            path=str(candidate),
        )
    checked["verified_path"] = str(candidate)
    if not candidate.is_file():
        return checked, _blocker("source-artifact-missing", "Source artifact does not exist.", role=role, path=str(candidate))
    actual_sha = _sha256_file(candidate)
    checked["actual_sha256"] = actual_sha
    if actual_sha != str(item.get("sha256") or ""):
        return checked, _blocker(
            "source-artifact-sha-mismatch",
            "Source artifact SHA-256 does not match the apply plan.",
            role=role,
            expected=item.get("sha256"),
            actual=actual_sha,
        )
    return checked, None


def _path_boundary_blockers(source: Path, shadow: Path) -> list[dict[str, Any]]:
    try:
        source_resolved = source.resolve()
        shadow_resolved = shadow.resolve()
    except OSError as exc:
        return [_blocker("runtime-path-resolution-failed", str(exc))]
    if source_resolved == shadow_resolved:
        return [_blocker("source-shadow-path-equal", "source_data_dir and shadow_data_dir must differ.")]
    if source_resolved in shadow_resolved.parents or shadow_resolved in source_resolved.parents:
        return [
            _blocker(
                "source-shadow-path-nested",
                "source_data_dir and shadow_data_dir must not contain one another.",
                source=str(source_resolved),
                shadow=str(shadow_resolved),
            )
        ]
    return []


def _report_output_boundaries(
    *,
    out_json: Path | None,
    out_md: Path | None,
    source_data_dir: Path,
    shadow_data_dir: Path,
) -> tuple[list[dict[str, Any]], Path | None, Path | None]:
    blockers: list[dict[str, Any]] = []
    safe: dict[str, Path | None] = {"out_json": out_json, "out_md": out_md}
    runtime_roots = {
        "source_data_dir": source_data_dir.resolve(),
        "shadow_data_dir": shadow_data_dir.resolve(),
    }
    for field, path in (("out_json", out_json), ("out_md", out_md)):
        if path is None:
            continue
        try:
            resolved = path.resolve()
        except OSError as exc:
            blockers.append(_blocker("report-output-path-resolution-failed", str(exc), field=field))
            safe[field] = None
            continue
        for root_name, root in runtime_roots.items():
            if resolved == root or root in resolved.parents:
                blockers.append(
                    _blocker(
                        "report-output-inside-runtime",
                        "Report outputs must stay outside source and shadow runtime directories.",
                        field=field,
                        runtime=root_name,
                        path=str(resolved),
                    )
                )
                safe[field] = None
                break
    if out_json is not None and out_md is not None and out_json.resolve() == out_md.resolve():
        blockers.append(
            _blocker(
                "report-output-paths-equal",
                "out_json and out_md must be different files.",
                path=str(out_json.resolve()),
            )
        )
        safe["out_json"] = None
        safe["out_md"] = None
    return blockers, safe["out_json"], safe["out_md"]


def _copy_shadow_runtime(
    source: Path,
    shadow: Path,
    *,
    tenant_id: str,
    tenant_storage_isolation: bool,
) -> None:
    if shadow.exists():
        raise ValueError("shadow_data_dir already exists")

    source_settings = settings_for_tenant(
        Settings(data_dir=source, tenant_storage_isolation=tenant_storage_isolation),
        tenant_id,
    )
    selected_vector_dir = (
        source_settings.data_dir / "vector_db" / tenant_storage_key(tenant_id)
    ).resolve()

    def ignore_selected_tenant_vector(path: str, names: list[str]) -> set[str]:
        try:
            current = Path(path).resolve()
        except OSError:
            return set()
        if current == selected_vector_dir.parent and selected_vector_dir.name in names:
            return {selected_vector_dir.name}
        return set()

    shutil.copytree(source, shadow, ignore=ignore_selected_tenant_vector)


def _batch_action_sets(batch: dict[str, Any]) -> dict[str, set[str]]:
    return {
        "approve": {str(value) for value in batch.get("approve_chunk_ids") or [] if str(value or "")},
        "reject": {str(value) for value in batch.get("reject_chunk_ids") or [] if str(value or "")},
        "reprocess": {str(value) for value in batch.get("reprocess_chunk_ids") or [] if str(value or "")},
        "defer": {str(value) for value in batch.get("defer_chunk_ids") or [] if str(value or "")},
        "unresolved": {str(value) for value in batch.get("unresolved_chunk_ids") or [] if str(value or "")},
    }


def _overlapping_ids(action_sets: dict[str, set[str]]) -> list[str]:
    counts: dict[str, int] = {}
    for ids in action_sets.values():
        for chunk_id in ids:
            counts[chunk_id] = counts.get(chunk_id, 0) + 1
    return sorted(chunk_id for chunk_id, count in counts.items() if count > 1)


def _refresh_exports(settings: Settings, document_id: str, chunks: list[Chunk]) -> dict[str, str]:
    exporter = Exporter()
    file_store = FileStore(settings)
    payloads = {
        "jsonl": exporter.to_jsonl(chunks),
        "csv": exporter.to_csv(chunks),
        "md": exporter.to_markdown(chunks),
        "tables.jsonl": exporter.to_tables_jsonl(chunks),
        "tables.csv": exporter.to_tables_csv(chunks),
    }
    artifacts: dict[str, str] = {}
    for extension, content in payloads.items():
        path = file_store.export_path(document_id, extension)
        path.write_text(content, encoding="utf-8")
        artifacts[extension] = str(path)
    return artifacts


def _write_snapshot(settings: Settings, document_id: str, record_id: str, chunks: list[Chunk]) -> str:
    path = settings.data_dir / "repository" / "review_snapshots" / (
        f"{tenant_storage_key(document_id)}.{tenant_storage_key(record_id)}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_jsonl(path, [chunk.model_dump(mode="json") for chunk in chunks])
    return path.relative_to(settings.data_dir).as_posix()


def _atomic_write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{hashlib.sha256(str(path).encode()).hexdigest()[:12]}.tmp")
    try:
        temp.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows)
            + ("\n" if rows else ""),
            encoding="utf-8",
        )
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _completed_shadow_state(
    path: Path,
    plan_id: str,
    *,
    plan_payload_sha256: str,
    tenant_id: str,
) -> dict[str, Any] | None:
    state_path = path / STATE_FILENAME
    if not state_path.is_file():
        return None
    try:
        state = _load_json(state_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if (
        str(state.get("state_type") or "") == "reapproval_shadow_state"
        and str(state.get("status") or "") == "completed"
        and not bool(state.get("ready_for_promotion"))
        and bool(state.get("ready_for_promotion_review"))
        and not bool(state.get("official_runtime_promoted"))
        and str(state.get("execution_plan_id") or "") == plan_id
        and str(state.get("plan_payload_sha256") or "") == plan_payload_sha256
        and str(state.get("tenant_id") or "") == tenant_id
    ):
        relative_path = str(state.get("vector_relative_path") or "")
        expected_sha = str(state.get("vector_sha256") or "")
        try:
            vector_path = (path / relative_path).resolve()
            vector_path.relative_to(path.resolve())
        except (OSError, ValueError):
            return None
        if relative_path and expected_sha and vector_path.is_file() and _sha256_file(vector_path) == expected_sha:
            return state
    return None


def _write_state(
    path: Path,
    *,
    status: str,
    plan_id: str,
    tenant_id: str,
    ready_for_promotion: bool,
    detail: str,
    extra: dict[str, Any] | None = None,
) -> None:
    payload = {
        "state_type": "reapproval_shadow_state",
        "status": status,
        "execution_plan_id": plan_id,
        "tenant_id": tenant_id,
        "ready_for_promotion": ready_for_promotion,
        "official_runtime_promoted": False,
        "updated_at": _now(),
        "detail": detail,
        **(extra or {}),
    }
    _write_json(path, payload)


def _base_report(
    *,
    plan: dict[str, Any],
    apply_plan_path: Path,
    source_data_dir: Path,
    shadow_data_dir: Path,
    tenant_id: str,
    operator_id: str,
    tenant_storage_isolation: bool,
    generated_at: str,
    source_artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "report_type": REPORT_TYPE,
        "generated_at": generated_at,
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "execution_plan_id": plan.get("execution_plan_id"),
        "plan_payload_sha256": plan.get("plan_payload_sha256"),
        "apply_plan_path": str(apply_plan_path),
        "apply_plan_sha256": _sha256_file(apply_plan_path) if apply_plan_path.is_file() else "",
        "source_data_dir": str(source_data_dir),
        "shadow_data_dir": str(shadow_data_dir),
        "tenant_id": tenant_id,
        "operator_id": operator_id,
        "tenant_storage_isolation": tenant_storage_isolation,
        "source_artifacts": source_artifacts,
        "official_runtime_promoted": False,
        "api_call_count": 0,
        "safety_note": (
            "This executor never mutates source_data_dir and never promotes the shadow runtime. "
            "The shadow Vector DB is absent until every reviewed decision and journal write succeeds."
        ),
    }


def _write_report(report: dict[str, Any], *, out_json: Path | None, out_md: Path | None) -> None:
    if out_json:
        _write_json(out_json, report)
    if out_md:
        lines = [
            "# Reapproval Shadow Apply",
            "",
            f"- Generated at: {report.get('generated_at')}",
            f"- Mode: `{report.get('mode')}`",
            f"- Status: `{report.get('status')}`",
            f"- Passed: `{str(report.get('passed')).lower()}`",
            f"- Execution plan: `{report.get('execution_plan_id')}`",
            f"- Source runtime: `{report.get('source_data_dir')}`",
            f"- Shadow runtime: `{report.get('shadow_data_dir')}`",
            f"- Vector records: `{report.get('vector_record_count', 0)}`",
            f"- Official runtime promoted: `{str(report.get('official_runtime_promoted')).lower()}`",
            "",
            "## Blockers",
            "",
        ]
        if report.get("blockers"):
            lines.extend(
                f"- `{item.get('code')}`: {item.get('detail')}" for item in report.get("blockers") or []
            )
        else:
            lines.append("- None")
        lines.extend(["", "## Safety", "", str(report.get("safety_note") or "")])
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return payload


def _id_token(plan: dict[str, Any], subject: str, action: str) -> str:
    basis = f"{plan.get('execution_plan_id')}|{subject}|{action}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _stable_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _blocker(code: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "detail": detail, **extra}


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and apply a reviewed reapproval plan to a new shadow runtime only."
    )
    parser.add_argument("--apply-plan", type=Path, required=True)
    parser.add_argument("--source-data-dir", type=Path, required=True)
    parser.add_argument("--shadow-data-dir", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--operator-id", required=True)
    storage = parser.add_mutually_exclusive_group()
    storage.add_argument("--tenant-storage-isolation", action="store_true")
    storage.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--confirm-shadow-apply", action="store_true")
    parser.add_argument("--confirm-execution-plan-id")
    parser.add_argument("--confirm-plan-payload-sha256")
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-md", type=Path)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    args = build_parser().parse_args(argv)
    report = apply_reapproval_plan_shadow(
        apply_plan_path=args.apply_plan,
        source_data_dir=args.source_data_dir,
        shadow_data_dir=args.shadow_data_dir,
        artifact_root=args.artifact_root,
        tenant_id=args.tenant_id,
        operator_id=args.operator_id,
        tenant_storage_isolation=bool(args.tenant_storage_isolation and not args.flat_storage),
        confirm_shadow_apply=args.confirm_shadow_apply,
        confirm_execution_plan_id=args.confirm_execution_plan_id,
        confirm_plan_payload_sha256=args.confirm_plan_payload_sha256,
        out_json=args.out_json,
        out_md=args.out_md,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stdout)
    if args.fail_on_issue and not bool(report.get("passed")):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
