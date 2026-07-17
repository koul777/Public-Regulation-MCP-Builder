from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.export_vectordb_ingestion import _iter_batch_chunks, load_json


def load_chunks_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load chunk records from JSONL or the repository's JSON-array export."""
    raw = path.read_text(encoding="utf-8-sig")
    stripped = raw.lstrip()
    if stripped.startswith("["):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid chunk JSON array at {path}: {exc}") from exc
        if not isinstance(payload, list):
            raise ValueError(f"Chunk JSON array must contain a list at {path}")
        return [item for item in payload if isinstance(item, dict)]

    chunks: list[dict[str, Any]] = []
    for line_no, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid chunk JSONL at {path}:{line_no}: {exc}") from exc
        if isinstance(item, dict):
            chunks.append(item)
    return chunks


def summarize_law_references(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    internal_counts: Counter[str] = Counter()
    external_counts: Counter[str] = Counter()
    article_counts: Counter[str] = Counter()
    chunks_with_internal = 0
    chunks_with_external = 0
    revision_span_count = 0
    override_window_count = 0
    for chunk in chunks:
        internal_refs = chunk.get("internal_regulation_refs") or chunk.get("metadata", {}).get("internal_regulation_refs") or []
        external_refs = chunk.get("external_law_refs") or chunk.get("metadata", {}).get("external_law_refs") or []
        article_refs = chunk.get("article_refs") or chunk.get("metadata", {}).get("article_refs") or []
        revision_spans = chunk.get("revision_history_spans") or chunk.get("metadata", {}).get("revision_history_spans") or []
        validity_windows = chunk.get("article_validity_windows") or chunk.get("metadata", {}).get("article_validity_windows") or []
        if internal_refs:
            chunks_with_internal += 1
            internal_counts.update(internal_refs)
        if external_refs:
            chunks_with_external += 1
            external_counts.update(external_refs)
        if article_refs:
            article_counts.update(article_refs)
        revision_span_count += len(revision_spans)
        override_window_count += sum(1 for item in validity_windows if item.get("source") == "article_effective_override")
    return {
        "chunk_count": len(chunks),
        "chunks_with_internal_regulation_refs": chunks_with_internal,
        "chunks_with_external_law_refs": chunks_with_external,
        "unique_internal_regulation_ref_count": len(internal_counts),
        "unique_external_law_ref_count": len(external_counts),
        "unique_article_ref_count": len(article_counts),
        "revision_history_span_count": revision_span_count,
        "article_override_window_count": override_window_count,
        "top_internal_regulation_refs": internal_counts.most_common(20),
        "top_external_law_refs": external_counts.most_common(20),
        "top_article_refs": article_counts.most_common(20),
    }


def export_law_reference_report(
    *,
    chunks_jsonl: Path | None = None,
    batch_report_path: Path | None = None,
    out_json: Path,
    out_md: Path | None = None,
) -> dict[str, Any]:
    if chunks_jsonl is None and batch_report_path is None:
        raise ValueError("Provide either chunks_jsonl or batch_report_path.")
    if chunks_jsonl is not None and batch_report_path is not None:
        raise ValueError("Provide only one of chunks_jsonl or batch_report_path.")
    if batch_report_path is not None:
        return export_batch_law_reference_report(
            batch_report_path=batch_report_path,
            out_json=out_json,
            out_md=out_md,
        )
    chunks = load_chunks_jsonl(chunks_jsonl)
    summary = summarize_law_references(chunks)
    report = {
        "report_type": "law_reference_report",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_chunks_jsonl": str(chunks_jsonl),
        "source_batch_report_file": None,
        "api_call_count": 0,
        "summary": summary,
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if out_md:
        out_md.write_text(_to_markdown(report), encoding="utf-8")
        report["out_md"] = str(out_md)
    report["out_json"] = str(out_json)
    return report


def export_batch_law_reference_report(
    *,
    batch_report_path: Path,
    out_json: Path,
    out_md: Path | None = None,
) -> dict[str, Any]:
    batch_report = load_json(batch_report_path)
    chunks = list(_iter_batch_chunks(batch_report, batch_report_path=batch_report_path))
    summary = summarize_law_references(chunks)
    documents: list[dict[str, Any]] = []
    for row in batch_report.get("rows", []) or []:
        if row.get("status") not in {"completed", "skipped_unchanged"}:
            continue
        document_id = str(row.get("document_id") or "")
        if not document_id:
            continue
        document_chunks = [chunk for chunk in chunks if str(chunk.get("document_id") or "") == document_id]
        if not document_chunks:
            continue
        document_summary = summarize_law_references(document_chunks)
        documents.append(
            {
                "document_id": document_id,
                "filename": row.get("filename"),
                "source_record_id": row.get("source_record_id"),
                "chunk_count": document_summary["chunk_count"],
                "chunks_with_internal_regulation_refs": document_summary["chunks_with_internal_regulation_refs"],
                "chunks_with_external_law_refs": document_summary["chunks_with_external_law_refs"],
                "unique_internal_regulation_ref_count": document_summary["unique_internal_regulation_ref_count"],
                "unique_external_law_ref_count": document_summary["unique_external_law_ref_count"],
                "revision_history_span_count": document_summary["revision_history_span_count"],
            }
        )
    report = {
        "report_type": "law_reference_report",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_chunks_jsonl": None,
        "source_batch_report_file": batch_report_path.name,
        "source_batch_generated_at": batch_report.get("generated_at"),
        "input_count": batch_report.get("input_count", 0),
        "successful_count": batch_report.get("successful_count", 0),
        "document_count": len(documents),
        "api_call_count": 0,
        "summary": summary,
        "documents": documents,
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if out_md:
        out_md.write_text(_to_markdown(report), encoding="utf-8")
        report["out_md"] = str(out_md)
    report["out_json"] = str(out_json)
    return report


def _to_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    lines = [
        "# Law Reference Report",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Chunks: {summary.get('chunk_count', 0)}",
        f"- Chunks with internal regulation refs: {summary.get('chunks_with_internal_regulation_refs', 0)}",
        f"- Chunks with external law refs: {summary.get('chunks_with_external_law_refs', 0)}",
        f"- Revision history spans: {summary.get('revision_history_span_count', 0)}",
        f"- Article override windows: {summary.get('article_override_window_count', 0)}",
        "",
        "## Top external law refs",
    ]
    for name, count in summary.get("top_external_law_refs", []):
        lines.append(f"- {name}: {count}")
    lines.extend(["", "## Top internal regulation refs"])
    for name, count in summary.get("top_internal_regulation_refs", []):
        lines.append(f"- {name}: {count}")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize internal/external law references from chunk JSONL or batch report.")
    parser.add_argument("--chunks-jsonl", default=None)
    parser.add_argument("--batch-report", default=None)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.chunks_jsonl and not args.batch_report:
        raise SystemExit("Provide --chunks-jsonl or --batch-report.")
    if args.chunks_jsonl and args.batch_report:
        raise SystemExit("Provide only one of --chunks-jsonl or --batch-report.")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_json = Path(args.out_json) if args.out_json else Path("reports") / f"law_reference_report_{timestamp}.json"
    out_md = Path(args.out_md) if args.out_md else out_json.with_suffix(".md")
    report = export_law_reference_report(
        chunks_jsonl=Path(args.chunks_jsonl) if args.chunks_jsonl else None,
        batch_report_path=Path(args.batch_report) if args.batch_report else None,
        out_json=out_json,
        out_md=out_md,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    try:
        print(payload)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(payload.encode("utf-8"))
        sys.stdout.buffer.write(b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
