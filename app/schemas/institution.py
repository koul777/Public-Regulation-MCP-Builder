from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class InstitutionProfileUpsertRequest(BaseModel):
    """Tenant-scoped institution profile registration payload."""

    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    display_name: str = Field(min_length=1, max_length=200)
    institution_name: str = Field(min_length=1, max_length=200)
    apba_id: str | None = Field(default=None, max_length=128)
    source_system: str | None = Field(default=None, max_length=200)
    source_url: str | None = Field(default=None, max_length=2000)
    required_row_fields: list[str] = Field(default_factory=list)
    max_upload_mb: int | None = Field(default=None, gt=0, le=10240)
    notes: str = Field(default="", max_length=2000)
    make_default: bool = False
