from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.agents.claim_auditor import ClaimAuditResult
from app.agents.grounded_qa import AnswerClaim, GroundedAnswerDraft
from app.api import routes_rag
from app.core.config import Settings
from app.rag.context_builder import ContextBuilder
from app.rag.extractive_answer import NO_EVIDENCE_ANSWER


REVIEW_REASON = "table_structure_review_required"


def _record(*, unsafe: bool = True) -> dict:
    return {
        "document_id": "synthetic-document", "chunk_id": "synthetic-table",
        "approval_status": "approved", "approval_id": "synthetic-approval",
        "approved_content_hash": "a" * 64, "regulation_title": "Synthetic rule",
        "text": "Synthetic table: day allowance 25, lodging 100.",
        "table_like": True, "table_review_required": unsafe,
        "table_review_flags": ["unstable_column_count"] if unsafe else [],
    }


def _draft(*, abstained: bool = False) -> GroundedAnswerDraft:
    return GroundedAnswerDraft(
        answer=NO_EVIDENCE_ANSWER if abstained else "Synthetic lodging is 25. [E1]",
        claims=() if abstained else (AnswerClaim(
            claim_id="C1", text="Synthetic lodging is 25.", evidence_context_ids=("E1",),
        ),),
        abstained=abstained,
        answer_mode="abstained" if abstained else "grounded_local",
        model="qwen3:8b",
        fallback_reason=REVIEW_REASON if abstained else None,
    )


class RagTableReviewGateTests(unittest.TestCase):
    def settings(self) -> Settings:
        return Settings(data_dir=Path("synthetic-unused-runtime"))

    def assert_review_abstention(self, response: dict) -> None:
        self.assertEqual(NO_EVIDENCE_ANSWER, response["answer"])
        self.assertEqual("abstained", response["answer_mode"])
        self.assertEqual(REVIEW_REASON, response.get("claim_audit_reason"))
        self.assertEqual("deterministic_review_required", response["claim_audit_status"])
        self.assertEqual("abstained", response["citation_verification_status"])
        self.assertEqual([], response["citations"])
        self.assertEqual(0, response["claim_count"])

    def test_model_abstention_preserves_structure_review_reason(self) -> None:
        with patch.object(routes_rag.GroundedQwenAnswerAgent, "answer", return_value=_draft(abstained=True)), patch.object(
            routes_rag.ClaimAuditAgent, "audit", return_value=ClaimAuditResult(status="abstained", audit_mode="abstained")
        ):
            result = routes_rag._orchestrated_chat_answer(self.settings(), "Synthetic lodging?", [_record()])
        self.assert_review_abstention(result)

    def test_risky_model_claim_cannot_reach_audit_or_extractive_fallback(self) -> None:
        with patch.object(routes_rag.GroundedQwenAnswerAgent, "answer", return_value=_draft()) as answer, patch.object(
            routes_rag.ClaimAuditAgent, "audit"
        ) as audit:
            result = routes_rag._orchestrated_chat_answer(self.settings(), "Synthetic lodging?", [_record()])
        self.assert_review_abstention(result)
        self.assertEqual(1, answer.call_count)
        audit.assert_not_called()

    def test_explicit_audit_structure_rejection_cannot_enter_generic_fallback(self) -> None:
        audit = ClaimAuditResult(status="review_required", audit_mode="deterministic_gate", reason_code=REVIEW_REASON)
        with patch.object(routes_rag.GroundedQwenAnswerAgent, "answer", return_value=_draft()) as answer, patch.object(
            routes_rag.ClaimAuditAgent, "audit", return_value=audit
        ):
            result = routes_rag._orchestrated_chat_answer(self.settings(), "Synthetic lodging?", [_record(unsafe=False)])
        self.assert_review_abstention(result)
        self.assertEqual(1, answer.call_count)

    def test_fast_identity_check_cannot_promote_uncertain_table_to_verified(self) -> None:
        records = [_record()]
        result = routes_rag._deterministic_chat_draft_verification(_draft(), ContextBuilder().build(records), records)
        self.assert_review_abstention(result)

    def test_fast_abstention_retains_structure_review_reason(self) -> None:
        records = [_record()]
        result = routes_rag._deterministic_chat_draft_verification(_draft(abstained=True), ContextBuilder().build(records), records)
        self.assert_review_abstention(result)

    def test_legacy_paths_cannot_resurrect_uncertain_table_from_answer_metadata(self) -> None:
        for backend in ("extractive", "ollama"):
            with self.subTest(backend=backend), patch.object(routes_rag, "GroundedAnswerAgent") as model:
                answer, evidence_ids = routes_rag._chat_answer(backend, SimpleNamespace(), "Synthetic lodging?", [_record()])
                model.assert_not_called()
            self.assertEqual(NO_EVIDENCE_ANSWER, answer)
            self.assertEqual([], evidence_ids)

    def test_healthy_evidence_answer_remains_available_with_uncited_risky_candidate(self) -> None:
        records = [_record(unsafe=False), {**_record(), "chunk_id": "other-table", "document_id": "other-document"}]
        result = routes_rag._deterministic_chat_draft_verification(_draft(), ContextBuilder().build(records), records)
        self.assertEqual("grounded_local", result["answer_mode"])
        self.assertEqual("deterministic_identity_verified", result["claim_audit_status"])
        self.assertEqual(1, len(result["citations"]))


if __name__ == "__main__":
    unittest.main()
