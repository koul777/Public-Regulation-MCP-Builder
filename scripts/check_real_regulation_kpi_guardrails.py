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
import re
import sys
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.evaluate_live_local_llm import _percentile  # noqa: E402
from scripts.holdout_spec_guard import HoldoutSpecRefused, normalize_split_label  # noqa: E402
from scripts.run_real_regulation_e2e import EvidenceError, holdout_record_is_valid, require_private_path  # noqa: E402


POLICY_PATH = Path(__file__).resolve().parent / "policies" / "real_regulation_kpi_guardrails.v1.json"
SCHEMA_VERSION = 1
REQUIRED_STAGES = ("preprocessing", "retrieval", "mcp", "live_llm", "input_integrity", "active_runtime_integrity")
REQUIRED_REQUEST_MODE_KEYS = ("llm_backend", "claim_audit_mode")
# Limitations the live evaluator records when the citation contract itself has
# no context ID or gold hash (evaluate_live_local_llm.score_case).
KNOWN_TRACE_LIMITATIONS = frozenset({"deterministic_citation_omits_context_and_hash",
                                     "content_hash_not_available_in_gold"})
HARD_CHECKS = ("coverage", "integrity", "no_auto_approval", "mcp_contract", "request_modes",
               "retrieval_no_evidence", "negative_cases", "forbidden_facts", "citation_trace",
               "holdout_protocol")
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


def _report_split(report: Mapping[str, Any]) -> str:
    """development, holdout, unspecified or invalid; labels are read like the spec's (scripts/holdout_spec_guard.py)."""
    value = report.get("query_split", "unspecified")
    if value == "unspecified":
        return "unspecified"
    try:
        return normalize_split_label(value)
    except HoldoutSpecRefused:
        return "invalid"


def check_holdout_protocol(report: Mapping[str, Any], expected_ledger_id: str | None = None) -> dict[str, Any]:
    """A holdout report must carry the ledger record written when the holdout was opened.

    The reviewer must pin the ledger_id printed by the first seal. Only a holdout
    report whose opened record has exactly the runner's fields, this ledger_id, and
    the report's own spec hash and query count passes. A report from a fresh ledger
    fails, and so does any holdout report judged without a pin.
    """
    pinned = {} if expected_ledger_id is None else {"expected_ledger_id": expected_ledger_id}
    split = _report_split(report)
    if split in ("development", "unspecified"):
        return _check("holdout_protocol", [] if expected_ledger_id is None else ["holdout_ledger_id_unexpected"],
                      split=split, **pinned)
    if split != "holdout":
        return _check("holdout_protocol", ["query_split_invalid"], split=str(report.get("query_split")), **pinned)
    record = report.get("holdout_ledger_record")
    reasons = []
    if expected_ledger_id is None:
        reasons.append("expected_holdout_ledger_id_required")
    if not isinstance(record, dict):
        reasons.append("holdout_ledger_record_missing")
    else:
        if record.get("event") != "opened":
            reasons.append("holdout_ledger_record_not_opened")
        elif not holdout_record_is_valid(record):
            reasons.append("holdout_ledger_record_schema_invalid")
        record_hash = record.get("query_spec_sha256")
        if not isinstance(record_hash, str) or not record_hash or record_hash != report.get("query_spec_sha256"):
            reasons.append("holdout_ledger_record_spec_mismatch")
        record_count = record.get("query_count")
        if not _is_int(record_count) or record_count != report.get("query_count"):
            reasons.append("holdout_ledger_record_query_count_mismatch")
        ledger_id = record.get("ledger_id")
        if not isinstance(ledger_id, str) or not ledger_id:
            reasons.append("holdout_ledger_id_missing")
        elif not re.fullmatch(r"[0-9a-f]{32}", ledger_id):
            reasons.append("holdout_ledger_id_invalid")
    if expected_ledger_id is not None and (not isinstance(record, dict) or record.get("ledger_id") != expected_ledger_id):
        reasons.append("holdout_ledger_id_unexpected")
    return _check("holdout_protocol", reasons, split=split, **pinned)


def _holdout_record(report: Mapping[str, Any]) -> Mapping[str, Any]:
    record = report.get("holdout_ledger_record") if _report_split(report) == "holdout" else None
    return record if isinstance(record, dict) else {}


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
    # A holdout result must not steer later development through a baseline comparison.
    if _report_split(baseline) not in ("development", "unspecified"):
        differences.append("baseline_is_holdout")
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


def _require_report(report: Any, policy: Mapping[str, Any]) -> Mapping[str, Any]:
    if (not isinstance(report, dict) or report.get("report_type") != policy["report_type"]
            or report.get("schema_version") not in policy["report_schema_versions"]):
        raise GuardrailInputError("unsupported_report")
    return report


def evaluate_kpi_guardrails(report: Any, policy: Mapping[str, Any], *, coverage_profile: str = "full",
                            baseline: Any = None, expected_holdout_ledger_id: str | None = None) -> dict[str, Any]:
    """Re-judge the candidate (and optional baseline) under one current policy."""
    if expected_holdout_ledger_id is not None and (
            not isinstance(expected_holdout_ledger_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", expected_holdout_ledger_id)):
        raise GuardrailInputError("expected_holdout_ledger_id_invalid")
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
            check_negative_cases(report, profile), check_forbidden_facts(report), check_citation_trace(report),
            check_holdout_protocol(report, expected_holdout_ledger_id)]
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
                      "query_split": report.get("query_split", "unspecified"),
                      "holdout_ledger_id": _holdout_record(report).get("ledger_id"),
                      "authorized_rerun": _holdout_record(report).get("authorized_rerun"),
                      "expected_holdout_ledger_id": expected_holdout_ledger_id,
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
    }


def _load_report(path: Path) -> tuple[Any, str]:
    raw = require_private_path(path).read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a private real-regulation E2E report against KPI guardrails.")
    parser.add_argument("--report", type=Path, required=True, help="Private report.json from run_real_regulation_e2e.py.")
    parser.add_argument("--baseline", type=Path, help="Private baseline report.json from the same inputs and models.")
    parser.add_argument("--coverage-profile", default="full", help="Policy coverage profile: full or compact.")
    parser.add_argument("--expected-holdout-ledger-id", help="32 hex characters. Pin the ledger_id of the first holdout seal; "
                        "only a holdout report written through that ledger passes holdout_protocol.")
    parser.add_argument("--output", type=Path, required=True, help="New private JSON path for the guardrail result.")
    args = parser.parse_args(argv)
    try:
        output = require_private_path(args.output)
        if output.exists() or output.is_symlink():
            raise GuardrailInputError("output_already_exists")
        policy = load_policy()
        report, report_sha = _load_report(args.report)
        baseline, baseline_sha = _load_report(args.baseline) if args.baseline else (None, None)
        result = evaluate_kpi_guardrails(report, policy, coverage_profile=args.coverage_profile, baseline=baseline,
                                         expected_holdout_ledger_id=args.expected_holdout_ledger_id)
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
                      "legacy_report_passed": result["candidate"]["legacy_report_passed"]}))
    if result["verdict"] == "not_comparable":
        return 2
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
