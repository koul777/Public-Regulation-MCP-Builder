from __future__ import annotations

"""Recover a source-body witness without treating retrieval augmentation as a quote.

The caller must first enforce current approval, tenant, profile and access policy.
This module additionally binds every returned character to the verified vector
record. A repository body is a witness for exact reconstruction, not a new source
of authority. This proves stored-format correspondence, not human PDF review.
"""

from dataclasses import dataclass
from typing import Any, Mapping

from app.ingestion.vector_adapter import stable_content_hash
from app.processors.answer_profile import ANSWER_PROFILE_MARKER, append_answer_profile_to_retrieval_text


@dataclass(frozen=True)
class SourceText:
    text: str
    status: str


SOURCE_UNAVAILABLE = SourceText("", "unavailable")
_AUGMENTATION_PREFIXES = ("[문서명]", "[위치]", "[본문]", "[답변분류]", "[참조]")
# Generated suffix markers. Each suffix helper skips its marker when it is
# already present, so reconstruction must never feed them one.
_DERIVED_SUFFIX_MARKERS = (ANSWER_PROFILE_MARKER, "[참조]")


def looks_like_augmented_text(text: str) -> bool:
    """Recognize a stored wrapper, without stripping literal markers in its body."""
    return text.lstrip().startswith(_AUGMENTATION_PREFIXES)


def source_text_status_unavailable(item: Any) -> bool:
    """Fail closed for either explicit status; falsey unknown values are not absent."""
    if not isinstance(item, Mapping):
        return False  # The evidence schema handles malformed record objects.
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    allowed = (None, "", "verified_plain", "verified_reconstruction")
    return any(value not in allowed for value in (item.get("source_text_status"), metadata.get("source_text_status")))


def _reference_text(text: str, metadata: Mapping[str, Any]) -> str:
    # Same source-mention format as Chunker._append_reference_retrieval_text.
    edges = metadata.get("reference_edges") or []
    if not edges or not text or "[참조]" in text:
        return text
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for edge in edges:
        if not isinstance(edge, Mapping):
            return ""
        kind = str(edge.get("type") or "reference").strip()
        value = str(edge.get("value") or "").strip()
        if not value or (kind, value) in seen:
            continue
        seen.add((kind, value))
        label = {"regulation": "규정", "article": "조문"}.get(kind, "참조")
        lines.append(f"- {label}: {value}")
        if len(lines) >= 20:
            break
    return f"{text.rstrip()}\n[참조]\n" + "\n".join(lines) if lines else text


def source_text_from_verified_record(
    record: Mapping[str, Any],
    *,
    body_witness: str | None = None,
    table_witness: str | None = None,
    expected_tenant_id: str | None = None,
) -> SourceText:
    """Return only an exact body witness of an intact, approved vector record.

    Wrapper title/path and all derived suffixes come from hash-verified vector
    metadata. Neither a source_text flag nor repository metadata can replace
    those fields. No whitespace-normalized comparison or marker splitting is
    used. Unknown formats and failed witnesses are unavailable for citation.
    A witness that contains a generated suffix marker is also unavailable:
    the idempotent suffix helpers would otherwise let it carry the answer
    profile or reference lines and still rebuild the record exactly.
    """
    metadata = record.get("metadata")
    text = record.get("text")
    if not isinstance(metadata, Mapping) or not isinstance(text, str) or not text:
        return SOURCE_UNAVAILABLE
    if (
        metadata.get("approval_status") != "approved"
        or not metadata.get("approval_id")
        or not metadata.get("approved_content_hash")
        or (expected_tenant_id is not None and metadata.get("tenant_id") != expected_tenant_id)
        or stable_content_hash(text, dict(metadata)) != record.get("content_hash")
    ):
        return SOURCE_UNAVAILABLE
    if not looks_like_augmented_text(text):
        return SourceText(text, "verified_plain")
    if not isinstance(body_witness, str) or not body_witness or body_witness != body_witness.strip():
        return SOURCE_UNAVAILABLE
    # This also prevents a malformed witness from adding even one character
    # outside the authoritative retrieval text, including normalized whitespace.
    if body_witness not in text:
        return SOURCE_UNAVAILABLE
    table = table_witness.strip() if isinstance(table_witness, str) else ""
    titles = {
        value for key in ("canonical_regulation_title", "regulation_title", "document_name", "source_file")
        if isinstance(value := metadata.get(key), str) and value
    }
    paths = {
        value for key in ("canonical_hierarchy_path", "hierarchy_path", "source_hierarchy_path")
        if isinstance(value := metadata.get(key), str) and value
    }
    for title in titles:
        for path in paths:
            # Exact Chunker._retrieval_text format. A literal [표] in the body
            # remains body text; the separate table witness is never split out.
            rebuilt = f"[문서명] {title}\n[위치] {path}\n[본문]\n{body_witness}"
            if table:
                rebuilt += f"\n[표]\n{table}"
            if any(marker in rebuilt for marker in _DERIVED_SUFFIX_MARKERS):
                return SOURCE_UNAVAILABLE
            try:
                rebuilt = append_answer_profile_to_retrieval_text(rebuilt, metadata)
                rebuilt = _reference_text(rebuilt, metadata)
            except (TypeError, ValueError, AttributeError):
                return SOURCE_UNAVAILABLE
            if rebuilt == text:
                return SourceText(body_witness, "verified_reconstruction")
    return SOURCE_UNAVAILABLE
