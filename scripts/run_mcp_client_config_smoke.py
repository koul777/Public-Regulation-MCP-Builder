from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import os
import json
from pathlib import Path
import sys
import time
import tomllib
from typing import Any, Sequence, TextIO

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


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
    plugin_mcp_config: str | Path | None = None,
    remote_url: str | None = None,
    remote_token_env: str | None = None,
    query: str | None = None,
    out_json: str | Path | None = None,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    targets: list[tuple[str, Path]] = []
    if codex_config is not None:
        targets.append(("codex", Path(codex_config)))
    if claude_desktop_config is not None:
        targets.append(("claude_desktop", Path(claude_desktop_config)))
    if plugin_mcp_config is not None:
        targets.append(("chatgpt_desktop_local", Path(plugin_mcp_config)))

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
    if remote_url:
        results.append(
            _run_remote_client_smoke(
                remote_url=remote_url,
                remote_token_env=remote_token_env,
                timeout_seconds=timeout_seconds,
            )
        )

    launcher_ready = bool(results) and all(bool(result.get("launcher_ready")) for result in results)
    process_started = bool(results) and all(bool(result.get("process_started")) for result in results)
    mcp_initialized = bool(results) and all(bool(result.get("mcp_initialized")) for result in results)
    tools_discovered = bool(results) and all(bool(result.get("tools_discovered")) for result in results)
    end_to_end_verified = bool(results) and all(
        bool(result.get("end_to_end_verified")) and bool(result.get("index_status_verified"))
        for result in results
    )
    verification_prompt = f"@{server_name} MCP 연결 상태와 사용 가능한 규정 도구를 보여줘."
    report = {
        "report_type": "mcp_client_config_smoke",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "server_name": server_name,
        "passed": end_to_end_verified,
        "launcher_ready": launcher_ready,
        "process_started": process_started,
        "mcp_initialized": mcp_initialized,
        "tools_discovered": tools_discovered,
        "conversation_attachment_unverified": True,
        "tool_scan_unverified": bool(remote_url),
        "end_to_end_verified": end_to_end_verified,
        "verification_prompt": verification_prompt,
        "results": results,
    }
    report["verification_answer"] = _verification_answer(report)
    if not results:
        report["error"] = (
            "At least one local config or --remote-url is required."
        )

    if out_json is not None:
        out_path = Path(out_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _verification_answer(report: dict[str, Any]) -> dict[str, Any]:
    results = report.get("results") if isinstance(report.get("results"), list) else []
    tool_names = sorted(
        {
            str(tool_name)
            for result in results
            if isinstance(result, dict)
            for tool_name in (result.get("tool_names") or [])
            if str(tool_name).strip()
        }
    )
    index_summaries = [
        result.get("index_status_summary")
        for result in results
        if isinstance(result, dict) and isinstance(result.get("index_status_summary"), dict)
    ]
    verified = bool(report.get("end_to_end_verified"))
    return {
        "status": "verified" if verified else "not_verified",
        "mcp_initialized": bool(report.get("mcp_initialized")),
        "tools_discovered": bool(report.get("tools_discovered")),
        "get_index_status_verified": verified,
        "available_regulation_tools": tool_names,
        "index_status_summaries": index_summaries,
        "conversation_attachment_unverified": bool(report.get("conversation_attachment_unverified")),
        "message": (
            "MCP initialize, tools/list, and get_index_status completed successfully."
            if verified
            else "MCP connection verification is incomplete; do not report this connection as connected."
        ),
    }


def _run_single_client_config_smoke(
    *,
    client_key: str,
    config_path: Path,
    server_name: str,
    query: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    label = {
        "codex": "Codex",
        "claude_desktop": "Claude Desktop",
        "chatgpt_desktop_local": "ChatGPT Desktop local plugin",
    }.get(client_key, client_key)
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
                "launcher_ready": True,
            }
        )
        return result
    except Exception as exc:
        return {
            "label": client_key,
            "config_path": str(config_path),
            "passed": False,
            "launcher_ready": False,
            "process_started": False,
            "mcp_initialized": False,
            "tools_discovered": False,
            "end_to_end_verified": False,
            "error": str(exc),
        }


def _read_client_server_entry(*, client_key: str, config_path: Path, server_name: str) -> dict[str, Any]:
    if client_key == "codex":
        payload = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
        servers = payload.get("mcp_servers") if isinstance(payload, dict) else None
    elif client_key in {"claude_desktop", "chatgpt_desktop_local"}:
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


def _run_remote_client_smoke(
    *,
    remote_url: str,
    remote_token_env: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    url = remote_url.strip()
    if not url.lower().startswith("https://"):
        return {
            "label": "chatgpt_remote",
            "remote_url": url,
            "passed": False,
            "launcher_ready": False,
            "process_started": False,
            "mcp_initialized": False,
            "tools_discovered": False,
            "end_to_end_verified": False,
            "error": "Remote ChatGPT MCP verification requires an https:// endpoint.",
        }
    token = os.getenv(remote_token_env, "").strip() if remote_token_env else ""
    if remote_token_env and not token:
        return {
            "label": "chatgpt_remote",
            "remote_url": url,
            "passed": False,
            "launcher_ready": False,
            "process_started": False,
            "mcp_initialized": False,
            "tools_discovered": False,
            "end_to_end_verified": False,
            "error": f"Environment variable is not set or empty: {remote_token_env}",
        }
    try:
        result = asyncio.run(
            asyncio.wait_for(
                _run_remote_entry(url=url, token=token or None),
                timeout=timeout_seconds,
            )
        )
        result.update({"label": "chatgpt_remote", "remote_url": url, "launcher_ready": True})
        return result
    except Exception as exc:
        return {
            "label": "chatgpt_remote",
            "remote_url": url,
            "passed": False,
            "launcher_ready": True,
            "process_started": False,
            "mcp_initialized": False,
            "tools_discovered": False,
            "end_to_end_verified": False,
            "error": str(exc),
        }


async def _run_remote_entry(*, url: str, token: str | None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(headers=headers, follow_redirects=False) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write, get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tool_result = await session.list_tools()
                tool_names = sorted(tool.name for tool in tool_result.tools)
                if "get_index_status" not in tool_names:
                    return {
                        "passed": False,
                        "process_started": True,
                        "mcp_initialized": True,
                        "tools_discovered": bool(tool_names),
                        "index_status_verified": False,
                        "end_to_end_verified": False,
                        "tool_names": tool_names,
                        "session_id_present": bool(get_session_id()),
                        "error": "tools/list did not expose get_index_status.",
                    }
                index_status = await session.call_tool(
                    "get_index_status",
                    {"security_levels": ["internal"]},
                )
                index_payload = _tool_payload(index_status)
                index_summary = index_payload.get("summary") if isinstance(index_payload.get("summary"), dict) else {}
                verified = bool(index_summary)
                return {
                    "passed": verified,
                    "process_started": True,
                    "mcp_initialized": True,
                    "tools_discovered": bool(tool_names),
                    "index_status_verified": verified,
                    "end_to_end_verified": verified,
                    "tool_names": tool_names,
                    "index_status_summary": index_summary,
                    "session_id_present": bool(get_session_id()),
                }


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

            index_status_started_at = time.perf_counter()
            index_status = await session.call_tool(
                "get_index_status",
                {"security_levels": ["internal"]},
            )
            index_status_elapsed_ms = _elapsed_ms(index_status_started_at)
            index_status_payload = _tool_payload(index_status)
            index_summary = (
                index_status_payload.get("summary")
                if isinstance(index_status_payload.get("summary"), dict)
                else {}
            )
            index_status_verified = bool(index_summary)

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
                "passed": bool(
                    {"search", "fetch", "get_index_status"}.issubset(set(tool_names))
                    and index_status_verified
                    and results
                    and fetch_payload.get("text")
                ),
                "process_started": True,
                "mcp_initialized": True,
                "tools_discovered": bool(tool_names),
                "index_status_verified": index_status_verified,
                "end_to_end_verified": bool(
                    {"search", "fetch", "get_index_status"}.issubset(set(tool_names))
                    and index_status_verified
                    and results
                    and fetch_payload.get("text")
                ),
                "tool_names": tool_names,
                "index_status_summary": index_summary,
                "search_result_count": len(results),
                "fetch_has_text": bool(fetch_payload.get("text")),
                "first_id": first_id,
                "first_result_metadata": (results[0] if results else {}).get("metadata") or {},
                "list_tools_elapsed_ms": list_tools_elapsed_ms,
                "index_status_elapsed_ms": index_status_elapsed_ms,
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
    parser.add_argument("--plugin-mcp-config", default=None)
    parser.add_argument("--remote-url", default=None)
    parser.add_argument("--remote-token-env", default=None)
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
        plugin_mcp_config=args.plugin_mcp_config,
        remote_url=args.remote_url,
        remote_token_env=args.remote_token_env,
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
