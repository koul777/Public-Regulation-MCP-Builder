from __future__ import annotations

import unittest
from unittest.mock import patch

from app.retrieval.tokenizer import FALLBACK_TOKENIZER_MODEL, TOKENIZER_MODEL, tokenize


class RetrievalTokenizerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
