from __future__ import annotations

"""Bounded Qwen3 4B review of uncertain deterministic structure nodes."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents.base import AgentResult, BaseAgent
from app.agents.model_router import QWEN3_REVIEW_MODEL, get_model_profile
from app.agents.ollama_runtime import OllamaRuntime
from app.schemas.structure import StructureNode


IssueType = Literal[
    "merged_boundary",
    "split_boundary",
    "wrong_parent",
    "missing_body",
    "garbled_text",
    "table_boundary",
    "caption_or_footnote_attachment",
    "other",
]


class StructureReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(min_length=1, max_length=160)
    risk_level: Literal["low", "medium", "high"]
    issue_type: IssueType
    source_quote: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=700)
    recommended_human_check: str = Field(min_length=1, max_length=700)


class LocalStructureReviewReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["skipped", "verified", "review_required", "degraded"]
    model: str | None = None
    candidate_count: int = Field(default=0, ge=0)
    reviewed_node_ids: tuple[str, ...] = ()
    findings: tuple[StructureReviewFinding, ...] = ()
    duration_ms: float = Field(default=0.0, ge=0.0)
    reason_code: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_status(self) -> "LocalStructureReviewReport":
        if self.status == "review_required" and not self.findings:
            raise ValueError("review_required structure report needs findings")
        if self.status == "verified" and self.findings:
            raise ValueError("verified structure report cannot contain findings")
        return self


class _ModelStructureReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[StructureReviewFinding] = Field(default_factory=list, max_length=24)


class LocalStructureReviewAgent(BaseAgent):
    def __init__(
        self,
        runtime: OllamaRuntime | None = None,
        *,
        max_nodes: int = 12,
    ) -> None:
        self.runtime = runtime or OllamaRuntime()
        self.profile = get_model_profile("review-qwen3-4b")
        self.max_nodes = max(1, min(int(max_nodes), 40))

    def review(
        self,
        nodes: list[StructureNode],
        *,
        strict_model: bool = False,
    ) -> LocalStructureReviewReport:
        candidates = _review_candidates(nodes, limit=self.max_nodes)
        candidate_ids = tuple(node.node_id for node in candidates)
        if not candidates:
            return LocalStructureReviewReport(
                status="skipped",
                candidate_count=0,
                reason_code="no_uncertain_structure_nodes",
            )
        try:
            if not self.runtime.model_available(self.profile.model):
                raise RuntimeError("required_model_not_installed")
            payload, generation = self.runtime.generate_json(
                model=self.profile.model,
                prompt=_review_prompt(candidates),
                schema=_ModelStructureReview.model_json_schema(),
                timeout_seconds=self.profile.timeout_seconds,
                temperature=float(self.profile.temperature or 0.0),
                max_output_tokens=1600,
            )
            response = _ModelStructureReview.model_validate(payload)
            findings = _validated_findings(response.findings, candidates)
            return LocalStructureReviewReport(
                status="review_required" if findings else "verified",
                model=QWEN3_REVIEW_MODEL,
                candidate_count=len(candidates),
                reviewed_node_ids=candidate_ids,
                findings=findings,
                duration_ms=generation.duration_ms,
            )
        except Exception as exc:
            if strict_model:
                raise
            return LocalStructureReviewReport(
                status="degraded",
                model=QWEN3_REVIEW_MODEL,
                candidate_count=len(candidates),
                reviewed_node_ids=candidate_ids,
                reason_code=_safe_reason(exc),
            )

    def run(self, payload: dict) -> AgentResult:
        nodes = [StructureNode.model_validate(item) for item in payload.get("nodes", [])]
        report = self.review(nodes, strict_model=bool(payload.get("strict_model", False)))
        return AgentResult(report.model_dump(mode="json"))


def apply_structure_review(
    nodes: list[StructureNode],
    report: LocalStructureReviewReport,
) -> list[StructureNode]:
    findings_by_node: dict[str, list[StructureReviewFinding]] = {}
    for finding in report.findings:
        findings_by_node.setdefault(finding.node_id, []).append(finding)
    reviewed_ids = set(report.reviewed_node_ids)
    updated: list[StructureNode] = []
    for node in nodes:
        findings = findings_by_node.get(node.node_id, [])
        if node.node_id not in reviewed_ids:
            updated.append(node)
            continue
        warnings = list(node.warnings)
        if report.status == "degraded":
            warnings.append("local_structure_review_unavailable")
        for finding in findings:
            warnings.append(f"local_structure_review:{finding.issue_type}")
        review_metadata = {
            "status": report.status,
            "model": report.model,
            "reason_code": report.reason_code,
            "findings": [finding.model_dump(mode="json") for finding in findings],
        }
        updated.append(
            node.model_copy(
                update={
                    "warnings": sorted(set(warnings)),
                    "metadata": {
                        **dict(node.metadata or {}),
                        "local_structure_review": review_metadata,
                    },
                }
            )
        )
    return updated


def _review_candidates(nodes: list[StructureNode], *, limit: int) -> list[StructureNode]:
    candidates: list[StructureNode] = []
    seen_numbers: dict[tuple[str, str], str] = {}
    for node in sorted(nodes, key=lambda item: item.order_index):
        metadata = node.metadata or {}
        has_duplicate_number = False
        if node.number:
            key = (node.node_type, str(node.number).strip())
            has_duplicate_number = key in seen_numbers
            seen_numbers[key] = node.node_id
        uncertain = (
            node.confidence < 0.95
            or bool(node.warnings)
            or bool(metadata.get("structure_fallback"))
            or bool(metadata.get("ambiguous_combined_book_boundary"))
            or has_duplicate_number
        )
        if uncertain and str(node.text or "").strip():
            candidates.append(node)
        if len(candidates) >= limit:
            break
    return candidates


def _validated_findings(
    findings: list[StructureReviewFinding],
    candidates: list[StructureNode],
) -> tuple[StructureReviewFinding, ...]:
    lookup = {node.node_id: node for node in candidates}
    validated: list[StructureReviewFinding] = []
    seen: set[tuple[str, str, str]] = set()
    for finding in findings:
        node = lookup.get(finding.node_id)
        if node is None:
            raise ValueError("structure reviewer referenced an unknown node")
        quote = " ".join(finding.source_quote.split())
        source = " ".join(str(node.text or "").split())
        if quote not in source:
            raise ValueError("structure reviewer source_quote is not exact source text")
        key = (finding.node_id, finding.issue_type, quote)
        if key in seen:
            continue
        seen.add(key)
        validated.append(finding.model_copy(update={"source_quote": quote}))
    return tuple(validated)


def _review_prompt(nodes: list[StructureNode]) -> str:
    payload = [
        {
            "node_id": node.node_id,
            "node_type": node.node_type,
            "number": node.number,
            "title": node.title,
            "parent_id": node.parent_id,
            "confidence": node.confidence,
            "parser_warnings": node.warnings,
            "source_text": str(node.text or "")[:5000],
        }
        for node in nodes
    ]
    return (
        "당신은 한국 공공기관 규정 전처리 구조 검수자다. JSON만 출력한다. "
        "원문을 수정하거나 새 조문을 만들지 말고, 실제 구조 오류가 있는 경우에만 findings를 작성한다. "
        "각 finding의 node_id는 입력 ID 중 하나여야 하며 source_quote는 해당 source_text에 연속해서 "
        "정확히 존재하는 짧은 인용문이어야 한다. 조문 병합·분리, 잘못된 부모, 본문 누락, 깨진 문자, "
        "표 경계, 캡션·각주 오부착만 검토한다. 단순 문장 품질이나 법률 해석은 평가하지 않는다. "
        "문제가 없으면 findings를 빈 배열로 반환한다. reason과 recommended_human_check는 한국어로 쓴다.\n"
        f"검수 대상 JSON:\n{json.dumps(payload, ensure_ascii=False)}"
    )


def _safe_reason(exc: Exception) -> str:
    if str(exc) == "required_model_not_installed":
        return "required_model_not_installed"
    return f"model_{type(exc).__name__}"[:120]
