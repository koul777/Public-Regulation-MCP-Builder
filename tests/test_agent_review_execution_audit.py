from __future__ import annotations

import json
import multiprocessing
import tempfile
import traceback
import unittest
from pathlib import Path

from app.agents.execution_audit import (
    append_budget_reservation_record,
    append_provider_execution_record,
    budget_reservation_audit_path,
    provider_execution_audit_path,
    validate_budget_reservation_record,
    validate_provider_execution_record,
)
from app.core.config import Settings


def audit_record() -> dict:
    return {
        "actor": "service:batch",
        "approval_reference": "approval-123",
        "document_id": "doc_1",
        "run_id": "run_1",
        "provider": "openai",
        "model": "example-model",
        "budget_reservation_id": "agent_budget_123",
        "prompt_hash": "sha256:abc",
        "payload_hash": "sha256:def",
        "payload_classification": "public-regulation-minimal",
        "reserved_total_tokens": 100,
        "actual_total_tokens": 80,
        "estimated_cost": "0.0010",
        "actual_cost": "0.0008",
        "provider_request_id": "req_123",
        "outcome": "succeeded",
    }


def budget_reservation_record() -> dict:
    return {
        "reservation_id": "agent_budget_123",
        "created_at": "2026-07-03T00:00:00+00:00",
        "provider": "openai",
        "approved_model": "example-model",
        "actor": "service:batch",
        "approval_reference": "approval-123",
        "mode": "pre_call_budget_reservation",
        "allowed": True,
        "errors": [],
        "selected_chunk_ids": ["chunk_1", "chunk_2"],
        "selected_content_hashes": {
            "chunk_1": "sha256:" + "b" * 64,
            "chunk_2": "sha256:" + "c" * 64,
        },
        "selected_chunk_count": 2,
        "selected_documents": 1,
        "prompt_hash": "sha256:" + "a" * 64,
        "prompt_input_tokens": 10,
        "chunk_input_tokens": 100,
        "estimated_input_tokens": 110,
        "estimated_output_tokens": 50,
        "estimated_total_tokens": 200,
        "currency": "USD",
        "price_version": "2026-07-03",
        "price_effective_at": "2026-07-03T00:00:00Z",
        "input_price_per_1m_tokens": "1",
        "output_price_per_1m_tokens": "4",
        "estimated_input_cost": "0.0001",
        "estimated_output_cost": "0.0002",
        "estimated_total_cost": "0.0003",
        "max_cost_per_batch": "0.01",
        "api_call_count": 0,
    }


def _append_audit_records(data_dir: str, prefix: str, count: int, queue) -> None:
    try:
        settings = Settings(data_dir=Path(data_dir))
        for index in range(count):
            record = audit_record()
            record["provider_request_id"] = f"{prefix}_req_{index}"
            append_provider_execution_record(settings, record)
        queue.put(None)
    except Exception:  # pragma: no cover - surfaced in parent process
        queue.put(traceback.format_exc())


class AgentReviewExecutionAuditTests(unittest.TestCase):
    def test_appends_budget_reservation_record_as_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))

            saved = append_budget_reservation_record(settings, budget_reservation_record())

            path = budget_reservation_audit_path(settings)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["record_id"], saved["record_id"])
            self.assertEqual(rows[0]["reservation_id"], "agent_budget_123")
            self.assertEqual(rows[0]["api_call_count"], 0)

    def test_rejects_budget_reservation_selected_chunk_count_mismatch(self) -> None:
        record = budget_reservation_record()
        record["selected_chunk_count"] = 3

        with self.assertRaisesRegex(ValueError, "selected_chunk_count"):
            validate_budget_reservation_record(record)

    def test_rejects_budget_reservation_without_prompt_hash(self) -> None:
        record = budget_reservation_record()
        record["prompt_hash"] = ""

        with self.assertRaisesRegex(ValueError, "prompt_hash"):
            validate_budget_reservation_record(record)

    def test_rejects_budget_reservation_without_selected_content_hashes(self) -> None:
        record = budget_reservation_record()
        record["selected_content_hashes"] = {"chunk_1": "sha256:" + "b" * 64}

        with self.assertRaisesRegex(ValueError, "selected_content_hashes"):
            validate_budget_reservation_record(record)

    def test_rejects_missing_required_fields(self) -> None:
        record = audit_record()
        record["approval_reference"] = ""

        with self.assertRaisesRegex(ValueError, "approval_reference"):
            validate_provider_execution_record(record)

    def test_rejects_actual_usage_above_reserved_without_override(self) -> None:
        record = audit_record()
        record["actual_total_tokens"] = 101

        with self.assertRaisesRegex(ValueError, "actual_total_tokens exceeds"):
            validate_provider_execution_record(record)

    def test_rejects_actual_cost_above_estimated_without_override(self) -> None:
        record = audit_record()
        record["actual_cost"] = "0.0011"

        with self.assertRaisesRegex(ValueError, "actual_cost exceeds"):
            validate_provider_execution_record(record)

    def test_allows_budget_override_reference_for_overrun(self) -> None:
        record = audit_record()
        record["actual_total_tokens"] = 101
        record["actual_cost"] = "0.0011"
        record["budget_override_reference"] = "approval-override-1"

        validate_provider_execution_record(record)

    def test_rejects_negative_or_non_numeric_usage(self) -> None:
        record = audit_record()
        record["actual_total_tokens"] = -1

        with self.assertRaisesRegex(ValueError, "non-negative"):
            validate_provider_execution_record(record)

        record = audit_record()
        record["actual_cost"] = "not-a-number"
        with self.assertRaisesRegex(ValueError, "numeric"):
            validate_provider_execution_record(record)

    def test_appends_provider_execution_record_as_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))

            saved = append_provider_execution_record(settings, audit_record())

            path = provider_execution_audit_path(settings)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["record_id"], saved["record_id"])
            self.assertEqual(rows[0]["actor"], "service:batch")
            self.assertEqual(rows[0]["actual_total_tokens"], 80)

    def test_provider_execution_audit_appends_are_safe_across_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = multiprocessing.get_context("spawn")
            queue = ctx.Queue()
            processes = [
                ctx.Process(target=_append_audit_records, args=(tmp, f"proc{index}", 4, queue))
                for index in range(3)
            ]

            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=20)

            errors = [queue.get(timeout=5) for _ in processes]
            for process in processes:
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(errors, [None, None, None])

            path = provider_execution_audit_path(Settings(data_dir=Path(tmp)))
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 12)
            self.assertEqual(len({row["provider_request_id"] for row in rows}), 12)


if __name__ == "__main__":
    unittest.main()
