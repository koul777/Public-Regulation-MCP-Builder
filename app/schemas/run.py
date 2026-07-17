from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProcessingRun(BaseModel):
    run_id: str
    document_id: str
    job_id: str
    tenant_id: str | None = None
    status: Literal["completed", "failed"]
    started_at: datetime
    completed_at: datetime = Field(default_factory=utc_now)
    elapsed_seconds: float
    options: dict = Field(default_factory=dict)
    stats: dict = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    error: str | None = None
