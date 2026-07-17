from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.validate_reapproval_decisions import main, validate_reapproval_decisions


class ValidateReapprovalDecisionsTests(unittest.TestCase):
    def test_complete_decisions_pass_without_applying_reapproval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_manifest(root / "batches.json", ["batch-a", "batch-b"])
            decisions = root / "decisions.csv"
            _write_decisions(
                decisions,
                [
                    _decision("batch-a", "approve_all_reviewed"),
                    _decision("batch-b", "needs_reprocess"),
                ],
            )

            report = validate_reapproval_decisions(
                reapproval_review_batch_report=manifest,
                decision_template_csv=decisions,
            )

        self.assertTrue(report["passed"])
        self.assertEqual("ready_for_reapproval_apply", report["release_gate_status"])
        self.assertEqual(2, report["expected_batch_count"])
        self.assertEqual(2, report["decision_row_count"])
        self.assertEqual(2, report["complete_row_count"])
        self.assertFalse(report["operator_controls"]["auto_approval"])
        self.assertFalse(report["operator_controls"]["auto_reindex"])
        self.assertFalse(report["operator_controls"]["applies_reapproval_decisions"])
        self.assertIn("approve_all_reviewed", report["allowed_operator_decisions"])
        self.assertIn("operator_decision", report["required_operator_fields"])
        self.assertIn("confirmed", report["allowed_approval_scope_confirmations"])
        self.assertIn("needs_reprocess", report["allowed_override_decisions"])
        self.assertIn("does not approve chunks", report["safety_note"])

    def test_blank_and_invalid_decisions_block_release_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_manifest(root / "batches.json", ["batch-a", "batch-b"])
            decisions = root / "decisions.csv"
            _write_decisions(
                decisions,
                [
                    _decision("batch-a", ""),
                    _decision("batch-b", "approved"),
                ],
            )

            report = validate_reapproval_decisions(
                reapproval_review_batch_report=manifest,
                decision_template_csv=decisions,
            )

        self.assertFalse(report["passed"])
        self.assertEqual("blocked_pending_operator_decisions", report["release_gate_status"])
        codes = {item["code"] for item in report["findings"]}
        self.assertIn("decision-template-required-fields-missing", codes)
        self.assertIn("decision-template-invalid-operator-decision", codes)
        self.assertEqual(0, report["complete_row_count"])

    def test_unknown_duplicate_and_missing_batches_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_manifest(root / "batches.json", ["batch-a", "batch-b"])
            decisions = root / "decisions.csv"
            _write_decisions(
                decisions,
                [
                    _decision("batch-a", "approve_all_reviewed"),
                    _decision("batch-a", "reject_all"),
                    _decision("batch-x", "defer"),
                ],
            )

            report = validate_reapproval_decisions(
                reapproval_review_batch_report=manifest,
                decision_template_csv=decisions,
            )

        self.assertFalse(report["passed"])
        codes = {item["code"] for item in report["findings"]}
        self.assertIn("decision-template-duplicate-batch-id", codes)
        self.assertIn("decision-template-unknown-batch-id", codes)
        self.assertIn("decision-template-missing-batches", codes)

    def test_failed_manifest_blocks_even_with_complete_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_manifest(root / "batches.json", ["batch-a"], passed=False, blocker_count=1)
            decisions = root / "decisions.csv"
            _write_decisions(decisions, [_decision("batch-a", "approve_all_reviewed")])

            report = validate_reapproval_decisions(
                reapproval_review_batch_report=manifest,
                decision_template_csv=decisions,
            )

        self.assertFalse(report["passed"])
        self.assertEqual("blocked_pending_operator_decisions", report["release_gate_status"])
        self.assertIn("reapproval-review-batch-manifest-not-ready", {item["code"] for item in report["findings"]})

    def test_manifest_batch_count_mismatch_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_manifest(root / "batches.json", ["batch-a"], reported_batch_count=2)
            decisions = root / "decisions.csv"
            _write_decisions(decisions, [_decision("batch-a", "approve_all_reviewed")])

            report = validate_reapproval_decisions(
                reapproval_review_batch_report=manifest,
                decision_template_csv=decisions,
            )

        self.assertFalse(report["passed"])
        self.assertIn("reapproval-review-batch-count-mismatch", {item["code"] for item in report["findings"]})

    def test_manifest_invalid_batch_count_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_manifest(root / "batches.json", ["batch-a"], reported_batch_count="not-a-number")
            decisions = root / "decisions.csv"
            _write_decisions(decisions, [_decision("batch-a", "approve_all_reviewed")])

            report = validate_reapproval_decisions(
                reapproval_review_batch_report=manifest,
                decision_template_csv=decisions,
            )

        self.assertFalse(report["passed"])
        self.assertIn("reapproval-review-batch-count-missing-or-invalid", {item["code"] for item in report["findings"]})

    def test_partial_decision_requires_non_empty_valid_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_manifest(root / "batches.json", ["batch-a", "batch-b"])
            decisions = root / "decisions.csv"
            _write_decisions(
                decisions,
                [
                    _decision("batch-a", "partial_with_overrides"),
                    _decision("batch-b", "partial_with_overrides", overrides='{"chunk-1":"reject"}'),
                ],
            )

            report = validate_reapproval_decisions(
                reapproval_review_batch_report=manifest,
                decision_template_csv=decisions,
            )

        self.assertFalse(report["passed"])
        codes = {item["code"] for item in report["findings"]}
        self.assertIn("partial-decision-overrides-missing", codes)
        self.assertEqual(1, report["complete_row_count"])

    def test_override_scope_and_action_errors_block_before_apply_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_manifest(root / "batches.json", ["batch-a"])
            decisions = root / "decisions.csv"
            _write_decisions(
                decisions,
                [
                    _decision(
                        "batch-a",
                        "partial_with_overrides",
                        overrides=json.dumps(
                            [
                                {"chunk_id": "chunk-1", "decision": "reject"},
                                {"chunk_id": "chunk-1", "decision": "defer"},
                                {"chunk_id": "chunk-x", "decision": "approve"},
                                {"chunk_id": "chunk-2", "decision": "bad"},
                                {"decision": "approve"},
                            ]
                        ),
                    ),
                ],
            )

            report = validate_reapproval_decisions(
                reapproval_review_batch_report=manifest,
                decision_template_csv=decisions,
            )

        self.assertFalse(report["passed"])
        codes = {item["code"] for item in report["findings"]}
        self.assertIn("decision-template-duplicate-override-chunk-id", codes)
        self.assertIn("decision-template-override-chunk-id-outside-batch", codes)
        self.assertIn("decision-template-invalid-override-decision", codes)
        self.assertIn("decision-template-override-chunk-id-missing", codes)
        invalid_override = next(
            item for item in report["findings"] if item["code"] == "decision-template-invalid-override-decision"
        )
        self.assertIn("reject", invalid_override["allowed_override_decisions"])

    def test_unsupported_override_json_shape_blocks_before_apply_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_manifest(root / "batches.json", ["batch-a"])
            decisions = root / "decisions.csv"
            _write_decisions(
                decisions,
                [
                    _decision("batch-a", "partial_with_overrides", overrides='"reject"'),
                ],
            )

            report = validate_reapproval_decisions(
                reapproval_review_batch_report=manifest,
                decision_template_csv=decisions,
            )

        self.assertFalse(report["passed"])
        codes = {item["code"] for item in report["findings"]}
        self.assertIn("decision-template-unsupported-overrides-shape", codes)
        self.assertIn("partial-decision-overrides-missing", codes)

    def test_cli_writes_report_and_returns_nonzero_on_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _write_manifest(root / "batches.json", ["batch-a"])
            decisions = root / "decisions.csv"
            out_json = root / "validation.json"
            out_md = root / "validation.md"
            _write_decisions(decisions, [_decision("batch-a", "")])

            exit_code = main(
                [
                    "--reapproval-review-batch-report",
                    str(manifest),
                    "--decision-template-csv",
                    str(decisions),
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                    "--fail-on-issue",
                ],
                stdout=io.StringIO(),
            )
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            markdown_exists = out_md.is_file()

        self.assertEqual(2, exit_code)
        self.assertEqual("reapproval_decision_validation", payload["report_type"])
        self.assertFalse(payload["passed"])
        self.assertTrue(markdown_exists)


def _write_manifest(
    path: Path,
    batch_ids: list[str],
    *,
    passed: bool = True,
    blocker_count: int = 0,
    reported_batch_count: object | None = None,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "report_type": "reapproval_review_batch_manifest",
                "generated_at": "2026-07-10T00:00:00+00:00",
                "passed": passed,
                "blocker_count": blocker_count,
                "batch_count": len(batch_ids) if reported_batch_count is None else reported_batch_count,
                "batches": [
                    {
                        "batch_rank": index,
                        "reapproval_batch_id": batch_id,
                        "chunk_count": 1,
                        "chunk_ids": [f"chunk-{index}"],
                    }
                    for index, batch_id in enumerate(batch_ids, start=1)
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_decisions(path: Path, rows: list[dict[str, str]]) -> None:
    fields = [
        "batch_rank",
        "reapproval_batch_id",
        "operator_decision",
        "reviewer_id",
        "reviewed_at",
        "decision_notes",
        "chunk_decision_overrides_json",
        "approval_scope_confirmation",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _decision(batch_id: str, decision: str, *, overrides: str = "[]") -> dict[str, str]:
    return {
        "batch_rank": "1",
        "reapproval_batch_id": batch_id,
        "operator_decision": decision,
        "reviewer_id": "reviewer-a",
        "reviewed_at": "2026-07-10T09:00:00+09:00",
        "decision_notes": "",
        "chunk_decision_overrides_json": overrides,
        "approval_scope_confirmation": "confirmed",
    }


if __name__ == "__main__":
    unittest.main()
