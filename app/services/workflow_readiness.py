"""Pure workflow readiness rules shared by operator-facing UIs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable


class WorkflowStage(StrEnum):
    """Stable identifiers for the four beginner workflow stages."""

    PREPROCESS = "preprocess"
    RESULTS = "results"
    APPROVAL = "approval"
    USE = "use"

    @property
    def display_name(self) -> str:
        """Return a beginner-facing label instead of an internal identifier."""

        return {
            WorkflowStage.PREPROCESS: "전처리",
            WorkflowStage.RESULTS: "결과 확인",
            WorkflowStage.APPROVAL: "승인·색인",
            WorkflowStage.USE: "AI 사용",
        }[self]


@dataclass(frozen=True)
class WorkflowReadiness:
    """Small presentation-neutral summary of workflow completion."""

    completed_steps: int
    total_steps: int
    current_stage: WorkflowStage | None

    @property
    def is_complete(self) -> bool:
        return self.current_stage is None


_WORKFLOW_STAGES = (
    WorkflowStage.PREPROCESS,
    WorkflowStage.RESULTS,
    WorkflowStage.APPROVAL,
    WorkflowStage.USE,
)


def summarize_workflow_readiness(states: Iterable[bool]) -> WorkflowReadiness:
    """Return the first incomplete stage and a count for the operator UI.

    The function intentionally accepts only already-evaluated boolean states;
    document, approval, and session-state rules remain in their owning layer.
    """

    normalized = tuple(states)
    if len(normalized) != len(_WORKFLOW_STAGES):
        raise ValueError("workflow readiness requires exactly four stage states")
    if any(not isinstance(state, bool) for state in normalized):
        raise ValueError("workflow readiness states must be boolean")
    first_incomplete = next(
        (index for index, done in enumerate(normalized) if not done),
        len(normalized),
    )
    if any(normalized[index] for index in range(first_incomplete, len(normalized))):
        raise ValueError("workflow stages must be completed in order")
    completed_steps = sum(normalized)
    current_stage = next(
        (stage for stage, done in zip(_WORKFLOW_STAGES, normalized) if not done),
        None,
    )
    return WorkflowReadiness(
        completed_steps=completed_steps,
        total_steps=len(_WORKFLOW_STAGES),
        current_stage=current_stage,
    )


def next_workflow_stage(states: Iterable[bool]) -> WorkflowStage:
    """Return the next stage, keeping the final-use screen as the fallback."""

    readiness = summarize_workflow_readiness(states)
    return readiness.current_stage or WorkflowStage.USE


def safe_summarize_workflow_readiness(states: Iterable[bool]) -> WorkflowReadiness:
    """Return a safe first-stage fallback for malformed persisted UI state."""

    try:
        return summarize_workflow_readiness(states)
    except ValueError:
        return WorkflowReadiness(
            completed_steps=0,
            total_steps=len(_WORKFLOW_STAGES),
            current_stage=_WORKFLOW_STAGES[0],
        )
