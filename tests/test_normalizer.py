from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
