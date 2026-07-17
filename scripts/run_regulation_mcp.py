from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.mcp_server.regulation_server import create_regulation_mcp_server


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local approved-regulation MCP server.")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--profile-id", default=None, help="Default institution profile scope for MCP tools.")
    parser.add_argument("--actor", default="mcp-regulation-server")
    parser.add_argument("--role", default="operator")
    parser.add_argument("--department-id", action="append", default=[])
    parser.add_argument("--tenant-storage-isolation", action="store_true")
    parser.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--tool-profile",
        choices=["full", "chatgpt-data"],
        default="full",
        help="Tool exposure profile. Use chatgpt-data to expose only search/fetch for data connectors.",
    )
    parser.add_argument(
        "--http-bearer-token",
        default=None,
        help="Bearer token required for streamable-http or sse transports. Prefer --http-bearer-token-env.",
    )
    parser.add_argument(
        "--http-bearer-token-env",
        default=None,
        help="Environment variable containing the bearer token for streamable-http or sse transports.",
    )
    parser.add_argument(
        "--auth-issuer-url",
        default=None,
        help="Issuer URL advertised by the MCP auth metadata when bearer auth is enabled.",
    )
    parser.add_argument(
        "--allow-unauthenticated-http",
        action="store_true",
        help="Allow non-loopback HTTP/SSE startup without bearer auth. Use only behind approved network controls.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default="stdio",
        help="MCP transport. Use stdio for local desktop clients and streamable-http for internal HTTP clients.",
    )
    parser.add_argument(
        "--no-warm-cache",
        action="store_true",
        help="Skip startup cache warmup. By default the server preloads approved vectors, approval snapshots, and BM25.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    http_bearer_token = _resolve_http_bearer_token(args)
    _validate_http_auth_posture(args, http_bearer_token)
    _validate_storage_posture(args)
    tenant_storage_isolation = None
    if args.tenant_storage_isolation:
        tenant_storage_isolation = True
    if args.flat_storage:
        tenant_storage_isolation = False
    server = create_regulation_mcp_server(
        data_dir=args.data_dir,
        tenant_id=args.tenant_id,
        profile_id=args.profile_id,
        actor=args.actor,
        role=args.role,
        department_ids=args.department_id,
        tenant_storage_isolation=tenant_storage_isolation,
        host=args.host,
        port=args.port,
        http_bearer_token=http_bearer_token,
        auth_issuer_url=args.auth_issuer_url,
        tool_profile=args.tool_profile,
        warm_cache=not args.no_warm_cache,
    )
    server.run(transport=args.transport)
    return 0


def _resolve_http_bearer_token(args: argparse.Namespace) -> str | None:
    if args.http_bearer_token and args.http_bearer_token_env:
        raise SystemExit("--http-bearer-token and --http-bearer-token-env are mutually exclusive.")
    if args.http_bearer_token_env:
        token = os.getenv(args.http_bearer_token_env)
        if not token:
            raise SystemExit(f"Environment variable is not set or empty: {args.http_bearer_token_env}")
        return token
    return args.http_bearer_token


def _validate_http_auth_posture(args: argparse.Namespace, http_bearer_token: str | None) -> None:
    if args.transport not in {"streamable-http", "sse"}:
        return
    if _is_loopback_host(args.host) or http_bearer_token or args.allow_unauthenticated_http:
        return
    raise SystemExit(
        "Refusing to start unauthenticated MCP HTTP/SSE on a non-loopback host. "
        "Set --http-bearer-token-env MCP_AUTH_TOKEN or pass --allow-unauthenticated-http only behind approved controls."
    )


def _validate_storage_posture(args: argparse.Namespace) -> None:
    protected_env = os.getenv("APP_ENV", "local").lower() not in {"local", "dev", "development", "test"}
    if protected_env and args.flat_storage:
        raise SystemExit(
            "Refusing to start MCP with --flat-storage in a protected environment. "
            "Use --tenant-storage-isolation or run only in approved local/dev contexts."
        )


def _is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1", "[::1]"}


if __name__ == "__main__":
    raise SystemExit(main())
