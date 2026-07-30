from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.ingestion.vector_adapter import stable_content_hash
from app.retrieval.hierarchical_index import (
    build_hierarchical_runtime_index,
    hierarchical_index_path,
    load_record_by_chunk,
    write_vector_records_with_offsets,
)
from scripts.benchmark_mcp_queries import (
    _result_findings,
    _runtime_record_matches_index,
    _stats,
    _threshold_findings,
    _verified_runtime_target_ids,
    _warmup_findings,
    benchmark_mcp_queries,
)


TEST_SOURCE_STATE = {
    "scope": "mcp-performance-python-source-v1",
    "status": "available",
    "sha256": "b" * 64,
    "file_count": 3,
    "byte_count": 101,
    "stable": True,
}


def _runtime_record(
    *,
    tenant_id: str,
    profile_id: str,
    document_id: str,
    chunk_id: str,
    effective_from: str = "2025-01-01",
    effective_to: str = "",
) -> dict[str, object]:
    text = f"Approved runtime evidence for {chunk_id}."
    metadata = {
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "institution_name": "Synthetic Institution",
        "document_name": f"Synthetic Regulation {document_id}",
        "regulation_no": document_id,
        "regulation_title": f"Synthetic Regulation {document_id}",
        "regulation_status": "approved",
        "regulation_version": "v1",
        "revision_date": effective_from,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "chunk_type": "article",
        "hierarchy_path": f"Synthetic Regulation {document_id} > Article 1",
        "article_no": "Article 1",
        "article_title": "Purpose",
        "approval_status": "approved",
        "security_level": "internal",
        "department_acl": [],
    }
    return {
        "schema_version": "reg-rag-vector-record-v1",
        "id": f"{document_id}:{chunk_id}",
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "document_id": document_id,
        "chunk_id": chunk_id,
        "text": text,
        "metadata": metadata,
        "content_hash": stable_content_hash(text, metadata),
    }


def _write_verified_runtime(
    data_dir: Path,
    *,
    tenant_id: str,
    profile_id: str,
    vector_records: list[dict[str, object]],
    index_records: list[dict[str, object]] | None = None,
) -> None:
    vector_path = (
        data_dir
        / "vector_db"
        / tenant_id
        / "approved_vectors.jsonl"
    )
    offsets = write_vector_records_with_offsets(vector_path, vector_records)
    hierarchy = build_hierarchical_runtime_index(
        hierarchical_index_path(data_dir),
        index_records if index_records is not None else vector_records,
        tenant_id=tenant_id,
        profile_id=profile_id,
        vector_offsets=offsets,
    )
    (data_dir / "mcp_runtime_manifest.json").write_text(
        json.dumps(
            {
                "report_type": "mcp_runtime_data_bundle",
                "tenant_id": tenant_id,
                "profile_id": profile_id,
                "record_count": len(
                    index_records if index_records is not None else vector_records
                ),
                "files": {
                    "hierarchical_index_sha256": hierarchy["sha256"],
                },
            }
        ),
        encoding="utf-8",
    )


class BenchmarkMcpQueriesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.capture_source_state = patch(
            "scripts.benchmark_mcp_queries.capture_mcp_performance_source_state",
            return_value=dict(TEST_SOURCE_STATE),
        ).start()
        self.finalize_source_state = patch(
            "scripts.benchmark_mcp_queries.finalize_mcp_performance_source_state",
            return_value=dict(TEST_SOURCE_STATE),
        ).start()
        self.addCleanup(patch.stopall)

    def test_exports_query_benchmark_with_timing_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_json = root / "benchmark.json"
            out_md = root / "benchmark.md"

            with (
                patch(
                    "scripts.benchmark_mcp_queries.settings_for_mcp_project",
                    return_value=object(),
                ) as settings_factory,
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
                    profile_id="profile-demo",
                    queries=["childcare leave"],
                    iterations=2,
                    max_total_ms=1000.0,
                    max_warm_search_ms=500.0,
                    min_warm_records=1,
                    out_json=out_json,
                    out_md=out_md,
                )
                markdown = out_md.read_text(encoding="utf-8")
                written = json.loads(out_json.read_text(encoding="utf-8"))
                self.assertTrue(out_json.exists())
                self.assertIn("MCP Query Benchmark", markdown)
                self.assertIn("MCP Search Internal Timing", markdown)
                self.assertIn("scoring_elapsed_ms", markdown)

        self.assertTrue(report["passed"])
        self.assertEqual("mcp_query_benchmark", report["report_type"])
        self.assertEqual(TEST_SOURCE_STATE, report["source_state"])
        self.assertEqual(TEST_SOURCE_STATE, written["source_state"])
        self.capture_source_state.assert_called_once()
        self.finalize_source_state.assert_called_once()
        self.assertEqual("profile-demo", report["profile_id"])
        self.assertEqual(
            {"api_audit_enabled": False, "rag_trace_enabled": False},
            report["settings_overrides"],
        )
        self.assertEqual(2, report["summary"]["measurement_count"])
        self.assertEqual(1, report["query_count"])
        self.assertTrue(report["thresholds_configured"])
        self.assertEqual(
            {
                "max_total_ms": 1000.0,
                "max_warm_search_ms": 500.0,
                "min_warm_records": 1,
            },
            report["thresholds"],
        )
        self.assertIn("Total max threshold ms: 1000.0", markdown)
        self.assertIn("Warm search max threshold ms: 500.0", markdown)
        self.assertEqual(2, search_mock.call_count)
        self.assertEqual(2, fetch_mock.call_count)
        self.assertEqual(
            False,
            settings_factory.call_args.kwargs["api_audit_enabled"],
        )
        self.assertEqual(
            False,
            settings_factory.call_args.kwargs["rag_trace_enabled"],
        )
        self.assertTrue(all(call.kwargs["profile_id"] == "profile-demo" for call in search_mock.call_args_list))
        self.assertTrue(all(call.kwargs["profile_id"] == "profile-demo" for call in fetch_mock.call_args_list))
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

    def test_vector_targets_without_runtime_bundle_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vector_dir = root / "data" / "vector_db" / "tenant-demo"
            vector_dir.mkdir(parents=True, exist_ok=True)
            (vector_dir / "approved_vectors.jsonl").write_text(
                json.dumps(
                    {
                        "chunk_id": "missing-chunk",
                        "document_id": "missing-doc",
                        "metadata": {},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with (
                patch(
                    "scripts.benchmark_mcp_queries.settings_for_mcp_project",
                    return_value=SimpleNamespace(data_dir=root / "data"),
                ),
                patch(
                    "scripts.benchmark_mcp_queries.mcp_auth_context",
                    return_value=SimpleNamespace(tenant_id="tenant-demo"),
                ),
                patch("scripts.benchmark_mcp_queries.warm_mcp_runtime", return_value={"warmed": True}),
                patch("scripts.benchmark_mcp_queries.search_regulations") as search_mock,
            ):
                report = benchmark_mcp_queries(
                    data_dir=root / "data",
                    tenant_id="tenant-demo",
                    query_specs=[
                        {
                            "id": "stale-target",
                            "query": "stale benchmark target",
                            "target_chunk_id": "missing-chunk",
                            "target_document_id": "missing-doc",
                        }
                    ],
                    iterations=1,
                )

        self.assertFalse(report["passed"])
        self.assertEqual(0, search_mock.call_count)
        self.assertEqual(
            ["benchmark-query-spec-target-missing-from-runtime"],
            [finding["code"] for finding in report["findings"]],
        )
        self.assertFalse(report["items"][0]["query_spec_valid"])
        self.assertEqual(["missing-chunk"], report["items"][0]["missing_target_chunk_ids"])
        self.assertEqual(["missing-doc"], report["items"][0]["missing_target_document_ids"])

    def test_verified_runtime_targets_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            record = _runtime_record(
                tenant_id="tenant-demo",
                profile_id="profile-demo",
                document_id="verified-doc",
                chunk_id="verified-chunk",
            )
            _write_verified_runtime(
                data_dir,
                tenant_id="tenant-demo",
                profile_id="profile-demo",
                vector_records=[record],
            )

            with (
                patch(
                    "scripts.benchmark_mcp_queries.settings_for_mcp_project",
                    return_value=SimpleNamespace(data_dir=data_dir),
                ),
                patch(
                    "scripts.benchmark_mcp_queries.mcp_auth_context",
                    return_value=SimpleNamespace(tenant_id="tenant-demo"),
                ),
                patch(
                    "scripts.benchmark_mcp_queries.warm_mcp_runtime",
                    return_value={"warmed": True},
                ),
                patch(
                    "scripts.benchmark_mcp_queries.search_regulations",
                    return_value={
                        "results": [{"id": "verified-result"}],
                        "metadata": {},
                    },
                ) as search_mock,
                patch(
                    "scripts.benchmark_mcp_queries.fetch_regulation",
                    return_value={
                        "id": "verified-result",
                        "text": "Approved runtime evidence.",
                        "metadata": {},
                    },
                ),
            ):
                report = benchmark_mcp_queries(
                    data_dir=data_dir,
                    tenant_id="tenant-demo",
                    profile_id="profile-demo",
                    query_specs=[
                        {
                            "id": "verified-target",
                            "query": "verified benchmark target",
                            "target_chunk_id": "verified-chunk",
                            "target_document_id": "verified-doc",
                        }
                    ],
                    iterations=1,
                )

        self.assertTrue(report["passed"])
        self.assertEqual(1, search_mock.call_count)
        self.assertTrue(report["items"][0]["query_spec_valid"])

    def test_same_length_top_level_tenant_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            canonical = _runtime_record(
                tenant_id="tenant-demo",
                profile_id="profile-demo",
                document_id="tenant-bound-doc",
                chunk_id="tenant-bound-chunk",
            )
            tampered = dict(canonical)
            tampered["tenant_id"] = "tenant-evil"
            self.assertEqual(
                len(str(canonical["tenant_id"])),
                len(str(tampered["tenant_id"])),
            )
            _write_verified_runtime(
                data_dir,
                tenant_id="tenant-demo",
                profile_id="profile-demo",
                vector_records=[tampered],
                index_records=[canonical],
            )
            offset_verified_record = load_record_by_chunk(
                hierarchical_index_path(data_dir),
                data_dir
                / "vector_db"
                / "tenant-demo"
                / "approved_vectors.jsonl",
                document_id="tenant-bound-doc",
                chunk_id="tenant-bound-chunk",
            )
            self.assertIsNotNone(offset_verified_record)
            self.assertEqual(
                "tenant-evil",
                offset_verified_record["tenant_id"],
            )

            found = _verified_runtime_target_ids(
                settings=SimpleNamespace(data_dir=data_dir),
                auth=SimpleNamespace(tenant_id="tenant-demo"),
                profile_id="profile-demo",
                requested_chunk_ids={"tenant-bound-chunk"},
                requested_document_ids={"tenant-bound-doc"},
                as_of_dates={None},
            )

        self.assertEqual({None: (set(), set())}, found)

    def test_runtime_record_scope_requires_resolved_tenant_and_profile(self) -> None:
        metadata_only = {
            "document_id": "scope-doc",
            "chunk_id": "scope-chunk",
            "content_hash": "scope-hash",
            "metadata": {
                "tenant_id": "tenant-demo",
                "profile_id": "Profile-Demo",
            },
        }
        self.assertTrue(
            _runtime_record_matches_index(
                metadata_only,
                document_id="scope-doc",
                chunk_id="scope-chunk",
                content_hash="scope-hash",
                expected_tenant_id="tenant-demo",
                expected_profile_id="profile-demo",
            )
        )
        for missing_field in ("tenant_id", "profile_id"):
            incomplete = {
                **metadata_only,
                "metadata": {
                    key: value
                    for key, value in metadata_only["metadata"].items()
                    if key != missing_field
                },
            }
            self.assertFalse(
                _runtime_record_matches_index(
                    incomplete,
                    document_id="scope-doc",
                    chunk_id="scope-chunk",
                    content_hash="scope-hash",
                    expected_tenant_id="tenant-demo",
                    expected_profile_id="profile-demo",
                )
            )

    def test_runtime_target_validation_honors_profile_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            record = _runtime_record(
                tenant_id="tenant-demo",
                profile_id="profile-other",
                document_id="shared-doc",
                chunk_id="shared-chunk",
            )
            _write_verified_runtime(
                data_dir,
                tenant_id="tenant-demo",
                profile_id="profile-other",
                vector_records=[record],
            )

            with (
                patch(
                    "scripts.benchmark_mcp_queries.settings_for_mcp_project",
                    return_value=SimpleNamespace(data_dir=data_dir),
                ),
                patch(
                    "scripts.benchmark_mcp_queries.mcp_auth_context",
                    return_value=SimpleNamespace(tenant_id="tenant-demo"),
                ),
                patch("scripts.benchmark_mcp_queries.warm_mcp_runtime", return_value={"warmed": True}),
                patch("scripts.benchmark_mcp_queries.search_regulations") as search_mock,
            ):
                report = benchmark_mcp_queries(
                    data_dir=data_dir,
                    tenant_id="tenant-demo",
                    profile_id="profile-selected",
                    query_specs=[
                        {
                            "id": "wrong-profile-target",
                            "query": "profile scoped benchmark target",
                            "target_chunk_id": "shared-chunk",
                            "target_document_id": "shared-doc",
                        }
                    ],
                    iterations=1,
                )

        self.assertFalse(report["passed"])
        self.assertEqual(0, search_mock.call_count)
        self.assertEqual(["shared-chunk"], report["items"][0]["missing_target_chunk_ids"])
        self.assertEqual(["shared-doc"], report["items"][0]["missing_target_document_ids"])

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
