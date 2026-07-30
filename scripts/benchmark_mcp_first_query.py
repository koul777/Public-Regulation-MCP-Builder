from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_metadata import (
    capture_mcp_performance_source_state,
    current_repo_commit,
    finalize_mcp_performance_source_state,
)


CHILD_PROTOCOL_VERSION = 1
DEFAULT_CHILD_TIMEOUT_SECONDS = 120.0
MAX_ERROR_INPUT_CHARS = 20_000
MAX_IDENTIFIER_CHARS = 120
_SAFE_TIMING_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
SAFE_RETRIEVAL_STRATEGIES = frozenset({"catalog_toc_body", "flat_rag"})


def benchmark_mcp_first_query(
    *,
    data_dir: Path,
    tenant_id: str,
    profile_id: str | None = None,
    queries: list[str] | None = None,
    query_specs: list[dict[str, Any]] | None = None,
    query_spec_source: Path | None = None,
    iterations: int = 5,
    warm_iterations: int = 0,
    top_k: int = 5,
    security_levels: list[str] | None = None,
    tenant_storage_isolation: bool | None = None,
    min_success_count: int | None = None,
    min_result_count: int = 1,
    required_retrieval_strategy: str | None = None,
    max_cold_p95_ms: float | None = None,
    max_warm_p95_ms: float | None = None,
    child_timeout_seconds: float = DEFAULT_CHILD_TIMEOUT_SECONDS,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    """Measure the first real MCP search in fresh Python processes.

    The parent intentionally does not import the application. Each child imports
    and configures the application, performs one cold search, and may then perform
    warm searches in that same process.
    """

    started_source_state = capture_mcp_performance_source_state(PROJECT_ROOT)
    selected_queries = normalize_query_specs(queries=queries, query_specs=query_specs)
    iteration_count = max(1, int(iterations or 1))
    warm_iteration_count = max(0, int(warm_iterations or 0))
    timeout_seconds = float(child_timeout_seconds)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("child_timeout_seconds must be a finite positive number.")
    if min_success_count is not None and int(min_success_count) < 1:
        raise ValueError("min_success_count must be at least 1 when provided.")
    effective_min_result_count = int(min_result_count)
    if effective_min_result_count < 0:
        raise ValueError("min_result_count must be non-negative.")
    normalized_required_strategy = _normalize_required_retrieval_strategy(
        required_retrieval_strategy
    )
    for name, value in (
        ("max_cold_p95_ms", max_cold_p95_ms),
        ("max_warm_p95_ms", max_warm_p95_ms),
    ):
        if value is not None and (not math.isfinite(float(value)) or float(value) < 0):
            raise ValueError(f"{name} must be a finite non-negative number.")

    levels = [str(level).strip() for level in (security_levels or ["internal"]) if str(level).strip()]
    if not levels:
        levels = ["internal"]

    measurements: list[dict[str, Any]] = []
    for spec in selected_queries:
        for index in range(iteration_count):
            measurement = _run_child_query(
                data_dir=Path(data_dir),
                tenant_id=tenant_id,
                profile_id=profile_id,
                query_id=spec["query_id"],
                query=spec["query"],
                query_sha256=spec["query_sha256"],
                iteration=index + 1,
                warm_iterations=warm_iteration_count,
                top_k=top_k,
                security_levels=levels,
                tenant_storage_isolation=tenant_storage_isolation,
                child_timeout_seconds=timeout_seconds,
            )
            measurement["expect_no_evidence"] = bool(
                spec.get("expect_no_evidence")
            )
            measurements.append(measurement)

    summary = _summarize_measurements(
        measurements,
        min_result_count=effective_min_result_count,
        required_retrieval_strategy=normalized_required_strategy,
    )
    effective_min_success_count = (
        len(measurements) if min_success_count is None else int(min_success_count)
    )
    findings = _threshold_findings(
        summary,
        effective_min_success_count=effective_min_success_count,
        min_result_count=effective_min_result_count,
        required_retrieval_strategy=normalized_required_strategy,
        max_cold_p95_ms=max_cold_p95_ms,
        max_warm_p95_ms=max_warm_p95_ms,
    )
    query_public = [
        {
            "query_id": spec["query_id"],
            "query_sha256": spec["query_sha256"],
            "expect_no_evidence": bool(spec.get("expect_no_evidence")),
        }
        for spec in selected_queries
    ]
    source_state = finalize_mcp_performance_source_state(
        started_source_state,
        PROJECT_ROOT,
    )
    report: dict[str, Any] = {
        "report_type": "mcp_first_query_benchmark",
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "source_state": source_state,
        "data_dir": str(data_dir),
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "tenant_storage_isolation": tenant_storage_isolation,
        "query_count": len(selected_queries),
        "query_fingerprint_algorithm": "sha256",
        "query_set_sha256": _query_set_fingerprint(query_public),
        "query_sha256": query_public[0]["query_sha256"] if len(query_public) == 1 else None,
        "queries": query_public,
        "iterations_per_query": iteration_count,
        "warm_iterations_per_child": warm_iteration_count,
        "top_k": max(1, min(int(top_k or 5), 20)),
        "security_levels": levels,
        "child_timeout_seconds": timeout_seconds,
        "settings_overrides": {
            "api_audit_enabled": False,
            "rag_trace_enabled": False,
        },
        "thresholds": {
            "min_success_count": min_success_count,
            "effective_min_success_count": effective_min_success_count,
            "min_result_count": effective_min_result_count,
            "required_retrieval_strategy": normalized_required_strategy,
            "max_cold_p95_ms": max_cold_p95_ms,
            "cold_p95_metric": "process_wall_elapsed_ms",
            "max_warm_p95_ms": max_warm_p95_ms,
            "warm_p95_metric": "search_elapsed_ms",
        },
        "summary": summary,
        "finding_count": len(findings),
        "findings": findings,
        "passed": not findings,
        "api_call_count": 0,
        "search_call_count": summary["cold"]["attempt_count"] + summary["warm"]["attempt_count"],
        "items": _summarize_by_query(
            measurements,
            query_public,
            min_result_count=effective_min_result_count,
            required_retrieval_strategy=normalized_required_strategy,
        ),
        "measurements": measurements,
    }
    if query_spec_source is not None:
        report["query_spec"] = _query_spec_fingerprint(
            Path(query_spec_source),
            item_count=len(selected_queries),
        )
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def normalize_query_specs(
    *,
    queries: list[str] | None = None,
    query_specs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if queries and query_specs:
        raise ValueError("Use either queries or query_specs, not both.")

    candidates: list[tuple[str | None, str, bool]] = []
    if query_specs is not None:
        for item in query_specs:
            if not isinstance(item, dict):
                continue
            query = str(item.get("query") or item.get("question") or "").strip()
            if not query:
                continue
            supplied_id = str(item.get("query_id") or item.get("id") or item.get("name") or "").strip() or None
            candidates.append(
                (
                    supplied_id,
                    query,
                    item.get("expect_no_evidence") is True,
                )
            )
    else:
        for query_value in queries or []:
            query = str(query_value or "").strip()
            if query:
                candidates.append((None, query, False))

    if not candidates:
        raise ValueError("At least one non-empty query is required.")

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for supplied_id, query, expect_no_evidence in candidates:
        query_sha256 = query_fingerprint(query)
        query_id = _normalize_query_id(supplied_id, query_sha256=query_sha256)
        if query_id in seen_ids:
            raise ValueError(f"Duplicate query_id: {query_id}")
        seen_ids.add(query_id)
        normalized.append(
            {
                "query_id": query_id,
                "query": query,
                "query_sha256": query_sha256,
                "expect_no_evidence": expect_no_evidence,
            }
        )
    return normalized


def load_query_specs(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        if "queries" in payload or "items" in payload:
            payload = payload.get("queries") or payload.get("items") or []
        elif payload.get("query") or payload.get("question"):
            payload = [payload]
    if not isinstance(payload, list):
        raise ValueError("--query-spec-json must contain a list or an object with queries/items.")
    normalized: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, str):
            normalized.append({"query": item})
        elif isinstance(item, dict):
            normalized.append(item)
    return normalized


def query_fingerprint(query: str) -> str:
    return hashlib.sha256(str(query).encode("utf-8")).hexdigest()


def _normalize_query_id(value: str | None, *, query_sha256: str) -> str:
    if value is None:
        return f"query-{query_sha256[:12]}"
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "-", value.strip()).strip("-")
    if not normalized:
        return f"query-{query_sha256[:12]}"
    return normalized[:MAX_IDENTIFIER_CHARS]


def _query_set_fingerprint(items: list[dict[str, Any]]) -> str:
    canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _query_spec_fingerprint(path: Path, *, item_count: int) -> dict[str, Any]:
    return {
        "sha256": _sha256_bytes(path.read_bytes()),
        "byte_count": path.stat().st_size,
        "item_count": item_count,
    }


def _run_child_query(
    *,
    data_dir: Path,
    tenant_id: str,
    profile_id: str | None,
    query_id: str,
    query: str,
    query_sha256: str,
    iteration: int,
    warm_iterations: int,
    top_k: int,
    security_levels: list[str],
    tenant_storage_isolation: bool | None,
    child_timeout_seconds: float,
) -> dict[str, Any]:
    request = {
        "protocol_version": CHILD_PROTOCOL_VERSION,
        "data_dir": str(data_dir),
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "query_id": query_id,
        "query": query,
        "query_sha256": query_sha256,
        "warm_iterations": warm_iterations,
        "top_k": max(1, min(int(top_k or 5), 20)),
        "security_levels": security_levels,
        "tenant_storage_isolation": tenant_storage_isolation,
    }
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--child-query",
    ]
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONHASHSEED", "0")
    started_at = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=env,
            input=json.dumps(request, ensure_ascii=False),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=child_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = _elapsed_ms(started_at)
        return {
            "iteration": iteration,
            "query_id": query_id,
            "query_sha256": query_sha256,
            "returncode": None,
            "timed_out": True,
            "protocol_valid": False,
            "process_wall_elapsed_ms": elapsed_ms,
            "process_error": {
                "type": "TimeoutExpired",
                "timeout_seconds": child_timeout_seconds,
            },
            **_stream_digests(
                stdout=getattr(exc, "stdout", None),
                stderr=getattr(exc, "stderr", None),
            ),
            "cold": _not_attempted_search("child_timeout"),
            "warm": [],
        }

    elapsed_ms = _elapsed_ms(started_at)
    measurement: dict[str, Any] = {
        "iteration": iteration,
        "query_id": query_id,
        "query_sha256": query_sha256,
        "returncode": completed.returncode,
        "timed_out": False,
        "protocol_valid": False,
        "process_wall_elapsed_ms": elapsed_ms,
        **_stream_digests(stdout=None, stderr=completed.stderr),
        "cold": _not_attempted_search("child_output_unavailable"),
        "warm": [],
    }
    try:
        payload = _parse_child_payload(completed.stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        measurement["process_error"] = {
            "type": type(exc).__name__,
            "reason": "invalid_child_json",
        }
        measurement.update(_stream_digests(stdout=completed.stdout, stderr=completed.stderr))
        return measurement

    if (
        payload.get("protocol_version") != CHILD_PROTOCOL_VERSION
        or payload.get("query_id") != query_id
        or payload.get("query_sha256") != query_sha256
    ):
        measurement["process_error"] = {
            "type": "ChildProtocolMismatch",
            "reason": "identity_or_version_mismatch",
        }
        return measurement

    measurement["protocol_valid"] = True
    measurement["setup"] = _normalize_setup(payload.get("setup"))
    measurement["cold"] = _normalize_search_measurement(payload.get("cold"))
    warm_payload = payload.get("warm") if isinstance(payload.get("warm"), list) else []
    measurement["warm"] = [
        {
            "iteration": index + 1,
            **_normalize_search_measurement(item),
        }
        for index, item in enumerate(warm_payload[:warm_iterations])
    ]
    return measurement


def _parse_child_payload(stdout: str | None) -> dict[str, Any]:
    lines = [line.strip() for line in str(stdout or "").splitlines() if line.strip()]
    if not lines:
        raise ValueError("Child process returned no JSON payload.")
    payload = json.loads(lines[-1])
    if not isinstance(payload, dict):
        raise ValueError("Child JSON payload must be an object.")
    return payload


def _child_query(*, request: dict[str, Any], stdout: TextIO) -> int:
    query_id = str(request.get("query_id") or "")[:MAX_IDENTIFIER_CHARS]
    query = str(request.get("query") or "")
    query_sha256 = str(request.get("query_sha256") or "")
    payload: dict[str, Any] = {
        "protocol_version": CHILD_PROTOCOL_VERSION,
        "query_id": query_id,
        "query_sha256": query_sha256,
        "setup": {"success": False, "elapsed_ms": None},
        "cold": _not_attempted_search("setup_not_started"),
        "warm": [],
    }
    if (
        request.get("protocol_version") != CHILD_PROTOCOL_VERSION
        or not query_id
        or not query.strip()
        or query_sha256 != query_fingerprint(query)
    ):
        payload["setup"]["error"] = {
            "type": "InvalidChildRequest",
            "message_sha256": _sha256_bytes(b"invalid child request"),
            "message_length": len("invalid child request"),
        }
        stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        return 1

    setup_started_at = time.perf_counter()
    try:
        from app.mcp_server.regulation_tools import (
            mcp_auth_context,
            search_regulations,
            settings_for_mcp_project,
        )

        storage_isolation = request.get("tenant_storage_isolation")
        if storage_isolation not in {None, True, False}:
            raise ValueError("tenant_storage_isolation must be true, false, or null.")
        settings = settings_for_mcp_project(
            data_dir=Path(str(request.get("data_dir") or "data")),
            tenant_id=str(request.get("tenant_id") or "default"),
            tenant_storage_isolation=storage_isolation,
            api_audit_enabled=False,
            rag_trace_enabled=False,
        )
        auth = mcp_auth_context(tenant_id=str(request.get("tenant_id") or "default"))
    except Exception as exc:
        payload["setup"] = {
            "success": False,
            "elapsed_ms": _elapsed_ms(setup_started_at),
            "error": _error_summary(exc),
        }
        payload["cold"] = _not_attempted_search("setup_failed")
        stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        return 0

    payload["setup"] = {
        "success": True,
        "elapsed_ms": _elapsed_ms(setup_started_at),
    }
    search_kwargs = {
        "settings": settings,
        "auth": auth,
        "query": query,
        "top_k": max(1, min(int(request.get("top_k") or 5), 20)),
        "security_levels": [
            str(level)
            for level in (request.get("security_levels") or ["internal"])
            if str(level).strip()
        ],
        "profile_id": str(request.get("profile_id") or "").strip() or None,
    }
    payload["cold"] = _measure_search(search_regulations, search_kwargs)
    warm_iterations = max(0, int(request.get("warm_iterations") or 0))
    payload["warm"] = [
        _measure_search(search_regulations, search_kwargs)
        for _index in range(warm_iterations)
    ]
    stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


def _measure_search(search_callable: Any, search_kwargs: dict[str, Any]) -> dict[str, Any]:
    started_at = time.perf_counter()
    try:
        response = search_callable(**search_kwargs)
    except Exception as exc:
        return {
            "attempted": True,
            "success": False,
            "search_elapsed_ms": _elapsed_ms(started_at),
            "result_count": None,
            "retrieval_strategy": None,
            "trace_timing_ms": {},
            "error": _error_summary(exc),
        }
    results = (
        response.get("results")
        if isinstance(response, dict) and isinstance(response.get("results"), list)
        else []
    )
    metadata = (
        response.get("metadata")
        if isinstance(response, dict) and isinstance(response.get("metadata"), dict)
        else {}
    )
    return {
        "attempted": True,
        "success": True,
        "search_elapsed_ms": _elapsed_ms(started_at),
        "result_count": len(results),
        "retrieval_strategy": _normalize_retrieval_strategy(
            metadata.get("retrieval_strategy")
        ),
        "trace_timing_ms": _normalize_trace_timing(metadata.get("timing_ms")),
        "error": None,
    }


def _error_summary(exc: Exception) -> dict[str, Any]:
    raw = str(exc)[:MAX_ERROR_INPUT_CHARS]
    encoded = raw.encode("utf-8", errors="replace")
    return {
        "type": type(exc).__name__[:80],
        "message_sha256": _sha256_bytes(encoded),
        "message_length": len(raw),
    }


def _normalize_setup(value: Any) -> dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    return {
        "success": bool(item.get("success")),
        "elapsed_ms": _finite_number(item.get("elapsed_ms")),
        "error": _normalize_error(item.get("error")),
    }


def _normalize_search_measurement(value: Any) -> dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    result_count = item.get("result_count")
    if isinstance(result_count, bool) or not isinstance(result_count, int) or result_count < 0:
        result_count = None
    return {
        "attempted": bool(item.get("attempted")),
        "success": bool(item.get("success")),
        "search_elapsed_ms": _finite_number(item.get("search_elapsed_ms")),
        "result_count": result_count,
        "retrieval_strategy": _normalize_retrieval_strategy(
            item.get("retrieval_strategy")
        ),
        "trace_timing_ms": _normalize_trace_timing(item.get("trace_timing_ms")),
        "error": _normalize_error(item.get("error")),
    }


def _normalize_retrieval_strategy(value: Any) -> str | None:
    strategy = str(value or "").strip()
    return strategy if strategy in SAFE_RETRIEVAL_STRATEGIES else None


def _normalize_required_retrieval_strategy(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _normalize_retrieval_strategy(value)
    if normalized is None:
        raise ValueError(
            "required_retrieval_strategy must be one of: "
            + ", ".join(sorted(SAFE_RETRIEVAL_STRATEGIES))
        )
    return normalized


def _normalize_error(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    error_type = str(value.get("type") or "")[:80]
    digest = str(value.get("message_sha256") or "").lower()
    message_length = value.get("message_length")
    normalized: dict[str, Any] = {"type": error_type or "UnknownError"}
    if re.fullmatch(r"[a-f0-9]{64}", digest):
        normalized["message_sha256"] = digest
    if isinstance(message_length, int) and not isinstance(message_length, bool) and message_length >= 0:
        normalized["message_length"] = message_length
    reason = str(value.get("reason") or "")
    if _SAFE_TIMING_KEY.fullmatch(reason):
        normalized["reason"] = reason
    return normalized


def _normalize_trace_timing(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    timings: dict[str, float] = {}
    for key, raw in value.items():
        name = str(key)
        number = _finite_number(raw)
        if _SAFE_TIMING_KEY.fullmatch(name) and number is not None and number >= 0:
            timings[name] = number
    return timings


def _not_attempted_search(reason: str) -> dict[str, Any]:
    return {
        "attempted": False,
        "success": False,
        "search_elapsed_ms": None,
        "result_count": None,
        "retrieval_strategy": None,
        "trace_timing_ms": {},
        "error": {
            "type": "NotAttempted",
            "reason": reason,
        },
    }


def _stream_digests(*, stdout: str | bytes | None, stderr: str | bytes | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in (("stdout", stdout), ("stderr", stderr)):
        if value in {None, "", b""}:
            continue
        encoded = value if isinstance(value, bytes) else str(value).encode("utf-8", errors="replace")
        result[f"{name}_byte_count"] = len(encoded)
        result[f"{name}_sha256"] = _sha256_bytes(encoded)
    return result


def _summarize_measurements(
    measurements: list[dict[str, Any]],
    *,
    min_result_count: int,
    required_retrieval_strategy: str | None,
) -> dict[str, Any]:
    cold_pairs = [
        (measurement, measurement.get("cold") or {})
        for measurement in measurements
    ]
    warm_pairs = [
        (measurement, warm_item)
        for measurement in measurements
        for warm_item in measurement.get("warm") or []
    ]
    cold = [item for _measurement, item in cold_pairs]
    warm = [item for _measurement, item in warm_pairs]
    cold_attempt_count = sum(1 for item in cold if item.get("attempted"))
    cold_operational_pairs = [
        (measurement, item)
        for measurement, item in cold_pairs
        if _successful_child_search(measurement, item)
    ]
    cold_success_pairs = [
        (measurement, item)
        for measurement, item in cold_operational_pairs
        if _search_meets_requirements(
            item,
            expect_no_evidence=bool(
                measurement.get("expect_no_evidence")
            ),
            min_result_count=min_result_count,
            required_retrieval_strategy=required_retrieval_strategy,
        )
    ]
    warm_operational_pairs = [
        (measurement, item)
        for measurement, item in warm_pairs
        if item.get("attempted") and item.get("success")
    ]
    warm_success_pairs = [
        (measurement, item)
        for measurement, item in warm_operational_pairs
        if _search_meets_requirements(
            item,
            expect_no_evidence=bool(
                measurement.get("expect_no_evidence")
            ),
            min_result_count=min_result_count,
            required_retrieval_strategy=required_retrieval_strategy,
        )
    ]
    cold_operational_successes = [
        item for _measurement, item in cold_operational_pairs
    ]
    cold_successes = [item for _measurement, item in cold_success_pairs]
    warm_operational_successes = [
        item for _measurement, item in warm_operational_pairs
    ]
    warm_successes = [item for _measurement, item in warm_success_pairs]
    cold_answerable_successes = [
        item
        for measurement, item in cold_success_pairs
        if not measurement.get("expect_no_evidence")
    ]
    cold_no_evidence_successes = [
        item
        for measurement, item in cold_success_pairs
        if measurement.get("expect_no_evidence")
    ]
    warm_answerable_successes = [
        item
        for measurement, item in warm_success_pairs
        if not measurement.get("expect_no_evidence")
    ]
    warm_no_evidence_successes = [
        item
        for measurement, item in warm_success_pairs
        if measurement.get("expect_no_evidence")
    ]
    setup_elapsed_values = [
        setup_elapsed_ms
        for measurement in measurements
        for setup_elapsed_ms in [
            _finite_number((measurement.get("setup") or {}).get("elapsed_ms"))
        ]
        if setup_elapsed_ms is not None
    ]
    process_successes = [
        measurement
        for measurement, _item in cold_success_pairs
    ]
    cold_non_search_overhead_values = [
        round(process_elapsed_ms - search_elapsed_ms, 3)
        for measurement, item in cold_success_pairs
        for process_elapsed_ms in [_finite_number(measurement.get("process_wall_elapsed_ms"))]
        for search_elapsed_ms in [_finite_number(item.get("search_elapsed_ms"))]
        if (
            process_elapsed_ms is not None
            and search_elapsed_ms is not None
            and process_elapsed_ms >= search_elapsed_ms
        )
    ]
    child_harness_overhead_values = [
        round(process_elapsed_ms - setup_elapsed_ms - search_elapsed_ms, 3)
        for measurement, item in cold_success_pairs
        for process_elapsed_ms in [_finite_number(measurement.get("process_wall_elapsed_ms"))]
        for setup_elapsed_ms in [
            _finite_number((measurement.get("setup") or {}).get("elapsed_ms"))
        ]
        for search_elapsed_ms in [_finite_number(item.get("search_elapsed_ms"))]
        if (
            process_elapsed_ms is not None
            and setup_elapsed_ms is not None
            and search_elapsed_ms is not None
            and process_elapsed_ms >= setup_elapsed_ms + search_elapsed_ms
        )
    ]
    return {
        "measurement_count": len(measurements),
        "setup_elapsed_ms": _stats(setup_elapsed_values),
        "process_wall_elapsed_ms": _stats(_numeric_values(measurements, "process_wall_elapsed_ms")),
        "successful_process_wall_elapsed_ms": _stats(
            _numeric_values(process_successes, "process_wall_elapsed_ms")
        ),
        "cold_non_search_overhead_ms": _stats(cold_non_search_overhead_values),
        "child_harness_overhead_ms": _stats(child_harness_overhead_values),
        "cold": {
            "requested_count": len(measurements),
            "answerable_requested_count": sum(
                1
                for measurement in measurements
                if not measurement.get("expect_no_evidence")
            ),
            "no_evidence_requested_count": sum(
                1
                for measurement in measurements
                if measurement.get("expect_no_evidence")
            ),
            "attempt_count": cold_attempt_count,
            "not_attempted_count": len(measurements) - cold_attempt_count,
            "operational_successful_count": len(cold_operational_successes),
            "successful_count": len(cold_successes),
            "answerable_successful_count": len(
                cold_answerable_successes
            ),
            "no_evidence_successful_count": len(
                cold_no_evidence_successes
            ),
            "failed_count": len(measurements) - len(cold_successes),
            "result_requirement_failed_count": _result_requirement_failed_count(
                cold_operational_pairs,
                min_result_count=min_result_count,
            ),
            "retrieval_strategy_requirement_failed_count": (
                _retrieval_strategy_requirement_failed_count(
                    cold_operational_successes,
                    required_retrieval_strategy=required_retrieval_strategy,
                )
            ),
            **_success_rates(len(cold_successes), len(measurements)),
            "search_elapsed_ms": _stats(_numeric_values(cold_successes, "search_elapsed_ms")),
            "result_count": _stats(_numeric_values(cold_successes, "result_count")),
            "answerable_result_count": _stats(
                _numeric_values(cold_answerable_successes, "result_count")
            ),
            "no_evidence_result_count": _stats(
                _numeric_values(cold_no_evidence_successes, "result_count")
            ),
            "trace_timing_ms": _summarize_trace_timings(cold_successes),
        },
        "warm": {
            "attempt_count": len(warm),
            "answerable_attempt_count": sum(
                len(measurement.get("warm") or [])
                for measurement in measurements
                if not measurement.get("expect_no_evidence")
            ),
            "no_evidence_attempt_count": sum(
                len(measurement.get("warm") or [])
                for measurement in measurements
                if measurement.get("expect_no_evidence")
            ),
            "operational_successful_count": len(warm_operational_successes),
            "successful_count": len(warm_successes),
            "answerable_successful_count": len(
                warm_answerable_successes
            ),
            "no_evidence_successful_count": len(
                warm_no_evidence_successes
            ),
            "failed_count": len(warm) - len(warm_successes),
            "result_requirement_failed_count": _result_requirement_failed_count(
                warm_operational_pairs,
                min_result_count=min_result_count,
            ),
            "retrieval_strategy_requirement_failed_count": (
                _retrieval_strategy_requirement_failed_count(
                    warm_operational_successes,
                    required_retrieval_strategy=required_retrieval_strategy,
                )
            ),
            **_success_rates(len(warm_successes), len(warm)),
            "search_elapsed_ms": _stats(_numeric_values(warm_successes, "search_elapsed_ms")),
            "result_count": _stats(_numeric_values(warm_successes, "result_count")),
            "answerable_result_count": _stats(
                _numeric_values(warm_answerable_successes, "result_count")
            ),
            "no_evidence_result_count": _stats(
                _numeric_values(warm_no_evidence_successes, "result_count")
            ),
            "trace_timing_ms": _summarize_trace_timings(warm_successes),
        },
        "timed_out_count": sum(1 for item in measurements if item.get("timed_out")),
        "invalid_protocol_count": sum(1 for item in measurements if not item.get("protocol_valid")),
    }


def _successful_child_search(measurement: dict[str, Any], search: dict[str, Any]) -> bool:
    return bool(
        measurement.get("returncode") == 0
        and measurement.get("protocol_valid")
        and search.get("attempted")
        and search.get("success")
    )


def _search_meets_requirements(
    search: dict[str, Any],
    *,
    expect_no_evidence: bool,
    min_result_count: int,
    required_retrieval_strategy: str | None,
) -> bool:
    result_count = search.get("result_count")
    if (
        isinstance(result_count, bool)
        or not isinstance(result_count, int)
        or (
            result_count != 0
            if expect_no_evidence
            else result_count < min_result_count
        )
    ):
        return False
    return (
        required_retrieval_strategy is None
        or search.get("retrieval_strategy") == required_retrieval_strategy
    )


def _result_requirement_failed_count(
    searches: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    min_result_count: int,
) -> int:
    return sum(
        1
        for measurement, search in searches
        if (
            isinstance(search.get("result_count"), bool)
            or not isinstance(search.get("result_count"), int)
            or (
                int(search["result_count"]) != 0
                if measurement.get("expect_no_evidence")
                else int(search["result_count"]) < min_result_count
            )
        )
    )


def _retrieval_strategy_requirement_failed_count(
    searches: list[dict[str, Any]],
    *,
    required_retrieval_strategy: str | None,
) -> int:
    if required_retrieval_strategy is None:
        return 0
    return sum(
        1
        for search in searches
        if search.get("retrieval_strategy") != required_retrieval_strategy
    )


def _success_rates(successful_count: int, attempt_count: int) -> dict[str, float | None]:
    if attempt_count <= 0:
        return {
            "success_rate": None,
            "success_rate_percent": None,
        }
    rate = successful_count / attempt_count
    return {
        "success_rate": round(rate, 6),
        "success_rate_percent": round(rate * 100, 3),
    }


def _summarize_by_query(
    measurements: list[dict[str, Any]],
    queries: list[dict[str, str]],
    *,
    min_result_count: int,
    required_retrieval_strategy: str | None,
) -> list[dict[str, Any]]:
    return [
        {
            "query_id": query["query_id"],
            "query_sha256": query["query_sha256"],
            "summary": _summarize_measurements(
                [item for item in measurements if item.get("query_id") == query["query_id"]],
                min_result_count=min_result_count,
                required_retrieval_strategy=required_retrieval_strategy,
            ),
        }
        for query in queries
    ]


def _summarize_trace_timings(measurements: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    stages = sorted(
        {
            stage
            for item in measurements
            for stage in (item.get("trace_timing_ms") or {})
        }
    )
    return {
        stage: _stats(
            [
                float((item.get("trace_timing_ms") or {})[stage])
                for item in measurements
                if _finite_number((item.get("trace_timing_ms") or {}).get(stage)) is not None
            ]
        )
        for stage in stages
    }


def _numeric_values(items: list[dict[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for item in items:
        value = _finite_number(item.get(key))
        if value is not None:
            values.append(value)
    return values


def _stats(values: list[float]) -> dict[str, float | int | None]:
    ordered = sorted(
        number
        for value in values
        if (number := _finite_number(value)) is not None
    )
    if not ordered:
        return {
            "count": 0,
            "min": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "max": None,
            "avg": None,
        }
    return {
        "count": len(ordered),
        "min": round(ordered[0], 3),
        "p50": round(_percentile(ordered, 0.50), 3),
        "p95": round(_percentile(ordered, 0.95), 3),
        "p99": round(_percentile(ordered, 0.99), 3),
        "max": round(ordered[-1], 3),
        "avg": round(sum(ordered) / len(ordered), 3),
    }


def _percentile(ordered_values: list[float], percentile: float) -> float:
    index = min(
        len(ordered_values) - 1,
        max(0, math.ceil(percentile * len(ordered_values)) - 1),
    )
    return ordered_values[index]


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _threshold_findings(
    summary: dict[str, Any],
    *,
    effective_min_success_count: int,
    min_result_count: int,
    required_retrieval_strategy: str | None,
    max_cold_p95_ms: float | None,
    max_warm_p95_ms: float | None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    cold = summary.get("cold") or {}
    warm = summary.get("warm") or {}
    if int(cold.get("answerable_requested_count") or 0) <= 0:
        findings.append(
            {
                "code": "first-query-answerable-query-required",
                "answerable_requested_count": int(
                    cold.get("answerable_requested_count") or 0
                ),
            }
        )
    actual_success_count = int(cold.get("successful_count") or 0)
    if actual_success_count < effective_min_success_count:
        findings.append(
            {
                "code": "first-query-success-count-below-minimum",
                "actual_success_count": actual_success_count,
                "threshold_success_count": effective_min_success_count,
            }
        )
    result_failure_count = int(cold.get("result_requirement_failed_count") or 0)
    if result_failure_count:
        findings.append(
            {
                "code": "first-query-result-count-below-minimum",
                "failed_count": result_failure_count,
                "min_result_count": min_result_count,
            }
        )
    strategy_failure_count = int(
        cold.get("retrieval_strategy_requirement_failed_count") or 0
    )
    if strategy_failure_count and required_retrieval_strategy is not None:
        findings.append(
            {
                "code": "first-query-retrieval-strategy-mismatch",
                "failed_count": strategy_failure_count,
                "required_retrieval_strategy": required_retrieval_strategy,
            }
        )

    if max_cold_p95_ms is not None:
        actual_cold_p95 = (summary.get("process_wall_elapsed_ms") or {}).get("p95")
        if actual_cold_p95 is None:
            findings.append(
                {
                    "code": "first-query-cold-p95-unavailable",
                    "metric": "process_wall_elapsed_ms",
                    "threshold_ms": max_cold_p95_ms,
                }
            )
        elif float(actual_cold_p95) > float(max_cold_p95_ms):
            findings.append(
                {
                    "code": "first-query-cold-p95-exceeded",
                    "metric": "process_wall_elapsed_ms",
                    "actual_ms": actual_cold_p95,
                    "threshold_ms": max_cold_p95_ms,
                }
            )

    if max_warm_p95_ms is not None:
        actual_warm_p95 = (warm.get("search_elapsed_ms") or {}).get("p95")
        if actual_warm_p95 is None:
            findings.append(
                {
                    "code": "first-query-warm-p95-unavailable",
                    "metric": "search_elapsed_ms",
                    "threshold_ms": max_warm_p95_ms,
                }
            )
        elif float(actual_warm_p95) > float(max_warm_p95_ms):
            findings.append(
                {
                    "code": "first-query-warm-p95-exceeded",
                    "metric": "search_elapsed_ms",
                    "actual_ms": actual_warm_p95,
                    "threshold_ms": max_warm_p95_ms,
                }
            )
    if int(warm.get("failed_count") or 0) > 0:
        findings.append(
            {
                "code": "first-query-warm-search-failed",
                "failed_count": int(warm.get("failed_count") or 0),
                "attempt_count": int(warm.get("attempt_count") or 0),
            }
        )
    return findings


def _to_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    cold = summary.get("cold") or {}
    warm = summary.get("warm") or {}
    thresholds = report.get("thresholds") or {}
    setup = summary.get("setup_elapsed_ms") or {}
    process = summary.get("process_wall_elapsed_ms") or {}
    cold_non_search = summary.get("cold_non_search_overhead_ms") or {}
    harness_overhead = summary.get("child_harness_overhead_ms") or {}
    cold_search = cold.get("search_elapsed_ms") or {}
    warm_search = warm.get("search_elapsed_ms") or {}
    lines = [
        "# MCP First Query Benchmark",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Repository commit: `{_md_cell(report.get('repo_commit') or 'unavailable')}`",
        (
            "- Source state: "
            f"`{_md_cell((report.get('source_state') or {}).get('status') or 'unavailable')}` "
            f"({_md_cell((report.get('source_state') or {}).get('scope') or '')})"
        ),
        f"- Passed: `{str(bool(report.get('passed'))).lower()}`",
        f"- Query count: {report.get('query_count')}",
        f"- Fresh child measurements: {summary.get('measurement_count')}",
        f"- Minimum results per successful search: {thresholds.get('min_result_count')}",
        (
            "- Required retrieval strategy: "
            f"`{_md_cell(thresholds.get('required_retrieval_strategy') or 'any')}`"
        ),
        (
            f"- Cold qualified success: {cold.get('successful_count')} / "
            f"{cold.get('requested_count')} "
            f"({cold.get('success_rate_percent')}%)"
        ),
        (
            f"- Warm qualified success: {warm.get('successful_count')} / "
            f"{warm.get('attempt_count')} "
            f"({warm.get('success_rate_percent')}%)"
        ),
        (
            f"- Setup elapsed p50/p95/p99/max ms: {setup.get('p50')} / "
            f"{setup.get('p95')} / {setup.get('p99')} / {setup.get('max')}"
        ),
        (
            f"- Process wall p50/p95/p99/max ms: {process.get('p50')} / {process.get('p95')} / "
            f"{process.get('p99')} / {process.get('max')}"
        ),
        (
            f"- Cold non-search overhead p50/p95/p99/max ms: {cold_non_search.get('p50')} / "
            f"{cold_non_search.get('p95')} / {cold_non_search.get('p99')} / {cold_non_search.get('max')}"
        ),
        (
            f"- Child harness overhead p50/p95/p99/max ms: {harness_overhead.get('p50')} / "
            f"{harness_overhead.get('p95')} / {harness_overhead.get('p99')} / {harness_overhead.get('max')}"
        ),
        (
            f"- Cold search p50/p95/p99/max ms: {cold_search.get('p50')} / "
            f"{cold_search.get('p95')} / {cold_search.get('p99')} / {cold_search.get('max')}"
        ),
        (
            f"- Warm search p50/p95/p99/max ms: {warm_search.get('p50')} / "
            f"{warm_search.get('p95')} / {warm_search.get('p99')} / {warm_search.get('max')}"
        ),
        "",
        "## Queries",
        "",
        "| Query ID | SHA-256 | Cold success |",
        "| --- | --- | ---: |",
    ]
    for item in report.get("items") or []:
        item_summary = item.get("summary") or {}
        item_cold = item_summary.get("cold") or {}
        lines.append(
            f"| `{_md_cell(item.get('query_id'))}` | `{_md_cell(item.get('query_sha256'))}` | "
            f"{item_cold.get('successful_count')} / {item_cold.get('requested_count')} |"
        )

    for title, timing in (
        ("Cold Trace Timing", cold.get("trace_timing_ms") or {}),
        ("Warm Trace Timing", warm.get("trace_timing_ms") or {}),
    ):
        if not timing:
            continue
        lines.extend(
            [
                "",
                f"## {title}",
                "",
                "| Stage | Count | p50 ms | p95 ms | p99 ms | max ms |",
                "| --- | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for stage, stats in sorted(timing.items()):
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"`{_md_cell(stage)}`",
                        _md_cell(stats.get("count")),
                        _md_cell(stats.get("p50")),
                        _md_cell(stats.get("p95")),
                        _md_cell(stats.get("p99")),
                        _md_cell(stats.get("max")),
                    ]
                )
                + " |"
            )

    lines.extend(["", "## Findings", ""])
    if report.get("findings"):
        for finding in report["findings"]:
            lines.append(f"- `{_md_cell(finding.get('code'))}`: {_md_cell(finding)}")
    else:
        lines.append("- None")
    return "\n".join(lines).rstrip() + "\n"


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 3)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark the first real approved MCP search in fresh Python child processes."
    )
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--profile-id", default=None)
    query_group = parser.add_mutually_exclusive_group()
    query_group.add_argument("--query", action="append", default=[])
    query_group.add_argument("--query-spec-json", "--query-spec", dest="query_spec_json", default=None)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--warm-iterations", type=int, default=0)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--security-level", action="append", default=None)
    storage_group = parser.add_mutually_exclusive_group()
    storage_group.add_argument("--tenant-storage-isolation", action="store_true")
    storage_group.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--min-success-count", "--min-successful", dest="min_success_count", type=int, default=None)
    parser.add_argument(
        "--min-result-count",
        type=int,
        default=1,
        help="Minimum result count for a search to qualify as successful (default: 1).",
    )
    parser.add_argument(
        "--required-retrieval-strategy",
        "--require-retrieval-strategy",
        dest="required_retrieval_strategy",
        default=None,
        choices=sorted(SAFE_RETRIEVAL_STRATEGIES),
        help="Optional exact retrieval strategy required for each successful search.",
    )
    parser.add_argument("--max-cold-p95-ms", type=float, default=None)
    parser.add_argument("--max-warm-p95-ms", type=float, default=None)
    parser.add_argument("--child-timeout-seconds", type=float, default=DEFAULT_CHILD_TIMEOUT_SECONDS)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--fail-on-threshold", action="store_true")
    parser.add_argument("--child-query", action="store_true", help=argparse.SUPPRESS)
    return parser


def run(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    if stdout is sys.stdout and hasattr(stdout, "reconfigure"):
        stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.child_query:
        try:
            request = json.loads(stdin.read())
        except (json.JSONDecodeError, OSError) as exc:
            payload = {
                "protocol_version": CHILD_PROTOCOL_VERSION,
                "query_id": "",
                "query_sha256": "",
                "setup": {
                    "success": False,
                    "elapsed_ms": None,
                    "error": _error_summary(exc),
                },
                "cold": _not_attempted_search("invalid_child_input"),
                "warm": [],
            }
            stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            return 1
        if not isinstance(request, dict):
            request = {}
        return _child_query(request=request, stdout=stdout)

    if not args.query and not args.query_spec_json:
        parser.error("one of --query or --query-spec-json is required")
    query_specs = load_query_specs(Path(args.query_spec_json)) if args.query_spec_json else None
    tenant_storage_isolation = None
    if args.tenant_storage_isolation:
        tenant_storage_isolation = True
    elif args.flat_storage:
        tenant_storage_isolation = False

    report = benchmark_mcp_first_query(
        data_dir=Path(args.data_dir),
        tenant_id=args.tenant_id,
        profile_id=args.profile_id,
        queries=args.query or None,
        query_specs=query_specs,
        query_spec_source=Path(args.query_spec_json) if args.query_spec_json else None,
        iterations=args.iterations,
        warm_iterations=args.warm_iterations,
        top_k=args.top_k,
        security_levels=args.security_level,
        tenant_storage_isolation=tenant_storage_isolation,
        min_success_count=args.min_success_count,
        min_result_count=args.min_result_count,
        required_retrieval_strategy=args.required_retrieval_strategy,
        max_cold_p95_ms=args.max_cold_p95_ms,
        max_warm_p95_ms=args.max_warm_p95_ms,
        child_timeout_seconds=args.child_timeout_seconds,
        out_json=Path(args.out_json) if args.out_json else None,
        out_md=Path(args.out_md) if args.out_md else None,
    )
    stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if args.fail_on_threshold and not report["passed"]:
        return 2
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
