from __future__ import annotations

import hashlib
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

from app.retrieval.bm25_index import Bm25Index, load_bm25_index, write_bm25_index
from app.retrieval.searcher import search


class Bm25IndexTests(unittest.TestCase):
    def test_nfd_query_matches_nfc_indexed_document(self) -> None:
        # A document indexed in NFC must still be found by an NFD query; Unicode
        # composition differences must not silently drop an obvious match.
        records = [_record("doc:leave", "제29조 육아휴직 기간은 3년 이내로 한다.")]
        index = Bm25Index.build(records)

        nfd_query = unicodedata.normalize("NFD", "육아휴직")

        self.assertIn("doc:leave", index.score(nfd_query))

    def test_particle_variant_query_ranks_base_noun_chunk_first(self) -> None:
        records = [
            _record("doc:병가", "직원은 병가 사용을 신청할 수 있다.", article_title="병가"),
            _record("doc:출장", "직원은 출장 여비를 신청할 수 있다.", article_title="출장"),
        ]
        index = Bm25Index.build(records)

        scored, metadata = search("병가를 사용한 직원", records, index, top_k=2)

        self.assertEqual("kiwi-bm25-v1", metadata["retrieval_model"])
        self.assertFalse(metadata["retrieval_fallback"])
        self.assertEqual("doc:병가", scored[0][1]["id"])

    def test_common_terms_have_lower_weight_than_rare_terms(self) -> None:
        records = [
            _record("doc:병가", "공통 병가"),
            _record("doc:출장", "공통 출장"),
            _record("doc:교육", "공통 교육"),
        ]
        index = Bm25Index.build(records)

        common_score = index.score("공통")["doc:병가"]
        rare_score = index.score("병가")["doc:병가"]

        self.assertEqual(3, index.document_frequencies["공통"])
        self.assertEqual(1, index.document_frequencies["병가"])
        self.assertGreater(rare_score, common_score)

    def test_duplicate_query_terms_keep_repeated_weight(self) -> None:
        records = [
            _record("doc:병가", "병가 신청"),
            _record("doc:출장", "출장 신청"),
        ]
        index = Bm25Index.build(records)

        single_score = index.score("병가")["doc:병가"]
        repeated_score = index.score("병가 병가")["doc:병가"]

        self.assertEqual(round(single_score * 2, 8), repeated_score)

    def test_repeated_document_term_outranks_single_occurrence(self) -> None:
        # Both documents tokenize to the same length, so only term frequency
        # separates them; deduping the body would make the scores identical.
        records = [
            _record("doc:many", "병가 병가 병가 출장 출장 교육"),
            _record("doc:one", "병가 출장 출장 교육 교육 교육"),
        ]
        index = Bm25Index.build(records)

        scores = index.score("병가")

        self.assertGreater(scores["doc:many"], scores["doc:one"])

    def test_score_uses_the_tokenizer_recorded_in_the_index(self) -> None:
        records = [_record("doc:effective-date", "제44조제2항은 2026년 7월 1일부터 시행한다.")]
        index = Bm25Index.build(records)

        with patch(
            "app.retrieval.bm25_index.tokenize",
            return_value=["시행"],
        ) as tokenizer:
            index.score("시행일은?")

        tokenizer.assert_called_once_with(
            "시행일은?",
            dedupe=False,
            tokenizer_model=index.tokenizer,
        )

    def test_serialized_index_does_not_store_full_text(self) -> None:
        records = [_record("doc:병가", "원문 전문은 인덱스에 저장하지 않는다.")]
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "bm25_index.json"

            write_bm25_index(index_path, records)
            loaded = load_bm25_index(index_path)
            raw = index_path.read_text(encoding="utf-8")

        self.assertIsNotNone(loaded)
        self.assertNotIn("원문 전문은 인덱스에 저장하지 않는다.", raw)
        self.assertIn("term_frequencies", raw)

    def test_stale_index_falls_back_to_hash_embedding(self) -> None:
        records = [_record("doc:old", "병가")]
        changed = [_record("doc:old", "출장")]
        index = Bm25Index.build(records)

        _scored, metadata = search("병가", changed, index, top_k=1, index_records=changed)

        self.assertTrue(metadata["retrieval_fallback"])
        self.assertEqual("stale_bm25_index", metadata["bm25_index_status"])

    def test_precomputed_source_hash_controls_stale_index_check(self) -> None:
        records = [_record("doc:policy", "병가 신청")]
        index = Bm25Index.build(records)

        scored, ready_metadata = search(
            "병가",
            records,
            index,
            top_k=1,
            index_records=records,
            index_source_content_hashes=index.source_content_hashes,
        )
        _stale_scored, stale_metadata = search(
            "병가",
            records,
            index,
            top_k=1,
            index_records=records,
            index_source_content_hashes="stale-source-hash",
        )

        self.assertFalse(ready_metadata["retrieval_fallback"])
        self.assertEqual("ready", ready_metadata["bm25_index_status"])
        self.assertEqual("doc:policy", scored[0][1]["id"])
        self.assertTrue(stale_metadata["retrieval_fallback"])
        self.assertEqual("stale_bm25_index", stale_metadata["bm25_index_status"])

    def test_missing_bm25_without_embeddings_uses_lexical_fallback(self) -> None:
        records = [
            _record("doc:병가", "직원은 병가 사용을 신청할 수 있다.", include_embedding=False),
            _record("doc:출장", "직원은 출장 여비를 신청할 수 있다.", include_embedding=False),
        ]

        scored, metadata = search("병가 신청", records, None, top_k=2)

        self.assertTrue(metadata["retrieval_fallback"])
        self.assertEqual("missing_bm25_index", metadata["bm25_index_status"])
        self.assertEqual("token-lexical-fallback-v1", metadata["retrieval_model"])
        self.assertEqual("doc:병가", scored[0][1]["id"])

    def test_score_can_limit_to_visible_candidate_ids(self) -> None:
        records = [
            _record("doc:visible", "visible policy"),
            _record("doc:hidden", "hidden confidential policy"),
        ]
        index = Bm25Index.build(records)

        scores = index.score("hidden confidential", allowed_ids={"doc:visible"})

        self.assertEqual({}, scores)

    def test_ready_bm25_empty_scores_use_literal_substring_fallback(self) -> None:
        records = [
            _record("doc:leave", "육아휴직 신청 절차는 승인된 규정에 따른다."),
            _record("doc:travel", "국외출장 신청 절차"),
        ]
        index = Bm25Index.build(records)

        with patch.object(Bm25Index, "score", return_value={}):
            scored, metadata = search("육아휴직", records, index, top_k=2)

        self.assertTrue(metadata["retrieval_fallback"])
        self.assertEqual("ready_bm25_no_hits_literal_fallback", metadata["bm25_index_status"])
        self.assertEqual("doc:leave", scored[0][1]["id"])

    def test_regulation_query_expansion_ranks_performance_bonus_payment_timing(self) -> None:
        records = [
            _record(
                "doc:pay-time",
                "제24조(성과연봉의 지급방법) 성과연봉은 이등분하여 6월 및 12월에 일시금으로 지급한다.",
                article_title="성과연봉의 지급방법",
            ),
            _record(
                "doc:pay-exclusion",
                "제27조의2(성과연봉 지급대상 제외) 중징계 처분을 받은 경우 성과연봉 지급대상에서 제외한다.",
                article_title="성과연봉 지급대상 제외",
            ),
        ]
        index = Bm25Index.build(records)

        scored, metadata = search("성과연봉은 언제 지급해?", records, index, top_k=2)

        self.assertTrue(metadata["query_expanded"])
        self.assertEqual("doc:pay-time", scored[0][1]["id"])

    def test_regulation_query_expansion_ranks_performance_bonus_exclusion(self) -> None:
        records = [
            _record(
                "doc:pay-time",
                "제24조(성과연봉의 지급방법) 성과연봉은 이등분하여 6월 및 12월에 일시금으로 지급한다.",
                article_title="성과연봉의 지급방법",
            ),
            _record(
                "doc:pay-exclusion",
                (
                    "제27조의2(성과연봉 지급대상 제외) 평가대상 기간 중 중징계 처분을 받거나 "
                    "성폭력, 성매매, 성희롱, 음주운전 사유로 징계를 받은 경우 성과연봉 지급 대상에서 제외한다."
                ),
                article_title="성과연봉 지급대상 제외",
            ),
        ]
        index = Bm25Index.build(records)

        scored, metadata = search("성과연봉 지급 제외 사유는?", records, index, top_k=2)

        self.assertTrue(metadata["query_expanded"])
        self.assertEqual("doc:pay-exclusion", scored[0][1]["id"])

    def test_regulation_query_expansion_ranks_childcare_leave_duration(self) -> None:
        records = [
            _record(
                "doc:leave-eligibility",
                "제29조(휴직) 만 8세 이하 또는 초등학교 2학년 이하의 자녀를 양육하기 위하여 필요한 경우 육아휴직을 명하여야 한다.",
                article_title="휴직",
            ),
            _record(
                "doc:leave-duration",
                "제30조(휴직 기간) 인사규정 제29조 제3항에 따른 육아휴직은 자녀 1명에 대하여 3년 이내로 한다.",
                article_title="휴직 기간",
            ),
            _record(
                "doc:leave-allowance",
                "제33조(육아휴직수당) 30일 이상 육아휴직한 교직원에게 육아휴직수당을 지급한다.",
                article_title="육아휴직수당",
            ),
        ]
        index = Bm25Index.build(records)

        scored, metadata = search("육아휴직의 신청 요건과 기간, 수당은?", records, index, top_k=3)

        self.assertTrue(metadata["query_expanded"])
        top_ids = [item[1]["id"] for item in scored]
        self.assertIn("doc:leave-eligibility", top_ids)
        self.assertIn("doc:leave-duration", top_ids)
        self.assertIn("doc:leave-allowance", top_ids)

    def test_childcare_leave_query_keeps_eligibility_duration_and_allowance_in_top_results(self) -> None:
        records = [
            _record(
                "doc:time-select",
                "제7조(시간선택제의 신청) 육아휴직 대신 시간선택제 전환을 신청할 수 있다.",
                article_title="시간선택제의 신청",
            ),
            _record(
                "doc:allowance-special",
                "육아휴직 7개월째부터 12개월째까지 제1항에 따른 금액을 지급한다.",
                article_title="육아휴직수당 특례",
            ),
            _record(
                "doc:leave-eligibility",
                "제29조(휴직) 만 8세 이하 또는 초등학교 2학년 이하의 자녀를 양육하기 위하여 필요한 경우 휴직을 명하여야 한다.",
                article_title="휴직",
            ),
            _record(
                "doc:leave-duration",
                "제30조(휴직 기간) 제29조 제3항의 휴직 기간은 자녀 1명에 대하여 3년 이내로 한다.",
                article_title="휴직 기간",
            ),
            _record(
                "doc:leave-allowance",
                "제33조(육아휴직수당) 30일 이상 휴직한 교직원의 육아휴직수당은 기본연봉월액의 78퍼센트와 62.4퍼센트 기준으로 한다.",
                article_title="육아휴직수당",
            ),
        ]
        index = Bm25Index.build(records)

        scored, metadata = search("육아휴직의 신청 요건과 기간, 수당은?", records, index, top_k=3)

        self.assertTrue(metadata["query_expanded"])
        self.assertEqual(
            {"doc:leave-eligibility", "doc:leave-duration", "doc:leave-allowance"},
            {item[1]["id"] for item in scored},
        )

    def test_regulation_query_expansion_prefers_leave_of_absence_over_vacation_types(self) -> None:
        records = [
            _record(
                "doc:leave-reasons",
                "제29조(휴직 사유) 임용권자는 교직원이 휴직 사유에 해당하는 경우 휴직을 명하여야 한다.",
                article_title="휴직 사유",
            ),
            _record(
                "doc:leave-operation",
                "제31조(휴직의 운영) 휴직 중인 교직원은 신분은 보유하나 직무에 종사하지 못한다. 사유가 소멸되면 30일 이내 신고하여야 한다.",
                article_title="휴직의 운영",
            ),
            _record(
                "doc:vacation-types",
                "제19조(휴가의 종류) 교직원의 휴가는 연가, 병가, 공가 및 특별휴가로 구분한다.",
                article_title="휴가의 종류",
            ),
        ]
        index = Bm25Index.build(records)

        scored, metadata = search("휴직의 종류와 절차", records, index, top_k=3)

        self.assertTrue(metadata["query_expanded"])
        self.assertIn(scored[0][1]["id"], {"doc:leave-reasons", "doc:leave-operation"})
        self.assertIn("doc:leave-reasons", [item[1]["id"] for item in scored[:2]])
        self.assertNotEqual("doc:vacation-types", scored[0][1]["id"])

    def test_kinds_query_promotes_definition_articles_for_enumerated_terms(self) -> None:
        records = [
            _record(
                "doc:vacation-types",
                "제19조(휴가의 종류) 교직원의 휴가는 연가․병가․공가․청가 및 특별휴가로 구분한다.",
                article_title="휴가의 종류",
            ),
            _record(
                "doc:special-vacation",
                "제24조(특별휴가) 원장은 풍해, 수해, 화재 등 재해로 인하여 피해를 입은 교직원에 대해 5일 이내의 특별휴가를 줄 수 있다.",
                article_title="특별휴가",
            ),
            _record(
                "doc:travel",
                "제40조(출장) 교직원의 출장은 원장이 명한다.",
                article_title="출장",
            ),
        ]
        index = Bm25Index.build(records)

        scored, metadata = search("휴가의 종류에는 뭐가 있어?", records, index, top_k=3)

        result_ids = [record["id"] for _, record in scored]
        self.assertIn("doc:vacation-types", result_ids)
        self.assertIn("doc:special-vacation", result_ids)
        self.assertIn("특별휴가", metadata["enumeration_definition_terms"])

    def test_non_kinds_query_does_not_promote_enumeration_definitions(self) -> None:
        records = [
            _record(
                "doc:vacation-types",
                "제19조(휴가의 종류) 교직원의 휴가는 연가․병가․공가․청가 및 특별휴가로 구분한다.",
                article_title="휴가의 종류",
            ),
            _record(
                "doc:special-vacation",
                "제24조(특별휴가) 원장은 재해로 인하여 피해를 입은 교직원에 대해 5일 이내의 특별휴가를 줄 수 있다.",
                article_title="특별휴가",
            ),
        ]
        index = Bm25Index.build(records)

        _, metadata = search("휴가 신청 절차", records, index, top_k=2)

        self.assertNotIn("enumeration_definition_terms", metadata)

    def test_regulation_query_expansion_prefers_faculty_committee_function(self) -> None:
        records = [
            _record(
                "doc:achievement-review",
                "교원업적평가 규정 제13조(인사위원회 심의) 연구직 직무수행 평가 결과를 인사위원회에 상정한다.",
                article_title="인사위원회 심의",
            ),
            _record(
                "doc:committee-function",
                "인사규정 제8조(위원회 기능) 교원 인사위원회는 교원의 신규 채용, 재계약, 승진, 정년보장, 강임에 관한 심의를 관장한다.",
                article_title="위원회 기능",
            ),
        ]
        index = Bm25Index.build(records)

        scored, metadata = search("교원 인사위원회 심의 대상은?", records, index, top_k=2)

        self.assertTrue(metadata["query_expanded"])
        self.assertEqual("doc:committee-function", scored[0][1]["id"])

    def test_regulation_query_expansion_prefers_appendix_form_rule_over_references(self) -> None:
        records = [
            _record(
                "doc:payment-reference",
                "제50조(지급근거) 교육훈련여비지급 기준은 별표 7에 따른다.",
                article_title="지급근거",
            ),
            _record(
                "doc:appendix-rule",
                "제18조(별표와 별지 서식) 내용이 길거나 복잡한 표, 그림, 계산식은 별표로 구분하고 별지 서식은 일정한 형식으로 사용한다.",
                article_title="별표와 별지 서식",
            ),
        ]
        index = Bm25Index.build(records)

        scored, metadata = search("별표나 서식 근거가 필요한 경우 어떻게 확인하나?", records, index, top_k=2)

        self.assertTrue(metadata["query_expanded"])
        self.assertEqual("doc:appendix-rule", scored[0][1]["id"])

    def test_regulation_query_expansion_does_not_overboost_generic_appendix_rule_for_domain_form(self) -> None:
        records = [
            _record(
                "doc:domain-contract",
                "\uc81c14\uc870(\uc784\uc6a9\uacc4\uc57d) \uc6d0\uc7a5\uc740 \uac15\uc0ac\uc784\uc6a9\uacc4\uc57d\uc11c\ub97c "
                "\ubcc4\uc9c0 \uc81c1\ud638\uc11c\uc2dd\uc73c\ub85c \uc791\uc131\ud55c\ub2e4.",
                article_title="\uc784\uc6a9\uacc4\uc57d",
            ),
            _record(
                "doc:appendix-rule",
                "\uc81c18\uc870(\ubcc4\ud45c\uc640 \ubcc4\uc9c0 \uc11c\uc2dd) \ub0b4\uc6a9\uc774 \uae38\uac70\ub098 "
                "\ubcf5\uc7a1\ud55c \ud45c, \uadf8\ub9bc, \uacc4\uc0b0\uc2dd\uc740 \ubcc4\ud45c\ub85c \uad6c\ubd84\ud55c\ub2e4.",
                article_title="\ubcc4\ud45c\uc640 \ubcc4\uc9c0 \uc11c\uc2dd",
            ),
        ]
        index = Bm25Index.build(records)

        scored, metadata = search(
            "\uac15\uc0ac\uc784\uc6a9\uacc4\uc57d\uc11c\ub294 \ubcc4\uc9c0 \uc81c1\ud638\uc11c\uc2dd\uc73c\ub85c "
            "\uc791\uc131\ud558\ub098\uc694?",
            records,
            index,
            top_k=2,
        )

        self.assertFalse(metadata["query_expanded"])
        self.assertEqual("doc:domain-contract", scored[0][1]["id"])

    def test_regulation_query_expansion_does_not_overboost_generic_appendix_rule_for_domain_table(self) -> None:
        records = [
            _record(
                "doc:research-qualification",
                "\ubcc4\ud45c 2-1 \uc5f0\uad6c\uc9c1 \uc784\uc6a9\uc790\uaca9 \uae30\uc900\ud45c\ub294 "
                "\uc5f0\uad6c\uacbd\ub825 \uc778\uc815\uae30\uc900\uc744 \ud3ec\ud568\ud55c\ub2e4.",
                article_title="\uc5f0\uad6c\uacbd\ub825 \uc778\uc815\uae30\uc900",
            ),
            _record(
                "doc:appendix-rule",
                "\uc81c18\uc870(\ubcc4\ud45c\uc640 \ubcc4\uc9c0 \uc11c\uc2dd) \ub0b4\uc6a9\uc774 \uae38\uac70\ub098 "
                "\ubcf5\uc7a1\ud55c \ud45c, \uadf8\ub9bc, \uacc4\uc0b0\uc2dd\uc740 \ubcc4\ud45c\ub85c \uad6c\ubd84\ud55c\ub2e4.",
                article_title="\ubcc4\ud45c\uc640 \ubcc4\uc9c0 \uc11c\uc2dd",
            ),
        ]
        index = Bm25Index.build(records)

        scored, metadata = search(
            "\uc5f0\uad6c\uc9c1 \uc784\uc6a9\uc790\uaca9\uae30\uc900\ud45c\ub294 \ubcc4\ud45c 2-1\ub85c "
            "\uc815\ud558\ub098\uc694?",
            records,
            index,
            top_k=2,
        )

        self.assertFalse(metadata["query_expanded"])
        self.assertEqual("doc:research-qualification", scored[0][1]["id"])

    def test_regulation_query_expansion_ranks_full_time_faculty_hiring_process(self) -> None:
        records = [
            _record(
                "doc:faculty-process",
                "교원 임용 세칙은 신규 채용 공개 공고, 기초심사, 연구실적심사, 공개발표심사, 면접심사, 교원 인사위원회 심의를 규정한다.",
                article_title="교원 임용 절차",
            ),
            _record(
                "doc:non-tenure",
                "비전임교원의 신규임용과 재계약 심사는 강사 임용 등에 관한 규정을 준용한다.",
                article_title="비전임교원 임용 절차",
            ),
        ]
        index = Bm25Index.build(records)

        scored, metadata = search("전임 교원 채용 절차는 어떻게 돼?", records, index, top_k=2)

        self.assertTrue(metadata["query_expanded"])
        self.assertEqual("doc:faculty-process", scored[0][1]["id"])

    def test_regulation_query_expansion_prefers_faculty_process_over_single_stage(self) -> None:
        records = [
            _record(
                "doc:faculty-process",
                "제7조 신규임용의 시기 및 제8조 신규임용 후보자 심사. 지원 마감일 전까지 15일 이상 공고하고 "
                "단계별로 기초심사, 연구실적심사, 공개발표심사, 면접심사를 진행한다.",
                article_title="신규임용 후보자 심사",
            ),
            _record(
                "doc:basic-screening",
                "제10조 기초심사. 기초심사의 합격자는 평균 80점 이상인 자를 대상으로 임용 예정 인원의 5배수 이내를 선발한다.",
                article_title="기초심사",
            ),
        ]
        index = Bm25Index.build(records)

        scored, metadata = search("전임 교원 채용 절차는 어떻게 돼?", records, index, top_k=2)

        self.assertTrue(metadata["query_expanded"])
        self.assertEqual("doc:faculty-process", scored[0][1]["id"])

    def test_regulation_query_expansion_keeps_faculty_definition_ahead_of_lecturer_notice(self) -> None:
        records = [
            _record(
                "doc:faculty-definition",
                "제38조(교원) 전임 교원은 교수, 부교수, 조교수로 하며 교원은 학부별 소속을 원칙으로 한다.",
                article_title="교원",
            ),
            _record(
                "doc:lecturer-notice",
                "제9조(임용공고) 강사를 신규채용하는 경우에는 채용분야와 지원자격을 7일 이상 공고한다.",
                article_title="임용공고",
            ),
            _record(
                "doc:faculty-process",
                "교원 임용 세칙 제8조 신규임용 후보자 심사는 기초심사, 연구실적심사, 공개발표심사, 면접심사를 포함한다.",
                article_title="신규임용 후보자 심사",
            ),
        ]
        index = Bm25Index.build(records)

        scored, metadata = search("전임 교원 채용 절차는 어떻게 돼?", records, index, top_k=3)

        self.assertTrue(metadata["query_expanded"])
        self.assertLess(
            [item[1]["id"] for item in scored].index("doc:faculty-definition"),
            [item[1]["id"] for item in scored].index("doc:lecturer-notice"),
        )
        self.assertLess(
            [item[1]["id"] for item in scored].index("doc:faculty-process"),
            [item[1]["id"] for item in scored].index("doc:lecturer-notice"),
        )

    def test_regulation_query_expansion_ranks_leave_foreign_travel_report_governing_article(self) -> None:
        records = [
            _record(
                "doc:travel-rule",
                (
                    "⑤ 휴직자가 국외로 출국하는 경우에는 별지 제16호서식에 따른 휴직자 국외 출국 신고서를 "
                    "작성하여 출국 7일 전까지 사무국장에게 제출해야 한다. 다만 14일 이하 국외 출국과 "
                    "영유아를 동반한 육아휴직자의 국외 출국의 경우 신고를 생략할 수 있다."
                ),
                article_title="",
            ),
            _record(
                "doc:governing-article",
                "제29조의3(휴직자의 복무실태 점검) 휴직 중인 교직원의 복무실태 점검과 휴직 목적 외 사용을 정한다.",
                article_title="휴직자의 복무실태 점검",
            ),
            _record(
                "doc:gift-form",
                "제12조(금품등의 인도 및 처리 등) 별지 제16호서식 금품등 폐기처분 동의확인서를 사용한다.",
                article_title="금품등의 인도 및 처리 등",
            ),
        ]
        index = Bm25Index.build(records)

        scored, metadata = search("휴직자 국외 출국 신고서는 언제 제출하나요?", records, index, top_k=3)

        self.assertTrue(metadata["query_expanded"])
        top_ids = [item[1]["id"] for item in scored]
        self.assertIn("doc:governing-article", top_ids)
        self.assertLess(top_ids.index("doc:governing-article"), top_ids.index("doc:gift-form"))


def _record(record_id: str, text: str, *, article_title: str = "", include_embedding: bool = True) -> dict:
    chunk_id = record_id.rsplit(":", 1)[-1]
    metadata = {
        "tenant_id": "tenant-a",
        "document_id": "doc",
        "chunk_id": chunk_id,
        "approval_status": "approved",
        "approval_id": f"approval-{chunk_id}",
        "security_level": "internal",
        "regulation_title": "복무규정",
        "article_title": article_title,
    }
    return {
        "id": record_id,
        "document_id": "doc",
        "chunk_id": chunk_id,
        "text": text,
        "metadata": metadata,
        "content_hash": hashlib.sha256(f"{record_id}\n{text}".encode("utf-8")).hexdigest(),
    }
    if include_embedding:
        record["embedding"] = [1.0, 0.0]
    return record


if __name__ == "__main__":
    unittest.main()
