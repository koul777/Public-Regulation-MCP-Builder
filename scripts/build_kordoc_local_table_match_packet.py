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

from app.processors.kordoc_table_matcher import (
    classify_match,
    match_score,
    mergeable_kordoc_tables,
    table_text,
    tokenize,
)
from scripts.build_kordoc_table_review_packet import latest_batch_csv


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_chunks(data_dir: Path, document_id: str) -> list[dict[str, Any]]:
    path = data_dir / "repository" / f"{document_id}_chunks.json"
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, list) else []


def kordoc_inventory(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    if not chunks:
        return {}
    metadata = chunks[0].get("metadata") if isinstance(chunks[0], dict) else {}
    inventory = metadata.get("kordoc_table_inventory") if isinstance(metadata, dict) else None
    return inventory if isinstance(inventory, dict) else {}


def local_table_candidates(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = []
    for chunk in chunks:
        metadata = chunk.get("metadata") if isinstance(chunk, dict) else {}
        if not isinstance(metadata, dict):
            continue
        if not metadata.get("table_like"):
            continue
        if metadata.get("table_cell_rows"):
            continue
        candidates.append(chunk)
    return candidates


def best_kordoc_match(local_chunk: dict[str, Any], tables: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float, str]:
    local_text = str(local_chunk.get("normalized_text") or local_chunk.get("text") or "")
    local_page = local_chunk.get("source_page_start")
    best_table: dict[str, Any] | None = None
    best_score = 0.0
    for table in tables:
        kordoc_text = table_text(table, max_rows=8)
        same_page = local_page is not None and table.get("source_page") == local_page
        score = match_score(local_text, kordoc_text, same_page=same_page)
        if score > best_score:
            best_score = score
            best_table = table
    label = classify_match(best_score, str((best_table or {}).get("codex_triage_label") or ""))
    return best_table, best_score, label


def build_rows(batch_csv: Path, data_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for document in load_csv(batch_csv):
        document_id = document.get("document_id") or ""
        chunks = load_chunks(data_dir, document_id)
        local_candidates = local_table_candidates(chunks)
        inventory = kordoc_inventory(chunks)
        kordoc_tables = mergeable_kordoc_tables(inventory)
        for chunk in local_candidates:
            metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
            table, score, match_label = best_kordoc_match(chunk, kordoc_tables)
            table = table or {}
            rows.append(
                {
                    "institution_name": document.get("institution_name", ""),
                    "filename": document.get("filename", ""),
                    "document_id": document_id,
                    "chunk_id": chunk.get("chunk_id", ""),
                    "chunk_type": chunk.get("chunk_type", ""),
                    "source_page_start": chunk.get("source_page_start", ""),
                    "hierarchy_path": metadata.get("hierarchy_path", ""),
                    "table_classification": metadata.get("table_classification", ""),
                    "table_review_reason": metadata.get("table_review_reason", ""),
                    "local_text_sample": sample_text(str(chunk.get("normalized_text") or chunk.get("text") or "")),
                    "kordoc_table_index": table.get("table_index", ""),
                    "kordoc_triage_label": table.get("codex_triage_label", ""),
                    "kordoc_row_count": table.get("row_count", ""),
                    "kordoc_column_count": table.get("column_count", ""),
                    "kordoc_sample_rows": table_text(table, max_rows=4) if table else "",
                    "match_score": score,
                    "match_label": match_label,
                    "codex_recommendation": recommendation(match_label),
                }
            )
    return rows


def sample_text(value: str, max_chars: int = 240) -> str:
    clean = re.sub(r"\s+", " ", value).strip()
    return clean[:max_chars]


def recommendation(match_label: str) -> str:
    if match_label in {"strong_review_match", "medium_review_match"}:
        return "AI/Codex should compare source span before provisional Kordoc-assisted table merge; keep review_required."
    if match_label == "weak_review_match":
        return "Use as a weak review hint only; do not merge automatically."
    return "No safe Kordoc match; keep local warning or reclassify local false positive after review."


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else [
        "institution_name",
        "filename",
        "chunk_id",
        "match_label",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(path: Path, rows: list[dict[str, Any]], *, batch_csv: Path, csv_path: Path) -> None:
    counts = Counter(row.get("match_label") for row in rows)
    document_count = len({row.get("document_id") for row in rows})
    review_match_count = counts.get("strong_review_match", 0) + counts.get("medium_review_match", 0)
    lines = [
        "# Kordoc-로컬 표 후보 매칭 패킷",
        "",
        f"- 작성 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 입력 배치: `{batch_csv}`",
        f"- 상세 CSV: `{csv_path}`",
        f"- 문서 수: {document_count}",
        f"- 로컬 셀 미보존 table-like chunk: {len(rows)}",
        f"- 강/중 매칭 후보: {review_match_count}",
        "",
        "## 핵심 판단",
        "",
        "- 이 패킷은 Kordoc 표를 정식 table chunk로 확정하지 않습니다.",
        "- 목적은 로컬에서 셀이 비어 있는 table-like chunk에 대해 Kordoc 후보가 대응될 가능성이 있는지 AI/Codex 검수 대상으로 좁히는 것입니다.",
        "- `strong_review_match`와 `medium_review_match`만 provisional merge 후보이고, 최종 인덱싱은 기존 승인 게이트를 통과해야 합니다.",
        "",
        "## 매칭 현황",
        "",
        "|분류|건수|",
        "|---|---:|",
    ]
    for label, count in counts.most_common():
        lines.append(f"|{label}|{count}|")
    lines.extend(["", "## 문서별 요약", "", "|기관|문서|셀 미보존 chunk|강/중 후보|주요 분류|", "|---|---|---:|---:|---|"])
    by_doc: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        by_doc.setdefault((str(row.get("institution_name") or ""), str(row.get("filename") or "")), []).append(row)
    for (institution, filename), doc_rows in sorted(by_doc.items(), key=lambda item: item[0]):
        doc_counts = Counter(row.get("match_label") for row in doc_rows)
        doc_review = doc_counts.get("strong_review_match", 0) + doc_counts.get("medium_review_match", 0)
        summary = ", ".join(f"{label}:{count}" for label, count in doc_counts.most_common(3))
        lines.append(f"|{md(institution)}|{md(filename)}|{len(doc_rows)}|{doc_review}|{md(summary)}|")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Match local table-like chunks without cells to Kordoc table candidates.")
    parser.add_argument("--reports-dir", default="reports/parser_10doc_eval_kordoc_20260711")
    parser.add_argument("--batch-csv", default=None)
    parser.add_argument("--data-dir", default="data/parser_10doc_eval_kordoc_20260711")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--out-csv", default="parser_10doc_kordoc_local_table_match_packet_20260711.csv")
    parser.add_argument("--out-markdown", default="parser_10doc_kordoc_local_table_match_packet_20260711.md")
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
                "match_counts": Counter(row.get("match_label") for row in rows),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
