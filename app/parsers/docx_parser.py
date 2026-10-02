from __future__ import annotations

from pathlib import Path
from typing import Any
import zipfile

from app.parsers.archive_safety import (
    OfficeArchiveLimits,
    read_archive_member_bounded,
    validate_office_archive,
    validate_office_archive_file_size,
)
from app.parsers.base import BaseParser, ParserError, document_name_from_path, parser_uncertainty_metadata
from app.schemas.parsed import ParsedBlock, ParsedDocument, ParsedPage


class DocxParser(BaseParser):
    supported_extensions = {".docx"}

    def __init__(self, *, archive_limits: OfficeArchiveLimits | None = None) -> None:
        self.archive_limits = archive_limits or OfficeArchiveLimits()

    def parse(self, path: Path, document_id: str) -> ParsedDocument:
        validate_office_archive_file_size(path, format_name="DOCX", limits=self.archive_limits)
        try:
            from docx import Document as DocxDocument
            from docx.oxml.table import CT_Tbl
            from docx.oxml.text.paragraph import CT_P
            from docx.table import Table
        except ImportError as exc:
            raise ParserError("DOCX parsing requires python-docx. Install package 'python-docx'.") from exc

        try:
            with path.open("rb") as source:
                with zipfile.ZipFile(source) as archive:
                    infos = validate_office_archive(
                        archive,
                        format_name="DOCX",
                        limits=self.archive_limits,
                    )
                    unparsed_parts = self._unparsed_parts(archive, infos)
                source.seek(0)
                doc = DocxDocument(source)
        except ParserError:
            raise
        except Exception as exc:
            raise ParserError(f"Failed to parse DOCX file: {exc}") from exc

        blocks: list[ParsedBlock] = []
        raw_parts: list[str] = []

        for child in self._body_items(doc.element.body):
            if isinstance(child, CT_P):
                text = self._paragraph_text(child).strip()
                if text:
                    blocks.append(ParsedBlock(text=text))
                    raw_parts.append(text)
            elif isinstance(child, CT_Tbl):
                table = Table(child, doc)
                table_text = self._table_text(table)
                if table_text:
                    blocks.append(ParsedBlock(type="table", text=table_text, metadata=self._table_layout(table)))
                    raw_parts.append(table_text)

        if not blocks:
            raise ParserError("No text blocks were extracted from the DOCX file.")

        metadata: dict[str, Any] = {
            "docx_unparsed_parts": unparsed_parts,
        }
        complex_tables = [block for block in blocks if block.type == "table" and (
            block.metadata.get("docx_nested_table_count") or any(
                cell["column_span"] > 1 or cell["vertical_merge"]
                for cell in block.metadata.get("docx_table_cells", [])
            )
        )]
        metadata["docx_complex_table_count"] = len(complex_tables)
        if unparsed_parts or complex_tables:
            flags = (["docx_unparsed_parts"] if unparsed_parts else []) + (
                ["docx_complex_table_layout"] if complex_tables else []
            )
            metadata.update(
                parser_uncertainty_metadata(
                    source="docx",
                    risk_level="medium",
                    flags=flags,
                    confidence=0.72,
                    recommendation="review_missing_docx_parts" if unparsed_parts else "review_docx_table_layout",
                    remediation_hint=(
                        ("Review DOCX parts not included in body order extraction before approval: "
                         + ", ".join(unparsed_parts) + ". " if unparsed_parts else "")
                        + ("Compare merged and nested table cell relationships with the original document."
                           if complex_tables else "")
                    ),
                )
            )
        else:
            metadata.update(
                parser_uncertainty_metadata(
                    source="docx",
                    risk_level="low",
                    flags=["body_text_extracted"],
                    confidence=0.95,
                )
            )

        return ParsedDocument(
            document_id=document_id,
            source_file=path.name,
            document_name=document_name_from_path(path),
            file_type="docx",
            pages=[ParsedPage(page_no=1, blocks=blocks)],
            raw_text="\n".join(raw_parts),
            metadata=metadata,
        )

    def _unparsed_parts(self, archive: zipfile.ZipFile, infos: list[zipfile.ZipInfo]) -> list[str]:
        """Detect text-bearing OOXML parts that body iteration does not preserve.

        The parser intentionally keeps the existing body paragraph/table order. This
        helper only emits review metadata for related parts instead of injecting them
        at an unknown location in the document stream.
        """
        names = {str(info.filename) for info in infos}
        detected: list[str] = []
        for name in sorted(names):
            normalized = name.casefold()
            if not normalized.startswith("word/") or not normalized.endswith(".xml"):
                continue
            part_name = normalized.rsplit("/", 1)[-1]
            if (
                part_name.startswith("header")
                or part_name.startswith("footer")
                or part_name in {"footnotes.xml", "endnotes.xml", "comments.xml", "glossary.document.xml"}
            ):
                detected.append(name)

        document_info = next((info for info in infos if str(info.filename).casefold() == "word/document.xml"), None)
        if document_info is not None:
            document_xml = read_archive_member_bounded(
                archive,
                document_info,
                format_name="DOCX",
                max_bytes=min(self.archive_limits.max_entry_uncompressed_bytes, 8 * 1024 * 1024),
            )
            if b"txbxContent" in document_xml:
                detected.append("word/document.xml#w:txbxContent")
            if b"altChunk" in document_xml:
                detected.append("word/document.xml#w:altChunk")
        return sorted(set(detected), key=str.casefold)

    def _body_items(self, parent: Any):
        """Unwrap content controls without walking a table's paragraphs twice."""
        for child in parent.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag in {"p", "tbl"}:
                yield child
            elif tag in {"sdt", "sdtContent", "customXml", "ins", "moveTo"}:
                yield from self._body_items(child)

    def _table_layout(self, table: Any) -> dict[str, Any]:
        from docx.table import _Cell
        cells = []
        for row_index, row in enumerate(table.rows):
            column = int(getattr(row, "grid_cols_before", 0) or 0)
            for tc in row._tr.tc_lst:
                span = int(tc.grid_span or 1)
                merge = tc.vMerge
                cell = _Cell(tc, table)
                cells.append({"row": row_index, "column": column, "column_span": span,
                              "vertical_merge": str(merge or ""),
                              "text": self._cell_content(cell), "nested_table_count": len(cell.tables)})
                column += span
        return {"docx_table_cells": cells, "docx_table_row_count": len(table.rows),
                "docx_table_column_count": len(table.columns),
                "docx_nested_table_count": sum(c["nested_table_count"] for c in cells)}

    def _cell_content(self, cell: Any) -> str:
        from docx.table import Table
        parts = []
        for child in self._body_items(cell._tc):
            if child.tag.rsplit("}", 1)[-1] == "tbl":
                text = self._table_text(Table(child, cell))
            else:
                text = self._paragraph_text(child)
            if text.strip():
                parts.append(text.strip())
        return "\n".join(parts)

    # Runs Word shows with tracked changes accepted: inserted text (w:ins),
    # smart tags, simple fields and inline content controls are kept; deleted
    # or moved-away text is not, and text boxes stay out as before.
    _VISIBLE_RUN_XPATH = (
        ".//w:r[not(ancestor::w:del) and not(ancestor::w:moveFrom) and not(ancestor::w:txbxContent)]"
    )

    def _paragraph_text(self, paragraph: Any) -> str:
        """Return a paragraph's visible text, including runs nested in wrappers.

        python-docx ``Paragraph.text`` joins only direct ``w:r``/``w:hyperlink``
        children, so text inside ``w:ins``, ``w:smartTag``, ``w:fldSimple`` or an
        inline ``w:sdt`` was silently dropped.
        """

        return "".join(run.text for run in paragraph.xpath(self._VISIBLE_RUN_XPATH))

    def _table_text(self, table: Any) -> str:
        from docx.table import _Cell
        rows: list[str] = []
        for row in table.rows:
            cells: list[str] = []
            # Physical cells preserve vertical continuation and omitted grid
            # positions; row.cells repeats merged origin text in later rows.
            for tc in row._tr.tc_lst:
                cell = _Cell(tc, table)
                cells.append(self._cell_text(self._cell_content(cell)))
            row_text = " | ".join(cells).strip()
            if row_text:
                rows.append(row_text)
        return "\n".join(rows).strip()

    def _cell_text(self, text: str) -> str:
        return " ".join(part.strip() for part in text.splitlines() if part.strip())
