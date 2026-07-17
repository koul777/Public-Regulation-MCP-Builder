from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.processors.kordoc_table_matcher import table_text, triage_kordoc_table


DOTTED_LEADER_PATTERN = re.compile(r"(\.{3,}|·{3,}|…{2,})")
ARTICLE_TEXT_PATTERN = re.compile(r"(제\s*\d+\s*조|제\s*조|<\s*(개정|신설|삭제)|shall|must)", re.IGNORECASE)
SENTENCE_PATTERN = re.compile(r"([.!?]|다\.|한다\.|한다|하여야|shall|must)")
HEADER_WORDS = {
    "category",
    "type",
    "item",
    "standard",
    "criteria",
    "amount",
    "rate",
    "date",
    "name",
    "position",
    "grade",
    "division",
    "description",
    "구분",
    "항목",
    "기준",
    "지급",
    "금액",
    "비율",
    "직급",
    "대상",
    "내용",
    "명칭",
    "위치",
}


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def latest_batch_csv(reports_dir: Path) -> Path:
    candidates = sorted(reports_dir.glob("batch_quality_*.csv"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No batch_quality_*.csv found in {reports_dir}")
    return candidates[0]


def load_inventory(data_dir: Path, document_id: str) -> dict[str, Any]:
    chunks_path = data_dir / "repository" / f"{document_id}_chunks.json"
    if not chunks_path.exists():
        return {"status": "missing_chunks", "tables": []}
    chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
    if not chunks:
        return {"status": "missing_chunks", "tables": []}
    metadata = chunks[0].get("metadata") if isinstance(chunks[0], dict) else {}
    inventory = metadata.get("kordoc_table_inventory") if isinstance(metadata, dict) else None
    if not isinstance(inventory, dict):
        return {"status": "missing_inventory", "tables": []}
    return inventory


def first_rows(table: dict[str, Any]) -> tuple[str, str]:
    rows = [row for row in table.get("cell_rows") or [] if isinstance(row, dict)]
    first = str(rows[0].get("raw") or "") if rows else ""
    second = str(rows[1].get("raw") or "") if len(rows) > 1 else ""
    return first, second


def triage_table(table: dict[str, Any]) -> tuple[str, str, str]:
    return triage_kordoc_table(table)


def _legacy_triage_table(table: dict[str, Any]) -> tuple[str, str, str]:
    row_count = safe_int(table.get("row_count"))
    column_count = safe_int(table.get("column_count"))
    cell_count = safe_int(table.get("cell_count"))
    sample = table_text(table, max_rows=6)
    lower = sample.lower()
    average_cell_chars = len(sample) / max(cell_count, 1)

    if DOTTED_LEADER_PATTERN.search(sample):
        return (
            "probable_toc_table",
            "Dotted leaders/page-number layout suggests table-of-contents extraction, not a regulation table.",
            "Do not merge into table chunks; keep as source-navigation evidence only.",
        )
    if row_count <= 1 and column_count <= 1:
        return (
            "weak_single_cell_signal",
            "Single-cell Kordoc table signal is too weak to upgrade local table structure.",
            "Use only as an AI review hint.",
        )
    if ARTICLE_TEXT_PATTERN.search(sample) and (average_cell_chars >= 45 or row_count <= 5):
        return (
            "probable_prose_false_positive",
            "Article or amendment prose appears split into cells.",
            "Do not auto-merge; send to AI/human review as a possible false positive.",
        )
    if column_count >= 2 and row_count >= 2 and header_score(table) >= 1:
        return (
            "structured_table_candidate",
            "Multi-row, multi-column signal with header-like cells.",
            "Candidate for Kordoc-assisted merge, still review_required until approved.",
        )
    if column_count >= 2 and row_count >= 3 and average_cell_chars < 60:
        return (
            "possible_table_candidate",
            "Multi-row, multi-column signal with compact cells.",
            "Candidate for AI comparison against local table chunk before merge.",
        )
    if "appendix" in lower or "form" in lower:
        return (
            "attachment_candidate",
            "Attachment/form wording appears in the table sample.",
            "Compare with appendix/form boundaries before merge.",
        )
    return (
        "needs_ai_review",
        "Kordoc table signal is not strong enough for automatic classification.",
        "Review source span or compare with local chunk before use.",
    )


def header_score(table: dict[str, Any]) -> int:
    rows = [row for row in table.get("cell_rows") or [] if isinstance(row, dict)]
    if not rows:
        return 0
    cells = [str(cell).strip() for cell in rows[0].get("cells") or [] if str(cell).strip()]
    score = 0
    for cell in cells:
        compact = re.sub(r"\s+", "", cell.lower())
        if len(compact) <= 20 and any(word in compact for word in HEADER_WORDS):
            score += 1
    return score


def safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def build_rows(batch_csv: Path, data_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for document in load_csv(batch_csv):
        document_id = document.get("document_id") or ""
        inventory = load_inventory(data_dir, document_id)
        tables = inventory.get("tables") if isinstance(inventory.get("tables"), list) else []
        for table in tables:
            if not isinstance(table, dict):
                continue
            first, second = first_rows(table)
            label, reason, action = triage_table(table)
            rows.append(
                {
                    "institution_name": document.get("institution_name", ""),
                    "filename": document.get("filename", ""),
                    "document_id": document_id,
                    "kordoc_status": inventory.get("status", ""),
                    "kordoc_table_total": inventory.get("table_count", 0),
                    "stored_table_total": inventory.get("stored_table_count", len(tables)),
                    "tables_truncated": inventory.get("tables_truncated", False),
                    "table_index": table.get("table_index"),
                    "source_page": table.get("source_page"),
                    "title": table.get("title", ""),
                    "row_count": table.get("row_count", 0),
                    "column_count": table.get("column_count", 0),
                    "cell_count": table.get("cell_count", 0),
                    "merged_cell_count": table.get("merged_cell_count", 0),
                    "nested_table_count": table.get("nested_table_count", 0),
                    "first_row": first,
                    "second_row": second,
                    "sample_rows": table_text(table, max_rows=4),
                    "codex_triage_label": label,
                    "codex_triage_reason": reason,
                    "recommended_action": action,
                }
            )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else [
        "institution_name",
        "filename",
        "document_id",
        "codex_triage_label",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(path: Path, rows: list[dict[str, Any]], *, batch_csv: Path, csv_path: Path) -> None:
    counts = Counter(row.get("codex_triage_label") for row in rows)
    document_count = len({row.get("document_id") for row in rows})
    total_kordoc_tables = sum(
        safe_int(row.get("kordoc_table_total"))
        for row in {row.get("document_id"): row for row in rows}.values()
    )
    lines = [
        "# Kordoc 표 후보 리뷰 패킷",
        "",
        f"- 작성 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 입력 배치: `{batch_csv}`",
        f"- 상세 CSV: `{csv_path}`",
        f"- 문서 수: {document_count}",
        f"- Kordoc 감지 표 총계: {total_kordoc_tables}",
        f"- 저장된 리뷰 후보: {len(rows)}",
        "",
        "## 핵심 판단",
        "",
        "- Kordoc은 10개 문서에서 표 신호를 모두 만들었지만, PDF에서는 목차와 산문을 표로 잡는 경우가 보입니다.",
        "- 따라서 Kordoc 결과는 바로 RAG/MCP 검색 본문으로 확정 병합하지 말고, AI/Codex 검수와 사람 승인 전 단계의 후보로 다루는 것이 맞습니다.",
        "- 병합 대상 1순위는 `structured_table_candidate`와 `possible_table_candidate`이고, `probable_toc_table` 및 `probable_prose_false_positive`는 오탐 후보입니다.",
        "",
        "## 분류 현황",
        "",
        "|분류|건수|",
        "|---|---:|",
    ]
    for label, count in counts.most_common():
        lines.append(f"|{label}|{count}|")
    lines.extend(["", "## 문서별 요약", "", "|기관|문서|후보 수|주요 분류|", "|---|---|---:|---|"])
    by_doc: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        by_doc.setdefault((str(row.get("institution_name") or ""), str(row.get("filename") or "")), []).append(row)
    for (institution, filename), doc_rows in sorted(by_doc.items(), key=lambda item: item[0]):
        doc_counts = Counter(row.get("codex_triage_label") for row in doc_rows)
        summary = ", ".join(f"{label}:{count}" for label, count in doc_counts.most_common(3))
        lines.append(f"|{md(institution)}|{md(filename)}|{len(doc_rows)}|{md(summary)}|")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a review packet for Kordoc table candidates.")
    parser.add_argument("--reports-dir", default="reports/parser_10doc_eval_kordoc_20260711")
    parser.add_argument("--batch-csv", default=None)
    parser.add_argument("--data-dir", default="data/parser_10doc_eval_kordoc_20260711")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--out-csv", default="parser_10doc_kordoc_table_review_packet_20260711.csv")
    parser.add_argument("--out-markdown", default="parser_10doc_kordoc_table_review_packet_20260711.md")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    batch_csv = Path(args.batch_csv) if args.batch_csv else latest_batch_csv(Path(args.reports_dir))
    out_dir = Path(args.out_dir) if args.out_dir else Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
    csv_path = out_dir / args.out_csv
    md_path = out_dir / args.out_markdown
    rows = build_rows(batch_csv, Path(args.data_dir))
    write_csv(csv_path, rows)
    write_markdown(md_path, rows, batch_csv=batch_csv, csv_path=csv_path)
    print(
        json.dumps(
            {
                "markdown": str(md_path),
                "csv": str(csv_path),
                "batch_csv": str(batch_csv),
                "row_count": len(rows),
                "triage_counts": Counter(row.get("codex_triage_label") for row in rows),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
