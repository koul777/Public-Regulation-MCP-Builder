"""State transitions for institution-scoped regulation versions."""

from __future__ import annotations

from datetime import datetime, timezone

from app.schemas.document import Document


MANUAL_REGULATION_STATUSES = frozenset({"draft", "pending_approval", "superseded", "repealed"})
REGULATION_STATUS_TRANSITIONS = {
    "draft": frozenset({"pending_approval"}),
    "pending_approval": frozenset({"draft"}),
    "approved": frozenset({"superseded", "repealed"}),
    "superseded": frozenset({"repealed"}),
    "repealed": frozenset(),
}


class RegulationLifecycleError(ValueError):
    """Raised when a regulation version cannot make the requested transition."""


def validate_transition(document: Document, target_status: str, *, reason: str = "") -> str:
    current_status = str(document.regulation_status or "draft").strip().lower()
    normalized_target = str(target_status or "").strip().lower()
    if normalized_target not in MANUAL_REGULATION_STATUSES:
        raise RegulationLifecycleError(
            "Manual lifecycle transitions support draft, pending_approval, superseded, and repealed."
        )
    if normalized_target == current_status:
        raise RegulationLifecycleError(f"Regulation is already in status '{current_status}'.")
    if normalized_target not in REGULATION_STATUS_TRANSITIONS.get(current_status, frozenset()):
        raise RegulationLifecycleError(
            f"Invalid regulation lifecycle transition: {current_status} -> {normalized_target}."
        )
    if normalized_target in {"superseded", "repealed"} and not str(reason or "").strip():
        raise RegulationLifecycleError(f"A reason is required when marking a regulation {normalized_target}.")
    if normalized_target in {"pending_approval", "superseded", "repealed"}:
        _require_version_identity(document)
    return normalized_target


def apply_transition(document: Document, target_status: str, *, reason: str, actor: str) -> tuple[Document, dict]:
    normalized_target = validate_transition(document, target_status, reason=reason)
    update_fields = {"regulation_status": normalized_target}
    if normalized_target == "repealed" and not str(document.repealed_at or "").strip():
        update_fields["repealed_at"] = datetime.now(timezone.utc).date().isoformat()
    updated = document.model_copy(update=update_fields)
    event = {
        "event_id": f"regulation_lifecycle_{document.document_id}_{int(datetime.now(timezone.utc).timestamp() * 1000000)}",
        "event_type": "regulation_lifecycle_transition",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "document_id": document.document_id,
        "tenant_id": document.tenant_id,
        "profile_id": document.profile_id,
        "regulation_id": document.regulation_id,
        "regulation_version": document.regulation_version,
        "from_status": document.regulation_status,
        "to_status": normalized_target,
        "repealed_at": updated.repealed_at,
        "reason": str(reason or "").strip(),
        "actor": str(actor or "").strip(),
    }
    return updated, event


def _require_version_identity(document: Document) -> None:
    missing = [
        label
        for label, value in {
            "regulation_id": document.regulation_id,
            "regulation_version": document.regulation_version,
            "effective_from": document.effective_from,
        }.items()
        if not str(value or "").strip()
    ]
    if missing:
        raise RegulationLifecycleError(
            "A regulation identifier, version, and effective start date are required: "
            + ", ".join(missing)
        )
