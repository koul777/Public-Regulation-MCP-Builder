from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_temporal_ambiguity_policy_decision_sheet import (
    build_temporal_ambiguity_policy_decision_sheet,
    main,
)


class TemporalAmbiguityPolicyDecisionSheetTests(unittest.TestCase):
    def test_builds_pending_policy_decision_rows_from_scope_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scope = root / "scope.json"
            out_csv = root / "decisions.csv"
            out_md = root / "decisions.md"
            _write_json(scope, _scope_payload())

            report = build_temporal_ambiguity_policy_decision_sheet(
                scope_report=scope,
                out_csv=out_csv,
                out_md=out_md,
            )
            rows = list(csv.DictReader(out_csv.read_text(encoding="utf-8-sig").splitlines()))
            markdown = out_md.read_text(encoding="utf-8")

        self.assertEqual("temporal_ambiguity_policy_decision_sheet", report["report_type"])
        self.assertFalse(report["passed"])
        self.assertEqual("pending_operator_policy_decisions", report["status"])
        self.assertEqual(2, report["summary"]["decision_count"])
        self.assertEqual(2, report["summary"]["pending_release_blocking_decision_count"])
        self.assertEqual(["temporal_ambiguity_index_policy", "temporal_ambiguity_answer_policy"], [row["decision_id"] for row in rows])
        self.assertEqual("pending_operator_decision", rows[0]["decision_status"])
        self.assertEqual("", rows[0]["operator_decision"])
        self.assertEqual("approve_index_with_disclosure; block_ambiguous_indexing", rows[0]["allowed_operator_decisions"])
        self.assertIn("post_policy_readiness_report", rows[0]["minimum_evidence_fields"])
        self.assertEqual("approve_with_disclosure", rows[1]["allowed_operator_decisions"])
        self.assertIn("sample_answer_artifact", rows[1]["minimum_evidence_fields"])
        self.assertIn("Temporal Ambiguity Policy Decision Sheet", markdown)
        self.assertIn("temporal_ambiguity_answer_policy", markdown)
        self.assertIn("Policy Guidance", markdown)

    def test_clear_scope_is_ready_for_policy_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scope = root / "scope.json"
            _write_json(
                scope,
                {
                    "report_type": "temporal_ambiguity_review_scope",
                    "status": "temporal_ambiguity_clear",
                    "passed": True,
                    "decision_requirements": [
                        {
                            "decision_id": "temporal_ambiguity_clear",
                            "required_decision": "No temporal ambiguity release decision is required.",
                            "blocks_product_release": False,
                            "evidence_required": ["temporal ambiguity scope report"],
                        }
                    ],
                },
            )

            report = build_temporal_ambiguity_policy_decision_sheet(scope_report=scope)

        self.assertTrue(report["passed"])
        self.assertEqual("ready_for_policy_validation", report["status"])
        self.assertEqual(0, report["summary"]["pending_release_blocking_decision_count"])
        self.assertEqual("not_required", report["decision_rows"][0]["decision_status"])

    def test_not_passed_scope_without_decision_requirements_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scope = root / "scope.json"
            _write_json(
                scope,
                {
                    "report_type": "temporal_ambiguity_review_scope",
                    "status": "temporal_ambiguity_policy_required",
                    "passed": False,
                },
            )

            report = build_temporal_ambiguity_policy_decision_sheet(scope_report=scope)

        self.assertFalse(report["passed"])
        self.assertEqual("pending_operator_policy_decisions", report["status"])
        self.assertEqual(1, report["summary"]["missing_scope_decision_requirement_count"])
        self.assertEqual(
            {"temporal-scope-decision-requirements-missing"},
            {finding["code"] for finding in report["findings"]},
        )

    def test_cli_writes_json_csv_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scope = root / "scope.json"
            out_json = root / "decisions.json"
            out_csv = root / "decisions.csv"
            out_md = root / "decisions.md"
            _write_json(scope, _scope_payload())
            stdout = io.StringIO()

            exit_code = main(
                [
                    "--scope-report",
                    str(scope),
                    "--out-json",
                    str(out_json),
                    "--out-csv",
                    str(out_csv),
                    "--out-md",
                    str(out_md),
                ],
                stdout=stdout,
            )

            payload = json.loads(out_json.read_text(encoding="utf-8"))
            csv_exists = out_csv.is_file()
            md_exists = out_md.is_file()

        self.assertEqual(0, exit_code)
        self.assertTrue(csv_exists)
        self.assertTrue(md_exists)
        self.assertEqual("temporal_ambiguity_policy_decision_sheet", payload["report_type"])
        self.assertIn('"pending_release_blocking_decision_count": 2', stdout.getvalue())


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


if __name__ == "__main__":
    unittest.main()
