from __future__ import annotations

"""Bounded Qwen3 4B review of deterministically extracted table nodes."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents.base import AgentResult, BaseAgent
from app.agents.model_router import QWEN3_REVIEW_MODEL, get_model_profile
from app.agents.ollama_runtime import OllamaRuntime
from app.schemas.structure import StructureNode


TableIssueType = Literal[
    "inconsistent_columns",
    "missing_header",
    "merged_cell_ambiguity",
    "unit_scope_ambiguity",
    "footnote_attachment",
    "garbled_text",
    "other",
]


class TableReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    table_node_id: str = Field(min_length=1, max_length=160)
    risk_level: Literal["low", "medium", "high"]
    issue_type: TableIssueType
    source_quote: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=700)
    recommended_human_check: str = Field(min_length=1, max_length=700)


class LocalTableReviewReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["skipped", "verified", "review_required", "degraded"]
    model: str | None = None
    candidate_count: int = Field(default=0, ge=0)
    reviewed_table_ids: tuple[str, ...] = ()
    findings: tuple[TableReviewFinding, ...] = ()
    duration_ms: float = Field(default=0.0, ge=0.0)
    reason_code: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_status(self) -> "LocalTableReviewReport":
        if self.status == "review_required" and not self.findings:
            raise ValueError("review_required table report needs findings")
        if self.status == "verified" and self.findings:
            raise ValueError("verified table report cannot contain findings")
        return self


class _ModelTableReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[TableReviewFinding] = Field(default_factory=list, max_length=24)


class LocalTableReviewAgent(BaseAgent):
    def __init__(
        self,
        runtime: OllamaRuntime | None = None,
        *,
        max_tables: int = 8,
    ) -> None:
        self.runtime = runtime or OllamaRuntime()
        self.profile = get_model_profile("review-qwen3-4b")
        self.max_tables = max(1, min(int(max_tables), 24))

    def review(
        self,
        nodes: list[StructureNode],
        *,
        strict_model: bool = False,
    ) -> LocalTableReviewReport:
        candidates = [
            node
            for node in sorted(nodes, key=lambda item: item.order_index)
            if node.node_type == "table" and str(node.text or "").strip()
        ][: self.max_tables]
        candidate_ids = tuple(node.node_id for node in candidates)
        if not candidates:
            return LocalTableReviewReport(
                status="skipped",
                reason_code="no_table_nodes",
            )
        try:
            if not self.runtime.model_available(self.profile.model):
                raise RuntimeError("required_model_not_installed")
            payload, generation = self.runtime.generate_json(
                model=self.profile.model,
                prompt=_review_prompt(candidates),
                schema=_ModelTableReview.model_json_schema(),
                timeout_seconds=self.profile.timeout_seconds,
                temperature=float(self.profile.temperature or 0.0),
                max_output_tokens=1600,
            )
            response = _ModelTableReview.model_validate(payload)
            findings = _validated_findings(response.findings, candidates)
            return LocalTableReviewReport(
                status="review_required" if findings else "verified",
                model=QWEN3_REVIEW_MODEL,
                candidate_count=len(candidates),
                reviewed_table_ids=candidate_ids,
                findings=findings,
                duration_ms=generation.duration_ms,
            )
        except Exception as exc:
            if strict_model:
                raise
            return LocalTableReviewReport(
                status="degraded",
                model=QWEN3_REVIEW_MODEL,
                candidate_count=len(candidates),
                reviewed_table_ids=candidate_ids,
                reason_code=(
                    "required_model_not_installed"
                    if str(exc) == "required_model_not_installed"
                    else f"model_{type(exc).__name__}"[:120]
                ),
            )

    def run(self, payload: dict) -> AgentResult:
        nodes = [StructureNode.model_validate(item) for item in payload.get("nodes", [])]
        report = self.review(nodes, strict_model=bool(payload.get("strict_model", False)))
        return AgentResult(report.model_dump(mode="json"))


def apply_table_review(
    nodes: list[StructureNode],
    report: LocalTableReviewReport,
) -> list[StructureNode]:
    findings_by_node: dict[str, list[TableReviewFinding]] = {}
    for finding in report.findings:
        findings_by_node.setdefault(finding.table_node_id, []).append(finding)
    reviewed_ids = set(report.reviewed_table_ids)
    updated: list[StructureNode] = []
    for node in nodes:
        if node.node_id not in reviewed_ids:
            updated.append(node)
            continue
        findings = findings_by_node.get(node.node_id, [])
        warnings = list(node.warnings)
        if report.status == "degraded":
            warnings.append("local_table_review_unavailable")
        warnings.extend(f"local_table_review:{finding.issue_type}" for finding in findings)
        updated.append(
            node.model_copy(
                update={
                    "warnings": sorted(set(warnings)),
                    "metadata": {
                        **dict(node.metadata or {}),
                        "local_table_review": {
                            "status": report.status,
                            "model": report.model,
                            "reason_code": report.reason_code,
                            "findings": [finding.model_dump(mode="json") for finding in findings],
                        },
                    },
                }
            )
        )
    return updated


def _validated_findings(
    findings: list[TableReviewFinding],
    candidates: list[StructureNode],
) -> tuple[TableReviewFinding, ...]:
    lookup = {node.node_id: node for node in candidates}
    validated: list[TableReviewFinding] = []
    seen: set[tuple[str, str, str]] = set()
    for finding in findings:
        node = lookup.get(finding.table_node_id)
        if node is None:
            raise ValueError("table reviewer referenced an unknown table node")
        quote = " ".join(finding.source_quote.split())
        source = " ".join(str(node.text or "").split())
        if quote not in source:
            raise ValueError("table reviewer source_quote is not exact source text")
        key = (finding.table_node_id, finding.issue_type, quote)
        if key not in seen:
            seen.add(key)
            validated.append(finding.model_copy(update={"source_quote": quote}))
    return tuple(validated)


def _review_prompt(nodes: list[StructureNode]) -> str:
    payload = [
        {
            "table_node_id": node.node_id,
            "title": node.title,
            "page_start": node.page_start,
            "page_end": node.page_end,
            "parser_warnings": node.warnings,
            "source_text": str(node.text or "")[:6000],
        }
        for node in nodes
    ]
    return (
        "당신은 한국 공공기관 규정의 표 전처리 검수자다. JSON만 출력한다. "
        "셀 값을 고치거나 추측하지 말고, 표 원문의 열 수 불일치, 헤더 누락, 병합 셀 의미 불명확, "
        "단위 적용 범위, 각주 오부착, 깨진 문자만 찾는다. table_node_id는 입력 ID만 사용하고 "
        "source_quote는 해당 source_text에 연속해서 정확히 존재해야 한다. 문제가 없으면 findings를 "
        "빈 배열로 반환한다. reason과 recommended_human_check는 한국어로 쓴다.\n"
        f"검수 대상 JSON:\n{json.dumps(payload, ensure_ascii=False)}"
    )
