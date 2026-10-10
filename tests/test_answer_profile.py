from __future__ import annotations

import hashlib
import json
import unittest

from app.processors.answer_profile import (
    _procedure_steps,
    _sentences,
    build_answer_profile,
    clean_answer_profile_text,
)


class AnswerProfileProcedureStepsTests(unittest.TestCase):
    def test_procedure_step_containing_a_digit_is_not_dropped(self) -> None:
        sentence = (
            "채용 절차는 다음과 같다. "
            "① 서류심사는 접수 마감일부터 5일 이내에 실시한다 "
            "② 면접은 서류합격자를 대상으로 한다"
        )

        steps = _procedure_steps(sentence)

        self.assertIn("서류심사는 접수 마감일부터 5일 이내에 실시한다", steps)
        self.assertIn("면접은 서류합격자를 대상으로 한다", steps)

    def test_sentences_preserve_korean_spaced_effective_date(self) -> None:
        # "YYYY. M. D." spaced dates must not be shredded into digit fragments
        # when the answer profile splits sentences at ingestion time.
        sentences = _sentences("이 규정은 2025. 1. 1.부터 시행한다.")

        self.assertIn("이 규정은 2025.1.1.부터 시행한다.", sentences)
        self.assertNotIn("1.부터 시행한다.", sentences)


# Outputs recorded from the implementation that compiled its regular expressions
# on every call.  Precompiling the patterns must not change a single character.
GOLDEN_PROFILE_CASES = [
 {
  "text": "[위치] 한국공공가상연구원 총규정 > preamble Preamble\n[본문]\n한국공공가상연구원 총규정\n(2026년 9월 30일 현재)",
  "metadata": {
   "regulation_title": "한국공공가상연구원 총규정",
   "article_title": "",
   "paragraph_label": ""
  },
  "sha256": "2c1cfaf58777aa61b544f598f835fc78439ce22f5d0a86af5b8e45c6036c32b5",
  "profile": {
   "answer_profile_version": "reg-rag-answer-profile-v1",
   "answer_intents": [
    "duration"
   ],
   "answer_keywords": [
    "한국공공가상연구원 총규정",
    "duration"
   ],
   "answer_facts": [],
   "answer_outline": [
    "한국공공가상연구원 총규정 > preamble Preamble 한국공공가상연구원 총규정 (2026년 9월 30일 현재)"
   ]
  }
 },
 {
  "text": "[위치] 인사규정 > 제2장 채용 > 제1절 공개채용 및 경력채용 > 제6조 공개채용의 심의\n[본문]\n제6조(공개채용의 심의)\n① 인사 담당 부서의 장은 공개채용에 관한 중요 사항에 대하여 필요하다고 인정하는 경우 인사위원회의 심의를 거칠 수 있다.\n② 인사위원회는 재적위원 과반수의 출석과 출석위원 과반수의 찬성으로 의결한다.",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "공개채용의 심의",
   "paragraph_label": ""
  },
  "sha256": "2c37108843b25e870fcf4480e5d660831b3bddf6a5f4620bfd526f08d8d56781",
  "profile": {
   "answer_profile_version": "reg-rag-answer-profile-v1",
   "answer_intents": [
    "procedure"
   ],
   "answer_keywords": [
    "인사규정",
    "공개채용의 심의",
    "procedure",
    "채용",
    "공개채용",
    "경력채용",
    "공개채용의",
    "공개채용에",
    "인사위원회의",
    "인사위원회는"
   ],
   "answer_facts": [
    {
     "type": "procedure_step",
     "value": "제6조(공개채용의 심의)",
     "sentence": "제6조(공개채용의 심의)"
    },
    {
     "type": "procedure_step",
     "value": "인사 담당 부서의 장은 공개채용에 관한 중요 사항에 대하여 필요하다고 인정하는 경우 인사위원회의 심의를 거칠 수 있다.",
     "sentence": "① 인사 담당 부서의 장은 공개채용에 관한 중요 사항에 대하여 필요하다고 인정하는 경우 인사위원회의 심의를 거칠 수 있다."
    },
    {
     "type": "condition",
     "value": "① 인사 담당 부서의 장은 공개채용에 관한 중요 사항에 대하여 필요하다고 인정하는 경우 인사위원회의 심의를 거칠 수 있다.",
     "sentence": "① 인사 담당 부서의 장은 공개채용에 관한 중요 사항에 대하여 필요하다고 인정하는 경우 인사위원회의 심의를 거칠 수 있다."
    }
   ],
   "answer_outline": [
    "제6조(공개채용의 심의)",
    "① 인사 담당 부서의 장은 공개채용에 관한 중요 사항에 대하여 필요하다고 인정하는 경우 인사위원회의 심의를 거칠 수 있다.",
    "② 인사위원회는 재적위원 과반수의 출석과 출석위원 과반수의 찬성으로 의결한다.",
    "인사규정 > 제2장 채용 > 제1절 공개채용 및 경력채용 > 제6조 공개채용의 심의"
   ]
  }
 },
 {
  "text": "[위치] 인사규정 > 제2장 채용 > 제1절 공개채용 및 경력채용 > 제12조 계약직 채용\n[본문]\n제12조(계약직 채용)\n① 인사 담당 부서의 장은 계약직 채용에 관한 업무를 총괄하며, 필요한 경우 관계 부서에 협조를 요청할 수 있다.\n② 직원은 계약직 채용에 관한 사항을 인사 담당 부서의 장에게 신청하거나 신고할 수 있다.",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "계약직 채용",
   "paragraph_label": ""
  },
  "sha256": "6b17e15463c926e7c87820d84370654b2d237ac84cc3fc4bdef40530e2041ef9",
  "profile": {
   "answer_profile_version": "reg-rag-answer-profile-v1",
   "answer_intents": [
    "procedure",
    "obligation"
   ],
   "answer_keywords": [
    "인사규정",
    "계약직 채용",
    "procedure",
    "obligation",
    "채용",
    "공개채용",
    "경력채용",
    "채용에"
   ],
   "answer_facts": [
    {
     "type": "procedure_step",
     "value": "제12조(계약직 채용)",
     "sentence": "제12조(계약직 채용)"
    },
    {
     "type": "procedure_step",
     "value": "인사 담당 부서의 장은 계약직 채용에 관한 업무를 총괄하며, 필요한 경우 관계 부서에 협조를 요청할 수 있다.",
     "sentence": "① 인사 담당 부서의 장은 계약직 채용에 관한 업무를 총괄하며, 필요한 경우 관계 부서에 협조를 요청할 수 있다."
    },
    {
     "type": "condition",
     "value": "① 인사 담당 부서의 장은 계약직 채용에 관한 업무를 총괄하며, 필요한 경우 관계 부서에 협조를 요청할 수 있다.",
     "sentence": "① 인사 담당 부서의 장은 계약직 채용에 관한 업무를 총괄하며, 필요한 경우 관계 부서에 협조를 요청할 수 있다."
    },
    {
     "type": "procedure_step",
     "value": "직원은 계약직 채용에 관한 사항을 인사 담당 부서의 장에게 신청하거나 신고할 수 있다.",
     "sentence": "② 직원은 계약직 채용에 관한 사항을 인사 담당 부서의 장에게 신청하거나 신고할 수 있다."
    },
    {
     "type": "obligation",
     "value": "② 직원은 계약직 채용에 관한 사항을 인사 담당 부서의 장에게 신청하거나 신고할 수 있다.",
     "sentence": "② 직원은 계약직 채용에 관한 사항을 인사 담당 부서의 장에게 신청하거나 신고할 수 있다."
    }
   ],
   "answer_outline": [
    "① 인사 담당 부서의 장은 계약직 채용에 관한 업무를 총괄하며, 필요한 경우 관계 부서에 협조를 요청할 수 있다.",
    "② 직원은 계약직 채용에 관한 사항을 인사 담당 부서의 장에게 신청하거나 신고할 수 있다.",
    "제12조(계약직 채용)",
    "인사규정 > 제2장 채용 > 제1절 공개채용 및 경력채용 > 제12조 계약직 채용"
   ]
  }
 },
 {
  "text": "[위치] 인사규정 > 제2장 채용 > 제2절 연구직 채용 및 채용 후보자 등록 > 제19조 삭제\n[본문]\n제19조 삭제 <2016. 7. 1.>",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "삭제",
   "paragraph_label": ""
  },
  "sha256": "725833cab6dd9aeb670f09869fbc8645003a48cfbf1733db197ead48b7c817a4"
 },
 {
  "text": "[위치] 인사규정 > 제3장 임용 > 제1절 신규 임용 및 시보 임용 > 제26조 신규 임용의 특례\n[본문]\n제26조(신규 임용의 특례)\n① 원장은 업무상 불가피한 사유가 있는 경우에는 제25조제2항에도 불구하고 신규 임용에 관하여 별도의 기준을 정하여 적용할 수 있다.\n② 제1항에 따라 별도의 기준을 적용한 경우에는 그 사유와 내용을 인사위원회에 보고하여야 한다.",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "신규 임용의 특례",
   "paragraph_label": ""
  },
  "sha256": "d5b751690839449df81f81feded6b1a231c7e858c11e8cd2f5041c4a3f2463a8"
 },
 {
  "text": "[위치] 인사규정 > 제3장 임용 > 제1절 신규 임용 및 시보 임용 > 제33조 승진 임용\n[본문]\n제33조(승진 임용) 원장은 승진 임용에 관한 업무를 효율적으로 수행하기 위하여 필요한 경우 인사위원회의 의견을 들을 수 있다.",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "승진 임용",
   "paragraph_label": ""
  },
  "sha256": "bddfb13ce60778c0ff0c99854b38f0379da9b678a04851c114c7ef8c527fb3d5"
 },
 {
  "text": "[위치] 인사규정 > 제3장 임용 > 제2절 전보 임용 및 파견 근무 > 제38조 겸임의 기준\n[본문]\n제38조(겸임의 기준)\n① 겸임에 관한 기준은 다음 각 호와 같다.\n1. 사후 관리 기준은 다음 각 목과 같다\n가. 처리 결과가 기록되어 있을 것\n나. 관련 서류가 보존기간 동안 보관되고 있을 것\n2. 절차 기준은 다음 각 목과 같다\n가. 신청서와 증빙서류가 갖추어져 있을 것\n나. 소관 부서장의 의견이 첨부되어 있을 것\n다. 신청 기한을 지켰을 것\n② 원장은\n제1항 각 호의 기준을 업무 여건에 따라 조정할 수 있다. 이 경우 인사위원회의 의견을 들어야 한다. <개정 2016. 5. 1.>",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "겸임의 기준",
   "paragraph_label": ""
  },
  "sha256": "b8c92f69f55a9f54aa26c6fff4986d95d3359570deb6d2f5fd21485ff5638971"
 },
 {
  "text": "[위치] 인사규정 > 제3장 임용 > 제3절 직위 부여 및 임용 제청 > 제45조 임용 제청의 현황 보고\n[본문]\n제45조(임용 제청의 현황 보고) 인사 담당 부서의 장은 매년 2월 말까지 임용 제청에 관한 운영 현황을 원장에게 보고하여야 한다.",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "임용 제청의 현황 보고",
   "paragraph_label": ""
  },
  "sha256": "497490d599dc801069474e4b2505dc008e6d337133a7e2312fc37de19fd46919"
 },
 {
  "text": "[위치] 인사규정 > 제4장 승진 > 제1절 승진 심사 및 승진 후보자 명부 > 제51조 삭제\n[본문]\n제51조 삭제 <2016. 7. 1.>",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "삭제",
   "paragraph_label": ""
  },
  "sha256": "6145b3d9d296379af4367aff073065a31a30cccb44a5f4fdb6b3ebda75b00bbb"
 },
 {
  "text": "[위치] 인사규정 > 제4장 승진 > 제2절 특별 승진 및 승진 제한 > 제58조 특별 승진의 통보\n[본문]\n제58조(특별 승진의 통보) 인사 담당 부서의 장은 특별 승진에 관한 처리 결과를 직원에게 10일 이내에 서면 또는 전자적 방법으로 통보하여야 한다. <개정 2017. 3. 1.>",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "특별 승진의 통보",
   "paragraph_label": ""
  },
  "sha256": "16e05ec41822df0b7499c2fefc5833531836e23d95591def3ea70b952d582e6d"
 },
 {
  "text": "[위치] 인사규정 > 제5장 인사위원회 > 제64조 인사위원회 구성\n[본문]\n제64조(인사위원회 구성)\n① 인사 담당 부서의 장은 인사위원회 구성에 관한 업무를 총괄하며, 필요한 경우 관계 부서에 협조를 요청할 수 있다. <개정 2016. 7. 1., 2017. 3. 1.>\n② 직원은 인사위원회 구성에 관한 사항을 인사 담당 부서의 장에게 신청하거나 신고할 수 있다.",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "인사위원회 구성",
   "paragraph_label": ""
  },
  "sha256": "05e0890ce7149475787bfabbf5fdf51fb92a7099a9126798e52e9d5e3ca06752"
 },
 {
  "text": "[위치] 인사규정 > 제5장 인사위원회 > 제70조 위원 제척에 대한 이의신청\n[본문]\n제70조(위원 제척에 대한 이의신청)\n① 위원 제척에 관한 처리 결과에 이의가 있는 사람은 통보를 받은 날부터 14일 이내에 인사 담당 부서의 장에게 서면으로 재검토를 신청할 수 있다.\n② 인사 담당 부서의 장은 제1항에 따른 신청을 받은 날부터 10일 이내에 재검토 결과를 신청인에게 통보하여야 한다. <개정 2016. 7. 30., 2017. 3. 1.>\n③ 제69조제2항에 따라 처리한 위원 제척에 관한 사항은 인사 담당 부서의 장이 기록하여 관리한다.",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "위원 제척에 대한 이의신청",
   "paragraph_label": ""
  },
  "sha256": "c1e5f0bfb1deacc158561a5ab797cc5dbbf5068ae22903c2fcac0be9269b08ba"
 },
 {
  "text": "[위치] 인사규정 > 제6장 휴직 > 제1절 질병 휴직 및 육아 휴직 > 제77조 질병 휴직에 따른 환수\n[본문]\n제77조(질병 휴직에 따른 환수)\n① 거짓이나 그 밖의 부정한 방법으로 질병 휴직에 관한 혜택을 받은 사람에 대하여는 인사 담당 부서의 장은 그 혜택을 환수할 수 있다. <개정 2016. 7. 30., 2021. 6. 1.>\n② 인사 담당 부서의 장은 제1항에 따라 환수하는 경우 환수 금액의 납부 기한을 30일 이내로 정하여 통보한다.",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "질병 휴직에 따른 환수",
   "paragraph_label": ""
  },
  "sha256": "92fdf4a4afec586e05b53228a03cdbad2dc49608461bc9f63d033f381d6dfbc7"
 },
 {
  "text": "[위치] 인사규정 > 제6장 휴직 > 제1절 질병 휴직 및 육아 휴직 > 제84조 가족돌봄 휴직에 따른 환수\n[본문]\n제84조(가족돌봄 휴직에 따른 환수)\n① 거짓이나 그 밖의 부정한 방법으로 가족돌봄 휴직에 관한 혜택을 받은 사람에 대하여는 인사 담당 부서의 장은 그 혜택을 환수할 수 있다.\n② 인사 담당 부서의 장은 제1항에 따라 환수하는 경우 환수 금액의 납부 기한을 14일 이내로 정하여 통보한다.",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "가족돌봄 휴직에 따른 환수",
   "paragraph_label": ""
  },
  "sha256": "a07cf9e1f1afb7811cb06e5f69826f06e21006df4e71f505efc93179abaec87a"
 },
 {
  "text": "[위치] 인사규정 > 제6장 휴직 > 제2절 공무상 휴직 및 휴직 복귀 > 제91조 공무상 휴직의 심의\n[본문]\n제91조(공무상 휴직의 심의) 공무상 휴직에 관한 사항 중 원장이 심의가 필요하다고 정한 사항은 인사위원회의 심의를 거쳐야 한다.",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "공무상 휴직의 심의",
   "paragraph_label": ""
  },
  "sha256": "5106c71e895cd8a6ffc1706fb8bd6f91f921a1f5cf4b74fab74e2b382cd62f28"
 },
 {
  "text": "[위치] 인사규정 > 제6장 휴직 > 제2절 공무상 휴직 및 휴직 복귀 > 제97조의2 휴직 기간 산정에 관한 비밀 유지\n[본문]\n제97조의2(휴직 기간 산정에 관한 비밀 유지)\n① 휴직 기간 산정에 관한 업무를 처리하면서 알게 된 비밀은 누설하거나 목적 외의 용도로 사용하여서는 아니 된다.\n② 제55조제1항에도 불구하고 원장이 인정하는 경우에는 그러하지 아니하다. <신설 2018. 10. 31.>",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "휴직 기간 산정에 관한 비밀 유지",
   "paragraph_label": ""
  },
  "sha256": "7a6a4e04a17d7313cf32cf550c283470ad42154ce750ca15dc61bfa495d240dd"
 },
 {
  "text": "[위치] 인사규정 > 제7장 직위해제 > 제103조 삭제\n[본문]\n제103조 삭제 <2019. 11. 25.>",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "삭제",
   "paragraph_label": ""
  },
  "sha256": "6b2cf704713bf64c7127765cccf98ecfe5ebe0499195bd8e5c733a4539f0c799"
 },
 {
  "text": "[위치] 인사규정 > 제8장 퇴직 > 제1절 정년 퇴직 및 명예 퇴직 > 제110조 명예 퇴직\n[본문]\n제110조(명예 퇴직)\n① 인사 담당 부서의 장은 명예 퇴직에 관한 업무를 총괄하며, 필요한 경우 관계 부서에 협조를 요청할 수 있다.\n② 직원은 명예 퇴직에 관한 사항을 인사 담당 부서의 장에게 신청하거나 신고할 수 있다. <개정 2020. 6. 1., 2025. 4. 1.>",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "명예 퇴직",
   "paragraph_label": ""
  },
  "sha256": "4be81995a6c9af05025dec6fea78a1d6d5c6428668cb85e070aaed7dfe80f763"
 },
 {
  "text": "[위치] 인사규정 > 제8장 퇴직 > 제2절 당연 퇴직 및 퇴직 예정자 관리 > 제116조 당연 퇴직에 관한 비밀 유지\n[본문]\n제116조(당연 퇴직에 관한 비밀 유지) 당연 퇴직에 관한 업무를 처리하면서 알게 된 비밀은 누설하거나 목적 외의 용도로 사용하여서는 아니 된다.",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "당연 퇴직에 관한 비밀 유지",
   "paragraph_label": ""
  },
  "sha256": "ed59d352b2c2cac71a4712dd4671b663519d5ddbd38f0ddbd9afeec95727beab"
 },
 {
  "text": "[위치] 인사규정 > 제8장 퇴직 > 제2절 당연 퇴직 및 퇴직 예정자 관리 > 제123조 사직서 처리의 통보\n[본문]\n제123조(사직서 처리의 통보) 인사 담당 부서의 장은 사직서 처리에 관한 처리 결과를 직원에게 14일 이내에 서면 또는 전자적 방법으로 통보하여야 한다. <개정 2021. 11. 15.>",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "사직서 처리의 통보",
   "paragraph_label": ""
  },
  "sha256": "d6dde75cf00967a63787f54328dd348cacd896a5bf966e3c1a142413ba26dff2"
 },
 {
  "text": "[위치] 인사규정 > 제9장 징계 > 제128조 징계 사유에 관한 기록의 보존\n[본문]\n제128조(징계 사유에 관한 기록의 보존)\n① 징계 사유에 관한 서류는 1년간 보존하여야 하며, 보존기간이 지난 서류는 인사 담당 부서의 장의 승인을 받아 폐기한다.\n② 징계 사유에 관하여 제125조의3에서 정한 사항을 제외하고는 이 조의 규정을 적용한다. <개정 2017. 3. 1.>",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "징계 사유에 관한 기록의 보존",
   "paragraph_label": ""
  },
  "sha256": "455ae8668cf75c0a429119e59e3704afa7d2df05d286e908b51287024a4e2350"
 },
 {
  "text": "[위치] 인사규정 > 제9장 징계 > 제135조 징계 효력에 대한 이의신청\n[본문]\n제135조(징계 효력에 대한 이의신청)\n① 징계 효력에 관한 처리 결과에 이의가 있는 사람은 통보를 받은 날부터 10일 이내에 인사 담당 부서의 장에게 서면으로 재검토를 신청할 수 있다.\n② 인사 담당 부서의 장은 제1항에 따른 신청을 받은 날부터 7일 이내에 재검토 결과를 신청인에게 통보하여야 한다.",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "징계 효력에 대한 이의신청",
   "paragraph_label": ""
  },
  "sha256": "de38b68334cd0e6bdadd0ccd3e25a53b5868fd7ab8a90df9db1e36c4c9b03957"
 },
 {
  "text": "[위치] 인사규정 > 제10장 인사기록 > 제141조 인사기록 열람의 기준\n[본문]\n제141조(인사기록 열람의 기준)\n① 인사기록 열람에 관한 기준은 다음 각 호와 같다. <개정 2016. 5. 1., 2022. 10. 28.>\n1. 일반 기준은 다음 각 목과 같다\n가. 관련 법령과 연구원의 규정에 부합할 것\n나. 예산의 범위에서 집행이 가능할 것\n2. 절차 기준은 다음 각 목과 같다\n가. 신청서와 증빙서류가 갖추어져 있을 것\n나. 신청 기한을 지켰을 것\n② 원장은\n제1항 각 호의 기준을 업무 여건에 따라 조정할 수 있다. 이 경우 인사위원회의 의견을 들어야 한다.",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "인사기록 열람의 기준",
   "paragraph_label": ""
  },
  "sha256": "3c5ec640ca1a8d7e38c62304532359a640c24fe05cb9b4ec0d4217da5048092b"
 },
 {
  "text": "[위치] 인사규정 > 제11장 보칙 > 제147조 세부 사항의 위임\n[본문]\n제147조(세부 사항의 위임) 이 규정의 시행에 필요한 세부 사항은 원장이 따로 정한다.",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "세부 사항의 위임",
   "paragraph_label": ""
  },
  "sha256": "5e35b79ad3536286f1c727ed7e70879a26ab501183641647743ebb0235210ff2"
 },
 {
  "text": "[위치] 인사규정 > 부칙 <2016. 5. 1.> > 제2조 경과조치\n[본문]\n제2조(경과조치) 이 규정 시행 당시 종전의 규정에 따라 진행 중인 겸임에 관한 사항은 종전의 규정에 따른다.",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "경과조치",
   "paragraph_label": ""
  },
  "sha256": "dfc19be358bf1c3afe05a79d54147484e7103c922273d02c4a3581c6ad76b20e"
 },
 {
  "text": "[위치] 인사규정 > 부칙 <2016. 7. 30.> > 제2조 적용례\n[본문]\n제2조(적용례) 제16조의 개정규정은 이 규정 시행 후 최초로 신청하는 분부터 적용한다.",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "적용례",
   "paragraph_label": ""
  },
  "sha256": "3b62ae311a3286993e0f2d88b63227a1b6858abd7a11b7cc0ed3a81071ee64b6"
 },
 {
  "text": "[위치] 인사규정 > 부칙 <2021. 6. 1.> > 제1조 시행일\n[본문]\n제1조(시행일) 이 규정은 2021년 6월 1일부터 시행한다.",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "시행일",
   "paragraph_label": ""
  },
  "sha256": "c79ef9c00419e2d85740732636c9daddfd23cd60e959c7fedda346974846f0bd"
 },
 {
  "text": "[위치] 인사규정 > 부칙 <2022. 10. 28.> > 제2조 경과조치\n[본문]\n제2조(경과조치) 이 규정 시행 당시 종전의 규정에 따라 진행 중인 승급에 관한 사항은 종전의 규정에 따른다.",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "경과조치",
   "paragraph_label": ""
  },
  "sha256": "488ee98d7dfa59a2971b3d829fcd4a83aec24a4476cf2343f2d51b11fef589d8"
 },
 {
  "text": "[위치] 인사규정 > 별지제1호서식 휴직 신청서\n[본문]\n[별지 제1호서식] 휴직 신청서\n구분 | 작성 내용\n성명 | 신청인의 성명을 적습니다\n소속 | 소속 부서를 적습니다\n직급 | 현재 직급을 적습니다\n신청 내용 | 휴직 신청서의 구체적 내용을 적습니다\n첨부 서류 | 해당 증빙서류를 붙입니다\n위와 같이 신청합니다.\n년 월 일\n신청인 (서명 또는 인)\n원장 귀하",
  "metadata": {
   "regulation_title": "인사규정",
   "article_title": "",
   "paragraph_label": ""
  },
  "sha256": "faa5f1ab6ad6fc67711b03f2402161fccfdd7fb45df76ce6b9b2085821633082"
 },
 {
  "text": "[위치] 복무규정 > 제1장 총칙 > 제3조 적용 범위\n[본문]\n제3조(적용 범위) 이 규정은 연구원의 모든 직원에게 적용한다. 다만, 다른 법령이나 규정에 특별한 규정이 있는 경우에는 그에 따른다.",
  "metadata": {
   "regulation_title": "복무규정",
   "article_title": "적용 범위",
   "paragraph_label": ""
  },
  "sha256": "0a74a9a9cec93c8e2a1a651d5b160d7ea3ffbf28a97b6756ec5f11f2f9ca8229"
 }
]

GOLDEN_CLEAN_CASES = [
 {
  "input": "일 시금 지급 정 산 방법  ",
  "expected": "일시금 지급 정산 방법"
 },
 {
  "input": "하 되 경 우 교 직원 재직기 간 휴 직한 7개 월째",
  "expected": "하되 경우 교직원 재직기간 휴직한 7개월째"
 },
 {
  "input": "해당 하는 70 만원 임신또는 3년이내 다 음 음주운 전 등 급 징계 량 다 시",
  "expected": "해당하는 70만원 임신 또는 3년 이내 다음 음주운전 등급 징계량 다시"
 },
 {
  "input": "제1조 (목적) <개정 2020. 1. 1.> 이 규정은 목적으로 한다 <신설",
  "expected": "제1조 (목적) 이 규정은 목적으로 한다"
 },
 {
  "input": "4-5-2. 기록물관리규정 12",
  "expected": ""
 },
 {
  "input": "인사규정 3.1",
  "expected": ""
 },
 {
  "input": "①휴직 ②복직 ③3년이내 사용한다",
  "expected": "① 휴직 ② 복직 ③ 3년 이내 사용한다"
 },
 {
  "input": "5 년 이내 3 개월 이상 10일 초과",
  "expected": "5 년 이내 3 개월 이상 10일 초과"
 },
 {
  "input": "직원 으로 임용 에서 근무 에게 통보 부터 시행 까지 보다 처럼 만큼 와 과 를 을 은 는 도 의 기준",
  "expected": "직원으로 임용에서 근무에게 통보부터 시행까지 보다처럼 만큼와 과를 을은 는도 의 기준"
 },
 {
  "input": "- 항목 설명 ; ,",
  "expected": "- 항목 설명"
 },
 {
  "input": "",
  "expected": ""
 },
 {
  "input": "   ",
  "expected": ""
 },
 {
  "input": "한 줄\n두 줄\t세 줄",
  "expected": "한 줄 두 줄 세 줄"
 },
 {
  "input": "<주> 비고 <별표 1>",
  "expected": "비고"
 }
]


def _profile_digest(profile: dict) -> str:
    payload = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AnswerProfileGoldenOutputTests(unittest.TestCase):
    def test_build_answer_profile_matches_recorded_outputs(self) -> None:
        for index, case in enumerate(GOLDEN_PROFILE_CASES):
            with self.subTest(index=index):
                profile = build_answer_profile(case["text"], case["metadata"])
                self.assertEqual(case["sha256"], _profile_digest(profile))
                if "profile" in case:
                    self.assertEqual(case["profile"], profile)

    def test_clean_answer_profile_text_matches_recorded_outputs(self) -> None:
        for index, case in enumerate(GOLDEN_CLEAN_CASES):
            with self.subTest(index=index, value=case["input"][:20]):
                self.assertEqual(case["expected"], clean_answer_profile_text(case["input"]))

    def test_empty_and_marker_free_inputs(self) -> None:
        self.assertEqual({}, build_answer_profile("", {}))
        self.assertEqual({}, build_answer_profile("   ", None))
        self.assertEqual("", clean_answer_profile_text(""))
        self.assertEqual("", clean_answer_profile_text(None))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
