from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from scripts.batch_process_regulations import build_batch_summary, write_reports


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def row_is_ocr_required(row: dict[str, Any]) -> bool:
    if _truthy(row.get("ocr_required")):
        return True
    return str(row.get("failure_category") or "").strip().lower() == "ocr_required"


def filter_batch_quality_report(
    batch_report_path: Path,
    *,
    exclude_ocr_required: bool = False,
) -> dict[str, Any]:
    source = load_json(batch_report_path)
    source_rows = [row for row in source.get("rows", []) or [] if isinstance(row, dict)]
    kept_rows: list[dict[str, Any]] = []
    excluded_rows: list[dict[str, Any]] = []
    for row in source_rows:
        if exclude_ocr_required and row_is_ocr_required(row):
            excluded_rows.append(row)
        else:
            kept_rows.append(row)

    summary = build_batch_summary(kept_rows)
    summary["report_type"] = "batch_quality_filtered"
    summary["generated_at"] = datetime.now(timezone.utc).isoformat()
    summary["source_batch_report_file"] = str(batch_report_path)
    summary["source_batch_report_sha256"] = sha256_file(batch_report_path)
    summary["source_batch_generated_at"] = source.get("generated_at")
    summary["source_input_count"] = source.get("input_count", len(source_rows))
    summary["source_successful_count"] = source.get("successful_count")
    summary["source_failed_count"] = source.get("failed_count")
    summary["filter_exclude_ocr_required"] = bool(exclude_ocr_required)
    summary["excluded_count"] = len(excluded_rows)
    summary["excluded_ocr_required_count"] = sum(1 for row in excluded_rows if row_is_ocr_required(row))
    summary["excluded_failure_category_counts"] = dict(
        sorted(Counter(str(row.get("failure_category") or "unclassified") for row in excluded_rows).items())
    )
    summary["excluded_rows_sample"] = [_excluded_row_sample(row) for row in excluded_rows[:20]]
    return summary


def _excluded_row_sample(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "filename": row.get("filename") or "",
        "input_path": row.get("input_path") or "",
        "status": row.get("status") or "",
        "failure_category": row.get("failure_category") or "",
        "ocr_required": row.get("ocr_required") or False,
        "failure_next_action": row.get("failure_next_action") or "",
        "error": row.get("error") or "",
    }


def _truthy(value: Any) -> bool:
    if value is True:
        return True
    if value in (False, None, ""):
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a filtered batch_quality report while preserving source/exclusion evidence."
    )
    parser.add_argument("--batch-report", required=True)
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--prefix", default="batch_quality_filtered")
    parser.add_argument(
        "--exclude-ocr-required",
        action="store_true",
        help="Exclude OCR-required failed rows so parser regression evidence can be separated from the OCR queue.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = filter_batch_quality_report(
        Path(args.batch_report),
        exclude_ocr_required=args.exclude_ocr_required,
    )
    report_paths = write_reports(summary, Path(args.reports_dir), prefix=args.prefix)
    print(json.dumps({"summary": summary, "reports": report_paths}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
