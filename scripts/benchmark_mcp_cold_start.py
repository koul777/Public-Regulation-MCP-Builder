from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.benchmark_mcp_queries import _stats
from scripts.report_metadata import current_repo_commit


def benchmark_mcp_cold_start(
    *,
    data_dir: Path,
    tenant_id: str,
    iterations: int = 3,
    tenant_storage_isolation: bool | None = None,
    min_record_count: int | None = None,
    max_process_elapsed_ms: float | None = None,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    measurements = [
        _run_child_warmup(
            data_dir=data_dir,
            tenant_id=tenant_id,
            tenant_storage_isolation=tenant_storage_isolation,
            iteration=index + 1,
        )
        for index in range(max(1, int(iterations or 1)))
    ]
    summary = _summarize_measurements(measurements)
    findings = _findings(
        measurements,
        min_record_count=min_record_count,
        max_process_elapsed_ms=max_process_elapsed_ms,
    )
    blocker_count = sum(1 for item in findings if item["severity"] == "blocker")
    report = {
        "report_type": "mcp_cold_start_benchmark",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "data_dir": str(data_dir),
        "tenant_id": tenant_id,
        "tenant_storage_isolation": tenant_storage_isolation,
        "iterations": max(1, int(iterations or 1)),
        "min_record_count": min_record_count,
        "max_process_elapsed_ms": max_process_elapsed_ms,
        "summary": summary,
        "finding_count": len(findings),
        "blocking_count": blocker_count,
        "findings": findings,
        "passed": blocker_count == 0,
        "api_call_count": 0,
        "measurements": measurements,
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _run_child_warmup(
    *,
    data_dir: Path,
    tenant_id: str,
    tenant_storage_isolation: bool | None,
    iteration: int,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--child-warmup",
        "--data-dir",
        str(data_dir),
        "--tenant-id",
        tenant_id,
    ]
    if tenant_storage_isolation is True:
        command.append("--tenant-storage-isolation")
    elif tenant_storage_isolation is False:
        command.append("--flat-storage")
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    started_at = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    process_elapsed_ms = _elapsed_ms(started_at)
    measurement: dict[str, Any] = {
        "iteration": iteration,
        "returncode": completed.returncode,
        "process_elapsed_ms": process_elapsed_ms,
        "stderr_tail": _tail(completed.stderr),
    }
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        measurement["parse_error"] = str(exc)
        measurement["stdout_tail"] = _tail(completed.stdout)
        payload = {}
    measurement["warmup"] = payload
    measurement["record_count"] = int(payload.get("record_count") or 0)
    measurement["bm25_index_ready"] = bool(payload.get("bm25_index_ready"))
    timing = payload.get("timing_ms") if isinstance(payload.get("timing_ms"), dict) else {}
    measurement["warmup_total_elapsed_ms"] = timing.get("total_elapsed_ms")
    measurement["load_vector_records_elapsed_ms"] = timing.get("load_vector_records_elapsed_ms")
    measurement["approval_snapshot_elapsed_ms"] = timing.get("approval_snapshot_elapsed_ms")
    measurement["bm25_index_elapsed_ms"] = timing.get("bm25_index_elapsed_ms")
    measurement["scoring_warmup_elapsed_ms"] = timing.get("scoring_warmup_elapsed_ms")
    return measurement


def _child_warmup(
    *,
    data_dir: Path,
    tenant_id: str,
    tenant_storage_isolation: bool | None,
    stdout: TextIO,
) -> int:
    from app.mcp_server.regulation_tools import mcp_auth_context, settings_for_mcp_project, warm_mcp_runtime

    settings = settings_for_mcp_project(
        data_dir=data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
    )
    auth = mcp_auth_context(tenant_id=tenant_id)
    stdout.write(json.dumps(warm_mcp_runtime(settings=settings, auth=auth), ensure_ascii=False) + "\n")
    return 0


def _summarize_measurements(measurements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "measurement_count": len(measurements),
        "successful_count": sum(1 for item in measurements if int(item.get("returncode") or 0) == 0 and not item.get("parse_error")),
        "record_count_min": min([int(item.get("record_count") or 0) for item in measurements] or [0]),
        "record_count_max": max([int(item.get("record_count") or 0) for item in measurements] or [0]),
        "process_elapsed_ms": _stats(
            [float(item["process_elapsed_ms"]) for item in measurements if isinstance(item.get("process_elapsed_ms"), (int, float))]
        ),
        "warmup_total_elapsed_ms": _stats(_numeric_values(measurements, "warmup_total_elapsed_ms")),
        "load_vector_records_elapsed_ms": _stats(_numeric_values(measurements, "load_vector_records_elapsed_ms")),
        "approval_snapshot_elapsed_ms": _stats(_numeric_values(measurements, "approval_snapshot_elapsed_ms")),
        "bm25_index_elapsed_ms": _stats(_numeric_values(measurements, "bm25_index_elapsed_ms")),
        "scoring_warmup_elapsed_ms": _stats(_numeric_values(measurements, "scoring_warmup_elapsed_ms")),
    }


def _numeric_values(measurements: list[dict[str, Any]], key: str) -> list[float]:
    return [float(item[key]) for item in measurements if isinstance(item.get(key), (int, float))]


def _findings(
    measurements: list[dict[str, Any]],
    *,
    min_record_count: int | None,
    max_process_elapsed_ms: float | None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for item in measurements:
        iteration = int(item.get("iteration") or 0)
        if int(item.get("returncode") or 0) != 0:
            findings.append(
                _finding(
                    "blocker",
                    "cold-start-child-failed",
                    "Cold-start child process returned a non-zero exit code.",
                    iteration=iteration,
                    returncode=item.get("returncode"),
                    stderr_tail=item.get("stderr_tail"),
                )
            )
        if item.get("parse_error"):
            findings.append(
                _finding(
                    "blocker",
                    "cold-start-child-output-invalid",
                    "Cold-start child process did not write valid JSON.",
                    iteration=iteration,
                    parse_error=item.get("parse_error"),
                )
            )
        if min_record_count and int(item.get("record_count") or 0) < int(min_record_count):
            findings.append(
                _finding(
                    "blocker",
                    "cold-start-record-count-below-minimum",
                    "Cold-start warmup record count is below the configured minimum.",
                    iteration=iteration,
                    actual_record_count=item.get("record_count"),
                    threshold_record_count=int(min_record_count),
                )
            )
        if not bool(item.get("bm25_index_ready")):
            findings.append(
                _finding(
                    "blocker",
                    "cold-start-bm25-index-not-ready",
                    "Cold-start warmup did not load a BM25 index.",
                    iteration=iteration,
                )
            )
        if (
            max_process_elapsed_ms is not None
            and isinstance(item.get("process_elapsed_ms"), (int, float))
            and float(item["process_elapsed_ms"]) > max_process_elapsed_ms
        ):
            findings.append(
                _finding(
                    "blocker",
                    "cold-start-process-elapsed-too-high",
                    "Cold-start process elapsed time exceeded the configured threshold.",
                    iteration=iteration,
                    actual_ms=item.get("process_elapsed_ms"),
                    threshold_ms=max_process_elapsed_ms,
                )
            )
    return findings


def _finding(severity: str, code: str, detail: str, **extra: Any) -> dict[str, Any]:
    item = {"severity": severity, "code": code, "detail": detail}
    item.update(extra)
    return item


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 3)


def _tail(text: str | None, *, limit: int = 1200) -> str:
    if not text:
        return ""
    return text[-limit:]


def _to_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    process = summary.get("process_elapsed_ms") or {}
    warmup = summary.get("warmup_total_elapsed_ms") or {}
    lines = [
        "# MCP Cold Start Benchmark",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Iterations: {report.get('iterations')}",
        f"- Successful: {summary.get('successful_count')} / {summary.get('measurement_count')}",
        f"- Record count min/max: {summary.get('record_count_min')} / {summary.get('record_count_max')}",
        f"- Process elapsed p50/p95/max ms: {process.get('p50')} / {process.get('p95')} / {process.get('max')}",
        f"- Warmup elapsed p50/p95/max ms: {warmup.get('p50')} / {warmup.get('p95')} / {warmup.get('max')}",
        "",
        "## Findings",
        "",
    ]
    if report.get("findings"):
        lines.extend(f"- `{item.get('severity')}` `{item.get('code')}`: {item.get('detail')}" for item in report["findings"])
    else:
        lines.append("- None")
    return "\n".join(lines).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark MCP warmup cold-start distribution in fresh Python child processes.")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--tenant-storage-isolation", action="store_true")
    parser.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--min-record-count", type=int, default=None)
    parser.add_argument("--max-process-elapsed-ms", type=float, default=None)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--fail-on-threshold", action="store_true")
    parser.add_argument("--child-warmup", action="store_true", help=argparse.SUPPRESS)
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
    if args.child_warmup:
        return _child_warmup(
            data_dir=Path(args.data_dir),
            tenant_id=args.tenant_id,
            tenant_storage_isolation=tenant_storage_isolation,
            stdout=stdout,
        )
    report = benchmark_mcp_cold_start(
        data_dir=Path(args.data_dir),
        tenant_id=args.tenant_id,
        iterations=args.iterations,
        tenant_storage_isolation=tenant_storage_isolation,
        min_record_count=args.min_record_count,
        max_process_elapsed_ms=args.max_process_elapsed_ms,
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
