from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.check_github_publish_owner_decisions import build_owner_decision_gate, run


FIELDS = (
    "decision_id",
    "workstream",
    "summary",
    "input_count",
    "inputs",
    "decision",
    "decision_owner",
    "decision_reference",
    "notes",
)


class GithubPublishOwnerDecisionGateTests(unittest.TestCase):
    def test_blocks_blank_owner_decision_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decisions = root / "decisions.csv"
            summary = root / "summary.json"
            _write_decisions(
                decisions,
                [
                    {
                        "decision_id": "license_selection",
                        "workstream": "source_only_github_publish",
                        "summary": "Choose license.",
                        "input_count": "1",
                        "inputs": "LICENSE",
                    },
                    {
                        "decision_id": "nonpublic_doc_policy",
                        "workstream": "source_only_github_publish",
                        "summary": "Remove or rewrite private docs.",
                        "input_count": "6",
                        "inputs": "docs/private_release_checklist.md",
                    },
                ],
            )
            _write_json(
                summary,
                {
                    "report_type": "github_publish_readiness_summary",
                    "owner_decisions_required": [
                        {"decision_id": "license_selection"},
                        {"decision_id": "nonpublic_doc_policy"},
                    ],
                },
            )

            report = build_owner_decision_gate(
                decisions_csv=decisions,
                readiness_summary_report=summary,
            )

        self.assertFalse(report["passed"])
        self.assertEqual("blocked_pending_owner_decisions", report["status"])
        self.assertEqual(2, report["decision_count"])
        self.assertEqual(0, report["complete_decision_count"])
        self.assertEqual(2, report["incomplete_decision_count"])
        self.assertEqual(
            {"decision": 2, "decision_owner": 2, "decision_reference": 2},
            report["required_field_missing_counts"],
        )
        self.assertEqual([], report["missing_expected_decision_ids"])
        self.assertEqual(6, report["blocker_count"])

    def test_passes_completed_owner_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decisions = root / "decisions.csv"
            summary = root / "summary.json"
            _write_decisions(
                decisions,
                [
                    {
                        "decision_id": "license_selection",
                        "workstream": "source_only_github_publish",
                        "summary": "Choose license.",
                        "input_count": "1",
                        "inputs": "LICENSE",
                        "decision": "Use Apache-2.0",
                        "decision_owner": "owner@example.com",
                        "decision_reference": "LEGAL-1",
                    }
                ],
            )
            _write_json(
                summary,
                {
                    "report_type": "github_publish_readiness_summary",
                    "owner_decisions_required": [{"decision_id": "license_selection"}],
                },
            )

            report = build_owner_decision_gate(
                decisions_csv=decisions,
                readiness_summary_report=summary,
            )

        self.assertTrue(report["passed"])
        self.assertEqual("owner_decisions_ready", report["status"])
        self.assertEqual(1, report["complete_decision_count"])
        self.assertEqual(0, report["blocker_count"])

    def test_flags_missing_expected_decision_from_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decisions = root / "decisions.csv"
            summary = root / "summary.json"
            _write_decisions(
                decisions,
                [
                    {
                        "decision_id": "license_selection",
                        "workstream": "source_only_github_publish",
                        "summary": "Choose license.",
                        "decision": "Use Apache-2.0",
                        "decision_owner": "owner@example.com",
                        "decision_reference": "LEGAL-1",
                    }
                ],
            )
            _write_json(
                summary,
                {
                    "owner_decisions_required": [
                        {"decision_id": "license_selection"},
                        {"decision_id": "sample_redistribution_policy"},
                    ]
                },
            )

            report = build_owner_decision_gate(
                decisions_csv=decisions,
                readiness_summary_report=summary,
            )

        self.assertFalse(report["passed"])
        self.assertIn("sample_redistribution_policy", report["missing_expected_decision_ids"])
        self.assertIn("missing_expected_decision", {blocker["code"] for blocker in report["blockers"]})

    def test_cli_writes_reports_and_can_fail_on_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decisions = root / "decisions.csv"
            out_json = root / "gate.json"
            out_md = root / "gate.md"
            _write_decisions(
                decisions,
                [
                    {
                        "decision_id": "license_selection",
                        "workstream": "source_only_github_publish",
                        "summary": "Choose license.",
                    }
                ],
            )
            stdout = io.StringIO()

            exit_code = run(
                [
                    "--decisions-csv",
                    str(decisions),
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                    "--json",
                    "--fail-on-blocker",
                ],
                stdout=stdout,
            )

            payload = json.loads(out_json.read_text(encoding="utf-8"))
            markdown = out_md.read_text(encoding="utf-8")

        self.assertEqual(1, exit_code)
        self.assertEqual("github_publish_owner_decision_gate", payload["report_type"])
        self.assertIn("blocked_pending_owner_decisions", stdout.getvalue())
        self.assertIn("GitHub Publish Owner Decision Gate", markdown)


def _write_decisions(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
