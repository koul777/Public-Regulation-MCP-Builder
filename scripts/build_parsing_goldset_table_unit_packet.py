from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

try:
    from scripts.parsing_goldset_table_review_contract import (
        TABLE_REVIEW_ALLOWED_UNIT_STATUSES,
        TABLE_REVIEW_COMPLETION_GUIDANCE,
        TABLE_REVIEW_REQUIRED_COMPLETE_FIELDS,
        TABLE_REVIEW_TRUE_VALUES,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution path
    from parsing_goldset_table_review_contract import (
        TABLE_REVIEW_ALLOWED_UNIT_STATUSES,
        TABLE_REVIEW_COMPLETION_GUIDANCE,
        TABLE_REVIEW_REQUIRED_COMPLETE_FIELDS,
        TABLE_REVIEW_TRUE_VALUES,
    )


UNIT_FIELDNAMES = [
    "unit_rank",
    "review_priority",
    "table_unit_key",
    "recommended_action",
    "document_id",
    "review_order",
    "extension",
    "institution_name",
    "filename",
    "source_path",
    "chunk_artifact",
    "table_citation_label",
    "table_appendix_no",
    "article_no",
    "article_title",
    "source_page_start",
    "source_page_end",
    "candidate_count",
    "source_compare_candidate_count",
    "parser_structure_candidate_count",
    "parentage_candidate_count",
    "structured_spot_candidate_count",
    "missing_label_candidate_count",
    "table_review_flags",
    "table_label_review_flags",
    "source_parser_flags",
    "chunk_types",
    "first_chunk_id",
    "chunk_ids_sample",
    "allowed_human_unit_statuses",
    "required_complete_fields",
    "accepted_confirmation_values",
    "review_entry_guidance",
    "human_source_pages_checked",
    "human_unit_status",
    "human_manual_table_count",
    "human_matched_table_count",
    "human_row_column_match",
    "human_parentage_ok",
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

PRIORITY_ACTIONS = {
    "source_table_compare": "Compare this table unit against the original page(s) before matched-count scoring.",
    "parser_structure_review": "Check parser flags, row/column shape, and extraction risk before matched-count scoring.",
    "parentage_review": "Confirm appendix/form/table label and governing context before matched-count scoring.",
    "structured_spot_check": "Spot-check extracted rows, columns, and citation label.",
    "low_signal_table_candidate": "Review only if nearby source context makes this a true table.",
}

LABEL_REVIEW_FLAG_ORDER = {
    "missing_table_label": 0,
    "embedded_table_parentage_candidate": 1,
    "article_reference_fragment_loss_candidate": 2,
    "duplicated_appendix_label_candidate": 3,
    "long_table_label_candidate": 4,
}


def build_parsing_goldset_table_unit_packet(
    *,
    table_candidates_csv: Path,
    labels_csv: Path | None = None,
    out_json: Path,
    out_csv: Path,
    out_md: Path,
    source_compare_only: bool = False,
    max_md_rows: int = 80,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if max_md_rows <= 0:
        raise ValueError("max_md_rows must be greater than zero.")
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    rows = _load_rows(table_candidates_csv)
    source_index = _load_source_index(labels_csv) if labels_csv else {}
    units = _build_units(rows, source_index=source_index)
    selected_units = [
        unit for unit in units if not source_compare_only or unit["review_priority"] == "source_table_compare"
    ]
    selected_units.sort(key=_unit_sort_key)
    for index, unit in enumerate(selected_units, start=1):
        unit["unit_rank"] = str(index)

    priority_counts = Counter(unit["review_priority"] for unit in selected_units)
    label_flag_counts = Counter(
        flag for unit in selected_units for flag in _split_joined(unit.get("table_label_review_flags") or "")
    )
    source_compare_unit_count = sum(1 for unit in units if unit["review_priority"] == "source_table_compare")
    report = {
        "report_type": "parsing_goldset_table_unit_packet",
        "generated_at": generated_at,
        "source_table_candidates_csv": str(table_candidates_csv),
        "source_labels_csv": str(labels_csv) if labels_csv else "",
        "source_compare_only": source_compare_only,
        "row_count": len(rows),
        "unit_count": len(units),
        "selected_unit_count": len(selected_units),
        "source_compare_unit_count": source_compare_unit_count,
        "review_priority_counts": dict(priority_counts),
        "label_review_flag_counts": dict(label_flag_counts),
        "review_contract": {
            "allowed_human_unit_statuses": list(TABLE_REVIEW_ALLOWED_UNIT_STATUSES),
            "required_complete_fields": list(TABLE_REVIEW_REQUIRED_COMPLETE_FIELDS),
            "accepted_confirmation_values": list(TABLE_REVIEW_TRUE_VALUES),
            "completion_guidance": TABLE_REVIEW_COMPLETION_GUIDANCE,
        },
        "max_md_rows": max_md_rows,
        "artifacts": {
            "json": str(out_json),
            "csv": str(out_csv),
            "markdown": str(out_md),
        },
        "safety_note": (
            "This parsing goldset table unit packet is read-only. It does not fill goldset labels, "
            "approve chunks, acknowledge review flags, index vectors, or publish MCP evidence."
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
        raise ValueError("table_candidates_csv must contain at least one row.")
    required = {"document_id", "chunk_id", "review_priority"}
    missing = sorted(required - set(rows[0]))
    if missing:
        raise ValueError(f"table_candidates_csv missing required columns: {', '.join(missing)}")
    return rows


def _load_source_index(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    index: dict[str, dict[str, str]] = {}
    for row in rows:
        document_id = str(row.get("document_id") or "").strip()
        if not document_id:
            continue
        index[document_id] = {
            "extension": str(row.get("extension") or "").strip(),
            "source_path": str(row.get("source_path") or "").strip(),
        }
    return index


def _build_units(rows: list[dict[str, str]], *, source_index: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[_table_unit_key(row)].append(row)

    units: list[dict[str, str]] = []
    for key, unit_rows in grouped.items():
        priorities = _ordered_unique(row.get("review_priority") or "" for row in unit_rows)
        priority = _priority(priorities)
        chunk_ids = [row.get("chunk_id") or "" for row in unit_rows if row.get("chunk_id")]
        pages = [_to_int(row.get("source_page_start")) for row in unit_rows if _to_int(row.get("source_page_start"))]
        page_ends = [_to_int(row.get("source_page_end")) for row in unit_rows if _to_int(row.get("source_page_end"))]
        flags = _ordered_unique(
            flag for row in unit_rows for flag in _split_joined(row.get("table_review_flags") or "")
        )
        label_flags = _table_label_review_flags(unit_rows)
        parser_flags = _ordered_unique(
            flag for row in unit_rows for flag in _split_joined(row.get("source_parser_flags") or "")
        )
        chunk_types = _ordered_unique(row.get("chunk_type") or "" for row in unit_rows)
        priority_counter = Counter(row.get("review_priority") or "" for row in unit_rows)
        first = unit_rows[0]
        source_info = source_index.get(first.get("document_id") or "", {})
        missing_label_count = sum(
            1 for row in unit_rows if not row.get("table_citation_label") and not row.get("table_appendix_no")
        )
        units.append(
            {
                "unit_rank": "",
                "review_priority": priority,
                "table_unit_key": key,
                "recommended_action": PRIORITY_ACTIONS.get(priority, ""),
                "document_id": first.get("document_id") or "",
                "review_order": first.get("review_order") or "",
                "extension": source_info.get("extension") or first.get("extension") or "",
                "institution_name": first.get("institution_name") or "",
                "filename": first.get("filename") or "",
                "source_path": source_info.get("source_path") or first.get("source_path") or "",
                "chunk_artifact": first.get("chunk_artifact") or "",
                "table_citation_label": first.get("table_citation_label") or "",
                "table_appendix_no": first.get("table_appendix_no") or "",
                "article_no": first.get("article_no") or "",
                "article_title": first.get("article_title") or "",
                "source_page_start": str(min(pages)) if pages else "",
                "source_page_end": str(max(page_ends or pages)) if (page_ends or pages) else "",
                "candidate_count": str(len(unit_rows)),
                "source_compare_candidate_count": str(priority_counter.get("source_table_compare", 0)),
                "parser_structure_candidate_count": str(priority_counter.get("parser_structure_review", 0)),
                "parentage_candidate_count": str(priority_counter.get("parentage_review", 0)),
                "structured_spot_candidate_count": str(priority_counter.get("structured_spot_check", 0)),
                "missing_label_candidate_count": str(missing_label_count),
                "table_review_flags": "; ".join(flags),
                "table_label_review_flags": "; ".join(label_flags),
                "source_parser_flags": "; ".join(parser_flags),
                "chunk_types": "; ".join(chunk_types),
                "first_chunk_id": chunk_ids[0] if chunk_ids else "",
                "chunk_ids_sample": "; ".join(chunk_ids[:8]),
                "allowed_human_unit_statuses": "; ".join(TABLE_REVIEW_ALLOWED_UNIT_STATUSES),
                "required_complete_fields": "; ".join(TABLE_REVIEW_REQUIRED_COMPLETE_FIELDS),
                "accepted_confirmation_values": "; ".join(TABLE_REVIEW_TRUE_VALUES),
                "review_entry_guidance": TABLE_REVIEW_COMPLETION_GUIDANCE,
                "human_source_pages_checked": "",
                "human_unit_status": "",
                "human_manual_table_count": "",
                "human_matched_table_count": "",
                "human_row_column_match": "",
                "human_parentage_ok": "",
                "human_reviewer": "",
                "human_reviewed_at": "",
                "human_notes": "",
            }
        )
    return units


def _table_unit_key(row: dict[str, str]) -> str:
    document_id = row.get("document_id") or "(missing-document)"
    label = row.get("table_citation_label") or row.get("table_appendix_no")
    if not label:
        article = " ".join(value for value in [row.get("article_no") or "", row.get("article_title") or ""] if value)
        label = article or "(missing-table-label)"
    start = row.get("source_page_start") or "unknown"
    end = row.get("source_page_end") or start
    return f"{document_id} | {label} | p.{start}-{end}"


def _priority(priorities: list[str]) -> str:
    known = [priority for priority in priorities if priority in PRIORITY_ORDER]
    if not known:
        return "low_signal_table_candidate"
    return min(known, key=lambda priority: PRIORITY_ORDER[priority])


def _table_label_review_flags(unit_rows: list[dict[str, str]]) -> list[str]:
    flags: set[str] = set()
    label_values = _ordered_unique(
        str(row.get(field) or "").strip()
        for row in unit_rows
        for field in ("table_citation_label", "table_appendix_no", "table_title")
        if str(row.get(field) or "").strip()
    )
    missing_table_label = not any((row.get("table_citation_label") or row.get("table_appendix_no")) for row in unit_rows)
    if missing_table_label:
        flags.add("missing_table_label")
        if any((row.get("chunk_type") or "").strip() in {"paragraph", "item", "subitem"} for row in unit_rows):
            flags.add("embedded_table_parentage_candidate")
    combined = " ".join(label_values)
    normalized = re.sub(r"\s+", "", combined)
    if normalized:
        if len(combined) > 120:
            flags.add("long_table_label_candidate")
        if any(_has_duplicated_appendix_label(re.sub(r"\s+", "", value)) for value in label_values):
            flags.add("duplicated_appendix_label_candidate")
        if _has_article_reference_fragment_loss(normalized):
            flags.add("article_reference_fragment_loss_candidate")
    return sorted(flags, key=lambda flag: LABEL_REVIEW_FLAG_ORDER.get(flag, 99))


def _has_duplicated_appendix_label(normalized: str) -> bool:
    numbered_labels = re.findall(r"별(?:표|지)\d+(?:-\d+)?", normalized)
    if any(left == right for left, right in zip(numbered_labels, numbered_labels[1:])):
        return True
    return bool(re.search(r"(별표|별지)(?:\d+(?:-\d+)?)?\1", normalized))


def _has_article_reference_fragment_loss(normalized: str) -> bool:
    if "관련" not in normalized or not re.search(r"\d", normalized):
        return False
    return bool(re.search(r"제(?:조|항)|제\d*조제항", normalized))


def _unit_sort_key(unit: dict[str, str]) -> tuple[int, int, int, int, str]:
    return (
        PRIORITY_ORDER.get(unit["review_priority"], 99),
        -_to_int(unit["source_compare_candidate_count"]),
        -_to_int(unit["candidate_count"]),
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
        "# Parsing Goldset Table Unit Packet",
        "",
        f"- Generated at: {report['generated_at']}",
        f"- Source table candidates CSV: `{report['source_table_candidates_csv']}`",
        f"- Source labels CSV: `{report['source_labels_csv']}`" if report.get("source_labels_csv") else "- Source labels CSV: none",
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
        lines.append(f"| {priority} | {count:,} |")
    if report.get("label_review_flag_counts"):
        lines.extend(
            [
                "",
                "## Label Review Flags",
                "",
                "| Flag | Units |",
                "| --- | ---: |",
            ]
        )
        for flag, count in sorted(
            report["label_review_flag_counts"].items(),
            key=lambda item: LABEL_REVIEW_FLAG_ORDER.get(item[0], 99),
        ):
            lines.append(f"| {flag} | {count:,} |")
    review_contract = report.get("review_contract") or {}
    lines.extend(
        [
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
            "## Units",
            "",
            "| Rank | Priority | Document | Key | Pages | Candidates | Source compare | Flags | Label flags | First chunk |",
            "| ---: | --- | --- | --- | --- | ---: | ---: | --- | --- | --- |",
        ]
    )
    for unit in units:
        pages = (
            f"{unit.get('source_page_start')}-{unit.get('source_page_end')}"
            if unit.get("source_page_start") or unit.get("source_page_end")
            else ""
        )
        lines.append(
            f"| {unit['unit_rank']} | {_md(unit['review_priority'])} | {_md(unit['document_id'])} | "
            f"{_md(unit['table_unit_key'])} | {_md(pages)} | {int(unit['candidate_count']):,} | "
            f"{int(unit['source_compare_candidate_count']):,} | {_md(unit['table_review_flags'])} | "
            f"{_md(unit.get('table_label_review_flags') or '')} | {_md(unit['first_chunk_id'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Group parsing goldset table candidates into source-review table units."
    )
    parser.add_argument("--table-candidates-csv", required=True)
    parser.add_argument("--labels-csv")
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-csv", required=True)
    parser.add_argument("--out-md", required=True)
    parser.add_argument("--source-compare-only", action="store_true")
    parser.add_argument("--max-md-rows", type=int, default=80)
    args = parser.parse_args(argv)

    report = build_parsing_goldset_table_unit_packet(
        table_candidates_csv=Path(args.table_candidates_csv),
        labels_csv=Path(args.labels_csv) if args.labels_csv else None,
        out_json=Path(args.out_json),
        out_csv=Path(args.out_csv),
        out_md=Path(args.out_md),
        source_compare_only=args.source_compare_only,
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
                "source_compare_unit_count": report["source_compare_unit_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
