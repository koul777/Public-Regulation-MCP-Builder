from __future__ import annotations

from pathlib import Path
from typing import Any
import zipfile

from app.parsers.archive_safety import (
    OfficeArchiveLimits,
    validate_office_archive,
    validate_office_archive_file_size,
)
from app.parsers.base import BaseParser, ParserError, document_name_from_path
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
            from docx.text.paragraph import Paragraph
        except ImportError as exc:
            raise ParserError("DOCX parsing requires python-docx. Install package 'python-docx'.") from exc

        try:
            with path.open("rb") as source:
                with zipfile.ZipFile(source) as archive:
                    validate_office_archive(
                        archive,
                        format_name="DOCX",
                        limits=self.archive_limits,
                    )
                source.seek(0)
                doc = DocxDocument(source)
        except ParserError:
            raise
        except Exception as exc:
            raise ParserError(f"Failed to parse DOCX file: {exc}") from exc

        blocks: list[ParsedBlock] = []
        raw_parts: list[str] = []

        for child in doc.element.body.iterchildren():
            if isinstance(child, CT_P):
                paragraph = Paragraph(child, doc)
                text = paragraph.text.strip()
                if text:
                    blocks.append(ParsedBlock(text=text))
                    raw_parts.append(text)
            elif isinstance(child, CT_Tbl):
                table = Table(child, doc)
                table_text = self._table_text(table)
                if table_text:
                    blocks.append(ParsedBlock(type="table", text=table_text))
                    raw_parts.append(table_text)

        if not blocks:
            raise ParserError("No text blocks were extracted from the DOCX file.")

        return ParsedDocument(
            document_id=document_id,
            source_file=path.name,
            document_name=document_name_from_path(path),
            file_type="docx",
            pages=[ParsedPage(page_no=1, blocks=blocks)],
            raw_text="\n".join(raw_parts),
        )

    def _table_text(self, table: Any) -> str:
        rows: list[str] = []
        for row in table.rows:
            cells = [self._cell_text(cell.text) for cell in row.cells]
            row_text = " | ".join(cells).strip()
            if row_text:
                rows.append(row_text)
        return "\n".join(rows).strip()

    def _cell_text(self, text: str) -> str:
        return " ".join(part.strip() for part in text.splitlines() if part.strip())
