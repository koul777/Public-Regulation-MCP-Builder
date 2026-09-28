from __future__ import annotations

import unittest

from app.services.readiness_adapter import OperatorReadinessState, adapt_readiness_report


class ReadinessAdapterTests(unittest.TestCase):
    def test_explicit_failure_overrides_conflicting_success_signals(self) -> None:
        for report in (
            {"overall_state": "connected", "passed": False, "reason": "available"},
            {"passed": True, "probe": True, "deploy_ready": False},
            {"passed": True, "probe": True, "health": {"available": False}},
        ):
            with self.subTest(report=report):
                card = adapt_readiness_report(report, component="로컬 QA")
                self.assertEqual(OperatorReadinessState.ACTION_REQUIRED, card.state)
                self.assertNotIn("응답을 확인했습니다", card.next_action)

    def test_reason_codes_produce_specific_recovery_without_echoing_payload(self) -> None:
        for reason, expected in (
            ("endpoint_not_allowed_or_missing", "localhost"),
            ("local_probe_invalid", "해석하지 못했습니다"),
            ("local_backend_unavailable", "설치 여부"),
        ):
            with self.subTest(reason=reason):
                card = adapt_readiness_report(
                    {"passed": False, "reason": reason, "error": "synthetic-private-detail"},
                    component="로컬 QA",
                )
                self.assertIn(expected, card.next_action)
                self.assertNotIn("synthetic-private-detail", str(card))

    def test_failed_model_free_report_has_no_success_instruction(self) -> None:
        card = adapt_readiness_report({"passed": False, "reason": "model_free_mode"}, component="QA")
        self.assertEqual(OperatorReadinessState.ACTION_REQUIRED, card.state)
        self.assertNotIn("답변할 수", card.next_action)

    def test_invalid_success_types_cannot_enable_ready_state(self) -> None:
        for value in ("true", 1, [], None):
            with self.subTest(value=value):
                card = adapt_readiness_report({"passed": value, "probe": True}, component="QA")
                self.assertNotEqual(OperatorReadinessState.READY, card.state)
