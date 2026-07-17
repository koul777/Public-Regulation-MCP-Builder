from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class PrivateReleaseScriptExecutionTests(unittest.TestCase):
    def test_private_release_readiness_script_runs_from_source_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **os.environ,
                "APP_ENV": "production",
                "API_AUTH_REQUIRED": "true",
                "API_AUTH_TOKEN": "secret",
                "TENANT_STORAGE_ISOLATION": "true",
                "API_AUDIT_ENABLED": "true",
                "DATA_DIR": str(Path(tmp) / "data"),
            }
            Path(env["DATA_DIR"]).mkdir(parents=True)

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/check_private_release_readiness.py",
                    "--require-shared-deployment",
                ],
                cwd=REPO_ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )

        report = json.loads(result.stdout)
        self.assertTrue(report["passed"])
        self.assertEqual("private_release_readiness", report["report_type"])

    def test_private_release_manifest_script_runs_from_source_tree(self):
        result = subprocess.run(
            [
                sys.executable,
                "scripts/build_private_release_manifest.py",
                "--include-release-hygiene-result",
                "--workflow-scope",
                "available",
            ],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )

        manifest = json.loads(result.stdout)
        self.assertEqual("private_release_handoff", manifest["manifest_type"])
        self.assertEqual(0, manifest["release_hygiene"]["observed_result"]["exit_code"])

    def test_private_release_gate_script_runs_from_source_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            smoke_report = Path(tmp) / "private_release_smoke_current.json"
            smoke_report.write_text(
                json.dumps(
                    {
                        "report_type": "private_release_smoke",
                        "passed": True,
                        "data_dir_mode": "explicit",
                        "handoff_evidence": True,
                        "http": {
                            "unauthorized_upload_status_code": 401,
                            "missing_tenant_upload_status_code": 400,
                            "authorized_upload_status_code": 200,
                        },
                        "audit": {
                            "passed": True,
                            "auth_denial_passed": True,
                            "tenant_header_required_passed": True,
                        },
                        "exports": [
                            {"format": "jsonl", "status_code": 200, "exists": True},
                            {"format": "csv", "status_code": 200, "exists": True},
                            {"format": "markdown", "status_code": 200, "exists": True},
                            {"format": "tables_jsonl", "status_code": 200, "exists": True},
                            {"format": "tables_csv", "status_code": 200, "exists": True},
                            {"format": "quality_json", "status_code": 200, "exists": True},
                            {"format": "quality_md", "status_code": 200, "exists": True},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/run_private_release_gate.py",
                    "--workflow-scope",
                    "available",
                    "--allow-local-deployment",
                    "--allow-dirty-worktree",
                    "--dirty-worktree-approval",
                    "TEST-DIRTY-TREE",
                    "--smoke-report",
                    str(smoke_report),
                ],
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("private_release_gate", result.stdout)

    def test_private_release_gate_help_exposes_official_rag_mcp_evidence_flags(self):
        result = subprocess.run(
            [
                sys.executable,
                "scripts/run_private_release_gate.py",
                "--help",
            ],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )

        self.assertIn("--require-official-rag-mcp-evidence", result.stdout)
        self.assertIn("--mcp-handoff-report", result.stdout)
        self.assertIn("--mcp-release-evidence-verification-report", result.stdout)

    def test_private_release_manifest_defaults_to_invocation_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "build_private_release_manifest.py"),
                ],
                cwd=tmp,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )

        manifest = json.loads(result.stdout)
        self.assertEqual(Path(tmp).name, manifest["project_root_name"])


if __name__ == "__main__":
    unittest.main()
