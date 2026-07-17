from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class PrivateReleaseRunbookTests(unittest.TestCase):
    def test_private_release_runbooks_are_excluded_from_source_only_release(self) -> None:
        for filename in (
            "private_release_runbook.md",
            "private_release_incident_response.md",
        ):
            with self.subTest(filename=filename):
                self.assertFalse((REPO_ROOT / "docs" / filename).exists())
        self.assertTrue((REPO_ROOT / "docs" / "public_github_release_checklist_ko.md").exists())
        self.assertTrue((REPO_ROOT / "SECURITY.md").exists())


if __name__ == "__main__":
    unittest.main()
