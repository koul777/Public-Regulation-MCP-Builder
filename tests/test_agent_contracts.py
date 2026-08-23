from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.agents.contracts import AgentResultEnvelope, AgentTaskEnvelope


HASH_A = "sha256:" + ("a" * 64)


class AgentContractsTests(unittest.TestCase):
    def test_task_envelope_binds_opaque_artifact_to_hash(self) -> None:
        task = AgentTaskEnvelope(
            workflow_id="wf-1",
            run_id="run-1",
            pipeline_id="local_regulation_qa_v1",
            stage_id="query_analysis",
            agent_id="query_analyst",
            tenant_scope_hash=HASH_A,
            input_artifact_refs=["artifact:query-1"],
            input_content_hashes=[HASH_A],
            model_profile="query-qwen3-1.7b",
            idempotency_key="query-analysis:run-1",
        )

        self.assertEqual("artifact:query-1", task.input_artifact_refs[0])
        self.assertTrue(task.model_config.get("frozen"))

    def test_task_envelope_rejects_local_path_reference(self) -> None:
        with self.assertRaises(ValidationError):
            AgentTaskEnvelope(
                workflow_id="wf-1",
                run_id="run-1",
                pipeline_id="p",
                stage_id="s",
                agent_id="a",
                tenant_scope_hash=HASH_A,
                input_artifact_refs=["artifact:C:/private/source.pdf"],
                idempotency_key="12345678",
            )

    def test_task_envelope_rejects_local_path_run_id(self) -> None:
        with self.assertRaisesRegex(ValidationError, "opaque identifier"):
            AgentTaskEnvelope(
                workflow_id="wf-1",
                run_id="C:/private/source.pdf",
                pipeline_id="p",
                stage_id="s",
                agent_id="a",
                tenant_scope_hash=HASH_A,
                idempotency_key="12345678",
            )

    def test_task_envelope_rejects_artifact_without_content_hash(self) -> None:
        with self.assertRaisesRegex(ValidationError, "one-to-one"):
            AgentTaskEnvelope(
                workflow_id="wf-1",
                run_id="run-1",
                pipeline_id="p",
                stage_id="s",
                agent_id="a",
                tenant_scope_hash=HASH_A,
                input_artifact_refs=["artifact:a"],
                idempotency_key="12345678",
            )

    def test_task_envelope_rejects_content_hash_without_artifact(self) -> None:
        with self.assertRaisesRegex(ValidationError, "one-to-one"):
            AgentTaskEnvelope(
                workflow_id="wf-1",
                run_id="run-1",
                pipeline_id="p",
                stage_id="s",
                agent_id="a",
                tenant_scope_hash=HASH_A,
                input_content_hashes=[HASH_A],
                idempotency_key="12345678",
            )

    def test_task_envelope_rejects_unbound_hash_count(self) -> None:
        with self.assertRaisesRegex(ValidationError, "one-to-one"):
            AgentTaskEnvelope(
                workflow_id="wf-1",
                run_id="run-1",
                pipeline_id="p",
                stage_id="s",
                agent_id="a",
                tenant_scope_hash=HASH_A,
                input_artifact_refs=["artifact:a", "artifact:b"],
                input_content_hashes=[HASH_A],
                idempotency_key="12345678",
            )

    def test_blocked_result_requires_reason_code(self) -> None:
        with self.assertRaisesRegex(ValidationError, "require reason_code"):
            AgentResultEnvelope(role_id="security_guard", stage_id="query_analysis", status="blocked")

    def test_review_result_requires_review_flag(self) -> None:
        with self.assertRaisesRegex(ValidationError, "require review_flags"):
            AgentResultEnvelope(role_id="quality_gate", stage_id="quality_gate", status="review_required")

    def test_completed_result_keeps_only_safe_details(self) -> None:
        result = AgentResultEnvelope(
            role_id="query_analyst",
            stage_id="query_analysis",
            status="completed",
            output_artifact_refs=["artifact:query-plan-1"],
            output_content_hashes=[HASH_A],
            confidence=0.99,
            details={"intent_count": 1, "model_ready": True},
        )

        self.assertEqual("completed", result.status)
        self.assertEqual({"intent_count": 1, "model_ready": True}, result.details)

    def test_result_rejects_artifact_without_content_hash(self) -> None:
        with self.assertRaisesRegex(ValidationError, "one-to-one"):
            AgentResultEnvelope(
                role_id="query_analyst",
                stage_id="query_analysis",
                status="completed",
                output_artifact_refs=["artifact:query-plan-1"],
            )

    def test_result_rejects_content_hash_without_artifact(self) -> None:
        with self.assertRaisesRegex(ValidationError, "one-to-one"):
            AgentResultEnvelope(
                role_id="query_analyst",
                stage_id="query_analysis",
                status="completed",
                output_content_hashes=[HASH_A],
            )


if __name__ == "__main__":
    unittest.main()
