from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


DEFAULT_CATEGORIES = (
    "attachment_parentage",
    "table_structure",
    "supplementary_temporal",
    "parser_uncertainty",
    "no_signal_sample",
)

TRIAGE_FIELDNAMES = [
    "triage_rank",
    "triage_category",
    "review_batch_id",
    "review_type",
    "review_priority_tier",
    "chunk_id",
    "chunk_type",
    "article_no",
    "article_title",
    "regulation_title",
    "source_page_start",
    "source_page_end",
    "attention_reasons",
    "appendix_refs",
    "form_refs",
    "table_citation_label",
    "table_review_flags",
    "parser_uncertainty_flags",
    "recommended_action",
    "human_label",
    "human_notes",
    "snippet",
]

CATEGORY_ACTIONS = {
    "attachment_parentage": "Confirm governing article for this appendix/form/table evidence before approval.",
    "table_structure": "Compare source table with extracted rows/cells and mark whether table structure is acceptable.",
    "supplementary_temporal": "Confirm effective-date or supplementary-provision context before approval.",
    "parser_uncertainty": "Check source extraction uncertainty and decide whether parser repair or source review is required.",
    "no_signal_sample": "Spot-check a no-signal draft chunk before any broad approval decision.",
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def build_approval_review_triage(
    *,
    review_batches_json: Path,
    chunks_json: Path,
    out_csv: Path,
    out_json: Path,
    out_md: Path,
    categories: Sequence[str] = DEFAULT_CATEGORIES,
    max_per_category: int = 25,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if max_per_category <= 0:
        raise ValueError("max_per_category must be greater than zero.")
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    categories = tuple(dict.fromkeys(str(category) for category in categories if str(category).strip()))
    if not categories:
        raise ValueError("At least one triage category is required.")

    review_batches = load_json(review_batches_json)
    chunks = load_json(chunks_json)
    if not isinstance(chunks, list):
        raise ValueError("chunks_json must contain a JSON array.")
    chunk_lookup = {str(chunk.get("chunk_id") or ""): chunk for chunk in chunks if isinstance(chunk, dict)}
    candidates = _candidate_rows(review_batches, chunk_lookup)

    selected: list[dict[str, str]] = []
    used_chunk_ids: set[str] = set()
    for category in categories:
        category_rows = [row for row in candidates if _matches_category(row, category)]
        category_rows.sort(key=_candidate_sort_key)
        count = 0
        for row in category_rows:
            if count >= max_per_category:
                break
            chunk_id = row["chunk_id"]
            if chunk_id in used_chunk_ids:
                continue
            selected.append(_triage_row(row, category=category, rank=len(selected) + 1))
            used_chunk_ids.add(chunk_id)
            count += 1

    report = {
        "report_type": "approval_review_triage",
        "generated_at": generated_at,
        "source_review_batches": str(review_batches_json),
        "source_chunks": str(chunks_json),
        "categories": list(categories),
        "max_per_category": max_per_category,
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "selected_category_counts": dict(Counter(row["triage_category"] for row in selected)),
        "review_batch_count": int(review_batches.get("batch_count") or len(review_batches.get("batches") or [])),
        "approval_chunk_count": int(review_batches.get("approval_chunk_count") or 0),
        "manual_attention_chunks": int(review_batches.get("manual_attention_chunks") or 0),
        "safety_note": (
            "This triage packet only selects representative chunks for human review. "
            "It does not approve chunks, acknowledge review flags, index vectors, or publish MCP evidence."
        ),
        "artifacts": {
            "csv": str(out_csv),
            "json": str(out_json),
            "markdown": str(out_md),
        },
    }
    _write_csv(out_csv, selected)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps({**report, "rows": selected}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(_markdown(report, selected), encoding="utf-8")
    return report


def _candidate_rows(review_batches: dict[str, Any], chunk_lookup: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for batch in review_batches.get("batches") or []:
        if not isinstance(batch, dict):
            continue
        for item in batch.get("chunks") or []:
            if not isinstance(item, dict):
                continue
            chunk_id = str(item.get("chunk_id") or "")
            chunk = chunk_lookup.get(chunk_id) or {}
            metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
            rows.append(
                {
                    "review_batch_id": str(batch.get("review_batch_id") or ""),
                    "review_type": str(batch.get("review_type") or ""),
                    "review_priority_tier": str(item.get("review_priority_tier") or ""),
                    "review_category": str(item.get("review_category") or ""),
                    "attention_reasons": [str(value) for value in item.get("attention_reasons") or []],
                    "chunk_id": chunk_id,
                    "chunk_type": str(item.get("chunk_type") or chunk.get("chunk_type") or metadata.get("chunk_type") or ""),
                    "article_no": str(item.get("article_no") or metadata.get("article_no") or ""),
                    "article_title": str(item.get("article_title") or metadata.get("article_title") or ""),
                    "regulation_title": str(metadata.get("regulation_title") or ""),
                    "source_page_start": str(metadata.get("source_page_start") or ""),
                    "source_page_end": str(metadata.get("source_page_end") or ""),
                    "appendix_refs": _join(metadata.get("appendix_refs")),
                    "form_refs": _join(metadata.get("form_refs")),
                    "table_citation_label": str(metadata.get("table_citation_label") or ""),
                    "table_review_flags": _join(metadata.get("table_review_flags")),
                    "parser_uncertainty_flags": _join(metadata.get("parser_uncertainty_flags")),
                    "snippet": _snippet(str(chunk.get("text") or chunk.get("retrieval_text") or "")),
                }
            )
    return rows


def _matches_category(row: dict[str, Any], category: str) -> bool:
    reasons = set(row.get("attention_reasons") or [])
    reason_blob = " ".join(sorted(reasons))
    chunk_type = str(row.get("chunk_type") or "")
    if category == "attachment_parentage":
        return (
            chunk_type in {"appendix", "form", "table"}
            or "form_or_appendix_candidate" in reasons
            or "review_category:appendix_form_review" in reasons
            or bool(row.get("appendix_refs") or row.get("form_refs"))
        )
    if category == "table_structure":
        return (
            "table_context_candidate" in reasons
            or "table_review_required" in reasons
            or "table_review_flags" in reason_blob
            or bool(row.get("table_citation_label") or row.get("table_review_flags"))
        )
    if category == "supplementary_temporal":
        return "supplementary" in reason_blob or "effective_date" in reason_blob
    if category == "parser_uncertainty":
        return "parser_uncertainty" in reason_blob or bool(row.get("parser_uncertainty_flags"))
    if category == "no_signal_sample":
        return str(row.get("review_priority_tier") or "") == "no_signal"
    return False


def _candidate_sort_key(row: dict[str, Any]) -> tuple[int, int, str, str]:
    tier_order = {
        "blocking_review": 0,
        "domain_attention": 1,
        "stable_false_positive": 2,
        "informational": 3,
        "no_signal": 4,
    }
    reasons = row.get("attention_reasons") or []
    return (
        tier_order.get(str(row.get("review_priority_tier") or ""), 99),
        -len(reasons),
        str(row.get("review_batch_id") or ""),
        str(row.get("chunk_id") or ""),
    )


def _triage_row(row: dict[str, Any], *, category: str, rank: int) -> dict[str, str]:
    return {
        "triage_rank": str(rank),
        "triage_category": category,
        "review_batch_id": str(row.get("review_batch_id") or ""),
        "review_type": str(row.get("review_type") or ""),
        "review_priority_tier": str(row.get("review_priority_tier") or ""),
        "chunk_id": str(row.get("chunk_id") or ""),
        "chunk_type": str(row.get("chunk_type") or ""),
        "article_no": str(row.get("article_no") or ""),
        "article_title": str(row.get("article_title") or ""),
        "regulation_title": str(row.get("regulation_title") or ""),
        "source_page_start": str(row.get("source_page_start") or ""),
        "source_page_end": str(row.get("source_page_end") or ""),
        "attention_reasons": _join(row.get("attention_reasons")),
        "appendix_refs": str(row.get("appendix_refs") or ""),
        "form_refs": str(row.get("form_refs") or ""),
        "table_citation_label": str(row.get("table_citation_label") or ""),
        "table_review_flags": str(row.get("table_review_flags") or ""),
        "parser_uncertainty_flags": str(row.get("parser_uncertainty_flags") or ""),
        "recommended_action": CATEGORY_ACTIONS.get(category, ""),
        "human_label": "",
        "human_notes": "",
        "snippet": str(row.get("snippet") or ""),
    }


def _join(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return "; ".join(str(item) for item in value if str(item).strip())
    return str(value)


def _snippet(value: str, limit: int = 360) -> str:
    compact = " ".join(value.split())
    return compact[:limit]


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRIAGE_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _markdown(report: dict[str, Any], rows: list[dict[str, str]]) -> str:
    lines = [
        "# Approval Review Triage",
        "",
        f"- Generated at: {report['generated_at']}",
        f"- Source review batches: `{report['source_review_batches']}`",
        f"- Source chunks: `{report['source_chunks']}`",
        f"- Candidate chunks: {report['candidate_count']:,}",
        f"- Selected rows: {report['selected_count']:,}",
        f"- Manual-attention chunks: {report['manual_attention_chunks']:,}",
        "",
        "## Category Summary",
        "",
        "| Category | Selected |",
        "| --- | ---: |",
    ]
    counts = report["selected_category_counts"]
    for category in report["categories"]:
        lines.append(f"| {category} | {counts.get(category, 0):,} |")
    lines.extend(
        [
            "",
            "## Selected Rows",
            "",
            "| Rank | Category | Tier | Batch | Chunk | Article | Reasons | Snippet |",
            "| ---: | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        article = " ".join(part for part in [row["regulation_title"], row["article_no"], row["article_title"]] if part)
        lines.append(
            f"| {row['triage_rank']} | {_md(row['triage_category'])} | {_md(row['review_priority_tier'])} | "
            f"{_md(row['review_batch_id'])} | {_md(row['chunk_id'])} | {_md(article)} | "
            f"{_md(row['attention_reasons'])} | {_md(row['snippet'])} |"
        )
    lines.append("")
    lines.append(f"Safety: {report['safety_note']}")
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a representative triage packet from approval review batches.")
    parser.add_argument("--review-batches-json", type=Path, required=True)
    parser.add_argument("--chunks-json", type=Path, required=True)
    parser.add_argument("--out-csv", type=Path, required=True)
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--max-per-category", type=int, default=25)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    categories = args.category or list(DEFAULT_CATEGORIES)
    try:
        report = build_approval_review_triage(
            review_batches_json=args.review_batches_json,
            chunks_json=args.chunks_json,
            out_csv=args.out_csv,
            out_json=args.out_json,
            out_md=args.out_md,
            categories=categories,
            max_per_category=args.max_per_category,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"ok": True, **{k: v for k, v in report.items() if k != "artifacts"}, "artifacts": report["artifacts"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
