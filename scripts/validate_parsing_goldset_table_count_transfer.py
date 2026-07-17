from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


CSV_FIELDNAMES = [
    "document_id",
    "label_manual_table_count",
    "label_matched_table_count",
    "summary_manual_table_count",
    "summary_matched_table_count",
    "summary_unit_count",
    "summary_completed_unit_count",
    "status",
    "issues",
]


def validate_parsing_goldset_table_count_transfer(
    *,
    labels_csv: Path,
    table_review_summary_json: Path,
    out_json: Path,
    out_csv: Path,
    out_md: Path,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    label_rows = _load_labels(labels_csv)
    summary = json.loads(table_review_summary_json.read_text(encoding="utf-8"))
    if not isinstance(summary, dict) or summary.get("report_type") != "parsing_goldset_table_unit_review_summary":
        raise ValueError("table_review_summary_json must be a parsing_goldset_table_unit_review_summary report.")
    summary_rows = {
        str(row.get("document_id") or "").strip(): row
        for row in summary.get("document_summaries", [])
        if isinstance(row, dict) and str(row.get("document_id") or "").strip()
    }

    findings: list[dict[str, Any]] = []
    rows: list[dict[str, str]] = []
    if bool(summary.get("source_compare_only")):
        findings.append(
            _finding(
                "blocker",
                "table-summary-source-compare-only",
                "Source-compare-only summaries cannot validate total goldset table counts.",
            )
        )
    if not bool(summary.get("ready_for_table_score_transfer")):
        findings.append(
            _finding(
                "blocker",
                "table-review-summary-not-ready",
                "Table-unit review summary is not ready for table score transfer.",
                pending_unit_count=summary.get("pending_unit_count"),
                invalid_unit_count=summary.get("invalid_unit_count"),
            )
        )

    labels_by_doc = {str(row.get("document_id") or "").strip(): row for row in label_rows}
    for document_id, summary_row in sorted(summary_rows.items()):
        label_row = labels_by_doc.get(document_id, {})
        manual_label = _int_value(label_row.get("manual_table_count"))
        matched_label = _int_value(label_row.get("matched_table_count"))
        manual_summary = _int_value(summary_row.get("manual_table_count_from_completed_units"))
        matched_summary = _int_value(summary_row.get("matched_table_count_from_completed_units"))
        row_issues: list[str] = []
        if not label_row:
            row_issues.append("label-document-missing")
        if manual_label is None:
            row_issues.append("manual-table-count-missing-or-invalid")
        if matched_label is None:
            row_issues.append("matched-table-count-missing-or-invalid")
        if manual_summary is None:
            row_issues.append("summary-manual-table-count-invalid")
        if matched_summary is None:
            row_issues.append("summary-matched-table-count-invalid")
        if manual_label is not None and manual_summary is not None and manual_label != manual_summary:
            row_issues.append("manual-table-count-mismatch")
        if matched_label is not None and matched_summary is not None and matched_label != matched_summary:
            row_issues.append("matched-table-count-mismatch")
        if matched_label is not None and manual_label is not None and matched_label > manual_label:
            row_issues.append("matched-table-count-exceeds-manual")
        for issue in row_issues:
            findings.append(
                _finding(
                    "blocker",
                    issue,
                    "Goldset table-count transfer requires matching reviewed summary counts.",
                    document_id=document_id,
                )
            )
        rows.append(
            {
                "document_id": document_id,
                "label_manual_table_count": "" if manual_label is None else str(manual_label),
                "label_matched_table_count": "" if matched_label is None else str(matched_label),
                "summary_manual_table_count": "" if manual_summary is None else str(manual_summary),
                "summary_matched_table_count": "" if matched_summary is None else str(matched_summary),
                "summary_unit_count": str(summary_row.get("unit_count") or ""),
                "summary_completed_unit_count": str(summary_row.get("completed_unit_count") or ""),
                "status": "ready" if not row_issues else "blocked",
                "issues": "; ".join(sorted(set(row_issues))),
            }
        )
    for document_id in sorted(set(labels_by_doc) - set(summary_rows)):
        label_row = labels_by_doc[document_id]
        manual_label = _int_value(label_row.get("manual_table_count"))
        matched_label = _int_value(label_row.get("matched_table_count"))
        row_issues = ["summary-document-missing"]
        if manual_label is None:
            row_issues.append("manual-table-count-missing-or-invalid")
        if matched_label is None:
            row_issues.append("matched-table-count-missing-or-invalid")
        if matched_label is not None and manual_label is not None and matched_label > manual_label:
            row_issues.append("matched-table-count-exceeds-manual")
        for issue in row_issues:
            findings.append(
                _finding(
                    "blocker",
                    issue,
                    "Goldset table-count transfer requires every labeled document to appear in the reviewed summary.",
                    document_id=document_id,
                )
            )
        rows.append(
            {
                "document_id": document_id,
                "label_manual_table_count": "" if manual_label is None else str(manual_label),
                "label_matched_table_count": "" if matched_label is None else str(matched_label),
                "summary_manual_table_count": "",
                "summary_matched_table_count": "",
                "summary_unit_count": "",
                "summary_completed_unit_count": "",
                "status": "blocked",
                "issues": "; ".join(sorted(set(row_issues))),
            }
        )

    severity_counts = Counter(str(item.get("severity") or "") for item in findings)
    finding_code_counts = Counter(str(item.get("code") or "") for item in findings)
    row_issue_counts = Counter()
    for row in rows:
        for issue in str(row.get("issues") or "").split(";"):
            issue = issue.strip()
            if issue:
                row_issue_counts[issue] += 1
    row_status_counts = Counter(str(row.get("status") or "") for row in rows)
    root_cause_summary = _root_cause_summary(
        summary=summary,
        rows=rows,
        finding_code_counts=finding_code_counts,
        row_issue_counts=row_issue_counts,
        row_status_counts=row_status_counts,
    )
    report = {
        "report_type": "parsing_goldset_table_count_transfer_validation",
        "generated_at": generated_at,
        "source_labels_csv": str(labels_csv),
        "source_table_review_summary_json": str(table_review_summary_json),
        "labels_document_count": len(label_rows),
        "summary_document_count": len(summary_rows),
        "row_count": len(rows),
        "blocker_count": int(severity_counts.get("blocker", 0)),
        "warning_count": int(severity_counts.get("warning", 0)),
        "passed": int(severity_counts.get("blocker", 0)) == 0,
        "source_summary_ready_for_table_score_transfer": bool(summary.get("ready_for_table_score_transfer")),
        "source_summary_source_compare_only": bool(summary.get("source_compare_only")),
        "finding_code_counts": dict(sorted(finding_code_counts.items())),
        "row_issue_counts": dict(sorted(row_issue_counts.items())),
        "row_status_counts": dict(sorted(row_status_counts.items())),
        "root_cause_summary": root_cause_summary,
        "artifacts": {
            "json": str(out_json),
            "csv": str(out_csv),
            "markdown": str(out_md),
        },
        "safety_note": (
            "This validation is read-only. It does not fill goldset labels, approve chunks, "
            "acknowledge review flags, index vectors, or publish MCP evidence."
        ),
        "findings": findings,
    }

    _write_csv(out_csv, rows)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps({**report, "rows": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(_markdown(report, rows), encoding="utf-8")
    return report


def _load_labels(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    if not rows:
        raise ValueError("labels_csv must contain at least one row.")
    required = {"document_id", "manual_table_count", "matched_table_count"}
    missing = sorted(required - set(rows[0]))
    if missing:
        raise ValueError(f"labels_csv missing required columns: {', '.join(missing)}")
    return rows


def _int_value(value: Any) -> int | None:
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    if not parsed.is_integer():
        return None
    return int(parsed)


def _finding(severity: str, code: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"severity": severity, "code": code, "detail": detail, **extra}


def _root_cause_summary(
    *,
    summary: dict[str, Any],
    rows: list[dict[str, str]],
    finding_code_counts: Counter[str],
    row_issue_counts: Counter[str],
    row_status_counts: Counter[str],
) -> dict[str, Any]:
    pending_units = _int_value(summary.get("pending_unit_count"))
    invalid_units = _int_value(summary.get("invalid_unit_count"))
    source_ready = bool(summary.get("ready_for_table_score_transfer"))
    source_compare_only = bool(summary.get("source_compare_only"))
    review_dependency_open = source_compare_only or not source_ready or (pending_units or 0) > 0 or (invalid_units or 0) > 0
    missing_label_codes = {
        "manual-table-count-missing-or-invalid",
        "matched-table-count-missing-or-invalid",
        "label-document-missing",
    }
    mismatch_codes = {
        "manual-table-count-mismatch",
        "matched-table-count-mismatch",
        "matched-table-count-exceeds-manual",
    }
    summary_invalid_codes = {
        "summary-manual-table-count-invalid",
        "summary-matched-table-count-invalid",
        "summary-document-missing",
    }
    if review_dependency_open:
        primary_blocker = "table_unit_human_review_pending"
        recommended_next_step = "complete_table_unit_human_review"
    elif any(row_issue_counts.get(code, 0) for code in missing_label_codes | mismatch_codes | summary_invalid_codes):
        primary_blocker = "goldset_count_sync_pending"
        recommended_next_step = "sync_reviewed_table_counts_to_goldset_labels"
    else:
        primary_blocker = "none"
        recommended_next_step = "none"
    return {
        "primary_blocker": primary_blocker,
        "recommended_next_step": recommended_next_step,
        "review_dependency_open": review_dependency_open,
        "source_summary_ready_for_table_score_transfer": source_ready,
        "source_summary_source_compare_only": source_compare_only,
        "source_pending_unit_count": pending_units,
        "source_invalid_unit_count": invalid_units,
        "row_count": len(rows),
        "blocked_row_count": int(row_status_counts.get("blocked", 0)),
        "ready_row_count": int(row_status_counts.get("ready", 0)),
        "missing_label_document_count": int(
            sum(
                1
                for row in rows
                if any(code in str(row.get("issues") or "") for code in missing_label_codes)
            )
        ),
        "mismatch_document_count": int(
            sum(1 for row in rows if any(code in str(row.get("issues") or "") for code in mismatch_codes))
        ),
        "summary_invalid_document_count": int(
            sum(1 for row in rows if any(code in str(row.get("issues") or "") for code in summary_invalid_codes))
        ),
        "finding_code_counts": dict(sorted(finding_code_counts.items())),
        "row_issue_counts": dict(sorted(row_issue_counts.items())),
    }


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _markdown(report: dict[str, Any], rows: list[dict[str, str]]) -> str:
    lines = [
        "# Parsing Goldset Table Count Transfer Validation",
        "",
        f"- Generated at: {report['generated_at']}",
        f"- Labels CSV: `{report['source_labels_csv']}`",
        f"- Table review summary JSON: `{report['source_table_review_summary_json']}`",
        f"- Source summary ready: {str(report['source_summary_ready_for_table_score_transfer']).lower()}",
        f"- Source summary source-compare only: {str(report['source_summary_source_compare_only']).lower()}",
        f"- Blockers: {int(report['blocker_count']):,}",
        f"- Passed: {str(report['passed']).lower()}",
        "",
        "## Safety Note",
        "",
        report["safety_note"],
        "",
        "## Root Cause Summary",
        "",
        f"- Primary blocker: `{report['root_cause_summary']['primary_blocker']}`",
        f"- Recommended next step: `{report['root_cause_summary']['recommended_next_step']}`",
        f"- Review dependency open: {str(report['root_cause_summary']['review_dependency_open']).lower()}",
        f"- Pending / invalid source units: {_md(report['root_cause_summary']['source_pending_unit_count'])} / {_md(report['root_cause_summary']['source_invalid_unit_count'])}",
        f"- Blocked / ready rows: {_md(report['root_cause_summary']['blocked_row_count'])} / {_md(report['root_cause_summary']['ready_row_count'])}",
        f"- Missing-label / mismatch / summary-invalid documents: {_md(report['root_cause_summary']['missing_label_document_count'])} / {_md(report['root_cause_summary']['mismatch_document_count'])} / {_md(report['root_cause_summary']['summary_invalid_document_count'])}",
        "",
        "## Rows",
        "",
        "| Document | Label manual | Summary manual | Label matched | Summary matched | Status | Issues |",
        "| --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {_md(row['document_id'])} | {_md(row['label_manual_table_count'])} | "
            f"{_md(row['summary_manual_table_count'])} | {_md(row['label_matched_table_count'])} | "
            f"{_md(row['summary_matched_table_count'])} | {_md(row['status'])} | {_md(row['issues'])} |"
        )
    if report["findings"]:
        lines.extend(["", "## Findings", ""])
        for finding in report["findings"]:
            document = f" document={finding.get('document_id')}" if finding.get("document_id") else ""
            lines.append(f"- {finding['severity']} `{finding['code']}`{document}: {finding['detail']}")
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate that reviewed table-unit summary counts match parsing goldset table counts."
    )
    parser.add_argument("--labels-csv", required=True)
    parser.add_argument("--table-review-summary-json", required=True)
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-csv", required=True)
    parser.add_argument("--out-md", required=True)
    parser.add_argument("--fail-on-issue", action="store_true")
    args = parser.parse_args(argv)

    report = validate_parsing_goldset_table_count_transfer(
        labels_csv=Path(args.labels_csv),
        table_review_summary_json=Path(args.table_review_summary_json),
        out_json=Path(args.out_json),
        out_csv=Path(args.out_csv),
        out_md=Path(args.out_md),
    )
    print(
        json.dumps(
            {
                "ok": bool(report["passed"]),
                "json": str(args.out_json),
                "csv": str(args.out_csv),
                "markdown": str(args.out_md),
                "blocker_count": report["blocker_count"],
                "passed": report["passed"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 2 if args.fail_on_issue and not report["passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
