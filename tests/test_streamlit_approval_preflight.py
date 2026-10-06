from __future__ import annotations

import ast
from contextlib import ExitStack
import inspect
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.core.config import Settings
from app.schemas.chunk import Chunk
from app.services.document_service import DocumentService
from app.storage.repository import JsonRepository
from frontend import streamlit_app


class StreamlitApprovalPreflightTests(unittest.TestCase):
    def _chunk(self, chunk_id: str = "synthetic-chunk", **updates) -> Chunk:
        return Chunk(
            chunk_id=chunk_id,
            document_id="synthetic-document",
            chunk_type="article",
            text="합성 조항 본문",
            **updates,
        )

    def test_all_backend_markers_block_before_plan_mutations_even_with_override(self) -> None:
        markers = (
            {"metadata": {"ambiguous_combined_book_boundary": True}},
            {"metadata": {"structure_boundary_diagnostic": "ambiguous_combined_book_boundary_after_attachment"}},
            {"warnings": ["ambiguous_combined_book_boundary_requires_reparse"]},
        )
        for marker in markers:
            with self.subTest(marker=marker), ExitStack() as stack:
                mutators = [
                    stack.enter_context(patch.object(streamlit_app, name))
                    for name in (
                        "_approval_save_text_edits",
                        "_build_current_document_approval_templates",
                        "approve_review_chunks",
                        "index_document",
                    )
                ]
                ctx = {"document_id": "synthetic-document", "chunks": [self._chunk(**marker)]}
                with self.assertRaisesRegex(ValueError, "조항 1개.*규정 경계"):
                    streamlit_app._prepare_reviewed_document_approval_plan(
                        ctx, approval_override_reason="합성 검토 사유"
                    )
                for mutator in mutators:
                    mutator.assert_not_called()

    def test_final_approval_execution_checks_before_session_or_storage_mutation(self) -> None:
        # Invoke the production closure with explicit captured values. Streamlit's
        # disabled-widget semantics are covered separately by AppTest.
        page = ast.parse(inspect.getsource(streamlit_app._page_approval))
        callback = next(
            node for node in ast.walk(page)
            if isinstance(node, ast.FunctionDef) and node.name == "_execute_final_approval"
        )
        chunk = self._chunk(metadata={"ambiguous_combined_book_boundary": True})
        original_state = {"previous-error": "preserve this status"}
        session_state = dict(original_state)
        mutators = {name: Mock() for name in (
            "_approval_save_text_edits", "_build_current_document_approval_templates",
            "approve_review_chunks", "index_document",
        )}
        namespace = {
            **vars(streamlit_app), **mutators,
            "ctx": {"chunks": [chunk]}, "chunks": [chunk],
            "document_id": chunk.document_id, "operation_error_key": "previous-error",
            "security_level_key": "security-level",
            "st": SimpleNamespace(session_state=session_state),
        }
        exec(compile(ast.Module(body=[callback], type_ignores=[]), "<final-approval>", "exec"), namespace)
        with self.assertRaisesRegex(ValueError, "승인 차단"):
            namespace["_execute_final_approval"](
                [{"chunk_id": chunk.chunk_id, "chunk": chunk}],
                override_reason_text="합성 검토 사유",
            )
        self.assertEqual(original_state, session_state)
        for mutator in mutators.values():
            mutator.assert_not_called()

    def test_all_documents_are_checked_before_preparing_the_first_plan(self) -> None:
        normal = {"chunks": [self._chunk("normal")]}
        blocked = {"chunks": [self._chunk("blocked", metadata={"ambiguous_combined_book_boundary": True})]}
        with patch.object(streamlit_app, "_prepare_reviewed_document_approval_plan") as prepare:
            with self.assertRaisesRegex(ValueError, "문서 1개.*조항 1개"):
                streamlit_app._prepare_reviewed_document_approval_plans([normal, blocked])
        prepare.assert_not_called()

    def test_evidence_builder_does_not_create_reports_for_blocked_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            chunk = self._chunk(metadata={"ambiguous_combined_book_boundary": True})
            ctx = {"document": object(), "document_id": chunk.document_id, "chunks": [chunk]}
            with patch.object(streamlit_app, "settings", settings):
                with self.assertRaisesRegex(ValueError, "승인 차단"):
                    streamlit_app._build_current_document_approval_templates(ctx, security_level="internal")
            self.assertEqual([], list(Path(tmp).rglob("*")))

    def test_message_is_bounded_and_does_not_disclose_chunk_ids(self) -> None:
        chunks = [
            self._chunk(f"synthetic-long-id-{index}-" + "x" * 100,
                        metadata={"ambiguous_combined_book_boundary": True})
            for index in range(1200)
        ]
        message = streamlit_app._bulk_approval_blocker([{"chunks": chunks}])
        self.assertIn("조항 1,200개", message)
        self.assertIn("규정별로 나눈 파일", message)
        self.assertNotIn("synthetic-long-id", message)
        self.assertLess(len(message), 400)

    def test_scope_does_not_silently_drop_blocked_targets_or_overblock_other_regulations(self) -> None:
        normal = self._chunk("normal")
        blocked = self._chunk("blocked", metadata={"ambiguous_combined_book_boundary": True})
        ctx = {"chunks": [normal, blocked]}
        self.assertEqual("", streamlit_app._bulk_approval_blocker([ctx], candidate_chunk_ids=["normal"]))
        self.assertEqual("", streamlit_app._bulk_approval_blocker([ctx], candidate_chunk_ids=[]))
        with self.assertRaisesRegex(ValueError, "조항 1개"):
            streamlit_app._require_bulk_approval_preflight([ctx], candidate_chunk_ids=["normal", "blocked"])

    def test_ordinary_warnings_keep_plan_preparation_and_override_behavior(self) -> None:
        chunk = self._chunk(warnings=["table_structure_review_required"])
        ctx = {"chunks": [chunk]}
        with patch.object(streamlit_app, "_prepare_reviewed_document_approval_plan", return_value={"prepared": True}) as prepare:
            plans = streamlit_app._prepare_reviewed_document_approval_plans([ctx], security_level="public")
        self.assertEqual([{"prepared": True}], plans)
        prepare.assert_called_once_with(ctx, security_level="public")
        self.assertEqual(
            streamlit_app.DEFAULT_UNREVIEWED_OVERRIDE_REASON,
            streamlit_app._approval_override_reason_for_entries([{"state": {"approve_enabled": False}}]),
        )

    def test_completed_boundary_blocked_outputs_are_never_reused_on_reupload(self) -> None:
        markers = (
            {"metadata": {"ambiguous_combined_book_boundary": True}},
            {"metadata": {"structure_boundary_diagnostic": "ambiguous_combined_book_boundary_after_attachment"}},
            {"warnings": ["ambiguous_combined_book_boundary_requires_reparse"]},
        )
        for marker in markers:
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "synthetic.pdf"
                source.write_bytes(b"%PDF-1.4 synthetic source")
                document = SimpleNamespace(document_id="synthetic-document", tenant_id="tenant-a")
                completed = SimpleNamespace(status="completed", stats={})
                repository = Mock()
                repository.find_reusable_run.return_value = (document, completed)
                repository.get_chunks.return_value = [self._chunk(**marker)]
                actual = streamlit_app._find_reusable_preprocessing_run(
                    repository, source, processing_options={}, upload_metadata={}, tenant_id="tenant-a"
                )
                self.assertIsNone(actual)
                repository.get_chunks.assert_called_once_with("synthetic-document")
                repository.save_chunks.assert_not_called()
                repository.upsert_document.assert_not_called()

    def test_reuse_keeps_tenant_gate_before_reading_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "synthetic.pdf"
            source.write_bytes(b"%PDF-1.4 synthetic source")
            repository = Mock()
            repository.find_reusable_run.return_value = (
                SimpleNamespace(document_id="synthetic-document", tenant_id="another-tenant"), object()
            )
            self.assertIsNone(streamlit_app._find_reusable_preprocessing_run(
                repository, source, processing_options={}, upload_metadata={}, tenant_id="tenant-a"
            ))
            repository.get_chunks.assert_not_called()

    def test_same_source_reupload_creates_separate_draft_and_preserves_approved_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            repository = JsonRepository(settings)
            service = DocumentService(settings, repository)
            content = b"%PDF-1.4 synthetic upload contract fixture"
            source = service.upload_stream("synthetic.pdf", io.BytesIO(content), tenant_id="tenant-a")
            source = source.model_copy(update={"status": "completed", "regulation_status": "approved"})
            repository.upsert_document(source)
            chunk = self._chunk(approval_status="approved").model_copy(update={"document_id": source.document_id})
            repository.save_chunks(source.document_id, [chunk])
            repository.append_approval_record({
                "approval_id": "synthetic-approval", "document_id": source.document_id,
                "tenant_id": "tenant-a", "chunk_ids": [chunk.chunk_id],
            })
            approvals_before = repository.list_approval_records(source.document_id)
            source_before = repository.get_document(source.document_id)
            source_path = service.path_for(source)

            draft = service.upload_stream("synthetic.pdf", io.BytesIO(content), tenant_id="tenant-a")

            self.assertNotEqual(source.document_id, draft.document_id)
            self.assertNotEqual(source_path, service.path_for(draft))
            self.assertEqual(source.file_hash, draft.file_hash)
            self.assertEqual("draft", draft.regulation_status)
            self.assertEqual("tenant-a", draft.tenant_id)
            self.assertEqual(content, source_path.read_bytes())
            self.assertEqual(content, service.path_for(draft).read_bytes())
            self.assertEqual(source_before, repository.get_document(source.document_id))
            self.assertEqual([chunk], repository.get_chunks(source.document_id))
            self.assertEqual(approvals_before, repository.list_approval_records(source.document_id))
            self.assertEqual([], repository.get_chunks(draft.document_id))
            self.assertEqual([], repository.list_approval_records(draft.document_id))
            self.assertEqual([], repository.list_indexing_jobs(draft.document_id))
            self.assertIsNone(repository.latest_completed_run(draft.document_id))

    def test_reprocessing_rechecks_blockers_in_a_reused_completed_draft(self) -> None:
        draft = self._chunk(metadata={"ambiguous_combined_book_boundary": True})
        repository = Mock()
        repository.get_chunks.return_value = [draft]
        result = SimpleNamespace(source_document_id="source", draft_document_id="old-draft", reused=True)
        with patch.object(streamlit_app, "KordocReprocessingService") as service:
            service.return_value.recover.return_value = result
            with self.assertRaisesRegex(ValueError, "차단이 남아.*재사용"):
                streamlit_app._safe_kordoc_reprocess_documents(Settings(), repository, ["source"])
        repository.get_chunks.assert_called_once_with("old-draft")
        repository.save_chunks.assert_not_called()
        repository.upsert_document.assert_not_called()

    def test_reprocessing_returns_verified_draft_without_changing_settings(self) -> None:
        settings = Settings()
        repository = Mock()
        repository.get_chunks.return_value = [self._chunk(warnings=["table_structure_review_required"])]
        result = SimpleNamespace(source_document_id="source", draft_document_id="new-draft")
        with patch.object(streamlit_app, "KordocReprocessingService") as service:
            service.return_value.recover.return_value = result
            actual = streamlit_app._safe_kordoc_reprocess_documents(settings, repository, ["source", "source"])
        self.assertEqual([result], actual)
        service.assert_called_once_with(settings, repository, quality_profile_config=None)
        self.assertEqual(1, service.return_value.recover.call_count)


if __name__ == "__main__":
    unittest.main()
