from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

from app.core.config import Settings
from scripts.check_private_release_readiness import build_readiness_report


class PrivateReleaseReadinessCLITests(unittest.TestCase):
    def test_shared_deployment_readiness_passes_with_private_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            settings = Settings(
                app_env="production",
                data_dir=data_dir,
                api_auth_required=True,
                api_auth_token="secret",
                tenant_storage_isolation=True,
                api_audit_enabled=True,
            )

            report = build_readiness_report(settings, require_shared_deployment=True)

        self.assertTrue(report["passed"])
        self.assertEqual([], report["failed_check_names"])
        self.assertTrue(all(check["passed"] for check in report["checks"]))
        self.assertTrue(report["execution_context"]["same_environment_required"])
        self.assertTrue(report["execution_context"]["path_details_redacted"])
        self.assertEqual(100.0, report["readiness_score"]["percent"])
        self.assertEqual([], report["remediation_plan"])
        self.assertFalse(report["scope"]["checks_external_provider_reachability"])
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(tmp, serialized)
        self.assertNotIn(str(settings.data_dir), serialized)

    def test_shared_deployment_readiness_passes_with_role_specific_token_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            settings = Settings(
                app_env="production",
                data_dir=data_dir,
                api_auth_required=True,
                api_auth_token="",
                api_auth_tokens=json.dumps({"operator-token": {"role": "operator", "actor": "batch-operator"}}),
                tenant_storage_isolation=True,
                api_audit_enabled=True,
            )

            report = build_readiness_report(settings, require_shared_deployment=True)

        self.assertTrue(report["passed"])
        checks = {check["name"]: check for check in report["checks"]}
        self.assertTrue(checks["api_auth_token_nonempty_for_shared_deployment"]["passed"])
        self.assertTrue(checks["explicit_tenant_header_required_for_shared_deployment"]["passed"])

    def test_shared_deployment_readiness_fails_for_invalid_role_token_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            settings = Settings(
                app_env="production",
                data_dir=data_dir,
                api_auth_required=True,
                api_auth_token="",
                api_auth_tokens="{invalid-json",
                tenant_storage_isolation=True,
                api_audit_enabled=True,
            )

            report = build_readiness_report(settings, require_shared_deployment=True)

        self.assertFalse(report["passed"])
        self.assertIn("api_auth_token_nonempty_for_shared_deployment", report["failed_check_names"])
        self.assertIn("explicit_tenant_header_required_for_shared_deployment", report["failed_check_names"])

    def test_shared_deployment_readiness_fails_closed_for_local_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                app_env="local",
                data_dir=Path(tmp) / "data",
                api_auth_required=False,
                api_auth_token="",
                tenant_storage_isolation=False,
                api_audit_enabled=True,
            )

            report = build_readiness_report(settings, require_shared_deployment=True)

        self.assertFalse(report["passed"])
        self.assertIn("app_env_is_production", report["failed_check_names"])
        checks = {check["name"]: check for check in report["checks"]}
        self.assertFalse(checks["app_env_is_production"]["passed"])
        self.assertFalse(checks["api_auth_required_for_shared_deployment"]["passed"])
        self.assertFalse(checks["tenant_storage_isolation_required_for_shared_deployment"]["passed"])
        self.assertFalse(checks["explicit_tenant_header_required_for_shared_deployment"]["passed"])
        self.assertGreater(len(report["remediation_plan"]), 0)
        remediation = {item["check_name"]: item for item in report["remediation_plan"]}
        self.assertIn("API_AUTH_REQUIRED=true", remediation["api_auth_required_for_shared_deployment"]["action"])
        self.assertLess(report["readiness_score"]["percent"], 100.0)

    def test_shared_deployment_readiness_fails_for_unscoped_repository_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository_dir = data_dir / "repository"
            repository_dir.mkdir(parents=True)
            (repository_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "documents": {
                            "doc_legacy": {
                                "document_id": "doc_legacy",
                                "filename": "legacy.pdf",
                            }
                        },
                        "jobs": {},
                        "runs": {},
                    }
                ),
                encoding="utf-8",
            )
            settings = Settings(
                app_env="production",
                data_dir=data_dir,
                api_auth_required=True,
                api_auth_token="secret",
                tenant_storage_isolation=True,
                api_audit_enabled=True,
            )

            report = build_readiness_report(settings, require_shared_deployment=True)

        self.assertFalse(report["passed"])
        self.assertIn(
            "no_unscoped_repository_records_for_shared_deployment",
            report["failed_check_names"],
        )
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(
            1,
            checks["no_unscoped_repository_records_for_shared_deployment"]["unscoped_record_counts"]["documents"],
        )

    def test_shared_deployment_readiness_checks_tenant_isolated_repository_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            tenant_repository_dir = data_dir / "tenants" / "tenant-a" / "repository"
            tenant_repository_dir.mkdir(parents=True)
            (tenant_repository_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "documents": {},
                        "jobs": {
                            "job_legacy": {
                                "job_id": "job_legacy",
                                "document_id": "doc_legacy",
                            }
                        },
                        "runs": {},
                    }
                ),
                encoding="utf-8",
            )
            settings = Settings(
                app_env="production",
                data_dir=data_dir,
                api_auth_required=True,
                api_auth_token="secret",
                tenant_storage_isolation=True,
                api_audit_enabled=True,
            )

            report = build_readiness_report(settings, require_shared_deployment=True)

        self.assertFalse(report["passed"])
        checks = {check["name"]: check for check in report["checks"]}
        self.assertTrue(checks["explicit_tenant_header_required_for_shared_deployment"]["passed"])
        tenant_check = checks["no_unscoped_repository_records_for_shared_deployment"]
        self.assertEqual(1, tenant_check["unscoped_record_counts"]["jobs"])
        self.assertEqual(["tenant-a"], tenant_check["tenant_keys_checked"])
        self.assertEqual(["tenants/tenant-a/repository/manifest.json"], tenant_check["tenant_repository_files_checked"])


if __name__ == "__main__":
    unittest.main()

import unittest as _unittest_private_release_readiness_stdout


class PrivateReleaseReadinessOutJsonStdoutTests(_unittest_private_release_readiness_stdout.TestCase):
    def test_out_json_stdout_redacts_absolute_path(self):
        import contextlib
        import io
        import json
        import sys
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        from scripts import check_private_release_readiness as readiness_cli

        report = {
            "report_type": "private_release_readiness",
            "passed": True,
            "execution_context": {"path_details_redacted": True},
            "checks": [],
        }

        with TemporaryDirectory() as tmp:
            out_json = Path(tmp) / "nested" / "private_release_readiness.json"
            stdout = io.StringIO()
            argv = ["check_private_release_readiness.py", "--out-json", str(out_json)]
            with patch.object(sys, "argv", argv), patch.object(
                readiness_cli,
                "build_readiness_report",
                return_value=dict(report),
            ), contextlib.redirect_stdout(stdout):
                exit_code = readiness_cli.main()

            stdout_text = stdout.getvalue()
            printed = json.loads(stdout_text)
            written = json.loads(out_json.read_text(encoding="utf-8"))

            self.assertEqual(0, exit_code)
            self.assertTrue(out_json.exists())
            self.assertEqual(report, written)
            self.assertTrue(printed["out_json"]["path_redacted"])
            self.assertEqual(out_json.name, printed["out_json"]["filename"])
            self.assertNotIn(str(out_json), stdout_text)
            self.assertNotIn(str(Path(tmp)), stdout_text)
