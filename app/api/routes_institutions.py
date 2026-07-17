from __future__ import annotations

from datetime import date
import re

from fastapi import APIRouter, Depends, HTTPException

from app.core.api_audit import audit_api_event
from app.core.config import get_settings
from app.core.institution_profiles import (
    InstitutionProfileRegistry,
    load_institution_profile_registry,
    save_institution_profile_registry,
    upsert_institution_profile,
)
from app.core.tenant_access import settings_for_tenant
from app.core.security import (
    API_READ_ROLES,
    API_WRITE_ROLES,
    AuthContext,
    coerce_auth_context,
    get_auth_context,
    require_api_role,
)
from app.schemas.institution import InstitutionProfileUpsertRequest
from app.services.regulation_catalog_service import (
    group_documents_by_regulation,
    latest_active_version,
    read_regulation_metadata,
)
from app.storage.repository import JsonRepository


router = APIRouter(prefix="/api/institutions", tags=["institutions"])


@router.post("", status_code=201)
def upsert_institution(
    request: InstitutionProfileUpsertRequest,
    auth_context: AuthContext = Depends(get_auth_context),
):
    settings = get_settings()
    auth = coerce_auth_context(auth_context)
    tenant_settings = settings_for_tenant(settings, auth.tenant_id)
    try:
        require_api_role(auth, API_WRITE_ROLES)
    except HTTPException as exc:
        audit_api_event(
            tenant_settings,
            auth,
            action="institution.profile.upsert",
            outcome="denied",
            status_code=exc.status_code,
            resource_type="institution_profile",
            source_record_id=request.profile_id,
            detail=str(exc.detail),
        )
        raise
    if not settings.institution_profiles_path:
        detail = "INSTITUTION_PROFILES_PATH must be configured to create an institution profile."
        audit_api_event(
            tenant_settings,
            auth,
            action="institution.profile.upsert",
            outcome="failure",
            status_code=503,
            resource_type="institution_profile",
            source_record_id=request.profile_id,
            detail=detail,
        )
        raise HTTPException(status_code=503, detail=detail)
    try:
        try:
            registry = load_institution_profile_registry(settings.institution_profiles_path)
        except FileNotFoundError:
            registry = InstitutionProfileRegistry(profiles={})
        existing = registry.resolve(request.profile_id)
        if existing is not None and existing.tenant_id and existing.tenant_id != auth.tenant_id:
            raise PermissionError("The institution profile is assigned to another tenant.")
        updated_registry = upsert_institution_profile(
            registry,
            request.profile_id,
            display_name=request.display_name,
            institution_name=request.institution_name,
            tenant_id=auth.tenant_id,
            apba_id=request.apba_id,
            source_system=request.source_system,
            source_url=request.source_url,
            required_row_fields=request.required_row_fields,
            max_upload_mb=request.max_upload_mb,
            notes=request.notes,
            make_default=request.make_default or not registry.profiles,
        )
        save_result = save_institution_profile_registry(settings.institution_profiles_path, updated_registry)
        profile = updated_registry.resolve(request.profile_id, strict=True)
        if profile is None:
            raise ValueError("The institution profile was not available after persistence.")
        audit_api_event(
            tenant_settings,
            auth,
            action="institution.profile.upsert",
            outcome="success",
            status_code=201,
            resource_type="institution_profile",
            source_record_id=profile.profile_id,
            detail=f"institution_name={profile.institution_name or profile.display_name}",
        )
        return {
            "profile": profile.summary(),
            "registry": {
                "sha256": save_result["sha256"],
                "profile_count": save_result["profile_count"],
                "backup_created": bool(save_result.get("backup_path")),
            },
        }
    except PermissionError as exc:
        audit_api_event(
            tenant_settings,
            auth,
            action="institution.profile.upsert",
            outcome="denied",
            status_code=403,
            resource_type="institution_profile",
            source_record_id=request.profile_id,
            detail=str(exc),
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except OSError as exc:
        audit_api_event(
            tenant_settings,
            auth,
            action="institution.profile.upsert",
            outcome="failure",
            status_code=503,
            resource_type="institution_profile",
            source_record_id=request.profile_id,
            detail="Institution profile registry is unavailable.",
        )
        raise HTTPException(status_code=503, detail="Institution profile registry is unavailable.") from exc
    except ValueError as exc:
        audit_api_event(
            tenant_settings,
            auth,
            action="institution.profile.upsert",
            outcome="failure",
            status_code=422,
            resource_type="institution_profile",
            source_record_id=request.profile_id,
            detail=str(exc),
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("")
def list_institutions(auth_context: AuthContext = Depends(get_auth_context)):
    settings = get_settings()
    auth = coerce_auth_context(auth_context)
    require_api_role(auth, API_READ_ROLES)
    if not settings.institution_profiles_path:
        return []
    try:
        registry = load_institution_profile_registry(settings.institution_profiles_path)
    except FileNotFoundError:
        return []
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail="Institution profile registry is unavailable.",
        ) from exc
    return [
        profile.summary()
        for profile in sorted(registry.profiles.values(), key=lambda item: item.profile_id)
        if _profile_visible_to_tenant(profile, auth.tenant_id, settings.app_env)
    ]


@router.get("/{profile_id}/regulations")
def list_institution_regulations(
    profile_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
):
    settings = get_settings()
    auth = coerce_auth_context(auth_context)
    require_api_role(auth, API_READ_ROLES)
    if not settings.institution_profiles_path:
        return []
    try:
        registry = load_institution_profile_registry(settings.institution_profiles_path)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Institution profile registry is unavailable.",
        ) from exc
    try:
        profile = registry.resolve(profile_id, strict=True)
    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Institution profile '{profile_id}' is not available.",
        ) from exc
    if profile is None or not _profile_visible_to_tenant(profile, auth.tenant_id, settings.app_env):
        raise HTTPException(status_code=404, detail="Institution profile is not available for the current tenant.")
    tenant_settings = settings_for_tenant(settings, auth.tenant_id)
    repository = JsonRepository(tenant_settings)
    documents = [
        document
        for document in repository.list_documents()
        if str(document.profile_id or "").strip().lower() == profile.profile_id.lower()
        and str(document.tenant_id or "").strip() == str(auth.tenant_id or "").strip()
    ]
    groups = group_documents_by_regulation(documents)
    catalog: list[dict[str, object]] = []
    for group_key, group_documents in sorted(groups.items(), key=lambda item: str(item[0][1] or "")):
        current_document = latest_active_version(group_documents, active_statuses={"approved"})
        current_document_id = str(getattr(current_document, "document_id", "") or "") if current_document else ""
        versions = []
        for document in sorted(group_documents, key=_document_version_sort_key):
            metadata = read_regulation_metadata(document)
            document_id = str(getattr(document, "document_id", "") or "")
            versions.append(
                {
                    "document_id": document_id,
                    "document_name": getattr(document, "document_name", None),
                    "regulation_version": metadata.version,
                    "regulation_status": metadata.status,
                    "effective_from": metadata.effective_from,
                    "effective_to": metadata.effective_to,
                    "repealed_at": metadata.repealed_at,
                    "is_current": bool(document_id and document_id == current_document_id),
                }
            )
        catalog.append(
            {
                "profile_id": group_key[0],
                "regulation_id": group_key[1],
                "current_document_id": current_document_id or None,
                "versions": versions,
            }
        )
    return catalog


def _profile_visible_to_tenant(profile, tenant_id: str, app_env: str) -> bool:
    assigned_tenant = str(profile.tenant_id or "").strip()
    if assigned_tenant:
        return assigned_tenant == str(tenant_id or "").strip()
    return str(app_env or "").strip().lower() in {"local", "dev", "development", "test"}


def _document_version_sort_key(document) -> tuple:
    metadata = read_regulation_metadata(document)
    version = str(metadata.version or "").strip().casefold()
    tokens = tuple(
        (0, int(token)) if token.isdigit() else (1, token)
        for token in re.findall(r"\d+|[a-z]+", version)
    )
    document_id = str(getattr(document, "document_id", "") or "")
    return (metadata.effective_from or date.min, (tokens, version), metadata.effective_to or date.min, document_id)
