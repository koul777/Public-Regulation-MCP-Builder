from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scripts.run_mcp_smoke import main, run_mcp_smoke


class RunMcpSmokeTests(unittest.TestCase):
    def test_run_mcp_smoke_passes_with_synthetic_data(self) -> None:
        report = run_mcp_smoke(
            tenant_id="tenant-mcp-smoke",
            tenant_storage_isolation=True,
        )

        self.assertTrue(report["passed"])
        self.assertEqual("temporary", report["data_dir_mode"])
        self.assertTrue(report["synthetic_runtime"])
        self.assertFalse(report["handoff_evidence"])
        self.assertFalse(report["persistent_smoke_data_opt_in"])
        self.assertGreaterEqual(report["search_result_count"], 1)
        self.assertTrue(report["fetch_has_text"])
        self.assertGreaterEqual(report["article_count"], 1)
        self.assertGreaterEqual(report["table_count"], 1)
        self.assertGreaterEqual(report["comparison_summary"]["changed_count"], 1)
        self.assertTrue(report["evidence_summary"]["passed"])
        self.assertEqual(report["evidence_summary"]["approval_record_count"], 2)
        self.assertEqual(report["evidence_summary"]["approval_vector_sync_outcome_count"], 2)
        self.assertEqual(report["evidence_summary"]["approval_vector_sync_legacy_approval_count"], 0)
        self.assertFalse(
            report["evidence_summary"]["approval_vector_sync_policy"]["legacy_approvals_grandfathered"]
        )
        self.assertEqual(report["evidence_summary"]["approval_vector_sync_failure_count"], 0)
        self.assertEqual(report["evidence_summary"]["approval_vector_sync_failure_samples"], [])

    def test_run_mcp_smoke_refuses_explicit_empty_runtime_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "--allow-persistent-smoke-data"):
                run_mcp_smoke(
                    data_dir=Path(tmp) / "data",
                    tenant_id="tenant-mcp-smoke",
                    tenant_storage_isolation=True,
                )

    def test_run_mcp_smoke_allows_explicit_runtime_with_persistent_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = run_mcp_smoke(
                data_dir=Path(tmp) / "data",
                tenant_id="tenant-mcp-smoke",
                tenant_storage_isolation=True,
                allow_persistent_smoke_data=True,
            )

        self.assertTrue(report["passed"])
        self.assertEqual("explicit_persistent_opt_in", report["data_dir_mode"])
        self.assertTrue(report["persistent_smoke_data_opt_in"])
        self.assertFalse(report["handoff_evidence"])

    def test_run_mcp_smoke_refuses_existing_non_smoke_runtime_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository_dir = data_dir / "tenants" / "tenant-mcp-smoke" / "repository"
            repository_dir.mkdir(parents=True)
            (repository_dir / "doc_real_chunks.json").write_text("[]", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "existing non-smoke runtime"):
                run_mcp_smoke(
                    data_dir=data_dir,
                    tenant_id="tenant-mcp-smoke",
                    tenant_storage_isolation=True,
                    allow_persistent_smoke_data=True,
                )

    def test_cli_refusal_writes_json_failure_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_json = Path(tmp) / "mcp_smoke_refused.json"
            argv = [
                "run_mcp_smoke.py",
                "--data-dir",
                str(Path(tmp) / "data"),
                "--out-json",
                str(out_json),
            ]
            stdout = StringIO()
            with patch("sys.argv", argv), redirect_stdout(stdout):
                exit_code = main()

            payload = json.loads(out_json.read_text(encoding="utf-8"))
            printed = json.loads(stdout.getvalue())

        self.assertEqual(2, exit_code)
        self.assertFalse(payload["passed"])
        self.assertEqual("explicit_refused", payload["data_dir_mode"])
        self.assertFalse(payload["handoff_evidence"])
        self.assertIn("--allow-persistent-smoke-data", payload["error"])
        self.assertEqual(payload, printed)


if __name__ == "__main__":
    unittest.main()
