from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.schemas.chunk import Chunk, ChunkOptions
from app.schemas.document import ProcessingJob
from app.schemas.run import ProcessingRun
from app.services.document_service import DocumentService
from app.services.kordoc_reprocessing_service import (
    KordocReprocessingError,
    KordocReprocessingService,
    _reprocessing_chunk_options,
)
from app.storage.repository import JsonRepository


class KordocReprocessingServiceTests(unittest.TestCase):
    def test_recover_creates_verified_draft_and_reuses_prior_chunk_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repository = JsonRepository(settings)
            documents = DocumentService(settings, repository)
            source = documents.upload(
                "regulation.hwp",
                b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1source",
                profile_id="profile-a",
                regulation_id="reg-a",
                regulation_version="v1",
                tenant_id="tenant-a",
            ).model_copy(update={"status": "completed", "regulation_status": "approved"})
            repository.upsert_document(source)
            original_chunk = Chunk(
                chunk_id="approved-original",
                document_id=source.document_id,
                chunk_type="article",
                text="approved original",
                approval_status="approved",
            )
            repository.save_chunks(source.document_id, [original_chunk])
            repository.upsert_run(
                ProcessingRun(
                    run_id="run-original",
                    document_id=source.document_id,
                    job_id="job-original",
                    tenant_id="tenant-a",
                    status="completed",
                    started_at=datetime.now(timezone.utc),
                    completed_at=datetime.now(timezone.utc),
                    elapsed_seconds=1.0,
                    options={
                        "max_chunk_chars": 2400,
                        "min_chunk_chars": 420,
                        "overlap_chars": 80,
                        "chunk_mode": "hybrid",
                        "include_context_header": False,
                        "enable_table_extraction": True,
                        "pipeline_version": "old-version",
                    },
                )
            )
            service = KordocReprocessingService(settings, repository)
            seen_options: list[ChunkOptions] = []

            def fake_process(
                document_id: str,
                options: ChunkOptions,
                progress_callback=None,
            ) -> ProcessingJob:
                seen_options.append(options)
                repository.save_chunks(
                    document_id,
                    [
                        Chunk(
                            chunk_id=f"{document_id}-table",
                            document_id=document_id,
                            chunk_type="table",
                            text="parsed without tables",
                            metadata={
                                "kordoc_table_parser_status": "parsed",
                                "kordoc_table_count": 0,
                                "kordoc_table_inventory": {
                                    "status": "parsed",
                                    "parser": "kordoc",
                                    "table_count": 0,
                                },
                            },
                        )
                    ],
                )
                draft = repository.get_document(document_id)
                repository.upsert_document(draft.model_copy(update={"status": "completed"}))
                job = ProcessingJob(
                    job_id="job-reprocessed",
                    document_id=document_id,
                    tenant_id="tenant-a",
                    status="completed",
                    progress=100,
                    message="completed",
                )
                if progress_callback is not None:
                    progress_callback(job)
                return job

            source_before = repository.get_document(source.document_id)
            chunks_before = repository.get_chunks(source.document_id)
            with patch.object(service, "_require_available_kordoc"), patch.object(
                service.processing,
                "process",
                side_effect=fake_process,
            ) as process:
                first = service.recover(source.document_id)
                second = service.recover(source.document_id)

            self.assertFalse(first.reused)
            self.assertTrue(second.reused)
            self.assertEqual(first.draft_document_id, second.draft_document_id)
            self.assertEqual(first.parser_status, "parsed")
            self.assertEqual(first.parser, "kordoc")
            self.assertEqual(first.table_count, 0)
            self.assertEqual(process.call_count, 1)
            self.assertEqual(len(seen_options), 1)
            self.assertEqual(seen_options[0].max_chunk_chars, 2400)
            self.assertEqual(seen_options[0].min_chunk_chars, 420)
            self.assertEqual(seen_options[0].overlap_chars, 80)
            self.assertEqual(seen_options[0].chunk_mode, "hybrid")
            self.assertFalse(seen_options[0].include_context_header)
            self.assertTrue(seen_options[0].enable_table_extraction)
            self.assertTrue(seen_options[0].enable_agent_review)
            draft = repository.get_document(first.draft_document_id)
            self.assertEqual(draft.regulation_status, "draft")
            self.assertEqual(draft.supersedes_document_id, source.document_id)
            self.assertEqual(draft.reprocessing_source_document_id, source.document_id)
            self.assertEqual(repository.get_document(source.document_id), source_before)
            self.assertEqual(repository.get_chunks(source.document_id), chunks_before)

    def test_recover_rejects_nonparsed_evidence_without_switching_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repository = JsonRepository(settings)
            documents = DocumentService(settings, repository)
            source = documents.upload(
                "regulation.pdf",
                b"%PDF-1.4\nsource",
                tenant_id="tenant-a",
            )
            service = KordocReprocessingService(settings, repository)

            def fake_process(document_id: str, _options: ChunkOptions, progress_callback=None) -> ProcessingJob:
                repository.save_chunks(
                    document_id,
                    [
                        Chunk(
                            chunk_id=f"{document_id}-chunk",
                            document_id=document_id,
                            chunk_type="article",
                            text="fallback",
                            metadata={
                                "kordoc_table_parser_status": "timeout",
                                "kordoc_table_inventory": {
                                    "status": "timeout",
                                    "parser": "kordoc",
                                    "table_count": 0,
                                },
                            },
                        )
                    ],
                )
                return ProcessingJob(
                    job_id="job-timeout",
                    document_id=document_id,
                    tenant_id="tenant-a",
                    status="completed",
                    progress=100,
                )

            source_before = repository.get_document(source.document_id)
            with patch.object(service, "_require_available_kordoc"), patch.object(
                service.processing,
                "process",
                side_effect=fake_process,
            ):
                with self.assertRaisesRegex(KordocReprocessingError, "status=timeout") as raised:
                    service.recover(source.document_id)

            self.assertIsNotNone(raised.exception.draft_document_id)
            self.assertEqual(repository.get_document(source.document_id), source_before)
            failed_draft = repository.get_document(str(raised.exception.draft_document_id))
            self.assertEqual(failed_draft.regulation_status, "draft")
            self.assertIsNone(failed_draft.supersedes_document_id)

    def test_reprocessing_options_use_settings_when_source_has_no_completed_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                default_max_chunk_chars=3200,
                default_overlap_chars=160,
            )
            repository = JsonRepository(settings)

            options = _reprocessing_chunk_options(repository, "doc-missing-run", settings)

        self.assertEqual(options.max_chunk_chars, 3200)
        self.assertEqual(options.overlap_chars, 160)
        self.assertTrue(options.enable_agent_review)


if __name__ == "__main__":
    unittest.main()
