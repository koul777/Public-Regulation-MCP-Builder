from __future__ import annotations

import unittest

from app.processors.metadata_extractor import MetadataExtractor


class MetadataExtractorTests(unittest.TestCase):
    def test_extracts_references_and_revision_dates(self) -> None:
        text = (
            "제2조(자료) 「국가재정법」 제6조제2항 및 별표 1, 별지 제2호 서식을 따른다. "
            "<개정 2024. 12. 18.> 이 규정은 2025년 1월 1일부터 시행한다."
        )

        metadata = MetadataExtractor().extract(text, "제2조")

        self.assertIn("국가재정법", metadata["external_law_refs"])
        self.assertNotIn("제6조제2항", metadata["article_refs"])
        self.assertIn("별표1", metadata["appendix_refs"])
        self.assertIn("별지제2호서식", metadata["form_refs"])
        self.assertEqual(metadata["revision_date"], "2024-12-18")
        self.assertEqual(metadata["effective_date"], "2025-01-01")
        self.assertTrue(metadata["references"])

    def test_preserves_hyphenated_appendix_and_form_labels(self) -> None:
        text = "제15조(기준) 연구경력은 별표 2-1 및 별표 2를 따르고 별지 제3-1호 서식을 사용한다."

        metadata = MetadataExtractor().extract(text, "제15조")

        self.assertIn("별표2-1", metadata["appendix_refs"])
        self.assertIn("별표2", metadata["appendix_refs"])
        self.assertIn("별지제3-1호서식", metadata["form_refs"])

    def test_skips_current_article_self_reference_at_start(self) -> None:
        text = "제2조(자료) 제3조를 준용한다."

        metadata = MetadataExtractor().extract(text, "제2조")

        self.assertNotIn("제2조", metadata["article_refs"])
        self.assertIn("제3조", metadata["article_refs"])

    def test_extracts_halfwidth_law_quotes_and_spaced_revision_events(self) -> None:
        text = (
            "다른 법령이나 지침상의 ｢공기업·준정부기관 예산편성지침｣ 및 "
            "｢공기업·준정부기관 예산집행지침｣은 ｢공기업·준정부기관 예산운용지침｣으로 본다. "
            "< 신 설 > < 개 정 2025. 7. 30.>"
        )

        metadata = MetadataExtractor().extract(text)

        self.assertEqual(
            metadata["external_law_refs"],
            [],
        )
        self.assertEqual(
            metadata["internal_regulation_refs"],
            [
                "공기업·준정부기관 예산편성지침",
                "공기업·준정부기관 예산집행지침",
                "공기업·준정부기관 예산운용지침",
            ],
        )
        self.assertTrue(all(ref.get("scope") == "internal" for ref in metadata["references"] if ref["type"] == "regulation"))
        self.assertEqual([event["type"] for event in metadata["revision_events"]], ["신설", "개정"])
        self.assertEqual(metadata["revision_date"], "2025-07-30")
        self.assertTrue(metadata["references"])

    def test_extracts_revision_history_lines_with_rule_numbers_and_effective_dates(self) -> None:
        text = "\n".join(
            [
                "제정 1980. 12. 23. 규정 제1호",
                "개정 2022. 4. 1",
                "일부개정 2025. 1. 13. 규정 제1234호(시행 2025. 1. 13.)",
                "제1장 총칙",
            ]
        )

        metadata = MetadataExtractor().extract(text)

        self.assertEqual(len(metadata["revision_history"]), 3)
        self.assertEqual(metadata["revision_history"][0]["event_type"], "제정")
        self.assertEqual(metadata["revision_history"][0]["date"], "1980-12-23")
        self.assertEqual(metadata["revision_history"][0]["rule_no"], "제1호")
        self.assertEqual(metadata["revision_history"][2]["event_type"], "일부개정")
        self.assertEqual(metadata["revision_history"][2]["effective_date"], "2025-01-13")
        self.assertEqual(metadata["revision_date"], "2025-01-13")

    def test_extracts_supplementary_metadata_and_article_effective_overrides(self) -> None:
        text = (
            "부칙 <2025. 12. 30.>\n"
            "제1조(시행일) 이 규정은 2026년 1월 1일부터 시행한다. "
            "다만, 제17조제1항제1호의 개정규정은 2026년 1월 2일부터 시행한다.\n"
            "제2조(경과조치) 종전 규정에 따른다."
        )

        metadata = MetadataExtractor().extract(text)

        self.assertTrue(metadata["is_supplementary_provision"])
        self.assertEqual(metadata["supplementary_label"], "부칙")
        self.assertEqual(metadata["supplementary_identifier_date"], "2025-12-30")
        self.assertEqual(metadata["effective_date"], "2026-01-01")
        self.assertEqual(metadata["valid_from"], "2026-01-01")
        self.assertEqual(
            metadata["article_effective_overrides"],
            [
                {
                    "article_ref": "제17조제1항제1호",
                    "effective_date": "2026-01-02",
                    "raw": "제17조제1항제1호의 개정규정은 2026년 1월 2일부터 시행한다",
                }
            ],
        )

    def test_marks_one_line_supplementary_effective_date_as_boilerplate(self) -> None:
        text = "부칙 <2022. 4. 1.>\n이 정관은 2022년 4월 1일부터 시행한다."

        metadata = MetadataExtractor().extract(text)

        self.assertTrue(metadata["is_supplementary_provision"])
        self.assertTrue(metadata["supplementary_boilerplate"])
        self.assertEqual(metadata["supplementary_identifier_date"], "2022-04-01")
        self.assertEqual(metadata["effective_date"], "2022-04-01")

    def test_marks_supplementary_context_boilerplate_without_marker_in_chunk_text(self) -> None:
        text = "①(시행일) 이 규정은 공포한 날부터 시행한다."

        metadata = MetadataExtractor().extract(text, supplementary_context=True)

        self.assertTrue(metadata["is_supplementary_provision"])
        self.assertTrue(metadata["supplementary_boilerplate"])
        self.assertEqual(metadata["effective_date"], "promulgation_date")

    def test_supplementary_transition_content_is_not_boilerplate(self) -> None:
        text = (
            "부칙 <2025. 12. 30.>\n"
            "①(시행일) 이 규정은 2026년 1월 1일부터 시행한다.\n"
            "②(경과 조치) 종전 규정에 따른 인가는 계속 효력을 가진다."
        )

        metadata = MetadataExtractor().extract(text)

        self.assertTrue(metadata["is_supplementary_provision"])
        self.assertFalse(metadata["supplementary_boilerplate"])


    def test_splits_internal_and_external_law_references(self) -> None:
        text = "「국가재정법」 및 「인사규정」을 따른다."

        metadata = MetadataExtractor().extract(text)

        self.assertEqual(metadata["external_law_refs"], ["국가재정법"])
        self.assertEqual(metadata["internal_regulation_refs"], ["인사규정"])

    def test_splits_current_and_other_regulation_article_references(self) -> None:
        text = (
            "제6조(공고) 직원의 채용은 세칙 제8조와 규정 제20조 및 지침 제17조 제1항에 따른다. "
            "계약직의 경우 계약직 채용 및 운용규정 제6조에 따른다. "
            "승진 임용(직제규정 시행세칙 제6조에 따라) 처리한다."
        )

        metadata = MetadataExtractor().extract(
            text,
            "제6조",
            current_regulation_title="채용업무지침",
        )

        self.assertEqual(metadata["article_refs"], ["제17조제1항"])
        self.assertEqual(
            metadata["internal_regulation_refs"],
            ["세칙", "규정", "계약직 채용 및 운용규정", "직제규정 시행세칙"],
        )
        self.assertEqual(
            metadata["regulation_article_refs"],
            [
                {"regulation_ref": "세칙", "article_ref": "제8조"},
                {"regulation_ref": "규정", "article_ref": "제20조"},
                {"regulation_ref": "계약직 채용 및 운용규정", "article_ref": "제6조"},
                {"regulation_ref": "직제규정 시행세칙", "article_ref": "제6조"},
            ],
        )

    def test_excludes_explicit_external_law_articles_from_internal_article_refs(self) -> None:
        text = (
            "지방공무원법 제55조는 국가공무원법 제63조와 함께 공무원의 품위유지의무를 규정한다. "
            "다만 제12조는 내부 조문 참조로 남긴다."
        )

        metadata = MetadataExtractor().extract(text)

        self.assertNotIn("제55조", metadata["article_refs"])
        self.assertNotIn("제63조", metadata["article_refs"])
        self.assertIn("제12조", metadata["article_refs"])

    def test_treats_internal_enforcement_rule_article_as_regulation_article_ref(self) -> None:
        text = "금품ㆍ향응 수수 관련 징계양정 기준은 「인사규정 시행규칙」 제75조 제2항 관련 별표를 따른다."

        metadata = MetadataExtractor().extract(text)

        self.assertNotIn("제75조제2항", metadata["article_refs"])
        self.assertIn("인사규정 시행규칙", metadata["internal_regulation_refs"])
        self.assertIn(
            {"regulation_ref": "인사규정 시행규칙", "article_ref": "제75조제2항"},
            metadata["regulation_article_refs"],
        )

    def test_inherits_internal_regulation_prefix_for_parallel_article_refs(self) -> None:
        text = "‘정해진 시간’이란 「인사규정」 제36조(근무시간) 및 제37조(근무시간 등의 변경)에 따른 시간을 말한다."

        metadata = MetadataExtractor().extract(text, current_regulation_title="복무편람")

        self.assertNotIn("제37조", metadata["article_refs"])
        self.assertIn("인사규정", metadata["internal_regulation_refs"])
        self.assertEqual(
            metadata["regulation_article_refs"],
            [
                {"regulation_ref": "인사규정", "article_ref": "제36조"},
                {"regulation_ref": "인사규정", "article_ref": "제37조"},
            ],
        )

    def test_extracts_smart_quoted_regulation_article_refs(self) -> None:
        text = "“공무원 수당 등에 관한 규정” 제11조의2의 별표에 따른다."

        metadata = MetadataExtractor().extract(text, current_regulation_title="해외파견제운영지침")

        self.assertNotIn("제11조의2", metadata["article_refs"])
        self.assertIn("공무원 수당 등에 관한 규정", metadata["internal_regulation_refs"])
        self.assertIn(
            {"regulation_ref": "공무원 수당 등에 관한 규정", "article_ref": "제11조의2"},
            metadata["regulation_article_refs"],
        )

    def test_treats_yegyu_article_as_internal_regulation_article_ref(self) -> None:
        text = "민원처리예규 제15조(민원사항의 처리)에 따른다."

        metadata = MetadataExtractor().extract(text, current_regulation_title="복무편람")

        self.assertNotIn("제15조", metadata["article_refs"])
        self.assertIn("민원처리예규", metadata["internal_regulation_refs"])
        self.assertIn(
            {"regulation_ref": "민원처리예규", "article_ref": "제15조"},
            metadata["regulation_article_refs"],
        )

    def test_inherits_external_law_prefix_for_parallel_article_refs(self) -> None:
        text = (
            "「국가공무원법」 제63조 및 제64조를 준수한다. "
            "「형법」 제129조부터 제132조까지, 제314조, 제315조 및 제359조 중 어느 하나에 해당하는 죄. "
            "다만 제3조는 내부 기준이다."
        )

        metadata = MetadataExtractor().extract(text)

        self.assertNotIn("제63조", metadata["article_refs"])
        self.assertNotIn("제64조", metadata["article_refs"])
        self.assertNotIn("제314조", metadata["article_refs"])
        self.assertNotIn("제315조", metadata["article_refs"])
        self.assertNotIn("제359조", metadata["article_refs"])
        self.assertIn("제3조", metadata["article_refs"])

    def test_inherits_external_law_prefix_across_clause_only_refs(self) -> None:
        text = (
            "「공공기관의 운영에 관한 법률」 제22조, 제48조 제4항 및 제8항, "
            "제52조의3에 따라 해임된 사람은 제한한다. 다만 제7조는 내부 기준이다."
        )

        metadata = MetadataExtractor().extract(text)

        self.assertNotIn("제22조", metadata["article_refs"])
        self.assertNotIn("제48조제4항", metadata["article_refs"])
        self.assertNotIn("제52조의3", metadata["article_refs"])
        self.assertIn("제7조", metadata["article_refs"])

    def test_ignores_quoted_prose_when_extracting_regulation_refs(self) -> None:
        text = (
            "\u300c\uc5f0\ubd09\ubc0f\ubcf5\ub9ac\n"
            "\ud6c4\uc0dd\uad00\ub9ac\uaddc\uc815\ubc0f\uac19\uc740\uaddc\uc815"
            "\uc2dc\ud589\uc138\uce59\uc5d0\ub530\ub978\ub2e4.\n"
            "5) \ub2e4\ub978 \ub0b4\uc6a9\u300d "
            "\u300c\uc5f0\ubd09\ubc0f\ubcf5\ub9ac\ud6c4\uc0dd\uad00\ub9ac\uaddc\uc815\u300d"
        )

        metadata = MetadataExtractor().extract(text)

        self.assertEqual(
            metadata["internal_regulation_refs"],
            ["\uc5f0\ubd09\ubc0f\ubcf5\ub9ac\ud6c4\uc0dd\uad00\ub9ac\uaddc\uc815"],
        )
        self.assertEqual(metadata["external_law_refs"], [])

    def test_extracts_revision_history_spans(self) -> None:
        text = "\n".join(
            [
                "제정 1980. 12. 23. 규정 제1호",
                "개정 2022. 4. 1",
                "일부개정 2025. 1. 13. 규정 제1234호(시행 2025. 1. 13.)",
                "제1장 총칙",
            ]
        )

        metadata = MetadataExtractor().extract(text)

        self.assertEqual(len(metadata["revision_history_spans"]), 1)
        span = metadata["revision_history_spans"][0]
        self.assertEqual(span["start_line"], 1)
        self.assertEqual(span["end_line"], 3)
        self.assertEqual(span["event_count"], 3)


if __name__ == "__main__":
    unittest.main()
