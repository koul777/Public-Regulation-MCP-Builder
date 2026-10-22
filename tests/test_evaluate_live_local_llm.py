from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app.rag.extractive_answer import NO_EVIDENCE_ANSWER
from scripts.evaluate_live_local_llm import (
    LocalOllamaObserver,
    _query_specs,
    _read_json,
    _request_payload,
    _write_private_json,
    check_citations,
    evaluate_live_local_llm,
    evaluate_mcp_fetch_bridge,
    generation_observation,
    literal_fact_matches,
    main,
    mcp_fetch_evidence,
    private_output_directory,
    score_case,
    summarize_cases,
    validate_loopback_endpoint,
)


def _spec() -> dict:
    return {
        "id": "synthetic-deadline",
        "query": "What is the synthetic deadline?",
        "required_facts": [["seven days", "7 days"]],
        "forbidden_facts": ["thirty days"],
        "allowed_citations": [{"document_id": "synthetic-document", "chunk_id": "synthetic-chunk",
                               "approval_id": "synthetic-approval", "approved_content_hash": "a" * 64,
                               "article_no": "Article 1", "source_page_start": 2, "source_page_end": 2,
                               "source_text": "Applications must be submitted within seven days."}],
    }


def _response() -> dict:
    return {
        "answer": "Submit within seven days. [E1]",
        "citations": [{"context_id": "E1", "document_id": "synthetic-document",
                       "evidence_ids": ["synthetic-chunk"], "approval_ids": ["synthetic-approval"],
                       "content_hashes": ["a" * 64], "article_no": "Article 1",
                       "source_page_start": 2, "source_page_end": 2,
                       "support_quote": "within seven days"}],
        "orchestration": {"answer_model": "synthetic-model", "answer_mode": "grounded_local",
                          "claim_audit_status": "verified", "citation_verification_status": "verified"},
    }


def _event(answer: str | None = None) -> dict:
    request = {"model": "synthetic-model", "prompt": "synthetic-private-prompt", "stream": False,
               "options": {"temperature": 0, "seed": 0, "num_predict": 200}}
    payload = {"model": "synthetic-model", "done": True, "done_reason": "stop", "eval_count": 10,
               "response": json.dumps({"answer": answer or _response()["answer"], "claims": []})}
    return {"route": "/api/generate", **generation_observation(request, payload, status=200, duration_ms=100)}


def _score(spec: dict | None = None, response: dict | None = None, events: list | None = None,
           status: int = 200) -> dict:
    return score_case(spec if spec is not None else _spec(), response if response is not None else _response(),
                      http_status=status, events=events if events is not None else [_event()], duration_ms=123)


class LiveLocalLlmScoringTests(unittest.TestCase):
    def test_numeric_fact_anchors_do_not_match_larger_or_decimal_numbers(self) -> None:
        for anchor, answer in (("10일", "110일"), ("1년", "11년"), ("제1조", "제11조"),
                               ("10", "10.5"), ("10", "1,010"), ("10.5", "110.5")):
            with self.subTest(anchor=anchor, answer=answer):
                self.assertFalse(literal_fact_matches(anchor, answer))
        for anchor, answer in (("10일", "기한은 10일이다."), ("1년", "1년 이내"),
                               ("1,000원", "총 1,000원"), ("[E1]", "내용 [E1]"), ("10", "10.")):
            with self.subTest(anchor=anchor, answer=answer):
                self.assertTrue(literal_fact_matches(anchor, answer))

    def test_required_and_forbidden_facts_share_numeric_boundary_rule(self) -> None:
        spec = {**_spec(), "required_facts": ["10일"], "forbidden_facts": ["1일"]}
        response = {**_response(), "answer": "110일 [E1]"}
        result = _score(spec=spec, response=response, events=[_event(response["answer"])])
        self.assertEqual(0, result["correctness"]["matched_count"])
        self.assertEqual([], result["correctness"]["forbidden_match_indices"])

    def test_observed_final_model_answer_with_independent_facts_and_citation_passes(self) -> None:
        result = _score()
        self.assertEqual("passed", result["status"])
        self.assertTrue(result["final_model_answer"])
        self.assertEqual("verified", result["citation_checks"]["checks"][0]["support_quote_status"])

    def test_model_name_without_observed_generation_is_not_success(self) -> None:
        result = _score(events=[])
        self.assertFalse(result["actual_model_called"])
        self.assertFalse(result["final_model_answer"])
        self.assertIn("final_model_generation_not_verified", result["errors"])

    def test_generation_attempt_or_empty_response_does_not_prove_model_completed(self) -> None:
        event = _event()
        event["generation_completed"] = False
        result = _score(events=[event])
        self.assertEqual(1, result["model_request_attempt_count"])
        self.assertFalse(result["actual_model_called"])

    def test_claim_audit_reason_is_recorded_next_to_the_status_without_changing_the_verdict(self) -> None:
        self.assertIsNone(_score()["claim_audit_reason"])
        response = _response()
        response["orchestration"].update(claim_audit_status="rejected",
                                          claim_audit_reason="answer_number_not_in_evidence")
        result = _score(response=response)
        self.assertEqual("rejected", result["claim_audit_status"])
        self.assertEqual("answer_number_not_in_evidence", result["claim_audit_reason"])
        self.assertIn("claim_audit_not_accepted", result["errors"])
        response["orchestration"].update(claim_audit_status="verified")
        self.assertEqual("passed", _score(response=response)["status"])

    def test_fallback_after_real_model_call_is_separately_counted(self) -> None:
        response = _response()
        response["orchestration"].update(answer_mode="grounded_extractive", answer_model=None,
                                          attempted_answer_model="synthetic-model", claim_audit_status="fallback_from_rejected")
        result = _score(response=response)
        self.assertTrue(result["actual_model_called"])
        self.assertTrue(result["fallback"])
        self.assertFalse(result["final_model_answer"])
        self.assertEqual("failed", result["status"])

    def test_observed_auditor_alone_does_not_prove_final_answer_generation(self) -> None:
        event = _event()
        event["model"] = "synthetic-auditor"
        result = _score(events=[event])
        self.assertTrue(result["actual_model_called"])
        self.assertFalse(result["final_model_answer"])

    def test_answer_must_match_observed_model_text(self) -> None:
        result = _score(events=[_event("Different synthetic answer")])
        self.assertFalse(result["answer_matches_observed_generation"])
        self.assertFalse(result["final_model_answer"])

    def test_missing_facts_are_unverified_even_with_real_generation(self) -> None:
        spec = _spec()
        spec["required_facts"] = []
        result = _score(spec=spec)
        self.assertEqual("unverified", result["correctness"]["status"])
        self.assertEqual("failed", result["status"])

    def test_forbidden_fact_fails_even_if_required_fact_also_present(self) -> None:
        response = _response()
        response["answer"] += " Or thirty days."
        result = _score(response=response, events=[_event(response["answer"])])
        self.assertEqual([0], result["correctness"]["forbidden_match_indices"])
        self.assertEqual("failed", result["status"])

    def test_clean_abstention_does_not_require_model_call(self) -> None:
        result = _score(spec={"id": "negative", "expected_abstain": True},
                        response={"answer": NO_EVIDENCE_ANSWER, "citations": [],
                                  "orchestration": {"answer_mode": "abstained"}}, events=[])
        self.assertEqual("passed", result["status"])
        self.assertFalse(result["actual_model_called"])

    def test_abstention_with_citations_or_forbidden_fact_fails(self) -> None:
        result = _score(spec={"id": "negative", "expected_abstain": True}, response=_response())
        self.assertIn("expected_clean_abstention", result["errors"])

    def test_expected_block_requires_no_answer_citation_or_model_completion(self) -> None:
        spec = {"id": "scope", "expected_block": True, "expected_http_status": 403}
        self.assertEqual("passed", _score(spec=spec, response={"detail": "denied"}, events=[], status=403)["status"])
        self.assertEqual("failed", _score(spec=spec, response={"detail": "denied"}, events=[_event()], status=403)["status"])
        self.assertEqual("failed", _score(spec=spec, response={}, events=[], status=500)["status"])

    def test_http_error_is_not_reclassified_as_expected_abstention(self) -> None:
        result = _score(spec={"id": "negative", "expected_abstain": True}, response={}, events=[], status=503)
        self.assertEqual("failed", result["status"])

    def test_abstained_mode_label_cannot_hide_a_substantive_answer(self) -> None:
        result = _score(spec={"id": "negative", "expected_abstain": True},
                        response={"answer": "The invented deadline is 99 days.", "citations": [],
                                  "orchestration": {"answer_mode": "abstained"}}, events=[])
        self.assertFalse(result["abstained"])
        self.assertEqual("failed", result["status"])

    def test_abstention_cannot_expect_server_failure_even_with_canonical_text(self) -> None:
        spec = {**_spec(), "expected_abstain": True, "expected_http_status": 503}
        with self.assertRaisesRegex(ValueError, "abstention_requires_success"):
            _query_specs([spec])
        result = _score(spec=spec, response={"answer": NO_EVIDENCE_ANSWER, "citations": []},
                        events=[], status=503)
        self.assertEqual("failed", result["status"])

    def test_expected_block_rejects_incomplete_model_attempt(self) -> None:
        result = _score(spec={"id": "scope", "expected_block": True}, response={}, status=403,
                        events=[{"route": "/api/generate", "upstream_attempted": True,
                                 "generation_completed": False}])
        self.assertFalse(result["actual_model_called"])
        self.assertEqual("failed", result["status"])

    def test_summary_excludes_negative_cases_from_model_completion_denominator(self) -> None:
        positive = _score()
        negative = _score(spec={"id": "negative", "expected_abstain": True},
                          response={"answer": NO_EVIDENCE_ANSWER}, events=[])
        summary = summarize_cases([positive, negative])
        self.assertEqual(1, summary["answerable_count"])
        self.assertEqual(1.0, summary["final_model_answer_rate"])
        self.assertEqual(2, summary["passed_count"])


class LiveLocalLlmCitationTests(unittest.TestCase):
    def test_same_article_and_chunk_with_wrong_regulation_title_fails(self) -> None:
        spec = _spec()
        spec["allowed_citations"][0]["regulation_title"] = "Synthetic Working Hours Rule"
        citation = _response()["citations"][0]
        citation["regulation_title"] = "Synthetic Overseas Training Rule"
        self.assertFalse(check_citations([citation], spec)["all_verified"])
        citation["regulation_title"] = "Synthetic Working Hours Rule"
        self.assertTrue(check_citations([citation], spec)["all_verified"])
        citation["document_title"] = citation.pop("regulation_title")
        self.assertTrue(check_citations([citation], spec)["all_verified"])
        citation["regulation_title"] = "Synthetic Conflicting Rule"
        self.assertFalse(check_citations([citation], spec)["all_verified"])

    def test_title_article_match_without_approval_identity_is_not_enough(self) -> None:
        citation = _response()["citations"][0]
        citation.pop("approval_ids")
        self.assertFalse(check_citations([citation], _spec())["all_verified"])

    def test_unknown_chunk_approval_and_hash_each_fail(self) -> None:
        for key in ("evidence_ids", "approval_ids", "content_hashes"):
            with self.subTest(key=key):
                citation = _response()["citations"][0]
                citation[key].append("unknown")
                self.assertFalse(check_citations([citation], _spec())["all_verified"])

    def test_wrong_page_and_nonliteral_quote_fail(self) -> None:
        for key, value in (("source_page_start", 99), ("support_quote", "within thirty days")):
            with self.subTest(key=key):
                citation = _response()["citations"][0]
                citation[key] = value
                self.assertFalse(check_citations([citation], _spec())["all_verified"])

    def test_deterministic_identity_citation_does_not_claim_semantic_quote_verification(self) -> None:
        citation = _response()["citations"][0]
        citation.pop("support_quote")
        result = check_citations([citation], _spec())
        self.assertTrue(result["all_verified"])
        self.assertEqual("not_provided", result["checks"][0]["support_quote_status"])
        spec = {**_spec(), "require_support_quote": True}
        self.assertFalse(check_citations([citation], spec)["all_verified"])

    def test_merged_context_citation_covers_every_chunk_and_approval(self) -> None:
        spec = _spec()
        second = {**spec["allowed_citations"][0], "chunk_id": "synthetic-chunk-2",
                  "approval_id": "synthetic-approval-2", "approved_content_hash": "b" * 64,
                  "source_page_start": 3, "source_page_end": 3}
        spec["allowed_citations"].append(second)
        citation = _response()["citations"][0]
        citation.update(evidence_ids=["synthetic-chunk", "synthetic-chunk-2"],
                        approval_ids=["synthetic-approval", "synthetic-approval-2"],
                        content_hashes=["a" * 64, "b" * 64], source_page_end=3)
        self.assertTrue(check_citations([citation], spec)["all_verified"])


class LiveLocalLlmSafetyTests(unittest.TestCase):
    def test_cli_uses_auto_baseline_and_reads_token_without_serializing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            spec_path = Path(temp) / "spec.json"
            spec_path.write_text(json.dumps([_spec()]), encoding="utf-8")
            with patch.dict("os.environ", {"REG_RAG_EVAL_API_TOKEN": "synthetic-secret"}), \
                 patch("scripts.evaluate_live_local_llm.LocalOllamaObserver"), \
                 patch("scripts.evaluate_live_local_llm.evaluate_live_local_llm", return_value={"passed": True, "aggregate": {}}) as evaluate, \
                 patch("sys.stdout", new_callable=io.StringIO) as output:
                code = main(["--api-url", "http://127.0.0.1:123", "--query-spec", str(spec_path),
                             "--output-dir", temp, "--observer-port", "234", "--tenant-id", "synthetic-tenant"])
            self.assertEqual(0, code)
            self.assertEqual("auto", evaluate.call_args.kwargs["request_defaults"]["retrieval_mode"])
            self.assertEqual("Bearer synthetic-secret", evaluate.call_args.kwargs["headers"]["Authorization"])
            self.assertNotIn("synthetic-secret", output.getvalue())

    def test_loopback_url_validation_rejects_credentials_query_remote_and_file(self) -> None:
        for endpoint in ("https://remote.invalid", "file:///tmp/file", "http://127.0.0.1:123/a",
                         "http://secret@127.0.0.1:123", "http://127.0.0.1:123?secret=value",
                         "http://127.0.0.1:123#fragment", "http://127.0.0.1:0", "http://127.0.0.1:bad"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                validate_loopback_endpoint(endpoint)
        self.assertEqual("http://[::1]:123", validate_loopback_endpoint("http://[::1]:123/"))

    def test_output_inside_checkout_is_rejected_including_dotdot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "checkout"
            root.mkdir()
            for path in (root, root / "private", root / "child" / ".." / "private"):
                with self.assertRaises(ValueError):
                    private_output_directory(path, project_root=root)
            self.assertTrue(private_output_directory(Path(temp) / "private", project_root=root).is_dir())

    def test_json_writer_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp).resolve() / "private"
            directory.mkdir()
            with self.assertRaises(ValueError):
                _write_private_json(directory, "../escaped.json", {})

    def test_evidence_write_is_exclusive_and_cannot_replace_a_frozen_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp).resolve()
            first = _write_private_json(directory, "result.json", {"frozen": True})
            original = first.read_bytes()
            with self.assertRaises(FileExistsError):
                _write_private_json(directory, "result.json", {"frozen": False})
            self.assertEqual(original, first.read_bytes())

    def test_existing_case_artifact_blocks_rerun_before_any_api_or_inventory_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp).resolve()
            (directory / "case-0001-old.json").write_text("{}", encoding="utf-8")
            with patch("scripts.evaluate_live_local_llm._read_json") as request:
                with self.assertRaisesRegex(ValueError, "already_contains_evidence"):
                    evaluate_live_local_llm(api_endpoint="http://127.0.0.1:123", query_spec=[_spec()],
                                            observer=object(), output_dir=directory)
                request.assert_not_called()

    def test_generation_evidence_omits_prompt_answer_and_arbitrary_model_field_names(self) -> None:
        secret = "synthetic-secret-token"
        path = "C:/synthetic-private/raw-document.pdf"
        result = generation_observation(
            {"model": "synthetic-model", "prompt": secret + path, "stream": False,
             "options": {"temperature": 0, "seed": 0, "unknown": secret}},
            {"model": "synthetic-model", "done": True, "eval_count": 10,
             "response": json.dumps({"answer": secret + path, secret: path})},
            status=200, duration_ms=1,
        )
        encoded = json.dumps(result)
        self.assertNotIn(secret, encoded)
        self.assertNotIn(path, encoded)
        self.assertEqual(["answer"], result["structured_keys"])
        self.assertTrue(result["generation_completed"])

    def test_unfinished_zero_tokens_mismatched_model_are_not_completed(self) -> None:
        request = {"model": "synthetic-model"}
        for updates in ({"done": False}, {"eval_count": 0}, {"model": "other"}, {"response": ""}):
            response = {"model": "synthetic-model", "done": True, "eval_count": 3, "response": "text", **updates}
            self.assertFalse(generation_observation(request, response, status=200, duration_ms=1)["generation_completed"])

    def test_spec_validation_rejects_duplicates_empty_anchors_and_string_booleans(self) -> None:
        for specs in ([_spec(), _spec()], [{**_spec(), "expected_abstain": "false"}],
                      [{**_spec(), "required_facts": [[]]}], [{**_spec(), "required_facts": [""]}],
                      [{**_spec(), "expected_block": True, "expected_http_status": 503}]):
            with self.subTest(specs=specs), self.assertRaises(ValueError):
                _query_specs(specs)

    def test_tenant_and_endpoint_body_overrides_are_not_silently_ignored(self) -> None:
        for key in ("tenant_id", "endpoint", "api_token"):
            with self.assertRaises(ValueError):
                _request_payload({**_spec(), "request_overrides": {key: "value"}}, {})

    def test_private_artifacts_do_not_include_headers_and_case_id_is_not_a_path(self) -> None:
        class FakeObserver:
            @contextmanager
            def case(self, _case_id):
                yield

            def events_for(self, _case_id):
                return [_event()]

            def provenance(self):
                return {"observer": "synthetic-unit-test"}

        with tempfile.TemporaryDirectory() as temp, patch(
            "scripts.evaluate_live_local_llm._read_json", return_value=(200, _response())
        ) as request:
            spec = {**_spec(), "id": "../../synthetic-case"}
            report = evaluate_live_local_llm(api_endpoint="http://127.0.0.1:123", query_spec=[spec],
                                             observer=FakeObserver(), output_dir=Path(temp),
                                             headers={"Authorization": "Bearer synthetic-secret-token"})
            self.assertTrue(report["passed"])
            self.assertEqual("Bearer synthetic-secret-token", request.call_args.kwargs["headers"]["Authorization"])
            for artifact in Path(temp).glob("*.json"):
                self.assertNotIn("synthetic-secret-token", artifact.read_text(encoding="utf-8"))
            self.assertTrue(report["cases"][0]["artifact_name"].startswith("case-0001-"))

    def test_http_timeout_stops_suite_and_reports_remaining_cases_not_run(self) -> None:
        class FakeObserver:
            @contextmanager
            def case(self, _case_id):
                yield

            def events_for(self, _case_id):
                return []

            def provenance(self):
                return {"observer": "synthetic-unit-test"}

        with tempfile.TemporaryDirectory() as temp, patch(
            "scripts.evaluate_live_local_llm._read_json", side_effect=TimeoutError("synthetic secret")
        ) as request:
            report = evaluate_live_local_llm(api_endpoint="http://127.0.0.1:123",
                                             query_spec=[_spec(), {**_spec(), "id": "second"}],
                                             observer=FakeObserver(), output_dir=Path(temp))
        self.assertEqual(1, request.call_count)
        self.assertEqual(1, report["not_run_count"])
        self.assertEqual(["second"], report["not_run_case_ids"])
        self.assertFalse(report["passed"])
        self.assertNotIn("synthetic secret", json.dumps(report))

    def test_observer_error_after_timeout_keeps_the_timeout_in_the_class_chain(self) -> None:
        class StuckObserver:
            @contextmanager
            def case(self, _case_id):
                try:
                    yield
                finally:
                    raise RuntimeError("observer_call_still_running")

            def events_for(self, _case_id):
                return []

            def provenance(self):
                return {"observer": "synthetic-unit-test"}

        with tempfile.TemporaryDirectory() as temp, patch(
            "scripts.evaluate_live_local_llm._read_json", side_effect=TimeoutError("synthetic secret")
        ):
            report = evaluate_live_local_llm(api_endpoint="http://127.0.0.1:123", query_spec=[_spec()],
                                             observer=StuckObserver(), output_dir=Path(temp))
        case = report["cases"][0]
        self.assertEqual("RuntimeError", case["error_type"])
        self.assertEqual(["RuntimeError", "TimeoutError"], case["error_chain_types"])
        self.assertNotIn("synthetic secret", json.dumps(report))
        self.assertNotIn("observer_call_still_running", json.dumps(report))

    def test_repeated_server_failures_stop_but_success_resets_counter(self) -> None:
        class FakeObserver:
            @contextmanager
            def case(self, _case_id):
                yield

            def events_for(self, _case_id):
                return []

            def provenance(self):
                return {"observer": "synthetic-unit-test"}

        for statuses, expected_count in (([503, 503, 200], 2), ([503, 200, 503, 503, 200], 4)):
            with self.subTest(statuses=statuses), tempfile.TemporaryDirectory() as temp, patch(
                "scripts.evaluate_live_local_llm._read_json",
                side_effect=[(status, {}) for status in statuses],
            ) as request:
                report = evaluate_live_local_llm(
                    api_endpoint="http://127.0.0.1:123",
                    query_spec=[{**_spec(), "id": str(index)} for index in range(len(statuses))],
                    observer=FakeObserver(), output_dir=Path(temp),
                )
            self.assertEqual(expected_count, request.call_count)
            self.assertEqual(1, report["not_run_count"])
            self.assertEqual("consecutive_server_failures", report["interrupted_reason"])
            self.assertFalse(report["passed"])


class LiveLocalLlmObserverTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.received: list[dict] = []
        received = self.received

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append({"body": payload, "headers": dict(self.headers)})
                response = {"model": payload["model"], "done": True, "eval_count": 2,
                            "response": json.dumps({"answer": "synthetic answer"})}
                body = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.worker.join, 5)
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)

    def test_observer_forwards_bytes_without_auth_headers_and_attributes_real_http_call(self) -> None:
        endpoint = f"http://127.0.0.1:{self.upstream.server_port}"
        with LocalOllamaObserver(endpoint) as observer:
            payload = {"model": "synthetic-model", "prompt": "private synthetic prompt", "stream": False}
            with observer.case("synthetic-case"):
                status, _ = _read_json(observer.endpoint + "/api/generate", payload=payload,
                                       headers={"Authorization": "Bearer synthetic-secret"})
            events = observer.events_for("synthetic-case")
        self.assertEqual(200, status)
        self.assertEqual(payload, self.received[0]["body"])
        self.assertNotIn("Authorization", self.received[0]["headers"])
        self.assertEqual(1, len(events))
        self.assertTrue(events[0]["generation_completed"])
        self.assertNotIn("private synthetic prompt", json.dumps(events))

    def test_observer_blocks_model_download_and_streaming_routes(self) -> None:
        endpoint = f"http://127.0.0.1:{self.upstream.server_port}"
        with LocalOllamaObserver(endpoint) as observer:
            status, _ = _read_json(observer.endpoint + "/api/pull", payload={"model": "synthetic-model"})
            self.assertEqual(404, status)
            status, _ = _read_json(observer.endpoint + "/api/generate", payload={"model": "synthetic-model", "stream": True})
            self.assertEqual(400, status)
            with observer.case("outer"):
                with self.assertRaises(RuntimeError):
                    with observer.case("inner"):
                        pass
        self.assertEqual([], self.received)

    def test_repeated_case_id_does_not_reuse_previous_generation_evidence(self) -> None:
        endpoint = f"http://127.0.0.1:{self.upstream.server_port}"
        with LocalOllamaObserver(endpoint) as observer:
            with observer.case("repeated"):
                _read_json(observer.endpoint + "/api/generate",
                           payload={"model": "synthetic-model", "prompt": "first", "stream": False})
            self.assertEqual(1, len(observer.events_for("repeated")))
            with observer.case("repeated"):
                pass
            self.assertEqual([], observer.events_for("repeated"))


class McpFetchLlmBridgeTests(unittest.TestCase):
    def test_empty_mcp_evidence_requires_verified_empty_search_and_explicit_negative_case(self) -> None:
        case = {"passed": True, "fetch": [], "search_protocol_passed": True,
                "result_count": 0, "search": {"results": []}}
        self.assertEqual(([], []), mcp_fetch_evidence(case, allow_empty=True))
        with self.assertRaises(ValueError):
            mcp_fetch_evidence(case)
        for changed in ({"search_protocol_passed": False}, {"result_count": False},
                        {"search": {"results": [{"id": "unfetched"}]}}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                mcp_fetch_evidence({**case, **changed}, allow_empty=True)

    def _case(self) -> dict:
        return {"id": _spec()["id"], "query": _spec()["query"], "passed": True,
                "fetch": [{"passed": True, "payload": {
                    "text": "Unmodified synthetic MCP text.",
                    "metadata": {"approval_status": "approved", "document_id": "synthetic-document",
                                 "chunk_id": "synthetic-chunk", "approval_id": "synthetic-approval",
                                 "approved_content_hash": "a" * 64, "article_no": "Article 1"}}}]}

    def test_bridge_preserves_exact_mcp_text_and_records_payload_hash(self) -> None:
        case = self._case()
        evidence, lineage = mcp_fetch_evidence(case)
        self.assertEqual(case["fetch"][0]["payload"]["text"], evidence[0]["text"])
        self.assertEqual(lineage[0]["mcp_text_sha256"], lineage[0]["forwarded_text_sha256"])
        self.assertEqual(64, len(lineage[0]["mcp_fetch_payload_sha256"]))

    def test_bridge_rejects_unapproved_or_incomplete_identity(self) -> None:
        for key, value in (("approval_status", "pending"), ("approval_id", ""), ("approved_content_hash", "missing")):
            with self.subTest(key=key):
                case = self._case()
                case["fetch"][0]["payload"]["metadata"][key] = value
                with self.assertRaises(ValueError):
                    mcp_fetch_evidence(case)

    def test_bridge_query_mismatch_does_not_call_model_and_is_not_production_api(self) -> None:
        class FakeObserver:
            endpoint = "http://127.0.0.1:123"

            def provenance(self):
                return {"observer": "synthetic-unit-test"}

            def events_for(self, _case_id):
                return []

        case = self._case()
        case["query"] = "Different query"
        with tempfile.TemporaryDirectory() as temp, patch("app.api.routes_rag._orchestrated_chat_answer") as call:
            report = evaluate_mcp_fetch_bridge(mcp_report={"passed": True, "cases": [case]},
                                               query_spec=[_spec()], observer=FakeObserver(), output_dir=Path(temp))
        call.assert_not_called()
        self.assertFalse(report["production_endpoint"])
        self.assertFalse(report["passed"])
        self.assertFalse(report["cases"][0]["actual_model_called"])

    def test_bridge_repeated_runtime_failure_stops_and_records_only_exception_types(self) -> None:
        from fastapi import HTTPException

        class FakeObserver:
            endpoint = "http://127.0.0.1:123"

            @contextmanager
            def case(self, _case_id):
                yield

            def provenance(self):
                return {"observer": "synthetic-unit-test"}

            def events_for(self, _case_id):
                return []

        def fail(*_args, **_kwargs):
            try:
                raise TimeoutError("synthetic-private-token")
            except TimeoutError as exc:
                raise HTTPException(status_code=503, detail="synthetic-private-path") from exc

        specs = [{**_spec(), "id": str(index)} for index in range(3)]
        cases = [{**self._case(), "id": spec["id"]} for spec in specs]
        with tempfile.TemporaryDirectory() as temp, patch(
            "app.api.routes_rag._orchestrated_chat_answer", side_effect=fail,
        ) as call:
            report = evaluate_mcp_fetch_bridge(
                mcp_report={"passed": True, "cases": cases}, query_spec=specs,
                observer=FakeObserver(), output_dir=Path(temp),
            )
        self.assertEqual(2, call.call_count)
        self.assertEqual(["2"], report["not_run_case_ids"])
        self.assertFalse(report["passed"])
        self.assertEqual(["HTTPException", "TimeoutError"], report["cases"][0]["error_chain_types"])
        self.assertNotIn("synthetic-private", json.dumps(report))

    def test_bridge_observer_failure_does_not_count_unexecuted_cases_as_failures(self) -> None:
        class FakeObserver:
            endpoint = "http://127.0.0.1:123"

            @contextmanager
            def case(self, _case_id):
                raise TimeoutError("synthetic-observer-call-still-running")
                yield

            def provenance(self):
                return {"observer": "synthetic-unit-test"}

            def events_for(self, _case_id):
                return []

        specs = [{**_spec(), "id": str(index)} for index in range(2)]
        cases = [{**self._case(), "id": spec["id"]} for spec in specs]
        with tempfile.TemporaryDirectory() as temp, patch("app.api.routes_rag._orchestrated_chat_answer") as call:
            report = evaluate_mcp_fetch_bridge(
                mcp_report={"passed": True, "cases": cases}, query_spec=specs,
                observer=FakeObserver(), output_dir=Path(temp),
            )
        call.assert_not_called()
        self.assertEqual(1, report["aggregate"]["case_count"])
        self.assertEqual(["1"], report["not_run_case_ids"])
        self.assertEqual("transport_observer_or_evidence_failure", report["interrupted_reason"])


if __name__ == "__main__":
    unittest.main()
