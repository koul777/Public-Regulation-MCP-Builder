from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class QualityCheck(BaseModel):
    name: str
    severity: Literal["info", "warning", "error"]
    passed: bool
    value: int | float | str | bool | None = None
    threshold: int | float | str | bool | None = None
    message: str


class QualityReport(BaseModel):
    document_id: str
    generated_at: datetime = Field(default_factory=utc_now)
    passed: bool
    score: float
    node_count: int
    chunk_count: int
    issue_count: int
    error_count: int
    warning_count: int
    validation_error_count: int = 0
    validation_warning_count: int = 0
    failed_error_check_count: int = 0
    failed_warning_check_count: int = 0
    duplicate_chunk_id_count: int
    empty_chunk_count: int
    missing_page_count: int
    missing_required_metadata_count: int
    missing_required_metadata_field_count: int = 0
    node_type_counts: dict[str, int] = Field(default_factory=dict)
    chunk_type_counts: dict[str, int] = Field(default_factory=dict)
    metadata_coverage: dict[str, int] = Field(default_factory=dict)
    table_metrics: dict[str, int | float] = Field(default_factory=dict)
    structure_metrics: dict[str, int | float] = Field(default_factory=dict)
    text_quality_metrics: dict[str, int | float] = Field(default_factory=dict)
    coverage_metrics: dict[str, int | float] = Field(default_factory=dict)
    checks: list[QualityCheck] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
