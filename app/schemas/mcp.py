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
