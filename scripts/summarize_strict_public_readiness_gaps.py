from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_metadata import current_repo_commit


HASH_CHUNK_BYTES = 1024 * 1024


def build_strict_public_readiness_gap_summary(
    *,
    readiness_report: Path,
    out_json: Path | None = None,
    out_md: Path | None = None,
    out_worklist_csv: Path | None = None,
    out_worklist_md: Path | None = None,
) -> dict[str, Any]:
    source = _load_json(readiness_report)
    failures = _dict(source.get("failures"))
    failed_info_rows = _rows(failures.get("failed_info_check_rows"))
    recommendation_rows = _rows(failures.get("recommendation_rows"))
    missing_required_rows = _rows(failures.get("missing_required_fields"))
    failed_checks = _failed_checks(source)
    recommendation_row_count_sum = sum(_int(row.get("recommendation_count")) for row in recommendation_rows)
    recommendation_total = (
        _failed_check_detail_int(failed_checks, "recommendations_within_limit", "recommendation_total")
        or recommendation_row_count_sum
    )
    failed_info_check_total = (
        _failed_check_detail_int(failed_checks, "failed_info_checks_within_limit", "failed_info_check_total")
        or len(failed_info_rows)
    )
    missing_required_field_total = (
        _failed_check_detail_int(failed_checks, "required_row_fields_present", "missing_count")
        or sum(_missing_field_counts(missing_required_rows).values())
    )
    missing_field_counts = _missing_field_counts(missing_required_rows)
    report = {
        "report_type": "strict_public_readiness_gap_summary",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "source_report": str(readiness_report),
        "source_report_artifact": _source_artifact("strict_public_readiness_report", readiness_report),
        "source_passed": bool(source.get("passed")),
        "passed": bool(source.get("passed")),
        "status": "strict_public_readiness_ready" if source.get("passed") else "strict_public_readiness_blocked",
        "readiness_profile": source.get("readiness_profile"),
        "strict_release_evidence": bool(source.get("strict_release_evidence")),
        "failed_check_count": len(failed_checks),
        "failed_checks": failed_checks,
        "gap_counts": {
            "failed_info_check_row_count": len(failed_info_rows),
            "failed_info_check_total": failed_info_check_total,
            "recommendation_row_count": len(recommendation_rows),
            "recommendation_total": recommendation_total,
            "recommendation_row_count_sum": recommendation_row_count_sum,
            "missing_required_field_row_count": len(missing_required_rows),
            "missing_required_field_total": missing_required_field_total,
            "missing_artifact_count": len(_rows(failures.get("missing_artifacts"))),
            "ocr_required_row_count": len(_rows(failures.get("ocr_required_rows"))),
            "failed_row_count": len(_rows(failures.get("failed_rows"))),
        },
        "file_format_counts": _top_counts(
            [
                _file_suffix(row)
                for row in [*failed_info_rows, *recommendation_rows, *missing_required_rows]
                if _file_suffix(row)
            ]
        ),
        "affected_institution_counts": _top_counts(
            [str(row.get("institution_name") or "") for row in [*failed_info_rows, *recommendation_rows, *missing_required_rows]]
        ),
        "affected_profile_counts": _top_counts(
            [str(row.get("profile_id") or "") for row in [*failed_info_rows, *recommendation_rows, *missing_required_rows]]
        ),
        "top_recommendations": _top_counts(
            [str(row.get("top_recommendation") or "") for row in recommendation_rows],
            limit=20,
        ),
        "missing_required_field_counts": dict(sorted(missing_field_counts.items())),
        "missing_required_field_profile_counts": _missing_required_field_profile_counts(missing_required_rows),
        "remediation_work_items": _remediation_work_items(
            failed_info_rows=failed_info_rows,
            recommendation_rows=recommendation_rows,
            missing_required_rows=missing_required_rows,
            missing_field_counts=missing_field_counts,
            recommendation_total=recommendation_total,
        ),
        "worklist_summary": _worklist_summary(
            failed_info_rows=failed_info_rows,
            recommendation_rows=recommendation_rows,
            missing_required_rows=missing_required_rows,
        ),
        "sample_rows": {
            "failed_info_checks": _sample_rows(failed_info_rows),
            "recommendations": _sample_rows(recommendation_rows),
            "missing_required_fields": _sample_rows(missing_required_rows),
        },
        "safety_note": (
            "This summary is read-only and may include source identifiers from the strict readiness report. "
            "Do not publish it without redaction review."
        ),
        "api_call_count": 0,
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    if out_worklist_csv:
        _write_worklist_csv(
            out_worklist_csv,
            failed_info_rows=failed_info_rows,
            recommendation_rows=recommendation_rows,
            missing_required_rows=missing_required_rows,
        )
    if out_worklist_md:
        out_worklist_md.parent.mkdir(parents=True, exist_ok=True)
        out_worklist_md.write_text(
            _worklist_to_markdown(
                _worklist_rows(
                    failed_info_rows=failed_info_rows,
                    recommendation_rows=recommendation_rows,
                    missing_required_rows=missing_required_rows,
                ),
                report=report,
            ),
            encoding="utf-8",
        )
    return report


def _failed_checks(source: dict[str, Any]) -> list[dict[str, Any]]:
    checks = []
    for check in source.get("checks") or []:
        if isinstance(check, dict) and not check.get("passed"):
            checks.append(
                {
                    "name": check.get("name"),
                    "details": _dict(check.get("details")),
                }
            )
    return checks


def _failed_check_detail_int(failed_checks: list[dict[str, Any]], name: str, key: str) -> int:
    for check in failed_checks:
        if check.get("name") == name:
            return _int(_dict(check.get("details")).get(key))
    return 0


def _missing_field_counts(rows: list[dict[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        fields = row.get("missing_fields")
        if isinstance(fields, list):
            for field in fields:
                counts[str(field)] += 1
    return counts


def _missing_required_field_profile_counts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str]] = Counter()
    for row in rows:
        profile = str(row.get("profile_id") or "")
        fields = row.get("missing_fields")
        if not isinstance(fields, list):
            continue
        for field in fields:
            counts[(str(field), profile)] += 1
    return [
        {"field": field, "profile_id": profile, "count": count}
        for (field, profile), count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _remediation_work_items(
    *,
    failed_info_rows: list[dict[str, Any]],
    recommendation_rows: list[dict[str, Any]],
    missing_required_rows: list[dict[str, Any]],
    missing_field_counts: Counter[str],
    recommendation_total: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if failed_info_rows:
        items.append(
            {
                "item_id": "failed_info_check_triage",
                "status": "needs_parser_or_report_review",
                "affected_row_count": len(failed_info_rows),
                "operator_action": "Inspect failed info checks and decide whether parser output or readiness expectations need correction.",
            }
        )
    if recommendation_rows:
        items.append(
            {
                "item_id": "recommendation_triage",
                "status": "needs_parser_quality_review",
                "affected_row_count": len(recommendation_rows),
                "recommendation_total": recommendation_total,
                "operator_action": "Group repeated recommendations and fix parser/table classification before strict release evidence.",
            }
        )
    if missing_required_rows:
        items.append(
            {
                "item_id": "required_metadata_backfill",
                "status": "needs_source_metadata_backfill",
                "affected_row_count": len(missing_required_rows),
                "missing_required_field_counts": dict(sorted(missing_field_counts.items())),
                "operator_action": "Backfill or justify required source metadata before using strict public readiness as release evidence.",
            }
        )
    return items


def _sample_rows(rows: list[dict[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
    fields = (
        "filename",
        "document_id",
        "institution_name",
        "profile_id",
        "source_record_id",
        "source_file_id",
        "failed_info_check_count",
        "recommendation_count",
        "top_recommendation",
        "missing_fields",
    )
    samples = []
    for row in rows[:limit]:
        samples.append({field: row.get(field) for field in fields if field in row})
    return samples


WORKLIST_FIELDS = (
    "issue_id",
    "issue_type",
    "severity",
    "operator_action",
    "filename",
    "file_format",
    "document_id",
    "institution_name",
    "profile_id",
    "source_record_id",
    "source_file_id",
    "apba_id",
    "failed_info_check_count",
    "recommendation_count",
    "top_recommendation",
    "missing_fields",
)


def _worklist_summary(
    *,
    failed_info_rows: list[dict[str, Any]],
    recommendation_rows: list[dict[str, Any]],
    missing_required_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "row_count": len(failed_info_rows) + len(recommendation_rows) + len(missing_required_rows),
        "issue_type_counts": {
            "failed_info_check": len(failed_info_rows),
            "recommendation": len(recommendation_rows),
            "missing_required_field": len(missing_required_rows),
        },
    }


def _worklist_rows(
    *,
    failed_info_rows: list[dict[str, Any]],
    recommendation_rows: list[dict[str, Any]],
    missing_required_rows: list[dict[str, Any]],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for row in failed_info_rows:
        rows.append(
            _worklist_row(
                row,
                issue_type="failed_info_check",
                severity="high",
                operator_action="Inspect failed info checks and decide whether parser output or readiness expectations need correction.",
            )
        )
    for row in recommendation_rows:
        rows.append(
            _worklist_row(
                row,
                issue_type="recommendation",
                severity="medium",
                operator_action="Group repeated recommendations and fix parser/table classification before strict release evidence.",
            )
        )
    for row in missing_required_rows:
        rows.append(
            _worklist_row(
                row,
                issue_type="missing_required_field",
                severity="high",
                operator_action="Backfill required source metadata or record a release-owner exception.",
            )
        )
    for index, row in enumerate(rows, start=1):
        row["issue_id"] = f"strict-gap-{index:04d}"
    return rows


def _worklist_row(row: dict[str, Any], *, issue_type: str, severity: str, operator_action: str) -> dict[str, str]:
    missing_fields = row.get("missing_fields")
    return {
        "issue_id": "",
        "issue_type": issue_type,
        "severity": severity,
        "operator_action": operator_action,
        "filename": str(row.get("filename") or ""),
        "file_format": _file_suffix(row),
        "document_id": str(row.get("document_id") or ""),
        "institution_name": str(row.get("institution_name") or ""),
        "profile_id": str(row.get("profile_id") or ""),
        "source_record_id": str(row.get("source_record_id") or ""),
        "source_file_id": str(row.get("source_file_id") or ""),
        "apba_id": str(row.get("apba_id") or ""),
        "failed_info_check_count": str(_int(row.get("failed_info_check_count"))),
        "recommendation_count": str(_int(row.get("recommendation_count"))),
        "top_recommendation": str(row.get("top_recommendation") or ""),
        "missing_fields": "; ".join(str(field) for field in missing_fields) if isinstance(missing_fields, list) else "",
    }


def _write_worklist_csv(
    path: Path,
    *,
    failed_info_rows: list[dict[str, Any]],
    recommendation_rows: list[dict[str, Any]],
    missing_required_rows: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = _worklist_rows(
        failed_info_rows=failed_info_rows,
        recommendation_rows=recommendation_rows,
        missing_required_rows=missing_required_rows,
    )
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=WORKLIST_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _worklist_to_markdown(rows: list[dict[str, str]], *, report: dict[str, Any]) -> str:
    lines = [
        "# Strict Public Readiness Gap Worklist",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Status: `{report.get('status')}`",
        f"- Worklist rows: {len(rows)}",
        "",
        "| Issue ID | Type | Severity | File | Profile | Action |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| {issue_id} | {issue_type} | {severity} | {filename} | {profile_id} | {operator_action} |".format(
                issue_id=_md_cell(row["issue_id"]),
                issue_type=_md_cell(row["issue_type"]),
                severity=_md_cell(row["severity"]),
                filename=_md_cell(row["filename"]),
                profile_id=_md_cell(row["profile_id"]),
                operator_action=_md_cell(row["operator_action"]),
            )
        )
    lines.extend(
        [
            "",
            "> Worklist rows intentionally omit local input_path values. Review before public distribution.",
            "",
        ]
    )
    return "\n".join(lines)


def _top_counts(values: Iterable[str], *, limit: int = 20) -> list[dict[str, Any]]:
    counter = Counter(value for value in values if value)
    return [
        {"value": value, "count": count}
        for value, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def _file_suffix(row: dict[str, Any]) -> str:
    suffix = Path(str(row.get("filename") or "")).suffix.lower()
    return suffix.lstrip(".")


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int:
    if isinstance(value, bool) or value in (None, ""):
        return 0
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _source_artifact(role: str, path: Path) -> dict[str, Any]:
    exists = path.exists()
    return {
        "role": role,
        "path": str(path),
        "exists": exists,
        "sha256": _sha256_file(path) if exists else None,
        "byte_count": path.stat().st_size if exists else None,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Strict Public Readiness Gap Summary",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Status: `{report.get('status')}`",
        f"- Source passed: `{str(report.get('source_passed')).lower()}`",
        f"- Readiness profile: `{report.get('readiness_profile')}`",
        "",
        "## Gap Counts",
        "",
    ]
    for key, value in _dict(report.get("gap_counts")).items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Failed Checks", ""])
    for check in report.get("failed_checks") or []:
        if isinstance(check, dict):
            lines.append(f"- `{check.get('name')}`: {_compact_json(check.get('details'))}")
    lines.extend(["", "## Top Recommendations", ""])
    recommendations = report.get("top_recommendations") or []
    if not recommendations:
        lines.append("- None.")
    for item in recommendations[:10]:
        if isinstance(item, dict):
            lines.append(f"- {item.get('count')}: {item.get('value')}")
    lines.extend(["", "## Missing Required Fields", ""])
    missing = _dict(report.get("missing_required_field_counts"))
    if not missing:
        lines.append("- None.")
    for field, count in missing.items():
        lines.append(f"- `{field}`: {count}")
    lines.extend(["", "## Remediation Work Items", ""])
    for item in report.get("remediation_work_items") or []:
        if isinstance(item, dict):
            lines.append(
                f"- `{item.get('item_id')}`: {item.get('status')}, "
                f"affected rows {item.get('affected_row_count')}. {item.get('operator_action')}"
            )
    lines.extend(["", f"> {report.get('safety_note')}", ""])
    return "\n".join(lines)


def _compact_json(value: Any) -> str:
    if not value:
        return "{}"
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize strict public readiness parser/metadata gaps.")
    parser.add_argument("--readiness-report", type=Path, required=True)
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-md", type=Path)
    parser.add_argument("--out-worklist-csv", type=Path)
    parser.add_argument("--out-worklist-md", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    if stdout is sys.stdout and hasattr(stdout, "reconfigure"):
        stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    report = build_strict_public_readiness_gap_summary(
        readiness_report=args.readiness_report,
        out_json=args.out_json,
        out_md=args.out_md,
        out_worklist_csv=args.out_worklist_csv,
        out_worklist_md=args.out_worklist_md,
    )
    if args.json:
        stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    else:
        stdout.write(_to_markdown(report))
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
