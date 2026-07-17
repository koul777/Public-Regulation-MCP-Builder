from __future__ import annotations

import math
import re
from typing import Any

from app.schemas.chunk import Chunk


DOTTED_LEADER_PATTERN = re.compile(r"(\.{3,}|·{3,}|…{2,})")
ARTICLE_TEXT_PATTERN = re.compile(r"(제\s*\d+\s*조|제\s*조|<\s*(개정|신설|삭제)|shall|must)", re.IGNORECASE)
TOKEN_PATTERN = re.compile(r"[0-9A-Za-z\uac00-\ud7a3]{2,}")
MERGE_CANDIDATE_LABELS = {"structured_table_candidate", "possible_table_candidate"}
HEADER_WORDS = {
    "category",
    "type",
    "item",
    "standard",
    "criteria",
    "amount",
    "rate",
    "date",
    "name",
    "position",
    "grade",
    "division",
    "description",
    "구분",
    "항목",
    "기준",
    "지급",
    "금액",
    "비율",
    "직급",
    "대상",
    "내용",
    "명칭",
    "위치",
    # Korean regulation-table headers frequently used in AKS appendices and
    # forms.  Without these, valid Kordoc grids are downgraded to
    # ``needs_ai_review`` and never become the main table source.
    "자격",
    "경력",
    "환산율",
    "보존기간",
    "해설",
    "성명",
    "소속",
    "직위",
    "기간",
    "겸직기관",
    "겸직직위",
    "비고",
    "심사",
    "배점",
    "의견",
    "분류번호",
    "연도",
    "만료일",
    "월정직책급",
    "특정직무급",
    "가족수당",
    "계정과목",
}
MATCH_TEXT_KEY = "_codex_match_text"
MATCH_TOKENS_KEY = "_codex_match_tokens"
MATCH_NUMBERS_KEY = "_codex_match_numbers"


def attach_kordoc_table_matches(chunks: list[Chunk], inventory: dict[str, Any] | None) -> None:
    """Attach review-only Kordoc match hints to local table-like chunks."""
    if not chunks or not isinstance(inventory, dict):
        return
    tables = mergeable_kordoc_tables(inventory)
    if not tables:
        return
    tables = prepare_kordoc_table_match_index(tables)
    for chunk in chunks:
        metadata = chunk.metadata or {}
        if not metadata.get("table_like") or metadata.get("table_cell_rows"):
            continue
        table, score, match_label = best_kordoc_match(chunk, tables)
        if not table or match_label == "no_confident_match":
            continue
        updated = dict(metadata)
        updated["kordoc_table_match"] = kordoc_match_summary(table, score=score, match_label=match_label)
        updated["kordoc_table_match_review_required"] = True
        updated["kordoc_table_match_provisional"] = True
        chunk.metadata = updated


def mergeable_kordoc_tables(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    tables = []
    for table in inventory.get("tables") or []:
        if not isinstance(table, dict):
            continue
        label, reason, action = triage_kordoc_table(table)
        if label not in MERGE_CANDIDATE_LABELS:
            continue
        table = dict(table)
        table["codex_triage_label"] = label
        table["codex_triage_reason"] = reason
        table["recommended_action"] = action
        tables.append(table)
    return tables


def prepare_kordoc_table_match_index(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cache text/token features for repeated Kordoc-to-local table matching."""
    prepared: list[dict[str, Any]] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        item = dict(table)
        text = table_text(item, max_rows=8)
        item[MATCH_TEXT_KEY] = text
        item[MATCH_TOKENS_KEY] = tokenize(text)
        item[MATCH_NUMBERS_KEY] = numeric_tokens(text)
        prepared.append(item)
    return prepared


def best_kordoc_match(chunk: Chunk, tables: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float, str]:
    local_text = chunk.normalized_text or chunk.text or ""
    local_tokens = tokenize(local_text)
    local_numbers = numeric_tokens(local_text)
    if not local_tokens:
        return None, 0.0, "no_confident_match"
    best_table: dict[str, Any] | None = None
    best_score = 0.0
    for table in tables:
        same_page = chunk.source_page_start is not None and table.get("source_page") == chunk.source_page_start
        kordoc_tokens, kordoc_numbers = _prepared_match_features(table)
        if not kordoc_tokens:
            continue
        score = _match_score_from_features(local_tokens, local_numbers, kordoc_tokens, kordoc_numbers, same_page=same_page)
        if score > best_score:
            best_table = table
            best_score = score
    match_label = classify_match(best_score, str((best_table or {}).get("codex_triage_label") or ""))
    return best_table, best_score, match_label


def kordoc_match_summary(table: dict[str, Any], *, score: float, match_label: str) -> dict[str, Any]:
    return {
        "match_label": match_label,
        "match_strength": match_label,
        "match_score": score,
        "kordoc_triage_label": table.get("codex_triage_label"),
        "kordoc_triage_reason": table.get("codex_triage_reason"),
        "table_index": table.get("table_index"),
        "title": table.get("title"),
        "source_page": table.get("source_page"),
        "row_count": table.get("row_count"),
        "column_count": table.get("column_count"),
        "cell_count": table.get("cell_count"),
        "merged_cell_count": table.get("merged_cell_count"),
        "nested_table_count": table.get("nested_table_count"),
        "row_samples": _bounded_value((table.get("cell_rows") or [])[:3]),
    }


def triage_kordoc_table(table: dict[str, Any]) -> tuple[str, str, str]:
    row_count = safe_int(table.get("row_count"))
    column_count = safe_int(table.get("column_count"))
    cell_count = safe_int(table.get("cell_count"))
    sample = table_text(table, max_rows=6)
    lower = sample.lower()
    average_cell_chars = len(sample) / max(cell_count, 1)
    if DOTTED_LEADER_PATTERN.search(sample):
        return (
            "probable_toc_table",
            "Dotted leaders/page-number layout suggests table-of-contents extraction, not a regulation table.",
            "Do not merge into table chunks; keep as source-navigation evidence only.",
        )
    if row_count <= 1 and column_count <= 1:
        return (
            "weak_single_cell_signal",
            "Single-cell Kordoc table signal is too weak to upgrade local table structure.",
            "Use only as an AI review hint.",
        )
    if ARTICLE_TEXT_PATTERN.search(sample) and (average_cell_chars >= 45 or row_count <= 5):
        return (
            "probable_prose_false_positive",
            "Article or amendment prose appears split into cells.",
            "Do not auto-merge; send to AI/human review as a possible false positive.",
        )
    if column_count >= 2 and row_count >= 2 and header_score(table) >= 1:
        return (
            "structured_table_candidate",
            "Multi-row, multi-column signal with header-like cells.",
            "Candidate for Kordoc-assisted merge, still review_required until approved.",
        )
    if column_count >= 4 and row_count >= 2 and average_cell_chars < 35 and not ARTICLE_TEXT_PATTERN.search(sample):
        return (
            "possible_table_candidate",
            "Short multi-column signal with compact cells.",
            "Candidate for AI comparison against local table chunk before merge.",
        )
    if column_count >= 2 and row_count >= 3 and average_cell_chars < 60:
        return (
            "possible_table_candidate",
            "Multi-row, multi-column signal with compact cells.",
            "Candidate for AI comparison against local table chunk before merge.",
        )
    if "appendix" in lower or "form" in lower:
        return (
            "attachment_candidate",
            "Attachment/form wording appears in the table sample.",
            "Compare with appendix/form boundaries before merge.",
        )
    return (
        "needs_ai_review",
        "Kordoc table signal is not strong enough for automatic classification.",
        "Review source span or compare with local chunk before use.",
    )


def table_text(table: dict[str, Any], *, max_rows: int = 5) -> str:
    rows = []
    # Kordoc's normalized inventory usually exposes ``cell_rows``.  Keep the
    # geometry-preserving ``grid_rows`` as a valid matching source as well so
    # a table does not lose Kordoc-main eligibility merely because the caller
    # retained only the dense grid representation.
    source_rows = table.get("cell_rows") or table.get("grid_rows") or []
    for row in source_rows:
        if not isinstance(row, dict):
            continue
        raw = str(row.get("raw") or " | ".join(str(cell) for cell in row.get("cells") or [])).strip()
        if raw:
            rows.append(raw)
        if len(rows) >= max_rows:
            break
    return " / ".join(rows)


def header_score(table: dict[str, Any]) -> int:
    source_rows = table.get("cell_rows") or table.get("grid_rows") or []
    rows = [row for row in source_rows if isinstance(row, dict)]
    if not rows:
        return 0
    cells = [str(cell).strip() for cell in rows[0].get("cells") or [] if str(cell).strip()]
    score = 0
    for cell in cells:
        compact = re.sub(r"\s+", "", cell.lower())
        if len(compact) <= 20 and any(word in compact for word in HEADER_WORDS):
            score += 1
    return score


def tokenize(value: str) -> set[str]:
    text = re.sub(r"\s+", " ", value.lower())
    tokens = set(TOKEN_PATTERN.findall(text))
    compact = re.sub(r"[^0-9a-z\uac00-\ud7a3]+", "", text)
    if len(compact) >= 6:
        tokens.update(compact[index : index + 3] for index in range(0, len(compact) - 2))
    return {token for token in tokens if len(token) >= 2}


def numeric_tokens(value: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", value))


def match_score(local_text: str, kordoc_text: str, *, same_page: bool = False) -> float:
    local_tokens = tokenize(local_text)
    kordoc_tokens = tokenize(kordoc_text)
    if not local_tokens or not kordoc_tokens:
        return 0.0
    return _match_score_from_features(
        local_tokens,
        numeric_tokens(local_text),
        kordoc_tokens,
        numeric_tokens(kordoc_text),
        same_page=same_page,
    )


def _prepared_match_features(table: dict[str, Any]) -> tuple[set[str], set[str]]:
    tokens = table.get(MATCH_TOKENS_KEY)
    numbers = table.get(MATCH_NUMBERS_KEY)
    if isinstance(tokens, set) and isinstance(numbers, set):
        return tokens, numbers
    text = str(table.get(MATCH_TEXT_KEY) or table_text(table, max_rows=8))
    return tokenize(text), numeric_tokens(text)


def _match_score_from_features(
    local_tokens: set[str],
    local_numbers: set[str],
    kordoc_tokens: set[str],
    kordoc_numbers: set[str],
    *,
    same_page: bool = False,
) -> float:
    if not local_tokens or not kordoc_tokens:
        return 0.0
    overlap = len(local_tokens & kordoc_tokens)
    cosine = overlap / math.sqrt(len(local_tokens) * len(kordoc_tokens))
    number_bonus = 0.0
    if local_numbers and kordoc_numbers:
        number_bonus = min(0.2, len(local_numbers & kordoc_numbers) / max(len(local_numbers), len(kordoc_numbers)))
    page_bonus = 0.1 if same_page else 0.0
    return round(min(1.0, cosine + number_bonus + page_bonus) * 100, 2)


def classify_match(score: float, kordoc_label: str) -> str:
    if score >= 45 and kordoc_label == "structured_table_candidate":
        return "strong_review_match"
    if score >= 30:
        return "medium_review_match"
    if score >= 18:
        return "weak_review_match"
    return "no_confident_match"


def safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _bounded_value(value: Any, *, max_string_chars: int = 500) -> Any:
    if isinstance(value, str):
        return value[:max_string_chars]
    if isinstance(value, list):
        return [_bounded_value(item, max_string_chars=max_string_chars) for item in value[:20]]
    if isinstance(value, dict):
        return {str(key): _bounded_value(item, max_string_chars=max_string_chars) for key, item in list(value.items())[:50]}
    return value
