from __future__ import annotations

from app.agents.base import AgentResult, BaseAgent


class TableNarrationAgent(BaseAgent):
    """Produces conservative table summaries without fabricating missing values."""

    def run(self, payload: dict) -> AgentResult:
        table_text = str(payload.get("table_text", "")).strip()
        rows = [row for row in table_text.splitlines() if row.strip()]
        summary = f"표는 {len(rows)}개 행으로 구성되어 있습니다." if rows else "표 텍스트가 비어 있습니다."
        return AgentResult({"table_summary": summary, "raw_table_text": table_text})

