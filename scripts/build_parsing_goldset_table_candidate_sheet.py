from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.analyze_regulation_corpus import (  # noqa: E402
    chunk_get,
    chunk_meta,
    load_goldset_label_rows,
    normalized_table_review_flags,
    report_path,
    resolve_workspace_path,
    review_body_text,
    safe_int,
    snippet,
)


SHEET_FIELDNAMES = [
    "rank",
    "document_rank",
    "review_order",
    "document_id",
    "extension",
    "institution_name",
    "filename",
    "chunk_artifact",
    "review_priority",
    "recommended_action",
    "chunk_id",
    "chunk_type",
    "source_page_start",
    "source_page_end",
    "article_no",
    "article_title",
    "table_citation_label",
    "table_appendix_no",
    "table_title",
    "table_review_required",
    "table_classification",
    "table_review_flags",
    "source_parser_flags",
    "table_structured_row_count",
    "table_column_count",
    "table_record_count",
    "human_source_checked",
    "human_table_status",
    "human_match_decision",
    "human_notes",
    "snippet",
]

PRIORITY_ORDER = {
    "source_table_compare": 0,
    "parser_structure_review": 1,
    "parentage_review": 2,
    "structured_spot_check": 3,
    "low_signal_table_candidate": 4,
}

PRIORITY_ACTIONS = {
    "source_table_compare": "Compare this table candidate against the original page before matched-count scoring.",
    "parser_structure_review": "Check parser flags, row/column shape, and possible extraction failure before scoring.",
    "parentage_review": "Confirm appendix/form/table label and governing context before scoring.",
    "structured_spot_check": "Spot-check extracted rows, columns, and citation label.",
    "low_signal_table_candidate": "Review only if nearby source context makes this a true table.",
}


def build_parsing_goldset_table_candidate_sheet(
    *,
    workspace: Path,
    labels_csv: Path,
    out_json: Path,
    out_csv: Path,
    out_md: Path,
    max_md_rows: int = 80,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if max_md_rows <= 0:
        raise ValueError("max_md_rows must be greater than zero.")
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    label_rows = load_goldset_label_rows(labels_csv)
    rows: list[dict[str, str]] = []
    document_errors: list[dict[str, str]] = []
    for document_rank, label_row in enumerate(label_rows, start=1):
        chunk_artifact = str(label_row.get("chunk_artifact") or "").strip()
        chunk_path = resolve_workspace_path(workspace, chunk_artifact)
        if not chunk_path or not chunk_path.exists():
            document_errors.append(
                {
                    "document_id": str(label_row.get("document_id") or ""),
                    "chunk_artifact": chunk_artifact,
                    "error": "chunk-artifact-not-found",
                }
            )
            continue
        chunks = _load_chunks(chunk_path)
        for chunk in chunks:
            if not _is_table_candidate(chunk):
                continue
            rows.append(_candidate_row(label_row, chunk, chunk_artifact=chunk_artifact, document_rank=document_rank))

    rows.sort(key=_row_sort_key)
    for index, row in enumerate(rows, start=1):
        row["rank"] = str(index)

    priority_counts = Counter(row["review_priority"] for row in rows)
    flag_counts = Counter(flag for row in rows for flag in _split_joined(row["table_review_flags"]))
    parser_flag_counts = Counter(flag for row in rows for flag in _split_joined(row["source_parser_flags"]))
    by_document: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        doc = row["document_id"]
        by_document[doc]["candidate_count"] += 1
        by_document[doc][row["review_priority"]] += 1
        if row["table_review_required"] == "true":
            by_document[doc]["review_required_count"] += 1

    document_summaries = [
        {
            "document_id": document_id,
            **dict(counts),
        }
        for document_id, counts in sorted(
            by_document.items(),
            key=lambda item: (-int(item[1].get("candidate_count", 0)), item[0]),
        )
    ]
    report = {
        "report_type": "parsing_goldset_table_candidate_sheet",
        "generated_at": generated_at,
        "source_labels_csv": str(labels_csv),
        "document_count": len(label_rows),
        "candidate_count": len(rows),
        "document_error_count": len(document_errors),
        "review_required_count": sum(1 for row in rows if row["table_review_required"] == "true"),
        "missing_label_candidate_count": sum(
            1 for row in rows if not row["table_citation_label"] and not row["table_appendix_no"]
        ),
        "priority_counts": dict(priority_counts),
        "table_review_flag_counts": dict(flag_counts),
        "source_parser_flag_counts": dict(parser_flag_counts),
        "document_summaries": document_summaries,
        "document_errors": document_errors,
        "max_md_rows": max_md_rows,
        "artifacts": {
            "json": str(out_json),
            "csv": str(out_csv),
            "markdown": str(out_md),
        },
        "safety_note": (
            "This sheet is read-only. It does not fill goldset labels, approve chunks, "
            "acknowledge review flags, index vectors, or publish MCP evidence."
        ),
    }

    _write_csv(out_csv, rows)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps({**report, "rows": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(_markdown(report, rows[:max_md_rows]), encoding="utf-8")
    return report


def _load_chunks(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list):
        raise ValueError(f"Chunk artifact must contain a JSON array: {path}")
    return [chunk for chunk in payload if isinstance(chunk, dict)]


def _is_table_candidate(chunk: dict[str, Any]) -> bool:
    chunk_type = str(chunk_get(chunk, "chunk_type", chunk.get("chunk_type") or "") or "")
    parser_flags = _source_parser_flags(chunk)
    return (
        bool(chunk_get(chunk, "table_like", False))
        or bool(chunk_get(chunk, "table_review_required", False))
        or bool(normalized_table_review_flags(chunk))
        or chunk_type == "table"
        or bool(chunk_get(chunk, "table_citation_label", ""))
        or bool(chunk_get(chunk, "table_appendix_no", ""))
        or any("table" in flag or flag in {"merged_cell"} for flag in parser_flags)
    )


def _candidate_row(
    label_row: dict[str, Any],
    chunk: dict[str, Any],
    *,
    chunk_artifact: str,
    document_rank: int,
) -> dict[str, str]:
    table_flags = normalized_table_review_flags(chunk)
    parser_flags = _source_parser_flags(chunk)
    priority = _priority(chunk, table_flags, parser_flags)
    return {
        "rank": "",
        "document_rank": str(document_rank),
        "review_order": str(label_row.get("review_order") or ""),
        "document_id": str(label_row.get("document_id") or ""),
        "extension": str(label_row.get("extension") or ""),
        "institution_name": str(label_row.get("institution_name") or ""),
        "filename": str(label_row.get("filename") or ""),
        "chunk_artifact": chunk_artifact,
        "review_priority": priority,
        "recommended_action": PRIORITY_ACTIONS[priority],
        "chunk_id": str(chunk.get("chunk_id") or ""),
        "chunk_type": str(chunk_get(chunk, "chunk_type", chunk.get("chunk_type") or "") or ""),
        "source_page_start": _text(chunk_get(chunk, "source_page_start", "")),
        "source_page_end": _text(chunk_get(chunk, "source_page_end", "")),
        "article_no": _text(chunk_get(chunk, "article_no", "")),
        "article_title": _text(chunk_get(chunk, "article_title", "")),
        "table_citation_label": _text(chunk_get(chunk, "table_citation_label", "")),
        "table_appendix_no": _text(chunk_get(chunk, "table_appendix_no", "")),
        "table_title": _text(chunk_get(chunk, "table_title", "")),
        "table_review_required": str(bool(chunk_get(chunk, "table_review_required", False))).lower(),
        "table_classification": _text(chunk_get(chunk, "table_classification", "")),
        "table_review_flags": "; ".join(table_flags),
        "source_parser_flags": "; ".join(parser_flags),
        "table_structured_row_count": _text(chunk_get(chunk, "table_structured_row_count", "")),
        "table_column_count": _text(chunk_get(chunk, "table_column_count", "")),
        "table_record_count": _text(chunk_get(chunk, "table_record_count", "")),
        "human_source_checked": "",
        "human_table_status": "",
        "human_match_decision": "",
        "human_notes": "",
        "snippet": snippet(review_body_text(chunk), 180),
    }


def _source_parser_flags(chunk: dict[str, Any]) -> list[str]:
    metadata = chunk_meta(chunk)
    values = metadata.get("source_hwpx_parser_review_flags") or []
    if isinstance(values, str):
        raw = [values]
    else:
        raw = list(values) if isinstance(values, (list, tuple, set)) else []
    return sorted({str(value).strip() for value in raw if str(value).strip()})


def _priority(chunk: dict[str, Any], table_flags: list[str], parser_flags: list[str]) -> str:
    classification = str(chunk_get(chunk, "table_classification", "") or "")
    if bool(chunk_get(chunk, "table_review_required", False)):
        return "source_table_compare"
    if table_flags or parser_flags or classification == "probable_table_extraction_failed":
        return "parser_structure_review"
    if not chunk_get(chunk, "table_citation_label", "") and not chunk_get(chunk, "table_appendix_no", ""):
        return "parentage_review"
    if safe_int(chunk_get(chunk, "table_structured_row_count", 0)) > 0:
        return "structured_spot_check"
    return "low_signal_table_candidate"


def _row_sort_key(row: dict[str, str]) -> tuple[int, int, int, int, str]:
    return (
        safe_int(row["document_rank"]),
        PRIORITY_ORDER.get(row["review_priority"], 99),
        safe_int(row["source_page_start"]) or 999999,
        -safe_int(row["table_structured_row_count"]),
        row["chunk_id"],
    )


def _split_joined(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(";") if item.strip()]


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SHEET_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _markdown(report: dict[str, Any], rows: list[dict[str, str]]) -> str:
    lines = [
        "# Parsing Goldset Table Candidate Sheet",
        "",
        f"- Generated at: {report['generated_at']}",
        f"- Source labels CSV: `{report['source_labels_csv']}`",
        f"- Documents: {int(report['document_count']):,}",
        f"- Table candidates: {int(report['candidate_count']):,}",
        f"- Review-required candidates: {int(report['review_required_count']):,}",
        f"- Missing-label candidates: {int(report['missing_label_candidate_count']):,}",
        f"- Document errors: {int(report['document_error_count']):,}",
        "",
        "## Safety Note",
        "",
        report["safety_note"],
        "",
        "## Priority Counts",
        "",
        "| Priority | Candidates |",
        "| --- | ---: |",
    ]
    for priority, count in sorted(
        report["priority_counts"].items(),
        key=lambda item: PRIORITY_ORDER.get(item[0], 99),
    ):
        lines.append(f"| {_md(priority)} | {int(count):,} |")
    lines.extend(
        [
            "",
            "## Candidate Rows",
            "",
            "| Rank | Document | Priority | Chunk | Page | Label | Flags | Rows | Cols |",
            "| ---: | --- | --- | --- | --- | --- | --- | ---: | ---: |",
        ]
    )
    for row in rows:
        page = row["source_page_start"]
        if row["source_page_end"] and row["source_page_end"] != row["source_page_start"]:
            page = f"{row['source_page_start']}-{row['source_page_end']}"
        label = row["table_citation_label"] or row["table_appendix_no"]
        flags = "; ".join(part for part in (row["table_review_flags"], row["source_parser_flags"]) if part)
        lines.append(
            f"| {row['rank']} | {_md(row['document_id'])} | {_md(row['review_priority'])} | "
            f"{_md(row['chunk_id'])} | {_md(page)} | {_md(label)} | {_md(flags)} | "
            f"{_md(row['table_structured_row_count'])} | {_md(row['table_column_count'])} |"
        )
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a read-only table-candidate sheet for parsing goldset review.")
    parser.add_argument("--workspace", type=Path, default=Path("."))
    parser.add_argument("--labels-csv", required=True, type=Path)
    parser.add_argument("--out-json", required=True, type=Path)
    parser.add_argument("--out-csv", required=True, type=Path)
    parser.add_argument("--out-md", required=True, type=Path)
    parser.add_argument("--max-md-rows", type=int, default=80)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    workspace = args.workspace.resolve()
    report = build_parsing_goldset_table_candidate_sheet(
        workspace=workspace,
        labels_csv=resolve_workspace_path(workspace, args.labels_csv) or args.labels_csv,
        out_json=resolve_workspace_path(workspace, args.out_json) or args.out_json,
        out_csv=resolve_workspace_path(workspace, args.out_csv) or args.out_csv,
        out_md=resolve_workspace_path(workspace, args.out_md) or args.out_md,
        max_md_rows=args.max_md_rows,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "report_type": report["report_type"],
                "candidate_count": report["candidate_count"],
                "review_required_count": report["review_required_count"],
                "out_json": report["artifacts"]["json"],
                "out_csv": report["artifacts"]["csv"],
                "out_md": report["artifacts"]["markdown"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
