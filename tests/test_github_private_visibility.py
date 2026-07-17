import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.check_github_private_visibility import (
    CommandResult,
    build_visibility_report,
    main,
    parse_github_repo_from_remote,
    sanitize_remote_url,
)


class GitHubPrivateVisibilityTests(unittest.TestCase):
    def test_parses_https_remote(self) -> None:
        self.assertEqual(
            "owner/repo",
            parse_github_repo_from_remote("https://github.com/owner/repo.git"),
        )

    def test_parses_ssh_remote(self) -> None:
        self.assertEqual(
            "owner/repo",
            parse_github_repo_from_remote("git@github.com:owner/repo.git"),
        )

    def test_sanitizes_https_remote_credentials(self) -> None:
        self.assertEqual(
            "https://github.com/owner/repo.git",
            sanitize_remote_url("https://token:x-oauth-basic@github.com/owner/repo.git"),
        )

    def test_build_report_passes_for_private_repo(self) -> None:
        def fake_run_command(args: list[str], cwd: Path) -> CommandResult:
            if args[:3] == ["git", "remote", "get-url"]:
                return CommandResult(0, "https://github.com/owner/repo.git\n", "")
            if args[:2] == ["git", "rev-parse"]:
                return CommandResult(0, "a" * 40 + "\n", "")
            return CommandResult(
                0,
                '{"nameWithOwner":"owner/repo","visibility":"PRIVATE","isPrivate":true,"url":"https://github.com/owner/repo"}',
                "",
            )

        with patch("scripts.check_github_private_visibility.run_command", fake_run_command):
            report = build_visibility_report(
                repo_root=Path("repo"),
                repo=None,
                remote_name="origin",
                generated_at="2026-07-07T00:00:00+00:00",
            )

        self.assertTrue(report["passed"])
        self.assertEqual("github_private_visibility", report["report_type"])
        self.assertEqual("a" * 40, report["repo_commit"])
        self.assertEqual([], report["failed_check_names"])
        self.assertEqual("owner/repo", report["github_repo"])
        self.assertEqual("owner/repo", report["remote_github_repo"])
        self.assertEqual("https://github.com/owner/repo.git", report["remote_url"])
        self.assertNotIn("repo_root", report)

    def test_build_report_fails_for_public_repo(self) -> None:
        def fake_run_command(args: list[str], cwd: Path) -> CommandResult:
            if args[:3] == ["git", "remote", "get-url"]:
                return CommandResult(0, "https://github.com/owner/repo.git\n", "")
            if args[:2] == ["git", "rev-parse"]:
                return CommandResult(0, "a" * 40 + "\n", "")
            return CommandResult(
                0,
                '{"nameWithOwner":"owner/repo","visibility":"PUBLIC","isPrivate":false,"url":"https://github.com/owner/repo"}',
                "",
            )

        with patch("scripts.check_github_private_visibility.run_command", fake_run_command):
            report = build_visibility_report(
                repo_root=Path("repo"),
                repo="owner/repo",
                remote_name="origin",
                generated_at="2026-07-07T00:00:00+00:00",
            )

        self.assertFalse(report["passed"])
        self.assertEqual(["github_repository_private"], report["failed_check_names"])

    def test_build_report_fails_when_explicit_repo_does_not_match_remote(self) -> None:
        calls: list[list[str]] = []

        def fake_run_command(args: list[str], cwd: Path) -> CommandResult:
            calls.append(args)
            if args[:3] == ["git", "remote", "get-url"]:
                return CommandResult(0, "https://github.com/owner/repo.git\n", "")
            if args[:2] == ["git", "rev-parse"]:
                return CommandResult(0, "a" * 40 + "\n", "")
            return CommandResult(0, "{}", "")

        with patch("scripts.check_github_private_visibility.run_command", fake_run_command):
            report = build_visibility_report(
                repo_root=Path("repo"),
                repo="other/repo",
                remote_name="origin",
                generated_at="2026-07-07T00:00:00+00:00",
            )

        self.assertFalse(report["passed"])
        self.assertEqual(["github_repo_matches_remote"], report["failed_check_names"])
        self.assertEqual("owner/repo", report["remote_github_repo"])
        self.assertEqual("other/repo", report["github_repo"])
        self.assertFalse(any(args[:3] == ["gh", "repo", "view"] for args in calls))

    def test_main_writes_failure_report_on_check_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_json = Path(tmp) / "visibility.json"

            with patch(
                "scripts.check_github_private_visibility.run_command",
                return_value=CommandResult(1, "", "gh unavailable"),
            ):
                exit_code = main(["--repo", "owner/repo", "--out-json", str(out_json)])

            self.assertEqual(2, exit_code)
            self.assertIn("github_visibility_check_error", out_json.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
