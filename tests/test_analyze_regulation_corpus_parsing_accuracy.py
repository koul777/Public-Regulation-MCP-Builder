from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from scripts.analyze_regulation_corpus import (
    build_goldset_score_payload,
    build_parsing_automation_payload,
    chunk_review_flags,
    make_goldset_score_markdown,
    make_goldset_markdown,
    review_group_key,
    review_priority_tier,
    review_queue_row,
    refresh_goldset_label_rows,
    load_goldset_label_rows,
    select_goldset_rows,
    summarize_pipeline_counts,
    write_refreshed_goldset_labels,
    write_goldset_review_packets,
    write_goldset_report,
    write_goldset_score_report,
    write_parsing_automation_report,
)


class ParsingAccuracyReportTests(unittest.TestCase):
    def test_automation_payload_resolves_repository_from_batch_export_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "reports" / "overnight_runs" / "run-1" / "runtime"
            repository = runtime / "repository"
            exports = runtime / "exports"
            repository.mkdir(parents=True)
            exports.mkdir(parents=True)
            document_id = "doc_run_root"
            (repository / f"{document_id}_chunks.json").write_text(
                json.dumps([{"chunk_id": "c1", "chunk_type": "article", "text": "approved text"}]),
                encoding="utf-8",
            )
            quality_path = exports / f"{document_id}.quality.json"
            quality_path.write_text("{}", encoding="utf-8")
            batch_path = root / "reports" / "batch_quality_run_root.json"
            batch_path.write_text(
                json.dumps(
                    {
                        "rows": [
                            {
                                "document_id": document_id,
                                "filename": "sample.hwp",
                                "status": "completed",
                                "quality_json": str(quality_path.relative_to(root)),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            payload = build_parsing_automation_payload(root, [batch_path], root / "reports")

        self.assertEqual(1, payload["scope"]["analyzed_document_count"])
        self.assertEqual(0, payload["scope"]["missing_chunk_artifact_count"])
        self.assertEqual(1, payload["overall"]["chunk_count"])
        self.assertIn("reports", payload["documents"][0]["chunk_artifact"])

    def test_automation_payload_does_not_follow_export_path_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "workspace"
            outside = parent / "outside_runtime"
            (root / "reports").mkdir(parents=True)
            (outside / "repository").mkdir(parents=True)
            (outside / "exports").mkdir(parents=True)
            document_id = "doc_outside"
            (outside / "repository" / f"{document_id}_chunks.json").write_text(
                json.dumps([{"chunk_id": "secret", "text": "outside"}]),
                encoding="utf-8",
            )
            quality_path = outside / "exports" / f"{document_id}.quality.json"
            quality_path.write_text("{}", encoding="utf-8")
            batch_path = root / "reports" / "batch_quality_outside.json"
            batch_path.write_text(
                json.dumps(
                    {
                        "rows": [
                            {
                                "document_id": document_id,
                                "filename": "sample.pdf",
                                "status": "completed",
                                "quality_json": str(quality_path),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            payload = build_parsing_automation_payload(root, [batch_path], root / "reports")

        self.assertEqual(0, payload["scope"]["analyzed_document_count"])
        self.assertEqual(1, payload["scope"]["missing_chunk_artifact_count"])

    def test_automation_payload_accepts_absolute_export_path_inside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            document_id = "doc_absolute"
            quality_path = self._write_runtime_artifact(
                root / "reports" / "overnight_runs" / "run-absolute" / "runtime",
                document_id,
                "absolute-chunk",
            )
            batch_path = self._write_single_row_batch(
                root,
                {
                    "document_id": document_id,
                    "filename": "absolute.hwpx",
                    "status": "completed",
                    "quality_json": str(quality_path),
                },
                "absolute",
            )

            payload = build_parsing_automation_payload(root, [batch_path], root / "reports")

        self.assertEqual(1, payload["scope"]["analyzed_document_count"])
        self.assertEqual(0, payload["scope"]["missing_chunk_artifact_count"])
        self.assertEqual(1, payload["overall"]["chunk_count"])

    def test_automation_payload_ignores_missing_artifact_and_uses_existing_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            document_id = "doc_existing_later_field"
            runtime = root / "reports" / "overnight_runs" / "run-valid" / "runtime"
            tables_path = self._write_runtime_artifact(
                runtime,
                document_id,
                "tables-chunk",
                artifact_name=f"{document_id}.tables.jsonl",
            )
            batch_path = self._write_single_row_batch(
                root,
                {
                    "document_id": document_id,
                    "filename": "existing-later.pdf",
                    "status": "completed",
                    "quality_json": str(runtime / "exports" / "missing.quality.json"),
                    "tables_jsonl": str(tables_path.relative_to(root)),
                },
                "existing-later",
            )

            payload = build_parsing_automation_payload(root, [batch_path], root / "reports")

        self.assertEqual(1, payload["scope"]["analyzed_document_count"])
        self.assertEqual(0, payload["scope"]["missing_chunk_artifact_count"])
        self.assertEqual(1, payload["overall"]["chunk_count"])

    def test_automation_payload_rejects_conflicting_existing_runtime_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            document_id = "doc_conflicting_runtime"
            quality_path = self._write_runtime_artifact(
                root / "reports" / "overnight_runs" / "run-a" / "runtime",
                document_id,
                "runtime-a-chunk",
            )
            tables_path = self._write_runtime_artifact(
                root / "reports" / "overnight_runs" / "run-b" / "runtime",
                document_id,
                "runtime-b-chunk",
                artifact_name=f"{document_id}.tables.jsonl",
            )
            batch_path = self._write_single_row_batch(
                root,
                {
                    "document_id": document_id,
                    "filename": "conflict.hwp",
                    "status": "completed",
                    "quality_json": str(quality_path.relative_to(root)),
                    "tables_jsonl": str(tables_path.relative_to(root)),
                },
                "conflicting-runtime",
            )

            payload = build_parsing_automation_payload(root, [batch_path], root / "reports")

        self.assertEqual(0, payload["scope"]["analyzed_document_count"])
        self.assertEqual(1, payload["scope"]["missing_chunk_artifact_count"])

    def test_automation_payload_does_not_follow_repository_symlink_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "workspace"
            runtime = root / "reports" / "overnight_runs" / "run-link" / "runtime"
            exports = runtime / "exports"
            outside_repository = parent / "outside" / "repository"
            exports.mkdir(parents=True)
            outside_repository.mkdir(parents=True)
            document_id = "doc_repository_link"
            (outside_repository / f"{document_id}_chunks.json").write_text(
                json.dumps([{"chunk_id": "outside-secret", "text": "outside"}]),
                encoding="utf-8",
            )
            try:
                (runtime / "repository").symlink_to(outside_repository, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlink unavailable: {exc}")
            quality_path = exports / f"{document_id}.quality.json"
            quality_path.write_text("{}", encoding="utf-8")
            batch_path = self._write_single_row_batch(
                root,
                {
                    "document_id": document_id,
                    "filename": "link.pdf",
                    "status": "completed",
                    "quality_json": str(quality_path.relative_to(root)),
                },
                "repository-link",
            )

            payload = build_parsing_automation_payload(root, [batch_path], root / "reports")

        self.assertEqual(0, payload["scope"]["analyzed_document_count"])
        self.assertEqual(1, payload["scope"]["missing_chunk_artifact_count"])

    def test_automation_payload_preserves_data_discovery_when_row_artifact_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            document_id = "doc_data_compatibility"
            self._write_chunks(root, document_id, [{"chunk_id": "data-chunk", "text": "data"}])
            batch_path = self._write_single_row_batch(
                root,
                {
                    "document_id": document_id,
                    "filename": "data.hwp",
                    "status": "completed",
                    "quality_json": "reports/missing/runtime/exports/missing.quality.json",
                },
                "data-compatibility",
            )

            payload = build_parsing_automation_payload(root, [batch_path], root / "reports")

        self.assertEqual(1, payload["scope"]["analyzed_document_count"])
        self.assertEqual(0, payload["scope"]["missing_chunk_artifact_count"])
        self.assertIn("data", payload["documents"][0]["chunk_artifact"])

    def test_automation_payload_rejects_document_id_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "reports" / "overnight_runs" / "run-traversal" / "runtime"
            repository = runtime / "repository"
            exports = runtime / "exports"
            repository.mkdir(parents=True)
            exports.mkdir(parents=True)
            document_id = "../escaped"
            (runtime / "escaped_chunks.json").write_text(
                json.dumps([{"chunk_id": "escaped", "text": "escaped"}]),
                encoding="utf-8",
            )
            quality_path = exports / "escaped.quality.json"
            quality_path.write_text("{}", encoding="utf-8")
            batch_path = self._write_single_row_batch(
                root,
                {
                    "document_id": document_id,
                    "filename": "traversal.pdf",
                    "status": "completed",
                    "quality_json": str(quality_path.relative_to(root)),
                },
                "document-id-traversal",
            )

            payload = build_parsing_automation_payload(root, [batch_path], root / "reports")

        self.assertEqual(0, payload["scope"]["analyzed_document_count"])
        self.assertEqual(1, payload["scope"]["missing_chunk_artifact_count"])

    def test_automation_payload_counts_review_flags_by_extension_and_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_path = self._write_batch_report(root)
            self._write_chunks(
                root,
                "doc_hwp",
                [
                    {
                        "chunk_id": "c1",
                        "chunk_type": "article",
                        "text": "Article text",
                        "metadata": {"article_no": "제1조", "article_title": "Purpose"},
                    },
                    {
                        "chunk_id": "c2",
                        "chunk_type": "table",
                        "text": "table text",
                        "metadata": {
                            "table_like": True,
                            "table_review_required": True,
                            "table_review_flags": ["low_structured_row_count"],
                        },
                    },
                ],
            )
            self._write_chunks(
                root,
                "doc_hwpx",
                [
                    {
                        "chunk_id": "c3",
                        "chunk_type": "appendix",
                        "text": "appendix text",
                        "metadata": {"appendix_refs": ["별표1"]},
                    }
                ],
            )

            payload = build_parsing_automation_payload(
                root,
                [batch_path],
                Path("reports"),
                generated_at="20260708-120000",
            )

            self.assertEqual("heuristic_review_need_rate", payload["measurement_kind"])
            self.assertEqual(2, payload["scope"]["analyzed_document_count"])
            self.assertEqual(1, payload["scope"]["failed_document_count"])
            self.assertEqual(3, payload["overall"]["chunk_count"])
            self.assertEqual(1, payload["overall"]["review_required_candidate_count"])
            self.assertEqual(33.33, payload["overall"]["review_required_candidate_rate"])
            self.assertEqual(1, payload["priority_summary"]["no_signal"]["count"])
            self.assertEqual(1, payload["priority_summary"]["blocking_review"]["count"])
            self.assertEqual(0, payload["priority_summary"]["domain_attention"]["count"])
            self.assertEqual(1, payload["priority_summary"]["informational"]["count"])
            self.assertEqual(1, payload["by_extension"][".hwp"]["review_required_candidate_count"])
            self.assertEqual(2, payload["by_apba_id"]["C0147"]["chunk_count"])
            self.assertEqual(1, payload["by_apba_id"]["C0147"]["review_required_candidate_count"])
            self.assertEqual(1, payload["priority_by_apba_id"]["C0147"]["blocking_review"]["count"])
            self.assertEqual(1, payload["review_category_by_apba_id"]["C0147"]["table_structure_review"]["count"])
            self.assertIn("table_review_required", payload["by_chunk_type"]["table"]["flag_counts"])
            self.assertIn("low_structured_row_count", payload["by_chunk_type"]["table"]["flag_counts"])
            self.assertIn("form_or_appendix_candidate", payload["by_extension"][".hwpx"]["flag_counts"])
            self.assertIn("table_review_required", payload["review_flag_samples"])
            self.assertIn("low_structured_row_count", payload["review_flag_samples"])
            self.assertIn("form_or_appendix_candidate", payload["review_flag_samples"])
            self.assertEqual(1, len(payload["review_queue"]))
            self.assertEqual("blocking_review", payload["review_queue"][0]["priority_tier"])
            self.assertEqual("table_structure_review", payload["review_queue"][0]["review_category"])
            self.assertEqual(2, payload["review_queue"][0]["review_severity_rank"])
            self.assertIn("headers", payload["review_queue"][0]["review_step"])
            self.assertEqual("review_before_citation_grade_use", payload["review_queue"][0]["review_action"])
            self.assertIn("table_review_required", payload["review_queue"][0]["review_reason"])
            self.assertIn("low_structured_row_count", payload["review_queue"][0]["review_reason"])
            self.assertEqual("low_structured_row_count", payload["review_queue"][0]["table_review_flags"])
            self.assertEqual("C0147", payload["review_queue"][0]["apba_id"])
            self.assertEqual("public_portal-c0147", payload["review_queue"][0]["profile_id"])
            self.assertEqual("rule-a", payload["review_queue"][0]["source_record_id"])
            self.assertEqual("file-a", payload["review_queue"][0]["source_file_id"])
            self.assertEqual(1, payload["review_category_summary"]["table_structure_review"]["count"])
            self.assertEqual(1, payload["review_group_summary"]["review_queue_row_count"])
            self.assertEqual(1, payload["review_group_summary"]["review_group_count"])
            self.assertEqual(0, payload["review_group_summary"]["duplicate_review_queue_row_count"])
            chunk_artifacts = {row["chunk_artifact"] for row in payload["documents"]}
            source_reports = {row["source_batch_report"] for row in payload["documents"]}
            documents_by_id = {row["document_id"]: row for row in payload["documents"]}
            self.assertEqual("C0147", documents_by_id["doc_hwp"]["apba_id"])
            self.assertIn("data\\repository\\doc_hwp_chunks.json", chunk_artifacts)
            self.assertEqual({"reports\\batch_quality_test.json"}, source_reports)

    def test_pipeline_counts_appendix_form_logical_units_not_split_parts(self) -> None:
        chunks = [
            {
                "chunk_id": "form_7_part_1",
                "chunk_type": "form",
                "text": "Form 7 first page",
                "metadata": {"entity_id": "node_form_7", "part_index": 1, "part_count": 2},
            },
            {
                "chunk_id": "form_7_part_2",
                "chunk_type": "form",
                "text": "Form 7 second page",
                "metadata": {"entity_id": "node_form_7", "part_index": 2, "part_count": 2},
            },
            {
                "chunk_id": "appendix_1",
                "chunk_type": "appendix",
                "text": "Appendix 1",
                "metadata": {"entity_id": "node_appendix_1"},
            },
        ]

        summary = summarize_pipeline_counts(chunks, {"filename": "sample.pdf"})

        self.assertEqual(3, summary["appendix_or_form_candidate_chunk_count"])
        self.assertEqual(2, summary["appendix_or_form_logical_unit_count"])
        self.assertEqual(2, summary["appendix_or_form_candidate_count"])

    def test_pipeline_counts_hwpx_appendix_tables_are_not_separate_appendix_units(self) -> None:
        chunks = [
            {
                "chunk_id": "appendix_1",
                "chunk_type": "appendix",
                "text": "[\ubcc4\ud45c 1] \uae30\uc900\ud45c",
                "metadata": {"entity_id": "node_appendix_1"},
            },
            {
                "chunk_id": "appendix_1_table",
                "chunk_type": "table",
                "text": "\ubcc4\ud45c 1 \uc138\ubd80 \uae30\uc900",
                "metadata": {"entity_id": "node_table_1", "table_appendix_no": "\ubcc4\ud45c1"},
            },
        ]

        summary = summarize_pipeline_counts(chunks, {"filename": "sample.hwpx"})

        self.assertEqual(2, summary["appendix_or_form_candidate_chunk_count"])
        self.assertEqual(1, summary["appendix_or_form_logical_unit_count"])
        self.assertEqual(1, summary["appendix_or_form_candidate_count"])

    def test_pipeline_counts_hwpx_appendix_continuations_use_attachment_heading(self) -> None:
        chunks = [
            {
                "chunk_id": "appendix_1_part_1",
                "chunk_type": "appendix",
                "text": "[\ubcc4\ud45c 1] \uae30\uc900\ud45c",
                "metadata": {"entity_id": "node_appendix_1_part_1"},
            },
            {
                "chunk_id": "appendix_1_part_2",
                "chunk_type": "appendix",
                "text": "\uc774\uc5b4\uc9c0\ub294 \uae30\uc900\ud45c \ubcf8\ubb38",
                "metadata": {"entity_id": "node_appendix_1_part_2"},
            },
            {
                "chunk_id": "form_2",
                "chunk_type": "form",
                "text": "[\ubcc4\uc9c0 \uc81c2\ud638 \uc11c\uc2dd] \uc2e0\uccad\uc11c",
                "metadata": {"entity_id": "node_form_2"},
            },
        ]

        summary = summarize_pipeline_counts(chunks, {"filename": "sample.hwpx"})

        self.assertEqual(3, summary["appendix_or_form_candidate_chunk_count"])
        self.assertEqual(2, summary["appendix_or_form_logical_unit_count"])
        self.assertEqual(2, summary["appendix_or_form_candidate_count"])

    def test_pipeline_counts_visible_article_body_item_markers_without_inventory(self) -> None:
        chunks = [
            {
                "chunk_id": "article_with_inline_items",
                "chunk_type": "article",
                "normalized_text": "\uc81c4\uc870(\uacf5\uace0)\n\u2460 first paragraph\n1. numbered item\n\ub098. hangul item\n(1) parenthesized item",
                "metadata": {"article_no": "\uc81c4\uc870"},
            },
            {
                "chunk_id": "already_item",
                "chunk_type": "item",
                "normalized_text": "existing item chunk",
                "metadata": {},
            },
        ]

        summary = summarize_pipeline_counts(chunks, {"filename": "sample.pdf"})

        self.assertEqual("visible_article_body_deduped", summary["paragraph_item_count_source"])
        self.assertEqual(4, summary["visible_article_body_paragraph_item_count"])
        self.assertEqual(4, summary["paragraph_item_visible_marker_candidate_count"])
        self.assertEqual(1, summary["paragraph_item_traceable_unit_count"])
        self.assertEqual(4, summary["paragraph_or_item_chunk_count"])

    def test_pipeline_counts_prefers_article_structural_metadata_over_visible_markers(self) -> None:
        chunks = [
            {
                "chunk_id": "article_with_structural_count_part_1",
                "chunk_type": "article",
                "normalized_text": "\uc81c4\uc870(\uacf5\uace0)\n\u2460 visible paragraph\n1. visible item\n\ub098. visible item\n(1) visible subitem",
                "metadata": {
                    "article_no": "\uc81c4\uc870",
                    "entity_id": "article_4",
                    "paragraph_item_unit_count": 3,
                    "structural_child_count_source": "structure_detector",
                },
            },
            {
                "chunk_id": "article_with_structural_count_part_2",
                "chunk_type": "article",
                "normalized_text": "\uc81c4\uc870(\uacf5\uace0)\n\u2460 duplicate visible paragraph\n1. duplicate visible item",
                "metadata": {
                    "article_no": "\uc81c4\uc870",
                    "entity_id": "article_4",
                    "paragraph_item_unit_count": 3,
                    "structural_child_count_source": "structure_detector",
                },
            },
            {
                "chunk_id": "already_item",
                "chunk_type": "item",
                "normalized_text": "existing item chunk",
                "metadata": {},
            },
        ]

        summary = summarize_pipeline_counts(chunks, {"filename": "sample.pdf"})

        self.assertEqual("structural_child_metadata", summary["paragraph_item_count_source"])
        self.assertEqual(3, summary["structural_article_body_paragraph_item_count"])
        self.assertEqual(3, summary["paragraph_item_traceable_unit_count"])
        self.assertEqual(0, summary["paragraph_item_inventory_candidate_count"])
        self.assertEqual(0, summary["visible_article_body_paragraph_item_count"])
        self.assertEqual(3, summary["paragraph_or_item_chunk_count"])

    def test_pipeline_counts_does_not_double_count_article_scoped_item_chunks(self) -> None:
        chunks = [
            {
                "chunk_id": "article_with_structural_count",
                "chunk_type": "article",
                "normalized_text": "\uc81c4\uc870(\uacf5\uace0)\n\u2460 visible paragraph\n1. visible item\n\ub098. visible item",
                "metadata": {
                    "article_no": "\uc81c4\uc870",
                    "entity_id": "article_4",
                    "paragraph_item_unit_count": 3,
                    "structural_child_count_source": "structure_detector",
                },
            },
            {
                "chunk_id": "same_article_item_chunk",
                "chunk_type": "item",
                "normalized_text": "1. visible item",
                "metadata": {"article_no": "\uc81c4\uc870"},
            },
            {
                "chunk_id": "standalone_preamble_paragraph",
                "chunk_type": "paragraph",
                "normalized_text": "preamble",
                "metadata": {},
            },
        ]

        summary = summarize_pipeline_counts(chunks, {"filename": "sample.pdf"})

        self.assertEqual("structural_child_metadata", summary["paragraph_item_count_source"])
        self.assertEqual(3, summary["structural_article_body_paragraph_item_count"])
        self.assertEqual(3, summary["paragraph_or_item_chunk_count"])

    def test_pipeline_counts_deduplicates_split_article_structural_metadata_by_article_no(self) -> None:
        chunks = [
            {
                "chunk_id": "article_5_part_1",
                "chunk_type": "article",
                "metadata": {
                    "regulation_no": "rule-a",
                    "article_no": "\uc81c5\uc870",
                    "entity_id": "article_5_part_1",
                    "paragraph_item_unit_count": 36,
                    "structural_child_count_source": "structure_detector",
                },
            },
            {
                "chunk_id": "article_5_part_2",
                "chunk_type": "article",
                "metadata": {
                    "regulation_no": "rule-a",
                    "article_no": "\uc81c5\uc870",
                    "entity_id": "article_5_part_2",
                    "paragraph_item_unit_count": 36,
                    "structural_child_count_source": "structure_detector",
                },
            },
        ]

        summary = summarize_pipeline_counts(chunks, {"filename": "sample.pdf"})

        self.assertEqual("structural_child_metadata", summary["paragraph_item_count_source"])
        self.assertEqual(36, summary["structural_article_body_paragraph_item_count"])
        self.assertEqual(36, summary["paragraph_or_item_chunk_count"])

    def test_pipeline_counts_keeps_same_article_no_in_different_regulations_separate(self) -> None:
        chunks = [
            {
                "chunk_id": "rule_a_article_5",
                "chunk_type": "article",
                "metadata": {
                    "regulation_no": "rule-a",
                    "article_no": "\uc81c5\uc870",
                    "entity_id": "rule_a_article_5",
                    "paragraph_item_unit_count": 3,
                    "structural_child_count_source": "structure_detector",
                },
            },
            {
                "chunk_id": "rule_b_article_5",
                "chunk_type": "article",
                "metadata": {
                    "regulation_no": "rule-b",
                    "article_no": "\uc81c5\uc870",
                    "entity_id": "rule_b_article_5",
                    "paragraph_item_unit_count": 4,
                    "structural_child_count_source": "structure_detector",
                },
            },
        ]

        summary = summarize_pipeline_counts(chunks, {"filename": "sample.pdf"})

        self.assertEqual(7, summary["structural_article_body_paragraph_item_count"])
        self.assertEqual(7, summary["paragraph_or_item_chunk_count"])

    def test_pipeline_counts_uses_footnote_links_as_logical_units(self) -> None:
        chunks = [
            {
                "chunk_id": "article_with_footnotes",
                "chunk_type": "article",
                "normalized_text": "Article body",
                "metadata": {
                    "article_no": "Article 1",
                    "footnote_links": [
                        {"marker": "1", "text": "first footnote"},
                        {"marker": "2", "text": "second footnote"},
                    ],
                },
            }
        ]

        summary = summarize_pipeline_counts(chunks, {"filename": "sample.pdf"})

        self.assertEqual("footnote_links", summary["footnote_or_caption_count_source"])
        self.assertEqual(2, summary["footnote_link_logical_unit_count"])
        self.assertEqual(2, summary["footnote_or_caption_candidate_count"])

    def test_pipeline_counts_uses_pdf_roman_marker_references_when_links_are_incomplete(self) -> None:
        chunks = [
            {
                "chunk_id": "appendix_with_continuation_markers",
                "chunk_type": "appendix",
                "normalized_text": "별표4 보상대상 직무발명자 세부기준",
                "metadata": {
                    "footnote_links": [
                        {"marker": "ⅰ", "source_page": 23},
                        {"marker": "ⅱ", "source_page": 23},
                    ],
                    "footnote_marker_reference_count": 5,
                    "footnote_marker_references": [
                        {"source_page": 23, "marker_count": 2, "markers": ["ⅰ", "ⅱ"]},
                        {"source_page": 24, "marker_count": 3, "markers": ["ⅰ", "ⅲ", "ⅴ"]},
                    ],
                },
            }
        ]

        summary = summarize_pipeline_counts(chunks, {"filename": "sample.pdf"})

        self.assertEqual("footnote_marker_references", summary["footnote_or_caption_count_source"])
        self.assertEqual(2, summary["footnote_link_logical_unit_count"])
        self.assertEqual(5, summary["footnote_marker_reference_unit_count"])
        self.assertEqual(5, summary["footnote_or_caption_candidate_count"])

    def test_pipeline_counts_hwp_inventory_remains_authoritative_for_paragraph_items(self) -> None:
        chunks = [
            {
                "chunk_id": "article_with_inventory",
                "chunk_type": "article",
                "normalized_text": "\uc81c4\uc870(\uacf5\uace0)\n\u2460 visible paragraph\n1. visible numbered item",
                "metadata": {
                    "document_inventory": {
                        "source": "hwp",
                        "hierarchy": {
                            "articles": 1,
                            "paragraphs": 7,
                            "numbered_items": 3,
                            "hangul_items": 2,
                            "parenthesized_items": 1,
                        },
                    }
                },
            }
        ]

        summary = summarize_pipeline_counts(chunks)

        self.assertEqual(0, summary["visible_article_body_paragraph_item_count"])
        self.assertEqual(0, summary["paragraph_item_traceable_unit_count"])
        self.assertEqual(13, summary["paragraph_item_inventory_candidate_count"])
        self.assertEqual(13, summary["paragraph_or_item_chunk_count"])

    def test_pipeline_counts_hwp_table_count_prefers_kordoc_promoted_units(self) -> None:
        chunks = [
            {
                "chunk_id": "inventory",
                "chunk_type": "article",
                "text": "inventory carrier",
                "metadata": {
                    "document_inventory": {
                        "source": "hwp",
                        "tables": {"total": 4, "nested": 1},
                        "hierarchy": {"articles": 1},
                    }
                },
            },
            {
                "chunk_id": "kordoc_table_1",
                "chunk_type": "table",
                "text": "table one",
                "metadata": {
                    "kordoc_table_promoted": True,
                    "table_like": True,
                    "kordoc_table_match": {"table_index": 1},
                },
            },
            {
                "chunk_id": "kordoc_table_1_part_2",
                "chunk_type": "table",
                "text": "table one continued",
                "metadata": {
                    "kordoc_table_promoted": True,
                    "table_like": True,
                    "kordoc_table_match": {"table_index": 1},
                },
            },
            {
                "chunk_id": "kordoc_table_2",
                "chunk_type": "table",
                "text": "table two",
                "metadata": {"table_source": "kordoc", "table_like": True, "kordoc_table_match": {"table_index": 2}},
            },
        ]

        summary = summarize_pipeline_counts(chunks)

        self.assertEqual(2, summary["table_like_chunk_count"])
        self.assertEqual("kordoc_promoted_hwp", summary["table_like_count_source"])
        self.assertEqual(2, summary["kordoc_promoted_table_unit_count"])
        self.assertEqual(4, summary["hwp_inventory_table_like_chunk_count"])
        self.assertEqual(1, summary["nested_table_candidate_count"])

    def test_pipeline_counts_non_hwp_inventory_keeps_inventory_table_total(self) -> None:
        chunks = [
            {
                "chunk_id": "inventory",
                "chunk_type": "article",
                "text": "inventory carrier",
                "metadata": {
                    "document_inventory": {
                        "source": "hwpx",
                        "tables": {"total": 5, "nested": 1},
                        "hierarchy": {"articles": 1},
                    }
                },
            },
            {
                "chunk_id": "kordoc_table_1",
                "chunk_type": "table",
                "text": "table one",
                "metadata": {"kordoc_table_promoted": True, "table_like": True},
            },
        ]

        summary = summarize_pipeline_counts(chunks)

        self.assertEqual(5, summary["table_like_chunk_count"])
        self.assertEqual("document_inventory", summary["table_like_count_source"])
        self.assertEqual(1, summary["kordoc_promoted_table_unit_count"])
        self.assertEqual(0, summary["hwp_inventory_table_like_chunk_count"])

    def test_pipeline_counts_table_citation_ready_excludes_page_less_kordoc_unmatched(self) -> None:
        chunks = [
            {
                "chunk_id": "appendix_table",
                "chunk_type": "appendix",
                "source_page_start": 3,
                "source_page_end": 3,
                "metadata": {
                    "table_like": True,
                    "table_appendix_no": "appendix-1",
                    "table_cell_rows": [{"cells": ["A", "B"]}],
                },
            },
            {
                "chunk_id": "unmatched_kordoc_table",
                "chunk_type": "table",
                "metadata": {
                    "table_like": True,
                    "table_source": "kordoc",
                    "kordoc_table_unmatched_source": True,
                    "table_cell_rows": [{"cells": ["ordinary prose", "split as table"]}],
                    "table_citation_label": "not page anchored",
                },
            },
        ]

        summary = summarize_pipeline_counts(chunks, {"filename": "sample.pdf"})

        self.assertEqual(2, summary["table_like_chunk_count"])
        self.assertEqual(1, summary["table_citation_ready_chunk_count"])
        self.assertEqual(1, summary["table_goldset_preserved_count"])
        self.assertEqual("citation_ready", summary["table_goldset_count_source"])
        self.assertEqual(1, summary["page_less_kordoc_only_table_count"])

    def test_pipeline_counts_hwpx_supplementary_prefers_explicit_units(self) -> None:
        chunks = [
            {
                "chunk_id": "supplementary_1",
                "chunk_type": "supplementary_provision",
                "text": "\ubd80\uce59 \uc81c1\uc870(\uc801\uc6a9\ub840) \uc81c5\uc870\ub294 2026\ub144\ubd80\ud130 \uc801\uc6a9\ud55c\ub2e4.",
                "metadata": {
                    "is_supplementary_provision": True,
                    "article_effective_overrides": [
                        {"article_ref": "\uc81c5\uc870", "effective_date": "2026-01-01"}
                    ],
                },
            },
            {
                "chunk_id": "supplementary_2",
                "chunk_type": "supplementary_provision",
                "text": "\ubd80\uce59 \uc81c2\uc870(\uacbd\uacfc\uc870\uce58) \uc885\uc804 \uaddc\uc815\uc744 \uc801\uc6a9\ud55c\ub2e4.",
                "metadata": {
                    "is_supplementary_provision": True,
                    "article_effective_overrides": [
                        {"article_ref": "\uc81c6\uc870", "effective_date": "2026-02-01"}
                    ],
                },
            },
            {
                "chunk_id": "supplementary_child_article",
                "chunk_type": "article",
                "text": "\uc81c1\uc870(\uc2dc\ud589\uc77c) \uc774 \uaddc\uc815\uc740 2026\ub144 1\uc6d4 1\uc77c\ubd80\ud130 \uc2dc\ud589\ud55c\ub2e4.",
                "metadata": {"effective_date": "2026-01-01"},
            },
        ]

        summary = summarize_pipeline_counts(chunks, {"filename": "sample.hwpx"})

        self.assertEqual(3, summary["supplementary_or_effective_date_candidate_chunk_count"])
        self.assertEqual(2, summary["supplementary_logical_unit_count"])
        self.assertEqual(2, summary["supplementary_or_effective_date_candidate_count"])
        self.assertEqual("supplementary_logical_units", summary["supplementary_count_source"])

    def test_pipeline_counts_sparse_pdf_supplementary_keeps_chunk_count(self) -> None:
        chunks = [
            {
                "chunk_id": "supplementary_1",
                "chunk_type": "supplementary_provision",
                "text": "\ubd80\uce59 \uc81c1\uc870(\uc801\uc6a9\ub840) \uc81c5\uc870\ub294 2026\ub144\ubd80\ud130 \uc801\uc6a9\ud55c\ub2e4.",
                "metadata": {
                    "is_supplementary_provision": True,
                    "article_effective_overrides": [
                        {"article_ref": "\uc81c5\uc870", "effective_date": "2026-01-01"}
                    ],
                },
            },
            {
                "chunk_id": "article_1",
                "chunk_type": "article",
                "text": "\uc81c1\uc870(\uc2dc\ud589\uc77c) \uc774 \uaddc\uc815\uc740 2026\ub144 1\uc6d4 1\uc77c\ubd80\ud130 \uc2dc\ud589\ud55c\ub2e4.",
                "metadata": {"effective_date": "2026-01-01"},
            },
        ]

        summary = summarize_pipeline_counts(chunks, {"filename": "sample.pdf"})

        self.assertEqual(2, summary["supplementary_or_effective_date_candidate_chunk_count"])
        self.assertEqual(0, summary["supplementary_logical_unit_count"])
        self.assertEqual(2, summary["supplementary_or_effective_date_candidate_count"])
        self.assertEqual("chunk_flags", summary["supplementary_count_source"])

    def test_pipeline_counts_dense_pdf_supplementary_prefers_explicit_units(self) -> None:
        chunks = []
        for index in range(10):
            chunks.append(
                {
                    "chunk_id": f"supplementary_{index}",
                    "chunk_type": "supplementary_provision",
                    "text": "\ubd80\uce59 \uc81c1\uc870(\uc2dc\ud589\uc77c) \uc774 \uaddc\uc815\uc740 2026\ub144 1\uc6d4 1\uc77c\ubd80\ud130 \uc2dc\ud589\ud55c\ub2e4.",
                    "metadata": {
                        "is_supplementary_provision": True,
                        "article_effective_overrides": [
                            {"article_ref": "\uc81c5\uc870", "effective_date": "2026-01-01"}
                        ],
                    },
                }
            )
        for index in range(12):
            chunks.append(
                {
                    "chunk_id": f"supplementary_child_{index}",
                    "chunk_type": "paragraph",
                    "text": "\u2460(\uc2dc\ud589\uc77c) \uc774 \uaddc\uc815\uc740 2026\ub144 1\uc6d4 1\uc77c\ubd80\ud130 \uc2dc\ud589\ud55c\ub2e4.",
                    "metadata": {
                        "is_supplementary_provision": True,
                        "article_effective_overrides": [
                            {"article_ref": "\uc81c5\uc870", "effective_date": "2026-01-01"}
                        ],
                    },
                }
            )

        summary = summarize_pipeline_counts(chunks, {"filename": "sample.pdf"})

        self.assertEqual(22, summary["supplementary_or_effective_date_candidate_chunk_count"])
        self.assertEqual(10, summary["supplementary_logical_unit_count"])
        self.assertEqual(10, summary["supplementary_or_effective_date_candidate_count"])
        self.assertEqual("supplementary_logical_units", summary["supplementary_count_source"])

    def test_review_queue_groups_rows_from_same_hwpx_source_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_path = self._write_batch_report(root)
            self._write_chunks(
                root,
                "doc_hwp",
                [
                    {
                        "chunk_id": "clean",
                        "chunk_type": "article",
                        "text": "Clean article text",
                        "metadata": {"article_no": "1", "article_title": "Purpose"},
                    }
                ],
            )
            parent_source_metadata = {
                "source_hwpx_xml_block_indices": [41, 42],
                "source_hwpx_parser_review_flags": ["nested_table"],
                "source_hwpx_nested_table_count": 1,
                "source_hwpx_nested_table_text_snippets": ["same nested evidence checklist"],
            }
            child_source_metadata = {
                "source_hwpx_xml_block_indices": [42],
                "source_hwpx_parser_review_flags": ["nested_table"],
                "source_hwpx_nested_table_count": 1,
                "source_hwpx_nested_table_text_snippets": ["same nested evidence checklist"],
            }
            self._write_chunks(
                root,
                "doc_hwpx",
                [
                    {
                        "chunk_id": "form_from_block",
                        "chunk_type": "form",
                        "text": "Nested form table",
                        "metadata": dict(parent_source_metadata),
                    },
                    {
                        "chunk_id": "table_from_block",
                        "chunk_type": "table",
                        "text": "Nested table rows",
                        "metadata": dict(child_source_metadata),
                    },
                ],
            )

            payload = build_parsing_automation_payload(
                root,
                [batch_path],
                Path("reports"),
                generated_at="20260708-120000",
            )

            rows = payload["review_queue"]
            self.assertEqual(2, len(rows))
            group_keys = {row["review_group_key"] for row in rows}
            self.assertEqual(1, len(group_keys))
            self.assertTrue(next(iter(group_keys)).startswith("doc:doc_hwpx|hwpx_nested:"))
            self.assertTrue(next(iter(group_keys)).endswith("|xml:42"))
            self.assertEqual([2, 2], [row["review_group_duplicate_count"] for row in rows])
            self.assertEqual([True, False], [row["review_group_primary"] for row in rows])
            self.assertEqual(2, payload["review_group_summary"]["review_queue_row_count"])
            self.assertEqual(1, payload["review_group_summary"]["review_group_count"])
            self.assertEqual(1, payload["review_group_summary"]["duplicate_review_queue_row_count"])
            self.assertEqual(50.0, payload["review_group_summary"]["grouped_review_workload_rate"])

    def test_review_queue_does_not_group_same_hwpx_snippet_from_different_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_path = self._write_batch_report(root)
            self._write_chunks(root, "doc_hwp", [])
            base_metadata = {
                "source_hwpx_parser_review_flags": ["nested_table"],
                "source_hwpx_nested_table_count": 1,
                "source_hwpx_nested_table_text_snippets": ["same repeated nested boilerplate"],
            }
            self._write_chunks(
                root,
                "doc_hwpx",
                [
                    {
                        "chunk_id": "nested_a",
                        "chunk_type": "table",
                        "text": "Nested A",
                        "metadata": {**base_metadata, "source_hwpx_xml_block_indices": [1]},
                    },
                    {
                        "chunk_id": "nested_b",
                        "chunk_type": "table",
                        "text": "Nested B",
                        "metadata": {**base_metadata, "source_hwpx_xml_block_indices": [99]},
                    },
                ],
            )

            payload = build_parsing_automation_payload(
                root,
                [batch_path],
                Path("reports"),
                generated_at="20260708-120000",
            )

        rows = payload["review_queue"]
        self.assertEqual(2, len(rows))
        self.assertEqual(2, len({row["review_group_key"] for row in rows}))
        self.assertEqual(2, payload["review_group_summary"]["review_group_count"])
        self.assertEqual(0, payload["review_group_summary"]["duplicate_review_queue_row_count"])

    def test_revision_structural_group_key_groups_same_source_appendix_part(self) -> None:
        row_a = {"document_id": "doc_2024", "source_record_id": "rule-1"}
        row_b = {"document_id": "doc_2025", "source_record_id": "rule-1"}
        base_metadata = {
            "regulation_no": "직제규정시행세칙",
            "table_review_required": True,
            "table_appendix_no": "appendix",
        }
        chunk_a = {
            "chunk_id": "doc_2024_appendix_appendix-3_0296_p30_001",
            "chunk_type": "appendix",
            "text": "same appendix table",
            "metadata": {
                **base_metadata,
                "table_review_flags": ["dense_numeric_row_reconstruction", "unstable_column_count"],
            },
        }
        chunk_b = {
            "chunk_id": "doc_2025_appendix_appendix-3_0296_p30_001",
            "chunk_type": "appendix",
            "text": "same appendix table in revised version",
            "metadata": {
                **base_metadata,
                "table_review_flags": ["unstable_column_count", "dense_numeric_row_reconstruction"],
            },
        }
        chunk_c = {
            "chunk_id": "doc_2025_appendix_appendix-3_0296_p30_002",
            "chunk_type": "appendix",
            "text": "next part of same appendix table",
            "metadata": {
                **base_metadata,
                "table_review_flags": ["dense_numeric_row_reconstruction", "unstable_column_count"],
            },
        }

        key_a = review_group_key(row_a, chunk_a, chunk_a["metadata"])
        key_b = review_group_key(row_b, chunk_b, chunk_b["metadata"])
        key_c = review_group_key(row_b, chunk_c, chunk_c["metadata"])

        self.assertEqual(key_a, key_b)
        self.assertNotEqual(key_a, key_c)
        self.assertIn("source_record:rule-1", key_a)
        self.assertIn("unit:appendix-3", key_a)
        self.assertIn("part:001", key_a)

    def test_revision_structural_group_key_keeps_different_form_numbers_separate(self) -> None:
        row = {"document_id": "doc_2025", "source_record_id": "rule-2"}
        base_metadata = {
            "regulation_no": "conduct-rule",
            "table_review_required": True,
            "form_no": "form",
            "table_review_flags": ["unstable_column_count"],
        }
        chunk_a = {
            "chunk_id": "doc_2025_form_form-7_0162_p11_001",
            "chunk_type": "form",
            "text": "form 7",
            "metadata": dict(base_metadata),
        }
        chunk_b = {
            "chunk_id": "doc_2025_form_form-10_0163_p12_001",
            "chunk_type": "form",
            "text": "form 10",
            "metadata": dict(base_metadata),
        }

        key_a = review_group_key(row, chunk_a, chunk_a["metadata"])
        key_b = review_group_key(row, chunk_b, chunk_b["metadata"])

        self.assertNotEqual(key_a, key_b)
        self.assertIn("unit:form-7", key_a)
        self.assertIn("unit:form-10", key_b)

    def test_revision_structural_group_key_skips_unit_labels_without_numbers(self) -> None:
        row = {"document_id": "doc_2025", "source_record_id": "rule-3"}
        metadata = {
            "regulation_no": "conduct-rule",
            "table_review_required": True,
            "form_no": "form",
            "table_review_flags": ["unstable_column_count"],
        }
        chunk = {
            "chunk_id": "doc_2025_form_form_0162_p11_001",
            "chunk_type": "form",
            "text": "form with missing number",
            "metadata": metadata,
        }

        key = review_group_key(row, chunk, metadata)

        self.assertEqual("doc:doc_2025|chunk:doc_2025_form_form_0162_p11_001", key)

    def test_hwp_binary_geometry_group_key_groups_same_source_unit(self) -> None:
        row_a = {"document_id": "doc_2024", "source_record_id": "rule-hwp"}
        row_b = {"document_id": "doc_2025", "source_record_id": "rule-hwp"}
        base_metadata = {
            "regulation_no": "pay-rule",
            "table_like": True,
            "table_appendix_no": "appendix-1",
            "source_hwp_extraction_modes": ["legacy_ole_para_text_only"],
            "source_hwp_section_indices": [0],
            "source_hwp_native_table_geometry": False,
        }
        chunk_a = {
            "chunk_id": "doc_2024_table_appendix-1_0100_p10_001",
            "chunk_type": "table",
            "text": "same hwp table unit",
            "metadata": dict(base_metadata),
        }
        chunk_b = {
            "chunk_id": "doc_2025_table_appendix-1_0100_p10_002",
            "chunk_type": "table",
            "text": "same hwp table unit in revised document",
            "metadata": dict(base_metadata),
        }
        chunk_c = {
            "chunk_id": "doc_2025_table_appendix-2_0100_p10_001",
            "chunk_type": "table",
            "text": "different hwp table unit",
            "metadata": {**base_metadata, "table_appendix_no": "appendix-2"},
        }
        flags = ["hwp_binary_table_geometry_candidate"]

        key_a = review_group_key(row_a, chunk_a, chunk_a["metadata"], flags)
        key_b = review_group_key(row_b, chunk_b, chunk_b["metadata"], flags)
        key_c = review_group_key(row_b, chunk_c, chunk_c["metadata"], flags)

        self.assertEqual(key_a, key_b)
        self.assertNotEqual(key_a, key_c)
        self.assertIn("source_record:rule-hwp", key_a)
        self.assertIn("hwp_geometry:table", key_a)
        self.assertIn("unit:appendix-1", key_a)

    def test_hwp_binary_geometry_group_key_skips_table_extraction_blockers(self) -> None:
        row = {"document_id": "doc_hwp", "source_record_id": "rule-hwp"}
        metadata = {
            "regulation_no": "pay-rule",
            "table_like": True,
            "table_appendix_no": "appendix-1",
            "source_hwp_extraction_modes": ["legacy_ole_para_text_only"],
            "source_hwp_section_indices": [0],
            "source_hwp_native_table_geometry": False,
        }
        chunk_a = {
            "chunk_id": "doc_hwp_table_appendix-1_0100_p10_001",
            "chunk_type": "table",
            "text": "blocked hwp table unit",
            "metadata": metadata,
        }
        chunk_b = {
            **chunk_a,
            "chunk_id": "doc_hwp_table_appendix-1_0100_p10_002",
        }
        flags = ["hwp_binary_table_geometry_candidate", "table_extraction_failed_candidate"]

        key_a = review_group_key(row, chunk_a, metadata, flags)
        key_b = review_group_key(row, chunk_b, metadata, flags)

        self.assertNotEqual(key_a, key_b)
        self.assertEqual("doc:doc_hwp|chunk:doc_hwp_table_appendix-1_0100_p10_001", key_a)

    def test_repeated_note_group_key_groups_same_source_article_note_by_text(self) -> None:
        row_a = {"document_id": "doc_2024", "source_record_id": "rule-4"}
        row_b = {"document_id": "doc_2025", "source_record_id": "rule-4"}
        metadata = {"regulation_no": "사규규정", "article_no": "제3조", "form_refs": ["별지제1호서식"]}
        chunk_a = {
            "chunk_id": "doc_2024_article_제3조_0209_p13_001",
            "chunk_type": "article",
            "text": "[위치] 사규규정 > 부칙 > 제3조 [본문] 제3조(○○○) 주) 부칙에 시행일 외 다른 내용이 포함된 경우",
            "metadata": metadata,
        }
        chunk_b = {
            "chunk_id": "doc_2025_article_제3조_0188_p11_001",
            "chunk_type": "article",
            "text": "[위치] 사규규정 > 부칙 > 제3조 [본문] 제3조(○○○) 주) 부칙에 시행일 외 다른 내용이 포함된 경우",
            "metadata": metadata,
        }
        chunk_c = {
            "chunk_id": "doc_2025_article_제3조_0189_p11_001",
            "chunk_type": "article",
            "text": "[위치] 사규규정 > 부칙 > 제3조 [본문] 제3조(○○○) 주) 다른 주석 문구",
            "metadata": metadata,
        }
        flags = ["footnote_or_caption_candidate"]

        key_a = review_group_key(row_a, chunk_a, metadata, flags)
        key_b = review_group_key(row_b, chunk_b, metadata, flags)
        key_c = review_group_key(row_b, chunk_c, metadata, flags)

        self.assertEqual(key_a, key_b)
        self.assertNotEqual(key_a, key_c)
        self.assertIn("note:article", key_a)
        self.assertIn("anchor:제3조", key_a)

    def test_repeated_supplementary_group_key_uses_location_date_signature(self) -> None:
        row_a = {"document_id": "doc_2024", "source_record_id": "rule-5"}
        row_b = {"document_id": "doc_2025", "source_record_id": "rule-5"}
        metadata = {"regulation_no": "취업규칙"}
        chunk_a = {
            "chunk_id": "doc_2024_paragraph_①_0436_p18_001",
            "chunk_type": "paragraph",
            "text": "[위치] 취업규칙 > 부칙 (2018. 7. 23) > ① 시행일 [본문] ① (시행일) 이 규칙은 공포일부터 시행한다.",
            "metadata": metadata,
        }
        chunk_b = {
            "chunk_id": "doc_2025_paragraph_①_0436_p18_001",
            "chunk_type": "paragraph",
            "text": "[위치] 취업규칙 > 부칙 (2018. 7. 23) > ① 시행일 [본문] ① (시행일) 이 규칙은 공포일부터 시행한다.",
            "metadata": metadata,
        }
        chunk_c = {
            "chunk_id": "doc_2025_paragraph_①_0437_p18_001",
            "chunk_type": "paragraph",
            "text": "[위치] 취업규칙 > 부칙 (2019. 1. 1) > ① 시행일 [본문] ① (시행일) 이 규칙은 공포일부터 시행한다.",
            "metadata": metadata,
        }
        flags = ["supplementary_or_effective_date_candidate"]

        key_a = review_group_key(row_a, chunk_a, metadata, flags)
        key_b = review_group_key(row_b, chunk_b, metadata, flags)
        key_c = review_group_key(row_b, chunk_c, metadata, flags)

        self.assertEqual(key_a, key_b)
        self.assertNotEqual(key_a, key_c)
        self.assertIn("supplementary:paragraph", key_a)
        self.assertIn("source_record:rule-5", key_a)

    def test_repeated_supplementary_group_key_splits_temporal_metadata(self) -> None:
        row = {"document_id": "doc_2025", "source_record_id": "rule-6", "apba_id": "C0165", "profile_id": "public_portal-c0165"}
        text = "[location] employment-rule > addenda > effective date [body] This rule takes effect on promulgation."
        chunk_a = {
            "chunk_id": "doc_2025_paragraph_001",
            "chunk_type": "paragraph",
            "paragraph_no": "1",
            "text": text,
            "metadata": {
                "regulation_no": "employment-rule",
                "effective_date": "2026-01-01",
                "valid_from": "2026-01-01",
                "supplementary_identifier_date": "2025-12-31",
            },
        }
        chunk_b = {
            **chunk_a,
            "metadata": {
                "regulation_no": "employment-rule",
                "effective_date": "2026-02-01",
                "valid_from": "2026-02-01",
                "supplementary_identifier_date": "2026-01-31",
            },
        }
        flags = ["supplementary_or_effective_date_candidate"]

        key_a = review_group_key(row, chunk_a, chunk_a["metadata"], flags)
        key_b = review_group_key(row, chunk_b, chunk_b["metadata"], flags)

        self.assertNotEqual(key_a, key_b)
        self.assertIn("effective_date=2026-01-01", key_a)
        self.assertIn("effective_date=2026-02-01", key_b)

    def test_repeated_supplementary_group_key_splits_reference_metadata(self) -> None:
        row = {"document_id": "doc_2025", "source_record_id": "rule-7", "apba_id": "C0165", "profile_id": "public_portal-c0165"}
        text = "[location] employment-rule > addenda > transition [body] This transition applies to referenced articles."
        base_metadata = {
            "regulation_no": "employment-rule",
            "effective_date": "2026-01-01",
            "article_refs": ["article-1"],
        }
        chunk_a = {
            "chunk_id": "doc_2025_paragraph_001",
            "chunk_type": "paragraph",
            "paragraph_no": "1",
            "text": text,
            "metadata": dict(base_metadata),
        }
        chunk_b = {
            **chunk_a,
            "metadata": {
                **base_metadata,
                "article_refs": ["article-2"],
            },
        }
        flags = ["supplementary_or_effective_date_candidate"]

        key_a = review_group_key(row, chunk_a, chunk_a["metadata"], flags)
        key_b = review_group_key(row, chunk_b, chunk_b["metadata"], flags)

        self.assertNotEqual(key_a, key_b)
        self.assertIn("article_refs=article-1", key_a)
        self.assertIn("article_refs=article-2", key_b)

    def test_review_queue_sorts_by_severity_within_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_path = self._write_batch_report(root)
            self._write_chunks(
                root,
                "doc_hwp",
                [
                    {
                        "chunk_id": "c_table",
                        "chunk_type": "table",
                        "text": "table text",
                        "metadata": {
                            "table_like": True,
                            "table_review_required": True,
                            "table_review_flags": ["low_structured_row_count"],
                        },
                    },
                    {
                        "chunk_id": "c_encoding",
                        "chunk_type": "article",
                        "text": "?꾩튂 蹂몃Ц 媛쒖젙 議곌굔 蹂몃Ц ?꾨룄",
                        "metadata": {"article_no": "article-1", "article_title": "Broken"},
                    },
                ],
            )
            self._write_chunks(root, "doc_hwpx", [])

            payload = build_parsing_automation_payload(
                root,
                [batch_path],
                Path("reports"),
                generated_at="20260708-120000",
            )

            self.assertEqual("c_encoding", payload["review_queue"][0]["chunk_id"])
            self.assertEqual("ocr_or_encoding_blocker", payload["review_queue"][0]["review_category"])
            self.assertEqual(0, payload["review_queue"][0]["review_severity_rank"])
            self.assertEqual("c_table", payload["review_queue"][1]["chunk_id"])
            self.assertEqual("table_structure_review", payload["review_queue"][1]["review_category"])

    def test_write_parsing_automation_report_outputs_review_queue_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_path = self._write_batch_report(root)
            self._write_chunks(
                root,
                "doc_hwp",
                [
                    {
                        "chunk_id": "c2",
                        "chunk_type": "table",
                        "text": "table text",
                        "metadata": {
                            "table_like": True,
                            "table_review_required": True,
                            "table_review_flags": ["low_structured_row_count"],
                        },
                    },
                ],
            )
            self._write_chunks(root, "doc_hwpx", [])

            outputs = write_parsing_automation_report(
                root,
                [batch_path],
                Path("reports"),
                timestamp="20260708-120000",
            )

            csv_text = outputs["review_csv"].read_text(encoding="utf-8-sig")
            json_exists = outputs["json"].exists()
            markdown_exists = outputs["markdown"].exists()
            review_csv_exists = outputs["review_csv"].exists()

        self.assertTrue(json_exists)
        self.assertTrue(markdown_exists)
        self.assertTrue(review_csv_exists)
        self.assertIn("institution_name,apba_id,profile_id,source_record_id,source_file_id,document_id", csv_text)
        self.assertIn("table_review_flags", csv_text)
        self.assertIn("low_structured_row_count", csv_text)
        self.assertIn("blocking_review,2,table_structure_review,review_before_citation_grade_use", csv_text)

    def test_goldset_template_is_manual_worksheet_not_accuracy_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_path = self._write_batch_report(root)
            self._write_chunks(
                root,
                "doc_hwp",
                [
                    {
                        "chunk_id": "c1",
                        "chunk_type": "article",
                        "text": "Article text",
                        "metadata": {"article_no": "제1조", "article_title": "Purpose"},
                    }
                ],
            )
            self._write_chunks(
                root,
                "doc_hwpx",
                [
                    {
                        "chunk_id": "c2",
                        "chunk_type": "appendix",
                        "text": "appendix text",
                        "metadata": {"appendix_refs": ["별표1"]},
                    }
                ],
            )

            markdown = make_goldset_markdown(
                root,
                [batch_path],
                Path("reports"),
                size=2,
                generated_at="20260708-120000",
            )

            self.assertIn("Manual counts to fill", markdown)
            self.assertIn("Article precision", markdown)
            self.assertIn("TBD", markdown)
            self.assertIn("Until then, this file is a review worksheet", markdown)
            self.assertIn("Pipeline article candidates", markdown)
            self.assertIn("Pipeline appendix/form candidates", markdown)
            self.assertIn("data\\repository\\doc_hwp_chunks.json", markdown)
            self.assertNotIn(str(root), markdown)

    def test_goldset_template_writes_scoreable_label_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_path = self._write_batch_report(root)
            self._write_chunks(
                root,
                "doc_hwp",
                [
                    {
                        "chunk_id": "c1",
                        "chunk_type": "article",
                        "text": "Article text",
                        "metadata": {"article_no": "article-1", "article_title": "Purpose"},
                    },
                    {
                        "chunk_id": "c2",
                        "chunk_type": "table",
                        "text": "table text",
                        "source_page_start": 1,
                        "metadata": {
                            "table_like": True,
                            "table_cell_rows": [{"cells": ["A", "B"]}],
                            "table_citation_label": "Table 1",
                        },
                    },
                ],
            )
            self._write_chunks(root, "doc_hwpx", [])

            outputs = write_goldset_report(
                root,
                [batch_path],
                Path("reports"),
                timestamp="20260708-template",
                size=2,
            )
            labels = outputs["labels_csv"].read_text(encoding="utf-8-sig")
            markdown_exists = outputs["markdown"].is_file()

        self.assertTrue(markdown_exists)
        self.assertIn("label_status", labels)
        self.assertIn("pending_human_review", labels)
        self.assertIn("pipeline_article_count", labels)
        self.assertIn("manual_article_count", labels)
        self.assertIn("matched_article_count", labels)
        self.assertIn("pipeline_nested_table_count", labels)
        self.assertIn("manual_nested_table_count", labels)
        self.assertIn("matched_nested_table_count", labels)
        self.assertIn("doc_hwp", labels)
        self.assertIn("data\\repository\\doc_hwp_chunks.json", labels)

    def test_goldset_review_packets_write_per_document_reviewer_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_path = self._write_batch_report(root)
            self._write_chunks(
                root,
                "doc_hwp",
                [
                    {
                        "chunk_id": "article_1",
                        "chunk_type": "article",
                        "text": "Article text",
                        "metadata": {"article_no": "article-1", "article_title": "Purpose"},
                    }
                ],
            )
            self._write_chunks(
                root,
                "doc_hwpx",
                [
                    {
                        "chunk_id": "nested_table_1",
                        "chunk_type": "table",
                        "text": "Nested table evidence",
                        "metadata": {
                            "table_like": True,
                            "table_review_required": True,
                            "table_review_flags": ["merged_cell"],
                            "table_classification": "structured_table",
                            "table_structured_row_count": 3,
                            "table_column_count": 2,
                            "table_citation_label": "Appendix 1",
                            "source_hwpx_parser_review_flags": ["nested_table"],
                            "source_hwpx_nested_table_count": 1,
                            "source_hwpx_nested_table_text_snippets": ["Nested Cell"],
                            "source_hwpx_xml_block_indices": [42],
                        },
                    }
                ],
            )

            labels = write_goldset_report(
                root,
                [batch_path],
                Path("reports"),
                timestamp="20260708-template",
                size=2,
            )["labels_csv"]
            outputs = write_goldset_review_packets(root, labels, Path("reports/packets"))
            index = outputs["index"].read_text(encoding="utf-8")
            packet_paths = [path for path in outputs["packet_dir"].glob("*.md") if path.name != "README.md"]
            packet_text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(packet_paths))

        self.assertIn("Packet count: 2", index)
        self.assertIn("reports\\packets", index)
        self.assertNotIn(str(root), index)
        self.assertEqual(2, len(packet_paths))
        self.assertIn("Do not use pipeline counts as the manual answer", packet_text)
        self.assertIn("manual_article_count", packet_text)
        self.assertIn("matched_nested_table_count", packet_text)
        self.assertIn("Nested-table candidate count: 1", packet_text)
        self.assertIn("Pipeline table candidates", packet_text)
        self.assertIn("nested_table_1", packet_text)
        self.assertIn("label=Appendix 1", packet_text)
        self.assertIn("table_review_required=True", packet_text)
        self.assertIn("flags=merged_cell", packet_text)
        self.assertIn("parser_flags=nested_table", packet_text)
        self.assertIn("rows=3", packet_text)
        self.assertIn("cols=2", packet_text)
        self.assertIn("data\\repository\\doc_hwpx_chunks.json", packet_text)
        self.assertNotIn(str(root), packet_text)

    def test_goldset_selection_prefers_institution_diversity_within_format(self) -> None:
        rows = [
            self._goldset_row("doc_a1", "same_high_1.hwp", "기관A", 50),
            self._goldset_row("doc_a2", "same_high_2.hwp", "기관A", 49),
            self._goldset_row("doc_b1", "other_low.hwp", "기관B", 1),
            self._goldset_row("doc_c1", "third.hwpx", "기관C", 1),
            self._goldset_row("doc_d1", "fourth.hwpx", "기관D", 1),
            self._goldset_row("doc_e1", "fifth.hwpx", "기관E", 1),
            self._goldset_row("doc_f1", "sixth.pdf", "기관F", 1),
        ]

        selected, _ = select_goldset_rows(rows, 6)
        selected_hwp_institutions = [
            row["institution_name"]
            for row in selected
            if row["filename"].endswith(".hwp")
        ]

        self.assertIn("기관B", selected_hwp_institutions)

    def test_goldset_score_computes_precision_recall_from_manual_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_path = self._write_batch_report(root)
            self._write_chunks(
                root,
                "doc_hwp",
                [
                    {
                        "chunk_id": "c1",
                        "chunk_type": "article",
                        "text": "Article text",
                        "metadata": {"article_no": "article-1", "article_title": "Purpose"},
                    },
                    {
                        "chunk_id": "c2",
                        "chunk_type": "table",
                        "text": "table text",
                        "source_page_start": 1,
                        "metadata": {
                            "table_like": True,
                            "table_cell_rows": [{"cells": ["A", "B"]}],
                            "table_citation_label": "Table 1",
                        },
                    },
                ],
            )
            self._write_chunks(
                root,
                "doc_hwpx",
                [
                    {
                        "chunk_id": "c3",
                        "chunk_type": "appendix",
                        "text": "appendix text",
                        "metadata": {"appendix_refs": ["appendix-1"]},
                    }
                ],
            )

            payload = build_goldset_score_payload(
                root,
                [
                    self._goldset_label_row(
                        "doc_hwp",
                        manual_article_count=1,
                        matched_article_count=1,
                        manual_table_count=2,
                        matched_table_count=1,
                    ),
                    self._goldset_label_row(
                        "doc_hwpx",
                        manual_appendix_form_count=1,
                        matched_appendix_form_count=1,
                    ),
                ],
                [batch_path],
                Path("reports"),
                generated_at="20260709-score",
            )

        self.assertEqual("parsing_goldset_score", payload["report_type"])
        self.assertEqual(0, payload["summary"]["issue_count"])
        self.assertEqual(100.0, payload["by_structure"]["article"]["precision"])
        self.assertEqual(100.0, payload["by_structure"]["article"]["recall"])
        self.assertEqual(100.0, payload["by_structure"]["table"]["precision"])
        self.assertEqual(50.0, payload["by_structure"]["table"]["recall"])
        self.assertEqual(66.67, payload["by_structure"]["table"]["f1"])
        self.assertEqual(100.0, payload["overall"]["precision"])
        self.assertEqual(75.0, payload["overall"]["recall"])
        markdown = make_goldset_score_markdown(payload)
        self.assertIn("Precision/recall is computed only", markdown)
        self.assertIn("Ready for quality claim: True", markdown)
        self.assertIn("| table | 2 | 1 | 1 | 100.0 | 50.0 | 66.67 |", markdown)

    def test_goldset_score_triages_paragraph_scope_mismatch_from_structural_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_path = self._write_batch_report(root)
            self._write_chunks(
                root,
                "doc_hwp",
                [
                    {
                        "chunk_id": "article_5",
                        "chunk_type": "article",
                        "metadata": {
                            "article_no": "\uc81c5\uc870",
                            "paragraph_item_unit_count": 36,
                            "structural_child_count_source": "structure_detector",
                        },
                    }
                ],
            )
            self._write_chunks(root, "doc_hwpx", [])

            payload = build_goldset_score_payload(
                root,
                [
                    self._goldset_label_row(
                        "doc_hwp",
                        manual_article_count=1,
                        matched_article_count=1,
                        manual_paragraph_item_count=1,
                        matched_paragraph_item_count=1,
                    )
                ],
                [batch_path],
                Path("reports"),
                generated_at="20260709-score",
            )

        score = payload["documents"][0]["scores"]["paragraph_item"]
        self.assertEqual(36, score["pipeline_count"])
        self.assertEqual("structural_child_metadata", score["pipeline_count_source"])
        self.assertEqual("scope_mismatch_candidate", score["drift_triage"])
        self.assertEqual(36.0, score["pipeline_to_manual_ratio"])
        self.assertEqual(36, score["pipeline_count_breakdown"]["structural_article_body_paragraph_item_count"])
        markdown = make_goldset_score_markdown(payload)
        self.assertIn("Count source", markdown)
        self.assertIn("scope_mismatch_candidate", markdown)

    def test_goldset_score_blocks_silent_label_fallback_when_batch_document_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_path = self._write_batch_report(root)
            label_row = self._goldset_label_row(
                "doc_missing_from_batch",
                manual_article_count=1,
                pipeline_article_count=1,
                matched_article_count=1,
            )

            payload = build_goldset_score_payload(
                root,
                [label_row],
                [batch_path],
                Path("reports"),
                generated_at="20260709-score",
            )

        issue_codes = {issue["code"] for issue in payload["issues"]}
        self.assertIn("batch-report-document-not-found", issue_codes)
        self.assertFalse(payload["summary"]["ready_for_quality_claim"])
        article_score = payload["documents"][0]["scores"]["article"]
        self.assertIsNone(article_score["pipeline_count"])
        self.assertIsNone(article_score["matched_count"])
        self.assertEqual("batch_report_missing", article_score["pipeline_count_origin"])
        self.assertFalse(article_score["scorable"])

    def test_goldset_score_exposes_kordoc_breakdown_while_scoring_citation_ready_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_path = self._write_batch_report(root)
            self._write_chunks(
                root,
                "doc_hwp",
                [
                    {
                        "chunk_id": "inventory",
                        "chunk_type": "article",
                        "metadata": {
                            "document_inventory": {
                                "source": "hwp",
                                "tables": {"total": 4, "nested": 1},
                                "hierarchy": {"articles": 1},
                            }
                        },
                    },
                    {
                        "chunk_id": "kordoc_table_1",
                        "chunk_type": "table",
                        "source_page_start": 1,
                        "metadata": {
                            "table_like": True,
                            "table_cell_rows": [{"cells": ["A", "B"]}],
                            "table_citation_label": "Table 1",
                            "kordoc_table_promoted": True,
                            "kordoc_table_match": {"table_index": 1},
                        },
                    },
                ],
            )
            self._write_chunks(root, "doc_hwpx", [])

            payload = build_goldset_score_payload(
                root,
                [
                    self._goldset_label_row(
                        "doc_hwp",
                        manual_article_count=1,
                        matched_article_count=1,
                        manual_table_count=1,
                        matched_table_count=1,
                        manual_nested_table_count=0,
                        matched_nested_table_count=0,
                    )
                ],
                [batch_path],
                Path("reports"),
                generated_at="20260709-score",
            )

        table_score = payload["documents"][0]["scores"]["table"]
        nested_score = payload["documents"][0]["scores"]["nested_table"]
        self.assertEqual(1, table_score["pipeline_count"])
        self.assertEqual("citation_ready", table_score["pipeline_count_source"])
        self.assertEqual(1, table_score["pipeline_count_breakdown"]["table_goldset_preserved_count"])
        self.assertEqual(1, table_score["pipeline_count_breakdown"]["table_citation_ready_chunk_count"])
        self.assertEqual(1, table_score["pipeline_count_breakdown"]["kordoc_promoted_table_unit_count"])
        self.assertEqual(4, table_score["pipeline_count_breakdown"]["hwp_inventory_table_like_chunk_count"])
        self.assertEqual("hwp_inventory", nested_score["pipeline_count_source"])
        self.assertEqual("nested_inventory_review_candidate", nested_score["drift_triage"])

    def test_goldset_score_matches_latest_batch_by_filename_when_document_id_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_path = self._write_batch_report(root)
            self._write_chunks(
                root,
                "doc_hwp",
                [
                    {"chunk_id": "a1", "chunk_type": "article", "metadata": {"article_no": "\uc81c1\uc870"}},
                    {"chunk_id": "a2", "chunk_type": "article", "metadata": {"article_no": "\uc81c2\uc870"}},
                ],
            )
            self._write_chunks(root, "doc_hwpx", [])

            payload = build_goldset_score_payload(
                root,
                [
                    {
                        "document_id": "doc_old_label",
                        "filename": "a.hwp",
                        "label_status": "reviewed",
                        "reviewer": "tester",
                        "reviewed_at": "2026-07-12",
                        "manual_article_count": "2",
                        "pipeline_article_count": "1",
                        "matched_article_count": "1",
                        "manual_paragraph_item_count": "0",
                        "matched_paragraph_item_count": "0",
                        "manual_appendix_form_count": "0",
                        "matched_appendix_form_count": "0",
                        "manual_table_count": "0",
                        "matched_table_count": "0",
                        "manual_nested_table_count": "0",
                        "matched_nested_table_count": "0",
                        "manual_supplementary_effective_date_count": "0",
                        "matched_supplementary_effective_date_count": "0",
                        "manual_footnote_caption_count": "0",
                        "matched_footnote_caption_count": "0",
                    }
                ],
                [batch_path],
                Path("reports"),
                generated_at="20260709-score",
            )

        document = payload["documents"][0]
        article_score = document["scores"]["article"]
        self.assertEqual("doc_hwp", document["pipeline_document_id"])
        self.assertEqual("filename:a.hwp", document["pipeline_match_key"])
        self.assertEqual(2, article_score["pipeline_count"])
        self.assertEqual("pipeline_summary", article_score["pipeline_count_source"])
        markdown = make_goldset_score_markdown(payload)
        self.assertIn("Pipeline document ID: doc_hwp", markdown)
        self.assertIn("Pipeline match key: filename:a.hwp", markdown)
        self.assertIn("pipeline-count-stale-after-reprocess", {issue["code"] for issue in payload["issues"]})

    def test_goldset_score_marks_upward_paragraph_pipeline_drift_stale_after_reprocess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_path = self._write_batch_report(root)
            self._write_chunks(
                root,
                "doc_hwp",
                [
                    {
                        "chunk_id": "article_1",
                        "chunk_type": "article",
                        "metadata": {
                            "article_no": "\uc81c1\uc870",
                            "paragraph_item_unit_count": 238,
                            "structural_child_count_source": "structure_detector",
                        },
                    }
                ],
            )
            self._write_chunks(root, "doc_hwpx", [])

            payload = build_goldset_score_payload(
                root,
                [
                    self._goldset_label_row(
                        "doc_hwp",
                        pipeline_paragraph_item_count=1,
                        manual_paragraph_item_count=1,
                        matched_paragraph_item_count=1,
                    )
                ],
                [batch_path],
                Path("reports"),
                generated_at="20260709-score",
            )

        paragraph_score = payload["documents"][0]["scores"]["paragraph_item"]
        paragraph_issues = [
            issue for issue in payload["issues"] if issue["structure_type"] == "paragraph_item"
        ]
        self.assertEqual(238, paragraph_score["pipeline_count"])
        self.assertEqual(1, paragraph_score["label_pipeline_count"])
        self.assertFalse(paragraph_score["scorable"])
        self.assertEqual(["pipeline-count-stale-after-reprocess"], [issue["code"] for issue in paragraph_issues])

    def test_goldset_score_marks_upward_footnote_pipeline_drift_stale_after_reprocess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_path = self._write_batch_report(root)
            self._write_chunks(
                root,
                "doc_hwp",
                [
                    {
                        "chunk_id": "article_footnote",
                        "chunk_type": "article",
                        "text": "\ubcf8\ubb38",
                        "metadata": {
                            "article_no": "\uc81c1\uc870",
                            "footnote_marker_reference_count": 15,
                            "footnote_marker_references": [f"marker-{index}" for index in range(15)],
                        },
                    }
                ],
            )
            self._write_chunks(root, "doc_hwpx", [])

            payload = build_goldset_score_payload(
                root,
                [
                    self._goldset_label_row(
                        "doc_hwp",
                        pipeline_footnote_caption_count=0,
                        manual_footnote_caption_count=15,
                        matched_footnote_caption_count=0,
                    )
                ],
                [batch_path],
                Path("reports"),
                generated_at="20260709-score",
            )

        footnote_score = payload["documents"][0]["scores"]["footnote_caption"]
        footnote_issues = [
            issue for issue in payload["issues"] if issue["structure_type"] == "footnote_caption"
        ]
        self.assertEqual(15, footnote_score["pipeline_count"])
        self.assertEqual(0, footnote_score["label_pipeline_count"])
        self.assertFalse(footnote_score["scorable"])
        self.assertEqual(["pipeline-count-stale-after-reprocess"], [issue["code"] for issue in footnote_issues])

    def test_refresh_goldset_labels_updates_pipeline_counts_and_clears_stale_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_path = self._write_batch_report(root)
            self._write_chunks(
                root,
                "doc_hwp",
                [
                    {
                        "chunk_id": "article_1",
                        "chunk_type": "article",
                        "metadata": {
                            "article_no": "제1조",
                            "paragraph_item_unit_count": 2,
                            "structural_child_count_source": "structure_detector",
                        },
                    },
                    {
                        "chunk_id": "table_1",
                        "chunk_type": "table",
                        "source_page_start": 1,
                        "metadata": {
                            "table_like": True,
                            "table_cell_rows": [{"cells": ["A", "B"]}],
                            "table_citation_label": "Table 1",
                            "kordoc_table_promoted": True,
                            "kordoc_table_match": {"table_index": 1},
                        },
                    },
                ],
            )
            self._write_chunks(root, "doc_hwpx", [])

            rows = refresh_goldset_label_rows(
                root,
                [
                    self._goldset_label_row(
                        "doc_hwp",
                        pipeline_paragraph_item_count=1,
                        manual_paragraph_item_count=2,
                        matched_paragraph_item_count=1,
                        pipeline_table_count=1,
                        manual_table_count=1,
                        matched_table_count=1,
                    )
                ],
                [batch_path],
                Path("reports"),
            )

        row = rows[0]
        self.assertEqual("pending_human_review", row["label_status"])
        self.assertEqual(2, row["pipeline_paragraph_item_count"])
        self.assertEqual("", row["matched_paragraph_item_count"])
        self.assertEqual(1, row["pipeline_table_count"])
        self.assertEqual(1, row["matched_table_count"])
        self.assertEqual("doc_hwp", row["current_runtime_document_id"])
        self.assertIn("paragraph_item", row["parser_miss_false_positive_notes"])

    def test_write_refreshed_goldset_labels_requires_re_review_for_changed_structures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_path = self._write_batch_report(root)
            self._write_chunks(
                root,
                "doc_hwp",
                [
                    {
                        "chunk_id": "article_1",
                        "chunk_type": "article",
                        "metadata": {
                            "article_no": "제1조",
                            "footnote_marker_reference_count": 2,
                        },
                    }
                ],
            )
            self._write_chunks(root, "doc_hwpx", [])
            labels_path = root / "labels.csv"
            write_goldset_rows = [
                self._goldset_label_row(
                    "doc_hwp",
                    pipeline_footnote_caption_count=0,
                    manual_footnote_caption_count=2,
                    matched_footnote_caption_count=0,
                )
            ]
            from scripts.analyze_regulation_corpus import write_csv

            write_csv(labels_path, write_goldset_rows)
            out_path = root / "refreshed.csv"

            outputs = write_refreshed_goldset_labels(
                root,
                labels_path,
                [batch_path],
                Path("reports"),
                out_path,
            )

            text = outputs["labels_csv"].read_text(encoding="utf-8-sig")
            self.assertIn("pending_human_review", text)
            self.assertIn("pipeline refreshed; rerun matched-count review for: footnote_caption", text)

    def test_goldset_score_excludes_manual_non_article_scope_from_quality_claim(self) -> None:
        payload = build_goldset_score_payload(
            Path("workspace"),
            [
                self._goldset_label_row(
                    "doc_regulation",
                    pipeline_article_count=1,
                    manual_article_count=1,
                    matched_article_count=1,
                    pipeline_paragraph_item_count=0,
                    pipeline_appendix_form_count=0,
                    pipeline_table_count=0,
                    pipeline_nested_table_count=0,
                    pipeline_supplementary_effective_date_count=0,
                    pipeline_footnote_caption_count=0,
                ),
                {
                    "document_id": "doc_handbook",
                    "label_status": "reviewed",
                    "reviewer": "tester",
                    "reviewed_at": "2026-07-11",
                    "manual_article_count": "0",
                    "matched_article_count": "0",
                    "pipeline_article_count": "2",
                    "manual_paragraph_item_count": "0",
                    "matched_paragraph_item_count": "0",
                    "pipeline_paragraph_item_count": "10",
                    "manual_appendix_form_count": "0",
                    "matched_appendix_form_count": "0",
                    "pipeline_appendix_form_count": "1",
                    "manual_table_count": "0",
                    "matched_table_count": "0",
                    "pipeline_table_count": "5",
                    "manual_nested_table_count": "0",
                    "matched_nested_table_count": "0",
                    "pipeline_nested_table_count": "1",
                    "manual_supplementary_effective_date_count": "0",
                    "matched_supplementary_effective_date_count": "0",
                    "pipeline_supplementary_effective_date_count": "1",
                    "manual_footnote_caption_count": "0",
                    "matched_footnote_caption_count": "0",
                    "pipeline_footnote_caption_count": "0",
                    "table_preservation_notes": "\uc870\ubb38\ud615\ud0dc \uc544\ub2d8",
                },
            ],
            [],
            Path("reports"),
            generated_at="20260709-score",
        )

        self.assertEqual(2, payload["summary"]["document_count"])
        self.assertEqual(1, payload["summary"]["scored_document_count"])
        self.assertEqual(1, payload["summary"]["excluded_document_count"])
        self.assertEqual(7, payload["completion"]["expected_structure_score_count"])
        self.assertEqual(100.0, payload["overall"]["f1"])
        excluded = payload["documents"][1]
        self.assertTrue(excluded["excluded_from_quality_claim"])
        self.assertEqual("manual_non_article_form", excluded["score_scope"])
        self.assertTrue(excluded["scores"]["table"]["excluded_from_quality_claim"])
        markdown = make_goldset_score_markdown(payload)
        self.assertIn("Excluded non-article documents: 1", markdown)
        self.assertIn("Score scope: manual_non_article_form", markdown)

    def test_goldset_score_all_excluded_documents_is_not_ready_for_quality_claim(self) -> None:
        payload = build_goldset_score_payload(
            Path("workspace"),
            [
                {
                    "document_id": "doc_handbook",
                    "label_status": "reviewed",
                    "reviewer": "tester",
                    "reviewed_at": "2026-07-11",
                    "manual_article_count": "0",
                    "matched_article_count": "0",
                    "pipeline_article_count": "2",
                    "manual_paragraph_item_count": "0",
                    "matched_paragraph_item_count": "0",
                    "pipeline_paragraph_item_count": "10",
                    "manual_appendix_form_count": "0",
                    "matched_appendix_form_count": "0",
                    "pipeline_appendix_form_count": "1",
                    "manual_table_count": "0",
                    "matched_table_count": "0",
                    "pipeline_table_count": "5",
                    "manual_nested_table_count": "0",
                    "matched_nested_table_count": "0",
                    "pipeline_nested_table_count": "1",
                    "manual_supplementary_effective_date_count": "0",
                    "matched_supplementary_effective_date_count": "0",
                    "pipeline_supplementary_effective_date_count": "1",
                    "manual_footnote_caption_count": "0",
                    "matched_footnote_caption_count": "0",
                    "pipeline_footnote_caption_count": "0",
                    "table_preservation_notes": "not article",
                }
            ],
            [],
            Path("reports"),
            generated_at="20260709-score",
        )

        self.assertEqual(0, payload["summary"]["issue_count"])
        self.assertEqual(0, payload["summary"]["scored_document_count"])
        self.assertEqual(1, payload["summary"]["excluded_document_count"])
        self.assertEqual(0, payload["completion"]["expected_structure_score_count"])
        self.assertFalse(payload["summary"]["ready_for_quality_claim"])

    def test_goldset_score_reports_missing_match_counts(self) -> None:
        payload = build_goldset_score_payload(
            Path("workspace"),
            [{"document_id": "doc_manual", "manual_article_count": "3", "pipeline_article_count": "3"}],
            [],
            Path("reports"),
            generated_at="20260709-score",
        )

        issue_codes = {issue["code"] for issue in payload["issues"]}
        self.assertIn("matched-count-missing", issue_codes)
        self.assertGreater(payload["summary"]["issue_count"], 0)
        self.assertEqual(0, payload["by_structure"]["article"]["scorable_count"])
        self.assertFalse(payload["summary"]["ready_for_quality_claim"])

    def test_goldset_score_derives_zero_match_when_manual_or_pipeline_is_zero(self) -> None:
        payload = build_goldset_score_payload(
            Path("workspace"),
            [
                self._goldset_label_row(
                    "doc_manual",
                    manual_article_count=0,
                    pipeline_article_count=3,
                    matched_article_count=None,
                    manual_table_count=2,
                    pipeline_table_count=0,
                    matched_table_count=None,
                )
            ],
            [],
            Path("reports"),
            generated_at="20260709-score",
        )

        article_score = payload["documents"][0]["scores"]["article"]
        table_score = payload["documents"][0]["scores"]["table"]
        issue_keys = {
            (issue.get("structure_type"), issue.get("code"))
            for issue in payload["issues"]
        }
        self.assertEqual(0, article_score["matched_count"])
        self.assertEqual("derived_zero_bound", article_score["matched_count_source"])
        self.assertEqual(0, table_score["matched_count"])
        self.assertEqual("derived_zero_bound", table_score["matched_count_source"])
        self.assertNotIn(("article", "matched-count-missing"), issue_keys)
        self.assertNotIn(("table", "matched-count-missing"), issue_keys)

    def test_goldset_score_rejects_invalid_counts_from_metrics(self) -> None:
        payload = build_goldset_score_payload(
            Path("workspace"),
            [
                self._goldset_label_row(
                    "doc_manual",
                    pipeline_article_count=1,
                    manual_article_count=1,
                    matched_article_count=2,
                    pipeline_paragraph_item_count=0,
                    pipeline_appendix_form_count=0,
                    pipeline_table_count=1,
                    manual_table_count=-1,
                    matched_table_count=0,
                    pipeline_nested_table_count=0,
                    pipeline_supplementary_effective_date_count=0,
                    pipeline_footnote_caption_count=0,
                )
            ],
            [],
            Path("reports"),
            generated_at="20260709-score",
        )

        issue_codes = {issue["code"] for issue in payload["issues"]}
        self.assertIn("matched-count-exceeds-bound", issue_codes)
        self.assertIn("count-negative", issue_codes)
        self.assertFalse(payload["documents"][0]["scores"]["article"]["scorable"])
        self.assertFalse(payload["documents"][0]["scores"]["table"]["scorable"])
        self.assertEqual(0, payload["by_structure"]["article"]["scorable_count"])
        self.assertEqual(0, payload["by_structure"]["table"]["scorable_count"])
        self.assertFalse(payload["completion"]["ready_for_quality_claim"])

    def test_goldset_score_uses_label_chunk_artifact_without_batch_report(self) -> None:
        payload = build_goldset_score_payload(
            Path("workspace"),
            [
                {
                    "document_id": "doc_manual",
                    "filename": "manual.pdf",
                    "chunk_artifact": "data/current/doc_manual_chunks.json",
                    "manual_article_count": "1",
                    "pipeline_article_count": "1",
                    "matched_article_count": "1",
                }
            ],
            [],
            Path("reports"),
            generated_at="20260709-score",
        )
        markdown = make_goldset_score_markdown(payload)

        self.assertEqual("data/current/doc_manual_chunks.json", payload["documents"][0]["chunk_artifact"])
        self.assertIn("data/current/doc_manual_chunks.json", markdown)

    def test_goldset_score_requires_completed_human_review_metadata(self) -> None:
        payload = build_goldset_score_payload(
            Path("workspace"),
            [
                self._goldset_label_row(
                    "doc_manual",
                    label_status="pending_human_review",
                    reviewer="",
                    reviewed_at="",
                    manual_article_count=1,
                    pipeline_article_count=1,
                    matched_article_count=1,
                )
            ],
            [],
            Path("reports"),
            generated_at="20260709-score",
        )

        issue_codes = {issue["code"] for issue in payload["issues"]}
        self.assertIn("label-status-not-complete", issue_codes)
        self.assertFalse(payload["completion"]["ready_for_quality_claim"])
        self.assertEqual(0, payload["completion"]["completed_document_count"])
        self.assertFalse(payload["documents"][0]["scores"]["article"]["scorable"])
        self.assertEqual("label_review_incomplete", payload["documents"][0]["scores"]["article"]["drift_triage"])

    def test_goldset_score_requires_reviewed_at_for_completed_label(self) -> None:
        payload = build_goldset_score_payload(
            Path("workspace"),
            [
                self._goldset_label_row(
                    "doc_manual",
                    label_status="reviewed",
                    reviewer="tester",
                    reviewed_at="",
                    manual_article_count=1,
                    pipeline_article_count=1,
                    matched_article_count=1,
                )
            ],
            [],
            Path("reports"),
            generated_at="20260709-score",
        )

        issue_codes = {issue["code"] for issue in payload["issues"]}
        self.assertIn("reviewed-at-missing", issue_codes)
        self.assertFalse(payload["completion"]["ready_for_quality_claim"])
        self.assertFalse(payload["documents"][0]["scores"]["article"]["scorable"])
        self.assertEqual("label_review_incomplete", payload["documents"][0]["scores"]["article"]["drift_triage"])
        self.assertEqual(0, payload["by_structure"]["article"]["scorable_count"])

    def test_goldset_score_tracks_nested_table_as_scoreable_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_path = self._write_batch_report(root)
            self._write_chunks(
                root,
                "doc_hwpx",
                [
                    {
                        "chunk_id": "nested_parent",
                        "chunk_type": "table",
                        "text": "Nested table evidence",
                        "metadata": {
                            "source_hwpx_parser_review_flags": ["nested_table"],
                            "source_hwpx_nested_table_count": 1,
                            "source_hwpx_nested_table_text_snippets": ["Nested Cell"],
                            "source_hwpx_xml_block_indices": [42],
                        },
                    },
                    {
                        "chunk_id": "nested_child_duplicate",
                        "chunk_type": "table",
                        "text": "Nested table evidence duplicate",
                        "metadata": {
                            "source_hwpx_parser_review_flags": ["nested_table"],
                            "source_hwpx_nested_table_count": 1,
                            "source_hwpx_nested_table_text_snippets": ["Nested Cell"],
                            "source_hwpx_xml_block_indices": [42],
                        },
                    },
                ],
            )
            self._write_chunks(root, "doc_hwp", [])

            payload = build_goldset_score_payload(
                root,
                [
                    self._goldset_label_row(
                        "doc_hwpx",
                        manual_nested_table_count=1,
                        matched_nested_table_count=1,
                    )
                ],
                [batch_path],
                Path("reports"),
                generated_at="20260709-score",
            )

        nested = payload["by_structure"]["nested_table"]
        self.assertEqual(7, payload["summary"]["structure_type_count"])
        self.assertEqual(1, nested["pipeline_total"])
        self.assertEqual(1, nested["manual_total"])
        self.assertEqual(100.0, nested["precision"])
        self.assertEqual(100.0, nested["recall"])

    def test_write_goldset_score_report_outputs_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels = root / "labels.csv"
            labels.write_text(
                "\n".join(
                    [
                        "document_id,manual_article_count,pipeline_article_count,matched_article_count,manual_table_count,pipeline_table_count,matched_table_count",
                        "doc_manual,2,3,2,1,1,1",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            outputs = write_goldset_score_report(
                root,
                labels,
                [],
                Path("reports"),
                timestamp="20260709-score",
            )

            payload = json.loads(outputs["json"].read_text(encoding="utf-8"))
            markdown = outputs["markdown"].read_text(encoding="utf-8")

        self.assertEqual("parsing_goldset_score", payload["report_type"])
        self.assertIsNotNone(datetime.fromisoformat(payload["generated_at"]).tzinfo)
        self.assertIn("Parsing Goldset Score", markdown)

    def test_write_goldset_score_report_refreshes_labels_before_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "reports" / "overnight_runs" / "run-refresh-score" / "runtime"
            repository = runtime / "repository"
            exports = runtime / "exports"
            repository.mkdir(parents=True)
            exports.mkdir(parents=True)
            document_id = "doc_hwp"
            (repository / f"{document_id}_chunks.json").write_text(
                json.dumps(
                    [
                        {
                            "chunk_id": "article_1",
                            "chunk_type": "article",
                            "metadata": {
                                "article_no": "\uc81c1\uc870",
                                "paragraph_item_unit_count": 238,
                                "structural_child_count_source": "structure_detector",
                            },
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            quality_path = exports / f"{document_id}.quality.json"
            quality_path.write_text("{}", encoding="utf-8")
            batch_path = self._write_single_row_batch(
                root,
                {
                    "document_id": document_id,
                    "filename": "refresh-score.hwp",
                    "status": "completed",
                    "quality_json": str(quality_path.relative_to(root)),
                },
                "refresh-score",
            )
            labels = root / "labels.csv"
            label_row = self._goldset_label_row(
                document_id,
                pipeline_article_count=0,
                manual_article_count=0,
                matched_article_count=0,
                pipeline_paragraph_item_count=1,
                manual_paragraph_item_count=2,
                matched_paragraph_item_count=1,
                pipeline_appendix_form_count=0,
                manual_appendix_form_count=0,
                matched_appendix_form_count=0,
                pipeline_table_count=0,
                manual_table_count=0,
                matched_table_count=0,
                pipeline_nested_table_count=0,
                manual_nested_table_count=0,
                matched_nested_table_count=0,
                pipeline_supplementary_effective_date_count=0,
                manual_supplementary_effective_date_count=0,
                matched_supplementary_effective_date_count=0,
                pipeline_footnote_caption_count=0,
                manual_footnote_caption_count=0,
                matched_footnote_caption_count=0,
            )
            with labels.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(label_row.keys()))
                writer.writeheader()
                writer.writerow(label_row)

            outputs = write_goldset_score_report(
                root,
                labels,
                [batch_path],
                Path("reports"),
                out_json=Path("reports/goldset_score_refresh.json"),
                out_md=Path("reports/goldset_score_refresh.md"),
                refresh_labels_out_csv=Path("reports/refreshed_labels.csv"),
                timestamp="20260709-score-refresh",
            )

            refreshed_rows = load_goldset_label_rows(root / "reports" / "refreshed_labels.csv")
            payload = json.loads(outputs["json"].read_text(encoding="utf-8"))

        self.assertEqual(1, len(refreshed_rows))
        self.assertEqual("pending_human_review", refreshed_rows[0]["label_status"])
        self.assertEqual("238", refreshed_rows[0]["pipeline_paragraph_item_count"])
        self.assertEqual("", refreshed_rows[0]["matched_paragraph_item_count"])

        paragraph_score = payload["documents"][0]["scores"]["paragraph_item"]
        issue_codes = {issue["code"] for issue in payload["issues"]}
        self.assertEqual(238, paragraph_score["pipeline_count"])
        self.assertEqual(238, paragraph_score["label_pipeline_count"])
        self.assertIsNone(paragraph_score["matched_count"])
        self.assertFalse(paragraph_score["scorable"])
        self.assertFalse(paragraph_score["label_review_complete"])
        self.assertEqual("label_review_incomplete", paragraph_score["drift_triage"])
        self.assertIn("label-status-not-complete", issue_codes)
        self.assertIn("matched-count-missing", issue_codes)
        self.assertNotIn("pipeline-count-stale-after-reprocess", issue_codes)

    def test_location_prefix_does_not_create_supplementary_review_flag(self) -> None:
        chunk = {
            "chunk_id": "article_1",
            "chunk_type": "article",
            "text": "[위치] Example Regulation > 부칙 > 제1조\n[본문]\n제1조(목적) 이 규정은 목적을 정한다.",
            "normalized_text": "제1조(목적) 이 규정은 목적을 정한다.",
            "metadata": {
                "chunk_type": "article",
                "article_no": "제1조",
                "article_title": "목적",
            },
        }

        flags = chunk_review_flags(chunk)

        self.assertNotIn("supplementary_or_effective_date_candidate", flags)
        self.assertEqual("no_signal", review_priority_tier(chunk, flags))

    def test_article_appendix_reference_does_not_create_form_review_flag(self) -> None:
        chunk = {
            "chunk_id": "article_appendix_ref",
            "chunk_type": "article",
            "text": "\uc81c2\uc870(\uae30\uad6c) \ud558\ubd80\uae30\uad6c\ub294 \ubcc4\ud45c 1\uacfc \uac19\ub2e4.",
            "metadata": {
                "chunk_type": "article",
                "article_no": "\uc81c2\uc870",
                "article_title": "\uae30\uad6c",
                "appendix_refs": ["\ubcc4\ud45c1"],
            },
        }

        flags = chunk_review_flags(chunk)

        self.assertNotIn("form_or_appendix_candidate", flags)
        self.assertNotIn("appendix_form_review", flags)

    def test_table_appendix_reference_does_not_create_form_review_flag(self) -> None:
        chunk = {
            "chunk_id": "table_appendix_ref",
            "chunk_type": "table",
            "text": "표 1은 별표 1에 따른다.",
            "metadata": {
                "chunk_type": "table",
                "table_like": True,
            },
        }

        flags = chunk_review_flags(chunk)

        self.assertNotIn("form_or_appendix_candidate", flags)

    def test_paragraph_appendix_reference_does_not_create_form_review_flag(self) -> None:
        chunk = {
            "chunk_id": "paragraph_appendix_ref",
            "chunk_type": "paragraph",
            "text": "\uc774 \uaddc\uc815\uc740 \ubcc4\uc9c0 \uc81c1\ud638 \uc11c\uc2dd\uc5d0 \ub530\ub978\ub2e4.",
            "metadata": {"chunk_type": "paragraph"},
        }

        flags = chunk_review_flags(chunk)

        self.assertNotIn("form_or_appendix_candidate", flags)

    def test_paragraph_starting_with_attachment_heading_still_flags_form_candidate(self) -> None:
        chunk = {
            "chunk_id": "paragraph_attachment_heading",
            "chunk_type": "paragraph",
            "text": "[\ubcc4\uc9c0 \uc81c1\ud638 \uc11c\uc2dd] \uc2e0\uccad\uc11c",
            "metadata": {"chunk_type": "paragraph"},
        }

        flags = chunk_review_flags(chunk)

        self.assertIn("form_or_appendix_candidate", flags)

    def test_actual_appendix_chunk_still_creates_informational_form_signal(self) -> None:
        chunk = {
            "chunk_id": "appendix_1",
            "chunk_type": "appendix",
            "text": "[\ubcc4\ud45c 1] \uae30\uad6c\ud45c",
            "metadata": {"chunk_type": "appendix", "appendix_no": "\ubcc4\ud45c1"},
        }

        flags = chunk_review_flags(chunk)

        self.assertIn("form_or_appendix_candidate", flags)
        self.assertEqual("informational", review_priority_tier(chunk, flags))

    def test_title_only_supplementary_heading_is_not_review_candidate(self) -> None:
        chunk = {
            "chunk_id": "supplementary_heading",
            "chunk_type": "supplementary_provision",
            "text": "\ubd80 \uce59",
            "metadata": {"is_supplementary_provision": True},
        }

        flags = chunk_review_flags(chunk)

        self.assertNotIn("supplementary_or_effective_date_candidate", flags)
        self.assertEqual("no_signal", review_priority_tier(chunk, flags))

    def test_boilerplate_supplementary_without_overrides_is_not_domain_review(self) -> None:
        chunk = {
            "chunk_id": "supplementary_boilerplate",
            "chunk_type": "supplementary_provision",
            "text": "\ubd80 \uce59 <1987.03.28.> \uc774 \uaddc\uc815\uc740 1987\ub144 3\uc6d4 28\uc77c\ubd80\ud130 \uc2dc\ud589\ud55c\ub2e4.",
            "metadata": {
                "is_supplementary_provision": True,
                "supplementary_boilerplate": True,
                "effective_date": "1987-03-28",
                "article_effective_overrides": [],
            },
        }

        flags = chunk_review_flags(chunk)

        self.assertNotIn("supplementary_or_effective_date_candidate", flags)
        self.assertEqual("no_signal", review_priority_tier(chunk, flags))

    def test_supplementary_with_effective_override_stays_review_candidate(self) -> None:
        chunk = {
            "chunk_id": "supplementary_override",
            "chunk_type": "supplementary_provision",
            "text": "\ubd80\uce59 \uc81c5\uc870\ub294 2026\ub144 1\uc6d4 1\uc77c\ubd80\ud130 \uc801\uc6a9\ud55c\ub2e4.",
            "metadata": {
                "is_supplementary_provision": True,
                "supplementary_boilerplate": False,
                "article_effective_overrides": [
                    {"article_ref": "\uc81c5\uc870", "effective_date": "2026-01-01"}
                ],
            },
        }

        flags = chunk_review_flags(chunk)

        self.assertIn("supplementary_or_effective_date_candidate", flags)
        self.assertEqual("domain_attention", review_priority_tier(chunk, flags))

    def test_hwpx_source_metadata_creates_caption_review_flag(self) -> None:
        chunk = {
            "chunk_id": "article_1",
            "chunk_type": "article",
            "text": "제1조(목적) 본문",
            "metadata": {
                "article_no": "제1조",
                "article_title": "목적",
                "source_hwpx_block_types": ["paragraph", "footnote", "image"],
                "source_caption_count": 1,
            },
        }

        flags = chunk_review_flags(chunk)

        self.assertIn("footnote_or_caption_candidate", flags)
        self.assertEqual("domain_attention", review_priority_tier(chunk, flags))

    def test_footnote_links_metadata_creates_caption_review_flag(self) -> None:
        chunk = {
            "chunk_id": "article_footnote_links",
            "chunk_type": "article",
            "text": "Article body",
            "metadata": {
                "article_no": "Article 1",
                "article_title": "Purpose",
                "footnote_links": [{"marker": "1", "text": "linked footnote"}],
            },
        }

        flags = chunk_review_flags(chunk)

        self.assertIn("footnote_or_caption_candidate", flags)
        self.assertEqual("domain_attention", review_priority_tier(chunk, flags))

    def test_inline_star_marker_does_not_create_caption_review_flag(self) -> None:
        chunk = {
            "chunk_id": "table_1",
            "chunk_type": "table",
            "text": "| 구분 | * 비고 |\n| --- | --- |\n본문 설명 * 참고",
            "metadata": {"table_like": True},
        }

        flags = chunk_review_flags(chunk)

        self.assertNotIn("footnote_or_caption_candidate", flags)

    def test_source_caption_count_without_visible_marker_does_not_create_caption_review_flag(self) -> None:
        chunk = {
            "chunk_id": "appendix_part_2",
            "chunk_type": "appendix",
            "text": "1. 추정가격이 고시금액 미만인 소규모 계약",
            "metadata": {"source_caption_count": 1},
        }

        flags = chunk_review_flags(chunk)

        self.assertNotIn("footnote_or_caption_candidate", flags)

    def test_region_miju_inside_table_does_not_create_caption_review_flag(self) -> None:
        chunk = {
            "chunk_id": "table_region_miju",
            "chunk_type": "table",
            "text": (
                "| \uad6c\ubd84\uc9c0\uc5ed | \uac00 \uc9c0\uc5ed | \ub098 \uc9c0\uc5ed |\n"
                "| --- | --- | --- |\n"
                "| \ubbf8\uc8fc | \uacfc\ud14c\ub9d0\ub77c, \uc5d0\ucf70\ub3c4\ub974 | "
                "\ucf5c\ub86c\ube44\uc544, \ud398\ub8e8 |"
            ),
            "metadata": {"table_like": True},
        }

        flags = chunk_review_flags(chunk)

        self.assertNotIn("footnote_or_caption_candidate", flags)

    def test_explicit_miju_note_line_creates_caption_review_flag(self) -> None:
        chunk = {
            "chunk_id": "article_miju_note",
            "chunk_type": "article",
            "text": "Article body\n\ubbf8\uc8fc: \uc774 \uc904\uc740 \ubb38\uc11c \ub05d\uc8fc\uc11d\uc785\ub2c8\ub2e4.",
            "metadata": {"article_no": "Article 1", "article_title": "Purpose"},
        }

        flags = chunk_review_flags(chunk)

        self.assertIn("footnote_or_caption_candidate", flags)

    def test_general_note_line_does_not_create_caption_review_flag(self) -> None:
        chunk = {
            "chunk_id": "article_1",
            "chunk_type": "article",
            "text": "제1조(목적) 본문\n※ 붙임 자료는 별도 제출한다.",
            "metadata": {"article_no": "제1조", "article_title": "목적"},
        }

        flags = chunk_review_flags(chunk)

        self.assertNotIn("footnote_or_caption_candidate", flags)

    def test_source_caption_count_with_general_note_line_does_not_create_caption_review_flag(self) -> None:
        chunk = {
            "chunk_id": "article_note",
            "chunk_type": "article",
            "text": "제1조(목적) 본문\n※ 붙임 자료는 별도 제출한다.\n* 담당 부서는 확인한다.",
            "metadata": {"article_no": "제1조", "article_title": "목적", "source_caption_count": 2},
        }

        flags = chunk_review_flags(chunk)

        self.assertNotIn("footnote_or_caption_candidate", flags)

    def test_explicit_figure_caption_line_creates_caption_review_flag(self) -> None:
        chunk = {
            "chunk_id": "article_1",
            "chunk_type": "article",
            "text": "제1조(목적) 본문\n그림 1. 처리 흐름",
            "metadata": {"article_no": "제1조", "article_title": "목적"},
        }

        flags = chunk_review_flags(chunk)

        self.assertIn("footnote_or_caption_candidate", flags)

    def test_explicit_caption_line_variants_create_caption_review_flag(self) -> None:
        chunk = {
            "chunk_id": "article_1",
            "chunk_type": "article",
            "text": "제1조(목적) 본문\n표 1) 평가 기준\n그림 2: 처리 흐름",
            "metadata": {"article_no": "제1조", "article_title": "목적"},
        }

        flags = chunk_review_flags(chunk)

        self.assertIn("footnote_or_caption_candidate", flags)

    def test_bracketed_caption_line_variants_create_caption_review_flag(self) -> None:
        chunk = {
            "chunk_id": "article_1",
            "chunk_type": "article",
            "text": "제1조(목적) 본문\n[표 1] 평가 기준\n<그림 2> 처리 흐름",
            "metadata": {"article_no": "제1조", "article_title": "목적"},
        }

        flags = chunk_review_flags(chunk)

        self.assertIn("footnote_or_caption_candidate", flags)

    def test_hwpx_complex_source_metadata_creates_structure_review_flag(self) -> None:
        chunk = {
            "chunk_id": "table_1",
            "chunk_type": "table",
            "text": "Outer A | Nested A Nested B Figure Caption Cell Note",
            "metadata": {
                "source_hwpx_block_types": ["table"],
                "source_hwpx_parser_review_flags": ["table_caption", "nested_table", "merged_cell"],
                "source_hwpx_nested_table_count": 1,
                "source_hwpx_merged_cell_count": 2,
            },
        }

        flags = chunk_review_flags(chunk)

        self.assertIn("footnote_or_caption_candidate", flags)
        self.assertIn("hwpx_complex_structure_candidate", flags)
        self.assertEqual("domain_attention", review_priority_tier(chunk, flags))

    def test_review_queue_row_includes_hwpx_complex_evidence(self) -> None:
        chunk = {
            "chunk_id": "table_1",
            "chunk_type": "table",
            "text": "Header | Nested Cell Image Caption End Note Text",
            "metadata": {
                "source_hwpx_block_types": ["table"],
                "source_hwpx_parser_review_flags": ["table_caption", "nested_table", "table_image", "table_note"],
                "source_hwpx_nested_table_count": 1,
                "source_hwpx_table_image_count": 1,
                "source_hwpx_table_note_count": 1,
                "source_hwpx_table_direct_captions": ["Direct Table Caption"],
                "source_hwpx_table_image_captions": ["Image Caption"],
                "source_hwpx_table_note_snippets": ["End Note Text"],
                "source_hwpx_nested_table_text_snippets": ["Nested Cell"],
                "source_hwpx_xml_block_indices": [4],
            },
        }
        flags = chunk_review_flags(chunk)

        row = review_queue_row(
            Path("workspace"),
            {"document_id": "doc_hwpx", "filename": "sample.hwpx", "institution_name": "기관"},
            chunk,
            flags,
            Path("exports/doc_hwpx.jsonl"),
        )

        self.assertEqual(row["source_hwpx_table_direct_captions"], "Direct Table Caption")
        self.assertEqual(row["source_hwpx_table_image_captions"], "Image Caption")
        self.assertEqual(row["source_hwpx_table_note_snippets"], "End Note Text")
        self.assertEqual(row["source_hwpx_nested_table_text_snippets"], "Nested Cell")
        self.assertEqual(row["source_hwpx_xml_block_indices"], "4")

    def test_hwp_binary_table_metadata_creates_geometry_review_flag(self) -> None:
        chunk = {
            "chunk_id": "table_hwp",
            "chunk_type": "table",
            "text": "HWP table-like text",
            "metadata": {
                "table_like": True,
                "table_classification": "structured_table",
                "table_review_reason": "header_and_numeric_rows",
                "table_structured_row_count": 3,
                "table_record_count": 2,
                "table_header_cells": ["Grade", "Rate"],
                "source_hwp_extraction_modes": ["legacy_ole_para_text_only"],
                "source_hwp_streams": ["BodyText/Section0"],
                "source_hwp_section_indices": [1],
                "source_hwp_native_table_geometry": False,
            },
        }

        flags = chunk_review_flags(chunk)

        self.assertIn("hwp_binary_table_geometry_candidate", flags)
        self.assertEqual("domain_attention", review_priority_tier(chunk, flags))

    def test_hwp_binary_appendix_without_table_signal_does_not_create_geometry_review_flag(self) -> None:
        chunk = {
            "chunk_id": "appendix_hwp",
            "chunk_type": "appendix",
            "text": "Appendix prose without detected table rows.",
            "metadata": {
                "source_hwp_extraction_modes": ["legacy_ole_para_text_only"],
                "source_hwp_streams": ["BodyText/Section0"],
                "source_hwp_section_indices": [1],
                "source_hwp_native_table_geometry": False,
            },
        }

        flags = chunk_review_flags(chunk)

        self.assertIn("form_or_appendix_candidate", flags)
        self.assertNotIn("hwp_binary_table_geometry_candidate", flags)
        self.assertEqual("informational", review_priority_tier(chunk, flags))

    def test_high_parser_uncertainty_creates_blocking_review_flag(self) -> None:
        chunk = {
            "chunk_id": "ocr_required",
            "chunk_type": "article",
            "text": "\uc81c1\uc870(\ubaa9\uc801) OCR fallback text",
            "metadata": {
                "article_no": "\uc81c1\uc870",
                "article_title": "\ubaa9\uc801",
                "parser_uncertainty_source": "pdf",
                "parser_uncertainty_risk_level": "high",
                "parser_uncertainty_flags": ["ocr_required", "no_text_extracted"],
                "parser_uncertainty_recommendation": "run_ocr",
            },
        }

        flags = chunk_review_flags(chunk)
        row = review_queue_row(
            Path("workspace"),
            {"document_id": "doc_pdf", "filename": "sample.pdf", "institution_name": "\uae30\uad00"},
            chunk,
            flags,
            Path("exports/doc_pdf.jsonl"),
        )

        self.assertIn("parser_uncertainty_blocker", flags)
        self.assertEqual("blocking_review", review_priority_tier(chunk, flags))
        self.assertEqual("parser_uncertainty_blocker", row["review_category"])
        self.assertEqual("pdf", row["parser_uncertainty_source"])
        self.assertEqual("high", row["parser_uncertainty_risk_level"])
        self.assertIn("ocr_required", row["parser_uncertainty_flags"])
        self.assertEqual("run_ocr", row["parser_uncertainty_recommendation"])

    def test_medium_parser_uncertainty_creates_domain_review_flag(self) -> None:
        chunk = {
            "chunk_id": "hwp_uncertain",
            "chunk_type": "article",
            "text": "\uc81c1\uc870(\ubaa9\uc801) body",
            "metadata": {
                "article_no": "\uc81c1\uc870",
                "article_title": "\ubaa9\uc801",
                "parser_uncertainty": {
                    "source": "hwp",
                    "risk_level": "medium",
                    "confidence": 0.72,
                    "flags": ["native_table_geometry_unavailable"],
                    "recommendation": "review_tables_and_appendices",
                    "remediation_hint": "Compare with source HWP.",
                },
            },
        }

        flags = chunk_review_flags(chunk)
        row = review_queue_row(
            Path("workspace"),
            {"document_id": "doc_hwp", "filename": "sample.hwp", "institution_name": "\uae30\uad00"},
            chunk,
            flags,
            Path("exports/doc_hwp.jsonl"),
        )

        self.assertIn("parser_uncertainty_review", flags)
        self.assertEqual("domain_attention", review_priority_tier(chunk, flags))
        self.assertEqual("parser_uncertainty_review", row["review_category"])
        self.assertEqual("hwp", row["parser_uncertainty_source"])
        self.assertEqual("medium", row["parser_uncertainty_risk_level"])
        self.assertEqual(0.72, row["parser_uncertainty_confidence"])
        self.assertEqual("native_table_geometry_unavailable", row["parser_uncertainty_flags"])
        self.assertEqual("review_tables_and_appendices", row["parser_uncertainty_recommendation"])

    def test_low_parser_uncertainty_is_not_review_candidate(self) -> None:
        chunk = {
            "chunk_id": "pdf_text",
            "chunk_type": "article",
            "text": "\uc81c1\uc870(\ubaa9\uc801) body",
            "metadata": {
                "article_no": "\uc81c1\uc870",
                "article_title": "\ubaa9\uc801",
                "parser_uncertainty_source": "pdf",
                "parser_uncertainty_risk_level": "low",
                "parser_uncertainty_flags": ["embedded_text_extracted"],
            },
        }

        flags = chunk_review_flags(chunk)

        self.assertNotIn("parser_uncertainty_blocker", flags)
        self.assertNotIn("parser_uncertainty_review", flags)
        self.assertEqual("no_signal", review_priority_tier(chunk, flags))

    def test_review_queue_row_includes_hwp_binary_evidence(self) -> None:
        chunk = {
            "chunk_id": "table_hwp",
            "chunk_type": "table",
            "text": "HWP table-like text",
            "metadata": {
                "table_like": True,
                "table_classification": "structured_table",
                "table_review_reason": "header_and_numeric_rows",
                "table_structured_row_count": 3,
                "table_record_count": 2,
                "table_header_cells": ["Grade", "Rate"],
                "source_hwp_extraction_modes": ["legacy_ole_para_text_only"],
                "source_hwp_streams": ["BodyText/Section0"],
                "source_hwp_section_indices": [1],
                "source_hwp_native_table_geometry": False,
            },
        }
        flags = chunk_review_flags(chunk)

        row = review_queue_row(
            Path("workspace"),
            {"document_id": "doc_hwp", "filename": "sample.hwp", "institution_name": "湲곌?"},
            chunk,
            flags,
            Path("exports/doc_hwp.jsonl"),
        )

        self.assertEqual(row["review_category"], "hwp_binary_geometry_review")
        self.assertEqual(row["source_hwp_extraction_modes"], "legacy_ole_para_text_only")
        self.assertEqual(row["source_hwp_streams"], "BodyText/Section0")
        self.assertEqual(row["source_hwp_section_indices"], "1")
        self.assertEqual(row["source_hwp_native_table_geometry"], "false")
        self.assertEqual(row["table_classification"], "structured_table")
        self.assertEqual(row["table_review_reason"], "header_and_numeric_rows")
        self.assertEqual(row["table_structured_row_count"], 3)
        self.assertEqual(row["table_record_count"], 2)
        self.assertEqual(row["table_header_cells"], "Grade | Rate")

    def test_stable_table_false_positive_is_not_urgent_review(self) -> None:
        chunk = {
            "chunk_id": "table_1",
            "chunk_type": "paragraph",
            "text": "예산 설명 문장",
            "normalized_text": "예산 설명 문장",
            "metadata": {
                "table_like": True,
                "table_probable_false_positive": True,
                "table_false_positive_stability": "stable",
            },
        }

        flags = chunk_review_flags(chunk)

        self.assertIn("table_context_candidate", flags)
        self.assertIn("table_false_positive_candidate", flags)
        self.assertEqual("stable_false_positive", review_priority_tier(chunk, flags))

    def test_legal_hanja_terms_do_not_create_encoding_blocker(self) -> None:
        chunk = {
            "chunk_id": "article_hanja",
            "chunk_type": "article",
            "text": "임직원은 금품등의 收受, 權原, 社會常規에 따라 처리한다.",
            "metadata": {"article_no": "제1조", "article_title": "한자 병기"},
        }

        flags = chunk_review_flags(chunk)

        self.assertNotIn("ocr_or_encoding_noise", flags)
        self.assertEqual("no_signal", review_priority_tier(chunk, flags))

    def test_korean_checklist_questions_do_not_create_encoding_blocker(self) -> None:
        chunk = {
            "chunk_id": "checklist_form",
            "chunk_type": "form",
            "text": (
                "공정채용 점검 체크리스트\n"
                "• 시험 실시계획 수립시기 및 계획내용은 적정한가?\n"
                "- 시험 시행 전 계획수립 및 보고\n"
                "채용공고\n"
                "• 채용계획과 채용공고 내용이 동일한가?\n"
                "시험위원\n"
                "• 전형시험별 외부위원 구성비율을 준수했는가?\n"
                "합격자 결정\n"
                "• 최종합격자가 공정하게 결정되었는가?\n"
            ),
            "metadata": {
                "table_like": True,
                "table_review_required": True,
                "table_review_flags": ["unstable_column_count"],
            },
        }

        flags = chunk_review_flags(chunk)

        self.assertNotIn("ocr_or_encoding_noise", flags)
        self.assertIn("table_review_required", flags)

    def test_orphan_preamble_warning_is_not_blocking_processor_review(self) -> None:
        chunk = {
            "chunk_id": "doc_paragraph_preamble_0001_p1_001",
            "chunk_type": "paragraph",
            "text": "[위치] 사규규정 > preamble Preamble\n[본문]\n사규규정\n<개정> 2024.10.28.",
            "warnings": ["orphan_preamble_text"],
            "metadata": {"warnings": ["orphan_preamble_text"]},
        }

        flags = chunk_review_flags(chunk)

        self.assertNotIn("processor_warning_candidate", flags)
        self.assertEqual("no_signal", review_priority_tier(chunk, flags))

    def test_non_preamble_processor_warning_stays_blocking_review(self) -> None:
        chunk = {
            "chunk_id": "doc_article_제1조_0001_p1_001",
            "chunk_type": "article",
            "text": "제1조 제목 없는 조문",
            "warnings": ["article_title_missing"],
            "metadata": {"article_no": "제1조", "warnings": ["article_title_missing"]},
        }

        flags = chunk_review_flags(chunk)

        self.assertIn("processor_warning_candidate", flags)
        self.assertEqual("blocking_review", review_priority_tier(chunk, flags))

    def _write_batch_report(self, root: Path) -> Path:
        reports = root / "reports"
        reports.mkdir(parents=True)
        path = reports / "batch_quality_test.json"
        payload = {
            "generated_at": "2026-07-08T12:00:00",
            "input_count": 3,
            "successful_count": 2,
            "failed_count": 1,
            "rows": [
                {
                    "input_path": str(root / "fixtures" / "a.hwp"),
                    "filename": "a.hwp",
                    "document_id": "doc_hwp",
                    "apba_id": "C0147",
                    "profile_id": "public_portal-c0147",
                    "source_record_id": "rule-a",
                    "source_file_id": "file-a",
                    "institution_name": "기관A",
                    "status": "completed",
                    "chunk_count": 2,
                    "quality_score": 98.0,
                    "quality_passed": True,
                    "table_like_chunks": 1,
                    "probable_table_false_positive_chunks": 0,
                },
                {
                    "input_path": str(root / "fixtures" / "b.hwpx"),
                    "filename": "b.hwpx",
                    "document_id": "doc_hwpx",
                    "apba_id": "C0165",
                    "profile_id": "public_portal-c0165",
                    "source_record_id": "rule-b",
                    "source_file_id": "file-b",
                    "institution_name": "기관B",
                    "status": "completed",
                    "chunk_count": 1,
                    "quality_score": 99.0,
                    "quality_passed": True,
                    "table_like_chunks": 0,
                    "probable_table_false_positive_chunks": 0,
                },
                {
                    "input_path": str(root / "fixtures" / "c.pdf"),
                    "filename": "c.pdf",
                    "document_id": "doc_pdf_failed",
                    "apba_id": "C0999",
                    "profile_id": "public_portal-c0999",
                    "source_record_id": "rule-c",
                    "source_file_id": "file-c",
                    "institution_name": "기관C",
                    "status": "failed",
                    "failure_category": "ocr_required",
                    "ocr_required": True,
                },
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def _write_chunks(self, root: Path, document_id: str, chunks: list[dict[str, object]]) -> None:
        repository = root / "data" / "repository"
        repository.mkdir(parents=True, exist_ok=True)
        path = repository / f"{document_id}_chunks.json"
        path.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")

    def _write_runtime_artifact(
        self,
        runtime: Path,
        document_id: str,
        chunk_id: str,
        *,
        artifact_name: str | None = None,
    ) -> Path:
        repository = runtime / "repository"
        exports = runtime / "exports"
        repository.mkdir(parents=True)
        exports.mkdir(parents=True)
        (repository / f"{document_id}_chunks.json").write_text(
            json.dumps([{"chunk_id": chunk_id, "chunk_type": "article", "text": chunk_id}]),
            encoding="utf-8",
        )
        artifact = exports / (artifact_name or f"{document_id}.quality.json")
        artifact.write_text("{}", encoding="utf-8")
        return artifact

    def _write_single_row_batch(
        self,
        root: Path,
        row: dict[str, object],
        name: str,
    ) -> Path:
        reports = root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        batch_path = reports / f"batch_quality_{name}.json"
        batch_path.write_text(json.dumps({"rows": [row]}), encoding="utf-8")
        return batch_path

    def _goldset_row(self, document_id: str, filename: str, institution: str, table_like: int) -> dict[str, object]:
        return {
            "document_id": document_id,
            "filename": filename,
            "institution_name": institution,
            "status": "completed",
            "table_like_chunks": table_like,
            "issue_count": 0,
            "warning_count": 0,
            "probable_table_false_positive_chunks": 0,
            "chunk_count": table_like,
        }

    def _goldset_label_row(
        self,
        document_id: str,
        *,
        label_status: str = "reviewed",
        reviewer: str = "tester",
        reviewed_at: str = "2026-07-09",
        pipeline_article_count: int | None = None,
        manual_article_count: int = 0,
        matched_article_count: int = 0,
        pipeline_paragraph_item_count: int | None = None,
        manual_paragraph_item_count: int = 0,
        matched_paragraph_item_count: int = 0,
        pipeline_appendix_form_count: int | None = None,
        manual_appendix_form_count: int = 0,
        matched_appendix_form_count: int = 0,
        pipeline_table_count: int | None = None,
        manual_table_count: int = 0,
        matched_table_count: int = 0,
        pipeline_nested_table_count: int | None = None,
        manual_nested_table_count: int = 0,
        matched_nested_table_count: int = 0,
        pipeline_supplementary_effective_date_count: int | None = None,
        manual_supplementary_effective_date_count: int = 0,
        matched_supplementary_effective_date_count: int = 0,
        pipeline_footnote_caption_count: int | None = None,
        manual_footnote_caption_count: int = 0,
        matched_footnote_caption_count: int = 0,
    ) -> dict[str, object]:
        row: dict[str, object] = {
            "document_id": document_id,
            "label_status": label_status,
            "reviewer": reviewer,
            "reviewed_at": reviewed_at,
            "manual_article_count": manual_article_count,
            "matched_article_count": matched_article_count,
            "manual_paragraph_item_count": manual_paragraph_item_count,
            "matched_paragraph_item_count": matched_paragraph_item_count,
            "manual_appendix_form_count": manual_appendix_form_count,
            "matched_appendix_form_count": matched_appendix_form_count,
            "manual_table_count": manual_table_count,
            "matched_table_count": matched_table_count,
            "manual_nested_table_count": manual_nested_table_count,
            "matched_nested_table_count": matched_nested_table_count,
            "manual_supplementary_effective_date_count": manual_supplementary_effective_date_count,
            "matched_supplementary_effective_date_count": matched_supplementary_effective_date_count,
            "manual_footnote_caption_count": manual_footnote_caption_count,
            "matched_footnote_caption_count": matched_footnote_caption_count,
        }
        optional_pipeline_counts = {
            "pipeline_article_count": pipeline_article_count,
            "pipeline_paragraph_item_count": pipeline_paragraph_item_count,
            "pipeline_appendix_form_count": pipeline_appendix_form_count,
            "pipeline_table_count": pipeline_table_count,
            "pipeline_nested_table_count": pipeline_nested_table_count,
            "pipeline_supplementary_effective_date_count": pipeline_supplementary_effective_date_count,
            "pipeline_footnote_caption_count": pipeline_footnote_caption_count,
        }
        row.update({key: value for key, value in optional_pipeline_counts.items() if value is not None})
        return row


if __name__ == "__main__":
    unittest.main()
