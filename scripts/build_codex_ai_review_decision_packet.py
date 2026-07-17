from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def safe_float(value: Any) -> float:
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def codex_table_decision(row: dict[str, Any]) -> dict[str, str]:
    match_label = str(row.get("match_label") or "")
    triage_label = str(row.get("kordoc_triage_label") or "")
    table_classification = str(row.get("table_classification") or "")
    score = safe_float(row.get("match_score"))

    if "false_positive" in table_classification:
        decision = "human_source_check_probable_false_positive"
        risk = "high"
        action = "Do not merge. Compare the source span and reclassify the local chunk if it is prose or TOC text."
    elif match_label == "strong_review_match" and triage_label == "structured_table_candidate":
        decision = "provisional_table_structure_candidate"
        risk = "medium"
        action = "Heuristic triage marks this as a strong table-structure candidate; human approval is still required before indexing."
    elif match_label == "strong_review_match":
        decision = "provisional_candidate_needs_header_check"
        risk = "medium"
        action = "Heuristic triage accepts the match signal, but the header/row semantics need source-span confirmation."
    elif match_label == "medium_review_match":
        decision = "source_span_check_before_provisional_merge"
        risk = "medium"
        action = "Use as a review target. Do not merge until the source page confirms row and column boundaries."
    elif match_label == "weak_review_match":
        decision = "weak_hint_keep_local_warning"
        risk = "high"
        action = "Keep as a weak hint only. It is not enough to repair the table structure."
    else:
        decision = "no_confident_kordoc_support"
        risk = "high"
        action = "Keep the local parser warning and review manually if the table is citation-critical."

    if score < 25 and decision.startswith("provisional"):
        decision = "source_span_check_low_score_override"
        risk = "high"
        action = "The match score is too low for a provisional table candidate; source-span review is required."

    return {
        "codex_decision": decision,
        "codex_risk": risk,
        "codex_action": action,
        "codex_review_required": "true",
        "decision_basis": "deterministic_heuristic_no_model_call",
        "model_api_called": "false",
        "human_approval_required": "true",
        "vector_indexing_allowed": "false",
        "merge_allowed_without_human": "false",
    }


def build_rows(local_match_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    decision_rows: list[dict[str, str]] = []
    for row in local_match_rows:
        decision = codex_table_decision(row)
        decision_rows.append(
            {
                "institution_name": row.get("institution_name", ""),
                "filename": row.get("filename", ""),
                "document_id": row.get("document_id", ""),
                "chunk_id": row.get("chunk_id", ""),
                "source_page_start": row.get("source_page_start", ""),
                "hierarchy_path": row.get("hierarchy_path", ""),
                "table_classification": row.get("table_classification", ""),
                "match_label": row.get("match_label", ""),
                "match_score": row.get("match_score", ""),
                "kordoc_triage_label": row.get("kordoc_triage_label", ""),
                "kordoc_row_count": row.get("kordoc_row_count", ""),
                "kordoc_column_count": row.get("kordoc_column_count", ""),
                **decision,
                "local_text_sample": row.get("local_text_sample", ""),
                "kordoc_sample_rows": row.get("kordoc_sample_rows", ""),
            }
        )
    return decision_rows


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else [
        "institution_name",
        "filename",
        "chunk_id",
        "codex_decision",
        "vector_indexing_allowed",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(path: Path, rows: list[dict[str, str]], *, source_csv: Path, csv_path: Path) -> None:
    decision_counts = Counter(row.get("codex_decision") for row in rows)
    match_counts = Counter(row.get("match_label") for row in rows)
    provisional_count = sum(
        count
        for decision, count in decision_counts.items()
        if str(decision).startswith("provisional") or str(decision).startswith("source_span_check_before")
    )
    lines = [
        "# Heuristic Table Review Decision Packet",
        "",
        f"- Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Source CSV: `{source_csv}`",
        f"- Detail CSV: `{csv_path}`",
        f"- Review target rows: {len(rows)}",
        f"- Heuristic provisional candidate rows: {provisional_count}",
        "",
        "## Scope",
        "",
        "- This packet is deterministic triage over Kordoc/local table match CSV fields. It does not call an AI model.",
        "- No row in this packet permits Vector DB indexing or automatic merge before human approval.",
        "- `provisional_*` decisions are review priorities, not verified table structure or approval evidence.",
        "",
        "## Match Distribution",
        "",
        "|Label|Count|",
        "|---|---:|",
    ]
    for label, count in match_counts.most_common():
        lines.append(f"|{label}|{count}|")
    lines.extend(["", "## Decision Distribution", "", "|Decision|Count|", "|---|---:|"])
    for label, count in decision_counts.most_common():
        lines.append(f"|{label}|{count}|")
    lines.extend(["", "## Document Summary", "", "|Institution|Document|Rows|Provisional rows|Top decisions|", "|---|---|---:|---:|---|"])
    by_doc: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in rows:
        key = (row.get("institution_name", ""), row.get("filename", ""))
        by_doc.setdefault(key, []).append(row)
    for (institution, filename), doc_rows in sorted(by_doc.items(), key=lambda item: item[0]):
        doc_decisions = Counter(row.get("codex_decision") for row in doc_rows)
        doc_provisional = sum(
            count
            for decision, count in doc_decisions.items()
            if str(decision).startswith("provisional") or str(decision).startswith("source_span_check_before")
        )
        summary = ", ".join(f"{label}:{count}" for label, count in doc_decisions.most_common(3))
        lines.append(f"|{md(institution)}|{md(filename)}|{len(doc_rows)}|{doc_provisional}|{md(summary)}|")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build deterministic triage decisions for Kordoc/local table matches.")
    parser.add_argument(
        "--local-match-csv",
        default="parser_10doc_kordoc_local_table_match_packet_20260711.csv",
    )
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--out-csv", default="parser_10doc_codex_ai_review_decisions_20260711.csv")
    parser.add_argument("--out-markdown", default="parser_10doc_codex_ai_review_decisions_20260711.md")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir) if args.out_dir else Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
    source_csv = Path(args.local_match_csv)
    rows = build_rows(load_csv(source_csv))
    csv_path = out_dir / args.out_csv
    md_path = out_dir / args.out_markdown
    write_csv(csv_path, rows)
    write_markdown(md_path, rows, source_csv=source_csv, csv_path=csv_path)
    print(
        json.dumps(
            {
                "markdown": str(md_path),
                "csv": str(csv_path),
                "source_csv": str(source_csv),
                "row_count": len(rows),
                "decision_counts": Counter(row.get("codex_decision") for row in rows),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
