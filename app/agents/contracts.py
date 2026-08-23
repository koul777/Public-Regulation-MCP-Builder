from __future__ import annotations

"""Typed, path-free contracts shared by every orchestrated agent task."""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_OPAQUE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")


def require_opaque_identifier(value: object, field_name: str) -> str:
    """Normalize a durable id while refusing paths and delimiter injection."""

    candidate = str(value or "").strip()
    if not _OPAQUE_IDENTIFIER_PATTERN.fullmatch(candidate):
        raise ValueError(
            f"{field_name} must be an opaque identifier using letters, digits, '.', '_' or '-'."
        )
    return candidate


AgentTaskStatus = Literal[
    "pending",
    "running",
    "completed",
    "review_required",
    "human_approved",
    "human_rejected",
    "degraded",
    "blocked",
    "failed",
]


class AgentTaskEnvelope(BaseModel):
    """Immutable task hand-off containing references, never raw source text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str = Field(min_length=1, max_length=120)
    run_id: str = Field(min_length=1, max_length=160)
    pipeline_id: str = Field(min_length=1, max_length=120)
    stage_id: str = Field(min_length=1, max_length=120)
    agent_id: str = Field(min_length=1, max_length=120)
    tenant_scope_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    input_artifact_refs: list[str] = Field(default_factory=list, max_length=100)
    input_content_hashes: list[str] = Field(default_factory=list, max_length=100)
    model_profile: str | None = Field(default=None, max_length=120)
    deadline_ms: int = Field(default=60_000, ge=100, le=3_600_000)
    attempt: int = Field(default=1, ge=1, le=3)
    idempotency_key: str = Field(min_length=8, max_length=200)

    @field_validator("input_artifact_refs")
    @classmethod
    def validate_artifact_refs(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            candidate = str(value or "").strip()
            if not candidate.startswith("artifact:"):
                raise ValueError("artifact references must start with 'artifact:'")
            opaque_id = candidate.removeprefix("artifact:")
            if not opaque_id or len(opaque_id) > 128:
                raise ValueError("artifact references require a bounded opaque id")
            if any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in opaque_id):
                raise ValueError("artifact references must not contain paths or unsafe characters")
            normalized.append(candidate)
        if len(set(normalized)) != len(normalized):
            raise ValueError("artifact references must be unique")
        return normalized

    @field_validator("run_id")
    @classmethod
    def validate_run_identifier(cls, value: str) -> str:
        return require_opaque_identifier(value, "run_id")

    @field_validator("input_content_hashes")
    @classmethod
    def validate_content_hashes(cls, values: list[str]) -> list[str]:
        normalized = [str(value or "").strip().lower() for value in values]
        if any(not _is_sha256(value) for value in normalized):
            raise ValueError("content hashes must use sha256:<64 lowercase hex>")
        if len(set(normalized)) != len(normalized):
            raise ValueError("content hashes must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_reference_binding(self) -> "AgentTaskEnvelope":
        if (self.input_artifact_refs or self.input_content_hashes) and len(self.input_content_hashes) != len(self.input_artifact_refs):
            raise ValueError("input_content_hashes must bind one-to-one with input_artifact_refs")
        return self


class AgentMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_ms: float = Field(default=0.0, ge=0.0)
    input_units: int = Field(default=0, ge=0)
    output_units: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0, le=2)


class AgentResultEnvelope(BaseModel):
    """Validated role result consumed by the deterministic orchestrator."""

    model_config = ConfigDict(extra="forbid")

    role_id: str = Field(min_length=1, max_length=120)
    stage_id: str = Field(min_length=1, max_length=120)
    status: AgentTaskStatus
    output_artifact_refs: list[str] = Field(default_factory=list, max_length=100)
    output_content_hashes: list[str] = Field(default_factory=list, max_length=100)
    evidence_ids: list[str] = Field(default_factory=list, max_length=200)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    review_flags: list[str] = Field(default_factory=list, max_length=100)
    reason_code: str | None = Field(default=None, max_length=120)
    model_profile: str | None = Field(default=None, max_length=120)
    metrics: AgentMetrics = Field(default_factory=AgentMetrics)
    details: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("output_artifact_refs")
    @classmethod
    def validate_output_refs(cls, values: list[str]) -> list[str]:
        return AgentTaskEnvelope.validate_artifact_refs(values)

    @field_validator("output_content_hashes")
    @classmethod
    def validate_output_hashes(cls, values: list[str]) -> list[str]:
        return AgentTaskEnvelope.validate_content_hashes(values)

    @field_validator("evidence_ids", "review_flags")
    @classmethod
    def validate_bounded_codes(cls, values: list[str]) -> list[str]:
        normalized = [str(value or "").strip() for value in values if str(value or "").strip()]
        if any(len(value) > 160 for value in normalized):
            raise ValueError("evidence ids and review flags must be bounded")
        return list(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def validate_result_state(self) -> "AgentResultEnvelope":
        if (self.output_artifact_refs or self.output_content_hashes) and len(self.output_content_hashes) != len(self.output_artifact_refs):
            raise ValueError("output_content_hashes must bind one-to-one with output_artifact_refs")
        if self.status in {"blocked", "failed", "human_rejected"} and not self.reason_code:
            raise ValueError(f"{self.status} results require reason_code")
        if self.status == "review_required" and not self.review_flags:
            raise ValueError("review_required results require review_flags")
        return self


def safe_result_payload(result: AgentResultEnvelope) -> dict[str, Any]:
    """Return a JSON-safe representation suitable for a durable audit trace."""

    return result.model_dump(mode="json")


def _is_sha256(value: str) -> bool:
    if not value.startswith("sha256:") or len(value) != 71:
        return False
    return all(character in "0123456789abcdef" for character in value.removeprefix("sha256:"))
