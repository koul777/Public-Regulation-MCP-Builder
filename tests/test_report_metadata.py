from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.report_metadata import current_repo_commit


class ReportMetadataTests(unittest.TestCase):
    def test_current_repo_commit_returns_none_when_git_is_unavailable(self) -> None:
        with patch("scripts.report_metadata.subprocess.run", side_effect=FileNotFoundError("git")):
            self.assertIsNone(current_repo_commit(Path("missing-git-checkout")))

    def test_current_repo_commit_returns_valid_hash(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout=("a" * 40 + "\n").encode("ascii"),
            stderr=b"",
        )
        with patch("scripts.report_metadata.subprocess.run", return_value=completed):
            self.assertEqual(current_repo_commit(Path("repo")), "a" * 40)


if __name__ == "__main__":
    unittest.main()
