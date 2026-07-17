from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.export_vectordb_ingestion import _iter_batch_chunks, load_json


BRIDGE_RELATION_TYPES = {
    "article_cites_regulation_article",
    "chunk_cites_regulation_article",
    "article_cites_law_article",
    "chunk_cites_law_article",
}


def summarize_relation_bridges(
    edges: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    *,
    limit: int = 80,
) -> dict[str, Any]:
    chunk_index = {str(chunk.get("chunk_id") or ""): chunk for chunk in chunks if chunk.get("chunk_id")}
    bridge_edges = [
        edge
        for edge in edges
        if str(edge.get("relation_type") or "") in BRIDGE_RELATION_TYPES
    ]
    relation_type_counts = Counter(str(edge.get("relation_type") or "") for edge in bridge_edges)
    target_counts = Counter(str(edge.get("target_label") or "") for edge in bridge_edges if edge.get("target_label"))
    source_document_counts = Counter(str(edge.get("document_id") or "") for edge in bridge_edges if edge.get("document_id"))
    samples = bridge_samples(bridge_edges, chunk_index, limit=limit)
    return {
        "report_type": "relation_bridge_summary",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bridge_edge_count": len(bridge_edges),
        "relation_type_counts": dict(relation_type_counts),
        "unique_target_count": len(target_counts),
        "top_targets": [{"target_label": label, "count": count} for label, count in target_counts.most_common(30)],
        "source_document_counts": dict(source_document_counts),
        "sample_count": len(samples),
        "samples": samples,
    }


def bridge_samples(
    edges: list[dict[str, Any]],
    chunk_index: dict[str, dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    priority = {
        "article_cites_regulation_article": 0,
        "chunk_cites_regulation_article": 1,
        "article_cites_law_article": 2,
        "chunk_cites_law_article": 3,
    }
    sorted_edges = sorted(
        edges,
        key=lambda edge: (
            priority.get(str(edge.get("relation_type") or ""), 9),
            str(edge.get("source_label") or ""),
            str(edge.get("target_label") or ""),
            str(edge.get("evidence_text") or ""),
        ),
    )
    seen: set[tuple[str, str, str, str]] = set()
    samples: list[dict[str, Any]] = []
    for edge in sorted_edges:
        key = (
            str(edge.get("relation_type") or ""),
            str(edge.get("source_label") or ""),
            str(edge.get("target_label") or ""),
            str(edge.get("evidence_text") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        chunk = chunk_index.get(str(edge.get("chunk_id") or "")) or {}
        samples.append(
            {
                "relation_type": edge.get("relation_type"),
                "source_document": chunk.get("document_name"),
                "institution_name": chunk.get("institution_name"),
                "source_label": edge.get("source_label"),
                "target_label": edge.get("target_label"),
                "evidence_text": edge.get("evidence_text"),
                "confidence": edge.get("confidence"),
                "chunk_id": edge.get("chunk_id"),
                "page_start": edge.get("source_page_start") or chunk.get("source_page_start"),
                "snippet": snippet(str(chunk.get("normalized_text") or chunk.get("text") or chunk.get("retrieval_text") or "")),
            }
        )
        if len(samples) >= limit:
            break
    return samples


def load_relation_edges(path: Path) -> list[dict[str, Any]]:
    edges = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            edge = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid relation graph JSONL at {path}:{line_no}: {exc}") from exc
        if isinstance(edge, dict):
            edges.append(edge)
    return edges


def load_batch_chunks(batch_report_paths: list[Path]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for path in batch_report_paths:
        batch_report = load_json(path)
        chunks.extend(_iter_batch_chunks(batch_report, batch_report_path=path))
    return chunks


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Relation Bridge Summary",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Bridge edges: {report.get('bridge_edge_count')}",
        f"- Unique targets: {report.get('unique_target_count')}",
        "",
        "## Relation Types",
        "",
    ]
    for relation_type, count in sorted((report.get("relation_type_counts") or {}).items()):
        lines.append(f"- {relation_type}: {count}")
    lines.extend(["", "## Top Targets", ""])
    for item in report.get("top_targets") or []:
        lines.append(f"- {escape_md(str(item.get('target_label') or ''))}: {item.get('count')}")
    lines.extend(
        [
            "",
            "## Samples",
            "",
            "| Type | Institution | Source Document | Source | Target | Evidence | Page | Snippet |",
            "|---|---|---|---|---|---|---:|---|",
        ]
    )
    for sample in report.get("samples") or []:
        lines.append(
            "| {type} | {institution} | {doc} | {source} | {target} | {evidence} | {page} | {snippet} |".format(
                type=escape_md(str(sample.get("relation_type") or "")),
                institution=escape_md(str(sample.get("institution_name") or "")),
                doc=escape_md(str(sample.get("source_document") or "")),
                source=escape_md(str(sample.get("source_label") or "")),
                target=escape_md(str(sample.get("target_label") or "")),
                evidence=escape_md(str(sample.get("evidence_text") or "")),
                page=sample.get("page_start") or "",
                snippet=escape_md(str(sample.get("snippet") or "")),
            )
        )
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def snippet(text: str, *, max_chars: int = 180) -> str:
    return re.sub(r"\s+", " ", text).strip()[:max_chars]


def escape_md(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize cross-regulation and law-article bridge edges.")
    parser.add_argument("--relation-graph", required=True)
    parser.add_argument("--batch-report", action="append", required=True)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--limit", type=int, default=80)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_json = Path(args.out_json) if args.out_json else Path("reports") / f"relation_bridge_summary_{timestamp}.json"
    out_md = Path(args.out_md) if args.out_md else out_json.with_suffix(".md")
    edges = load_relation_edges(Path(args.relation_graph))
    chunks = load_batch_chunks([Path(path) for path in args.batch_report])
    report = summarize_relation_bridges(edges, chunks, limit=args.limit)
    report["source_relation_graph_file"] = Path(args.relation_graph).name
    report["source_batch_report_files"] = [Path(path).name for path in args.batch_report]
    report["out_json"] = str(out_json)
    report["out_md"] = str(out_md)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(out_md, report)
    print(
        json.dumps(
            {
                "ok": True,
                "bridge_edge_count": report["bridge_edge_count"],
                "unique_target_count": report["unique_target_count"],
                "sample_count": report["sample_count"],
                "out_json": str(out_json),
                "out_md": str(out_md),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
