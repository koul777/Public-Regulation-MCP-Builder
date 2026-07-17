from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.config import Settings
from app.core.tenant_access import settings_for_tenant
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository
from scripts.build_approval_worklist import build_approval_worklist


class BuildApprovalWorklistTests(unittest.TestCase):
    def test_groups_bulk_candidates_and_manual_attention_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc-a",
                    filename="a.pdf",
                    document_name="A",
                    file_type="pdf",
                    file_hash="hash-a",
                    institution_name="Institution A",
                    apba_id="C0001",
                    source_system="PUBLIC_PORTAL",
                    source_record_id="record-a",
                    profile_id="public_portal-c0001",
                )
            )
            repository.save_processing_result(
                "doc-a",
                [],
                [
                    Chunk(
                        chunk_id="clean",
                        document_id="doc-a",
                        chunk_type="article",
                        text="clean",
                        metadata={"article_no": "\uc81c1\uc870", "article_title": "\ubaa9\uc801"},
                    ),
                    Chunk(
                        chunk_id="attention",
                        document_id="doc-a",
                        chunk_type="table",
                        text="table",
                        metadata={"table_review_required": True, "table_review_flags": ["row_review_required"]},
                    ),
                    Chunk(
                        chunk_id="approved",
                        document_id="doc-a",
                        chunk_type="article",
                        text="approved",
                        approval_status="approved",
                    ),
                ],
                [],
            )

            report = build_approval_worklist(data_dir=settings.data_dir, source_system="PUBLIC_PORTAL", apba_id="C0001")

        self.assertEqual(1, report["document_count"])
        self.assertEqual(str(settings.data_dir), report["effective_data_dir"])
        self.assertEqual("default", report["tenant_id"])
        self.assertEqual({"approved": 1, "draft": 2}, report["approval_status_totals"])
        self.assertEqual(1, report["bulk_review_candidate_chunks"])
        self.assertEqual(1, report["manual_attention_chunks"])
        self.assertEqual(1, report["blocking_review_chunks"])
        self.assertEqual(1, report["no_signal_chunks"])
        self.assertEqual(1, report["low_risk_batch_review_candidate_chunks"])
        self.assertEqual("manual_review_first", report["documents"][0]["suggested_action"])
        self.assertIn("table_review_required", report["documents"][0]["top_attention_reasons"])
        self.assertEqual(64, len(report["documents"][0]["review_candidate_fingerprint"]))
        self.assertEqual(64, len(report["documents"][0]["manual_attention_fingerprint"]))
        self.assertEqual(64, len(report["documents"][0]["low_risk_batch_review_candidate_fingerprint"]))

    def test_review_candidate_fingerprint_changes_when_same_chunk_content_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc-a",
                    filename="a.pdf",
                    document_name="A",
                    file_type="pdf",
                    file_hash="hash-a",
                    source_system="PUBLIC_PORTAL",
                )
            )
            repository.save_processing_result(
                "doc-a",
                [],
                [
                    Chunk(
                        chunk_id="clean",
                        document_id="doc-a",
                        chunk_type="article",
                        text="original body",
                        metadata={"article_no": "\uc81c1\uc870", "article_title": "\ubaa9\uc801"},
                    )
                ],
                [],
            )
            before = build_approval_worklist(data_dir=settings.data_dir, source_system="PUBLIC_PORTAL")

            repository.save_chunks(
                "doc-a",
                [
                    Chunk(
                        chunk_id="clean",
                        document_id="doc-a",
                        chunk_type="article",
                        text="changed body",
                        metadata={"article_no": "\uc81c1\uc870", "article_title": "\ubaa9\uc801"},
                    )
                ],
            )
            after = build_approval_worklist(data_dir=settings.data_dir, source_system="PUBLIC_PORTAL")

        self.assertNotEqual(
            before["documents"][0]["low_risk_batch_review_candidate_fingerprint"],
            after["documents"][0]["low_risk_batch_review_candidate_fingerprint"],
        )

    def test_filter_excludes_other_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            for document_id, apba_id in (("doc-a", "C0001"), ("doc-b", "C0002")):
                repository.upsert_document(
                    Document(
                        document_id=document_id,
                        filename=f"{document_id}.pdf",
                        file_type="pdf",
                        file_hash=f"hash-{document_id}",
                        apba_id=apba_id,
                        source_system="PUBLIC_PORTAL",
                    )
                )
                repository.save_processing_result(
                    document_id,
                    [],
                    [
                        Chunk(
                            chunk_id=f"{document_id}-chunk",
                            document_id=document_id,
                            chunk_type="article",
                            text="clean",
                            metadata={"article_no": "\uc81c1\uc870", "article_title": "\ubaa9\uc801"},
                        )
                    ],
                    [],
                )

            report = build_approval_worklist(data_dir=settings.data_dir, source_system="PUBLIC_PORTAL", apba_id="C0002")

        self.assertEqual(1, report["document_count"])
        self.assertEqual("doc-b", report["documents"][0]["document_id"])
        self.assertEqual("bulk_review_candidate", report["documents"][0]["suggested_action"])

    def test_supplementary_effective_date_chunks_need_manual_attention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc-temporal",
                    filename="temporal.pdf",
                    file_type="pdf",
                    file_hash="hash-temporal",
                    apba_id="C0001",
                    source_system="PUBLIC_PORTAL",
                )
            )
            repository.save_processing_result(
                "doc-temporal",
                [],
                [
                    Chunk(
                        chunk_id="temporal-chunk",
                        document_id="doc-temporal",
                        chunk_type="supplementary_provision",
                        text="부칙 이 규정은 공포한 날부터 시행한다.",
                        metadata={"is_supplementary_provision": True, "supplementary_label": "부칙"},
                    )
                ],
                [],
            )

            report = build_approval_worklist(data_dir=settings.data_dir, source_system="PUBLIC_PORTAL", apba_id="C0001")

        self.assertEqual(0, report["bulk_review_candidate_chunks"])
        self.assertEqual(1, report["manual_attention_chunks"])
        self.assertIn("supplementary_or_effective_date_candidate", report["documents"][0]["top_attention_reasons"])

    def test_needs_review_status_without_extra_reason_stays_manual_attention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc-needs-review",
                    filename="needs.pdf",
                    file_type="pdf",
                    file_hash="hash-needs-review",
                    source_system="PUBLIC_PORTAL",
                )
            )
            repository.save_processing_result(
                "doc-needs-review",
                [],
                [
                    Chunk(
                        chunk_id="needs-review-chunk",
                        document_id="doc-needs-review",
                        chunk_type="article",
                        text="operator must inspect this chunk",
                        approval_status="needs_review",
                    )
                ],
                [],
            )

            report = build_approval_worklist(data_dir=settings.data_dir, source_system="PUBLIC_PORTAL")

        self.assertEqual({"needs_review": 1}, report["approval_status_totals"])
        self.assertEqual(0, report["bulk_review_candidate_chunks"])
        self.assertEqual(1, report["manual_attention_chunks"])
        self.assertEqual("manual_review_first", report["documents"][0]["suggested_action"])
        self.assertIn("approval_status_needs_review", report["documents"][0]["top_attention_reasons"])

    def test_text_only_supplementary_effective_date_needs_manual_attention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc-text-temporal",
                    filename="text-temporal.pdf",
                    file_type="pdf",
                    file_hash="hash-text-temporal",
                    source_system="PUBLIC_PORTAL",
                )
            )
            repository.save_processing_result(
                "doc-text-temporal",
                [],
                [
                    Chunk(
                        chunk_id="text-temporal-chunk",
                        document_id="doc-text-temporal",
                        chunk_type="article",
                        text="부칙 이 규정은 공포한 날부터 시행한다.",
                    )
                ],
                [],
            )

            report = build_approval_worklist(data_dir=settings.data_dir, source_system="PUBLIC_PORTAL")

        self.assertEqual(0, report["bulk_review_candidate_chunks"])
        self.assertEqual(1, report["manual_attention_chunks"])
        self.assertEqual("manual_review_first", report["documents"][0]["suggested_action"])
        self.assertIn("supplementary_or_effective_date_candidate", report["documents"][0]["top_attention_reasons"])

    def test_generic_parser_warning_stays_manual_attention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc-warning",
                    filename="warning.pdf",
                    file_type="pdf",
                    file_hash="hash-warning",
                    source_system="PUBLIC_PORTAL",
                )
            )
            repository.save_processing_result(
                "doc-warning",
                [],
                [
                    Chunk(
                        chunk_id="warning-chunk",
                        document_id="doc-warning",
                        chunk_type="article",
                        text="fallback structure text",
                        warnings=["structure_fallback_document_chunk"],
                    )
                ],
                [],
            )

            report = build_approval_worklist(data_dir=settings.data_dir, source_system="PUBLIC_PORTAL")

        self.assertEqual(0, report["bulk_review_candidate_chunks"])
        self.assertEqual(1, report["manual_attention_chunks"])
        self.assertEqual("manual_review_first", report["documents"][0]["suggested_action"])
        self.assertIn("warning:structure_fallback_document_chunk", report["documents"][0]["top_attention_reasons"])

    def test_parser_uncertainty_routes_chunk_to_manual_attention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc-parser-uncertainty",
                    filename="parser-uncertainty.pdf",
                    file_type="pdf",
                    file_hash="hash-parser-uncertainty",
                    source_system="PUBLIC_PORTAL",
                )
            )
            repository.save_processing_result(
                "doc-parser-uncertainty",
                [],
                [
                    Chunk(
                        chunk_id="parser-uncertain",
                        document_id="doc-parser-uncertainty",
                        chunk_type="article",
                        text="\uc81c1\uc870(\ubaa9\uc801) body",
                        metadata={
                            "article_no": "\uc81c1\uc870",
                            "article_title": "\ubaa9\uc801",
                            "parser_uncertainty_source": "pdf",
                            "parser_uncertainty_risk_level": "high",
                            "parser_uncertainty_flags": ["ocr_required"],
                            "parser_uncertainty_recommendation": "run_ocr",
                        },
                    )
                ],
                [],
            )

            report = build_approval_worklist(data_dir=settings.data_dir, source_system="PUBLIC_PORTAL")

        self.assertEqual(1, report["manual_attention_chunks"])
        self.assertEqual(1, report["blocking_review_chunks"])
        self.assertEqual("manual_review_first", report["documents"][0]["suggested_action"])
        self.assertIn("parser_uncertainty_blocker", report["documents"][0]["top_attention_reasons"])
        self.assertIn("parser_uncertainty_flags:ocr_required", report["documents"][0]["top_attention_reasons"])

    def test_orphan_preamble_warning_is_low_risk_bulk_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc-preamble",
                    filename="preamble.pdf",
                    file_type="pdf",
                    file_hash="hash-preamble",
                    source_system="PUBLIC_PORTAL",
                )
            )
            repository.save_processing_result(
                "doc-preamble",
                [],
                [
                    Chunk(
                        chunk_id="doc-preamble-preamble",
                        document_id="doc-preamble",
                        chunk_type="paragraph",
                        text="preamble text",
                        warnings=["orphan_preamble_text"],
                    )
                ],
                [],
            )

            report = build_approval_worklist(data_dir=settings.data_dir, source_system="PUBLIC_PORTAL")

        self.assertEqual(1, report["bulk_review_candidate_chunks"])
        self.assertEqual(0, report["manual_attention_chunks"])
        self.assertEqual(1, report["no_signal_chunks"])
        self.assertEqual(1, report["low_risk_batch_review_candidate_chunks"])
        self.assertEqual("bulk_review_candidate", report["documents"][0]["suggested_action"])
        self.assertEqual("", report["documents"][0]["top_attention_reasons"])

    def test_supplementary_boilerplate_is_low_risk_bulk_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc-supp-boilerplate",
                    filename="supp.pdf",
                    file_type="pdf",
                    file_hash="hash-supp-boilerplate",
                    source_system="PUBLIC_PORTAL",
                )
            )
            repository.save_processing_result(
                "doc-supp-boilerplate",
                [],
                [
                    Chunk(
                        chunk_id="supp-boilerplate",
                        document_id="doc-supp-boilerplate",
                        chunk_type="supplementary_provision",
                        text="\ubd80\uce59",
                        metadata={
                            "is_supplementary_provision": True,
                            "supplementary_boilerplate": True,
                            "effective_date": "2024-01-01",
                            "article_effective_overrides": [],
                        },
                    )
                ],
                [],
            )

            report = build_approval_worklist(data_dir=settings.data_dir, source_system="PUBLIC_PORTAL")

        self.assertEqual(1, report["bulk_review_candidate_chunks"])
        self.assertEqual(0, report["manual_attention_chunks"])
        self.assertEqual(1, report["no_signal_chunks"])
        self.assertEqual(1, report["low_risk_batch_review_candidate_chunks"])
        self.assertEqual("", report["documents"][0]["top_attention_reasons"])

    def test_stable_table_false_positive_is_low_risk_bulk_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc-table-fp",
                    filename="table-fp.pdf",
                    file_type="pdf",
                    file_hash="hash-table-fp",
                    source_system="PUBLIC_PORTAL",
                )
            )
            repository.save_processing_result(
                "doc-table-fp",
                [],
                [
                    Chunk(
                        chunk_id="stable-table-fp",
                        document_id="doc-table-fp",
                        chunk_type="paragraph",
                        text="Known table-like heading pattern",
                        metadata={
                            "table_like": True,
                            "table_probable_false_positive": True,
                            "table_false_positive_stability": "stable",
                        },
                    )
                ],
                [],
            )

            report = build_approval_worklist(data_dir=settings.data_dir, source_system="PUBLIC_PORTAL")

        self.assertEqual(1, report["bulk_review_candidate_chunks"])
        self.assertEqual(0, report["manual_attention_chunks"])
        self.assertEqual(1, report["stable_false_positive_chunks"])
        self.assertEqual(1, report["low_risk_batch_review_candidate_chunks"])
        self.assertEqual("bulk_review_candidate", report["documents"][0]["suggested_action"])

    def test_tenant_isolated_runtime_is_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", tenant_storage_isolation=True)
            tenant_settings = settings_for_tenant(settings, "tenant-a")
            repository = JsonRepository(tenant_settings)
            repository.upsert_document(
                Document(
                    document_id="doc-tenant",
                    filename="tenant.pdf",
                    file_type="pdf",
                    file_hash="hash-tenant",
                    source_system="PUBLIC_PORTAL",
                )
            )
            repository.save_processing_result(
                "doc-tenant",
                [],
                [
                    Chunk(
                        chunk_id="tenant-chunk",
                        document_id="doc-tenant",
                        chunk_type="article",
                        text="clean",
                        metadata={"article_no": "\uc81c1\uc870", "article_title": "\ubaa9\uc801"},
                    )
                ],
                [],
            )

            report = build_approval_worklist(
                data_dir=settings.data_dir,
                tenant_id="tenant-a",
                tenant_storage_isolation=True,
                source_system="PUBLIC_PORTAL",
            )

        self.assertEqual(1, report["document_count"])
        self.assertEqual("tenant-a", report["tenant_id"])
        self.assertEqual(str(tenant_settings.data_dir), report["effective_data_dir"])
        self.assertEqual("bulk_review_candidate", report["documents"][0]["suggested_action"])


if __name__ == "__main__":
    unittest.main()
