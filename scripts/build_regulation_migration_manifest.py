"""Build a non-mutating migration manifest for legacy regulation vectors."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from app.core.tenant_access import tenant_storage_key


REQUIRED_FIELDS = (
    "tenant_id",
    "profile_id",
    "regulation_id",
    "regulation_version",
    "regulation_status",
    "effective_from",
)
OPTIONAL_DATE_FIELDS = ("effective_to", "repealed_at")


def build_regulation_migration_manifest(
    *,
    data_dir: Path,
    tenant_id: str = "default",
    profile_id: str | None = None,
    tenant_storage_isolation: bool = True,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    effective_dir = data_dir / "tenants" / tenant_id if tenant_storage_isolation else data_dir
    vector_path = effective_dir / "vector_db" / tenant_storage_key(tenant_id) / "approved_vectors.jsonl"
    requested_profile_id = str(profile_id or "").strip()
    records: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []

    if vector_path.is_file():
        for line_number, line in enumerate(vector_path.read_text(encoding="utf-8-sig").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                parse_errors.append({"line": line_number, "error": str(exc)})
                continue
            if isinstance(item, dict):
                records.append(item)

    candidates: list[dict[str, Any]] = []
    for record in records:
        metadata = dict(record.get("metadata") or {}) if isinstance(record.get("metadata"), dict) else {}
        for field in (*REQUIRED_FIELDS, *OPTIONAL_DATE_FIELDS):
            if field not in metadata and record.get(field) not in (None, ""):
                metadata[field] = record.get(field)
        if requested_profile_id and str(metadata.get("profile_id") or "").strip() != requested_profile_id:
            continue

        missing_fields = [field for field in REQUIRED_FIELDS if metadata.get(field) in (None, "")]
        invalid_date_fields = [
            field
            for field in ("effective_from", *OPTIONAL_DATE_FIELDS)
            if metadata.get(field) not in (None, "") and _parse_date(metadata.get(field)) is None
        ]
        if not missing_fields and not invalid_date_fields:
            continue

        actions: list[str] = []
        if "tenant_id" in missing_fields:
            actions.append("confirm_tenant_scope")
        if "profile_id" in missing_fields:
            actions.append("assign_institution_profile")
        if "regulation_id" in missing_fields or "regulation_version" in missing_fields:
            actions.append("register_regulation_family_and_version")
        if "regulation_status" in missing_fields:
            actions.append("record_lifecycle_status_and_human_approval")
        if "effective_from" in missing_fields or "effective_from" in invalid_date_fields:
            actions.append("record_source_effective_start_date")
        if invalid_date_fields:
            actions.append("correct_invalid_source_dates")
        actions.append("reindex_only_after_review")

        candidates.append(
            {
                "record_id": str(record.get("id") or ""),
                "document_id": str(record.get("document_id") or metadata.get("document_id") or ""),
                "chunk_id": str(record.get("chunk_id") or metadata.get("chunk_id") or ""),
                "institution_name": str(metadata.get("institution_name") or ""),
                "article_no": str(metadata.get("article_no") or ""),
                "profile_id": str(metadata.get("profile_id") or ""),
                "regulation_id": str(metadata.get("regulation_id") or ""),
                "regulation_version": str(metadata.get("regulation_version") or ""),
                "missing_fields": missing_fields,
                "invalid_date_fields": invalid_date_fields,
                "suggested_actions": actions,
            }
        )

    manifest = {
        "manifest_type": "regulation_migration_dry_run",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime_data_dir": str(data_dir),
        "effective_runtime_data_dir": str(effective_dir),
        "vector_path": str(vector_path),
        "tenant_id": tenant_id,
        "profile_id": requested_profile_id or None,
        "tenant_storage_isolation": tenant_storage_isolation,
        "record_count": len(records),
        "candidate_count": len(candidates),
        "parse_error_count": len(parse_errors),
        "passed": not candidates and not parse_errors,
        "parse_errors": parse_errors,
        "candidates": candidates,
        "mutation_performed": False,
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(manifest), encoding="utf-8")
    return manifest


def _parse_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _to_markdown(manifest: dict[str, Any]) -> str:
    lines = [
        "# Regulation Migration Dry Run",
        "",
        f"- Passed: `{str(manifest.get('passed')).lower()}`",
        f"- Tenant: `{manifest.get('tenant_id')}`",
        f"- Profile: `{manifest.get('profile_id') or ''}`",
        f"- Vector path: `{manifest.get('vector_path')}`",
        f"- Records: {manifest.get('record_count')}",
        f"- Migration candidates: {manifest.get('candidate_count')}",
        f"- Parse errors: {manifest.get('parse_error_count')}",
        "",
        "No source or vector data was modified by this command.",
        "",
        "## Candidates",
        "",
        "| Document | Chunk | Profile | Regulation | Version | Missing | Invalid dates |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in manifest.get("candidates") or []:
        lines.append(
            "| {document} | {chunk} | {profile} | {regulation} | {version} | {missing} | {invalid} |".format(
                document=_md(row.get("document_id")),
                chunk=_md(row.get("chunk_id")),
                profile=_md(row.get("profile_id")),
                regulation=_md(row.get("regulation_id")),
                version=_md(row.get("regulation_version")),
                missing=_md(", ".join(row.get("missing_fields") or [])),
                invalid=_md(", ".join(row.get("invalid_date_fields") or [])),
            )
        )
    return "\n".join(lines) + "\n"


def _md(value: object) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a non-mutating regulation migration manifest.")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--profile-id", default=None)
    storage = parser.add_mutually_exclusive_group()
    storage.add_argument("--tenant-storage-isolation", action="store_true")
    storage.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--fail-on-blocker", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tenant_storage_isolation = True
    if args.flat_storage:
        tenant_storage_isolation = False
    manifest = build_regulation_migration_manifest(
        data_dir=Path(args.data_dir),
        tenant_id=args.tenant_id,
        profile_id=args.profile_id,
        tenant_storage_isolation=tenant_storage_isolation,
        out_json=Path(args.out_json) if args.out_json else None,
        out_md=Path(args.out_md) if args.out_md else None,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), file=sys.stdout)
    if args.fail_on_blocker and not manifest["passed"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
