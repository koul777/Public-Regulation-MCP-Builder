from __future__ import annotations

import argparse
import asyncio
import json
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_mcp_smoke import run_mcp_smoke
from scripts.report_metadata import current_repo_commit


DEFAULT_SEARCH_QUERY = "Article"


def run_mcp_transport_smoke(
    *,
    data_dir: Path | None = None,
    tenant_id: str = "tenant-mcp-transport-smoke",
    profile_id: str | None = None,
    tenant_storage_isolation: bool = True,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int | None = None,
    out_json: Path | None = None,
    timeout_seconds: float = 20.0,
    prepare: bool = True,
    query: str = DEFAULT_SEARCH_QUERY,
    allow_persistent_smoke_data: bool = False,
    no_warm_cache: bool = False,
) -> dict[str, Any]:
    if data_dir is None:
        with tempfile.TemporaryDirectory(prefix="reg_rag_mcp_transport_smoke_") as tmp:
            return _run_transport_smoke_with_data_dir(
                Path(tmp) / "data",
                tenant_id=tenant_id,
                profile_id=profile_id,
                tenant_storage_isolation=tenant_storage_isolation,
                transport=transport,
                host=host,
                port=port,
                out_json=out_json,
                timeout_seconds=timeout_seconds,
                prepare=prepare,
                query=query,
                allow_persistent_smoke_data=False,
                no_warm_cache=no_warm_cache,
                disposable_data_dir=True,
            )
    return _run_transport_smoke_with_data_dir(
        data_dir,
        tenant_id=tenant_id,
        profile_id=profile_id,
        tenant_storage_isolation=tenant_storage_isolation,
        transport=transport,
        host=host,
        port=port,
        out_json=out_json,
        timeout_seconds=timeout_seconds,
        prepare=prepare,
        query=query,
        allow_persistent_smoke_data=allow_persistent_smoke_data,
        no_warm_cache=no_warm_cache,
        disposable_data_dir=False,
    )


def _run_transport_smoke_with_data_dir(
    data_dir: Path,
    *,
    tenant_id: str,
    profile_id: str | None,
    tenant_storage_isolation: bool,
    transport: str,
    host: str,
    port: int | None,
    out_json: Path | None,
    timeout_seconds: float,
    prepare: bool,
    query: str,
    allow_persistent_smoke_data: bool,
    no_warm_cache: bool,
    disposable_data_dir: bool,
) -> dict[str, Any]:
    normalized_transport = transport.strip().lower()
    if normalized_transport not in {"stdio", "streamable-http"}:
        raise ValueError("transport must be stdio or streamable-http.")
    if prepare:
        try:
            preparation = run_mcp_smoke(
                data_dir=data_dir,
                tenant_id=tenant_id,
                profile_id=profile_id,
                tenant_storage_isolation=tenant_storage_isolation,
                allow_persistent_smoke_data=allow_persistent_smoke_data,
                disposable_data_dir=disposable_data_dir,
            )
        except ValueError as exc:
            report = _preparation_failure_report(
                tenant_id=tenant_id,
                profile_id=profile_id,
                tenant_storage_isolation=tenant_storage_isolation,
                query=query,
                error=str(exc),
                transport=normalized_transport,
                persistent_smoke_data_opt_in=allow_persistent_smoke_data,
            )
            if out_json:
                out_json.parent.mkdir(parents=True, exist_ok=True)
                out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            return report
    else:
        preparation = {
            "passed": True,
            "search_result_count": None,
            "evidence_summary": {"passed": None},
            "skipped": True,
        }
    try:
        if normalized_transport == "stdio":
            check_coro = _run_stdio_client_checks(
                data_dir=data_dir,
                tenant_id=tenant_id,
                profile_id=profile_id,
                tenant_storage_isolation=tenant_storage_isolation,
                query=query,
                no_warm_cache=no_warm_cache,
            )
        else:
            check_coro = _run_streamable_http_client_checks(
                data_dir=data_dir,
                tenant_id=tenant_id,
                profile_id=profile_id,
                tenant_storage_isolation=tenant_storage_isolation,
                query=query,
                no_warm_cache=no_warm_cache,
                host=host,
                port=port,
                startup_timeout_seconds=min(timeout_seconds, 15.0),
            )
        transport = asyncio.run(
            asyncio.wait_for(
                check_coro,
                timeout=timeout_seconds,
            )
        )
    except Exception as exc:
        transport = {
            "passed": False,
            "error": str(exc),
            "full_profile": {},
            "chatgpt_data_profile": {},
        }

    full_profile = transport.get("full_profile") if isinstance(transport.get("full_profile"), dict) else {}
    chatgpt_profile = (
        transport.get("chatgpt_data_profile") if isinstance(transport.get("chatgpt_data_profile"), dict) else {}
    )
    report = {
        "report_type": "mcp_transport_smoke",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "tenant_storage_isolation": tenant_storage_isolation,
        "transport": normalized_transport,
        "host": host if normalized_transport == "streamable-http" else None,
        "query": query,
        "no_warm_cache": no_warm_cache,
        "passed": bool(
            preparation.get("passed")
            and transport.get("passed")
            and full_profile.get("search_result_count", 0) >= 1
            and full_profile.get("fetch_has_text")
            and full_profile.get("history_tool_available")
            and full_profile.get("history_passed")
            and set(chatgpt_profile.get("tool_names") or []) == {"search", "fetch"}
        ),
        "process_started": bool(full_profile.get("process_started")),
        "mcp_initialized": bool(full_profile.get("mcp_initialized")),
        "tools_discovered": bool(full_profile.get("tools_discovered")),
        "end_to_end_verified": bool(full_profile.get("end_to_end_verified")),
        "preparation": {
            "passed": bool(preparation.get("passed")),
            "search_result_count": preparation.get("search_result_count"),
            "evidence_passed": (preparation.get("evidence_summary") or {}).get("passed"),
            "skipped": bool(preparation.get("skipped")),
            "data_dir_mode": preparation.get("data_dir_mode"),
            "synthetic_runtime": preparation.get("synthetic_runtime"),
            "handoff_evidence": preparation.get("handoff_evidence"),
            "persistent_smoke_data_opt_in": preparation.get("persistent_smoke_data_opt_in"),
        },
        "full_profile": full_profile,
        "chatgpt_data_profile": chatgpt_profile,
        "error": transport.get("error"),
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _preparation_failure_report(
    *,
    tenant_id: str,
    profile_id: str | None,
    tenant_storage_isolation: bool,
    query: str,
    error: str,
    transport: str,
    persistent_smoke_data_opt_in: bool,
) -> dict[str, Any]:
    return {
        "report_type": "mcp_transport_smoke",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "tenant_storage_isolation": tenant_storage_isolation,
        "transport": transport,
        "query": query,
        "passed": False,
        "preparation": {
            "passed": False,
            "search_result_count": None,
            "evidence_passed": None,
            "skipped": False,
            "data_dir_mode": "explicit_refused",
            "synthetic_runtime": True,
            "handoff_evidence": False,
            "persistent_smoke_data_opt_in": persistent_smoke_data_opt_in,
            "error": error,
        },
        "full_profile": {},
        "chatgpt_data_profile": {},
        "error": error,
    }


async def _run_stdio_client_checks(
    *,
    data_dir: Path,
    tenant_id: str,
    profile_id: str | None,
    tenant_storage_isolation: bool,
    query: str,
    no_warm_cache: bool,
) -> dict[str, Any]:
    full_profile = await _call_stdio_profile(
        data_dir=data_dir,
        tenant_id=tenant_id,
        profile_id=profile_id,
        tenant_storage_isolation=tenant_storage_isolation,
        tool_profile="full",
        query=query,
        no_warm_cache=no_warm_cache,
    )
    chatgpt_data_profile = await _call_stdio_profile(
        data_dir=data_dir,
        tenant_id=tenant_id,
        profile_id=profile_id,
        tenant_storage_isolation=tenant_storage_isolation,
        tool_profile="chatgpt-data",
        query=query,
        no_warm_cache=no_warm_cache,
    )
    return {
        "passed": bool(full_profile.get("passed") and chatgpt_data_profile.get("passed")),
        "full_profile": full_profile,
        "chatgpt_data_profile": chatgpt_data_profile,
    }


async def _run_streamable_http_client_checks(
    *,
    data_dir: Path,
    tenant_id: str,
    profile_id: str | None,
    tenant_storage_isolation: bool,
    query: str,
    no_warm_cache: bool,
    host: str,
    port: int | None,
    startup_timeout_seconds: float,
) -> dict[str, Any]:
    full_profile = await _call_streamable_http_profile(
        data_dir=data_dir,
        tenant_id=tenant_id,
        profile_id=profile_id,
        tenant_storage_isolation=tenant_storage_isolation,
        tool_profile="full",
        query=query,
        no_warm_cache=no_warm_cache,
        host=host,
        port=port,
        startup_timeout_seconds=startup_timeout_seconds,
    )
    chatgpt_data_profile = await _call_streamable_http_profile(
        data_dir=data_dir,
        tenant_id=tenant_id,
        profile_id=profile_id,
        tenant_storage_isolation=tenant_storage_isolation,
        tool_profile="chatgpt-data",
        query=query,
        no_warm_cache=no_warm_cache,
        host=host,
        port=port,
        startup_timeout_seconds=startup_timeout_seconds,
    )
    return {
        "passed": bool(full_profile.get("passed") and chatgpt_data_profile.get("passed")),
        "full_profile": full_profile,
        "chatgpt_data_profile": chatgpt_data_profile,
    }


async def _call_stdio_profile(
    *,
    data_dir: Path,
    tenant_id: str,
    profile_id: str | None,
    tenant_storage_isolation: bool,
    tool_profile: str,
    query: str,
    no_warm_cache: bool,
) -> dict[str, Any]:
    server_script = PROJECT_ROOT / "scripts" / "run_regulation_mcp.py"
    profile_started_at = time.perf_counter()
    server_args = [
        str(server_script),
        "--data-dir",
        str(data_dir),
        "--tenant-id",
        tenant_id,
        "--tool-profile",
        tool_profile,
        "--transport",
        "stdio",
    ]
    if profile_id:
        server_args.extend(["--profile-id", profile_id])
    if tenant_storage_isolation:
        server_args.append("--tenant-storage-isolation")
    else:
        server_args.append("--flat-storage")
    if no_warm_cache:
        server_args.append("--no-warm-cache")
    params = StdioServerParameters(
        command=sys.executable,
        args=server_args,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await _call_profile_tools(
                session,
                tool_profile=tool_profile,
                profile_id=profile_id,
                query=query,
                no_warm_cache=no_warm_cache,
                profile_started_at=profile_started_at,
            )


async def _call_streamable_http_profile(
    *,
    data_dir: Path,
    tenant_id: str,
    profile_id: str | None,
    tenant_storage_isolation: bool,
    tool_profile: str,
    query: str,
    no_warm_cache: bool,
    host: str,
    port: int | None,
    startup_timeout_seconds: float,
) -> dict[str, Any]:
    server_script = PROJECT_ROOT / "scripts" / "run_regulation_mcp.py"
    selected_port = port or _find_free_tcp_port(host)
    endpoint_url = f"http://{_url_host(host)}:{selected_port}/mcp"
    profile_started_at = time.perf_counter()
    server_args = [
        str(server_script),
        "--data-dir",
        str(data_dir),
        "--tenant-id",
        tenant_id,
        "--tool-profile",
        tool_profile,
        "--transport",
        "streamable-http",
        "--host",
        host,
        "--port",
        str(selected_port),
    ]
    if profile_id:
        server_args.extend(["--profile-id", profile_id])
    if tenant_storage_isolation:
        server_args.append("--tenant-storage-isolation")
    else:
        server_args.append("--flat-storage")
    if no_warm_cache:
        server_args.append("--no-warm-cache")
    process = subprocess.Popen(
        [sys.executable, *server_args],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        _wait_for_tcp_port(host, selected_port, process, timeout_seconds=startup_timeout_seconds)
        async with streamable_http_client(endpoint_url) as (read, write, get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                profile = await _call_profile_tools(
                    session,
                    tool_profile=tool_profile,
                    profile_id=profile_id,
                    query=query,
                    no_warm_cache=no_warm_cache,
                    profile_started_at=profile_started_at,
                )
                profile["server_url"] = endpoint_url
                profile["server_port"] = selected_port
                profile["session_id_present"] = bool(get_session_id())
                return profile
    finally:
        _terminate_process(process)


async def _call_profile_tools(
    session: ClientSession,
    *,
    tool_profile: str,
    profile_id: str | None,
    query: str,
    no_warm_cache: bool,
    profile_started_at: float,
) -> dict[str, Any]:
    list_tools_started_at = time.perf_counter()
    tool_result = await session.list_tools()
    list_tools_elapsed_ms = _elapsed_ms(list_tools_started_at)
    tool_names = sorted(tool.name for tool in tool_result.tools)
    index_status_payload: dict[str, Any] = {}
    index_status_elapsed_ms = 0.0
    index_status_verified = False
    if "get_index_status" in tool_names:
        index_status_started_at = time.perf_counter()
        index_status = await session.call_tool(
            "get_index_status",
            {"security_levels": ["internal"]},
        )
        index_status_elapsed_ms = _elapsed_ms(index_status_started_at)
        index_status_payload = _tool_payload(index_status)
        index_status_verified = isinstance(index_status_payload.get("summary"), dict)
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
    search_metadata = search_payload.get("metadata") if isinstance(search_payload.get("metadata"), dict) else {}
    first_id = str((results[0] if results else {}).get("id") or "")
    fetch_payload: dict[str, Any] = {}
    fetch_elapsed_ms = 0.0
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
    history_payload: dict[str, Any] = {}
    history_error = ""
    history_attempted = False
    history_tool_available = "get_regulation_history" in tool_names
    regulation_id = str(
        ((results[0] if results else {}).get("metadata") or {}).get("regulation_id") or ""
    ).strip()
    if tool_profile == "full" and history_tool_available and regulation_id:
        history_attempted = True
        try:
            history = await session.call_tool(
                "get_regulation_history",
                {
                    "regulation_id": regulation_id,
                    **({"profile_id": profile_id} if profile_id else {}),
                },
            )
            history_payload = _tool_payload(history)
        except Exception as exc:
            history_error = str(exc)
    first_result_metadata = (results[0] if results else {}).get("metadata") or {}
    history_versions = history_payload.get("versions") if isinstance(history_payload.get("versions"), list) else []
    history_current_document_id = str(history_payload.get("current_document_id") or "").strip()
    first_result_document_id = str(first_result_metadata.get("document_id") or "").strip()
    history_current_match = bool(
        history_attempted
        and history_current_document_id
        and first_result_document_id
        and history_current_document_id == first_result_document_id
    )
    history_has_superseded = any(
        str(version.get("regulation_status") or "").strip().casefold() == "superseded"
        for version in history_versions
        if isinstance(version, dict)
    )
    warm_search_started_at = time.perf_counter()
    warm_search = await session.call_tool(
        "search",
        {
            "query": query,
            "top_k": 3,
            "security_levels": ["internal"],
        },
    )
    warm_search_elapsed_ms = _elapsed_ms(warm_search_started_at)
    warm_search_payload = _tool_payload(warm_search)
    warm_results = (
        warm_search_payload.get("results")
        if isinstance(warm_search_payload.get("results"), list)
        else []
    )
    expected_tools = (
        {"search", "fetch"}
        if tool_profile == "chatgpt-data"
        else {"search", "fetch", "list_documents", "get_index_status"}
    )
    return {
        "passed": bool(
            expected_tools.issubset(set(tool_names))
            and (tool_profile == "chatgpt-data" or index_status_verified)
            and results
            and fetch_payload.get("text")
        ),
        "process_started": True,
        "mcp_initialized": True,
        "tools_discovered": bool(tool_names),
        "index_status_verified": index_status_verified,
        "end_to_end_verified": bool(
            expected_tools.issubset(set(tool_names))
            and (tool_profile == "chatgpt-data" or index_status_verified)
            and results
            and fetch_payload.get("text")
        ),
        "tool_profile": tool_profile,
        "tool_names": tool_names,
        "query": query,
        "no_warm_cache": no_warm_cache,
        "search_result_count": len(results),
        "warm_search_result_count": len(warm_results),
        "fetch_has_text": bool(fetch_payload.get("text")),
        "history_tool_available": history_tool_available,
        "history_attempted": history_attempted,
        "history_passed": bool(history_versions) and history_current_match if history_attempted else False,
        "history_version_count": len(history_versions),
        "history_current_document_id": history_current_document_id,
        "history_current_match": history_current_match,
        "history_has_superseded": history_has_superseded,
        "history_error": history_error,
        "first_result_metadata": first_result_metadata,
        "search_metadata": search_metadata,
        "list_tools_elapsed_ms": list_tools_elapsed_ms,
        "index_status_elapsed_ms": index_status_elapsed_ms,
        "index_status_summary": index_status_payload.get("summary") or {},
        "search_elapsed_ms": search_elapsed_ms,
        "fetch_elapsed_ms": fetch_elapsed_ms,
        "warm_search_elapsed_ms": warm_search_elapsed_ms,
        "total_elapsed_ms": _elapsed_ms(profile_started_at),
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


def _find_free_tcp_port(host: str) -> int:
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _wait_for_tcp_port(
    host: str,
    port: int,
    process: subprocess.Popen[str],
    *,
    timeout_seconds: float,
) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if process.poll() is not None:
            output = _read_process_output(process)
            raise RuntimeError(f"MCP streamable-http server exited with code {process.returncode}: {output}")
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"MCP streamable-http server did not listen on {host}:{port} within {timeout_seconds:.1f}s")


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    if process.stdout is not None:
        process.stdout.close()


def _read_process_output(process: subprocess.Popen[str]) -> str:
    if process.stdout is None:
        return ""
    try:
        return process.stdout.read()[-4000:]
    except OSError:
        return ""


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a real MCP client/server transport smoke.")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--tenant-id", default="tenant-mcp-transport-smoke")
    parser.add_argument("--profile-id", default=None)
    parser.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--skip-preparation", action="store_true")
    parser.add_argument("--query", default=DEFAULT_SEARCH_QUERY)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--fail-on-issue", action="store_true")
    parser.add_argument("--allow-persistent-smoke-data", action="store_true")
    parser.add_argument("--no-warm-cache", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    if stdout is sys.stdout and hasattr(stdout, "reconfigure"):
        stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    report = run_mcp_transport_smoke(
        data_dir=Path(args.data_dir) if args.data_dir else None,
        tenant_id=args.tenant_id,
        profile_id=args.profile_id,
        tenant_storage_isolation=not args.flat_storage,
        transport=args.transport,
        host=args.host,
        port=args.port,
        out_json=Path(args.out_json) if args.out_json else None,
        timeout_seconds=args.timeout_seconds,
        prepare=not args.skip_preparation,
        query=args.query,
        allow_persistent_smoke_data=args.allow_persistent_smoke_data,
        no_warm_cache=args.no_warm_cache,
    )
    stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if args.fail_on_issue and not report["passed"]:
        return 2
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
