from __future__ import annotations

import unittest

from scripts.build_kordoc_local_table_match_packet import classify_match, match_score, tokenize


class KordocLocalTableMatchPacketTests(unittest.TestCase):
    def test_tokenize_adds_compact_korean_ngrams(self) -> None:
        tokens = tokenize("직무발명 보상금 지급기준")

        self.assertIn("직무발명", tokens)
        self.assertIn("직무발", tokens)

    def test_match_score_rewards_shared_cells_and_numbers(self) -> None:
        score = match_score(
            "직무발명 보상금 지급기준 보상구분 지급금액 출원 10만원",
            "보상구분 | 지급기준 | 지급금액 / 출원 | 국내 | 10만원",
        )

        self.assertGreaterEqual(score, 30)

    def test_match_score_rejects_unrelated_tables(self) -> None:
        score = match_score(
            "직무발명 보상금 지급기준 보상구분 지급금액",
            "근무기간 | 연차휴가 | 휴직 | 복무",
        )

        self.assertLess(score, 10)

    def test_classify_requires_higher_score_for_strong_match(self) -> None:
        self.assertEqual(classify_match(46, "structured_table_candidate"), "strong_review_match")
        self.assertEqual(classify_match(34, "possible_table_candidate"), "medium_review_match")
        self.assertEqual(classify_match(20, "structured_table_candidate"), "weak_review_match")
        self.assertEqual(classify_match(5, "structured_table_candidate"), "no_confident_match")


if __name__ == "__main__":
    unittest.main()
