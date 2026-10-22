from __future__ import annotations

"""Repeated core-case runs, cold/warm latency labels and their KPI checker use.

Synthetic data only. No real model, API or private document is involved.
"""

from contextlib import ExitStack, contextmanager, nullcontext, redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.check_real_regulation_kpi_guardrails import (
    build_stability,
    evaluate_kpi_guardrails,
    load_policy,
    main as kpi_main,
)
from scripts.evaluate_live_local_llm import (
    COLD_LOAD_DURATION_NS,
    classify_latency,
    evaluate_live_local_llm,
    generation_observation,
    model_load_summary,
    score_case,
    summarize_cases,
    validate_core_repeat,
)
from scripts.run_real_regulation_e2e import (
    EvidenceError,
    RunOptions,
    options_from_config,
    run_real_regulation_e2e,
    seal_holdout,
    validate_options,
)
from tests.test_evaluate_live_local_llm import _response, _spec
from tests.test_real_regulation_kpi_guardrails import _cases, _report, _row


ANSWER_MODEL = "synthetic-model"


def _event_with_load(load_ns: int | None, answer: str | None = None, *, completed: bool = True,
                     model: str = ANSWER_MODEL) -> dict:
    request = {"model": model, "prompt": "synthetic-private-prompt", "stream": False,
               "options": {"temperature": 0, "seed": 0, "num_predict": 200}}
    payload = {"model": model, "done": True, "done_reason": "stop",
               "eval_count": 10 if completed else 0,
               "response": json.dumps({"answer": answer or _response()["answer"], "claims": []})}
    if load_ns is not None:
        payload["load_duration"] = load_ns
    return {"route": "/api/generate", **generation_observation(request, payload, status=200, duration_ms=100)}


def _variant_response(suffix: str) -> dict:
    return {**_response(), "answer": _response()["answer"] + " " + suffix}


class _Scenario:
    """Scripted API and observer in one object; ``steps[n]`` drives the n-th request."""

    def __init__(self, steps: list[dict]) -> None:
        self.steps = steps
        self.index = -1
        self.case_order: list[str] = []

    def _step(self) -> dict:
        return self.steps[self.index] if 0 <= self.index < len(self.steps) else {}

    def read_json(self, url, *, payload=None, headers=None, timeout=10):
        step = self._step()
        if "error" in step:
            raise step["error"]
        return step.get("status", 200), step.get("response", _response())

    @contextmanager
    def case(self, case_id):
        self.index += 1
        self.case_order.append(case_id)
        yield

    def events_for(self, _case_id):
        step = self._step()
        if "error" in step or step.get("status", 200) != 200:
            return []
        answer = step.get("response", _response())["answer"]
        return [_event_with_load(step.get("load_ns", 0), answer)]

    def provenance(self):
        return {"observer": "synthetic-unit-test"}


def _specs(*ids: str) -> list[dict]:
    return [{**_spec(), "id": case_id} for case_id in ids]


def _evaluate(scenario: _Scenario, specs: list[dict], directory: str, **kwargs) -> dict:
    with patch("scripts.evaluate_live_local_llm._read_json", side_effect=scenario.read_json):
        return evaluate_live_local_llm(api_endpoint="http://127.0.0.1:123", query_spec=specs,
                                       observer=scenario, output_dir=Path(directory), **kwargs)


class CoreRepeatLoopTests(unittest.TestCase):
    def test_listed_cases_repeat_round_robin_after_the_main_pass_without_entering_cases(self) -> None:
        scenario = _Scenario([{}] * 7)
        specs = _specs("a", "b", "c")
        with tempfile.TemporaryDirectory() as temp:
            report = _evaluate(scenario, specs, temp, core_repeat={"case_ids": ["c", "a"], "extra_runs": 2})
            files = sorted(path.name for path in Path(temp).glob("*.json"))
            repeat_artifact = json.loads((Path(temp) / report["repetitions"][0]["artifact_name"]).read_text(encoding="utf-8"))
        self.assertEqual(["a", "b", "c", "a", "c", "a", "c"], scenario.case_order)
        self.assertEqual(["a", "b", "c"], [case["id"] for case in report["cases"]])
        self.assertEqual(3, report["aggregate"]["case_count"])
        self.assertEqual(0, report["not_run_count"])
        self.assertTrue(report["passed"])
        self.assertEqual(["a", "c", "a", "c"], [row["id"] for row in report["repetitions"]])
        self.assertEqual([1, 1, 2, 2], [row["repeat_index"] for row in report["repetitions"]])
        self.assertNotIn("repeat_index", report["cases"][0])
        # The plan is recorded sorted whatever order the config listed.
        self.assertEqual({"case_ids": ["a", "c"], "extra_runs": 2}, report["repetition_plan"])
        self.assertEqual(["a", "c"], report["repetition_summary"]["case_ids"])
        self.assertEqual((4, 0), (report["repetitions_expected_count"], report["repetitions_not_run_count"]))
        self.assertNotIn("repetitions_interrupted_reason", report)
        self.assertEqual(1, repeat_artifact["result"]["repeat_index"])
        # Seven distinct artifacts plus the report; repeat names cannot collide with case names.
        names = [row["artifact_name"] for row in report["cases"] + report["repetitions"]]
        self.assertEqual(7, len(set(names)))
        self.assertEqual(sorted(names + ["live-local-llm-report.json"]), files)
        self.assertEqual(["case-0001-", "case-0002-", "case-0003-"], [name[:10] for name in names[:3]])
        self.assertTrue(all(name.startswith("repeat-") for name in names[3:]))
        self.assertTrue(report["repetitions"][0]["artifact_name"].startswith("repeat-0001-r01-"))
        self.assertTrue(report["repetitions"][1]["artifact_name"].startswith("repeat-0003-r01-"))
        self.assertTrue(report["repetitions"][2]["artifact_name"].startswith("repeat-0001-r02-"))

    def test_artifact_names_do_not_contain_the_case_id(self) -> None:
        scenario = _Scenario([{}] * 2)
        with tempfile.TemporaryDirectory() as temp:
            report = _evaluate(scenario, _specs("../../synthetic-case"), temp,
                               core_repeat={"case_ids": ["../../synthetic-case"], "extra_runs": 1})
        for row in report["cases"] + report["repetitions"]:
            self.assertNotIn("synthetic-case", row["artifact_name"])
            self.assertNotIn("/", row["artifact_name"])

    def test_summary_counts_runs_statuses_and_distinct_final_answers(self) -> None:
        steps = [{}, {}, {}, {"response": _variant_response("Another wording.")}, {}, {}]
        scenario = _Scenario(steps)
        with tempfile.TemporaryDirectory() as temp:
            report = _evaluate(scenario, _specs("a", "b"), temp, core_repeat={"case_ids": ["a", "b"], "extra_runs": 2})
        summary = report["repetition_summary"]
        self.assertEqual((6, 6), (summary["core_run_count"], summary["expected_core_run_count"]))
        by_id = {row["id"]: row for row in summary["cases"]}
        self.assertEqual(3, by_id["a"]["run_count"])
        self.assertEqual(["passed"] * 3, by_id["a"]["statuses"])
        self.assertTrue(by_id["a"]["all_passed"] and by_id["a"]["complete"])
        # Repeat order is a1 b1 a2 b2 a3 b3, so the variant is the second run of "b".
        self.assertEqual((1, 2), (by_id["a"]["distinct_answer_digests"], by_id["b"]["distinct_answer_digests"]))
        self.assertEqual(3, len(by_id["a"]["duration_ms"]))
        self.assertEqual(["warm"] * 3, by_id["a"]["latency_classes"])
        self.assertIsNotNone(summary["p50_duration_ms"])
        self.assertIsNotNone(summary["p95_duration_ms"])
        self.assertEqual((0, 6), (summary["cold_run_count"], summary["warm_run_count"]))
        self.assertNotIn("first_token", json.dumps(summary))
        self.assertIs(False, report["timing_policy"]["first_token_latency_measured"])
        self.assertEqual(COLD_LOAD_DURATION_NS, report["timing_policy"]["cold_load_threshold_ns"])

    def test_a_run_without_an_answer_is_its_own_value_in_the_distinct_count(self) -> None:
        bad = {**_response(), "answer": "Submit within thirty days. [E1]"}
        unavailable = {"status": 503, "response": {}}
        # Three failed runs; only the first produced an answer text.
        scenario = _Scenario([{"response": bad}, unavailable, unavailable])
        with tempfile.TemporaryDirectory() as temp:
            report = _evaluate(scenario, _specs("a"), temp, core_repeat={"case_ids": ["a"], "extra_runs": 2})
        row = report["repetition_summary"]["cases"][0]
        self.assertEqual(["failed"] * 3, row["statuses"])
        self.assertEqual((2, 2, 3), (row["distinct_answer_digests"], row["runs_without_answer"], row["run_count"]))

    def test_distinct_count_stays_one_when_every_run_has_the_same_answer_or_none(self) -> None:
        unavailable = {"status": 503, "response": {}}
        for name, steps, expected_without in (("same_answer", [{}, {}], 0),
                                              ("no_answer_anywhere", [unavailable, unavailable], 2)):
            with self.subTest(name), tempfile.TemporaryDirectory() as temp:
                report = _evaluate(_Scenario(steps), _specs("a"), temp, core_repeat={"case_ids": ["a"], "extra_runs": 1})
                row = report["repetition_summary"]["cases"][0]
                self.assertEqual((1, expected_without), (row["distinct_answer_digests"], row["runs_without_answer"]))

    def test_repeat_failure_is_scored_but_does_not_change_main_pass_or_passed(self) -> None:
        bad = {**_response(), "answer": "Submit within thirty days. [E1]"}
        scenario = _Scenario([{}, {"response": bad}])
        with tempfile.TemporaryDirectory() as temp:
            report = _evaluate(scenario, _specs("a"), temp, core_repeat={"case_ids": ["a"], "extra_runs": 1})
        self.assertTrue(report["passed"])
        self.assertEqual(0, report["aggregate"]["failed_count"])
        self.assertEqual("failed", report["repetitions"][0]["status"])
        self.assertEqual([0], report["repetitions"][0]["correctness"]["forbidden_match_indices"])
        row = report["repetition_summary"]["cases"][0]
        self.assertEqual(["passed", "failed"], row["statuses"])
        self.assertFalse(row["all_passed"])

    def test_failed_main_case_is_still_repeated(self) -> None:
        bad = {**_response(), "answer": "Submit within thirty days. [E1]"}
        scenario = _Scenario([{"response": bad}, {}])
        with tempfile.TemporaryDirectory() as temp:
            report = _evaluate(scenario, _specs("a"), temp, core_repeat={"case_ids": ["a"], "extra_runs": 1})
        self.assertFalse(report["passed"])
        self.assertEqual(["failed", "passed"], report["repetition_summary"]["cases"][0]["statuses"])
        self.assertNotIn("interrupted_reason", report)

    def test_transport_error_in_a_repeat_stops_the_remaining_repeats(self) -> None:
        scenario = _Scenario([{}, {}, {}, {"error": TimeoutError("synthetic secret")}, {}, {}])
        with tempfile.TemporaryDirectory() as temp:
            report = _evaluate(scenario, _specs("a", "b"), temp, core_repeat={"case_ids": ["a", "b"], "extra_runs": 2})
        self.assertEqual(4, len(scenario.case_order))
        self.assertEqual("transport_or_observer_failure", report["repetitions_interrupted_reason"])
        self.assertEqual(2, len(report["repetitions"]))
        self.assertEqual("TimeoutError", report["repetitions"][1]["error_type"])
        self.assertEqual((4, 2), (report["repetitions_expected_count"], report["repetitions_not_run_count"]))
        self.assertNotIn("interrupted_reason", report)
        self.assertTrue(report["passed"])
        self.assertNotIn("synthetic secret", json.dumps(report))
        rows = {row["id"]: row for row in report["repetition_summary"]["cases"]}
        self.assertFalse(rows["a"]["complete"])
        self.assertFalse(rows["b"]["complete"])

    def test_server_failure_counter_carries_over_from_the_main_pass_and_resets_on_success(self) -> None:
        cases = (
            # (main statuses, repeat statuses, expected repeat count, expected reason)
            ([200, 503], [503, 200], 1, "consecutive_server_failures"),
            ([503, 200], [503, 200], 2, None),
            ([200, 200], [503, 200, 503, 503], 4, "consecutive_server_failures"),
        )
        for main_statuses, repeat_statuses, expected_count, reason in cases:
            with self.subTest(main=main_statuses, repeats=repeat_statuses):
                steps = [{"status": status, "response": {}} if status != 200 else {}
                         for status in main_statuses + repeat_statuses]
                scenario = _Scenario(steps)
                ids = [f"case{index}" for index in range(len(main_statuses))]
                runs = len(repeat_statuses) // len(ids)
                with tempfile.TemporaryDirectory() as temp:
                    report = _evaluate(scenario, _specs(*ids), temp,
                                       core_repeat={"case_ids": ids, "extra_runs": runs})
                self.assertNotIn("interrupted_reason", report)
                self.assertEqual(expected_count, len(report["repetitions"]))
                self.assertEqual(reason, report.get("repetitions_interrupted_reason"))

    def test_interrupted_main_pass_runs_no_repeats(self) -> None:
        scenario = _Scenario([{"error": TimeoutError("synthetic")}])
        with tempfile.TemporaryDirectory() as temp:
            report = _evaluate(scenario, _specs("a", "b"), temp, core_repeat={"case_ids": ["a", "b"], "extra_runs": 1})
        self.assertEqual(1, len(scenario.case_order))
        self.assertEqual("transport_or_observer_failure", report["interrupted_reason"])
        self.assertEqual("main_pass_interrupted", report["repetitions_interrupted_reason"])
        self.assertEqual([], report["repetitions"])
        self.assertEqual((2, 2), (report["repetitions_expected_count"], report["repetitions_not_run_count"]))
        rows = {row["id"]: row for row in report["repetition_summary"]["cases"]}
        self.assertEqual((1, 0), (rows["a"]["run_count"], rows["b"]["run_count"]))
        self.assertFalse(report["passed"])

    def test_repeat_artifact_write_failure_is_recorded_and_the_main_pass_report_is_still_written(self) -> None:
        from scripts import evaluate_live_local_llm as module
        real_write = module._write_private_json
        for prefix, repetitions_kept in (("repeat-0001-", 0), ("repeat-0002-", 1)):
            def failing_write(directory, filename, payload, _prefix=prefix):
                if filename.startswith(_prefix):
                    raise OSError("synthetic secret disk path")
                return real_write(directory, filename, payload)

            with self.subTest(failing=prefix):
                scenario = _Scenario([{}, {}, {}, {}])
                with tempfile.TemporaryDirectory() as temp, patch(
                    "scripts.evaluate_live_local_llm._write_private_json", side_effect=failing_write
                ):
                    report = _evaluate(scenario, _specs("a", "b"), temp,
                                       core_repeat={"case_ids": ["a", "b"], "extra_runs": 1})
                    written = json.loads((Path(temp) / "live-local-llm-report.json").read_text(encoding="utf-8"))
                self.assertEqual("repeat_phase_error", report["repetitions_interrupted_reason"])
                self.assertEqual("OSError", report["repetitions_error_type"])
                self.assertEqual(repetitions_kept, len(report["repetitions"]))
                self.assertEqual(2 - repetitions_kept, report["repetitions_not_run_count"])
                # The main pass is complete, scored and persisted regardless.
                self.assertEqual(["a", "b"], [case["id"] for case in report["cases"]])
                self.assertTrue(report["passed"])
                self.assertEqual(0, report["not_run_count"])
                self.assertNotIn("interrupted_reason", report)
                self.assertEqual(("repeat_phase_error", "OSError", 2),
                                 (written["repetitions_interrupted_reason"], written["repetitions_error_type"],
                                  len(written["cases"])))
                self.assertNotIn("synthetic secret", json.dumps(written))
                self.assertIn("repetition_summary", written)

    def test_repeat_summary_failure_is_recorded_without_losing_the_main_pass_report(self) -> None:
        scenario = _Scenario([{}, {}])
        with tempfile.TemporaryDirectory() as temp, patch(
            "scripts.evaluate_live_local_llm.summarize_repetitions", side_effect=ValueError("synthetic secret")
        ):
            report = _evaluate(scenario, _specs("a"), temp, core_repeat={"case_ids": ["a"], "extra_runs": 1})
            written = json.loads((Path(temp) / "live-local-llm-report.json").read_text(encoding="utf-8"))
        self.assertEqual(("repeat_phase_error", "ValueError"),
                         (report["repetitions_interrupted_reason"], report["repetitions_error_type"]))
        self.assertNotIn("repetition_summary", report)
        self.assertEqual(1, len(report["repetitions"]))
        self.assertTrue(report["passed"])
        self.assertEqual("ValueError", written["repetitions_error_type"])
        self.assertNotIn("synthetic secret", json.dumps(written))

    def test_absent_core_repeat_leaves_the_report_without_repetition_keys(self) -> None:
        scenario = _Scenario([{}, {}])
        with tempfile.TemporaryDirectory() as temp:
            report = _evaluate(scenario, _specs("a", "b"), temp)
            names = sorted(path.name for path in Path(temp).glob("*.json"))
        self.assertEqual(2, len(scenario.case_order))
        self.assertFalse([key for key in report if key.startswith("repetition")])
        self.assertTrue(all(name.startswith(("case-", "live-local")) for name in names))

    def test_invalid_plan_is_rejected_before_any_request_or_file(self) -> None:
        specs = _specs("a", "b")
        cases = (
            ("core_repeat_must_be_object", ["a"]),
            ("core_repeat_must_be_object", "a"),
            ("core_repeat_unknown_field", {"case_ids": ["a"], "extra_runs": 1, "mode": "all"}),
            ("core_repeat_case_ids_invalid", {"extra_runs": 1}),
            ("core_repeat_case_ids_invalid", {"case_ids": [], "extra_runs": 1}),
            ("core_repeat_case_ids_invalid", {"case_ids": "a", "extra_runs": 1}),
            ("core_repeat_case_ids_invalid", {"case_ids": ["a", "a"], "extra_runs": 1}),
            ("core_repeat_case_ids_invalid", {"case_ids": ["a", ""], "extra_runs": 1}),
            ("core_repeat_case_ids_invalid", {"case_ids": ["a", 3], "extra_runs": 1}),
            ("core_repeat_case_not_in_query_spec", {"case_ids": ["a", "zzz"], "extra_runs": 1}),
            ("core_repeat_extra_runs_invalid", {"case_ids": ["a"]}),
            ("core_repeat_extra_runs_invalid", {"case_ids": ["a"], "extra_runs": 0}),
            ("core_repeat_extra_runs_invalid", {"case_ids": ["a"], "extra_runs": 5}),
            ("core_repeat_extra_runs_invalid", {"case_ids": ["a"], "extra_runs": -1}),
            ("core_repeat_extra_runs_invalid", {"case_ids": ["a"], "extra_runs": True}),
            ("core_repeat_extra_runs_invalid", {"case_ids": ["a"], "extra_runs": "2"}),
            ("core_repeat_extra_runs_invalid", {"case_ids": ["a"], "extra_runs": 2.0}),
        )
        for code, plan in cases:
            with self.subTest(plan=plan), tempfile.TemporaryDirectory() as temp, patch(
                "scripts.evaluate_live_local_llm._read_json"
            ) as request:
                with self.assertRaisesRegex(ValueError, f"^{code}$"):
                    evaluate_live_local_llm(api_endpoint="http://127.0.0.1:123", query_spec=specs,
                                            observer=object(), output_dir=Path(temp), core_repeat=plan)
                request.assert_not_called()
                self.assertEqual([], list(Path(temp).iterdir()))

    def test_extra_run_bounds_are_inclusive(self) -> None:
        for runs in (1, 4):
            self.assertEqual({"case_ids": ["a"], "extra_runs": runs},
                             validate_core_repeat({"case_ids": ["a"], "extra_runs": runs}, {"a"}))
        self.assertEqual(["a", "b"], validate_core_repeat({"case_ids": ("a", "b"), "extra_runs": 1}, {"a", "b"})["case_ids"])

    def test_existing_repeat_artifact_blocks_a_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "repeat-0001-r01-old.json").write_text("{}", encoding="utf-8")
            with patch("scripts.evaluate_live_local_llm._read_json") as request:
                with self.assertRaisesRegex(ValueError, "already_contains_evidence"):
                    evaluate_live_local_llm(api_endpoint="http://127.0.0.1:123", query_spec=_specs("a"),
                                            observer=object(), output_dir=Path(temp))
                request.assert_not_called()


class ColdWarmClassificationTests(unittest.TestCase):
    def test_threshold_is_one_second_and_strict(self) -> None:
        self.assertEqual(1_000_000_000, COLD_LOAD_DURATION_NS)
        for load, expected in ((0, "warm"), (COLD_LOAD_DURATION_NS - 1, "warm"), (COLD_LOAD_DURATION_NS, "warm"),
                               (COLD_LOAD_DURATION_NS + 1, "cold"), (30_000_000_000, "cold")):
            with self.subTest(load=load):
                self.assertEqual(expected, classify_latency([_event_with_load(load)], ANSWER_MODEL))

    def test_any_cold_completed_generation_makes_the_case_cold(self) -> None:
        events = [_event_with_load(5_000_000), _event_with_load(COLD_LOAD_DURATION_NS + 1), _event_with_load(0)]
        self.assertEqual("cold", classify_latency(events, ANSWER_MODEL))
        self.assertEqual("warm", classify_latency([_event_with_load(5_000_000), _event_with_load(0)], ANSWER_MODEL))

    def test_no_completed_generation_or_unreported_load_is_not_assumed_warm(self) -> None:
        self.assertEqual("unclassified", classify_latency([], ANSWER_MODEL))
        self.assertEqual("unclassified", classify_latency([_event_with_load(None)], ANSWER_MODEL))
        # A warm event beside an event without a load figure is still unproven.
        self.assertEqual("unclassified", classify_latency([_event_with_load(0), _event_with_load(None)], ANSWER_MODEL))
        # An incomplete generation does not count, even with a huge load duration.
        self.assertEqual("unclassified", classify_latency([_event_with_load(9_000_000_000, completed=False)], ANSWER_MODEL))
        self.assertEqual("warm", classify_latency(
            [_event_with_load(0), _event_with_load(9_000_000_000, completed=False)], ANSWER_MODEL))
        self.assertEqual("unclassified", classify_latency([{"route": "/api/tags", "load_duration_ns": 0}], ANSWER_MODEL))

    def test_invalid_load_values_are_treated_as_unreported(self) -> None:
        for bad in (-1, float("nan"), float("inf"), True, "7"):
            event = _event_with_load(0)
            event["load_duration_ns"] = bad
            with self.subTest(value=bad):
                self.assertEqual("unclassified", classify_latency([event], ANSWER_MODEL))

    def test_unknown_answer_model_is_unclassified(self) -> None:
        for model in (None, "", 7):
            with self.subTest(model=model):
                self.assertEqual("unclassified", classify_latency([_event_with_load(0)], model))

    def test_only_the_answer_model_decides_the_label(self) -> None:
        # Query model and audit model reload on every case under a short keep-alive; the answer model is resident.
        events = [_event_with_load(9_000_000_000, "query analysis", model="qwen3:1.7b"),
                  _event_with_load(5_000_000, model="qwen3:8b"),
                  _event_with_load(9_000_000_000, "audit verdict", model="qwen3:4b")]
        self.assertEqual("warm", classify_latency(events, "qwen3:8b"))
        self.assertEqual("cold", classify_latency(events, "qwen3:4b"))
        # No completed generation of the answer model: other models' loads prove nothing about it.
        self.assertEqual("unclassified", classify_latency(events[:1] + events[2:], "qwen3:8b"))

    def test_model_load_summary_sums_and_maxes_per_model_without_reading_missing_as_zero(self) -> None:
        events = [_event_with_load(2_000_000_000, "first", model="qwen3:8b"),
                  _event_with_load(3_000_000_000, "second", model="qwen3:8b"),
                  _event_with_load(None, "audit", model="qwen3:4b"),
                  _event_with_load(7_000_000_000, "ignored", completed=False, model="qwen3:1.7b")]
        self.assertEqual({
            "qwen3:4b": {"completed_generation_count": 1, "load_reported_count": 0,
                         "load_duration_ns_sum": None, "load_duration_ns_max": None},
            "qwen3:8b": {"completed_generation_count": 2, "load_reported_count": 2,
                         "load_duration_ns_sum": 5_000_000_000, "load_duration_ns_max": 3_000_000_000},
        }, model_load_summary(events))
        self.assertEqual({}, model_load_summary([]))

    def test_score_case_records_every_models_load_but_labels_by_the_answer_model(self) -> None:
        response = {**_response(), "orchestration": {**_response()["orchestration"], "answer_model": "qwen3:8b"}}
        events = [_event_with_load(9_000_000_000, "query analysis", model="qwen3:1.7b"),
                  _event_with_load(1, response["answer"], model="qwen3:8b"),
                  _event_with_load(9_000_000_000, "audit verdict", model="qwen3:4b")]
        result = score_case(_spec(), response, http_status=200, events=events, duration_ms=10)
        self.assertEqual("warm", result["latency_class"])
        info = result["latency_info"]
        self.assertEqual(("answer_model_load_duration", "qwen3:8b"), (info["label_basis"], info["answer_model"]))
        self.assertEqual(["qwen3:1.7b", "qwen3:4b", "qwen3:8b"], list(info["models"]))
        self.assertEqual(9_000_000_000, info["models"]["qwen3:4b"]["load_duration_ns_sum"])
        self.assertEqual(1, info["models"]["qwen3:8b"]["load_duration_ns_max"])
        # A cold answer model makes the case cold even when the helper models are warm.
        events[1] = _event_with_load(COLD_LOAD_DURATION_NS + 1, response["answer"], model="qwen3:8b")
        events[0] = _event_with_load(0, "query analysis", model="qwen3:1.7b")
        self.assertEqual("cold", score_case(_spec(), response, http_status=200, events=events,
                                            duration_ms=10)["latency_class"])

    def test_answer_model_falls_back_to_the_single_model_whose_output_is_the_final_answer(self) -> None:
        response = {**_response(), "orchestration": {"answer_mode": "grounded_local", "claim_audit_status": "verified",
                                                     "citation_verification_status": "verified"}}
        helper = _event_with_load(9_000_000_000, "query analysis", model="qwen3:1.7b")
        answer = _event_with_load(1, response["answer"], model="qwen3:8b")
        result = score_case(_spec(), response, http_status=200, events=[helper, answer], duration_ms=10)
        self.assertEqual(("warm", "qwen3:8b"), (result["latency_class"], result["latency_info"]["answer_model"]))
        # Two models producing the identical final text leave the answer model ambiguous.
        twin = _event_with_load(1, response["answer"], model="qwen3:4b")
        ambiguous = score_case(_spec(), response, http_status=200, events=[answer, twin], duration_ms=10)
        self.assertEqual(("unclassified", None), (ambiguous["latency_class"], ambiguous["latency_info"]["answer_model"]))
        # No event produced the final text: unknown, not guessed.
        unmatched = score_case(_spec(), response, http_status=200, events=[helper], duration_ms=10)
        self.assertEqual("unclassified", unmatched["latency_class"])

    def test_score_case_labels_each_run_and_keeps_the_answer_digest_only(self) -> None:
        cold = score_case(_spec(), _response(), http_status=200, duration_ms=10,
                          events=[_event_with_load(COLD_LOAD_DURATION_NS + 1)])
        warm = score_case(_spec(), _response(), http_status=200, duration_ms=10, events=[_event_with_load(1)])
        self.assertEqual(("cold", "warm"), (cold["latency_class"], warm["latency_class"]))
        expected = hashlib.sha256(" ".join(_response()["answer"].split()).encode("utf-8")).hexdigest()
        self.assertEqual(expected, warm["answer_sha256"])
        self.assertNotIn(_response()["answer"], json.dumps({key: value for key, value in warm.items()
                                                            if key != "generation_events"}))
        empty = score_case({"id": "x", "expected_block": True}, {}, http_status=403, events=[], duration_ms=1)
        self.assertEqual(("unclassified", None), (empty["latency_class"], empty["answer_sha256"]))

    def test_aggregate_counts_cold_warm_and_computes_warm_only_percentiles(self) -> None:
        def run(duration: float, load: int | None) -> dict:
            events = [] if load is None else [_event_with_load(load)]
            return score_case(_spec(), _response(), http_status=200, events=events, duration_ms=duration)

        cases = [run(100, 5_000_000_000), run(200, 0), run(300, 1), run(400, COLD_LOAD_DURATION_NS), run(500, None)]
        summary = summarize_cases(cases)
        self.assertEqual((1, 3, 1), (summary["cold_case_count"], summary["warm_case_count"],
                                     summary["unclassified_latency_case_count"]))
        self.assertEqual(summary["case_count"], summary["cold_case_count"] + summary["warm_case_count"]
                         + summary["unclassified_latency_case_count"])
        self.assertEqual((300, 400), (summary["warm_p50_duration_ms"], summary["warm_p95_duration_ms"]))
        # The slowest case is the unclassified one, so all-case p95 is higher than warm p95.
        self.assertEqual((300, 500), (summary["p50_duration_ms"], summary["p95_duration_ms"]))

    def test_aggregate_without_a_warm_case_has_no_warm_percentiles(self) -> None:
        summary = summarize_cases([score_case(_spec(), _response(), http_status=200, duration_ms=5,
                                              events=[_event_with_load(COLD_LOAD_DURATION_NS + 1)])])
        self.assertEqual((1, 0), (summary["cold_case_count"], summary["warm_case_count"]))
        self.assertEqual((None, None), (summary["warm_p50_duration_ms"], summary["warm_p95_duration_ms"]))

    def test_repetition_summary_reports_percentiles_over_all_core_runs_and_warm_only(self) -> None:
        # a: main run cold, then two warm repeats. Wall times here are real and tiny, so only
        # the arithmetic is asserted: all-run p95 is the largest of three, warm p95 the larger of two.
        scenario = _Scenario([{"load_ns": COLD_LOAD_DURATION_NS + 1}, {}, {}])
        with tempfile.TemporaryDirectory() as temp:
            report = _evaluate(scenario, _specs("a"), temp, core_repeat={"case_ids": ["a"], "extra_runs": 2})
        summary = report["repetition_summary"]
        self.assertEqual(["cold", "warm", "warm"], summary["cases"][0]["latency_classes"])
        self.assertEqual((1, 2), (summary["cold_run_count"], summary["warm_run_count"]))
        durations = summary["cases"][0]["duration_ms"]
        self.assertEqual(round(sorted(durations)[-1], 3), summary["p95_duration_ms"])
        self.assertEqual(round(sorted(durations[1:])[-1], 3), summary["warm_p95_duration_ms"])
        self.assertEqual((1, 0, 0), (report["aggregate"]["cold_case_count"], report["aggregate"]["warm_case_count"],
                                     report["aggregate"]["unclassified_latency_case_count"]))


class _E2eBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="core_repeat_test_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.runtime = self.root / "source-runtime"
        self.runtime.mkdir()
        (self.runtime / "manifest.json").write_text('{"approved": true}', encoding="utf-8")
        pdf = self.root / "synthetic.pdf"
        pdf.write_bytes(b"%PDF-1.7\nsynthetic test document\n")
        self.manifest = self.root / "corpus.json"
        self.manifest.write_text(json.dumps({"documents": [{
            "document_id": "synthetic-document", "snapshot_pdf": str(pdf),
            "source_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(), "human_review_complete": False}],
            "label_provenance": "synthetic"}), encoding="utf-8")
        self.query_spec = self.root / "queries.json"
        self.query_spec.write_text(json.dumps({"queries": [
            {"id": "case-a", "query": "synthetic question a", "target_document_ids": ["synthetic-document"]},
            {"id": "case-b", "query": "synthetic question b", "target_document_ids": ["synthetic-document"]}]}),
            encoding="utf-8")
        self.options = RunOptions(runtime_source=self.runtime, corpus_manifest=self.manifest,
                                  query_spec=self.query_spec, output_dir=self.root / "new-run",
                                  tenant_id="synthetic-tenant")


class CoreRepeatConfigurationTests(_E2eBase):
    def _stages(self, stack: ExitStack) -> dict:
        stack.enter_context(patch("scripts.report_metadata.capture_mcp_performance_source_state", return_value={}))
        stack.enter_context(patch("scripts.report_metadata.finalize_mcp_performance_source_state", return_value={"stable": True}))
        stack.enter_context(patch("scripts.report_metadata.current_repo_commit", return_value="synthetic"))
        mocks = {
            "probe": stack.enter_context(patch("scripts.real_regulation_corpus_probe.run_corpus_probe", return_value={
                "passed": True, "results": [{"source_document_id": "synthetic-document", "passed": True}]})),
            "retrieval": stack.enter_context(patch("scripts.evaluate_mcp_retrieval_quality.evaluate_mcp_retrieval_quality",
                                                   return_value={"passed": True})),
            "mcp": stack.enter_context(patch("scripts.real_regulation_mcp_probe.run_mcp_probe", return_value={"passed": True})),
            "live": stack.enter_context(patch("scripts.evaluate_live_local_llm.evaluate_live_local_llm",
                                              return_value={"passed": True})),
        }
        stack.enter_context(patch("scripts.evaluate_live_local_llm.LocalOllamaObserver"))
        stack.enter_context(patch("scripts.run_real_regulation_e2e.managed_local_api",
                                  return_value=nullcontext(("http://127.0.0.1:1", {}))))
        return mocks

    def _run(self, options: RunOptions):
        with ExitStack() as stack:
            mocks = self._stages(stack)
            report = run_real_regulation_e2e(options)
        return report, mocks["live"]

    def test_absent_key_keeps_configuration_and_validation_unchanged(self) -> None:
        self.assertNotIn("core_repeat", validate_options(self.options))
        report, live = self._run(self.options)
        self.assertEqual({"model", "api_role", "request_defaults", "security_levels", "mcp_top_k",
                          "enable_kordoc_table_parser", "timeout_seconds", "preprocessing_document_ids"},
                         set(report["configuration"]))
        self.assertIsNone(live.call_args.kwargs["core_repeat"])

    def test_requested_repeat_is_recorded_outside_configuration_and_passed_sorted_to_the_evaluator(self) -> None:
        plain, _ = self._run(self.options)
        self.assertNotIn("core_repeat", plain)
        plan = {"case_ids": ["case-b", "case-a"], "extra_runs": 3}
        options = replace(self.options, core_repeat=plan, output_dir=self.root / "repeated-run")
        normalized = {"case_ids": ["case-a", "case-b"], "extra_runs": 3}
        self.assertEqual(normalized, validate_options(options)["core_repeat"])
        report, live = self._run(options)
        # configuration is a baseline-comparison field; it must read exactly as without repeats.
        self.assertNotIn("core_repeat", report["configuration"])
        self.assertEqual(plain["configuration"], report["configuration"])
        self.assertEqual(normalized, report["core_repeat"])
        self.assertEqual(normalized, live.call_args.kwargs["core_repeat"])
        self.assertTrue(report["passed"])
        saved = json.loads((options.output_dir / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(normalized, saved["core_repeat"])

    def _seal_holdout_spec(self) -> tuple[str, Path]:
        cases = [{"id": f"case-{name}", "query": f"synthetic holdout question {name}",
                  "target_document_ids": ["synthetic-document"]} for name in ("a", "b")]
        self.query_spec.write_text(json.dumps({"split": "holdout", "queries": cases}), encoding="utf-8")
        sha = hashlib.sha256(self.query_spec.read_bytes()).hexdigest()
        ledger = self.root / "ledger" / "holdout.jsonl"
        seal_holdout(ledger, development_sha256="d" * 64, holdout_sha256=sha,
                     development_query_count=1, holdout_query_count=2)
        return sha, ledger

    @staticmethod
    def _ledger_events(ledger: Path) -> list[str]:
        return [json.loads(line)["event"] for line in ledger.read_text(encoding="utf-8").splitlines()]

    def test_holdout_run_with_repeats_writes_exactly_one_opened_record(self) -> None:
        sha, ledger = self._seal_holdout_spec()
        plan = {"case_ids": ["case-a", "case-b"], "extra_runs": 4}
        options = replace(self.options, holdout_ledger=ledger, core_repeat=plan)
        # --check-only validates the plan and the gate without writing the ledger.
        before = ledger.read_bytes()
        validate_options(options)
        self.assertEqual(before, ledger.read_bytes())

        report, live = self._run(options)
        self.assertEqual(["sealed", "opened"], self._ledger_events(ledger))
        self.assertEqual(1, live.call_count)
        self.assertEqual(plan, live.call_args.kwargs["core_repeat"])
        record = report["holdout_ledger_record"]
        self.assertEqual(("opened", sha), (record["event"], record["query_spec_sha256"]))
        # The repeats belong to this one opening; they do not buy or need another.
        after = ledger.read_bytes()
        with self.assertRaisesRegex(EvidenceError, "holdout_already_opened"):
            self._run(replace(options, output_dir=self.root / "second-run"))
        self.assertEqual(after, ledger.read_bytes())

    def test_invalid_plan_on_a_holdout_run_leaves_the_ledger_and_stages_untouched(self) -> None:
        _, ledger = self._seal_holdout_spec()
        before = ledger.read_bytes()
        for plan in ({"case_ids": ["case-a", "missing"], "extra_runs": 2},
                     {"case_ids": ["case-a"], "extra_runs": 9},
                     {"case_ids": ["case-a"]}):
            options = replace(self.options, holdout_ledger=ledger, core_repeat=plan)
            with self.subTest(plan=plan), ExitStack() as stack:
                mocks = self._stages(stack)
                with self.assertRaisesRegex(EvidenceError, "^core_repeat_"):
                    run_real_regulation_e2e(options)
                for mock in mocks.values():
                    mock.assert_not_called()
            self.assertEqual(before, ledger.read_bytes())
            self.assertEqual(["sealed"], self._ledger_events(ledger))
            self.assertFalse(options.output_dir.exists())

    def test_invalid_plan_is_refused_with_a_fixed_code_before_any_output(self) -> None:
        cases = (
            ("core_repeat_must_be_object", ["case-a"]),
            ("core_repeat_unknown_field", {"case_ids": ["case-a"], "extra_runs": 1, "x": 1}),
            ("core_repeat_case_ids_invalid", {"case_ids": [], "extra_runs": 1}),
            ("core_repeat_case_ids_invalid", {"case_ids": ["case-a", "case-a"], "extra_runs": 1}),
            ("core_repeat_case_not_in_query_spec", {"case_ids": ["missing"], "extra_runs": 1}),
            ("core_repeat_extra_runs_invalid", {"case_ids": ["case-a"], "extra_runs": 0}),
            ("core_repeat_extra_runs_invalid", {"case_ids": ["case-a"], "extra_runs": 5}),
            ("core_repeat_extra_runs_invalid", {"case_ids": ["case-a"], "extra_runs": True}),
            ("core_repeat_extra_runs_invalid", {"case_ids": ["case-a"]}),
        )
        for code, plan in cases:
            with self.subTest(plan=plan):
                with self.assertRaises(EvidenceError) as context:
                    validate_options(replace(self.options, core_repeat=plan))
                self.assertEqual(code, str(context.exception))
                self.assertFalse(self.options.output_dir.exists())

    def test_bounds_one_and_four_extra_runs_are_accepted(self) -> None:
        for runs in (1, 4):
            plan = {"case_ids": ["case-a", "case-b"], "extra_runs": runs}
            self.assertEqual(plan, validate_options(replace(self.options, core_repeat=plan))["core_repeat"])

    def test_config_loader_passes_the_object_through_and_rejects_other_types(self) -> None:
        base = {"runtime_source": str(self.runtime), "corpus_manifest": str(self.manifest),
                "query_spec": str(self.query_spec), "tenant_id": "synthetic-tenant"}
        self.assertIsNone(options_from_config(base, self.root / "out").core_repeat)
        self.assertIsNone(options_from_config({**base, "core_repeat": None}, self.root / "out").core_repeat)
        plan = {"case_ids": ["case-a"], "extra_runs": 2}
        self.assertEqual(plan, options_from_config({**base, "core_repeat": plan}, self.root / "out").core_repeat)
        for bad in ("case-a", ["case-a"], 3, True):
            with self.subTest(value=bad), self.assertRaisesRegex(EvidenceError, "^core_repeat_must_be_object$"):
                options_from_config({**base, "core_repeat": bad}, self.root / "out")


def _latency_report(spec, *, answerable: int = 20) -> dict:
    """KPI fixture whose answerable cases take (class, duration_ms[, http_status]) from spec(index).

    Abstention cases are unclassified. A third element marks a request that was attempted but
    did not complete (timeout or 5xx): failed, no answer, never warm."""
    report = _report(answerable=answerable)
    for case in _cases(report):
        if case["expected_abstain"]:
            case["latency_class"] = "unclassified"
            continue
        label, duration, *failure = spec(int(case["id"].rsplit("-", 1)[1]))
        case["latency_class"], case["duration_ms"] = label, float(duration)
        if failure:
            case.update(http_status=failure[0], status="failed", answer_present=False, final_model_answer=False)
    return report


def _warm_on(indexes, warm_ms, other=("cold", 5000)):
    return lambda index: ("warm", warm_ms(index)) if index in indexes else other


def _cold_tail_pair(*, candidate_scale: float = 1.05) -> tuple[dict, dict]:
    """Three slow cold cases and 17 warm cases in both reports; the cold tail is the all-case p95."""
    baseline = _latency_report(lambda i: ("cold", 5000) if i < 3 else ("warm", 100))
    candidate = _latency_report(lambda i: ("cold", 20000) if i < 3 else ("warm", 100 * candidate_scale))
    return candidate, baseline


class WarmOnlyLatencyRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_policy()

    def evaluate(self, candidate: dict, baseline: dict) -> dict:
        return evaluate_kpi_guardrails(candidate, self.policy, baseline=baseline)

    def latency(self, candidate: dict, baseline: dict) -> dict:
        return _row(self.evaluate(candidate, baseline), "regressions", "llm_p95_duration_ratio")

    def test_value_is_the_larger_ratio_and_both_are_reported(self) -> None:
        candidate, baseline = _cold_tail_pair()
        row = self.latency(candidate, baseline)
        self.assertEqual((4.0, "all_cases", "advisory_exceeded"), (row["value"], row["basis"], row["status"]))
        self.assertEqual(4.0, row["all_cases"]["ratio"])
        warm = row["warm_only"]
        self.assertEqual((True, 17, 10), (warm["evaluated"], warm["paired_case_count"], warm["min_paired_case_count"]))
        self.assertEqual((1.05, 100.0, 105.0), (warm["ratio"], warm["baseline_p95_ms"], warm["candidate_p95_ms"]))
        self.assertEqual((28, 28), (row["all_cases"]["baseline_sample_count"], row["all_cases"]["candidate_sample_count"]))

    def test_warm_regression_behind_an_unchanged_cold_tail_is_reported_and_stays_advisory(self) -> None:
        baseline = _latency_report(_warm_on(range(8, 20), lambda i: 100))
        candidate = _latency_report(_warm_on(range(8, 20), lambda i: 200))
        result = self.evaluate(candidate, baseline)
        row = _row(result, "regressions", "llm_p95_duration_ratio")
        self.assertEqual(1.0, row["all_cases"]["ratio"])
        self.assertEqual(("warm_only", 2.0, "advisory_exceeded"), (row["basis"], row["value"], row["status"]))
        self.assertEqual((100.0, 200.0, 12, 12), (row["baseline_p95_ms"], row["candidate_p95_ms"],
                                                  row["baseline_sample_count"], row["candidate_sample_count"]))
        self.assertFalse(row["blocking"])
        self.assertEqual(("passed", True, True), (result["verdict"], result["passed"], result["regression_passed"]))

    def test_slow_failed_requests_cannot_hide_behind_a_smaller_warm_sample(self) -> None:
        # Reviewer's case: 17 warm cases against 7 warm plus 10 slow 503s. The old rule compared
        # the 7 warm cases against the 17 and reported a ratio of 1.0.
        baseline = _latency_report(lambda i: ("warm", 100), answerable=17)
        candidate = _latency_report(lambda i: ("warm", 100) if i < 7 else ("unclassified", 5000, 503), answerable=17)
        result = self.evaluate(candidate, baseline)
        row = _row(result, "regressions", "llm_p95_duration_ratio")
        self.assertEqual(("all_cases", 50.0, "advisory_exceeded"), (row["basis"], row["value"], row["status"]))
        self.assertEqual((False, 7), (row["warm_only"]["evaluated"], row["warm_only"]["paired_case_count"]))
        self.assertIsNone(row["warm_only"]["ratio"])
        # The failed requests stay in the all-case sample: 17 answerable plus 8 abstention cases.
        self.assertEqual(25, row["all_cases"]["candidate_sample_count"])
        self.assertEqual(10, _row(result, "regressions", "llm_new_case_failures")["value"])
        # Advisory: it is reported, and the regression group does not fail on it.
        self.assertFalse(row["blocking"])
        self.assertIs(True, result["regression_passed"])

    def test_enough_paired_warm_cases_do_not_hide_failed_ones(self) -> None:
        baseline = _latency_report(lambda i: ("warm", 100))
        candidate = _latency_report(lambda i: ("warm", 100) if i < 10 else ("unclassified", 5000, 503))
        row = self.latency(candidate, baseline)
        self.assertEqual((True, 10, 1.0), (row["warm_only"]["evaluated"], row["warm_only"]["paired_case_count"],
                                           row["warm_only"]["ratio"]))
        self.assertEqual(("all_cases", 50.0), (row["basis"], row["value"]))

    def test_a_tiny_warm_sample_is_not_trusted(self) -> None:
        # Three warm cases doubled in time, everything else cold and unchanged.
        baseline = _latency_report(_warm_on(range(3), lambda i: 100))
        candidate = _latency_report(_warm_on(range(3), lambda i: 200))
        row = self.latency(candidate, baseline)
        self.assertEqual((False, 3), (row["warm_only"]["evaluated"], row["warm_only"]["paired_case_count"]))
        self.assertEqual(("all_cases", 1.0, "passed"), (row["basis"], row["value"], row["status"]))
        self.assertEqual((28, 28), (row["baseline_sample_count"], row["candidate_sample_count"]))

    def test_warm_cases_are_paired_by_id_with_a_minimum_of_ten(self) -> None:
        cases = (
            # (name, baseline warm indexes, candidate warm indexes, expected paired, evaluated)
            ("disjoint_but_large", range(0, 12), range(8, 20), 4, False),
            ("nine_paired", range(0, 12), range(3, 15), 9, False),
            ("ten_paired", range(0, 12), range(2, 14), 10, True),
            ("all_paired", range(0, 20), range(0, 20), 20, True),
        )
        for name, old_warm, new_warm, paired, evaluated in cases:
            with self.subTest(name):
                baseline = _latency_report(_warm_on(set(old_warm), lambda i: 100))
                # Candidate-only warm cases are slower, so including them would move the warm p95.
                candidate = _latency_report(_warm_on(set(new_warm), lambda i: 100 if i in old_warm else 150))
                warm = self.latency(candidate, baseline)["warm_only"]
                self.assertEqual((paired, evaluated), (warm["paired_case_count"], warm["evaluated"]))
                if evaluated:
                    self.assertEqual((1.0, paired, paired), (warm["ratio"], warm["baseline_sample_count"],
                                                             warm["candidate_sample_count"]))
                else:
                    self.assertIsNone(warm["ratio"])

    def test_fallback_to_all_cases_when_labels_are_missing_partial_unknown_or_ambiguous(self) -> None:
        def unlabeled(report: dict) -> None:
            for case in _cases(report):
                case.pop("latency_class")

        def partly_labeled(report: dict) -> None:
            _cases(report)[5].pop("latency_class")

        def no_warm(report: dict) -> None:
            for case in _cases(report):
                if case.get("latency_class") == "warm":
                    case["latency_class"] = "unclassified"

        def unknown_label(report: dict) -> None:
            _cases(report)[4]["latency_class"] = "hot"

        def duplicate_ids(report: dict) -> None:
            _cases(report)[5]["id"] = _cases(report)[4]["id"]

        mutations = {"unlabeled": unlabeled, "partly_labeled": partly_labeled, "no_warm": no_warm,
                     "unknown_label": unknown_label, "duplicate_ids": duplicate_ids}
        for name, mutate in mutations.items():
            # A baseline with duplicate ids fails its own coverage and is not comparable at all.
            for side in (("candidate",) if name == "duplicate_ids" else ("baseline", "candidate")):
                with self.subTest(name, side=side):
                    candidate, baseline = _cold_tail_pair()
                    mutate(candidate if side == "candidate" else baseline)
                    row = self.latency(candidate, baseline)
                    self.assertEqual("all_cases", row["basis"])
                    self.assertFalse(row["warm_only"]["evaluated"])
                    self.assertEqual((28, 28), (row["baseline_sample_count"], row["candidate_sample_count"]))

    @staticmethod
    def _uniform_duration(report: dict, duration_ms: float) -> dict:
        for case in _cases(report):
            case["duration_ms"] = duration_ms
        return report

    def test_unrounded_ratio_is_compared_with_the_policy_maximum(self) -> None:
        maximum = self.policy["regressions"]["llm_p95_duration_ratio"]["maximum"]
        self.assertEqual(1.10, maximum)
        baseline = self._uniform_duration(_report(), 1_000_000.0)
        cases = (
            # (candidate p95 ms, judged exact ratio, shown ratio, status)
            (1_100_000.4, "1.100000400000", 1.1, "advisory_exceeded"),
            (1_100_000.0, "1.100000000000", 1.1, "passed"),
            (1_099_999.9, "1.099999900000", 1.1, "passed"),
            (1_100_000.001, "1.100000001000", 1.1, "advisory_exceeded"),
        )
        for candidate_ms, exact, shown, status in cases:
            with self.subTest(candidate_ms=candidate_ms):
                candidate = self._uniform_duration(_report(), candidate_ms)
                result = self.evaluate(candidate, baseline)
                row = _row(result, "regressions", "llm_p95_duration_ratio")
                self.assertEqual((status, shown, exact, "all_cases"),
                                 (row["status"], row["value"], row["value_exact"], row["basis"]))
                # The advisory exceedance never changes the verdict or the regression group.
                self.assertEqual(("passed", True), (result["verdict"], result["regression_passed"]))

    def test_unrounded_ratio_also_decides_the_warm_only_comparison(self) -> None:
        # A cold tail identical in both reports keeps the all-case ratio at 1.0.
        tail = ("cold", 9_000_000)
        baseline = _latency_report(_warm_on(range(8, 20), lambda i: 1_000_000, tail))
        candidate = _latency_report(_warm_on(range(8, 20), lambda i: 1_100_000.4, tail))
        row = self.latency(candidate, baseline)
        self.assertEqual(1.0, row["all_cases"]["ratio"])
        self.assertEqual(("warm_only", 1.1, "1.100000400000", "advisory_exceeded"),
                         (row["basis"], row["value"], row["value_exact"], row["status"]))
        self.assertEqual(1.1, row["warm_only"]["ratio"])

    def test_missing_ratio_has_no_exact_value(self) -> None:
        baseline = self._uniform_duration(_report(), 0.0)
        row = self.latency(_report(), baseline)
        self.assertEqual(("unavailable", None, None), (row["status"], row["value"], row["value_exact"]))

    def test_fixture_reports_from_before_the_labels_keep_the_previous_all_case_result(self) -> None:
        candidate, baseline = _report(), _report()
        for report in (candidate, baseline):
            for case in _cases(report):
                case.pop("latency_class", None)
        for case in _cases(candidate):
            case["duration_ms"] = case["duration_ms"] * 2
        row = self.latency(candidate, baseline)
        self.assertEqual(("all_cases", 2.0, "advisory_exceeded"), (row["basis"], row["value"], row["status"]))
        self.assertFalse(row["warm_only"]["evaluated"])

    def test_without_a_baseline_the_row_is_not_evaluated_and_has_no_basis(self) -> None:
        row = _row(evaluate_kpi_guardrails(_report(), self.policy), "regressions", "llm_p95_duration_ratio")
        self.assertEqual("not_evaluated", row["status"])
        self.assertNotIn("basis", row)


def _summary_row(case_id: str, statuses: list[str], digests: int, *, complete: bool = True,
                 without_answer: int = 0) -> dict:
    return {"id": case_id, "run_count": len(statuses), "expected_run_count": 3, "complete": complete,
            "statuses": statuses, "all_passed": bool(statuses) and set(statuses) == {"passed"},
            "distinct_answer_digests": digests, "runs_without_answer": without_answer,
            "latency_classes": ["warm"] * len(statuses), "duration_ms": [100.0] * len(statuses)}


def _with_summary(report: dict, rows: list[dict], **extra) -> dict:
    result = report["stages"]["live_llm"]["result"]
    result["repetition_summary"] = {
        "schema_version": 1, "case_ids": [row["id"] for row in rows], "planned_extra_runs": 2,
        "core_run_count": sum(row["run_count"] for row in rows), "expected_core_run_count": 3 * len(rows),
        "p50_duration_ms": 100.0, "p95_duration_ms": 120.0, "cold_run_count": 1, "warm_run_count": 8,
        "warm_p95_duration_ms": 110.0, "cases": rows}
    result.update(extra)
    return report


class StabilityDiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_policy()

    def evaluate(self, report: dict, **kwargs) -> dict:
        return evaluate_kpi_guardrails(report, self.policy, **kwargs)

    @staticmethod
    def judged(result: dict) -> dict:
        return {key: value for key, value in result.items() if key not in {"generated_at", "stability"}}

    def test_block_is_absent_when_the_run_requested_no_repeats(self) -> None:
        self.assertNotIn("stability", self.evaluate(_report()))
        self.assertIsNone(build_stability(_report()))

    def test_disagreeing_incomplete_and_failed_core_cases_are_listed(self) -> None:
        rows = [_summary_row("answer-00", ["passed"] * 3, 1),
                _summary_row("answer-01", ["passed", "failed", "passed"], 2),
                _summary_row("answer-02", ["passed", "passed"], 1, complete=False),
                _summary_row("answer-03", ["passed"] * 3, 2)]
        report = _with_summary(_report(), rows, repetitions_interrupted_reason="consecutive_server_failures")
        stability = self.evaluate(report)["stability"]
        self.assertEqual(("ok", True), (stability["status"], stability["diagnostic_only"]))
        self.assertEqual(["answer-01"], stability["status_disagreement_case_ids"])
        self.assertEqual(["answer-01", "answer-03"], stability["answer_disagreement_case_ids"])
        self.assertEqual(["answer-01", "answer-03"], stability["unstable_case_ids"])
        self.assertEqual(["answer-02"], stability["incomplete_case_ids"])
        self.assertEqual(["answer-01"], stability["not_all_passed_case_ids"])
        self.assertFalse(stability["all_stable"])
        self.assertEqual("consecutive_server_failures", stability["repetitions_interrupted_reason"])
        self.assertEqual((4, 11, 12), (stability["core_case_count"], stability["core_run_count"],
                                       stability["expected_core_run_count"]))
        self.assertEqual((100.0, 120.0, 110.0), (stability["p50_duration_ms"], stability["p95_duration_ms"],
                                                 stability["warm_p95_duration_ms"]))
        self.assertEqual({"answer-01", "answer-03"}, {row["id"] for row in stability["unstable_cases"]})

    def test_runs_that_answered_only_sometimes_are_answer_disagreement(self) -> None:
        rows = [
            # Current evaluator output: no answer counts as a distinct value.
            _summary_row("answer-00", ["failed"] * 3, 2, without_answer=2),
            # Summary written when only answers were counted: one digest, two runs without an answer.
            _summary_row("answer-01", ["failed"] * 3, 1, without_answer=2),
            # Not a disagreement: every run answered the same, or no run answered.
            _summary_row("answer-02", ["failed"] * 3, 1, without_answer=0),
            _summary_row("answer-03", ["failed"] * 3, 1, without_answer=3),
        ]
        stability = self.evaluate(_with_summary(_report(), rows))["stability"]
        self.assertEqual(["answer-00", "answer-01"], stability["answer_disagreement_case_ids"])
        self.assertEqual([], stability["status_disagreement_case_ids"])
        self.assertEqual(["answer-00", "answer-01"], stability["unstable_case_ids"])
        self.assertFalse(stability["all_stable"])
        self.assertEqual({"answer-00": 2, "answer-01": 2},
                         {row["id"]: row["runs_without_answer"] for row in stability["unstable_cases"]})

    def test_evaluator_output_with_one_answered_run_is_reported_unstable(self) -> None:
        bad = {**_response(), "answer": "Submit within thirty days. [E1]"}
        unavailable = {"status": 503, "response": {}}
        scenario = _Scenario([{"response": bad}, unavailable, unavailable])
        with tempfile.TemporaryDirectory() as temp:
            evaluation = _evaluate(scenario, _specs("answer-00"), temp,
                                   core_repeat={"case_ids": ["answer-00"], "extra_runs": 2})
        report = _report()
        report["stages"]["live_llm"]["result"].update(repetition_summary=evaluation["repetition_summary"])
        stability = self.evaluate(report)["stability"]
        self.assertEqual(["answer-00"], stability["answer_disagreement_case_ids"])
        self.assertEqual([], stability["status_disagreement_case_ids"])
        self.assertFalse(stability["all_stable"])

    def test_agreeing_complete_runs_are_stable(self) -> None:
        report = _with_summary(_report(), [_summary_row("answer-00", ["passed"] * 3, 1),
                                           _summary_row("answer-01", ["failed"] * 3, 1)])
        stability = self.evaluate(report)["stability"]
        self.assertTrue(stability["all_stable"])
        self.assertEqual([], stability["unstable_case_ids"])
        # Stable failure is still not a pass; that is listed apart from instability.
        self.assertEqual(["answer-01"], stability["not_all_passed_case_ids"])

    def test_diagnostic_never_changes_verdict_rows_or_exit_codes(self) -> None:
        baseline = _report()
        plain = self.evaluate(_report(), baseline=baseline)
        unstable_rows = [_summary_row("answer-01", ["passed", "failed", "passed"], 3, complete=False)]
        with_block = self.evaluate(_with_summary(_report(), unstable_rows), baseline=baseline)
        self.assertIn("stability", with_block)
        self.assertEqual(self.judged(plain), self.judged(with_block))
        self.assertEqual("passed", with_block["verdict"])

    def test_malformed_summary_and_internal_errors_are_contained(self) -> None:
        for cases in ("nope", [1, 2], None):
            with self.subTest(cases=cases):
                report = _report()
                report["stages"]["live_llm"]["result"]["repetition_summary"] = {"cases": cases}
                result = self.evaluate(report)
                self.assertEqual("malformed", result["stability"]["status"])
                self.assertEqual("passed", result["verdict"])
        odd = _report()
        odd["stages"]["live_llm"]["result"]["repetition_summary"] = {"cases": [
            {"id": None, "statuses": 5, "distinct_answer_digests": "x", "complete": "yes"}]}
        self.assertEqual("ok", self.evaluate(odd)["stability"]["status"])
        with patch("scripts.check_real_regulation_kpi_guardrails._build_stability", side_effect=RuntimeError("x")):
            result = self.evaluate(_with_summary(_report(), [_summary_row("answer-00", ["passed"] * 3, 1)]))
        self.assertEqual(("error", "RuntimeError", "passed"),
                         (result["stability"]["status"], result["stability"]["reason"], result["verdict"]))

    def test_evaluator_summary_feeds_the_checker_with_matching_field_names(self) -> None:
        steps = [{}, {}, {"response": _variant_response("Different wording.")}, {}]
        scenario = _Scenario(steps)
        with tempfile.TemporaryDirectory() as temp:
            evaluation = _evaluate(scenario, _specs("answer-00", "answer-01"), temp,
                                   core_repeat={"case_ids": ["answer-00", "answer-01"], "extra_runs": 1})
        report = _report()
        report["stages"]["live_llm"]["result"].update(
            repetition_summary=evaluation["repetition_summary"],
            repetitions_interrupted_reason=evaluation.get("repetitions_interrupted_reason"))
        stability = self.evaluate(report)["stability"]
        self.assertEqual(["answer-00"], stability["answer_disagreement_case_ids"])
        self.assertEqual([], stability["status_disagreement_case_ids"])
        self.assertEqual([], stability["incomplete_case_ids"])
        self.assertEqual(4, stability["core_run_count"])

    def test_failing_repeat_does_not_change_coverage_breakdown_regressions_or_verdict(self) -> None:
        bad = {**_response(), "answer": "Submit within thirty days. [E1]"}
        scenario = _Scenario([{}, {}, {"response": bad}, {}])
        with tempfile.TemporaryDirectory() as temp:
            evaluation = _evaluate(scenario, _specs("answer-00", "answer-01"), temp,
                                   core_repeat={"case_ids": ["answer-00", "answer-01"], "extra_runs": 1})
        self.assertEqual("failed", evaluation["repetitions"][0]["status"])
        baseline, plain, repeated = _report(), _report(), _report()
        repeated["stages"]["live_llm"]["result"].update(
            repetitions=evaluation["repetitions"], repetition_summary=evaluation["repetition_summary"],
            repetition_plan=evaluation["repetition_plan"],
            repetitions_interrupted_reason="repeat_phase_error", repetitions_error_type="OSError")
        without = self.evaluate(plain, baseline=baseline)
        with_repeats = self.evaluate(repeated, baseline=baseline)
        self.assertEqual(self.judged(without), self.judged(with_repeats))
        self.assertEqual(without["failure_breakdown"], with_repeats["failure_breakdown"])
        self.assertEqual(0, with_repeats["failure_breakdown"]["failed_case_count"])
        self.assertEqual("passed", _row(with_repeats, "hard", "coverage")["status"])
        self.assertEqual("passed", with_repeats["verdict"])
        self.assertEqual(without["regressions"], with_repeats["regressions"])
        stability = with_repeats["stability"]
        self.assertEqual(["answer-00"], stability["not_all_passed_case_ids"])
        self.assertEqual(("repeat_phase_error", "OSError"), (stability["repetitions_interrupted_reason"],
                                                             stability["repetitions_error_type"]))

    def test_requesting_repeats_does_not_make_a_run_incomparable_with_a_baseline_without_them(self) -> None:
        candidate = _with_summary(_report(), [_summary_row("answer-00", ["passed"] * 3, 1)])
        candidate["core_repeat"] = {"case_ids": ["answer-00"], "extra_runs": 2}
        result = self.evaluate(candidate, baseline=_report())
        self.assertTrue(result["comparability"]["comparable"])
        self.assertEqual("passed", result["verdict"])
        # Only the main pass is compared, so repeats cannot enter the latency or failure rows.
        self.assertEqual(28, _row(result, "regressions", "llm_p95_duration_ratio")["all_cases"]["candidate_sample_count"])

    def test_cli_prints_counts_only_when_repeats_exist_and_exit_code_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            plain = root / "plain.json"
            plain.write_text(json.dumps(_report()), encoding="utf-8")
            repeated = root / "repeated.json"
            repeated.write_text(json.dumps(_with_summary(
                _report(), [_summary_row("answer-01", ["passed", "failed", "passed"], 2)])), encoding="utf-8")

            def run(*args: str) -> tuple[int, dict]:
                stream = io.StringIO()
                with redirect_stdout(stream):
                    code = kpi_main(list(args))
                return code, json.loads(stream.getvalue())

            code, summary = run("--report", str(plain), "--output", str(root / "plain-out.json"))
            self.assertEqual((0, "passed"), (code, summary["verdict"]))
            self.assertNotIn("stability", summary)
            self.assertNotIn("stability", json.loads((root / "plain-out.json").read_text(encoding="utf-8")))

            code, summary = run("--report", str(repeated), "--output", str(root / "repeated-out.json"))
            self.assertEqual((0, "passed"), (code, summary["verdict"]))
            self.assertEqual({"all_stable": False, "unstable_case_count": 1, "incomplete_case_count": 0},
                             summary["stability"])
            written = json.loads((root / "repeated-out.json").read_text(encoding="utf-8"))
            self.assertEqual(["answer-01"], written["stability"]["unstable_case_ids"])


if __name__ == "__main__":
    unittest.main()
