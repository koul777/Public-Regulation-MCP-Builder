from __future__ import annotations

import re

from app.agents.base import AgentResult, BaseAgent


class MetadataAgent(BaseAgent):
    def run(self, payload: dict) -> AgentResult:
        text = str(payload.get("text", ""))
        dates = re.findall(r"\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.?", text)
        return AgentResult(
            {
                "effective_date": dates[0] if dates else None,
                "revision_date": dates[-1] if len(dates) > 1 else None,
                "date_candidates": dates,
            }
        )

