from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class GitHubWorkflowTemplatesTests(unittest.TestCase):
    def test_auto_release_replaces_the_complete_semantic_version_line(self) -> None:
        path = REPO_ROOT / ".github" / "workflows" / "auto-release.yml"
        text = path.read_text(encoding="utf-8")

        self.assertIn("contents[:match.start()]", text)
        self.assertIn("contents[match.end():]", text)
        self.assertNotIn("match.start(1)", text)
        self.assertNotIn("match.end(1)", text)

    def test_preprocessing_policy_never_executes_pull_request_code(self) -> None:
        path = REPO_ROOT / ".github" / "workflows" / "preprocessing-change-policy.yml"
        text = path.read_text(encoding="utf-8")

        self.assertIn("pull_request_target:", text)
        self.assertIn("ref: ${{ github.event.pull_request.base.sha }}", text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn("gh api --paginate", text)
        self.assertIn("scripts/check_preprocessing_change_guard.py", text)
        self.assertNotIn("github.event.pull_request.head.sha", text)

    def test_preprocessing_regression_runs_protected_suite_and_release_checks(self) -> None:
        path = REPO_ROOT / ".github" / "workflows" / "preprocessing-regression.yml"
        text = path.read_text(encoding="utf-8")

        self.assertIn("pull_request:", text)
        self.assertIn("runs-on: windows-latest", text)
        self.assertIn("shell: bash", text)
        self.assertIn("tests.test_preprocessing_change_guard", text)
        self.assertIn("tests.test_deployment_defaults", text)
        self.assertIn("tests.test_hwpx_parser", text)
        self.assertIn("tests.test_table_extractor", text)
        self.assertIn("tests.test_processing_service", text)
        self.assertIn("tests.test_generate_mcp_client_config", text)
        self.assertIn("tests.test_run_mcp_client_config_smoke", text)
        self.assertIn("tests.test_run_mcp_transport_smoke", text)
        self.assertIn("tests.test_check_mcp_connection_readiness", text)
        self.assertIn("python -m build --sdist --wheel", text)
        self.assertIn("--include-source-path-scan", text)

    def test_ci_template_exercises_mcp_connection_paths(self) -> None:
        path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
        if not path.exists():
            self.skipTest("GitHub workflow templates are optional in source-only distributions.")
        text = path.read_text(encoding="utf-8")

        self.assertIn("scripts/run_mcp_smoke.py", text)
        self.assertIn("reg-rag-mcp-transport-smoke", text)
        self.assertIn("reports/mcp_transport_smoke_ci.json", text)
        self.assertIn("reg-rag-release-harness", text)
        self.assertIn("reg-rag-sdist-rehearsal", text)
        self.assertIn("reg-rag-fresh-clone-rehearsal", text)
        self.assertIn("reg-rag-hermes", text)
        self.assertIn("reports/hermes_mcp_check_ci.json", text)
        self.assertIn("reports/fresh_clone_rehearsal_plan_ci.json", text)
        self.assertIn("reports/sdist_rehearsal_ci.json", text)
        self.assertIn("reports/release_harness_mcp_ci.json", text)
        self.assertIn("public-release-audit", text)
        self.assertIn("reg-rag-audit-public-release", text)
        self.assertIn("reg-rag-public-release-gate", text)
        self.assertIn("reports/public_release_gate_ci.json", text)
        self.assertIn("--execute-harness", text)
        self.assertIn("--fail-on-blocked", text)
        self.assertIn("python -m pip install -e . build", text)
        self.assertIn("python -m pip install . build", text)
        self.assertIn("scripts/generate_mcp_client_config.py", text)
        self.assertIn("scripts/check_mcp_connection_readiness.py", text)
        self.assertIn("reg-rag-check-console-scripts", text)
        self.assertIn("reports/installed_console_scripts_ci.json", text)
        self.assertIn("--zip-out reports/mcp_connection_bundle_ci.zip", text)
        self.assertIn("--include-wheel", text)
        self.assertIn("--bundle-dir reports/mcp_connection_bundle_ci", text)
        self.assertIn("--connection-mode openai-tunnel", text)
        self.assertIn("MCP_AUTH_TOKEN=ci-token", text)

    def test_nightly_template_uploads_mcp_artifacts(self) -> None:
        path = REPO_ROOT / ".github" / "workflows" / "nightly.yml"
        if not path.exists():
            self.skipTest("GitHub workflow templates are optional in source-only distributions.")
        text = path.read_text(encoding="utf-8")

        self.assertIn("reports/mcp_smoke_nightly.json", text)
        self.assertIn("reports/mcp_transport_smoke_nightly.json", text)
        self.assertIn("reports/release_harness_mcp_nightly.json", text)
        self.assertIn("reports/hermes_mcp_check_nightly.json", text)
        self.assertIn("reg-rag-hermes", text)
        self.assertIn("--fail-on-attention", text)
        self.assertIn("--include-wheel-in-bundle", text)
        self.assertIn("reg-rag-release-evidence-index --profile hermes-mcp", text)
        self.assertIn("reg-rag-verify-release-evidence", text)
        self.assertIn("reports/hermes_release_evidence_index_current.json", text)
        self.assertIn("reports/hermes_release_evidence_verification_current.json", text)
        self.assertIn("reg-rag-fresh-clone-rehearsal", text)
        self.assertIn("reports/fresh_clone_rehearsal_plan_nightly.json", text)
        self.assertIn("reports/sdist_rehearsal_nightly.json", text)
        self.assertIn("reg-rag-sdist-rehearsal", text)
        self.assertIn("reports/public_release_gate_nightly.json", text)
        self.assertIn("reg-rag-public-release-gate", text)
        self.assertIn("reports/installed_console_scripts_nightly.json", text)
        self.assertIn("reports/mcp_client_bundle_nightly.json", text)
        self.assertIn("reports/mcp_connection_bundle_nightly.zip", text)
        self.assertIn("reports/mcp_connection_bundle_nightly/", text)
        self.assertIn("--include-wheel", text)
        self.assertIn("--bundle-dir reports/mcp_connection_bundle_nightly", text)
        self.assertIn("--connection-mode openai-tunnel", text)
        self.assertIn("--allow-missing-optional-artifacts", text)


if __name__ == "__main__":
    unittest.main()
