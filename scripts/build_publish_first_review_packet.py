"""Build CSV packets for the first publish-readiness human review batch."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return payload if isinstance(payload, dict) else {}


def _load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [{key: str(value or "") for key, value in row.items() if key is not None} for row in reader]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _page_range(row: dict[str, str]) -> str:
    start = row.get("source_page_start", "").strip()
    end = row.get("source_page_end", "").strip()
    if start and end:
        return f"{start}-{end}"
    if start:
        return f"{start}-{start}"
    if end:
        return f"{end}-{end}"
    return ""


def _split_ranges(value: Any) -> set[str]:
    ranges: set[str] = set()
    for part in str(value or "").split(";"):
        text = part.strip()
        if text:
            ranges.add(text)
    return ranges


def _first_parser_document_ids(parser_batches: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for item in _list(parser_batches.get("first_review_batch")):
        document_id = str(_dict(item).get("document_id") or "").strip()
        if document_id:
            ids.add(document_id)
    return ids


def _first_table_targets(table_batches: dict[str, Any]) -> tuple[set[tuple[str, str]], list[dict[str, Any]]]:
    targets: set[tuple[str, str]] = set()
    first_batches: list[dict[str, Any]] = []
    for item in _list(table_batches.get("first_review_batches")):
        batch = _dict(item)
        if not batch:
            continue
        first_batches.append(batch)
        document_id = str(batch.get("document_id") or "").strip()
        for page_range in _split_ranges(batch.get("source_page_ranges")):
            targets.add((document_id, page_range))
    return targets, first_batches


def build_publish_first_review_packet(
    *,
    parser_open_item_worklist_csv: Path,
    parser_review_batches_report: Path,
    table_unit_worklist_csv: Path,
    table_review_batches_report: Path,
) -> dict[str, Any]:
    parser_rows = _load_rows(parser_open_item_worklist_csv)
    parser_batches = _load_json(parser_review_batches_report)
    table_rows = _load_rows(table_unit_worklist_csv)
    table_batches = _load_json(table_review_batches_report)

    parser_document_ids = _first_parser_document_ids(parser_batches)
    parser_packet_rows = [
        row
        for row in parser_rows
        if row.get("document_id", "").strip() in parser_document_ids
        or (
            "__missing_document_id__" in parser_document_ids
            and not row.get("document_id", "").strip()
        )
    ]

    table_targets, first_table_batches = _first_table_targets(table_batches)
    table_packet_rows = [
        row
        for row in table_rows
        if (row.get("document_id", "").strip(), _page_range(row)) in table_targets
    ]
    table_target_unit_count = sum(_int(batch.get("unit_count")) for batch in first_table_batches)

    return {
        "report_type": "publish_first_review_packet",
        "generated_at": _utc_now(),
        "source_parser_open_item_worklist_csv": str(parser_open_item_worklist_csv),
        "source_parser_review_batches_report": str(parser_review_batches_report),
        "source_table_unit_worklist_csv": str(table_unit_worklist_csv),
        "source_table_review_batches_report": str(table_review_batches_report),
        "parser_first_document_ids": sorted(parser_document_ids),
        "parser_packet_row_count": len(parser_packet_rows),
        "parser_expected_open_item_count": _int(parser_batches.get("first_batch_open_item_count")),
        "parser_packet_rows_match_expected": len(parser_packet_rows)
        == _int(parser_batches.get("first_batch_open_item_count")),
        "table_first_batch_ids": [
            str(batch.get("table_review_batch_id") or "") for batch in first_table_batches
        ],
        "table_packet_row_count": len(table_packet_rows),
        "table_expected_unit_count": table_target_unit_count,
        "table_packet_rows_match_expected": len(table_packet_rows) == table_target_unit_count,
        "parser_packet_rows": parser_packet_rows,
        "table_packet_rows": table_packet_rows,
        "safety_note": (
            "This packet is read-only. It selects rows for human review but does not fill labels, "
            "approve chunks, transfer table scores, or write Vector DB records."
        ),
        "api_call_count": 0,
    }


def render_markdown(report: dict[str, Any], *, parser_packet_csv: Path, table_packet_csv: Path) -> str:
    lines = [
        "# Publish First Review Packet",
        "",
        f"- Generated at: {report.get('generated_at')}",
        (
            "- Parser packet rows: "
            f"{report.get('parser_packet_row_count')} / expected {report.get('parser_expected_open_item_count')} "
            f"(match=`{str(report.get('parser_packet_rows_match_expected')).lower()}`)"
        ),
        (
            "- Table packet rows: "
            f"{report.get('table_packet_row_count')} / expected {report.get('table_expected_unit_count')} "
            f"(match=`{str(report.get('table_packet_rows_match_expected')).lower()}`)"
        ),
        f"- Parser packet CSV: `{parser_packet_csv}`",
        f"- Table packet CSV: `{table_packet_csv}`",
        "",
        "## Parser First Documents",
        "",
    ]
    for document_id in report.get("parser_first_document_ids") or []:
        lines.append(f"- `{document_id}`")
    lines.extend(["", "## Table First Batches", ""])
    for batch_id in report.get("table_first_batch_ids") or []:
        lines.append(f"- `{batch_id}`")
    lines.extend(["", f"> {report.get('safety_note')}", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parser-open-item-worklist-csv", required=True, type=Path)
    parser.add_argument("--parser-review-batches-report", required=True, type=Path)
    parser.add_argument("--table-unit-worklist-csv", required=True, type=Path)
    parser.add_argument("--table-review-batches-report", required=True, type=Path)
    parser.add_argument("--out-json", required=True, type=Path)
    parser.add_argument("--out-md", required=True, type=Path)
    parser.add_argument("--out-parser-csv", required=True, type=Path)
    parser.add_argument("--out-table-csv", required=True, type=Path)
    args = parser.parse_args(argv)

    report = build_publish_first_review_packet(
        parser_open_item_worklist_csv=args.parser_open_item_worklist_csv,
        parser_review_batches_report=args.parser_review_batches_report,
        table_unit_worklist_csv=args.table_unit_worklist_csv,
        table_review_batches_report=args.table_review_batches_report,
    )
    _write_json(args.out_json, report)
    _write_rows(args.out_parser_csv, [dict(row) for row in report["parser_packet_rows"]])
    _write_rows(args.out_table_csv, [dict(row) for row in report["table_packet_rows"]])
    _write_text(
        args.out_md,
        render_markdown(
            report,
            parser_packet_csv=args.out_parser_csv,
            table_packet_csv=args.out_table_csv,
        ),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
