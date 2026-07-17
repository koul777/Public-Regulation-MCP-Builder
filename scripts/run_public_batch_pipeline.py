from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from app.core.config import Settings
from app.core.institution_profiles import load_institution_profile_registry
from app.schemas.chunk import ChunkOptions
from scripts.batch_process_regulations import (
    DEFAULT_GLOB_PATTERNS,
    DEFAULT_PROFILE_ID,
    apply_institution_profile_defaults,
    attach_institution_profile_registry_summary,
    collect_input_files,
    load_manifest_entries,
    parse_patterns,
    process_entries,
    write_reports,
)
from scripts.emit_batch_failure_alert import emit_batch_failure_alert
from scripts.export_batch_retry_manifest import export_retry_manifest
from scripts.export_ocr_manifest import export_ocr_manifest
from scripts.export_public_batch_report import export_public_batch_report, to_csv as public_to_csv, to_markdown as public_to_md
from scripts.validate_public_batch_readiness import to_markdown as readiness_to_md, validate_public_batch_readiness


def run_public_batch_pipeline(
    *,
    inputs: Sequence[str | Path] = (),
    manifest_csv: Path | None = None,
    patterns: str = ",".join(DEFAULT_GLOB_PATTERNS),
    recursive: bool = True,
    profile_id: str = DEFAULT_PROFILE_ID,
    institution_name: str | None = None,
    source_system: str | None = "PUBLIC_PORTAL",
    source_url: str | None = None,
    institution_profiles: Path | None = Path("config/institution_profiles.example.json"),
    strict_institution_profiles: bool = True,
    quality_profiles: Path | None = Path("config/quality_profiles.example.json"),
    strict_quality_profiles: bool = True,
    reports_dir: Path = Path("reports"),
    data_dir: Path = Path("data"),
    max_upload_mb: int | None = 1000,
    max_chunk_chars: int = 1800,
    min_chunk_chars: int = 300,
    overlap_chars: int = 120,
    chunk_mode: str = "article",
    include_context_header: bool = True,
    enable_agent_review: bool = True,
    force_reprocess: bool = False,
    webhook_url: str | None = None,
    alert_log: Path | None = Path("reports/batch_failure_alerts.jsonl"),
    include_local_paths_in_alert: bool = False,
    ocr_price_per_page: float | None = 0.03,
    fail_on_alert: bool = False,
    fail_on_readiness: bool = False,
    timestamp: str | None = None,
) -> dict[str, Any]:
    if not inputs and manifest_csv is None:
        raise ValueError("Provide at least one input path or --manifest-csv.")

    reports_dir = Path(reports_dir)
    data_dir = Path(data_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = timestamp or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    registry = load_institution_profile_registry(str(institution_profiles)) if institution_profiles else None
    base_profile = registry.resolve(profile_id, strict=strict_institution_profiles) if registry else None
    resolved_max_upload_mb = max_upload_mb or (base_profile.max_upload_mb if base_profile else None) or 500
    resolved_institution_name = institution_name or (base_profile.institution_name if base_profile else None)
    resolved_source_system = source_system or (base_profile.source_system if base_profile else None)
    resolved_source_url = source_url or (base_profile.source_url if base_profile else None)

    settings_kwargs: dict[str, Any] = {
        "data_dir": data_dir,
        "max_upload_mb": resolved_max_upload_mb,
    }
    if quality_profiles:
        settings_kwargs["quality_profiles_path"] = str(quality_profiles)
    if strict_quality_profiles:
        settings_kwargs["quality_profiles_strict"] = True
    settings = Settings(**settings_kwargs)
    chunk_options = ChunkOptions(
        max_chunk_chars=max_chunk_chars,
        min_chunk_chars=min_chunk_chars,
        overlap_chars=overlap_chars,
        chunk_mode=chunk_mode,
        include_context_header=include_context_header,
        enable_agent_review=True,
    )

    entries = _load_entries(
        inputs=inputs,
        manifest_csv=manifest_csv,
        patterns=patterns,
        recursive=recursive,
        institution_name=resolved_institution_name,
        source_system=resolved_source_system,
        source_url=resolved_source_url,
        profile_id=profile_id,
    )
    entries = apply_institution_profile_defaults(entries, registry, strict=strict_institution_profiles)

    batch_summary = process_entries(
        entries,
        settings=settings,
        chunk_options=chunk_options,
        force_reprocess=force_reprocess,
    )
    attach_institution_profile_registry_summary(batch_summary, registry)
    batch_reports = write_reports(batch_summary, reports_dir)
    batch_report_path = Path(batch_reports["json"])

    public_reports = _write_public_report_artifacts(
        batch_summary,
        source_report_path=batch_report_path,
        reports_dir=reports_dir,
        timestamp=timestamp,
    )
    retry_json = reports_dir / f"batch_retry_manifest_{timestamp}.json"
    retry_csv = reports_dir / f"batch_retry_manifest_{timestamp}.csv"
    retry_report = export_retry_manifest(
        batch_report_path,
        out_csv=retry_csv,
        out_json=retry_json,
        require_existing_files=True,
    )
    ocr_json = reports_dir / f"ocr_manifest_{timestamp}.json"
    ocr_csv = reports_dir / f"ocr_manifest_{timestamp}.csv"
    ocr_report = export_ocr_manifest(
        batch_report_path,
        out_csv=ocr_csv,
        out_json=ocr_json,
        price_per_page=ocr_price_per_page,
    )
    readiness_report = validate_public_batch_readiness(
        batch_summary,
        batch_report_path=batch_report_path,
        institution_profile_registry=registry,
        strict_institution_profiles=strict_institution_profiles,
    )
    readiness_json = reports_dir / f"public_batch_readiness_{timestamp}.json"
    readiness_md = reports_dir / f"public_batch_readiness_{timestamp}.md"
    readiness_json.write_text(json.dumps(readiness_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    readiness_md.write_text(readiness_to_md(readiness_report), encoding="utf-8")

    alert_json = reports_dir / f"batch_failure_alert_{timestamp}.json"
    alert = emit_batch_failure_alert(
        batch_report_path,
        readiness_report_path=readiness_json,
        out_json=alert_json,
        alert_log=alert_log,
        webhook_url=webhook_url,
        include_local_paths=include_local_paths_in_alert,
    )

    blockers = _pipeline_blockers(
        public_report=public_reports["report"],
        readiness_report=readiness_report,
        alert=alert,
        batch_summary=batch_summary,
        fail_on_alert=fail_on_alert,
        fail_on_readiness=fail_on_readiness,
    )
    result = {
        "report_type": "public_batch_pipeline",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "blocked" if blockers else "completed",
        "passed": not blockers,
        "blocker_count": len(blockers),
        "blockers": blockers,
        "summary": {
            "input_count": batch_summary.get("input_count", 0),
            "successful_count": batch_summary.get("successful_count", 0),
            "failed_count": batch_summary.get("failed_count", 0),
            "ocr_required_count": batch_summary.get("ocr_required_count", 0),
            "retryable_count": retry_report.get("retryable_count", 0),
            "readiness_passed": readiness_report.get("passed"),
            "alert_status": alert.get("status"),
            "alert_severity": alert.get("severity"),
            "public_report_path_leak_count": public_reports["report"]
            .get("sanitization", {})
            .get("sensitive_path_leak_count", 0),
            "source_selection_warning_count": alert.get("summary", {}).get("source_selection_warning_count", 0),
        },
        "artifacts": {
            "batch_report_json": str(batch_report_path),
            "batch_report_csv": batch_reports.get("csv"),
            "batch_report_markdown": batch_reports.get("markdown"),
            "public_report_json": str(public_reports["json"]),
            "public_report_csv": str(public_reports["csv"]),
            "public_report_markdown": str(public_reports["markdown"]),
            "retry_manifest_json": str(retry_json),
            "retry_manifest_csv": str(retry_csv),
            "ocr_manifest_json": str(ocr_json),
            "ocr_manifest_csv": str(ocr_csv),
            "readiness_json": str(readiness_json),
            "readiness_markdown": str(readiness_md),
            "alert_json": str(alert_json),
            "alert_log": str(alert_log) if alert_log else None,
        },
        "safety_note": (
            "This pipeline preprocesses, exports public reports, validates readiness, and emits alerts. "
            "It does not approve chunks, index Vector DB records, or publish an MCP runtime."
        ),
        "api_call_count": 0,
    }
    pipeline_json = reports_dir / f"public_batch_pipeline_{timestamp}.json"
    pipeline_md = reports_dir / f"public_batch_pipeline_{timestamp}.md"
    pipeline_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pipeline_md.write_text(_pipeline_to_markdown(result), encoding="utf-8")
    result["artifacts"]["pipeline_json"] = str(pipeline_json)
    result["artifacts"]["pipeline_markdown"] = str(pipeline_md)
    return result


def _load_entries(
    *,
    inputs: Sequence[str | Path],
    manifest_csv: Path | None,
    patterns: str,
    recursive: bool,
    institution_name: str | None,
    source_system: str | None,
    source_url: str | None,
    profile_id: str,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if manifest_csv:
        entries.extend(
            load_manifest_entries(
                Path(manifest_csv),
                institution_name=institution_name,
                source_system=source_system,
                source_url=source_url,
                profile_id=profile_id,
            )
        )
    if inputs:
        files = collect_input_files(inputs, patterns=parse_patterns(patterns), recursive=recursive)
        entries.extend(
            {
                "path": path,
                "document_name": None,
                "institution_name": institution_name,
                "source_system": source_system,
                "source_url": source_url,
                "source_record_id": None,
                "source_file_id": None,
                "source_disclosure_date": None,
                "source_posted_date": None,
                "profile_id": profile_id,
            }
            for path in files
        )
    return entries


def _write_public_report_artifacts(
    batch_report: dict[str, Any],
    *,
    source_report_path: Path,
    reports_dir: Path,
    timestamp: str,
) -> dict[str, Any]:
    public_report = export_public_batch_report(batch_report, source_report_path=source_report_path)
    public_json = reports_dir / f"public_batch_quality_{timestamp}.json"
    public_csv = reports_dir / f"public_batch_quality_{timestamp}.csv"
    public_md = reports_dir / f"public_batch_quality_{timestamp}.md"
    public_json.write_text(json.dumps(public_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    public_csv.write_text(public_to_csv(public_report), encoding="utf-8-sig")
    public_md.write_text(public_to_md(public_report), encoding="utf-8")
    return {
        "report": public_report,
        "json": public_json,
        "csv": public_csv,
        "markdown": public_md,
    }


def _pipeline_blockers(
    *,
    public_report: dict[str, Any],
    readiness_report: dict[str, Any],
    alert: dict[str, Any],
    batch_summary: dict[str, Any],
    fail_on_alert: bool,
    fail_on_readiness: bool,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    leak_count = int(public_report.get("sanitization", {}).get("sensitive_path_leak_count", 0) or 0)
    if leak_count:
        blockers.append(
            {
                "code": "public-report-path-leak",
                "severity": "blocker",
                "count": leak_count,
                "next_action": "Inspect public_batch_quality output and fix sanitization before sharing.",
            }
        )
    if batch_summary.get("agent_review_batch_budget_exceeded") is True:
        blockers.append(
            {
                "code": "agent-review-batch-budget-exceeded",
                "severity": "blocker",
                "next_action": "Reduce AI review candidates or increase approved budget before any provider execution.",
            }
        )
    if fail_on_readiness and readiness_report.get("passed") is False:
        blockers.append(
            {
                "code": "public-readiness-failed",
                "severity": "blocker",
                "failed_checks": [
                    check.get("name")
                    for check in readiness_report.get("checks", [])
                    if isinstance(check, dict) and not check.get("passed")
                ],
                "next_action": "Review public_batch_readiness and fix failed checks before treating this as release evidence.",
            }
        )
    if fail_on_alert and alert.get("status") == "needs_attention":
        blockers.append(
            {
                "code": "batch-alert-needs-attention",
                "severity": "blocker",
                "alert_severity": alert.get("severity"),
                "next_action": "Open batch_failure_alert and complete the recommended actions.",
            }
        )
    return blockers


def _pipeline_to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Public Batch Pipeline",
        "",
        f"- Status: {report['status']}",
        f"- Passed: {report['passed']}",
        f"- Blockers: {report['blocker_count']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in report["summary"].items():
        lines.append(f"- {key}: {value}")
    if report["blockers"]:
        lines.extend(["", "## Blockers", ""])
        for blocker in report["blockers"]:
            lines.append(f"- {json.dumps(blocker, ensure_ascii=False, sort_keys=True)}")
    lines.extend(["", "## Artifacts", ""])
    for key, value in report["artifacts"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", f"Safety: {report['safety_note']}", ""])
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the safe public batch preprocessing pipeline through readiness and alert artifacts."
    )
    parser.add_argument("inputs", nargs="*", help="Files or directories containing PDF, DOCX, HWPX, or HWP files.")
    parser.add_argument("--manifest-csv", default=None)
    parser.add_argument("--patterns", default=",".join(DEFAULT_GLOB_PATTERNS))
    parser.add_argument("--non-recursive", action="store_true")
    parser.add_argument("--profile-id", default=DEFAULT_PROFILE_ID)
    parser.add_argument("--institution-name", default=None)
    parser.add_argument("--source-system", default="PUBLIC_PORTAL")
    parser.add_argument("--source-url", default=None)
    parser.add_argument("--institution-profiles", default="config/institution_profiles.example.json")
    parser.add_argument("--no-strict-institution-profiles", action="store_true")
    parser.add_argument("--quality-profiles", default="config/quality_profiles.example.json")
    parser.add_argument("--no-strict-quality-profiles", action="store_true")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--max-upload-mb", type=int, default=1000)
    parser.add_argument("--max-chunk-chars", type=int, default=1800)
    parser.add_argument("--min-chunk-chars", type=int, default=300)
    parser.add_argument("--overlap-chars", type=int, default=120)
    parser.add_argument("--chunk-mode", choices=["article", "paragraph", "hybrid"], default="article")
    parser.add_argument("--no-context-header", action="store_true")
    parser.add_argument("--enable-agent-review", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--force-reprocess", action="store_true")
    parser.add_argument("--webhook-url", default=None)
    parser.add_argument("--alert-log", default="reports/batch_failure_alerts.jsonl")
    parser.add_argument("--include-local-paths-in-alert", action="store_true")
    parser.add_argument("--ocr-price-per-page", type=float, default=0.03)
    parser.add_argument("--fail-on-alert", action="store_true")
    parser.add_argument("--fail-on-readiness", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print compact JSON instead of pretty JSON.")
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None, *, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    try:
        report = run_public_batch_pipeline(
            inputs=args.inputs,
            manifest_csv=Path(args.manifest_csv) if args.manifest_csv else None,
            patterns=args.patterns,
            recursive=not args.non_recursive,
            profile_id=args.profile_id,
            institution_name=args.institution_name,
            source_system=args.source_system,
            source_url=args.source_url,
            institution_profiles=Path(args.institution_profiles) if args.institution_profiles else None,
            strict_institution_profiles=not args.no_strict_institution_profiles,
            quality_profiles=Path(args.quality_profiles) if args.quality_profiles else None,
            strict_quality_profiles=not args.no_strict_quality_profiles,
            reports_dir=Path(args.reports_dir),
            data_dir=Path(args.data_dir),
            max_upload_mb=args.max_upload_mb,
            max_chunk_chars=args.max_chunk_chars,
            min_chunk_chars=args.min_chunk_chars,
            overlap_chars=args.overlap_chars,
            chunk_mode=args.chunk_mode,
            include_context_header=not args.no_context_header,
            enable_agent_review=True,
            force_reprocess=args.force_reprocess,
            webhook_url=args.webhook_url,
            alert_log=Path(args.alert_log) if args.alert_log else None,
            include_local_paths_in_alert=args.include_local_paths_in_alert,
            ocr_price_per_page=args.ocr_price_per_page,
            fail_on_alert=args.fail_on_alert,
            fail_on_readiness=args.fail_on_readiness,
        )
    except Exception as exc:
        failure = {
            "report_type": "public_batch_pipeline",
            "status": "failed",
            "passed": False,
            "error": str(exc),
        }
        stdout.write(json.dumps(failure, ensure_ascii=False, indent=None if args.json else 2) + "\n")
        return 2
    stdout.write(json.dumps(report, ensure_ascii=False, indent=None if args.json else 2) + "\n")
    return 1 if report.get("blocker_count", 0) else 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
