from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


CSV_FIELDNAMES = [
    "batch_rank",
    "table_review_batch_id",
    "document_id",
    "review_priority",
    "unit_count",
    "unit_ranks",
    "unit_key_fingerprint",
    "source_path",
    "filename",
    "extension",
    "source_page_ranges",
    "review_priority_counts",
    "label_review_flag_counts",
    "table_unit_packet_csv",
    "human_batch_status",
    "human_reviewer",
    "human_reviewed_at",
    "human_notes",
]

PRIORITY_ORDER = {
    "source_table_compare": 0,
    "parser_structure_review": 1,
    "parentage_review": 2,
    "structured_spot_check": 3,
    "low_signal_table_candidate": 4,
}


def build_parsing_goldset_table_review_batches(
    *,
    table_units_csv: Path,
    out_json: Path,
    out_csv: Path,
    out_md: Path,
    source_compare_only: bool = False,
    max_units_per_batch: int = 20,
    max_md_rows: int = 60,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if max_units_per_batch <= 0:
        raise ValueError("max_units_per_batch must be greater than zero.")
    if max_md_rows <= 0:
        raise ValueError("max_md_rows must be greater than zero.")
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    rows = _load_rows(table_units_csv)
    selected_rows = [
        row for row in rows if not source_compare_only or row.get("review_priority") == "source_table_compare"
    ]
    batches = _build_batches(
        selected_rows,
        table_units_csv=table_units_csv,
        max_units_per_batch=max_units_per_batch,
    )
    for index, batch in enumerate(batches, start=1):
        batch["batch_rank"] = str(index)

    document_counts = Counter(batch["document_id"] for batch in batches)
    document_unit_counts = Counter(row.get("document_id") or "(missing-document)" for row in selected_rows)
    burndown_summary = _burndown_summary(selected_rows=selected_rows, batches=batches)
    report = {
        "report_type": "parsing_goldset_table_review_batches",
        "generated_at": generated_at,
        "source_table_units_csv": str(table_units_csv),
        "source_compare_only": source_compare_only,
        "row_count": len(rows),
        "selected_unit_count": len(selected_rows),
        "batch_count": len(batches),
        "document_count": len(document_counts),
        "max_units_per_batch": max_units_per_batch,
        "document_batch_counts": dict(document_counts),
        "document_unit_counts": dict(document_unit_counts),
        "burndown_summary": burndown_summary,
        "max_md_rows": max_md_rows,
        "artifacts": {
            "json": str(out_json),
            "csv": str(out_csv),
            "markdown": str(out_md),
        },
        "safety_note": (
            "This table review batch manifest is read-only. It does not fill goldset labels, approve chunks, "
            "acknowledge review flags, index vectors, or publish MCP evidence."
        ),
    }

    _write_csv(out_csv, batches)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps({**report, "batches": batches}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(_markdown(report, batches[:max_md_rows]), encoding="utf-8")
    return report


def _load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    if not rows:
        raise ValueError("table_units_csv must contain at least one row.")
    required = {"document_id", "table_unit_key", "review_priority", "unit_rank"}
    missing = sorted(required - set(rows[0]))
    if missing:
        raise ValueError(f"table_units_csv missing required columns: {', '.join(missing)}")
    return rows


def _build_batches(
    rows: list[dict[str, str]],
    *,
    table_units_csv: Path,
    max_units_per_batch: int,
) -> list[dict[str, str]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row.get("document_id") or "(missing-document)"].append(row)

    batches: list[dict[str, str]] = []
    for document_id, document_rows in sorted(grouped.items(), key=lambda item: _document_sort_key(item[1], item[0])):
        document_rows.sort(key=_unit_sort_key)
        for local_index, start in enumerate(range(0, len(document_rows), max_units_per_batch), start=1):
            unit_rows = document_rows[start : start + max_units_per_batch]
            first = unit_rows[0]
            priority_counts = Counter(row.get("review_priority") or "" for row in unit_rows)
            label_counts = Counter(
                flag
                for row in unit_rows
                for flag in _split_joined(row.get("table_label_review_flags") or "")
            )
            unit_keys = [row.get("table_unit_key") or "" for row in unit_rows]
            fingerprint = _fingerprint(unit_keys)
            batch_priority = min(
                (row.get("review_priority") or "low_signal_table_candidate" for row in unit_rows),
                key=lambda priority: PRIORITY_ORDER.get(priority, 99),
            )
            batches.append(
                {
                    "batch_rank": "",
                    "table_review_batch_id": (
                        f"table-review-{_safe_id(document_id)}-{local_index:03d}-{fingerprint[:12]}"
                    ),
                    "document_id": document_id,
                    "review_priority": batch_priority,
                    "unit_count": str(len(unit_rows)),
                    "unit_ranks": "; ".join(row.get("unit_rank") or "" for row in unit_rows),
                    "unit_key_fingerprint": fingerprint,
                    "source_path": first.get("source_path") or "",
                    "filename": first.get("filename") or "",
                    "extension": first.get("extension") or "",
                    "source_page_ranges": "; ".join(_page_ranges(unit_rows)),
                    "review_priority_counts": _counts_text(priority_counts),
                    "label_review_flag_counts": _counts_text(label_counts),
                    "table_unit_packet_csv": str(table_units_csv),
                    "human_batch_status": "",
                    "human_reviewer": "",
                    "human_reviewed_at": "",
                    "human_notes": "",
                }
            )
    return batches


def _burndown_summary(
    *,
    selected_rows: list[dict[str, str]],
    batches: list[dict[str, str]],
) -> dict[str, Any]:
    priority_unit_counts = Counter(row.get("review_priority") or "(missing-priority)" for row in selected_rows)
    label_flag_unit_counts = Counter(
        flag for row in selected_rows for flag in _split_joined(row.get("table_label_review_flags") or "")
    )
    extension_unit_counts = Counter((row.get("extension") or "(missing-extension)").lower() for row in selected_rows)
    batch_priority_counts = Counter(batch.get("review_priority") or "(missing-priority)" for batch in batches)
    document_unit_counts = Counter(row.get("document_id") or "(missing-document)" for row in selected_rows)
    high_attention_priorities = {"source_table_compare", "parser_structure_review", "parentage_review"}
    high_attention_unit_count = sum(
        count for priority, count in priority_unit_counts.items() if priority in high_attention_priorities
    )
    top_documents = [
        {"document_id": document_id, "unit_count": count}
        for document_id, count in sorted(document_unit_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
    ]
    return {
        "selected_unit_count": len(selected_rows),
        "batch_count": len(batches),
        "high_attention_unit_count": high_attention_unit_count,
        "high_attention_priority_names": sorted(high_attention_priorities),
        "priority_unit_counts": dict(sorted(priority_unit_counts.items())),
        "batch_priority_counts": dict(sorted(batch_priority_counts.items())),
        "label_flag_unit_counts": dict(sorted(label_flag_unit_counts.items())),
        "extension_unit_counts": dict(sorted(extension_unit_counts.items())),
        "top_documents_by_unit_count": top_documents,
        "recommended_review_order": [
            "source_table_compare",
            "parser_structure_review",
            "parentage_review",
            "structured_spot_check",
            "low_signal_table_candidate",
        ],
        "safety_note": "This summary prioritizes human review work only; it does not mark any unit reviewed.",
    }


def _document_sort_key(rows: list[dict[str, str]], document_id: str) -> tuple[int, int, str]:
    source_compare = sum(1 for row in rows if row.get("review_priority") == "source_table_compare")
    label_flags = sum(1 for row in rows if row.get("table_label_review_flags"))
    return (-source_compare, -label_flags, document_id)


def _unit_sort_key(row: dict[str, str]) -> tuple[int, int, str]:
    return (
        PRIORITY_ORDER.get(row.get("review_priority") or "", 99),
        _to_int(row.get("unit_rank")) or 999999,
        row.get("table_unit_key") or "",
    )


def _page_range(row: dict[str, str]) -> str:
    start = str(row.get("source_page_start") or "").strip()
    end = str(row.get("source_page_end") or "").strip()
    if not start and not end:
        return ""
    return f"{start or '?'}-{end or start or '?'}"


def _page_ranges(rows: list[dict[str, str]]) -> list[str]:
    ranges = {_page_range(row) for row in rows if _page_range(row)}
    return sorted(ranges, key=_page_range_sort_key)


def _page_range_sort_key(value: str) -> tuple[int, int, str]:
    start, _, end = value.partition("-")
    start_no = _to_int(start) or 999999
    end_no = _to_int(end) or start_no
    return (start_no, end_no, value)


def _fingerprint(values: Sequence[str]) -> str:
    canonical = "\n".join(str(value) for value in values)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_id(value: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in value.lower()).strip("-") or "missing"


def _counts_text(counter: Counter[str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in sorted(counter.items()) if key)


def _split_joined(value: str) -> list[str]:
    return [item.strip() for item in value.split(";") if item.strip()]


def _to_int(value: Any) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _markdown(report: dict[str, Any], batches: list[dict[str, str]]) -> str:
    lines = [
        "# Parsing Goldset Table Review Batches",
        "",
        f"- Generated at: {report['generated_at']}",
        f"- Source table units CSV: `{report['source_table_units_csv']}`",
        f"- Source-compare only: {str(report['source_compare_only']).lower()}",
        f"- Selected units: {int(report['selected_unit_count']):,}",
        f"- Batch count: {int(report['batch_count']):,}",
        f"- Document count: {int(report['document_count']):,}",
        f"- Max units per batch: {int(report['max_units_per_batch']):,}",
        "",
        "## Safety Note",
        "",
        report["safety_note"],
        "",
        "## Burndown Summary",
        "",
        f"- High-attention units: {int(report['burndown_summary']['high_attention_unit_count']):,}",
        f"- Priority unit counts: `{report['burndown_summary']['priority_unit_counts']}`",
        f"- Batch priority counts: `{report['burndown_summary']['batch_priority_counts']}`",
        f"- Label flag unit counts: `{report['burndown_summary']['label_flag_unit_counts']}`",
        f"- Extension unit counts: `{report['burndown_summary']['extension_unit_counts']}`",
        f"- Top documents by unit count: `{report['burndown_summary']['top_documents_by_unit_count']}`",
        "",
        "## Batches",
        "",
        "| Rank | Batch | Document | Units | Priority | Source | Pages | Label flags |",
        "| ---: | --- | --- | ---: | --- | --- | --- | --- |",
    ]
    for batch in batches:
        lines.append(
            f"| {batch['batch_rank']} | {_md(batch['table_review_batch_id'])} | "
            f"{_md(batch['document_id'])} | {int(batch['unit_count']):,} | "
            f"{_md(batch['review_priority'])} | {_md(batch['source_path'])} | "
            f"{_md(batch['source_page_ranges'])} | {_md(batch['label_review_flag_counts'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build read-only parsing goldset table review batches from table-unit packets."
    )
    parser.add_argument("--table-units-csv", required=True)
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-csv", required=True)
    parser.add_argument("--out-md", required=True)
    parser.add_argument("--source-compare-only", action="store_true")
    parser.add_argument("--max-units-per-batch", type=int, default=20)
    parser.add_argument("--max-md-rows", type=int, default=60)
    args = parser.parse_args(argv)

    report = build_parsing_goldset_table_review_batches(
        table_units_csv=Path(args.table_units_csv),
        out_json=Path(args.out_json),
        out_csv=Path(args.out_csv),
        out_md=Path(args.out_md),
        source_compare_only=args.source_compare_only,
        max_units_per_batch=args.max_units_per_batch,
        max_md_rows=args.max_md_rows,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "json": str(args.out_json),
                "csv": str(args.out_csv),
                "markdown": str(args.out_md),
                "selected_unit_count": report["selected_unit_count"],
                "batch_count": report["batch_count"],
                "document_count": report["document_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
