from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.run_ci_regression_gate import run_regression_gate


class RunCIRegressionGateTests(unittest.TestCase):
    def test_default_gates_fail_closed_when_release_artifacts_are_absent(self) -> None:
        report = run_regression_gate(project_root=Path(__file__).resolve().parents[1])

        self.assertFalse(report["passed"])
        self.assertEqual(report["gate_count"], 2)
        self.assertTrue(all(not gate["passed"] for gate in report["gates"]))
        self.assertTrue(all(gate["reason"] == "missing_batch_report" for gate in report["gates"]))

    def test_fallback_is_used_when_configured_gates_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "reports").mkdir(parents=True)
            (root / "tests" / "fixtures" / "regression").mkdir(parents=True)
            batch_report = {
                "rows": [
                    {
                        "document_id": "doc-001",
                        "source_system": "PUBLIC_PORTAL",
                        "source_record_id": "R-001",
                        "source_file_id": "F-001",
                        "chunk_to_source_char_ratio": 0.123,
                    }
                ]
            }
            fixtures = {
                "fixtures": [
                    {
                        "identity": "PUBLIC_PORTAL:R-001:F-001",
                        "metrics": {
                            "chunk_to_source_char_ratio": 0.123,
                        },
                    }
                ]
            }
            batch_path = root / "reports" / "public_batch_quality_latest.json"
            expectation_path = root / "tests" / "fixtures" / "regression" / "public_portal_quality_expectations_latest.json"
            batch_path.write_text(json.dumps(batch_report, ensure_ascii=False, indent=2), encoding="utf-8")
            expectation_path.write_text(json.dumps(fixtures, ensure_ascii=False, indent=2), encoding="utf-8")

            report = run_regression_gate(
                project_root=root,
                gates=(
                    {
                        "name": "public_portal_fallback_gate",
                        "batch_report": "reports/missing_batch.json",
                        "batch_report_fallback_pattern": "reports/public_batch_quality_*.json",
                        "expectations": "tests/fixtures/regression/missing_expectations.json",
                        "expectations_fallback_pattern": "tests/fixtures/regression/public_portal_quality_expectations_*.json",
                        "required_fixture_count": 1,
                    },
                ),
            )

            self.assertTrue(report["passed"])
            self.assertEqual(report["gate_count"], 1)
            self.assertTrue(report["gates"][0]["passed"])
            self.assertTrue(report["gates"][0]["batch_report_was_fallback"])
            self.assertTrue(report["gates"][0]["expectations_was_fallback"])
            self.assertTrue(any("configured_missing" in item for item in report["gates"][0]["fallback_details"]))

    def test_release_hygiene_gate_can_be_included(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "reports").mkdir(parents=True)
            (root / "tests" / "fixtures" / "regression").mkdir(parents=True)
            (root / "safe.txt").write_text("safe release artifact\n", encoding="utf-8")
            batch_report = {
                "rows": [
                    {
                        "document_id": "doc-001",
                        "source_system": "PUBLIC_PORTAL",
                        "source_record_id": "R-001",
                        "source_file_id": "F-001",
                        "chunk_to_source_char_ratio": 0.123,
                    }
                ]
            }
            fixtures = {
                "fixtures": [
                    {
                        "identity": "PUBLIC_PORTAL:R-001:F-001",
                        "metrics": {
                            "chunk_to_source_char_ratio": 0.123,
                        },
                    }
                ]
            }
            batch_path = root / "reports" / "public_batch_quality_latest.json"
            expectation_path = root / "tests" / "fixtures" / "regression" / "public_portal_quality_expectations_latest.json"
            batch_path.write_text(json.dumps(batch_report, ensure_ascii=False, indent=2), encoding="utf-8")
            expectation_path.write_text(json.dumps(fixtures, ensure_ascii=False, indent=2), encoding="utf-8")

            with mock.patch(
                "scripts.run_ci_regression_gate.audit_release_hygiene.collect_candidate_paths",
                return_value=["safe.txt"],
            ):
                report = run_regression_gate(
                    project_root=root,
                    gates=(
                        {
                            "name": "public_portal_fallback_gate",
                            "batch_report": "reports/missing_batch.json",
                            "batch_report_fallback_pattern": "reports/public_batch_quality_*.json",
                            "expectations": "tests/fixtures/regression/missing_expectations.json",
                            "expectations_fallback_pattern": "tests/fixtures/regression/public_portal_quality_expectations_*.json",
                            "required_fixture_count": 1,
                        },
                    ),
                    include_release_hygiene=True,
                    release_hygiene_workflow_scope="available",
                )

        self.assertTrue(report["passed"])
        self.assertTrue(report["release_hygiene"]["passed"])
        self.assertEqual(0, report["release_hygiene"]["finding_count"])
