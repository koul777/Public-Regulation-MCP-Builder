from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.benchmark_mcp_first_query import (
    CHILD_PROTOCOL_VERSION,
    _child_query,
    _run_child_query,
    benchmark_mcp_first_query,
    query_fingerprint,
    run,
)


TEST_REPO_COMMIT = "a" * 40
TEST_SOURCE_STATE = {
    "scope": "mcp-performance-python-source-v1",
    "status": "available",
    "sha256": "b" * 64,
    "file_count": 3,
    "byte_count": 101,
    "stable": True,
}


class BenchmarkMcpFirstQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.capture_source_state = patch(
            "scripts.benchmark_mcp_first_query.capture_mcp_performance_source_state",
            return_value=dict(TEST_SOURCE_STATE),
        ).start()
        self.finalize_source_state = patch(
            "scripts.benchmark_mcp_first_query.finalize_mcp_performance_source_state",
            return_value=dict(TEST_SOURCE_STATE),
        ).start()
        self.addCleanup(patch.stopall)

    def test_child_disables_operational_writes_and_measures_cold_then_warm(self) -> None:
        query = "육아휴직 규정"
        request = _child_request(query=query, warm_iterations=2)
        stdout = io.StringIO()
        search_response = {
            "results": [
                {
                    "id": "sensitive-document-id",
                    "text": "sensitive regulation text",
                }
            ],
            "metadata": {
                "trace_id": "sensitive-trace-id",
                "retrieval_strategy": "catalog_toc_body",
                "timing_ms": {
                    "load_vector_records_elapsed_ms": 4.0,
                    "scoring_elapsed_ms": 2.0,
                },
            },
        }

        with (
            patch(
                "app.mcp_server.regulation_tools.settings_for_mcp_project",
                return_value="settings",
            ) as settings_for_project,
            patch(
                "app.mcp_server.regulation_tools.mcp_auth_context",
                return_value="auth",
            ) as auth_context,
            patch(
                "app.mcp_server.regulation_tools.search_regulations",
                side_effect=[search_response, search_response, search_response],
            ) as search,
            patch(
                "scripts.benchmark_mcp_first_query._elapsed_ms",
                side_effect=[10.0, 20.0, 5.0, 6.0],
            ),
        ):
            exit_code = _child_query(request=request, stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertEqual(3, search.call_count)
        settings_for_project.assert_called_once_with(
            data_dir=Path("data"),
            tenant_id="tenant-demo",
            tenant_storage_isolation=True,
            api_audit_enabled=False,
            rag_trace_enabled=False,
        )
        auth_context.assert_called_once_with(tenant_id="tenant-demo")
        self.assertEqual(1, payload["cold"]["result_count"])
        self.assertEqual(20.0, payload["cold"]["search_elapsed_ms"])
        self.assertEqual(2, len(payload["warm"]))
        self.assertEqual("catalog_toc_body", payload["cold"]["retrieval_strategy"])
        self.assertEqual(
            4.0,
            payload["cold"]["trace_timing_ms"]["load_vector_records_elapsed_ms"],
        )
        rendered = stdout.getvalue()
        self.assertNotIn("sensitive-document-id", rendered)
        self.assertNotIn("sensitive regulation text", rendered)
        self.assertNotIn("sensitive-trace-id", rendered)
        self.assertNotIn(query, rendered)

    def test_parent_runs_fresh_child_and_extracts_only_safe_fields(self) -> None:
        query = "복무 규정"
        child_payload = {
            "protocol_version": CHILD_PROTOCOL_VERSION,
            "query_id": "q-safe",
            "query_sha256": query_fingerprint(query),
            "setup": {"success": True, "elapsed_ms": 90.0},
            "cold": {
                "attempted": True,
                "success": True,
                "search_elapsed_ms": 75.0,
                "result_count": 2,
                "retrieval_strategy": "catalog_toc_body",
                "trace_timing_ms": {"scoring_elapsed_ms": 40.0},
                "results": [{"text": "must-not-be-copied", "id": "doc-secret"}],
            },
            "warm": [
                {
                    "attempted": True,
                    "success": True,
                    "search_elapsed_ms": 12.0,
                    "result_count": 2,
                    "retrieval_strategy": "catalog_toc_body",
                    "trace_timing_ms": {"scoring_elapsed_ms": 4.0},
                }
            ],
        }
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(child_payload),
            stderr="diagnostic secret",
        )

        with (
            patch(
                "scripts.benchmark_mcp_first_query.subprocess.run",
                return_value=completed,
            ) as run_child,
            patch(
                "scripts.benchmark_mcp_first_query._elapsed_ms",
                return_value=250.0,
            ),
        ):
            measurement = _run_child_query(
                data_dir=Path("data"),
                tenant_id="tenant-demo",
                profile_id="profile-demo",
                query_id="q-safe",
                query=query,
                query_sha256=query_fingerprint(query),
                iteration=1,
                warm_iterations=1,
                top_k=3,
                security_levels=["internal"],
                tenant_storage_isolation=False,
                child_timeout_seconds=30.0,
            )

        call_args = run_child.call_args
        command = call_args.args[0]
        child_request = json.loads(call_args.kwargs["input"])
        self.assertNotIn(query, command)
        self.assertEqual(query, child_request["query"])
        self.assertEqual(["--child-query"], command[-1:])
        self.assertEqual(250.0, measurement["process_wall_elapsed_ms"])
        self.assertEqual(75.0, measurement["cold"]["search_elapsed_ms"])
        self.assertEqual(12.0, measurement["warm"][0]["search_elapsed_ms"])
        self.assertEqual(
            "catalog_toc_body",
            measurement["cold"]["retrieval_strategy"],
        )
        self.assertIn("stderr_sha256", measurement)
        rendered = json.dumps(measurement)
        self.assertNotIn("diagnostic secret", rendered)
        self.assertNotIn("must-not-be-copied", rendered)
        self.assertNotIn("doc-secret", rendered)

    def test_report_summarizes_percentiles_success_rate_and_trace_stages(self) -> None:
        process_times = [100.0, 200.0, 300.0, 400.0]
        measurements = [
            _measurement(
                process_elapsed_ms=process_elapsed,
                cold_elapsed_ms=process_elapsed / 2,
                warm_elapsed_ms=process_elapsed / 10,
                cold_success=index < 3,
            )
            for index, process_elapsed in enumerate(process_times)
        ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_json = root / "first-query.json"
            out_md = root / "first-query.md"
            with (
                patch(
                    "scripts.benchmark_mcp_first_query._run_child_query",
                    side_effect=measurements,
                ),
                patch(
                    "scripts.benchmark_mcp_first_query.current_repo_commit",
                    return_value=TEST_REPO_COMMIT,
                ) as commit_metadata,
            ):
                report = benchmark_mcp_first_query(
                    data_dir=root / "data",
                    tenant_id="tenant-demo",
                    queries=["휴가 규정"],
                    iterations=4,
                    warm_iterations=1,
                    min_success_count=3,
                    max_cold_p95_ms=450.0,
                    max_warm_p95_ms=45.0,
                    out_json=out_json,
                    out_md=out_md,
                )
            written = json.loads(out_json.read_text(encoding="utf-8"))
            markdown = out_md.read_text(encoding="utf-8")

        self.assertTrue(report["passed"])
        self.assertEqual(TEST_REPO_COMMIT, report["repo_commit"])
        self.assertEqual(TEST_REPO_COMMIT, written["repo_commit"])
        self.assertEqual(TEST_SOURCE_STATE, report["source_state"])
        self.assertEqual(TEST_SOURCE_STATE, written["source_state"])
        self.capture_source_state.assert_called_once()
        self.finalize_source_state.assert_called_once()
        commit_metadata.assert_called_once_with(
            Path(__file__).resolve().parents[1]
        )
        self.assertEqual(3, report["summary"]["cold"]["successful_count"])
        self.assertEqual(75.0, report["summary"]["cold"]["success_rate_percent"])
        self.assertEqual(20.0, report["summary"]["setup_elapsed_ms"]["p50"])
        self.assertEqual(200.0, report["summary"]["process_wall_elapsed_ms"]["p50"])
        self.assertEqual(400.0, report["summary"]["process_wall_elapsed_ms"]["p95"])
        self.assertEqual(400.0, report["summary"]["process_wall_elapsed_ms"]["p99"])
        self.assertEqual(150.0, report["summary"]["cold_non_search_overhead_ms"]["p95"])
        self.assertEqual(130.0, report["summary"]["child_harness_overhead_ms"]["p95"])
        self.assertEqual(40.0, report["summary"]["warm"]["search_elapsed_ms"]["max"])
        self.assertEqual(
            20.0,
            report["summary"]["cold"]["trace_timing_ms"]["scoring_elapsed_ms"]["p50"],
        )
        self.assertEqual(report["query_set_sha256"], written["query_set_sha256"])
        self.assertIn("MCP First Query Benchmark", markdown)
        self.assertIn(f"Repository commit: `{TEST_REPO_COMMIT}`", markdown)
        self.assertIn(
            "Source state: `available` (mcp-performance-python-source-v1)",
            markdown,
        )
        self.assertIn("Cold non-search overhead", markdown)
        self.assertIn("Child harness overhead", markdown)
        self.assertNotIn("휴가 규정", json.dumps(report, ensure_ascii=False))
        self.assertNotIn("휴가 규정", markdown)

    def test_threshold_findings_and_fail_on_threshold_exit(self) -> None:
        failed = _measurement(
            process_elapsed_ms=700.0,
            cold_elapsed_ms=None,
            warm_elapsed_ms=None,
            cold_success=False,
            warm_success=False,
        )
        with patch(
            "scripts.benchmark_mcp_first_query._run_child_query",
            return_value=failed,
        ):
            stdout = io.StringIO()
            exit_code = run(
                [
                    "--query",
                    "비밀 쿼리 원문",
                    "--iterations",
                    "1",
                    "--warm-iterations",
                    "1",
                    "--min-success-count",
                    "1",
                    "--max-cold-p95-ms",
                    "500",
                    "--max-warm-p95-ms",
                    "50",
                    "--flat-storage",
                    "--profile-id",
                    "profile-demo",
                    "--fail-on-threshold",
                ],
                stdout=stdout,
            )

        report = json.loads(stdout.getvalue())
        codes = {item["code"] for item in report["findings"]}
        self.assertEqual(2, exit_code)
        self.assertIn("first-query-success-count-below-minimum", codes)
        self.assertIn("first-query-cold-p95-exceeded", codes)
        self.assertIn("first-query-warm-p95-unavailable", codes)
        self.assertIn("first-query-warm-search-failed", codes)
        self.assertFalse(report["tenant_storage_isolation"])
        self.assertEqual("profile-demo", report["profile_id"])
        self.assertNotIn("비밀 쿼리 원문", stdout.getvalue())

    def test_zero_result_search_does_not_qualify_as_success_by_default(self) -> None:
        with patch(
            "scripts.benchmark_mcp_first_query._run_child_query",
            return_value=_measurement(result_count=0),
        ):
            report = benchmark_mcp_first_query(
                data_dir=Path("data"),
                tenant_id="tenant-demo",
                queries=["empty storage query"],
                iterations=1,
            )

        codes = {item["code"] for item in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertEqual(1, report["thresholds"]["min_result_count"])
        self.assertEqual(1, report["summary"]["cold"]["operational_successful_count"])
        self.assertEqual(0, report["summary"]["cold"]["successful_count"])
        self.assertEqual(
            1,
            report["summary"]["cold"]["result_requirement_failed_count"],
        )
        self.assertIn("first-query-success-count-below-minimum", codes)
        self.assertIn("first-query-result-count-below-minimum", codes)

    def test_result_requirement_failure_is_reported_when_minimum_success_is_met(self) -> None:
        with patch(
            "scripts.benchmark_mcp_first_query._run_child_query",
            side_effect=[
                _measurement(result_count=1),
                _measurement(result_count=0),
            ],
        ):
            report = benchmark_mcp_first_query(
                data_dir=Path("data"),
                tenant_id="tenant-demo",
                queries=["qualifying query", "empty query"],
                iterations=1,
                min_success_count=1,
                min_result_count=1,
            )

        codes = {item["code"] for item in report["findings"]}
        self.assertEqual(1, report["summary"]["cold"]["successful_count"])
        self.assertEqual(
            1,
            report["summary"]["cold"]["result_requirement_failed_count"],
        )
        self.assertNotIn("first-query-success-count-below-minimum", codes)
        self.assertIn("first-query-result-count-below-minimum", codes)
        self.assertFalse(report["passed"])

    def test_no_evidence_query_requires_zero_results_but_cannot_be_only_probe(self) -> None:
        with patch(
            "scripts.benchmark_mcp_first_query._run_child_query",
            return_value=_measurement(result_count=0),
        ):
            report = benchmark_mcp_first_query(
                data_dir=Path("data"),
                tenant_id="tenant-demo",
                query_specs=[
                    {
                        "id": "no-evidence-control",
                        "query": "synthetic absent policy",
                        "expect_no_evidence": True,
                    }
                ],
                iterations=1,
            )

        codes = {item["code"] for item in report["findings"]}
        self.assertEqual(1, report["summary"]["cold"]["successful_count"])
        self.assertEqual(
            1,
            report["summary"]["cold"]["no_evidence_successful_count"],
        )
        self.assertEqual(
            0,
            report["summary"]["cold"]["result_requirement_failed_count"],
        )
        self.assertFalse(report["passed"])
        self.assertIn("first-query-answerable-query-required", codes)

    def test_mixed_query_spec_applies_answerable_and_no_evidence_result_rules(self) -> None:
        with patch(
            "scripts.benchmark_mcp_first_query._run_child_query",
            side_effect=[
                _measurement(result_count=2),
                _measurement(result_count=0),
            ],
        ):
            report = benchmark_mcp_first_query(
                data_dir=Path("data"),
                tenant_id="tenant-demo",
                query_specs=[
                    {"id": "answerable", "query": "synthetic policy"},
                    {
                        "id": "no-evidence",
                        "query": "synthetic absent policy",
                        "expect_no_evidence": True,
                    },
                ],
                iterations=1,
            )

        self.assertTrue(report["passed"])
        self.assertEqual(2, report["summary"]["cold"]["successful_count"])
        self.assertEqual(
            1,
            report["summary"]["cold"]["answerable_successful_count"],
        )
        self.assertEqual(
            1,
            report["summary"]["cold"]["no_evidence_successful_count"],
        )
        self.assertEqual(
            1,
            report["summary"]["cold"]["answerable_result_count"]["count"],
        )
        self.assertEqual(
            0.0,
            report["summary"]["cold"]["no_evidence_result_count"]["min"],
        )

    def test_required_retrieval_strategy_is_fail_closed_and_cli_configurable(self) -> None:
        with patch(
            "scripts.benchmark_mcp_first_query._run_child_query",
            return_value=_measurement(retrieval_strategy="flat_rag"),
        ):
            stdout = io.StringIO()
            exit_code = run(
                [
                    "--query",
                    "hierarchy query",
                    "--iterations",
                    "1",
                    "--min-result-count",
                    "1",
                    "--require-retrieval-strategy",
                    "catalog_toc_body",
                    "--fail-on-threshold",
                ],
                stdout=stdout,
            )

        report = json.loads(stdout.getvalue())
        codes = {item["code"] for item in report["findings"]}
        self.assertEqual(2, exit_code)
        self.assertFalse(report["passed"])
        self.assertEqual(
            "catalog_toc_body",
            report["thresholds"]["required_retrieval_strategy"],
        )
        self.assertEqual(0, report["summary"]["cold"]["successful_count"])
        self.assertEqual(
            1,
            report["summary"]["cold"][
                "retrieval_strategy_requirement_failed_count"
            ],
        )
        self.assertIn("first-query-retrieval-strategy-mismatch", codes)
        self.assertNotIn("hierarchy query", stdout.getvalue())

    def test_matching_required_retrieval_strategy_qualifies(self) -> None:
        with patch(
            "scripts.benchmark_mcp_first_query._run_child_query",
            return_value=_measurement(retrieval_strategy="catalog_toc_body"),
        ):
            report = benchmark_mcp_first_query(
                data_dir=Path("data"),
                tenant_id="tenant-demo",
                queries=["hierarchy query"],
                iterations=1,
                required_retrieval_strategy="catalog_toc_body",
            )

        self.assertTrue(report["passed"])
        self.assertEqual(1, report["summary"]["cold"]["successful_count"])

    def test_strategy_failure_is_reported_when_minimum_success_is_met(self) -> None:
        with patch(
            "scripts.benchmark_mcp_first_query._run_child_query",
            side_effect=[
                _measurement(retrieval_strategy="catalog_toc_body"),
                _measurement(retrieval_strategy="flat_rag"),
            ],
        ):
            report = benchmark_mcp_first_query(
                data_dir=Path("data"),
                tenant_id="tenant-demo",
                queries=["hierarchy query", "fallback query"],
                iterations=1,
                min_success_count=1,
                required_retrieval_strategy="catalog_toc_body",
            )

        codes = {item["code"] for item in report["findings"]}
        self.assertEqual(1, report["summary"]["cold"]["successful_count"])
        self.assertEqual(
            1,
            report["summary"]["cold"][
                "retrieval_strategy_requirement_failed_count"
            ],
        )
        self.assertNotIn("first-query-success-count-below-minimum", codes)
        self.assertIn("first-query-retrieval-strategy-mismatch", codes)
        self.assertFalse(report["passed"])

    def test_unsafe_retrieval_strategy_is_not_copied_from_child_output(self) -> None:
        query = "safe query"
        child_payload = {
            "protocol_version": CHILD_PROTOCOL_VERSION,
            "query_id": "q-safe",
            "query_sha256": query_fingerprint(query),
            "setup": {"success": True, "elapsed_ms": 1.0},
            "cold": {
                "attempted": True,
                "success": True,
                "search_elapsed_ms": 2.0,
                "result_count": 1,
                "retrieval_strategy": "secret strategy | do-not-copy",
                "trace_timing_ms": {},
            },
            "warm": [],
        }
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(child_payload),
            stderr="",
        )
        with (
            patch(
                "scripts.benchmark_mcp_first_query.subprocess.run",
                return_value=completed,
            ),
            patch(
                "scripts.benchmark_mcp_first_query._elapsed_ms",
                return_value=3.0,
            ),
        ):
            measurement = _run_child_query(
                data_dir=Path("data"),
                tenant_id="tenant-demo",
                profile_id=None,
                query_id="q-safe",
                query=query,
                query_sha256=query_fingerprint(query),
                iteration=1,
                warm_iterations=0,
                top_k=5,
                security_levels=["internal"],
                tenant_storage_isolation=None,
                child_timeout_seconds=2.0,
            )

        self.assertIsNone(measurement["cold"]["retrieval_strategy"])
        self.assertNotIn("do-not-copy", json.dumps(measurement))

    def test_query_spec_ids_and_file_fingerprint_are_reported_without_query_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_path = root / "queries.json"
            spec_path.write_text(
                json.dumps(
                    {
                        "queries": [
                            {
                                "id": "leave-policy",
                                "question": "연차 휴가 기준",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with patch(
                "scripts.benchmark_mcp_first_query._run_child_query",
                return_value=_measurement(),
            ):
                stdout = io.StringIO()
                exit_code = run(
                    [
                        "--query-spec-json",
                        str(spec_path),
                        "--iterations",
                        "1",
                        "--tenant-storage-isolation",
                    ],
                    stdout=stdout,
                )

        report = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertEqual("leave-policy", report["queries"][0]["query_id"])
        self.assertEqual(64, len(report["queries"][0]["query_sha256"]))
        self.assertEqual(64, len(report["query_spec"]["sha256"]))
        self.assertTrue(report["tenant_storage_isolation"])
        self.assertNotIn("연차 휴가 기준", stdout.getvalue())

    def test_timeout_is_a_safe_failed_measurement(self) -> None:
        timeout = subprocess.TimeoutExpired(
            cmd=["python"],
            timeout=2.0,
            output="sensitive stdout",
            stderr="sensitive stderr",
        )
        query = "징계 규정"
        with (
            patch(
                "scripts.benchmark_mcp_first_query.subprocess.run",
                side_effect=timeout,
            ),
            patch(
                "scripts.benchmark_mcp_first_query._elapsed_ms",
                return_value=2000.0,
            ),
        ):
            measurement = _run_child_query(
                data_dir=Path("data"),
                tenant_id="tenant-demo",
                profile_id=None,
                query_id="discipline",
                query=query,
                query_sha256=query_fingerprint(query),
                iteration=1,
                warm_iterations=0,
                top_k=5,
                security_levels=["internal"],
                tenant_storage_isolation=None,
                child_timeout_seconds=2.0,
            )

        self.assertTrue(measurement["timed_out"])
        self.assertFalse(measurement["protocol_valid"])
        self.assertFalse(measurement["cold"]["attempted"])
        rendered = json.dumps(measurement)
        self.assertNotIn("sensitive stdout", rendered)
        self.assertNotIn("sensitive stderr", rendered)


def _child_request(*, query: str, warm_iterations: int) -> dict:
    return {
        "protocol_version": CHILD_PROTOCOL_VERSION,
        "data_dir": "data",
        "tenant_id": "tenant-demo",
        "profile_id": "profile-demo",
        "query_id": "q-demo",
        "query": query,
        "query_sha256": query_fingerprint(query),
        "warm_iterations": warm_iterations,
        "top_k": 3,
        "security_levels": ["internal"],
        "tenant_storage_isolation": True,
    }


def _measurement(
    *,
    process_elapsed_ms: float = 100.0,
    cold_elapsed_ms: float | None = 50.0,
    warm_elapsed_ms: float | None = 10.0,
    cold_success: bool = True,
    warm_success: bool = True,
    result_count: int = 2,
    retrieval_strategy: str | None = "flat_rag",
) -> dict:
    return {
        "iteration": 1,
        "query_id": "query-placeholder",
        "query_sha256": "a" * 64,
        "returncode": 0,
        "timed_out": False,
        "protocol_valid": True,
        "process_wall_elapsed_ms": process_elapsed_ms,
        "setup": {"success": True, "elapsed_ms": 20.0, "error": None},
        "cold": {
            "attempted": True,
            "success": cold_success,
            "search_elapsed_ms": cold_elapsed_ms,
            "result_count": result_count if cold_success else None,
            "retrieval_strategy": retrieval_strategy if cold_success else None,
            "trace_timing_ms": (
                {"scoring_elapsed_ms": (cold_elapsed_ms or 0.0) / 5}
                if cold_success
                else {}
            ),
            "error": None if cold_success else {"type": "ValueError"},
        },
        "warm": [
            {
                "iteration": 1,
                "attempted": True,
                "success": warm_success,
                "search_elapsed_ms": warm_elapsed_ms,
                "result_count": result_count if warm_success else None,
                "retrieval_strategy": retrieval_strategy if warm_success else None,
                "trace_timing_ms": (
                    {"scoring_elapsed_ms": (warm_elapsed_ms or 0.0) / 5}
                    if warm_success
                    else {}
                ),
                "error": None if warm_success else {"type": "ValueError"},
            }
        ],
    }


if __name__ == "__main__":
    unittest.main()
