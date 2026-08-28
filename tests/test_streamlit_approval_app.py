from __future__ import annotations

from datetime import datetime, timezone
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
    save_institution_profile_registry,
)
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.schemas.run import ProcessingRun
from app.services.document_service import DocumentService
from app.services.institution_purge_service import InstitutionPurgeService
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


def _confirm_rendered_approval_rows(app) -> None:
    """Exercise every explicit approval control currently visible in the sheet."""

    reflect_keys = [
        button.key
        for button in app.button
        if button.label == "수정 필요로 판단"
    ]
    for key in reflect_keys:
        next(button for button in app.button if button.key == key).click().run()

    resolution_note_keys = [
        area.key
        for area in app.text_area
        if area.label == "수정 필요 항목 처리 메모"
    ]
    for key in resolution_note_keys:
        next(area for area in app.text_area if area.key == key).set_value(
            "AI 지적을 원문과 대조해 처리했습니다."
        ).run()

    ai_confirmation_keys = [
        checkbox.key
        for checkbox in app.checkbox
        if checkbox.label in {
            "AI 검수 항목에 대한 판단을 모두 확인했습니다.",
            "AI 검수 항목이 없음을 확인했습니다.",
        }
    ]
    for key in ai_confirmation_keys:
        next(checkbox for checkbox in app.checkbox if checkbox.key == key).set_value(True).run()

    human_confirmation_keys = [
        checkbox.key
        for checkbox in app.checkbox
        if checkbox.label
        == "원본과 최종본을 직접 대조했고, 이 내용으로 승인·색인하는 데 동의합니다."
    ]
    for key in human_confirmation_keys:
        next(checkbox for checkbox in app.checkbox if checkbox.key == key).set_value(True).run()


class StreamlitApprovalAppTests(unittest.TestCase):
    def test_home_document_delete_requires_explicit_confirmation(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            _seed_streamlit_approval_document(settings)
            repository = JsonRepository(settings)
            document = repository.get_document("doc_streamlit_approval")
            self.assertIsNotNone(document)
            source_path = DocumentService(
                settings=settings,
                repository=repository,
            ).path_for(document)
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_bytes(b"synthetic source")
            set_runtime_settings_overrides(
                data_dir=settings.data_dir,
                artifact_root=settings.artifact_root,
            )
            self.addCleanup(clear_runtime_settings_overrides)

            app = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=20,
            )
            _seed_app_institution_context(app)
            app.session_state["nav_page"] = "🏠 시작하기"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()
            next(button for button in app.button if button.label == "삭제").click().run()

            self.assertIsNotNone(repository.get_document("doc_streamlit_approval"))
            self.assertTrue(source_path.is_file())
            permanent_delete = next(
                button for button in app.button if button.label == "영구 삭제"
            )
            self.assertTrue(permanent_delete.disabled)
            next(button for button in app.button if button.label == "취소").click().run()
            self.assertIsNotNone(repository.get_document("doc_streamlit_approval"))
            self.assertTrue(source_path.is_file())

            next(button for button in app.button if button.label == "삭제").click().run()
            next(
                checkbox
                for checkbox in app.checkbox
                if checkbox.label == "이 문서와 원본 파일의 영구 삭제를 확인했습니다."
            ).set_value(True).run()
            next(button for button in app.button if button.label == "영구 삭제").click().run()

            deleted_document = repository.get_document("doc_streamlit_approval")
            source_exists_after_delete = source_path.exists()

        self.assertFalse(app.exception)
        self.assertIsNone(deleted_document)
        self.assertFalse(source_exists_after_delete)

    def test_preprocess_document_delete_requires_second_confirmation(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            _seed_streamlit_approval_document(settings)
            repository = JsonRepository(settings)
            set_runtime_settings_overrides(
                data_dir=settings.data_dir,
                artifact_root=settings.artifact_root,
            )
            self.addCleanup(clear_runtime_settings_overrides)

            app = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=20,
            )
            _seed_app_institution_context(app)
            app.session_state["nav_page"] = "① 문서 올려서 전처리"
            app.session_state["beginner_guide_enabled"] = False
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()
            next(
                item
                for item in app.multiselect
                if item.label == "삭제할 이전 전처리 작업"
            ).select(app.multiselect[-1].options[0]).run()
            next(
                button
                for button in app.button
                if button.label == "선택한 전처리 작업 삭제"
            ).click().run()

            self.assertIsNotNone(repository.get_document("doc_streamlit_approval"))
            permanent_delete = next(
                button
                for button in app.button
                if button.label == "선택 작업 영구 삭제"
            )
            self.assertTrue(permanent_delete.disabled)

            next(
                checkbox
                for checkbox in app.checkbox
                if checkbox.label
                == "선택한 문서와 관련 검색·승인 기록의 영구 삭제를 확인했습니다."
            ).set_value(True).run()
            next(
                button
                for button in app.button
                if button.label == "선택 작업 영구 삭제"
            ).click().run()

            deleted_document = repository.get_document("doc_streamlit_approval")

        self.assertFalse(app.exception)
        self.assertIsNone(deleted_document)

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
        # 저장된 규정이 없는 기관은 지울 데이터도 없다고 사실대로 적는다.
        self.assertIn("저장된 규정 데이터 없음", warning_text)
        self.assertEqual({}, saved_registry.profiles)

    def test_institution_delete_removes_its_regulation_data(self) -> None:
        """기관을 지우면 그 기관 규정도 함께 사라져야 한다.

        예전에는 프로필만 지웠다. 기관 ID가 기관명 해시라 같은 이름으로 다시 등록하면
        규정이 전부 되살아났고, 운영자에게는 삭제가 안 된 것으로 보였다.
        """
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_institution_purge",
                    filename="인사규정.hwp",
                    document_name="인사규정",
                    file_type="hwp",
                    file_hash="purge-hash",
                    tenant_id="default",
                    profile_id="existing-profile",
                    status="completed",
                )
            )
            repository.save_chunks(
                "doc_institution_purge",
                [
                    Chunk(
                        chunk_id="doc_institution_purge_chunk_1",
                        document_id="doc_institution_purge",
                        chunk_type="article",
                        text="제1조(목적) 본문",
                    )
                ],
            )
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

            warning_text = "\n".join(str(item.value) for item in app.warning)
            confirm_button = next(button for button in app.button if button.label == "삭제 확인")
            blocked_before_typing = confirm_button.disabled
            # 되돌릴 수 없는 삭제라 기관명을 그대로 입력해야 버튼이 열린다.
            app.text_input[-1].set_value("삭제 대상 기관").run()
            next(button for button in app.button if button.label == "삭제 확인").click().run()

            saved_registry = load_institution_profile_registry(settings.data_dir / "institution_profiles.json")
            remaining_documents = [
                document.document_id for document in JsonRepository(settings).list_documents()
            ]

        self.assertFalse(app.exception)
        self.assertTrue(blocked_before_typing)
        self.assertIn("규정 1개", warning_text)
        self.assertEqual({}, saved_registry.profiles)
        self.assertEqual([], remaining_documents)

    def test_institution_delete_keeps_profile_when_deindex_fails(self) -> None:
        """A failed deindex must leave the profile, document, and index discoverable."""

        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_failed_institution_purge",
                    filename="인사규정.hwp",
                    document_name="인사규정",
                    file_type="hwp",
                    file_hash="failed-purge-hash",
                    tenant_id="default",
                    profile_id="existing-profile",
                    status="completed",
                )
            )
            registry = InstitutionProfileRegistry(
                profiles={
                    "existing-profile": InstitutionProfile(
                        profile_id="existing-profile",
                        display_name="삭제 실패 기관",
                        institution_name="삭제 실패 기관",
                        tenant_id="default",
                    )
                },
                default_profile_id="existing-profile",
            )
            registry_path = settings.data_dir / "institution_profiles.json"
            save_institution_profile_registry(registry_path, registry)
            vector_path = settings.data_dir / "vector_db" / "default" / "approved_vectors.jsonl"
            vector_path.parent.mkdir(parents=True, exist_ok=True)
            vector_bytes = (
                json.dumps(
                    {
                        "id": "failed-purge-chunk",
                        "document_id": "doc_failed_institution_purge",
                        "text": "제1조 본문",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            ).encode("utf-8")
            vector_path.write_bytes(vector_bytes)
            set_runtime_settings_overrides(
                data_dir=settings.data_dir,
                artifact_root=settings.artifact_root,
            )
            self.addCleanup(clear_runtime_settings_overrides)

            app = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=20,
            )
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.session_state["institution_profile_registry_bytes"] = (
                institution_profile_registry_to_bytes(registry)
            )
            app.run()
            next(button for button in app.button if button.label == "기관 삭제").click().run()
            app.text_input[-1].set_value("삭제 실패 기관").run()

            def fail_deindex(_service, _documents, result) -> int:
                result.failures.append("simulated deindex failure")
                return 0

            with patch.object(
                InstitutionPurgeService,
                "_deindex_documents",
                fail_deindex,
            ):
                next(
                    button for button in app.button if button.label == "삭제 확인"
                ).click().run()

            saved_registry = load_institution_profile_registry(registry_path)
            remaining_document = JsonRepository(settings).get_document(
                "doc_failed_institution_purge"
            )
            remaining_vector_bytes = vector_path.read_bytes()

        self.assertFalse(app.exception)
        self.assertIn("existing-profile", saved_registry.profiles)
        self.assertIsNotNone(remaining_document)
        self.assertEqual(vector_bytes, remaining_vector_bytes)
        self.assertTrue(
            any("기관 프로필을 유지했습니다" in str(item.value) for item in app.error)
        )

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
            project_selector = next(item for item in app.selectbox if item.label == "저장한 프로젝트")
            project_selector.select(project_selector.options[1]).run()
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

            approve = next(button for button in app.button if button.label == "\uc774 \uaddc\uc815 \ucd5c\uc885 \ud655\uc815 \u00b7 \uc2b9\uc778\ud558\uace0 \uc0c9\uc778")
            self.assertTrue(approve.disabled)
            self.assertNotIn(
                "approval:doc_streamlit_approval:chunk-streamlit:human_confirmed",
                app.session_state.filtered_state,
            )
            _confirm_rendered_approval_rows(app)
            approve = next(button for button in app.button if button.label == "\uc774 \uaddc\uc815 \ucd5c\uc885 \ud655\uc815 \u00b7 \uc2b9\uc778\ud558\uace0 \uc0c9\uc778")
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
            next(area for area in app.text_area if area.label == "제안 내용 수정").set_value(edited).run()
            reflect_keys = [
                button.key
                for button in app.button
                if button.label == "수정 필요로 판단"
            ]
            for key in reflect_keys:
                next(button for button in app.button if button.key == key).click().run()
            human_keys = [
                checkbox.key
                for checkbox in app.checkbox
                if checkbox.label
                == "원본과 최종본을 직접 대조했고, 이 내용으로 승인·색인하는 데 동의합니다."
            ]
            for key in human_keys:
                next(checkbox for checkbox in app.checkbox if checkbox.key == key).set_value(
                    True
                ).run()
            self.assertTrue(
                all(
                    not area.value
                    for area in app.text_area
                    if area.label == "수정 필요 항목 처리 메모"
                )
            )
            next(button for button in app.button if button.label == "이 규정 최종 확정 · 승인하고 색인").click().run()

            saved = JsonRepository(settings).get_chunks("doc_streamlit_approval")[0]

        self.assertEqual(edited, saved.text)
        self.assertEqual(edited, saved.normalized_text)
        self.assertEqual(edited, saved.retrieval_text)
        self.assertTrue(saved.metadata["human_review_edited"])
        self.assertEqual(64, len(saved.metadata["human_review_original_sha256"]))

    def test_action_required_needs_edit_or_resolution_note_before_approval(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            _seed_streamlit_approval_document(settings)
            set_runtime_settings_overrides(
                data_dir=settings.data_dir,
                artifact_root=settings.artifact_root,
            )
            self.addCleanup(clear_runtime_settings_overrides)

            app = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=20,
            )
            _seed_app_institution_context(app)
            app.session_state["document_id"] = "doc_streamlit_approval"
            app.session_state["nav_page"] = "③ 검수하고 승인"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            reflect_keys = [
                button.key
                for button in app.button
                if button.label == "수정 필요로 판단"
            ]
            for key in reflect_keys:
                next(button for button in app.button if button.key == key).click().run()
            human_checks = [
                checkbox
                for checkbox in app.checkbox
                if checkbox.label
                == "원본과 최종본을 직접 대조했고, 이 내용으로 승인·색인하는 데 동의합니다."
            ]
            blocked_approve = next(
                button
                for button in app.button
                if button.label == "이 규정 최종 확정 · 승인하고 색인"
            )
            self.assertTrue(all(checkbox.disabled for checkbox in human_checks))
            self.assertTrue(blocked_approve.disabled)

            note_keys = [
                area.key
                for area in app.text_area
                if area.label == "수정 필요 항목 처리 메모"
            ]
            for key in note_keys:
                next(area for area in app.text_area if area.key == key).set_value(
                    "원문 표와 대조해 현재 최종본이 맞음을 확인"
                ).run()
            human_keys = [
                checkbox.key
                for checkbox in app.checkbox
                if checkbox.label
                == "원본과 최종본을 직접 대조했고, 이 내용으로 승인·색인하는 데 동의합니다."
            ]
            for key in human_keys:
                next(checkbox for checkbox in app.checkbox if checkbox.key == key).set_value(
                    True
                ).run()
            resolved_approve = next(
                button
                for button in app.button
                if button.label == "이 규정 최종 확정 · 승인하고 색인"
            )

        self.assertFalse(app.exception)
        self.assertTrue(note_keys)
        self.assertFalse(resolved_approve.disabled)

    def test_edit_after_confirmation_invalidates_sign_off(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            _seed_streamlit_approval_document(settings)
            set_runtime_settings_overrides(
                data_dir=settings.data_dir,
                artifact_root=settings.artifact_root,
            )
            self.addCleanup(clear_runtime_settings_overrides)

            app = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=20,
            )
            _seed_app_institution_context(app)
            app.session_state["document_id"] = "doc_streamlit_approval"
            app.session_state["nav_page"] = "③ 검수하고 승인"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()
            _confirm_rendered_approval_rows(app)
            ready_button = next(
                button
                for button in app.button
                if button.label == "이 규정 최종 확정 · 승인하고 색인"
            )
            self.assertFalse(ready_button.disabled)

            next(
                area for area in app.text_area if area.label == "제안 내용 수정"
            ).set_value("확인 뒤 다시 고친 본문").run()
            blocked_button = next(
                button
                for button in app.button
                if button.label == "이 규정 최종 확정 · 승인하고 색인"
            )

        self.assertTrue(blocked_button.disabled)
        self.assertFalse(
            app.session_state[
                "approval:doc_streamlit_approval:chunk-streamlit:human_confirmed"
            ]
        )

    def test_paginated_approval_requires_confirmation_for_unseen_row(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            _seed_streamlit_many_chunk_document(settings, chunk_count=26)
            set_runtime_settings_overrides(
                data_dir=settings.data_dir,
                artifact_root=settings.artifact_root,
            )
            self.addCleanup(clear_runtime_settings_overrides)

            app = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=30,
            )
            _seed_app_institution_context(app)
            app.session_state["document_id"] = "doc_streamlit_many"
            app.session_state["nav_page"] = "③ 검수하고 승인"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            self.assertEqual(
                25,
                len([area for area in app.text_area if area.label == "제안 내용 수정"]),
            )
            self.assertNotIn(
                "approval:doc_streamlit_many:chunk-many-26:human_confirmed",
                app.session_state.filtered_state,
            )
            first_page_confirmations = [
                checkbox
                for checkbox in app.checkbox
                if checkbox.label
                == "원본과 최종본을 직접 대조했고, 이 내용으로 승인·색인하는 데 동의합니다."
            ]
            self.assertEqual(25, len(first_page_confirmations))
            for checkbox in first_page_confirmations:
                checkbox.set_value(True)
            app.run()
            for index in range(1, 26):
                self.assertTrue(
                    app.session_state[
                        f"approval:doc_streamlit_many:chunk-many-{index:02d}:human_confirmed"
                    ]
                )
            first_page_button = next(
                button
                for button in app.button
                if button.label == "이 규정 최종 확정 · 승인하고 색인"
            )
            self.assertTrue(first_page_button.disabled)

            page_control = next(
                control
                for control in app.number_input
                if str(control.label).startswith("검증 시트 쪽")
            )
            page_control.set_value(2).run()
            self.assertEqual(
                1,
                len([area for area in app.text_area if area.label == "제안 내용 수정"]),
            )
            second_page_confirmation = next(
                checkbox
                for checkbox in app.checkbox
                if checkbox.label
                == "원본과 최종본을 직접 대조했고, 이 내용으로 승인·색인하는 데 동의합니다."
            )
            second_page_confirmation.set_value(True).run()
            all_pages_button = next(
                button
                for button in app.button
                if button.label == "이 규정 최종 확정 · 승인하고 색인"
            )
            next(
                control
                for control in app.number_input
                if str(control.label).startswith("검증 시트 쪽")
            ).set_value(1).run()
            first_page_after_round_trip = [
                checkbox
                for checkbox in app.checkbox
                if checkbox.label
                == "원본과 최종본을 직접 대조했고, 이 내용으로 승인·색인하는 데 동의합니다."
            ]

        self.assertFalse(app.exception)
        self.assertFalse(all_pages_button.disabled)
        self.assertEqual(25, len(first_page_after_round_trip))
        self.assertTrue(all(checkbox.value for checkbox in first_page_after_round_trip))

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

            next(button for button in app.button if button.label == "④ Qwen 규정 챗봇·AI 연결로 이동").click().run()

        self.assertEqual("④ Qwen 규정 챗봇·AI 연결", app.session_state["nav_page"])
        self.assertNotIn("workflow_transition_state", app.session_state.filtered_state)
        self.assertFalse(app.exception)

    def test_beginner_mode_blocks_mcp_navigation_until_approval_and_index_complete(self) -> None:
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
            app.session_state["beginner_guide_choice_made"] = True
            app.session_state["beginner_guide_enabled"] = True
            app.session_state["beginner_guide_step"] = 3
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            results_button = next(
                button
                for button in app.button
                if button.label == "현재 규정의 결과 두 곳 확인하러 가기"
            )
            next(
                radio for radio in app.radio if radio.label == "기본 작업 순서"
            ).set_value("④ Qwen 규정 챗봇·AI 연결").run()

        self.assertFalse(results_button.disabled)
        self.assertFalse(
            any(button.label == "④ Qwen 규정 챗봇·AI 연결로 이동" for button in app.button)
        )
        self.assertNotEqual("④ Qwen 규정 챗봇·AI 연결", app.session_state["nav_page"])
        self.assertFalse(app.exception)

    def test_approval_tabs_approve_only_reviewed_compare_chunk(self) -> None:
        # The continuous-scroll screen approves the open regulation only after every
        # pending row has an explicit review decision and human confirmation.
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

            approve = next(button for button in app.button if button.label == "\uc774 \uaddc\uc815 \ucd5c\uc885 \ud655\uc815 \u00b7 \uc2b9\uc778\ud558\uace0 \uc0c9\uc778")
            self.assertTrue(approve.disabled)
            _confirm_rendered_approval_rows(app)
            approve = next(button for button in app.button if button.label == "\uc774 \uaddc\uc815 \ucd5c\uc885 \ud655\uc815 \u00b7 \uc2b9\uc778\ud558\uace0 \uc0c9\uc778")
            approve.click().run()

            chunks = {chunk.chunk_id: chunk for chunk in JsonRepository(settings).get_chunks("doc_streamlit_approval")}
            approvals = JsonRepository(settings).list_approval_records("doc_streamlit_approval")

        self.assertEqual("approved", chunks["chunk-streamlit"].approval_status)
        self.assertEqual("approved", chunks["chunk-streamlit-second"].approval_status)
        self.assertEqual(
            {"chunk-streamlit", "chunk-streamlit-second"},
            {chunk_id for record in approvals for chunk_id in record["chunk_ids"]},
        )

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

            approve = next(button for button in app.button if button.label == "\uc774 \uaddc\uc815 \ucd5c\uc885 \ud655\uc815 \u00b7 \uc2b9\uc778\ud558\uace0 \uc0c9\uc778")
            self.assertTrue(approve.disabled)
            _confirm_rendered_approval_rows(app)
            approve = next(button for button in app.button if button.label == "\uc774 \uaddc\uc815 \ucd5c\uc885 \ud655\uc815 \u00b7 \uc2b9\uc778\ud558\uace0 \uc0c9\uc778")
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

    def test_override_approval_records_only_approved_without_review_event(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            _seed_streamlit_approval_document(settings)
            set_runtime_settings_overrides(
                data_dir=settings.data_dir,
                artifact_root=settings.artifact_root,
            )
            self.addCleanup(clear_runtime_settings_overrides)

            app = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=20,
            )
            _seed_app_institution_context(app)
            app.session_state["document_id"] = "doc_streamlit_approval"
            app.session_state["nav_page"] = "③ 검수하고 승인"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            reason = "별도 결재 문서에서 원문 대조를 완료한 긴급 배포"
            next(
                area for area in app.text_area if area.label == "확인 생략 승인 사유"
            ).set_value(reason).run()
            approve = next(
                button
                for button in app.button
                if button.label == "이 규정 최종 확정 · 승인하고 색인"
            )
            self.assertFalse(approve.disabled)
            approve.click().run()

            approvals = JsonRepository(settings).list_approval_records(
                "doc_streamlit_approval"
            )
            review_events = [
                event
                for record in approvals
                for event in record.get("review_decision_events", [])
                if isinstance(event, dict)
            ]

        self.assertFalse(app.exception)
        self.assertEqual(["approved_without_review"], [event["event"] for event in review_events])
        self.assertEqual(reason, review_events[0]["override_reason"])
        self.assertFalse(approvals[0]["human_review_confirmed"])
        self.assertFalse(approvals[0]["ai_review_confirmed"])
        self.assertEqual(reason, approvals[0]["approval_override_reason"])

    def test_beginner_mode_does_not_expose_approval_override(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            _seed_streamlit_approval_document(settings)
            set_runtime_settings_overrides(
                data_dir=settings.data_dir,
                artifact_root=settings.artifact_root,
            )
            self.addCleanup(clear_runtime_settings_overrides)

            app = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=20,
            )
            _seed_app_institution_context(app)
            app.session_state["document_id"] = "doc_streamlit_approval"
            app.session_state["nav_page"] = "③ 검수하고 승인"
            app.session_state["beginner_guide_enabled"] = True
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            override_areas = [
                area
                for area in app.text_area
                if area.label == "확인 생략 승인 사유"
            ]

        self.assertFalse(app.exception)
        self.assertEqual([], override_areas)

    def test_bulk_confirm_preserves_chunk_state_and_allows_remaining_review(self) -> None:
        # Every pending chunk renders with independent editable text and sign-off.
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
            app.session_state["nav_page"] = "③ 검수하고 승인"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            proposal_areas = [area for area in app.text_area if area.label == "제안 내용 수정"]
            self.assertEqual(2, len(proposal_areas))
            proposal_areas[0].set_value("첫 번째 청크만 고친 내용").run()

            proposal_areas_after_edit = [area for area in app.text_area if area.label == "제안 내용 수정"]
            values = {area.value for area in proposal_areas_after_edit}
            self.assertIn("첫 번째 청크만 고친 내용", values)
            self.assertIn("second draft content", values)

            self.assertFalse(app.exception)
            approve = next(button for button in app.button if button.label == "이 규정 최종 확정 · 승인하고 색인")
            self.assertTrue(approve.disabled)
            _confirm_rendered_approval_rows(app)
            self.assertTrue(
                app.session_state["approval:doc_streamlit_approval:chunk-streamlit:human_confirmed"]
            )
            self.assertTrue(
                app.session_state["approval:doc_streamlit_approval:chunk-streamlit-second:human_confirmed"]
            )
            approve = next(button for button in app.button if button.label == "이 규정 최종 확정 · 승인하고 색인")
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
                        # Processing-run journal entries are immutable. Seed a
                        # later completed run instead of rewriting the original
                        # run identity solely to prepare this UI fixture.
                        "run_id": "run-streamlit-approval-remaining-review",
                        "started_at": datetime.now(timezone.utc),
                        "completed_at": datetime.now(timezone.utc),
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

            self.assertNotIn(
                "approval:doc_streamlit_approval:chunk-streamlit:ai_decisions",
                app.session_state.filtered_state,
            )
            self.assertNotIn(
                "approval:doc_streamlit_approval:chunk-streamlit:human_confirmed",
                app.session_state.filtered_state,
            )
            _confirm_rendered_approval_rows(app)
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
        self.assertEqual(2, list(decisions.values()).count("reflect"))
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
            app.session_state["nav_page"] = "③ 검수하고 승인"
            app.session_state["approval-selected-chunk-ids-doc_streamlit_approval"] = ["chunk-streamlit-second"]
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            approve = next(button for button in app.button if button.label == "이 규정 최종 확정 · 승인하고 색인")
            self.assertTrue(approve.disabled)
            _confirm_rendered_approval_rows(app)
            approve = next(button for button in app.button if button.label == "이 규정 최종 확정 · 승인하고 색인")
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
        # There is no more single "selected chunk" concept. Already-approved chunks
        # are filtered out of the continuous scroll entirely, so only the still-draft
        # chunk gets an editable proposal box.
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
            app.session_state["nav_page"] = "③ 검수하고 승인"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            proposal_areas = [area for area in app.text_area if area.label == "제안 내용 수정"]

        self.assertEqual(1, len(proposal_areas))
        self.assertEqual("second draft content", proposal_areas[0].value)

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
            # 디렉터리에서 연 규정만 상세 렌더링되고, 전체 규정 상태는 명시적으로 불러온 뒤에만 집계된다.
            app.session_state["workflow_opened_document_id"] = "doc_streamlit_approval"
            # '전체 규정 확인'을 켠 뒤에만 일괄 검수·확정 화면이 열린다.
            app.session_state["approval-bulk-open-doc_streamlit_approval"] = True
            app.session_state["approval-batch-loaded-doc_streamlit_approval"] = True
            app.session_state["approval-bulk-sheet-doc_streamlit_approval"] = True
            app.session_state["nav_page"] = "③ 검수하고 승인"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            approve = next(
                button
                for button in app.button
                if button.label == "전체 규정 최종 확정 · 선택한 2개 승인·색인"
            )
            self.assertTrue(approve.disabled)
            _confirm_rendered_approval_rows(app)
            approve = next(
                button
                for button in app.button
                if button.label == "전체 규정 최종 확정 · 선택한 2개 승인·색인"
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

    def test_opened_regulation_button_approves_only_that_regulation(self) -> None:
        """규정 하나를 열면 '이 규정 최종 확정'은 그 규정 조항만 승인한다."""
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            _seed_streamlit_regulation_bundle_document(settings)
            set_runtime_settings_overrides(data_dir=settings.data_dir, artifact_root=settings.artifact_root)
            self.addCleanup(clear_runtime_settings_overrides)

            app = AppTest.from_file(str(REPO_ROOT / "frontend" / "streamlit_app.py"), default_timeout=40)
            _seed_app_institution_context(app)
            app.session_state["document_id"] = "doc_streamlit_bundle"
            app.session_state["workflow_opened_document_id"] = "doc_streamlit_bundle"
            app.session_state["approval-regulation-unit-doc_streamlit_bundle"] = "제1호|인사규정"
            app.session_state["nav_page"] = "③ 검수하고 승인"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            _confirm_rendered_approval_rows(app)
            next(
                button for button in app.button if button.label == "이 규정 최종 확정 · 승인하고 색인"
            ).click().run()

            chunks = {chunk.chunk_id: chunk for chunk in JsonRepository(settings).get_chunks("doc_streamlit_bundle")}

        self.assertFalse(app.exception)
        self.assertEqual("approved", chunks["chunk-bundle-personnel"].approval_status)
        self.assertNotEqual("approved", chunks["chunk-bundle-service"].approval_status)

    def test_bundle_file_approves_every_regulation_from_the_opened_one(self) -> None:
        """규정 하나를 열어 둔 채로도 옆 버튼 한 번에 파일 전체 규정을 승인·색인한다."""
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            _seed_streamlit_regulation_bundle_document(settings)
            set_runtime_settings_overrides(data_dir=settings.data_dir, artifact_root=settings.artifact_root)
            self.addCleanup(clear_runtime_settings_overrides)

            app = AppTest.from_file(str(REPO_ROOT / "frontend" / "streamlit_app.py"), default_timeout=40)
            _seed_app_institution_context(app)
            app.session_state["document_id"] = "doc_streamlit_bundle"
            app.session_state["workflow_opened_document_id"] = "doc_streamlit_bundle"
            # 인사규정만 열어 둔 상태에서는 복무규정 조항을 확인하지 않았으므로 차단된다.
            app.session_state["approval-regulation-unit-doc_streamlit_bundle"] = "제1호|인사규정"
            app.session_state["nav_page"] = "③ 검수하고 승인"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            approve_all = next(
                button
                for button in app.button
                if button.label == "이 파일의 전체 규정 2개 최종 확정 · 승인하고 색인"
            )
            self.assertTrue(approve_all.disabled)
            _confirm_rendered_approval_rows(app)
            app.session_state["approval-regulation-unit-doc_streamlit_bundle"] = "제2호|복무규정"
            app.run()
            _confirm_rendered_approval_rows(app)
            approve_all = next(
                button
                for button in app.button
                if button.label == "이 파일의 전체 규정 2개 최종 확정 · 승인하고 색인"
            )
            self.assertFalse(approve_all.disabled)
            approve_all.click().run()

            chunks = JsonRepository(settings).get_chunks("doc_streamlit_bundle")

        self.assertFalse(app.exception)
        self.assertTrue(all(chunk.approval_status == "approved" for chunk in chunks))
        # 규정 경계는 그대로 남아야 한다. 통합본을 한 규정으로 합쳐 승인하는 것이 아니다.
        self.assertEqual(
            {"인사규정", "복무규정"},
            {str(chunk.metadata.get("regulation_title")) for chunk in chunks},
        )

    def test_single_regulation_file_hides_the_whole_file_approval_button(self) -> None:
        """규정이 하나뿐인 파일에서는 '이 규정' 버튼이 이미 파일 전체라 옆 버튼을 만들지 않는다."""
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

            labels = [button.label for button in app.button]

        self.assertIn("이 규정 최종 확정 · 승인하고 색인", labels)
        self.assertFalse([label for label in labels if label.startswith("이 파일의 전체 규정")])

    def test_bulk_review_sheet_compares_and_edits_every_selected_regulation(self) -> None:
        """'전체 규정 확인'은 규정 경계를 유지한 채 모든 미승인 조항을 한 화면에서 검수한다."""
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
            app.session_state["workflow_opened_document_id"] = "doc_streamlit_approval"
            app.session_state["approval-bulk-open-doc_streamlit_approval"] = True
            app.session_state["approval-batch-loaded-doc_streamlit_approval"] = True
            app.session_state["approval-bulk-sheet-doc_streamlit_approval"] = True
            app.session_state["nav_page"] = "③ 검수하고 승인"
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            # 열어 둔 규정의 조항이 두 번 그려지지 않아야 한다(편집 칸 중복 키 방지).
            proposal_areas = [area for area in app.text_area if area.label == "제안 내용 수정"]
            self.assertEqual(2, len(proposal_areas))
            row_labels = [
                str(markdown.value)
                for markdown in app.markdown
                if str(markdown.value).startswith("**") and "제1조" in str(markdown.value)
            ]
            self.assertTrue(any("인사규정" in label for label in row_labels))
            self.assertTrue(any("복무규정" in label for label in row_labels))
            decision_keys = [
                str(button.key or "")
                for button in app.button
                if button.label == "수정 필요로 판단"
            ]
            self.assertEqual(2, len(decision_keys))
            self.assertTrue(any("chunk-streamlit:" in key for key in decision_keys))
            self.assertTrue(any("chunk-streamlit-service:" in key for key in decision_keys))

            edited = "전체 규정 확인에서 고친 복무규정 본문"
            service_area = next(
                area
                for area in proposal_areas
                if "doc_streamlit_service" in str(area.key or "")
            )
            service_area.set_value(edited).run()
            _confirm_rendered_approval_rows(app)
            next(
                button
                for button in app.button
                if button.label == "전체 규정 최종 확정 · 선택한 2개 승인·색인"
            ).click().run()

            repository = JsonRepository(settings)
            service_chunks = repository.get_chunks("doc_streamlit_service")
            personnel_chunks = repository.get_chunks("doc_streamlit_approval")

        self.assertFalse(app.exception)
        self.assertEqual(edited, service_chunks[0].text)
        self.assertTrue(all(chunk.approval_status == "approved" for chunk in service_chunks))
        self.assertTrue(all(chunk.approval_status == "approved" for chunk in personnel_chunks))


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


def _seed_streamlit_many_chunk_document(
    settings: Settings,
    *,
    chunk_count: int,
) -> None:
    repository = JsonRepository(settings)
    repository.upsert_document(
        Document(
            document_id="doc_streamlit_many",
            filename="many.pdf",
            document_name="다중 조항 규정",
            file_type="pdf",
            file_hash="many-hash",
            tenant_id="default",
            status="completed",
            institution_name="테스트기관",
            source_system="LOCAL",
            source_url="https://example.test/many.pdf",
            profile_id="test-profile",
        )
    )
    repository.save_processing_result(
        "doc_streamlit_many",
        [],
        [
            Chunk(
                chunk_id=f"chunk-many-{index:02d}",
                document_id="doc_streamlit_many",
                chunk_type="article",
                text=f"제{index}조 본문",
                retrieval_text=f"제{index}조 본문",
                metadata={"raw_text": f"제{index}조 원본"},
            )
            for index in range(1, chunk_count + 1)
        ],
        [],
    )
    now = datetime.now(timezone.utc)
    repository.upsert_run(
        ProcessingRun(
            run_id="run-streamlit-many",
            document_id="doc_streamlit_many",
            job_id="job-streamlit-many",
            tenant_id="default",
            status="completed",
            started_at=now,
            completed_at=now,
            elapsed_seconds=0.1,
            stats={},
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


def _seed_streamlit_regulation_bundle_document(settings: Settings) -> None:
    """규정 두 개가 한 파일에 들어 있는 규정집 통합본을 만든다."""
    repository = JsonRepository(settings)
    repository.upsert_document(
        Document(
            document_id="doc_streamlit_bundle",
            filename="bundle.pdf",
            document_name="규정집 통합본",
            file_type="pdf",
            file_hash="bundle-hash",
            tenant_id="default",
            status="completed",
            institution_name="테스트기관",
            source_system="LOCAL",
            source_url="https://example.test/bundle.pdf",
            profile_id="test-profile",
        )
    )
    repository.save_processing_result(
        "doc_streamlit_bundle",
        [],
        [
            Chunk(
                chunk_id="chunk-bundle-personnel",
                document_id="doc_streamlit_bundle",
                chunk_type="article",
                text="인사규정 제1조 본문",
                retrieval_text="인사규정 제1조 본문",
                metadata={
                    "raw_text": "인사규정 제1조 원본",
                    "regulation_no": "제1호",
                    "regulation_title": "인사규정",
                },
            ),
            Chunk(
                chunk_id="chunk-bundle-service",
                document_id="doc_streamlit_bundle",
                chunk_type="article",
                text="복무규정 제1조 본문",
                retrieval_text="복무규정 제1조 본문",
                metadata={
                    "raw_text": "복무규정 제1조 원본",
                    "regulation_no": "제2호",
                    "regulation_title": "복무규정",
                },
            ),
        ],
        [],
    )
    now = datetime.now(timezone.utc)
    repository.upsert_run(
        ProcessingRun(
            run_id="run-streamlit-bundle",
            document_id="doc_streamlit_bundle",
            job_id="job-streamlit-bundle",
            tenant_id="default",
            status="completed",
            started_at=now,
            completed_at=now,
            elapsed_seconds=0.1,
            stats={},
        )
    )


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
    now = datetime.now(timezone.utc)
    repository.upsert_run(
        ProcessingRun(
            run_id="run-streamlit-service",
            document_id="doc_streamlit_service",
            job_id="job-streamlit-service",
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
                            "chunk_id": "chunk-streamlit-service",
                            "chunk_type": "article",
                            "reasons": ["summary_only_service_check"],
                        }
                    ],
                }
            },
        )
    )


class ApprovalCompareSheetVisibilityTests(unittest.TestCase):
    """'전체 규정 확인'을 켜도 비교 시트가 사라지지 않아야 한다.

    아래 전체 목록은 '상태 불러오기' 버튼과 별도 체크박스를 더 눌러야 나온다. 체크박스를
    켰다는 이유만으로 위 시트를 감추면, 그 사이 ③ 화면에는 원본·전처리본·AI 검수 의견이
    하나도 남지 않는다.
    """

    def test_sheet_is_hidden_only_when_the_bulk_list_actually_draws_it(self) -> None:
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        # 감추는 조건은 아래 목록이 실제로 그려지는 것과 같아야 한다.
        self.assertIn(
            "bulk_section_open and batch_loaded and bool(st.session_state.get(bulk_sheet_key))",
            source,
        )
        self.assertIn("if bulk_sheet_rendered:", source)
        # 켜졌다는 사실만 보고 감추면 안 된다.
        self.assertNotIn("    if bulk_section_open:\n", source)
        # 아래 체크박스와 같은 키를 봐야 두 조건이 어긋나지 않는다.
        self.assertIn("key=bulk_sheet_key,", source)


if __name__ == "__main__":
    unittest.main()
