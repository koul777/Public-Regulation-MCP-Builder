from __future__ import annotations

import unittest

from app.ingestion.vector_adapter import (
    VECTOR_RECORD_SCHEMA_VERSION,
    VECTOR_RECORD_VERIFICATION_VERSION,
    build_vector_records,
    vector_record_verification_hash,
    vector_record_from_chunk,
    vector_record_path_leaks,
)


class VectorIngestionAdapterTests(unittest.TestCase):
    def test_builds_provider_neutral_record_with_public_metadata(self) -> None:
        record = vector_record_from_chunk(
            {
                "chunk_id": "chunk-1",
                "document_id": "doc-1",
                "tenant_id": "tenant-a",
                "retrieval_text": "Article purpose text",
                "text": "raw",
                "document_name": "Rules",
                "source_file": "rules.pdf",
                "input_path": "C:\\private\\rules.pdf",
                "source_system": "PUBLIC_PORTAL",
                "source_record_id": "board-1",
                "effective_date": "2026-01-01",
                "valid_from": "2026-01-01",
                "temporal_metadata_ambiguous_fields": ["revision_date"],
                "temporal_metadata_ambiguous_scope": "regulation",
                "temporal_metadata_ambiguous_source_chunk_ids": ["chunk-source"],
                "approval_status": "approved",
                "approval_id": "approval-1",
                "approved_content_hash": "approved-hash-1",
                "approved_by": "reviewer",
                "approved_at": "2026-07-08T00:00:00+09:00",
                "approval_worklist_report_path": "reports/approval_worklist_current.json",
                "approval_worklist_report_sha256": "b" * 64,
                "approval_review_batch_id": "batch-20260709",
                "approval_review_batch_chunk_fingerprint": "c" * 64,
                "approval_review_strategy": "human_bulk_review",
                "security_level": "internal",
                "revision_history": [{"event_type": "revision", "date": "2025-12-30"}],
                "article_effective_overrides": [{"article_ref": "article-7", "effective_date": "2026-01-02"}],
                "supplementary_paragraph_label": "effective date",
                "supplementary_boilerplate": True,
                "source_hwpx_block_types": ["table"],
                "source_hwpx_parser_review_flags": ["nested_table"],
                "source_hwpx_xml_block_indices": [312, 313, 314],
                "source_hwpx_nested_table_text_snippets": ["Nested table evidence"],
                "source_hwp_extraction_modes": ["legacy_ole_para_text_only"],
                "source_hwp_streams": ["BodyText/Section0"],
                "source_hwp_section_indices": [1],
                "source_hwp_native_table_geometry": False,
                "table_source": "kordoc",
                "table_geometry_source": "kordoc",
                "kordoc_table_parser_status": "parsed",
                "kordoc_table_count": 3,
                "kordoc_table_match": {
                    "match_label": "medium_review_match",
                    "table_index": 2,
                    "row_count": 4,
                    "column_count": 3,
                },
                "kordoc_table_match_review_required": True,
                "kordoc_table_match_provisional": True,
                "kordoc_table_promoted": True,
                "kordoc_table_promotion_review_required": True,
                "parser_uncertainty_schema_version": "reg-rag-parser-uncertainty-v1",
                "parser_uncertainty_source": "hwp",
                "parser_uncertainty_risk_level": "medium",
                "parser_uncertainty_confidence": 0.72,
                "parser_uncertainty_flags": ["legacy_ole_para_text_only", "native_table_geometry_unavailable"],
                "parser_uncertainty_recommendation": "review_tables_and_appendices",
                "answer_profile_version": "reg-rag-answer-profile-v1",
                "answer_intents": ["duration"],
                "answer_keywords": ["휴직 기간"],
                "answer_facts": [{"type": "duration", "value": "3년", "sentence": "자녀 1명에 대하여 3년 이내"}],
                "answer_outline": ["자녀 1명에 대하여 3년 이내"],
            }
        )

        self.assertIsNotNone(record)
        self.assertEqual(record["schema_version"], VECTOR_RECORD_SCHEMA_VERSION)
        self.assertEqual(record["id"], "doc-1:chunk-1")
        self.assertEqual(record["text"], "Article purpose text")
        self.assertEqual(record["metadata"]["tenant_id"], "tenant-a")
        self.assertEqual(record["metadata"]["effective_date"], "2026-01-01")
        self.assertEqual(record["metadata"]["temporal_metadata_ambiguous_fields"], ["revision_date"])
        self.assertEqual(record["metadata"]["temporal_metadata_ambiguous_scope"], "regulation")
        self.assertEqual(record["metadata"]["temporal_metadata_ambiguous_source_chunk_ids"], ["chunk-source"])
        self.assertEqual(record["metadata"]["approval_status"], "approved")
        self.assertEqual(record["metadata"]["approval_id"], "approval-1")
        self.assertEqual(record["metadata"]["approved_content_hash"], "approved-hash-1")
        self.assertEqual(record["metadata"]["approval_worklist_report_path"], "reports/approval_worklist_current.json")
        self.assertEqual(record["metadata"]["approval_worklist_report_sha256"], "b" * 64)
        self.assertEqual(record["metadata"]["approval_review_batch_id"], "batch-20260709")
        self.assertEqual(record["metadata"]["approval_review_batch_chunk_fingerprint"], "c" * 64)
        self.assertEqual(record["metadata"]["approval_review_strategy"], "human_bulk_review")
        self.assertEqual(record["metadata"]["security_level"], "internal")
        self.assertIn("revision_history", record["metadata"])
        self.assertEqual(record["metadata"]["supplementary_paragraph_label"], "effective date")
        self.assertTrue(record["metadata"]["supplementary_boilerplate"])
        self.assertEqual(record["metadata"]["answer_profile_version"], "reg-rag-answer-profile-v1")
        self.assertEqual(record["metadata"]["answer_intents"], ["duration"])
        self.assertEqual(record["metadata"]["answer_facts"][0]["value"], "3년")
        self.assertEqual(record["metadata"]["source_hwpx_block_types"], ["table"])
        self.assertEqual(record["metadata"]["source_hwpx_parser_review_flags"], ["nested_table"])
        self.assertEqual(record["metadata"]["source_hwpx_xml_block_indices"], [312, 313, 314])
        self.assertEqual(record["metadata"]["source_hwpx_nested_table_text_snippets"], ["Nested table evidence"])
        self.assertEqual(record["metadata"]["source_hwp_extraction_modes"], ["legacy_ole_para_text_only"])
        self.assertEqual(record["metadata"]["source_hwp_streams"], ["BodyText/Section0"])
        self.assertEqual(record["metadata"]["source_hwp_section_indices"], [1])
        self.assertFalse(record["metadata"]["source_hwp_native_table_geometry"])
        self.assertEqual(record["metadata"]["table_source"], "kordoc")
        self.assertEqual(record["metadata"]["table_geometry_source"], "kordoc")
        self.assertEqual(record["metadata"]["kordoc_table_parser_status"], "parsed")
        self.assertEqual(record["metadata"]["kordoc_table_count"], 3)
        self.assertEqual(record["metadata"]["kordoc_table_match"]["table_index"], 2)
        self.assertTrue(record["metadata"]["kordoc_table_match_review_required"])
        self.assertTrue(record["metadata"]["kordoc_table_match_provisional"])
        self.assertTrue(record["metadata"]["kordoc_table_promoted"])
        self.assertTrue(record["metadata"]["kordoc_table_promotion_review_required"])
        self.assertEqual(record["metadata"]["parser_uncertainty_schema_version"], "reg-rag-parser-uncertainty-v1")
        self.assertEqual(record["metadata"]["parser_uncertainty_source"], "hwp")
        self.assertEqual(record["metadata"]["parser_uncertainty_risk_level"], "medium")
        self.assertIn("native_table_geometry_unavailable", record["metadata"]["parser_uncertainty_flags"])
        self.assertNotIn("input_path", record["metadata"])
        self.assertEqual(len(record["content_hash"]), 64)
        self.assertEqual(record["verification_version"], VECTOR_RECORD_VERIFICATION_VERSION)
        self.assertEqual(record["verification_hash"], vector_record_verification_hash(record))
        self.assertEqual(len(record["verification_hash"]), 64)
        self.assertIn("verified_at", record)

    def test_normalizes_legacy_regulation_lifecycle_aliases_into_vector_metadata(self) -> None:
        record = vector_record_from_chunk(
            {
                "chunk_id": "chunk-legacy",
                "document_id": "doc-legacy",
                "tenant_id": "tenant-a",
                "retrieval_text": "Legacy regulation text",
                "regulation_no": "4-4-1",
                "revision_date": "2025-01-02",
                "approval_status": "approved",
                "approval_id": "approval-legacy",
                "approved_content_hash": "approved-hash-legacy",
                "security_level": "internal",
            }
        )

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual("4-4-1", record["metadata"]["regulation_id"])
        self.assertEqual("2025-01-02", record["metadata"]["regulation_version"])
        self.assertEqual("2025-01-02", record["metadata"]["effective_from"])
        self.assertIn("effective_to", record["metadata"])
        self.assertIn("repealed_at", record["metadata"])

    def test_summarizes_temporal_metadata_and_duplicates(self) -> None:
        records, summary = build_vector_records(
            [
                {
                    "chunk_id": "chunk-1",
                    "document_id": "doc-1",
                    "tenant_id": "tenant-a",
                    "retrieval_text": "text",
                    "chunk_type": "article",
                    "profile_id": "public_portal",
                    "source_system": "PUBLIC_PORTAL",
                    "effective_date": "2026-01-01",
                    "supplementary_boilerplate": True,
                    "source_hwpx_xml_block_indices": [1],
                    "source_hwpx_nested_table_text_snippets": ["nested"],
                    "source_hwp_extraction_modes": ["legacy_ole_para_text_only"],
                    "source_hwp_streams": ["BodyText/Section0"],
                    "source_hwp_section_indices": [1],
                    "source_hwp_native_table_geometry": False,
                    "kordoc_table_parser_status": "parsed",
                    "kordoc_table_count": 2,
                    "kordoc_table_match": {"match_label": "medium_review_match"},
                    "kordoc_table_promoted": True,
                    "approval_status": "approved",
                    "approval_id": "approval-1",
                    "approved_content_hash": "approved-hash-1",
                    "security_level": "internal",
                },
                {
                    "chunk_id": "chunk-1",
                    "document_id": "doc-1",
                    "tenant_id": "tenant-a",
                    "retrieval_text": "text again",
                    "chunk_type": "article",
                    "profile_id": "public_portal",
                    "source_system": "PUBLIC_PORTAL",
                    "temporal_metadata_ambiguous_fields": ["effective_date"],
                    "approval_status": "approved",
                    "approval_id": "approval-2",
                    "approved_content_hash": "approved-hash-2",
                    "security_level": "internal",
                },
                {"chunk_id": "empty", "document_id": "doc-1", "retrieval_text": ""},
                {
                    "chunk_id": "draft",
                    "document_id": "doc-1",
                    "tenant_id": "tenant-a",
                    "retrieval_text": "draft text",
                    "approval_status": "draft",
                },
            ]
        )

        self.assertEqual(len(records), 2)
        self.assertEqual(summary["record_count"], 2)
        self.assertEqual(summary["duplicate_id_count"], 1)
        self.assertEqual(summary["skipped_empty_text_count"], 1)
        self.assertEqual(summary["skipped_unapproved_count"], 1)
        self.assertEqual(summary["approval_status_counts"]["approved"], 2)
        self.assertEqual(summary["approval_status_counts"]["draft"], 1)
        self.assertEqual(summary["approval_status_counts"]["missing"], 1)
        self.assertEqual(summary["temporal_metadata_counts"]["effective_date"], 1)
        self.assertEqual(summary["temporal_metadata_counts"]["revision_date"], 0)
        self.assertEqual(summary["temporal_metadata_counts"]["article_validity_windows"], 0)
        self.assertEqual(summary["temporal_metadata_counts"]["temporal_metadata_inherited"], 0)
        self.assertEqual(summary["temporal_metadata_counts"]["temporal_metadata_normalized"], 0)
        self.assertEqual(summary["temporal_metadata_counts"]["temporal_metadata_ambiguous"], 1)
        self.assertEqual(summary["temporal_metadata_counts"]["supplementary_boilerplate"], 1)
        self.assertEqual(summary["hwpx_metadata_counts"]["source_hwpx_xml_block_indices"], 1)
        self.assertEqual(summary["hwpx_metadata_counts"]["source_hwpx_nested_table_text_snippets"], 1)
        self.assertEqual(summary["hwp_metadata_counts"]["source_hwp_extraction_modes"], 1)
        self.assertEqual(summary["hwp_metadata_counts"]["source_hwp_streams"], 1)
        self.assertEqual(summary["hwp_metadata_counts"]["source_hwp_section_indices"], 1)
        self.assertEqual(summary["hwp_metadata_counts"]["source_hwp_native_table_geometry_false"], 1)
        self.assertEqual(summary["kordoc_metadata_counts"]["kordoc_table_parser_status"], 1)
        self.assertEqual(summary["kordoc_metadata_counts"]["kordoc_table_count"], 1)
        self.assertEqual(summary["kordoc_metadata_counts"]["kordoc_table_match"], 1)
        self.assertEqual(summary["kordoc_metadata_counts"]["kordoc_table_promoted"], 1)
        self.assertEqual(summary["chunk_type_counts"], {"article": 2})

    def test_retrieval_text_appends_table_markdown_for_vector_records(self) -> None:
        record = vector_record_from_chunk(
            {
                "chunk_id": "chunk-table",
                "document_id": "doc-1",
                "tenant_id": "tenant-a",
                "retrieval_text": "[Document] Rules\n[Body]\nTable intro",
                "table_markdown": "| Type | Value |\n| --- | --- |\n| A | B |",
                "chunk_type": "appendix",
                "approval_status": "approved",
                "approval_id": "approval-table",
                "approved_content_hash": "approved-hash-table",
                "security_level": "internal",
            }
        )

        self.assertIsNotNone(record)
        self.assertIn("| Type | Value |", record["text"])

    def test_approved_chunk_without_approved_content_hash_does_not_create_vector_record(self) -> None:
        record = vector_record_from_chunk(
            {
                "chunk_id": "chunk-missing-approved-hash",
                "document_id": "doc-1",
                "tenant_id": "tenant-a",
                "retrieval_text": "Article purpose text",
                "approval_status": "approved",
                "approval_id": "approval-missing-hash",
                "security_level": "internal",
            }
        )

        self.assertIsNone(record)

    def test_unapproved_chunk_does_not_create_vector_record(self) -> None:
        record = vector_record_from_chunk(
            {
                "chunk_id": "chunk-draft",
                "document_id": "doc-1",
                "tenant_id": "tenant-a",
                "retrieval_text": "Article purpose text",
                "approval_status": "draft",
            }
        )

        self.assertIsNone(record)

    def test_detects_local_path_leaks(self) -> None:
        leaks = vector_record_path_leaks(
            [
                {
                    "id": "record-1",
                    "text": "safe",
                    "metadata": {"source_file": "C:\\Users\\dd\\secret.pdf"},
                }
            ]
        )

        self.assertEqual(leaks[0]["id"], "record-1")
        self.assertIn("metadata.source_file", leaks[0]["field_path"])

    def test_detects_posix_container_path_leaks(self) -> None:
        leaks = vector_record_path_leaks(
            [
                {
                    "id": "record-1",
                    "text": "safe",
                    "metadata": {
                        "source_file": "/tmp/secret.pdf",
                        "quality_json": "/app/runtime/private/quality.json",
                        "runtime_path": "/usr/src/app/secret.pdf",
                    },
                }
            ]
        )

        leaked_paths = {item["field_path"] for item in leaks}
        self.assertIn("$.metadata.source_file", leaked_paths)
        self.assertIn("$.metadata.quality_json", leaked_paths)
        self.assertIn("$.metadata.runtime_path", leaked_paths)


if __name__ == "__main__":
    unittest.main()
