from __future__ import annotations

import json
from pathlib import Path
import unittest

from app.processors.table_extractor import TableExtractor


class TableExtractorTests(unittest.TestCase):
    @staticmethod
    def _load_jsonl_normalized_text(relative_path: str, line_number: int) -> str:
        path = Path(__file__).resolve().parents[1] / relative_path
        with path.open(encoding="utf-8") as handle:
            for current_line, line in enumerate(handle, start=1):
                if current_line == line_number:
                    return json.loads(line)["normalized_text"]
        raise AssertionError(f"Line {line_number} not found in {path}")

    def test_detects_table_like_appendix_rows(self) -> None:
        text = "\n".join(
            [
                "[별표 1]",
                "재산명 수량 평가액(원) 비고",
                "토지 181,425.08 2,646,882,423",
                "건물 65,386.94 93,368,436,537",
                "총계 96,015,318,960",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertTrue(analysis["table_like"])
        self.assertGreaterEqual(analysis["table_confidence"], 0.55)
        self.assertIn("table_markdown", analysis)
        self.assertGreaterEqual(len(analysis["table_rows"]), 3)
        self.assertGreaterEqual(analysis["table_column_count"], 3)
        self.assertTrue(analysis["table_cell_rows"])

    def test_plain_article_is_not_table_like(self) -> None:
        text = "제1조(목적) 이 규정은 복무에 관한 사항을 정함을 목적으로 한다."

        analysis = TableExtractor().analyze_text(text, "article")

        self.assertFalse(analysis["table_like"])

    def test_extracts_cell_rows_from_pdf_style_numeric_table(self) -> None:
        text = "\n".join(
            [
                "\uc7ac \uc0b0 \uba85 \uc218\ub7c9 \ud3c9\uac00\uc561(\uc6d0) \ube44 \uace0",
                "\ud1a0 \uc9c0 181,425.08\u33a1 2,646,882,423",
                "\uac74 \ubb3c 65,386.94\u33a1 93,368,436,537",
                "\ucd1d \uacc4 96,015,318,960",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertTrue(analysis["table_like"])
        self.assertGreaterEqual(analysis["table_structured_row_count"], 3)
        self.assertGreaterEqual(analysis["table_column_count"], 3)
        self.assertIn("table_cell_rows", analysis)
        self.assertIn("181,425.08", analysis["table_cell_rows"][1]["raw"])

    def test_performance_grade_table_survives_without_headcount_row(self) -> None:
        rows = ["등 급", "S", "A", "B", "지급률", "134%", "115%", "100%"]

        cell_rows = TableExtractor().extract_cell_rows(rows, "appendix")

        cells = [row["cells"] for row in cell_rows]
        self.assertIn(["등급", "S", "A", "B"], cells)
        self.assertIn(["지급률", "134%", "115%", "100%"], cells)

    def test_does_not_structure_appendix_title_or_revision_history_as_table_rows(self) -> None:
        rows = [
            "[\ubcc4\ud45c] <\uc2e0\uc124 2018. 4. 24.>",
            "\uacfc\ud0dc\ub8cc\uc758 \ubd80\uacfc\uae30\uc900(\uc81c15\uc870 \uad00\ub828)",
            "\uc81c\uc815 1980. 9. 1. \uaddc\uc815 \uc81c63\ud638 \uac1c\uc815 1981. 5. 12. \uaddc\uc815 \uc81c83\ud638",
            "\ub54c\ub85c\ubd80\ud130 7\uc77c \uc774\ub0b4\uc5d0 \uc6d0\uc7a5\uc5d0\uac8c \uc7ac\uc2ec\uc758\ub97c \uc694\uccad\ud560 \uc218 \uc788\ub2e4. \u2461 \uc81c1\ud56d\uc758 \uc694\uccad\uc774 \uc788\ub294 \uacbd\uc6b0",
            "\uc704\ubc18\ud589\uc704 1\ucc28 2\ucc28 3\ucc28",
            "\ubc95 \uc81c11\uc870\uc81c 1\ud56d 100\ub9cc\uc6d0 150\ub9cc\uc6d0 200\ub9cc\uc6d0",
        ]

        cell_rows = TableExtractor().extract_cell_rows(rows)

        self.assertEqual(len(cell_rows), 1)
        self.assertEqual(cell_rows[0]["raw"], "\ubc95 \uc81c11\uc870\uc81c 1\ud56d 100\ub9cc\uc6d0 150\ub9cc\uc6d0 200\ub9cc\uc6d0")

    def test_appendix_revision_title_only_does_not_count_dates_as_table_rows(self) -> None:
        text = "\n".join(
            [
                "[\ubcc4\ud45c2] <\uac1c\uc815 2013.12.31./2014.5.23./2014.5.29.>",
                "2016.12.30./2017.2.28./2017.9.28./2018.7.24./2019.1.30./2019.8.1./",
                "2020.12.24./2021.12.24./2022.1.25./2022.12.26.>",
                "\uc5c5 \ubb34 \ubd84 \uc7a5 \ud45c",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertFalse(analysis["table_like"])
        self.assertFalse(analysis["table_review_required"])
        self.assertEqual(analysis["table_numeric_rows"], 0)
        self.assertEqual(analysis["table_classification"], "not_table_like")

    def test_synthetic_appendix_prose_list_is_not_promoted_to_a_table(self) -> None:
        text = "\n".join(
            [
                "Appendix A",
                "- First checklist item",
                "- Second checklist item",
                "- Third checklist item",
                "- Fourth checklist item",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertFalse(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "not_table_like")

    def test_synthetic_vertical_checklist_is_structured(self) -> None:
        text = "\n".join(
            [
                "Type",
                "Details",
                "Hazard",
                "Required safety check",
                "Location",
                "Required safety check",
                "Owner",
                "Required safety check",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertTrue(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "structured_table")
        self.assertGreaterEqual(analysis["table_structured_row_count"], 2)
        self.assertTrue(analysis["table_review_required"])
        self.assertIn("vertical_checklist_row", analysis["table_review_flags"])

    def test_demotes_revision_and_article_prose_false_positive(self) -> None:
        text = "\n".join(
            [
                "\uc81c580\ud638",
                "\uac1c\uc815 2010. 1. 29. \uaddc\uc815 \uc81c780\ud638",
                "\uac1c\uc815 2015. 1. 5. \uaddc\uc815 \uc81c907\ud638",
                "\uac1c\uc815 2024. 12. 27. \uaddc\uc815 \uc81c1233\ud638",
                "\uc81c1\uc870(\ubaa9\uc801) \uc774 \uaddc\uc815\uc740 \uc774\uc0ac\ud68c\uc758 \uc6b4\uc601\uc5d0 \uad00\ud55c \uc0ac\ud56d\uc744 \uaddc\uc815\ud568\uc744 \ubaa9\uc801\uc73c\ub85c \ud55c\ub2e4.",
                "\uc81c2\uc870(\uc801\uc6a9 \ubc94\uc704) \uc774\uc0ac\ud68c\uc758 \uc6b4\uc601\uc5d0 \uad00\ud558\uc5ec \ubc95\ub839 \ubc0f \uc815\uad00 \ub4f1\uc5d0 \ub530\ub85c \uc815\ud55c \uac83\uc744 \uc81c\uc678\ud558\uace0\ub294 \uc774 \uaddc\uc815\uc5d0 \uc758\ud55c\ub2e4.",
                "\uc81c3\uc870(\ud68c\uc758 \uc885\ub958) \uc774\uc0ac\ud68c\ub294 \uc815\uae30\ud68c\uc640 \uc784\uc2dc\ud68c\ub85c \uad6c\ubd84\ud55c\ub2e4.",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertFalse(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "probable_false_positive_prose_revision")
        self.assertTrue(analysis["table_probable_false_positive"])

    def test_keeps_compact_table_candidate_for_review(self) -> None:
        text = "\n".join(
            [
                "\ud559\ubd80 \uc804\uacf5 \uc785\ud559\uc815\uc6d0",
                "\uae00\ub85c\ubc8c\ud55c\uad6d\ud559\ubd80 \ud55c\uad6d\ubb38\ud654\ud559 35\uba85",
                "\uad6d\uc81c\ud55c\uad6d\ud559\ubd80 \ud55c\uad6d\uc5b4\uad50\uc721\ud559 25\uba85",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertTrue(analysis["table_like"])
        self.assertGreaterEqual(analysis["table_structured_row_count"], 2)
        self.assertEqual(analysis["table_classification"], "structured_table")

    def test_org_quota_dense_inline_rows_are_reconstructed_for_review(self) -> None:
        text = "\n".join(
            [
                "[\ubcc4\ud45c3] <\uac1c\uc815 2013.12.31./2014.5.23.>",
                "\uc815 \uc6d0 \ud45c",
                "1. \ucd1d\uad04\ud45c",
                "\uad6c \ubd84 \uc784\uc6d0",
                "\uc77c \ubc18 \uc9c1",
                "\uae30\ub2a5\uc9c1 \uad00\uad11",
                "\ud1b5\uc5ed\uc9c1 \ud569\uacc4 1\uae09 2\uae09 3\uae09",
                "4.5\uae09\uc18c\uacc4",
                "\uc784 \uc6d0 5 5 \ube44\uc11c\ud300 3 3 2 5 \ud64d\ubcf4\uc2e4 7 7 7 \uac10\uc0ac\uc2e4 8 8 8",
                "\ub514\uc9c0\ud138\ud601\uc2e0\uc2e4 27 27 1 28 \uad00\uad11\ube45\ub370\uc774\ud130\uc2e4 23 23 1 24 \uc18c\uacc4 94 94 3 97",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertTrue(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "structured_table")
        self.assertTrue(analysis["table_review_required"])
        self.assertIn("dense_numeric_row_reconstruction", analysis["table_review_flags"])
        self.assertGreaterEqual(analysis["table_record_count"], 5)
        self.assertIn(
            ["\ube44\uc11c\ud300", "3", "3", "2", "5"],
            [row["cells"] for row in analysis["table_cell_rows"]],
        )

    def test_spaced_form_header_is_structured(self) -> None:
        text = "\n".join(
            [
                "\uad6c \ubd84 \uc131 \uba85 \uc0dd\ub144\uc6d4\uc77c \uc9c1 \uc704",
                "\uc704\uc784\uc778 \ud64d\uae38\ub3d9 1980.1.1 \ud300\uc7a5",
                "\ub300\ub9ac\uc778 \uae40\ud55c\uad6d 1990.2.2 \ub300\ub9ac",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "form")

        self.assertTrue(analysis["table_like"])
        self.assertTrue(analysis["table_cell_rows"])
        self.assertEqual(analysis["table_classification"], "structured_table")

    def test_two_cell_public_form_label_rows_are_structured(self) -> None:
        text = "\n".join(
            [
                "\ud559\ubd80/\uc804\uacf5 \uc5f0\ub77d\ucc98(\ud578\ub4dc\ud3f0)",
                "\ud559\ubc88 \ud559\uc704\ucde8\ub4dd(\uc608\uc815)\uc77c OOOO \ub144 O \uc6d4",
                "\uc131\uba85 \ud1b5\uc0b0\uacf5\uac1c\uc720\uc608\uae30\uac04",
                "\uc9c0\ub3c4\uad50\uc218\uba85 \ube44\uace0",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "form")

        self.assertTrue(analysis["table_like"])
        self.assertGreaterEqual(analysis["table_structured_row_count"], 3)
        self.assertGreaterEqual(analysis["table_column_count"], 2)
        self.assertEqual(analysis["table_classification"], "structured_table")

    def test_colon_label_public_form_rows_are_structured(self) -> None:
        text = "\n".join(
            [
                "\uac74\ubb3c\uba85 :",
                "\uc131 \uba85 : (\uc778)",
                "\uc8fc \uc18c :",
                "\ubcc0 \uc0c1 \uae08 \uc561 :",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "form")

        self.assertTrue(analysis["table_like"])
        self.assertGreaterEqual(analysis["table_structured_row_count"], 3)
        self.assertEqual(analysis["table_cell_rows"][0]["cells"][0], "\uac74\ubb3c\uba85")

    def test_time_schedule_rows_are_structured(self) -> None:
        text = "\n".join(
            [
                "\uad6c\ubd84 \ucd9c\ud1f4\uadfc\uc2dc\uac04 \uadfc\ubb34\uc2dc\uac04",
                "\uc6d4 00:00 ~ 00:00 00\uc2dc\uac04 00\ubd84",
                "\ud654 00:00 ~ 00:00 00\uc2dc\uac04 00\ubd84",
                "A-1 08 : 00 11 : 30 \ub3c4\uc911 \ud734\uac8c\uc2dc\uac04 30\ubd84 \ubd80\uc5ec",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertTrue(analysis["table_like"])
        self.assertGreaterEqual(analysis["table_structured_row_count"], 3)

    def test_numbered_quantity_appendix_rows_are_structured(self) -> None:
        text = "\n".join(
            [
                "\uc784\uc6a9 \uad6c\ube44 \uc11c\ub958",
                "1. \uc774\ub825\uc11c 1\ub9e4",
                "2. \uae30\ubcf8\uc99d\uba85\uc11c 1\ud1b5",
                "3. \uc8fc\ubbfc\ub4f1\ub85d\ub4f1\ubcf8 1\ud1b5",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertTrue(analysis["table_like"])
        self.assertGreaterEqual(analysis["table_structured_row_count"], 3)

    def test_leave_days_by_tenure_rows_are_structured(self) -> None:
        text = "\n".join(
            [
                "[\ubcc4\ud45c 1] <\uac1c\uc815 2026. 6. 30.>",
                "\uc784\uc6d0\uc758 \uc5f0\ucc28\ud734\uac00 \ubd80\uc5ec \uae30\uc900",
                "\uadfc\uc18d\ub144\uc218 \uc5f0\ucc28\ud734\uac00 \uc77c\uc218",
                "1\uac1c\uc6d4 \uc774\uc0c1 1\ub144 \ubbf8\ub9cc 11",
                "1\ub144 \uc774\uc0c1 2\ub144 \ubbf8\ub9cc 12",
                "2\ub144 \uc774\uc0c1 3\ub144 \ubbf8\ub9cc 14",
                "3\ub144 \uc774\uc0c1 4\ub144 \ubbf8\ub9cc 15",
                "4\ub144 \uc774\uc0c1 5\ub144 \ubbf8\ub9cc 17",
                "5\ub144 \uc774\uc0c1 6\ub144 \ubbf8\ub9cc 20",
                "6\ub144 \uc774\uc0c1 21",
                "\u203b \uc0c1\uae30 \uae30\uc900 \ud734\uac00\uc77c\uc218\ub294 \uad6d\uac00\uacf5\ubb34\uc6d0 \ubcf5\ubb34\uaddc\uc815\uc744 \uc900\uc6a9\ud55c\ub2e4.",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertTrue(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "structured_table")
        self.assertEqual(analysis["table_cell_rows"][0]["cells"], ["\uadfc\uc18d\ub144\uc218", "\uc5f0\ucc28\ud734\uac00 \uc77c\uc218"])
        self.assertIn(["6\ub144 \uc774\uc0c1", "21"], [row["cells"] for row in analysis["table_cell_rows"]])
        self.assertTrue(
            any(
                record["record"] == {"\uadfc\uc18d\ub144\uc218": "6\ub144 \uc774\uc0c1", "\uc5f0\ucc28\ud734\uac00 \uc77c\uc218": "21"}
                for record in analysis["table_records"]
            )
        )

    def test_dense_spaced_public_form_header_is_structured(self) -> None:
        text = "\n".join(
            [
                "\ucd9c\uc785\ud1b5\uc81c\ub300\uc7a5",
                "\ub144\uc6d4\uc77c",
                "\ucd9c\uc785\uc2dc\uac04 \ubc0f",
                "\ud1f4 \uc2e4 \uc2dc \uac04 \uc6a9 \ubb34 \ucd9c \uc785 \uc790 \uc785 \ud68c \uc790 \ube44 \uace0 \uc18c\uc18d(\uc8fc\uc18c) \uc131 \uba85 \uc9c1 \uae09 \uc131 \uba85",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "form")

        self.assertTrue(analysis["table_like"])
        self.assertTrue(analysis["table_cell_rows"])

    def test_contract_form_prose_is_demoted_when_no_cell_rows(self) -> None:
        text = "\n".join(
            [
                "\u2461 (\uc744)\uacfc (\ubcd1)\uc740 \uc5f0\uad6c\ubd80\uc815\ud589\uc704\ub97c \ubc29\uc9c0\ud558\uace0 \uc5f0\uad6c\uc724\ub9ac\ub97c \ud655\ubcf4\ud558\ub3c4\ub85d",
                "\ub178\ub825\ud558\uc5ec\uc57c \ud55c\ub2e4.",
                "\uc81c13\uc870(\ud574\uc11d) \ubcf8 \ud611\uc57d\uc11c\uc758 \ud574\uc11d\uc0c1 \uc758\ubb38\uc774 \uc788\uc744 \uacbd\uc6b0\uc5d0\ub294 (\uac11)\uc758 \ud574\uc11d\uc5d0 \uc758\ud55c\ub2e4.",
                "\uc81c14\uc870(\ud611\uc57d\uc758 \ud6a8\ub825 \ubc1c\uc0dd) \ubcf8 \ud611\uc57d\uc11c\ub294 2\ud1b5\uc744 \uc791\uc131\ud558\uc5ec (\uac11), (\uc744)\uc774 \uac01\uac01 1\ud1b5\uc529 \ubcf4\uad00\ud55c\ub2e4.",
                "\ubd99\uc784 1. \uc5f0\uad6c\uacc4\ud68d\uc11c 1\ubd80.",
                "\ub144 \uc6d4 \uc77c",
                "(\uac11) (\ud55c\uad6d\ud559\uc911\uc559\uc5f0\uad6c\uc6d0\uc7a5) (\uc9c1\uc778)",
                "(\uc744) (\uc5f0\uad6c\ucc45\uc784\uc790\uac00 \uc18d\ud55c \uae30\uad00\uc758 \uc7a5) (\uc9c1\uc778)",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "form")

        self.assertFalse(analysis["table_like"])
        self.assertIn(analysis["table_classification"], {"not_table_like", "probable_false_positive_prose_revision"})

    def test_appendix_amendment_example_is_demoted(self) -> None:
        text = "\n".join(
            [
                "[\ubcc4\ud45c 1] <20AA.B.CC.>",
                "[\uc608] \ubcc4\ud45c 5\ub97c \ubcc4\ud45c 6\uc73c\ub85c 20AA.B.CC.\uc77c \uc774\ub3d9\ud558\uace0, \uadf8 \ud45c\ub97c 20DD.E.F.\uc77c,",
                "20GG.H.I\uc77c \uac1c\uc815\ud55c \uacbd\uc6b0",
                "1) [\ubcc4\ud45c 5] <[\ubcc4\ud45c 6]\uc73c\ub85c \uc774\ub3d9 20AA.B.CC>",
                "2) [\ubcc4\ud45c 6] <[\ubcc4\ud45c 5]\uc5d0\uc11c \uc774\ub3d9 20AA.B.CC> <2023.DD.E.F, 20GG.H.I.>",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertFalse(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "probable_false_positive_prose_list")

    def test_record_retention_fragment_rows_are_structured(self) -> None:
        text = "\n".join(
            [
                "\ubb38\uc11c\ub85c\uc11c 4\ub144 \uc774\uc0c1 5\ub144 \uc774\ud558\uc758 \uae30\uac04 \ub3d9\uc548 \ubcf4\uc874\ud560 \ud544\uc694\uac00 \uc788\ub294 \ubb38\uc11c",
                "4-5-2 \uae30\ub85d\ubb3c\uad00\ub9ac\uaddc\uc815",
                "3\ub144 \ubcf4\uc874",
                "\u25aa\ucc98\ub9ac\uacfc \uc218\uc900\uc758 \uc77c\uc0c1\uc5c5\ubb34\uc5d0 \uad00\ud55c \uae30\ub85d\ubb3c",
                "1\ub144 \ubcf4\uc874",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertTrue(analysis["table_like"])
        self.assertGreaterEqual(analysis["table_structured_row_count"], 3)
        self.assertIn(["\ubcf4\uc874\uae30\uac04", "3\ub144\ubcf4\uc874"], [row["cells"] for row in analysis["table_cell_rows"]])

    def test_conflict_definition_form_prose_is_demoted(self) -> None:
        text = "\n".join(
            [
                "\u2463 \u201c\uc9c1\ubb34\uad00\ub828\uc790\u201d\ub294\u300c\uacf5\uc9c1\uc790\uc758 \uc774\ud574\ucda9\ub3cc \ubc29\uc9c0\ubc95\u300d\uc81c2\uc870\uc81c5\ud638\uc5d0 \ub530\ub978 \ub2e4\uc74c \uac01 \ubaa9\uc758 \uc5b4\ub290 \ud558\ub098\uc5d0 \ud574\ub2f9\ud558\ub294 \uac1c\uc778\u00b7\ubc95\uc778\u00b7\ub2e8\uccb4 \ubc0f \uacf5\uc9c1\uc790\ub97c \uc801\uc2b5\ub2c8\ub2e4.",
                "\uac00. \uacf5\uc9c1\uc790\uc758 \uc9c1\ubb34\uc218\ud589\uacfc \uad00\ub828\ud558\uc5ec \uc77c\uc815\ud55c \ud589\uc704\ub098 \uc870\uce58\ub97c \uc694\uad6c\ud558\ub294 \uac1c\uc778\uc774\ub098 \ubc95\uc778 \ub610\ub294 \ub2e8\uccb4",
                "\ub098. \uacf5\uc9c1\uc790\uc758 \uc9c1\ubb34\uc218\ud589\uacfc \uad00\ub828\ud558\uc5ec \uc774\uc775 \ub610\ub294 \ubd88\uc774\uc775\uc744 \uc9c1\uc811\uc801\uc73c\ub85c \ubc1b\ub294 \uac1c\uc778\uc774\ub098 \ubc95\uc778 \ub610\ub294 \ub2e8\uccb4",
                "210mm\u00d7297mm[\uc77c\ubc18\uc6a9\uc9c0 60g/\u33a1(\uc7ac\ud65c\uc6a9\ud488)]",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "form")

        self.assertFalse(analysis["table_like"])
        self.assertIn(analysis["table_classification"], {"not_table_like", "probable_false_positive_prose_list"})

    def test_budget_guideline_bullet_prose_is_demoted(self) -> None:
        text = "\n".join(
            [
                "(5) \uae30\ud0c0",
                "\u25a1 \uc9c1\uc81c\uc0c1 \uc815\uc6d0 \uc678\uc758 \uc9c1\uc6d0\uc5d0 \ub300\ud55c \uc778\uac74\ube44\ub294 \uc7a1\uae09\uc5d0 \uacc4\uc0c1\ud558\uace0, \ub2e4\ub978 \ube44\ubaa9\uc5d0 \uacc4\uc0c1\ud560 \uc218 \uc5c6\ub2e4.",
                "\u25a1 \uacbd\uc601\ud3c9\uac00 \uc131\uacfc\uae09 \ubc0f \uc9c1\ubb34\uc218\ud589\uc2e4\uc801 \ud3c9\uac00 \uc131\uacfc\uae09\uc758 \uae30\uc900\ub144\ub3c4\ub294 2020\ub144\ub3c4\ub85c \ud558\uba70, \uae30\ubcf8\uc5f0\ubd09, \uc6d4 \uae30\ubcf8\uae09, \uae30\uc900\uc6d4\ubd09 \uc0b0\ucd9c\uc740 [\ubd99\uc7841]\uc744 \ub530\ub978\ub2e4.",
                "\U000f02b4 \uacbd \ube44",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "paragraph")

        self.assertFalse(analysis["table_like"])
        self.assertIn(analysis["table_classification"], {"probable_false_positive_budget_prose", "probable_false_positive_prose_list"})
        self.assertEqual(analysis["table_false_positive_stability"], "stable")

    def test_truncated_budget_intro_bullet_prose_is_demoted(self) -> None:
        text = "\n".join(
            [
                "\u3147 \u2018\uc77c\uc790\ub9ac \uc815\ucc45 5\ub144 \ub85c\ub4dc\ub9f5(\u201917.10\uc6d4)\u2019\uc5d0 \ub530\ub978 \ube44\uc815\uaddc\uc9c1\u00b7\uac04\uc811\uace0\uc6a9 \uadfc\ub85c\uc790\uc758 \uc815\uaddc\uc9c1 \uc804\ud658 \uacc4\ud68d\uc744 \ucc28\uc9c8 \uc5c6\uc774 \ucd94\uc9c4\ud558\uc5ec \uc77c\uc790\ub9ac\uc758 \uc9c8\uc744 \uac1c\uc120\ud55c\ub2e4. \u3147 \uae30\uad00\ub0b4 \uacfc\ub3c4\ud55c \uc784\uae08\uaca9\ucc28 \ubc0f \ubd88\ud569\ub9ac\ud55c \uc784\uae08\ucc28\ubcc4\uc774 \ubc1c\uc0dd\ud558\uc9c0 \uc54a\ub3c4\ub85d \uc784\uae08\uccb4\uacc4\ub97c \uac1c\uc120\ud558\uace0, \uc9c1\ubb34\u00b7\uc9c1\uae09\u00b7\uc9c1\uc885\ubcc4 \uc784\uae08 \uc778\uc0c1\ub960\uc744 \ud569\ub9ac\uc801\uc73c\ub85c \uad00\ub9ac\ud55c\ub2e4.",
                "\u2161",
                "\uc8fc\uc694 \ud56d\ubaa9\ubcc4 \uc9c0\uce68",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "paragraph")

        self.assertFalse(analysis["table_like"])

    def test_budget_guideline_cover_intro_is_demoted(self) -> None:
        text = "\n".join(
            [
                "2018\ub144\ub3c4",
                "\uacf5\uae30\uc5c5\u00b7\uc900\uc815\ubd80\uae30\uad00 \uc608\uc0b0\uc9d1\ud589\uc9c0\uce68",
                "\uae30 \ud68d \uc7ac \uc815 \ubd80",
                "\u2160. \uc77c\ubc18\uc9c0\uce68",
                "\u25c7 \uacbd\uae30\ud65c\uc131\ud654\ub97c \uc704\ud55c \uc608\uc0b0 \uc870\uae30\uc9d1\ud589 \ubc0f \ud22c\uc790\ud655\ub300\uc5d0 \uc911\uc810\uc744 \ub450\uc5b4 \uc608\uc0b0\uc9d1\ud589",
                "\u25c7 \uacf5\uacf5\uae30\uad00\uc758 \uc0ac\ud68c\uc801 \uac00\uce58 \uc2e4\ud604 \ubc0f \uacf5\uacf5\uc131 \uc81c\uace0\uc5d0 \uae30\uc5ec",
                "\u25a1 \uac01 \uacf5\uacf5\uae30\uad00\uc740 \ub300\ub0b4\uc678 \uacbd\uae30\ubcc0\ub3d9\uc5d0 \uc120\uc81c\uc801\uc73c\ub85c \ub300\uc751\ud558\uace0, \uc11c\ubbfc\uc0dd\ud65c \uc548\uc815\uc744 \uc9c0\uc6d0\ud558\uae30 \uc704\ud574 \uc608\uc0b0\uc758 \uc870\uae30\uc9d1\ud589\uc744 \ucd94\uc9c4\ud55c\ub2e4.",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "paragraph")

        self.assertFalse(analysis["table_like"])
        self.assertIn(analysis["table_classification"], {"not_table_like", "probable_false_positive_cover_prose"})
        if analysis["table_classification"] == "probable_false_positive_cover_prose":
            self.assertEqual(analysis["table_false_positive_stability"], "stable")

    def test_vertical_report_length_table_is_structured(self) -> None:
        text = "\n".join(
            [
                "2. \uaddc \uaca9",
                "\uac00. \ubcf8\ubcf4\uace0\uc11c\ub294 \uac00\ub85c21cm\u00d7\uc138\ub85c29.7cm(A4\uc6a9\uc9c0\ud06c\uae30)\uc758 \uaddc\uaca9\uc73c\ub85c \uc88c\ucca0\ud558\uba70, \uc804\uccb4\ub97c \ud558\ub098\uc758 \ubcf4\uace0\uc11c\ub85c \ud3b8\uc9d1\ud55c\ub2e4.",
                "< \uc720\ud615\ubcc4 \ubcf4\uace0\uc11c \ubd84\ub7c9 >",
                "\uad6c \ubd84",
                "\uc548\uc804\ub4f1\uae09\uc81c \ube44\ub300\uc0c1",
                "\uc548\uc804\ub4f1\uae09\uc81c \ub300\uc0c1",
                "\uc704\ud5d8\uc694\uc18c 2\uac1c \uc774\ud558",
                "\uc704\ud5d8\uc694\uc18c 3\uac1c \uc774\uc0c1",
                "\ubd84 \ub7c9",
                "50\ud398\uc774\uc9c0 \uc774\ub0b4",
                "55\ud398\uc774\uc9c0 \uc774\ub0b4",
                "60\ud398\uc774\uc9c0 \uc774\ub0b4",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "item")

        self.assertTrue(analysis["table_like"])
        self.assertGreaterEqual(analysis["table_structured_row_count"], 2)
        self.assertEqual(analysis["table_cell_rows"][0]["cells"][0].replace(" ", ""), "\uad6c\ubd84")
        self.assertIn("50\ud398\uc774\uc9c0 \uc774\ub0b4", analysis["table_cell_rows"][1]["cells"])

    def test_merged_appendix_prefix_vertical_header_is_structured(self) -> None:
        text = "\n".join(
            [
                "2. \uaddc \uaca9",
                "\uac00. \ubcf8\ubcf4\uace0\uc11c\ub294 \uac00\ub85c21cm\u00d7\uc138\ub85c29.7cm(A4\uc6a9\uc9c0\ud06c\uae30)\uc758 \uaddc\uaca9\uc73c\ub85c \uc88c\ucca0\ud558\uba70, \uc804\uccb4\ub97c \ud558\ub098\uc758 \ubcf4\uace0\uc11c\ub85c \ud3b8\uc9d1\ud55c\ub2e4.",
                "< \uc720\ud615\ubcc4 \ubcf4\uace0\uc11c \ubd84\ub7c9 >",
                "[\ubcc4\ud45c 1] \uad6c \ubd84",
                "\uc548\uc804\ub4f1\uae09\uc81c \ube44\ub300\uc0c1",
                "\uc548\uc804\ub4f1\uae09\uc81c \ub300\uc0c1",
                "\uc704\ud5d8\uc694\uc18c 2\uac1c \uc774\ud558",
                "\uc704\ud5d8\uc694\uc18c 3\uac1c \uc774\uc0c1",
                "\ubd84 \ub7c9",
                "50\ud398\uc774\uc9c0 \uc774\ub0b4",
                "55\ud398\uc774\uc9c0 \uc774\ub0b4",
                "60\ud398\uc774\uc9c0 \uc774\ub0b4",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "item")

        self.assertTrue(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "structured_table")
        self.assertEqual(analysis["table_cell_rows"][0]["cells"][0].replace(" ", ""), "\uad6c\ubd84")
        self.assertNotIn("\ubcc4\ud45c", analysis["table_markdown"])

    def test_vertical_performance_grade_example_is_structured(self) -> None:
        text = "\n".join(
            [
                "(예시) 성과급 등급 구분 예시",
                "등 급",
                "S",
                "A",
                "B",
                "C",
                "D",
                "E",
                "지급률",
                "134%",
                "115%",
                "100%",
                "85%",
                "66%",
                "0%",
                "인 원",
                "10%",
                "15%",
                "50%",
                "15%",
                "10%",
                "* 6개 등급 구성",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "paragraph")

        self.assertTrue(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "structured_table")
        self.assertEqual(analysis["table_structured_row_count"], 3)
        self.assertEqual(len(analysis["table_cell_rows"]), 3)
        self.assertEqual(analysis["table_cell_rows"][0]["cells"], ["등급", "S", "A", "B", "C", "D", "E"])
        self.assertEqual(analysis["table_cell_rows"][1]["cells"][0], "지급률")
        self.assertEqual(analysis["table_cell_rows"][2]["cells"][0], "인원")
        self.assertNotIn("예시", analysis["table_markdown"])

    def test_disclosure_appendix_bullet_list_is_demoted(self) -> None:
        text = "\n".join(
            [
                "47. 국회 등 외부 평가",
                "▪국회 지적사항",
                "- 국회 지적사항과 그 시정조치 및 계획",
                "* 최근 3년간 국정감사 결과보고서상 지적사항, 결산ㆍ예산 심의시 부대의견, 국회 결산 심사결과 시정요구사항 등",
                "▪감사원/주무부처 지적사항",
                "- 최근 3년간 감사원/주무부처 감사결과 지적사항과 그 시정조치 및 계획",
                "- 수시공시",
                "* 사유 발생일 기준 14일 이내",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertFalse(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "probable_false_positive_prose_list")
        self.assertEqual(analysis["table_false_positive_stability"], "stable")

    def test_starred_numeric_table_row_is_preserved(self) -> None:
        text = "\n".join(
            [
                "구분 금액 비고",
                "*특례 100만원 200만원",
                "일반 50만원 100만원",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertTrue(analysis["table_like"])
        self.assertIn(["*특례", "100만원", "200만원"], [row["cells"] for row in analysis["table_cell_rows"]])

    def test_code_description_appendix_rows_are_structured(self) -> None:
        text = "\n".join(
            [
                "및 소음저감 서비스(건설시공 서비스는 제외한다)",
                "9406*, 9409*",
                "환경 검사 및 평가 서비스(환경영향평가 서비스로 한정한다)",
                "9.A. 641",
                "호텔 및 기타 숙박 서비스",
                "9.A. 642",
                "음식 수발 서비스",
                "9.A. 6431",
                "UN표준산품분류(CPC)",
                "대상 공사",
                "51",
                "건설 공사",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertTrue(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "structured_table")
        self.assertGreaterEqual(analysis["table_structured_row_count"], 5)
        self.assertEqual(analysis["table_cell_rows"][0]["cells"], ["분류코드", "대상"])
        self.assertIn(["9406*, 9409*", "및 소음저감 서비스(건설시공 서비스는 제외한다)"], [row["cells"] for row in analysis["table_cell_rows"]])
    def test_code_description_rows_are_not_synthesized_in_paragraph_context(self) -> None:
        text = "\n".join(
            [
                "특정 개인법인단체에 투자예치대여출연출자기부후원협찬 등을 하도록 개입하는 행위",
                "1.",
                "채용승진전보 등 인사업무나 징계업무에 관하여 개입하는 행위",
                "2.",
                "입찰경매연구개발시험특허 등에 관한 업무상 비밀을 누설하는 행위",
                "3.",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "paragraph")

        self.assertFalse(analysis["table_like"])
        self.assertEqual(analysis["table_cell_rows"], [])

    def test_single_wide_paragraph_row_without_records_is_demoted(self) -> None:
        text = "\n".join(
            [
                "일반 문단",
                "구분 내용 금액 비고",
                "다음 문단",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "paragraph")

        self.assertFalse(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "probable_false_positive_single_row")
        self.assertEqual(analysis["table_review_reason"], "single_structured_row_without_records")

    def test_form_article_prose_without_cells_is_demoted(self) -> None:
        text = "\n".join(
            [
                "2. 철도안전확보를위하여公社의품질기준에따른품질검사가필",
                "요한경우로서해당공사에포함된대상품목의추정가격합계가",
                "2천만원이상인경우",
                "②제1항제2호에서정한금액미만으로직접구매가어렵지만, 公社",
                "품질기준에따른품질검사가필요한경우사업부서담당자는품질",
                "검사가필요한품목, 품질검사기준및제53조에따른검사담당을명",
                "시하여공사계약요청할수있고, 계약부서담당자는계약체결시",
                "그요청사항을반영하여계약을체결한다.",
                "제9조(계약요청반송) 계약부서담당자는계약요청을검토한결과다",
                "음각호의어느하나에해당하는경우계약요청을반송할수있다.",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "form")

        self.assertFalse(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "probable_false_positive_article_prose")
        self.assertEqual(analysis["table_false_positive_stability"], "stable")

    def test_appendix_repeated_article_headings_without_cells_are_demoted(self) -> None:
        text = "\n".join(
            [
                "⑧",
                "계약담당자는 입찰공고 시 당사 입찰 및 계약 관련 신고제도를 입찰공고문에 명시하여야 한다.",
                "(2013.10.29 신설)",
                "제 27 조 (수의계약) 수의계약을 체결하고자 할 때에는 100만원 이상 계약 3건을 수의계약대상자에게 사전에 통보하여야 한다.",
                "제 27 조 (수의계약)",
                "1. 품명, 규격, 수량",
                "2. 인도요청 시기 또는 납품기한",
                "3. 구매규격서",
                "4. 그 밖의 계약조건",
                "제 28 조 (규격서 등의 비치) 계약담당자는 200만원 이상 구매물자의 명세, 수량, 규격서, 도면 및 평가기준을 입찰공고문에 첨부하여야 한다.",
                "제 29 조 (입찰참가신청) ① 계약담당자는 500만원 이상 입찰참가신청서를 제출하게 하여야 한다.",
                "제 30 조 (낙찰자 결정) 낙찰금액 1000만원 이상인 경우 산출내역서를 제출하게 하여야 한다.",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertFalse(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "probable_false_positive_article_prose")
        self.assertEqual(analysis["table_review_reason"], "repeated_article_heading_fragment_without_structured_cells")
        self.assertEqual(analysis["table_false_positive_stability"], "stable")

    def test_english_yes_no_form_questions_are_structured(self) -> None:
        text = "\n".join(
            [
                "◯ 6 Are you a local assemblyman who investigates and audits public institutions? [ ] Yes [ ] No",
                "[ ] N/A",
                "◯ 7 Are you a corporation or an organization whose representative falls under any of the categories from ◯ 1 to ◯ 6 [ ] Yes [ ] No",
                "[ ] N/A",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "form")

        self.assertTrue(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "structured_table")
        self.assertEqual(analysis["table_header_cells"], ["항목", "질문", "Yes", "No", "N/A"])
        self.assertEqual(analysis["table_records"][0]["record"]["항목"], "6")
        self.assertIn("local assemblyman", analysis["table_records"][0]["record"]["질문"])
        self.assertEqual(analysis["table_records"][0]["record"]["N/A"], "N/A")

    def test_numbered_colon_threshold_rows_are_structured(self) -> None:
        text = "\n".join(
            [
                "③ 평가위원회는 추정가격별로 다음 각 호의 기준에 따라 구성한다.",
                "1. 1억원 미만 : 3명 이상",
                "1. 1억원 미만 : 3명 이상",
                "2. 1억원 이상 50억원 미만 : 4명 이상",
                "3. 50억원 이상 : 5명 이상",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertTrue(analysis["table_like"])
        self.assertEqual(analysis["table_header_cells"], ["항목", "기준", "값"])
        self.assertEqual(len(analysis["table_records"]), 3)
        self.assertEqual(analysis["table_records"][0]["record"]["기준"], "1억원 미만")
        self.assertEqual(analysis["table_records"][0]["record"]["값"], "3명 이상")

    def test_time_schedule_row_is_not_split_at_the_clock_colon(self) -> None:
        extractor = TableExtractor()

        self.assertEqual(extractor._split_row("월 09:00 ~ 18:00"), ["월", "09:00", "~", "18:00"])
        # A genuine "label : value" row must still split on the colon.
        self.assertEqual(extractor._split_row("근무형태 : 시간선택제"), ["근무형태", "시간선택제"])

    def test_table_records_preserve_values_under_duplicate_headers(self) -> None:
        cell_rows = [
            {"row_index": 0, "cells": ["구분", "금액", "금액"]},
            {"row_index": 1, "cells": ["항목A", "100", "200"]},
        ]

        records = TableExtractor()._table_records(cell_rows)

        self.assertEqual(records[0]["record"]["금액"], "100")
        self.assertEqual(records[0]["record"]["금액 (2)"], "200")

    def test_qualification_inline_role_with_spaces_does_not_leak_into_value(self) -> None:
        rows = [
            "구분 자격",
            "교 수 1. 박사학위 소지자",
            "부교수 1. 박사학위 소지 후 3년",
            "조교수 1. 석사학위 소지자",
        ]

        records = TableExtractor()._extract_qualification_rows(rows, ("교수", "부교수", "조교수"))

        cells = [record["cells"] for record in records]
        self.assertIn(["교수", "1. 박사학위 소지자"], cells)

    def test_delegation_outline_conditions_are_structured(self) -> None:
        text = "\n".join(
            [
                "4. 예산집행",
                "가. 지급수수료 및 기타 경비집행",
                "(1) 5천만원 이상",
                "(2) 5천만원 미만",
                "(3) 정기적 집행성 경비",
                "바. 공사계약 방침",
                "(1) 10억원 이상",
                "(2) 3억원 이상 ~ 10억원 미만",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertTrue(analysis["table_like"])
        self.assertEqual(analysis["table_header_cells"], ["대항목", "세부항목", "순번", "기준"])
        self.assertGreaterEqual(len(analysis["table_records"]), 4)
        self.assertEqual(analysis["table_records"][0]["record"]["대항목"], "예산집행")
        self.assertEqual(analysis["table_records"][0]["record"]["세부항목"], "지급수수료 및 기타 경비집행")

    def test_organization_chart_without_cells_is_demoted(self) -> None:
        text = "\n".join(
            [
                "[별표 1] 기구표(제2조제1항 관련)",
                "1. 본사 기구표",
                "사 장 감 사",
                "미래전략실  전력ICT연구원  안전품질처  홍보소통실  감사실",
                "전략기획부  투자사업전략부  AX국정과제추진부",
                "감사총괄부  일상감사부  종합감사부  국민소통담당",
                "기획관리본부  전력ICT본부  전력지능화안전본부  신사업개발본부",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertFalse(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "probable_false_positive_org_chart")
        self.assertEqual(analysis["table_false_positive_stability"], "stable")

    def test_organization_profile_appendix_is_demoted(self) -> None:
        text = "\n".join(
            [
                "[별표 제1호] <개정 2024.04.22.>",
                "기 구 표",
                "<조 직>",
                "이 사 장",
                "감 사 (비상임) 안전관리실",
                "감 사 실",
                "경영전략처 사업운영처",
                "전략기획실 경영지원실 자산기획실 자산관리실 시설관리실",
                "<명 칭>",
                "한 글 영 문",
                "(재)우체국시설관리단 Postal Facility Management Agency",
                "<소재지>",
                "주 소",
                "서울특별시 광진구 강변역로 2(구의동)",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertFalse(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "probable_false_positive_org_chart")
        self.assertEqual(analysis["table_review_reason"], "organization_profile_without_structured_cells")

    def test_long_contract_sentence_without_cells_is_demoted(self) -> None:
        text = "\n".join(
            [
                "① 낙찰자가 결정되었을 때에는 관계법령의 규정에 따른 구비서류와 낙찰금액의 산출내역서 및 보증금 등을 7일 이내에 제출받아 10일 이내에 계약을 체결하여야 한다.",
                "7일 이내에 제출하지 않을 경우에는 낙찰자에게 미체결시 입찰보증금 귀속 및 입찰참가자격제한 조치 등을 문서로 독촉하여야 한다. 다만,",
                "불가항력의 사유로 인하여 계약을 체결할 수 없는 경우에는 그 사유가 존속하는 기간은 이를 포함하지 않는다.",
                "(2024. 12. 24. 개정)",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertFalse(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "probable_false_positive_article_prose")
        self.assertEqual(analysis["table_false_positive_stability"], "stable")

    def test_disciplinary_sanction_vertical_rows_are_structured(self) -> None:
        text = "\n".join(
            [
                "[별표 2] <신설 2026. 6. 30.>",
                "징계의 종류 및 양정기준",
                "임원 징계 제재조치 직원 징계양정과 비교",
                "해임 “해임”일 경우 준용",
                "연임제한 업무배제를 병과함",
                "“정직”일 경우 준용",
                "업무배제",
                "1개월 이상 3개월 이하의 기간 동안",
                "출근 및 직무정지를 하고, 기본연봉은",
                "지급하지 않음",
                "기본연봉",
                "감액",
                "1개월 이상 3개월 이하의 기간 동안",
                "임원연봉규정에 따른 기본연봉 월액의",
                "3분의 1을 감하여 지급함",
                "(경고 3회 이상 누적 시에도 적용)",
                "“감봉”일 경우 준용",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertTrue(analysis["table_like"])
        self.assertEqual(analysis["table_header_cells"], ["임원 징계", "제재조치", "직원 징계양정과 비교"])
        self.assertGreaterEqual(len(analysis["table_records"]), 4)
        self.assertEqual(analysis["table_records"][0]["record"]["임원 징계"], "해임")
        self.assertEqual(analysis["table_records"][1]["record"]["직원 징계양정과 비교"], "“정직”일 경우 준용")
        self.assertIn("3분의 1", analysis["table_records"][-1]["record"]["제재조치"])

    def test_multiline_leave_table_candidate_is_not_demoted_as_article_prose(self) -> None:
        text = "\n".join(
            [
                "복무제도",
                "구분 대상 휴가일수 증빙자료 기타",
                "특별",
                "휴가",
                "수업",
                "휴가",
                "∘ 방송통신대학교",
                "재학 중인 직원이 출석 수업에 참석할 때",
                "∘ 연간 총 출석수업기간의",
                "100분의 50이내",
                "∘ 출석수업을",
                "확인할 수 있는 서류",
                "-",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "paragraph")

        self.assertNotEqual(analysis["table_classification"], "probable_false_positive_article_prose")

    def test_appendix_context_and_wrapped_cells_are_reviewable(self) -> None:
        text = "\n".join(
            [
                "[별표 1] 법무 처리 기준",
                "구분 내용",
                "서에제출하여야하며, 법무담당부서는점검결과를검토하",
                "의견을제시할수있고, 업무주관부서장의청구금액확인",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertTrue(analysis["table_like"])
        self.assertEqual(analysis["table_appendix_no"], "별표1")
        self.assertEqual(analysis["table_appendix_title"], "법무 처리 기준")
        self.assertEqual(analysis["table_citation_label"], "별표1 법무 처리 기준")

    def test_hyphenated_appendix_context_label_is_preserved(self) -> None:
        text = "\n".join(
            [
                "[별표 2-1] 연구직 임용자격 기준표",
                "구분 자격",
                "수석연구원 박사학위",
                "책임연구원 연구경력",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertTrue(analysis["table_like"])
        self.assertEqual(analysis["table_appendix_no"], "별표2-1")
        self.assertEqual(analysis["table_citation_label"], "별표2-1 연구직 임용자격 기준표")

    def test_parallel_tail_duration_values_are_structured(self) -> None:
        text = "\n".join(
            [
                "8. 제112조제1항제7호에 해당하는 자",
                "가. 2억원 이상의 뇌물을 준 자",
                "나. 1억원 이상 2억원 미만의 뇌물을 준 자",
                "다. 1천만원 이상 1억원 미만의 뇌물을 준 자",
                "라. 1천만원 미만의 뇌물을 준 자",
                "2년",
                "1년",
                "6개월",
                "3개월",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertTrue(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "structured_table")
        self.assertEqual(analysis["table_cell_rows"][0]["cells"], ["기준", "기간"])
        self.assertIn(
            ["가. 2억원 이상의 뇌물을 준 자", "2년"],
            [row["cells"] for row in analysis["table_cell_rows"]],
        )
        self.assertIn("parallel_value_tail_reconstruction", analysis["table_review_flags"])

    def test_salary_assessment_table_separates_career_and_pay_periods(self) -> None:
        text = "\n".join(
            [
                "[별표4] <개정 2017.12.29./2024.12.24.>",
                "업무지원직 연봉 사정 기준표",
                "1. 경력환산기준",
                "경력기준 환산율",
                "해당직무와 동일한 계통의 직무 경력 100%",
                "해당직무와 유사한 부류의 직무 경력 80%",
                "2. 연봉 사정기준(1항에 의거 산정된 경력 년수에 따라)",
                "* 비 고 : 차 ⇒ 업무지원직 기본연봉 차등조정기준액",
                "3년미만 3년이상 5년미만",
                "5년이상 7년미만 7년이상 비 고",
                "최저연봉 최저연봉+(차×1) 최저연봉+(차×2) 최저연봉+(차×3)",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertTrue(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "structured_table")
        self.assertIn("salary_assessment_reconstruction", analysis["table_review_flags"])
        rows = [row["cells"] for row in analysis["table_cell_rows"]]
        self.assertIn(["경력환산기준", "해당직무와 유사한 부류의 직무 경력", "80%"], rows)
        self.assertIn(["연봉 사정기준", "3년이상 5년미만", "최저연봉+(차×1)"], rows)
        self.assertNotIn(
            ["2. 연봉 사정기준(1항에 의거 산정된 경력 년수에 따라)", "5년이상 7년미만 7년이상 비 고"],
            rows,
        )

    def test_korean_letter_colon_duration_rows_are_structured(self) -> None:
        text = "\n".join(
            [
                "3. 입찰, 계약체결 과정에서 금품 등을 제공하지 않겠습니다.",
                "가. 2억원 이상의 뇌물을 준 자 : 2년",
                "나. 1억원 이상 2억원 미만의 뇌물을 준 자 : 1년",
                "다. 1천만원 이상 1억원 미만의 뇌물을 준 자 : 6개월",
                "라. 1천만원 미만의 뇌물을 준 자 : 3개월",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "form")

        self.assertTrue(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "structured_table")
        self.assertEqual(analysis["table_cell_rows"][0]["cells"], ["항목", "기준", "값"])
        self.assertIn(
            ["가", "2억원 이상의 뇌물을 준 자", "2년"],
            [row["cells"] for row in analysis["table_cell_rows"]],
        )

    def test_korean_letter_colon_duration_rows_without_amount_are_structured(self) -> None:
        text = "\n".join(
            [
                "1. 입찰가격의 유지나 특정인의 낙찰을 위한 담합을 하지 않겠습니다.",
                "가. 입찰가격을 서로 상의하여 미리 입찰가격협정을 주도하여 낙찰을 받은 자 : 2년",
                "나. 특정인의 낙찰을 위하여 담합을 주도한 자 : 1년",
                "다. 입찰자 간에 미리 입찰 자격을 협정하거나 특정인의 낙찰을 위하여 담합하는 자 : 6월",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "form")

        self.assertTrue(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "structured_table")
        self.assertIn(
            ["가", "입찰가격을 서로 상의하여 미리 입찰가격협정을 주도하여 낙찰을 받은 자", "2년"],
            [row["cells"] for row in analysis["table_cell_rows"]],
        )
        self.assertIn(
            ["다", "입찰자 간에 미리 입찰 자격을 협정하거나 특정인의 낙찰을 위하여 담합하는 자", "6월"],
            [row["cells"] for row in analysis["table_cell_rows"]],
        )

    def test_named_area_vertical_rows_are_structured(self) -> None:
        text = "\n".join(
            [
                "[별표 2](제9조의2제1항 관련)",
                "지역본부의 명칭 및 구역(공원명)",
                "본부의 명칭",
                "구 역(공원명)",
                "비 고",
                "동부지역본부",
                "지리산, 경주, 한려해상, 가야산, 주왕산, 팔공산, 금정산",
                "7공원 11사무소 3생태탐방원",
                "서부지역본부",
                "내장산, 덕유산, 다도해해상, 변산반도, 월출산, 무등산",
                "6공원 9사무소 3생태탐방원",
                "계 4개 본부",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertTrue(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "structured_table")
        self.assertEqual(analysis["table_cell_rows"][0]["cells"], ["본부의 명칭", "구 역(공원명)", "비 고"])
        self.assertIn(
            ["동부지역본부", "지리산, 경주, 한려해상, 가야산, 주왕산, 팔공산, 금정산", "7공원 11사무소 3생태탐방원"],
            [row["cells"] for row in analysis["table_cell_rows"]],
        )
        self.assertIn("named_area_vertical_reconstruction", analysis["table_review_flags"])

    def test_salary_group_rows_are_structured(self) -> None:
        text = "\n".join(
            [
                "임․직원 기본연봉표",
                "나.",
                "직 원 (단위：천원)",
                "연봉그룹 직 급 S그룹 A그룹 B그룹 C그룹 D그룹 E그룹",
                "1 급 102,360 98,435 93,004 87,576 82,147 76,952",
                "2 급 87,241 83,892 78,518 73,141 67,766 62,392",
                "3 급 71,022 68,291 64,529 60,763 57,002 53,241",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertTrue(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "structured_table")
        self.assertEqual(analysis["table_cell_rows"][0]["cells"], ["직급", "S그룹", "A그룹", "B그룹", "C그룹", "D그룹", "E그룹"])
        self.assertIn(
            ["1급", "102,360", "98,435", "93,004", "87,576", "82,147", "76,952"],
            [row["cells"] for row in analysis["table_cell_rows"]],
        )
        self.assertIn("salary_group_row", analysis["table_review_flags"])

    def test_short_paragraph_clause_is_demoted_from_table_extraction_failed(self) -> None:
        text = "\n".join(
            [
                "② 질병이나 부상으로 인한 지각·조퇴·외출은 누계 8시간을 병가 1일로 계산하고,",
                "제47조제2항에 따라 연차휴가일수에서 공제한 병가는 이를 병가일수에 포함",
                "하지 아니한다.",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "paragraph")

        self.assertFalse(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "probable_false_positive_article_prose")

    def test_c0147_hwp_numbered_leave_clause_is_not_split_as_table(self) -> None:
        text = "\n".join(
            [
                "② 연도중 임명된 경우 기준 휴가일수에서 당해년도 임기만큼 월할 계산하여 부여한다.",
                "□ (병가) 상임이사의 병가는 취업규칙상의 병가에 준하여 운용한다.",
                "□ (특별휴가) 경조사 휴가 등 상임이사의 특별휴가는 취업규칙상의 특별휴가에 준하여 운용한다.",
                "□ (휴가 승인) ① 사장과 상임감사위원은 주어진 휴가일수 범위 내에서 자율적으로 사용한다.",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "paragraph")

        self.assertFalse(analysis["table_like"])
        self.assertEqual(analysis["table_cell_rows"], [])

    def test_nested_parallel_tail_duration_values_use_leaf_labels(self) -> None:
        text = "\n".join(
            [
                "2. 제112조제1항제1호에 해당하는 자 중 계약의 이행을 조잡하게 한 자",
                "가. 공사",
                "1) 하자비율이 100분의 500 이상인 자",
                "2) 하자비율이 100분의 300 이상 100분의 500 미만인 자",
                "3) 하자비율이 100분의 200 이상 100분의 300 미만인 자",
                "4) 하자비율이 100분의 100 이상 100분의 200 미만인 자",
                "나. 물품",
                "1) 보수비율이 100분의 25 이상인 자",
                "2) 보수비율이 100분의 15 이상 100분의 25 미만인 자",
                "3) 보수비율이 100분의 10 이상 100분의 15 미만인 자",
                "4) 보수비율이 100분의 6 이상 100분의 10 미만인 자",
                "2년",
                "1년",
                "8개월",
                "3개월",
                "2년",
                "1년",
                "8개월",
                "3개월",
            ]
        )

        analysis = TableExtractor().analyze_text(text, "appendix")

        self.assertTrue(analysis["table_like"])
        self.assertEqual(analysis["table_classification"], "structured_table")
        self.assertEqual(len(analysis["table_cell_rows"]), 9)
        self.assertEqual(analysis["table_cell_rows"][1]["cells"][0], "1) 하자비율이 100분의 500 이상인 자")
        self.assertEqual(analysis["table_cell_rows"][5]["cells"][0], "1) 보수비율이 100분의 25 이상인 자")
        self.assertEqual(analysis["table_record_count"], 8)


if __name__ == "__main__":
    unittest.main()
