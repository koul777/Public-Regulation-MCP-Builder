from __future__ import annotations

import unittest

from app.agents.role_registry import (
    AGENT_ROLE_REGISTRY,
    QWEN3_8B_MODEL,
    WORKFLOW_ROLE_SEQUENCES,
    get_agent_role,
    validate_role_registry,
    workflow_roles,
)


class AgentRoleRegistryTests(unittest.TestCase):
    def test_registry_is_valid(self) -> None:
        validate_role_registry()

    def test_qwen3_8b_is_the_grounded_answerer_model(self) -> None:
        role = get_agent_role("grounded_answerer")
        self.assertEqual(role.primary_model, QWEN3_8B_MODEL)
        self.assertEqual(role.kind, "local_llm_answerer")
        self.assertIn("invent_citation", role.forbidden_actions)

    def test_ai_roles_cannot_approve_chunks(self) -> None:
        for role_id in ("structure_reviewer", "table_reviewer", "query_analyst", "grounded_answerer", "claim_auditor"):
            self.assertIn("approve_chunks", get_agent_role(role_id).forbidden_actions)

    def test_qa_workflow_verifies_citations_after_answer(self) -> None:
        role_ids = WORKFLOW_ROLE_SEQUENCES["local_regulation_qa"]
        self.assertLess(role_ids.index("grounded_answerer"), role_ids.index("citation_verifier"))
        self.assertLess(role_ids.index("reranker"), role_ids.index("context_builder"))
        self.assertLess(role_ids.index("claim_auditor"), role_ids.index("citation_verifier"))
        self.assertEqual(workflow_roles("local_regulation_qa")[-1].role_id, "security_guard")

    def test_ingestion_requires_human_approval_before_indexing(self) -> None:
        role_ids = WORKFLOW_ROLE_SEQUENCES["ingestion_and_approval"]
        self.assertLess(role_ids.index("human_approval_gate"), role_ids.index("index_builder"))
        self.assertLess(role_ids.index("semantic_embedder"), role_ids.index("index_builder"))

    def test_unknown_role_and_workflow_fail_clearly(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown agent role"):
            get_agent_role("missing")
        with self.assertRaisesRegex(ValueError, "Unknown workflow"):
            workflow_roles("missing")

    def test_role_registry_has_expected_core_roles(self) -> None:
        expected = {
            "orchestrator",
            "intake_guard",
            "parser_extractor",
            "ocr_extractor",
            "normalizer",
            "structure_detector",
            "chunk_builder",
            "quality_gate",
            "human_approval_gate",
            "exporter",
            "semantic_embedder",
            "index_builder",
            "query_analyst",
            "query_rewriter",
            "retrieval_guard",
            "reranker",
            "context_builder",
            "grounded_answerer",
            "claim_auditor",
            "citation_verifier",
            "security_guard",
            "evaluation_agent",
            "release_operator",
        }
        self.assertTrue(expected.issubset(AGENT_ROLE_REGISTRY))

    def test_no_registered_role_is_left_as_planned(self) -> None:
        self.assertFalse(
            {
                role_id: role.implementation_status
                for role_id, role in AGENT_ROLE_REGISTRY.items()
                if role.implementation_status == "planned"
            }
        )


if __name__ == "__main__":
    unittest.main()
