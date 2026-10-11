from __future__ import annotations

import unicodedata
import unittest
from unittest.mock import patch

from app.retrieval import tokenizer as tokenizer_module
from app.retrieval.tokenizer import (
    FALLBACK_TOKENIZER_MODEL,
    TOKENIZER_MODEL,
    preloaded_kiwi_tokens,
    recording_kiwi_inputs,
    tokenize,
)


class RetrievalTokenizerTests(unittest.TestCase):
    def tearDown(self) -> None:
        tokenizer_module._cached_expanded_token.cache_clear()

    def test_cached_expansion_does_not_share_mutable_results(self) -> None:
        expected = ["점검규정", "점검", "규정"]
        first = tokenizer_module._expand_token("점검규정")
        self.assertEqual(expected, first)
        first[:] = ["다른 요청에서 변경한 값"]
        self.assertEqual(expected, tokenizer_module._expand_token("점검규정"))

    def test_oversized_token_is_processed_without_cache_retention(self) -> None:
        tokenizer_module._cached_expanded_token.cache_clear()
        token = "가" * 129 + "규정"
        self.assertEqual(
            [token, "가" * 129, "규정"],
            tokenizer_module._expand_token(token),
        )
        self.assertEqual(0, tokenizer_module._cached_expanded_token.cache_info().currsize)

    def test_cached_expansion_preserves_repeated_and_deduplicated_tokens(self) -> None:
        text = "제７조 점검규정 점검규정"
        repeated = tokenize(text, dedupe=False, tokenizer_model=FALLBACK_TOKENIZER_MODEL)
        self.assertEqual(2, repeated.count("점검규정"))
        self.assertIn("제7조", repeated)
        deduplicated = tokenize(text, tokenizer_model=FALLBACK_TOKENIZER_MODEL)
        self.assertEqual(list(dict.fromkeys(repeated)), deduplicated)
        repeated.clear()
        self.assertEqual(deduplicated, tokenize(text, tokenizer_model=FALLBACK_TOKENIZER_MODEL))

    def test_tokenize_normalizes_unicode_so_nfd_matches_nfc(self) -> None:
        # Korean text from PDF/DOCX extraction or macOS filenames can arrive
        # decomposed (NFD).  Indexing and querying both flow through tokenize,
        # so it must yield identical tokens regardless of composition; otherwise
        # an NFD document is invisible to an NFC query and vice versa.
        nfc = "육아휴직"
        nfd = unicodedata.normalize("NFD", nfc)
        self.assertNotEqual(nfc, nfd)
        self.assertEqual(
            tokenize(nfc, tokenizer_model=FALLBACK_TOKENIZER_MODEL),
            tokenize(nfd, tokenizer_model=FALLBACK_TOKENIZER_MODEL),
        )

    def test_tokenize_normalizes_fullwidth_article_numbers(self) -> None:
        self.assertEqual(
            tokenize("제12조 적용 범위", tokenizer_model=FALLBACK_TOKENIZER_MODEL),
            tokenize("제１２조 적용 범위", tokenizer_model=FALLBACK_TOKENIZER_MODEL),
        )
        self.assertIn(
            "제12조",
            tokenize("제１２조", tokenizer_model=FALLBACK_TOKENIZER_MODEL),
        )

    def test_korean_particle_variant_includes_base_noun(self) -> None:
        tokens = tokenize("병가를 사용한 직원")

        self.assertIn("병가", tokens)
        self.assertNotIn("를", tokens)

    def test_article_number_is_preserved(self) -> None:
        tokens = tokenize("제35조에 따른 휴직")

        self.assertIn("제35조", tokens)

    def test_common_predicate_suffix_is_stripped_in_fallback_safe_way(self) -> None:
        tokens = tokenize("직원이 휴직하는 경우")

        self.assertIn("휴직", tokens)


    def test_fast_fallback_does_not_initialize_cold_kiwi(self) -> None:
        with patch("app.retrieval.tokenizer.kiwi_is_ready", return_value=False), patch(
            "app.retrieval.tokenizer._kiwi", side_effect=AssertionError("kiwi should stay cold")
        ):
            tokens = tokenize("육아휴직 신청", prefer_regex_if_kiwi_cold=True)

        self.assertIn("육아휴직", tokens)
        self.assertIn("육아", tokens)
        self.assertIn("휴직", tokens)

    def test_explicit_kiwi_model_ignores_cold_cache_state(self) -> None:
        fake_kiwi = object()
        with patch("app.retrieval.tokenizer.kiwi_is_ready", return_value=False), patch(
            "app.retrieval.tokenizer._kiwi", return_value=fake_kiwi
        ) as kiwi, patch(
            "app.retrieval.tokenizer._kiwi_tokens", return_value=["시행일"]
        ):
            tokens = tokenize("시행일", tokenizer_model=TOKENIZER_MODEL)

        kiwi.assert_called_once_with()
        self.assertIn("시행일", tokens)

    def test_explicit_regex_model_never_initializes_kiwi(self) -> None:
        with patch(
            "app.retrieval.tokenizer._kiwi",
            side_effect=AssertionError("explicit regex model must not initialize kiwi"),
        ):
            tokens = tokenize("육아휴직 신청", tokenizer_model=FALLBACK_TOKENIZER_MODEL)

        self.assertIn("육아휴직", tokens)


    def test_preloaded_batch_tokens_match_per_call_tokens(self) -> None:
        texts = [
            "제12조(휴직) ① 직원이 질병으로 휴직을 신청하면",
            "육아휴직 신청 절차와 수당 지급",
            unicodedata.normalize("NFD", "병가를 사용한 직원"),
            "제１２조의２ 별표 1",
            "",
            "육아휴직 신청 절차와 수당 지급",
        ]
        expected = [
            (tokenize(text, tokenizer_model=TOKENIZER_MODEL), tokenize(text, dedupe=False, tokenizer_model=TOKENIZER_MODEL))
            for text in texts
        ]

        with preloaded_kiwi_tokens(texts):
            actual = [
                (tokenize(text, tokenizer_model=TOKENIZER_MODEL), tokenize(text, dedupe=False, tokenizer_model=TOKENIZER_MODEL))
                for text in texts
            ]
            unlisted = tokenize("목록에 없는 문장도 평소처럼 분석", tokenizer_model=TOKENIZER_MODEL)

        self.assertEqual(expected, actual)
        self.assertEqual(tokenize("목록에 없는 문장도 평소처럼 분석", tokenizer_model=TOKENIZER_MODEL), unlisted)

    def test_recording_collects_unique_kiwi_inputs_without_analyzing(self) -> None:
        with recording_kiwi_inputs() as recorded:
            first = tokenize("휴직 신청", tokenizer_model=TOKENIZER_MODEL)
            tokenize("휴직 신청", tokenizer_model=TOKENIZER_MODEL)
            tokenize(unicodedata.normalize("NFD", "육아휴직"), tokenizer_model=TOKENIZER_MODEL)
            regex_tokens = tokenize("병가 사용", tokenizer_model=FALLBACK_TOKENIZER_MODEL)

        self.assertEqual([], first)
        self.assertEqual(["휴직 신청", "육아휴직"], list(recorded))
        self.assertIn("병가", regex_tokens)
        self.assertIn("휴직", tokenize("휴직 신청", tokenizer_model=TOKENIZER_MODEL))

    def test_preloading_never_replaces_explicit_regex_tokenizer(self) -> None:
        text = "병가를 사용한 직원"
        expected = tokenize(text, tokenizer_model=FALLBACK_TOKENIZER_MODEL)

        with preloaded_kiwi_tokens([text]):
            self.assertEqual(expected, tokenize(text, tokenizer_model=FALLBACK_TOKENIZER_MODEL))

    def test_preloaded_tokens_bypass_kiwi_per_call_analysis(self) -> None:
        from app.retrieval import tokenizer as tokenizer_module

        texts = [
            "제12조(휴직) ① 직원이 질병으로 휴직을 신청하면",
            "육아휴직 신청 절차와 수당 지급",
            unicodedata.normalize("NFD", "병가를 사용한 직원"),
        ]

        with preloaded_kiwi_tokens(texts):
            with patch.object(
                tokenizer_module,
                "_kiwi_tokens",
                wraps=tokenizer_module._kiwi_tokens,
            ) as mock_kiwi_tokens:
                # Call tokenize for each preloaded text
                for text in texts:
                    tokenize(text, tokenizer_model=TOKENIZER_MODEL)
                # _kiwi_tokens should not have been called for preloaded texts
                self.assertEqual(0, mock_kiwi_tokens.call_count)

                # Call tokenize on a text NOT in the preloaded list
                tokenize(
                    "이 문장은 사전로드되지 않았다",
                    tokenizer_model=TOKENIZER_MODEL,
                )
                # Now _kiwi_tokens should have been called once
                self.assertEqual(1, mock_kiwi_tokens.call_count)


class _FakeKiwiItem:
    def __init__(self, form, tag) -> None:
        self.form = form
        self.tag = tag


def _reference_kiwi_items_to_tokens(analyzed) -> list[str]:
    """Verbatim copy of _kiwi_items_to_tokens before the (form, tag) table."""

    tokens: list[str] = []
    for item in analyzed:
        form = str(getattr(item, "form", "") or "").strip().lower()
        tag = str(getattr(item, "tag", "") or "")
        if not form:
            continue
        if tokenizer_module._is_article_no(form) or tag.startswith(tokenizer_module._KEEP_KIWI_TAG_PREFIXES):
            tokens.extend(tokenizer_module._expand_token(form))
    return tokens


class KiwiItemTokenTableTests(unittest.TestCase):
    FORMS = ["점검규정", "제7조", "제 7 조", "휴직", "Rule", "  ", "", None, "으로", "하는", "ABC", "제12조의2", "가" * 129 + "규정"]
    TAGS = ["NNG", "NNP", "VV", "VA", "XR", "SL", "SN", "JKS", "EF", "SF", "", None, "NNG-extra", "J"]

    def tearDown(self) -> None:
        tokenizer_module._KIWI_ITEM_TOKEN_CACHE.clear()
        tokenizer_module._cached_expanded_token.cache_clear()

    def test_items_to_tokens_matches_the_reference_for_all_form_tag_pairs(self) -> None:
        items = [_FakeKiwiItem(form, tag) for form in self.FORMS for tag in self.TAGS]
        for _ in range(2):  # the second pass is answered from the table
            self.assertEqual(_reference_kiwi_items_to_tokens(items), tokenizer_module._kiwi_items_to_tokens(items))
        for item in items:
            self.assertEqual(
                _reference_kiwi_items_to_tokens([item]),
                tokenizer_module._kiwi_items_to_tokens([item]),
                (item.form, item.tag),
            )

    def test_non_string_attributes_and_missing_attributes_take_the_original_path(self) -> None:
        class Odd:
            form = 123
            tag = "NNG"

        class Bare:
            pass

        class StrSub(str):
            pass

        items = [Odd(), Bare(), _FakeKiwiItem(StrSub("점검규정"), "NNG"), _FakeKiwiItem("점검규정", StrSub("NNG"))]
        self.assertEqual(_reference_kiwi_items_to_tokens(items), tokenizer_module._kiwi_items_to_tokens(items))

    def test_table_is_bounded_and_returns_fresh_lists(self) -> None:
        with patch.object(tokenizer_module, "_KIWI_ITEM_TOKEN_CACHE_MAX", 3):
            items = [_FakeKiwiItem(f"단어{index}규정", "NNG") for index in range(20)]
            self.assertEqual(_reference_kiwi_items_to_tokens(items), tokenizer_module._kiwi_items_to_tokens(items))
            self.assertLessEqual(len(tokenizer_module._KIWI_ITEM_TOKEN_CACHE), 3)
        first = tokenizer_module._kiwi_items_to_tokens([_FakeKiwiItem("점검규정", "NNG")])
        first.append("mutated")
        self.assertEqual(
            ["점검규정", "점검", "규정"],
            tokenizer_module._kiwi_items_to_tokens([_FakeKiwiItem("점검규정", "NNG")]),
        )


if __name__ == "__main__":
    unittest.main()
