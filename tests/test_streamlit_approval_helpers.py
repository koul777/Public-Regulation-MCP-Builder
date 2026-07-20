from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.core.config import Settings
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository
from frontend import streamlit_app


class StreamlitApprovalHelperTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
