from __future__ import annotations

from collections import Counter
from datetime import date
import re
import unicodedata
import zlib
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree

from app.parsers.base import BaseParser, ParserError, document_name_from_path, parser_uncertainty_metadata
from app.schemas.parsed import ParsedBlock, ParsedDocument, ParsedPage

try:
    import olefile
except ImportError:  # pragma: no cover - exercised only when optional dependency is missing
    olefile = None


HWP_TAG_PARA_TEXT = 67
HWP_TAG_CTRL_HEADER = 71
HWP_TAG_TABLE = 77
HWP_TABLE_CONTROL_ID = b" lbt"
HWP_LEGACY_EXTRACTION_MODE = "legacy_ole_para_text_only"
HWPML_EXTRACTION_MODE = "hwpml_xml_text_only"
DEFAULT_HWP_MAX_DECOMPRESSED_SECTION_BYTES = 256 * 1024 * 1024
DEFAULT_HWP_MAX_DECOMPRESSED_DOCUMENT_BYTES = 512 * 1024 * 1024
HWP_ARTIFACT_TOKENS = (
    "捤獥",
    "汤捯",
    "氠瑢",
    "湰灧",
    "桤灧",
    "灳瑣",
    "湯慴",
    "湯湷",
    "†普",
)
HWP_PUA_TRANSLATION = str.maketrans(
    {
        "\U000F0852": '"',
        "\U000F0853": '"',
        "\U000F0854": "'",
        "\U000F0855": "'",
    }
)
ARTICLE_RE = re.compile(r"^\s*제\s*\d+\s*조(?:의\s*\d+)?\s*(?:\([^)]*\))?")
SUPPLEMENT_HEADER_RE = re.compile(r"^\s*부\s*칙\s*(?:[<(（].{0,40}[>)）])?\s*$")
EXPLICIT_EFFECTIVE_ARTICLE_RE = re.compile(r"^\s*제\s*\d+\s*조\s*\(\s*시행일\s*\)")
APPLICATION_ARTICLE_RE = re.compile(r"^\s*제\s*\d+\s*조\s*\([^)]*적용례[^)]*\)")
ATTACHMENT_BRACKETED_HEADER_RE = re.compile(
    r"^\s*(?P<open>[\[【<])\s*"
    r"(?P<kind>별\s*표|별\s*지|서\s*식|붙\s*임)"
    r"\s*(?:제?\s*(?P<number>\d+)(?:\s*호\s*의\s*(?P<ho_suffix>\d+)|\s*(?:의|-)\s*(?P<plain_suffix>\d+))?)?"
    r"(?:\s*호\s*서식|\s*서식)?"
    r"\s*(?P<close>[\]】>])(?P<trailing>.*)$"
)
ATTACHMENT_BARE_HEADER_RE = re.compile(
    r"^\s*"
    r"(?P<kind>별\s*표|별\s*지|서\s*식|붙\s*임)"
    r"\s*(?:제?\s*(?P<number>\d+)(?:\s*호\s*의\s*(?P<ho_suffix>\d+)|\s*(?:의|-)\s*(?P<plain_suffix>\d+))?)?"
    r"(?:\s*호\s*서식|\s*서식)?"
    r"\s*(?:<[^>]+>)?\s*$"
)
CIRCLED_MARKER_RE = re.compile("[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]")
CIRCLED_LINE_MARKER_RE = re.compile(r"^\s*[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]")
NUMBERED_ITEM_RE = re.compile(r"^\s*(?:\d{1,2}(?:\.\d{1,2})*\.|\d{1,2}\))(?:\s+|(?=[^\s\d.)]))")
HANGUL_ITEM_RE = re.compile(r"^\s*[가나다라마바사아자차카타파하]\.\s+")
PARENTHESIZED_ITEM_RE = re.compile(r"^\s*\(\d+\)\s+")
NOTE_LINE_RE = re.compile(r"^\s*(?:※|\*)")
DATE_TOKEN_RE = re.compile(
    r"(?P<raw>(?P<year>(?:18|19|20|21)\d{2})\s*(?:\.|년)\s*"
    r"(?P<month>\d{1,2})\s*(?:\.|월)?\s*(?P<day>\d{1,2})\s*(?:\.|일)?)"
)


class HwpParser(BaseParser):
    supported_extensions = {".hwp"}

    def __init__(
        self,
        max_decompressed_section_bytes: int | None = None,
        max_decompressed_document_bytes: int | None = None,
    ) -> None:
        configured_limit = (
            DEFAULT_HWP_MAX_DECOMPRESSED_SECTION_BYTES
            if max_decompressed_section_bytes is None
            else int(max_decompressed_section_bytes)
        )
        if configured_limit <= 0:
            raise ValueError("max_decompressed_section_bytes must be greater than zero.")
        self.max_decompressed_section_bytes = configured_limit
        configured_document_limit = (
            DEFAULT_HWP_MAX_DECOMPRESSED_DOCUMENT_BYTES
            if max_decompressed_document_bytes is None
            else int(max_decompressed_document_bytes)
        )
        if configured_document_limit <= 0:
            raise ValueError("max_decompressed_document_bytes must be greater than zero.")
        self.max_decompressed_document_bytes = configured_document_limit

    def parse(self, path: Path, document_id: str) -> ParsedDocument:
        if self._looks_like_hwpml(path):
            return self._parse_hwpml(path, document_id)
        if olefile is None:
            raise ParserError("olefile is required to parse legacy HWP files.")
        if not olefile.isOleFile(path):
            raise ParserError("HWP file is not a valid OLE compound document.")

        blocks: list[ParsedBlock] = []
        raw_parts: list[str] = []
        table_records: list[dict] = []
        table_control_flags: Counter[str] = Counter()
        total_section_bytes = 0
        with olefile.OleFileIO(path) as ole:
            compressed = self._is_compressed(ole)
            section_names = self._bodytext_section_names(ole)
            if not section_names:
                raise ParserError("No BodyText section streams were found in the HWP file.")

            for section_index, stream_name in enumerate(section_names, start=1):
                data = ole.openstream(stream_name).read()
                if compressed:
                    data = self._decompress_section(data, stream_name)
                else:
                    self._enforce_section_size_limit(data, stream_name)
                total_section_bytes += len(data)
                self._enforce_document_size_limit(total_section_bytes)
                section_table_inventory = self._table_inventory_from_section(data, stream_name)
                table_records.extend(section_table_inventory["tables"])
                table_control_flags.update(section_table_inventory["table_control_flags"])
                for text in self._paragraph_texts(data):
                    clean = self._clean_text(text)
                    if clean and not self._looks_like_hwp_mojibake_block(clean):
                        blocks.append(
                            ParsedBlock(
                                text=clean,
                                metadata={
                                    "hwp_stream": stream_name,
                                    "section_index": section_index,
                                    "hwp_extraction_mode": HWP_LEGACY_EXTRACTION_MODE,
                                    "hwp_native_table_geometry": False,
                                },
                            )
                        )
                        raw_parts.append(clean)

        if not blocks:
            raise ParserError("No text blocks were extracted from the HWP file.")
        document_inventory = self._document_inventory(blocks, table_records, table_control_flags)

        return ParsedDocument(
            document_id=document_id,
            source_file=path.name,
            document_name=document_name_from_path(path),
            file_type="hwp",
            pages=[ParsedPage(page_no=1, blocks=blocks)],
            raw_text="\n".join(raw_parts),
            metadata={
                "hwp_encoding": "ole",
                "hwp_extraction_mode": HWP_LEGACY_EXTRACTION_MODE,
                "hwp_native_table_geometry": False,
                "hwp_native_table_inventory": True,
                "document_inventory": document_inventory,
                "hwp_inventory": document_inventory,
                **parser_uncertainty_metadata(
                    source="hwp",
                    risk_level="medium",
                    flags=[HWP_LEGACY_EXTRACTION_MODE, "native_table_geometry_unavailable"],
                    confidence=0.72,
                    recommendation="review_tables_and_appendices",
                    remediation_hint="Legacy HWP text extraction lacks native table geometry; review tables, forms, appendices, and effective-date sections before approval.",
                ),
            },
        )

    def _looks_like_hwpml(self, path: Path) -> bool:
        with path.open("rb") as handle:
            prefix = handle.read(256).lstrip()
        return prefix.startswith(b"<?xml") and b"<HWPML" in prefix

    def _parse_hwpml(self, path: Path, document_id: str) -> ParsedDocument:
        raw_xml = path.read_text(encoding="utf-8-sig", errors="replace")
        raw_xml = raw_xml.replace("&nbsp;", "&#160;")
        try:
            root = ElementTree.fromstring(raw_xml)
        except ElementTree.ParseError as exc:
            raise ParserError(f"HWPML file is not valid XML: {exc}") from exc

        document_title = self._first_xml_text(root, "TITLE") or document_name_from_path(path)
        blocks: list[ParsedBlock] = []
        raw_parts: list[str] = []
        for index, text in enumerate(self._hwpml_paragraph_texts(root), start=1):
            clean = self._clean_text(text)
            if not clean or self._looks_like_hwp_mojibake_block(clean):
                continue
            blocks.append(
                ParsedBlock(
                    text=clean,
                    metadata={
                        "hwpml_paragraph_index": index,
                        "hwp_extraction_mode": HWPML_EXTRACTION_MODE,
                        "hwp_native_table_geometry": False,
                    },
                )
            )
            raw_parts.append(clean)

        if not blocks:
            raise ParserError("No text blocks were extracted from the HWPML file.")
        document_inventory = self._document_inventory(blocks, [], Counter())

        return ParsedDocument(
            document_id=document_id,
            source_file=path.name,
            document_name=document_title,
            file_type="hwp",
            pages=[ParsedPage(page_no=1, blocks=blocks)],
            raw_text="\n".join(raw_parts),
            metadata={
                "hwp_encoding": "hwpml",
                "hwp_extraction_mode": HWPML_EXTRACTION_MODE,
                "hwp_native_table_geometry": False,
                "hwp_native_table_inventory": False,
                "document_inventory": document_inventory,
                "hwp_inventory": document_inventory,
                **parser_uncertainty_metadata(
                    source="hwp",
                    risk_level="medium",
                    flags=[HWPML_EXTRACTION_MODE, "native_table_geometry_unavailable"],
                    confidence=0.82,
                    recommendation="review_tables_and_appendices",
                    remediation_hint="HWPML text extraction is structure-light; review tables, forms, appendices, and effective-date sections before approval.",
                ),
            },
        )

    def _first_xml_text(self, root: ElementTree.Element, local_name: str) -> str | None:
        for element in root.iter():
            tag = element.tag.rsplit("}", 1)[-1]
            if tag == local_name:
                text = "".join(element.itertext()).strip()
                if text:
                    return text
        return None

    def _hwpml_paragraph_texts(self, root: ElementTree.Element) -> list[str]:
        texts: list[str] = []
        for element in root.iter():
            tag = element.tag.rsplit("}", 1)[-1]
            if tag != "P":
                continue
            text = "".join(element.itertext())
            if text.strip():
                texts.append(text)
        return texts

    def _is_compressed(self, ole) -> bool:
        if not ole.exists("FileHeader"):
            return True
        header = ole.openstream("FileHeader").read()
        if len(header) < 40:
            return True
        flags = int.from_bytes(header[36:40], byteorder="little", signed=False)
        return bool(flags & 0x01)

    def _bodytext_section_names(self, ole) -> list[str]:
        names = []
        for parts in ole.listdir(streams=True, storages=False):
            name = "/".join(parts)
            if name.lower().startswith("bodytext/section"):
                names.append(name)
        return sorted(names, key=self._section_sort_key)

    def _section_sort_key(self, name: str) -> tuple[int, str]:
        match = re.search(r"section(\d+)$", name, flags=re.IGNORECASE)
        return (int(match.group(1)) if match else 0, name.lower())

    def _decompress_section(self, data: bytes, stream_name: str) -> bytes:
        for window_bits in (-15, zlib.MAX_WBITS):
            try:
                decompressor = zlib.decompressobj(window_bits)
                section_data = decompressor.decompress(
                    data,
                    self.max_decompressed_section_bytes + 1,
                )
                if (
                    len(section_data) > self.max_decompressed_section_bytes
                    or decompressor.unconsumed_tail
                ):
                    self._raise_section_size_limit(stream_name)
                section_data += decompressor.flush()
                self._enforce_section_size_limit(section_data, stream_name)
                if not decompressor.eof:
                    raise zlib.error("incomplete or truncated compressed stream")
                return section_data
            except ParserError:
                raise
            except zlib.error:
                continue
        raise ParserError(f"Failed to decompress HWP section stream: {stream_name}")

    def _enforce_section_size_limit(self, data: bytes, stream_name: str) -> None:
        if len(data) > self.max_decompressed_section_bytes:
            self._raise_section_size_limit(stream_name)

    def _raise_section_size_limit(self, stream_name: str) -> None:
        raise ParserError(
            "HWP section stream exceeds the configured decompressed size limit "
            f"({self.max_decompressed_section_bytes} bytes): {stream_name}"
        )

    def _enforce_document_size_limit(self, total_section_bytes: int) -> None:
        if total_section_bytes > self.max_decompressed_document_bytes:
            raise ParserError(
                "HWP document sections exceed the configured total decompressed size limit "
                f"({self.max_decompressed_document_bytes} bytes)."
            )

    def _paragraph_texts(self, section_data: bytes) -> list[str]:
        texts: list[str] = []
        for tag_id, payload in self._records(section_data):
            if tag_id != HWP_TAG_PARA_TEXT:
                continue
            try:
                text = payload.decode("utf-16le", errors="ignore")
            except UnicodeDecodeError:
                continue
            if text.strip():
                texts.append(text)
        return texts

    def _table_inventory_from_section(self, section_data: bytes, stream_name: str) -> dict:
        table_controls: list[dict] = []
        tables: list[dict] = []
        for record_index, (tag_id, level, payload) in enumerate(self._record_infos(section_data), start=1):
            if tag_id == HWP_TAG_CTRL_HEADER and self._is_table_control_payload(payload):
                raw_flags = int.from_bytes(payload[4:8], byteorder="little", signed=False) if len(payload) >= 8 else 0
                table_controls.append(
                    {
                        "section": stream_name,
                        "record_index": record_index,
                        "record_level": level,
                        "raw_flags": f"0x{raw_flags:08X}",
                    }
                )
            elif tag_id == HWP_TAG_TABLE:
                control = table_controls[len(tables)] if len(tables) < len(table_controls) else {}
                raw_flags = control.get("raw_flags")
                tables.append(
                    {
                        "table_id": f"{stream_name}:table:{len(tables) + 1}",
                        "section": stream_name,
                        "record_index": record_index,
                        "record_level": level,
                        "raw_flags": raw_flags,
                        "nested": level > 2,
                    }
                )
        return {
            "tables": tables,
            "table_control_flags": Counter(table["raw_flags"] for table in tables if table.get("raw_flags")),
        }

    def _is_table_control_payload(self, payload: bytes) -> bool:
        return len(payload) >= 4 and payload[:4] == HWP_TABLE_CONTROL_ID

    def _document_inventory(
        self,
        blocks: list[ParsedBlock],
        table_records: list[dict],
        table_control_flags: Counter[str],
    ) -> dict:
        texts = [block.text.strip() for block in blocks if block.text and block.text.strip()]
        main_start = self._first_index(texts, ARTICLE_RE)
        first_supplement = self._first_index(texts, SUPPLEMENT_HEADER_RE, start=main_start or 0)
        first_attachment = self._first_attachment_index(texts, start=main_start or 0)
        main_end_candidates = [value for value in (first_supplement, first_attachment) if value is not None]
        main_end = min(main_end_candidates) if main_end_candidates else len(texts)
        main_texts = texts[main_start:main_end] if main_start is not None else []

        attachments = self._attachment_inventory(texts)
        supplements = self._supplement_inventory(texts, first_supplement, first_attachment)
        footnote_captions = self._footnote_caption_inventory(texts, attachments)
        tables = {
            "total": len(table_records),
            "top_level": sum(1 for table in table_records if not table.get("nested")),
            "nested": sum(1 for table in table_records if table.get("nested")),
            "raw_flag_counts": dict(sorted(table_control_flags.items())),
            "tables": table_records[:200],
        }
        preamble_end = main_start if main_start is not None else 0
        warnings = self._date_warnings("\n".join(texts[:preamble_end]), 0)
        warnings.extend(supplements.pop("warnings", []))
        warnings.extend(
            self._boundary_warnings(
                texts=texts,
                main_start=main_start,
                first_supplement=first_supplement,
                attachments=attachments,
            )
        )
        return {
            "schema_version": "reg-rag-document-inventory-v1",
            "source": "hwp",
            "hierarchy": self._hierarchy_inventory(main_texts),
            "attachments": attachments,
            "tables": tables,
            "supplements": supplements,
            "footnotes": footnote_captions["footnotes"],
            "endnotes": footnote_captions["endnotes"],
            "captions": footnote_captions["captions"],
            "attachment_caption_count": footnote_captions["attachment_caption_count"],
            "note_line_count": footnote_captions["note_line_count"],
            "footnote_caption_definition": footnote_captions["definition"],
            "warnings": warnings,
        }

    def _hierarchy_inventory(self, main_texts: list[str]) -> dict:
        main_lines = list(self._iter_text_lines(main_texts))
        return {
            "articles": sum(1 for text in main_texts if ARTICLE_RE.match(text)),
            "paragraphs": sum(1 for line in main_lines if CIRCLED_LINE_MARKER_RE.match(line)),
            "numbered_items": sum(1 for line in main_lines if NUMBERED_ITEM_RE.match(line)),
            "hangul_items": sum(1 for line in main_lines if HANGUL_ITEM_RE.match(line)),
            "parenthesized_items": sum(1 for line in main_lines if PARENTHESIZED_ITEM_RE.match(line)),
        }

    def _iter_text_lines(self, texts: Iterable[str]) -> Iterable[str]:
        for text in texts:
            for line in str(text or "").splitlines() or [str(text or "")]:
                yield line

    def _attachment_inventory(self, texts: list[str]) -> dict:
        attachments: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for index, text in enumerate(texts, start=1):
            header = self._attachment_header(text)
            if not header:
                continue
            kind = header["kind"]
            number = header["number"]
            key = (kind, number)
            if key in seen:
                continue
            seen.add(key)
            attachments.append(
                {
                    "kind": kind,
                    "number": number or None,
                    "deleted": header["deleted"],
                    "text": text,
                    "block_index": index,
                }
            )
        annexes = sum(1 for item in attachments if item["kind"] == "별표")
        forms = sum(1 for item in attachments if item["kind"] in {"별지", "서식"})
        sheets = sum(1 for item in attachments if item["kind"] == "붙임")
        deleted_count = sum(1 for item in attachments if item.get("deleted"))
        return {
            "annexes": annexes,
            "forms": forms,
            "sheets": sheets,
            "total": annexes + forms + sheets,
            "deleted_count": deleted_count,
            "attachments": attachments,
        }

    def _attachment_header(self, text: str) -> dict[str, object] | None:
        stripped = str(text or "").strip()
        bracketed = ATTACHMENT_BRACKETED_HEADER_RE.match(stripped)
        match = bracketed or ATTACHMENT_BARE_HEADER_RE.match(stripped)
        if not match:
            return None
        trailing = str(match.groupdict().get("trailing") or "")
        if bracketed and self._looks_like_attachment_reference_trailing(trailing):
            return None
        kind = re.sub(r"\s+", "", match.group("kind") or "")
        number = re.sub(r"\s+", "", match.group("number") or "")
        suffix = re.sub(r"\s+", "", (match.groupdict().get("ho_suffix") or match.groupdict().get("plain_suffix") or ""))
        if number and suffix:
            number = f"{number}의{suffix}"
        return {
            "kind": kind,
            "number": number,
            "deleted": bool(re.search(r"삭제\s*\d{4}", stripped)),
        }

    def _looks_like_attachment_reference_trailing(self, trailing: str) -> bool:
        if not trailing:
            return False
        first = trailing[0]
        return not first.isspace() and first in {"의", "을", "를", "에", "은", "는", "도", "만", "로", "과", "와"}

    def _first_attachment_index(self, texts: list[str], start: int = 0) -> int | None:
        for index in range(max(start, 0), len(texts)):
            if self._attachment_header(texts[index]):
                return index
        return None

    def _supplement_inventory(
        self,
        texts: list[str],
        first_supplement: int | None,
        first_attachment: int | None,
    ) -> dict:
        if first_supplement is None:
            return {
                "blocks": 0,
                "blocks_with_effective_date": 0,
                "explicit_effective_articles": 0,
                "direct_effective_clauses": 0,
                "application_clauses": 0,
                "warnings": [],
                "block_refs": [],
            }
        end = first_attachment if first_attachment is not None and first_attachment > first_supplement else len(texts)
        supplement_texts = texts[first_supplement:end]
        block_ranges: list[tuple[int, int]] = []
        starts = [index for index, text in enumerate(supplement_texts) if SUPPLEMENT_HEADER_RE.match(text)]
        for position, start in enumerate(starts):
            stop = starts[position + 1] if position + 1 < len(starts) else len(supplement_texts)
            block_ranges.append((start, stop))

        blocks_with_effective_date = 0
        block_refs: list[dict] = []
        warnings: list[dict] = []
        for block_number, (start, stop) in enumerate(block_ranges, start=1):
            block_lines = supplement_texts[start:stop]
            block_text = "\n".join(block_lines)
            has_effective = "시행" in block_text
            if has_effective:
                blocks_with_effective_date += 1
            warnings.extend(self._date_warnings("\n".join(block_lines[1:]), block_number))
            block_refs.append(
                {
                    "block_number": block_number,
                    "source_block_start": first_supplement + start + 1,
                    "source_block_end": first_supplement + stop,
                    "has_effective_date": has_effective,
                    "header": block_lines[0] if block_lines else "",
                    "identifier_date": self._identifier_date(block_lines[0] if block_lines else ""),
                }
            )

        explicit_effective_articles = sum(
            1 for text in supplement_texts if EXPLICIT_EFFECTIVE_ARTICLE_RE.match(text)
        )
        application_clauses = sum(1 for text in supplement_texts if APPLICATION_ARTICLE_RE.match(text))
        return {
            "blocks": len(block_ranges),
            "blocks_with_effective_date": blocks_with_effective_date,
            "explicit_effective_articles": explicit_effective_articles,
            "direct_effective_clauses": max(blocks_with_effective_date - explicit_effective_articles, 0),
            "application_clauses": application_clauses,
            "warnings": warnings,
            "block_refs": block_refs[:200],
        }

    def _identifier_date(self, text: str) -> dict[str, object] | None:
        match = DATE_TOKEN_RE.search(str(text or ""))
        if not match:
            return None
        year = int(match.group("year"))
        month = int(match.group("month"))
        day = int(match.group("day"))
        normalized: str | None
        try:
            date(year, month, day)
        except ValueError:
            normalized = None
        else:
            normalized = f"{year:04d}-{month:02d}-{day:02d}"
        return {"raw": match.group("raw"), "normalized": normalized}

    def _footnote_caption_inventory(self, texts: list[str], attachments: dict) -> dict[str, object]:
        attachment_items = attachments.get("attachments") if isinstance(attachments.get("attachments"), list) else []
        attachment_caption_count = sum(
            1 for item in attachment_items if isinstance(item, dict) and item.get("kind") in {"별표", "별지", "서식"}
        )
        note_line_count = 0
        for line in self._iter_text_lines(texts):
            if NOTE_LINE_RE.match(line):
                note_line_count += 1
        return {
            "footnotes": 0,
            "endnotes": 0,
            "captions": attachment_caption_count + note_line_count,
            "attachment_caption_count": attachment_caption_count,
            "note_line_count": note_line_count,
            "definition": "HWP footnote/caption count is native footnotes/endnotes plus independent appendix/form headers and lines starting with ※ or *.",
        }

    def _boundary_warnings(
        self,
        *,
        texts: list[str],
        main_start: int | None,
        first_supplement: int | None,
        attachments: dict,
    ) -> list[dict]:
        body_texts = texts[main_start or 0 :]
        warnings: list[dict] = []
        if first_supplement is None and any(re.match(r"^\s*부\s*칙\b", text) for text in body_texts):
            warnings.append(
                {
                    "warning": "supplement_boundary_not_detected",
                    "detail": "Supplementary provision token exists, but no supplementary boundary was detected.",
                }
            )
        if not int(attachments.get("total") or 0) and any(
            re.match(r"^\s*[\[【<]?\s*(?:별\s*표|별\s*지|서\s*식)", text) for text in body_texts
        ):
            warnings.append(
                {
                    "warning": "attachment_boundary_not_detected",
                    "detail": "Attachment token exists, but no appendix/form boundary was detected.",
                }
            )
        return warnings

    def _date_warnings(self, text: str, block_number: int) -> list[dict]:
        warnings: list[dict] = []
        for match in DATE_TOKEN_RE.finditer(text):
            raw = match.group("raw")
            year = int(match.group("year"))
            month = int(match.group("month"))
            day = int(match.group("day"))
            compact = re.sub(r"\s+", "", raw)
            if re.fullmatch(r"(?:18|19|20|21)\d{2}\.\d{4}\.?", compact):
                warnings.append(
                    {
                        "raw": raw,
                        "normalized": f"{year:04d}-{month:02d}-{day:02d}",
                        "warning": "compact_month_day_separator",
                        "supplement_block_number": block_number,
                    }
                )
            try:
                date(year, month, day)
            except ValueError:
                warnings.append(
                    {
                        "raw": raw,
                        "normalized": None,
                        "warning": "invalid_date",
                        "supplement_block_number": block_number,
                    }
                )
        return warnings

    def _first_index(self, texts: list[str], pattern: re.Pattern[str], start: int = 0) -> int | None:
        for index in range(max(start, 0), len(texts)):
            if pattern.match(texts[index]):
                return index
        return None

    def _records(self, section_data: bytes) -> Iterable[tuple[int, bytes]]:
        for tag_id, _level, payload in self._record_infos(section_data):
            yield tag_id, payload

    def _record_infos(self, section_data: bytes) -> Iterable[tuple[int, int, bytes]]:
        offset = 0
        length = len(section_data)
        while offset + 4 <= length:
            header = int.from_bytes(section_data[offset : offset + 4], byteorder="little", signed=False)
            offset += 4
            tag_id = header & 0x3FF
            level = (header >> 10) & 0x3FF
            size = (header >> 20) & 0xFFF
            if size == 0xFFF:
                if offset + 4 > length:
                    break
                size = int.from_bytes(section_data[offset : offset + 4], byteorder="little", signed=False)
                offset += 4
            if offset + size > length:
                break
            yield tag_id, level, section_data[offset : offset + size]
            offset += size

    def _clean_text(self, text: str) -> str:
        text = unicodedata.normalize("NFC", text).translate(HWP_PUA_TRANSLATION)
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
        text = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]", " ", text)
        text = re.sub(r"[ \u3000]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text.strip()
        text = re.sub(r"^[\u3400-\u9fff]{1,4}(?:\s+[\u3400-\u9fff]{1,4}){1,5}\s+(?=[가-힣])", "", text)
        text = self._strip_standalone_mojibake_lines(text)
        text = self._strip_known_hwp_artifact_tokens(text)
        return text.strip()

    def _strip_standalone_mojibake_lines(self, text: str) -> str:
        lines = text.splitlines()
        if not any(self._looks_like_hwp_mojibake_line(line) for line in lines):
            return text
        has_meaningful_line = any(
            re.search(r"[가-힣A-Za-z0-9]", line) and not self._looks_like_hwp_mojibake_line(line) for line in lines
        )
        if not has_meaningful_line:
            return text
        return "\n".join(line for line in lines if not self._looks_like_hwp_mojibake_line(line))

    def _looks_like_hwp_mojibake_line(self, line: str) -> bool:
        compact = re.sub(r"\s+", "", line)
        if not compact or len(compact) > 16:
            return False
        if re.search(r"[가-힣A-Za-z0-9]", compact):
            return False
        known_markers = set("捤獥汤捯氠瑢桤灧灳湯湷")
        return any(char in known_markers for char in compact) and bool(re.fullmatch(r"[\u3400-\u9fff]+", compact))

    def _looks_like_hwp_mojibake_block(self, text: str) -> bool:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return bool(lines) and all(self._looks_like_hwp_mojibake_line(line) for line in lines)

    def _strip_known_hwp_artifact_tokens(self, text: str) -> str:
        for token in HWP_ARTIFACT_TOKENS:
            text = text.replace(token, " ")
        text = re.sub(r"[ \u3000]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        return text.strip()
