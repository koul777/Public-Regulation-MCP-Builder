from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class InternalMcpOperationDocsTests(unittest.TestCase):
    def test_internal_operation_doc_is_excluded_from_source_only_release(self) -> None:
        self.assertFalse((REPO_ROOT / "docs" / "internal_mcp_operation_ko.md").exists())
        self.assertTrue((REPO_ROOT / "docs" / "public_github_release_checklist_ko.md").exists())


if __name__ == "__main__":
    unittest.main()
