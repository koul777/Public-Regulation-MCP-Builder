from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_mcp_readiness_remediation_plan import build_mcp_readiness_remediation_plan, main


class BuildMcpReadinessRemediationPlanTests(unittest.TestCase):
    def test_maps_product_readiness_warnings_to_operator_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "mcp_product_readiness.json"
            reapproval = root / "reapproval_worklist.json"
            verification = root / "evidence_verification.json"
            strict_public_readiness = root / "strict_public_readiness.json"
            _write_json(product, _product_payload())
            _write_json(
                reapproval,
                {
                    "report_type": "reapproval_worklist",
                    "generated_at": "2026-07-09T13:00:00+00:00",
                    "repo_commit": "a" * 40,
                    "review_triage_counts": {"high": 80, "low": 20},
                },
            )
            _write_json(
                verification,
                {
                    "report_type": "release_evidence_bundle_verification",
                    "generated_at": "2026-07-09T13:05:00+00:00",
                    "repo_commit": "a" * 40,
                    "passed": True,
                    "warning_count": 2,
                    "warnings": [
                        {
                            "check": "json_artifact_release_blocker_count",
                            "artifact_path": "reports/reapproval_review_burden_current.json",
                            "release_blocker_count": 1,
                            "release_gate_status": "blocked_pending_operator_decisions",
                        },
                        {
                            "check": "index_repo_worktree_dirty",
                            "tracked_change_count": 3,
                            "untracked_change_count": 2,
                        }
                    ],
                },
            )
            _write_json(strict_public_readiness, _strict_public_readiness_payload())

            report = build_mcp_readiness_remediation_plan(
                product_readiness_report=product,
                reapproval_worklist_report=reapproval,
                evidence_verification_report=verification,
                strict_public_readiness_report=strict_public_readiness,
            )

        self.assertEqual("mcp_readiness_remediation_plan", report["report_type"])
        self.assertTrue(report["passed"])
        self.assertEqual("ready_for_human_remediation", report["plan_status"])
        self.assertEqual([], report["plan_blockers"])
        self.assertEqual(3, report["remediation_item_count"])
        self.assertEqual(
            {1: "parser_release_evidence", 2: "temporal_metadata_review", 3: "runtime_reapproval_and_reindex"},
            {item["priority"]: item["item_id"] for item in report["remediation_items"]},
        )
        by_id = {item["item_id"]: item for item in report["remediation_items"]}
        parser_counts = by_id["parser_release_evidence"]["source_counts"]
        self.assertEqual("review_tolerance", parser_counts["readiness_profile"])
        self.assertFalse(parser_counts["strict_release_evidence"])
        self.assertEqual(86, parser_counts["thresholds"]["max_recommendations"])
        self.assertEqual(
            ["failed_info_checks_within_limit", "recommendations_within_limit"],
            parser_counts["strict_candidate"]["failed_checks"],
        )
        self.assertEqual(7, parser_counts["strict_candidate"]["failed_info_check_total"])
        self.assertEqual(86, parser_counts["strict_candidate"]["recommendation_total"])
        temporal_counts = by_id["temporal_metadata_review"]["source_counts"]
        self.assertEqual(33, temporal_counts["shadow_conflict_chunk_count"])
        self.assertEqual(44, temporal_counts["shadow_ambiguous_chunk_count"])
        self.assertEqual(0, temporal_counts["temporal_ambiguity_blocking_decision_count"])
        self.assertEqual(0, temporal_counts["revision_impact_approval_required_count"])
        self.assertEqual(
            100,
            by_id["runtime_reapproval_and_reindex"]["source_counts"]["approval_provenance_record_count"],
        )
        self.assertEqual(
            {"approval_review_batch_manifest_path": 100},
            by_id["runtime_reapproval_and_reindex"]["source_counts"]["approval_provenance_missing_field_counts"],
        )
        self.assertEqual(80, by_id["runtime_reapproval_and_reindex"]["source_counts"]["recommended_initial_review_chunks"])
        self.assertEqual(True, report["evidence_verification_status"]["dirty_worktree"])
        self.assertEqual(1, report["evidence_verification_status"]["release_blocker_count"])
        self.assertEqual("blocked", report["release_gate_status"])
        self.assertEqual(
            [
                "product-readiness-warnings-present",
                "evidence-generated-from-dirty-worktree",
                "evidence-release-blockers-present",
            ],
            [item["code"] for item in report["release_gate_blockers"]],
        )
        self.assertTrue(report["source_consistency_status"]["consistent"])
        self.assertEqual(4, len(report["source_report_artifacts"]))
        self.assertEqual([], report["unmapped_warning_codes"])

    def test_approval_journal_gap_maps_to_runtime_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "mcp_product_readiness.json"
            _write_json(
                product,
                {
                    "report_type": "mcp_product_readiness",
                    "generated_at": "2026-07-09T13:00:00+00:00",
                    "repo_commit": "a" * 40,
                    "passed": False,
                    "blocking_count": 1,
                    "warning_count": 0,
                    "blocking_codes": ["approval-journal-vector-evidence-missing"],
                    "warning_codes": [],
                    "runtime_summary": {
                        "approval_journal_coverage": {
                            "journal_record_count": 8,
                            "record_count": 10,
                            "eligible_record_count": 10,
                            "matched_record_count": 8,
                            "missing_record_count": 2,
                        }
                    },
                },
            )

            report = build_mcp_readiness_remediation_plan(product_readiness_report=product)

        self.assertEqual(1, report["remediation_item_count"])
        self.assertEqual([], report["unmapped_warning_codes"])
        item = report["remediation_items"][0]
        self.assertEqual("runtime_reapproval_and_reindex", item["item_id"])
        self.assertEqual(["approval-journal-vector-evidence-missing"], item["warning_codes"])
        self.assertEqual(10, item["source_counts"]["approval_journal_eligible_record_count"])
        self.assertEqual(8, item["source_counts"]["approval_journal_matched_record_count"])
        self.assertEqual(2, item["source_counts"]["approval_journal_missing_record_count"])
        self.assertIn("append-only approval journal records", item["operator_inputs_required"])
        self.assertIn("approval journal coverage complete", item["verification_after_remediation"])

    def test_approval_review_event_gap_maps_to_runtime_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "mcp_product_readiness.json"
            _write_json(
                product,
                {
                    "report_type": "mcp_product_readiness",
                    "generated_at": "2026-07-09T13:00:00+00:00",
                    "repo_commit": "a" * 40,
                    "passed": False,
                    "blocking_count": 1,
                    "warning_count": 0,
                    "blocking_codes": ["approval-journal-review-events-incomplete"],
                    "warning_codes": [],
                    "runtime_summary": {
                        "approval_journal_coverage": {
                            "journal_record_count": 1,
                            "record_count": 208,
                            "eligible_record_count": 208,
                            "matched_record_count": 208,
                            "missing_record_count": 0,
                        },
                        "approval_journal_review_event_coverage": {
                            "review_decision_event_count": 100,
                            "expected_event_chunk_counts": {
                                "ai_review_confirmed": 208,
                                "approved": 208,
                                "human_review_confirmed": 208,
                            },
                            "event_chunk_counts": {
                                "ai_review_confirmed": 34,
                                "approved": 33,
                                "human_review_confirmed": 33,
                            },
                            "missing_event_chunk_counts": {
                                "ai_review_confirmed": 174,
                                "approved": 175,
                                "human_review_confirmed": 175,
                            },
                            "incomplete_record_count": 1,
                        },
                    },
                },
            )

            report = build_mcp_readiness_remediation_plan(product_readiness_report=product)

        self.assertEqual(1, report["remediation_item_count"])
        item = report["remediation_items"][0]
        self.assertEqual("runtime_reapproval_and_reindex", item["item_id"])
        self.assertEqual(["approval-journal-review-events-incomplete"], item["warning_codes"])
        counts = item["source_counts"]
        self.assertEqual(100, counts["approval_journal_review_event_count"])
        self.assertEqual(175, counts["approval_journal_review_event_missing_chunk_counts"]["approved"])
        self.assertIn(
            "complete per-chunk AI/human/approval review decision events",
            item["operator_inputs_required"],
        )
        self.assertIn(
            "approval journal review-event coverage complete",
            item["verification_after_remediation"],
        )
        self.assertIn("Do not silently edit an old approval journal", item["recommended_action"])

    def test_parser_goldset_blockers_map_to_label_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "mcp_product_readiness.json"
            _write_json(
                product,
                {
                    "report_type": "mcp_product_readiness",
                    "generated_at": "2026-07-09T13:00:00+00:00",
                    "repo_commit": "a" * 40,
                    "passed": False,
                    "blocking_count": 3,
                    "warning_count": 0,
                    "blocking_codes": [
                        "parser-goldset-quality-claim-not-ready",
                        "parser-goldset-score-issues",
                        "parser-goldset-f1-missing",
                    ],
                    "warning_codes": [],
                    "parser_goldset_score_summary": {
                        "document_count": 12,
                        "pending_document_count": 12,
                        "issue_count": 52,
                        "overall_f1": None,
                    },
                    "parser_goldset_completion_board_summary": {
                        "expected_structure_score_rows": 84,
                        "completed_structure_score_rows": 44,
                        "missing_structure_score_rows": 40,
                        "missing_matched_field_count": 40,
                        "completion_gate_status": "blocked_pending_human_labels",
                    },
                },
            )

            report = build_mcp_readiness_remediation_plan(product_readiness_report=product)

        self.assertEqual(1, report["remediation_item_count"])
        item = report["remediation_items"][0]
        self.assertEqual("parser_goldset_label_completion", item["item_id"])
        self.assertEqual(
            [
                "parser-goldset-quality-claim-not-ready",
                "parser-goldset-score-issues",
                "parser-goldset-f1-missing",
            ],
            item["warning_codes"],
        )
        counts = item["source_counts"]
        self.assertEqual(12, counts["score_pending_document_count"])
        self.assertEqual(40, counts["completion_missing_matched_field_count"])
        self.assertIn("completed parsing goldset label CSV", item["operator_inputs_required"])

    def test_parser_goldset_warnings_map_without_unmapped_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "mcp_product_readiness.json"
            payload = _product_payload()
            payload["warning_codes"] = ["parser-goldset-score-issues"]
            payload["warning_count"] = 1
            payload["parser_goldset_score_summary"] = {
                "document_count": 12,
                "pending_document_count": 0,
                "issue_count": 2,
                "overall_f1": 94.0,
            }
            _write_json(product, payload)

            report = build_mcp_readiness_remediation_plan(product_readiness_report=product)

        self.assertEqual([], report["unmapped_warning_codes"])
        self.assertEqual(1, report["remediation_item_count"])
        item = report["remediation_items"][0]
        self.assertEqual("parser_goldset_label_completion", item["item_id"])
        self.assertEqual(["parser-goldset-score-issues"], item["warning_codes"])

    def test_complete_goldset_labels_map_low_f1_to_parser_accuracy_investigation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "mcp_product_readiness.json"
            payload = _product_payload()
            payload["warning_codes"] = []
            payload["warning_count"] = 0
            payload["blocking_codes"] = ["parser-goldset-f1-low"]
            payload["blocking_count"] = 1
            payload["parser_goldset_score_summary"] = {
                "document_count": 12,
                "pending_document_count": 0,
                "issue_count": 0,
                "overall_f1": 61.06,
            }
            payload["parser_goldset_completion_board_summary"] = {
                "expected_structure_score_rows": 84,
                "completed_structure_score_rows": 84,
                "missing_structure_score_rows": 0,
                "missing_matched_field_count": 0,
                "completion_gate_status": "ready_for_quality_claim",
            }
            _write_json(product, payload)

            report = build_mcp_readiness_remediation_plan(product_readiness_report=product)

        item = report["remediation_items"][0]
        self.assertTrue(item["source_counts"]["labels_complete"])
        self.assertIn("current goldset labels are complete", item["summary"])
        self.assertIn("false positives and false negatives", item["recommended_action"])
        self.assertNotIn("Fill the remaining human matched-count fields", item["recommended_action"])
        self.assertIn("structure-level parser error analysis", item["operator_inputs_required"])

    def test_table_preprocessing_blocker_maps_to_human_review_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "mcp_product_readiness.json"
            _write_json(
                product,
                {
                    "report_type": "mcp_product_readiness",
                    "generated_at": "2026-07-09T13:00:00+00:00",
                    "repo_commit": "a" * 40,
                    "passed": False,
                    "blocking_count": 1,
                    "warning_count": 0,
                    "blocking_codes": ["table-preprocessing-claim-not-ready"],
                    "warning_codes": [],
                    "table_preprocessing_claim_gate_summary": {
                        "status": "blocked_pending_human_review",
                        "claim_level": "review_ready_not_accuracy_proven",
                        "feasibility_status": "feasible_with_human_review",
                        "selected_unit_count": 255,
                        "completed_unit_count": 0,
                        "pending_unit_count": 255,
                        "invalid_unit_count": 0,
                        "ready_for_table_score_transfer": False,
                        "transfer_passed": False,
                        "transfer_blocker_count": 25,
                        "required_field_missing_total": 2040,
                        "required_field_missing_counts": {
                            "human_source_pages_checked": 255,
                            "human_unit_status": 255,
                        },
                        "review_priority_counts": {
                            "source_table_compare": 144,
                            "structured_spot_check": 81,
                        },
                        "label_review_flag_counts": {"missing_table_label": 60},
                        "source_traceability_passed": True,
                        "source_traceability_issue_count": 0,
                        "source_traceability_require_page_count_verification": True,
                        "drift_check_present": True,
                        "drift_check_passed": True,
                        "drift_check_blocker_count": 0,
                        "table_answer_blocker_count": 0,
                        "non_review_evidence_ready": True,
                        "release_blocked_by_human_review": True,
                        "finding_code_counts": {
                            "table-human-review-pending": 1,
                            "table-count-transfer-blocked": 1,
                        },
                    },
                },
            )

            report = build_mcp_readiness_remediation_plan(product_readiness_report=product)

        self.assertEqual(1, report["remediation_item_count"])
        self.assertEqual([], report["unmapped_warning_codes"])
        item = report["remediation_items"][0]
        self.assertEqual("table_preprocessing_human_review", item["item_id"])
        self.assertEqual(["table-preprocessing-claim-not-ready"], item["warning_codes"])
        counts = item["source_counts"]
        self.assertEqual(255, counts["pending_unit_count"])
        self.assertEqual(25, counts["transfer_blocker_count"])
        self.assertEqual(2040, counts["required_field_missing_total"])
        self.assertEqual(255, counts["required_field_missing_counts"]["human_unit_status"])
        self.assertEqual(144, counts["review_priority_counts"]["source_table_compare"])
        self.assertEqual(60, counts["label_review_flag_counts"]["missing_table_label"])
        self.assertTrue(counts["non_review_evidence_ready"])
        self.assertTrue(counts["release_blocked_by_human_review"])
        self.assertIn("completed table unit review CSV/JSON", item["operator_inputs_required"])
        self.assertIn("transfer_passed is true", item["verification_after_remediation"])

    def test_table_remediation_preserves_traceability_issue_breakdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "mcp_product_readiness.json"
            payload = _product_payload()
            payload["blocking_codes"] = ["table-preprocessing-claim-not-ready"]
            payload["blocking_count"] = 1
            payload["warning_codes"] = []
            payload["warning_count"] = 0
            payload["table_preprocessing_claim_gate_summary"] = {
                "status": "blocked_source_traceability",
                "claim_level": "review_ready_not_accuracy_proven",
                "feasibility_status": "blocked_before_review",
                "selected_unit_count": 31,
                "completed_unit_count": 0,
                "pending_unit_count": 31,
                "invalid_unit_count": 0,
                "ready_for_table_score_transfer": False,
                "transfer_passed": False,
                "transfer_blocker_count": 25,
                "source_traceability_passed": False,
                "source_traceability_issue_count": 15,
                "source_traceability_issue_counts": {"pdf-reader-backend-unavailable": 15},
                "source_traceability_operator_next_action_counts": {
                    "Fix the Python PDF reader backend or run traceability in the packaged project environment; the source PDF has not been proven invalid.": 15
                },
                "source_traceability_require_page_count_verification": False,
                "drift_check_present": True,
                "drift_check_passed": True,
                "drift_check_blocker_count": 0,
                "table_answer_blocker_count": 0,
                "non_review_evidence_ready": False,
                "release_blocked_by_human_review": False,
                "finding_code_counts": {"table-source-traceability-blocked": 1},
            }
            _write_json(product, payload)

            report = build_mcp_readiness_remediation_plan(product_readiness_report=product)

        item = report["remediation_items"][0]
        counts = item["source_counts"]
        self.assertEqual({"pdf-reader-backend-unavailable": 15}, counts["source_traceability_issue_counts"])
        self.assertIn(
            "Fix the Python PDF reader backend",
            next(iter(counts["source_traceability_operator_next_action_counts"])),
        )

    def test_parser_scope_warning_maps_to_scope_decision_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "mcp_product_readiness.json"
            payload = _product_payload()
            payload["warning_codes"] = ["parser-goldset-scope-exclusions"]
            payload["warning_count"] = 1
            payload["parser_goldset_score_summary"] = {
                "document_count": 12,
                "scored_document_count": 11,
                "excluded_document_count": 1,
            }
            payload["parser_goldset_completion_board_summary"] = {
                "excluded_document_count": 1,
                "completion_gate_status": "blocked_pending_human_labels",
            }
            _write_json(product, payload)

            report = build_mcp_readiness_remediation_plan(product_readiness_report=product)

        self.assertEqual([], report["unmapped_warning_codes"])
        self.assertEqual(1, report["remediation_item_count"])
        item = report["remediation_items"][0]
        self.assertEqual("parser_goldset_scope_decision", item["item_id"])
        self.assertEqual(["parser-goldset-scope-exclusions"], item["warning_codes"])
        self.assertEqual(1, item["source_counts"]["excluded_document_count"])
        self.assertIn("release-owner goldset scope decision", item["operator_inputs_required"])

    def test_profile_provenance_warnings_map_to_generality_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "mcp_product_readiness.json"
            payload = _product_payload()
            payload["warning_codes"] = [
                "institution-profile-generic-only",
                "profile-provenance-warnings",
            ]
            payload["warning_count"] = 2
            payload["profile_provenance_summary"] = {
                "passed": True,
                "row_count": 29,
                "institution_count": 21,
                "batch_profile_counts": {"default-public-institution": 29},
                "apba_id_count": 0,
                "apba_id_counts": {"missing": 29},
                "registry_profile_count": 3,
                "matched_profile_count": 1,
                "unknown_profile_counts": {},
                "warning_count": 1,
                "blocker_count": 0,
                "file_type_counts": {"hwp": 10, "hwpx": 10, "pdf": 9},
            }
            _write_json(product, payload)

            report = build_mcp_readiness_remediation_plan(product_readiness_report=product)

        self.assertEqual([], report["unmapped_warning_codes"])
        self.assertEqual(1, report["remediation_item_count"])
        item = report["remediation_items"][0]
        self.assertEqual("institution_profile_provenance", item["item_id"])
        self.assertEqual(
            ["institution-profile-generic-only", "profile-provenance-warnings"],
            item["warning_codes"],
        )
        self.assertEqual({"default-public-institution": 29}, item["source_counts"]["batch_profile_counts"])
        self.assertEqual(0, item["source_counts"]["apba_id_count"])
        self.assertIn("institution profile registry or generated PUBLIC_PORTAL profile manifest", item["operator_inputs_required"])

    def test_cli_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "mcp_product_readiness.json"
            strict_public_readiness = root / "strict_public_readiness.json"
            out_json = root / "remediation.json"
            out_md = root / "remediation.md"
            _write_json(product, _product_payload())
            _write_json(strict_public_readiness, _strict_public_readiness_payload())
            stdout = io.StringIO()

            exit_code = main(
                [
                    "--readiness-report",
                    str(product),
                    "--strict-public-readiness-report",
                    str(strict_public_readiness),
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                ],
                stdout=stdout,
            )
            report = json.loads(out_json.read_text(encoding="utf-8"))
            markdown = out_md.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertEqual(report["remediation_item_count"], len(report["remediation_items"]))
        self.assertIn("MCP Readiness Remediation Plan", markdown)
        self.assertIn("Release gate status", markdown)
        self.assertEqual("mcp_readiness_remediation_plan", json.loads(stdout.getvalue())["report_type"])

    def test_unmapped_warning_codes_are_preserved_and_blockers_block_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "mcp_product_readiness.json"
            payload = _product_payload()
            payload["passed"] = False
            payload["blocking_count"] = 1
            payload["blocking_codes"] = ["runtime-not-fully-indexed"]
            payload["warning_codes"].append("new-warning-code")
            _write_json(product, payload)

            report = build_mcp_readiness_remediation_plan(product_readiness_report=product)

        self.assertFalse(report["passed"])
        self.assertEqual("blocked", report["plan_status"])
        self.assertEqual(["product-readiness-blockers-present"], report["plan_blockers"])
        self.assertEqual(["new-warning-code"], report["unmapped_warning_codes"])

    def test_temporal_blocking_codes_map_to_operator_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "mcp_product_readiness.json"
            payload = _product_payload()
            payload["passed"] = False
            payload["blocking_count"] = 3
            payload["blocking_codes"] = [
                "temporal-ambiguity-policy-required",
                "temporal-backfill-conflict",
                "temporal-evidence-runtime-lineage-mismatch",
            ]
            payload["warning_count"] = 0
            payload["warning_codes"] = []
            payload["temporal_ambiguity_scope_summary"] = {
                "status": "temporal_ambiguity_policy_required",
                "ambiguous_chunk_count": 44,
                "blocking_decision_count": 2,
                "review_slice_count": 3,
            }
            payload["temporal_evidence_guard_summary"] = {
                "stale_artifact_count": 0,
                "runtime_lineage_mismatch_count": 2,
                "payload_generated_at_span_hours": 0.5,
                "strict_temporal_evidence": True,
            }
            payload["revision_impact_summary"] = {
                "approval_required_count": 25,
                "metadata_only_changed_count": 3,
                "deindex_required_count": 4,
            }
            _write_json(product, payload)

            report = build_mcp_readiness_remediation_plan(product_readiness_report=product)

        self.assertFalse(report["passed"])
        self.assertEqual("blocked", report["plan_status"])
        self.assertEqual(["product-readiness-blockers-present"], report["plan_blockers"])
        self.assertEqual(1, report["remediation_item_count"])
        item = report["remediation_items"][0]
        self.assertEqual("temporal_metadata_review", item["item_id"])
        self.assertEqual(
            [
                "temporal-backfill-conflict",
                "temporal-ambiguity-policy-required",
                "temporal-evidence-runtime-lineage-mismatch",
            ],
            item["warning_codes"],
        )
        counts = item["source_counts"]
        self.assertEqual("temporal_ambiguity_policy_required", counts["temporal_ambiguity_status"])
        self.assertEqual(2, counts["temporal_ambiguity_blocking_decision_count"])
        self.assertEqual(3, counts["temporal_ambiguity_review_slice_count"])
        self.assertEqual(2, counts["temporal_evidence_runtime_lineage_mismatch_count"])
        self.assertTrue(counts["temporal_evidence_strict"])
        self.assertEqual(25, counts["revision_impact_approval_required_count"])
        self.assertEqual(4, counts["revision_impact_deindex_required_count"])
        self.assertEqual([], report["unmapped_warning_codes"])

    def test_reapproval_apply_plan_blocking_codes_map_to_operator_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "mcp_product_readiness.json"
            payload = _product_payload()
            payload["passed"] = False
            payload["blocking_count"] = 1
            payload["blocking_codes"] = ["reapproval-apply-plan-safety-contract-missing"]
            payload["warning_count"] = 0
            payload["warning_codes"] = []
            payload["reapproval_apply_plan_summary"] = {
                "report_count": 1,
                "passed": True,
                "blocker_count": 0,
                "unsafe_contract_violation_count": 2,
                "ready_plan_count": 1,
                "batch_count": 4,
                "batch_apply_control_count": 4,
                "direct_metadata_write_allowed_count": 0,
                "mcp_publish_allowed_count": 0,
                "release_gate_status_counts": {"ready_for_apply_execution": 1},
                "observed_execution_step_counts": {
                    "use_shared_review_workflow_contract": 1,
                    "run_preapproval_security_scan": 1,
                },
            }
            _write_json(product, payload)

            report = build_mcp_readiness_remediation_plan(product_readiness_report=product)

        self.assertFalse(report["passed"])
        self.assertEqual("blocked", report["plan_status"])
        self.assertEqual(["product-readiness-blockers-present"], report["plan_blockers"])
        self.assertEqual(1, report["remediation_item_count"])
        item = report["remediation_items"][0]
        self.assertEqual("reapproval_apply_plan_safety", item["item_id"])
        self.assertEqual(["reapproval-apply-plan-safety-contract-missing"], item["warning_codes"])
        counts = item["source_counts"]
        self.assertEqual(2, counts["unsafe_contract_violation_count"])
        self.assertEqual(4, counts["batch_apply_control_count"])
        self.assertIn(
            "validate_approval_preconditions",
            counts["missing_required_execution_steps"],
        )
        self.assertIn(
            "rerun_mcp_visibility_gate",
            counts["missing_required_execution_steps"],
        )
        self.assertEqual([], report["unmapped_warning_codes"])

    def test_failed_evidence_verification_blocks_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "mcp_product_readiness.json"
            verification = root / "evidence_verification.json"
            _write_json(product, _product_payload())
            _write_json(
                verification,
                {
                    "report_type": "release_evidence_bundle_verification",
                    "generated_at": "2026-07-09T13:05:00+00:00",
                    "passed": False,
                    "failure_count": 1,
                    "warnings": [],
                },
            )

            report = build_mcp_readiness_remediation_plan(
                product_readiness_report=product,
                evidence_verification_report=verification,
            )

        self.assertFalse(report["passed"])
        self.assertEqual("blocked", report["plan_status"])
        self.assertIn("evidence-verification-failed", report["plan_blockers"])

    def test_source_report_commit_mismatch_blocks_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "mcp_product_readiness.json"
            reapproval = root / "reapproval_worklist.json"
            product_payload = _product_payload()
            product_payload["repo_commit"] = "a" * 40
            _write_json(product, product_payload)
            _write_json(
                reapproval,
                {
                    "report_type": "reapproval_worklist",
                    "generated_at": "2026-07-09T13:00:00+00:00",
                    "repo_commit": "b" * 40,
                },
            )

            report = build_mcp_readiness_remediation_plan(
                product_readiness_report=product,
                reapproval_worklist_report=reapproval,
            )

        self.assertFalse(report["passed"])
        self.assertEqual("blocked", report["plan_status"])
        self.assertIn("source-report-repo-commit-mismatch", report["plan_blockers"])
        self.assertFalse(report["source_consistency_status"]["consistent"])

    def test_source_report_missing_commit_blocks_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "mcp_product_readiness.json"
            verification = root / "evidence_verification.json"
            _write_json(product, _product_payload())
            _write_json(
                verification,
                {
                    "report_type": "release_evidence_bundle_verification",
                    "generated_at": "2026-07-09T13:05:00+00:00",
                    "passed": True,
                    "failure_count": 0,
                    "warnings": [],
                },
            )

            report = build_mcp_readiness_remediation_plan(
                product_readiness_report=product,
                evidence_verification_report=verification,
            )

        self.assertFalse(report["passed"])
        self.assertEqual("blocked", report["plan_status"])
        self.assertIn("source-report-repo-commit-missing", report["plan_blockers"])
        self.assertFalse(report["source_consistency_status"]["consistent"])
        self.assertEqual(
            ["evidence_verification_report"],
            report["source_consistency_status"]["missing_repo_commit_roles"],
        )


def _product_payload() -> dict:
    return {
        "report_type": "mcp_product_readiness",
        "generated_at": "2026-07-09T12:00:00+00:00",
        "repo_commit": "a" * 40,
        "passed": True,
        "blocking_count": 0,
        "warning_count": 5,
        "warning_codes": [
            "public-readiness-review-tolerance-evidence",
            "temporal-metadata-coverage-partial",
            "runtime-version-drift-evidence",
            "approval-provenance-vector-evidence-incomplete",
            "reapproval-worklist-review-evidence",
        ],
        "public_readiness_summary": {
            "status": "review_tolerance",
            "readiness_profile": "review_tolerance",
            "strict_release_evidence": False,
            "thresholds": {
                "min_average_quality": 98.0,
                "max_failed_info": 7,
                "max_recommendations": 86,
                "max_table_attention": 0,
                "max_current_ai_tokens": 0,
            },
            "input_count": 64,
            "failed_count": 0,
            "failed_checks": ["strict_review_tolerance"],
            "recommendation_total": 86,
        },
        "temporal_coverage_summary": {
            "record_count": 100,
            "with_temporal_metadata_count": 20,
            "without_temporal_metadata_count": 80,
            "temporal_metadata_ratio": 0.2,
            "candidate_missing_record_count": 70,
        },
        "temporal_backfill_shadow_summary": {
            "delta_temporal_metadata_count": 10,
            "conflict_chunk_count": 33,
            "ambiguous_chunk_count": 44,
            "write_blocked": True,
        },
        "runtime_version_drift_summary": {
            "current_chunker_version": "0.1.5",
            "approved_repository_stale_chunker_count": 100,
            "vector_stale_chunker_count": 100,
            "reprocess_requires_reapproval": True,
        },
        "runtime_summary": {
            "approval_provenance_coverage": {
                "record_count": 100,
                "complete_record_count": 0,
                "missing_field_counts": {
                    "approval_id": 0,
                    "approval_review_batch_manifest_path": 100,
                },
            }
        },
        "reapproval_workload_summary": {
            "reapproval_candidate_chunks": 100,
            "recommended_initial_review_chunks": 80,
            "estimated_initial_review_minutes": 27,
        },
        "reapproval_review_batch_summary": {
            "batch_count": 4,
            "selected_candidate_count": 100,
            "risk_tier_chunk_counts": {"high": 80, "low": 20},
        },
    }


def _strict_public_readiness_payload() -> dict:
    return {
        "report_type": "public_batch_readiness",
        "generated_at": "2026-07-09T13:01:00+00:00",
        "repo_commit": "a" * 40,
        "status": "needs_attention",
        "passed": False,
        "readiness_profile": "strict",
        "strict_release_evidence": False,
        "summary": {
            "input_count": 64,
            "average_quality_score": 99.434,
            "failed_info_check_total": 7,
            "recommendation_total": 86,
        },
        "checks": [
            {
                "name": "failed_info_checks_within_limit",
                "passed": False,
                "details": {"failed_info_check_total": 7, "maximum": 0},
            },
            {
                "name": "recommendations_within_limit",
                "passed": False,
                "details": {"recommendation_total": 86, "maximum": 0},
            },
            {
                "name": "required_row_fields_present",
                "passed": True,
                "details": {"missing_count": 0},
            },
        ],
    }


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
