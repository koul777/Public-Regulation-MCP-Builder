from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable

from app.processors.mojibake import (
    MOJIBAKE_CLEANED_CHARS_KEY,
    MOJIBAKE_REMOVED_BLOCKS_KEY,
    MOJIBAKE_REMOVED_CHARS_KEY,
    strip_mojibake_artifacts,
)
from app.schemas.parsed import ParsedBlock, ParsedDocument, ParsedPage


# 쪽마다 화면을 갱신하면 진행 표시 자체가 정리보다 오래 걸린다.
NORMALIZE_PROGRESS_PAGE_STEP = 20
HEADING_PREFIX = re.compile(
    r"^\s*(제\s*\d+\s*(?:편|장|절|관|조)|[①-⑳㉑-㉚]|\(\d+\)|\d+\.|[가-힣][\.\)])"
)
PRIVATE_USE_REPEAT = re.compile(r"([\ue000-\uf8ff])\1{2,}")
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
    def normalize_document(
        self,
        parsed: ParsedDocument,
        *,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> ParsedDocument:
        """본문을 정리한다. 통합 규정집에서는 이 단계만 몇 분씩 걸린다.

        ``progress_callback``은 실제로 정리한 쪽 수만 알린다. 진행률을 시간으로
        추정하지 않는다. 화면이 멈춘 것처럼 보이는 이유는 오래 걸려서가 아니라
        오래 걸리는 동안 아무 숫자도 세어 주지 않았기 때문이다.
        """

        repeated = self._repeated_edge_lines(parsed)
        pages: list[ParsedPage] = []
        raw_parts: list[str] = []
        removed_chars = 0
        removed_blocks = 0
        boilerplate_chars = 0
        page_total = len(parsed.pages)
        if progress_callback is not None:
            progress_callback(0, page_total)
        for page_index, page in enumerate(parsed.pages, start=1):
            blocks: list[ParsedBlock] = []
            for block in page.blocks:
                normalized, block_removed, block_boilerplate = self._normalize_text_with_stats(block.text)
                boilerplate_chars += block_boilerplate
                if block_removed:
                    removed_chars += block_removed
                    removed_blocks += 1
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
            if progress_callback is not None and (
                page_index == page_total or page_index % NORMALIZE_PROGRESS_PAGE_STEP == 0
            ):
                progress_callback(page_index, page_total)

        # \uae68\uc9c4 \uae00\uc790\ub97c \uc9c0\uc6b0\uace0 \ub098\uba74 \ubcf8\ubb38\ub9cc \ubd10\uc11c\ub294 \uc190\uc0c1 \ud754\uc801\uc744 \uc54c \uc218 \uc5c6\ub2e4. \uc9c0\uc6b4 \uc591\uc744 \ub0a8\uaca8
        # \ud488\uc9c8 \uac80\uc0ac\uac00 \uacc4\uc18d \uacbd\uace0\ud558\ub3c4\ub85d \ud55c\ub2e4(\uc218\uc2dd\ucc98\ub7fc \ub0b4\uc6a9\uc774 \ud1b5\uc9f8\ub85c \ub0a0\uc544\uac04 \uacbd\uc6b0\uac00 \uc788\ub2e4).
        metadata = {
            **dict(parsed.metadata or {}),
            MOJIBAKE_REMOVED_CHARS_KEY: removed_chars,
            MOJIBAKE_REMOVED_BLOCKS_KEY: removed_blocks,
            MOJIBAKE_CLEANED_CHARS_KEY: boilerplate_chars,
        }
        return parsed.model_copy(
            update={"pages": pages, "raw_text": "\n".join(raw_parts), "metadata": metadata}
        )

    def normalize_text(self, text: str) -> str:
        normalized, _damaged, _boilerplate = self._normalize_text_with_stats(text)
        return normalized

    def _normalize_text_with_stats(self, text: str) -> tuple[str, int, int]:
        """\uc815\uaddc\ud654\ud55c \ubcf8\ubb38\uacfc \uadf8 \uacfc\uc815\uc5d0\uc11c \uc9c0\uc6b4 \uae68\uc9c4 \uae00\uc790 \uc218\ub97c \ud568\uaed8 \ub3cc\ub824\uc900\ub2e4."""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\u00a0", " ").replace("\u200b", "")
        text = text.translate(PRIVATE_USE_GLYPH_TRANSLATION)
        text, removed, boilerplate = strip_mojibake_artifacts(text)
        text = PRIVATE_USE_REPEAT.sub(" ", text)
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return "\n".join(line.strip() for line in text.splitlines()).strip(), removed, boilerplate

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
                # dedupe so a single-line page (first line == last line) counts once
                edges.extend(dict.fromkeys([*lines[:1], *lines[-1:]]))
        counts = Counter(edges)
        threshold = max(3, len(parsed.pages) // 2)
        return {line for line, count in counts.items() if count >= threshold and not self._looks_like_structure(line)}

    def _looks_like_structure(self, line: str) -> bool:
        return bool(HEADING_PREFIX.match(line) or line.startswith(("부칙", "[별표", "별표", "[별지", "별지")))

    def _looks_like_page_footer(self, line: str) -> bool:
        return bool(re.fullmatch(r"-\s*\d+\s*-", line))
