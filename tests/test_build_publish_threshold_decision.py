import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts.build_publish_threshold_decision import (
    build_publish_threshold_decision,
    render_markdown,
    main,
)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class BuildPublishThresholdDecisionTests(unittest.TestCase):
    def test_builds_conditional_85_and_blocked_90_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hitl = root / "hitl.json"
            github = root / "github.json"
            product = root / "product.json"
            table = root / "table.json"
            parser = root / "parser.json"
            answer_depth = root / "answer_depth.json"

            _write_json(
                hitl,
                {
                    "official_public_release_ready": False,
                    "limited_human_loop_pilot_status": "conditional_human_review_required",
                    "summary": {
                        "open_queue_count": 4,
                        "total_open_items": 316,
                        "parser_open_item_count": 52,
                        "parser_first_review_batch_open_item_count": 26,
                        "table_pending_unit_count": 255,
                        "table_first_review_batch_unit_count": 50,
                        "table_required_field_missing_total": 2040,
                        "owner_decision_count": 7,
                        "machine_cleanup_action_count": 2,
                        "source_lineage_blocking_count": 0,
                        "product_blocking_count": 4,
                        "product_warning_count": 1,
                        "public_finding_count": 203,
                        "public_action_count": 183,
                    },
                },
            )
            _write_json(
                github,
                {
                    "public_release_gate_status": {"passed": False, "finding_count": 203},
                    "product_readiness_status": {"passed": False, "blocking_count": 4},
                    "source_lineage_status": {
                        "passed": True,
                        "blocking_count": 0,
                        "warning_count": 0,
                    },
                    "progress_tracks": [
                        {"track": "core_pipeline", "progress_band": "55-65%", "status": "blocked"},
                        {
                            "track": "human_intervention_minimization",
                            "progress_band": "70-75%",
                            "status": "approval_queue_clear",
                        },
                        {
                            "track": "source_only_github_publish",
                            "progress_band": "45-55%",
                            "status": "blocked_by_public_audit",
                        },
                        {
                            "track": "product_public_release",
                            "progress_band": "40-50%",
                            "status": "public_gate_blocked",
                        },
                    ],
                    "owner_decisions_required": [
                        {"decision_id": "license_selection", "summary": "Choose license"},
                        {
                            "decision_id": "sample_redistribution_policy",
                            "summary": "Decide sample redistribution policy",
                        },
                    ],
                },
            )
            _write_json(
                product,
                {
                    "passed": False,
                    "blocking_codes": [
                        "parser-goldset-f1-missing",
                        "table-preprocessing-claim-not-ready",
                    ],
                    "warning_codes": ["parser-goldset-scope-exclusions"],
                },
            )
            _write_json(
                table,
                {
                    "status": "blocked_pending_human_review",
                    "feasibility_status": "feasible_with_human_review",
                    "summary": {
                        "source_traceability_passed": True,
                        "pending_unit_count": 255,
                        "required_field_missing_total": 2040,
                    },
                },
            )
            _write_json(parser, {"open_item_summary": {"open_item_count": 52}})
            _write_json(
                answer_depth,
                {
                    "passed_for_public_release_depth": False,
                    "pilot_evidence_status": "usable_with_disclosure",
                    "warning_count": 3,
                    "blocker_count": 0,
                    "findings": [
                        {"code": "answer-evidence-query-count-thin"},
                        {"code": "answer-evidence-no-evidence-controls-thin"},
                    ],
                },
            )

            report = build_publish_threshold_decision(
                hitl_workboard_report=hitl,
                github_publish_summary_report=github,
                product_readiness_report=product,
                table_claim_gate_report=table,
                parser_start_report=parser,
                answer_accuracy_depth_report=answer_depth,
            )

        self.assertEqual("blocked", report["current_decision"]["official_public_release"])
        self.assertEqual("conditional", report["current_decision"]["limited_human_loop_pilot"])
        self.assertEqual(
            "conditional_limited_pilot_only",
            report["threshold_assessment"]["eighty_five_plus"]["decision"],
        )
        self.assertEqual(
            "required_for_public_release_grade_claims",
            report["threshold_assessment"]["ninety_plus"]["decision"],
        )
        self.assertEqual("40-50%", report["current_progress_bands"]["product_public_release"])
        self.assertEqual(316, report["evidence_counts"]["total_open_items"])
        self.assertEqual(0, report["evidence_counts"]["source_lineage_blocking_count"])
        self.assertTrue(report["table_claim_gate"]["source_traceability_passed"])
        self.assertEqual(255, report["table_claim_gate"]["pending_unit_count"])
        self.assertEqual(3, report["evidence_counts"]["answer_depth_warning_count"])
        self.assertEqual(
            "usable_with_disclosure",
            report["answer_accuracy_depth"]["pilot_evidence_status"],
        )
        self.assertIn(
            "parser-goldset-f1-missing",
            report["product_readiness"]["blocking_codes"],
        )
        self.assertEqual(4, len(report["hard_blockers"]))
        markdown = render_markdown(report)
        self.assertIn("Owner decision IDs", markdown)
        self.assertIn("license_selection", markdown)

    def test_table_gate_summary_overrides_stale_hitl_table_pending_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hitl = root / "hitl.json"
            github = root / "github.json"
            product = root / "product.json"
            table = root / "table.json"

            _write_json(
                hitl,
                {
                    "official_public_release_ready": False,
                    "summary": {
                        "table_pending_unit_count": 255,
                        "table_required_field_missing_total": 2040,
                        "owner_decision_count": 0,
                        "machine_cleanup_action_count": 0,
                    },
                },
            )
            _write_json(github, {"public_release_gate_status": {"passed": False}, "progress_tracks": []})
            _write_json(
                product,
                {
                    "passed": False,
                    "blocking_count": 9,
                    "warning_count": 5,
                    "blocking_codes": [],
                    "warning_codes": [],
                },
            )
            _write_json(
                table,
                {
                    "passed": True,
                    "status": "ready_for_table_quality_claim",
                    "summary": {
                        "pending_unit_count": 0,
                        "invalid_unit_count": 0,
                        "required_field_missing_total": 0,
                        "source_traceability_passed": True,
                        "transfer_passed": True,
                        "transfer_blocker_count": 0,
                    },
                },
            )

            report = build_publish_threshold_decision(
                hitl_workboard_report=hitl,
                github_publish_summary_report=github,
                product_readiness_report=product,
                table_claim_gate_report=table,
            )

        self.assertEqual(0, report["evidence_counts"]["table_pending_unit_count"])
        self.assertEqual(0, report["evidence_counts"]["table_required_field_missing_total"])
        self.assertEqual(0, report["evidence_counts"]["table_first_review_batch_unit_count"])
        self.assertEqual(9, report["evidence_counts"]["product_blocking_count"])
        self.assertEqual(5, report["evidence_counts"]["product_warning_count"])
        self.assertTrue(report["table_claim_gate"]["claim_ready"])
        self.assertNotIn(
            "table_unit_human_review",
            {blocker["blocker"] for blocker in report["hard_blockers"]},
        )
        self.assertNotIn(
            "table_first_review_batch_unit_count",
            {action["evidence_target"] for action in report["next_actions_to_reach_90_plus"]},
        )

    def test_cli_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hitl = root / "hitl.json"
            github = root / "github.json"
            product = root / "product.json"
            out_json = root / "threshold.json"
            out_md = root / "threshold.md"
            _write_json(hitl, {"official_public_release_ready": True, "summary": {}})
            _write_json(
                github,
                {
                    "public_release_gate_status": {"passed": True},
                    "progress_tracks": [
                        {
                            "track": "source_only_github_publish",
                            "status": "ready",
                            "progress_band": "90-95%",
                        }
                    ],
                },
            )
            _write_json(product, {"passed": True, "blocking_codes": [], "warning_codes": []})

            import sys

            old_argv = sys.argv
            try:
                sys.argv = [
                    "build_publish_threshold_decision.py",
                    "--hitl-workboard-report",
                    str(hitl),
                    "--github-publish-summary-report",
                    str(github),
                    "--product-readiness-report",
                    str(product),
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                ]
                with redirect_stdout(StringIO()):
                    self.assertEqual(0, main())
            finally:
                sys.argv = old_argv

            self.assertTrue(out_json.exists())
            self.assertIn("Publish Threshold Decision", out_md.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
