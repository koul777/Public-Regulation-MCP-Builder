from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


NodeType = Literal[
    "part",
    "chapter",
    "section",
    "subsection",
    "regulation",
    "article",
    "paragraph",
    "item",
    "subitem",
    "appendix",
    "form",
    "supplementary",
    "table",
]


class StructureNode(BaseModel):
    node_id: str
    document_id: str
    node_type: NodeType
    number: str | None = None
    title: str | None = None
    text: str
    page_start: int | None = None
    page_end: int | None = None
    parent_id: str | None = None
    order_index: int
    confidence: float = 1.0
    warnings: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
