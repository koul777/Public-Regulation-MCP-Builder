from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.check_release_tree_clean import _parse_porcelain, check_release_tree_clean, run


class ReleaseTreeCleanTests(unittest.TestCase):
    def test_parse_porcelain_preserves_index_and_worktree_status(self) -> None:
        changes = _parse_porcelain(
            "D  .github/workflows/ci.yml\n"
            " M app/main.py\n"
            "?? scripts/new_check.py\n"
        )

        self.assertEqual(
            changes,
            [
                {
                    "status": "D ",
                    "path": ".github/workflows/ci.yml",
                    "index_status": "D",
                    "worktree_status": " ",
                },
                {
                    "status": " M",
                    "path": "app/main.py",
                    "index_status": " ",
                    "worktree_status": "M",
                },
                {
                    "status": "??",
                    "path": "scripts/new_check.py",
                    "index_status": "?",
                    "worktree_status": "?",
                },
            ],
        )

    def test_dirty_tree_report_is_fail_closed_and_counted(self) -> None:
        responses = [
            subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout="D  old.yml\n M app.py\n?? new.py\n",
                stderr="",
            ),
            subprocess.CompletedProcess(args=["git"], returncode=0, stdout="abc123\n", stderr=""),
        ]
        with patch("scripts.check_release_tree_clean._git", side_effect=responses):
            report = check_release_tree_clean(Path.cwd())

        self.assertFalse(report["passed"])
        self.assertEqual(report["change_count"], 3)
        self.assertEqual(report["staged_count"], 1)
        self.assertEqual(report["unstaged_count"], 1)
        self.assertEqual(report["untracked_count"], 1)

    def test_git_status_failure_is_not_treated_as_clean(self) -> None:
        response = subprocess.CompletedProcess(
            args=["git"],
            returncode=128,
            stdout="",
            stderr="not a repository",
        )
        with patch("scripts.check_release_tree_clean._git", return_value=response):
            report = check_release_tree_clean(Path.cwd())

        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], "git_status_failed")

    def test_cli_writes_report_and_returns_two_for_dirty_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            out_json = Path(temp_dir) / "tree.json"
            stdout = io.StringIO()
            with patch(
                "scripts.check_release_tree_clean.check_release_tree_clean",
                return_value={"report_type": "release_tree_cleanliness", "passed": False, "changes": []},
            ):
                exit_code = run(["--out-json", str(out_json)], stdout=stdout)

            self.assertEqual(exit_code, 2)
            self.assertFalse(json.loads(out_json.read_text(encoding="utf-8"))["passed"])
            self.assertIn("release_tree_cleanliness", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
