"""Read-only projection of persisted AI review data for the operator screen."""

from __future__ import annotations

import difflib
import html
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AIReviewDisplay:
    proposal: str
    changed: bool
    diff_html: str
    issues: tuple[str, ...]
    recommendation: str
    risk_level: str


def review_display(chunk: Any, summary: dict | None = None) -> AIReviewDisplay:
    """Expose stored proposals without selecting them as the approved text."""
    original = str(getattr(chunk, "text", "") or "")
    proposal = str(getattr(chunk, "ai_preprocessed_text", "") or "").strip()
    metadata = getattr(chunk, "metadata", None) or {}
    findings = metadata.get("agent_review_findings")
    if not isinstance(findings, dict):
        findings = {}
    if not findings:
        provider = (summary or {}).get("provider_review_json")
        items = provider.get("items", []) if isinstance(provider, dict) else []
        items = items if isinstance(items, list) else []
        findings = next((item for item in items if isinstance(item, dict)
                         and str(item.get("chunk_id") or "") == str(getattr(chunk, "chunk_id", ""))), {})
    changed = bool(proposal and proposal != original.strip())
    parts: list[str] = []
    if changed:
        # Keep whitespace and every original/proposed character visible. All
        # document content is escaped before adding trusted highlight tags.
        before, after = (original, proposal) if max(len(original), len(proposal)) <= 8000 else (
            original.splitlines(keepends=True), proposal.splitlines(keepends=True)
        )
        matcher = difflib.SequenceMatcher(None, before, after)
        for tag, a, b, c, d in matcher.get_opcodes():
            removed, added = "".join(before[a:b]), "".join(after[c:d])
            if tag == "equal":
                parts.append(html.escape(removed))
            else:
                if a != b:
                    parts.append(f'<del>{html.escape(removed)}</del>')
                if c != d:
                    parts.append(f'<ins>{html.escape(added)}</ins>')
    issues = findings.get("issues") or []
    if isinstance(issues, str):
        issues = [issues]
    if not isinstance(issues, (list, tuple)):
        issues = []
    return AIReviewDisplay(
        proposal=proposal, changed=changed, diff_html="".join(parts),
        issues=tuple(str(x).strip() for x in issues if str(x).strip()),
        recommendation=str(findings.get("recommended_human_check") or "").strip(),
        risk_level=str(findings.get("risk_level") or "medium").lower(),
    )
