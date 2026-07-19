from __future__ import annotations

import argparse
import json
import math
import sys
import time
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
from scripts.export_mcp_demo_answers import normalize_query_specs, query_spec_fingerprint
from scripts.report_metadata import current_repo_commit


def benchmark_mcp_queries(
    *,
    data_dir: Path,
    tenant_id: str,
    profile_id: str | None = None,
    queries: list[str] | None = None,
    query_specs: list[dict[str, Any]] | None = None,
    top_k: int = 5,
    iterations: int = 2,
    security_levels: list[str] | None = None,
    tenant_storage_isolation: bool | None = None,
    query_spec_source: Path | None = None,
    warm_runtime: bool = True,
    max_total_ms: float | None = None,
    max_warm_search_ms: float | None = None,
    min_warm_records: int | None = None,
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

    warmup_summary = None
    if warm_runtime:
        warmup_started_at = time.perf_counter()
        warmup_summary = warm_mcp_runtime(settings=settings, auth=auth)
        warmup_summary["external_elapsed_ms"] = _elapsed_ms(warmup_started_at)

    items = [
        _benchmark_query(
            settings=settings,
            auth=auth,
            query=str(spec["query"]),
            expect_no_evidence=bool(spec.get("expect_no_evidence") or spec.get("expected_no_evidence")),
            top_k=top_k,
            iterations=max(1, int(iterations or 1)),
            security_levels=levels,
            profile_id=profile_id,
        )
        for spec in selected_queries
    ]
    summary = _summarize_items(items)
    threshold_findings = _threshold_findings(
        items,
        max_total_ms=max_total_ms,
        max_warm_search_ms=max_warm_search_ms,
    )
    warmup_findings = _warmup_findings(warmup_summary, min_warm_records=min_warm_records)
    result_failures = _result_findings(items)
    findings = warmup_findings + result_failures + threshold_findings
    report = {
        "report_type": "mcp_query_benchmark",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "data_dir": str(data_dir),
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "tenant_storage_isolation": tenant_storage_isolation,
        "security_levels": levels,
        "top_k": top_k,
        "iterations": max(1, int(iterations or 1)),
        "min_warm_records": min_warm_records,
        "query_count": len(items),
        "warmup": warmup_summary,
        "summary": summary,
        "finding_count": len(findings),
        "findings": findings,
        "passed": not findings,
        "api_call_count": 0,
        "items": items,
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


def _benchmark_query(
    *,
    settings: Any,
    auth: Any,
    query: str,
    expect_no_evidence: bool,
    top_k: int,
    iterations: int,
    security_levels: list[str],
    profile_id: str | None,
) -> dict[str, Any]:
    measurements: list[dict[str, Any]] = []
    for index in range(iterations):
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
        answer = build_structured_extractive_answer(query, [_flatten_mcp_result(item) for item in fetched])
        answer_elapsed_ms = _elapsed_ms(answer_started_at)

        measurements.append(
            {
                "iteration": index + 1,
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
        )
    return {
        "query": query,
        "expect_no_evidence": expect_no_evidence,
        "iteration_count": len(measurements),
        "search_result_counts": [item["search_result_count"] for item in measurements],
        "fetch_result_counts": [item["fetch_result_count"] for item in measurements],
        "summary": _summarize_measurements(measurements),
        "measurements": measurements,
    }


def _flatten_mcp_result(result: dict[str, Any]) -> dict[str, Any]:
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    flattened = dict(metadata)
    flattened["id"] = result.get("id") or ""
    flattened["title"] = result.get("title") or ""
    flattened["url"] = result.get("url") or ""
    flattened["text"] = result.get("text") or ""
    return flattened


def _summarize_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    measurements = [measurement for item in items for measurement in item.get("measurements") or []]
    summary = _summarize_measurements(measurements)
    summary["measurement_count"] = len(measurements)
    summary["query_count"] = len(items)
    return summary


def _summarize_measurements(measurements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "search_elapsed_ms": _stats([item["search_elapsed_ms"] for item in measurements]),
        "fetch_elapsed_ms": _stats([item["fetch_elapsed_ms"] for item in measurements]),
        "answer_elapsed_ms": _stats([item["answer_elapsed_ms"] for item in measurements]),
        "total_elapsed_ms": _stats([item["total_elapsed_ms"] for item in measurements]),
        "warm_search_elapsed_ms": _stats(
            [item["search_elapsed_ms"] for item in measurements if int(item.get("iteration") or 0) > 1]
        ),
        "mcp_search_timing_summary": _summarize_mcp_search_timings(measurements),
    }


def _summarize_mcp_search_timings(measurements: list[dict[str, Any]]) -> dict[str, dict[str, float | int | None]]:
    timing_keys = sorted(
        {
            key
            for measurement in measurements
            for key, value in (measurement.get("mcp_search_timing_ms") or {}).items()
            if isinstance(value, (int, float))
        }
    )
    return {
        key: _stats(
            [
                float((measurement.get("mcp_search_timing_ms") or {}).get(key))
                for measurement in measurements
                if isinstance((measurement.get("mcp_search_timing_ms") or {}).get(key), (int, float))
            ]
        )
        for key in timing_keys
    }


def _stats(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "p50": None, "p95": None, "max": None, "avg": None}
    ordered = sorted(float(value) for value in values)
    return {
        "count": len(ordered),
        "min": round(ordered[0], 3),
        "p50": round(_percentile(ordered, 0.50), 3),
        "p95": round(_percentile(ordered, 0.95), 3),
        "max": round(ordered[-1], 3),
        "avg": round(sum(ordered) / len(ordered), 3),
    }


def _percentile(ordered_values: list[float], percentile: float) -> float:
    if len(ordered_values) == 1:
        return ordered_values[0]
    index = min(len(ordered_values) - 1, max(0, math.ceil(percentile * len(ordered_values)) - 1))
    return ordered_values[index]


def _threshold_findings(
    items: list[dict[str, Any]],
    *,
    max_total_ms: float | None,
    max_warm_search_ms: float | None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for item in items:
        total_max = ((item.get("summary") or {}).get("total_elapsed_ms") or {}).get("max")
        if max_total_ms and total_max is not None and float(total_max) > max_total_ms:
            findings.append(
                {
                    "code": "benchmark-total-threshold-exceeded",
                    "query": item.get("query"),
                    "actual_ms": total_max,
                    "threshold_ms": max_total_ms,
                }
            )
        warm_search_max = ((item.get("summary") or {}).get("warm_search_elapsed_ms") or {}).get("max")
        if max_warm_search_ms and warm_search_max is not None and float(warm_search_max) > max_warm_search_ms:
            findings.append(
                {
                    "code": "benchmark-warm-search-threshold-exceeded",
                    "query": item.get("query"),
                    "actual_ms": warm_search_max,
                    "threshold_ms": max_warm_search_ms,
                }
            )
    return findings


def _result_findings(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for item in items:
        search_result_counts = item.get("search_result_counts") or [0]
        if item.get("expect_no_evidence"):
            max_result_count = max(search_result_counts)
            if max_result_count > 0:
                findings.append(
                    {
                        "code": "benchmark-expected-no-evidence-returned-results",
                        "query": item.get("query"),
                        "max_search_result_count": max_result_count,
                        "detail": "Query is marked expect_no_evidence but at least one benchmark iteration returned search results.",
                    }
                )
            continue
        if min(search_result_counts) < 1:
            findings.append(
                {
                    "code": "benchmark-query-no-results",
                    "query": item.get("query"),
                    "detail": "At least one benchmark iteration returned no search results.",
                }
            )
    return findings


def _warmup_findings(
    warmup_summary: dict[str, Any] | None,
    *,
    min_warm_records: int | None,
) -> list[dict[str, Any]]:
    if min_warm_records is None or int(min_warm_records) <= 0:
        return []
    if not warmup_summary:
        return [
            {
                "code": "benchmark-warmup-required-for-record-threshold",
                "detail": "Warm runtime summary is required to verify the minimum warmed record count.",
                "threshold_record_count": int(min_warm_records),
            }
        ]
    record_count_available = bool(warmup_summary.get("warmed")) or bool(warmup_summary.get("record_count_available"))
    if not record_count_available:
        return [
            {
                "code": "benchmark-warmup-required-for-record-threshold",
                "detail": "Warm runtime summary is required to verify the minimum warmed record count.",
                "threshold_record_count": int(min_warm_records),
                "warmup_mode": warmup_summary.get("warmup_mode"),
                "skip_reason": warmup_summary.get("skip_reason"),
            }
        ]
    actual_record_count = int(warmup_summary.get("record_count") or 0)
    if actual_record_count >= int(min_warm_records):
        return []
    return [
        {
            "code": "benchmark-warm-record-count-below-minimum",
            "actual_record_count": actual_record_count,
            "threshold_record_count": int(min_warm_records),
        }
    ]


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 3)


def _to_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    total = summary.get("total_elapsed_ms") or {}
    warm_search = summary.get("warm_search_elapsed_ms") or {}
    warmup = report.get("warmup") or {}
    search_timing = summary.get("mcp_search_timing_summary") or {}
    lines = [
        "# MCP Query Benchmark",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Tenant: `{report.get('tenant_id')}`",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Queries: {report.get('query_count')}",
        f"- Iterations per query: {report.get('iterations')}",
        f"- Warm runtime records: {warmup.get('record_count')} / minimum {report.get('min_warm_records') or ''}",
        f"- Total p50/p95/max ms: {total.get('p50')} / {total.get('p95')} / {total.get('max')}",
        f"- Warm search p50/p95/max ms: {warm_search.get('p50')} / {warm_search.get('p95')} / {warm_search.get('max')}",
        f"- API calls: {report.get('api_call_count')}",
        "",
    ]
    if search_timing:
        lines.extend(
            [
                "## MCP Search Internal Timing",
                "",
                "| Stage | Count | p50 ms | p95 ms | max ms |",
                "| --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for stage in sorted(search_timing):
            stats = search_timing.get(stage) or {}
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"`{_md_cell(stage)}`",
                        _md_cell(stats.get("count")),
                        _md_cell(stats.get("p50")),
                        _md_cell(stats.get("p95")),
                        _md_cell(stats.get("max")),
                    ]
                )
                + " |"
            )
        lines.append("")
    lines.extend(
        [
        "| Query | Expected no evidence | Results | Fetches | Total max ms | Warm search max ms |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in report.get("items") or []:
        item_summary = item.get("summary") or {}
        item_total = item_summary.get("total_elapsed_ms") or {}
        item_warm_search = item_summary.get("warm_search_elapsed_ms") or {}
        lines.append(
            "| "
            + " | ".join(
                [
                    _md_cell(item.get("query")),
                    _md_cell(str(bool(item.get("expect_no_evidence"))).lower()),
                    _md_cell(max(item.get("search_result_counts") or [0])),
                    _md_cell(max(item.get("fetch_result_counts") or [0])),
                    _md_cell(item_total.get("max")),
                    _md_cell(item_warm_search.get("max")),
                ]
            )
            + " |"
        )
    if report.get("findings"):
        lines.extend(["", "## Findings", ""])
        for finding in report["findings"]:
            lines.append(f"- `{finding.get('code')}` {finding.get('query') or ''}: {_md_cell(finding)}")
    return "\n".join(lines).rstrip() + "\n"


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def load_query_specs(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("queries") or payload.get("items") or []
    if not isinstance(payload, list):
        raise ValueError("--query-spec-json must contain a list or an object with queries/items.")
    return [item for item in payload if isinstance(item, dict)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark approved local MCP search/fetch/answer query performance.")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--profile-id", default=None, help="Institution profile scope when a tenant has multiple profiles.")
    parser.add_argument("--query", action="append", default=[])
    parser.add_argument("--query-spec-json", default=None)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--iterations", type=int, default=2)
    parser.add_argument("--security-level", action="append", default=["internal"])
    parser.add_argument("--tenant-storage-isolation", action="store_true")
    parser.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--skip-warm-runtime", action="store_true")
    parser.add_argument("--max-total-ms", type=float, default=None)
    parser.add_argument("--max-warm-search-ms", type=float, default=None)
    parser.add_argument("--min-warm-records", type=int, default=None)
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
    report = benchmark_mcp_queries(
        data_dir=Path(args.data_dir),
        tenant_id=args.tenant_id,
        profile_id=args.profile_id,
        queries=args.query or None,
        query_specs=query_specs,
        top_k=args.top_k,
        iterations=args.iterations,
        security_levels=args.security_level,
        tenant_storage_isolation=tenant_storage_isolation,
        query_spec_source=query_spec_source,
        warm_runtime=not args.skip_warm_runtime,
        max_total_ms=args.max_total_ms,
        max_warm_search_ms=args.max_warm_search_ms,
        min_warm_records=args.min_warm_records,
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
