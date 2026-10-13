"""Repair parser boundary findings in a fresh, unapproved document only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.core.config import Settings
from app.core.pipeline import kordoc_table_command_status
from app.processors.quality_gate import QualityProfileConfig
from app.schemas.document import ProcessingJob
from app.services.document_service import DocumentService
from app.services.kordoc_reprocessing_service import (
    _document_kordoc_evidence,
    _recovery_lock,
    _reprocessing_chunk_options,
)
from app.services.processing_service import ProcessingService
from app.services.review_workflow_service import ambiguous_combined_book_chunk_ids
from app.storage.repository import JsonRepository


class BoundaryReprocessingError(RuntimeError):
    def __init__(self, message: str, *, draft_document_id: str = "") -> None:
        super().__init__(message)
        self.draft_document_id = draft_document_id


@dataclass(frozen=True)
class BoundaryReprocessingResult:
    source_document_id: str
    draft_document_id: str
    chunk_count: int


class BoundaryReprocessingService:
    """Never reuse an old parse, edit source chunks, approve, index, or deploy."""

    def __init__(self, settings: Settings, repository: JsonRepository,
                 *, quality_profile_config: QualityProfileConfig | None = None) -> None:
        self.settings = settings
        self.repository = repository
        self.documents = DocumentService(settings, repository)
        self.processing = ProcessingService(settings, repository,
                                            quality_profile_config=quality_profile_config)

    def recover(
        self, source_document_id: str, *, tenant_id: str, profile_id: str,
        progress_callback: Callable[[int, str, int | None, int | None], None] | None = None,
    ) -> BoundaryReprocessingResult:
        source = self.documents.get(source_document_id)
        if (str(source.tenant_id or "default") != str(tenant_id or "default")
                or str(source.profile_id or "") != str(profile_id or "")):
            raise BoundaryReprocessingError("현재 기관의 문서만 복구할 수 있습니다. 원본은 변경하지 않았습니다.")
        if not ambiguous_combined_book_chunk_ids(self.repository.get_chunks(source_document_id)):
            raise BoundaryReprocessingError("이 문서에는 복구할 규정 경계 차단이 없습니다.")
        kordoc_table_command_status.cache_clear()
        if (not self.settings.enable_kordoc_table_parser
                or not kordoc_table_command_status(str(self.settings.kordoc_table_command or "")).get("available")):
            raise BoundaryReprocessingError(
                "먼저 ① 전처리 화면에서 Kordoc 설치·사용 가능 상태를 확인하세요. "
                "새 초안은 아직 만들지 않았으며 기존 원본·승인본은 변경하지 않았습니다."
            )
        with _recovery_lock(f"boundary:{tenant_id}:{source_document_id}"):
            if progress_callback:
                progress_callback(2, "원본·승인본을 보존하고 새 초안 준비", 0, 1)
            try:
                draft = self.documents.clone_as_reprocessing_draft(
                    source_document_id, reason="structure_boundary_recovery",
                )
            except (OSError, ValueError, KeyError) as exc:
                raise BoundaryReprocessingError(
                    "저장된 원본을 확인할 수 없습니다. 원본 파일을 다시 올려 주세요. 기존 문서는 변경하지 않았습니다."
                ) from exc
            def report(job: ProcessingJob) -> None:
                if progress_callback:
                    progress_callback(10 + min(85, int(job.progress or 0) * 85 // 100),
                                      str(job.message or "최신 파서로 새 초안 분석 중"),
                                      job.current_unit, job.total_units)

            try:
                options = _reprocessing_chunk_options(self.repository, source_document_id, self.settings)
                # Parser repair is not permission to send content to an AI provider.
                options = options.model_copy(update={"enable_agent_review": False})
                job = self.processing.process(draft.document_id, options, progress_callback=report)
                chunks = self.repository.get_chunks(draft.document_id)
                blocked = ambiguous_combined_book_chunk_ids(chunks)
                status, parser, _ = _document_kordoc_evidence(self.repository, draft.document_id)
                if job.status != "completed" or not chunks:
                    raise ValueError("incomplete")
                if blocked:
                    raise BoundaryReprocessingError(
                        f"새 초안에도 조항 {len(blocked):,}개의 규정 경계 확인이 필요합니다. "
                        "자동 승인하지 않았으며 기존 문서는 유지됩니다. 원문 구조를 확인해 주세요.",
                        draft_document_id=draft.document_id,
                    )
                if status != "parsed" or parser != "kordoc":
                    raise BoundaryReprocessingError(
                        "새 초안의 Kordoc 표 파싱 증거를 확인하지 못했습니다. Kordoc 설치 상태를 확인해 주세요. "
                        "기존 문서는 유지되며 승인·색인은 실행하지 않았습니다.",
                        draft_document_id=draft.document_id,
                    )
            except BoundaryReprocessingError:
                raise
            except Exception as exc:
                raise BoundaryReprocessingError(
                    "새 초안 분석을 완료하지 못했습니다. 기존 원본·승인본은 유지되며 승인·색인은 실행하지 않았습니다.",
                    draft_document_id=draft.document_id,
                ) from exc
            if progress_callback:
                progress_callback(100, "규정 경계 복구 완료 · 새 초안 검수 필요", 1, 1)
            return BoundaryReprocessingResult(source_document_id, draft.document_id, len(chunks))
