"""Deterministic planning layer for regulation-agent workflows.

This first implementation plans and validates role transitions only.  It does
not execute an LLM, approve a document, write an index, or mutate production
data.  Keeping planning separate from execution gives the later Qwen3 8B
integration a typed, auditable boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.agents.base import AgentResult, BaseAgent
from collections.abc import Mapping

from app.agents.executor import (
    ArtifactReader,
    WorkflowExecutionState,
    advance_workflow,
    build_next_task,
    create_workflow_state,
)
from app.agents.contracts import require_opaque_identifier
from app.agents.role_registry import AgentRoleSpec, workflow_roles


PLAN_REPORT_TYPE = "agent_orchestration_plan_v1"
EXECUTION_REPORT_TYPE = "agent_orchestration_execution_v1"
# `advance()` is a separate execution method. Keeping the plan builder
# plan-only prevents a caller from mistaking a report label for execution.
SUPPORTED_MODES = {"plan"}


class RegulationOrchestrator(BaseAgent):
    """Build the next safe role transition for a named workflow."""

    def run(self, payload: dict) -> AgentResult:
        if bool(payload.get("execute", False)):
            return AgentResult(start_orchestration_execution(payload))
        return AgentResult(build_orchestration_plan(payload))

    def advance(
        self,
        state: WorkflowExecutionState,
        *,
        agents: Mapping[str, BaseAgent],
        artifact_reader: ArtifactReader,
        input_artifact_refs: list[str] | None = None,
        input_content_hashes: list[str] | None = None,
    ) -> tuple[WorkflowExecutionState, AgentResult, Any | None]:
        """Run one registered role and return the auditable next handoff.

        This is the execution entrypoint for integrations that own durable
        artifact storage.  The orchestrator itself never receives raw file
        paths or writes production data; handlers receive opaque artifact
        references through ``executor.advance_workflow``.
        """

        state, result, next_task = advance_workflow(
            state,
            agents=agents,
            artifact_reader=artifact_reader,
            input_artifact_refs=input_artifact_refs,
            input_content_hashes=input_content_hashes,
        )
        return state, AgentResult(result.model_dump(mode="json")), next_task


def start_orchestration_execution(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a typed workflow state and issue its first executable task."""

    workflow_id = _required_text(payload, "workflow_id")
    run_id = require_opaque_identifier(_required_text(payload, "run_id"), "run_id")
    tenant_id = _required_text(payload, "tenant_id")
    state = create_workflow_state(
        workflow_id=workflow_id,
        run_id=run_id,
        tenant_id=tenant_id,
        pipeline_id=_optional_text(payload.get("pipeline_id")),
    )
    state, task = build_next_task(
        state,
        input_artifact_refs=list(payload.get("input_artifact_refs") or []),
        input_content_hashes=list(payload.get("input_content_hashes") or []),
    )
    return {
        "report_type": EXECUTION_REPORT_TYPE,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "pipeline_id": state.pipeline_id,
        "tenant_scoped": True,
        "plan_only": False,
        "status": state.status,
        "state": state.model_dump(mode="json"),
        "task": task.model_dump(mode="json"),
        "audit_event": {
            "event_type": "agent_workflow_execution_started",
            "workflow_id": workflow_id,
            "run_id": run_id,
            "pipeline_id": state.pipeline_id,
            "next_role_id": task.agent_id,
            "tenant_scoped": True,
            "side_effects": [],
        },
    }


def build_orchestration_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a validated, side-effect-free next-step plan.

    ``completed_roles`` must be an exact prefix of the configured workflow.
    This prevents a caller from skipping approval, security, or citation
    verification by submitting an arbitrary list of completed steps.
    """

    workflow_id = _required_text(payload, "workflow_id")
    tenant_id = _required_text(payload, "tenant_id")
    run_id = require_opaque_identifier(payload.get("run_id") or workflow_id, "run_id")
    mode = str(payload.get("mode") or "plan").strip().lower()
    if mode not in SUPPORTED_MODES:
        raise ValueError(f"mode must be one of: {', '.join(sorted(SUPPORTED_MODES))}")

    roles = workflow_roles(workflow_id)
    completed_role_ids = _completed_role_ids(payload.get("completed_roles"))
    # The orchestrator is the component producing this plan, so it is not an
    # actionable next role. Treat its coordination turn as already completed
    # when a caller starts a fresh workflow.
    if not completed_role_ids and roles and roles[0].role_id == "orchestrator":
        completed_role_ids = ["orchestrator"]
    expected_prefix = tuple(role.role_id for role in roles[: len(completed_role_ids)])
    if tuple(completed_role_ids) != expected_prefix:
        raise ValueError(
            "completed_roles must exactly match the configured workflow prefix; "
            "roles cannot be skipped or completed out of order."
        )

    requested_execution = bool(payload.get("execute", False))
    next_index = len(completed_role_ids)
    status = "ready"
    blocked_reason: str | None = None
    next_role: AgentRoleSpec | None = None

    if requested_execution:
        raise ValueError("execute=True must be submitted through RegulationOrchestrator.run")
    if next_index >= len(roles):
        status = "completed"
    else:
        next_role = roles[next_index]

    remaining_roles = roles[next_index:] if status != "blocked" else roles[next_index:]
    report: dict[str, Any] = {
        "report_type": PLAN_REPORT_TYPE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workflow_id": workflow_id,
        "run_id": run_id,
        "tenant_scoped": bool(tenant_id),
        "profile_scoped": bool(_optional_text(payload.get("profile_id"))),
        "mode": mode,
        "plan_only": True,
        "status": status,
        "completed_roles": list(completed_role_ids),
        "completed_count": len(completed_role_ids),
        "total_role_count": len(roles),
        "remaining_roles": [role.role_id for role in remaining_roles],
        "blocked_reason": blocked_reason,
        "next_action": _next_action(status=status, next_role=next_role, blocked_reason=blocked_reason),
        "next_role": _serialize_role(next_role) if next_role else None,
        "workflow_roles": [_serialize_role(role) for role in roles],
        "execution_rule": (
            "한 번에 다음 역할 하나만 실행하며, 실패·검토 필요·사람 승인은 자동으로 건너뛰지 않습니다."
        ),
        "audit_event": {
            "event_type": "agent_role_transition_planned",
            "workflow_id": workflow_id,
            "run_id": run_id,
            "tenant_scoped": True,
            "next_role_id": next_role.role_id if next_role else None,
            "completed_role_count": len(completed_role_ids),
            "side_effects": [],
        },
    }
    return report


def _serialize_role(role: AgentRoleSpec | None) -> dict[str, Any] | None:
    if role is None:
        return None
    return {
        "role_id": role.role_id,
        "display_name": role.display_name,
        "kind": role.kind,
        "implementation_status": role.implementation_status,
        "purpose": role.purpose,
        "required_inputs": list(role.required_inputs),
        "outputs": list(role.outputs),
        "can_mutate": list(role.can_mutate),
        "forbidden_actions": list(role.forbidden_actions),
        "failure_policy": role.failure_policy,
        "primary_model": role.primary_model,
        "model_profile": role.model_profile,
    }


def _next_action(*, status: str, next_role: AgentRoleSpec | None, blocked_reason: str | None) -> str:
    if status == "blocked":
        return f"blocked:{blocked_reason}"
    if status == "completed":
        return "workflow_complete"
    if next_role is None:
        return "workflow_invalid"
    return f"prepare_role:{next_role.role_id}"


def _required_text(payload: dict[str, Any], field_name: str) -> str:
    value = _optional_text(payload.get(field_name))
    if not value:
        raise ValueError(f"{field_name} is required")
    return value


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _completed_role_ids(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("completed_roles must be a list of role ids")
    normalized = [_optional_text(item) for item in value]
    if any(item is None for item in normalized):
        raise ValueError("completed_roles cannot contain empty role ids")
    return [str(item) for item in normalized]
