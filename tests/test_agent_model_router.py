from __future__ import annotations

import unittest

from app.agents.model_router import (
    MODEL_PROFILE_REGISTRY,
    QWEN3_ANSWER_MODEL,
    QWEN3_EMBEDDING_MODEL,
    QWEN3_QUERY_MODEL,
    QWEN3_RERANKER_MODEL,
    QWEN3_REVIEW_MODEL,
    model_profile_for_role,
    require_loopback_endpoint,
    validate_model_registry,
)


class AgentModelRouterTests(unittest.TestCase):
    def test_registry_is_local_only_and_valid(self) -> None:
        validate_model_registry()
        self.assertTrue(MODEL_PROFILE_REGISTRY)
        self.assertFalse(any(profile.external_network_allowed for profile in MODEL_PROFILE_REGISTRY.values()))

    def test_difficulty_levels_route_to_distinct_qwen_models(self) -> None:
        self.assertEqual(QWEN3_QUERY_MODEL, model_profile_for_role("query_analyst").model)
        self.assertEqual(QWEN3_REVIEW_MODEL, model_profile_for_role("structure_reviewer").model)
        self.assertEqual(QWEN3_ANSWER_MODEL, model_profile_for_role("grounded_answerer").model)
        self.assertEqual(QWEN3_EMBEDDING_MODEL, model_profile_for_role("semantic_embedder").model)
        self.assertEqual(QWEN3_RERANKER_MODEL, model_profile_for_role("reranker").model)

    def test_deterministic_role_has_no_model_profile(self) -> None:
        self.assertIsNone(model_profile_for_role("security_guard"))

    def test_answer_and_claim_context_limits_do_not_expand_structure_review(self) -> None:
        answer = model_profile_for_role("grounded_answerer")
        audit = model_profile_for_role("claim_auditor")
        self.assertEqual((16_384, 180), (answer.max_input_units, answer.timeout_seconds))
        self.assertEqual((16_384, 45), (audit.max_input_units, audit.timeout_seconds))
        for role in ("structure_reviewer", "table_reviewer"):
            profile = model_profile_for_role(role)
            self.assertEqual((8_192, 45), (profile.max_input_units, profile.timeout_seconds))
            self.assertNotEqual(audit.profile_id, profile.profile_id)

    def test_endpoint_guard_rejects_external_and_credentialed_urls(self) -> None:
        for endpoint in (
            "https://example.com",
            "http://10.0.0.5:11434",
            "http://user:secret@127.0.0.1:11434",
            "http://127.0.0.1:11434?token=secret",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                require_loopback_endpoint(endpoint)

    def test_endpoint_guard_accepts_loopback(self) -> None:
        self.assertEqual("http://127.0.0.1:11434", require_loopback_endpoint("http://127.0.0.1:11434/"))
        self.assertEqual("http://localhost:11434", require_loopback_endpoint("http://localhost:11434"))


if __name__ == "__main__":
    unittest.main()
