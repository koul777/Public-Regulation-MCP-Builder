from __future__ import annotations

import re
from dataclasses import dataclass

from app.schemas.parsed import ParsedBlock, ParsedDocument, ParsedPage
from app.schemas.structure import StructureNode


PATTERNS = {
    "part": re.compile(r"^\s*(제\s*\d+\s*편)\s+(.+)$"),
    "chapter": re.compile(r"^\s*(제\s*\d+\s*장)\s+(.+)$"),
    "section": re.compile(r"^\s*(제\s*\d+\s*절)\s+(.+)$"),
    "subsection": re.compile(r"^\s*(제\s*\d+\s*관)\s+(.+)$"),
    "regulation": re.compile(r"^\s*(\d+-\d+-\d+)\.\s+(.+)$"),
    "article": re.compile(r"^\s*(제\s*\d+\s*조(?:의\s*\d+)?)(?=\s*(?:\(|<|삭제|$|\s))\s*(?:\(([^)]+)\))?\s*(.*)$"),
    "paragraph_symbol": re.compile(r"^\s*([①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚])\s*(.*)$"),
    "paragraph_je_hang": re.compile(r"^\s*(제\s*\d+\s*항)(?=\s|$)\s*(.*)$"),
    "paragraph_number": re.compile(r"^\s*(\(\d+\))\s*(.*)$"),
    "paragraph_square": re.compile(r"^\s*(□)\s+(.+)$"),
    "item_decimal_compact": re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2})+\.)(?=[^\s\d.])(.+)$"),
    "item_decimal": re.compile(r"^\s*(\d+(?:\.\d+)+\.)\s+(.+)$"),
    "item_number_compact": re.compile(r"^\s*(\d{1,2}\.)(?=[^\s\d.])(.+)$"),
    "item_number": re.compile(r"^\s*(\d+\.)\s+(.+)$"),
    "item_number_paren_compact": re.compile(r"^\s*(\d{1,2}\))(?=[^\s\d)])(.+)$"),
    "item_number_paren": re.compile(r"^\s*(\d+\))\s+(.+)$"),
    "item_je_ho": re.compile(r"^\s*(제\s*\d+\s*호)(?=\s|$)\s*(.*)$"),
    "subitem_korean": re.compile(r"^\s*([가나다라마바사아자차카타파하][\.\)])\s*(.+)$"),
    "subitem_hangul_paren": re.compile(r"^\s*(\([가나다라마바사아자차카타파하]\))\s*(.+)$"),
    "appendix": re.compile(r"^\s*[\[【<]?\s*(별\s*표\s*(?:\d+(?:\s*(?:의|-)\s*\d+)?)?)\s*[\]】>]?\s*(.*)$"),
    "form": re.compile(r"^\s*[\[【<]?\s*(별\s*지\s*제?\s*(?:\d+(?:\s*(?:의|-)\s*\d+)?)?\s*호?\s*서식)\s*[\]】>]?\s*(.*)$"),
    "supplementary": re.compile(r"^\s*(부\s*칙)\s*(.*)$"),
}

ATTACHMENT_REF_PATTERNS = {
    "appendix": re.compile(r"별\s*표\s*(?:제\s*)?(?:\d+(?:\s*(?:의|-)\s*\d+)?)?(?:\s*호)?"),
    "form": re.compile(r"별\s*지\s*제?\s*(?:\d+(?:\s*(?:의|-)\s*\d+)?)?\s*호?\s*서식"),
}

INLINE_STRUCTURE_MARKER_PATTERN = re.compile(
    r"(?<!\S)(?:"
    r"[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚]"
    r"|□(?=\s)"
    r"|\(\d{1,2}\)(?=\s)"
    r"|\d{1,2}\)(?=\s)"
    r"|\d{1,2}(?:\.\d{1,2})*\.(?=\s)"
    r"|\d{1,2}(?:\.\d{1,2})+\.(?=[^\s\d.])"
    r"|\d{1,2}\)(?=[^\s\d)])"
    r"|\d{1,2}\.(?=[^\s\d.])"
    r"|\([가나다라마바사아자차카타파하]\)(?=\s)"
    r"|[가나다라마바사아자차카타파하][\.\)](?=\s)"
    r"|제\s*\d+\s*항(?=\s|$|[\(<【\[:.])"
    r"|제\s*\d+\s*호(?!\s*(?:서식|전문개정|일부개정|개정|시행|삭제|신설|변경))(?=\s|$|[\(<【\[:.])"
    r")"
)
INLINE_ARTICLE_MARKER_PATTERN = re.compile(
    r"(?<!\S)제\s*\d+\s*조(?:의\s*\d+)?(?=\s*\([^)]{1,80}\))"
)

FOOTNOTE_CAPTION_MARKER_PATTERN = re.compile(
    r"^\s*(?:(?:[\[【<〈(]\s*)?(?:표|그림)\s*\d+\s*(?:[\.\-\):：\]】>〉)]|$)|(?:각주|미주|캡션|caption)\b)"
)

HWPX_SOURCE_COUNT_METADATA_KEYS = (
    "hwpx_image_caption_count",
    "hwpx_table_row_count",
    "hwpx_table_cell_count",
    "hwpx_table_caption_count",
    "hwpx_nested_table_count",
    "hwpx_table_image_count",
    "hwpx_table_note_count",
    "hwpx_merged_cell_count",
)

HWPX_SOURCE_LIST_METADATA_KEYS = (
    "hwpx_table_direct_captions",
    "hwpx_table_image_captions",
    "hwpx_table_note_snippets",
    "hwpx_nested_table_text_snippets",
)


@dataclass
class SourceLine:
    text: str
    page_no: int | None
    block_type: str = "text"
    metadata: dict | None = None


class StructureDetector:
    def detect(self, parsed: ParsedDocument) -> list[StructureNode]:
        lines = self._extract_lines(parsed)
        nodes: list[StructureNode] = []
        pending_orphan_lines: list[SourceLine] = []
        current: dict[str, StructureNode | None] = {
            "part": None,
            "chapter": None,
            "section": None,
            "subsection": None,
            "regulation": None,
            "article": None,
            "paragraph": None,
            "item": None,
            "subitem": None,
            "supplementary": None,
            "regulation_parent": None,
            "last_article": None,
        }
        seen_regulation_keys: set[tuple[str | None, str]] = set()

        for line in lines:
            detected = self._detect_line(line, parsed.document_id, len(nodes))
            if detected is None:
                if not self._append_to_current(current, line):
                    pending_orphan_lines.append(line)
                continue
            if self._should_demote_unanchored_numbered_item(current, detected, pending_orphan_lines):
                if not self._append_to_current(current, line):
                    pending_orphan_lines.append(line)
                continue
            if pending_orphan_lines:
                nodes.append(self._orphan_paragraph_node(parsed.document_id, pending_orphan_lines, len(nodes)))
                pending_orphan_lines = []
                detected = self._reindex_node(detected, len(nodes))
            if self._should_skip_repeated_regulation_header(current, detected, seen_regulation_keys):
                continue
            if self._should_keep_inside_current_container(current, detected):
                self._append_to_current(current, line)
                continue
            self._close_appendix_or_form_container(current, detected)

            node_type = detected.node_type
            detected.parent_id = self._parent_id_for(node_type, current)
            nodes.append(detected)
            if node_type == "regulation":
                seen_regulation_keys.add(self._regulation_identity(detected))
            self._update_current(current, detected)

        if pending_orphan_lines and nodes:
            nodes.append(self._orphan_paragraph_node(parsed.document_id, pending_orphan_lines, len(nodes)))
        for node in nodes:
            if not (node.metadata.get("source_hwpx_block_types") and node.metadata.get("caption_count")):
                self._apply_footnote_caption_metadata(node.metadata, node.text)
            self._strip_internal_metadata(node)
        return nodes

    def detect_from_text(
        self,
        text: str,
        document_id: str = "doc_test",
        source_file: str = "sample_regulation.md",
        document_name: str = "sample_regulation",
    ) -> list[StructureNode]:
        parsed = ParsedDocument(
            document_id=document_id,
            source_file=source_file,
            document_name=document_name,
            file_type="text",
            pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text=text)])],
            raw_text=text,
        )
        return self.detect(parsed)

    def _extract_lines(self, parsed: ParsedDocument) -> list[SourceLine]:
        lines: list[SourceLine] = []
        for page in parsed.pages:
            for block_index, block in enumerate(page.blocks, start=1):
                block_metadata = dict(block.metadata)
                if block.bbox and "source_bbox" not in block_metadata:
                    block_metadata["source_bbox"] = [float(part) for part in block.bbox]
                if "source_page" not in block_metadata:
                    block_metadata["source_page"] = page.page_no
                block_metadata["_source_block_key"] = self._source_block_key(page.page_no, block_index, block_metadata)
                if block.type == "table":
                    table_text = block.text.strip()
                    if table_text:
                        table_metadata = dict(block_metadata)
                        table_metadata["attachment_references"] = self._attachment_references_in_text(table_text, page.page_no)
                        self._apply_footnote_caption_metadata(table_metadata, table_text)
                        lines.append(SourceLine(table_text, page.page_no, block.type, table_metadata))
                    continue
                for raw_line in block.text.splitlines():
                    line = raw_line.strip()
                    if line:
                        for split_line in self._split_inline_structure_lines(line):
                            line_metadata = dict(block_metadata)
                            line_metadata["attachment_references"] = self._attachment_references_in_text(
                                split_line,
                                page.page_no,
                            )
                            self._apply_footnote_caption_metadata(line_metadata, split_line)
                            lines.append(SourceLine(split_line, page.page_no, block.type, line_metadata))
        return lines

    def _source_block_key(self, page_no: int | None, block_index: int, metadata: dict) -> str:
        xml_file = metadata.get("xml_file")
        xml_block_index = metadata.get("hwpx_xml_block_index")
        if xml_file or xml_block_index:
            return f"{xml_file or ''}#{xml_block_index or block_index}"
        return f"page:{page_no or 0}:block:{block_index}"

    def _detect_line(self, line: SourceLine, document_id: str, order_index: int) -> StructureNode | None:
        text = line.text
        if line.block_type == "table":
            return self._node(document_id, "table", None, None, text, line.page_no, order_index, line.metadata)

        for node_type in ("appendix", "form", "supplementary", "part", "chapter", "section", "subsection", "regulation"):
            match = PATTERNS[node_type].match(text)
            if match:
                number = self._normalize_number(match.group(1))
                title = match.group(2).strip() if len(match.groups()) > 1 else None
                if node_type in {"appendix", "form"} and self._looks_like_appendix_form_reference(text, title or ""):
                    return None
                if node_type in {"appendix", "form"} and not self._has_attachment_start_evidence(
                    node_type,
                    text,
                    title or "",
                    line.metadata or {},
                ):
                    return None
                return self._node(document_id, node_type, number, title or None, text, line.page_no, order_index, line.metadata)

        match = PATTERNS["article"].match(text)
        if match:
            number = self._normalize_number(match.group(1))
            title = (match.group(2) or "").strip() or None
            trailing = (match.group(3) or "").strip()
            if not title:
                title = self._article_lifecycle_title(trailing)
            if not title and self._looks_like_article_reference_tail(trailing):
                return None
            node = self._node(document_id, "article", number, title, text, line.page_no, order_index, line.metadata)
            if not title:
                node.warnings.append("article_title_missing")
                node.confidence = 0.9
            if trailing.startswith("삭제") or trailing.startswith("<삭제"):
                node.metadata["lifecycle"] = "deleted"
            if trailing and title:
                node.metadata["article_lead_text"] = trailing
            return node

        for node_type, pattern_name in (
            ("paragraph", "paragraph_symbol"),
            ("paragraph", "paragraph_je_hang"),
            ("paragraph", "paragraph_number"),
            ("paragraph", "paragraph_square"),
            ("item", "item_decimal_compact"),
            ("item", "item_decimal"),
            ("item", "item_number_compact"),
            ("item", "item_number"),
            ("item", "item_number_paren_compact"),
            ("item", "item_number_paren"),
            ("item", "item_je_ho"),
            ("subitem", "subitem_korean"),
            ("subitem", "subitem_hangul_paren"),
        ):
            match = PATTERNS[pattern_name].match(text)
            if match:
                if node_type == "item" and self._looks_like_line_start_date_fragment(match.group(1), text):
                    continue
                number = self._normalize_number(match.group(1))
                trailing = match.group(2) if len(match.groups()) > 1 else ""
                title = self._paragraph_label(trailing) if node_type == "paragraph" else None
                node = self._node(document_id, node_type, number, title, text, line.page_no, order_index, line.metadata)
                if title:
                    node.metadata["paragraph_label"] = title
                return node

        return None

    def _looks_like_article_reference_tail(self, trailing: str) -> bool:
        return bool(re.match(r"^(제\s*\d+\s*(항|호)|및|내지|부터|까지|관련|중\b)", trailing.strip()))

    def _article_lifecycle_title(self, trailing: str) -> str | None:
        compact = re.sub(r"\s+", "", trailing or "").lstrip("<")
        if compact.startswith("생략"):
            return "생략"
        if compact.startswith("삭제"):
            return "삭제"
        return None

    def _should_skip_repeated_regulation_header(
        self,
        current: dict[str, StructureNode | None],
        detected: StructureNode,
        seen_regulation_keys: set[tuple[str | None, str]],
    ) -> bool:
        if detected.node_type != "regulation":
            return False
        if current.get("supplementary"):
            active = current.get("regulation")
            if active and self._regulation_identity(active) == self._regulation_identity(detected):
                return True
        if self._regulation_identity(detected) in seen_regulation_keys:
            return True
        active = current.get("regulation")
        if not active:
            return False
        container = current.get("article")
        if not container or container.node_type != "article":
            return False
        same_number = active.number == detected.number
        same_title = (active.title or "").strip() == (detected.title or "").strip()
        return bool(same_number and same_title)

    def _should_keep_inside_current_container(
        self,
        current: dict[str, StructureNode | None],
        detected: StructureNode,
    ) -> bool:
        container = current.get("article")
        if detected.node_type == "regulation" and current.get("supplementary"):
            return False
        if detected.node_type == "regulation" and container and container.node_type in {"appendix", "form"}:
            return False
        if detected.node_type == "regulation" and container and container.node_type not in {"supplementary"}:
            active = current.get("regulation")
            if active and self._regulation_identity(active) != self._regulation_identity(detected):
                return False
            return True
        if container and container.node_type in {"appendix", "form"} and detected.node_type in {
            "article",
            "paragraph",
            "item",
            "subitem",
            "regulation",
        }:
            if self._detected_clause_closes_attachment_container(container, detected):
                return False
            return True
        if self._looks_like_amended_article_quote(current, detected):
            return True
        if (
            detected.node_type == "subitem"
            and not any(
                current.get(key)
                for key in ("part", "chapter", "section", "subsection", "regulation", "supplementary")
            )
            and not current.get("item")
            and not current.get("subitem")
            and not current.get("paragraph")
            and not current.get("article")
        ):
            return True
        return False

    def _close_appendix_or_form_container(
        self,
        current: dict[str, StructureNode | None],
        detected: StructureNode,
    ) -> None:
        article = current.get("article")
        if not article or article.node_type not in {"appendix", "form"}:
            return
        boundary = detected.node_type in {"part", "chapter", "section", "subsection", "regulation", "supplementary"}
        clause_boundary = self._detected_clause_closes_attachment_container(article, detected)
        if boundary or clause_boundary:
            current["article"] = current.get("last_article") if clause_boundary else None
            self._clear(current, "paragraph", "item", "subitem")
            if clause_boundary and "attachment_container_boundary_inferred" not in detected.warnings:
                detected.warnings.append("attachment_container_boundary_inferred")

    def _looks_like_amended_article_quote(
        self,
        current: dict[str, StructureNode | None],
        detected: StructureNode,
    ) -> bool:
        if detected.node_type != "article":
            return False
        supplementary = current.get("supplementary")
        container = current.get("article")
        if not supplementary or not container or container.node_type != "article":
            return False
        container_text = f"{container.title or ''} {container.text or ''}"
        amendment_context = any(
            marker in container_text
            for marker in ["다른 규정의 개정", "관련 규정의 개정", "다른 법령의 개정", "개정한다", "다음과 같이 한다"]
        )
        if not amendment_context:
            return False
        title = detected.title or ""
        supplementary_article_title = any(
            marker in title for marker in ["시행일", "적용례", "경과", "특례", "다른 규정의 개정", "다른 법령의 개정"]
        )
        return not supplementary_article_title

    def _looks_like_appendix_form_reference(self, text: str, trailing: str) -> bool:
        compact = re.sub(r"\s+", "", text)
        if re.search(r"[\]】](?:의|을|를|은|는|과|와|중|에|에서|으로|로)", compact):
            return True
        if re.search(r"[\]】].{0,20}(?:하여야한다|작성하여|제출하여|참조하여|따른다)", compact):
            return True
        if re.match(r"^(?:의|을|를|은|는|과|와|중|에|에서|으로|로)\b", trailing.strip()):
            return True
        if re.match(r"^[-–]\s*\d+\s*(?:의|을|를|은|는|과|와|중|에|에서|으로|로)\b", trailing.strip()):
            return True
        if re.match(r"^(?:참조|참고)(?:\b|[.\s])", trailing.strip()):
            return True
        return bool(re.search(r"(?:같이|다음과|개정|삭제|신설|이동).{0,20}한다", trailing))

    def _split_inline_structure_lines(self, text: str) -> list[str]:
        if FOOTNOTE_CAPTION_MARKER_PATTERN.match(text):
            return [text]
        starts: list[int] = []
        for match in INLINE_ARTICLE_MARKER_PATTERN.finditer(text):
            if match.start() == 0:
                continue
            if self._looks_like_inline_article_reference(text, match):
                continue
            starts.append(match.start())
        for match in INLINE_STRUCTURE_MARKER_PATTERN.finditer(text):
            if match.start() == 0:
                continue
            if self._line_starts_with_paragraph_marker(text) and self._is_circled_marker(match.group(0)):
                continue
            if self._looks_like_inline_hangul_sentence_word(text, match):
                continue
            if self._looks_like_inline_date_fragment(text, match):
                continue
            if self._looks_like_inline_paragraph_item_reference(text, match):
                continue
            starts.append(match.start())
        if not starts:
            return [text]
        starts = sorted(set(starts))

        parts: list[str] = []
        start = 0
        for next_start in starts:
            part = text[start:next_start].strip()
            if part:
                parts.append(part)
            start = next_start
        tail = text[start:].strip()
        if tail:
            parts.append(tail)
        return parts or [text]

    def _looks_like_inline_article_reference(self, text: str, match: re.Match[str]) -> bool:
        before = text[: match.start()].strip()
        after = text[match.end() :]
        if re.match(r"\s*제\s*\d+\s*(?:항|호)", after):
            return True
        if re.match(r"\s*\([^)]{1,80}\)\s*(?:의|에|에서|으로|로|을|를|은|는|과|와|및|관련|따라|중)", after):
            return True
        if re.search(r"(?:따라|관련|준용|의한다|정한다|개정|삭제|신설|변경|중)\s*$", before):
            return True
        return False

    def _looks_like_inline_paragraph_item_reference(self, text: str, match: re.Match[str]) -> bool:
        """Treat a 제N항/제N호 marker as a cross-reference, not a new node.

        Genuine flattened enumeration items follow a clause end or plain prose
        ("... 같다. 제1호 본부"), whereas a citation follows another 조/항/호
        marker or a reference-list connector ("제5조 제1항", "제5호 및 제6호",
        "종전의 제6호").  Only the citation forms are suppressed here; the split
        pattern already ignores the particle form ("제1항의").
        """

        if not re.match(r"제\s*\d+\s*(?:항|호)", match.group(0)):
            return False
        before = text[: match.start()].rstrip()
        if re.search(r"제\s*\d+\s*(?:조|항|호)$", before):
            return True
        if re.search(r"(?:및|또는|내지|과|와|·|ㆍ|각각|종전의)$", before):
            return True
        if re.search(r"(?:따라|관련|준용|의한다|정한다|개정|삭제|신설|변경|중)\s*$", before):
            return True
        return False

    def _line_starts_with_paragraph_marker(self, text: str) -> bool:
        return bool(re.match(r"^\s*(?:[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚]|□|\(\d+\))", text))

    def _is_circled_marker(self, value: str) -> bool:
        return bool(re.match(r"^[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚]$", value))

    def _looks_like_inline_date_fragment(self, text: str, match: re.Match[str]) -> bool:
        marker = match.group(0)
        if not re.match(r"\d{1,2}\.$", marker):
            return False
        before = text[: match.start()]
        after = text[match.end() :]
        return bool(re.search(r"\d{1,4}\.\s*$", before) or re.match(r"\s*\d{1,2}\.", after))

    def _looks_like_inline_hangul_sentence_word(self, text: str, match: re.Match[str]) -> bool:
        marker = re.sub(r"\s+", "", match.group(0))
        if marker not in {"자.", "자)"}:
            return False
        before = text[: match.start()].rstrip()
        after = text[match.end() :].lstrip()
        return bool(
            re.search(r"(?:않은|있는|없는|아닌|해당하는|된|인)\s*$", before)
            and re.match(r"(?:다만|그러나|단(?:\s|[,，]))", after)
        )

    def _looks_like_line_start_date_fragment(self, marker: str, text: str = "") -> bool:
        compact_marker = re.sub(r"\s+", "", str(marker or ""))
        if re.fullmatch(r"\d{4}\.\d{1,2}\.\d{1,2}\.", compact_marker):
            return True
        return bool(
            re.match(
                r"^\s*(?:18|19|20|21)\d{2}\s*[.\-]\s*\d{1,2}\s*[.\-]\s*\d{1,2}(?:\s*\.|\b)",
                str(text or ""),
            )
        )

    def _should_demote_unanchored_numbered_item(
        self,
        current: dict[str, StructureNode | None],
        detected: StructureNode,
        pending_orphan_lines: list[SourceLine],
    ) -> bool:
        if detected.node_type != "item":
            return False
        if any(current.get(key) for key in ("article", "paragraph", "item", "subitem")):
            return False
        text = re.sub(r"\s+", " ", str(detected.text or "")).strip()
        if re.match(
            r"^\d{1,3}[\.)]\s*"
            r"(?:규정|내규|정관|세칙|기준|요령|지침|편람|규칙)\s*제\s*\d+\s*호\s*"
            r"(?:일부|전부|전문)?(?:개정|제정|폐지)(?:$|\s|[<\(])",
            text,
        ):
            return True
        if re.match(r"^\d{1,3}[\.)]\s+", text) and re.search(
            r"(?:전문개정|일부개정|개정|시행|삭제|신설|변경)",
            text,
        ) and re.search(r"\d{4}\s*[.\-]\s*\d{1,2}\s*[.\-]\s*\d{1,2}", text):
            return True
        if re.match(r"^\d{1,2}\.\s+.{1,80}\.{3,}\s*\d{1,4}$", text):
            return True
        return bool(
            re.match(r"^\d{1,2}\.\s+.{1,80}\s+\d{1,4}$", text)
            and any(re.match(r"^\s*(?:목\s*차|차\s*례|contents)\s*$", line.text, re.IGNORECASE) for line in pending_orphan_lines[-3:])
        )

    def _footnote_caption_metadata(self, text: str) -> dict:
        count = 0
        for raw_line in str(text or "").splitlines() or [str(text or "")]:
            line = raw_line.strip()
            if not line or line.startswith("|"):
                continue
            if FOOTNOTE_CAPTION_MARKER_PATTERN.match(line):
                count += 1
        if not count:
            return {}
        return {"caption_count": count, "caption_parent": "line_note"}

    def _apply_footnote_caption_metadata(self, metadata: dict, text: str) -> None:
        derived = self._footnote_caption_metadata(text)
        if not derived:
            return
        for key, value in derived.items():
            if key == "caption_count":
                metadata[key] = max(int(metadata.get(key) or 0), int(value or 0))
            else:
                metadata.setdefault(key, value)

    def _has_attachment_start_evidence(
        self,
        node_type: str,
        text: str,
        trailing: str,
        metadata: dict,
    ) -> bool:
        if not metadata.get("pdf_layout"):
            return True
        bbox = metadata.get("source_bbox") or []
        page_width = float(metadata.get("page_width") or 0)
        page_height = float(metadata.get("page_height") or 0)
        if not isinstance(bbox, (list, tuple)) or len(bbox) < 4 or page_width <= 0 or page_height <= 0:
            return False
        x0 = float(bbox[0])
        y0 = float(bbox[1])
        compact = re.sub(r"\s+", "", text)
        bracketed = compact.startswith(("[별표", "【별표", "<별표", "[별지", "【별지", "<별지"))
        top_header = y0 <= page_height * 0.17 and x0 <= page_width * 0.16
        near_top_header = y0 <= page_height * 0.12 and x0 <= page_width * 0.25
        prose_tail = bool(re.search(r"(하여야\s*한다|제출하여야|작성하여|참조하여|따른다)", trailing))
        label_only_or_header = bracketed or len(compact) <= 90
        if prose_tail and not top_header:
            return False
        return bool(label_only_or_header and (top_header or near_top_header))

    def _attachment_references_in_text(self, text: str, page_no: int | None) -> list[dict]:
        refs: list[dict] = []
        for ref_type, pattern in ATTACHMENT_REF_PATTERNS.items():
            for match in pattern.finditer(text):
                label = self._normalize_attachment_label(match.group(0))
                if not label:
                    continue
                item = {"type": "form" if ref_type == "form" else "appendix", "label": label, "source_page": page_no}
                if item not in refs:
                    refs.append(item)
        return refs

    def _detected_clause_closes_attachment_container(
        self,
        container: StructureNode,
        detected: StructureNode,
    ) -> bool:
        if container.node_type not in {"appendix", "form"}:
            return False
        if detected.node_type not in {"paragraph", "item", "subitem"}:
            return False
        container_label = self._normalize_attachment_label(container.number)
        if not container_label:
            return False
        refs = self._attachment_refs_in_text(detected.text, container.node_type)
        if not refs:
            return False
        return any(ref != container_label and re.search(r"\d", ref) for ref in refs)

    def _attachment_refs_in_text(self, text: str, container_type: str) -> list[str]:
        pattern = ATTACHMENT_REF_PATTERNS["form"] if container_type == "form" else ATTACHMENT_REF_PATTERNS["appendix"]
        refs: list[str] = []
        for match in pattern.finditer(text):
            label = self._normalize_attachment_label(match.group(0))
            if label and label not in refs:
                refs.append(label)
        return refs

    def _normalize_attachment_label(self, value: str | None) -> str:
        cleaned = re.sub(r"[\[\]【】<>〈〉]", "", str(value or ""))
        return re.sub(r"\s+", "", cleaned.strip())

    def _paragraph_label(self, trailing: str) -> str | None:
        match = re.match(r"^\s*[\(（]\s*([^()（）]{1,40}?)\s*[\)）]", trailing or "")
        if not match:
            return None
        label = re.sub(r"\s+", " ", match.group(1)).strip()
        return label or None

    def _regulation_identity(self, node: StructureNode) -> tuple[str | None, str]:
        return node.number, re.sub(r"\s+", "", node.title or "")

    def _node(
        self,
        document_id: str,
        node_type: str,
        number: str | None,
        title: str | None,
        text: str,
        page_no: int | None,
        order_index: int,
        source_metadata: dict | None = None,
    ) -> StructureNode:
        safe_type = node_type.replace(" ", "_")
        node = StructureNode(
            node_id=f"{document_id}_{safe_type}_{order_index + 1:04d}",
            document_id=document_id,
            node_type=node_type,  # type: ignore[arg-type]
            number=number,
            title=title,
            text=text,
            page_start=page_no,
            page_end=page_no,
            order_index=order_index,
        )
        self._merge_source_metadata(node, source_metadata or {})
        return node

    def _append_to_current(self, current: dict[str, StructureNode | None], line: SourceLine) -> bool:
        target = self._append_target(current)
        if target is None:
            return False
        target.text = f"{target.text}\n{line.text}"
        if line.page_no is not None:
            target.page_end = max(target.page_end or line.page_no, line.page_no)
        self._merge_source_metadata(target, line.metadata or {})
        return True

    def _append_target(self, current: dict[str, StructureNode | None]) -> StructureNode | None:
        for key in ("subitem", "item", "paragraph", "article", "supplementary"):
            if current.get(key):
                return current[key]
        if current.get("regulation"):
            for key in ("subsection", "section", "chapter"):
                node = current.get(key)
                if node and self._is_within_active_regulation(current, node):
                    return node
            return current["regulation"]
        for key in ("subsection", "section", "chapter", "part"):
            if current.get(key):
                return current[key]
        return None

    def _orphan_paragraph_node(
        self,
        document_id: str,
        lines: list[SourceLine],
        order_index: int,
    ) -> StructureNode:
        text = "\n".join(line.text for line in lines if line.text.strip())
        page_values = [line.page_no for line in lines if line.page_no is not None]
        node = self._node(
            document_id,
            "paragraph",
            "preamble",
            "Preamble",
            text,
            page_values[0] if page_values else None,
            order_index,
        )
        node.page_end = page_values[-1] if page_values else node.page_end
        node.confidence = 0.85
        node.warnings.append("orphan_preamble_text")
        for line in lines:
            self._merge_source_metadata(node, line.metadata or {})
        return node

    def _merge_source_metadata(self, node: StructureNode, source_metadata: dict) -> None:
        if not source_metadata:
            return
        hwpx_block_type = source_metadata.get("hwpx_block_type")
        if hwpx_block_type:
            values = list(node.metadata.get("source_hwpx_block_types") or [])
            if hwpx_block_type not in values:
                values.append(hwpx_block_type)
            node.metadata["source_hwpx_block_types"] = values
            if "hwpx_block_type" not in node.metadata:
                node.metadata["hwpx_block_type"] = hwpx_block_type
        xml_file = source_metadata.get("xml_file")
        if xml_file:
            xml_files = list(node.metadata.get("source_xml_files") or [])
            if xml_file not in xml_files:
                xml_files.append(xml_file)
            node.metadata["source_xml_files"] = xml_files
        xml_block_index = source_metadata.get("hwpx_xml_block_index")
        if isinstance(xml_block_index, int):
            xml_block_indices = list(node.metadata.get("source_hwpx_xml_block_indices") or [])
            if xml_block_index not in xml_block_indices:
                xml_block_indices.append(xml_block_index)
            node.metadata["source_hwpx_xml_block_indices"] = xml_block_indices
        hwp_extraction_mode = source_metadata.get("hwp_extraction_mode")
        if hwp_extraction_mode:
            modes = list(node.metadata.get("source_hwp_extraction_modes") or [])
            if hwp_extraction_mode not in modes:
                modes.append(hwp_extraction_mode)
            node.metadata["source_hwp_extraction_modes"] = modes
        hwp_stream = source_metadata.get("hwp_stream")
        if hwp_stream:
            streams = list(node.metadata.get("source_hwp_streams") or [])
            if hwp_stream not in streams:
                streams.append(hwp_stream)
            node.metadata["source_hwp_streams"] = streams
        section_index = source_metadata.get("section_index")
        if isinstance(section_index, int):
            section_indices = list(node.metadata.get("source_hwp_section_indices") or [])
            if section_index not in section_indices:
                section_indices.append(section_index)
            node.metadata["source_hwp_section_indices"] = section_indices
        if "hwp_native_table_geometry" in source_metadata:
            node.metadata["source_hwp_native_table_geometry"] = bool(
                node.metadata.get("source_hwp_native_table_geometry") or source_metadata.get("hwp_native_table_geometry")
            )
        for key in ("caption_parent", "caption_count"):
            if key in source_metadata and key not in node.metadata:
                node.metadata[key] = source_metadata[key]
        for key in HWPX_SOURCE_COUNT_METADATA_KEYS:
            value = source_metadata.get(key)
            if isinstance(value, int):
                source_block_key = source_metadata.get("_source_block_key")
                if source_block_key:
                    merged_sources = node.metadata.setdefault("_merged_hwpx_count_sources", {})
                    key_sources = merged_sources.setdefault(key, [])
                    if source_block_key in key_sources:
                        continue
                    key_sources.append(source_block_key)
                node.metadata[key] = int(node.metadata.get(key) or 0) + value
        for key in HWPX_SOURCE_LIST_METADATA_KEYS:
            values = source_metadata.get(key)
            if not isinstance(values, list):
                continue
            merged = list(node.metadata.get(key) or [])
            for value in values:
                if value not in merged:
                    merged.append(value)
            if merged:
                node.metadata[key] = merged[:20]
        review_flags = source_metadata.get("hwpx_parser_review_flags") or []
        if review_flags:
            values = list(node.metadata.get("hwpx_parser_review_flags") or [])
            for value in review_flags:
                if value not in values:
                    values.append(value)
            node.metadata["hwpx_parser_review_flags"] = values
        raw_text = str(source_metadata.get("raw_text") or "").strip()
        if raw_text:
            raw_lines = list(node.metadata.get("source_raw_text_lines") or [])
            if raw_text not in raw_lines:
                raw_lines.append(raw_text)
            node.metadata["source_raw_text_lines"] = raw_lines[:200]
            node.metadata["raw_text"] = "\n".join(raw_lines[:200])
        for key in ("source_page", "page_width", "page_height", "font_size_median"):
            if key in source_metadata and key not in node.metadata:
                node.metadata[key] = source_metadata[key]
        source_bbox = source_metadata.get("source_bbox")
        if isinstance(source_bbox, (list, tuple)) and len(source_bbox) >= 4:
            bboxes = list(node.metadata.get("source_bboxes") or [])
            bbox_values = [float(value) for value in source_bbox[:4]]
            if bbox_values not in bboxes:
                bboxes.append(bbox_values)
            node.metadata["source_bboxes"] = bboxes[:50]
            if "source_bbox" not in node.metadata:
                node.metadata["source_bbox"] = bbox_values
        attachment_refs = source_metadata.get("attachment_references") or []
        if isinstance(attachment_refs, list) and attachment_refs:
            merged_refs = list(node.metadata.get("attachment_references") or [])
            for ref in attachment_refs:
                if ref and ref not in merged_refs:
                    merged_refs.append(ref)
            node.metadata["attachment_references"] = merged_refs[:100]

    def _strip_internal_metadata(self, node: StructureNode) -> None:
        node.metadata.pop("_merged_hwpx_count_sources", None)

    def _reindex_node(self, node: StructureNode, order_index: int) -> StructureNode:
        safe_type = node.node_type.replace(" ", "_")
        node.order_index = order_index
        node.node_id = f"{node.document_id}_{safe_type}_{order_index + 1:04d}"
        return node

    def _update_current(self, current: dict[str, StructureNode | None], node: StructureNode) -> None:
        node_type = node.node_type
        if node_type in {"part", "chapter", "section", "subsection", "article", "paragraph", "item", "subitem"}:
            current[node_type] = node
        elif node_type == "regulation":
            current["regulation"] = node
            current["regulation_parent"] = self._current_node_by_id(current, node.parent_id)
        if node_type == "part":
            self._clear(current, "chapter", "section", "subsection", "regulation", "article", "paragraph", "item", "subitem", "supplementary", "regulation_parent", "last_article")
        elif node_type == "chapter":
            if self._is_within_active_regulation(current, node):
                self._clear(current, "section", "subsection", "article", "paragraph", "item", "subitem", "supplementary", "last_article")
            else:
                self._clear(current, "section", "subsection", "regulation", "article", "paragraph", "item", "subitem", "supplementary", "regulation_parent", "last_article")
        elif node_type == "section":
            if self._is_within_active_regulation(current, node):
                self._clear(current, "subsection", "article", "paragraph", "item", "subitem", "supplementary", "last_article")
            else:
                self._clear(current, "subsection", "regulation", "article", "paragraph", "item", "subitem", "supplementary", "regulation_parent", "last_article")
        elif node_type == "subsection":
            if self._is_within_active_regulation(current, node):
                self._clear(current, "article", "paragraph", "item", "subitem", "supplementary", "last_article")
            else:
                self._clear(current, "regulation", "article", "paragraph", "item", "subitem", "supplementary", "regulation_parent", "last_article")
        elif node_type == "regulation":
            self._clear(current, "chapter", "section", "subsection", "article", "paragraph", "item", "subitem", "supplementary", "last_article")
        elif node_type == "article":
            current["last_article"] = node
            self._clear(current, "paragraph", "item", "subitem")
        elif node_type == "paragraph":
            self._clear(current, "item", "subitem")
        elif node_type == "item":
            self._clear(current, "subitem")
        elif node_type in {"appendix", "form", "table"}:
            current["article"] = node
            self._clear(current, "paragraph", "item", "subitem")
        elif node_type == "supplementary":
            current["supplementary"] = node
            current["article"] = node
            self._clear(current, "paragraph", "item", "subitem")

    def _clear(self, current: dict[str, StructureNode | None], *keys: str) -> None:
        for key in keys:
            current[key] = None

    def _parent_id_for(self, node_type: str, current: dict[str, StructureNode | None]) -> str | None:
        if node_type == "regulation" and current.get("regulation"):
            parent = current.get("regulation_parent")
            if parent:
                return parent.node_id
            part = current.get("part")
            return part.node_id if part else None
        if node_type in {"chapter", "section", "subsection"} and current.get("regulation") and current.get("supplementary"):
            if node_type == "chapter":
                part = current.get("part")
                return part.node_id if part else None
            current["regulation"] = None
            current["regulation_parent"] = None
        parent_priority = {
            "part": [],
            "chapter": ["regulation", "part"],
            "section": ["chapter", "regulation", "part"],
            "subsection": ["section", "chapter", "regulation", "part"],
            "regulation": ["subsection", "section", "chapter", "part"],
            "article": ["supplementary", "subsection", "section", "chapter", "regulation", "part"],
            "paragraph": ["article"],
            "item": ["paragraph", "article"],
            "subitem": ["item", "paragraph", "article"],
            "appendix": ["regulation", "section", "chapter", "part"],
            "form": ["regulation", "section", "chapter", "part"],
            "supplementary": ["regulation", "section", "chapter", "part"],
            "table": ["article", "regulation", "section", "chapter", "part"],
        }
        for key in parent_priority.get(node_type, []):
            parent = current.get(key)
            if parent:
                return parent.node_id
        return None

    def _current_node_by_id(self, current: dict[str, StructureNode | None], node_id: str | None) -> StructureNode | None:
        if not node_id:
            return None
        for node in current.values():
            if node and node.node_id == node_id:
                return node
        return None

    def _is_within_active_regulation(
        self,
        current: dict[str, StructureNode | None],
        node: StructureNode,
    ) -> bool:
        regulation = current.get("regulation")
        if not regulation or not node.parent_id:
            return False
        current_nodes = {item.node_id: item for item in current.values() if item}
        parent_id = node.parent_id
        while parent_id:
            if parent_id == regulation.node_id:
                return True
            parent = current_nodes.get(parent_id)
            parent_id = parent.parent_id if parent else None
        return False

    def _normalize_number(self, value: str | None) -> str | None:
        if value is None:
            return None
        return re.sub(r"\s+", "", value.strip())
