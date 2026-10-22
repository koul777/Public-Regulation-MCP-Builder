"""Read a private runtime through a real stdio MCP process without approving data."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Any, Sequence, TextIO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.input_limits import MAX_MCP_TOP_K
from scripts.holdout_spec_guard import HoldoutSpecRefused, refuse_holdout_spec
from scripts.run_mcp_client_config_smoke import _exception_message, _valid_index_status_summary


def _private_paths(data_dir: Path, output_path: Path) -> tuple[Path, Path]:
    runtime = Path(data_dir).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    source = PROJECT_ROOT.resolve()
    if runtime.is_relative_to(source) or output.is_relative_to(source):
        raise ValueError("Runtime snapshots and probe reports must be outside the source repository.")
    if not runtime.is_dir():
        raise ValueError("A pre-existing private runtime snapshot directory is required.")
    if output.is_relative_to(runtime):
        raise ValueError("Probe reports must be outside the runtime snapshot.")
    if output.exists() or output.with_suffix(".stderr.log").exists():
        raise FileExistsError("Refusing to overwrite an existing probe report or stderr log.")
    return runtime, output


def _normalize_specs(query_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(query_specs, list) or not query_specs:
        raise ValueError("At least one query specification is required.")
    normalized: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for spec in query_specs:
        if not isinstance(spec, dict):
            raise ValueError("Each query specification must be an object.")
        spec = dict(spec)
        overrides = spec.get("request_overrides", {})
        allowed_overrides = {"profile_id", "document_id", "security_levels", "department_ids", "as_of_date", "top_k"}
        if not isinstance(overrides, dict) or set(overrides) - allowed_overrides:
            raise ValueError("request_overrides must contain only supported MCP search scope fields.")
        for key, value in overrides.items():
            if key in spec and spec[key] != value:
                raise ValueError(f"Conflicting top-level and request_overrides values for {key}.")
            spec[key] = value
        identifier = spec.get("id")
        query = spec.get("query", spec.get("question"))
        if not isinstance(identifier, str) or not identifier.strip() or identifier in identifiers:
            raise ValueError("Query identifiers must be non-empty and unique.")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Each query must be a non-empty string.")
        top_k = spec.get("top_k", 5)
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= MAX_MCP_TOP_K:
            raise ValueError(f"top_k must be an integer from 1 to {MAX_MCP_TOP_K}.")
        expect_empty = spec.get("expect_no_evidence", spec.get("expect_empty", False))
        if not isinstance(expect_empty, bool):
            raise ValueError("expect_no_evidence must be a boolean.")
        row = dict(spec, id=identifier, query=query, top_k=top_k, expect_no_evidence=expect_empty)
        for field, alias in (("target_document_ids", "expected_document_ids"), ("target_chunk_ids", "expected_chunk_ids")):
            values = spec.get(field, spec.get(alias, []))
            if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"{field} must be a list of non-empty strings.")
            row[field] = values
        if expect_empty and (row["target_document_ids"] or row["target_chunk_ids"]):
            raise ValueError("No-evidence queries cannot also require target evidence.")
        identifiers.add(identifier)
        normalized.append(row)
    return normalized


def _payload(result: Any, tool: str) -> dict[str, Any]:
    if bool(getattr(result, "isError", False)):
        detail = " ".join(str(getattr(block, "text", "")) for block in (getattr(result, "content", None) or []))
        raise ValueError(f"MCP {tool} returned isError=true: {detail}")
    payload = getattr(result, "structuredContent", None)
    if not isinstance(payload, dict):
        payload = None
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                try:
                    decoded = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(decoded, dict):
                    payload = decoded
                    break
    if not isinstance(payload, dict) or not payload:
        raise ValueError(f"MCP {tool} did not return a non-empty object payload.")
    return payload


def _elapsed(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _target_rank(results: list[dict[str, Any]], spec: dict[str, Any]) -> int | None:
    documents = set(spec["target_document_ids"])
    chunks = set(spec["target_chunk_ids"])
    for rank, result in enumerate(results, start=1):
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        if (not documents or metadata.get("document_id") in documents) and (not chunks or metadata.get("chunk_id") in chunks):
            return rank
    return None


async def _probe_case(
    session: Any, spec: dict[str, Any], *, case_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    case = case_report if case_report is not None else {}
    case.update({
        "id": spec["id"], "query": spec["query"], "expect_no_evidence": spec["expect_no_evidence"],
        "target_document_ids": spec["target_document_ids"], "target_chunk_ids": spec["target_chunk_ids"],
        "search": None, "fetch": [], "search_protocol_passed": False,
        "retrieval_passed": False, "target_match_rank": None, "errors": [], "completed": False, "passed": False,
    })
    search_args: dict[str, Any] = {"query": spec["query"], "top_k": spec["top_k"]}
    scope_args = {key: spec[key] for key in ("profile_id", "security_levels", "department_ids", "as_of_date") if spec.get(key) is not None}
    search_args.update(scope_args)
    if spec.get("document_id") is not None:
        search_args["document_id"] = spec["document_id"]
    search_started = time.perf_counter()
    try:
        search = _payload(await session.call_tool("search", search_args), "search")
        case["search"] = search
        results = search.get("results")
        if not isinstance(results, list) or any(not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip() for row in results):
            raise ValueError("MCP search returned a malformed results collection.")
        ids = [row["id"] for row in results]
        if len(ids) != len(set(ids)):
            raise ValueError("MCP search returned duplicate result identifiers.")
        case["search_protocol_passed"] = True
        case["search_duration_ms"] = _elapsed(search_started)
        case["result_count"] = len(results)
        rank = _target_rank(results, spec)
        case["target_match_rank"] = rank
        case["retrieval_passed"] = not results if spec["expect_no_evidence"] else rank is not None
        if not case["retrieval_passed"]:
            case["errors"].append("unexpected_evidence" if spec["expect_no_evidence"] else "target_evidence_not_retrieved")
        for result in results:
            fetch_started = time.perf_counter()
            fetched: dict[str, Any] = {"id": result["id"], "payload": None, "passed": False, "error": None}
            case["fetch"].append(fetched)
            try:
                payload = _payload(await session.call_tool("fetch", {"id": result["id"], **scope_args}), "fetch")
                fetched["payload"] = payload
                if payload.get("id") != result["id"]:
                    raise ValueError("MCP fetch returned a different result identifier.")
                if not isinstance(payload.get("text"), str) or not payload["text"].strip():
                    raise ValueError("MCP fetch returned no evidence text.")
                fetched["passed"] = True
            except asyncio.CancelledError:
                fetched["error"] = "MCP fetch cancelled before completion."
                raise
            except Exception as exc:
                fetched["error"] = _exception_message(exc)
                case["errors"].append(fetched["error"])
            finally:
                fetched["duration_ms"] = _elapsed(fetch_started)
        case["passed"] = bool(case["retrieval_passed"] and all(row["passed"] for row in case["fetch"]))
    except asyncio.CancelledError:
        case["errors"].append("MCP case cancelled before completion.")
        raise
    except Exception as exc:
        case["errors"].append(_exception_message(exc))
        case["search_duration_ms"] = _elapsed(search_started)
    finally:
        case["duration_ms"] = _elapsed(started)
    case["completed"] = True
    return case


async def _probe_session(session: Any, specs: list[dict[str, Any]], report: dict[str, Any]) -> None:
    checks = report["protocol_checks"]
    started = time.perf_counter()
    initialized = await session.initialize()
    checks["initialize"] = {"passed": True, "duration_ms": _elapsed(started), "protocol_version": getattr(initialized, "protocolVersion", None)}
    started = time.perf_counter()
    tools = await session.list_tools()
    names = sorted(tool.name for tool in tools.tools)
    checks["tools_list"] = {"passed": {"search", "fetch", "get_index_status"}.issubset(names), "names": names, "duration_ms": _elapsed(started)}
    if not checks["tools_list"]["passed"]:
        raise ValueError("MCP server does not expose all required probe tools.")
    started = time.perf_counter()
    status = _payload(await session.call_tool("get_index_status", {}), "get_index_status")
    summary = status.get("summary")
    valid_status = isinstance(summary, dict) and _valid_index_status_summary(summary)
    checks["get_index_status"] = {"passed": valid_status, "payload": status, "duration_ms": _elapsed(started)}
    if not valid_status:
        raise ValueError("MCP get_index_status returned an invalid summary.")
    for spec in specs:
        case: dict[str, Any] = {}
        report["cases"].append(case)
        await _probe_case(session, spec, case_report=case)


async def _run_stdio(report: dict[str, Any], specs: list[dict[str, Any]], stderr: TextIO) -> None:
    scope = report["runtime_scope"]
    args = [str(PROJECT_ROOT / "scripts" / "run_regulation_mcp.py"), "--data-dir", scope["data_dir"],
            "--tenant-id", scope["tenant_id"], "--transport", "stdio", "--tool-profile", "full", "--no-warm-cache",
            "--tenant-storage-isolation" if scope["tenant_storage_isolation"] else "--flat-storage"]
    if scope["profile_id"]:
        args.extend(["--profile-id", scope["profile_id"]])
    env = get_default_environment()
    env.update({"API_AUDIT_ENABLED": "false", "RAG_TRACE_ENABLED": "false", "PYTHONIOENCODING": "utf-8"})
    params = StdioServerParameters(command=sys.executable, args=args, env=env, cwd=str(PROJECT_ROOT))
    async with stdio_client(params, errlog=stderr) as (read, write):
        async with ClientSession(read, write) as session:
            await _probe_session(session, specs, report)


def run_mcp_probe(
    *, data_dir: Path, query_specs: list[dict[str, Any]], tenant_id: str,
    profile_id: str | None, tenant_storage_isolation: bool, output_path: Path,
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    """Probe an existing private snapshot; save raw evidence only outside source/runtime."""
    runtime, output = _private_paths(data_dir, output_path)
    specs = _normalize_specs(query_specs)
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id is required.")
    if not isinstance(tenant_storage_isolation, bool):
        raise ValueError("tenant_storage_isolation must be explicit and boolean.")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds < float("inf"):
        raise ValueError("timeout_seconds must be positive and finite.")
    started = time.perf_counter()
    manifest = runtime / "mcp_runtime_manifest.json"
    report: dict[str, Any] = {
        "report_type": "real_regulation_mcp_probe", "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(), "transport": "stdio",
        "runtime_scope": {"data_dir": str(runtime), "tenant_id": tenant_id, "profile_id": profile_id,
                          "tenant_storage_isolation": tenant_storage_isolation},
        "query_count": len(specs), "query_specs_sha256": hashlib.sha256(json.dumps(specs, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
        "protocol_checks": {name: {"passed": False} for name in ("initialize", "tools_list", "get_index_status")},
        "approval_review_status": "not_assessed_by_transport_probe",
        "provenance": {"approval_mutation": False, "synthetic_preparation": False,
                       "runtime_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.is_file() else None},
        "release_ready": False, "cases": [], "errors": [], "passed": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.with_suffix(".stderr.log").open("x", encoding="utf-8") as stderr:
        try:
            asyncio.run(asyncio.wait_for(_run_stdio(report, specs, stderr), timeout=timeout_seconds))
        except Exception as exc:
            report["errors"].append(_exception_message(exc))
    report["passed"] = bool(not report["errors"] and all(check["passed"] for check in report["protocol_checks"].values())
                            and len(report["cases"]) == len(specs) and all(case["passed"] for case in report["cases"]))
    report["duration_ms"] = _elapsed(started)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return report


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--query-spec", type=Path, required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--profile-id")
    storage = parser.add_mutually_exclusive_group(required=True)
    storage.add_argument("--tenant-storage-isolation", action="store_true")
    storage.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--output-path", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    args = parser.parse_args(argv)
    spec_path = args.query_spec.expanduser().resolve()
    if spec_path.is_relative_to(PROJECT_ROOT.resolve()):
        parser.error("Real query specifications must be outside the source repository.")
    payload = json.loads(spec_path.read_text(encoding="utf-8"))
    try:
        refuse_holdout_spec(payload)
    except HoldoutSpecRefused as exc:
        print(json.dumps({"passed": False, "reason_code": str(exc)}))
        return 2
    specs = payload if isinstance(payload, list) else payload.get("queries", payload.get("query_specs"))
    report = run_mcp_probe(data_dir=args.data_dir, query_specs=specs, tenant_id=args.tenant_id,
        profile_id=args.profile_id, tenant_storage_isolation=args.tenant_storage_isolation,
        output_path=args.output_path, timeout_seconds=args.timeout_seconds)
    print(json.dumps({"passed": report["passed"], "query_count": report["query_count"],
                      "completed_cases": sum(case["completed"] for case in report["cases"]),
                      "duration_ms": report["duration_ms"]}))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(run())
