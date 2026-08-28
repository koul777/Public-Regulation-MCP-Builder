from __future__ import annotations

import hashlib
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

from app.retrieval.bm25_index import (
    Bm25Index,
    _fast_query_terms,
    load_bm25_index,
    write_bm25_index,
)
from app.retrieval import searcher as searcher_module
from app.retrieval.searcher import (
    _structured_query_boost,
    rerank_bm25_candidates,
    search,
)
from app.retrieval.tokenizer import FALLBACK_TOKENIZER_MODEL, tokenize


class Bm25IndexTests(unittest.TestCase):
    def test_search_reuses_full_candidate_structured_context_for_boosts(
        self,
    ) -> None:
        records = [
            _record(
                "doc:target",
                "target policy article",
                regulation_title="target policy",
            ),
            _record(
                "doc:other",
                "other policy article",
                regulation_title="other policy",
            ),
        ]
        index = Bm25Index.build(records)

        with patch(
            "app.retrieval.searcher._build_structured_query_context",
            wraps=searcher_module._build_structured_query_context,
        ) as build_context:
            scored, _metadata = search(
                "target policy article",
                records,
                index,
                top_k=2,
            )

        self.assertTrue(scored)
        self.assertEqual(1, build_context.call_count)

    def test_structured_context_regex_scans_only_matching_unique_titles(
        self,
    ) -> None:
        records = [
            _record(
                f"doc:other-{index}",
                "other policy article",
                regulation_title="other policy",
            )
            for index in range(100)
        ]
        records.append(
            _record(
                "doc:target",
                "target policy article",
                regulation_title="target policy",
            )
        )

        with patch(
            "app.retrieval.searcher._query_named_title_spans",
            wraps=searcher_module._query_named_title_spans,
        ) as title_spans:
            context = searcher_module._build_structured_query_context(
                "target policy article",
                [(0.0, record) for record in records],
            )

        self.assertEqual(frozenset({"targetpolicy"}), context.matching_titles)
        self.assertEqual(1, title_spans.call_count)

    def test_structured_locator_boost_normalizes_fullwidth_article_number(self) -> None:
        metadata = {
            "regulation_title": "복무규정",
            "article_no": "제12조",
            "article_title": "적용 범위",
        }

        ascii_boost = _structured_query_boost("복무규정 제12조 적용 범위", metadata)
        fullwidth_boost = _structured_query_boost("복무규정 제１２조 적용 범위", metadata)

        self.assertGreater(ascii_boost, 0.0)
        self.assertEqual(ascii_boost, fullwidth_boost)

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

    def test_score_terms_preserves_normal_score_semantics(self) -> None:
        records = [
            _record("doc:travel", "approved overseas travel regulation"),
            _record("doc:leave", "approved leave regulation"),
        ]
        index = Bm25Index.build(records)
        query = "overseas travel regulation"
        query_terms = tokenize(
            query,
            dedupe=False,
            tokenizer_model=index.tokenizer,
        )

        self.assertEqual(index.score(query), index.score_terms(query_terms))

    def test_fast_query_terms_bridge_korean_compounds_without_kiwi(self) -> None:
        document_frequencies = {
            "공무": 2,
            "국외": 3,
            "여행": 4,
            "규정": 5,
            "무기": 1,
            "정의": 1,
        }
        query = "공무국외여행규정 규정의"

        with patch(
            "app.retrieval.bm25_index.tokenize",
            return_value=["공무국외여행규정", "규정의"],
        ) as tokenizer:
            terms = _fast_query_terms(query, document_frequencies)

        tokenizer.assert_called_once_with(
            query,
            dedupe=False,
            tokenizer_model=FALLBACK_TOKENIZER_MODEL,
        )
        self.assertTrue({"공무", "국외", "여행", "규정"}.issubset(terms))
        self.assertNotIn("무기", terms)
        self.assertNotIn("정의", terms)

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

    def test_candidate_reranker_never_expands_pre_authorized_records(self) -> None:
        visible = _record("doc:visible", "approved travel policy")
        hidden = _record("doc:hidden", "confidential hiring policy exact target")
        index = Bm25Index.build([visible, hidden])

        reranked = rerank_bm25_candidates(
            "confidential hiring policy exact target",
            [(0.25, visible)],
            index,
        )

        self.assertEqual(["doc:visible"], [record["id"] for _, record in reranked])

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

    def test_regulation_query_expansion_does_not_inject_institution_specific_facts(self) -> None:
        childcare = searcher_module._expand_regulation_query(
            "육아휴직 신청 요건과 기간, 수당은?"
        )
        foreign_travel = searcher_module._expand_regulation_query(
            "휴직자 국외 출국 신고서는 언제 제출하나요?"
        )

        for expanded in (childcare, foreign_travel):
            self.assertNotIn("제29조", expanded)
            self.assertNotIn("제30조", expanded)
            self.assertNotIn("제33조", expanded)
            self.assertNotIn("7일 전", expanded)
            self.assertNotIn("14일 이하", expanded)
            self.assertNotIn("78퍼센트", expanded)

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

    def test_numbered_named_appendix_query_is_not_expanded_as_generic_authoring_question(self) -> None:
        target = _record(
            "doc:target-appendix",
            "[별표 5] 조문별 개정 서식과 작성 항목",
            regulation_title="원규관리규정 시행세칙",
        )
        target["metadata"]["appendix_refs"] = ["별표5"]
        generic_rule = _record(
            "doc:generic-authoring-rule",
            (
                "제18조(별표와 별지 서식) 내용이 길거나 복잡한 표, 그림, 계산식은 별표로 "
                "구분하고 별지 서식은 일정한 형식으로 작성한다. 본칙에 부수되는 별표와 "
                "별지 서식의 작성방식, 일부개정 및 전부개정을 정한다."
            ),
            article_title="별표와 별지 서식",
            regulation_title="원규관리규정",
        )
        generic_rule["metadata"]["article_no"] = "제18조"
        records = [generic_rule, target]
        index = Bm25Index.build(records)

        scored, metadata = search(
            "원규관리규정 시행세칙 별표 5의 작성 항목",
            records,
            index,
            top_k=2,
        )

        self.assertFalse(metadata["query_expanded"])
        self.assertEqual("doc:target-appendix", scored[0][1]["id"])

    def test_numbered_appendix_without_regulation_name_is_not_generically_expanded(self) -> None:
        records = [_record("doc:appendix-five", "[별표 5] 작성 항목")]
        records[0]["metadata"]["appendix_refs"] = ["별표5"]
        index = Bm25Index.build(records)

        _scored, metadata = search("별표 5 작성 항목", records, index, top_k=1)

        self.assertFalse(metadata["query_expanded"])

    def test_named_regulation_without_appendix_number_is_not_generically_expanded(self) -> None:
        records = [
            _record(
                "doc:document-rule",
                "별표 작성 방식",
                regulation_title="문서관리규정",
            )
        ]
        index = Bm25Index.build(records)

        _scored, metadata = search(
            "문서관리규정의 별표 작성 방식",
            records,
            index,
            top_k=1,
        )

        self.assertFalse(metadata["query_expanded"])

    def test_structured_locator_boost_allows_particle_but_rejects_number_collision(self) -> None:
        query = "별표 5의 작성 항목"

        exact = _structured_query_boost(query, {"appendix_refs": ["별표5"]})
        longer_number = _structured_query_boost(query, {"appendix_refs": ["별표50"]})
        child_locator = _structured_query_boost("별표 5-1 작성 항목", {"appendix_refs": ["별표5"]})

        self.assertGreater(exact, 0.0)
        self.assertEqual(0.0, longer_number)
        self.assertEqual(0.0, child_locator)

    def test_nfd_named_appendix_query_preserves_specific_query_behavior(self) -> None:
        target = _record(
            "doc:nfd-target",
            "[별표 5] 작성 항목",
            regulation_title="원규관리규정 시행세칙",
        )
        target["metadata"]["appendix_refs"] = ["별표5"]
        records = [target]
        index = Bm25Index.build(records)
        query = unicodedata.normalize("NFD", "원규관리규정 시행세칙 별표 5 작성 항목")

        scored, metadata = search(query, records, index, top_k=1)

        self.assertFalse(metadata["query_expanded"])
        self.assertEqual("doc:nfd-target", scored[0][1]["id"])

    def test_reference_locator_query_prefers_exact_form_chunk_over_same_article_number(self) -> None:
        article = _record(
            "doc:staff-rule-article",
            "제1조 목적",
            regulation_title="직원 채용 세칙",
        )
        article["metadata"]["article_no"] = "제1조"
        form = _record(
            "doc:staff-rule-form",
            "별지 제1호서식 계약당사자",
            regulation_title="직원 채용 세칙",
        )
        form["metadata"]["article_no"] = "제1조"
        form["metadata"]["form_refs"] = ["별지제1호서식"]
        form["metadata"]["chunk_type"] = "form"
        records = [article, form]
        index = Bm25Index.build(records)

        scored, _metadata = search(
            "직원 채용 세칙 별지 제1호서식 (제1조 관련)에는 어떤 항목이 있는가?",
            records,
            index,
            top_k=2,
        )

        self.assertEqual("doc:staff-rule-form", scored[0][1]["id"])

    def test_structured_form_locator_allows_trailing_revision_dates(self) -> None:
        boost = _structured_query_boost(
            "직원 채용 세칙의 별지 제1호서식] <2022.12.14., 2024.9.27.> (제1조 관련)에는 어떤 기준이나 항목이 정리되어 있는가?",
            {
                "regulation_title": "직원 채용 세칙",
                "chunk_type": "form",
                "form_refs": ["별지제1호서식"],
            },
        )

        self.assertGreater(boost, 0.0)

    def test_named_regulation_query_prefers_exact_article_title_with_same_article_number(self) -> None:
        target = _record(
            "doc:rule-amendment",
            "제3조 다른 규정의 개정",
            regulation_title="강사임용 등에 관한 규정",
        )
        target["metadata"]["article_no"] = "제3조"
        target["metadata"]["article_title"] = "다른 규정의 개정"
        sibling = _record(
            "doc:rule-qualification",
            "제3조 자격",
            regulation_title="강사임용 등에 관한 규정",
        )
        sibling["metadata"]["article_no"] = "제3조"
        sibling["metadata"]["article_title"] = "자격"
        records = [sibling, target]
        index = Bm25Index.build(records)

        scored, _metadata = search(
            "강사임용 등에 관한 규정 제3조(다른 규정의 개정)의 핵심 내용과 적용 조건은 무엇인가?",
            records,
            index,
            top_k=2,
        )

        self.assertEqual("doc:rule-amendment", scored[0][1]["id"])

    def test_exact_article_title_overcomes_small_sibling_lexical_lead(self) -> None:
        target = _record(
            "doc:rule-amendment",
            "제3조 다른 규정의 개정",
            regulation_title="강사 채용 규정",
        )
        target["metadata"]["article_no"] = "제3조"
        target["metadata"]["article_title"] = "다른 규정의 개정"
        sibling = _record(
            "doc:rule-purpose",
            "제3조 목적",
            regulation_title="강사 채용 규정",
        )
        sibling["metadata"]["article_no"] = "제3조"
        sibling["metadata"]["article_title"] = "목적"

        class SiblingFavoredIndex:
            def score_fast_query(
                self,
                _query: str,
                *,
                allowed_ids: set[str] | None = None,
            ) -> dict[str, float]:
                return {
                    "doc:rule-purpose": 10.0,
                    "doc:rule-amendment": 0.0,
                }

        reranked = rerank_bm25_candidates(
            "강사 채용 규정 제3조(다른 규정의 개정)의 적용 내용",
            [(0.0, sibling), (0.0, target)],
            SiblingFavoredIndex(),  # type: ignore[arg-type]
        )

        self.assertEqual("doc:rule-amendment", reranked[0][1]["id"])

    def test_candidate_named_regulation_signal_recognizes_spaced_title(self) -> None:
        target = _record(
            "doc:spaced-title",
            "적용 범위",
            regulation_title="직원 채용 세칙",
        )
        competitor = _record(
            "doc:lexical-competitor",
            "직원 채용 세칙 적용 범위",
            regulation_title="일반 운영 규정",
        )

        class CompetitorIndex:
            def score_fast_query(
                self,
                _query: str,
                *,
                allowed_ids: set[str] | None = None,
            ) -> dict[str, float]:
                self.allowed_ids = allowed_ids
                return {"doc:lexical-competitor": 10.0}

        index = CompetitorIndex()
        reranked = rerank_bm25_candidates(
            "직원 채용 세칙의 적용 범위",
            [(0.0, competitor), (0.0, target)],
            index,  # type: ignore[arg-type]
        )

        self.assertEqual("doc:spaced-title", reranked[0][1]["id"])
        self.assertEqual(
            {"doc:spaced-title", "doc:lexical-competitor"},
            index.allowed_ids,
        )

    def test_named_unnumbered_appendix_intent_promotes_appendix_without_expansion(self) -> None:
        appendix = _record(
            "doc:staff-appendix",
            "[별표] 평가 항목",
            regulation_title="직원 채용 세칙",
        )
        appendix["metadata"]["chunk_type"] = "appendix"
        appendix["metadata"]["appendix_refs"] = ["별표"]
        governing_article = _record(
            "doc:staff-governing-article",
            (
                "제9조 별표의 적용 기준과 평가 항목을 정한다. "
                "직원 채용 세칙의 별표 항목과 적용 기준을 설명한다."
            ),
            article_title="별표의 적용 기준",
            regulation_title="직원 채용 세칙",
        )
        governing_article["metadata"]["article_no"] = "제9조"
        records = [governing_article, appendix]
        index = Bm25Index.build(records)

        scored, metadata = search(
            "직원 채용 세칙의 별표에는 어떤 항목이 있는가?",
            records,
            index,
            top_k=2,
        )

        self.assertFalse(metadata["query_expanded"])
        self.assertEqual("doc:staff-appendix", scored[0][1]["id"])

    def test_unnumbered_attachment_boost_excludes_governing_article_reference(self) -> None:
        query = "직원 채용 세칙의 별표에는 어떤 항목이 있는가?"
        appendix_metadata = {
            "regulation_title": "직원 채용 세칙",
            "chunk_type": "appendix",
            "appendix_refs": ["별표"],
        }
        governing_article_metadata = {
            "regulation_title": "직원 채용 세칙",
            "chunk_type": "article",
            "appendix_refs": ["별표"],
        }

        appendix_boost = _structured_query_boost(query, appendix_metadata)
        governing_article_boost = _structured_query_boost(
            query,
            governing_article_metadata,
        )

        self.assertGreater(appendix_boost, governing_article_boost)

    def test_exact_named_regulation_and_article_locator_outrank_suffix_title_collision(self) -> None:
        target = _record(
            "doc:faculty-article",
            "제7조 적용 대상과 심의 기준",
            regulation_title="교원인사규정",
        )
        target["metadata"]["article_no"] = "제7조"
        suffix_collision = _record(
            "doc:generic-personnel",
            "제7조 적용 대상과 심의 기준을 정하고 심의 기준을 반복하여 설명한다.",
            regulation_title="인사규정",
        )
        suffix_collision["metadata"]["article_no"] = "제7조"
        records = [suffix_collision, target]
        index = Bm25Index.build(records)

        scored, _metadata = search(
            "교원 인사규정 제7조의 적용 대상은?",
            records,
            index,
            top_k=2,
        )

        self.assertEqual("doc:faculty-article", scored[0][1]["id"])

    def test_named_form_query_prefers_exact_form_chunk_over_same_document_articles(self) -> None:
        target = _record(
            "doc:staff-form",
            "[별지 제1호서식] 계약당사자",
            regulation_title="직원 채용 세칙",
        )
        target["metadata"]["chunk_type"] = "form"
        target["metadata"]["form_refs"] = ["별지제1호서식"]
        target["metadata"]["article_no"] = "제1조"
        article_purpose = _record(
            "doc:staff-purpose",
            "제1조 목적",
            regulation_title="직원 채용 세칙",
        )
        article_purpose["metadata"]["article_no"] = "제1조"
        article_effective = _record(
            "doc:staff-effective",
            "제1조 시행일",
            regulation_title="직원 채용 세칙",
        )
        article_effective["metadata"]["article_no"] = "제1조"
        records = [article_purpose, article_effective, target]
        index = Bm25Index.build(records)

        scored, _metadata = search(
            "직원 채용 세칙 별지 제1호서식 제1조 관련 항목",
            records,
            index,
            top_k=3,
        )

        self.assertEqual("doc:staff-form", scored[0][1]["id"])

    def test_named_form_query_with_trailing_revision_dates_prefers_form_chunk(self) -> None:
        target = _record(
            "doc:staff-form-dated",
            "[별지 제1호서식] 응시원서",
            regulation_title="직원 채용 세칙",
        )
        target["metadata"]["chunk_type"] = "form"
        target["metadata"]["form_refs"] = ["별지제1호서식"]
        target["metadata"]["article_no"] = "제1조"
        article_effective = _record(
            "doc:staff-effective-dated",
            "제1조 시행일",
            regulation_title="직원 채용 세칙",
        )
        article_effective["metadata"]["article_no"] = "제1조"
        records = [article_effective, target]
        index = Bm25Index.build(records)

        scored, _metadata = search(
            "직원 채용 세칙의 별지 제1호서식] <2022.12.14., 2024.9.27.> (제1조 관련)에는 어떤 기준이나 항목이 정리되어 있는가?",
            records,
            index,
            top_k=2,
        )

        self.assertEqual("doc:staff-form-dated", scored[0][1]["id"])

    def test_named_appendix_query_prefers_exact_appendix_chunk_over_same_document_article(self) -> None:
        target = _record(
            "doc:rule-appendix",
            "[별표 5] 작성 항목",
            regulation_title="원규관리규정 시행세칙",
        )
        target["metadata"]["chunk_type"] = "appendix"
        target["metadata"]["appendix_refs"] = ["별표5"]
        target["metadata"]["article_no"] = "제20조"
        article = _record(
            "doc:rule-article",
            "제20조 별표 기준",
            regulation_title="원규관리규정 시행세칙",
        )
        article["metadata"]["article_no"] = "제20조"
        records = [article, target]
        index = Bm25Index.build(records)

        scored, _metadata = search(
            "원규관리규정 시행세칙 별표 5 제20조 관련 작성 항목",
            records,
            index,
            top_k=2,
        )

        self.assertEqual("doc:rule-appendix", scored[0][1]["id"])

    def test_named_appendix_query_prefers_exact_table_chunk_over_related_article(self) -> None:
        target = _record(
            "doc:rule-table",
            "별표 5 세부 작성 항목",
            regulation_title="문서관리규정",
        )
        target["metadata"]["chunk_type"] = "table"
        target["metadata"]["table_appendix_no"] = "별표5"
        target["metadata"]["article_no"] = "제20조"
        article = _record(
            "doc:rule-article",
            "제20조 별표 기준과 작성 항목",
            regulation_title="문서관리규정",
        )
        article["metadata"]["article_no"] = "제20조"
        records = [article, target]
        index = Bm25Index.build(records)

        scored, _metadata = search(
            "문서관리규정 별표 5 제20조 관련 작성 항목",
            records,
            index,
            top_k=2,
        )

        self.assertEqual("doc:rule-table", scored[0][1]["id"])

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


def _record(
    record_id: str,
    text: str,
    *,
    article_title: str = "",
    regulation_title: str = "복무규정",
    include_embedding: bool = False,
) -> dict:
    chunk_id = record_id.rsplit(":", 1)[-1]
    metadata = {
        "tenant_id": "tenant-a",
        "document_id": "doc",
        "chunk_id": chunk_id,
        "approval_status": "approved",
        "approval_id": f"approval-{chunk_id}",
        "security_level": "internal",
        "regulation_title": regulation_title,
        "article_title": article_title,
    }
    record = {
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
