from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def summarize_table_exports(
    batch_report: dict[str, Any],
    *,
    base_dir: Path | None = None,
    allow_missing_exports: bool = False,
) -> dict[str, Any]:
    base_dir = base_dir or Path.cwd()
    row_kind_counts: Counter[str] = Counter()
    classification_counts: Counter[str] = Counter()
    cell_count_histogram: Counter[str] = Counter()
    documents: list[dict[str, Any]] = []
    missing_exports: list[dict[str, Any]] = []
    total_rows = 0
    expected_table_cell_rows_total = 0
    actual_structured_table_rows_total = 0
    table_cell_row_mismatches: list[dict[str, Any]] = []

    for row in batch_report.get("rows", []) or []:
        raw_table_path = row.get("tables_jsonl") or ""
        table_path = resolve_path(raw_table_path, base_dir)
        expected_cell_rows = _to_int(row.get("table_cell_row_count"))
        expected_table_cell_rows_total += expected_cell_rows
        if not table_path:
            missing_exports.append(
                {
                    "document_id": row.get("document_id"),
                    "filename": row.get("filename"),
                    "tables_jsonl": raw_table_path,
                }
            )
            if not allow_missing_exports:
                continue
        table_rows = read_jsonl(table_path) if table_path else []
        row_count = len(table_rows)
        actual_structured_rows = sum(1 for item in table_rows if item.get("row_kind") == "cell")
        actual_structured_table_rows_total += actual_structured_rows
        delta = actual_structured_rows - expected_cell_rows
        if delta:
            table_cell_row_mismatches.append(
                {
                    "document_id": row.get("document_id"),
                    "filename": row.get("filename"),
                    "source_record_id": row.get("source_record_id"),
                    "source_file_id": row.get("source_file_id"),
                    "expected_table_cell_row_count": expected_cell_rows,
                    "actual_structured_table_row_count": actual_structured_rows,
                    "table_cell_row_delta": delta,
                    "tables_jsonl": raw_table_path,
                }
            )
        if row_count or expected_cell_rows:
            documents.append(
                {
                    "document_id": row.get("document_id"),
                    "filename": row.get("filename"),
                    "source_record_id": row.get("source_record_id"),
                    "source_file_id": row.get("source_file_id"),
                    "table_row_count": row_count,
                    "expected_table_cell_row_count": expected_cell_rows,
                    "structured_table_row_count": actual_structured_rows,
                    "table_cell_row_delta": delta,
                    "raw_table_row_count": sum(1 for item in table_rows if item.get("row_kind") == "raw"),
                    "max_cell_count": max((int(item.get("cell_count") or 0) for item in table_rows), default=0),
                }
            )
        for item in table_rows:
            total_rows += 1
            row_kind_counts[str(item.get("row_kind") or "unknown")] += 1
            classification = item.get("table_classification") or "unclassified"
            classification_counts[str(classification)] += 1
            cell_count_histogram[str(int(item.get("cell_count") or 0))] += 1

    documents.sort(key=lambda item: (item["table_row_count"], item.get("filename") or ""), reverse=True)
    if missing_exports and not allow_missing_exports:
        sample = ", ".join(str(item.get("filename") or item.get("document_id") or item.get("tables_jsonl")) for item in missing_exports[:5])
        raise FileNotFoundError(f"Missing table export files for {len(missing_exports)} documents: {sample}")
    return {
        "generated_from": batch_report.get("generated_at"),
        "input_count": int(batch_report.get("input_count") or 0),
        "document_count": len(batch_report.get("rows", []) or []),
        "documents_with_tables": len(documents),
        "table_row_count": total_rows,
        "expected_table_cell_row_count": expected_table_cell_rows_total,
        "actual_structured_table_row_count": actual_structured_table_rows_total,
        "table_cell_row_delta": actual_structured_table_rows_total - expected_table_cell_rows_total,
        "table_cell_row_mismatch_count": len(table_cell_row_mismatches),
        "table_cell_row_mismatch_samples": table_cell_row_mismatches[:20],
        "row_kind_counts": dict(sorted(row_kind_counts.items())),
        "table_classification_counts": dict(classification_counts.most_common()),
        "cell_count_histogram": dict(sorted(cell_count_histogram.items(), key=lambda item: int(item[0]))),
        "missing_export_count": len(missing_exports),
        "missing_exports": missing_exports,
        "documents": documents,
    }


def resolve_path(raw_path: str, base_dir: Path) -> Path | None:
    if not raw_path:
        return None
    path = Path(raw_path)
    if path.is_absolute():
        return path if path.exists() else None
    candidates = [path, base_dir / path]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def read_jsonl(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Table Export Summary",
        "",
        f"- Source generated_at: {report.get('generated_from')}",
        f"- Inputs: {report.get('input_count')}",
        f"- Documents: {report.get('document_count')}",
        f"- Documents with tables: {report.get('documents_with_tables')}",
        f"- Table rows: {report.get('table_row_count')}",
        f"- Expected table cell rows: {report.get('expected_table_cell_row_count', 0)}",
        f"- Actual structured table rows: {report.get('actual_structured_table_row_count', 0)}",
        f"- Table cell row mismatches: {report.get('table_cell_row_mismatch_count', 0)}",
        f"- Missing table exports: {report.get('missing_export_count', 0)}",
        "",
        "## Row Kinds",
        "",
    ]
    for key, value in (report.get("row_kind_counts") or {}).items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Table Classifications", ""])
    for key, value in (report.get("table_classification_counts") or {}).items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Cell Count Histogram", ""])
    for key, value in (report.get("cell_count_histogram") or {}).items():
        lines.append(f"- {key}: {value}")
    if report.get("table_cell_row_mismatch_count"):
        lines.extend(["", "## Table Cell Row Mismatches", ""])
        for item in report.get("table_cell_row_mismatch_samples") or []:
            lines.append(
                f"- {item.get('filename')}: expected={item.get('expected_table_cell_row_count')}, "
                f"actual={item.get('actual_structured_table_row_count')}, delta={item.get('table_cell_row_delta')}"
            )
    lines.extend(["", "## Top Documents", ""])
    for item in (report.get("documents") or [])[:20]:
        lines.append(
            f"- {item.get('table_row_count')}: {item.get('filename')} "
            f"(expected_cells={item.get('expected_table_cell_row_count')}, "
            f"structured={item.get('structured_table_row_count')}, raw={item.get('raw_table_row_count')}, "
            f"max_cells={item.get('max_cell_count')})"
        )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize table-only exports referenced by a batch quality report.")
    parser.add_argument("--batch-report", required=True)
    parser.add_argument("--base-dir", default=".")
    parser.add_argument("--out-prefix", required=True)
    parser.add_argument("--allow-missing-exports", action="store_true", help="Summarize available table exports and report missing paths instead of failing.")
    parser.add_argument("--fail-on-mismatch", action="store_true", help="Exit non-zero when exported structured table rows differ from batch table_cell_row_count.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = summarize_table_exports(
            load_json(Path(args.batch_report)),
            base_dir=Path(args.base_dir),
            allow_missing_exports=args.allow_missing_exports,
        )
    except Exception as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    out_prefix = Path(args.out_prefix)
    json_path = out_prefix.with_suffix(".json")
    md_path = out_prefix.with_suffix(".md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, md_path)
    print(
        json.dumps(
            {
                "json": str(json_path),
                "markdown": str(md_path),
                "table_row_count": report["table_row_count"],
                "table_cell_row_mismatch_count": report["table_cell_row_mismatch_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 2 if args.fail_on_mismatch and report["table_cell_row_mismatch_count"] else 0


def _to_int(value: Any) -> int:
    if isinstance(value, bool) or value in (None, ""):
        return 0
    try:
        return int(float(str(value)))
    except ValueError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
