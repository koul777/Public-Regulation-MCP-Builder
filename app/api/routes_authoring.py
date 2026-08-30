from __future__ import annotations

from collections.abc import Callable
from pathlib import PurePath
from typing import Any, TypeVar
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.core.api_audit import audit_api_event
from app.core.config import Settings, get_settings
from app.core.security import (
    API_WRITE_ROLES,
    AuthContext,
    coerce_auth_context,
    get_auth_context,
    require_api_role,
)
from app.core.tenant_access import settings_for_tenant
from app.schemas.authoring import (
    INSTITUTION_PROFILE_ID_MAX_LENGTH,
    INSTITUTION_PROFILE_ID_PATTERN,
    AuthoringExportRequest,
    AuthoringProjectCreateRequest,
    AuthoringProjectFreezeRequest,
    AuthoringProjectUpdateRequest,
    AuthoringTransitionRequest,
)
from app.services.authoring_service import AuthoringConflictError, AuthoringService
from app.services.authoring_template_service import (
    AuthoringTemplateNotFoundError,
    AuthoringTemplateService,
)
from app.storage.authoring_repository import (
    AuthoringRepositoryIntegrityError,
    AuthoringRevisionConflictError,
)


router = APIRouter(prefix="/api/authoring", tags=["authoring"])
_T = TypeVar("_T")


@router.get("/templates")
def list_authoring_templates(
    auth_context: AuthContext = Depends(get_auth_context),
):
    auth = coerce_auth_context(auth_context)
    settings = _request_settings(auth)
    return _run_read(
        settings,
        auth,
        action="authoring.template.list",
        resource_type="authoring_template",
        operation=AuthoringTemplateService().list_templates,
    )


@router.get("/templates/{template_id}")
def get_authoring_template(
    template_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
):
    auth = coerce_auth_context(auth_context)
    settings = _request_settings(auth)
    return _run_read(
        settings,
        auth,
        action="authoring.template.get",
        resource_type="authoring_template",
        source_record_id=template_id,
        operation=lambda: AuthoringTemplateService().get_template(template_id),
    )


@router.post("/projects", status_code=201)
def create_authoring_project(
    request: AuthoringProjectCreateRequest,
    auth_context: AuthContext = Depends(get_auth_context),
):
    auth = coerce_auth_context(auth_context)
    settings = _request_settings(auth)
    return _run_mutation(
        settings,
        auth,
        action="authoring.project.create",
        status_code=201,
        operation=lambda: AuthoringService(settings).create_project(
            request,
            tenant_id=auth.tenant_id,
            actor=auth.actor,
        ),
    )


@router.get("/projects")
def list_authoring_projects(
    profile_id: str = Query(
        min_length=1,
        max_length=INSTITUTION_PROFILE_ID_MAX_LENGTH,
        pattern=INSTITUTION_PROFILE_ID_PATTERN,
    ),
    auth_context: AuthContext = Depends(get_auth_context),
):
    auth = coerce_auth_context(auth_context)
    settings = _request_settings(auth)
    return _run_read(
        settings,
        auth,
        action="authoring.project.list",
        operation=lambda: AuthoringService(settings).list_projects(
            tenant_id=auth.tenant_id,
            profile_id=profile_id,
        ),
    )


@router.get("/projects/{project_id}")
def get_authoring_project(
    project_id: str,
    profile_id: str = Query(
        min_length=1,
        max_length=INSTITUTION_PROFILE_ID_MAX_LENGTH,
        pattern=INSTITUTION_PROFILE_ID_PATTERN,
    ),
    auth_context: AuthContext = Depends(get_auth_context),
):
    auth = coerce_auth_context(auth_context)
    settings = _request_settings(auth)
    return _run_read(
        settings,
        auth,
        action="authoring.project.get",
        source_record_id=project_id,
        operation=lambda: AuthoringService(settings).get_project(
            project_id,
            tenant_id=auth.tenant_id,
            profile_id=profile_id,
        ),
    )


@router.patch("/projects/{project_id}")
@router.put("/projects/{project_id}", include_in_schema=False)
def update_authoring_project(
    project_id: str,
    request: AuthoringProjectUpdateRequest,
    profile_id: str = Query(
        min_length=1,
        max_length=INSTITUTION_PROFILE_ID_MAX_LENGTH,
        pattern=INSTITUTION_PROFILE_ID_PATTERN,
    ),
    auth_context: AuthContext = Depends(get_auth_context),
):
    auth = coerce_auth_context(auth_context)
    settings = _request_settings(auth)
    return _run_mutation(
        settings,
        auth,
        action="authoring.project.update",
        source_record_id=project_id,
        operation=lambda: AuthoringService(settings).update_project(
            project_id,
            request,
            tenant_id=auth.tenant_id,
            profile_id=profile_id,
            actor=auth.actor,
        ),
    )


@router.post("/projects/{project_id}/lint")
def lint_authoring_project(
    project_id: str,
    profile_id: str = Query(
        min_length=1,
        max_length=INSTITUTION_PROFILE_ID_MAX_LENGTH,
        pattern=INSTITUTION_PROFILE_ID_PATTERN,
    ),
    auth_context: AuthContext = Depends(get_auth_context),
):
    auth = coerce_auth_context(auth_context)
    settings = _request_settings(auth)
    return _run_read(
        settings,
        auth,
        action="authoring.project.lint",
        source_record_id=project_id,
        operation=lambda: AuthoringService(settings).lint_project(
            project_id,
            tenant_id=auth.tenant_id,
            profile_id=profile_id,
        ),
    )


@router.get("/projects/{project_id}/events")
def list_authoring_events(
    project_id: str,
    profile_id: str = Query(
        min_length=1,
        max_length=INSTITUTION_PROFILE_ID_MAX_LENGTH,
        pattern=INSTITUTION_PROFILE_ID_PATTERN,
    ),
    auth_context: AuthContext = Depends(get_auth_context),
):
    auth = coerce_auth_context(auth_context)
    settings = _request_settings(auth)
    return _run_read(
        settings,
        auth,
        action="authoring.project.events.list",
        resource_type="authoring_event",
        source_record_id=project_id,
        operation=lambda: AuthoringService(settings).list_events(
            project_id,
            tenant_id=auth.tenant_id,
            profile_id=profile_id,
        ),
    )


@router.post("/projects/{project_id}/start-drafting")
def start_authoring_drafting(
    project_id: str,
    request: AuthoringTransitionRequest,
    profile_id: str = Query(
        min_length=1,
        max_length=INSTITUTION_PROFILE_ID_MAX_LENGTH,
        pattern=INSTITUTION_PROFILE_ID_PATTERN,
    ),
    auth_context: AuthContext = Depends(get_auth_context),
):
    return _transition(
        project_id,
        request,
        profile_id,
        auth_context,
        action="authoring.project.start_drafting",
        method_name="start_drafting",
    )


@router.post("/projects/{project_id}/request-review")
def request_authoring_review(
    project_id: str,
    request: AuthoringTransitionRequest,
    profile_id: str = Query(
        min_length=1,
        max_length=INSTITUTION_PROFILE_ID_MAX_LENGTH,
        pattern=INSTITUTION_PROFILE_ID_PATTERN,
    ),
    auth_context: AuthContext = Depends(get_auth_context),
):
    return _transition(
        project_id,
        request,
        profile_id,
        auth_context,
        action="authoring.project.request_review",
        method_name="request_review",
    )


@router.post("/projects/{project_id}/request-changes")
def request_authoring_changes(
    project_id: str,
    request: AuthoringTransitionRequest,
    profile_id: str = Query(
        min_length=1,
        max_length=INSTITUTION_PROFILE_ID_MAX_LENGTH,
        pattern=INSTITUTION_PROFILE_ID_PATTERN,
    ),
    auth_context: AuthContext = Depends(get_auth_context),
):
    return _transition(
        project_id,
        request,
        profile_id,
        auth_context,
        action="authoring.project.request_changes",
        method_name="request_changes",
    )


@router.post("/projects/{project_id}/freeze")
def freeze_authoring_project(
    project_id: str,
    request: AuthoringProjectFreezeRequest,
    profile_id: str = Query(
        min_length=1,
        max_length=INSTITUTION_PROFILE_ID_MAX_LENGTH,
        pattern=INSTITUTION_PROFILE_ID_PATTERN,
    ),
    auth_context: AuthContext = Depends(get_auth_context),
):
    auth = coerce_auth_context(auth_context)
    settings = _request_settings(auth)
    return _run_mutation(
        settings,
        auth,
        action="authoring.project.freeze",
        source_record_id=project_id,
        operation=lambda: AuthoringService(settings).freeze_project(
            project_id,
            request,
            tenant_id=auth.tenant_id,
            profile_id=profile_id,
            actor=auth.actor,
        ),
    )


@router.post("/projects/{project_id}/reopen")
def reopen_authoring_project(
    project_id: str,
    request: AuthoringTransitionRequest,
    profile_id: str = Query(
        min_length=1,
        max_length=INSTITUTION_PROFILE_ID_MAX_LENGTH,
        pattern=INSTITUTION_PROFILE_ID_PATTERN,
    ),
    auth_context: AuthContext = Depends(get_auth_context),
):
    return _transition(
        project_id,
        request,
        profile_id,
        auth_context,
        action="authoring.project.reopen",
        method_name="reopen_project",
    )


@router.post("/projects/{project_id}/abandon")
def abandon_authoring_project(
    project_id: str,
    request: AuthoringTransitionRequest,
    profile_id: str = Query(
        min_length=1,
        max_length=INSTITUTION_PROFILE_ID_MAX_LENGTH,
        pattern=INSTITUTION_PROFILE_ID_PATTERN,
    ),
    auth_context: AuthContext = Depends(get_auth_context),
):
    return _transition(
        project_id,
        request,
        profile_id,
        auth_context,
        action="authoring.project.abandon",
        method_name="abandon_project",
    )


@router.get("/projects/{project_id}/export")
def get_authoring_export(
    project_id: str,
    profile_id: str = Query(
        min_length=1,
        max_length=INSTITUTION_PROFILE_ID_MAX_LENGTH,
        pattern=INSTITUTION_PROFILE_ID_PATTERN,
    ),
    auth_context: AuthContext = Depends(get_auth_context),
):
    auth = coerce_auth_context(auth_context)
    settings = _request_settings(auth)
    artifact = _run_read(
        settings,
        auth,
        action="authoring.project.export.get",
        source_record_id=project_id,
        operation=lambda: AuthoringService(settings).get_export(
            project_id,
            tenant_id=auth.tenant_id,
            profile_id=profile_id,
        ),
    )
    return _export_response(artifact)


@router.post("/projects/{project_id}/export")
def export_authoring_project(
    project_id: str,
    request: AuthoringExportRequest,
    profile_id: str = Query(
        min_length=1,
        max_length=INSTITUTION_PROFILE_ID_MAX_LENGTH,
        pattern=INSTITUTION_PROFILE_ID_PATTERN,
    ),
    auth_context: AuthContext = Depends(get_auth_context),
):
    auth = coerce_auth_context(auth_context)
    settings = _request_settings(auth)
    artifact = _run_mutation(
        settings,
        auth,
        action="authoring.project.export",
        source_record_id=project_id,
        export_format=request.export_format,
        operation=lambda: AuthoringService(settings).export_project(
            project_id,
            request,
            tenant_id=auth.tenant_id,
            profile_id=profile_id,
            actor=auth.actor,
        ),
    )
    return _export_response(artifact)


def _export_response(artifact: object) -> Response:
    filename = _download_filename(
        getattr(artifact, "filename", "authoring-export.bin")
    )
    content = getattr(artifact, "content", b"")
    return Response(
        content=content,
        media_type=str(getattr(artifact, "media_type", "application/octet-stream")),
        headers={
            "Content-Disposition": _content_disposition(filename),
            "X-Content-SHA256": str(getattr(artifact, "content_sha256", "")),
            "X-Semantic-Content-SHA256": str(
                getattr(artifact, "semantic_content_sha256", "")
            ),
        },
    )


def _transition(
    project_id: str,
    request: AuthoringTransitionRequest,
    profile_id: str,
    auth_context: AuthContext,
    *,
    action: str,
    method_name: str,
):
    auth = coerce_auth_context(auth_context)
    settings = _request_settings(auth)
    return _run_mutation(
        settings,
        auth,
        action=action,
        source_record_id=project_id,
        operation=lambda: getattr(AuthoringService(settings), method_name)(
            project_id,
            request,
            tenant_id=auth.tenant_id,
            profile_id=profile_id,
            actor=auth.actor,
        ),
    )


def _request_settings(auth: AuthContext) -> Settings:
    return settings_for_tenant(get_settings(), auth.tenant_id)


def _require_authoring_access(settings: Settings, auth: AuthContext) -> None:
    if not bool(settings.enable_regulation_authoring):
        raise HTTPException(status_code=404, detail="Regulation authoring is not enabled.")
    require_api_role(auth, API_WRITE_ROLES)


def _run_read(
    settings: Settings,
    auth: AuthContext,
    *,
    action: str,
    operation: Callable[[], _T],
    resource_type: str = "authoring_project",
    source_record_id: str = "",
) -> _T:
    try:
        _require_authoring_access(settings, auth)
        result = operation()
    except Exception as exc:
        _raise_audited_error(
            settings,
            auth,
            exc,
            action=action,
            resource_type=resource_type,
            source_record_id=source_record_id,
        )
    audit_api_event(
        settings,
        auth,
        action=action,
        outcome="success",
        status_code=200,
        resource_type=resource_type,
        source_record_id=_audit_project_id(source_record_id),
        detail="authoring resource accessed",
    )
    return result


def _run_mutation(
    settings: Settings,
    auth: AuthContext,
    *,
    action: str,
    operation: Callable[[], _T],
    status_code: int = 200,
    source_record_id: str = "",
    export_format: str = "",
) -> _T:
    try:
        _require_authoring_access(settings, auth)
        result = operation()
    except Exception as exc:
        _raise_audited_error(
            settings,
            auth,
            exc,
            action=action,
            source_record_id=source_record_id,
            export_format=export_format,
        )
    result_project_id = _audit_project_id(
        getattr(result, "project_id", "") or source_record_id
    )
    revision = getattr(result, "revision", None)
    audit_api_event(
        settings,
        auth,
        action=action,
        outcome="success",
        status_code=status_code,
        resource_type="authoring_project",
        source_record_id=result_project_id,
        export_format=export_format,
        detail=(
            f"authoring mutation committed; revision={revision}"
            if isinstance(revision, int)
            else "authoring mutation committed"
        ),
    )
    return result


def _raise_audited_error(
    settings: Settings,
    auth: AuthContext,
    exc: Exception,
    *,
    action: str,
    resource_type: str = "authoring_project",
    source_record_id: str = "",
    export_format: str = "",
) -> Any:
    status_code, public_detail, audit_detail = _classify_error(exc)
    outcome = "denied" if status_code in {401, 403} else "failure"
    audit_api_event(
        settings,
        auth,
        action=action,
        outcome=outcome,
        status_code=status_code,
        resource_type=resource_type,
        source_record_id=_audit_project_id(source_record_id),
        export_format=export_format,
        detail=audit_detail,
    )
    if isinstance(exc, HTTPException):
        raise exc
    if status_code == 500:
        # Do not let an unexpected exception string echo draft text, secrets,
        # or a local path through a debug response or chained traceback.
        raise HTTPException(status_code=500, detail=public_detail) from None
    raise HTTPException(status_code=status_code, detail=public_detail) from exc


def _classify_error(exc: Exception) -> tuple[int, str, str]:
    if isinstance(exc, HTTPException):
        status_code = int(exc.status_code)
        detail = str(exc.detail)
        category = "authoring access denied" if status_code == 403 else "authoring request rejected"
        return status_code, detail, category
    if isinstance(exc, (AuthoringConflictError, AuthoringRevisionConflictError)):
        return 409, "Authoring project revision is stale.", "authoring revision conflict"
    if isinstance(exc, PermissionError):
        return 403, "A different actor must complete this authoring action.", "authoring separation-of-duties denied"
    if isinstance(exc, (AuthoringTemplateNotFoundError, KeyError)):
        return 404, "Authoring resource not found.", "authoring resource not found"
    if isinstance(exc, (AuthoringRepositoryIntegrityError, OSError, TimeoutError)):
        return 503, "Authoring storage is unavailable.", "authoring storage unavailable"
    if isinstance(exc, ValueError):
        return 422, "Authoring transition or lint validation failed.", "authoring transition or lint validation failed"
    return 500, "Internal authoring error.", "unexpected authoring failure"


def _download_filename(value: object) -> str:
    filename = PurePath(str(value or "authoring-export.bin")).name
    if not filename or any(character in filename for character in "\r\n"):
        return "authoring-export.bin"
    return filename


def _audit_project_id(value: object) -> str:
    """Return only a canonical UUID; never persist arbitrary route text."""

    candidate = str(value or "").strip()
    if not candidate:
        return ""
    try:
        return str(UUID(candidate))
    except (TypeError, ValueError, AttributeError):
        return ""


def _content_disposition(filename: str) -> str:
    fallback = "authoring-export.json" if filename.endswith(".json") else "authoring-export.md"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"
