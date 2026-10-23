from __future__ import annotations

import unittest

from app.rag.extractive_answer import NO_EVIDENCE_ANSWER
from scripts.evaluate_live_local_llm import score_case, summarize_cases
from tests.test_evaluate_live_local_llm import _event, _response, _spec


def _score(response: dict, *, spec: dict | None = None, status: int = 200, events: list | None = None) -> dict:
    return score_case(spec if spec is not None else _spec(), response, http_status=status,
                      events=events if events is not None else [_event(response.get("answer"))], duration_ms=0)


class LiveLocalLlmFinalVerdictTests(unittest.TestCase):
    def test_missing_or_empty_verification_status_cannot_be_model_success(self) -> None:
        for field, missing_error, rejected_error in (
            ("claim_audit_status", "claim_audit_status_missing", "claim_audit_not_accepted"),
            ("citation_verification_status", "citation_verification_status_missing",
             "citation_verification_not_accepted"),
        ):
            for value in (None, "", "missing"):
                with self.subTest(field=field, value=value):
                    response = _response()
                    if value == "missing":
                        response["orchestration"].pop(field)
                    else:
                        response["orchestration"][field] = value
                    result = _score(response)
                    self.assertEqual("failed", result["status"])
                    self.assertFalse(result["final_model_answer"])
                    self.assertIn(missing_error, result["errors"])
                    self.assertIn(rejected_error, result["errors"])

    def test_missing_both_statuses_is_not_verified_by_generation_or_gold_alone(self) -> None:
        response = _response()
        response["orchestration"].pop("claim_audit_status")
        response["orchestration"].pop("citation_verification_status")
        result = _score(response)
        self.assertTrue(result["actual_model_called"])
        self.assertTrue(result["answer_matches_observed_generation"])
        self.assertEqual("passed", result["correctness"]["status"])
        self.assertTrue(result["citation_checks"]["all_verified"])
        self.assertEqual("failed", result["status"])
        self.assertFalse(result["final_model_answer"])
        self.assertIn("claim_audit_status_missing", result["errors"])
        self.assertIn("citation_verification_status_missing", result["errors"])

    def test_explicit_audit_failure_cannot_be_final_model_success(self) -> None:
        for status in ("rejected", "review_required", "deterministic_review_required", "abstained"):
            with self.subTest(status=status):
                response = _response()
                response["orchestration"]["claim_audit_status"] = status
                result = _score(response)
                self.assertEqual("failed", result["status"])
                self.assertFalse(result["final_model_answer"])
                self.assertIn("claim_audit_not_accepted", result["errors"])

    def test_explicit_citation_failure_cannot_be_final_model_success(self) -> None:
        response = _response()
        response["orchestration"]["citation_verification_status"] = "rejected"
        result = _score(response)
        self.assertEqual("failed", result["status"])
        self.assertFalse(result["final_model_answer"])
        self.assertIn("citation_verification_not_accepted", result["errors"])

    def test_unknown_or_absent_answer_marker_fails_model_audited_contract(self) -> None:
        for answer in ("Submit within seven days. [E99]", "Submit within seven days.",
                       "Submit within seven days. [E1] [E99]"):
            with self.subTest(answer=answer):
                response = _response()
                response["answer"] = answer
                self.assertIn("answer_citation_context_not_verified", _score(response)["errors"])

    def test_missing_or_invalid_citation_context_fails_model_audited_contract(self) -> None:
        for context_id in (None, "", "E1 E99", 1):
            with self.subTest(context_id=context_id):
                response = _response()
                response["citations"][0]["context_id"] = context_id
                self.assertEqual("failed", _score(response)["status"])

    def test_gold_hash_cannot_be_omitted_from_model_audited_citation(self) -> None:
        response = _response()
        del response["citations"][0]["content_hashes"]
        result = _score(response)
        self.assertFalse(result["citation_checks"]["all_verified"])
        self.assertEqual("failed", result["status"])

    def test_model_citation_matching_context_and_hash_passes(self) -> None:
        result = _score(_response())
        self.assertEqual("passed", result["status"])
        self.assertEqual("verified", result["answer_citation_context"]["status"])
        self.assertEqual("verified", result["citation_checks"]["checks"][0]["content_hash_status"])

    def test_legacy_deterministic_contract_reports_missing_context_and_hash(self) -> None:
        response = _response()
        response["orchestration"]["claim_audit_status"] = "deterministic_identity_verified"
        response["citations"][0].pop("context_id")
        response["citations"][0].pop("content_hashes")
        response["citations"][0].pop("support_quote")
        result = _score(response)
        self.assertEqual("passed", result["status"])
        self.assertEqual("not_available_by_contract", result["answer_citation_context"]["status"])
        self.assertEqual("not_available_by_contract", result["citation_checks"]["checks"][0]["content_hash_status"])
        self.assertIn("deterministic_citation_omits_context_and_hash", result["verification_limitations"])

    def test_deterministic_contract_cannot_hide_explicit_wrong_context_or_hash(self) -> None:
        for key, value in (("context_id", "E99"), ("content_hashes", ["b" * 64])):
            with self.subTest(key=key):
                response = _response()
                response["orchestration"]["claim_audit_status"] = "deterministic_identity_verified"
                response["citations"][0][key] = value
                self.assertEqual("failed", _score(response)["status"])

    def test_gold_without_hash_does_not_claim_hash_verification(self) -> None:
        spec = _spec()
        del spec["allowed_citations"][0]["approved_content_hash"]
        response = _response()
        del response["citations"][0]["content_hashes"]
        result = _score(response, spec=spec)
        self.assertEqual("passed", result["status"])
        self.assertEqual("not_available_in_gold", result["citation_checks"]["checks"][0]["content_hash_status"])

    def test_negative_and_fallback_denominators_remain_separate(self) -> None:
        negative = _score({"answer": NO_EVIDENCE_ANSWER, "citations": [],
                           "orchestration": {"claim_audit_status": "review_required"}},
                          spec={"id": "negative", "expected_abstain": True}, events=[])
        blocked = _score({}, spec={"id": "blocked", "expected_block": True}, status=403, events=[])
        response = _response()
        response["orchestration"].update(answer_mode="grounded_extractive", claim_audit_status="fallback_from_rejected")
        fallback = _score(response)
        error = _score({}, status=503, events=[])
        summary = summarize_cases([negative, blocked, fallback, error])
        self.assertEqual("passed", negative["status"])
        self.assertEqual("passed", blocked["status"])
        self.assertEqual(2, summary["answerable_count"])
        self.assertEqual(2, summary["negative_passed_count"])
        self.assertEqual(1, summary["fallback_count"])
        self.assertEqual(2, summary["failed_count"])


if __name__ == "__main__":
    unittest.main()
