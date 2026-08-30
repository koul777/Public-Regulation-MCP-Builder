from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from app.core.config import Settings, get_settings
from app.core.institution_profiles import normalize_profile_id
from app.schemas.authoring import (
    OFFICIAL_BOUNDARY_NOTICE,
    AuthoringEventType,
    AuthoringExportRequest,
    AuthoringLintReport,
    AuthoringMode,
    AuthoringProject,
    AuthoringProjectCreateRequest,
    AuthoringProjectFreezeRequest,
    AuthoringProjectStatus,
    AuthoringProjectSummary,
    AuthoringProjectUpdateRequest,
    AuthoringTransitionRequest,
    BeginnerChecklistItem,
    ClauseDraft,
    FrozenAuthoringArtifact,
)
from app.schemas.authoring_integrity import (
    semantic_content_hash,
    semantic_content_payload,
)
from app.services.authoring_lint_service import AuthoringLintService
from app.services.authoring_safety_service import sanitize_authoring_reason
from app.services.authoring_template_service import AuthoringTemplateService
from app.storage.authoring_repository import (
    AuthoringRepository,
    AuthoringRepositoryIntegrityError,
    AuthoringRevisionConflictError,
)


_LOCAL_ENVIRONMENTS = frozenset({"local", "dev", "development", "test"})
_AUTHOR_EVENT_TYPES = frozenset(
    {
        AuthoringEventType.CREATED.value,
        AuthoringEventType.UPDATED.value,
        AuthoringEventType.REOPENED.value,
        AuthoringEventType.REVIEW_REQUESTED.value,
    }
)


class AuthoringConflictError(RuntimeError):
    """A stable service-level optimistic concurrency error."""

    def __init__(self, *, expected_revision: int, actual_revision: int):
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            "Authoring project revision conflict "
            f"(expected={expected_revision}, actual={actual_revision})."
        )


class AuthoringStateTransitionError(ValueError):
    """Raised when an operation is not valid in the current authoring state."""


class AuthoringSelfFreezeError(PermissionError):
    """Raised when the author attempts to freeze their own protected draft."""


@dataclass(frozen=True)
class AuthoringExportArtifact:
    """A verified draft export without a local path or official approval claim."""

    project_id: UUID
    frozen_revision: int
    export_format: Literal["json", "markdown"]
    filename: str
    media_type: str
    content: bytes
    content_sha256: str
    semantic_content_sha256: str
    boundary_notice: str = OFFICIAL_BOUNDARY_NOTICE


class AuthoringService:
    """Coordinate the isolated regulation-authoring lifecycle.

    This service deliberately depends only on the authoring schema, templates,
    linter, and isolated repository. It has no bridge to official document,
    approval, vector, retrieval, or MCP domains.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        repository: AuthoringRepository | None = None,
        lint_service: AuthoringLintService | None = None,
        template_service: AuthoringTemplateService | None = None,
    ):
        self.settings = settings or get_settings()
        self.repository = repository or AuthoringRepository(self.settings)
        self.lint_service = lint_service or AuthoringLintService()
        self.template_service = template_service or AuthoringTemplateService()

    def create_project(
        self,
        request: AuthoringProjectCreateRequest,
        *,
        tenant_id: str,
        actor: str,
    ) -> AuthoringProject:
        now = _utc_now()
        project_id = uuid4()
        template = self.template_service.get_template(request.template_id)
        project = AuthoringProject(
            project_id=project_id,
            tenant_id=tenant_id,
            profile_id=_required_profile_id(request.profile_id),
            authoring_mode=request.authoring_mode,
            template_id=template.template_id,
            template_version=template.version,
            title=request.title,
            purpose=request.purpose,
            scope=request.scope,
            legal_bases=request.legal_bases,
            responsible_department=request.responsible_department,
            planned_effective_date=request.planned_effective_date,
            revision_reason=request.revision_reason,
            predecessor_reference=request.predecessor_reference,
            clauses=self.template_service.instantiate_clauses(
                template.template_id,
                project_id=project_id,
            ),
            checklist=_default_checklist(),
            status=AuthoringProjectStatus.PLANNING,
            revision=1,
            created_by=actor,
            created_at=now,
            updated_by=actor,
            updated_at=now,
        )
        project = project.model_copy(
            update={"semantic_content_hash": semantic_content_hash(project)}
        )
        return self.repository.create_project(
            project,
            actor=actor,
            event_type=AuthoringEventType.CREATED.value,
            event_metadata={"template_id_value": template.template_id},
        )

    def list_projects(
        self,
        *,
        tenant_id: str,
        profile_id: str,
    ) -> list[AuthoringProjectSummary]:
        normalized_profile_id = _required_profile_id(profile_id)
        projects = self.repository.list_projects(
            tenant_id=tenant_id,
            profile_id=normalized_profile_id,
        )
        return [self._summary(project) for project in projects]

    def get_project(
        self,
        project_id: str | UUID,
        *,
        tenant_id: str,
        profile_id: str,
    ) -> AuthoringProject:
        return self.repository.get_project(
            str(project_id),
            tenant_id=tenant_id,
            profile_id=_required_profile_id(profile_id),
        )

    def list_events(
        self,
        project_id: str | UUID,
        *,
        tenant_id: str,
        profile_id: str,
    ) -> list[dict[str, object]]:
        self.get_project(
            project_id,
            tenant_id=tenant_id,
            profile_id=profile_id,
        )
        return self.repository.list_events(str(project_id), tenant_id=tenant_id)

    def lint_project(
        self,
        project_id: str | UUID,
        *,
        tenant_id: str,
        profile_id: str,
    ) -> AuthoringLintReport:
        return self.lint_service.lint(
            self.get_project(
                project_id,
                tenant_id=tenant_id,
                profile_id=profile_id,
            )
        )

    def update_project(
        self,
        project_id: str | UUID,
        request: AuthoringProjectUpdateRequest,
        *,
        tenant_id: str,
        profile_id: str,
        actor: str,
    ) -> AuthoringProject:
        project = self.get_project(
            project_id,
            tenant_id=tenant_id,
            profile_id=profile_id,
        )
        self._require_revision(project, request.expected_revision)
        self._require_status(
            project,
            {
                AuthoringProjectStatus.PLANNING,
                AuthoringProjectStatus.DRAFTING,
                AuthoringProjectStatus.CHANGES_REQUESTED,
            },
            operation="edit",
        )
        self._require_canonical_clause_template(project)
        update = request.model_dump(exclude_unset=True)
        update.pop("expected_revision", None)
        if "clauses" in update:
            update["clauses"] = _validated_clause_content(
                current=project.clauses,
                requested=request.clauses or [],
            )
        if "checklist" in update:
            update["checklist"] = _validated_checklist_progress(
                current=project.checklist,
                requested=request.checklist or [],
            )
        update.update(
            {
                "revision": project.revision + 1,
                "last_lint_report": None,
                "updated_by": actor,
                "updated_at": _utc_now(),
            }
        )
        candidate = AuthoringProject.model_validate(
            {**project.model_dump(mode="json"), **update}
        )
        candidate = candidate.model_copy(
            update={"semantic_content_hash": semantic_content_hash(candidate)}
        )
        return self._save(
            candidate,
            tenant_id=tenant_id,
            expected_revision=request.expected_revision,
            actor=actor,
            event_type=AuthoringEventType.UPDATED,
            metadata={"from_status": project.status.value, "to_status": candidate.status.value},
        )

    def start_drafting(
        self,
        project_id: str | UUID,
        request: AuthoringTransitionRequest,
        *,
        tenant_id: str,
        profile_id: str,
        actor: str,
    ) -> AuthoringProject:
        project = self.get_project(
            project_id,
            tenant_id=tenant_id,
            profile_id=profile_id,
        )
        self._require_revision(project, request.expected_revision)
        self._require_status(
            project,
            {AuthoringProjectStatus.PLANNING, AuthoringProjectStatus.CHANGES_REQUESTED},
            operation="start drafting",
        )
        self._require_canonical_clause_template(project)
        missing_metadata = _missing_required_metadata(project)
        if missing_metadata:
            raise AuthoringStateTransitionError(
                "Drafting cannot start until required metadata is saved: "
                + ", ".join(missing_metadata)
            )
        return self._transition(
            project,
            to_status=AuthoringProjectStatus.DRAFTING,
            expected_revision=request.expected_revision,
            actor=actor,
            event_type=AuthoringEventType.UPDATED,
            reason=request.comment,
        )

    def request_review(
        self,
        project_id: str | UUID,
        request: AuthoringTransitionRequest,
        *,
        tenant_id: str,
        profile_id: str,
        actor: str,
    ) -> AuthoringProject:
        project = self.get_project(
            project_id,
            tenant_id=tenant_id,
            profile_id=profile_id,
        )
        self._require_revision(project, request.expected_revision)
        self._require_status(
            project,
            {AuthoringProjectStatus.DRAFTING},
            operation="request review",
        )
        self._require_canonical_clause_template(project)
        _require_canonical_checklist(project.checklist)
        now = _utc_now()
        candidate = project.model_copy(
            update={
                "revision": project.revision + 1,
                "status": AuthoringProjectStatus.REVIEW_REQUESTED,
                "review_requested_by": actor,
                "review_requested_at": now,
                "change_request_comment": None,
                "updated_by": actor,
                "updated_at": now,
            }
        )
        lint_report = self.lint_service.lint(candidate)
        if lint_report.blocking_findings:
            codes = sorted({finding.code for finding in lint_report.blocking_findings})
            raise AuthoringStateTransitionError(
                "Review cannot be requested while blocking lint findings remain: "
                + ", ".join(codes)
            )
        candidate = candidate.model_copy(update={"last_lint_report": lint_report})
        return self._save(
            candidate,
            tenant_id=tenant_id,
            expected_revision=request.expected_revision,
            actor=actor,
            event_type=AuthoringEventType.REVIEW_REQUESTED,
            reason=request.comment,
            metadata={
                "from_status": project.status.value,
                "to_status": candidate.status.value,
                "lint_error_count": 0,
                "lint_warning_count": lint_report.summary()["warning"],
            },
        )

    def request_changes(
        self,
        project_id: str | UUID,
        request: AuthoringTransitionRequest,
        *,
        tenant_id: str,
        profile_id: str,
        actor: str,
    ) -> AuthoringProject:
        project = self.get_project(
            project_id,
            tenant_id=tenant_id,
            profile_id=profile_id,
        )
        self._require_revision(project, request.expected_revision)
        self._require_status(
            project,
            {AuthoringProjectStatus.REVIEW_REQUESTED},
            operation="request changes",
        )
        reason = _required_reason(request.comment, operation="request changes")
        protected_comment = sanitize_authoring_reason(reason)
        now = _utc_now()
        candidate = project.model_copy(
            update={
                "revision": project.revision + 1,
                "status": AuthoringProjectStatus.CHANGES_REQUESTED,
                "last_lint_report": None,
                "changes_requested_by": actor,
                "changes_requested_at": now,
                "change_request_comment": protected_comment,
                "updated_by": actor,
                "updated_at": now,
            }
        )
        return self._save(
            candidate,
            tenant_id=tenant_id,
            expected_revision=request.expected_revision,
            actor=actor,
            event_type=AuthoringEventType.CHANGES_REQUESTED,
            reason=reason,
            metadata={"from_status": project.status.value, "to_status": candidate.status.value},
        )

    def freeze_project(
        self,
        project_id: str | UUID,
        request: AuthoringProjectFreezeRequest,
        *,
        tenant_id: str,
        profile_id: str,
        actor: str,
    ) -> FrozenAuthoringArtifact:
        project = self.get_project(
            project_id,
            tenant_id=tenant_id,
            profile_id=profile_id,
        )
        self._require_revision(project, request.expected_revision)
        self._require_status(
            project,
            {AuthoringProjectStatus.REVIEW_REQUESTED},
            operation="freeze content",
        )
        self._require_canonical_clause_template(project)
        _require_canonical_checklist(project.checklist)
        incomplete_checklist = [
            item.item_id for item in project.checklist if not item.completed
        ]
        if incomplete_checklist:
            raise AuthoringStateTransitionError(
                "Every beginner checklist item must be completed before content freeze: "
                + ", ".join(incomplete_checklist)
            )
        lint_report = self.lint_service.lint(
            project.model_copy(update={"revision": project.revision + 1})
        )
        if lint_report.blocking_findings:
            raise AuthoringStateTransitionError(
                "Content cannot be frozen while blocking lint findings remain."
            )

        authoring_actors = self._authoring_actors(project, tenant_id=tenant_id)
        is_self_freeze = actor in authoring_actors
        protected_mode = self._protected_or_authenticated_mode()
        training_only = project.training_only
        reason = request.comment
        if is_self_freeze and protected_mode:
            raise AuthoringSelfFreezeError(
                "An authenticated reviewer who did not author the draft must freeze protected content."
            )
        if is_self_freeze:
            if not request.allow_training_self_freeze:
                raise AuthoringSelfFreezeError(
                    "Local self-freeze requires explicit training-only consent and a reason."
                )
            reason = _required_reason(reason, operation="local training self-freeze")
            training_only = True

        now = _utc_now()
        next_revision = project.revision + 1
        content_hash = semantic_content_hash(project)
        candidate = project.model_copy(
            update={
                "revision": next_revision,
                "status": AuthoringProjectStatus.CONTENT_FROZEN,
                "semantic_content_hash": content_hash,
                "frozen_revision": next_revision,
                "frozen_content_hash": content_hash,
                "training_only": training_only,
                "last_lint_report": lint_report,
                "frozen_by": actor,
                "frozen_at": now,
                "updated_by": actor,
                "updated_at": now,
            }
        )
        saved = self._save(
            candidate,
            tenant_id=tenant_id,
            expected_revision=request.expected_revision,
            actor=actor,
            event_type=AuthoringEventType.CONTENT_FROZEN,
            reason=reason,
            metadata={
                "from_status": project.status.value,
                "to_status": candidate.status.value,
                "training_only": training_only,
                "self_freeze": is_self_freeze,
            },
        )
        return FrozenAuthoringArtifact(
            project_id=saved.project_id,
            tenant_id=saved.tenant_id,
            revision=saved.frozen_revision or saved.revision,
            content_hash=saved.frozen_content_hash or content_hash,
            training_only=saved.training_only,
            frozen_by=actor,
            frozen_at=saved.frozen_at or now,
        )

    def reopen_project(
        self,
        project_id: str | UUID,
        request: AuthoringTransitionRequest,
        *,
        tenant_id: str,
        profile_id: str,
        actor: str,
    ) -> AuthoringProject:
        project = self.get_project(
            project_id,
            tenant_id=tenant_id,
            profile_id=profile_id,
        )
        self._require_revision(project, request.expected_revision)
        self._require_status(
            project,
            {AuthoringProjectStatus.CONTENT_FROZEN, AuthoringProjectStatus.EXPORTED},
            operation="reopen",
        )
        return self._transition(
            project,
            to_status=AuthoringProjectStatus.DRAFTING,
            expected_revision=request.expected_revision,
            actor=actor,
            event_type=AuthoringEventType.REOPENED,
            reason=request.comment,
        )

    def abandon_project(
        self,
        project_id: str | UUID,
        request: AuthoringTransitionRequest,
        *,
        tenant_id: str,
        profile_id: str,
        actor: str,
    ) -> AuthoringProject:
        project = self.get_project(
            project_id,
            tenant_id=tenant_id,
            profile_id=profile_id,
        )
        self._require_revision(project, request.expected_revision)
        self._require_status(
            project,
            {
                AuthoringProjectStatus.PLANNING,
                AuthoringProjectStatus.DRAFTING,
                AuthoringProjectStatus.CHANGES_REQUESTED,
            },
            operation="abandon",
        )
        return self._transition(
            project,
            to_status=AuthoringProjectStatus.ABANDONED,
            expected_revision=request.expected_revision,
            actor=actor,
            event_type=AuthoringEventType.ABANDONED,
            reason=_required_reason(request.comment, operation="abandon"),
        )

    def export_project(
        self,
        project_id: str | UUID,
        request: AuthoringExportRequest,
        *,
        tenant_id: str,
        profile_id: str,
        actor: str,
    ) -> AuthoringExportArtifact:
        project = self.get_project(
            project_id,
            tenant_id=tenant_id,
            profile_id=profile_id,
        )
        self._require_revision(project, request.expected_revision)
        self._require_status(
            project,
            {AuthoringProjectStatus.CONTENT_FROZEN},
            operation="export",
        )
        frozen_revision = project.frozen_revision or 0
        content_hash = project.frozen_content_hash or ""
        frozen_project = self._load_verified_frozen_project(
            project,
            tenant_id=tenant_id,
            profile_id=profile_id,
        )

        if request.export_format == "json":
            content = self._render_json_export(frozen_project)
            suffix = "json"
            media_type = "application/json"
        else:
            content = self._render_markdown_export(frozen_project)
            suffix = "md"
            media_type = "text/markdown; charset=utf-8"
        content_sha256 = hashlib.sha256(content).hexdigest()
        self._verify_export_content(
            content,
            export_format=request.export_format,
            semantic_content_sha256=content_hash,
        )
        filename = f"regulation-draft-{project.project_id}-r{frozen_revision}.{suffix}"
        now = _utc_now()
        exported = project.model_copy(
            update={
                "revision": project.revision + 1,
                "status": AuthoringProjectStatus.EXPORTED,
                "updated_by": actor,
                "updated_at": now,
                "exported_by": actor,
                "exported_at": now,
            }
        )
        try:
            _, stored = self.repository.save_exported_project(
                exported,
                tenant_id=tenant_id,
                expected_revision=request.expected_revision,
                actor=actor,
                frozen_revision=frozen_revision,
                frozen_content_hash=content_hash,
                export_format=request.export_format,
                content=content,
                content_sha256=content_sha256,
                event_metadata={
                    "from_status": project.status.value,
                    "to_status": AuthoringProjectStatus.EXPORTED.value,
                    "export_format": request.export_format,
                    "file_sha256": content_sha256,
                },
            )
        except AuthoringRevisionConflictError as exc:
            raise AuthoringConflictError(
                expected_revision=exc.expected_revision,
                actual_revision=exc.actual_revision,
            ) from exc
        self._verify_export_content(
            stored,
            export_format=request.export_format,
            semantic_content_sha256=content_hash,
        )
        if hashlib.sha256(stored).hexdigest() != content_sha256:
            raise AuthoringRepositoryIntegrityError(
                "Stored authoring export failed its SHA-256 revalidation."
            )
        return AuthoringExportArtifact(
            project_id=project.project_id,
            frozen_revision=frozen_revision,
            export_format=request.export_format,
            filename=filename,
            media_type=media_type,
            content=stored,
            content_sha256=content_sha256,
            semantic_content_sha256=content_hash,
        )

    def get_export(
        self,
        project_id: str | UUID,
        *,
        tenant_id: str,
        profile_id: str,
    ) -> AuthoringExportArtifact:
        """Re-read a verified immutable package without creating a revision."""

        project = self.get_project(
            project_id,
            tenant_id=tenant_id,
            profile_id=profile_id,
        )
        self._require_status(
            project,
            {AuthoringProjectStatus.EXPORTED},
            operation="download export",
        )
        frozen_revision = project.frozen_revision or 0
        content_hash = project.frozen_content_hash or ""
        frozen_project = self._load_verified_frozen_project(
            project,
            tenant_id=tenant_id,
            profile_id=profile_id,
        )
        events = self.repository.list_events(
            str(project.project_id),
            tenant_id=tenant_id,
        )
        export_event = events[-1] if events else {}
        metadata = export_event.get("metadata")
        if (
            export_event.get("event_type") != AuthoringEventType.EXPORTED.value
            or export_event.get("revision") != project.revision
            or export_event.get("content_hash") != content_hash
            or not isinstance(metadata, dict)
        ):
            raise AuthoringRepositoryIntegrityError(
                "The exported project has no matching immutable export event."
            )
        export_format = metadata.get("export_format")
        content_sha256 = metadata.get("file_sha256")
        if export_format not in {"json", "markdown"} or not isinstance(
            content_sha256, str
        ):
            raise AuthoringRepositoryIntegrityError(
                "The immutable export event is incomplete."
            )
        suffix = "json" if export_format == "json" else "md"
        media_type = (
            "application/json"
            if export_format == "json"
            else "text/markdown; charset=utf-8"
        )
        export_path = self._export_path(
            project.project_id,
            frozen_revision=frozen_revision,
            content_hash=content_hash,
            suffix=suffix,
            create_directories=False,
        )
        if not export_path.is_file() or export_path.is_symlink():
            raise AuthoringRepositoryIntegrityError(
                "The committed authoring export is missing."
            )
        try:
            content = export_path.read_bytes()
        except OSError as exc:
            raise AuthoringRepositoryIntegrityError(
                "Unable to read the committed authoring export."
            ) from exc
        self._verify_export_content(
            content,
            export_format=export_format,
            semantic_content_sha256=content_hash,
        )
        if hashlib.sha256(content).hexdigest() != content_sha256:
            raise AuthoringRepositoryIntegrityError(
                "The committed authoring export failed its SHA-256 revalidation."
            )
        expected_content = (
            self._render_json_export(frozen_project)
            if export_format == "json"
            else self._render_markdown_export(frozen_project)
        )
        if content != expected_content:
            raise AuthoringRepositoryIntegrityError(
                "The committed authoring export does not match its frozen snapshot."
            )
        return AuthoringExportArtifact(
            project_id=project.project_id,
            frozen_revision=frozen_revision,
            export_format=export_format,
            filename=(
                f"regulation-draft-{project.project_id}-r{frozen_revision}.{suffix}"
            ),
            media_type=media_type,
            content=content,
            content_sha256=content_sha256,
            semantic_content_sha256=content_hash,
        )

    def _load_verified_frozen_project(
        self,
        current: AuthoringProject,
        *,
        tenant_id: str,
        profile_id: str,
    ) -> AuthoringProject:
        frozen_revision = current.frozen_revision or 0
        content_hash = current.frozen_content_hash or ""
        if (
            current.semantic_content_hash != content_hash
            or semantic_content_hash(current) != content_hash
        ):
            raise AuthoringRepositoryIntegrityError(
                "The current frozen project does not match its declared content hash."
            )
        if not self.repository.verify_frozen_artifact_manifest(
            str(current.project_id),
            tenant_id=tenant_id,
            frozen_revision=frozen_revision,
            content_hash=content_hash,
        ):
            raise AuthoringRepositoryIntegrityError(
                "Only a verified committed frozen authoring snapshot can be used."
            )
        frozen = self.repository.get_project_revision(
            str(current.project_id),
            tenant_id=tenant_id,
            profile_id=profile_id,
            revision=frozen_revision,
        )
        if (
            frozen.status != AuthoringProjectStatus.CONTENT_FROZEN
            or frozen.frozen_revision != frozen_revision
            or frozen.frozen_content_hash != content_hash
            or frozen.semantic_content_hash != content_hash
            or semantic_content_hash(frozen) != content_hash
        ):
            raise AuthoringRepositoryIntegrityError(
                "The committed frozen snapshot failed semantic integrity validation."
            )
        return frozen

    def _transition(
        self,
        project: AuthoringProject,
        *,
        to_status: AuthoringProjectStatus,
        expected_revision: int,
        actor: str,
        event_type: AuthoringEventType,
        reason: str | None = None,
        metadata: dict[str, object] | None = None,
        updates: dict[str, object] | None = None,
    ) -> AuthoringProject:
        now = _utc_now()
        transition_updates: dict[str, object] = {
            "revision": project.revision + 1,
            "status": to_status,
            "updated_by": actor,
            "updated_at": now,
        }
        if to_status == AuthoringProjectStatus.DRAFTING:
            transition_updates["last_lint_report"] = None
        transition_updates.update(updates or {})
        candidate = project.model_copy(
            update=transition_updates
        )
        event_metadata: dict[str, object] = {
            "from_status": project.status.value,
            "to_status": to_status.value,
        }
        event_metadata.update(metadata or {})
        return self._save(
            candidate,
            tenant_id=project.tenant_id,
            expected_revision=expected_revision,
            actor=actor,
            event_type=event_type,
            reason=reason,
            metadata=event_metadata,
        )

    def _save(
        self,
        project: AuthoringProject,
        *,
        tenant_id: str,
        expected_revision: int,
        actor: str,
        event_type: AuthoringEventType,
        reason: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> AuthoringProject:
        sanitized_reason = sanitize_authoring_reason(reason)
        event_metadata = dict(metadata or {})
        if sanitized_reason:
            event_metadata["reason_supplied"] = True
            event_metadata["reason_sha256"] = hashlib.sha256(
                sanitized_reason.encode("utf-8")
            ).hexdigest()
        try:
            return self.repository.save_project(
                project,
                tenant_id=tenant_id,
                expected_revision=expected_revision,
                actor=actor,
                event_type=event_type.value,
                reason=sanitized_reason or None,
                event_metadata=event_metadata,
            )
        except AuthoringRevisionConflictError as exc:
            raise AuthoringConflictError(
                expected_revision=exc.expected_revision,
                actual_revision=exc.actual_revision,
            ) from exc

    def _authoring_actors(
        self,
        project: AuthoringProject,
        *,
        tenant_id: str,
    ) -> set[str]:
        events = self.repository.list_events(str(project.project_id), tenant_id=tenant_id)
        return {
            str(event.get("actor") or "")
            for event in events
            if event.get("event_type") in _AUTHOR_EVENT_TYPES
        }

    def _protected_or_authenticated_mode(self) -> bool:
        protected_environment = self.settings.app_env.strip().lower() not in _LOCAL_ENVIRONMENTS
        return protected_environment or bool(self.settings.api_auth_required)

    def _require_canonical_clause_template(
        self,
        project: AuthoringProject,
    ) -> None:
        template = self.template_service.get_template(project.template_id)
        if template.version != project.template_version:
            raise AuthoringStateTransitionError(
                "The P0 clause template version is not available for safe editing."
            )
        canonical = self.template_service.instantiate_clauses(
            project.template_id,
            project_id=project.project_id,
        )
        if _clause_structure_contract(project.clauses) != _clause_structure_contract(
            canonical
        ):
            raise AuthoringStateTransitionError(
                "The stored P0 clause template structure does not match its server-owned definition."
            )

    def _render_json_export(self, project: AuthoringProject) -> bytes:
        payload = {
            "package_type": "regulation_authoring_draft_v1",
            "boundary_notice": OFFICIAL_BOUNDARY_NOTICE,
            "official_approval": False,
            "semantic_content_sha256": project.frozen_content_hash,
            "project_id": str(project.project_id),
            "frozen_revision": project.frozen_revision,
            "training_only": project.training_only,
            "draft": semantic_content_payload(project),
        }
        return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")

    def _render_markdown_export(self, project: AuthoringProject) -> bytes:
        lines = [
            f"> **{OFFICIAL_BOUNDARY_NOTICE}**",
            "> 이 파일은 작성용 초안이며 법적 검토, 기관 결재, 기존 시스템 승인 또는 공개를 뜻하지 않습니다.",
            "",
            f"# {_markdown_inline(project.title)}",
            "",
            f"- 내용 SHA256: `{project.frozen_content_hash}`",
            f"- 동결 개정 번호: {project.frozen_revision}",
            f"- 연습용 표시: {'예' if project.training_only else '아니오'}",
            f"- 작성 유형: {project.authoring_mode.value}",
            f"- 담당부서: {_markdown_inline(project.responsible_department)}",
            f"- 시행 예정일: {project.planned_effective_date.isoformat() if project.planned_effective_date else ''}",
            "",
            "## 목적",
            "",
            project.purpose,
            "",
            "## 적용 범위",
            "",
            project.scope,
            "",
            "## 근거",
            "",
        ]
        lines.extend(f"- {_markdown_inline(basis)}" for basis in project.legal_bases)
        lines.extend(["", "## 조문"])
        for clause in project.clauses:
            heading = "###" if clause.parent_id is None else "####"
            title = f" {clause.title}" if clause.title else ""
            lines.extend(["", f"{heading} {clause.article_number}{title}", "", clause.body])
        if project.references:
            lines.extend(["", "## 작성자가 기록한 근거 자료", ""])
            for reference in project.references:
                source = (
                    f" ({_markdown_inline(reference.source_title)})"
                    if reference.source_title
                    else ""
                )
                lines.append(f"- {_markdown_inline(reference.citation)}{source}")
        return ("\n".join(lines).rstrip() + "\n").encode("utf-8")

    def _verify_export_content(
        self,
        content: bytes,
        *,
        export_format: Literal["json", "markdown"],
        semantic_content_sha256: str,
    ) -> None:
        if export_format == "json":
            try:
                payload = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AuthoringRepositoryIntegrityError(
                    "Stored authoring JSON export is invalid."
                ) from exc
            valid = (
                isinstance(payload, dict)
                and payload.get("boundary_notice") == OFFICIAL_BOUNDARY_NOTICE
                and payload.get("official_approval") is False
                and payload.get("semantic_content_sha256") == semantic_content_sha256
            )
        else:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AuthoringRepositoryIntegrityError(
                    "Stored authoring Markdown export is not UTF-8."
                ) from exc
            valid = (
                text.startswith(f"> **{OFFICIAL_BOUNDARY_NOTICE}**\n")
                and semantic_content_sha256 in text
                and "기존 시스템 승인 또는 공개를 뜻하지 않습니다" in text
            )
        if not valid:
            raise AuthoringRepositoryIntegrityError(
                "Stored authoring export is missing its boundary notice or content hash."
            )

    def _export_path(
        self,
        project_id: UUID,
        *,
        frozen_revision: int,
        content_hash: str,
        suffix: str,
        create_directories: bool = True,
    ) -> Path:
        authoring_root = Path(self.settings.authoring_dir)
        directory_loader = (
            _confined_export_directory
            if create_directories
            else _confined_existing_export_directory
        )
        root = directory_loader(authoring_root, "exports")
        project_root = directory_loader(root, str(project_id))
        revision_root = directory_loader(
            project_root,
            f"{frozen_revision:020d}",
        )
        path = (revision_root / f"{content_hash}.{suffix}").resolve(strict=False)
        try:
            path.relative_to(root)
        except ValueError as exc:  # pragma: no cover - UUID and hash are schema constrained
            raise AuthoringRepositoryIntegrityError(
                "Authoring export path escaped its isolated root."
            ) from exc
        return path

    @staticmethod
    def _require_revision(project: AuthoringProject, expected_revision: int) -> None:
        if project.revision != expected_revision:
            raise AuthoringConflictError(
                expected_revision=expected_revision,
                actual_revision=project.revision,
            )

    @staticmethod
    def _require_status(
        project: AuthoringProject,
        allowed: set[AuthoringProjectStatus],
        *,
        operation: str,
    ) -> None:
        if project.status not in allowed:
            allowed_values = ", ".join(sorted(status.value for status in allowed))
            raise AuthoringStateTransitionError(
                f"Cannot {operation} while status is {project.status.value}; "
                f"allowed status: {allowed_values}."
            )

    @staticmethod
    def _summary(project: AuthoringProject) -> AuthoringProjectSummary:
        return AuthoringProjectSummary(
            project_id=project.project_id,
            profile_id=project.profile_id,
            title=project.title,
            authoring_mode=project.authoring_mode,
            status=project.status,
            revision=project.revision,
            clause_count=len(project.clauses),
            training_only=project.training_only,
            updated_at=project.updated_at,
        )


def _default_checklist() -> list[BeginnerChecklistItem]:
    return [
        BeginnerChecklistItem(
            item_id="purpose_scope",
            label="목적과 적용 범위를 확인했습니다.",
            guidance="누가 어떤 상황에서 이 규정을 따라야 하는지 다른 사람이 이해할 수 있는지 확인하세요.",
        ),
        BeginnerChecklistItem(
            item_id="legal_basis",
            label="법적·내부 근거를 확인했습니다.",
            guidance="근거의 최신성이나 적용 여부가 불확실하면 담당부서 또는 법무 검토가 필요하다고 표시하세요.",
        ),
        BeginnerChecklistItem(
            item_id="roles_process",
            label="담당자와 절차를 확인했습니다.",
            guidance="누가, 언제, 무엇을 하고 결과를 어떻게 남기는지 빠진 단계가 없는지 확인하세요.",
        ),
        BeginnerChecklistItem(
            item_id="human_review",
            label="사람의 내용 확인이 필요함을 이해했습니다.",
            guidance="내용 동결은 공식 결재나 기존 승인·색인을 대신하지 않습니다.",
        ),
    ]


def _validated_checklist_progress(
    *,
    current: list[BeginnerChecklistItem],
    requested: list[BeginnerChecklistItem],
) -> list[BeginnerChecklistItem]:
    """Keep checklist definitions server-owned while accepting user attestations."""

    _require_canonical_checklist(current)
    _require_canonical_checklist(requested)
    return requested


def _validated_clause_content(
    *,
    current: list[ClauseDraft],
    requested: list[ClauseDraft],
) -> list[ClauseDraft]:
    """Keep the P0 template structure server-owned while accepting content."""

    if _clause_structure_contract(requested) != _clause_structure_contract(current):
        raise AuthoringStateTransitionError(
            "The P0 clause template structure cannot be added, removed, reordered, or changed."
        )
    return requested


def _clause_structure_contract(clauses: list[ClauseDraft]) -> list[tuple[object, ...]]:
    structural_fields = (
        "clause_id",
        "node_type",
        "parent_id",
        "order",
        "article_number",
        "title",
        "beginner_guidance",
        "required",
    )
    return [
        tuple(getattr(clause, field_name) for field_name in structural_fields)
        for clause in clauses
    ]


def _require_canonical_checklist(checklist: list[BeginnerChecklistItem]) -> None:
    canonical_contract = [
        (item.item_id, item.label, item.guidance) for item in _default_checklist()
    ]
    actual_contract = [
        (item.item_id, item.label, item.guidance) for item in checklist
    ]
    if actual_contract != canonical_contract:
        raise AuthoringStateTransitionError(
            "The beginner checklist definition cannot be added, removed, reordered, or changed."
        )


def _missing_required_metadata(project: AuthoringProject) -> list[str]:
    missing: list[str] = []
    for field_name, value in (
        ("title", project.title),
        ("purpose", project.purpose),
        ("scope", project.scope),
        ("responsible_department", project.responsible_department),
    ):
        if not value.strip():
            missing.append(field_name)
    if not project.legal_bases:
        missing.append("legal_bases")
    if project.planned_effective_date is None:
        missing.append("planned_effective_date")
    if project.authoring_mode != AuthoringMode.ENACTMENT:
        if not (project.revision_reason or "").strip():
            missing.append("revision_reason")
        if not (project.predecessor_reference or "").strip():
            missing.append("predecessor_reference")
    return missing


def _required_reason(value: str | None, *, operation: str) -> str:
    reason = (value or "").strip()
    if not reason:
        raise AuthoringStateTransitionError(f"A reason is required to {operation}.")
    return reason


def _required_profile_id(value: str) -> str:
    normalized = normalize_profile_id(value)
    if not normalized:
        # Invalid and mismatched profile scopes share the missing-resource path.
        raise KeyError("authoring project")
    return normalized


def _confined_export_directory(parent: Path, child_name: str) -> Path:
    parent = Path(parent)
    if parent.exists() and parent.is_symlink():
        raise AuthoringRepositoryIntegrityError(
            "Authoring export directories must not be symbolic links."
        )
    parent.mkdir(parents=True, exist_ok=True)
    child = parent / child_name
    if child.exists() and child.is_symlink():
        raise AuthoringRepositoryIntegrityError(
            "Authoring export directories must not be symbolic links."
        )
    child.mkdir(parents=False, exist_ok=True)
    resolved_parent = parent.resolve(strict=True)
    resolved_child = child.resolve(strict=True)
    try:
        resolved_child.relative_to(resolved_parent)
    except ValueError as exc:
        raise AuthoringRepositoryIntegrityError(
            "Authoring export path escaped its isolated root."
        ) from exc
    return resolved_child


def _confined_existing_export_directory(parent: Path, child_name: str) -> Path:
    parent = Path(parent)
    if not parent.is_dir() or parent.is_symlink():
        raise AuthoringRepositoryIntegrityError(
            "Authoring export directories are missing or symbolic links."
        )
    child = parent / child_name
    if not child.is_dir() or child.is_symlink():
        raise AuthoringRepositoryIntegrityError(
            "Authoring export directories are missing or symbolic links."
        )
    resolved_parent = parent.resolve(strict=True)
    resolved_child = child.resolve(strict=True)
    try:
        resolved_child.relative_to(resolved_parent)
    except ValueError as exc:
        raise AuthoringRepositoryIntegrityError(
            "Authoring export path escaped its isolated root."
        ) from exc
    return resolved_child


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _markdown_inline(value: object) -> str:
    """Keep one-line metadata from injecting Markdown block structure."""

    return " ".join(str(value or "").split())
