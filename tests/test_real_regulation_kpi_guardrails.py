from __future__ import annotations

import copy
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest

from app.rag.extractive_answer import NO_EVIDENCE_ANSWER
from scripts.check_real_regulation_kpi_guardrails import (
    HARD_CHECKS,
    POLICY_PATH,
    GuardrailInputError,
    evaluate_kpi_guardrails,
    load_policy,
    main,
    validate_policy,
)
from scripts.evaluate_live_local_llm import score_case
from tests.test_evaluate_live_local_llm import _event, _response, _spec


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODES = {"llm_backend": "ollama", "orchestration_mode": "multi_model", "retrieval_mode": "auto",
         "claim_audit_mode": "model"}
# Independent floors from the plan's section 6. Lowering the policy below these
# values must fail here, not only in the reviewed policy file.
TARGET_FLOORS = {"retrieval_recall_at_5": 0.95, "retrieval_mrr": 0.85, "final_model_answer_rate": 0.95,
                 "required_fact_match_rate": 0.90, "case_fact_pass_rate": 0.90}
PROFILE_FLOORS = {"full": {"min_answerable_cases": 20, "min_negative_cases": 8, "min_no_evidence_queries": 4},
                  "compact": {"min_answerable_cases": 10, "min_negative_cases": 4, "min_no_evidence_queries": 2}}
COMPACT_MAX_QUERY_COUNT = 30


def _answer_case(case_id: str, *, response: dict | None = None, duration_ms: float = 100) -> dict:
    result = score_case({**_spec(), "id": case_id}, response if response is not None else _response(),
                        http_status=200, events=[_event((response or _response()).get("answer"))],
                        duration_ms=duration_ms)
    result["request_modes"] = dict(MODES)
    return result


def _abstained_answer_case(case_id: str) -> dict:
    return _answer_case(case_id, response={"answer": NO_EVIDENCE_ANSWER, "citations": [],
                                           "orchestration": {"answer_mode": "abstained"}})


def _negative_case(case_id: str) -> dict:
    result = score_case({"id": case_id, "query": "synthetic missing rule", "expected_abstain": True},
                        {"answer": NO_EVIDENCE_ANSWER, "citations": []}, http_status=200, events=[], duration_ms=50)
    result["request_modes"] = dict(MODES)
    return result


def _report(answerable: int = 20, negative: int = 8) -> dict:
    answer_ids = [f"answer-{index:02d}" for index in range(answerable)]
    negative_ids = [f"negative-{index:02d}" for index in range(negative)]
    retrieval = ([{"id": case_id, "expect_no_evidence": False, "query_spec_valid": True, "search_succeeded": True,
                   "recall_at_5": 1.0, "reciprocal_rank": 1.0, "no_evidence_false_positive": False,
                   "no_evidence_abstained": False} for case_id in answer_ids]
                 + [{"id": case_id, "expect_no_evidence": True, "query_spec_valid": True, "search_succeeded": True,
                     "recall_at_5": 0.0, "reciprocal_rank": None, "no_evidence_false_positive": False,
                     "no_evidence_abstained": True} for case_id in negative_ids])
    return {
        "report_type": "real_regulation_e2e", "schema_version": 1, "repo_commit": "synthetic-commit",
        "query_count": answerable + negative, "query_spec_sha256": "q" * 64,
        "corpus": {"manifest_sha256": "m" * 64, "human_review_complete": True, "documents": []},
        "runtime_source_fingerprint": {"manifest.json": "r" * 64},
        "runtime_scope": {"tenant_id": "synthetic-tenant", "profile_id": None, "tenant_storage_isolation": False},
        "configuration": {"model": "synthetic-model", "timeout_seconds": 600},
        "label_provenance": "synthetic", "passed": True, "product_release_ready": False,
        "human_approval_created": False, "source_state": {"stable": True},
        "stages": {
            "preprocessing": {"status": "passed", "result": {"passed": True}},
            "retrieval": {"status": "passed", "result": {"results": retrieval, "passed": True}},
            "mcp": {"status": "passed", "result": {
                "protocol_checks": {name: {"passed": True} for name in ("initialize", "tools_list", "get_index_status")},
                "cases": [{"id": case_id, "passed": True} for case_id in answer_ids + negative_ids],
                "errors": [], "passed": True}},
            "live_llm": {"status": "passed", "result": {
                "cases": [_answer_case(case_id) for case_id in answer_ids] + [_negative_case(case_id) for case_id in negative_ids],
                "not_run_count": 0, "passed": True,
                "provenance": {"models": [{"name": "synthetic-model", "digest": "sha256:synthetic"}]}}},
            "input_integrity": {"status": "passed", "result": {"passed": True}},
            "active_runtime_integrity": {"status": "passed", "result": {"passed": True}},
        },
    }


def _cases(report: dict) -> list[dict]:
    return report["stages"]["live_llm"]["result"]["cases"]


def _row(result: dict, group: str, row_id: str) -> dict:
    return next(row for row in result[group] if row["id"] == row_id)


class KpiPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_policy()

    def test_policy_is_not_looser_than_documented_floors(self) -> None:
        self.assertEqual(set(HARD_CHECKS), set(self.policy["hard_checks"]))
        for name, floor in TARGET_FLOORS.items():
            with self.subTest(target=name):
                self.assertGreaterEqual(self.policy["targets"][name]["minimum"], floor)
        for profile, floors in PROFILE_FLOORS.items():
            for key, floor in floors.items():
                with self.subTest(profile=profile, key=key):
                    self.assertGreaterEqual(self.policy["coverage_profiles"][profile][key], floor)
        self.assertNotIn("max_query_count", self.policy["coverage_profiles"]["full"])
        self.assertLessEqual(self.policy["coverage_profiles"]["compact"]["max_query_count"], COMPACT_MAX_QUERY_COUNT)
        for name in ("retrieval_new_misses", "mcp_new_failures"):
            self.assertIs(True, self.policy["regressions"][name]["blocking"])
            self.assertEqual(0, self.policy["regressions"][name]["maximum"])
        self.assertLessEqual(self.policy["regressions"]["llm_p95_duration_ratio"]["maximum"], 1.10)
        self.assertEqual("model", self.policy["required_request_modes"]["claim_audit_mode"])
        self.assertEqual("ollama", self.policy["required_request_modes"]["llm_backend"])

    def test_policy_cannot_disable_a_check_or_weaken_its_shape(self) -> None:
        raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        mutations = {
            "hard_check_removed": lambda p: p["hard_checks"].remove("citation_trace"),
            "target_removed": lambda p: p["targets"].pop("retrieval_mrr"),
            "target_disabled": lambda p: p["targets"]["final_model_answer_rate"].update(minimum=0),
            "regression_removed": lambda p: p["regressions"].pop("mcp_new_failures"),
            "blocking_not_boolean": lambda p: p["regressions"]["mcp_new_failures"].update(blocking="true"),
            "profile_without_minimum": lambda p: p["coverage_profiles"]["full"].update(min_answerable_cases=0),
            "default_profile_removed": lambda p: p["coverage_profiles"].pop("full"),
            "request_mode_dropped": lambda p: p["required_request_modes"].pop("claim_audit_mode"),
            "stage_dropped": lambda p: p["required_stages"].remove("mcp"),
            "full_profile_capped": lambda p: p["coverage_profiles"]["full"].update(max_query_count=30),
            "no_evidence_minimum_removed": lambda p: p["coverage_profiles"]["compact"].pop("min_no_evidence_queries"),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                policy = copy.deepcopy(raw)
                mutate(policy)
                with self.assertRaisesRegex(GuardrailInputError, "invalid_kpi_policy"):
                    validate_policy(policy)

    def test_policy_ships_with_the_scripts_package(self) -> None:
        self.assertTrue(POLICY_PATH.is_relative_to(PROJECT_ROOT / "scripts"))
        self.assertIn('scripts = ["policies/*.json"]', (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("include scripts/policies/*.json", (PROJECT_ROOT / "MANIFEST.in").read_text(encoding="utf-8"))


class KpiGuardrailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_policy()

    def evaluate(self, report: dict, **kwargs) -> dict:
        return evaluate_kpi_guardrails(report, self.policy, **kwargs)

    def test_complete_synthetic_run_passes_with_exact_counts(self) -> None:
        result = self.evaluate(_report())
        self.assertEqual("passed", result["verdict"])
        self.assertTrue(result["hard_passed"])
        self.assertTrue(result["targets_met"])
        self.assertIsNone(result["regression_passed"])
        self.assertEqual({"not_evaluated"}, {row["status"] for row in result["regressions"]})
        recall = _row(result, "targets", "retrieval_recall_at_5")
        self.assertEqual((20, 20), (recall["full_hit_count"], recall["sample_size"]))
        self.assertEqual(self.policy["sha256"], result["policy"]["sha256"])

    def test_targets_allow_exact_failure_budget_while_legacy_verdict_stays_strict(self) -> None:
        report = _report()
        _cases(report)[0] = _abstained_answer_case("answer-00")
        report["passed"] = False
        result = self.evaluate(report)
        rate = _row(result, "targets", "final_model_answer_rate")
        self.assertEqual((19, 20, 19, 1), (rate["numerator"], rate["denominator"],
                                           rate["required_successes"], rate["allowed_failures"]))
        self.assertEqual("passed", result["verdict"])
        self.assertFalse(result["candidate"]["legacy_report_passed"])

        _cases(report)[1] = _abstained_answer_case("answer-01")
        result = self.evaluate(report)
        self.assertEqual("failed", _row(result, "targets", "final_model_answer_rate")["status"])
        self.assertEqual("failed", result["verdict"])
        self.assertTrue(result["hard_passed"])

    def test_any_hard_violation_fails_even_when_targets_are_met(self) -> None:
        def forbidden(report: dict) -> None:
            response = _response()
            response["answer"] = "Submit within seven days, not thirty days. [E1]"
            _cases(report)[0] = _answer_case("answer-00", response=response)

        def uncited(report: dict) -> None:
            response = _response()
            response["citations"] = []
            _cases(report)[0] = _answer_case("answer-00", response=response)

        def leaked_negative(report: dict) -> None:
            case = score_case({"id": "negative-00", "query": "missing", "expected_abstain": True}, _response(),
                              http_status=200, events=[_event()], duration_ms=50)
            case["request_modes"] = dict(MODES)
            _cases(report)[20] = case

        def retrieval_false_positive(report: dict) -> None:
            item = report["stages"]["retrieval"]["result"]["results"][20]
            item.update(no_evidence_false_positive=True, no_evidence_abstained=False)

        def deterministic_audit(report: dict) -> None:
            _cases(report)[0]["request_modes"]["claim_audit_mode"] = "deterministic"

        def mcp_case_failed(report: dict) -> None:
            report["stages"]["mcp"]["result"]["cases"][0]["passed"] = False

        def approval_created(report: dict) -> None:
            report["human_approval_created"] = True

        def runtime_changed(report: dict) -> None:
            report["stages"]["active_runtime_integrity"]["status"] = "failed"

        for check_id, mutate in (("forbidden_facts", forbidden), ("citation_trace", uncited),
                                 ("negative_cases", leaked_negative), ("retrieval_no_evidence", retrieval_false_positive),
                                 ("request_modes", deterministic_audit), ("mcp_contract", mcp_case_failed),
                                 ("no_auto_approval", approval_created), ("integrity", runtime_changed)):
            with self.subTest(check=check_id):
                report = _report()
                mutate(report)
                result = self.evaluate(report)
                self.assertEqual("failed", _row(result, "hard", check_id)["status"])
                self.assertEqual("failed", result["verdict"])
                self.assertFalse(result["passed"])

    def test_missing_duplicate_or_unrun_cases_cannot_pass(self) -> None:
        def dropped(report: dict) -> None:
            _cases(report).pop()

        def duplicated(report: dict) -> None:
            _cases(report)[1] = _answer_case("answer-00")

        def not_run(report: dict) -> None:
            report["stages"]["live_llm"]["result"]["not_run_count"] = 1

        def interrupted(report: dict) -> None:
            report["stages"]["live_llm"]["result"]["interrupted_reason"] = "consecutive_server_failures"

        def stage_missing(report: dict) -> None:
            del report["stages"]["mcp"]

        def search_error(report: dict) -> None:
            report["stages"]["retrieval"]["result"]["results"][0]["search_succeeded"] = False

        for name, mutate in (("dropped", dropped), ("duplicated", duplicated), ("not_run", not_run),
                             ("interrupted", interrupted), ("stage_missing", stage_missing),
                             ("search_error", search_error)):
            with self.subTest(mutation=name):
                report = _report()
                mutate(report)
                result = self.evaluate(report)
                self.assertEqual("failed", _row(result, "hard", "coverage")["status"])
                self.assertEqual("failed", result["verdict"])

    def test_missing_llm_evidence_is_unavailable_and_fails(self) -> None:
        report = _report()
        report["stages"]["live_llm"] = {"status": "failed", "error_type": "TimeoutError"}
        result = self.evaluate(report)
        for check_id in ("request_modes", "negative_cases", "forbidden_facts", "citation_trace"):
            self.assertEqual("unavailable", _row(result, "hard", check_id)["status"])
        self.assertEqual("unavailable", _row(result, "targets", "final_model_answer_rate")["status"])
        self.assertEqual("failed", result["verdict"])

    def test_answer_presence_must_be_recorded_for_citation_trace(self) -> None:
        report = _report()
        del _cases(report)[0]["answer_present"]
        result = self.evaluate(report)
        self.assertIn("answer_presence_not_recorded", _row(result, "hard", "citation_trace")["reasons"])

    def test_small_samples_are_incomplete_until_the_declared_profile_allows_them(self) -> None:
        result = self.evaluate(_report(answerable=10, negative=4))
        self.assertEqual("incomplete", result["verdict"])
        self.assertFalse(result["passed"])
        self.assertEqual("insufficient_sample", _row(result, "targets", "retrieval_mrr")["status"])
        self.assertEqual("insufficient_sample", _row(result, "hard", "negative_cases")["status"])

        compact = self.evaluate(_report(answerable=10, negative=4), coverage_profile="compact")
        self.assertEqual("passed", compact["verdict"])
        self.assertEqual("compact", compact["coverage_profile"])
        with self.assertRaisesRegex(GuardrailInputError, "unknown_coverage_profile"):
            self.evaluate(_report(), coverage_profile="tiny")

    def test_compact_profile_cannot_judge_a_larger_run(self) -> None:
        with self.assertRaisesRegex(GuardrailInputError, "coverage_profile_not_allowed_for_query_count"):
            self.evaluate(_report(answerable=10, negative=21), coverage_profile="compact")

    def test_few_no_evidence_queries_are_insufficient_but_a_leak_still_fails(self) -> None:
        report = _report(negative=3)
        result = self.evaluate(report)
        self.assertEqual("insufficient_sample", _row(result, "hard", "retrieval_no_evidence")["status"])
        self.assertEqual("incomplete", result["verdict"])

        report["stages"]["retrieval"]["result"]["results"][20].update(no_evidence_false_positive=True,
                                                                     no_evidence_abstained=False)
        result = self.evaluate(report)
        self.assertEqual("failed", _row(result, "hard", "retrieval_no_evidence")["status"])
        self.assertEqual("failed", result["verdict"])

    def test_unlabeled_answerable_cases_make_fact_targets_unavailable(self) -> None:
        report = _report()
        for case in _cases(report)[:2]:
            case["correctness"].update(required_count=0, matched_count=0, status="unverified")
        result = self.evaluate(report)
        for target_id in ("required_fact_match_rate", "case_fact_pass_rate"):
            row = _row(result, "targets", target_id)
            self.assertEqual(("unavailable", ["answer-00", "answer-01"]), (row["status"], row["unlabeled_case_ids"]))
        self.assertEqual("incomplete", result["verdict"])

    def test_mean_targets_are_judged_before_display_rounding(self) -> None:
        report = _report()
        results = report["stages"]["retrieval"]["result"]["results"]
        results[0]["recall_at_5"] = 0.999992
        results[1]["recall_at_5"] = 0.0
        recall = _row(self.evaluate(report), "targets", "retrieval_recall_at_5")
        self.assertEqual(0.95, recall["value"])
        self.assertEqual("failed", recall["status"])

    def test_unknown_verification_limitation_fails_citation_trace(self) -> None:
        report = _report()
        _cases(report)[0]["verification_limitations"] = ["identity_not_verified"]
        trace = _row(self.evaluate(report), "hard", "citation_trace")
        self.assertEqual("failed", trace["status"])
        self.assertIn("unknown_verification_limitation", trace["violations"][0]["reasons"])

    def test_no_negative_cases_is_unavailable_not_insufficient(self) -> None:
        result = self.evaluate(_report(negative=0))
        self.assertEqual("unavailable", _row(result, "hard", "negative_cases")["status"])
        self.assertEqual("unavailable", _row(result, "hard", "retrieval_no_evidence")["status"])
        self.assertEqual("failed", result["verdict"])

    def test_fallback_citation_without_hash_is_listed_but_identity_still_required(self) -> None:
        report = _report()
        _cases(report)[0]["verification_limitations"] = ["deterministic_citation_omits_context_and_hash"]
        result = self.evaluate(report)
        trace = _row(result, "hard", "citation_trace")
        self.assertEqual("passed", trace["status"])
        self.assertEqual(["answer-00"], trace["limited_trace_case_ids"])
        self.assertIn("some_citations_verified_without_context_or_content_hash", result["limitations"])

        _cases(report)[0]["citation_checks"]["all_verified"] = False
        self.assertEqual("failed", _row(self.evaluate(report), "hard", "citation_trace")["status"])

    def test_unsupported_report_is_an_input_error(self) -> None:
        for report in ([], {**_report(), "report_type": "other"}, {**_report(), "schema_version": 2}):
            with self.subTest(report=type(report).__name__):
                with self.assertRaisesRegex(GuardrailInputError, "unsupported_report"):
                    self.evaluate(report)


class KpiRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_policy()
        self.baseline = _report()

    def evaluate(self, candidate: dict) -> dict:
        return evaluate_kpi_guardrails(candidate, self.policy, baseline=self.baseline)

    def test_identical_inputs_are_comparable_and_pass(self) -> None:
        result = self.evaluate(_report())
        self.assertTrue(result["comparability"]["comparable"])
        self.assertEqual({"synthetic-model": "sha256:synthetic"}, result["comparability"]["called_models"])
        self.assertIs(True, result["regression_passed"])
        self.assertEqual("passed", result["verdict"])

    def test_new_retrieval_miss_blocks_even_when_mean_recall_target_holds(self) -> None:
        candidate = _report()
        candidate["stages"]["retrieval"]["result"]["results"][0].update(recall_at_5=0.0, reciprocal_rank=0.0)
        result = self.evaluate(candidate)
        self.assertEqual("passed", _row(result, "targets", "retrieval_recall_at_5")["status"])
        misses = _row(result, "regressions", "retrieval_new_misses")
        self.assertEqual(("failed", ["answer-00"]), (misses["status"], misses["case_ids"]))
        self.assertEqual("failed", result["verdict"])

    def test_new_mcp_failure_blocks(self) -> None:
        candidate = _report()
        candidate["stages"]["mcp"]["result"]["cases"][3]["passed"] = False
        result = self.evaluate(candidate)
        self.assertEqual("failed", _row(result, "regressions", "mcp_new_failures")["status"])
        self.assertFalse(result["regression_passed"])

    def test_llm_case_and_latency_regressions_are_advisory(self) -> None:
        candidate = _report()
        _cases(candidate)[0] = _abstained_answer_case("answer-00")
        for case in _cases(candidate):
            case["duration_ms"] = case["duration_ms"] * 2
        result = self.evaluate(candidate)
        self.assertEqual("advisory_exceeded", _row(result, "regressions", "llm_new_case_failures")["status"])
        latency = _row(result, "regressions", "llm_p95_duration_ratio")
        self.assertEqual(("advisory_exceeded", 2.0), (latency["status"], latency["value"]))
        self.assertEqual("passed", result["verdict"])
        self.assertIs(True, result["regression_passed"])

    def test_different_inputs_or_models_are_not_comparable(self) -> None:
        def query_changed(report: dict) -> None:
            report["query_spec_sha256"] = "z" * 64

        def mode_changed(report: dict) -> None:
            report["configuration"]["timeout_seconds"] = 900

        def digest_changed(report: dict) -> None:
            report["stages"]["live_llm"]["result"]["provenance"]["models"][0]["digest"] = "sha256:other"

        def digest_missing(report: dict) -> None:
            report["stages"]["live_llm"]["result"]["provenance"]["models"] = []

        def no_model_called(report: dict) -> None:
            for case in _cases(report):
                case["generation_events"] = []

        for expected, mutate in (("query_spec_sha256", query_changed), ("configuration", mode_changed),
                                 ("called_model_digests", digest_changed),
                                 ("called_model_digest_unavailable", digest_missing),
                                 ("called_model_digest_unavailable", no_model_called)):
            with self.subTest(difference=expected):
                candidate = _report()
                mutate(candidate)
                result = self.evaluate(candidate)
                self.assertEqual("not_comparable", result["verdict"])
                self.assertIn(expected, result["comparability"]["differences"])
                self.assertEqual([], result["regressions"])
                self.assertIs(False, result["regression_passed"])

    def test_duplicate_baseline_ids_cannot_hide_a_new_miss(self) -> None:
        results = self.baseline["stages"]["retrieval"]["result"]["results"]
        results[1] = {**results[0]}
        candidate = _report()
        candidate["stages"]["retrieval"]["result"]["results"][1]["recall_at_5"] = 0.0
        result = self.evaluate(candidate)
        self.assertEqual("not_comparable", result["verdict"])
        self.assertIn("baseline_coverage_failed", result["comparability"]["differences"])


class KpiHoldoutProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_policy()

    def holdout_report(self) -> dict:
        report = _report()
        report["query_spec_sha256"] = "a" * 64
        report["query_split"] = "holdout"
        report["holdout_ledger_record"] = {"event": "opened", "query_spec_sha256": "a" * 64,
                                           "ledger_id": "1" * 32, "authorized_rerun": False}
        return report

    def evaluate(self, report: dict) -> dict:
        return evaluate_kpi_guardrails(report, self.policy)

    def test_policy_requires_the_holdout_check(self) -> None:
        self.assertIn("holdout_protocol", HARD_CHECKS)
        self.assertIn("holdout_protocol", self.policy["hard_checks"])
        raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        raw["hard_checks"].remove("holdout_protocol")
        with self.assertRaisesRegex(GuardrailInputError, "invalid_kpi_policy"):
            validate_policy(raw)

    def test_non_holdout_splits_pass_and_are_recorded(self) -> None:
        for split, report in (("unspecified", _report()),
                              ("unspecified", {**_report(), "query_split": "unspecified"}),
                              ("development", {**_report(), "query_split": "development"})):
            with self.subTest(split=split):
                result = self.evaluate(report)
                row = _row(result, "hard", "holdout_protocol")
                self.assertEqual(("passed", split), (row["status"], row["split"]))
                self.assertEqual(split, result["candidate"]["query_split"])
                self.assertEqual("passed", result["verdict"])

    def test_opened_holdout_record_with_matching_hash_passes(self) -> None:
        result = self.evaluate(self.holdout_report())
        row = _row(result, "hard", "holdout_protocol")
        self.assertEqual(("passed", "holdout", []), (row["status"], row["split"], row["reasons"]))
        self.assertEqual("holdout", result["candidate"]["query_split"])
        self.assertEqual(("1" * 32, False), (result["candidate"]["holdout_ledger_id"], result["candidate"]["authorized_rerun"]))
        self.assertEqual("passed", result["verdict"])

    def test_candidate_shows_whether_the_holdout_was_an_authorized_rerun(self) -> None:
        report = self.holdout_report()
        report["holdout_ledger_record"]["authorized_rerun"] = True
        self.assertIs(True, self.evaluate(report)["candidate"]["authorized_rerun"])
        for plain in (_report(), {**_report(), "query_split": "development"}):
            candidate = self.evaluate(plain)["candidate"]
            self.assertEqual((None, None), (candidate["holdout_ledger_id"], candidate["authorized_rerun"]))

    def test_holdout_report_cannot_be_a_baseline(self) -> None:
        candidate = _report()
        candidate["query_spec_sha256"] = "a" * 64
        for baseline in (self.holdout_report(), {**self.holdout_report(), "query_split": "Holdout"}):
            result = evaluate_kpi_guardrails(candidate, self.policy, baseline=baseline)
            with self.subTest(split=baseline["query_split"]):
                self.assertEqual("not_comparable", result["verdict"])
                self.assertEqual(["baseline_is_holdout"], result["comparability"]["differences"])
                self.assertEqual([], result["regressions"])
        for allowed in ("development", "unspecified"):
            result = evaluate_kpi_guardrails(candidate, self.policy, baseline={**_report(), "query_split": allowed,
                                                                             "query_spec_sha256": "a" * 64})
            with self.subTest(split=allowed):
                self.assertTrue(result["comparability"]["comparable"])

    def test_holdout_report_without_a_valid_opened_record_fails(self) -> None:
        def missing(report: dict) -> None:
            del report["holdout_ledger_record"]

        def not_a_record(report: dict) -> None:
            report["holdout_ledger_record"] = "opened"

        def sealed_only(report: dict) -> None:
            report["holdout_ledger_record"]["event"] = "sealed"

        def other_spec(report: dict) -> None:
            report["holdout_ledger_record"]["query_spec_sha256"] = "b" * 64

        def record_without_hash(report: dict) -> None:
            del report["holdout_ledger_record"]["query_spec_sha256"]

        def no_ledger_id(report: dict) -> None:
            del report["holdout_ledger_record"]["ledger_id"]

        def empty_ledger_id(report: dict) -> None:
            report["holdout_ledger_record"]["ledger_id"] = ""

        def both_hashes_empty(report: dict) -> None:
            report["query_spec_sha256"] = ""
            report["holdout_ledger_record"]["query_spec_sha256"] = ""

        for expected, mutate in (("holdout_ledger_record_missing", missing), ("holdout_ledger_record_missing", not_a_record),
                                 ("holdout_ledger_record_not_opened", sealed_only),
                                 ("holdout_ledger_record_spec_mismatch", other_spec),
                                 ("holdout_ledger_record_spec_mismatch", record_without_hash),
                                 ("holdout_ledger_record_spec_mismatch", both_hashes_empty),
                                 ("holdout_ledger_id_missing", no_ledger_id),
                                 ("holdout_ledger_id_missing", empty_ledger_id)):
            with self.subTest(mutation=mutate.__name__):
                report = self.holdout_report()
                mutate(report)
                result = self.evaluate(report)
                row = _row(result, "hard", "holdout_protocol")
                self.assertEqual("failed", row["status"])
                self.assertIn(expected, row["reasons"])
                self.assertEqual(("failed", False), (result["verdict"], result["passed"]))

    def test_unknown_split_value_cannot_skip_the_holdout_check(self) -> None:
        for value in ("Holdout", "holdout ", "test", "", None, ["holdout"], 1):
            with self.subTest(split=value):
                report = _report()
                report["query_split"] = value
                row = _row(self.evaluate(report), "hard", "holdout_protocol")
                self.assertEqual(("failed", ["query_split_invalid"]), (row["status"], row["reasons"]))


class KpiGuardrailCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="kpi_guardrail_test_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def write(self, name: str, payload: object) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def run_main(self, *args: str) -> tuple[int, dict]:
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(list(args))
        return code, json.loads(stream.getvalue())

    def test_exit_codes_follow_the_verdict(self) -> None:
        passing = self.write("passing.json", _report())
        failing_report = _report()
        failing_report["human_approval_created"] = True
        failing = self.write("failing.json", failing_report)
        changed = _report()
        changed["query_spec_sha256"] = "z" * 64
        incomparable = self.write("changed.json", changed)

        code, summary = self.run_main("--report", str(passing), "--output", str(self.root / "pass-out.json"))
        self.assertEqual((0, "passed"), (code, summary["verdict"]))
        written = json.loads((self.root / "pass-out.json").read_text(encoding="utf-8"))
        self.assertEqual(64, len(written["candidate"]["report_sha256"]))

        code, summary = self.run_main("--report", str(failing), "--output", str(self.root / "fail-out.json"))
        self.assertEqual((1, "failed"), (code, summary["verdict"]))

        code, summary = self.run_main("--report", str(incomparable), "--baseline", str(passing),
                                      "--output", str(self.root / "compare-out.json"))
        self.assertEqual((2, "not_comparable"), (code, summary["verdict"]))
        self.assertTrue((self.root / "compare-out.json").is_file())

    def test_inputs_errors_and_existing_output_exit_two_without_overwrite(self) -> None:
        report = self.write("report.json", _report())
        existing = self.write("existing.json", {"keep": True})
        code, summary = self.run_main("--report", str(report), "--output", str(existing))
        self.assertEqual((2, "output_already_exists"), (code, summary["reason_code"]))
        self.assertEqual({"keep": True}, json.loads(existing.read_text(encoding="utf-8")))

        code, summary = self.run_main("--report", str(report), "--output", str(PROJECT_ROOT / "kpi-out.json"))
        self.assertEqual((2, "private_path_inside_public_checkout"), (code, summary["reason_code"]))
        self.assertFalse((PROJECT_ROOT / "kpi-out.json").exists())

        unsupported = self.write("unsupported.json", {"report_type": "other"})
        code, summary = self.run_main("--report", str(unsupported), "--output", str(self.root / "out.json"))
        self.assertEqual((2, "unsupported_report"), (code, summary["reason_code"]))


if __name__ == "__main__":
    unittest.main()
