from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = (
    REPO_ROOT / "docs" / "public-institution-operations-runbook.md",
)


class PublicInstitutionOperatorDocsEncodingTests(unittest.TestCase):
    def test_operator_docs_are_valid_utf8_and_public_safe(self) -> None:
        for path in DOCS:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertTrue(text.strip())
                self.assertIn("RAG", text)
                self.assertNotIn("data/private_release_runtime", text)
                self.assertNotIn("github_private_visibility", text)
                self.assertNotIn("CODEX_DIRECTIVES", text)

        runbook = (REPO_ROOT / "docs" / "public-institution-operations-runbook.md").read_text(encoding="utf-8")
        for phrase in [
            "approval_journal_coverage",
            "approval-journal-vector-evidence-missing",
            "--authoritative-artifact mcp_connection_readiness=",
            "mcp_index_visibility_approval_journal_coverage_missing_record_count",
            "approval_journal_coverage.matched_record_count",
        ]:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, runbook)


if __name__ == "__main__":
    unittest.main()
