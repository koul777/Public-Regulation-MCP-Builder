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


OCR_MANIFEST_COLUMNS = [
    "input_path",
    "filename",
    "document_id",
    "document_name",
    "institution_name",
    "source_system",
    "source_url",
    "source_record_id",
    "source_file_id",
    "source_disclosure_date",
    "source_posted_date",
    "profile_id",
    "ocr_page_count",
    "previous_error",
    "failure_category",
    "failure_next_action",
]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def ocr_manifest_rows(batch_report: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in batch_report.get("rows", []) or []:
        if not _is_ocr_required(row):
            continue
        filename = str(row.get("filename") or "")
        rows.append(
            {
                "input_path": row.get("input_path") or "",
                "filename": filename,
                "document_id": row.get("document_id") or "",
                "document_name": row.get("document_name") or Path(filename).stem,
                "institution_name": row.get("institution_name") or "",
                "source_system": row.get("source_system") or "",
                "source_url": row.get("source_url") or "",
                "source_record_id": row.get("source_record_id") or "",
                "source_file_id": row.get("source_file_id") or "",
                "source_disclosure_date": row.get("source_disclosure_date") or "",
                "source_posted_date": row.get("source_posted_date") or "",
                "profile_id": row.get("profile_id") or "",
                "ocr_page_count": row.get("ocr_page_count") or "",
                "previous_error": row.get("error") or "",
                "failure_category": row.get("failure_category") or "",
                "failure_next_action": row.get("failure_next_action") or "",
            }
        )
    return rows


def export_ocr_manifest(
    batch_report_path: Path,
    *,
    out_csv: Path,
    out_json: Path | None = None,
    price_per_page: float | None = None,
    currency: str = "USD",
    budget: float | None = None,
) -> dict[str, Any]:
    batch_report = load_json(batch_report_path)
    rows = ocr_manifest_rows(batch_report)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OCR_MANIFEST_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    known_page_count = sum(1 for row in rows if _positive_int(row.get("ocr_page_count")) > 0)
    unknown_page_count = len(rows) - known_page_count
    estimated_pages = sum(_positive_int(row.get("ocr_page_count")) for row in rows)
    estimated_total_cost = None
    if price_per_page is not None:
        estimated_total_cost = round(estimated_pages * price_per_page, 6)
    budget_exceeded = bool(
        budget is not None
        and estimated_total_cost is not None
        and estimated_total_cost > budget
    )
    report = {
        "report_type": "ocr_manifest",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_batch_report_file": batch_report_path.name,
        "source_batch_generated_at": batch_report.get("generated_at"),
        "source_input_count": batch_report.get("input_count", 0),
        "source_failed_count": batch_report.get("failed_count", 0),
        "ocr_required_count": len(rows),
        "known_page_count": known_page_count,
        "unknown_page_count": unknown_page_count,
        "estimated_ocr_pages": estimated_pages,
        "price_per_page": price_per_page,
        "currency": currency,
        "estimated_total_cost": estimated_total_cost,
        "budget": budget,
        "budget_exceeded": budget_exceeded,
        "budget_evaluation_status": _budget_status(
            price_per_page=price_per_page,
            unknown_page_count=unknown_page_count,
            budget=budget,
            budget_exceeded=budget_exceeded,
        ),
        "api_call_count": 0,
        "mode": "manifest_only",
        "out_csv": str(out_csv),
        "next_action": "Run an approved OCR job for this manifest, then reprocess OCR output.",
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _is_ocr_required(row: dict[str, Any]) -> bool:
    if str(row.get("failure_category") or "").strip().lower() == "ocr_required":
        return True
    value = row.get("ocr_required")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _positive_int(value: Any) -> int:
    if isinstance(value, bool) or value in ("", None):
        return 0
    try:
        number = int(float(str(value)))
    except ValueError:
        return 0
    return number if number > 0 else 0


def _budget_status(
    *,
    price_per_page: float | None,
    unknown_page_count: int,
    budget: float | None,
    budget_exceeded: bool,
) -> str:
    if price_per_page is None:
        return "page_count_only"
    if unknown_page_count:
        return "estimate_missing_unknown_pages"
    if budget is None:
        return "estimated"
    return "budget_exceeded" if budget_exceeded else "within_budget"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export OCR-required rows from a batch quality report.")
    parser.add_argument("--batch-report", required=True)
    parser.add_argument("--out-csv", default=None)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--price-per-page", type=float, default=None)
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--budget", type=float, default=None)
    parser.add_argument("--fail-over-budget", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_csv = Path(args.out_csv) if args.out_csv else Path("reports") / f"ocr_manifest_{timestamp}.csv"
    out_json = Path(args.out_json) if args.out_json else out_csv.with_suffix(".json")
    report = export_ocr_manifest(
        Path(args.batch_report),
        out_csv=out_csv,
        out_json=out_json,
        price_per_page=args.price_per_page,
        currency=args.currency,
        budget=args.budget,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.fail_over_budget and report["budget_exceeded"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
