from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from app.parsers.base import ParserError
from app.parsers.docx_parser import DocxParser
from app.parsers.factory import get_parser


DOCX_AVAILABLE = importlib.util.find_spec("docx") is not None


class DocxParserTests(unittest.TestCase):
    def test_factory_supports_docx_extension(self) -> None:
        self.assertIsInstance(get_parser(Path("sample.docx")), DocxParser)

    @unittest.skipUnless(DOCX_AVAILABLE, "python-docx is not installed")
    def test_preserves_paragraph_table_order(self) -> None:
        from docx import Document

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ordered.docx"
            doc = Document()
            doc.add_paragraph("제1조 목적")
            table = doc.add_table(rows=2, cols=2)
            table.cell(0, 0).text = "구분"
            table.cell(0, 1).text = "내용"
            table.cell(1, 0).text = "가"
            table.cell(1, 1).text = "본문"
            doc.add_paragraph("제2조 적용")
            doc.save(path)

            parsed = DocxParser().parse(path, "doc_ordered")

        blocks = parsed.pages[0].blocks
        self.assertEqual([block.type for block in blocks], ["text", "table", "text"])
        self.assertEqual([block.text for block in blocks], ["제1조 목적", "구분 | 내용\n가 | 본문", "제2조 적용"])
        self.assertEqual(parsed.raw_text, "제1조 목적\n구분 | 내용\n가 | 본문\n제2조 적용")

    @unittest.skipUnless(DOCX_AVAILABLE, "python-docx is not installed")
    def test_horizontally_merged_cell_is_not_repeated_per_column(self) -> None:
        from docx import Document

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "merged.docx"
            doc = Document()
            table = doc.add_table(rows=2, cols=3)
            header = table.cell(0, 0).merge(table.cell(0, 2))
            header.text = "공통 기준"
            table.cell(1, 0).text = "A"
            table.cell(1, 1).text = "B"
            table.cell(1, 2).text = "C"
            doc.save(path)

            parsed = DocxParser().parse(path, "doc_merged")

        table_block = next(block for block in parsed.pages[0].blocks if block.type == "table")
        self.assertEqual(table_block.text, "공통 기준\nA | B | C")

    @unittest.skipUnless(DOCX_AVAILABLE, "python-docx is not installed")
    def test_header_part_is_exposed_as_review_metadata_without_reordering_body(self) -> None:
        from docx import Document

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "header.docx"
            doc = Document()
            doc.add_paragraph("본문")
            doc.sections[0].header.paragraphs[0].text = "머리말"
            doc.sections[0].footer.paragraphs[0].text = "꼬리말"
            doc.save(path)

            parsed = DocxParser().parse(path, "doc_header")

        self.assertEqual([block.text for block in parsed.pages[0].blocks], ["본문"])
        self.assertEqual(parsed.metadata["parser_uncertainty_risk_level"], "medium")
        self.assertIn("docx_unparsed_parts", parsed.metadata["parser_uncertainty_flags"])
        self.assertEqual(parsed.metadata["parser_uncertainty_recommendation"], "review_missing_docx_parts")
        self.assertIn("word/header1.xml", parsed.metadata["docx_unparsed_parts"])
        self.assertIn("word/footer1.xml", parsed.metadata["docx_unparsed_parts"])

    @unittest.skipUnless(DOCX_AVAILABLE, "python-docx is not installed")
    def test_invalid_docx_raises_parser_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid.docx"
            path.write_bytes(b"not a docx")

            with self.assertRaisesRegex(ParserError, "Failed to parse DOCX file"):
                DocxParser().parse(path, "doc_invalid_docx")

    @unittest.skipUnless(DOCX_AVAILABLE, "python-docx is not installed")
    def test_nested_table_text_and_merged_cell_coordinates_are_preserved(self) -> None:
        from docx import Document
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested.docx"
            doc = Document()
            table = doc.add_table(rows=3, cols=3)
            table.cell(0, 0).merge(table.cell(0, 2)).text = "공통 기준"
            table.cell(1, 0).merge(table.cell(2, 0)).text = "세로 기준"
            table.cell(1, 1).text = "내부 표 앞"
            nested = table.cell(1, 1).add_table(rows=1, cols=2)
            nested.cell(0, 0).text = "신청 기한"
            nested.cell(0, 1).text = "3근무일"
            table.cell(1, 1).add_paragraph("내부 표 뒤")
            doc.save(path)
            parsed = DocxParser().parse(path, "doc_nested")
            block = parsed.pages[0].blocks[0]
        for text in ("공통 기준", "세로 기준", "내부 표 앞", "신청 기한", "3근무일", "내부 표 뒤"):
            self.assertEqual(1, block.text.count(text))
        cells = block.metadata["docx_table_cells"]
        self.assertEqual(3, cells[0]["column_span"])
        self.assertEqual("continue", next(c["vertical_merge"] for c in cells if c["row"] == 2 and c["column"] == 0))
        self.assertEqual(1, block.metadata["docx_nested_table_count"])
        self.assertIn("docx_complex_table_layout", parsed.metadata["parser_uncertainty_flags"])
        self.assertEqual("medium", parsed.metadata["parser_uncertainty_risk_level"])
        self.assertLess(block.text.index("신청 기한"), block.text.index("내부 표 뒤"))

    @unittest.skipUnless(DOCX_AVAILABLE, "python-docx is not installed")
    def test_content_controls_preserve_article_order_without_duplicate_table_text(self) -> None:
        from docx import Document
        from docx.oxml import OxmlElement
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "controlled.docx"
            doc = Document()
            doc.add_paragraph("제1조(목적) 원문")
            paragraph = doc.add_paragraph("제2조(신청) 3일 전 신청한다.")
            control, content = OxmlElement("w:sdt"), OxmlElement("w:sdtContent")
            paragraph._p.addprevious(control)
            content.append(paragraph._p)
            control.append(content)
            doc.add_paragraph("제3조(반납) 다음 날 반납한다.")
            doc.save(path)
            parsed = DocxParser().parse(path, "doc_controlled")
        self.assertEqual(["제1조(목적) 원문", "제2조(신청) 3일 전 신청한다.", "제3조(반납) 다음 날 반납한다."],
                         [block.text for block in parsed.pages[0].blocks])


@unittest.skipUnless(DOCX_AVAILABLE, "python-docx is not installed")
class DocxWrappedRunTextTests(unittest.TestCase):
    """변경 추적·스마트 태그·필드 안의 글자를 빠뜨리지 않는다."""

    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

    MC = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'

    def _parse_document(
        self, paragraphs: list[str], *, table_cell: str | None = None, header_text: str | None = None
    ):
        from docx import Document
        from docx.oxml import parse_xml

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wrapped.docx"
            doc = Document()
            body = doc.element.body
            for inner in paragraphs:
                body.insert(len(body) - 1, parse_xml(f"<w:p {self.W} {self.MC}>{inner}</w:p>"))
            if table_cell is not None:
                table = doc.add_table(rows=1, cols=2)
                table.cell(0, 0).text = "구분"
                cell_p = table.cell(0, 1)._tc.p_lst[0]
                cell_p.getparent().replace(cell_p, parse_xml(f"<w:p {self.W}>{table_cell}</w:p>"))
            if header_text is not None:
                doc.sections[0].header.paragraphs[0].text = header_text
            doc.save(path)
            return DocxParser().parse(path, "doc_wrapped")

    def _parse(self, paragraphs: list[str], *, table_cell: str | None = None) -> list[str]:
        parsed = self._parse_document(paragraphs, table_cell=table_cell)
        return [block.text for block in parsed.pages[0].blocks]

    def test_tracked_insertions_are_kept_and_deletions_dropped(self) -> None:
        texts = self._parse(
            [
                '<w:r><w:t xml:space="preserve">① 연차는 </w:t></w:r>'
                '<w:ins w:id="1" w:author="a"><w:r><w:t xml:space="preserve">20일로 </w:t></w:r></w:ins>'
                '<w:del w:id="2" w:author="a"><w:r><w:delText xml:space="preserve">15일로 </w:delText></w:r></w:del>'
                "<w:r><w:t>한다.</w:t></w:r>",
                '<w:ins w:id="3" w:author="a"><w:r><w:t>제3조(특별휴가) 별도로 정한다.</w:t></w:r></w:ins>',
                '<w:moveFrom w:id="4" w:author="a"><w:r><w:t>옮겨 간 문장</w:t></w:r></w:moveFrom>'
                '<w:r><w:t>제5조(남는 문장) 그대로 둔다.</w:t></w:r>',
            ]
        )

        self.assertEqual(
            ["① 연차는 20일로 한다.", "제3조(특별휴가) 별도로 정한다.", "제5조(남는 문장) 그대로 둔다."],
            texts,
        )

    def test_smart_tag_field_and_inline_content_control_text_is_kept(self) -> None:
        texts = self._parse(
            [
                '<w:r><w:t>제4조(인용) 「</w:t></w:r><w:smartTag w:uri="x" w:element="y"><w:r><w:t>근로기준법</w:t></w:r></w:smartTag>'
                "<w:r><w:t>」에 따른다.</w:t></w:r>",
                '<w:r><w:t xml:space="preserve">신청은 </w:t></w:r><w:fldSimple w:instr="REF x"><w:r><w:t>별지 제1호 서식</w:t></w:r></w:fldSimple>'
                "<w:r><w:t>에 따른다.</w:t></w:r>",
                '<w:r><w:t xml:space="preserve">담당 부서는 </w:t></w:r><w:sdt><w:sdtContent><w:r><w:t>인사팀</w:t></w:r></w:sdtContent></w:sdt>'
                "<w:r><w:t>이다.</w:t></w:r>",
            ]
        )

        self.assertEqual(
            ["제4조(인용) 「근로기준법」에 따른다.", "신청은 별지 제1호 서식에 따른다.", "담당 부서는 인사팀이다."],
            texts,
        )

    def test_tracked_insertion_inside_a_table_cell_is_kept(self) -> None:
        texts = self._parse(
            ["<w:r><w:t>제6조(수당) 수당은 다음과 같다.</w:t></w:r>"],
            table_cell='<w:r><w:t xml:space="preserve">월 </w:t></w:r>'
            '<w:ins w:id="5" w:author="a"><w:r><w:t>4만원</w:t></w:r></w:ins>',
        )

        self.assertEqual(["제6조(수당) 수당은 다음과 같다.", "구분 | 월 4만원"], texts)


    def test_tracked_changes_raise_parser_uncertainty_to_medium(self) -> None:
        parsed = self._parse_document(
            [
                '<w:r><w:t xml:space="preserve">① 연차는 </w:t></w:r>'
                '<w:ins w:id="1" w:author="a"><w:r><w:t xml:space="preserve">20일로 </w:t></w:r></w:ins>'
                '<w:del w:id="2" w:author="a"><w:r><w:delText xml:space="preserve">15일로 </w:delText></w:r></w:del>'
                "<w:r><w:t>한다.</w:t></w:r>",
                '<w:moveFrom w:id="3" w:author="a"><w:r><w:t>옮겨 간 문장</w:t></w:r></w:moveFrom>',
                '<w:moveTo w:id="4" w:author="a"><w:r><w:t>옮겨 온 문장</w:t></w:r></w:moveTo>',
            ]
        )

        metadata = parsed.metadata
        self.assertEqual("medium", metadata["parser_uncertainty_risk_level"])
        self.assertIn("docx_tracked_changes_present", metadata["parser_uncertainty_flags"])
        self.assertEqual("review_docx_tracked_changes", metadata["parser_uncertainty_recommendation"])
        self.assertIn("tracked changes", metadata["parser_uncertainty_remediation_hint"])
        self.assertEqual({"ins": 1, "del": 1, "moveFrom": 1, "moveTo": 1}, metadata["docx_tracked_change_counts"])

    def test_paragraph_mark_and_table_cell_tracked_changes_are_detected(self) -> None:
        parsed = self._parse_document(
            ['<w:pPr><w:rPr><w:ins w:id="7" w:author="a"/></w:rPr></w:pPr><w:r><w:t>제1조(목적) 이 규정은 목적을 정한다.</w:t></w:r>'],
            table_cell='<w:ins w:id="8" w:author="a"><w:r><w:t>4만원</w:t></w:r></w:ins>',
        )

        self.assertEqual("medium", parsed.metadata["parser_uncertainty_risk_level"])
        self.assertEqual(2, parsed.metadata["docx_tracked_change_counts"]["ins"])

    def test_tracked_changes_replace_the_low_risk_body_text_flag(self) -> None:
        parsed = self._parse_document(
            ['<w:ins w:id="1" w:author="a"><w:r><w:t>제1조(목적) 이 규정은 목적을 정한다.</w:t></w:r></w:ins>'],
        )

        self.assertEqual(["docx_tracked_changes_present"], parsed.metadata["parser_uncertainty_flags"])
        self.assertNotIn("body_text_extracted", parsed.metadata["parser_uncertainty_flags"])

    def test_tracked_changes_flag_is_combined_with_unparsed_part_flag(self) -> None:
        parsed = self._parse_document(
            ['<w:ins w:id="1" w:author="a"><w:r><w:t>제1조(목적) 이 규정은 목적을 정한다.</w:t></w:r></w:ins>'],
            header_text="머리말",
        )

        self.assertEqual("medium", parsed.metadata["parser_uncertainty_risk_level"])
        self.assertEqual(
            ["docx_tracked_changes_present", "docx_unparsed_parts"], parsed.metadata["parser_uncertainty_flags"]
        )
        # Existing recommendation precedence is unchanged: unparsed parts first.
        self.assertEqual("review_missing_docx_parts", parsed.metadata["parser_uncertainty_recommendation"])
        self.assertIn("tracked changes", parsed.metadata["parser_uncertainty_remediation_hint"])

    def test_document_without_tracked_changes_keeps_low_risk_and_metadata_shape(self) -> None:
        parsed = self._parse_document(["<w:r><w:t>제1조(목적) 이 규정은 목적을 정한다.</w:t></w:r>"])

        self.assertEqual("low", parsed.metadata["parser_uncertainty_risk_level"])
        self.assertEqual(["body_text_extracted"], parsed.metadata["parser_uncertainty_flags"])
        self.assertEqual("none", parsed.metadata["parser_uncertainty_recommendation"])
        self.assertNotIn("docx_tracked_change_counts", parsed.metadata)

    def test_alternate_content_fallback_is_not_duplicated(self) -> None:
        texts = self._parse(
            [
                "<w:r><w:t xml:space=\"preserve\">제8조(대체) </w:t></w:r>"
                "<mc:AlternateContent>"
                '<mc:Choice Requires="w14"><w:r><w:t>선택 본문</w:t></w:r></mc:Choice>'
                "<mc:Fallback><w:r><w:t>선택 본문</w:t></w:r></mc:Fallback>"
                "</mc:AlternateContent>"
                "<w:r><w:t>이다.</w:t></w:r>",
            ]
        )

        self.assertEqual(["제8조(대체) 선택 본문이다."], texts)

    def test_ruby_base_text_is_kept_and_annotation_dropped(self) -> None:
        texts = self._parse(
            [
                "<w:r><w:t>제9조(용어) </w:t></w:r>"
                "<w:r><w:ruby><w:rubyPr><w:rubyAlign w:val=\"center\"/></w:rubyPr>"
                "<w:rt><w:r><w:t>한자</w:t></w:r></w:rt>"
                "<w:rubyBase><w:r><w:t>漢字</w:t></w:r></w:rubyBase></w:ruby></w:r>"
                "<w:r><w:t>를 쓴다.</w:t></w:r>",
            ]
        )

        self.assertEqual(["제9조(용어) 漢字를 쓴다."], texts)

@unittest.skipUnless(DOCX_AVAILABLE, "python-docx is not installed")
class DocxAutomaticNumberingTests(unittest.TestCase):
    """Word 자동 번호(numbering.xml)로 붙은 조·항·호 번호를 본문에 되살린다."""

    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

    # abstractNum 10: 제%1조 / %2(①) / %3.(가나다) / %4)(ㄱㄴㄷ), level 0 linked to style "Article".
    ARTICLE_LIST = (
        '<w:abstractNum w:abstractNumId="10"><w:multiLevelType w:val="multilevel"/>'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:pStyle w:val="Article"/>'
        '<w:lvlText w:val="제%1조"/></w:lvl>'
        '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimalEnclosedCircle"/><w:lvlText w:val="%2"/></w:lvl>'
        '<w:lvl w:ilvl="2"><w:start w:val="1"/><w:numFmt w:val="ganada"/><w:lvlText w:val="%3."/></w:lvl>'
        '<w:lvl w:ilvl="3"><w:start w:val="1"/><w:numFmt w:val="chosung"/><w:lvlText w:val="%4)"/></w:lvl>'
        "</w:abstractNum>"
    )
    BULLET_LIST = (
        '<w:abstractNum w:abstractNumId="20"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/>'
        '<w:lvlText w:val="\u2022"/></w:lvl></w:abstractNum>'
    )

    def _build(self, paragraphs: list[str], abstracts: str, nums: str, *, table_cell: str | None = None):
        from docx import Document
        from docx.oxml import parse_xml

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "numbered.docx"
        doc = Document()
        numbering = doc.part.numbering_part.element
        for child in list(numbering):
            numbering.remove(child)
        for xml in (abstracts, nums):
            for element in parse_xml(f"<w:numbering {self.W}>{xml}</w:numbering>"):
                numbering.append(element)
        styles = doc.styles.element
        styles.append(parse_xml(
            f'<w:style {self.W} w:type="paragraph" w:styleId="Article"><w:name w:val="Article"/>'
            '<w:basedOn w:val="Normal"/><w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:style>'
        ))
        styles.append(parse_xml(
            f'<w:style {self.W} w:type="paragraph" w:styleId="ArticleChild"><w:name w:val="Article Child"/>'
            '<w:basedOn w:val="Article"/></w:style>'
        ))
        body = doc.element.body
        for inner in paragraphs:
            body.insert(len(body) - 1, parse_xml(f"<w:p {self.W}>{inner}</w:p>"))
        if table_cell is not None:
            table = doc.add_table(rows=1, cols=2)
            table.cell(0, 0).text = "구분"
            cell_p = table.cell(0, 1)._tc.p_lst[0]
            cell_p.getparent().replace(cell_p, parse_xml(f"<w:p {self.W}>{table_cell}</w:p>"))
        doc.save(path)
        return DocxParser().parse(path, "doc_numbered")

    @staticmethod
    def _p(text: str, *, style: str | None = None, num_id: int | None = None, ilvl: int | None = None) -> str:
        props = f'<w:pStyle w:val="{style}"/>' if style else ""
        if num_id is not None or ilvl is not None:
            props += "<w:numPr>"
            props += f'<w:ilvl w:val="{ilvl}"/>' if ilvl is not None else ""
            props += f'<w:numId w:val="{num_id}"/>' if num_id is not None else ""
            props += "</w:numPr>"
        return (f"<w:pPr>{props}</w:pPr>" if props else "") + f"<w:r><w:t>{text}</w:t></w:r>"

    def _texts(self, parsed) -> list[str]:
        return [block.text for block in parsed.pages[0].blocks]

    def test_article_numbers_from_style_numbering_are_rendered(self) -> None:
        parsed = self._build(
            [
                self._p("가상기관 복무규정"),
                self._p("(목적) 이 규정은 복무를 정한다.", style="Article"),
                self._p("(정의) 용어의 뜻은 다음과 같다.", style="ArticleChild"),
                self._p("(시행) 공포한 날부터 시행한다.", style="Article"),
            ],
            self.ARTICLE_LIST,
            '<w:num w:numId="1"><w:abstractNumId w:val="10"/></w:num>',
        )

        self.assertEqual(
            ["가상기관 복무규정", "제1조 (목적) 이 규정은 복무를 정한다.",
             "제2조 (정의) 용어의 뜻은 다음과 같다.", "제3조 (시행) 공포한 날부터 시행한다."],
            self._texts(parsed),
        )
        self.assertEqual(3, parsed.metadata["docx_auto_numbered_paragraph_count"])
        self.assertIn("docx_auto_numbering_rendered", parsed.metadata["parser_uncertainty_flags"])
        self.assertEqual("low", parsed.metadata["parser_uncertainty_risk_level"])

    def test_paragraph_and_item_levels_restart_when_the_article_advances(self) -> None:
        parsed = self._build(
            [
                self._p("(휴가) 휴가는 다음과 같다.", style="Article"),
                self._p("연차휴가는 15일로 한다.", num_id=1, ilvl=1),
                self._p("특별휴가는 다음과 같다.", num_id=1, ilvl=1),
                self._p("경조휴가", num_id=1, ilvl=2),
                self._p("본인 결혼", num_id=1, ilvl=3),
                self._p("포상휴가", num_id=1, ilvl=2),
                self._p("(출장) 출장은 승인을 받는다.", style="Article"),
                self._p("출장은 사전에 신청한다.", num_id=1, ilvl=1),
                self._p("긴급 출장", num_id=1, ilvl=2),
            ],
            self.ARTICLE_LIST,
            '<w:num w:numId="1"><w:abstractNumId w:val="10"/></w:num>',
        )

        self.assertEqual(
            ["제1조 (휴가) 휴가는 다음과 같다.", "① 연차휴가는 15일로 한다.", "② 특별휴가는 다음과 같다.",
             "가. 경조휴가", "ㄱ) 본인 결혼", "나. 포상휴가",
             "제2조 (출장) 출장은 승인을 받는다.", "① 출장은 사전에 신청한다.", "가. 긴급 출장"],
            self._texts(parsed),
        )

    def test_start_override_restarts_a_num_instance(self) -> None:
        parsed = self._build(
            [
                self._p("(목적) 목적을 정한다.", num_id=1, ilvl=0),
                self._p("(정의) 용어를 정한다.", num_id=1, ilvl=0),
                self._p("(부칙 시행) 시행일을 정한다.", num_id=2, ilvl=0),
            ],
            self.ARTICLE_LIST,
            '<w:num w:numId="1"><w:abstractNumId w:val="10"/></w:num>'
            '<w:num w:numId="2"><w:abstractNumId w:val="10"/>'
            '<w:lvlOverride w:ilvl="0"><w:startOverride w:val="7"/></w:lvlOverride></w:num>',
        )

        self.assertEqual(
            ["제1조 (목적) 목적을 정한다.", "제2조 (정의) 용어를 정한다.", "제7조 (부칙 시행) 시행일을 정한다."],
            self._texts(parsed),
        )

    def test_bullets_and_unnumbered_paragraphs_are_left_unchanged(self) -> None:
        parsed = self._build(
            [
                self._p("제1조(목적) 손으로 쓴 번호"),
                self._p("첫째 항목", num_id=3, ilvl=0),
                self._p("번호 해제 문단", style="Article", num_id=0),
            ],
            self.ARTICLE_LIST + self.BULLET_LIST,
            '<w:num w:numId="1"><w:abstractNumId w:val="10"/></w:num>'
            '<w:num w:numId="3"><w:abstractNumId w:val="20"/></w:num>',
        )

        self.assertEqual(["제1조(목적) 손으로 쓴 번호", "첫째 항목", "번호 해제 문단"], self._texts(parsed))
        self.assertNotIn("docx_auto_numbered_paragraph_count", parsed.metadata)
        self.assertEqual(["body_text_extracted"], parsed.metadata["parser_uncertainty_flags"])

    def test_letter_roman_and_zero_padded_formats(self) -> None:
        abstract = (
            '<w:abstractNum w:abstractNumId="30">'
            '<w:lvl w:ilvl="0"><w:start w:val="2"/><w:numFmt w:val="upperRoman"/><w:lvlText w:val="%1."/></w:lvl>'
            '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="(%2)"/></w:lvl>'
            '<w:lvl w:ilvl="2"><w:start w:val="9"/><w:numFmt w:val="decimalZero"/>'
            '<w:lvlText w:val="%1-%3"/><w:suff w:val="nothing"/></w:lvl>'
            "</w:abstractNum>"
        )
        parsed = self._build(
            [
                self._p("총칙", num_id=5, ilvl=0),
                self._p("세부", num_id=5, ilvl=1),
                self._p("세부", num_id=5, ilvl=1),
                self._p("호", num_id=5, ilvl=2),
                self._p("호", num_id=5, ilvl=2),
            ],
            abstract,
            '<w:num w:numId="5"><w:abstractNumId w:val="30"/></w:num>',
        )

        self.assertEqual(["II. 총칙", "(a) 세부", "(b) 세부", "II-09호", "II-10호"], self._texts(parsed))

    def test_unsupported_format_falls_back_to_decimal_with_review_flag(self) -> None:
        abstract = (
            '<w:abstractNum w:abstractNumId="40"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
            '<w:numFmt w:val="koreanCounting"/><w:lvlText w:val="제%1장"/></w:lvl></w:abstractNum>'
        )
        parsed = self._build(
            [self._p("총칙", num_id=6, ilvl=0)],
            abstract,
            '<w:num w:numId="6"><w:abstractNumId w:val="40"/></w:num>',
        )

        self.assertEqual(["제1장 총칙"], self._texts(parsed))
        self.assertEqual(["koreanCounting"], parsed.metadata["docx_auto_numbering_fallback_formats"])
        self.assertIn("docx_auto_numbering_format_fallback", parsed.metadata["parser_uncertainty_flags"])
        self.assertEqual("medium", parsed.metadata["parser_uncertainty_risk_level"])
        self.assertEqual("review_docx_list_numbering", parsed.metadata["parser_uncertainty_recommendation"])

    def test_numbered_paragraph_inside_table_cell_gets_its_label(self) -> None:
        parsed = self._build(
            [self._p("(수당) 수당은 다음과 같다.", style="Article")],
            self.ARTICLE_LIST,
            '<w:num w:numId="1"><w:abstractNumId w:val="10"/></w:num>',
            table_cell=self._p("월 4만원", num_id=1, ilvl=1),
        )

        self.assertEqual(["제1조 (수당) 수당은 다음과 같다.", "구분 | ① 월 4만원"], self._texts(parsed))

    def test_structure_detector_finds_auto_numbered_articles(self) -> None:
        from app.processors.structure_detector import StructureDetector

        parsed = self._build(
            [
                self._p("가상기관 여비규정"),
                self._p("(목적) 이 규정은 여비 지급을 정한다.", style="Article"),
                self._p("(지급) 여비는 다음과 같이 지급한다.", style="Article"),
                self._p("국내 여비는 실비로 한다.", num_id=1, ilvl=1),
                self._p("국외 여비는 정액으로 한다.", num_id=1, ilvl=1),
                self._p("(정산) 여비는 귀임 후 정산한다.", style="Article"),
            ],
            self.ARTICLE_LIST,
            '<w:num w:numId="1"><w:abstractNumId w:val="10"/></w:num>',
        )

        nodes = StructureDetector().detect(parsed)
        articles = [node for node in nodes if node.node_type == "article"]
        self.assertEqual(["목적", "지급", "정산"], [node.title for node in articles])
        paragraphs = [node for node in nodes if node.node_type == "paragraph" and node.text.startswith(("①", "②"))]
        self.assertEqual(2, len(paragraphs))


if __name__ == "__main__":
    unittest.main()
