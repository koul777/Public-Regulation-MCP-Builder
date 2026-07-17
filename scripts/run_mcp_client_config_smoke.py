from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import tomllib
from typing import Any, Sequence, TextIO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_metadata import current_repo_commit


DEFAULT_SEARCH_QUERY = "\uc81c1\uc870"


def run_mcp_client_config_smoke(
    *,
    server_name: str = "regulation_mcp",
    codex_config: str | Path | None = None,
    claude_desktop_config: str | Path | None = None,
    query: str | None = None,
    out_json: str | Path | None = None,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    targets: list[tuple[str, Path]] = []
    if codex_config is not None:
        targets.append(("codex", Path(codex_config)))
    if claude_desktop_config is not None:
        targets.append(("claude_desktop", Path(claude_desktop_config)))

    results: list[dict[str, Any]] = []
    for client_key, config_path in targets:
        results.append(
            _run_single_client_config_smoke(
                client_key=client_key,
                config_path=config_path,
                server_name=server_name,
                query=query,
                timeout_seconds=timeout_seconds,
            )
        )

    report = {
        "report_type": "mcp_client_config_smoke",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "server_name": server_name,
        "passed": bool(results) and all(bool(result.get("passed")) for result in results),
        "results": results,
    }
    if not results:
        report["error"] = "At least one of --codex-config or --claude-desktop-config is required."

    if out_json is not None:
        out_path = Path(out_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _run_single_client_config_smoke(
    *,
    client_key: str,
    config_path: Path,
    server_name: str,
    query: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    label = "Codex" if client_key == "codex" else "Claude Desktop"
    try:
        entry = _read_client_server_entry(client_key=client_key, config_path=config_path, server_name=server_name)
        command = str(entry.get("command") or "")
        args = entry.get("args")
        if not command or not isinstance(args, list) or not all(isinstance(value, str) for value in args):
            raise ValueError(f"{label} server {server_name} must contain command and string args for local stdio.")
        smoke_query = query or _recommended_query_from_args(args) or DEFAULT_SEARCH_QUERY
        result = asyncio.run(
            asyncio.wait_for(
                _run_client_entry(command=command, args=list(args), query=smoke_query),
                timeout=timeout_seconds,
            )
        )
        result.update(
            {
                "label": client_key,
                "config_path": str(config_path),
                "command": command,
                "args": list(args),
                "query": smoke_query,
            }
        )
        return result
    except Exception as exc:
        return {
            "label": client_key,
            "config_path": str(config_path),
            "passed": False,
            "error": str(exc),
        }


def _read_client_server_entry(*, client_key: str, config_path: Path, server_name: str) -> dict[str, Any]:
    if client_key == "codex":
        payload = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
        servers = payload.get("mcp_servers") if isinstance(payload, dict) else None
    elif client_key == "claude_desktop":
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
        servers = payload.get("mcpServers") if isinstance(payload, dict) else None
    else:
        raise ValueError(f"Unsupported client key: {client_key}")
    if not isinstance(servers, dict):
        raise ValueError(f"{config_path} does not contain an MCP server container.")
    entry = servers.get(server_name)
    if not isinstance(entry, dict):
        raise ValueError(f"{config_path} does not contain MCP server {server_name}.")
    if entry.get("url"):
        raise ValueError(f"{config_path} server {server_name} is a remote URL entry, not local stdio.")
    return entry


def _recommended_query_from_args(args: Sequence[str]) -> str | None:
    data_dir = _arg_value(args, "--data-dir")
    if not data_dir:
        return None
    manifest_path = Path(data_dir).expanduser() / "mcp_runtime_manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    value = str(payload.get("recommended_smoke_query") or "").strip() if isinstance(payload, dict) else ""
    return value or None


def _arg_value(args: Sequence[str], flag: str) -> str | None:
    for index, value in enumerate(args[:-1]):
        if value == flag:
            return args[index + 1]
    return None


async def _run_client_entry(*, command: str, args: list[str], query: str) -> dict[str, Any]:
    started_at = time.perf_counter()
    params = StdioServerParameters(command=command, args=args)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            list_tools_started_at = time.perf_counter()
            tool_result = await session.list_tools()
            list_tools_elapsed_ms = _elapsed_ms(list_tools_started_at)
            tool_names = sorted(tool.name for tool in tool_result.tools)

            search_started_at = time.perf_counter()
            search = await session.call_tool(
                "search",
                {
                    "query": query,
                    "top_k": 3,
                    "security_levels": ["internal"],
                },
            )
            search_elapsed_ms = _elapsed_ms(search_started_at)
            search_payload = _tool_payload(search)
            results = search_payload.get("results") if isinstance(search_payload.get("results"), list) else []

            fetch_payload: dict[str, Any] = {}
            fetch_elapsed_ms = 0.0
            first_id = str((results[0] if results else {}).get("id") or "")
            if first_id:
                fetch_started_at = time.perf_counter()
                fetch = await session.call_tool(
                    "fetch",
                    {
                        "id": first_id,
                        "security_levels": ["internal"],
                    },
                )
                fetch_elapsed_ms = _elapsed_ms(fetch_started_at)
                fetch_payload = _tool_payload(fetch)

            return {
                "passed": bool({"search", "fetch"}.issubset(set(tool_names)) and results and fetch_payload.get("text")),
                "tool_names": tool_names,
                "search_result_count": len(results),
                "fetch_has_text": bool(fetch_payload.get("text")),
                "first_id": first_id,
                "first_result_metadata": (results[0] if results else {}).get("metadata") or {},
                "list_tools_elapsed_ms": list_tools_elapsed_ms,
                "search_elapsed_ms": search_elapsed_ms,
                "fetch_elapsed_ms": fetch_elapsed_ms,
                "total_elapsed_ms": _elapsed_ms(started_at),
            }


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 3)


def _tool_payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    content = getattr(result, "content", None)
    if isinstance(content, list) and content:
        text = getattr(content[0], "text", "")
        if text:
            decoded = json.loads(text)
            if isinstance(decoded, dict):
                return decoded
    return {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run MCP stdio smoke through generated client config files.")
    parser.add_argument("--server-name", default="regulation_mcp")
    parser.add_argument("--codex-config", default=None)
    parser.add_argument("--claude-desktop-config", default=None)
    parser.add_argument("--query", default=None)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    if stdout is sys.stdout and hasattr(stdout, "reconfigure"):
        stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    report = run_mcp_client_config_smoke(
        server_name=args.server_name,
        codex_config=args.codex_config,
        claude_desktop_config=args.claude_desktop_config,
        query=args.query,
        out_json=args.out_json,
        timeout_seconds=args.timeout_seconds,
    )
    stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if args.fail_on_issue and not report["passed"]:
        return 2
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
