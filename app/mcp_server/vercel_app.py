from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from starlette.applications import Starlette

from app.mcp_server.regulation_server import create_regulation_mcp_server


DEFAULT_VERCEL_DATA_DIR = "mcp_runtime"
DEFAULT_VERCEL_MCP_PATH = "/mcp"


def create_vercel_mcp_app(environ: Mapping[str, str]) -> Starlette:
    """Create a stateless, read-only Streamable HTTP app for Vercel."""
    data_dir = Path(environ.get("MCP_DATA_DIR", DEFAULT_VERCEL_DATA_DIR))
    manifest = _load_runtime_manifest(data_dir)
    tenant_id = _bound_runtime_value(
        environ=environ,
        environment_name="MCP_TENANT_ID",
        manifest=manifest,
        manifest_name="tenant_id",
        required=True,
    )
    profile_id = _bound_runtime_value(
        environ=environ,
        environment_name="MCP_PROFILE_ID",
        manifest=manifest,
        manifest_name="profile_id",
        required=False,
    )

    bearer_token = str(environ.get("MCP_AUTH_TOKEN") or "").strip() or None
    allow_unauthenticated = _env_bool(environ, "MCP_ALLOW_UNAUTHENTICATED_HTTP")
    tool_profile = str(environ.get("MCP_TOOL_PROFILE") or "chatgpt-data").strip().lower()
    if not bearer_token and not allow_unauthenticated:
        raise RuntimeError(
            "Vercel MCP refuses unauthenticated startup. Set MCP_AUTH_TOKEN or explicitly set "
            "MCP_ALLOW_UNAUTHENTICATED_HTTP=true for an approved public read-only deployment."
        )
    if not bearer_token and tool_profile != "chatgpt-data":
        raise RuntimeError(
            "Unauthenticated Vercel MCP deployments must use MCP_TOOL_PROFILE=chatgpt-data."
        )

    issuer_url = str(environ.get("MCP_AUTH_ISSUER_URL") or "").strip() or None
    if bearer_token and not issuer_url:
        public_host = _first_nonempty(
            environ.get("VERCEL_PROJECT_PRODUCTION_URL"),
            environ.get("VERCEL_URL"),
        )
        if not public_host:
            raise RuntimeError(
                "MCP_AUTH_ISSUER_URL is required with MCP_AUTH_TOKEN outside a Vercel deployment."
            )
        issuer_url = f"https://{public_host}"

    allowed_hosts = set(_csv_values(environ.get("MCP_ALLOWED_HTTP_HOSTS")))
    for value in (
        environ.get("VERCEL_PROJECT_PRODUCTION_URL"),
        environ.get("VERCEL_URL"),
    ):
        host = _host_only(value)
        if host:
            allowed_hosts.add(host)
    issuer_host = _host_only(issuer_url)
    if issuer_host:
        allowed_hosts.add(issuer_host)
    if not allowed_hosts:
        raise RuntimeError(
            "No public HTTP host is configured. Set MCP_ALLOWED_HTTP_HOSTS or a Vercel URL environment variable."
        )

    server = create_regulation_mcp_server(
        data_dir=data_dir,
        tenant_id=tenant_id,
        profile_id=profile_id,
        tenant_storage_isolation=manifest.get("tenant_storage_isolation"),
        host="0.0.0.0",
        port=8000,
        http_bearer_token=bearer_token,
        auth_issuer_url=issuer_url,
        allowed_http_hosts=sorted(allowed_hosts),
        allowed_http_origins=_csv_values(environ.get("MCP_ALLOWED_HTTP_ORIGINS")),
        tool_profile=tool_profile,
        warm_cache=_env_bool(environ, "MCP_WARM_CACHE"),
        streamable_http_path=str(
            environ.get("MCP_STREAMABLE_HTTP_PATH") or DEFAULT_VERCEL_MCP_PATH
        ).strip(),
        stateless_http=True,
        json_response=True,
        api_audit_enabled=False,
        rag_trace_enabled=False,
        background_tokenizer_warmup=False,
    )
    server._reg_rag_scope["deployment_runtime"] = "vercel"
    server._reg_rag_scope["read_only_runtime"] = True
    return server.streamable_http_app()


def _load_runtime_manifest(data_dir: Path) -> dict[str, Any]:
    manifest_path = data_dir / "mcp_runtime_manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Approved MCP runtime manifest is missing: {manifest_path}. "
            "Package an approved runtime instead of the repository data directory."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Approved MCP runtime manifest is unreadable: {manifest_path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Approved MCP runtime manifest must be a JSON object: {manifest_path}")
    if not payload.get("tenant_id"):
        raise RuntimeError(f"Approved MCP runtime manifest has no tenant_id: {manifest_path}")
    return payload


def _bound_runtime_value(
    *,
    environ: Mapping[str, str],
    environment_name: str,
    manifest: Mapping[str, Any],
    manifest_name: str,
    required: bool,
) -> str | None:
    configured = str(environ.get(environment_name) or "").strip() or None
    recorded = str(manifest.get(manifest_name) or "").strip() or None
    if configured and recorded and configured.casefold() != recorded.casefold():
        raise RuntimeError(
            f"{environment_name} does not match mcp_runtime_manifest.json {manifest_name}."
        )
    value = configured or recorded
    if required and not value:
        raise RuntimeError(f"{environment_name} or manifest {manifest_name} is required.")
    return value


def _env_bool(environ: Mapping[str, str], name: str) -> bool:
    return str(environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _csv_values(value: str | None) -> list[str]:
    return [item.strip().rstrip("/") for item in str(value or "").split(",") if item.strip()]


def _host_only(value: str | None) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"https://{text}")
    return str(parsed.netloc or parsed.path).strip()


def _first_nonempty(*values: str | None) -> str:
    return next((str(value).strip().rstrip("/") for value in values if str(value or "").strip()), "")
