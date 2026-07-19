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
            from docx.text.paragraph import Paragraph
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

        metadata: dict[str, Any] = {
            "docx_unparsed_parts": unparsed_parts,
        }
        if unparsed_parts:
            metadata.update(
                parser_uncertainty_metadata(
                    source="docx",
                    risk_level="medium",
                    flags=["docx_unparsed_parts"],
                    confidence=0.72,
                    recommendation="review_missing_docx_parts",
                    remediation_hint=(
                        "Review DOCX parts not included in body order extraction before approval: "
                        + ", ".join(unparsed_parts)
                        + "."
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

    def _table_text(self, table: Any) -> str:
        rows: list[str] = []
        for row in table.rows:
            cells: list[str] = []
            previous_tc = None
            for cell in row.cells:
                # A cell merged across grid columns is yielded once per column
                # by python-docx, all sharing the same underlying <w:tc>.
                if cell._tc is previous_tc:
                    continue
                previous_tc = cell._tc
                cells.append(self._cell_text(cell.text))
            row_text = " | ".join(cells).strip()
            if row_text:
                rows.append(row_text)
        return "\n".join(rows).strip()

    def _cell_text(self, text: str) -> str:
        return " ".join(part.strip() for part in text.splitlines() if part.strip())
