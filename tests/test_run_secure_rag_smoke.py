from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scripts.run_secure_rag_smoke import main, run_secure_rag_smoke


class SecureRagSmokeTests(unittest.TestCase):
    def test_default_smoke_builds_temp_approval_index_search_evidence_without_path_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_json = Path(tmp) / "secure_rag_smoke.json"
            report = run_secure_rag_smoke(out_json=out_json)

            payload = out_json.read_text(encoding="utf-8")

        self.assertTrue(report["passed"])
        self.assertEqual("temporary", report["data_dir_mode"])
        self.assertTrue(report["synthetic_runtime"])
        self.assertFalse(report["handoff_evidence"])
        self.assertFalse(report["persistent_smoke_data_opt_in"])
        self.assertEqual(report["search_result_count"], 1)
        self.assertEqual(report["evidence_summary"]["vector_record_count"], 1)
        self.assertEqual(report["evidence_summary"]["approval_chain_failure_count"], 0)
        self.assertIn("rag.search", report["evidence_summary"]["api_audit_action_counts"])
        self.assertEqual(len(report["evidence_summary"]["component_manifest_hash"]), 64)
        self.assertEqual(report, json.loads(payload))
        self.assertNotIn("C:" + "\\", payload)
        self.assertNotIn("/tmp/", payload)
        self.assertNotIn("/var/", payload)

    def test_explicit_data_dir_requires_persistent_smoke_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "--allow-persistent-smoke-data"):
                run_secure_rag_smoke(data_dir=Path(tmp) / "data")

    def test_explicit_data_dir_opt_in_reports_non_handoff_synthetic_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = run_secure_rag_smoke(
                data_dir=Path(tmp) / "data",
                allow_persistent_smoke_data=True,
            )

        self.assertTrue(report["passed"])
        self.assertEqual("explicit_persistent_opt_in", report["data_dir_mode"])
        self.assertTrue(report["synthetic_runtime"])
        self.assertFalse(report["handoff_evidence"])
        self.assertTrue(report["persistent_smoke_data_opt_in"])

    def test_cli_refusal_writes_json_failure_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_json = Path(tmp) / "secure_rag_smoke_refused.json"
            argv = [
                "run_secure_rag_smoke.py",
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
