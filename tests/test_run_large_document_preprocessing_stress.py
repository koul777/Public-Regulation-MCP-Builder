from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.run_large_document_preprocessing_stress import (
    _performance_gate,
    run_large_document_preprocessing_stress,
)


class LargeDocumentPreprocessingStressTests(unittest.TestCase):
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
            self.assertEqual(json.loads(out_json.read_text(encoding="utf-8"))["passed"], True)
            self.assertIn("Performance gate: `passed`", out_md.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
