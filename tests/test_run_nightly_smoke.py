from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.run_nightly_smoke import run_nightly_smoke


class RunNightlySmokeTests(unittest.TestCase):
    def test_default_smoke_fails_closed_without_generated_release_artifacts(self) -> None:
        report = run_nightly_smoke(project_root=Path(__file__).resolve().parents[1])

        self.assertFalse(report["passed"])
        self.assertEqual(report["api_call_count"], 0)
        self.assertFalse(any(check["name"] == "ci_regression_gate" and check["passed"] for check in report["checks"]))
        regression_check = next(check for check in report["checks"] if check["name"] == "ci_regression_gate")
        self.assertIn("release_hygiene", regression_check["details"])

    def test_missing_artifacts_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            report = run_nightly_smoke(project_root=Path(tmpdir))

            self.assertFalse(report["passed"])
            self.assertEqual(report["api_call_count"], 0)
            self.assertFalse(any(check["name"] == "ci_regression_gate" and check["passed"] for check in report["checks"]))
            self.assertFalse(any(check["name"] == "qdrant_local_export_smoke" and check["passed"] for check in report["checks"]))
            self.assertTrue(all("details" in check for check in report["checks"]))
            self.assertTrue(any(check["name"] != "ci_regression_gate" and check["details"].get("reason") == "missing_artifact" for check in report["checks"]))

    def test_missing_optional_artifacts_can_be_advisory_for_fresh_clone_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            report = run_nightly_smoke(project_root=Path(tmpdir), require_optional_artifacts=False)

            self.assertFalse(report["passed"])
            self.assertFalse(report["require_optional_artifacts"])
            optional_checks = [check for check in report["checks"] if check["name"] != "ci_regression_gate"]
            self.assertTrue(optional_checks)
            self.assertTrue(all(check["passed"] for check in optional_checks))
            self.assertTrue(all(check["details"].get("reason") == "missing_optional_artifact" for check in optional_checks))
