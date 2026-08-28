"""Document-scoped irreversible deletion with RAG/MCP cleanup."""

from __future__ import annotations

from app.core.config import Settings
from app.services.institution_purge_service import (
    InstitutionPurgeResult,
    InstitutionPurgeService,
)
from app.storage.file_store import FileStore
from app.storage.repository import JsonRepository


class DocumentPurgeService:
    """Expose one safe purge path for every document-deletion UI."""

    def __init__(
        self,
        settings: Settings | None = None,
        repository: JsonRepository | None = None,
        file_store: FileStore | None = None,
    ) -> None:
        self._delegate = InstitutionPurgeService(
            settings=settings,
            repository=repository,
            file_store=file_store,
        )

    def purge(
        self,
        document_ids: list[str] | tuple[str, ...],
    ) -> InstitutionPurgeResult:
        """Deindex first, then remove document artifacts, exports, and journals."""

        return self._delegate.purge_documents(document_ids)
