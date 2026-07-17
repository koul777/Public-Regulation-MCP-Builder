from __future__ import annotations

import io
import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.build_mcp_handoff_report import build_mcp_handoff_report, main
from scripts.mcp_bundle_contract import REQUIRED_SETUP_BUNDLE_FILES


class BuildMcpHandoffReportTests(unittest.TestCase):
    def test_ready_artifacts_build_local_claude_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            demo = root / "demo.json"
            readiness = root / "readiness.json"
            visibility = root / "visibility.json"
            benchmark = root / "benchmark.json"
            bundle = root / "bundle"
            _seed_bundle(bundle, server_name="aks-regulation-mcp")
            _write_json(product, _product_payload())
            _write_json(demo, _demo_payload())
            _write_json(readiness, _readiness_payload())
            _write_json(visibility, _visibility_payload())
            _write_json(benchmark, _benchmark_payload())
            out_json = root / "handoff.json"
            out_md = root / "handoff.md"

            report = build_mcp_handoff_report(
                product_readiness_report=product,
                mcp_demo_answer_report=demo,
                mcp_readiness_report=readiness,
                mcp_index_visibility_report=visibility,
                mcp_query_benchmark_report=benchmark,
                bundle_dir=bundle,
                server_name="aks-regulation-mcp",
                out_json=out_json,
                out_md=out_md,
            )
            self.assertTrue(out_json.is_file())
            markdown = out_md.read_text(encoding="utf-8")
            self.assertIn("Claude Desktop Steps", markdown)
            self.assertIn("MCP-visible records: 5997", markdown)
            self.assertIn("Parser evidence in MCP-visible index: HWPX docs 2 / HWP mode docs 1 / HWP geometry-review docs 1", markdown)
            self.assertIn("Parser uncertainty in MCP-visible index: risks {'medium': 12, 'high': 5} / flags {'hwp_table_geometry_uncertain': 5, 'hwpx_nested_table': 12}", markdown)
            self.assertIn("Approval provenance coverage: complete 5997 of 5997", markdown)
            self.assertIn("Approval journal coverage: matched 5997 of 5997 / missing 0 / journal records 5997", markdown)
            self.assertIn("Approval journal review events: incomplete records 0", markdown)
            self.assertIn("MCP query benchmark: `true`", markdown)
            self.assertIn("warm records 5997 of min 5000", markdown)
            self.assertIn("Approval workload: manual attention 69 (5.65%) / low-risk human batch candidates 1153 (94.35%)", markdown)
            self.assertIn("Reapproval workload: candidates 5997 / initial human review 419 / reduction ratio 0.9301", markdown)
            self.assertIn("Reapproval approval provenance gaps: 197 / provenance-only 50", markdown)
            self.assertIn("Reapproval review batches: 60 batches / 5997 chunks / selected 5997 of 5997", markdown)

        self.assertTrue(report["handoff_ready"])
        self.assertEqual(2, report["handoff_schema_version"])
        self.assertEqual("ready_for_local_claude_desktop_mvp", report["decision"])
        self.assertEqual(0, report["blocking_count"])
        self.assertEqual(0, report["warning_count"])
        self.assertEqual(5997, report["product_summary"]["repository_chunk_count"])
        self.assertEqual(
            0,
            report["product_summary"]["approval_journal_review_event_coverage"]["incomplete_record_count"],
        )
        self.assertEqual(
            {"ai_review_confirmed": 0, "approved": 0, "human_review_confirmed": 0},
            report["product_summary"]["approval_journal_review_event_coverage"]["missing_event_chunk_counts"],
        )
        self.assertEqual(69, report["product_summary"]["approval_workload"]["manual_attention_chunks"])
        self.assertEqual(1153, report["product_summary"]["approval_workload"]["low_risk_batch_review_candidate_chunks"])
        self.assertEqual(18, report["product_summary"]["approval_review_batch"]["batch_count"])
        self.assertEqual(1222, report["product_summary"]["approval_review_batch"]["approval_chunk_count"])
        self.assertEqual(419, report["product_summary"]["reapproval_workload"]["recommended_initial_review_chunks"])
        self.assertEqual(0.9301, report["product_summary"]["reapproval_workload"]["initial_review_reduction_ratio"])
        self.assertEqual(197, report["product_summary"]["reapproval_workload"]["approval_provenance_missing_chunks"])
        self.assertEqual(50, report["product_summary"]["reapproval_workload"]["approval_provenance_only_chunks"])
        self.assertEqual(
            {"approval_worklist_report_path": 197},
            report["product_summary"]["reapproval_workload"]["approval_provenance_missing_field_counts"],
        )
        self.assertEqual(60, report["product_summary"]["reapproval_review_batch"]["batch_count"])
        self.assertEqual(5997, report["product_summary"]["reapproval_review_batch"]["selected_candidate_count"])
        self.assertEqual(5997, report["product_summary"]["reapproval_review_batch"]["reapproval_chunk_count"])
        self.assertEqual(
            {"high": 5997},
            report["product_summary"]["reapproval_review_batch"]["risk_tier_chunk_counts"],
        )
        self.assertTrue(report["bundle_summary"]["claude_desktop_config_valid"])
        self.assertEqual(1, report["demo_summary"]["expected_term_query_count"])
        self.assertEqual([0.5], report["demo_summary"]["expected_term_hit_ratios"])
        self.assertEqual(0.5, report["demo_summary"]["expected_term_min_hit_ratio"])
        self.assertEqual(0.5, report["demo_summary"]["expected_term_average_hit_ratio"])
        self.assertEqual(1, report["demo_summary"]["expected_term_partial_hit_count"])
        self.assertEqual(0, report["demo_summary"]["expected_term_low_hit_count"])
        self.assertEqual(1, report["demo_summary"]["expected_article_no_query_count"])
        self.assertEqual([1.0], report["demo_summary"]["expected_article_no_hit_ratios"])
        self.assertEqual(1.0, report["demo_summary"]["expected_article_no_min_hit_ratio"])
        self.assertEqual(1, report["demo_summary"]["expected_article_title_query_count"])
        self.assertEqual([1.0], report["demo_summary"]["expected_article_title_hit_ratios"])
        self.assertEqual(1.0, report["demo_summary"]["expected_article_title_min_hit_ratio"])
        self.assertEqual(5997, report["mcp_index_visibility_summary"]["total_mcp_visible_records"])
        self.assertEqual(0, report["mcp_index_visibility_summary"]["smoke_like_document_count"])
        self.assertEqual(2, report["mcp_index_visibility_summary"]["parser_evidence_summary"]["hwpx_evidence_document_count"])
        self.assertEqual({"medium": 12, "high": 5}, report["mcp_index_visibility_summary"]["parser_uncertainty_summary"]["risk_level_counts"])
        self.assertEqual(5997, report["mcp_index_visibility_summary"]["approval_provenance_coverage"]["complete_record_count"])
        self.assertEqual(5997, report["mcp_index_visibility_summary"]["approval_journal_coverage"]["matched_record_count"])
        self.assertEqual(0, report["mcp_index_visibility_summary"]["approval_journal_coverage"]["missing_record_count"])
        self.assertEqual(369.061, report["mcp_query_benchmark_summary"]["total_p95_ms"])
        self.assertEqual(238.07, report["mcp_query_benchmark_summary"]["warm_search_p95_ms"])
        self.assertEqual(5997, report["mcp_query_benchmark_summary"]["warm_record_count"])
        self.assertEqual(5000, report["mcp_query_benchmark_summary"]["min_warm_records"])
        steps = {step["name"]: step for step in report["operator_steps"]}
        self.assertIn("validate_claude_desktop_config", steps)
        self.assertIn("-ValidateClaudeDesktop", steps["validate_claude_desktop_config"]["command"])
        self.assertIn("-InstallClaudeDesktop", steps["merge_claude_desktop_config"]["command"])

    def test_missing_approval_journal_coverage_blocks_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            demo = root / "demo.json"
            readiness = root / "readiness.json"
            visibility = root / "visibility.json"
            bundle = root / "bundle"
            visibility_payload = _visibility_payload()
            visibility_payload.pop("approval_journal_coverage")
            _seed_bundle(bundle, server_name="aks-regulation-mcp")
            _write_json(product, _product_payload())
            _write_json(demo, _demo_payload())
            _write_json(readiness, _readiness_payload())
            _write_json(visibility, visibility_payload)

            report = build_mcp_handoff_report(
                product_readiness_report=product,
                mcp_demo_answer_report=demo,
                mcp_readiness_report=readiness,
                mcp_index_visibility_report=visibility,
                bundle_dir=bundle,
                server_name="aks-regulation-mcp",
            )

        self.assertFalse(report["passed"])
        self.assertFalse(report["handoff_ready"])
        self.assertIn("approval-journal-coverage-missing", {item["code"] for item in report["findings"]})

    def test_missing_approval_journal_records_block_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            demo = root / "demo.json"
            readiness = root / "readiness.json"
            visibility = root / "visibility.json"
            bundle = root / "bundle"
            visibility_payload = _visibility_payload()
            visibility_payload["approval_journal_coverage"]["matched_record_count"] = 5996
            visibility_payload["approval_journal_coverage"]["missing_record_count"] = 1
            _seed_bundle(bundle, server_name="aks-regulation-mcp")
            _write_json(product, _product_payload())
            _write_json(demo, _demo_payload())
            _write_json(readiness, _readiness_payload())
            _write_json(visibility, visibility_payload)

            report = build_mcp_handoff_report(
                product_readiness_report=product,
                mcp_demo_answer_report=demo,
                mcp_readiness_report=readiness,
                mcp_index_visibility_report=visibility,
                bundle_dir=bundle,
                server_name="aks-regulation-mcp",
            )

        self.assertFalse(report["passed"])
        self.assertFalse(report["handoff_ready"])
        self.assertIn("approval-journal-records-missing", {item["code"] for item in report["findings"]})

    def test_incomplete_approval_review_events_block_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            demo = root / "demo.json"
            readiness = root / "readiness.json"
            visibility = root / "visibility.json"
            bundle = root / "bundle"
            product_payload = _product_payload()
            coverage = product_payload["runtime_summary"]["approval_journal_review_event_coverage"]
            coverage["event_chunk_counts"]["approved"] = 5996
            coverage["missing_event_chunk_counts"]["approved"] = 1
            coverage["incomplete_record_count"] = 1
            _seed_bundle(bundle, server_name="aks-regulation-mcp")
            _write_json(product, product_payload)
            _write_json(demo, _demo_payload())
            _write_json(readiness, _readiness_payload())
            _write_json(visibility, _visibility_payload())

            report = build_mcp_handoff_report(
                product_readiness_report=product,
                mcp_demo_answer_report=demo,
                mcp_readiness_report=readiness,
                mcp_index_visibility_report=visibility,
                bundle_dir=bundle,
                server_name="aks-regulation-mcp",
            )

        self.assertFalse(report["passed"])
        self.assertFalse(report["handoff_ready"])
        self.assertIn("approval-journal-review-events-incomplete", {item["code"] for item in report["findings"]})

    def test_partial_approval_review_event_keys_block_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            demo = root / "demo.json"
            readiness = root / "readiness.json"
            visibility = root / "visibility.json"
            bundle = root / "bundle"
            product_payload = _product_payload()
            coverage = product_payload["runtime_summary"]["approval_journal_review_event_coverage"]
            coverage["event_chunk_counts"] = {"approved": 5997}
            coverage["missing_event_chunk_counts"] = {"approved": 0}
            coverage["incomplete_record_count"] = 0
            _seed_bundle(bundle, server_name="aks-regulation-mcp")
            _write_json(product, product_payload)
            _write_json(demo, _demo_payload())
            _write_json(readiness, _readiness_payload())
            _write_json(visibility, _visibility_payload())

            report = build_mcp_handoff_report(
                product_readiness_report=product,
                mcp_demo_answer_report=demo,
                mcp_readiness_report=readiness,
                mcp_index_visibility_report=visibility,
                bundle_dir=bundle,
                server_name="aks-regulation-mcp",
            )

        self.assertFalse(report["passed"])
        self.assertIn("approval-journal-review-events-incomplete", {item["code"] for item in report["findings"]})
        self.assertEqual(
            ["ai_review_confirmed", "human_review_confirmed"],
            report["product_summary"]["approval_journal_review_event_coverage"]["missing_required_event_types"],
        )

    def test_malformed_approval_review_event_counts_block_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            demo = root / "demo.json"
            readiness = root / "readiness.json"
            visibility = root / "visibility.json"
            bundle = root / "bundle"
            product_payload = _product_payload()
            coverage = product_payload["runtime_summary"]["approval_journal_review_event_coverage"]
            coverage["incomplete_record_count"] = "not-a-count"
            coverage["missing_event_chunk_counts"]["approved"] = "bad"
            _seed_bundle(bundle, server_name="aks-regulation-mcp")
            _write_json(product, product_payload)
            _write_json(demo, _demo_payload())
            _write_json(readiness, _readiness_payload())
            _write_json(visibility, _visibility_payload())

            report = build_mcp_handoff_report(
                product_readiness_report=product,
                mcp_demo_answer_report=demo,
                mcp_readiness_report=readiness,
                mcp_index_visibility_report=visibility,
                bundle_dir=bundle,
                server_name="aks-regulation-mcp",
            )

        self.assertFalse(report["passed"])
        self.assertIn("approval-journal-review-events-malformed", {item["code"] for item in report["findings"]})
        malformed = report["product_summary"]["approval_journal_review_event_coverage"]["malformed_count_fields"]
        self.assertIn("incomplete_record_count", malformed)
        self.assertIn("missing_event_chunk_counts.approved", malformed)

    def test_missing_mcp_servers_blocks_claude_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            demo = root / "demo.json"
            readiness = root / "readiness.json"
            bundle = root / "bundle"
            _seed_bundle(bundle, server_name="aks-regulation-mcp", claude_config={"notMcpServers": {}})
            _write_json(product, _product_payload())
            _write_json(demo, _demo_payload())
            _write_json(readiness, _readiness_payload())

            report = build_mcp_handoff_report(
                product_readiness_report=product,
                mcp_demo_answer_report=demo,
                mcp_readiness_report=readiness,
                bundle_dir=bundle,
                server_name="aks-regulation-mcp",
            )

        self.assertFalse(report["handoff_ready"])
        self.assertFalse(report["passed"])
        self.assertIn("claude-desktop-config-invalid", {item["code"] for item in report["findings"]})

    def test_product_warning_keeps_report_from_handoff_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            demo = root / "demo.json"
            readiness = root / "readiness.json"
            bundle = root / "bundle"
            payload = _product_payload()
            payload["warning_count"] = 1
            payload["warning_codes"] = ["rag-quality-warning-chunks"]
            _seed_bundle(bundle, server_name="aks-regulation-mcp")
            _write_json(product, payload)
            _write_json(demo, _demo_payload())
            _write_json(readiness, _readiness_payload())

            report = build_mcp_handoff_report(
                product_readiness_report=product,
                mcp_demo_answer_report=demo,
                mcp_readiness_report=readiness,
                bundle_dir=bundle,
                server_name="aks-regulation-mcp",
            )

        self.assertTrue(report["passed"])
        self.assertFalse(report["handoff_ready"])
        self.assertEqual(1, report["warning_count"])
        self.assertIn("product-readiness-warnings", {item["code"] for item in report["findings"]})

    def test_blocked_product_gate_blocks_handoff_even_when_top_level_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            demo = root / "demo.json"
            readiness = root / "readiness.json"
            bundle = root / "bundle"
            payload = _product_payload()
            payload["gates"]["answer_accuracy"] = {"status": "blocked", "blocker_count": 1, "warning_count": 0}
            _seed_bundle(bundle, server_name="aks-regulation-mcp")
            _write_json(product, payload)
            _write_json(demo, _demo_payload())
            _write_json(readiness, _readiness_payload())

            report = build_mcp_handoff_report(
                product_readiness_report=product,
                mcp_demo_answer_report=demo,
                mcp_readiness_report=readiness,
                bundle_dir=bundle,
                server_name="aks-regulation-mcp",
            )

        self.assertFalse(report["passed"])
        self.assertFalse(report["handoff_ready"])
        self.assertEqual(1, report["blocking_count"])
        self.assertIn("product-gate-blocked", {item["code"] for item in report["findings"]})
        self.assertEqual(1, report["product_summary"]["gates"]["answer_accuracy"]["blocker_count"])

    def test_wrong_demo_report_type_blocks_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            demo = root / "demo_bundle.json"
            readiness = root / "readiness.json"
            bundle = root / "bundle"
            _seed_bundle(bundle, server_name="aks-regulation-mcp")
            _write_json(product, _product_payload())
            _write_json(
                demo,
                {
                    "report_type": "mcp_answer_evidence_bundle",
                    "passed": True,
                    "bundle_ready": True,
                    "query_count": 5,
                    "quality_issue_count": 0,
                },
            )
            _write_json(readiness, _readiness_payload())

            report = build_mcp_handoff_report(
                product_readiness_report=product,
                mcp_demo_answer_report=demo,
                mcp_readiness_report=readiness,
                bundle_dir=bundle,
                server_name="aks-regulation-mcp",
            )

        self.assertFalse(report["passed"])
        self.assertFalse(report["handoff_ready"])
        self.assertEqual("mcp_answer_evidence_bundle", report["demo_summary"]["report_type"])
        self.assertIn("demo-report-type-mismatch", {item["code"] for item in report["findings"]})

    def test_zero_query_demo_report_blocks_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            demo = root / "demo.json"
            readiness = root / "readiness.json"
            bundle = root / "bundle"
            payload = _demo_payload()
            payload["query_count"] = 0
            payload["items"] = []
            _seed_bundle(bundle, server_name="aks-regulation-mcp")
            _write_json(product, _product_payload())
            _write_json(demo, payload)
            _write_json(readiness, _readiness_payload())

            report = build_mcp_handoff_report(
                product_readiness_report=product,
                mcp_demo_answer_report=demo,
                mcp_readiness_report=readiness,
                bundle_dir=bundle,
                server_name="aks-regulation-mcp",
            )

        self.assertFalse(report["passed"])
        self.assertFalse(report["handoff_ready"])
        self.assertIn("demo-answer-query-count-zero", {item["code"] for item in report["findings"]})

    def test_failed_query_benchmark_adds_handoff_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            demo = root / "demo.json"
            readiness = root / "readiness.json"
            benchmark = root / "benchmark.json"
            bundle = root / "bundle"
            payload = _benchmark_payload()
            payload["passed"] = False
            payload["finding_count"] = 1
            payload["findings"] = [{"code": "benchmark-total-threshold-exceeded"}]
            _seed_bundle(bundle, server_name="aks-regulation-mcp")
            _write_json(product, _product_payload())
            _write_json(demo, _demo_payload())
            _write_json(readiness, _readiness_payload())
            _write_json(benchmark, payload)

            report = build_mcp_handoff_report(
                product_readiness_report=product,
                mcp_demo_answer_report=demo,
                mcp_readiness_report=readiness,
                mcp_query_benchmark_report=benchmark,
                bundle_dir=bundle,
                server_name="aks-regulation-mcp",
            )

        self.assertTrue(report["passed"])
        self.assertFalse(report["handoff_ready"])
        self.assertEqual(1, report["warning_count"])
        self.assertIn("mcp-query-benchmark-failed", {item["code"] for item in report["findings"]})

    def test_incomplete_bundle_blocks_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            demo = root / "demo.json"
            readiness = root / "readiness.json"
            bundle = root / "bundle"
            _seed_bundle(bundle, server_name="aks-regulation-mcp")
            bundle.joinpath("run_http_server.ps1").unlink()
            _write_json(product, _product_payload())
            _write_json(demo, _demo_payload())
            _write_json(readiness, _readiness_payload())

            report = build_mcp_handoff_report(
                product_readiness_report=product,
                mcp_demo_answer_report=demo,
                mcp_readiness_report=readiness,
                bundle_dir=bundle,
                server_name="aks-regulation-mcp",
            )

        self.assertFalse(report["passed"])
        self.assertFalse(report["handoff_ready"])
        self.assertIn("bundle-files-missing", {item["code"] for item in report["findings"]})
        self.assertIn("run_http_server.ps1", report["bundle_summary"]["missing_files"])

    def test_bundle_runtime_manifest_must_match_product_readiness_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            demo = root / "demo.json"
            readiness = root / "readiness.json"
            bundle = root / "bundle"
            _seed_bundle(bundle, server_name="aks-regulation-mcp")
            bundle_data = bundle / "data"
            bundle_data.mkdir()
            _write_json(
                bundle_data / "mcp_runtime_manifest.json",
                {
                    "tenant_id": "default",
                    "tenant_storage_isolation": False,
                    "document_ids": ["doc-other"],
                    "record_count": 176,
                    "chunk_count": 176,
                },
            )
            _write_json(product, _product_payload())
            _write_json(demo, _demo_payload())
            _write_json(readiness, _readiness_payload())

            report = build_mcp_handoff_report(
                product_readiness_report=product,
                mcp_demo_answer_report=demo,
                mcp_readiness_report=readiness,
                bundle_dir=bundle,
                server_name="aks-regulation-mcp",
            )

        codes = {item["code"] for item in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("bundle-product-tenant-mismatch", codes)
        self.assertIn("bundle-product-record-count-mismatch", codes)
        self.assertEqual("default", report["bundle_summary"]["runtime_manifest"]["tenant_id"])

    def test_authority_manifest_records_upstream_digests_for_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            demo = root / "demo.json"
            readiness = root / "readiness.json"
            authority = root / "authority.json"
            bundle = root / "bundle"
            _seed_bundle(bundle, server_name="aks-regulation-mcp")
            product_payload = _product_payload()
            product_payload["repo_commit"] = "a" * 40
            readiness_payload = _readiness_payload()
            readiness_payload["repo_commit"] = "b" * 40
            _write_json(product, product_payload)
            _write_json(demo, _demo_payload())
            _write_json(readiness, readiness_payload)
            product_sha = hashlib.sha256(product.read_bytes()).hexdigest()
            _write_json(authority, _authority_payload(product_sha=product_sha))

            report = build_mcp_handoff_report(
                product_readiness_report=product,
                mcp_demo_answer_report=demo,
                mcp_readiness_report=readiness,
                authority_manifest=authority,
                bundle_dir=bundle,
                server_name="aks-regulation-mcp",
            )

        self.assertTrue(report["handoff_ready"])
        self.assertEqual(0, report["blocking_count"])
        self.assertEqual(4, len(report["source_report_artifacts"]))
        by_role = {artifact["role"]: artifact for artifact in report["source_report_artifacts"]}
        self.assertEqual(product_sha, by_role["product_readiness_report"]["sha256"])
        self.assertEqual("a" * 40, by_role["product_readiness_report"]["repo_commit"])
        self.assertEqual("b" * 40, by_role["mcp_readiness_report"]["repo_commit"])
        self.assertEqual("mcp_readiness_authority", report["authority_summary"]["report_type"])
        self.assertEqual(1, report["authority_summary"]["authoritative_artifact_count"])

    def test_authority_manifest_blocks_superseded_product_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            demo = root / "demo.json"
            authority = root / "authority.json"
            _write_json(product, _product_payload())
            _write_json(demo, _demo_payload())
            product_sha = hashlib.sha256(product.read_bytes()).hexdigest()
            _write_json(authority, _authority_payload(product_sha=product_sha, superseded=True))

            report = build_mcp_handoff_report(
                product_readiness_report=product,
                mcp_demo_answer_report=demo,
                authority_manifest=authority,
            )

        self.assertFalse(report["passed"])
        self.assertIn("product-readiness-superseded", {item["code"] for item in report["findings"]})

    def test_cli_fail_on_issue_returns_nonzero_for_warning_only_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            demo = root / "demo.json"
            out_json = root / "handoff.json"
            _write_json(product, _product_payload())
            _write_json(demo, _demo_payload())

            argv = [
                "build_mcp_handoff_report.py",
                "--product-readiness-report",
                str(product),
                "--mcp-demo-answer-report",
                str(demo),
                "--out-json",
                str(out_json),
                "--fail-on-issue",
            ]
            with patch("sys.argv", argv), redirect_stdout(io.StringIO()):
                exit_code = main()
            payload = json.loads(out_json.read_text(encoding="utf-8"))

        self.assertEqual(2, exit_code)
        self.assertTrue(payload["passed"])
        self.assertFalse(payload["handoff_ready"])
        self.assertIn("mcp-readiness-report-missing", {item["code"] for item in payload["findings"]})


def _seed_bundle(bundle: Path, *, server_name: str, claude_config: dict | None = None) -> None:
    bundle.mkdir(parents=True)
    _write_json(bundle / "manifest.json", {"server_name": server_name, "ready": {"chatgpt": False, "claude_api": False}})
    _write_json(
        bundle / "claude_desktop_config.json",
        claude_config if claude_config is not None else {"mcpServers": {server_name: {"command": "reg-rag-mcp-server", "args": []}}},
    )
    for name in sorted(REQUIRED_SETUP_BUNDLE_FILES - {"manifest.json", "claude_desktop_config.json"}):
        content = "{}\n" if name.endswith(".json") else "# test\n"
        bundle.joinpath(name).write_text(content, encoding="utf-8")


def _product_payload() -> dict:
    return {
        "report_type": "mcp_product_readiness",
        "passed": True,
        "blocking_count": 0,
        "warning_count": 0,
        "blocking_codes": [],
        "warning_codes": [],
        "api_call_count": 0,
        "tenant_id": "tenant-aks-publish",
        "effective_runtime_data_dir": "data/aks_mcp_publish_runtime/tenants/tenant-aks-publish",
        "runtime_summary": {
            "repository_chunk_count": 5997,
            "vector_record_count": 5997,
            "full_index_match": True,
            "approval_journal_review_event_coverage": _review_event_coverage_payload(),
            "article_like_count": 3047,
            "appendix_count": 549,
            "supplementary_count": 1397,
        },
        "public_readiness_summary": {
            "input_count": 64,
            "successful_count": 64,
            "failed_count": 0,
        },
        "rag_eval_summary": {
            "answerable_ratio": 1.0,
            "quality_warning_chunk_count": 0,
        },
        "approval_workload_summary": {
            "report_count": 1,
            "document_count": 5,
            "total_chunks": 1222,
            "manual_attention_chunks": 69,
            "manual_attention_rate": 5.65,
            "low_risk_batch_review_candidate_chunks": 1153,
            "low_risk_batch_review_candidate_rate": 94.35,
            "blocking_review_chunks": 47,
            "domain_attention_chunks": 22,
        },
        "approval_review_batch_summary": {
            "report_count": 1,
            "batch_count": 18,
            "approval_chunk_count": 1222,
            "manual_attention_chunks": 69,
            "low_risk_batch_review_candidate_chunks": 1153,
            "blocker_count": 0,
            "warning_count": 0,
        },
        "reapproval_workload_summary": {
            "report_count": 1,
            "document_count": 199,
            "reapproval_candidate_chunks": 5997,
            "recommended_initial_review_chunks": 419,
            "estimated_initial_review_minutes": 140,
            "approval_provenance_missing_chunks": 197,
            "approval_provenance_only_chunks": 50,
            "approval_provenance_missing_field_counts": {"approval_worklist_report_path": 197},
            "source_vector_integrity_failure_count": 0,
            "pre_reapproval_blocker_count": 0,
            "initial_review_reduction_ratio": 0.9301,
        },
        "reapproval_review_batch_summary": {
            "report_count": 1,
            "candidate_count": 5997,
            "selected_candidate_count": 5997,
            "batch_count": 60,
            "reapproval_chunk_count": 5997,
            "blocker_count": 0,
            "warning_count": 0,
            "risk_tier_chunk_counts": {"high": 5997},
            "action_chunk_counts": {"reprocess_then_reapprove_and_reindex": 5997},
        },
        "gates": {
            "parsing_accuracy": {"status": "ready", "blocker_count": 0, "warning_count": 0},
            "revision_response": {"status": "ready", "blocker_count": 0, "warning_count": 0},
            "generality": {"status": "ready", "blocker_count": 0, "warning_count": 0},
            "answer_accuracy": {"status": "ready", "blocker_count": 0, "warning_count": 0},
            "operations": {"status": "ready", "blocker_count": 0, "warning_count": 0},
        },
    }


def _review_event_coverage_payload() -> dict:
    return {
        "journal_record_count": 5997,
        "applicable_record_count": 5997,
        "chunk_reference_count": 5997,
        "review_decision_event_count": 17991,
        "expected_event_chunk_counts": {
            "ai_review_confirmed": 5997,
            "approved": 5997,
            "human_review_confirmed": 5997,
        },
        "event_chunk_counts": {
            "ai_review_confirmed": 5997,
            "approved": 5997,
            "human_review_confirmed": 5997,
        },
        "missing_event_chunk_counts": {
            "ai_review_confirmed": 0,
            "approved": 0,
            "human_review_confirmed": 0,
        },
        "incomplete_record_count": 0,
    }


def _demo_payload() -> dict:
    return {
        "report_type": "mcp_demo_answers",
        "passed": True,
        "query_count": 1,
        "quality_issue_count": 0,
        "api_call_count": 0,
        "items": [
            {
                "query": "leave procedure?",
                "passed": True,
                "supporting_result_count": 2,
                "expected_terms": ["leave", "procedure"],
                "expected_term_hits": ["leave"],
                "expected_term_hit_ratio": 0.5,
                "expected_article_nos": ["제10조"],
                "expected_article_no_hits": ["제10조"],
                "expected_article_no_hit_ratio": 1.0,
                "expected_article_titles": ["육아휴직"],
                "expected_article_title_hits": ["육아휴직"],
                "expected_article_title_hit_ratio": 1.0,
                "citations": [
                    {
                        "document_id": "doc-aks",
                        "chunk_id": "chunk-29",
                        "article_no": "Article 29",
                        "article_title": "Leave",
                    }
                ],
            }
        ],
    }


def _readiness_payload() -> dict:
    return {
        "report_type": "mcp_connection_readiness",
        "passed": True,
        "deploy_ready": True,
        "high_count": 0,
        "medium_count": 0,
        "finding_count": 0,
        "client_profile": "bundle",
        "connection_mode": "local_stdio",
        "transport": "stdio",
        "allow_local_only_bundle": True,
    }


def _visibility_payload() -> dict:
    return {
        "report_type": "mcp_index_visibility_audit",
        "passed": True,
        "effective_data_dir": "data/aks_mcp_publish_runtime/tenants/tenant-aks-publish",
        "document_count": 1,
        "total_approved_chunks": 5997,
        "total_mcp_visible_records": 5997,
        "total_skipped_unapproved_count": 0,
        "status_counts": {"indexed": 1},
        "smoke_like_document_count": 0,
        "parser_evidence_summary": {
            "hwpx_evidence_document_count": 2,
            "hwp_extraction_mode_document_count": 1,
            "hwp_native_table_geometry_review_document_count": 1,
            "hwpx_metadata_counts": {
                "source_hwpx_xml_block_indices": 22,
                "source_hwpx_nested_table_text_snippets": 3,
            },
            "hwp_metadata_counts": {
                "source_hwp_extraction_modes": 10,
                "source_hwp_native_table_geometry_false": 10,
            },
        },
        "parser_uncertainty_summary": {
            "record_count": 5997,
            "parser_uncertainty_record_count": 17,
            "missing_parser_uncertainty_count": 5980,
            "risk_level_counts": {"medium": 12, "high": 5},
            "flag_counts": {"hwp_table_geometry_uncertain": 5, "hwpx_nested_table": 12},
        },
        "approval_provenance_coverage": {
            "record_count": 5997,
            "field_counts": {
                "approval_id": 5997,
                "approved_content_hash": 5997,
                "approval_worklist_report_sha256": 5997,
                "approval_review_batch_manifest_sha256": 5997,
                "approval_review_batch_id": 5997,
                "approval_review_batch_chunk_fingerprint": 5997,
                "approval_review_strategy": 5997,
            },
            "missing_field_counts": {
                "approval_id": 0,
                "approved_content_hash": 0,
                "approval_worklist_report_sha256": 0,
                "approval_review_batch_manifest_sha256": 0,
                "approval_review_batch_id": 0,
                "approval_review_batch_chunk_fingerprint": 0,
                "approval_review_strategy": 0,
            },
            "complete_record_count": 5997,
        },
        "approval_journal_coverage": {
            "journal_record_count": 5997,
            "record_count": 5997,
            "eligible_record_count": 5997,
            "matched_record_count": 5997,
            "missing_record_count": 0,
        },
        "finding_count": 0,
        "findings": [],
    }


def _benchmark_payload() -> dict:
    return {
        "report_type": "mcp_query_benchmark",
        "passed": True,
        "query_count": 5,
        "iterations": 3,
        "min_warm_records": 5000,
        "finding_count": 0,
        "api_call_count": 0,
        "warmup": {
            "warmed": True,
            "record_count": 5997,
        },
        "summary": {
            "measurement_count": 15,
            "total_elapsed_ms": {"p50": 288.952, "p95": 369.061, "max": 369.061},
            "warm_search_elapsed_ms": {"p50": 153.807, "p95": 238.07, "max": 238.07},
        },
    }


def _authority_payload(*, product_sha: str, superseded: bool = False) -> dict:
    authoritative = [] if superseded else [{"role": "product_readiness", "sha256": product_sha}]
    supersedes = [{"path": "product.json", "sha256": product_sha, "reason": "replaced"}] if superseded else []
    return {
        "report_type": "mcp_readiness_authority",
        "authority_version": 1,
        "passed": True,
        "blocking_count": 0,
        "warning_count": 0,
        "finding_count": 0,
        "authoritative_artifacts": authoritative,
        "supersedes": supersedes,
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
