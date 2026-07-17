from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings
from app.core.tenant_access import settings_for_tenant, tenant_storage_key
from app.ingestion.vector_adapter import build_vector_records
from scripts.backfill_temporal_metadata import (
    backfill_temporal_metadata,
    load_chunks,
    summarize_temporal_metadata,
    write_chunks,
)


def build_temporal_backfill_shadow_runtime(
    *,
    source_data_dir: Path,
    out_data_dir: Path,
    tenant_id: str = "default",
    tenant_storage_isolation: bool | None = None,
    out_manifest: Path | None = None,
    text_field: str = "retrieval_text",
    fail_on_conflict: bool = False,
) -> dict[str, Any]:
    source_effective_dir = _effective_runtime_dir(
        source_data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
    )
    target_effective_dir = _effective_runtime_dir(
        out_data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
    )
    if source_effective_dir.resolve() == target_effective_dir.resolve():
        raise ValueError("source and shadow runtime directories must differ")

    storage_isolated = _tenant_storage_isolation(source_data_dir, tenant_storage_isolation)
    document_tenants = _document_tenant_map(source_effective_dir)
    chunk_files = sorted((source_effective_dir / "repository").glob("*_chunks.json"))
    all_before: list[dict[str, Any]] = []
    all_after: list[dict[str, Any]] = []
    output_chunks: list[tuple[Path, list[dict[str, Any]]]] = []
    file_manifests: list[dict[str, Any]] = []
    tenant_enrichment_count = 0
    tenant_provenance_missing_count = 0
    target_repository = target_effective_dir / "repository"
    for chunk_file in chunk_files:
        source_chunks = load_chunks(chunk_file)
        before: list[dict[str, Any]] = []
        for chunk in source_chunks:
            enriched, was_enriched, provenance_missing = _with_runtime_tenant(
                chunk,
                tenant_id=tenant_id,
                document_tenants=document_tenants,
                storage_isolated=storage_isolated,
            )
            before.append(enriched)
            tenant_enrichment_count += int(was_enriched)
            tenant_provenance_missing_count += int(provenance_missing)
        after, backfill_manifest = backfill_temporal_metadata(before, source_label=str(chunk_file))
        target_chunk_file = target_repository / chunk_file.name
        output_chunks.append((target_chunk_file, after))
        all_before.extend(before)
        all_after.extend(after)
        file_manifests.append(
            {
                "source_chunks": str(chunk_file),
                "shadow_chunks": str(target_chunk_file),
                "input_count": backfill_manifest["input_count"],
                "output_count": backfill_manifest["output_count"],
                "delta": backfill_manifest["delta"],
                "after": {
                    "temporal_metadata_count": backfill_manifest["after"]["temporal_metadata_count"],
                    "inherited_chunk_count": backfill_manifest["after"]["inherited_chunk_count"],
                    "normalized_chunk_count": backfill_manifest["after"]["normalized_chunk_count"],
                    "conflict_chunk_count": backfill_manifest["after"]["conflict_chunk_count"],
                    "ambiguous_chunk_count": backfill_manifest["after"].get("ambiguous_chunk_count", 0),
                },
                "passed": backfill_manifest["passed"],
            }
        )

    records, vector_summary = build_vector_records(all_after, text_field=text_field)
    vector_path = target_effective_dir / "vector_db" / tenant_storage_key(tenant_id) / "approved_vectors.jsonl"

    before_summary = summarize_temporal_metadata(all_before)
    after_summary = summarize_temporal_metadata(all_after)
    conflict_samples = _conflict_samples(all_after)
    ambiguous_samples = _ambiguity_samples(all_after)
    write_blocked = bool(fail_on_conflict and after_summary["conflict_chunk_count"])
    manifest = {
        "report_type": "temporal_backfill_shadow_runtime",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_runtime_data_dir": str(source_data_dir),
        "source_effective_runtime_data_dir": str(source_effective_dir),
        "shadow_runtime_data_dir": str(out_data_dir),
        "shadow_effective_runtime_data_dir": str(target_effective_dir),
        "tenant_id": tenant_id,
        "tenant_storage_isolation": storage_isolated,
        "tenant_provenance": {
            "requested_tenant_id": tenant_id,
            "document_manifest_tenant_count": len(document_tenants),
            "enriched_chunk_count": tenant_enrichment_count,
            "missing_chunk_count": tenant_provenance_missing_count,
            "policy": "validate explicit chunk/document tenant values, then enrich only the shadow copy",
        },
        "text_field": text_field,
        "runtime_copy_scope": "repository_chunks_and_approved_vectors_only",
        "shadow_runtime_runnable": False,
        "chunk_file_count": len(chunk_files),
        "input_chunk_count": len(all_before),
        "output_chunk_count": len(all_after),
        "vector_record_count": len(records),
        "before": before_summary,
        "after": after_summary,
        "delta": {
            "temporal_metadata_count": after_summary["temporal_metadata_count"] - before_summary["temporal_metadata_count"],
            "inherited_chunk_count": after_summary["inherited_chunk_count"] - before_summary["inherited_chunk_count"],
            "normalized_chunk_count": after_summary["normalized_chunk_count"] - before_summary["normalized_chunk_count"],
            "conflict_chunk_count": after_summary["conflict_chunk_count"] - before_summary["conflict_chunk_count"],
            "ambiguous_chunk_count": after_summary.get("ambiguous_chunk_count", 0)
            - before_summary.get("ambiguous_chunk_count", 0),
        },
        "vector_summary": vector_summary,
        "vector_path": str(vector_path),
        "files": file_manifests,
        "conflict_samples": conflict_samples,
        "ambiguous_samples": ambiguous_samples,
        "write_blocked": write_blocked,
        "shadow_runtime_written": not write_blocked,
        "passed": len(all_before) == len(all_after)
        and len(records) == len([chunk for chunk in all_after if _approved_for_indexing_shape(chunk)])
        and tenant_provenance_missing_count == 0
        and after_summary["conflict_chunk_count"] == 0,
        "api_call_count": 0,
    }
    if not write_blocked:
        for target_chunk_file, chunks in output_chunks:
            write_chunks(target_chunk_file, chunks)
        vector_path.parent.mkdir(parents=True, exist_ok=True)
        vector_path.write_text(
            "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + ("\n" if records else ""),
            encoding="utf-8",
        )
    if out_manifest:
        out_manifest.parent.mkdir(parents=True, exist_ok=True)
        out_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def _effective_runtime_dir(
    data_dir: Path,
    *,
    tenant_id: str,
    tenant_storage_isolation: bool | None,
) -> Path:
    settings = Settings(
        data_dir=data_dir,
        tenant_storage_isolation=_tenant_storage_isolation(data_dir, tenant_storage_isolation),
    )
    return settings_for_tenant(settings, tenant_id).data_dir


def _tenant_storage_isolation(data_dir: Path, tenant_storage_isolation: bool | None) -> bool:
    if tenant_storage_isolation is not None:
        return tenant_storage_isolation
    return Settings(data_dir=data_dir).tenant_storage_isolation


def _document_tenant_map(effective_runtime_dir: Path) -> dict[str, str]:
    manifest_path = effective_runtime_dir / "repository" / "manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read runtime repository manifest: {manifest_path}") from exc
    documents = payload.get("documents") if isinstance(payload, dict) else None
    if not isinstance(documents, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in documents.items():
        if not isinstance(value, dict):
            continue
        document_id = str(value.get("document_id") or key or "").strip()
        document_tenant = str(value.get("tenant_id") or "").strip()
        if document_id and document_tenant:
            result[document_id] = document_tenant
    return result


def _with_runtime_tenant(
    chunk: dict[str, Any],
    *,
    tenant_id: str,
    document_tenants: dict[str, str],
    storage_isolated: bool,
) -> tuple[dict[str, Any], bool, bool]:
    enriched = dict(chunk)
    metadata = dict(enriched.get("metadata") or {})
    document_id = str(enriched.get("document_id") or metadata.get("document_id") or "").strip()
    explicit = {
        str(value).strip()
        for value in (
            enriched.get("tenant_id"),
            metadata.get("tenant_id"),
            document_tenants.get(document_id),
        )
        if str(value or "").strip()
    }
    if any(value != tenant_id for value in explicit):
        raise ValueError(
            f"Chunk tenant scope mismatch for document_id={document_id or '<missing>'}: "
            f"expected={tenant_id}, observed={sorted(explicit)}"
        )
    if not explicit and not storage_isolated:
        return enriched, False, True
    was_enriched = enriched.get("tenant_id") != tenant_id or metadata.get("tenant_id") != tenant_id
    enriched["tenant_id"] = tenant_id
    metadata["tenant_id"] = tenant_id
    enriched["metadata"] = metadata
    return enriched, was_enriched, False


def _conflict_samples(chunks: list[dict[str, Any]], *, limit: int = 25) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for chunk in chunks:
        metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
        conflict_fields = metadata.get("temporal_metadata_conflict_fields")
        if not conflict_fields:
            continue
        samples.append(
            {
                "document_id": chunk.get("document_id") or metadata.get("document_id") or "",
                "chunk_id": chunk.get("chunk_id") or metadata.get("chunk_id") or "",
                "chunk_type": chunk.get("chunk_type") or metadata.get("chunk_type") or "",
                "regulation_title": metadata.get("regulation_title") or metadata.get("document_name") or "",
                "article_no": metadata.get("article_no") or "",
                "source_page_start": chunk.get("source_page_start") or metadata.get("source_page_start"),
                "conflict_fields": [str(field) for field in conflict_fields],
            }
        )
        if len(samples) >= limit:
            break
    return samples


def _ambiguity_samples(chunks: list[dict[str, Any]], *, limit: int = 25) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for chunk in chunks:
        metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
        ambiguous_fields = metadata.get("temporal_metadata_ambiguous_fields")
        if not ambiguous_fields:
            continue
        samples.append(
            {
                "document_id": chunk.get("document_id") or metadata.get("document_id") or "",
                "chunk_id": chunk.get("chunk_id") or metadata.get("chunk_id") or "",
                "chunk_type": chunk.get("chunk_type") or metadata.get("chunk_type") or "",
                "regulation_title": metadata.get("regulation_title") or metadata.get("document_name") or "",
                "article_no": metadata.get("article_no") or "",
                "source_page_start": chunk.get("source_page_start") or metadata.get("source_page_start"),
                "ambiguous_fields": [str(field) for field in ambiguous_fields],
            }
        )
        if len(samples) >= limit:
            break
    return samples


def _approved_for_indexing_shape(chunk: dict[str, Any]) -> bool:
    metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
    return bool(
        (chunk.get("retrieval_text") or chunk.get("text") or chunk.get("normalized_text"))
        and str(chunk.get("approval_status") or metadata.get("approval_status") or "").strip().lower() == "approved"
        and (chunk.get("approval_id") or metadata.get("approval_id"))
        and (chunk.get("tenant_id") or metadata.get("tenant_id"))
        and str(chunk.get("security_level") or metadata.get("security_level") or "").strip().lower()
        in {"public", "internal", "sensitive", "confidential"}
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a shadow runtime with deterministic temporal metadata backfill.")
    parser.add_argument("--source-data-dir", required=True)
    parser.add_argument("--out-data-dir", required=True)
    parser.add_argument("--tenant-id", default="default")
    storage = parser.add_mutually_exclusive_group()
    storage.add_argument("--tenant-storage-isolation", action="store_true")
    storage.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--out-manifest", default=None)
    parser.add_argument("--text-field", choices=["retrieval_text", "text", "normalized_text"], default="retrieval_text")
    parser.add_argument("--fail-on-conflict", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    if stdout is sys.stdout and hasattr(stdout, "reconfigure"):
        stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    tenant_storage_isolation = None
    if args.tenant_storage_isolation:
        tenant_storage_isolation = True
    if args.flat_storage:
        tenant_storage_isolation = False
    try:
        manifest = build_temporal_backfill_shadow_runtime(
            source_data_dir=Path(args.source_data_dir),
            out_data_dir=Path(args.out_data_dir),
            tenant_id=args.tenant_id,
            tenant_storage_isolation=tenant_storage_isolation,
            out_manifest=Path(args.out_manifest) if args.out_manifest else None,
            text_field=args.text_field,
            fail_on_conflict=args.fail_on_conflict,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=stdout)
        return 2
    print(json.dumps({"ok": True, **manifest}, ensure_ascii=False, indent=2), file=stdout)
    if args.fail_on_conflict and manifest["after"]["conflict_chunk_count"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
