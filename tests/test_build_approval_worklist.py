from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest import mock

from app.core.config import Settings
from app.core.tenant_access import settings_for_tenant
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository
from scripts import build_approval_worklist as worklist_module
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


def _reference_json_safe(value):
    """Verbatim copy of the pre-optimization _json_safe, kept as the oracle."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {
            str(key): _reference_json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set)):
        return [_reference_json_safe(item) for item in value]
    return str(value)


def _reference_review_content_hash(chunk) -> str:
    """Pre-optimization review_content_hash: separate dump, always rebuilds via _json_safe."""
    row = worklist_module.chunk_to_review_dict(chunk)
    text_basis, text = worklist_module._review_text_basis(row)
    payload = {
        "schema_version": worklist_module.REVIEW_CONTENT_HASH_VERSION,
        "chunk_type": worklist_module.clean_text(row.get("chunk_type")),
        "source_page_start": row.get("source_page_start"),
        "source_page_end": row.get("source_page_end"),
        "text_basis": text_basis,
        "text": text,
        "metadata": _reference_json_safe(row.get("metadata") or {}),
        "warnings": _reference_json_safe(row.get("warnings") or []),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class _Opaque:
    def __str__(self) -> str:
        return "opaque-value"


class _StrSub(str):
    pass


class _IntSub(int):
    pass


class ReviewContentHashEquivalenceTests(unittest.TestCase):
    """The shared-row / plain-JSON fast path must keep every review_content_hash byte-identical."""

    METADATA_CASES = [
        {},
        {"article_no": "\uc81c1\uc870", "article_title": "\ubaa9\uc801"},
        {"nested": {"b": [1, 2.5, None, True, "x"], "a": {"z": [], "y": {}}}, "k": "\ud55c\uae00 \u2603"},
        {"floats": [float("nan"), float("inf"), -0.0, 1e300], "big": 2**70},
        {"tuple": (1, 2, ("a", "b")), "set": {3}, "opaque": _Opaque()},
        {1: "int-key", "1": "str-key-collides", None: "none-key", True: "bool-key"},
        {2: "b", 10: "c", 1: "a"},
        {"str_sub": _StrSub("sub"), "int_sub": _IntSub(7), "ordered": OrderedDict([("b", 1), ("a", 2)])},
        {"deep": [[[[[[[{"leaf": ["x"]}]]]]]]]},
        {"shared": [1, 2, 3]},
    ]

    def test_hash_matches_reference_for_dict_chunks(self) -> None:
        for index, metadata in enumerate(self.METADATA_CASES):
            for warnings in ([], ["w1", "\ud55c\uae00"], [("t", 1)], None):
                chunk = {
                    "chunk_id": f"c{index}",
                    "document_id": "d",
                    "chunk_type": "article",
                    "text": "  body text  ",
                    "retrieval_text": "",
                    "normalized_text": "normalized",
                    "source_page_start": 1,
                    "source_page_end": 2,
                    "metadata": metadata,
                    "warnings": warnings,
                    "approval_status": "draft",
                }
                with self.subTest(index=index, warnings=warnings):
                    self.assertEqual(
                        _reference_review_content_hash(chunk),
                        worklist_module.review_content_hash(chunk),
                    )
                    row = worklist_module.approval_chunk_row(chunk)
                    self.assertEqual(_reference_review_content_hash(chunk), row["review_content_hash"])

    def test_hash_matches_reference_for_model_chunks(self) -> None:
        for index, metadata in enumerate(self.METADATA_CASES[:5] + self.METADATA_CASES[8:]):
            try:
                chunk = Chunk(
                    chunk_id=f"m{index}",
                    document_id="d",
                    chunk_type="table" if index % 2 else "article",
                    text="body",
                    metadata=metadata,
                    warnings=["w"] if index % 3 == 0 else [],
                )
                chunk.model_dump(mode="json")
            except Exception:  # unsupported metadata shapes (for example opaque objects) cannot be model inputs
                continue
            with self.subTest(index=index):
                self.assertEqual(
                    _reference_review_content_hash(chunk),
                    worklist_module.review_content_hash(chunk),
                )
                self.assertEqual(
                    _reference_review_content_hash(chunk),
                    worklist_module.approval_chunk_row(chunk)["review_content_hash"],
                )

    def test_approval_chunk_row_keeps_signal_and_hash_independent_of_sharing(self) -> None:
        chunk = Chunk(
            chunk_id="shared-row",
            document_id="d",
            chunk_type="table",
            text="table",
            metadata={"table_review_required": True, "table_review_flags": ["row_review_required"]},
        )
        row = worklist_module.approval_chunk_row(chunk)
        signal = worklist_module.chunk_review_signal(chunk)
        self.assertEqual(signal["review_flags"], row["review_flags"])
        self.assertEqual(signal["review_priority_tier"], row["review_priority_tier"])
        self.assertEqual(signal["review_category"], row["review_category"])
        self.assertEqual(_reference_review_content_hash(chunk), row["review_content_hash"])
        self.assertTrue(row["manual_attention"])
        # the model object itself is untouched by the shared dump
        self.assertEqual({"table_review_required": True, "table_review_flags": ["row_review_required"]}, chunk.metadata)

    def test_plain_json_detection_accepts_only_exact_plain_data(self) -> None:
        plain = {"a": [1, 2.0, "x", None, True, {"b": []}], "c": {}}
        self.assertTrue(worklist_module._is_plain_json(plain))
        self.assertTrue(worklist_module._is_plain_json([]))
        self.assertTrue(worklist_module._is_plain_json("text"))
        self.assertTrue(worklist_module._is_plain_json(None))
        for not_plain in (
            {"a": (1, 2)},
            {"a": {1}},
            {"a": _Opaque()},
            {1: "x"},
            {"a": [{"b": {2: "x"}}]},
            {"a": _StrSub("x")},
            {"a": _IntSub(1)},
            OrderedDict(a=1),
            [b"bytes"],
        ):
            with self.subTest(value=repr(not_plain)):
                self.assertFalse(worklist_module._is_plain_json(not_plain))

    def test_hash_safe_is_the_input_for_plain_data_and_json_safe_otherwise(self) -> None:
        plain = {"b": [1, {"z": 1, "a": 2}], "a": "x"}
        self.assertIs(plain, worklist_module._hash_safe(plain))
        mixed = {"k": (1, 2), 3: {"x"}}
        self.assertEqual(_reference_json_safe(mixed), worklist_module._hash_safe(mixed))
        # _json_safe itself is unchanged
        self.assertEqual(_reference_json_safe(mixed), worklist_module._json_safe(mixed))

    def test_cyclic_input_falls_back_to_the_original_failure(self) -> None:
        cyclic: dict = {"a": []}
        cyclic["a"].append(cyclic)
        with mock.patch.object(worklist_module, "_JSON_PLAIN_MAX_CONTAINERS", 500):
            self.assertFalse(worklist_module._is_plain_json(cyclic))
            with self.assertRaises(RecursionError):
                worklist_module._hash_safe(cyclic)
        with self.assertRaises(RecursionError):
            _reference_json_safe(cyclic)

    def test_review_row_is_shared_but_never_mutated(self) -> None:
        chunk = {
            "chunk_id": "r",
            "document_id": "d",
            "chunk_type": "article",
            "text": "body",
            "metadata": {"review_flags": ["x"], "nested": {"k": [1, 2]}},
            "warnings": ["w"],
            "approval_status": "draft",
        }
        before = json.dumps(chunk, sort_keys=True)
        worklist_module.approval_chunk_row(chunk)
        self.assertEqual(before, json.dumps(chunk, sort_keys=True))


class WorklistSkipsNonCandidateRowsTests(unittest.TestCase):
    STATUSES = ("draft", "approved", "needs_review", "rejected", "security_blocked", "approved")

    def _chunks(self) -> list[Chunk]:
        chunks = []
        for index, status in enumerate(self.STATUSES):
            chunks.append(
                Chunk(
                    chunk_id=f"c{index}",
                    document_id="doc-a",
                    chunk_type="table" if index % 2 else "article",
                    text=f"text {index}",
                    approval_status=status,
                    metadata=(
                        {"table_review_required": True, "table_review_flags": ["row_review_required"]}
                        if index % 2
                        else {"article_no": "1", "article_title": "Purpose"}
                    ),
                )
            )
        return chunks

    def _seed(self, tmp: str) -> Settings:
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
        repository.save_processing_result("doc-a", [], self._chunks(), [])
        return settings

    def test_rows_are_built_only_for_chunks_awaiting_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._seed(tmp)
            seen: list[str] = []
            real_row = worklist_module.approval_chunk_row

            def spy(chunk):
                seen.append(chunk.chunk_id)
                return real_row(chunk)

            with mock.patch.object(worklist_module, "approval_chunk_row", spy):
                report = build_approval_worklist(data_dir=settings.data_dir)

        self.assertEqual(["c0", "c2"], seen)
        document = report["documents"][0]
        self.assertEqual(6, document["total_chunks"])
        self.assertEqual(2, document["approved_chunks"])
        self.assertEqual(1, document["draft_chunks"])
        self.assertEqual(1, document["needs_review_chunks"])

    def test_report_equals_the_one_computed_from_rows_of_every_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._seed(tmp)
            report = build_approval_worklist(data_dir=settings.data_dir)
            chunks = JsonRepository(settings).get_chunks("doc-a")

        all_rows = [worklist_module.approval_chunk_row(chunk) for chunk in chunks]
        candidates = [row for row in all_rows if row["approval_status"] in worklist_module.APPROVAL_WORKLIST_STATUSES]
        document = report["documents"][0]
        self.assertEqual(worklist_module.review_candidate_fingerprint(candidates), document["review_candidate_fingerprint"])
        self.assertEqual(
            worklist_module.review_candidate_fingerprint([row for row in candidates if row["manual_attention"]]),
            document["manual_attention_fingerprint"],
        )
        self.assertEqual(sum(1 for row in candidates if row["manual_attention"]), document["manual_attention_chunks"])
        self.assertEqual(
            sum(1 for row in candidates if row["low_risk_batch_candidate"]),
            document["bulk_review_candidate_chunks"],
        )

    def test_chunk_approval_status_matches_the_row_status(self) -> None:
        class Plain:
            def __init__(self, status) -> None:
                self.approval_status = status
                self.chunk_id = "x"
                self.chunk_type = "article"
                self.text = "t"
                self.normalized_text = None
                self.retrieval_text = None
                self.metadata = {}
                self.warnings = []
                self.document_id = "d"

        for status in ("draft", " Needs_Review ", "APPROVED", "", None, "weird"):
            with self.subTest(status=status):
                chunk = Plain(status)
                self.assertEqual(
                    worklist_module.approval_chunk_row(chunk)["approval_status"],
                    worklist_module.chunk_approval_status(chunk),
                )
        self.assertEqual("missing", worklist_module.chunk_approval_status(object()))


if __name__ == "__main__":
    unittest.main()
