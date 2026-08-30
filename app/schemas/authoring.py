from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum
import re
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.institution import (
    INSTITUTION_PROFILE_ID_MAX_LENGTH,
    INSTITUTION_PROFILE_ID_PATTERN,
)


OFFICIAL_BOUNDARY_NOTICE: Literal["공식 승인 아님"] = "공식 승인 아님"
ARTICLE_REFERENCE_RE = re.compile(r"제\s*(\d+)\s*조(?:의\s*(\d+))?")
CONTENT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
LegalBasisText = Annotated[str, Field(min_length=1, max_length=500)]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AuthoringBaseModel(BaseModel):
    """Strict base model for the isolated regulation-authoring domain."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AuthoringMode(StrEnum):
    ENACTMENT = "enactment"
    PARTIAL_REVISION = "partial_revision"
    FULL_REVISION = "full_revision"


class AuthoringProjectStatus(StrEnum):
    PLANNING = "planning"
    DRAFTING = "drafting"
    REVIEW_REQUESTED = "review_requested"
    CHANGES_REQUESTED = "changes_requested"
    CONTENT_FROZEN = "content_frozen"
    EXPORTED = "exported"
    ABANDONED = "abandoned"


class DraftNodeType(StrEnum):
    CHAPTER = "chapter"
    SECTION = "section"
    ARTICLE = "article"
    PARAGRAPH = "paragraph"
    ITEM = "item"
    SUPPLEMENTARY = "supplementary"


class AuthoringEventType(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    REVIEW_REQUESTED = "review_requested"
    CHANGES_REQUESTED = "changes_requested"
    CONTENT_FROZEN = "content_frozen"
    EXPORTED = "exported"
    REOPENED = "reopened"
    ABANDONED = "abandoned"
    TRANSITION_REJECTED = "transition_rejected"


class AuthoringLintSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class BeginnerChecklistItem(AuthoringBaseModel):
    item_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    label: str = Field(min_length=1, max_length=200)
    guidance: str = Field(min_length=1, max_length=500)
    completed: bool = False
    notes: str | None = Field(default=None, max_length=1000)


class LegalReferenceSnapshot(AuthoringBaseModel):
    """A citation entered by the author, not an assertion of legal validity."""

    reference_id: UUID = Field(default_factory=uuid4)
    citation: str = Field(min_length=1, max_length=500)
    source_title: str | None = Field(default=None, max_length=300)
    source_url: str | None = Field(default=None, max_length=1000)
    notes: str | None = Field(default=None, max_length=2000)
    captured_at: datetime = Field(default_factory=utc_now)


class ClauseDraft(AuthoringBaseModel):
    """One ordered chapter, section, article, paragraph, item, or addendum node."""

    clause_id: UUID = Field(default_factory=uuid4)
    node_type: DraftNodeType = DraftNodeType.ARTICLE
    parent_id: UUID | None = None
    order: int = Field(default=1, ge=1, le=10000)
    article_number: str = Field(min_length=1, max_length=80)
    title: str | None = Field(default=None, max_length=300)
    body: str = Field(default="", max_length=20000)
    beginner_guidance: str = Field(default="내용을 구체적으로 작성하세요.", min_length=1, max_length=1000)
    required: bool = True
    reference_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @field_validator("reference_ids")
    @classmethod
    def reference_ids_must_be_unique(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("reference_ids must be unique")
        return value


class AuthoringLintFinding(AuthoringBaseModel):
    code: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_]*$")
    severity: AuthoringLintSeverity
    message: str = Field(min_length=1, max_length=500)
    field_path: str = Field(min_length=1, max_length=300)
    suggestion: str = Field(min_length=1, max_length=500)
    clause_id: UUID | None = None
    article_number: str | None = Field(default=None, max_length=80)


class AuthoringLintReport(AuthoringBaseModel):
    report_type: Literal["authoring_lint_report_v1"] = "authoring_lint_report_v1"
    project_id: UUID
    revision: int = Field(ge=1)
    findings: list[AuthoringLintFinding] = Field(default_factory=list)

    @property
    def blocking_findings(self) -> list[AuthoringLintFinding]:
        return [finding for finding in self.findings if finding.severity == AuthoringLintSeverity.ERROR]

    @property
    def can_request_review(self) -> bool:
        return not self.blocking_findings

    def summary(self) -> dict[str, int]:
        counts = {severity.value: 0 for severity in AuthoringLintSeverity}
        for finding in self.findings:
            counts[finding.severity.value] += 1
        counts["total"] = len(self.findings)
        return counts


class AuthoringProject(AuthoringBaseModel):
    project_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(min_length=1, max_length=128)
    profile_id: str = Field(
        min_length=1,
        max_length=INSTITUTION_PROFILE_ID_MAX_LENGTH,
        pattern=INSTITUTION_PROFILE_ID_PATTERN,
    )
    authoring_mode: AuthoringMode = AuthoringMode.ENACTMENT
    template_id: str = Field(default="general-regulation", min_length=1, max_length=100)
    template_version: str = Field(default="1.0", min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=300)
    purpose: str = Field(default="", max_length=4000)
    scope: str = Field(default="", max_length=4000)
    legal_bases: list[LegalBasisText] = Field(default_factory=list, max_length=100)
    responsible_department: str = Field(default="", max_length=300)
    planned_effective_date: date | None = None
    revision_reason: str | None = Field(default=None, max_length=4000)
    predecessor_reference: str | None = Field(default=None, max_length=500)
    clauses: list[ClauseDraft] = Field(default_factory=list, max_length=2000)
    references: list[LegalReferenceSnapshot] = Field(default_factory=list, max_length=500)
    checklist: list[BeginnerChecklistItem] = Field(default_factory=list, max_length=100)
    status: AuthoringProjectStatus = AuthoringProjectStatus.PLANNING
    revision: int = Field(default=1, ge=1)
    semantic_content_hash: str | None = Field(default=None, min_length=64, max_length=64)
    frozen_revision: int | None = Field(default=None, ge=1)
    frozen_content_hash: str | None = Field(default=None, min_length=64, max_length=64)
    training_only: bool = False
    boundary_notice: Literal["공식 승인 아님"] = OFFICIAL_BOUNDARY_NOTICE
    last_lint_report: AuthoringLintReport | None = None
    created_by: str = Field(min_length=1, max_length=200)
    created_at: datetime = Field(default_factory=utc_now)
    updated_by: str = Field(min_length=1, max_length=200)
    updated_at: datetime = Field(default_factory=utc_now)
    review_requested_by: str | None = Field(default=None, max_length=200)
    review_requested_at: datetime | None = None
    changes_requested_by: str | None = Field(default=None, max_length=200)
    changes_requested_at: datetime | None = None
    change_request_comment: str | None = Field(default=None, max_length=1000)
    frozen_by: str | None = Field(default=None, max_length=200)
    frozen_at: datetime | None = None
    exported_by: str | None = Field(default=None, max_length=200)
    exported_at: datetime | None = None

    @field_validator("legal_bases")
    @classmethod
    def normalize_legal_bases(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        if len(normalized) != len(set(normalized)):
            raise ValueError("legal_bases must be unique")
        return normalized

    @field_validator("semantic_content_hash", "frozen_content_hash")
    @classmethod
    def validate_content_hash(cls, value: str | None) -> str | None:
        if value is not None and not CONTENT_HASH_RE.fullmatch(value):
            raise ValueError("content hash must be a lowercase SHA-256 value")
        return value

    @model_validator(mode="after")
    def validate_identifiers_and_frozen_fields(self) -> AuthoringProject:
        clause_ids = [clause.clause_id for clause in self.clauses]
        if len(clause_ids) != len(set(clause_ids)):
            raise ValueError("clause_id values must be unique")
        reference_ids = [reference.reference_id for reference in self.references]
        if len(reference_ids) != len(set(reference_ids)):
            raise ValueError("reference_id values must be unique")
        checklist_ids = [item.item_id for item in self.checklist]
        if len(checklist_ids) != len(set(checklist_ids)):
            raise ValueError("checklist item_id values must be unique")
        if self.frozen_revision is not None and self.frozen_content_hash is None:
            raise ValueError("frozen_content_hash is required when frozen_revision is set")
        return self


class AuthoringProjectCreateRequest(AuthoringBaseModel):
    profile_id: str = Field(
        min_length=1,
        max_length=INSTITUTION_PROFILE_ID_MAX_LENGTH,
        pattern=INSTITUTION_PROFILE_ID_PATTERN,
    )
    authoring_mode: AuthoringMode = AuthoringMode.ENACTMENT
    template_id: str = Field(default="general-regulation", min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    purpose: str = Field(default="", max_length=4000)
    scope: str = Field(default="", max_length=4000)
    legal_bases: list[LegalBasisText] = Field(default_factory=list, max_length=100)
    responsible_department: str = Field(default="", max_length=300)
    planned_effective_date: date | None = None
    revision_reason: str | None = Field(default=None, max_length=4000)
    predecessor_reference: str | None = Field(default=None, max_length=500)


class AuthoringProjectUpdateRequest(AuthoringBaseModel):
    expected_revision: int = Field(strict=True, ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    purpose: str | None = Field(default=None, max_length=4000)
    scope: str | None = Field(default=None, max_length=4000)
    legal_bases: list[LegalBasisText] | None = Field(default=None, max_length=100)
    responsible_department: str | None = Field(default=None, max_length=300)
    planned_effective_date: date | None = None
    revision_reason: str | None = Field(default=None, max_length=4000)
    predecessor_reference: str | None = Field(default=None, max_length=500)
    clauses: list[ClauseDraft] | None = Field(default=None, max_length=2000)
    references: list[LegalReferenceSnapshot] | None = Field(default=None, max_length=500)
    checklist: list[BeginnerChecklistItem] | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def explicitly_updated_required_fields_must_not_be_null(
        self,
    ) -> AuthoringProjectUpdateRequest:
        non_nullable_updates = {
            "title",
            "purpose",
            "scope",
            "legal_bases",
            "responsible_department",
            "clauses",
            "references",
            "checklist",
        }
        invalid = sorted(
            field_name
            for field_name in self.model_fields_set & non_nullable_updates
            if getattr(self, field_name) is None
        )
        if invalid:
            raise ValueError(
                "Explicit authoring updates must not set required fields to null: "
                + ", ".join(invalid)
            )
        return self


class AuthoringTransitionRequest(AuthoringBaseModel):
    expected_revision: int = Field(strict=True, ge=1)
    comment: str | None = Field(default=None, max_length=2000)


class AuthoringProjectFreezeRequest(AuthoringTransitionRequest):
    """Freeze-only consent kept out of unrelated transition contracts."""

    allow_training_self_freeze: bool = False

    @model_validator(mode="after")
    def training_self_freeze_requires_reason(self) -> AuthoringProjectFreezeRequest:
        if self.allow_training_self_freeze and not (self.comment or "").strip():
            raise ValueError("comment is required when training self-freeze is explicitly allowed")
        return self


class AuthoringExportRequest(AuthoringBaseModel):
    expected_revision: int = Field(strict=True, ge=1)
    export_format: Literal["json", "markdown"]


class AuthoringEvent(AuthoringBaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    tenant_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    event_type: AuthoringEventType
    actor: str = Field(min_length=1, max_length=200)
    occurred_at: datetime = Field(default_factory=utc_now)
    from_status: AuthoringProjectStatus | None = None
    to_status: AuthoringProjectStatus | None = None
    reason: str | None = Field(default=None, max_length=2000)
    content_hash: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator("content_hash")
    @classmethod
    def validate_event_content_hash(cls, value: str | None) -> str | None:
        if value is not None and not CONTENT_HASH_RE.fullmatch(value):
            raise ValueError("content hash must be a lowercase SHA-256 value")
        return value


class FrozenAuthoringArtifact(AuthoringBaseModel):
    artifact_id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    tenant_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    content_hash: str = Field(min_length=64, max_length=64)
    training_only: bool
    boundary_notice: Literal["공식 승인 아님"] = OFFICIAL_BOUNDARY_NOTICE
    frozen_by: str = Field(min_length=1, max_length=200)
    frozen_at: datetime = Field(default_factory=utc_now)

    @field_validator("content_hash")
    @classmethod
    def validate_artifact_content_hash(cls, value: str) -> str:
        if not CONTENT_HASH_RE.fullmatch(value):
            raise ValueError("content hash must be a lowercase SHA-256 value")
        return value


class AuthoringProjectSummary(AuthoringBaseModel):
    project_id: UUID
    profile_id: str = Field(
        min_length=1,
        max_length=INSTITUTION_PROFILE_ID_MAX_LENGTH,
        pattern=INSTITUTION_PROFILE_ID_PATTERN,
    )
    title: str
    authoring_mode: AuthoringMode
    status: AuthoringProjectStatus
    revision: int = Field(ge=1)
    clause_count: int = Field(ge=0)
    training_only: bool
    boundary_notice: Literal["공식 승인 아님"] = OFFICIAL_BOUNDARY_NOTICE
    updated_at: datetime


class AuthoringTemplateNode(AuthoringBaseModel):
    node_key: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    node_type: DraftNodeType
    parent_key: str | None = Field(default=None, max_length=100)
    order: int = Field(ge=1, le=10000)
    article_number: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=300)
    beginner_guidance: str = Field(min_length=1, max_length=1000)
    required: bool = True


class AuthoringTemplate(AuthoringBaseModel):
    template_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    version: str = Field(min_length=1, max_length=40)
    name_ko: str = Field(min_length=1, max_length=200)
    description_ko: str = Field(min_length=1, max_length=1000)
    recommended_for_ko: str = Field(min_length=1, max_length=500)
    first_action_ko: str = Field(min_length=1, max_length=500)
    boundary_notice: Literal["공식 승인 아님"] = OFFICIAL_BOUNDARY_NOTICE
    nodes: list[AuthoringTemplateNode] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_node_keys(self) -> AuthoringTemplate:
        node_keys = [node.node_key for node in self.nodes]
        if len(node_keys) != len(set(node_keys)):
            raise ValueError("template node_key values must be unique")
        known = set(node_keys)
        missing_parents = sorted(
            {node.parent_key for node in self.nodes if node.parent_key is not None and node.parent_key not in known}
        )
        if missing_parents:
            raise ValueError(f"unknown template parent_key values: {', '.join(missing_parents)}")
        return self
