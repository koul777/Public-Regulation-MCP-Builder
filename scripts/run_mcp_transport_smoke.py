from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import streamable_http_client


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_mcp_smoke import run_mcp_smoke
from scripts.report_metadata import (
    capture_mcp_performance_source_state,
    current_repo_commit,
    finalize_mcp_performance_source_state,
)


DEFAULT_SEARCH_QUERY = "Article"
TEMPORAL_REGULATION_TITLE = "MCP Smoke Regulation"
TEMPORAL_REGULATION_ID = "reg_mcp_smoke"
TEMPORAL_CURRENT_DOCUMENT_ID = "doc_mcp_smoke_v2"
TEMPORAL_AS_OF_CASES: tuple[dict[str, str | None], ...] = (
    {
        "label": "v1",
        "as_of_date": "2025-06-01",
        "expected_document_id": "doc_mcp_smoke_v1",
        "expected_regulation_version": "1.0",
        "expected_effective_from": "2025-01-01",
        "expected_effective_to": "2025-12-31",
    },
    {
        "label": "v2",
        "as_of_date": "2026-06-01",
        "expected_document_id": "doc_mcp_smoke_v2",
        "expected_regulation_version": "2.0",
        "expected_effective_from": "2026-01-01",
        "expected_effective_to": None,
    },
)


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
    http_bearer_token: str | None = None,
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
                http_bearer_token=http_bearer_token,
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
        http_bearer_token=http_bearer_token,
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
    http_bearer_token: str | None,
    disposable_data_dir: bool,
) -> dict[str, Any]:
    started_source_state = capture_mcp_performance_source_state(PROJECT_ROOT)
    normalized_transport = transport.strip().lower()
    if normalized_transport not in {"stdio", "streamable-http"}:
        raise ValueError("transport must be stdio or streamable-http.")
    if http_bearer_token and normalized_transport != "streamable-http":
        raise ValueError("http_bearer_token is only supported with streamable-http transport.")
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
            source_state = finalize_mcp_performance_source_state(
                started_source_state,
                PROJECT_ROOT,
            )
            report = _preparation_failure_report(
                tenant_id=tenant_id,
                profile_id=profile_id,
                tenant_storage_isolation=tenant_storage_isolation,
                query=query,
                error=str(exc),
                transport=normalized_transport,
                persistent_smoke_data_opt_in=allow_persistent_smoke_data,
                http_bearer_token=http_bearer_token,
                source_state=source_state,
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
                http_bearer_token=http_bearer_token,
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
    source_state = finalize_mcp_performance_source_state(
        started_source_state,
        PROJECT_ROOT,
    )
    report = {
        "report_type": "mcp_transport_smoke",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "source_state": source_state,
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
            and (not prepare or full_profile.get("as_of_date_verification_passed"))
            and set(chatgpt_profile.get("tool_names") or [])
            == {
                "search",
                "fetch",
                "list_regulations",
                "get_regulation_toc",
                "get_regulation_article",
                "get_regulation_references",
                "list_regulation_reference_cycles",
            }
            and (
                normalized_transport != "streamable-http"
                or not http_bearer_token
                or full_profile.get("auth_wire_verified")
            )
        ),
        "process_started": bool(full_profile.get("process_started")),
        "mcp_initialized": bool(full_profile.get("mcp_initialized")),
        "tools_discovered": bool(full_profile.get("tools_discovered")),
        "end_to_end_verified": bool(full_profile.get("end_to_end_verified")),
        "as_of_date_verification_passed": full_profile.get("as_of_date_verification_passed"),
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
        "http_auth": {
            "configured": bool(http_bearer_token) if normalized_transport == "streamable-http" else False,
            "wire_verified": bool(
                normalized_transport != "streamable-http"
                or not http_bearer_token
                or full_profile.get("auth_wire_verified")
            ),
        },
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
    http_bearer_token: str | None,
    source_state: dict[str, Any],
) -> dict[str, Any]:
    return {
        "report_type": "mcp_transport_smoke",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "source_state": source_state,
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
        "http_auth": {
            "configured": bool(http_bearer_token) if transport == "streamable-http" else False,
            "wire_verified": False if http_bearer_token and transport == "streamable-http" else True,
        },
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
    http_bearer_token: str | None,
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
        http_bearer_token=http_bearer_token,
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
        http_bearer_token=http_bearer_token,
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
    stdio_env = _transport_smoke_server_env(get_default_environment())
    params = StdioServerParameters(
        command=sys.executable,
        args=server_args,
        env=stdio_env,
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
    http_bearer_token: str | None,
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
    process_env = _transport_smoke_server_env(os.environ.copy())
    if http_bearer_token:
        process_env["MCP_TRANSPORT_SMOKE_TOKEN"] = http_bearer_token
        server_args.extend(["--http-bearer-token-env", "MCP_TRANSPORT_SMOKE_TOKEN"])
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as process_output:
        process = subprocess.Popen(
            [sys.executable, *server_args],
            cwd=str(PROJECT_ROOT),
            env=process_env,
            stdout=process_output,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            _wait_for_tcp_port(
                host,
                selected_port,
                process,
                timeout_seconds=startup_timeout_seconds,
                process_output=process_output,
            )
            auth_wire_verified = True
            if http_bearer_token:
                async with httpx.AsyncClient(timeout=startup_timeout_seconds) as unauthenticated_client:
                    unauthorized = await unauthenticated_client.get(
                        endpoint_url,
                        headers={"Accept": "application/json, text/event-stream"},
                    )
                auth_wire_verified = unauthorized.status_code == 401
                http_client = httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {http_bearer_token}"},
                    timeout=startup_timeout_seconds,
                )
            else:
                http_client = None
            try:
                client_kwargs = {"http_client": http_client} if http_client is not None else {}
                async with streamable_http_client(endpoint_url, **client_kwargs) as (read, write, get_session_id):
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
                        profile["auth_wire_verified"] = auth_wire_verified
                        return profile
            finally:
                if http_client is not None:
                    await http_client.aclose()
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
    list_regulations_payload: dict[str, Any] = {}
    list_regulations_elapsed_ms = 0.0
    list_regulations_result_count = 0
    list_regulations_total_count = 0
    first_catalog_unit_id = ""
    hierarchy_payload: dict[str, Any] = {}
    hierarchy_elapsed_ms = 0.0
    hierarchy_verified = False
    exact_article_payload: dict[str, Any] = {}
    exact_article_elapsed_ms = 0.0
    exact_article_verified = False
    exact_article_regulation_id = ""
    exact_article_document_id = ""
    first_article_no = ""
    reference_lookup_payload: dict[str, Any] = {}
    reference_lookup_elapsed_ms = 0.0
    reference_lookup_verified = False
    reference_lookup_attempted = False
    reference_lookup_result_count = 0
    reference_lookup_cycle_count = 0
    reference_cycle_lookup_payload: dict[str, Any] = {}
    reference_cycle_lookup_elapsed_ms = 0.0
    reference_cycle_lookup_verified = False
    reference_cycle_lookup_attempted = False
    reference_cycle_result_count = 0
    catalog_rows: list[Any] = []
    if "list_regulations" in tool_names:
        list_regulations_started_at = time.perf_counter()
        list_regulations = await session.call_tool(
            "list_regulations",
            {"page": 1, "page_size": 100},
        )
        list_regulations_elapsed_ms = _elapsed_ms(list_regulations_started_at)
        list_regulations_payload = _tool_payload(list_regulations)
        catalog_rows = (
            list_regulations_payload.get("regulations")
            if isinstance(list_regulations_payload.get("regulations"), list)
            else []
        )
        list_regulations_result_count = len(catalog_rows)
        list_regulations_total_count = int(list_regulations_payload.get("total_count") or 0)
        first_catalog_row = next(
            (
                row
                for row in catalog_rows
                if tool_profile == "full"
                and isinstance(row, dict)
                and str(row.get("regulation_title") or "").strip() == TEMPORAL_REGULATION_TITLE
            ),
            catalog_rows[0] if catalog_rows else {},
        )
        first_catalog_unit_id = str(
            first_catalog_row.get("regulation_unit_id")
            if isinstance(first_catalog_row, dict)
            else ""
        ).strip()
        if "get_regulation_toc" in tool_names and first_catalog_unit_id:
            hierarchy_started_at = time.perf_counter()
            hierarchy = await session.call_tool(
                "get_regulation_toc",
                {"regulation_unit_id": first_catalog_unit_id},
            )
            hierarchy_elapsed_ms = _elapsed_ms(hierarchy_started_at)
            hierarchy_payload = _tool_payload(hierarchy)
            hierarchy_verified = bool(
                isinstance(hierarchy_payload.get("regulation"), dict)
                and isinstance(hierarchy_payload.get("nodes"), list)
            )
            hierarchy_nodes = (
                hierarchy_payload.get("nodes")
                if isinstance(hierarchy_payload.get("nodes"), list)
                else []
            )
            article_number_candidates = _article_number_candidates(hierarchy_nodes)
            if article_number_candidates:
                first_article_no = article_number_candidates[0]
            if "get_regulation_article" in tool_names and article_number_candidates:
                exact_article_started_at = time.perf_counter()
                for article_no in article_number_candidates:
                    exact_article = await session.call_tool(
                        "get_regulation_article",
                        {
                            "regulation_unit_id": first_catalog_unit_id,
                            "article_no": article_no,
                        },
                    )
                    exact_article_payload = _tool_payload(exact_article)
                    exact_articles = (
                        exact_article_payload.get("articles")
                        if isinstance(exact_article_payload.get("articles"), list)
                        else []
                    )
                    first_exact_article = _first_textual_article(exact_articles) or next(
                        (item for item in exact_articles if isinstance(item, dict)),
                        {},
                    )
                    first_exact_article_metadata = (
                        first_exact_article.get("metadata")
                        if isinstance(first_exact_article.get("metadata"), dict)
                        else {}
                    )
                    first_exact_article_verbatim = (
                        first_exact_article.get("verbatim")
                        if isinstance(first_exact_article.get("verbatim"), dict)
                        else {}
                    )
                    exact_article_verified = bool(
                        str(first_exact_article.get("text") or "").strip()
                    )
                    exact_article_regulation_id = str(
                        first_exact_article_metadata.get("regulation_id")
                        or first_exact_article_verbatim.get("regulation_id")
                        or ""
                    ).strip()
                    exact_article_document_id = str(
                        first_exact_article_metadata.get("document_id")
                        or first_exact_article_verbatim.get("document_id")
                        or ""
                    ).strip()
                    if exact_article_verified:
                        first_article_no = article_no
                        break
                exact_article_elapsed_ms = _elapsed_ms(exact_article_started_at)
        if "get_regulation_references" in tool_names and first_catalog_unit_id:
            reference_lookup_attempted = True
            reference_lookup_started_at = time.perf_counter()
            reference_lookup = await session.call_tool(
                "get_regulation_references",
                {"regulation_unit_id": first_catalog_unit_id, "page": 1, "page_size": 50},
            )
            reference_lookup_elapsed_ms = _elapsed_ms(reference_lookup_started_at)
            reference_lookup_payload = _tool_payload(reference_lookup)
            reference_lookup_verified = _valid_reference_lookup_payload(reference_lookup_payload)
            reference_lookup_result_count = len(_payload_list(reference_lookup_payload, "references"))
            reference_lookup_cycle_count = len(_payload_list(reference_lookup_payload, "cycles"))
        if "list_regulation_reference_cycles" in tool_names:
            reference_cycle_lookup_attempted = True
            reference_cycle_lookup_started_at = time.perf_counter()
            reference_cycle_args: dict[str, Any] = {"page": 1, "page_size": 50}
            if first_catalog_unit_id:
                reference_cycle_args["regulation_unit_id"] = first_catalog_unit_id
            reference_cycle_lookup = await session.call_tool(
                "list_regulation_reference_cycles",
                reference_cycle_args,
            )
            reference_cycle_lookup_elapsed_ms = _elapsed_ms(reference_cycle_lookup_started_at)
            reference_cycle_lookup_payload = _tool_payload(reference_cycle_lookup)
            reference_cycle_lookup_verified = _valid_reference_cycle_payload(reference_cycle_lookup_payload)
            reference_cycle_result_count = len(_payload_list(reference_cycle_lookup_payload, "cycles"))
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
        {"query": query},
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
            {"id": first_id},
        )
        fetch_elapsed_ms = _elapsed_ms(fetch_started_at)
        fetch_payload = _tool_payload(fetch)
    history_payload: dict[str, Any] = {}
    history_error = ""
    history_attempted = False
    history_tool_available = "get_regulation_history" in tool_names
    as_of_date_verification = _unavailable_as_of_date_verification(tool_profile=tool_profile)
    first_result_metadata = (results[0] if results else {}).get("metadata") or {}
    history_regulation_id = exact_article_regulation_id or str(first_result_metadata.get("regulation_id") or "").strip()
    if tool_profile == "full":
        temporal_fixture_available = bool(
            history_tool_available
            and exact_article_regulation_id == TEMPORAL_REGULATION_ID
            and exact_article_document_id == TEMPORAL_CURRENT_DOCUMENT_ID
            and first_catalog_unit_id
            and first_article_no
        )
        if temporal_fixture_available:
            as_of_date_verification, history_payload = await _run_as_of_date_verification(
                session,
                regulation_id=exact_article_regulation_id,
                regulation_unit_id=first_catalog_unit_id,
                article_no=first_article_no,
                profile_id=profile_id,
            )
            history_attempted = bool(as_of_date_verification.get("attempted"))
            history_error = str(as_of_date_verification.get("error") or "")
        elif history_tool_available and history_regulation_id:
            history_attempted = True
            try:
                history = await session.call_tool(
                    "get_regulation_history",
                    {
                        "regulation_id": history_regulation_id,
                        **({"profile_id": profile_id} if profile_id else {}),
                    },
                )
                history_payload = _tool_payload(history)
            except Exception as exc:
                history_error = str(exc)
    history_versions = history_payload.get("versions") if isinstance(history_payload.get("versions"), list) else []
    history_current_document_id = str(history_payload.get("current_document_id") or "").strip()
    first_result_document_id = str(first_result_metadata.get("document_id") or "").strip()
    history_current_match_target = exact_article_document_id or first_result_document_id
    history_current_match = bool(
        history_attempted
        and history_current_document_id
        and history_current_match_target
        and history_current_document_id == history_current_match_target
    )
    history_has_superseded = any(
        str(version.get("regulation_status") or "").strip().casefold() == "superseded"
        for version in history_versions
        if isinstance(version, dict)
    )
    warm_search_started_at = time.perf_counter()
    warm_search = await session.call_tool(
        "search",
        {"query": query},
    )
    warm_search_elapsed_ms = _elapsed_ms(warm_search_started_at)
    warm_search_payload = _tool_payload(warm_search)
    warm_results = (
        warm_search_payload.get("results")
        if isinstance(warm_search_payload.get("results"), list)
        else []
    )
    expected_tools = (
        {
            "search",
            "fetch",
            "list_regulations",
            "get_regulation_toc",
            "get_regulation_article",
            "get_regulation_references",
            "list_regulation_reference_cycles",
        }
        if tool_profile == "chatgpt-data"
        else {
            "search",
            "fetch",
            "list_documents",
            "list_regulations",
            "get_regulation_toc",
            "get_regulation_article",
            "get_regulation_references",
            "list_regulation_reference_cycles",
            "get_index_status",
        }
    )
    catalog_verified = list_regulations_result_count > 0 and list_regulations_total_count >= list_regulations_result_count
    return {
        "passed": bool(
            expected_tools.issubset(set(tool_names))
            and (tool_profile == "chatgpt-data" or index_status_verified)
            and (
                tool_profile == "chatgpt-data"
                or not as_of_date_verification.get("applicable")
                or as_of_date_verification.get("passed")
            )
            and catalog_verified
            and hierarchy_verified
            and exact_article_verified
            and reference_lookup_verified
            and reference_cycle_lookup_verified
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
            and (
                tool_profile == "chatgpt-data"
                or not as_of_date_verification.get("applicable")
                or as_of_date_verification.get("passed")
            )
            and catalog_verified
            and hierarchy_verified
            and exact_article_verified
            and reference_lookup_verified
            and reference_cycle_lookup_verified
            and results
            and fetch_payload.get("text")
        ),
        "tool_profile": tool_profile,
        "tool_names": tool_names,
        "list_regulations_result_count": list_regulations_result_count,
        "list_regulations_total_count": list_regulations_total_count,
        "list_regulations_elapsed_ms": list_regulations_elapsed_ms,
        "hierarchy_verified": hierarchy_verified,
        "hierarchy_elapsed_ms": hierarchy_elapsed_ms,
        "exact_article_verified": exact_article_verified,
        "exact_article_elapsed_ms": exact_article_elapsed_ms,
        "reference_lookup_attempted": reference_lookup_attempted,
        "reference_lookup_verified": reference_lookup_verified,
        "reference_lookup_result_count": reference_lookup_result_count,
        "reference_lookup_cycle_count": reference_lookup_cycle_count,
        "reference_lookup_elapsed_ms": reference_lookup_elapsed_ms,
        "reference_cycle_lookup_attempted": reference_cycle_lookup_attempted,
        "reference_cycle_lookup_verified": reference_cycle_lookup_verified,
        "reference_cycle_result_count": reference_cycle_result_count,
        "reference_cycle_lookup_elapsed_ms": reference_cycle_lookup_elapsed_ms,
        "catalog_verified": catalog_verified,
        "query": query,
        "no_warm_cache": no_warm_cache,
        "search_result_count": len(results),
        "warm_search_result_count": len(warm_results),
        "fetch_has_text": bool(fetch_payload.get("text")),
        "history_tool_available": history_tool_available,
        "history_attempted": history_attempted,
        "history_passed": bool(
            history_attempted
            and history_versions
            and history_current_match
            and (
                not as_of_date_verification.get("applicable")
                or as_of_date_verification.get("passed")
            )
        ),
        "history_version_count": len(history_versions),
        "history_current_document_id": history_current_document_id,
        "history_current_match": history_current_match,
        "history_current_match_target_document_id": history_current_match_target,
        "history_has_superseded": history_has_superseded,
        "history_error": history_error,
        "as_of_date_verification_passed": as_of_date_verification.get("passed"),
        "as_of_date_verification": as_of_date_verification,
        "reference_lookup_metadata": reference_lookup_payload.get("metadata") or {},
        "reference_cycle_lookup_metadata": reference_cycle_lookup_payload.get("metadata") or {},
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


def _unavailable_as_of_date_verification(*, tool_profile: str) -> dict[str, Any]:
    return {
        "applicable": False,
        "attempted": False,
        "passed": None,
        "skipped_reason": (
            "The deterministic synthetic temporal fixture is not available."
            if tool_profile == "full"
            else "The chatgpt-data profile intentionally has no temporal verification extension."
        ),
        "regulation_id": "",
        "regulation_unit_id": "",
        "article_no": "",
        "as_of_dates": [str(case["as_of_date"]) for case in TEMPORAL_AS_OF_CASES],
        "cases": [],
        "selected_document_ids_differ": False,
        "history_versions_differ": False,
        "toc_versions_differ": False,
        "article_document_ids_differ": False,
        "article_versions_differ": False,
        "article_texts_differ": False,
        "history_total_elapsed_ms": 0.0,
        "toc_total_elapsed_ms": 0.0,
        "article_total_elapsed_ms": 0.0,
        "total_elapsed_ms": 0.0,
        "error": "",
    }


async def _run_as_of_date_verification(
    session: ClientSession,
    *,
    regulation_id: str,
    regulation_unit_id: str,
    article_no: str,
    profile_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    started_at = time.perf_counter()
    cases: list[dict[str, Any]] = []
    latest_history_payload: dict[str, Any] = {}
    profile_arguments = {"profile_id": profile_id} if profile_id else {}
    for case_spec in TEMPORAL_AS_OF_CASES:
        as_of_date = str(case_spec["as_of_date"])
        common_unit_arguments = {
            "regulation_unit_id": regulation_unit_id,
            "as_of_date": as_of_date,
            **profile_arguments,
        }
        tool_calls = (
            (
                "history",
                "get_regulation_history",
                {"regulation_id": regulation_id, "as_of_date": as_of_date, **profile_arguments},
            ),
            ("toc", "get_regulation_toc", common_unit_arguments),
            (
                "article",
                "get_regulation_article",
                {**common_unit_arguments, "article_no": article_no},
            ),
        )
        payloads: dict[str, dict[str, Any]] = {}
        timings: dict[str, float] = {}
        call_errors: list[str] = []
        for key, tool_name, arguments in tool_calls:
            payloads[key], timings[key], call_error = await _timed_tool_call(session, tool_name, arguments)
            if call_error:
                call_errors.append(f"{tool_name}: {call_error}")
        case_result = _validate_temporal_case_payloads(
            case_spec=case_spec,
            regulation_id=regulation_id,
            regulation_unit_id=regulation_unit_id,
            article_no=article_no,
            history_payload=payloads["history"],
            toc_payload=payloads["toc"],
            article_payload=payloads["article"],
        )
        if call_errors:
            case_result["errors"] = [*case_result["errors"], *call_errors]
            case_result["passed"] = False
        case_result.update(
            {
                "history_elapsed_ms": timings["history"],
                "toc_elapsed_ms": timings["toc"],
                "article_elapsed_ms": timings["article"],
            }
        )
        cases.append(case_result)
        if case_spec["label"] == "v2":
            latest_history_payload = payloads["history"]

    selected_document_ids = [str(case.get("history_current_document_id") or "") for case in cases]
    history_versions = [str(case.get("history_regulation_version") or "") for case in cases]
    toc_versions = [str(case.get("toc_version") or "") for case in cases]
    article_document_ids = [str(case.get("article_document_id") or "") for case in cases]
    article_versions = [str(case.get("article_regulation_version") or "") for case in cases]
    article_text_hashes = [str(case.get("article_text_sha256") or "") for case in cases]
    comparison_checks = {
        "selected_document_ids_differ": _two_nonempty_values_differ(selected_document_ids),
        "history_versions_differ": _two_nonempty_values_differ(history_versions),
        "toc_versions_differ": _two_nonempty_values_differ(toc_versions),
        "article_document_ids_differ": _two_nonempty_values_differ(article_document_ids),
        "article_versions_differ": _two_nonempty_values_differ(article_versions),
        "article_texts_differ": _two_nonempty_values_differ(article_text_hashes),
    }
    errors = [
        f"{case.get('label')} ({case.get('as_of_date')}): {', '.join(case.get('errors') or [])}"
        for case in cases
        if case.get("errors")
    ]
    failed_comparisons = [name for name, passed in comparison_checks.items() if not passed]
    if failed_comparisons:
        errors.append("Temporal comparison checks failed: " + ", ".join(failed_comparisons))
    report = {
        "applicable": True,
        "attempted": True,
        "skipped_reason": "",
        "passed": bool(
            len(cases) == len(TEMPORAL_AS_OF_CASES)
            and all(case.get("passed") for case in cases)
            and all(comparison_checks.values())
        ),
        "regulation_id": regulation_id,
        "regulation_unit_id": regulation_unit_id,
        "article_no": article_no,
        "as_of_dates": [str(case["as_of_date"]) for case in TEMPORAL_AS_OF_CASES],
        "cases": cases,
        **comparison_checks,
        "history_total_elapsed_ms": round(sum(float(case["history_elapsed_ms"]) for case in cases), 3),
        "toc_total_elapsed_ms": round(sum(float(case["toc_elapsed_ms"]) for case in cases), 3),
        "article_total_elapsed_ms": round(sum(float(case["article_elapsed_ms"]) for case in cases), 3),
        "total_elapsed_ms": _elapsed_ms(started_at),
        "error": "; ".join(errors),
    }
    return report, latest_history_payload


async def _timed_tool_call(
    session: ClientSession,
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], float, str]:
    started_at = time.perf_counter()
    try:
        result = await session.call_tool(tool_name, arguments)
        return _tool_payload(result), _elapsed_ms(started_at), ""
    except Exception as exc:
        return {}, _elapsed_ms(started_at), str(exc)


def _validate_temporal_case_payloads(
    *,
    case_spec: dict[str, str | None],
    regulation_id: str,
    regulation_unit_id: str,
    article_no: str,
    history_payload: dict[str, Any],
    toc_payload: dict[str, Any],
    article_payload: dict[str, Any],
) -> dict[str, Any]:
    label = str(case_spec.get("label") or "")
    as_of_date = str(case_spec.get("as_of_date") or "")
    expected_document_id = str(case_spec.get("expected_document_id") or "")
    expected_regulation_version = str(case_spec.get("expected_regulation_version") or "")
    expected_effective_from = str(case_spec.get("expected_effective_from") or "")
    expected_effective_to = case_spec.get("expected_effective_to")
    errors: list[str] = []

    raw_history_versions = history_payload.get("versions")
    history_versions = _payload_list(history_payload, "versions")
    history_current_document_id = str(history_payload.get("current_document_id") or "").strip()
    effective_history_versions = [
        version for version in history_versions if version.get("is_effective_on_as_of") is True
    ]
    current_history_versions = [version for version in history_versions if version.get("is_current") is True]
    selected_history_version = next(
        (
            version
            for version in history_versions
            if str(version.get("document_id") or "").strip() == history_current_document_id
        ),
        {},
    )
    history_regulation_version = str(selected_history_version.get("regulation_version") or "").strip()
    history_verified = bool(
        isinstance(history_payload, dict)
        and str(history_payload.get("regulation_id") or "").strip() == regulation_id
        and str(history_payload.get("as_of_date") or "").strip() == as_of_date
        and isinstance(raw_history_versions, list)
        and bool(raw_history_versions)
        and len(history_versions) == len(raw_history_versions)
        and history_current_document_id == expected_document_id
        and len(effective_history_versions) == 1
        and str(effective_history_versions[0].get("document_id") or "").strip() == expected_document_id
        and len(current_history_versions) == 1
        and str(current_history_versions[0].get("document_id") or "").strip() == expected_document_id
        and history_regulation_version == expected_regulation_version
        and str(selected_history_version.get("effective_from") or "").strip() == expected_effective_from
        and _optional_temporal_value_matches(
            selected_history_version.get("effective_to"),
            expected_effective_to,
        )
    )
    if not history_verified:
        errors.append("history payload did not select the expected effective version")

    toc_regulation = toc_payload.get("regulation")
    toc_metadata = toc_payload.get("metadata")
    raw_toc_nodes = toc_payload.get("nodes")
    toc_version = (
        str(toc_regulation.get("version") or "").strip() if isinstance(toc_regulation, dict) else ""
    )
    toc_verified = bool(
        isinstance(toc_regulation, dict)
        and str(toc_regulation.get("regulation_unit_id") or "").strip() == regulation_unit_id
        and toc_version
        and str(toc_regulation.get("effective_from") or "").strip() == expected_effective_from
        and _optional_temporal_value_matches(toc_regulation.get("effective_to"), expected_effective_to)
        and isinstance(raw_toc_nodes, list)
        and bool(raw_toc_nodes)
        and all(isinstance(node, dict) for node in raw_toc_nodes)
        and isinstance(toc_metadata, dict)
        and toc_metadata.get("hierarchical_index_ready") is True
        and str(toc_metadata.get("as_of_date") or "").strip() == as_of_date
    )
    if not toc_verified:
        errors.append("TOC payload did not resolve the expected dated regulation unit")

    raw_articles = article_payload.get("articles")
    articles = _payload_list(article_payload, "articles")
    matching_articles = []
    for article in articles:
        metadata = article.get("metadata") if isinstance(article.get("metadata"), dict) else {}
        verbatim = article.get("verbatim") if isinstance(article.get("verbatim"), dict) else {}
        text = str(article.get("text") or "").strip()
        if (
            text
            and str(article.get("verbatim_text") or "").strip() == text
            and str(verbatim.get("document_id") or "").strip() == expected_document_id
            and str(verbatim.get("text") or "").strip() == text
            and str(metadata.get("document_id") or "").strip() == expected_document_id
            and str(metadata.get("regulation_version") or "").strip() == expected_regulation_version
            and str(metadata.get("effective_from") or "").strip() == expected_effective_from
            and _optional_temporal_value_matches(metadata.get("effective_to"), expected_effective_to)
        ):
            matching_articles.append(article)
    selected_article = matching_articles[0] if matching_articles else {}
    article_metadata = (
        selected_article.get("metadata") if isinstance(selected_article.get("metadata"), dict) else {}
    )
    article_text = "\n".join(
        str(article.get("text") or "").strip()
        for article in matching_articles
        if str(article.get("text") or "").strip()
    )
    article_document_id = str(article_metadata.get("document_id") or "").strip()
    article_regulation_version = str(article_metadata.get("regulation_version") or "").strip()
    article_verified = bool(
        str(article_payload.get("regulation_unit_id") or "").strip() == regulation_unit_id
        and str(article_payload.get("article_no") or "").strip() == article_no
        and str(article_payload.get("as_of_date") or "").strip() == as_of_date
        and isinstance(raw_articles, list)
        and bool(raw_articles)
        and len(articles) == len(raw_articles)
        and len(matching_articles) == len(articles)
    )
    if not article_verified:
        errors.append("article payload did not return the expected dated document and text")

    return {
        "label": label,
        "as_of_date": as_of_date,
        "expected_document_id": expected_document_id,
        "expected_regulation_version": expected_regulation_version,
        "expected_effective_from": expected_effective_from,
        "expected_effective_to": expected_effective_to,
        "history_verified": history_verified,
        "history_current_document_id": history_current_document_id,
        "history_effective_document_ids": [
            str(version.get("document_id") or "").strip() for version in effective_history_versions
        ],
        "history_regulation_version": history_regulation_version,
        "toc_verified": toc_verified,
        "toc_regulation_unit_id": (
            str(toc_regulation.get("regulation_unit_id") or "").strip()
            if isinstance(toc_regulation, dict)
            else ""
        ),
        "toc_version": toc_version,
        "toc_effective_from": (
            str(toc_regulation.get("effective_from") or "").strip()
            if isinstance(toc_regulation, dict)
            else ""
        ),
        "toc_effective_to": (
            toc_regulation.get("effective_to") if isinstance(toc_regulation, dict) else None
        ),
        "article_verified": article_verified,
        "article_regulation_unit_id": str(article_payload.get("regulation_unit_id") or "").strip(),
        "article_document_id": article_document_id,
        "article_regulation_version": article_regulation_version,
        "article_text_length": len(article_text),
        "article_text_sha256": (
            hashlib.sha256(article_text.encode("utf-8")).hexdigest() if article_text else ""
        ),
        "passed": bool(history_verified and toc_verified and article_verified),
        "errors": errors,
    }


def _optional_temporal_value_matches(value: Any, expected: str | None) -> bool:
    normalized = str(value or "").strip()
    return normalized == (expected or "")


def _two_nonempty_values_differ(values: list[str]) -> bool:
    return len(values) == 2 and all(values) and values[0] != values[1]


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


def _payload_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _article_number_candidates(nodes: list[Any]) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        article_no = str(node.get("number") or node.get("label") or "").strip()
        if not article_no:
            continue
        if str(node.get("node_type") or "") != "article" and "조" not in article_no:
            continue
        if article_no in seen:
            continue
        seen.add(article_no)
        candidates.append(article_no)
    return candidates


def _first_textual_article(articles: list[Any]) -> dict[str, Any]:
    for article in articles:
        if isinstance(article, dict) and str(article.get("text") or "").strip():
            return article
    return {}


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized >= 1 else None


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized >= 0 else None


def _valid_reference_lookup_payload(payload: dict[str, Any]) -> bool:
    references = _payload_list(payload, "references")
    cycles = _payload_list(payload, "cycles")
    regulation = payload.get("regulation")
    metadata = payload.get("metadata")
    total_count = _non_negative_int(payload.get("total_count"))
    page = _positive_int(payload.get("page"))
    page_size = _positive_int(payload.get("page_size"))
    if (
        not isinstance(regulation, dict)
        or not str(regulation.get("regulation_unit_id") or "").strip()
        or not isinstance(payload.get("references"), list)
        or not isinstance(payload.get("cycles"), list)
        or not isinstance(metadata, dict)
        or total_count is None
        or page is None
        or page_size is None
        or total_count < len(references)
        or payload.get("next_cursor") is not None
        and not isinstance(payload.get("next_cursor"), str)
        or metadata.get("hierarchical_index_ready") is not True
        or str(metadata.get("direction") or "").strip().casefold() not in {"incoming", "outgoing", "both"}
    ):
        return False
    if references:
        first_reference = references[0]
        if (
            not str(first_reference.get("reference_id") or "").strip()
            or not isinstance(first_reference.get("candidate_regulations"), list)
            or not isinstance(first_reference.get("reason_codes"), list)
            or not isinstance(first_reference.get("match_types"), list)
        ):
            return False
    if cycles:
        first_cycle = cycles[0]
        if (
            not str(first_cycle.get("cycle_id") or "").strip()
            or _non_negative_int(first_cycle.get("size")) is None
            or not isinstance(first_cycle.get("regulations"), list)
        ):
            return False
    return True


def _valid_reference_cycle_payload(payload: dict[str, Any]) -> bool:
    cycles = _payload_list(payload, "cycles")
    metadata = payload.get("metadata")
    total_count = _non_negative_int(payload.get("total_count"))
    page = _positive_int(payload.get("page"))
    page_size = _positive_int(payload.get("page_size"))
    if (
        not isinstance(payload.get("cycles"), list)
        or not isinstance(metadata, dict)
        or total_count is None
        or page is None
        or page_size is None
        or total_count < len(cycles)
        or payload.get("next_cursor") is not None
        and not isinstance(payload.get("next_cursor"), str)
        or metadata.get("hierarchical_index_ready") is not True
    ):
        return False
    if cycles:
        first_cycle = cycles[0]
        if (
            not str(first_cycle.get("cycle_id") or "").strip()
            or _non_negative_int(first_cycle.get("size")) is None
            or not isinstance(first_cycle.get("regulations"), list)
        ):
            return False
    return True


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
    process_output: TextIO | None = None,
) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if process.poll() is not None:
            output = _read_process_output(process, process_output=process_output)
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


def _read_process_output(
    process: subprocess.Popen[str],
    *,
    process_output: TextIO | None = None,
) -> str:
    output = process_output or process.stdout
    if output is None:
        return ""
    try:
        output.flush()
        output.seek(0)
        return output.read()[-4000:]
    except OSError:
        return ""


def _transport_smoke_server_env(base_env: dict[str, str]) -> dict[str, str]:
    env = dict(base_env)
    configured_python_path = os.environ.get("PYTHONPATH")
    if configured_python_path:
        # mcp's safe default environment intentionally excludes PYTHONPATH. Keep
        # that default, but preserve an explicitly configured interpreter path so
        # smoke subprocesses use the same isolated dependency set as the caller.
        env["PYTHONPATH"] = configured_python_path
    # Persistent runtime-bundle smoke runs against read-only evidence data.
    # Disable write-on-read diagnostics so tool calls stay compatible.
    env["API_AUDIT_ENABLED"] = "false"
    env["RAG_TRACE_ENABLED"] = "false"
    return env


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
    parser.add_argument(
        "--http-bearer-token-env",
        default=None,
        help="Environment variable containing a bearer token for authenticated streamable-http smoke.",
    )
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
        http_bearer_token=os.getenv(args.http_bearer_token_env) if args.http_bearer_token_env else None,
    )
    stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if args.fail_on_issue and not report["passed"]:
        return 2
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
