from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from scripts.build_private_release_manifest import build_manifest


class PrivateReleaseManifestTests(unittest.TestCase):
    def test_manifest_captures_private_release_contract(self):
        manifest = build_manifest(Path(__file__).resolve().parents[1])

        self.assertEqual("private_release_handoff", manifest["manifest_type"])
        self.assertEqual(1, manifest["manifest_version"])
        self.assertIn("dirty", manifest["repo_status"])
        self.assertIn("changed_path_count", manifest["repo_status"])
        self.assertEqual("/ready", manifest["deployment"]["readiness_endpoint"]["value"])
        self.assertFalse(manifest["deployment"]["readiness_auth_required"]["value"])
        self.assertEqual("local-ui", manifest["streamlit_local_profile"]["profile_name"]["value"])
        self.assertEqual("127.0.0.1:8501", manifest["streamlit_local_profile"]["host_publish_address"]["value"])
        self.assertFalse(manifest["streamlit_local_profile"]["allowed_for_shared_or_protected_use"]["value"])
        self.assertEqual("synchronous", manifest["processing_contract"]["process_mode"]["value"])
        self.assertIn("tables_jsonl", manifest["exports"]["table_formats"])
        self.assertIn("quality_json", manifest["exports"]["quality_formats"])
        self.assertIn("--require-shared-deployment", manifest["release_gates"]["private_release_readiness"]["command"])
        self.assertIn("--include-source-path-scan", manifest["release_gates"]["ci_gate"]["command"])
        self.assertIn("--workflow-scope unavailable", manifest["release_gates"]["ci_gate"]["command"])
        self.assertEqual("unavailable", manifest["release_hygiene"]["workflow_scope_mode"]["value"])
        self.assertEqual(
            "local_or_non_isolated_single_tenant_only",
            manifest["auth_tenant_defaults"]["x_tenant_id_runtime_fallback_scope"]["value"],
        )
        self.assertTrue(
            manifest["auth_tenant_defaults"]["x_tenant_id_required_when_tenant_storage_isolation_enabled"]["value"]
        )
        self.assertTrue(
            manifest["auth_tenant_defaults"]["x_tenant_id_required_for_shared_public_institution_pilots"]["value"]
        )
        self.assertEqual(
            "single_tenant_local_demo_only",
            manifest["auth_tenant_defaults"]["api_default_tenant_id_scope"]["value"],
        )
        self.assertEqual(0, manifest["provider_posture"]["agent_review_expected_api_call_count"]["value"])
        self.assertFalse(manifest["provider_posture"]["network_provider_calls_allowed_by_default"]["value"])

    def test_manifest_can_embed_release_hygiene_result(self):
        root = Path(__file__).resolve().parents[1]
        with mock.patch(
            "scripts.build_private_release_manifest.audit_release_hygiene.collect_candidate_paths",
            return_value=["README.md"],
        ):
            manifest = build_manifest(
                root,
                include_release_hygiene_result=True,
                workflow_scope="available",
                include_source_path_scan=True,
            )

        result = manifest["release_hygiene"]["observed_result"]
        self.assertEqual(0, result["exit_code"])
        self.assertEqual(0, result["finding_count"])
        self.assertEqual("available", manifest["release_hygiene"]["workflow_scope_mode"]["value"])
        self.assertIn("--include-source-path-scan", manifest["release_hygiene"]["script_command"]["command"])


if __name__ == "__main__":
    unittest.main()

import unittest as _unittest_private_release_manifest_audit_paths


class PrivateReleaseManifestAuditPathScopeTests(_unittest_private_release_manifest_audit_paths.TestCase):
    def test_manifest_documents_base_and_tenant_audit_paths(self):
        import json
        import subprocess
        import sys
        from pathlib import Path
        from tempfile import TemporaryDirectory

        repo_root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as tmp:
            out_json = Path(tmp) / "private_release_manifest.json"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/build_private_release_manifest.py",
                    "--out-json",
                    str(out_json),
                ],
                cwd=repo_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            manifest = json.loads(out_json.read_text(encoding="utf-8"))

        defaults = manifest["auth_tenant_defaults"]
        self.assertEqual(
            "DATA_DIR/repository/api_audit.jsonl",
            defaults["api_audit_base_path"]["value"],
        )
        self.assertEqual(
            "DATA_DIR/tenants/<safe-tenant-id>/repository/api_audit.jsonl",
            defaults["api_audit_tenant_scoped_path"]["value"],
        )
        self.assertIn(
            "tenant-scoped audit log",
            defaults["api_audit_path_scope"]["value"],
        )
