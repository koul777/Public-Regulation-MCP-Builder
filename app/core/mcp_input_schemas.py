"""Pydantic-aware schema aliases for MCP and API protocol boundaries."""

from __future__ import annotations

from typing import Annotated, TypeAlias

from pydantic import Field

from app.core.input_limits import (
    MAX_MCP_ARTICLE_NO_CHARS,
    MAX_MCP_DEPARTMENT_IDS,
    MAX_MCP_IDENTIFIER_CHARS,
    MAX_MCP_PAGE,
    MAX_MCP_PAGE_SIZE,
    MAX_MCP_QUERY_CHARS,
    MAX_MCP_RESULT_ID_CHARS,
    MAX_MCP_SCOPE_VALUE_CHARS,
    MAX_MCP_SECURITY_LEVELS,
    MAX_MCP_TOP_K,
)


__all__ = [
    "McpArticleNo",
    "McpDepartmentIds",
    "McpIdentifier",
    "McpOptionalIdentifier",
    "McpPage",
    "McpPageSize",
    "McpQuery",
    "McpResultId",
    "McpScopeValue",
    "McpSecurityLevels",
    "McpTopK",
]


# These aliases belong at Pydantic-aware protocol boundaries. Keep the shared
# limits and direct-call validators in input_limits so service-only imports do
# not pay the Pydantic cold-import cost.
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
McpPage: TypeAlias = Annotated[int, Field(ge=1, le=MAX_MCP_PAGE)]
McpPageSize: TypeAlias = Annotated[int, Field(ge=1, le=MAX_MCP_PAGE_SIZE)]
