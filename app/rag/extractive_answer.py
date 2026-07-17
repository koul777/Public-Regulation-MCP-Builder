from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


NO_EVIDENCE_ANSWER = "승인된 규정 근거에서 확인할 수 없습니다."
_ARTICLE_REFERENCE_RE = re.compile(
    r"제\s*\d+\s*조(?:\s*의\s*\d+)?(?:\s*제\s*\d+\s*항)?",
    flags=re.IGNORECASE,
)
_APPENDIX_REFERENCE_RE = re.compile(r"별표\s*\d+(?:\s*-\s*\d+)?", flags=re.IGNORECASE)
_FORM_REFERENCE_RE = re.compile(r"별지\s*(?:제\s*)?\d+\s*호\s*서식", flags=re.IGNORECASE)


def build_structured_extractive_answer(query: str, results: list[dict[str, Any]]) -> str:
    if not results:
        return NO_EVIDENCE_ANSWER
    spec = _answer_spec(query)
    if spec:
        return _intent_answer(query, results, spec)
    return _generic_answer(results)


def select_supporting_answer_results(query: str, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not results:
        return []
    spec = _answer_spec(query)
    if spec:
        evidence = _select_intent_evidence(query, results, spec)
        supporting = _supporting_results_for_spec(evidence, results, spec)
    else:
        evidence = _select_sentence_evidence(results, (), limit=5)
        supporting = _supporting_results(evidence)
    return supporting or results[:5]


@dataclass(frozen=True)
class _AnswerSpec:
    conclusion_terms: tuple[str, ...]
    detail_terms: tuple[str, ...]
    fallback_heading: str
    conclusion_limit: int = 4
    detail_limit: int = 4
    required_terms: tuple[str, ...] = ()
    reject_terms: tuple[str, ...] = ()
    article_title_priority: tuple[str, ...] = ()
    sentence_priority_terms: tuple[str, ...] = ()
    per_result_limit: int = 0
    related_list_terms: tuple[str, ...] = ()
    related_list_limit: int = 0
    result_anchor_terms: tuple[str, ...] = ()
    support_article_nos: tuple[str, ...] = ()
    support_article_titles: tuple[str, ...] = ()
    support_result_terms: tuple[str, ...] = ()
    support_result_limit: int = 0


@dataclass(frozen=True)
class _EvidenceSentence:
    text: str
    result: dict[str, Any]


def _classify_intent(query: str) -> str:
    compact = str(query or "").replace(" ", "")
    if "육아휴직수당" in compact and any(
        term in compact for term in ("적용례", "경과조치", "종전규정", "개정규정", "2025")
    ):
        return "childcare_allowance_transition"
    if "육아휴직" in compact:
        return "childcare_leave"
    if "휴직" in compact and any(term in compact for term in ("종류", "절차", "신청", "복직")):
        return "leave_procedure"
    if "성과연봉" in compact and any(
        term in compact for term in ("제외", "제한", "지급대상", "대상제외", "못받", "미지급", "지급하지", "중징계", "징계")
    ):
        return "performance_pay_exclusion"
    if "성과연봉" in compact:
        return "performance_pay"
    if "신규채용후보자" in compact and "심사절차" in compact:
        return "lecturer_screening_procedure"
    if "재계약절차" in compact and ("의견진술" in compact or "서면제출" in compact):
        return "lecturer_recontract_procedure"
    if "재계약심사" in compact and "70점" in compact:
        return "lecturer_recontract_review"
    if "인사위원회" in compact and "교원" in compact:
        return "faculty_committee"
    if "휴직자" in compact and "국외출국" in compact and "신고서" in compact:
        return "leave_foreign_travel_report"
    # Late-document tables/forms need their own evidence route.  A generic
    # appendix intent can otherwise select nearby article prose even when the
    # exact table/form was retrieved.
    if "교수직임용자격기준표" in compact:
        return "late_table_professor_qualification"
    if "연구직임용자격기준표" in compact and not any(label in compact for label in ("별표2-1", "별표21")):
        return "late_table_researcher_qualification"
    if "연구직경력기간환산율표" in compact:
        return "late_table_career_conversion"
    if "장기요양휴직" in compact and ("월정직책급" in compact or "가족수당" in compact):
        return "late_table_allowance"
    if "기록물보존기간기준표" in compact:
        return "late_table_record_retention"
    if "서류전형평가표" in compact:
        return "late_table_screening_score"
    if "채용비리처리기준" in compact:
        return "late_table_recruitment_corruption"
    if "계정과목" in compact:
        return "late_table_account_title"
    if "겸직자명부" in compact:
        return "late_table_dual_position"
    if "기록물평가심의서" in compact:
        return "late_table_record_review_form"
    if any(term in compact for term in ("별표", "별지", "서식")):
        return "appendix_or_form"
    if "교원" in compact and any(term in compact for term in ("채용", "임용", "절차")):
        return "faculty_hiring"
    return "generic"


def _answer_spec(query: str) -> _AnswerSpec | None:
    intent = _classify_intent(query)
    if intent == "childcare_allowance_transition":
        return _AnswerSpec(
            conclusion_terms=("적용례", "경과조치", "2025년 1월 1일", "종전 규정", "감액", "개정규정"),
            detail_terms=(),
            fallback_heading="육아휴직수당 적용례",
            conclusion_limit=4,
            detail_limit=0,
            required_terms=("육아휴직수당",),
            article_title_priority=(
                "육아휴직수당 지급에 관한 적용례",
                "육아휴직수당 지급 방법에 관한 경과조치",
                "육아휴직수당에 관한 적용례",
                "육아휴직수당 및 육아기 근무시간 단축수당의 지급에 관한 특례",
                "육아휴직수당",
            ),
            result_anchor_terms=("2025년 1월 1일", "종전 규정", "감액"),
            sentence_priority_terms=(
                "육아휴직수당 지급에 관한 적용례",
                "2025년 1월 1일",
                "종전 규정",
                "감액",
                "개정규정",
            ),
            per_result_limit=3,
        )
    if intent == "childcare_leave":
        return _AnswerSpec(
            conclusion_terms=(
                "만 8세",
                "초등학교 2학년",
                "자녀",
                "3년",
                "육아휴직",
                "시간선택제",
                "30일 이상",
                "기본연봉월액",
                "78퍼센트",
                "62.4퍼센트",
                "250만원",
                "200만원",
                "160만원",
            ),
            detail_terms=("휴직 중", "증빙", "30일", "복직", "신고", "수당", "지급기간", "18개월"),
            fallback_heading="육아휴직",
            conclusion_limit=7,
            detail_limit=0,
            required_terms=("육아휴직", "휴직", "수당"),
            reject_terms=("제29조 제1항", "제29조 제2항"),
            article_title_priority=("휴직", "휴직 기간", "육아휴직수당", "휴직의 운영"),
            sentence_priority_terms=(
                "제29조 제3항",
                "자녀 1명",
                "시간선택제",
                "만 8세",
                "초등학교 2학년",
                "육아휴직수당",
                "기본연봉월액",
                "78퍼센트",
                "62.4퍼센트",
                "250만원",
                "200만원",
                "160만원",
            ),
            per_result_limit=5,
        )
    if intent == "leave_foreign_travel_report":
        return _AnswerSpec(
            conclusion_terms=(
                "제29조의3",
                "휴직자의 복무실태 점검",
                "별지 제16호서식",
                "휴직자 국외 출국 신고서",
                "국외로 출국",
                "출국 7일 전",
                "14일 이하",
                "영유아",
            ),
            detail_terms=(),
            fallback_heading="휴직자 국외 출국 신고서",
            conclusion_limit=5,
            detail_limit=0,
            required_terms=("휴직",),
            article_title_priority=("휴직자의 복무실태 점검",),
            sentence_priority_terms=(
                "제29조의3",
                "휴직자의 복무실태 점검",
                "출국 7일 전",
                "별지 제16호서식",
                "14일 이하",
                "영유아",
            ),
            per_result_limit=2,
        )
    if intent == "leave_procedure":
        compact = str(query or "").replace(" ", "")
        if "복직" in compact:
            return _AnswerSpec(
                conclusion_terms=("복직", "신고", "30일", "만료", "휴직 사유"),
                detail_terms=("증빙", "직무", "종사", "원직"),
                fallback_heading="휴직 후 복직 절차",
                conclusion_limit=3,
                detail_limit=0,
                required_terms=("복직", "신고", "휴직"),
                article_title_priority=("휴직의 운영", "휴직", "휴직 기간", "휴직자의 복무실태 점검"),
                sentence_priority_terms=("복직", "30일", "신고", "만료", "휴직 사유", "증빙"),
                per_result_limit=3,
            )
        return _AnswerSpec(
            conclusion_terms=("휴직", "신청", "명하여야", "사유", "기간", "복직", "신고", "증빙"),
            detail_terms=(),
            fallback_heading="휴직 종류와 절차",
            conclusion_limit=6,
            detail_limit=0,
            required_terms=("휴직", "복직"),
            article_title_priority=("휴직", "휴직의 운영", "휴직 기간", "휴직자의 복무실태 점검"),
            sentence_priority_terms=("본인의 의사에 불구하고", "휴직을 원하는 경우", "인사위원회", "복직", "30일", "증빙"),
            per_result_limit=2,
            related_list_terms=(
                "장기요양",
                "병역",
                "천재지변",
                "생사",
                "소재",
                "법률",
                "국제기구",
                "외국기관",
                "국가기관",
                "대학",
                "연구기관",
                "유학",
                "연수",
                "정무직",
                "공공기관",
                "질병",
                "부상",
                "부모",
                "배우자",
                "간호",
                "외국에서 근무",
            ),
            related_list_limit=12,
        )
    if intent == "performance_pay":
        return _AnswerSpec(
            conclusion_terms=("성과연봉", "6월", "12월", "일시금", "지급"),
            detail_terms=(),
            fallback_heading="성과연봉 지급",
            detail_limit=0,
            required_terms=("성과연봉", "연봉"),
            sentence_priority_terms=("성과연봉은 이등분", "6월", "12월", "일시금", "기본연봉", "퇴직", "연봉 조정"),
            per_result_limit=4,
        )
    if intent == "lecturer_screening_procedure":
        return _AnswerSpec(
            conclusion_terms=("신규채용후보자", "심사절차", "서류심사", "면접심사"),
            detail_terms=(),
            fallback_heading="신규채용후보자 심사절차",
            conclusion_limit=5,
            detail_limit=0,
            required_terms=("신규채용후보자", "심사"),
            article_title_priority=("심사절차",),
            sentence_priority_terms=("신규채용후보자", "서류심사", "면접심사", "심사절차"),
            per_result_limit=5,
        )
    if intent == "lecturer_recontract_procedure":
        return _AnswerSpec(
            conclusion_terms=("재계약 절차", "의견을 진술", "서면으로 의견", "교원소청심사위원회", "30일"),
            detail_terms=(),
            fallback_heading="재계약 절차",
            conclusion_limit=5,
            detail_limit=0,
            required_terms=("재계약",),
            article_title_priority=("재계약 절차",),
            sentence_priority_terms=("재계약 절차", "의견을 진술", "서면으로 의견", "교원소청심사위원회", "30일"),
            per_result_limit=5,
        )
    if intent == "lecturer_recontract_review":
        return _AnswerSpec(
            conclusion_terms=("재계약 심사", "70점", "교원 인사위원회", "심의대상자"),
            detail_terms=(),
            fallback_heading="재계약 심사",
            conclusion_limit=5,
            detail_limit=0,
            required_terms=("재계약", "심사"),
            article_title_priority=("재계약 심사",),
            sentence_priority_terms=("재계약 심사", "70점", "교원 인사위원회", "심의대상자"),
            per_result_limit=5,
        )
    if intent == "performance_pay_exclusion":
        return _AnswerSpec(
            conclusion_terms=(
                "성과연봉 지급대상 제외",
                "지급 대상에서 제외",
                "중징계",
                "징계",
                "시효가 5년",
                "비위",
                "성폭력",
                "성매매",
                "성희롱",
                "음주운전",
                "음주측정",
            ),
            detail_terms=(),
            fallback_heading="성과연봉 지급대상 제외",
            conclusion_limit=7,
            detail_limit=0,
            required_terms=(
                "성과연봉",
                "제외",
                "중징계",
                "징계",
                "비위",
                "성폭력",
                "성매매",
                "성희롱",
                "음주운전",
                "음주측정",
            ),
            reject_terms=("6월", "12월", "일시금", "이등분", "기본연봉월액"),
            article_title_priority=("성과연봉 지급대상 제외",),
            result_anchor_terms=("제27조의2", "성과연봉 지급대상 제외"),
            sentence_priority_terms=(
                "성과연봉 지급대상 제외",
                "지급 대상에서 제외",
                "중징계",
                "시효가 5년",
                "성폭력",
                "성매매",
                "성희롱",
                "음주운전",
                "음주측정",
            ),
            per_result_limit=7,
        )
    if intent == "faculty_hiring":
        return _AnswerSpec(
            conclusion_terms=(
                "전임 교원",
                "교원의 임용",
                "15일",
                "공고",
                "신규채용",
                "기초심사",
                "연구실적심사",
                "공개발표심사",
                "면접심사",
            ),
            detail_terms=(),
            fallback_heading="교원 채용 절차",
            conclusion_limit=6,
            detail_limit=0,
            required_terms=("교원", "임용", "채용", "심사", "공고"),
            reject_terms=("강사를 신규채용", "강사임용", "비전임교원", "연구직임용세칙"),
            article_title_priority=("교원", "신규임용의 시기", "신규임용 후보자 심사", "면접심사위원회", "기초심사"),
            sentence_priority_terms=(
                "전임 교원",
                "교원의 임용",
                "지원 마감일",
                "15일 이상",
                "임용분야",
                "지원자격",
                "기초심사",
                "연구실적심사",
                "공개발표심사",
                "면접심사",
                "평균 80점",
            ),
            per_result_limit=5,
        )
    if intent == "faculty_committee":
        return _AnswerSpec(
            conclusion_terms=(
                "교원 인사위원회",
                "인사위원회",
                "심의",
                "신규 채용",
                "승진",
                "정년보장",
                "강임",
                "파견",
                "퇴직교원",
            ),
            detail_terms=("심의", "위원회", "원장", "임용", "강사"),
            fallback_heading="교원 인사위원회 심의",
            conclusion_limit=5,
            detail_limit=0,
            required_terms=("인사위원회", "교원"),
            reject_terms=("직원 인사위원회",),
            article_title_priority=("위원회 기능", "임용", "신규임용", "인사위원회 심의"),
            sentence_priority_terms=(
                "교원 인사위원회는 다음 사항",
                "신규 채용",
                "재계약",
                "승진",
                "정년보장",
                "강임",
                "파견",
                "퇴직교원",
                "기타 교원 인사",
            ),
            per_result_limit=5,
        )
    if intent == "late_table_professor_qualification":
        return _AnswerSpec(
            conclusion_terms=("교수직 임용 자격 기준표", "교수", "부교수", "조교수", "연구경력", "논문"),
            detail_terms=(),
            fallback_heading="교수직 임용 자격 기준표",
            conclusion_limit=6,
            required_terms=("교수직", "임용", "자격"),
            result_anchor_terms=("교수직 임용 자격 기준표",),
            sentence_priority_terms=("교수직 임용 자격 기준표", "교수", "부교수", "조교수", "연구경력", "논문"),
            per_result_limit=8,
            support_result_terms=("교수직 임용 자격 기준표",),
            support_result_limit=1,
        )
    if intent == "late_table_researcher_qualification":
        return _AnswerSpec(
            conclusion_terms=("연구직 임용자격 기준표", "수석연구원", "책임연구원", "선임연구원", "정연구원", "자격"),
            detail_terms=(),
            fallback_heading="연구직 임용자격 기준표",
            conclusion_limit=7,
            required_terms=("연구직", "임용자격"),
            result_anchor_terms=("연구직 임용자격 기준표",),
            sentence_priority_terms=("연구직 임용자격 기준표", "수석연구원", "책임연구원", "선임연구원", "정연구원"),
            per_result_limit=8,
            support_result_terms=("연구직 임용자격 기준표",),
            support_result_limit=1,
        )
    if intent == "late_table_career_conversion":
        return _AnswerSpec(
            conclusion_terms=("연구직 경력기간 환산율표", "대학", "연구기관", "각종 회사", "동일", "비동일", "100%", "80%", "50%"),
            detail_terms=(),
            fallback_heading="연구직 경력기간 환산율표",
            conclusion_limit=8,
            required_terms=("연구직", "경력기간", "환산"),
            result_anchor_terms=("연구직 경력기간 환산율표",),
            sentence_priority_terms=("연구직 경력기간 환산율표", "대학", "연구기관", "각종 회사", "100%", "80%", "50%"),
            per_result_limit=8,
            support_result_terms=("연구직 경력기간 환산율표",),
            support_result_limit=1,
        )
    if intent == "late_table_allowance":
        return _AnswerSpec(
            conclusion_terms=("기타수당 지급 기준표", "장기요양휴직", "월정직책급", "가족수당", "미지급", "70퍼센트 지급"),
            detail_terms=(),
            fallback_heading="기타수당 지급 기준표",
            conclusion_limit=6,
            required_terms=("장기요양휴직",),
            result_anchor_terms=("기타수당 지급 기준표",),
            sentence_priority_terms=("기타수당 지급 기준표", "장기요양휴직", "월정직책급", "가족수당", "미지급", "70퍼센트"),
            per_result_limit=8,
            support_result_terms=("기타수당 지급 기준표",),
            support_result_limit=1,
        )
    if intent == "late_table_record_retention":
        return _AnswerSpec(
            conclusion_terms=("기록물보존기간 기준표", "영구 보존", "법령", "규정", "교직원 채용관계 문서", "인사기록카드"),
            detail_terms=(),
            fallback_heading="기록물보존기간 기준표",
            conclusion_limit=7,
            required_terms=("기록물", "보존기간"),
            result_anchor_terms=("기록물보존기간 기준표",),
            sentence_priority_terms=("기록물보존기간 기준표", "영구 보존", "법령", "규정", "교직원 채용관계 문서", "인사기록카드"),
            per_result_limit=8,
            support_result_terms=("기록물보존기간 기준표",),
            support_result_limit=1,
        )
    if intent == "late_table_screening_score":
        return _AnswerSpec(
            conclusion_terms=("서류전형 평가표", "응시 요건의 적합성", "직무수행 요건의 적합성", "조직", "25점", "35점", "20점", "80점 미만", "불합격"),
            detail_terms=(),
            fallback_heading="서류전형 평가표",
            conclusion_limit=9,
            required_terms=("서류전형", "평가"),
            result_anchor_terms=("서류전형 평가표",),
            sentence_priority_terms=("서류전형 평가표", "응시 요건의 적합성", "직무수행 요건의 적합성", "25점", "35점", "80점 미만"),
            per_result_limit=10,
            support_result_terms=("서류전형 평가표",),
            support_result_limit=1,
        )
    if intent == "late_table_recruitment_corruption":
        return _AnswerSpec(
            conclusion_terms=("채용 비리 처리 기준", "응시·자격 요건 미확인", "전형 단계별 점수 부여 부적정", "경징계", "중징계", "주의·경고"),
            detail_terms=(),
            fallback_heading="채용 비리 처리 기준",
            conclusion_limit=7,
            required_terms=("채용", "비리", "처리"),
            result_anchor_terms=("채용 비리 처리 기준",),
            sentence_priority_terms=("채용 비리 처리 기준", "응시·자격 요건 미확인", "전형 단계별 점수 부여 부적정", "경징계", "중징계", "주의·경고"),
            per_result_limit=8,
            support_result_terms=("채용 비리 처리 기준",),
            support_result_limit=1,
        )
    if intent == "late_table_account_title":
        return _AnswerSpec(
            conclusion_terms=("계 정 과 목", "계정과목", "관 항 목 해 설", "현 금", "보유현금", "정기예금", "토 지", "업무용토지", "감가상각누계액"),
            detail_terms=(),
            fallback_heading="계정과목 표",
            conclusion_limit=9,
            required_terms=("계", "과목"),
            result_anchor_terms=("관 항 목 해 설",),
            sentence_priority_terms=("계 정 과 목", "관 항 목 해 설", "현 금", "보유현금", "정기예금", "토 지", "업무용토지", "감가상각누계액"),
            per_result_limit=12,
            support_result_terms=("관 항 목 해 설",),
            support_result_limit=1,
        )
    if intent == "late_table_dual_position":
        return _AnswerSpec(
            conclusion_terms=("겸 직 자 명 부", "겸직자 명부", "번호", "소 속", "직 위", "성 명", "기 간", "겸직기관", "겸직직위", "비 고"),
            detail_terms=(),
            fallback_heading="겸직자 명부",
            conclusion_limit=10,
            required_terms=("겸직",),
            result_anchor_terms=("겸 직 자 명 부",),
            sentence_priority_terms=("겸 직 자 명 부", "겸직자 명부", "번호", "소 속", "직 위", "성 명", "기 간", "겸직기관", "겸직직위", "비 고"),
            per_result_limit=10,
            support_result_terms=("겸 직 자 명 부",),
            support_result_limit=1,
        )
    if intent == "late_table_record_review_form":
        return _AnswerSpec(
            conclusion_terms=("기록물평가심의서", "기록물철 분류번호", "생산 연도", "기록물철 제 목", "보존기간", "보존기간 만료일", "심의회 의 견", "처리과", "기록물관리담당자"),
            detail_terms=(),
            fallback_heading="기록물평가심의서",
            conclusion_limit=9,
            required_terms=("기록물",),
            result_anchor_terms=("기록물평가심의서",),
            sentence_priority_terms=("기록물평가심의서", "기록물철 분류번호", "생산 연도", "기록물철 제 목", "보존기간", "보존기간 만료일", "심의회 의 견"),
            per_result_limit=10,
            support_result_terms=("기록물평가심의서",),
            support_result_limit=1,
        )
    if intent == "appendix_or_form":
        compact = str(query or "").replace(" ", "")
        if "연구직" in compact and "임용자격" in compact and "별표" in compact:
            return _AnswerSpec(
                conclusion_terms=(
                    "별표 2-1",
                    "별표2-1",
                    "연구직 임용자격 기준표",
                    "연구직 임용자격기준표",
                    "특별한 경우를 제외하고",
                    "박사학위",
                ),
                detail_terms=(),
                fallback_heading="연구직 임용자격 기준표",
                conclusion_limit=4,
                detail_limit=0,
                required_terms=("연구직", "임용자격", "별표"),
                reject_terms=(
                    "연구실적물의 인정범위",
                    "연구실적심사",
                    "내용이 길거나 복잡한 표",
                    "별표와 별지 서식",
                    "원규관리규정",
                ),
                article_title_priority=("임용자격기준", "신규임용 자격기준"),
                sentence_priority_terms=(
                    "연구직의 임용자격기준은 별표 2-1",
                    "[별표 2-1]의",
                    "연구직 임용자격 기준표",
                    "특별한 경우를 제외하고",
                    "박사학위 소지자",
                ),
                per_result_limit=2,
                support_article_nos=("제5조",),
                support_article_titles=("신규임용 자격기준",),
                support_result_terms=("연구직", "신규임용", "자격기준"),
                support_result_limit=1,
            )
        return _AnswerSpec(
            conclusion_terms=(
                "별표",
                "별지",
                "서식",
                "표, 그림, 계산식",
                "일정한 형식",
                "작성방식",
                "일부개정",
                "전부개정",
            ),
            detail_terms=(),
            fallback_heading="별표와 서식 근거",
            conclusion_limit=3,
            detail_limit=0,
            required_terms=("별표", "별지", "서식"),
            reject_terms=("지급근거", "손망실", "개인정보", "가스안전", "삭제하고자", "기호표시"),
            article_title_priority=("별표와 별지 서식",),
            sentence_priority_terms=(
                "내용이 길거나 복잡한 표",
                "별지 서식",
                "일정한 형식",
                "인용할 경우",
                "본칙에 부수되는 별표",
                "별표 또는 별지 서식",
                "일부개정",
                "전부개정",
            ),
            per_result_limit=4,
        )
    return None


def _intent_answer(query: str, results: list[dict[str, Any]], spec: _AnswerSpec) -> str:
    conclusion_evidence, detail_evidence = _select_intent_evidence_parts(query, results, spec)
    if not conclusion_evidence:
        conclusion_evidence = _select_sentence_evidence(
            _rank_results_for_spec(results, spec),
            (),
            limit=2,
            required_terms=spec.required_terms,
            reject_terms=spec.reject_terms,
            priority_terms=spec.sentence_priority_terms,
            per_result_limit=spec.per_result_limit,
        )
    lines = ["승인된 규정 근거 기준입니다.", "", "핵심 답변"]
    related_list_evidence = (
        [item for item in conclusion_evidence if _numbered_list_sentence(item.text)]
        if spec.related_list_limit > 0
        else []
    )
    primary_conclusion_evidence = (
        [item for item in conclusion_evidence if not _numbered_list_sentence(item.text)]
        if related_list_evidence
        else conclusion_evidence
    )
    if primary_conclusion_evidence:
        lines.extend(f"- {item.text}" for item in primary_conclusion_evidence)
    else:
        lines.append(f"- {spec.fallback_heading} 관련 직접 근거는 검색됐지만 요약 가능한 문장이 충분하지 않습니다.")
    if related_list_evidence:
        lines.extend(["", "휴직 사유(각 호)"])
        lines.extend(f"- {item.text}" for item in related_list_evidence)
    if detail_evidence:
        lines.extend(["", "세부 근거"])
        lines.extend(f"- {item.text}" for item in detail_evidence)
    lines.extend(["", "근거 조항"])
    supporting_results = _supporting_results_for_spec([*conclusion_evidence, *detail_evidence], results, spec)
    lines.extend(f"- {citation}" for citation in _citations(supporting_results or results[:5]))
    return "\n".join(lines)


def _generic_answer(results: list[dict[str, Any]]) -> str:
    evidence = _select_sentence_evidence(results, (), limit=5)
    lines = ["승인된 규정 근거 기준입니다.", "", "확인된 내용"]
    lines.extend(f"- {item.text}" for item in evidence)
    lines.extend(["", "근거 조항"])
    supporting_results = _supporting_results(evidence)
    lines.extend(f"- {citation}" for citation in _citations(supporting_results or results[:5]))
    return "\n".join(lines)


def _select_intent_evidence(
    query: str,
    results: list[dict[str, Any]],
    spec: _AnswerSpec,
) -> list[_EvidenceSentence]:
    conclusion, detail = _select_intent_evidence_parts(query, results, spec)
    return [*conclusion, *detail]


def _select_intent_evidence_parts(
    query: str,
    results: list[dict[str, Any]],
    spec: _AnswerSpec,
) -> tuple[list[_EvidenceSentence], list[_EvidenceSentence]]:
    ranked_results = _rank_results_for_spec(results, spec)
    anchored_results = _filter_results_by_anchor_terms(ranked_results, spec.result_anchor_terms)
    if anchored_results:
        ranked_results = anchored_results
    conclusion_evidence = _select_query_reference_evidence(
        query,
        ranked_results,
        limit=min(2, spec.conclusion_limit),
    )
    remaining_conclusion_limit = max(0, spec.conclusion_limit - len(conclusion_evidence))
    if remaining_conclusion_limit:
        conclusion_evidence.extend(
            _select_sentence_evidence(
                ranked_results,
                spec.conclusion_terms,
                limit=remaining_conclusion_limit,
                exclude={item.text for item in conclusion_evidence},
                required_terms=spec.required_terms,
                reject_terms=spec.reject_terms,
                priority_terms=spec.sentence_priority_terms,
                per_result_limit=spec.per_result_limit,
            )
        )
    related_list_evidence = _select_related_list_item_evidence(
        ranked_results,
        terms=spec.related_list_terms,
        limit=spec.related_list_limit,
        selected=conclusion_evidence,
    )
    if related_list_evidence:
        conclusion_evidence.extend(related_list_evidence)
    detail_evidence = _select_sentence_evidence(
        ranked_results,
        spec.detail_terms,
        limit=spec.detail_limit,
        exclude={item.text for item in conclusion_evidence},
        required_terms=spec.required_terms,
        reject_terms=spec.reject_terms,
        priority_terms=spec.sentence_priority_terms,
        per_result_limit=spec.per_result_limit,
    )
    return conclusion_evidence, detail_evidence


def _select_related_list_item_evidence(
    results: list[dict[str, Any]],
    *,
    terms: tuple[str, ...],
    limit: int,
    selected: list[_EvidenceSentence],
) -> list[_EvidenceSentence]:
    if limit <= 0 or not terms:
        return []
    evidence: list[_EvidenceSentence] = []
    for result in results:
        if not _selected_result_has_list_anchor(result, selected):
            continue
        candidates = _numbered_list_items_with_paragraph_context(str(result.get("text") or ""))
        if not candidates:
            candidates = [sentence for sentence in _candidate_sentences(result) if _numbered_list_sentence(sentence)]
        for sentence in candidates:
            if not _numbered_list_sentence(sentence):
                continue
            if not any(term in sentence for term in terms):
                continue
            if _duplicate_sentence(sentence, [*selected, *evidence], set()):
                continue
            evidence.append(_EvidenceSentence(text=sentence, result=result))
            if len(evidence) >= limit:
                return evidence
    return evidence


def _selected_result_has_list_anchor(result: dict[str, Any], selected: list[_EvidenceSentence]) -> bool:
    return any(
        item.result is result and ("각 호" in item.text or _numbered_list_sentence(item.text)) for item in selected
    )


def _numbered_list_sentence(sentence: str) -> bool:
    return bool(
        re.match(r"^(?:[①-⑳]\s*)?\d{1,2}\.\s*[「『\"'“‘(]?[가-힣A-Za-z0-9]", str(sentence or "").strip())
    )


def _rank_results_for_spec(results: list[dict[str, Any]], spec: _AnswerSpec) -> list[dict[str, Any]]:
    if not spec.article_title_priority:
        return results
    priority = {title: index for index, title in enumerate(spec.article_title_priority)}

    def key(item: tuple[int, dict[str, Any]]) -> tuple[int, int]:
        original_index, result = item
        title = str(result.get("article_title") or "").strip()
        return (priority.get(title, len(priority)), original_index)

    return [result for _, result in sorted(enumerate(results), key=key)]


def _filter_results_by_anchor_terms(results: list[dict[str, Any]], terms: tuple[str, ...]) -> list[dict[str, Any]]:
    if not terms:
        return []
    return [result for result in results if any(term in _result_blob(result) for term in terms)]


def _result_blob(result: dict[str, Any]) -> str:
    return " ".join(
        str(value or "")
        for value in (
            result.get("text"),
            result.get("regulation_title"),
            result.get("article_no"),
            result.get("article_title"),
            result.get("title"),
            result.get("document_name"),
        )
    )


def _select_query_reference_evidence(
    query: str,
    results: list[dict[str, Any]],
    *,
    limit: int,
) -> list[_EvidenceSentence]:
    if limit <= 0:
        return []
    query_labels = _appendix_form_references(query)
    if not query_labels:
        return []
    query_articles = _article_references(query)
    aligned: list[tuple[int, int, dict[str, Any]]] = []
    for index, result in enumerate(results):
        result_labels = _result_appendix_form_references(result)
        label_matches = query_labels & result_labels
        if not label_matches:
            continue
        result_articles = _result_article_references(result)
        article_matches = query_articles & result_articles
        if query_articles and not article_matches:
            continue
        score = (len(label_matches) * 10) + (len(article_matches) * 5)
        aligned.append((-score, index, result))

    evidence: list[_EvidenceSentence] = []
    for _, _, result in sorted(aligned):
        sentence = _reference_summary_sentence(query, result)
        if not sentence or _duplicate_sentence(sentence, evidence, set()):
            continue
        evidence.append(_EvidenceSentence(text=sentence, result=result))
        if len(evidence) >= limit:
            break
    return evidence


def _reference_summary_sentence(query: str, result: dict[str, Any]) -> str:
    query_labels = _appendix_form_references(query)
    query_articles = _article_references(query)
    labels = [
        str(value or "").strip()
        for value in [*(result.get("appendix_refs") or []), *(result.get("form_refs") or [])]
        if _normalize_reference(value) in query_labels
    ]
    articles = [
        str(value or "").strip()
        for value in result.get("article_refs") or []
        if _normalize_reference(value) in query_articles
    ]
    if not labels:
        return ""
    heading = labels[0]
    if articles:
        heading += f" ({articles[0]} 관련)"
    body = _reference_summary_body(result)
    if not body:
        return f"{heading}의 승인된 구조화 근거입니다."
    return f"{heading}: {body}"


def _reference_summary_body(result: dict[str, Any]) -> str:
    for value in result.get("answer_outline") or []:
        candidate = _clean_reference_summary(value)
        if candidate:
            return candidate
    for fact in result.get("answer_facts") or []:
        if not isinstance(fact, dict):
            continue
        candidate = _clean_reference_summary(fact.get("sentence") or fact.get("value"))
        if candidate:
            return candidate
    text = str(result.get("text") or "")
    body = text.split("[본문]", 1)[-1]
    body = re.split(r"\[(?:표|답변분류|참조)\]", body, maxsplit=1)[0]
    for line in body.splitlines():
        candidate = _clean_reference_summary(line)
        if candidate and not candidate.startswith(("[별표", "[별지", "<별표", "<별지")):
            return candidate
    return ""


def _clean_reference_summary(value: Any) -> str:
    candidate = " ".join(str(value or "").split())
    candidate = re.sub(r"^[-•·\s]+", "", candidate)
    if len(candidate) < 12 or _metadata_like_sentence(candidate):
        return ""
    if len(candidate) <= 280:
        return candidate
    cutoff = candidate.rfind(" ", 0, 280)
    if cutoff < 180:
        cutoff = 280
    return candidate[:cutoff].rstrip(" ,;:") + "…"


def _result_appendix_form_references(result: dict[str, Any]) -> set[str]:
    values = [
        *(result.get("appendix_refs") or []),
        *(result.get("form_refs") or []),
        result.get("table_citation_label"),
    ]
    return {
        normalized
        for value in values
        for normalized in _appendix_form_references(str(value or ""))
    }


def _result_article_references(result: dict[str, Any]) -> set[str]:
    values = [
        result.get("direct_article_no"),
        result.get("article_no"),
        result.get("governing_article_no"),
        *(result.get("article_refs") or []),
    ]
    return {
        normalized
        for value in values
        if (normalized := _normalize_reference(value))
    }


def _article_references(value: str) -> set[str]:
    return {
        normalized
        for match in _ARTICLE_REFERENCE_RE.finditer(str(value or ""))
        if (normalized := _normalize_reference(match.group(0)))
    }


def _appendix_form_references(value: str) -> set[str]:
    text = str(value or "")
    return {
        normalized
        for pattern in (_APPENDIX_REFERENCE_RE, _FORM_REFERENCE_RE)
        for match in pattern.finditer(text)
        if (normalized := _normalize_reference(match.group(0)))
    }


def _normalize_reference(value: Any) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", str(value or "")).lower()


def _select_sentence_evidence(
    results: list[dict[str, Any]],
    terms: tuple[str, ...],
    *,
    limit: int,
    exclude: set[str] | None = None,
    required_terms: tuple[str, ...] = (),
    reject_terms: tuple[str, ...] = (),
    priority_terms: tuple[str, ...] = (),
    per_result_limit: int = 0,
) -> list[_EvidenceSentence]:
    if limit <= 0:
        return []
    excluded = exclude or set()
    selected: list[_EvidenceSentence] = []
    result_counts: dict[int, int] = {}
    for result in results:
        result_id = id(result)
        for sentence in _rank_candidate_sentences(_candidate_sentences(result), priority_terms):
            if per_result_limit > 0 and result_counts.get(result_id, 0) >= per_result_limit:
                break
            if _duplicate_sentence(sentence, selected, excluded):
                continue
            if terms and not any(term in sentence for term in terms):
                continue
            if required_terms and not any(term in sentence for term in required_terms):
                continue
            if reject_terms and any(term in sentence for term in reject_terms):
                continue
            selected.append(_EvidenceSentence(text=sentence, result=result))
            result_counts[result_id] = result_counts.get(result_id, 0) + 1
            if len(selected) >= limit:
                return selected
    return selected


def _rank_candidate_sentences(sentences: list[str], priority_terms: tuple[str, ...]) -> list[str]:
    if not priority_terms:
        return sentences
    weighted_terms = {term: len(priority_terms) - index for index, term in enumerate(priority_terms)}

    def key(item: tuple[int, str]) -> tuple[int, int]:
        index, sentence = item
        score = sum(weight for term, weight in weighted_terms.items() if term and term in sentence)
        return (-score, index)

    return [sentence for _, sentence in sorted(enumerate(sentences), key=key)]


def _supporting_results(evidence: list[_EvidenceSentence]) -> list[dict[str, Any]]:
    supporting: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in evidence:
        identity = id(item.result)
        if identity in seen:
            continue
        seen.add(identity)
        supporting.append(item.result)
    return supporting


def _supporting_results_for_spec(
    evidence: list[_EvidenceSentence],
    results: list[dict[str, Any]],
    spec: _AnswerSpec,
) -> list[dict[str, Any]]:
    supporting = _supporting_results(evidence)
    if spec.support_result_limit <= 0:
        return supporting
    added = 0
    for result in _rank_results_for_spec(results, spec):
        if any(result is item for item in supporting):
            continue
        if not _matches_support_result(result, spec):
            continue
        supporting.insert(0, result)
        added += 1
        if added >= spec.support_result_limit:
            break
    return supporting


def _matches_support_result(result: dict[str, Any], spec: _AnswerSpec) -> bool:
    if spec.support_article_nos and str(result.get("article_no") or "").strip() not in spec.support_article_nos:
        return False
    if spec.support_article_titles and str(result.get("article_title") or "").strip() not in spec.support_article_titles:
        return False
    blob = _result_blob(result)
    return all(term in blob for term in spec.support_result_terms)


def _duplicate_sentence(sentence: str, selected: list[_EvidenceSentence], excluded: set[str]) -> bool:
    if sentence in excluded or any(item.text == sentence for item in selected):
        return True
    existing_sentences = [*excluded, *(item.text for item in selected)]
    key = _sentence_dedupe_key(sentence)
    for existing in existing_sentences:
        if key and key == _sentence_dedupe_key(existing):
            return True
        if len(sentence) >= 20 and sentence in existing:
            return True
        if len(existing) >= 20 and existing in sentence:
            return True
    return False


def _sentence_dedupe_key(sentence: str) -> str:
    normalized = re.sub(r"<[^>]+>", "", sentence or "")
    normalized = re.sub(r"^\d+\.\s*", "", normalized)
    normalized = re.sub(r"\s*:\s*", ":", normalized).strip()
    compact = normalized.replace(" ", "")
    if "지원마감일전까지15일이상" in compact and "공고" in compact:
        return "faculty-hiring-public-notice"
    if "신규임용후보자" in compact and "단계별" in compact and "심사" in compact:
        return "faculty-hiring-review-stages"
    if ":" in normalized:
        return normalized.split(":", 1)[0].replace(" ", "")
    if len(normalized) <= 20 and any(term in normalized for term in ("심사", "공고", "지급", "휴직")):
        return normalized.replace(" ", "")
    return ""


def _candidate_sentences(result: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    if _is_table_like_result(result):
        for sentence in _table_row_candidates(str(result.get("text") or "")):
            if sentence not in candidates:
                candidates.append(sentence)
    for value in result.get("answer_outline") or []:
        sentence = _clean_sentence(str(value or ""))
        if not sentence and _is_table_like_result(result):
            sentence = _clean_table_candidate(value)
        if sentence and sentence not in candidates:
            candidates.append(sentence)
    for fact in result.get("answer_facts") or []:
        if not isinstance(fact, dict):
            continue
        sentence = _clean_sentence(str(fact.get("sentence") or fact.get("value") or ""))
        if not sentence and _is_table_like_result(result):
            sentence = _clean_table_candidate(fact.get("sentence") or fact.get("value") or "")
        if sentence and sentence not in candidates:
            candidates.append(sentence)
    for sentence in _sentences(str(result.get("text") or "")):
        if sentence not in candidates:
            candidates.append(sentence)
    return candidates


def _is_table_like_result(result: dict[str, Any]) -> bool:
    return bool(
        result.get("table_like")
        or result.get("table_classification")
        or result.get("table_source")
        or str(result.get("chunk_type") or "").strip().lower() in {"appendix", "form"}
    )


def _table_row_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for raw_line in _clean_text(text).splitlines():
        sentence = _clean_table_candidate(raw_line)
        if sentence and sentence not in candidates:
            candidates.append(sentence)
    return candidates


def _clean_table_candidate(value: Any) -> str:
    sentence = " ".join(str(value or "").split())
    sentence = re.sub(r"\s*<[^>]{1,120}>", "", sentence)
    sentence = re.sub(r"^\[(?:문서명|위치|본문)\]\s*", "", sentence)
    sentence = re.sub(r"^\d+(?:-\d+)+\.\s*[^\n]{0,80}(?:규정|세칙|지침|요강|규칙)\s*$", "", sentence)
    if not sentence or len(sentence) < 2 or not re.search(r"[가-힣]", sentence):
        return ""
    if sentence.startswith(("[문서명]", "[위치]", "[본문]")):
        return ""
    # OCR table cells frequently split short labels into individual syllables.
    # Repair only stable labels so ordinary prose spacing is preserved.
    for source, target in (
        ("교 수", "교수"),
        ("부 교 수", "부교수"),
        ("조 교 수", "조교수"),
        ("현 금", "현금"),
        ("토 지", "토지"),
        ("소 속", "소속"),
        ("직 위", "직위"),
        ("성 명", "성명"),
        ("기 간", "기간"),
        ("비 고", "비고"),
        ("계 정 과 목", "계정과목"),
        ("기록물철 제 목", "기록물철 제목"),
        ("심의회 의 견", "심의회 의견"),
    ):
        sentence = sentence.replace(source, target)
    return sentence.strip()


_SPACED_NUMERIC_DATE_PATTERN = re.compile(r"(\d{4})\.\s+(\d{1,2})\.\s+(\d{1,2})\.")


def _merge_spaced_numeric_dates(text: str) -> str:
    """Collapse spaces inside official Korean dates ("YYYY. M. D.").

    The sentence splitter treats digit-period-space as a list/sentence
    boundary, so a spaced date is cut at its month and day and its year is
    dropped.  Removing the intra-date spaces keeps the date and its clause
    together.  The 4-digit year anchor leaves genuine list markers untouched.
    """

    return _SPACED_NUMERIC_DATE_PATTERN.sub(r"\1.\2.\3.", text)


def _sentences(text: str) -> list[str]:
    cleaned = _merge_spaced_numeric_dates(_clean_text(text))
    if not cleaned:
        return []
    raw_parts = re.split(r"(?:(?<=[.?!。])\s+|\n+|(?=\d+\.\s)|(?=제\d+조(?:의\d+)?\s*\())", cleaned)
    sentences: list[str] = []
    for part in raw_parts:
        sentence = _clean_sentence(part)
        if not sentence or sentence in sentences:
            continue
        if sentence.startswith("[문서명]") or sentence.startswith("[위치]") or sentence.startswith("[본문]"):
            continue
        sentences.append(sentence)
    for sentence in _numbered_list_items(cleaned):
        if sentence not in sentences:
            sentences.append(sentence)
    return sentences


def _numbered_list_items(text: str) -> list[str]:
    items: list[str] = []
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return items
    marker_pattern = re.compile(r"(?:(?<=^)|(?<=\s))\d{1,2}\.\s+")
    paragraph_pattern = re.compile(r"\s[①-⑳]\s")
    markers = list(marker_pattern.finditer(normalized))
    for index, marker in enumerate(markers):
        end_candidates = [markers[index + 1].start()] if index + 1 < len(markers) else [len(normalized)]
        paragraph = paragraph_pattern.search(normalized, marker.end())
        if paragraph:
            end_candidates.append(paragraph.start())
        end = min(end_candidates)
        sentence = _clean_sentence(normalized[marker.start() : end])
        if sentence and sentence not in items:
            items.append(sentence)
    return items


def _numbered_list_items_with_paragraph_context(text: str) -> list[str]:
    items: list[str] = []
    normalized = " ".join(_clean_text(text).split())
    if not normalized:
        return items
    marker_pattern = re.compile(r"(?:(?<=^)|(?<=\s))\d{1,2}\.\s+")
    paragraph_pattern = re.compile(r"(?:(?<=^)|(?<=\s))([①-⑳])\s")
    markers = list(marker_pattern.finditer(normalized))
    paragraph_markers = list(paragraph_pattern.finditer(normalized))
    for index, marker in enumerate(markers):
        end_candidates = [markers[index + 1].start()] if index + 1 < len(markers) else [len(normalized)]
        next_paragraph = next((item for item in paragraph_markers if item.start() > marker.start()), None)
        if next_paragraph:
            end_candidates.append(next_paragraph.start())
        end = min(end_candidates)
        sentence = _clean_sentence(normalized[marker.start() : end])
        if not sentence:
            continue
        current_paragraph = _current_paragraph_marker(paragraph_markers, marker.start())
        if current_paragraph and not sentence.startswith(current_paragraph):
            sentence = f"{current_paragraph} {sentence}"
        if sentence not in items:
            items.append(sentence)
    return items


def _current_paragraph_marker(markers: list[re.Match[str]], position: int) -> str:
    current = ""
    for marker in markers:
        if marker.start() >= position:
            break
        current = marker.group(1)
    return current


def _clean_text(text: str) -> str:
    text = str(text or "")
    text = re.sub(r"\[(?:표|답변분류)\].*$", "", text, flags=re.DOTALL)
    text = text.replace("[본문]", "\n")
    text = re.sub(r"\n\d+(?:-\d+)+\.\s*[^\n]{1,80}(?:규정|세칙|지침|요강|규칙)\s*\n", "\n", text)
    text = re.sub(r"(?<=[가-힣A-Za-z0-9])\s*\n\s*(?=[가-힣A-Za-z0-9])", " ", text)
    return text


def _clean_sentence(sentence: str) -> str:
    sentence = " ".join(str(sentence or "").split())
    sentence = re.sub(r"\s*<[^>]{1,120}>", "", sentence)
    sentence = re.sub(r"\b(20\d)\s+(\d년)", r"\1\2", sentence)
    sentence = sentence.replace("일 시금", "일시금").replace("정 산", "정산")
    sentence = sentence.replace("하 되", "하되").replace("경 우", "경우")
    sentence = sentence.replace("교 직원", "교직원").replace("재직기 간", "재직기간")
    sentence = sentence.replace("휴 직한", "휴직한").replace("7개 월째", "7개월째")
    sentence = sentence.replace("해당 하는", "해당하는").replace("70 만원", "70만원")
    sentence = sentence.replace("임신또는", "임신 또는").replace("3년이내", "3년 이내")
    sentence = sentence.replace("분 량", "분량").replace("필요 가", "필요가")
    sentence = sentence.replace("대괄호( )", "대괄호([ ])")
    sentence = sentence.replace("필요 하거나", "필요하거나").replace("없는한", "없는 한")
    sentence = sentence.replace("종사 하지", "종사하지")
    sentence = sentence.replace("불 명하게", "불명하게").replace("인 정하는", "인정하는")
    sentence = sentence.replace("연구또는", "연구 또는").replace("배 우자의", "배우자의")
    sentence = sentence.replace("규 정", "규정").replace("하 며", "하며")
    sentence = sentence.replace("다 음", "다음").replace("음주운 전", "음주운전")
    sentence = re.sub(r"([①-⑳])(?=[가-힣A-Za-z0-9])", r"\1 ", sentence)
    sentence = re.sub(r"([가-힣])\s+(으로|로|에|에서|에게|부터|까지|보다|처럼|만큼|와|과|를|을|은|는|도|의)\b", r"\1\2", sentence)
    sentence = re.sub(r"^[-•·\s]+", "", sentence)
    sentence = re.sub(r"^<[^>]+>\s*", "", sentence)
    if _metadata_like_sentence(sentence) or _fragment_like_sentence(sentence):
        return ""
    return sentence.strip()


def _metadata_like_sentence(sentence: str) -> bool:
    normalized = sentence.strip()
    if not normalized:
        return True
    metadata_prefixes = (
        "키워드:",
        "문서명:",
        "문서:",
        "위치:",
        "보안등급:",
        "청크:",
        "chunk:",
        "keywords:",
        "duration:",
        "payment:",
        "procedure:",
        "procedure_step:",
        "eligibility:",
        "exception:",
        "condition:",
        "source:",
        "의도:",
        "intent:",
        "obligation:",
        "prohibition:",
        "reference:",
        "definition:",
        "scope:",
    )
    if normalized.lower().startswith(metadata_prefixes):
        return True
    if normalized.startswith("[") and "]" in normalized[:20]:
        return not _meaningful_bracket_label(normalized)
    return False


def _meaningful_bracket_label(sentence: str) -> bool:
    label = sentence[1 : sentence.find("]")].strip()
    compact = label.replace(" ", "")
    return compact.startswith(("별표", "별지")) or "서식" in compact


def _fragment_like_sentence(sentence: str) -> bool:
    normalized = sentence.strip()
    if not normalized:
        return True
    if normalized.count("∨") >= 2 or normalized.count("▲") >= 3:
        return True
    if len(normalized) > 300 and re.search(r"[-─]{10,}", normalized):
        return True
    if re.match(r"^\d+\.\s*[「『\"'“‘(]?[가-힣A-Za-z0-9]", normalized) and len(normalized) >= 5:
        return False
    if re.fullmatch(r"제\d+조(?:의\d+)?\([^)]+\)", normalized):
        return True
    if re.search(r"\b\d{2,3}$", normalized):
        return True
    if re.search(r"제\d+조(?:의\d+)?$", normalized):
        return True
    if re.search(r"(규정|세칙|지침|요강|규칙)\s+직한", normalized):
        return True
    if normalized.endswith(("원칙으", "이루어지지", "지급하되", "심사")) and ":" not in normalized:
        return True
    if normalized.startswith(("으로 ", "으로", "및 ", "등 ", "관한 ", "게 ", "거쳐 ", "따라 ", "따른 ", "하여 ")):
        return True
    if len(normalized) < 12 and ":" not in normalized:
        return True
    has_sentence_finish = normalized.endswith((".", "다.", "다", "요.", "함.", "음.")) or "다.<" in normalized
    if ":" not in normalized and not has_sentence_finish and not re.search(r"[가-힣]+ 등(?:<[^>]+>)?$", normalized):
        return True
    return False


def _citations(results: list[dict[str, Any]]) -> list[str]:
    citations: list[str] = []
    for result in results:
        label = _reference_citation_label(result)
        if not label:
            label = " ".join(
                str(value)
                for value in (result.get("regulation_title"), result.get("article_no"), result.get("article_title"))
                if value
            )
        if not label:
            label = str(result.get("document_name") or result.get("document_id") or "근거")
        page = _page_label(result)
        approval = str(result.get("approval_id") or "").strip()
        citation = label
        if page:
            citation += f", {page}"
        if approval:
            citation += f", approval={approval}"
        if citation not in citations:
            citations.append(citation)
    return citations


def _reference_citation_label(result: dict[str, Any]) -> str:
    if str(result.get("chunk_type") or "") not in {"appendix", "form", "table"}:
        return ""
    references = [
        str(value or "").strip()
        for value in [*(result.get("appendix_refs") or []), *(result.get("form_refs") or [])]
        if str(value or "").strip()
    ]
    if not references:
        return ""
    parts = [str(result.get("regulation_title") or "").strip(), references[0]]
    article_refs = [str(value or "").strip() for value in result.get("article_refs") or [] if str(value or "").strip()]
    if article_refs:
        parts.append(f"({article_refs[0]} 관련)")
    return " ".join(part for part in parts if part)


def _page_label(result: dict[str, Any]) -> str:
    start = result.get("source_page_start")
    end = result.get("source_page_end")
    if start and end and start != end:
        return f"p.{start}-{end}"
    if start:
        return f"p.{start}"
    return ""
