from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from app.parsers.archive_safety import (
    OfficeArchiveLimits,
    read_archive_member_bounded,
    validate_office_archive,
    validate_office_archive_file_size,
)
from app.parsers.base import BaseParser, ParserError, document_name_from_path, parser_uncertainty_metadata
from app.parsers.xml_safety import reject_unsafe_xml_declarations
from app.schemas.parsed import ParsedBlock, ParsedDocument, ParsedPage


class HwpxParser(BaseParser):
    supported_extensions = {".hwpx"}
    NOTE_TAGS = {"footnote", "endnote", "footnotes", "endnotes"}
    IMAGE_TAGS = {"pic", "image", "img"}
    TABLE_TAGS = {"tbl", "table"}
    ROW_TAGS = {"tr", "row"}
    CELL_TAGS = {"tc", "cell"}
    STRUCTURAL_INLINE_TAGS = TABLE_TAGS | IMAGE_TAGS | NOTE_TAGS | {"caption"}

    def __init__(self, *, archive_limits: OfficeArchiveLimits | None = None) -> None:
        self.archive_limits = archive_limits or OfficeArchiveLimits()

    def parse(self, path: Path, document_id: str) -> ParsedDocument:
        validate_office_archive_file_size(path, format_name="HWPX", limits=self.archive_limits)
        if not zipfile.is_zipfile(path):
            raise ParserError("HWPX file is not a valid zip archive.")

        blocks: list[ParsedBlock] = []
        raw_parts: list[str] = []
        parse_error_sections: list[str] = []
        xml_role_counts: dict[str, int] = {}
        try:
            with zipfile.ZipFile(path) as archive:
                infos = validate_office_archive(
                    archive,
                    format_name="HWPX",
                    limits=self.archive_limits,
                )
                xml_infos = sorted(
                    (
                        info
                        for info in infos
                        if info.filename.lower().endswith(".xml")
                        and (
                            "section" in info.filename.lower()
                            or "bodytext" in info.filename.lower()
                            or "contents" in info.filename.lower()
                        )
                    ),
                    key=lambda info: self._section_sort_key(info.filename),
                )
                for info in xml_infos:
                    xml_role = self._xml_role(info.filename)
                    xml_role_counts[xml_role] = xml_role_counts.get(xml_role, 0) + 1
                    payload = read_archive_member_bounded(
                        archive,
                        info,
                        format_name="HWPX",
                        max_bytes=self.archive_limits.max_entry_uncompressed_bytes,
                    )
                    try:
                        reject_unsafe_xml_declarations(payload, format_name="HWPX")
                        root = ElementTree.fromstring(payload)
                    except ElementTree.ParseError:
                        parse_error_sections.append(info.filename)
                        continue
                    for block in self._blocks(root, info.filename, xml_role=xml_role):
                        if block.text:
                            blocks.append(block)
                            raw_parts.append(block.text)
        except ParserError:
            raise
        except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError, OSError) as exc:
            raise ParserError("Failed to parse HWPX archive safely.") from exc

        if not blocks:
            raise ParserError("No text blocks were extracted from the HWPX file.")

        flagged_tables = self._has_parser_review_flags(blocks)
        flags = self._document_uncertainty_flags(blocks)
        non_body_xml = any(
            str((block.metadata or {}).get("source_xml_role") or "unknown") != "body"
            for block in blocks
        )
        if non_body_xml:
            flags = sorted({*flags, "hwpx_non_body_xml_content"})
        if parse_error_sections:
            flags = sorted({*flags, "hwpx_section_parse_error"})
        needs_review = flagged_tables or bool(parse_error_sections) or non_body_xml
        if flagged_tables:
            recommendation = "review_flagged_tables"
            remediation_hint = (
                "Review HWPX tables, captions, notes, images, and merged cells flagged by the parser before approval."
            )
        elif parse_error_sections:
            recommendation = "review_dropped_sections"
            remediation_hint = (
                "One or more HWPX sections could not be parsed and were dropped; review the source before approval: "
                + ", ".join(parse_error_sections)
            )
        elif non_body_xml:
            recommendation = "review_non_body_xml"
            remediation_hint = (
                "HWPX XML outside body sections was extracted with an explicit role; review metadata/header content "
                "before approval."
            )
        else:
            recommendation = "none"
            remediation_hint = ""

        return ParsedDocument(
            document_id=document_id,
            source_file=path.name,
            document_name=document_name_from_path(path),
            file_type="hwpx",
            pages=[ParsedPage(page_no=1, blocks=blocks)],
            raw_text="\n".join(raw_parts),
            metadata={
                **parser_uncertainty_metadata(
                    source="hwpx",
                    risk_level="low" if not needs_review else "medium",
                    flags=flags,
                    confidence=0.92 if not needs_review else 0.82,
                    recommendation=recommendation,
                    remediation_hint=remediation_hint,
                ),
                "hwpx_xml_role_counts": dict(sorted(xml_role_counts.items())),
            },
        )

    def _section_sort_key(self, filename: str) -> tuple[int, str]:
        match = re.search(r"section(\d+)", filename, flags=re.IGNORECASE)
        return (int(match.group(1)) if match else 0, filename.lower())

    def _xml_role(self, filename: str) -> str:
        normalized = str(filename or "").replace("\\", "/").casefold()
        basename = normalized.rsplit("/", 1)[-1]
        if re.fullmatch(r"section\d+\.xml", basename) or "/bodytext/" in normalized:
            return "body"
        if any(token in basename for token in ("header", "manifest", "meta")):
            return "metadata"
        return "unknown"

    def _has_parser_review_flags(self, blocks: list[ParsedBlock]) -> bool:
        return any((block.metadata or {}).get("hwpx_parser_review_flags") for block in blocks)

    def _document_uncertainty_flags(self, blocks: list[ParsedBlock]) -> list[str]:
        flags = {"xml_structured_extraction"}
        for block in blocks:
            for flag in (block.metadata or {}).get("hwpx_parser_review_flags") or []:
                if str(flag or "").strip():
                    flags.add(f"hwpx_{str(flag).strip()}")
        return sorted(flags)

    def _blocks(
        self,
        root: ElementTree.Element,
        xml_file: str,
        *,
        xml_role: str = "unknown",
    ) -> list[ParsedBlock]:
        blocks: list[ParsedBlock] = []
        for block_index, (block_type, text, metadata) in enumerate(self._iter_blocks(root), start=1):
            clean = self._clean_table_text(text) if block_type == "table" else self._clean_text(text)
            if clean:
                blocks.append(
                    ParsedBlock(
                        type=block_type,
                        text=clean,
                        metadata={
                            "xml_file": xml_file,
                            "source_xml_role": xml_role,
                            "hwpx_xml_block_index": block_index,
                            **metadata,
                        },
                    )
                )
        return blocks

    def _iter_blocks(self, element: ElementTree.Element) -> list[tuple[str, str, dict]]:
        blocks: list[tuple[str, str, dict]] = []
        self._collect_blocks(element, blocks)
        if not blocks:
            for text in self._loose_text_runs(element):
                blocks.append(("text", text, {"hwpx_block_type": "loose_text"}))
        return blocks

    def _collect_blocks(self, element: ElementTree.Element, blocks: list[tuple[str, str, dict]]) -> None:
        tag = self._local_name(element)
        if tag in self.TABLE_TAGS:
            for caption in self._caption_texts(element):
                blocks.append(
                    (
                        "text",
                        caption,
                        {
                            "hwpx_block_type": "caption",
                            "caption_parent": "table",
                            "hwpx_parser_review_flags": ["caption", "table_caption"],
                        },
                    )
                )
            table_text = self._table_text(element)
            if table_text.strip():
                blocks.append(("table", table_text, self._table_metadata(element)))
            return
        if tag in self.IMAGE_TAGS:
            captions = self._caption_texts(element)
            image_text = "\n".join(captions) or self._element_text(element)
            if image_text.strip():
                metadata = {
                    "hwpx_block_type": "image",
                    "caption_count": len(captions),
                    "hwpx_image_caption_count": len(captions),
                }
                if captions:
                    metadata["hwpx_parser_review_flags"] = ["image_caption"]
                blocks.append(
                    (
                        "image",
                        image_text,
                        metadata,
                    )
                )
            return
        if tag == "caption":
            caption_text = self._element_text(element)
            if caption_text.strip():
                blocks.append(("text", caption_text, {"hwpx_block_type": "caption", "hwpx_parser_review_flags": ["caption"]}))
            return
        if tag in self.NOTE_TAGS:
            note_text = self._element_text(element)
            if note_text.strip():
                blocks.append(("text", note_text, {"hwpx_block_type": tag, "hwpx_parser_review_flags": [tag]}))
            return
        if tag in {"p", "para"}:
            if self._has_structural_inline_child(element):
                text = self._inline_text_excluding(element, self.STRUCTURAL_INLINE_TAGS)
                if text.strip():
                    blocks.append(("text", text, {"hwpx_block_type": "paragraph"}))
                self._collect_structural_inline_blocks(element, blocks)
                return
            text = "".join(element.itertext())
            if text.strip():
                blocks.append(("text", text, {"hwpx_block_type": "paragraph"}))
            return
        for child in list(element):
            self._collect_blocks(child, blocks)

    def _table_text(self, table: ElementTree.Element) -> str:
        rows: list[str] = []
        for row in self._outer_table_rows(table):
            cells: list[str] = []
            for cell in list(row):
                if self._local_name(cell) not in self.CELL_TAGS:
                    continue
                cell_text = self._clean_text(" ".join(part.strip() for part in cell.itertext() if part.strip()))
                if cell_text:
                    cells.append(cell_text)
            if cells:
                rows.append(" | ".join(cells))
        if rows:
            return "\n".join(rows)
        return "".join(table.itertext())

    def _table_metadata(self, table: ElementTree.Element) -> dict:
        rows = list(self._outer_table_rows(table))
        cells = [cell for row in rows for cell in list(row) if self._local_name(cell) in self.CELL_TAGS]
        direct_captions = self._direct_table_captions(table)
        image_captions = self._table_image_captions(table)
        note_snippets = self._descendant_text_snippets(table, self.NOTE_TAGS)
        nested_table_snippets = self._nested_table_text_snippets(table)
        caption_count = len(self._caption_texts(table))
        nested_table_count = self._descendant_count(table, self.TABLE_TAGS)
        image_count = self._descendant_count(table, self.IMAGE_TAGS)
        note_count = self._descendant_count(table, self.NOTE_TAGS)
        merged_cell_count = sum(1 for cell in table.iter() if self._local_name(cell) in self.CELL_TAGS and self._has_cell_span(cell))
        flags: list[str] = []
        if caption_count:
            flags.append("table_caption")
        if nested_table_count:
            flags.append("nested_table")
        if image_count:
            flags.append("table_image")
        if note_count:
            flags.append("table_note")
        if merged_cell_count:
            flags.append("merged_cell")

        metadata: dict = {
            "hwpx_block_type": "table",
            "hwpx_table_row_count": len(rows),
            "hwpx_table_cell_count": len(cells),
            "hwpx_table_caption_count": caption_count,
            "hwpx_nested_table_count": nested_table_count,
            "hwpx_table_image_count": image_count,
            "hwpx_table_note_count": note_count,
            "hwpx_merged_cell_count": merged_cell_count,
        }
        if direct_captions:
            metadata["hwpx_table_direct_captions"] = direct_captions
        if image_captions:
            metadata["hwpx_table_image_captions"] = image_captions
        if note_snippets:
            metadata["hwpx_table_note_snippets"] = note_snippets
        if nested_table_snippets:
            metadata["hwpx_nested_table_text_snippets"] = nested_table_snippets
        if flags:
            metadata["hwpx_parser_review_flags"] = flags
        return metadata

    def _outer_table_rows(self, table: ElementTree.Element) -> list[ElementTree.Element]:
        rows: list[ElementTree.Element] = []

        def collect(element: ElementTree.Element) -> None:
            for child in list(element):
                tag = self._local_name(child)
                if tag in self.TABLE_TAGS:
                    continue
                if tag in self.ROW_TAGS:
                    rows.append(child)
                    continue
                collect(child)

        collect(table)
        return rows

    def _descendant_count(self, element: ElementTree.Element, tag_names: set[str]) -> int:
        return sum(1 for descendant in element.iter() if descendant is not element and self._local_name(descendant) in tag_names)

    def _has_cell_span(self, cell: ElementTree.Element) -> bool:
        for key, value in cell.attrib.items():
            local_key = key.rsplit("}", 1)[-1].lower()
            if local_key not in {"rowspan", "colspan"}:
                continue
            try:
                if int(str(value).strip()) > 1:
                    return True
            except ValueError:
                if str(value).strip() not in {"", "0", "1"}:
                    return True
        return False

    def _caption_texts(self, element: ElementTree.Element) -> list[str]:
        captions: list[str] = []
        for descendant in element.iter():
            if descendant is element or self._local_name(descendant) != "caption":
                continue
            caption_text = self._element_text(descendant)
            if caption_text:
                captions.append(caption_text)
        return captions

    def _direct_table_captions(self, table: ElementTree.Element) -> list[str]:
        captions: list[str] = []
        for child in list(table):
            if self._local_name(child) != "caption":
                continue
            caption_text = self._element_text(child)
            if caption_text:
                captions.append(caption_text)
        return captions[:5]

    def _table_image_captions(self, table: ElementTree.Element) -> list[str]:
        captions: list[str] = []
        for descendant in table.iter():
            if descendant is table or self._local_name(descendant) not in self.IMAGE_TAGS:
                continue
            for caption in self._caption_texts(descendant):
                if caption not in captions:
                    captions.append(caption)
        return captions[:5]

    def _descendant_text_snippets(self, element: ElementTree.Element, tag_names: set[str], *, limit: int = 5) -> list[str]:
        snippets: list[str] = []
        for descendant in element.iter():
            if descendant is element or self._local_name(descendant) not in tag_names:
                continue
            snippet = self._element_text(descendant)
            if snippet and snippet not in snippets:
                snippets.append(snippet[:160])
            if len(snippets) >= limit:
                break
        return snippets

    def _nested_table_text_snippets(self, table: ElementTree.Element, *, limit: int = 5) -> list[str]:
        snippets: list[str] = []
        for descendant in table.iter():
            if descendant is table or self._local_name(descendant) not in self.TABLE_TAGS:
                continue
            snippet = self._clean_text(" ".join(part.strip() for part in descendant.itertext() if part.strip()))
            if snippet and snippet not in snippets:
                snippets.append(snippet[:160])
            if len(snippets) >= limit:
                break
        return snippets

    def _element_text(self, element: ElementTree.Element) -> str:
        return self._clean_text(" ".join(part.strip() for part in element.itertext() if part.strip()))

    def _has_structural_inline_child(self, element: ElementTree.Element) -> bool:
        for descendant in element.iter():
            if descendant is element:
                continue
            if self._local_name(descendant) in self.STRUCTURAL_INLINE_TAGS:
                return True
        return False

    def _collect_structural_inline_blocks(self, element: ElementTree.Element, blocks: list[tuple[str, str, dict]]) -> None:
        for child in list(element):
            tag = self._local_name(child)
            if tag in self.STRUCTURAL_INLINE_TAGS:
                self._collect_blocks(child, blocks)
                continue
            self._collect_structural_inline_blocks(child, blocks)

    def _inline_text_excluding(self, element: ElementTree.Element, excluded_tags: set[str]) -> str:
        parts: list[str] = []

        def collect(current: ElementTree.Element) -> None:
            if current is not element and self._local_name(current) in excluded_tags:
                return
            if current.text:
                parts.append(current.text)
            for child in list(current):
                collect(child)
                if child.tail:
                    parts.append(child.tail)

        collect(element)
        return "".join(parts)

    def _local_name(self, element: ElementTree.Element) -> str:
        return element.tag.rsplit("}", 1)[-1].lower()

    def _loose_text_runs(self, root: ElementTree.Element) -> list[str]:
        candidates: list[str] = []
        for element in root.iter():
            if self._local_name(element) != "t":
                continue
            text = "".join(element.itertext())
            if text.strip():
                candidates.append(text)
        return candidates

    def _clean_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    def _clean_table_text(self, text: str) -> str:
        lines = []
        for line in text.splitlines():
            clean = re.sub(r"\s+", " ", line).strip()
            if clean:
                lines.append(clean)
        return "\n".join(lines)
