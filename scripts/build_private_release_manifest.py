from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import audit_release_hygiene


def build_manifest(
    project_root: Path | None = None,
    *,
    include_release_hygiene_result: bool = False,
    workflow_scope: str = "unavailable",
    include_untracked: bool = False,
    include_source_path_scan: bool = False,
) -> dict[str, Any]:
    root = audit_release_hygiene.resolve_repo_root(project_root or Path.cwd())
    generated_at = datetime.now(timezone.utc).isoformat()
    source_scan_flag = " --include-source-path-scan" if include_source_path_scan else ""
    untracked_flag = " --include-untracked" if include_untracked else ""
    release_hygiene_result = None
    if include_release_hygiene_result:
        release_hygiene_result = _release_hygiene_result(
            root,
            workflow_scope=workflow_scope,
            include_untracked=include_untracked,
            include_source_path_scan=include_source_path_scan,
            observed_at=generated_at,
        )
    return {
        "manifest_type": "private_release_handoff",
        "manifest_version": 1,
        "generated_at": generated_at,
        "repo_commit": _git_output(root, ["rev-parse", "HEAD"]),
        "release_ref": _git_output(root, ["rev-parse", "--abbrev-ref", "HEAD"]),
        "repo_status": _repo_status(root),
        "project_root_name": root.name,
        "auth_tenant_defaults": {
            "app_env": _attestation("production", ".env.example"),
            "api_auth_required": _attestation(True, ".env.example"),
            "api_auth_credentials_required": _attestation("API_AUTH_TOKEN_or_API_AUTH_TOKENS", "app/core/security.py"),
            "api_auth_token_legacy_admin_supported": _attestation(True, "app/core/security.py"),
            "api_auth_tokens_rbac_supported": _attestation(True, "app/core/security.py"),
            "api_default_tenant_id": _attestation("default", ".env.example"),
            "auth_headers_supported": _attestation(["Authorization: Bearer <token>", "X-API-Key"], "app/core/security.py"),
            "x_actor_required_when_auth_enabled": _attestation(True, "app/core/security.py"),
            "x_tenant_id_runtime_fallback_scope": _attestation(
                "local_or_non_isolated_single_tenant_only",
                "app/core/security.py",
            ),
            "x_tenant_id_required_when_tenant_storage_isolation_enabled": _attestation(
                True,
                "app/core/security.py",
            ),
            "x_tenant_id_required_for_shared_public_institution_pilots": _attestation(True, "README.md"),
            "api_default_tenant_id_scope": _attestation("single_tenant_local_demo_only", "README.md"),
            "tenant_storage_isolation": _attestation(True, ".env.example"),
            "tenant_storage_path_pattern": _attestation("DATA_DIR/tenants/<safe-tenant-id>/", "app/core/tenant_access.py"),
            "cross_tenant_access_behavior": _attestation("404_not_found", "app/api/routes_documents.py"),
            "api_audit_enabled": _attestation(True, ".env.example"),
            "api_audit_base_path": _attestation("DATA_DIR/repository/api_audit.jsonl", "app/core/api_audit.py"),
            "api_audit_tenant_scoped_path": _attestation(
                "DATA_DIR/tenants/<safe-tenant-id>/repository/api_audit.jsonl",
                "app/core/tenant_access.py",
            ),
            "api_audit_path_scope": _attestation(
                "auth denials may be written to the base audit log; authenticated tenant actions are written to the tenant-scoped audit log when tenant storage isolation is enabled",
                "scripts/run_private_release_smoke.py",
            ),
            "api_audit_required_fields": _attestation(
                ["actor", "tenant_id", "auth_mode", "action", "outcome", "status_code"],
                "app/core/api_audit.py",
            ),
        },
        "deployment": {
            "shared_api_service": _attestation("api", "docker-compose.yml"),
            "readiness_endpoint": _attestation("/ready", "app/main.py"),
            "liveness_endpoint": _attestation("/health", "app/main.py"),
            "readiness_scope": _attestation(
                "configuration_and_filesystem_writability_not_database_redis_or_provider_reachability",
                "docs/private_release_operator_notes.md",
            ),
            "readiness_auth_required": _attestation(False, "docs/private_release_operator_notes.md"),
        },
        "streamlit_local_profile": {
            "profile_name": _attestation("local-ui", "docker-compose.yml"),
            "enabled_by_default": _attestation(False, "docker-compose.yml"),
            "compose_start_command": _attestation(
                "docker compose --profile local-ui up --build streamlit",
                "README.md",
            ),
            "container_bind_address": _attestation("0.0.0.0:8501", "docker-compose.yml"),
            "host_publish_address": _attestation("127.0.0.1:8501", "docker-compose.yml"),
            "app_env": _attestation("local", "docker-compose.yml"),
            "api_auth_required": _attestation(False, "docker-compose.yml"),
            "tenant_storage_isolation": _attestation(False, "docker-compose.yml"),
            "runtime_guard_condition": _attestation(
                "settings.api_auth_required or settings.tenant_storage_isolation",
                "frontend/streamlit_app.py",
            ),
            "runtime_guard_message": _attestation(
                "Streamlit is disabled for protected or tenant-isolated deployments.",
                "frontend/streamlit_app.py",
            ),
            "allowed_for_shared_or_protected_use": _attestation(False, "frontend/streamlit_app.py"),
        },
        "processing_contract": {
            "process_endpoint": _attestation("POST /api/documents/{document_id}/process", "app/api/routes_documents.py"),
            "process_mode": _attestation("synchronous", "app/api/routes_documents.py"),
            "process_returns_completed_or_failed_job_inline": _attestation(True, "app/api/routes_documents.py"),
            "jobs_endpoint": _attestation("GET /api/jobs/{job_id}", "app/api/routes_jobs.py"),
            "jobs_endpoint_semantics": _attestation(
                "stored_record_lookup_not_background_queue_progress",
                "app/api/routes_jobs.py",
            ),
            "background_queue_supported": _attestation(False, "app/api/routes_documents.py"),
            "required_export_formats": _attestation(
                ["jsonl", "csv", "markdown", "tables_jsonl", "tables_csv", "quality_json", "quality_md"],
                "README.md",
            ),
        },
        "exports": {
            "chunk_formats": ["jsonl", "csv", "markdown"],
            "table_formats": ["tables_jsonl", "tables_csv"],
            "quality_formats": ["quality_json", "quality_md"],
        },
        "release_hygiene": {
            "console_command": _command_attestation(
                f"reg-rag-audit-release{untracked_flag}{source_scan_flag} --workflow-scope {workflow_scope}",
                generated_at,
            ),
            "script_command": _command_attestation(
                f"python scripts/audit_release_hygiene.py{untracked_flag}{source_scan_flag} --workflow-scope {workflow_scope}",
                generated_at,
            ),
            "workflow_scope_mode": _attestation(workflow_scope, "docs/private_release_checklist.md"),
            "include_untracked_used": _attestation(include_untracked, "scripts/audit_release_hygiene.py"),
            "include_source_path_scan_used": _attestation(include_source_path_scan, "scripts/audit_release_hygiene.py"),
            "max_file_bytes": _attestation(10 * 1024 * 1024, "scripts/audit_release_hygiene.py"),
            "max_scan_bytes": _attestation(1024 * 1024, "scripts/audit_release_hygiene.py"),
            "observed_result": release_hygiene_result,
        },
        "release_gates": {
            "private_release_readiness": _command_attestation(
                "python scripts/check_private_release_readiness.py --require-shared-deployment",
                generated_at,
            ),
            "installed_private_release_readiness": _command_attestation(
                "reg-rag-check-private-release --require-shared-deployment",
                generated_at,
            ),
            "ci_gate": _command_attestation(
                "python scripts/run_ci_regression_gate.py --include-source-path-scan --workflow-scope unavailable",
                generated_at,
            ),
            "installed_ci_gate": _command_attestation(
                "reg-rag-ci-gate --include-source-path-scan --workflow-scope unavailable",
                generated_at,
            ),
            "release_hygiene_with_untracked": _command_attestation(
                "python scripts/audit_release_hygiene.py --include-untracked --include-source-path-scan --workflow-scope unavailable",
                generated_at,
            ),
            "nightly_smoke": _command_attestation(
                "python scripts/run_nightly_smoke.py --fail-on-smoke-failure",
                generated_at,
            ),
        },
        "provider_posture": {
            "enable_agent_review": _attestation(False, ".env.example"),
            "agent_review_mode": _attestation("plan_only_when_enabled", "README.md"),
            "agent_review_expected_api_call_count": _attestation(0, "README.md"),
            "llm_provider_config_informational_only_when_disabled": _attestation(True, ".env.example"),
            "openai_api_key_configured": _attestation(False, ".env.example"),
            "azure_openai_endpoint_configured": _attestation(False, ".env.example"),
            "azure_openai_api_key_configured": _attestation(False, ".env.example"),
            "embedding_adapter": _attestation("local-hash-embedding-v1", "README.md"),
            "embedding_expected_api_call_count": _attestation(0, "README.md"),
            "ocr_mode": _attestation("manifest_only_until_approved_provider_adapter", "README.md"),
            "ocr_expected_api_call_count": _attestation(0, "README.md"),
            "provider_executor_wired": _attestation(False, "README.md"),
            "network_provider_calls_allowed_by_default": _attestation(False, "README.md"),
            "billable_provider_required_controls": [
                "explicit_budget",
                "approval_reference",
                "price_version",
                "preflight_reservation",
                "append_only_execution_audit",
            ],
        },
        "docs": [
            "README.md",
            "docs/private_release_checklist.md",
            "docs/private_release_operator_notes.md",
        ],
    }


def _attestation(value: Any, evidence_source: str) -> dict[str, Any]:
    return {"value": value, "evidence_source": evidence_source}


def _command_attestation(command: str, observed_at: str) -> dict[str, str]:
    return {"command": command, "observed_at": observed_at, "evidence_source": "declared_release_command"}


def _git_output(root: Path, args: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.decode("utf-8", "replace").strip()


def _repo_status(root: Path) -> dict[str, Any]:
    short_status = _git_output(root, ["status", "--short"])
    lines = [line for line in short_status.splitlines() if line.strip()]
    return {
        "dirty": bool(lines),
        "changed_path_count": len(lines),
        "changed_paths_preview": lines[:50],
    }


def _release_hygiene_result(
    root: Path,
    *,
    workflow_scope: str,
    include_untracked: bool,
    include_source_path_scan: bool,
    observed_at: str,
) -> dict[str, Any]:
    command = "python scripts/audit_release_hygiene.py"
    if include_untracked:
        command += " --include-untracked"
    if include_source_path_scan:
        command += " --include-source-path-scan"
    command += f" --workflow-scope {workflow_scope}"
    try:
        candidate_paths = audit_release_hygiene.collect_candidate_paths(root, include_untracked=include_untracked)
        findings = audit_release_hygiene.audit_paths(
            root,
            candidate_paths,
            workflow_scope_unavailable=audit_release_hygiene.workflow_scope_is_unavailable(workflow_scope),
            include_source_path_scan=include_source_path_scan,
        )
        filtered_findings = audit_release_hygiene.filter_allowed_findings(
            findings,
            audit_release_hygiene.load_allowlist(root / audit_release_hygiene.DEFAULT_ALLOWLIST_FILENAME),
        )
    except audit_release_hygiene.AuditError as exc:
        return {
            "command": command,
            "observed_at": observed_at,
            "exit_code": 2,
            "finding_count": 0,
            "findings": [],
            "error": str(exc),
        }

    return {
        "command": command,
        "observed_at": observed_at,
        "exit_code": 1 if filtered_findings else 0,
        "finding_count": len(filtered_findings),
        "suppressed_finding_count": len(findings) - len(filtered_findings),
        "findings": [finding.to_dict() for finding in filtered_findings[:50]],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a private release handoff manifest.")
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--include-release-hygiene-result", action="store_true")
    parser.add_argument("--workflow-scope", choices=("auto", "available", "unavailable"), default="unavailable")
    parser.add_argument("--include-untracked", action="store_true")
    parser.add_argument("--include-source-path-scan", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.project_root) if args.project_root else Path.cwd()
    manifest = build_manifest(
        root,
        include_release_hygiene_result=args.include_release_hygiene_result,
        workflow_scope=args.workflow_scope,
        include_untracked=args.include_untracked,
        include_source_path_scan=args.include_source_path_scan,
    )
    if args.out_json:
        out_json = Path(args.out_json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest["out_json"] = str(out_json)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
