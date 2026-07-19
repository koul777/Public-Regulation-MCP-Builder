from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Sequence, TextIO
import urllib.error
import urllib.request

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.audit_mcp_index_visibility import audit_mcp_index_visibility
from scripts.mcp_bundle_contract import REQUIRED_SETUP_BUNDLE_FILES
from scripts.report_metadata import current_repo_commit
from app.core.tenant_access import tenant_storage_key


PROFILE_ALIASES = {"chatgpt": "chatgpt-remote"}
REMOTE_CLIENTS = {"chatgpt-remote", "claude-api"}
VALID_CLIENTS = {
    "bundle",
    "claude-desktop",
    "claude-code",
    "chatgpt-desktop-local",
    "chatgpt-remote",
    "chatgpt",
    "claude-api",
}
VALID_CONNECTION_MODES = {"direct", "openai-tunnel"}
PLACEHOLDER_VALUES = {"", "<strong-token>", "<strong-internal-token>", "<strong-approved-token>", "<runtime-api-key>", "<tunnel_id>"}
BUNDLE_REQUIRED_FILES = REQUIRED_SETUP_BUNDLE_FILES
BUNDLE_FORBIDDEN_PATTERNS = {
    '$env:MCP_AUTH_TOKEN =': "bundle-token-assignment",
    '$env:CONTROL_PLANE_API_KEY =': "bundle-token-assignment",
    '$env:OPENAI_TUNNEL_ID =': "bundle-token-assignment",
    "<strong-token>": "bundle-placeholder-secret",
    "<strong-internal-token>": "bundle-placeholder-secret",
    "<strong-approved-token>": "bundle-placeholder-secret",
    "<runtime-api-key>": "bundle-placeholder-secret",
    "<tunnel_id>": "bundle-placeholder-secret",
    "Replace the generated <strong-token>": "bundle-stale-secret-instructions",
    "Edit runtime API key": "bundle-stale-secret-instructions",
}
RUNTIME_REPOSITORY_RESULT_SUFFIXES = ("_chunks.json", "_nodes.json", "_issues.json", "_quality.json")


@dataclass(frozen=True)
class McpConnectionFinding:
    severity: str
    code: str
    detail: str
    remediation: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def check_mcp_connection_readiness(
    *,
    client_profile: str,
    connection_mode: str = "direct",
    transport: str = "stdio",
    host: str = "127.0.0.1",
    public_url: str | None = None,
    token_env: str | None = "MCP_AUTH_TOKEN",
    tunnel_id_env: str = "OPENAI_TUNNEL_ID",
    control_plane_api_key_env: str = "CONTROL_PLANE_API_KEY",
    data_dir: str | Path = "data",
    bundle_dir: str | Path | None = None,
    server_name: str = "regulation_mcp",
    codex_config: str | Path | None = None,
    claude_desktop_config: str | Path | None = None,
    check_cli: bool = True,
    check_data: bool = True,
    require_full_index: bool = False,
    audit_index_visibility: bool = False,
    tenant_id: str | None = None,
    tenant_storage_isolation: bool = False,
    min_visible_records: int = 1,
    require_indexed: bool = False,
    forbid_smoke_docs: bool = False,
    probe_public_url: bool = False,
    probe_timeout_seconds: float = 5.0,
    allow_local_only_bundle: bool = False,
) -> dict[str, object]:
    requested_profile = client_profile.strip().lower()
    if requested_profile not in VALID_CLIENTS:
        raise ValueError(
            "client_profile must be bundle, claude-desktop, claude-code, chatgpt-desktop-local, "
            "chatgpt-remote, chatgpt (legacy alias), or claude-api."
        )
    profile = PROFILE_ALIASES.get(requested_profile, requested_profile)
    mode = connection_mode.strip().lower()
    if mode not in VALID_CONNECTION_MODES:
        raise ValueError("connection_mode must be direct or openai-tunnel.")
    normalized_transport = transport.strip().lower()
    if normalized_transport not in {"stdio", "streamable-http", "sse"}:
        raise ValueError("transport must be stdio, streamable-http, or sse.")

    findings: list[McpConnectionFinding] = []
    visibility_integrity_requested = bool(
        require_indexed or forbid_smoke_docs or min_visible_records != 1
    )
    if check_data and audit_index_visibility and not tenant_id:
        findings.append(
            McpConnectionFinding(
                "high",
                "index-visibility-tenant-required",
                "Index visibility auditing requires an explicit tenant_id.",
                "Pass --tenant-id with --audit-index-visibility before checking MCP-visible records.",
            )
        )
        audit_index_visibility = False
    elif check_data and visibility_integrity_requested and not audit_index_visibility:
        if tenant_id:
            # Integrity flags must not be silently ignored when the caller
            # omits the lower-level audit switch.
            audit_index_visibility = True
        else:
            findings.append(
                McpConnectionFinding(
                    "high",
                    "index-visibility-tenant-required",
                    "Index integrity flags require an explicit tenant_id for the visibility audit.",
                    "Pass --tenant-id together with --audit-index-visibility, --require-indexed, or --forbid-smoke-docs.",
                )
            )
    uses_openai_tunnel = mode == "openai-tunnel"
    remote_required = not uses_openai_tunnel and (
        profile in REMOTE_CLIENTS or (profile == "bundle" and public_url is not None)
    )
    if profile in REMOTE_CLIENTS and normalized_transport == "stdio" and not uses_openai_tunnel:
        findings.append(
            McpConnectionFinding(
                "high",
                "remote-client-stdio",
                f"{profile} cannot connect directly to a local stdio MCP server.",
                "Use --transport streamable-http and provide --public-url https://.../mcp.",
            )
        )
    if uses_openai_tunnel:
        _check_openai_tunnel(
            profile=profile,
            tunnel_id_env=tunnel_id_env,
            control_plane_api_key_env=control_plane_api_key_env,
            check_cli=check_cli,
            findings=findings,
        )
    if remote_required:
        _check_public_url(public_url, findings)
        remote_probe = _build_remote_probe(public_url=public_url, performed=False, passed=None, detail="configuration_only")
        if probe_public_url:
            remote_probe = _probe_public_url(public_url=public_url, timeout_seconds=probe_timeout_seconds, findings=findings)
        if normalized_transport in {"streamable-http", "sse"} and _is_loopback_host(host):
            findings.append(
                McpConnectionFinding(
                    "medium",
                    "remote-loopback-host",
                    "Remote client setup is requested but the generated server host is loopback.",
                    "Use --host 0.0.0.0 behind approved controls, or terminate HTTPS on a reverse proxy on the same host.",
                )
            )
    else:
        remote_probe = _build_remote_probe(public_url=public_url, performed=False, passed=None, detail="not_required")
    if normalized_transport in {"streamable-http", "sse"} and not _is_loopback_host(host):
        if not token_env:
            findings.append(
                McpConnectionFinding(
                    "high",
                    "missing-http-auth-token-env",
                    "Non-loopback HTTP/SSE MCP is configured without a bearer-token environment variable.",
                    "Set --token-env MCP_AUTH_TOKEN or place the MCP server behind approved authenticated controls.",
                )
            )
        elif not os.getenv(token_env):
            findings.append(
                McpConnectionFinding(
                    "medium",
                    "http-auth-token-env-empty",
                    f"Environment variable {token_env} is not set in this shell.",
                    f"Set $env:{token_env} before starting the HTTP MCP server.",
                )
            )
        elif _is_placeholder_secret(os.getenv(token_env)):
            findings.append(
                McpConnectionFinding(
                    "high",
                    "http-auth-token-env-placeholder",
                    f"Environment variable {token_env} still contains a generated placeholder value.",
                    f"Set $env:{token_env} to a real approved token before starting the HTTP MCP server.",
                )
            )
    if check_cli and profile == "claude-code" and not shutil.which("claude"):
        findings.append(
            McpConnectionFinding(
                "medium",
                "claude-cli-not-found",
                "The claude CLI is not available on PATH.",
                "Install Claude Code or run the generated Claude Code command on a machine where claude is installed.",
            )
        )
    effective_data_dir = _effective_readiness_data_dir(data_dir, bundle_dir)
    if check_data:
        _check_data_dir(effective_data_dir, findings, require_full_index=require_full_index)
    index_visibility_summary = None
    if check_data and audit_index_visibility:
        index_visibility_summary = _check_index_visibility(
            data_dir=effective_data_dir,
            tenant_id=tenant_id,
            tenant_storage_isolation=tenant_storage_isolation,
            min_visible_records=min_visible_records,
            require_indexed=require_indexed,
            forbid_smoke_docs=forbid_smoke_docs,
            findings=findings,
        )
    if bundle_dir is not None:
        bundle_path = Path(bundle_dir)
        _check_bundle_dir(bundle_path, findings, allow_local_only_bundle=allow_local_only_bundle)
        bundle_connection_summary = _bundle_connection_summary(bundle_path)
    else:
        bundle_connection_summary = None
    installed_client_config_summary = _check_installed_client_configs(
        effective_data_dir=effective_data_dir,
        server_name=server_name,
        codex_config=codex_config,
        claude_desktop_config=claude_desktop_config,
        tenant_storage_isolation=tenant_storage_isolation,
        findings=findings,
    )

    findings = _dedupe_findings(findings)
    high_count = sum(1 for finding in findings if finding.severity == "high")
    medium_count = sum(1 for finding in findings if finding.severity == "medium")
    deploy_ready = high_count == 0 and (not remote_required or (remote_probe["performed"] and remote_probe["passed"]))
    return {
        "report_type": "mcp_connection_readiness",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": _safe_current_repo_commit(),
        "client_profile": profile,
        "readiness_scope": "deploy" if remote_probe["performed"] else "configuration",
        "connection_mode": mode,
        "transport": normalized_transport,
        "host": host,
        "public_url": _normalize_mcp_url(public_url),
        "data_dir": str(data_dir),
        "effective_data_dir": str(effective_data_dir),
        "tenant_id": tenant_id,
        "tenant_storage_isolation": tenant_storage_isolation,
        "bundle_dir": str(bundle_dir) if bundle_dir is not None else None,
        "allow_local_only_bundle": allow_local_only_bundle,
        "bundle_connection_summary": bundle_connection_summary,
        "installed_client_config_summary": installed_client_config_summary,
        "passed": high_count == 0,
        "deploy_ready": deploy_ready,
        "remote_probe": remote_probe,
        "mcp_index_visibility_summary": index_visibility_summary,
        "high_count": high_count,
        "medium_count": medium_count,
        "finding_count": len(findings),
        "findings": [finding.to_dict() for finding in findings],
    }


def _dedupe_findings(findings: Sequence[McpConnectionFinding]) -> list[McpConnectionFinding]:
    deduped: list[McpConnectionFinding] = []
    seen: set[tuple[str, str, str, str]] = set()
    for finding in findings:
        key = (finding.severity, finding.code, finding.detail, finding.remediation)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
    return deduped


def _safe_current_repo_commit() -> str | None:
    try:
        return current_repo_commit(PROJECT_ROOT)
    except OSError:
        return None


def _effective_readiness_data_dir(data_dir: str | Path, bundle_dir: str | Path | None) -> Path:
    data_path = Path(data_dir)
    if bundle_dir is not None and data_path == Path("data"):
        return Path(bundle_dir) / "data"
    return data_path


def _check_index_visibility(
    *,
    data_dir: Path,
    tenant_id: str | None,
    tenant_storage_isolation: bool,
    min_visible_records: int,
    require_indexed: bool,
    forbid_smoke_docs: bool,
    findings: list[McpConnectionFinding],
) -> dict[str, object] | None:
    if not tenant_id:
        findings.append(
            McpConnectionFinding(
                "high",
                "mcp-index-visibility-tenant-missing",
                "Index visibility audit was requested without a tenant id.",
                "Pass --tenant-id with the same tenant used by reg-rag-mcp-server.",
            )
        )
        return None
    try:
        report = audit_mcp_index_visibility(
            data_dir=data_dir,
            tenant_id=tenant_id,
            tenant_storage_isolation=tenant_storage_isolation,
            min_visible_records=min_visible_records,
            forbid_smoke_docs=forbid_smoke_docs,
            require_indexed=require_indexed,
        )
    except Exception as exc:
        findings.append(
            McpConnectionFinding(
                "high",
                "mcp-index-visibility-audit-failed",
                f"Index visibility audit failed: {exc}",
                "Verify --data-dir, --tenant-id, tenant isolation mode, and approved vector metadata.",
            )
        )
        return None
    for finding in report.get("findings") or []:
        findings.append(
            McpConnectionFinding(
                str(finding.get("severity") or "high"),
                f"mcp-index-{finding.get('code') or 'finding'}",
                str(finding.get("detail") or "MCP index visibility audit reported an issue."),
                str(finding.get("remediation") or "Fix the MCP runtime data before connecting clients."),
            )
        )
    return {
        "passed": report.get("passed"),
        "tenant_id": report.get("tenant_id"),
        "effective_data_dir": report.get("effective_data_dir"),
        "document_count": report.get("document_count"),
        "total_approved_chunks": report.get("total_approved_chunks"),
        "total_indexable_record_count": report.get("total_indexable_record_count"),
        "total_mcp_visible_records": report.get("total_mcp_visible_records"),
        "status_counts": report.get("status_counts"),
        "smoke_like_document_count": report.get("smoke_like_document_count"),
        "parser_evidence_summary": report.get("parser_evidence_summary"),
        "approval_provenance_coverage": report.get("approval_provenance_coverage"),
        "approval_journal_coverage": report.get("approval_journal_coverage"),
        "finding_count": report.get("finding_count"),
    }


def _check_openai_tunnel(
    *,
    profile: str,
    tunnel_id_env: str,
    control_plane_api_key_env: str,
    check_cli: bool,
    findings: list[McpConnectionFinding],
) -> None:
    if profile == "claude-api":
        findings.append(
            McpConnectionFinding(
                "high",
                "openai-tunnel-not-claude-api",
                "OpenAI Secure MCP Tunnel is for supported OpenAI products, not Claude API.",
                "Use direct HTTPS /mcp for Claude API, or connect Claude Desktop/Code locally with stdio/http.",
            )
        )
    if check_cli and not shutil.which("tunnel-client"):
        findings.append(
            McpConnectionFinding(
                "medium",
                "tunnel-client-not-found",
                "The tunnel-client CLI is not available on PATH.",
                "Install tunnel-client from OpenAI Platform tunnel settings or the latest openai/tunnel-client release.",
            )
        )
    tunnel_id = os.getenv(tunnel_id_env) if tunnel_id_env else None
    control_plane_api_key = os.getenv(control_plane_api_key_env) if control_plane_api_key_env else None
    if tunnel_id_env and not tunnel_id:
        findings.append(
            McpConnectionFinding(
                "medium",
                "openai-tunnel-id-env-empty",
                f"Environment variable {tunnel_id_env} is not set in this shell.",
                f"Set $env:{tunnel_id_env} to the OpenAI tunnel_id before running tunnel-client.",
            )
        )
    elif _is_placeholder_secret(tunnel_id):
        findings.append(
            McpConnectionFinding(
                "high",
                "openai-tunnel-id-env-placeholder",
                f"Environment variable {tunnel_id_env} still contains a generated placeholder value.",
                f"Set $env:{tunnel_id_env} to the real OpenAI tunnel_id before running tunnel-client.",
            )
        )
    if control_plane_api_key_env and not control_plane_api_key:
        findings.append(
            McpConnectionFinding(
                "medium",
                "openai-control-plane-api-key-env-empty",
                f"Environment variable {control_plane_api_key_env} is not set in this shell.",
                f"Set $env:{control_plane_api_key_env} to the runtime API key used by tunnel-client.",
            )
        )
    elif _is_placeholder_secret(control_plane_api_key):
        findings.append(
            McpConnectionFinding(
                "high",
                "openai-control-plane-api-key-env-placeholder",
                f"Environment variable {control_plane_api_key_env} still contains a generated placeholder value.",
                f"Set $env:{control_plane_api_key_env} to the real runtime API key used by tunnel-client.",
            )
        )


def _check_public_url(public_url: str | None, findings: list[McpConnectionFinding]) -> None:
    if not public_url:
        findings.append(
            McpConnectionFinding(
                "high",
                "missing-public-url",
                "ChatGPT and Claude API remote MCP connectors need a reachable HTTPS /mcp URL.",
                "Regenerate with --public-url https://your-host.example/mcp.",
            )
        )
        return
    cleaned = public_url.strip().rstrip("/")
    normalized = _normalize_mcp_url(public_url)
    if not normalized.startswith("https://"):
        findings.append(
            McpConnectionFinding(
                "high",
                "public-url-not-https",
                "Remote MCP URL must use HTTPS.",
                "Use an approved HTTPS endpoint such as https://mcp.example.go.kr/mcp.",
            )
        )
    if not cleaned.endswith("/mcp"):
        findings.append(
            McpConnectionFinding(
                "medium",
                "public-url-missing-mcp-suffix",
                "Remote MCP URL does not end with /mcp.",
                "Use the generated connector_url value or append /mcp to the HTTPS base URL.",
            )
        )


def _build_remote_probe(
    *,
    public_url: str | None,
    performed: bool,
    passed: bool | None,
    detail: str,
    status_code: int | None = None,
) -> dict[str, object]:
    return {
        "performed": performed,
        "passed": passed,
        "url": _normalize_mcp_url(public_url),
        "status_code": status_code,
        "detail": detail,
    }


def _probe_public_url(
    *,
    public_url: str | None,
    timeout_seconds: float,
    findings: list[McpConnectionFinding],
) -> dict[str, object]:
    normalized = _normalize_mcp_url(public_url)
    if not normalized or not normalized.startswith("https://"):
        findings.append(
            McpConnectionFinding(
                "high",
                "public-url-probe-not-available",
                "Remote MCP URL probe requires a valid HTTPS public_url.",
                "Provide --public-url https://.../mcp before enabling --probe-public-url.",
            )
        )
        return _build_remote_probe(public_url=public_url, performed=True, passed=False, detail="invalid_public_url")
    request = urllib.request.Request(normalized, method="GET", headers={"User-Agent": "reg-rag-mcp-doctor/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status_code = int(getattr(response, "status", 0) or 0)
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        findings.append(
            McpConnectionFinding(
                "high",
                "public-url-probe-failed",
                f"Could not reach remote MCP URL: {exc}",
                "Start the HTTPS MCP endpoint or tunnel, verify DNS/proxy/TLS, then rerun with --probe-public-url.",
            )
        )
        return _build_remote_probe(public_url=public_url, performed=True, passed=False, detail=str(exc))

    reachable_statuses = {200, 204, 400, 401, 403, 405}
    if status_code not in reachable_statuses:
        findings.append(
            McpConnectionFinding(
                "high",
                "public-url-probe-bad-status",
                f"Remote MCP URL responded with HTTP {status_code}.",
                "Verify the /mcp endpoint is routed to the MCP server and protected by the expected authentication layer.",
            )
        )
        return _build_remote_probe(
            public_url=public_url,
            performed=True,
            passed=False,
            detail="unexpected_status",
            status_code=status_code,
        )
    return _build_remote_probe(
        public_url=public_url,
        performed=True,
        passed=True,
        detail="reachable",
        status_code=status_code,
    )


def _check_data_dir(data_dir: Path, findings: list[McpConnectionFinding], *, require_full_index: bool = False) -> None:
    if not data_dir.exists():
        findings.append(
            McpConnectionFinding(
                "medium",
                "data-dir-missing",
                f"Data directory does not exist: {data_dir}",
                "Run preprocessing and approval/indexing first, or pass --data-dir for the target tenant data directory.",
            )
        )
        return
    if not data_dir.joinpath("vector_db").exists() and not data_dir.joinpath("tenants").exists():
        findings.append(
            McpConnectionFinding(
                "medium",
                "local-index-not-detected",
                f"No vector_db or tenants directory detected under {data_dir}.",
                "Approve and index at least one document before expecting MCP search results.",
            )
        )
    smoke_artifacts = _find_smoke_artifacts(data_dir)
    if smoke_artifacts:
        sample = ", ".join(path.name for path in smoke_artifacts[:5])
        findings.append(
            McpConnectionFinding(
                "high",
                "mcp-smoke-docs-present",
                f"Runtime data contains synthetic MCP smoke documents: {sample}",
                "Regenerate the MCP runtime after clearing the target tenant directory, or remove smoke documents before publishing.",
            )
        )
    candidates = _runtime_data_candidates(data_dir)
    manifest_candidates: list[Path] = []
    for candidate in [data_dir, *candidates]:
        if candidate not in manifest_candidates:
            manifest_candidates.append(candidate)
    synthetic_manifest_candidates = [
        candidate
        for candidate in manifest_candidates
        if _runtime_manifest_payload(candidate).get("synthetic_runtime") is True
    ]
    if synthetic_manifest_candidates:
        sample = ", ".join(str(path / "mcp_runtime_manifest.json") for path in synthetic_manifest_candidates[:5])
        findings.append(
            McpConnectionFinding(
                "high",
                "synthetic-runtime-manifest",
                f"Runtime manifest marks synthetic provenance: {sample}",
                "Regenerate the runtime from an approved publish bundle before production connection.",
            )
        )
    if not candidates:
        return
    summaries = [_summarize_runtime_data(candidate) for candidate in candidates]
    for candidate in candidates:
        _check_runtime_data_document_consistency(candidate, findings, require_manifest=True)
    if not any(summary["vector_record_count"] for summary in summaries):
        findings.append(
            McpConnectionFinding(
                "medium",
                "approved-vector-records-missing",
                f"No approved vector records were found under {data_dir}.",
                "Approve and index the target regulation corpus before connecting MCP clients.",
            )
        )
    if require_full_index:
        for summary in summaries:
            chunk_count = int(summary["chunk_count"])
            vector_count = int(summary["vector_record_count"])
            if chunk_count and vector_count != chunk_count:
                findings.append(
                    McpConnectionFinding(
                        "high",
                        "mcp-runtime-not-fully-indexed",
                        f"{summary['runtime_dir']} has {chunk_count} repository chunks but {vector_count} approved vector records.",
                        "Regenerate with full-corpus approval/indexing, or rerun without --require-full-index only after explicitly accepting a partial MCP.",
                    )
                )


def _runtime_data_candidates(data_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    if data_dir.joinpath("repository").exists() or data_dir.joinpath("vector_db").exists():
        candidates.append(data_dir)
    tenants_dir = data_dir / "tenants"
    if tenants_dir.is_dir():
        for tenant_dir in sorted(tenants_dir.iterdir()):
            if tenant_dir.is_dir() and (tenant_dir.joinpath("repository").exists() or tenant_dir.joinpath("vector_db").exists()):
                candidates.append(tenant_dir)
    return candidates


def _summarize_runtime_data(runtime_dir: Path) -> dict[str, object]:
    chunk_count = 0
    for path in sorted(runtime_dir.joinpath("repository").glob("*_chunks.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(payload, list):
            chunk_count += len(payload)
    vector_record_count = 0
    vector_dir = runtime_dir / "vector_db"
    for path in sorted(vector_dir.rglob("approved_vectors.jsonl")) if vector_dir.exists() else []:
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                vector_record_count += sum(1 for line in handle if line.strip())
        except (OSError, UnicodeDecodeError):
            continue
    return {
        "runtime_dir": str(runtime_dir),
        "chunk_count": chunk_count,
        "vector_record_count": vector_record_count,
    }


def _check_runtime_data_document_consistency(
    runtime_dir: Path,
    findings: list[McpConnectionFinding],
    *,
    require_manifest: bool = True,
) -> None:
    expected_ids = _runtime_manifest_document_ids(runtime_dir)
    if not expected_ids:
        manifest_required = _runtime_data_files_requiring_manifest(runtime_dir) if require_manifest else []
        if require_manifest and manifest_required:
            sample = ", ".join(path.relative_to(runtime_dir).as_posix() for path in manifest_required[:5])
            findings.append(
                McpConnectionFinding(
                    "high",
                    "mcp-runtime-manifest-missing",
                    "Runtime data contains repository/vector artifacts but is missing a valid "
                    f"mcp_runtime_manifest.json with document_ids: {sample}",
                    "Regenerate the MCP runtime bundle from a clean export directory so the runtime manifest, repository artifacts, and vector indexes agree.",
                )
            )
        if not require_manifest:
            return
        disallowed = _disallowed_runtime_repository_result_files(runtime_dir)
        if disallowed:
            sample = ", ".join(path.relative_to(runtime_dir).as_posix() for path in disallowed[:5])
            findings.append(
                McpConnectionFinding(
                    "high",
                    "mcp-runtime-raw-preprocessing-artifacts",
                    f"Runtime data contains raw preprocessing result artifacts that must not be shipped: {sample}",
                    "Regenerate the MCP runtime bundle from a clean export directory so only approved chunks, manifests, journals, and vector indexes are included.",
                )
            )
        return
    disallowed = _disallowed_runtime_repository_result_files(runtime_dir)
    if disallowed:
        sample = ", ".join(path.relative_to(runtime_dir).as_posix() for path in disallowed[:5])
        findings.append(
            McpConnectionFinding(
                "high",
                "mcp-runtime-raw-preprocessing-artifacts",
                f"Runtime data contains raw preprocessing result artifacts that must not be shipped: {sample}",
                "Regenerate the MCP runtime bundle from a clean export directory so only approved chunks, manifests, journals, and vector indexes are included.",
            )
        )
    unexpected_vectors = _unexpected_runtime_vector_store_files(runtime_dir)
    if unexpected_vectors:
        sample = ", ".join(path.relative_to(runtime_dir).as_posix() for path in unexpected_vectors[:5])
        findings.append(
            McpConnectionFinding(
                "high",
                "mcp-runtime-cross-tenant-vector-artifacts",
                f"Runtime data contains vector store files outside the manifest tenant: {sample}",
                "Regenerate the MCP runtime bundle from a clean export directory for the intended tenant only.",
            )
        )
    document_sets = {
        "repository result files": _repository_result_file_document_ids(runtime_dir),
        "repository manifest": _repository_manifest_document_ids(runtime_dir),
        "approval snapshot": _approval_snapshot_document_ids(runtime_dir),
        "approved vectors": _vector_document_ids(runtime_dir),
    }
    stale: list[str] = []
    for label, document_ids in document_sets.items():
        extra = sorted(document_ids - expected_ids)
        if extra:
            stale.append(f"{label}: {', '.join(extra[:5])}")
    if stale:
        findings.append(
            McpConnectionFinding(
                "high",
                "mcp-runtime-stale-document-artifacts",
                "Runtime data contains document artifacts outside mcp_runtime_manifest.document_ids: "
                + "; ".join(stale),
                "Regenerate the MCP runtime data bundle from a clean output directory before publishing or connecting clients.",
            )
        )


def _unexpected_runtime_vector_store_files(runtime_dir: Path) -> list[Path]:
    payload = _runtime_manifest_payload(runtime_dir)
    tenant_id = str(payload.get("tenant_id") or "").strip() if payload else ""
    if not tenant_id:
        return []
    expected_storage_key = tenant_storage_key(tenant_id)
    vector_dir = runtime_dir / "vector_db"
    if not vector_dir.is_dir():
        return []
    unexpected: list[Path] = []
    for path in sorted(vector_dir.rglob("*")):
        if not path.is_file() or path.name not in {"approved_vectors.jsonl", "bm25_index.json"}:
            continue
        try:
            relative_parts = path.relative_to(vector_dir).parts
        except ValueError:
            continue
        if not relative_parts or relative_parts[0] != expected_storage_key:
            unexpected.append(path)
    return unexpected


def _runtime_data_files_requiring_manifest(runtime_dir: Path) -> list[Path]:
    files: list[Path] = []
    repository_dir = runtime_dir / "repository"
    if repository_dir.is_dir():
        for path in sorted(repository_dir.glob("*.json")):
            if path.name in {"manifest.json", "approval_snapshot.json"} or any(
                path.name.endswith(suffix) for suffix in RUNTIME_REPOSITORY_RESULT_SUFFIXES
            ):
                files.append(path)
    vector_dir = runtime_dir / "vector_db"
    if vector_dir.is_dir():
        files.extend(
            path
            for path in sorted(vector_dir.rglob("*"))
            if path.is_file() and path.name in {"approved_vectors.jsonl", "bm25_index.json"}
        )
    return files


def _disallowed_runtime_repository_result_files(runtime_dir: Path) -> list[Path]:
    repository_dir = runtime_dir / "repository"
    if not repository_dir.is_dir():
        return []
    disallowed_suffixes = tuple(
        suffix for suffix in RUNTIME_REPOSITORY_RESULT_SUFFIXES if suffix != "_chunks.json"
    )
    return sorted(
        path
        for path in repository_dir.glob("*.json")
        if any(path.name.endswith(suffix) for suffix in disallowed_suffixes)
    )


def _runtime_manifest_payload(runtime_dir: Path) -> dict[str, object]:
    manifest_path = runtime_dir / "mcp_runtime_manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _runtime_manifest_document_ids(runtime_dir: Path) -> set[str]:
    payload = _runtime_manifest_payload(runtime_dir)
    if not payload:
        return set()
    values = payload.get("document_ids")
    if isinstance(values, list):
        return {str(value) for value in values if str(value).strip()}
    value = str(payload.get("document_id") or "").strip()
    return {value} if value else set()


def _repository_result_file_document_ids(runtime_dir: Path) -> set[str]:
    repository_dir = runtime_dir / "repository"
    document_ids: set[str] = set()
    if not repository_dir.is_dir():
        return document_ids
    for path in repository_dir.glob("*.json"):
        for suffix in RUNTIME_REPOSITORY_RESULT_SUFFIXES:
            if path.name.endswith(suffix):
                document_ids.add(path.name[: -len(suffix)])
                break
    return document_ids


def _repository_manifest_document_ids(runtime_dir: Path) -> set[str]:
    manifest_path = runtime_dir / "repository" / "manifest.json"
    if not manifest_path.is_file():
        return set()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return set()
    documents = payload.get("documents") if isinstance(payload, dict) else None
    if not isinstance(documents, dict):
        return set()
    return {str(document_id) for document_id in documents if str(document_id).strip()}


def _approval_snapshot_document_ids(runtime_dir: Path) -> set[str]:
    snapshot_path = runtime_dir / "repository" / "approval_snapshot.json"
    if not snapshot_path.is_file():
        return set()
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return set()
    if not isinstance(payload, dict):
        return set()
    raw_document_ids = payload.get("document_ids")
    document_ids = {
        str(value)
        for value in raw_document_ids
        if str(value).strip()
    } if isinstance(raw_document_ids, list) else set()
    entries = payload.get("entries")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            document_id = str(entry.get("document_id") or "").strip()
            if document_id:
                document_ids.add(document_id)
    return document_ids


def _vector_document_ids(runtime_dir: Path) -> set[str]:
    vector_dir = runtime_dir / "vector_db"
    document_ids: set[str] = set()
    if not vector_dir.is_dir():
        return document_ids
    for path in sorted(vector_dir.rglob("approved_vectors.jsonl")):
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        continue
                    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
                    document_id = str(record.get("document_id") or metadata.get("document_id") or "").strip()
                    if document_id:
                        document_ids.add(document_id)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
    return document_ids


def _find_smoke_artifacts(data_dir: Path) -> list[Path]:
    if not data_dir.exists():
        return []
    matches: list[Path] = []
    for path in data_dir.rglob("*"):
        if "doc_mcp_smoke" in path.name:
            matches.append(path)
            continue
        if path.is_file() and path.suffix.lower() in {".json", ".jsonl", ".csv"}:
            if _file_contains_ascii_marker(path, b"doc_mcp_smoke"):
                matches.append(path)
    return matches


def _file_contains_ascii_marker(path: Path, marker: bytes, *, chunk_size: int = 1024 * 1024) -> bool:
    overlap = max(len(marker) - 1, 0)
    tail = b""
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    return False
                window = tail + chunk
                if marker in window:
                    return True
                tail = window[-overlap:] if overlap else b""
    except OSError:
        return False


def _check_bundle_dir(
    bundle_dir: Path,
    findings: list[McpConnectionFinding],
    *,
    allow_local_only_bundle: bool,
) -> None:
    if not bundle_dir.exists():
        findings.append(
            McpConnectionFinding(
                "high",
                "bundle-dir-missing",
                f"MCP setup bundle directory does not exist: {bundle_dir}",
                "Generate the bundle with reg-rag-mcp-config --client-profile bundle --out-dir <dir>.",
            )
        )
        return
    if not bundle_dir.is_dir():
        findings.append(
            McpConnectionFinding(
                "high",
                "bundle-dir-not-directory",
                f"MCP setup bundle path is not a directory: {bundle_dir}",
                "Pass the generated bundle directory, not a single file.",
            )
        )
        return

    for filename in sorted(BUNDLE_REQUIRED_FILES):
        if not bundle_dir.joinpath(filename).is_file():
            findings.append(
                McpConnectionFinding(
                    "high",
                    "bundle-required-file-missing",
                    f"Generated MCP setup bundle is missing {filename}.",
                    "Regenerate the setup bundle with the current reg-rag-mcp-config command.",
                )
            )

    scanned_suffixes = {".ps1", ".md", ".json"}
    for path in sorted(bundle_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in scanned_suffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            findings.append(
                McpConnectionFinding(
                    "medium",
                    "bundle-file-read-failed",
                    f"Could not read generated bundle file {path.name}: {exc}",
                    "Check file permissions and regenerate the setup bundle if needed.",
                )
            )
            continue
        for pattern, code in BUNDLE_FORBIDDEN_PATTERNS.items():
            if pattern in text:
                findings.append(
                    McpConnectionFinding(
                        "high",
                        code,
                        f"Generated bundle file {path.name} contains a forbidden secret placeholder or assignment.",
                        "Regenerate the bundle with the current secretless templates and inject secrets only via environment variables.",
                    )
                )
        if path.suffix.lower() == ".json":
            _check_bundle_json_file(path, findings)

    connect_path = bundle_dir / "connect_mcp_client.ps1"
    if connect_path.is_file():
        connect_text = connect_path.read_text(encoding="utf-8", errors="replace")
        if "install_local_package.ps1" not in connect_text:
            findings.append(
                McpConnectionFinding(
                    "medium",
                    "bundle-connect-install-hint-missing",
                    "connect_mcp_client.ps1 does not mention install_local_package.ps1.",
                    "Regenerate the bundle so operators get a clear first-run install path.",
                )
            )
    _check_bundle_manifest_readiness(bundle_dir, findings, allow_local_only_bundle=allow_local_only_bundle)
    bundle_data_dir = bundle_dir / "data"
    if bundle_data_dir.is_dir():
        _check_runtime_data_document_consistency(bundle_data_dir, findings, require_manifest=True)


def _bundle_connection_summary(bundle_dir: Path) -> dict[str, object] | None:
    status_path = bundle_dir / "bundle_status.json"
    if not status_path.is_file():
        return None
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "status_path": str(status_path),
            "status_loaded": False,
        }
    connections = payload.get("connections")
    if not isinstance(connections, list):
        connections = []
    rows = [row for row in connections if isinstance(row, dict)]
    local_rows = [
        row
        for row in rows
        if str(row.get("mode") or "").strip().lower() in {"local_stdio", "stdio"}
    ]
    remote_rows = [row for row in rows if row not in local_rows]
    remote_ready_rows = [row for row in remote_rows if row.get("ready") is True]
    manual_setup_rows = [
        row
        for row in remote_rows
        if str(row.get("ready") or "").strip().lower() == "manual_setup_required"
    ]
    not_ready_rows = [row for row in remote_rows if row.get("ready") is False]
    return {
        "status_path": str(status_path),
        "status_loaded": True,
        "local_stdio_ready": bool(local_rows) and all(row.get("ready") is True for row in local_rows),
        "local_stdio_ready_count": sum(1 for row in local_rows if row.get("ready") is True),
        "local_stdio_count": len(local_rows),
        "remote_connector_ready": bool(remote_rows) and len(remote_ready_rows) == len(remote_rows),
        "remote_ready_count": len(remote_ready_rows),
        "remote_connector_count": len(remote_rows),
        "remote_not_ready_count": len(not_ready_rows),
        "remote_manual_setup_required_count": len(manual_setup_rows),
        "local_stdio_clients": [str(row.get("client") or "") for row in local_rows],
        "remote_not_ready_clients": [
            {
                "client": str(row.get("client") or ""),
                "mode": str(row.get("mode") or ""),
                "ready": row.get("ready"),
                "operator_action": str(row.get("operator_action") or ""),
            }
            for row in [*not_ready_rows, *manual_setup_rows]
        ],
    }


def _check_installed_client_configs(
    *,
    effective_data_dir: Path,
    server_name: str,
    codex_config: str | Path | None,
    claude_desktop_config: str | Path | None,
    tenant_storage_isolation: bool,
    findings: list[McpConnectionFinding],
) -> dict[str, object] | None:
    targets: list[tuple[str, Path, str]] = []
    if codex_config is not None:
        targets.append(("codex", Path(codex_config), "Codex"))
    if claude_desktop_config is not None:
        targets.append(("claude_desktop", Path(claude_desktop_config), "Claude Desktop"))
    if not targets:
        return None

    expected_storage_flag = _expected_storage_flag(effective_data_dir, tenant_storage_isolation)
    summary: dict[str, object] = {
        "server_name": server_name,
        "expected_data_dir": str(effective_data_dir),
        "expected_storage_flag": expected_storage_flag,
        "clients": {},
    }
    client_summaries: dict[str, object] = summary["clients"]  # type: ignore[assignment]
    for client_key, config_path, label in targets:
        client_summary = _check_installed_client_config(
            client_key=client_key,
            label=label,
            config_path=config_path,
            server_name=server_name,
            expected_data_dir=effective_data_dir,
            expected_storage_flag=expected_storage_flag,
            findings=findings,
        )
        client_summaries[client_key] = client_summary
    return summary


def _check_installed_client_config(
    *,
    client_key: str,
    label: str,
    config_path: Path,
    server_name: str,
    expected_data_dir: Path,
    expected_storage_flag: str,
    findings: list[McpConnectionFinding],
) -> dict[str, object]:
    summary: dict[str, object] = {
        "path": str(config_path),
        "exists": config_path.is_file(),
        "server_name": server_name,
        "status": "not_checked",
    }
    if not config_path.is_file():
        findings.append(
            McpConnectionFinding(
                "medium",
                "installed-client-config-missing",
                f"{label} config was requested but the file does not exist: {config_path}",
                "Install or merge the generated MCP client config, or pass the correct config path to reg-rag-mcp-doctor.",
            )
        )
        summary["status"] = "missing_config"
        return summary

    entry = _installed_client_server_entry(client_key, config_path, server_name, findings)
    if entry is None:
        summary["status"] = "missing_or_invalid_server"
        return summary
    if entry.get("url"):
        summary["status"] = "remote_url"
        summary["url"] = entry.get("url")
        return summary

    command = str(entry.get("command") or "")
    summary["command"] = command
    args = entry.get("args")
    if not isinstance(args, list) or not all(isinstance(value, str) for value in args):
        findings.append(
            McpConnectionFinding(
                "high",
                "installed-client-args-invalid",
                f"{label} server {server_name} must contain a string args list for local stdio MCP.",
                "Regenerate and merge the MCP client config instead of hand-editing command arguments.",
            )
        )
        summary["status"] = "invalid_args"
        return summary

    transport = (_arg_value(args, "--transport") or str(entry.get("type") or "stdio")).strip().lower()
    summary["transport"] = transport
    summary["args_checked"] = True
    if transport != "stdio":
        summary["status"] = "non_stdio_skipped"
        return summary

    _check_installed_stdio_launcher(
        label=label,
        server_name=server_name,
        command=command,
        args=args,
        expected_data_dir=expected_data_dir,
        summary=summary,
        findings=findings,
    )

    configured_data_dir = _arg_value(args, "--data-dir")
    summary["configured_data_dir"] = configured_data_dir
    if not configured_data_dir:
        findings.append(
            McpConnectionFinding(
                "high",
                "installed-client-data-dir-missing",
                f"{label} server {server_name} does not pass --data-dir, so it may read a default or stale runtime.",
                "Regenerate the client config and ensure --data-dir points to the generated bundle data directory.",
            )
        )
    elif not _same_filesystem_path(configured_data_dir, expected_data_dir):
        findings.append(
            McpConnectionFinding(
                "high",
                "installed-client-data-dir-mismatch",
                f"{label} server {server_name} points to {configured_data_dir}, expected {expected_data_dir}.",
                "Reinstall or merge the generated MCP client config so Codex/Claude reads the same data directory as the checked bundle.",
            )
        )

    if "--no-warm-cache" not in args:
        findings.append(
            McpConnectionFinding(
                "medium",
                "installed-client-missing-no-warm-cache",
                f"{label} server {server_name} is missing --no-warm-cache, which can make startup feel slow on large HWP/HWPX runtimes.",
                "Regenerate the stdio client config or add --no-warm-cache for fast client startup.",
            )
        )

    opposite_storage_flag = "--flat-storage" if expected_storage_flag == "--tenant-storage-isolation" else "--tenant-storage-isolation"
    if opposite_storage_flag in args:
        findings.append(
            McpConnectionFinding(
                "high",
                "installed-client-storage-flag-conflict",
                f"{label} server {server_name} uses {opposite_storage_flag}, expected {expected_storage_flag}.",
                "Regenerate the client config using the same tenant storage mode as the MCP runtime bundle.",
            )
        )
    elif expected_storage_flag not in args:
        findings.append(
            McpConnectionFinding(
                "medium",
                "installed-client-storage-flag-missing",
                f"{label} server {server_name} is missing {expected_storage_flag}; it may resolve the wrong runtime layout.",
                "Regenerate the client config or add the expected storage-mode flag.",
            )
        )

    summary["status"] = "checked"
    return summary


def _check_installed_stdio_launcher(
    *,
    label: str,
    server_name: str,
    command: str,
    args: Sequence[str],
    expected_data_dir: Path,
    summary: dict[str, object],
    findings: list[McpConnectionFinding],
) -> None:
    launcher_path = _stdio_launcher_path(args)
    if launcher_path is None:
        return
    summary["stdio_launcher_path"] = launcher_path
    expected_launcher = expected_data_dir.parent / "run_mcp_stdio_server.ps1"
    if Path(launcher_path).name.lower() != "run_mcp_stdio_server.ps1":
        findings.append(
            McpConnectionFinding(
                "high",
                "installed-client-stdio-launcher-invalid",
                f"{label} server {server_name} uses an unexpected stdio launcher path: {launcher_path}.",
                "Regenerate or reinstall the MCP bundle config so it points to run_mcp_stdio_server.ps1 in the current bundle.",
            )
        )
        return
    if not Path(_expand_filesystem_path(launcher_path)).is_file():
        findings.append(
            McpConnectionFinding(
                "high",
                "installed-client-stdio-launcher-missing",
                f"{label} server {server_name} points to a missing stdio launcher: {launcher_path}.",
                "Run connect_mcp_client.ps1 from the extracted bundle so the client config points to the current run_mcp_stdio_server.ps1.",
            )
        )
        return
    if not _same_filesystem_path(launcher_path, expected_launcher):
        findings.append(
            McpConnectionFinding(
                "high",
                "installed-client-stdio-launcher-mismatch",
                f"{label} server {server_name} launcher points to {launcher_path}, expected {expected_launcher}.",
                "Reinstall or merge the generated client config from the current bundle after moving or unzipping it.",
            )
        )


def _stdio_launcher_path(args: Sequence[str]) -> str | None:
    for index, value in enumerate(args[:-1]):
        if value.lower() == "-file":
            return args[index + 1]
    return None


def _installed_client_server_entry(
    client_key: str,
    config_path: Path,
    server_name: str,
    findings: list[McpConnectionFinding],
) -> dict[str, object] | None:
    label = "Codex" if client_key == "codex" else "Claude Desktop"
    try:
        if client_key == "codex":
            payload = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
            servers = payload.get("mcp_servers") if isinstance(payload, dict) else None
        else:
            payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
            servers = payload.get("mcpServers") if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        findings.append(
            McpConnectionFinding(
                "high",
                "installed-client-config-invalid",
                f"{label} config is not readable or valid: {config_path}: {exc}",
                "Fix the installed client config syntax, then rerun reg-rag-mcp-doctor.",
            )
        )
        return None
    if not isinstance(servers, dict):
        findings.append(
            McpConnectionFinding(
                "high",
                "installed-client-server-container-missing",
                f"{label} config does not contain the MCP server container for {server_name}.",
                "Merge the generated MCP client config into the installed client config.",
            )
        )
        return None
    entry = servers.get(server_name)
    if not isinstance(entry, dict):
        findings.append(
            McpConnectionFinding(
                "high",
                "installed-client-server-missing",
                f"{label} config does not contain MCP server {server_name}.",
                "Install or merge the generated MCP client config for this server name.",
            )
        )
        return None
    return entry


def _expected_storage_flag(data_dir: Path, tenant_storage_isolation: bool) -> str:
    payload = _runtime_manifest_payload(data_dir)
    if "tenant_storage_isolation" in payload:
        return "--tenant-storage-isolation" if bool(payload.get("tenant_storage_isolation")) else "--flat-storage"
    return "--tenant-storage-isolation" if tenant_storage_isolation else "--flat-storage"


def _arg_value(args: Sequence[str], flag: str) -> str | None:
    for index, value in enumerate(args):
        if value == flag and index + 1 < len(args):
            return args[index + 1]
    return None


def _same_filesystem_path(left: str | Path, right: str | Path) -> bool:
    return _normalized_filesystem_path(left) == _normalized_filesystem_path(right)


def _normalized_filesystem_path(value: str | Path) -> str:
    return os.path.normcase(os.path.abspath(_expand_filesystem_path(value)))


def _expand_filesystem_path(value: str | Path) -> str:
    return os.path.expandvars(os.path.expanduser(str(value)))


def _check_bundle_json_file(path: Path, findings: list[McpConnectionFinding]) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        findings.append(
            McpConnectionFinding(
                "high",
                "bundle-json-invalid",
                f"Generated bundle JSON file {path.name} is not valid JSON: {exc}",
                "Regenerate the setup bundle, then merge only the mcpServers object into Claude Desktop config.",
            )
        )
        return
    if path.name == "claude_desktop_config.json":
        _check_claude_desktop_config_payload(payload, path.name, findings)


def _check_claude_desktop_config_payload(payload: object, filename: str, findings: list[McpConnectionFinding]) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("mcpServers"), dict) or not payload["mcpServers"]:
        findings.append(
            McpConnectionFinding(
                "high",
                "bundle-claude-desktop-config-invalid",
                f"{filename} must contain a non-empty top-level mcpServers object.",
                "Regenerate with reg-rag-mcp-config --client-profile bundle or claude-desktop.",
            )
        )
        return
    for server_name, server in payload["mcpServers"].items():
        if not isinstance(server_name, str) or not isinstance(server, dict):
            findings.append(
                McpConnectionFinding(
                    "high",
                    "bundle-claude-desktop-config-invalid",
                    f"{filename} contains an invalid mcpServers entry.",
                    "Each mcpServers entry must be an object keyed by server name.",
                )
            )
            continue
        has_stdio = server.get("command") and isinstance(server.get("args"), list)
        has_http = server.get("url")
        if not (has_stdio or has_http):
            findings.append(
                McpConnectionFinding(
                    "high",
                    "bundle-claude-desktop-config-invalid",
                    f"{filename} entry {server_name} has neither stdio command/args nor URL.",
                    "Regenerate the Claude Desktop config and avoid hand-editing required fields.",
                )
            )


def _check_bundle_manifest_readiness(
    bundle_dir: Path,
    findings: list[McpConnectionFinding],
    *,
    allow_local_only_bundle: bool,
) -> None:
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        findings.append(
            McpConnectionFinding(
                "high",
                "bundle-manifest-invalid",
                f"Generated MCP setup bundle manifest is not valid JSON: {exc}",
                "Regenerate the setup bundle with the current reg-rag-mcp-config command.",
            )
        )
        return
    ready = manifest.get("ready") if isinstance(manifest, dict) else None
    if not isinstance(ready, dict):
        findings.append(
            McpConnectionFinding(
                "high",
                "bundle-manifest-ready-missing",
                "Generated MCP setup bundle manifest does not contain ready flags.",
                "Regenerate the setup bundle so ChatGPT and Claude API readiness is explicit.",
            )
        )
        return
    readiness_checks = (
        (("chatgpt_remote", "chatgpt"), "ChatGPT remote"),
        (("claude_api",), "Claude API"),
    )
    for keys, label in readiness_checks:
        if not any(ready.get(key) is True for key in keys):
            if allow_local_only_bundle:
                continue
            findings.append(
                McpConnectionFinding(
                    "high",
                    "bundle-remote-profile-not-ready",
                    f"Generated MCP setup bundle marks {label} remote profile as not ready.",
                    "Regenerate with --public-url https://.../mcp for remote clients, or use local Claude Desktop/Claude Code stdio only.",
                )
            )


def _normalize_mcp_url(public_url: str | None) -> str | None:
    if not public_url:
        return None
    cleaned = public_url.strip().rstrip("/")
    if not cleaned:
        return None
    return cleaned if cleaned.endswith("/mcp") else f"{cleaned}/mcp"


def _is_placeholder_secret(value: str | None) -> bool:
    return (value or "").strip() in PLACEHOLDER_VALUES


def _is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1", "[::1]"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check MCP client connection readiness without making network calls.")
    parser.add_argument(
        "--client-profile",
        choices=sorted(VALID_CLIENTS),
        default="bundle",
        help="Target MCP client profile to check.",
    )
    parser.add_argument(
        "--connection-mode",
        choices=sorted(VALID_CONNECTION_MODES),
        default="direct",
        help="Use direct for HTTPS/stdio client setup or openai-tunnel for OpenAI Secure MCP Tunnel setup.",
    )
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--public-url", default=None)
    parser.add_argument("--token-env", default="MCP_AUTH_TOKEN")
    parser.add_argument("--tunnel-id-env", default="OPENAI_TUNNEL_ID")
    parser.add_argument("--control-plane-api-key-env", default="CONTROL_PLANE_API_KEY")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--bundle-dir", default=None, help="Generated MCP setup bundle directory to validate.")
    parser.add_argument("--server-name", default="regulation_mcp", help="MCP server name to validate in installed client configs.")
    parser.add_argument("--codex-config", default=None, help="Optional installed Codex config.toml to validate against the checked bundle.")
    parser.add_argument(
        "--claude-desktop-config",
        default=None,
        help="Optional installed Claude Desktop claude_desktop_config.json to validate against the checked bundle.",
    )
    parser.add_argument(
        "--allow-local-only-bundle",
        action="store_true",
        help="Do not fail bundle readiness when only local Claude Desktop/Claude Code stdio profiles are intended.",
    )
    parser.add_argument("--skip-cli-check", action="store_true")
    parser.add_argument("--skip-data-check", action="store_true")
    parser.add_argument(
        "--require-full-index",
        action="store_true",
        help="Fail if repository chunk counts do not match approved vector record counts.",
    )
    parser.add_argument(
        "--audit-index-visibility",
        action="store_true",
        help=(
            "Run the tenant-aware MCP-visible record audit; integrity flags also enable it automatically "
            "when --tenant-id is supplied."
        ),
    )
    parser.add_argument("--tenant-id", default=None, help="Tenant ID used by the MCP server for index visibility audit.")
    parser.add_argument(
        "--tenant-storage-isolation",
        action="store_true",
        help="Use tenant-isolated runtime layout for index visibility audit.",
    )
    parser.add_argument(
        "--min-visible-records",
        type=int,
        default=1,
        help="Minimum tenant-visible vector records required when --audit-index-visibility is enabled.",
    )
    parser.add_argument(
        "--require-indexed",
        action="store_true",
        help="Fail index visibility audit if any document is not fully indexed; auto-enables it with --tenant-id.",
    )
    parser.add_argument(
        "--forbid-smoke-docs",
        action="store_true",
        help="Fail index visibility audit if smoke-test-like documents are visible; auto-enables it with --tenant-id.",
    )
    parser.add_argument("--probe-public-url", action="store_true", help="Make a live HTTPS probe to the normalized public /mcp URL.")
    parser.add_argument("--probe-timeout-seconds", type=float, default=5.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--fail-on-warning", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    args = build_parser().parse_args(argv)
    report = check_mcp_connection_readiness(
        client_profile=args.client_profile,
        connection_mode=args.connection_mode,
        transport=args.transport,
        host=args.host,
        public_url=args.public_url,
        token_env=args.token_env,
        tunnel_id_env=args.tunnel_id_env,
        control_plane_api_key_env=args.control_plane_api_key_env,
        data_dir=args.data_dir,
        bundle_dir=args.bundle_dir,
        server_name=args.server_name,
        codex_config=args.codex_config,
        claude_desktop_config=args.claude_desktop_config,
        check_cli=not args.skip_cli_check,
        check_data=not args.skip_data_check,
        require_full_index=args.require_full_index,
        audit_index_visibility=args.audit_index_visibility,
        tenant_id=args.tenant_id,
        tenant_storage_isolation=args.tenant_storage_isolation,
        min_visible_records=args.min_visible_records,
        require_indexed=args.require_indexed,
        forbid_smoke_docs=args.forbid_smoke_docs,
        probe_public_url=args.probe_public_url,
        probe_timeout_seconds=args.probe_timeout_seconds,
        allow_local_only_bundle=args.allow_local_only_bundle,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out_json:
        out_path = Path(args.out_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload + "\n", encoding="utf-8")
    if args.json:
        stdout.write(payload + "\n")
    elif report["findings"]:
        for finding in report["findings"]:
            stdout.write(
                f"{finding['severity']} {finding['code']}: {finding['detail']} "
                f"Remediation: {finding['remediation']}\n"
            )
    else:
        stdout.write("MCP connection readiness passed\n")
    if int(report["high_count"]) > 0:
        return 1
    if args.fail_on_warning and int(report["medium_count"]) > 0:
        return 1
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
