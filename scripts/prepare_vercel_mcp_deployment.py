from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Leave room below Vercel's standard 500 MB uncompressed Python Function
# bundle limit for application code and installed dependencies.
DEFAULT_MAX_RUNTIME_BYTES = 400 * 1024 * 1024
FORBIDDEN_RUNTIME_NAMES = {
    "uploads",
    "exports",
    "pending_uploads",
    "operator_projects",
}
FORBIDDEN_RUNTIME_FILENAMES = {
    ".api_audit.lock",
    ".write.lock",
    "api_audit.jsonl",
    "rag_feedback.jsonl",
    "rag_traces.jsonl",
}
FORBIDDEN_RUNTIME_SUFFIXES = (
    "_nodes.json",
    "_issues.json",
    "_quality.json",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare a source-only Vercel deployment directory for an approved HTTPS MCP runtime."
    )
    parser.add_argument("--runtime-data-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--max-runtime-bytes",
        type=int,
        default=DEFAULT_MAX_RUNTIME_BYTES,
        help="Reject runtime payloads larger than this many bytes (default: 400 MiB).",
    )
    return parser.parse_args()


def prepare_vercel_mcp_deployment(
    *,
    runtime_data_dir: Path,
    out_dir: Path,
    max_runtime_bytes: int = DEFAULT_MAX_RUNTIME_BYTES,
) -> dict[str, Any]:
    source_runtime = runtime_data_dir.resolve()
    target = out_dir.resolve()
    _validate_target(source_runtime, target)
    manifest = _load_runtime_manifest(source_runtime)
    runtime_files = _runtime_files(source_runtime)
    _validate_runtime_files(source_runtime, runtime_files)
    runtime_bytes = sum(path.stat().st_size for path in runtime_files)
    if runtime_bytes > max_runtime_bytes:
        raise ValueError(
            f"Approved runtime is too large for the configured Vercel deployment limit: "
            f"{runtime_bytes} > {max_runtime_bytes} bytes."
        )

    target.mkdir(parents=True)
    shutil.copytree(
        PROJECT_ROOT / "app",
        target / "app",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    (target / "vercel_mcp.py").write_text(_vercel_entrypoint(), encoding="utf-8")
    shutil.copytree(source_runtime, target / "mcp_runtime")
    (target / "pyproject.toml").write_text(_deployment_pyproject(), encoding="utf-8")
    (target / "vercel.json").write_text(
        json.dumps(_vercel_config(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (target / ".env.example").write_text(_environment_example(manifest), encoding="utf-8")
    (target / "DEPLOY.md").write_text(_deployment_guide(manifest), encoding="utf-8")

    report = {
        "report_type": "vercel_mcp_deployment_stage",
        "out_dir": str(target),
        "runtime_file_count": len(runtime_files),
        "runtime_bytes": runtime_bytes,
        "tenant_id": manifest.get("tenant_id"),
        "profile_id": manifest.get("profile_id"),
        "tool_profile": "chatgpt-data",
        "mcp_path": "/mcp",
        "stateless_http": True,
        "json_response": True,
        "local_trace_writes": False,
        "local_api_audit_writes": False,
        "deploy_command": f'vercel --cwd "{target}"',
        "production_deploy_command": f'vercel --prod --cwd "{target}"',
    }
    (target / "deployment_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def _validate_target(source_runtime: Path, target: Path) -> None:
    if not source_runtime.is_dir():
        raise ValueError(f"Approved runtime data directory does not exist: {source_runtime}")
    if target.exists():
        raise ValueError(f"Output directory already exists; choose a new empty path: {target}")
    if target == PROJECT_ROOT or PROJECT_ROOT in target.parents and target.name in {"app", "data", "tests"}:
        raise ValueError(f"Refusing unsafe Vercel staging target: {target}")
    if target == source_runtime or source_runtime in target.parents:
        raise ValueError("Output directory must not be inside the approved runtime data directory.")


def _load_runtime_manifest(runtime_data_dir: Path) -> dict[str, Any]:
    manifest_path = runtime_data_dir / "mcp_runtime_manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Approved MCP runtime manifest is missing: {manifest_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Approved MCP runtime manifest is unreadable: {manifest_path}") from exc
    if not isinstance(payload, dict) or not str(payload.get("tenant_id") or "").strip():
        raise ValueError("mcp_runtime_manifest.json must be an object with a tenant_id.")
    return payload


def _runtime_files(runtime_data_dir: Path) -> list[Path]:
    return sorted(path for path in runtime_data_dir.rglob("*") if path.is_file())


def _validate_runtime_files(runtime_data_dir: Path, runtime_files: list[Path]) -> None:
    if not runtime_files:
        raise ValueError("Approved MCP runtime is empty.")
    violations: list[str] = []
    for path in runtime_files:
        relative = path.relative_to(runtime_data_dir)
        casefolded_name = path.name.casefold()
        if (
            casefolded_name in FORBIDDEN_RUNTIME_FILENAMES
            or any(part.casefold() in FORBIDDEN_RUNTIME_NAMES for part in relative.parts)
        ):
            violations.append(relative.as_posix())
            continue
        if casefolded_name.endswith(FORBIDDEN_RUNTIME_SUFFIXES):
            violations.append(relative.as_posix())
    if violations:
        samples = ", ".join(violations[:5])
        raise ValueError(
            "Runtime contains raw or generated preprocessing data that must not be deployed: "
            f"{samples}"
        )


def _deployment_pyproject() -> str:
    return """[project]
name = "public-regulation-vercel-mcp"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.111",
  "uvicorn>=0.30",
  "pydantic>=2.0",
  "mcp>=1.26,<2",
  "kiwipiepy>=0.21",
]

[tool.vercel]
entrypoint = "vercel_mcp:app"
"""


def _vercel_entrypoint() -> str:
    return """from __future__ import annotations

import os

from app.mcp_server.vercel_app import create_vercel_mcp_app


app = create_vercel_mcp_app(os.environ)
"""


def _vercel_config() -> dict[str, Any]:
    return {
        "$schema": "https://openapi.vercel.sh/vercel.json",
        "functions": {
            "vercel_mcp.py": {
                "maxDuration": 300,
                "includeFiles": "mcp_runtime/**",
                "excludeFiles": "{**/__pycache__/**,**/*.pyc,**/*.pyo}",
            }
        },
    }


def _environment_example(manifest: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"MCP_TENANT_ID={manifest.get('tenant_id') or ''}",
            f"MCP_PROFILE_ID={manifest.get('profile_id') or ''}",
            "MCP_AUTH_TOKEN=",
            "MCP_AUTH_ISSUER_URL=",
            "MCP_ALLOWED_HTTP_HOSTS=",
            "MCP_ALLOWED_HTTP_ORIGINS=",
            "MCP_ALLOW_UNAUTHENTICATED_HTTP=false",
            "MCP_TOOL_PROFILE=chatgpt-data",
            "MCP_WARM_CACHE=false",
            "",
        ]
    )


def _deployment_guide(manifest: dict[str, Any]) -> str:
    return f"""# Vercel HTTPS MCP deployment

This directory contains source code plus one approved, read-only MCP runtime.

1. Choose one explicit access mode:
   - approved public read-only endpoint: set `MCP_ALLOW_UNAUTHENTICATED_HTTP=true`
     and leave `MCP_AUTH_TOKEN` empty;
   - a generic client that accepts a static bearer: set `MCP_AUTH_TOKEN` as a
     Vercel secret;
   - private ChatGPT access: place an OAuth 2.1 authorization server/gateway in
     front of this origin. A static bearer cannot be entered in ChatGPT.
2. If using a custom domain, set `MCP_AUTH_ISSUER_URL=https://your-domain` and
   `MCP_ALLOWED_HTTP_HOSTS=your-domain`.
3. Run `vercel`, test the preview endpoint at `https://<preview-host>/mcp`, then run
   `vercel --prod`.
4. Use Streamable HTTP and the same final `https://<production-host>/mcp` URL in
   both ChatGPT and Claude connectors.

Runtime binding:

- tenant_id: `{manifest.get("tenant_id") or ""}`
- profile_id: `{manifest.get("profile_id") or ""}`
- tools: `search`, `fetch`

The Vercel adapter is stateless and disables local trace/audit file writes because the
function bundle is read-only. Use Vercel logs or an approved external audit sink for
durable production auditing.
"""


def main() -> int:
    args = parse_args()
    report = prepare_vercel_mcp_deployment(
        runtime_data_dir=args.runtime_data_dir,
        out_dir=args.out_dir,
        max_runtime_bytes=args.max_runtime_bytes,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
