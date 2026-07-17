from __future__ import annotations

from typing import Any


def evaluate_embedding_readiness(
    estimates: list[dict[str, Any]] | None = None,
    *,
    require_semantic_provider_approval: bool = False,
    approval_reference: str | None = None,
) -> dict[str, Any]:
    estimates = estimates or []
    failures: list[dict[str, Any]] = []
    approval_present = bool(str(approval_reference or "").strip())
    total_records = sum(_to_int(estimate.get("record_count")) for estimate in estimates)
    total_tokens = sum(_to_int(estimate.get("estimated_input_tokens")) for estimate in estimates)
    api_calls = sum(_to_int(estimate.get("api_call_count")) for estimate in estimates)
    statuses = sorted(
        {
            str(estimate.get("budget_evaluation_status") or "").strip()
            for estimate in estimates
            if str(estimate.get("budget_evaluation_status") or "").strip()
        }
    )

    if require_semantic_provider_approval and not estimates:
        failures.append({"reason": "missing_embedding_cost_estimate"})
    if require_semantic_provider_approval and not approval_present:
        failures.append({"reason": "missing_embedding_approval_reference"})

    for index, estimate in enumerate(estimates):
        failures.extend(_estimate_failures(index, estimate, require_semantic_provider_approval))

    checks = [
        _check(
            "embedding_estimates_provided_when_required",
            not require_semantic_provider_approval or bool(estimates),
            {"estimate_count": len(estimates), "required": require_semantic_provider_approval},
        ),
        _check(
            "embedding_estimates_are_local_estimate_only",
            all(estimate.get("mode") == "estimate_only" for estimate in estimates),
            {"estimate_count": len(estimates)},
        ),
        _check("embedding_api_calls_zero", api_calls == 0, {"api_call_count": api_calls}),
        _check(
            "semantic_embedding_approval_present_when_required",
            not require_semantic_provider_approval or approval_present,
            {"approval_reference_present": approval_present, "required": require_semantic_provider_approval},
        ),
        _check(
            "semantic_embedding_budget_ready_when_required",
            not any(failure["reason"].startswith("semantic_embedding_") for failure in failures),
            {"required": require_semantic_provider_approval},
        ),
        _check(
            "embedding_estimate_artifacts_valid",
            not any(failure["reason"].startswith("embedding_estimate_") for failure in failures),
            {"failure_count": len(failures)},
        ),
    ]
    provider_readiness = "not_requested"
    if require_semantic_provider_approval:
        provider_readiness = "approved_budget_ready" if not failures else "needs_attention"
    elif estimates:
        provider_readiness = "estimate_only_local_validation"
    return {
        "passed": not failures,
        "summary": {
            "embedding_estimate_count": len(estimates),
            "embedding_record_count": total_records,
            "embedding_estimated_input_tokens": total_tokens,
            "embedding_api_call_count": api_calls,
            "embedding_budget_evaluation_statuses": statuses,
            "semantic_embedding_provider_readiness": provider_readiness,
            "semantic_embedding_approval_reference_present": approval_present,
        },
        "checks": checks,
        "failures": failures,
    }


def _estimate_failures(
    index: int,
    estimate: dict[str, Any],
    require_semantic_provider_approval: bool,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    prefix = {"estimate_index": index, "provider_model": estimate.get("provider_model", "")}
    if estimate.get("report_type") != "embedding_cost_estimate":
        failures.append({**prefix, "reason": "embedding_estimate_invalid_report_type"})
    if estimate.get("mode") != "estimate_only":
        failures.append({**prefix, "reason": "embedding_estimate_not_estimate_only"})
    if _to_int(estimate.get("api_call_count")) != 0:
        failures.append({**prefix, "reason": "embedding_estimate_has_api_calls"})
    if _to_int(estimate.get("estimated_input_tokens")) < 0:
        failures.append({**prefix, "reason": "embedding_estimate_negative_tokens"})

    if not require_semantic_provider_approval:
        if estimate.get("budget") is not None and estimate.get("budget_exceeded") is not False:
            failures.append({**prefix, "reason": "embedding_estimate_budget_not_ready"})
        return failures

    if not str(estimate.get("provider_model") or "").strip():
        failures.append({**prefix, "reason": "semantic_embedding_missing_provider_model"})
    if estimate.get("price_per_1m_tokens") is None:
        failures.append({**prefix, "reason": "semantic_embedding_missing_price"})
    if estimate.get("budget") is None:
        failures.append({**prefix, "reason": "semantic_embedding_missing_budget"})
    if estimate.get("budget_evaluation_status") != "estimated":
        failures.append(
            {
                **prefix,
                "reason": "semantic_embedding_budget_not_estimated",
                "budget_evaluation_status": estimate.get("budget_evaluation_status"),
            }
        )
    if estimate.get("budget_exceeded") is not False:
        failures.append(
            {
                **prefix,
                "reason": "semantic_embedding_budget_exceeded_or_unknown",
                "budget_exceeded": estimate.get("budget_exceeded"),
            }
        )
    if estimate.get("estimated_total_cost") in (None, ""):
        failures.append({**prefix, "reason": "semantic_embedding_missing_estimated_cost"})
    return failures


def _check(name: str, passed: bool, details: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "details": details}


def _to_int(value: Any) -> int:
    if isinstance(value, bool) or value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(float(str(value)))
    except ValueError:
        return 0
