from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.tenant_access import settings_for_tenant
from app.storage.repository import JsonRepository


def backfill_chunk_profile_ids(
    *,
    data_dir: Path,
    tenant_id: str = "default",
    tenant_storage_isolation: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    settings = Settings(data_dir=data_dir, tenant_storage_isolation=tenant_storage_isolation)
    effective_settings = settings_for_tenant(settings, tenant_id)
    repository = JsonRepository(effective_settings)

    scanned_chunk_files = 0
    scanned_documents = 0
    updated_chunk_files = 0
    updated_chunk_count = 0
    missing_document_profile_count = 0
    conflict_count = 0
    conflict_samples: list[dict[str, Any]] = []
    update_samples: list[dict[str, Any]] = []
    missing_profile_samples: list[dict[str, Any]] = []

    for chunk_path in sorted(repository.root.glob("*_chunks.json")):
        scanned_chunk_files += 1
        document_id = chunk_path.name[: -len("_chunks.json")]
        document = repository.get_document(document_id)
        if document is None:
            continue
        scanned_documents += 1
        desired_profile_id = str(document.profile_id or "").strip()
        chunks = repository.get_chunks(document_id)
        if not chunks:
            continue

        current_profile_ids = [
            str((chunk.metadata or {}).get("profile_id") or "").strip()
            for chunk in chunks
        ]
        missing_count = sum(1 for value in current_profile_ids if not value)
        conflict_values = sorted(
            {
                value
                for value in current_profile_ids
                if value and desired_profile_id and value.casefold() != desired_profile_id.casefold()
            }
        )
        if conflict_values:
            conflict_count += 1
            if len(conflict_samples) < 20:
                conflict_samples.append(
                    {
                        "document_id": document_id,
                        "chunk_file": str(chunk_path),
                        "document_profile_id": desired_profile_id,
                        "conflicting_profile_ids": conflict_values,
                    }
                )
            continue

        if not desired_profile_id:
            if missing_count:
                missing_document_profile_count += 1
                if len(missing_profile_samples) < 20:
                    missing_profile_samples.append(
                        {
                            "document_id": document_id,
                            "chunk_file": str(chunk_path),
                            "missing_chunk_count": missing_count,
                        }
                    )
            continue

        if not missing_count:
            continue

        updated_chunks = []
        for chunk in chunks:
            metadata = dict(chunk.metadata or {})
            if not str(metadata.get("profile_id") or "").strip():
                metadata["profile_id"] = desired_profile_id
            updated_chunks.append(chunk.model_copy(update={"metadata": metadata}))

        updated_chunk_files += 1
        updated_chunk_count += missing_count
        if apply:
            repository.save_chunks(document_id, updated_chunks)
        if len(update_samples) < 20:
            update_samples.append(
                {
                    "document_id": document_id,
                    "chunk_file": str(chunk_path),
                    "missing_chunk_count": missing_count,
                    "profile_id": desired_profile_id,
                }
            )

    return {
        "report_type": "backfill_chunk_profile_ids",
        "data_dir": str(data_dir),
        "effective_data_dir": str(effective_settings.data_dir),
        "tenant_id": tenant_id,
        "tenant_storage_isolation": tenant_storage_isolation,
        "apply": apply,
        "scanned_chunk_files": scanned_chunk_files,
        "scanned_documents": scanned_documents,
        "updated_chunk_files": updated_chunk_files,
        "updated_chunk_count": updated_chunk_count,
        "missing_document_profile_count": missing_document_profile_count,
        "conflict_count": conflict_count,
        "update_samples": update_samples,
        "missing_profile_samples": missing_profile_samples,
        "conflict_samples": conflict_samples,
        "passed": conflict_count == 0,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill missing chunk profile_id metadata from documents.")
    parser.add_argument("--data-dir", type=Path, required=True, help="Repository data directory.")
    parser.add_argument("--tenant-id", default="default", help="Tenant ID for isolated data layouts.")
    parser.add_argument(
        "--tenant-storage-isolation",
        action="store_true",
        help="Resolve --data-dir through tenants/<tenant-id> before scanning repository files.",
    )
    parser.add_argument("--apply", action="store_true", help="Write updates back to disk.")
    parser.add_argument("--out-json", type=Path, help="Write the JSON report to this path.")
    parser.add_argument("--fail-on-issue", action="store_true", help="Exit non-zero when conflicts are found.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = backfill_chunk_profile_ids(
        data_dir=args.data_dir,
        tenant_id=args.tenant_id,
        tenant_storage_isolation=args.tenant_storage_isolation,
        apply=args.apply,
    )
    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.fail_on_issue and not report["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
