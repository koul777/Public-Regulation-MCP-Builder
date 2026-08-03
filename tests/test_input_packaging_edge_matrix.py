from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile
import unittest

from app.retrieval.hierarchical_index import load_article_records, regulation_toc
from app.schemas.parsed import ParsedBlock, ParsedDocument, ParsedPage
from tests.test_input_packaging_parity import (
    _article_projection,
    _build_runtime_bundle,
    _catalog_projection,
    _public_toc_projection,
)


@dataclass(frozen=True)
class _RegulationFixture:
    title: str
    title_line: str | None = None
    repeated_running_header: bool = False
    trailing_lines: tuple[str, ...] = ()

    def body_lines(self) -> tuple[str, ...]:
        title_line = self.title_line or self.title
        middle = (title_line,) if self.repeated_running_header else ()
        return (
            title_line,
            "2026. 7. 1. 일부개정",
            "제1장 총칙",
            f"제1조(목적) {self.title}의 운영 기준을 정한다.",
            *middle,
            f"제2조(적용범위) {self.title}은 모든 직원에게 적용한다.",
            *self.trailing_lines,
        )


class InputPackagingEdgeMatrixTests(unittest.TestCase):
    def test_common_regulation_suffixes_have_packaging_parity(self) -> None:
        fixtures = (
            _RegulationFixture("인사운영세칙"),
            _RegulationFixture("출장비지급기준"),
            _RegulationFixture("공직자행동강령"),
        )

        self._assert_packaging_parity(fixtures)

    def test_repeated_running_headers_have_packaging_parity(self) -> None:
        fixtures = (
            _RegulationFixture("인사규정", repeated_running_header=True),
            _RegulationFixture("보수규정", repeated_running_header=True),
        )

        self._assert_packaging_parity(fixtures)

    def test_wrapped_titles_with_inline_revision_have_packaging_parity(self) -> None:
        fixtures = (
            _RegulationFixture("인사규정", "「인사규정」 (2026. 7. 1. 일부개정)"),
            _RegulationFixture("보수규정", "【보수규정】 (2026. 7. 1. 전부개정)"),
        )

        self._assert_packaging_parity(fixtures)

    def test_contents_longer_than_sixty_lines_has_packaging_parity(self) -> None:
        fixtures = (
            _RegulationFixture("인사규정"),
            _RegulationFixture("보수규정"),
            _RegulationFixture("복무규정"),
        )

        self._assert_packaging_parity(fixtures, long_contents=True)

    def test_appendix_and_supplementary_before_next_page_unit_have_packaging_parity(self) -> None:
        fixtures = (
            _RegulationFixture(
                "인사규정",
                trailing_lines=(
                    "[별표 1] 인사 평가 기준",
                    "등급 | 점수",
                    "A | 90",
                    "부칙 <2026.7.1.>",
                    "제1조(시행일) 이 규정은 2026년 7월 1일부터 시행한다.",
                ),
            ),
            _RegulationFixture("보수규정"),
        )

        self._assert_packaging_parity(fixtures)

    def _assert_packaging_parity(
        self,
        fixtures: tuple[_RegulationFixture, ...],
        *,
        long_contents: bool = False,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            separate = _build_runtime_bundle(
                root / "separate",
                [_standalone_document(fixture, index) for index, fixture in enumerate(fixtures, start=1)],
            )
            combined = _build_runtime_bundle(
                root / "combined",
                [_combined_document(fixtures, long_contents=long_contents)],
            )

            separate_catalog = _catalog_projection(separate.index_path)
            combined_catalog = _catalog_projection(combined.index_path)
            self.assertEqual(separate_catalog, combined_catalog)
            self.assertEqual(set(fixture.title for fixture in fixtures), set(separate_catalog))
            self.assertEqual(separate.logical_corpus_sha256, combined.logical_corpus_sha256)

            for fixture in fixtures:
                separate_unit_id = separate_catalog[fixture.title]["regulation_unit_id"]
                combined_unit_id = combined_catalog[fixture.title]["regulation_unit_id"]
                self.assertEqual(separate_unit_id, combined_unit_id)

                separate_toc = _public_toc_projection(
                    regulation_toc(separate.index_path, regulation_unit_id=separate_unit_id)
                )
                combined_toc = _public_toc_projection(
                    regulation_toc(combined.index_path, regulation_unit_id=combined_unit_id)
                )
                self.assertEqual(separate_toc, combined_toc)
                self.assertEqual(
                    _article_parent_path_projection(separate_toc),
                    _article_parent_path_projection(combined_toc),
                )

                for article_no in ("제1조", "제2조"):
                    separate_articles = load_article_records(
                        separate.index_path,
                        separate.vector_path,
                        regulation_unit_id=separate_unit_id,
                        article_no=article_no,
                    )
                    combined_articles = load_article_records(
                        combined.index_path,
                        combined.vector_path,
                        regulation_unit_id=combined_unit_id,
                        article_no=article_no,
                    )
                    self.assertTrue(separate_articles, (fixture.title, article_no))
                    self.assertEqual(
                        _article_projection(separate_articles),
                        _article_projection(combined_articles),
                    )


def _standalone_document(fixture: _RegulationFixture, index: int) -> ParsedDocument:
    body = "\n".join(fixture.body_lines())
    return ParsedDocument(
        document_id=f"edge-standalone-{index}",
        source_file=f"{fixture.title}.pdf",
        document_name=fixture.title,
        file_type="pdf",
        pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=body)])],
        raw_text=body,
    )


def _combined_document(
    fixtures: tuple[_RegulationFixture, ...],
    *,
    long_contents: bool,
) -> ParsedDocument:
    contents_lines = ["목차"]
    for index, fixture in enumerate(fixtures, start=1):
        contents_lines.append(f"1-1-{index}. {fixture.title} ........................ {index * 10}")
        if long_contents and index == 2:
            contents_lines.extend(f"분류 안내 {line}" for line in range(61))
    contents = "\n".join(contents_lines)
    pages = [ParsedPage(page_no=1, blocks=[ParsedBlock(text=contents)])]
    pages.extend(
        ParsedPage(
            page_no=index * 10,
            blocks=[ParsedBlock(text="\n".join(fixture.body_lines()))],
        )
        for index, fixture in enumerate(fixtures, start=1)
    )
    raw_text = "\n".join(block.text for page in pages for block in page.blocks)
    return ParsedDocument(
        document_id="edge-combined-regulation-book",
        source_file="통합_규정집.pdf",
        document_name="통합 규정집",
        file_type="pdf",
        pages=pages,
        raw_text=raw_text,
    )


def _article_parent_path_projection(toc: dict) -> list[dict[str, str | None]]:
    return [
        {
            "parent_id": node["parent_id"],
            "hierarchy_path": node["hierarchy_path"],
        }
        for node in toc["nodes"]
        if node["node_type"] == "article"
    ]
