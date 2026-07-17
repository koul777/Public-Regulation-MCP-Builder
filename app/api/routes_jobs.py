from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.core.config import get_settings
from app.core.security import AuthContext, coerce_auth_context, get_auth_context
from app.core.tenant_access import resource_visible_to_tenant, settings_for_tenant
from app.storage.repository import JsonRepository


router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get(
    "/{job_id}",
    summary="Read a stored processing job",
    description=(
        "Returns a tenant-scoped job record that was already created by a processing request. "
        "Processing is currently synchronous, so this endpoint is a status lookup rather than a queue progress stream."
    ),
)
def get_job(job_id: str, auth_context: AuthContext = Depends(get_auth_context)):
    auth = coerce_auth_context(auth_context)
    job = JsonRepository(settings_for_tenant(get_settings(), auth.tenant_id)).get_job(job_id)
    if job is None or not resource_visible_to_tenant(job, auth.tenant_id):
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return job.model_dump(mode="json")
