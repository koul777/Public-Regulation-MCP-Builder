from __future__ import annotations

import math
from typing import Annotated, Any, TypeAlias

from pydantic import Field


# Review requests may legitimately cover an institution-wide approval batch.  The
# current release evidence contains roughly 6,000 chunks, so this ceiling leaves
# substantial headroom while preventing an unbounded list from reaching storage
# and vector synchronization code.
MAX_REVIEW_CHUNK_IDS = 20_000
MAX_REVIEW_DECISION_EVENTS = 100_000
MAX_REVIEW_DEPARTMENT_IDS = 1_000
MAX_SPLIT_PARTS = 1_000

MAX_IDENTIFIER_CHARS = 1_024
MAX_SHORT_LABEL_CHARS = 256
MAX_ARTIFACT_PATH_CHARS = 4_096
MAX_NOTE_CHARS = 4_000
MAX_REVIEW_TEXT_CHARS = 4_000_000
MAX_REVIEW_TEXT_TOTAL_CHARS = 8_000_000
MAX_METADATA_PATCH_KEYS = 1_000

MAX_REVIEW_EVENT_JSON_ITEMS = 500_000
MAX_REVIEW_EVENT_JSON_TEXT_CHARS = 16_000_000
MAX_METADATA_PATCH_JSON_ITEMS = 50_000
MAX_METADATA_PATCH_JSON_TEXT_CHARS = 2_000_000
MAX_REQUEST_JSON_DEPTH = 16
MAX_REQUEST_JSON_KEY_CHARS = 256

MAX_MCP_QUERY_CHARS = 2_000
MAX_MCP_RESULT_ID_CHARS = 2_048
MAX_MCP_IDENTIFIER_CHARS = 1_024
MAX_MCP_ARTICLE_NO_CHARS = 256
MAX_MCP_SECURITY_LEVELS = 4
MAX_MCP_DEPARTMENT_IDS = 256
MAX_MCP_SCOPE_VALUE_CHARS = 256
MAX_MCP_TOP_K = 20


McpQuery: TypeAlias = Annotated[
    str,
    Field(min_length=1, max_length=MAX_MCP_QUERY_CHARS),
]
McpResultId: TypeAlias = Annotated[
    str,
    Field(min_length=1, max_length=MAX_MCP_RESULT_ID_CHARS),
]
McpIdentifier: TypeAlias = Annotated[
    str,
    Field(min_length=1, max_length=MAX_MCP_IDENTIFIER_CHARS),
]
McpOptionalIdentifier: TypeAlias = Annotated[
    str,
    Field(max_length=MAX_MCP_IDENTIFIER_CHARS),
]
McpArticleNo: TypeAlias = Annotated[
    str,
    Field(min_length=1, max_length=MAX_MCP_ARTICLE_NO_CHARS),
]
McpScopeValue: TypeAlias = Annotated[
    str,
    Field(min_length=1, max_length=MAX_MCP_SCOPE_VALUE_CHARS),
]
McpSecurityLevels: TypeAlias = Annotated[
    list[McpScopeValue],
    Field(max_length=MAX_MCP_SECURITY_LEVELS),
]
McpDepartmentIds: TypeAlias = Annotated[
    list[McpScopeValue],
    Field(max_length=MAX_MCP_DEPARTMENT_IDS),
]
McpTopK: TypeAlias = Annotated[int, Field(ge=1, le=MAX_MCP_TOP_K)]


def require_bounded_text(
    value: object,
    *,
    field_name: str,
    max_chars: int,
    required: bool = True,
) -> str:
    """Normalize an internal tool value and enforce the public input budget.

    FastMCP validates the same limits at the protocol boundary.  This helper
    keeps direct service calls fail-closed as well, which is important for tests,
    local integrations, and future transports that may bypass FastMCP.
    """

    normalized = str(value or "").strip()
    if required and not normalized:
        raise ValueError(f"{field_name} is required.")
    if len(normalized) > max_chars:
        raise ValueError(f"{field_name} must contain at most {max_chars} characters.")
    return normalized


def validate_json_value_budget(
    value: Any,
    *,
    field_name: str,
    max_items: int,
    max_text_chars: int,
    max_depth: int = MAX_REQUEST_JSON_DEPTH,
    max_key_chars: int = MAX_REQUEST_JSON_KEY_CHARS,
) -> Any:
    """Reject deeply nested or oversized JSON-compatible request values.

    Collection length constraints alone do not constrain a single dictionary
    value containing a very large nested list or string.  This iterative walk
    applies a total budget without recursion and also rejects cyclic containers
    supplied by direct Python callers.
    """

    stack: list[tuple[Any, int, bool]] = [(value, 0, False)]
    active_containers: set[int] = set()
    item_count = 0
    text_chars = 0

    while stack:
        current, depth, leaving = stack.pop()
        if leaving:
            active_containers.discard(id(current))
            continue
        if depth > max_depth:
            raise ValueError(f"{field_name} exceeds the maximum JSON depth of {max_depth}.")

        if isinstance(current, dict):
            identity = id(current)
            if identity in active_containers:
                raise ValueError(f"{field_name} must not contain recursive containers.")
            active_containers.add(identity)
            stack.append((current, depth, True))
            item_count += len(current)
            if item_count > max_items:
                raise ValueError(f"{field_name} exceeds the maximum JSON item count of {max_items}.")
            for key, child in current.items():
                if not isinstance(key, str):
                    raise ValueError(f"{field_name} JSON object keys must be strings.")
                key_text = key
                if len(key_text) > max_key_chars:
                    raise ValueError(
                        f"{field_name} JSON keys must contain at most {max_key_chars} characters."
                    )
                text_chars += len(key_text)
                if text_chars > max_text_chars:
                    raise ValueError(
                        f"{field_name} exceeds the maximum JSON text budget of "
                        f"{max_text_chars} characters."
                    )
                stack.append((child, depth + 1, False))
        elif isinstance(current, (list, tuple)):
            identity = id(current)
            if identity in active_containers:
                raise ValueError(f"{field_name} must not contain recursive containers.")
            active_containers.add(identity)
            stack.append((current, depth, True))
            item_count += len(current)
            if item_count > max_items:
                raise ValueError(f"{field_name} exceeds the maximum JSON item count of {max_items}.")
            stack.extend((child, depth + 1, False) for child in current)
        elif isinstance(current, str):
            text_chars += len(current)
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError(f"{field_name} must not contain non-finite numbers.")
        elif current is None or isinstance(current, (bool, int)):
            pass
        else:
            raise ValueError(f"{field_name} must contain JSON-compatible values only.")

        if item_count > max_items:
            raise ValueError(f"{field_name} exceeds the maximum JSON item count of {max_items}.")
        if text_chars > max_text_chars:
            raise ValueError(
                f"{field_name} exceeds the maximum JSON text budget of {max_text_chars} characters."
            )

    return value
