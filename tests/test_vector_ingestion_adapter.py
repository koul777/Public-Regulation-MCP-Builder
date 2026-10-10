from __future__ import annotations

import unittest

from app.ingestion import vector_adapter as vector_adapter_module
from app.ingestion.vector_adapter import (
    VECTOR_METADATA_SEMANTIC_FINGERPRINT_VERSION,
    VECTOR_RECORD_SCHEMA_VERSION,
    VECTOR_RECORD_SEMANTIC_FINGERPRINT_VERSION,
    VECTOR_RECORD_VERIFICATION_VERSION,
    build_vector_records,
    stable_content_hash,
    vector_metadata_semantic_fingerprint,
    vector_record_semantic_fingerprint,
    vector_record_verification_hash,
    vector_record_from_chunk,
    vector_record_path_leaks,
    with_vector_record_verification,
)


class VectorIngestionAdapterTests(unittest.TestCase):
    def test_publishes_canonical_hierarchy_and_keeps_source_structure_trace(self) -> None:
        record = vector_record_from_chunk(
            {
                "chunk_id": "chunk-canonical-hierarchy",
                "document_id": "doc-combined-book",
                "tenant_id": "tenant-a",
                "retrieval_text": "[문서명] 인사규정\n[위치] 인사규정 > 제1조 목적\n[본문]\n본문",
                "hierarchy_path": "기관 규정집 > 제2장 인사 > 4-1 인사규정 > 제1조 목적",
                "source_hierarchy_path": "기관 규정집 > 제2장 인사 > 4-1 인사규정 > 제1조 목적",
                "canonical_hierarchy_path": "인사규정 > 제1조 목적",
                "canonical_regulation_title": "인사규정",
                "canonical_regulation_no": "4-1",
                "parent_id": "source-parent-node",
                "entity_id": "source-article-node",
                "regulation_node_id": "source-regulation-node",
                "regulation_source_node_id": "source-regulation-boundary",
                "approval_status": "approved",
                "approval_id": "approval-canonical-hierarchy",
                "approved_content_hash": "approved-canonical-hierarchy",
                "security_level": "internal",
            }
        )

        self.assertIsNotNone(record)
        assert record is not None
        metadata = record["metadata"]
        self.assertEqual("인사규정 > 제1조 목적", metadata["hierarchy_path"])
        self.assertEqual(
            "기관 규정집 > 제2장 인사 > 4-1 인사규정 > 제1조 목적",
            metadata["source_hierarchy_path"],
        )
        self.assertEqual("인사규정 > 제1조 목적", metadata["canonical_hierarchy_path"])
        self.assertEqual("source-parent-node", metadata["parent_id"])
        self.assertEqual("source-article-node", metadata["entity_id"])
        self.assertEqual("source-regulation-node", metadata["regulation_node_id"])
        self.assertEqual(
            "source-regulation-boundary",
            metadata["regulation_source_node_id"],
        )

    def test_semantic_fingerprints_cover_retrieval_lifecycle_acl_and_profile_metadata(self) -> None:
        base = {
            "chunk_id": "chunk-semantic",
            "document_id": "doc-semantic",
            "tenant_id": "tenant-a",
            "retrieval_text": "제16조 임용 기준",
            "profile_id": "profile-a",
            "regulation_title": "인사규정",
            "regulation_status": "approved",
            "effective_to": None,
            "hierarchy_path": "제2장 > 제16조",
            "department_acl": ["hr"],
            "approval_status": "approved",
            "approval_id": "approval-semantic",
            "approved_content_hash": "approved-hash-semantic",
            "security_level": "internal",
        }
        first = vector_record_from_chunk(base)
        reordered = vector_record_from_chunk(dict(reversed(list(base.items()))))
        changed = vector_record_from_chunk(
            {
                **base,
                "profile_id": "profile-b",
                "regulation_title": "개정 인사규정",
                "regulation_status": "superseded",
                "effective_to": "2026-07-31",
                "hierarchy_path": "제3장 > 제16조",
                "department_acl": ["audit", "hr"],
                "security_level": "sensitive",
            }
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(reordered)
        self.assertIsNotNone(changed)
        assert first is not None and reordered is not None and changed is not None
        self.assertEqual(
            VECTOR_METADATA_SEMANTIC_FINGERPRINT_VERSION,
            first["metadata_semantic_fingerprint_version"],
        )
        self.assertEqual(
            VECTOR_RECORD_SEMANTIC_FINGERPRINT_VERSION,
            first["record_semantic_fingerprint_version"],
        )
        self.assertEqual(
            vector_metadata_semantic_fingerprint(first["metadata"]),
            first["metadata_semantic_fingerprint"],
        )
        self.assertEqual(
            vector_record_semantic_fingerprint(first),
            first["record_semantic_fingerprint"],
        )
        self.assertEqual(
            first["metadata_semantic_fingerprint"],
            reordered["metadata_semantic_fingerprint"],
        )
        self.assertEqual(
            first["record_semantic_fingerprint"],
            reordered["record_semantic_fingerprint"],
        )
        self.assertNotEqual(
            first["metadata_semantic_fingerprint"],
            changed["metadata_semantic_fingerprint"],
        )
        self.assertNotEqual(
            first["record_semantic_fingerprint"],
            changed["record_semantic_fingerprint"],
        )

    def test_preserves_safe_structural_child_sample_for_exact_reference_resolution(self) -> None:
        record = vector_record_from_chunk(
            {
                "chunk_id": "chunk-structure",
                "document_id": "doc-structure",
                "tenant_id": "tenant-a",
                "retrieval_text": "제16조 구조화 본문",
                "article_no": "제16조",
                "paragraph_unit_count": 1,
                "item_unit_count": 1,
                "paragraph_item_unit_sample": [
                    {
                        "node_id": "private-node-1",
                        "node_type": "paragraph",
                        "number": "②",
                        "text_preview": "민감할 수 있는 본문",
                    },
                    {
                        "node_id": "private-node-2",
                        "node_type": "item",
                        "number": "1.",
                    },
                ],
                "approval_status": "approved",
                "approval_id": "approval-structure",
                "approved_content_hash": "approved-hash-structure",
                "security_level": "internal",
            }
        )

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(1, record["metadata"]["paragraph_unit_count"])
        self.assertEqual(1, record["metadata"]["item_unit_count"])
        self.assertEqual(
            [
                {"node_type": "paragraph", "number": "②"},
                {"node_type": "item", "number": "1."},
            ],
            record["metadata"]["paragraph_item_unit_sample"],
        )
        self.assertNotIn("private-node-1", str(record["metadata"]))
        self.assertNotIn("민감할 수 있는 본문", str(record["metadata"]))

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
                "source_xml_files": ["Contents/section0.xml"],
                "source_xml_roles": ["body"],
                "source_hwpx_parser_review_flags": ["nested_table"],
                "source_hwpx_xml_block_indices": [312, 313, 314],
                "source_hwpx_nested_table_text_snippets": ["Nested table evidence"],
                "source_hwp_extraction_modes": ["legacy_ole_para_text_only"],
                "source_hwp_streams": ["BodyText/Section0"],
                "source_hwp_section_indices": [1],
                "source_hwp_native_table_geometry": False,
                "pdf_embedded_image_pages": [3, 8],
                "table_source": "kordoc",
                "table_geometry_source": "kordoc",
                "kordoc_table_parser_status": "parsed",
                "kordoc_table_count": 3,
                "kordoc_elapsed_ms": 12.5,
                "kordoc_input_extension": ".hwp",
                "kordoc_timeout_seconds": 120,
                "kordoc_table_inventory": {"tables": [{"title": "internal"}]},
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
        self.assertEqual(record["metadata"]["source_xml_files"], ["Contents/section0.xml"])
        self.assertEqual(record["metadata"]["source_xml_roles"], ["body"])
        self.assertEqual(record["metadata"]["source_hwpx_parser_review_flags"], ["nested_table"])
        self.assertEqual(record["metadata"]["source_hwpx_xml_block_indices"], [312, 313, 314])
        self.assertEqual(record["metadata"]["source_hwpx_nested_table_text_snippets"], ["Nested table evidence"])
        self.assertEqual(record["metadata"]["source_hwp_extraction_modes"], ["legacy_ole_para_text_only"])
        self.assertEqual(record["metadata"]["source_hwp_streams"], ["BodyText/Section0"])
        self.assertEqual(record["metadata"]["source_hwp_section_indices"], [1])
        self.assertFalse(record["metadata"]["source_hwp_native_table_geometry"])
        self.assertEqual(record["metadata"]["pdf_embedded_image_pages"], [3, 8])
        self.assertEqual(record["metadata"]["table_source"], "kordoc")
        self.assertEqual(record["metadata"]["table_geometry_source"], "kordoc")
        self.assertEqual(record["metadata"]["kordoc_table_parser_status"], "parsed")
        self.assertEqual(record["metadata"]["kordoc_table_count"], 3)
        self.assertNotIn("kordoc_elapsed_ms", record["metadata"])
        self.assertNotIn("kordoc_input_extension", record["metadata"])
        self.assertNotIn("kordoc_timeout_seconds", record["metadata"])
        self.assertNotIn("kordoc_table_inventory", record["metadata"])
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


_VERIFICATION_METADATA_CASES = [
    {},
    {"approval_status": "approved", "approval_id": "a", "approved_content_hash": "h", "tenant_id": "t"},
    {
        "nested": {"z": [1, 2.5, None, True, "\ud55c\uae00"], "a": {"k": []}},
        "unicode": "\u2603 \ud55c\uae00 \"quote\" \\ back",
    },
    {"floats": [0.1, 1e-7, 1e21, -0.0], "ints": [0, -1, 2**65]},
    {"department_acl": ["b", "a"], "security_level": "Internal"},
]


class VectorRecordVerificationEquivalenceTests(unittest.TestCase):
    """The single-serialization verification path must keep every digest unchanged."""

    def test_combined_digests_equal_the_individual_helpers(self) -> None:
        for index, metadata in enumerate(_VERIFICATION_METADATA_CASES):
            for text in ("", "plain", "\ud55c\uae00 \ubcf8\ubb38\n\t\"quoted\"", "x" * 5000):
                with self.subTest(index=index, text=text[:10]):
                    content_hash, metadata_fingerprint = vector_adapter_module._content_hash_and_metadata_fingerprint(
                        text, metadata
                    )
                    self.assertEqual(stable_content_hash(text, metadata), content_hash)
                    self.assertEqual(vector_metadata_semantic_fingerprint(metadata), metadata_fingerprint)

    def test_verification_equals_the_unoptimized_composition(self) -> None:
        for index, metadata in enumerate(_VERIFICATION_METADATA_CASES):
            record = {
                "schema_version": VECTOR_RECORD_SCHEMA_VERSION,
                "id": f"doc:chunk-{index}",
                "document_id": "doc",
                "chunk_id": f"chunk-{index}",
                "tenant_id": "tenant-a",
                "text": "\ubcf8\ubb38 text",
                "metadata": metadata,
                "content_hash": "stale",
            }
            expected = dict(record)
            expected["content_hash"] = stable_content_hash(record["text"], metadata)
            expected["metadata_semantic_fingerprint_version"] = VECTOR_METADATA_SEMANTIC_FINGERPRINT_VERSION
            expected["metadata_semantic_fingerprint"] = vector_metadata_semantic_fingerprint(metadata)
            expected["record_semantic_fingerprint_version"] = VECTOR_RECORD_SEMANTIC_FINGERPRINT_VERSION
            expected["record_semantic_fingerprint"] = vector_record_semantic_fingerprint(expected)
            expected["verification_version"] = VECTOR_RECORD_VERIFICATION_VERSION
            expected["verification_hash"] = vector_record_verification_hash(expected)
            expected["verified_at"] = "2026-01-01T00:00:00+00:00"
            with self.subTest(index=index):
                actual = with_vector_record_verification(record, verified_at="2026-01-01T00:00:00+00:00")
                self.assertEqual(expected, actual)
                self.assertEqual(list(expected), list(actual))

    def test_record_fingerprint_accepts_a_precomputed_metadata_fingerprint(self) -> None:
        record = {
            "id": "doc:c",
            "document_id": "doc",
            "chunk_id": "c",
            "tenant_id": "t",
            "text": "text",
            "metadata": {"k": "v"},
        }
        precomputed = vector_metadata_semantic_fingerprint(record["metadata"])
        self.assertEqual(
            vector_record_semantic_fingerprint(record),
            vector_record_semantic_fingerprint(record, metadata_fingerprint=precomputed),
        )
        # the argument is trusted verbatim, so a different value yields a different digest
        self.assertNotEqual(
            vector_record_semantic_fingerprint(record),
            vector_record_semantic_fingerprint(record, metadata_fingerprint="0" * 64),
        )

    def test_vector_record_from_chunk_hash_matches_its_final_text_and_metadata(self) -> None:
        record = vector_record_from_chunk(
            {
                "chunk_id": "c1",
                "document_id": "d1",
                "tenant_id": "t",
                "retrieval_text": "\ubcf8\ubb38",
                "approval_status": "approved",
                "approval_id": "ap",
                "approved_content_hash": "h",
                "security_level": "internal",
            }
        )
        self.assertEqual(stable_content_hash(record["text"], record["metadata"]), record["content_hash"])
        self.assertEqual(
            list(record)[:7],
            ["schema_version", "id", "document_id", "chunk_id", "text", "metadata", "content_hash"],
        )


def _reference_is_empty(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _reference_json_safe(value):
    """Verbatim copy of vector_adapter._json_safe before items were evaluated once."""

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_reference_json_safe(item) for item in value if not _reference_is_empty(_reference_json_safe(item))]
    if isinstance(value, tuple):
        return [_reference_json_safe(item) for item in value if not _reference_is_empty(_reference_json_safe(item))]
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            safe_item = _reference_json_safe(item)
            if _reference_is_empty(safe_item):
                continue
            cleaned[str(key)] = safe_item
        return cleaned
    return str(value)


class _EqualsEverything:
    def __eq__(self, other) -> bool:
        return True

    __hash__ = None  # type: ignore[assignment]


class _StrBox(str):
    pass


class VectorMetadataSanitizingEquivalenceTests(unittest.TestCase):
    VALUES = [
        None,
        "",
        "text",
        0,
        1,
        0.0,
        float("nan"),
        False,
        True,
        [],
        {},
        [None, "", [], {}, "x", 0, [[]]],
        (1, "", None, ("a", ())),
        {"a": None, "b": "", "c": [], "d": {}, "e": 0, "f": {"g": [None, "h"]}},
        {1: "int key", (2, 3): "tuple key"},
        {"s": {1, 2}, "o": object},
        _StrBox(""),
        _StrBox("boxed"),
        _EqualsEverything(),
    ]

    def test_is_empty_matches_the_original_expression(self) -> None:
        for value in self.VALUES:
            with self.subTest(value=repr(value)[:40]):
                try:
                    expected = _reference_is_empty(value)
                except TypeError:
                    continue
                self.assertEqual(expected, vector_adapter_module._is_empty(value))

    def test_json_safe_matches_the_original_recursion(self) -> None:
        for value in self.VALUES:
            with self.subTest(value=repr(value)[:40]):
                try:
                    expected = _reference_json_safe(value)
                except TypeError:
                    continue
                actual = vector_adapter_module._json_safe(value)
                if isinstance(expected, float) and expected != expected:
                    self.assertTrue(actual != actual)
                else:
                    self.assertEqual(expected, actual)
        nested = {"outer": [self.VALUES[11], self.VALUES[12], {"inner": self.VALUES[13]}], "empty": self.VALUES[9]}
        self.assertEqual(_reference_json_safe(nested), vector_adapter_module._json_safe(nested))

    def test_numeric_lists_are_skipped_by_the_leak_scan_but_strings_inside_are_not(self) -> None:
        clean = {"id": "r", "embedding": [0.1, 0.2, 3, True, None]}
        self.assertEqual([], vector_record_path_leaks([clean]))
        leaking = {"id": "r", "embedding": [0.1, 0.2, "/home/user/secret.txt"]}
        self.assertEqual(
            [("r", "$.embedding[2]")],
            [(item["id"], item["field_path"]) for item in vector_record_path_leaks([leaking])],
        )
        nested = {"id": "n", "rows": [[1, 2], [3, "C:\\leak\\file"]]}
        self.assertEqual(
            [("n", "$.rows[1][1]")],
            [(item["id"], item["field_path"]) for item in vector_record_path_leaks([nested])],
        )

    def test_build_vector_records_leaves_the_collector_state_alone(self) -> None:
        import gc

        was_enabled = gc.isenabled()
        gc.enable()
        try:
            records, summary = build_vector_records(
                [
                    {
                        "chunk_id": "c1",
                        "document_id": "d1",
                        "tenant_id": "t",
                        "retrieval_text": "본문",
                        "approval_status": "approved",
                        "approval_id": "ap",
                        "approved_content_hash": "h",
                        "security_level": "internal",
                    }
                ]
            )
            self.assertTrue(gc.isenabled())
            self.assertEqual(1, len(records))
            self.assertEqual(1, summary["record_count"])
        finally:
            if not was_enabled:
                gc.disable()


if __name__ == "__main__":
    unittest.main()
