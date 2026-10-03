from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.processors.table_extractor import TableExtractor


# Case definitions describe one private regulation corpus (chunk id fragments,
# page numbers, table titles), so they are never kept in public source. The
# operator passes them as a JSON file next to the private runtime data:
#
#   [
#     {
#       "case_id": "allowance",
#       "case_label": "수당 지급 기준표",
#       "chunk_id_fragment": "p0123_001",
#       "required_row_tokens": ["수당 지급 기준표", "구 분"]
#     }
#   ]
CASE_REQUIRED_FIELDS = ("case_id", "case_label", "chunk_id_fragment", "required_row_tokens")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate flattened late-table cases of one processed regulation corpus with the current parser. "
            "All inputs and outputs are explicit so no private runtime path is built into public source."
        )
    )
    parser.add_argument("--chunks-json", type=Path, required=True, help="Processed *_chunks.json of the corpus.")
    parser.add_argument("--cases-json", type=Path, required=True, help="JSON array of case definitions.")
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    return parser.parse_args(argv)


def load_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"{path} must contain a non-empty JSON array of cases.")
    cases: list[dict[str, Any]] = []
    for index, case in enumerate(payload):
        if not isinstance(case, dict):
            raise ValueError(f"Case #{index} in {path} must be an object.")
        missing = [field for field in CASE_REQUIRED_FIELDS if not case.get(field)]
        if missing:
            raise ValueError(f"Case #{index} in {path} is missing: {', '.join(missing)}.")
        tokens = case["required_row_tokens"]
        if not isinstance(tokens, list) or not all(isinstance(token, str) and token for token in tokens):
            raise ValueError(f"Case #{index} in {path}: required_row_tokens must be a list of strings.")
        cases.append({field: case[field] for field in CASE_REQUIRED_FIELDS})
    return cases


def load_chunks(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list):
        raise ValueError(f"{path} must contain a JSON array of chunks.")
    return [chunk for chunk in payload if isinstance(chunk, dict)]


def normalize_rows(value: Any) -> list[str]:
    rows: list[str] = []
    if not isinstance(value, list):
        return rows
    for item in value:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = str(item.get("raw") or " ".join(str(cell).strip() for cell in item.get("cells") or [] if str(cell).strip())).strip()
        else:
            text = str(item).strip()
        if text:
            rows.append(text)
    return rows


def chunk_text(chunk: dict[str, Any]) -> tuple[list[str], str]:
    metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
    rows = normalize_rows(metadata.get("table_rows"))
    return rows, "\n".join(rows)


def find_case_chunk(chunks: list[dict[str, Any]], case: dict[str, Any]) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    fragment = str(case["chunk_id_fragment"])
    required_row_tokens = [str(token) for token in case["required_row_tokens"]]

    for chunk in chunks:
        chunk_id = str(chunk.get("chunk_id") or "")
        if fragment not in chunk_id:
            continue
        rows, text = chunk_text(chunk)
        if not rows:
            continue
        if all(token in text for token in required_row_tokens):
            matches.append(chunk)

    if not matches:
        raise ValueError(f"No chunk matched case {case['case_id']} ({fragment}).")
    if len(matches) > 1:
        matches.sort(key=lambda item: str(item.get("chunk_id") or ""))
    return matches[0]


def classify_case(analysis: dict[str, Any]) -> str:
    if analysis.get("table_like") and analysis.get("table_cell_rows"):
        if analysis.get("table_review_required"):
            return "handled_review_required"
        return "handled"
    if analysis.get("table_classification") in {"probable_table_extraction_failed", "probable_false_positive_org_chart"}:
        return "ambiguous"
    if analysis.get("table_review_required"):
        return "ambiguous_review_required"
    return "ambiguous"


def build_report(chunks_json: Path, cases_json: Path, out_json: Path, out_md: Path) -> dict[str, Any]:
    extractor = TableExtractor()
    chunks = load_chunks(chunks_json)
    cases = load_cases(cases_json)
    generated_at = datetime.now(timezone.utc).isoformat()

    case_rows: list[dict[str, Any]] = []
    for case in cases:
        chunk = find_case_chunk(chunks, case)
        rows, text = chunk_text(chunk)
        analysis = extractor.analyze_text(text, chunk.get("chunk_type"))
        status = classify_case(analysis)
        metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
        case_rows.append(
            {
                "case_id": case["case_id"],
                "case_label": case["case_label"],
                "chunk_id": chunk.get("chunk_id") or "",
                "chunk_type": chunk.get("chunk_type") or "",
                "source_page_start": metadata.get("source_page_start", ""),
                "source_page_end": metadata.get("source_page_end", ""),
                "row_count": len(rows),
                "raw_preview": rows[:5],
                "table_like": bool(analysis.get("table_like")),
                "table_classification": analysis.get("table_classification") or "",
                "table_review_required": bool(analysis.get("table_review_required")),
                "table_review_reason": analysis.get("table_review_reason") or "",
                "table_review_flags": list(analysis.get("table_review_flags") or []),
                "table_structured_row_count": int(analysis.get("table_structured_row_count") or 0),
                "table_column_count": int(analysis.get("table_column_count") or 0),
                "table_record_count": int(analysis.get("table_record_count") or 0),
                "table_cell_row_count": len(analysis.get("table_cell_rows") or []),
                "status": status,
                "cell_rows_preview": (analysis.get("table_cell_rows") or [])[:4],
            }
        )

    status_counts = Counter(row["status"] for row in case_rows)
    classification_counts = Counter(row["table_classification"] for row in case_rows)
    flag_counts = Counter(flag for row in case_rows for flag in row["table_review_flags"])

    report = {
        "report_type": "late_table_cases_eval",
        "generated_at": generated_at,
        "source_chunks_json": chunks_json.name,
        "source_cases_json": cases_json.name,
        "case_count": len(case_rows),
        "status_counts": dict(status_counts),
        "classification_counts": dict(classification_counts),
        "review_flag_counts": dict(flag_counts),
        "handled_case_count": status_counts["handled"] + status_counts["handled_review_required"],
        "ambiguous_case_count": status_counts["ambiguous"] + status_counts["ambiguous_review_required"],
        "cases": case_rows,
        "artifacts": {
            "json": out_json.name,
            "markdown": out_md.name,
        },
        "safety_note": (
            "Read-only evaluation artifact. It does not modify parser code, approve chunks, or change indexes."
        ),
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(render_markdown(report), encoding="utf-8")
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Late Table Case Evaluation",
        "",
        f"- Generated at: {report['generated_at']}",
        f"- Source chunks: `{report['source_chunks_json']}`",
        f"- Cases evaluated: {report['case_count']}",
        f"- Handled cases: {report['handled_case_count']}",
        f"- Ambiguous cases: {report['ambiguous_case_count']}",
        "",
        "## Status Summary",
        "",
        "| Status | Count |",
        "| --- | ---: |",
    ]
    for status, count in sorted(report["status_counts"].items()):
        lines.append(f"| {md(status)} | {int(count):,} |")

    lines.extend(
        [
            "",
            "## Case Summary",
            "",
            "| Case | Chunk | Status | Classification | Rows | Cells | Cols | Flags |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for case in report["cases"]:
        lines.append(
            "| {case} | {chunk} | {status} | {classification} | {rows} | {cells} | {cols} | {flags} |".format(
                case=md(case["case_label"]),
                chunk=md(case["chunk_id"]),
                status=md(case["status"]),
                classification=md(case["table_classification"]),
                rows=int(case["row_count"]),
                cells=int(case["table_cell_row_count"]),
                cols=int(case["table_column_count"]),
                flags=md("; ".join(case["table_review_flags"]) or "-"),
            )
        )

    for case in report["cases"]:
        lines.extend(
            [
                "",
                f"## {case['case_label']}",
                "",
                f"- Case id: `{case['case_id']}`",
                f"- Chunk id: `{case['chunk_id']}`",
                f"- Status: `{case['status']}`",
                f"- Parser classification: `{case['table_classification']}`",
                f"- Review required: `{str(case['table_review_required']).lower()}`",
                f"- Review reason: {case['table_review_reason'] or '-'}",
                f"- Review flags: {', '.join(case['table_review_flags']) or '-'}",
                f"- Raw row count: {case['row_count']}",
                f"- Structured cell row count: {case['table_cell_row_count']}",
                f"- Column count: {case['table_column_count']}",
                "",
                "### Raw Preview",
                "",
            ]
        )
        for row in case["raw_preview"]:
            lines.append(f"- {row}")
        if case["cell_rows_preview"]:
            lines.extend(["", "### Cell Preview", ""])
            for row in case["cell_rows_preview"]:
                lines.append(f"- {json.dumps(row, ensure_ascii=False)}")

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- `handled` means the current parser reconstructed structured cell rows from the flattened table text.",
            "- `handled_review_required` means reconstruction succeeded, but the parser still kept review flags.",
            "- `ambiguous` means the current parser did not produce a reliable structured table from the flattened rows.",
        ]
    )
    return "\n".join(lines) + "\n"


def md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(args.chunks_json, args.cases_json, args.out_json, args.out_md)
    print(json.dumps({"out_json": str(args.out_json), "out_md": str(args.out_md), "case_count": report["case_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
