from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
import json
from datetime import datetime, timezone
import os
from pathlib import Path
from threading import Lock
import time
from typing import Any
from uuid import uuid4

from app.core.config import Settings


_AUDIT_LOCK = Lock()
_LOCK_POLL_SECONDS = 0.05
_LOCK_TIMEOUT_SECONDS = 30.0

REQUIRED_PROVIDER_EXECUTION_FIELDS = (
    "actor",
    "approval_reference",
    "document_id",
    "run_id",
    "provider",
    "model",
    "budget_reservation_id",
    "prompt_hash",
    "payload_hash",
    "payload_classification",
    "reserved_total_tokens",
    "actual_total_tokens",
    "estimated_cost",
    "actual_cost",
    "provider_request_id",
    "outcome",
)

REQUIRED_ALLOWED_BUDGET_RESERVATION_FIELDS = (
    "reservation_id",
    "created_at",
    "provider",
    "approved_model",
    "actor",
    "approval_reference",
    "mode",
    "allowed",
    "selected_chunk_ids",
    "selected_content_hashes",
    "selected_chunk_count",
    "selected_documents",
    "prompt_hash",
    "prompt_input_tokens",
    "chunk_input_tokens",
    "estimated_input_tokens",
    "estimated_output_tokens",
    "estimated_total_tokens",
    "currency",
    "price_version",
    "price_effective_at",
    "input_price_per_1m_tokens",
    "output_price_per_1m_tokens",
    "estimated_input_cost",
    "estimated_output_cost",
    "estimated_total_cost",
    "max_cost_per_batch",
    "api_call_count",
)


def validate_budget_reservation_record(record: dict[str, Any]) -> None:
    if record.get("allowed") is not True:
        return
    missing = [field for field in REQUIRED_ALLOWED_BUDGET_RESERVATION_FIELDS if record.get(field) in (None, "")]
    if missing:
        raise ValueError(f"Budget reservation audit record is missing required fields: {', '.join(missing)}")
    selected_chunk_ids = record.get("selected_chunk_ids")
    if not isinstance(selected_chunk_ids, list):
        raise ValueError("Budget reservation selected_chunk_ids must be a list.")
    selected_content_hashes = record.get("selected_content_hashes")
    if not isinstance(selected_content_hashes, dict):
        raise ValueError("Budget reservation selected_content_hashes must be a dict.")
    selected_chunk_count = _non_negative_int(record, "selected_chunk_count")
    if selected_chunk_count != len(selected_chunk_ids):
        raise ValueError("Budget reservation selected_chunk_count does not match selected_chunk_ids.")
    missing_hashes = [chunk_id for chunk_id in selected_chunk_ids if not selected_content_hashes.get(chunk_id)]
    if missing_hashes:
        raise ValueError("Budget reservation selected_content_hashes is missing selected chunk ids.")
    api_call_count = _non_negative_int(record, "api_call_count")
    if api_call_count != 0:
        raise ValueError("Budget reservation must be recorded before provider calls.")
    prompt_tokens = _non_negative_int(record, "prompt_input_tokens")
    chunk_tokens = _non_negative_int(record, "chunk_input_tokens")
    input_tokens = _non_negative_int(record, "estimated_input_tokens")
    output_tokens = _non_negative_int(record, "estimated_output_tokens")
    total_tokens = _non_negative_int(record, "estimated_total_tokens")
    if input_tokens < prompt_tokens + chunk_tokens:
        raise ValueError("Budget reservation estimated_input_tokens is below prompt plus chunk tokens.")
    if total_tokens < input_tokens + output_tokens:
        raise ValueError("Budget reservation estimated_total_tokens is below input plus output tokens.")
    for field_name in (
        "input_price_per_1m_tokens",
        "output_price_per_1m_tokens",
        "estimated_input_cost",
        "estimated_output_cost",
        "estimated_total_cost",
        "max_cost_per_batch",
    ):
        _non_negative_decimal(record, field_name)


def append_budget_reservation_record(settings: Settings, record: dict[str, Any]) -> dict[str, Any]:
    validate_budget_reservation_record(record)
    audit_record = {
        "record_id": f"budget_reservation_{uuid4().hex[:12]}",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        **record,
    }
    audit_path = budget_reservation_audit_path(settings)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with _AUDIT_LOCK, _audit_file_lock(audit_path.parent, ".provider_budget_reservation.lock"):
        with audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(audit_record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return audit_record


def budget_reservation_audit_path(settings: Settings) -> Path:
    return settings.data_dir / "repository" / "provider_budget_reservations.jsonl"


def validate_provider_execution_record(record: dict[str, Any]) -> None:
    missing = [field for field in REQUIRED_PROVIDER_EXECUTION_FIELDS if record.get(field) in (None, "")]
    if missing:
        raise ValueError(f"Provider execution audit record is missing required fields: {', '.join(missing)}")
    reserved_tokens = _non_negative_int(record, "reserved_total_tokens")
    actual_tokens = _non_negative_int(record, "actual_total_tokens")
    estimated_cost = _non_negative_decimal(record, "estimated_cost")
    actual_cost = _non_negative_decimal(record, "actual_cost")
    override = record.get("budget_override_reference")
    if actual_tokens > reserved_tokens and not override:
        raise ValueError("Provider execution actual_total_tokens exceeds reserved_total_tokens without override.")
    if actual_cost > estimated_cost and not override:
        raise ValueError("Provider execution actual_cost exceeds estimated_cost without override.")


def append_provider_execution_record(settings: Settings, record: dict[str, Any]) -> dict[str, Any]:
    validate_provider_execution_record(record)
    audit_record = {
        "record_id": f"provider_exec_{uuid4().hex[:12]}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        **record,
    }
    audit_path = provider_execution_audit_path(settings)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with _AUDIT_LOCK, _audit_file_lock(audit_path.parent):
        with audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(audit_record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return audit_record


def provider_execution_audit_path(settings: Settings) -> Path:
    return settings.data_dir / "repository" / "provider_execution_audit.jsonl"


def _non_negative_int(record: dict[str, Any], field_name: str) -> int:
    try:
        value = int(record[field_name])
    except (TypeError, ValueError):
        raise ValueError(f"Provider execution audit field must be an integer: {field_name}") from None
    if value < 0:
        raise ValueError(f"Provider execution audit field must be non-negative: {field_name}")
    return value


def _non_negative_decimal(record: dict[str, Any], field_name: str) -> Decimal:
    try:
        value = Decimal(str(record[field_name]))
    except Exception:
        raise ValueError(f"Provider execution audit field must be numeric: {field_name}") from None
    if not value.is_finite():
        raise ValueError(f"Provider execution audit field must be numeric: {field_name}")
    if value < 0:
        raise ValueError(f"Provider execution audit field must be non-negative: {field_name}")
    return value


@contextmanager
def _audit_file_lock(root: Path, lock_filename: str = ".provider_execution_audit.lock"):
    lock_path = root / lock_filename
    with lock_path.open("a+b") as handle:
        _lock_handle(handle)
        try:
            yield
        finally:
            _unlock_handle(handle)


def _lock_handle(handle) -> None:
    if os.name == "nt":
        import msvcrt

        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for provider audit lock: {handle.name}")
                time.sleep(_LOCK_POLL_SECONDS)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_handle(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
