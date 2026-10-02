from __future__ import annotations

from dataclasses import dataclass, field
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
        numbering = _render_list_labels(doc)
        labels = numbering.labels

        for child in self._body_items(doc.element.body):
            if isinstance(child, CT_P):
                text = self._paragraph_text(child, labels).strip()
                if text:
                    blocks.append(ParsedBlock(text=text))
                    raw_parts.append(text)
            elif isinstance(child, CT_Tbl):
                table = Table(child, doc)
                table_text = self._table_text(table, labels)
                if table_text:
                    blocks.append(
                        ParsedBlock(type="table", text=table_text, metadata=self._table_layout(table, labels))
                    )
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
        tracked_changes = self._tracked_change_counts(doc.element.body)
        if tracked_changes:
            metadata["docx_tracked_change_counts"] = tracked_changes

        # (flag, recommendation, hint) in recommendation precedence order.
        reasons: list[tuple[str, str, str]] = []
        if unparsed_parts:
            reasons.append((
                "docx_unparsed_parts",
                "review_missing_docx_parts",
                "Review DOCX parts not included in body order extraction before approval: "
                + ", ".join(unparsed_parts) + ".",
            ))
        if complex_tables:
            reasons.append((
                "docx_complex_table_layout",
                "review_docx_table_layout",
                "Compare merged and nested table cell relationships with the original document.",
            ))
        if tracked_changes:
            counts = ", ".join(f"{name}={count}" for name, count in tracked_changes.items())
            reasons.append((
                "docx_tracked_changes_present",
                "review_docx_tracked_changes",
                "The DOCX has pending tracked changes (" + counts + "); extracted text follows Word's "
                "accepted view (inserted text kept, deleted or moved-away text dropped). Accept or reject "
                "the changes in Word, or compare the text with the original, before approval.",
            ))

        # Word draws automatic list numbers ("제1조", "①", "가.") from numbering.xml;
        # they are not in the runs. Record when the parser rendered them so a
        # reviewer can compare article numbers with the original. Article-level
        # (제N조) and circled-number (①) labels become chunk ids and citation
        # keys, so rendering one is a medium-risk signal rather than a note.
        numbering_flags: list[str] = []
        if labels:
            metadata["docx_auto_numbered_paragraph_count"] = len(labels)
            numbering_flags.append("docx_auto_numbering_rendered")
        if numbering.fallback_formats:
            metadata["docx_auto_numbering_fallback_formats"] = numbering.fallback_formats
            numbering_flags.append("docx_auto_numbering_format_fallback")
            reasons.append((
                "docx_auto_numbering_format_fallback",
                "review_docx_list_numbering",
                "Automatic list numbers in unsupported formats or ranges were rendered as decimals ("
                + ", ".join(numbering.fallback_formats) + "); compare them with the original.",
            ))

        if reasons:
            metadata.update(
                parser_uncertainty_metadata(
                    source="docx",
                    risk_level="medium",
                    flags=[reason[0] for reason in reasons] + numbering_flags,
                    confidence=0.72,
                    recommendation=reasons[0][1],
                    remediation_hint=" ".join(reason[2] for reason in reasons),
                )
            )
        else:
            metadata.update(
                parser_uncertainty_metadata(
                    source="docx",
                    risk_level="low",
                    flags=["body_text_extracted", *numbering_flags],
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

    def _table_layout(self, table: Any, labels: dict[Any, str] | None = None) -> dict[str, Any]:
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
                              "text": self._cell_content(cell, labels), "nested_table_count": len(cell.tables)})
                column += span
        return {"docx_table_cells": cells, "docx_table_row_count": len(table.rows),
                "docx_table_column_count": len(table.columns),
                "docx_nested_table_count": sum(c["nested_table_count"] for c in cells)}

    def _cell_content(self, cell: Any, labels: dict[Any, str] | None = None) -> str:
        from docx.table import Table
        parts = []
        for child in self._body_items(cell._tc):
            if child.tag.rsplit("}", 1)[-1] == "tbl":
                text = self._table_text(Table(child, cell), labels)
            else:
                text = self._paragraph_text(child, labels)
            if text.strip():
                parts.append(text.strip())
        return "\n".join(parts)

    # Runs Word shows with tracked changes accepted: inserted text (w:ins),
    # smart tags, simple fields and inline content controls are kept; deleted
    # or moved-away text is not, and text boxes stay out as before. Two more
    # exclusions avoid double or foreign text: the mc:Fallback branch of
    # mc:AlternateContent (a duplicate of the mc:Choice content; matched with
    # local-name()/namespace-uri() because python-docx's nsmap has no mc prefix)
    # and w:rt (ruby annotations; the ruby base text in w:rubyBase is kept).
    _MC_NAMESPACE = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    _VISIBLE_RUN_XPATH = (
        ".//w:r[not(ancestor::w:del) and not(ancestor::w:moveFrom) and not(ancestor::w:txbxContent)"
        " and not(ancestor::w:rt)"
        f" and not(ancestor::*[local-name()='Fallback' and namespace-uri()='{_MC_NAMESPACE}'])]"
    )
    _TRACKED_CHANGE_TAGS = ("ins", "del", "moveFrom", "moveTo")

    def _paragraph_text(self, paragraph: Any, labels: dict[Any, str] | None = None) -> str:
        """Return a paragraph's visible text, including runs nested in wrappers.

        python-docx ``Paragraph.text`` joins only direct ``w:r``/``w:hyperlink``
        children, so text inside ``w:ins``, ``w:smartTag``, ``w:fldSimple`` or an
        inline ``w:sdt`` was silently dropped. An automatic list label rendered by
        ``_render_list_labels`` is prefixed the way Word displays it.
        """

        text = "".join(run.text for run in paragraph.xpath(self._VISIBLE_RUN_XPATH))
        label = labels.get(paragraph) if labels else None
        return label + text if label else text

    def _tracked_change_counts(self, body: Any) -> dict[str, int]:
        """Count pending tracked changes in the body (empty when there are none).

        Text-bearing wrappers (w:ins, w:del, w:moveFrom, w:moveTo) and their
        paragraph-mark or table-row counterparts under w:rPr/w:trPr all count:
        any of them means the indexed text depends on accepting the changes.
        """

        counts = {
            tag: len(body.xpath(f".//w:{tag}")) for tag in self._TRACKED_CHANGE_TAGS
        }
        return counts if any(counts.values()) else {}

    def _table_text(self, table: Any, labels: dict[Any, str] | None = None) -> str:
        from docx.table import _Cell
        rows: list[str] = []
        for row in table.rows:
            cells: list[str] = []
            # Physical cells preserve vertical continuation and omitted grid
            # positions; row.cells repeats merged origin text in later rows.
            for tc in row._tr.tc_lst:
                cell = _Cell(tc, table)
                cells.append(self._cell_text(self._cell_content(cell, labels)))
            row_text = " | ".join(cells).strip()
            if row_text:
                rows.append(row_text)
        return "\n".join(rows).strip()

    def _cell_text(self, text: str) -> str:
        return " ".join(part.strip() for part in text.splitlines() if part.strip())


# --- Automatic list numbering -------------------------------------------------
#
# python-docx returns run text only; Word draws list labels such as "제1조",
# "①" or "가." from word/numbering.xml. The helpers below replay Word's counters
# over the document once, in document order, and map each numbered paragraph
# element to the label (plus separator) Word would display in front of it.

_MAX_LIST_LEVEL = 8
_GANADA = "가나다라마바사아자차카타파하"
_CHOSUNG = "ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ"
_ROMAN = ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
          (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))


@dataclass
class _ListLevel:
    start: int = 1
    num_format: str = "decimal"
    text: str | None = None
    restart: int | None = None
    suffix: str = "tab"
    legal: bool = False
    style_id: str | None = None


@dataclass
class _ListLabels:
    labels: dict[Any, str] = field(default_factory=dict)
    fallback_formats: list[str] = field(default_factory=list)


def _w_val(element: Any, path: str) -> str | None:
    from docx.oxml.ns import qn

    found = element.find(path) if path else element
    if found is None:
        return None
    return found.get(qn("w:val"))


def _w_int(element: Any, path: str) -> int | None:
    value = _w_val(element, path)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _read_level(lvl: Any, base: _ListLevel | None = None) -> _ListLevel:
    from docx.oxml.ns import qn

    level = _ListLevel(**vars(base)) if base else _ListLevel()
    start = _w_int(lvl, qn("w:start"))
    if start is not None:
        level.start = start
    num_format = _w_val(lvl, qn("w:numFmt"))
    if num_format:
        level.num_format = num_format
    if lvl.find(qn("w:lvlText")) is not None:
        level.text = _w_val(lvl, qn("w:lvlText")) or ""
    restart = _w_int(lvl, qn("w:lvlRestart"))
    if restart is not None:
        level.restart = restart
    suffix = _w_val(lvl, qn("w:suff"))
    if suffix:
        level.suffix = suffix
    legal = lvl.find(qn("w:isLgl"))
    if legal is not None:
        level.legal = (legal.get(qn("w:val")) or "1").lower() not in {"0", "false", "off"}
    style_id = _w_val(lvl, qn("w:pStyle"))
    if style_id:
        level.style_id = style_id
    return level


def _format_number(value: int, num_format: str) -> str | None:
    """Render ``value`` like Word's ``w:numFmt``; ``None`` if it cannot be shown exactly."""
    if num_format == "none":
        return ""
    if num_format == "decimal":
        return str(value)
    if num_format == "decimalZero":
        return f"{value:02d}" if 0 <= value < 10 else str(value)
    if num_format in {"decimalEnclosedCircle", "decimalEnclosedCircleChinese"} and 1 <= value <= 20:
        return chr(0x2460 + value - 1)
    if num_format == "ganada" and 1 <= value <= len(_GANADA):
        return _GANADA[value - 1]
    if num_format == "chosung" and 1 <= value <= len(_CHOSUNG):
        return _CHOSUNG[value - 1]
    if num_format in {"upperLetter", "lowerLetter"} and value >= 1:
        letter = chr(ord("A") + (value - 1) % 26) * ((value - 1) // 26 + 1)
        return letter if num_format == "upperLetter" else letter.lower()
    if num_format in {"upperRoman", "lowerRoman"} and 1 <= value < 4000:
        remaining, roman = value, ""
        for amount, symbol in _ROMAN:
            count, remaining = divmod(remaining, amount)
            roman += symbol * count
        return roman if num_format == "upperRoman" else roman.lower()
    return None


class _Numbering:
    def __init__(self, numbering_element: Any, styles_element: Any) -> None:
        from docx.oxml.ns import qn

        self.abstract_levels: dict[str, dict[int, _ListLevel]] = {}
        self.abstract_style_links: dict[str, str] = {}
        for abstract in numbering_element.iterchildren(qn("w:abstractNum")):
            abstract_id = abstract.get(qn("w:abstractNumId"))
            if abstract_id is None:
                continue
            levels: dict[int, _ListLevel] = {}
            for lvl in abstract.iterchildren(qn("w:lvl")):
                try:
                    ilvl = int(lvl.get(qn("w:ilvl")) or "")
                except ValueError:
                    continue
                if 0 <= ilvl <= _MAX_LIST_LEVEL:
                    levels[ilvl] = _read_level(lvl)
            self.abstract_levels[abstract_id] = levels
            link = _w_val(abstract, qn("w:numStyleLink"))
            if link:
                self.abstract_style_links[abstract_id] = link
        # numId -> (abstractNumId, per-level overrides, startOverride values)
        self.nums: dict[str, tuple[str, dict[int, Any], dict[int, int]]] = {}
        for num in numbering_element.iterchildren(qn("w:num")):
            num_id = num.get(qn("w:numId"))
            abstract_id = _w_val(num, qn("w:abstractNumId"))
            if num_id is None or abstract_id is None:
                continue
            overrides: dict[int, Any] = {}
            starts: dict[int, int] = {}
            for override in num.iterchildren(qn("w:lvlOverride")):
                try:
                    ilvl = int(override.get(qn("w:ilvl")) or "")
                except ValueError:
                    continue
                lvl = override.find(qn("w:lvl"))
                if lvl is not None:
                    overrides[ilvl] = lvl
                start = _w_int(override, qn("w:startOverride"))
                if start is not None:
                    starts[ilvl] = start
            self.nums[num_id] = (abstract_id, overrides, starts)

        self.styles: dict[str, Any] = {}
        self.default_paragraph_style: str | None = None
        if styles_element is not None:
            for style in styles_element.iterchildren(qn("w:style")):
                style_id = style.get(qn("w:styleId"))
                if style_id is None:
                    continue
                self.styles[style_id] = style
                if (
                    style.get(qn("w:type")) == "paragraph"
                    and (style.get(qn("w:default")) or "").lower() in {"1", "true", "on"}
                ):
                    self.default_paragraph_style = style_id
        self._level_cache: dict[str, dict[int, _ListLevel]] = {}
        self._style_cache: dict[str, tuple[str | None, int | None, str | None]] = {}

    def _abstract_for(self, abstract_id: str) -> str:
        """Follow ``w:numStyleLink`` once to the list style's real definition."""
        from docx.oxml.ns import qn

        link = self.abstract_style_links.get(abstract_id)
        style = self.styles.get(link) if link else None
        if style is None:
            return abstract_id
        linked_num = _w_val(style, f"{qn('w:pPr')}/{qn('w:numPr')}/{qn('w:numId')}")
        linked = self.nums.get(linked_num or "")
        return linked[0] if linked else abstract_id

    def levels(self, num_id: str) -> dict[int, _ListLevel]:
        cached = self._level_cache.get(num_id)
        if cached is None:
            abstract_id, overrides, starts = self.nums[num_id]
            cached = dict(self.abstract_levels.get(self._abstract_for(abstract_id), {}))
            for ilvl, lvl in overrides.items():
                if 0 <= ilvl <= _MAX_LIST_LEVEL:
                    cached[ilvl] = _read_level(lvl, cached.get(ilvl))
            for ilvl, start in starts.items():
                if ilvl in cached:
                    cached[ilvl] = _ListLevel(**{**vars(cached[ilvl]), "start": start})
            self._level_cache[num_id] = cached
        return cached

    def counter_key(self, num_id: str) -> tuple[str, str]:
        # Word continues one list across w:num instances sharing an abstractNum;
        # an instance with w:startOverride ("restart numbering") counts on its own.
        abstract_id, overrides, starts = self.nums[num_id]
        if starts or overrides:
            return ("num", num_id)
        return ("abstract", self._abstract_for(abstract_id))

    def _style_numbering(self, style_id: str | None) -> tuple[str | None, int | None, str | None]:
        """Return (numId, ilvl, defining style) inherited through ``w:basedOn``."""
        from docx.oxml.ns import qn

        if style_id is None:
            return None, None, None
        cached = self._style_cache.get(style_id)
        if cached is not None:
            return cached
        num_id: str | None = None
        ilvl: int | None = None
        numbering_style: str | None = None
        seen: set[str] = set()
        current: str | None = style_id
        while current and current not in seen and (num_id is None or ilvl is None):
            seen.add(current)
            style = self.styles.get(current)
            if style is None:
                break
            num_pr = style.find(f"{qn('w:pPr')}/{qn('w:numPr')}")
            if num_pr is not None:
                if num_id is None:
                    num_id = _w_val(num_pr, qn("w:numId"))
                    if num_id is not None:
                        numbering_style = current
                if ilvl is None:
                    ilvl = _w_int(num_pr, qn("w:ilvl"))
            current = _w_val(style, qn("w:basedOn"))
        resolved = (num_id, ilvl, numbering_style)
        self._style_cache[style_id] = resolved
        return resolved

    def paragraph_numbering(self, paragraph: Any) -> tuple[str, int] | None:
        """Resolve (numId, ilvl) from direct ``w:numPr`` and the style chain."""
        from docx.oxml.ns import qn

        num_id: str | None = None
        ilvl: int | None = None
        style_id = self.default_paragraph_style
        p_pr = paragraph.find(qn("w:pPr"))
        if p_pr is not None:
            num_pr = p_pr.find(qn("w:numPr"))
            if num_pr is not None:
                num_id = _w_val(num_pr, qn("w:numId"))
                ilvl = _w_int(num_pr, qn("w:ilvl"))
            style_id = _w_val(p_pr, qn("w:pStyle")) or style_id
        numbering_style: str | None = None
        if num_id is None or ilvl is None:
            style_num_id, style_ilvl, numbering_style = self._style_numbering(style_id)
            if num_id is None:
                num_id = style_num_id
            else:
                numbering_style = None
            if ilvl is None:
                ilvl = style_ilvl
        if num_id is None or num_id == "0" or num_id not in self.nums:
            return None
        if ilvl is None and numbering_style is not None:
            ilvl = next(
                (index for index, level in self.levels(num_id).items() if level.style_id == numbering_style),
                None,
            )
        return num_id, ilvl or 0


def _render_list_labels(doc: Any) -> _ListLabels:
    """Map numbered paragraph elements to the label Word displays for them."""
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml.ns import qn

    result = _ListLabels()
    try:
        numbering_element = doc.part.part_related_by(RT.NUMBERING).element
    except KeyError:
        return result
    try:
        styles_element = doc.part.part_related_by(RT.STYLES).element
    except KeyError:
        styles_element = None
    numbering = _Numbering(numbering_element, styles_element)
    if not numbering.nums:
        return result

    counters: dict[tuple[str, str], list[int | None]] = {}
    fallback_formats: set[str] = set()
    skipped_ancestors = {qn("w:txbxContent"), qn("w:del"), qn("w:moveFrom")}
    for paragraph in doc.element.body.iter(qn("w:p")):
        resolved = numbering.paragraph_numbering(paragraph)
        if resolved is None:
            continue
        if any(ancestor.tag in skipped_ancestors for ancestor in paragraph.iterancestors()):
            continue
        num_id, ilvl = resolved
        levels = numbering.levels(num_id)
        level = levels.get(ilvl)
        if level is None:
            continue
        values = counters.setdefault(numbering.counter_key(num_id), [None] * (_MAX_LIST_LEVEL + 1))
        values[ilvl] = level.start if values[ilvl] is None else values[ilvl] + 1
        for deeper in range(ilvl + 1, _MAX_LIST_LEVEL + 1):
            restart = levels[deeper].restart if deeper in levels else None
            # lvlRestart is one-based; 0 means "never restart".
            if restart is None or (restart > 0 and ilvl <= restart - 1):
                values[deeper] = None
        if level.num_format == "bullet" or not level.text:
            continue
        # An empty numbered paragraph still advances Word's counter, but it was
        # never emitted as a block; do not turn it into a bare "제N조" line.
        if not "".join(run.text for run in paragraph.xpath(DocxParser._VISIBLE_RUN_XPATH)).strip():
            continue

        def render(match_level: int) -> str:
            referenced = levels.get(match_level)
            if referenced is None:
                return ""
            value = values[match_level] if values[match_level] is not None else referenced.start
            num_format = "decimal" if level.legal and referenced.num_format != "none" else referenced.num_format
            rendered = _format_number(value, num_format)
            if rendered is None:
                # Unknown format, or a value outside it (e.g. the 15th 가나다 item):
                # show the decimal and ask a reviewer to compare with the original.
                fallback_formats.add(num_format)
                return str(value)
            return rendered

        label = ""
        text = level.text
        index = 0
        while index < len(text):
            char = text[index]
            if char == "%" and index + 1 < len(text) and text[index + 1] in "123456789":
                label += render(int(text[index + 1]) - 1)
                index += 2
            else:
                label += char
                index += 1
        if label.strip():
            separator = "" if level.suffix == "nothing" else " "
            result.labels[paragraph] = label + separator
    result.fallback_formats = sorted(fallback_formats)
    return result
