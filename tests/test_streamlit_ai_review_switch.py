from __future__ import annotations

import unittest

from app.storage.repository import JsonRepository
from tests.test_ai_review_ui_workflow import AIReviewUIFixture


class StreamlitAIReviewSwitchTests(AIReviewUIFixture):
    """사이드바 'AI 검수 사용' 스위치는 저장 버튼 없이도 실제 설정에 반영돼야 한다."""

    def test_toggle_alone_enables_review_when_connection_is_already_configured(self) -> None:
        # .env 등으로 공급자·모델·주소가 이미 있으면 스위치만 켜도 전처리에 AI 검수가 붙어야 한다.
        self.app.session_state["ai_connection_overrides"].update({
            "llm_provider": "openai-compatible",
            "agent_review_model": "synthetic-review",
            "agent_review_api_base_url": self.endpoint,
        })
        self._run()
        self.app.toggle(key="sidebar-ai-review-enabled").set_value(True).run()
        self.assertFalse(self.app.exception)
        self.assertTrue(self.app.session_state["ai_connection_overrides"]["enable_agent_review"])
        document_id = self._process()
        summary = JsonRepository(self.settings).list_runs(document_id)[-1].stats["agent_review"]
        self.assertTrue(summary["request_enabled"])
        self.assertNotEqual("agent_review_not_requested", summary.get("skip_reason"))
        self.assertTrue(self.requests)

        self._run()
        self.app.toggle(key="sidebar-ai-review-enabled").set_value(False).run()
        self.assertFalse(self.app.exception)
        self.assertFalse(self.app.session_state["ai_connection_overrides"]["enable_agent_review"])


if __name__ == "__main__":
    unittest.main()
