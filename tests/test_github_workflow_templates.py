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

    def test_auto_release_runs_public_gates_before_versioning_and_publish(self) -> None:
        path = REPO_ROOT / ".github" / "workflows" / "auto-release.yml"
        text = path.read_text(encoding="utf-8")

        hygiene_command = (
            "python scripts/audit_release_hygiene.py --workflow-scope available "
            "--include-untracked --include-source-path-scan"
        )
        public_gate_command = (
            "python scripts/run_public_release_gate.py --root . "
            "--include-untracked --fail-on-blocked"
        )
        release_steps = (
            "Determine and write the release version",
            "Build release artifacts",
            "Build Windows portable ZIP",
            "Commit the release version and tag it",
            "Publish GitHub Release",
        )

        self.assertIn(hygiene_command, text)
        self.assertIn(public_gate_command, text)
        hygiene_index = text.index(hygiene_command)
        public_gate_index = text.index(public_gate_command)
        for release_step in release_steps:
            release_step_index = text.index(release_step)
            self.assertLess(hygiene_index, release_step_index)
            self.assertLess(public_gate_index, release_step_index)

    def test_auto_release_grants_write_only_to_the_release_job_after_verification(self) -> None:
        path = REPO_ROOT / ".github" / "workflows" / "auto-release.yml"
        text = path.read_text(encoding="utf-8")

        verify_start = text.index("\n  verify:\n")
        release_start = text.index("\n  release:\n")
        self.assertLess(verify_start, release_start)
        workflow_header = text[:verify_start]
        verify_job = text[verify_start:release_start]
        release_job = text[release_start:]

        # Workflow default is read-only; the single write grant sits on the release job.
        self.assertIn("\npermissions:\n  contents: read\n", workflow_header)
        self.assertNotIn("contents: write", workflow_header)
        self.assertNotIn("contents: write", verify_job)
        self.assertEqual(1, text.count("contents: write"))
        self.assertIn("    permissions:\n      contents: write\n", release_job)
        self.assertIn("    needs: verify\n", release_job)

        skip_condition = (
            "if: github.event_name != 'push' || "
            "!contains(github.event.head_commit.message, '[skip auto-release]')"
        )
        self.assertIn(skip_condition, verify_job)
        self.assertIn(skip_condition, release_job)

        # verify holds every test and public gate and none of the publishing operations.
        for command in (
            'python -m pip install ".[dev]"',
            "python -m unittest discover -s tests -v",
            (
                "python scripts/audit_release_hygiene.py --workflow-scope available "
                "--include-untracked --include-source-path-scan"
            ),
            (
                "python scripts/run_public_release_gate.py --root . "
                "--include-untracked --fail-on-blocked"
            ),
        ):
            self.assertIn(command, verify_job)
        self.assertIn("persist-credentials: false", verify_job)
        self.assertIn("sha: ${{ steps.verified.outputs.sha }}", verify_job)
        for publishing_operation in (
            "git push",
            "git tag -a",
            "gh release",
            "python -m build",
            "build_windows_portable.ps1",
        ):
            self.assertNotIn(publishing_operation, verify_job)

        # release rebuilds the exact verified commit and keeps the ordered release steps.
        self.assertIn("ref: ${{ needs.verify.outputs.sha }}", release_job)
        self.assertNotIn("ref: main", release_job)
        # If main moved after verification, fail closed instead of rebasing onto
        # commits that no gate verified, and compare before anything is pushed.
        self.assertIn("VERIFIED_SHA: ${{ needs.verify.outputs.sha }}", release_job)
        self.assertNotIn("git rebase", release_job)
        compare_index = release_job.index("git ls-remote origin refs/heads/main")
        self.assertLess(compare_index, release_job.index("git push origin HEAD:main"))
        self.assertLess(compare_index, release_job.index("git tag -a"))
        release_steps = (
            'python -m pip install ".[dev]"',
            "Determine and write the release version",
            "Build release artifacts",
            "Build Windows portable ZIP",
            "Commit the release version and tag it",
            "Publish GitHub Release",
        )
        positions = [release_job.index(step) for step in release_steps]
        self.assertEqual(sorted(positions), positions)
        self.assertLess(release_job.index("actions/checkout@v4"), positions[0])
        self.assertLess(release_job.index("actions/setup-python@v5"), positions[0])

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
        self.assertIn("tests.test_vector_ingestion_adapter", text)
        self.assertIn("tests.test_bm25_index", text)
        self.assertIn("tests.test_bm25_structured_metadata", text)
        self.assertIn("tests.test_hierarchical_index", text)
        self.assertIn("tests.test_input_packaging_parity", text)
        self.assertIn("tests.test_generate_mcp_client_config", text)
        self.assertIn("tests.test_run_mcp_client_config_smoke", text)
        self.assertIn("tests.test_run_mcp_transport_smoke", text)
        self.assertIn("tests.test_check_mcp_connection_readiness", text)
        self.assertIn("tests.test_beginner_workflow_services", text)
        self.assertIn("tests.test_local_llm_doctor", text)
        self.assertIn("tests.test_qwen_chat_app", text)
        self.assertIn("tests.test_streamlit_ai_usage_path", text)
        self.assertIn("tests.test_streamlit_approval_helpers", text)
        self.assertIn("tests.test_hidden_process", text)
        self.assertIn("tests.test_local_http", text)
        self.assertIn("tests.test_ollama_runtime", text)
        self.assertIn("python -m build --sdist --wheel", text)
        self.assertIn("--include-source-path-scan", text)

    def test_beginner_contracts_run_before_the_broader_regression_suite(self) -> None:
        path = REPO_ROOT / ".github" / "workflows" / "preprocessing-regression.yml"
        text = path.read_text(encoding="utf-8")
        start = text.index("- name: Run beginner service and recovery contracts first")
        end = text.index("- name: Run parsing and preprocessing regression suite")
        self.assertLess(start, end)
        fast_step = text[start:end]
        for module in (
            "test_readiness_adapter", "test_local_llm_readiness_service",
            "test_indexing_readiness_service",
            "test_operator_setup_service", "test_local_app_service",
            "test_authoring_service", "test_authoring_official_isolation",
            "test_streamlit_setup", "test_streamlit_authoring",
            "test_streamlit_approval_app",
            "test_ai_review_display",
        ):
            self.assertIn(f"tests.{module}", fast_step)
            self.assertTrue((REPO_ROOT / "tests" / f"{module}.py").is_file())
        self.assertNotIn("continue-on-error", fast_step)
        modules = [line for line in fast_step.splitlines() if line.strip().startswith("tests.")]
        for line in modules[:-1]:
            self.assertTrue(line.rstrip().endswith("\\"), f"Shell would execute module as a command: {line.strip()}")

    def test_preprocessing_regression_runs_tenant_security_and_approval_modules(self) -> None:
        path = REPO_ROOT / ".github" / "workflows" / "preprocessing-regression.yml"
        text = path.read_text(encoding="utf-8")

        suite_name = "- name: Run parsing and preprocessing regression suite"
        security_name = "- name: Run tenant isolation, API security, approval and repository contracts"
        large_name = "- name: Run large API route and MCP tool suites"
        build_name = "- name: Build source and wheel distributions"
        self.assertLess(text.index(suite_name), text.index(security_name))
        self.assertLess(text.index(security_name), text.index(large_name))
        self.assertLess(text.index(large_name), text.index(build_name))

        steps = (
            (
                text[text.index(security_name):text.index(large_name)],
                (
                    "test_api_security", "test_api_tenant_isolation", "test_tenant_access",
                    "test_repository", "test_repository_journal_integrity",
                    "test_approval_governance", "test_approval_governance_invariants",
                    "test_retrieval_security", "test_official_rag_approval_gate_policy",
                ),
            ),
            (
                text[text.index(large_name):text.index(build_name)],
                ("test_routes_rag", "test_routes_documents", "test_regulation_mcp_tools"),
            ),
        )
        for step, modules in steps:
            self.assertNotIn("continue-on-error", step)
            module_lines = [line for line in step.splitlines() if line.strip().startswith("tests.")]
            listed = [line.strip().rstrip("\\").strip() for line in module_lines]
            for module in modules:
                self.assertIn(f"tests.{module}", listed)
                self.assertTrue((REPO_ROOT / "tests" / f"{module}.py").is_file())
            for line in module_lines[:-1]:
                self.assertTrue(
                    line.rstrip().endswith("\\"),
                    f"Shell would execute module as a command: {line.strip()}",
                )

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
        self.assertNotIn("--connection-mode openai-tunnel", text)
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
        self.assertNotIn("--connection-mode openai-tunnel", text)
        self.assertIn("--allow-missing-optional-artifacts", text)


if __name__ == "__main__":
    unittest.main()
