from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


UNIT_FIELDNAMES = [
    "unit_rank",
    "review_priority",
    "table_unit_key",
    "recommended_action",
    "regulation_title",
    "source_file",
    "source_path",
    "table_citation_label",
    "table_appendix_no",
    "source_page_start",
    "source_page_end",
    "chunk_count",
    "source_compare_chunk_count",
    "risk_tiers",
    "table_review_flags",
    "chunk_types",
    "first_chunk_id",
    "chunk_ids_sample",
    "human_source_pages_checked",
    "human_unit_status",
    "human_row_count_match",
    "human_column_count_match",
    "human_merged_cells_preserved",
    "human_truncated_cell_issue",
    "human_parentage_ok",
    "human_notes",
]

PRIORITY_ORDER = {
    "source_table_compare": 0,
    "structured_table_spot_check": 1,
    "table_parentage_spot_check": 2,
    "low_signal_table_candidate": 3,
}

RECOMMENDED_ACTIONS = {
    "source_table_compare": "Compare this source table unit against the original page(s) before approval.",
    "structured_table_spot_check": "Spot-check extracted rows and citation label before broad approval.",
    "table_parentage_spot_check": "Confirm table parentage and governing context before approval.",
    "low_signal_table_candidate": "Review only if nearby context raises concern.",
}


def build_table_unit_review_packet(
    *,
    table_risk_csv: Path,
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
    rows = _load_rows(table_risk_csv)
    units = _build_units(rows)
    if source_compare_only:
        selected_units = [unit for unit in units if unit["review_priority"] == "source_table_compare"]
    else:
        selected_units = list(units)

    selected_units.sort(key=_unit_sort_key)
    for index, unit in enumerate(selected_units, start=1):
        unit["unit_rank"] = str(index)

    review_priority_counts = Counter(unit["review_priority"] for unit in selected_units)
    source_compare_unit_count = sum(1 for unit in units if unit["review_priority"] == "source_table_compare")
    report = {
        "report_type": "table_unit_review_packet",
        "generated_at": generated_at,
        "source_table_risk_csv": str(table_risk_csv),
        "source_compare_only": source_compare_only,
        "row_count": len(rows),
        "unit_count": len(units),
        "selected_unit_count": len(selected_units),
        "source_compare_unit_count": source_compare_unit_count,
        "review_priority_counts": dict(review_priority_counts),
        "max_md_rows": max_md_rows,
        "artifacts": {
            "json": str(out_json),
            "csv": str(out_csv),
            "markdown": str(out_md),
        },
        "safety_note": (
            "This table unit packet is read-only. It does not approve chunks, acknowledge review flags, "
            "index vectors, or publish MCP evidence."
        ),
    }

    _write_csv(out_csv, selected_units)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps({**report, "units": selected_units}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(_markdown(report, selected_units[:max_md_rows]), encoding="utf-8")
    return report


def _load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    if not rows:
        raise ValueError("table_risk_csv must contain at least one row.")
    if "table_unit_key" not in rows[0] or "chunk_id" not in rows[0]:
        raise ValueError("table_risk_csv must include table_unit_key and chunk_id columns.")
    return rows


def _build_units(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        key = row.get("table_unit_key") or "(missing-unit)"
        grouped[key].append(row)

    units: list[dict[str, str]] = []
    for key, unit_rows in grouped.items():
        risk_tiers = _ordered_unique(row.get("risk_tier") or "" for row in unit_rows)
        priority = _priority(risk_tiers)
        flags = _ordered_unique(
            flag for row in unit_rows for flag in _split_joined(row.get("table_review_flags") or "")
        )
        chunk_types = _ordered_unique(row.get("chunk_type") or "" for row in unit_rows)
        pages = [_to_int(row.get("source_page_start")) for row in unit_rows if _to_int(row.get("source_page_start"))]
        chunk_ids = [row.get("chunk_id") or "" for row in unit_rows if row.get("chunk_id")]
        source_compare_count = sum(1 for row in unit_rows if row.get("risk_tier") == "source_table_compare")
        first = unit_rows[0]
        units.append(
            {
                "unit_rank": "",
                "review_priority": priority,
                "table_unit_key": key,
                "recommended_action": RECOMMENDED_ACTIONS.get(priority, ""),
                "regulation_title": first.get("regulation_title") or "",
                "source_file": first.get("source_file") or "",
                "source_path": first.get("source_path") or "",
                "table_citation_label": first.get("table_citation_label") or "",
                "table_appendix_no": first.get("table_appendix_no") or "",
                "source_page_start": str(min(pages)) if pages else "",
                "source_page_end": str(max(pages)) if pages else "",
                "chunk_count": str(len(unit_rows)),
                "source_compare_chunk_count": str(source_compare_count),
                "risk_tiers": "; ".join(risk_tiers),
                "table_review_flags": "; ".join(flags),
                "chunk_types": "; ".join(chunk_types),
                "first_chunk_id": chunk_ids[0] if chunk_ids else "",
                "chunk_ids_sample": "; ".join(chunk_ids[:8]),
                "human_source_pages_checked": "",
                "human_unit_status": "",
                "human_row_count_match": "",
                "human_column_count_match": "",
                "human_merged_cells_preserved": "",
                "human_truncated_cell_issue": "",
                "human_parentage_ok": "",
                "human_notes": "",
            }
        )
    return units


def _priority(risk_tiers: list[str]) -> str:
    known = [tier for tier in risk_tiers if tier in PRIORITY_ORDER]
    if not known:
        return "low_signal_table_candidate"
    return min(known, key=lambda tier: PRIORITY_ORDER[tier])


def _unit_sort_key(unit: dict[str, str]) -> tuple[int, int, int, int, str]:
    return (
        PRIORITY_ORDER.get(unit["review_priority"], 99),
        -_to_int(unit["source_compare_chunk_count"]),
        -_to_int(unit["chunk_count"]),
        _to_int(unit["source_page_start"]) or 999999,
        unit["table_unit_key"],
    )


def _ordered_unique(values: Any) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen[text] = None
    return list(seen)


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
        writer = csv.DictWriter(handle, fieldnames=UNIT_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _markdown(report: dict[str, Any], units: list[dict[str, str]]) -> str:
    lines = [
        "# Table Unit Review Packet",
        "",
        f"- Generated at: {report['generated_at']}",
        f"- Source table risk CSV: `{report['source_table_risk_csv']}`",
        f"- Source-compare only: {str(report['source_compare_only']).lower()}",
        f"- Source rows: {int(report['row_count']):,}",
        f"- Total table units: {int(report['unit_count']):,}",
        f"- Selected table units: {int(report['selected_unit_count']):,}",
        f"- Source-compare table units: {int(report['source_compare_unit_count']):,}",
        "",
        "## Review Priorities",
        "",
        "| Priority | Units |",
        "| --- | ---: |",
    ]
    for priority, count in sorted(
        report["review_priority_counts"].items(),
        key=lambda item: PRIORITY_ORDER.get(item[0], 99),
    ):
        lines.append(f"| {_md(priority)} | {int(count):,} |")
    lines.extend(
        [
            "",
            "## Selected Units",
            "",
            "| Rank | Priority | Unit | Source | Chunks | Source Compare | Pages | Flags |",
            "| ---: | --- | --- | --- | ---: | ---: | --- | --- |",
        ]
    )
    for unit in units:
        page_range = unit["source_page_start"]
        if unit["source_page_end"] and unit["source_page_end"] != unit["source_page_start"]:
            page_range = f"{unit['source_page_start']}-{unit['source_page_end']}"
        lines.append(
            f"| {unit['unit_rank']} | {_md(unit['review_priority'])} | {_md(unit['table_unit_key'])} | "
            f"{_md(unit['source_file'])} | "
            f"{int(unit['chunk_count']):,} | {int(unit['source_compare_chunk_count']):,} | "
            f"{_md(page_range)} | {_md(unit['table_review_flags'])} |"
        )
    if report["selected_unit_count"] > len(units):
        lines.append(f"| ... | ... | {int(report['selected_unit_count']) - len(units):,} more units omitted from Markdown preview |  |  |  |  |  |")
    lines.append("")
    lines.append("## Reviewer Columns")
    lines.append("")
    lines.append(
        "The CSV includes blank human-review fields for source-page check, unit status, row/column match, "
        "merged cells, truncation, parentage, and notes."
    )
    lines.append("")
    lines.append(f"Safety: {report['safety_note']}")
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Group table-risk rows into source-table review units.")
    parser.add_argument("--table-risk-csv", type=Path, required=True)
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-csv", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    parser.add_argument("--source-compare-only", action="store_true")
    parser.add_argument("--max-md-rows", type=int, default=50)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = build_table_unit_review_packet(
            table_risk_csv=args.table_risk_csv,
            out_json=args.out_json,
            out_csv=args.out_csv,
            out_md=args.out_md,
            source_compare_only=args.source_compare_only,
            max_md_rows=args.max_md_rows,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
