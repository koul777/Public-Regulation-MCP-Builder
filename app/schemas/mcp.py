from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ChatGPTDataSearchResult(BaseModel):
    """OpenAI data-source search result contract."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    url: str


class ChatGPTDataSearchOutput(BaseModel):
    """Exact structured output exposed by the ChatGPT data profile."""

    model_config = ConfigDict(extra="forbid")

    results: list[ChatGPTDataSearchResult]


class ChatGPTDataFetchOutput(BaseModel):
    """Exact structured output for fetching one ChatGPT search result."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    text: str
    url: str
    metadata: dict[str, str] = Field(default_factory=dict)


class ChatGPTDataRegulation(BaseModel):
    """One unique approved regulation entry for the ChatGPT data profile."""

    model_config = ConfigDict(extra="forbid")

    regulation_title: str
    regulation_category: str = ""
    regulation_no: str = ""
    regulation_unit_id: str = ""
    version: str = ""
    revision_date: str = ""
    effective_from: str = ""
    effective_to: str = ""
    status: str


class ChatGPTDataRegulationListOutput(BaseModel):
    """Paginated regulation catalog output for the ChatGPT data profile."""

    model_config = ConfigDict(extra="forbid")

    regulations: list[ChatGPTDataRegulation]
    total_count: int
    page: int
    page_size: int
    next_cursor: str | None = None


class ChatGPTDataRegulationArticleOutput(BaseModel):
    """Exact approved article evidence for one catalog regulation."""

    model_config = ConfigDict(extra="forbid")

    regulation_unit_id: str
    article_no: str
    articles: list[ChatGPTDataFetchOutput]
