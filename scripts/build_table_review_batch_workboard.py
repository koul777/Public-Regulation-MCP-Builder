"""Build a first-pass workboard for table-unit human review batches."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BATCH_FIELDS = (
    "batch_rank",
    "in_first_review_batch",
    "table_review_batch_id",
    "document_id",
    "review_priority",
    "unit_count",
    "source_page_ranges",
    "review_priority_counts",
    "label_review_flag_counts",
    "source_path",
    "open_source_command",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value or "").strip())
    except ValueError:
        return default


def _load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [{key: str(value or "") for key, value in row.items() if key is not None} for row in reader]


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return payload if isinstance(payload, dict) else {}


def _parse_count_map(value: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for part in str(value or "").split(";"):
        text = part.strip()
        if not text:
            continue
        if "=" not in text:
            counts[text] += 1
            continue
        key, raw_count = text.split("=", 1)
        counts[key.strip()] += _int(raw_count, 1)
    return counts


def _ps_literal(path: str) -> str:
    return path.replace("'", "''")


def _compact_counts(counter: Counter[str]) -> dict[str, int]:
    return dict(sorted(counter.items()))


def build_table_review_batch_workboard(
    *,
    table_review_batches_csv: Path,
    source_traceability_report: Path | None = None,
    first_batch_count: int = 5,
) -> dict[str, Any]:
    rows = _load_rows(table_review_batches_csv)
    traceability = _load_json(source_traceability_report)
    normalized: list[dict[str, Any]] = []
    review_priority_counts: Counter[str] = Counter()
    label_review_flag_counts: Counter[str] = Counter()
    document_counts: Counter[str] = Counter()
    human_status_missing_count = 0

    for row in rows:
        rank = _int(row.get("batch_rank"))
        unit_count = _int(row.get("unit_count"))
        source_path = row.get("source_path", "")
        review_counts = _parse_count_map(row.get("review_priority_counts", ""))
        flag_counts = _parse_count_map(row.get("label_review_flag_counts", ""))
        review_priority_counts.update(review_counts)
        label_review_flag_counts.update(flag_counts)
        document_counts[row.get("document_id", "")] += unit_count
        if not str(row.get("human_batch_status") or "").strip():
            human_status_missing_count += 1
        normalized.append(
            {
                "batch_rank": rank,
                "table_review_batch_id": row.get("table_review_batch_id", ""),
                "document_id": row.get("document_id", ""),
                "review_priority": row.get("review_priority", ""),
                "unit_count": unit_count,
                "source_page_ranges": row.get("source_page_ranges", ""),
                "review_priority_counts": dict(review_counts),
                "label_review_flag_counts": dict(flag_counts),
                "source_path": source_path,
                "open_source_command": (
                    f"Invoke-Item -LiteralPath '{_ps_literal(source_path)}'" if source_path else ""
                ),
            }
        )

    normalized.sort(key=lambda item: (_int(item.get("batch_rank"), 999999), str(item.get("document_id"))))
    for index, batch in enumerate(normalized, start=1):
        batch["batch_rank"] = index
        batch["in_first_review_batch"] = index <= first_batch_count

    first_batches = [batch for batch in normalized if batch["in_first_review_batch"]]
    return {
        "report_type": "table_review_batch_workboard",
        "generated_at": _utc_now(),
        "source_table_review_batches_csv": str(table_review_batches_csv),
        "source_traceability_report": str(source_traceability_report)
        if source_traceability_report
        else None,
        "batch_count": len(normalized),
        "unit_count": sum(_int(batch.get("unit_count")) for batch in normalized),
        "first_batch_count": len(first_batches),
        "first_batch_unit_count": sum(_int(batch.get("unit_count")) for batch in first_batches),
        "document_count": len([key for key in document_counts if key]),
        "human_status_missing_batch_count": human_status_missing_count,
        "review_priority_counts": _compact_counts(review_priority_counts),
        "label_review_flag_counts": _compact_counts(label_review_flag_counts),
        "traceability_summary": {
            "passed": bool(traceability.get("traceability_passed")),
            "issue_count": _int(traceability.get("issue_count")),
            "blocked_batch_count": _int(traceability.get("blocked_batch_count")),
            "page_count_status_counts": traceability.get("page_count_status_counts") or {},
            "source_format_status_counts": traceability.get("source_format_status_counts") or {},
        },
        "first_review_batches": first_batches,
        "review_batches": normalized,
        "safety_note": (
            "This table review batch workboard is read-only. It does not fill human review fields, "
            "approve chunks, transfer table scores, or write Vector DB records."
        ),
        "api_call_count": 0,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "-").replace("|", "\\|").replace("\n", " ")


def _compact_dict(value: Any, *, limit: int = 4) -> str:
    if not isinstance(value, dict) or not value:
        return "-"
    items = list(value.items())
    text = ", ".join(f"{key}={count}" for key, count in items[:limit])
    if len(items) > limit:
        text += f", ... (+{len(items) - limit})"
    return text


def render_markdown(report: dict[str, Any]) -> str:
    traceability = report.get("traceability_summary") or {}
    lines = [
        "# Table Review Batch Workboard",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Review batches / units: {report.get('batch_count')} / {report.get('unit_count')}",
        f"- First review batch: {report.get('first_batch_count')} batches / {report.get('first_batch_unit_count')} units",
        f"- Human batch status missing: {report.get('human_status_missing_batch_count')}",
        f"- Source traceability: `{str(bool(traceability.get('passed'))).lower()}`; issues {traceability.get('issue_count')}",
        f"- Review priorities: {_compact_dict(report.get('review_priority_counts'))}",
        f"- Label flags: {_compact_dict(report.get('label_review_flag_counts'))}",
        "",
        "## First Review Batches",
        "",
        "| Rank | Batch ID | Document | Priority | Units | Pages | Flags |",
        "| ---: | --- | --- | --- | ---: | --- | --- |",
    ]
    for batch in report.get("first_review_batches") or []:
        if not isinstance(batch, dict):
            continue
        lines.append(
            "| {rank} | {batch_id} | {doc} | {priority} | {units} | {pages} | {flags} |".format(
                rank=_md_cell(batch.get("batch_rank")),
                batch_id=_md_cell(batch.get("table_review_batch_id")),
                doc=_md_cell(batch.get("document_id")),
                priority=_md_cell(batch.get("review_priority")),
                units=_md_cell(batch.get("unit_count")),
                pages=_md_cell(batch.get("source_page_ranges")),
                flags=_md_cell(_compact_dict(batch.get("label_review_flag_counts"), limit=3)),
            )
        )
    lines.extend(["", "## Source Commands", ""])
    for batch in report.get("first_review_batches") or []:
        if not isinstance(batch, dict):
            continue
        lines.append(f"- `{batch.get('table_review_batch_id')}`: `{batch.get('open_source_command') or '-'}`")
    lines.extend(["", f"> {report.get('safety_note')}", ""])
    return "\n".join(lines)


def write_batch_csv(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=BATCH_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(report.get("review_batches") or [])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table-review-batches-csv", required=True, type=Path)
    parser.add_argument("--source-traceability-report", type=Path)
    parser.add_argument("--first-batch-count", type=int, default=5)
    parser.add_argument("--out-json", required=True, type=Path)
    parser.add_argument("--out-csv", required=True, type=Path)
    parser.add_argument("--out-md", required=True, type=Path)
    args = parser.parse_args(argv)

    report = build_table_review_batch_workboard(
        table_review_batches_csv=args.table_review_batches_csv,
        source_traceability_report=args.source_traceability_report,
        first_batch_count=args.first_batch_count,
    )
    _write_json(args.out_json, report)
    write_batch_csv(args.out_csv, report)
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
