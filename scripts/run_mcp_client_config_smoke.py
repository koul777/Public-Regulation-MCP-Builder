from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import ipaddress
import os
import json
from pathlib import Path
import re
import secrets
import sys
import time
import tomllib
from typing import Any, Sequence, TextIO
from urllib.parse import urlsplit, urlunsplit

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_metadata import current_repo_commit


UTF8_BOM = b"\xef\xbb\xbf"
DEFAULT_SEARCH_QUERY = "\uc81c1\uc870"
_EXTERNAL_METADATA_DENY_KEYS = frozenset(
    {
        "source_record_id",
        "source_file_id",
        "approval_review_batch_manifest_path",
        "approval_review_batch_manifest_sha256",
        "approval_worklist_report_sha256",
    }
)


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
        bool(result.get("contract_verified", result.get("end_to_end_verified")))
        for result in results
    )
    local_results = [result for result in results if result.get("label") != "chatgpt_remote"]
    direct_stdio_verified = bool(local_results) and all(
        bool(result.get("end_to_end_verified")) and bool(result.get("strict_stdio_wire_verified"))
        for result in local_results
    )
    verification_prompt = f"{server_name} MCP의 연결 상태와 사용 가능한 규정 도구를 보여줘."
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
        "direct_stdio_verified": direct_stdio_verified,
        "desktop_tool_scan_verified": False,
        "conversation_attachment_verified": False,
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
    verification_modes = sorted(
        {
            str(result.get("verification_mode") or "index_status")
            for result in results
            if isinstance(result, dict) and result.get("end_to_end_verified")
        }
    )
    index_status_verified = any(
        str(result.get("verification_mode") or "index_status") == "index_status"
        and bool(result.get("index_status_verified"))
        for result in results
        if isinstance(result, dict)
    )
    search_fetch_verified = any(
        str(result.get("verification_mode") or "") == "search_fetch"
        and bool(result.get("contract_verified"))
        for result in results
        if isinstance(result, dict)
    )
    return {
        "status": "verified" if verified else "not_verified",
        "mcp_initialized": bool(report.get("mcp_initialized")),
        "tools_discovered": bool(report.get("tools_discovered")),
        "get_index_status_verified": index_status_verified,
        "search_fetch_verified": search_fetch_verified,
        "verification_modes": verification_modes,
        "direct_stdio_verified": bool(report.get("direct_stdio_verified")),
        "desktop_tool_scan_verified": bool(report.get("desktop_tool_scan_verified")),
        "conversation_attachment_verified": bool(report.get("conversation_attachment_verified")),
        "available_regulation_tools": tool_names,
        "index_status_summaries": index_summaries,
        "conversation_attachment_unverified": bool(report.get("conversation_attachment_unverified")),
        "message": (
            "MCP initialize and the configured verification contract completed successfully on the direct transport; Desktop tool exposure and conversation attachment remain separate states."
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
                "config_encoding_verified": True,
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
            "config_encoding_verified": False,
            "strict_stdio_wire_verified": False,
            "end_to_end_verified": False,
            "error": _exception_message(exc),
        }


def _read_client_server_entry(*, client_key: str, config_path: Path, server_name: str) -> dict[str, Any]:
    if client_key == "codex":
        payload = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
        servers = payload.get("mcp_servers") if isinstance(payload, dict) else None
    elif client_key == "claude_desktop":
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
        servers = payload.get("mcpServers") if isinstance(payload, dict) else None
    elif client_key == "chatgpt_desktop_local":
        payload = _read_strict_utf8_json(config_path)
        if isinstance(payload, dict) and "mcp_servers" in payload:
            raise ValueError(
                f"{config_path} uses unsupported mcp_servers; Codex plugin .mcp.json requires mcpServers."
            )
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


def _read_strict_utf8_json(path: Path) -> Any:
    raw = path.read_bytes()
    if raw.startswith(UTF8_BOM):
        raise ValueError(
            f"{path} must be UTF-8 without BOM; found forbidden EF BB BF prefix in ChatGPT Desktop plugin config."
        )
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} must contain strict UTF-8 JSON: {exc}") from exc
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{path} must contain valid strict UTF-8 JSON: {exc}") from exc


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _validate_strict_jsonrpc_stdout(stdout: bytes) -> dict[str, Any]:
    """Validate a captured MCP stdio stdout stream without tolerating noise or blank records."""
    if stdout.startswith(UTF8_BOM):
        raise ValueError("MCP stdio stdout must not begin with a UTF-8 BOM.")
    try:
        text = stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"MCP stdio stdout must be strict UTF-8: {exc}") from exc
    records = text.split("\n")
    if records and records[-1] == "":
        records.pop()
    if not records:
        raise ValueError("MCP stdio stdout did not contain a JSON-RPC message.")
    messages: list[dict[str, Any]] = []
    for line_number, record in enumerate(records, start=1):
        line = record.removesuffix("\r")
        if not line:
            raise ValueError(f"MCP stdio stdout contains a blank line at record {line_number}.")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"MCP stdio stdout record {line_number} is not JSON-RPC JSON: {exc}"
            ) from exc
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ValueError(f"MCP stdio stdout record {line_number} is not a JSON-RPC 2.0 object.")
        if not any(key in message for key in ("method", "result", "error")):
            raise ValueError(f"MCP stdio stdout record {line_number} has no method, result, or error member.")
        messages.append(message)
    return {"passed": True, "message_count": len(messages)}


def _run_remote_client_smoke(
    *,
    remote_url: str,
    remote_token_env: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    url = _normalize_remote_mcp_url(remote_url)
    report_url = _sanitize_remote_url_for_report(remote_url)
    if url is None:
        return {
            "label": "chatgpt_remote",
            "remote_url": report_url,
            "passed": False,
            "launcher_ready": False,
            "process_started": False,
            "mcp_initialized": False,
            "tools_discovered": False,
            "end_to_end_verified": False,
            "auth_wire_verified": False,
            "error": (
                "Remote MCP verification requires a valid public https:// URL without "
                "credentials, query parameters, fragments, whitespace, or a non-public IP literal."
            ),
        }
    if not remote_token_env:
        return {
            "label": "chatgpt_remote",
            "remote_url": report_url,
            "passed": False,
            "launcher_ready": False,
            "process_started": False,
            "mcp_initialized": False,
            "tools_discovered": False,
            "end_to_end_verified": False,
            "auth_wire_verified": False,
            "error": "A named bearer-token environment variable is required for a direct remote MCP URL.",
        }
    token = os.getenv(remote_token_env, "").strip()
    if not token:
        return {
            "label": "chatgpt_remote",
            "remote_url": report_url,
            "passed": False,
            "launcher_ready": False,
            "process_started": False,
            "mcp_initialized": False,
            "tools_discovered": False,
            "end_to_end_verified": False,
            "auth_wire_verified": False,
            "error": f"Environment variable is not set or empty: {remote_token_env}",
        }
    try:
        result = asyncio.run(
            asyncio.wait_for(
                _run_remote_entry_with_auth_verification(url=url, token=token or None),
                timeout=timeout_seconds,
            )
        )
        result.update({"label": "chatgpt_remote", "remote_url": report_url, "launcher_ready": True})
        return result
    except Exception as exc:
        return {
            "label": "chatgpt_remote",
            "remote_url": report_url,
            "passed": False,
            "launcher_ready": True,
            "process_started": False,
            "mcp_initialized": False,
            "tools_discovered": False,
            "end_to_end_verified": False,
            "auth_wire_verified": False,
            "error": _redact_remote_exception(exc, token=token, remote_url=remote_url),
        }


async def _run_remote_entry_with_auth_verification(*, url: str, token: str | None) -> dict[str, Any]:
    auth_challenge_observed = await _remote_unauthenticated_request_is_rejected(url=url) if token else False
    result = await _run_remote_entry(url=url, token=token)
    result["auth_challenge_observed"] = auth_challenge_observed
    result["auth_wire_verified"] = bool(auth_challenge_observed)
    if not token or not auth_challenge_observed:
        protocol_contract_verified = bool(result.get("contract_verified", result.get("end_to_end_verified")))
        result["protocol_contract_verified"] = protocol_contract_verified
        result["passed"] = False
        result["contract_verified"] = False
        result["end_to_end_verified"] = False
        result["error"] = (
            "The endpoint did not reject both unauthenticated and invalid-bearer MCP initialize requests with 401/403; "
            "bearer authentication is not fail-closed."
        )
    return result


async def _remote_unauthenticated_request_is_rejected(*, url: str) -> bool:
    initialize_request = {
        "jsonrpc": "2.0",
        "id": "auth-wire-probe",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "reg-rag-mcp-auth-probe", "version": "0.1"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}
    invalid_headers = {
        **headers,
        "Authorization": f"Bearer invalid-{secrets.token_urlsafe(32)}",
    }
    async with httpx.AsyncClient(follow_redirects=False) as http_client:
        unauthenticated = await http_client.post(url, json=initialize_request, headers=headers)
        invalid_bearer = await http_client.post(url, json=initialize_request, headers=invalid_headers)
    return all(response.status_code in {401, 403} for response in (unauthenticated, invalid_bearer))


async def _run_remote_entry(*, url: str, token: str | None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(headers=headers, follow_redirects=False) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write, get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tool_result = await session.list_tools()
                tool_names = sorted(tool.name for tool in tool_result.tools)
                if "get_index_status" not in tool_names and not {"search", "fetch"}.issubset(set(tool_names)):
                    return {
                        "passed": False,
                        "process_started": True,
                        "mcp_initialized": True,
                        "tools_discovered": bool(tool_names),
                        "index_status_verified": False,
                        "end_to_end_verified": False,
                        "tool_names": tool_names,
                        "session_id_present": bool(get_session_id()),
                        "error": "tools/list did not expose get_index_status or the external search/fetch contract.",
                    }
                verification_mode = "index_status"
                index_summary: dict[str, Any] = {}
                verified = False
                if "get_index_status" in tool_names:
                    index_status = await session.call_tool(
                        "get_index_status",
                        {"security_levels": ["internal"]},
                    )
                    index_payload = _successful_tool_payload(index_status, tool_name="get_index_status")
                    index_summary = index_payload.get("summary") if isinstance(index_payload.get("summary"), dict) else {}
                    verified = _valid_index_status_summary(index_summary)
                else:
                    # The privacy-reduced ChatGPT profile intentionally exposes
                    # only search/fetch. Verify that content contract without
                    # requiring internal index diagnostics to cross the boundary.
                    verification_mode = "search_fetch"
                    search = await session.call_tool(
                        "search",
                        {"query": DEFAULT_SEARCH_QUERY, "top_k": 1},
                    )
                    search_payload = _successful_tool_payload(search, tool_name="search")
                    results = search_payload.get("results") if isinstance(search_payload.get("results"), list) else []
                    first_id = _first_search_result_id(results)
                    fetch_payload: dict[str, Any] = {}
                    if first_id:
                        fetch = await session.call_tool("fetch", {"id": first_id})
                        fetch_payload = _successful_tool_payload(fetch, tool_name="fetch")
                    metadata_candidates: list[Any] = []
                    if results and isinstance(results[0], dict):
                        metadata_candidates.append(results[0].get("metadata") or {})
                    metadata_candidates.append(fetch_payload.get("metadata") or {})
                    metadata_violations = _external_metadata_violations(metadata_candidates)
                    verified = bool(
                        _valid_search_results(results)
                        and _valid_fetch_payload(fetch_payload)
                        and not metadata_violations
                    )
                return {
                    "passed": verified,
                    "process_started": True,
                    "mcp_initialized": True,
                    "tools_discovered": bool(tool_names),
                    "index_status_verified": bool(verification_mode == "index_status" and verified),
                    "end_to_end_verified": verified,
                    "contract_verified": verified,
                    "verification_mode": verification_mode,
                    "external_metadata_violations": metadata_violations if verification_mode == "search_fetch" else [],
                    "external_metadata_redaction_verified": bool(
                        verification_mode != "search_fetch" or not metadata_violations
                    ),
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
    # The MCP client library intentionally inherits a narrow environment and
    # drops custom interpreter selectors. Preserve the explicit runtime
    # selector used by generated bundle launchers so smoke tests exercise the
    # same wheel/source fallback that operators configure.
    runtime_python = os.getenv("REG_RAG_PYTHON", "").strip()
    params = StdioServerParameters(
        command=command,
        args=args,
        env={"REG_RAG_PYTHON": runtime_python} if runtime_python else None,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            list_tools_started_at = time.perf_counter()
            tool_result = await session.list_tools()
            list_tools_elapsed_ms = _elapsed_ms(list_tools_started_at)
            tool_names = sorted(tool.name for tool in tool_result.tools)

            if "get_index_status" not in tool_names and {"search", "fetch"}.issubset(set(tool_names)):
                search_payload, results, search_query_used, search_queries_attempted, search_elapsed_ms = (
                    await _search_with_fallback(session, query=query)
                )
                first_id = str((results[0] if results else {}).get("id") or "")
                fetch_payload: dict[str, Any] = {}
                fetch_elapsed_ms = 0.0
                if first_id:
                    fetch_started_at = time.perf_counter()
                    fetch = await session.call_tool("fetch", {"id": first_id})
                    fetch_elapsed_ms = _elapsed_ms(fetch_started_at)
                    fetch_payload = _tool_payload(fetch)
                metadata_candidates: list[Any] = []
                if results and isinstance(results[0], dict):
                    metadata_candidates.append(results[0].get("metadata") or {})
                metadata_candidates.append(fetch_payload.get("metadata") or {})
                metadata_violations = _external_metadata_violations(metadata_candidates)
                verified = bool(results and fetch_payload.get("text") and not metadata_violations)
                return {
                    "passed": verified,
                    "process_started": True,
                    "mcp_initialized": True,
                    "tools_discovered": bool(tool_names),
                    "strict_stdio_wire_verified": True,
                    "index_status_verified": False,
                    "contract_verified": verified,
                    "end_to_end_verified": verified,
                    "verification_mode": "search_fetch",
                    "external_metadata_violations": metadata_violations,
                    "external_metadata_redaction_verified": not metadata_violations,
                    "tool_names": tool_names,
                    "index_status_summary": {},
                    "search_result_count": len(results),
                    "search_query_used": search_query_used,
                    "search_queries_attempted": search_queries_attempted,
                    "fetch_has_text": bool(fetch_payload.get("text")),
                    "first_id": first_id,
                    "first_result_metadata": (results[0] if results else {}).get("metadata") or {},
                    "list_tools_elapsed_ms": list_tools_elapsed_ms,
                    "index_status_elapsed_ms": 0.0,
                    "search_elapsed_ms": search_elapsed_ms,
                    "fetch_elapsed_ms": fetch_elapsed_ms,
                    "total_elapsed_ms": _elapsed_ms(started_at),
                }

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

            search_payload, results, search_query_used, search_queries_attempted, search_elapsed_ms = (
                await _search_with_fallback(session, query=query)
            )

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
                "strict_stdio_wire_verified": True,
                "index_status_verified": index_status_verified,
                "contract_verified": bool(
                    {"search", "fetch", "get_index_status"}.issubset(set(tool_names))
                    and index_status_verified
                    and results
                    and fetch_payload.get("text")
                ),
                "end_to_end_verified": bool(
                    {"search", "fetch", "get_index_status"}.issubset(set(tool_names))
                    and index_status_verified
                    and results
                    and fetch_payload.get("text")
                ),
                "tool_names": tool_names,
                "index_status_summary": index_summary,
                "search_result_count": len(results),
                "search_query_used": search_query_used,
                "search_queries_attempted": search_queries_attempted,
                "fetch_has_text": bool(fetch_payload.get("text")),
                "first_id": first_id,
                "first_result_metadata": (results[0] if results else {}).get("metadata") or {},
                "list_tools_elapsed_ms": list_tools_elapsed_ms,
                "index_status_elapsed_ms": index_status_elapsed_ms,
                "search_elapsed_ms": search_elapsed_ms,
                "fetch_elapsed_ms": fetch_elapsed_ms,
                "total_elapsed_ms": _elapsed_ms(started_at),
            }


async def _search_with_fallback(
    session: ClientSession,
    *,
    query: str,
) -> tuple[dict[str, Any], list[Any], str, list[str], float]:
    candidates: list[str] = []
    for value in (query, DEFAULT_SEARCH_QUERY, "규정"):
        normalized = str(value or "").strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    started_at = time.perf_counter()
    last_payload: dict[str, Any] = {}
    attempted: list[str] = []
    for candidate in candidates:
        attempted.append(candidate)
        search = await session.call_tool(
            "search",
            {
                "query": candidate,
                "top_k": 3,
                "security_levels": ["internal"],
            },
        )
        last_payload = _tool_payload(search)
        results = last_payload.get("results") if isinstance(last_payload.get("results"), list) else []
        if results:
            return last_payload, results, candidate, attempted, _elapsed_ms(started_at)
    return last_payload, [], attempted[-1] if attempted else "", attempted, _elapsed_ms(started_at)


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 3)


def _external_metadata_violations(metadata_candidates: Sequence[Any]) -> list[str]:
    return sorted(
        {
            key
            for metadata in metadata_candidates
            if isinstance(metadata, dict)
            for key in _EXTERNAL_METADATA_DENY_KEYS
            if key in metadata and metadata.get(key) not in (None, "", [], {})
        }
    )


def _normalize_remote_mcp_url(remote_url: str | None) -> str | None:
    cleaned = str(remote_url or "").strip()
    if not cleaned or any(character.isspace() or ord(character) < 32 for character in cleaned):
        return None
    try:
        parsed = urlsplit(cleaned)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
        or _is_non_public_ip_literal(parsed.hostname)
    ):
        return None
    return urlunsplit(("https", parsed.netloc, parsed.path or "/mcp", "", ""))


def _sanitize_remote_url_for_report(remote_url: str | None) -> str | None:
    cleaned = str(remote_url or "").strip()
    if not cleaned:
        return None
    try:
        parsed = urlsplit(cleaned)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return None
    report_host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        report_host = f"{report_host}:{port}"
    return urlunsplit((parsed.scheme.lower(), report_host, parsed.path or "/mcp", "", ""))


def _is_non_public_ip_literal(hostname: str) -> bool:
    normalized_hostname = hostname.rstrip(".").casefold()
    if normalized_hostname == "localhost" or normalized_hostname.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(normalized_hostname)
    except ValueError:
        return False
    return bool(not address.is_global or address.is_multicast)


def _successful_tool_payload(result: Any, *, tool_name: str) -> dict[str, Any]:
    if bool(getattr(result, "isError", False)):
        raise ValueError(f"MCP tool {tool_name} returned isError=true.")
    payload = _tool_payload(result)
    if not isinstance(payload, dict) or not payload:
        raise ValueError(f"MCP tool {tool_name} did not return a non-empty object payload.")
    return payload


def _valid_index_status_summary(summary: dict[str, Any]) -> bool:
    if not summary:
        return False
    count_keys = ("document_count", "record_count", "indexed_records", "regulation_count")
    has_valid_count = any(
        isinstance(summary.get(key), int) and not isinstance(summary.get(key), bool) and summary[key] >= 0
        for key in count_keys
    )
    status_counts = summary.get("status_counts")
    has_valid_status_counts = isinstance(status_counts, dict) and bool(status_counts) and all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in status_counts.values()
    )
    return bool(has_valid_count or has_valid_status_counts)


def _valid_search_results(results: list[Any]) -> bool:
    return bool(results) and all(
        isinstance(result, dict) and isinstance(result.get("id"), str) and bool(result["id"].strip())
        for result in results
    )


def _first_search_result_id(results: list[Any]) -> str:
    if not _valid_search_results(results):
        return ""
    return str(results[0]["id"]).strip()


def _valid_fetch_payload(payload: dict[str, Any]) -> bool:
    text = payload.get("text")
    return isinstance(text, str) and bool(text.strip())


def _exception_message(exc: BaseException) -> str:
    """Flatten TaskGroup/ExceptionGroup errors into an actionable smoke detail."""
    nested = getattr(exc, "exceptions", None)
    if isinstance(nested, tuple) and nested:
        details = [_exception_message(item) for item in nested]
        return f"{exc.__class__.__name__}: {'; '.join(details)}"
    message = str(exc).strip()
    return f"{exc.__class__.__name__}: {message}" if message else exc.__class__.__name__


def _redact_values(text: str, values: Sequence[str]) -> str:
    """Remove runtime-only secret values from diagnostics before serialization."""
    redacted = text
    for value in sorted({item for item in values if item}, key=len, reverse=True):
        redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def _redact_remote_exception(exc: BaseException, *, token: str, remote_url: str) -> str:
    detail = _redact_values(_exception_message(exc), [token])
    safe_url = _sanitize_remote_url_for_report(remote_url) or "[REDACTED_REMOTE_URL]"
    if remote_url:
        detail = detail.replace(remote_url, safe_url)
    detail = re.sub(
        r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+",
        r"\1[REDACTED]",
        detail,
    )

    def sanitize_url(match: re.Match[str]) -> str:
        return _sanitize_remote_url_for_report(match.group(0)) or "[REDACTED_REMOTE_URL]"

    return re.sub(r"https?://[^\s]+", sanitize_url, detail, flags=re.IGNORECASE)


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
