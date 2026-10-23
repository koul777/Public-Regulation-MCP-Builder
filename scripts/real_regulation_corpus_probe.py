"""Reprocess private PDF samples without granting approval or writing an index."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings
from app.ingestion.vector_adapter import build_vector_records
from app.schemas.chunk import ChunkOptions
from app.services.document_service import DocumentService
from app.services.processing_service import ProcessingService
from app.storage.repository import JsonRepository

DEFAULT_MAX_SOURCE_PAGES = 50


def _sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _source_path(row: dict[str, Any], manifest_path: Path) -> Path:
    value = str(row.get("snapshot_pdf") or row.get("source_path") or "").strip()
    if not value:
        raise ValueError("Each selected document requires snapshot_pdf or source_path.")
    path = Path(value)
    return (path if path.is_absolute() else manifest_path.parent / path).resolve()


def _selection(
    payload: dict[str, Any], selected_document_ids: Sequence[str] | None
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    rows = payload.get("documents")
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Corpus manifest must contain a nonempty documents list.")
    by_id = {str(row.get("document_id") or ""): row for row in rows}
    if "" in by_id or len(by_id) != len(rows):
        raise ValueError("Document identifiers must be nonempty and unique.")
    if selected_document_ids is not None:
        identifiers = list(dict.fromkeys(str(value) for value in selected_document_ids))
        if not identifiers or any(value not in by_id for value in identifiers):
            raise ValueError("Select at least one document present in the corpus manifest.")
        return [by_id[value] for value in identifiers], [
            {"document_id": value, "reason": "not_selected"} for value in by_id if value not in identifiers
        ]
    chosen: list[dict[str, Any]] = []
    deferred: list[dict[str, str]] = []
    seen: set[str] = set()
    for identifier, row in by_id.items():
        fingerprint = str(row.get("source_sha256") or "")
        pages = row.get("page_count")
        reason = ""
        if str(row.get("file_type") or "").lower() != "pdf":
            reason = "default_probe_pdf_only"
        elif not isinstance(pages, int) or isinstance(pages, bool) or pages <= 0:
            reason = "page_count_required_for_default_selection"
        elif pages > DEFAULT_MAX_SOURCE_PAGES:
            reason = "large_source_requires_explicit_selection"
        elif fingerprint in seen:
            reason = "duplicate_source_sha256"
        if reason:
            deferred.append({"document_id": identifier, "reason": reason})
        else:
            chosen.append(row)
            seen.add(fingerprint)
    if not chosen:
        raise ValueError("No small PDF source is eligible; explicitly select documents to process.")
    return chosen, deferred


def _validate_inputs(
    manifest_path: Path,
    output_dir: Path,
    payload: dict[str, Any],
    selected: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], Path, str]]:
    if output_dir == PROJECT_ROOT or output_dir.is_relative_to(PROJECT_ROOT):
        raise ValueError("Private probe output must be outside the source checkout.")
    if output_dir.exists():
        raise ValueError("Probe output must be a fresh directory; existing data is never overwritten.")
    for key in ("source_data_dir", "runtime_source_data_dir"):
        if payload.get(key):
            value = Path(str(payload[key]))
            protected = (value if value.is_absolute() else manifest_path.parent / value).resolve()
            if output_dir == protected or output_dir.is_relative_to(protected):
                raise ValueError("Probe output cannot be inside an existing source runtime.")
    validated = []
    for row in selected:
        source = _source_path(row, manifest_path)
        expected = str(row.get("source_sha256") or "").lower()
        if source.suffix.lower() != ".pdf" or not source.is_file():
            raise ValueError("Selected sources must be existing PDF files.")
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("Selected sources require a SHA-256 fingerprint.")
        if output_dir == source.parent or output_dir.is_relative_to(source.parent):
            raise ValueError("Probe output must be separate from original PDF directories.")
        actual = _sha256(source)
        if actual != expected:
            raise ValueError("Selected source SHA-256 does not match the corpus manifest.")
        validated.append((row, source, actual))
    return validated


def run_corpus_probe(
    *,
    corpus_manifest: Path,
    output_dir: Path,
    selected_document_ids: Sequence[str] | None = None,
    enable_kordoc_table_parser: bool = False,
) -> dict[str, Any]:
    """Run upload and preprocessing on fresh copies, recording pending review.

    The default selects unique PDFs of at most 50 pages. Larger PDFs require
    explicit selection and are reported as deferred otherwise. External AI
    review and OCR are disabled. Native PDF table extraction remains enabled;
    the supplementary Kordoc CLI can be enabled explicitly to reproduce the
    production table path. No approval or index-write API is called.
    """
    manifest_path = Path(corpus_manifest).resolve()
    destination = Path(output_dir).resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    selected, deferred = _selection(payload, selected_document_ids)
    validated = _validate_inputs(manifest_path, destination, payload, selected)
    destination.mkdir(parents=True, exist_ok=False)
    settings = Settings(
        data_dir=destination / "data",
        artifact_root=destination,
        tenant_storage_isolation=False,
        enable_agent_review=False,
        local_structure_review_enabled=False,
        enable_kordoc_table_parser=enable_kordoc_table_parser,
        pdf_ocr_backend="",
        quality_profiles_path="",
        institution_profiles_path="",
        api_audit_enabled=False,
        rag_trace_enabled=False,
    )
    repository = JsonRepository(settings)
    documents = DocumentService(settings=settings, repository=repository)
    processing = ProcessingService(settings=settings, repository=repository)
    options = ChunkOptions(enable_agent_review=False, enable_table_extraction=True)
    records = []
    started = time.perf_counter()
    for row, source, before_hash in validated:
        item: dict[str, Any] = {
            "source_document_id": row["document_id"],
            "source_sha256": before_hash,
            "source_path": str(source),
            "passed": False,
            "chunk_count": 0,
            "review_required_count": 0,
        }
        progress = []
        item_started = time.perf_counter()
        try:
            document = documents.upload(
                str(row.get("filename") or source.name),
                source.read_bytes(),
                document_name=str(row.get("document_name") or source.stem),
                institution_name=row.get("institution_name"),
                source_system="LOCAL_REGRESSION_COPY",
                profile_id=str(row.get("profile_id") or "regression-probe"),
                regulation_id=row.get("regulation_id"),
                regulation_version=row.get("regulation_version"),
                revision_date=row.get("revision_date"),
                effective_from=row.get("effective_from"),
                tenant_id=str(row.get("tenant_id") or "default"),
                regulation_status="draft",
            )
            item["probe_document_id"] = document.document_id

            def on_progress(job: Any) -> None:
                progress.append({
                    "stage_id": job.stage_id, "stage_status": job.stage_status,
                    "progress": job.progress, "elapsed_seconds": round(time.perf_counter() - item_started, 6),
                })

            job = processing.process(document.document_id, options, progress_callback=on_progress)
            current = repository.get_document(document.document_id)
            chunks = repository.get_chunks(document.document_id)
            nodes = repository.get_nodes(document.document_id)
            quality = repository.get_quality_report(document.document_id)
            runs = repository.list_runs(document.document_id)
            vector_records, admission = build_vector_records(
                [chunk.model_dump(mode="json") for chunk in chunks], require_approval=True,
            )
            statuses = Counter(chunk.approval_status for chunk in chunks)
            kordoc_summary = dict(chunks[0].metadata.get("kordoc_table_summary") or {}) if chunks else {}
            kordoc_completed = not enable_kordoc_table_parser or kordoc_summary.get("status") == "parsed"
            flags = Counter(
                str(flag)
                for chunk in chunks
                for flag in [*chunk.warnings, *chunk.metadata.get("parser_uncertainty_flags", [])]
            )
            item.update({
                "job_status": job.status, "job_error": job.error,
                "document_status": current.status if current else None,
                "regulation_status": current.regulation_status if current else None,
                "page_count": current.page_count if current else None,
                "node_count": len(nodes), "chunk_count": len(chunks),
                "chunk_type_counts": dict(Counter(chunk.chunk_type for chunk in chunks)),
                "approval_status_counts": dict(statuses),
                "review_required_count": sum(chunk.approval_status != "approved" for chunk in chunks),
                "review_flag_counts": dict(flags),
                "index_admission_record_count": len(vector_records),
                "index_admission_summary": admission,
                "kordoc_table_summary": kordoc_summary,
                "requested_table_parser_completed": kordoc_completed,
                "quality": quality.model_dump(mode="json") if quality else None,
                "phase_timings_ms": dict((runs[-1].stats or {}).get("phase_timings_ms") or {}) if runs else {},
                "passed": bool(job.status == "completed" and chunks and not statuses.get("approved")
                               and not vector_records and kordoc_completed
                               and current and current.regulation_status in {"draft", "pending_approval"}),
            })
        except Exception as exc:
            item.update({"error_type": type(exc).__name__, "error": str(exc), "passed": False})
        item["source_unchanged"] = _sha256(source) == before_hash
        item["passed"] = item["passed"] and item["source_unchanged"]
        item["elapsed_seconds"] = round(time.perf_counter() - item_started, 6)
        item["progress"] = progress
        records.append(item)
    approval_records = repository.list_approval_records()
    index_files = list((settings.data_dir / "vector_db").rglob("approved_vectors.jsonl"))
    report = {
        "schema_version": 1,
        "report_type": "real_regulation_corpus_probe",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corpus_manifest_sha256": _sha256(manifest_path),
        "output_dir": str(destination),
        "selected_document_count": len(records), "deferred_document_count": len(deferred),
        "parsed_count": sum(item.get("job_status") == "completed" for item in records),
        "failed_count": sum(not item["passed"] for item in records),
        "review_required_count": sum(item["review_required_count"] for item in records),
        "new_approvals": len(approval_records), "index_file_count": len(index_files),
        "passed": all(item["passed"] for item in records) and not approval_records and not index_files,
        "product_release_ready": False,
        "policy": {
            "default_max_source_pages": DEFAULT_MAX_SOURCE_PAGES,
            "external_ai_review_enabled": False, "ocr_enabled": False,
            "supplementary_kordoc_enabled": enable_kordoc_table_parser, "native_pdf_table_extraction_enabled": True,
            "kordoc_timeout_seconds": settings.kordoc_table_timeout_seconds,
            "kordoc_max_tables": settings.kordoc_table_max_tables,
            "kordoc_as_main": settings.kordoc_table_as_main,
            "kordoc_promote_min_match": settings.kordoc_table_promote_min_match,
            "chunk_options": options.model_dump(mode="json"),
            "approval_created": False, "index_written": False,
        },
        "deferred_documents": deferred, "results": records,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
    }
    (destination / "corpus-probe-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--document-id", action="append")
    parser.add_argument("--enable-kordoc-table-parser", action="store_true")
    args = parser.parse_args(argv)
    report = run_corpus_probe(
        corpus_manifest=args.corpus_manifest, output_dir=args.output_dir,
        selected_document_ids=args.document_id,
        enable_kordoc_table_parser=args.enable_kordoc_table_parser,
    )
    print(json.dumps({key: report[key] for key in (
        "passed", "parsed_count", "failed_count", "review_required_count", "new_approvals", "index_file_count",
    )}))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
