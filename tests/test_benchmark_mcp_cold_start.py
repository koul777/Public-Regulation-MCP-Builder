from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.benchmark_mcp_cold_start import _child_warmup, benchmark_mcp_cold_start, run


class BenchmarkMcpColdStartTests(unittest.TestCase):
    def test_summarizes_child_warmup_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_json = root / "cold.json"
            out_md = root / "cold.md"

            with patch("scripts.benchmark_mcp_cold_start._run_child_warmup") as run_child:
                run_child.side_effect = [
                    _measurement(iteration=1, process_elapsed=1200.0),
                    _measurement(iteration=2, process_elapsed=900.0),
                    _measurement(iteration=3, process_elapsed=1000.0),
                ]
                report = benchmark_mcp_cold_start(
                    data_dir=root / "data",
                    tenant_id="tenant-demo",
                    iterations=3,
                    tenant_storage_isolation=True,
                    min_record_count=3,
                    max_process_elapsed_ms=1500.0,
                    out_json=out_json,
                    out_md=out_md,
                )

            markdown = out_md.read_text(encoding="utf-8")
            json_written = out_json.is_file()

        self.assertTrue(report["passed"])
        self.assertEqual(3, report["summary"]["successful_count"])
        self.assertEqual(1000.0, report["summary"]["process_elapsed_ms"]["p50"])
        self.assertEqual(1200.0, report["summary"]["process_elapsed_ms"]["p95"])
        self.assertIn("MCP Cold Start Benchmark", markdown)
        self.assertTrue(json_written)

    def test_findings_flag_failed_or_slow_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with patch("scripts.benchmark_mcp_cold_start._run_child_warmup") as run_child:
                run_child.side_effect = [
                    _measurement(iteration=1, process_elapsed=2000.0, record_count=2),
                    _measurement(iteration=2, process_elapsed=900.0, returncode=1, bm25_ready=False),
                ]
                report = benchmark_mcp_cold_start(
                    data_dir=root / "data",
                    tenant_id="tenant-demo",
                    iterations=2,
                    min_record_count=3,
                    max_process_elapsed_ms=1500.0,
                )

        codes = {item["code"] for item in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("cold-start-record-count-below-minimum", codes)
        self.assertIn("cold-start-process-elapsed-too-high", codes)
        self.assertIn("cold-start-child-failed", codes)
        self.assertIn("cold-start-bm25-index-not-ready", codes)

    def test_child_warmup_mode_writes_json(self) -> None:
        stdout = io.StringIO()
        with (
            patch("app.mcp_server.regulation_tools.settings_for_mcp_project", return_value=object()),
            patch("app.mcp_server.regulation_tools.mcp_auth_context", return_value=object()),
            patch(
                "app.mcp_server.regulation_tools.warm_mcp_runtime",
                return_value={"warmed": True, "record_count": 3, "bm25_index_ready": True},
            ),
        ):
            exit_code = _child_warmup(
                data_dir=Path("data"),
                tenant_id="tenant-demo",
                tenant_storage_isolation=True,
                stdout=stdout,
            )

        self.assertEqual(0, exit_code)
        self.assertEqual(3, json.loads(stdout.getvalue())["record_count"])

    def test_cli_can_fail_on_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("scripts.benchmark_mcp_cold_start._run_child_warmup", return_value=_measurement(process_elapsed=2000.0)):
                stdout = io.StringIO()
                exit_code = run(
                    [
                        "--data-dir",
                        str(root / "data"),
                        "--tenant-id",
                        "tenant-demo",
                        "--iterations",
                        "1",
                        "--max-process-elapsed-ms",
                        "1000",
                        "--fail-on-threshold",
                    ],
                    stdout=stdout,
                )

        self.assertEqual(2, exit_code)
        self.assertIn("cold-start-process-elapsed-too-high", stdout.getvalue())


def _measurement(
    *,
    iteration: int = 1,
    process_elapsed: float = 1000.0,
    returncode: int = 0,
    record_count: int = 3,
    bm25_ready: bool = True,
) -> dict:
    return {
        "iteration": iteration,
        "returncode": returncode,
        "process_elapsed_ms": process_elapsed,
        "stderr_tail": "",
        "warmup": {
            "warmed": True,
            "record_count": record_count,
            "bm25_index_ready": bm25_ready,
            "timing_ms": {"total_elapsed_ms": 750.0},
        },
        "record_count": record_count,
        "bm25_index_ready": bm25_ready,
        "warmup_total_elapsed_ms": 750.0,
        "load_vector_records_elapsed_ms": 250.0,
        "approval_snapshot_elapsed_ms": 150.0,
        "bm25_index_elapsed_ms": 100.0,
        "scoring_warmup_elapsed_ms": 250.0,
    }


if __name__ == "__main__":
    unittest.main()
