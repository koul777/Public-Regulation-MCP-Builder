from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings, clear_runtime_settings_overrides, set_runtime_settings_overrides
from app.storage.repository import JsonRepository
from frontend import streamlit_app
from tests import test_streamlit_approval_app as approval_test_support
from tests.test_streamlit_approval_app import (
    AppTest,
    REPO_ROOT,
    _seed_app_institution_context,
    _seed_streamlit_multi_approval_documents,
    _seed_streamlit_regulation_bundle_document,
)


class StreamlitBulkApprovalBlockerAppTests(unittest.TestCase):
    def setUp(self) -> None:
        approval_test_support.StreamlitApprovalAppTests.setUp(self)
        self.addCleanup(clear_runtime_settings_overrides)
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

    def _app(self, settings: Settings, document_id: str, *, beginner: bool = False):
        set_runtime_settings_overrides(data_dir=settings.data_dir, artifact_root=settings.artifact_root)
        app = AppTest.from_file(str(REPO_ROOT / "frontend" / "streamlit_app.py"), default_timeout=40)
        _seed_app_institution_context(app)
        app.session_state["document_id"] = document_id
        app.session_state["workflow_opened_document_id"] = document_id
        app.session_state["nav_page"] = "③ 검수하고 승인"
        app.session_state["beginner_guide_choice_made"] = True
        app.session_state["beginner_guide_enabled"] = beginner
        app.session_state["ai_connection_overrides"] = {
            "data_dir": settings.data_dir, "artifact_root": settings.artifact_root,
        }
        return app

    def _mark_blocked(self, repository, document_id):
        chunks = repository.get_chunks(document_id)
        chunks[0].metadata["ambiguous_combined_book_boundary"] = True
        repository.save_chunks(document_id, chunks)
        return [chunk.model_dump(mode="json") for chunk in repository.get_chunks(document_id)]

    def _assert_no_approval_mutation(self, repository, document_id, before, settings):
        self.assertEqual(before, [chunk.model_dump(mode="json") for chunk in repository.get_chunks(document_id)])
        self.assertEqual([], repository.list_approval_records(document_id))
        self.assertEqual([], repository.list_indexing_jobs(document_id))
        self.assertFalse((settings.artifact_root / "reports").exists())

    def test_current_and_whole_file_buttons_block_before_saving_edits(self) -> None:
        for key in ("approval-approve-index-doc_streamlit_bundle", "approval-approve-index-all-doc_streamlit_bundle"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
                _seed_streamlit_regulation_bundle_document(settings)
                repository = JsonRepository(settings)
                before = self._mark_blocked(repository, "doc_streamlit_bundle")
                app = self._app(settings, "doc_streamlit_bundle")
                app.session_state["approval-regulation-unit-doc_streamlit_bundle"] = "제1호|인사규정"
                edit_key = streamlit_app._approval_edited_text_key("doc_streamlit_bundle", "chunk-bundle-personnel")
                app.session_state[edit_key] = "아직 저장하지 않은 합성 수정안"
                app.run()
                self.assertFalse(app.exception)
                button = app.button(key=key)
                self.assertTrue(button.disabled)
                self.assertTrue(any("규정 경계" in item.value and "조항 1개" in item.value for item in app.error))
                self.assertTrue(any(item.label == "같은 원본 다시 올려 전처리하기" for item in app.button))
                # Disabled controls cannot be clicked by a browser or AppTest.
                # A normal rerun must not mutate approval state either.
                with patch("app.api.routes_documents.approve_review_chunks") as approve:
                    app.run()
                approve.assert_not_called()
                self.assertFalse(app.exception)
                self._assert_no_approval_mutation(repository, "doc_streamlit_bundle", before, settings)
                self.assertEqual("아직 저장하지 않은 합성 수정안", app.session_state[edit_key])
                with (
                    patch("app.services.document_service.DocumentService.upload_stream") as upload,
                    patch("app.services.processing_service.ProcessingService.process") as process,
                ):
                    app.button(key="approval-boundary-upload-doc_streamlit_bundle").click().run()
                upload.assert_not_called()
                process.assert_not_called()
                self.assertFalse(app.exception)
                self.assertEqual("① 문서 올려서 전처리", app.session_state["nav_page"])
                self.assertTrue(app.get("file_uploader"))

    def test_beginner_shortcut_blocks_even_after_unreviewed_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            _seed_streamlit_regulation_bundle_document(settings)
            repository = JsonRepository(settings)
            before = self._mark_blocked(repository, "doc_streamlit_bundle")
            app = self._app(settings, "doc_streamlit_bundle", beginner=True)
            app.session_state["approval-bulk-finish-ack-doc_streamlit_bundle"] = True
            app.run()
            self.assertFalse(app.exception)
            button = app.button(key="approval-bulk-finish-doc_streamlit_bundle")
            self.assertTrue(button.disabled)
            with patch("app.api.routes_documents.approve_review_chunks") as approve:
                app.run()
            approve.assert_not_called()
            self.assertFalse(app.exception)
            self._assert_no_approval_mutation(repository, "doc_streamlit_bundle", before, settings)

    def test_advanced_manual_approval_cannot_bypass_the_boundary_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            _seed_streamlit_regulation_bundle_document(settings)
            repository = JsonRepository(settings)
            before = self._mark_blocked(repository, "doc_streamlit_bundle")
            app = self._app(settings, "doc_streamlit_bundle")
            app.session_state["show-advanced-approval-doc_streamlit_bundle"] = True
            for field in (
                "approval-worklist-path", "approval-worklist-sha256",
                "approval-review-batch-manifest-path", "approval-review-batch-manifest-sha256",
                "approval-review-batch", "approval-review-batch-fingerprint",
            ):
                app.session_state[f"{field}-doc_streamlit_bundle"] = "synthetic-evidence"
            app.session_state["review-flags-ack-doc_streamlit_bundle"] = True
            app.run()
            self.assertFalse(app.exception)
            button = app.button(key="approve-all-doc_streamlit_bundle")
            self.assertTrue(button.disabled)
            with patch("app.api.routes_documents.approve_review_chunks") as approve:
                app.run()
            approve.assert_not_called()
            self.assertFalse(app.exception)
            self._assert_no_approval_mutation(repository, "doc_streamlit_bundle", before, settings)

    def test_multi_document_blocker_prevents_mutation_of_the_first_normal_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            _seed_streamlit_multi_approval_documents(settings)
            repository = JsonRepository(settings)
            blocked_before = self._mark_blocked(repository, "doc_streamlit_service")
            normal_before = [chunk.model_dump(mode="json") for chunk in repository.get_chunks("doc_streamlit_approval")]
            app = self._app(settings, "doc_streamlit_approval")
            ids = ["doc_streamlit_approval", "doc_streamlit_service"]
            app.session_state["workflow_document_ids"] = ids
            app.session_state["workflow_selected_document_ids"] = ids
            for document_id in ids:
                app.session_state[f"workflow-document-selected-{document_id}"] = True
            app.session_state["approval-bulk-open-doc_streamlit_approval"] = True
            app.session_state["approval-batch-loaded-doc_streamlit_approval"] = True
            app.run()
            self.assertFalse(app.exception)
            self.assertFalse(app.button(key="approval-approve-index-doc_streamlit_approval").disabled)
            button = app.button(key="workflow-approve-index-doc_streamlit_approval")
            self.assertTrue(button.disabled)
            with patch("app.api.routes_documents.approve_review_chunks") as approve:
                app.run()
            approve.assert_not_called()
            self.assertFalse(app.exception)
            self._assert_no_approval_mutation(repository, "doc_streamlit_approval", normal_before, settings)
            self._assert_no_approval_mutation(repository, "doc_streamlit_service", blocked_before, settings)


if __name__ == "__main__":
    unittest.main()
