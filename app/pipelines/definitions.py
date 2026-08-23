from __future__ import annotations

"""Stable stage contracts for the product's two user-visible pipelines.

The repository already contains the implementation of most individual stages.
This module gives those stages one explicit vocabulary so API jobs, operator UI,
audit records, and agent orchestration do not invent different names for the
same operation.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Mapping

from app.agents.model_router import model_profile_for_role
from app.agents.role_registry import get_agent_role


PREPROCESSING_PIPELINE_ID = "regulation_preprocessing_v1"
LOCAL_QA_PIPELINE_ID = "local_regulation_qa_v1"
StageStatus = Literal["pending", "running", "completed", "blocked", "failed"]
AgentRoleStatus = Literal[
    "pending",
    "running",
    "completed",
    "skipped",
    "degraded",
    "review_required",
    "blocked",
    "failed",
]


@dataclass(frozen=True)
class PipelineStageSpec:
    stage_id: str
    order: int
    title_ko: str
    owner: str
    purpose: str
    input_keys: tuple[str, ...] = ()
    output_keys: tuple[str, ...] = ()
    failure_policy: Literal["block", "review", "degrade"] = "block"
    security_gate: bool = False
    agent_role_ids: tuple[str, ...] = ()


@dataclass
class PipelineStageTracker:
    """Small in-memory state machine used to publish durable stage snapshots.

    It deliberately does not execute arbitrary callables. Side effects remain
    owned by the existing document, retrieval, approval, and indexing services;
    this object only validates and records the stage transition around them.
    """

    pipeline_id: str
    tenant_id: str | None = None
    _events: list[dict[str, Any]] = field(default_factory=list)
    _current_stage_id: str | None = None

    def start(self, stage_id: str, *, detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
        spec = _stage_by_id(self.pipeline_id, stage_id)
        if self._current_stage_id == stage_id:
            return self.snapshot()
        if self._current_stage_id is not None:
            current = _last_event_for(self._events, self._current_stage_id)
            if current and current["status"] == "running":
                raise ValueError(
                    f"Cannot start {stage_id!r} while {self._current_stage_id!r} is running."
                )
        event = self._event(spec, "running", detail=detail)
        self._events.append(event)
        self._current_stage_id = stage_id
        return self.snapshot()

    def complete(self, stage_id: str, *, detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
        spec = _stage_by_id(self.pipeline_id, stage_id)
        self._require_current(stage_id)
        running = _last_event_for(self._events, stage_id)
        if running is None or running["status"] != "running":
            raise ValueError(f"Stage {stage_id!r} is not running.")
        running.update({"status": "completed", "completed_at": _now(), "detail": _safe_detail(detail)})
        self._current_stage_id = None
        return self.snapshot()

    def set_agent_role_status(
        self,
        stage_id: str,
        role_id: str,
        *,
        status: AgentRoleStatus,
        reason_code: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record the real status of one role inside the running stage.

        Stage ownership is intentionally not enough for operator guidance: a
        stage may contain a deterministic parser, an optional local model, and
        a human gate. Keeping these statuses beside the stage event lets the
        UI explain what actually happened without exposing source content or
        runtime paths.
        """

        spec = _stage_by_id(self.pipeline_id, stage_id)
        self._require_current(stage_id)
        if role_id not in spec.agent_role_ids:
            raise ValueError(
                f"Role {role_id!r} is not assigned to stage {stage_id!r}."
            )
        event = _last_event_for(self._events, stage_id)
        if event is None or event["status"] != "running":
            raise ValueError(f"Stage {stage_id!r} is not running.")
        role_statuses = event.setdefault("agent_role_statuses", [])
        role_event = next(
            (item for item in role_statuses if item["role_id"] == role_id),
            None,
        )
        if role_event is None:
            raise ValueError(f"Role {role_id!r} is missing from the stage trace.")
        role_event.update(
            {
                "status": status,
                "reason_code": str(reason_code)[:120] if reason_code else None,
                "detail": _safe_detail(detail),
            }
        )
        return self.snapshot()

    def block(self, stage_id: str, *, reason_code: str, detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
        _stage_by_id(self.pipeline_id, stage_id)
        self._require_current(stage_id)
        running = _last_event_for(self._events, stage_id)
        if running is None or running["status"] != "running":
            raise ValueError(f"Stage {stage_id!r} is not running.")
        running.update(
            {
                "status": "blocked",
                "completed_at": _now(),
                "reason_code": str(reason_code)[:120],
                "detail": _safe_detail(detail),
            }
        )
        _close_running_roles(running, status="blocked", reason_code=reason_code)
        self._current_stage_id = None
        return self.snapshot()

    def fail(self, stage_id: str, *, reason_code: str, detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
        _stage_by_id(self.pipeline_id, stage_id)
        self._require_current(stage_id)
        running = _last_event_for(self._events, stage_id)
        if running is None or running["status"] != "running":
            raise ValueError(f"Stage {stage_id!r} is not running.")
        running.update(
            {
                "status": "failed",
                "completed_at": _now(),
                "reason_code": str(reason_code)[:120],
                "detail": _safe_detail(detail),
            }
        )
        _close_running_roles(running, status="failed", reason_code=reason_code)
        self._current_stage_id = None
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        definition = get_pipeline_definition(self.pipeline_id)
        current = next(
            (item for item in reversed(self._events) if item["status"] == "running"),
            None,
        )
        return {
            "pipeline_id": self.pipeline_id,
            "tenant_scoped": bool(self.tenant_id),
            "stage_count": len(definition),
            "current_stage_id": current["stage_id"] if current else None,
            "stages": [dict(item) for item in self._events],
        }

    def _require_current(self, stage_id: str) -> None:
        if self._current_stage_id != stage_id:
            raise ValueError(
                f"Stage {stage_id!r} is not the current stage of {self.pipeline_id!r}."
            )

    def _event(
        self,
        spec: PipelineStageSpec,
        status: StageStatus,
        *,
        detail: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "stage_id": spec.stage_id,
            "stage_number": spec.order,
            "stage_total": len(get_pipeline_definition(self.pipeline_id)),
            "title_ko": spec.title_ko,
            "owner": spec.owner,
            "agent_role_ids": list(spec.agent_role_ids),
            "agent_roles": _agent_role_manifest(spec.agent_role_ids),
            "agent_role_statuses": _agent_role_status_manifest(spec.agent_role_ids),
            "purpose": spec.purpose,
            "input_keys": list(spec.input_keys),
            "output_keys": list(spec.output_keys),
            "failure_policy": spec.failure_policy,
            "security_gate": spec.security_gate,
            "status": status,
            "started_at": _now(),
            "detail": _safe_detail(detail),
        }


_PREPROCESSING_STAGES = (
    PipelineStageSpec(
        "upload_admission", 1, "문서 업로드", "document_service", "파일·테넌트·중복·해시 검증", output_keys=("uploaded_document",), security_gate=True, agent_role_ids=("security_guard", "intake_guard")
    ),
    PipelineStageSpec(
        "parse_extract", 2, "파싱·추출", "parser", "텍스트·표·이미지·OCR 후보 추출", input_keys=("uploaded_document",), output_keys=("parsed_document",), failure_policy="review", agent_role_ids=("parser_extractor", "ocr_extractor")
    ),
    PipelineStageSpec(
        "normalize", 3, "정규화", "normalizer", "문자·공백·페이지·출처 보존 정리", input_keys=("parsed_document",), output_keys=("normalized_document",), agent_role_ids=("normalizer",)
    ),
    PipelineStageSpec(
        "structure_detect", 4, "조문 구조 인식", "structure_detector", "규정·장·절·조·항·호 계층 생성", input_keys=("normalized_document",), output_keys=("structure_nodes",), failure_policy="review", agent_role_ids=("structure_detector", "structure_reviewer", "table_reviewer")
    ),
    PipelineStageSpec(
        "chunk_generate", 5, "청크 생성", "chunker", "검색 가능한 조문 단위 청크 생성", input_keys=("structure_nodes",), output_keys=("chunks",), agent_role_ids=("chunk_builder",)
    ),
    PipelineStageSpec(
        "quality_gate", 6, "품질 검사·승인 대기", "quality_gate", "검증·품질 점수·사람 승인 대상 판정", input_keys=("chunks",), output_keys=("quality_report", "approval_worklist"), failure_policy="review", security_gate=True, agent_role_ids=("quality_gate", "human_approval_gate")
    ),
    PipelineStageSpec(
        "export", 7, "내보내기", "exporter", "JSONL·CSV·Markdown·표 export", input_keys=("chunks", "quality_report"), output_keys=("artifacts",), agent_role_ids=("exporter",)
    ),
    PipelineStageSpec(
        "vector_index", 8, "벡터 DB 입력", "index_builder", "승인된 청크만 임베딩·벡터 저장소 입력", input_keys=("approved_chunks",), output_keys=("vector_index",), security_gate=True, agent_role_ids=("semantic_embedder", "index_builder")
    ),
)

_LOCAL_QA_STAGES = (
    PipelineStageSpec(
        "query_analysis", 1, "질문 분석", "query_analyst", "질문 의도·조문·날짜·기관 조건 추출", output_keys=("query_plan",), agent_role_ids=("query_analyst",)
    ),
    PipelineStageSpec(
        "query_correction", 2, "검색어 보정", "query_analyst", "동의어·조문 번호·규정명 검색어 확장", input_keys=("query_plan",), output_keys=("search_queries",), agent_role_ids=("query_rewriter",)
    ),
    PipelineStageSpec(
        "hybrid_retrieval", 3, "하이브리드 검색", "retrieval_guard", "키워드·BM25·벡터 후보 검색과 승인/테넌트 필터", input_keys=("search_queries",), output_keys=("candidates",), security_gate=True, agent_role_ids=("retrieval_guard",)
    ),
    PipelineStageSpec(
        "rerank_filter", 4, "재순위·필터", "retrieval_guard", "관련도 재순위·최신 버전·ACL·보안 필터", input_keys=("candidates",), output_keys=("evidence",), security_gate=True, agent_role_ids=("reranker",)
    ),
    PipelineStageSpec(
        "context_build", 5, "컨텍스트 구성", "context_builder", "근거 본문과 인용 메타데이터 패키징", input_keys=("evidence",), output_keys=("grounding_context",), agent_role_ids=("context_builder",)
    ),
    PipelineStageSpec(
        "local_llm_answer", 6, "Qwen3 8B 답변", "grounded_answerer", "로컬 모델의 근거 제한 답변 생성", input_keys=("grounding_context",), output_keys=("draft_answer",), failure_policy="degrade", agent_role_ids=("grounded_answerer",)
    ),
    PipelineStageSpec(
        "citation_verify", 7, "인용 검증", "citation_verifier", "검색 결과에 존재하는 근거만 인용하도록 최종 검증", input_keys=("draft_answer", "evidence"), output_keys=("answer", "citations"), security_gate=True, agent_role_ids=("claim_auditor", "citation_verifier")
    ),
)

_PIPELINES: dict[str, tuple[PipelineStageSpec, ...]] = {
    PREPROCESSING_PIPELINE_ID: _PREPROCESSING_STAGES,
    LOCAL_QA_PIPELINE_ID: _LOCAL_QA_STAGES,
}


def get_pipeline_definition(pipeline_id: str) -> tuple[PipelineStageSpec, ...]:
    try:
        return _PIPELINES[pipeline_id]
    except KeyError as exc:
        raise ValueError(f"Unknown pipeline_id: {pipeline_id}") from exc


def pipeline_manifest() -> dict[str, Any]:
    return {
        pipeline_id: [
            {
                "stage_id": stage.stage_id,
                "stage_number": stage.order,
                "stage_total": len(stages),
                "title_ko": stage.title_ko,
                "owner": stage.owner,
                "agent_role_ids": list(stage.agent_role_ids),
                "agent_roles": _agent_role_manifest(stage.agent_role_ids),
                "purpose": stage.purpose,
                "input_keys": list(stage.input_keys),
                "output_keys": list(stage.output_keys),
                "failure_policy": stage.failure_policy,
                "security_gate": stage.security_gate,
            }
            for stage in stages
        ]
        for pipeline_id, stages in _PIPELINES.items()
    }


def _stage_by_id(pipeline_id: str, stage_id: str) -> PipelineStageSpec:
    for stage in get_pipeline_definition(pipeline_id):
        if stage.stage_id == stage_id:
            return stage
    raise ValueError(f"Unknown stage_id {stage_id!r} for pipeline {pipeline_id!r}.")


def _agent_role_manifest(role_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    """Expose role/model ownership without leaking runtime paths or prompts."""

    result: list[dict[str, Any]] = []
    for role_id in role_ids:
        role = get_agent_role(role_id)
        profile = model_profile_for_role(role_id)
        result.append(
            {
                "role_id": role.role_id,
                "display_name": role.display_name,
                "kind": role.kind,
                "purpose": role.purpose,
                "implementation_status": role.implementation_status,
                "required_inputs": list(role.required_inputs),
                "outputs": list(role.outputs),
                "can_mutate": list(role.can_mutate),
                "forbidden_actions": list(role.forbidden_actions),
                "model_profile": profile.profile_id if profile else None,
                "primary_model": profile.model if profile else role.primary_model,
                "failure_policy": role.failure_policy,
                "human_decision_required": role.kind == "human_gate",
            }
        )
    return result


def _agent_role_status_manifest(role_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    """Create a path-free mutable status view for one stage event."""

    return [
        {
            "role_id": role["role_id"],
            "display_name": role["display_name"],
            "model_profile": role["model_profile"],
            "primary_model": role["primary_model"],
            "status": "pending",
            "reason_code": None,
            "detail": {},
        }
        for role in _agent_role_manifest(role_ids)
    ]


def _last_event_for(events: list[dict[str, Any]], stage_id: str) -> dict[str, Any] | None:
    return next((item for item in reversed(events) if item["stage_id"] == stage_id), None)


def _close_running_roles(
    event: dict[str, Any],
    *,
    status: Literal["blocked", "failed"],
    reason_code: str,
) -> None:
    """Close roles that were running when their containing stage stopped."""

    for role_event in event.get("agent_role_statuses") or []:
        if role_event.get("status") == "running":
            role_event.update(
                {
                    "status": status,
                    "reason_code": str(reason_code)[:120],
                    "detail": {},
                }
            )


def _safe_detail(detail: Mapping[str, Any] | None) -> dict[str, Any]:
    if not detail:
        return {}
    safe: dict[str, Any] = {}
    for key, value in detail.items():
        # Stage details are operational counters and codes, never raw document text.
        if key in {"text", "raw_text", "path", "source_file", "filename", "prompt", "answer"}:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[str(key)[:80]] = value
    return safe


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
