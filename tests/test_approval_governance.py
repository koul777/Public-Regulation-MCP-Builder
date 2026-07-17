from __future__ import annotations

import unittest

from app.services.approval_governance import (
    apply_ai_review_decisions_to_preview_text,
    approval_review_completion_state,
    approval_state_transition,
    build_approval_review_events,
    sanitize_review_decision_events,
)


class ApprovalGovernanceTests(unittest.TestCase):
    def test_completion_state_requires_all_ai_items_and_human_confirm(self) -> None:
        partial = approval_review_completion_state(
            ["item-a", "item-b"],
            {"item-a": "reflect"},
            human_confirmed=True,
        )
        complete = approval_review_completion_state(
            ["item-a", "item-b"],
            {"item-a": "reflect", "item-b": "skip"},
            human_confirmed=True,
        )

        self.assertFalse(partial["ai_confirmed"])
        self.assertFalse(partial["approve_enabled"])
        self.assertEqual(1, partial["remaining"])
        self.assertTrue(complete["ai_confirmed"])
        self.assertTrue(complete["human_confirmed"])
        self.assertTrue(complete["approve_enabled"])
        self.assertEqual(1, complete["reflected"])
        self.assertEqual(1, complete["skipped"])

    def test_ai_decisions_apply_only_to_preview_text(self) -> None:
        preview = apply_ai_review_decisions_to_preview_text(
            "본문",
            [
                {"item_id": "a", "title": "표 구조", "suggestion": "Kordoc 표를 기준으로 확인"},
                {"item_id": "b", "title": "각주", "suggestion": "각주로 분류"},
            ],
            {"a": "reflect", "b": "skip"},
        )

        self.assertIn("본문", preview)
        self.assertIn("[AI 제안 반영 미리보기]", preview)
        self.assertIn("표 구조", preview)
        self.assertNotIn("각주로 분류", preview)

    def test_build_events_records_ai_human_and_override_details(self) -> None:
        events = build_approval_review_events(
            chunk_id="chunk-1",
            actor="reviewer",
            item_ids=["a", "b"],
            ai_decisions={"a": "reflect", "b": "skip"},
            human_confirmed=True,
            table_source="kordoc",
            kordoc_table_promoted=True,
            approve_event="approved",
            override_reason="urgent approved by offline review",
            timestamp="2026-07-12T00:00:00+00:00",
        )

        self.assertEqual(
            ["ai_review_confirmed", "human_review_confirmed", "approved_without_review"],
            [event["event"] for event in events],
        )
        self.assertEqual(1, events[0]["ai_reflected"])
        self.assertEqual(1, events[0]["ai_skipped"])
        self.assertEqual({"a": "reflect", "b": "skip"}, events[0]["ai_decisions"])
        self.assertEqual("kordoc", events[0]["source_of_truth"]["table_source"])
        self.assertTrue(events[0]["source_of_truth"]["kordoc_table_promoted"])
        self.assertEqual("urgent approved by offline review", events[-1]["override_reason"])

    def test_state_transition_records_required_reviewed_step(self) -> None:
        transition = approval_state_transition(["draft", "needs_review"])

        self.assertEqual(["pending_human_review"], transition["from_statuses"])
        self.assertEqual(["pending_human_review", "reviewed", "approved"], transition["required_sequence"])
        self.assertEqual("review_decision_events", transition["reviewed_step_recorded_in"])
        self.assertEqual("approved", transition["final_status"])

    def test_sanitize_review_decision_events_preserves_bulk_evidence(self) -> None:
        raw_events = [
            {
                "event": "human_review_confirmed",
                "timestamp": "2026-07-12T00:00:00+00:00",
                "actor": "reviewer",
                "chunk_id": f"chunk-{index}",
            }
            for index in range(208)
        ]

        events = sanitize_review_decision_events(raw_events)

        self.assertEqual(208, len(events))
        self.assertEqual("chunk-0", events[0]["chunk_id"])
        self.assertEqual("chunk-207", events[-1]["chunk_id"])


if __name__ == "__main__":
    unittest.main()
