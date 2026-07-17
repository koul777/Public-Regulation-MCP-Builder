from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ParsedBlock(BaseModel):
    type: Literal["text", "table", "image"] = "text"
    text: str
    bbox: tuple[float, float, float, float] | None = None
    metadata: dict = Field(default_factory=dict)


class ParsedPage(BaseModel):
    page_no: int
    blocks: list[ParsedBlock] = Field(default_factory=list)


class ParsedDocument(BaseModel):
    document_id: str
    source_file: str
    document_name: str | None = None
    file_type: str
    pages: list[ParsedPage] = Field(default_factory=list)
    raw_text: str = ""
    metadata: dict = Field(default_factory=dict)

    @property
    def page_count(self) -> int:
        return len(self.pages)

