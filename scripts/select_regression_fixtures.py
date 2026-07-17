from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def select_fixtures(batch_report: dict[str, Any], catalog: dict[str, Any] | None = None, *, top_n: int = 8) -> dict[str, Any]:
    rows = batch_report.get("rows", []) or []
    catalog_rows = (catalog or {}).get("rows", []) or []
    selected = {
        "table_false_positive_top": top_rows(rows, "probable_table_false_positive_chunks", top_n),
        "table_heavy_top": top_rows(rows, "table_cell_row_count", top_n),
        "coverage_low": sorted(
            [row_summary(row) for row in rows],
            key=lambda row: row["chunk_to_source_char_ratio"],
        )[:top_n],
        "coverage_high": sorted(
            [row_summary(row) for row in rows],
            key=lambda row: row["chunk_to_source_char_ratio"],
            reverse=True,
        )[:top_n],
        "format_diversity": format_diversity(rows),
        "duplicate_board_attachments": duplicate_board_attachments(catalog_rows),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_batch_generated_at": batch_report.get("generated_at"),
        "source_catalog_generated_at": (catalog or {}).get("generated_at"),
        "criteria": {
            "top_n": top_n,
            "purpose": "Regression fixture candidates for public-institution preprocessing drift.",
        },
        "selected": selected,
    }


def top_rows(rows: list[dict[str, Any]], key: str, limit: int) -> list[dict[str, Any]]:
    candidates = [row for row in rows if numeric(row.get(key)) > 0]
    return [row_summary(row) for row in sorted(candidates, key=lambda row: numeric(row.get(key)), reverse=True)[:limit]]


def format_diversity(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: str(item.get("filename", "")).lower()):
        suffix = Path(str(row.get("filename", ""))).suffix.lower()
        if suffix in seen:
            continue
        seen.add(suffix)
        result.append(row_summary(row))
    return result


def duplicate_board_attachments(catalog_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_board: dict[str, list[dict[str, Any]]] = {}
    for row in catalog_rows:
        board_no = str(row.get("board_no") or "").strip()
        if not board_no:
            continue
        by_board.setdefault(board_no, []).append(row)
    result: list[dict[str, Any]] = []
    for board_no, rows in sorted(by_board.items()):
        if len(rows) < 2:
            continue
        result.append(
            {
                "board_no": board_no,
                "attachment_count": len(rows),
                "files": [
                    {
                        "file_no": str(row.get("file_no") or ""),
                        "file_name": str(row.get("file_name") or ""),
                        "file_sha256": str(row.get("file_sha256") or ""),
                    }
                    for row in rows
                ],
            }
        )
    return result


def row_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "document_id": row.get("document_id", ""),
        "filename": row.get("filename", ""),
        "quality_score": numeric(row.get("quality_score")),
        "warning_count": numeric(row.get("warning_count")),
        "node_count": numeric(row.get("node_count")),
        "chunk_count": numeric(row.get("chunk_count")),
        "chunk_to_source_char_ratio": numeric(row.get("chunk_to_source_char_ratio")),
        "table_like_chunks": numeric(row.get("table_like_chunks")),
        "table_cell_row_count": numeric(row.get("table_cell_row_count")),
        "probable_table_false_positive_chunks": numeric(row.get("probable_table_false_positive_chunks")),
        "probable_table_extraction_failed_chunks": numeric(row.get("probable_table_extraction_failed_chunks")),
    }


def numeric(value: Any) -> int | float:
    if isinstance(value, bool) or value in ("", None):
        return 0
    if isinstance(value, (int, float)):
        return value
    try:
        number = float(str(value))
    except ValueError:
        return 0
    return int(number) if number.is_integer() else number


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Regression Fixture Candidates",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Batch generated at: {report.get('source_batch_generated_at')}",
        f"- Catalog generated at: {report.get('source_catalog_generated_at')}",
        "",
    ]
    selected = report["selected"]
    for section in [
        "table_false_positive_top",
        "table_heavy_top",
        "coverage_low",
        "coverage_high",
        "format_diversity",
    ]:
        lines.extend([f"## {section}", ""])
        rows = selected.get(section) or []
        if not rows:
            lines.append("- None")
        for row in rows:
            lines.append(
                "- `{document_id}` {filename} score={quality_score} chunks={chunk_count} "
                "coverage={chunk_to_source_char_ratio} table_rows={table_cell_row_count} false_pos={probable_table_false_positive_chunks}".format(
                    **row
                )
            )
        lines.append("")
    lines.extend(["## duplicate_board_attachments", ""])
    duplicates = selected.get("duplicate_board_attachments") or []
    if not duplicates:
        lines.append("- None")
    for item in duplicates:
        names = ", ".join(file["file_name"] for file in item["files"])
        lines.append(f"- `{item['board_no']}` {item['attachment_count']} files: {names}")
    return "\n".join(lines).strip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select regression fixture candidates from batch/catalog reports.")
    parser.add_argument("--batch-report", required=True)
    parser.add_argument("--catalog-json", default=None)
    parser.add_argument("--out-prefix", required=True)
    parser.add_argument("--top-n", type=int, default=8)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    batch_report = load_json(Path(args.batch_report))
    catalog = load_json(Path(args.catalog_json)) if args.catalog_json else None
    report = select_fixtures(batch_report, catalog, top_n=args.top_n)
    prefix = Path(args.out_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = prefix.with_suffix(".json")
    md_path = prefix.with_suffix(".md")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(markdown_report(report), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(md_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
