from __future__ import annotations

import hashlib
import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.build_mcp_performance_load_evidence import (
    build_mcp_performance_load_evidence,
    public_mcp_performance_load_evidence,
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


class BuildMcpPerformanceLoadEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.capture_source_state = patch(
            "scripts.build_mcp_performance_load_evidence.capture_mcp_performance_source_state",
            return_value=dict(TEST_SOURCE_STATE),
        ).start()
        self.finalize_source_state = patch(
            "scripts.build_mcp_performance_load_evidence.finalize_mcp_performance_source_state",
            return_value=dict(TEST_SOURCE_STATE),
        ).start()
        self.addCleanup(patch.stopall)

    def test_composes_ready_large_runtime_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            benchmark = _write_json(root / "benchmark.json", _benchmark_payload())
            transport = _write_json(root / "transport.json", _transport_payload())
            visibility = _write_json(root / "visibility.json", _visibility_payload())
            vectors = _write_jsonl(root / "approved_vectors.jsonl", [{"id": "1"}, {"id": "2"}, {"id": "3"}])
            bm25 = _write_json(root / "bm25_index.json", _bm25_payload())
            out_json = root / "out.json"
            out_md = root / "out.md"

            report = build_mcp_performance_load_evidence(
                query_benchmark_report=benchmark,
                transport_smoke_report=transport,
                index_visibility_report=visibility,
                approved_vectors_jsonl=vectors,
                bm25_index_json=bm25,
                min_warm_records=3,
                max_total_p95_ms=200.0,
                max_warm_search_p95_ms=100.0,
                max_transport_warm_search_ms=80.0,
                out_json=out_json,
                out_md=out_md,
            )
            markdown = out_md.read_text(encoding="utf-8")
            json_written = out_json.is_file()

        self.assertTrue(report["passed"])
        self.assertTrue(report["evidence_ready"])
        self.assertFalse(report["performance_release_ready"])
        self.assertTrue(report["latency_slo"]["evaluated"])
        self.assertFalse(report["first_query_release_gate"]["present"])
        self.assertFalse(report["retrieval_quality_release_gate"]["present"])
        self.assertEqual(0, report["finding_count"])
        self.assertEqual(3, report["query_benchmark_summary"]["warm_record_count"])
        self.assertEqual(3, report["file_summary"]["approved_vectors"]["record_count"])
        self.assertEqual(3, report["file_summary"]["bm25_index"]["document_count"])
        self.assertNotIn("first_query_benchmark_summary", report)
        self.assertNotIn("retrieval_quality_summary", report)
        self.assertIn("MCP Performance Load Evidence", markdown)
        self.assertTrue(json_written)

    def test_includes_valid_optional_release_evidence_and_public_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_query = _write_json(
                root / "first_query.json",
                _with_source_state(_with_repo_commit(_first_query_payload())),
            )
            retrieval_quality = _write_json(
                root / "retrieval_quality.json",
                _with_source_state(
                    _with_repo_commit(_retrieval_quality_payload())
                ),
            )
            concurrent_query = _write_json(
                root / "concurrent_query.json",
                _with_source_state(
                    _with_repo_commit(_concurrent_query_payload())
                ),
            )
            expected_first_query_sha256 = hashlib.sha256(
                first_query.read_bytes()
            ).hexdigest()
            out_md = root / "evidence.md"
            out_public_json = root / "evidence.public.json"
            out_public_md = root / "evidence.public.md"

            report = build_mcp_performance_load_evidence(
                query_benchmark_report=_write_json(
                    root / "benchmark.json",
                    _with_source_state(_with_repo_commit(_benchmark_payload())),
                ),
                transport_smoke_report=_write_json(
                    root / "transport.json",
                    _with_source_state(_with_repo_commit(_transport_payload())),
                ),
                index_visibility_report=_write_json(
                    root / "visibility.json",
                    _with_source_state(_with_repo_commit(_visibility_payload())),
                ),
                approved_vectors_jsonl=_write_jsonl(
                    root / "approved_vectors.jsonl",
                    [{"id": "1"}, {"id": "2"}, {"id": "3"}],
                ),
                bm25_index_json=_write_json(
                    root / "bm25_index.json",
                    _bm25_payload(),
                ),
                first_query_benchmark_report=first_query,
                retrieval_quality_report=retrieval_quality,
                concurrent_query_benchmark_report=concurrent_query,
                min_warm_records=3,
                max_total_p95_ms=200.0,
                max_warm_search_p95_ms=100.0,
                max_transport_warm_search_ms=80.0,
                require_repo_commit_consistency=True,
                require_concurrent_query_benchmark=True,
                min_concurrent_query_concurrency=2,
                min_concurrent_query_task_count=4,
                max_concurrent_query_task_total_ms=80.0,
                max_concurrent_query_batch_elapsed_ms=120.0,
                out_md=out_md,
                out_public_json=out_public_json,
                out_public_md=out_public_md,
            )
            markdown = out_md.read_text(encoding="utf-8")
            public_report = json.loads(out_public_json.read_text(encoding="utf-8"))
            public_markdown = out_public_md.read_text(encoding="utf-8")

        self.assertTrue(report["passed"])
        self.assertTrue(report["evidence_ready"])
        self.assertTrue(report["performance_release_ready"])
        self.assertTrue(report["repo_commit_consistency"]["required"])
        self.assertTrue(report["repo_commit_consistency"]["fully_verified"])
        self.assertEqual(TEST_SOURCE_STATE, report["source_state"])
        self.assertTrue(report["source_state_consistency"]["required"])
        self.assertTrue(report["source_state_consistency"]["fully_verified"])
        self.assertTrue(report["first_query_release_gate"]["passed"])
        self.assertTrue(report["retrieval_quality_release_gate"]["passed"])
        self.assertTrue(report["concurrent_query_release_gate"]["passed"])
        first_summary = report["first_query_benchmark_summary"]
        self.assertEqual("loaded", first_summary["source_load_status"])
        self.assertEqual("accepted", first_summary["source_validation_status"])
        self.assertEqual(str(first_query), first_summary["source_path"])
        self.assertEqual(expected_first_query_sha256, first_summary["source_sha256"])
        self.assertEqual(160.0, first_summary["cold_process_wall_p95_ms"])
        self.assertEqual(80.0, first_summary["cold_search_p95_ms"])
        self.assertEqual(35.0, first_summary["warm_search_p95_ms"])
        self.assertEqual(2, first_summary["cold_successful_count"])
        self.assertEqual(2, first_summary["warm_successful_count"])
        quality_summary = report["retrieval_quality_summary"]
        self.assertEqual("loaded", quality_summary["source_load_status"])
        self.assertEqual("accepted", quality_summary["source_validation_status"])
        self.assertEqual(str(retrieval_quality), quality_summary["source_path"])
        self.assertEqual(0.5, quality_summary["recall_at_1"])
        self.assertEqual(1.0, quality_summary["recall_at_5"])
        self.assertEqual(0.75, quality_summary["mrr"])
        self.assertEqual(1.0, quality_summary["document_recall_at_5"])
        self.assertEqual(0.0, quality_summary["no_evidence_false_positive_rate"])
        self.assertEqual(1.0, quality_summary["no_evidence_abstention_rate"])
        concurrent_summary = report["concurrent_query_benchmark_summary"]
        self.assertEqual("accepted", concurrent_summary["source_validation_status"])
        self.assertEqual(2, concurrent_summary["concurrency"])
        self.assertEqual(4, concurrent_summary["task_count"])
        self.assertEqual(0, concurrent_summary["error_count"])
        self.assertEqual(60.0, concurrent_summary["recomputed_task_total_max_ms"])
        self.assertIn("First-query cold process/search p95", markdown)
        self.assertIn("Retrieval Recall@1/3/5", markdown)
        self.assertIn("Concurrent tasks/concurrency", markdown)
        self.assertEqual("public_summary", public_report["report_scope"])
        self.assertEqual(
            {
                "scope": TEST_SOURCE_STATE["scope"],
                "status": "available",
                "file_count": 3,
                "byte_count": 101,
                "stable": True,
            },
            public_report["source_state"],
        )
        self.assertTrue(
            public_report["source_state_consistency"]["fully_verified"]
        )
        self.assertEqual(
            {
                "scope",
                "required",
                "status",
                "passed",
                "consistent",
                "fully_verified",
                "selected_report_count",
                "verified_report_count",
                "legacy_missing_report_count",
                "unavailable_report_count",
                "invalid_report_count",
                "digest_group_count",
            },
            set(public_report["source_state_consistency"]),
        )
        public_keys = _nested_keys(public_report)
        for sensitive_key in (
            "data_dir",
            "department_ids",
            "document_id",
            "document_ids",
            "id",
            "path",
            "profile_id",
            "query",
            "query_id",
            "query_spec_sha256",
            "repo_commit",
            "results",
            "sha256",
            "source_sha256",
            "source_path",
            "source_reports",
            "tenant_id",
            "trace_id",
        ):
            self.assertNotIn(sensitive_key, public_keys)
        self.assertNotIn(str(root), public_markdown)
        self.assertNotIn("tenant-demo", public_markdown)
        serialized_public = json.dumps(public_report, ensure_ascii=False)
        for private_value in (
            "private childcare query",
            "private no evidence query",
            "result-private-1",
            "trace-private-1",
            TEST_SOURCE_STATE["sha256"],
            str(concurrent_query),
        ):
            self.assertNotIn(private_value, serialized_public)
            self.assertNotIn(private_value, public_markdown)

    def test_legacy_missing_repo_commits_remain_composable_but_not_release_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = _build_with_optional_reports(
                root,
                first_payload=_first_query_payload(),
                retrieval_payload=_retrieval_quality_payload(),
            )

        codes = {item["code"] for item in report["findings"]}
        commit_gate = report["repo_commit_consistency"]
        self.assertTrue(report["passed"])
        self.assertTrue(report["evidence_ready"])
        self.assertFalse(report["performance_release_ready"])
        self.assertEqual("compatible_unverified", commit_gate["status"])
        self.assertFalse(commit_gate["fully_verified"])
        self.assertEqual(0, commit_gate["verified_report_count"])
        self.assertNotIn("source-report-repo-commit-unverifiable", codes)

    def test_public_concurrent_summary_redacts_raw_findings_and_identifiers(self) -> None:
        private_report = {
            "report_type": "mcp_performance_load_evidence",
            "repo_commit": TEST_REPO_COMMIT,
            "source_state": TEST_SOURCE_STATE,
            "source_state_consistency": {
                "scope": TEST_SOURCE_STATE["scope"],
                "required": True,
                "status": "verified",
                "passed": True,
                "consistent": True,
                "fully_verified": True,
            },
            "concurrent_query_benchmark_summary": {
                "source_path": "C:/private/concurrent.json",
                "source_sha256": "c" * 64,
                "query": "private raw query",
                "result_id": "private-result-id",
                "trace_id": "private-trace-id",
                "report_type": "mcp_concurrent_query_benchmark",
                "schema_version": 1,
                "task_count": 4,
                "concurrency": 2,
            },
            "finding_count": 1,
            "findings": [
                {
                    "severity": "blocker",
                    "code": "concurrent-query-error",
                    "detail": "raw finding: private raw query",
                    "query": "private raw query",
                    "result_id": "private-result-id",
                }
            ],
        }

        public_report = public_mcp_performance_load_evidence(private_report)
        serialized = json.dumps(public_report, ensure_ascii=False)

        for private_value in (
            TEST_REPO_COMMIT,
            TEST_SOURCE_STATE["sha256"],
            "c" * 64,
            "C:/private/concurrent.json",
            "private raw query",
            "private-result-id",
            "private-trace-id",
            "raw finding:",
        ):
            self.assertNotIn(private_value, serialized)
        self.assertEqual(
            "See the private evidence report for local diagnostic details.",
            public_report["findings"][0]["detail"],
        )

    def test_mismatched_source_repo_commits_block_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = _build_with_report_commits(
                root,
                {
                    "query_benchmark": "a" * 40,
                    "transport_smoke": "a" * 40,
                    "index_visibility": "b" * 40,
                    "first_query_benchmark": "a" * 40,
                    "retrieval_quality": "a" * 40,
                },
            )

        codes = {item["code"] for item in report["findings"]}
        commit_gate = report["repo_commit_consistency"]
        self.assertFalse(report["passed"])
        self.assertFalse(report["evidence_ready"])
        self.assertFalse(report["performance_release_ready"])
        self.assertEqual("mismatch", commit_gate["status"])
        self.assertEqual(2, commit_gate["commit_group_count"])
        self.assertIn("source-report-repo-commit-mismatch", codes)

    def test_required_repo_commit_consistency_rejects_missing_and_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = _build_with_report_commits(
                root,
                {
                    "query_benchmark": TEST_REPO_COMMIT,
                    "transport_smoke": "UNKNOWN",
                    "index_visibility": TEST_REPO_COMMIT,
                    "first_query_benchmark": None,
                    "retrieval_quality": TEST_REPO_COMMIT,
                },
                required=True,
            )

        codes = {item["code"] for item in report["findings"]}
        commit_gate = report["repo_commit_consistency"]
        self.assertFalse(report["passed"])
        self.assertFalse(report["evidence_ready"])
        self.assertEqual("unverifiable_required", commit_gate["status"])
        self.assertEqual(
            ["first_query_benchmark", "transport_smoke"],
            commit_gate["unavailable_report_roles"],
        )
        self.assertIn("source-report-repo-commit-unverifiable", codes)

    def test_mismatched_report_source_states_block_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = _build_with_source_states(
                root,
                {
                    "query_benchmark": _source_state(),
                    "transport_smoke": _source_state(),
                    "index_visibility": _source_state("c" * 64),
                    "first_query_benchmark": _source_state(),
                    "retrieval_quality": _source_state(),
                },
            )

        codes = {item["code"] for item in report["findings"]}
        source_gate = report["source_state_consistency"]
        self.assertFalse(report["passed"])
        self.assertFalse(report["evidence_ready"])
        self.assertFalse(report["performance_release_ready"])
        self.assertEqual("mismatch", source_gate["status"])
        self.assertEqual(2, source_gate["digest_group_count"])
        self.assertIn("source-report-source-state-mismatch", codes)

    def test_builder_source_state_mismatch_blocks_evidence(self) -> None:
        self.finalize_source_state.return_value = _source_state("c" * 64)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = _build_with_source_states(
                root,
                {
                    role: _source_state()
                    for role in (
                        "query_benchmark",
                        "transport_smoke",
                        "index_visibility",
                        "first_query_benchmark",
                        "retrieval_quality",
                    )
                },
            )

        source_gate = report["source_state_consistency"]
        mismatch_finding = next(
            item
            for item in report["findings"]
            if item["code"] == "source-report-source-state-mismatch"
        )
        self.assertFalse(report["evidence_ready"])
        self.assertEqual("mismatch", source_gate["status"])
        self.assertIn(
            "evidence_builder",
            {
                role
                for group in mismatch_finding["digest_groups"]
                for role in group["report_roles"]
            },
        )

    def test_malformed_source_state_blocks_evidence(self) -> None:
        malformed = _source_state()
        malformed["sha256"] = "not-a-sha256"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = _build_with_source_states(
                root,
                {
                    "query_benchmark": _source_state(),
                    "transport_smoke": malformed,
                    "index_visibility": _source_state(),
                    "first_query_benchmark": _source_state(),
                    "retrieval_quality": _source_state(),
                },
            )

        codes = {item["code"] for item in report["findings"]}
        source_gate = report["source_state_consistency"]
        self.assertFalse(report["passed"])
        self.assertEqual("invalid", source_gate["status"])
        self.assertEqual(["transport_smoke"], source_gate["invalid_report_roles"])
        self.assertIn("source-report-source-state-invalid", codes)

    def test_explicit_unavailable_source_state_blocks_evidence(self) -> None:
        unavailable = {
            "scope": TEST_SOURCE_STATE["scope"],
            "status": "changed_during_run",
            "sha256": None,
            "file_count": None,
            "byte_count": None,
            "stable": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = _build_with_source_states(
                root,
                {
                    "query_benchmark": _source_state(),
                    "transport_smoke": unavailable,
                    "index_visibility": _source_state(),
                    "first_query_benchmark": _source_state(),
                    "retrieval_quality": _source_state(),
                },
            )

        codes = {item["code"] for item in report["findings"]}
        source_gate = report["source_state_consistency"]
        self.assertFalse(report["passed"])
        self.assertEqual("source_unavailable", source_gate["status"])
        self.assertEqual(
            ["transport_smoke"],
            source_gate["unavailable_report_roles"],
        )
        self.assertIn("source-report-source-state-unavailable", codes)

    def test_strict_source_state_rejects_legacy_missing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = _build_with_source_states(
                root,
                {
                    "query_benchmark": _source_state(),
                    "transport_smoke": _source_state(),
                    "index_visibility": _source_state(),
                    "first_query_benchmark": None,
                    "retrieval_quality": _source_state(),
                },
                required=True,
            )

        codes = {item["code"] for item in report["findings"]}
        source_gate = report["source_state_consistency"]
        self.assertFalse(report["passed"])
        self.assertFalse(report["evidence_ready"])
        self.assertEqual("unverifiable_required", source_gate["status"])
        self.assertEqual(
            ["first_query_benchmark"],
            source_gate["legacy_missing_report_roles"],
        )
        self.assertIn("source-report-source-state-unverifiable", codes)

    def test_legacy_missing_source_state_is_nonrelease_diagnostic_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = _build_with_source_states(
                root,
                {
                    "query_benchmark": _source_state(),
                    "transport_smoke": _source_state(),
                    "index_visibility": _source_state(),
                    "first_query_benchmark": None,
                    "retrieval_quality": _source_state(),
                },
            )

        codes = {item["code"] for item in report["findings"]}
        source_gate = report["source_state_consistency"]
        self.assertTrue(report["passed"])
        self.assertTrue(report["evidence_ready"])
        self.assertFalse(report["performance_release_ready"])
        self.assertEqual("compatible_unverified", source_gate["status"])
        self.assertFalse(source_gate["fully_verified"])
        self.assertNotIn("source-report-source-state-unverifiable", codes)

    def test_failed_optional_reports_block_evidence_without_copying_source_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_payload = _first_query_payload()
            first_payload.update(
                {
                    "passed": False,
                    "finding_count": 1,
                    "findings": [{"code": "first-query-cold-p95-exceeded"}],
                }
            )
            quality_payload = _retrieval_quality_payload()
            quality_payload.update(
                {
                    "passed": False,
                    "finding_count": 1,
                    "threshold_failure_count": 1,
                    "findings": [
                        {
                            "code": "retrieval-quality-threshold-not-met",
                            "query": "must-not-be-copied",
                        }
                    ],
                }
            )
            report = _build_with_optional_reports(
                root,
                first_payload=first_payload,
                retrieval_payload=quality_payload,
            )

        codes = {item["code"] for item in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertFalse(report["evidence_ready"])
        self.assertIn("first-query-benchmark-failed", codes)
        self.assertIn("retrieval-quality-failed", codes)
        self.assertNotIn("query", report["findings"][-1])
        self.assertEqual(
            ["retrieval-quality-threshold-not-met"],
            report["retrieval_quality_summary"]["finding_codes"],
        )

    def test_query_benchmark_advisory_drift_does_not_create_benchmark_failure_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            benchmark_payload = _benchmark_payload()
            benchmark_payload.update(
                {
                    "passed": False,
                    "finding_count": 1,
                    "findings": [
                        {
                            "code": "benchmark-query-spec-target-missing-from-runtime",
                        }
                    ],
                }
            )
            report = build_mcp_performance_load_evidence(
                query_benchmark_report=_write_json(
                    root / "benchmark.json",
                    benchmark_payload,
                ),
                transport_smoke_report=_write_json(
                    root / "transport.json",
                    _transport_payload(),
                ),
                index_visibility_report=_write_json(
                    root / "visibility.json",
                    _visibility_payload(),
                ),
                approved_vectors_jsonl=_write_jsonl(
                    root / "approved_vectors.jsonl",
                    [{"id": "1"}, {"id": "2"}, {"id": "3"}],
                ),
                bm25_index_json=_write_json(
                    root / "bm25_index.json",
                    _bm25_payload(),
                ),
                min_warm_records=3,
                max_total_p95_ms=200.0,
                max_warm_search_p95_ms=100.0,
                max_transport_warm_search_ms=80.0,
            )

        codes = {item["code"] for item in report["findings"]}
        self.assertTrue(report["passed"])
        self.assertNotIn("query-benchmark-failed", codes)
        self.assertFalse(report["query_benchmark_summary"]["passed"])

    def test_retrieval_quality_advisory_drift_does_not_block_release_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            retrieval_payload = _retrieval_quality_payload()
            retrieval_payload.update(
                {
                    "passed": False,
                    "finding_count": 1,
                    "search_call_count": 2,
                    "query_spec_validation_finding_count": 1,
                    "findings": [
                        {
                            "code": "query-spec-target-missing-from-runtime",
                        }
                    ],
                }
            )
            retrieval_payload["summary"].update(
                {
                    "valid_query_spec_count": 2,
                    "invalid_query_spec_count": 1,
                    "answerable_query_count": 1,
                    "chunk_target_query_count": 1,
                    "document_target_query_count": 1,
                    "no_evidence_query_count": 1,
                }
            )
            report = build_mcp_performance_load_evidence(
                query_benchmark_report=_write_json(
                    root / "benchmark.json",
                    _benchmark_payload(),
                ),
                transport_smoke_report=_write_json(
                    root / "transport.json",
                    _transport_payload(),
                ),
                index_visibility_report=_write_json(
                    root / "visibility.json",
                    _visibility_payload(),
                ),
                approved_vectors_jsonl=_write_jsonl(
                    root / "approved_vectors.jsonl",
                    [{"id": "1"}, {"id": "2"}, {"id": "3"}],
                ),
                bm25_index_json=_write_json(
                    root / "bm25_index.json",
                    _bm25_payload(),
                ),
                retrieval_quality_report=_write_json(
                    root / "retrieval_quality.json",
                    retrieval_payload,
                ),
                min_warm_records=3,
                max_total_p95_ms=200.0,
                max_warm_search_p95_ms=100.0,
                max_transport_warm_search_ms=80.0,
            )

        codes = {item["code"] for item in report["findings"]}
        self.assertTrue(report["passed"])
        self.assertTrue(report["retrieval_quality_release_gate"]["passed"])
        self.assertNotIn("retrieval-quality-failed", codes)
        self.assertFalse(report["retrieval_quality_summary"]["passed"])

    def test_required_optional_release_gates_fail_closed_when_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = build_mcp_performance_load_evidence(
                query_benchmark_report=_write_json(
                    root / "benchmark.json",
                    _benchmark_payload(),
                ),
                transport_smoke_report=_write_json(
                    root / "transport.json",
                    _transport_payload(),
                ),
                index_visibility_report=_write_json(
                    root / "visibility.json",
                    _visibility_payload(),
                ),
                approved_vectors_jsonl=_write_jsonl(
                    root / "approved_vectors.jsonl",
                    [{"id": "1"}, {"id": "2"}, {"id": "3"}],
                ),
                bm25_index_json=_write_json(
                    root / "bm25_index.json",
                    _bm25_payload(),
                ),
                max_total_p95_ms=200.0,
                max_warm_search_p95_ms=100.0,
                max_transport_warm_search_ms=80.0,
                require_first_query_benchmark=True,
                require_retrieval_quality=True,
                require_concurrent_query_benchmark=True,
            )

        codes = {item["code"] for item in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertFalse(report["performance_release_ready"])
        self.assertIn("first-query-benchmark-release-gate-missing", codes)
        self.assertIn("retrieval-quality-release-gate-missing", codes)
        self.assertIn("concurrent-query-benchmark-release-gate-missing", codes)
        self.assertEqual(
            [
                "min_concurrency",
                "min_task_count",
                "max_task_total_ms",
                "max_batch_elapsed_ms",
            ],
            report["concurrent_query_release_gate"]["missing_policy_values"],
        )

    def test_concurrent_report_type_schema_counts_and_results_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = _concurrent_query_payload()
            payload["report_type"] = "mcp_query_benchmark"
            payload["schema_version"] = 2
            payload["summary"]["error_count"] = 1
            payload["measurements"][1]["search_result_count"] = 1
            report = _build_with_concurrent_payload(root, payload)

        codes = {item["code"] for item in report["findings"]}
        structure = next(
            item
            for item in report["findings"]
            if item["code"] == "concurrent-query-benchmark-structure-invalid"
        )
        self.assertFalse(report["passed"])
        self.assertFalse(report["concurrent_query_release_gate"]["passed"])
        self.assertIn("concurrent-query-benchmark-report-type-invalid", codes)
        self.assertIn("concurrent-query-benchmark-schema-invalid", codes)
        self.assertIn("summary.error_count_consistency", structure["issues"])
        self.assertIn(
            "measurements[1].no_evidence_result_count",
            structure["issues"],
        )

    def test_concurrent_non_finite_measurement_and_batch_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = _concurrent_query_payload()
            payload["measurements"][0]["total_elapsed_ms"] = float("nan")
            payload["summary"]["batch_elapsed_ms"] = float("inf")
            report = _build_with_concurrent_payload(root, payload)

        structure = next(
            item
            for item in report["findings"]
            if item["code"] == "concurrent-query-benchmark-structure-invalid"
        )
        self.assertFalse(report["passed"])
        self.assertIn("measurements[0].total_elapsed_ms", structure["issues"])
        self.assertIn("summary.batch_elapsed_ms", structure["issues"])

    def test_concurrent_external_minimums_reject_insufficient_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = _build_with_concurrent_payload(
                root,
                _concurrent_query_payload(),
                min_concurrency=3,
                min_task_count=5,
            )

        codes = {item["code"] for item in report["findings"]}
        self.assertFalse(report["concurrent_query_release_gate"]["passed"])
        self.assertIn("concurrent-query-concurrency-below-policy", codes)
        self.assertIn("concurrent-query-task-count-below-policy", codes)

    def test_concurrent_external_latency_policy_cannot_be_downgraded_by_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = _concurrent_query_payload()
            payload["thresholds"]["max_task_total_ms"] = 1000.0
            payload["thresholds"]["max_batch_elapsed_ms"] = 1000.0
            payload["max_task_total_ms"] = 1000.0
            payload["max_batch_elapsed_ms"] = 1000.0
            report = _build_with_concurrent_payload(
                root,
                payload,
                max_task_total_ms=55.0,
                max_batch_elapsed_ms=90.0,
            )

        codes = {item["code"] for item in report["findings"]}
        self.assertTrue(payload["passed"])
        self.assertFalse(report["passed"])
        self.assertIn("concurrent-query-task-total-exceeds-policy", codes)
        self.assertIn("concurrent-query-batch-elapsed-exceeds-policy", codes)

    def test_concurrent_source_state_mismatch_names_selected_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = _with_repo_commit(_concurrent_query_payload())
            payload["source_state"] = _source_state("c" * 64)
            report = _build_with_concurrent_payload(
                root,
                payload,
                verified_sources=True,
            )

        codes = {item["code"] for item in report["findings"]}
        digest_groups = report["source_state_consistency"]["digest_groups"]
        self.assertIn("source-report-source-state-mismatch", codes)
        self.assertTrue(
            any(
                "concurrent_query_benchmark" in group["report_roles"]
                for group in digest_groups
            )
        )

    def test_concurrent_external_policy_values_must_be_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for kwargs in (
                {"min_concurrent_query_concurrency": 1},
                {"min_concurrent_query_task_count": -1},
                {"max_concurrent_query_task_total_ms": float("nan")},
                {"max_concurrent_query_batch_elapsed_ms": float("inf")},
            ):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    build_mcp_performance_load_evidence(
                        query_benchmark_report=root / "unused.json",
                        transport_smoke_report=root / "unused.json",
                        index_visibility_report=root / "unused.json",
                        approved_vectors_jsonl=root / "unused.jsonl",
                        bm25_index_json=root / "unused-index.json",
                        **kwargs,
                    )

    def test_threshold_free_optional_reports_cannot_claim_release_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_payload = _first_query_payload()
            first_payload["thresholds"]["max_cold_p95_ms"] = None
            retrieval_payload = _retrieval_quality_payload()
            retrieval_payload["thresholds_configured"] = False
            retrieval_payload["thresholds"] = {
                name: None
                for name in retrieval_payload["thresholds"]
            }
            report = _build_with_optional_reports(
                root,
                first_payload=first_payload,
                retrieval_payload=retrieval_payload,
            )

        self.assertTrue(report["passed"])
        self.assertTrue(report["evidence_ready"])
        self.assertFalse(report["performance_release_ready"])
        self.assertFalse(report["first_query_release_gate"]["passed"])
        self.assertFalse(report["retrieval_quality_release_gate"]["passed"])

    def test_optional_report_threshold_claims_are_recomputed_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_payload = _first_query_payload()
            first_payload["thresholds"]["max_cold_p95_ms"] = 100.0
            retrieval_payload = _retrieval_quality_payload()
            retrieval_payload["thresholds"]["min_mrr"] = 0.9
            report = _build_with_optional_reports(
                root,
                first_payload=first_payload,
                retrieval_payload=retrieval_payload,
            )

        codes = {item["code"] for item in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("first-query-benchmark-threshold-inconsistent", codes)
        self.assertIn("retrieval-quality-threshold-inconsistent", codes)

    def test_first_query_result_and_strategy_requirements_are_recomputed_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_payload = _first_query_payload()
            first_payload["summary"]["cold"][
                "answerable_result_count"
            ]["min"] = 0.0
            first_payload["summary"]["warm"]["operational_successful_count"] = 3
            report = _build_with_optional_reports(
                root,
                first_payload=first_payload,
                retrieval_payload=_retrieval_quality_payload(),
            )

        finding = next(
            item
            for item in report["findings"]
            if item["code"] == "first-query-benchmark-threshold-inconsistent"
        )
        self.assertFalse(report["passed"])
        self.assertIn("min_result_count.cold", finding["issues"])
        self.assertIn(
            "qualification_counts.warm",
            finding["issues"],
        )

    def test_first_query_qualification_counts_and_non_finite_results_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_payload = _first_query_payload()
            first_payload["summary"]["cold"][
                "result_requirement_failed_count"
            ] = 1
            first_payload["summary"]["cold"][
                "answerable_result_count"
            ]["min"] = "NaN"
            first_payload["summary"]["warm"]["failed_count"] = 1
            report = _build_with_optional_reports(
                root,
                first_payload=first_payload,
                retrieval_payload=_retrieval_quality_payload(),
            )

        finding = next(
            item
            for item in report["findings"]
            if item["code"] == "first-query-benchmark-threshold-inconsistent"
        )
        self.assertFalse(report["passed"])
        self.assertIn("qualification_counts.cold", finding["issues"])
        self.assertIn("min_result_count.cold", finding["issues"])
        self.assertIn("warm_failed_count", finding["issues"])

    def test_external_first_query_strategy_policy_blocks_report_threshold_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_payload = _first_query_payload()
            first_payload["thresholds"]["required_retrieval_strategy"] = "flat_rag"
            report = build_mcp_performance_load_evidence(
                query_benchmark_report=_write_json(
                    root / "benchmark.json",
                    _benchmark_payload(),
                ),
                transport_smoke_report=_write_json(
                    root / "transport.json",
                    _transport_payload(),
                ),
                index_visibility_report=_write_json(
                    root / "visibility.json",
                    _visibility_payload(),
                ),
                approved_vectors_jsonl=_write_jsonl(
                    root / "approved_vectors.jsonl",
                    [{"id": "1"}, {"id": "2"}, {"id": "3"}],
                ),
                bm25_index_json=_write_json(
                    root / "bm25_index.json",
                    _bm25_payload(),
                ),
                first_query_benchmark_report=_write_json(
                    root / "first_query.json",
                    first_payload,
                ),
                retrieval_quality_report=_write_json(
                    root / "retrieval_quality.json",
                    _retrieval_quality_payload(),
                ),
                min_warm_records=3,
                max_total_p95_ms=200.0,
                max_warm_search_p95_ms=100.0,
                max_transport_warm_search_ms=80.0,
                expected_first_query_retrieval_strategy="catalog_toc_body",
            )

        codes = {item["code"] for item in report["findings"]}
        self.assertFalse(report["performance_release_ready"])
        self.assertIn(
            "first-query-retrieval-strategy-policy-mismatch",
            codes,
        )

    def test_optional_report_type_schema_and_structure_are_validated_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_payload = _first_query_payload()
            first_payload["report_type"] = "mcp_query_benchmark"
            retrieval_payload = _retrieval_quality_payload()
            retrieval_payload["schema_version"] = 2
            retrieval_payload["summary"].pop("recall_at_3")

            report = _build_with_optional_reports(
                root,
                first_payload=first_payload,
                retrieval_payload=retrieval_payload,
            )

        codes = {item["code"] for item in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("first-query-benchmark-report-type-invalid", codes)
        self.assertIn("retrieval-quality-schema-invalid", codes)
        self.assertIn("retrieval-quality-structure-invalid", codes)

    def test_retrieval_quality_invalid_query_specs_are_not_treated_as_structure_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            retrieval_payload = _retrieval_quality_payload()
            retrieval_payload.update(
                {
                    "passed": False,
                    "finding_count": 1,
                    "query_count": 3,
                    "search_call_count": 2,
                    "query_spec_item_count": 3,
                    "query_spec_validation_finding_count": 1,
                    "findings": [
                        {"code": "query-spec-target-missing-from-runtime"}
                    ],
                }
            )
            retrieval_payload["summary"].update(
                {
                    "valid_query_spec_count": 2,
                    "invalid_query_spec_count": 1,
                    "answerable_query_count": 1,
                    "chunk_target_query_count": 1,
                    "document_target_query_count": 1,
                    "no_evidence_query_count": 1,
                }
            )

            report = _build_with_optional_reports(
                root,
                first_payload=_first_query_payload(),
                retrieval_payload=retrieval_payload,
            )

        codes = {item["code"] for item in report["findings"]}
        self.assertNotIn("retrieval-quality-structure-invalid", codes)
        self.assertNotIn("retrieval-quality-failed", codes)
        self.assertTrue(report["retrieval_quality_release_gate"]["passed"])

    def test_optional_non_object_and_invalid_json_reports_block_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_query = root / "first_query.json"
            first_query.write_text("[]\n", encoding="utf-8")
            retrieval_quality = root / "retrieval_quality.json"
            retrieval_quality.write_text("{not-json", encoding="utf-8")
            report = _build_with_optional_report_paths(
                root,
                first_query=first_query,
                retrieval_quality=retrieval_quality,
            )

        codes = {item["code"] for item in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("first-query-benchmark-root-invalid", codes)
        self.assertIn("retrieval-quality-parse-error", codes)
        self.assertEqual(
            "failed",
            report["first_query_benchmark_summary"]["source_load_status"],
        )
        self.assertEqual(
            "blocked",
            report["first_query_benchmark_summary"]["source_validation_status"],
        )
        self.assertEqual(
            "failed",
            report["retrieval_quality_summary"]["source_load_status"],
        )

    def test_first_query_search_call_count_must_match_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_payload = _first_query_payload()
            first_payload["search_call_count"] = 99
            report = _build_with_optional_reports(
                root,
                first_payload=first_payload,
                retrieval_payload=_retrieval_quality_payload(),
            )

        self.assertFalse(report["passed"])
        finding = next(
            item
            for item in report["findings"]
            if item["code"] == "first-query-benchmark-structure-invalid"
        )
        self.assertIn("search_call_count_consistency", finding["issues"])

    def test_record_count_mismatch_blocks_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            benchmark = _write_json(root / "benchmark.json", _benchmark_payload())
            transport = _write_json(root / "transport.json", _transport_payload())
            visibility = _write_json(root / "visibility.json", _visibility_payload())
            vectors = _write_jsonl(root / "approved_vectors.jsonl", [{"id": "1"}, {"id": "2"}])
            bm25 = _write_json(root / "bm25_index.json", _bm25_payload())

            report = build_mcp_performance_load_evidence(
                query_benchmark_report=benchmark,
                transport_smoke_report=transport,
                index_visibility_report=visibility,
                approved_vectors_jsonl=vectors,
                bm25_index_json=bm25,
                min_warm_records=3,
            )

        self.assertFalse(report["passed"])
        self.assertIn("large-runtime-record-count-mismatch", {item["code"] for item in report["findings"]})

    def test_strict_indexed_visibility_requires_provenance_and_all_documents_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            benchmark = _write_json(root / "benchmark.json", _benchmark_payload())
            transport = _write_json(root / "transport.json", _transport_payload())
            vectors = _write_jsonl(
                root / "approved_vectors.jsonl",
                [{"id": "1"}, {"id": "2"}, {"id": "3"}],
            )
            bm25 = _write_json(root / "bm25_index.json", _bm25_payload())

            legacy_visibility = _visibility_payload()
            legacy_visibility["status_counts"] = {"indexed": 1}
            legacy_report = build_mcp_performance_load_evidence(
                query_benchmark_report=benchmark,
                transport_smoke_report=transport,
                index_visibility_report=_write_json(
                    root / "visibility-legacy.json",
                    legacy_visibility,
                ),
                approved_vectors_jsonl=vectors,
                bm25_index_json=bm25,
                min_warm_records=3,
                require_indexed_visibility=True,
            )

            incomplete_visibility = _visibility_payload()
            incomplete_visibility["requirements"] = {"require_indexed": True}
            incomplete_visibility["status_counts"] = {
                "indexed": 0,
                "reindex_required": 1,
            }
            incomplete_report = build_mcp_performance_load_evidence(
                query_benchmark_report=benchmark,
                transport_smoke_report=transport,
                index_visibility_report=_write_json(
                    root / "visibility-incomplete.json",
                    incomplete_visibility,
                ),
                approved_vectors_jsonl=vectors,
                bm25_index_json=bm25,
                min_warm_records=3,
                require_indexed_visibility=True,
            )

            strict_visibility = _visibility_payload()
            strict_visibility["requirements"] = {"require_indexed": True}
            strict_visibility["status_counts"] = {"indexed": 1}
            strict_report = build_mcp_performance_load_evidence(
                query_benchmark_report=benchmark,
                transport_smoke_report=transport,
                index_visibility_report=_write_json(
                    root / "visibility-strict.json",
                    strict_visibility,
                ),
                approved_vectors_jsonl=vectors,
                bm25_index_json=bm25,
                min_warm_records=3,
                require_indexed_visibility=True,
            )

        legacy_codes = {item["code"] for item in legacy_report["findings"]}
        incomplete_codes = {
            item["code"] for item in incomplete_report["findings"]
        }
        self.assertIn(
            "index-visibility-indexed-requirement-missing",
            legacy_codes,
        )
        self.assertNotIn(
            "index-visibility-indexed-status-incomplete",
            legacy_codes,
        )
        self.assertIn(
            "index-visibility-indexed-status-incomplete",
            incomplete_codes,
        )
        self.assertFalse(legacy_report["evidence_ready"])
        self.assertFalse(incomplete_report["evidence_ready"])
        self.assertTrue(strict_report["evidence_ready"])
        self.assertTrue(
            strict_report["thresholds"]["require_indexed_visibility"]
        )
        self.assertEqual(
            1,
            strict_report["index_visibility_summary"][
                "indexed_document_count"
            ],
        )

    def test_latency_thresholds_must_be_finite_and_non_negative(self) -> None:
        required_paths = {
            "query_benchmark_report": Path("benchmark.json"),
            "transport_smoke_report": Path("transport.json"),
            "index_visibility_report": Path("visibility.json"),
            "approved_vectors_jsonl": Path("approved_vectors.jsonl"),
            "bm25_index_json": Path("bm25_index.json"),
        }
        cases = (
            ("max_total_p95_ms", float("nan")),
            ("max_warm_search_p95_ms", float("inf")),
            ("max_transport_warm_search_ms", float("-inf")),
            ("max_total_p95_ms", -1.0),
        )
        for name, value in cases:
            with self.subTest(name=name, value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    f"{name} must be a finite non-negative number",
                ):
                    build_mcp_performance_load_evidence(
                        **required_paths,
                        **{name: value},
                    )

    def test_functional_evidence_without_latency_thresholds_is_not_release_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = build_mcp_performance_load_evidence(
                query_benchmark_report=_write_json(root / "benchmark.json", _benchmark_payload()),
                transport_smoke_report=_write_json(root / "transport.json", _transport_payload()),
                index_visibility_report=_write_json(root / "visibility.json", _visibility_payload()),
                approved_vectors_jsonl=_write_jsonl(
                    root / "approved_vectors.jsonl", [{"id": "1"}, {"id": "2"}, {"id": "3"}]
                ),
                bm25_index_json=_write_json(root / "bm25_index.json", _bm25_payload()),
                min_warm_records=3,
            )

        self.assertTrue(report["evidence_ready"])
        self.assertFalse(report["performance_release_ready"])
        self.assertFalse(report["latency_slo"]["evaluated"])
        self.assertEqual(
            report["latency_slo"]["claim_scope"],
            "functional_evidence_only_no_latency_slo",
        )

    def test_required_latency_slo_fails_closed_when_thresholds_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = build_mcp_performance_load_evidence(
                query_benchmark_report=_write_json(root / "benchmark.json", _benchmark_payload()),
                transport_smoke_report=_write_json(root / "transport.json", _transport_payload()),
                index_visibility_report=_write_json(root / "visibility.json", _visibility_payload()),
                approved_vectors_jsonl=_write_jsonl(
                    root / "approved_vectors.jsonl", [{"id": "1"}, {"id": "2"}, {"id": "3"}]
                ),
                bm25_index_json=_write_json(root / "bm25_index.json", _bm25_payload()),
                min_warm_records=3,
                require_latency_slo=True,
            )

        self.assertFalse(report["passed"])
        self.assertFalse(report["evidence_ready"])
        self.assertFalse(report["performance_release_ready"])
        self.assertIn(
            "latency-slo-thresholds-missing",
            {item["code"] for item in report["findings"]},
        )

    def test_threshold_failures_block_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            benchmark = _write_json(root / "benchmark.json", _benchmark_payload(total_p95=250.0))
            transport = _write_json(root / "transport.json", _transport_payload(warm_search=90.0))
            visibility = _write_json(root / "visibility.json", _visibility_payload(smoke_docs=1))
            vectors = _write_jsonl(root / "approved_vectors.jsonl", [{"id": "1"}, {"id": "2"}, {"id": "3"}])
            bm25 = _write_json(root / "bm25_index.json", _bm25_payload())

            report = build_mcp_performance_load_evidence(
                query_benchmark_report=benchmark,
                transport_smoke_report=transport,
                index_visibility_report=visibility,
                approved_vectors_jsonl=vectors,
                bm25_index_json=bm25,
                min_warm_records=3,
                max_total_p95_ms=200.0,
                max_transport_warm_search_ms=80.0,
            )

        codes = {item["code"] for item in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("query-benchmark-total-p95-too-high", codes)
        self.assertIn("transport-warm-search-too-high", codes)
        self.assertIn("index-visibility-smoke-documents-present", codes)

    def test_warm_total_p95_is_used_for_query_benchmark_latency_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            benchmark = _write_json(
                root / "benchmark.json",
                _benchmark_payload(total_p95=250.0, warm_total_p95=90.0),
            )
            transport = _write_json(root / "transport.json", _transport_payload())
            visibility = _write_json(root / "visibility.json", _visibility_payload())
            vectors = _write_jsonl(root / "approved_vectors.jsonl", [{"id": "1"}, {"id": "2"}, {"id": "3"}])
            bm25 = _write_json(root / "bm25_index.json", _bm25_payload())

            report = build_mcp_performance_load_evidence(
                query_benchmark_report=benchmark,
                transport_smoke_report=transport,
                index_visibility_report=visibility,
                approved_vectors_jsonl=vectors,
                bm25_index_json=bm25,
                min_warm_records=3,
                max_total_p95_ms=200.0,
                max_transport_warm_search_ms=80.0,
            )

        codes = {item["code"] for item in report["findings"]}
        self.assertTrue(report["passed"])
        self.assertEqual(90.0, report["query_benchmark_summary"]["warm_total_p95_ms"])
        self.assertNotIn("query-benchmark-total-p95-too-high", codes)

    def test_configured_slo_blocks_missing_latency_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            benchmark_payload = _benchmark_payload()
            benchmark_payload["summary"]["total_elapsed_ms"].pop("p95")
            benchmark_payload["summary"]["warm_total_elapsed_ms"].pop("p95")
            benchmark_payload["summary"]["warm_search_elapsed_ms"].pop("p95")
            transport_payload = _transport_payload()
            transport_payload["full_profile"].pop("warm_search_elapsed_ms")
            transport_payload["chatgpt_data_profile"].pop("warm_search_elapsed_ms")
            report = build_mcp_performance_load_evidence(
                query_benchmark_report=_write_json(root / "benchmark.json", benchmark_payload),
                transport_smoke_report=_write_json(root / "transport.json", transport_payload),
                index_visibility_report=_write_json(root / "visibility.json", _visibility_payload()),
                approved_vectors_jsonl=_write_jsonl(
                    root / "approved_vectors.jsonl", [{"id": "1"}, {"id": "2"}, {"id": "3"}]
                ),
                bm25_index_json=_write_json(root / "bm25_index.json", _bm25_payload()),
                min_warm_records=3,
                max_total_p95_ms=200.0,
                max_warm_search_p95_ms=100.0,
                max_transport_warm_search_ms=80.0,
                require_latency_slo=True,
            )

        codes = {item["code"] for item in report["findings"]}
        self.assertFalse(report["performance_release_ready"])
        self.assertFalse(report["latency_slo"]["passed"])
        self.assertIn("query-benchmark-total-p95-missing", codes)
        self.assertIn("query-benchmark-warm-search-p95-missing", codes)
        self.assertIn("transport-warm-search-missing", codes)

    def test_cli_writes_outputs_and_can_fail_on_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            benchmark = _write_json(root / "benchmark.json", _benchmark_payload(total_p95=250.0))
            transport = _write_json(root / "transport.json", _transport_payload())
            visibility = _write_json(root / "visibility.json", _visibility_payload())
            vectors = _write_jsonl(root / "approved_vectors.jsonl", [{"id": "1"}, {"id": "2"}, {"id": "3"}])
            bm25 = _write_json(root / "bm25_index.json", _bm25_payload())
            first_query = _write_json(root / "first_query.json", _first_query_payload())
            retrieval_quality = _write_json(
                root / "retrieval_quality.json",
                _retrieval_quality_payload(),
            )
            concurrent_query = _write_json(
                root / "concurrent.json",
                _concurrent_query_payload(),
            )
            out_json = root / "evidence.json"
            out_md = root / "evidence.md"

            stdout = io.StringIO()
            exit_code = run(
                [
                    "--query-benchmark-report",
                    str(benchmark),
                    "--transport-smoke-report",
                    str(transport),
                    "--index-visibility-report",
                    str(visibility),
                    "--approved-vectors-jsonl",
                    str(vectors),
                    "--bm25-index-json",
                    str(bm25),
                    "--first-query-benchmark-report",
                    str(first_query),
                    "--retrieval-quality-report",
                    str(retrieval_quality),
                    "--concurrent-query-benchmark-report",
                    str(concurrent_query),
                    "--require-concurrent-query-benchmark",
                    "--min-concurrent-query-concurrency",
                    "2",
                    "--min-concurrent-query-task-count",
                    "4",
                    "--max-concurrent-query-task-total-ms",
                    "80",
                    "--max-concurrent-query-batch-elapsed-ms",
                    "120",
                    "--max-total-p95-ms",
                    "200",
                    "--require-repo-commit-consistency",
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                    "--fail-on-issue",
                ],
                stdout=stdout,
            )
            json_written = out_json.is_file()
            md_written = out_md.is_file()
            written = json.loads(out_json.read_text(encoding="utf-8"))

        self.assertEqual(2, exit_code)
        self.assertTrue(json_written)
        self.assertTrue(md_written)
        self.assertEqual(
            160.0,
            written["first_query_benchmark_summary"]["cold_process_wall_p95_ms"],
        )
        self.assertEqual(0.75, written["retrieval_quality_summary"]["mrr"])
        self.assertEqual(
            4,
            written["concurrent_query_benchmark_summary"]["task_count"],
        )
        self.assertTrue(written["concurrent_query_release_gate"]["passed"])
        self.assertTrue(written["repo_commit_consistency"]["required"])


def _benchmark_payload(
    *,
    total_p95: float = 120.0,
    warm_total_p95: float | None = None,
) -> dict:
    effective_warm_total_p95 = total_p95 if warm_total_p95 is None else warm_total_p95
    return {
        "report_type": "mcp_query_benchmark",
        "passed": True,
        "query_count": 2,
        "iterations": 2,
        "min_warm_records": 3,
        "finding_count": 0,
        "api_call_count": 0,
        "warmup": {"warmed": True, "record_count": 3, "bm25_index_ready": True},
        "summary": {
            "measurement_count": 4,
            "total_elapsed_ms": {"p50": 100.0, "p95": total_p95, "max": total_p95},
            "warm_total_elapsed_ms": {
                "p50": effective_warm_total_p95,
                "p95": effective_warm_total_p95,
                "max": effective_warm_total_p95,
            },
            "warm_search_elapsed_ms": {"p50": 50.0, "p95": 70.0, "max": 70.0},
        },
        "query_spec_sha256": "query-sha",
    }


def _transport_payload(*, warm_search: float = 40.0) -> dict:
    profile = {
        "passed": True,
        "tool_profile": "full",
        "search_result_count": 3,
        "warm_search_result_count": 3,
        "fetch_has_text": True,
        "list_tools_elapsed_ms": 5.0,
        "search_elapsed_ms": 45.0,
        "warm_search_elapsed_ms": warm_search,
        "fetch_elapsed_ms": 8.0,
        "total_elapsed_ms": 60.0,
    }
    return {
        "report_type": "mcp_transport_smoke",
        "passed": True,
        "tenant_id": "tenant-demo",
        "tenant_storage_isolation": True,
        "transport": "stdio",
        "full_profile": profile,
        "chatgpt_data_profile": dict(profile, tool_profile="chatgpt-data"),
    }


def _visibility_payload(*, smoke_docs: int = 0) -> dict:
    return {
        "report_type": "mcp_index_visibility_audit",
        "passed": True,
        "tenant_id": "tenant-demo",
        "document_count": 1,
        "total_approved_chunks": 3,
        "total_indexable_record_count": 3,
        "total_mcp_visible_records": 3,
        "total_skipped_unapproved_count": 0,
        "smoke_like_document_count": smoke_docs,
        "finding_count": 0,
    }


def _first_query_payload() -> dict:
    return {
        "report_type": "mcp_first_query_benchmark",
        "schema_version": 1,
        "passed": True,
        "finding_count": 0,
        "findings": [],
        "query_count": 1,
        "iterations_per_query": 2,
        "warm_iterations_per_child": 1,
        "api_call_count": 0,
        "search_call_count": 4,
        "thresholds": {
            "effective_min_success_count": 2,
            "min_result_count": 1,
            "required_retrieval_strategy": "catalog_toc_body",
            "max_cold_p95_ms": 200.0,
            "max_warm_p95_ms": 50.0,
        },
        "summary": {
            "measurement_count": 2,
            "process_wall_elapsed_ms": _stats_payload(160.0),
            "successful_process_wall_elapsed_ms": _stats_payload(150.0),
            "cold": {
                "requested_count": 2,
                "answerable_requested_count": 2,
                "no_evidence_requested_count": 0,
                "attempt_count": 2,
                "not_attempted_count": 0,
                "operational_successful_count": 2,
                "successful_count": 2,
                "answerable_successful_count": 2,
                "no_evidence_successful_count": 0,
                "failed_count": 0,
                "result_requirement_failed_count": 0,
                "retrieval_strategy_requirement_failed_count": 0,
                "search_elapsed_ms": _stats_payload(80.0),
                "result_count": _stats_payload(5.0),
                "answerable_result_count": _stats_payload(5.0),
                "no_evidence_result_count": _empty_stats_payload(),
            },
            "warm": {
                "attempt_count": 2,
                "answerable_attempt_count": 2,
                "no_evidence_attempt_count": 0,
                "operational_successful_count": 2,
                "successful_count": 2,
                "answerable_successful_count": 2,
                "no_evidence_successful_count": 0,
                "failed_count": 0,
                "result_requirement_failed_count": 0,
                "retrieval_strategy_requirement_failed_count": 0,
                "search_elapsed_ms": _stats_payload(35.0),
                "result_count": _stats_payload(5.0),
                "answerable_result_count": _stats_payload(5.0),
                "no_evidence_result_count": _empty_stats_payload(),
            },
            "timed_out_count": 0,
            "invalid_protocol_count": 0,
        },
    }


def _retrieval_quality_payload() -> dict:
    return {
        "report_type": "mcp_retrieval_quality",
        "schema_version": 1,
        "passed": True,
        "finding_count": 0,
        "findings": [],
        "threshold_failure_count": 0,
        "search_error_finding_count": 0,
        "query_spec_validation_finding_count": 0,
        "query_count": 3,
        "search_call_count": 3,
        "api_call_count": 0,
        "query_spec_item_count": 3,
        "query_spec_sha256": "a" * 64,
        "thresholds_configured": True,
        "thresholds": {
            "min_recall_at_1": None,
            "min_recall_at_3": None,
            "min_recall_at_5": 0.9,
            "min_mrr": 0.7,
            "min_document_recall_at_1": None,
            "min_document_recall_at_3": None,
            "min_document_recall_at_5": 0.9,
            "max_no_evidence_false_positive_rate": 0.0,
            "min_no_evidence_abstention_rate": 1.0,
        },
        "summary": {
            "valid_query_spec_count": 3,
            "invalid_query_spec_count": 0,
            "answerable_query_count": 2,
            "chunk_target_query_count": 2,
            "document_target_query_count": 2,
            "no_evidence_query_count": 1,
            "search_error_count": 0,
            "recall_at_1": 0.5,
            "recall_at_3": 1.0,
            "recall_at_5": 1.0,
            "mrr": 0.75,
            "mean_reciprocal_rank": 0.75,
            "document_recall_at_1": 0.5,
            "document_recall_at_3": 1.0,
            "document_recall_at_5": 1.0,
            "no_evidence_false_positive_count": 0,
            "no_evidence_false_positive_rate": 0.0,
            "no_evidence_abstention_count": 1,
            "no_evidence_abstention_rate": 1.0,
        },
    }


def _concurrent_query_payload() -> dict:
    measurements = [
        _concurrent_measurement(
            round_index=1,
            query_index=1,
            query="private childcare query",
            expect_no_evidence=False,
            result_count=1,
            search_ms=10.0,
            fetch_ms=5.0,
            single_fetch_ms=4.0,
            total_ms=50.0,
            trace_id="trace-private-1",
        ),
        _concurrent_measurement(
            round_index=1,
            query_index=2,
            query="private no evidence query",
            expect_no_evidence=True,
            result_count=0,
            search_ms=8.0,
            fetch_ms=0.0,
            single_fetch_ms=None,
            total_ms=40.0,
            trace_id="trace-private-2",
        ),
        _concurrent_measurement(
            round_index=2,
            query_index=1,
            query="private childcare query",
            expect_no_evidence=False,
            result_count=1,
            search_ms=12.0,
            fetch_ms=6.0,
            single_fetch_ms=5.0,
            total_ms=60.0,
            trace_id="trace-private-3",
        ),
        _concurrent_measurement(
            round_index=2,
            query_index=2,
            query="private no evidence query",
            expect_no_evidence=True,
            result_count=0,
            search_ms=9.0,
            fetch_ms=0.0,
            single_fetch_ms=None,
            total_ms=45.0,
            trace_id="trace-private-4",
        ),
    ]
    answerable = [item for item in measurements if not item["expect_no_evidence"]]
    no_evidence = [item for item in measurements if item["expect_no_evidence"]]
    return {
        "report_type": "mcp_concurrent_query_benchmark",
        "schema_version": 1,
        "passed": True,
        "finding_count": 0,
        "findings": [],
        "data_dir": "C:/private/runtime",
        "tenant_id": "tenant-demo",
        "profile_id": "profile-private",
        "query_spec_path": "C:/private/queries.json",
        "query_spec_sha256": "d" * 64,
        "query_count": 2,
        "rounds": 2,
        "concurrency": 2,
        "task_count": 4,
        "top_k": 5,
        "api_call_count": 0,
        "min_warm_records": 3,
        "max_task_total_ms": 100.0,
        "max_batch_elapsed_ms": 200.0,
        "thresholds": {
            "min_warm_records": 3,
            "max_task_total_ms": 100.0,
            "max_batch_elapsed_ms": 200.0,
        },
        "thresholds_configured": True,
        "settings_overrides": {
            "api_audit_enabled": False,
            "rag_trace_enabled": False,
        },
        "warmup": {
            "warmed": True,
            "record_count": 3,
            "bm25_index_ready": True,
            "hierarchical_index_ready": False,
            "external_elapsed_ms": 2.0,
            "timing_ms": {"total_elapsed_ms": 1.0},
        },
        "summary": {
            "batch_elapsed_ms": 100.0,
            "measurement_count": 4,
            "successful_count": 4,
            "error_count": 0,
            "answerable_measurement_count": 2,
            "no_evidence_measurement_count": 2,
            "answerable_zero_result_count": 0,
            "no_evidence_nonzero_result_count": 0,
            "answerable_result_count": _concurrent_stats(
                [float(item["search_result_count"]) for item in answerable]
            ),
            "no_evidence_result_count": _concurrent_stats(
                [float(item["search_result_count"]) for item in no_evidence]
            ),
            "search_elapsed_ms": _concurrent_stats(
                [float(item["search_elapsed_ms"]) for item in measurements]
            ),
            "fetch_elapsed_ms": _concurrent_stats(
                [float(item["fetch_elapsed_ms"]) for item in measurements]
            ),
            "single_fetch_elapsed_ms": _concurrent_stats([4.0, 5.0]),
            "answer_elapsed_ms": _concurrent_stats(
                [float(item["answer_elapsed_ms"]) for item in measurements]
            ),
            "total_elapsed_ms": _concurrent_stats(
                [float(item["total_elapsed_ms"]) for item in measurements]
            ),
            "search_result_count_min": 0,
            "fetch_result_count_min": 0,
        },
        "measurements": measurements,
    }


def _concurrent_measurement(
    *,
    round_index: int,
    query_index: int,
    query: str,
    expect_no_evidence: bool,
    result_count: int,
    search_ms: float,
    fetch_ms: float,
    single_fetch_ms: float | None,
    total_ms: float,
    trace_id: str,
) -> dict:
    fetch_measurements = []
    if single_fetch_ms is not None:
        fetch_measurements.append(
            {
                "fetch_index": 1,
                "elapsed_ms": single_fetch_ms,
                "title": "private regulation title",
                "chunk_type": "article",
                "result_id": "result-private-1",
            }
        )
    return {
        "round": round_index,
        "query_index": query_index,
        "query": query,
        "expect_no_evidence": expect_no_evidence,
        "search_result_count": result_count,
        "fetch_result_count": result_count,
        "answer_char_count": 20 if result_count else 0,
        "search_elapsed_ms": search_ms,
        "fetch_elapsed_ms": fetch_ms,
        "fetch_measurements": fetch_measurements,
        "answer_elapsed_ms": 1.0,
        "total_elapsed_ms": total_ms,
        "mcp_search_timing_ms": {"scoring_elapsed_ms": 2.0},
        "trace_id": trace_id,
        "result_id": "result-private-1" if result_count else None,
    }


def _concurrent_stats(values: list[float]) -> dict:
    if not values:
        return {
            "count": 0,
            "min": None,
            "p50": None,
            "p95": None,
            "max": None,
            "avg": None,
        }
    ordered = sorted(values)

    def percentile(value: float) -> float:
        index = min(
            len(ordered) - 1,
            max(0, math.ceil(value * len(ordered)) - 1),
        )
        return ordered[index]

    return {
        "count": len(ordered),
        "min": round(ordered[0], 3),
        "p50": round(percentile(0.5), 3),
        "p95": round(percentile(0.95), 3),
        "max": round(ordered[-1], 3),
        "avg": round(sum(ordered) / len(ordered), 3),
    }


def _stats_payload(p95: float) -> dict:
    return {
        "count": 2,
        "min": p95,
        "p50": p95,
        "p95": p95,
        "p99": p95,
        "max": p95,
        "avg": p95,
    }


def _empty_stats_payload() -> dict:
    return {
        "count": 0,
        "min": None,
        "p50": None,
        "p95": None,
        "p99": None,
        "max": None,
        "avg": None,
    }


def _build_with_concurrent_payload(
    root: Path,
    payload: dict,
    *,
    min_concurrency: int = 2,
    min_task_count: int = 4,
    max_task_total_ms: float = 80.0,
    max_batch_elapsed_ms: float = 120.0,
    verified_sources: bool = False,
) -> dict:
    benchmark_payload = _benchmark_payload()
    transport_payload = _transport_payload()
    visibility_payload = _visibility_payload()
    if verified_sources:
        for source_payload in (
            benchmark_payload,
            transport_payload,
            visibility_payload,
        ):
            _with_source_state(_with_repo_commit(source_payload))
        payload.setdefault("repo_commit", TEST_REPO_COMMIT)
        payload.setdefault("source_state", _source_state())
    return build_mcp_performance_load_evidence(
        query_benchmark_report=_write_json(
            root / "benchmark.json",
            benchmark_payload,
        ),
        transport_smoke_report=_write_json(
            root / "transport.json",
            transport_payload,
        ),
        index_visibility_report=_write_json(
            root / "visibility.json",
            visibility_payload,
        ),
        approved_vectors_jsonl=_write_jsonl(
            root / "approved_vectors.jsonl",
            [{"id": "1"}, {"id": "2"}, {"id": "3"}],
        ),
        bm25_index_json=_write_json(
            root / "bm25_index.json",
            _bm25_payload(),
        ),
        concurrent_query_benchmark_report=_write_json(
            root / "concurrent.json",
            payload,
        ),
        min_warm_records=3,
        max_total_p95_ms=200.0,
        max_warm_search_p95_ms=100.0,
        max_transport_warm_search_ms=80.0,
        require_repo_commit_consistency=verified_sources,
        require_concurrent_query_benchmark=True,
        min_concurrent_query_concurrency=min_concurrency,
        min_concurrent_query_task_count=min_task_count,
        max_concurrent_query_task_total_ms=max_task_total_ms,
        max_concurrent_query_batch_elapsed_ms=max_batch_elapsed_ms,
    )


def _build_with_optional_reports(
    root: Path,
    *,
    first_payload: dict,
    retrieval_payload: dict,
) -> dict:
    return _build_with_optional_report_paths(
        root,
        first_query=_write_json(root / "first_query.json", first_payload),
        retrieval_quality=_write_json(
            root / "retrieval_quality.json",
            retrieval_payload,
        ),
    )


def _build_with_optional_report_paths(
    root: Path,
    *,
    first_query: Path,
    retrieval_quality: Path,
) -> dict:
    return build_mcp_performance_load_evidence(
        query_benchmark_report=_write_json(
            root / "benchmark.json",
            _benchmark_payload(),
        ),
        transport_smoke_report=_write_json(
            root / "transport.json",
            _transport_payload(),
        ),
        index_visibility_report=_write_json(
            root / "visibility.json",
            _visibility_payload(),
        ),
        approved_vectors_jsonl=_write_jsonl(
            root / "approved_vectors.jsonl",
            [{"id": "1"}, {"id": "2"}, {"id": "3"}],
        ),
        bm25_index_json=_write_json(
            root / "bm25_index.json",
            _bm25_payload(),
        ),
        first_query_benchmark_report=first_query,
        retrieval_quality_report=retrieval_quality,
        min_warm_records=3,
        max_total_p95_ms=200.0,
        max_warm_search_p95_ms=100.0,
        max_transport_warm_search_ms=80.0,
    )


def _build_with_report_commits(
    root: Path,
    commits: dict[str, str | None],
    *,
    required: bool = False,
) -> dict:
    payloads = {
        "query_benchmark": _benchmark_payload(),
        "transport_smoke": _transport_payload(),
        "index_visibility": _visibility_payload(),
        "first_query_benchmark": _first_query_payload(),
        "retrieval_quality": _retrieval_quality_payload(),
    }
    for payload in payloads.values():
        payload["source_state"] = _source_state()
    for role, repo_commit in commits.items():
        if repo_commit is not None:
            payloads[role]["repo_commit"] = repo_commit
    return build_mcp_performance_load_evidence(
        query_benchmark_report=_write_json(
            root / "benchmark.json",
            payloads["query_benchmark"],
        ),
        transport_smoke_report=_write_json(
            root / "transport.json",
            payloads["transport_smoke"],
        ),
        index_visibility_report=_write_json(
            root / "visibility.json",
            payloads["index_visibility"],
        ),
        approved_vectors_jsonl=_write_jsonl(
            root / "approved_vectors.jsonl",
            [{"id": "1"}, {"id": "2"}, {"id": "3"}],
        ),
        bm25_index_json=_write_json(
            root / "bm25_index.json",
            _bm25_payload(),
        ),
        first_query_benchmark_report=_write_json(
            root / "first_query.json",
            payloads["first_query_benchmark"],
        ),
        retrieval_quality_report=_write_json(
            root / "retrieval_quality.json",
            payloads["retrieval_quality"],
        ),
        min_warm_records=3,
        max_total_p95_ms=200.0,
        max_warm_search_p95_ms=100.0,
        max_transport_warm_search_ms=80.0,
        require_repo_commit_consistency=required,
    )


def _build_with_source_states(
    root: Path,
    source_states: dict[str, dict | None],
    *,
    required: bool = False,
) -> dict:
    payloads = {
        "query_benchmark": _with_repo_commit(_benchmark_payload()),
        "transport_smoke": _with_repo_commit(_transport_payload()),
        "index_visibility": _with_repo_commit(_visibility_payload()),
        "first_query_benchmark": _with_repo_commit(_first_query_payload()),
        "retrieval_quality": _with_repo_commit(_retrieval_quality_payload()),
    }
    for role, source_state in source_states.items():
        if source_state is not None:
            payloads[role]["source_state"] = dict(source_state)
    return build_mcp_performance_load_evidence(
        query_benchmark_report=_write_json(
            root / "benchmark.json",
            payloads["query_benchmark"],
        ),
        transport_smoke_report=_write_json(
            root / "transport.json",
            payloads["transport_smoke"],
        ),
        index_visibility_report=_write_json(
            root / "visibility.json",
            payloads["index_visibility"],
        ),
        approved_vectors_jsonl=_write_jsonl(
            root / "approved_vectors.jsonl",
            [{"id": "1"}, {"id": "2"}, {"id": "3"}],
        ),
        bm25_index_json=_write_json(
            root / "bm25_index.json",
            _bm25_payload(),
        ),
        first_query_benchmark_report=_write_json(
            root / "first_query.json",
            payloads["first_query_benchmark"],
        ),
        retrieval_quality_report=_write_json(
            root / "retrieval_quality.json",
            payloads["retrieval_quality"],
        ),
        min_warm_records=3,
        max_total_p95_ms=200.0,
        max_warm_search_p95_ms=100.0,
        max_transport_warm_search_ms=80.0,
        require_repo_commit_consistency=required,
    )


def _bm25_payload() -> dict:
    return {
        "index_version": "reg-rag-bm25-index-v1",
        "retrieval_model": "kiwi-bm25-v1",
        "tokenizer": "kiwi-tokenizer-v1",
        "document_count": 3,
        "document_frequencies": {"a": 3},
        "documents": [{"id": "1"}, {"id": "2"}, {"id": "3"}],
    }


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _with_repo_commit(
    payload: dict,
    repo_commit: str = TEST_REPO_COMMIT,
) -> dict:
    payload["repo_commit"] = repo_commit
    return payload


def _source_state(sha256: str = "b" * 64) -> dict:
    return {
        **TEST_SOURCE_STATE,
        "sha256": sha256,
    }


def _with_source_state(
    payload: dict,
    source_state: dict | None = None,
) -> dict:
    payload["source_state"] = dict(source_state or TEST_SOURCE_STATE)
    return payload


def _nested_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {
            str(key)
            for key in value
        } | {
            nested
            for item in value.values()
            for nested in _nested_keys(item)
        }
    if isinstance(value, list):
        return {
            nested
            for item in value
            for nested in _nested_keys(item)
        }
    return set()


if __name__ == "__main__":
    unittest.main()
