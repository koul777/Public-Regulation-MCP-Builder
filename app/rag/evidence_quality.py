from __future__ import annotations

"""Preserve approved table-quality signals without treating them as new approval."""

from collections.abc import Mapping
from typing import Any


def table_review_metadata(item: Any) -> dict[str, Any]:
    """Read typed quality fields from a public result or context evidence.

    Callers remain responsible for approval and scope verification. In particular,
    this helper must not be used to authorize arbitrary repository metadata.
    """
    if isinstance(item, Mapping):
        sources = [item]
        nested = item.get("metadata")
        if isinstance(nested, Mapping):
            sources.append(nested)
        values = [
            (source.get("table_like"), source.get("table_review_required"), source.get("table_review_flags"))
            for source in sources
        ]
    else:
        values = [(getattr(item, "table_like", None), getattr(item, "table_review_required", None),
                   getattr(item, "table_review_flags", None))]
    flags: list[str] = []
    for _table_like, _required, raw_flags in values:
        if not isinstance(raw_flags, (list, tuple)):
            continue
        for flag in raw_flags:
            if isinstance(flag, str) and flag and flag not in flags:
                flags.append(flag)
    return {
        "table_like": any(value[0] is True for value in values),
        "table_review_required": any(value[1] is True for value in values),
        "table_review_flags": flags,
    }


def requires_table_structure_review(item: Any) -> bool:
    """Identify the demonstrated unresolved column-relationship condition.

    Other review flags remain visible but do not broaden this answer gate.
    Absence of these flags is not a claim that table geometry was verified.
    """
    quality = table_review_metadata(item)
    return quality["table_review_required"] and "unstable_column_count" in quality["table_review_flags"]
