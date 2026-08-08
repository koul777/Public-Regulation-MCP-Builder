from __future__ import annotations

import unittest

from app.processors.mojibake import (
    MOJIBAKE_CLEANED_CHARS_KEY,
    MOJIBAKE_REMOVED_BLOCKS_KEY,
    MOJIBAKE_REMOVED_CHARS_KEY,
)
from app.processors.normalizer import TextNormalizer
from app.schemas.parsed import ParsedBlock, ParsedDocument, ParsedPage


class TextNormalizerTests(unittest.TestCase):
    def test_removes_pdf_control_characters(self) -> None:
        text = "제1조(목적)\x01 이\u00a0규정은\u200b 목적을 정한다."

        normalized = TextNormalizer().normalize_text(text)

        self.assertNotIn("\x01", normalized)
        self.assertNotIn("\u200b", normalized)
        self.assertIn("이 규정은", normalized)

    def test_collapses_repeated_private_use_leader_and_maps_formula_glyphs(self) -> None:
        text = "목 차\n\ue70d\ue70d\ue70d\ue70d\n환산식 = 110-(40×\ue06d 점수)"

        normalized = TextNormalizer().normalize_text(text)

        self.assertNotIn("\ue70d", normalized)
        self.assertNotIn("\ue06d", normalized)
        self.assertIn("40×/ 점수", normalized)

    def test_maps_hwp_formula_minus_private_use_glyph(self) -> None:
        text = "110-(40×\ue06d 당해최고점수\ue046당해최저점수 당해최고점수\ue046획득점수)"

        normalized = TextNormalizer().normalize_text(text)

        self.assertEqual(normalized, "110-(40×/ 당해최고점수-당해최저점수 당해최고점수-획득점수)")

    def test_maps_common_private_use_bullets_numbers_and_arrows(self) -> None:
        text = "\uf09f 항목\n\uf0a7 하위항목\n\uf081 첫째 \uf082 둘째\nA \uf0e8 B"

        normalized = TextNormalizer().normalize_text(text)

        self.assertIn("• 항목", normalized)
        self.assertIn("▪ 하위항목", normalized)
        self.assertIn("① 첫째 ② 둘째", normalized)
        self.assertIn("A → B", normalized)

    def test_maps_pdf_private_use_quote_glyph(self) -> None:
        text = "이 경우 \uf000○○○\uf000를 \uf000△△△\uf000로 본다."

        normalized = TextNormalizer().normalize_text(text)

        self.assertEqual(normalized, '이 경우 "○○○"를 "△△△"로 본다.')
        self.assertNotIn("\uf000", normalized)

    def test_removes_inline_hwp_mojibake_artifact_token(self) -> None:
        text = "--------- 湯湷 -------------------------\n정상 본문"

        normalized = TextNormalizer().normalize_text(text)

        self.assertNotIn("湯湷", normalized)
        self.assertIn("정상 본문", normalized)

    def test_repair_line_breaks_keeps_paragraph_symbols_past_fifteen(self) -> None:
        text = "⑮ 지급 기준은 별표 3과 같다\n⑯ 이 규정 시행에 필요한 사항은 따로 정한다."

        repaired = TextNormalizer().repair_line_breaks(text)

        self.assertEqual(
            ["⑮ 지급 기준은 별표 3과 같다", "⑯ 이 규정 시행에 필요한 사항은 따로 정한다."],
            repaired.splitlines(),
        )

    def test_single_line_page_edge_is_not_double_counted_as_repeated_header(self) -> None:
        def page(text: str) -> ParsedPage:
            return ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])

        # "협의체 운영세칙" appears on only 2 of 6 pages (below the threshold of 3),
        # but each is a single-line page whose only line is both first and last.
        pages = [
            page("협의체 운영세칙"),
            page("제1조 본문내용 여기"),
            page("제2조 다른내용 여기"),
            page("협의체 운영세칙"),
            page("제3조 또다른내용"),
            page("제4조 마지막내용"),
        ]
        parsed = ParsedDocument(
            document_id="doc", source_file="x.md", file_type="text", pages=pages, raw_text=""
        )

        repeated = TextNormalizer()._repeated_edge_lines(parsed)

        self.assertNotIn("협의체 운영세칙", repeated)

    def test_removes_simple_page_footer_lines(self) -> None:
        text = "제1조(목적) 본문\n- 12 -\n다음 문장"
        normalizer = TextNormalizer()
        parsed = ParsedDocument(
            document_id="doc",
            source_file="x.md",
            file_type="text",
            pages=[
                ParsedPage(
                    page_no=1,
                    blocks=[ParsedBlock(text=text)],
                )
            ],
            raw_text=text,
        )

        normalized = normalizer.normalize_document(parsed)

        self.assertNotIn("- 12 -", normalized.raw_text)


class NormalizerMojibakeCleanupTests(unittest.TestCase):
    """깨진 글자를 지우되, 지웠다는 사실과 정상 한자는 남기는지 확인한다."""

    def _document(self, *texts: str) -> ParsedDocument:
        return ParsedDocument(
            document_id="doc-mojibake",
            source_file="regulation.hwp",
            file_type="hwp",
            pages=[
                ParsedPage(page_no=index + 1, blocks=[ParsedBlock(type="text", text=text)])
                for index, text in enumerate(texts)
            ],
        )

    def test_removes_utf16_byte_pair_mojibake_from_the_title_line(self) -> None:
        # \u6164\u6865 (慤桥) 의 UTF-16BE 바이트는 "adhe" 라서 본문이 아니라 내부 이름 조각이다.
        normalized = TextNormalizer().normalize_text("강사임용 등에 관한 규정 \u6164\u6865 \u6164\u6865")

        self.assertEqual("강사임용 등에 관한 규정", normalized)

    def test_removes_layout_coordinate_characters_from_a_form_line(self) -> None:
        # \u0fa0 = 4000, \u0100 = 256 은 HWP 배치 좌표값이 글자로 읽힌 것이다.
        normalized = TextNormalizer().normalize_text("\u0fa0 \u0100 \u0fa0 \u0100 신 청 인 : (서명)")

        self.assertEqual("신 청 인 : (서명)", normalized)

    def test_absorbs_a_single_char_artifact_glued_to_a_confirmed_one(self) -> None:
        # \u6d6b (浫) 는 "mk" 라 한 글자만으로는 판정할 수 없지만 \u0262 에 붙어 있다.
        normalized = TextNormalizer().normalize_text("제1조(목적) \u6d6b\u0262 이 지침은 정한다.")

        self.assertEqual("제1조(목적) 이 지침은 정한다.", normalized)

    def test_keeps_legitimate_hanja_that_regulations_actually_use(self) -> None:
        # 이 글자들을 지우면 옛 규정문의 제목과 기관명이 통째로 사라진다.
        for legitimate in (
            "제1장 總則",
            "細則",
            "本則",
            "韓國學中央研究院",
            "학위(學位) 수여에 관한 규정",
            "「學位」 규정",
            "改正 2024. 1. 1.",
        ):
            with self.subTest(legitimate=legitimate):
                self.assertEqual(legitimate, TextNormalizer().normalize_text(legitimate))

    def test_records_how_much_was_removed_so_the_damage_is_not_hidden(self) -> None:
        document = self._document(
            "4-4-4. 공무국외여행규정\n\u6564\u6571\n\u6564\u6571",
            "\u0fa0 \u0100 신 청 인 : (서명)",
        )

        normalized = TextNormalizer().normalize_document(document)

        self.assertEqual(4, normalized.metadata[MOJIBAKE_REMOVED_CHARS_KEY])
        self.assertEqual(1, normalized.metadata[MOJIBAKE_REMOVED_BLOCKS_KEY])
        # \ubc30\uce58 \uc88c\ud45c \ub204\ucd9c\uc740 \uc9c0\uc6b0\uba74 \ub05d\ub098\ub294 \uad70\ub354\ub354\uae30\ub77c \uc190\uc0c1\uc73c\ub85c \uc138\uc9c0 \uc54a\ub294\ub2e4.
        self.assertEqual(2, normalized.metadata[MOJIBAKE_CLEANED_CHARS_KEY])

    def test_export_boilerplate_is_removed_but_not_reported_as_damage(self) -> None:
        """\u6164\u6865\u00b7\u6f20\u6773\ub294 \ubcf4\uad00 \ubb38\uc11c 26\uac74 \uc804\ubd80\uc5d0 \uc788\uc5c8\ub2e4. \uc9c0\uc6b0\ub418 \uc190\uc0c1\uc73c\ub85c\ub294 \uc138\uc9c0 \uc54a\ub294\ub2e4."""
        document = self._document("4-4-1. \ubcf5\ubb34\uaddc\uc815 \u6164\u6865 \u6f20\u6773")

        normalized = TextNormalizer().normalize_document(document)

        self.assertEqual("4-4-1. \ubcf5\ubb34\uaddc\uc815", normalized.raw_text)
        self.assertEqual(0, normalized.metadata[MOJIBAKE_REMOVED_CHARS_KEY])
        self.assertEqual(0, normalized.metadata[MOJIBAKE_REMOVED_BLOCKS_KEY])
        self.assertEqual(4, normalized.metadata[MOJIBAKE_CLEANED_CHARS_KEY])

    def test_reports_zero_removals_for_a_clean_document(self) -> None:
        normalized = TextNormalizer().normalize_document(
            self._document("제1조(목적) 이 규정은 목적을 정한다.")
        )

        self.assertEqual(0, normalized.metadata[MOJIBAKE_REMOVED_CHARS_KEY])
        self.assertEqual(0, normalized.metadata[MOJIBAKE_REMOVED_BLOCKS_KEY])

    def test_keeps_existing_parsed_metadata_when_recording_removals(self) -> None:
        document = self._document("제1조(목적) 이 규정은 목적을 정한다.")
        document.metadata = {"parser": "hwp"}

        normalized = TextNormalizer().normalize_document(document)

        self.assertEqual("hwp", normalized.metadata["parser"])

    def test_reports_measured_page_progress_while_normalizing(self) -> None:
        # 통합본 정리 중에 진행 표시가 멈춘 것처럼 보이면 안 된다. 다만 진행률은
        # 실제로 끝낸 쪽 수여야 하고, 시간으로 추정한 값이면 안 된다.
        document = self._document(*[f"제{index}조(목적) 본문" for index in range(1, 46)])
        events: list[tuple[int, int]] = []

        TextNormalizer().normalize_document(
            document,
            progress_callback=lambda current, total: events.append((current, total)),
        )

        self.assertEqual((0, 45), events[0])
        self.assertEqual((45, 45), events[-1])
        self.assertLess(1, len(events), events)
        self.assertTrue(
            all(
                previous <= current
                for (previous, _), (current, _) in zip(events, events[1:])
            ),
            events,
        )
        self.assertTrue(all(total == 45 for _current, total in events), events)

    def test_normalizing_without_a_progress_callback_still_works(self) -> None:
        normalized = TextNormalizer().normalize_document(self._document("제1조(목적) 본문"))

        self.assertIn("제1조(목적) 본문", normalized.raw_text)


if __name__ == "__main__":
    unittest.main()
