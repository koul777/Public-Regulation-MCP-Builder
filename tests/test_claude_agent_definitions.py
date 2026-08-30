from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = REPO_ROOT / ".claude" / "agents"


class ClaudeAgentDefinitionTests(unittest.TestCase):
    def test_source_distribution_includes_project_agents(self) -> None:
        manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertIn("recursive-include .claude *.md", manifest)

    def test_project_agents_are_read_only_and_separate_security_from_release(self) -> None:
        expected = {
            "regulation-security-auditor.md": (
                "name: regulation-security-auditor",
                "tenant isolation",
                "approval",
            ),
            "regulation-release-reviewer.md": (
                "name: regulation-release-reviewer",
                "release-gate",
                "CODEOWNERS",
            ),
        }

        for filename, required_phrases in expected.items():
            with self.subTest(filename=filename):
                text = (AGENT_DIR / filename).read_text(encoding="utf-8")
                self.assertTrue(text.startswith("---\n"))
                self.assertIn("tools: Read, Grep, Glob", text)
                self.assertNotIn("Edit", text)
                self.assertNotIn("Write", text)
                for phrase in required_phrases:
                    self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
