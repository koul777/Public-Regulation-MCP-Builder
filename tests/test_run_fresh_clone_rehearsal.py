from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.run_fresh_clone_rehearsal import (
    build_fresh_clone_rehearsal_report,
    build_fresh_clone_steps,
    run,
)


class FreshCloneRehearsalTests(unittest.TestCase):
    def test_quick_plan_uses_source_only_tests_and_harness_dry_run(self) -> None:
        steps = build_fresh_clone_steps(clone_root=Path("clone"), mode="public", full=False, python_executable="python")

        names = [step.name for step in steps]

        self.assertEqual(["source_only_tests", "release_harness_plan"], names)
        harness_step = steps[-1]
        self.assertIn("--dry-run", harness_step.command)
        self.assertIn("--mode", harness_step.command)
        self.assertIn("public", harness_step.command)

    def test_full_plan_installs_package_and_runs_full_harness(self) -> None:
        steps = build_fresh_clone_steps(clone_root=Path("clone"), mode="internal", full=True, python_executable="python")

        names = [step.name for step in steps]

        self.assertEqual(["create_venv", "install_package", "source_only_tests", "release_harness_full"], names)
        self.assertIn(".[dev]", steps[1].command)
        self.assertNotIn("-e", steps[1].command)
        self.assertIn("PATH", steps[1].env)
        self.assertIn("PATH", steps[-1].env)
        self.assertNotIn("--dry-run", steps[-1].command)
        self.assertIn("--keep-going", steps[-1].command)

    def test_missing_source_reports_high_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = build_fresh_clone_rehearsal_report(source_root=Path(tmp) / "missing", dry_run=True)

        self.assertFalse(report["passed"])
        self.assertEqual("source-root-missing", report["issues"][0]["code"])
        self.assertEqual(1, report["high_count"])

    def test_cli_writes_json_and_returns_nonzero_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_json = Path(tmp) / "fresh_clone.json"
            stdout = io.StringIO()

            exit_code = run(
                [
                    "--source-root",
                    str(Path(tmp) / "missing"),
                    "--dry-run",
                    "--out-json",
                    str(out_json),
                    "--json",
                    "--fail-on-issue",
                ],
                stdout=stdout,
            )
            payload = json.loads(out_json.read_text(encoding="utf-8"))

        self.assertEqual(1, exit_code)
        self.assertEqual("fresh_clone_rehearsal", payload["report_type"])
        self.assertIn('"passed": false', stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
