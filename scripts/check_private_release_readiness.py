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

from app.core.config import Settings, get_settings
from app.core.security import api_auth_credentials_configured, authenticate_request, representative_api_auth_credentials
from app.main import readiness_checks


def build_readiness_report(
    settings: Settings | None = None,
    *,
    require_shared_deployment: bool = False,
) -> dict[str, Any]:
    active_settings = settings or get_settings()
    checks = readiness_checks(active_settings)
    if require_shared_deployment:
        checks.extend(_shared_deployment_checks(active_settings))
    failed_check_names = [str(check.get("name", "")) for check in checks if not check.get("passed")]
    passed_check_count = sum(1 for check in checks if check.get("passed"))
    check_count = len(checks)
    readiness_score_percent = round((passed_check_count / check_count) * 100, 1) if check_count else 0.0
    return {
        "report_type": "private_release_readiness",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": _git_commit(PROJECT_ROOT),
        "passed": not failed_check_names,
        "require_shared_deployment": require_shared_deployment,
        "failed_check_names": failed_check_names,
        "readiness_score": {
            "passed_checks": passed_check_count,
            "total_checks": check_count,
            "percent": readiness_score_percent,
            "interpretation": (
                "All checks passed; deployment gate is open."
                if not failed_check_names
                else "This is a diagnostic score, not a deployment approval; every blocking check must pass."
            ),
        },
        "remediation_plan": [
            {
                "check_name": str(check.get("name", "")),
                "category": str(check.get("category", "general")),
                "severity": str(check.get("severity", "blocker")),
                "action": str(check.get("remediation", "Resolve this readiness check in the same runtime environment.")),
            }
            for check in checks
            if not check.get("passed")
        ],
        "execution_context": {
            "project_root_name": PROJECT_ROOT.name,
            "data_dir_configured": bool(active_settings.data_dir),
            "path_details_redacted": True,
            "app_env": active_settings.app_env,
            "same_environment_required": True,
            "same_environment_note": (
                "Run this command in the same environment and with the same mounted DATA_DIR as the API process."
            ),
        },
        "scope": {
            "checks_configuration": True,
            "checks_filesystem_writability": True,
            "checks_database_reachability": False,
            "checks_redis_reachability": False,
            "checks_external_provider_reachability": False,
        },
        "checks": [_redact_readiness_check(check) for check in checks],
    }


def _git_commit(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.decode("utf-8", "replace").strip()


def _shared_deployment_checks(settings: Settings) -> list[dict[str, object]]:
    return [
        {
            "name": "app_env_is_production",
            "passed": settings.app_env.lower() == "production",
            "value": settings.app_env,
            "category": "deployment_configuration",
            "severity": "blocker",
            "remediation": "Set APP_ENV=production in the API container environment.",
        },
        {
            "name": "api_auth_required_for_shared_deployment",
            "passed": settings.api_auth_required is True,
            "category": "deployment_security",
            "severity": "blocker",
            "remediation": "Set API_AUTH_REQUIRED=true for every shared API process.",
        },
        {
            "name": "api_auth_token_nonempty_for_shared_deployment",
            "passed": _api_auth_credentials_configured(settings),
            "category": "deployment_security",
            "severity": "blocker",
            "remediation": "Inject a real API_AUTH_TOKEN or valid API_AUTH_TOKENS secret at deployment time; never commit it.",
        },
        {
            "name": "tenant_storage_isolation_required_for_shared_deployment",
            "passed": settings.tenant_storage_isolation is True,
            "category": "tenant_isolation",
            "severity": "blocker",
            "remediation": "Set TENANT_STORAGE_ISOLATION=true and mount only the isolated runtime data directory.",
        },
        {
            "name": "api_audit_enabled_for_shared_deployment",
            "passed": settings.api_audit_enabled is True,
            "category": "auditability",
            "severity": "blocker",
            "remediation": "Set API_AUDIT_ENABLED=true and persist the API audit directory.",
        },
        {
            **_explicit_tenant_header_required_check(settings),
            "category": "tenant_isolation",
            "severity": "blocker",
            "remediation": "Require X-Tenant-Id on every authenticated shared request and preserve the tenant in the API client configuration.",
        },
        {
            **_tenant_scoped_repository_records_check(settings),
            "category": "tenant_isolation",
            "severity": "blocker",
            "remediation": "Migrate or remove unscoped repository records; do not expose the shared API until every record has tenant_id.",
        },
    ]


def _redact_readiness_check(check: dict[str, object]) -> dict[str, object]:
    redacted: dict[str, object] = {
        "name": check.get("name", ""),
        "passed": bool(check.get("passed")),
    }
    if "value" in check:
        redacted["value"] = check["value"]
    for key in ("category", "severity", "remediation"):
        if key in check:
            redacted[key] = check[key]
    if "error" in check:
        redacted["error_redacted"] = True
    if "unscoped_record_counts" in check:
        redacted["unscoped_record_counts"] = check["unscoped_record_counts"]
    if "repository_files_checked" in check:
        redacted["repository_files_checked"] = check["repository_files_checked"]
    if "tenant_repository_files_checked" in check:
        redacted["tenant_repository_files_checked"] = check["tenant_repository_files_checked"]
    if "tenant_keys_checked" in check:
        redacted["tenant_keys_checked"] = check["tenant_keys_checked"]
    if "status_code" in check:
        redacted["status_code"] = check["status_code"]
    if "policy" in check:
        redacted["policy"] = check["policy"]
    return redacted


def _explicit_tenant_header_required_check(settings: Settings) -> dict[str, object]:
    status_code: int | None = None
    detail_matches = False
    try:
        auth_token, auth_actor = representative_api_auth_credentials(settings)
    except Exception as exc:
        return {
            "name": "explicit_tenant_header_required_for_shared_deployment",
            "passed": False,
            "status_code": status_code,
            "policy": "X-Tenant-Id must be required when tenant storage isolation is enabled.",
            "error": str(exc),
        }
    try:
        authenticate_request(
            settings,
            authorization=f"Bearer {auth_token}",
            actor=auth_actor,
            tenant_id=None,
        )
    except Exception as exc:
        status_code = int(getattr(exc, "status_code", 0) or 0)
        detail_matches = "X-Tenant-Id" in str(getattr(exc, "detail", ""))
    return {
        "name": "explicit_tenant_header_required_for_shared_deployment",
        "passed": status_code == 400 and detail_matches,
        "status_code": status_code,
        "policy": "X-Tenant-Id must be required when tenant storage isolation is enabled.",
    }


def _api_auth_credentials_configured(settings: Settings) -> bool:
    try:
        return api_auth_credentials_configured(settings)
    except Exception:
        return False


def _tenant_scoped_repository_records_check(settings: Settings) -> dict[str, object]:
    counts = {
        "documents": 0,
        "jobs": 0,
        "runs": 0,
        "legacy_documents": 0,
        "legacy_jobs": 0,
    }
    files_checked: list[str] = []
    tenant_files_checked: list[str] = []
    tenant_keys_checked: list[str] = []
    manifest_path = settings.data_dir / "repository" / "manifest.json"
    legacy_path = settings.data_dir / "repository.json"

    if manifest_path.is_file():
        files_checked.append("repository/manifest.json")
        _add_manifest_counts(counts, manifest_path)

    if legacy_path.is_file():
        files_checked.append("repository.json")
        _add_legacy_counts(counts, legacy_path)

    tenants_dir = settings.data_dir / "tenants"
    if settings.tenant_storage_isolation and tenants_dir.is_dir():
        for tenant_dir in sorted(path for path in tenants_dir.iterdir() if path.is_dir()):
            tenant_key = tenant_dir.name
            tenant_had_repository_file = False
            tenant_manifest_path = tenant_dir / "repository" / "manifest.json"
            tenant_legacy_path = tenant_dir / "repository.json"
            if tenant_manifest_path.is_file():
                tenant_files_checked.append(f"tenants/{tenant_key}/repository/manifest.json")
                _add_manifest_counts(counts, tenant_manifest_path)
                tenant_had_repository_file = True
            if tenant_legacy_path.is_file():
                tenant_files_checked.append(f"tenants/{tenant_key}/repository.json")
                _add_legacy_counts(counts, tenant_legacy_path)
                tenant_had_repository_file = True
            if tenant_had_repository_file:
                tenant_keys_checked.append(tenant_key)

    total_unscoped = sum(counts.values())
    return {
        "name": "no_unscoped_repository_records_for_shared_deployment",
        "passed": total_unscoped == 0,
        "unscoped_record_counts": counts,
        "repository_files_checked": files_checked,
        "tenant_repository_files_checked": tenant_files_checked,
        "tenant_keys_checked": tenant_keys_checked,
    }


def _add_manifest_counts(counts: dict[str, int], path: Path) -> None:
    manifest = _read_json_object(path)
    counts["documents"] += _count_unscoped_records(manifest.get("documents"))
    counts["jobs"] += _count_unscoped_records(manifest.get("jobs"))
    counts["runs"] += _count_unscoped_records(manifest.get("runs"))


def _add_legacy_counts(counts: dict[str, int], path: Path) -> None:
    legacy = _read_json_object(path)
    counts["legacy_documents"] += _count_unscoped_records(legacy.get("documents"))
    counts["legacy_jobs"] += _count_unscoped_records(legacy.get("jobs"))


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _count_unscoped_records(records: object) -> int:
    if not isinstance(records, dict):
        return 0
    count = 0
    for record in records.values():
        if isinstance(record, dict) and record.get("tenant_id") in (None, ""):
            count += 1
    return count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check private release readiness for the current environment.")
    parser.add_argument("--require-shared-deployment", action="store_true")
    parser.add_argument("--out-json", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_readiness_report(require_shared_deployment=args.require_shared_deployment)
    stdout_report = dict(report)
    if args.out_json:
        out_json = Path(args.out_json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        stdout_report["out_json"] = {
            "written": True,
            "filename": out_json.name,
            "path_redacted": True,
        }
    print(json.dumps(stdout_report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
