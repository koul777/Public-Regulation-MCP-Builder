from __future__ import annotations

from datetime import datetime, timezone
import json
import tempfile
import unittest
from pathlib import Path

try:
    from streamlit.testing.v1 import AppTest
except Exception:  # pragma: no cover - optional in minimal environments
    AppTest = None

from app.core.config import Settings, clear_runtime_settings_overrides, set_runtime_settings_overrides
from app.core.institution_profiles import (
    InstitutionProfile,
    InstitutionProfileRegistry,
    institution_profile_registry_to_bytes,
    load_institution_profile_registry,
)
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.schemas.run import ProcessingRun
from app.storage.repository import JsonRepository


REPO_ROOT = Path(__file__).resolve().parents[1]


def _seed_app_institution_context(app) -> None:
    """Simulate the explicit institution selection required by the operator UI."""
    registry = InstitutionProfileRegistry(
        profiles={
            "test-profile": InstitutionProfile(
                profile_id="test-profile",
                display_name="Test",
                institution_name="Test Institution",
                tenant_id="default",
            )
        },
        default_profile_id="test-profile",
    )
    app.session_state["institution_profile_registry_bytes"] = institution_profile_registry_to_bytes(registry)
    app.session_state["selected_institution_profile_id"] = "test-profile"


class StreamlitApprovalAppTests(unittest.TestCase):
    def test_empty_institution_registry_stops_cleanly(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)

            app = AppTest.from_file(str(REPO_ROOT / "frontend" / "streamlit_app.py"), default_timeout=20)
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            body = "\n".join(str(markdown.value) for markdown in app.markdown)
            text_input_labels = [text_input.label for text_input in app.text_input]
            button_labels = [button.label for button in app.button]

        self.assertFalse(app.exception)
        self.assertIn("기관 선택", body)
        self.assertEqual(["기관명"], text_input_labels)
        self.assertNotIn("🔑 API Key 입력·변경", button_labels)
        self.assertNotIn("API 키 (OPENAI_API_KEY)", text_input_labels)
        self.assertFalse(
            any("INSTITUTION_PROFILES_PATH가 설정되지 않아" in error.value for error in app.error)
        )

    def test_local_institution_registration_persists_without_env_path(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)

            app = AppTest.from_file(str(REPO_ROOT / "frontend" / "streamlit_app.py"), default_timeout=20)
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()
            next(item for item in app.text_input if item.label == "기관명").input("테스트 기관 A")
            next(button for button in app.button if button.label == "기관 생성").click().run()

            registry_path = settings.data_dir / "institution_profiles.json"
            saved_registry = load_institution_profile_registry(registry_path)
            saved_profile = next(iter(saved_registry.profiles.values()))

        self.assertFalse(app.exception)
        self.assertTrue(saved_profile.profile_id.startswith("institution-"))
        self.assertEqual("테스트 기관 A", saved_profile.display_name)
        self.assertEqual("테스트 기관 A", saved_profile.institution_name)

    def test_first_screen_keeps_institution_name_input_when_profiles_already_exist(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            legacy_project_dir = settings.data_dir / "operator_projects" / "institution-legacy"
            legacy_project_dir.mkdir(parents=True, exist_ok=True)
            (legacy_project_dir / "project-saved.json").write_text(
                json.dumps(
                    {
                        "report_type": "streamlit_operator_project_checkpoint",
                        "schema_version": 1,
                        "project_name": "기존 저장 프로젝트",
                        "institution_profile_id": "existing-profile",
                        "document_id": "",
                        "page": "작업 홈",
                        "session_values": {},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            registry = InstitutionProfileRegistry(
                profiles={
                    "existing-profile": InstitutionProfile(
                        profile_id="existing-profile",
                        display_name="기존 기관",
                        institution_name="기존 기관",
                        tenant_id="default",
                    )
                },
                default_profile_id="existing-profile",
            )

            app = AppTest.from_file(str(REPO_ROOT / "frontend" / "streamlit_app.py"), default_timeout=20)
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.session_state["institution_profile_registry_bytes"] = institution_profile_registry_to_bytes(registry)
            app.run()

            text_input_labels = [text_input.label for text_input in app.text_input]
            button_labels = [button.label for button in app.button]
            next(button for button in app.button if button.label == "이 기관으로 시작").click().run()
            dashboard_button_labels = [button.label for button in app.button]

        self.assertFalse(app.exception)
        self.assertIn("기관명", text_input_labels)
        self.assertIn("기관 생성", button_labels)
        self.assertIn("이 기관으로 시작", button_labels)
        self.assertNotIn("저장한 프로젝트 불러오기", button_labels)
        self.assertNotIn("📂 불러오기", button_labels)
        self.assertIn("📂 불러오기", dashboard_button_labels)

    def test_existing_institution_can_be_deleted_after_confirmation(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            registry = InstitutionProfileRegistry(
                profiles={
                    "existing-profile": InstitutionProfile(
                        profile_id="existing-profile",
                        display_name="삭제 대상 기관",
                        institution_name="삭제 대상 기관",
                        tenant_id="default",
                    )
                },
                default_profile_id="existing-profile",
            )

            app = AppTest.from_file(str(REPO_ROOT / "frontend" / "streamlit_app.py"), default_timeout=20)
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.session_state["institution_profile_registry_bytes"] = institution_profile_registry_to_bytes(registry)
            app.run()
            next(button for button in app.button if button.label == "기관 삭제").click().run()

            confirm_labels = [button.label for button in app.button]
            warning_text = "\n".join(str(item.value) for item in app.warning)
            next(button for button in app.button if button.label == "삭제 확인").click().run()

            saved_registry = load_institution_profile_registry(settings.data_dir / "institution_profiles.json")

        self.assertFalse(app.exception)
        self.assertIn("삭제 확인", confirm_labels)
        self.assertIn("규정·승인 데이터는 자동 삭제하지 않습니다", warning_text)
        self.assertEqual({}, saved_registry.profiles)

    def test_local_quality_profile_save_persists_without_env_path(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)

            app = AppTest.from_file(str(REPO_ROOT / "frontend" / "streamlit_app.py"), default_timeout=20)
            _seed_app_institution_context(app)
            app.session_state["nav_page"] = "⚙️ 관리자 설정"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()
            next(button for button in app.button if button.label == "품질 프로필 저장").click().run()

            quality_saved = (settings.data_dir / "quality_profiles.json").exists()

        self.assertFalse(app.exception)
        self.assertTrue(quality_saved)

    def test_preprocess_page_exposes_named_project_save_and_red_api_setup_button(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)

            app = AppTest.from_file(str(REPO_ROOT / "frontend" / "streamlit_app.py"), default_timeout=20)
            _seed_app_institution_context(app)
            app.session_state["nav_page"] = "① 문서 올려서 전처리"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            page_text_input_labels = [item.label for item in app.text_input]
            page_button_labels = [item.label for item in app.button]
            next(item for item in app.button if item.label == "💾 저장하기").click().run()
            dialog_text_input_labels = [item.label for item in app.text_input]
            dialog_button_labels = [item.label for item in app.button]
            chosen_project_dir = root / "chosen-projects"
            next(item for item in app.text_input if item.label == "저장 폴더 위치").input(
                str(chosen_project_dir)
            )
            next(item for item in app.text_input if item.label == "프로젝트 이름").input("테스트 프로젝트")
            next(item for item in app.button if item.label == "💾 이 폴더에 프로젝트 저장").click().run()
            project_files = list(chosen_project_dir.glob("project-*.json"))
            self.assertTrue(
                project_files,
                {
                    "directory": app.session_state["operator_project_directory"],
                    "errors": [str(item.value) for item in app.error],
                    "success": [str(item.value) for item in app.success],
                },
            )
            project_payload = json.loads(project_files[0].read_text(encoding="utf-8"))
            next(item for item in app.selectbox if item.label == "저장한 프로젝트").select(str(project_files[0])).run()
            next(item for item in app.button if item.label == "저장한 프로젝트 불러오기").click().run()
            loaded_project_name = app.session_state["operator_project_name"]

        self.assertFalse(app.exception)
        self.assertNotIn("프로젝트 이름", page_text_input_labels)
        self.assertNotIn("API 키 (OPENAI_API_KEY)", page_text_input_labels)
        self.assertIn("💾 저장하기", page_button_labels)
        self.assertIn("AI 검수 공급자·모델·API 키 설정", page_button_labels)
        self.assertIn("프로젝트 이름", dialog_text_input_labels)
        self.assertIn("저장 폴더 위치", dialog_text_input_labels)
        self.assertIn("Windows 탐색기에서 저장 폴더 선택", dialog_button_labels)
        self.assertIn("💾 이 폴더에 프로젝트 저장", dialog_button_labels)
        self.assertIn("저장한 프로젝트 불러오기", dialog_button_labels)
        self.assertEqual("테스트 프로젝트", project_payload["project_name"])
        self.assertEqual("① 문서 올려서 전처리", project_payload["page"])
        self.assertEqual("테스트 프로젝트", loaded_project_name)
        self.assertNotIn("openai_api_key", json.dumps(project_payload, ensure_ascii=False).casefold())

    def test_approval_tabs_smoke_reflect_human_check_and_approve(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            _seed_streamlit_approval_document(settings)
            set_runtime_settings_overrides(data_dir=settings.data_dir, artifact_root=settings.artifact_root)
            self.addCleanup(clear_runtime_settings_overrides)

            app = AppTest.from_file(str(REPO_ROOT / "frontend" / "streamlit_app.py"), default_timeout=20)
            _seed_app_institution_context(app)
            app.session_state["document_id"] = "doc_streamlit_approval"
            app.session_state["nav_page"] = "\u2462 \uac80\uc218\ud558\uace0 \uc2b9\uc778"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            body = "\n".join(str(markdown.value) for markdown in app.markdown)
            self.assertIn("\u2462 \uac80\uc218\ud558\uace0 \uc2b9\uc778", body)

            reflect = next(button for button in app.button if button.label == "\ubc18\uc601")
            reflect.click().run()

            human_check = next(
                checkbox for checkbox in app.checkbox if checkbox.label == "\uc6d0\ubcf8\uacfc \uc804\ucc98\ub9ac \uacb0\uacfc\ub97c \ud655\uc778\ud588\uc2b5\ub2c8\ub2e4."
            )
            human_check.check().run()

            approve = next(button for button in app.button if button.label == "\uc2b9\uc778\ud558\uace0 \uc0c9\uc778")
            self.assertFalse(approve.disabled)
            approve.click().run()

            approved = JsonRepository(settings).get_chunks("doc_streamlit_approval")[0]
            approvals = JsonRepository(settings).list_approval_records("doc_streamlit_approval")

        self.assertEqual("approved", approved.approval_status)
        self.assertTrue(approvals)
        self.assertIn("review_decision_events", approvals[0])

    def test_approval_saves_direct_before_after_text_edit_before_indexing(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            _seed_streamlit_approval_document(settings)
            set_runtime_settings_overrides(data_dir=settings.data_dir, artifact_root=settings.artifact_root)
            self.addCleanup(clear_runtime_settings_overrides)

            app = AppTest.from_file(str(REPO_ROOT / "frontend" / "streamlit_app.py"), default_timeout=20)
            _seed_app_institution_context(app)
            app.session_state["document_id"] = "doc_streamlit_approval"
            app.session_state["nav_page"] = "③ 검수하고 승인"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            edited = "사람이 직접 고친 최종 규정 본문"
            next(area for area in app.text_area if area.label == "수정 후 내용").set_value(edited).run()
            next(button for button in app.button if button.label == "반영").click().run()
            next(
                checkbox
                for checkbox in app.checkbox
                if checkbox.label == "원본과 전처리 결과를 확인했습니다."
            ).check().run()
            next(button for button in app.button if button.label == "승인하고 색인").click().run()

            saved = JsonRepository(settings).get_chunks("doc_streamlit_approval")[0]

        self.assertEqual(edited, saved.text)
        self.assertEqual(edited, saved.normalized_text)
        self.assertEqual(edited, saved.retrieval_text)
        self.assertTrue(saved.metadata["human_review_edited"])
        self.assertEqual(64, len(saved.metadata["human_review_original_sha256"]))

    def test_primary_next_button_uses_transition_dialog_then_changes_page(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            _seed_streamlit_approval_document(settings)
            set_runtime_settings_overrides(data_dir=settings.data_dir, artifact_root=settings.artifact_root)
            self.addCleanup(clear_runtime_settings_overrides)

            app = AppTest.from_file(str(REPO_ROOT / "frontend" / "streamlit_app.py"), default_timeout=20)
            _seed_app_institution_context(app)
            app.session_state["document_id"] = "doc_streamlit_approval"
            app.session_state["nav_page"] = "③ 검수하고 승인"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            next(button for button in app.button if button.label == "④ MCP 생성·AI 연결로 이동").click().run()

        self.assertEqual("④ MCP 생성·AI 연결", app.session_state["nav_page"])
        self.assertNotIn("workflow_transition_state", app.session_state.filtered_state)
        self.assertFalse(app.exception)

    def test_approval_tabs_approve_only_reviewed_compare_chunk(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            _seed_streamlit_approval_document_with_second_chunk(settings)
            set_runtime_settings_overrides(data_dir=settings.data_dir, artifact_root=settings.artifact_root)
            self.addCleanup(clear_runtime_settings_overrides)

            app = AppTest.from_file(str(REPO_ROOT / "frontend" / "streamlit_app.py"), default_timeout=20)
            _seed_app_institution_context(app)
            app.session_state["document_id"] = "doc_streamlit_approval"
            app.session_state["nav_page"] = "\u2462 \uac80\uc218\ud558\uace0 \uc2b9\uc778"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            reflect = next(button for button in app.button if button.label == "\ubc18\uc601")
            reflect.click().run()
            human_check = next(
                checkbox for checkbox in app.checkbox if checkbox.label == "\uc6d0\ubcf8\uacfc \uc804\ucc98\ub9ac \uacb0\uacfc\ub97c \ud655\uc778\ud588\uc2b5\ub2c8\ub2e4."
            )
            human_check.check().run()
            approve = next(button for button in app.button if button.label == "\uc2b9\uc778\ud558\uace0 \uc0c9\uc778")
            approve.click().run()

            chunks = {chunk.chunk_id: chunk for chunk in JsonRepository(settings).get_chunks("doc_streamlit_approval")}
            approvals = JsonRepository(settings).list_approval_records("doc_streamlit_approval")
            next_compare_chunk = app.session_state["approval-compare-chunk-doc_streamlit_approval"]

        self.assertEqual("approved", chunks["chunk-streamlit"].approval_status)
        self.assertEqual("draft", chunks["chunk-streamlit-second"].approval_status)
        self.assertEqual(["chunk-streamlit"], approvals[0]["chunk_ids"])
        self.assertEqual("chunk-streamlit-second", next_compare_chunk)

    def test_approval_tabs_bulk_ai_and_human_confirm_enable_approval(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            _seed_streamlit_approval_document_with_second_chunk(settings)
            set_runtime_settings_overrides(data_dir=settings.data_dir, artifact_root=settings.artifact_root)
            self.addCleanup(clear_runtime_settings_overrides)

            app = AppTest.from_file(str(REPO_ROOT / "frontend" / "streamlit_app.py"), default_timeout=20)
            _seed_app_institution_context(app)
            app.session_state["document_id"] = "doc_streamlit_approval"
            app.session_state["nav_page"] = "\u2462 \uac80\uc218\ud558\uace0 \uc2b9\uc778"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            bulk_ai = next(
                button
                for button in app.button
                if button.label == "현재 규정 AI 검수 완료"
            )
            bulk_ai.click().run()
            bulk_human = next(
                button for button in app.button if button.label == "현재 규정 사람 확인 완료"
            )
            bulk_human.click().run()

            approve = next(button for button in app.button if button.label == "\uc2b9\uc778\ud558\uace0 \uc0c9\uc778")
            ai_decisions = app.session_state["approval:doc_streamlit_approval:chunk-streamlit:ai_decisions"]
            first_human = app.session_state["approval:doc_streamlit_approval:chunk-streamlit:human_confirmed"]
            second_human = app.session_state["approval:doc_streamlit_approval:chunk-streamlit-second:human_confirmed"]
            approve.click().run()

            chunks = {chunk.chunk_id: chunk for chunk in JsonRepository(settings).get_chunks("doc_streamlit_approval")}
            approvals = JsonRepository(settings).list_approval_records("doc_streamlit_approval")
            review_events = [
                event
                for approval in approvals
                for event in approval.get("review_decision_events", [])
                if isinstance(event, dict)
            ]
            events_by_chunk = {
                chunk_id: {event.get("event") for event in review_events if event.get("chunk_id") == chunk_id}
                for chunk_id in ("chunk-streamlit", "chunk-streamlit-second")
            }

        self.assertFalse(approve.disabled)
        self.assertTrue(ai_decisions)
        self.assertEqual({"reflect"}, set(ai_decisions.values()))
        self.assertTrue(first_human)
        self.assertTrue(second_human)
        self.assertEqual("approved", chunks["chunk-streamlit"].approval_status)
        self.assertEqual("approved", chunks["chunk-streamlit-second"].approval_status)
        self.assertEqual(
            {"chunk-streamlit", "chunk-streamlit-second"},
            {chunk_id for record in approvals for chunk_id in record["chunk_ids"]},
        )
        for chunk_id in ("chunk-streamlit", "chunk-streamlit-second"):
            self.assertEqual(
                {"ai_review_confirmed", "human_review_confirmed", "approved"},
                events_by_chunk[chunk_id],
            )

    def test_bulk_confirm_preserves_chunk_state_and_allows_remaining_review(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            _seed_streamlit_approval_document_with_second_chunk(settings)
            set_runtime_settings_overrides(data_dir=settings.data_dir, artifact_root=settings.artifact_root)
            self.addCleanup(clear_runtime_settings_overrides)

            app = AppTest.from_file(str(REPO_ROOT / "frontend" / "streamlit_app.py"), default_timeout=20)
            _seed_app_institution_context(app)
            app.session_state["document_id"] = "doc_streamlit_approval"
            app.session_state["nav_page"] = "\u2462 \uac80\uc218\ud558\uace0 \uc2b9\uc778"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            self.assertEqual(
                "chunk-streamlit",
                app.session_state["approval-compare-chunk-doc_streamlit_approval"],
            )
            chunk_selector = next(
                selectbox for selectbox in app.selectbox if selectbox.label == "\uac80\uc218\ud560 \uccad\ud06c \uc120\ud0dd"
            )
            chunk_selector.select("chunk-streamlit-second").run()
            self.assertEqual(
                "chunk-streamlit-second",
                app.session_state["approval-compare-chunk-doc_streamlit_approval"],
            )
            bulk_human = next(
                button for button in app.button if button.label == "현재 규정 사람 확인 완료"
            )
            bulk_human.click().run()

            chunk_selector = next(
                selectbox
                for selectbox in app.selectbox
                if selectbox.label == "\uac80\uc218\ud560 \uccad\ud06c \uc120\ud0dd"
            )
            chunk_selector.select("chunk-streamlit").run()
            first_human_check = next(
                checkbox
                for checkbox in app.checkbox
                if checkbox.label == "\uc6d0\ubcf8\uacfc \uc804\ucc98\ub9ac \uacb0\uacfc\ub97c \ud655\uc778\ud588\uc2b5\ub2c8\ub2e4."
            )
            self.assertTrue(first_human_check.value)

            reflect = next(button for button in app.button if button.label == "\ubc18\uc601")
            reflect.click().run()
            self.assertEqual(
                {"reflect"},
                set(
                    app.session_state[
                        "approval:doc_streamlit_approval:chunk-streamlit:ai_decisions"
                    ].values()
                ),
            )

            chunk_selector = next(
                selectbox
                for selectbox in app.selectbox
                if selectbox.label == "\uac80\uc218\ud560 \uccad\ud06c \uc120\ud0dd"
            )
            chunk_selector.select("chunk-streamlit-second").run()
            second_human_check = next(
                checkbox
                for checkbox in app.checkbox
                if checkbox.label == "\uc6d0\ubcf8\uacfc \uc804\ucc98\ub9ac \uacb0\uacfc\ub97c \ud655\uc778\ud588\uc2b5\ub2c8\ub2e4."
            )
            self.assertTrue(second_human_check.value)

            self.assertFalse(app.exception)
            self.assertTrue(
                app.session_state["approval:doc_streamlit_approval:chunk-streamlit:human_confirmed"]
            )
            self.assertTrue(
                app.session_state["approval:doc_streamlit_approval:chunk-streamlit-second:human_confirmed"]
            )
            approve = next(button for button in app.button if button.label == "\uc2b9\uc778\ud558\uace0 \uc0c9\uc778")
            self.assertFalse(approve.disabled)

    def test_remaining_review_buttons_preserve_completed_work_and_fill_only_missing_items(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            _seed_streamlit_approval_document_with_second_chunk(settings)
            repository = JsonRepository(settings)
            run = repository.latest_completed_run("doc_streamlit_approval")
            self.assertIsNotNone(run)
            repository.upsert_run(
                run.model_copy(
                    update={
                        "stats": {
                            "agent_review": {
                                "status": "planned",
                                "candidate_count": 1,
                                "selected_count": 1,
                                "selected_candidates": [
                                    {
                                        "chunk_id": "chunk-streamlit",
                                        "chunk_type": "table",
                                        "reasons": ["table_like_without_cell_rows", "table_review_required"],
                                    }
                                ],
                            }
                        }
                    }
                )
            )
            set_runtime_settings_overrides(data_dir=settings.data_dir, artifact_root=settings.artifact_root)
            self.addCleanup(clear_runtime_settings_overrides)

            app = AppTest.from_file(str(REPO_ROOT / "frontend" / "streamlit_app.py"), default_timeout=20)
            _seed_app_institution_context(app)
            app.session_state["document_id"] = "doc_streamlit_approval"
            app.session_state["nav_page"] = "③ 검수하고 승인"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            next(button for button in app.button if button.label == "반영").click().run()
            first_human_check = next(
                checkbox
                for checkbox in app.checkbox
                if checkbox.label == "원본과 전처리 결과를 확인했습니다."
            )
            first_human_check.check().run()
            next(
                button for button in app.button if button.label == "나머지 부분 AI 점검 전체 완료"
            ).click().run()
            next(
                button for button in app.button if button.label == "나머지 부분 사람 점검 전체 완료"
            ).click().run()

            decisions = app.session_state[
                "approval:doc_streamlit_approval:chunk-streamlit:ai_decisions"
            ]
            first_human = app.session_state[
                "approval:doc_streamlit_approval:chunk-streamlit:human_confirmed"
            ]
            second_human = app.session_state[
                "approval:doc_streamlit_approval:chunk-streamlit-second:human_confirmed"
            ]

        self.assertFalse(app.exception)
        self.assertEqual(2, len(decisions))
        self.assertEqual(1, list(decisions.values()).count("reflect"))
        self.assertEqual(1, list(decisions.values()).count("skip"))
        self.assertTrue(first_human)
        self.assertTrue(second_human)

    def test_approval_tabs_bulk_approval_ignores_stale_selected_batch_scope(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            _seed_streamlit_approval_document_with_second_chunk(settings)
            set_runtime_settings_overrides(data_dir=settings.data_dir, artifact_root=settings.artifact_root)
            self.addCleanup(clear_runtime_settings_overrides)

            app = AppTest.from_file(str(REPO_ROOT / "frontend" / "streamlit_app.py"), default_timeout=20)
            _seed_app_institution_context(app)
            app.session_state["document_id"] = "doc_streamlit_approval"
            app.session_state["nav_page"] = "\u2462 \uac80\uc218\ud558\uace0 \uc2b9\uc778"
            app.session_state["approval-selected-chunk-ids-doc_streamlit_approval"] = ["chunk-streamlit-second"]
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            bulk_ai = next(
                button
                for button in app.button
                if button.label == "현재 규정 AI 검수 완료"
            )
            bulk_ai.click().run()
            bulk_human = next(
                button for button in app.button if button.label == "현재 규정 사람 확인 완료"
            )
            bulk_human.click().run()
            approve = next(button for button in app.button if button.label == "\uc2b9\uc778\ud558\uace0 \uc0c9\uc778")
            approve.click().run()

            chunks = {chunk.chunk_id: chunk for chunk in JsonRepository(settings).get_chunks("doc_streamlit_approval")}
            approvals = JsonRepository(settings).list_approval_records("doc_streamlit_approval")

        self.assertFalse(approve.disabled)
        self.assertEqual("approved", chunks["chunk-streamlit"].approval_status)
        self.assertEqual("approved", chunks["chunk-streamlit-second"].approval_status)
        self.assertEqual(
            {"chunk-streamlit", "chunk-streamlit-second"},
            {chunk_id for record in approvals for chunk_id in record["chunk_ids"]},
        )

    def test_approval_tabs_advance_from_already_approved_selected_chunk(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            _seed_streamlit_approval_document_with_second_chunk(settings)
            repository = JsonRepository(settings)
            chunks = repository.get_chunks("doc_streamlit_approval")
            chunks[0] = chunks[0].model_copy(update={"approval_status": "approved", "approval_id": "approval-existing"})
            repository.save_chunks("doc_streamlit_approval", chunks)
            set_runtime_settings_overrides(data_dir=settings.data_dir, artifact_root=settings.artifact_root)
            self.addCleanup(clear_runtime_settings_overrides)

            app = AppTest.from_file(str(REPO_ROOT / "frontend" / "streamlit_app.py"), default_timeout=20)
            _seed_app_institution_context(app)
            app.session_state["document_id"] = "doc_streamlit_approval"
            app.session_state["nav_page"] = "\u2462 \uac80\uc218\ud558\uace0 \uc2b9\uc778"
            app.session_state["approval-compare-chunk-doc_streamlit_approval"] = "chunk-streamlit"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

        self.assertEqual("chunk-streamlit-second", app.session_state["approval-compare-chunk-doc_streamlit_approval"])

    def test_selected_regulations_are_reviewed_approved_and_indexed_separately(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            _seed_streamlit_multi_approval_documents(settings)
            set_runtime_settings_overrides(data_dir=settings.data_dir, artifact_root=settings.artifact_root)
            self.addCleanup(clear_runtime_settings_overrides)

            app = AppTest.from_file(str(REPO_ROOT / "frontend" / "streamlit_app.py"), default_timeout=40)
            _seed_app_institution_context(app)
            app.session_state["document_id"] = "doc_streamlit_approval"
            app.session_state["workflow_document_ids"] = ["doc_streamlit_approval", "doc_streamlit_service"]
            app.session_state["workflow_selected_document_ids"] = ["doc_streamlit_approval", "doc_streamlit_service"]
            app.session_state["workflow-document-selected-doc_streamlit_approval"] = True
            app.session_state["workflow-document-selected-doc_streamlit_service"] = True
            app.session_state["nav_page"] = "③ 검수하고 승인"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            next(
                button
                for button in app.button
                if button.label == "전체 규정 자료 AI 검수 완료 (선택 2개)"
            ).click().run()
            next(
                button
                for button in app.button
                if button.label == "전체 규정 자료 사람 확인 완료 (선택 2개)"
            ).click().run()
            approve = next(
                button for button in app.button if button.label == "선택한 규정 2개 승인·색인"
            )
            self.assertFalse(approve.disabled)
            approve.click().run()

            repository = JsonRepository(settings)
            personnel_chunks = repository.get_chunks("doc_streamlit_approval")
            service_chunks = repository.get_chunks("doc_streamlit_service")
            personnel_document = repository.get_document("doc_streamlit_approval")
            service_document = repository.get_document("doc_streamlit_service")

        self.assertFalse(app.exception)
        self.assertTrue(all(chunk.approval_status == "approved" for chunk in personnel_chunks))
        self.assertTrue(all(chunk.approval_status == "approved" for chunk in service_chunks))
        self.assertEqual("reg-personnel", personnel_document.regulation_id)
        self.assertEqual("reg-service", service_document.regulation_id)
        self.assertEqual("인사규정 > 제1조", personnel_chunks[0].metadata["hierarchy_path"])
        self.assertEqual("복무규정 > 제1조", service_chunks[0].metadata["hierarchy_path"])


def _seed_streamlit_approval_document(settings: Settings) -> None:
    repository = JsonRepository(settings)
    repository.upsert_document(
        Document(
            document_id="doc_streamlit_approval",
            filename="approval.pdf",
            document_name="Approval",
            file_type="pdf",
            file_hash="hash",
            tenant_id="default",
            status="completed",
            institution_name="테스트기관",
            source_system="LOCAL",
            source_url="https://example.test/approval.pdf",
            profile_id="test-profile",
        )
    )
    repository.save_processing_result(
        "doc_streamlit_approval",
        [],
        [
            Chunk(
                chunk_id="chunk-streamlit",
                document_id="doc_streamlit_approval",
                chunk_type="table",
                text="전처리 표 본문",
                retrieval_text="전처리 표 본문",
                metadata={
                    "raw_text": "원본 표 본문",
                    "table_review_required": True,
                    "table_review_flags": ["row_review_required"],
                    "table_source": "kordoc",
                    "kordoc_table_promoted": True,
                    "table_cell_rows": [
                        {"row_index": 0, "cells": ["구분", "내용"], "raw": "구분 | 내용"},
                        {"row_index": 1, "cells": ["A", "B"], "raw": "A | B"},
                    ],
                },
            )
        ],
        [],
    )
    now = datetime.now(timezone.utc)
    repository.upsert_run(
        ProcessingRun(
            run_id="run-streamlit-approval",
            document_id="doc_streamlit_approval",
            job_id="job-streamlit-approval",
            tenant_id="default",
            status="completed",
            started_at=now,
            completed_at=now,
            elapsed_seconds=0.1,
            stats={
                "agent_review": {
                    "status": "planned",
                    "candidate_count": 1,
                    "selected_count": 1,
                    "selected_candidates": [
                        {
                            "chunk_id": "chunk-streamlit",
                            "chunk_type": "table",
                            "reasons": ["table_like_without_cell_rows"],
                        }
                    ],
                }
            },
        )
    )


def _seed_streamlit_approval_document_with_second_chunk(settings: Settings) -> None:
    _seed_streamlit_approval_document(settings)
    repository = JsonRepository(settings)
    chunks = repository.get_chunks("doc_streamlit_approval")
    chunks.append(
        Chunk(
            chunk_id="chunk-streamlit-second",
            document_id="doc_streamlit_approval",
            chunk_type="article",
            text="second draft content",
            retrieval_text="second draft content",
            metadata={"raw_text": "second source content"},
        )
    )
    repository.save_chunks("doc_streamlit_approval", chunks)


def _seed_streamlit_multi_approval_documents(settings: Settings) -> None:
    _seed_streamlit_approval_document(settings)
    repository = JsonRepository(settings)
    personnel_document = repository.get_document("doc_streamlit_approval")
    repository.upsert_document(
        personnel_document.model_copy(
            update={
                "document_name": "인사규정",
                "regulation_id": "reg-personnel",
                "regulation_version": "rev-20250101",
                "revision_date": "2025-01-01",
                "effective_from": "2025-01-01",
            }
        )
    )
    personnel_chunks = repository.get_chunks("doc_streamlit_approval")
    personnel_chunks[0].metadata = {
        **personnel_chunks[0].metadata,
        "hierarchy_path": "인사규정 > 제1조",
    }
    repository.save_chunks("doc_streamlit_approval", personnel_chunks)

    repository.upsert_document(
        Document(
            document_id="doc_streamlit_service",
            filename="service.hwp",
            document_name="복무규정",
            file_type="hwp",
            file_hash="service-hash",
            tenant_id="default",
            status="completed",
            institution_name="테스트기관",
            source_system="LOCAL",
            source_url="https://example.test/service.hwp",
            profile_id="test-profile",
            regulation_id="reg-service",
            regulation_version="rev-20250201",
            revision_date="2025-02-01",
            effective_from="2025-02-01",
        )
    )
    repository.save_processing_result(
        "doc_streamlit_service",
        [],
        [
            Chunk(
                chunk_id="chunk-streamlit-service",
                document_id="doc_streamlit_service",
                chunk_type="article",
                text="복무규정 제1조 본문",
                retrieval_text="복무규정 제1조 본문",
                metadata={
                    "raw_text": "복무규정 제1조 원문",
                    "hierarchy_path": "복무규정 > 제1조",
                    "kordoc_table_parser_status": "parsed",
                    "kordoc_table_count": 0,
                },
            )
        ],
        [],
    )


if __name__ == "__main__":
    unittest.main()
