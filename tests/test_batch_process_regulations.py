from __future__ import annotations

import tempfile
import unittest
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import Settings
from app.core.institution_profiles import load_institution_profile_registry
from app.core.pipeline import processing_options_payload
from app.parsers.base import OCRRequiredError
from app.schemas.chunk import Chunk, ChunkOptions
from app.schemas.document import Document
from app.schemas.quality import QualityCheck, QualityReport
from app.schemas.run import ProcessingRun
from app.schemas.structure import StructureNode
from app.storage.file_store import sha256_bytes
from app.storage.repository import JsonRepository
from scripts.batch_process_regulations import (
    agent_review_batch_budget_is_fatal,
    apply_agent_review_batch_budget,
    apply_institution_profile_defaults,
    build_batch_summary,
    build_failure_row,
    build_success_row,
    collect_input_files,
    load_manifest_entries,
    process_file,
    to_markdown,
    write_kordoc_sidecar_report,
)


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


class BatchProcessRegulationsTests(unittest.TestCase):
    def test_apply_institution_profile_defaults_fills_missing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "institution_profiles.json"
            path.write_text(
                json.dumps(
                    {
                        "profiles": {
                            "public_portal-etc-law": {
                                "institution_name": "PUBLIC_PORTAL public institution disclosure",
                                "source_system": "PUBLIC_PORTAL",
                                "source_url": "https://example.org/regulations/etc/etcLawList.do",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry = load_institution_profile_registry(path)

            entries = apply_institution_profile_defaults(
                [
                    {
                        "path": Path(tmp) / "sample.hwp",
                        "profile_id": "public_portal-etc-law",
                        "institution_name": None,
                        "source_system": None,
                        "source_url": "https://example.test/override",
                    }
                ],
                registry,
                strict=True,
            )

        self.assertEqual(entries[0]["institution_name"], "PUBLIC_PORTAL public institution disclosure")
        self.assertEqual(entries[0]["source_system"], "PUBLIC_PORTAL")
        self.assertEqual(entries[0]["source_url"], "https://example.test/override")

    def test_apply_institution_profile_defaults_strict_rejects_unknown_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "institution_profiles.json"
            path.write_text(json.dumps({"profiles": {"known": {}}}), encoding="utf-8")
            registry = load_institution_profile_registry(path)

            with self.assertRaisesRegex(ValueError, "Unknown institution profile_id"):
                apply_institution_profile_defaults(
                    [{"path": Path(tmp) / "sample.hwp", "profile_id": "typo"}],
                    registry,
                    strict=True,
                )

    def test_collect_input_files_recurses_and_deduplicates_supported_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "nested"
            nested.mkdir()
            pdf = root / "a.pdf"
            docx = nested / "b.docx"
            txt = nested / "ignore.txt"
            pdf.write_bytes(b"pdf")
            docx.write_bytes(b"docx")
            txt.write_text("ignore", encoding="utf-8")

            files = collect_input_files([root, pdf], recursive=True)

            self.assertEqual([path.name for path in files], ["a.pdf", "b.docx"])

    def test_collect_input_files_rejects_explicit_unsupported_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.txt"
            path.write_text("bad", encoding="utf-8")

            with self.assertRaises(ValueError):
                collect_input_files([path])

    def test_write_kordoc_sidecar_report_is_separate_diagnostic_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "sample.hwp"
            input_path.write_bytes(b"fake")
            captured: dict[str, object] = {}

            def fake_builder(paths, *, kordoc_command, data_dir, timeout_seconds):
                captured["paths"] = [path.name for path in paths]
                captured["kordoc_command"] = kordoc_command
                captured["data_dir"] = data_dir
                captured["timeout_seconds"] = timeout_seconds
                return {
                    "contract": "kordoc_sidecar_crosscheck_v1",
                    "scope": "diagnostic_only_not_indexing_input",
                    "counts": {
                        "documents": 1,
                        "review_required": 0,
                        "local_parsed": 1,
                        "kordoc_parsed": 1,
                    },
                    "rows": [],
                }

            paths = write_kordoc_sidecar_report(
                [{"path": input_path}, {"path": input_path}],
                root / "reports",
                kordoc_command="fake-kordoc",
                data_dir=root / "data",
                timeout_seconds=7,
                builder=fake_builder,
            )

            self.assertEqual(captured["paths"], ["sample.hwp"])
            self.assertEqual(captured["kordoc_command"], "fake-kordoc")
            self.assertEqual(captured["data_dir"], root / "data")
            self.assertEqual(captured["timeout_seconds"], 7)
            self.assertTrue(Path(paths["kordoc_sidecar_json"]).exists())
            markdown = Path(paths["kordoc_sidecar_markdown"]).read_text(encoding="utf-8")
            self.assertIn("diagnostic_only_not_indexing_input", markdown)
            self.assertIn("Do not index Kordoc output directly.", markdown)

    def test_failure_row_preserves_retry_provenance(self) -> None:
        row = build_failure_row(
            Path("sample.hwp"),
            error="parse failed",
            elapsed_seconds=0.1,
            institution_name="Example Institution",
            source_system="PUBLIC_PORTAL",
            source_url="https://example.test/detail",
            source_record_id="board-1",
            source_file_id="file-1",
            source_disclosure_date="2026.01.01",
            source_posted_date="2026.01.02",
            profile_id="public_portal-etc-law",
            apba_id="C0147",
        )

        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["source_record_id"], "board-1")
        self.assertEqual(row["apba_id"], "C0147")
        self.assertEqual(row["source_file_id"], "file-1")
        self.assertEqual(row["source_disclosure_date"], "2026.01.01")
        self.assertEqual(row["source_posted_date"], "2026.01.02")

    def test_failure_row_classifies_ocr_required_errors(self) -> None:
        row = build_failure_row(
            Path("scan.pdf"),
            error=OCRRequiredError("No text blocks were extracted from the PDF file. OCR may be required.", page_count=7),
            elapsed_seconds=0.1,
            source_system="PUBLIC_PORTAL",
        )

        self.assertEqual(row["failure_category"], "ocr_required")
        self.assertTrue(row["ocr_required"])
        self.assertEqual(row["ocr_page_count"], 7)
        self.assertFalse(row["retry_recommended"])
        self.assertEqual(row["failure_next_action"], "run_ocr_then_reprocess")

    def test_success_row_flattens_quality_metrics(self) -> None:
        document = Document(
            document_id="doc_1",
            filename="sample.pdf",
            file_type="pdf",
            file_hash="abc",
            institution_name="Example Institution",
            source_system="PUBLIC_PORTAL",
            source_url="https://example.test/regulations",
            profile_id="default-public-institution",
        )
        report = QualityReport(
            document_id=document.document_id,
            passed=True,
            score=97.5,
            node_count=10,
            chunk_count=5,
            issue_count=1,
            error_count=0,
            warning_count=1,
            failed_error_check_count=0,
            failed_warning_check_count=1,
            duplicate_chunk_id_count=0,
            empty_chunk_count=0,
            missing_page_count=0,
            missing_required_metadata_count=0,
            table_metrics={
                "table_like_chunks": 2,
                "table_citation_ready_chunks": 1,
                "chunks_with_table_cell_rows": 1,
                "table_like_without_cell_rows": 1,
                "table_cell_row_count": 3,
                "probable_table_false_positive_chunks": 0,
                "probable_table_extraction_failed_chunks": 1,
            },
            structure_metrics={
                "duplicate_regulation_node_count": 2,
                "zero_chunk_regulation_node_count": 1,
                "chunks_missing_regulation_no": 0,
                "article_chunks_missing_regulation_no": 0,
                "detected_reg_no_without_chunk_metadata_count": 0,
            },
            coverage_metrics={"chunk_to_source_char_ratio": 0.91},
            checks=[
                QualityCheck(
                    name="table_false_positive_attention",
                    severity="info",
                    passed=False,
                    value=8,
                    threshold="< 7 and <= 15% of chunks",
                    message="High table false-positive counts should become regression fixtures.",
                )
            ],
            recommendations=["High table false-positive counts should become regression fixtures."],
        )
        run = ProcessingRun(
            run_id="run_1",
            document_id=document.document_id,
            job_id="job_1",
            status="completed",
            started_at=datetime.now(timezone.utc),
            elapsed_seconds=1.25,
            stats={
                "agent_review": {
                    "status": "planned",
                    "skip_reason": None,
                    "candidate_count": 3,
                    "selected_count": 2,
                    "estimated_input_tokens": 1200,
                    "budget_exhausted": False,
                }
            },
            artifacts={
                "agent_review_plan.json": "data/exports/doc_1.agent_review_plan.json",
                "quality.json": "data/exports/doc_1.quality.json",
                "quality.md": "data/exports/doc_1.quality.md",
                "tables.csv": "data/exports/doc_1.tables.csv",
                "tables.jsonl": "data/exports/doc_1.tables.jsonl",
            },
        )

        row = build_success_row(Path("sample.pdf"), document, run.job_id, report, run)

        self.assertEqual(row["institution_name"], "Example Institution")
        self.assertEqual(row["source_system"], "PUBLIC_PORTAL")
        self.assertTrue(row["quality_passed"])
        self.assertEqual(row["quality_score"], 97.5)
        self.assertEqual(row["table_citation_ready_chunks"], 1)
        self.assertEqual(row["table_like_without_cell_rows"], 1)
        self.assertEqual(row["duplicate_regulation_node_count"], 2)
        self.assertEqual(row["agent_review_status"], "planned")
        self.assertEqual(row["agent_review_selected_count"], 2)
        self.assertEqual(row["agent_review_estimated_input_tokens"], 1200)
        self.assertEqual(row["agent_review_plan_json"], "data/exports/doc_1.agent_review_plan.json")
        self.assertEqual(row["quality_json"], "data/exports/doc_1.quality.json")
        self.assertEqual(row["failed_info_check_count"], 1)
        self.assertEqual(row["recommendation_count"], 1)
        self.assertEqual(row["top_recommendation"], "High table false-positive counts should become regression fixtures.")

    def test_batch_summary_and_markdown_include_failure_counts(self) -> None:
        success = {
            "filename": "ok.pdf",
            "document_id": "doc_ok",
            "status": "completed",
            "apba_id": "C0147",
            "quality_passed": True,
            "quality_score": 100.0,
            "warning_count": 0,
            "chunk_count": 3,
            "table_like_chunks": 2,
            "table_citation_ready_chunks": 1,
            "table_like_without_cell_rows": 0,
            "chunks_missing_regulation_no": 0,
            "agent_review_status": "skipped",
            "agent_review_selected_count": 0,
            "agent_review_estimated_input_tokens": 0,
            "agent_review_budget_exhausted": False,
            "failed_info_check_count": 1,
            "recommendation_count": 1,
            "elapsed_seconds": 0.5,
        }
        failure = build_failure_row(Path("bad.pdf"), error="parse failed", elapsed_seconds=0.2)

        summary = build_batch_summary([success, failure])
        markdown = to_markdown(summary)

        self.assertEqual(summary["input_count"], 2)
        self.assertEqual(summary["completed_count"], 1)
        self.assertEqual(summary["failed_count"], 1)
        self.assertEqual(summary["retry_recommended_failed_count"], 1)
        self.assertEqual(summary["ocr_required_count"], 0)
        self.assertEqual(summary["quality_passed_count"], 1)
        self.assertEqual(summary["failed_info_check_total"], 1)
        self.assertEqual(summary["recommendation_total"], 1)
        self.assertEqual(summary["table_like_total"], 2)
        self.assertEqual(summary["table_citation_ready_total"], 1)
        self.assertEqual(summary["table_like_without_cell_rows_total"], 0)
        self.assertEqual(summary["apba_id_counts"], {"C0147": 1, "missing": 1})
        self.assertEqual(summary["agent_review_selected_total"], 0)
        self.assertEqual(summary["agent_review_estimated_input_tokens_total"], 0)
        self.assertIn("- Citation-ready table chunks: 1", markdown)
        self.assertIn("| ok.pdf | doc_ok | completed | 100.0 |", markdown)
        self.assertIn("| skipped | 0 | 0 |", markdown)
        self.assertIn("| bad.pdf |  | failed |", markdown)

    def test_batch_summary_counts_ocr_required_failures(self) -> None:
        failure = build_failure_row(
            Path("scan.pdf"),
            error=OCRRequiredError("No text blocks were extracted from the PDF file. OCR may be required.", page_count=4),
            elapsed_seconds=0.2,
        )

        summary = build_batch_summary([failure])
        markdown = to_markdown(summary)

        self.assertEqual(summary["failed_count"], 1)
        self.assertEqual(summary["failure_category_counts"], {"ocr_required": 1})
        self.assertEqual(summary["ocr_required_count"], 1)
        self.assertEqual(summary["ocr_required_page_count"], 4)
        self.assertEqual(summary["retry_recommended_failed_count"], 0)
        self.assertIn("- OCR required: 1", markdown)
        self.assertIn("- OCR required pages: 4", markdown)

    def test_batch_summary_partitions_processed_and_skipped_successes(self) -> None:
        processed = {
            "filename": "processed.pdf",
            "document_id": "doc_processed",
            "status": "completed",
            "quality_passed": True,
            "quality_score": 100.0,
            "agent_review_selected_count": 2,
            "agent_review_estimated_input_tokens": 1000,
            "agent_review_budget_exhausted": False,
            "failed_info_check_count": 1,
            "recommendation_count": 2,
        }
        skipped = {
            "filename": "skipped.pdf",
            "document_id": "doc_skipped",
            "status": "skipped_unchanged",
            "quality_passed": True,
            "quality_score": 98.0,
            "agent_review_selected_count": "3",
            "agent_review_estimated_input_tokens": "1500",
            "agent_review_budget_exhausted": True,
            "historical_agent_review_selected_count": "3",
            "historical_agent_review_estimated_input_tokens": "1500",
            "historical_agent_review_estimated_output_tokens": "600",
            "historical_agent_review_estimated_total_tokens": "2625",
            "historical_agent_review_api_call_count": "0",
            "failed_info_check_count": "3",
            "recommendation_count": "4",
        }
        failure = build_failure_row(Path("bad.pdf"), error="parse failed", elapsed_seconds=0.2)

        summary = build_batch_summary([processed, skipped, failure])
        markdown = to_markdown(summary)

        self.assertEqual(summary["completed_count"], 1)
        self.assertEqual(summary["skipped_unchanged_count"], 1)
        self.assertEqual(summary["successful_count"], 2)
        self.assertEqual(summary["failed_count"], 1)
        self.assertEqual(summary["quality_passed_count"], 2)
        self.assertEqual(summary["average_quality_score"], 99.0)
        self.assertEqual(summary["failed_info_check_total"], 4)
        self.assertEqual(summary["recommendation_total"], 6)
        self.assertEqual(summary["agent_review_selected_total"], 2)
        self.assertEqual(summary["agent_review_estimated_input_tokens_total"], 1000)
        self.assertEqual(summary["agent_review_cost_missing_count"], 1)
        self.assertEqual(summary["agent_review_budget_exhausted_count"], 0)
        self.assertEqual(summary["historical_agent_review_selected_total"], 3)
        self.assertEqual(summary["historical_agent_review_estimated_input_tokens_total"], 1500)
        self.assertEqual(summary["historical_agent_review_estimated_output_tokens_total"], 600)
        self.assertEqual(summary["historical_agent_review_estimated_total_tokens_total"], 2625)
        self.assertEqual(summary["historical_agent_review_api_call_count_total"], 0)
        self.assertIn("- Successful total: 2", markdown)
        self.assertIn("- Failed info checks: 4", markdown)
        self.assertIn("- AI review estimated input tokens: 1000", markdown)
        self.assertIn("- AI review cost missing documents: 1", markdown)
        self.assertIn("- Historical AI review estimated input tokens on reused runs: 1500", markdown)

    def test_agent_review_batch_budget_is_fail_closed_when_review_chunks_are_selected(self) -> None:
        summary = build_batch_summary(
            [
                {
                    "filename": "review.pdf",
                    "document_id": "doc_review",
                    "status": "completed",
                    "quality_passed": True,
                    "quality_score": 98.0,
                    "agent_review_selected_count": 2,
                    "agent_review_estimated_input_tokens": 1000,
                    "agent_review_estimated_output_tokens": 400,
                    "agent_review_estimated_total_tokens": 2000,
                    "agent_review_estimated_cost": "0.0062",
                    "agent_review_budget_exhausted": False,
                    "failed_info_check_count": 0,
                    "recommendation_count": 0,
                }
            ]
        )

        apply_agent_review_batch_budget(summary, Settings(data_dir=Path("data")))

        self.assertTrue(summary["agent_review_batch_budget_exceeded"])
        self.assertEqual(summary["agent_review_selected_document_total"], 1)
        self.assertEqual(summary["agent_review_estimated_input_tokens_total"], 1000)
        self.assertEqual(summary["agent_review_estimated_total_tokens_total"], 2000)
        self.assertIn("AGENT_REVIEW_MAX_DOCUMENTS_PER_BATCH", summary["agent_review_batch_budget_errors"][0])

    def test_agent_review_batch_budget_passes_with_explicit_caps(self) -> None:
        summary = build_batch_summary(
            [
                {
                    "filename": "review.pdf",
                    "document_id": "doc_review",
                    "status": "completed",
                    "quality_passed": True,
                    "quality_score": 98.0,
                    "agent_review_selected_count": 2,
                    "agent_review_estimated_input_tokens": 1000,
                    "agent_review_estimated_output_tokens": 400,
                    "agent_review_estimated_total_tokens": 2000,
                    "agent_review_estimated_cost": "0.0062",
                    "agent_review_budget_exhausted": False,
                    "failed_info_check_count": 0,
                    "recommendation_count": 0,
                }
            ]
        )
        settings = Settings(
            data_dir=Path("data"),
            agent_review_max_documents_per_batch=1,
            agent_review_max_input_tokens_per_batch=1002,
            agent_review_max_total_tokens_per_batch=2002,
            agent_review_prompt_input_tokens_per_batch=2,
            agent_review_input_price_per_1m_tokens=2.0,
            agent_review_output_price_per_1m_tokens=10.5,
            agent_review_max_cost_per_batch=0.01,
            agent_review_budget_currency="USD",
        )

        apply_agent_review_batch_budget(summary, settings)

        self.assertFalse(summary["agent_review_batch_budget_exceeded"])
        self.assertEqual(summary["agent_review_batch_budget_errors"], [])
        self.assertEqual(summary["agent_review_estimated_cost_total"], "0.0062")
        self.assertEqual(summary["agent_review_prompt_input_tokens_total"], 2)
        self.assertEqual(summary["agent_review_estimated_input_tokens_with_prompt_total"], 1002)
        self.assertEqual(summary["agent_review_estimated_total_tokens_with_prompt_total"], 2002)
        self.assertEqual(summary["agent_review_estimated_cost_with_prompt_total"], "0.0062")
        self.assertEqual(summary["agent_review_batch_max_cost"], "0.01")
        self.assertEqual(summary["agent_review_batch_budget_currency"], "USD")

    def test_agent_review_batch_budget_fails_without_prompt_token_reservation(self) -> None:
        summary = build_batch_summary(
            [
                {
                    "filename": "review.pdf",
                    "document_id": "doc_review",
                    "status": "completed",
                    "quality_passed": True,
                    "quality_score": 98.0,
                    "agent_review_selected_count": 1,
                    "agent_review_estimated_input_tokens": 100,
                    "agent_review_estimated_output_tokens": 50,
                    "agent_review_estimated_total_tokens": 188,
                    "agent_review_estimated_cost": "0.0003",
                    "agent_review_budget_exhausted": False,
                    "failed_info_check_count": 0,
                    "recommendation_count": 0,
                }
            ]
        )
        settings = Settings(
            data_dir=Path("data"),
            agent_review_max_documents_per_batch=1,
            agent_review_max_input_tokens_per_batch=100,
            agent_review_max_total_tokens_per_batch=188,
            agent_review_input_price_per_1m_tokens=1.0,
            agent_review_output_price_per_1m_tokens=4.0,
            agent_review_max_cost_per_batch=0.01,
        )

        apply_agent_review_batch_budget(summary, settings)

        self.assertTrue(summary["agent_review_batch_budget_exceeded"])
        self.assertTrue(
            any("AGENT_REVIEW_PROMPT_INPUT_TOKENS_PER_BATCH" in error for error in summary["agent_review_batch_budget_errors"])
        )

    def test_agent_review_batch_budget_counts_prompt_tokens_against_caps(self) -> None:
        summary = build_batch_summary(
            [
                {
                    "filename": "review.pdf",
                    "document_id": "doc_review",
                    "status": "completed",
                    "quality_passed": True,
                    "quality_score": 98.0,
                    "agent_review_selected_count": 1,
                    "agent_review_estimated_input_tokens": 100,
                    "agent_review_estimated_output_tokens": 0,
                    "agent_review_estimated_total_tokens": 125,
                    "agent_review_estimated_cost": "0.0001",
                    "agent_review_budget_exhausted": False,
                    "failed_info_check_count": 0,
                    "recommendation_count": 0,
                }
            ]
        )
        settings = Settings(
            data_dir=Path("data"),
            agent_review_max_documents_per_batch=1,
            agent_review_max_input_tokens_per_batch=100,
            agent_review_max_total_tokens_per_batch=125,
            agent_review_prompt_input_tokens_per_batch=1,
            agent_review_input_price_per_1m_tokens=1.0,
            agent_review_output_price_per_1m_tokens=4.0,
            agent_review_max_cost_per_batch=0.01,
        )

        apply_agent_review_batch_budget(summary, settings)

        self.assertTrue(summary["agent_review_batch_budget_exceeded"])
        self.assertEqual(summary["agent_review_estimated_input_tokens_with_prompt_total"], 101)
        self.assertTrue(any("including prompt" in error for error in summary["agent_review_batch_budget_errors"]))

    def test_agent_review_batch_budget_fails_when_estimated_cost_exceeds_cap(self) -> None:
        summary = build_batch_summary(
            [
                {
                    "filename": "review.pdf",
                    "document_id": "doc_review",
                    "status": "completed",
                    "quality_passed": True,
                    "quality_score": 98.0,
                    "agent_review_selected_count": 1,
                    "agent_review_estimated_input_tokens": 1000,
                    "agent_review_estimated_output_tokens": 500,
                    "agent_review_estimated_total_tokens": 1800,
                    "agent_review_estimated_cost": "0.025",
                    "agent_review_budget_exhausted": False,
                    "failed_info_check_count": 0,
                    "recommendation_count": 0,
                }
            ]
        )
        settings = Settings(
            data_dir=Path("data"),
            agent_review_max_documents_per_batch=1,
            agent_review_max_input_tokens_per_batch=1002,
            agent_review_max_total_tokens_per_batch=1802,
            agent_review_prompt_input_tokens_per_batch=2,
            agent_review_input_price_per_1m_tokens=2.0,
            agent_review_output_price_per_1m_tokens=10.5,
            agent_review_max_cost_per_batch=0.02,
        )

        apply_agent_review_batch_budget(summary, settings)

        self.assertTrue(summary["agent_review_batch_budget_exceeded"])
        self.assertTrue(any("exceeds batch cost cap 0.02" in error for error in summary["agent_review_batch_budget_errors"]))

    def test_agent_review_batch_budget_fails_when_selected_cost_is_missing(self) -> None:
        summary = build_batch_summary(
            [
                {
                    "filename": "review.pdf",
                    "document_id": "doc_review",
                    "status": "completed",
                    "quality_passed": True,
                    "quality_score": 98.0,
                    "agent_review_selected_count": 1,
                    "agent_review_estimated_input_tokens": 100,
                    "agent_review_estimated_output_tokens": 20,
                    "agent_review_estimated_total_tokens": 150,
                    "agent_review_estimated_cost": "",
                    "agent_review_budget_exhausted": False,
                    "failed_info_check_count": 0,
                    "recommendation_count": 0,
                }
            ]
        )
        settings = Settings(
            data_dir=Path("data"),
            agent_review_max_documents_per_batch=1,
            agent_review_max_input_tokens_per_batch=102,
            agent_review_max_total_tokens_per_batch=153,
            agent_review_prompt_input_tokens_per_batch=2,
            agent_review_input_price_per_1m_tokens=2.0,
            agent_review_output_price_per_1m_tokens=10.5,
            agent_review_max_cost_per_batch=0.02,
        )

        apply_agent_review_batch_budget(summary, settings)

        self.assertTrue(summary["agent_review_batch_budget_exceeded"])
        self.assertEqual(summary["agent_review_cost_missing_count"], 1)
        self.assertTrue(any("without a numeric estimated cost" in error for error in summary["agent_review_batch_budget_errors"]))

    def test_agent_review_batch_budget_not_fatal_when_api_configuration_is_missing(self) -> None:
        summary = build_batch_summary(
            [
                {
                    "filename": "review.pdf",
                    "document_id": "doc_review",
                    "status": "completed",
                    "quality_passed": True,
                    "quality_score": 98.0,
                    "agent_review_status": "api_configuration_needed",
                    "agent_review_skip_reason": "openai_api_key_missing",
                    "agent_review_selected_count": 1,
                    "agent_review_estimated_input_tokens": 100,
                    "agent_review_estimated_output_tokens": 20,
                    "agent_review_estimated_total_tokens": 150,
                    "agent_review_estimated_cost": "",
                    "agent_review_budget_exhausted": False,
                    "failed_info_check_count": 0,
                    "recommendation_count": 0,
                }
            ]
        )
        apply_agent_review_batch_budget(summary, Settings(data_dir=Path("data")))

        self.assertTrue(summary["agent_review_batch_budget_exceeded"])
        self.assertFalse(agent_review_batch_budget_is_fatal(summary))

    def test_agent_review_batch_budget_fatal_when_review_can_run(self) -> None:
        summary = build_batch_summary(
            [
                {
                    "filename": "review.pdf",
                    "document_id": "doc_review",
                    "status": "completed",
                    "quality_passed": True,
                    "quality_score": 98.0,
                    "agent_review_status": "planned",
                    "agent_review_selected_count": 1,
                    "agent_review_estimated_input_tokens": 100,
                    "agent_review_estimated_output_tokens": 20,
                    "agent_review_estimated_total_tokens": 150,
                    "agent_review_estimated_cost": "",
                    "agent_review_budget_exhausted": False,
                    "failed_info_check_count": 0,
                    "recommendation_count": 0,
                }
            ]
        )
        apply_agent_review_batch_budget(summary, Settings(data_dir=Path("data")))

        self.assertTrue(agent_review_batch_budget_is_fatal(summary))

    def test_load_manifest_entries_uses_public_portal_catalog_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "catalog.csv"
            downloaded = root / "data" / "public_portal" / "sample.hwp"
            downloaded.parent.mkdir(parents=True)
            downloaded.write_bytes(b"hwp")
            with manifest.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "downloaded_path",
                        "title",
                        "institution_name",
                        "detail_url",
                        "board_no",
                        "file_no",
                        "disclosure_date",
                        "posted_date",
                        "profile_id",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "downloaded_path": "data/public_portal/sample.hwp",
                        "title": "Clean PUBLIC_PORTAL Title",
                        "institution_name": "Example Institution",
                        "detail_url": "https://example.test/detail",
                        "board_no": "3533584",
                        "file_no": "3050658",
                        "disclosure_date": "2026.05.19",
                        "posted_date": "2026.05.19",
                        "profile_id": "public_portal-etc-law",
                    }
                )

            entries = load_manifest_entries(manifest, source_system="PUBLIC_PORTAL")

            self.assertEqual(entries[0]["path"], downloaded.resolve())
            self.assertEqual(entries[0]["document_name"], "Clean PUBLIC_PORTAL Title")
            self.assertEqual(entries[0]["institution_name"], "Example Institution")
            self.assertEqual(entries[0]["source_system"], "PUBLIC_PORTAL")
            self.assertEqual(entries[0]["source_url"], "https://example.test/detail")
            self.assertEqual(entries[0]["source_record_id"], "3533584")
            self.assertEqual(entries[0]["source_record_id_origin"], "board_no")
            self.assertEqual(entries[0]["source_file_id"], "3050658")
            self.assertEqual(entries[0]["source_disclosure_date"], "2026.05.19")
            self.assertEqual(entries[0]["source_disclosure_date_origin"], "disclosure_date")
            self.assertEqual(entries[0]["source_posted_date"], "2026.05.19")
            self.assertEqual(entries[0]["profile_id"], "public_portal-etc-law")

    def test_load_manifest_entries_uses_internal_rule_catalog_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "catalog.csv"
            downloaded = root / "data" / "public_portal" / "rule.hwpx"
            downloaded.parent.mkdir(parents=True)
            downloaded.write_bytes(b"hwpx")
            with manifest.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "downloaded_path",
                        "apba_id",
                        "rule_title",
                        "file_name",
                        "institution_name",
                        "detail_url",
                        "rule_idx",
                        "file_no",
                        "latest_file_no",
                        "latest_file_name",
                        "latest_file_ext",
                        "selected_latest_file",
                        "selection_policy",
                        "selection_warning",
                        "revision_date",
                        "posted_date",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "downloaded_path": "data/public_portal/rule.hwpx",
                        "apba_id": "C0147",
                        "rule_title": "Internal Rule Title",
                        "file_name": "fallback.hwpx",
                        "institution_name": "Example Institution",
                        "detail_url": "https://example.test/itemBoard21110.do",
                        "rule_idx": "21110",
                        "file_no": "98765",
                        "latest_file_no": "98766",
                        "latest_file_name": "latest-rules.zip",
                        "latest_file_ext": ".zip",
                        "selected_latest_file": "False",
                        "selection_policy": "latest_supported_fallback",
                        "selection_warning": "selected_supported_file_is_not_latest_public_portal_file",
                        "revision_date": "2026.07.09",
                        "posted_date": "2026.07.10",
                    }
                )

            entries = load_manifest_entries(manifest, source_system="PUBLIC_PORTAL")

            self.assertEqual(entries[0]["document_name"], "Internal Rule Title")
            self.assertEqual(entries[0]["source_record_id"], "21110")
            self.assertEqual(entries[0]["source_record_id_origin"], "rule_idx")
            self.assertEqual(entries[0]["source_file_id"], "98765")
            self.assertEqual(entries[0]["source_disclosure_date"], "2026.07.09")
            self.assertEqual(entries[0]["source_disclosure_date_origin"], "revision_date")
            self.assertEqual(entries[0]["source_posted_date"], "2026.07.10")
            self.assertEqual(entries[0]["apba_id"], "C0147")
            self.assertEqual(entries[0]["profile_id"], "public_portal-c0147")
            self.assertEqual(entries[0]["latest_file_no"], "98766")
            self.assertEqual(entries[0]["latest_file_name"], "latest-rules.zip")
            self.assertEqual(entries[0]["latest_file_ext"], ".zip")
            self.assertEqual(entries[0]["selected_latest_file"], "False")
            self.assertEqual(entries[0]["selection_policy"], "latest_supported_fallback")
            self.assertEqual(entries[0]["selection_warning"], "selected_supported_file_is_not_latest_public_portal_file")

    def test_source_selection_metadata_survives_failure_summary_rows(self) -> None:
        row = build_failure_row(
            Path("fallback.hwpx"),
            error=RuntimeError("parser failed"),
            elapsed_seconds=0.1,
            institution_name="Example Institution",
            apba_id="C0147",
            source_system="PUBLIC_PORTAL",
            source_record_id="21110",
            source_file_id="98765",
            source_selection_metadata={
                "latest_file_no": "98766",
                "latest_file_name": "latest-rules.zip",
                "latest_file_ext": ".zip",
                "selected_latest_file": "False",
                "selection_policy": "latest_supported_fallback",
                "selection_warning": "selected_supported_file_is_not_latest_public_portal_file",
            },
        )

        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["source_file_id"], "98765")
        self.assertEqual(row["latest_file_no"], "98766")
        self.assertEqual(row["latest_file_name"], "latest-rules.zip")
        self.assertEqual(row["latest_file_ext"], ".zip")
        self.assertEqual(row["selected_latest_file"], "False")
        self.assertEqual(row["selection_policy"], "latest_supported_fallback")
        self.assertEqual(row["selection_warning"], "selected_supported_file_is_not_latest_public_portal_file")

    def test_load_manifest_entries_skips_explicit_unsupported_catalog_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "catalog.csv"
            zip_path = root / "data" / "public_portal" / "rules.zip"
            pdf_path = root / "data" / "public_portal" / "rules.pdf"
            zip_path.parent.mkdir(parents=True)
            zip_path.write_bytes(b"zip")
            pdf_path.write_bytes(b"%PDF-1.4\n")
            with manifest.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["downloaded_path", "supported_by_preprocessor", "rule_idx", "file_no", "file_name"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "downloaded_path": "data/public_portal/rules.zip",
                        "supported_by_preprocessor": "False",
                        "rule_idx": "3854",
                        "file_no": "237775",
                        "file_name": "rules.zip",
                    }
                )
                writer.writerow(
                    {
                        "downloaded_path": "data/public_portal/rules.pdf",
                        "supported_by_preprocessor": "True",
                        "rule_idx": "3854",
                        "file_no": "237775::rules.pdf",
                        "file_name": "rules.pdf",
                    }
                )

            entries = load_manifest_entries(manifest, source_system="PUBLIC_PORTAL")

            self.assertEqual(1, len(entries))
            self.assertEqual(pdf_path.resolve(), entries[0]["path"])
            self.assertEqual("237775::rules.pdf", entries[0]["source_file_id"])

    def test_load_manifest_entries_rejects_supported_flag_on_unsupported_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "catalog.csv"
            exe_path = root / "data" / "public_portal" / "payload.exe"
            exe_path.parent.mkdir(parents=True)
            exe_path.write_bytes(b"exe")
            with manifest.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=["downloaded_path", "supported_by_preprocessor"])
                writer.writeheader()
                writer.writerow(
                    {
                        "downloaded_path": "data/public_portal/payload.exe",
                        "supported_by_preprocessor": "True",
                    }
                )

            with self.assertRaisesRegex(ValueError, "unsupported file extension"):
                load_manifest_entries(manifest, source_system="PUBLIC_PORTAL")

    def test_load_manifest_entries_dedupes_duplicate_source_file_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "catalog.csv"
            first = root / "first.hwp"
            second = root / "second.hwp"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            with manifest.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=["downloaded_path", "board_no", "file_no", "title"])
                writer.writeheader()
                writer.writerow({"downloaded_path": str(first), "board_no": "1", "file_no": "10", "title": "A"})
                writer.writerow({"downloaded_path": str(first), "board_no": "1", "file_no": "10", "title": "A duplicate"})
                writer.writerow({"downloaded_path": str(second), "board_no": "1", "file_no": "11", "title": "B"})

            entries = load_manifest_entries(manifest, source_system="PUBLIC_PORTAL")

            self.assertEqual(len(entries), 2)
            self.assertEqual([entry["source_file_id"] for entry in entries], ["10", "11"])

    def test_load_manifest_entries_prefers_existing_duplicate_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "catalog.csv"
            valid = root / "valid.hwp"
            valid.write_bytes(b"valid")
            missing = root / "missing.hwp"
            with manifest.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=["downloaded_path", "board_no", "file_no", "title", "file_sha256"])
                writer.writeheader()
                writer.writerow(
                    {
                        "downloaded_path": str(missing),
                        "board_no": "1",
                        "file_no": "10",
                        "title": "Missing duplicate",
                        "file_sha256": "",
                    }
                )
                writer.writerow(
                    {
                        "downloaded_path": str(valid),
                        "board_no": "1",
                        "file_no": "10",
                        "title": "Valid duplicate",
                        "file_sha256": "abc",
                    }
                )

            entries = load_manifest_entries(manifest, source_system="PUBLIC_PORTAL")

            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["path"], valid.resolve())
            self.assertEqual(entries[0]["document_name"], "Valid duplicate")

    def test_process_file_skips_existing_completed_same_source_hash_options(self) -> None:
        class UploadShouldNotRun:
            def upload(self, *args, **kwargs):
                raise AssertionError("upload should not run for unchanged input")

        class ProcessShouldNotRun:
            def process(self, *args, **kwargs):
                raise AssertionError("process should not run for unchanged input")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.hwp"
            content = b"same regulation bytes"
            source.write_bytes(content)
            settings = Settings(data_dir=root / "data")
            repo = JsonRepository(settings)
            options = ChunkOptions()
            document = Document(
                document_id="doc_existing",
                filename="sample.hwp",
                file_type="hwp",
                file_hash=sha256_bytes(content),
                source_system="PUBLIC_PORTAL",
                source_record_id="board-1",
                source_file_id="file-1",
                profile_id="public_portal-etc-law",
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
                    options=processing_options_payload(options),
                    stats={
                        "agent_review": {
                            "status": "planned",
                            "skip_reason": "",
                            "candidate_count": 5,
                            "selected_count": 3,
                            "estimated_input_tokens": 1200,
                            "estimated_output_tokens": 300,
                            "estimated_total_tokens": 1875,
                            "budget_exhausted": True,
                            "budget_reservation_id": "budget-old",
                            "approval_reference": "approval-old",
                            "model": "model-old",
                            "payload_hash": "sha256:old",
                            "estimated_cost": "1.23",
                            "actual_cost": "1.25",
                            "provider_request_id": "request-old",
                        }
                    },
                    artifacts=artifacts,
                )
            )

            row = process_file(
                source,
                document_service=UploadShouldNotRun(),
                processing_service=ProcessShouldNotRun(),
                repository=repo,
                chunk_options=options,
                source_system="PUBLIC_PORTAL",
                source_record_id="board-1",
                source_file_id="file-1",
                profile_id="public_portal-etc-law",
            )

            self.assertEqual(row["status"], "skipped_unchanged")
            self.assertEqual(row["document_id"], "doc_existing")
            self.assertTrue(row["quality_passed"])
            self.assertEqual(row["agent_review_status"], "skipped")
            self.assertEqual(row["agent_review_skip_reason"], "reused_unchanged")
            self.assertEqual(row["agent_review_candidate_count"], 0)
            self.assertEqual(row["agent_review_selected_count"], 0)
            self.assertEqual(row["agent_review_estimated_input_tokens"], 0)
            self.assertEqual(row["agent_review_estimated_output_tokens"], 0)
            self.assertEqual(row["agent_review_estimated_total_tokens"], 0)
            self.assertFalse(row["agent_review_budget_exhausted"])
            self.assertEqual(row["agent_review_budget_reservation_id"], "")
            self.assertEqual(row["agent_review_approval_reference"], "")
            self.assertEqual(row["agent_review_model"], "")
            self.assertEqual(row["agent_review_payload_hash"], "")
            self.assertEqual(row["agent_review_estimated_cost"], "")
            self.assertEqual(row["agent_review_actual_cost"], "")
            self.assertEqual(row["agent_review_provider_request_id"], "")
            self.assertEqual(row["agent_review_plan_json"], "")
            self.assertEqual(row["reused_from_run_id"], "run_existing")
            self.assertEqual(row["reused_from_job_id"], "job_existing")
            self.assertEqual(row["historical_agent_review_status"], "planned")
            self.assertEqual(row["historical_agent_review_selected_count"], 3)
            self.assertEqual(row["historical_agent_review_estimated_input_tokens"], 1200)
            self.assertEqual(row["historical_agent_review_estimated_output_tokens"], 300)
            self.assertEqual(row["historical_agent_review_estimated_total_tokens"], 1875)
            self.assertEqual(row["historical_agent_review_api_call_count"], 0)

    def test_process_file_does_not_skip_when_provenance_changes(self) -> None:
        class UploadExpected:
            def __init__(self) -> None:
                self.called = False

            def upload(self, *args, **kwargs):
                self.called = True
                raise RuntimeError("upload attempted")

        class ProcessShouldNotRun:
            def process(self, *args, **kwargs):
                raise AssertionError("process should not run after upload failure")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.hwp"
            content = b"same regulation bytes"
            source.write_bytes(content)
            settings = Settings(data_dir=root / "data")
            repo = JsonRepository(settings)
            options = ChunkOptions()
            document = Document(
                document_id="doc_existing",
                filename="sample.hwp",
                file_type="hwp",
                file_hash=sha256_bytes(content),
                source_system="PUBLIC_PORTAL",
                source_record_id="board-1",
                source_file_id="file-1",
                institution_name="Old Institution",
                source_url="https://example.test/old",
                source_disclosure_date="2026.01.01",
                source_posted_date="2026.01.02",
                profile_id="public_portal-etc-law",
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
                    options=processing_options_payload(options),
                    artifacts=artifacts,
                )
            )
            upload = UploadExpected()

            row = process_file(
                source,
                document_service=upload,
                processing_service=ProcessShouldNotRun(),
                repository=repo,
                chunk_options=options,
                institution_name="New Institution",
                source_system="PUBLIC_PORTAL",
                source_url="https://example.test/new",
                source_record_id="board-1",
                source_file_id="file-1",
                source_disclosure_date="2026.05.01",
                source_posted_date="2026.05.02",
                profile_id="public_portal-etc-law",
            )

            self.assertTrue(upload.called)
            self.assertEqual(row["status"], "failed")
            self.assertIn("upload attempted", row["error"])

    def test_process_file_does_not_skip_when_reusable_outputs_are_missing(self) -> None:
        class UploadExpected:
            def __init__(self) -> None:
                self.called = False

            def upload(self, *args, **kwargs):
                self.called = True
                raise RuntimeError("upload attempted")

        class ProcessShouldNotRun:
            def process(self, *args, **kwargs):
                raise AssertionError("process should not run after upload failure")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.hwp"
            content = b"same regulation bytes"
            source.write_bytes(content)
            settings = Settings(data_dir=root / "data")
            repo = JsonRepository(settings)
            options = ChunkOptions()
            document = Document(
                document_id="doc_missing_outputs",
                filename="sample.hwp",
                file_type="hwp",
                file_hash=sha256_bytes(content),
                source_system="PUBLIC_PORTAL",
                source_record_id="board-1",
                source_file_id="file-1",
                profile_id="public_portal-etc-law",
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
            upload = UploadExpected()

            row = process_file(
                source,
                document_service=upload,
                processing_service=ProcessShouldNotRun(),
                repository=repo,
                chunk_options=options,
                source_system="PUBLIC_PORTAL",
                source_record_id="board-1",
                source_file_id="file-1",
                profile_id="public_portal-etc-law",
            )

            self.assertTrue(upload.called)
            self.assertEqual(row["status"], "failed")
            self.assertIn("upload attempted", row["error"])

    def test_load_manifest_entries_preserves_existing_cwd_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "catalog.csv"
            existing = Path("data") / "public_portal_laws_sample"
            existing.mkdir(parents=True, exist_ok=True)
            sample = existing / "cwd_sample.hwp"
            sample.write_bytes(b"hwp")
            try:
                with manifest.open("w", newline="", encoding="utf-8-sig") as handle:
                    writer = csv.DictWriter(handle, fieldnames=["downloaded_path"])
                    writer.writeheader()
                    writer.writerow({"downloaded_path": str(sample)})

                entries = load_manifest_entries(manifest)

                self.assertEqual(entries[0]["path"], sample.resolve())
            finally:
                sample.unlink(missing_ok=True)

    def test_load_manifest_entries_prefers_manifest_relative_path_when_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "catalog.csv"
            manifest_relative = root / "data" / "public_portal" / "sample.hwp"
            cwd_relative = Path("data") / "public_portal" / "sample.hwp"
            manifest_relative.parent.mkdir(parents=True)
            cwd_relative.parent.mkdir(parents=True, exist_ok=True)
            manifest_relative.write_bytes(b"manifest")
            cwd_relative.write_bytes(b"cwd")
            try:
                with manifest.open("w", newline="", encoding="utf-8-sig") as handle:
                    writer = csv.DictWriter(handle, fieldnames=["downloaded_path"])
                    writer.writeheader()
                    writer.writerow({"downloaded_path": "data/public_portal/sample.hwp"})

                entries = load_manifest_entries(manifest)

                self.assertEqual(entries[0]["path"], manifest_relative.resolve())
            finally:
                cwd_relative.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
