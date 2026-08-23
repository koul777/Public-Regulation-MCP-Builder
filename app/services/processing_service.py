from __future__ import annotations

import json
import hashlib
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.core.config import Settings, get_settings
from app.core.failure_classification import classify_processing_failure
from app.core.pipeline import processing_options_payload
from app.agents.review_executor import AgentReviewExecutor
from app.agents.review_policy import AgentReviewPolicy
from app.agents.local_structure_review import LocalStructureReviewAgent, apply_structure_review
from app.agents.local_table_review import LocalTableReviewAgent, apply_table_review
from app.parsers.factory import get_parser
from app.parsers.base import ParserError
from app.parsers.extraction_quality import build_extraction_quality_report
from app.pipelines.definitions import PREPROCESSING_PIPELINE_ID, PipelineStageTracker, get_pipeline_definition
from app.processors.chunker import Chunker
from app.processors.exporter import Exporter
from app.processors.kordoc_table_parser import KordocTableParser
from app.processors.normalizer import TextNormalizer
from app.processors.quality_gate import (
    QualityGate,
    QualityProfileConfig,
    load_quality_gate_profile_config,
    quality_profile_config_to_bytes,
)
from app.processors.structure_detector import StructureDetector
from app.processors.validator import Validator
from app.schemas.chunk import ChunkOptions
from app.schemas.document import ProcessingJob
from app.schemas.run import ProcessingRun
from app.storage.file_store import FileStore
from app.storage.repository import JsonRepository
from app.services.document_service import DocumentService
from app.services.regulation_metadata_service import (
    infer_regulation_metadata,
    is_generic_regulation_title,
)


AGENT_REVIEW_FINDINGS_KEY = "agent_review_findings"

# 지적이 아닌데 지적 자리에 들어오는 문구. 실제 실행 기록에서 600건 중
# "No parsing risks identified." 18건, "Spacing and line break consistency" 15건처럼
# 같은 빈 말이 반복됐다. 이런 줄이 화면에 200개 쌓이면 진짜 지적까지 안 읽게 된다.
#
# "제3조 본문이 없음"처럼 '없음'으로 끝나는 진짜 지적을 지우면 안 되므로, 한국어 쪽은
# 정해진 짧은 관용구 전체와 일치할 때만 걸러낸다.
_AGENT_REVIEW_NON_FINDING = re.compile(
    r"(?i)^\W*(?:"
    r"no\s+(?:parsing\s+)?(?:risks?|issues?|problems?)\b.*"
    r"|none|n/?a"
    r"|spacing\s+and\s+line\s+break\s+consistency"
    r"|(?:지적\s*사항|문제점?|이상|위험\s*요소?|특이\s*사항|해당)?\s*없(?:음|습니다)"
    r")\W*$"
)


def _agent_review_finding_texts(raw_issues: object) -> list[str]:
    """지적으로 볼 수 있는 줄만 남긴다."""
    if not isinstance(raw_issues, list):
        return []
    kept: list[str] = []
    for issue in raw_issues:
        text = str(issue or "").strip()
        if not text or _AGENT_REVIEW_NON_FINDING.match(text):
            continue
        kept.append(text)
    return kept


def _apply_ai_review_findings(chunks: list, agent_review_plan: dict) -> None:
    """AI가 낸 지적을 조항 메타데이터에 붙인다. 본문은 건드리지 않는다.

    본문을 AI가 다시 쓰게 했을 때, 되돌아온 교정본의 77%가 원문과 완전히 같았고
    실제로 바뀐 것들은 ``2012. 6. 14.``를 ``2012. 06. 14.``로 고치는 식으로 규정
    원문에 없는 표기를 만들어 냈다. 저장된 본문은 MCP가 인용할 법적 근거라
    사람이 승인 화면에서 직접 고쳐야 하고, AI는 어디를 볼지 짚어 주는 데까지만 쓴다.
    """

    review_json = agent_review_plan.get("provider_review_json") if isinstance(agent_review_plan, dict) else None
    items = review_json.get("items") if isinstance(review_json, dict) else None
    if not isinstance(items, list):
        return
    chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
    for item in items:
        if not isinstance(item, dict):
            continue
        chunk = chunks_by_id.get(str(item.get("chunk_id") or ""))
        if chunk is None:
            continue
        issues = _agent_review_finding_texts(item.get("issues"))
        recommended = str(item.get("recommended_human_check") or "").strip()
        if _AGENT_REVIEW_NON_FINDING.match(recommended):
            recommended = ""
        risk_level = str(item.get("risk_level") or "").strip().lower()
        if not issues and not recommended:
            continue
        chunk.metadata = {
            **dict(getattr(chunk, "metadata", {}) or {}),
            AGENT_REVIEW_FINDINGS_KEY: {
                "risk_level": risk_level or "medium",
                "issues": issues,
                "recommended_human_check": recommended,
            },
        }


def _agent_review_findings_of(chunk) -> dict:
    findings = (getattr(chunk, "metadata", None) or {}).get(AGENT_REVIEW_FINDINGS_KEY)
    return dict(findings) if isinstance(findings, dict) else {}


class ProcessingService:
    def __init__(
        self,
        settings: Settings | None = None,
        repository: JsonRepository | None = None,
        file_store: FileStore | None = None,
        quality_profile_config: QualityProfileConfig | None = None,
    ):
        self.settings = settings or get_settings()
        self.repository = repository or JsonRepository(self.settings)
        self.file_store = file_store or FileStore(self.settings)
        self.documents = DocumentService(self.settings, self.repository, self.file_store)
        self.normalizer = TextNormalizer()
        self.detector = StructureDetector()
        self.chunker = Chunker(self.settings)
        self.kordoc_table_parser = KordocTableParser(self.settings)
        self.validator = Validator()
        profile_config = quality_profile_config or load_quality_gate_profile_config(self.settings.quality_profiles_path)
        if quality_profile_config is not None and not profile_config.sha256:
            profile_config = profile_config.__class__(
                default_profile=profile_config.default_profile,
                profiles=profile_config.profiles,
                sha256=hashlib.sha256(quality_profile_config_to_bytes(profile_config)).hexdigest(),
            )
        self.quality_profiles_sha256 = profile_config.sha256
        self.quality_gate = QualityGate(
            default_profile=profile_config.default_profile,
            profiles=profile_config.profiles,
            strict_profile_ids=self.settings.quality_profiles_strict,
        )
        self.agent_review_policy = AgentReviewPolicy(self.settings)
        self.agent_review_executor = AgentReviewExecutor(self.settings)
        self.local_structure_review = LocalStructureReviewAgent(
            max_nodes=self.settings.local_structure_review_max_nodes
        )
        self.local_table_review = LocalTableReviewAgent(
            max_tables=self.settings.local_structure_review_max_nodes
        )
        self.exporter = Exporter()

    def process(
        self,
        document_id: str,
        options: ChunkOptions | None = None,
        progress_callback: Callable[[ProcessingJob], None] | None = None,
    ) -> ProcessingJob:
        options = options or ChunkOptions(
            max_chunk_chars=self.settings.default_max_chunk_chars,
            overlap_chars=self.settings.default_overlap_chars,
        )
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        started_at = datetime.now(timezone.utc)
        started_perf = time.perf_counter()
        phase_timings_ms: dict[str, float] = {}

        def record_phase(name: str, phase_started: float) -> None:
            phase_timings_ms[name] = round((time.perf_counter() - phase_started) * 1000, 3)

        document = self.documents.get(document_id)
        job = ProcessingJob(
            job_id=f"job_{uuid.uuid4().hex[:12]}",
            document_id=document_id,
            tenant_id=document.tenant_id,
            status="processing",
            progress=5,
            message="Processing started",
            pipeline_id=PREPROCESSING_PIPELINE_ID,
            stage_id="upload_admission",
            stage_number=1,
            stage_total=len(get_pipeline_definition(PREPROCESSING_PIPELINE_ID)),
            stage_status="completed",
        )
        pipeline_tracker = PipelineStageTracker(
            PREPROCESSING_PIPELINE_ID,
            tenant_id=document.tenant_id,
        )
        pipeline_tracker.start("upload_admission", detail={"source": "document.upload"})
        pipeline_tracker.set_agent_role_status(
            "upload_admission",
            "security_guard",
            status="completed",
            detail={"decision": "accepted"},
        )
        pipeline_tracker.set_agent_role_status(
            "upload_admission",
            "intake_guard",
            status="completed",
            detail={"decision": "accepted"},
        )
        pipeline_tracker.complete("upload_admission")

        def set_stage(stage_id: str, *, status: str, detail: dict | None = None) -> None:
            if status == "running":
                pipeline_tracker.start(stage_id, detail=detail)
            elif status == "completed":
                pipeline_tracker.complete(stage_id, detail=detail)
            elif status == "blocked":
                pipeline_tracker.block(stage_id, reason_code=str((detail or {}).get("reason_code") or "blocked"), detail=detail)
            elif status == "failed":
                pipeline_tracker.fail(stage_id, reason_code=str((detail or {}).get("reason_code") or "failed"), detail=detail)
            else:
                raise ValueError(f"Unsupported pipeline stage status: {status}")
            spec = next(item for item in get_pipeline_definition(PREPROCESSING_PIPELINE_ID) if item.stage_id == stage_id)
            job.pipeline_id = PREPROCESSING_PIPELINE_ID
            job.stage_id = stage_id
            job.stage_number = spec.order
            job.stage_total = len(get_pipeline_definition(PREPROCESSING_PIPELINE_ID))
            job.stage_status = status

        def set_role_status(
            stage_id: str,
            role_id: str,
            *,
            status: str,
            reason_code: str | None = None,
            detail: dict | None = None,
        ) -> None:
            pipeline_tracker.set_agent_role_status(
                stage_id,
                role_id,
                status=status,  # type: ignore[arg-type]
                reason_code=reason_code,
                detail=detail,
            )
        processing_options = processing_options_payload(
            options,
            settings=self.settings,
            quality_profiles_sha256=self.quality_profiles_sha256,
        )
        document_runs = self.repository.list_runs(document_id)
        previous_owner_run_id = (
            document_runs[-1].run_id
            if document_runs and document_runs[-1].status == "completed"
            else None
        )
        claim = self.repository.begin_processing_claim(
            document_id=document_id,
            run_id=run_id,
            job_id=job.job_id,
            previous_owner_run_id=previous_owner_run_id,
        )
        if not claim.acquired:
            active_job = (
                self.repository.get_job(claim.job_id) if claim.job_id else None
            )
            if active_job is None:
                active_job = ProcessingJob(
                    job_id=claim.job_id or f"active_{document_id}",
                    document_id=document_id,
                    tenant_id=document.tenant_id,
                    status="processing",
                    progress=5,
                    message="Processing is already active for this document",
                )
            self._notify_progress(active_job, progress_callback)
            return active_job

        terminal_outcome_committed = False
        try:
            self.repository.upsert_job(job)
            self._notify_progress(job, progress_callback)
            reusable_run = self.repository.latest_completed_run(
                document_id,
                options=processing_options,
                require_outputs=True,
                processing_claim_id=run_id,
            )
            if reusable_run is not None:
                job.status = "completed"
                job.progress = 100
                job.message = "Processing skipped; reusable completed run exists"
                job.stage_id = "export"
                job.stage_number = 7
                job.stage_total = 8
                job.stage_status = "completed"
                job.completed_at = datetime.now(timezone.utc)
                self.repository.upsert_job(job)
                self.repository.finish_processing_claim(
                    document_id=document_id,
                    run_id=run_id,
                    owner_run_id=reusable_run.run_id,
                )
                terminal_outcome_committed = True
                self._notify_progress(job, progress_callback)
                return job

            path = self.documents.path_for(document)
            job.progress = 15
            job.message = "원본 파일에서 텍스트를 추출하는 중"
            set_stage("parse_extract", status="running", detail={"file_type": document.file_type})
            set_role_status("parse_extract", "parser_extractor", status="running")
            set_role_status("parse_extract", "ocr_extractor", status="pending")
            self.repository.upsert_job(job)
            self._notify_progress(job, progress_callback)
            phase_started = time.perf_counter()
            parser = get_parser(path, settings=self.settings)
            parsed = parser.parse(path, document_id)
            extraction_quality = build_extraction_quality_report(parsed)
            set_role_status(
                "parse_extract",
                "parser_extractor",
                status="completed",
                detail={"status": extraction_quality.get("status")},
            )
            ocr_candidate = bool(
                extraction_quality.get("missing_content_page_numbers")
                or extraction_quality.get("image_block_count")
                or extraction_quality.get("embedded_image_page_numbers")
            )
            set_role_status(
                "parse_extract",
                "ocr_extractor",
                status="review_required" if ocr_candidate else "skipped",
                reason_code="ocr_candidate_detected" if ocr_candidate else "no_ocr_candidate",
                detail={
                    "missing_content_page_count": len(
                        extraction_quality.get("missing_content_page_numbers") or []
                    ),
                },
            )
            parsed = parsed.model_copy(
                update={
                    "metadata": {
                        **parsed.metadata,
                        "extraction_quality": extraction_quality,
                    }
                }
            )
            if not bool(extraction_quality.get("ready_for_normalization")):
                set_stage(
                    "parse_extract",
                    status="blocked",
                    detail={
                        "reason_code": "extraction_not_ready_for_normalization",
                        "extraction_status": extraction_quality.get("status"),
                    },
                )
                raise ParserError(
                    "Extraction produced no source text; normalization is blocked pending parser or OCR recovery."
                )
            record_phase("native_parse", phase_started)
            # Kordoc can take a while on large integrated HWP books; expose this
            # separately so the operator does not mistake the wait for a hang.
            job.progress = 22
            # 표 분석은 외부 프로세스라 중간 숫자를 셀 수 없다. 대신 이 기다림에
            # 정해진 한계가 있다는 사실만은 알려 준다.
            job.message = (
                "대용량 문서 표 구조를 분석하는 중입니다 (Kordoc · 최대 "
                f"{max(1, int(self.settings.kordoc_table_timeout_seconds))}초 대기)…"
            )
            self.repository.upsert_job(job)
            self._notify_progress(job, progress_callback)
            phase_started = time.perf_counter()
            kordoc_table_inventory = self.kordoc_table_parser.parse_file(path)
            record_phase("kordoc_table_parse", phase_started)
            job.progress = 27
            job.message = "표 구조 분석 완료 · 규정 메타데이터를 확인하는 중입니다…"
            self.repository.upsert_job(job)
            self._notify_progress(job, progress_callback)
            phase_started = time.perf_counter()
            # Both filename-only and content-aware inference must see the same
            # repository state.  A single snapshot avoids a second manifest,
            # legacy-store, and progress-sidecar scan for every document while
            # also making the paired metadata decisions deterministic.
            existing_documents = self.repository.list_documents()
            filename_detected = infer_regulation_metadata(
                document.filename,
                existing_documents=existing_documents,
                profile_id=document.profile_id,
                tenant_id=document.tenant_id,
            )
            detected = infer_regulation_metadata(
                document.filename,
                text=parsed.raw_text,
                existing_documents=existing_documents,
                profile_id=document.profile_id,
                tenant_id=document.tenant_id,
            )
            del existing_documents
            old_regulation_id = document.regulation_id
            old_regulation_version = document.regulation_version
            # 올린 사람이 규정 이름을 직접 정했으면 본문에서 찾은 제목으로 덮어쓰지 않는다.
            user_named_upload = str(getattr(document, "document_name_source", "auto") or "auto") == "user"
            auto_named_upload = not user_named_upload and (
                is_generic_regulation_title(document.document_name)
                or str(document.document_name or "").strip() == filename_detected.document_name
            )
            auto_grouped_upload = (
                not str(document.regulation_id or "").strip()
                or str(document.regulation_id or "").strip() == filename_detected.regulation_id
            )
            if detected.title_source == "content" and auto_named_upload:
                document.document_name = detected.document_name
            if detected.title_source == "content" and auto_grouped_upload:
                document.regulation_id = detected.regulation_id
            if detected.revision_date_source == "content":
                document.revision_date = detected.revision_date
            if detected.effective_from_source == "content":
                document.effective_from = detected.effective_from
            if detected.version_source == "content":
                document.regulation_version = detected.regulation_version
            if not document.supersedes_document_id and not document.reprocessing_source_document_id:
                document.supersedes_document_id = detected.supersedes_document_id
            if (
                document.regulation_id != old_regulation_id
                or document.regulation_version != old_regulation_version
            ):
                self.file_store.relocate_upload(
                    document.document_id,
                    document.filename,
                    old_regulation_id=old_regulation_id,
                    new_regulation_id=document.regulation_id,
                    profile_id=document.profile_id,
                    old_regulation_version=old_regulation_version,
                    new_regulation_version=document.regulation_version,
                )
            self.repository.upsert_document_progress(document)
            # Keep the full Kordoc inventory at document level only.  Copying it
            # into every chunk multiplies a multi-megabyte table list thousands
            # of times and can produce multi-GB result files.
            kordoc_summary = {
                "status": kordoc_table_inventory.get("status"),
                "table_count": kordoc_table_inventory.get("table_count", 0),
                "parser": kordoc_table_inventory.get("parser", "kordoc"),
                "elapsed_ms": kordoc_table_inventory.get("kordoc_elapsed_ms"),
                "input_extension": kordoc_table_inventory.get("kordoc_input_extension"),
                "timeout_seconds": kordoc_table_inventory.get("kordoc_timeout_seconds"),
                "tables_truncated": bool(kordoc_table_inventory.get("tables_truncated")),
            }
            document_metadata = {
                key: value
                for key, value in {
                    "institution_name": document.institution_name,
                    "apba_id": document.apba_id,
                    "source_system": document.source_system,
                    "source_url": document.source_url,
                    "source_record_id": document.source_record_id,
                    "source_file_id": document.source_file_id,
                    "source_disclosure_date": document.source_disclosure_date,
                    "source_posted_date": document.source_posted_date,
                    "profile_id": document.profile_id,
                    "regulation_id": document.regulation_id,
                    "regulation_version": document.regulation_version,
                    "revision_date": document.revision_date,
                    "effective_from": document.effective_from,
                    "effective_to": document.effective_to,
                    "repealed_at": document.repealed_at,
                    "regulation_status": document.regulation_status,
                    "supersedes_document_id": document.supersedes_document_id,
                    "reprocessing_source_document_id": document.reprocessing_source_document_id,
                    "reprocessing_reason": document.reprocessing_reason,
                    "tenant_id": document.tenant_id,
                    "kordoc_table_summary": kordoc_summary,
                }.items()
                if value
            }
            parsed = parsed.model_copy(
                update={
                    "source_file": document.filename,
                    "document_name": document.document_name or Path(document.filename).stem,
                    "metadata": {
                        **parsed.metadata,
                        **document_metadata,
                        "kordoc_table_inventory": kordoc_table_inventory,
                    },
                }
            )
            set_stage(
                "parse_extract",
                status="completed",
                detail={
                    "page_count": parsed.page_count,
                    "block_count": sum(len(page.blocks) for page in parsed.pages),
                    "kordoc_table_count": kordoc_summary.get("table_count", 0),
                    "extraction_status": extraction_quality.get("status"),
                    "extraction_review_required": bool(extraction_quality.get("review_required")),
                },
            )
            record_phase("metadata_inference_and_staging", phase_started)
            job.progress = 35
            job.message = "텍스트 추출 완료 · 통합 규정 구조를 분석하는 중"
            set_stage("normalize", status="running")
            self.repository.upsert_job(job)
            self._notify_progress(job, progress_callback)

            # 정리와 구조 분석은 규정 수십 개짜리 통합본에서 몇 분씩 걸린다.
            # 예전에는 그동안 35%에 멈춰 있어 죽은 화면처럼 보였다. 실제로 훑은
            # 쪽 수와 줄 수를 세어 알린다. 남은 시간은 추정하지 않는다.
            structure_phase_bounds = {
                "normalize": (35, 42, "본문 정리"),
                "scan": (42, 50, "조문 표시 찾기"),
                "assemble": (50, 58, "조문 계층 조립"),
            }

            def _structure_progress(phase: str, current: int, total: int) -> None:
                bounds = structure_phase_bounds.get(phase)
                if bounds is None:
                    # 모르는 단계 이름 때문에 전처리 자체가 죽으면 안 된다.
                    return
                start_percent, end_percent, label = bounds
                safe_total = max(int(total), 1)
                safe_current = max(0, min(int(current), safe_total))
                measured = start_percent + int(
                    (end_percent - start_percent) * safe_current / safe_total
                )
                job.progress = max(int(job.progress), measured)
                job.current_unit = safe_current
                job.total_units = safe_total
                job.unit_label = label
                job.message = f"{label} {safe_current:,}/{safe_total:,}"
                self._notify_progress(job, progress_callback)

            phase_started = time.perf_counter()
            set_role_status("normalize", "normalizer", status="running")
            normalized = self.normalizer.normalize_document(
                parsed,
                progress_callback=lambda current, total: _structure_progress(
                    "normalize", current, total
                ),
            )
            set_role_status(
                "normalize",
                "normalizer",
                status="completed",
                detail={"page_count": normalized.page_count},
            )
            set_stage(
                "normalize",
                status="completed",
                detail={"page_count": normalized.page_count, "raw_text_chars": len(normalized.raw_text or "")},
            )
            set_stage("structure_detect", status="running")
            set_role_status("structure_detect", "structure_detector", status="running")
            nodes = self.detector.detect(normalized, progress_callback=_structure_progress)
            set_role_status(
                "structure_detect",
                "structure_detector",
                status="completed",
                detail={"node_count": len(nodes)},
            )
            if self.settings.local_structure_review_enabled:
                set_role_status("structure_detect", "structure_reviewer", status="running")
                set_role_status("structure_detect", "table_reviewer", status="running")
                structure_review_report = self.local_structure_review.review(nodes)
                nodes = apply_structure_review(nodes, structure_review_report)
                table_review_report = self.local_table_review.review(nodes)
                nodes = apply_table_review(nodes, table_review_report)
            else:
                structure_review_report = None
                table_review_report = None
            for role_id, report, disabled_reason in (
                ("structure_reviewer", structure_review_report, "local_structure_review_not_enabled"),
                ("table_reviewer", table_review_report, "local_table_review_not_enabled"),
            ):
                if report is None:
                    set_role_status(
                        "structure_detect",
                        role_id,
                        status="skipped",
                        reason_code=disabled_reason,
                    )
                else:
                    set_role_status(
                        "structure_detect",
                        role_id,
                        status=(
                            "review_required"
                            if report.status == "review_required"
                            else "degraded"
                            if report.status == "degraded"
                            else "skipped"
                            if report.status == "skipped"
                            else "completed"
                        ),
                        reason_code=report.reason_code,
                        detail={
                            "candidate_count": report.candidate_count,
                            "finding_count": len(report.findings),
                            "model": report.model,
                        },
                    )
            normalized = normalized.model_copy(
                update={
                    "metadata": {
                        **dict(normalized.metadata or {}),
                        "local_structure_review": (
                            structure_review_report.model_dump(mode="json")
                            if structure_review_report is not None
                            else {
                                "status": "disabled",
                                "reason_code": "local_structure_review_not_enabled",
                            }
                        ),
                        "local_table_review": (
                            table_review_report.model_dump(mode="json")
                            if table_review_report is not None
                            else {
                                "status": "disabled",
                                "reason_code": "local_structure_review_not_enabled",
                            }
                        ),
                    }
                }
            )
            set_stage(
                "structure_detect",
                status="completed",
                detail={
                    "node_count": len(nodes),
                    "regulation_count": sum(1 for node in nodes if node.node_type == "regulation"),
                    "local_review_status": (
                        structure_review_report.status
                        if structure_review_report is not None
                        else "disabled"
                    ),
                    "local_review_model": (
                        structure_review_report.model
                        if structure_review_report is not None
                        else None
                    ),
                    "local_review_finding_count": (
                        len(structure_review_report.findings)
                        if structure_review_report is not None
                        else 0
                    ),
                    "local_table_review_status": (
                        table_review_report.status
                        if table_review_report is not None
                        else "disabled"
                    ),
                    "local_table_review_model": (
                        table_review_report.model
                        if table_review_report is not None
                        else None
                    ),
                    "local_table_review_finding_count": (
                        len(table_review_report.findings)
                        if table_review_report is not None
                        else 0
                    ),
                },
            )
            record_phase("normalize_and_structure_detect", phase_started)
            regulation_nodes = [node for node in nodes if node.node_type == "regulation"]
            regulation_total = len(regulation_nodes)
            job.progress = 60
            if regulation_total > 1:
                job.current_unit = 0
                job.total_units = regulation_total
                job.unit_label = "규정"
                job.message = f"통합 규정집 구조 분석 완료 · 규정 0/{regulation_total} 전처리 준비"
            else:
                job.message = "문서 구조 분석 완료 · 청크를 만드는 중"
            self.repository.upsert_job(job)
            self._notify_progress(job, progress_callback)

            def _regulation_progress(current: int, total: int, label: str) -> None:
                job.current_unit = current
                job.total_units = total
                job.unit_label = "규정"
                job.progress = min(74, 60 + int((current / max(total, 1)) * 14))
                job.message = f"통합 규정집 전처리 {current}/{total} · {label}"
                self.repository.upsert_job(job)
                self._notify_progress(job, progress_callback)

            phase_started = time.perf_counter()
            set_stage("chunk_generate", status="running")
            set_role_status("chunk_generate", "chunk_builder", status="running")
            chunks = self.chunker.build_chunks(
                nodes,
                normalized,
                options,
                regulation_progress_callback=_regulation_progress,
            )
            for chunk in chunks:
                chunk.metadata = {**document_metadata, **dict(chunk.metadata or {})}
            set_role_status(
                "chunk_generate",
                "chunk_builder",
                status="completed",
                detail={"chunk_count": len(chunks)},
            )
            set_stage("chunk_generate", status="completed", detail={"chunk_count": len(chunks)})
            set_stage("quality_gate", status="running")
            set_role_status("quality_gate", "quality_gate", status="running")
            set_role_status("quality_gate", "human_approval_gate", status="pending")
            issues = self.validator.validate(nodes, chunks, document_id, options)
            quality_report = self.quality_gate.evaluate(
                nodes,
                chunks,
                issues,
                document_id,
                normalized.raw_text,
                profile_id=document.profile_id,
                normalizer_metadata=normalized.metadata,
            )
            quality_report = quality_report.model_copy(
                update={"extraction_metrics": extraction_quality}
            )
            record_phase("chunk_validate_quality", phase_started)
            quality_passed = bool(quality_report.passed)
            set_role_status(
                "quality_gate",
                "quality_gate",
                status="completed" if quality_passed else "review_required",
                reason_code=None if quality_passed else "quality_review_required",
                detail={
                    "quality_score": quality_report.score,
                    "issue_count": len(issues),
                },
            )
            set_role_status(
                "quality_gate",
                "human_approval_gate",
                status="pending" if quality_passed else "review_required",
                reason_code="awaiting_human_approval",
            )
            set_stage(
                "quality_gate",
                status="completed",
                detail={
                    "quality_passed": bool(quality_report.passed),
                    "quality_score": quality_report.score,
                    "issue_count": len(issues),
                    "review_required": not bool(quality_report.passed),
                },
            )
            job.progress = 75
            job.message = (
                f"통합 규정집 {regulation_total}/{regulation_total} 구조화 완료 · 품질 검사 완료"
                if regulation_total > 1
                else "청크 생성과 품질 검사 완료"
            )
            self.repository.upsert_job(job)
            self._notify_progress(job, progress_callback)

            phase_started = time.perf_counter()
            review_cache_index = (
                self._agent_review_cache_index(
                    document.tenant_id,
                    cache_scope_hash=self.agent_review_policy.cache_scope_hash(),
                )
                if options.enable_agent_review
                else {}
            )
            agent_review_plan = self.agent_review_policy.plan(
                chunks,
                quality_report,
                options,
                cached_content_hashes=set(review_cache_index),
            )
            # 재사용은 '부르지 않는다'가 아니라 '이전 결과를 그대로 가져온다'는 뜻이다.
            # 가져오지 못한 조항은 캐시에서 빼고 다시 계획해, 이번 실행에서 검수한다.
            reused_candidates = self._reuse_cached_review_findings(
                chunks,
                agent_review_plan,
                review_cache_index,
            )
            if len(reused_candidates) != int(agent_review_plan.get("cached_candidate_count") or 0):
                agent_review_plan = self.agent_review_policy.plan(
                    chunks,
                    quality_report,
                    options,
                    cached_content_hashes={
                        str(candidate["content_hash"]) for candidate in reused_candidates
                    },
                )
            agent_review_plan.update(
                {
                    "reused_candidates": reused_candidates,
                    "reused_chunk_count": len(reused_candidates),
                    "reused_finding_count": sum(
                        1 for candidate in reused_candidates if candidate["has_findings"]
                    ),
                }
            )
            job.progress = 85
            job.message = (
                "전체 규정 AI 검수 초안을 준비하는 중"
                if agent_review_plan.get("request_enabled")
                else "전처리 결과 검증을 마무리하는 중"
            )
            self.repository.upsert_job(job)
            self._notify_progress(job, progress_callback)
            # AI 응답을 기다리는 동안이 전처리에서 가장 오래 멈춰 보이는 구간이다.
            # 끝난 요청 묶음 수를 세어 그동안에도 게이지가 실제로 올라가게 한다.
            def _agent_review_progress(completed: int, total: int) -> None:
                safe_total = max(int(total), 1)
                safe_completed = max(0, min(int(completed), safe_total))
                job.progress = max(
                    int(job.progress),
                    85 + int(6 * safe_completed / safe_total),
                )
                job.current_unit = safe_completed
                job.total_units = safe_total
                job.unit_label = "AI 검수 묶음"
                job.message = f"AI 검수 묶음 {safe_completed:,}/{safe_total:,} 완료"
                self._notify_progress(job, progress_callback)

            agent_review_plan = self.agent_review_executor.execute(
                document_id=document_id,
                run_id=run_id,
                plan=agent_review_plan,
                chunks=chunks,
                progress_callback=_agent_review_progress,
            )
            _apply_ai_review_findings(chunks, agent_review_plan)
            record_phase("agent_review", phase_started)
            job.progress = 92
            job.message = "전처리 결과와 저장 파일을 작성하는 중"
            self.repository.upsert_job(job)
            self._notify_progress(job, progress_callback)

            storage_labels = {
                "nodes": "구조 저장",
                "chunks": "청크 저장",
                "issues": "검사 결과 저장",
            }

            def _storage_progress(phase: str, current: int, total: int) -> None:
                job.progress = {"nodes": 93, "chunks": 94, "issues": 95}.get(phase, 94)
                job.current_unit = current
                job.total_units = total
                job.unit_label = storage_labels.get(phase, "결과 저장")
                job.message = f"{job.unit_label} 저장 {current}/{total} · 대용량 결과 저장 중 (잠시 기다려 주세요)"
                self._notify_progress(job, progress_callback)

            phase_started = time.perf_counter()
            self.repository.save_processing_result(
                document_id,
                nodes,
                chunks,
                issues,
                processing_claim_id=run_id,
                progress_callback=_storage_progress,
            )
            job.progress = 96
            job.current_unit = 1
            job.total_units = 1
            job.unit_label = "품질 보고서 저장"
            job.message = "품질 보고서 저장 1/1"
            self.repository.upsert_job(job)
            self._notify_progress(job, progress_callback)
            self.repository.save_quality_report(
                document_id,
                quality_report,
                processing_claim_id=run_id,
            )
            record_phase("processing_result_storage", phase_started)

            def _export_progress(
                label: str,
                current: int,
                total: int,
                overall_fraction: float,
            ) -> None:
                bounded_fraction = min(1.0, max(0.0, overall_fraction))
                job.progress = max(
                    job.progress,
                    min(99, 97 + int(bounded_fraction * 2)),
                )
                job.current_unit = current
                job.total_units = total
                job.unit_label = label
                job.message = f"내보내기 · {label} {current}/{total}"
                self._notify_progress(job, progress_callback)

            phase_started = time.perf_counter()
            set_stage("export", status="running")
            set_role_status("export", "exporter", status="running")
            artifacts = self._write_exports(
                document_id,
                chunks,
                issues,
                quality_report,
                agent_review_plan,
                progress_callback=_export_progress,
            )
            set_role_status(
                "export",
                "exporter",
                status="completed",
                detail={"artifact_count": len(artifacts)},
            )
            set_stage("export", status="completed", detail={"artifact_count": len(artifacts)})
            record_phase("exports", phase_started)
            phase_timings_ms["total_before_terminal_commit"] = round(
                (time.perf_counter() - started_perf) * 1000,
                3,
            )

            document.page_count = normalized.page_count
            if not user_named_upload:
                document.document_name = normalized.document_name
            document.status = "completed"
            document.processed_at = datetime.now(timezone.utc)

            job.status = "completed"
            job.progress = 100
            job.message = "통합 규정집 전처리 완료" if regulation_total > 1 else "전처리 완료"
            if regulation_total > 1:
                job.current_unit = regulation_total
                job.total_units = regulation_total
                job.unit_label = "규정"
            else:
                job.current_unit = None
                job.total_units = None
                job.unit_label = None
            job.completed_at = datetime.now(timezone.utc)
            run = ProcessingRun(
                run_id=run_id,
                document_id=document_id,
                job_id=job.job_id,
                tenant_id=document.tenant_id,
                status="completed",
                started_at=started_at,
                completed_at=job.completed_at,
                elapsed_seconds=round(time.perf_counter() - started_perf, 3),
                options=processing_options,
                stats=self._run_stats(
                    quality_report,
                    agent_review_plan,
                    phase_timings_ms=phase_timings_ms,
                    pipeline_trace=pipeline_tracker.snapshot(),
                ),
                artifacts=artifacts,
            )
            self.repository.commit_processing_outcome(
                document=document,
                job=job,
                run=run,
                processing_claim_id=run_id,
            )
            terminal_outcome_committed = True
            self._notify_progress(job, progress_callback)
            return job
        except Exception as exc:
            if terminal_outcome_committed:
                raise
            phase_timings_ms["total_before_terminal_commit"] = round(
                (time.perf_counter() - started_perf) * 1000,
                3,
            )
            failure = classify_processing_failure(exc, filename=document.filename)
            current_stage = pipeline_tracker.snapshot().get("current_stage_id")
            if current_stage:
                try:
                    set_stage(
                        current_stage,
                        status="failed",
                        detail={"reason_code": failure.failure_category, "error_type": type(exc).__name__},
                    )
                except (ValueError, StopIteration):
                    # A failure while recording the failure must not hide the
                    # original processing error or prevent terminal journaling.
                    pass
            if failure.ocr_page_count:
                document.page_count = failure.ocr_page_count
            document.status = "failed"
            document.error = str(exc)
            job.status = "failed"
            job.progress = 100
            job.message = "Processing failed"
            job.error = str(exc)
            job.completed_at = datetime.now(timezone.utc)
            run = ProcessingRun(
                run_id=run_id,
                document_id=document_id,
                job_id=job.job_id,
                tenant_id=document.tenant_id,
                status="failed",
                started_at=started_at,
                completed_at=job.completed_at,
                elapsed_seconds=round(time.perf_counter() - started_perf, 3),
                options=processing_options,
                stats={
                    "failure": failure.as_row_fields(),
                    "phase_timings_ms": phase_timings_ms,
                    "pipeline_trace": pipeline_tracker.snapshot(),
                },
                error=str(exc),
            )
            try:
                self.repository.commit_processing_outcome(
                    document=document,
                    job=job,
                    run=run,
                    processing_claim_id=run_id,
                )
            except Exception as terminal_commit_error:
                if hasattr(exc, "add_note"):
                    exc.add_note(
                        "Failed to persist the terminal failure outcome: "
                        f"{terminal_commit_error}"
                    )
                try:
                    # If the terminal manifest/journal commit rolled back, do
                    # not leave a same-process claim looking permanently live.
                    # Invalid ownership is conservative and permits a retry.
                    self.repository.finish_processing_claim(
                        document_id=document_id,
                        run_id=run_id,
                        owner_run_id=None,
                    )
                except Exception as claim_cleanup_error:
                    if hasattr(exc, "add_note"):
                        exc.add_note(
                            "Failed to invalidate the processing output claim: "
                            f"{claim_cleanup_error}"
                        )
                raise exc from terminal_commit_error
            self._notify_progress(job, progress_callback)
            raise

    def _notify_progress(
        self,
        job: ProcessingJob,
        progress_callback: Callable[[ProcessingJob], None] | None,
    ) -> None:
        if progress_callback is not None:
            progress_callback(job.model_copy(deep=True))

    def _write_exports(
        self,
        document_id: str,
        chunks,
        issues,
        quality_report,
        agent_review_plan: dict,
        *,
        progress_callback: Callable[[str, int, int, float], None] | None = None,
    ) -> dict[str, str]:
        export_names = (
            "jsonl",
            "csv",
            "md",
            "tables.jsonl",
            "tables.csv",
            "manifest.json",
            "quality.json",
            "quality.md",
            "agent_review_plan.json",
            "ai_review_draft.json",
        )
        export_total = len(export_names)
        artifacts: dict[str, str] = {}

        def report(
            label: str,
            current: int,
            total: int,
            overall_fraction: float,
        ) -> None:
            if progress_callback is not None:
                progress_callback(label, current, total, overall_fraction)

        streaming_writers = (
            ("jsonl", self.exporter.write_jsonl),
            ("csv", self.exporter.write_csv),
            ("md", self.exporter.write_markdown),
        )
        completed = 0
        for extension, writer in streaming_writers:
            path = self.file_store.export_path(document_id, extension)
            artifact_offset = completed
            writer(
                path,
                chunks,
                progress_callback=lambda current, total, name=extension, offset=artifact_offset: report(
                    f"{name} 청크",
                    current,
                    total,
                    (offset + (current / max(total, 1))) / export_total,
                ),
            )
            completed += 1
            artifacts[extension] = str(path)
            report(extension, completed, export_total, completed / export_total)

        for extension, writer in (
            ("tables.jsonl", self.exporter.write_tables_jsonl),
            ("tables.csv", self.exporter.write_tables_csv),
        ):
            path = self.file_store.export_path(document_id, extension)
            writer(path, chunks)
            completed += 1
            artifacts[extension] = str(path)
            report(extension, completed, export_total, completed / export_total)

        payloads = (
            ("manifest.json", self.exporter.manifest(chunks, issues), True),
            ("quality.json", quality_report.model_dump(mode="json"), True),
            ("quality.md", self.quality_gate.to_markdown(quality_report), False),
            ("agent_review_plan.json", agent_review_plan, True),
            ("ai_review_draft.json", agent_review_plan, True),
        )
        encoder = json.JSONEncoder(ensure_ascii=False, indent=2)
        for extension, payload, is_json in payloads:
            path = self.file_store.export_path(document_id, extension)
            with path.open("w", encoding="utf-8", newline="\n") as handle:
                if is_json:
                    for piece in encoder.iterencode(payload):
                        handle.write(piece)
                else:
                    handle.write(str(payload))
            completed += 1
            artifacts[extension] = str(path)
            report(extension, completed, export_total, completed / export_total)
        return artifacts

    def _run_stats(
        self,
        quality_report,
        agent_review_plan: dict | None = None,
        *,
        phase_timings_ms: dict[str, float] | None = None,
        pipeline_trace: dict | None = None,
    ) -> dict:
        agent_review_summary = dict(agent_review_plan or {})
        candidate_details = agent_review_summary.pop("candidates", None)
        if isinstance(candidate_details, list):
            # The complete plan is already written to agent_review_plan.json.
            # Duplicating every candidate in the mutable repository manifest
            # makes each progress/state write grow with the full corpus.
            agent_review_summary["candidate_details_artifact"] = "agent_review_plan.json"
        return {
            "quality_passed": quality_report.passed,
            "quality_score": quality_report.score,
            "node_count": quality_report.node_count,
            "chunk_count": quality_report.chunk_count,
            "issue_count": quality_report.issue_count,
            "table_metrics": quality_report.table_metrics,
            "metadata_coverage": quality_report.metadata_coverage,
            "structure_metrics": quality_report.structure_metrics,
            "coverage_metrics": quality_report.coverage_metrics,
            "extraction_metrics": quality_report.extraction_metrics,
            "agent_review": agent_review_summary,
            "phase_timings_ms": dict(phase_timings_ms or {}),
            "pipeline_trace": dict(pipeline_trace or {}),
        }

    def _agent_review_cache_index(
        self,
        tenant_id: str | None,
        *,
        cache_scope_hash: str,
    ) -> dict[str, tuple[str, str]]:
        """이미 검수한 내용 해시 → 그 결과가 남아 있는 (문서, 청크).

        같은 규정을 다시 올리면 내용 해시가 그대로라 제공자를 다시 부르지 않는다.
        예전에는 여기서 해시만 모아 호출을 건너뛰었는데, 그러면 새 문서에는 지적이
        하나도 붙지 않아 검수를 켠 운영자가 조항마다 'AI 검수 의견 없음'만 보게 됐다.
        결과를 가져오려면 어디에 남아 있는지까지 기억해야 한다.
        """
        tenant_key = str(tenant_id or "").strip()
        expected_scope = str(cache_scope_hash or "").strip()
        if not expected_scope:
            return {}
        # list_runs()는 시작 시각 오름차순이라, 같은 해시는 나중 실행 결과가 이긴다.
        index: dict[str, tuple[str, str]] = {}
        for run in self.repository.list_runs():
            if run.status != "completed":
                continue
            if tenant_key and str(run.tenant_id or "").strip() != tenant_key:
                continue
            agent_review = (run.stats or {}).get("agent_review") or {}
            if str(agent_review.get("cache_scope_hash") or "").strip() != expected_scope:
                continue
            if not self._agent_review_has_provider_result(agent_review):
                continue
            # 호출이 실패해 끝내 검토되지 못한 조항은 재사용 대상이 아니다.
            # 재사용하면 아무도 보지 않은 조항이 '검토 완료'로 남는다.
            unreviewed = {
                str(chunk_id or "").strip()
                for chunk_id in agent_review.get("unreviewed_chunk_ids") or []
            }
            for candidate in agent_review.get("selected_candidates") or []:
                if not isinstance(candidate, dict):
                    continue
                content_hash = str(candidate.get("content_hash") or "").strip()
                chunk_id = str(candidate.get("chunk_id") or "").strip()
                if not content_hash.startswith("sha256:") or not chunk_id:
                    continue
                if chunk_id in unreviewed:
                    continue
                index[content_hash] = (run.document_id, chunk_id)
        return index

    def _reuse_cached_review_findings(
        self,
        chunks: list,
        agent_review_plan: dict,
        cache_index: dict[str, tuple[str, str]],
    ) -> list[dict]:
        """재사용 대상 조항에 이전 지적을 붙이고, 실제로 재사용한 목록을 돌려준다.

        결과가 남아 있던 문서가 지워졌으면 재사용할 근거가 없다. 그런 조항은 목록에서
        빠지고, 호출하는 쪽이 다시 계획을 세워 이번 실행에서 검수한다.
        """
        reused_targets = [
            candidate
            for candidate in agent_review_plan.get("candidates") or []
            if isinstance(candidate, dict) and candidate.get("cache_status") == "reused"
        ]
        if not reused_targets:
            return []
        chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        source_findings: dict[str, dict[str, dict] | None] = {}
        reused: list[dict] = []
        for candidate in reused_targets:
            content_hash = str(candidate.get("content_hash") or "").strip()
            source = cache_index.get(content_hash)
            chunk = chunks_by_id.get(str(candidate.get("chunk_id") or ""))
            if not source or chunk is None:
                continue
            source_document_id, source_chunk_id = source
            if source_document_id not in source_findings:
                source_findings[source_document_id] = self._agent_review_findings_by_chunk(
                    source_document_id
                )
            findings_by_chunk = source_findings[source_document_id]
            if not findings_by_chunk or source_chunk_id not in findings_by_chunk:
                continue
            findings = findings_by_chunk[source_chunk_id]
            if findings:
                chunk.metadata = {
                    **dict(getattr(chunk, "metadata", {}) or {}),
                    AGENT_REVIEW_FINDINGS_KEY: findings,
                }
            reused.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "content_hash": content_hash,
                    "source_document_id": source_document_id,
                    "source_chunk_id": source_chunk_id,
                    # 지적 없이 깨끗하다고 판정된 것도 검수 결과다. 그 사실을 남겨야
                    # 승인 화면이 '검수 완료 · 지적 없음'과 '검수 안 됨'을 구분한다.
                    "has_findings": bool(findings),
                }
            )
        return reused

    def _agent_review_findings_by_chunk(self, document_id: str) -> dict[str, dict] | None:
        """이전 문서의 조항별 AI 지적. 문서를 더 읽을 수 없으면 None."""
        try:
            source_chunks = self.repository.get_chunks(document_id)
        except Exception:
            return None
        if not source_chunks:
            return None
        return {chunk.chunk_id: _agent_review_findings_of(chunk) for chunk in source_chunks}

    def _agent_review_has_provider_result(self, agent_review: dict) -> bool:
        if int(agent_review.get("api_call_count") or 0) > 0:
            return True
        if str(agent_review.get("provider_request_id") or "").strip():
            return True
        if str(agent_review.get("actual_cost") or "").strip():
            return True
        return str(agent_review.get("status") or "").strip().lower() in {"executed", "reviewed"}
