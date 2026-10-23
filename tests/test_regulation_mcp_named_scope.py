from __future__ import annotations

import unittest
from unittest.mock import patch

from app.mcp_server.regulation_tools import _mcp_relevance_guard
from app.retrieval.tokenizer import FALLBACK_TOKENIZER_MODEL, tokenize


class RegulationMcpNamedScopeTests(unittest.TestCase):
    @staticmethod
    def payment_result(**extra: object) -> dict:
        return {
            "score": 8.0,
            "regulation_title": "보수 지급규정",
            "article_title": "이주보조금 지급",
            "text": "이주보조금과 주거 수당의 지급 조건 및 계산 방법을 정한다.",
            **extra,
        }

    def test_explicit_named_rule_cannot_drop_its_qualifier(self) -> None:
        guard = _mcp_relevance_guard(
            "가상연구소 이주보조금 규정의 주거 수당 지급 조건은 무엇인가요?",
            [self.payment_result()],
        )
        self.assertTrue(guard["refused"])

    def test_quoted_rule_cannot_be_rescued_by_generic_payment_overlap(self) -> None:
        guard = _mcp_relevance_guard(
            "「가상연구소 이주보조금 규정」에 따르면 주거 수당 지급 조건은 무엇인가요?",
            [self.payment_result(), self.payment_result(text="주거 수당 신청과 지급 조건을 확인한다.")],
        )
        self.assertTrue(guard["refused"])

    def test_spacing_variation_in_known_named_rule_is_allowed(self) -> None:
        guard = _mcp_relevance_guard(
            "사내 이동 지원 규정의 주거 수당 지급 조건은 무엇인가요?",
            [self.payment_result(regulation_title="사내이동지원규정")],
        )
        self.assertFalse(guard["refused"])

    def test_lower_ranked_named_rule_is_preserved(self) -> None:
        guard = _mcp_relevance_guard(
            "가상연구소 이주보조금 규정의 주거 수당 지급 조건은 무엇인가요?",
            [self.payment_result(), self.payment_result(regulation_title="가상연구소이주보조금규정")],
        )
        self.assertFalse(guard["refused"])
        self.assertEqual(guard.get("promote_result_index"), 1)

    def test_scope_words_in_unrelated_body_do_not_establish_named_rule(self) -> None:
        guard = _mcp_relevance_guard(
            "사내 이동 지원 규정에 따르면 주거 수당 지급 조건은 무엇인가요?",
            [self.payment_result(text="사내 이동 지원 대상 직원에게 주거 수당을 지급한다.")],
        )
        self.assertTrue(guard["refused"])

    def test_title_occurring_only_as_cross_reference_is_not_the_named_rule(self) -> None:
        guard = _mcp_relevance_guard(
            "「사내 이동 지원 규정」에 따르면 주거 수당 지급 조건은 무엇인가요?",
            [self.payment_result(text="주거 수당 지급 조건은 사내 이동 지원 규정을 참조한다.")],
        )
        self.assertTrue(guard["refused"])

    def test_quoted_exact_title_supports_the_named_rule(self) -> None:
        guard = _mcp_relevance_guard(
            "「사내 이동 지원 규정」에 따르면 주거 수당 지급 조건은 무엇인가요?",
            [self.payment_result(regulation_title="사내이동지원규정")],
        )
        self.assertFalse(guard["refused"])

    def test_shortened_unquoted_name_can_match_longer_regulation_title(self) -> None:
        guard = _mcp_relevance_guard(
            "사내 이동 지원 규정의 주거 수당 지급 조건은 무엇인가요?",
            [self.payment_result(regulation_title="직원의 사내 이동 및 주거 지원 규정")],
        )
        self.assertFalse(guard["refused"])

    def test_single_broad_noun_keeps_existing_relevance_behavior(self) -> None:
        guard = _mcp_relevance_guard(
            "복무규정의 직원 휴가 신청 절차는 무엇인가요?",
            [{"regulation_title": "복무 규정", "text": "직원 휴가 신청 절차를 정한다."}],
        )
        self.assertFalse(guard["refused"])

    def test_quoted_single_noun_requires_the_actual_regulation_title(self) -> None:
        guard = _mcp_relevance_guard(
            "「복무규정」의 직원 휴가 신청 절차는 무엇인가요?",
            [{"regulation_title": "인사규정",
              "text": "직원 휴가 신청 절차는 복무규정을 참조한다."}],
        )
        self.assertTrue(guard["refused"])

    def test_article_locator_survives_named_title_extraction(self) -> None:
        guard = _mcp_relevance_guard(
            "근태 관리규정 제22조",
            [{"regulation_title": "근태 관리규정", "article_no": "제1조",
              "text": "제1조 목적과 적용 범위를 정한다."}],
        )
        self.assertTrue(guard["refused"])

    def test_article_locator_can_promote_the_matching_scoped_candidate(self) -> None:
        guard = _mcp_relevance_guard(
            "근태 관리규정 제22조",
            [{"regulation_title": "근태 관리규정", "article_no": "제1조", "text": "제1조 목적"},
             {"regulation_title": "근태 관리규정", "article_no": "제22조", "text": "제22조 휴가"}],
        )
        self.assertFalse(guard["refused"])
        self.assertEqual(guard.get("promote_result_index"), 1)

    def test_appendix_locator_survives_named_title_extraction(self) -> None:
        guard = _mcp_relevance_guard(
            "근태 관리규정 별표 3",
            [{"regulation_title": "근태 관리규정", "text": "제1조 목적과 적용 범위를 정한다."}],
        )
        self.assertTrue(guard["refused"])

    def test_matching_title_does_not_rescue_unrelated_question_anchor(self) -> None:
        guard = _mcp_relevance_guard(
            "사내 이동 지원 규정의 원자력연구연수특별수당은 얼마인가요?",
            [self.payment_result(regulation_title="사내이동지원규정", text="주거 이전의 신고 기준을 정한다.")],
        )
        self.assertTrue(guard["refused"])

    def test_exploratory_policy_question_keeps_existing_body_match(self) -> None:
        guard = _mcp_relevance_guard(
            "이주보조금 주거 수당 지급 규정이 있나요?",
            [self.payment_result()],
        )
        self.assertFalse(guard["refused"])

    def test_appointment_synonym_without_an_explicit_rule_name_is_unchanged(self) -> None:
        guard = _mcp_relevance_guard(
            "전임 교원 채용 절차는 어떻게 진행되나요?",
            [{"score": 3.0, "regulation_title": "교원 임용 세칙",
              "text": "교원 임용 세칙에 따라 신규임용 후보자 심사를 진행한다."}],
        )
        self.assertFalse(guard["refused"])

    def test_named_rule_preserves_hiring_and_appointment_synonym(self) -> None:
        guard = _mcp_relevance_guard(
            "교직원 채용 규정에 따르면 지원자 심사는 어떻게 하나요?",
            [{"score": 3.0, "regulation_title": "교직원 임용규정",
              "text": "교직원 임용 절차에서 지원자 심사를 진행한다."}],
        )
        self.assertFalse(guard["refused"])

    def test_context_prefix_before_known_rule_does_not_become_a_qualifier(self) -> None:
        guard = _mcp_relevance_guard(
            "이 규정집의 개인정보 관리지침 제12조에서 기록 보관 기간은 얼마인가요?",
            [{"score": 3.0, "regulation_title": "개인정보관리지침",
              "text": "제12조 기록 보관 기간을 정한다."}],
        )
        self.assertFalse(guard["refused"])

    def test_conversational_clause_is_not_a_named_rule(self) -> None:
        guard = _mcp_relevance_guard(
            "직원이 이주할 때 적용되는 규정에서 주거 수당 지급 조건을 알려 주세요.",
            [self.payment_result(text="직원 주거 수당과 이주보조금 지급 조건을 정한다.")],
        )
        self.assertFalse(guard["refused"])

    def test_unknown_compound_is_not_lost_after_tokenizer_warmup(self) -> None:
        query = "심해잠수근무 휴가 기간은 어떻게 정하나요?"
        candidates = [{"regulation_title": "직원 복무규정",
                       "text": "직원의 휴가 기간과 근무 조건을 정한다."}]

        def morphological_tokens(_backend: object, text: str) -> list[str]:
            if text == query:
                return ["근무", "휴가", "기간"]
            return tokenize(text, tokenizer_model=FALLBACK_TOKENIZER_MODEL)

        with patch("app.retrieval.tokenizer.kiwi_is_ready", return_value=False):
            cold = _mcp_relevance_guard(query, candidates)
        with (
            patch("app.retrieval.tokenizer.kiwi_is_ready", return_value=True),
            patch("app.retrieval.tokenizer._kiwi", return_value=object()),
            patch("app.retrieval.tokenizer._kiwi_tokens", side_effect=morphological_tokens),
        ):
            warm = _mcp_relevance_guard(query, candidates)
        self.assertTrue(cold["refused"])
        self.assertEqual(cold, warm)

    def test_supported_compound_stays_available_after_tokenizer_warmup(self) -> None:
        query = "직원 특별휴가 신청 기간은 어떻게 정하나요?"
        candidates = [{"regulation_title": "직원 복무규정",
                       "text": "직원 특별 휴가 신청 기간은 근무일을 기준으로 정한다."}]
        with patch("app.retrieval.tokenizer.kiwi_is_ready", return_value=False):
            cold = _mcp_relevance_guard(query, candidates)
        with (
            patch("app.retrieval.tokenizer.kiwi_is_ready", return_value=True),
            patch("app.retrieval.tokenizer._kiwi", return_value=object()),
            patch("app.retrieval.tokenizer._kiwi_tokens", return_value=["직원", "특별", "휴가", "신청", "기간"]),
        ):
            warm = _mcp_relevance_guard(query, candidates)
        self.assertFalse(cold["refused"])
        self.assertEqual(cold, warm)


if __name__ == "__main__":
    unittest.main()
