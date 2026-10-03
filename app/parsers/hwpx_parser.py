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
from app.parsers.xml_safety import elementtree_xml_input, reject_unsafe_xml_declarations
from app.schemas.parsed import ParsedBlock, ParsedDocument, ParsedPage


# 한컴은 그림 개체(hp:pic)의 hp:shapeComment에 "그림입니다. 원본 그림의 이름: …
# 원본 그림의 크기: 가로 …pixel, 세로 …pixel" 같은 설명을 자동으로 넣는다. 본문이
# 아니므로 이 형식으로만 이루어진 설명의 "텍스트"는 뺀다. 그림 자체는 남겨서 호출부가
# 짧은 자리표시자(HWPX_IMAGE_PLACEHOLDER_TEXT)로 image 블록을 계속 만든다. 그래야 캡션 없는
# 스캔 그림(별표 등)도 image_blocks_detected 검수 사유와 OCR 후보 판정에 그대로 잡힌다.
# 사용자가 고쳐 쓴 설명이나 hp:caption은 그대로 둔다.
HWPX_IMAGE_PLACEHOLDER_TEXT = "[그림]"
HWPX_IMAGE_DESCRIPTION_LEAD_PATTERN = re.compile(r"그림입니다\.?")
HWPX_IMAGE_DESCRIPTION_FIELD_SPLIT_PATTERN = re.compile(
    r"\s*(?=(?:원본\s*그림의\s*(?:이름|크기)|사진\s*찍은\s*날짜|프로그램\s*이름)\s*:)"
)
HWPX_IMAGE_ORIGINAL_NAME_FIELD_PATTERN = re.compile(r"원본\s*그림의\s*이름\s*:\s*(?P<name>.+)")
# 프로그램 이름은 "Adobe Photoshop CS6", "Microsoft Office PowerPoint"처럼 짧은 영문 제품명
# 모양일 때만 자동 설명으로 본다. 한글·콜론·문장부호가 섞인 뒤따르는 글은 작성자가 덧붙인
# 내용일 수 있으므로 매칭하지 않고(= 설명 전체를 본문으로 남기고) 안전한 쪽으로 처리한다.
HWPX_IMAGE_DESCRIPTION_FIELD_PATTERNS = (
    re.compile(r"원본\s*그림의\s*이름\s*:\s*(?:.{0,240}\.[A-Za-z0-9]{2,5}|\S{1,240})"),
    re.compile(r"원본\s*그림의\s*크기\s*:\s*가로\s*\d+\s*(?:pixel|픽셀)\s*,\s*세로\s*\d+\s*(?:pixel|픽셀)"),
    re.compile(r"사진\s*찍은\s*날짜\s*:\s*[0-9년월일시분초오전후AaPpMm:./\-\s]{1,60}"),
    re.compile(r"프로그램\s*이름\s*:\s*[A-Za-z0-9][A-Za-z0-9 .,+\-_()/&®™©]{0,79}"),
)
HWPX_IMAGE_NAME_METADATA_LIMIT = 120


class HwpxParser(BaseParser):
    supported_extensions = {".hwpx"}
    NOTE_TAGS = {"footnote", "endnote", "footnotes", "endnotes"}
    IMAGE_TAGS = {"pic", "image", "img"}
    TABLE_TAGS = {"tbl", "table"}
    ROW_TAGS = {"tr", "row"}
    CELL_TAGS = {"tc", "cell"}
    STRUCTURAL_INLINE_TAGS = TABLE_TAGS | IMAGE_TAGS | NOTE_TAGS | {"caption"}
    # Running page header/footer controls live inside the first paragraph's
    # run. Their text is page furniture, not regulation body text.
    PAGE_FURNITURE_TAGS = {"header", "footer"}
    # The dropped furniture is recorded in document metadata so a reviewer can
    # see it, and a confidentiality marking in it (e.g. a "대외비" footer) must
    # not disappear before the approver sets the security level. Matching runs
    # on text with whitespace/middle dots removed ("대 외 비" is common).
    PAGE_FURNITURE_TEXT_LIMIT = 80
    PAGE_FURNITURE_MAX_RECORDED_TEXTS = 5
    CONFIDENTIALITY_MARKINGS = (
        "대외비",
        "비공개",
        "내부용",
        "보안",
        "기밀",
        "극비",
        "사외비",
        "취급주의",
        "confidential",
    )
    # Hancom stores these as empty elements inside <hp:t>; itertext() drops
    # them, which glued words together ("이<nbSpace/>규정은" -> "이규정은").
    INLINE_SPACE_TAGS = {"tab", "nbspace", "fwspace"}
    INLINE_LINE_BREAK_TAGS = {"linebreak"}
    # Placeholder for a paragraph-internal line break until whitespace is
    # collapsed, so XML indentation never turns into a line break.
    LINE_BREAK_SENTINEL = "\u2028"

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
        page_furniture: list[str] = []
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
                        root = ElementTree.fromstring(elementtree_xml_input(payload))
                    except (ElementTree.ParseError, LookupError, UnicodeError, ValueError) as exc:
                        if xml_role == "body":
                            raise ParserError(
                                "HWPX body XML is malformed; processing stopped to prevent missing regulation content."
                            ) from exc
                        parse_error_sections.append(info.filename)
                        continue
                    for block in self._blocks(root, info.filename, xml_role=xml_role, page_furniture=page_furniture):
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
        furniture_metadata, confidentiality_markers = self._page_furniture_metadata(page_furniture)
        if furniture_metadata:
            flags = sorted({*flags, "hwpx_page_header_footer_excluded"})
        if confidentiality_markers:
            flags = sorted({*flags, "hwpx_confidentiality_marking_in_header_footer"})
        needs_review = (
            flagged_tables or bool(parse_error_sections) or non_body_xml or bool(confidentiality_markers)
        )
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
        if confidentiality_markers:
            # Page header/footer text is excluded from the body, so a marking
            # there never reaches a chunk. Ask for the security level first.
            security_hint = (
                "A confidentiality marking ("
                + ", ".join(confidentiality_markers)
                + ") was found in an HWPX page header/footer that is excluded from body text; "
                "check the document security level before approval."
            )
            recommendation = "review_security_level"
            remediation_hint = " ".join(part for part in (security_hint, remediation_hint) if part)

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
                **furniture_metadata,
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

    def _confidentiality_markers(self, text: str) -> list[str]:
        compact = re.sub(r"[\s\u00b7\u318d\u30fb]+", "", str(text or "")).casefold()
        return [marking for marking in self.CONFIDENTIALITY_MARKINGS if marking in compact]

    def _page_furniture_metadata(self, page_furniture: list[str]) -> tuple[dict, list[str]]:
        """Summarize excluded page header/footer controls for document metadata.

        Returns (metadata, confidentiality markers). Texts that carry a
        confidentiality marking are recorded first so the bounded list never
        hides them behind ordinary running headers.
        """
        if not page_furniture:
            return {}, []
        marked: list[str] = []
        unmarked: list[str] = []
        markers: set[str] = set()
        for text in dict.fromkeys(item for item in page_furniture if item):
            found = self._confidentiality_markers(text)
            markers.update(found)
            (marked if found else unmarked).append(text)
        recorded = [
            text[: self.PAGE_FURNITURE_TEXT_LIMIT].rstrip()
            for text in (marked + unmarked)[: self.PAGE_FURNITURE_MAX_RECORDED_TEXTS]
        ]
        metadata: dict = {
            "hwpx_page_header_footer_excluded_count": len(page_furniture),
            "hwpx_page_header_footer_excluded_texts": recorded,
        }
        sorted_markers = sorted(markers)
        if sorted_markers:
            metadata["hwpx_page_header_footer_confidentiality_markers"] = sorted_markers
        return metadata, sorted_markers

    def _blocks(
        self,
        root: ElementTree.Element,
        xml_file: str,
        *,
        xml_role: str = "unknown",
        page_furniture: list[str] | None = None,
    ) -> list[ParsedBlock]:
        blocks: list[ParsedBlock] = []
        for block_index, (block_type, text, metadata) in enumerate(
            self._iter_blocks(root, page_furniture), start=1
        ):
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

    def _iter_blocks(
        self, element: ElementTree.Element, page_furniture: list[str] | None = None
    ) -> list[tuple[str, str, dict]]:
        blocks: list[tuple[str, str, dict]] = []
        self._collect_blocks(element, blocks, page_furniture)
        if not blocks:
            for text in self._loose_text_runs(element):
                blocks.append(("text", text, {"hwpx_block_type": "loose_text"}))
        return blocks

    def _collect_blocks(
        self,
        element: ElementTree.Element,
        blocks: list[tuple[str, str, dict]],
        page_furniture: list[str] | None = None,
    ) -> None:
        tag = self._local_name(element)
        if tag in self.PAGE_FURNITURE_TAGS:
            # Whole subtree (text, tables, pictures) is page furniture.
            if page_furniture is not None:
                page_furniture.append(self._element_text(element))
            return
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
            if not table_text.strip() and self._omitted_image_descriptions(element):
                # 그림만 든 표(스캔한 별표를 표 안에 붙인 경우 등)도 표 블록과 검수 신호를 유지한다.
                table_text = HWPX_IMAGE_PLACEHOLDER_TEXT
            if table_text.strip():
                blocks.append(("table", table_text, self._table_metadata(element)))
            return
        if tag in self.IMAGE_TAGS:
            captions = self._caption_texts(element)
            image_text = "\n".join(captions) or self._element_text(element)
            omitted = self._omitted_image_descriptions(element)
            if omitted and not image_text.strip():
                # 자동 설명만 있던 그림. 설명 텍스트는 버리되 그림 자리는 남긴다.
                image_text = HWPX_IMAGE_PLACEHOLDER_TEXT
            if image_text.strip():
                metadata = {
                    "hwpx_block_type": "image",
                    "caption_count": len(captions),
                    "hwpx_image_caption_count": len(captions),
                }
                if captions:
                    metadata["hwpx_parser_review_flags"] = ["image_caption"]
                if omitted:
                    metadata["hwpx_image_description_omitted"] = True
                    original_name = self._image_original_name(omitted)
                    if original_name:
                        metadata["hwpx_image_original_name"] = original_name
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
                text = self._inline_text_excluding(
                    element,
                    self.STRUCTURAL_INLINE_TAGS | self.PAGE_FURNITURE_TAGS,
                    page_furniture,
                )
                if text.strip():
                    blocks.append(("text", text, {"hwpx_block_type": "paragraph"}))
                self._collect_structural_inline_blocks(element, blocks)
                return
            text = self._inline_text_excluding(element, self.PAGE_FURNITURE_TAGS, page_furniture)
            if text.strip():
                blocks.append(("text", text, {"hwpx_block_type": "paragraph"}))
            return
        for child in list(element):
            self._collect_blocks(child, blocks, page_furniture)

    def _table_text(self, table: ElementTree.Element) -> str:
        rows: list[str] = []
        for row in self._outer_table_rows(table):
            cells: list[str] = []
            for cell in list(row):
                if self._local_name(cell) not in self.CELL_TAGS:
                    continue
                cell_text = self._clean_text(" ".join(part.strip() for part in self._text_parts(cell) if part.strip()))
                if cell_text:
                    cells.append(cell_text)
            if cells:
                rows.append(" | ".join(cells))
        if rows:
            return "\n".join(rows)
        return "".join(self._text_parts(table))

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
        # Hancom writes spans on a <hp:cellSpan colSpan=".." rowSpan=".."/>
        # child; some producers put them on the cell itself.
        span_attributes = list(cell.attrib.items())
        for child in list(cell):
            if self._local_name(child) == "cellspan":
                span_attributes.extend(child.attrib.items())
        for key, value in span_attributes:
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
            snippet = self._clean_text(" ".join(part.strip() for part in self._text_parts(descendant) if part.strip()))
            if snippet and snippet not in snippets:
                snippets.append(snippet[:160])
            if len(snippets) >= limit:
                break
        return snippets

    def _element_text(self, element: ElementTree.Element) -> str:
        return self._clean_text(" ".join(part.strip() for part in self._text_parts(element) if part.strip()))

    def _text_parts(self, element: ElementTree.Element) -> list[str]:
        """``itertext()`` without Hancom's auto-generated image descriptions."""

        parts: list[str] = []
        stack: list[tuple[ElementTree.Element, bool]] = [(element, False)]
        while stack:
            current, emit_tail = stack.pop()
            if emit_tail:
                if current.tail:
                    parts.append(current.tail)
                continue
            if not isinstance(current.tag, str):
                # itertext()와 같이 주석·처리 지시문의 내용은 건너뛰고 tail만 남긴다.
                continue
            if current is not element and self._is_image_description_boilerplate(current):
                continue
            if current.text:
                parts.append(current.text)
            for child in reversed(list(current)):
                if child.tail:
                    stack.append((child, True))
                stack.append((child, False))
        return parts

    def _is_image_description_boilerplate(self, element: ElementTree.Element) -> bool:
        return self._image_description_fields(element) is not None

    def _image_description_fields(self, element: ElementTree.Element) -> list[str] | None:
        """Return the fields of an auto-generated image description, else ``None``."""

        if self._local_name(element) != "shapecomment":
            return None
        text = self._clean_text("".join(element.itertext()))
        lead = HWPX_IMAGE_DESCRIPTION_LEAD_PATTERN.match(text)
        if lead is None:
            return None
        fields = [
            field.strip()
            for field in HWPX_IMAGE_DESCRIPTION_FIELD_SPLIT_PATTERN.split(text[lead.end() :])
            if field.strip()
        ]
        if all(
            any(pattern.fullmatch(field) for pattern in HWPX_IMAGE_DESCRIPTION_FIELD_PATTERNS)
            for field in fields
        ):
            return fields
        return None

    def _omitted_image_descriptions(self, element: ElementTree.Element) -> list[list[str]]:
        """Auto-generated descriptions below ``element`` that ``_text_parts`` leaves out."""

        omitted: list[list[str]] = []
        for descendant in element.iter():
            if descendant is element or not isinstance(descendant.tag, str):
                continue
            fields = self._image_description_fields(descendant)
            if fields is not None:
                omitted.append(fields)
        return omitted

    def _image_original_name(self, omitted: list[list[str]]) -> str | None:
        """Original picture file name for metadata only; never a local path."""

        for fields in omitted:
            for field in fields:
                match = HWPX_IMAGE_ORIGINAL_NAME_FIELD_PATTERN.fullmatch(field)
                if match is None:
                    continue
                name = self._clean_text(match.group("name"))
                if (
                    name
                    and len(name) <= HWPX_IMAGE_NAME_METADATA_LIMIT
                    and not re.search(r"[\\/]|^[A-Za-z]:|^~|\.\.", name)
                ):
                    return name
        return None

    def _has_structural_inline_child(self, element: ElementTree.Element) -> bool:
        # Page header/footer subtrees are not walked: a table or picture inside
        # a running header is page furniture, not an inline body block.
        stack = list(element)
        while stack:
            descendant = stack.pop()
            tag = self._local_name(descendant)
            if tag in self.PAGE_FURNITURE_TAGS:
                continue
            if tag in self.STRUCTURAL_INLINE_TAGS:
                return True
            stack.extend(descendant)
        return False

    def _collect_structural_inline_blocks(self, element: ElementTree.Element, blocks: list[tuple[str, str, dict]]) -> None:
        for child in list(element):
            tag = self._local_name(child)
            if tag in self.PAGE_FURNITURE_TAGS:
                continue
            if tag in self.STRUCTURAL_INLINE_TAGS:
                self._collect_blocks(child, blocks)
                continue
            self._collect_structural_inline_blocks(child, blocks)

    def _inline_text_excluding(
        self,
        element: ElementTree.Element,
        excluded_tags: set[str],
        page_furniture: list[str] | None = None,
    ) -> str:
        parts: list[str] = []

        def collect(current: ElementTree.Element) -> None:
            tag = self._local_name(current)
            if current is not element and tag in excluded_tags:
                if page_furniture is not None and tag in self.PAGE_FURNITURE_TAGS:
                    page_furniture.append(self._element_text(current))
                return
            if tag in self.INLINE_LINE_BREAK_TAGS:
                parts.append(self.LINE_BREAK_SENTINEL)
            elif tag in self.INLINE_SPACE_TAGS:
                parts.append(" ")
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
        stack = [root]
        while stack:
            element = stack.pop()
            tag = self._local_name(element)
            if tag in self.PAGE_FURNITURE_TAGS:
                continue
            if tag == "t":
                text = "".join(element.itertext())
                if text.strip():
                    candidates.append(text)
            stack.extend(reversed(list(element)))
        return candidates

    def _clean_text(self, text: str) -> str:
        # Collapse whitespace (XML indentation included) inside each line and
        # keep only explicit <hp:lineBreak/> breaks, so "제5조(휴가)" / "① …" /
        # "② …" written with Shift+Enter stay on separate lines.
        lines = [re.sub(r"\s+", " ", part).strip() for part in text.split(self.LINE_BREAK_SENTINEL)]
        return "\n".join(line for line in lines if line)

    def _clean_table_text(self, text: str) -> str:
        lines = []
        for line in text.splitlines():
            clean = re.sub(r"\s+", " ", line).strip()
            if clean:
                lines.append(clean)
        return "\n".join(lines)
