from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


CSV_FIELDNAMES = [
    "rank",
    "risk_tier",
    "recommended_action",
    "chunk_id",
    "chunk_type",
    "regulation_title",
    "source_file",
    "source_path",
    "article_no",
    "article_title",
    "source_page_start",
    "source_page_end",
    "table_like",
    "table_review_required",
    "table_classification",
    "table_review_reason",
    "table_review_flags",
    "table_confidence",
    "table_structured_row_count",
    "table_column_count",
    "table_record_count",
    "table_citation_label",
    "table_appendix_no",
    "table_unit_key",
    "table_unit_size",
    "parser_uncertainty_flags",
    "human_source_page_checked",
    "human_table_status",
    "human_row_count_match",
    "human_column_count_match",
    "human_merged_cells_preserved",
    "human_truncated_cell_issue",
    "human_parentage_ok",
    "human_notes",
    "snippet",
]

SOURCE_COMPARE_FLAGS = {
    "row_review_required",
    "unstable_column_count",
    "possible_truncated_cell",
    "parallel_value_tail_reconstruction",
    "dense_numeric_row_reconstruction",
    "low_structured_row_count",
    "appendix_table_low_structured_row_count",
    "many_text_cells_without_numeric_signal",
    "short_cell",
    "nested_table",
    "merged_cell",
}

TIER_ORDER = {
    "source_table_compare": 0,
    "structured_table_spot_check": 1,
    "table_parentage_spot_check": 2,
    "low_signal_table_candidate": 3,
}

TIER_ACTIONS = {
    "source_table_compare": "Compare against the source page/table before approval.",
    "structured_table_spot_check": "Spot-check extracted rows and citation label.",
    "table_parentage_spot_check": "Confirm appendix/form/table parentage and governing article.",
    "low_signal_table_candidate": "Check only if nearby review context raises concern.",
}


def build_table_preprocessing_risk_report(
    *,
    chunks_json: Path,
    out_json: Path,
    out_csv: Path,
    out_md: Path,
    max_sample_rows: int = 50,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if max_sample_rows <= 0:
        raise ValueError("max_sample_rows must be greater than zero.")

    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    chunks = _load_chunks(chunks_json)
    source_file_index = _source_file_index(chunks_json, chunks)
    rows = [
        _candidate_row(chunk, source_file_index=source_file_index)
        for chunk in chunks
        if _is_table_candidate(chunk)
    ]
    rows.sort(key=_row_sort_key)
    unit_counts = Counter(row["table_unit_key"] for row in rows)
    unit_source_compare_counts = Counter(
        row["table_unit_key"] for row in rows if row["risk_tier"] == "source_table_compare"
    )
    for index, row in enumerate(rows, start=1):
        row["rank"] = str(index)
        row["table_unit_size"] = str(unit_counts[row["table_unit_key"]])

    flag_counts = Counter()
    flag_combo_counts = Counter()
    risk_tier_counts = Counter()
    chunk_type_counts = Counter()
    classification_counts = Counter()
    structured_row_buckets = Counter()
    column_count_buckets = Counter()
    regulation_counts: dict[str, Counter] = defaultdict(Counter)

    for row in rows:
        flags = _split_joined(row["table_review_flags"])
        flag_counts.update(flags)
        if flags:
            flag_combo_counts.update(["; ".join(flags)])
        risk_tier_counts[row["risk_tier"]] += 1
        chunk_type_counts[row["chunk_type"] or ""] += 1
        classification_counts[row["table_classification"] or ""] += 1
        structured_row_buckets[_count_bucket(_to_int(row["table_structured_row_count"]))] += 1
        column_count_buckets[_count_bucket(_to_int(row["table_column_count"]))] += 1
        regulation_key = row["regulation_title"] or "(missing)"
        regulation_counts[regulation_key]["candidate_count"] += 1
        if row["table_review_required"] == "true":
            regulation_counts[regulation_key]["review_required_count"] += 1

    source_compare_count = risk_tier_counts["source_table_compare"]
    label_rows = [row for row in rows if row["table_citation_label"] or row["table_appendix_no"]]
    hyphenated_label_count = sum(
        1
        for row in label_rows
        if re.search(r"\d+\s*-\s*\d+", row["table_citation_label"] or row["table_appendix_no"])
    )
    missing_label_rows = [row for row in rows if not row["table_citation_label"] and not row["table_appendix_no"]]
    missing_label_chunk_type_counts = Counter(row["chunk_type"] or "" for row in missing_label_rows)
    missing_label_risk_tier_counts = Counter(row["risk_tier"] for row in missing_label_rows)
    missing_label_flag_counts = Counter(
        flag for row in missing_label_rows for flag in _split_joined(row["table_review_flags"])
    )

    sample_rows = rows[:max_sample_rows]
    report = {
        "report_type": "table_preprocessing_risk_report",
        "generated_at": generated_at,
        "source_chunks": str(chunks_json),
        "total_chunks": len(chunks),
        "candidate_count": len(rows),
        "table_like_count": sum(1 for row in rows if row["table_like"] == "true"),
        "table_review_required_count": sum(1 for row in rows if row["table_review_required"] == "true"),
        "source_table_compare_count": source_compare_count,
        "table_unit_count": len(unit_counts),
        "source_compare_table_unit_count": len(unit_source_compare_counts),
        "source_file_count": len({row["source_file"] for row in rows if row["source_file"]}),
        "resolved_source_path_count": sum(1 for row in rows if row["source_path"]),
        "candidate_rate": round((len(rows) / len(chunks) * 100.0), 3) if chunks else 0.0,
        "review_required_rate": round((source_compare_count / len(rows) * 100.0), 3) if rows else 0.0,
        "risk_tier_counts": dict(risk_tier_counts),
        "chunk_type_counts": dict(chunk_type_counts),
        "classification_counts": dict(classification_counts),
        "table_review_flag_counts": dict(flag_counts),
        "structured_row_buckets": dict(structured_row_buckets),
        "column_count_buckets": dict(column_count_buckets),
        "top_flag_combinations": _top_counter(flag_combo_counts, 10),
        "top_table_units": _top_table_units(unit_counts, unit_source_compare_counts, 10),
        "top_regulations": _top_regulations(regulation_counts, 10),
        "label_summary": {
            "with_table_label_count": len(label_rows),
            "missing_table_label_count": len(rows) - len(label_rows),
            "hyphenated_label_count": hyphenated_label_count,
            "missing_label_chunk_type_counts": dict(missing_label_chunk_type_counts),
            "missing_label_risk_tier_counts": dict(missing_label_risk_tier_counts),
            "missing_label_source_compare_count": missing_label_risk_tier_counts["source_table_compare"],
            "missing_label_top_flags": _top_counter(missing_label_flag_counts, 10),
            "missing_label_review_guidance": (
                "Treat missing table labels as citation/parentage review gaps. Do not infer appendix/form labels "
                "unless the source hierarchy or table text contains an explicit appendix/form/table label."
            ),
        },
        "sample_count": len(sample_rows),
        "artifacts": {
            "json": str(out_json),
            "csv": str(out_csv),
            "markdown": str(out_md),
        },
        "safety_note": (
            "This report is read-only. It does not approve chunks, acknowledge review flags, "
            "index vectors, or publish MCP evidence."
        ),
    }

    _write_csv(out_csv, rows)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps({**report, "sample_rows": sample_rows}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(_markdown(report, sample_rows), encoding="utf-8")
    return report


def _load_chunks(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list):
        raise ValueError("chunks_json must contain a JSON array.")
    return [chunk for chunk in payload if isinstance(chunk, dict)]


def _metadata(chunk: dict[str, Any]) -> dict[str, Any]:
    metadata = chunk.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _value(chunk: dict[str, Any], key: str, default: Any = "") -> Any:
    metadata = _metadata(chunk)
    if key in metadata:
        return metadata.get(key)
    return chunk.get(key, default)


def _list_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _table_flags(chunk: dict[str, Any]) -> list[str]:
    flags = _list_value(_value(chunk, "table_review_flags", []))
    parser_flags = _list_value(_metadata(chunk).get("source_hwpx_parser_review_flags"))
    table_like_parser_flags = [flag for flag in parser_flags if "table" in flag or flag in {"merged_cell"}]
    return sorted(dict.fromkeys(flags + table_like_parser_flags))


def _is_table_candidate(chunk: dict[str, Any]) -> bool:
    flags = _table_flags(chunk)
    chunk_type = str(_value(chunk, "chunk_type", chunk.get("chunk_type") or ""))
    return (
        bool(_value(chunk, "table_like", False))
        or bool(_value(chunk, "table_review_required", False))
        or bool(flags)
        or chunk_type == "table"
        or bool(_value(chunk, "table_citation_label", ""))
        or bool(_value(chunk, "table_appendix_no", ""))
    )


def _candidate_row(chunk: dict[str, Any], *, source_file_index: dict[str, str]) -> dict[str, str]:
    metadata = _metadata(chunk)
    flags = _table_flags(chunk)
    tier = _risk_tier(chunk, flags)
    source_file = _text(_value(chunk, "source_file", ""))
    source_path = _text(_value(chunk, "source_path", "")) or source_file_index.get(source_file, "")
    return {
        "rank": "",
        "risk_tier": tier,
        "recommended_action": TIER_ACTIONS[tier],
        "chunk_id": _text(chunk.get("chunk_id")),
        "chunk_type": _text(_value(chunk, "chunk_type", chunk.get("chunk_type") or "")),
        "regulation_title": _text(_value(chunk, "regulation_title", "")),
        "source_file": source_file,
        "source_path": source_path,
        "article_no": _text(_value(chunk, "article_no", "")),
        "article_title": _text(_value(chunk, "article_title", "")),
        "source_page_start": _text(_value(chunk, "source_page_start", "")),
        "source_page_end": _text(_value(chunk, "source_page_end", "")),
        "table_like": _bool_text(_value(chunk, "table_like", False)),
        "table_review_required": _bool_text(_value(chunk, "table_review_required", False)),
        "table_classification": _text(_value(chunk, "table_classification", "")),
        "table_review_reason": _text(_value(chunk, "table_review_reason", "")),
        "table_review_flags": "; ".join(flags),
        "table_confidence": _text(_value(chunk, "table_confidence", "")),
        "table_structured_row_count": _text(_value(chunk, "table_structured_row_count", 0)),
        "table_column_count": _text(_value(chunk, "table_column_count", 0)),
        "table_record_count": _text(_value(chunk, "table_record_count", 0)),
        "table_citation_label": _text(_value(chunk, "table_citation_label", "")),
        "table_appendix_no": _text(_value(chunk, "table_appendix_no", "")),
        "table_unit_key": _table_unit_key(chunk),
        "table_unit_size": "",
        "parser_uncertainty_flags": "; ".join(_list_value(metadata.get("parser_uncertainty_flags"))),
        "human_source_page_checked": "",
        "human_table_status": "",
        "human_row_count_match": "",
        "human_column_count_match": "",
        "human_merged_cells_preserved": "",
        "human_truncated_cell_issue": "",
        "human_parentage_ok": "",
        "human_notes": "",
        "snippet": _snippet(str(chunk.get("text") or chunk.get("retrieval_text") or "")),
    }


def _risk_tier(chunk: dict[str, Any], flags: list[str]) -> str:
    flag_set = set(flags)
    if bool(_value(chunk, "table_review_required", False)) or flag_set.intersection(SOURCE_COMPARE_FLAGS):
        return "source_table_compare"
    if bool(_value(chunk, "table_like", False)):
        return "structured_table_spot_check"
    if _value(chunk, "table_citation_label", "") or _value(chunk, "table_appendix_no", ""):
        return "table_parentage_spot_check"
    return "low_signal_table_candidate"


def _source_file_index(chunks_json: Path, chunks: list[dict[str, Any]]) -> dict[str, str]:
    source_files = sorted(
        {
            _text(_value(chunk, "source_file", ""))
            for chunk in chunks
            if _text(_value(chunk, "source_file", ""))
        }
    )
    return {
        source_file: resolved
        for source_file in source_files
        if (resolved := _resolve_source_file(chunks_json, source_file))
    }


def _resolve_source_file(chunks_json: Path, source_file: str) -> str:
    path = Path(source_file)
    if path.is_absolute():
        return str(path) if path.exists() else ""
    candidates = [
        chunks_json.parent / path,
        chunks_json.parent / "uploads" / path,
        chunks_json.parent.parent / path,
        chunks_json.parent.parent / "uploads" / path,
        chunks_json.parent.parent.parent / path,
        chunks_json.parent.parent.parent / "uploads" / path,
        Path.cwd() / path,
        Path.cwd() / "data" / "uploads" / path,
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return ""


def _row_sort_key(row: dict[str, str]) -> tuple[int, int, int, str]:
    return (
        TIER_ORDER.get(row["risk_tier"], 99),
        -len(_split_joined(row["table_review_flags"])),
        -_to_int(row["table_structured_row_count"]),
        row["chunk_id"],
    )


def _table_unit_key(chunk: dict[str, Any]) -> str:
    regulation_title = _text(_value(chunk, "regulation_title", "")) or "(missing-regulation)"
    label = _text(_value(chunk, "table_citation_label", "")) or _text(_value(chunk, "table_appendix_no", "")) or "(missing-label)"
    page = _text(_value(chunk, "source_page_start", "")) or "(missing-page)"
    return f"{regulation_title} | {label} | p.{page}"


def _split_joined(value: str) -> list[str]:
    return [item.strip() for item in value.split(";") if item.strip()]


def _bool_text(value: Any) -> str:
    return "true" if bool(value) else "false"


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _to_int(value: Any) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _count_bucket(value: int) -> str:
    if value <= 0:
        return "0"
    if value == 1:
        return "1"
    if value <= 4:
        return "2-4"
    if value <= 9:
        return "5-9"
    return "10+"


def _top_counter(counter: Counter, limit: int) -> list[dict[str, Any]]:
    return [{"value": key, "count": count} for key, count in counter.most_common(limit)]


def _top_regulations(regulation_counts: dict[str, Counter], limit: int) -> list[dict[str, Any]]:
    rows = [
        {
            "regulation_title": title,
            "candidate_count": counts["candidate_count"],
            "review_required_count": counts["review_required_count"],
        }
        for title, counts in regulation_counts.items()
    ]
    rows.sort(key=lambda row: (-int(row["review_required_count"]), -int(row["candidate_count"]), row["regulation_title"]))
    return rows[:limit]


def _top_table_units(
    unit_counts: Counter[str],
    unit_source_compare_counts: Counter[str],
    limit: int,
) -> list[dict[str, Any]]:
    rows = [
        {
            "table_unit_key": key,
            "candidate_count": count,
            "source_compare_count": unit_source_compare_counts.get(key, 0),
        }
        for key, count in unit_counts.items()
    ]
    rows.sort(key=lambda row: (-int(row["source_compare_count"]), -int(row["candidate_count"]), row["table_unit_key"]))
    return rows[:limit]


def _snippet(value: str, limit: int = 300) -> str:
    return " ".join(value.split())[:limit]


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _markdown(report: dict[str, Any], sample_rows: list[dict[str, str]]) -> str:
    lines = [
        "# Table Preprocessing Risk Report",
        "",
        f"- Generated at: {report['generated_at']}",
        f"- Source chunks: `{report['source_chunks']}`",
        f"- Total chunks: {report['total_chunks']:,}",
        f"- Table candidates: {report['candidate_count']:,} ({report['candidate_rate']}%)",
        f"- Table review-required chunks: {report['table_review_required_count']:,}",
        f"- Source table compare tier: {report['source_table_compare_count']:,}",
        f"- Table units: {report['table_unit_count']:,}",
        f"- Source-compare table units: {report['source_compare_table_unit_count']:,}",
        f"- Source files: {report['source_file_count']:,}",
        f"- Resolved source paths: {report['resolved_source_path_count']:,}",
        "",
        "## Risk Tiers",
        "",
        "| Tier | Count |",
        "| --- | ---: |",
    ]
    for tier, count in sorted(report["risk_tier_counts"].items(), key=lambda item: TIER_ORDER.get(item[0], 99)):
        lines.append(f"| {tier} | {count:,} |")
    lines.extend(["", "## Top Table Review Flags", "", "| Flag | Count |", "| --- | ---: |"])
    for flag, count in Counter(report["table_review_flag_counts"]).most_common(15):
        lines.append(f"| {_md(flag)} | {count:,} |")
    lines.extend(["", "## Top Table Units", "", "| Table Unit | Candidates | Source Compare |", "| --- | ---: | ---: |"])
    for row in report["top_table_units"]:
        lines.append(
            f"| {_md(row['table_unit_key'])} | {int(row['candidate_count']):,} | "
            f"{int(row['source_compare_count']):,} |"
        )
    lines.extend(["", "## Top Regulations", "", "| Regulation | Candidates | Review Required |", "| --- | ---: | ---: |"])
    for row in report["top_regulations"]:
        lines.append(
            f"| {_md(row['regulation_title'])} | {int(row['candidate_count']):,} | "
            f"{int(row['review_required_count']):,} |"
        )
    label_summary = report["label_summary"]
    lines.extend(
        [
            "",
            "## Table Label Gaps",
            "",
            f"- With table labels: {int(label_summary['with_table_label_count']):,}",
            f"- Missing table labels: {int(label_summary['missing_table_label_count']):,}",
            f"- Hyphenated labels: {int(label_summary['hyphenated_label_count']):,}",
            f"- Missing-label source-compare rows: {int(label_summary['missing_label_source_compare_count']):,}",
            "",
            "Missing-label chunk types:",
            "",
            "| Chunk Type | Count |",
            "| --- | ---: |",
        ]
    )
    for chunk_type, count in sorted(
        label_summary["missing_label_chunk_type_counts"].items(),
        key=lambda item: (-int(item[1]), item[0]),
    ):
        lines.append(f"| {_md(chunk_type or '(missing)')} | {int(count):,} |")
    lines.extend(["", "Missing-label risk tiers:", "", "| Tier | Count |", "| --- | ---: |"])
    for tier, count in sorted(
        label_summary["missing_label_risk_tier_counts"].items(),
        key=lambda item: TIER_ORDER.get(item[0], 99),
    ):
        lines.append(f"| {_md(tier)} | {int(count):,} |")
    lines.extend(
        [
            "",
            f"Guidance: {label_summary['missing_label_review_guidance']}",
        ]
    )
    lines.extend(
        [
            "",
            "## Reviewer Columns",
            "",
            "The CSV includes blank human-review fields for source-page comparison, table status, row/column match, merged-cell preservation, truncated-cell issue, parentage, and notes.",
            "",
            "## Sample Rows",
            "",
            "| Rank | Tier | Chunk | Source | Type | Article | Flags | Snippet |",
            "| ---: | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in sample_rows:
        article = " ".join(part for part in [row["regulation_title"], row["article_no"], row["article_title"]] if part)
        lines.append(
            f"| {row['rank']} | {_md(row['risk_tier'])} | {_md(row['chunk_id'])} | "
            f"{_md(row['source_file'])} | {_md(row['chunk_type'])} | {_md(article)} | {_md(row['table_review_flags'])} | "
            f"{_md(row['snippet'])} |"
        )
    lines.append("")
    lines.append(f"Safety: {report['safety_note']}")
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a read-only table preprocessing risk report from chunk JSON.")
    parser.add_argument("--chunks-json", type=Path, required=True)
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-csv", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    parser.add_argument("--max-sample-rows", type=int, default=50)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = build_table_preprocessing_risk_report(
            chunks_json=args.chunks_json,
            out_json=args.out_json,
            out_csv=args.out_csv,
            out_md=args.out_md,
            max_sample_rows=args.max_sample_rows,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
