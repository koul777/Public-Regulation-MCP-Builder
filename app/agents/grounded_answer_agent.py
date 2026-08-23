"""Grounded Qwen3 8B answer role.

The role accepts only already-retrieved evidence.  It does not search, change
approval state, create an index, or invent citation records.  Citation
verification remains a separate downstream role.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.agents.base import AgentResult, BaseAgent
from app.core.config import Settings
from app.ingestion.vector_adapter import APPROVED_CHUNK_STATUS
from app.rag.extractive_answer import (
    NO_EVIDENCE_ANSWER,
    build_structured_extractive_answer,
    select_supporting_answer_results,
)
from app.rag.local_llm import DEFAULT_LOCAL_LLM_MODEL, generate_local_llm_answer, local_llm_available
from app.rag.output_filter import sanitize_rag_answer


SUPPORTED_BACKENDS = {"extractive", "ollama", "llama-cpp", "openai-compatible"}


class GroundedAnswerAgent(BaseAgent):
    """Generate a grounded answer from an upstream retrieval result set."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(self, payload: dict) -> AgentResult:
        query = _required_query(payload)
        evidence = _validated_evidence(payload.get("evidence"))
        backend = _backend(payload, self.settings)
        if backend not in SUPPORTED_BACKENDS:
            raise ValueError(f"Unsupported grounded answer backend: {backend}")

        evidence_ids = _evidence_ids(evidence)
        if not evidence:
            return AgentResult(
                {
                    "role_id": "grounded_answerer",
                    "status": "abstained",
                    "answer": NO_EVIDENCE_ANSWER,
                    "answer_mode": "grounded_extractive",
                    "abstained": True,
                    "evidence_ids": [],
                    "fallback_reason": "no_approved_evidence",
                    "model": None,
                }
            )

        settings = replace(self.settings, rag_llm_backend=backend)
        if backend == "extractive":
            answer = build_structured_extractive_answer(query, evidence)
            return self._result(
                answer=answer,
                status="success",
                answer_mode="grounded_extractive",
                evidence=evidence,
                evidence_ids=evidence_ids,
                model=None,
            )

        model = str(settings.rag_llm_model or DEFAULT_LOCAL_LLM_MODEL).strip()
        if not local_llm_available(settings):
            return self._fallback_or_unavailable(
                query=query,
                evidence=evidence,
                evidence_ids=evidence_ids,
                backend=backend,
                model=model,
                reason="local_backend_not_available",
                allow_fallback=_allow_fallback(payload),
            )

        try:
            answer = generate_local_llm_answer(
                settings=settings,
                query=query,
                evidence=evidence,
                history=payload.get("history") if isinstance(payload.get("history"), list) else None,
            )
        except Exception as exc:
            return self._fallback_or_unavailable(
                query=query,
                evidence=evidence,
                evidence_ids=evidence_ids,
                backend=backend,
                model=model,
                reason=f"backend_error:{type(exc).__name__}",
                allow_fallback=_allow_fallback(payload),
            )

        answer = sanitize_rag_answer(answer)
        if not answer.strip():
            return self._fallback_or_unavailable(
                query=query,
                evidence=evidence,
                evidence_ids=evidence_ids,
                backend=backend,
                model=model,
                reason="empty_model_answer",
                allow_fallback=_allow_fallback(payload),
            )
        return self._result(
            answer=answer,
            status="success",
            answer_mode="grounded_local",
            evidence=evidence,
            evidence_ids=evidence_ids,
            model=model,
            backend=backend,
        )

    def _fallback_or_unavailable(
        self,
        *,
        query: str,
        evidence: list[dict[str, Any]],
        evidence_ids: list[str],
        backend: str,
        model: str,
        reason: str,
        allow_fallback: bool,
    ) -> AgentResult:
        if allow_fallback:
            answer = build_structured_extractive_answer(query, evidence)
            return self._result(
                answer=answer,
                status="fallback",
                answer_mode="grounded_extractive",
                evidence=evidence,
                evidence_ids=evidence_ids,
                model=model,
                backend=backend,
                fallback_reason=reason,
            )
        return AgentResult(
            {
                "role_id": "grounded_answerer",
                "status": "unavailable",
                "answer": NO_EVIDENCE_ANSWER,
                "answer_mode": "unavailable",
                "abstained": True,
                "evidence_ids": evidence_ids,
                "model": model,
                "backend": backend,
                "fallback_reason": reason,
            }
        )

    def _result(
        self,
        *,
        answer: str,
        status: str,
        answer_mode: str,
        evidence: list[dict[str, Any]],
        evidence_ids: list[str],
        model: str | None,
        backend: str | None = None,
        fallback_reason: str | None = None,
    ) -> AgentResult:
        supporting = select_supporting_answer_results("", evidence)
        result: dict[str, Any] = {
            "role_id": "grounded_answerer",
            "status": status,
            "answer": sanitize_rag_answer(answer),
            "answer_mode": answer_mode,
            "abstained": False,
            "evidence_ids": evidence_ids,
            "supporting_evidence_ids": _evidence_ids(supporting),
            "model": model,
            "backend": backend,
            "fallback_reason": fallback_reason,
        }
        return AgentResult(result)


def _required_query(payload: dict[str, Any]) -> str:
    query = str(payload.get("query") or "").strip()
    if not query:
        raise ValueError("grounded_answerer requires a non-empty query")
    return query


def _validated_evidence(value: Any) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("grounded_answerer evidence must be a list")
    validated: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("grounded_answerer evidence items must be objects")
        approval_status = str(item.get("approval_status") or "").strip().lower()
        if approval_status and approval_status != APPROVED_CHUNK_STATUS:
            raise ValueError("grounded_answerer received non-approved evidence")
        validated.append(dict(item))
    return validated


def _evidence_ids(evidence: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for item in evidence:
        value = str(item.get("chunk_id") or item.get("document_id") or "").strip()
        if value and value not in ids:
            ids.append(value)
    return ids


def _backend(payload: dict[str, Any], settings: Settings) -> str:
    return str(payload.get("backend") or settings.rag_llm_backend or "extractive").strip().lower()


def _allow_fallback(payload: dict[str, Any]) -> bool:
    return bool(payload.get("allow_fallback", True))
