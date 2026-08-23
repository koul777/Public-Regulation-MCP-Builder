from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.core.api_audit import audit_api_event
from app.core.config import get_settings
from app.core.security import API_READ_ROLES, AuthContext, coerce_auth_context, get_auth_context, require_api_role
from app.core.tenant_access import settings_for_tenant
from app.agents.orchestrator import RegulationOrchestrator
from app.agents.model_router import model_profile_manifest
from app.agents.role_registry import WORKFLOW_ROLE_SEQUENCES, workflow_roles
from app.pipelines.definitions import pipeline_manifest


router = APIRouter(prefix="/api/pipelines", tags=["pipelines"])


class OrchestrationPlanRequest(BaseModel):
    """Bounded, side-effect-free request for the next role transition."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(min_length=1, max_length=120)
    run_id: str | None = Field(default=None, max_length=160)
    # This endpoint is deliberately plan-only. Execution is owned by the
    # durable executor boundary and must never be implied by a read route.
    mode: Literal["plan"] = "plan"
    completed_roles: list[str] = Field(default_factory=list, max_length=40)
    profile_id: str | None = Field(default=None, max_length=120)


@router.get("/manifest")
def get_pipeline_manifest(auth_context: AuthContext = Depends(get_auth_context)):
    auth = coerce_auth_context(auth_context)
    settings = settings_for_tenant(get_settings(), auth.tenant_id)
    require_api_role(auth, API_READ_ROLES)
    response = {
        "schema_version": "reg-rag-pipeline-manifest-v1",
        "local_only": True,
        "external_api_calls_enabled": False,
        "local_llm_model": str(settings.rag_llm_model or "qwen3:8b"),
        "pipelines": pipeline_manifest(),
        "model_profiles": model_profile_manifest(),
        "agent_workflows": {
            workflow_id: [
                {
                    "role_id": role.role_id,
                    "display_name": role.display_name,
                    "kind": role.kind,
                    "implementation_status": role.implementation_status,
                    "purpose": role.purpose,
                    "required_inputs": list(role.required_inputs),
                    "outputs": list(role.outputs),
                    "can_mutate": list(role.can_mutate),
                    "forbidden_actions": list(role.forbidden_actions),
                    "model_profile": role.model_profile,
                    "primary_model": role.primary_model,
                    "human_decision_required": role.kind == "human_gate",
                    "failure_policy": role.failure_policy,
                }
                for role in workflow_roles(workflow_id)
            ]
            for workflow_id in WORKFLOW_ROLE_SEQUENCES
        },
    }
    audit_api_event(
        settings,
        auth,
        action="pipeline.manifest",
        outcome="success",
        status_code=200,
        resource_type="pipeline",
        detail="Returned path-free pipeline stage manifest.",
    )
    return response


@router.post("/orchestration/plan")
def get_orchestration_plan(
    request: OrchestrationPlanRequest,
    auth_context: AuthContext = Depends(get_auth_context),
):
    """Return the next safe role/model handoff without executing side effects."""

    auth = coerce_auth_context(auth_context)
    settings = settings_for_tenant(get_settings(), auth.tenant_id)
    require_api_role(auth, API_READ_ROLES)
    tenant_id = str(auth.tenant_id or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=403, detail="tenant scope is required")
    try:
        report = RegulationOrchestrator().run(
            {
                **request.model_dump(mode="json"),
                "tenant_id": tenant_id,
            }
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit_api_event(
        settings,
        auth,
        action="pipeline.orchestration_plan",
        outcome="success",
        status_code=200,
        resource_type="pipeline",
        detail="Returned one path-free next agent role and model handoff.",
    )
    return report
