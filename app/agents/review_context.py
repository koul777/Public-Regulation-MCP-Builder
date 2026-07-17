from __future__ import annotations

from typing import Any


def review_context_for_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {}
    table_fields = (
        "table_like",
        "table_classification",
        "table_confidence",
        "table_review_reason",
        "table_review_required",
        "table_review_flags",
        "table_column_count",
        "table_structured_row_count",
        "table_probable_false_positive",
        "table_probable_extraction_failed",
        "table_false_positive_stability",
        "kordoc_table_match",
        "kordoc_table_match_review_required",
        "kordoc_table_match_provisional",
        "source_hwpx_parser_review_flags",
        "source_hwpx_nested_table_count",
        "source_hwpx_table_image_count",
        "source_hwpx_table_note_count",
        "source_hwpx_merged_cell_count",
        "source_hwp_extraction_modes",
        "parser_uncertainty_risk_level",
        "parser_uncertainty_flags",
        "parser_uncertainty_recommendation",
    )
    for field in table_fields:
        value = metadata.get(field)
        if value not in (None, "", [], {}):
            context[field] = bounded_value(value)
    rows = metadata.get("table_cell_rows") or metadata.get("table_rows") or []
    if isinstance(rows, list) and rows:
        context["table_row_samples"] = bounded_value(rows[:5])
    kordoc_inventory = metadata.get("kordoc_table_inventory")
    if isinstance(kordoc_inventory, dict):
        context["kordoc_table_inventory"] = {
            "status": kordoc_inventory.get("status"),
            "table_count": kordoc_inventory.get("table_count"),
            "stored_table_count": kordoc_inventory.get("stored_table_count"),
            "tables_truncated": kordoc_inventory.get("tables_truncated"),
            "review_flags": bounded_value(kordoc_inventory.get("review_flags") or []),
            "table_samples": _kordoc_table_samples(kordoc_inventory.get("tables") or []),
        }
    document_inventory = metadata.get("document_inventory")
    if isinstance(document_inventory, dict):
        context["document_inventory"] = bounded_value(document_inventory)
    return context


def _kordoc_table_samples(tables: Any) -> list[dict[str, Any]]:
    if not isinstance(tables, list):
        return []
    samples = []
    for table in tables[:3]:
        if not isinstance(table, dict):
            continue
        samples.append(
            {
                "table_index": table.get("table_index"),
                "title": table.get("title"),
                "source_page": table.get("source_page"),
                "row_count": table.get("row_count"),
                "column_count": table.get("column_count"),
                "cell_count": table.get("cell_count"),
                "merged_cell_count": table.get("merged_cell_count"),
                "nested_table_count": table.get("nested_table_count"),
                "row_samples": bounded_value((table.get("cell_rows") or [])[:2]),
            }
        )
    return samples


def bounded_value(value: Any, *, max_string_chars: int = 500) -> Any:
    if isinstance(value, str):
        return value[:max_string_chars]
    if isinstance(value, list):
        return [bounded_value(item, max_string_chars=max_string_chars) for item in value[:20]]
    if isinstance(value, dict):
        return {
            str(key): bounded_value(item, max_string_chars=max_string_chars)
            for key, item in list(value.items())[:50]
        }
    return value
