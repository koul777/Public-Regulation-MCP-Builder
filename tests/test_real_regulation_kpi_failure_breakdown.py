from __future__ import annotations

import copy
import inspect
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app.agents.citation_verifier import CitationVerifierAgent
from app.rag.extractive_answer import NO_EVIDENCE_ANSWER
from scripts import check_real_regulation_kpi_guardrails as guardrails
from scripts.check_real_regulation_kpi_guardrails import (
    CAUSE_PRECEDENCE,
    CRITICAL_CAUSES,
    EXECUTION_CODE_BRANCHES,
    FAILURE_CAUSES,
    LLM_ERROR_CODES,
    build_failure_breakdown,
    evaluate_kpi_guardrails,
    failure_breakdown_summary,
    load_policy,
    main,
)
from scripts.evaluate_live_local_llm import HTTP_ERROR_CODES, _http_error_code, score_case
from tests.test_evaluate_live_local_llm import _event, _response, _spec
from tests.test_real_regulation_kpi_guardrails import MODES, _cases, _report


PROJECT_ROOT = Path(__file__).resolve().parents[1]
A, N = "answer-00", "negative-00"
# The four section 7 branches, spelled out here independently of the checker's constants.
APPROVAL, RETRIEVAL, ANSWER, LATENCY = "승인 가시성·번들 결함", "검색 누락", "근거는 맞지만 답변이 틀림", "지연"
PLAN_BRANCHES = {APPROVAL, RETRIEVAL, ANSWER, LATENCY}
DEFAULT_BRANCH = {"security_leak": APPROVAL, "fabricated_citation": ANSWER, "critical_fact": ANSWER,
                  "retrieval_miss": RETRIEVAL, "retrieval_false_positive": RETRIEVAL, "execution_error": APPROVAL,
                  "generation_fallback": ANSWER, "answer_incomplete": ANSWER}
# Real server values: citation_verification_status is verified or abstained, and the
# claim audit status is verified, rejected, review_required or abstained (fallback_from_<status>).
ABSTAINED = {"answer_model": "synthetic-model", "answer_mode": "grounded_local",
             "claim_audit_status": "abstained", "citation_verification_status": "abstained"}


def _scored(case_id: str, *, extra: dict | None = None, response: dict | None = None, status: int = 200,
            events: list | None = None, negative: bool = False) -> dict:
    """Build a case with the real score_case so every field matches the live evaluator."""
    response = _response() if response is None else response
    spec = ({"id": case_id, "query": "synthetic missing rule"} if negative else {**_spec(), "id": case_id})
    spec.update(extra or {})
    events = [_event(response.get("answer"))] if events is None else events
    case = score_case(spec, response, http_status=status, events=events, duration_ms=100)
    case["request_modes"] = dict(MODES)
    return case


def _put_llm(report: dict, case: dict) -> None:
    cases = _cases(report)
    cases[next(index for index, item in enumerate(cases) if item["id"] == case["id"])] = case


def _retrieval(report: dict, case_id: str) -> dict:
    return next(item for item in report["stages"]["retrieval"]["result"]["results"] if item["id"] == case_id)


def _mcp(report: dict, case_id: str) -> dict:
    return next(item for item in report["stages"]["mcp"]["result"]["cases"] if item["id"] == case_id)


def _response_with(**orchestration: object) -> dict:
    response = _response()
    response["orchestration"] = {**response["orchestration"], **orchestration}
    return response


def _real_fallback_response(document_id: str = "synthetic-document") -> dict:
    """An extractive fallback built with the real verifier, so its citations have the real shape.

    The real citation has evidence_id, approval_id and page fields, but no context_id,
    no content hash and no support quote.
    """
    evidence = [{"chunk_id": "synthetic-chunk", "document_id": document_id, "approval_id": "synthetic-approval",
                 "approval_status": "approved", "article_no": "Article 1",
                 "source_page_start": 2, "source_page_end": 2}]
    verified = CitationVerifierAgent().run({"answer": "Applications must be submitted within seven days. [E1]",
                                            "evidence": evidence, "evidence_ids": ["synthetic-chunk"]})
    assert verified["status"] == "verified" and "context_id" not in verified["citations"][0]
    return {"answer": verified["verified_answer"], "citations": list(verified["citations"]),
            "orchestration": {"answer_model": "synthetic-model", "attempted_answer_model": "synthetic-model",
                              "answer_mode": "grounded_extractive", "claim_audit_status": "fallback_from_rejected",
                              "citation_verification_status": "verified"}}


def _record(breakdown: dict, case_id: str) -> dict:
    return next(case for case in breakdown["cases"] if case["id"] == case_id)


def _scenario(cause: str, name: str, case_id: str, mutate, causes: list[str], codes: dict[str, list[str]],
              branch: str | None = None) -> dict:
    return {"cause": cause, "name": name, "id": case_id, "mutate": mutate, "causes": causes, "codes": codes,
            "branch": branch or DEFAULT_BRANCH[cause]}


def _mcp_failure(report: dict, case_id: str, **fields: object) -> None:
    _mcp(report, case_id).update(passed=False, **fields)


def _build_scenarios() -> list[dict]:
    def negative_answered(report):
        _put_llm(report, _scored(N, negative=True, extra={"expected_abstain": True}))

    def blocked_answered(report):
        _put_llm(report, _scored(N, negative=True, extra={"expected_block": True}))

    def negative_forbidden(report):
        response = _response()
        response["answer"] = "Submit within thirty days. [E1]"
        _put_llm(report, _scored(N, negative=True, response=response,
                                 extra={"expected_abstain": True, "forbidden_facts": ["thirty days"]}))

    def block_not_enforced(report):
        _put_llm(report, _scored(N, negative=True, response={"answer": "", "citations": []}, events=[],
                                 extra={"expected_block": True}))

    def no_citations(report):
        response = _response()
        response["citations"] = []
        _put_llm(report, _scored(A, response=response))

    def wrong_identity(report):
        response = _response()
        response["citations"][0]["document_id"] = "another-document"
        _put_llm(report, _scored(A, response=response))

    def context_mismatch(report):
        response = _response()
        response["answer"] = "Submit within seven days."
        _put_llm(report, _scored(A, response=response))

    def invalid_shape(report):
        response = _response()
        response["citations"].append("not-an-object")
        _put_llm(report, _scored(A, response=response))

    def abstained_citation_status(report):
        _put_llm(report, _scored(A, response=_response_with(citation_verification_status="abstained")))

    def unknown_limitation(report):
        _cases(report)[0]["verification_limitations"] = ["identity_not_verified"]

    def fallback_identity_mismatch(report):
        _put_llm(report, _scored(A, response=_real_fallback_response("another-document"), events=[]))

    def forbidden_in_answer(report):
        response = _response()
        response["answer"] = "Submit within seven days, not thirty days. [E1]"
        _put_llm(report, _scored(A, response=response))

    def recall_zero(report):
        _retrieval(report, A).update(recall_at_5=0.0, reciprocal_rank=0.0)

    def recall_partial(report):
        _retrieval(report, A).update(recall_at_5=0.5)

    def mcp_not_retrieved(report):
        _mcp_failure(report, A, search_protocol_passed=True, retrieval_passed=False, expect_no_evidence=False, fetch=[])

    def mcp_bare(report):
        _mcp_failure(report, A)

    def retrieval_and_mcp(report):
        recall_zero(report)
        mcp_not_retrieved(report)

    def retrieval_false_positive(report):
        _retrieval(report, N).update(no_evidence_false_positive=True, no_evidence_abstained=False)

    def mcp_unexpected(report):
        _mcp_failure(report, N, search_protocol_passed=True, retrieval_passed=False, expect_no_evidence=True, fetch=[])

    def server_error(detail: str, case_id: str = A):
        def mutate(report):
            negative = case_id == N
            _put_llm(report, _scored(case_id, negative=negative, response={"detail": detail}, status=503, events=[],
                                     extra={"expected_abstain": True} if negative else None))
        return mutate

    def http_500(report):
        _put_llm(report, _scored(A, response={}, status=500, events=[]))

    def transport(error_type: str):
        def mutate(report):
            case = _scored(A, response={}, status=0, events=[])
            case["error_type"] = error_type
            _put_llm(report, case)
        return mutate

    def missing_statuses(report):
        response = _response()
        response["orchestration"] = {"answer_model": "synthetic-model", "answer_mode": "grounded_local"}
        _put_llm(report, _scored(A, response=response))

    def blocked_server_error(report):
        _put_llm(report, _scored(N, negative=True, response={}, status=500, events=[], extra={"expected_block": True}))

    def abstain_empty(report):
        _put_llm(report, _scored(N, negative=True, response={"answer": "", "citations": []}, events=[],
                                 extra={"expected_abstain": True}))

    def negative_invalid_shape(report):
        _put_llm(report, _scored(N, negative=True, response={"answer": NO_EVIDENCE_ANSWER, "citations": "bad"},
                                 events=[], extra={"expected_abstain": True}))

    def search_error(report):
        _retrieval(report, A).update(search_succeeded=False)

    def spec_invalid(report):
        _retrieval(report, A).update(query_spec_valid=False)

    def mcp_search_failed(report):
        _mcp_failure(report, A, search_protocol_passed=False, retrieval_passed=False)

    def mcp_fetch_failed(report):
        _mcp_failure(report, A, search_protocol_passed=True, retrieval_passed=True, fetch=[{"id": "x", "passed": False}])

    def mcp_incomplete(report):
        _mcp_failure(report, A, search_protocol_passed=True, retrieval_passed=True, fetch=[], completed=False)

    def abstained(report):
        _put_llm(report, _scored(A, response={"answer": NO_EVIDENCE_ANSWER, "citations": [],
                                              "orchestration": dict(ABSTAINED)}, events=[]))

    def bound_fallback(report):
        response = _response_with(answer_mode="grounded_extractive", claim_audit_status="fallback_from_review_required")
        _put_llm(report, _scored(A, response=response, events=[]))

    def real_fallback(report):
        _put_llm(report, _scored(A, response=_real_fallback_response(), events=[]))

    def final_flag_false(report):
        _cases(report)[0]["final_model_answer"] = False

    def missing_fact(report):
        response = _response()
        response["answer"] = "Submit soon. [E1]"
        _put_llm(report, _scored(A, response=response))

    S = _scenario
    return [
        S("security_leak", "negative answered", N, negative_answered, ["security_leak"],
          {"security_leak": ["expected_clean_abstention"]}, branch=ANSWER),
        S("security_leak", "blocked case answered", N, blocked_answered, ["security_leak"],
          {"security_leak": ["blocked_case_exposed_answer_or_called_model", "expected_client_rejection"]}),
        S("security_leak", "forbidden fact in negative", N, negative_forbidden, ["security_leak"],
          {"security_leak": ["expected_clean_abstention", "forbidden_fact_exposed"]}, branch=ANSWER),
        S("security_leak", "block not enforced but empty", N, block_not_enforced, ["security_leak"],
          {"security_leak": ["expected_client_rejection"]}),
        S("fabricated_citation", "no citations", A, no_citations, ["fabricated_citation"],
          {"fabricated_citation": ["answer_citation_context_not_verified", "missing_citation"]}),
        S("fabricated_citation", "wrong identity", A, wrong_identity, ["fabricated_citation"],
          {"fabricated_citation": ["citation_not_verified"]}),
        S("fabricated_citation", "context mismatch", A, context_mismatch, ["fabricated_citation"],
          {"fabricated_citation": ["answer_citation_context_not_verified"]}),
        S("fabricated_citation", "invalid shape", A, invalid_shape, ["fabricated_citation"],
          {"fabricated_citation": ["invalid_citations_shape"]}),
        S("fabricated_citation", "substantive answer with abstained citation status", A, abstained_citation_status,
          ["fabricated_citation", "generation_fallback"],
          {"fabricated_citation": ["citation_verification_not_accepted"],
           "generation_fallback": ["final_model_generation_not_verified"]}),
        S("fabricated_citation", "unknown limitation", A, unknown_limitation, ["fabricated_citation"],
          {"fabricated_citation": ["unknown_verification_limitation"]}),
        S("fabricated_citation", "fallback with identity mismatch", A, fallback_identity_mismatch,
          ["fabricated_citation", "generation_fallback"],
          {"fabricated_citation": ["answer_citation_context_not_verified", "citation_not_verified"],
           "generation_fallback": ["answer_mode_fallback", "claim_audit_not_accepted",
                                   "final_model_generation_not_verified"]}),
        S("critical_fact", "forbidden in answer", A, forbidden_in_answer, ["critical_fact"],
          {"critical_fact": ["forbidden_fact_exposed"]}),
        S("retrieval_miss", "recall zero", A, recall_zero, ["retrieval_miss"],
          {"retrieval_miss": ["recall_at_5_below_1"]}),
        S("retrieval_miss", "partial recall", A, recall_partial, ["retrieval_miss"],
          {"retrieval_miss": ["recall_at_5_below_1"]}),
        S("retrieval_miss", "mcp target not retrieved", A, mcp_not_retrieved, ["retrieval_miss"],
          {"retrieval_miss": ["mcp_target_evidence_not_retrieved"]}),
        S("retrieval_miss", "mcp bare failure", A, mcp_bare, ["retrieval_miss"],
          {"retrieval_miss": ["mcp_case_not_passed"]}),
        S("retrieval_miss", "retrieval and mcp", A, retrieval_and_mcp, ["retrieval_miss"],
          {"retrieval_miss": ["mcp_target_evidence_not_retrieved", "recall_at_5_below_1"]}),
        S("retrieval_false_positive", "retrieval returned results", N, retrieval_false_positive,
          ["retrieval_false_positive"], {"retrieval_false_positive": ["no_evidence_query_returned_results"]}),
        S("retrieval_false_positive", "mcp unexpected evidence", N, mcp_unexpected, ["retrieval_false_positive"],
          {"retrieval_false_positive": ["mcp_unexpected_evidence"]}),
        S("execution_error", "http 500", A, http_500, ["execution_error"],
          {"execution_error": ["unexpected_http_status"]}),
        S("execution_error", "backend failed 503", A,
          server_error("Multi-model local QA execution failed: TimeoutError"), ["execution_error"],
          {"execution_error": ["llm_backend_failed", "unexpected_http_status"]}, branch=LATENCY),
        S("execution_error", "single-model backend failed 503", A,
          server_error("Local LLM backend request failed: ConnectionError"), ["execution_error"],
          {"execution_error": ["llm_backend_failed", "unexpected_http_status"]}, branch=LATENCY),
        S("execution_error", "backend unavailable 503", A,
          server_error("Configured local LLM backend is not available."), ["execution_error"],
          {"execution_error": ["llm_backend_unavailable", "unexpected_http_status"]}, branch=LATENCY),
        S("execution_error", "backend failed 503 on a negative case", N,
          server_error("Local LLM backend request failed: backend unavailable", N), ["execution_error"],
          {"execution_error": ["llm_backend_unavailable", "unexpected_http_status"]}, branch=LATENCY),
        S("execution_error", "evidence identity 503", A,
          server_error("Final evidence identity verification failed"), ["execution_error"],
          {"execution_error": ["evidence_identity_verification_failed", "unexpected_http_status"]}, branch=APPROVAL),
        S("execution_error", "citation verification 503", A,
          server_error("Answer citation verification failed"), ["execution_error"],
          {"execution_error": ["citation_verification_failed", "unexpected_http_status"]}, branch=ANSWER),
        S("execution_error", "fallback unverified 503", A,
          server_error("Multi-model answer claim audit did not verify: rejected"), ["execution_error"],
          {"execution_error": ["claim_audit_fallback_unverified", "unexpected_http_status"]}, branch=ANSWER),
        S("execution_error", "unrecognized 503 message", A, server_error("something the server never says"),
          ["execution_error"], {"execution_error": ["unexpected_http_status"]}),
        S("execution_error", "transport timeout", A, transport("TimeoutError"), ["execution_error"],
          {"execution_error": ["llm_transport_error", "llm_transport_timeout", "unexpected_http_status"]},
          branch=LATENCY),
        S("execution_error", "transport reset", A, transport("ConnectionResetError"), ["execution_error"],
          {"execution_error": ["llm_transport_error", "unexpected_http_status"]}),
        S("execution_error", "audit and citation status missing", A, missing_statuses,
          ["execution_error", "generation_fallback"],
          {"execution_error": ["citation_verification_status_missing", "claim_audit_status_missing"],
           "generation_fallback": ["final_model_generation_not_verified"]}),
        S("execution_error", "blocked case server error", N, blocked_server_error, ["execution_error"],
          {"execution_error": ["unexpected_http_status"]}),
        S("execution_error", "abstention with empty body", N, abstain_empty, ["execution_error"],
          {"execution_error": ["expected_clean_abstention"]}),
        S("execution_error", "negative invalid citations shape", N, negative_invalid_shape, ["execution_error"],
          {"execution_error": ["invalid_citations_shape"]}),
        S("execution_error", "retrieval search error", A, search_error, ["execution_error"],
          {"execution_error": ["retrieval_search_error"]}),
        S("execution_error", "retrieval query spec invalid", A, spec_invalid, ["execution_error"],
          {"execution_error": ["retrieval_query_spec_invalid"]}),
        S("execution_error", "mcp search failed", A, mcp_search_failed, ["execution_error"],
          {"execution_error": ["mcp_search_failed"]}),
        S("execution_error", "mcp fetch failed", A, mcp_fetch_failed, ["execution_error"],
          {"execution_error": ["mcp_fetch_failed"]}),
        S("execution_error", "mcp case incomplete", A, mcp_incomplete, ["execution_error"],
          {"execution_error": ["mcp_case_incomplete"]}),
        S("generation_fallback", "abstained answerable", A, abstained, ["generation_fallback"],
          {"generation_fallback": ["answer_abstained", "citation_verification_not_accepted",
                                   "claim_audit_not_accepted", "final_model_generation_not_verified"]}),
        S("generation_fallback", "fallback with context-bound citations", A, bound_fallback, ["generation_fallback"],
          {"generation_fallback": ["answer_mode_fallback", "claim_audit_not_accepted",
                                   "final_model_generation_not_verified"]}),
        S("generation_fallback", "real extractive fallback citations", A, real_fallback, ["generation_fallback"],
          {"generation_fallback": ["answer_mode_fallback", "claim_audit_not_accepted",
                                   "fallback_citation_without_context_or_hash",
                                   "final_model_generation_not_verified"]}),
        S("generation_fallback", "final flag false", A, final_flag_false, ["generation_fallback"],
          {"generation_fallback": ["final_model_answer_false"]}),
        S("answer_incomplete", "required fact missing", A, missing_fact, ["answer_incomplete"],
          {"answer_incomplete": ["gold_facts_failed"]}),
    ]


SCENARIOS = _build_scenarios()


def _scenario_named(name: str) -> dict:
    return next(item for item in SCENARIOS if item["name"] == name)


class FailureCauseTableTests(unittest.TestCase):
    def test_precedence_critical_causes_and_plan_branches(self) -> None:
        self.assertEqual(("security_leak", "fabricated_citation", "critical_fact", "retrieval_miss",
                          "retrieval_false_positive", "execution_error", "generation_fallback",
                          "answer_incomplete", "unknown"), CAUSE_PRECEDENCE)
        self.assertEqual(("security_leak", "fabricated_citation", "critical_fact"), CRITICAL_CAUSES)
        for cause, row in FAILURE_CAUSES.items():
            with self.subTest(cause=cause):
                if cause == "unknown":
                    self.assertIsNone(row["plan_branch"])
                else:
                    self.assertEqual(DEFAULT_BRANCH[cause], row["plan_branch"])
        self.assertEqual(PLAN_BRANCHES, set(DEFAULT_BRANCH.values()) | {LATENCY})

    def test_llm_error_table_matches_every_code_score_case_can_emit(self) -> None:
        source = inspect.getsource(score_case)
        emitted = set(re.findall(r'errors\.append\("([a-z_]+)"\)', source))
        # score_case builds the gold-fact code from the correctness status.
        self.assertIn('errors.append("gold_facts_" + str(correctness["status"]))', source)
        emitted |= {"gold_facts_failed", "gold_facts_unverified"}
        self.assertEqual(emitted, set(LLM_ERROR_CODES))

    def test_http_error_codes_and_execution_branches_are_declared_codes(self) -> None:
        declared = set(FAILURE_CAUSES["execution_error"]["codes"])
        self.assertEqual(set(HTTP_ERROR_CODES) - {"other"}, declared & set(HTTP_ERROR_CODES))
        for code, branch in EXECUTION_CODE_BRANCHES:
            with self.subTest(code=code):
                self.assertIn(code, declared)
                self.assertIn(branch, PLAN_BRANCHES)

    def test_declared_codes_are_exactly_the_codes_the_scenarios_emit(self) -> None:
        emitted: dict[str, set[str]] = {cause: set() for cause in FAILURE_CAUSES}
        for scenario in SCENARIOS:
            report = _report()
            scenario["mutate"](report)
            for case in build_failure_breakdown(report)["cases"]:
                for cause, codes in case["codes"].items():
                    emitted[cause] |= set(codes)
        for cause, row in FAILURE_CAUSES.items():
            with self.subTest(cause=cause):
                self.assertEqual(set(row["codes"]), emitted[cause])


class HttpErrorCodeTests(unittest.TestCase):
    EXPECTED = {
        "Configured local LLM backend is not available.": "llm_backend_unavailable",
        "Local LLM backend request failed: backend unavailable": "llm_backend_unavailable",
        "Local LLM backend request failed: ReadTimeout": "llm_backend_failed",
        "Multi-model local QA execution failed: TimeoutError": "llm_backend_failed",
        "Final evidence identity verification failed": "evidence_identity_verification_failed",
        "Fast chat evidence identity verification failed": "evidence_identity_verification_failed",
        "Answer citation verification failed": "citation_verification_failed",
        "Multi-model answer claim audit did not verify: rejected": "claim_audit_fallback_unverified",
    }

    def test_known_messages_map_to_fixed_codes(self) -> None:
        for detail, code in self.EXPECTED.items():
            with self.subTest(detail=detail):
                self.assertEqual(code, _http_error_code(503, {"detail": detail}))
        self.assertEqual({*self.EXPECTED.values(), "other"}, set(HTTP_ERROR_CODES))

    def test_everything_else_is_other_and_success_is_none(self) -> None:
        for response in ({}, {"detail": "denied"}, {"detail": ["validation"]}, {"detail": 7}, {"detail": None},
                         {"detail": "Answer citation verification failed."},
                         {"detail": "prefix Local LLM backend request failed: x"},
                         {"detail": "Multi-model local QA execution failed"}):
            with self.subTest(response=response):
                self.assertEqual("other", _http_error_code(503, response))
        self.assertEqual("other", _http_error_code(0, {}))
        self.assertEqual("other", _http_error_code(403, {"detail": "denied"}))
        for status in (200, 201, 299):
            self.assertIsNone(_http_error_code(status, {"detail": "Answer citation verification failed"}))

    def test_score_case_records_the_code_and_never_the_raw_detail(self) -> None:
        marker = "SECRET-EXCEPTION-TEXT"
        case = _scored(A, response={"detail": f"Multi-model local QA execution failed: {marker}"}, status=503, events=[])
        self.assertEqual("llm_backend_failed", case["http_error_code"])
        self.assertNotIn(marker, json.dumps(case))
        self.assertIsNone(_scored(A)["http_error_code"])
        self.assertEqual("other", _scored(A, response={"detail": "denied"}, status=403, events=[])["http_error_code"])

    def test_every_503_message_in_the_chat_routes_is_classified(self) -> None:
        source = (PROJECT_ROOT / "app" / "api" / "routes_rag.py").read_text(encoding="utf-8")
        found = re.findall(r'status_code=503,\s*detail=f?"([^"]*)"', source)
        self.assertGreaterEqual(len(found), 9)
        for message in found:
            with self.subTest(message=message):
                rendered = re.sub(r"\{[^}]*\}", "SomeError", message)
                self.assertNotEqual("other", _http_error_code(503, {"detail": rendered}))


class FailureCauseScenarioTests(unittest.TestCase):
    def run_cause(self, cause: str) -> None:
        scenarios = [scenario for scenario in SCENARIOS if scenario["cause"] == cause]
        self.assertTrue(scenarios)
        for scenario in scenarios:
            with self.subTest(cause=cause, scenario=scenario["name"]):
                report = _report()
                scenario["mutate"](report)
                breakdown = build_failure_breakdown(report)
                self.assertEqual("ok", breakdown["status"])
                self.assertEqual([scenario["id"]], [case["id"] for case in breakdown["cases"]])
                record = _record(breakdown, scenario["id"])
                self.assertEqual(scenario["causes"], record["causes"])
                self.assertEqual(cause, record["primary_cause"])
                self.assertEqual(scenario["codes"], record["codes"])
                self.assertEqual(scenario["branch"], record["plan_branch"])
                self.assertEqual(scenario["branch"], breakdown["dominant_plan_branch"])
                self.assertEqual([], record["unknown_reasons"])
                self.assertEqual([scenario["id"]], breakdown["case_ids"]["primary"][cause])

    def test_security_leak(self) -> None:
        self.run_cause("security_leak")

    def test_fabricated_citation(self) -> None:
        self.run_cause("fabricated_citation")

    def test_critical_fact(self) -> None:
        self.run_cause("critical_fact")

    def test_retrieval_miss(self) -> None:
        self.run_cause("retrieval_miss")

    def test_retrieval_false_positive(self) -> None:
        self.run_cause("retrieval_false_positive")

    def test_execution_error(self) -> None:
        self.run_cause("execution_error")

    def test_generation_fallback(self) -> None:
        self.run_cause("generation_fallback")

    def test_answer_incomplete(self) -> None:
        self.run_cause("answer_incomplete")

    def test_scenarios_use_real_final_answer_semantics(self) -> None:
        report = _report()
        _scenario_named("required fact missing")["mutate"](report)
        case = _cases(report)[0]
        self.assertTrue(case["final_model_answer"])
        self.assertEqual("failed", case["correctness"]["status"])

    def test_clean_run_has_no_failures(self) -> None:
        breakdown = build_failure_breakdown(_report())
        self.assertEqual(("ok", 28, 0, None, None, []), (
            breakdown["status"], breakdown["case_count"], breakdown["failed_case_count"], breakdown["dominant_cause"],
            breakdown["dominant_plan_branch"], breakdown["critical_case_ids"]))
        self.assertEqual({0}, set(breakdown["counts"]["primary"].values()))
        self.assertEqual((0, True, None, 0), (breakdown["unknown_case_count"], breakdown["classification_complete"],
                                              breakdown["interrupted_reason"], breakdown["not_run_count"]))

    def test_http_failure_does_not_repeat_as_answer_causes(self) -> None:
        report = _report()
        _scenario_named("http 500")["mutate"](report)
        record = _record(build_failure_breakdown(report), A)
        self.assertEqual(["execution_error"], record["causes"])
        self.assertEqual(["live_llm"], record["failed_stages"])


class SecurityLeakBranchTests(unittest.TestCase):
    def breakdown_for(self, *mutations) -> dict:
        report = _report()
        for mutation in mutations:
            mutation(report)
        return build_failure_breakdown(report)

    def test_blocked_case_points_at_approval_and_bundle(self) -> None:
        record = _record(self.breakdown_for(_scenario_named("blocked case answered")["mutate"]), N)
        self.assertEqual((["security_leak"], APPROVAL), (record["causes"], record["plan_branch"]))

    def test_abstention_case_follows_the_co_occurring_cause(self) -> None:
        leak = _scenario_named("negative answered")["mutate"]
        alone = _record(self.breakdown_for(leak), N)
        self.assertEqual((["security_leak"], ANSWER), (alone["causes"], alone["plan_branch"]))
        with_retrieval = _record(self.breakdown_for(leak, _scenario_named("retrieval returned results")["mutate"]), N)
        self.assertEqual((["security_leak", "retrieval_false_positive"], RETRIEVAL),
                         (with_retrieval["causes"], with_retrieval["plan_branch"]))
        with_mcp = _record(self.breakdown_for(leak, _scenario_named("mcp unexpected evidence")["mutate"]), N)
        self.assertEqual(RETRIEVAL, with_mcp["plan_branch"])


class FailureCausePrecedenceTests(unittest.TestCase):
    def breakdown_for(self, *mutations) -> dict:
        report = _report()
        for mutation in mutations:
            mutation(report)
        return build_failure_breakdown(report)

    def scenario(self, name: str):
        return _scenario_named(name)["mutate"]

    def test_primary_follows_the_fixed_precedence_and_all_causes_are_listed(self) -> None:
        cases = {
            "leak beats retrieval false positive": (
                (self.scenario("negative answered"), self.scenario("retrieval returned results")), N,
                ["security_leak", "retrieval_false_positive"], RETRIEVAL),
            "blocked leak beats retrieval false positive": (
                (self.scenario("blocked case answered"), self.scenario("retrieval returned results")), N,
                ["security_leak", "retrieval_false_positive"], APPROVAL),
            "fabricated beats critical fact": (
                (self.scenario("forbidden in answer"), self.scenario("wrong identity")), A,
                ["fabricated_citation"], ANSWER),
            "critical fact beats retrieval miss": (
                (self.scenario("forbidden in answer"), self.scenario("recall zero")), A,
                ["critical_fact", "retrieval_miss"], ANSWER),
            "retrieval miss beats fallback": (
                (self.scenario("recall zero"), self.scenario("abstained answerable")), A,
                ["retrieval_miss", "generation_fallback"], RETRIEVAL),
            "execution error beats fallback": (
                (self.scenario("audit and citation status missing"),), A,
                ["execution_error", "generation_fallback"], APPROVAL),
            "retrieval miss beats execution error": (
                (self.scenario("recall zero"), self.scenario("mcp search failed")), A,
                ["retrieval_miss", "execution_error"], RETRIEVAL),
        }
        for name, (mutations, case_id, expected, branch) in cases.items():
            with self.subTest(name):
                breakdown = self.breakdown_for(*mutations)
                record = _record(breakdown, case_id)
                self.assertEqual(expected, record["causes"])
                self.assertEqual(expected[0], record["primary_cause"])
                self.assertEqual(branch, record["plan_branch"])
                for cause in expected:
                    self.assertIn(case_id, breakdown["case_ids"]["any"][cause])
                self.assertEqual([case_id], breakdown["case_ids"]["primary"][expected[0]])

    def test_failed_stages_name_every_stage_that_contributed(self) -> None:
        breakdown = self.breakdown_for(self.scenario("recall zero"), self.scenario("mcp bare failure"),
                                       self.scenario("abstained answerable"))
        self.assertEqual(["retrieval", "mcp", "live_llm"], _record(breakdown, A)["failed_stages"])

    def test_critical_case_ids_cover_secondary_critical_causes(self) -> None:
        breakdown = self.breakdown_for(self.scenario("negative answered"), self.scenario("forbidden in answer"),
                                       self.scenario("wrong identity"), self.scenario("recall zero"))
        # A has fabricated_citation first; N is a leak. Both are critical.
        self.assertEqual([A, N], breakdown["critical_case_ids"])
        report = _report()
        self.scenario("recall zero")(report)
        self.assertEqual([], build_failure_breakdown(report)["critical_case_ids"])


class FailureCauseDominanceTests(unittest.TestCase):
    def make_fallback(self, report: dict, index: int) -> None:
        response = {"answer": NO_EVIDENCE_ANSWER, "citations": [], "orchestration": dict(ABSTAINED)}
        _put_llm(report, _scored(f"answer-{index:02d}", response=response, events=[]))

    def make_backend_failure(self, report: dict, index: int, detail: str) -> None:
        _put_llm(report, _scored(f"answer-{index:02d}", response={"detail": detail}, status=503, events=[]))

    def test_higher_count_wins_over_higher_precedence(self) -> None:
        report = _report()
        _scenario_named("negative answered")["mutate"](report)
        self.make_fallback(report, 0)
        self.make_fallback(report, 1)
        breakdown = build_failure_breakdown(report)
        self.assertEqual({"security_leak": 1, "generation_fallback": 2},
                         {cause: count for cause, count in breakdown["counts"]["primary"].items() if count})
        self.assertEqual("generation_fallback", breakdown["dominant_cause"])
        self.assertEqual(ANSWER, breakdown["dominant_plan_branch"])
        self.assertEqual({APPROVAL: 0, ANSWER: 3, RETRIEVAL: 0, LATENCY: 0}, breakdown["plan_branch_counts"])

    def test_tie_goes_to_the_earlier_cause_in_the_precedence(self) -> None:
        report = _report()
        self.make_fallback(report, 1)
        _retrieval(report, A).update(recall_at_5=0.0)
        breakdown = build_failure_breakdown(report)
        self.assertEqual(1, breakdown["counts"]["primary"]["retrieval_miss"])
        self.assertEqual(1, breakdown["counts"]["primary"]["generation_fallback"])
        self.assertEqual("retrieval_miss", breakdown["dominant_cause"])
        self.assertEqual(RETRIEVAL, breakdown["dominant_plan_branch"])

    def test_a_cause_spanning_branches_reports_its_most_common_branch(self) -> None:
        report = _report()
        timeout = "Multi-model local QA execution failed: TimeoutError"
        self.make_backend_failure(report, 0, timeout)
        self.make_backend_failure(report, 1, timeout)
        self.make_backend_failure(report, 2, "Final evidence identity verification failed")
        breakdown = build_failure_breakdown(report)
        self.assertEqual(("execution_error", LATENCY), (breakdown["dominant_cause"], breakdown["dominant_plan_branch"]))
        self.assertEqual({APPROVAL: 1, ANSWER: 0, RETRIEVAL: 0, LATENCY: 2}, breakdown["plan_branch_counts"])
        # An even split goes to approval and bundle, which section 7 says to fix first.
        self.make_backend_failure(report, 3, "Final evidence identity verification failed")
        self.assertEqual(APPROVAL, build_failure_breakdown(report)["dominant_plan_branch"])

    def test_counts_and_case_ids_are_sorted_and_deterministic(self) -> None:
        report = _report()
        for index in (5, 2, 9):
            _retrieval(report, f"answer-{index:02d}").update(recall_at_5=0.0)
        first = build_failure_breakdown(report)
        self.assertEqual(["answer-02", "answer-05", "answer-09"], first["case_ids"]["primary"]["retrieval_miss"])
        self.assertEqual(first, build_failure_breakdown(copy.deepcopy(report)))
        self.assertEqual(3, first["counts"]["any"]["retrieval_miss"])


class InterruptedRunTests(unittest.TestCase):
    def interrupted_report(self) -> dict:
        report = _report()
        result = report["stages"]["live_llm"]["result"]
        timeout = {"detail": "Multi-model local QA execution failed: TimeoutError"}
        first = [_scored(f"answer-{index:02d}", response=timeout, status=503, events=[]) for index in range(2)]
        not_run = [case["id"] for case in result["cases"][2:]]
        result.update(cases=first, not_run_count=len(not_run), not_run_case_ids=not_run,
                      interrupted_reason="consecutive_server_failures")
        return report

    def test_unknown_never_wins_and_the_interruption_is_reported(self) -> None:
        breakdown = build_failure_breakdown(self.interrupted_report())
        self.assertEqual(("execution_error", LATENCY), (breakdown["dominant_cause"], breakdown["dominant_plan_branch"]))
        self.assertEqual({"execution_error": 2, "unknown": 26},
                         {cause: count for cause, count in breakdown["counts"]["primary"].items() if count})
        self.assertEqual((26, False, "consecutive_server_failures", 26), (
            breakdown["unknown_case_count"], breakdown["classification_complete"],
            breakdown["interrupted_reason"], breakdown["not_run_count"]))
        self.assertEqual({"llm_case_not_run": 26}, breakdown["unknown_reason_counts"])
        self.assertEqual(["answer-00", "answer-01"], breakdown["case_ids"]["primary"]["execution_error"])
        self.assertEqual({LATENCY: 2, APPROVAL: 0, ANSWER: 0, RETRIEVAL: 0}, breakdown["plan_branch_counts"])

    def test_only_unknown_failures_leave_no_dominant_cause(self) -> None:
        report = self.interrupted_report()
        report["stages"]["live_llm"]["result"]["cases"] = []
        report["stages"]["live_llm"]["result"]["not_run_case_ids"] = [
            f"answer-{index:02d}" for index in range(20)] + [f"negative-{index:02d}" for index in range(8)]
        breakdown = build_failure_breakdown(report)
        self.assertEqual((None, None, 28), (breakdown["dominant_cause"], breakdown["dominant_plan_branch"],
                                            breakdown["unknown_case_count"]))

    def test_verdict_is_unchanged_for_an_interrupted_run(self) -> None:
        policy = load_policy()
        report = self.interrupted_report()
        with_breakdown = evaluate_kpi_guardrails(report, policy)
        with patch.object(guardrails, "_build_failure_breakdown", side_effect=RuntimeError("boom")):
            without = evaluate_kpi_guardrails(report, policy)
        self.assertEqual("failed", with_breakdown["verdict"])
        self.assertEqual(with_breakdown["verdict"], without["verdict"])


class FallbackCitationTests(unittest.TestCase):
    """Real extractive fallback citations have no context ID or content hash."""

    def real_fallback_report(self) -> dict:
        report = _report()
        _scenario_named("real extractive fallback citations")["mutate"](report)
        return report

    def test_the_fixture_has_the_real_citation_shape(self) -> None:
        case = _cases(self.real_fallback_report())[0]
        self.assertTrue(case["fallback"])
        self.assertEqual("fallback_from_rejected", case["claim_audit_status"])
        self.assertEqual(["missing"], [check["content_hash_status"] for check in case["citation_checks"]["checks"]])
        self.assertEqual([], case["answer_citation_context"]["citation_context_ids"])
        self.assertEqual([], case["verification_limitations"])

    def test_missing_context_id_and_hash_is_a_fallback_not_a_fabricated_citation(self) -> None:
        breakdown = build_failure_breakdown(self.real_fallback_report())
        record = _record(breakdown, A)
        self.assertEqual("generation_fallback", record["primary_cause"])
        self.assertIn("fallback_citation_without_context_or_hash", record["codes"]["generation_fallback"])
        self.assertEqual([], breakdown["case_ids"]["any"]["fabricated_citation"])
        self.assertEqual([], breakdown["critical_case_ids"])

    def test_the_citation_trace_hard_check_still_flags_it(self) -> None:
        # Documented difference: the hard check stays fail-closed for fallback_from_* answers,
        # because score_case only exempts the missing context ID and hash on the fast
        # deterministic path. The breakdown only changes the label, not the judgement.
        result = evaluate_kpi_guardrails(self.real_fallback_report(), load_policy())
        trace = next(row for row in result["hard"] if row["id"] == "citation_trace")
        self.assertEqual("failed", trace["status"])
        self.assertEqual([A], [row["id"] for row in trace["violations"]])
        self.assertEqual(["answer_citation_context_not_verified", "unverified_citation"],
                         sorted(trace["violations"][0]["reasons"]))
        self.assertEqual("failed", result["verdict"])
        self.assertEqual("generation_fallback", result["failure_breakdown"]["dominant_cause"])

    def test_real_citation_problems_are_still_fabricated(self) -> None:
        report = _report()
        _scenario_named("fallback with identity mismatch")["mutate"](report)
        breakdown = build_failure_breakdown(report)
        self.assertEqual("fabricated_citation", _record(breakdown, A)["primary_cause"])
        self.assertEqual([A], breakdown["critical_case_ids"])
        # A bad quote on an otherwise hash-less citation is not a missing hash either.
        report = self.real_fallback_report()
        _cases(report)[0]["citation_checks"]["checks"][0]["support_quote_status"] = "invalid"
        self.assertEqual("fabricated_citation", _record(build_failure_breakdown(report), A)["primary_cause"])
        # Context IDs that exist but do not match the answer are a real mismatch.
        report = self.real_fallback_report()
        _cases(report)[0]["answer_citation_context"]["citation_context_ids"] = ["E2"]
        self.assertEqual("fabricated_citation", _record(build_failure_breakdown(report), A)["primary_cause"])

    def test_a_non_fallback_answer_gets_no_exemption(self) -> None:
        report = self.real_fallback_report()
        _cases(report)[0]["fallback"] = False
        self.assertIn("fabricated_citation", _record(build_failure_breakdown(report), A)["causes"])


class FailureCauseMissingDataTests(unittest.TestCase):
    def test_missing_llm_stage_marks_cases_unknown_with_a_reason(self) -> None:
        report = _report()
        report["stages"]["live_llm"] = {"status": "failed", "error_type": "TimeoutError"}
        _retrieval(report, A).update(recall_at_5=0.0)
        breakdown = build_failure_breakdown(report)
        self.assertEqual(28, breakdown["failed_case_count"])
        self.assertEqual(27, breakdown["unknown_case_count"])
        self.assertFalse(breakdown["classification_complete"])
        # Unknown never wins, so the one classified failure is still reported.
        self.assertEqual(("retrieval_miss", RETRIEVAL), (breakdown["dominant_cause"], breakdown["dominant_plan_branch"]))
        self.assertEqual({"llm_stage_missing": 28}, breakdown["unknown_reason_counts"])
        unknown = _record(breakdown, "answer-01")
        self.assertEqual((["unknown"], ["llm_stage_missing"], {}, None), (
            unknown["causes"], unknown["unknown_reasons"], unknown["codes"], unknown["plan_branch"]))
        # A known cause is not hidden by the missing stage; the gap stays visible.
        known = _record(breakdown, A)
        self.assertEqual(("retrieval_miss", ["llm_stage_missing"]), (known["primary_cause"], known["unknown_reasons"]))

    def test_case_missing_from_one_stage_or_not_run_is_unknown(self) -> None:
        report = _report()
        report["stages"]["mcp"]["result"]["cases"].pop(3)
        cases = _cases(report)
        dropped = cases.pop(5)["id"]
        report["stages"]["live_llm"]["result"].update(not_run_count=1, not_run_case_ids=[dropped])
        report["stages"]["retrieval"]["result"]["results"].pop(7)
        breakdown = build_failure_breakdown(report)
        self.assertEqual(
            {"answer-03": ["mcp_case_missing"], "answer-05": ["llm_case_not_run"], "answer-07": ["retrieval_case_missing"]},
            {case["id"]: case["unknown_reasons"] for case in breakdown["cases"]})
        self.assertEqual({"unknown"}, {case["primary_cause"] for case in breakdown["cases"]})
        self.assertIsNone(breakdown["dominant_cause"])

    def test_unrecorded_or_unclassifiable_fields_are_unknown(self) -> None:
        report = _report()
        del _retrieval(report, "answer-00")["recall_at_5"]
        del _cases(report)[1]["answer_present"]
        _cases(report)[2].update(status="failed", errors=["brand_new_error_code"])
        _cases(report)[3].update(status="failed", errors=[])
        _cases(report)[4]["correctness"].update(required_count=0, matched_count=0, status="unverified")
        _cases(report)[4].update(status="failed", errors=["gold_facts_unverified"])
        breakdown = build_failure_breakdown(report)
        self.assertEqual({
            "answer-00": ["retrieval_recall_not_recorded"],
            "answer-01": ["answer_presence_not_recorded"],
            "answer-02": ["unmapped_error:brand_new_error_code"],
            "answer-03": ["llm_case_failed_unclassified"],
            "answer-04": ["gold_facts_unlabeled"],
        }, {case["id"]: case["unknown_reasons"] for case in breakdown["cases"]})

    def test_garbage_input_never_raises(self) -> None:
        garbage = [
            {}, {"stages": None}, {"stages": {}}, {"stages": {"retrieval": "x", "mcp": [], "live_llm": 3}},
            {"stages": {"retrieval": {"result": {"results": [None, 1, "x"]}},
                        "mcp": {"result": {"cases": [{"id": None}, {"id": ""}]}},
                        "live_llm": {"result": {"cases": [{"id": "a", "errors": "oops", "correctness": [],
                                                           "citation_checks": 4, "http_status": "200",
                                                           "http_error_code": ["x"]}],
                                                "not_run_case_ids": "nope", "interrupted_reason": 5,
                                                "not_run_count": "many"}}}},
        ]
        for index, report in enumerate(garbage):
            with self.subTest(index=index):
                breakdown = guardrails._build_failure_breakdown(report)
                self.assertTrue(breakdown["diagnostic_only"])
                self.assertIn(breakdown["status"], {"ok", "no_case_data"})
        self.assertEqual("no_case_data", build_failure_breakdown({})["status"])
        self.assertIsNone(build_failure_breakdown({})["dominant_cause"])

    def test_records_without_a_joinable_id_are_counted(self) -> None:
        report = _report()
        report["stages"]["mcp"]["result"]["cases"].append({"id": "", "passed": False})
        self.assertEqual(1, build_failure_breakdown(report)["unjoinable_record_count"])


class FailureBreakdownIsDiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_policy()

    def reports(self) -> dict[str, dict]:
        leaked = _report()
        _scenario_named("negative answered")["mutate"](leaked)
        return {"passed": _report(), "failed": leaked, "incomplete": _report(answerable=10, negative=4),
                "missing_stage": {**_report(), "stages": {**_report()["stages"], "mcp": {"status": "failed"}}}}

    @staticmethod
    def judged(result: dict) -> dict:
        return {key: value for key, value in result.items() if key not in {"generated_at", "failure_breakdown"}}

    def test_breakdown_does_not_change_any_judgement(self) -> None:
        for name, report in self.reports().items():
            with self.subTest(report=name):
                with_breakdown = evaluate_kpi_guardrails(report, self.policy)
                self.assertIn("failure_breakdown", with_breakdown)
                with patch.object(guardrails, "_build_failure_breakdown", side_effect=RuntimeError("boom")):
                    without = evaluate_kpi_guardrails(report, self.policy)
                self.assertEqual("error", without["failure_breakdown"]["status"])
                self.assertEqual(self.judged(with_breakdown), self.judged(without))

    def test_baseline_comparison_keeps_its_verdict_and_gets_a_breakdown(self) -> None:
        candidate = _report()
        _retrieval(candidate, A).update(recall_at_5=0.0, reciprocal_rank=0.0)
        result = evaluate_kpi_guardrails(candidate, self.policy, baseline=_report())
        self.assertEqual("failed", result["verdict"])
        self.assertEqual("retrieval_miss", result["failure_breakdown"]["dominant_cause"])

    def test_breakdown_agrees_with_the_citation_trace_hard_check(self) -> None:
        # Every citation_trace violation shows up as fabricated_citation, except the documented
        # fallback case covered in FallbackCitationTests.
        report = _report()
        for name in ("no citations", "wrong identity", "context mismatch", "invalid shape", "unknown limitation"):
            _scenario_named(name)["mutate"](report)
            result = evaluate_kpi_guardrails(report, self.policy)
            trace = next(row for row in result["hard"] if row["id"] == "citation_trace")
            self.assertEqual(["answer-00"], [row["id"] for row in trace["violations"]], name)
            self.assertIn("answer-00", result["failure_breakdown"]["case_ids"]["any"]["fabricated_citation"], name)
            report = _report()
        clean = evaluate_kpi_guardrails(_report(), self.policy)
        self.assertEqual([], clean["failure_breakdown"]["case_ids"]["any"]["fabricated_citation"])

    def test_summary_has_only_the_dominant_cause_and_nonzero_counts(self) -> None:
        report = _report()
        _scenario_named("recall zero")["mutate"](report)
        summary = failure_breakdown_summary(build_failure_breakdown(report))
        self.assertEqual({"dominant_cause": "retrieval_miss", "counts": {"retrieval_miss": 1}}, summary)
        self.assertEqual({"dominant_cause": None, "counts": {}}, failure_breakdown_summary({"status": "error"}))


class FailureBreakdownCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="kpi_breakdown_test_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def run_main(self, report: dict, name: str) -> tuple[int, dict, dict]:
        path = self.root / f"{name}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        output = self.root / f"{name}-out.json"
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(["--report", str(path), "--output", str(output)])
        return code, json.loads(stream.getvalue()), json.loads(output.read_text(encoding="utf-8"))

    def test_stdout_has_a_short_summary_and_the_file_has_the_full_block(self) -> None:
        report = _report()
        _scenario_named("blocked case answered")["mutate"](report)
        code, summary, written = self.run_main(report, "leak")
        self.assertEqual((1, "failed"), (code, summary["verdict"]))
        self.assertEqual({"dominant_cause": "security_leak", "counts": {"security_leak": 1}},
                         summary["failure_breakdown"])
        self.assertNotIn(N, json.dumps(summary))
        self.assertEqual([N], written["failure_breakdown"]["case_ids"]["primary"]["security_leak"])
        self.assertEqual([N], written["failure_breakdown"]["critical_case_ids"])
        self.assertEqual(APPROVAL, written["failure_breakdown"]["dominant_plan_branch"])

    def test_exit_codes_are_unchanged_by_the_breakdown(self) -> None:
        code, summary, _ = self.run_main(_report(), "clean")
        self.assertEqual((0, "passed"), (code, summary["verdict"]))
        self.assertEqual({"dominant_cause": None, "counts": {}}, summary["failure_breakdown"])
        with patch.object(guardrails, "_build_failure_breakdown", side_effect=RuntimeError("boom")):
            code, summary, written = self.run_main(_report(), "clean-broken-diagnostic")
        self.assertEqual((0, "passed"), (code, summary["verdict"]))
        self.assertEqual("error", written["failure_breakdown"]["status"])


if __name__ == "__main__":
    unittest.main()
