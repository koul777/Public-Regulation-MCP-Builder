from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.benchmark_mcp_queries import (
    _result_findings,
    _stats,
    _threshold_findings,
    _warmup_findings,
    benchmark_mcp_queries,
)


class BenchmarkMcpQueriesTests(unittest.TestCase):
    def test_exports_query_benchmark_with_timing_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_json = root / "benchmark.json"
            out_md = root / "benchmark.md"

            with (
                patch("scripts.benchmark_mcp_queries.settings_for_mcp_project", return_value=object()),
                patch("scripts.benchmark_mcp_queries.mcp_auth_context", return_value=object()),
                patch(
                    "scripts.benchmark_mcp_queries.warm_mcp_runtime",
                    return_value={"warmed": True, "record_count": 1, "timing_ms": {"total_elapsed_ms": 3.0}},
                ),
                patch(
                    "scripts.benchmark_mcp_queries.search_regulations",
                    return_value={
                        "results": [{"id": "result-1"}],
                        "metadata": {
                            "trace_id": "trace-1",
                            "timing_ms": {
                                "scoring_elapsed_ms": 2.5,
                                "trace_write_elapsed_ms": 1.25,
                            },
                        },
                    },
                ) as search_mock,
                patch(
                    "scripts.benchmark_mcp_queries.fetch_regulation",
                    return_value={
                        "id": "result-1",
                        "title": "Demo",
                        "text": "Article 10 childcare leave may be requested within 3 years.",
                        "metadata": {
                            "document_name": "Demo Regulation",
                            "article_no": "Article 10",
                            "article_title": "Childcare leave",
                        },
                    },
                ) as fetch_mock,
            ):
                report = benchmark_mcp_queries(
                    data_dir=root / "data",
                    tenant_id="tenant-demo",
                    queries=["childcare leave"],
                    iterations=2,
                    out_json=out_json,
                    out_md=out_md,
                )
                markdown = out_md.read_text(encoding="utf-8")
                self.assertTrue(out_json.exists())
                self.assertIn("MCP Query Benchmark", markdown)
                self.assertIn("MCP Search Internal Timing", markdown)
                self.assertIn("scoring_elapsed_ms", markdown)

        self.assertTrue(report["passed"])
        self.assertEqual("mcp_query_benchmark", report["report_type"])
        self.assertEqual(2, report["summary"]["measurement_count"])
        self.assertEqual(1, report["query_count"])
        self.assertEqual(2, search_mock.call_count)
        self.assertEqual(2, fetch_mock.call_count)
        self.assertEqual(1, report["items"][0]["measurements"][0]["search_result_count"])
        self.assertEqual(
            {"scoring_elapsed_ms": 2.5, "trace_write_elapsed_ms": 1.25},
            report["items"][0]["measurements"][0]["mcp_search_timing_ms"],
        )
        self.assertEqual(2.5, report["summary"]["mcp_search_timing_summary"]["scoring_elapsed_ms"]["p50"])
        self.assertEqual(
            2.5,
            report["items"][0]["summary"]["mcp_search_timing_summary"]["scoring_elapsed_ms"]["p50"],
        )

    def test_report_records_query_spec_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            query_specs = [{"query": "childcare leave"}]
            query_spec_source = root / "queries.json"
            query_spec_source.write_text(json.dumps(query_specs), encoding="utf-8")
            expected_query_spec_size = query_spec_source.stat().st_size
            expected_query_spec_sha = hashlib.sha256(query_spec_source.read_bytes()).hexdigest()

            with (
                patch("scripts.benchmark_mcp_queries.settings_for_mcp_project", return_value=object()),
                patch("scripts.benchmark_mcp_queries.mcp_auth_context", return_value=object()),
                patch("scripts.benchmark_mcp_queries.warm_mcp_runtime", return_value={"warmed": True}),
                patch(
                    "scripts.benchmark_mcp_queries.search_regulations",
                    return_value={"results": [{"id": "result-1"}], "metadata": {"trace_id": "trace-1"}},
                ),
                patch(
                    "scripts.benchmark_mcp_queries.fetch_regulation",
                    return_value={"id": "result-1", "text": "childcare leave", "metadata": {}},
                ),
            ):
                report = benchmark_mcp_queries(
                    data_dir=root / "data",
                    tenant_id="tenant-demo",
                    query_specs=query_specs,
                    query_spec_source=query_spec_source,
                    iterations=1,
                )

        self.assertEqual(str(query_spec_source), report["query_spec_path"])
        self.assertEqual(1, report["query_spec_item_count"])
        self.assertEqual(expected_query_spec_size, report["query_spec_byte_count"])
        self.assertEqual(expected_query_spec_sha, report["query_spec_sha256"])

    def test_min_warm_records_flags_small_runtime_benchmark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with (
                patch("scripts.benchmark_mcp_queries.settings_for_mcp_project", return_value=object()),
                patch("scripts.benchmark_mcp_queries.mcp_auth_context", return_value=object()),
                patch("scripts.benchmark_mcp_queries.warm_mcp_runtime", return_value={"warmed": True, "record_count": 3}),
                patch(
                    "scripts.benchmark_mcp_queries.search_regulations",
                    return_value={"results": [{"id": "result-1"}], "metadata": {"trace_id": "trace-1"}},
                ),
                patch(
                    "scripts.benchmark_mcp_queries.fetch_regulation",
                    return_value={"id": "result-1", "text": "childcare leave", "metadata": {}},
                ),
            ):
                report = benchmark_mcp_queries(
                    data_dir=root / "data",
                    tenant_id="tenant-demo",
                    queries=["childcare leave"],
                    iterations=1,
                    min_warm_records=5,
                )

        self.assertFalse(report["passed"])
        self.assertEqual(5, report["min_warm_records"])
        self.assertEqual(
            ["benchmark-warm-record-count-below-minimum"],
            [finding["code"] for finding in report["findings"]],
        )
        self.assertEqual(3, report["findings"][0]["actual_record_count"])

    def test_expect_no_evidence_benchmark_passes_when_no_results_are_returned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with (
                patch("scripts.benchmark_mcp_queries.settings_for_mcp_project", return_value=object()),
                patch("scripts.benchmark_mcp_queries.mcp_auth_context", return_value=object()),
                patch("scripts.benchmark_mcp_queries.warm_mcp_runtime", return_value={"warmed": True}),
                patch("scripts.benchmark_mcp_queries.search_regulations", return_value={"results": []}),
            ):
                report = benchmark_mcp_queries(
                    data_dir=root / "data",
                    tenant_id="tenant-demo",
                    query_specs=[{"query": "nonexistent rule", "expect_no_evidence": True}],
                    iterations=1,
                )

        self.assertTrue(report["passed"])
        self.assertTrue(report["items"][0]["expect_no_evidence"])
        self.assertEqual([0], report["items"][0]["search_result_counts"])

    def test_result_findings_flag_expected_no_evidence_with_results(self) -> None:
        findings = _result_findings(
            [
                {
                    "query": "nonexistent rule",
                    "expect_no_evidence": True,
                    "search_result_counts": [0, 2],
                }
            ]
        )

        self.assertEqual(["benchmark-expected-no-evidence-returned-results"], [item["code"] for item in findings])

    def test_threshold_findings_flag_slow_totals_and_warm_search(self) -> None:
        items = [
            {
                "query": "slow query",
                "summary": {
                    "total_elapsed_ms": {"max": 1200.0},
                    "warm_search_elapsed_ms": {"max": 550.0},
                },
            }
        ]

        findings = _threshold_findings(items, max_total_ms=1000.0, max_warm_search_ms=500.0)

        self.assertEqual(
            ["benchmark-total-threshold-exceeded", "benchmark-warm-search-threshold-exceeded"],
            [finding["code"] for finding in findings],
        )

    def test_min_warm_records_requires_warmup_summary(self) -> None:
        findings = _warmup_findings(None, min_warm_records=1)

        self.assertEqual(["benchmark-warmup-required-for-record-threshold"], [item["code"] for item in findings])

    def test_min_warm_records_accepts_lightweight_manifest_record_count(self) -> None:
        findings = _warmup_findings(
            {
                "warmed": False,
                "skipped": True,
                "warmup_mode": "lightweight",
                "record_count": 5000,
                "record_count_available": True,
                "record_count_source": "mcp_runtime_manifest",
                "bm25_index_ready": True,
            },
            min_warm_records=5000,
        )

        self.assertEqual([], findings)

    def test_min_warm_records_rejects_lightweight_without_record_count(self) -> None:
        findings = _warmup_findings(
            {
                "warmed": False,
                "skipped": True,
                "warmup_mode": "lightweight",
                "record_count": None,
                "record_count_available": False,
            },
            min_warm_records=1,
        )

        self.assertEqual(["benchmark-warmup-required-for-record-threshold"], [item["code"] for item in findings])

    def test_stats_reports_empty_and_percentiles(self) -> None:
        self.assertEqual({"count": 0, "min": None, "p50": None, "p95": None, "max": None, "avg": None}, _stats([]))
        self.assertEqual(20.0, _stats([10.0, 20.0, 30.0])["p50"])
        self.assertEqual(30.0, _stats([10.0, 20.0, 30.0])["p95"])


if __name__ == "__main__":
    unittest.main()
