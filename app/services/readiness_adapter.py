"""Translate diagnostic reports into stable, beginner-facing readiness states."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class OperatorReadinessState(StrEnum):
    """Small state vocabulary shared by operator-facing screens."""

    UNKNOWN = "unknown"
    READY = "ready"
    REGISTRATION_REQUIRED = "registration_required"
    CONFIGURED_PENDING = "configured_pending"
    ACTION_REQUIRED = "action_required"
    OPTIONAL_UNUSED = "optional_unused"
    STALE = "stale"

    @property
    def display_name(self) -> str:
        return {
            OperatorReadinessState.UNKNOWN: "확인 불가",
            OperatorReadinessState.READY: "준비 완료",
            OperatorReadinessState.REGISTRATION_REQUIRED: "등록 필요",
            OperatorReadinessState.CONFIGURED_PENDING: "설정 완료·연결 확인 대기",
            OperatorReadinessState.ACTION_REQUIRED: "조치 필요",
            OperatorReadinessState.OPTIONAL_UNUSED: "선택 기능 미사용",
            OperatorReadinessState.STALE: "오래된 확인",
        }[self]


@dataclass(frozen=True)
class ReadinessCard:
    """Presentation-neutral readiness result for one optional dependency."""

    component: str
    state: OperatorReadinessState
    reason_code: str
    next_action: str

    @property
    def display_name(self) -> str:
        return self.state.display_name


SAFE_REASON_CODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,95}$")
_BLOCKING_SEVERITIES = {"blocker", "critical", "high", "error", "failed"}
_STALE_TOKENS = {"stale", "outdated", "old_evidence", "legacy_evidence_unattributed"}
_OPTIONAL_UNSELECTED_REASONS = {
    "not_required",
    "not_selected",
    "optional_unselected",
    "optional_unused",
}

_NEXT_ACTIONS = {
    OperatorReadinessState.UNKNOWN: "진단을 다시 실행하고 결과 파일이 생성됐는지 확인하세요.",
    OperatorReadinessState.READY: "이 상태의 다음 단계로 이동하세요.",
    OperatorReadinessState.REGISTRATION_REQUIRED: "생성한 MCP 설정을 선택한 AI 앱에 등록한 뒤 연결 확인을 다시 실행하세요.",
    OperatorReadinessState.CONFIGURED_PENDING: "앱을 재시작하거나 실제 도구 호출로 연결을 확인하세요.",
    OperatorReadinessState.ACTION_REQUIRED: "오류 원인과 조치 방법을 확인한 뒤 다시 진단하세요.",
    OperatorReadinessState.OPTIONAL_UNUSED: "선택 기능이므로 건너뛰어도 됩니다.",
    OperatorReadinessState.STALE: "최신 상태를 다시 확인하고 새 번들을 다시 등록하세요.",
}


def adapt_readiness_report(
    report: Mapping[str, Any] | object | None,
    *,
    component: str,
    selected: bool | None = None,
    optional: bool = False,
) -> ReadinessCard:
    """Map a doctor/diagnostic payload without exposing its raw JSON to the UI.

    The adapter is deliberately conservative: malformed payloads become
    ``UNKNOWN`` and incomplete signals remain non-success states.
    """

    safe_component = _safe_component(component)
    if optional and selected is False:
        return _card(safe_component, OperatorReadinessState.OPTIONAL_UNUSED, "not_selected")
    if not isinstance(report, Mapping):
        return _card(safe_component, OperatorReadinessState.UNKNOWN, "report_invalid")

    reason = _first_reason(report)
    overall_state = _safe_state(report.get("overall_state"))
    if _is_optional_unused(report, selected=selected, optional=optional, reason=reason):
        return _card(safe_component, OperatorReadinessState.OPTIONAL_UNUSED, reason or "not_required")
    if _has_blocking_finding(report):
        return _card(safe_component, OperatorReadinessState.ACTION_REQUIRED, reason or "readiness_failed")
    if _is_stale(report, reason):
        return _card(safe_component, OperatorReadinessState.STALE, reason or "stale_evidence")
    if not _has_readiness_signal(report):
        return _card(safe_component, OperatorReadinessState.UNKNOWN, reason or "report_incomplete")
    if overall_state in {"pending", "not_checked"}:
        if report.get("configured") is True:
            return _card(
                safe_component,
                OperatorReadinessState.CONFIGURED_PENDING,
                reason or "probe_pending",
            )
        return _card(
            safe_component,
            OperatorReadinessState.REGISTRATION_REQUIRED,
            reason or "registration_required",
        )
    if overall_state == "connected":
        return _card(safe_component, OperatorReadinessState.READY, reason or "ok")
    if reason == "model_free_mode" and report.get("passed") is True:
        return _card(safe_component, OperatorReadinessState.READY, reason)
    if _awaiting_probe(report):
        return _card(
            safe_component,
            OperatorReadinessState.CONFIGURED_PENDING,
            reason or "probe_pending",
        )
    if report.get("deploy_ready") is True:
        return _card(safe_component, OperatorReadinessState.READY, reason or "ok")
    if report.get("passed") is True:
        state = (
            OperatorReadinessState.READY
            if report.get("probe") is True
            else OperatorReadinessState.CONFIGURED_PENDING
        )
        return _card(safe_component, state, reason or "ok")
    return _card(safe_component, OperatorReadinessState.ACTION_REQUIRED, reason or "readiness_failed")


def adapt_local_llm_probe(
    result: object,
    *,
    component: str = "Qwen3 8B",
) -> ReadinessCard:
    """Convert a local-LLM probe response to the shared operator contract."""

    available = isinstance(result, Mapping) and result.get("available") is True
    return adapt_readiness_report(
        {
            "passed": available,
            "probe": True,
            "reason": "available" if available else "local_backend_unavailable",
        },
        component=component,
    )


def _card(
    component: str,
    state: OperatorReadinessState,
    reason: str,
) -> ReadinessCard:
    return ReadinessCard(
        component=component,
        state=state,
        reason_code=_safe_reason(reason),
        next_action=_NEXT_ACTIONS[state],
    )


def _safe_component(value: object) -> str:
    """Keep caller-provided labels bounded and free of local path syntax."""

    candidate = " ".join(str(value or "").split())
    if not candidate or len(candidate) > 96 or "\\" in candidate or "/" in candidate:
        return "component"
    return candidate


def _safe_reason(value: object) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if SAFE_REASON_CODE_PATTERN.fullmatch(candidate) else "unknown"


def _safe_state(value: object) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _first_reason(report: Mapping[str, Any]) -> str:
    reason = report.get("reason") or report.get("reason_code")
    if reason:
        return str(reason).strip().lower()
    findings = report.get("findings")
    if isinstance(findings, list):
        for finding in findings:
            if isinstance(finding, Mapping) and finding.get("code"):
                return str(finding["code"]).strip().lower()
    stages = report.get("stages")
    if isinstance(stages, Mapping):
        for stage in stages.values():
            if isinstance(stage, Mapping) and stage.get("reason_code"):
                return str(stage["reason_code"]).strip().lower()
    return ""


def _is_optional_unused(
    report: Mapping[str, Any],
    *,
    selected: bool | None,
    optional: bool,
    reason: str,
) -> bool:
    report_optional = bool(report.get("optional") or report.get("optional_dependency"))
    report_selected = report.get("selected")
    return (
        (optional or report_optional)
        and (selected is False or report_selected is False or reason in _OPTIONAL_UNSELECTED_REASONS)
    )


def _is_stale(report: Mapping[str, Any], reason: str) -> bool:
    if report.get("overall_state") == "stale" or reason in _STALE_TOKENS:
        return True
    stages = report.get("stages")
    if isinstance(stages, Mapping):
        return any(
            isinstance(stage, Mapping)
            and (
                stage.get("state") == "stale"
                or str(stage.get("reason_code") or "").strip().lower() in _STALE_TOKENS
            )
            for stage in stages.values()
        )
    return any(token in reason for token in _STALE_TOKENS)


def _has_blocking_finding(report: Mapping[str, Any]) -> bool:
    findings = report.get("findings")
    if isinstance(findings, list):
        for finding in findings:
            if not isinstance(finding, Mapping):
                return True
            severity = str(finding.get("severity") or "").strip().lower()
            if severity in _BLOCKING_SEVERITIES:
                return True
    stages = report.get("stages")
    if isinstance(stages, Mapping):
        return any(
            isinstance(stage, Mapping)
            and str(stage.get("state") or "").strip().lower() in {"failed", "error"}
            for stage in stages.values()
        )
    return report.get("passed") is False and bool(findings)


def _has_readiness_signal(report: Mapping[str, Any]) -> bool:
    return any(
        key in report
        for key in (
            "passed",
            "deploy_ready",
            "overall_state",
            "remote_probe",
            "stages",
            "health",
        )
    )


def _awaiting_probe(report: Mapping[str, Any]) -> bool:
    if report.get("overall_state") == "configured":
        return True
    probe = report.get("remote_probe")
    if not isinstance(probe, Mapping):
        return bool(report.get("probe") is False and report.get("passed") is True)
    return probe.get("performed") is not True and bool(report.get("public_url"))
