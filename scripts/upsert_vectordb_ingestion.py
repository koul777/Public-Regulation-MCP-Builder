from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ingestion.vector_upsert import (
    canonical_vector_target_path,
    load_vector_records_jsonl,
    validate_vector_record_tenant_scope,
    validate_vector_target_tenant_scope,
    vector_upsert_target,
)
from app.services.approval_validation import validate_vector_records_against_repository


def upsert_vectordb_ingestion(
    records_jsonl: Path,
    *,
    target_type: str,
    target_path: Path,
    out_manifest: Path,
    dry_run: bool = False,
    fail_on_leak: bool = True,
    collection_name: str | None = None,
    document_id: str | None = None,
    data_dir: Path | None = None,
    tenant_storage_isolation: bool = False,
    tenant_id: str | None = None,
    require_repository_approval: bool = True,
) -> dict:
    records = load_vector_records_jsonl(records_jsonl)
    if tenant_storage_isolation and not str(tenant_id or "").strip():
        raise ValueError("tenant_id is required when tenant storage isolation is enabled.")
    validate_vector_record_tenant_scope(records, expected_tenant_id=tenant_id)
    if tenant_id:
        validate_vector_target_tenant_scope(
            target_type,
            target_path,
            expected_tenant_id=tenant_id,
        )
        canonical_target = canonical_vector_target_path(
            data_dir or (PROJECT_ROOT / "data"),
            tenant_id,
            target_type=target_type,
            tenant_storage_isolation=tenant_storage_isolation,
        )
        if canonical_target is not None and target_path.resolve() != canonical_target.resolve():
            raise ValueError(
                "Tenant-scoped local vector target must use the canonical path: "
                f"{canonical_target}"
            )
    repository_validation = (
        validate_vector_records_against_repository(
            records,
            data_dir=data_dir or (PROJECT_ROOT / "data"),
            tenant_storage_isolation=tenant_storage_isolation,
            tenant_id=tenant_id,
        )
        if require_repository_approval
        else {
            "checked": False,
            "reason": "repository_approval_check_disabled",
            "data_dir": str((data_dir or (PROJECT_ROOT / "data")).resolve()),
            "tenant_storage_isolation": tenant_storage_isolation,
            "tenant_id": str(tenant_id or ""),
            "validated_record_count": 0,
            "document_count": 0,
        }
    )
    target = vector_upsert_target(target_type, target_path=target_path, collection_name=collection_name)
    result = target.upsert(records, dry_run=dry_run, fail_on_leak=fail_on_leak, document_id=document_id)
    manifest = {
        "report_type": "vectordb_upsert",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_records_jsonl": str(records_jsonl),
        "document_id": document_id or "",
        "tenant_id": str(tenant_id or ""),
        "repository_approval_validation": repository_validation,
        **result,
    }
    out_manifest.parent.mkdir(parents=True, exist_ok=True)
    out_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upsert provider-neutral VectorDB records into a target adapter.")
    parser.add_argument("--records-jsonl", required=True)
    parser.add_argument("--target-type", choices=["local-jsonl", "qdrant-local-jsonl", "pgvector-local-jsonl", "chroma-local-jsonl", "qdrant-rest-manifest"], default="local-jsonl")
    parser.add_argument("--target-path", required=True)
    parser.add_argument("--collection-name", default=None, help="Qdrant collection name for qdrant-rest-manifest.")
    parser.add_argument("--document-id", default=None, help="Remove stale records for this document id after upsert.")
    parser.add_argument("--out-manifest", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-local-path-leaks", action="store_true")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--tenant-storage-isolation", action="store_true")
    parser.add_argument("--tenant-id", default=None)
    parser.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--allow-missing-repository-approval", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    records_jsonl = Path(args.records_jsonl)
    target_path = Path(args.target_path)
    out_manifest = (
        Path(args.out_manifest)
        if args.out_manifest
        else Path("reports") / f"vectordb_upsert_{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    )
    try:
        manifest = upsert_vectordb_ingestion(
            records_jsonl,
            target_type=args.target_type,
            target_path=target_path,
            out_manifest=out_manifest,
            dry_run=args.dry_run,
            fail_on_leak=not args.allow_local_path_leaks,
            collection_name=args.collection_name,
            document_id=args.document_id,
            data_dir=Path(args.data_dir),
            tenant_storage_isolation=bool(args.tenant_storage_isolation and not args.flat_storage),
            tenant_id=args.tenant_id,
            require_repository_approval=not args.allow_missing_repository_approval,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"ok": True, **manifest}, ensure_ascii=False, indent=2))
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
