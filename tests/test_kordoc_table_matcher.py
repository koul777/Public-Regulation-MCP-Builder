from __future__ import annotations

import unittest

from app.processors.kordoc_table_matcher import (
    attach_kordoc_table_matches,
    best_kordoc_match,
    classify_match,
    match_score,
    mergeable_kordoc_tables,
    prepare_kordoc_table_match_index,
    triage_kordoc_table,
    tokenize,
)
from app.schemas.chunk import Chunk


class KordocTableMatcherTests(unittest.TestCase):
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

    def test_classify_requires_higher_score_for_strong_match(self) -> None:
        self.assertEqual(classify_match(46, "structured_table_candidate"), "strong_review_match")
        self.assertEqual(classify_match(34, "possible_table_candidate"), "medium_review_match")
        self.assertEqual(classify_match(20, "structured_table_candidate"), "weak_review_match")
        self.assertEqual(classify_match(5, "structured_table_candidate"), "no_confident_match")

    def test_aks_regulation_headers_make_kordoc_table_mergeable(self) -> None:
        label, _, _ = triage_kordoc_table(
            {
                "row_count": 4,
                "column_count": 2,
                "cell_count": 8,
                "cell_rows": [
                    {"cells": ["경력종별", "환산율"], "raw": "경력종별 | 환산율"},
                    {"cells": ["대학 연구기관", "100%"], "raw": "대학 연구기관 | 100%"},
                ],
            }
        )

        self.assertEqual("structured_table_candidate", label)

    def test_attach_match_keeps_table_rows_provisional(self) -> None:
        chunk = Chunk(
            chunk_id="chunk_table",
            document_id="doc",
            chunk_type="paragraph",
            text="직무발명 보상금 지급기준 보상구분 지급금액 출원 10만원",
            normalized_text="직무발명 보상금 지급기준 보상구분 지급금액 출원 10만원",
            metadata={"table_like": True, "table_classification": "probable_table_extraction_failed"},
        )
        inventory = {
            "status": "parsed",
            "tables": [
                {
                    "table_index": 1,
                    "row_count": 3,
                    "column_count": 3,
                    "cell_count": 9,
                    "cell_rows": [
                        {"cells": ["보상구분", "지급기준", "지급금액"], "raw": "보상구분 | 지급기준 | 지급금액"},
                        {"cells": ["출원", "국내", "10만원"], "raw": "출원 | 국내 | 10만원"},
                    ],
                }
            ],
        }

        attach_kordoc_table_matches([chunk], inventory)

        self.assertIn("kordoc_table_match", chunk.metadata)
        self.assertEqual(
            chunk.metadata["kordoc_table_match"]["match_strength"],
            chunk.metadata["kordoc_table_match"]["match_label"],
        )
        self.assertTrue(chunk.metadata["kordoc_table_match_review_required"])
        self.assertTrue(chunk.metadata["kordoc_table_match_provisional"])
        self.assertNotIn("table_cell_rows", chunk.metadata)

    def test_prepared_match_index_preserves_best_match(self) -> None:
        chunk = Chunk(
            chunk_id="chunk_table",
            document_id="doc",
            chunk_type="table",
            text="보상구분 지급기준 지급금액 출원 국내 10만원",
            normalized_text="보상구분 지급기준 지급금액 출원 국내 10만원",
            metadata={"table_like": True},
            source_page_start=7,
        )
        tables = [
            {
                "table_index": 1,
                "row_count": 2,
                "column_count": 3,
                "cell_count": 6,
                "source_page": 3,
                "codex_triage_label": "structured_table_candidate",
                "cell_rows": [
                    {"cells": ["무관", "내용"], "raw": "무관 | 내용"},
                    {"cells": ["다른", "표"], "raw": "다른 | 표"},
                ],
            },
            {
                "table_index": 2,
                "row_count": 3,
                "column_count": 3,
                "cell_count": 9,
                "source_page": 7,
                "codex_triage_label": "structured_table_candidate",
                "cell_rows": [
                    {"cells": ["보상구분", "지급기준", "지급금액"], "raw": "보상구분 | 지급기준 | 지급금액"},
                    {"cells": ["출원", "국내", "10만원"], "raw": "출원 | 국내 | 10만원"},
                ],
            },
        ]

        raw_table, raw_score, raw_label = best_kordoc_match(chunk, tables)
        prepared_table, prepared_score, prepared_label = best_kordoc_match(
            chunk, prepare_kordoc_table_match_index(tables)
        )

        self.assertEqual(raw_table["table_index"], prepared_table["table_index"])
        self.assertEqual(raw_score, prepared_score)
        self.assertEqual(raw_label, prepared_label)
        self.assertEqual(2, prepared_table["table_index"])

    def test_short_multi_column_kordoc_table_is_merge_candidate(self) -> None:
        table = {
            "table_index": 1,
            "row_count": 2,
            "column_count": 5,
            "cell_count": 10,
            "cell_rows": [
                {
                    "cells": ["예산액", "1억원미만", "1억~5억원 미만", "5억~10억원 미만", "10억원 이상"],
                    "raw": "예산액 | 1억원미만 | 1억~5억원 미만 | 5억~10억원 미만 | 10억원 이상",
                },
                {
                    "cells": ["평가위원 수", "4명 이상", "5명 이상", "6명 이상", "7명 이상"],
                    "raw": "평가위원 수 | 4명 이상 | 5명 이상 | 6명 이상 | 7명 이상",
                },
            ],
        }

        label, reason, action = triage_kordoc_table(table)
        mergeable = mergeable_kordoc_tables({"tables": [table]})

        self.assertEqual("possible_table_candidate", label)
        self.assertIn("Short multi-column", reason)
        self.assertIn("AI comparison", action)
        self.assertEqual(1, len(mergeable))
        self.assertEqual(1, mergeable[0]["table_index"])


if __name__ == "__main__":
    unittest.main()
