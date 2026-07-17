from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings
from app.schemas.chunk import ChunkOptions
from app.services.document_service import DocumentService
from app.services.processing_service import ProcessingService
from app.storage.repository import JsonRepository
from scripts.report_metadata import current_repo_commit


def run_large_document_preprocessing_stress(
    *,
    page_count: int,
    data_dir: Path,
    sample_pdf: Path,
    out_json: Path,
    out_md: Path,
    max_chunk_chars: int = 1800,
    overlap_chars: int = 120,
    include_table_rows: bool = True,
    force_regenerate_pdf: bool = False,
    max_elapsed_seconds: float | None = None,
    max_peak_tracemalloc_mb: float | None = None,
    min_pages_per_second: float | None = None,
) -> dict[str, Any]:
    if page_count <= 0:
        raise ValueError("page_count must be greater than zero.")
    _validate_positive_optional("max_elapsed_seconds", max_elapsed_seconds)
    _validate_positive_optional("max_peak_tracemalloc_mb", max_peak_tracemalloc_mb)
    _validate_positive_optional("min_pages_per_second", min_pages_per_second)
    if force_regenerate_pdf or not sample_pdf.is_file():
        generate_synthetic_regulation_pdf(sample_pdf, page_count=page_count, include_table_rows=include_table_rows)

    settings = Settings(data_dir=data_dir, enable_agent_review=False)
    repository = JsonRepository(settings)
    document_service = DocumentService(settings=settings, repository=repository)
    processing_service = ProcessingService(settings=settings, repository=repository)
    content = sample_pdf.read_bytes()
    document = document_service.upload(
        sample_pdf.name,
        content,
        document_name=f"합성 1000페이지 대용량 규정 스트레스 테스트" if page_count == 1000 else f"합성 {page_count}페이지 대용량 규정 스트레스 테스트",
        institution_name="대용량 스트레스 테스트 기관",
        source_system="SYNTHETIC_STRESS",
        source_record_id=f"synthetic-{page_count}-pages",
        source_file_id=sample_pdf.name,
        profile_id="default-public-institution",
    )
    options = ChunkOptions(
        max_chunk_chars=max_chunk_chars,
        overlap_chars=overlap_chars,
        chunk_mode="article",
        include_context_header=True,
        enable_table_extraction=True,
        enable_agent_review=False,
    )
    progress_events: list[dict[str, Any]] = []

    def _record_progress(job) -> None:
        progress_events.append(
            {
                "elapsed_seconds": round(time.perf_counter() - started_perf, 3),
                "progress": job.progress,
                "message": job.message,
                "status": job.status,
            }
        )

    started_at = datetime.now(timezone.utc)
    started_perf = time.perf_counter()
    tracemalloc.start()
    try:
        job = processing_service.process(document.document_id, options, progress_callback=_record_progress)
        current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    elapsed_seconds = round(time.perf_counter() - started_perf, 3)
    processed_document = document_service.get(document.document_id)
    quality_report = repository.get_quality_report(document.document_id)
    chunks = repository.get_chunks(document.document_id)
    latest_run = next(
        (run for run in reversed(repository.list_runs()) if run.document_id == document.document_id),
        None,
    )
    functional_passed = bool(
        job.status == "completed"
        and processed_document.page_count == page_count
        and quality_report is not None
        and quality_report.chunk_count == len(chunks)
        and len(chunks) > 0
    )
    pages_per_second = round(page_count / elapsed_seconds, 3) if elapsed_seconds > 0 else None
    peak_tracemalloc_mb = round(peak_bytes / (1024 * 1024), 3)
    performance_gate = _performance_gate(
        elapsed_seconds=elapsed_seconds,
        peak_tracemalloc_mb=peak_tracemalloc_mb,
        pages_per_second=pages_per_second,
        max_elapsed_seconds=max_elapsed_seconds,
        max_peak_tracemalloc_mb=max_peak_tracemalloc_mb,
        min_pages_per_second=min_pages_per_second,
    )
    passed = functional_passed and bool(performance_gate["passed"])
    report = {
        "report_type": "large_document_preprocessing_stress",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "passed": passed,
        "functional_passed": functional_passed,
        "started_at": started_at.isoformat(),
        "elapsed_seconds": elapsed_seconds,
        "pages_per_second": pages_per_second,
        "source_pdf": str(sample_pdf),
        "source_pdf_bytes": sample_pdf.stat().st_size,
        "source_pdf_sha256": hashlib.sha256(content).hexdigest(),
        "data_dir": str(data_dir),
        "page_count_requested": page_count,
        "document": {
            "document_id": processed_document.document_id,
            "document_name": processed_document.document_name,
            "page_count": processed_document.page_count,
            "status": processed_document.status,
            "error": processed_document.error,
        },
        "job": job.model_dump(mode="json"),
        "quality": quality_report.model_dump(mode="json") if quality_report else None,
        "chunk_count": len(chunks),
        "chunk_type_counts": _count_by(chunks, "chunk_type"),
        "peak_tracemalloc_bytes": peak_bytes,
        "peak_tracemalloc_mb": peak_tracemalloc_mb,
        "current_tracemalloc_bytes": current_bytes,
        "performance_gate": performance_gate,
        "progress_events": progress_events,
        "latest_run": latest_run.model_dump(mode="json") if latest_run else None,
        "interpretation": _interpretation(
            passed=passed,
            functional_passed=functional_passed,
            performance_gate=performance_gate,
            page_count=page_count,
            elapsed_seconds=elapsed_seconds,
            quality_report=quality_report.model_dump(mode="json") if quality_report else {},
        ),
        "safety_note": (
            "This stress test uses a synthetic embedded-text PDF. It proves the large text-PDF path, "
            "not scanned-image OCR throughput or every HWP/HWPX table-layout edge case."
        ),
    }
    _write_json(out_json, report)
    _write_text(out_md, _markdown(report))
    return report


def generate_synthetic_regulation_pdf(path: Path, *, page_count: int, include_table_rows: bool = True) -> None:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required to generate the synthetic stress PDF.") from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    document = fitz.open()
    for page_no in range(1, page_count + 1):
        page = document.new_page()
        article_no = page_no
        y = 54
        lines = [
            "합성 대용량 규정",
            f"제{article_no}조(대용량 처리 검증 {article_no})",
            (
                f"이 조문은 1000페이지급 규정 전처리 스트레스 테스트를 위한 본문이다. "
                f"페이지 번호는 {page_no}이며 파서가 원문 페이지 메타데이터를 유지해야 한다."
            ),
            "1. 기관은 문서 접수, 전처리, 품질검사, 검수, 승인, 색인을 순서대로 수행한다.",
            "2. 전처리 결과에는 조문 번호, 제목, 본문, 원문 페이지, 검수 상태가 남아야 한다.",
            "3. 승인 전 청크는 공식 RAG 또는 MCP 검색 색인에 포함하지 않는다.",
        ]
        if include_table_rows:
            lines.extend(
                [
                    "구분 | 항목 | 확인내용",
                    f"{page_no:04d} | 처리단계 | 업로드-파싱-청킹-품질검사",
                    f"{page_no:04d} | 검수단계 | 휴먼리뷰 후 승인",
                ]
            )
        lines.extend(
            [
                f"부칙 제{page_no}호 이 조문은 합성 테스트 생성일 이후 적용한다.",
                f"- {page_no}쪽 끝 -",
            ]
        )
        for line in lines:
            page.insert_text((54, y), line, fontname="korea", fontsize=10.5)
            y += 24
    document.save(path)
    document.close()


def _count_by(chunks: list[Any], field_name: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for chunk in chunks:
        value = str(getattr(chunk, field_name, "") or "missing")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _validate_positive_optional(name: str, value: float | None) -> None:
    if value is not None and value <= 0:
        raise ValueError(f"{name} must be greater than zero when provided.")


def _performance_gate(
    *,
    elapsed_seconds: float,
    peak_tracemalloc_mb: float,
    pages_per_second: float | None,
    max_elapsed_seconds: float | None,
    max_peak_tracemalloc_mb: float | None,
    min_pages_per_second: float | None,
) -> dict[str, Any]:
    thresholds = {
        "max_elapsed_seconds": max_elapsed_seconds,
        "max_peak_tracemalloc_mb": max_peak_tracemalloc_mb,
        "min_pages_per_second": min_pages_per_second,
    }
    configured = any(value is not None for value in thresholds.values())
    violations: list[dict[str, Any]] = []
    if max_elapsed_seconds is not None and elapsed_seconds > max_elapsed_seconds:
        violations.append(
            {
                "metric": "elapsed_seconds",
                "observed": elapsed_seconds,
                "operator": "<=",
                "threshold": max_elapsed_seconds,
            }
        )
    if max_peak_tracemalloc_mb is not None and peak_tracemalloc_mb > max_peak_tracemalloc_mb:
        violations.append(
            {
                "metric": "peak_tracemalloc_mb",
                "observed": peak_tracemalloc_mb,
                "operator": "<=",
                "threshold": max_peak_tracemalloc_mb,
            }
        )
    if min_pages_per_second is not None and (
        pages_per_second is None or pages_per_second < min_pages_per_second
    ):
        violations.append(
            {
                "metric": "pages_per_second",
                "observed": pages_per_second,
                "operator": ">=",
                "threshold": min_pages_per_second,
            }
        )
    return {
        "configured": configured,
        "status": "passed" if configured and not violations else "failed" if violations else "not_configured",
        "passed": not violations,
        "thresholds": thresholds,
        "observed": {
            "elapsed_seconds": elapsed_seconds,
            "peak_tracemalloc_mb": peak_tracemalloc_mb,
            "pages_per_second": pages_per_second,
        },
        "violations": violations,
    }


def _interpretation(
    *,
    passed: bool,
    functional_passed: bool,
    performance_gate: dict[str, Any],
    page_count: int,
    elapsed_seconds: float,
    quality_report: dict[str, Any],
) -> dict[str, Any]:
    quality_passed = bool(quality_report.get("passed"))
    return {
        "large_text_pdf_processed": functional_passed,
        "performance_budget_passed": bool(performance_gate.get("passed")),
        "publish_claim_scope": (
            "large embedded-text PDF preprocessing path"
            if passed
            else "not ready to claim large embedded-text PDF processing"
        ),
        "not_proven_by_this_test": [
            "scanned-image OCR throughput",
            "institution-specific HWP native table geometry at 1000 pages",
            "every merged-cell or image-table edge case",
        ],
        "quality_gate_passed": quality_passed,
        "operator_message": (
            f"{page_count}페이지 텍스트 PDF는 {elapsed_seconds:.1f}초에 끝까지 전처리됐습니다."
            if passed
            else (
                f"{page_count}페이지 텍스트 PDF는 처리됐지만 설정한 성능 예산을 통과하지 못했습니다."
                if functional_passed
                else f"{page_count}페이지 텍스트 PDF 전처리 증적이 통과하지 못했습니다."
            )
        ),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _markdown(report: dict[str, Any]) -> str:
    quality = report.get("quality") or {}
    interpretation = report.get("interpretation") or {}
    performance_gate = report.get("performance_gate") or {}
    lines = [
        "# Large Document Preprocessing Stress",
        "",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Functional processing passed: `{str(report.get('functional_passed')).lower()}`",
        f"- Pages: {report.get('page_count_requested')}",
        f"- Elapsed seconds: {report.get('elapsed_seconds')}",
        f"- Pages per second: {report.get('pages_per_second')}",
        f"- Source PDF bytes: {report.get('source_pdf_bytes')}",
        f"- Peak Python traced memory MB: {report.get('peak_tracemalloc_mb')}",
        f"- Document status: `{(report.get('document') or {}).get('status')}`",
        f"- Document page count: {(report.get('document') or {}).get('page_count')}",
        f"- Chunk count: {report.get('chunk_count')}",
        f"- Quality passed: `{str(quality.get('passed')).lower()}`",
        f"- Quality score: {quality.get('score')}",
        f"- Issue count: {quality.get('issue_count')}",
        f"- Performance gate: `{performance_gate.get('status')}`",
        f"- Performance violations: {len(performance_gate.get('violations') or [])}",
        "",
        "## Interpretation",
        "",
        f"- Scope: {interpretation.get('publish_claim_scope')}",
        f"- Operator message: {interpretation.get('operator_message')}",
        "",
        "## Not Proven By This Test",
        "",
    ]
    for item in interpretation.get("not_proven_by_this_test") or []:
        lines.append(f"- {item}")
    lines.extend(["", f"> {report.get('safety_note')}", ""])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    parser = argparse.ArgumentParser(description="Run a large synthetic PDF preprocessing stress test.")
    parser.add_argument("--pages", type=int, default=1000)
    parser.add_argument("--data-dir", type=Path, default=Path("data") / "large_document_stress_runtime")
    parser.add_argument(
        "--sample-pdf",
        type=Path,
        default=Path("reports") / "large_document_stress" / "synthetic_regulation_1000p.pdf",
    )
    parser.add_argument("--out-json", type=Path, default=Path("reports") / "large_document_stress" / "stress_1000p.json")
    parser.add_argument("--out-md", type=Path, default=Path("reports") / "large_document_stress" / "stress_1000p.md")
    parser.add_argument("--max-chunk-chars", type=int, default=1800)
    parser.add_argument("--overlap-chars", type=int, default=120)
    parser.add_argument("--no-table-rows", action="store_true")
    parser.add_argument("--force-regenerate-pdf", action="store_true")
    parser.add_argument("--max-elapsed-seconds", type=float)
    parser.add_argument("--max-peak-tracemalloc-mb", type=float)
    parser.add_argument("--min-pages-per-second", type=float)
    args = parser.parse_args(argv)
    report = run_large_document_preprocessing_stress(
        page_count=args.pages,
        data_dir=args.data_dir,
        sample_pdf=args.sample_pdf,
        out_json=args.out_json,
        out_md=args.out_md,
        max_chunk_chars=args.max_chunk_chars,
        overlap_chars=args.overlap_chars,
        include_table_rows=not args.no_table_rows,
        force_regenerate_pdf=args.force_regenerate_pdf,
        max_elapsed_seconds=args.max_elapsed_seconds,
        max_peak_tracemalloc_mb=args.max_peak_tracemalloc_mb,
        min_pages_per_second=args.min_pages_per_second,
    )
    print(
        json.dumps(
            {
                "ok": bool(report["passed"]),
                "json": str(args.out_json),
                "markdown": str(args.out_md),
                "pages": report["page_count_requested"],
                "elapsed_seconds": report["elapsed_seconds"],
                "pages_per_second": report["pages_per_second"],
                "chunk_count": report["chunk_count"],
                "quality_passed": (report.get("quality") or {}).get("passed"),
                "quality_score": (report.get("quality") or {}).get("score"),
                "performance_gate": report.get("performance_gate"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
