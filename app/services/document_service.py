from __future__ import annotations

import uuid
from pathlib import Path
from typing import BinaryIO, Callable

from app.core.config import Settings, get_settings
from app.schemas.document import Document
from app.services.regulation_metadata_service import infer_regulation_metadata
from app.storage.file_store import FileStore, sha256_bytes
from app.storage.repository import JsonRepository


class DocumentService:
    def __init__(
        self,
        settings: Settings | None = None,
        repository: JsonRepository | None = None,
        file_store: FileStore | None = None,
    ):
        self.settings = settings or get_settings()
        self.repository = repository or JsonRepository(self.settings)
        self.file_store = file_store or FileStore(self.settings)

    def upload(
        self,
        filename: str,
        content: bytes,
        *,
        document_name: str | None = None,
        institution_name: str | None = None,
        apba_id: str | None = None,
        source_system: str | None = None,
        source_url: str | None = None,
        source_record_id: str | None = None,
        source_file_id: str | None = None,
        source_disclosure_date: str | None = None,
        source_posted_date: str | None = None,
        profile_id: str | None = None,
        regulation_id: str | None = None,
        regulation_version: str | None = None,
        revision_date: str | None = None,
        effective_from: str | None = None,
        effective_to: str | None = None,
        repealed_at: str | None = None,
        regulation_status: str = "draft",
        supersedes_document_id: str | None = None,
        tenant_id: str | None = None,
    ) -> Document:
        if regulation_status not in {"draft", "pending_approval"}:
            raise ValueError("New regulation uploads must start as draft or pending_approval.")
        self.file_store.validate_upload(filename, content)
        file_hash = sha256_bytes(content)
        detected = infer_regulation_metadata(
            filename,
            existing_documents=self.repository.list_documents(),
            profile_id=profile_id,
            tenant_id=tenant_id,
        )
        resolved_regulation_id = regulation_id or detected.regulation_id
        resolved_regulation_version = regulation_version or detected.regulation_version
        document_id = f"doc_{uuid.uuid4().hex[:12]}"
        path = self.file_store.save_upload(
            document_id,
            filename,
            content,
            regulation_id=resolved_regulation_id,
            profile_id=profile_id,
            regulation_version=resolved_regulation_version,
        )
        document = Document(
            document_id=document_id,
            filename=Path(filename).name,
            document_name=document_name or detected.document_name,
            file_type=path.suffix.lstrip("."),
            file_hash=file_hash,
            institution_name=institution_name,
            apba_id=apba_id,
            source_system=source_system,
            source_url=source_url,
            source_record_id=source_record_id,
            source_file_id=source_file_id,
            source_disclosure_date=source_disclosure_date,
            source_posted_date=source_posted_date,
            profile_id=profile_id,
            regulation_id=resolved_regulation_id,
            regulation_version=resolved_regulation_version,
            revision_date=revision_date or detected.revision_date,
            effective_from=effective_from or detected.effective_from,
            effective_to=effective_to,
            repealed_at=repealed_at,
            regulation_status=regulation_status,
            supersedes_document_id=supersedes_document_id or detected.supersedes_document_id,
            tenant_id=tenant_id,
            status="uploaded",
        )
        self.repository.upsert_document(document)
        return document

    def upload_stream(
        self,
        filename: str,
        source: BinaryIO,
        *,
        document_name: str | None = None,
        institution_name: str | None = None,
        apba_id: str | None = None,
        source_system: str | None = None,
        source_url: str | None = None,
        source_record_id: str | None = None,
        source_file_id: str | None = None,
        source_disclosure_date: str | None = None,
        source_posted_date: str | None = None,
        profile_id: str | None = None,
        regulation_id: str | None = None,
        regulation_version: str | None = None,
        revision_date: str | None = None,
        effective_from: str | None = None,
        effective_to: str | None = None,
        repealed_at: str | None = None,
        regulation_status: str = "draft",
        supersedes_document_id: str | None = None,
        tenant_id: str | None = None,
        expected_size: int | None = None,
        progress_callback: Callable[[int, int | None], None] | None = None,
    ) -> Document:
        if regulation_status not in {"draft", "pending_approval"}:
            raise ValueError("New regulation uploads must start as draft or pending_approval.")
        detected = infer_regulation_metadata(
            filename,
            existing_documents=self.repository.list_documents(),
            profile_id=profile_id,
            tenant_id=tenant_id,
        )
        resolved_regulation_id = regulation_id or detected.regulation_id
        resolved_regulation_version = regulation_version or detected.regulation_version
        document_id = f"doc_{uuid.uuid4().hex[:12]}"
        path, file_hash, _size = self.file_store.save_upload_stream(
            document_id,
            filename,
            source,
            expected_size=expected_size,
            progress_callback=progress_callback,
            regulation_id=resolved_regulation_id,
            profile_id=profile_id,
            regulation_version=resolved_regulation_version,
        )
        document = Document(
            document_id=document_id,
            filename=Path(filename).name,
            document_name=document_name or detected.document_name,
            file_type=path.suffix.lstrip("."),
            file_hash=file_hash,
            institution_name=institution_name,
            apba_id=apba_id,
            source_system=source_system,
            source_url=source_url,
            source_record_id=source_record_id,
            source_file_id=source_file_id,
            source_disclosure_date=source_disclosure_date,
            source_posted_date=source_posted_date,
            profile_id=profile_id,
            regulation_id=resolved_regulation_id,
            regulation_version=resolved_regulation_version,
            revision_date=revision_date or detected.revision_date,
            effective_from=effective_from or detected.effective_from,
            effective_to=effective_to,
            repealed_at=repealed_at,
            regulation_status=regulation_status,
            supersedes_document_id=supersedes_document_id or detected.supersedes_document_id,
            tenant_id=tenant_id,
            status="uploaded",
        )
        self.repository.upsert_document(document)
        return document

    def get(self, document_id: str) -> Document:
        document = self.repository.get_document(document_id)
        if document is None:
            raise KeyError(f"Document not found: {document_id}")
        return document

    def list(self) -> list[Document]:
        return self.repository.list_documents()

    def path_for(self, document: Document) -> Path:
        return self.file_store.upload_path(
            document.document_id,
            document.filename,
            regulation_id=document.regulation_id,
            profile_id=document.profile_id,
            regulation_version=document.regulation_version,
        )
