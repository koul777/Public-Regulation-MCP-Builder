from __future__ import annotations

import re
from collections import Counter

from app.schemas.parsed import ParsedBlock, ParsedDocument, ParsedPage


HEADING_PREFIX = re.compile(
    r"^\s*(제\s*\d+\s*(?:편|장|절|관|조)|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮]|\(\d+\)|\d+\.|[가-힣][\.\)])"
)
PRIVATE_USE_REPEAT = re.compile(r"([\ue000-\uf8ff])\1{2,}")
HWP_INLINE_ARTIFACT = re.compile(
    r"(^|[\s\-–—])(?:捤獥|汤捯|氠瑢|湰灧|桤灧|灳瑣|湯慴|湯湷|†普)(?=$|[\s\-–—])"
)
PRIVATE_USE_GLYPH_TRANSLATION = str.maketrans(
    {
        "\uf09f": "•",
        "\uf09e": "◦",
        "\uf0a7": "▪",
        "\uf077": "▪",
        "\uf0e8": "→",
        "\uf081": "①",
        "\uf082": "②",
        "\uf083": "③",
        "\uf084": "④",
        "\uf085": "⑤",
        "\uf086": "⑥",
        "\uf087": "⑦",
        "\uf088": "⑧",
        "\uf089": "⑨",
        "\uf08a": "⑩",
        "\uf000": '"',
        "\ue046": "-",
        "\ue06d": "/",
    }
)


class TextNormalizer:
    def normalize_document(self, parsed: ParsedDocument) -> ParsedDocument:
        repeated = self._repeated_edge_lines(parsed)
        pages: list[ParsedPage] = []
        raw_parts: list[str] = []
        for page in parsed.pages:
            blocks: list[ParsedBlock] = []
            for block in page.blocks:
                normalized = self.normalize_text(block.text)
                filtered_lines = [
                    line
                    for line in normalized.splitlines()
                    if line.strip()
                    and line.strip() not in repeated
                    and not self._looks_like_page_footer(line.strip())
                ]
                if not filtered_lines:
                    continue
                text = self.repair_line_breaks("\n".join(filtered_lines))
                blocks.append(block.model_copy(update={"text": text}))
                raw_parts.append(text)
            pages.append(page.model_copy(update={"blocks": blocks}))

        return parsed.model_copy(update={"pages": pages, "raw_text": "\n".join(raw_parts)})

    def normalize_text(self, text: str) -> str:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\u00a0", " ").replace("\u200b", "")
        text = text.translate(PRIVATE_USE_GLYPH_TRANSLATION)
        text = HWP_INLINE_ARTIFACT.sub(r"\1", text)
        text = PRIVATE_USE_REPEAT.sub(" ", text)
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return "\n".join(line.strip() for line in text.splitlines()).strip()

    def repair_line_breaks(self, text: str) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return ""
        repaired: list[str] = []
        for line in lines:
            if not repaired:
                repaired.append(line)
                continue
            previous = repaired[-1]
            if HEADING_PREFIX.match(line) or previous.endswith((".", "다.", "함.", "음.", "요.", "?", "!", ":", ";")):
                repaired.append(line)
            else:
                repaired[-1] = f"{previous} {line}"
        return "\n".join(repaired)

    def _repeated_edge_lines(self, parsed: ParsedDocument) -> set[str]:
        if len(parsed.pages) < 3:
            return set()
        edges: list[str] = []
        for page in parsed.pages:
            lines = [line.strip() for block in page.blocks for line in block.text.splitlines() if line.strip()]
            if lines:
                edges.extend(lines[:1])
                edges.extend(lines[-1:])
        counts = Counter(edges)
        threshold = max(3, len(parsed.pages) // 2)
        return {line for line, count in counts.items() if count >= threshold and not self._looks_like_structure(line)}

    def _looks_like_structure(self, line: str) -> bool:
        return bool(HEADING_PREFIX.match(line) or line.startswith(("부칙", "[별표", "별표", "[별지", "별지")))

    def _looks_like_page_footer(self, line: str) -> bool:
        return bool(re.fullmatch(r"-\s*\d+\s*-", line))
