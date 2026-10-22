from __future__ import annotations

"""Judge a private real-regulation E2E report against the versioned KPI policy.

Hard guardrails fail on any violation or missing evidence. Targets are judged
only with enough answerable cases, using exact success counts. Regressions run
only when a private baseline report from the same inputs and models is given.
The checker never edits the run report and never changes its legacy verdict.
"""

import argparse
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.evaluate_live_local_llm import HTTP_ERROR_CODES, _percentile  # noqa: E402
from scripts.run_real_regulation_e2e import EvidenceError, require_private_path  # noqa: E402


POLICY_PATH = Path(__file__).resolve().parent / "policies" / "real_regulation_kpi_guardrails.v1.json"
SCHEMA_VERSION = 1
REQUIRED_STAGES = ("preprocessing", "retrieval", "mcp", "live_llm", "input_integrity", "active_runtime_integrity")
REQUIRED_REQUEST_MODE_KEYS = ("llm_backend", "claim_audit_mode")
# Limitations the live evaluator records when the citation contract itself has
# no context ID or gold hash (evaluate_live_local_llm.score_case).
KNOWN_TRACE_LIMITATIONS = frozenset({"deterministic_citation_omits_context_and_hash",
                                     "content_hash_not_available_in_gold"})
HARD_CHECKS = ("coverage", "integrity", "no_auto_approval", "mcp_contract", "request_modes",
               "retrieval_no_evidence", "negative_cases", "forbidden_facts", "citation_trace")
TARGETS = ("retrieval_recall_at_5", "retrieval_mrr", "final_model_answer_rate",
           "required_fact_match_rate", "case_fact_pass_rate")
REGRESSIONS = ("retrieval_new_misses", "mcp_new_failures", "llm_new_case_failures", "llm_p95_duration_ratio")
COMPARABLE_FIELDS = ("report_type", "schema_version", "query_spec_sha256", "corpus", "runtime_source_fingerprint",
                     "runtime_scope", "configuration", "label_provenance")
PASSED, FAILED, UNAVAILABLE = "passed", "failed", "unavailable"
INSUFFICIENT, NOT_EVALUATED, ADVISORY = "insufficient_sample", "not_evaluated", "advisory_exceeded"


class GuardrailInputError(ValueError):
    """A fixed, path-free reason the inputs cannot be judged."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_policy(policy: Any) -> None:
    """Reject a policy that disables a hard check or drops a target or regression."""
    try:
        valid = (isinstance(policy, dict) and isinstance(policy["policy_id"], str) and _is_int(policy["policy_version"])
                 and isinstance(policy["report_type"], str)
                 and policy["report_schema_versions"] and all(_is_int(v) for v in policy["report_schema_versions"])
                 and all(isinstance(v, str) for v in policy["required_stages"])
                 and set(REQUIRED_STAGES) <= set(policy["required_stages"])
                 and all(isinstance(k, str) and isinstance(v, str) for k, v in policy["required_request_modes"].items())
                 and set(REQUIRED_REQUEST_MODE_KEYS) <= set(policy["required_request_modes"])
                 and sorted(policy["hard_checks"]) == sorted(HARD_CHECKS)
                 and "full" in policy["coverage_profiles"]
                 and all(_is_int(p[key]) and p[key] > 0 for p in policy["coverage_profiles"].values()
                         for key in ("min_answerable_cases", "min_negative_cases", "min_no_evidence_queries"))
                 and all(_is_int(p.get("max_query_count", 1)) and p.get("max_query_count", 1) > 0
                         for p in policy["coverage_profiles"].values())
                 and "max_query_count" not in policy["coverage_profiles"]["full"]
                 and sorted(policy["targets"]) == sorted(TARGETS)
                 and all(_is_number(t["minimum"]) and 0 < t["minimum"] <= 1 for t in policy["targets"].values())
                 and sorted(policy["regressions"]) == sorted(REGRESSIONS)
                 and all(_is_number(r["maximum"]) and r["maximum"] >= 0 and isinstance(r["blocking"], bool)
                         for r in policy["regressions"].values()))
    except (KeyError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise GuardrailInputError("invalid_kpi_policy")


def load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    raw = path.read_bytes()
    policy = json.loads(raw)
    validate_policy(policy)
    return {**policy, "sha256": hashlib.sha256(raw).hexdigest()}


def _stage(report: Mapping[str, Any], name: str) -> dict[str, Any]:
    stages = report.get("stages")
    stage = stages.get(name) if isinstance(stages, dict) else None
    return stage if isinstance(stage, dict) else {}


def _items(report: Mapping[str, Any], stage: str, key: str) -> list[dict[str, Any]] | None:
    result = _stage(report, stage).get("result")
    items = result.get(key) if isinstance(result, dict) else None
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        return None
    return items


def _llm_result(report: Mapping[str, Any]) -> dict[str, Any]:
    result = _stage(report, "live_llm").get("result")
    return result if isinstance(result, dict) else {}


def _answerable(case: Mapping[str, Any]) -> bool:
    return not case.get("expected_abstain") and not case.get("expected_block")


def _retrieval_answerable(item: Mapping[str, Any]) -> bool:
    return item.get("query_spec_valid", True) is True and item.get("expect_no_evidence") is not True


def _ids(items: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("id") or "") for item in items]


def _check(check_id: str, reasons: list[str], **details: Any) -> dict[str, Any]:
    return {"id": check_id, "status": FAILED if reasons else PASSED, "reasons": sorted(set(reasons)), **details}


def _unavailable(check_id: str, reason: str) -> dict[str, Any]:
    return {"id": check_id, "status": UNAVAILABLE, "reasons": [reason]}


def check_coverage(report: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    stages = report.get("stages") if isinstance(report.get("stages"), dict) else {}
    if any(not isinstance(stages.get(name), dict) for name in policy["required_stages"]):
        reasons.append("required_stage_missing")
    if _stage(report, "preprocessing").get("status") != "passed":
        reasons.append("preprocessing_not_passed")
    query_count = report.get("query_count")
    if not _is_int(query_count) or query_count <= 0:
        reasons.append("query_count_invalid")
    id_sets = []
    for stage, key, label in (("live_llm", "cases", "llm"), ("retrieval", "results", "retrieval"), ("mcp", "cases", "mcp")):
        items = _items(report, stage, key)
        if items is None:
            reasons.append(f"{label}_results_missing")
            continue
        ids = _ids(items)
        if not all(ids) or len(set(ids)) != len(ids):
            reasons.append(f"{label}_case_ids_invalid")
        if len(items) != query_count:
            reasons.append(f"{label}_case_count_mismatch")
        id_sets.append(set(ids))
    if len({frozenset(ids) for ids in id_sets}) > 1:
        reasons.append("case_id_sets_differ")
    llm = _llm_result(report)
    if llm.get("not_run_count") != 0:
        reasons.append("llm_cases_not_run")
    if llm.get("interrupted_reason"):
        reasons.append("llm_run_interrupted")
    for item in _items(report, "retrieval", "results") or []:
        if item.get("query_spec_valid", True) is not True:
            reasons.append("retrieval_query_spec_invalid")
        if item.get("search_succeeded") is not True:
            reasons.append("retrieval_search_error")
    return _check("coverage", reasons, query_count=query_count)


def check_integrity(report: Mapping[str, Any]) -> dict[str, Any]:
    reasons = [f"{name}_not_passed" for name in ("input_integrity", "active_runtime_integrity")
               if _stage(report, name).get("status") != "passed"]
    source_state = report.get("source_state")
    if not isinstance(source_state, dict) or source_state.get("stable") is not True:
        reasons.append("source_state_not_stable")
    return _check("integrity", reasons)


def check_no_auto_approval(report: Mapping[str, Any]) -> dict[str, Any]:
    reasons = [f"{field}_not_false" for field in ("human_approval_created", "product_release_ready")
               if report.get(field) is not False]
    return _check("no_auto_approval", reasons)


def check_mcp_contract(report: Mapping[str, Any]) -> dict[str, Any]:
    result = _stage(report, "mcp").get("result")
    cases = _items(report, "mcp", "cases")
    if not isinstance(result, dict) or cases is None:
        return _unavailable("mcp_contract", "mcp_results_missing")
    reasons = [] if _stage(report, "mcp").get("status") == "passed" else ["mcp_stage_not_passed"]
    checks = result.get("protocol_checks")
    if not isinstance(checks, dict) or not checks or any(
            not isinstance(check, dict) or check.get("passed") is not True for check in checks.values()):
        reasons.append("protocol_check_failed")
    if result.get("errors") != []:
        reasons.append("mcp_errors_reported")
    failed = [str(case.get("id") or "") for case in cases if case.get("passed") is not True]
    if failed:
        reasons.append("mcp_case_failed")
    return _check("mcp_contract", reasons, failed_case_ids=failed)


def check_request_modes(report: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    cases = _items(report, "live_llm", "cases")
    if not cases:
        return _unavailable("request_modes", "llm_results_missing")
    required = policy["required_request_modes"]
    mismatched = [str(case.get("id") or "") for case in cases
                  if not isinstance(case.get("request_modes"), dict)
                  or any(case["request_modes"].get(key) != value for key, value in required.items())]
    return _check("request_modes", ["request_mode_not_allowed"] if mismatched else [],
                  required=dict(required), mismatched_case_ids=mismatched)


def _with_minimum(result: dict[str, Any], count: int, minimum: int) -> dict[str, Any]:
    """A clean but too-small sample is not a pass; a violation still fails."""
    result.update(case_count=count, min_case_count=minimum)
    if result["status"] == PASSED and count < minimum:
        result["status"] = INSUFFICIENT
    return result


def check_retrieval_no_evidence(report: Mapping[str, Any], profile: Mapping[str, int]) -> dict[str, Any]:
    results = _items(report, "retrieval", "results")
    if results is None:
        return _unavailable("retrieval_no_evidence", "retrieval_results_missing")
    negatives = [item for item in results if item.get("query_spec_valid", True) is True
                 and item.get("expect_no_evidence") is True]
    if not negatives:
        return _unavailable("retrieval_no_evidence", "no_evidence_queries_missing")
    returned = [str(item.get("id") or "") for item in negatives
                if item.get("no_evidence_false_positive") is not False or item.get("no_evidence_abstained") is not True]
    result = _check("retrieval_no_evidence", ["no_evidence_query_returned_results"] if returned else [],
                    failed_case_ids=returned)
    return _with_minimum(result, len(negatives), profile["min_no_evidence_queries"])


def check_negative_cases(report: Mapping[str, Any], profile: Mapping[str, int]) -> dict[str, Any]:
    cases = _items(report, "live_llm", "cases")
    if cases is None:
        return _unavailable("negative_cases", "llm_results_missing")
    negatives = [case for case in cases if not _answerable(case)]
    if not negatives:
        return _unavailable("negative_cases", "negative_cases_missing")
    failed = [str(case.get("id") or "") for case in negatives if case.get("status") != "passed"]
    result = _check("negative_cases", ["negative_case_failed"] if failed else [], failed_case_ids=failed)
    return _with_minimum(result, len(negatives), profile["min_negative_cases"])


def check_forbidden_facts(report: Mapping[str, Any]) -> dict[str, Any]:
    cases = _items(report, "live_llm", "cases")
    if cases is None:
        return _unavailable("forbidden_facts", "llm_results_missing")
    reasons: list[str] = []
    exposed: list[str] = []
    for case in cases:
        correctness = case.get("correctness")
        matches = correctness.get("forbidden_match_indices") if isinstance(correctness, dict) else None
        if not isinstance(matches, list):
            reasons.append("forbidden_fact_result_missing")
        elif matches:
            exposed.append(str(case.get("id") or ""))
    if exposed:
        reasons.append("forbidden_fact_exposed")
    return _check("forbidden_facts", reasons, exposed_case_ids=exposed)


def check_citation_trace(report: Mapping[str, Any]) -> dict[str, Any]:
    """Every substantive answer must cite approved evidence whose identity was verified.

    Deterministic fallback citations may omit context IDs and hashes by contract.
    They still need verified identity and quotes; they are listed separately
    rather than failed, because a safe extractive fallback is not a leak. Any
    other limitation is unknown evidence and fails.
    """
    cases = _items(report, "live_llm", "cases")
    if cases is None:
        return _unavailable("citation_trace", "llm_results_missing")
    reasons: list[str] = []
    violations: list[dict[str, Any]] = []
    limited: list[str] = []
    checked = 0
    for case in (case for case in cases if _answerable(case)):
        if not isinstance(case.get("answer_present"), bool):
            reasons.append("answer_presence_not_recorded")
            continue
        if case.get("http_status") != 200 or not case["answer_present"] or case.get("abstained") is True:
            continue
        checked += 1
        citations = case.get("citation_checks") if isinstance(case.get("citation_checks"), dict) else {}
        context = case.get("answer_citation_context") if isinstance(case.get("answer_citation_context"), dict) else {}
        problems = []
        if not _is_int(citations.get("citation_count")) or citations["citation_count"] < 1:
            problems.append("missing_citation")
        if citations.get("all_verified") is not True:
            problems.append("unverified_citation")
        if context.get("passed") is not True:
            problems.append("answer_citation_context_not_verified")
        if "invalid_citations_shape" in (case.get("errors") or []):
            problems.append("invalid_citations_shape")
        limitations = case.get("verification_limitations")
        if not isinstance(limitations, list) or not set(limitations) <= KNOWN_TRACE_LIMITATIONS:
            problems.append("unknown_verification_limitation")
        if problems:
            violations.append({"id": str(case.get("id") or ""), "reasons": problems})
        elif case.get("verification_limitations"):
            limited.append(str(case.get("id") or ""))
    if violations:
        reasons.append("substantive_answer_citation_not_traceable")
    return _check("citation_trace", reasons, checked_answer_count=checked, violations=violations,
                  limited_trace_case_ids=limited)


def _rate_target(target_id: str, minimum: float, successes: int, denominator: int, *,
                 sample_size: int, min_sample_size: int, **details: Any) -> dict[str, Any]:
    required = int((Decimal(str(minimum)) * denominator).to_integral_value(rounding=ROUND_CEILING))
    if denominator == 0:
        status = UNAVAILABLE
    elif sample_size < min_sample_size:
        status = INSUFFICIENT
    else:
        status = PASSED if successes >= required else FAILED
    return {"id": target_id, "status": status, "minimum": minimum,
            "value": round(successes / denominator, 6) if denominator else None,
            "numerator": successes, "denominator": denominator, "required_successes": required,
            "allowed_failures": denominator - required, "sample_size": sample_size,
            "min_sample_size": min_sample_size, **details}


def _mean_target(target_id: str, minimum: float, values: list[float], *, min_sample_size: int,
                 **details: Any) -> dict[str, Any]:
    # Judge the exact sum; the rounded mean is for display only.
    total = sum((Decimal(str(item)) for item in values), Decimal(0))
    value = round(float(total / len(values)), 6) if values else None
    if value is None:
        status = UNAVAILABLE
    elif len(values) < min_sample_size:
        status = INSUFFICIENT
    else:
        status = PASSED if total >= Decimal(str(minimum)) * len(values) else FAILED
    return {"id": target_id, "status": status, "minimum": minimum, "value": value,
            "sample_size": len(values), "min_sample_size": min_sample_size, **details}


def _float(value: Any) -> float:
    return float(value) if _is_number(value) else 0.0


def evaluate_targets(report: Mapping[str, Any], policy: Mapping[str, Any],
                     profile: Mapping[str, int]) -> list[dict[str, Any]]:
    minimum = {name: policy["targets"][name]["minimum"] for name in TARGETS}
    min_cases = profile["min_answerable_cases"]
    results = [item for item in _items(report, "retrieval", "results") or [] if _retrieval_answerable(item)]
    recall = [_float(item.get("recall_at_5")) for item in results]
    cases = [case for case in _items(report, "live_llm", "cases") or [] if _answerable(case)]
    correctness = [case.get("correctness") if isinstance(case.get("correctness"), dict) else {} for case in cases]
    # An answerable case without gold anchors must not become a free failure allowance.
    unlabeled = [str(case.get("id") or "") for case, item in zip(cases, correctness)
                 if not (_is_int(item.get("required_count")) and item["required_count"] > 0
                         and _is_int(item.get("matched_count")) and 0 <= item["matched_count"] <= item["required_count"])]
    anchors = sum(item["required_count"] for item in correctness if _is_int(item.get("required_count")))
    matched = sum(item["matched_count"] for item in correctness if _is_int(item.get("matched_count")))
    fact_targets = [
        _rate_target("required_fact_match_rate", minimum["required_fact_match_rate"], matched, anchors,
                     sample_size=len(cases), min_sample_size=min_cases, unlabeled_case_ids=unlabeled),
        _rate_target("case_fact_pass_rate", minimum["case_fact_pass_rate"],
                     sum(item.get("status") == "passed" for item in correctness), len(cases),
                     sample_size=len(cases), min_sample_size=min_cases, unlabeled_case_ids=unlabeled),
    ]
    if unlabeled:
        for row in fact_targets:
            row["status"] = UNAVAILABLE
    return [
        _mean_target("retrieval_recall_at_5", minimum["retrieval_recall_at_5"], recall, min_sample_size=min_cases,
                     full_hit_count=sum(value == 1.0 for value in recall)),
        _mean_target("retrieval_mrr", minimum["retrieval_mrr"],
                     [_float(item.get("reciprocal_rank")) for item in results], min_sample_size=min_cases),
        _rate_target("final_model_answer_rate", minimum["final_model_answer_rate"],
                     sum(case.get("final_model_answer") is True for case in cases), len(cases),
                     sample_size=len(cases), min_sample_size=min_cases,
                     fallback_count=sum(case.get("fallback") is True for case in cases)),
        *fact_targets,
    ]


def _called_model_digests(report: Mapping[str, Any]) -> dict[str, Any] | None:
    llm = _llm_result(report)
    provenance = llm.get("provenance") if isinstance(llm.get("provenance"), dict) else {}
    inventory = {str(model.get("name")): model.get("digest") for model in provenance.get("models") or []
                 if isinstance(model, dict)}
    called = {str(event.get("model")) for case in _items(report, "live_llm", "cases") or []
              for event in case.get("generation_events") or []
              if isinstance(event, dict) and event.get("route") == "/api/generate" and event.get("model")}
    digests = {name: inventory.get(name) for name in sorted(called)}
    if not digests or any(not isinstance(value, str) or not value for value in digests.values()):
        return None
    return digests


def check_comparability(candidate: Mapping[str, Any], baseline: Mapping[str, Any],
                        policy: Mapping[str, Any]) -> dict[str, Any]:
    differences = [field for field in COMPARABLE_FIELDS if candidate.get(field) != baseline.get(field)]
    # Case-by-case regressions need unique, complete baseline IDs.
    if check_coverage(baseline, policy)["status"] != PASSED:
        differences.append("baseline_coverage_failed")
    candidate_models, baseline_models = _called_model_digests(candidate), _called_model_digests(baseline)
    if candidate_models is None or baseline_models is None:
        differences.append("called_model_digest_unavailable")
    elif candidate_models != baseline_models:
        differences.append("called_model_digests")
    return {"comparable": not differences, "differences": differences, "called_models": candidate_models}


def _regression(regression_id: str, policy: Mapping[str, Any], value: float | int | None, **details: Any) -> dict[str, Any]:
    rule = policy["regressions"][regression_id]
    if value is None:
        status = FAILED if rule["blocking"] else UNAVAILABLE
    elif value <= rule["maximum"]:
        status = PASSED
    else:
        status = FAILED if rule["blocking"] else ADVISORY
    return {"id": regression_id, "status": status, "blocking": rule["blocking"], "maximum": rule["maximum"],
            "value": value, **details}


def evaluate_regressions(candidate: Mapping[str, Any], baseline: Mapping[str, Any] | None,
                         policy: Mapping[str, Any]) -> list[dict[str, Any]]:
    if baseline is None:
        return [{"id": name, "status": NOT_EVALUATED, "blocking": policy["regressions"][name]["blocking"]}
                for name in REGRESSIONS]

    def by_id(report: Mapping[str, Any], stage: str, key: str) -> dict[str, dict[str, Any]] | None:
        items = _items(report, stage, key)
        return None if items is None else {str(item.get("id") or ""): item for item in items}

    rows = []
    old, new = by_id(baseline, "retrieval", "results"), by_id(candidate, "retrieval", "results")
    misses = None if old is None or new is None else sorted(
        case_id for case_id, item in old.items()
        if _retrieval_answerable(item) and _float(item.get("recall_at_5")) == 1.0
        and _float((new.get(case_id) or {}).get("recall_at_5")) < 1.0)
    rows.append(_regression("retrieval_new_misses", policy, None if misses is None else len(misses), case_ids=misses))
    for regression_id, stage, passed in (("mcp_new_failures", "mcp", lambda item: item.get("passed") is True),
                                         ("llm_new_case_failures", "live_llm", lambda item: item.get("status") == "passed")):
        old, new = by_id(baseline, stage, "cases"), by_id(candidate, stage, "cases")
        failures = None if old is None or new is None else sorted(
            case_id for case_id, item in old.items() if passed(item) and not passed(new.get(case_id) or {}))
        rows.append(_regression(regression_id, policy, None if failures is None else len(failures), case_ids=failures))

    def p95(report: Mapping[str, Any]) -> float | None:
        cases = _items(report, "live_llm", "cases") or []
        return _percentile([_float(case.get("duration_ms")) for case in cases], .95)

    old_p95, new_p95 = p95(baseline), p95(candidate)
    ratio = round(new_p95 / old_p95, 6) if old_p95 and new_p95 is not None else None
    rows.append(_regression("llm_p95_duration_ratio", policy, ratio, baseline_p95_ms=old_p95, candidate_p95_ms=new_p95))
    return rows


# --- Failure-cause breakdown (diagnostic only) ---------------------------------------------
# The guardrail rows say pass or fail per KPI. The plan (section 7 of
# docs/real_regulation_mcp_local_llm_10hour_plan_ko.md) picks the next fix by the
# dominant failure cause, so each failed case gets one primary cause plus the full
# list. Nothing here feeds verdict, passed, hard/target/regression rows or exit codes.
BRANCH_APPROVAL_BUNDLE = "승인 가시성·번들 결함"
BRANCH_RETRIEVAL = "검색 누락"
BRANCH_ANSWER = "근거는 맞지만 답변이 틀림"
BRANCH_LATENCY = "지연"
# Section 7 says approval and bundle defects are fixed first, so they win branch ties.
BRANCH_PRECEDENCE = (BRANCH_APPROVAL_BUNDLE, BRANCH_ANSWER, BRANCH_RETRIEVAL, BRANCH_LATENCY)
TIMEOUT_ERROR_TYPES = frozenset({"TimeoutError", "ReadTimeout", "ConnectTimeout"})
# Insertion order is the precedence: the first cause that applies is the primary one.
# `codes` lists every code the cause can put in a case record; tests keep it exact.
# `plan_branch` is the default section 7 branch; _plan_branch() applies the two exceptions
# (security_leak by case kind, execution_error by code).
FAILURE_CAUSES: dict[str, dict[str, Any]] = {
    # No section 7 bullet names a leak. A blocked case that answered is an isolation defect,
    # so it points at approval and bundle. An abstention case follows the cause that
    # co-occurs with it (a retrieval false positive points at retrieval); alone, the model
    # and audit let an unsupported answer through, which is the answer branch.
    "security_leak": {"plan_branch": BRANCH_APPROVAL_BUNDLE,
                      "codes": ("blocked_case_exposed_answer_or_called_model", "forbidden_fact_exposed",
                                "expected_clean_abstention", "expected_client_rejection")},
    "fabricated_citation": {"plan_branch": BRANCH_ANSWER,
                            "codes": ("missing_citation", "citation_not_verified",
                                      "answer_citation_context_not_verified", "invalid_citations_shape",
                                      "citation_verification_not_accepted", "unknown_verification_limitation")},
    "critical_fact": {"plan_branch": BRANCH_ANSWER, "codes": ("forbidden_fact_exposed",)},
    "retrieval_miss": {"plan_branch": BRANCH_RETRIEVAL,
                       "codes": ("recall_at_5_below_1", "mcp_target_evidence_not_retrieved")},
    # A no-evidence query that returned results is a retrieval-stage failure that is neither
    # a miss nor a model leak, so it has its own cause.
    "retrieval_false_positive": {"plan_branch": BRANCH_RETRIEVAL,
                                 "codes": ("no_evidence_query_returned_results", "mcp_unexpected_evidence")},
    "execution_error": {"plan_branch": BRANCH_APPROVAL_BUNDLE,
                        "codes": ("llm_transport_error", "llm_transport_timeout", "unexpected_http_status",
                                  "llm_backend_failed", "llm_backend_unavailable", "llm_output_invalid",
                                  "llm_runtime_failed", "evidence_identity_verification_failed",
                                  "citation_verification_failed",
                                  "claim_audit_fallback_unverified", "claim_audit_status_missing",
                                  "citation_verification_status_missing", "invalid_citations_shape",
                                  "expected_clean_abstention", "retrieval_query_spec_invalid",
                                  "retrieval_search_error", "mcp_search_failed", "mcp_fetch_failed",
                                  "mcp_case_incomplete")},
    # With claim_audit_mode=model a backend failure or timeout is an HTTP 503 (execution_error),
    # never a fallback. A fallback or abstention on an answerable case happens only when the
    # audit rejected the model's answer, which is the answer branch.
    "generation_fallback": {"plan_branch": BRANCH_ANSWER,
                            "codes": ("final_model_generation_not_verified", "claim_audit_not_accepted",
                                      "citation_verification_not_accepted", "answer_mode_fallback",
                                      "answer_abstained", "final_model_answer_false",
                                      "fallback_citation_without_context_or_hash")},
    "answer_incomplete": {"plan_branch": BRANCH_ANSWER, "codes": ("gold_facts_failed",)},
    # Stage data is missing or a failure cannot be classified. `unknown_reasons` carry the details.
    "unknown": {"plan_branch": None, "codes": ()},
}
CAUSE_PRECEDENCE = tuple(FAILURE_CAUSES)
CRITICAL_CAUSES = CAUSE_PRECEDENCE[:3]
# execution_error codes that override its default branch; the first one present wins.
# Transport and timeout failures are latency; model output the checks rejected, a rejected
# citation and a rejected audit are the answer branch; identity failures and runtime state
# (a missing model, an inner HTTP error) are approval and bundle defects. Every other
# execution_error code (missing statuses, search and MCP failures, malformed bodies)
# keeps the default.
EXECUTION_CODE_BRANCHES = (
    ("llm_backend_failed", BRANCH_LATENCY), ("llm_backend_unavailable", BRANCH_LATENCY),
    ("llm_transport_timeout", BRANCH_LATENCY),
    ("llm_output_invalid", BRANCH_ANSWER), ("llm_runtime_failed", BRANCH_APPROVAL_BUNDLE),
    ("evidence_identity_verification_failed", BRANCH_APPROVAL_BUNDLE),
    ("citation_verification_failed", BRANCH_ANSWER), ("claim_audit_fallback_unverified", BRANCH_ANSWER),
)
# Every error code evaluate_live_local_llm.score_case can emit. A test compares this set with
# the source, so a new score_case code cannot be skipped silently; unlisted codes become
# `unknown` with an `unmapped_error:` reason.
LLM_ERROR_CODES = frozenset({
    "unexpected_http_status", "claim_audit_status_missing", "citation_verification_status_missing",
    "claim_audit_not_accepted", "citation_verification_not_accepted", "final_model_generation_not_verified",
    "gold_facts_failed", "gold_facts_unverified", "citation_not_verified",
    "answer_citation_context_not_verified", "invalid_citations_shape", "expected_client_rejection",
    "blocked_case_exposed_answer_or_called_model", "expected_clean_abstention", "forbidden_fact_exposed"})
Finding = tuple[str, str]  # (cause, code)


def _error_codes(case: Mapping[str, Any]) -> list[str]:
    errors = case.get("errors")
    return [item for item in errors if isinstance(item, str)] if isinstance(errors, list) else []


def _retrieval_findings(item: Mapping[str, Any]) -> tuple[list[Finding], list[str]]:
    if item.get("query_spec_valid", True) is not True:
        return [("execution_error", "retrieval_query_spec_invalid")], []
    if item.get("search_succeeded") is not True:
        return [("execution_error", "retrieval_search_error")], []
    if item.get("expect_no_evidence") is True:
        returned = item.get("no_evidence_false_positive") is not False or item.get("no_evidence_abstained") is not True
        return ([("retrieval_false_positive", "no_evidence_query_returned_results")] if returned else []), []
    recall = item.get("recall_at_5")
    if not _is_number(recall):
        return [], ["retrieval_recall_not_recorded"]
    return ([("retrieval_miss", "recall_at_5_below_1")] if recall < 1.0 else []), []


def _mcp_findings(case: Mapping[str, Any]) -> tuple[list[Finding], list[str]]:
    if case.get("passed") is True:
        return [], []
    protocol, retrieval = case.get("search_protocol_passed"), case.get("retrieval_passed")
    if protocol is False:
        return [("execution_error", "mcp_search_failed")], []
    if protocol is True and retrieval is False:
        if case.get("expect_no_evidence") is True:
            return [("retrieval_false_positive", "mcp_unexpected_evidence")], []
        return [("retrieval_miss", "mcp_target_evidence_not_retrieved")], []
    if protocol is True and retrieval is True:
        fetch = case.get("fetch")
        fetch_failed = isinstance(fetch, list) and any(
            isinstance(row, dict) and row.get("passed") is not True for row in fetch)
        return [("execution_error", "mcp_fetch_failed" if fetch_failed else "mcp_case_incomplete")], []
    # passed=false with no stage detail (no search_protocol_passed, retrieval_passed or fetch
    # evidence): "not passed" is all that is known, so the cause is not guessed.
    return [], ["mcp_case_failed_unclassified"]


def _fallback_citation_gap_only(problems: list[str], citations: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    """True when an extractive fallback fails only because its citations carry no context ID or hash.

    The real fallback citation (CitationVerifierAgent) has no context_id and no content hash by
    construction. score_case only exempts that for the fast deterministic path, so it reports
    citation_not_verified and answer_citation_context_not_verified for fallback_from_* answers.
    """
    if not problems or not set(problems) <= {"citation_not_verified", "answer_citation_context_not_verified"}:
        return False
    if "citation_not_verified" in problems:
        checks = citations.get("checks")
        if not isinstance(checks, list) or not checks or not all(isinstance(check, dict) for check in checks):
            return False
        failing = [check for check in checks if check.get("passed") is not True]
        # A bad quote or an identity mismatch is a real citation problem, not a missing hash.
        if not failing or any(check.get("content_hash_status") != "missing"
                              or check.get("support_quote_status") == "invalid" for check in failing):
            return False
    if "answer_citation_context_not_verified" in problems and context.get("citation_context_ids") != []:
        return False
    return True


def _llm_findings(case: Mapping[str, Any]) -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    unknown: list[str] = []
    errors = _error_codes(case)
    http_status = case.get("http_status")
    status_2xx = _is_int(http_status) and 200 <= http_status < 300
    answer_present = case.get("answer_present")
    abstained = case.get("abstained") is True
    substantive = http_status == 200 and answer_present is True and not abstained
    citations = case.get("citation_checks") if isinstance(case.get("citation_checks"), dict) else {}
    context = case.get("answer_citation_context") if isinstance(case.get("answer_citation_context"), dict) else {}
    correctness = case.get("correctness") if isinstance(case.get("correctness"), dict) else None
    forbidden = (correctness or {}).get("forbidden_match_indices")
    forbidden_hit = isinstance(forbidden, list) and bool(forbidden)
    transport_error = bool(case.get("error_type"))

    def add(cause: str, code: str) -> None:
        findings.append((cause, code))

    def add_unexpected_status() -> None:
        add("execution_error", "unexpected_http_status")
        http_error = case.get("http_error_code")
        if http_error in HTTP_ERROR_CODES and http_error != "other":
            add("execution_error", http_error)

    if transport_error:
        add("execution_error", "llm_transport_error")
        if case.get("error_type") in TIMEOUT_ERROR_TYPES:
            add("execution_error", "llm_transport_timeout")
    if not _answerable(case):
        exposed = (answer_present is True and not abstained) or (
            _is_int(citations.get("citation_count")) and citations["citation_count"] > 0)
        if "blocked_case_exposed_answer_or_called_model" in errors:
            add("security_leak", "blocked_case_exposed_answer_or_called_model")
        if forbidden_hit or "forbidden_fact_exposed" in errors:
            add("security_leak", "forbidden_fact_exposed")
        # A non-2xx status is already reported as unexpected_http_status.
        if "expected_clean_abstention" in errors and status_2xx:
            add("security_leak" if exposed else "execution_error", "expected_clean_abstention")
        if "expected_client_rejection" in errors and status_2xx:
            add("security_leak", "expected_client_rejection")
        if "unexpected_http_status" in errors and not (case.get("expected_block") is True and status_2xx):
            add_unexpected_status()
        if "invalid_citations_shape" in errors:
            add("execution_error", "invalid_citations_shape")
    else:
        if "unexpected_http_status" in errors:
            add_unexpected_status()
        # Without a usable HTTP answer every answer-derived cause would only repeat this one.
        if not transport_error and "unexpected_http_status" not in errors:
            for code in ("claim_audit_status_missing", "citation_verification_status_missing"):
                if code in errors:
                    add("execution_error", code)
            if not isinstance(answer_present, bool):
                unknown.append("answer_presence_not_recorded")
            if forbidden_hit:
                add("critical_fact", "forbidden_fact_exposed")
            if substantive:
                problems: list[str] = []
                count = citations.get("citation_count")
                if not _is_int(count) or count < 1:
                    problems.append("missing_citation")
                elif citations.get("all_verified") is not True:
                    problems.append("citation_not_verified")
                if context.get("passed") is not True:
                    problems.append("answer_citation_context_not_verified")
                if "invalid_citations_shape" in errors:
                    problems.append("invalid_citations_shape")
                if "citation_verification_not_accepted" in errors and "citation_verification_status_missing" not in errors:
                    problems.append("citation_verification_not_accepted")
                limitations = case.get("verification_limitations")
                if not (isinstance(limitations, list) and all(isinstance(item, str) for item in limitations)
                        and set(limitations) <= KNOWN_TRACE_LIMITATIONS):
                    problems.append("unknown_verification_limitation")
                if case.get("fallback") is True and _fallback_citation_gap_only(problems, citations, context):
                    add("generation_fallback", "fallback_citation_without_context_or_hash")
                else:
                    for code in problems:
                        add("fabricated_citation", code)
            elif "invalid_citations_shape" in errors:
                add("execution_error", "invalid_citations_shape")
            final = case.get("final_model_answer")
            if "final_model_generation_not_verified" in errors:
                add("generation_fallback", "final_model_generation_not_verified")
            if "claim_audit_not_accepted" in errors and "claim_audit_status_missing" not in errors:
                add("generation_fallback", "claim_audit_not_accepted")
            if (not substantive and "citation_verification_not_accepted" in errors
                    and "citation_verification_status_missing" not in errors):
                add("generation_fallback", "citation_verification_not_accepted")
            if case.get("fallback") is True:
                add("generation_fallback", "answer_mode_fallback")
            if abstained:
                add("generation_fallback", "answer_abstained")
            if final is False and not any(cause == "generation_fallback" for cause, _ in findings):
                add("generation_fallback", "final_model_answer_false")
            if not isinstance(final, bool) and not any(cause == "generation_fallback" for cause, _ in findings):
                unknown.append("final_model_answer_not_recorded")
            if final is True:
                required = (correctness or {}).get("required_count")
                matched = (correctness or {}).get("matched_count")
                if not (_is_int(required) and _is_int(matched)):
                    unknown.append("correctness_not_recorded")
                elif required == 0:
                    if not forbidden_hit:
                        unknown.append("gold_facts_unlabeled")
                elif matched < required:
                    add("answer_incomplete", "gold_facts_failed")
    unknown.extend("unmapped_error:" + code[:60] for code in errors if code not in LLM_ERROR_CODES)
    if case.get("status") != "passed" and not findings and not unknown:
        unknown.append("llm_case_failed_unclassified")
    return findings, unknown


def _plan_branch(cause: str, causes: list[str], codes: Mapping[str, list[str]], expected_block: bool) -> str | None:
    """The section 7 branch for one failed case, from its primary cause."""
    if cause == "unknown":
        return None
    if cause == "security_leak":
        if expected_block:
            return BRANCH_APPROVAL_BUNDLE
        for other in causes:
            if other not in {"security_leak", "unknown"}:
                return _plan_branch(other, causes, codes, expected_block)
        return BRANCH_ANSWER
    if cause == "execution_error":
        present = set(codes.get(cause, ()))
        return next((branch for code, branch in EXECUTION_CODE_BRANCHES if code in present),
                    FAILURE_CAUSES[cause]["plan_branch"])
    return FAILURE_CAUSES[cause]["plan_branch"]


_BREAKDOWN_STAGES = (("retrieval", "results", "retrieval", _retrieval_findings),
                     ("mcp", "cases", "mcp", _mcp_findings),
                     ("live_llm", "cases", "llm", _llm_findings))


def _group_by_id(items: list[dict[str, Any]] | None) -> tuple[dict[str, list[dict[str, Any]]] | None, int]:
    if items is None:
        return None, 0
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        case_id = str(item.get("id") or "")
        if case_id:
            grouped.setdefault(case_id, []).append(item)
    return grouped, len(items) - sum(len(rows) for rows in grouped.values())


def _build_failure_breakdown(report: Mapping[str, Any]) -> dict[str, Any]:
    groups: dict[str, dict[str, list[dict[str, Any]]] | None] = {}
    unjoinable = 0
    for stage, key, _label, _judge in _BREAKDOWN_STAGES:
        groups[stage], without_id = _group_by_id(_items(report, stage, key))
        unjoinable += without_id
    llm_result = _llm_result(report)
    raw_not_run = llm_result.get("not_run_case_ids")
    not_run = {item for item in raw_not_run if isinstance(item, str) and item} if isinstance(raw_not_run, list) else set()
    case_ids = sorted(set().union(not_run, *(grouped.keys() for grouped in groups.values() if grouped is not None)))
    cases: list[dict[str, Any]] = []
    for case_id in case_ids:
        findings: list[tuple[str, str, str]] = []  # (stage, cause, code)
        unknown: list[tuple[str, str]] = []  # (stage, reason)
        for stage, _key, label, judge in _BREAKDOWN_STAGES:
            grouped = groups[stage]
            if grouped is None:
                unknown.append((stage, f"{label}_stage_missing"))
            elif case_id not in grouped:
                unknown.append((stage, "llm_case_not_run" if stage == "live_llm" and case_id in not_run
                                else f"{label}_case_missing"))
            else:
                for record in grouped[case_id]:
                    stage_findings, stage_unknown = judge(record)
                    findings.extend((stage, cause, code) for cause, code in stage_findings)
                    unknown.extend((stage, reason) for reason in stage_unknown)
        causes = [cause for cause in CAUSE_PRECEDENCE if any(item[1] == cause for item in findings)]
        if not causes and unknown:
            causes = ["unknown"]
        if not causes:
            continue
        codes = {cause: sorted({code for _, found, code in findings if found == cause}) for cause in causes
                 if cause != "unknown"}
        expected_block = any(row.get("expected_block") is True for row in (groups["live_llm"] or {}).get(case_id, []))
        cases.append({
            "id": case_id, "primary_cause": causes[0], "causes": causes,
            "plan_branch": _plan_branch(causes[0], causes, codes, expected_block), "codes": codes,
            "unknown_reasons": sorted({reason for _, reason in unknown}),
            "failed_stages": [stage for stage, *_ in _BREAKDOWN_STAGES
                              if any(item[0] == stage for item in findings) or any(item[0] == stage for item in unknown)],
        })
    primary = {cause: [case["id"] for case in cases if case["primary_cause"] == cause] for cause in CAUSE_PRECEDENCE}
    anywhere = {cause: [case["id"] for case in cases if cause in case["causes"]] for cause in CAUSE_PRECEDENCE}
    # `unknown` never wins: a run with many unclassified cases still points at the known causes,
    # and unknown_case_count says how much is unclassified. max() keeps the first of equal
    # counts, so ties go to the earlier cause in the precedence.
    known = [cause for cause in CAUSE_PRECEDENCE if cause != "unknown" and primary[cause]]
    dominant = max(known, key=lambda cause: len(primary[cause])) if known else None
    # Causes can span branches (execution_error, security_leak) and different causes can share
    # one, so the branch is counted over every classified failed case, not taken from the
    # dominant cause. Unknown cases have no branch. Ties go to BRANCH_PRECEDENCE.
    branch_counts = {branch: sum(case["plan_branch"] == branch for case in cases) for branch in BRANCH_PRECEDENCE}
    dominant_branch = max(BRANCH_PRECEDENCE, key=branch_counts.get) if any(branch_counts.values()) else None
    reason_counts: dict[str, int] = {}
    for case in cases:
        for reason in case["unknown_reasons"]:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    interrupted = llm_result.get("interrupted_reason")
    interrupted = interrupted if isinstance(interrupted, str) and interrupted else None
    not_run_count = llm_result.get("not_run_count")
    not_run_count = not_run_count if _is_int(not_run_count) else None
    return {
        "schema_version": 1, "diagnostic_only": True, "status": "ok" if case_ids else "no_case_data",
        "case_count": len(case_ids), "failed_case_count": len(cases), "unknown_case_count": len(primary["unknown"]),
        "unjoinable_record_count": unjoinable, "interrupted_reason": interrupted, "not_run_count": not_run_count,
        "classification_complete": not primary["unknown"] and interrupted is None and not_run_count in {0, None},
        "cause_precedence": list(CAUSE_PRECEDENCE),
        "dominant_cause": dominant, "dominant_plan_branch": dominant_branch,
        "plan_branch_counts": branch_counts,
        "counts": {"primary": {cause: len(ids) for cause, ids in primary.items()},
                   "any": {cause: len(ids) for cause, ids in anywhere.items()}},
        "case_ids": {"primary": primary, "any": anywhere},
        "critical_case_ids": sorted({case_id for cause in CRITICAL_CAUSES for case_id in anywhere[cause]}),
        "unknown_reason_counts": dict(sorted(reason_counts.items())),
        "cases": cases,
    }


def build_failure_breakdown(report: Mapping[str, Any]) -> dict[str, Any]:
    """Classify failed cases by cause. Diagnostic only: it must never change a verdict or exit code."""
    try:
        return _build_failure_breakdown(report)
    except Exception as exc:  # a diagnostic bug must not turn a judged run into an input error
        return {"schema_version": 1, "diagnostic_only": True, "status": "error", "reason": type(exc).__name__,
                "dominant_cause": None, "counts": {"primary": {}, "any": {}}}


def failure_breakdown_summary(breakdown: Mapping[str, Any]) -> dict[str, Any]:
    """Short stdout form: the dominant cause and non-zero primary counts, no case ids."""
    counts = breakdown.get("counts")
    primary = counts.get("primary") if isinstance(counts, dict) else None
    return {"dominant_cause": breakdown.get("dominant_cause"),
            "counts": {cause: count for cause, count in (primary or {}).items() if count}}


def _require_report(report: Any, policy: Mapping[str, Any]) -> Mapping[str, Any]:
    if (not isinstance(report, dict) or report.get("report_type") != policy["report_type"]
            or report.get("schema_version") not in policy["report_schema_versions"]):
        raise GuardrailInputError("unsupported_report")
    return report


def evaluate_kpi_guardrails(report: Any, policy: Mapping[str, Any], *, coverage_profile: str = "full",
                            baseline: Any = None) -> dict[str, Any]:
    """Re-judge the candidate (and optional baseline) under one current policy."""
    report = _require_report(report, policy)
    baseline = None if baseline is None else _require_report(baseline, policy)
    profile = policy["coverage_profiles"].get(coverage_profile)
    if profile is None:
        raise GuardrailInputError("unknown_coverage_profile")
    # A reduced profile is a declaration about a small question set, not a way to
    # judge a larger run by lower minimums.
    if "max_query_count" in profile and (not _is_int(report.get("query_count"))
                                         or report["query_count"] > profile["max_query_count"]):
        raise GuardrailInputError("coverage_profile_not_allowed_for_query_count")
    hard = [check_coverage(report, policy), check_integrity(report), check_no_auto_approval(report),
            check_mcp_contract(report), check_request_modes(report, policy), check_retrieval_no_evidence(report, profile),
            check_negative_cases(report, profile), check_forbidden_facts(report), check_citation_trace(report)]
    targets = evaluate_targets(report, policy, profile)
    comparability = None if baseline is None else check_comparability(report, baseline, policy)
    regressions = (evaluate_regressions(report, baseline, policy)
                   if comparability is None or comparability["comparable"] else [])
    failed = (any(row["status"] in {FAILED, UNAVAILABLE} for row in hard)
              or any(row["status"] == FAILED for row in targets)
              or any(row["status"] == FAILED for row in regressions))
    incomplete = (any(row["status"] == INSUFFICIENT for row in hard)
                  or any(row["status"] in {INSUFFICIENT, UNAVAILABLE} for row in targets))
    if comparability is not None and not comparability["comparable"]:
        verdict = "not_comparable"
    else:
        verdict = "failed" if failed else "incomplete" if incomplete else "passed"
    corpus = report.get("corpus") if isinstance(report.get("corpus"), dict) else {}
    limitations = ["literal_fact_anchors_do_not_prove_semantic_accuracy",
                   "citation_identity_does_not_prove_claim_support"]
    if corpus.get("human_review_complete") is not True:
        limitations.append("source_human_review_incomplete")
    if next(row for row in hard if row["id"] == "citation_trace").get("limited_trace_case_ids"):
        limitations.append("some_citations_verified_without_context_or_content_hash")
    if baseline is not None:
        limitations.append("latency_regression_is_advisory_without_controlled_repeats")
    return {
        "report_type": "real_regulation_kpi_guardrails", "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": {"policy_id": policy["policy_id"], "policy_version": policy["policy_version"],
                   "sha256": policy.get("sha256")},
        "coverage_profile": coverage_profile,
        "candidate": {"repo_commit": report.get("repo_commit"), "query_count": report.get("query_count"),
                      "query_spec_sha256": report.get("query_spec_sha256"),
                      "label_provenance": report.get("label_provenance"),
                      "legacy_report_passed": report.get("passed") is True},
        "baseline": None if baseline is None else {"repo_commit": baseline.get("repo_commit"),
                                                   "legacy_report_passed": baseline.get("passed") is True},
        "comparability": comparability,
        "hard": hard, "targets": targets, "regressions": regressions,
        "hard_passed": all(row["status"] == PASSED for row in hard),
        "targets_met": all(row["status"] == PASSED for row in targets),
        "regression_passed": None if comparability is None else verdict != "not_comparable" and not any(
            row["status"] == FAILED for row in regressions),
        "verdict": verdict, "passed": verdict == "passed",
        "legacy_verdict_note": "The run report's own passed flag fails on any single LLM case or retrieval "
                               "threshold miss, so it is stricter than these targets and is reported unchanged.",
        "limitations": limitations,
        "failure_breakdown": build_failure_breakdown(report),
    }


def _load_report(path: Path) -> tuple[Any, str]:
    raw = require_private_path(path).read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a private real-regulation E2E report against KPI guardrails.")
    parser.add_argument("--report", type=Path, required=True, help="Private report.json from run_real_regulation_e2e.py.")
    parser.add_argument("--baseline", type=Path, help="Private baseline report.json from the same inputs and models.")
    parser.add_argument("--coverage-profile", default="full", help="Policy coverage profile: full or compact.")
    parser.add_argument("--output", type=Path, required=True, help="New private JSON path for the guardrail result.")
    args = parser.parse_args(argv)
    try:
        output = require_private_path(args.output)
        if output.exists() or output.is_symlink():
            raise GuardrailInputError("output_already_exists")
        policy = load_policy()
        report, report_sha = _load_report(args.report)
        baseline, baseline_sha = _load_report(args.baseline) if args.baseline else (None, None)
        result = evaluate_kpi_guardrails(report, policy, coverage_profile=args.coverage_profile, baseline=baseline)
        result["candidate"]["report_sha256"] = report_sha
        if result["baseline"] is not None:
            result["baseline"]["report_sha256"] = baseline_sha
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    except Exception as exc:
        reason = str(exc) if isinstance(exc, (GuardrailInputError, EvidenceError)) else type(exc).__name__
        print(json.dumps({"passed": False, "reason_code": reason}))
        return 2
    print(json.dumps({"verdict": result["verdict"], "passed": result["passed"], "hard_passed": result["hard_passed"],
                      "targets_met": result["targets_met"], "regression_passed": result["regression_passed"],
                      "legacy_report_passed": result["candidate"]["legacy_report_passed"],
                      "failure_breakdown": failure_breakdown_summary(result["failure_breakdown"])}))
    if result["verdict"] == "not_comparable":
        return 2
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
