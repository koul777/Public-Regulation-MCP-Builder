from __future__ import annotations

from pathlib import Path
import unittest

from frontend import streamlit_app


class StreamlitOrchestrationExplanationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (Path(__file__).parents[1] / "frontend" / "streamlit_app.py").read_text(
            encoding="utf-8"
        )

    def test_beginner_mode_explains_both_complete_pipelines(self) -> None:
        self.assertIn("def _render_beginner_orchestration_explanation", self.source)
        self.assertIn('"regulation_preprocessing_v1": "① 문서 전처리·승인·색인"', self.source)
        self.assertIn('"local_regulation_qa_v1": "② 질문 분석·근거 답변"', self.source)
        self.assertIn('workflow_roles("release_and_mcp_handoff")', self.source)
        self.assertIn("③ 검증 결과를 릴리스하고 MCP 연결 준비", self.source)
        self.assertIn("stage.get(\"agent_roles\")", self.source)
        self.assertIn("role.get(\"purpose\")", self.source)
        self.assertIn("failure_policy", self.source)
        self.assertIn("_BEGINNER_PIPELINE_FIELD_LABELS", self.source)
        self.assertIn("받는 것:", self.source)
        self.assertIn("만드는 것:", self.source)

    def test_sidebar_renders_the_explanation_in_beginner_mode(self) -> None:
        self.assertIn(
            "_render_beginner_orchestration_explanation(nav_page=current_nav_page)",
            self.source,
        )
        self.assertIn("AI는 초안·검색어·검수 의견만 만들 수 있습니다", self.source)

    def test_beginner_results_can_show_actual_role_statuses(self) -> None:
        self.assertIn("def _render_actual_pipeline_role_trace", self.source)
        self.assertIn("agent_role_statuses", self.source)
        self.assertIn("이번 문서에서 실제로 수행된 역할 보기", self.source)
        self.assertIn("_AGENT_TRACE_NEXT_ACTIONS", self.source)
        self.assertIn("awaiting_human_approval", self.source)
        self.assertIn("_agent_trace_next_action(role)", self.source)
        self.assertIn('role.get("purpose")', self.source)
        self.assertIn("담당:", self.source)
        self.assertIn('"verified": "검증 완료"', self.source)
        self.assertIn('"unavailable": "사용 불가"', self.source)
        self.assertIn("다음:", self.source)
        self.assertIn("_render_actual_pipeline_role_trace(ctx)", self.source)

    def test_release_role_tuple_fields_are_explained(self) -> None:
        rendered = streamlit_app._beginner_pipeline_fields(("quality_metrics", "blockers"))

        self.assertIn("품질 측정 결과", rendered)
        self.assertIn("출시를 막는 문제 목록", rendered)


if __name__ == "__main__":
    unittest.main()
