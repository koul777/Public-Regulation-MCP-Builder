from __future__ import annotations

import hashlib
import json

from app.schemas.authoring import AuthoringProject


def semantic_content_hash(project: AuthoringProject) -> str:
    """Return the canonical hash for user-authored regulation content."""

    encoded = json.dumps(
        semantic_content_payload(project),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def semantic_content_payload(project: AuthoringProject) -> dict[str, object]:
    """Project the fields covered by review and content-freeze integrity."""

    return {
        "schema_version": 1,
        "authoring_mode": project.authoring_mode.value,
        "template_id": project.template_id,
        "template_version": project.template_version,
        "title": project.title,
        "purpose": project.purpose,
        "scope": project.scope,
        "legal_bases": list(project.legal_bases),
        "responsible_department": project.responsible_department,
        "planned_effective_date": (
            project.planned_effective_date.isoformat()
            if project.planned_effective_date is not None
            else None
        ),
        "revision_reason": project.revision_reason,
        "predecessor_reference": project.predecessor_reference,
        "clauses": [
            {
                "clause_id": str(clause.clause_id),
                "node_type": clause.node_type.value,
                "parent_id": str(clause.parent_id) if clause.parent_id else None,
                "order": clause.order,
                "article_number": clause.article_number,
                "title": clause.title,
                "body": clause.body,
                "required": clause.required,
                "reference_ids": [
                    str(reference_id) for reference_id in clause.reference_ids
                ],
            }
            for clause in project.clauses
        ],
        "references": [
            {
                "reference_id": str(reference.reference_id),
                "citation": reference.citation,
                "source_title": reference.source_title,
                "source_url": reference.source_url,
                "notes": reference.notes,
                "captured_at": reference.captured_at.isoformat(),
            }
            for reference in project.references
        ],
    }
