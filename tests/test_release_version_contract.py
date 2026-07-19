from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

from app import __version__
from app.main import app


REPO_ROOT = Path(__file__).resolve().parents[1]


class ReleaseVersionContractTests(unittest.TestCase):
    def test_auto_release_reads_single_version_source_and_publishes_all_artifacts(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "auto-release.yml").read_text(encoding="utf-8")

        self.assertIn('path = Path("app/__init__.py")', workflow)
        self.assertIn('^__version__ =', workflow)
        self.assertIn("scripts\\build_windows_portable.ps1", workflow)
        self.assertIn("dist/reg_rag_preprocessor-${VERSION}-py3-none-any.whl", workflow)
        self.assertIn("dist/reg_rag_preprocessor-${VERSION}.tar.gz", workflow)
        self.assertIn("dist/PR-MCP-Builder-Windows-x64-${VERSION}.zip", workflow)
        self.assertIn('gh release upload "$TAG" "${artifacts[@]}" --clobber', workflow)
        self.assertIn('gh release create "$TAG" "${artifacts[@]}"', workflow)

    def test_application_version_is_semantic_and_shared_with_fastapi(self) -> None:
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")
        self.assertEqual(app.version, __version__)

    def test_python_package_reads_the_application_version(self) -> None:
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(pyproject["project"]["dynamic"], ["version"])
        self.assertNotIn("version", pyproject["project"])
        self.assertEqual(
            pyproject["tool"]["setuptools"]["dynamic"]["version"]["attr"],
            "app.__version__",
        )

    def test_readme_does_not_pin_a_stale_release_version(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertNotRegex(readme, re.compile(r"PR-MCP-Builder-Windows-x64-\d+\.\d+\.\d+\.zip"))
        self.assertIn("releases/latest", readme)


if __name__ == "__main__":
    unittest.main()
