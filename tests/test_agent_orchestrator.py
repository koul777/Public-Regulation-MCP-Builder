from __future__ import annotations

import unittest

from app.agents.orchestrator import RegulationOrchestrator, build_orchestration_plan
from app.agents.base import AgentResult, BaseAgent
from app.agents.executor import create_workflow_state, stable_artifact_hash


class _SecurityHandler(BaseAgent):
    def run(self, payload: dict) -> AgentResult:
        return AgentResult(
            {
                "role_id": "security_guard",
                "stage_id": "security_gate",
                "status": "completed",
                "output_artifact_refs": ["artifact:security-result"],
                "output_content_hashes": [stable_artifact_hash({"ok": True})],
                "model_profile": None,
            }
        )


class AgentOrchestratorTests(unittest.TestCase):
    def _payload(self, workflow_id: str = "local_regulation_qa", **overrides: object) -> dict:
        payload: dict[str, object] = {
            "workflow_id": workflow_id,
            "run_id": "run-1",
            "tenant_id": "tenant-a",
            "profile_id": "institution-a",
            "mode": "plan",
        }
        payload.update(overrides)
        return payload

    def test_initial_qa_plan_starts_with_security_guard(self) -> None:
        report = build_orchestration_plan(self._payload())

        self.assertEqual(report["report_type"], "agent_orchestration_plan_v1")
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["next_role"]["role_id"], "security_guard")
        self.assertEqual(report["next_action"], "prepare_role:security_guard")
        self.assertEqual(report["audit_event"]["side_effects"], [])
        self.assertTrue(report["tenant_scoped"])
        self.assertTrue(report["profile_scoped"])
        self.assertNotIn("tenant-a", str(report))
        self.assertNotIn("institution-a", str(report))

    def test_qa_plan_places_citation_verifier_after_qwen_answerer(self) -> None:
        completed = [
            "orchestrator",
            "security_guard",
            "query_analyst",
            "query_rewriter",
            "retrieval_guard",
            "reranker",
            "context_builder",
            "grounded_answerer",
            "claim_auditor",
        ]
        report = build_orchestration_plan(self._payload(completed_roles=completed))

        self.assertEqual(report["next_role"]["role_id"], "citation_verifier")
        self.assertEqual(report["next_role"]["primary_model"], None)

    def test_ingestion_cannot_skip_human_approval(self) -> None:
        completed = [
            "orchestrator",
            "security_guard",
            "intake_guard",
            "parser_extractor",
            "ocr_extractor",
            "normalizer",
            "structure_detector",
            "structure_reviewer",
            "table_reviewer",
            "chunk_builder",
            "quality_gate",
        ]
        report = build_orchestration_plan(
            self._payload("ingestion_and_approval", completed_roles=completed)
        )

        self.assertEqual(report["next_role"]["role_id"], "human_approval_gate")

    def test_out_of_order_completed_roles_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "workflow prefix"):
            build_orchestration_plan(
                self._payload(
                    completed_roles=["orchestrator", "security_guard", "grounded_answerer"]
                )
            )

    def test_execution_request_starts_typed_state_machine(self) -> None:
        report = RegulationOrchestrator().run(self._payload(execute=True))

        self.assertEqual("agent_orchestration_execution_v1", report["report_type"])
        self.assertFalse(report["plan_only"])
        self.assertEqual("running", report["status"])
        self.assertEqual("security_guard", report["task"]["agent_id"])
        self.assertNotIn("tenant_id", report["state"])

    def test_plan_builder_rejects_execute_flag_to_avoid_ambiguous_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "RegulationOrchestrator.run"):
            build_orchestration_plan(self._payload(execute=True))

    def test_plan_builder_rejects_advance_mode_as_execution_boundary(self) -> None:
        with self.assertRaisesRegex(ValueError, "mode must be one of"):
            build_orchestration_plan(self._payload(mode="advance"))

    def test_completed_workflow_has_no_next_role(self) -> None:
        completed = [
            "orchestrator",
            "security_guard",
            "query_analyst",
            "query_rewriter",
            "retrieval_guard",
            "reranker",
            "context_builder",
            "grounded_answerer",
            "claim_auditor",
            "citation_verifier",
            "security_guard",
        ]
        report = build_orchestration_plan(self._payload(completed_roles=completed))

        self.assertEqual(report["status"], "completed")
        self.assertIsNone(report["next_role"])
        self.assertEqual(report["next_action"], "workflow_complete")

    def test_base_agent_entrypoint_returns_same_plan(self) -> None:
        report = RegulationOrchestrator().run(self._payload())

        self.assertEqual(report["next_role"]["role_id"], "security_guard")

    def test_runtime_entrypoint_advances_one_role_without_raw_payload_in_state(self) -> None:
        state = create_workflow_state(
            workflow_id="local_regulation_qa",
            run_id="run-runtime",
            tenant_id="tenant-a",
        )
        state, result, next_task = RegulationOrchestrator().advance(
            state,
            agents={"security_guard": _SecurityHandler()},
            artifact_reader=lambda _ref: {"ok": True},
            input_artifact_refs=["artifact:query"],
            input_content_hashes=[stable_artifact_hash({"ok": True})],
        )

        self.assertEqual("completed", result["status"])
        self.assertEqual("query_analyst", next_task.agent_id)
        self.assertNotIn("tenant_id", state.model_dump())

    def test_required_scope_is_not_optional(self) -> None:
        with self.assertRaisesRegex(ValueError, "tenant_id is required"):
            build_orchestration_plan(self._payload(tenant_id=""))

    def test_plan_rejects_path_like_run_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "opaque identifier"):
            build_orchestration_plan(self._payload(run_id="C:/private/source.pdf"))


if __name__ == "__main__":
    unittest.main()
