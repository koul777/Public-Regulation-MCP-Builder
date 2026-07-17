import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts.build_publish_hitl_operator_workboard import (
    build_publish_hitl_operator_workboard,
    render_markdown,
    main,
)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class BuildPublishHitlOperatorWorkboardTests(unittest.TestCase):
    def test_builds_operator_queues_from_publish_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parser_report = root / "parser.json"
            parser_batches_report = root / "parser_batches.json"
            table_report = root / "table.json"
            table_batches_report = root / "table_batches.json"
            github_report = root / "github.json"
            remediation_report = root / "remediation.json"

            _write_json(
                parser_report,
                {
                    "open_item_summary": {
                        "open_item_count": 52,
                        "item_kind_counts": {"matched_count": 40, "label_status": 12},
                        "structure_counts": {"paragraph_item": 11, "table": 6},
                    },
                    "completion_summary": {
                        "pending_document_count": 12,
                        "ready_for_quality_claim": False,
                    },
                    "structure_review_queue": [{"structure": "paragraph_item"}],
                    "top_documents": [{"document_id": "doc-a"}],
                    "source_artifacts": {"labels_csv": {"path": "labels.csv"}},
                    "output_artifacts": {"open_item_worklist_csv": "open_items.csv"},
                    "open_commands": {"open_label_csv": "Invoke-Item labels.csv"},
                },
            )
            _write_json(
                parser_batches_report,
                {
                    "first_batch_document_count": 2,
                    "first_batch_open_item_count": 9,
                    "first_batch_pipeline_count_total": 1445,
                    "first_review_batch": [
                        {"document_id": "doc-a"},
                        {"document_id": "doc-b"},
                    ],
                },
            )
            _write_json(
                table_report,
                {
                    "source_table_units_csv": "table_units.csv",
                    "selected_unit_count": 255,
                    "completed_unit_count": 0,
                    "pending_unit_count": 255,
                    "required_field_missing_total": 2040,
                    "required_field_missing_counts": {"human_unit_status": 255},
                    "review_priority_counts": {"source_table_compare": 144},
                    "label_review_flag_counts": {"missing_table_label": 60},
                    "ready_for_table_score_transfer": False,
                    "document_summaries": [{"document_id": "doc-table", "unit_count": "50"}],
                    "artifacts": {"csv": "table_summary.csv", "markdown": "table_summary.md"},
                },
            )
            _write_json(
                table_batches_report,
                {
                    "first_batch_count": 5,
                    "first_batch_unit_count": 50,
                    "human_status_missing_batch_count": 31,
                    "first_review_batches": [
                        {
                            "batch_rank": 1,
                            "table_review_batch_id": "batch-1",
                            "document_id": "doc-table",
                            "unit_count": 20,
                            "review_priority": "source_table_compare",
                        }
                    ],
                },
            )
            _write_json(
                github_report,
                {
                    "product_readiness_status": {
                        "passed": False,
                        "blocking_count": 4,
                        "warning_count": 1,
                    },
                    "public_release_gate_status": {
                        "passed": False,
                        "status": "blocked_by_public_audit",
                        "finding_count": 203,
                        "action_count": 183,
                    },
                    "owner_decisions_required": [
                        {"decision_id": "license_selection", "summary": "Choose license"}
                    ],
                    "machine_cleanup_action_count": 2,
                    "machine_cleanup_actions": [
                        {"action": "remove_generated_report", "path": "reports/a.json"},
                        {"action": "remove_generated_report", "path": "reports/b.json"},
                    ],
                    "cleanup_breakdown": {
                        "destructive_action_count": 2,
                        "safe_machine_action_count": 2,
                    },
                    "recommended_sequence": [
                        {
                            "order": 1,
                            "workstream": "public_branch_policy",
                            "action": "Resolve owner policy.",
                            "blocks_public_publish": True,
                        }
                    ],
                },
            )
            _write_json(
                remediation_report,
                {
                    "remediation_items": [
                        {
                            "item_id": "table_preprocessing_human_review",
                            "source_counts": {
                                "source_traceability_passed": True,
                                "source_traceability_issue_count": 0,
                            },
                        }
                    ]
                },
            )

            report = build_publish_hitl_operator_workboard(
                parser_start_report=parser_report,
                parser_review_batches_report=parser_batches_report,
                table_review_summary_report=table_report,
                table_review_batches_report=table_batches_report,
                github_publish_summary_report=github_report,
                remediation_plan_report=remediation_report,
            )

        self.assertFalse(report["official_public_release_ready"])
        self.assertEqual(4, report["summary"]["open_queue_count"])
        self.assertEqual(310, report["summary"]["total_open_items"])
        self.assertEqual(2040, report["summary"]["table_required_field_missing_total"])
        queue_by_id = {queue["queue_id"]: queue for queue in report["operator_queues"]}
        self.assertEqual(52, queue_by_id["parser_goldset_open_items"]["item_count"])
        self.assertEqual(
            "open_items.csv",
            queue_by_id["parser_goldset_open_items"]["start_here"][
                "open_item_worklist_csv"
            ],
        )
        self.assertEqual(
            9,
            queue_by_id["parser_goldset_open_items"]["evidence"][
                "first_review_batch_open_item_count"
            ],
        )
        self.assertEqual(9, report["summary"]["parser_first_review_batch_open_item_count"])
        self.assertEqual(255, queue_by_id["table_unit_human_review"]["item_count"])
        self.assertEqual(50, report["summary"]["table_first_review_batch_unit_count"])
        self.assertEqual(
            50,
            queue_by_id["table_unit_human_review"]["evidence"][
                "first_review_batch_unit_count"
            ],
        )
        self.assertTrue(
            queue_by_id["table_unit_human_review"]["evidence"][
                "source_traceability_passed"
            ]
        )
        self.assertEqual(
            "owner_decision_required",
            queue_by_id["public_branch_owner_decisions"]["status"],
        )
        markdown = render_markdown(report)
        self.assertIn("| Decision ID | Blocking Decision |", markdown)
        self.assertIn("license_selection", markdown)
        self.assertIn("Choose license", markdown)
        self.assertIn("| Batch Rank | Batch ID | Document | Units | Priority |", markdown)
        self.assertIn("batch-1", markdown)

    def test_cli_writes_json_markdown_and_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parser_report = root / "parser.json"
            table_report = root / "table.json"
            github_report = root / "github.json"
            out_json = root / "workboard.json"
            out_md = root / "workboard.md"
            out_csv = root / "workboard.csv"
            _write_json(parser_report, {"open_item_summary": {}})
            _write_json(table_report, {})
            _write_json(
                github_report,
                {
                    "product_readiness_status": {"passed": True},
                    "public_release_gate_status": {"passed": True},
                },
            )

            import sys

            old_argv = sys.argv
            try:
                sys.argv = [
                    "build_publish_hitl_operator_workboard.py",
                    "--parser-start-report",
                    str(parser_report),
                    "--table-review-summary-report",
                    str(table_report),
                    "--github-publish-summary-report",
                    str(github_report),
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                    "--out-csv",
                    str(out_csv),
                ]
                with redirect_stdout(StringIO()):
                    self.assertEqual(0, main())
            finally:
                sys.argv = old_argv

            self.assertTrue(out_json.exists())
            self.assertIn("Publish HITL Operator Workboard", out_md.read_text(encoding="utf-8"))
            self.assertIn("queue_id", out_csv.read_text(encoding="utf-8"))

    def test_blocks_official_ready_when_source_lineage_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parser_report = root / "parser.json"
            table_report = root / "table.json"
            github_report = root / "github.json"
            _write_json(parser_report, {"open_item_summary": {}})
            _write_json(table_report, {})
            _write_json(
                github_report,
                {
                    "overall_status": "source_report_lineage_blocked",
                    "product_readiness_status": {"passed": True},
                    "public_release_gate_status": {"passed": True},
                    "source_lineage_status": {
                        "passed": False,
                        "status": "blocked",
                        "blocking_count": 1,
                        "warning_count": 0,
                        "findings": [{"code": "stale-product-readiness-report"}],
                    },
                },
            )

            report = build_publish_hitl_operator_workboard(
                parser_start_report=parser_report,
                table_review_summary_report=table_report,
                github_publish_summary_report=github_report,
            )

        self.assertFalse(report["official_public_release_ready"])
        self.assertEqual(1, report["summary"]["source_lineage_blocking_count"])
        queue_by_id = {queue["queue_id"]: queue for queue in report["operator_queues"]}
        self.assertEqual("open", queue_by_id["source_report_lineage"]["status"])
        self.assertEqual(1, queue_by_id["source_report_lineage"]["item_count"])
        self.assertEqual(1, report["summary"]["open_queue_count"])


if __name__ == "__main__":
    unittest.main()
