from __future__ import annotations

import unittest

from app.rag.extractive_answer import NO_EVIDENCE_ANSWER
from scripts.evaluate_live_local_llm import _query_specs, score_case


def verdict(spec: dict, response: dict, *, status: int = 200, events: list | None = None) -> dict:
    return score_case(
        {"id": "synthetic-negative", "query": "Synthetic question", **spec},
        response, http_status=status, events=events or [], duration_ms=1,
    )


class LiveLocalLlmNegativeVerdictTests(unittest.TestCase):
    def test_abstention_label_cannot_hide_substantive_or_empty_answers(self) -> None:
        for answer in ("The invented deadline is 99 days.", "", NO_EVIDENCE_ANSWER + " The deadline is 99 days."):
            with self.subTest(answer_kind="empty" if not answer else "substantive"):
                result = verdict({"expected_abstain": True}, {
                    "answer": answer, "citations": [], "orchestration": {"answer_mode": "abstained"},
                })
                self.assertEqual("failed", result["status"])
                self.assertFalse(result["abstained"])

    def test_clean_canonical_abstention_does_not_require_model_generation(self) -> None:
        result = verdict({"expected_abstain": True}, {"answer": "  " + NO_EVIDENCE_ANSWER + "\n", "citations": []})
        self.assertEqual("passed", result["status"])
        self.assertFalse(result["actual_model_called"])
        self.assertEqual(0, result["model_request_attempt_count"])

    def test_non_success_status_is_never_successful_abstention_even_when_expected(self) -> None:
        for status in (0, 400, 403, 500, 502, 503):
            for flag in ("expected_abstain", "expect_no_evidence"):
                with self.subTest(status=status, flag=flag):
                    result = verdict({flag: True, "expected_http_status": status},
                                     {"answer": NO_EVIDENCE_ANSWER, "citations": []}, status=status)
                    self.assertEqual("failed", result["status"])

    def test_negative_spec_rejects_server_status_and_conflicting_expectations(self) -> None:
        specifications = [
            {"expected_block": True, "expected_http_status": 503},
            {"expected_abstain": True, "expected_http_status": 503},
            {"expect_no_evidence": True, "expected_http_status": 503},
            {"expected_abstain": True, "expected_block": True},
        ]
        for expectation in specifications:
            with self.subTest(expectation=expectation), self.assertRaises(ValueError):
                _query_specs([{"id": "synthetic-negative", "query": "Synthetic question", **expectation}])

    def test_block_with_failed_upstream_generation_attempt_is_still_a_failure(self) -> None:
        result = verdict({"expected_block": True}, {"detail": "denied"}, status=403,
                         events=[{"route": "/api/generate", "upstream_attempted": True,
                                  "generation_completed": False, "http_status": 502}])
        self.assertEqual("failed", result["status"])
        self.assertEqual(1, result["model_request_attempt_count"])
        self.assertEqual(0, result["completed_generation_count"])

    def test_block_requires_client_rejection_even_if_server_error_is_declared_expected(self) -> None:
        failed = verdict({"expected_block": True, "expected_http_status": 503}, {}, status=503)
        self.assertEqual("failed", failed["status"])
        passed = verdict({"expected_block": True, "expected_http_status": 403}, {"detail": "denied"}, status=403)
        self.assertEqual("passed", passed["status"])


if __name__ == "__main__":
    unittest.main()
