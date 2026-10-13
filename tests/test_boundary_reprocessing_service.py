from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.schemas.chunk import Chunk
from app.schemas.document import ProcessingJob
from app.schemas.run import ProcessingRun
from app.services.boundary_reprocessing_service import BoundaryReprocessingError, BoundaryReprocessingService
from app.services.document_service import DocumentService
from app.storage.repository import JsonRepository


class BoundaryReprocessingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(data_dir=Path(self.temp.name) / "data", artifact_root=Path(self.temp.name),
                                 enable_kordoc_table_parser=True)
        command = patch("app.services.boundary_reprocessing_service.kordoc_table_command_status",
                        return_value={"available": True})
        self.command_status = command.start()
        self.addCleanup(command.stop)
        self.repository = JsonRepository(self.settings)
        self.documents = DocumentService(self.settings, self.repository)
        source = self.documents.upload("regulation.pdf", b"%PDF-1.4\nsynthetic source",
                                       tenant_id="tenant-a", profile_id="profile-a", regulation_id="reg-a")
        self.source = source.model_copy(update={"regulation_status": "approved", "status": "completed"})
        self.repository.upsert_document(self.source)
        self.repository.save_chunks(source.document_id, [Chunk(
            chunk_id="source-approved", document_id=source.document_id, chunk_type="article", text="Source",
            approval_status="approved", approval_id="source-approval", approved_content_hash="original-hash",
            metadata={"ambiguous_combined_book_boundary": True},
        )])
        self.repository.append_approval_record({"approval_id": "source-approval", "document_id": source.document_id,
            "tenant_id": "tenant-a", "chunk_ids": ["source-approved"], "approved_at": "2026-01-01T00:00:00Z"})
        self.repository.append_indexing_job({"indexing_job_id": "source-index", "document_id": source.document_id,
            "tenant_id": "tenant-a", "status": "indexed", "created_at": "2026-01-01T00:00:00Z"})
        self.repository.upsert_run(ProcessingRun(
            run_id="old-run", document_id=source.document_id, job_id="old-job", tenant_id="tenant-a",
            status="completed", started_at=datetime.now(timezone.utc), completed_at=datetime.now(timezone.utc),
            elapsed_seconds=1, options={"enable_agent_review": True, "pipeline_version": "old", "max_chunk_chars": 2400},
        ))
        self.service = BoundaryReprocessingService(self.settings, self.repository)

    def _snapshot_source(self) -> dict:
        document_id = self.source.document_id
        return {
            "document": self.repository.get_document(document_id).model_dump(mode="json"),
            "raw": self.documents.path_for(self.source).read_bytes(),
            "chunks": [item.model_dump(mode="json") for item in self.repository.get_chunks(document_id)],
            "approvals": self.repository.list_approval_records(document_id),
            "indexes": self.repository.list_indexing_jobs(document_id),
        }

    def _process(self, document_id, options, progress_callback=None, *, blocked=False, kordoc=True):
        self.assertFalse(options.enable_agent_review)
        self.assertEqual(2400, options.max_chunk_chars)
        self.assertNotEqual(self.source.document_id, document_id)
        self.assertIsNone(self.repository.latest_completed_run(document_id))
        metadata = {"kordoc_table_parser_status": "parsed" if kordoc else "missing",
                    "kordoc_table_inventory": {"parser": "kordoc" if kordoc else "", "table_count": 0}}
        if blocked:
            metadata["ambiguous_combined_book_boundary"] = True
        self.repository.save_chunks(document_id, [Chunk(chunk_id=f"{document_id}-article", document_id=document_id,
                                                       chunk_type="article", text="New parse", metadata=metadata)])
        document = self.repository.get_document(document_id)
        self.repository.upsert_document(document.model_copy(update={"status": "completed"}))
        return ProcessingJob(job_id=f"job-{document_id}", document_id=document_id, status="completed", progress=100)

    def _recover(self, **kwargs):
        return self.service.recover(self.source.document_id, tenant_id="tenant-a", profile_id="profile-a", **kwargs)

    def test_fresh_local_only_drafts_preserve_source_and_never_reuse_previous_parse(self) -> None:
        before = self._snapshot_source()
        with patch.object(self.service.processing, "process", side_effect=self._process) as process:
            first = self._recover()
            second = self._recover()
        self.assertEqual(2, process.call_count)
        self.assertNotEqual(first.draft_document_id, second.draft_document_id)
        self.assertEqual(before, self._snapshot_source())
        for result in (first, second):
            draft = self.repository.get_document(result.draft_document_id)
            self.assertEqual("structure_boundary_recovery", draft.reprocessing_reason)
            self.assertEqual(self.source.document_id, draft.reprocessing_source_document_id)
            self.assertEqual(self.source.document_id, draft.supersedes_document_id)
            self.assertEqual("draft", draft.regulation_status)
            self.assertEqual(self.source.file_hash, draft.file_hash)
            self.assertEqual([], self.repository.list_approval_records(draft.document_id))
            self.assertEqual([], self.repository.list_indexing_jobs(draft.document_id))
            self.assertTrue(all(chunk.approval_status != "approved" for chunk in self.repository.get_chunks(draft.document_id)))

    def test_remaining_boundary_and_missing_kordoc_preserve_source_and_report_failed_draft(self) -> None:
        before = self._snapshot_source()
        for mode in ("boundary", "kordoc"):
            with self.subTest(mode=mode), patch.object(self.service.processing, "process", side_effect=
                    lambda doc, options, progress_callback=None: self._process(doc, options, blocked=mode == "boundary", kordoc=mode != "kordoc")):
                with self.assertRaises(BoundaryReprocessingError) as caught:
                    self._recover()
            draft_id = caught.exception.draft_document_id
            self.assertTrue(draft_id)
            self.assertEqual([], self.repository.list_approval_records(draft_id))
            self.assertEqual([], self.repository.list_indexing_jobs(draft_id))
            self.assertEqual(before, self._snapshot_source())

    def test_tenant_or_profile_mismatch_is_rejected_before_clone(self) -> None:
        for tenant, profile in (("tenant-other", "profile-a"), ("tenant-a", "profile-other")):
            with self.subTest(tenant=tenant, profile=profile), patch.object(self.service.documents, "clone_as_reprocessing_draft") as clone:
                with self.assertRaises(BoundaryReprocessingError):
                    self.service.recover(self.source.document_id, tenant_id=tenant, profile_id=profile)
                clone.assert_not_called()

    def test_source_hash_mismatch_does_not_create_or_process_a_draft(self) -> None:
        self.documents.path_for(self.source).write_bytes(b"%PDF-1.4\nchanged synthetic source")
        before = self._snapshot_source()
        with patch.object(self.service.processing, "process") as process:
            with self.assertRaises(BoundaryReprocessingError):
                self._recover()
            process.assert_not_called()
        self.assertEqual(before, self._snapshot_source())
        self.assertEqual(1, len(self.repository.list_documents()))

    def test_processing_failure_keeps_source_and_redacts_internal_error(self) -> None:
        before = self._snapshot_source()
        with patch.object(self.service.processing, "process", side_effect=RuntimeError("private-path-secret")):
            with self.assertRaises(BoundaryReprocessingError) as caught:
                self._recover()
        self.assertTrue(caught.exception.draft_document_id)
        self.assertNotIn("private-path-secret", str(caught.exception))
        self.assertEqual(before, self._snapshot_source())

    def test_missing_or_disabled_kordoc_stops_before_clone_and_parser_work(self) -> None:
        for enabled, available in ((False, True), (True, False)):
            self.service.settings = replace(self.settings, enable_kordoc_table_parser=enabled)
            self.command_status.return_value = {"available": available}
            with self.subTest(enabled=enabled, available=available), \
                 patch.object(self.service.documents, "clone_as_reprocessing_draft") as clone, \
                 patch.object(self.service.processing, "process") as process:
                with self.assertRaisesRegex(BoundaryReprocessingError, "Kordoc"):
                    self._recover()
                clone.assert_not_called()
                process.assert_not_called()
            self.assertEqual(1, len(self.repository.list_documents()))


if __name__ == "__main__":
    unittest.main()
