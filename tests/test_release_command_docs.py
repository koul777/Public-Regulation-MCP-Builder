from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class ReleaseCommandDocsTests(unittest.TestCase):
    def test_public_release_gate_uses_supported_blocking_flag(self) -> None:
        documents = (
            REPO_ROOT / "docs" / "operator_quickstart_ko.md",
            REPO_ROOT / "docs" / "public-institution-operations-runbook.md",
        )

        for document in documents:
            with self.subTest(document=document.name):
                text = document.read_text(encoding="utf-8")
                self.assertIn("run_public_release_gate.py", text)
                self.assertIn("--fail-on-blocked", text)
                self.assertNotRegex(
                    text,
                    r"run_public_release_gate\.py[^\n]*--fail-on-issue",
                )


if __name__ == "__main__":
    unittest.main()
