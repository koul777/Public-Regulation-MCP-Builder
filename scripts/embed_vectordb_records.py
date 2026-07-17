from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ingestion.embedding_adapter import LOCAL_HASH_EMBEDDING_MODEL, embed_vector_records
from app.ingestion.vector_upsert import load_vector_records_jsonl


def embed_vectordb_records(
    records_jsonl: Path,
    *,
    out_jsonl: Path,
    out_manifest: Path,
    dimensions: int = 384,
    model: str = LOCAL_HASH_EMBEDDING_MODEL,
    fail_on_leak: bool = False,
) -> dict:
    records = load_vector_records_jsonl(records_jsonl)
    embedded, summary = embed_vector_records(records, dimensions=dimensions, model=model)
    if fail_on_leak and summary["local_path_leak_count"]:
        raise ValueError(f"Embedded vector records contain local path leaks: {summary['local_path_leak_count']}")

    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    out_jsonl.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in embedded) + ("\n" if embedded else ""),
        encoding="utf-8",
    )
    manifest = {
        "report_type": "vectordb_embedding",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_records_jsonl": str(records_jsonl),
        "embedding_model": model,
        "embedding_dimensions": dimensions,
        "summary": summary,
        "out_jsonl": str(out_jsonl),
        "out_manifest": str(out_manifest),
    }
    out_manifest.parent.mkdir(parents=True, exist_ok=True)
    out_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create deterministic local embeddings for provider-neutral VectorDB records."
    )
    parser.add_argument("--records-jsonl", required=True)
    parser.add_argument("--out-jsonl", default=None)
    parser.add_argument("--out-manifest", default=None)
    parser.add_argument("--dimensions", type=int, default=384)
    parser.add_argument("--model", default=LOCAL_HASH_EMBEDDING_MODEL)
    parser.add_argument("--fail-on-leak", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_jsonl = (
        Path(args.out_jsonl)
        if args.out_jsonl
        else Path("reports") / f"vectordb_embedded_{timestamp}.jsonl"
    )
    out_manifest = (
        Path(args.out_manifest)
        if args.out_manifest
        else out_jsonl.with_suffix(".manifest.json")
    )
    try:
        manifest = embed_vectordb_records(
            Path(args.records_jsonl),
            out_jsonl=out_jsonl,
            out_manifest=out_manifest,
            dimensions=args.dimensions,
            model=args.model,
            fail_on_leak=args.fail_on_leak,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"ok": True, **manifest}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
