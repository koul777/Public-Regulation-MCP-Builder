from __future__ import annotations

import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.ingestion.vector_adapter import VECTOR_RECORD_SCHEMA_VERSION, stable_content_hash
from scripts.estimate_embedding_cost import estimate_embedding_cost, main, to_markdown


class EstimateEmbeddingCostTests(unittest.TestCase):
    def test_estimates_tokens_without_price_or_api_calls(self) -> None:
        estimate = estimate_embedding_cost([_record("doc:chunk-1", "abcd"), _record("doc:chunk-2", "abcde")])

        self.assertEqual(estimate["record_count"], 2)
        self.assertEqual(estimate["estimated_input_tokens"], 3)
        self.assertEqual(estimate["mode"], "estimate_only")
        self.assertEqual(estimate["api_call_count"], 0)
        self.assertEqual(estimate["budget_evaluation_status"], "token_only")
        self.assertIn("does not call an embedding API", to_markdown(estimate))

    def test_marks_budget_unknown_when_price_is_missing_for_billable_tokens(self) -> None:
        estimate = estimate_embedding_cost([_record("doc:chunk-1", "abcd")], budget=Decimal("0.01"))

        self.assertIsNone(estimate["estimated_total_cost"])
        self.assertIsNone(estimate["budget_exceeded"])
        self.assertEqual(estimate["budget_evaluation_status"], "unknown_price")

    def test_estimates_cost_and_budget_from_operator_price(self) -> None:
        estimate = estimate_embedding_cost(
            [_record("doc:chunk-1", "a" * 4_000_000)],
            price_per_1m_tokens=Decimal("2.50"),
            budget=Decimal("2.00"),
            provider_model="future-embedding-model",
        )

        self.assertEqual(estimate["estimated_input_tokens"], 1_000_000)
        self.assertEqual(estimate["estimated_total_cost"], "2.5")
        self.assertTrue(estimate["budget_exceeded"])
        self.assertEqual(estimate["provider_model"], "future-embedding-model")

    def test_safety_margin_is_applied(self) -> None:
        estimate = estimate_embedding_cost(
            [_record("doc:chunk-1", "abcdefgh")],
            chars_per_token=4,
            token_safety_margin=Decimal("1.5"),
        )

        self.assertEqual(estimate["estimated_input_tokens_raw"], 2)
        self.assertEqual(estimate["estimated_input_tokens"], 3)

    def test_fail_over_budget_returns_failure_when_budget_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records_jsonl = root / "records.jsonl"
            records_jsonl.write_text(json.dumps(_record("doc:chunk-1", "text"), ensure_ascii=False) + "\n", encoding="utf-8")
            with patch(
                "sys.argv",
                [
                    "estimate_embedding_cost.py",
                    "--records-jsonl",
                    str(records_jsonl),
                    "--budget",
                    "0.01",
                    "--fail-over-budget",
                ],
            ), patch("sys.stdout", new_callable=io.StringIO):
                exit_code = main()

        self.assertEqual(exit_code, 2)


def _record(record_id: str, text: str) -> dict:
    metadata = {
        "document_id": "doc",
        "tenant_id": "tenant-a",
        "chunk_id": record_id.rsplit(":", 1)[-1],
        "profile_id": "public_portal",
        "approval_status": "approved",
        "approval_id": f"approval-{record_id.rsplit(':', 1)[-1]}",
        "approved_content_hash": "d" * 64,
        "security_level": "internal",
        "approval_worklist_report_path": "reports/approval_worklist_current.json",
        "approval_worklist_report_sha256": "a" * 64,
        "approval_review_batch_manifest_path": "reports/approval_review_batches_current.json",
        "approval_review_batch_manifest_sha256": "b" * 64,
        "approval_review_batch_id": "approval-batch-001",
        "approval_review_batch_chunk_fingerprint": "c" * 64,
        "approval_review_strategy": "human_bulk_review",
    }
    return {
        "schema_version": VECTOR_RECORD_SCHEMA_VERSION,
        "id": record_id,
        "document_id": "doc",
        "tenant_id": "tenant-a",
        "chunk_id": metadata["chunk_id"],
        "text": text,
        "metadata": metadata,
        "content_hash": stable_content_hash(text, metadata),
    }


if __name__ == "__main__":
    unittest.main()
