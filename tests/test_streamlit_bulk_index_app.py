from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api import routes_documents
from app.core.config import Settings, clear_runtime_settings_overrides, set_runtime_settings_overrides
from app.storage.repository import JsonRepository
from tests import test_streamlit_approval_app as approval_support
from tests.test_streamlit_approval_app import (
    AppTest,
    REPO_ROOT,
    _seed_app_institution_context,
    _seed_streamlit_multi_approval_documents,
    _seed_streamlit_regulation_bundle_document,
)


class StreamlitBulkIndexAppTests(unittest.TestCase):
    """Exercise the visible batch entry without pre-opening hidden UI state."""

    def setUp(self) -> None:
        approval_support.StreamlitApprovalAppTests.setUp(self)
        self.addCleanup(clear_runtime_settings_overrides)
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = Settings(
            data_dir=self.root / "data",
            artifact_root=self.root,
            tenant_storage_isolation=False,
        )
        set_runtime_settings_overrides(
            data_dir=self.settings.data_dir,
            artifact_root=self.settings.artifact_root,
            tenant_storage_isolation=False,
        )
        self.repository = JsonRepository(self.settings)
        self.document_ids = ["doc_streamlit_approval", "doc_streamlit_service"]

    def _app(self, document_ids: list[str], *, beginner: bool = True):
        app = AppTest.from_file(str(REPO_ROOT / "frontend" / "streamlit_app.py"), default_timeout=60)
        _seed_app_institution_context(app)
        app.session_state["document_id"] = document_ids[0]
        app.session_state["workflow_opened_document_id"] = document_ids[0]
        app.session_state["workflow_document_ids"] = document_ids
        app.session_state["workflow_selected_document_ids"] = document_ids
        for document_id in document_ids:
            app.session_state[f"workflow-document-selected-{document_id}"] = True
        app.session_state["nav_page"] = "③ 검수하고 승인"
        app.session_state["beginner_guide_choice_made"] = True
        app.session_state["beginner_guide_enabled"] = beginner
        app.session_state["ai_connection_overrides"] = {
            "data_dir": self.settings.data_dir,
            "artifact_root": self.settings.artifact_root,
            "tenant_storage_isolation": False,
        }
        return app

    def _snapshot(self, document_ids: list[str]) -> dict:
        return {
            document_id: {
                "chunks": [chunk.model_dump(mode="json") for chunk in self.repository.get_chunks(document_id)],
                "approvals": self.repository.list_approval_records(document_id),
                "jobs": self.repository.list_indexing_jobs(document_id),
            }
            for document_id in document_ids
        }

    def _enter_batch(self, app, document_ids: list[str]) -> None:
        before = self._snapshot(document_ids)
        app.run()
        self.assertFalse(app.exception)
        entry = app.button(key=f"approval-batch-entry-{document_ids[0]}")
        self.assertEqual("전체 규정 한꺼번에 처리", entry.label)
        entry.click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.session_state[f"approval-batch-active-{document_ids[0]}"])
        self.assertEqual(before, self._snapshot(document_ids), "Opening the batch panel must not approve or index.")

    def _acknowledge_unreviewed(self, app, document_ids: list[str]) -> None:
        before = self._snapshot(document_ids)
        button_key = f"workflow-approve-index-{document_ids[0]}"
        self.assertTrue(app.button(key=button_key).disabled)
        app.checkbox(key=f"approval-batch-unreviewed-ack-{document_ids[0]}").check().run()
        self.assertFalse(app.exception)
        self.assertEqual(before, self._snapshot(document_ids), "Acknowledgement alone must not approve or index.")
        self.assertFalse(app.button(key=button_key).disabled)

    def _finish_batch(self, app, opened_document_id: str, expected_index_ids: list[str]) -> None:
        with patch("app.api.routes_documents.index_documents_batch", wraps=routes_documents.index_documents_batch) as batch:
            app.button(key=f"workflow-approve-index-{opened_document_id}").click().run()
        self.assertFalse(app.exception)
        self.assertFalse(app.error, [item.value for item in app.error])
        self.assertEqual(1, batch.call_count)
        self.assertEqual(set(expected_index_ids), set(batch.call_args.args[0]))
        for document_id in expected_index_ids:
            self.assertTrue(all(chunk.approval_status == "approved" for chunk in self.repository.get_chunks(document_id)))
            jobs = self.repository.list_indexing_jobs(document_id)
            self.assertTrue(jobs)
            self.assertEqual("indexed", jobs[-1]["status"])
            self.assertEqual(len(self.repository.get_chunks(document_id)), jobs[-1]["record_count"])

    def _approve_without_index(self, document_ids: list[str]) -> None:
        """Persist real approval journals, with neither an index nor deferred batch IDs."""
        approve_route = routes_documents.approve_review_chunks

        def approve_without_deferral(document_id, request, auth):
            # Model an existing approval made before deferred batch recovery was
            # enabled. Keep the real generated evidence and approval route.
            request = request.model_copy(update={"defer_vector_sync": False, "vector_sync_batch_id": None})
            return approve_route(document_id, request, auth)

        for document_id in document_ids:
            app = self._app([document_id], beginner=False)
            app.run()
            self.assertFalse(app.exception)
            with (
                patch("app.api.routes_documents.index_document", side_effect=RuntimeError("synthetic index unavailable")),
                patch("app.api.routes_documents.approve_review_chunks", side_effect=approve_without_deferral),
            ):
                app.button(key=f"approval-approve-index-{document_id}").click().run()
            self.assertFalse(app.exception)
            self.assertTrue(self.repository.list_approval_records(document_id))
            self.assertTrue(all(chunk.approval_status == "approved" for chunk in self.repository.get_chunks(document_id)))
            self.assertFalse(self.repository.list_indexing_jobs(document_id))
            self.assertEqual([], routes_documents.pending_deferred_vector_sync_batch_ids(self.repository, document_id))

    def test_beginner_multiple_files_can_prepare_acknowledge_and_index_together(self) -> None:
        _seed_streamlit_multi_approval_documents(self.settings)
        app = self._app(self.document_ids)
        self._enter_batch(app, self.document_ids)
        self._acknowledge_unreviewed(app, self.document_ids)
        self._finish_batch(app, self.document_ids[0], self.document_ids)
        for document_id in self.document_ids:
            records = self.repository.list_approval_records(document_id)
            self.assertTrue(records)
            self.assertTrue(all(not record.get("human_review_confirmed") for record in records))
            self.assertTrue(all(record.get("approval_override_reason") for record in records))
        self.assertEqual(
            ["reg-personnel", "reg-service"],
            [self.repository.get_document(document_id).regulation_id for document_id in self.document_ids],
        )

    def test_beginner_combined_file_includes_all_regulations_when_one_is_open(self) -> None:
        _seed_streamlit_regulation_bundle_document(self.settings)
        document_id = "doc_streamlit_bundle"
        chunks = self.repository.get_chunks(document_id)
        chunks.append(chunks[-1].model_copy(deep=True, update={
            "chunk_id": "chunk-bundle-finance",
            "text": "회계규정 제1조 본문",
            "retrieval_text": "회계규정 제1조 본문",
            "metadata": {"raw_text": "회계규정 제1조 원본", "regulation_no": "제3호", "regulation_title": "회계규정"},
        }))
        self.repository.save_chunks(document_id, chunks)
        app = self._app([document_id])
        app.session_state[f"approval-regulation-unit-{document_id}"] = "제1호|인사규정"
        self._enter_batch(app, [document_id])
        self._acknowledge_unreviewed(app, [document_id])
        self._finish_batch(app, document_id, [document_id])
        self.assertEqual(
            {"인사규정", "복무규정", "회계규정"},
            {chunk.metadata["regulation_title"] for chunk in self.repository.get_chunks(document_id)},
        )
        approved_ids = {
            chunk_id
            for record in self.repository.list_approval_records(document_id)
            for chunk_id in record["chunk_ids"]
        }
        self.assertEqual({chunk.chunk_id for chunk in chunks}, approved_ids)

    def test_already_approved_files_without_deferred_batches_are_indexed_without_reapproval(self) -> None:
        _seed_streamlit_multi_approval_documents(self.settings)
        self._approve_without_index(self.document_ids)
        journals = {document_id: self.repository.list_approval_records(document_id) for document_id in self.document_ids}
        app = self._app(self.document_ids)
        self._enter_batch(app, self.document_ids)
        self.assertFalse(any(item.key == f"approval-batch-unreviewed-ack-{self.document_ids[0]}" for item in app.checkbox))
        self.assertFalse(any(item.label == "내용이 맞아요 · 다음" for item in app.button))
        self.assertFalse(app.button(key=f"workflow-approve-index-{self.document_ids[0]}").disabled)
        with patch("app.api.routes_documents.approve_review_chunks", wraps=routes_documents.approve_review_chunks) as approve:
            self._finish_batch(app, self.document_ids[0], self.document_ids)
        approve.assert_not_called()
        self.assertEqual(journals, {document_id: self.repository.list_approval_records(document_id) for document_id in self.document_ids})

    def test_batch_skips_a_healthy_indexed_file(self) -> None:
        _seed_streamlit_multi_approval_documents(self.settings)
        self._approve_without_index(self.document_ids)
        first = self.document_ids[0]
        first_app = self._app([first], beginner=False)
        first_app.run()
        first_app.button(key=f"quick-index-only-{first}").click().run()
        self.assertFalse(first_app.exception)
        self.assertEqual("indexed", self.repository.list_indexing_jobs(first)[-1]["status"])
        before = self._snapshot([first])
        app = self._app(self.document_ids)
        self._enter_batch(app, self.document_ids)
        self._finish_batch(app, first, [self.document_ids[1]])
        self.assertEqual(before, self._snapshot([first]))

    def test_beginner_batch_boundary_blocker_prevents_all_mutation(self) -> None:
        _seed_streamlit_multi_approval_documents(self.settings)
        blocked_id = self.document_ids[1]
        chunks = self.repository.get_chunks(blocked_id)
        chunks[0].metadata["ambiguous_combined_book_boundary"] = True
        self.repository.save_chunks(blocked_id, chunks)
        before = self._snapshot(self.document_ids)
        app = self._app(self.document_ids)
        self._enter_batch(app, self.document_ids)
        acknowledge = [item for item in app.checkbox if item.key == f"approval-batch-unreviewed-ack-{self.document_ids[0]}"]
        if acknowledge:
            acknowledge[0].check().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.button(key=f"workflow-approve-index-{self.document_ids[0]}").disabled)
        self.assertTrue(any("규정 경계" in item.value for item in app.error))
        self.assertEqual(before, self._snapshot(self.document_ids))

    def test_batch_rejects_another_tenant_even_when_profile_identifier_matches(self) -> None:
        _seed_streamlit_multi_approval_documents(self.settings)
        foreign_id = self.document_ids[1]
        foreign = self.repository.get_document(foreign_id)
        self.repository.upsert_document(foreign.model_copy(update={"tenant_id": "another-tenant"}))
        before = self._snapshot(self.document_ids)
        app = self._app(self.document_ids)
        self._enter_batch(app, self.document_ids)
        acknowledge = [item for item in app.checkbox if item.key == f"approval-batch-unreviewed-ack-{self.document_ids[0]}"]
        if acknowledge:
            acknowledge[0].check().run()
        self.assertFalse(app.exception)
        self.assertFalse(any(
            item.key == f"workflow-approve-index-{self.document_ids[0]}" and not item.disabled
            for item in app.button
        ))
        messages = [item.value for item in (*app.error, *app.warning)]
        self.assertTrue(any("기관" in value or "테넌트" in value or "tenant" in value.lower() for value in messages), messages)
        self.assertEqual(before, self._snapshot(self.document_ids))

    def test_exiting_batch_keeps_unapproved_documents_unchanged(self) -> None:
        _seed_streamlit_multi_approval_documents(self.settings)
        app = self._app(self.document_ids)
        before = self._snapshot(self.document_ids)
        self._enter_batch(app, self.document_ids)
        app.button(key=f"approval-batch-exit-{self.document_ids[0]}").click().run()
        self.assertFalse(app.exception)
        self.assertFalse(app.session_state[f"approval-batch-active-{self.document_ids[0]}"])
        self.assertEqual(before, self._snapshot(self.document_ids))


if __name__ == "__main__":
    unittest.main()
