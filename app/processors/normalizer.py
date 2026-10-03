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
# 쪽번호로 보고 지운 줄 수. 본문 숫자를 잘못 지웠을 때 흔적이 남도록 기록한다.
PAGE_NUMBER_LINES_REMOVED_KEY = "page_number_lines_removed"
PAGE_NUMBER_EDGES = ("top", "bottom")
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
        page_number_patterns = self._repeated_page_number_patterns(parsed)
        pages: list[ParsedPage] = []
        raw_parts: list[str] = []
        removed_chars = 0
        removed_blocks = 0
        boilerplate_chars = 0
        page_number_lines_removed = 0
        page_total = len(parsed.pages)
        if progress_callback is not None:
            progress_callback(0, page_total)
        for page_index, page in enumerate(parsed.pages, start=1):
            blocks: list[ParsedBlock] = []
            normalized_blocks: list[tuple[ParsedBlock, list[str]]] = []
            for block in page.blocks:
                normalized, block_removed, block_boilerplate = self._normalize_text_with_stats(block.text)
                boilerplate_chars += block_boilerplate
                if block_removed:
                    removed_chars += block_removed
                    removed_blocks += 1
                normalized_blocks.append((block, normalized.splitlines()))
            page_number_lines = self._page_edge_number_positions(
                page_index, normalized_blocks, page_number_patterns
            )
            for block_index, (block, lines) in enumerate(normalized_blocks):
                filtered_lines: list[str] = []
                for line_index, line in enumerate(lines):
                    stripped = line.strip()
                    if not stripped or stripped in repeated:
                        continue
                    if self._looks_like_page_footer(stripped) or (block_index, line_index) in page_number_lines:
                        page_number_lines_removed += 1
                        continue
                    filtered_lines.append(line)
                if not filtered_lines:
                    continue
                joined = "\n".join(filtered_lines)
                # 표 블록의 줄바꿈은 행 경계다. 문장 줄바꿈 복구를 적용하면 "다."로 끝나지
                # 않는 행이 모두 한 줄로 합쳐져 셀이 "금액 가족수당"처럼 섞이고 표 판정도 잃는다.
                text = joined if block.type == "table" else self.repair_line_breaks(joined)
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
            PAGE_NUMBER_LINES_REMOVED_KEY: page_number_lines_removed,
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
        # "- 3 -", "— 3 —", "- 3 페이지 -": a dashed page number is never body text.
        return bool(re.fullmatch(r"[-‐‑–—―]\s*\d+\s*(?:쪽|페이지)?\s*[-‐‑–—―]", line))

    def _page_number_shape(self, line: str) -> str | None:
        """Return the digit-free shape of a short page-number line, else None.

        Recognized shapes: ``3``, ``(3)``, ``[3]``, ``3 / 12``, ``3 of 12``,
        ``Page 3``, ``p. 3``, ``3쪽``, ``3 페이지`` and dashed variants.
        """

        stripped = line.strip()
        if not stripped or len(stripped) > 24 or not re.search(r"\d", stripped):
            return None
        shape = re.sub(r"\s+", " ", re.sub(r"\d+", "#", stripped))
        if re.fullmatch(
            r"(?:[-‐‑–—―] ?)?(?:(?:page|p\.) ?)?[(\[<]?#[)\]>]?(?: ?(?:/|of) ?#)? ?(?:쪽|페이지)?(?: ?[-‐‑–—―])?",
            shape,
            re.IGNORECASE,
        ):
            return shape.casefold()
        return None

    def _page_number_parts(self, line: str) -> tuple[str, int, int | None] | None:
        """Split a page-number line into (shape, page number, total pages).

        ``3 / 12`` and ``3 of 12`` give ``(shape, 3, 12)``; single-number
        shapes give ``(shape, 3, None)``. Anything else gives None.
        """

        shape = self._page_number_shape(line)
        if shape is None:
            return None
        numbers = [int(value) for value in re.findall(r"\d+", line)]
        if not numbers:
            return None
        return shape, numbers[0], numbers[1] if len(numbers) > 1 else None

    def _repeated_page_number_patterns(
        self, parsed: ParsedDocument
    ) -> dict[tuple[str, str], frozenset[tuple[int, int | None]]]:
        """Learn page-number patterns separately for the top and bottom page edge.

        Page numbers change on every page, so exact repetition cannot catch
        them. A pattern is keyed by (edge, digit-free shape) and holds the
        (offset, total) pairs it was learned with, where offset is the printed
        number minus the page's position in the document (numbering may start
        at any value) and total is the ``M`` of ``N / M`` or ``N of M``.

        A pair is learned only when it occurs on the same edge of at least half
        of the pages (minimum three). So a number has to track the page to be
        treated as a page number: a table cell "20" or a carried-over "30" at a
        page edge does not match the page's expected number and is kept, and a
        pattern seen only at page bottoms never removes a top line.
        """

        if len(parsed.pages) < 3:
            return {}
        observed: dict[tuple[str, str], Counter[tuple[int, int | None]]] = {}
        for page_index, page in enumerate(parsed.pages, start=1):
            lines = [line.strip() for block in page.blocks for line in block.text.splitlines() if line.strip()]
            if not lines:
                continue
            for edge, line in zip(PAGE_NUMBER_EDGES, (lines[0], lines[-1])):
                parts = self._page_number_parts(line)
                if parts is None:
                    continue
                shape, number, total = parts
                observed.setdefault((edge, shape), Counter())[(number - page_index, total)] += 1
        threshold = max(3, len(parsed.pages) // 2)
        patterns: dict[tuple[str, str], frozenset[tuple[int, int | None]]] = {}
        for key, counts in observed.items():
            learned = frozenset(pair for pair, count in counts.items() if count >= threshold)
            if learned:
                patterns[key] = learned
        return patterns

    def _page_edge_number_positions(
        self,
        page_index: int,
        normalized_blocks: list[tuple[ParsedBlock, list[str]]],
        patterns: dict[tuple[str, str], frozenset[tuple[int, int | None]]],
    ) -> set[tuple[int, int]]:
        """Return (block, line) positions of this page's edge lines that are its page number.

        The first line is checked only against top-edge patterns and the last
        line only against bottom-edge patterns, and the number must equal
        ``page_index + offset`` (with the same total) for a learned pair.
        """

        if not patterns:
            return set()
        positions = [
            (block_index, line_index)
            for block_index, (_block, lines) in enumerate(normalized_blocks)
            for line_index, line in enumerate(lines)
            if line.strip()
        ]
        if not positions:
            return set()
        found: set[tuple[int, int]] = set()
        for edge, (block_index, line_index) in zip(PAGE_NUMBER_EDGES, (positions[0], positions[-1])):
            parts = self._page_number_parts(normalized_blocks[block_index][1][line_index])
            if parts is None:
                continue
            shape, number, total = parts
            if (number - page_index, total) in patterns.get((edge, shape), frozenset()):
                found.add((block_index, line_index))
        return found
