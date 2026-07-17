from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO


SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
COMMIT_RE = re.compile(r"^[a-f0-9]{40}$")
HASH_CHUNK_BYTES = 1024 * 1024
EVIDENCE_PROFILES = frozenset({"private-release", "hermes-mcp", "mcp-product-readiness"})
PRIVATE_REQUIRED_REPORT_ARTIFACTS = frozenset(
    {
        "reports/private_release_gate_current.json",
        "reports/private_release_readiness_current.json",
        "reports/private_release_manifest_current.json",
        "reports/github_private_visibility_current.json",
        "reports/release_hygiene_current.json",
        "reports/private_release_smoke_current.json",
    }
)
HERMES_MCP_REQUIRED_ARTIFACTS = frozenset(
    {
        "reports/hermes_mcp_check_current.json",
        "reports/hermes_mcp_check_current.md",
        "reports/installed_console_scripts_hermes.json",
        "reports/mcp_smoke_hermes.json",
        "reports/mcp_transport_smoke_hermes.json",
        "reports/mcp_client_bundle_hermes.json",
        "reports/mcp_connection_bundle_hermes.zip",
        "reports/mcp_connection_readiness_bundle_hermes.json",
        "reports/mcp_connection_readiness_chatgpt_https_hermes.json",
        "reports/mcp_connection_readiness_chatgpt_tunnel_hermes.json",
    }
)
MCP_PRODUCT_READINESS_REQUIRED_ARTIFACTS = frozenset(
    {
        "reports/mcp_readiness_authority_current.json",
        "reports/mcp_readiness_authority_current.md",
        "reports/mcp_product_readiness_current.json",
        "reports/mcp_product_readiness_current.md",
        "reports/table_review_source_traceability_current.json",
        "reports/table_review_source_traceability_current.md",
        "reports/parsing_goldset_table_drift_check_current.json",
        "reports/parsing_goldset_table_drift_check_current.md",
        "reports/table_preprocessing_claim_gate_current.json",
        "reports/table_preprocessing_claim_gate_current.md",
        "reports/mcp_demo_answers_current.json",
        "reports/simple_rag_vs_mcp_accuracy_current.json",
        "reports/mcp_transport_smoke_current.json",
        "reports/mcp_index_visibility_current.json",
        "reports/mcp_connection_readiness_current.json",
        "reports/mcp_handoff_current.json",
        "reports/aks_mcp_publish_runtime_report.json",
        "data/aks_mcp_publish_runtime/reports/aks_mcp_publish_tenant-aks-publish_approval_worklist.json",
        "data/aks_mcp_publish_runtime/reports/aks_mcp_publish_tenant-aks-publish_approval_review_batches.json",
        "reports/approval_worklist_current.json",
        "reports/approval_worklist_current.md",
        "reports/approval_review_batches_current.json",
        "reports/approval_review_batches_current.md",
        "reports/reapproval_worklist_current.json",
        "reports/reapproval_worklist_current_chunks.csv",
        "reports/reapproval_worklist_current_chunks.json",
        "reports/reapproval_worklist_current.md",
        "reports/reapproval_review_batches_current.json",
        "reports/reapproval_review_batches_current.csv",
        "reports/reapproval_review_batches_current.md",
        "reports/reapproval_review_batch_decisions_current.csv",
        "reports/reapproval_decision_validation_current.json",
        "reports/reapproval_decision_validation_current.md",
        "reports/reapproval_apply_plan_current.json",
        "reports/reapproval_apply_plan_current.md",
        "reports/reapproval_review_burden_current.json",
        "reports/reapproval_review_burden_current.md",
    }
)
HERMES_BUNDLE_JSON_KEYS = frozenset({"quickstart", "claude_desktop", "claude_code", "chatgpt", "claude_api"})
MCP_PRODUCT_READINESS_AUTHORITY_EXPECTED_ARTIFACTS = {
    "product_readiness": {
        "path": "reports/mcp_product_readiness_current.json",
        "report_type": "mcp_product_readiness",
    },
    "mcp_demo_answers": {
        "path": "reports/mcp_demo_answers_current.json",
        "report_type": "mcp_demo_answers",
    },
    "mcp_transport_smoke": {
        "path": "reports/mcp_transport_smoke_current.json",
        "report_type": "mcp_transport_smoke",
    },
    "mcp_index_visibility": {
        "path": "reports/mcp_index_visibility_current.json",
        "report_type": "mcp_index_visibility_audit",
    },
    "mcp_connection_readiness": {
        "path": "reports/mcp_connection_readiness_current.json",
        "report_type": "mcp_connection_readiness",
    },
}
MCP_PRODUCT_READINESS_AUTHORITY_REQUIRED_ROLES = frozenset(
    MCP_PRODUCT_READINESS_AUTHORITY_EXPECTED_ARTIFACTS
)
REQUIRED_APPROVAL_REVIEW_EVENT_TYPES = (
    "ai_review_confirmed",
    "approved",
    "human_review_confirmed",
)
HERMES_BUNDLE_QUICKSTART_KEYS = frozenset(
    {
        "validate_synthetic_chain",
        "run_local_stdio_server",
        "run_http_server",
        "run_chatgpt_data_server",
        "openai_secure_tunnel",
    }
)
HERMES_BUNDLE_ZIP_REQUIRED_ENTRIES = frozenset(
    {
        "README.md",
        "README.ko.md",
        "manifest.json",
        "mcp_config.bundle.json",
        "connect_mcp_client.ps1",
        "MCP 사용 시작하기.txt",
        "설치 후 MCP 사용 방법 보기.bat",
        "Codex 플러그인 MCP 입력값.txt",
        "ChatGPT Desktop에 연결하기.bat",
        "Codex에 연결하기.bat",
        "Claude Desktop에 연결하기.bat",
        "Claude Code에 연결하기.bat",
        "ChatGPT HTTPS에 연결하기.bat",
        "ChatGPT 보안 Tunnel에 연결하기.bat",
        "Claude HTTPS에 연결하기.bat",
        "install_local_package.ps1",
        "doctor_mcp_connection.ps1",
        "연결 상태 확인하기.bat",
        "validate_mcp_smoke.ps1",
        "run_local_stdio_server.ps1",
        "run_http_server.ps1",
        "run_chatgpt_data_server.ps1",
        "run_openai_secure_tunnel.ps1",
        "claude_desktop_config.json",
        "chatgpt_desktop_local_mcp.json",
        "chatgpt_connector.json",
        "claude_api_fragment.json",
    }
)
ALLOWLIST_ARTIFACT = ".release-hygiene-allowlist.json"
REQUIRED_JSON_MARKERS: dict[str, dict[str, Any]] = {
    "reports/private_release_gate_current.json": {"report_type": "private_release_gate"},
    "reports/private_release_readiness_current.json": {"report_type": "private_release_readiness"},
    "reports/private_release_manifest_current.json": {"manifest_type": "private_release_handoff"},
    "reports/github_private_visibility_current.json": {"report_type": "github_private_visibility"},
    "reports/release_hygiene_current.json": {"report_type": "release_hygiene"},
    "reports/private_release_smoke_current.json": {"report_type": "private_release_smoke"},
    "reports/hermes_mcp_check_current.json": {"report_type": "hermes_agent_run"},
    "reports/installed_console_scripts_hermes.json": {"report_type": "installed_console_scripts"},
    "reports/mcp_smoke_hermes.json": {"report_type": "local_mcp_smoke"},
    "reports/mcp_transport_smoke_hermes.json": {"report_type": "mcp_transport_smoke"},
    "reports/mcp_connection_readiness_bundle_hermes.json": {"report_type": "mcp_connection_readiness"},
    "reports/mcp_connection_readiness_chatgpt_https_hermes.json": {"report_type": "mcp_connection_readiness"},
    "reports/mcp_connection_readiness_chatgpt_tunnel_hermes.json": {"report_type": "mcp_connection_readiness"},
    "reports/mcp_readiness_authority_current.json": {"report_type": "mcp_readiness_authority", "authority_version": 1},
    "reports/mcp_product_readiness_current.json": {"report_type": "mcp_product_readiness"},
    "reports/table_review_source_traceability_current.json": {"report_type": "table_review_source_traceability"},
    "reports/parsing_goldset_table_drift_check_current.json": {"report_type": "parsing_goldset_table_drift_check"},
    "reports/table_preprocessing_claim_gate_current.json": {"report_type": "table_preprocessing_claim_gate"},
    "reports/mcp_demo_answers_current.json": {"report_type": "mcp_demo_answers"},
    "reports/simple_rag_vs_mcp_accuracy_current.json": {"report_type": "simple_rag_vs_mcp_accuracy"},
    "reports/mcp_transport_smoke_current.json": {"report_type": "mcp_transport_smoke"},
    "reports/mcp_index_visibility_current.json": {"report_type": "mcp_index_visibility_audit"},
    "reports/mcp_connection_readiness_current.json": {"report_type": "mcp_connection_readiness"},
    "reports/mcp_handoff_current.json": {"report_type": "mcp_handoff_report", "handoff_schema_version": 2},
    "reports/mcp_query_benchmark_current.json": {"report_type": "mcp_query_benchmark"},
    "reports/mcp_answer_evidence_bundle_current.json": {"report_type": "mcp_answer_evidence_bundle"},
    "reports/mcp_performance_load_evidence_current.json": {"report_type": "mcp_performance_load_evidence"},
    "reports/mcp_cold_start_benchmark_current.json": {"report_type": "mcp_cold_start_benchmark"},
    "reports/mcp_concurrent_benchmark_current.json": {"report_type": "mcp_concurrent_query_benchmark"},
    "data/aks_mcp_publish_runtime/reports/aks_mcp_publish_tenant-aks-publish_approval_worklist.json": {
        "report_type": "approval_worklist"
    },
    "data/aks_mcp_publish_runtime/reports/aks_mcp_publish_tenant-aks-publish_approval_review_batches.json": {
        "report_type": "approval_review_batch_manifest"
    },
    "reports/approval_worklist_current.json": {"report_type": "approval_worklist"},
    "reports/approval_review_batches_current.json": {"report_type": "approval_review_batch_manifest"},
    "reports/reapproval_worklist_current.json": {"report_type": "reapproval_worklist"},
    "reports/reapproval_worklist_current_chunks.json": {"report_type": "reapproval_worklist_chunk_candidates"},
    "reports/reapproval_review_batches_current.json": {"report_type": "reapproval_review_batch_manifest"},
    "reports/reapproval_decision_validation_current.json": {"report_type": "reapproval_decision_validation"},
    "reports/reapproval_apply_plan_current.json": {"report_type": "reapproval_apply_plan"},
    "reports/reapproval_review_burden_current.json": {"report_type": "reapproval_review_burden"},
}
MCP_PRODUCT_READINESS_DIAGNOSTIC_REPORT_TYPES = frozenset(
    {
        "strict_public_readiness_gap_summary",
        "temporal_ambiguity_review_scope",
    }
)


def verify_release_evidence_index(index: dict[str, Any], repo_root: Path | str | None = None) -> dict[str, Any]:
    root = Path(repo_root).resolve() if repo_root is not None else Path.cwd().resolve()
    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    observed_artifact_paths: set[str] = set()
    observed_repo_commits: dict[str, str] = {}
    evidence_context: dict[str, Any] = {"release_hygiene_suppressed_finding_count": 0}
    evidence_profile = str(index.get("evidence_profile") or "private-release")
    if index.get("index_type") != "release_evidence_index":
        failures.append({"check": "index_type", "reason": "expected release_evidence_index"})
    if index.get("index_version") != 1:
        failures.append(
            {
                "check": "index_version",
                "reason": "expected supported release evidence index_version 1",
                "observed": index.get("index_version"),
            }
        )
    if evidence_profile not in EVIDENCE_PROFILES:
        failures.append(
            {
                "check": "evidence_profile",
                "reason": "expected one of: " + ", ".join(sorted(EVIDENCE_PROFILES)),
                "observed": evidence_profile,
            }
        )

    artifacts = index.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        failures.append({"check": "artifacts_present", "reason": "artifact list is missing or empty"})
        artifacts = []

    for artifact in artifacts:
        if not isinstance(artifact, dict):
            failures.append({"check": "artifact_shape", "reason": "artifact entry is not an object"})
            continue
        artifact_path = str(artifact.get("artifact_path") or "")
        if not artifact_path:
            failures.append({"check": "artifact_path", "reason": "artifact path is missing"})
            continue

        normalized_artifact_path = _normalize_artifact_path(artifact_path)
        if _unsafe_artifact_path(normalized_artifact_path):
            failures.append({"check": "artifact_path_safe", "artifact_path": artifact_path})
            continue

        observed_artifact_paths.add(normalized_artifact_path)
        _verify_artifact_on_disk(
            root,
            artifact,
            artifact_path,
            normalized_artifact_path,
            evidence_profile,
            failures,
            warnings,
            observed_repo_commits,
            evidence_context,
        )

        summary = artifact.get("json_summary")
        if isinstance(summary, dict):
            if summary.get("passed") is False:
                if not _allowed_diagnostic_not_passed(evidence_profile, summary):
                    failures.append({"check": "json_summary_passed", "artifact_path": artifact_path})
            failed_check_count = summary.get("failed_check_count")
            if isinstance(failed_check_count, int) and failed_check_count:
                if not _allowed_diagnostic_not_passed(evidence_profile, summary):
                    failures.append(
                        {
                            "check": "json_summary_failed_check_count",
                            "artifact_path": artifact_path,
                            "failed_check_count": failed_check_count,
                        }
                    )

    _append_required_artifact_failures(index, observed_artifact_paths, failures, evidence_context, evidence_profile)
    _append_repo_worktree_warnings(index, warnings)
    repo_commit = _append_repo_commit_consistency_failures(
        index,
        observed_repo_commits,
        failures,
        require_repo_commit=True,
    )

    return {
        "report_type": "release_evidence_bundle_verification",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evidence_profile": evidence_profile,
        "repo_commit": repo_commit,
        "passed": not failures,
        "artifact_count": len(artifacts),
        "failure_count": len(failures),
        "warning_count": len(warnings),
        "failures": failures,
        "warnings": warnings,
    }


def _verify_artifact_on_disk(
    root: Path,
    artifact: dict[str, Any],
    artifact_path: str,
    normalized_artifact_path: str,
    evidence_profile: str,
    failures: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    observed_repo_commits: dict[str, str],
    evidence_context: dict[str, Any],
) -> None:
    candidate = (root / normalized_artifact_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        failures.append({"check": "artifact_path_safe", "artifact_path": artifact_path})
        return

    actual_exists = candidate.is_file()
    claimed_exists = artifact.get("exists") is True
    if claimed_exists != actual_exists:
        failures.append(
            {
                "check": "artifact_exists_mismatch",
                "artifact_path": artifact_path,
                "claimed_exists": claimed_exists,
                "actual_exists": actual_exists,
            }
        )

    if not actual_exists:
        failures.append({"check": "artifact_file_present", "artifact_path": artifact_path})
        return

    actual_size = candidate.stat().st_size
    claimed_size = artifact.get("size_bytes")
    if claimed_size != actual_size:
        failures.append(
            {
                "check": "artifact_size_mismatch",
                "artifact_path": artifact_path,
                "claimed_size_bytes": claimed_size,
                "actual_size_bytes": actual_size,
            }
        )

    claimed_sha = artifact.get("sha256")
    actual_sha = _sha256_file(candidate)
    if not _valid_sha256(claimed_sha):
        failures.append({"check": "artifact_sha256", "artifact_path": artifact_path})
    elif claimed_sha != actual_sha:
        failures.append(
            {
                "check": "artifact_sha256_mismatch",
                "artifact_path": artifact_path,
                "claimed_sha256": claimed_sha,
                "actual_sha256": actual_sha,
            }
        )

    _append_json_artifact_failures(
        root,
        candidate,
        artifact_path,
        normalized_artifact_path,
        evidence_profile,
        failures,
        warnings,
        observed_repo_commits,
        evidence_context,
    )
    _append_hermes_zip_artifact_failures(candidate, normalized_artifact_path, artifact_path, failures)


def _append_json_artifact_failures(
    root: Path,
    path: Path,
    artifact_path: str,
    normalized_artifact_path: str,
    evidence_profile: str,
    failures: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    observed_repo_commits: dict[str, str],
    evidence_context: dict[str, Any],
) -> None:
    if path.suffix.lower() != ".json":
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        failures.append({"check": "json_artifact_readable", "artifact_path": artifact_path, "reason": str(exc)})
        return
    if not isinstance(payload, dict):
        failures.append({"check": "json_artifact_object", "artifact_path": artifact_path})
        return
    if _profile_requires_repo_commit(evidence_profile) and normalized_artifact_path in REQUIRED_JSON_MARKERS:
        repo_commit = payload.get("repo_commit")
        if not isinstance(repo_commit, str) or not COMMIT_RE.fullmatch(repo_commit.strip()):
            failures.append({"check": "json_artifact_repo_commit", "artifact_path": artifact_path})
        else:
            observed_repo_commits[artifact_path] = repo_commit.strip()
    expected_markers = REQUIRED_JSON_MARKERS.get(normalized_artifact_path)
    if expected_markers:
        for key, expected_value in expected_markers.items():
            if payload.get(key) != expected_value:
                failures.append(
                    {
                        "check": "json_artifact_expected_marker",
                        "artifact_path": artifact_path,
                        "marker": key,
                        "expected": expected_value,
                        "actual": payload.get(key),
                    }
                )
    if payload.get("passed") is False:
        if _allowed_diagnostic_not_passed(evidence_profile, payload):
            warnings.append(
                {
                    "check": "json_artifact_diagnostic_not_passed",
                    "artifact_path": artifact_path,
                    "report_type": payload.get("report_type"),
                    "status": payload.get("status"),
                }
            )
        else:
            failures.append({"check": "json_artifact_passed", "artifact_path": artifact_path})
    if normalized_artifact_path == "reports/mcp_client_bundle_hermes.json":
        _append_hermes_bundle_json_failures(payload, artifact_path, failures)
    if normalized_artifact_path == "reports/hermes_mcp_check_current.json":
        status = payload.get("status")
        if status not in {"ready", "ready_with_advisories"}:
            failures.append({"check": "json_artifact_hermes_status", "artifact_path": artifact_path, "status": status})
    if evidence_profile == "mcp-product-readiness":
        _append_mcp_product_readiness_json_failures(
            root,
            payload,
            normalized_artifact_path,
            artifact_path,
            failures,
        )
    if normalized_artifact_path == "reports/private_release_smoke_current.json":
        if payload.get("data_dir_mode") != "explicit" or payload.get("handoff_evidence") is not True:
            failures.append(
                {
                    "check": "json_artifact_private_smoke_handoff_evidence",
                    "artifact_path": artifact_path,
                    "data_dir_mode": payload.get("data_dir_mode"),
                    "handoff_evidence": payload.get("handoff_evidence"),
                }
            )
    for key in ("failed_check_count", "failure_count", "finding_count"):
        value = payload.get(key)
        if isinstance(value, int) and value > 0:
            if not (key == "failed_check_count" and _allowed_diagnostic_not_passed(evidence_profile, payload)):
                failures.append({"check": f"json_artifact_{key}", "artifact_path": artifact_path, key: value})
    release_blocker_count = payload.get("release_blocker_count")
    if isinstance(release_blocker_count, int) and release_blocker_count > 0:
        warnings.append(
            {
                "check": "json_artifact_release_blocker_count",
                "artifact_path": artifact_path,
                "release_blocker_count": release_blocker_count,
                "release_gate_status": payload.get("release_gate_status"),
            }
        )
    suppressed_finding_count = payload.get("suppressed_finding_count")
    if isinstance(suppressed_finding_count, int) and suppressed_finding_count > 0:
        if normalized_artifact_path == "reports/release_hygiene_current.json":
            evidence_context["release_hygiene_suppressed_finding_count"] = suppressed_finding_count
        warnings.append(
            {
                "check": "json_artifact_suppressed_finding_count",
                "artifact_path": artifact_path,
                "suppressed_finding_count": suppressed_finding_count,
            }
        )
        if normalized_artifact_path == "reports/release_hygiene_current.json":
            _append_suppression_approval_failures(payload, artifact_path, failures)
    failed_check_names = payload.get("failed_check_names")
    if isinstance(failed_check_names, list) and failed_check_names:
        failures.append(
            {
                "check": "json_artifact_failed_check_names",
                "artifact_path": artifact_path,
                "failed_check_count": len(failed_check_names),
            }
        )


def _allowed_diagnostic_not_passed(evidence_profile: str, payload: dict[str, Any]) -> bool:
    return (
        evidence_profile == "mcp-product-readiness"
        and payload.get("report_type") in MCP_PRODUCT_READINESS_DIAGNOSTIC_REPORT_TYPES
    )


def _append_suppression_approval_failures(
    payload: dict[str, Any], artifact_path: str, failures: list[dict[str, Any]]
) -> None:
    allowlist = payload.get("allowlist")
    if not isinstance(allowlist, dict):
        failures.append(
            {
                "check": "json_artifact_suppression_approval_metadata",
                "artifact_path": artifact_path,
                "reason": "release hygiene suppressions require allowlist approval metadata",
            }
        )
        return
    missing_count = allowlist.get("missing_approval_metadata_count")
    if not isinstance(missing_count, int) or missing_count > 0:
        failures.append(
            {
                "check": "json_artifact_suppression_approval_metadata",
                "artifact_path": artifact_path,
                "missing_approval_metadata_count": missing_count,
            }
        )
    non_attributable_count = allowlist.get("non_attributable_approval_count")
    if not isinstance(non_attributable_count, int) or non_attributable_count > 0:
        failures.append(
            {
                "check": "json_artifact_suppression_approval_attribution",
                "artifact_path": artifact_path,
                "non_attributable_approval_count": non_attributable_count,
            }
        )


def _append_mcp_product_readiness_json_failures(
    root: Path,
    payload: dict[str, Any],
    normalized_artifact_path: str,
    artifact_path: str,
    failures: list[dict[str, Any]],
) -> None:
    if normalized_artifact_path == "reports/mcp_product_readiness_current.json":
        _append_product_readiness_source_artifact_failures(root, payload, artifact_path, failures)
    if normalized_artifact_path == "reports/aks_mcp_publish_runtime_report.json":
        _append_publish_runtime_approval_evidence_failures(root, payload, artifact_path, failures)
    if normalized_artifact_path == "reports/mcp_readiness_authority_current.json":
        authoritative_artifacts = payload.get("authoritative_artifacts")
        if not isinstance(authoritative_artifacts, list) or not authoritative_artifacts:
            failures.append(
                {
                    "check": "json_artifact_mcp_authority_artifacts",
                    "artifact_path": artifact_path,
                    "reason": "authority manifest must list authoritative_artifacts",
                }
            )
            return
        product_authority = [
            item
            for item in authoritative_artifacts
            if isinstance(item, dict) and item.get("role") == "product_readiness"
        ]
        observed_roles = {
            str(item.get("role") or "")
            for item in authoritative_artifacts
            if isinstance(item, dict)
        }
        authority_by_role = {
            str(item.get("role") or ""): item
            for item in authoritative_artifacts
            if isinstance(item, dict)
        }
        missing_roles = sorted(MCP_PRODUCT_READINESS_AUTHORITY_REQUIRED_ROLES - observed_roles)
        if missing_roles:
            failures.append(
                {
                    "check": "json_artifact_mcp_authority_required_roles",
                    "artifact_path": artifact_path,
                    "missing_roles": missing_roles,
                }
            )
        if not product_authority:
            failures.append(
                {
                    "check": "json_artifact_mcp_authority_product_readiness",
                    "artifact_path": artifact_path,
                    "reason": "authority manifest must identify the authoritative product_readiness artifact",
                }
            )
        for role, expected in MCP_PRODUCT_READINESS_AUTHORITY_EXPECTED_ARTIFACTS.items():
            if role == "product_readiness":
                continue
            item = authority_by_role.get(role)
            if not isinstance(item, dict):
                continue
            _append_authority_role_artifact_file_failures(
                root,
                item,
                artifact_path,
                failures,
                expected_path=expected["path"],
                expected_report_type=expected["report_type"],
            )
        for item in product_authority:
            _append_authority_product_artifact_file_failures(root, item, artifact_path, failures)
            contract = item.get("product_readiness_contract") if isinstance(item.get("product_readiness_contract"), dict) else {}
            if int(contract.get("source_report_artifact_count") or 0) <= 0:
                failures.append(
                    {
                        "check": "json_artifact_mcp_authority_product_fingerprints",
                        "artifact_path": artifact_path,
                        "reason": "authoritative product_readiness must include upstream source report fingerprints",
                    }
                )
            if int(contract.get("missing_source_report_artifact_count") or 0) > 0:
                failures.append(
                    {
                        "check": "json_artifact_mcp_authority_product_fingerprints_incomplete",
                        "artifact_path": artifact_path,
                        "missing_source_report_artifact_count": contract.get("missing_source_report_artifact_count"),
                    }
                )
        supersedes = payload.get("supersedes")
        if isinstance(supersedes, list):
            missing_reason_count = sum(
                1
                for item in supersedes
                if isinstance(item, dict) and not str(item.get("reason") or "").strip()
            )
            if missing_reason_count:
                failures.append(
                    {
                        "check": "json_artifact_mcp_authority_supersedes_reason",
                        "artifact_path": artifact_path,
                        "missing_reason_count": missing_reason_count,
                    }
                )
    if normalized_artifact_path == "reports/mcp_handoff_current.json":
        _append_mcp_handoff_authority_failures(root, payload, artifact_path, failures)


def _append_publish_runtime_approval_evidence_failures(
    root: Path,
    payload: dict[str, Any],
    artifact_path: str,
    failures: list[dict[str, Any]],
) -> None:
    if payload.get("report_type") not in {"mcp_publish_runtime", "aks_mcp_publish_runtime"}:
        failures.append(
            {
                "check": "json_artifact_publish_runtime_report_type",
                "artifact_path": artifact_path,
                "report_type": payload.get("report_type"),
            }
        )
        return
    approval_evidence = payload.get("approval_evidence")
    if not isinstance(approval_evidence, dict):
        failures.append(
            {
                "check": "json_artifact_publish_runtime_approval_evidence",
                "artifact_path": artifact_path,
                "reason": "publish runtime report must include approval_evidence",
            }
        )
        return
    artifacts = approval_evidence.get("artifacts")
    if not isinstance(artifacts, dict):
        failures.append(
            {
                "check": "json_artifact_publish_runtime_approval_artifacts",
                "artifact_path": artifact_path,
                "reason": "approval_evidence must include concrete artifact paths",
            }
        )
        return
    expected = {
        "worklist_json": "worklist_report_sha256",
        "review_batch_manifest_json": "review_batch_manifest_sha256",
    }
    for artifact_role, sha_key in expected.items():
        path_value = str(artifacts.get(artifact_role) or "")
        expected_sha = approval_evidence.get(sha_key)
        if not path_value or not _valid_sha256(expected_sha):
            failures.append(
                {
                    "check": "json_artifact_publish_runtime_approval_fingerprint_complete",
                    "artifact_path": artifact_path,
                    "artifact_role": artifact_role,
                    "source_path": path_value,
                }
            )
            continue
        resolved = _resolve_repo_artifact(root, path_value)
        if resolved is None:
            failures.append(
                {
                    "check": "json_artifact_publish_runtime_approval_path",
                    "artifact_path": artifact_path,
                    "artifact_role": artifact_role,
                    "source_path": path_value,
                }
            )
            continue
        normalized_path, candidate = resolved
        if not candidate.is_file():
            failures.append(
                {
                    "check": "json_artifact_publish_runtime_approval_file",
                    "artifact_path": artifact_path,
                    "artifact_role": artifact_role,
                    "source_path": normalized_path,
                }
            )
            continue
        actual_sha = _sha256_file(candidate)
        if actual_sha != expected_sha:
            failures.append(
                {
                    "check": "json_artifact_publish_runtime_approval_sha256",
                    "artifact_path": artifact_path,
                    "artifact_role": artifact_role,
                    "source_path": normalized_path,
                    "claimed_sha256": expected_sha,
                    "actual_sha256": actual_sha,
                }
            )
    _append_publish_runtime_vector_approval_evidence_failures(root, payload, artifact_path, approval_evidence, failures)
    _append_publish_runtime_journal_approval_evidence_failures(root, payload, artifact_path, approval_evidence, failures)


def _append_publish_runtime_vector_approval_evidence_failures(
    root: Path,
    payload: dict[str, Any],
    artifact_path: str,
    approval_evidence: dict[str, Any],
    failures: list[dict[str, Any]],
) -> None:
    vector_path = _publish_runtime_vector_path(root, payload)
    if vector_path is None:
        return
    expected_fields = {
        "approval_worklist_report_sha256": str(approval_evidence.get("worklist_report_sha256") or ""),
        "approval_review_batch_manifest_sha256": str(
            approval_evidence.get("review_batch_manifest_sha256") or ""
        ),
    }
    observed: dict[str, dict[str, int]] = {field: {} for field in expected_fields}
    record_count = 0
    try:
        with vector_path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record_count += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                metadata = row.get("metadata") if isinstance(row, dict) else None
                if not isinstance(metadata, dict):
                    continue
                for field_name in expected_fields:
                    value = str(metadata.get(field_name) or "")
                    observed[field_name][value] = observed[field_name].get(value, 0) + 1
    except OSError:
        return

    for field_name, expected_sha in expected_fields.items():
        if not _valid_sha256(expected_sha):
            continue
        mismatched = {
            value: count
            for value, count in observed.get(field_name, {}).items()
            if value != expected_sha
        }
        missing_count = record_count - sum(observed.get(field_name, {}).values())
        if mismatched or missing_count:
            failures.append(
                {
                    "check": "json_artifact_publish_runtime_vector_approval_sha256",
                    "artifact_path": artifact_path,
                    "vector_path": str(vector_path.relative_to(root)) if _is_relative_to(vector_path, root) else str(vector_path),
                    "field": field_name,
                    "expected_sha256": expected_sha,
                    "record_count": record_count,
                    "mismatch_count": sum(mismatched.values()) + missing_count,
                    "missing_count": missing_count,
                    "observed_sha256_counts": dict(sorted(mismatched.items())),
                }
            )


def _append_publish_runtime_journal_approval_evidence_failures(
    root: Path,
    payload: dict[str, Any],
    artifact_path: str,
    approval_evidence: dict[str, Any],
    failures: list[dict[str, Any]],
) -> None:
    journal_path = _publish_runtime_approval_journal_path(root, payload)
    if journal_path is None or not journal_path.is_file():
        return
    expected_fields = {
        "worklist_report_sha256": str(approval_evidence.get("worklist_report_sha256") or ""),
        "review_batch_manifest_sha256": str(approval_evidence.get("review_batch_manifest_sha256") or ""),
    }
    observed: dict[str, dict[str, int]] = {field: {} for field in expected_fields}
    record_count = 0
    try:
        with journal_path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record_count += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                evidence = row.get("worklist_evidence") if isinstance(row, dict) else None
                if not isinstance(evidence, dict):
                    continue
                for field_name in expected_fields:
                    value = str(evidence.get(field_name) or "")
                    observed[field_name][value] = observed[field_name].get(value, 0) + 1
    except OSError:
        return

    for field_name, expected_sha in expected_fields.items():
        if not _valid_sha256(expected_sha):
            continue
        mismatched = {
            value: count
            for value, count in observed.get(field_name, {}).items()
            if value != expected_sha
        }
        missing_count = record_count - sum(observed.get(field_name, {}).values())
        if mismatched or missing_count:
            failures.append(
                {
                    "check": "json_artifact_publish_runtime_journal_approval_sha256",
                    "artifact_path": artifact_path,
                    "journal_path": str(journal_path.relative_to(root)) if _is_relative_to(journal_path, root) else str(journal_path),
                    "field": field_name,
                    "expected_sha256": expected_sha,
                    "record_count": record_count,
                    "mismatch_count": sum(mismatched.values()) + missing_count,
                    "missing_count": missing_count,
                    "observed_sha256_counts": dict(sorted(mismatched.items())),
                }
            )


def _publish_runtime_vector_path(root: Path, payload: dict[str, Any]) -> Path | None:
    tenant_id = str(payload.get("tenant_id") or "").strip()
    if not tenant_id:
        return None
    candidate_roots = _publish_runtime_candidate_roots(root, payload)
    candidates: list[Path] = []
    for base in candidate_roots:
        candidates.append(base / "vector_db" / tenant_id / "approved_vectors.jsonl")
    target_data_dir = str(payload.get("target_data_dir") or "").strip()
    if target_data_dir:
        resolved = _resolve_repo_artifact(root, target_data_dir)
        if resolved is not None:
            _, target_root = resolved
            candidates.append(target_root / "tenants" / tenant_id / "vector_db" / tenant_id / "approved_vectors.jsonl")
            candidates.append(target_root / "vector_db" / tenant_id / "approved_vectors.jsonl")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _publish_runtime_approval_journal_path(root: Path, payload: dict[str, Any]) -> Path | None:
    for base in _publish_runtime_candidate_roots(root, payload):
        candidate = base / "repository" / "journals" / "approvals.jsonl"
        if candidate.is_file():
            return candidate
    return None


def _publish_runtime_candidate_roots(root: Path, payload: dict[str, Any]) -> list[Path]:
    candidates: list[Path] = []
    for key in ("tenant_data_dir",):
        value = str(payload.get(key) or "").strip()
        if not value:
            continue
        resolved = _resolve_repo_artifact(root, value)
        if resolved is not None:
            _, candidate = resolved
            candidates.append(candidate)
    return candidates


def _append_mcp_handoff_authority_failures(
    root: Path,
    payload: dict[str, Any],
    artifact_path: str,
    failures: list[dict[str, Any]],
) -> None:
    source_artifacts = payload.get("source_report_artifacts")
    if not isinstance(source_artifacts, list) or not source_artifacts:
        failures.append(
            {
                "check": "json_artifact_mcp_handoff_source_fingerprints",
                "artifact_path": artifact_path,
                "reason": "mcp_handoff must include source_report_artifacts",
            }
        )
        return

    by_role = {str(item.get("role") or ""): item for item in source_artifacts if isinstance(item, dict)}
    required_roles = {
        "product_readiness_report": "mcp_product_readiness",
        "authority_manifest": "mcp_readiness_authority",
    }
    missing_roles = sorted(role for role in required_roles if role not in by_role)
    if missing_roles:
        failures.append(
            {
                "check": "json_artifact_mcp_handoff_source_fingerprints",
                "artifact_path": artifact_path,
                "missing_roles": missing_roles,
            }
        )

    for role, expected_report_type in required_roles.items():
        item = by_role.get(role)
        if not isinstance(item, dict):
            continue
        _append_mcp_handoff_source_artifact_failures(
            root,
            item,
            role,
            expected_report_type,
            artifact_path,
            failures,
        )

    authority_summary = payload.get("authority_summary")
    if not isinstance(authority_summary, dict):
        failures.append(
            {
                "check": "json_artifact_mcp_handoff_authority_summary",
                "artifact_path": artifact_path,
                "reason": "mcp_handoff must include authority_summary",
            }
        )
        return
    if (
        authority_summary.get("report_type") != "mcp_readiness_authority"
        or authority_summary.get("passed") is not True
        or int(authority_summary.get("blocking_count") or 0) > 0
        or int(authority_summary.get("authoritative_artifact_count") or 0) <= 0
    ):
        failures.append(
            {
                "check": "json_artifact_mcp_handoff_authority_summary",
                "artifact_path": artifact_path,
                "authority_summary": authority_summary,
            }
        )
    _append_mcp_handoff_approval_journal_failures(payload, artifact_path, failures)


def _append_mcp_handoff_approval_journal_failures(
    payload: dict[str, Any],
    artifact_path: str,
    failures: list[dict[str, Any]],
) -> None:
    visibility = payload.get("mcp_index_visibility_summary")
    if not isinstance(visibility, dict) or not visibility:
        failures.append(
            {
                "check": "json_artifact_mcp_handoff_index_visibility_summary",
                "artifact_path": artifact_path,
                "reason": "mcp_handoff must include mcp_index_visibility_summary",
            }
        )
        return
    journal = visibility.get("approval_journal_coverage")
    if not isinstance(journal, dict) or not journal:
        failures.append(
            {
                "check": "json_artifact_mcp_handoff_approval_journal_coverage",
                "artifact_path": artifact_path,
                "reason": "mcp_handoff must include approval_journal_coverage",
            }
        )
        return
    eligible = _nonnegative_int_or_none(
        journal["eligible_record_count"] if "eligible_record_count" in journal else journal.get("record_count"),
        default=0,
    )
    matched = _nonnegative_int_or_none(journal.get("matched_record_count"), default=0)
    missing = _nonnegative_int_or_none(journal.get("missing_record_count"), default=0)
    if eligible is None or matched is None or missing is None:
        failures.append(
            {
                "check": "json_artifact_mcp_handoff_approval_journal_coverage",
                "artifact_path": artifact_path,
                "reason": "approval_journal_coverage counts must be non-negative integers",
                "approval_journal_coverage": journal,
            }
        )
        return
    if eligible <= 0 or missing > 0 or matched < eligible:
        failures.append(
            {
                "check": "json_artifact_mcp_handoff_approval_journal_coverage",
                "artifact_path": artifact_path,
                "approval_journal_coverage": journal,
            }
        )
    product = payload.get("product_summary")
    review_events = (
        product.get("approval_journal_review_event_coverage")
        if isinstance(product, dict)
        and isinstance(product.get("approval_journal_review_event_coverage"), dict)
        else None
    )
    _append_mcp_handoff_approval_review_event_failures(
        review_events,
        artifact_path,
        failures,
    )


def _append_mcp_handoff_approval_review_event_failures(
    coverage: dict[str, Any] | None,
    artifact_path: str,
    failures: list[dict[str, Any]],
) -> None:
    if not isinstance(coverage, dict) or not coverage:
        failures.append(
            {
                "check": "json_artifact_mcp_handoff_approval_journal_review_event_coverage",
                "artifact_path": artifact_path,
                "reason": "mcp_handoff must include approval_journal_review_event_coverage",
            }
        )
        return
    status = _review_event_coverage_status(coverage)
    if status["malformed_count_fields"]:
        failures.append(
            {
                "check": "json_artifact_mcp_handoff_approval_journal_review_event_coverage",
                "artifact_path": artifact_path,
                "reason": "approval_journal_review_event_coverage counts must be non-negative integers",
                "approval_journal_review_event_coverage": coverage,
                "malformed_count_fields": status["malformed_count_fields"],
            }
        )
        return
    if status["missing_required_event_types"]:
        failures.append(
            {
                "check": "json_artifact_mcp_handoff_approval_journal_review_event_coverage",
                "artifact_path": artifact_path,
                "reason": "approval_journal_review_event_coverage is missing required event types",
                "approval_journal_review_event_coverage": coverage,
                "missing_required_event_types": status["missing_required_event_types"],
            }
        )
        return
    if status["incomplete_record_count"] > 0 or any(
        count > 0 for count in status["computed_missing_event_chunk_counts"].values()
    ):
        failures.append(
            {
                "check": "json_artifact_mcp_handoff_approval_journal_review_event_coverage",
                "artifact_path": artifact_path,
                "approval_journal_review_event_coverage": coverage,
                "computed_missing_event_chunk_counts": status["computed_missing_event_chunk_counts"],
            }
        )


def _review_event_coverage_status(coverage: dict[str, Any]) -> dict[str, Any]:
    expected_raw = coverage.get("expected_event_chunk_counts")
    observed_raw = coverage.get("event_chunk_counts")
    missing_raw = coverage.get("missing_event_chunk_counts")
    expected, expected_bad = _strict_nonnegative_int_dict(expected_raw)
    observed, observed_bad = _strict_nonnegative_int_dict(observed_raw)
    missing, missing_bad = _strict_nonnegative_int_dict(missing_raw)
    incomplete = _strict_nonnegative_int(coverage.get("incomplete_record_count"))
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


def _strict_nonnegative_int_dict(value: Any) -> tuple[dict[str, int], list[str]]:
    if not isinstance(value, dict):
        return {}, []
    parsed: dict[str, int] = {}
    malformed: list[str] = []
    for key, raw_count in value.items():
        count = _strict_nonnegative_int(raw_count)
        if count is None:
            malformed.append(str(key))
        else:
            parsed[str(key)] = count
    return parsed, malformed


def _strict_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        count = int(str(value))
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def _append_mcp_handoff_source_artifact_failures(
    root: Path,
    item: dict[str, Any],
    role: str,
    expected_report_type: str,
    artifact_path: str,
    failures: list[dict[str, Any]],
) -> None:
    path_value = str(item.get("path") or "")
    sha256 = item.get("sha256")
    byte_count = item.get("byte_count")
    if (
        not path_value
        or item.get("exists") is not True
        or not isinstance(byte_count, int)
        or byte_count <= 0
        or not _valid_sha256(sha256)
        or item.get("report_type") != expected_report_type
    ):
        failures.append(
            {
                "check": "json_artifact_mcp_handoff_source_fingerprint_complete",
                "artifact_path": artifact_path,
                "role": role,
            }
        )
        return
    if item.get("passed") is not True:
        failures.append(
            {
                "check": "json_artifact_mcp_handoff_source_report_passed",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
                "passed": item.get("passed"),
            }
        )

    resolved = _resolve_repo_artifact(root, path_value)
    if resolved is None:
        failures.append(
            {
                "check": "json_artifact_mcp_handoff_source_path",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
            }
        )
        return
    _normalized_path, candidate = resolved
    if not candidate.is_file():
        failures.append(
            {
                "check": "json_artifact_mcp_handoff_source_file",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
            }
        )
        return
    actual_sha = _sha256_file(candidate)
    if actual_sha != sha256:
        failures.append(
            {
                "check": "json_artifact_mcp_handoff_source_sha256",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
                "claimed_sha256": sha256,
                "actual_sha256": actual_sha,
            }
        )


def _append_product_readiness_source_artifact_failures(
    root: Path,
    payload: dict[str, Any],
    artifact_path: str,
    failures: list[dict[str, Any]],
) -> None:
    source_artifacts = payload.get("source_report_artifacts")
    if not isinstance(source_artifacts, list) or not source_artifacts:
        failures.append(
            {
                "check": "json_artifact_mcp_product_source_fingerprints",
                "artifact_path": artifact_path,
                "reason": "mcp_product_readiness must include source_report_artifacts",
            }
        )
        return
    incomplete_count = sum(
        1
        for item in source_artifacts
        if not isinstance(item, dict) or not _source_artifact_fingerprint_complete(item)
    )
    if incomplete_count:
        failures.append(
            {
                "check": "json_artifact_mcp_product_source_fingerprints_complete",
                "artifact_path": artifact_path,
                "incomplete_count": incomplete_count,
            }
        )
    for item in source_artifacts:
        if not isinstance(item, dict):
            continue
        _append_product_readiness_source_file_failures(root, item, artifact_path, failures)
    summary = payload.get("source_report_artifact_summary")
    if not isinstance(summary, dict):
        failures.append(
            {
                "check": "json_artifact_mcp_product_source_fingerprint_summary",
                "artifact_path": artifact_path,
                "reason": "mcp_product_readiness must include source_report_artifact_summary",
            }
        )
    elif int(summary.get("sha256_count") or 0) != len(source_artifacts):
        failures.append(
            {
                "check": "json_artifact_mcp_product_source_fingerprint_summary",
                "artifact_path": artifact_path,
                "sha256_count": summary.get("sha256_count"),
                "source_report_artifact_count": len(source_artifacts),
            }
        )
    _append_product_readiness_worklist_failures(payload, source_artifacts, artifact_path, failures)
    _append_product_readiness_table_claim_failures(payload, source_artifacts, artifact_path, failures)


def _append_product_readiness_table_claim_failures(
    payload: dict[str, Any],
    source_artifacts: list[Any],
    artifact_path: str,
    failures: list[dict[str, Any]],
) -> None:
    roles = {
        str(item.get("role") or "")
        for item in source_artifacts
        if isinstance(item, dict)
    }
    if "table_preprocessing_claim_gate_report" not in roles:
        failures.append(
            {
                "check": "json_artifact_mcp_product_table_claim_source",
                "artifact_path": artifact_path,
                "reason": "mcp_product_readiness must include a table_preprocessing_claim_gate_report source artifact",
            }
        )
    summary = payload.get("table_preprocessing_claim_gate_summary")
    if not isinstance(summary, dict):
        failures.append(
            {
                "check": "json_artifact_mcp_product_table_claim_summary",
                "artifact_path": artifact_path,
                "reason": "mcp_product_readiness must include table_preprocessing_claim_gate_summary",
            }
        )
        return
    required_fields = (
        "passed",
        "status",
        "pending_unit_count",
        "invalid_unit_count",
        "transfer_blocker_count",
        "source_traceability_issue_count",
        "source_traceability_require_page_count_verification",
        "drift_check_present",
        "drift_check_passed",
        "drift_check_blocker_count",
        "table_answer_blocker_count",
    )
    missing_fields = [field for field in required_fields if field not in summary]
    if missing_fields:
        failures.append(
            {
                "check": "json_artifact_mcp_product_table_claim_summary_fields",
                "artifact_path": artifact_path,
                "missing_fields": missing_fields,
            }
        )
        return
    if summary.get("passed") is not True or summary.get("status") not in {"ready", "ready_for_table_quality_claim"}:
        failures.append(
            {
                "check": "json_artifact_mcp_product_table_claim_ready",
                "artifact_path": artifact_path,
                "passed": summary.get("passed"),
                "status": summary.get("status"),
            }
        )
    numeric_zero_fields = (
        "pending_unit_count",
        "invalid_unit_count",
        "transfer_blocker_count",
        "source_traceability_issue_count",
        "drift_check_blocker_count",
        "table_answer_blocker_count",
    )
    nonzero_fields = {
        field: summary.get(field)
        for field in numeric_zero_fields
        if _int_value(summary.get(field)) != 0
    }
    if nonzero_fields:
        failures.append(
            {
                "check": "json_artifact_mcp_product_table_claim_blockers",
                "artifact_path": artifact_path,
                "nonzero_fields": nonzero_fields,
            }
        )
    if summary.get("source_traceability_require_page_count_verification") is not True:
        failures.append(
            {
                "check": "json_artifact_mcp_product_table_claim_source_page_count_verification",
                "artifact_path": artifact_path,
                "source_traceability_require_page_count_verification": summary.get(
                    "source_traceability_require_page_count_verification"
                ),
            }
        )
    if summary.get("drift_check_present") is not True or summary.get("drift_check_passed") is not True:
        failures.append(
            {
                "check": "json_artifact_mcp_product_table_claim_drift_check",
                "artifact_path": artifact_path,
                "drift_check_present": summary.get("drift_check_present"),
                "drift_check_passed": summary.get("drift_check_passed"),
            }
        )


def _append_product_readiness_worklist_failures(
    payload: dict[str, Any],
    source_artifacts: list[Any],
    artifact_path: str,
    failures: list[dict[str, Any]],
) -> None:
    roles = {
        str(item.get("role") or "")
        for item in source_artifacts
        if isinstance(item, dict)
    }
    approval_summary = payload.get("approval_workload_summary")
    approval_batch_summary = payload.get("approval_review_batch_summary")
    reapproval_summary = payload.get("reapproval_workload_summary")
    reapproval_batch_summary = payload.get("reapproval_review_batch_summary")
    reapproval_decision_validation_summary = payload.get("reapproval_decision_validation_summary")
    reapproval_apply_plan_summary = payload.get("reapproval_apply_plan_summary")
    runtime_drift = payload.get("runtime_version_drift_summary")
    revision_impact = payload.get("revision_impact_summary")
    if isinstance(approval_summary, dict):
        missing = [
            key
            for key in (
                "manual_attention_chunks",
                "low_risk_batch_review_candidate_chunks",
                "blocking_review_chunks",
                "domain_attention_chunks",
            )
            if key not in approval_summary
        ]
        if missing:
            failures.append(
                {
                    "check": "json_artifact_mcp_product_approval_workload_summary",
                    "artifact_path": artifact_path,
                    "missing_fields": missing,
                }
            )
        if "approval_worklist_report" not in roles:
            failures.append(
                {
                    "check": "json_artifact_mcp_product_approval_worklist_source",
                    "artifact_path": artifact_path,
                    "reason": "approval_workload_summary requires an approval_worklist_report source artifact",
                }
            )
    if isinstance(approval_batch_summary, dict):
        missing = [
            key
            for key in (
                "batch_count",
                "approval_chunk_count",
                "blocker_count",
                "warning_count",
            )
            if key not in approval_batch_summary
        ]
        if missing:
            failures.append(
                {
                    "check": "json_artifact_mcp_product_approval_review_batch_summary",
                    "artifact_path": artifact_path,
                    "missing_fields": missing,
                }
            )
        if "approval_review_batch_manifest_report" not in roles:
            failures.append(
                {
                    "check": "json_artifact_mcp_product_approval_review_batch_source",
                    "artifact_path": artifact_path,
                    "reason": "approval_review_batch_summary requires an approval_review_batch_manifest_report source artifact",
                }
            )
    if isinstance(reapproval_summary, dict):
        missing = [
            key
            for key in (
                "reapproval_candidate_chunks",
                "recommended_initial_review_chunks",
                "approval_provenance_missing_chunks",
                "approval_provenance_only_chunks",
                "approval_provenance_missing_field_counts",
                "pre_reapproval_blocker_count",
                "initial_review_reduction_ratio",
                "source_vector_integrity_failure_count",
            )
            if key not in reapproval_summary
        ]
        if missing:
            failures.append(
                {
                    "check": "json_artifact_mcp_product_reapproval_workload_summary",
                    "artifact_path": artifact_path,
                    "missing_fields": missing,
                }
            )
        if "reapproval_worklist_report" not in roles:
            failures.append(
                {
                    "check": "json_artifact_mcp_product_reapproval_worklist_source",
                    "artifact_path": artifact_path,
                    "reason": "reapproval_workload_summary requires a reapproval_worklist_report source artifact",
                }
            )
        reapproval_candidates = int(reapproval_summary.get("reapproval_candidate_chunks") or 0)
        if reapproval_candidates > 0:
            if not isinstance(reapproval_batch_summary, dict):
                failures.append(
                    {
                        "check": "json_artifact_mcp_product_reapproval_review_batch_summary",
                        "artifact_path": artifact_path,
                        "reason": "reapproval candidates require reapproval_review_batch_summary",
                    }
                )
            if "reapproval_review_batch_manifest_report" not in roles:
                failures.append(
                    {
                        "check": "json_artifact_mcp_product_reapproval_review_batch_source",
                        "artifact_path": artifact_path,
                        "reason": "reapproval candidates require a reapproval_review_batch_manifest_report source artifact",
                    }
                )
    if isinstance(reapproval_batch_summary, dict):
        missing = [
            key
            for key in (
                "candidate_count",
                "selected_candidate_count",
                "batch_count",
                "reapproval_chunk_count",
                "blocker_count",
                "warning_count",
            )
            if key not in reapproval_batch_summary
        ]
        if missing:
            failures.append(
                {
                    "check": "json_artifact_mcp_product_reapproval_review_batch_summary",
                    "artifact_path": artifact_path,
                    "missing_fields": missing,
                }
            )
        if "reapproval_review_batch_manifest_report" not in roles:
            failures.append(
                {
                    "check": "json_artifact_mcp_product_reapproval_review_batch_source",
                    "artifact_path": artifact_path,
                    "reason": "reapproval_review_batch_summary requires a reapproval_review_batch_manifest_report source artifact",
                }
            )
        if isinstance(reapproval_summary, dict):
            reapproval_candidates = int(reapproval_summary.get("reapproval_candidate_chunks") or 0)
            if reapproval_candidates > 0:
                mismatched = {
                    key: reapproval_batch_summary.get(key)
                    for key in ("candidate_count", "selected_candidate_count", "reapproval_chunk_count")
                    if int(reapproval_batch_summary.get(key) or 0) != reapproval_candidates
                }
                if mismatched:
                    failures.append(
                        {
                            "check": "json_artifact_mcp_product_reapproval_review_batch_coverage",
                            "artifact_path": artifact_path,
                            "reapproval_candidate_chunks": reapproval_candidates,
                            "mismatched_fields": mismatched,
                        }
                    )
        reapproval_batch_count = int(reapproval_batch_summary.get("batch_count") or 0)
        if reapproval_batch_count > 0:
            if not isinstance(reapproval_decision_validation_summary, dict):
                failures.append(
                    {
                        "check": "json_artifact_mcp_product_reapproval_decision_validation_summary",
                        "artifact_path": artifact_path,
                        "reason": "reapproval review batches require reapproval_decision_validation_summary",
                    }
                )
            if "reapproval_decision_validation_report" not in roles:
                failures.append(
                    {
                        "check": "json_artifact_mcp_product_reapproval_decision_validation_source",
                        "artifact_path": artifact_path,
                        "reason": "reapproval review batches require a reapproval_decision_validation_report source artifact",
                    }
                )
            if not isinstance(reapproval_apply_plan_summary, dict):
                failures.append(
                    {
                        "check": "json_artifact_mcp_product_reapproval_apply_plan_summary",
                        "artifact_path": artifact_path,
                        "reason": "reapproval review batches require reapproval_apply_plan_summary",
                    }
                )
            if "reapproval_apply_plan_report" not in roles:
                failures.append(
                    {
                        "check": "json_artifact_mcp_product_reapproval_apply_plan_source",
                        "artifact_path": artifact_path,
                        "reason": "reapproval review batches require a reapproval_apply_plan_report source artifact",
                    }
                )
    if isinstance(reapproval_decision_validation_summary, dict):
        missing = [
            key
            for key in (
                "expected_batch_count",
                "decision_row_count",
                "complete_row_count",
                "blank_or_incomplete_row_count",
                "blocking_count",
                "warning_count",
                "release_gate_status_counts",
            )
            if key not in reapproval_decision_validation_summary
        ]
        if missing:
            failures.append(
                {
                    "check": "json_artifact_mcp_product_reapproval_decision_validation_summary",
                    "artifact_path": artifact_path,
                    "missing_fields": missing,
                }
            )
        if "reapproval_decision_validation_report" not in roles:
            failures.append(
                {
                    "check": "json_artifact_mcp_product_reapproval_decision_validation_source",
                    "artifact_path": artifact_path,
                    "reason": "reapproval_decision_validation_summary requires a reapproval_decision_validation_report source artifact",
                }
            )
        reapproval_batch_count = (
            int(reapproval_batch_summary.get("batch_count") or 0)
            if isinstance(reapproval_batch_summary, dict)
            else 0
        )
        decision_issues = {
            "blocking_count": reapproval_decision_validation_summary.get("blocking_count"),
            "blank_or_incomplete_row_count": reapproval_decision_validation_summary.get(
                "blank_or_incomplete_row_count"
            ),
            "expected_batch_count": reapproval_decision_validation_summary.get("expected_batch_count"),
            "complete_row_count": reapproval_decision_validation_summary.get("complete_row_count"),
            "reapproval_batch_count": reapproval_batch_count,
        }
        if (
            int(reapproval_decision_validation_summary.get("blocking_count") or 0) > 0
            or int(reapproval_decision_validation_summary.get("blank_or_incomplete_row_count") or 0) > 0
            or (
                reapproval_batch_count > 0
                and (
                    int(reapproval_decision_validation_summary.get("expected_batch_count") or 0)
                    < reapproval_batch_count
                    or int(reapproval_decision_validation_summary.get("complete_row_count") or 0)
                    < reapproval_batch_count
                )
            )
        ):
            failures.append(
                {
                    "check": "json_artifact_mcp_product_reapproval_decision_validation_complete",
                    "artifact_path": artifact_path,
                    **decision_issues,
                }
            )
    if isinstance(reapproval_apply_plan_summary, dict):
        missing = [
            key
            for key in (
                "report_count",
                "passed",
                "blocker_count",
                "unsafe_contract_violation_count",
                "batch_apply_control_count",
                "required_execution_steps",
                "observed_execution_step_counts",
            )
            if key not in reapproval_apply_plan_summary
        ]
        if missing:
            failures.append(
                {
                    "check": "json_artifact_mcp_product_reapproval_apply_plan_summary",
                    "artifact_path": artifact_path,
                    "missing_fields": missing,
                }
            )
        if "reapproval_apply_plan_report" not in roles:
            failures.append(
                {
                    "check": "json_artifact_mcp_product_reapproval_apply_plan_source",
                    "artifact_path": artifact_path,
                    "reason": "reapproval_apply_plan_summary requires a reapproval_apply_plan_report source artifact",
                }
            )
        required_steps = set(reapproval_apply_plan_summary.get("required_execution_steps") or [])
        expected_steps = {
            "enforce_tenant_and_operator_access",
            "use_shared_review_workflow_contract",
            "validate_approval_preconditions",
            "validate_rejection_decision_contract",
            "run_preapproval_security_scan",
            "acknowledge_review_attention_flags",
            "recalculate_approval_hashes",
            "append_review_journals_and_snapshots",
            "record_apply_audit_event",
            "refresh_exports_and_vector_state",
            "keep_reindex_as_explicit_phase",
            "rerun_mcp_visibility_gate",
        }
        missing_steps = sorted(expected_steps - required_steps)
        observed_step_counts = reapproval_apply_plan_summary.get("observed_execution_step_counts") or {}
        if not isinstance(observed_step_counts, dict):
            observed_step_counts = {}
        missing_observed_steps: list[str] = []
        for step in sorted(expected_steps):
            try:
                observed_count = int(observed_step_counts.get(step) or 0)
            except (TypeError, ValueError):
                observed_count = 0
            if observed_count <= 0:
                missing_observed_steps.append(step)
        unsafe_issues = {
            "passed": reapproval_apply_plan_summary.get("passed"),
            "blocker_count": reapproval_apply_plan_summary.get("blocker_count"),
            "unsafe_contract_violation_count": reapproval_apply_plan_summary.get(
                "unsafe_contract_violation_count"
            ),
            "missing_required_execution_steps": missing_steps,
            "missing_observed_execution_steps": missing_observed_steps,
        }
        if (
            reapproval_apply_plan_summary.get("passed") is not True
            or int(reapproval_apply_plan_summary.get("blocker_count") or 0) > 0
            or int(reapproval_apply_plan_summary.get("unsafe_contract_violation_count") or 0) > 0
            or missing_steps
            or missing_observed_steps
        ):
            failures.append(
                {
                    "check": "json_artifact_mcp_product_reapproval_apply_plan_safe",
                    "artifact_path": artifact_path,
                    **unsafe_issues,
                }
            )
    if isinstance(runtime_drift, dict) and runtime_drift.get("reprocess_requires_reapproval") is True:
        if not isinstance(reapproval_summary, dict) or "reapproval_worklist_report" not in roles:
            failures.append(
                {
                    "check": "json_artifact_mcp_product_reapproval_worklist_required",
                    "artifact_path": artifact_path,
                    "reason": "runtime drift requires reapproval evidence and a reapproval worklist source artifact",
                }
            )
        if not isinstance(reapproval_batch_summary, dict) or "reapproval_review_batch_manifest_report" not in roles:
            failures.append(
                {
                    "check": "json_artifact_mcp_product_reapproval_review_batch_required",
                    "artifact_path": artifact_path,
                    "reason": "runtime drift requires reapproval review batch evidence and a reapproval review batch source artifact",
                }
            )
        if (
            not isinstance(reapproval_decision_validation_summary, dict)
            or "reapproval_decision_validation_report" not in roles
        ):
            failures.append(
                {
                    "check": "json_artifact_mcp_product_reapproval_decision_validation_required",
                    "artifact_path": artifact_path,
                    "reason": "runtime drift requires completed reapproval decision validation evidence and source artifact",
                }
            )
        if not isinstance(reapproval_apply_plan_summary, dict) or "reapproval_apply_plan_report" not in roles:
            failures.append(
                {
                    "check": "json_artifact_mcp_product_reapproval_apply_plan_required",
                    "artifact_path": artifact_path,
                    "reason": "runtime drift requires reapproval apply plan evidence and source artifact",
                }
            )
    if isinstance(revision_impact, dict):
        missing = [
            key
            for key in (
                "report_count",
                "changed_count",
                "added_count",
                "removed_count",
                "metadata_only_changed_count",
                "approval_required_count",
                "approval_reuse_candidate_count",
                "deindex_required_count",
            )
            if key not in revision_impact
        ]
        if missing:
            failures.append(
                {
                    "check": "json_artifact_mcp_product_revision_impact_summary",
                    "artifact_path": artifact_path,
                    "missing_fields": missing,
                }
            )
        if "revision_impact_report" not in roles:
            failures.append(
                {
                    "check": "json_artifact_mcp_product_revision_impact_source",
                    "artifact_path": artifact_path,
                    "reason": "revision_impact_summary requires a revision_impact_report source artifact",
                }
            )


def _append_product_readiness_source_file_failures(
    root: Path,
    item: dict[str, Any],
    artifact_path: str,
    failures: list[dict[str, Any]],
) -> None:
    role = str(item.get("role") or "")
    path_value = str(item.get("path") or "")
    sha256 = item.get("sha256")
    byte_count = item.get("byte_count")
    if not _source_artifact_fingerprint_complete(item):
        failures.append(
            {
                "check": "json_artifact_mcp_product_source_fingerprint_complete",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
            }
        )
        return
    resolved = _resolve_repo_artifact(root, path_value)
    if resolved is None:
        failures.append(
            {
                "check": "json_artifact_mcp_product_source_path",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
            }
        )
        return
    _normalized_path, candidate = resolved
    if not candidate.is_file():
        failures.append(
            {
                "check": "json_artifact_mcp_product_source_file",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
            }
        )
        return
    actual_size = candidate.stat().st_size
    if actual_size != byte_count:
        failures.append(
            {
                "check": "json_artifact_mcp_product_source_size",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
                "claimed_byte_count": byte_count,
                "actual_byte_count": actual_size,
            }
        )
    actual_sha = _sha256_file(candidate)
    if actual_sha != sha256:
        failures.append(
            {
                "check": "json_artifact_mcp_product_source_sha256",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
                "claimed_sha256": sha256,
                "actual_sha256": actual_sha,
            }
        )


def _append_authority_product_artifact_file_failures(
    root: Path,
    item: dict[str, Any],
    artifact_path: str,
    failures: list[dict[str, Any]],
) -> None:
    role = str(item.get("role") or "")
    path_value = str(item.get("path") or "")
    sha256 = item.get("sha256")
    byte_count = item.get("byte_count")
    if (
        role != "product_readiness"
        or not path_value
        or item.get("exists") is not True
        or not isinstance(byte_count, int)
        or byte_count <= 0
        or not _valid_sha256(sha256)
    ):
        failures.append(
            {
                "check": "json_artifact_mcp_authority_product_fingerprint_complete",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
            }
        )
        return
    resolved = _resolve_repo_artifact(root, path_value)
    if resolved is None:
        failures.append(
            {
                "check": "json_artifact_mcp_authority_product_path",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
            }
        )
        return
    normalized_path, candidate = resolved
    if normalized_path != "reports/mcp_product_readiness_current.json":
        failures.append(
            {
                "check": "json_artifact_mcp_authority_product_path",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
                "expected_path": "reports/mcp_product_readiness_current.json",
            }
        )
        return
    if not candidate.is_file():
        failures.append(
            {
                "check": "json_artifact_mcp_authority_product_file",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
            }
        )
        return
    actual_size = candidate.stat().st_size
    if actual_size != byte_count:
        failures.append(
            {
                "check": "json_artifact_mcp_authority_product_size",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
                "claimed_byte_count": byte_count,
                "actual_byte_count": actual_size,
            }
        )
    actual_sha = _sha256_file(candidate)
    if actual_sha != sha256:
        failures.append(
            {
                "check": "json_artifact_mcp_authority_product_sha256",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
                "claimed_sha256": sha256,
                "actual_sha256": actual_sha,
            }
        )
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        failures.append(
            {
                "check": "json_artifact_mcp_authority_product_json",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
            }
        )
        return
    if not isinstance(payload, dict) or payload.get("report_type") != "mcp_product_readiness" or payload.get("passed") is not True:
        failures.append(
            {
                "check": "json_artifact_mcp_authority_product_json",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
                "report_type": payload.get("report_type") if isinstance(payload, dict) else None,
                "passed": payload.get("passed") if isinstance(payload, dict) else None,
            }
        )


def _append_authority_role_artifact_file_failures(
    root: Path,
    item: dict[str, Any],
    artifact_path: str,
    failures: list[dict[str, Any]],
    *,
    expected_path: str,
    expected_report_type: str,
) -> None:
    role = str(item.get("role") or "")
    path_value = str(item.get("path") or "")
    sha256 = item.get("sha256")
    byte_count = item.get("byte_count")
    if (
        not role
        or not path_value
        or item.get("exists") is not True
        or not isinstance(byte_count, int)
        or byte_count <= 0
        or not _valid_sha256(sha256)
    ):
        failures.append(
            {
                "check": "json_artifact_mcp_authority_role_fingerprint_complete",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
            }
        )
        return
    resolved = _resolve_repo_artifact(root, path_value)
    if resolved is None:
        failures.append(
            {
                "check": "json_artifact_mcp_authority_role_path",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
            }
        )
        return
    normalized_path, candidate = resolved
    if normalized_path != expected_path:
        failures.append(
            {
                "check": "json_artifact_mcp_authority_role_path",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
                "expected_path": expected_path,
            }
        )
        return
    if not candidate.is_file():
        failures.append(
            {
                "check": "json_artifact_mcp_authority_role_file",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
            }
        )
        return
    actual_size = candidate.stat().st_size
    if actual_size != byte_count:
        failures.append(
            {
                "check": "json_artifact_mcp_authority_role_size",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
                "claimed_byte_count": byte_count,
                "actual_byte_count": actual_size,
            }
        )
    actual_sha = _sha256_file(candidate)
    if actual_sha != sha256:
        failures.append(
            {
                "check": "json_artifact_mcp_authority_role_sha256",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
                "claimed_sha256": sha256,
                "actual_sha256": actual_sha,
            }
        )
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        failures.append(
            {
                "check": "json_artifact_mcp_authority_role_json",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
            }
        )
        return
    if not isinstance(payload, dict) or payload.get("report_type") != expected_report_type or payload.get("passed") is not True:
        failures.append(
            {
                "check": "json_artifact_mcp_authority_role_json",
                "artifact_path": artifact_path,
                "role": role,
                "source_path": path_value,
                "report_type": payload.get("report_type") if isinstance(payload, dict) else None,
                "expected_report_type": expected_report_type,
                "passed": payload.get("passed") if isinstance(payload, dict) else None,
            }
        )


def _source_artifact_fingerprint_complete(item: dict[str, Any]) -> bool:
    return (
        bool(str(item.get("path") or "").strip())
        and _valid_sha256(item.get("sha256"))
        and isinstance(item.get("byte_count"), int)
        and item.get("byte_count") > 0
        and bool(str(item.get("modified_at") or "").strip())
    )


def _int_value(value: Any) -> int:
    if isinstance(value, bool) or value in (None, ""):
        return 0
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _append_repo_commit_consistency_failures(
    index: dict[str, Any],
    observed_repo_commits: dict[str, str],
    failures: list[dict[str, Any]],
    *,
    require_repo_commit: bool,
) -> str | None:
    index_repo_commit = index.get("repo_commit")
    verified_repo_commit = index_repo_commit.strip() if isinstance(index_repo_commit, str) else None
    if not verified_repo_commit or not COMMIT_RE.fullmatch(verified_repo_commit):
        if require_repo_commit or verified_repo_commit:
            failures.append(
                {
                    "check": "index_repo_commit",
                    "reason": "release evidence index must carry one full 40-character repo_commit",
                    "observed": index_repo_commit,
                }
            )
        verified_repo_commit = None
    unique_commits = sorted(set(observed_repo_commits.values()))
    if len(unique_commits) > 1:
        failures.append(
            {
                "check": "json_artifact_repo_commit_consistency",
                "commit_count": len(unique_commits),
                "artifacts": observed_repo_commits,
            }
        )
    if verified_repo_commit and len(unique_commits) == 1 and verified_repo_commit != unique_commits[0]:
        failures.append(
            {
                "check": "index_repo_commit_consistency",
                "index_repo_commit": verified_repo_commit,
                "artifact_repo_commit": unique_commits[0],
            }
        )
    return verified_repo_commit


def _append_repo_worktree_warnings(index: dict[str, Any], warnings: list[dict[str, Any]]) -> None:
    worktree = index.get("repo_worktree")
    if not isinstance(worktree, dict):
        return
    state = worktree.get("state")
    if state == "dirty" or worktree.get("dirty") is True:
        warnings.append(
            {
                "check": "index_repo_worktree_dirty",
                "tracked_change_count": worktree.get("tracked_change_count"),
                "untracked_change_count": worktree.get("untracked_change_count"),
                "reason": "evidence was generated from a working tree with uncommitted changes",
            }
        )
    elif state not in {"clean", "unknown"}:
        warnings.append(
            {
                "check": "index_repo_worktree_state",
                "observed": state,
                "reason": "repo_worktree.state should be clean, dirty, or unknown",
            }
        )


def _profile_requires_repo_commit(evidence_profile: str) -> bool:
    return evidence_profile in {"private-release", "mcp-product-readiness"}


def _append_required_artifact_failures(
    index: dict[str, Any],
    observed_artifact_paths: set[str],
    failures: list[dict[str, Any]],
    evidence_context: dict[str, Any],
    evidence_profile: str,
) -> None:
    if index.get("index_version") != 1:
        return
    if evidence_profile == "hermes-mcp":
        missing_reports = sorted(HERMES_MCP_REQUIRED_ARTIFACTS - observed_artifact_paths)
        if missing_reports:
            failures.append({"check": "required_hermes_mcp_artifacts_present", "missing_artifacts": missing_reports})
        return
    if evidence_profile == "mcp-product-readiness":
        missing_reports = sorted(MCP_PRODUCT_READINESS_REQUIRED_ARTIFACTS - observed_artifact_paths)
        if missing_reports:
            failures.append({"check": "required_mcp_product_readiness_artifacts_present", "missing_artifacts": missing_reports})
        return
    missing_reports = sorted(PRIVATE_REQUIRED_REPORT_ARTIFACTS - observed_artifact_paths)
    if missing_reports:
        failures.append({"check": "required_report_artifacts_present", "missing_artifacts": missing_reports})
    if not any(path.startswith("dist/") and path.endswith(".whl") for path in observed_artifact_paths):
        failures.append({"check": "required_wheel_artifact_present", "required_pattern": "dist/*.whl"})
    if not any(path.startswith("dist/") and path.endswith(".tar.gz") for path in observed_artifact_paths):
        failures.append({"check": "required_sdist_artifact_present", "required_pattern": "dist/*.tar.gz"})
    suppressed_count = evidence_context.get("release_hygiene_suppressed_finding_count")
    if isinstance(suppressed_count, int) and suppressed_count > 0 and ALLOWLIST_ARTIFACT not in observed_artifact_paths:
        failures.append(
            {
                "check": "required_release_hygiene_allowlist_artifact_present",
                "required_artifact": ALLOWLIST_ARTIFACT,
                "suppressed_finding_count": suppressed_count,
            }
        )


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA256_RE.fullmatch(value))


def _nonnegative_int_or_none(value: Any, *, default: int) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _append_hermes_bundle_json_failures(
    payload: dict[str, Any],
    artifact_path: str,
    failures: list[dict[str, Any]],
) -> None:
    missing_top_level = sorted(key for key in HERMES_BUNDLE_JSON_KEYS if key not in payload)
    if missing_top_level:
        failures.append(
            {
                "check": "json_artifact_hermes_bundle_shape",
                "artifact_path": artifact_path,
                "missing_keys": missing_top_level,
            }
        )
        return
    quickstart = payload.get("quickstart")
    if not isinstance(quickstart, dict):
        failures.append(
            {
                "check": "json_artifact_hermes_bundle_quickstart",
                "artifact_path": artifact_path,
                "reason": "quickstart must be an object",
            }
        )
        return
    missing_quickstart = sorted(key for key in HERMES_BUNDLE_QUICKSTART_KEYS if key not in quickstart)
    if missing_quickstart:
        failures.append(
            {
                "check": "json_artifact_hermes_bundle_quickstart",
                "artifact_path": artifact_path,
                "missing_keys": missing_quickstart,
            }
        )


def _append_hermes_zip_artifact_failures(
    path: Path,
    normalized_artifact_path: str,
    artifact_path: str,
    failures: list[dict[str, Any]],
) -> None:
    if normalized_artifact_path != "reports/mcp_connection_bundle_hermes.zip":
        return
    try:
        with zipfile.ZipFile(path) as archive:
            bad_file = archive.testzip()
            names = set(archive.namelist())
    except zipfile.BadZipFile as exc:
        failures.append({"check": "zip_artifact_readable", "artifact_path": artifact_path, "reason": str(exc)})
        return
    if bad_file:
        failures.append({"check": "zip_artifact_integrity", "artifact_path": artifact_path, "bad_file": bad_file})
    unsafe_entries = sorted(name for name in names if _unsafe_artifact_path(name))
    if unsafe_entries:
        failures.append({"check": "zip_artifact_entry_safe", "artifact_path": artifact_path, "entries": unsafe_entries})
    missing_entries = sorted(HERMES_BUNDLE_ZIP_REQUIRED_ENTRIES - names)
    if missing_entries:
        failures.append(
            {
                "check": "zip_artifact_hermes_bundle_entries",
                "artifact_path": artifact_path,
                "missing_entries": missing_entries,
            }
        )
    if not any(name.endswith(".whl") for name in names):
        failures.append(
            {
                "check": "zip_artifact_hermes_bundle_wheel",
                "artifact_path": artifact_path,
                "required_pattern": "*.whl",
            }
        )


def _unsafe_artifact_path(value: str) -> bool:
    normalized = _normalize_artifact_path(value)
    if not normalized:
        return True
    return normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized) is not None or ".." in normalized.split("/")


def _resolve_repo_artifact(root: Path, path_value: str) -> tuple[str, Path] | None:
    if not path_value:
        return None
    root = root.resolve()
    raw_path = Path(path_value)
    if raw_path.is_absolute():
        candidate = raw_path.resolve()
        try:
            normalized_path = candidate.relative_to(root).as_posix()
        except ValueError:
            return None
        if _unsafe_artifact_path(normalized_path):
            return None
        return normalized_path, candidate
    normalized_path = _normalize_artifact_path(path_value)
    if _unsafe_artifact_path(normalized_path):
        return None
    candidate = (root / normalized_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return normalized_path, candidate


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _normalize_artifact_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify a release evidence index for missing or failed artifacts.")
    parser.add_argument("--index-json", required=True, help="Path to release evidence index JSON.")
    parser.add_argument("--repo-root", default=".", help="Repository root used to resolve indexed artifact paths.")
    parser.add_argument("--out-json", default=None, help="Optional path to write verification JSON.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    args = parse_args(argv)
    try:
        index = json.loads(Path(args.index_json).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        report = {
            "report_type": "release_evidence_bundle_verification",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "passed": False,
            "artifact_count": 0,
            "failure_count": 1,
            "failures": [{"check": "index_json_readable", "reason": str(exc)}],
        }
    else:
        report = verify_release_evidence_index(index if isinstance(index, dict) else {}, repo_root=args.repo_root)

    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out_json:
        out_json = Path(args.out_json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(output + "\n", encoding="utf-8")
    print(output, file=stdout or sys.stdout)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
