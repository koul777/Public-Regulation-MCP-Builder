from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

try:
    from scripts.parsing_goldset_table_review_contract import (
        TABLE_REVIEW_ATTENTION_STATUSES,
        TABLE_REVIEW_ALLOWED_UNIT_STATUSES,
        TABLE_REVIEW_COMPLETE_STATUSES,
        TABLE_REVIEW_COMPLETION_GUIDANCE,
        TABLE_REVIEW_REQUIRED_COMPLETE_FIELDS,
        TABLE_REVIEW_TRUE_VALUES,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution path
    from parsing_goldset_table_review_contract import (
        TABLE_REVIEW_ATTENTION_STATUSES,
        TABLE_REVIEW_ALLOWED_UNIT_STATUSES,
        TABLE_REVIEW_COMPLETE_STATUSES,
        TABLE_REVIEW_COMPLETION_GUIDANCE,
        TABLE_REVIEW_REQUIRED_COMPLETE_FIELDS,
        TABLE_REVIEW_TRUE_VALUES,
    )


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

COMPLETE_STATUSES = set(TABLE_REVIEW_COMPLETE_STATUSES)
ATTENTION_STATUSES = set(TABLE_REVIEW_ATTENTION_STATUSES)
TRUE_VALUES = set(TABLE_REVIEW_TRUE_VALUES)


def summarize_parsing_goldset_table_unit_review(
    *,
    table_units_csv: Path,
    out_json: Path,
    out_csv: Path,
    out_md: Path,
    source_compare_only: bool = False,
    max_md_rows: int = 50,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if max_md_rows <= 0:
        raise ValueError("max_md_rows must be greater than zero.")
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    rows = _load_rows(table_units_csv)
    selected_rows = [
        row for row in rows if not source_compare_only or row.get("review_priority") == "source_table_compare"
    ]
    summaries, issues = _summarize_rows(selected_rows)
    summaries.sort(key=lambda row: (-int(row["invalid_unit_count"]), -int(row["pending_unit_count"]), row["document_id"]))

    completed_unit_count = sum(int(row["completed_unit_count"]) for row in summaries)
    invalid_unit_count = sum(int(row["invalid_unit_count"]) for row in summaries)
    pending_unit_count = sum(int(row["pending_unit_count"]) for row in summaries)
    attention_unit_count = sum(int(row["attention_unit_count"]) for row in summaries)
    report = {
        "report_type": "parsing_goldset_table_unit_review_summary",
        "generated_at": generated_at,
        "source_table_units_csv": str(table_units_csv),
        "source_compare_only": source_compare_only,
        "row_count": len(rows),
        "selected_unit_count": len(selected_rows),
        "document_count": len(summaries),
        "completed_unit_count": completed_unit_count,
        "attention_unit_count": attention_unit_count,
        "pending_unit_count": pending_unit_count,
        "invalid_unit_count": invalid_unit_count,
        "issue_count": len(issues),
        "status_counts": dict(Counter(_normalized_status(row) or "(missing)" for row in selected_rows)),
        "review_priority_counts": dict(Counter(row.get("review_priority") or "(missing)" for row in selected_rows)),
        "label_review_flag_counts": _flag_counts(selected_rows, "table_label_review_flags"),
        "required_field_missing_counts": _required_field_missing_counts(selected_rows),
        "review_contract": {
            "allowed_human_unit_statuses": list(TABLE_REVIEW_ALLOWED_UNIT_STATUSES),
            "required_complete_fields": list(TABLE_REVIEW_REQUIRED_COMPLETE_FIELDS),
            "accepted_confirmation_values": list(TABLE_REVIEW_TRUE_VALUES),
            "completion_guidance": TABLE_REVIEW_COMPLETION_GUIDANCE,
        },
        "ready_for_table_score_transfer": (
            bool(selected_rows)
            and completed_unit_count == len(selected_rows)
            and invalid_unit_count == 0
            and attention_unit_count == 0
            and pending_unit_count == 0
        ),
        "max_md_rows": max_md_rows,
        "artifacts": {
            "json": str(out_json),
            "csv": str(out_csv),
            "markdown": str(out_md),
        },
        "safety_note": (
            "This table-unit review summary is read-only. It does not fill goldset labels, approve chunks, "
            "acknowledge review flags, index vectors, or publish MCP evidence."
        ),
    }
    report["required_field_missing_total"] = sum(report["required_field_missing_counts"].values())

    _write_csv(out_csv, summaries)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps({**report, "document_summaries": summaries, "issues": issues}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(_markdown(report, summaries[:max_md_rows], issues[:max_md_rows]), encoding="utf-8")
    return report


def _load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    if not rows:
        raise ValueError("table_units_csv must contain at least one row.")
    required = {"document_id", "table_unit_key", "review_priority"}
    missing = sorted(required - set(rows[0]))
    if missing:
        raise ValueError(f"table_units_csv missing required columns: {', '.join(missing)}")
    return rows


def _summarize_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row.get("document_id") or "(missing-document)"].append(row)

    issues: list[dict[str, str]] = []
    summaries: list[dict[str, str]] = []
    for document_id, document_rows in grouped.items():
        document_issues: list[str] = []
        completed = 0
        attention = 0
        pending = 0
        invalid = 0
        manual_total = 0
        matched_total = 0
        label_flag_units = 0
        for row in document_rows:
            status = _normalized_status(row)
            row_issues = _row_issues(row)
            if row.get("table_label_review_flags"):
                label_flag_units += 1
            if status in COMPLETE_STATUSES and not row_issues:
                completed += 1
                manual_total += _int_value(row.get("human_manual_table_count")) or 0
                matched_total += _int_value(row.get("human_matched_table_count")) or 0
            elif status in ATTENTION_STATUSES:
                attention += 1
            elif row_issues:
                invalid += 1
                for issue in row_issues:
                    document_issues.append(issue)
                    issues.append(
                        {
                            "document_id": document_id,
                            "unit_rank": row.get("unit_rank") or "",
                            "table_unit_key": row.get("table_unit_key") or "",
                            "issue_code": issue,
                        }
                    )
            else:
                pending += 1
        issue_codes = sorted(set(document_issues))
        summaries.append(
            {
                "document_id": document_id,
                "unit_count": str(len(document_rows)),
                "source_compare_unit_count": str(
                    sum(1 for row in document_rows if row.get("review_priority") == "source_table_compare")
                ),
                "completed_unit_count": str(completed),
                "attention_unit_count": str(attention),
                "pending_unit_count": str(pending),
                "invalid_unit_count": str(invalid),
                "manual_table_count_from_completed_units": str(manual_total),
                "matched_table_count_from_completed_units": str(matched_total),
                "label_flag_unit_count": str(label_flag_units),
                "issue_count": str(len(document_issues)),
                "issue_codes": "; ".join(issue_codes),
            }
        )
    return summaries, issues


def _row_issues(row: dict[str, str]) -> list[str]:
    status = _normalized_status(row)
    if status not in COMPLETE_STATUSES:
        return []
    issues: list[str] = []
    manual = _int_value(row.get("human_manual_table_count"))
    matched = _int_value(row.get("human_matched_table_count"))
    if not _truthy(row.get("human_source_pages_checked")):
        issues.append("source-pages-not-confirmed")
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
    if not _truthy(row.get("human_row_column_match")):
        issues.append("row-column-match-not-confirmed")
    if not _truthy(row.get("human_parentage_ok")):
        issues.append("parentage-not-confirmed")
    if not str(row.get("human_reviewer") or "").strip():
        issues.append("reviewer-missing")
    if not _valid_reviewed_at(row.get("human_reviewed_at")):
        issues.append("reviewed-at-missing-or-invalid")
    return issues


def _required_field_missing_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        for field in TABLE_REVIEW_REQUIRED_COMPLETE_FIELDS:
            if not str(row.get(field) or "").strip():
                counts[field] += 1
    return dict(counts)


def _flag_counts(rows: list[dict[str, str]], field_name: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        for flag in str(row.get(field_name) or "").split(";"):
            flag = flag.strip()
            if flag:
                counts[flag] += 1
    return dict(counts)


def _normalized_status(row: dict[str, str]) -> str:
    return str(row.get("human_unit_status") or "").strip().lower()


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in TRUE_VALUES


def _valid_reviewed_at(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        if "T" in text or " " in text:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _int_value(value: Any) -> int | None:
    text = str(value or "").strip()
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


def _markdown(report: dict[str, Any], summaries: list[dict[str, str]], issues: list[dict[str, str]]) -> str:
    review_contract = report.get("review_contract") or {}
    lines = [
        "# Parsing Goldset Table Unit Review Summary",
        "",
        f"- Generated at: {report['generated_at']}",
        f"- Source table units CSV: `{report['source_table_units_csv']}`",
        f"- Source-compare only: {str(report['source_compare_only']).lower()}",
        f"- Selected units: {int(report['selected_unit_count']):,}",
        f"- Completed units: {int(report['completed_unit_count']):,}",
        f"- Attention units: {int(report['attention_unit_count']):,}",
        f"- Pending units: {int(report['pending_unit_count']):,}",
        f"- Invalid units: {int(report['invalid_unit_count']):,}",
        f"- Required field missing total: {int(report['required_field_missing_total']):,}",
        f"- Ready for table score transfer: {str(report['ready_for_table_score_transfer']).lower()}",
        "",
        "## Review Workload",
        "",
        f"- Review priority counts: {_md(_counter_text(report.get('review_priority_counts')))}",
        f"- Label review flag counts: {_md(_counter_text(report.get('label_review_flag_counts')))}",
        f"- Missing required field counts: {_md(_counter_text(report.get('required_field_missing_counts')))}",
        "",
        "## Review Entry Contract",
        "",
        f"- Allowed `human_unit_status`: {', '.join(review_contract.get('allowed_human_unit_statuses') or [])}",
        f"- Required fields for complete rows: {', '.join(review_contract.get('required_complete_fields') or [])}",
        f"- Accepted confirmation values: {', '.join(review_contract.get('accepted_confirmation_values') or [])}",
        f"- Guidance: {review_contract.get('completion_guidance') or ''}",
        "",
        "## Safety Note",
        "",
        report["safety_note"],
        "",
        "## Document Summaries",
        "",
        "| Document | Units | Done | Attention | Pending | Invalid | Manual tables | Matched tables | Label flags | Issues |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in summaries:
        lines.append(
            f"| {_md(row['document_id'])} | {int(row['unit_count']):,} | "
            f"{int(row['completed_unit_count']):,} | {int(row['attention_unit_count']):,} | "
            f"{int(row['pending_unit_count']):,} | {int(row['invalid_unit_count']):,} | "
            f"{int(row['manual_table_count_from_completed_units']):,} | "
            f"{int(row['matched_table_count_from_completed_units']):,} | "
            f"{int(row['label_flag_unit_count']):,} | {_md(row['issue_codes'])} |"
        )
    if issues:
        lines.extend(
            [
                "",
                "## Issues",
                "",
                "| Document | Rank | Issue | Key |",
                "| --- | ---: | --- | --- |",
            ]
        )
        for issue in issues:
            lines.append(
                f"| {_md(issue['document_id'])} | {_md(issue['unit_rank'])} | "
                f"{_md(issue['issue_code'])} | {_md(issue['table_unit_key'])} |"
            )
    lines.append("")
    return "\n".join(lines)


def _counter_text(counts: Any) -> str:
    if not isinstance(counts, dict) or not counts:
        return "-"
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize human-filled parsing goldset table-unit review fields by document."
    )
    parser.add_argument("--table-units-csv", required=True)
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-csv", required=True)
    parser.add_argument("--out-md", required=True)
    parser.add_argument("--source-compare-only", action="store_true")
    parser.add_argument("--max-md-rows", type=int, default=50)
    parser.add_argument(
        "--fail-on-issue",
        action="store_true",
        help="Exit non-zero when table review is not ready for score transfer.",
    )
    args = parser.parse_args(argv)

    report = summarize_parsing_goldset_table_unit_review(
        table_units_csv=Path(args.table_units_csv),
        out_json=Path(args.out_json),
        out_csv=Path(args.out_csv),
        out_md=Path(args.out_md),
        source_compare_only=args.source_compare_only,
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
                "required_field_missing_total": report["required_field_missing_total"],
                "ready_for_table_score_transfer": report["ready_for_table_score_transfer"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 2 if args.fail_on_issue and not report["ready_for_table_score_transfer"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
