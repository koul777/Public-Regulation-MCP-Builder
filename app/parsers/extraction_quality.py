from __future__ import annotations

"""Post-parse extraction coverage checks.

Native parsers are intentionally conservative: they may return usable text
while also flagging image-only pages, embedded images, or uncertain layout. This
module turns those signals into a small, path-free quality contract that can be
stored in the processing run and shown to an operator before approval.
"""

from typing import Any

from app.schemas.parsed import ParsedDocument


EXTRACTION_QUALITY_KEY = "extraction_quality"


def build_extraction_quality_report(parsed: ParsedDocument) -> dict[str, Any]:
    page_count = parsed.page_count
    pages_with_blocks = 0
    pages_with_text = 0
    text_block_count = 0
    table_block_count = 0
    image_block_count = 0
    nonempty_block_count = 0
    empty_page_numbers: list[int] = []
    image_page_numbers: set[int] = set()
    table_page_numbers: set[int] = set()

    for page in parsed.pages:
        nonempty_blocks = [block for block in page.blocks if str(block.text or "").strip()]
        if nonempty_blocks:
            pages_with_blocks += 1
        else:
            empty_page_numbers.append(int(page.page_no))
        page_has_text = False
        for block in nonempty_blocks:
            nonempty_block_count += 1
            if block.type == "text":
                text_block_count += 1
                page_has_text = True
            elif block.type == "table":
                table_block_count += 1
                table_page_numbers.add(int(page.page_no))
            elif block.type == "image":
                image_block_count += 1
                image_page_numbers.add(int(page.page_no))
        if page_has_text:
            pages_with_text += 1

    metadata = parsed.metadata if isinstance(parsed.metadata, dict) else {}
    parser_flags = _string_list(
        metadata.get("parser_uncertainty_flags")
        or (metadata.get("parser_uncertainty") or {}).get("flags")
    )
    parser_risk = str(
        metadata.get("parser_uncertainty_risk_level")
        or (metadata.get("parser_uncertainty") or {}).get("risk_level")
        or "low"
    ).strip().lower()
    if parser_risk not in {"low", "medium", "high", "critical"}:
        parser_risk = "medium"

    embedded_image_pages = _positive_int_list(metadata.get("pdf_embedded_image_pages"))
    missing_content_pages = _positive_int_list(metadata.get("missing_content_pages"))
    blank_pages = _positive_int_list(metadata.get("blank_pages"))
    image_page_numbers.update(embedded_image_pages)

    review_reasons: list[str] = []
    if missing_content_pages:
        review_reasons.append("image_only_or_missing_text_pages")
    if embedded_image_pages:
        review_reasons.append("embedded_images_detected")
    if image_block_count:
        review_reasons.append("image_blocks_detected")
    if table_block_count or metadata.get("pdf_table_regions"):
        review_reasons.append("table_content_detected")
    if parser_flags:
        review_reasons.append("parser_uncertainty_flags_present")
    if parser_risk in {"high", "critical"}:
        review_reasons.append("high_parser_uncertainty")

    if page_count <= 0 or not str(parsed.raw_text or "").strip():
        status = "blocked"
        ready_for_normalization = False
        blocking_reasons = ["no_pages_or_text_extracted"]
    else:
        status = "review_required" if review_reasons else "pass"
        ready_for_normalization = True
        blocking_reasons = []

    return {
        "schema_version": "reg-rag-extraction-quality-v1",
        "status": status,
        "ready_for_normalization": ready_for_normalization,
        "review_required": bool(review_reasons),
        "blocking_reasons": blocking_reasons,
        "review_reasons": sorted(set(review_reasons)),
        "page_count": page_count,
        "pages_with_blocks": pages_with_blocks,
        "pages_with_text": pages_with_text,
        "page_coverage_ratio": _ratio(pages_with_blocks, page_count),
        "text_page_coverage_ratio": _ratio(pages_with_text, page_count),
        "text_block_count": text_block_count,
        "table_block_count": table_block_count,
        "image_block_count": image_block_count,
        "nonempty_block_count": nonempty_block_count,
        "raw_text_chars": len(str(parsed.raw_text or "")),
        "empty_page_numbers": empty_page_numbers[:200],
        "missing_content_page_numbers": missing_content_pages[:200],
        "embedded_image_page_numbers": embedded_image_pages[:200],
        "table_page_numbers": sorted(table_page_numbers)[:200],
        "image_page_numbers": sorted(image_page_numbers)[:200],
        "parser_uncertainty_risk_level": parser_risk,
        "parser_uncertainty_flags": parser_flags,
    }


def _positive_int_list(value: object) -> list[int]:
    if not isinstance(value, (list, tuple, set)):
        return []
    values: list[int] = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in values:
            values.append(number)
    return sorted(values)


def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return sorted({str(item).strip() for item in value if str(item).strip()})


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(max(0.0, min(1.0, numerator / denominator)), 4)
