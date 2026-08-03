from __future__ import annotations

import tempfile
import unittest
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.core.pipeline import processing_options_payload
from app.core.pipeline import quality_profile_config_hash
from app.parsers.base import parser_uncertainty_metadata
from app.schemas.chunk import Chunk, ChunkOptions
from app.schemas.document import Document, ProcessingJob
from app.schemas.parsed import ParsedBlock, ParsedDocument, ParsedPage
from app.schemas.quality import QualityReport
from app.schemas.run import ProcessingRun
from app.schemas.structure import StructureNode
from app.services.processing_service import ProcessingService
from app.services.regulation_metadata_service import RegulationMetadataGuess
from app.storage.repository import JsonRepository


def _save_reusable_outputs(settings: Settings, repo: JsonRepository, document_id: str) -> dict[str, str]:
    node = StructureNode(
        node_id=f"{document_id}_node_1",
        document_id=document_id,
        node_type="article",
        number="1",
        title="Purpose",
        text="Article 1 Purpose",
        order_index=0,
    )
    chunk = Chunk(
        chunk_id=f"{document_id}_chunk_1",
        document_id=document_id,
        source_node_ids=[node.node_id],
        chunk_type="article",
        text="Article 1 Purpose",
    )
    repo.save_processing_result(document_id, [node], [chunk], [])
    repo.save_quality_report(
        document_id,
        QualityReport(
            document_id=document_id,
            passed=True,
            score=100.0,
            node_count=1,
            chunk_count=1,
            issue_count=0,
            error_count=0,
            warning_count=0,
            duplicate_chunk_id_count=0,
            empty_chunk_count=0,
            missing_page_count=0,
            missing_required_metadata_count=0,
        ),
    )
    settings.exports_dir.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, str] = {}
    for artifact_name in (
        "jsonl",
        "csv",
        "md",
        "tables.jsonl",
        "tables.csv",
        "manifest.json",
        "quality.json",
        "quality.md",
        "agent_review_plan.json",
        "ai_review_draft.json",
    ):
        path = settings.exports_dir / f"{document_id}.{artifact_name}"
        path.write_text("{}\n", encoding="utf-8")
        artifacts[artifact_name] = str(path)
    return artifacts


class ProcessingServiceTests(unittest.TestCase):
    def test_run_stats_keeps_candidate_details_in_export_artifact_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ProcessingService(settings=Settings(data_dir=Path(tmp)))
            quality_report = QualityReport(
                document_id="doc-summary",
                passed=True,
                score=100.0,
                node_count=1,
                chunk_count=1,
                issue_count=0,
                error_count=0,
                warning_count=0,
                duplicate_chunk_id_count=0,
                empty_chunk_count=0,
                missing_page_count=0,
                missing_required_metadata_count=0,
            )
            stats = service._run_stats(
                quality_report,
                {
                    "status": "planned",
                    "candidate_count": 2,
                    "candidates": [
                        {"chunk_id": "chunk-1", "reasons": ["parser_uncertainty"]},
                        {"chunk_id": "chunk-2", "reasons": ["table_review"]},
                    ],
                    "selected_candidates": [{"chunk_id": "chunk-1", "content_hash": "sha256:test"}],
                },
                phase_timings_ms={"native_parse": 12.5, "exports": 3.0},
            )

        agent_review = stats["agent_review"]
        self.assertNotIn("candidates", agent_review)
        self.assertEqual("agent_review_plan.json", agent_review["candidate_details_artifact"])
        self.assertEqual(2, agent_review["candidate_count"])
        self.assertEqual("chunk-1", agent_review["selected_candidates"][0]["chunk_id"])
        self.assertEqual({"native_parse": 12.5, "exports": 3.0}, stats["phase_timings_ms"])

    def test_loads_quality_gate_profiles_from_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_path = root / "quality_profiles.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "profiles": {
                            "strict": {
                                "coverage_ratio_min": 0.95,
                                "coverage_ratio_max": 1.05,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            settings = Settings(data_dir=root / "data", quality_profiles_path=str(profile_path))
            service = ProcessingService(settings=settings, repository=JsonRepository(settings))

        self.assertIn("strict", service.quality_gate.profiles)
        self.assertEqual(service.quality_gate.profiles["strict"].coverage_ratio_min, 0.95)

    def test_quality_profile_hash_is_loaded_snapshot_not_live_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_path = root / "quality_profiles.json"
            profile_path.write_text('{"profiles":{"strict":{"coverage_ratio_min":0.95}}}', encoding="utf-8")
            settings = Settings(data_dir=root / "data", quality_profiles_path=str(profile_path))
            service = ProcessingService(settings=settings, repository=JsonRepository(settings))
            loaded_hash = service.quality_profiles_sha256

            profile_path.write_text('{"profiles":{"strict":{"coverage_ratio_min":0.90}}}', encoding="utf-8")
            payload = processing_options_payload(
                ChunkOptions(),
                settings=settings,
                quality_profiles_sha256=service.quality_profiles_sha256,
            )
            current_file_hash = quality_profile_config_hash(profile_path)

        self.assertNotEqual(loaded_hash, current_file_hash)
        self.assertEqual(payload["quality_profiles_sha256"], loaded_hash)

    def test_passes_strict_quality_profile_setting_to_quality_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), quality_profiles_strict=True)
            service = ProcessingService(settings=settings, repository=JsonRepository(settings))

        self.assertTrue(service.quality_gate.strict_profile_ids)

    def test_process_agent_review_request_false_skips_provider_and_records_reason(self) -> None:
        class Parser:
            def parse(self, path: Path, document_id: str) -> ParsedDocument:
                return ParsedDocument(
                    document_id=document_id,
                    source_file=path.name,
                    document_name="Review opt-out rule",
                    file_type="pdf",
                    pages=[
                        ParsedPage(
                            page_no=1,
                            blocks=[ParsedBlock(type="table", text="table text requiring review")],
                        )
                    ],
                    raw_text="table text requiring review",
                )

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                enable_agent_review=True,
                openai_api_key="configured",
                agent_review_model="review-model",
            )
            repo = JsonRepository(settings)
            document = Document(
                document_id="doc_review_opt_out",
                filename="review-opt-out.pdf",
                document_name="Review opt-out rule",
                file_type="pdf",
                file_hash="review-opt-out-hash",
                tenant_id="tenant-a",
                status="uploaded",
            )
            repo.upsert_document(document)
            service = ProcessingService(settings=settings, repository=repo)
            provider_calls: list[tuple] = []
            service.agent_review_executor.http_post = (
                lambda *args: provider_calls.append(args) or {}
            )
            progress_events: list[tuple[int, str]] = []

            with patch(
                "app.services.processing_service.get_parser",
                return_value=Parser(),
            ), patch.object(
                service.kordoc_table_parser,
                "parse_file",
                return_value={"status": "disabled", "table_count": 0, "tables": []},
            ), patch.object(
                service,
                "_agent_review_content_hash_cache",
                wraps=service._agent_review_content_hash_cache,
            ) as content_hash_cache:
                job = service.process(
                    document.document_id,
                    ChunkOptions(enable_agent_review=False),
                    progress_callback=lambda current_job: progress_events.append(
                        (current_job.progress, current_job.message)
                    ),
                )

            completed_run = repo.latest_completed_run(document.document_id)

        self.assertEqual("completed", job.status)
        self.assertEqual([], provider_calls)
        content_hash_cache.assert_not_called()
        self.assertIsNotNone(completed_run)
        agent_review = completed_run.stats["agent_review"]
        self.assertFalse(agent_review["enabled"])
        self.assertFalse(agent_review["request_enabled"])
        self.assertTrue(agent_review["provider_execution_ready"])
        self.assertFalse(agent_review["provider_execution_enabled"])
        self.assertEqual("skipped", agent_review["status"])
        self.assertEqual("agent_review_not_requested", agent_review["skip_reason"])
        self.assertEqual(0, agent_review["api_call_count"])
        self.assertIn((85, "전처리 결과 검증을 마무리하는 중"), progress_events)
        self.assertFalse(any("AI 검수" in message for _, message in progress_events))

    def test_process_propagates_document_apba_id_into_chunk_metadata(self) -> None:
        class Parser:
            def parse(self, path: Path, document_id: str) -> ParsedDocument:
                return ParsedDocument(
                    document_id=document_id,
                    source_file=path.name,
                    document_name="PUBLIC_PORTAL rule",
                    file_type="pdf",
                    pages=[
                        ParsedPage(
                            page_no=1,
                            blocks=[ParsedBlock(type="table", text="approved regulation table")],
                        )
                    ],
                    raw_text="approved regulation table",
                    metadata=parser_uncertainty_metadata(
                        source="pdf",
                        risk_level="medium",
                        flags=["ocr_text_extracted"],
                        confidence=0.72,
                        recommendation="review_ocr_text",
                    ),
                )

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            document = Document(
                document_id="doc_public_portal_apba",
                filename="public_portal.pdf",
                document_name="PUBLIC_PORTAL rule",
                file_type="pdf",
                file_hash="hash-public_portal",
                institution_name="PUBLIC_PORTAL Disclosure",
                apba_id="C9999",
                source_system="PUBLIC_PORTAL",
                source_record_id="board-1",
                profile_id="public_portal-test-profile",
                tenant_id="tenant-a",
                status="uploaded",
            )
            repo.upsert_document(document)
            service = ProcessingService(settings=settings, repository=repo)
            progress_events: list[tuple[int, str]] = []

            with patch(
                "app.services.processing_service.get_parser",
                return_value=Parser(),
            ), patch.object(
                service.kordoc_table_parser,
                "parse_file",
                return_value={
                    "status": "parsed",
                    "parser": "kordoc",
                    "table_count": 2,
                    "tables": [],
                    "tables_truncated": True,
                    "kordoc_elapsed_ms": 12.5,
                    "kordoc_input_extension": ".pdf",
                    "kordoc_timeout_seconds": 120,
                },
            ), patch.object(
                repo,
                "list_documents",
                wraps=repo.list_documents,
            ) as list_documents, patch(
                "app.services.processing_service.processing_options_payload",
                wraps=processing_options_payload,
            ) as options_payload:
                job = service.process(
                    document.document_id,
                    ChunkOptions(include_context_header=False),
                    progress_callback=lambda current_job: progress_events.append(
                        (current_job.progress, current_job.message)
                    ),
                )

            chunks = repo.get_chunks(document.document_id)
            completed_run = repo.latest_completed_run(document.document_id)

        self.assertEqual(job.status, "completed")
        self.assertEqual(1, list_documents.call_count)
        self.assertEqual(1, options_payload.call_count)
        self.assertIsNotNone(completed_run)
        self.assertGreaterEqual(
            completed_run.stats["phase_timings_ms"]["metadata_inference_and_staging"],
            0.0,
        )
        self.assertIn((15, "원본 파일에서 텍스트를 추출하는 중"), progress_events)
        self.assertIn((35, "텍스트 추출 완료 · 통합 규정 구조를 분석하는 중"), progress_events)
        self.assertIn((60, "문서 구조 분석 완료 · 청크를 만드는 중"), progress_events)
        self.assertIn((75, "청크 생성과 품질 검사 완료"), progress_events)
        self.assertIn((92, "전처리 결과와 저장 파일을 작성하는 중"), progress_events)
        self.assertTrue(any(message.startswith("청크 저장 ") for _, message in progress_events))
        self.assertTrue(any(message.startswith("내보내기 · ") for _, message in progress_events))
        self.assertTrue(
            all(
                previous_progress <= current_progress
                for (previous_progress, _), (current_progress, _) in zip(
                    progress_events,
                    progress_events[1:],
                )
            ),
            progress_events,
        )
        self.assertEqual((100, "전처리 완료"), progress_events[-1])
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].metadata["apba_id"], "C9999")
        self.assertEqual(chunks[0].metadata["profile_id"], "public_portal-test-profile")
        self.assertEqual(chunks[0].metadata["source_system"], "PUBLIC_PORTAL")
        self.assertEqual(chunks[0].metadata["source_record_id"], "board-1")
        self.assertEqual(chunks[0].metadata["parser_uncertainty_schema_version"], "reg-rag-parser-uncertainty-v1")
        self.assertEqual(chunks[0].metadata["parser_uncertainty_source"], "pdf")
        self.assertEqual(chunks[0].metadata["parser_uncertainty_risk_level"], "medium")
        self.assertIn("ocr_text_extracted", chunks[0].metadata["parser_uncertainty_flags"])
        self.assertEqual(
            chunks[0].metadata["kordoc_table_summary"],
            {
                "status": "parsed",
                "table_count": 2,
                "parser": "kordoc",
                "elapsed_ms": 12.5,
                "input_extension": ".pdf",
                "timeout_seconds": 120,
                "tables_truncated": True,
            },
        )

    def test_process_does_not_infer_supersedes_for_unapproved_reprocessing_draft(self) -> None:
        class Parser:
            def parse(self, path: Path, document_id: str) -> ParsedDocument:
                return ParsedDocument(
                    document_id=document_id,
                    source_file=path.name,
                    document_name="Recovery Regulation",
                    file_type="pdf",
                    pages=[ParsedPage(page_no=1, blocks=[ParsedBlock(text="Article 1 Purpose")])],
                    raw_text="Article 1 Purpose",
                )

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            source = Document(
                document_id="doc_unapproved_source",
                filename="recovery.pdf",
                document_name="Recovery Regulation",
                file_type="pdf",
                file_hash="source-hash",
                profile_id="profile-a",
                regulation_id="reg-recovery",
                regulation_version="v1",
                regulation_status="draft",
                tenant_id="tenant-a",
            )
            recovery = Document(
                document_id="doc_recovery_draft",
                filename="recovery.pdf",
                document_name="Recovery Regulation",
                file_type="pdf",
                file_hash="source-hash",
                profile_id="profile-a",
                regulation_id="reg-recovery",
                regulation_version="v1",
                regulation_status="draft",
                supersedes_document_id=None,
                reprocessing_source_document_id=source.document_id,
                reprocessing_reason="kordoc_evidence_recovery",
                tenant_id="tenant-a",
            )
            repo.upsert_document(source)
            repo.upsert_document(recovery)
            service = ProcessingService(settings=settings, repository=repo)
            inferred = RegulationMetadataGuess(
                document_name="Recovery Regulation",
                regulation_id="reg-recovery",
                regulation_version="v1",
                revision_date="2026-01-01",
                effective_from="2026-01-01",
                supersedes_document_id=source.document_id,
                title_source="filename",
                revision_date_source="filename",
                effective_from_source="filename",
                version_source="filename",
            )

            with patch("app.services.processing_service.get_parser", return_value=Parser()), patch(
                "app.services.processing_service.infer_regulation_metadata",
                return_value=inferred,
            ), patch.object(
                service.kordoc_table_parser,
                "parse_file",
                return_value={
                    "status": "parsed",
                    "parser": "kordoc",
                    "table_count": 0,
                    "tables": [],
                },
            ):
                job = service.process(recovery.document_id, ChunkOptions(include_context_header=False))

            stored = repo.get_document(recovery.document_id)
            chunks = repo.get_chunks(recovery.document_id)

        self.assertEqual(job.status, "completed")
        self.assertIsNone(stored.supersedes_document_id)
        self.assertEqual(stored.reprocessing_source_document_id, source.document_id)
        self.assertTrue(chunks)
        self.assertTrue(
            all(
                chunk.metadata["reprocessing_source_document_id"] == source.document_id
                for chunk in chunks
            )
        )
        self.assertTrue(all("supersedes_document_id" not in chunk.metadata for chunk in chunks))

    def test_process_skips_existing_completed_run_with_same_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            options = ChunkOptions()
            document = Document(
                document_id="doc_existing",
                filename="missing.pdf",
                file_type="pdf",
                file_hash="same-hash",
                status="completed",
            )
            repo.upsert_document(document)
            artifacts = _save_reusable_outputs(settings, repo, document.document_id)
            repo.upsert_run(
                ProcessingRun(
                    run_id="run_existing",
                    document_id=document.document_id,
                    job_id="job_existing",
                    status="completed",
                    started_at=datetime.now(timezone.utc),
                    elapsed_seconds=1.0,
                    options=processing_options_payload(options, settings=settings),
                    artifacts=artifacts,
                )
            )
            service = ProcessingService(settings=settings, repository=repo)

            with patch("app.services.processing_service.get_parser") as get_parser:
                job = service.process(document.document_id, options)

            get_parser.assert_not_called()
            self.assertEqual(job.status, "completed")
            self.assertIn("skipped", job.message)
            self.assertEqual(len(repo.list_runs(document.document_id)), 1)

    def test_process_returns_live_job_instead_of_starting_second_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            document = Document(
                document_id="doc-live-processing-claim",
                filename="missing.pdf",
                file_type="pdf",
                file_hash="same-hash",
                status="uploaded",
            )
            repo.upsert_document(document)
            active_job = ProcessingJob(
                job_id="job-live-processing-claim",
                document_id=document.document_id,
                status="processing",
                progress=42,
                message="Parsing",
            )
            claim = repo.begin_processing_claim(
                document_id=document.document_id,
                run_id="run-live-processing-claim",
                job_id=active_job.job_id,
            )
            self.assertTrue(claim.acquired)
            repo.upsert_job(active_job)
            service = ProcessingService(settings=settings, repository=repo)

            with patch("app.services.processing_service.get_parser") as get_parser:
                returned = service.process(document.document_id, ChunkOptions())

            get_parser.assert_not_called()
            self.assertEqual(active_job.job_id, returned.job_id)
            self.assertEqual("processing", returned.status)
            self.assertEqual(42, returned.progress)
            self.assertEqual(1, len(list(repo.job_progress_root.glob("*.json"))))
            repo.finish_processing_claim(
                document_id=document.document_id,
                run_id=claim.run_id,
                owner_run_id=None,
            )

    def test_process_does_not_skip_completed_run_with_missing_outputs(self) -> None:
        class ParserExpected:
            def parse(self, *args, **kwargs):
                raise RuntimeError("parse attempted")

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            options = ChunkOptions()
            document = Document(
                document_id="doc_missing_outputs",
                filename="missing.pdf",
                file_type="pdf",
                file_hash="same-hash",
                status="completed",
            )
            repo.upsert_document(document)
            repo.save_quality_report(
                document.document_id,
                QualityReport(
                    document_id=document.document_id,
                    passed=True,
                    score=100.0,
                    node_count=1,
                    chunk_count=1,
                    issue_count=0,
                    error_count=0,
                    warning_count=0,
                    duplicate_chunk_id_count=0,
                    empty_chunk_count=0,
                    missing_page_count=0,
                    missing_required_metadata_count=0,
                ),
            )
            repo.upsert_run(
                ProcessingRun(
                    run_id="run_missing_outputs",
                    document_id=document.document_id,
                    job_id="job_missing_outputs",
                    status="completed",
                    started_at=datetime.now(timezone.utc),
                    elapsed_seconds=1.0,
                    options=processing_options_payload(options),
                )
            )
            service = ProcessingService(settings=settings, repository=repo)

            with patch("app.services.processing_service.get_parser", return_value=ParserExpected()) as get_parser:
                with self.assertRaisesRegex(RuntimeError, "parse attempted"):
                    service.process(document.document_id, options)

            get_parser.assert_called_once()
            self.assertEqual(repo.get_document(document.document_id).status, "failed")

    def test_process_does_not_reuse_completed_run_when_agent_review_scope_changes(self) -> None:
        class ParserExpected:
            def parse(self, *args, **kwargs):
                raise RuntimeError("parse attempted")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_settings = Settings(data_dir=root / "data", enable_agent_review=True, agent_review_model="model-a")
            new_settings = Settings(data_dir=root / "data", enable_agent_review=True, agent_review_model="model-b")
            repo = JsonRepository(new_settings)
            options = ChunkOptions(enable_agent_review=True)
            document = Document(
                document_id="doc_agent_scope_change",
                filename="missing.pdf",
                file_type="pdf",
                file_hash="same-hash",
                status="completed",
            )
            repo.upsert_document(document)
            artifacts = _save_reusable_outputs(new_settings, repo, document.document_id)
            repo.upsert_run(
                ProcessingRun(
                    run_id="run_agent_scope_old",
                    document_id=document.document_id,
                    job_id="job_agent_scope_old",
                    status="completed",
                    started_at=datetime.now(timezone.utc),
                    elapsed_seconds=1.0,
                    options=processing_options_payload(options, settings=old_settings),
                    artifacts=artifacts,
                )
            )
            service = ProcessingService(settings=new_settings, repository=repo)

            with patch("app.services.processing_service.get_parser", return_value=ParserExpected()) as get_parser:
                with self.assertRaisesRegex(RuntimeError, "parse attempted"):
                    service.process(document.document_id, options)

            get_parser.assert_called_once()

    def test_process_does_not_reuse_completed_run_when_agent_review_api_becomes_ready(self) -> None:
        class ParserExpected:
            def parse(self, *args, **kwargs):
                raise RuntimeError("parse attempted")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staged_settings = Settings(
                data_dir=root / "data",
                enable_agent_review=True,
                openai_api_key="",
                agent_review_model="model-a",
            )
            executable_settings = Settings(
                data_dir=root / "data",
                enable_agent_review=True,
                openai_api_key="configured",
                agent_review_model="model-a",
            )
            repo = JsonRepository(executable_settings)
            options = ChunkOptions(enable_agent_review=True)
            document = Document(
                document_id="doc_agent_api_ready_change",
                filename="missing.pdf",
                file_type="pdf",
                file_hash="same-hash",
                status="completed",
            )
            repo.upsert_document(document)
            artifacts = _save_reusable_outputs(executable_settings, repo, document.document_id)
            repo.upsert_run(
                ProcessingRun(
                    run_id="run_agent_api_not_ready",
                    document_id=document.document_id,
                    job_id="job_agent_api_not_ready",
                    status="completed",
                    started_at=datetime.now(timezone.utc),
                    elapsed_seconds=1.0,
                    options=processing_options_payload(options, settings=staged_settings),
                    artifacts=artifacts,
                )
            )
            service = ProcessingService(settings=executable_settings, repository=repo)

            with patch("app.services.processing_service.get_parser", return_value=ParserExpected()) as get_parser:
                with self.assertRaisesRegex(RuntimeError, "parse attempted"):
                    service.process(document.document_id, options)

            get_parser.assert_called_once()

    def test_process_does_not_reuse_completed_run_when_agent_review_request_changes(self) -> None:
        class ParserExpected:
            def parse(self, *args, **kwargs):
                raise RuntimeError("parse attempted")

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp) / "data",
                enable_agent_review=True,
                openai_api_key="configured",
                agent_review_model="model-a",
            )
            repo = JsonRepository(settings)
            document = Document(
                document_id="doc_agent_request_change",
                filename="missing.pdf",
                file_type="pdf",
                file_hash="same-hash",
                status="completed",
            )
            repo.upsert_document(document)
            artifacts = _save_reusable_outputs(settings, repo, document.document_id)
            repo.upsert_run(
                ProcessingRun(
                    run_id="run_agent_request_disabled",
                    document_id=document.document_id,
                    job_id="job_agent_request_disabled",
                    status="completed",
                    started_at=datetime.now(timezone.utc),
                    elapsed_seconds=1.0,
                    options=processing_options_payload(
                        ChunkOptions(enable_agent_review=False),
                        settings=settings,
                    ),
                    artifacts=artifacts,
                )
            )
            service = ProcessingService(settings=settings, repository=repo)

            with patch(
                "app.services.processing_service.get_parser",
                return_value=ParserExpected(),
            ) as get_parser:
                with self.assertRaisesRegex(RuntimeError, "parse attempted"):
                    service.process(
                        document.document_id,
                        ChunkOptions(enable_agent_review=True),
                    )

            get_parser.assert_called_once()

    def test_agent_review_cache_uses_only_provider_results_for_same_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            service = ProcessingService(settings=settings, repository=repo)
            cache_scope_hash = service.agent_review_policy.cache_scope_hash()
            executed_hash = "sha256:" + "a" * 64
            plan_only_hash = "sha256:" + "b" * 64
            other_tenant_hash = "sha256:" + "c" * 64
            stale_scope_hash = "sha256:" + "d" * 64
            stale_scope_candidate_hash = "sha256:" + "e" * 64
            now = datetime.now(timezone.utc)
            for run_id, tenant_id, agent_review in (
                (
                    "run_executed",
                    "tenant-a",
                    {
                        "status": "planned",
                        "api_call_count": 1,
                        "cache_scope_hash": cache_scope_hash,
                        "selected_candidates": [{"content_hash": executed_hash}],
                    },
                ),
                (
                    "run_plan_only",
                    "tenant-a",
                    {
                        "status": "planned",
                        "api_call_count": 0,
                        "cache_scope_hash": cache_scope_hash,
                        "selected_candidates": [{"content_hash": plan_only_hash}],
                    },
                ),
                (
                    "run_other_tenant",
                    "tenant-b",
                    {
                        "status": "reviewed",
                        "cache_scope_hash": cache_scope_hash,
                        "selected_candidates": [{"content_hash": other_tenant_hash}],
                    },
                ),
                (
                    "run_stale_scope",
                    "tenant-a",
                    {
                        "status": "reviewed",
                        "cache_scope_hash": stale_scope_hash,
                        "selected_candidates": [{"content_hash": stale_scope_candidate_hash}],
                    },
                ),
            ):
                repo.upsert_run(
                    ProcessingRun(
                        run_id=run_id,
                        document_id=f"doc_{run_id}",
                        job_id=f"job_{run_id}",
                        tenant_id=tenant_id,
                        status="completed",
                        started_at=now,
                        elapsed_seconds=1.0,
                        stats={"agent_review": agent_review},
                    )
                )

            self.assertEqual(
                service._agent_review_content_hash_cache("tenant-a", cache_scope_hash=cache_scope_hash),
                {executed_hash},
            )


if __name__ == "__main__":
    unittest.main()
