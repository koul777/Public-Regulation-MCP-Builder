from __future__ import annotations

import unittest

from app.ingestion.vector_adapter import build_vector_records
from app.services.approval_governance import (
    apply_ai_review_decisions_to_preview_text,
    approval_review_completion_state,
    approval_state_transition,
    build_approval_review_events,
)


class ApprovalGovernanceInvariantTests(unittest.TestCase):
    def test_ai_reflect_decision_is_not_approval(self) -> None:
        preview = apply_ai_review_decisions_to_preview_text(
            "draft content",
            [{"item_id": "risk-1", "title": "표 구조", "suggestion": "Kordoc 표 대조"}],
            {"risk-1": "reflect"},
        )
        state = approval_review_completion_state(["risk-1"], {"risk-1": "reflect"}, human_confirmed=False)

        self.assertIn("Kordoc 표 대조", preview)
        self.assertFalse(state["approve_enabled"])
        self.assertFalse(state["human_confirmed"])

    def test_unreviewed_preview_does_not_write_official_approved_vectors(self) -> None:
        records, summary = build_vector_records(
            [
                {
                    "document_id": "doc-1",
                    "chunk_id": "chunk-1",
                    "chunk_type": "article",
                    "text": "미검수 본문",
                    "retrieval_text": "미검수 본문",
                    "approval_status": "draft",
                    "metadata": {
                        "document_id": "doc-1",
                        "chunk_id": "chunk-1",
                        "approval_status": "draft",
                    },
                }
            ],
            require_approval=True,
        )

        self.assertEqual([], records)
        self.assertEqual(1, summary["skipped_unapproved_count"])
        self.assertEqual({"draft": 1}, summary["approval_status_counts"])

    def test_state_transition_records_pending_reviewed_approved_sequence(self) -> None:
        transition = approval_state_transition(["draft", "needs_review"])

        self.assertEqual(["pending_human_review"], transition["from_statuses"])
        self.assertEqual(["pending_human_review", "reviewed", "approved"], transition["required_sequence"])
        self.assertEqual("approved", transition["final_status"])

    def test_approval_without_review_requires_override_reason_in_audit_event(self) -> None:
        events = build_approval_review_events(
            chunk_id="chunk-1",
            actor="reviewer",
            item_ids=["risk-1"],
            ai_decisions={},
            human_confirmed=False,
            approve_event="approved",
            override_reason="offline director approval",
            timestamp="2026-07-12T00:00:00+00:00",
        )

        self.assertEqual(["approved_without_review"], [event["event"] for event in events])
        self.assertEqual("offline director approval", events[0]["override_reason"])
        self.assertEqual("reviewer", events[0]["actor"])


if __name__ == "__main__":
    unittest.main()

