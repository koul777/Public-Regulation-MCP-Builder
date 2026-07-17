from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_metadata import current_repo_commit


def check_parsing_goldset_table_drift(
    *,
    table_unit_review_summary_report: Path,
    table_count_transfer_validation_report: Path,
    table_source_traceability_report: Path,
    base_dir: Path | None = None,
    out_json: Path | None = None,
    out_md: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    base_dir = (base_dir or Path.cwd()).resolve()
    review_summary = _load_json(table_unit_review_summary_report)
    transfer_validation = _load_json(table_count_transfer_validation_report)
    source_traceability = _load_json(table_source_traceability_report)

    findings: list[dict[str, Any]] = []
    source_artifacts: list[dict[str, Any]] = []

    table_units_path = _resolve_source_path(
        review_summary.get("source_table_units_csv"),
        base_dir=base_dir,
        report_path=table_unit_review_summary_report,
    )
    table_units_rows = _load_csv_if_present(
        role="table_units_csv",
        path=table_units_path,
        source_artifacts=source_artifacts,
        findings=findings,
    )
    if table_units_rows is not None:
        _compare_int(
            findings,
            role="table_units_csv",
            code="table-units-row-count-drift",
            expected=_int(review_summary.get("row_count")),
            actual=len(table_units_rows),
            detail="Table unit review summary row_count does not match the current table units CSV.",
        )
        _compare_int(
            findings,
            role="table_units_csv",
            code="table-units-selected-count-drift",
            expected=_int(review_summary.get("selected_unit_count")),
            actual=len(table_units_rows),
            detail="Table unit review summary selected_unit_count does not match the current table units CSV.",
        )
        _compare_int(
            findings,
            role="table_units_csv",
            code="table-units-document-count-drift",
            expected=_int(review_summary.get("document_count")),
            actual=_distinct_count(table_units_rows, "document_id"),
            detail="Table unit review summary document_count does not match the current table units CSV.",
        )

    labels_path = _resolve_source_path(
        transfer_validation.get("source_labels_csv"),
        base_dir=base_dir,
        report_path=table_count_transfer_validation_report,
    )
    labels_rows = _load_csv_if_present(
        role="labels_csv",
        path=labels_path,
        source_artifacts=source_artifacts,
        findings=findings,
    )
    if labels_rows is not None:
        _compare_int(
            findings,
            role="labels_csv",
            code="labels-document-count-drift",
            expected=_int(transfer_validation.get("labels_document_count")),
            actual=_distinct_count(labels_rows, "document_id"),
            detail="Table count transfer validation labels_document_count does not match the current labels CSV.",
        )

    transfer_summary_path = _resolve_source_path(
        transfer_validation.get("source_table_review_summary_json"),
        base_dir=base_dir,
        report_path=table_count_transfer_validation_report,
    )
    source_artifacts.append(_artifact("transfer_source_table_review_summary_json", transfer_summary_path))
    if transfer_summary_path is None or not transfer_summary_path.exists():
        findings.append(
            _finding(
                "blocker",
                "source-file-missing",
                "A source file referenced by a table evidence report is missing.",
                role="transfer_source_table_review_summary_json",
                path=str(transfer_summary_path or ""),
            )
        )
    expected_summary_path = table_unit_review_summary_report.resolve()
    if transfer_summary_path and transfer_summary_path.exists() and transfer_summary_path.resolve() != expected_summary_path:
        findings.append(
            _finding(
                "blocker",
                "transfer-summary-lineage-mismatch",
                "Table count transfer validation was built from a different review summary report than the claim bundle.",
                role="transfer_source_table_review_summary_json",
                expected_path=str(expected_summary_path),
                actual_path=str(transfer_summary_path.resolve()),
            )
        )

    batches_path = _resolve_source_path(
        source_traceability.get("source_table_review_batches_csv"),
        base_dir=base_dir,
        report_path=table_source_traceability_report,
    )
    batch_rows = _load_csv_if_present(
        role="table_review_batches_csv",
        path=batches_path,
        source_artifacts=source_artifacts,
        findings=findings,
    )
    if batch_rows is not None:
        expected_record_count = _int(source_traceability.get("record_count", source_traceability.get("batch_count")))
        _compare_int(
            findings,
            role="table_review_batches_csv",
            code="traceability-record-count-drift",
            expected=expected_record_count,
            actual=len(batch_rows),
            detail="Table source traceability record_count does not match the current table review batches CSV.",
        )
        _compare_int(
            findings,
            role="table_review_batches_csv",
            code="traceability-embedded-record-count-drift",
            expected=len(source_traceability.get("batches") or []),
            actual=len(batch_rows),
            detail="Embedded traceability rows do not match the current table review batches CSV.",
        )
        if source_traceability.get("source_record_type") != "table_unit":
            _check_batch_unit_packet_linkage(
                findings,
                batch_rows=batch_rows,
                expected_table_units_path=table_units_path,
                base_dir=base_dir,
            )
        _check_traceability_embedded_rows(
            findings,
            batch_rows=batch_rows,
            embedded_rows=source_traceability.get("batches") or [],
        )

    blocker_count = sum(1 for finding in findings if finding["severity"] == "blocker")
    warning_count = sum(1 for finding in findings if finding["severity"] == "warning")
    report = {
        "report_type": "parsing_goldset_table_drift_check",
        "generated_at": generated_at,
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "base_dir": str(base_dir),
        "passed": blocker_count == 0,
        "blocker_count": blocker_count,
        "warning_count": warning_count,
        "source_reports": {
            "table_unit_review_summary_report": str(table_unit_review_summary_report),
            "table_count_transfer_validation_report": str(table_count_transfer_validation_report),
            "table_source_traceability_report": str(table_source_traceability_report),
        },
        "source_artifacts": source_artifacts,
        "findings": findings,
        "safety_note": (
            "This drift check is read-only. It does not fill goldset labels, approve chunks, "
            "acknowledge review flags, index vectors, or publish MCP evidence."
        ),
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_csv_if_present(
    *,
    role: str,
    path: Path | None,
    source_artifacts: list[dict[str, Any]],
    findings: list[dict[str, Any]],
) -> list[dict[str, str]] | None:
    source_artifacts.append(_artifact(role, path))
    if path is None or not path.exists():
        findings.append(
            _finding(
                "blocker",
                "source-file-missing",
                "A source file referenced by a table evidence report is missing.",
                role=role,
                path=str(path or ""),
            )
        )
        return None
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _resolve_source_path(value: Any, *, base_dir: Path, report_path: Path) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text)
    if path.is_absolute():
        return path
    report_relative = (report_path.resolve().parent / path).resolve()
    if report_relative.exists():
        return report_relative
    return (base_dir / path).resolve()


def _artifact(role: str, path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "role": role,
            "path": "",
            "exists": False,
            "byte_count": 0,
            "sha256": "",
        }
    exists = path.exists()
    return {
        "role": role,
        "path": str(path),
        "exists": exists,
        "byte_count": path.stat().st_size if exists else 0,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if exists else "",
    }


def _compare_int(
    findings: list[dict[str, Any]],
    *,
    role: str,
    code: str,
    expected: int,
    actual: int,
    detail: str,
) -> None:
    if expected != actual:
        findings.append(
            _finding(
                "blocker",
                code,
                detail,
                role=role,
                expected=expected,
                actual=actual,
            )
        )


def _check_batch_unit_packet_linkage(
    findings: list[dict[str, Any]],
    *,
    batch_rows: list[dict[str, str]],
    expected_table_units_path: Path | None,
    base_dir: Path,
) -> None:
    if expected_table_units_path is None:
        return
    expected = expected_table_units_path.resolve()
    missing_link_count = sum(
        1
        for row in batch_rows
        if not str(row.get("table_unit_packet_csv") or "").strip()
    )
    if missing_link_count:
        findings.append(
            _finding(
                "blocker",
                "table-review-batch-unit-packet-link-missing",
                "Table review batch rows must retain table_unit_packet_csv lineage.",
                role="table_review_batches_csv",
                missing_link_count=missing_link_count,
            )
        )
    linked_paths = {
        _resolve_linked_csv_path(row.get("table_unit_packet_csv"), base_dir=base_dir)
        for row in batch_rows
        if str(row.get("table_unit_packet_csv") or "").strip()
    }
    linked_paths.discard(None)
    mismatches = sorted(str(path) for path in linked_paths if path and path.resolve() != expected)
    if mismatches:
        findings.append(
            _finding(
                "blocker",
                "table-review-batch-unit-packet-lineage-mismatch",
                "Table review batches point to a different table unit packet than the review summary.",
                role="table_review_batches_csv",
                expected_path=str(expected),
                mismatched_path_count=len(mismatches),
                sample_mismatched_paths=mismatches[:5],
            )
        )


def _resolve_linked_csv_path(value: Any, *, base_dir: Path) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text)
    return path.resolve() if path.is_absolute() else (base_dir / path).resolve()


def _check_traceability_embedded_rows(
    findings: list[dict[str, Any]],
    *,
    batch_rows: list[dict[str, str]],
    embedded_rows: list[Any],
) -> None:
    embedded_by_id = {
        str(row.get("table_review_batch_id") or ""): row
        for row in embedded_rows
        if isinstance(row, dict)
    }
    current_ids = {_row_record_id(row) for row in batch_rows}
    embedded_ids = set(embedded_by_id)
    missing_in_report = sorted(current_ids - embedded_ids)
    missing_in_source = sorted(embedded_ids - current_ids)
    changed_ranges = []
    changed_sources = []
    for row in batch_rows:
        batch_id = _row_record_id(row)
        embedded = embedded_by_id.get(batch_id)
        if not embedded:
            continue
        current_ranges = _row_source_page_ranges(row)
        embedded_ranges = str(embedded.get("source_page_ranges") or "").strip()
        if current_ranges != embedded_ranges:
            changed_ranges.append(batch_id)
        current_source = str(row.get("source_path") or "").strip()
        embedded_source = str(embedded.get("source_path") or "").strip()
        if current_source and embedded_source and current_source != embedded_source:
            changed_sources.append(batch_id)
    if missing_in_report:
        findings.append(
            _finding(
                "blocker",
                "traceability-source-record-missing-in-report",
                "Current table review batch records are missing from the embedded traceability report rows.",
                role="table_source_traceability_report",
                missing_count=len(missing_in_report),
                sample_ids=missing_in_report[:5],
            )
        )
    if missing_in_source:
        findings.append(
            _finding(
                "blocker",
                "traceability-report-record-missing-in-source",
                "Embedded traceability report rows no longer exist in the current table review batches CSV.",
                role="table_source_traceability_report",
                missing_count=len(missing_in_source),
                sample_ids=missing_in_source[:5],
            )
        )
    if changed_ranges:
        findings.append(
            _finding(
                "blocker",
                "traceability-source-page-range-drift",
                "Current table review batch source_page_ranges differ from the embedded traceability report rows.",
                role="table_source_traceability_report",
                changed_count=len(changed_ranges),
                sample_ids=changed_ranges[:5],
            )
        )
    if changed_sources:
        findings.append(
            _finding(
                "blocker",
                "traceability-source-path-drift",
                "Current table review batch source_path differs from the embedded traceability report rows.",
                role="table_source_traceability_report",
                changed_count=len(changed_sources),
                sample_ids=changed_sources[:5],
            )
        )


def _row_record_id(row: dict[str, str]) -> str:
    return str(row.get("table_review_batch_id") or row.get("table_unit_key") or "").strip()


def _row_source_page_ranges(row: dict[str, str]) -> str:
    value = str(row.get("source_page_ranges") or "").strip()
    if value:
        return value
    start = str(row.get("source_page_start") or "").strip()
    end = str(row.get("source_page_end") or "").strip()
    if not start and not end:
        return ""
    return f"{start or '?'}-{end or start or '?'}"


def _distinct_count(rows: list[dict[str, str]], key: str) -> int:
    return len({str(row.get(key) or "").strip() for row in rows if str(row.get(key) or "").strip()})


def _int(value: Any) -> int:
    if isinstance(value, bool) or value in (None, ""):
        return 0
    try:
        return int(float(str(value)))
    except ValueError:
        return 0


def _finding(severity: str, code: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"severity": severity, "code": code, "detail": detail, **extra}


def _to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Parsing Goldset Table Drift Check",
        "",
        f"- Generated at: {report['generated_at']}",
        f"- Passed: `{str(report['passed']).lower()}`",
        f"- Blockers / warnings: {report['blocker_count']} / {report['warning_count']}",
        "",
        "## Source Artifacts",
        "",
        "| Role | Exists | Bytes | SHA-256 | Path |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for artifact in report.get("source_artifacts") or []:
        lines.append(
            f"| {_md(artifact.get('role'))} | {str(artifact.get('exists')).lower()} | "
            f"{int(artifact.get('byte_count') or 0):,} | `{_md(artifact.get('sha256') or '-')}` | "
            f"`{_md(artifact.get('path') or '')}` |"
        )
    lines.extend(["", "## Findings", "", "| Severity | Code | Detail |", "| --- | --- | --- |"])
    for finding in report.get("findings") or []:
        lines.append(
            f"| {_md(finding.get('severity'))} | `{_md(finding.get('code'))}` | {_md(finding.get('detail'))} |"
        )
    if not report.get("findings"):
        lines.append("| none | none | No drift detected. |")
    lines.extend(["", "## Safety Note", "", report["safety_note"], ""])
    return "\n".join(lines)


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check drift between parsing goldset table evidence reports and their current source files.")
    parser.add_argument("--table-unit-review-summary-report", required=True, type=Path)
    parser.add_argument("--table-count-transfer-validation-report", required=True, type=Path)
    parser.add_argument("--table-source-traceability-report", required=True, type=Path)
    parser.add_argument("--base-dir", type=Path, default=Path("."))
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-md", type=Path)
    parser.add_argument("--fail-on-issue", action="store_true")
    args = parser.parse_args(argv)

    report = check_parsing_goldset_table_drift(
        table_unit_review_summary_report=args.table_unit_review_summary_report,
        table_count_transfer_validation_report=args.table_count_transfer_validation_report,
        table_source_traceability_report=args.table_source_traceability_report,
        base_dir=args.base_dir,
        out_json=args.out_json,
        out_md=args.out_md,
    )
    print(
        json.dumps(
            {
                "ok": bool(report["passed"]),
                "blocker_count": report["blocker_count"],
                "warning_count": report["warning_count"],
                "out_json": str(args.out_json) if args.out_json else "",
                "out_md": str(args.out_md) if args.out_md else "",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.fail_on_issue and not report["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
