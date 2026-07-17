from __future__ import annotations

import unittest

from scripts.build_kordoc_table_review_packet import triage_table


class KordocTableReviewPacketTests(unittest.TestCase):
    def test_triage_marks_dotted_leaders_as_toc(self) -> None:
        label, reason, action = triage_table(
            {
                "row_count": 2,
                "column_count": 3,
                "cell_count": 6,
                "cell_rows": [
                    {"raw": "Part I"},
                    {"raw": "1. Duty ...... 10"},
                ],
            }
        )

        self.assertEqual(label, "probable_toc_table")
        self.assertIn("Do not merge", action)

    def test_triage_marks_article_prose_as_false_positive(self) -> None:
        label, reason, action = triage_table(
            {
                "row_count": 2,
                "column_count": 2,
                "cell_count": 4,
                "cell_rows": [
                    {
                        "raw": (
                            "Article 1 shall apply to every case and must be reviewed "
                            "before registration. <amended 2026.04.30>"
                        )
                    },
                    {"raw": "Article 2 must follow the same process."},
                ],
            }
        )

        self.assertEqual(label, "probable_prose_false_positive")
        self.assertIn("Do not auto-merge", action)

    def test_triage_marks_header_like_multi_column_table_as_candidate(self) -> None:
        label, reason, action = triage_table(
            {
                "row_count": 3,
                "column_count": 3,
                "cell_count": 9,
                "cell_rows": [
                    {"cells": ["Category", "Criteria", "Amount"], "raw": "Category | Criteria | Amount"},
                    {"cells": ["A", "Domestic", "100"], "raw": "A | Domestic | 100"},
                ],
            }
        )

        self.assertEqual(label, "structured_table_candidate")
        self.assertIn("review_required", action)


if __name__ == "__main__":
    unittest.main()
