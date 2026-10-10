"""End-to-end regression harness for one combined regulation book (총규정).

The harness drives the same code path the operator console uses, in a fresh
disposable data directory, and compares what an MCP consumer finally sees with
the generator ground truth (``scripts/generate_synthetic_combined_regulation_book.py``):

1. upload    ``DocumentService.upload`` with full source metadata
2. process   ``ProcessingService.process`` (``ChunkOptions(enable_agent_review=False)``,
             no AI review, Kordoc disabled), progress callback recorded per phase
3. approve   approval evidence from the repository generators
             (``scripts.build_approval_worklist`` + ``scripts.build_approval_review_batches``),
             regenerated before every batch, then ``routes_documents.approve_review_chunks``.
             Nobody reviewed the chunks, so every request carries an explicit
             ``approval_override_reason`` and the route journals ``approved_without_review``
             for each chunk (the console's unreviewed bulk-approval contract).
4. index     ``routes_documents.index_document`` (local-jsonl, local hash embedding)
5. bundle    ``scripts.generate_mcp_client_config.write_mcp_runtime_data_bundle`` - the
             function behind the console's "MCP로 쓸 파일 묶음 만들기" button - followed by
             ``build_mcp_client_config`` + ``write_mcp_setup_bundle`` like the button does
6. MCP       the produced ``<bundle>/data`` is opened as an MCP consumer would: direct
             ``app.mcp_server.regulation_tools`` calls plus in-process FastMCP ``call_tool``
             for the ``chatgpt-data`` and ``full`` tool profiles.

Every stage records wall time, CPU time and peak RSS (``VmHWM`` reset per stage on
Linux, ``ru_maxrss`` otherwise), optionally ``tracemalloc`` peaks and one cProfile
``.prof`` file per stage. Checks never stop at the first failure; each check
reports pass/fail with counts and failure samples.

Usage (single run)::

    python scripts/run_combined_book_e2e_regression.py \
        --input book.docx --ground-truth book.ground_truth.json \
        --work-dir /tmp/e2e --out-json report.json --out-md report.md

``--runs N`` repeats the run in N fresh interpreters and writes an aggregate with
per-stage mean/stdev. ``--profile-dir DIR`` writes ``<label>_<stage>.prof`` files.
``--expect-approval-blocked`` runs the negative path for a book that must trip the
ambiguous combined-book boundary gate.
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import gc
import hashlib
import json
import math
import os
import platform
import re
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
import tracemalloc
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import HTTPException  # noqa: E402

from app.api import routes_documents  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.security import AuthContext  # noqa: E402
from app.core.tenant_access import settings_for_tenant, tenant_directory_key  # noqa: E402
from app.ingestion.embedding_adapter import LOCAL_HASH_EMBEDDING_MODEL  # noqa: E402
from app.mcp_server import regulation_tools  # noqa: E402
from app.mcp_server.regulation_server import create_regulation_mcp_server  # noqa: E402
from app.retrieval import tokenizer as retrieval_tokenizer  # noqa: E402
from app.retrieval.hierarchical_index import normalize_regulation_title  # noqa: E402
from app.schemas.chunk import ChunkOptions  # noqa: E402
from app.services.document_service import DocumentService  # noqa: E402
from app.services.processing_service import ProcessingService  # noqa: E402
from app.services.review_workflow_service import ambiguous_combined_book_chunk_ids  # noqa: E402
from app.storage.repository import JsonRepository  # noqa: E402
from scripts.build_approval_review_batches import build_approval_review_batches  # noqa: E402
from scripts.build_approval_worklist import build_approval_worklist  # noqa: E402
from scripts.generate_mcp_client_config import (  # noqa: E402
    build_mcp_client_config,
    write_mcp_runtime_data_bundle,
    write_mcp_setup_bundle,
)


HARNESS_VERSION = "1.0.0"
REPORT_TYPE = "combined_book_e2e_regression"
AGGREGATE_REPORT_TYPE = "combined_book_e2e_regression_aggregate"
DEFAULT_TENANT_ID = "default"
DEFAULT_PROFILE_ID = "synthetic-combined-book"
DEFAULT_SOURCE_SYSTEM = "synthetic-fixture"
DEFAULT_SOURCE_URL = "https://example.invalid/synthetic-combined-book"
DEFAULT_REGULATION_ID = "synthetic-combined-book"
DEFAULT_SERVER_NAME = "regulation_mcp"
HARNESS_ACTOR = "combined-book-e2e-harness"
UNREVIEWED_APPROVAL_REASON = (
    "Automated end-to-end regression approval of a synthetic corpus; "
    "no human or AI review was performed."
)
# Search quality is a metric, not an invariant. The defaults below are the
# floor measured on the 48-regulation baseline book (docx/pdf/hwpx); see the
# baseline report for the per-format numbers.
DEFAULT_MIN_SEARCH_TOP1 = 0.0
DEFAULT_MIN_SEARCH_TOP5 = 0.0
DEFAULT_APPROVAL_BATCHES = 3
DEFAULT_QUERY_REPEATS = 3
SEARCH_TOP_K = 5
BODY_SECTION_END_MARKERS = ("[답변분류]", "[참조]", "[표]")
AMBIGUOUS_APPROVAL_MESSAGE = "Ambiguous combined-book regulation boundaries"
CHATGPT_DATA_TOOLS = {
    "list_regulations",
    "get_regulation_toc",
    "get_regulation_article",
    "get_regulation_references",
    "list_regulation_reference_cycles",
    "search",
    "fetch",
}
# Checks that fail on the current main line because of product defects found
# by this harness. They are reported as failures; ``regression_passed`` only
# ignores them as long as their failure count does not grow beyond the count
# recorded in a baseline report passed with ``--compare-baseline``.
KNOWN_DEFECT_CHECKS: dict[str, str] = {}

CheckRecord = dict[str, Any]


# ---------------------------------------------------------------------------
# Measurement helpers
# ---------------------------------------------------------------------------


def _read_proc_status_kb(field: str) -> int | None:
    try:
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith(field + ":"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _reset_peak_rss() -> bool:
    """Reset the kernel peak-RSS counter (Linux ``clear_refs`` value 5)."""

    try:
        with open("/proc/self/clear_refs", "w", encoding="ascii") as handle:
            handle.write("5")
        return True
    except OSError:
        return False


def _ru_maxrss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, macOS bytes.
    return round(value / (1024 * 1024) if sys.platform == "darwin" else value / 1024, 1)


def _kb_to_mb(value: int | None) -> float | None:
    return None if value is None else round(value / 1024, 1)


def _percentile(values: Sequence[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percent / 100.0
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[int(rank)]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def _latency_summary(values_s: Sequence[float]) -> dict[str, Any]:
    values_ms = [value * 1000 for value in values_s]
    if not values_ms:
        return {"n": 0}
    return {
        "n": len(values_ms),
        "p50_ms": round(_percentile(values_ms, 50) or 0.0, 3),
        "p95_ms": round(_percentile(values_ms, 95) or 0.0, 3),
        "mean_ms": round(statistics.fmean(values_ms), 3),
        "max_ms": round(max(values_ms), 3),
        "total_s": round(sum(values_s), 4),
    }


class Redactor:
    """Remove host-specific absolute paths from report text."""

    def __init__(self, replacements: dict[str, str]) -> None:
        pairs = [(str(Path(key)), value) for key, value in replacements.items() if key]
        self._pairs = sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)

    def __call__(self, text: object) -> str:
        value = str(text)
        for needle, replacement in self._pairs:
            value = value.replace(needle, replacement)
        return re.sub(r"(?<![\w.])/(?:home|root|tmp|Users|var)/[^\s'\"<>]+", "<path>", value)


def _error_payload(exc: BaseException, redact: Redactor) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": type(exc).__name__, "message": redact(exc)[:2000]}
    if isinstance(exc, HTTPException):
        payload["status_code"] = exc.status_code
        payload["detail"] = redact(json.dumps(exc.detail, ensure_ascii=False, default=str))[:2000]
    cause = exc.__cause__ or exc.__context__
    if cause is not None:
        payload["cause"] = {"type": type(cause).__name__, "message": redact(cause)[:2000]}
    frames = traceback.format_exception(exc)
    payload["traceback_tail"] = [redact(line.rstrip()) for line in "".join(frames).splitlines()[-24:]]
    return payload


class StageRecorder:
    """Time one pipeline stage: wall, CPU, peak RSS, tracemalloc, cProfile."""

    def __init__(
        self,
        *,
        profile_dir: Path | None,
        profile_prefix: str,
        trace_memory: bool,
        redact: Redactor,
    ) -> None:
        self.profile_dir = profile_dir
        self.profile_prefix = profile_prefix
        self.trace_memory = trace_memory
        self.redact = redact
        self.stages: list[dict[str, Any]] = []
        if profile_dir is not None:
            profile_dir.mkdir(parents=True, exist_ok=True)

    def run(self, name: str, function: Callable[[dict[str, Any]], Any]) -> tuple[bool, Any]:
        record: dict[str, Any] = {"name": name, "status": "running", "substages": {}}
        gc.collect()
        hwm_reset = _reset_peak_rss()
        rss_start = _read_proc_status_kb("VmRSS")
        if self.trace_memory:
            if not tracemalloc.is_tracing():
                tracemalloc.start()
            tracemalloc.reset_peak()
            traced_start = tracemalloc.get_traced_memory()[0]
        profiler = cProfile.Profile() if self.profile_dir is not None else None
        cpu_started = time.process_time()
        started = time.perf_counter()
        result: Any = None
        ok = False
        if profiler is not None:
            profiler.enable()
        try:
            result = function(record)
            ok = True
        except Exception as exc:  # noqa: BLE001 - the harness records every failure
            record["error"] = _error_payload(exc, self.redact)
        finally:
            if profiler is not None:
                profiler.disable()
            record["wall_s"] = round(time.perf_counter() - started, 4)
            record["cpu_s"] = round(time.process_time() - cpu_started, 4)
            record["status"] = "ok" if ok else "error"
            record["rss_start_mb"] = _kb_to_mb(rss_start)
            record["rss_end_mb"] = _kb_to_mb(_read_proc_status_kb("VmRSS"))
            record["peak_rss_mb"] = _kb_to_mb(_read_proc_status_kb("VmHWM")) if hwm_reset else None
            record["peak_rss_source"] = "VmHWM_reset_per_stage" if hwm_reset else "unavailable"
            record["process_max_rss_mb"] = _ru_maxrss_mb()
            if self.trace_memory:
                current, peak = tracemalloc.get_traced_memory()
                record["tracemalloc_peak_mb"] = round(peak / (1024 * 1024), 2)
                record["tracemalloc_delta_mb"] = round((current - traced_start) / (1024 * 1024), 2)
            if profiler is not None and self.profile_dir is not None:
                profile_name = f"{self.profile_prefix}{name}.prof"
                profiler.dump_stats(str(self.profile_dir / profile_name))
                record["profile_file"] = profile_name
            self.stages.append(record)
        return ok, result


class ProgressTimeline:
    """Turn progress callbacks into consecutive named phases with durations."""

    def __init__(self, labeler: Callable[..., str]) -> None:
        self._labeler = labeler
        self.started = time.perf_counter()
        self.events: list[dict[str, Any]] = []

    def restart(self) -> None:
        self.started = time.perf_counter()
        self.events = []

    def add(self, label: str, **detail: Any) -> None:
        self.events.append({"t_s": round(time.perf_counter() - self.started, 4), "phase": label, **detail})

    def phases(self, total_s: float, *, first_phase: str | None = None) -> list[dict[str, Any]]:
        phases: list[dict[str, Any]] = []
        if first_phase and (not self.events or self.events[0]["t_s"] > 0):
            phases.append({"phase": first_phase, "start_s": 0.0, "events": 0})
        for event in self.events:
            if phases and phases[-1]["phase"] == event["phase"]:
                phases[-1]["events"] += 1
                continue
            phases.append({"phase": event["phase"], "start_s": event["t_s"], "events": 1})
        for index, phase in enumerate(phases):
            end = phases[index + 1]["start_s"] if index + 1 < len(phases) else total_s
            phase["duration_s"] = round(max(0.0, end - phase["start_s"]), 4)
        return phases


def _process_phase_label(job: Any) -> str:
    stage_id = str(getattr(job, "stage_id", "") or "")
    message = str(getattr(job, "message", "") or "")
    unit_label = str(getattr(job, "unit_label", "") or "")
    progress = int(getattr(job, "progress", 0) or 0)
    if stage_id == "upload_admission":
        return "admission_and_reuse_check"
    if stage_id == "parse_extract":
        if progress <= 15:
            return "native_parse"
        if progress <= 22:
            return "kordoc_table_parse"
        return "metadata_inference_and_staging"
    if stage_id == "normalize":
        return "normalize"
    if stage_id == "structure_detect":
        if unit_label == "조문 표시 찾기":
            return "structure_scan"
        if unit_label == "조문 계층 조립":
            return "structure_assemble"
        return "structure_finalize"
    if stage_id == "chunk_generate":
        return "chunk_build"
    if stage_id in {"quality_gate", "export"}:
        if job_status := str(getattr(job, "status", "") or ""):
            if job_status == "completed" and progress >= 100:
                return "terminal_commit"
        if progress <= 75:
            return "chunk_tail_validate_quality_gate"
        if progress <= 85:
            return "agent_review_plan"
        if progress <= 92:
            return "result_storage_prepare"
        if unit_label == "구조 저장":
            return "store_nodes"
        if unit_label == "청크 저장":
            return "store_chunks"
        if unit_label == "검사 결과 저장":
            return "store_issues"
        if unit_label == "품질 보고서 저장":
            return "store_quality_report"
        if message.startswith("내보내기"):
            return "exports"
        return f"{stage_id}:{unit_label or progress}"
    return stage_id or "unknown"


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def ws_strip(text: object) -> str:
    """Whitespace-only normalization used for exact-text comparisons."""

    return re.sub(r"\s+", "", str(text or ""))


def ws_collapse(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def body_section(text: object) -> str:
    """Return the ``[본문]`` part of a chunk text (the verbatim article body)."""

    lines = str(text or "").split("\n")
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == "[본문]")
    except StopIteration:
        return str(text or "").strip()
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() in BODY_SECTION_END_MARKERS:
            break
        body.append(line)
    return "\n".join(body).strip()


def location_line(text: object) -> str:
    for line in str(text or "").split("\n"):
        if line.startswith("[위치]"):
            return line[len("[위치]") :].strip()
    return ""


def _location_is_addendum(location: str) -> bool:
    return any(segment.strip().startswith("부칙") for segment in location.split(">"))


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _catalog_sort_key(title: str) -> str:
    """Python equivalent of the catalog ``ORDER BY title COLLATE NOCASE``."""

    return "".join(character.lower() if character.isascii() else character for character in title)


def _structured_tool_result(result: Any) -> Any:
    if isinstance(result, tuple) and len(result) == 2:
        return result[1]
    if isinstance(result, dict):
        return result
    if isinstance(result, list) and result:
        text = getattr(result[0], "text", None)
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
    return result


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


class CheckBook:
    def __init__(self) -> None:
        self.records: list[CheckRecord] = []

    def add(
        self,
        check_id: str,
        *,
        title: str,
        passed: bool | None,
        kind: str = "correctness",
        total: int | None = None,
        failed: int | None = None,
        metric: float | None = None,
        threshold: float | None = None,
        detail: Any = None,
        samples: Sequence[Any] | None = None,
    ) -> CheckRecord:
        record: CheckRecord = {
            "id": check_id,
            "title": title,
            "kind": kind,
            "status": "skipped" if passed is None else ("pass" if passed else "fail"),
        }
        if total is not None:
            record["total"] = total
        if failed is not None:
            record["failed"] = failed
            if total is not None:
                record["passed_count"] = total - failed
        if metric is not None:
            record["metric"] = round(metric, 4)
        if threshold is not None:
            record["threshold"] = threshold
        if detail is not None:
            record["detail"] = detail
        if samples:
            record["failure_samples"] = list(samples)[:25]
        if check_id in KNOWN_DEFECT_CHECKS:
            record["known_defect"] = KNOWN_DEFECT_CHECKS[check_id]
        self.records.append(record)
        return record

    def skip(self, check_id: str, *, title: str, reason: str, kind: str = "correctness") -> None:
        self.add(check_id, title=title, passed=None, kind=kind, detail={"skipped_reason": reason})


# ---------------------------------------------------------------------------
# Pipeline context
# ---------------------------------------------------------------------------


class E2EContext:
    def __init__(
        self,
        *,
        input_path: Path,
        ground_truth: dict[str, Any] | None,
        work_dir: Path,
        tenant_id: str,
        profile_id: str,
        tenant_storage_isolation: bool,
    ) -> None:
        self.input_path = input_path
        self.ground_truth = ground_truth
        self.work_dir = work_dir
        self.tenant_id = tenant_id
        self.profile_id = profile_id
        self.base_settings = Settings(
            data_dir=work_dir / "data",
            artifact_root=work_dir,
            tenant_storage_isolation=tenant_storage_isolation,
            enable_agent_review=False,
            local_structure_review_enabled=False,
            enable_kordoc_table_parser=False,
            quality_profiles_path="",
            institution_profiles_path="",
            pdf_ocr_backend="",
            api_audit_enabled=True,
            rag_trace_enabled=True,
        )
        self.settings = settings_for_tenant(self.base_settings, tenant_id)
        self.repository = JsonRepository(self.settings)
        self.auth = AuthContext(actor=HARNESS_ACTOR, tenant_id=tenant_id, auth_mode="script", role="admin")
        self.bundle_dir = work_dir / "mcp_bundle"
        self.document_id = ""
        self.approved_chunk_count = 0
        self.mcp_settings: Settings | None = None
        self.mcp_auth: AuthContext | None = None

    @contextmanager
    def route_settings(self) -> Iterator[None]:
        with patch.object(routes_documents, "get_settings", return_value=self.base_settings):
            yield


def _upload_metadata(ground_truth: dict[str, Any] | None, input_path: Path, tenant_id: str, profile_id: str) -> dict[str, Any]:
    truth = ground_truth or {}
    snapshot = str(truth.get("snapshot_date") or "2026-01-01")
    file_format = input_path.suffix.lower().lstrip(".")
    return {
        "document_name": str(truth.get("book_title") or input_path.stem),
        "institution_name": str(truth.get("institution") or "Synthetic Institution"),
        "source_system": DEFAULT_SOURCE_SYSTEM,
        "source_url": DEFAULT_SOURCE_URL,
        "source_record_id": f"synthetic-record-{file_format}",
        "source_file_id": f"synthetic-file-{file_format}",
        "profile_id": profile_id,
        "regulation_id": DEFAULT_REGULATION_ID,
        "regulation_version": snapshot,
        "effective_from": snapshot,
        "regulation_status": "draft",
        "tenant_id": tenant_id,
    }


def _stage_upload(ctx: E2EContext, record: dict[str, Any]) -> dict[str, Any]:
    content = ctx.input_path.read_bytes()
    metadata = _upload_metadata(ctx.ground_truth, ctx.input_path, ctx.tenant_id, ctx.profile_id)
    document = DocumentService(settings=ctx.settings, repository=ctx.repository).upload(
        ctx.input_path.name,
        content,
        **metadata,
    )
    ctx.document_id = document.document_id
    record["substages"]["bytes"] = len(content)
    return {
        "document_id_assigned": bool(document.document_id),
        "upload_metadata": {key: value for key, value in metadata.items() if key != "tenant_id"},
        "document_after_upload": _document_summary(document),
    }


def _document_summary(document: Any) -> dict[str, Any]:
    return {
        key: getattr(document, key, None)
        for key in (
            "document_name",
            "file_type",
            "institution_name",
            "profile_id",
            "regulation_id",
            "regulation_version",
            "revision_date",
            "effective_from",
            "regulation_status",
            "status",
            "page_count",
        )
    }


def _stage_process(ctx: E2EContext, record: dict[str, Any]) -> dict[str, Any]:
    timeline = ProgressTimeline(_process_phase_label)
    progress_rows: list[dict[str, Any]] = []

    def on_progress(job: Any) -> None:
        timeline.add(
            _process_phase_label(job),
            stage_id=str(getattr(job, "stage_id", "") or ""),
            progress=int(getattr(job, "progress", 0) or 0),
        )
        if len(progress_rows) < 5000:
            progress_rows.append(
                {
                    "stage_id": str(getattr(job, "stage_id", "") or ""),
                    "progress": int(getattr(job, "progress", 0) or 0),
                    "unit_label": str(getattr(job, "unit_label", "") or ""),
                    "current_unit": getattr(job, "current_unit", None),
                    "total_units": getattr(job, "total_units", None),
                }
            )

    service = ProcessingService(settings=ctx.settings, repository=ctx.repository)
    options = ChunkOptions(enable_agent_review=False)
    timeline.restart()
    started = time.perf_counter()
    job = service.process(ctx.document_id, options, progress_callback=on_progress)
    total_s = time.perf_counter() - started
    runs = ctx.repository.list_runs(ctx.document_id)
    latest = runs[-1] if runs else None
    stats = dict(latest.stats or {}) if latest is not None else {}
    record["substages"]["phase_timings_ms"] = dict(stats.get("phase_timings_ms") or {})
    record["substages"]["progress_phases"] = timeline.phases(total_s)
    record["substages"]["progress_event_count"] = len(timeline.events)
    document = ctx.repository.get_document(ctx.document_id)
    return {
        "job_status": job.status,
        "job_error": job.error,
        "job_message": job.message,
        "run_statuses": [run.status for run in runs],
        "run_elapsed_s": getattr(latest, "elapsed_seconds", None),
        "document_after_process": _document_summary(document) if document is not None else None,
        "options": options.model_dump(),
    }


def _chunk_summary(ctx: E2EContext) -> dict[str, Any]:
    chunks = ctx.repository.get_chunks(ctx.document_id)
    ambiguous = ambiguous_combined_book_chunk_ids(chunks)
    regulation_titles = Counter(str(chunk.metadata.get("regulation_title") or "") for chunk in chunks)
    fingerprint_rows = sorted(
        (
            str(chunk.chunk_type or ""),
            str(chunk.metadata.get("canonical_hierarchy_path") or chunk.metadata.get("hierarchy_path") or ""),
            ws_collapse(chunk.normalized_text or chunk.text),
        )
        for chunk in chunks
    )
    return {
        "chunk_count": len(chunks),
        "chunk_types": dict(sorted(Counter(chunk.chunk_type for chunk in chunks).items())),
        "approval_statuses": dict(sorted(Counter(chunk.approval_status for chunk in chunks).items())),
        "regulation_title_count": len(regulation_titles),
        "chunks_per_regulation_title": dict(regulation_titles),
        "ambiguous_boundary_chunk_count": len(ambiguous),
        "structure_boundary_diagnostics": sorted(
            {
                str(chunk.metadata.get("structure_boundary_diagnostic"))
                for chunk in chunks
                if chunk.metadata.get("structure_boundary_diagnostic")
            }
        ),
        "chunk_content_fingerprint": _sha256_json(fingerprint_rows),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return _sha256_file(path)


def _approval_request_from_batch(
    batch: dict[str, Any],
    *,
    manifest_relative: str,
    manifest_sha256: str,
    approval_id: str,
) -> routes_documents.ApprovalRequest:
    template = dict(batch.get("approval_request_template") or {})
    return routes_documents.ApprovalRequest(
        chunk_ids=[str(chunk_id) for chunk_id in template.get("chunk_ids") or []],
        approval_id=approval_id,
        security_level=str(template.get("security_level") or "internal"),
        review_flags_acknowledged=False,
        worklist_report_path=str(template.get("worklist_report_path") or ""),
        worklist_report_sha256=str(template.get("worklist_report_sha256") or ""),
        review_batch_manifest_path=manifest_relative,
        review_batch_manifest_sha256=manifest_sha256,
        review_batch_id=str(template.get("review_batch_id") or ""),
        review_batch_chunk_fingerprint=str(template.get("review_batch_chunk_fingerprint") or ""),
        review_strategy=str(template.get("review_strategy") or ""),
        approval_override_reason=UNREVIEWED_APPROVAL_REASON,
        note="combined_book_e2e_regression",
    )


def _generate_batch_evidence(ctx: E2EContext, *, iteration: int, max_chunks_per_batch: int) -> tuple[dict[str, Any], str, str]:
    reports = ctx.base_settings.artifact_root / "reports"
    worklist_relative = f"reports/e2e_{iteration:03d}_approval_worklist.json"
    manifest_relative = f"reports/e2e_{iteration:03d}_approval_review_batches.json"
    worklist = build_approval_worklist(
        data_dir=ctx.base_settings.data_dir,
        tenant_id=ctx.tenant_id,
        tenant_storage_isolation=ctx.base_settings.tenant_storage_isolation,
    )
    _write_json(ctx.base_settings.artifact_root / worklist_relative, worklist)
    manifest = build_approval_review_batches(
        data_dir=ctx.base_settings.data_dir,
        worklist_report=ctx.base_settings.artifact_root / worklist_relative,
        worklist_report_artifact_path=worklist_relative,
        tenant_id=ctx.tenant_id,
        tenant_storage_isolation=ctx.base_settings.tenant_storage_isolation,
        max_chunks_per_batch=max(1, max_chunks_per_batch),
        default_security_level="internal",
    )
    manifest_sha256 = _write_json(ctx.base_settings.artifact_root / manifest_relative, manifest)
    reports.mkdir(parents=True, exist_ok=True)
    return manifest, manifest_relative, manifest_sha256


def _approvable_chunk_ids(ctx: E2EContext) -> list[str]:
    return [
        chunk.chunk_id
        for chunk in ctx.repository.get_chunks(ctx.document_id)
        if str(chunk.approval_status or "") in {"draft", "needs_review"}
    ]


def _stage_approve(ctx: E2EContext, record: dict[str, Any], *, approval_batches: int) -> dict[str, Any]:
    pending = _approvable_chunk_ids(ctx)
    per_batch = max(1, math.ceil(len(pending) / max(1, approval_batches)))
    batches: list[dict[str, Any]] = []
    evidence_s = 0.0
    route_s = 0.0
    iteration = 0
    max_iterations = approval_batches * 3 + 3
    with ctx.route_settings():
        while iteration < max_iterations:
            iteration += 1
            started = time.perf_counter()
            manifest, manifest_relative, manifest_sha256 = _generate_batch_evidence(
                ctx, iteration=iteration, max_chunks_per_batch=per_batch
            )
            evidence_elapsed = time.perf_counter() - started
            evidence_s += evidence_elapsed
            if not manifest.get("passed"):
                raise RuntimeError(f"approval review batch manifest has blockers: {manifest.get('findings')}")
            document_batches = [
                batch
                for batch in manifest.get("batches") or []
                if str(batch.get("document_id") or "") == ctx.document_id
            ]
            if not document_batches:
                break
            batch = document_batches[0]
            request = _approval_request_from_batch(
                batch,
                manifest_relative=manifest_relative,
                manifest_sha256=manifest_sha256,
                approval_id=f"approval-e2e-{iteration:03d}",
            )
            started = time.perf_counter()
            response = routes_documents.approve_review_chunks(ctx.document_id, request, ctx.auth)
            route_elapsed = time.perf_counter() - started
            route_s += route_elapsed
            batches.append(
                {
                    "iteration": iteration,
                    "review_type": batch.get("review_type"),
                    "chunk_count": len(request.chunk_ids),
                    "evidence_s": round(evidence_elapsed, 4),
                    "route_s": round(route_elapsed, 4),
                    "vector_sync_status": (response.get("vector_sync") or {}).get("status"),
                    "review_decision_event_counts": response.get("review_decision_event_counts"),
                }
            )
    remaining = _approvable_chunk_ids(ctx)
    chunks = ctx.repository.get_chunks(ctx.document_id)
    ctx.approved_chunk_count = sum(1 for chunk in chunks if chunk.approval_status == "approved")
    journal = ctx.repository.list_approval_journal_records(ctx.document_id)
    record["substages"].update(
        {
            "evidence_generation_s": round(evidence_s, 4),
            "approve_route_s": round(route_s, 4),
            "batch_count": len(batches),
        }
    )
    return {
        "approvable_before": len(pending),
        "max_chunks_per_batch": per_batch,
        "batches": batches,
        "approved_chunk_count": ctx.approved_chunk_count,
        "remaining_unapproved_chunk_count": len(remaining),
        "approval_journal_record_count": len(journal),
        "approval_journal_chunk_count": sum(len(item.get("chunk_ids") or []) for item in journal),
        "evidence_generator": "scripts.build_approval_worklist + scripts.build_approval_review_batches",
        "review_claim": "approved_without_review (approval_override_reason)",
    }


def _stage_index(ctx: E2EContext, record: dict[str, Any]) -> dict[str, Any]:
    with ctx.route_settings():
        job = routes_documents.index_document(
            ctx.document_id,
            routes_documents.IndexRequest(
                target_type="local-jsonl",
                embedding_dimensions=384,
                embedding_model=LOCAL_HASH_EMBEDDING_MODEL,
            ),
            ctx.auth,
        )
    record["substages"]["timing_ms"] = dict(job.get("timing_ms") or {})
    return {
        "status": job.get("status"),
        "record_count": job.get("record_count"),
        "embedding_model": job.get("embedding_model"),
        "embedding_dimensions": job.get("embedding_dimensions"),
        "upsert_summary": {
            key: value
            for key, value in (job.get("upsert_summary") or {}).items()
            if isinstance(value, (int, float, str, bool)) or value is None
        },
    }


def _stage_bundle(ctx: E2EContext, record: dict[str, Any]) -> dict[str, Any]:
    timeline = ProgressTimeline(lambda message: message)

    def on_progress(percent: int, message: str, current: int | None = None, total: int | None = None) -> None:
        timeline.add(str(message), percent=int(percent))

    timeline.restart()
    started = time.perf_counter()
    manifest = write_mcp_runtime_data_bundle(
        source_data_dir=ctx.base_settings.data_dir,
        out_dir=ctx.bundle_dir,
        tenant_id=ctx.tenant_id,
        profile_id=ctx.profile_id,
        document_id=ctx.document_id,
        scope="document",
        tenant_storage_isolation=ctx.base_settings.tenant_storage_isolation,
        # Kordoc is not installed in this harness. The console calls this
        # function with the default ``require_kordoc_table_parser=True`` and
        # would refuse DOCX/PDF/HWPX bundles without Kordoc evidence.
        require_kordoc_table_parser=False,
        progress_callback=on_progress,
    )
    total_s = time.perf_counter() - started
    record["substages"]["progress_phases"] = timeline.phases(total_s, first_phase="prepare_inputs_validate_and_snapshot")
    hierarchy = manifest.get("hierarchical_index") or {}
    return {
        "record_count": manifest.get("record_count"),
        "chunk_count": manifest.get("chunk_count"),
        "regulation_count": manifest.get("regulation_count"),
        "regulation_version_count": manifest.get("regulation_version_count"),
        "toc_node_count": manifest.get("toc_node_count"),
        "bm25_document_count": manifest.get("bm25_document_count"),
        "hierarchical_index_status": manifest.get("hierarchical_index_status"),
        "logical_corpus_sha256": manifest.get("logical_corpus_sha256"),
        "reference_edge_count": hierarchy.get("reference_edge_count"),
        "resolved_reference_edge_count": hierarchy.get("resolved_reference_edge_count"),
        "unresolved_reference_edge_count": hierarchy.get("unresolved_reference_edge_count"),
        "kordoc_table_parser_required": manifest.get("kordoc_table_parser_required"),
        "runtime_manifest_present": (ctx.bundle_dir / "data" / "mcp_runtime_manifest.json").is_file(),
    }


def _stage_bundle_setup_files(ctx: E2EContext, record: dict[str, Any]) -> dict[str, Any]:
    config = build_mcp_client_config(
        server_name=DEFAULT_SERVER_NAME,
        data_dir=str(ctx.bundle_dir / "data"),
        tenant_id=ctx.tenant_id,
        profile_id=ctx.profile_id,
        tenant_storage_isolation=False,
        transport="stdio",
        client_profile="bundle",
    )
    files = write_mcp_setup_bundle(
        config,
        ctx.bundle_dir,
        server_name=DEFAULT_SERVER_NAME,
        preferred_python=sys.executable,
        preferred_project_root=PROJECT_ROOT,
    )
    return {"setup_file_keys": sorted(files)}


def _stage_mcp_warm(ctx: E2EContext, record: dict[str, Any]) -> dict[str, Any]:
    ctx.mcp_settings = regulation_tools.settings_for_mcp_project(
        data_dir=ctx.bundle_dir / "data",
        tenant_id=ctx.tenant_id,
    )
    ctx.mcp_auth = regulation_tools.mcp_auth_context(tenant_id=ctx.tenant_id)
    status = regulation_tools.warm_mcp_runtime(settings=ctx.mcp_settings, auth=ctx.mcp_auth)
    record["substages"]["timing_ms"] = dict(status.get("timing_ms") or {})
    return {
        key: status.get(key)
        for key in (
            "warmed",
            "record_count",
            "regulation_count",
            "toc_node_count",
            "reference_edge_count",
            "hierarchical_index_ready",
            "bm25_index_ready",
            "retrieval_index_mode",
            "approval_snapshot_ready",
        )
    }


def _stage_mcp_server_create(ctx: E2EContext, record: dict[str, Any]) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    for tool_profile in ("chatgpt-data", "full"):
        started = time.perf_counter()
        server = create_regulation_mcp_server(
            data_dir=ctx.bundle_dir / "data",
            tenant_id=ctx.tenant_id,
            profile_id=ctx.profile_id,
            tool_profile=tool_profile,
            warm_cache=True,
        )
        record["substages"][f"create_{tool_profile}_s"] = round(time.perf_counter() - started, 4)
        servers[tool_profile] = server
    return servers


# ---------------------------------------------------------------------------
# MCP verification against ground truth
# ---------------------------------------------------------------------------


class McpVerifier:
    def __init__(self, ctx: E2EContext, checks: CheckBook, redact: Redactor) -> None:
        if ctx.mcp_settings is None or ctx.mcp_auth is None:
            raise RuntimeError("MCP runtime was not opened.")
        self.ctx = ctx
        self.settings = ctx.mcp_settings
        self.auth = ctx.mcp_auth
        self.checks = checks
        self.redact = redact
        self.truth = ctx.ground_truth or {}
        self.latencies: dict[str, list[float]] = {}
        self.call_errors: list[dict[str, Any]] = []
        self.unit_by_title: dict[str, str] = {}
        self.fingerprint: dict[str, Any] = {}
        self.search_rows: list[dict[str, Any]] = []
        self.reference_summary: dict[str, Any] = {}

    def call(self, tool: str, function: Callable[..., Any], **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return function(settings=self.settings, auth=self.auth, **kwargs)
        except Exception as exc:  # noqa: BLE001 - record and keep verifying
            self.call_errors.append({"tool": tool, "arguments": _short_args(kwargs), **_error_payload(exc, self.redact)})
            return None
        finally:
            self.latencies.setdefault(tool, []).append(time.perf_counter() - started)

    # -- catalog -----------------------------------------------------------

    def verify_catalog(self) -> list[dict[str, Any]]:
        listed: list[dict[str, Any]] = []
        total_count = None
        page = 1
        while page <= 50:
            response = self.call(
                "list_regulations",
                regulation_tools.list_regulations,
                page=page,
                page_size=100,
                require_hierarchy=True,
            )
            if not isinstance(response, dict):
                break
            total_count = response.get("total_count")
            listed.extend(response.get("regulations") or [])
            if not response.get("next_cursor"):
                break
            page += 1
        expected_titles = [str(item["title"]) for item in self.truth.get("regulations") or []]
        listed_titles = [str(item.get("regulation_title") or "") for item in listed]
        missing = [title for title in expected_titles if title not in listed_titles]
        extra = [title for title in listed_titles if title not in expected_titles]
        duplicates = [title for title, count in Counter(listed_titles).items() if count > 1]
        self.checks.add(
            "catalog_regulation_count",
            title=f"list_regulations returns exactly {len(expected_titles)} regulations",
            passed=total_count == len(expected_titles) and len(listed) == len(expected_titles),
            total=len(expected_titles),
            detail={"total_count": total_count, "listed": len(listed)},
        )
        self.checks.add(
            "catalog_titles_match",
            title="catalog titles equal the ground-truth titles (no missing, extra or duplicate unit)",
            passed=not missing and not extra and not duplicates,
            total=len(expected_titles),
            failed=len(missing) + len(extra) + len(duplicates),
            detail={"missing": missing, "extra": extra, "duplicates": duplicates},
        )
        documented_order = sorted(expected_titles, key=_catalog_sort_key)
        listed_common = [title for title in listed_titles if title in set(expected_titles)]
        self.checks.add(
            "catalog_order_matches",
            title="catalog order equals the documented order (title COLLATE NOCASE) of the ground-truth titles",
            passed=listed_common == documented_order,
            total=len(expected_titles),
            failed=sum(1 for left, right in zip(listed_common, documented_order) if left != right)
            + abs(len(listed_common) - len(documented_order)),
            detail={
                "policy": "the catalog is sorted by title; book order (N-N-N numbering) is reported separately",
                "regulation_no_exposed_count": sum(1 for item in listed if str(item.get("regulation_no") or "").strip()),
                "book_order_reproducible": [title for title in listed_common] == expected_titles,
            },
        )
        for item in listed:
            title = str(item.get("regulation_title") or "")
            unit_id = str(item.get("regulation_unit_id") or "")
            if title and unit_id:
                self.unit_by_title.setdefault(title, unit_id)
        normalized_units = {normalize_regulation_title(title): unit for title, unit in self.unit_by_title.items()}
        for title in expected_titles:
            if title not in self.unit_by_title and normalize_regulation_title(title) in normalized_units:
                self.unit_by_title[title] = normalized_units[normalize_regulation_title(title)]
        self.fingerprint["catalog"] = listed_titles
        return listed

    # -- TOC -----------------------------------------------------------------

    def verify_toc(self) -> None:
        regulations = self.truth.get("regulations") or []
        set_failures: list[dict[str, Any]] = []
        order_failures: list[dict[str, Any]] = []
        structure_failures: list[dict[str, Any]] = []
        deleted_title_failures: list[dict[str, Any]] = []
        checked = 0
        toc_fingerprint: dict[str, Any] = {}
        for regulation in regulations:
            title = str(regulation["title"])
            unit_id = self.unit_by_title.get(title)
            expected_articles = [str(article["label"]) for article in regulation.get("articles") or []]
            if not unit_id:
                set_failures.append({"regulation": title, "error": "regulation not listed"})
                order_failures.append({"regulation": title, "error": "regulation not listed"})
                continue
            response = self.call("get_regulation_toc", regulation_tools.get_regulation_toc, regulation_unit_id=unit_id)
            if not isinstance(response, dict):
                set_failures.append({"regulation": title, "error": "get_regulation_toc failed"})
                order_failures.append({"regulation": title, "error": "get_regulation_toc failed"})
                continue
            checked += 1
            nodes = [node for node in response.get("nodes") or [] if isinstance(node, dict)]
            by_id = {str(node.get("node_id")): node for node in nodes}

            def under_addendum(node: dict[str, Any]) -> bool:
                parent = by_id.get(str(node.get("parent_id") or ""))
                hops = 0
                while parent is not None and hops < 64:
                    if parent.get("node_type") == "supplementary":
                        return True
                    parent = by_id.get(str(parent.get("parent_id") or ""))
                    hops += 1
                return False

            body_nodes = [node for node in nodes if node.get("node_type") == "article" and not under_addendum(node)]
            body_numbers = [str(node.get("number") or "") for node in body_nodes]
            toc_fingerprint[title] = [
                (node.get("node_type"), node.get("number"), node.get("title"), node.get("depth")) for node in nodes
            ]
            expected_counter = Counter(expected_articles)
            actual_counter = Counter(body_numbers)
            if expected_counter != actual_counter:
                set_failures.append(
                    {
                        "regulation": title,
                        "expected_count": len(expected_articles),
                        "actual_count": len(body_numbers),
                        "missing": sorted((expected_counter - actual_counter).elements())[:15],
                        "unexpected": sorted((actual_counter - expected_counter).elements())[:15],
                    }
                )
            if body_numbers != expected_articles:
                first_diff = next(
                    (
                        index
                        for index, (left, right) in enumerate(zip(body_numbers, expected_articles))
                        if left != right
                    ),
                    min(len(body_numbers), len(expected_articles)),
                )
                order_failures.append(
                    {
                        "regulation": title,
                        "first_difference_index": first_diff,
                        "expected_window": expected_articles[first_diff : first_diff + 5],
                        "actual_window": body_numbers[first_diff : first_diff + 5],
                    }
                )
            deleted_labels = {str(article["label"]) for article in regulation.get("articles") or [] if article.get("deleted")}
            for node in body_nodes:
                if str(node.get("number")) in deleted_labels and "삭제" not in str(node.get("title") or ""):
                    deleted_title_failures.append({"regulation": title, "article": node.get("number"), "title": node.get("title")})
            counts = Counter(str(node.get("node_type") or "") for node in nodes)
            expected_counts = {
                "chapter": len(regulation.get("chapters") or []),
                "section": sum(len(chapter.get("sections") or []) for chapter in regulation.get("chapters") or []),
                "supplementary": int((regulation.get("counts") or {}).get("addenda") or 0),
                "appendix": int((regulation.get("counts") or {}).get("appendices") or 0),
                "form": int((regulation.get("counts") or {}).get("forms") or 0),
            }
            mismatched = {
                node_type: {"expected": expected, "actual": counts.get(node_type, 0)}
                for node_type, expected in expected_counts.items()
                if counts.get(node_type, 0) != expected
            }
            if mismatched:
                structure_failures.append({"regulation": title, "mismatch": mismatched})
        total = len(regulations)
        self.checks.add(
            "toc_body_articles_match",
            title="get_regulation_toc body article numbers equal the ground truth (multiset; 부칙 clauses excluded; deleted placeholders included)",
            passed=not set_failures,
            total=total,
            failed=len(set_failures),
            detail={
                "policy": "article nodes without a supplementary (부칙) ancestor; '제N조 삭제' placeholders must be present",
                "regulations_checked": checked,
                "expected_body_articles": sum(len(r.get("articles") or []) for r in regulations),
            },
            samples=set_failures,
        )
        self.checks.add(
            "toc_body_article_order",
            title="get_regulation_toc lists body articles in source order",
            passed=not order_failures,
            total=total,
            failed=len(order_failures),
            samples=order_failures,
        )
        self.checks.add(
            "toc_structure_counts",
            title="TOC chapter/section/부칙/별표/별지 node counts equal the ground truth",
            passed=not structure_failures,
            total=total,
            failed=len(structure_failures),
            samples=structure_failures,
        )
        self.checks.add(
            "toc_deleted_article_titles",
            title="deleted articles appear as '삭제' placeholders",
            passed=not deleted_title_failures,
            total=sum(int(r.get("deleted_article_count") or 0) for r in regulations),
            failed=len(deleted_title_failures),
            samples=deleted_title_failures,
        )
        self.fingerprint["toc"] = toc_fingerprint

    # -- exact articles --------------------------------------------------------

    def verify_articles(self) -> None:
        failures: list[dict[str, Any]] = []
        first_not_body: list[dict[str, Any]] = []
        collapsed_failures = 0
        total = 0
        article_fingerprint: dict[str, str] = {}
        result_counts: list[int] = []
        for regulation in self.truth.get("regulations") or []:
            title = str(regulation["title"])
            unit_id = self.unit_by_title.get(title)
            for sample in regulation.get("sample_articles") or []:
                total += 1
                label = str(sample["label"])
                key = f"{title} {label}"
                if not unit_id:
                    failures.append({"article": key, "error": "regulation not listed"})
                    continue
                response = self.call(
                    "get_regulation_article",
                    regulation_tools.get_regulation_article,
                    regulation_unit_id=unit_id,
                    article_no=label,
                )
                if not isinstance(response, dict):
                    failures.append({"article": key, "error": "get_regulation_article failed"})
                    continue
                articles = [item for item in response.get("articles") or [] if isinstance(item, dict)]
                result_counts.append(len(articles))
                body_records = [
                    item
                    for item in articles
                    if not _location_is_addendum(location_line(item.get("text")))
                    and str((item.get("metadata") or {}).get("chunk_type") or "article") == "article"
                ]
                body_text = "\n".join(body_section(item.get("text")) for item in body_records)
                article_fingerprint[key] = body_text
                expected_text = str(sample["text"])
                if not body_records:
                    failures.append({"article": key, "error": "no body article record", "result_count": len(articles)})
                elif ws_strip(body_text) != ws_strip(expected_text):
                    failures.append(
                        {
                            "article": key,
                            "body_record_count": len(body_records),
                            "expected_prefix": expected_text[:120],
                            "actual_prefix": body_text[:120],
                            "expected_compact_len": len(ws_strip(expected_text)),
                            "actual_compact_len": len(ws_strip(body_text)),
                        }
                    )
                if body_records and ws_collapse(body_text) != ws_collapse(expected_text):
                    collapsed_failures += 1
                if articles and (articles[0] not in body_records):
                    first_not_body.append(
                        {
                            "article": key,
                            "result_count": len(articles),
                            "first_location": location_line(articles[0].get("text")),
                            "body_rank": next(
                                (index + 1 for index, item in enumerate(articles) if item in body_records), None
                            ),
                        }
                    )
        self.checks.add(
            "article_exact_text",
            title="get_regulation_article returns the exact body text for every sampled article (whitespace removed)",
            passed=not failures,
            total=total,
            failed=len(failures),
            detail={
                "normalization": "re.sub(r'\\s+', '', text) on both sides; body = the [본문] section of every non-부칙 article record",
                "whitespace_collapsed_mismatches": collapsed_failures,
                "max_records_per_lookup": max(result_counts) if result_counts else 0,
            },
            samples=failures,
        )
        self.checks.add(
            "article_lookup_first_result_is_body",
            title="get_regulation_article returns the body article first (not a 부칙 clause with the same number)",
            passed=not first_not_body,
            total=total,
            failed=len(first_not_body),
            samples=first_not_body,
        )
        self.fingerprint["articles"] = article_fingerprint

    # -- references ---------------------------------------------------------

    def verify_references(self) -> None:
        edges: dict[str, dict[str, Any]] = {}
        for title, unit_id in sorted(self.unit_by_title.items()):
            page = 1
            while page <= 50:
                response = self.call(
                    "get_regulation_references",
                    regulation_tools.get_regulation_references,
                    regulation_unit_id=unit_id,
                    direction="outgoing",
                    page=page,
                    page_size=100,
                )
                if not isinstance(response, dict):
                    break
                for edge in response.get("references") or []:
                    if isinstance(edge, dict):
                        edges[str(edge.get("reference_id") or len(edges))] = edge
                if not response.get("next_cursor"):
                    break
                page += 1

        def regulation_title(value: Any) -> str:
            return str((value or {}).get("regulation_title") or "") if isinstance(value, dict) else ""

        def article(value: Any) -> str:
            return str((value or {}).get("article") or "") if isinstance(value, dict) else ""

        resolved = [edge for edge in edges.values() if str(edge.get("status")) == "resolved"]
        truth_refs = [
            ref for ref in self.truth.get("cross_references") or [] if ref.get("kind") in {"external", "delegation"}
        ]
        unique_truth: dict[tuple[Any, ...], dict[str, Any]] = {}
        for ref in truth_refs:
            key = (
                ref.get("kind"),
                ref.get("source_regulation"),
                ref.get("source_article"),
                ref.get("target_regulation"),
                ref.get("target_article"),
            )
            unique_truth.setdefault(key, ref)
        matched_edges: set[str] = set()
        missing: list[dict[str, Any]] = []
        paragraph_mismatch = 0
        recall_by_kind: Counter[str] = Counter()
        total_by_kind: Counter[str] = Counter()
        for (kind, source, source_article, target, target_article), ref in unique_truth.items():
            total_by_kind[str(kind)] += 1
            candidates = []
            for edge in resolved:
                if regulation_title(edge.get("source_regulation")) != source:
                    continue
                if regulation_title(edge.get("target_regulation")) != target:
                    continue
                edge_source_article = article(edge.get("source_article"))
                if edge_source_article and edge_source_article != source_article:
                    continue
                if target_article:
                    edge_target_article = article(edge.get("target_article")) or article(edge.get("requested_article"))
                    if edge_target_article != target_article:
                        continue
                candidates.append(edge)
            if candidates:
                recall_by_kind[str(kind)] += 1
                matched_edges.update(str(edge.get("reference_id")) for edge in candidates)
                expected_paragraph = ref.get("target_paragraph")
                if expected_paragraph:
                    paragraphs = {
                        str((edge.get("target_article") or {}).get("paragraph") or "")
                        for edge in candidates
                        if isinstance(edge.get("target_article"), dict)
                    }
                    if f"제{expected_paragraph}항" not in paragraphs:
                        paragraph_mismatch += 1
            else:
                missing.append({"kind": kind, "text": ref.get("text"), "source": f"{source} {source_article}", "target": f"{target} {target_article or ''}".strip()})
        total = len(unique_truth)
        recalled = sum(recall_by_kind.values())
        cross_resolved = [edge for edge in resolved if regulation_title(edge.get("source_regulation")) != regulation_title(edge.get("target_regulation"))]
        unmatched_resolved = [edge for edge in cross_resolved if str(edge.get("reference_id")) not in matched_edges]
        unresolved = [edge for edge in edges.values() if str(edge.get("status")) != "resolved"]
        self.reference_summary = {
            "edge_count": len(edges),
            "resolved_edge_count": len(resolved),
            "cross_regulation_resolved_edge_count": len(cross_resolved),
            "unresolved_or_ambiguous_edge_count": len(unresolved),
            "unresolved_samples": [
                {
                    "status": edge.get("status"),
                    "source": regulation_title(edge.get("source_regulation")),
                    "requested_target": regulation_title(edge.get("target_regulation")),
                    "reason_codes": edge.get("reason_codes"),
                }
                for edge in unresolved[:10]
            ],
            "unmatched_cross_resolved_samples": [
                {
                    "source": f"{regulation_title(edge.get('source_regulation'))} {article(edge.get('source_article'))}",
                    "target": f"{regulation_title(edge.get('target_regulation'))} {article(edge.get('target_article'))}",
                }
                for edge in unmatched_resolved[:10]
            ],
        }
        self.checks.add(
            "references_cross_regulation_recall",
            title="get_regulation_references resolves every ground-truth cross-regulation reference (external + delegation)",
            passed=recalled == total,
            total=total,
            failed=total - recalled,
            metric=(recalled / total) if total else 1.0,
            detail={
                "recall_by_kind": {kind: f"{recall_by_kind[kind]}/{count}" for kind, count in sorted(total_by_kind.items())},
                "target_paragraph_mismatch": paragraph_mismatch,
                "matching": "resolved edge with equal source/target regulation titles, source article (when the edge has one) and target article",
                **{key: value for key, value in self.reference_summary.items() if not key.endswith("samples")},
            },
            samples=missing,
        )
        self.checks.add(
            "references_no_unresolved_edges",
            title="no unresolved/ambiguous reference edges are materialized for the book",
            passed=not unresolved,
            kind="quality",
            total=len(edges),
            failed=len(unresolved),
            samples=self.reference_summary["unresolved_samples"],
        )
        self.fingerprint["references"] = sorted(
            (
                str(edge.get("status")),
                regulation_title(edge.get("source_regulation")),
                article(edge.get("source_article")),
                regulation_title(edge.get("target_regulation")),
                article(edge.get("target_article")),
            )
            for edge in edges.values()
        )

    # -- search + fetch -----------------------------------------------------

    def _is_expected_hit(self, result: dict[str, Any], expected: dict[str, Any]) -> bool:
        metadata = result.get("metadata") or {}
        return (
            str(metadata.get("regulation_title") or "") == str(expected.get("regulation"))
            and str(metadata.get("article_no") or "") == str(expected.get("article"))
            and not _location_is_addendum(location_line(result.get("verbatim_text") or result.get("text")))
        )

    def verify_search_and_fetch(self, *, min_top1: float, min_top5: float) -> None:
        queries = self.truth.get("queries") or []
        top1 = 0
        top5 = 0
        reciprocal = 0.0
        must_contain_failures: list[dict[str, Any]] = []
        expected_text_failures: list[dict[str, Any]] = []
        fetch_failures: list[dict[str, Any]] = []
        identity_failures: list[dict[str, Any]] = []
        fetch_total = 0
        search_fingerprint: dict[str, Any] = {}
        institution = str(self.truth.get("institution") or "")
        for query in queries:
            expected = query.get("expected") or {}
            response = self.call(
                "search_regulations",
                regulation_tools.search_regulations,
                query=str(query["query"]),
                top_k=SEARCH_TOP_K,
            )
            results = [item for item in (response or {}).get("results") or [] if isinstance(item, dict)]
            rank = next((index + 1 for index, item in enumerate(results) if self._is_expected_hit(item, expected)), None)
            top1 += int(rank == 1)
            top5 += int(rank is not None and rank <= SEARCH_TOP_K)
            reciprocal += (1.0 / rank) if rank else 0.0
            row = {
                "id": query.get("id"),
                "query": query.get("query"),
                "expected": f"{expected.get('regulation')} {expected.get('article')}",
                "rank": rank,
                "top_results": [str(item.get("title") or "") for item in results],
                "retrieval_strategy": ((response or {}).get("metadata") or {}).get("retrieval_strategy"),
                "refused": bool(((response or {}).get("metadata") or {}).get("refused")),
            }
            search_fingerprint[str(query.get("id"))] = row["top_results"]
            for index, item in enumerate(results):
                fetch_total += 1
                fetched = self.call("fetch_regulation", regulation_tools.fetch_regulation, result_id=str(item.get("id") or ""))
                if not isinstance(fetched, dict):
                    fetch_failures.append({"query": query.get("id"), "rank": index + 1, "error": "fetch failed"})
                    continue
                search_metadata = item.get("metadata") or {}
                fetched_metadata = fetched.get("metadata") or {}
                problems = []
                if not fetched.get("text"):
                    problems.append("empty text")
                if fetched.get("text") != item.get("verbatim_text"):
                    problems.append("fetch text differs from search verbatim_text")
                for key in ("document_id", "chunk_id", "regulation_title", "article_no", "approved_content_hash"):
                    if str(fetched_metadata.get(key) or "") != str(search_metadata.get(key) or ""):
                        problems.append(f"{key} differs")
                if not str(fetched_metadata.get("approved_content_hash") or ""):
                    problems.append("missing approved_content_hash")
                if str(fetched_metadata.get("source_url") or "") != DEFAULT_SOURCE_URL:
                    problems.append("source_url differs")
                if institution and str(fetched_metadata.get("institution_name") or "") != institution:
                    problems.append("institution_name differs")
                if str(fetched_metadata.get("profile_id") or "") != self.ctx.profile_id:
                    problems.append("profile_id differs")
                if problems:
                    fetch_failures.append({"query": query.get("id"), "rank": index + 1, "problems": problems})
                if rank == index + 1:
                    body = body_section(fetched.get("text"))
                    if str(fetched_metadata.get("regulation_title")) != str(expected.get("regulation")) or str(
                        fetched_metadata.get("article_no")
                    ) != str(expected.get("article")):
                        identity_failures.append({"query": query.get("id"), "citation": fetched.get("title")})
                    absent = [needle for needle in query.get("must_contain") or [] if ws_strip(needle) not in ws_strip(body)]
                    if absent:
                        must_contain_failures.append({"query": query.get("id"), "absent": absent})
                    if ws_strip(body) != ws_strip(query.get("expected_text")):
                        expected_text_failures.append(
                            {"query": query.get("id"), "expected_prefix": str(query.get("expected_text"))[:80], "actual_prefix": body[:80]}
                        )
            row["must_contain_ok"] = rank is not None and not any(item["query"] == query.get("id") for item in must_contain_failures)
            self.search_rows.append(row)
        total = len(queries)
        top1_rate = top1 / total if total else 1.0
        top5_rate = top5 / total if total else 1.0
        self.checks.add(
            "search_top1_hit_rate",
            title=f"search top-1 hit rate >= {min_top1:.2f} (expected regulation + article, body not 부칙)",
            kind="quality",
            passed=top1_rate >= min_top1,
            total=total,
            failed=total - top1,
            metric=top1_rate,
            threshold=min_top1,
            detail={"mrr_at_5": round(reciprocal / total, 4) if total else None},
            samples=[row for row in self.search_rows if row["rank"] != 1],
        )
        self.checks.add(
            "search_top5_hit_rate",
            title=f"search top-5 hit rate >= {min_top5:.2f}",
            kind="quality",
            passed=top5_rate >= min_top5,
            total=total,
            failed=total - top5,
            metric=top5_rate,
            threshold=min_top5,
            samples=[row for row in self.search_rows if row["rank"] is None],
        )
        self.checks.add(
            "fetch_matches_search_results",
            title="fetch on every search result returns the full chunk text with identical citation identity",
            passed=not fetch_failures,
            total=fetch_total,
            failed=len(fetch_failures),
            samples=fetch_failures,
        )
        hits = sum(1 for row in self.search_rows if row["rank"] is not None)
        self.checks.add(
            "search_hit_citation_and_text",
            title="for every top-5 hit, fetch cites the expected regulation/article, contains must_contain and equals expected_text",
            passed=not identity_failures and not must_contain_failures and not expected_text_failures,
            total=hits,
            failed=len({item["query"] for item in identity_failures + must_contain_failures + expected_text_failures}),
            samples=identity_failures + must_contain_failures + expected_text_failures,
        )
        self.fingerprint["search_top5"] = search_fingerprint

    # -- FastMCP -------------------------------------------------------------

    def verify_fastmcp(self, servers: dict[str, Any]) -> None:
        failures: list[dict[str, Any]] = []
        calls = 0
        truth_regulations = self.truth.get("regulations") or []
        query = str(((self.truth.get("queries") or [{}])[0]).get("query") or "규정의 목적")
        regulation = truth_regulations[0] if truth_regulations else {}
        title = str(regulation.get("title") or "")
        unit_id = self.unit_by_title.get(title, "")
        sample = (regulation.get("sample_articles") or [{}])[0]
        label = str(sample.get("label") or "제1조")

        def invoke(server: Any, profile: str, name: str, arguments: dict[str, Any]) -> Any:
            nonlocal calls
            calls += 1
            started = time.perf_counter()
            try:
                return _structured_tool_result(asyncio.run(server.call_tool(name, arguments)))
            except Exception as exc:  # noqa: BLE001
                failures.append({"profile": profile, "tool": name, **_error_payload(exc, self.redact)})
                return None
            finally:
                self.latencies.setdefault(f"fastmcp[{profile}].{name}", []).append(time.perf_counter() - started)

        direct_search = self.call("search_regulations", regulation_tools.search_regulations, query=query, top_k=SEARCH_TOP_K) or {}
        direct_ids = [str(item.get("id")) for item in direct_search.get("results") or []]
        direct_list = self.call(
            "list_regulations", regulation_tools.list_regulations, page=1, page_size=100, require_hierarchy=True
        ) or {}
        direct_toc = (
            self.call("get_regulation_toc", regulation_tools.get_regulation_toc, regulation_unit_id=unit_id) if unit_id else None
        ) or {}
        direct_article = (
            self.call(
                "get_regulation_article",
                regulation_tools.get_regulation_article,
                regulation_unit_id=unit_id,
                article_no=label,
            )
            if unit_id
            else None
        ) or {}
        direct_refs = (
            self.call(
                "get_regulation_references",
                regulation_tools.get_regulation_references,
                regulation_unit_id=unit_id,
                page=1,
                page_size=100,
            )
            if unit_id
            else None
        ) or {}

        for profile, server in servers.items():
            tools = asyncio.run(server.list_tools())
            names = {tool.name for tool in tools}
            if profile == "chatgpt-data" and names != CHATGPT_DATA_TOOLS:
                failures.append({"profile": profile, "problem": "tool set differs", "tools": sorted(names)})
            if profile == "full" and not CHATGPT_DATA_TOOLS - {"list_regulation_reference_cycles"} <= names | {"fetch"}:
                failures.append({"profile": profile, "problem": "core tools missing", "tools": sorted(names)})
            listed = invoke(server, profile, "list_regulations", {"page": 1, "page_size": 100}) or {}
            if int(listed.get("total_count") or -1) != int(direct_list.get("total_count") or -2):
                failures.append({"profile": profile, "tool": "list_regulations", "problem": "total_count differs"})
            if unit_id:
                toc = invoke(server, profile, "get_regulation_toc", {"regulation_unit_id": unit_id}) or {}
                if len(toc.get("nodes") or []) != len(direct_toc.get("nodes") or []):
                    failures.append({"profile": profile, "tool": "get_regulation_toc", "problem": "node count differs"})
                article_response = invoke(
                    server, profile, "get_regulation_article", {"regulation_unit_id": unit_id, "article_no": label}
                ) or {}
                served_texts = [str(item.get("text") or "") for item in article_response.get("articles") or []]
                direct_texts = [str(item.get("text") or "") for item in direct_article.get("articles") or []]
                if served_texts != direct_texts:
                    failures.append({"profile": profile, "tool": "get_regulation_article", "problem": "article texts differ"})
                refs = invoke(server, profile, "get_regulation_references", {"regulation_unit_id": unit_id}) or {}
                if int(refs.get("total_count") or -1) != int(direct_refs.get("total_count") or -2):
                    failures.append({"profile": profile, "tool": "get_regulation_references", "problem": "total_count differs"})
            searched = invoke(server, profile, "search", {"query": query}) or {}
            served_ids = [str(item.get("id")) for item in searched.get("results") or []]
            if served_ids != direct_ids:
                failures.append({"profile": profile, "tool": "search", "problem": "result ids differ", "served": len(served_ids), "direct": len(direct_ids)})
            if served_ids:
                fetched = invoke(server, profile, "fetch", {"id": served_ids[0]}) or {}
                direct_fetch = self.call("fetch_regulation", regulation_tools.fetch_regulation, result_id=served_ids[0]) or {}
                if str(fetched.get("text") or "") != str(direct_fetch.get("text") or "") or not fetched.get("text"):
                    failures.append({"profile": profile, "tool": "fetch", "problem": "text differs or empty"})
                if profile == "chatgpt-data" and str(fetched.get("url") or "") != DEFAULT_SOURCE_URL:
                    failures.append({"profile": profile, "tool": "fetch", "problem": "chatgpt-data citation url differs"})
            else:
                failures.append({"profile": profile, "tool": "search", "problem": "no result"})
        self.checks.add(
            "fastmcp_tool_layer",
            title="in-process FastMCP call_tool (chatgpt-data and full) returns the same data as the direct tool functions",
            passed=not failures,
            total=calls,
            failed=len(failures),
            samples=failures,
        )

    # -- latency ---------------------------------------------------------------

    def measure_latency(self, repeats: int) -> dict[str, dict[str, Any]]:
        warm: dict[str, list[float]] = {}

        def timed(name: str, function: Callable[..., Any], **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return function(settings=self.settings, auth=self.auth, **kwargs)
            except Exception:  # noqa: BLE001 - correctness is verified elsewhere
                return None
            finally:
                warm.setdefault(name, []).append(time.perf_counter() - started)

        queries = [str(query["query"]) for query in self.truth.get("queries") or []]
        samples = [
            (self.unit_by_title.get(str(regulation["title"])), str(sample["label"]))
            for regulation in self.truth.get("regulations") or []
            for sample in regulation.get("sample_articles") or []
        ]
        for _ in range(max(0, repeats)):
            for query in queries:
                response = timed("search_regulations", regulation_tools.search_regulations, query=query, top_k=SEARCH_TOP_K)
                for item in (response or {}).get("results") or []:
                    timed("fetch_regulation", regulation_tools.fetch_regulation, result_id=str(item.get("id") or ""))
            for unit_id, label in samples:
                if unit_id:
                    timed("get_regulation_article", regulation_tools.get_regulation_article, regulation_unit_id=unit_id, article_no=label)
            for unit_id in self.unit_by_title.values():
                timed("get_regulation_toc", regulation_tools.get_regulation_toc, regulation_unit_id=unit_id)
            timed("list_regulations", regulation_tools.list_regulations, page=1, page_size=100, require_hierarchy=True)
        return {name: _latency_summary(values) for name, values in sorted(warm.items())}


def _short_args(arguments: dict[str, Any]) -> dict[str, Any]:
    return {key: (str(value)[:80] if isinstance(value, str) else value) for key, value in arguments.items()}


# ---------------------------------------------------------------------------
# Negative path: ambiguous combined-book boundary must block publication
# ---------------------------------------------------------------------------


def _run_blocked_publication(ctx: E2EContext, recorder: StageRecorder, checks: CheckBook, redact: Redactor) -> dict[str, Any]:
    outcome: dict[str, Any] = {}

    def attempt_approval(record: dict[str, Any]) -> dict[str, Any]:
        manifest, manifest_relative, manifest_sha256 = _generate_batch_evidence(ctx, iteration=1, max_chunks_per_batch=100000)
        batches = [batch for batch in manifest.get("batches") or [] if str(batch.get("document_id") or "") == ctx.document_id]
        if not batches:
            return {"approval_error": None, "batches": 0}
        request = _approval_request_from_batch(
            batches[0], manifest_relative=manifest_relative, manifest_sha256=manifest_sha256, approval_id="approval-e2e-blocked"
        )
        try:
            with ctx.route_settings():
                routes_documents.approve_review_chunks(ctx.document_id, request, ctx.auth)
        except HTTPException as exc:
            return {"approval_error": _error_payload(exc, redact), "batches": len(batches)}
        return {"approval_error": None, "batches": len(batches)}

    ok, approval = recorder.run("approve", attempt_approval)
    outcome["approve"] = approval
    error = (approval or {}).get("approval_error") or {}
    checks.add(
        "blocked_approval_rejected",
        title="approve_review_chunks rejects the ambiguous book (HTTP 400, boundary message)",
        kind="blocking",
        passed=bool(ok and error.get("status_code") == 400 and AMBIGUOUS_APPROVAL_MESSAGE in str(error.get("detail") or "")),
        detail={"status_code": error.get("status_code"), "detail": error.get("detail")},
    )
    chunks = ctx.repository.get_chunks(ctx.document_id)
    approved = [chunk.chunk_id for chunk in chunks if chunk.approval_status == "approved"]
    journal = ctx.repository.list_approval_journal_records(ctx.document_id)
    checks.add(
        "blocked_nothing_approved",
        title="no chunk is approved and the approval journal stays empty",
        kind="blocking",
        passed=not approved and not journal,
        total=len(chunks),
        failed=len(approved),
        detail={"approval_journal_records": len(journal)},
    )

    def attempt_index(record: dict[str, Any]) -> dict[str, Any]:
        try:
            with ctx.route_settings():
                routes_documents.index_document(
                    ctx.document_id,
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=384, embedding_model=LOCAL_HASH_EMBEDDING_MODEL),
                    ctx.auth,
                )
        except HTTPException as exc:
            return {"index_error": _error_payload(exc, redact)}
        return {"index_error": None}

    ok, indexed = recorder.run("index", attempt_index)
    outcome["index"] = indexed
    index_error = (indexed or {}).get("index_error") or {}
    vector_path = ctx.settings.data_dir / "vector_db" / tenant_directory_key(ctx.tenant_id) / "approved_vectors.jsonl"
    vector_rows = 0
    if vector_path.is_file():
        vector_rows = sum(1 for line in vector_path.read_text(encoding="utf-8").splitlines() if line.strip())
    checks.add(
        "blocked_index_refused",
        title="index_document refuses to index (no approved chunk) and the vector store stays empty",
        kind="blocking",
        passed=bool(ok and index_error.get("status_code") == 400 and vector_rows == 0),
        detail={"status_code": index_error.get("status_code"), "detail": index_error.get("detail"), "vector_rows": vector_rows},
    )

    def attempt_bundle(record: dict[str, Any]) -> dict[str, Any]:
        try:
            write_mcp_runtime_data_bundle(
                source_data_dir=ctx.base_settings.data_dir,
                out_dir=ctx.bundle_dir,
                tenant_id=ctx.tenant_id,
                profile_id=ctx.profile_id,
                document_id=ctx.document_id,
                scope="document",
                tenant_storage_isolation=ctx.base_settings.tenant_storage_isolation,
                require_kordoc_table_parser=False,
            )
        except (ValueError, RuntimeError) as exc:
            return {"bundle_error": _error_payload(exc, redact)}
        return {"bundle_error": None}

    ok, bundled = recorder.run("bundle", attempt_bundle)
    outcome["bundle"] = bundled
    bundle_error = (bundled or {}).get("bundle_error") or {}
    published = (ctx.bundle_dir / "data" / "mcp_runtime_manifest.json").is_file()
    checks.add(
        "blocked_nothing_published",
        title="MCP bundle creation is refused and no runtime manifest/data is published",
        kind="blocking",
        passed=bool(ok and bundle_error and not published),
        detail={"error_type": bundle_error.get("type"), "message": bundle_error.get("message"), "runtime_manifest_present": published},
    )
    return outcome


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _git_commit() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None if completed.returncode == 0 else None


def _environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(terse=True),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "git_commit": _git_commit(),
        "project_dotenv_present": (PROJECT_ROOT / ".env").is_file(),
        "tokenizer": retrieval_tokenizer.tokenizer_name(),
    }


def _cold_start_child_code() -> str:
    return (
        "import asyncio, json, sys, time\n"
        "started = time.perf_counter()\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from app.mcp_server.regulation_server import create_regulation_mcp_server\n"
        "imported = time.perf_counter()\n"
        "server = create_regulation_mcp_server(data_dir=sys.argv[2], tenant_id=sys.argv[3], profile_id=sys.argv[4] or None,"
        " tool_profile='chatgpt-data', warm_cache=True)\n"
        "created = time.perf_counter()\n"
        "result = asyncio.run(server.call_tool('search', {'query': sys.argv[5]}))\n"
        "searched = time.perf_counter()\n"
        "print(json.dumps({'import_s': imported - started, 'create_and_warm_s': created - imported,"
        " 'first_search_s': searched - created}))\n"
    )


def _stage_mcp_cold_start(ctx: E2EContext, record: dict[str, Any]) -> dict[str, Any]:
    query = str(((ctx.ground_truth or {}).get("queries") or [{}])[0].get("query") or "규정의 목적")
    started = time.perf_counter()
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _cold_start_child_code(),
            str(PROJECT_ROOT),
            str(ctx.bundle_dir / "data"),
            ctx.tenant_id,
            ctx.profile_id,
            query,
        ],
        cwd=str(ctx.work_dir),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    wall = time.perf_counter() - started
    if completed.returncode != 0:
        raise RuntimeError(f"cold-start child failed: {completed.stderr[-1500:]}")
    timings = json.loads(completed.stdout.strip().splitlines()[-1])
    record["substages"].update({key: round(float(value), 4) for key, value in timings.items()})
    record["substages"]["interpreter_wall_s"] = round(wall, 4)
    return timings


def run_combined_book_e2e(
    *,
    input_path: str | Path,
    ground_truth_path: str | Path | None,
    work_dir: str | Path | None = None,
    keep_work_dir: bool = False,
    label: str | None = None,
    profile_dir: str | Path | None = None,
    trace_memory: bool = False,
    approval_batches: int = DEFAULT_APPROVAL_BATCHES,
    query_repeats: int = DEFAULT_QUERY_REPEATS,
    min_search_top1: float = DEFAULT_MIN_SEARCH_TOP1,
    min_search_top5: float = DEFAULT_MIN_SEARCH_TOP5,
    expect_approval_blocked: bool = False,
    tenant_id: str = DEFAULT_TENANT_ID,
    profile_id: str = DEFAULT_PROFILE_ID,
    tenant_storage_isolation: bool = False,
    measure_cold_start: bool = True,
) -> dict[str, Any]:
    """Run the full builder path for one book and return the regression report."""

    source = Path(input_path).resolve()
    truth = json.loads(Path(ground_truth_path).read_text(encoding="utf-8")) if ground_truth_path else None
    if truth is None and not expect_approval_blocked:
        raise ValueError("ground_truth_path is required unless expect_approval_blocked is set.")
    owns_work_dir = work_dir is None
    if work_dir is None:
        run_root = Path(tempfile.mkdtemp(prefix="combined_book_e2e_"))
    else:
        parent = Path(work_dir).resolve()
        parent.mkdir(parents=True, exist_ok=True)
        run_root = Path(tempfile.mkdtemp(prefix=f"run-{source.suffix.lstrip('.')}-", dir=parent))
    data_root = (PROJECT_ROOT / "data").resolve()
    if run_root == data_root or data_root in run_root.parents:
        raise ValueError("The disposable work directory must not be inside the project data directory.")
    redact = Redactor(
        {
            str(run_root): "<work>",
            str(source.parent): "<input-dir>",
            str(PROJECT_ROOT): "<repo>",
            str(Path(sys.prefix)): "<python-prefix>",
        }
    )
    label = label or source.suffix.lstrip(".")
    recorder = StageRecorder(
        profile_dir=Path(profile_dir).resolve() if profile_dir else None,
        profile_prefix=f"{label}_",
        trace_memory=trace_memory,
        redact=redact,
    )
    checks = CheckBook()
    started_at = datetime.now(timezone.utc)
    run_started = time.perf_counter()
    ctx = E2EContext(
        input_path=source,
        ground_truth=truth,
        work_dir=run_root,
        tenant_id=tenant_id,
        profile_id=profile_id,
        tenant_storage_isolation=tenant_storage_isolation,
    )
    pipeline: dict[str, Any] = {}
    verification: dict[str, Any] = {}
    try:
        # One-time process initializations (Kiwi model load) are measured on
        # their own so that the first stage using the tokenizer is comparable.
        recorder.run("runtime_init", lambda record: {"tokenizer": retrieval_tokenizer.tokenizer_name()})
        ok, pipeline["upload"] = recorder.run("upload", lambda record: _stage_upload(ctx, record))
        if ok:
            ok, pipeline["process"] = recorder.run("process", lambda record: _stage_process(ctx, record))
        process_ok = bool(ok and (pipeline.get("process") or {}).get("job_status") == "completed")
        if ctx.document_id:
            summary = _chunk_summary(ctx) if process_ok else {}
            pipeline["chunks"] = summary
        failed_runs = [status for status in (pipeline.get("process") or {}).get("run_statuses") or [] if status != "completed"]
        checks.add(
            "process_completed",
            title="processing job completed and no processing run failed",
            kind="pipeline",
            passed=process_ok and not failed_runs,
            detail={
                "job_status": (pipeline.get("process") or {}).get("job_status"),
                "job_error": redact((pipeline.get("process") or {}).get("job_error")) if (pipeline.get("process") or {}).get("job_error") else None,
                "failed_runs": len(failed_runs),
            },
        )
        ambiguous = int((pipeline.get("chunks") or {}).get("ambiguous_boundary_chunk_count") or 0)
        if expect_approval_blocked:
            checks.add(
                "ambiguous_boundary_detected",
                title="the ambiguous combined-book boundary diagnostic is raised on the chunks",
                kind="blocking",
                passed=process_ok and ambiguous > 0,
                total=int((pipeline.get("chunks") or {}).get("chunk_count") or 0),
                detail={"ambiguous_chunks": ambiguous, "diagnostics": (pipeline.get("chunks") or {}).get("structure_boundary_diagnostics")},
            )
            if process_ok:
                pipeline["blocked_publication"] = _run_blocked_publication(ctx, recorder, checks, redact)
        else:
            checks.add(
                "no_ambiguous_boundary",
                title="no chunk carries the ambiguous combined-book boundary diagnostic",
                kind="pipeline",
                passed=process_ok and ambiguous == 0,
                total=int((pipeline.get("chunks") or {}).get("chunk_count") or 0),
                failed=ambiguous,
                detail={"diagnostics": (pipeline.get("chunks") or {}).get("structure_boundary_diagnostics")},
            )
            if process_ok and truth is not None:
                _check_chunk_coverage(ctx, truth, checks)
            ok = process_ok
            if ok:
                ok, pipeline["approve"] = recorder.run(
                    "approve", lambda record: _stage_approve(ctx, record, approval_batches=approval_batches)
                )
            approve = pipeline.get("approve") or {}
            checks.add(
                "all_chunks_approved",
                title="every approvable chunk is approved through the approval route with journal records",
                kind="pipeline",
                passed=bool(
                    ok
                    and approve.get("remaining_unapproved_chunk_count") == 0
                    and approve.get("approved_chunk_count") == approve.get("approvable_before")
                    and approve.get("approval_journal_chunk_count") == approve.get("approved_chunk_count")
                ),
                total=approve.get("approvable_before"),
                failed=approve.get("remaining_unapproved_chunk_count"),
                detail={key: approve.get(key) for key in ("approved_chunk_count", "approval_journal_record_count", "approval_journal_chunk_count")},
            )
            if ok:
                ok, pipeline["index"] = recorder.run("index", lambda record: _stage_index(ctx, record))
            index = pipeline.get("index") or {}
            checks.add(
                "index_completed",
                title="index_document indexed every approved chunk",
                kind="pipeline",
                passed=bool(ok and index.get("status") == "indexed" and index.get("record_count") == ctx.approved_chunk_count),
                detail={"status": index.get("status"), "record_count": index.get("record_count"), "approved": ctx.approved_chunk_count},
            )
            if ok:
                ok, pipeline["bundle"] = recorder.run("bundle", lambda record: _stage_bundle(ctx, record))
            bundle = pipeline.get("bundle") or {}
            checks.add(
                "bundle_created",
                title="write_mcp_runtime_data_bundle produced a verified runtime bundle with every approved chunk",
                kind="pipeline",
                passed=bool(
                    ok
                    and bundle.get("runtime_manifest_present")
                    and bundle.get("hierarchical_index_status") == "ready"
                    and bundle.get("record_count") == ctx.approved_chunk_count
                ),
                detail={key: bundle.get(key) for key in ("record_count", "regulation_count", "toc_node_count", "hierarchical_index_status")},
            )
            if ok:
                ok_setup, pipeline["bundle_setup_files"] = recorder.run(
                    "bundle_setup_files", lambda record: _stage_bundle_setup_files(ctx, record)
                )
                checks.add(
                    "bundle_setup_files",
                    title="MCP client setup files are written next to the runtime data",
                    kind="pipeline",
                    passed=ok_setup,
                )
            if ok:
                ok, verification["warm"] = recorder.run("mcp_warm", lambda record: _stage_mcp_warm(ctx, record))
            servers: dict[str, Any] = {}
            if ok:
                ok_servers, servers = recorder.run("mcp_server_create", lambda record: _stage_mcp_server_create(ctx, record))
                servers = servers or {}
            if ok and truth is not None:
                verifier = McpVerifier(ctx, checks, redact)

                def verify(record: dict[str, Any]) -> dict[str, Any]:
                    for name, step in (
                        ("catalog", verifier.verify_catalog),
                        ("toc", verifier.verify_toc),
                        ("articles", verifier.verify_articles),
                        ("references", verifier.verify_references),
                        ("search_fetch", lambda: verifier.verify_search_and_fetch(min_top1=min_search_top1, min_top5=min_search_top5)),
                        ("fastmcp", lambda: verifier.verify_fastmcp(servers) if servers else checks.skip(
                            "fastmcp_tool_layer", title="FastMCP tool layer", reason="server creation failed")),
                    ):
                        step_started = time.perf_counter()
                        step()
                        record["substages"][f"{name}_s"] = round(time.perf_counter() - step_started, 4)
                    return {}

                recorder.run("mcp_verify", verify)
                verification["first_call_latency"] = {
                    name: _latency_summary(values) for name, values in sorted(verifier.latencies.items())
                }
                ok_latency, verification["warm_latency"] = recorder.run(
                    "mcp_query_latency", lambda record: verifier.measure_latency(query_repeats)
                )
                verification["call_errors"] = verifier.call_errors[:25]
                verification["call_error_count"] = len(verifier.call_errors)
                verification["search"] = verifier.search_rows
                verification["references"] = verifier.reference_summary
                verification["mcp_output_fingerprint"] = _sha256_json(verifier.fingerprint)
                verification["mcp_output_fingerprint_parts"] = {
                    key: _sha256_json(value) for key, value in sorted(verifier.fingerprint.items())
                }
                checks.add(
                    "mcp_calls_without_error",
                    title="no MCP tool call raised during verification",
                    kind="pipeline",
                    passed=not verifier.call_errors,
                    failed=len(verifier.call_errors),
                    samples=verifier.call_errors,
                )
            if ok and measure_cold_start:
                recorder.run("mcp_cold_start_subprocess", lambda record: _stage_mcp_cold_start(ctx, record))
    finally:
        total_wall = time.perf_counter() - run_started
        report = _build_report(
            label=label,
            source=source,
            truth=truth,
            started_at=started_at,
            total_wall=total_wall,
            recorder=recorder,
            checks=checks,
            pipeline=pipeline,
            verification=verification,
            options={
                "approval_batches": approval_batches,
                "query_repeats": query_repeats,
                "min_search_top1": min_search_top1,
                "min_search_top5": min_search_top5,
                "expect_approval_blocked": expect_approval_blocked,
                "tenant_storage_isolation": tenant_storage_isolation,
                "trace_memory": trace_memory,
                "profiled": profile_dir is not None,
                "work_dir_kept": bool(keep_work_dir),
            },
            redact=redact,
        )
        if keep_work_dir:
            report["work_dir_name"] = run_root.name
        elif owns_work_dir or work_dir is not None:
            shutil.rmtree(run_root, ignore_errors=True)
    return report


def _check_chunk_coverage(ctx: E2EContext, truth: dict[str, Any], checks: CheckBook) -> None:
    """Pre-MCP diagnostic: every ground-truth body article exists as a chunk."""

    chunks = ctx.repository.get_chunks(ctx.document_id)
    articles_by_title: dict[str, Counter[str]] = {}
    for chunk in chunks:
        metadata = chunk.metadata or {}
        if chunk.chunk_type != "article" or metadata.get("is_supplementary_provision"):
            continue
        if _location_is_addendum(str(metadata.get("canonical_hierarchy_path") or metadata.get("hierarchy_path") or "")):
            continue
        title = str(metadata.get("regulation_title") or "")
        number = str(metadata.get("article_no") or "")
        if number:
            articles_by_title.setdefault(title, Counter())[number] += 1
    failures: list[dict[str, Any]] = []
    for regulation in truth.get("regulations") or []:
        expected = Counter(str(article["label"]) for article in regulation.get("articles") or [])
        actual = Counter({key: 1 for key in articles_by_title.get(str(regulation["title"]), Counter())})
        missing = sorted((expected - actual).elements())
        if missing:
            failures.append(
                {
                    "regulation": regulation["title"],
                    "expected": sum(expected.values()),
                    "found": sum(1 for key in expected if key in actual),
                    "missing_sample": missing[:10],
                }
            )
    extra_titles = sorted(set(articles_by_title) - {str(r["title"]) for r in truth.get("regulations") or []})
    checks.add(
        "chunks_cover_ground_truth_articles",
        title="(pre-MCP) every ground-truth body article exists as an article chunk of the right regulation",
        kind="pipeline",
        passed=not failures,
        total=len(truth.get("regulations") or []),
        failed=len(failures),
        detail={"article_chunk_regulation_titles_not_in_truth": extra_titles[:20]},
        samples=failures,
    )


def _build_report(
    *,
    label: str,
    source: Path,
    truth: dict[str, Any] | None,
    started_at: datetime,
    total_wall: float,
    recorder: StageRecorder,
    checks: CheckBook,
    pipeline: dict[str, Any],
    verification: dict[str, Any],
    options: dict[str, Any],
    redact: Redactor,
) -> dict[str, Any]:
    failed_checks = [item["id"] for item in checks.records if item["status"] == "fail"]
    known = [item for item in failed_checks if item in KNOWN_DEFECT_CHECKS]
    stage_errors = [stage["name"] for stage in recorder.stages if stage["status"] != "ok"]
    report = {
        "report_type": REPORT_TYPE,
        "harness_version": HARNESS_VERSION,
        "label": label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "started_at": started_at.isoformat(),
        "total_wall_s": round(total_wall, 4),
        "input": {
            "file_name": source.name,
            "format": source.suffix.lower().lstrip("."),
            "bytes": source.stat().st_size if source.is_file() else None,
            "sha256": _sha256_file(source) if source.is_file() else None,
        },
        "ground_truth": None
        if truth is None
        else {
            "schema": truth.get("schema"),
            "generator_version": truth.get("generator_version"),
            "seed": truth.get("seed"),
            "format": truth.get("format"),
            "running_header": (truth.get("layout") or {}).get("running_header_on_every_pdf_page"),
            "pdf_page_count": (truth.get("layout") or {}).get("pdf_page_count"),
            "totals": truth.get("totals"),
        },
        "options": options,
        "environment": _environment(),
        "verdict": {
            "passed": not failed_checks and not stage_errors and any(item["status"] == "pass" for item in checks.records),
            "regression_passed": not stage_errors and not [item for item in failed_checks if item not in KNOWN_DEFECT_CHECKS],
            "failed_checks": failed_checks,
            "known_defect_failures": known,
            "stage_errors": stage_errors,
            "check_counts": dict(Counter(item["status"] for item in checks.records)),
        },
        "checks": checks.records,
        "stages": recorder.stages,
        "pipeline": pipeline,
        "verification": verification,
    }
    return json.loads(redact(json.dumps(report, ensure_ascii=False, default=str)).replace("\\\\", "\\\\"))


# ---------------------------------------------------------------------------
# Aggregation and comparison
# ---------------------------------------------------------------------------


def _stats(values: Sequence[float]) -> dict[str, Any]:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return {"n": 0}
    mean = statistics.fmean(clean)
    stdev = statistics.stdev(clean) if len(clean) > 1 else 0.0
    return {
        "n": len(clean),
        "mean": round(mean, 4),
        "stdev": round(stdev, 4),
        "cv_pct": round(100 * stdev / mean, 2) if mean else None,
        "min": round(min(clean), 4),
        "max": round(max(clean), 4),
        "values": [round(value, 4) for value in clean],
    }


def _flatten_substage_timings(stage: dict[str, Any]) -> dict[str, float]:
    flat: dict[str, float] = {}
    substages = stage.get("substages") or {}
    for key, value in substages.items():
        if isinstance(value, (int, float)) and key.endswith("_s"):
            flat[key] = float(value)
        elif key in {"phase_timings_ms", "timing_ms"} and isinstance(value, dict):
            for name, milliseconds in value.items():
                if isinstance(milliseconds, (int, float)):
                    flat[f"{key}.{name}"] = float(milliseconds) / 1000.0
        elif key == "progress_phases" and isinstance(value, list):
            for phase in value:
                name = f"phase.{phase.get('phase')}"
                flat[name] = flat.get(name, 0.0) + float(phase.get("duration_s") or 0.0)
    return flat


def aggregate_reports(reports: Sequence[dict[str, Any]], *, label: str) -> dict[str, Any]:
    stage_names: list[str] = []
    for report in reports:
        for stage in report.get("stages") or []:
            if stage["name"] not in stage_names:
                stage_names.append(stage["name"])
    stage_stats: dict[str, Any] = {}
    for name in stage_names:
        rows = [stage for report in reports for stage in report.get("stages") or [] if stage["name"] == name]
        substage_values: dict[str, list[float]] = {}
        for stage in rows:
            for key, value in _flatten_substage_timings(stage).items():
                substage_values.setdefault(key, []).append(value)
        stage_stats[name] = {
            "wall_s": _stats([stage.get("wall_s") for stage in rows]),
            "cpu_s": _stats([stage.get("cpu_s") for stage in rows]),
            "peak_rss_mb": _stats([stage.get("peak_rss_mb") for stage in rows]),
            "substages": {key: _stats(values) for key, values in substage_values.items()},
        }
    latency: dict[str, Any] = {}
    for report in reports:
        for name, summary in ((report.get("verification") or {}).get("warm_latency") or {}).items():
            latency.setdefault(name, {"p50_ms": [], "p95_ms": []})
            latency[name]["p50_ms"].append(summary.get("p50_ms"))
            latency[name]["p95_ms"].append(summary.get("p95_ms"))
    check_outcomes: dict[str, list[str]] = {}
    for report in reports:
        for check in report.get("checks") or []:
            check_outcomes.setdefault(check["id"], []).append(check["status"])
    fingerprints = {
        "mcp_output": sorted({str((report.get("verification") or {}).get("mcp_output_fingerprint")) for report in reports}),
        "chunk_content": sorted({str(((report.get("pipeline") or {}).get("chunks") or {}).get("chunk_content_fingerprint")) for report in reports}),
        "logical_corpus_sha256": sorted({str(((report.get("pipeline") or {}).get("bundle") or {}).get("logical_corpus_sha256")) for report in reports}),
    }
    return {
        "report_type": AGGREGATE_REPORT_TYPE,
        "harness_version": HARNESS_VERSION,
        "label": label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_count": len(reports),
        "input": (reports[0] if reports else {}).get("input"),
        "ground_truth": (reports[0] if reports else {}).get("ground_truth"),
        "environment": (reports[0] if reports else {}).get("environment"),
        "total_wall_s": _stats([report.get("total_wall_s") for report in reports]),
        "stage_stats": stage_stats,
        "warm_latency": {
            name: {"p50_ms": _stats(values["p50_ms"]), "p95_ms": _stats(values["p95_ms"])} for name, values in sorted(latency.items())
        },
        "check_outcomes": {
            check_id: {"statuses": statuses, "consistent": len(set(statuses)) == 1} for check_id, statuses in check_outcomes.items()
        },
        "fingerprints": fingerprints,
        "fingerprints_stable": all(len(values) == 1 for values in fingerprints.values()),
        "verdicts": [report.get("verdict") for report in reports],
        "runs": reports,
    }


def compare_with_baseline(report: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Flag checks that got worse and output fingerprints that changed."""

    def first_run(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("report_type") == AGGREGATE_REPORT_TYPE:
            return (payload.get("runs") or [{}])[0]
        return payload

    current = first_run(report)
    previous = first_run(baseline)
    previous_checks = {item["id"]: item for item in previous.get("checks") or []}
    regressions: list[dict[str, Any]] = []
    for check in current.get("checks") or []:
        before = previous_checks.get(check["id"])
        if before is None:
            continue
        got_worse = (before["status"] == "pass" and check["status"] == "fail") or (
            isinstance(check.get("failed"), int)
            and isinstance(before.get("failed"), int)
            and check["failed"] > before["failed"]
        ) or (
            isinstance(check.get("metric"), (int, float))
            and isinstance(before.get("metric"), (int, float))
            and check["metric"] < before["metric"]
        )
        if got_worse:
            regressions.append(
                {"id": check["id"], "before": {k: before.get(k) for k in ("status", "failed", "metric")}, "after": {k: check.get(k) for k in ("status", "failed", "metric")}}
            )
    changed_fingerprints = []
    for path in (("verification", "mcp_output_fingerprint"), ("pipeline", "chunks", "chunk_content_fingerprint")):
        before_value: Any = previous
        after_value: Any = current
        for key in path:
            before_value = (before_value or {}).get(key) if isinstance(before_value, dict) else None
            after_value = (after_value or {}).get(key) if isinstance(after_value, dict) else None
        if before_value and after_value and before_value != after_value:
            changed_fingerprints.append(".".join(path))
    return {
        "baseline_label": previous.get("label"),
        "check_regressions": regressions,
        "changed_fingerprints": changed_fingerprints,
        "passed": not regressions and not changed_fingerprints,
    }


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _check_counts(check: dict[str, Any]) -> str:
    parts = []
    if "total" in check:
        passed = check.get("passed_count", check.get("total"))
        parts.append(f"{passed}/{check['total']}")
    if "metric" in check:
        parts.append(f"metric {check['metric']}")
    if "threshold" in check:
        parts.append(f"threshold {check['threshold']}")
    return ", ".join(parts)


def render_markdown(report: dict[str, Any]) -> str:
    if report.get("report_type") == AGGREGATE_REPORT_TYPE:
        return _render_aggregate_markdown(report)
    lines: list[str] = []
    truth = report.get("ground_truth") or {}
    totals = truth.get("totals") or {}
    verdict = report.get("verdict") or {}
    lines.append(f"# Combined-book E2E regression: {report.get('label')}")
    lines.append("")
    source = report.get("input") or {}
    lines.append(
        f"- Input: `{source.get('file_name')}` ({source.get('bytes'):,} bytes, sha256 `{str(source.get('sha256'))[:16]}`)"
        if source.get("bytes")
        else f"- Input: `{source.get('file_name')}`"
    )
    if truth:
        lines.append(
            f"- Ground truth: {totals.get('regulations')} regulations, {totals.get('articles')} body articles, "
            f"{totals.get('queries')} queries, seed {truth.get('seed')}, running header {truth.get('running_header')}"
        )
    lines.append(
        f"- Verdict: **{'PASS' if verdict.get('passed') else 'FAIL'}** · checks {verdict.get('check_counts')} · "
        f"stage errors {verdict.get('stage_errors') or 'none'} · total wall {report.get('total_wall_s')} s"
    )
    lines.append(f"- Options: `{json.dumps(report.get('options'), ensure_ascii=False)}`")
    lines.append("")
    lines.append("## Checks")
    lines.append("")
    lines.append("| Check | Kind | Result | Counts | Title |")
    lines.append("|---|---|---|---|---|")
    for check in report.get("checks") or []:
        status = check["status"].upper() + (" (known defect)" if check.get("known_defect") else "")
        lines.append(f"| `{check['id']}` | {check['kind']} | {status} | {_md(_check_counts(check))} | {_md(check['title'])} |")
    lines.append("")
    lines.append("## Stage timings")
    lines.append("")
    lines.append("| Stage | Status | Wall s | CPU s | Peak RSS MB | RSS end MB |")
    lines.append("|---|---|---:|---:|---:|---:|")
    for stage in report.get("stages") or []:
        lines.append(
            f"| {stage['name']} | {stage['status']} | {stage.get('wall_s')} | {stage.get('cpu_s')} | "
            f"{stage.get('peak_rss_mb')} | {stage.get('rss_end_mb')} |"
        )
    for stage in report.get("stages") or []:
        flat = _flatten_substage_timings(stage)
        if not flat:
            continue
        lines.append("")
        lines.append(f"### {stage['name']} sub-stages")
        lines.append("")
        lines.append("| Sub-stage | Seconds |")
        lines.append("|---|---:|")
        for key, value in flat.items():
            lines.append(f"| {_md(key)} | {round(value, 4)} |")
    verification = report.get("verification") or {}
    if verification.get("search"):
        lines.append("")
        lines.append("## Search (top-5)")
        lines.append("")
        lines.append("| Query | Expected | Rank | Top-1 returned |")
        lines.append("|---|---|---:|---|")
        for row in verification["search"]:
            top = (row.get("top_results") or [""])[0] if row.get("top_results") else ""
            lines.append(f"| {row.get('id')} | {_md(row.get('expected'))} | {row.get('rank') or '-'} | {_md(top)} |")
    if verification.get("warm_latency"):
        lines.append("")
        lines.append("## MCP latency (warm, repeated)")
        lines.append("")
        lines.append("| Tool | n | p50 ms | p95 ms | max ms |")
        lines.append("|---|---:|---:|---:|---:|")
        for name, summary in verification["warm_latency"].items():
            lines.append(f"| {name} | {summary.get('n')} | {summary.get('p50_ms')} | {summary.get('p95_ms')} | {summary.get('max_ms')} |")
    if verification.get("first_call_latency"):
        lines.append("")
        lines.append("## MCP latency (first verification pass)")
        lines.append("")
        lines.append("| Tool | n | p50 ms | p95 ms | max ms |")
        lines.append("|---|---:|---:|---:|---:|")
        for name, summary in verification["first_call_latency"].items():
            lines.append(f"| {name} | {summary.get('n')} | {summary.get('p50_ms')} | {summary.get('p95_ms')} | {summary.get('max_ms')} |")
    failing = [check for check in report.get("checks") or [] if check["status"] == "fail"]
    if failing:
        lines.append("")
        lines.append("## Failure samples")
        for check in failing:
            lines.append("")
            lines.append(f"### `{check['id']}`")
            if check.get("detail"):
                lines.append("")
                lines.append(f"- detail: `{_md(json.dumps(check['detail'], ensure_ascii=False)[:1500])}`")
            for sample in (check.get("failure_samples") or [])[:8]:
                lines.append(f"- `{_md(json.dumps(sample, ensure_ascii=False)[:400])}`")
    errors = [stage for stage in report.get("stages") or [] if stage.get("error")]
    if errors:
        lines.append("")
        lines.append("## Stage errors")
        for stage in errors:
            error = stage["error"]
            lines.append("")
            lines.append(f"### {stage['name']}: {error.get('type')}")
            lines.append("")
            lines.append("```")
            lines.append(str(error.get("message"))[:1500])
            lines.extend(error.get("traceback_tail") or [])
            lines.append("```")
    lines.append("")
    lines.append(f"Environment: `{json.dumps(report.get('environment'), ensure_ascii=False)}`")
    return "\n".join(lines) + "\n"


def _render_aggregate_markdown(report: dict[str, Any]) -> str:
    lines = [f"# Combined-book E2E regression aggregate: {report.get('label')} ({report.get('run_count')} runs)", ""]
    source = report.get("input") or {}
    lines.append(f"- Input: `{source.get('file_name')}` sha256 `{str(source.get('sha256'))[:16]}`")
    lines.append(f"- Total wall s: `{json.dumps(report.get('total_wall_s'))}`")
    lines.append(f"- Output fingerprints stable across runs: **{report.get('fingerprints_stable')}**")
    lines.append("")
    lines.append("## Checks (status per run)")
    lines.append("")
    lines.append("| Check | Statuses | Consistent |")
    lines.append("|---|---|---|")
    for check_id, outcome in report.get("check_outcomes", {}).items():
        lines.append(f"| `{check_id}` | {', '.join(outcome['statuses'])} | {outcome['consistent']} |")
    lines.append("")
    lines.append("## Stage wall time (mean ± stdev over runs)")
    lines.append("")
    lines.append("| Stage | mean s | stdev s | CV % | min s | max s | peak RSS MB (mean) |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for name, stats in report.get("stage_stats", {}).items():
        wall = stats["wall_s"]
        rss = stats["peak_rss_mb"]
        lines.append(
            f"| {name} | {wall.get('mean')} | {wall.get('stdev')} | {wall.get('cv_pct')} | {wall.get('min')} | {wall.get('max')} | {rss.get('mean')} |"
        )
    for name, stats in report.get("stage_stats", {}).items():
        if not stats.get("substages"):
            continue
        lines.append("")
        lines.append(f"### {name} sub-stages")
        lines.append("")
        lines.append("| Sub-stage | mean s | stdev s | min s | max s |")
        lines.append("|---|---:|---:|---:|---:|")
        for key, value in stats["substages"].items():
            lines.append(f"| {_md(key)} | {value.get('mean')} | {value.get('stdev')} | {value.get('min')} | {value.get('max')} |")
    if report.get("warm_latency"):
        lines.append("")
        lines.append("## MCP warm latency (mean of per-run percentiles)")
        lines.append("")
        lines.append("| Tool | p50 ms | p95 ms |")
        lines.append("|---|---:|---:|")
        for name, values in report["warm_latency"].items():
            lines.append(f"| {name} | {values['p50_ms'].get('mean')} | {values['p95_ms'].get('mean')} |")
    lines.append("")
    lines.append("Per-run details are in the JSON report (`runs`).")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="End-to-end regression of one combined regulation book through the builder path.")
    parser.add_argument("--input", required=True, help="combined book file (.docx/.pdf/.hwpx/.hwp)")
    parser.add_argument("--ground-truth", default=None, help="ground-truth JSON written by the synthetic book generator")
    parser.add_argument("--work-dir", default=None, help="parent directory for the disposable runtime (default: system temp)")
    parser.add_argument("--keep-work-dir", action="store_true", help="keep the disposable runtime after the run")
    parser.add_argument("--label", default=None, help="report label and .prof file prefix (default: input format)")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--profile-dir", default=None, help="write one cProfile .prof file per stage into this directory")
    parser.add_argument("--tracemalloc", action="store_true", help="record tracemalloc peaks per stage (slow; timings not comparable)")
    parser.add_argument("--runs", type=int, default=1, help="repeat in N fresh interpreters and aggregate")
    parser.add_argument("--approval-batches", type=int, default=DEFAULT_APPROVAL_BATCHES)
    parser.add_argument("--query-repeats", type=int, default=DEFAULT_QUERY_REPEATS)
    parser.add_argument("--min-search-top1", type=float, default=DEFAULT_MIN_SEARCH_TOP1)
    parser.add_argument("--min-search-top5", type=float, default=DEFAULT_MIN_SEARCH_TOP5)
    parser.add_argument("--expect-approval-blocked", action="store_true", help="negative path: approval must be blocked, nothing published")
    parser.add_argument("--tenant-storage-isolation", action="store_true")
    parser.add_argument("--no-cold-start", action="store_true", help="skip the fresh-interpreter MCP cold-start measurement")
    parser.add_argument("--compare-baseline", default=None, help="baseline JSON report; flags checks that got worse and changed fingerprints")
    parser.add_argument("--fail-on-check", action="store_true", help="exit 2 when a check fails (or regresses against the baseline)")
    return parser.parse_args(argv)


def _child_argv(args: argparse.Namespace, *, out_json: Path, run_index: int) -> list[str]:
    argv = [sys.executable, str(Path(__file__).resolve()), "--input", args.input, "--out-json", str(out_json)]
    if args.ground_truth:
        argv += ["--ground-truth", args.ground_truth]
    if args.work_dir:
        argv += ["--work-dir", args.work_dir]
    if args.keep_work_dir:
        argv.append("--keep-work-dir")
    label = f"{args.label or Path(args.input).suffix.lstrip('.')}_run{run_index}"
    argv += ["--label", label]
    if args.profile_dir:
        argv += ["--profile-dir", args.profile_dir]
    if args.tracemalloc:
        argv.append("--tracemalloc")
    argv += [
        "--approval-batches",
        str(args.approval_batches),
        "--query-repeats",
        str(args.query_repeats),
        "--min-search-top1",
        str(args.min_search_top1),
        "--min-search-top5",
        str(args.min_search_top5),
    ]
    if args.expect_approval_blocked:
        argv.append("--expect-approval-blocked")
    if args.tenant_storage_isolation:
        argv.append("--tenant-storage-isolation")
    if args.no_cold_start:
        argv.append("--no-cold-start")
    return argv


def _write_outputs(report: dict[str, Any], args: argparse.Namespace) -> None:
    if args.out_json:
        Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_json).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.out_md:
        Path(args.out_md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_md).write_text(render_markdown(report), encoding="utf-8")


def _console_summary(report: dict[str, Any]) -> str:
    if report.get("report_type") == AGGREGATE_REPORT_TYPE:
        rows = [f"{name}: {stats['wall_s'].get('mean')}s ±{stats['wall_s'].get('stdev')}" for name, stats in report["stage_stats"].items()]
        return f"runs={report['run_count']} fingerprints_stable={report['fingerprints_stable']}\n" + "\n".join(rows)
    verdict = report.get("verdict") or {}
    rows = [f"{check['status'].upper():7} {check['id']} {_check_counts(check)}" for check in report.get("checks") or []]
    stages = [f"{stage['name']}: {stage.get('wall_s')}s peak {stage.get('peak_rss_mb')} MB [{stage['status']}]" for stage in report.get("stages") or []]
    return "\n".join([f"passed={verdict.get('passed')} regression_passed={verdict.get('regression_passed')}", *rows, *stages])


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.runs > 1:
        reports: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="combined_book_e2e_runs_") as scratch:
            for run_index in range(1, args.runs + 1):
                child_json = Path(scratch) / f"run{run_index}.json"
                completed = subprocess.run(_child_argv(args, out_json=child_json, run_index=run_index), check=False)
                if not child_json.is_file():
                    print(f"run {run_index} produced no report (exit {completed.returncode})", file=sys.stderr)
                    return 1
                reports.append(json.loads(child_json.read_text(encoding="utf-8")))
        report = aggregate_reports(reports, label=args.label or Path(args.input).suffix.lstrip("."))
    else:
        report = run_combined_book_e2e(
            input_path=args.input,
            ground_truth_path=args.ground_truth,
            work_dir=args.work_dir,
            keep_work_dir=args.keep_work_dir,
            label=args.label,
            profile_dir=args.profile_dir,
            trace_memory=args.tracemalloc,
            approval_batches=args.approval_batches,
            query_repeats=args.query_repeats,
            min_search_top1=args.min_search_top1,
            min_search_top5=args.min_search_top5,
            expect_approval_blocked=args.expect_approval_blocked,
            tenant_storage_isolation=args.tenant_storage_isolation,
            measure_cold_start=not args.no_cold_start,
        )
    if args.compare_baseline:
        report["baseline_comparison"] = compare_with_baseline(
            report, json.loads(Path(args.compare_baseline).read_text(encoding="utf-8"))
        )
    _write_outputs(report, args)
    print(_console_summary(report))
    if args.fail_on_check:
        if report.get("report_type") == AGGREGATE_REPORT_TYPE:
            passed = all((verdict or {}).get("regression_passed") for verdict in report.get("verdicts") or [])
        else:
            passed = bool((report.get("verdict") or {}).get("regression_passed"))
        if report.get("baseline_comparison") is not None:
            passed = passed and bool(report["baseline_comparison"]["passed"])
        return 0 if passed else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
