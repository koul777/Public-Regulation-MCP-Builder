from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath, Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.batch_process_regulations import SUMMARY_COLUMNS


PATH_ROW_FIELDS = ("input_path", "quality_json", "quality_md", "tables_csv", "tables_jsonl", "agent_review_plan_json")
PRIVATE_AI_EVIDENCE_FIELDS = (
    "agent_review_budget_reservation_id",
    "agent_review_approval_reference",
    "agent_review_model",
    "agent_review_payload_hash",
    "agent_review_estimated_cost",
    "agent_review_actual_cost",
    "agent_review_provider_request_id",
)
PRIVATE_OPERATIONAL_FIELDS = ("job_id", "reused_from_run_id", "reused_from_job_id")
PRIVATE_IDENTIFIER_FIELDS = ("document_id",)
PRIVATE_FIELD_PREFIXES = ("agent_review_", "historical_agent_review_")
LOCAL_SOURCE_SYSTEM_VALUES = {"LOCAL"}
LOCAL_SOURCE_URL_PREFIXES = ("local:",)
LOCAL_SAMPLE_FILENAME_PLACEHOLDER = "local-sample"
PUBLIC_ROW_ID_FIELD = "public_row_id"
PRIVATE_ROW_FIELDS = set(PATH_ROW_FIELDS) | set(PRIVATE_AI_EVIDENCE_FIELDS) | set(PRIVATE_OPERATIONAL_FIELDS) | set(
    PRIVATE_IDENTIFIER_FIELDS
)


def _is_private_field(key: str) -> bool:
    return key in PRIVATE_ROW_FIELDS or any(key.startswith(prefix) for prefix in PRIVATE_FIELD_PREFIXES)


PUBLIC_ROW_COLUMNS = [PUBLIC_ROW_ID_FIELD] + [
    column
    for column in SUMMARY_COLUMNS
    if not _is_private_field(column) and column != PUBLIC_ROW_ID_FIELD
]
WINDOWS_ABSOLUTE_RE = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]")
UNC_PATH_RE = re.compile(r"\\\\[^\\]+\\[^\\]+")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def export_public_batch_report(report: dict[str, Any], *, source_report_path: Path | None = None) -> dict[str, Any]:
    rows = [sanitize_row(row, row_number=index + 1) for index, row in enumerate(report.get("rows", []) or [])]
    public_report = {
        **{
            key: value
            for key, value in report.items()
            if key != "rows" and not _is_private_field(key) and not _value_contains_sensitive_path(value)
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_generated_at": report.get("generated_at", ""),
        "source_report_file": source_report_path.name if source_report_path else "",
        "report_type": "public_batch_quality",
        "sanitization": {
            "removed_path_field_count": len(PATH_ROW_FIELDS),
            "removed_private_ai_evidence_field_count": len(PRIVATE_AI_EVIDENCE_FIELDS),
            "removed_private_ai_field_prefixes": list(PRIVATE_FIELD_PREFIXES),
            "removed_private_operational_field_count": len(PRIVATE_OPERATIONAL_FIELDS),
            "removed_private_identifier_field_count": len(PRIVATE_IDENTIFIER_FIELDS),
            "redacted_local_provenance_fields": ["source_system", "source_url", "source_filename"],
            "local_sample_filename_placeholder": LOCAL_SAMPLE_FILENAME_PLACEHOLDER,
            "public_identifier_field": PUBLIC_ROW_ID_FIELD,
        },
        "rows": rows,
    }
    leaks = find_sensitive_path_leaks(public_report)
    public_report["sanitization"]["sensitive_path_leak_count"] = len(leaks)
    public_report["sanitization"]["sensitive_path_leak_samples"] = leaks[:20]
    return public_report


def sanitize_row(row: dict[str, Any], *, row_number: int = 1) -> dict[str, Any]:
    row_uses_local_provenance = _row_uses_local_provenance(row)
    public_row = {
        key: value
        for key, value in row.items()
        if not _is_private_field(key)
        and not (row_uses_local_provenance and key in {"filename", "source_filename"})
        and not _is_local_provenance_field_value(key, value)
        and not _value_contains_sensitive_path(value)
    }
    public_row[PUBLIC_ROW_ID_FIELD] = f"public-row-{row_number:04d}"
    public_row["source_filename"] = _public_source_filename(row)
    return {column: public_row.get(column, "") for column in PUBLIC_ROW_COLUMNS if column in public_row} | {
        key: value for key, value in public_row.items() if key not in PUBLIC_ROW_COLUMNS
    }


def _public_source_filename(row: dict[str, Any]) -> str:
    if _row_uses_local_provenance(row):
        return LOCAL_SAMPLE_FILENAME_PLACEHOLDER
    return _public_basename(row.get("input_path") or row.get("filename") or row.get("source_filename") or "")


def _row_uses_local_provenance(row: dict[str, Any]) -> bool:
    return any(_is_local_provenance_field_value(key, value) for key, value in row.items())


def _is_local_provenance_field_value(key: str, value: Any) -> bool:
    if key == "source_system":
        return str(value or "").strip().upper() in LOCAL_SOURCE_SYSTEM_VALUES
    if key == "source_url":
        normalized = str(value or "").strip().lower()
        return any(normalized.startswith(prefix) for prefix in LOCAL_SOURCE_URL_PREFIXES)
    return False


def _public_basename(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "\\" in raw or WINDOWS_ABSOLUTE_RE.search(raw):
        return PureWindowsPath(raw).name
    return PurePosixPath(raw).name


def find_sensitive_path_leaks(payload: Any) -> list[dict[str, str]]:
    leaks: list[dict[str, str]] = []
    _collect_path_leaks(payload, path="$", leaks=leaks)
    return leaks


def _collect_path_leaks(value: Any, *, path: str, leaks: list[dict[str, str]]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _collect_path_leaks(item, path=f"{path}.{key}", leaks=leaks)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _collect_path_leaks(item, path=f"{path}[{index}]", leaks=leaks)
        return
    if not isinstance(value, str) or not value:
        return
    if _looks_like_sensitive_path(value):
        leaks.append({"path": path, "value": value})


def _looks_like_sensitive_path(value: str) -> bool:
    normalized = value.strip()
    lowered = normalized.lower()
    return bool(
        WINDOWS_ABSOLUTE_RE.search(normalized)
        or UNC_PATH_RE.search(normalized)
        or lowered.startswith(("/users/", "/home/", "/var/", "/tmp/", "/mnt/", "/workspace/", "/data/", "/app/"))
    )


def _value_contains_sensitive_path(value: Any) -> bool:
    if isinstance(value, str):
        return _looks_like_sensitive_path(value)
    if isinstance(value, list):
        return any(_value_contains_sensitive_path(item) for item in value)
    if isinstance(value, dict):
        return any(_value_contains_sensitive_path(item) for item in value.values())
    return False


def to_csv(report: dict[str, Any]) -> str:
    rows = report.get("rows", []) or []
    if not rows:
        return ""
    columns: list[str] = []
    for preferred in PUBLIC_ROW_COLUMNS:
        if any(preferred in row for row in rows):
            columns.append(preferred)
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Public Batch Quality Report",
        "",
        f"- Report type: {report.get('report_type')}",
        f"- Source generated at: {report.get('source_generated_at')}",
        f"- Inputs: {report.get('input_count', 0)}",
        f"- Successful: {report.get('successful_count', 0)}",
        f"- Failed: {report.get('failed_count', 0)}",
        f"- Average quality score: {report.get('average_quality_score', 0)}",
        f"- Current AI estimated total tokens: {report.get('agent_review_estimated_total_tokens_total', 0)}",
        f"- Sensitive path leak count: {report.get('sanitization', {}).get('sensitive_path_leak_count', 0)}",
        "",
        "| file | public_row_id | status | score | chunks | ai review | ai tokens |",
        "| --- | --- | --- | ---: | ---: | --- | ---: |",
    ]
    for row in report.get("rows", []) or []:
        lines.append(
            "| {file} | {public_row_id} | {status} | {score} | {chunks} | {ai_status} | {ai_tokens} |".format(
                file=_escape_md(str(row.get("source_filename") or row.get("filename") or "")),
                public_row_id=_escape_md(str(row.get(PUBLIC_ROW_ID_FIELD, ""))),
                status=_escape_md(str(row.get("status", ""))),
                score=row.get("quality_score", ""),
                chunks=row.get("chunk_count", ""),
                ai_status=_escape_md(str(row.get("agent_review_status", ""))),
                ai_tokens=row.get("agent_review_estimated_total_tokens", ""),
            )
        )
    return "\n".join(lines) + "\n"


def _escape_md(value: str) -> str:
    return value.replace("|", "\\|")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a path-redacted public batch report from an internal batch report.")
    parser.add_argument("--batch-report", required=True)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-csv", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--fail-on-leak", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    batch_report_path = Path(args.batch_report)
    public_report = export_public_batch_report(load_json(batch_report_path), source_report_path=batch_report_path)
    if args.out_json:
        Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_json).write_text(json.dumps(public_report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.out_csv:
        Path(args.out_csv).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_csv).write_text(to_csv(public_report), encoding="utf-8-sig")
    if args.out_md:
        Path(args.out_md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_md).write_text(to_markdown(public_report), encoding="utf-8")
    print(
        json.dumps(
            {
                "report_type": public_report["report_type"],
                "input_count": public_report.get("input_count", 0),
                "successful_count": public_report.get("successful_count", 0),
                "failed_count": public_report.get("failed_count", 0),
                "sensitive_path_leak_count": public_report["sanitization"]["sensitive_path_leak_count"],
                "out_json": args.out_json,
                "out_csv": args.out_csv,
                "out_md": args.out_md,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.fail_on_leak and public_report["sanitization"]["sensitive_path_leak_count"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
