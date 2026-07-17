from __future__ import annotations

from typing import Any


REAPPROVAL_DECISION_OPERATOR_DECISIONS = (
    "approve_all_reviewed",
    "reject_all",
    "partial_with_overrides",
    "needs_reprocess",
    "defer",
)

REAPPROVAL_APPROVAL_SCOPE_CONFIRMATIONS = ("confirmed", "scope_confirmed")

REAPPROVAL_DECISION_REQUIRED_FIELDS = (
    "operator_decision",
    "reviewer_id",
    "reviewed_at",
    "approval_scope_confirmation",
)
REAPPROVAL_OVERRIDE_DECISIONS = ("approve", "reject", "needs_reprocess", "defer")
REAPPROVAL_OVERRIDE_DECISION_ALIASES = {
    "approve": "approve",
    "approve_chunk": "approve",
    "approve_reviewed": "approve",
    "reapprove": "approve",
    "approve_all_reviewed": "approve",
    "reject": "reject",
    "reject_chunk": "reject",
    "reject_all": "reject",
    "rejected": "reject",
    "needs_reprocess": "needs_reprocess",
    "reprocess": "needs_reprocess",
    "reprocess_chunk": "needs_reprocess",
    "defer": "defer",
    "defer_chunk": "defer",
}


def normalize_operator_decision(value: Any) -> str:
    return str(value or "").strip()


def is_allowed_operator_decision(value: Any) -> bool:
    return normalize_operator_decision(value) in REAPPROVAL_DECISION_OPERATOR_DECISIONS


def normalize_override_decision(value: Any) -> str:
    return REAPPROVAL_OVERRIDE_DECISION_ALIASES.get(str(value or "").strip(), "")


def is_allowed_override_decision(value: Any) -> bool:
    return bool(normalize_override_decision(value))


def row_missing_required_decision_fields(row: dict[str, Any]) -> list[str]:
    return [field for field in REAPPROVAL_DECISION_REQUIRED_FIELDS if not str(row.get(field) or "").strip()]
