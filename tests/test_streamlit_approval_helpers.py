from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from dataclasses import replace

from app.core.config import Settings
from app.core.institution_profiles import InstitutionProfile, InstitutionProfileRegistry
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.services.approval_governance import approval_review_completion_state
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
        self.assertEqual("", result["output"])
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


class StreamlitOpenLocalArtifactTests(unittest.TestCase):
    """로컬 파일 열기는 PowerShell 콘솔을 띄우지 않고 셸 연결 프로그램으로 연다."""

    def test_windows_opens_with_startfile_without_spawning_a_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "labels.csv"
            artifact.write_text("document_id\n", encoding="utf-8")
            with patch.object(streamlit_app.sys, "platform", "win32"), patch.object(
                streamlit_app.os, "startfile", create=True
            ) as startfile, patch.object(streamlit_app.subprocess, "Popen") as popen:
                streamlit_app._open_local_artifact(artifact)

        startfile.assert_called_once_with(str(artifact))
        popen.assert_not_called()

    def test_missing_artifact_still_raises_file_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            streamlit_app.os, "startfile", create=True
        ) as startfile:
            with self.assertRaises(FileNotFoundError):
                streamlit_app._open_local_artifact(Path(tmp) / "missing.csv")

        startfile.assert_not_called()

    def test_non_windows_raises_clear_os_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "labels.csv"
            artifact.write_text("document_id\n", encoding="utf-8")
            with patch.object(streamlit_app.sys, "platform", "linux"), patch.object(
                streamlit_app.os, "startfile", create=True
            ) as startfile:
                with self.assertRaises(OSError) as raised:
                    streamlit_app._open_local_artifact(artifact)

        self.assertNotIsInstance(raised.exception, FileNotFoundError)
        self.assertIn("Windows", str(raised.exception))
        startfile.assert_not_called()

    def test_runnable_file_types_are_refused_without_opening(self) -> None:
        for name in ("run.exe", "run.BAT", "run.cmd", "run.ps1", "run.lnk", "run.js", "noext"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                artifact = Path(tmp) / name
                artifact.write_text("x", encoding="utf-8")
                with patch.object(streamlit_app.sys, "platform", "win32"), patch.object(
                    streamlit_app.os, "startfile", create=True
                ) as startfile:
                    with self.assertRaises(OSError) as raised:
                        streamlit_app._open_local_artifact(artifact)

                self.assertNotIsInstance(raised.exception, FileNotFoundError)
                startfile.assert_not_called()

    def test_regulation_documents_and_review_files_are_allowed(self) -> None:
        for name in ("rule.pdf", "rule.DOCX", "rule.hwpx", "rule.hwp", "packet.md", "labels.csv"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                artifact = Path(tmp) / name
                artifact.write_text("x", encoding="utf-8")
                with patch.object(streamlit_app.sys, "platform", "win32"), patch.object(
                    streamlit_app.os, "startfile", create=True
                ) as startfile:
                    streamlit_app._open_local_artifact(artifact)

                startfile.assert_called_once_with(str(artifact))

    def test_network_paths_are_refused(self) -> None:
        class _FakeNetworkPath:
            suffix = ".csv"

            def exists(self) -> bool:
                return True

            def __str__(self) -> str:
                return "\\\\server\\share\\labels.csv"

        with patch.object(streamlit_app.sys, "platform", "win32"), patch.object(
            streamlit_app.os, "startfile", create=True
        ) as startfile:
            with self.assertRaises(OSError):
                streamlit_app._open_local_artifact(_FakeNetworkPath())  # type: ignore[arg-type]

        startfile.assert_not_called()


class StreamlitLinkButtonCompatTests(unittest.TestCase):
    """link_button에 key 인자가 없는 Streamlit에서도 안내 화면이 깨지지 않아야 한다."""

    def test_key_is_passed_through_when_the_installed_streamlit_supports_it(self) -> None:
        calls: list[dict[str, object]] = []

        class _St:
            @staticmethod
            def link_button(label, url, key=None, width="content"):
                calls.append({"label": label, "url": url, "key": key, "width": width})

        with patch.object(streamlit_app, "st", _St):
            streamlit_app._link_button("안내", "https://example.invalid", key="k1", width="stretch")

        self.assertEqual(
            [{"label": "안내", "url": "https://example.invalid", "key": "k1", "width": "stretch"}],
            calls,
        )

    def test_keyed_container_wraps_the_button_when_key_is_unsupported(self) -> None:
        events: list[tuple[object, ...]] = []

        class _Container:
            def __init__(self, key):
                self.key = key

            def __enter__(self):
                events.append(("enter", self.key))
                return self

            def __exit__(self, *exc_info):
                events.append(("exit", self.key))
                return False

        class _St:
            @staticmethod
            def link_button(label, url):
                events.append(("link", label, url))

            @staticmethod
            def container(key=None):
                return _Container(key)

        with patch.object(streamlit_app, "st", _St):
            streamlit_app._link_button("안내", "https://example.invalid", key="k2")

        self.assertEqual(
            [("enter", "k2"), ("link", "안내", "https://example.invalid"), ("exit", "k2")],
            events,
        )


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

    def test_focused_comparison_includes_ai_review_without_an_extra_results_stage(self) -> None:
        with patch.object(streamlit_app, "_beginner_focus_review", return_value=True):
            self.assertFalse(streamlit_app._results_step_is_used(self._ctx({"request_enabled": True})))

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
            "if not _results_step_is_used(ctx):\n        _render_quality_banner(ctx.get(\"quality_report\"), at_review=True)",
            source,
        )

    def test_beginner_results_advisory_does_not_block_approval_page(self) -> None:
        source = Path(streamlit_app.__file__).read_text(encoding="utf-8")

        advisory = (
            "if beginner_mode_active and not _beginner_focus_review() and _results_step_is_used(ctx) "
            "and not beginner_current_results_confirmed:"
        )
        self.assertIn(advisory, source)
        advisory_start = source.index(advisory)
        advisory_end = source.index("if not chunks:", advisory_start)
        self.assertNotIn("return", source[advisory_start:advisory_end])
        self.assertIn("이 권고는 진행을 막지 않으며", source[advisory_start:advisory_end])


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


class PendingUploadCacheTests(unittest.TestCase):
    class CountingUpload(io.BytesIO):
        def __init__(self, payload: bytes, *, name: str, file_id: str = "upload-1") -> None:
            super().__init__(payload)
            self.name = name
            self.file_id = file_id
            self.size = len(payload)
            self.read_calls = 0

        def read(self, size: int = -1) -> bytes:
            self.read_calls += 1
            return super().read(size)

    def test_same_streamlit_upload_reuses_pending_file_without_rereading_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            upload = self.CountingUpload(b"large regulation bytes", name="regulation.hwp")
            cache: dict[str, object] = {}
            with patch.object(streamlit_app, "settings", settings):
                first = streamlit_app._persist_pending_upload(
                    "profile-a",
                    upload,
                    cache=cache,
                )
                self.assertGreater(upload.read_calls, 0)
                upload.read_calls = 0
                second = streamlit_app._persist_pending_upload(
                    "profile-a",
                    upload,
                    cache=cache,
                )

            self.assertEqual(first, second)
            self.assertEqual(0, upload.read_calls)

    def test_cached_path_outside_profile_pending_directory_is_never_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data")
            upload = self.CountingUpload(b"regulation", name="regulation.hwp")
            outside = root / ("0" * 64 + "__regulation.hwp")
            outside.write_bytes(b"regulation")
            identity = streamlit_app._pending_upload_cache_identity("profile-a", upload)
            self.assertIsNotNone(identity)
            cache: dict[str, object] = {
                str(identity): {
                    "path": str(outside),
                    "size": outside.stat().st_size,
                    "mtime_ns": outside.stat().st_mtime_ns,
                }
            }

            with patch.object(streamlit_app, "settings", settings):
                result = streamlit_app._persist_pending_upload(
                    "profile-a",
                    upload,
                    cache=cache,
                )
                expected_directory = streamlit_app._pending_upload_dir("profile-a").resolve()

            self.assertNotEqual(outside, result)
            self.assertEqual(expected_directory, result.resolve().parent)
            self.assertGreater(upload.read_calls, 0)

    def test_upload_without_stable_file_id_uses_streaming_fallback_each_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            upload = self.CountingUpload(
                b"regulation",
                name="regulation.hwp",
                file_id="",
            )
            cache: dict[str, object] = {}
            with patch.object(streamlit_app, "settings", settings):
                streamlit_app._persist_pending_upload("profile-a", upload, cache=cache)
                upload.read_calls = 0
                streamlit_app._persist_pending_upload("profile-a", upload, cache=cache)

            self.assertGreater(upload.read_calls, 0)
            self.assertEqual({}, cache)

    def test_deleted_cached_pending_file_is_recreated_from_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            upload = self.CountingUpload(b"regulation", name="regulation.hwp")
            cache: dict[str, object] = {}
            with patch.object(streamlit_app, "settings", settings):
                first = streamlit_app._persist_pending_upload(
                    "profile-a",
                    upload,
                    cache=cache,
                )
                first.unlink()
                upload.read_calls = 0
                second = streamlit_app._persist_pending_upload(
                    "profile-a",
                    upload,
                    cache=cache,
                )

            self.assertEqual(first, second)
            self.assertTrue(second.is_file())
            self.assertGreater(upload.read_calls, 0)
            self.assertEqual(b"regulation", second.read_bytes())

    def test_reusable_preprocessing_lookup_forwards_tenant_and_exact_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pending_path = Path(tmp) / "pending.hwp"
            pending_path.write_bytes(b"same source")
            document = SimpleNamespace(document_id="doc-a", tenant_id="tenant-a")
            reusable_run = SimpleNamespace(run_id="run-a")
            repository = SimpleNamespace(
                find_reusable_run=Mock(return_value=(document, reusable_run)),
                get_chunks=Mock(return_value=[]),
            )
            options = {
                "pipeline_version": "pipeline-v1",
                "kordoc_table_command_version": "4.12.0",
            }

            result = streamlit_app._find_reusable_preprocessing_run(
                repository,
                pending_path,
                processing_options=options,
                upload_metadata={
                    "profile_id": "profile-a",
                    "source_system": "LOCAL_UPLOAD",
                    "document_name": "pending",
                    "regulation_status": "draft",
                },
                tenant_id="tenant-a",
            )

            self.assertEqual((document, reusable_run), result)
            forwarded = repository.find_reusable_run.call_args.kwargs
            self.assertEqual("tenant-a", forwarded["tenant_id"])
            self.assertIs(options, forwarded["options"])
            self.assertEqual("4.12.0", forwarded["options"]["kordoc_table_command_version"])

    def test_reusable_preprocessing_is_disabled_for_explicit_revision_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pending_path = Path(tmp) / "pending.hwp"
            pending_path.write_bytes(b"same source")
            repository = SimpleNamespace(find_reusable_run=Mock())

            result = streamlit_app._find_reusable_preprocessing_run(
                repository,
                pending_path,
                processing_options={"pipeline_version": "pipeline-v1"},
                upload_metadata={
                    "profile_id": "profile-a",
                    "regulation_id": "manual-regulation-id",
                },
                tenant_id="tenant-a",
            )

            self.assertIsNone(result)
            repository.find_reusable_run.assert_not_called()


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


class ApprovalReviewReasonLabelTests(unittest.TestCase):
    """초보자 검수 화면에 내부 코드(table_review_flags:... 등)를 그대로 보여주지 않는다."""

    def _items(self, reasons: list[str]) -> list[dict]:
        chunk = SimpleNamespace(chunk_id="c1", warnings=[], metadata={"hierarchy_path": "제1조"}, chunk_type="article")
        return streamlit_app._approval_ai_review_items(chunk, reasons, None)

    def test_prefixed_table_flags_get_plain_wording(self) -> None:
        items = self._items(
            [
                "table_review_flags:possible_truncated_cell",
                "table_review_flags:row_review_required",
                "table_review_flags:wrapped_cell_merge",
            ]
        )

        self.assertEqual(["표 칸 잘림 가능성", "표 줄 확인", "표 줄 합침 확인"], [item["title"] for item in items])
        for item in items:
            self.assertNotIn("table_review_flags", str(item["suggestion"]))
            self.assertNotIn("_", str(item["suggestion"]))

    def test_unknown_flags_fall_back_to_their_family_without_the_code(self) -> None:
        reasons = [
            "table_review_flags:brand_new_flag",
            "parser_uncertainty_flags:some_flag",
            "warning:table_caption_split",
            "mystery_reason_code",
        ]
        items = self._items(reasons)

        self.assertEqual(
            ["표 내용 확인", "자동 변환이 불확실한 부분", "변환 경고 확인", "검수 항목 확인"],
            [item["title"] for item in items],
        )
        for item, reason in zip(items, reasons):
            self.assertNotIn(reason, str(item["suggestion"]))
            self.assertEqual(reason, item["reason"])

    def test_existing_reason_labels_are_unchanged(self) -> None:
        item = self._items(["table_review_required"])[0]

        self.assertEqual(streamlit_app.AI_REVIEW_REASON_LABELS["table_review_required"][0], item["title"])

    def test_every_reason_family_from_review_workflow_has_plain_wording(self) -> None:
        from app.services import review_workflow_service as workflow

        # Bool keys and the page-location key are emitted bare; list keys and
        # parser-uncertainty details are emitted as "<family>:<value>".
        codes = [
            *workflow.REVIEW_ATTENTION_BOOL_METADATA_KEYS,
            "source_page_unavailable_reason",
            *(f"{family}:x" for family in workflow.REVIEW_ATTENTION_LIST_METADATA_KEYS),
            "parser_uncertainty_risk_level:high",
            "parser_uncertainty_flags:x",
            "parser_uncertainty_recommendation:x",
            "warning:x",
        ]
        for code in codes:
            title, _severity, suggestion = streamlit_app._approval_review_reason_label(code)
            self.assertNotEqual(streamlit_app.AI_REVIEW_DEFAULT_LABEL[0], title, code)
            self.assertNotIn(code.partition(":")[0], suggestion)


    def test_parser_uncertainty_severity_follows_the_risk_value(self) -> None:
        expected = {"low": "낮음", "medium": "중간", "high": "높음", "critical": "높음"}
        for risk, severity in expected.items():
            title, actual, _suggestion = streamlit_app._approval_review_reason_label(
                f"parser_uncertainty_risk_level:{risk}"
            )
            self.assertEqual(severity, actual, risk)
            self.assertEqual("자동 변환이 불확실한 부분", title)
        # An unknown or empty value is shown at the minimum level these reasons are raised at.
        for risk in ("", "bogus"):
            self.assertEqual(
                "중간", streamlit_app._approval_review_reason_label(f"parser_uncertainty_risk_level:{risk}")[1]
            )

    def test_flags_and_recommendation_share_the_chunk_risk_severity(self) -> None:
        detail = ["parser_uncertainty_flags:some_flag", "parser_uncertainty_recommendation:manual_review"]
        for risk, severity in (("medium", "중간"), ("high", "높음"), ("low", "낮음")):
            items = self._items([f"parser_uncertainty_risk_level:{risk}", *detail])
            self.assertEqual([severity] * 3, [item["severity"] for item in items], risk)
        # No risk reason in the list: fall back to the risk stored in the chunk metadata.
        chunk = SimpleNamespace(
            chunk_id="c1", warnings=[], metadata={"parser_uncertainty": {"risk_level": "high"}}, chunk_type="article"
        )
        items = streamlit_app._approval_ai_review_items(chunk, detail, None)
        self.assertEqual(["높음", "높음"], [item["severity"] for item in items])
        # Nothing known at all: "중간", never an unconditional "높음".
        self.assertEqual(["중간", "중간"], [item["severity"] for item in self._items(detail)])

    def test_items_sharing_a_title_are_numbered_and_explained(self) -> None:
        items = self._items(
            [
                "warning:table_caption_split",
                "table_review_flags:possible_truncated_cell",
                "warning:ocr_low_confidence",
            ]
        )

        self.assertEqual(
            ["변환 경고 확인 (1/2)", "표 칸 잘림 가능성", "변환 경고 확인 (2/2)"],
            [item["title"] for item in items],
        )
        # A unique title is left alone and needs no extra hint.
        self.assertEqual("", items[1]["hint"])
        self.assertEqual("표 칸 잘림 가능성", items[1]["base_title"])
        # The two warnings differ by topic, in plain words and without the raw code.
        self.assertIn("표·그림 제목", items[0]["hint"])
        self.assertIn("스캔 글자 인식", items[2]["hint"])
        for item in (items[0], items[2]):
            self.assertNotIn(item["reason"], item["hint"])

    def test_same_source_duplicates_point_to_the_detail_and_stay_distinguishable(self) -> None:
        reasons = [
            "parser_uncertainty_risk_level:medium",
            "parser_uncertainty_flags:flag_a",
            "parser_uncertainty_flags:flag_b",
            "parser_uncertainty_recommendation:manual_review",
            "warning:first_table_note",
            "warning:second_table_note",
        ]
        items = self._items(reasons)

        titles = [str(item["title"]) for item in items]
        self.assertEqual(len(titles), len(set(titles)))
        self.assertEqual(
            [f"자동 변환이 불확실한 부분 ({n}/4)" for n in range(1, 5)]
            + [f"변환 경고 확인 ({n}/2)" for n in (1, 2)],
            titles,
        )
        hints = [str(item["hint"]) for item in items]
        self.assertTrue(all(hints))
        # Items whose source wording is identical are told apart through the detail expander.
        self.assertIn("자세히 보기", hints[1])
        self.assertIn("자세히 보기", hints[2])
        self.assertNotIn("자세히 보기", hints[0])
        self.assertIn("자세히 보기", hints[4])
        self.assertIn("자세히 보기", hints[5])

    def test_numbering_never_changes_item_ids_reasons_or_the_completion_gate(self) -> None:
        reasons = ["warning:table_caption_split", "warning:ocr_low_confidence", "table_review_required"]
        items = self._items(reasons)

        self.assertEqual([f"c1:{reason}:{index}" for index, reason in enumerate(reasons, start=1)],
                         [item["item_id"] for item in items])
        self.assertEqual(reasons, [item["reason"] for item in items])
        item_ids = [str(item["item_id"]) for item in items]
        decisions = {item_ids[0]: "skip", item_ids[1]: "skip"}
        state = approval_review_completion_state(item_ids, decisions, human_confirmed=True)
        self.assertFalse(state["approve_enabled"])
        self.assertEqual([item_ids[2]], state["undecided_item_ids"])
        decisions[item_ids[2]] = "skip"
        self.assertTrue(
            approval_review_completion_state(item_ids, decisions, human_confirmed=True)["approve_enabled"]
        )


class ApprovalReviewGuideBannerTests(unittest.TestCase):
    """초보자 안내 배너는 판단하지 않은 첫 항목 위에 한 번만 나온다."""

    REASONS = [
        "table_review_flags:possible_truncated_cell",
        "table_review_flags:row_review_required",
        "table_review_required",
    ]

    def _app(self):
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_string(
            "import streamlit as st\n"
            "from frontend.streamlit_app import _render_approval_chunk_confirmation_controls\n"
            "_render_approval_chunk_confirmation_controls(document_id='doc', chunk=st.session_state['chunk'],"
            " agent_review_summary=None, review_reasons=st.session_state['reasons'])\n"
        )
        app.session_state["chunk"] = Chunk(
            chunk_id="c1", document_id="doc", chunk_type="table", text="구분 | 내용", metadata={}
        )
        app.session_state["reasons"] = list(self.REASONS)
        app.session_state[streamlit_app.BEGINNER_GUIDE_ENABLED_KEY] = True
        app.session_state[streamlit_app.BEGINNER_GUIDE_STEP_KEY] = 3
        return app

    @staticmethod
    def _guide_markers(app) -> list[str]:
        return [
            str(item.value)
            for item in app.markdown
            if "data-rr-tour=" in str(item.value) and "주의할 점을 보고 버튼을 골라 주세요" in str(item.value)
        ]

    def test_one_banner_moves_to_the_next_undecided_item(self) -> None:
        app = self._app()
        app.run(timeout=60)
        self.assertFalse(app.exception)
        captions = " ".join(str(item.value) for item in app.caption)
        self.assertNotIn("table_review_flags", captions)
        markers = self._guide_markers(app)
        self.assertEqual(1, len(markers))
        self.assertIn("possible_truncated_cell", markers[0])

        skip_keys = [item.key for item in app.button if item.label == "이 문제는 없어요"]
        self.assertEqual(3, len(skip_keys))
        app.button(key=skip_keys[0]).click().run(timeout=60)
        self.assertFalse(app.exception)
        markers = self._guide_markers(app)
        self.assertEqual(1, len(markers))
        self.assertIn("row_review_required", markers[0])

        for key in skip_keys[1:]:
            app.button(key=key).click().run(timeout=60)
            self.assertFalse(app.exception)
        self.assertEqual([], self._guide_markers(app))


    DUPLICATE_REASONS = [
        "warning:table_caption_split",
        "warning:ocr_low_confidence",
        "parser_uncertainty_risk_level:medium",
    ]

    def _duplicate_app(self, *, beginner: bool):
        app = self._app()
        app.session_state["reasons"] = list(self.DUPLICATE_REASONS)
        app.session_state[streamlit_app.BEGINNER_GUIDE_ENABLED_KEY] = beginner
        return app

    def test_beginner_keeps_plain_wording_and_gets_the_raw_code_in_a_collapsed_expander(self) -> None:
        app = self._duplicate_app(beginner=True)
        app.run(timeout=60)
        self.assertFalse(app.exception)

        detail = [expander for expander in app.expander if expander.label == "자세히 보기 · 기술 정보"]
        self.assertEqual(3, len(detail))
        # Every item gets its own expander holding exactly its raw code.
        self.assertEqual(
            self.DUPLICATE_REASONS,
            [str(expander.code[0].value) for expander in detail],
        )
        # Captions stay plain: numbered titles, a distinguishing hint, no raw code (it lives in st.code).
        lines = " ".join(str(item.value) for item in app.caption)
        self.assertIn("변환 경고 확인 (1/2)", lines)
        self.assertIn("변환 경고 확인 (2/2)", lines)
        self.assertIn("구분: 변환 중 나온 경고 한 건이에요 (표·그림 제목 관련).", lines)
        self.assertIn("구분: 변환 중 나온 경고 한 건이에요 (스캔 글자 인식 관련).", lines)
        self.assertNotIn("(코드:", lines)
        for reason in self.DUPLICATE_REASONS:
            self.assertNotIn(reason, lines)
        # Collapsed by default: opening it is optional and decides nothing.
        self.assertTrue(all(not expander.proto.expanded for expander in detail))

    def test_operator_mode_keeps_the_inline_code_and_has_no_detail_expander(self) -> None:
        app = self._duplicate_app(beginner=False)
        app.run(timeout=60)
        self.assertFalse(app.exception)

        lines = [str(item.value) for item in app.caption]
        for reason in self.DUPLICATE_REASONS:
            self.assertTrue(any(f"(코드: {reason})" in line for line in lines), reason)
        self.assertEqual([], [e for e in app.expander if e.label == "자세히 보기 · 기술 정보"])

    def test_decision_keys_and_item_ids_are_unchanged_in_both_modes(self) -> None:
        item_ids = [f"c1:{reason}:{index}" for index, reason in enumerate(self.DUPLICATE_REASONS, start=1)]
        for beginner in (True, False):
            app = self._duplicate_app(beginner=beginner)
            app.run(timeout=60)
            self.assertFalse(app.exception)
            skip_label = "이 문제는 없어요" if beginner else "해당 없음"
            skip_keys = [item.key for item in app.button if item.label == skip_label]
            self.assertEqual([f"ai-skip-{item_id}" for item_id in item_ids], skip_keys, beginner)

            decisions_key = streamlit_app._approval_chunk_state_key("doc", "c1", "ai_decisions")
            for number, key in enumerate(skip_keys, start=1):
                app.button(key=key).click().run(timeout=60)
                self.assertFalse(app.exception)
                recorded = dict(app.session_state[decisions_key])
                self.assertEqual(item_ids[:number], list(recorded), beginner)
                gate = approval_review_completion_state(item_ids, recorded, human_confirmed=True)
                self.assertEqual(number == len(item_ids), gate["approve_enabled"], (beginner, number))


if __name__ == "__main__":
    unittest.main()
