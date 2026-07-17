from __future__ import annotations

import unittest

from app.rag.extractive_answer import (
    NO_EVIDENCE_ANSWER,
    build_structured_extractive_answer,
    select_supporting_answer_results,
)


class ExtractiveAnswerTests(unittest.TestCase):
    def test_aks_lecturer_screening_preserves_governing_article_steps(self) -> None:
        answer = build_structured_extractive_answer(
            "신규채용후보자 심사절차는 단계별로 어떻게 진행되나요?",
            [{
                "article_no": "제11조",
                "article_title": "심사절차",
                "text": "제11조(심사절차) ① 신규채용후보자에 대한 심사는 다음 단계를 거친다. 1. 서류심사 2. 면접심사",
            }],
        )

        self.assertIn("서류심사", answer)
        self.assertIn("면접심사", answer)

    def test_aks_lecturer_recontract_procedure_preserves_opportunity_to_respond(self) -> None:
        answer = build_structured_extractive_answer(
            "재계약 절차에서 의견진술이나 서면제출은 언제 가능한가요?",
            [{
                "article_no": "제16조",
                "article_title": "재계약 절차",
                "text": "제16조(재계약 절차) ④ 7일 이상의 기간을 정하여 의견을 진술하거나 서면으로 의견을 제출할 기회를 주어야 한다. ⑤ 30일 이내 교원소청심사위원회에 청구할 수 있다.",
            }],
        )

        self.assertIn("의견을 진술", answer)
        self.assertIn("서면으로 의견", answer)

    def test_aks_lecturer_recontract_review_preserves_threshold_subject(self) -> None:
        answer = build_structured_extractive_answer(
            "재계약 심사는 어떤 기준으로 70점 이상을 보나요?",
            [{
                "article_no": "제18조",
                "article_title": "재계약 심사",
                "text": "제18조(재계약 심사) ② 재계약을 위한 교원 인사위원회의 심의대상자는 재계약 심사 결과 70점 이상인 사람으로 한다.",
            }],
        )

        self.assertIn("교원 인사위원회", answer)
        self.assertIn("70점", answer)

    def test_childcare_leave_answer_is_structured_from_evidence(self) -> None:
        answer = build_structured_extractive_answer(
            "육아휴직은 얼마나 신청할 수 있어?",
            [
                {
                    "regulation_title": "인사규정",
                    "article_no": "제29조",
                    "article_title": "휴직",
                    "source_page_start": 751,
                    "approval_id": "approval-0",
                    "text": (
                        "제29조(휴직) 만 8세 이하 또는 초등학교 2학년 이하의 자녀를 양육하기 위하여 "
                        "필요하거나 임신 또는 출산하게 되어 휴직을 원하는 때에는 임용권자는 특별한 사정이 없는 한 "
                        "휴직을 명하여야 한다."
                    ),
                },
                {
                    "regulation_title": "인사규정",
                    "article_no": "제30조",
                    "article_title": "휴직 기간",
                    "source_page_start": 752,
                    "source_page_end": 753,
                    "approval_id": "approval-1",
                    "answer_outline": ["자녀 1명에 대하여 육아휴직은 3년 이내로 한다."],
                    "text": (
                        "제30조(휴직 기간) 휴직 기간은 다음과 같다. "
                        "9. 제29조의 규정에 의한 휴직 기간은 자녀 1명에 대하여 3년 이내로 한다. "
                        "다만, 임신ㆍ출산ㆍ육아를 위한 시간선택제 근무를 하려는 경우에는 그 근무기간과 "
                        "휴직기간을 합하여 3년을 초과할 수 없다."
                    ),
                },
                {
                    "article_no": "제31조",
                    "article_title": "휴직 중 의무",
                    "source_page_start": 753,
                    "approval_id": "approval-2",
                    "text": (
                        "제31조(휴직 중 의무) 휴직 중인 교직원은 그 신분은 보유하나 직무에 종사하지 못한다. "
                        "휴직 기간 중 그 사유가 소멸된 때에는 30일 이내 임용권자에게 신고하여야 한다."
                    ),
                },
                {
                    "regulation_title": "교직원보수규정",
                    "article_no": "제33조",
                    "article_title": "육아휴직수당",
                    "source_page_start": 1046,
                    "approval_id": "approval-3",
                    "text": (
                        "제33조(육아휴직수당) 인사규정 제29조제3항에 따른 사유로 30일 이상 휴직한 교직원의 "
                        "육아휴직수당은 육아휴직 시작일부터 6개월째까지 기본연봉월액의 78퍼센트로 하고, "
                        "7개월째 이후는 기본연봉월액의 62.4퍼센트로 한다. 월별 지급액 상한은 3개월째까지 "
                        "250만원, 4개월째부터 6개월째까지 200만원, 7개월째 이후 160만원으로 한다."
                    ),
                },
            ],
        )

        self.assertIn("핵심 답변", answer)
        self.assertIn("자녀 1명", answer)
        self.assertIn("3년", answer)
        self.assertIn("시간선택제", answer)
        self.assertIn("만 8세", answer)
        self.assertIn("초등학교 2학년", answer)
        self.assertIn("30일 이상", answer)
        self.assertIn("78퍼센트", answer)
        self.assertIn("62.4퍼센트", answer)
        self.assertIn("인사규정 제30조 휴직 기간", answer)
        self.assertIn("교직원보수규정 제33조 육아휴직수당", answer)
        self.assertIn("approval=approval-1", answer)

    def test_childcare_allowance_transition_prioritizes_supplementary_application_article(self) -> None:
        main_leave = {
            "regulation_title": "인사규정",
            "article_no": "제29조",
            "article_title": "휴직",
            "source_page_start": 751,
            "approval_id": "approval-leave",
            "text": "제29조(휴직) 만 8세 이하 자녀를 양육하기 위하여 육아휴직을 명하여야 한다.",
        }
        allowance = {
            "regulation_title": "교직원보수규정",
            "article_no": "제33조",
            "article_title": "육아휴직수당",
            "source_page_start": 1046,
            "approval_id": "approval-allowance",
            "text": (
                "제33조(육아휴직수당) 30일 이상 휴직한 교직원의 육아휴직수당은 "
                "기본연봉월액의 78퍼센트와 62.4퍼센트에 해당하는 금액으로 한다."
            ),
        }
        transition_2024 = {
            "regulation_title": "교직원보수규정",
            "article_no": "제2조",
            "article_title": "육아휴직수당 지급에 관한 적용례",
            "source_page_start": 1059,
            "approval_id": "approval-2024",
            "text": (
                "제2조(육아휴직수당 지급에 관한 적용례) 제33조 개정규정은 "
                "2024년 1월 1일 이후 지급하는 육아휴직수당부터 적용한다."
            ),
        }
        transition_2025 = {
            "regulation_title": "교직원보수규정",
            "article_no": "제3조",
            "article_title": "육아휴직수당 지급에 관한 적용례",
            "source_page_start": 1060,
            "approval_id": "approval-2025",
            "text": (
                "제3조(육아휴직수당 지급에 관한 적용례) ① 제33조제1항ㆍ제2항 및 제6항의 "
                "개정규정은 2025년 1월 1일 이후의 육아휴직기간에 대한 육아휴직수당부터 적용한다. "
                "② 제1항에도 불구하고 2025년도 육아휴직수당 지급 대상자 중 제1항을 적용하여 "
                "육아휴직수당 총 지급액이 감액되는 경우에는 종전 규정을 적용한다."
            ),
        }

        results = [allowance, main_leave, transition_2024, transition_2025]
        answer = build_structured_extractive_answer(
            "육아휴직수당 지급에 관한 적용례는 어떻게 되나요?",
            results,
        )
        supporting = select_supporting_answer_results(
            "육아휴직수당 지급에 관한 적용례는 어떻게 되나요?",
            results,
        )

        self.assertEqual("제3조", supporting[0]["article_no"])
        self.assertEqual("육아휴직수당 지급에 관한 적용례", supporting[0]["article_title"])
        self.assertIn("2025년 1월 1일", answer)
        self.assertIn("감액", answer)
        self.assertIn("종전 규정", answer)
        self.assertIn("교직원보수규정 제3조 육아휴직수당 지급에 관한 적용례", answer)

    def test_leave_foreign_travel_report_includes_governing_article_support(self) -> None:
        travel_rule = {
            "regulation_title": "근태 관리",
            "article_no": "",
            "article_title": "",
            "source_page_start": 1128,
            "source_page_end": 1130,
            "approval_id": "approval-form",
            "text": (
                "⑤ 휴직자가 국외로 출국하는 경우에는 별지 제16호서식에 따른 "
                "휴직자 국외 출국 신고서를 작성하여 출국 7일 전까지 사무국장에게 제출해야 한다. "
                "다만 14일 이하 국외 출국과 영유아를 동반한 육아휴직자의 국외 출국의 경우 신고를 생략할 수 있다."
            ),
        }
        governing_article = {
            "regulation_title": "인사규정",
            "article_no": "제29조의3",
            "article_title": "휴직자의 복무실태 점검",
            "source_page_start": 1128,
            "source_page_end": 1130,
            "approval_id": "approval-article",
            "text": (
                "제29조의3(휴직자의 복무실태 점검) ① 인사규정 제29조에 따라 휴직 중인 교직원이 "
                "휴직기간 중 휴직사유와 달리 휴직의 목적 외 사용을 하는 경우를 정한다."
            ),
        }
        unrelated_form = {
            "regulation_title": "복무규정",
            "article_no": "",
            "article_title": "",
            "source_page_start": 1148,
            "approval_id": "approval-unrelated",
            "text": "[별지 제16호서식] 휴직자 국외 출국 신고서 1. 소속 2. 직급 3. 성명",
        }

        query = "휴직자 국외 출국 신고서는 언제 제출하나요?"
        answer = build_structured_extractive_answer(query, [travel_rule, unrelated_form, governing_article])
        supporting = select_supporting_answer_results(query, [travel_rule, unrelated_form, governing_article])

        self.assertIn(governing_article, supporting)
        self.assertIn(travel_rule, supporting)
        self.assertIn("제29조의3", answer)
        self.assertIn("출국 7일 전", answer)
        self.assertIn("14일 이하", answer)
        self.assertIn("영유아", answer)

    def test_faculty_hiring_answer_keeps_process_steps(self) -> None:
        answer = build_structured_extractive_answer(
            "전임 교원 채용 절차는 어떻게 돼?",
            [
                {
                    "article_no": "제5조",
                    "article_title": "신규채용의 시기",
                    "source_page_start": 785,
                    "source_page_end": 789,
                    "text": (
                        "원장은 지원 마감일 전까지 15일 이상 채용분야, 채용인원, 지원자격, 심사기준 등에 관한 "
                        "사항을 공고한다. 제5조(신규채용의 시기) 신규채용은 3월 1일과 9월 1일에 하는 것을 원칙으로 한다. "
                        "제7조(신규채용 후보자 심사) 신규채용 후보자에 대하여 단계별로 다음 각 호의 사항을 심사한다. "
                        "1. 기초심사 2. 연구실적심사 3. 공개발표심사 4. 면접심사"
                    ),
                }
            ],
        )

        self.assertIn("15일", answer)
        self.assertIn("기초심사", answer)
        self.assertIn("연구실적심사", answer)
        self.assertIn("공개발표심사", answer)
        self.assertIn("면접심사", answer)
        self.assertIn("근거 조항", answer)

    def test_faculty_hiring_answer_repairs_spacing_and_deduplicates_notice(self) -> None:
        answer = build_structured_extractive_answer(
            "전임 교원 채용 절차는?",
            [
                {
                    "regulation_title": "인사규정",
                    "article_no": "제6조",
                    "article_title": "공고",
                    "text": (
                        "③원장은 지원 마감일 전까지 15일 이상 채용분야, 채용인원, 지원자격, 심사기준 등 에 관한 "
                        "사항을 효과적인 방법 으로 공고하여야 한다. "
                        "지원 마감일 전까지 15일 이상 채용분야, 채용인원, 지원자격, 심사기준 등에 관한 사항을 공고한다."
                    ),
                },
                {
                    "regulation_title": "인사규정",
                    "article_no": "제7조",
                    "article_title": "신규채용 후보자 심사",
                    "text": (
                        "신규임용후보자에 대하여 단계별로 다음 각 호의 사항을 심사한다. "
                        "1. 기초심사 2. 연구실적심사 3. 공개발표심사 4. 면접심사"
                    ),
                },
                {
                    "regulation_title": "인사규정",
                    "article_no": "제5조",
                    "article_title": "신규채용의 시기",
                    "text": "신규채용은 3월 1일과 9월 1일에 하는 것을 원칙으 로 한다.",
                },
            ],
        )

        self.assertIn("방법으로", answer)
        self.assertIn("등에", answer)
        self.assertIn("③ 원장은", answer)
        self.assertNotIn("방법 으로", answer)
        self.assertNotIn("등 에", answer)
        self.assertNotIn("③원장은", answer)
        self.assertNotIn("원칙으 로", answer)
        self.assertNotIn("세부 근거", answer)
        self.assertEqual(1, answer.count("지원 마감일 전까지 15일 이상"))

    def test_faculty_hiring_answer_keeps_full_time_definition_but_excludes_lecturer_notice(self) -> None:
        lecturer_notice = {
            "regulation_title": "강사임용규정",
            "article_no": "제9조",
            "article_title": "임용공고",
            "text": "강사를 신규채용하는 경우에는 채용분야, 채용인원, 지원자격 등을 7일 이상 공고한다.",
        }
        faculty_definition = {
            "regulation_title": "대학원학칙",
            "article_no": "제38조",
            "article_title": "교원",
            "text": "대학원의 교원은 교수직 및 강사이고, 전임 교원은 교수, 부교수, 조교수로 한다.",
        }
        faculty_process = {
            "regulation_title": "교원 임용 세칙",
            "article_no": "제8조",
            "article_title": "신규임용 후보자 심사",
            "text": (
                "원장은 지원 마감일 전까지 15일 이상 임용분야, 임용인원, 지원자격, 심사기준 등에 관한 "
                "사항을 공고한다. 신규임용 후보자에 대하여 기초심사, 연구실적심사, 공개발표심사, "
                "면접심사를 진행한다."
            ),
        }

        answer = build_structured_extractive_answer(
            "전임 교원 채용 절차는?", [lecturer_notice, faculty_definition, faculty_process]
        )

        self.assertIn("전임 교원은 교수, 부교수, 조교수", answer)
        self.assertIn("15일 이상", answer)
        self.assertIn("기초심사", answer)
        self.assertIn("면접심사", answer)
        self.assertNotIn("강사를 신규채용", answer)

    def test_no_evidence_answer_is_plain_korean(self) -> None:
        self.assertEqual(NO_EVIDENCE_ANSWER, build_structured_extractive_answer("없는 질문", []))

    def test_metadata_lines_are_not_used_as_answer_sentences(self) -> None:
        answer = build_structured_extractive_answer(
            "육아휴직",
            [
                {
                    "article_no": "제10조",
                    "article_title": "육아휴직",
                    "text": "키워드: 육아휴직, 기간\n의도: duration\n제10조(육아휴직) 육아휴직은 3년 이내로 한다.",
                }
            ],
        )

        self.assertIn("육아휴직은 3년 이내로 한다", answer)
        self.assertNotIn("키워드:", answer)
        self.assertNotIn("의도:", answer)

    def test_fragment_sentences_are_not_used_as_answer_sentences(self) -> None:
        answer = build_structured_extractive_answer(
            "성과연봉은 언제 지급돼?",
            [
                {
                    "article_no": "제24조",
                    "article_title": "연봉의 지급 방법",
                    "text": (
                        "제24조(연봉의 지급 방법)\n"
                        "② 성과연봉은 이등분하여 6월 및 12월에 일 시금으로 지급한다.\n"
                        "③ 연도 중 퇴직하는 교직원의 경우에는 연도말까지의 미지급된 성과연봉을 일\n"
                        "④ 제23조 제5항에 따른 연봉계약기간의 만료 이후 연봉 조정이 이루어지지"
                    ),
                }
            ],
        )

        self.assertIn("6월 및 12월", answer)
        self.assertIn("일시금", answer)
        self.assertNotIn("제24조(연봉의 지급 방법)", answer)
        self.assertNotIn("성과연봉을 일\n", answer)
        self.assertNotIn("이루어지지", answer)

    def test_performance_pay_answer_prioritizes_payment_timing(self) -> None:
        answer = build_structured_extractive_answer(
            "성과연봉은 언제 어떻게 지급되나?",
            [
                {
                    "article_no": "제24조",
                    "article_title": "연봉의 지급 방법",
                    "text": (
                        "④ 제23조 제5항에 따른 연봉계약기간의 만료 이후 연봉 조정이 이루어지지 않을 경우에는 "
                        "전년도 기본연봉월액 계약 체결 전까지 지급하되, 평가결과에 따라 정산하여 일괄 지급한다. "
                        "① 기본연봉은 기본연봉월액으로 지급한다. "
                        "② 성과연봉은 이등분하여 6월 및 12월에 일시금으로 지급한다. "
                        "③ 연도 중 퇴직하는 교직원의 경우에는 연도말까지의 미지급된 성과연봉을 일시금으로 지급할 수 있다. "
                        "⑤ 성과연봉을 부정한 방법으로 지급받은 때에는 환수한다."
                    ),
                }
            ],
        )

        self.assertLess(answer.index("② 성과연봉은"), answer.index("④ 제23조"))
        self.assertIn("6월 및 12월", answer)
        self.assertNotIn("세부 근거", answer)

    def test_performance_pay_exclusion_answer_prioritizes_exclusion_reasons(self) -> None:
        answer = build_structured_extractive_answer(
            "성과연봉 지급 제외 사유는 무엇인가요?",
            [
                {
                    "regulation_title": "교직원보수규정",
                    "article_no": "제24조",
                    "article_title": "연봉의 지급 방법",
                    "text": "② 성과연봉은 이등분하여 6월 및 12월에 일시금으로 지급한다.",
                },
                {
                    "regulation_title": "교직원보수규정",
                    "article_no": "제27조의2",
                    "article_title": "성과연봉 지급대상 제외",
                    "text": (
                        "제27조의2(성과연봉 지급대상 제외) 평가대상 기간 중 중징계 처분을 받거나 다음과 같은 사유로 "
                        "징계를 받은 경우 해당연도 성과연봉 지급 대상에서 제외한다. "
                        "1. 「인사규정」에 따른 중징계 처분 "
                        "2. 「인사규정」에 따른 징계 사유의 시효가 5년인 비위 "
                        "3. 「성폭력범죄의 처벌 등에 관한 특례법」에 따른 성폭력 범죄 "
                        "4. 「성매매알선 등 행위의 처벌에 관한 법률」에 따른 성매매 "
                        "5. 「국가인권위원회법」에 따른 성희롱 "
                        "6. 「도로교통법」에 따른 음주운전 또는 음주측정에 대한 불응"
                    ),
                },
                {
                    "regulation_title": "명예퇴직및조기퇴직수당지급세칙",
                    "article_no": "제8조",
                    "article_title": "지급 제한",
                    "text": "평가대상연도 중 중징계 처분을 받은 경우 경영평가 성과급 지급 시 최하위 등급을 부여한다.",
                },
            ],
        )
        supporting = select_supporting_answer_results(
            "성과연봉 지급 제외 사유는 무엇인가요?",
            [
                {
                    "regulation_title": "교직원보수규정",
                    "article_no": "제27조의2",
                    "article_title": "성과연봉 지급대상 제외",
                    "text": "제27조의2(성과연봉 지급대상 제외) 중징계, 성폭력, 성매매, 성희롱, 음주운전 사유는 제외 대상이다.",
                },
                {
                    "regulation_title": "명예퇴직및조기퇴직수당지급세칙",
                    "article_no": "제8조",
                    "article_title": "지급 제한",
                    "text": "중징계 처분을 받은 경우 경영평가 성과급 지급 시 최하위 등급을 부여한다.",
                },
            ],
        )

        self.assertIn("성과연봉 지급대상 제외", answer)
        self.assertIn("중징계", answer)
        self.assertIn("성폭력", answer)
        self.assertIn("성매매", answer)
        self.assertIn("성희롱", answer)
        self.assertIn("음주운전", answer)
        self.assertNotIn("6월 및 12월", answer)
        self.assertNotIn("경영평가 성과급", answer)
        self.assertEqual("제27조의2", supporting[0]["article_no"])

    def test_supporting_results_are_limited_to_used_evidence(self) -> None:
        relevant = {
            "document_id": "doc_real",
            "regulation_title": "인사규정",
            "article_no": "제30조",
            "article_title": "휴직 기간",
            "answer_outline": ["육아휴직은 자녀 1명에 대하여 3년 이내로 신청할 수 있다."],
        }
        unrelated = {
            "document_id": "doc_real",
            "regulation_title": "인사규정",
            "article_no": "제14조",
            "article_title": "경력 평정",
            "text": "경력 평정은 별표 기준에 따른다.",
        }

        supporting = select_supporting_answer_results("육아휴직의 기간은?", [relevant, unrelated])

        self.assertEqual([relevant], supporting)

    def test_childcare_leave_answer_prioritizes_specific_article_30_child_duration(self) -> None:
        answer = build_structured_extractive_answer(
            "육아휴직의 신청 요건과 기간, 수당은?",
            [
                {
                    "article_no": "제30조",
                    "article_title": "휴직 기간",
                    "answer_outline": [
                        "제29조 제2항 제2호 및 제7호의 규정에 의한 휴직 기간은 3년 이내로 한다.",
                        "제29조 제2항 제5호의 규정에 의한 휴직 기간은 3년 이내로 한다.",
                    ],
                    "text": (
                        "제30조(휴직 기간) 휴직 기간은 다음과 같다. "
                        "5. 제29조 제2항 제2호 및 제7호의 규정에 의한 휴직 기간은 3년 이내로 한다. "
                        "9. 제29조 제3항의 규정에 의한 휴직 기간은 자녀 1명에 대하여 3년 이내로 한다. "
                        "다만, 임신·출산·육아를 위한 시간선택제 근무를 하려는 경우에는 그 근무기간과 휴직기간을 합하여 3년을 초과할 수 없다."
                    ),
                },
                {
                    "article_no": "제33조",
                    "article_title": "육아휴직수당",
                    "text": "제33조(육아휴직수당) 30일 이상 휴직한 교직원에게 육아휴직수당을 지급한다.",
                },
            ],
        )

        self.assertIn("제29조 제3항", answer)
        self.assertIn("자녀 1명", answer)

    def test_childcare_leave_answer_keeps_wrapped_numbered_duration_item(self) -> None:
        answer = build_structured_extractive_answer(
            "육아휴직의 신청 요건과 기간, 수당은?",
            [
                {
                    "article_no": "제30조",
                    "article_title": "휴직 기간",
                    "text": (
                        "[본문]\n"
                        "제30조(휴직 기간) 휴직 기간은 다음과 같다.\n"
                        "5. 제29조 제2항 제2호 및 제7호의 규정에 의한 휴직 기간은 3년 이내로 하\n"
                        "되, 부득이한 경우에는 2년의 범위에서 연장할 수 있다. <2016.03.31.>\n"
                        "9. 제29조 제3항의 규정에 의한 휴직 기간은 자녀 1명에 대하여 3년이내로\n"
                        "한다. 다만, 임신·출산·육아를 위한 시간선택제 근무를 하려는 경우에는 그\n"
                        "근무기간과 휴직기간을 합하여 3년을 초과할 수 없다. <2008.04.01.>\n"
                        "[답변분류]\n"
                        "키워드: 휴직 기간, 자녀, 시간선택제\n"
                    ),
                },
                {
                    "article_no": "제33조",
                    "article_title": "육아휴직수당",
                    "text": "제33조(육아휴직수당) 30일 이상 휴직한 교직원에게 육아휴직수당을 지급한다.",
                },
            ],
        )

        self.assertIn("제29조 제3항", answer)
        self.assertIn("자녀 1명", answer)
        self.assertIn("3년 이내", answer)
        self.assertIn("시간선택제", answer)
        self.assertNotIn("키워드:", answer)
        self.assertNotIn("<2008", answer)

    def test_childcare_leave_answer_repairs_wrapped_allowance_sentence(self) -> None:
        answer = build_structured_extractive_answer(
            "육아휴직의 신청 요건과 기간, 수당은?",
            [
                {
                    "article_no": "제33조",
                    "article_title": "육아휴직수당",
                    "text": (
                        "[본문]\n"
                        "제33조(육아휴직수당) ① 인사규정 제29조제3항에 따른 사유로 30일 이상 휴\n"
                        "4-3-1. 교직원보수규정\n"
                        "직한 교직원의 육아휴직수당은 육아휴직 시작일부터 6개월째까지는 육아휴직\n"
                        "시작일을 기준으로 기본연봉월액의 78퍼센트에 해당하는 금액으로 하고, 7개\n"
                        "월째 이후는 육아휴직 시작일을 기준으로 기본연봉월액의 62.4퍼센트에 해당\n"
                        "하는 금액으로 한다. <2018.9.14., 2022.4.4.>\n"
                        "3. 육아휴직 시작일부터 3개월째까지: 250만원 <신설 2025.12.22.>\n"
                    ),
                }
            ],
        )

        self.assertIn("30일 이상 휴직한", answer)
        self.assertIn("기본연봉월액의 78퍼센트", answer)
        self.assertIn("62.4퍼센트", answer)
        self.assertIn("250만원", answer)
        self.assertNotIn("교직원보수규정 직한", answer)
        self.assertNotIn("<2018", answer)

    def test_leave_procedure_answer_excludes_off_topic_audit_sentence(self) -> None:
        leave = {
            "document_id": "doc_real",
            "article_no": "제31조",
            "article_title": "휴직의 운영",
            "text": (
                "제31조(휴직의 운영) 원장은 휴직 사유의 적정성을 판단하기 위하여 증빙 자료를 요구할 수 있다. "
                "휴직 기간 중 그 사유가 소멸된 때에는 30일 이내에 신고하여야 하며 지체 없이 복직을 명하여야 한다."
            ),
        }
        audit = {
            "document_id": "doc_real",
            "article_no": "제32조",
            "article_title": "감사결과의 통보",
            "text": "감사가 종료된 후 60일 이내에 감사결과를 수감부서에 통보하여야 한다.",
        }

        answer = build_structured_extractive_answer("휴직의 종류와 절차는?", [leave, audit])
        supporting = select_supporting_answer_results("휴직의 종류와 절차는?", [leave, audit])

        self.assertIn("휴직 사유", answer)
        self.assertIn("복직", answer)
        self.assertNotIn("감사결과", answer)
        self.assertEqual([leave], supporting)

    def test_return_from_leave_answer_prioritizes_operation_article(self) -> None:
        reasons = {
            "document_id": "doc_real",
            "article_no": "제29조",
            "article_title": "휴직",
            "text": "제29조(휴직) 교직원이 휴직 사유에 해당하면 휴직을 명하여야 한다.",
        }
        duration = {
            "document_id": "doc_real",
            "article_no": "제30조",
            "article_title": "휴직 기간",
            "text": "제30조(휴직 기간) 휴직 기간은 1년 이내로 한다.",
        }
        operation = {
            "document_id": "doc_real",
            "article_no": "제31조",
            "article_title": "휴직의 운영",
            "text": (
                "제31조(휴직의 운영) 휴직 기간 중 그 사유가 소멸된 때에는 30일 이내에 "
                "임용권자에게 이를 신고하여야 하며, 임용권자는 지체 없이 복직을 명하여야 한다. "
                "휴직 기간이 만료된 교직원은 당연히 복직된다."
            ),
        }

        answer = build_structured_extractive_answer("휴직 후 복직 절차는?", [reasons, duration, operation])
        supporting = select_supporting_answer_results("휴직 후 복직 절차는?", [reasons, duration, operation])

        self.assertIn("30일 이내", answer)
        self.assertIn("복직", answer)
        self.assertEqual(operation, supporting[0])

    def test_leave_procedure_answer_prioritizes_article_29_reasons(self) -> None:
        operation = {
            "document_id": "doc_real",
            "article_no": "제31조",
            "article_title": "휴직의 운영",
            "text": "제31조(휴직의 운영) 휴직 사유가 소멸된 때에는 30일 이내에 신고하여야 하며 복직을 명하여야 한다.",
        }
        duration = {
            "document_id": "doc_real",
            "article_no": "제30조",
            "article_title": "휴직 기간",
            "text": "제30조(휴직 기간) 제29조 제1항 제1호의 휴직 기간은 1년 이내로 한다.",
        }
        reasons = {
            "document_id": "doc_real",
            "article_no": "제29조",
            "article_title": "휴직",
            "text": (
                "제29조(휴직) 교직원이 다음 각 호의 어느 하나에 해당할 때에는 임용권자는 "
                "본인의 의사에 불구하고 휴직을 명하여야 한다. 따른 휴직을 신청하는 것으로 본다. 교직원이 휴직을 원하는 경우 "
                "원장은 인사위원회의 심의를 거쳐 휴직을 명할 수 있다."
            ),
        }

        answer = build_structured_extractive_answer("휴직의 종류와 절차는?", [operation, duration, reasons])
        supporting = select_supporting_answer_results("휴직의 종류와 절차는?", [operation, duration, reasons])

        self.assertIn("본인의 의사에 불구하고", answer)
        self.assertNotIn("따른 휴직을 신청", answer)
        self.assertEqual(reasons, supporting[0])

    def test_leave_type_answer_includes_related_numbered_reasons(self) -> None:
        reasons = {
            "document_id": "doc_real",
            "article_no": "제29조",
            "article_title": "휴직",
            "text": (
                "제29조(휴직) ① 교직원이 다음 각 호의 어느 하나에 해당할 때에는 임용권자는 "
                "본인의 의사에 불구하고 휴직을 명하여야 한다. "
                "1. 신체 정신상의 장애로 장기요양을 요할 때 "
                "2. <삭제 1989.08.21. 규정 제288호> "
                "3. 병역법에 의한 병역의무를 필하기 위하여 징집 또는 소집되었을 때 "
                "② 교직원이 다음 각 호의 어느 하나에 해당하는 사유로 휴직을 원하는 경우 "
                "원장은 인사위원회의 심의를 거쳐 휴직을 명할 수 있다. "
                "1. 국제기구, 외국기관, 국가기관에 임시로 채용될 때 "
                "2. 국외 유학을 하게 된 때"
            ),
        }
        operation = {
            "document_id": "doc_real",
            "article_no": "제31조",
            "article_title": "휴직의 운영",
            "text": "휴직 기간 중 그 사유가 소멸된 때에는 30일 이내에 신고하여야 하며 복직을 명하여야 한다.",
        }

        answer = build_structured_extractive_answer("휴직의 종류와 절차는?", [operation, reasons])
        supporting = select_supporting_answer_results("휴직의 종류와 절차는?", [operation, reasons])

        self.assertIn("본인의 의사에 불구하고", answer)
        self.assertIn("① 1. 신체 정신상의 장애", answer)
        self.assertIn("② 1. 국제기구", answer)
        self.assertIn("장기요양", answer)
        self.assertIn("병역의무", answer)
        self.assertIn("국제기구", answer)
        self.assertIn("국외 유학", answer)
        self.assertIn("인사위원회", answer)
        self.assertIn("복직", answer)
        self.assertNotIn("삭제", answer)
        self.assertEqual(reasons, supporting[0])

    def test_faculty_committee_answer_filters_form_fragments(self) -> None:
        form_fragment = {
            "document_id": "doc_real",
            "article_no": "제13조",
            "article_title": "인사위원회 심의",
            "text": "② 1차 평가자는 평가서를 제출하여야 한다. 따라 우선 활용하여야 한다.",
        }
        committee = {
            "document_id": "doc_real",
            "article_no": "제5조",
            "article_title": "임용",
            "text": "강사는 강사임용심사위원회의 심사 및 교원 인사위원회의 심의를 거쳐 원장이 임용한다.",
        }

        answer = build_structured_extractive_answer("교원 인사위원회 심의 대상은?", [form_fragment, committee])
        supporting = select_supporting_answer_results("교원 인사위원회 심의 대상은?", [form_fragment, committee])

        self.assertIn("교원 인사위원회", answer)
        self.assertNotIn("평가서를 제출", answer)
        self.assertNotIn("따라 우선", answer)
        self.assertEqual([committee], supporting)

    def test_faculty_committee_answer_excludes_employee_committee_items(self) -> None:
        committee_function = {
            "document_id": "doc_real",
            "article_no": "제8조",
            "article_title": "위원회 기능",
            "text": (
                "제8조(위원회 기능) ① 교원 인사위원회는 다음 사항을 관장한다. "
                "1. 교원의 신규 채용, 재계약, 승진, 정년보장, 강임에 관한 심의 "
                "2. 교원의 파견 요청에 관한 심의 "
                "3. 퇴직교원 정부포상에 관한 심의 "
                "4. 기타 교원 인사 관련 인사위원회에 부의하도록 규정된 사항의 심의 "
                "② 직원 인사위원회는 다음 사항을 관장한다. 직원의 채용과 승진에 관한 심의"
            ),
        }

        answer = build_structured_extractive_answer("교원 인사위원회 심의 대상은?", [committee_function])

        self.assertIn("교원의 신규 채용", answer)
        self.assertIn("교원의 파견", answer)
        self.assertIn("퇴직교원", answer)
        self.assertNotIn("직원 인사위원회", answer)

    def test_appendix_form_answer_prefers_structure_rule_over_incidental_references(self) -> None:
        payment_reference = {
            "document_id": "doc_real",
            "article_no": "제50조",
            "article_title": "지급근거",
            "text": "제50조(지급근거) 교육훈련여비지급 기준은 별표 7에 따른다.",
        }
        appendix_rule = {
            "document_id": "doc_real",
            "article_no": "제18조",
            "article_title": "별표와 별지 서식",
            "text": (
                "제18조(별표와 별지 서식) 내용이 길거나 복잡한 표, 그림, 계산식 또는 "
                "분량이 너무 많은 사항의 경우 별표로 구분하여 규정할 수 있다. "
                "별지 서식은 일정한 형식에 따라 계속적으로 사용할 필요가 있는 경우 둔다."
            ),
        }
        formatting_example = {
            "document_id": "doc_real",
            "regulation_title": "원규관리규정 시행세칙",
            "text": (
                "2. 별표 또는 별지 서식의 제목 가. 별표의 제목: 별표의 제목에는 밑줄을 긋는다. "
                "1.∨-------------------------------------------------------------------- "
                "∨∨가.∨----------------------------------------------------------------- "
                "구분 ▲▲▲▲▲"
            ),
        }

        answer = build_structured_extractive_answer(
            "별표나 서식 근거가 필요한 경우 어떻게 확인하나?",
            [payment_reference, appendix_rule, formatting_example],
        )

        self.assertIn("표, 그림, 계산식", answer)
        self.assertIn("별지 서식", answer)
        self.assertNotIn("교육훈련여비", answer)
        self.assertNotIn("∨", answer)
        self.assertNotIn("▲", answer)

    def test_research_track_table_query_preserves_specific_appendix_label(self) -> None:
        incomplete_article = {
            "document_id": "doc_aks",
            "regulation_title": "인사규정",
            "article_no": "제14조",
            "article_title": "임용자격기준",
            "text": "제14조(임용자격기준) 교원의 임용자격기준은 별표 2와 같이 하고, 연구직의 임용자격기준은",
        }
        research_track_rule = {
            "document_id": "doc_aks",
            "regulation_title": "연구직임용세칙",
            "article_no": "제5조",
            "article_title": "신규임용 자격기준",
            "text": (
                "제5조(신규임용 자격기준) 연구직 신규임용을 위한 자격기준은 인사규정 중 "
                "[별표 2-1]의 ‘연구직 임용자격 기준표’에 의하되, 특별한 경우를 제외하고는 "
                "박사학위 소지자 임용을 원칙으로 한다."
            ),
        }
        generic_appendix_rule = {
            "document_id": "doc_generic",
            "regulation_title": "원규관리규정 시행세칙",
            "article_no": "제18조",
            "article_title": "별표와 별지 서식",
            "text": "제18조(별표와 별지 서식) 내용이 길거나 복잡한 표는 별표로 구분한다.",
        }

        answer = build_structured_extractive_answer(
            "연구직 임용자격기준표는 별표 2-1로 정하나요?",
            [incomplete_article, generic_appendix_rule, research_track_rule],
        )
        supporting = select_supporting_answer_results(
            "연구직 임용자격기준표는 별표 2-1로 정하나요?",
            [incomplete_article, generic_appendix_rule, research_track_rule],
        )

        self.assertIn("별표 2-1", answer)
        self.assertIn("연구직 임용자격 기준표", answer)
        self.assertIn("특별한 경우를 제외하고", answer)
        self.assertNotIn("내용이 길거나 복잡한 표", answer)
        self.assertIn(research_track_rule, supporting)

    def test_research_track_table_query_keeps_incomplete_governing_article_support(self) -> None:
        governing_article = {
            "document_id": "doc_aks",
            "regulation_title": "인사규정",
            "article_no": "제5조",
            "article_title": "신규임용 자격기준",
            "text": "제5조(신규임용 자격기준) ① 연구직 신규임용을 위한 자격기준은 인사규정 중",
        }
        appendix_sentence = {
            "document_id": "doc_aks",
            "regulation_title": "신규 임용",
            "article_no": "",
            "article_title": "",
            "text": (
                "[별표 2-1]의 ‘연구직 임용자격 기준표’에 의하되, 특별한 경우를 제외하고는 "
                "박사학위 소지자 임용을 원칙으로 한다."
            ),
            "answer_outline": [
                "[별표 2-1]의 ‘연구직 임용자격 기준표’에 의하되, 특별한 경우를 제외하고는 "
                "박사학위 소지자 임용을 원칙으로 한다."
            ],
        }
        unrelated_appendix = {
            "document_id": "doc_generic",
            "regulation_title": "원규관리규정 시행세칙",
            "article_no": "제18조",
            "article_title": "별표와 별지 서식",
            "text": "제18조(별표와 별지 서식) 내용이 길거나 복잡한 표는 별표로 구분한다.",
        }

        query = "연구직 임용자격기준표는 별표 2-1로 정하나요?"
        answer = build_structured_extractive_answer(query, [appendix_sentence, governing_article, unrelated_appendix])
        supporting = select_supporting_answer_results(query, [appendix_sentence, governing_article, unrelated_appendix])

        self.assertIn("별표 2-1", answer)
        self.assertIn("연구직 임용자격 기준표", answer)
        self.assertIn("박사학위 소지자", answer)
        self.assertIn("제5조", answer)
        self.assertIn("신규임용 자격기준", answer)
        self.assertIn(governing_article, supporting)
        self.assertIn(appendix_sentence, supporting)
        self.assertNotIn(unrelated_appendix, supporting)


if __name__ == "__main__":
    unittest.main()
