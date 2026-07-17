from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class PrivateReleaseNotesTests(unittest.TestCase):
    def test_private_release_notes_are_excluded_from_source_only_release(self) -> None:
        self.assertFalse((REPO_ROOT / "docs" / "private_release_notes_2026-07-07.md").exists())
        checklist = (REPO_ROOT / "docs" / "public_github_release_checklist_ko.md").read_text(encoding="utf-8")
        self.assertIn("Fresh Clone", checklist)
        self.assertIn("source-only", checklist)


if __name__ == "__main__":
    unittest.main()
