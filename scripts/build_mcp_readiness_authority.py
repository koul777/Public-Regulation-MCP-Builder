from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_contracts import (
    EXPECTED_AUTHORITY_ARTIFACTS,
    REPORT_TYPE_MCP_CONNECTION_READINESS,
    REPORT_TYPE_MCP_INDEX_VISIBILITY_AUDIT,
    REPORT_TYPE_MCP_PRODUCT_READINESS,
    REPORT_TYPE_MCP_READINESS_AUTHORITY,
    REQUIRED_AUTHORITY_ROLES,
)


HASH_CHUNK_BYTES = 1024 * 1024


def build_mcp_readiness_authority(
    *,
    authoritative_artifacts: Iterable[tuple[str, Path]],
    supersedes: Iterable[tuple[Path, str]] | None = None,
    repo_root: Path | None = None,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    root = repo_root.resolve() if repo_root is not None else PROJECT_ROOT
    generated_at = datetime.now(timezone.utc).isoformat()
    authoritative = [
        _artifact_summary(root, path, role=role, generated_at=generated_at)
        for role, path in authoritative_artifacts
    ]
    superseded = [
        {
            **_artifact_summary(root, path, role="superseded", generated_at=generated_at),
            "reason": reason,
        }
        for path, reason in supersedes or []
    ]
    findings = [
        *_authoritative_findings(authoritative),
        *_supersedes_findings(authoritative, superseded),
    ]
    blocker_count = sum(1 for item in findings if item["severity"] == "blocker")
    warning_count = sum(1 for item in findings if item["severity"] == "warning")
    report = {
        "report_type": REPORT_TYPE_MCP_READINESS_AUTHORITY,
        "authority_version": 1,
        "generated_at": generated_at,
        "repo_commit": _current_repo_commit(root),
        "repo_worktree": _repo_worktree_state(root),
        "passed": blocker_count == 0,
        "blocking_count": blocker_count,
        "warning_count": warning_count,
        "finding_count": len(findings),
        "findings": findings,
        "authoritative_artifacts": authoritative,
        "supersedes": superseded,
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _artifact_summary(root: Path, path: Path, *, role: str, generated_at: str) -> dict[str, Any]:
    candidate = path if path.is_absolute() else root / path
    summary: dict[str, Any] = {
        "role": role,
        "path": _display_path(root, path, candidate),
        "exists": candidate.is_file(),
        "byte_count": None,
        "sha256": None,
        "generated_at": generated_at,
    }
    if not candidate.is_file():
        return summary
    summary["byte_count"] = candidate.stat().st_size
    summary["sha256"] = _sha256_file(candidate)
    payload = _load_json_object(candidate)
    if payload:
        summary["json_summary"] = _json_summary(payload)
        if role == "product_readiness":
            source_artifacts = payload.get("source_report_artifacts")
            source_summary = payload.get("source_report_artifact_summary")
            summary["product_readiness_contract"] = _product_readiness_contract_summary(
                source_artifacts,
                source_summary,
            )
    return summary


def _json_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in (
        "report_type",
        "generated_at",
        "passed",
        "blocking_count",
        "warning_count",
        "finding_count",
        "handoff_ready",
        "decision",
        "top_k",
        "query_count",
        "query_spec_path",
        "query_spec_sha256",
        "query_spec_byte_count",
        "query_spec_item_count",
        "quality_issue_count",
        "transport",
        "tenant_id",
        "repo_commit",
    ):
        if key in payload:
            summary[key] = payload[key]
    _merge_index_visibility_nested_summary(summary, payload)
    _merge_product_readiness_nested_summary(summary, payload)
    _merge_connection_readiness_nested_summary(summary, payload)
    return summary


def _merge_index_visibility_nested_summary(summary: dict[str, Any], payload: dict[str, Any]) -> None:
    if payload.get("report_type") != REPORT_TYPE_MCP_INDEX_VISIBILITY_AUDIT:
        return
    journal = payload.get("approval_journal_coverage")
    if isinstance(journal, dict):
        for key in (
            "journal_record_count",
            "record_count",
            "eligible_record_count",
            "matched_record_count",
            "missing_record_count",
        ):
            if key in journal:
                summary[f"approval_journal_coverage_{key}"] = journal[key]


def _merge_connection_readiness_nested_summary(summary: dict[str, Any], payload: dict[str, Any]) -> None:
    if payload.get("report_type") != REPORT_TYPE_MCP_CONNECTION_READINESS:
        return
    visibility = payload.get("mcp_index_visibility_summary")
    if not isinstance(visibility, dict):
        return
    for key in (
        "passed",
        "tenant_id",
        "document_count",
        "total_approved_chunks",
        "total_mcp_visible_records",
        "finding_count",
    ):
        if key in visibility:
            summary[f"mcp_index_visibility_{key}"] = visibility[key]
    journal = visibility.get("approval_journal_coverage")
    if isinstance(journal, dict):
        for key in (
            "journal_record_count",
            "record_count",
            "eligible_record_count",
            "matched_record_count",
            "missing_record_count",
        ):
            if key in journal:
                summary[f"mcp_index_visibility_approval_journal_coverage_{key}"] = journal[key]


def _merge_product_readiness_nested_summary(summary: dict[str, Any], payload: dict[str, Any]) -> None:
    for section_key, keys in (
        (
            "temporal_coverage_summary",
            (
                "record_count",
                "with_temporal_metadata_count",
                "without_temporal_metadata_count",
                "temporal_metadata_ratio",
                "candidate_missing_record_count",
            ),
        ),
        (
            "temporal_backfill_shadow_summary",
            (
                "delta_temporal_metadata_count",
                "after_temporal_metadata_ratio",
                "conflict_chunk_count",
                "ambiguous_chunk_count",
                "write_blocked",
                "shadow_runtime_written",
            ),
        ),
        (
            "temporal_ambiguity_scope_summary",
            (
                "status",
                "ambiguous_chunk_count",
                "ambiguous_chunk_ratio",
                "vector_record_count",
                "ambiguous_record_count",
                "review_slice_count",
                "blocking_decision_count",
            ),
        ),
        (
            "temporal_evidence_guard_summary",
            (
                "source_count",
                "stale_artifact_count",
                "payload_generated_at_span_hours",
                "payload_generated_at_span_exceeds_threshold",
                "runtime_lineage_mismatch_count",
                "runtime_lineage_value_count",
                "strict_temporal_evidence",
                "passed",
            ),
        ),
        (
            "revision_impact_summary",
            (
                "report_count",
                "before_unit_count",
                "after_unit_count",
                "changed_count",
                "added_count",
                "removed_count",
                "metadata_only_changed_count",
                "approval_required_count",
                "approval_reuse_candidate_count",
                "deindex_required_count",
            ),
        ),
        (
            "runtime_version_drift_summary",
            (
                "current_chunker_version",
                "approved_repository_stale_chunker_count",
                "vector_stale_chunker_count",
                "vector_integrity_failure_count",
                "vector_integrity_content_hash_mismatch_count",
                "vector_integrity_verification_hash_mismatch_count",
                "vector_integrity_metadata_missing_required_count",
                "vector_integrity_invalid_approval_status_count",
                "vector_integrity_invalid_security_level_count",
                "vector_integrity_embedded_dimension_mismatch_count",
                "vector_integrity_embedded_failure_count",
                "vector_integrity_local_path_leak_count",
                "reprocess_requires_reapproval",
                "approved_chunks_with_approved_hash_count",
            ),
        ),
        (
            "approval_workload_summary",
            (
                "report_count",
                "document_count",
                "total_chunks",
                "manual_attention_chunks",
                "manual_attention_rate",
                "low_risk_batch_review_candidate_chunks",
                "low_risk_batch_review_candidate_rate",
                "blocking_review_chunks",
                "domain_attention_chunks",
            ),
        ),
        (
            "approval_review_batch_summary",
            (
                "report_count",
                "batch_count",
                "approval_chunk_count",
                "manual_attention_chunks",
                "low_risk_batch_review_candidate_chunks",
                "blocker_count",
                "warning_count",
            ),
        ),
        (
            "reapproval_workload_summary",
            (
                "report_count",
                "document_count",
                "reapproval_candidate_chunks",
                "high_risk_candidate_chunks",
                "temporal_sample_candidate_chunks",
                "low_risk_candidate_chunks",
                "recommended_initial_review_chunks",
                "estimated_initial_review_minutes",
                "source_vector_integrity_failure_count",
                "pre_reapproval_blocker_count",
                "initial_review_reduction_ratio",
            ),
        ),
        (
            "reapproval_review_batch_summary",
            (
                "report_count",
                "candidate_count",
                "selected_candidate_count",
                "batch_count",
                "reapproval_chunk_count",
                "blocker_count",
                "warning_count",
                "max_chunks_per_batch",
            ),
        ),
        (
            "reapproval_decision_validation_summary",
            (
                "report_count",
                "expected_batch_count",
                "complete_row_count",
                "blank_or_incomplete_row_count",
                "blocking_count",
                "passed",
                "release_gate_status_counts",
                "operator_decision_counts",
            ),
        ),
        (
            "reapproval_apply_plan_summary",
            (
                "report_count",
                "passed",
                "blocker_count",
                "ready_plan_count",
                "batch_count",
                "approve_chunk_count",
                "reject_chunk_count",
                "reprocess_chunk_count",
                "defer_chunk_count",
                "batch_apply_control_count",
                "batch_requires_shared_review_workflow_contract_count",
                "batch_requires_explicit_reindex_phase_count",
                "batch_conditional_vector_sync_guard_count",
                "direct_metadata_write_allowed_count",
                "mcp_publish_allowed_count",
                "unsafe_contract_violation_count",
                "release_gate_status_counts",
                "observed_execution_step_counts",
            ),
        ),
    ):
        section = payload.get(section_key)
        if not isinstance(section, dict):
            continue
        for key in keys:
            if key in section:
                summary[f"{section_key}_{key}"] = section[key]
        if section_key == "reapproval_review_batch_summary":
            for count_key in ("risk_tier_chunk_counts", "action_chunk_counts"):
                counts = section.get(count_key)
                if isinstance(counts, dict):
                    summary[f"{section_key}_{count_key}"] = counts


def _product_readiness_contract_summary(
    source_artifacts: Any,
    source_summary: Any,
) -> dict[str, Any]:
    artifacts = [item for item in source_artifacts or [] if isinstance(item, dict)]
    source_roles = sorted(
        {
            str(item.get("role") or "")
            for item in artifacts
            if str(item.get("role") or "").strip()
        }
    )
    required_fields = ("path", "sha256", "byte_count", "modified_at")
    complete_count = sum(
        1
        for item in artifacts
        if all(item.get(field) not in (None, "") for field in required_fields)
    )
    return {
        "source_report_artifact_count": len(artifacts),
        "source_report_artifact_complete_count": complete_count,
        "missing_source_report_artifact_count": max(len(artifacts) - complete_count, 0),
        "source_report_roles": source_roles,
        "source_report_artifact_summary": source_summary if isinstance(source_summary, dict) else {},
    }


def _authoritative_findings(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not artifacts:
        return [_finding("blocker", "authority-artifacts-missing", "No authoritative artifacts were provided.")]
    roles = [str(item.get("role") or "") for item in artifacts]
    duplicate_roles = sorted({role for role in roles if role and roles.count(role) > 1})
    if duplicate_roles:
        findings.append(
            _finding(
                "blocker",
                "authority-artifact-role-duplicate",
                "Authoritative artifact roles must be unique.",
                roles=duplicate_roles,
            )
        )
    product_artifacts = [item for item in artifacts if item.get("role") == "product_readiness"]
    missing_required_roles = sorted(REQUIRED_AUTHORITY_ROLES - set(roles))
    if missing_required_roles:
        findings.append(
            _finding(
                "blocker",
                "authority-required-roles-missing",
                "MCP readiness authority must include the core product, demo, transport, index-visibility, and connection-readiness artifacts.",
                roles=missing_required_roles,
            )
        )
    if not product_artifacts:
        findings.append(
            _finding(
                "blocker",
                "product-readiness-authority-missing",
                "One authoritative artifact must use role product_readiness.",
            )
        )
    for artifact in artifacts:
        path = str(artifact.get("path") or "")
        if not artifact.get("exists"):
            findings.append(_finding("blocker", "authority-artifact-missing", "Authoritative artifact is missing.", path=path))
            continue
        if not artifact.get("sha256"):
            findings.append(_finding("blocker", "authority-artifact-sha-missing", "Authoritative artifact has no sha256.", path=path))
        summary = artifact.get("json_summary") if isinstance(artifact.get("json_summary"), dict) else {}
        if summary.get("passed") is False:
            findings.append(_finding("blocker", "authority-artifact-not-passing", "Authoritative artifact did not pass.", path=path))
        expected = EXPECTED_AUTHORITY_ARTIFACTS.get(str(artifact.get("role") or ""))
        if expected:
            normalized_path = path.replace("\\", "/")
            if normalized_path != expected["path"]:
                findings.append(
                    _finding(
                        "blocker",
                        "authority-artifact-role-path",
                        "Authoritative artifact role points to an unexpected path.",
                        role=artifact.get("role"),
                        path=path,
                        expected_path=expected["path"],
                    )
                )
            if summary.get("report_type") != expected["report_type"]:
                findings.append(
                    _finding(
                        "blocker",
                        "authority-artifact-role-report-type",
                        "Authoritative artifact role points to an unexpected report_type.",
                        role=artifact.get("role"),
                        path=path,
                        report_type=summary.get("report_type"),
                        expected_report_type=expected["report_type"],
                    )
                )
        if artifact.get("role") == "product_readiness":
            findings.extend(_product_readiness_findings(artifact, summary))
        if artifact.get("role") == "mcp_index_visibility":
            findings.extend(
                _approval_journal_coverage_findings(
                    artifact,
                    summary,
                    prefix="approval_journal_coverage_",
                    label="MCP index visibility",
                )
            )
        if artifact.get("role") == "mcp_connection_readiness":
            findings.extend(
                _approval_journal_coverage_findings(
                    artifact,
                    summary,
                    prefix="mcp_index_visibility_approval_journal_coverage_",
                    label="MCP connection readiness index visibility",
                )
            )
    return findings


def _product_readiness_findings(
    artifact: dict[str, Any],
    summary: dict[str, Any],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    path = str(artifact.get("path") or "")
    if summary.get("report_type") != REPORT_TYPE_MCP_PRODUCT_READINESS:
        findings.append(
            _finding(
                "blocker",
                "product-readiness-report-type",
                "product_readiness authority must point to an mcp_product_readiness JSON report.",
                path=path,
            )
        )
    if summary.get("passed") is not True:
        findings.append(_finding("blocker", "product-readiness-not-passing", "Product readiness report is not passing.", path=path))
    if int(summary.get("blocking_count") or 0) > 0:
        findings.append(
            _finding(
                "blocker",
                "product-readiness-blockers",
                "Product readiness report still has blocking findings.",
                path=path,
            )
        )
    contract = artifact.get("product_readiness_contract") if isinstance(artifact.get("product_readiness_contract"), dict) else {}
    artifact_count = int(contract.get("source_report_artifact_count") or 0)
    complete_count = int(contract.get("source_report_artifact_complete_count") or 0)
    if artifact_count == 0:
        findings.append(
            _finding(
                "blocker",
                "product-readiness-source-fingerprints-missing",
                "Product readiness report must include source_report_artifacts with upstream fingerprints.",
                path=path,
            )
        )
    elif artifact_count != complete_count:
        findings.append(
            _finding(
                "blocker",
                "product-readiness-source-fingerprints-incomplete",
                "Every source_report_artifacts item must include path, sha256, byte_count, and modified_at.",
                path=path,
                artifact_count=artifact_count,
                complete_count=complete_count,
            )
        )
    missing_reapproval_roles = _missing_reapproval_source_roles(summary, contract)
    if missing_reapproval_roles:
        findings.append(
            _finding(
                "blocker",
                "product-readiness-reapproval-source-roles-missing",
                "Product readiness reapproval summaries must include matching source report roles.",
                path=path,
                roles=missing_reapproval_roles,
            )
        )
    return findings


def _missing_reapproval_source_roles(
    summary: dict[str, Any],
    contract: dict[str, Any],
) -> list[str]:
    source_roles = {
        str(role)
        for role in contract.get("source_report_roles") or []
        if str(role).strip()
    }
    required_roles: set[str] = set()
    if int(summary.get("reapproval_workload_summary_reapproval_candidate_chunks") or 0) > 0:
        required_roles.add("reapproval_worklist_report")
    if int(summary.get("reapproval_review_batch_summary_batch_count") or 0) > 0:
        required_roles.update(
            {
                "reapproval_review_batch_manifest_report",
                "reapproval_decision_validation_report",
                "reapproval_apply_plan_report",
            }
        )
    if int(summary.get("reapproval_decision_validation_summary_report_count") or 0) > 0:
        required_roles.add("reapproval_decision_validation_report")
    if int(summary.get("reapproval_apply_plan_summary_report_count") or 0) > 0:
        required_roles.add("reapproval_apply_plan_report")
    return sorted(required_roles - source_roles)


def _approval_journal_coverage_findings(
    artifact: dict[str, Any],
    summary: dict[str, Any],
    *,
    prefix: str,
    label: str,
) -> list[dict[str, Any]]:
    path = str(artifact.get("path") or "")
    eligible_value = summary.get(f"{prefix}eligible_record_count", summary.get(f"{prefix}record_count"))
    matched_value = summary.get(f"{prefix}matched_record_count")
    missing_value = summary.get(f"{prefix}missing_record_count")
    if eligible_value is None or matched_value is None or missing_value is None:
        return [
            _finding(
                "blocker",
                "authority-approval-journal-coverage-missing",
                f"{label} authority artifact must include approval journal coverage counts.",
                role=artifact.get("role"),
                path=path,
            )
        ]
    eligible = _nonnegative_int_or_none(eligible_value)
    matched = _nonnegative_int_or_none(matched_value)
    missing = _nonnegative_int_or_none(missing_value)
    if eligible is None or matched is None or missing is None:
        return [
            _finding(
                "blocker",
                "authority-approval-journal-coverage-invalid",
                f"{label} approval journal coverage counts must be non-negative integers.",
                role=artifact.get("role"),
                path=path,
                eligible_record_count=eligible_value,
                matched_record_count=matched_value,
                missing_record_count=missing_value,
            )
        ]
    if eligible <= 0 or missing != 0 or matched < eligible:
        return [
            _finding(
                "blocker",
                "authority-approval-journal-coverage-incomplete",
                f"{label} approval journal coverage must have zero missing records and full matched coverage.",
                role=artifact.get("role"),
                path=path,
                eligible_record_count=eligible,
                matched_record_count=matched,
                missing_record_count=missing,
            )
        ]
    return []


def _nonnegative_int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _supersedes_findings(
    authoritative: list[dict[str, Any]],
    superseded: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    authoritative_paths = {str(item.get("path") or "") for item in authoritative}
    authoritative_hashes = {str(item.get("sha256") or "") for item in authoritative if item.get("sha256")}
    for item in superseded:
        path = str(item.get("path") or "")
        if not str(item.get("reason") or "").strip():
            findings.append(_finding("blocker", "superseded-reason-missing", "Every superseded artifact needs a reason.", path=path))
        if path in authoritative_paths:
            findings.append(_finding("blocker", "superseded-artifact-is-authoritative", "An authoritative artifact cannot supersede itself.", path=path))
        if item.get("sha256") and item.get("sha256") in authoritative_hashes:
            findings.append(_finding("warning", "superseded-artifact-same-hash", "Superseded artifact has the same sha256 as an authoritative artifact.", path=path))
    return findings


def _finding(severity: str, code: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"severity": severity, "code": code, "detail": detail, **extra}


def _load_json_object(path: Path) -> dict[str, Any]:
    if path.suffix.lower() != ".json":
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _display_path(root: Path, original_path: Path, candidate: Path) -> str:
    try:
        return str(candidate.resolve().relative_to(root))
    except ValueError:
        return str(original_path)


def _current_repo_commit(root: Path) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        return None
    commit = completed.stdout.decode("utf-8", "replace").strip()
    return commit if len(commit) == 40 else None


def _repo_worktree_state(root: Path) -> dict[str, Any]:
    completed = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        return {"state": "unknown", "dirty": None, "tracked_change_count": None, "untracked_change_count": None}
    lines = [line for line in completed.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    untracked_count = sum(1 for line in lines if line.startswith("??"))
    tracked_count = len(lines) - untracked_count
    return {
        "state": "dirty" if lines else "clean",
        "dirty": bool(lines),
        "tracked_change_count": tracked_count,
        "untracked_change_count": untracked_count,
    }


def _to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# MCP Readiness Authority",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Blocking: {report.get('blocking_count')}",
        f"- Warnings: {report.get('warning_count')}",
        f"- Repo commit: `{report.get('repo_commit') or '-'}`",
        "",
        "## Authoritative Artifacts",
        "",
    ]
    for artifact in report.get("authoritative_artifacts") or []:
        summary = artifact.get("json_summary") if isinstance(artifact.get("json_summary"), dict) else {}
        lines.extend(
            [
                f"- `{artifact.get('role')}`: `{artifact.get('path')}`",
                f"  - sha256: `{artifact.get('sha256') or '-'}`",
                f"  - report type: `{summary.get('report_type') or '-'}`",
                f"  - passed: `{str(summary.get('passed')).lower()}`",
                f"  - blockers/warnings: {summary.get('blocking_count')} / {summary.get('warning_count')}",
            ]
        )
    lines.extend(["", "## Supersedes", ""])
    supersedes = report.get("supersedes") or []
    if supersedes:
        for artifact in supersedes:
            lines.extend(
                [
                    f"- `{artifact.get('path')}`",
                    f"  - sha256: `{artifact.get('sha256') or '-'}`",
                    f"  - reason: {artifact.get('reason') or '-'}",
                ]
            )
    else:
        lines.append("- None.")
    lines.extend(["", "## Findings", ""])
    findings = report.get("findings") or []
    if findings:
        for finding in findings:
            lines.append(f"- {finding.get('severity')} `{finding.get('code')}`: {finding.get('detail')}")
    else:
        lines.append("- None.")
    return "\n".join(lines).rstrip() + "\n"


def _parse_role_path(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("expected ROLE=PATH")
    role, path = value.split("=", 1)
    role = role.strip()
    path = path.strip()
    if not role or not path:
        raise argparse.ArgumentTypeError("expected non-empty ROLE=PATH")
    return role, Path(path)


def _parse_supersedes(value: str) -> tuple[Path, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("expected PATH=REASON")
    path, reason = value.split("=", 1)
    path = path.strip()
    reason = reason.strip()
    if not path:
        raise argparse.ArgumentTypeError("expected non-empty PATH=REASON")
    return Path(path), reason


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the canonical MCP readiness authority manifest.")
    parser.add_argument(
        "--authoritative-artifact",
        action="append",
        type=_parse_role_path,
        default=[],
        help="ROLE=PATH, for example product_readiness=reports/mcp_product_readiness_current.json",
    )
    parser.add_argument(
        "--supersedes",
        action="append",
        type=_parse_supersedes,
        default=[],
        help="PATH=REASON for an older artifact superseded by this authority manifest.",
    )
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_mcp_readiness_authority(
        authoritative_artifacts=args.authoritative_artifact,
        supersedes=args.supersedes,
        repo_root=Path(args.repo_root) if args.repo_root else None,
        out_json=Path(args.out_json) if args.out_json else None,
        out_md=Path(args.out_md) if args.out_md else None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.fail_on_issue and not report["passed"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
