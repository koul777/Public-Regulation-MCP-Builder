from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from scripts.run_large_document_preprocessing_stress import (
    _performance_gate,
    build_benchmark_validity,
    build_host_runtime_evidence,
    run_large_document_preprocessing_stress,
)


class LargeDocumentPreprocessingStressTests(unittest.TestCase):
    def test_host_runtime_evidence_returns_factual_non_attributive_fields(self) -> None:
        evidence = build_host_runtime_evidence(wall_seconds=4.0, process_cpu_seconds=1.5)

        self.assertEqual(evidence["wall_seconds"], 4.0)
        self.assertEqual(evidence["process_cpu_seconds"], 1.5)
        self.assertEqual(evidence["wall_minus_process_cpu_seconds"], 2.5)
        self.assertEqual(evidence["process_cpu_to_wall_ratio"], 0.375)
        cpu_heavier_evidence = build_host_runtime_evidence(wall_seconds=1.0, process_cpu_seconds=1.5)
        self.assertEqual(cpu_heavier_evidence["wall_minus_process_cpu_seconds"], 0.0)
        self.assertIn("os_cpu_count", evidence)
        self.assertTrue(evidence["platform_system"])
        self.assertTrue(evidence["platform_machine"])
        self.assertTrue(evidence["python_implementation"])
        self.assertRegex(evidence["python_major_minor"], r"^\d+\.\d+$")
        self.assertNotIn("hostname", evidence)
        self.assertNotIn("host_cpu_percent", evidence)
        self.assertNotIn("io_wait", evidence)

    def test_unconfigured_performance_gate_is_non_blocking_and_explicit(self) -> None:
        gate = _performance_gate(
            elapsed_seconds=12.0,
            peak_tracemalloc_mb=34.0,
            pages_per_second=8.0,
            max_elapsed_seconds=None,
            max_peak_tracemalloc_mb=None,
            min_pages_per_second=None,
        )

        self.assertTrue(gate["passed"])
        self.assertFalse(gate["configured"])
        self.assertEqual(gate["status"], "not_configured")
        self.assertEqual(gate["violations"], [])

    def test_performance_gate_reports_all_budget_violations(self) -> None:
        gate = _performance_gate(
            elapsed_seconds=12.0,
            peak_tracemalloc_mb=34.0,
            pages_per_second=8.0,
            max_elapsed_seconds=10.0,
            max_peak_tracemalloc_mb=30.0,
            min_pages_per_second=9.0,
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["status"], "failed")
        self.assertEqual(
            [violation["metric"] for violation in gate["violations"]],
            ["elapsed_seconds", "peak_tracemalloc_mb", "pages_per_second"],
        )

    def test_nonpositive_budget_is_rejected_before_artifact_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(ValueError, "max_elapsed_seconds"):
                run_large_document_preprocessing_stress(
                    page_count=1,
                    data_dir=root / "runtime",
                    sample_pdf=root / "sample.pdf",
                    out_json=root / "report.json",
                    out_md=root / "report.md",
                    max_elapsed_seconds=0,
                )
            self.assertFalse((root / "sample.pdf").exists())

    def test_nonfinite_budget_is_rejected_before_artifact_generation(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                with self.assertRaisesRegex(ValueError, "finite value"):
                    run_large_document_preprocessing_stress(
                        page_count=1,
                        data_dir=root / "runtime",
                        sample_pdf=root / "sample.pdf",
                        out_json=root / "report.json",
                        out_md=root / "report.md",
                        max_elapsed_seconds=value,
                    )
                self.assertFalse((root / "sample.pdf").exists())

    def test_small_document_run_emits_traceable_functional_and_performance_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            out_json = root / "report.json"
            out_md = root / "report.md"
            report = run_large_document_preprocessing_stress(
                page_count=2,
                data_dir=root / "runtime",
                sample_pdf=root / "sample.pdf",
                out_json=out_json,
                out_md=out_md,
                include_table_rows=False,
                force_regenerate_pdf=True,
                max_elapsed_seconds=120.0,
                max_peak_tracemalloc_mb=256.0,
                min_pages_per_second=0.01,
            )

            self.assertTrue(report["passed"])
            self.assertTrue(report["functional_passed"])
            self.assertEqual(report["page_count_requested"], 2)
            self.assertEqual(report["document"]["page_count"], 2)
            self.assertEqual(len(report["source_pdf_sha256"]), 64)
            self.assertEqual(report["performance_gate"]["status"], "passed")
            self.assertEqual(report["performance_gate"]["violations"], [])
            self.assertEqual(report["benchmark_validity"]["gate_status"], "passed")
            self.assertEqual(
                report["benchmark_validity"]["observed_sla"], report["performance_gate"]["observed"]
            )
            self.assertFalse(report["benchmark_validity"]["diagnostic_evidence_changes_pass_fail"])
            self.assertEqual(report["benchmark_validity"]["host_contention_assessment"], "not_established")
            self.assertIn("same-bytes reference benchmark", report["benchmark_validity"]["causal_attribution_note"])
            self.assertIn("process_cpu_seconds", report["host_runtime_evidence"])
            self.assertEqual(json.loads(out_json.read_text(encoding="utf-8"))["passed"], True)
            self.assertIn("Performance gate: `passed`", out_md.read_text(encoding="utf-8"))
            self.assertIn("Gate preservation: diagnostics change pass/fail: `false`", out_md.read_text(encoding="utf-8"))

    def test_runtime_diagnostics_do_not_override_a_failed_performance_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report = run_large_document_preprocessing_stress(
                page_count=1,
                data_dir=root / "runtime",
                sample_pdf=root / "sample.pdf",
                out_json=root / "report.json",
                out_md=root / "report.md",
                include_table_rows=False,
                force_regenerate_pdf=True,
                min_pages_per_second=1_000_000.0,
            )

            self.assertEqual(report["performance_gate"]["status"], "failed")
            self.assertFalse(report["performance_gate"]["passed"])
            self.assertFalse(report["passed"])
            self.assertFalse(report["benchmark_validity"]["diagnostic_evidence_changes_pass_fail"])
            self.assertEqual(report["benchmark_validity"]["gate_status"], "failed")

        failed_gate = _performance_gate(
            elapsed_seconds=12.0,
            peak_tracemalloc_mb=34.0,
            pages_per_second=8.0,
            max_elapsed_seconds=10.0,
            max_peak_tracemalloc_mb=None,
            min_pages_per_second=None,
        )
        validity = build_benchmark_validity(failed_gate)
        self.assertEqual(validity["gate_status"], "failed")
        self.assertEqual(validity["observed_sla"], failed_gate["observed"])


if __name__ == "__main__":
    unittest.main()
