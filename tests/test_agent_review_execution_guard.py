from __future__ import annotations

import unittest
from pathlib import Path

from app.agents.execution_guard import (
    build_minimal_provider_payload,
    payload_hash,
    preflight_agent_review_execution,
    prompt_hash,
    validate_provider_execution_request,
)
from app.agents.review_policy import agent_review_content_hash
from app.core.config import Settings
from app.schemas.chunk import Chunk


REVIEW_PROMPT = "Review."


def planned_review(text: str = "body") -> dict:
    content_hash = agent_review_content_hash(
        chunk_type="article",
        text=text,
        reasons=["table_like_without_cell_rows"],
    )
    return {
        "status": "planned",
        "selected_count": 1,
        "estimated_input_tokens": 120,
        "estimated_output_tokens": 50,
        "estimated_total_tokens": 213,
        "selected_candidates": [
            {
                "chunk_id": "chunk_review",
                "chunk_type": "article",
                "reasons": ["table_like_without_cell_rows"],
                "content_hash": content_hash,
            }
        ],
    }


def execution_settings(**overrides) -> Settings:
    values = {
        "data_dir": Path("data"),
        "enable_agent_review": True,
        "openai_api_key": "configured",
        "agent_review_model": "example-model",
        "agent_review_price_version": "2026-07-03",
        "agent_review_price_effective_at": "2026-07-03T00:00:00Z",
        "agent_review_max_documents_per_batch": 1,
        "agent_review_max_input_tokens_per_batch": 122,
        "agent_review_max_total_tokens_per_batch": 215,
        "agent_review_input_price_per_1m_tokens": 1,
        "agent_review_output_price_per_1m_tokens": 4,
        "agent_review_max_cost_per_batch": 0.01,
    }
    values.update(overrides)
    return Settings(**values)


def allowed_reservation(plan: dict | None = None) -> dict:
    return preflight_agent_review_execution(
        [plan or planned_review()],
        execution_settings(),
        actor="service:batch",
        approval_reference="approval-123",
        prompt=REVIEW_PROMPT,
    )


class AgentReviewExecutionGuardTests(unittest.TestCase):
    def test_preflight_fails_closed_without_batch_caps(self) -> None:
        result = preflight_agent_review_execution([planned_review()], Settings(data_dir=Path("data"), enable_agent_review=False))

        self.assertFalse(result["allowed"])
        self.assertEqual(result["api_call_count"], 0)
        self.assertTrue(any("ENABLE_AGENT_REVIEW" in error for error in result["errors"]))
        self.assertTrue(any("OPENAI_API_KEY" in error for error in result["errors"]))
        self.assertTrue(any("approval_reference" in error for error in result["errors"]))
        self.assertTrue(any("AGENT_REVIEW_MAX_DOCUMENTS_PER_BATCH" in error for error in result["errors"]))
        self.assertTrue(any("AGENT_REVIEW_MAX_COST_PER_BATCH" in error for error in result["errors"]))

    def test_preflight_fails_closed_without_model_prices_and_cost_cap(self) -> None:
        result = preflight_agent_review_execution(
            [planned_review()],
            execution_settings(
                agent_review_input_price_per_1m_tokens=0,
                agent_review_output_price_per_1m_tokens=0,
                agent_review_max_cost_per_batch=0,
            ),
            actor="service:batch",
            approval_reference="approval-123",
        )

        self.assertFalse(result["allowed"])
        self.assertTrue(any("AGENT_REVIEW_MAX_COST_PER_BATCH" in error for error in result["errors"]))
        self.assertTrue(any("AGENT_REVIEW_INPUT_PRICE_PER_1M_TOKENS" in error for error in result["errors"]))
        self.assertTrue(any("AGENT_REVIEW_OUTPUT_PRICE_PER_1M_TOKENS" in error for error in result["errors"]))

    def test_preflight_fails_closed_without_openai_key(self) -> None:
        result = preflight_agent_review_execution(
            [planned_review()],
            execution_settings(openai_api_key=""),
            actor="service:batch",
            approval_reference="approval-123",
            prompt=REVIEW_PROMPT,
        )

        self.assertFalse(result["allowed"])
        self.assertTrue(any("OPENAI_API_KEY" in error for error in result["errors"]))

    def test_preflight_fails_closed_for_unsupported_provider(self) -> None:
        result = preflight_agent_review_execution(
            [planned_review()],
            execution_settings(llm_provider="unknown"),
            actor="service:batch",
            approval_reference="approval-123",
            prompt=REVIEW_PROMPT,
        )

        self.assertFalse(result["allowed"])
        self.assertTrue(any("LLM_PROVIDER must be one of" in error for error in result["errors"]))

    def test_preflight_rejects_non_planned_review_plan(self) -> None:
        plan = planned_review()
        plan["status"] = "api_configuration_needed"

        result = preflight_agent_review_execution(
            [plan],
            execution_settings(),
            actor="service:batch",
            approval_reference="approval-123",
            prompt=REVIEW_PROMPT,
        )

        self.assertFalse(result["allowed"])
        self.assertTrue(any("status planned" in error for error in result["errors"]))

    def test_preflight_allows_with_explicit_caps_and_cost_budget(self) -> None:
        result = allowed_reservation()

        self.assertTrue(result["allowed"])
        self.assertEqual(result["selected_documents"], 1)
        self.assertEqual(result["actor"], "service:batch")
        self.assertEqual(result["approval_reference"], "approval-123")
        self.assertEqual(result["approved_model"], "example-model")
        self.assertEqual(result["price_version"], "2026-07-03")
        self.assertEqual(result["selected_chunk_ids"], ["chunk_review"])
        self.assertEqual(result["selected_content_hashes"], {"chunk_review": planned_review()["selected_candidates"][0]["content_hash"]})
        self.assertEqual(result["prompt_hash"], prompt_hash(REVIEW_PROMPT))
        self.assertEqual(result["prompt_input_tokens"], 2)
        self.assertEqual(result["chunk_input_tokens"], 120)
        self.assertEqual(result["estimated_input_tokens"], 122)
        self.assertEqual(result["estimated_total_tokens"], 215)
        self.assertEqual(result["estimated_total_cost"], "0.0003")
        self.assertEqual(result["max_cost_per_batch"], "0.01")
        self.assertEqual(result["api_call_count"], 0)

    def test_preflight_rejects_selected_count_without_chunk_ids(self) -> None:
        plan = planned_review()
        plan["selected_candidates"] = []

        result = preflight_agent_review_execution(
            [plan],
            execution_settings(),
            actor="service:batch",
            approval_reference="approval-123",
            prompt=REVIEW_PROMPT,
        )

        self.assertFalse(result["allowed"])
        self.assertTrue(any("selected_count" in error and "chunk ids" in error for error in result["errors"]))

    def test_preflight_rejects_selected_count_mismatch(self) -> None:
        plan = planned_review()
        plan["selected_count"] = 2

        result = preflight_agent_review_execution(
            [plan],
            execution_settings(),
            actor="service:batch",
            approval_reference="approval-123",
            prompt=REVIEW_PROMPT,
        )

        self.assertFalse(result["allowed"])
        self.assertTrue(any("does not match" in error for error in result["errors"]))

    def test_preflight_rejects_candidates_without_selected_count(self) -> None:
        plan = planned_review()
        plan["selected_count"] = 0

        result = preflight_agent_review_execution(
            [plan],
            execution_settings(),
            actor="service:batch",
            approval_reference="approval-123",
            prompt=REVIEW_PROMPT,
        )

        self.assertFalse(result["allowed"])
        self.assertTrue(any("does not match" in error for error in result["errors"]))

    def test_preflight_rejects_estimated_cost_above_cap(self) -> None:
        result = preflight_agent_review_execution(
            [planned_review()],
            execution_settings(
                agent_review_input_price_per_1m_tokens=100,
                agent_review_output_price_per_1m_tokens=100,
                agent_review_max_cost_per_batch=0.0001,
            ),
            actor="service:batch",
            approval_reference="approval-123",
            prompt=REVIEW_PROMPT,
        )

        self.assertFalse(result["allowed"])
        self.assertTrue(any("exceeds batch cost cap" in error for error in result["errors"]))

    def test_provider_payload_uses_normalized_text_without_source_metadata(self) -> None:
        plan = planned_review(text="normalized body")
        reservation = allowed_reservation(plan)
        chunk = Chunk(
            chunk_id="chunk_review",
            document_id="doc_review",
            source_node_ids=["node_1"],
            chunk_type="article",
            text="raw body",
            normalized_text="normalized body",
            retrieval_text="[document] PUBLIC_PORTAL board 123\n[source_url] https://example.test\nnormalized body",
            metadata={
                "source_system": "PUBLIC_PORTAL",
                "source_url": "https://example.test",
                "source_record_id": "123",
                "hierarchy_path": "Document > Article 1",
            },
            source_page_start=1,
            source_page_end=2,
        )

        payload = build_minimal_provider_payload(plan, [chunk], budget_reservation=reservation)

        self.assertFalse(payload["source_metadata_included"])
        self.assertEqual(payload["text_basis"], "normalized_text")
        self.assertEqual(payload["item_count"], 1)
        self.assertEqual(payload["items"][0]["text"], "normalized body")
        self.assertEqual(payload["items"][0]["content_hash"], plan["selected_candidates"][0]["content_hash"])
        self.assertTrue(payload["payload_hash"].startswith("sha256:"))
        self.assertNotIn("source_url", payload["items"][0])
        self.assertNotIn("hierarchy_path", payload["items"][0])
        validate_provider_execution_request(
            budget_reservation=reservation,
            payload=payload,
            provider="openai",
            model="example-model",
            prompt=REVIEW_PROMPT,
        )

    def test_provider_execution_request_rejects_model_or_payload_tampering(self) -> None:
        reservation = allowed_reservation()
        chunk = Chunk(
            chunk_id="chunk_review",
            document_id="doc_review",
            source_node_ids=["node_1"],
            chunk_type="article",
            text="body",
            normalized_text="body",
        )
        payload = build_minimal_provider_payload(planned_review(), [chunk], budget_reservation=reservation)

        with self.assertRaisesRegex(ValueError, "Model does not match"):
            validate_provider_execution_request(
                budget_reservation=reservation,
                payload=payload,
                provider="openai",
                model="other-model",
                prompt=REVIEW_PROMPT,
            )
        payload["items"][0]["text"] = "tampered"
        with self.assertRaisesRegex(ValueError, "payload hash"):
            validate_provider_execution_request(
                budget_reservation=reservation,
                payload=payload,
                provider="openai",
                model="example-model",
                prompt=REVIEW_PROMPT,
            )

    def test_provider_execution_request_rejects_prompt_tampering(self) -> None:
        reservation = allowed_reservation()
        chunk = Chunk(
            chunk_id="chunk_review",
            document_id="doc_review",
            source_node_ids=["node_1"],
            chunk_type="article",
            text="body",
            normalized_text="body",
        )
        payload = build_minimal_provider_payload(planned_review(), [chunk], budget_reservation=reservation)

        with self.assertRaisesRegex(ValueError, "Prompt hash"):
            validate_provider_execution_request(
                budget_reservation=reservation,
                payload=payload,
                provider="openai",
                model="example-model",
                prompt="Different prompt.",
            )

    def test_provider_payload_rejects_chunks_outside_reservation(self) -> None:
        plan = planned_review()
        plan["selected_candidates"][0]["chunk_id"] = "chunk_other"
        chunk = Chunk(
            chunk_id="chunk_other",
            document_id="doc_review",
            source_node_ids=["node_1"],
            chunk_type="article",
            text="body",
            normalized_text="body",
        )

        with self.assertRaisesRegex(ValueError, "outside the budget reservation"):
            build_minimal_provider_payload(plan, [chunk], budget_reservation=allowed_reservation())

    def test_provider_payload_rejects_content_changed_after_reservation(self) -> None:
        plan = planned_review(text="original body")
        reservation = allowed_reservation(plan)
        changed = Chunk(
            chunk_id="chunk_review",
            document_id="doc_review",
            source_node_ids=["node_1"],
            chunk_type="article",
            text="changed body",
            normalized_text="changed body",
        )

        with self.assertRaisesRegex(ValueError, "content hash"):
            build_minimal_provider_payload(plan, [changed], budget_reservation=reservation)

    def test_provider_payload_rejects_missing_selected_chunks(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing selected chunks"):
            build_minimal_provider_payload(planned_review(), [], budget_reservation=allowed_reservation())

    def test_provider_payload_rejects_selected_count_mismatch(self) -> None:
        plan = planned_review()
        plan["selected_count"] = 0

        with self.assertRaisesRegex(ValueError, "selected_count does not match"):
            build_minimal_provider_payload(plan, [], budget_reservation=allowed_reservation())

    def test_provider_execution_request_rejects_empty_payload_for_nonempty_reservation(self) -> None:
        reservation = allowed_reservation()
        payload = {
            "mode": "minimal_provider_payload",
            "budget_reservation_id": reservation["reservation_id"],
            "source_metadata_included": False,
            "text_basis": "normalized_text",
            "item_count": 0,
            "items": [],
        }
        payload["payload_hash"] = payload_hash(payload)

        with self.assertRaisesRegex(ValueError, "at least one reserved chunk"):
            validate_provider_execution_request(
                budget_reservation=reservation,
                payload=payload,
                provider="openai",
                model="example-model",
                prompt=REVIEW_PROMPT,
            )

    def test_provider_payload_requires_allowed_reservation(self) -> None:
        with self.assertRaisesRegex(ValueError, "allowed budget reservation"):
            build_minimal_provider_payload(planned_review(), [], budget_reservation={"allowed": False})


if __name__ == "__main__":
    unittest.main()
