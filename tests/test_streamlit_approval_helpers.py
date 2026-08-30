from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dataclasses import replace

from app.core.config import Settings
from app.core.institution_profiles import InstitutionProfile, InstitutionProfileRegistry
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.services.institution_purge_service import (
    InstitutionPurgePlan,
    InstitutionPurgeResult,
    InstitutionPurgeService,
)
from app.storage.repository import JsonRepository
from frontend import streamlit_app


class StreamlitApprovalHelperTests(unittest.TestCase):
    def test_registered_institution_cleanup_forwards_profile_tenant(self) -> None:
        registry = InstitutionProfileRegistry(
            profiles={
                "profile-a": InstitutionProfile(
                    profile_id="profile-a",
                    display_name="기관 A",
                    institution_name="기관 A",
                    tenant_id="tenant-a",
                )
            },
            default_profile_id="profile-a",
        )
        completed = InstitutionPurgeResult(profile_id="profile-a")
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "institutions.json"
            with (
                patch.object(
                    streamlit_app,
                    "_institution_profiles_storage_path",
                    return_value=registry_path,
                ),
                patch.object(
                    streamlit_app,
                    "_purge_institution_documents",
                    return_value=completed,
                ) as purge,
                patch.object(streamlit_app.st, "session_state", {}),
                patch.object(
                    streamlit_app,
                    "_selected_institution_profile_id",
                    return_value=None,
                ),
            ):
                result = streamlit_app._delete_registered_institution(
                    registry,
                    "profile-a",
                    purge_documents=True,
                )

        self.assertIs(completed, result)
        purge.assert_called_once_with("profile-a", tenant_id="tenant-a")

    def test_keep_data_path_does_not_call_purge(self) -> None:
        registry = InstitutionProfileRegistry(
            profiles={
                "profile-a": InstitutionProfile(
                    profile_id="profile-a",
                    display_name="기관 A",
                    institution_name="기관 A",
                    tenant_id="tenant-a",
                )
            },
            default_profile_id="profile-a",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(
                    streamlit_app,
                    "_institution_profiles_storage_path",
                    return_value=Path(tmp) / "institutions.json",
                ),
                patch.object(streamlit_app, "_purge_institution_documents") as purge,
                patch.object(streamlit_app.st, "session_state", {}),
                patch.object(
                    streamlit_app,
                    "_selected_institution_profile_id",
                    return_value=None,
                ),
            ):
                result = streamlit_app._delete_registered_institution(
                    registry,
                    "profile-a",
                    purge_documents=False,
                )

        self.assertIsNone(result)
        purge.assert_not_called()

    def test_institution_purge_plan_forwards_explicit_tenant(self) -> None:
        expected = InstitutionPurgePlan(profile_id="profile-a")
        with patch.object(streamlit_app, "_institution_purge_service") as factory:
            factory.return_value.plan.return_value = expected

            actual = streamlit_app._institution_purge_plan(
                "profile-a",
                tenant_id="tenant-a",
            )

        self.assertIs(expected, actual)
        factory.return_value.plan.assert_called_once_with(
            "profile-a",
            tenant_id="tenant-a",
        )

    def test_source_context_resolves_pdf_page_bbox_and_uploaded_path(self) -> None:
        document = Document(
            document_id="doc_pdf",
            filename="rules.pdf",
            document_name="Rules",
            file_type="pdf",
            file_hash="hash",
            tenant_id="default",
        )
        chunk = Chunk(
            chunk_id="chunk-pdf",
            document_id="doc_pdf",
            chunk_type="article",
            text="전처리 본문",
            source_page_start=2,
            metadata={"source_page": 3, "source_bbox": [10, 20, 30, 40], "raw_text": "원본 본문"},
        )

        context = streamlit_app._approval_source_context(document, chunk)

        self.assertEqual("pdf", context["file_type"])
        self.assertEqual(3, context["source_page"])
        self.assertEqual([10, 20, 30, 40], context["source_bbox"])
        self.assertEqual("원본 본문", context["raw_text"])
        self.assertEqual(Path(streamlit_app.settings.uploads_dir) / "doc_pdf.pdf", context["source_path"])

    def test_source_context_preserves_hwp_kordoc_table_source(self) -> None:
        document = Document(
            document_id="doc_hwp",
            filename="rules.hwp",
            document_name="Rules",
            file_type="hwp",
            file_hash="hash",
            tenant_id="default",
        )
        chunk = Chunk(
            chunk_id="chunk-table",
            document_id="doc_hwp",
            chunk_type="table",
            text="승격 표",
            metadata={
                "raw_text": "원본 표",
                "table_source": "kordoc",
                "kordoc_table_promoted": True,
                "table_cell_rows": [
                    {"row_index": 0, "cells": ["구분", "내용"], "raw": "구분 | 내용"},
                    {"row_index": 1, "cells": ["A", "B"]},
                ],
            },
        )

        context = streamlit_app._approval_source_context(document, chunk)
        raw_rows = streamlit_app._approval_kordoc_raw_rows(chunk)

        self.assertEqual("hwp", context["file_type"])
        self.assertEqual("kordoc", context["table_source"])
        self.assertTrue(context["kordoc_table_promoted"])
        self.assertEqual(["구분 | 내용", "A | B"], raw_rows)

    def test_processed_preview_includes_promoted_table_and_reflected_ai_items_only(self) -> None:
        chunk = Chunk(
            chunk_id="chunk-table",
            document_id="doc_hwp",
            chunk_type="table",
            text="기본 본문",
            metadata={"table_markdown": "| 구분 | 내용 |\n|---|---|\n| A | B |"},
        )
        review_items = [
            {"item_id": "a", "title": "표 구조", "suggestion": "Kordoc 원본과 비교"},
            {"item_id": "b", "title": "각주", "suggestion": "각주 확인"},
        ]

        preview = streamlit_app._approval_processed_preview_text(
            chunk,
            review_items,
            {"a": "reflect", "b": "skip"},
        )

        self.assertIn("기본 본문", preview)
        self.assertIn("[표]", preview)
        self.assertIn("| 구분 | 내용 |", preview)
        self.assertIn("표 구조: Kordoc 원본과 비교", preview)
        self.assertNotIn("각주 확인", preview)


    def test_mcp_source_metadata_auto_fill_uses_local_provenance(self) -> None:
        document = Document(
            document_id="doc_missing_source",
            filename="rules.hwp",
            document_name="Rules",
            file_type="hwp",
            file_hash="hash",
            tenant_id="default",
        )
        with tempfile.TemporaryDirectory() as tmp:
            repository = JsonRepository(Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp)))
            repository.upsert_document(document)

            updated, patch = streamlit_app._ensure_mcp_source_metadata(
                document,
                tenant_id="default",
                target_repository=repository,
            )
            stored = repository.get_document("doc_missing_source")

        self.assertEqual(
            {"institution_name", "profile_id", "source_system", "source_url"},
            set(patch),
        )
        self.assertEqual("Local Upload", updated.institution_name)
        self.assertEqual("local-default", updated.profile_id)
        self.assertEqual("LOCAL_UPLOAD", updated.source_system)
        self.assertEqual("local-upload://doc_missing_source", updated.source_url)
        self.assertIsNotNone(stored)
        self.assertEqual("local-upload://doc_missing_source", stored.source_url)

    def test_mcp_connection_gate_does_not_block_on_missing_source_metadata_warning(self) -> None:
        document = Document(
            document_id="doc_missing_source",
            filename="rules.hwp",
            document_name="Rules",
            file_type="hwp",
            file_hash="hash",
            tenant_id="default",
        )

        gate = streamlit_app._mcp_connection_gate(
            {
                "indexing_status": "indexed",
                "vector_summary": {"record_count": 1},
                "vector_consistency": {"stale_count": 0},
            },
            approved_count=1,
        )

        self.assertEqual(
            {
                "institution_name",
                "profile_id",
                "source_system",
                "source_url",
                "regulation_id",
                "regulation_version",
                "effective_from",
            },
            set(streamlit_app._missing_mcp_source_metadata(document)),
        )
        self.assertTrue(gate["ready"])
        self.assertEqual("approved_chunks_indexed", gate["reason"])

    def test_mcp_kordoc_preflight_blocks_stale_missing_hwp_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_kordoc_preflight",
                    filename="rules.hwp",
                    document_name="Rules",
                    file_type="hwp",
                    file_hash="hash",
                    tenant_id="default",
                    status="completed",
                )
            )
            repository.save_chunks(
                "doc_kordoc_preflight",
                [
                    Chunk(
                        chunk_id="chunk-kordoc-preflight",
                        document_id="doc_kordoc_preflight",
                        chunk_type="article",
                        text="draft",
                    )
                ],
            )

            preflight = streamlit_app._mcp_kordoc_preflight(
                repository,
                ["doc_kordoc_preflight"],
                command="kordoc",
            )

        self.assertFalse(preflight["ready"])
        self.assertEqual(["doc_kordoc_preflight"], [item["document_id"] for item in preflight["missing"]])
        self.assertEqual("hwp", preflight["missing"][0]["file_type"])

    def test_mcp_kordoc_preflight_allows_parsed_hwp_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_kordoc_preflight",
                    filename="rules.hwp",
                    document_name="Rules",
                    file_type="hwp",
                    file_hash="hash",
                    tenant_id="default",
                    status="completed",
                )
            )
            repository.save_chunks(
                "doc_kordoc_preflight",
                [
                    Chunk(
                        chunk_id="chunk-kordoc-preflight",
                        document_id="doc_kordoc_preflight",
                        chunk_type="table",
                        text="parsed",
                        metadata={
                            "kordoc_table_parser_status": "parsed",
                            "kordoc_table_count": 1,
                            "kordoc_table_inventory": {
                                "status": "parsed",
                                "parser": "kordoc",
                                "table_count": 1,
                            },
                        },
                    )
                ],
            )

            preflight = streamlit_app._mcp_kordoc_preflight(
                repository,
                ["doc_kordoc_preflight"],
                command="kordoc",
            )

        self.assertTrue(preflight["ready"])
        self.assertEqual(1, preflight["parsed_document_count"])
        self.assertEqual([], preflight["missing"])

    def test_kordoc_installer_candidates_include_source_setup_script(self) -> None:
        candidates = streamlit_app._kordoc_installer_candidates()

        self.assertTrue(candidates)
        self.assertTrue(any(candidate.name == "INSTALL_KORDOC_KO.ps1" for candidate in candidates))

    def test_kordoc_installer_redacts_operator_output(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout="Kordoc 4.2.3\nsource=C:\\Users\\someone\\Desktop\\rules.hwp\n",
            stderr="",
        )
        with patch.object(streamlit_app.sys, "platform", "win32"), patch.object(
            streamlit_app, "_kordoc_installer_candidates", return_value=[Path("C:/Npm/INSTALL_KORDOC_KO.ps1")]
        ), patch.object(streamlit_app.subprocess, "run", return_value=completed) as run:
            result = streamlit_app._run_kordoc_installer()

        self.assertTrue(result["ok"])
        self.assertNotIn("C:\\Users\\someone", result["output"])
        self.assertIn("[local-path-redacted]", result["output"])
        self.assertIn("-PersistUserPath", run.call_args.args[0])

    def test_replace_workflow_document_id_switches_only_the_reprocessed_source(self) -> None:
        state = {
            streamlit_app.WORKFLOW_DOCUMENT_IDS_KEY: ["doc-old", "doc-other"],
            streamlit_app.WORKFLOW_SELECTED_DOCUMENT_IDS_KEY: ["doc-old", "doc-other"],
            "document_id": "doc-old",
            streamlit_app.WORKFLOW_MCP_GATE_CACHE_KEY: {"cached": True},
            streamlit_app.DOCUMENT_CONTEXT_CACHE_KEY: {
                "document_id": "doc-old",
                "revision": (),
                "context": {},
            },
        }

        with patch.object(streamlit_app.st, "session_state", state):
            streamlit_app._replace_workflow_document_id("doc-old", "doc-new")

        self.assertEqual(["doc-new", "doc-other"], state[streamlit_app.WORKFLOW_DOCUMENT_IDS_KEY])
        self.assertEqual(["doc-new", "doc-other"], state[streamlit_app.WORKFLOW_SELECTED_DOCUMENT_IDS_KEY])
        self.assertEqual("doc-new", state["document_id"])
        self.assertNotIn(streamlit_app.WORKFLOW_MCP_GATE_CACHE_KEY, state)
        self.assertNotIn(streamlit_app.DOCUMENT_CONTEXT_CACHE_KEY, state)


class StreamlitRegulationDirectoryTests(unittest.TestCase):
    """규정집 통합본 한 파일이든 규정별 개별 파일이든 같은 '규정 단위'로 나뉘어야 한다."""

    @staticmethod
    def _chunk(chunk_id: str, number: str, title: str, status: str = "draft") -> Chunk:
        return Chunk(
            chunk_id=chunk_id,
            document_id="doc_book",
            chunk_type="article",
            text="본문",
            approval_status=status,
            metadata={"regulation_no": number, "regulation_title": title},
        )

    def test_combined_book_splits_into_one_unit_per_regulation(self) -> None:
        chunks = [
            self._chunk("c1", "4-2-1", "인사규정"),
            self._chunk("c2", "4-2-1", "인사규정", status="approved"),
            self._chunk("c3", "1-2-2", "이사회운영규정"),
            self._chunk("c4", "4-2-1", "인사규정"),
        ]

        units = streamlit_app._document_regulation_units(chunks)

        self.assertEqual(2, len(units))
        personnel, board = units
        # 등장 순서를 유지해야 규정집 목차 순서대로 검수할 수 있다.
        self.assertEqual("인사규정", personnel["title"])
        self.assertEqual(["c1", "c2", "c4"], personnel["chunk_ids"])
        self.assertEqual(2, personnel["pending"])
        self.assertEqual(1, personnel["approved"])
        self.assertEqual("이사회운영규정", board["title"])
        self.assertEqual("4-2-1. 인사규정", streamlit_app._regulation_unit_label(personnel))

    def test_single_regulation_file_collapses_to_one_unit(self) -> None:
        chunks = [
            self._chunk("c1", "4-2-1", "인사규정"),
            self._chunk("c2", "4-2-1", "인사규정"),
        ]

        units = streamlit_app._document_regulation_units(chunks)

        # 단위가 하나면 _page_approval 이 문서 내 디렉터리를 아예 그리지 않는다.
        self.assertEqual(1, len(units))
        self.assertEqual(["c1", "c2"], units[0]["chunk_ids"])

    def test_missing_regulation_metadata_still_yields_a_named_unit(self) -> None:
        chunk = Chunk(
            chunk_id="c1",
            document_id="doc_book",
            chunk_type="article",
            text="본문",
            metadata={},
        )

        units = streamlit_app._document_regulation_units([chunk])

        self.assertEqual(1, len(units))
        self.assertEqual("(규정명 미확인)", units[0]["title"])
        self.assertEqual("(규정명 미확인)", streamlit_app._regulation_unit_label(units[0]))


class StreamlitAiReviewStatusTests(unittest.TestCase):
    """AI 검수를 켜지 않은 문서를 '실행됐다'고 말하지 않는지 고정한다."""

    def test_not_requested_run_is_never_reported_as_executed(self) -> None:
        summary = {
            "status": "skipped",
            "skip_reason": "agent_review_not_requested",
            "request_enabled": False,
        }

        tag, message, executed = streamlit_app._ai_review_status_text(summary)

        self.assertFalse(executed)
        self.assertEqual("AI 검수 사용 안 함", tag)
        self.assertIn("AI 추가 검수를 켜지 않고", message)
        self.assertNotIn("실행됐습니다", message)
        self.assertFalse(streamlit_app._agent_review_requested(summary))

    def test_missing_run_record_does_not_claim_the_ai_ran(self) -> None:
        tag, message, executed = streamlit_app._ai_review_status_text({})

        self.assertFalse(executed)
        self.assertEqual("AI 검수 사용 안 함", tag)
        self.assertNotIn("실행됐습니다", message)
        self.assertFalse(streamlit_app._agent_review_requested({}))
        self.assertFalse(streamlit_app._agent_review_requested(None))

    def test_requested_run_keeps_the_configuration_tag(self) -> None:
        summary = {
            "status": "skipped",
            "skip_reason": "quality_gate_clean",
            "request_enabled": True,
        }

        tag, message, executed = streamlit_app._ai_review_status_text(summary)

        self.assertFalse(executed)
        self.assertEqual("AI 검수 준비/설정 확인", tag)
        self.assertIn("품질 검사가 깨끗해", message)
        self.assertTrue(streamlit_app._agent_review_requested(summary))

    def test_executed_run_reports_the_draft_as_complete(self) -> None:
        tag, _message, executed = streamlit_app._ai_review_status_text(
            {"status": "executed", "skip_reason": "", "request_enabled": True}
        )

        self.assertTrue(executed)
        self.assertEqual("AI 검수 초안 완료", tag)

    def test_reused_review_is_not_reported_as_no_result(self) -> None:
        """이전 결과를 재사용한 규정을 '결과 없음'으로 적으면 화면이 거짓말을 한다.

        같은 규정을 다시 올리면 제공자를 부르지 않는다. 그때 선정 수와 호출 수만 보고
        문구를 고르면, 검수 의견이 붙어 있는데도 '검수 결과가 없다'고 안내하게 된다.
        """
        summary = {
            "status": "skipped",
            "skip_reason": "review_candidates_cached",
            "request_enabled": True,
            "selected_count": 0,
            "api_call_count": 0,
            "reused_chunk_count": 37,
            "reused_finding_count": 31,
            "reused_candidates": [{"chunk_id": "chunk-1"}, {"chunk_id": "chunk-2"}],
        }

        note = streamlit_app._approval_sheet_ai_review_note(summary)

        self.assertIn("재사용", note)
        self.assertNotIn("결과가 없어", note)
        self.assertEqual(
            {"chunk-1", "chunk-2"},
            streamlit_app._agent_review_selected_chunk_ids(summary),
        )
        self.assertEqual(
            {"chunk-1", "chunk-2"},
            streamlit_app._agent_review_reviewed_chunk_ids(summary),
        )

    def test_failed_batches_are_not_counted_as_reviewed(self) -> None:
        """호출이 실패한 조항까지 '검수 완료'로 세면 아무도 안 본 조항이 확인된 것이 된다."""
        summary = {
            "status": "executed",
            "request_enabled": True,
            "selected_candidates": [{"chunk_id": "chunk-1"}, {"chunk_id": "chunk-2"}],
            "unreviewed_chunk_ids": ["chunk-2"],
        }

        self.assertEqual(
            {"chunk-1", "chunk-2"},
            streamlit_app._agent_review_selected_chunk_ids(summary),
        )
        self.assertEqual({"chunk-1"}, streamlit_app._agent_review_reviewed_chunk_ids(summary))

    def test_selected_but_unexecuted_run_reports_nothing_as_reviewed(self) -> None:
        summary = {
            "status": "api_configuration_needed",
            "request_enabled": True,
            "selected_candidates": [{"chunk_id": "chunk-1"}],
        }

        self.assertEqual(set(), streamlit_app._agent_review_reviewed_chunk_ids(summary))

    def test_approval_sheet_caption_does_not_repeat_the_edit_instruction(self) -> None:
        """같은 문장이 한 줄 안에 두 번 나오면 안내가 아니라 잡음이 된다."""
        source = Path(streamlit_app.__file__).read_text(encoding="utf-8")
        caption_start = source.index("✅ 최종본 칸의 내용이 승인·색인되어 MCP에 들어갑니다.")
        caption_block = source[caption_start : caption_start + 400]
        self.assertNotIn("고칠 곳은 가운데 전처리본 칸에 직접 입력하세요.", caption_block)

    def test_approval_default_text_is_never_the_ai_rewrite(self) -> None:
        """AI가 쓴 글이 사람 손을 안 거치고 승인·색인되는 경로가 없어야 한다.

        기본값이 곧 승인될 본문이라, 여기에 AI 교정본을 넣으면 ``2012. 6. 14.``가
        ``2012. 06. 14.``로 바뀐 채 법적 근거로 굳는다.
        """
        chunk = SimpleNamespace(
            chunk_id="chunk-1",
            text="개정 2012. 6. 14. 규정",
            ai_preprocessed_text="개정 2012. 06. 14. 규정",
        )
        state: dict = {}
        original_st = streamlit_app.st
        streamlit_app.st = SimpleNamespace(session_state=state)
        try:
            default_text = streamlit_app._approval_edited_text_from_session("doc-1", chunk)
        finally:
            streamlit_app.st = original_st

        self.assertEqual("개정 2012. 6. 14. 규정", default_text)
        self.assertNotIn("06.", default_text)

    def test_findings_column_shows_what_the_ai_actually_reported(self) -> None:
        chunk = SimpleNamespace(
            chunk_id="chunk-1",
            metadata={
                "agent_review_findings": {
                    "risk_level": "high",
                    "issues": ["조문 경계가 합쳐졌을 수 있음"],
                    "recommended_human_check": "제3조 시작 지점 대조",
                }
            },
        )

        self.assertEqual(
            ["조문 경계가 합쳐졌을 수 있음"],
            streamlit_app._agent_review_findings(chunk)["issues"],
        )
        self.assertEqual({}, streamlit_app._agent_review_findings(SimpleNamespace(metadata={})))

    def test_sheet_note_does_not_claim_the_review_was_off_after_a_failed_call(self) -> None:
        """켜고 돌렸는데 호출이 끝나지 못한 규정을 '켜지 않았다'고 적으면 안 된다."""
        note = streamlit_app._approval_sheet_ai_review_note(
            {
                "status": "provider_execution_failed",
                "skip_reason": "provider_request_failed",
                "request_enabled": True,
                "selected_count": 20,
                "api_call_count": 0,
            }
        )

        self.assertNotIn("켜지 않았으므로", note)
        self.assertIn("실행이 끝나지 못했습니다", note)
        self.assertIn("provider_execution_failed", note)
        self.assertIn("다시 전처리", note)

    def test_sheet_note_still_says_it_was_off_when_it_really_was(self) -> None:
        note = streamlit_app._approval_sheet_ai_review_note(
            {
                "status": "skipped",
                "skip_reason": "agent_review_not_requested",
                "request_enabled": False,
            }
        )

        self.assertIn("AI 추가 검수를 켜지 않았으므로", note)
        self.assertNotIn("⚠️", note)

    def test_sheet_note_without_any_run_record_does_not_blame_a_failure(self) -> None:
        note = streamlit_app._approval_sheet_ai_review_note({})

        self.assertIn("켜지 않았으므로", note)
        self.assertNotIn("⚠️", note)

    def test_scope_caption_states_the_per_document_limit(self) -> None:
        caption = streamlit_app._ai_review_scope_caption(
            {"limits": {"max_chunks_per_document": 20}}
        )

        # "AI를 켰는데 왜 사람이 다 봐야 하냐"는 오해가 남지 않도록 범위와 한도를 함께 말한다.
        self.assertIn("의심 구간", caption)
        self.assertIn("최대 20개", caption)
        self.assertIn("③ 검수하고 승인", caption)

    def test_scope_caption_says_the_whole_document_when_there_is_no_cap(self) -> None:
        """한도가 없으면 '의심 구간만 본다'고 말하면 안 된다. 전체를 보기 때문이다."""
        caption = streamlit_app._ai_review_scope_caption({})

        self.assertIn("모든 조항", caption)
        self.assertNotIn("최대", caption)
        self.assertIn("③ 검수하고 승인", caption)



class StreamlitQualityBannerTests(unittest.TestCase):
    """깨진 글자가 있던 문서에 "통과했으니 넘어가도 된다"고 말하지 않는지."""

    def _report(self, **metrics):
        return SimpleNamespace(passed=True, text_quality_metrics=dict(metrics))

    def _render(self, report) -> dict[str, list[str]]:
        calls: dict[str, list[str]] = {"success": [], "warning": [], "info": []}

        def _record(kind):
            return lambda message, *args, **kwargs: calls[kind].append(str(message))

        with patch.multiple(
            streamlit_app.st,
            success=_record("success"),
            warning=_record("warning"),
            info=_record("info"),
        ):
            streamlit_app._render_quality_banner(report)
        return calls

    def test_counts_include_the_chars_the_normalizer_removed(self) -> None:
        counts = streamlit_app._quality_mojibake_counts(
            self._report(
                hwp_mojibake_artifact_chunks=2,
                suspicious_regulation_metadata_count=1,
                mojibake_removed_char_count=40,
            )
        )

        self.assertEqual((2, 1, 40), counts)

    def test_counts_are_zero_when_the_report_has_no_metrics(self) -> None:
        self.assertEqual((0, 0, 0), streamlit_app._quality_mojibake_counts(None))
        self.assertEqual((0, 0, 0), streamlit_app._quality_mojibake_counts(self._report()))

    def test_clean_document_still_gets_the_pass_message(self) -> None:
        calls = self._render(
            self._report(
                hwp_mojibake_artifact_chunks=0,
                suspicious_regulation_metadata_count=0,
                mojibake_removed_char_count=0,
            )
        )

        self.assertEqual(1, len(calls["success"]))
        self.assertEqual([], calls["warning"])

    def test_remaining_mojibake_is_reported_as_needing_repair(self) -> None:
        calls = self._render(
            self._report(
                hwp_mojibake_artifact_chunks=3,
                suspicious_regulation_metadata_count=1,
                mojibake_removed_char_count=0,
            )
        )

        self.assertEqual([], calls["success"])
        self.assertEqual(1, len(calls["warning"]))
        self.assertIn("조항 3개 본문", calls["warning"][0])
        self.assertIn("규정번호·제목 1건", calls["warning"][0])

    def test_cleaned_document_is_not_reported_as_simply_passing(self) -> None:
        # 지워서 화면은 깨끗해졌지만, 표·수식이 통째로 빠진 자리가 남아 있을 수 있다.
        calls = self._render(
            self._report(
                hwp_mojibake_artifact_chunks=0,
                suspicious_regulation_metadata_count=0,
                mojibake_removed_char_count=40,
            )
        )

        self.assertEqual([], calls["success"])
        self.assertEqual(1, len(calls["warning"]))
        self.assertIn("40자", calls["warning"][0])
        self.assertIn("③ 검수하고 승인", calls["warning"][0])



class StreamlitAiReviewSetupBlockerTests(unittest.TestCase):
    """AI 검수를 켜도 실행될 수 없는 상태를 전처리 전에 알려 주는지."""

    def _settings(self, **overrides) -> Settings:
        return replace(Settings(), **overrides)

    def test_reports_the_feature_switch_when_agent_review_is_off(self) -> None:
        blocker = streamlit_app._ai_review_setup_blocker(
            self._settings(enable_agent_review=False)
        )

        self.assertIn("꺼져 있어", blocker)
        self.assertIn("ENABLE_AGENT_REVIEW", blocker)

    def test_reports_the_missing_api_key_once_the_feature_is_on(self) -> None:
        blocker = streamlit_app._ai_review_setup_blocker(
            self._settings(
                enable_agent_review=True,
                llm_provider="openai",
                openai_api_key="",
                agent_review_model="gpt-4.1-mini",
            )
        )

        self.assertIn("API 키", blocker)
        self.assertIn("관리자 설정", blocker)

    def test_is_silent_when_the_provider_is_fully_configured(self) -> None:
        blocker = streamlit_app._ai_review_setup_blocker(
            self._settings(
                enable_agent_review=True,
                llm_provider="openai",
                openai_api_key="sk-test",
                agent_review_model="gpt-4.1-mini",
            )
        )

        self.assertEqual("", blocker)

    def test_sidebar_blocks_turning_ai_review_on_without_a_working_connection(self) -> None:
        source = Path(streamlit_app.__file__).read_text(encoding="utf-8")

        # 켜기 전에 실행 가능 여부를 확인해, '켜짐'과 '실제로 실행됨'이 어긋나지 않게 한다.
        self.assertIn("blocker_after_save = _ai_review_setup_blocker(candidate)", source)
        self.assertIn("if blocker_after_save:\n                    st.error(blocker_after_save)", source)
        # 전처리는 설정이 갖춰졌을 때만 AI 검수를 요청한다.
        self.assertIn(
            "ai_review_requested = bool(settings.enable_agent_review)"
            " and not _ai_review_setup_blocker(settings)",
            source,
        )

    def test_results_page_explains_how_to_turn_the_api_on(self) -> None:
        message = streamlit_app.AI_REVIEW_STATUS_MESSAGES[
            ("api_configuration_needed", "agent_review_api_disabled")
        ]

        self.assertIn("관리자 설정", message)
        self.assertIn("다시 전처리", message)



class StreamlitResultsStepVisibilityTests(unittest.TestCase):
    """AI 추가 검수를 쓰지 않은 규정에서 '② 결과 확인'을 단계에서 빼는지."""

    def _ctx(self, summary) -> dict:
        return {"document_id": "doc-1", "agent_review_summary": summary}

    def test_results_step_is_skipped_when_the_ai_was_never_requested(self) -> None:
        ctx = self._ctx({"status": "skipped", "skip_reason": "agent_review_not_requested"})

        self.assertFalse(streamlit_app._results_step_is_used(ctx))

    def test_results_step_is_kept_when_the_ai_review_was_requested(self) -> None:
        ctx = self._ctx({"request_enabled": True, "status": "executed"})

        self.assertTrue(streamlit_app._results_step_is_used(ctx))

    def test_results_step_is_kept_before_any_document_exists(self) -> None:
        # 문서가 없을 때 메뉴가 나타났다 사라지면 더 헷갈린다.
        self.assertTrue(streamlit_app._results_step_is_used(None))

    def test_nav_drops_the_results_page_for_a_parser_only_document(self) -> None:
        pages = streamlit_app._primary_nav_pages(
            self._ctx({"status": "skipped", "skip_reason": "agent_review_not_requested"})
        )

        self.assertNotIn(streamlit_app.NAV_RESULTS, pages)
        self.assertIn(streamlit_app.NAV_PREPROCESS, pages)
        self.assertIn(streamlit_app.NAV_APPROVAL, pages)

    def test_nav_keeps_the_results_page_while_it_is_open(self) -> None:
        # 라디오 선택값이 목록에서 빠지면 화면이 통째로 튕겨 나간다.
        pages = streamlit_app._primary_nav_pages(
            self._ctx({"status": "skipped", "skip_reason": "agent_review_not_requested"}),
            streamlit_app.NAV_RESULTS,
        )

        self.assertIn(streamlit_app.NAV_RESULTS, pages)

    def test_nav_keeps_every_page_when_the_ai_review_ran(self) -> None:
        pages = streamlit_app._primary_nav_pages(self._ctx({"request_enabled": True}))

        self.assertEqual(list(streamlit_app.PRIMARY_NAV_PAGES), pages)

    def test_approval_page_shows_the_quality_banner_when_results_is_skipped(self) -> None:
        source = Path(streamlit_app.__file__).read_text(encoding="utf-8")

        # ②를 건너뛰면 깨진 글자 경고를 볼 곳이 ③밖에 없다.
        self.assertIn(
            "if not _results_step_is_used(ctx):\n        _render_quality_banner(ctx.get(\"quality_report\"))",
            source,
        )

    def test_beginner_gate_does_not_demand_a_page_that_is_hidden(self) -> None:
        source = Path(streamlit_app.__file__).read_text(encoding="utf-8")

        self.assertIn(
            "if beginner_mode_active and _results_step_is_used(ctx) "
            "and not beginner_current_results_confirmed:",
            source,
        )


class PreprocessProgressGaugeTests(unittest.TestCase):
    def test_gauge_holds_the_highest_value_reached(self) -> None:
        floor: dict[str, int] = {}

        self.assertEqual(40, streamlit_app._monotonic_percent(floor, "overall", 40))
        # 단계가 바뀌며 낮은 값이 보고돼도 게이지는 뒤로 감기지 않는다.
        self.assertEqual(40, streamlit_app._monotonic_percent(floor, "overall", 5))
        self.assertEqual(61, streamlit_app._monotonic_percent(floor, "overall", 61))

    def test_each_gauge_keeps_its_own_floor(self) -> None:
        floor: dict[str, int] = {}

        streamlit_app._monotonic_percent(floor, "overall", 80)

        self.assertEqual(10, streamlit_app._monotonic_percent(floor, "file-1", 10))

    def test_clamps_out_of_range_reports(self) -> None:
        floor: dict[str, int] = {}

        self.assertEqual(0, streamlit_app._monotonic_percent(floor, "overall", -20))
        self.assertEqual(100, streamlit_app._monotonic_percent(floor, "overall", 140))

    def test_preprocess_loop_routes_every_gauge_through_the_floor(self) -> None:
        source = Path(streamlit_app.__file__).read_text(encoding="utf-8")
        start = source.index('progress_bar = st.progress(0, text="Saving uploaded file")')
        end = source.index("document = completed_documents[-1]", start)
        loop_source = source[start:end]

        # 하트비트 경로가 공식을 따로 계산해 바닥값을 우회하면 안 된다.
        self.assertNotIn("int(((file_index + last_fraction) / total_files) * 100)", loop_source)
        self.assertIn("safe_progress = _overall_percent(file_index, last_fraction)", loop_source)
        self.assertIn("last_fraction = max(last_fraction, reported_fraction)", loop_source)
        # 낱개를 셀 수 없는 단계로 넘어가면 이전 단계 숫자를 남기지 않는다.
        self.assertIn("regulation_progress_box.empty()", loop_source)


class InstitutionStorageDirAgreementTests(unittest.TestCase):
    """폴더를 만드는 쪽과 지우는 쪽이 같은 이름을 쓰는지 고정한다.

    두 쪽이 이름을 따로 계산하던 동안, 기관을 지워도 대기 파일과 저장한 작업은 그대로
    남았다. 기관 ID는 기관명 해시라 같은 이름으로 다시 등록하면 전부 되살아났다.
    """

    def test_frontend_folders_are_the_ones_the_purge_service_removes(self) -> None:
        profile_id = "institution-2974949d31f0307e"
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            settings = Settings(data_dir=data_dir)
            repository = JsonRepository(settings)
            service = InstitutionPurgeService(settings=settings, repository=repository)
            with patch.object(streamlit_app, "settings", settings):
                pending_dir = streamlit_app._pending_upload_dir(profile_id)
                projects_dir = streamlit_app._operator_projects_dir(profile_id, create=True)
            (pending_dir / "인사규정.hwp").write_bytes(b"hwp")
            (projects_dir / "project-abc.json").write_text("{}", encoding="utf-8")

            plan = service.plan(profile_id)
            self.assertEqual(1, plan.pending_file_count)
            self.assertEqual(1, plan.saved_project_count)
            self.assertEqual({profile_id}, service.profile_ids_with_stored_data())

            service.purge(profile_id)

            self.assertFalse(pending_dir.exists())
            self.assertFalse(projects_dir.exists())

    def test_marker_file_is_not_offered_as_an_uploaded_regulation(self) -> None:
        profile_id = "institution-2974949d31f0307e"
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            with patch.object(streamlit_app, "settings", settings):
                streamlit_app._pending_upload_dir(profile_id)

                self.assertEqual([], streamlit_app._pending_upload_paths(profile_id))


if __name__ == "__main__":
    unittest.main()
