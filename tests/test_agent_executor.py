from __future__ import annotations

import unittest

from app.agents.base import AgentResult, BaseAgent
from app.agents.contracts import AgentResultEnvelope
from app.agents.executor import (
    apply_role_result,
    advance_workflow,
    build_next_task,
    create_workflow_state,
    execute_task,
    resolve_review,
    stable_artifact_hash,
)
from app.agents.role_registry import workflow_roles


class _ResultAgent(BaseAgent):
    def __init__(self, result: dict) -> None:
        self.result = result

    def run(self, payload: dict) -> AgentResult:
        return AgentResult(self.result)


class AgentExecutorTests(unittest.TestCase):
    def test_qa_state_routes_security_then_1_7b_query_agent(self) -> None:
        state = create_workflow_state(
            workflow_id="local_regulation_qa",
            run_id="run-1",
            tenant_id="tenant-a",
        )
        state, security_task = build_next_task(state)
        self.assertEqual("security_guard", security_task.agent_id)
        self.assertIsNone(security_task.model_profile)

        state = apply_role_result(
            state,
            AgentResultEnvelope(
                role_id="security_guard",
                stage_id="security_gate",
                status="completed",
            ),
        )
        state, query_task = build_next_task(state)

        self.assertEqual("query_analyst", query_task.agent_id)
        self.assertEqual("query-qwen3-1.7b", query_task.model_profile)

    def test_artifact_hash_is_verified_before_agent_execution(self) -> None:
        state = create_workflow_state(
            workflow_id="local_regulation_qa",
            run_id="run-1",
            tenant_id="tenant-a",
        )
        artifact = {"query": "접근권한은 누가 관리하나요?"}
        state, task = build_next_task(
            state,
            input_artifact_refs=["artifact:query-1"],
            input_content_hashes=[stable_artifact_hash(artifact)],
        )
        agent = _ResultAgent(
            {
                "role_id": "security_guard",
                "stage_id": "security_gate",
                "status": "completed",
                "model_profile": None,
            }
        )

        result = execute_task(
            task,
            agents={"security_guard": agent},
            artifact_reader=lambda _ref: artifact,
        )
        mismatch = execute_task(
            task,
            agents={"security_guard": agent},
            artifact_reader=lambda _ref: {"query": "changed"},
        )

        self.assertEqual("completed", result.status)
        self.assertEqual("failed", mismatch.status)
        self.assertEqual("artifact_content_hash_mismatch", mismatch.reason_code)

    def test_artifact_hash_count_mismatch_fails_closed(self) -> None:
        state = create_workflow_state(
            workflow_id="local_regulation_qa",
            run_id="run-1",
            tenant_id="tenant-a",
        )
        with self.assertRaisesRegex(ValueError, "one-to-one"):
            build_next_task(
                state,
                input_artifact_refs=["artifact:one", "artifact:two"],
                input_content_hashes=[stable_artifact_hash({"query": "one"})],
            )

    def test_result_cannot_skip_current_role(self) -> None:
        state = create_workflow_state(
            workflow_id="local_regulation_qa",
            run_id="run-1",
            tenant_id="tenant-a",
        )
        state, _task = build_next_task(state)

        with self.assertRaisesRegex(ValueError, "current task"):
            apply_role_result(
                state,
                AgentResultEnvelope(
                    role_id="query_analyst",
                    stage_id="query_analysis",
                    status="completed",
                    model_profile="query-qwen3-1.7b",
                ),
            )

    def test_blocked_result_is_terminal(self) -> None:
        state = create_workflow_state(
            workflow_id="local_regulation_qa",
            run_id="run-1",
            tenant_id="tenant-a",
        )
        state, _task = build_next_task(state)
        state = apply_role_result(
            state,
            AgentResultEnvelope(
                role_id="security_guard",
                stage_id="security_gate",
                status="blocked",
                reason_code="tenant_scope_denied",
            ),
        )

        self.assertEqual("blocked", state.status)
        self.assertEqual("tenant_scope_denied", state.blocked_reason)
        with self.assertRaisesRegex(ValueError, "must be ready"):
            build_next_task(state)

    def test_review_requires_explicit_decision_artifact(self) -> None:
        state = create_workflow_state(
            workflow_id="ingestion_and_approval",
            run_id="run-1",
            tenant_id="tenant-a",
        )
        state, task = build_next_task(state)
        state = apply_role_result(
            state,
            AgentResultEnvelope(
                role_id=task.agent_id,
                stage_id=task.stage_id,
                status="review_required",
                review_flags=["manual_scope_review"],
                output_artifact_refs=["artifact:reviewed-security-result"],
                output_content_hashes=[stable_artifact_hash({"approved_scope": "tenant-a"})],
            ),
        )

        self.assertEqual(("artifact:reviewed-security-result",), state.pending_review_output_artifact_refs)
        self.assertEqual(1, len(state.pending_review_output_content_hashes))

        with self.assertRaisesRegex(ValueError, "artifact decision"):
            resolve_review(state, approved=True, decision_ref="C:/decision.json")
        resumed = resolve_review(state, approved=True, decision_ref="artifact:decision-1")
        self.assertEqual("ready", resumed.status)
        resumed, next_task = build_next_task(resumed)
        self.assertEqual("intake_guard", next_task.agent_id)
        self.assertEqual(["artifact:reviewed-security-result"], next_task.input_artifact_refs)
        self.assertEqual(
            [stable_artifact_hash({"approved_scope": "tenant-a"})],
            next_task.input_content_hashes,
        )
        self.assertEqual((), resumed.pending_review_output_artifact_refs)

    def test_degraded_role_is_auditable_but_can_advance(self) -> None:
        state = create_workflow_state(
            workflow_id="local_regulation_qa",
            run_id="run-1",
            tenant_id="tenant-a",
        )
        state, task = build_next_task(state)
        state = apply_role_result(
            state,
            AgentResultEnvelope(
                role_id=task.agent_id,
                stage_id=task.stage_id,
                status="degraded",
            ),
        )

        self.assertEqual("ready", state.status)
        self.assertIn("security_guard", state.degraded_roles)

    def test_advance_executes_one_role_and_prepares_the_next_handoff(self) -> None:
        state = create_workflow_state(
            workflow_id="local_regulation_qa",
            run_id="run-advance",
            tenant_id="tenant-a",
        )
        state, result, next_task = advance_workflow(
            state,
            agents={
                "security_guard": _ResultAgent(
                    {
                        "role_id": "security_guard",
                        "stage_id": "security_gate",
                        "status": "completed",
                        "model_profile": None,
                        "output_artifact_refs": ["artifact:security-result"],
                        "output_content_hashes": [stable_artifact_hash({"ok": True})],
                    }
                )
            },
            artifact_reader=lambda _ref: {"ok": True},
            input_artifact_refs=["artifact:query"],
            input_content_hashes=[stable_artifact_hash({"ok": True})],
        )

        self.assertEqual("completed", result.status)
        self.assertEqual("running", state.status)
        self.assertEqual("query_analyst", next_task.agent_id)
        self.assertEqual(["artifact:security-result"], next_task.input_artifact_refs)

    def test_advance_does_not_prepare_a_task_after_human_review_pause(self) -> None:
        state = create_workflow_state(
            workflow_id="ingestion_and_approval",
            run_id="run-review",
            tenant_id="tenant-a",
        )
        state, task = build_next_task(state)
        # Move deterministically to the first role that can request review.
        for role_id in ("security_guard", "intake_guard", "parser_extractor", "ocr_extractor", "normalizer", "structure_detector", "structure_reviewer", "table_reviewer", "chunk_builder", "quality_gate"):
            self.assertEqual(role_id, task.agent_id)
            state = apply_role_result(
                state,
                AgentResultEnvelope(
                    role_id=task.agent_id,
                    stage_id=task.stage_id,
                    status="completed",
                    model_profile=task.model_profile,
                ),
            )
            state, task = build_next_task(state)
        state = apply_role_result(
            state,
            AgentResultEnvelope(
                role_id=task.agent_id,
                stage_id=task.stage_id,
                status="review_required",
                review_flags=["manual_check"],
                model_profile=task.model_profile,
            ),
        )
        self.assertEqual("review_required", state.status)
        with self.assertRaisesRegex(ValueError, "ready without a task"):
            advance_workflow(state, agents={}, artifact_reader=lambda _ref: {})

    def test_all_registered_workflows_advance_every_role_in_order(self) -> None:
        class _PassThroughAgent(BaseAgent):
            def run(self, payload: dict) -> AgentResult:
                task = payload["task"]
                status = "human_approved" if task["agent_id"] == "human_approval_gate" else "completed"
                return AgentResult(
                    {
                        "role_id": task["agent_id"],
                        "stage_id": task["stage_id"],
                        "status": status,
                        "model_profile": task["model_profile"],
                    }
                )

        for workflow_id in (
            "ingestion_and_approval",
            "local_regulation_qa",
            "release_and_mcp_handoff",
        ):
            with self.subTest(workflow_id=workflow_id):
                state = create_workflow_state(
                    workflow_id=workflow_id,
                    run_id=f"run-{workflow_id}",
                    tenant_id="tenant-a",
                )
                agents = {
                    role.role_id: _PassThroughAgent()
                    for role in workflow_roles(workflow_id)
                    if role.role_id != "orchestrator"
                }
                seen_roles: list[str] = []
                for _ in range(40):
                    state, result, _next_task = advance_workflow(
                        state,
                        agents=agents,
                        artifact_reader=lambda _ref: {},
                    )
                    seen_roles.append(result.role_id)
                    if state.status == "completed":
                        break

                expected_roles = [role.role_id for role in workflow_roles(workflow_id)][1:]
                self.assertEqual("completed", state.status)
                self.assertEqual(expected_roles, seen_roles)


if __name__ == "__main__":
    unittest.main()
