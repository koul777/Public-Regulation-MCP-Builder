from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_worktree_commit_plan import build_worktree_commit_plan, main


class BuildWorktreeCommitPlanTests(unittest.TestCase):
    def test_groups_private_release_gate_hardening_together(self) -> None:
        report = build_worktree_commit_plan(
            status_lines=[
                " M scripts/run_private_release_gate.py",
                " M tests/test_private_release_gate.py",
                " M tests/test_private_release_script_execution.py",
                " M docs/private_release_checklist.md",
                " M docs/private_release_runbook.md",
                " M docs/operator_quickstart_ko.md",
            ]
        )

        release_slice = _slice(report, "release_handoff_safety")
        self.assertEqual(6, release_slice["path_count"])
        self.assertIn("scripts/run_private_release_gate.py", release_slice["paths"])
        self.assertIn("docs/operator_quickstart_ko.md", release_slice["paths"])
        self.assertTrue(
            any("tests.test_private_release_gate" in command for command in release_slice["verification_commands"])
        )

    def test_groups_parser_and_generated_artifacts_separately(self) -> None:
        report = build_worktree_commit_plan(
            status_lines=[
                "?? scripts/build_parsing_goldset_completion_board.py",
                "?? tests/test_build_parsing_goldset_completion_board.py",
                "?? reports/parsing_goldset_completion_board_current.json",
                "?? data/aks_mcp_publish_runtime/vector_records.jsonl",
            ]
        )

        parser_slice = _slice(report, "parsing_accuracy_evidence")
        generated_slice = _slice(report, "generated_runtime_reports")
        self.assertEqual(2, parser_slice["path_count"])
        self.assertEqual(2, generated_slice["path_count"])
        self.assertIn("reports/parsing_goldset_completion_board_current.json", generated_slice["paths"])
        self.assertIn("data/aks_mcp_publish_runtime/vector_records.jsonl", generated_slice["paths"])

    def test_renamed_paths_are_classified_by_destination(self) -> None:
        report = build_worktree_commit_plan(
            status_lines=[
                "R  docs/old.md -> docs/private_release_runbook.md",
            ]
        )

        release_slice = _slice(report, "release_handoff_safety")
        self.assertEqual(["docs/private_release_runbook.md"], release_slice["paths"])

    def test_cli_writes_json_and_markdown_from_real_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_json = Path(tmp) / "commit_plan.json"
            out_md = Path(tmp) / "commit_plan.md"
            stdout = io.StringIO()

            exit_code = main(["--out-json", str(out_json), "--out-md", str(out_md)], stdout=stdout)

            payload = json.loads(out_json.read_text(encoding="utf-8"))
            markdown = out_md.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertEqual("worktree_commit_plan", payload["report_type"])
        self.assertIn("Worktree Commit Plan", markdown)
        self.assertIn("recommended_sequence", stdout.getvalue())


def _slice(report: dict[str, object], slice_id: str) -> dict[str, object]:
    return next(row for row in report["slices"] if row["slice_id"] == slice_id)


if __name__ == "__main__":
    unittest.main()
