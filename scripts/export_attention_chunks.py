from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.compare_batch_quality_reports import row_identity


PRIVATE_USE_PATTERN = re.compile(r"[\ue000-\uf8ff]")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_chunks(repository_dir: Path, document_id: str) -> list[dict[str, Any]]:
    path = repository_dir / f"{document_id}_chunks.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8-sig"))


def attention_report(
    batch_report: dict[str, Any],
    repository_dir: Path,
    max_chunks_per_doc: int = 5,
) -> dict[str, Any]:
    documents: list[dict[str, Any]] = []
    classification_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    signal_counts: Counter[str] = Counter()
    attention_signal_counts: Counter[str] = Counter()
    stable_signal_counts: Counter[str] = Counter()
    max_signal_samples: dict[str, dict[str, Any]] = {}

    for row in batch_report.get("rows", []) or []:
        document_id = row.get("document_id")
        if not document_id:
            continue
        chunks = load_chunks(repository_dir, document_id)
        samples: list[dict[str, Any]] = []
        doc_signal_counts: Counter[str] = Counter()
        for chunk in chunks:
            signals = chunk_attention_signals(chunk)
            if not signals:
                continue
            for signal in signals:
                doc_signal_counts[signal] += 1
                signal_counts[signal] += 1
                if is_stable_signal(signal):
                    stable_signal_counts[signal] += 1
                else:
                    attention_signal_counts[signal] += 1
            metadata = chunk.get("metadata") or {}
            classification = metadata.get("table_classification") or ""
            reason = metadata.get("table_review_reason") or ""
            if classification:
                classification_counts[classification] += 1
            if reason:
                reason_counts[reason] += 1
            sample = chunk_sample(chunk, signals)
            collect_max_signal_samples(max_signal_samples, sample)
            if len(samples) < max_chunks_per_doc:
                samples.append(sample)

        if samples or row_needs_attention(row):
            documents.append(
                {
                    "identity": row_identity(row),
                    "document_id": document_id,
                    "filename": row.get("filename"),
                    "source_url": row.get("source_url"),
                    "quality_score": numeric(row.get("quality_score")),
                    "failed_info_check_count": int(row.get("failed_info_check_count") or 0),
                    "recommendation_count": int(row.get("recommendation_count") or 0),
                    "top_recommendation": row.get("top_recommendation") or "",
                    "probable_table_false_positive_chunks": int(row.get("probable_table_false_positive_chunks") or 0),
                    "stable_table_false_positive_chunks": int(row.get("stable_table_false_positive_chunks") or 0),
                    "table_false_positive_attention_chunks": int(row.get("table_false_positive_attention_chunks") or 0),
                    "probable_table_extraction_failed_chunks": int(row.get("probable_table_extraction_failed_chunks") or 0),
                    "attention_signal_count": sum(
                        count for signal, count in doc_signal_counts.items() if not is_stable_signal(signal)
                    ),
                    "stable_signal_count": sum(
                        count for signal, count in doc_signal_counts.items() if is_stable_signal(signal)
                    ),
                    "signal_counts": dict(sorted(doc_signal_counts.items())),
                    "sample_count": len(samples),
                    "samples": samples,
                }
            )

    documents.sort(
        key=lambda item: (
            item["probable_table_extraction_failed_chunks"],
            item["table_false_positive_attention_chunks"],
            item["stable_table_false_positive_chunks"],
            item["recommendation_count"],
            item["failed_info_check_count"],
        ),
        reverse=True,
    )
    return {
        "generated_from": batch_report.get("generated_at"),
        "input_count": batch_report.get("input_count"),
        "document_count": len(documents),
        "signal_counts": dict(sorted(signal_counts.items())),
        "attention_signal_counts": dict(sorted(attention_signal_counts.items())),
        "stable_signal_counts": dict(sorted(stable_signal_counts.items())),
        "attention_document_count": sum(1 for document in documents if document.get("attention_signal_count", 0) > 0),
        "stable_document_count": sum(1 for document in documents if document.get("stable_signal_count", 0) > 0),
        "table_classification_counts": dict(classification_counts.most_common()),
        "table_review_reason_counts": dict(reason_counts.most_common()),
        "max_signal_samples": [
            sample
            for _, sample in sorted(
                max_signal_samples.items(),
                key=lambda item: (item[1].get("watch_score", []), item[0]),
                reverse=True,
            )
        ],
        "documents": documents,
    }


def row_needs_attention(row: dict[str, Any]) -> bool:
    return any(
        int(row.get(key) or 0) > 0
        for key in (
            "failed_info_check_count",
            "recommendation_count",
            "table_false_positive_attention_chunks",
            "probable_table_extraction_failed_chunks",
            "table_like_without_cell_rows",
        )
    )


def is_stable_signal(signal: str) -> bool:
    return signal == "table_stable_false_positive"


def chunk_attention_signals(chunk: dict[str, Any]) -> list[str]:
    metadata = chunk.get("metadata") or {}
    signals: list[str] = []
    if metadata.get("table_probable_false_positive"):
        stability = metadata.get("table_false_positive_stability")
        signals.append("table_stable_false_positive" if stability == "stable" else "table_probable_false_positive")
    if metadata.get("table_probable_extraction_failed"):
        signals.append("table_probable_extraction_failed")
    if metadata.get("table_like") and not metadata.get("table_cell_rows"):
        signals.append("table_like_without_cell_rows")
    text = chunk.get("normalized_text") or chunk.get("text") or ""
    if PRIVATE_USE_PATTERN.search(text):
        signals.append("private_use_text")
    if "structure_fallback_document_chunk" in (chunk.get("warnings") or []) or metadata.get("structure_fallback"):
        signals.append("structure_fallback")
    return signals


def chunk_sample(chunk: dict[str, Any], signals: list[str]) -> dict[str, Any]:
    metadata = chunk.get("metadata") or {}
    text = chunk.get("normalized_text") or chunk.get("text") or ""
    table_rows = metadata.get("table_rows") or []
    return {
        "chunk_id": chunk.get("chunk_id"),
        "chunk_type": chunk.get("chunk_type"),
        "signals": signals,
        "hierarchy_path": metadata.get("hierarchy_path"),
        "table_classification": metadata.get("table_classification"),
        "table_review_reason": metadata.get("table_review_reason"),
        "table_false_positive_stability": metadata.get("table_false_positive_stability"),
        "table_confidence": metadata.get("table_confidence"),
        "table_header_hits": metadata.get("table_header_hits"),
        "table_numeric_rows": metadata.get("table_numeric_rows"),
        "table_delimiter_rows": metadata.get("table_delimiter_rows"),
        "table_row_count": len(table_rows) if isinstance(table_rows, list) else 0,
        "watch_score": watch_score(metadata, text),
        "snippet": snippet(text),
    }


def collect_max_signal_samples(max_signal_samples: dict[str, dict[str, Any]], sample: dict[str, Any]) -> None:
    classification = sample.get("table_classification") or "unclassified"
    for signal in sample.get("signals") or []:
        key = f"{signal}:{classification}"
        current = max_signal_samples.get(key)
        if current is None or tuple(sample["watch_score"]) > tuple(current["watch_score"]):
            copy = dict(sample)
            copy["watch_key"] = key
            max_signal_samples[key] = copy


def watch_score(metadata: dict[str, Any], text: str) -> list[float]:
    return [
        numeric(metadata.get("table_confidence")),
        numeric(metadata.get("table_header_hits")),
        numeric(metadata.get("table_numeric_rows")),
        numeric(metadata.get("table_delimiter_rows")),
        float(len(text)),
    ]


def snippet(text: str, max_chars: int = 500) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


def numeric(value: Any) -> float:
    if isinstance(value, bool) or value in ("", None):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return 0.0


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Attention Chunk Report",
        "",
        f"- Source generated_at: {report.get('generated_from')}",
        f"- Input count: {report.get('input_count')}",
        f"- Documents with any signals: {report.get('document_count')}",
        f"- Documents with attention signals: {report.get('attention_document_count', 0)}",
        f"- Documents with stable signals: {report.get('stable_document_count', 0)}",
        "",
        "## Attention Signal Counts",
        "",
    ]
    if report.get("attention_signal_counts"):
        for key, value in report["attention_signal_counts"].items():
            lines.append(f"- {key}: {value}")
    else:
        lines.append("- None")
    lines.extend(["", "## Stable Signal Counts", ""])
    if report.get("stable_signal_counts"):
        for key, value in report["stable_signal_counts"].items():
            lines.append(f"- {key}: {value}")
    else:
        lines.append("- None")
    lines.extend(["", "## All Signal Counts", ""])
    if report["signal_counts"]:
        for key, value in report["signal_counts"].items():
            lines.append(f"- {key}: {value}")
    else:
        lines.append("- None")
    lines.extend(["", "## Table Classifications", ""])
    if report["table_classification_counts"]:
        for key, value in report["table_classification_counts"].items():
            lines.append(f"- {key}: {value}")
    else:
        lines.append("- None")
    lines.extend(["", "## Max Signal Samples", ""])
    if report.get("max_signal_samples"):
        for sample in report["max_signal_samples"]:
            lines.append(
                f"- `{sample['chunk_id']}` key={sample.get('watch_key')} "
                f"score={sample.get('watch_score')} class={sample.get('table_classification') or ''} "
                f"reason={sample.get('table_review_reason') or ''}"
            )
            lines.append(f"  - {sample['snippet']}")
    else:
        lines.append("- None")
    lines.extend(["", "## Documents", ""])
    for document in report["documents"]:
        lines.append(
            f"### {document['identity']} {document.get('filename') or ''}".rstrip()
        )
        lines.append(
            f"- score={document['quality_score']} info={document['failed_info_check_count']} "
            f"recommendations={document['recommendation_count']} false_positive={document['probable_table_false_positive_chunks']} "
            f"stable_false_positive={document.get('stable_table_false_positive_chunks', 0)} "
            f"attention_false_positive={document.get('table_false_positive_attention_chunks', 0)} "
            f"extraction_failed={document['probable_table_extraction_failed_chunks']}"
        )
        if document.get("top_recommendation"):
            lines.append(f"- top: {document['top_recommendation']}")
        for sample in document["samples"]:
            lines.append(
                f"- `{sample['chunk_id']}` signals={','.join(sample['signals'])} "
                f"class={sample.get('table_classification') or ''} reason={sample.get('table_review_reason') or ''} "
                f"stability={sample.get('table_false_positive_stability') or ''} "
                f"confidence={sample.get('table_confidence')} header={sample.get('table_header_hits')} "
                f"numeric={sample.get('table_numeric_rows')} delimiter={sample.get('table_delimiter_rows')}"
            )
            lines.append(f"  - {sample['snippet']}")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export chunk-level attention samples from a batch quality report.")
    parser.add_argument("--batch-report", required=True)
    parser.add_argument("--repository-dir", default="data/repository")
    parser.add_argument("--out-prefix", required=True)
    parser.add_argument("--max-chunks-per-doc", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = attention_report(
        load_json(Path(args.batch_report)),
        Path(args.repository_dir),
        max_chunks_per_doc=args.max_chunks_per_doc,
    )
    out_prefix = Path(args.out_prefix)
    json_path = out_prefix.with_suffix(".json")
    md_path = out_prefix.with_suffix(".md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, md_path)
    print(json.dumps({"json": str(json_path), "markdown": str(md_path), "document_count": report["document_count"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
