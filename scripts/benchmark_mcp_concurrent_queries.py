from __future__ import annotations

import argparse
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.mcp_server.regulation_tools import (
    fetch_regulation,
    mcp_auth_context,
    search_regulations,
    settings_for_mcp_project,
    warm_mcp_runtime,
)
from app.rag.extractive_answer import build_structured_extractive_answer
from scripts.benchmark_mcp_queries import _stats, load_query_specs
from scripts.export_mcp_demo_answers import normalize_query_specs, query_spec_fingerprint
from scripts.report_metadata import current_repo_commit


def benchmark_mcp_concurrent_queries(
    *,
    data_dir: Path,
    tenant_id: str,
    profile_id: str | None = None,
    queries: list[str] | None = None,
    query_specs: list[dict[str, Any]] | None = None,
    top_k: int = 5,
    rounds: int = 2,
    concurrency: int = 3,
    security_levels: list[str] | None = None,
    tenant_storage_isolation: bool | None = None,
    query_spec_source: Path | None = None,
    min_warm_records: int | None = None,
    max_task_total_ms: float | None = None,
    max_batch_elapsed_ms: float | None = None,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    settings = settings_for_mcp_project(
        data_dir=data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
    )
    auth = mcp_auth_context(tenant_id=tenant_id)
    levels = security_levels or ["internal"]
    selected_queries = normalize_query_specs(queries=queries, query_specs=query_specs)

    warmup_started_at = time.perf_counter()
    warmup = warm_mcp_runtime(settings=settings, auth=auth)
    warmup["external_elapsed_ms"] = _elapsed_ms(warmup_started_at)

    tasks = [
        {
            "round": round_index + 1,
            "query_index": query_index + 1,
            "query": str(spec["query"]),
            "expect_no_evidence": bool(spec.get("expect_no_evidence") or spec.get("expected_no_evidence")),
        }
        for round_index in range(max(1, int(rounds or 1)))
        for query_index, spec in enumerate(selected_queries)
    ]
    batch_started_at = time.perf_counter()
    measurements: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, int(concurrency or 1))) as executor:
        future_map = {
            executor.submit(
                _run_query_task,
                settings=settings,
                auth=auth,
                query=task["query"],
                expect_no_evidence=task["expect_no_evidence"],
                top_k=top_k,
                security_levels=levels,
                profile_id=profile_id,
                round_index=task["round"],
                query_index=task["query_index"],
            ): task
            for task in tasks
        }
        for future in as_completed(future_map):
            task = future_map[future]
            try:
                measurements.append(future.result())
            except Exception as exc:  # pragma: no cover - exercised through failure reports in integration use.
                measurements.append(
                    {
                        "round": task["round"],
                        "query_index": task["query_index"],
                        "query": task["query"],
                        "expect_no_evidence": task["expect_no_evidence"],
                        "error": str(exc),
                        "search_result_count": 0,
                        "fetch_result_count": 0,
                        "total_elapsed_ms": 0.0,
                    }
                )
    measurements.sort(key=lambda item: (int(item.get("round") or 0), int(item.get("query_index") or 0)))
    batch_elapsed_ms = _elapsed_ms(batch_started_at)
    summary = _summarize_measurements(measurements, batch_elapsed_ms=batch_elapsed_ms)
    findings = _findings(
        measurements,
        warmup=warmup,
        batch_elapsed_ms=batch_elapsed_ms,
        min_warm_records=min_warm_records,
        max_task_total_ms=max_task_total_ms,
        max_batch_elapsed_ms=max_batch_elapsed_ms,
    )
    report = {
        "report_type": "mcp_concurrent_query_benchmark",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "data_dir": str(data_dir),
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "tenant_storage_isolation": tenant_storage_isolation,
        "security_levels": levels,
        "top_k": top_k,
        "rounds": max(1, int(rounds or 1)),
        "concurrency": max(1, int(concurrency or 1)),
        "query_count": len(selected_queries),
        "task_count": len(measurements),
        "min_warm_records": min_warm_records,
        "max_task_total_ms": max_task_total_ms,
        "max_batch_elapsed_ms": max_batch_elapsed_ms,
        "warmup": warmup,
        "summary": summary,
        "finding_count": len(findings),
        "findings": findings,
        "passed": not findings,
        "api_call_count": 0,
        "measurements": measurements,
    }
    if query_spec_source:
        report.update(query_spec_fingerprint(query_spec_source, item_count=len(selected_queries)))
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _run_query_task(
    *,
    settings: Any,
    auth: Any,
    query: str,
    expect_no_evidence: bool,
    top_k: int,
    security_levels: list[str],
    profile_id: str | None,
    round_index: int,
    query_index: int,
) -> dict[str, Any]:
    total_started_at = time.perf_counter()
    search_started_at = time.perf_counter()
    search = search_regulations(
        settings=settings,
        auth=auth,
        query=query,
        top_k=top_k,
        security_levels=security_levels,
        profile_id=profile_id,
    )
    search_elapsed_ms = _elapsed_ms(search_started_at)
    results = search.get("results") if isinstance(search.get("results"), list) else []

    fetch_started_at = time.perf_counter()
    fetched = [
        fetch_regulation(
            settings=settings,
            auth=auth,
                result_id=str(result.get("id") or ""),
                security_levels=security_levels,
                profile_id=profile_id,
        )
        for result in results
        if result.get("id")
    ]
    fetch_elapsed_ms = _elapsed_ms(fetch_started_at)

    answer_started_at = time.perf_counter()
    answer = build_structured_extractive_answer(query, [_flatten_result(item) for item in fetched])
    answer_elapsed_ms = _elapsed_ms(answer_started_at)
    return {
        "round": round_index,
        "query_index": query_index,
        "query": query,
        "expect_no_evidence": expect_no_evidence,
        "search_result_count": len(results),
        "fetch_result_count": len(fetched),
        "answer_char_count": len(answer),
        "search_elapsed_ms": search_elapsed_ms,
        "fetch_elapsed_ms": fetch_elapsed_ms,
        "answer_elapsed_ms": answer_elapsed_ms,
        "total_elapsed_ms": _elapsed_ms(total_started_at),
        "mcp_search_timing_ms": (search.get("metadata") or {}).get("timing_ms") or {},
        "trace_id": (search.get("metadata") or {}).get("trace_id"),
    }


def _flatten_result(result: dict[str, Any]) -> dict[str, Any]:
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    flattened = dict(metadata)
    flattened["id"] = result.get("id") or ""
    flattened["title"] = result.get("title") or ""
    flattened["url"] = result.get("url") or ""
    flattened["text"] = result.get("text") or ""
    return flattened


def _summarize_measurements(measurements: list[dict[str, Any]], *, batch_elapsed_ms: float) -> dict[str, Any]:
    return {
        "batch_elapsed_ms": batch_elapsed_ms,
        "measurement_count": len(measurements),
        "successful_count": sum(1 for item in measurements if not item.get("error")),
        "error_count": sum(1 for item in measurements if item.get("error")),
        "search_elapsed_ms": _stats(_numeric_values(measurements, "search_elapsed_ms")),
        "fetch_elapsed_ms": _stats(_numeric_values(measurements, "fetch_elapsed_ms")),
        "answer_elapsed_ms": _stats(_numeric_values(measurements, "answer_elapsed_ms")),
        "total_elapsed_ms": _stats(_numeric_values(measurements, "total_elapsed_ms")),
        "search_result_count_min": min([int(item.get("search_result_count") or 0) for item in measurements] or [0]),
        "fetch_result_count_min": min([int(item.get("fetch_result_count") or 0) for item in measurements] or [0]),
    }


def _numeric_values(measurements: list[dict[str, Any]], key: str) -> list[float]:
    return [float(item[key]) for item in measurements if isinstance(item.get(key), (int, float))]


def _findings(
    measurements: list[dict[str, Any]],
    *,
    warmup: dict[str, Any],
    batch_elapsed_ms: float,
    min_warm_records: int | None,
    max_task_total_ms: float | None,
    max_batch_elapsed_ms: float | None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if min_warm_records and int(warmup.get("record_count") or 0) < int(min_warm_records):
        findings.append(
            _finding(
                "concurrent-warm-record-count-below-minimum",
                "Warm runtime record count is below the configured minimum.",
                actual_record_count=int(warmup.get("record_count") or 0),
                threshold_record_count=int(min_warm_records),
            )
        )
    if not bool(warmup.get("bm25_index_ready")) and not bool(warmup.get("hierarchical_index_ready")):
        findings.append(
            _finding(
                "concurrent-bm25-index-not-ready",
                "Warm runtime has neither a BM25 index nor a verified hierarchical retrieval index.",
            )
        )
    if max_batch_elapsed_ms is not None and batch_elapsed_ms > max_batch_elapsed_ms:
        findings.append(
            _finding(
                "concurrent-batch-elapsed-too-high",
                "Concurrent batch wall-clock elapsed time exceeded the configured threshold.",
                actual_ms=batch_elapsed_ms,
                threshold_ms=max_batch_elapsed_ms,
            )
        )
    for item in measurements:
        if item.get("error"):
            findings.append(
                _finding(
                    "concurrent-query-error",
                    "Concurrent query task raised an error.",
                    round=item.get("round"),
                    query=item.get("query"),
                    error=item.get("error"),
                )
            )
        search_result_count = int(item.get("search_result_count") or 0)
        if bool(item.get("expect_no_evidence")) and search_result_count > 0:
            findings.append(
                _finding(
                    "concurrent-expected-no-evidence-returned-results",
                    "Concurrent query is marked expect_no_evidence but returned search results.",
                    round=item.get("round"),
                    query=item.get("query"),
                    search_result_count=search_result_count,
                )
            )
        elif not bool(item.get("expect_no_evidence")) and search_result_count < 1:
            findings.append(
                _finding(
                    "concurrent-query-no-results",
                    "Concurrent query returned no search results.",
                    round=item.get("round"),
                    query=item.get("query"),
                )
            )
        if (
            max_task_total_ms is not None
            and isinstance(item.get("total_elapsed_ms"), (int, float))
            and float(item["total_elapsed_ms"]) > max_task_total_ms
        ):
            findings.append(
                _finding(
                    "concurrent-task-elapsed-too-high",
                    "Concurrent query task elapsed time exceeded the configured threshold.",
                    round=item.get("round"),
                    query=item.get("query"),
                    actual_ms=item.get("total_elapsed_ms"),
                    threshold_ms=max_task_total_ms,
                )
            )
    return findings


def _finding(code: str, detail: str, **extra: Any) -> dict[str, Any]:
    item = {"code": code, "detail": detail}
    item.update(extra)
    return item


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 3)


def _to_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    total = summary.get("total_elapsed_ms") or {}
    lines = [
        "# MCP Concurrent Query Benchmark",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Queries: {report.get('query_count')}",
        f"- Rounds: {report.get('rounds')}",
        f"- Concurrency: {report.get('concurrency')}",
        f"- Tasks: {summary.get('successful_count')} successful / {summary.get('measurement_count')}",
        f"- Batch elapsed ms: {summary.get('batch_elapsed_ms')}",
        f"- Task total p50/p95/max ms: {total.get('p50')} / {total.get('p95')} / {total.get('max')}",
        f"- Warm records: {(report.get('warmup') or {}).get('record_count')}",
        "",
        "## Findings",
        "",
    ]
    if report.get("findings"):
        lines.extend(f"- `{item.get('code')}`: {item.get('detail')}" for item in report["findings"])
    else:
        lines.append("- None")
    return "\n".join(lines).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark concurrent approved local MCP query tasks.")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--profile-id", default=None, help="Institution profile scope when a tenant has multiple profiles.")
    parser.add_argument("--query", action="append", default=[])
    parser.add_argument("--query-spec-json", default=None)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--security-level", action="append", default=["internal"])
    parser.add_argument("--tenant-storage-isolation", action="store_true")
    parser.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--min-warm-records", type=int, default=None)
    parser.add_argument("--max-task-total-ms", type=float, default=None)
    parser.add_argument("--max-batch-elapsed-ms", type=float, default=None)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--fail-on-threshold", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    if stdout is sys.stdout and hasattr(stdout, "reconfigure"):
        stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    tenant_storage_isolation = None
    if args.tenant_storage_isolation:
        tenant_storage_isolation = True
    if args.flat_storage:
        tenant_storage_isolation = False
    query_specs = load_query_specs(Path(args.query_spec_json)) if args.query_spec_json else None
    query_spec_source = Path(args.query_spec_json) if args.query_spec_json else None
    report = benchmark_mcp_concurrent_queries(
        data_dir=Path(args.data_dir),
        tenant_id=args.tenant_id,
        profile_id=args.profile_id,
        queries=args.query or None,
        query_specs=query_specs,
        top_k=args.top_k,
        rounds=args.rounds,
        concurrency=args.concurrency,
        security_levels=args.security_level,
        tenant_storage_isolation=tenant_storage_isolation,
        query_spec_source=query_spec_source,
        min_warm_records=args.min_warm_records,
        max_task_total_ms=args.max_task_total_ms,
        max_batch_elapsed_ms=args.max_batch_elapsed_ms,
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
