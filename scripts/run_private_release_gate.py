from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.build_private_release_manifest import build_manifest
from scripts.check_github_private_visibility import build_visibility_report
from scripts.check_private_release_readiness import build_readiness_report
from scripts import audit_release_hygiene
from scripts.run_ci_regression_gate import run_regression_gate


REQUIRED_SMOKE_EXPORT_FORMATS = ("jsonl", "csv", "markdown", "tables_jsonl", "tables_csv", "quality_json", "quality_md")
REQUIRED_MCP_HANDOFF_SCHEMA_VERSION = 2
REQUIRED_APPROVAL_REVIEW_EVENT_TYPES = (
    "ai_review_confirmed",
    "approved",
    "human_review_confirmed",
)
REQUIRED_OFFICIAL_RAG_MCP_EVIDENCE_REPORTS = {
    "mcp_transport_smoke": Path("reports/mcp_transport_smoke_current.json"),
    "mcp_index_visibility": Path("reports/mcp_index_visibility_current.json"),
    "mcp_connection_readiness": Path("reports/mcp_connection_readiness_current.json"),
    "mcp_handoff": Path("reports/mcp_handoff_current.json"),
    "mcp_release_evidence_verification": Path("reports/mcp_release_evidence_verification_current.json"),
}
OPTIONAL_SMOKE_EVIDENCE_REPORTS = {
    "secure_rag_smoke": Path("reports/secure_rag_smoke_current.json"),
    "mcp_smoke": Path("reports/mcp_smoke_current.json"),
}


def run_private_release_gate(
    *,
    project_root: Path | None = None,
    workflow_scope: str = "unavailable",
    require_shared_deployment: bool = True,
    allow_dirty_worktree: bool = False,
    dirty_worktree_approval: str | None = None,
    github_remote: str = "origin",
    github_repo: str | None = None,
    smoke_report_path: Path | str | None = None,
    mcp_runtime_data_dir: Path | str | None = None,
    mcp_bundle_dir: Path | str | None = None,
    require_official_rag_mcp_evidence: bool = False,
    require_deploy_ready_mcp_connection: bool = False,
    secure_rag_smoke_report_path: Path | str | None = None,
    mcp_smoke_report_path: Path | str | None = None,
    mcp_transport_smoke_report_path: Path | str | None = None,
    mcp_index_visibility_report_path: Path | str | None = None,
    mcp_connection_readiness_report_path: Path | str | None = None,
    mcp_handoff_report_path: Path | str | None = None,
    release_evidence_verification_report_path: Path | str | None = None,
) -> dict[str, Any]:
    root = audit_release_hygiene.resolve_repo_root(project_root or Path.cwd())
    readiness = build_readiness_report(require_shared_deployment=require_shared_deployment)
    smoke_report = load_private_release_smoke_report(root, smoke_report_path)
    try:
        github_visibility = build_visibility_report(
            repo_root=root,
            repo=github_repo,
            remote_name=github_remote,
        )
    except Exception as exc:
        github_visibility = {
            "passed": False,
            "failed_check_names": ["github_visibility_check_error"],
            "error": str(exc),
        }
    ci_gate = run_regression_gate(
        project_root=root,
        include_release_hygiene=True,
        release_hygiene_workflow_scope=workflow_scope,
        release_hygiene_include_source_path_scan=True,
    )
    manifest = build_manifest(
        root,
        include_release_hygiene_result=True,
        workflow_scope=workflow_scope,
        include_untracked=True,
        include_source_path_scan=True,
    )
    repo_status = manifest.get("repo_status") or {}
    dirty_worktree_approved = allow_dirty_worktree and bool(str(dirty_worktree_approval or "").strip())
    dirty_worktree_passed = (not bool(repo_status.get("dirty"))) or dirty_worktree_approved
    checks = [
        {"name": "private_release_readiness", "passed": bool(readiness.get("passed")), "details": readiness},
        {
            "name": "private_release_smoke",
            "passed": _smoke_report_passed(smoke_report),
            "details": _smoke_report_details(smoke_report),
        },
        {"name": "github_repository_private", "passed": bool(github_visibility.get("passed")), "details": github_visibility},
        {"name": "ci_regression_and_release_hygiene", "passed": bool(ci_gate.get("passed")), "details": ci_gate},
        {
            "name": "clean_worktree_or_approved_dirty_release",
            "passed": dirty_worktree_passed,
            "details": {
                "dirty": bool(repo_status.get("dirty")),
                "changed_path_count": int(repo_status.get("changed_path_count") or 0),
                "approval_reference": str(dirty_worktree_approval).strip() if dirty_worktree_approved else None,
                "approval_required_when_dirty": True,
            },
        },
        {
            "name": "private_release_manifest",
            "passed": (
                manifest.get("manifest_type") == "private_release_handoff"
                and ((manifest.get("release_hygiene") or {}).get("observed_result") or {}).get("exit_code") == 0
            ),
            "details": {
                "manifest_type": manifest.get("manifest_type"),
                "manifest_version": manifest.get("manifest_version"),
                "release_hygiene_exit_code": (
                    (manifest.get("release_hygiene") or {}).get("observed_result") or {}
                ).get("exit_code"),
            },
        },
    ]
    official_evidence_paths = {
        "secure_rag_smoke": secure_rag_smoke_report_path,
        "mcp_smoke": mcp_smoke_report_path,
        "mcp_transport_smoke": mcp_transport_smoke_report_path,
        "mcp_index_visibility": mcp_index_visibility_report_path,
        "mcp_connection_readiness": mcp_connection_readiness_report_path,
        "mcp_handoff": mcp_handoff_report_path,
        "mcp_release_evidence_verification": release_evidence_verification_report_path,
    }
    mcp_runtime_or_bundle_requested = bool(
        str(mcp_runtime_data_dir or "").strip() or str(mcp_bundle_dir or "").strip()
    )
    official_evidence_trigger = (
        "explicit_flag"
        if require_official_rag_mcp_evidence
        else "mcp_runtime_or_bundle"
        if mcp_runtime_or_bundle_requested
        else "mcp_report_path"
        if any(path is not None for path in official_evidence_paths.values())
        else "not_requested"
    )
    official_evidence_requested = official_evidence_trigger != "not_requested"
    if official_evidence_requested:
        official_evidence = load_official_rag_mcp_evidence_reports(
            root,
            report_paths=official_evidence_paths,
            require_deploy_ready_mcp_connection=require_deploy_ready_mcp_connection,
        )
        checks.append(
            {
                "name": "official_rag_mcp_evidence",
                "passed": bool(official_evidence.get("passed")),
                "details": official_evidence,
            }
        )
    failed_check_names = [str(check["name"]) for check in checks if not check["passed"]]
    return {
        "report_type": "private_release_gate",
        "repo_commit": manifest.get("repo_commit"),
        "passed": not failed_check_names,
        "workflow_scope": workflow_scope,
        "require_shared_deployment": require_shared_deployment,
        "allow_dirty_worktree": allow_dirty_worktree,
        "require_official_rag_mcp_evidence": official_evidence_requested,
        "official_rag_mcp_evidence_trigger": official_evidence_trigger,
        "mcp_runtime_data_dir": str(mcp_runtime_data_dir) if mcp_runtime_data_dir else None,
        "mcp_bundle_dir": str(mcp_bundle_dir) if mcp_bundle_dir else None,
        "require_deploy_ready_mcp_connection": require_deploy_ready_mcp_connection if official_evidence_requested else False,
        "check_count": len(checks),
        "failed_check_names": failed_check_names,
        "checks": checks,
        "manifest": manifest,
    }


def load_private_release_smoke_report(root: Path, smoke_report_path: Path | str | None = None) -> dict[str, Any]:
    path = Path(smoke_report_path) if smoke_report_path is not None else Path("reports/private_release_smoke_current.json")
    candidate = path if path.is_absolute() else root / path
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "report_type": "private_release_smoke",
            "passed": False,
            "failed_check_names": ["private_release_smoke_report_readable"],
            "error": str(exc),
        }
    if not isinstance(payload, dict):
        return {
            "report_type": "private_release_smoke",
            "passed": False,
            "failed_check_names": ["private_release_smoke_report_object"],
        }
    return payload


def load_official_rag_mcp_evidence_reports(
    root: Path,
    *,
    report_paths: dict[str, Path | str | None],
    require_deploy_ready_mcp_connection: bool = False,
) -> dict[str, Any]:
    reports = {
        name: _load_json_report(root, report_paths.get(name) or default_path)
        for name, default_path in REQUIRED_OFFICIAL_RAG_MCP_EVIDENCE_REPORTS.items()
    }
    for name, default_path in OPTIONAL_SMOKE_EVIDENCE_REPORTS.items():
        if report_paths.get(name) is not None:
            reports[name] = _load_json_report(root, report_paths.get(name) or default_path)
    validations = {
        "mcp_transport_smoke": _validate_mcp_transport_smoke_report(reports["mcp_transport_smoke"]),
        "mcp_index_visibility": _validate_mcp_index_visibility_report(reports["mcp_index_visibility"]),
        "mcp_connection_readiness": _validate_mcp_connection_readiness_report(
            reports["mcp_connection_readiness"],
            require_deploy_ready=require_deploy_ready_mcp_connection,
        ),
        "mcp_handoff": _validate_mcp_handoff_report(reports["mcp_handoff"]),
        "mcp_release_evidence_verification": _validate_mcp_release_evidence_verification_report(
            reports["mcp_release_evidence_verification"]
        ),
    }
    if "secure_rag_smoke" in reports:
        validations["secure_rag_smoke"] = _validate_secure_rag_smoke_report(reports["secure_rag_smoke"])
    if "mcp_smoke" in reports:
        validations["mcp_smoke"] = _validate_local_mcp_smoke_report(reports["mcp_smoke"])
    _apply_official_rag_mcp_lineage_checks(validations)
    failed_report_names = [name for name, validation in validations.items() if not validation["passed"]]
    return {
        "report_type": "official_rag_mcp_evidence_gate",
        "passed": not failed_report_names,
        "require_deploy_ready_mcp_connection": require_deploy_ready_mcp_connection,
        "required_report_names": list(REQUIRED_OFFICIAL_RAG_MCP_EVIDENCE_REPORTS),
        "optional_report_names": list(OPTIONAL_SMOKE_EVIDENCE_REPORTS),
        "report_count": len(validations),
        "failed_report_names": failed_report_names,
        "reports": validations,
    }


def _apply_official_rag_mcp_lineage_checks(validations: dict[str, dict[str, Any]]) -> None:
    index_summary = validations.get("mcp_index_visibility", {}).get("summary")
    if not isinstance(index_summary, dict):
        return
    expected_tenant_id = str(index_summary.get("tenant_id") or "").strip()
    expected_indexable_records = _optional_int(index_summary.get("total_indexable_record_count"))

    transport = validations.get("mcp_transport_smoke")
    transport_summary = transport.get("summary") if isinstance(transport, dict) else {}
    if isinstance(transport_summary, dict):
        transport_tenant_id = str(transport_summary.get("tenant_id") or "").strip()
        if expected_tenant_id and transport_tenant_id and transport_tenant_id != expected_tenant_id:
            _mark_validation_failure(
                transport,
                "tenant_id_mismatch_with_mcp_index_visibility",
            )

    connection = validations.get("mcp_connection_readiness")
    connection_summary = connection.get("summary") if isinstance(connection, dict) else {}
    if isinstance(connection_summary, dict):
        if connection_summary.get("index_visibility_passed") is None:
            _mark_validation_failure(connection, "mcp_index_visibility_summary")
        connection_tenant_id = str(
            connection_summary.get("index_visibility_tenant_id")
            or connection_summary.get("tenant_id")
            or ""
        ).strip()
        if expected_tenant_id and connection_tenant_id and connection_tenant_id != expected_tenant_id:
            _mark_validation_failure(
                connection,
                "tenant_id_mismatch_with_mcp_index_visibility",
            )
        connection_indexable_records = _optional_int(
            connection_summary.get("index_visibility_total_indexable_records")
        )
        if (
            expected_indexable_records is not None
            and connection_indexable_records is not None
            and connection_indexable_records != expected_indexable_records
        ):
            _mark_validation_failure(
                connection,
                "record_count_mismatch_with_mcp_index_visibility",
            )


def _mark_validation_failure(validation: dict[str, Any] | None, field: str) -> None:
    if not isinstance(validation, dict):
        return
    failed_fields = validation.get("failed_fields")
    if not isinstance(failed_fields, list):
        failed_fields = []
        validation["failed_fields"] = failed_fields
    if field not in failed_fields:
        failed_fields.append(field)
    validation["passed"] = False


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_json_report(root: Path, report_path: Path | str) -> dict[str, Any]:
    path = Path(report_path)
    candidate = path if path.is_absolute() else root / path
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "path": str(path),
            "resolved_path": str(candidate),
            "passed": False,
            "load_error": str(exc),
        }
    if not isinstance(payload, dict):
        return {
            "path": str(path),
            "resolved_path": str(candidate),
            "passed": False,
            "load_error": "JSON artifact must be an object.",
        }
    payload = dict(payload)
    payload["_artifact_path"] = str(path)
    payload["_resolved_artifact_path"] = str(candidate)
    return payload


def _validate_secure_rag_smoke_report(report: dict[str, Any]) -> dict[str, Any]:
    evidence = report.get("evidence_summary") if isinstance(report.get("evidence_summary"), dict) else {}
    failures = _base_report_failures(report, expected_report_type="secure_rag_smoke")
    if evidence.get("passed") is not True:
        failures.append("evidence_summary.passed")
    if report.get("synthetic_runtime") is True:
        failures.append("synthetic_runtime")
    if report.get("handoff_evidence") is not True:
        failures.append("handoff_evidence")
    if int(report.get("search_result_count") or 0) <= 0:
        failures.append("search_result_count")
    if report.get("runtime_ok") is not True:
        failures.append("runtime_ok")
    for field_name in (
        "approval_chain_failure_count",
        "metadata_failure_count",
        "stale_vector_record_count",
        "indexing_job_failure_count",
        "audit_control_failure_count",
    ):
        if int(evidence.get(field_name) or 0) != 0:
            failures.append(f"evidence_summary.{field_name}")
    return {
        "passed": not failures,
        "path": report.get("_artifact_path"),
        "report_type": report.get("report_type"),
        "failed_fields": failures,
        "summary": {
            "passed": report.get("passed"),
            "tenant_id": report.get("tenant_id"),
            "indexing_status": report.get("indexing_status"),
            "search_result_count": report.get("search_result_count"),
            "runtime_ok": report.get("runtime_ok"),
            "synthetic_runtime": report.get("synthetic_runtime"),
            "handoff_evidence": report.get("handoff_evidence"),
            "evidence_passed": evidence.get("passed"),
        },
    }


def _validate_local_mcp_smoke_report(report: dict[str, Any]) -> dict[str, Any]:
    evidence = report.get("evidence_summary") if isinstance(report.get("evidence_summary"), dict) else {}
    failures = _base_report_failures(report, expected_report_type="local_mcp_smoke")
    if evidence.get("passed") is not True:
        failures.append("evidence_summary.passed")
    if report.get("synthetic_runtime") is True:
        failures.append("synthetic_runtime")
    if report.get("handoff_evidence") is not True:
        failures.append("handoff_evidence")
    if int(report.get("search_result_count") or 0) <= 0:
        failures.append("search_result_count")
    if report.get("fetch_has_text") is not True:
        failures.append("fetch_has_text")
    if report.get("citation_has_approved_hash") is not True:
        failures.append("citation_has_approved_hash")
    if int(report.get("document_count") or 0) <= 0:
        failures.append("document_count")
    return {
        "passed": not failures,
        "path": report.get("_artifact_path"),
        "report_type": report.get("report_type"),
        "failed_fields": failures,
        "summary": {
            "passed": report.get("passed"),
            "tenant_id": report.get("tenant_id"),
            "document_count": report.get("document_count"),
            "search_result_count": report.get("search_result_count"),
            "fetch_has_text": report.get("fetch_has_text"),
            "citation_has_approved_hash": report.get("citation_has_approved_hash"),
            "synthetic_runtime": report.get("synthetic_runtime"),
            "handoff_evidence": report.get("handoff_evidence"),
            "evidence_passed": evidence.get("passed"),
        },
    }


def _validate_mcp_transport_smoke_report(report: dict[str, Any]) -> dict[str, Any]:
    full_profile = report.get("full_profile") if isinstance(report.get("full_profile"), dict) else {}
    preparation = report.get("preparation") if isinstance(report.get("preparation"), dict) else {}
    failures = _base_report_failures(report, expected_report_type="mcp_transport_smoke")
    if full_profile.get("passed") is not True:
        failures.append("full_profile.passed")
    if int(full_profile.get("search_result_count") or 0) <= 0:
        failures.append("full_profile.search_result_count")
    if full_profile.get("fetch_has_text") is not True:
        failures.append("full_profile.fetch_has_text")
    if preparation.get("skipped") is not True and preparation.get("passed") is not True:
        failures.append("preparation.passed")
    if preparation.get("skipped") is not True and preparation.get("evidence_passed") is not True:
        failures.append("preparation.evidence_passed")
    if preparation.get("skipped") is not True and preparation.get("synthetic_runtime") is True:
        failures.append("preparation.synthetic_runtime")
    if preparation.get("skipped") is not True and preparation.get("handoff_evidence") is not True:
        failures.append("preparation.handoff_evidence")
    return {
        "passed": not failures,
        "path": report.get("_artifact_path"),
        "report_type": report.get("report_type"),
        "failed_fields": failures,
        "summary": {
            "passed": report.get("passed"),
            "tenant_id": report.get("tenant_id"),
            "transport": report.get("transport"),
            "preparation_passed": preparation.get("passed"),
            "preparation_skipped": preparation.get("skipped"),
            "preparation_synthetic_runtime": preparation.get("synthetic_runtime"),
            "preparation_handoff_evidence": preparation.get("handoff_evidence"),
            "full_profile_passed": full_profile.get("passed"),
            "search_result_count": full_profile.get("search_result_count"),
            "fetch_has_text": full_profile.get("fetch_has_text"),
        },
    }


def _validate_mcp_index_visibility_report(report: dict[str, Any]) -> dict[str, Any]:
    guard = report.get("preapproval_visibility_guard") if isinstance(report.get("preapproval_visibility_guard"), dict) else {}
    provenance = (
        report.get("approval_provenance_coverage")
        if isinstance(report.get("approval_provenance_coverage"), dict)
        else {}
    )
    journal = report.get("approval_journal_coverage") if isinstance(report.get("approval_journal_coverage"), dict) else {}
    failures = _base_report_failures(report, expected_report_type="mcp_index_visibility_audit")
    if int(report.get("total_approved_chunks") or 0) <= 0:
        failures.append("total_approved_chunks")
    if int(report.get("total_mcp_visible_records") or 0) <= 0:
        failures.append("total_mcp_visible_records")
    if int(report.get("smoke_like_document_count") or 0) != 0:
        failures.append("smoke_like_document_count")
    if guard.get("passed") is not True:
        failures.append("preapproval_visibility_guard.passed")
    if int(journal.get("missing_record_count") or 0) != 0:
        failures.append("approval_journal_coverage.missing_record_count")
    provenance_record_count = int(provenance.get("record_count") or 0)
    if provenance_record_count <= 0:
        failures.append("approval_provenance_coverage.record_count")
    if int(provenance.get("complete_record_count") or 0) != provenance_record_count:
        failures.append("approval_provenance_coverage.complete_record_count")
    return {
        "passed": not failures,
        "path": report.get("_artifact_path"),
        "report_type": report.get("report_type"),
        "failed_fields": failures,
        "summary": {
            "passed": report.get("passed"),
            "tenant_id": report.get("tenant_id"),
            "document_count": report.get("document_count"),
            "total_approved_chunks": report.get("total_approved_chunks"),
            "total_mcp_visible_records": report.get("total_mcp_visible_records"),
            "smoke_like_document_count": report.get("smoke_like_document_count"),
            "preapproval_visibility_guard_passed": guard.get("passed"),
            "approval_journal_missing_record_count": journal.get("missing_record_count"),
            "approval_provenance_complete_record_count": provenance.get("complete_record_count"),
            "approval_provenance_record_count": provenance.get("record_count"),
        },
    }


def _validate_mcp_connection_readiness_report(
    report: dict[str, Any],
    *,
    require_deploy_ready: bool,
) -> dict[str, Any]:
    index_summary = (
        report.get("mcp_index_visibility_summary")
        if isinstance(report.get("mcp_index_visibility_summary"), dict)
        else {}
    )
    failures = _base_report_failures(report, expected_report_type="mcp_connection_readiness")
    if require_deploy_ready and report.get("deploy_ready") is not True:
        failures.append("deploy_ready")
    if index_summary and index_summary.get("passed") is not True:
        failures.append("mcp_index_visibility_summary.passed")
    return {
        "passed": not failures,
        "path": report.get("_artifact_path"),
        "report_type": report.get("report_type"),
        "failed_fields": failures,
        "summary": {
            "passed": report.get("passed"),
            "deploy_ready": report.get("deploy_ready"),
            "readiness_scope": report.get("readiness_scope"),
            "client_profile": report.get("client_profile"),
            "connection_mode": report.get("connection_mode"),
            "transport": report.get("transport"),
            "tenant_id": report.get("tenant_id"),
            "data_dir": report.get("data_dir"),
            "effective_data_dir": report.get("effective_data_dir"),
            "bundle_dir": report.get("bundle_dir"),
            "high_count": report.get("high_count"),
            "medium_count": report.get("medium_count"),
            "index_visibility_passed": index_summary.get("passed"),
            "index_visibility_tenant_id": index_summary.get("tenant_id"),
            "index_visibility_total_mcp_visible_records": index_summary.get("total_mcp_visible_records"),
            "index_visibility_total_indexable_records": index_summary.get("total_indexable_record_count"),
        },
    }


def _validate_mcp_handoff_report(report: dict[str, Any]) -> dict[str, Any]:
    failures = _base_report_failures(report, expected_report_type="mcp_handoff_report")
    schema_version = _optional_int(report.get("handoff_schema_version"))
    if schema_version != REQUIRED_MCP_HANDOFF_SCHEMA_VERSION:
        failures.append("handoff_schema_version")
    if report.get("handoff_ready") is not True:
        failures.append("handoff_ready")
    if int(report.get("blocking_count") or 0) != 0:
        failures.append("blocking_count")
    if int(report.get("warning_count") or 0) != 0:
        failures.append("warning_count")
    approval_journal = (
        (report.get("mcp_index_visibility_summary") or {}).get("approval_journal_coverage")
        if isinstance(report.get("mcp_index_visibility_summary"), dict)
        else {}
    )
    if isinstance(approval_journal, dict) and int(approval_journal.get("missing_record_count") or 0) != 0:
        failures.append("mcp_index_visibility_summary.approval_journal_coverage.missing_record_count")
    product = report.get("product_summary") if isinstance(report.get("product_summary"), dict) else {}
    review_events = (
        product.get("approval_journal_review_event_coverage")
        if isinstance(product.get("approval_journal_review_event_coverage"), dict)
        else None
    )
    review_event_failures, review_event_summary = _handoff_review_event_coverage_failures(review_events)
    failures.extend(review_event_failures)
    return {
        "passed": not failures,
        "path": report.get("_artifact_path"),
        "report_type": report.get("report_type"),
        "failed_fields": failures,
        "summary": {
            "passed": report.get("passed"),
            "handoff_ready": report.get("handoff_ready"),
            "decision": report.get("decision"),
            "blocking_count": report.get("blocking_count"),
            "warning_count": report.get("warning_count"),
            "server_name": report.get("server_name"),
            "handoff_schema_version": report.get("handoff_schema_version"),
            **review_event_summary,
        },
    }


def _handoff_review_event_coverage_failures(
    coverage: dict[str, Any] | None,
) -> tuple[list[str], dict[str, Any]]:
    base_field = "product_summary.approval_journal_review_event_coverage"
    if not isinstance(coverage, dict):
        return [base_field], {
            "approval_journal_review_event_incomplete_record_count": None,
            "approval_journal_review_event_missing_chunk_counts": None,
        }
    failures: list[str] = []
    status = _review_event_coverage_status(coverage)
    if status["malformed_count_fields"]:
        failures.append(f"{base_field}.counts")
    if status["missing_required_event_types"]:
        failures.append(f"{base_field}.required_event_types")
    if status["incomplete_record_count"] > 0 or any(
        count > 0 for count in status["computed_missing_event_chunk_counts"].values()
    ):
        failures.append(f"{base_field}.missing_event_chunk_counts")
    return failures, {
        "approval_journal_review_event_incomplete_record_count": status["incomplete_record_count"],
        "approval_journal_review_event_missing_chunk_counts": status["computed_missing_event_chunk_counts"],
        "approval_journal_review_event_missing_required_event_types": status["missing_required_event_types"],
        "approval_journal_review_event_malformed_count_fields": status["malformed_count_fields"],
    }


def _review_event_coverage_status(coverage: dict[str, Any]) -> dict[str, Any]:
    expected_raw = coverage.get("expected_event_chunk_counts")
    observed_raw = coverage.get("event_chunk_counts")
    missing_raw = coverage.get("missing_event_chunk_counts")
    expected, expected_bad = _strict_int_dict(expected_raw)
    observed, observed_bad = _strict_int_dict(observed_raw)
    missing, missing_bad = _strict_int_dict(missing_raw)
    incomplete = _strict_int(coverage.get("incomplete_record_count"))
    malformed_fields: list[str] = []
    if not isinstance(expected_raw, dict):
        malformed_fields.append("expected_event_chunk_counts")
    malformed_fields.extend(f"expected_event_chunk_counts.{key}" for key in expected_bad)
    if not isinstance(observed_raw, dict):
        malformed_fields.append("event_chunk_counts")
    malformed_fields.extend(f"event_chunk_counts.{key}" for key in observed_bad)
    if not isinstance(missing_raw, dict):
        malformed_fields.append("missing_event_chunk_counts")
    malformed_fields.extend(f"missing_event_chunk_counts.{key}" for key in missing_bad)
    if incomplete is None:
        malformed_fields.append("incomplete_record_count")
    missing_types = [
        event_type
        for event_type in REQUIRED_APPROVAL_REVIEW_EVENT_TYPES
        if event_type not in expected or event_type not in observed or event_type not in missing
    ]
    computed_missing = {
        event_type: max(0, expected.get(event_type, 0) - observed.get(event_type, 0))
        for event_type in REQUIRED_APPROVAL_REVIEW_EVENT_TYPES
    }
    mismatched_precomputed = [
        event_type
        for event_type, count in computed_missing.items()
        if event_type in missing and missing[event_type] != count
    ]
    malformed_fields.extend(
        f"missing_event_chunk_counts.{event_type}.mismatch"
        for event_type in mismatched_precomputed
    )
    return {
        "incomplete_record_count": incomplete if incomplete is not None else 0,
        "computed_missing_event_chunk_counts": computed_missing,
        "missing_required_event_types": missing_types,
        "malformed_count_fields": sorted(set(malformed_fields)),
    }


def _strict_int_dict(value: Any) -> tuple[dict[str, int], list[str]]:
    if not isinstance(value, dict):
        return {}, []
    parsed: dict[str, int] = {}
    malformed: list[str] = []
    for key, raw_count in value.items():
        count = _strict_int(raw_count)
        if count is None:
            malformed.append(str(key))
        else:
            parsed[str(key)] = count
    return parsed, malformed


def _strict_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        count = int(str(value))
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def _validate_mcp_release_evidence_verification_report(report: dict[str, Any]) -> dict[str, Any]:
    failures = _base_report_failures(report, expected_report_type="release_evidence_bundle_verification")
    if report.get("evidence_profile") != "mcp-product-readiness":
        failures.append("evidence_profile")
    if int(report.get("failure_count") or 0) != 0:
        failures.append("failure_count")
    return {
        "passed": not failures,
        "path": report.get("_artifact_path"),
        "report_type": report.get("report_type"),
        "failed_fields": failures,
        "summary": {
            "passed": report.get("passed"),
            "evidence_profile": report.get("evidence_profile"),
            "artifact_count": report.get("artifact_count"),
            "failure_count": report.get("failure_count"),
            "warning_count": report.get("warning_count"),
        },
    }


def _base_report_failures(report: dict[str, Any], *, expected_report_type: str) -> list[str]:
    failures: list[str] = []
    if report.get("load_error"):
        failures.append("load_error")
        return failures
    if report.get("report_type") != expected_report_type:
        failures.append("report_type")
    if report.get("passed") is not True:
        failures.append("passed")
    return failures


def _smoke_report_passed(report: dict[str, Any]) -> bool:
    http = report.get("http") if isinstance(report.get("http"), dict) else {}
    audit = report.get("audit") if isinstance(report.get("audit"), dict) else {}
    return (
        report.get("report_type") == "private_release_smoke"
        and report.get("passed") is True
        and http.get("unauthorized_upload_status_code") in {401, 403}
        and http.get("missing_tenant_upload_status_code") == 400
        and http.get("authorized_upload_status_code") == 200
        and audit.get("passed") is True
        and audit.get("auth_denial_passed") is True
        and audit.get("tenant_header_required_passed") is True
        and report.get("data_dir_mode") == "explicit"
        and report.get("handoff_evidence") is True
        and not _missing_required_smoke_exports(report)
    )


def _smoke_report_details(report: dict[str, Any]) -> dict[str, Any]:
    http = report.get("http") if isinstance(report.get("http"), dict) else {}
    audit = report.get("audit") if isinstance(report.get("audit"), dict) else {}
    observed_export_formats = sorted(_successful_smoke_export_formats(report))
    missing_export_formats = _missing_required_smoke_exports(report)
    return {
        "report_type": report.get("report_type"),
        "passed": report.get("passed"),
        "sample_filename": report.get("sample_filename"),
        "data_dir_name": report.get("data_dir_name"),
        "data_dir_mode": report.get("data_dir_mode"),
        "handoff_evidence": report.get("handoff_evidence"),
        "tenant_id": report.get("tenant_id"),
        "http": {
            "unauthorized_upload_status_code": http.get("unauthorized_upload_status_code"),
            "missing_tenant_upload_status_code": http.get("missing_tenant_upload_status_code"),
            "authorized_upload_status_code": http.get("authorized_upload_status_code"),
        },
        "audit": {
            "passed": audit.get("passed"),
            "auth_denial_passed": audit.get("auth_denial_passed"),
            "tenant_header_required_passed": audit.get("tenant_header_required_passed"),
            "record_count": audit.get("record_count"),
        },
        "required_export_formats": list(REQUIRED_SMOKE_EXPORT_FORMATS),
        "observed_export_formats": observed_export_formats,
        "missing_export_formats": missing_export_formats,
        "failed_check_names": report.get("failed_check_names"),
    }


def _successful_smoke_export_formats(report: dict[str, Any]) -> set[str]:
    exports = report.get("exports")
    if not isinstance(exports, list):
        return set()
    successful: set[str] = set()
    for item in exports:
        if not isinstance(item, dict):
            continue
        if item.get("status_code") == 200 and item.get("exists") is True:
            export_format = str(item.get("format") or "").strip()
            if export_format:
                successful.add(export_format)
    return successful


def _missing_required_smoke_exports(report: dict[str, Any]) -> list[str]:
    observed = _successful_smoke_export_formats(report)
    return [export_format for export_format in REQUIRED_SMOKE_EXPORT_FORMATS if export_format not in observed]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the private release pre-push gate.")
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--workflow-scope", choices=("auto", "available", "unavailable"), default="unavailable")
    parser.add_argument(
        "--require-shared-deployment",
        action="store_true",
        default=None,
        help="Require protected shared-deployment settings. This is the default.",
    )
    parser.add_argument(
        "--allow-local-deployment",
        action="store_true",
        help="Explicitly allow local/demo settings instead of protected shared-deployment settings.",
    )
    parser.add_argument(
        "--allow-dirty-worktree",
        action="store_true",
        help="Allow a dirty worktree only when --dirty-worktree-approval is also supplied.",
    )
    parser.add_argument("--dirty-worktree-approval", default=None)
    parser.add_argument("--github-remote", default="origin")
    parser.add_argument("--github-repo", default=None)
    parser.add_argument("--smoke-report", default=None)
    parser.add_argument(
        "--mcp-runtime-data-dir",
        default=None,
        help="MCP runtime data directory included in the release; automatically requires official RAG/MCP evidence.",
    )
    parser.add_argument(
        "--mcp-bundle-dir",
        default=None,
        help="MCP client connection bundle included in the release; automatically requires official RAG/MCP evidence.",
    )
    parser.add_argument(
        "--require-official-rag-mcp-evidence",
        action="store_true",
        help=(
            "Require official approved-RAG/MCP handoff evidence reports: MCP transport smoke against an existing "
            "runtime, MCP index visibility, MCP connection readiness, MCP handoff, and mcp-product-readiness "
            "release evidence verification. Optional secure/local smoke reports are rejected when synthetic."
        ),
    )
    parser.add_argument(
        "--require-deploy-ready-mcp-connection",
        action="store_true",
        help="When official RAG/MCP evidence is required, also require MCP connection readiness deploy_ready=true.",
    )
    parser.add_argument("--secure-rag-smoke-report", default=None)
    parser.add_argument("--mcp-smoke-report", default=None)
    parser.add_argument("--mcp-transport-smoke-report", default=None)
    parser.add_argument("--mcp-index-visibility-report", default=None)
    parser.add_argument("--mcp-connection-readiness-report", default=None)
    parser.add_argument("--mcp-handoff-report", default=None)
    parser.add_argument(
        "--mcp-release-evidence-verification-report",
        "--release-evidence-verification-report",
        dest="release_evidence_verification_report",
        default=None,
        help="MCP product-readiness release evidence verification JSON.",
    )
    parser.add_argument("--out-json", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = audit_release_hygiene.resolve_repo_root(Path(args.project_root) if args.project_root else Path.cwd())
    report = run_private_release_gate(
        project_root=root,
        workflow_scope=args.workflow_scope,
        require_shared_deployment=False if args.allow_local_deployment else True,
        allow_dirty_worktree=args.allow_dirty_worktree,
        dirty_worktree_approval=args.dirty_worktree_approval,
        github_remote=args.github_remote,
        github_repo=args.github_repo,
        smoke_report_path=args.smoke_report,
        mcp_runtime_data_dir=args.mcp_runtime_data_dir,
        mcp_bundle_dir=args.mcp_bundle_dir,
        require_official_rag_mcp_evidence=args.require_official_rag_mcp_evidence,
        require_deploy_ready_mcp_connection=args.require_deploy_ready_mcp_connection,
        secure_rag_smoke_report_path=args.secure_rag_smoke_report,
        mcp_smoke_report_path=args.mcp_smoke_report,
        mcp_transport_smoke_report_path=args.mcp_transport_smoke_report,
        mcp_index_visibility_report_path=args.mcp_index_visibility_report,
        mcp_connection_readiness_report_path=args.mcp_connection_readiness_report,
        mcp_handoff_report_path=args.mcp_handoff_report,
        release_evidence_verification_report_path=args.release_evidence_verification_report,
    )
    if args.out_json:
        out_json = Path(args.out_json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["out_json"] = str(out_json)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
