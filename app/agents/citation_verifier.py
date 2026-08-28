"""Deterministic evidence-to-citation verification role."""

from __future__ import annotations

from typing import Any

from app.agents.base import AgentResult, BaseAgent
from app.ingestion.vector_adapter import APPROVED_CHUNK_STATUS
from app.rag.extractive_answer import NO_EVIDENCE_ANSWER
from app.rag.output_filter import sanitize_rag_answer


class CitationVerifierAgent(BaseAgent):
    """Verify that answer output is backed by the retrieved evidence set.

    This is intentionally conservative: it verifies evidence identity and
    public citation construction, but does not claim semantic entailment from
    a string-matching heuristic.  A future evaluator can add entailment as a
    separate metric without weakening this gate.
    """

    def run(self, payload: dict) -> AgentResult:
        answer = sanitize_rag_answer(str(payload.get("answer") or "").strip())
        evidence = _validated_evidence(payload.get("evidence"))
        requested_ids = _requested_ids(payload.get("evidence_ids"))

        if answer == NO_EVIDENCE_ANSWER:
            return AgentResult(
                {
                    "role_id": "citation_verifier",
                    "status": "abstained",
                    "verified_answer": NO_EVIDENCE_ANSWER,
                    "citations": [],
                    "verified_evidence_ids": [],
                    "findings": ["answer_abstained"],
                    "verification_mode": "evidence_identity_only",
                }
            )

        if not evidence:
            return AgentResult(
                {
                    "role_id": "citation_verifier",
                    "status": "rejected",
                    "verified_answer": "",
                    "citations": [],
                    "verified_evidence_ids": [],
                    "findings": ["evidence_empty"],
                    "verification_mode": "evidence_identity_only",
                }
            )

        evidence_by_id = {identifier: item for item in evidence for identifier in _item_ids(item)}
        if requested_ids and any(identifier not in evidence_by_id for identifier in requested_ids):
            return _rejected("answer_evidence_id_not_in_retrieval")

        citation_evidence = evidence
        if requested_ids:
            requested_id_set = set(requested_ids)
            citation_evidence = [
                item
                for item in evidence
                if requested_id_set.intersection(_item_ids(item))
            ]

        citations = [_public_citation(item) for item in citation_evidence]
        citations = [citation for citation in citations if citation["evidence_id"]]
        if not citations:
            return _rejected("citation_identity_missing")

        return AgentResult(
            {
                "role_id": "citation_verifier",
                "status": "verified",
                "verified_answer": answer,
                "citations": citations,
                "verified_evidence_ids": [citation["evidence_id"] for citation in citations],
                "findings": [],
                "verification_mode": "evidence_identity_only",
            }
        )


def _validated_evidence(value: Any) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("citation_verifier evidence must be a list")
    validated: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("citation_verifier evidence items must be objects")
        approval_status = str(item.get("approval_status") or "").strip().lower()
        if approval_status and approval_status != APPROVED_CHUNK_STATUS:
            raise ValueError("citation_verifier received non-approved evidence")
        validated.append(dict(item))
    return validated


def _requested_ids(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("citation_verifier evidence_ids must be a list")
    return [str(item).strip() for item in value if str(item).strip()]


def _item_ids(item: dict[str, Any]) -> set[str]:
    return {
        str(item.get("chunk_id") or "").strip(),
        str(item.get("document_id") or "").strip(),
    } - {""}


def _public_citation(item: dict[str, Any]) -> dict[str, Any]:
    evidence_id = str(item.get("chunk_id") or item.get("document_id") or "").strip()
    return {
        "citation_id": f"citation:{evidence_id}" if evidence_id else "",
        "evidence_id": evidence_id,
        "document_id": str(item.get("document_id") or ""),
        "chunk_id": str(item.get("chunk_id") or ""),
        "document_title": str(item.get("regulation_title") or item.get("document_name") or ""),
        "regulation_version": str(item.get("regulation_version") or ""),
        "article_no": str(item.get("article_no") or ""),
        "article_title": str(item.get("article_title") or ""),
        "source_page_start": item.get("source_page_start"),
        "source_page_end": item.get("source_page_end"),
        "approval_id": str(item.get("approval_id") or ""),
    }


def _rejected(reason: str) -> AgentResult:
    return AgentResult(
        {
            "role_id": "citation_verifier",
            "status": "rejected",
            "verified_answer": "",
            "citations": [],
            "verified_evidence_ids": [],
            "findings": [reason],
            "verification_mode": "evidence_identity_only",
        }
    )
