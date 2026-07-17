from __future__ import annotations

import importlib
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackagingEntrypointTests(unittest.TestCase):
    def test_operational_scripts_are_included_in_package_discovery(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        includes = pyproject["tool"]["setuptools"]["packages"]["find"]["include"]

        self.assertIn("scripts*", includes)

    def test_console_scripts_point_to_importable_main_functions(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        scripts = pyproject["project"]["scripts"]

        expected_commands = {
            "reg-rag-batch",
            "reg-rag-public-batch-pipeline",
            "reg-rag-ci-gate",
            "reg-rag-nightly-smoke",
            "reg-rag-audit-release",
            "reg-rag-audit-public-release",
            "reg-rag-plan-public-release-cleanup",
            "reg-rag-public-release-gate",
            "reg-rag-check-private-release",
            "reg-rag-check-github-private",
            "reg-rag-check-console-scripts",
            "reg-rag-release-harness",
            "reg-rag-hermes",
            "reg-rag-sdist-rehearsal",
            "reg-rag-fresh-clone-rehearsal",
            "reg-rag-private-release-gate",
            "reg-rag-private-release-manifest",
            "reg-rag-release-evidence-index",
            "reg-rag-verify-release-evidence",
            "reg-rag-private-release-smoke",
            "reg-rag-public-readiness",
            "reg-rag-review-queue-triage",
            "reg-rag-review-triage-summary",
            "reg-rag-human-review-evidence",
            "reg-rag-approval-evidence",
            "reg-rag-approval-worklist",
            "reg-rag-approval-review-batches",
            "reg-rag-approval-sha-drift-plan",
            "reg-rag-reapproval-evidence",
            "reg-rag-reapproval-worklist",
            "reg-rag-reapproval-review-batches",
            "reg-rag-reapproval-review-burden",
            "reg-rag-reapproval-decision-check",
            "reg-rag-reapproval-apply-plan",
            "reg-rag-reapproval-shadow-apply",
            "reg-rag-profile-registry-from-batch",
            "reg-rag-export-public-report",
            "reg-rag-export-vectordb",
            "reg-rag-export-relations",
            "reg-rag-estimate-agent-review-cost",
            "reg-rag-estimate-embedding-cost",
            "reg-rag-embed-vectors",
            "reg-rag-upsert-vectordb",
            "reg-rag-rag-security-evidence",
            "reg-rag-secure-rag-smoke",
            "reg-rag-mcp-server",
            "reg-rag-mcp-smoke",
            "reg-rag-mcp-transport-smoke",
            "reg-rag-mcp-client-config-smoke",
            "reg-rag-mcp-bundle-zip-extract-smoke",
            "reg-rag-mcp-prepare-runtime",
            "reg-rag-mcp-product-readiness",
            "reg-rag-mcp-temporal-readiness-bundle",
            "reg-rag-mcp-config",
            "reg-rag-mcp-doctor",
            "reg-rag-mcp-handoff-report",
            "reg-rag-mcp-authority",
            "reg-rag-mcp-remediation-plan",
            "reg-rag-mcp-demo-answers",
            "reg-rag-mcp-answer-evidence-bundle",
            "reg-rag-mcp-performance-load-evidence",
            "reg-rag-mcp-cold-start-benchmark",
            "reg-rag-mcp-concurrent-benchmark",
            "reg-rag-mcp-index-visibility",
            "reg-rag-mcp-query-benchmark",
            "reg-rag-revision-impact",
            "reg-rag-real-parser-fixtures",
            "reg-rag-parsing-goldset-start-here",
            "reg-rag-parsing-goldset-table-review-batches",
            "reg-rag-parsing-goldset-table-review-summary",
            "reg-rag-parsing-goldset-table-transfer-check",
            "reg-rag-parsing-goldset-table-source-check",
            "reg-rag-parsing-goldset-table-drift-check",
            "reg-rag-table-preprocessing-claim-gate",
            "reg-rag-pilot-blocker-action-board",
        }
        self.assertLessEqual(expected_commands, set(scripts))

        for target in scripts.values():
            module_name, function_name = target.split(":", 1)
            module = importlib.import_module(module_name)
            self.assertTrue(callable(getattr(module, function_name)))

    def test_readme_documents_current_source_entrypoint(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("START_HERE.bat", readme)
        self.assertIn("Python 3.11 이상", readme)
        self.assertIn(".\\.venv\\Scripts\\python.exe -m streamlit", readme)
        self.assertIn("scripts\\build_windows_portable.ps1", readme)
        self.assertIn("프로젝트 폴더의 `data\\`", readme)

    def test_windows_installer_stops_when_python_or_venv_is_missing(self) -> None:
        installer = (ROOT / "INSTALL_AND_RUN.bat").read_text(encoding="utf-8")

        self.assertIn("if errorlevel 1 goto :python_missing", installer)
        self.assertIn(
            'if not exist ".venv\\Scripts\\python.exe" goto :venv_missing',
            installer,
        )
        self.assertIn("py install 3.11", installer)

    def test_windows_launchers_select_an_available_ui_port(self) -> None:
        batch_launcher = (ROOT / "RUN_APP.bat").read_text(encoding="utf-8")
        packaged_launcher = (ROOT / "packaging" / "windows_launcher.py").read_text(encoding="utf-8")

        self.assertIn("scripts\\find_available_ui_port.py", batch_launcher)
        self.assertIn("--server.port %APP_PORT%", batch_launcher)
        self.assertIn("select_available_port(preferred_ui_port)", packaged_launcher)

    def test_windows_portable_release_includes_readme_link_targets(self) -> None:
        build_script = (ROOT / "scripts" / "build_windows_portable.ps1").read_text(encoding="utf-8")

        for expected_path in (
            "SECURITY.md",
            "THIRD_PARTY_NOTICES.md",
            "docs\\mcp_quickconnect_ko.md",
            "docs\\public_repository_history_policy_ko.md",
        ):
            self.assertIn(expected_path, build_script)

    def test_readme_discloses_kordoc_source_and_bundle_scope(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("https://github.com/chrisryugj/kordoc", readme)
        self.assertIn("https://github.com/chrisryugj/kordoc/blob/main/LICENSE", readme)
        self.assertIn("Kordoc 소스나 실행 파일이 포함되지 않음", readme)
        self.assertIn("THIRD_PARTY_NOTICES.md", readme)


if __name__ == "__main__":
    unittest.main()
