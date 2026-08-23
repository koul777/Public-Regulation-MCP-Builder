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
                "_agent_review_cache_index",
                wraps=service._agent_review_cache_index,
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
        pipeline_trace = completed_run.stats["pipeline_trace"]
        quality_stage = next(
            stage for stage in pipeline_trace["stages"] if stage["stage_id"] == "quality_gate"
        )
        quality_roles = {
            item["role_id"]: item for item in quality_stage["agent_role_statuses"]
        }
        quality_status = quality_roles["quality_gate"]["status"]
        self.assertIn(quality_status, {"completed", "review_required"})
        self.assertEqual(
            "pending" if quality_status == "completed" else "review_required",
            quality_roles["human_approval_gate"]["status"],
        )
        export_stage = next(
            stage for stage in pipeline_trace["stages"] if stage["stage_id"] == "export"
        )
        self.assertEqual("completed", export_stage["agent_role_statuses"][0]["status"])
        self.assertIn((85, "전처리 결과 검증을 마무리하는 중"), progress_events)
        self.assertFalse(any("AI 검수" in message for _, message in progress_events))

    def test_structure_analysis_fills_the_gauge_between_extraction_and_chunking(self) -> None:
        """예전에는 본문 정리와 구조 분석 내내 35%에 멈춰 있어 멈춘 화면처럼 보였다.

        지금은 실제로 끝낸 쪽 수와 줄 수만큼 게이지가 오른다. 시간으로 추정한
        값은 여전히 쓰지 않는다.
        """

        class Parser:
            def parse(self, path: Path, document_id: str) -> ParsedDocument:
                pages = [
                    ParsedPage(
                        page_no=index,
                        blocks=[ParsedBlock(type="text", text=f"제{index}조(목적{index}) 본문 {index}")],
                    )
                    for index in range(1, 61)
                ]
                return ParsedDocument(
                    document_id=document_id,
                    source_file=path.name,
                    document_name="구조 분석 진행률 규정",
                    file_type="pdf",
                    pages=pages,
                    raw_text="\n".join(
                        f"제{index}조(목적{index}) 본문 {index}" for index in range(1, 61)
                    ),
                )

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            document = Document(
                document_id="doc_structure_progress",
                filename="structure-progress.pdf",
                document_name="구조 분석 진행률 규정",
                file_type="pdf",
                file_hash="structure-progress-hash",
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
                return_value={"status": "disabled", "table_count": 0, "tables": []},
            ):
                job = service.process(
                    document.document_id,
                    ChunkOptions(enable_agent_review=False),
                    progress_callback=lambda current_job: progress_events.append(
                        (current_job.progress, current_job.message)
                    ),
                )

        self.assertEqual("completed", job.status)
        structure_labels = ("본문 정리", "조문 표시 찾기", "조문 계층 조립")
        structure_events = [
            (percent, message)
            for percent, message in progress_events
            if message.startswith(structure_labels)
        ]
        self.assertEqual(
            set(structure_labels),
            {message.rsplit(" ", 1)[0] for _percent, message in structure_events},
        )
        structure_percents = [percent for percent, _message in structure_events]
        # 35%에서 60%로 건너뛰지 않고 그 사이가 실제로 채워져야 한다.
        self.assertLess(35, max(structure_percents))
        self.assertGreater(60, max(structure_percents))
        self.assertLess(2, len(set(structure_percents)), structure_events)
        self.assertTrue(
            all(
                previous <= current
                for (previous, _), (current, _) in zip(progress_events, progress_events[1:])
            ),
            progress_events,
        )
        self.assertTrue(
            any(message.endswith(" 60/60") for _percent, message in structure_events),
            structure_events,
        )

    def test_ai_review_reports_finished_batches_while_waiting_for_the_provider(self) -> None:
        """AI 응답 대기는 전처리에서 가장 길다. 끝난 묶음 수만큼 게이지가 올라야 한다."""

        class Parser:
            def parse(self, path: Path, document_id: str) -> ParsedDocument:
                return ParsedDocument(
                    document_id=document_id,
                    source_file=path.name,
                    document_name="AI 검수 진행률 규정",
                    file_type="pdf",
                    pages=[
                        ParsedPage(
                            page_no=1,
                            blocks=[ParsedBlock(type="table", text="제1조(목적) 표로 된 본문")],
                        )
                    ],
                    raw_text="제1조(목적) 표로 된 본문",
                )

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                enable_agent_review=True,
                openai_api_key="configured",
                agent_review_model="review-model",
            )
            repo = JsonRepository(settings)
            service = ProcessingService(settings=settings, repository=repo)

            def _fake_post(url, headers, payload, timeout):
                sent = json.loads(payload["messages"][1]["content"])
                items = [
                    {
                        "chunk_id": item["chunk_id"],
                        "risk_level": "high",
                        "issues": ["제1조 본문이 표에 갇혀 있습니다."],
                        "recommended_human_check": "제1조 표 경계를 확인하세요.",
                    }
                    for item in sent["items"]
                ]
                return {
                    "id": "req_1",
                    "choices": [{"message": {"content": json.dumps({"items": items})}}],
                }

            service.agent_review_executor.http_post = _fake_post
            document = Document(
                document_id="doc_review_progress",
                filename="review-progress.pdf",
                document_name="AI 검수 진행률 규정",
                file_type="pdf",
                file_hash="review-progress-hash",
                tenant_id="tenant-a",
                status="uploaded",
            )
            repo.upsert_document(document)
            progress_events: list[tuple[int, str]] = []

            with patch(
                "app.services.processing_service.get_parser",
                return_value=Parser(),
            ), patch.object(
                service.kordoc_table_parser,
                "parse_file",
                return_value={"status": "disabled", "table_count": 0, "tables": []},
            ):
                job = service.process(
                    document.document_id,
                    ChunkOptions(enable_agent_review=True),
                    progress_callback=lambda current_job: progress_events.append(
                        (current_job.progress, current_job.message)
                    ),
                )

        self.assertEqual("completed", job.status)
        review_events = [
            (percent, message)
            for percent, message in progress_events
            if message.startswith("AI 검수 묶음 ")
        ]
        self.assertTrue(review_events, progress_events)
        self.assertTrue(all(85 <= percent <= 91 for percent, _message in review_events), review_events)
        self.assertTrue(
            any(message.endswith("1/1 완료") for _percent, message in review_events),
            review_events,
        )

    def test_reuploading_the_same_regulation_keeps_the_ai_review_visible(self) -> None:
        """같은 규정을 두 번 올리면 두 번째 문서에도 검수 의견이 남아야 한다.

        운영자가 신고한 증상이 정확히 이것이다. 검수를 켜고 다시 올렸는데 내용 해시가
        같아 호출을 건너뛰었고, 그 결과 두 번째 문서에는 지적이 하나도 붙지 않아
        승인 화면이 조항마다 'AI 검수 의견 없음'이었다.
        """

        class Parser:
            def parse(self, path: Path, document_id: str) -> ParsedDocument:
                return ParsedDocument(
                    document_id=document_id,
                    source_file=path.name,
                    document_name="강사임용 등에 관한 규정",
                    file_type="pdf",
                    pages=[
                        ParsedPage(
                            page_no=1,
                            blocks=[ParsedBlock(type="table", text="제1조(목적) 표로 된 본문")],
                        )
                    ],
                    raw_text="제1조(목적) 표로 된 본문",
                )

        finding_template = {
            "risk_level": "high",
            "issues": ["제1조 본문이 표에 갇혀 있습니다."],
            "recommended_human_check": "제1조 표 경계를 확인하세요.",
        }

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                enable_agent_review=True,
                openai_api_key="configured",
                agent_review_model="review-model",
            )
            repo = JsonRepository(settings)
            service = ProcessingService(settings=settings, repository=repo)
            provider_calls: list[tuple] = []

            def _fake_post(url, headers, payload, timeout):
                provider_calls.append(url)
                # 응답은 요청에 실린 청크 ID로 돌려줘야 그 조항에 붙는다.
                sent = json.loads(payload["messages"][1]["content"])
                items = [
                    {**finding_template, "chunk_id": item["chunk_id"]} for item in sent["items"]
                ]
                return {
                    "id": "req_1",
                    "choices": [{"message": {"content": json.dumps({"items": items})}}],
                }

            service.agent_review_executor.http_post = _fake_post

            findings_per_run: list[int] = []
            for index in (1, 2):
                document = Document(
                    document_id=f"doc_reupload_{index}",
                    filename="regulation.pdf",
                    document_name="강사임용 등에 관한 규정",
                    file_type="pdf",
                    file_hash="same-regulation-hash",
                    tenant_id="tenant-a",
                    status="uploaded",
                )
                repo.upsert_document(document)
                with patch(
                    "app.services.processing_service.get_parser",
                    return_value=Parser(),
                ), patch.object(
                    service.kordoc_table_parser,
                    "parse_file",
                    return_value={"status": "disabled", "table_count": 0, "tables": []},
                ):
                    service.process(
                        document.document_id,
                        ChunkOptions(enable_agent_review=True),
                    )
                chunks = repo.get_chunks(document.document_id)
                findings_per_run.append(
                    sum(1 for chunk in chunks if (chunk.metadata or {}).get("agent_review_findings"))
                )

            second_run = repo.latest_completed_run("doc_reupload_2")

        self.assertGreater(findings_per_run[0], 0)
        # 두 번째 문서에도 같은 수의 의견이 남아야 한다. 재사용은 결과를 버리는 것이 아니다.
        self.assertEqual(findings_per_run[0], findings_per_run[1])
        agent_review = second_run.stats["agent_review"]
        self.assertEqual("review_candidates_cached", agent_review["skip_reason"])
        self.assertGreater(int(agent_review["reused_chunk_count"]), 0)
        self.assertEqual(findings_per_run[1], int(agent_review["reused_finding_count"]))
        # 재사용했으므로 두 번째 실행에서는 제공자를 다시 부르지 않는다.
        self.assertEqual(1, len({call for call in provider_calls}))

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

    def test_process_keeps_a_user_supplied_document_name_over_the_body_title(self) -> None:
        class Parser:
            def parse(self, path: Path, document_id: str) -> ParsedDocument:
                return ParsedDocument(
                    document_id=document_id,
                    source_file=path.name,
                    document_name="본문에서 찾은 제목",
                    file_type="pdf",
                    pages=[
                        ParsedPage(page_no=1, blocks=[ParsedBlock(type="text", text="제1조 목적")])
                    ],
                    raw_text="제1조 목적",
                )

        content_guess = RegulationMetadataGuess(
            document_name="본문에서 찾은 제목",
            regulation_id="reg-body-title",
            regulation_version="rev-20240101",
            revision_date="2024-01-01",
            effective_from="2024-01-01",
            supersedes_document_id=None,
            title_source="content",
            revision_date_source="content",
            effective_from_source="content",
            version_source="content",
        )

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            document = Document(
                document_id="doc_user_named",
                filename="인사규정_2024개정.pdf",
                document_name="인사규정_2024개정",
                document_name_source="user",
                file_type="pdf",
                file_hash="hash-user-named",
                regulation_id="reg-body-title",
                regulation_version="rev-20240101",
                tenant_id="default",
                status="uploaded",
            )
            repo.upsert_document(document)
            service = ProcessingService(settings=settings, repository=repo)

            with patch(
                "app.services.processing_service.get_parser",
                return_value=Parser(),
            ), patch(
                "app.services.processing_service.infer_regulation_metadata",
                return_value=content_guess,
            ):
                job = service.process(document.document_id, ChunkOptions(include_context_header=False))

            stored = repo.get_document(document.document_id)
            chunks = repo.get_chunks(document.document_id)

        self.assertEqual("completed", job.status)
        # 올린 사람이 정한 이름은 본문 제목이 이겨서는 안 된다.
        self.assertEqual("인사규정_2024개정", stored.document_name)
        self.assertEqual("인사규정_2024개정", chunks[0].metadata["document_name"])

    def test_process_still_upgrades_an_auto_named_upload_from_the_body_title(self) -> None:
        class Parser:
            def parse(self, path: Path, document_id: str) -> ParsedDocument:
                return ParsedDocument(
                    document_id=document_id,
                    source_file=path.name,
                    document_name="scan_0001",
                    file_type="pdf",
                    pages=[
                        ParsedPage(page_no=1, blocks=[ParsedBlock(type="text", text="제1조 목적")])
                    ],
                    raw_text="제1조 목적",
                )

        content_guess = RegulationMetadataGuess(
            document_name="본문에서 찾은 제목",
            regulation_id="reg-body-title",
            regulation_version="rev-20240101",
            revision_date="2024-01-01",
            effective_from="2024-01-01",
            supersedes_document_id=None,
            title_source="content",
            revision_date_source="content",
            effective_from_source="content",
            version_source="content",
        )

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            document = Document(
                document_id="doc_auto_named",
                filename="scan_0001.pdf",
                document_name="scan_0001",
                file_type="pdf",
                file_hash="hash-auto-named",
                regulation_id="reg-body-title",
                regulation_version="rev-20240101",
                tenant_id="default",
                status="uploaded",
            )
            repo.upsert_document(document)
            service = ProcessingService(settings=settings, repository=repo)

            with patch(
                "app.services.processing_service.get_parser",
                return_value=Parser(),
            ), patch(
                "app.services.processing_service.infer_regulation_metadata",
                return_value=content_guess,
            ):
                service.process(document.document_id, ChunkOptions(include_context_header=False))

            stored = repo.get_document(document.document_id)

        self.assertEqual("본문에서 찾은 제목", stored.document_name)

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
            unreviewed_hash = "sha256:" + "f" * 64
            now = datetime.now(timezone.utc)
            for run_id, tenant_id, agent_review in (
                (
                    "run_executed",
                    "tenant-a",
                    {
                        "status": "planned",
                        "api_call_count": 1,
                        "cache_scope_hash": cache_scope_hash,
                        "selected_candidates": [
                            {"content_hash": executed_hash, "chunk_id": "chunk_executed"},
                            {"content_hash": unreviewed_hash, "chunk_id": "chunk_unreviewed"},
                        ],
                        "unreviewed_chunk_ids": ["chunk_unreviewed"],
                    },
                ),
                (
                    "run_plan_only",
                    "tenant-a",
                    {
                        "status": "planned",
                        "api_call_count": 0,
                        "cache_scope_hash": cache_scope_hash,
                        "selected_candidates": [
                            {"content_hash": plan_only_hash, "chunk_id": "chunk_plan_only"}
                        ],
                    },
                ),
                (
                    "run_other_tenant",
                    "tenant-b",
                    {
                        "status": "reviewed",
                        "cache_scope_hash": cache_scope_hash,
                        "selected_candidates": [
                            {"content_hash": other_tenant_hash, "chunk_id": "chunk_other_tenant"}
                        ],
                    },
                ),
                (
                    "run_stale_scope",
                    "tenant-a",
                    {
                        "status": "reviewed",
                        "cache_scope_hash": stale_scope_hash,
                        "selected_candidates": [
                            {"content_hash": stale_scope_candidate_hash, "chunk_id": "chunk_stale"}
                        ],
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
                service._agent_review_cache_index("tenant-a", cache_scope_hash=cache_scope_hash),
                {executed_hash: ("doc_run_executed", "chunk_executed")},
            )

    def _chunk_with_findings(self, chunk_id: str, text: str, findings: dict | None):
        return Chunk(
            chunk_id=chunk_id,
            document_id="doc_source",
            chunk_type="article",
            text=text,
            normalized_text=text,
            retrieval_text=text,
            metadata={"agent_review_findings": findings} if findings else {},
        )

    def test_cached_review_reuses_previous_findings_on_the_new_document(self) -> None:
        """같은 규정을 다시 올려 검수를 건너뛰어도 지적은 그대로 따라와야 한다.

        예전에는 캐시가 호출만 건너뛰고 결과를 옮기지 않아, 검수를 켠 운영자가
        조항마다 'AI 검수 의견 없음'만 보게 됐다.
        """
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            service = ProcessingService(settings=settings, repository=repo)
            findings = {
                "risk_level": "medium",
                "issues": ["제3조 본문이 앞 조항에 붙어 있습니다."],
                "recommended_human_check": "제3조 경계를 확인하세요.",
            }
            source_chunks = [
                self._chunk_with_findings("source_1", "제1조(목적) 본문", findings),
                self._chunk_with_findings("source_2", "제2조(적용) 본문", None),
            ]
            repo.save_chunks("doc_source", source_chunks)
            new_chunks = [
                self._chunk_with_findings("new_1", "제1조(목적) 본문", None),
                self._chunk_with_findings("new_2", "제2조(적용) 본문", None),
            ]
            plan = {
                "candidates": [
                    {"chunk_id": "new_1", "content_hash": "sha256:aa", "cache_status": "reused"},
                    {"chunk_id": "new_2", "content_hash": "sha256:bb", "cache_status": "reused"},
                    {"chunk_id": "new_3", "content_hash": "sha256:cc", "cache_status": "reused"},
                ]
            }
            cache_index = {
                "sha256:aa": ("doc_source", "source_1"),
                "sha256:bb": ("doc_source", "source_2"),
                "sha256:cc": ("doc_missing", "source_9"),
            }

            reused = service._reuse_cached_review_findings(new_chunks, plan, cache_index)

            self.assertEqual(
                [(item["chunk_id"], item["has_findings"]) for item in reused],
                [("new_1", True), ("new_2", False)],
            )
            self.assertEqual(new_chunks[0].metadata.get("agent_review_findings"), findings)
            self.assertNotIn("agent_review_findings", new_chunks[1].metadata)

    def test_unreusable_candidates_are_reviewed_again(self) -> None:
        """결과가 남아 있지 않은 조항은 캐시에서 빠지고 이번 실행에서 다시 검수한다."""
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            repo = JsonRepository(settings)
            service = ProcessingService(settings=settings, repository=repo)
            new_chunks = [self._chunk_with_findings("new_1", "제1조(목적) 본문", None)]
            plan = {
                "candidates": [
                    {"chunk_id": "new_1", "content_hash": "sha256:aa", "cache_status": "reused"}
                ]
            }

            reused = service._reuse_cached_review_findings(
                new_chunks,
                plan,
                {"sha256:aa": ("doc_missing", "source_1")},
            )

            self.assertEqual(reused, [])


if __name__ == "__main__":
    unittest.main()
