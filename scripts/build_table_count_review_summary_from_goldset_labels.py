from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


SUMMARY_FIELDNAMES = [
    "document_id",
    "unit_count",
    "source_compare_unit_count",
    "completed_unit_count",
    "attention_unit_count",
    "pending_unit_count",
    "invalid_unit_count",
    "manual_table_count_from_completed_units",
    "matched_table_count_from_completed_units",
    "label_flag_unit_count",
    "issue_count",
    "issue_codes",
]

COMPLETE_LABEL_STATUSES = {"reviewed", "complete", "completed", "accepted", "confirmed"}
PENDING_LABEL_STATUSES = {"", "pending", "todo", "tbd", "unreviewed", "review_needed", "needs_review"}
REQUIRED_LABEL_FIELDS = ["document_id", "label_status", "manual_table_count", "matched_table_count"]


def build_table_count_review_summary_from_goldset_labels(
    *,
    labels_csv: Path,
    out_json: Path,
    out_csv: Path,
    out_md: Path,
    max_md_rows: int = 50,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if max_md_rows <= 0:
        raise ValueError("max_md_rows must be greater than zero.")
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    rows = _load_labels(labels_csv)
    summaries, issues = _summarize_rows(rows)
    summaries.sort(
        key=lambda row: (
            -int(row["invalid_unit_count"]),
            -int(row["pending_unit_count"]),
            row["document_id"],
        )
    )

    completed_unit_count = sum(int(row["completed_unit_count"]) for row in summaries)
    pending_unit_count = sum(int(row["pending_unit_count"]) for row in summaries)
    invalid_unit_count = sum(int(row["invalid_unit_count"]) for row in summaries)
    attention_unit_count = sum(int(row["attention_unit_count"]) for row in summaries)
    required_missing = _required_field_missing_counts(rows)
    ready = bool(rows) and completed_unit_count == len(rows) and pending_unit_count == 0 and invalid_unit_count == 0
    report = {
        "report_type": "parsing_goldset_table_unit_review_summary",
        "generated_at": generated_at,
        "source_goldset_labels_csv": str(labels_csv),
        "source_table_units_csv": str(labels_csv),
        "source_review_basis": "manual_goldset_labels",
        "derived_from_goldset_labels": True,
        "source_compare_only": False,
        "row_count": len(rows),
        "selected_unit_count": len(rows),
        "document_count": len(summaries),
        "completed_unit_count": completed_unit_count,
        "attention_unit_count": attention_unit_count,
        "pending_unit_count": pending_unit_count,
        "invalid_unit_count": invalid_unit_count,
        "issue_count": len(issues),
        "status_counts": dict(Counter(_normalized_status(row) or "(missing)" for row in rows)),
        "review_priority_counts": {"document_level_goldset_label": len(rows)},
        "label_review_flag_counts": {},
        "required_field_missing_counts": required_missing,
        "required_field_missing_total": sum(required_missing.values()),
        "review_contract": {
            "source": "reviewed document-level goldset labels",
            "accepted_label_statuses": sorted(COMPLETE_LABEL_STATUSES),
            "pending_label_statuses": sorted(status for status in PENDING_LABEL_STATUSES if status),
            "required_label_fields": list(REQUIRED_LABEL_FIELDS),
            "completion_guidance": (
                "This bridge treats each reviewed goldset label row as one document-level table-count "
                "review unit. It does not create or fill per-table human review rows."
            ),
        },
        "ready_for_table_score_transfer": ready,
        "max_md_rows": max_md_rows,
        "artifacts": {
            "json": str(out_json),
            "csv": str(out_csv),
            "markdown": str(out_md),
        },
        "safety_note": (
            "This goldset-label table summary is read-only. It does not fill labels, approve chunks, "
            "acknowledge review flags, index vectors, or publish MCP evidence."
        ),
    }

    _write_csv(out_csv, summaries)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps({**report, "document_summaries": summaries, "issues": issues}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(_markdown(report, summaries[:max_md_rows], issues[:max_md_rows]), encoding="utf-8")
    return report


def _load_labels(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    if not rows:
        raise ValueError("labels_csv must contain at least one row.")
    missing = sorted(set(REQUIRED_LABEL_FIELDS) - set(rows[0]))
    if missing:
        raise ValueError(f"labels_csv missing required columns: {', '.join(missing)}")
    return rows


def _summarize_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    summaries: list[dict[str, str]] = []
    issues: list[dict[str, str]] = []
    for row in rows:
        document_id = str(row.get("document_id") or "").strip() or "(missing-document)"
        row_issues = _row_issues(row)
        status = _normalized_status(row)
        is_pending = status in PENDING_LABEL_STATUSES
        is_complete = status in COMPLETE_LABEL_STATUSES and not row_issues
        pending = 1 if is_pending else 0
        invalid = 0 if is_complete or is_pending else 1
        if row_issues and not is_pending:
            invalid = 1
        for issue in row_issues:
            issues.append(
                {
                    "document_id": document_id,
                    "label_status": status or "(missing)",
                    "issue_code": issue,
                }
            )
        manual = _int_value(row.get("manual_table_count"))
        matched = _int_value(row.get("matched_table_count"))
        summaries.append(
            {
                "document_id": document_id,
                "unit_count": "1",
                "source_compare_unit_count": "1",
                "completed_unit_count": "1" if is_complete else "0",
                "attention_unit_count": "0",
                "pending_unit_count": str(pending),
                "invalid_unit_count": str(invalid),
                "manual_table_count_from_completed_units": str(manual) if manual is not None else "",
                "matched_table_count_from_completed_units": str(matched) if matched is not None else "",
                "label_flag_unit_count": "0",
                "issue_count": str(len(row_issues)),
                "issue_codes": "; ".join(sorted(set(row_issues))),
            }
        )
    return summaries, issues


def _row_issues(row: dict[str, str]) -> list[str]:
    issues: list[str] = []
    document_id = str(row.get("document_id") or "").strip()
    status = _normalized_status(row)
    manual = _int_value(row.get("manual_table_count"))
    matched = _int_value(row.get("matched_table_count"))
    if not document_id:
        issues.append("document-id-missing")
    if status not in COMPLETE_LABEL_STATUSES and status not in PENDING_LABEL_STATUSES:
        issues.append("label-status-not-accepted")
    if manual is None:
        issues.append("manual-table-count-missing-or-invalid")
    elif manual < 0:
        issues.append("manual-table-count-negative")
    if matched is None:
        issues.append("matched-table-count-missing-or-invalid")
    elif matched < 0:
        issues.append("matched-table-count-negative")
    if manual is not None and matched is not None and matched > manual:
        issues.append("matched-table-count-exceeds-manual")
    return issues


def _required_field_missing_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        for field in REQUIRED_LABEL_FIELDS:
            if not str(row.get(field) or "").strip():
                counts[field] += 1
    return dict(counts)


def _normalized_status(row: dict[str, str]) -> str:
    return str(row.get("label_status") or "").strip().lower()


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


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _counter_text(counts: Any) -> str:
    if not isinstance(counts, dict) or not counts:
        return "-"
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _markdown(report: dict[str, Any], summaries: list[dict[str, str]], issues: list[dict[str, str]]) -> str:
    contract = report.get("review_contract") or {}
    lines = [
        "# Goldset Label Table Count Review Summary",
        "",
        f"- Generated at: {report['generated_at']}",
        f"- Source goldset labels CSV: `{report['source_goldset_labels_csv']}`",
        f"- Source review basis: `{report['source_review_basis']}`",
        f"- Selected document-level units: {int(report['selected_unit_count']):,}",
        f"- Completed units: {int(report['completed_unit_count']):,}",
        f"- Pending units: {int(report['pending_unit_count']):,}",
        f"- Invalid units: {int(report['invalid_unit_count']):,}",
        f"- Required field missing total: {int(report['required_field_missing_total']):,}",
        f"- Ready for table score transfer: {str(report['ready_for_table_score_transfer']).lower()}",
        "",
        "## Review Basis",
        "",
        f"- Status counts: {_md(_counter_text(report.get('status_counts')))}",
        f"- Accepted label statuses: {', '.join(contract.get('accepted_label_statuses') or [])}",
        f"- Required label fields: {', '.join(contract.get('required_label_fields') or [])}",
        f"- Guidance: {contract.get('completion_guidance') or ''}",
        "",
        "## Safety Note",
        "",
        report["safety_note"],
        "",
        "## Document Summaries",
        "",
        "| Document | Done | Pending | Invalid | Manual tables | Matched tables | Issues |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in summaries:
        lines.append(
            f"| {_md(row['document_id'])} | {int(row['completed_unit_count']):,} | "
            f"{int(row['pending_unit_count']):,} | {int(row['invalid_unit_count']):,} | "
            f"{_md(row['manual_table_count_from_completed_units'])} | "
            f"{_md(row['matched_table_count_from_completed_units'])} | {_md(row['issue_codes'])} |"
        )
    if issues:
        lines.extend(["", "## Issues", "", "| Document | Status | Issue |", "| --- | --- | --- |"])
        for issue in issues:
            lines.append(
                f"| {_md(issue['document_id'])} | {_md(issue['label_status'])} | {_md(issue['issue_code'])} |"
            )
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a table-count review summary from reviewed parsing goldset label rows."
    )
    parser.add_argument("--labels-csv", required=True)
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-csv", required=True)
    parser.add_argument("--out-md", required=True)
    parser.add_argument("--max-md-rows", type=int, default=50)
    parser.add_argument(
        "--fail-on-issue",
        action="store_true",
        help="Exit non-zero when the label-derived summary is not ready for table score transfer.",
    )
    args = parser.parse_args(argv)

    report = build_table_count_review_summary_from_goldset_labels(
        labels_csv=Path(args.labels_csv),
        out_json=Path(args.out_json),
        out_csv=Path(args.out_csv),
        out_md=Path(args.out_md),
        max_md_rows=args.max_md_rows,
    )
    print(
        json.dumps(
            {
                "ok": bool(report["ready_for_table_score_transfer"]) if args.fail_on_issue else True,
                "json": str(args.out_json),
                "csv": str(args.out_csv),
                "markdown": str(args.out_md),
                "selected_unit_count": report["selected_unit_count"],
                "completed_unit_count": report["completed_unit_count"],
                "pending_unit_count": report["pending_unit_count"],
                "invalid_unit_count": report["invalid_unit_count"],
                "issue_count": report["issue_count"],
                "ready_for_table_score_transfer": report["ready_for_table_score_transfer"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 2 if args.fail_on_issue and not report["ready_for_table_score_transfer"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
