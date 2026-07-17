from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings
from app.core.failure_classification import classify_processing_failure
from app.core.institution_profiles import InstitutionProfileRegistry, load_institution_profile_registry
from app.core.pipeline import processing_options_payload
from app.schemas.chunk import ChunkOptions
from app.schemas.document import Document
from app.schemas.quality import QualityReport
from app.schemas.run import ProcessingRun
from app.services.document_service import DocumentService
from app.services.processing_service import ProcessingService
from app.storage.file_store import SUPPORTED_EXTENSIONS, FileStore, sha256_bytes
from app.storage.repository import JsonRepository
from scripts.run_kordoc_crosscheck import build_crosscheck_report, markdown_report as kordoc_markdown_report


DEFAULT_GLOB_PATTERNS = ("*.pdf", "*.docx", "*.hwpx", "*.hwp")
DEFAULT_PROFILE_ID = "default-public-institution"
PUBLIC_PORTAL_SOURCE_SELECTION_COLUMNS = [
    "latest_file_no",
    "latest_file_name",
    "latest_file_ext",
    "selected_latest_file",
    "selection_policy",
    "selection_warning",
]
SUMMARY_COLUMNS = [
    "input_path",
    "filename",
    "document_id",
    "institution_name",
    "apba_id",
    "source_system",
    "source_url",
    "source_record_id",
    "source_record_id_origin",
    "source_file_id",
    *PUBLIC_PORTAL_SOURCE_SELECTION_COLUMNS,
    "source_disclosure_date",
    "source_disclosure_date_origin",
    "source_posted_date",
    "profile_id",
    "status",
    "error",
    "failure_category",
    "ocr_required",
    "ocr_page_count",
    "retry_recommended",
    "failure_next_action",
    "job_id",
    "elapsed_seconds",
    "quality_passed",
    "quality_score",
    "node_count",
    "chunk_count",
    "issue_count",
    "error_count",
    "warning_count",
    "failed_error_check_count",
    "failed_warning_check_count",
    "failed_info_check_count",
    "recommendation_count",
    "top_recommendation",
    "chunk_to_source_char_ratio",
    "table_like_chunks",
    "table_citation_ready_chunks",
    "chunks_with_table_cell_rows",
    "table_like_without_cell_rows",
    "table_cell_row_count",
    "probable_table_false_positive_chunks",
    "stable_table_false_positive_chunks",
    "table_false_positive_attention_chunks",
    "probable_table_extraction_failed_chunks",
    "duplicate_regulation_node_count",
    "zero_chunk_regulation_node_count",
    "chunks_missing_regulation_no",
    "article_chunks_missing_regulation_no",
    "detected_reg_no_without_chunk_metadata_count",
    "agent_review_status",
    "agent_review_skip_reason",
    "agent_review_candidate_count",
    "agent_review_cached_candidate_count",
    "agent_review_new_candidate_count",
    "agent_review_selected_count",
    "agent_review_estimated_input_tokens",
    "agent_review_estimated_output_tokens",
    "agent_review_estimated_total_tokens",
    "agent_review_budget_exhausted",
    "agent_review_budget_reservation_id",
    "agent_review_approval_reference",
    "agent_review_model",
    "agent_review_payload_hash",
    "agent_review_estimated_cost",
    "agent_review_actual_cost",
    "agent_review_provider_request_id",
    "agent_review_plan_json",
    "reused_from_run_id",
    "reused_from_job_id",
    "historical_agent_review_status",
    "historical_agent_review_selected_count",
    "historical_agent_review_estimated_input_tokens",
    "historical_agent_review_estimated_output_tokens",
    "historical_agent_review_estimated_total_tokens",
    "historical_agent_review_api_call_count",
    "quality_json",
    "quality_md",
    "tables_csv",
    "tables_jsonl",
]


def parse_patterns(value: str | None) -> tuple[str, ...]:
    if not value:
        return DEFAULT_GLOB_PATTERNS
    patterns = tuple(item.strip() for item in value.split(",") if item.strip())
    return patterns or DEFAULT_GLOB_PATTERNS


def collect_input_files(
    inputs: Iterable[str | Path],
    *,
    patterns: Iterable[str] = DEFAULT_GLOB_PATTERNS,
    recursive: bool = True,
) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for raw_input in inputs:
        input_path = Path(raw_input).expanduser()
        if input_path.is_file():
            _add_supported_file(input_path, files, seen)
            continue
        if input_path.is_dir():
            for pattern in patterns:
                iterator = input_path.rglob(pattern) if recursive else input_path.glob(pattern)
                for candidate in iterator:
                    if candidate.is_file():
                        _add_supported_file(candidate, files, seen)
            continue
        raise FileNotFoundError(f"Input path not found: {input_path}")
    return sorted(files, key=lambda path: str(path).lower())


def _add_supported_file(path: Path, files: list[Path], seen: set[Path]) -> None:
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file extension: {path.suffix} ({path})")
    resolved = path.resolve()
    if resolved not in seen:
        seen.add(resolved)
        files.append(resolved)


def process_batch(
    files: Iterable[Path],
    *,
    settings: Settings,
    chunk_options: ChunkOptions,
    institution_name: str | None = None,
    apba_id: str | None = None,
    source_system: str | None = None,
    source_url: str | None = None,
    profile_id: str | None = DEFAULT_PROFILE_ID,
    force_reprocess: bool = False,
) -> dict[str, Any]:
    entries = [
        {
            "path": path,
            "document_name": None,
            "institution_name": institution_name,
            "apba_id": apba_id,
            "source_system": source_system,
            "source_url": source_url,
            "source_record_id": None,
            "source_record_id_origin": None,
            "source_file_id": None,
            "source_disclosure_date": None,
            "source_disclosure_date_origin": None,
            "source_posted_date": None,
            "profile_id": profile_id,
        }
        for path in files
    ]
    return process_entries(entries, settings=settings, chunk_options=chunk_options, force_reprocess=force_reprocess)


def process_entries(
    entries: Iterable[dict[str, Any]],
    *,
    settings: Settings,
    chunk_options: ChunkOptions,
    force_reprocess: bool = False,
) -> dict[str, Any]:
    repository = JsonRepository(settings)
    file_store = FileStore(settings)
    document_service = DocumentService(settings=settings, repository=repository, file_store=file_store)
    processing_service = ProcessingService(settings=settings, repository=repository, file_store=file_store)

    rows: list[dict[str, Any]] = []
    for entry in entries:
        path = Path(entry["path"])
        rows.append(
            process_file(
                path,
                document_service=document_service,
                processing_service=processing_service,
                repository=repository,
                chunk_options=chunk_options,
                document_name=entry.get("document_name"),
                institution_name=entry.get("institution_name"),
                apba_id=entry.get("apba_id"),
                source_system=entry.get("source_system"),
                source_url=entry.get("source_url"),
                source_record_id=entry.get("source_record_id"),
                source_record_id_origin=entry.get("source_record_id_origin"),
                source_file_id=entry.get("source_file_id"),
                source_selection_metadata=_source_selection_metadata(entry),
                source_disclosure_date=entry.get("source_disclosure_date"),
                source_disclosure_date_origin=entry.get("source_disclosure_date_origin"),
                source_posted_date=entry.get("source_posted_date"),
                profile_id=entry.get("profile_id") or DEFAULT_PROFILE_ID,
                settings=settings,
                quality_profiles_sha256=processing_service.quality_profiles_sha256,
                force_reprocess=force_reprocess,
            )
        )
    summary = build_batch_summary(rows)
    apply_agent_review_batch_budget(summary, settings)
    return summary


def process_file(
    path: Path,
    *,
    document_service: DocumentService,
    processing_service: ProcessingService,
    repository: JsonRepository,
    chunk_options: ChunkOptions,
    institution_name: str | None = None,
    apba_id: str | None = None,
    source_system: str | None = None,
    source_url: str | None = None,
    source_record_id: str | None = None,
    source_record_id_origin: str | None = None,
    source_file_id: str | None = None,
    source_selection_metadata: dict[str, Any] | None = None,
    source_disclosure_date: str | None = None,
    source_disclosure_date_origin: str | None = None,
    source_posted_date: str | None = None,
    profile_id: str | None = DEFAULT_PROFILE_ID,
    document_name: str | None = None,
    settings: Settings | None = None,
    quality_profiles_sha256: str | None = None,
    force_reprocess: bool = False,
) -> dict[str, Any]:
    document: Document | None = None
    started = time.perf_counter()
    try:
        content = path.read_bytes()
        file_hash = sha256_bytes(content)
        if not force_reprocess:
            reusable = repository.find_reusable_run(
                file_hash=file_hash,
                options=processing_options_payload(
                    chunk_options,
                    settings=settings,
                    quality_profiles_sha256=quality_profiles_sha256,
                ),
                source_system=source_system,
                source_record_id=source_record_id,
                source_file_id=source_file_id,
                profile_id=profile_id,
                document_name=document_name,
                institution_name=institution_name,
                source_url=source_url,
                source_disclosure_date=source_disclosure_date,
                source_posted_date=source_posted_date,
            )
            if reusable is not None:
                document, latest_run = reusable
                quality_report = repository.get_quality_report(document.document_id)
                if quality_report is not None:
                    elapsed = round(time.perf_counter() - started, 3)
                    row = build_reused_row(path, document, quality_report, latest_run, elapsed)
                    row["apba_id"] = apba_id or row.get("apba_id") or ""
                    row = _with_source_origin(
                        row,
                        source_record_id_origin=source_record_id_origin,
                        source_disclosure_date_origin=source_disclosure_date_origin,
                    )
                    return _with_source_selection(row, source_selection_metadata)
        document = document_service.upload(
            path.name,
            content,
            document_name=document_name,
            institution_name=institution_name,
            apba_id=apba_id,
            source_system=source_system,
            source_url=source_url,
            source_record_id=source_record_id,
            source_file_id=source_file_id,
            source_disclosure_date=source_disclosure_date,
            source_posted_date=source_posted_date,
            profile_id=profile_id,
        )
        job = processing_service.process(document.document_id, chunk_options)
        document = document_service.get(document.document_id)
        quality_report = repository.get_quality_report(document.document_id)
        latest_run = _latest_run(repository, document.document_id)
        if quality_report is None or latest_run is None:
            raise RuntimeError(f"Processing finished without quality/run records: {document.document_id}")
        row = build_success_row(path, document, job.job_id, quality_report, latest_run)
        row["apba_id"] = apba_id or row.get("apba_id") or ""
        row = _with_source_origin(
            row,
            source_record_id_origin=source_record_id_origin,
            source_disclosure_date_origin=source_disclosure_date_origin,
        )
        return _with_source_selection(row, source_selection_metadata)
    except Exception as exc:
        elapsed = round(time.perf_counter() - started, 3)
        return build_failure_row(
            path,
            error=exc,
            elapsed_seconds=elapsed,
            document=document,
            institution_name=institution_name,
            apba_id=apba_id,
            source_system=source_system,
            source_url=source_url,
            source_record_id=source_record_id,
            source_record_id_origin=source_record_id_origin,
            source_file_id=source_file_id,
            source_selection_metadata=source_selection_metadata,
            source_disclosure_date=source_disclosure_date,
            source_disclosure_date_origin=source_disclosure_date_origin,
            source_posted_date=source_posted_date,
            profile_id=profile_id,
        )


def _latest_run(repository: JsonRepository, document_id: str) -> ProcessingRun | None:
    return repository.latest_completed_run(document_id)


def build_success_row(
    input_path: Path,
    document: Document,
    job_id: str,
    quality_report: QualityReport,
    run: ProcessingRun,
) -> dict[str, Any]:
    row = _base_row(input_path, document)
    row.update(
        {
            "status": run.status,
            "error": run.error or "",
            "job_id": job_id,
            "elapsed_seconds": run.elapsed_seconds,
            **flatten_quality_report(quality_report),
            **flatten_agent_review(run),
            "agent_review_plan_json": run.artifacts.get("agent_review_plan.json", ""),
            "quality_json": run.artifacts.get("quality.json", ""),
            "quality_md": run.artifacts.get("quality.md", ""),
            "tables_csv": run.artifacts.get("tables.csv", ""),
            "tables_jsonl": run.artifacts.get("tables.jsonl", ""),
        }
    )
    return _ordered_row(row)


def build_reused_row(
    input_path: Path,
    document: Document,
    quality_report: QualityReport,
    run: ProcessingRun,
    elapsed_seconds: float,
) -> dict[str, Any]:
    row = build_success_row(input_path, document, run.job_id, quality_report, run)
    row["status"] = "skipped_unchanged"
    row["elapsed_seconds"] = elapsed_seconds
    row.update(historical_agent_review(run))
    row.update(current_batch_reuse_agent_review())
    return _ordered_row(row)


def current_batch_reuse_agent_review() -> dict[str, Any]:
    return {
        "agent_review_status": "skipped",
        "agent_review_skip_reason": "reused_unchanged",
        "agent_review_candidate_count": 0,
        "agent_review_cached_candidate_count": 0,
        "agent_review_new_candidate_count": 0,
        "agent_review_selected_count": 0,
        "agent_review_estimated_input_tokens": 0,
        "agent_review_estimated_output_tokens": 0,
        "agent_review_estimated_total_tokens": 0,
        "agent_review_budget_exhausted": False,
        "agent_review_budget_reservation_id": "",
        "agent_review_approval_reference": "",
        "agent_review_model": "",
        "agent_review_payload_hash": "",
        "agent_review_estimated_cost": "",
        "agent_review_actual_cost": "",
        "agent_review_provider_request_id": "",
        "agent_review_plan_json": "",
    }


def historical_agent_review(run: ProcessingRun) -> dict[str, Any]:
    agent_review = (run.stats or {}).get("agent_review") or {}
    return {
        "reused_from_run_id": run.run_id,
        "reused_from_job_id": run.job_id,
        "historical_agent_review_status": agent_review.get("status", ""),
        "historical_agent_review_selected_count": agent_review.get("selected_count", 0),
        "historical_agent_review_estimated_input_tokens": agent_review.get("estimated_input_tokens", 0),
        "historical_agent_review_estimated_output_tokens": agent_review.get("estimated_output_tokens", 0),
        "historical_agent_review_estimated_total_tokens": agent_review.get("estimated_total_tokens", 0),
        "historical_agent_review_api_call_count": agent_review.get("api_call_count", 0),
    }


def build_failure_row(
    input_path: Path,
    *,
    error: BaseException | str,
    elapsed_seconds: float,
    document: Document | None = None,
    institution_name: str | None = None,
    apba_id: str | None = None,
    source_system: str | None = None,
    source_url: str | None = None,
    source_record_id: str | None = None,
    source_record_id_origin: str | None = None,
    source_file_id: str | None = None,
    source_selection_metadata: dict[str, Any] | None = None,
    source_disclosure_date: str | None = None,
    source_disclosure_date_origin: str | None = None,
    source_posted_date: str | None = None,
    profile_id: str | None = DEFAULT_PROFILE_ID,
) -> dict[str, Any]:
    if document:
        row = _base_row(input_path, document)
        row["apba_id"] = apba_id or row.get("apba_id") or ""
    else:
        row = {
            "input_path": str(input_path),
            "filename": input_path.name,
            "document_id": "",
            "institution_name": institution_name or "",
            "apba_id": apba_id or "",
            "source_system": source_system or "",
            "source_url": source_url or "",
            "source_record_id": source_record_id or "",
            "source_record_id_origin": source_record_id_origin or "",
            "source_file_id": source_file_id or "",
            "source_disclosure_date": source_disclosure_date or "",
            "source_disclosure_date_origin": source_disclosure_date_origin or "",
            "source_posted_date": source_posted_date or "",
            "profile_id": profile_id or "",
        }
    row = _with_source_origin(
        row,
        source_record_id_origin=source_record_id_origin,
        source_disclosure_date_origin=source_disclosure_date_origin,
        ordered=False,
    )
    row = _with_source_selection(row, source_selection_metadata, ordered=False)
    classification = classify_processing_failure(error, filename=input_path.name)
    row.update(
        {
            "status": "failed",
            "error": str(error),
            **classification.as_row_fields(),
            "job_id": "",
            "elapsed_seconds": elapsed_seconds,
        }
    )
    return _ordered_row(row)


def _base_row(input_path: Path, document: Document) -> dict[str, Any]:
    return {
        "input_path": str(input_path),
        "filename": document.filename,
        "document_id": document.document_id,
        "institution_name": document.institution_name or "",
        "apba_id": "",
        "source_system": document.source_system or "",
        "source_url": document.source_url or "",
        "source_record_id": document.source_record_id or "",
        "source_record_id_origin": "",
        "source_file_id": document.source_file_id or "",
        "source_disclosure_date": document.source_disclosure_date or "",
        "source_disclosure_date_origin": "",
        "source_posted_date": document.source_posted_date or "",
        "profile_id": document.profile_id or "",
    }


def _with_source_origin(
    row: dict[str, Any],
    *,
    source_record_id_origin: str | None,
    source_disclosure_date_origin: str | None,
    ordered: bool = True,
) -> dict[str, Any]:
    row["source_record_id_origin"] = source_record_id_origin or row.get("source_record_id_origin") or ""
    row["source_disclosure_date_origin"] = (
        source_disclosure_date_origin
        or row.get("source_disclosure_date_origin")
        or ""
    )
    return _ordered_row(row) if ordered else row


def _source_selection_metadata(entry: dict[str, Any]) -> dict[str, Any]:
    return {column: entry.get(column) for column in PUBLIC_PORTAL_SOURCE_SELECTION_COLUMNS}


def _with_source_selection(
    row: dict[str, Any],
    source_selection_metadata: dict[str, Any] | None,
    *,
    ordered: bool = True,
) -> dict[str, Any]:
    metadata = source_selection_metadata or {}
    for column in PUBLIC_PORTAL_SOURCE_SELECTION_COLUMNS:
        row[column] = metadata.get(column) or row.get(column) or ""
    return _ordered_row(row) if ordered else row


def flatten_quality_report(report: QualityReport) -> dict[str, Any]:
    table_metrics = report.table_metrics or {}
    structure_metrics = report.structure_metrics or {}
    coverage_metrics = report.coverage_metrics or {}
    return {
        "quality_passed": report.passed,
        "quality_score": report.score,
        "node_count": report.node_count,
        "chunk_count": report.chunk_count,
        "issue_count": report.issue_count,
        "error_count": report.error_count,
        "warning_count": report.warning_count,
        "failed_error_check_count": report.failed_error_check_count,
        "failed_warning_check_count": report.failed_warning_check_count,
        "failed_info_check_count": sum(1 for check in report.checks if check.severity == "info" and not check.passed),
        "recommendation_count": len(report.recommendations or []),
        "top_recommendation": (report.recommendations or [""])[0],
        "chunk_to_source_char_ratio": _metric(coverage_metrics, "chunk_to_source_char_ratio"),
        "table_like_chunks": _metric(table_metrics, "table_like_chunks"),
        "table_citation_ready_chunks": _metric(table_metrics, "table_citation_ready_chunks"),
        "chunks_with_table_cell_rows": _metric(table_metrics, "chunks_with_table_cell_rows"),
        "table_like_without_cell_rows": _metric(table_metrics, "table_like_without_cell_rows"),
        "table_cell_row_count": _metric(table_metrics, "table_cell_row_count"),
        "probable_table_false_positive_chunks": _metric(table_metrics, "probable_table_false_positive_chunks"),
        "stable_table_false_positive_chunks": _metric(table_metrics, "stable_table_false_positive_chunks"),
        "table_false_positive_attention_chunks": _metric(table_metrics, "table_false_positive_attention_chunks"),
        "probable_table_extraction_failed_chunks": _metric(table_metrics, "probable_table_extraction_failed_chunks"),
        "duplicate_regulation_node_count": _metric(structure_metrics, "duplicate_regulation_node_count"),
        "zero_chunk_regulation_node_count": _metric(structure_metrics, "zero_chunk_regulation_node_count"),
        "chunks_missing_regulation_no": _metric(structure_metrics, "chunks_missing_regulation_no"),
        "article_chunks_missing_regulation_no": _metric(structure_metrics, "article_chunks_missing_regulation_no"),
        "detected_reg_no_without_chunk_metadata_count": _metric(structure_metrics, "detected_reg_no_without_chunk_metadata_count"),
    }


def flatten_agent_review(run: ProcessingRun) -> dict[str, Any]:
    agent_review = (run.stats or {}).get("agent_review") or {}
    return {
        "agent_review_status": agent_review.get("status", ""),
        "agent_review_skip_reason": agent_review.get("skip_reason", ""),
        "agent_review_candidate_count": agent_review.get("candidate_count", ""),
        "agent_review_cached_candidate_count": agent_review.get("cached_candidate_count", ""),
        "agent_review_new_candidate_count": agent_review.get("new_candidate_count", ""),
        "agent_review_selected_count": agent_review.get("selected_count", ""),
        "agent_review_estimated_input_tokens": agent_review.get("estimated_input_tokens", ""),
        "agent_review_estimated_output_tokens": agent_review.get("estimated_output_tokens", ""),
        "agent_review_estimated_total_tokens": agent_review.get("estimated_total_tokens", ""),
        "agent_review_budget_exhausted": agent_review.get("budget_exhausted", ""),
        "agent_review_budget_reservation_id": agent_review.get("budget_reservation_id", ""),
        "agent_review_approval_reference": agent_review.get("approval_reference", ""),
        "agent_review_model": agent_review.get("model", agent_review.get("approved_model", "")),
        "agent_review_payload_hash": agent_review.get("payload_hash", ""),
        "agent_review_estimated_cost": agent_review.get("estimated_cost", ""),
        "agent_review_actual_cost": agent_review.get("actual_cost", ""),
        "agent_review_provider_request_id": agent_review.get("provider_request_id", ""),
    }


def _metric(metrics: dict[str, Any], key: str, default: int | float = 0) -> int | float:
    value = metrics.get(key, default)
    return value if isinstance(value, int | float) else default


def _ordered_row(row: dict[str, Any]) -> dict[str, Any]:
    return {column: row.get(column, "") for column in SUMMARY_COLUMNS}


def build_batch_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [row for row in rows if row.get("status") == "completed"]
    skipped_unchanged = [row for row in rows if row.get("status") == "skipped_unchanged"]
    successful = completed + skipped_unchanged
    failed = [row for row in rows if row.get("status") == "failed"]
    failure_category_counts: dict[str, int] = {}
    for row in failed:
        category = str(row.get("failure_category") or "unclassified")
        failure_category_counts[category] = failure_category_counts.get(category, 0) + 1
    ocr_required_count = sum(1 for row in failed if _bool_row_value(row, "ocr_required"))
    ocr_required_page_count = sum(_numeric_row_value(row, "ocr_page_count") for row in failed)
    retry_recommended_failed_count = sum(1 for row in failed if _bool_row_value(row, "retry_recommended"))
    passed = [row for row in successful if row.get("quality_passed") is True]
    scores = [float(row["quality_score"]) for row in successful if row.get("quality_score") not in ("", None)]
    agent_review_selected_total = sum(_numeric_row_value(row, "agent_review_selected_count") for row in completed)
    agent_review_estimated_input_tokens_total = sum(
        _numeric_row_value(row, "agent_review_estimated_input_tokens") for row in completed
    )
    agent_review_estimated_output_tokens_total = sum(
        _numeric_row_value(row, "agent_review_estimated_output_tokens") for row in completed
    )
    agent_review_estimated_total_tokens_total = sum(
        _numeric_row_value(row, "agent_review_estimated_total_tokens") for row in completed
    )
    agent_review_estimated_cost_total = sum(
        (_decimal_row_value(row, "agent_review_estimated_cost") for row in completed),
        Decimal("0"),
    )
    agent_review_cost_missing_count = sum(
        1
        for row in completed
        if _numeric_row_value(row, "agent_review_selected_count") > 0
        and _decimal_row_value(row, "agent_review_estimated_cost", missing=None) is None
    )
    agent_review_budget_exhausted_count = sum(1 for row in completed if row.get("agent_review_budget_exhausted") is True)
    agent_review_selected_document_total = sum(
        1 for row in completed if _numeric_row_value(row, "agent_review_selected_count") > 0
    )
    historical_agent_review_selected_total = sum(
        _numeric_row_value(row, "historical_agent_review_selected_count") for row in skipped_unchanged
    )
    historical_agent_review_estimated_input_tokens_total = sum(
        _numeric_row_value(row, "historical_agent_review_estimated_input_tokens") for row in skipped_unchanged
    )
    historical_agent_review_estimated_output_tokens_total = sum(
        _numeric_row_value(row, "historical_agent_review_estimated_output_tokens") for row in skipped_unchanged
    )
    historical_agent_review_estimated_total_tokens_total = sum(
        _numeric_row_value(row, "historical_agent_review_estimated_total_tokens") for row in skipped_unchanged
    )
    historical_agent_review_api_call_count_total = sum(
        _numeric_row_value(row, "historical_agent_review_api_call_count") for row in skipped_unchanged
    )
    probable_table_false_positive_total = sum(
        _numeric_row_value(row, "probable_table_false_positive_chunks") for row in successful
    )
    table_like_total = sum(_numeric_row_value(row, "table_like_chunks") for row in successful)
    table_citation_ready_total = sum(
        _numeric_row_value(row, "table_citation_ready_chunks") for row in successful
    )
    table_like_without_cell_rows_total = sum(
        _numeric_row_value(row, "table_like_without_cell_rows") for row in successful
    )
    stable_table_false_positive_total = sum(
        _numeric_row_value(row, "stable_table_false_positive_chunks") for row in successful
    )
    table_false_positive_attention_total = sum(
        _numeric_row_value(row, "table_false_positive_attention_chunks") for row in successful
    )
    failed_info_check_total = sum(_numeric_row_value(row, "failed_info_check_count") for row in successful)
    recommendation_total = sum(_numeric_row_value(row, "recommendation_count") for row in successful)
    apba_id_counts = Counter(str(row.get("apba_id") or "missing") for row in rows)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_count": len(rows),
        "completed_count": len(completed),
        "skipped_unchanged_count": len(skipped_unchanged),
        "successful_count": len(successful),
        "failed_count": len(failed),
        "failure_category_counts": failure_category_counts,
        "ocr_required_count": ocr_required_count,
        "ocr_required_page_count": ocr_required_page_count,
        "retry_recommended_failed_count": retry_recommended_failed_count,
        "quality_passed_count": len(passed),
        "average_quality_score": round(sum(scores) / len(scores), 3) if scores else 0.0,
        "agent_review_selected_total": agent_review_selected_total,
        "agent_review_selected_document_total": agent_review_selected_document_total,
        "agent_review_estimated_input_tokens_total": agent_review_estimated_input_tokens_total,
        "agent_review_estimated_output_tokens_total": agent_review_estimated_output_tokens_total,
        "agent_review_estimated_total_tokens_total": agent_review_estimated_total_tokens_total,
        "agent_review_estimated_cost_total": _decimal_to_string(agent_review_estimated_cost_total),
        "agent_review_cost_missing_count": agent_review_cost_missing_count,
        "agent_review_budget_exhausted_count": agent_review_budget_exhausted_count,
        "agent_review_batch_budget_exceeded": False,
        "agent_review_batch_budget_errors": [],
        "historical_agent_review_selected_total": historical_agent_review_selected_total,
        "historical_agent_review_estimated_input_tokens_total": historical_agent_review_estimated_input_tokens_total,
        "historical_agent_review_estimated_output_tokens_total": historical_agent_review_estimated_output_tokens_total,
        "historical_agent_review_estimated_total_tokens_total": historical_agent_review_estimated_total_tokens_total,
        "historical_agent_review_api_call_count_total": historical_agent_review_api_call_count_total,
        "table_like_total": table_like_total,
        "table_citation_ready_total": table_citation_ready_total,
        "table_like_without_cell_rows_total": table_like_without_cell_rows_total,
        "probable_table_false_positive_total": probable_table_false_positive_total,
        "stable_table_false_positive_total": stable_table_false_positive_total,
        "table_false_positive_attention_total": table_false_positive_attention_total,
        "failed_info_check_total": failed_info_check_total,
        "recommendation_total": recommendation_total,
        "apba_id_counts": dict(sorted(apba_id_counts.items())),
        "rows": rows,
    }


def apply_agent_review_batch_budget(summary: dict[str, Any], settings: Settings) -> dict[str, Any]:
    selected_documents = int(summary.get("agent_review_selected_document_total") or 0)
    selected_tokens = int(summary.get("agent_review_estimated_input_tokens_total") or 0)
    selected_output_tokens = int(summary.get("agent_review_estimated_output_tokens_total") or 0)
    selected_total_tokens = int(summary.get("agent_review_estimated_total_tokens_total") or 0)
    selected_cost = _decimal_value(summary.get("agent_review_estimated_cost_total"), missing=Decimal("0"))
    missing_cost_count = int(summary.get("agent_review_cost_missing_count") or 0)
    prompt_input_tokens = max(0, int(settings.agent_review_prompt_input_tokens_per_batch))
    selected_input_tokens_with_prompt = selected_tokens + (prompt_input_tokens if selected_documents > 0 else 0)
    selected_total_tokens_with_prompt = (
        max(
            selected_total_tokens + prompt_input_tokens,
            _with_safety_margin(selected_input_tokens_with_prompt + selected_output_tokens, settings),
        )
        if selected_documents > 0
        else selected_total_tokens
    )
    max_documents = max(0, int(settings.agent_review_max_documents_per_batch))
    max_tokens = max(0, int(settings.agent_review_max_input_tokens_per_batch))
    max_total_tokens = max(0, int(settings.agent_review_max_total_tokens_per_batch))
    max_cost = _decimal_value(settings.agent_review_max_cost_per_batch, missing=Decimal("0"))
    input_price = _decimal_value(settings.agent_review_input_price_per_1m_tokens, missing=Decimal("0"))
    output_price = _decimal_value(settings.agent_review_output_price_per_1m_tokens, missing=Decimal("0"))
    selected_cost_with_prompt = (
        _money((selected_cost or Decimal("0")) + _token_cost(prompt_input_tokens, input_price or Decimal("0")))
        if selected_documents > 0
        else selected_cost
    )
    errors: list[str] = []
    if selected_documents > 0 and max_documents <= 0:
        errors.append("Set AGENT_REVIEW_MAX_DOCUMENTS_PER_BATCH before enabling billable agent review.")
    elif max_documents > 0 and selected_documents > max_documents:
        errors.append(
            f"Agent review selected {selected_documents} documents, above batch cap {max_documents}."
        )
    if selected_documents > 0 and prompt_input_tokens <= 0:
        errors.append("Set AGENT_REVIEW_PROMPT_INPUT_TOKENS_PER_BATCH before enabling billable agent review.")
    if selected_input_tokens_with_prompt > 0 and max_tokens <= 0:
        errors.append("Set AGENT_REVIEW_MAX_INPUT_TOKENS_PER_BATCH before enabling billable agent review.")
    elif max_tokens > 0 and selected_input_tokens_with_prompt > max_tokens:
        errors.append(
            f"Agent review selected {selected_input_tokens_with_prompt} input tokens including prompt, above batch cap {max_tokens}."
        )
    if selected_total_tokens_with_prompt > 0 and max_total_tokens <= 0:
        errors.append("Set AGENT_REVIEW_MAX_TOTAL_TOKENS_PER_BATCH before enabling billable agent review.")
    elif max_total_tokens > 0 and selected_total_tokens_with_prompt > max_total_tokens:
        errors.append(
            "Agent review selected "
            f"{selected_total_tokens_with_prompt} total tokens including prompt and safety margin, "
            f"above batch cap {max_total_tokens}."
        )
    if selected_documents > 0 and max_cost <= 0:
        errors.append("Set AGENT_REVIEW_MAX_COST_PER_BATCH before enabling billable agent review.")
    if selected_input_tokens_with_prompt > 0 and input_price <= 0:
        errors.append("Set AGENT_REVIEW_INPUT_PRICE_PER_1M_TOKENS before enabling billable agent review.")
    if selected_output_tokens > 0 and output_price <= 0:
        errors.append("Set AGENT_REVIEW_OUTPUT_PRICE_PER_1M_TOKENS before enabling billable agent review.")
    if missing_cost_count > 0:
        errors.append(
            f"Agent review has {missing_cost_count} selected document(s) without a numeric estimated cost."
        )
    elif max_cost > 0 and selected_cost_with_prompt and selected_cost_with_prompt > max_cost:
        errors.append(f"Agent review estimated cost {selected_cost_with_prompt} exceeds batch cost cap {max_cost}.")

    summary["agent_review_batch_max_documents"] = max_documents
    summary["agent_review_batch_max_input_tokens"] = max_tokens
    summary["agent_review_batch_max_total_tokens"] = max_total_tokens
    summary["agent_review_batch_max_cost"] = _decimal_to_string(max_cost)
    summary["agent_review_batch_budget_currency"] = settings.agent_review_budget_currency
    summary["agent_review_prompt_input_tokens_total"] = prompt_input_tokens if selected_documents > 0 else 0
    summary["agent_review_estimated_input_tokens_with_prompt_total"] = selected_input_tokens_with_prompt
    summary["agent_review_estimated_total_tokens_with_prompt_total"] = selected_total_tokens_with_prompt
    summary["agent_review_estimated_cost_with_prompt_total"] = _decimal_to_string(selected_cost_with_prompt)
    summary["agent_review_batch_budget_exceeded"] = bool(errors)
    summary["agent_review_batch_budget_errors"] = errors
    return summary


def agent_review_batch_budget_is_fatal(summary: dict[str, Any]) -> bool:
    if not summary.get("agent_review_batch_budget_exceeded"):
        return False
    rows = summary.get("rows") or []
    selected_rows = [
        row
        for row in rows
        if int(row.get("agent_review_selected_count") or 0) > 0
    ]
    if not selected_rows:
        return False
    non_billable_statuses = {
        "api_configuration_needed",
        "disabled",
        "skipped",
        "not_requested",
    }
    return any(str(row.get("agent_review_status") or "").strip() not in non_billable_statuses for row in selected_rows)


def load_manifest_entries(
    manifest_csv: Path,
    *,
    institution_name: str | None = None,
    source_system: str | None = None,
    source_url: str | None = None,
    profile_id: str | None = DEFAULT_PROFILE_ID,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    manifest_dir = manifest_csv.resolve().parent
    with manifest_csv.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            raw_path = row.get("input_path") or row.get("downloaded_path") or row.get("path")
            if not raw_path:
                continue
            if _manifest_false(row.get("supported_by_preprocessor")):
                continue
            document_name, _document_name_origin = _first_manifest_value(
                row,
                ("document_name", "title", "rule_title", "file_name"),
            )
            source_record_id, source_record_id_origin = _first_manifest_value(
                row,
                ("source_record_id", "board_no", "rule_idx"),
            )
            source_disclosure_date, source_disclosure_date_origin = _first_manifest_value(
                row,
                ("source_disclosure_date", "disclosure_date", "revision_date"),
            )
            path = Path(raw_path)
            if not path.is_absolute():
                manifest_candidate = (manifest_dir / path).resolve()
                cwd_candidate = path.resolve()
                path = manifest_candidate if manifest_candidate.exists() else cwd_candidate
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                raise ValueError(f"Manifest row points to unsupported file extension: {path.suffix.lower()}")
            entries.append(
                {
                    "path": path,
                    "document_name": document_name,
                    "institution_name": row.get("institution_name") or institution_name,
                    "apba_id": row.get("apba_id") or None,
                    "source_system": row.get("source_system") or source_system,
                    "source_url": row.get("source_url") or row.get("detail_url") or source_url,
                    "source_record_id": source_record_id,
                    "source_record_id_origin": source_record_id_origin,
                    "source_file_id": row.get("source_file_id") or row.get("file_no") or None,
                    **{
                        column: row.get(column) or None
                        for column in PUBLIC_PORTAL_SOURCE_SELECTION_COLUMNS
                    },
                    "source_disclosure_date": source_disclosure_date,
                    "source_disclosure_date_origin": source_disclosure_date_origin,
                    "source_posted_date": row.get("source_posted_date") or row.get("posted_date") or None,
                    "file_sha256": row.get("file_sha256") or None,
                    "profile_id": _manifest_profile_id(row, profile_id),
                }
            )
    return dedupe_manifest_entries(entries)


def _manifest_profile_id(row: dict[str, Any], default_profile_id: str | None) -> str | None:
    explicit = str(row.get("profile_id") or "").strip()
    if explicit:
        return explicit
    apba_id = str(row.get("apba_id") or "").strip()
    source_system = str(row.get("source_system") or "").strip().upper()
    if apba_id and source_system in {"", "PUBLIC_PORTAL"}:
        normalized = re.sub(r"[^a-z0-9]+", "-", apba_id.lower()).strip("-")
        if normalized:
            return f"public_portal-{normalized}"
    return default_profile_id


def _manifest_false(value: Any) -> bool:
    return str(value or "").strip().lower() in {"false", "0", "no", "n"}


def _first_manifest_value(row: dict[str, Any], field_names: Iterable[str]) -> tuple[str | None, str | None]:
    for field_name in field_names:
        value = row.get(field_name)
        if value:
            return value, field_name
    return None, None


def dedupe_manifest_entries(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped_by_key: dict[tuple[str, ...], dict[str, Any]] = {}
    key_order: list[tuple[str, ...]] = []
    for entry in entries:
        key = manifest_entry_identity(entry)
        if key not in deduped_by_key:
            key_order.append(key)
            deduped_by_key[key] = entry
            continue
        deduped_by_key[key] = _preferred_manifest_entry(deduped_by_key[key], entry)
    return [deduped_by_key[key] for key in key_order]


def manifest_entry_identity(entry: dict[str, Any]) -> tuple[str, ...]:
    source_system = _identity_value(entry.get("source_system"))
    source_record_id = _identity_value(entry.get("source_record_id"))
    source_file_id = _identity_value(entry.get("source_file_id"))
    if source_system and source_record_id and source_file_id:
        return ("source", source_system, source_record_id, source_file_id)
    if source_system and source_file_id:
        return ("source_file", source_system, source_file_id)
    file_sha256 = _identity_value(entry.get("file_sha256"))
    if file_sha256:
        return ("hash", file_sha256)
    return ("path", str(Path(entry["path"]).resolve()).lower())


def _identity_value(value: Any) -> str:
    return str(value or "").strip().lower()


def _numeric_row_value(row: dict[str, Any], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or value in ("", None):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(float(str(value)))
    except ValueError:
        return 0


def _decimal_row_value(row: dict[str, Any], key: str, *, missing: Decimal | None = Decimal("0")) -> Decimal | None:
    return _decimal_value(row.get(key), missing=missing)


def _decimal_value(value: Any, *, missing: Decimal | None = Decimal("0")) -> Decimal | None:
    if isinstance(value, bool) or value in ("", None):
        return missing
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return missing


def _decimal_to_string(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value.normalize(), "f")


def _token_cost(tokens: int, price_per_1m_tokens: Decimal) -> Decimal:
    return _money(Decimal(max(0, tokens)) / Decimal("1000000") * price_per_1m_tokens)


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), ROUND_HALF_UP)


def _with_safety_margin(tokens: int, settings: Settings) -> int:
    margin = max(1.0, float(settings.agent_review_token_safety_margin))
    return int((tokens * margin) + 0.999999)


def _bool_row_value(row: dict[str, Any], key: str) -> bool:
    value = row.get(key)
    if isinstance(value, bool):
        return value
    if value in ("", None):
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _preferred_manifest_entry(existing: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    if _manifest_entry_quality(candidate) > _manifest_entry_quality(existing):
        return candidate
    return existing


def _manifest_entry_quality(entry: dict[str, Any]) -> int:
    score = 0
    if Path(entry["path"]).exists():
        score += 8
    if entry.get("file_sha256"):
        score += 4
    metadata_fields = [
        "document_name",
        "institution_name",
        "source_url",
        "source_record_id",
        "source_file_id",
        "source_disclosure_date",
        "source_posted_date",
        "profile_id",
    ]
    score += sum(1 for field in metadata_fields if entry.get(field))
    return score


def apply_institution_profile_defaults(
    entries: Iterable[dict[str, Any]],
    registry: InstitutionProfileRegistry | None,
    *,
    strict: bool = False,
) -> list[dict[str, Any]]:
    if registry is None:
        return [dict(entry) for entry in entries]
    resolved_entries: list[dict[str, Any]] = []
    for entry in entries:
        resolved = dict(entry)
        profile_id = resolved.get("profile_id") or registry.default_profile_id or DEFAULT_PROFILE_ID
        profile = registry.resolve(profile_id, strict=strict)
        if profile is not None:
            resolved["profile_id"] = profile.profile_id
            for field in ("institution_name", "source_system", "source_url"):
                if not resolved.get(field):
                    resolved[field] = getattr(profile, field)
        else:
            resolved["profile_id"] = profile_id
        resolved_entries.append(resolved)
    return resolved_entries


def attach_institution_profile_registry_summary(
    summary: dict[str, Any],
    registry: InstitutionProfileRegistry | None,
) -> dict[str, Any]:
    if registry is None:
        return summary
    row_profile_ids = sorted(
        {
            str(row.get("profile_id") or "").strip()
            for row in summary.get("rows", [])
            if str(row.get("profile_id") or "").strip()
        }
    )
    known = {profile.profile_id.lower() for profile in registry.profiles.values()}
    summary["institution_profile_registry"] = registry.summary()
    summary["institution_profile_ids_used"] = row_profile_ids
    summary["unknown_institution_profile_ids"] = [
        profile_id for profile_id in row_profile_ids if profile_id.lower() not in known
    ]
    return summary


def write_reports(summary: dict[str, Any], reports_dir: Path, *, prefix: str = "batch_quality") -> dict[str, str]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base_path = reports_dir / f"{prefix}_{timestamp}"
    json_path = base_path.with_suffix(".json")
    csv_path = base_path.with_suffix(".csv")
    md_path = base_path.with_suffix(".md")

    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_COLUMNS)
        writer.writeheader()
        writer.writerows(summary["rows"])
    md_path.write_text(to_markdown(summary), encoding="utf-8")
    return {"json": str(json_path), "csv": str(csv_path), "markdown": str(md_path)}


def write_kordoc_sidecar_report(
    entries: Iterable[dict[str, Any]],
    reports_dir: Path,
    *,
    kordoc_command: str,
    data_dir: Path,
    timeout_seconds: int = 120,
    builder: Callable[..., dict[str, Any]] = build_crosscheck_report,
) -> dict[str, str]:
    input_paths = sorted({Path(entry["path"]).resolve() for entry in entries}, key=lambda path: str(path).lower())
    report = builder(
        input_paths,
        kordoc_command=kordoc_command,
        data_dir=data_dir,
        timeout_seconds=timeout_seconds,
    )
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base_path = reports_dir / f"kordoc_sidecar_crosscheck_{timestamp}"
    json_path = base_path.with_suffix(".json")
    md_path = base_path.with_suffix(".md")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(kordoc_markdown_report(report), encoding="utf-8")
    return {
        "kordoc_sidecar_json": str(json_path),
        "kordoc_sidecar_markdown": str(md_path),
    }


def to_markdown(summary: dict[str, Any]) -> str:
    rows = summary.get("rows", [])
    lines = [
        "# Public Institution Regulation Batch Quality Report",
        "",
        f"- Generated at: {summary.get('generated_at')}",
        f"- Inputs: {summary.get('input_count', 0)}",
        f"- Completed: {summary.get('completed_count', 0)}",
        f"- Skipped unchanged: {summary.get('skipped_unchanged_count', 0)}",
        f"- Successful total: {summary.get('successful_count', summary.get('completed_count', 0))}",
        f"- Failed: {summary.get('failed_count', 0)}",
        f"- OCR required: {summary.get('ocr_required_count', 0)}",
        f"- OCR required pages: {summary.get('ocr_required_page_count', 0)}",
        f"- Retry-recommended failures: {summary.get('retry_recommended_failed_count', 0)}",
        f"- Quality passed: {summary.get('quality_passed_count', 0)}",
        f"- Average quality score: {summary.get('average_quality_score', 0.0)}",
        f"- Failed info checks: {summary.get('failed_info_check_total', 0)}",
        f"- Recommendations: {summary.get('recommendation_total', 0)}",
        f"- Table-like chunks: {summary.get('table_like_total', 0)}",
        f"- Citation-ready table chunks: {summary.get('table_citation_ready_total', 0)}",
        f"- Table-like chunks without cell rows: {summary.get('table_like_without_cell_rows_total', 0)}",
        f"- Probable table false positives: {summary.get('probable_table_false_positive_total', 0)}",
        f"- Stable table false positives: {summary.get('stable_table_false_positive_total', 0)}",
        f"- Table false-positive attention: {summary.get('table_false_positive_attention_total', 0)}",
        f"- AI review selected chunks: {summary.get('agent_review_selected_total', 0)}",
        f"- AI review selected documents: {summary.get('agent_review_selected_document_total', 0)}",
        f"- AI review estimated input tokens: {summary.get('agent_review_estimated_input_tokens_total', 0)}",
        f"- AI review estimated output tokens: {summary.get('agent_review_estimated_output_tokens_total', 0)}",
        f"- AI review estimated total tokens: {summary.get('agent_review_estimated_total_tokens_total', 0)}",
        f"- AI review estimated cost: {summary.get('agent_review_estimated_cost_total', 0)}",
        f"- AI review prompt input tokens: {summary.get('agent_review_prompt_input_tokens_total', 0)}",
        f"- AI review estimated input tokens with prompt: {summary.get('agent_review_estimated_input_tokens_with_prompt_total', 0)}",
        f"- AI review estimated total tokens with prompt: {summary.get('agent_review_estimated_total_tokens_with_prompt_total', 0)}",
        f"- AI review estimated cost with prompt: {summary.get('agent_review_estimated_cost_with_prompt_total', 0)}",
        f"- AI review cost missing documents: {summary.get('agent_review_cost_missing_count', 0)}",
        f"- AI review budget exhausted: {summary.get('agent_review_budget_exhausted_count', 0)}",
        f"- AI review batch budget exceeded: {summary.get('agent_review_batch_budget_exceeded', False)}",
        f"- Historical AI review selected chunks on reused runs: {summary.get('historical_agent_review_selected_total', 0)}",
        f"- Historical AI review estimated input tokens on reused runs: {summary.get('historical_agent_review_estimated_input_tokens_total', 0)}",
        f"- Historical AI review estimated output tokens on reused runs: {summary.get('historical_agent_review_estimated_output_tokens_total', 0)}",
        f"- Historical AI review estimated total tokens on reused runs: {summary.get('historical_agent_review_estimated_total_tokens_total', 0)}",
        f"- Historical AI review API calls on reused runs: {summary.get('historical_agent_review_api_call_count_total', 0)}",
    ]
    if summary.get("report_type") == "batch_quality_filtered":
        lines.extend(
            [
                f"- Source batch report: {summary.get('source_batch_report_file', '')}",
                f"- Source batch SHA-256: {summary.get('source_batch_report_sha256', '')}",
                f"- Filter excluded OCR-required rows: {summary.get('filter_exclude_ocr_required', False)}",
                f"- Excluded rows: {summary.get('excluded_count', 0)}",
                f"- Excluded OCR-required rows: {summary.get('excluded_ocr_required_count', 0)}",
            ]
        )
    for error in summary.get("agent_review_batch_budget_errors", []):
        lines.append(f"- AI review batch budget error: {error}")
    lines.extend(
        [
            f"- AI review batch max documents: {summary.get('agent_review_batch_max_documents', 0)}",
            f"- AI review batch max input tokens: {summary.get('agent_review_batch_max_input_tokens', 0)}",
            f"- AI review batch max total tokens: {summary.get('agent_review_batch_max_total_tokens', 0)}",
            f"- AI review batch max cost: {summary.get('agent_review_batch_max_cost', 0)}",
            f"- AI review batch budget currency: {summary.get('agent_review_batch_budget_currency', '')}",
            "",
            "| file | document_id | status | score | warnings | chunks | table gaps | missing reg | ai review | ai chunks | ai tokens | elapsed |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |",
        ]
    )
    for row in rows:
        lines.append(
            "| {filename} | {document_id} | {status} | {quality_score} | {warning_count} | {chunk_count} | "
            "{table_like_without_cell_rows} | {chunks_missing_regulation_no} | {agent_review_status} | "
            "{agent_review_selected_count} | {agent_review_estimated_input_tokens} | {elapsed_seconds} |".format(
                filename=_escape_md(str(row.get("filename", ""))),
                document_id=_escape_md(str(row.get("document_id", ""))),
                status=_escape_md(str(row.get("status", ""))),
                quality_score=row.get("quality_score", ""),
                warning_count=row.get("warning_count", ""),
                chunk_count=row.get("chunk_count", ""),
                table_like_without_cell_rows=row.get("table_like_without_cell_rows", ""),
                chunks_missing_regulation_no=row.get("chunks_missing_regulation_no", ""),
                agent_review_status=_escape_md(str(row.get("agent_review_status", ""))),
                agent_review_selected_count=row.get("agent_review_selected_count", ""),
                agent_review_estimated_input_tokens=row.get("agent_review_estimated_input_tokens", ""),
                elapsed_seconds=row.get("elapsed_seconds", ""),
            )
        )
    return "\n".join(lines) + "\n"


def _escape_md(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch-process public institution regulation documents.")
    parser.add_argument("inputs", nargs="*", help="Files or directories containing PDF, DOCX, HWPX, or HWP regulation documents.")
    parser.add_argument("--manifest-csv", default=None, help="CSV with input_path or downloaded_path plus per-file metadata.")
    parser.add_argument("--patterns", default=",".join(DEFAULT_GLOB_PATTERNS), help="Comma-separated glob patterns for directories.")
    parser.add_argument("--non-recursive", action="store_true", help="Do not recurse into input directories.")
    parser.add_argument("--data-dir", default="./data", help="Repository/upload/export directory.")
    parser.add_argument("--reports-dir", default="./reports", help="Directory for batch summary reports.")
    parser.add_argument("--institution-name", default=None, help="Institution name to attach to each uploaded document.")
    parser.add_argument("--source-system", default=None, help="Source system label, for example PUBLIC_PORTAL or internal.")
    parser.add_argument("--source-url", default=None, help="Source URL shared by this batch.")
    parser.add_argument("--profile-id", default=DEFAULT_PROFILE_ID, help="Institution/document profile identifier.")
    parser.add_argument(
        "--institution-profiles",
        default=None,
        help="JSON registry with institution profile defaults and required provenance fields.",
    )
    parser.add_argument(
        "--strict-institution-profiles",
        action="store_true",
        help="Fail if a row profile_id is not present in --institution-profiles.",
    )
    parser.add_argument("--quality-profiles", default=None, help="JSON file with default and profile-specific quality thresholds.")
    parser.add_argument("--strict-quality-profiles", action="store_true", help="Fail if a document profile_id is not present in --quality-profiles.")
    parser.add_argument("--max-upload-mb", type=int, default=None, help="Batch upload size limit.")
    parser.add_argument("--max-chunk-chars", type=int, default=1800)
    parser.add_argument("--min-chunk-chars", type=int, default=300)
    parser.add_argument("--overlap-chars", type=int, default=120)
    parser.add_argument("--chunk-mode", choices=["article", "paragraph", "hybrid"], default="article")
    parser.add_argument("--no-context-header", action="store_true", help="Disable hierarchy context headers in chunk text.")
    parser.add_argument(
        "--pdf-ocr-backend",
        choices=["windows"],
        default=None,
        help="Optional OCR fallback for image-only PDFs. 'windows' uses the local Windows OCR engine.",
    )
    parser.add_argument("--pdf-ocr-language", default=None, help="Language tag for PDF OCR fallback, for example ko.")
    parser.add_argument("--pdf-ocr-render-scale", type=float, default=None, help="PDF page render scale before OCR.")
    parser.add_argument("--pdf-ocr-timeout-seconds", type=int, default=None, help="Timeout for the OCR subprocess.")
    parser.add_argument("--pdf-ocr-max-pages", type=int, default=None, help="Limit OCR to the first N PDF pages; omit for all pages.")
    parser.add_argument("--enable-agent-review", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--agent-review-max-documents-per-batch",
        type=int,
        default=None,
        help="Batch cap for documents with selected AI review chunks.",
    )
    parser.add_argument(
        "--agent-review-max-input-tokens-per-batch",
        type=int,
        default=None,
        help="Batch cap for selected AI review input tokens.",
    )
    parser.add_argument(
        "--agent-review-max-total-tokens-per-batch",
        type=int,
        default=None,
        help="Batch cap for selected AI review total tokens after safety margin.",
    )
    parser.add_argument(
        "--agent-review-max-cost-per-batch",
        type=float,
        default=None,
        help="Batch cap for estimated AI review provider cost.",
    )
    parser.add_argument(
        "--agent-review-prompt-input-tokens-per-batch",
        type=int,
        default=None,
        help="Reserved prompt input tokens to include in batch AI review token and cost caps.",
    )
    parser.add_argument(
        "--kordoc-crosscheck-command",
        default=None,
        help="Internal diagnostic only: run Kordoc as a sidecar and write a parser disagreement report.",
    )
    parser.add_argument(
        "--kordoc-crosscheck-timeout-seconds",
        type=int,
        default=120,
        help="Timeout per document for the Kordoc sidecar command.",
    )
    parser.add_argument("--force-reprocess", action="store_true", help="Ignore reusable completed runs and process every input again.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.inputs and not args.manifest_csv:
        raise SystemExit("Provide at least one input path or --manifest-csv.")
    registry = load_institution_profile_registry(args.institution_profiles) if args.institution_profiles else None
    base_profile = registry.resolve(args.profile_id, strict=args.strict_institution_profiles) if registry else None
    max_upload_mb = args.max_upload_mb or (base_profile.max_upload_mb if base_profile else None) or 500
    institution_name = args.institution_name or (base_profile.institution_name if base_profile else None)
    source_system = args.source_system or (base_profile.source_system if base_profile else None)
    source_url = args.source_url or (base_profile.source_url if base_profile else None)
    settings_kwargs: dict[str, Any] = {"data_dir": Path(args.data_dir), "max_upload_mb": max_upload_mb}
    if args.agent_review_max_documents_per_batch is not None:
        settings_kwargs["agent_review_max_documents_per_batch"] = args.agent_review_max_documents_per_batch
    if args.agent_review_max_input_tokens_per_batch is not None:
        settings_kwargs["agent_review_max_input_tokens_per_batch"] = args.agent_review_max_input_tokens_per_batch
    if args.agent_review_max_total_tokens_per_batch is not None:
        settings_kwargs["agent_review_max_total_tokens_per_batch"] = args.agent_review_max_total_tokens_per_batch
    if args.agent_review_max_cost_per_batch is not None:
        settings_kwargs["agent_review_max_cost_per_batch"] = args.agent_review_max_cost_per_batch
    if args.agent_review_prompt_input_tokens_per_batch is not None:
        settings_kwargs["agent_review_prompt_input_tokens_per_batch"] = args.agent_review_prompt_input_tokens_per_batch
    if args.quality_profiles:
        settings_kwargs["quality_profiles_path"] = args.quality_profiles
    if args.strict_quality_profiles:
        settings_kwargs["quality_profiles_strict"] = True
    if args.pdf_ocr_backend is not None:
        settings_kwargs["pdf_ocr_backend"] = args.pdf_ocr_backend
    if args.pdf_ocr_language is not None:
        settings_kwargs["pdf_ocr_language"] = args.pdf_ocr_language
    if args.pdf_ocr_render_scale is not None:
        settings_kwargs["pdf_ocr_render_scale"] = args.pdf_ocr_render_scale
    if args.pdf_ocr_timeout_seconds is not None:
        settings_kwargs["pdf_ocr_timeout_seconds"] = args.pdf_ocr_timeout_seconds
    if args.pdf_ocr_max_pages is not None:
        settings_kwargs["pdf_ocr_max_pages"] = args.pdf_ocr_max_pages
    settings = Settings(**settings_kwargs)
    chunk_options = ChunkOptions(
        max_chunk_chars=args.max_chunk_chars,
        min_chunk_chars=args.min_chunk_chars,
        overlap_chars=args.overlap_chars,
        chunk_mode=args.chunk_mode,
        include_context_header=not args.no_context_header,
        enable_agent_review=True,
    )
    entries: list[dict[str, Any]] = []
    if args.manifest_csv:
        entries.extend(
            load_manifest_entries(
                Path(args.manifest_csv),
                institution_name=institution_name,
                source_system=source_system,
                source_url=source_url,
                profile_id=args.profile_id,
            )
        )
    if args.inputs:
        files = collect_input_files(args.inputs, patterns=parse_patterns(args.patterns), recursive=not args.non_recursive)
        entries.extend(
            {
                "path": path,
                "document_name": None,
                "institution_name": institution_name,
                "source_system": source_system,
                "source_url": source_url,
                "source_record_id": None,
                "source_file_id": None,
                "source_disclosure_date": None,
                "source_posted_date": None,
                "profile_id": args.profile_id,
            }
            for path in files
        )
    entries = apply_institution_profile_defaults(entries, registry, strict=args.strict_institution_profiles)
    summary = process_entries(entries, settings=settings, chunk_options=chunk_options, force_reprocess=args.force_reprocess)
    attach_institution_profile_registry_summary(summary, registry)
    report_paths = write_reports(summary, Path(args.reports_dir))
    if args.kordoc_crosscheck_command:
        report_paths.update(
            write_kordoc_sidecar_report(
                entries,
                Path(args.reports_dir),
                kordoc_command=args.kordoc_crosscheck_command,
                data_dir=Path(args.data_dir) / "kordoc_crosscheck",
                timeout_seconds=args.kordoc_crosscheck_timeout_seconds,
            )
        )
    print(json.dumps({"summary": summary, "reports": report_paths}, ensure_ascii=False, indent=2))
    if agent_review_batch_budget_is_fatal(summary):
        return 2
    return 1 if summary["failed_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
