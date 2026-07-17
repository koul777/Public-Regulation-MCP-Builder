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

from app.processors.chunker import Chunker
from app.schemas.chunk import Chunk


TEMPORAL_FIELDS = (
    "effective_date",
    "revision_date",
    "valid_from",
    "valid_to",
    "revision_history",
    "revision_history_spans",
    "article_effective_overrides",
    "article_validity_windows",
    "supplementary_identifier_date",
)


def backfill_temporal_metadata(
    chunks: Iterable[dict[str, Any]],
    *,
    source_label: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    before_rows = [dict(chunk) for chunk in chunks]
    before = summarize_temporal_metadata(before_rows)
    chunk_models = [Chunk.model_validate(chunk) for chunk in before_rows]
    Chunker()._inherit_temporal_metadata_from_chunks(chunk_models)
    after_rows = [chunk.model_dump(mode="json") for chunk in chunk_models]
    after = summarize_temporal_metadata(after_rows)
    manifest = {
        "report_type": "temporal_metadata_backfill",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_label": source_label,
        "input_count": len(before_rows),
        "output_count": len(after_rows),
        "before": before,
        "after": after,
        "delta": {
            "temporal_metadata_count": after["temporal_metadata_count"] - before["temporal_metadata_count"],
            "inherited_chunk_count": after["inherited_chunk_count"] - before["inherited_chunk_count"],
            "normalized_chunk_count": after["normalized_chunk_count"] - before["normalized_chunk_count"],
            "conflict_chunk_count": after["conflict_chunk_count"] - before["conflict_chunk_count"],
            "ambiguous_chunk_count": after["ambiguous_chunk_count"] - before["ambiguous_chunk_count"],
        },
        "passed": len(before_rows) == len(after_rows),
    }
    return after_rows, manifest


def summarize_temporal_metadata(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    field_counts: Counter[str] = Counter()
    inherited_field_counts: Counter[str] = Counter()
    normalized_field_counts: Counter[str] = Counter()
    ambiguous_field_counts: Counter[str] = Counter()
    temporal_count = 0
    inherited_count = 0
    normalized_count = 0
    conflict_count = 0
    ambiguous_count = 0
    for chunk in chunks:
        metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
        has_temporal = False
        for field in TEMPORAL_FIELDS:
            if _has_value(metadata.get(field)):
                field_counts[field] += 1
                has_temporal = True
        if has_temporal:
            temporal_count += 1
        if metadata.get("temporal_metadata_inherited"):
            inherited_count += 1
            for field in metadata.get("temporal_metadata_inherited_fields") or []:
                inherited_field_counts[str(field)] += 1
        if metadata.get("temporal_metadata_normalized_fields"):
            normalized_count += 1
            for field in metadata.get("temporal_metadata_normalized_fields") or []:
                normalized_field_counts[str(field)] += 1
        if metadata.get("temporal_metadata_conflict_fields"):
            conflict_count += 1
        if metadata.get("temporal_metadata_ambiguous_fields"):
            ambiguous_count += 1
            for field in metadata.get("temporal_metadata_ambiguous_fields") or []:
                ambiguous_field_counts[str(field)] += 1
    return {
        "chunk_count": len(chunks),
        "temporal_metadata_count": temporal_count,
        "temporal_metadata_ratio": _ratio(temporal_count, len(chunks)),
        "field_counts": dict(sorted(field_counts.items())),
        "inherited_chunk_count": inherited_count,
        "inherited_field_counts": dict(sorted(inherited_field_counts.items())),
        "normalized_chunk_count": normalized_count,
        "normalized_field_counts": dict(sorted(normalized_field_counts.items())),
        "conflict_chunk_count": conflict_count,
        "ambiguous_chunk_count": ambiguous_count,
        "ambiguous_field_counts": dict(sorted(ambiguous_field_counts.items())),
    }


def load_chunks(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".jsonl":
        return list(_iter_jsonl(path))
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("chunks"), list):
        return [item for item in payload["chunks"] if isinstance(item, dict)]
    raise ValueError(f"Unsupported chunk JSON payload: {path}")


def write_chunks(path: Path, chunks: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".jsonl":
        path.write_text(
            "\n".join(json.dumps(chunk, ensure_ascii=False) for chunk in chunks) + ("\n" if chunks else ""),
            encoding="utf-8",
        )
        return
    path.write_text(json.dumps(chunks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    for line_no, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError(f"Invalid JSONL object at {path}:{line_no}")
        yield item


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill deterministic temporal metadata across preprocessed chunks.")
    parser.add_argument("--chunks-in", required=True)
    parser.add_argument("--chunks-out", required=True)
    parser.add_argument("--manifest-out", default=None)
    parser.add_argument("--fail-on-conflict", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    chunks_in = Path(args.chunks_in)
    chunks_out = Path(args.chunks_out)
    manifest_out = Path(args.manifest_out) if args.manifest_out else chunks_out.with_suffix(".temporal-backfill.json")
    try:
        chunks, manifest = backfill_temporal_metadata(load_chunks(chunks_in), source_label=str(chunks_in))
        write_chunks(chunks_out, chunks)
        manifest_out.parent.mkdir(parents=True, exist_ok=True)
        manifest_out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.fail_on_conflict and manifest["after"]["conflict_chunk_count"]:
            print(json.dumps({"ok": False, **manifest}, ensure_ascii=False, indent=2))
            return 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"ok": True, **manifest}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
