from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_pilot_blocker_action_board import build_pilot_blocker_action_board, main


class BuildPilotBlockerActionBoardTests(unittest.TestCase):
    def test_builds_action_board_from_current_blocker_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            parser = root / "parser.json"
            table = root / "table.json"
            temporal = root / "temporal.json"
            reapproval = root / "reapproval.json"
            apply_plan = root / "apply_plan.json"
            _write_json(product, _product_payload())
            _write_json(parser, _parser_completion_payload())
            _write_json(table, _table_claim_payload())
            _write_json(temporal, _temporal_validation_payload())
            _write_json(reapproval, _reapproval_validation_payload())
            _write_json(apply_plan, _reapproval_apply_plan_payload())

            report = build_pilot_blocker_action_board(
                product_readiness_report=product,
                parser_completion_board_report=parser,
                table_preprocessing_claim_gate_report=table,
                temporal_policy_decision_validation_report=temporal,
                reapproval_decision_validation_report=reapproval,
                reapproval_apply_plan_report=apply_plan,
                out_json=root / "reports" / "board.json",
                out_md=root / "reports" / "board.md",
                generated_at="2026-07-10T00:00:00+00:00",
            )
            payload = json.loads((root / "reports" / "board.json").read_text(encoding="utf-8"))
            markdown = (root / "reports" / "board.md").read_text(encoding="utf-8")

        self.assertEqual("pilot_blocker_action_board", report["report_type"])
        self.assertEqual(6, report["action_count"])
        self.assertEqual(6, payload["action_count"])
        self.assertEqual(
            [
                "clear_product_readiness_blockers",
                "complete_parser_goldset_labels",
                "complete_table_unit_human_review",
                "fill_temporal_policy_decisions",
                "fill_reapproval_batch_decisions",
                "rebuild_reapproval_apply_plan_after_decisions",
            ],
            [action["action_id"] for action in report["actions"]],
        )
        self.assertEqual(84, report["actions"][1]["evidence"]["expected_structure_score_rows"])
        self.assertEqual(1, report["actions"][1]["evidence"]["missing_matched_field_document_count"])
        self.assertEqual(
            ["matched_paragraph_item_count", "matched_table_count"],
            report["actions"][1]["evidence"]["missing_matched_field_samples"][0]["missing_matched_fields"],
        )
        self.assertEqual(255, report["actions"][2]["evidence"]["pending_unit_count"])
        self.assertEqual(
            "table_unit_human_review_pending",
            report["actions"][2]["evidence"]["transfer_primary_blocker"],
        )
        self.assertTrue(report["actions"][2]["evidence"]["source_traceability_passed"])
        self.assertEqual({"blank": 2}, report["actions"][3]["evidence"]["operator_decision_counts"])
        self.assertEqual(61, report["actions"][4]["evidence"]["blank_or_incomplete_row_count"])
        self.assertIn("does not fill labels", report["safety_note"])
        self.assertIn("Pilot Blocker Action Board", markdown)
        self.assertIn("complete_parser_goldset_labels", markdown)
        self.assertIn("missing_matched_field_samples", markdown)

    def test_cli_writes_board(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product.json"
            parser = root / "parser.json"
            _write_json(product, _product_payload())
            _write_json(parser, _parser_completion_payload())
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--product-readiness-report",
                        str(product),
                        "--parser-completion-board-report",
                        str(parser),
                        "--out-json",
                        str(root / "reports" / "board.json"),
                        "--out-md",
                        str(root / "reports" / "board.md"),
                    ]
                )
            payload = json.loads((root / "reports" / "board.json").read_text(encoding="utf-8"))

        self.assertEqual(0, exit_code)
        self.assertIn('"ok": true', stdout.getvalue())
        self.assertEqual(2, payload["action_count"])


def _product_payload() -> dict:
    return {
        "report_type": "mcp_product_readiness",
        "generated_at": "2026-07-10T00:00:00+00:00",
        "passed": False,
        "blocking_count": 6,
        "warning_count": 4,
        "blocking_codes": [
            "parser-goldset-quality-claim-not-ready",
            "temporal-ambiguity-policy-required",
            "reapproval-decision-validation-blockers",
        ],
        "gates": {
            "parsing_accuracy": {"status": "blocked"},
            "generality": {"status": "ready"},
            "answer_accuracy": {"status": "ready"},
            "revision_response": {"status": "blocked"},
            "operations": {"status": "blocked"},
        },
    }


def _parser_completion_payload() -> dict:
    return {
        "report_type": "parsing_goldset_completion_board",
        "generated_at": "2026-07-10T00:00:00+00:00",
        "completion_gate_status": "blocked_pending_human_labels",
        "ready_for_quality_claim": False,
        "document_count": 12,
        "ready_document_count": 0,
        "completed_structure_score_rows": 0,
        "expected_structure_score_rows": 84,
        "missing_manual_field_count": 84,
        "missing_matched_field_count": 84,
        "label_status_counts": {"pending_human_review": 12},
        "rows": [
            {
                "document_id": "doc_1",
                "filename": "sample.hwp",
                "missing_matched_fields": "matched_paragraph_item_count; matched_table_count",
                "missing_structures": "paragraph_item; table",
                "next_structure_checklist": "paragraph_item: matched=matched_paragraph_item_count",
            }
        ],
    }


def _table_claim_payload() -> dict:
    return {
        "report_type": "table_preprocessing_claim_gate",
        "generated_at": "2026-07-10T00:00:00+00:00",
        "passed": False,
        "status": "blocked_pending_human_review",
        "claim_level": "review_ready_not_accuracy_proven",
        "summary": {
            "pending_unit_count": 255,
            "completed_unit_count": 0,
            "invalid_unit_count": 0,
            "transfer_blocker_count": 25,
            "transfer_finding_code_counts": {"manual-table-count-missing-or-invalid": 12},
            "transfer_root_cause_summary": {"primary_blocker": "table_unit_human_review_pending"},
            "table_answer_blocker_count": 0,
            "source_traceability_passed": True,
            "source_traceability_issue_count": 0,
            "drift_check_passed": True,
            "drift_check_blocker_count": 0,
            "source_format_status_counts": {"verified_pdf": 10, "verified_hwpx_zip": 5},
        },
    }


def _temporal_validation_payload() -> dict:
    return {
        "report_type": "temporal_ambiguity_policy_decision_validation",
        "generated_at": "2026-07-10T00:00:00+00:00",
        "passed": False,
        "status": "blocked_pending_policy_decisions",
        "decision_row_count": 2,
        "release_blocking_row_count": 2,
        "blocking_count": 2,
        "operator_decision_counts": {"blank": 2},
    }


def _reapproval_validation_payload() -> dict:
    return {
        "report_type": "reapproval_decision_validation",
        "generated_at": "2026-07-10T00:00:00+00:00",
        "passed": False,
        "release_gate_status": "blocked_pending_operator_decisions",
        "expected_batch_count": 61,
        "decision_row_count": 61,
        "complete_row_count": 0,
        "blank_or_incomplete_row_count": 61,
        "blocking_count": 1,
    }


def _reapproval_apply_plan_payload() -> dict:
    return {
        "report_type": "reapproval_apply_plan",
        "generated_at": "2026-07-10T00:00:00+00:00",
        "passed": False,
        "release_gate_status": "blocked_pending_apply_preflight",
        "blocker_count": 63,
        "ready_plan_count": 0,
        "unresolved_chunk_count": 5997,
    }


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
