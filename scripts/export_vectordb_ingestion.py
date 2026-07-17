from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ingestion.vector_adapter import build_vector_records
from app.services.approval_validation import validate_export_chunks_against_repository


SUCCESS_STATUSES = {"completed", "skipped_unchanged"}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def export_vectordb_ingestion(
    batch_report_path: Path,
    *,
    out_jsonl: Path,
    out_manifest: Path,
    text_field: str = "retrieval_text",
    fail_on_leak: bool = False,
    data_dir: Path | None = None,
    tenant_storage_isolation: bool = False,
    tenant_id: str | None = None,
    require_repository_approval: bool = True,
) -> dict[str, Any]:
    batch_report = load_json(batch_report_path)
    chunks = list(_iter_batch_chunks(batch_report, batch_report_path=batch_report_path))
    _validate_chunk_tenant_scope(chunks, expected_tenant_id=tenant_id)
    repository_validation = (
        validate_export_chunks_against_repository(
            chunks,
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
            "validated_chunk_count": 0,
            "document_count": 0,
        }
    )
    records, summary = build_vector_records(chunks, text_field=text_field)
    if fail_on_leak and summary["local_path_leak_count"]:
        raise ValueError(f"VectorDB ingestion export contains local path leaks: {summary['local_path_leak_count']}")
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    out_manifest.parent.mkdir(parents=True, exist_ok=True)
    out_jsonl.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + ("\n" if records else ""),
        encoding="utf-8",
    )
    manifest = {
        "report_type": "vectordb_ingestion",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_batch_report_file": batch_report_path.name,
        "source_batch_generated_at": batch_report.get("generated_at"),
        "tenant_id": str(tenant_id or ""),
        "text_field": text_field,
        "input_count": batch_report.get("input_count", 0),
        "successful_count": batch_report.get("successful_count", 0),
        "failed_count": batch_report.get("failed_count", 0),
        "quality_passed_count": batch_report.get("quality_passed_count", 0),
        "summary": summary,
        "repository_approval_validation": repository_validation,
        "out_jsonl": str(out_jsonl),
        "out_manifest": str(out_manifest),
    }
    out_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _iter_batch_chunks(batch_report: dict[str, Any], *, batch_report_path: Path) -> Iterable[dict[str, Any]]:
    for row in batch_report.get("rows", []) or []:
        if row.get("status") not in SUCCESS_STATUSES:
            continue
        document_id = row.get("document_id")
        if not document_id:
            continue
        chunk_path = _chunk_jsonl_path(row, batch_report_path=batch_report_path)
        if not chunk_path.is_file():
            raise FileNotFoundError(f"Chunk JSONL not found for {document_id}: {chunk_path}")
        yield from _iter_jsonl(chunk_path)


def _chunk_jsonl_path(row: dict[str, Any], *, batch_report_path: Path) -> Path:
    document_id = str(row.get("document_id") or "")
    candidates: list[Path] = []
    quality_json = row.get("quality_json")
    if quality_json:
        quality_path = Path(str(quality_json))
        if quality_path.is_absolute():
            candidates.append(quality_path.with_name(f"{document_id}.jsonl"))
        else:
            candidates.append((PROJECT_ROOT / quality_path).with_name(f"{document_id}.jsonl"))
            candidates.append((batch_report_path.resolve().parent / quality_path).with_name(f"{document_id}.jsonl"))
    candidates.append(PROJECT_ROOT / "data" / "exports" / f"{document_id}.jsonl")
    candidates.append(Path("data") / "exports" / f"{document_id}.jsonl")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at {path}:{line_no}: {exc}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"Invalid JSONL at {path}:{line_no}: expected object")
        yield item


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export provider-neutral VectorDB ingestion JSONL from a batch report.")
    parser.add_argument("--batch-report", required=True)
    parser.add_argument("--out-jsonl", default=None)
    parser.add_argument("--out-manifest", default=None)
    parser.add_argument("--text-field", choices=["retrieval_text", "text", "normalized_text"], default="retrieval_text")
    parser.add_argument("--fail-on-leak", action="store_true")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--tenant-storage-isolation", action="store_true")
    parser.add_argument("--tenant-id", default=None)
    parser.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--allow-missing-repository-approval", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    batch_report = Path(args.batch_report)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_jsonl = Path(args.out_jsonl) if args.out_jsonl else Path("reports") / f"vectordb_ingestion_{timestamp}.jsonl"
    out_manifest = (
        Path(args.out_manifest)
        if args.out_manifest
        else out_jsonl.with_suffix(".manifest.json")
    )
    try:
        manifest = export_vectordb_ingestion(
            batch_report,
            out_jsonl=out_jsonl,
            out_manifest=out_manifest,
            text_field=args.text_field,
            fail_on_leak=args.fail_on_leak,
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


def _validate_chunk_tenant_scope(chunks: Iterable[dict[str, Any]], *, expected_tenant_id: str | None) -> None:
    expected = str(expected_tenant_id or "").strip()
    if not expected:
        return
    observed: set[str] = set()
    for index, chunk in enumerate(chunks, start=1):
        tenant_id = str(chunk.get("tenant_id") or "").strip()
        if not tenant_id:
            raise ValueError(f"Export chunk {index} is missing tenant_id.")
        observed.add(tenant_id)
        if tenant_id != expected:
            raise ValueError(
                f"Export chunk {index} tenant_id does not match expected tenant: {tenant_id!r} != {expected!r}."
            )
    if len(observed) > 1:
        raise ValueError(f"Export input contains multiple tenant scopes: {', '.join(sorted(observed))}")


if __name__ == "__main__":
    raise SystemExit(main())
