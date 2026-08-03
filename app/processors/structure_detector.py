from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass

from app.schemas.parsed import ParsedBlock, ParsedDocument, ParsedPage
from app.schemas.structure import StructureNode


ARTICLE_TITLE_DELIMITER_PATTERN = r"(?:\([^\)\n]{1,80}\)|\[[^\]\n]{1,80}\]|【[^】\n]{1,80}】)"

# Keep this set aligned with the internal-regulation title vocabulary used by
# MetadataExtractor and the regulation metadata service.  The extra governance
# suffixes occur frequently as standalone public-institution rules.
IMPLICIT_REGULATION_TITLE_SUFFIXES = (
    "정관",
    "규정",
    "규칙",
    "지침",
    "요령",
    "규약",
    "내규",
    "준칙",
    "기준",
    "세칙",
    "편람",
    "강령",
    "예규",
    "직제",
    "규율",
)
IMPLICIT_REGULATION_TITLE_SUFFIX_PATTERN = re.compile(
    rf"(?:{'|'.join(re.escape(suffix) for suffix in IMPLICIT_REGULATION_TITLE_SUFFIXES)})"
    r"(?:[\(（](?:안|개정안)[\)）])?$"
)


PATTERNS = {
    "part": re.compile(r"^\s*(제\s*\d+\s*편)\s+(.+)$"),
    "chapter": re.compile(r"^\s*(제\s*\d+\s*장)\s+(.+)$"),
    "section": re.compile(r"^\s*(제\s*\d+\s*절)\s+(.+)$"),
    "subsection": re.compile(r"^\s*(제\s*\d+\s*관)\s+(.+)$"),
    "regulation": re.compile(r"^\s*(\d+-\d+-\d+)\.\s+(.+)$"),
    "article": re.compile(
        r"^\s*(제\s*\d+\s*조(?:의\s*\d+)?)(?=\s*(?:\(|\[|【|<|삭제|$|\s))"
        r"\s*(?:\(([^)\n]+)\)|\[([^\]\n]+)\]|【([^】\n]+)】)?\s*(.*)$"
    ),
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
    "appendix": re.compile(
        r"^\s*[\[【<]?\s*((?:별\s*표\s*(?:\d+(?:\s*(?:의|-)\s*\d+)?)?"
        r"|(?:붙\s*임|첨\s*부)\s*제?\s*\d+(?:\s*(?:의|-)\s*\d+)?"
        r"|부\s*록\s*(?:제?\s*\d+(?:\s*(?:의|-)\s*\d+)?)?))"
        r"\s*[\]】>]?\s*(.*)$"
    ),
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
    rf"(?<!\S)제\s*\d+\s*조(?:의\s*\d+)?(?=\s*{ARTICLE_TITLE_DELIMITER_PATTERN})"
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

STRUCTURE_BOUNDARY_DIAGNOSTIC_METADATA_KEY = "structure_boundary_diagnostic"
AMBIGUOUS_COMBINED_BOOK_BOUNDARY_DIAGNOSTIC = (
    "ambiguous_combined_book_boundary_after_attachment"
)


@dataclass
class SourceLine:
    text: str
    page_no: int | None
    block_type: str = "text"
    metadata: dict | None = None


class StructureDetector:
    def detect(self, parsed: ParsedDocument) -> list[StructureNode]:
        parsed.metadata.pop(STRUCTURE_BOUNDARY_DIAGNOSTIC_METADATA_KEY, None)
        lines = self._extract_lines(parsed)
        detected_lines = [
            self._detect_line(line, parsed.document_id, 0)
            for line in lines
        ]
        title_only_regulation_boundaries = self._title_only_regulation_boundaries(
            lines,
            parsed.document_id,
            detected_lines,
        )
        if not title_only_regulation_boundaries:
            title_only_regulation_boundaries = self._implicit_title_only_regulation_boundaries(
                lines,
                parsed.document_id,
                detected_lines,
                document_name=parsed.document_name,
                document_metadata=parsed.metadata,
            )
        navigation_line_indexes = self._navigation_line_indexes(
            lines,
            parsed.document_id,
            detected_lines,
            title_only_regulation_boundaries,
        )
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

        for line_index, line in enumerate(lines):
            if line_index in navigation_line_indexes:
                continue
            detected = title_only_regulation_boundaries.get(line_index)
            if detected is not None:
                detected = self._reindex_node(detected, len(nodes))
            else:
                detected = detected_lines[line_index]
                if detected is not None:
                    detected = self._reindex_node(detected, len(nodes))
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
            title = next(
                (
                    candidate.strip()
                    for candidate in match.group(2, 3, 4)
                    if candidate and candidate.strip()
                ),
                None,
            )
            trailing = (match.group(5) or "").strip()
            if not title:
                title = self._article_lifecycle_title(trailing)
            if not title and self._looks_like_article_reference_tail(trailing):
                return None
            if not title:
                plain_title = self._plain_article_title(trailing)
                if plain_title:
                    title = plain_title
                    trailing = ""
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

    def _plain_article_title(self, trailing: str) -> str | None:
        """Return an unbracketed article heading only when it is title-only.

        A bare ``제1조 목적`` is common in converted public regulations, but a
        bare ``제1조 이 규정은 ...`` is ordinary body text.  Do not guess a title
        from a line that contains sentence punctuation or a common Korean
        predicate; the latter must keep the existing missing-title warning.
        """
        candidate = trailing.strip()
        if not candidate or len(candidate) > 80:
            return None
        if re.search(r"[.!?。:：;；]", candidate):
            return None
        if re.search(r"(?:한다|된다|있다|없다|같다|따른다|본다|말한다|하여야\s*한다|할\s*수\s*있다)$", candidate):
            return None
        if re.search(r"(?:은|는|이|가|을|를|에|에서|으로|로)\s", candidate):
            return None
        if not re.fullmatch(r"[가-힣A-Za-z0-9ㆍ·&/\-\s]+", candidate):
            return None
        return candidate

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

    def _title_only_regulation_boundaries(
        self,
        lines: list[SourceLine],
        document_id: str,
        detected_lines: list[StructureNode | None],
    ) -> dict[int, StructureNode]:
        """Infer numbered regulation boundaries from ordered contents-title matches.

        A title match alone is too weak: the same text can be prose, an article
        title, or a running header.  Inference therefore requires at least two
        unique regulation entries in a contents block, all titles to recur as
        plain standalone lines in contents order, and the first article after
        each selected occurrence to restart at Article 1 before another known
        regulation title or numbered regulation boundary.
        """
        detected = list(enumerate(detected_lines))
        numbered_regulations = [
            (index, node)
            for index, node in detected
            if node is not None and node.node_type == "regulation"
        ]
        articles = [
            (index, node)
            for index, node in detected
            if node is not None and node.node_type == "article"
        ]
        if len(numbered_regulations) < 2 or not articles:
            return {}

        first_article_index = articles[0][0]
        numbered_by_index = dict(numbered_regulations)
        contents_starts = self._contents_starts_by_regulation_index(lines, numbered_by_index)
        contents_entries: list[tuple[int, StructureNode, str, str]] = []
        for index, regulation in numbered_regulations:
            if index >= first_article_index:
                continue
            if index not in contents_starts and not self._looks_like_navigation_entry(lines[index].text):
                continue
            title = self._navigation_regulation_title(regulation.title or "")
            if not self._looks_like_plain_regulation_title(title):
                continue
            identity = self._regulation_title_identity(title)
            if identity:
                contents_entries.append((index, regulation, title, identity))

        if len(contents_entries) < 2:
            return {}
        contents_identities = [entry[3] for entry in contents_entries]

        contents_end = max(entry[0] for entry in contents_entries)
        known_identities = set(contents_identities)
        occurrences: list[tuple[int, str]] = []
        for index, line in enumerate(lines):
            if index <= contents_end or line.block_type == "table":
                continue
            if detected_lines[index] is not None:
                continue
            if not self._looks_like_plain_regulation_title(line.text):
                continue
            identity = self._regulation_title_identity(line.text)
            if identity in known_identities:
                occurrences.append((index, identity))

        if len(occurrences) < len(contents_entries):
            return {}

        occurrence_indexes = {index for index, _identity in occurrences}
        numbered_body_indexes = {
            index for index, _node in numbered_regulations if index > contents_end
        }
        possible_boundary_indexes = sorted(occurrence_indexes | numbered_body_indexes)
        article_indexes = [index for index, _article in articles]
        article_by_index = dict(articles)
        attachment_indexes = [
            index
            for index, node in enumerate(detected_lines)
            if node is not None and node.node_type in {"appendix", "form", "supplementary"}
        ]
        preamble_prefix = [0]
        for node in detected_lines:
            preamble_prefix.append(
                preamble_prefix[-1]
                + int(node is not None and node.node_type in {"part", "chapter"})
            )
        first_text_index_by_page: dict[int, int] = {}
        for line_index, line in enumerate(lines):
            if line.block_type == "table" or line.page_no is None or not line.text.strip():
                continue
            first_text_index_by_page.setdefault(line.page_no, line_index)
        valid_occurrences: dict[str, list[int]] = {identity: [] for identity in known_identities}
        for index, identity in occurrences:
            boundary_position = bisect_right(possible_boundary_indexes, index)
            next_boundary = (
                possible_boundary_indexes[boundary_position]
                if boundary_position < len(possible_boundary_indexes)
                else len(lines)
            )
            article_position = bisect_right(article_indexes, index)
            first_following_article = None
            if (
                article_position < len(article_indexes)
                and article_indexes[article_position] < next_boundary
            ):
                first_following_article = article_by_index[article_indexes[article_position]]
            if first_following_article is None or first_following_article.number != "제1조":
                continue
            first_article_index = article_indexes[article_position]
            if not self._has_new_unit_evidence_after_attachment(
                lines=lines,
                candidate_index=index,
                article_index=first_article_index,
                attachment_indexes=attachment_indexes,
                first_text_index_by_page=first_text_index_by_page,
                preamble_prefix=preamble_prefix,
            ):
                continue
            valid_occurrences[identity].append(index)

        expected_occurrence_counts = {
            identity: contents_identities.count(identity)
            for identity in known_identities
        }
        if any(
            len(valid_occurrences[identity]) != expected_count
            for identity, expected_count in expected_occurrence_counts.items()
        ):
            return {}

        selected: list[tuple[int, StructureNode, str, int]] = []
        previous_body_index = contents_end
        for contents_index, regulation, title, identity in contents_entries:
            body_index = next(
                (index for index in valid_occurrences[identity] if index > previous_body_index),
                None,
            )
            if body_index is None:
                return {}
            selected.append((contents_index, regulation, title, body_index))
            previous_body_index = body_index

        boundaries: dict[int, StructureNode] = {}
        for contents_index, regulation, title, body_index in selected:
            line = lines[body_index]
            node = self._node(
                document_id,
                "regulation",
                regulation.number,
                title,
                line.text,
                line.page_no,
                0,
                line.metadata,
            )
            node.confidence = 0.96
            node.warnings.append("regulation_number_inferred_from_contents_title")
            node.metadata["regulation_boundary_source"] = "contents_title_and_article_restart"
            node.metadata["contents_line_index"] = contents_index
            boundaries[body_index] = node
        return boundaries

    def _implicit_title_only_regulation_boundaries(
        self,
        lines: list[SourceLine],
        document_id: str,
        detected_lines: list[StructureNode | None],
        *,
        document_name: str,
        document_metadata: dict | None = None,
    ) -> dict[int, StructureNode]:
        """Infer a title-only combined book when no numbered contents exists.

        Every candidate must be a regulation-shaped standalone title,
        candidates must be unique, and each candidate's first article before
        the next boundary must restart at Article 1. A single title is accepted
        only when it exactly matches the standalone document name.
        """

        raw_candidates = [
            (
                index,
                self._regulation_title_for_matching(line.text),
                self._regulation_title_identity(line.text),
            )
            for index, line in enumerate(lines)
            if line.block_type != "table"
            and detected_lines[index] is None
            and self._looks_like_implicit_regulation_title(line.text)
        ]
        if not raw_candidates:
            return {}

        articles = [
            (index, node)
            for index, node in enumerate(detected_lines)
            if node is not None and node.node_type == "article"
        ]
        article_indexes = [index for index, _node in articles]
        article_by_index = dict(articles)
        explicit_boundary_indexes = {
            index
            for index, node in enumerate(detected_lines)
            if node is not None and node.node_type == "regulation"
        }
        contents_marker_indexes = [
            index
            for index, line in enumerate(lines)
            if re.sub(r"\s+", "", line.text).casefold() in {"목차", "차례", "contents"}
        ]
        if explicit_boundary_indexes and contents_marker_indexes:
            first_explicit_boundary = min(explicit_boundary_indexes)
            active_contents_markers = [
                index for index in contents_marker_indexes if index < first_explicit_boundary
            ]
            if active_contents_markers:
                contents_start = max(active_contents_markers)
                raw_candidates = [
                    candidate
                    for candidate in raw_candidates
                    if candidate[0] < contents_start or candidate[0] >= first_explicit_boundary
                ]
                if not raw_candidates:
                    return {}
        raw_boundary_indexes = sorted(
            {index for index, _title, _identity in raw_candidates}
            | explicit_boundary_indexes
        )
        attachment_indexes = [
            index
            for index, node in enumerate(detected_lines)
            if node is not None and node.node_type in {"appendix", "form", "supplementary"}
        ]
        preamble_prefix = [0]
        for node in detected_lines:
            preamble_prefix.append(
                preamble_prefix[-1]
                + int(node is not None and node.node_type in {"part", "chapter"})
            )
        first_text_index_by_page: dict[int, int] = {}
        for index, line in enumerate(lines):
            if line.block_type == "table" or line.page_no is None or not line.text.strip():
                continue
            first_text_index_by_page.setdefault(line.page_no, index)

        valid_candidates: list[tuple[int, str, str]] = []
        ambiguous_candidates: list[tuple[int, str, str]] = []
        for candidate in raw_candidates:
            index, _title, _identity = candidate
            boundary_position = bisect_right(raw_boundary_indexes, index)
            next_boundary = (
                raw_boundary_indexes[boundary_position]
                if boundary_position < len(raw_boundary_indexes)
                else len(lines)
            )
            article_position = bisect_right(article_indexes, index)
            if article_position >= len(article_indexes):
                continue
            article_index = article_indexes[article_position]
            first_article = article_by_index[article_index]
            if article_index >= next_boundary or first_article.number != "제1조":
                continue

            if not self._has_new_unit_evidence_after_attachment(
                lines=lines,
                candidate_index=index,
                article_index=article_index,
                attachment_indexes=attachment_indexes,
                first_text_index_by_page=first_text_index_by_page,
                preamble_prefix=preamble_prefix,
            ):
                # A regulation-shaped title and Article 1 inside an
                # appendix/form/supplementary provision is commonly an
                # embedded sample or amendment quotation, not a new unit.
                # If another attachment/supplementary marker intervenes, the
                # later Article 1 belongs to that nested scope and this title
                # is ordinary attachment content. Without such a marker the
                # boundary is genuinely ambiguous, so a combined book must
                # fail closed instead of publishing only its earlier units.
                if not any(
                    index < attachment_index < article_index
                    for attachment_index in attachment_indexes
                ):
                    ambiguous_candidates.append(candidate)
                continue
            valid_candidates.append(candidate)

        valid_identities = {identity for _index, _title, identity in valid_candidates}
        has_distinct_ambiguous_candidate = any(
            identity not in valid_identities
            for _index, _title, identity in ambiguous_candidates
        )
        candidates = valid_candidates
        normalized_document_name = re.sub(
            r"\.(?:pdf|hwp|hwpx|docx)$",
            "",
            str(document_name or "").strip(),
            flags=re.IGNORECASE,
        )
        document_title_identity = self._regulation_title_identity(normalized_document_name)
        single_document_title_match = bool(
            len(candidates) == 1
            and document_title_identity
            and candidates[0][2] == document_title_identity
        )
        if has_distinct_ambiguous_candidate and not single_document_title_match:
            # A title followed by an Article 1 restart after an attachment may
            # be a real regulation whose page/preamble boundary evidence was
            # lost. A bare attachment label such as "지급기준" without an
            # Article 1 restart is ordinary attachment content and must not
            # hard-block an otherwise explicit single-regulation document.
            if document_metadata is not None:
                document_metadata[STRUCTURE_BOUNDARY_DIAGNOSTIC_METADATA_KEY] = (
                    AMBIGUOUS_COMBINED_BOOK_BOUNDARY_DIAGNOSTIC
                )
            return {}
        if len(candidates) < 2 and not single_document_title_match:
            return {}
        identities = [identity for _index, _title, identity in candidates]
        if len(set(identities)) != len(identities):
            return {}

        if len(articles) < len(candidates):
            return {}
        possible_boundary_indexes = sorted(
            {index for index, _title, _identity in candidates}
            | explicit_boundary_indexes
        )

        boundaries: dict[int, StructureNode] = {}
        for index, title, _identity in candidates:
            boundary_position = bisect_right(possible_boundary_indexes, index)
            next_boundary = (
                possible_boundary_indexes[boundary_position]
                if boundary_position < len(possible_boundary_indexes)
                else len(lines)
            )
            article_position = bisect_right(article_indexes, index)
            if article_position >= len(article_indexes):
                return {}
            article_index = article_indexes[article_position]
            first_article = article_by_index[article_index]
            if article_index >= next_boundary or first_article.number != "제1조":
                return {}

            line = lines[index]
            node = self._node(
                document_id,
                "regulation",
                None,
                title,
                line.text,
                line.page_no,
                0,
                line.metadata,
            )
            node.confidence = 0.9
            if single_document_title_match:
                node.warnings.append("regulation_boundary_inferred_from_document_title")
                node.metadata["regulation_boundary_source"] = "document_title_and_article_restart"
            else:
                node.warnings.append("regulation_boundary_inferred_without_contents")
                node.metadata["regulation_boundary_source"] = "title_suffix_and_article_restart"
            boundaries[index] = node
        return boundaries

    def _navigation_line_indexes(
        self,
        lines: list[SourceLine],
        document_id: str,
        detected_lines: list[StructureNode | None],
        title_only_regulation_boundaries: dict[int, StructureNode] | None = None,
    ) -> set[int]:
        """Return a tightly-evidenced table-of-contents range to exclude from body state.

        A repeated regulation code alone is a valid running-header pattern, so it
        must not be treated as navigation.  We only suppress an initial range
        when it has a contents marker (or a dot-leader/page-number entry), each
        candidate is repeated later, and that later occurrence is followed by a
        real article before the next regulation boundary.
        """
        detected = list(enumerate(detected_lines))
        numbered_regulations = [
            (index, node)
            for index, node in detected
            if node is not None and node.node_type == "regulation"
        ]
        preview_navigation_indexes, preview_identities = self._unnumbered_contents_preview_range(
            lines,
            numbered_regulations,
        )
        article_indexes = {
            index
            for index, node in detected
            if node is not None
            and node.node_type == "article"
            and index not in preview_navigation_indexes
        }
        if not numbered_regulations or not article_indexes:
            return set()

        first_article_index = min(article_indexes)
        evidence_regulations = sorted(
            [*numbered_regulations, *(title_only_regulation_boundaries or {}).items()],
            key=lambda item: item[0],
        )
        regulation_by_index = dict(evidence_regulations)
        body_article_evidence = self._regulation_body_article_evidence(
            len(lines),
            regulation_by_index,
            article_indexes,
        )
        repeated_body_evidence = self._later_repeated_regulation_evidence(
            evidence_regulations,
            body_article_evidence,
        )
        numbered_regulation_by_index = dict(numbered_regulations)
        contents_starts = self._contents_starts_by_regulation_index(lines, numbered_regulation_by_index)
        navigation_indexes: set[int] = set(preview_navigation_indexes)
        navigation_starts: list[int] = []
        named_contents_identities: set[tuple[str | None, str]] = set(preview_identities)

        for index, _regulation in numbered_regulations:
            # Navigation entries appear before the first body article.  This
            # prevents a repeated page header in an already-started regulation
            # from being misclassified as a contents entry.
            if index >= first_article_index:
                continue
            toc_start = contents_starts.get(index)
            has_dot_leader = self._looks_like_navigation_entry(lines[index].text)
            if toc_start is None and not has_dot_leader:
                continue
            identity = self._navigation_regulation_identity(_regulation)
            first_named_contents_occurrence = bool(
                toc_start is not None and identity not in named_contents_identities
            )
            if toc_start is not None:
                named_contents_identities.add(identity)
            # An exact named contents marker is sufficient evidence that these
            # first pre-article numbered occurrences are navigation. Suppress
            # them even when body boundary inference fails closed, while
            # preserving a later repeated numbered body boundary.
            if first_named_contents_occurrence or repeated_body_evidence.get(index, False):
                navigation_indexes.add(index)
                if toc_start is not None:
                    navigation_starts.append(toc_start)

        if not navigation_indexes:
            return set()
        # A named contents block is safe to remove as a contiguous range once
        # its pre-article regulation rows have been identified above.
        if navigation_starts:
            start = min(navigation_starts)
            end = max(navigation_indexes)
            navigation_indexes.update(range(start, end + 1))
        return navigation_indexes

    def _unnumbered_contents_preview_range(
        self,
        lines: list[SourceLine],
        numbered_regulations: list[tuple[int, StructureNode]],
    ) -> tuple[set[int], set[tuple[str | None, str]]]:
        """Recognize a named TOC whose title rows include Article 1 previews."""

        if len(numbered_regulations) < 2:
            return set(), set()
        body_identities = {
            self._navigation_regulation_identity(node)
            for _index, node in numbered_regulations
        }
        first_numbered_index = min(index for index, _node in numbered_regulations)
        marker_indexes = [
            index
            for index, line in enumerate(lines[:first_numbered_index])
            if re.sub(r"\s+", "", line.text).casefold() in {"목차", "차례", "contents"}
        ]
        if not marker_indexes:
            return set(), set()
        contents_start = max(marker_indexes)
        preview_identities = {
            (None, self._regulation_title_identity(line.text))
            for line in lines[contents_start + 1 : first_numbered_index]
            if line.block_type != "table"
            and self._looks_like_implicit_regulation_title(line.text)
            and self._regulation_title_identity(line.text)
        }
        if len(preview_identities) < 2:
            return set(), set()
        body_title_identities = {identity for _number, identity in body_identities}
        preview_title_identities = {identity for _number, identity in preview_identities}
        if not preview_title_identities.issubset(body_title_identities):
            return set(), set()
        matched_body_identities = {
            identity
            for identity in body_identities
            if identity[1] in preview_title_identities
        }
        return set(range(contents_start, first_numbered_index)), matched_body_identities

    def _regulation_body_article_evidence(
        self,
        line_count: int,
        regulation_by_index: dict[int, StructureNode],
        article_indexes: set[int],
    ) -> dict[int, bool]:
        """Return whether each regulation is followed by an article before the next one."""
        evidence: dict[int, bool] = {}
        article_before_next_regulation = False
        for index in range(line_count - 1, -1, -1):
            if index in article_indexes:
                article_before_next_regulation = True
            if index in regulation_by_index:
                evidence[index] = article_before_next_regulation
                article_before_next_regulation = False
        return evidence

    def _later_repeated_regulation_evidence(
        self,
        regulations: list[tuple[int, StructureNode]],
        body_article_evidence: dict[int, bool],
    ) -> dict[int, bool]:
        """Return later same-identity occurrences that have body-article evidence.

        Processing the ordered regulations in reverse avoids rescanning all
        regulations/articles for every table-of-contents entry.
        """
        seen_evidenced_identities: set[tuple[str | None, str]] = set()
        evidence: dict[int, bool] = {}
        for index, regulation in reversed(regulations):
            identity = self._navigation_regulation_identity(regulation)
            evidence[index] = identity in seen_evidenced_identities
            if body_article_evidence.get(index, False):
                seen_evidenced_identities.add(identity)
        return evidence

    def _contents_starts_by_regulation_index(
        self,
        lines: list[SourceLine],
        regulation_by_index: dict[int, StructureNode],
    ) -> dict[int, int]:
        """Map regulation entries to the active named contents block.

        Consumers already constrain navigation candidates to the lines before
        the first body article.  Avoid a fixed line-distance cutoff here:
        public regulation books can have long multi-page contents sections and
        partial TOC recognition would silently merge the omitted final units.
        """
        contents_start: int | None = None
        starts: dict[int, int] = {}
        for index, line in enumerate(lines):
            compact = re.sub(r"\s+", "", line.text).lower()
            if compact in {"목차", "차례", "contents"}:
                contents_start = index
            if (
                index in regulation_by_index
                and contents_start is not None
            ):
                starts[index] = contents_start
        return starts

    def _has_new_unit_evidence_after_attachment(
        self,
        *,
        lines: list[SourceLine],
        candidate_index: int,
        article_index: int,
        attachment_indexes: list[int],
        first_text_index_by_page: dict[int, int],
        preamble_prefix: list[int],
    ) -> bool:
        """Require both page-layout and structural evidence after an embedded scope."""

        attachment_position = bisect_right(attachment_indexes, candidate_index - 1) - 1
        if attachment_position < 0:
            return True
        preceding_attachment = attachment_indexes[attachment_position]
        candidate_page = lines[candidate_index].page_no
        attachment_page = lines[preceding_attachment].page_no
        starts_later_page = bool(
            candidate_page is not None
            and candidate_page != attachment_page
            and first_text_index_by_page.get(candidate_page) == candidate_index
        )
        has_new_regulation_preamble = bool(
            preamble_prefix[article_index] - preamble_prefix[candidate_index + 1]
        )
        return starts_later_page and has_new_regulation_preamble

    def _looks_like_navigation_entry(self, text: str) -> bool:
        return bool(re.search(r"\.{3,}\s*\d{1,4}\s*$", text))

    def _navigation_regulation_title(self, title: str) -> str:
        return re.sub(r"\.{3,}\s*\d{1,4}\s*$", "", title).strip()

    def _looks_like_plain_regulation_title(self, text: str) -> bool:
        candidate = self._regulation_title_for_matching(text)
        if not candidate or len(candidate) > 120:
            return False
        if re.search(r"[.!?。:：;；]", candidate):
            return False
        return bool(re.fullmatch(r"[가-힣A-Za-z0-9ㆍ·&/()（）\-\s]+", candidate))

    def _looks_like_implicit_regulation_title(self, text: str) -> bool:
        if not self._looks_like_plain_regulation_title(text):
            return False
        compact = re.sub(r"\s+", "", self._regulation_title_for_matching(text))
        return bool(IMPLICIT_REGULATION_TITLE_SUFFIX_PATTERN.search(compact))

    def _regulation_title_identity(self, title: str) -> str:
        return re.sub(r"\s+", "", self._regulation_title_for_matching(title)).casefold()

    def _regulation_title_for_matching(self, title: str) -> str:
        """Normalize display wrappers and trailing revision labels for identity only."""

        candidate = str(title or "").strip()
        revision_suffix = re.search(r"\s*[\(（](?P<label>[^\n()（）]{1,120})[\)）]\s*$", candidate)
        if revision_suffix:
            compact_label = re.sub(r"\s+", "", revision_suffix.group("label"))
            date_token = r"(?:\d{4}(?:\.\d{1,2}\.\d{1,2}\.?|년\d{1,2}월\d{1,2}일))"
            revision_marker = r"(?:제정|일부개정|전부개정|전문개정|개정|시행)"
            is_revision_label = bool(
                re.fullmatch(
                    rf"(?:{date_token})?{revision_marker}(?:{date_token})?",
                    compact_label,
                )
            )
            is_version_label = compact_label in {"구", "신", "현행", "개정전", "개정후"}
            if is_revision_label or is_version_label:
                candidate = candidate[: revision_suffix.start()].strip()
        wrapper_pairs = {
            "「": "」",
            "『": "』",
            "【": "】",
            "〈": "〉",
            "《": "》",
        }
        if candidate and wrapper_pairs.get(candidate[0]) == candidate[-1]:
            candidate = candidate[1:-1].strip()
        return candidate

    def _navigation_regulation_identity(self, node: StructureNode) -> tuple[str | None, str]:
        title = self._navigation_regulation_title(node.title or "")
        return node.number, self._regulation_title_identity(title)

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
        if PATTERNS["appendix"].match(text) or PATTERNS["form"].match(text):
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
            if (
                self._line_starts_with_paragraph_marker(text)
                and not self._line_starts_with_circled_marker(text)
                and self._is_circled_marker(match.group(0))
            ):
                # A circled marker inside a line that opens with □ or (N) is a
                # sub-enumeration of that paragraph, so keep it inline.  But when
                # the line itself opens with a circled marker (①), a following
                # circled marker (②) is a sibling 항 flattened onto one line and
                # must be split off.
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
        if re.match(
            rf"\s*{ARTICLE_TITLE_DELIMITER_PATTERN}\s*(?:의|에|에서|으로|로|을|를|은|는|과|와|및|관련|따라|중)",
            after,
        ):
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

    def _line_starts_with_circled_marker(self, text: str) -> bool:
        return bool(re.match(r"^\s*[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚]", text))

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
        bracketed = compact.startswith(
            (
                "[별표",
                "【별표",
                "<별표",
                "[별지",
                "【별지",
                "<별지",
                "[붙임",
                "【붙임",
                "<붙임",
                "[첨부",
                "【첨부",
                "<첨부",
                "[부록",
                "【부록",
                "<부록",
            )
        )
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
        xml_role = str(source_metadata.get("source_xml_role") or "").strip()
        if xml_role:
            xml_roles = list(node.metadata.get("source_xml_roles") or [])
            if xml_role not in xml_roles:
                xml_roles.append(xml_role)
            node.metadata["source_xml_roles"] = xml_roles
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
        if node_type in {"appendix", "form", "supplementary"}:
            regulation = current.get("regulation")
            return regulation.node_id if regulation else None
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
