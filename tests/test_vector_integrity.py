from __future__ import annotations

import math
import unittest

from app.ingestion.embedding_adapter import embed_vector_record
from app.ingestion.vector_adapter import VECTOR_RECORD_SCHEMA_VERSION, with_vector_record_verification
from app.ingestion.vector_integrity import embedded_vector_integrity_reason
from app.ingestion.vector_upsert import validate_vector_records


def _embedded(text: str = "제1조 목적 휴직 절차") -> dict:
    metadata = {
        "document_id": "doc",
        "tenant_id": "tenant-a",
        "chunk_id": "chunk-1",
        "profile_id": "public_portal",
        "approval_status": "approved",
        "approval_id": "approval-chunk-1",
        "approved_content_hash": "approved-hash-chunk-1",
        "security_level": "internal",
        "approval_worklist_report_path": "reports/approval_worklist_current.json",
        "approval_worklist_report_sha256": "a" * 64,
        "approval_review_batch_manifest_path": "reports/approval_review_batches_current.json",
        "approval_review_batch_manifest_sha256": "b" * 64,
        "approval_review_batch_id": "approval-batch-001",
        "approval_review_batch_chunk_fingerprint": "c" * 64,
        "approval_review_strategy": "human_bulk_review",
    }
    record = with_vector_record_verification(
        {
            "schema_version": VECTOR_RECORD_SCHEMA_VERSION,
            "id": "doc:chunk-1",
            "document_id": "doc",
            "tenant_id": "tenant-a",
            "chunk_id": "chunk-1",
            "text": text,
            "metadata": metadata,
            "content_hash": "",
        }
    )
    return embed_vector_record(record, dimensions=16)


def _reference_reason(record: dict) -> str:
    """Verbatim copy of embedded_vector_integrity_reason before the C-level scans."""

    from app.ingestion.embedding_adapter import (
        EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION,
        LOCAL_HASH_EMBEDDING_MODEL,
        MAX_EMBEDDING_DIMENSIONS,
        local_hash_embedding,
        stable_embedding_hash,
    )

    if record.get("schema_version") != EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION:
        return ""
    embedding = record.get("embedding")
    if not isinstance(embedding, list) or not embedding:
        return "missing_embedding"
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in embedding):
        return "embedding_non_numeric"
    if any(not math.isfinite(float(value)) for value in embedding):
        return "embedding_non_finite"
    dimensions = record.get("embedding_dimensions")
    if (
        not isinstance(dimensions, int)
        or isinstance(dimensions, bool)
        or dimensions != len(embedding)
        or not 1 <= dimensions <= MAX_EMBEDDING_DIMENSIONS
    ):
        return "embedding_dimensions_invalid"
    expected_hash = stable_embedding_hash([float(value) for value in embedding])
    if str(record.get("embedding_hash") or "") != expected_hash:
        return "embedding_hash_mismatch"
    if str(record.get("embedding_model") or "") == LOCAL_HASH_EMBEDDING_MODEL:
        text = str(record.get("text") or "").strip()
        dimensions = len(embedding)
        if [float(value) for value in embedding] != local_hash_embedding(text, dimensions=dimensions):
            return "embedding_vector_mismatch"
    return ""


class EmbeddedVectorIntegrityEquivalenceTests(unittest.TestCase):
    def _mutations(self) -> dict[str, dict]:
        base = _embedded()
        cases: dict[str, dict] = {"valid": base}

        def variant(name: str, **changes) -> None:
            record = dict(base)
            record.update(changes)
            cases[name] = record

        variant("missing", embedding=[])
        variant("not_a_list", embedding="oops")
        variant("bool_item", embedding=[True] + base["embedding"][1:])
        variant("string_item", embedding=["x"] + base["embedding"][1:])
        variant("none_item", embedding=[None] + base["embedding"][1:])
        variant("nan_item", embedding=[math.nan] + base["embedding"][1:])
        variant("inf_item", embedding=[math.inf] + base["embedding"][1:])
        variant("int_items", embedding=[1] + base["embedding"][1:])
        variant("wrong_dimensions", embedding_dimensions=17)
        variant("bool_dimensions", embedding_dimensions=True)
        variant("hash_mismatch", embedding_hash="0" * 64)
        variant("tampered_vector", embedding=[0.5] + base["embedding"][1:])
        variant("tampered_text", text="다른 본문")
        variant("other_model", embedding_model="other-model")
        variant("other_schema", schema_version="reg-rag-vector-record-v1")
        return cases

    def test_reason_matches_the_reference_for_valid_and_tampered_records(self) -> None:
        for name, record in self._mutations().items():
            with self.subTest(name=name):
                self.assertEqual(_reference_reason(record), embedded_vector_integrity_reason(record))

    def test_each_tamper_is_still_reported(self) -> None:
        cases = self._mutations()
        self.assertEqual("", embedded_vector_integrity_reason(cases["valid"]))
        self.assertEqual("missing_embedding", embedded_vector_integrity_reason(cases["missing"]))
        self.assertEqual("embedding_non_numeric", embedded_vector_integrity_reason(cases["bool_item"]))
        self.assertEqual("embedding_non_numeric", embedded_vector_integrity_reason(cases["string_item"]))
        self.assertEqual("embedding_non_finite", embedded_vector_integrity_reason(cases["nan_item"]))
        self.assertEqual("embedding_non_finite", embedded_vector_integrity_reason(cases["inf_item"]))
        self.assertEqual("embedding_dimensions_invalid", embedded_vector_integrity_reason(cases["wrong_dimensions"]))
        self.assertEqual("embedding_hash_mismatch", embedded_vector_integrity_reason(cases["hash_mismatch"]))
        self.assertEqual("embedding_vector_mismatch", embedded_vector_integrity_reason(cases["tampered_text"]))

    def test_validate_vector_records_rejects_non_numeric_embeddings(self) -> None:
        cases = self._mutations()
        self.assertEqual(1, len(validate_vector_records([cases["valid"]])))
        for name in ("bool_item", "string_item", "none_item"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "embedding must contain only numbers"):
                    validate_vector_records([cases[name]])
        with self.assertRaisesRegex(ValueError, "failed integrity check: embedding_non_finite"):
            validate_vector_records([cases["nan_item"]])


if __name__ == "__main__":
    unittest.main()
