from __future__ import annotations

import json
import hashlib
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
from app.parsers.factory import get_parser
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
        document = self.documents.get(document_id)
        job = ProcessingJob(
            job_id=f"job_{uuid.uuid4().hex[:12]}",
            document_id=document_id,
            tenant_id=document.tenant_id,
            status="processing",
            progress=5,
            message="Processing started",
        )
        self.repository.upsert_job(job)
        self._notify_progress(job, progress_callback)
        reusable_run = self.repository.latest_completed_run(
            document_id,
            options=processing_options_payload(
                options,
                settings=self.settings,
                quality_profiles_sha256=self.quality_profiles_sha256,
            ),
            require_outputs=True,
        )
        if reusable_run is not None:
            job.status = "completed"
            job.progress = 100
            job.message = "Processing skipped; reusable completed run exists"
            job.completed_at = datetime.now(timezone.utc)
            self.repository.upsert_job(job)
            self._notify_progress(job, progress_callback)
            return job

        try:
            path = self.documents.path_for(document)
            job.progress = 15
            job.message = "원본 파일에서 텍스트를 추출하는 중"
            self.repository.upsert_job(job)
            self._notify_progress(job, progress_callback)
            parser = get_parser(path, settings=self.settings)
            parsed = parser.parse(path, document_id)
            # Kordoc can take a while on large integrated HWP books; expose this
            # separately so the operator does not mistake the wait for a hang.
            job.progress = 22
            job.message = "대용량 문서 표 구조를 분석하는 중입니다 (Kordoc)…"
            self.repository.upsert_job(job)
            self._notify_progress(job, progress_callback)
            kordoc_table_inventory = self.kordoc_table_parser.parse_file(path)
            job.progress = 27
            job.message = "표 구조 분석 완료 · 규정 메타데이터를 확인하는 중입니다…"
            self.repository.upsert_job(job)
            self._notify_progress(job, progress_callback)
            filename_detected = infer_regulation_metadata(
                document.filename,
                existing_documents=self.repository.list_documents(),
                profile_id=document.profile_id,
                tenant_id=document.tenant_id,
            )
            detected = infer_regulation_metadata(
                document.filename,
                text=parsed.raw_text,
                existing_documents=self.repository.list_documents(),
                profile_id=document.profile_id,
                tenant_id=document.tenant_id,
            )
            old_regulation_id = document.regulation_id
            old_regulation_version = document.regulation_version
            auto_named_upload = (
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
            self.repository.upsert_document(document)
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
            job.progress = 35
            job.message = "텍스트 추출 완료 · 통합 규정 구조를 분석하는 중"
            self.repository.upsert_job(job)
            self._notify_progress(job, progress_callback)

            normalized = self.normalizer.normalize_document(parsed)
            nodes = self.detector.detect(normalized)
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

            chunks = self.chunker.build_chunks(
                nodes,
                normalized,
                options,
                regulation_progress_callback=_regulation_progress,
            )
            for chunk in chunks:
                chunk.metadata = {**document_metadata, **dict(chunk.metadata or {})}
            issues = self.validator.validate(nodes, chunks, document_id, options)
            quality_report = self.quality_gate.evaluate(
                nodes,
                chunks,
                issues,
                document_id,
                normalized.raw_text,
                profile_id=document.profile_id,
            )
            job.progress = 75
            job.message = (
                f"통합 규정집 {regulation_total}/{regulation_total} 구조화 완료 · 품질 검사 완료"
                if regulation_total > 1
                else "청크 생성과 품질 검사 완료"
            )
            self.repository.upsert_job(job)
            self._notify_progress(job, progress_callback)

            agent_review_plan = self.agent_review_policy.plan(
                chunks,
                quality_report,
                options,
                cached_content_hashes=self._agent_review_content_hash_cache(
                    document.tenant_id,
                    cache_scope_hash=self.agent_review_policy.cache_scope_hash(),
                ),
            )
            job.progress = 85
            job.message = "전체 규정 AI 검수 초안을 준비하는 중"
            self.repository.upsert_job(job)
            self._notify_progress(job, progress_callback)
            agent_review_plan = self.agent_review_executor.execute(
                document_id=document_id,
                run_id=run_id,
                plan=agent_review_plan,
                chunks=chunks,
            )
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

            self.repository.save_processing_result(
                document_id,
                nodes,
                chunks,
                issues,
                progress_callback=_storage_progress,
            )
            job.progress = 96
            job.current_unit = 1
            job.total_units = 1
            job.unit_label = "품질 보고서 저장"
            job.message = "품질 보고서 저장 1/1"
            self.repository.upsert_job(job)
            self._notify_progress(job, progress_callback)
            self.repository.save_quality_report(document_id, quality_report)

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

            artifacts = self._write_exports(
                document_id,
                chunks,
                issues,
                quality_report,
                agent_review_plan,
                progress_callback=_export_progress,
            )

            document.page_count = normalized.page_count
            document.document_name = normalized.document_name
            document.status = "completed"
            document.processed_at = datetime.now(timezone.utc)
            self.repository.upsert_document(document)

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
            self.repository.upsert_job(job)
            self._notify_progress(job, progress_callback)
            self.repository.upsert_run(
                ProcessingRun(
                    run_id=run_id,
                    document_id=document_id,
                    job_id=job.job_id,
                    tenant_id=document.tenant_id,
                    status="completed",
                    started_at=started_at,
                    completed_at=job.completed_at,
                    elapsed_seconds=round(time.perf_counter() - started_perf, 3),
                    options=processing_options_payload(
                        options,
                        settings=self.settings,
                        quality_profiles_sha256=self.quality_profiles_sha256,
                    ),
                    stats=self._run_stats(quality_report, agent_review_plan),
                    artifacts=artifacts,
                )
            )
            return job
        except Exception as exc:
            failure = classify_processing_failure(exc, filename=document.filename)
            if failure.ocr_page_count:
                document.page_count = failure.ocr_page_count
            document.status = "failed"
            document.error = str(exc)
            self.repository.upsert_document(document)
            job.status = "failed"
            job.progress = 100
            job.message = "Processing failed"
            job.error = str(exc)
            job.completed_at = datetime.now(timezone.utc)
            self.repository.upsert_job(job)
            self._notify_progress(job, progress_callback)
            self.repository.upsert_run(
                ProcessingRun(
                    run_id=run_id,
                    document_id=document_id,
                    job_id=job.job_id,
                    tenant_id=document.tenant_id,
                    status="failed",
                    started_at=started_at,
                    completed_at=job.completed_at,
                    elapsed_seconds=round(time.perf_counter() - started_perf, 3),
                    options=processing_options_payload(
                        options,
                        settings=self.settings,
                        quality_profiles_sha256=self.quality_profiles_sha256,
                    ),
                    stats={"failure": failure.as_row_fields()},
                    error=str(exc),
                )
            )
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

    def _run_stats(self, quality_report, agent_review_plan: dict | None = None) -> dict:
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
            "agent_review": agent_review_plan or {},
        }

    def _agent_review_content_hash_cache(self, tenant_id: str | None, *, cache_scope_hash: str) -> set[str]:
        tenant_key = str(tenant_id or "").strip()
        expected_scope = str(cache_scope_hash or "").strip()
        if not expected_scope:
            return set()
        hashes: set[str] = set()
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
            for candidate in agent_review.get("selected_candidates") or []:
                if not isinstance(candidate, dict):
                    continue
                content_hash = str(candidate.get("content_hash") or "").strip()
                if content_hash.startswith("sha256:"):
                    hashes.add(content_hash)
        return hashes

    def _agent_review_has_provider_result(self, agent_review: dict) -> bool:
        if int(agent_review.get("api_call_count") or 0) > 0:
            return True
        if str(agent_review.get("provider_request_id") or "").strip():
            return True
        if str(agent_review.get("actual_cost") or "").strip():
            return True
        return str(agent_review.get("status") or "").strip().lower() in {"executed", "reviewed"}
