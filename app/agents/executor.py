from __future__ import annotations

"""Fail-closed execution state machine for the regulation agent workflows."""

from collections.abc import Callable, Mapping
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents.base import BaseAgent
from app.agents.contracts import AgentResultEnvelope, AgentTaskEnvelope, require_opaque_identifier
from app.agents.model_router import model_profile_for_role
from app.agents.role_registry import get_agent_role, workflow_roles


WorkflowStatus = Literal["ready", "running", "review_required", "completed", "blocked", "failed"]

WORKFLOW_PIPELINES: dict[str, str] = {
    "ingestion_and_approval": "regulation_preprocessing_v1",
    "local_regulation_qa": "local_regulation_qa_v1",
    "release_and_mcp_handoff": "release_and_mcp_handoff_v1",
}

ROLE_STAGE_IDS: dict[str, str] = {
    "orchestrator": "orchestration",
    "security_guard": "security_gate",
    "intake_guard": "upload_admission",
    "parser_extractor": "parse_extract",
    "ocr_extractor": "parse_extract",
    "normalizer": "normalize",
    "structure_detector": "structure_detect",
    "structure_reviewer": "structure_detect",
    "table_reviewer": "structure_detect",
    "chunk_builder": "chunk_generate",
    "quality_gate": "quality_gate",
    "human_approval_gate": "human_approval",
    "exporter": "export",
    "semantic_embedder": "vector_index",
    "index_builder": "vector_index",
    "query_analyst": "query_analysis",
    "query_rewriter": "query_correction",
    "retrieval_guard": "hybrid_retrieval",
    "reranker": "rerank_filter",
    "context_builder": "context_build",
    "grounded_answerer": "local_llm_answer",
    "claim_auditor": "citation_verify",
    "citation_verifier": "citation_verify",
    "evaluation_agent": "evaluation",
    "release_operator": "release",
}


class WorkflowEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    role_id: str
    stage_id: str
    status: str
    reason_code: str | None = None
    model_profile: str | None = None
    output_content_hashes: list[str] = Field(default_factory=list)


class WorkflowExecutionState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str
    run_id: str
    pipeline_id: str
    tenant_scope_hash: str
    role_sequence: tuple[str, ...]
    next_role_index: int = Field(ge=0)
    status: WorkflowStatus = "ready"
    completed_roles: tuple[str, ...] = ()
    degraded_roles: tuple[str, ...] = ()
    pending_review_role: str | None = None
    pending_review_output_artifact_refs: tuple[str, ...] = ()
    pending_review_output_content_hashes: tuple[str, ...] = ()
    blocked_reason: str | None = None
    current_task: AgentTaskEnvelope | None = None
    events: tuple[WorkflowEvent, ...] = ()


ArtifactReader = Callable[[str], Any]


def create_workflow_state(
    *,
    workflow_id: str,
    run_id: str,
    tenant_id: str,
    pipeline_id: str | None = None,
) -> WorkflowExecutionState:
    normalized_workflow = str(workflow_id or "").strip()
    normalized_run = require_opaque_identifier(run_id, "run_id")
    normalized_tenant = str(tenant_id or "").strip()
    if not normalized_tenant:
        raise ValueError("tenant_id is required")
    roles = tuple(role.role_id for role in workflow_roles(normalized_workflow))
    expected_pipeline = WORKFLOW_PIPELINES.get(normalized_workflow)
    selected_pipeline = str(pipeline_id or expected_pipeline or "").strip()
    if not expected_pipeline or selected_pipeline != expected_pipeline:
        raise ValueError(f"workflow {normalized_workflow!r} requires pipeline_id {expected_pipeline!r}")
    completed_roles: tuple[str, ...] = ()
    next_index = 0
    if roles and roles[0] == "orchestrator":
        completed_roles = ("orchestrator",)
        next_index = 1
    return WorkflowExecutionState(
        workflow_id=normalized_workflow,
        run_id=normalized_run,
        pipeline_id=selected_pipeline,
        tenant_scope_hash=_scope_hash(normalized_tenant),
        role_sequence=roles,
        next_role_index=next_index,
        completed_roles=completed_roles,
    )


def build_next_task(
    state: WorkflowExecutionState,
    *,
    input_artifact_refs: list[str] | None = None,
    input_content_hashes: list[str] | None = None,
    deadline_ms: int | None = None,
    attempt: int = 1,
) -> tuple[WorkflowExecutionState, AgentTaskEnvelope]:
    if state.status != "ready":
        raise ValueError(f"workflow must be ready before building a task; current status={state.status}")
    if state.current_task is not None:
        raise ValueError("workflow already has a current task")
    if state.next_role_index >= len(state.role_sequence):
        raise ValueError("workflow has no remaining role")
    role_id = state.role_sequence[state.next_role_index]
    role = get_agent_role(role_id)
    model_profile = model_profile_for_role(role_id)
    if role.model_profile != (model_profile.profile_id if model_profile else None):
        raise ValueError(f"role {role_id} model routing is inconsistent")
    # A review pause must not discard the reviewed role's output hand-off.
    # Callers may explicitly provide new inputs when resuming, but the normal
    # resume path consumes the opaque refs and hashes captured in the state.
    task_input_refs = (
        list(input_artifact_refs)
        if input_artifact_refs is not None
        else list(state.pending_review_output_artifact_refs)
    )
    task_input_hashes = (
        list(input_content_hashes)
        if input_content_hashes is not None
        else list(state.pending_review_output_content_hashes)
    )
    task = AgentTaskEnvelope(
        workflow_id=state.workflow_id,
        run_id=state.run_id,
        pipeline_id=state.pipeline_id,
        stage_id=ROLE_STAGE_IDS.get(role_id, role_id),
        agent_id=role_id,
        tenant_scope_hash=state.tenant_scope_hash,
        input_artifact_refs=task_input_refs,
        input_content_hashes=task_input_hashes,
        model_profile=model_profile.profile_id if model_profile else None,
        deadline_ms=deadline_ms or (model_profile.timeout_seconds * 1000 if model_profile else 60_000),
        attempt=attempt,
        idempotency_key=f"{state.run_id}:{state.next_role_index}:{role_id}",
    )
    return state.model_copy(
        update={
            "status": "running",
            "current_task": task,
            "pending_review_output_artifact_refs": (),
            "pending_review_output_content_hashes": (),
        }
    ), task


def execute_task(
    task: AgentTaskEnvelope,
    *,
    agents: Mapping[str, BaseAgent],
    artifact_reader: ArtifactReader,
) -> AgentResultEnvelope:
    """Verify all referenced artifacts, then invoke one registered role agent."""

    role = get_agent_role(task.agent_id)
    routed_profile = model_profile_for_role(task.agent_id)
    expected_profile = routed_profile.profile_id if routed_profile else None
    if task.model_profile != expected_profile or role.model_profile != expected_profile:
        raise ValueError(f"task model profile does not match role policy: {task.agent_id}")
    agent = agents.get(task.agent_id)
    if agent is None:
        return _failed_result(task, "agent_handler_not_registered")

    # A supplied hash list is an integrity contract, not a best-effort hint.
    # Treat malformed cardinality as a structured failure before reading or
    # invoking anything so a caller cannot accidentally run an unverified
    # artifact because the list was truncated.
    if task.input_content_hashes and len(task.input_content_hashes) != len(task.input_artifact_refs):
        return _failed_result(task, "artifact_content_hash_count_mismatch")

    artifacts: list[Any] = []
    for index, artifact_ref in enumerate(task.input_artifact_refs):
        try:
            artifact = artifact_reader(artifact_ref)
        except Exception:
            return _failed_result(task, "artifact_read_failed")
        if task.input_content_hashes:
            actual_hash = stable_artifact_hash(artifact)
            if actual_hash != task.input_content_hashes[index]:
                return _failed_result(task, "artifact_content_hash_mismatch")
        artifacts.append(artifact)

    try:
        raw_result = agent.run(
            {
                "task": task.model_dump(mode="json"),
                "artifacts": artifacts,
            }
        )
        result = AgentResultEnvelope.model_validate(raw_result)
    except Exception as exc:
        return _failed_result(task, f"agent_execution_{type(exc).__name__}")
    if result.role_id != task.agent_id or result.stage_id != task.stage_id:
        return _failed_result(task, "agent_result_identity_mismatch")
    if result.model_profile != task.model_profile:
        return _failed_result(task, "agent_result_model_profile_mismatch")
    return result


def advance_workflow(
    state: WorkflowExecutionState,
    *,
    agents: Mapping[str, BaseAgent],
    artifact_reader: ArtifactReader,
    input_artifact_refs: list[str] | None = None,
    input_content_hashes: list[str] | None = None,
) -> tuple[WorkflowExecutionState, AgentResultEnvelope, AgentTaskEnvelope | None]:
    """Execute exactly one role and prepare the next role when safe.

    The function is deliberately one-step-at-a-time: a model cannot silently
    jump over a security gate, an approval gate, or a failed role.  Callers
    can persist the returned state after every invocation and resume it later.
    Output artifact references become the next task's inputs; raw output is
    never copied into the orchestration state.
    """

    if state.status == "ready" and state.current_task is None:
        state, task = build_next_task(
            state,
            input_artifact_refs=input_artifact_refs,
            input_content_hashes=input_content_hashes,
        )
    elif state.status == "running" and state.current_task is not None:
        task = state.current_task
    else:
        raise ValueError(
            "workflow must be ready without a task or running with a current task"
        )

    result = execute_task(task, agents=agents, artifact_reader=artifact_reader)
    next_state = apply_role_result(state, result)
    next_task: AgentTaskEnvelope | None = None
    if next_state.status == "ready" and next_state.next_role_index < len(next_state.role_sequence):
        next_state, next_task = build_next_task(
            next_state,
            input_artifact_refs=list(result.output_artifact_refs),
            input_content_hashes=list(result.output_content_hashes),
        )
    return next_state, result, next_task


def apply_role_result(
    state: WorkflowExecutionState,
    result: AgentResultEnvelope,
) -> WorkflowExecutionState:
    task = state.current_task
    if state.status != "running" or task is None:
        raise ValueError("workflow has no running task")
    if result.role_id != task.agent_id or result.stage_id != task.stage_id:
        raise ValueError("role result does not match the current task")
    event = WorkflowEvent(
        sequence=len(state.events) + 1,
        role_id=result.role_id,
        stage_id=result.stage_id,
        status=result.status,
        reason_code=result.reason_code,
        model_profile=result.model_profile,
        output_content_hashes=list(result.output_content_hashes),
    )
    events = (*state.events, event)
    if result.status in {"blocked", "human_rejected"}:
        return state.model_copy(
            update={
                "status": "blocked",
                "blocked_reason": result.reason_code,
                "current_task": None,
                "pending_review_output_artifact_refs": (),
                "pending_review_output_content_hashes": (),
                "events": events,
            }
        )
    if result.status == "failed":
        return state.model_copy(
            update={
                "status": "failed",
                "blocked_reason": result.reason_code,
                "current_task": None,
                "pending_review_output_artifact_refs": (),
                "pending_review_output_content_hashes": (),
                "events": events,
            }
        )
    if result.status == "review_required":
        return state.model_copy(
            update={
                "status": "review_required",
                "next_role_index": state.next_role_index + 1,
                "pending_review_role": result.role_id,
                "pending_review_output_artifact_refs": tuple(result.output_artifact_refs),
                "pending_review_output_content_hashes": tuple(result.output_content_hashes),
                "current_task": None,
                "events": events,
            }
        )
    if result.status not in {"completed", "degraded", "human_approved"}:
        raise ValueError(f"unsupported terminal role result status: {result.status}")
    next_index = state.next_role_index + 1
    completed_roles = (*state.completed_roles, result.role_id)
    degraded_roles = state.degraded_roles
    if result.status == "degraded":
        degraded_roles = (*degraded_roles, result.role_id)
    next_status: WorkflowStatus = "completed" if next_index >= len(state.role_sequence) else "ready"
    return state.model_copy(
        update={
            "status": next_status,
            "next_role_index": next_index,
            "completed_roles": completed_roles,
            "degraded_roles": degraded_roles,
            "pending_review_role": None,
            "pending_review_output_artifact_refs": (),
            "pending_review_output_content_hashes": (),
            "blocked_reason": None,
            "current_task": None,
            "events": events,
        }
    )


def resolve_review(
    state: WorkflowExecutionState,
    *,
    approved: bool,
    decision_ref: str,
    reason_code: str | None = None,
) -> WorkflowExecutionState:
    if state.status != "review_required" or not state.pending_review_role:
        raise ValueError("workflow is not waiting for review")
    if not str(decision_ref or "").startswith("artifact:"):
        raise ValueError("review resolution requires an artifact decision reference")
    if not approved:
        return state.model_copy(
            update={
                "status": "blocked",
                "blocked_reason": str(reason_code or "human_review_rejected")[:120],
                "pending_review_output_artifact_refs": (),
                "pending_review_output_content_hashes": (),
            }
        )
    return state.model_copy(
        update={
            "status": "ready",
            "completed_roles": (*state.completed_roles, state.pending_review_role),
            "pending_review_role": None,
            "blocked_reason": None,
        }
    )


def stable_artifact_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _failed_result(task: AgentTaskEnvelope, reason_code: str) -> AgentResultEnvelope:
    return AgentResultEnvelope(
        role_id=task.agent_id,
        stage_id=task.stage_id,
        status="failed",
        reason_code=str(reason_code)[:120],
        model_profile=task.model_profile,
    )


def _scope_hash(tenant_id: str) -> str:
    return "sha256:" + hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
