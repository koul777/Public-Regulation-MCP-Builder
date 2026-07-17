from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.validate_temporal_ambiguity_policy_decisions import (
    main,
    validate_temporal_ambiguity_policy_decisions,
)


class ValidateTemporalAmbiguityPolicyDecisionsTests(unittest.TestCase):
    def test_blank_release_blocking_decisions_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scope = root / "scope.json"
            decisions = root / "decisions.csv"
            _write_json(scope, _scope_payload())
            _write_decisions(
                decisions,
                [
                    {"decision_id": "temporal_ambiguity_index_policy", "blocks_product_release": "true"},
                    {"decision_id": "temporal_ambiguity_answer_policy", "blocks_product_release": "true"},
                ],
            )

            report = validate_temporal_ambiguity_policy_decisions(
                scope_report=scope,
                decisions_csv=decisions,
            )

        self.assertFalse(report["passed"])
        self.assertEqual("blocked_pending_policy_decisions", report["status"])
        self.assertEqual(2, report["blocking_count"])
        self.assertEqual({"blank": 2}, report["operator_decision_counts"])
        self.assertEqual({"temporal-policy-decision-blank"}, {finding["code"] for finding in report["findings"]})

    def test_completed_release_blocking_decisions_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scope = root / "scope.json"
            decisions = root / "decisions.csv"
            _write_json(scope, _scope_payload())
            _write_decisions(
                decisions,
                [
                    {
                        "decision_id": "temporal_ambiguity_index_policy",
                        "blocks_product_release": "true",
                        "operator_decision": "approve_index_with_disclosure",
                        "accepted_ambiguity_fields": "effective_date; revision_date",
                        "owner": "release-owner",
                        "decision_reference": "TEMP-001",
                        "post_policy_readiness_report": "reports/product.json",
                    },
                    {
                        "decision_id": "temporal_ambiguity_answer_policy",
                        "blocks_product_release": "true",
                        "operator_decision": "approve_with_disclosure",
                        "answer_disclosure_policy": "Disclose ambiguous effective/revision dates in answer caveat.",
                        "owner": "release-owner",
                        "decision_reference": "TEMP-002",
                        "sample_answer_artifact": "reports/sample_answers.json",
                    },
                ],
            )

            report = validate_temporal_ambiguity_policy_decisions(
                scope_report=scope,
                decisions_csv=decisions,
            )

        self.assertTrue(report["passed"])
        self.assertEqual("policy_decisions_valid", report["status"])
        self.assertEqual(0, report["blocking_count"])

    def test_decision_id_specific_operator_decisions_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scope = root / "scope.json"
            decisions = root / "decisions.csv"
            _write_json(scope, _scope_payload())
            _write_decisions(
                decisions,
                [
                    {
                        "decision_id": "temporal_ambiguity_index_policy",
                        "blocks_product_release": "true",
                        "operator_decision": "approve_index_with_disclosure",
                        "accepted_ambiguity_fields": "effective_date",
                        "owner": "release-owner",
                        "decision_reference": "TEMP-001",
                        "post_policy_readiness_report": "reports/product.json",
                    },
                    {
                        "decision_id": "temporal_ambiguity_answer_policy",
                        "blocks_product_release": "true",
                        "operator_decision": "block_ambiguous_indexing",
                        "answer_disclosure_policy": "Disclose ambiguous effective/revision dates in answer caveat.",
                        "owner": "release-owner",
                        "decision_reference": "TEMP-002",
                        "sample_answer_artifact": "reports/sample_answers.json",
                    },
                ],
            )

            report = validate_temporal_ambiguity_policy_decisions(
                scope_report=scope,
                decisions_csv=decisions,
            )

        self.assertFalse(report["passed"])
        self.assertIn(
            "temporal-policy-decision-not-release-ready",
            {finding["code"] for finding in report["findings"]},
        )

    def test_not_passed_scope_without_decision_requirements_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scope = root / "scope.json"
            decisions = root / "decisions.csv"
            _write_json(
                scope,
                {
                    "report_type": "temporal_ambiguity_review_scope",
                    "status": "temporal_ambiguity_policy_required",
                    "passed": False,
                },
            )
            _write_decisions(
                decisions,
                [
                    {
                        "decision_id": "temporal_ambiguity_index_policy",
                        "blocks_product_release": "true",
                        "operator_decision": "approve_index_with_disclosure",
                        "accepted_ambiguity_fields": "effective_date",
                        "owner": "release-owner",
                        "decision_reference": "TEMP-001",
                        "post_policy_readiness_report": "reports/product.json",
                    }
                ],
            )

            report = validate_temporal_ambiguity_policy_decisions(
                scope_report=scope,
                decisions_csv=decisions,
            )

        self.assertFalse(report["passed"])
        self.assertIn("temporal-policy-scope-requirements-missing", {finding["code"] for finding in report["findings"]})

    def test_missing_required_scope_decision_row_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scope = root / "scope.json"
            decisions = root / "decisions.csv"
            _write_json(scope, _scope_payload())
            _write_decisions(
                decisions,
                [
                    {
                        "decision_id": "temporal_ambiguity_index_policy",
                        "blocks_product_release": "true",
                        "operator_decision": "approve_index_with_disclosure",
                        "accepted_ambiguity_fields": "effective_date",
                        "owner": "release-owner",
                        "decision_reference": "TEMP-001",
                        "post_policy_readiness_report": "reports/product.json",
                    }
                ],
            )

            report = validate_temporal_ambiguity_policy_decisions(
                scope_report=scope,
                decisions_csv=decisions,
            )

        self.assertFalse(report["passed"])
        self.assertEqual(
            {"temporal-policy-decision-row-missing"},
            {finding["code"] for finding in report["findings"]},
        )

    def test_cli_writes_expected_fail_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scope = root / "scope.json"
            decisions = root / "decisions.csv"
            out_json = root / "validation.json"
            out_md = root / "validation.md"
            _write_json(scope, _scope_payload())
            _write_decisions(decisions, [{"decision_id": "temporal_ambiguity_index_policy", "blocks_product_release": "true"}])
            stdout = io.StringIO()

            exit_code = main(
                [
                    "--scope-report",
                    str(scope),
                    "--decisions-csv",
                    str(decisions),
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                ],
                stdout=stdout,
            )
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            markdown = out_md.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertFalse(payload["passed"])
        self.assertIn("blocked_pending_policy_decisions", stdout.getvalue())
        self.assertIn("temporal-policy-decision-blank", markdown)


def _scope_payload() -> dict[str, object]:
    return {
        "report_type": "temporal_ambiguity_review_scope",
        "status": "temporal_ambiguity_policy_required",
        "passed": False,
        "decision_requirements": [
            {
                "decision_id": "temporal_ambiguity_index_policy",
                "required_decision": "Decide whether chunks with temporal_metadata_ambiguous_fields remain indexable.",
                "blocks_product_release": True,
                "evidence_required": ["owner decision reference", "accepted ambiguity fields", "post-policy readiness report"],
            },
            {
                "decision_id": "temporal_ambiguity_answer_policy",
                "required_decision": "Decide how MCP answers disclose ambiguous effective/revision dates.",
                "blocks_product_release": True,
                "evidence_required": ["answer wording policy", "sample MCP answers with ambiguity disclosure"],
            },
        ],
    }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_decisions(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = [
        "decision_id",
        "blocks_product_release",
        "operator_decision",
        "accepted_ambiguity_fields",
        "answer_disclosure_policy",
        "owner",
        "decision_reference",
        "sample_answer_artifact",
        "post_policy_readiness_report",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
