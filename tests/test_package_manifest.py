from __future__ import annotations

import unittest
from pathlib import Path
import tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]


class PackageManifestTests(unittest.TestCase):
    def test_project_metadata_uses_readme_as_markdown_long_description(self) -> None:
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(
            pyproject["project"]["readme"],
            {"file": "README.md", "content-type": "text/markdown"},
        )

    def test_manifest_includes_public_docs_needed_by_tests(self) -> None:
        text = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")

        self.assertIn("include docs/mcp_quickconnect_ko.md", text)
        self.assertIn("include docs/harness_engineering_plan_ko.md", text)
        self.assertIn("include docs/hermes_engineering_plan_ko.md", text)
        self.assertIn("include docs/ui_ux_release_scope_ko.md", text)
        self.assertIn("include docs/mcp_client_config_examples_ko.md", text)
        self.assertIn("include docs/public_release_report_allowlist.json", text)
        self.assertIn("include AGENTS.md", text)
        self.assertIn("include CONTRIBUTING.md", text)
        self.assertIn("include SECURITY.md", text)
        self.assertIn("include THIRD_PARTY_NOTICES.md", text)
        self.assertIn("include *.bat", text)
        self.assertIn("recursive-include tests *.py", text)
        self.assertIn("recursive-include scripts *.ps1", text)
        self.assertIn("recursive-include packaging *.py *.spec *.txt", text)

    def test_manifest_excludes_runtime_and_private_artifacts(self) -> None:
        text = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")

        self.assertIn("prune data", text)
        self.assertIn("prune reports", text)
        self.assertNotIn("private_release", text)
        self.assertNotIn("internal_mcp_operation", text)


if __name__ == "__main__":
    unittest.main()
