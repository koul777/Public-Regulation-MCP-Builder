from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


RETRY_MANIFEST_COLUMNS = [
    "input_path",
    "document_name",
    "institution_name",
    "source_system",
    "source_url",
    "source_record_id",
    "source_file_id",
    "source_disclosure_date",
    "source_posted_date",
    "profile_id",
    "previous_status",
    "failure_category",
    "failure_next_action",
    "previous_error",
    "previous_document_id",
]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def retry_manifest_rows(
    batch_report: dict[str, Any],
    *,
    require_existing_files: bool = False,
    include_ocr_required: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in batch_report.get("rows", []) or []:
        if row.get("status") != "failed":
            continue
        input_path = str(row.get("input_path") or "")
        if not input_path:
            skipped.append(_skip(row, "missing_input_path"))
            continue
        if _is_explicit_false(row.get("retry_recommended")) and not (
            include_ocr_required and _is_ocr_required_failure(row)
        ):
            skipped.append(_skip(row, "not_retry_recommended"))
            continue
        if require_existing_files and not Path(input_path).is_file():
            skipped.append(_skip(row, "input_file_not_found"))
            continue
        rows.append(
            {
                "input_path": input_path,
                "document_name": row.get("document_name") or "",
                "institution_name": row.get("institution_name") or "",
                "source_system": row.get("source_system") or "",
                "source_url": row.get("source_url") or "",
                "source_record_id": row.get("source_record_id") or "",
                "source_file_id": row.get("source_file_id") or "",
                "source_disclosure_date": row.get("source_disclosure_date") or "",
                "source_posted_date": row.get("source_posted_date") or "",
                "profile_id": row.get("profile_id") or "",
                "previous_status": row.get("status") or "",
                "failure_category": row.get("failure_category") or "",
                "failure_next_action": row.get("failure_next_action") or "",
                "previous_error": row.get("error") or "",
                "previous_document_id": row.get("document_id") or "",
            }
        )
    return rows, skipped


def export_retry_manifest(
    batch_report_path: Path,
    *,
    out_csv: Path,
    out_json: Path | None = None,
    require_existing_files: bool = False,
    include_ocr_required: bool = False,
) -> dict[str, Any]:
    batch_report = load_json(batch_report_path)
    rows, skipped = retry_manifest_rows(
        batch_report,
        require_existing_files=require_existing_files,
        include_ocr_required=include_ocr_required,
    )
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=RETRY_MANIFEST_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    report = {
        "report_type": "batch_retry_manifest",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_batch_report_file": batch_report_path.name,
        "source_batch_generated_at": batch_report.get("generated_at"),
        "source_input_count": batch_report.get("input_count", 0),
        "source_failed_count": batch_report.get("failed_count", 0),
        "retryable_count": len(rows),
        "skipped_count": len(skipped),
        "skipped": skipped[:50],
        "out_csv": str(out_csv),
        "include_ocr_required": include_ocr_required,
        "next_command": _next_command(out_csv, include_ocr_required=include_ocr_required),
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _skip(row: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "filename": row.get("filename", ""),
        "document_id": row.get("document_id", ""),
        "input_path": row.get("input_path", ""),
        "reason": reason,
        "failure_category": row.get("failure_category", ""),
        "failure_next_action": row.get("failure_next_action", ""),
        "previous_error": row.get("error", ""),
    }


def _is_explicit_false(value: Any) -> bool:
    if value is False:
        return True
    if value in ("", None):
        return False
    return str(value).strip().lower() in {"0", "false", "no", "n"}


def _is_ocr_required_failure(row: dict[str, Any]) -> bool:
    if row.get("ocr_required") is True:
        return True
    if str(row.get("ocr_required") or "").strip().lower() in {"1", "true", "yes", "y"}:
        return True
    return str(row.get("failure_category") or "").strip().lower() == "ocr_required"


def _next_command(out_csv: Path, *, include_ocr_required: bool) -> str:
    command = (
        "python scripts/batch_process_regulations.py "
        f"--manifest-csv {out_csv} --force-reprocess"
    )
    if include_ocr_required:
        command += " --pdf-ocr-backend windows --pdf-ocr-language ko"
    return command


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export a retry manifest from failed rows in a batch quality report.")
    parser.add_argument("--batch-report", required=True)
    parser.add_argument("--out-csv", default=None)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--require-existing-files", action="store_true")
    parser.add_argument(
        "--include-ocr-required",
        action="store_true",
        help="Include OCR-required failed rows even when they were not marked as ordinary retryable failures.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_csv = Path(args.out_csv) if args.out_csv else Path("reports") / f"batch_retry_manifest_{timestamp}.csv"
    out_json = Path(args.out_json) if args.out_json else out_csv.with_suffix(".json")
    report = export_retry_manifest(
        Path(args.batch_report),
        out_csv=out_csv,
        out_json=out_json,
        require_existing_files=args.require_existing_files,
        include_ocr_required=args.include_ocr_required,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
