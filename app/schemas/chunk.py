from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


ChunkMode = Literal["article", "paragraph", "hybrid"]
ApprovalStatus = Literal["draft", "needs_review", "security_blocked", "approved", "rejected", "superseded"]


class ChunkOptions(BaseModel):
    max_chunk_chars: int = 1800
    min_chunk_chars: int = 300
    overlap_chars: int = 120
    chunk_mode: ChunkMode = "article"
    include_context_header: bool = True
    enable_table_extraction: bool = False
    enable_agent_review: bool = True


class Chunk(BaseModel):
    chunk_id: str
    document_id: str
    source_node_ids: list[str] = Field(default_factory=list)
    chunk_type: str
    text: str
    normalized_text: str | None = None
    retrieval_text: str | None = None
    metadata: dict = Field(default_factory=dict)
    source_page_start: int | None = None
    source_page_end: int | None = None
    confidence: float = 1.0
    warnings: list[str] = Field(default_factory=list)
    approval_status: ApprovalStatus = "draft"
    approval_id: str | None = None
    approved_by: str | None = None
    approved_at: str | None = None
    approved_content_hash: str | None = None
    security_level: str | None = None
    department_acl: list[str] = Field(default_factory=list)

