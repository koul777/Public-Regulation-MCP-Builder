from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import DEFAULT, patch

from scripts.benchmark_mcp_concurrent_queries import benchmark_mcp_concurrent_queries, run


class BenchmarkMcpConcurrentQueriesTests(unittest.TestCase):
    def test_runs_concurrent_query_tasks_and_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_json = root / "concurrent.json"
            out_md = root / "concurrent.md"
            with _patched_runtime() as mocks:
                report = benchmark_mcp_concurrent_queries(
                    data_dir=root / "data",
                    tenant_id="tenant-demo",
                    profile_id="profile-demo",
                    queries=["childcare", "faculty"],
                    rounds=2,
                    concurrency=2,
                    min_warm_records=3,
                    max_task_total_ms=1000.0,
                    max_batch_elapsed_ms=2000.0,
                    out_json=out_json,
                    out_md=out_md,
                )
            markdown = out_md.read_text(encoding="utf-8")
            json_written = out_json.is_file()

        self.assertTrue(report["passed"])
        self.assertEqual("profile-demo", report["profile_id"])
        self.assertEqual(4, report["task_count"])
        self.assertEqual(4, report["summary"]["successful_count"])
        self.assertEqual(1, report["summary"]["search_result_count_min"])
        self.assertIn("MCP Concurrent Query Benchmark", markdown)
        self.assertTrue(json_written)
        self.assertTrue(all(call.kwargs["profile_id"] == "profile-demo" for call in mocks["search_regulations"].call_args_list))
        self.assertTrue(all(call.kwargs["profile_id"] == "profile-demo" for call in mocks["fetch_regulation"].call_args_list))

    def test_records_query_spec_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            query_spec = root / "queries.json"
            query_spec.write_text(json.dumps([{"query": "childcare"}]), encoding="utf-8")
            expected_sha = hashlib.sha256(query_spec.read_bytes()).hexdigest()

            with _patched_runtime():
                report = benchmark_mcp_concurrent_queries(
                    data_dir=root / "data",
                    tenant_id="tenant-demo",
                    query_specs=[{"query": "childcare"}],
                    query_spec_source=query_spec,
                    rounds=1,
                    concurrency=1,
                )

        self.assertEqual(str(query_spec), report["query_spec_path"])
        self.assertEqual(expected_sha, report["query_spec_sha256"])

    def test_threshold_findings_fail_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with _patched_runtime(warm_record_count=2):
                report = benchmark_mcp_concurrent_queries(
                    data_dir=root / "data",
                    tenant_id="tenant-demo",
                    queries=["childcare"],
                    rounds=1,
                    concurrency=1,
                    min_warm_records=3,
                    max_task_total_ms=0.001,
                )

        codes = {item["code"] for item in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("concurrent-warm-record-count-below-minimum", codes)
        self.assertIn("concurrent-task-elapsed-too-high", codes)

    def test_expected_no_evidence_query_passes_when_no_results_are_returned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with _patched_runtime() as mocks:
                mocks["search_regulations"].return_value = {"results": [], "metadata": {"trace_id": "trace-none"}}
                report = benchmark_mcp_concurrent_queries(
                    data_dir=root / "data",
                    tenant_id="tenant-demo",
                    query_specs=[{"query": "nonexistent rule", "expect_no_evidence": True}],
                    rounds=2,
                    concurrency=2,
                )

        self.assertTrue(report["passed"])
        self.assertEqual(2, report["summary"]["successful_count"])
        self.assertTrue(all(item["expect_no_evidence"] for item in report["measurements"]))
        self.assertEqual([0, 0], [item["search_result_count"] for item in report["measurements"]])

    def test_expected_no_evidence_query_fails_when_results_are_returned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with _patched_runtime():
                report = benchmark_mcp_concurrent_queries(
                    data_dir=root / "data",
                    tenant_id="tenant-demo",
                    query_specs=[{"query": "nonexistent rule", "expect_no_evidence": True}],
                    rounds=1,
                    concurrency=1,
                )

        self.assertFalse(report["passed"])
        self.assertEqual(
            ["concurrent-expected-no-evidence-returned-results"],
            [item["code"] for item in report["findings"]],
        )

    def test_cli_can_fail_on_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with _patched_runtime(warm_record_count=2):
                stdout = io.StringIO()
                exit_code = run(
                    [
                        "--data-dir",
                        str(root / "data"),
                        "--tenant-id",
                        "tenant-demo",
                        "--query",
                        "childcare",
                        "--rounds",
                        "1",
                        "--concurrency",
                        "1",
                        "--min-warm-records",
                        "3",
                        "--fail-on-threshold",
                    ],
                    stdout=stdout,
                )

        self.assertEqual(2, exit_code)
        self.assertIn("concurrent-warm-record-count-below-minimum", stdout.getvalue())


def _configure_patches(mocks: dict, *, warm_record_count: int) -> None:
    mocks["settings_for_mcp_project"].return_value = object()
    mocks["mcp_auth_context"].return_value = object()
    mocks["warm_mcp_runtime"].return_value = {
        "warmed": True,
        "record_count": warm_record_count,
        "bm25_index_ready": True,
        "timing_ms": {"total_elapsed_ms": 1.0},
    }
    mocks["search_regulations"].return_value = {
        "results": [{"id": "result-1"}],
        "metadata": {"trace_id": "trace-1", "timing_ms": {"scoring_elapsed_ms": 1.0}},
    }
    mocks["fetch_regulation"].return_value = {
        "id": "result-1",
        "text": "childcare leave",
        "metadata": {"document_name": "Demo", "article_no": "Article 1"},
    }


class _PatchedRuntime:
    def __init__(self, *, warm_record_count: int = 3):
        self.warm_record_count = warm_record_count
        self.patcher = None
        self.mocks = None

    def __enter__(self):
        self.patcher = patch.multiple(
            "scripts.benchmark_mcp_concurrent_queries",
            settings_for_mcp_project=DEFAULT,
            mcp_auth_context=DEFAULT,
            warm_mcp_runtime=DEFAULT,
            search_regulations=DEFAULT,
            fetch_regulation=DEFAULT,
        )
        self.mocks = self.patcher.__enter__()
        _configure_patches(self.mocks, warm_record_count=self.warm_record_count)
        return self.mocks

    def __exit__(self, exc_type, exc, tb):
        return self.patcher.__exit__(exc_type, exc, tb)


def _patched_runtime(*, warm_record_count: int = 3):
    return _PatchedRuntime(warm_record_count=warm_record_count)


if __name__ == "__main__":
    unittest.main()
