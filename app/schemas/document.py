from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Document(BaseModel):
    document_id: str
    filename: str
    document_name: str | None = None
    # "user"면 올린 사람이 정한 이름이므로 본문에서 찾은 제목으로 덮어쓰지 않는다.
    document_name_source: Literal["auto", "user"] = "auto"
    file_type: str
    file_hash: str
    institution_name: str | None = None
    apba_id: str | None = None
    source_system: str | None = None
    source_url: str | None = None
    source_record_id: str | None = None
    source_file_id: str | None = None
    source_disclosure_date: str | None = None
    source_posted_date: str | None = None
    profile_id: str | None = None
    regulation_id: str | None = None
    regulation_version: str | None = None
    revision_date: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    repealed_at: str | None = None
    regulation_status: Literal["draft", "pending_approval", "approved", "superseded", "repealed"] = "draft"
    supersedes_document_id: str | None = None
    reprocessing_source_document_id: str | None = None
    reprocessing_reason: str | None = None
    tenant_id: str | None = None
    page_count: int | None = None
    status: Literal["uploaded", "processing", "completed", "failed"] = "uploaded"
    created_at: datetime = Field(default_factory=utc_now)
    processed_at: datetime | None = None
    error: str | None = None


class ProcessingJob(BaseModel):
    job_id: str
    document_id: str
    tenant_id: str | None = None
    status: Literal["queued", "processing", "completed", "failed"] = "queued"
    progress: int = 0
    message: str = "Queued"
    current_unit: int | None = None
    total_units: int | None = None
    unit_label: str | None = None
    pipeline_id: str | None = None
    stage_id: str | None = None
    stage_number: int | None = None
    stage_total: int | None = None
    stage_status: Literal["pending", "running", "completed", "blocked", "failed"] = "pending"
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    error: str | None = None
