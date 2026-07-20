from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from app.core.config import Settings, get_settings
from app.core.pipeline import kordoc_table_command_status
from app.processors.quality_gate import QualityProfileConfig
from app.schemas.chunk import Chunk, ChunkOptions
from app.schemas.document import Document, ProcessingJob
from app.services.document_service import DocumentService
from app.services.processing_service import ProcessingService
from app.storage.file_store import FileStore
from app.storage.repository import JsonRepository


KORDOC_REPROCESSING_REASON = "kordoc_evidence_recovery"
_RECOVERY_LOCKS_GUARD = threading.Lock()
_RECOVERY_LOCKS: dict[str, threading.Lock] = {}


class KordocReprocessingError(RuntimeError):
    """Raised when a safe recovery draft does not produce Kordoc evidence."""

    def __init__(self, message: str, *, draft_document_id: str | None = None):
        super().__init__(message)
        self.draft_document_id = draft_document_id


@dataclass(frozen=True)
class KordocReprocessingResult:
    source_document_id: str
    draft_document_id: str
    parser_status: str
    parser: str
    table_count: int
    reused: bool
    job: ProcessingJob | None = None


class KordocReprocessingService:
    """Create and process an isolated draft for missing Kordoc evidence."""

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
        self.processing = ProcessingService(
            self.settings,
            self.repository,
            self.file_store,
            quality_profile_config=quality_profile_config,
        )

    def recover(
        self,
        source_document_id: str,
        *,
        progress_callback: Callable[[int, str, int | None, int | None], None] | None = None,
    ) -> KordocReprocessingResult:
        source_document = self.documents.get(source_document_id)
        self._require_available_kordoc()
        lock_key = f"{source_document.tenant_id or 'default'}:{source_document.document_id}"
        with _recovery_lock(lock_key):
            return self._recover_locked(source_document, progress_callback)

    def _recover_locked(
        self,
        source_document: Document,
        progress_callback: Callable[[int, str, int | None, int | None], None] | None,
    ) -> KordocReprocessingResult:

        reusable = self._find_completed_recovery_draft(source_document)
        if reusable is not None:
            status, parser, table_count = _document_kordoc_evidence(self.repository, reusable.document_id)
            self._report(progress_callback, 100, "기존 Kordoc 재전처리 초안 확인", 1, 1)
            return KordocReprocessingResult(
                source_document_id=source_document.document_id,
                draft_document_id=reusable.document_id,
                parser_status=status,
                parser=parser,
                table_count=table_count,
                reused=True,
            )

        self._report(progress_callback, 2, "기존 승인본을 보존하고 새 초안 준비", 0, 2)

        def upload_progress(current: int, total: int | None) -> None:
            bounded_total = max(int(total or current or 1), 1)
            percent = 2 + min(10, int(int(current or 0) * 10 / bounded_total))
            self._report(progress_callback, percent, "원본을 새 초안으로 안전하게 복제", current, total)

        try:
            draft = self.documents.clone_as_reprocessing_draft(
                source_document.document_id,
                progress_callback=upload_progress,
            )
            options = _reprocessing_chunk_options(self.repository, source_document.document_id, self.settings)
        except Exception as exc:
            raise KordocReprocessingError(
                "저장된 원본을 검증하거나 안전한 재전처리 초안을 만들지 못했습니다. "
                "원본 파일을 다시 올린 뒤 재시도해 주세요."
            ) from exc

        def processing_progress(job: ProcessingJob) -> None:
            mapped_percent = 12 + min(83, int(int(job.progress or 0) * 83 / 100))
            self._report(
                progress_callback,
                mapped_percent,
                str(job.message or "Kordoc 재전처리"),
                job.current_unit,
                job.total_units,
            )

        try:
            job = self.processing.process(
                draft.document_id,
                options,
                progress_callback=processing_progress,
            )
        except Exception as exc:
            raise KordocReprocessingError(
                "Kordoc 재전처리를 완료하지 못했습니다. 실패 초안은 승인·색인되지 않았습니다.",
                draft_document_id=draft.document_id,
            ) from exc

        status, parser, table_count = _document_kordoc_evidence(self.repository, draft.document_id)
        if job.status != "completed" or status != "parsed" or parser != "kordoc":
            raise KordocReprocessingError(
                "재전처리는 끝났지만 Kordoc 표 파싱 증거가 생성되지 않았습니다. "
                f"status={status or 'missing'}, parser={parser or 'missing'}",
                draft_document_id=draft.document_id,
            )
        self._report(progress_callback, 100, "Kordoc 증거 검증 완료", 2, 2)
        return KordocReprocessingResult(
            source_document_id=source_document.document_id,
            draft_document_id=draft.document_id,
            parser_status=status,
            parser=parser,
            table_count=table_count,
            reused=False,
            job=job,
        )

    def _require_available_kordoc(self) -> None:
        if not self.settings.enable_kordoc_table_parser:
            raise KordocReprocessingError("Kordoc 표 파서가 설정에서 비활성화되어 있습니다.")
        kordoc_table_command_status.cache_clear()
        command_status = kordoc_table_command_status(str(self.settings.kordoc_table_command or ""))
        if not command_status.get("available"):
            raise KordocReprocessingError("현재 실행 환경에서 Kordoc 명령을 사용할 수 없습니다.")

    def _find_completed_recovery_draft(self, source_document: Document) -> Document | None:
        candidates = sorted(
            (
                document
                for document in self.repository.list_documents()
                if document.reprocessing_source_document_id == source_document.document_id
                and document.reprocessing_reason == KORDOC_REPROCESSING_REASON
                and document.file_hash == source_document.file_hash
                and document.status == "completed"
                and document.tenant_id == source_document.tenant_id
            ),
            key=lambda document: document.created_at,
            reverse=True,
        )
        for candidate in candidates:
            status, parser, _table_count = _document_kordoc_evidence(self.repository, candidate.document_id)
            if status == "parsed" and parser == "kordoc":
                return candidate
        return None

    @staticmethod
    def _report(
        callback: Callable[[int, str, int | None, int | None], None] | None,
        percent: int,
        message: str,
        current: int | None,
        total: int | None,
    ) -> None:
        if callback is not None:
            callback(percent, message, current, total)


def _reprocessing_chunk_options(
    repository: JsonRepository,
    document_id: str,
    settings: Settings,
) -> ChunkOptions:
    latest_run = repository.latest_completed_run(document_id)
    allowed_fields = set(ChunkOptions.model_fields)
    prior_options = dict(latest_run.options or {}) if latest_run is not None else {}
    values = {key: value for key, value in prior_options.items() if key in allowed_fields}
    values.setdefault("max_chunk_chars", settings.default_max_chunk_chars)
    values.setdefault("overlap_chars", settings.default_overlap_chars)
    values["enable_agent_review"] = True
    return ChunkOptions.model_validate(values)


def _recovery_lock(key: str) -> threading.Lock:
    with _RECOVERY_LOCKS_GUARD:
        return _RECOVERY_LOCKS.setdefault(key, threading.Lock())


def _document_kordoc_evidence(
    repository: JsonRepository,
    document_id: str,
) -> tuple[str, str, int]:
    try:
        chunks: list[Chunk] = repository.get_chunks(document_id)
    except Exception:
        chunks = []
    for chunk in chunks:
        metadata = dict(chunk.metadata or {})
        inventory = metadata.get("kordoc_table_inventory")
        inventory = inventory if isinstance(inventory, dict) else {}
        status = str(metadata.get("kordoc_table_parser_status") or inventory.get("status") or "").strip()
        parser = str(inventory.get("parser") or "").strip()
        try:
            table_count = int(metadata.get("kordoc_table_count", inventory.get("table_count", 0)) or 0)
        except (TypeError, ValueError):
            table_count = 0
        if status:
            return status, parser, table_count
    return "missing", "", 0
