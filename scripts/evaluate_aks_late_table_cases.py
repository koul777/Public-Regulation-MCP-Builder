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


RUN_ROOT = PROJECT_ROOT / "reports" / "overnight_runs" / "20260714-140648"
DEFAULT_CHUNKS_JSON = (
    RUN_ROOT
    / "tmp"
    / "aks_reindexed_eval"
    / "tenants"
    / "tenant-aks-publish"
    / "repository"
    / "doc_035798a12673_chunks.json"
)
DEFAULT_OUT_JSON = RUN_ROOT / "parsing" / "aks_late_table_cases_eval.json"
DEFAULT_OUT_MD = RUN_ROOT / "parsing" / "aks_late_table_cases_eval.md"


CASES: list[dict[str, Any]] = [
    {
        "case_id": "qualification",
        "case_label": "교수직 임용 자격 기준표",
        "chunk_id_fragment": "5580_p775_001",
        "required_row_tokens": ["교수직 임용 자격 기준표", "구 분 자 격"],
    },
    {
        "case_id": "career_conversion",
        "case_label": "연구직 경력기간 환산율표",
        "chunk_id_fragment": "7008_p1063_001",
        "required_row_tokens": ["연구직 경력기간 환산율표", "경 력 종 별 환산율"],
    },
    {
        "case_id": "account_title",
        "case_label": "계정과목 / 관항목해설",
        "chunk_id_fragment": "9937_p1494_001",
        "required_row_tokens": ["계 정 과 목", "관 항 목 해 설"],
    },
    {
        "case_id": "allowance",
        "case_label": "기타수당 지급 기준표",
        "chunk_id_fragment": "7015_p1068_001",
        "required_row_tokens": ["신분 및 복무 변동사항에 따른 기타수당 지급 기준표", "구 분 월정직책급 특정직무급 가족수당"],
    },
    {
        "case_id": "record_retention_continuation",
        "case_label": "기록물보존기간 기준표 연속 조각",
        "chunk_id_fragment": "p1254_002",
        "required_row_tokens": ["4-5-2 기록물관리규정", "3년 보존", "1년 보존"],
    },
    {
        "case_id": "recruitment_sanction",
        "case_label": "채용 비리 처리 기준",
        "chunk_id_fragment": "5968_p848_001",
        "required_row_tokens": ["채용 비리 처리 기준", "□ 채 용"],
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate AKS late-table cases with the current parser.")
    parser.add_argument("--chunks-json", type=Path, default=DEFAULT_CHUNKS_JSON)
    parser.add_argument("--out-json", type=Path, default=DEFAULT_OUT_JSON)
    parser.add_argument("--out-md", type=Path, default=DEFAULT_OUT_MD)
    return parser.parse_args()


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


def build_report(chunks_json: Path, out_json: Path, out_md: Path) -> dict[str, Any]:
    extractor = TableExtractor()
    chunks = load_chunks(chunks_json)
    generated_at = datetime.now(timezone.utc).isoformat()

    case_rows: list[dict[str, Any]] = []
    for case in CASES:
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
        "report_type": "aks_late_table_cases_eval",
        "generated_at": generated_at,
        "source_chunks_json": str(chunks_json),
        "case_count": len(case_rows),
        "status_counts": dict(status_counts),
        "classification_counts": dict(classification_counts),
        "review_flag_counts": dict(flag_counts),
        "handled_case_count": status_counts["handled"] + status_counts["handled_review_required"],
        "ambiguous_case_count": status_counts["ambiguous"] + status_counts["ambiguous_review_required"],
        "cases": case_rows,
        "artifacts": {
            "json": str(out_json),
            "markdown": str(out_md),
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
        "# AKS Late Table Case Evaluation",
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


def main() -> int:
    args = parse_args()
    report = build_report(args.chunks_json, args.out_json, args.out_md)
    print(json.dumps({"out_json": str(args.out_json), "out_md": str(args.out_md), "case_count": report["case_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
