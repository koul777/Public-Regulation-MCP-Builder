"""재시작 뒤 AI 검수 설정 복원과 초보자 모드의 '한 번에 승인' 선택지를 실제 화면으로 고정한다."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from streamlit.testing.v1 import AppTest
except ImportError:  # pragma: no cover - optional operator UI dependency
    AppTest = None

from app.core import config as config_module
from app.core.ai_review_preferences import load_ai_review_preferences, save_ai_review_preferences
from app.core.config import Settings
from app.storage.repository import JsonRepository
from tests.test_streamlit_approval_app import (
    _seed_app_institution_context,
    _seed_streamlit_approval_document,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = REPO_ROOT / "frontend" / "streamlit_app.py"
FINAL_BUTTONS = {"이 규정 최종 확정 · 승인하고 색인", "승인하고 AI 질문 준비하기"}


@unittest.skipIf(AppTest is None, "Streamlit AppTest is unavailable")
class AIReviewPreferencesRestoreUITests(unittest.TestCase):
    def _app(self, root: Path, *, seed_prefs: bool) -> tuple[AppTest, Settings]:
        settings = Settings(
            app_env="test",
            data_dir=root / "data",
            artifact_root=root,
            api_auth_required=False,
            tenant_storage_isolation=False,
            enable_agent_review=False,
            llm_provider="openai",
            agent_review_model="",
            openai_api_key="",
        )
        _seed_streamlit_approval_document(settings)
        if seed_prefs:
            save_ai_review_preferences(
                settings.data_dir,
                {
                    "enable_agent_review": True,
                    "llm_provider": "openai",
                    "agent_review_model": "gpt-4.1-mini",
                    "agent_review_max_chunks_per_document": 0,
                    "agent_review_max_input_tokens_per_document": 0,
                    "openai_api_key": "must-not-be-saved",
                },
            )
        self.enterContext(patch.object(config_module, "_runtime_overrides", {}))
        self.enterContext(patch.object(config_module, "_base_settings", return_value=settings))
        app = AppTest.from_file(str(APP_PATH), default_timeout=180)
        _seed_app_institution_context(app)
        app.session_state["beginner_guide_enabled"] = False
        app.session_state["beginner_guide_choice_made"] = True
        app.session_state["nav_page"] = "① 문서 올려서 전처리"
        return app, settings

    def test_saved_switch_is_restored_and_asks_only_for_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app, settings = self._app(Path(tmp), seed_prefs=True)
            app.run()
            self.assertFalse(app.exception)
            restored = app.session_state["ai_connection_overrides"]
            self.assertTrue(restored["enable_agent_review"])
            self.assertEqual("gpt-4.1-mini", restored["agent_review_model"])
            self.assertNotIn("openai_api_key", restored)
            # 켜져 있는데 키가 없으면 '꺼짐'이 아니라 '설정 필요'로 드러나야 한다.
            self.assertTrue(any(e.label.startswith("AI 검수 · 설정 필요") for e in app.expander))
            self.assertTrue(any("지난 실행에서 켜 둔 AI 검수 설정" in w.value for w in app.warning))
            self.assertTrue(any("API 키가 비어 있습니다" in w.value for w in app.warning))

    def test_saving_in_the_sidebar_writes_preferences_without_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app, settings = self._app(Path(tmp), seed_prefs=False)
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual({}, load_ai_review_preferences(settings.data_dir))
            app.toggle(key="sidebar-ai-review-enabled").set_value(True).run()
            app.selectbox(key="sidebar-ai-review-model-openai").set_value("gpt-4.1-mini").run()
            app.text_input(key="sidebar-ai-review-key-openai").input("sk-synthetic-key").run()
            app.button(key="sidebar-ai-review-save").click().run()
            self.assertFalse(app.exception)
            saved = load_ai_review_preferences(settings.data_dir)
            self.assertTrue(saved["enable_agent_review"])
            self.assertEqual("openai", saved["llm_provider"])
            raw = (settings.data_dir / "ai_review_preferences.json").read_text(encoding="utf-8")
            self.assertNotIn("sk-synthetic-key", raw)
            self.assertFalse((REPO_ROOT / ".env").exists() and "sk-synthetic-key" in (REPO_ROOT / ".env").read_text(encoding="utf-8"))


@unittest.skipIf(AppTest is None, "Streamlit AppTest is unavailable")
class BeginnerBulkFinishChoiceTests(unittest.TestCase):
    def _beginner_app(self, root: Path) -> tuple[AppTest, Settings]:
        settings = Settings(data_dir=root / "data", artifact_root=root)
        _seed_streamlit_approval_document(settings)
        repository = JsonRepository(settings)
        first = repository.get_chunks("doc_streamlit_approval")[0]
        first.warnings = []
        first.metadata = {**first.metadata, "review_required": False}
        second = first.model_copy(deep=True, update={"chunk_id": "chunk_bulk_second", "text": "두 번째 조항 본문"})
        repository.save_chunks("doc_streamlit_approval", [first, second])
        self.enterContext(patch.object(config_module, "_runtime_overrides", {}))
        self.enterContext(patch.object(config_module, "_base_settings", return_value=settings))
        return self._fresh_app(settings), settings

    @staticmethod
    def _fresh_app(settings: Settings) -> AppTest:
        app = AppTest.from_file(str(APP_PATH), default_timeout=180)
        _seed_app_institution_context(app)
        app.session_state["document_id"] = "doc_streamlit_approval"
        app.session_state["nav_page"] = "③ 검수하고 승인"
        app.session_state["ai_connection_overrides"] = {"data_dir": settings.data_dir, "artifact_root": settings.artifact_root}
        app.session_state["beginner_guide_enabled"] = True
        app.session_state["beginner_guide_choice_made"] = True
        return app

    def test_unreviewed_beginner_sees_recommendation_and_can_finish_in_one_click(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app, settings = self._beginner_app(Path(tmp))
            app.run()
            self.assertFalse(app.exception)
            repository = JsonRepository(settings)
            # 확인 전에는 기본 승인 버튼이 없고, 선택지는 접힌 상태로만 보인다.
            self.assertFalse(any(b.label in FINAL_BUTTONS for b in app.button))
            shortcut = next(e for e in app.expander if "한 번에 승인하기 (선택)" in e.label)
            self.assertIn("남은 조항 2개", shortcut.label)
            self.assertTrue(any("사람이 마지막은 직접 검수하는 것을 추천" in w.value for w in app.warning))
            bulk_button = app.button(key="approval-bulk-finish-doc_streamlit_approval")
            self.assertTrue(bulk_button.disabled)
            self.assertFalse(repository.list_approval_records("doc_streamlit_approval"))

            app.checkbox(key="approval-bulk-finish-ack-doc_streamlit_approval").check().run()
            self.assertFalse(app.exception)
            bulk_button = app.button(key="approval-bulk-finish-doc_streamlit_approval")
            self.assertFalse(bulk_button.disabled)
            bulk_button.click().run()
            self.assertFalse(app.exception)

            records = repository.list_approval_records("doc_streamlit_approval")
            self.assertTrue(records)
            approved_ids = {str(cid) for record in records for cid in record.get("chunk_ids", [])}
            self.assertEqual({first.chunk_id for first in repository.get_chunks("doc_streamlit_approval")}, approved_ids)
            # 미검수 일괄 승인은 감사 기록에 사유가 남아야 한다.
            self.assertTrue(
                any(
                    "미검수" in str(record.get("approval_override_reason") or record.get("override_reason") or "")
                    or any("approved_without_review" in str(event.get("event") or event.get("type") or "") for event in (record.get("review_decision_events") or []))
                    for record in records
                ),
                records,
            )
            self.assertTrue(any("승인이 끝났어요" in item.value for item in app.success))
            # 새로 연 화면에서는 남은 조항이 없으므로 선택지도 사라져야 한다.
            reopened = self._fresh_app(settings)
            reopened.run()
            self.assertFalse(reopened.exception)
            self.assertFalse(any("한 번에 승인하기 (선택)" in e.label for e in reopened.expander))
            self.assertFalse(any(b.label in FINAL_BUTTONS and not b.disabled for b in reopened.button))


if __name__ == "__main__":
    unittest.main()
