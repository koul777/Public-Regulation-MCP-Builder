from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


METRIC_KEYS = [
    "quality_score",
    "node_count",
    "chunk_count",
    "warning_count",
    "failed_info_check_count",
    "recommendation_count",
    "chunk_to_source_char_ratio",
    "table_like_chunks",
    "table_like_without_cell_rows",
    "table_cell_row_count",
    "probable_table_false_positive_chunks",
    "stable_table_false_positive_chunks",
    "table_false_positive_attention_chunks",
    "probable_table_extraction_failed_chunks",
    "chunks_missing_regulation_no",
    "article_chunks_missing_regulation_no",
]


def load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def row_identity(row: dict[str, Any]) -> str:
    source_system = clean(row.get("source_system"))
    source_record_id = clean(row.get("source_record_id"))
    source_file_id = clean(row.get("source_file_id"))
    if source_system and source_record_id and source_file_id:
        return f"{source_system}:{source_record_id}:{source_file_id}"
    input_path = clean(row.get("input_path"))
    if input_path:
        return f"path:{input_path.lower()}"
    return f"filename:{clean(row.get('filename')).lower()}"


def compare_reports(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_rows = before.get("rows", []) or []
    after_rows = after.get("rows", []) or []
    before_by_id = {row_identity(row): row for row in before_rows}
    after_by_id = {row_identity(row): row for row in after_rows}
    before_ids = set(before_by_id)
    after_ids = set(after_by_id)
    common_ids = sorted(before_ids & after_ids)
    metric_changed: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    for identity in common_ids:
        changes = metric_delta(before_by_id[identity], after_by_id[identity])
        if changes:
            metric_changed.append(
                {
                    "identity": identity,
                    "before": row_summary(before_by_id[identity]),
                    "after": row_summary(after_by_id[identity]),
                    "changes": changes,
                }
            )
        else:
            unchanged.append(row_summary(after_by_id[identity]))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "before_generated_at": before.get("generated_at"),
        "after_generated_at": after.get("generated_at"),
        "counts": {
            "before_rows": len(before_rows),
            "after_rows": len(after_rows),
            "added": len(after_ids - before_ids),
            "removed": len(before_ids - after_ids),
            "metric_changed": len(metric_changed),
            "unchanged": len(unchanged),
        },
        "added": [row_summary(after_by_id[identity]) for identity in sorted(after_ids - before_ids)],
        "removed": [row_summary(before_by_id[identity]) for identity in sorted(before_ids - after_ids)],
        "metric_changed": metric_changed,
        "unchanged": unchanged,
    }


def metric_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, int | float]]:
    changes: dict[str, dict[str, int | float]] = {}
    for key in METRIC_KEYS:
        before_value = numeric(before.get(key))
        after_value = numeric(after.get(key))
        if before_value != after_value:
            changes[key] = {
                "before": before_value,
                "after": after_value,
                "delta": round(after_value - before_value, 6),
            }
    return changes


def row_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "identity": row_identity(row),
        "document_id": clean(row.get("document_id")),
        "filename": clean(row.get("filename")),
        "status": clean(row.get("status")),
        "quality_score": numeric(row.get("quality_score")),
        "warning_count": numeric(row.get("warning_count")),
        "chunk_count": numeric(row.get("chunk_count")),
        "table_cell_row_count": numeric(row.get("table_cell_row_count")),
        "chunk_to_source_char_ratio": numeric(row.get("chunk_to_source_char_ratio")),
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


def clean(value: Any) -> str:
    return str(value or "").strip()


def markdown_report(result: dict[str, Any]) -> str:
    counts = result["counts"]
    lines = [
        "# Batch Quality Comparison",
        "",
        f"- Before rows: {counts['before_rows']}",
        f"- After rows: {counts['after_rows']}",
        f"- Added: {counts['added']}",
        f"- Removed: {counts['removed']}",
        f"- Metric changed: {counts['metric_changed']}",
        f"- Unchanged: {counts['unchanged']}",
        "",
        "## Metric Changed",
        "",
    ]
    if not result["metric_changed"]:
        lines.append("- None")
    for item in result["metric_changed"][:50]:
        changed = ", ".join(sorted(item["changes"].keys()))
        after = item["after"]
        lines.append(f"- `{item['identity']}` {after['filename']} ({changed})")
    lines.extend(["", "## Added", ""])
    lines.extend(row_lines(result["added"]))
    lines.extend(["", "## Removed", ""])
    lines.extend(row_lines(result["removed"]))
    return "\n".join(lines).strip() + "\n"


def row_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- None"]
    return [f"- `{row['identity']}` {row['filename']}" for row in rows]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare two batch quality reports by source identity.")
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--out-prefix", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = compare_reports(load_report(Path(args.before)), load_report(Path(args.after)))
    prefix = Path(args.out_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = prefix.with_suffix(".json")
    md_path = prefix.with_suffix(".md")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(markdown_report(result), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(md_path), "counts": result["counts"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
