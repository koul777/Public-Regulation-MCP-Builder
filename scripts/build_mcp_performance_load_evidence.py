from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_metadata import current_repo_commit

HASH_CHUNK_BYTES = 1024 * 1024


def build_mcp_performance_load_evidence(
    *,
    query_benchmark_report: Path,
    transport_smoke_report: Path,
    index_visibility_report: Path,
    approved_vectors_jsonl: Path,
    bm25_index_json: Path,
    min_warm_records: int | None = None,
    max_total_p95_ms: float | None = None,
    max_warm_search_p95_ms: float | None = None,
    max_transport_warm_search_ms: float | None = None,
    require_latency_slo: bool = False,
    require_visibility_match: bool = True,
    require_no_smoke_docs: bool = True,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    benchmark = _load_json_artifact(query_benchmark_report, role="query_benchmark")
    transport = _load_json_artifact(transport_smoke_report, role="transport_smoke")
    visibility = _load_json_artifact(index_visibility_report, role="index_visibility")
    vector_file = _jsonl_file_summary(approved_vectors_jsonl, role="approved_vectors")
    bm25_file = _bm25_file_summary(bm25_index_json, role="bm25_index")

    findings: list[dict[str, Any]] = [
        *benchmark["findings"],
        *transport["findings"],
        *visibility["findings"],
        *vector_file["findings"],
        *bm25_file["findings"],
    ]

    benchmark_summary = _benchmark_summary(benchmark["payload"])
    transport_summary = _transport_summary(transport["payload"])
    visibility_summary = _visibility_summary(visibility["payload"])
    file_summary = {
        "approved_vectors": vector_file["summary"],
        "bm25_index": bm25_file["summary"],
    }

    findings.extend(
        _benchmark_findings(
            benchmark["payload"],
            summary=benchmark_summary,
            min_warm_records=min_warm_records,
            max_total_p95_ms=max_total_p95_ms,
            max_warm_search_p95_ms=max_warm_search_p95_ms,
        )
    )
    findings.extend(
        _transport_findings(
            transport["payload"],
            summary=transport_summary,
            max_transport_warm_search_ms=max_transport_warm_search_ms,
        )
    )
    findings.extend(
        _visibility_findings(
            visibility["payload"],
            summary=visibility_summary,
            require_visibility_match=require_visibility_match,
            require_no_smoke_docs=require_no_smoke_docs,
        )
    )
    findings.extend(
        _record_count_findings(
            benchmark_summary=benchmark_summary,
            visibility_summary=visibility_summary,
            vector_summary=vector_file["summary"],
            bm25_summary=bm25_file["summary"],
        )
    )

    latency_thresholds = {
        "max_total_p95_ms": max_total_p95_ms,
        "max_warm_search_p95_ms": max_warm_search_p95_ms,
        "max_transport_warm_search_ms": max_transport_warm_search_ms,
    }
    missing_latency_thresholds = [
        name for name, value in latency_thresholds.items() if value is None
    ]
    latency_slo_evaluated = not missing_latency_thresholds
    if require_latency_slo and missing_latency_thresholds:
        findings.append(
            _finding(
                "blocker",
                "latency-slo-thresholds-missing",
                "Release performance evidence requires all latency SLO thresholds.",
                missing_thresholds=missing_latency_thresholds,
            )
        )

    blocker_count = sum(1 for item in findings if item["severity"] == "blocker")
    warning_count = sum(1 for item in findings if item["severity"] == "warning")
    evidence_ready = blocker_count == 0 and warning_count == 0
    latency_violation_codes = {
        "query-benchmark-total-p95-too-high",
        "query-benchmark-warm-search-p95-too-high",
        "transport-warm-search-too-high",
    }
    latency_slo_passed = latency_slo_evaluated and not any(
        item.get("code") in latency_violation_codes for item in findings
    )
    report = {
        "report_type": "mcp_performance_load_evidence",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "passed": blocker_count == 0,
        "evidence_ready": evidence_ready,
        "performance_release_ready": evidence_ready and latency_slo_passed,
        "latency_slo": {
            "required": require_latency_slo,
            "evaluated": latency_slo_evaluated,
            "passed": latency_slo_passed,
            "missing_thresholds": missing_latency_thresholds,
            "claim_scope": (
                "functional_and_latency_slo"
                if latency_slo_evaluated
                else "functional_evidence_only_no_latency_slo"
            ),
        },
        "blocking_count": blocker_count,
        "warning_count": warning_count,
        "finding_count": len(findings),
        "findings": findings,
        "thresholds": {
            "min_warm_records": min_warm_records,
            "max_total_p95_ms": max_total_p95_ms,
            "max_warm_search_p95_ms": max_warm_search_p95_ms,
            "max_transport_warm_search_ms": max_transport_warm_search_ms,
            "require_visibility_match": require_visibility_match,
            "require_no_smoke_docs": require_no_smoke_docs,
        },
        "source_reports": {
            "query_benchmark_report": str(query_benchmark_report),
            "transport_smoke_report": str(transport_smoke_report),
            "index_visibility_report": str(index_visibility_report),
            "approved_vectors_jsonl": str(approved_vectors_jsonl),
            "bm25_index_json": str(bm25_index_json),
        },
        "query_benchmark_summary": benchmark_summary,
        "transport_smoke_summary": transport_summary,
        "index_visibility_summary": visibility_summary,
        "file_summary": file_summary,
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _load_json_artifact(path: Path, *, role: str) -> dict[str, Any]:
    summary = {
        "role": role,
        "path": str(path),
        "exists": path.is_file(),
        "byte_count": path.stat().st_size if path.is_file() else 0,
        "sha256": _sha256(path) if path.is_file() else "",
    }
    if not path.is_file():
        return {
            "summary": summary,
            "payload": {},
            "findings": [_finding("blocker", f"{role}-missing", f"{path} is missing.")],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "summary": summary,
            "payload": {},
            "findings": [_finding("blocker", f"{role}-parse-error", str(exc))],
        }
    summary["report_type"] = str(payload.get("report_type") or "")
    return {"summary": summary, "payload": payload, "findings": []}


def _jsonl_file_summary(path: Path, *, role: str) -> dict[str, Any]:
    summary = {
        "role": role,
        "path": str(path),
        "exists": path.is_file(),
        "byte_count": path.stat().st_size if path.is_file() else 0,
        "sha256": "",
        "record_count": 0,
    }
    if not path.is_file():
        return {
            "summary": summary,
            "findings": [_finding("blocker", f"{role}-missing", f"{path} is missing.")],
        }
    digest = hashlib.sha256()
    record_count = 0
    with path.open("rb") as handle:
        for line in handle:
            digest.update(line)
            if line.strip():
                record_count += 1
    summary["sha256"] = digest.hexdigest()
    summary["record_count"] = record_count
    return {"summary": summary, "findings": []}


def _bm25_file_summary(path: Path, *, role: str) -> dict[str, Any]:
    base = _load_json_artifact(path, role=role)
    summary = dict(base["summary"])
    payload = base["payload"]
    if payload:
        documents = payload.get("documents") if isinstance(payload.get("documents"), list) else []
        document_frequencies = (
            payload.get("document_frequencies") if isinstance(payload.get("document_frequencies"), dict) else {}
        )
        summary.update(
            {
                "index_version": str(payload.get("index_version") or ""),
                "retrieval_model": str(payload.get("retrieval_model") or ""),
                "tokenizer": str(payload.get("tokenizer") or ""),
                "document_count": _int(payload.get("document_count")) or len(documents),
                "documents_array_count": len(documents),
                "document_frequency_count": len(document_frequencies),
            }
        )
    return {"summary": summary, "findings": base["findings"]}


def _benchmark_summary(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {}
    summary = _dict(report.get("summary"))
    total = _dict(summary.get("total_elapsed_ms"))
    warm_search = _dict(summary.get("warm_search_elapsed_ms"))
    warmup = _dict(report.get("warmup"))
    return {
        "report_type": str(report.get("report_type") or ""),
        "passed": bool(report.get("passed")),
        "finding_count": _int(report.get("finding_count")),
        "query_count": _int(report.get("query_count")),
        "iterations": _int(report.get("iterations")),
        "measurement_count": _int(summary.get("measurement_count")),
        "warm_record_count": _int(warmup.get("record_count")),
        "bm25_index_ready": bool(warmup.get("bm25_index_ready")),
        "reported_min_warm_records": _int(report.get("min_warm_records")),
        "total_p50_ms": _optional_float(total.get("p50")),
        "total_p95_ms": _optional_float(total.get("p95")),
        "total_max_ms": _optional_float(total.get("max")),
        "warm_search_p50_ms": _optional_float(warm_search.get("p50")),
        "warm_search_p95_ms": _optional_float(warm_search.get("p95")),
        "warm_search_max_ms": _optional_float(warm_search.get("max")),
        "api_call_count": _int(report.get("api_call_count")),
        "query_spec_sha256": str(report.get("query_spec_sha256") or ""),
    }


def _transport_summary(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {}
    profiles = {
        name: _transport_profile_summary(_dict(report.get(name)))
        for name in ("full_profile", "chatgpt_data_profile")
        if report.get(name)
    }
    warm_values = [
        value
        for profile in profiles.values()
        for value in [profile.get("warm_search_elapsed_ms")]
        if isinstance(value, (int, float))
    ]
    return {
        "report_type": str(report.get("report_type") or ""),
        "passed": bool(report.get("passed")),
        "tenant_id": str(report.get("tenant_id") or ""),
        "tenant_storage_isolation": report.get("tenant_storage_isolation"),
        "transport": str(report.get("transport") or ""),
        "profile_count": len(profiles),
        "max_warm_search_elapsed_ms": max(warm_values) if warm_values else None,
        "profiles": profiles,
    }


def _transport_profile_summary(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "passed": bool(profile.get("passed")),
        "tool_profile": str(profile.get("tool_profile") or ""),
        "search_result_count": _int(profile.get("search_result_count")),
        "warm_search_result_count": _int(profile.get("warm_search_result_count")),
        "fetch_has_text": bool(profile.get("fetch_has_text")),
        "list_tools_elapsed_ms": _optional_float(profile.get("list_tools_elapsed_ms")),
        "search_elapsed_ms": _optional_float(profile.get("search_elapsed_ms")),
        "warm_search_elapsed_ms": _optional_float(profile.get("warm_search_elapsed_ms")),
        "fetch_elapsed_ms": _optional_float(profile.get("fetch_elapsed_ms")),
        "total_elapsed_ms": _optional_float(profile.get("total_elapsed_ms")),
    }


def _visibility_summary(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {}
    return {
        "report_type": str(report.get("report_type") or ""),
        "passed": bool(report.get("passed")),
        "tenant_id": str(report.get("tenant_id") or ""),
        "document_count": _int(report.get("document_count")),
        "total_approved_chunks": _int(report.get("total_approved_chunks")),
        "total_indexable_record_count": _int(report.get("total_indexable_record_count")),
        "total_mcp_visible_records": _int(report.get("total_mcp_visible_records")),
        "total_skipped_unapproved_count": _int(report.get("total_skipped_unapproved_count")),
        "smoke_like_document_count": _int(report.get("smoke_like_document_count")),
        "finding_count": _int(report.get("finding_count")),
    }


def _benchmark_findings(
    report: dict[str, Any],
    *,
    summary: dict[str, Any],
    min_warm_records: int | None,
    max_total_p95_ms: float | None,
    max_warm_search_p95_ms: float | None,
) -> list[dict[str, Any]]:
    if not report:
        return []
    findings: list[dict[str, Any]] = []
    if not report.get("passed"):
        findings.append(_finding("blocker", "query-benchmark-failed", "Query benchmark report did not pass."))
    if _int(summary.get("measurement_count")) <= 0:
        findings.append(
            _finding("blocker", "query-benchmark-measurements-missing", "Query benchmark has no measurements.")
        )
    effective_min_records = min_warm_records or summary.get("reported_min_warm_records")
    if effective_min_records and _int(summary.get("warm_record_count")) < int(effective_min_records):
        findings.append(
            _finding(
                "blocker",
                "query-benchmark-warm-record-count-low",
                "Warm benchmark record count is below the configured minimum.",
                actual_record_count=summary.get("warm_record_count"),
                threshold_record_count=int(effective_min_records),
            )
        )
    if max_total_p95_ms is not None:
        actual_total_p95 = summary.get("total_p95_ms")
        if not isinstance(actual_total_p95, (int, float)):
            findings.append(
                _finding("blocker", "query-benchmark-total-p95-missing", "Query benchmark total p95 is missing.")
            )
        elif float(actual_total_p95) > max_total_p95_ms:
            findings.append(
                _finding(
                    "blocker",
                    "query-benchmark-total-p95-too-high",
                    "Query benchmark total p95 exceeded the configured threshold.",
                    actual_ms=actual_total_p95,
                    threshold_ms=max_total_p95_ms,
                )
            )
    if max_warm_search_p95_ms is not None:
        actual_warm_p95 = summary.get("warm_search_p95_ms")
        if not isinstance(actual_warm_p95, (int, float)):
            findings.append(
                _finding(
                    "blocker",
                    "query-benchmark-warm-search-p95-missing",
                    "Query benchmark warm-search p95 is missing.",
                )
            )
        elif float(actual_warm_p95) > max_warm_search_p95_ms:
            findings.append(
                _finding(
                    "blocker",
                    "query-benchmark-warm-search-p95-too-high",
                    "Query benchmark warm-search p95 exceeded the configured threshold.",
                    actual_ms=actual_warm_p95,
                    threshold_ms=max_warm_search_p95_ms,
                )
            )
    if _int(summary.get("api_call_count")) > 0:
        findings.append(_finding("warning", "query-benchmark-api-calls", "Benchmark recorded external API calls."))
    return findings


def _transport_findings(
    report: dict[str, Any],
    *,
    summary: dict[str, Any],
    max_transport_warm_search_ms: float | None,
) -> list[dict[str, Any]]:
    if not report:
        return []
    findings: list[dict[str, Any]] = []
    if not report.get("passed"):
        findings.append(_finding("blocker", "transport-smoke-failed", "Transport smoke report did not pass."))
    for name, profile in (summary.get("profiles") or {}).items():
        if not profile.get("passed"):
            findings.append(
                _finding("blocker", "transport-profile-failed", f"Transport profile {name} did not pass.")
            )
    actual = summary.get("max_warm_search_elapsed_ms")
    if max_transport_warm_search_ms is not None:
        if not isinstance(actual, (int, float)):
            findings.append(
                _finding("blocker", "transport-warm-search-missing", "Transport warm-search latency is missing.")
            )
        elif float(actual) > max_transport_warm_search_ms:
            findings.append(
                _finding(
                    "blocker",
                    "transport-warm-search-too-high",
                    "Transport smoke warm-search elapsed time exceeded the configured threshold.",
                    actual_ms=actual,
                    threshold_ms=max_transport_warm_search_ms,
                )
            )
    return findings


def _visibility_findings(
    report: dict[str, Any],
    *,
    summary: dict[str, Any],
    require_visibility_match: bool,
    require_no_smoke_docs: bool,
) -> list[dict[str, Any]]:
    if not report:
        return []
    findings: list[dict[str, Any]] = []
    if not report.get("passed"):
        findings.append(_finding("blocker", "index-visibility-failed", "Index visibility report did not pass."))
    indexable = _int(summary.get("total_indexable_record_count"))
    visible = _int(summary.get("total_mcp_visible_records"))
    if require_visibility_match and indexable != visible:
        findings.append(
            _finding(
                "blocker",
                "index-visible-record-count-mismatch",
                "MCP-visible record count does not match indexable record count.",
                indexable_record_count=indexable,
                visible_record_count=visible,
            )
        )
    smoke_docs = _int(summary.get("smoke_like_document_count"))
    if require_no_smoke_docs and smoke_docs > 0:
        findings.append(
            _finding(
                "blocker",
                "index-visibility-smoke-documents-present",
                "Smoke-like documents are present in the visibility report.",
                smoke_like_document_count=smoke_docs,
            )
        )
    return findings


def _record_count_findings(
    *,
    benchmark_summary: dict[str, Any],
    visibility_summary: dict[str, Any],
    vector_summary: dict[str, Any],
    bm25_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    counts = {
        "benchmark_warm_records": _int(benchmark_summary.get("warm_record_count")),
        "visibility_indexable_records": _int(visibility_summary.get("total_indexable_record_count")),
        "visibility_mcp_visible_records": _int(visibility_summary.get("total_mcp_visible_records")),
        "approved_vector_jsonl_records": _int(vector_summary.get("record_count")),
        "bm25_document_count": _int(bm25_summary.get("document_count")),
    }
    comparable = {key: value for key, value in counts.items() if value > 0}
    if len(set(comparable.values())) <= 1:
        return []
    return [
        _finding(
            "blocker",
            "large-runtime-record-count-mismatch",
            "Large-runtime evidence record counts do not agree.",
            **counts,
        )
    ]


def _finding(severity: str, code: str, detail: str, **extra: Any) -> dict[str, Any]:
    item = {"severity": severity, "code": code, "detail": detail}
    item.update(extra)
    return item


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _to_markdown(report: dict[str, Any]) -> str:
    benchmark = report.get("query_benchmark_summary") or {}
    transport = report.get("transport_smoke_summary") or {}
    visibility = report.get("index_visibility_summary") or {}
    files = report.get("file_summary") or {}
    vector = files.get("approved_vectors") or {}
    bm25 = files.get("bm25_index") or {}
    lines = [
        "# MCP Performance Load Evidence",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Evidence ready: `{str(report.get('evidence_ready')).lower()}`",
        f"- Performance release ready: `{str(report.get('performance_release_ready')).lower()}`",
        f"- Latency SLO evaluated: `{str((report.get('latency_slo') or {}).get('evaluated')).lower()}`",
        f"- Blocking: {report.get('blocking_count')}",
        f"- Warnings: {report.get('warning_count')}",
        f"- Query benchmark total p95/max ms: {benchmark.get('total_p95_ms')} / {benchmark.get('total_max_ms')}",
        f"- Query benchmark warm records: {benchmark.get('warm_record_count')} / min {report.get('thresholds', {}).get('min_warm_records') or benchmark.get('reported_min_warm_records')}",
        f"- Transport max warm-search ms: {transport.get('max_warm_search_elapsed_ms')}",
        f"- Visibility indexable/visible records: {visibility.get('total_indexable_record_count')} / {visibility.get('total_mcp_visible_records')}",
        f"- Approved vector JSONL: {vector.get('record_count')} records / {vector.get('byte_count')} bytes",
        f"- BM25 index: {bm25.get('document_count')} documents / {bm25.get('byte_count')} bytes / model `{bm25.get('retrieval_model')}`",
        "",
        "## Findings",
        "",
    ]
    if report.get("findings"):
        lines.extend(f"- `{item.get('severity')}` `{item.get('code')}`: {item.get('detail')}" for item in report["findings"])
    else:
        lines.append("- None")
    return "\n".join(lines).rstrip() + "\n"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compose MCP performance/load evidence from benchmark, transport, visibility, and local index files."
    )
    parser.add_argument("--query-benchmark-report", required=True)
    parser.add_argument("--transport-smoke-report", required=True)
    parser.add_argument("--index-visibility-report", required=True)
    parser.add_argument("--approved-vectors-jsonl", required=True)
    parser.add_argument("--bm25-index-json", required=True)
    parser.add_argument("--min-warm-records", type=int, default=None)
    parser.add_argument("--max-total-p95-ms", type=float, default=None)
    parser.add_argument("--max-warm-search-p95-ms", type=float, default=None)
    parser.add_argument("--max-transport-warm-search-ms", type=float, default=None)
    parser.add_argument(
        "--require-latency-slo",
        action="store_true",
        help="Fail closed unless all three latency thresholds are configured and pass.",
    )
    parser.add_argument("--allow-visibility-mismatch", action="store_true")
    parser.add_argument("--allow-smoke-docs", action="store_true")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    if stdout is sys.stdout and hasattr(stdout, "reconfigure"):
        stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    report = build_mcp_performance_load_evidence(
        query_benchmark_report=Path(args.query_benchmark_report),
        transport_smoke_report=Path(args.transport_smoke_report),
        index_visibility_report=Path(args.index_visibility_report),
        approved_vectors_jsonl=Path(args.approved_vectors_jsonl),
        bm25_index_json=Path(args.bm25_index_json),
        min_warm_records=args.min_warm_records,
        max_total_p95_ms=args.max_total_p95_ms,
        max_warm_search_p95_ms=args.max_warm_search_p95_ms,
        max_transport_warm_search_ms=args.max_transport_warm_search_ms,
        require_latency_slo=args.require_latency_slo,
        require_visibility_match=not args.allow_visibility_mismatch,
        require_no_smoke_docs=not args.allow_smoke_docs,
        out_json=Path(args.out_json) if args.out_json else None,
        out_md=Path(args.out_md) if args.out_md else None,
    )
    stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if args.fail_on_issue and not report["evidence_ready"]:
        return 2
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
