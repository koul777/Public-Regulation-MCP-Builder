from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


AI_DECISION_REFLECT = "reflect"
AI_DECISION_SKIP = "skip"
AI_DECISION_VALUES = {AI_DECISION_REFLECT, AI_DECISION_SKIP}

APPROVAL_REVIEW_EVENT_TYPES = {
    "ai_review_confirmed",
    "human_review_confirmed",
    "approved",
    "held",
    "approved_without_review",
}


def approval_review_completion_state(
    item_ids: list[str],
    ai_decisions: dict[str, str],
    *,
    human_confirmed: bool,
) -> dict[str, Any]:
    """Return tab completion state without changing approval status."""
    unique_item_ids = [str(item_id) for item_id in dict.fromkeys(item_ids) if str(item_id).strip()]
    normalized_decisions = {
        str(item_id): str(decision)
        for item_id, decision in ai_decisions.items()
        if str(item_id) in set(unique_item_ids) and str(decision) in AI_DECISION_VALUES
    }
    reflected = sum(1 for decision in normalized_decisions.values() if decision == AI_DECISION_REFLECT)
    skipped = sum(1 for decision in normalized_decisions.values() if decision == AI_DECISION_SKIP)
    undecided = [item_id for item_id in unique_item_ids if item_id not in normalized_decisions]
    ai_confirmed = not undecided
    return {
        "item_ids": unique_item_ids,
        "ai_decisions": normalized_decisions,
        "ai_confirmed": ai_confirmed,
        "human_confirmed": bool(human_confirmed),
        "approve_enabled": bool(ai_confirmed and human_confirmed),
        "total": len(unique_item_ids),
        "reflected": reflected,
        "skipped": skipped,
        "remaining": len(undecided),
        "undecided_item_ids": undecided,
    }


def canonical_review_status(status: object) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"draft", "needs_review", "pending", "pending_human_review", ""}:
        return "pending_human_review"
    if normalized in {"reviewed", "human_reviewed"}:
        return "reviewed"
    if normalized == "approved":
        return "approved"
    return normalized


def approval_state_transition(previous_statuses: list[object]) -> dict[str, Any]:
    raw_statuses = [str(status or "").strip() or "draft" for status in previous_statuses]
    logical_from = sorted({canonical_review_status(status) for status in raw_statuses})
    return {
        "previous_statuses": raw_statuses,
        "from_statuses": logical_from,
        "required_sequence": ["pending_human_review", "reviewed", "approved"],
        "reviewed_step_recorded_in": "review_decision_events",
        "final_status": "approved",
    }


def utc_event_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_approval_review_events(
    *,
    chunk_id: str,
    actor: str,
    item_ids: list[str],
    ai_decisions: dict[str, str],
    human_confirmed: bool,
    table_source: str | None = None,
    kordoc_table_promoted: bool = False,
    approve_event: str | None = None,
    override_reason: str | None = None,
    timestamp: str | None = None,
) -> list[dict[str, Any]]:
    state = approval_review_completion_state(item_ids, ai_decisions, human_confirmed=human_confirmed)
    event_time = timestamp or utc_event_timestamp()
    source_of_truth = {
        "table_source": str(table_source or ""),
        "kordoc_table_promoted": bool(kordoc_table_promoted),
    }
    events: list[dict[str, Any]] = []
    if state["ai_confirmed"]:
        events.append(
            {
                "event": "ai_review_confirmed",
                "timestamp": event_time,
                "actor": actor,
                "chunk_id": chunk_id,
                "ai_reflected": state["reflected"],
                "ai_skipped": state["skipped"],
                "ai_total": state["total"],
                "ai_decisions": state["ai_decisions"],
                "source_of_truth": source_of_truth,
            }
        )
    if human_confirmed:
        events.append(
            {
                "event": "human_review_confirmed",
                "timestamp": event_time,
                "actor": actor,
                "chunk_id": chunk_id,
                "source_of_truth": source_of_truth,
            }
        )
    if approve_event:
        event_name = "approved_without_review" if override_reason else approve_event
        events.append(
            {
                "event": event_name,
                "timestamp": event_time,
                "actor": actor,
                "chunk_id": chunk_id,
                "override_reason": str(override_reason or ""),
                "source_of_truth": source_of_truth,
            }
        )
    return sanitize_review_decision_events(events)


def apply_ai_review_decisions_to_preview_text(
    text: str,
    review_items: list[dict[str, Any]],
    ai_decisions: dict[str, str],
) -> str:
    """Apply reflected AI suggestions to a preview string without approving anything."""
    reflected: list[str] = []
    for item in review_items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("item_id") or "")
        if ai_decisions.get(item_id) != AI_DECISION_REFLECT:
            continue
        suggestion = str(item.get("suggestion") or "").strip()
        title = str(item.get("title") or item_id).strip()
        if suggestion:
            reflected.append(f"- {title}: {suggestion}")
    if not reflected:
        return str(text or "")
    return str(text or "").rstrip() + "\n\n[AI 제안 반영 미리보기]\n" + "\n".join(reflected)


def sanitize_review_decision_events(events: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    clean_events: list[dict[str, Any]] = []
    for raw in events or []:
        if not isinstance(raw, dict):
            continue
        event_name = str(raw.get("event") or "").strip()
        if event_name not in APPROVAL_REVIEW_EVENT_TYPES:
            continue
        clean: dict[str, Any] = {
            "event": event_name,
            "timestamp": str(raw.get("timestamp") or utc_event_timestamp())[:80],
            "actor": str(raw.get("actor") or "")[:120],
            "chunk_id": str(raw.get("chunk_id") or "")[:160],
        }
        for key in ("override_reason", "table_source"):
            if raw.get(key) is not None:
                clean[key] = str(raw.get(key) or "")[:1000]
        if isinstance(raw.get("source_of_truth"), dict):
            source = raw["source_of_truth"]
            clean["source_of_truth"] = {
                "table_source": str(source.get("table_source") or "")[:120],
                "kordoc_table_promoted": bool(source.get("kordoc_table_promoted")),
            }
        for key in ("ai_reflected", "ai_skipped", "ai_total"):
            if raw.get(key) is not None:
                try:
                    clean[key] = max(0, int(raw.get(key) or 0))
                except (TypeError, ValueError):
                    clean[key] = 0
        if isinstance(raw.get("ai_decisions"), dict):
            clean["ai_decisions"] = {
                str(item_id)[:160]: str(decision)
                for item_id, decision in raw["ai_decisions"].items()
                if str(decision) in AI_DECISION_VALUES
            }
        clean_events.append(clean)
    return clean_events
