from __future__ import annotations

from app.agents.base import AgentResult, BaseAgent
from app.processors.validator import Validator
from app.schemas.chunk import Chunk
from app.schemas.structure import StructureNode


class StructureReviewAgent(BaseAgent):
    """Rule-based MVP fallback for the future LLM structure review agent."""

    def run(self, payload: dict) -> AgentResult:
        nodes = [StructureNode.model_validate(item) for item in payload.get("nodes", [])]
        chunks = [Chunk.model_validate(item) for item in payload.get("chunks", [])]
        document_id = payload.get("document_id") or payload.get("document_name") or "document"
        issues = Validator().validate(nodes, chunks, document_id)
        return AgentResult({"issues": [issue.model_dump(mode="json") for issue in issues]})

